# -*- coding: utf-8 -*-
"""Immutable research outcomes for Prediction Ledger rows."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import json
import math
from typing import Any, Dict, Optional

from src.core.backtest_engine import BacktestEngine
from src.repositories.prediction_ledger_repo import PredictionLedgerRepository
from src.repositories.prediction_outcome_repo import PredictionOutcomeRepository
from src.repositories.stock_repo import StockRepository
from src.services.pit_identity import build_bar_sequence_identity, canonical_json, sha256_payload
from src.storage import DatabaseManager, utc_naive_now


PREDICTION_OUTCOME_ENGINE_VERSION = "prediction-outcome-fixed-horizon-v2"
PRIMARY_LABEL_IDENTITY = "META_TAKE_NET_POSITIVE_NEXT_OPEN_3S_FIXED_CLOSE_V1"
PRIMARY_HORIZON_IDENTITY = "XSHG_POSTMARKET_NEXT_OPEN_3_FORWARD_SESSIONS_FIXED_CLOSE_V1"

_COST_SCHEMA_VERSION = "cost-identity-v2"
_MINIMUM_FEE_POLICY = "PER_SIDE_MAX_NOTIONAL_RATE_OR_MINIMUM_CNY"
_ALLOWED_COMMISSION_BASES = {
    "ALL_IN_INCLUDES_EXCHANGE_HANDLING_AND_REGULATORY_LEVY_OTHER_IS_TRANSFER_ONLY",
    "NET_EXCLUDES_EXCHANGE_HANDLING_AND_REGULATORY_LEVY_OTHER_INCLUDES_THEM",
}
_COST_TEXT_FIELDS = (
    "schema_version",
    "market",
    "instrument_type",
    "exchange",
    "currency",
    "effective_from",
    "source",
    "version",
    "cost_model_mode",
    "commission_basis",
    "minimum_fee_policy",
)
_COST_NUMERIC_FIELDS = (
    "buy_fee_rate",
    "sell_fee_rate",
    "sell_tax_rate",
    "other_buy_rate",
    "other_sell_rate",
    "buy_slippage_bps",
    "sell_slippage_bps",
    "minimum_commission_cny",
    "reference_entry_notional_cny",
)


class PredictionOutcomeService:
    def __init__(
        self,
        *,
        ledger_repo: Optional[PredictionLedgerRepository] = None,
        outcome_repo: Optional[PredictionOutcomeRepository] = None,
        stock_repo: Optional[StockRepository] = None,
        db_manager: Optional[DatabaseManager] = None,
    ):
        self.db = db_manager or DatabaseManager.get_instance()
        self.ledger_repo = ledger_repo or PredictionLedgerRepository(self.db)
        self.outcome_repo = outcome_repo or PredictionOutcomeRepository(self.db)
        self.stock_repo = stock_repo or StockRepository(self.db)

    def evaluate_prediction(
        self,
        *,
        prediction_hash: str,
        cost_identity: Mapping[str, Any],
        correction_reason: Optional[str] = None,
        engine_version: str = PREDICTION_OUTCOME_ENGINE_VERSION,
    ) -> Dict[str, Any]:
        ledger = self.ledger_repo.get_by_prediction_hash(prediction_hash)
        if ledger is None:
            raise ValueError(f"prediction not found: {prediction_hash}")
        if not self._is_meta_opportunity(ledger.evidence_json):
            return {"status": "NOT_ELIGIBLE", "prediction_hash": prediction_hash}
        if ledger.data_as_of is None:
            return {"status": "UNLABELABLE", "reason": "DATA_AS_OF_NOT_BOUND"}

        normalized_cost = self.normalize_cost_identity(cost_identity)
        self._validate_cost_identity_for_ledger(normalized_cost, ledger)
        cost_hash = sha256_payload(normalized_cost)
        root_identity = {
            "prediction_hash": ledger.prediction_hash,
            "label_identity": PRIMARY_LABEL_IDENTITY,
            "horizon_identity": PRIMARY_HORIZON_IDENTITY,
            "cost_identity_hash": cost_hash,
            "evaluation_engine_version": str(engine_version),
        }
        root_hash = sha256_payload(root_identity)

        bars = self.stock_repo.get_forward_bars(
            code=ledger.stock_code,
            analysis_date=ledger.data_as_of,
            eval_window_days=3,
        )
        if len(bars) < 3:
            return {
                "status": "UNMATURED",
                "prediction_hash": ledger.prediction_hash,
                "required_forward_sessions": 3,
                "observed_forward_sessions": len(bars),
            }

        if not self._cost_identity_covers_sessions(
            normalized_cost,
            entry_session=bars[0].date,
            exit_session=bars[2].date,
        ):
            evaluation = {
                "eval_status": "unlabelable",
                "execution_state": "EXECUTION_UNKNOWN",
                "unable_reason": "COST_IDENTITY_OUT_OF_RANGE",
                "entry_session": bars[0].date,
                "exit_session": bars[2].date,
            }
        else:
            evaluation = BacktestEngine.evaluate_fixed_horizon_take(
                forward_bars=bars,
                cost_identity=normalized_cost,
                eval_window_days=3,
            )
        data_identity = build_bar_sequence_identity(
            bars[:3],
            stock_code=ledger.stock_code,
            market=ledger.market,
            purpose="prediction-outcome-fixed-horizon-v1",
        )
        available_at = utc_naive_now()
        if evaluation.get("eval_status") == "completed":
            net_return = float(evaluation["net_return_pct"])
            label_value = 1 if net_return > 0 else 0
            label_status = "TAKE_SUCCESS" if label_value == 1 else "TAKE_FAIL"
            label_reason = None
        else:
            label_value = None
            label_status = "UNLABELABLE"
            label_reason = str(evaluation.get("unable_reason") or "EXECUTION_UNKNOWN")

        substantive = {
            **root_identity,
            "data_snapshot_identity": data_identity["data_snapshot_identity"],
            "execution_state": evaluation.get("execution_state") or "EXECUTION_UNKNOWN",
            "entry_session": self._date_text(evaluation.get("entry_session")),
            "exit_session": self._date_text(evaluation.get("exit_session")),
            "entry_price": self._finite(evaluation.get("entry_price")),
            "exit_price": self._finite(evaluation.get("exit_price")),
            "gross_return_pct": self._finite(evaluation.get("gross_return_pct")),
            "net_return_pct": self._finite(evaluation.get("net_return_pct")),
            "label_value": label_value,
            "label_status": label_status,
            "label_reason": label_reason,
        }
        outcome_hash = sha256_payload(substantive)
        fields = {
            "outcome_hash": outcome_hash,
            "root_identity_hash": root_hash,
            "prediction_hash": ledger.prediction_hash,
            "label_identity": PRIMARY_LABEL_IDENTITY,
            "horizon_identity": PRIMARY_HORIZON_IDENTITY,
            "cost_identity_hash": cost_hash,
            "cost_identity_json": canonical_json(normalized_cost),
            "evaluation_engine_version": str(engine_version),
            "decision_session": ledger.data_as_of,
            "entry_session": evaluation.get("entry_session"),
            "exit_session": evaluation.get("exit_session"),
            "execution_state": evaluation.get("execution_state") or "EXECUTION_UNKNOWN",
            "entry_price": self._finite(evaluation.get("entry_price")),
            "exit_price": self._finite(evaluation.get("exit_price")),
            "gross_return_pct": self._finite(evaluation.get("gross_return_pct")),
            "net_return_pct": self._finite(evaluation.get("net_return_pct")),
            "max_adverse_excursion_pct": self._finite(
                evaluation.get("max_adverse_excursion_pct")
            ),
            "max_favorable_excursion_pct": self._finite(
                evaluation.get("max_favorable_excursion_pct")
            ),
            "label_value": label_value,
            "label_status": label_status,
            "label_reason": label_reason,
            "data_snapshot_identity": data_identity["data_snapshot_identity"],
            "provider_identity": data_identity.get("provider_identity"),
            "adjustment_basis": data_identity.get("adjustment_basis"),
            "available_at": available_at,
        }
        stored, disposition = self.outcome_repo.persist_terminal(
            fields,
            correction_reason=correction_reason,
        )
        return {
            "status": label_status,
            "label_value": label_value,
            "prediction_hash": ledger.prediction_hash,
            "outcome_hash": stored["outcome_hash"],
            "disposition": disposition,
            "supersedes_outcome_hash": stored["supersedes_outcome_hash"],
        }

    @classmethod
    def normalize_cost_identity(cls, value: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("cost_identity must be an object")
        normalized: Dict[str, Any] = {}
        for key in _COST_TEXT_FIELDS:
            text = str(value.get(key) or "").strip()
            if not text:
                raise ValueError(f"missing cost identity field: {key}")
            normalized[key] = text

        if normalized["schema_version"] != _COST_SCHEMA_VERSION:
            raise ValueError("unsupported cost identity schema_version")
        normalized["market"] = normalized["market"].lower()
        normalized["instrument_type"] = normalized["instrument_type"].lower()
        normalized["exchange"] = normalized["exchange"].upper()
        normalized["currency"] = normalized["currency"].upper()
        normalized["effective_from"] = cls._parse_iso_date(
            normalized["effective_from"],
            field="effective_from",
        ).isoformat()

        effective_to_text = str(value.get("effective_to") or "").strip()
        if effective_to_text:
            effective_to = cls._parse_iso_date(effective_to_text, field="effective_to")
            if effective_to < date.fromisoformat(normalized["effective_from"]):
                raise ValueError("cost identity effective_to precedes effective_from")
            normalized["effective_to"] = effective_to.isoformat()
        else:
            normalized["effective_to"] = None

        commission_basis = normalized["commission_basis"].upper()
        if commission_basis not in _ALLOWED_COMMISSION_BASES:
            raise ValueError("unsupported cost identity commission_basis")
        normalized["commission_basis"] = commission_basis

        if normalized["minimum_fee_policy"] != _MINIMUM_FEE_POLICY:
            raise ValueError("unsupported cost identity minimum_fee_policy")

        for key in _COST_NUMERIC_FIELDS:
            raw = value.get(key)
            try:
                number = float(raw)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"missing or invalid cost identity field: {key}") from exc
            if not math.isfinite(number) or number < 0:
                raise ValueError(f"missing or invalid cost identity field: {key}")
            normalized[key] = number
        if normalized["reference_entry_notional_cny"] <= 0:
            raise ValueError("reference_entry_notional_cny must be positive")
        return normalized

    @classmethod
    def _validate_cost_identity_for_ledger(
        cls,
        cost_identity: Mapping[str, Any],
        ledger: Any,
    ) -> None:
        if str(cost_identity["market"]).lower() != str(ledger.market or "").strip().lower():
            raise ValueError("cost identity market mismatch")
        if str(cost_identity["instrument_type"]).lower() != str(
            ledger.instrument_type or ""
        ).strip().lower():
            raise ValueError("cost identity instrument_type mismatch")

        try:
            asset_identity = json.loads(str(ledger.asset_identity_json or ""))
        except (TypeError, ValueError) as exc:
            raise ValueError("ledger asset identity is not available for cost matching") from exc
        if not isinstance(asset_identity, dict):
            raise ValueError("ledger asset identity is not available for cost matching")

        expected_market = str(asset_identity.get("market") or "").strip().lower()
        expected_type = str(asset_identity.get("instrument_type") or "").strip().lower()
        expected_exchange = str(asset_identity.get("exchange") or "").strip().upper()
        expected_currency = str(asset_identity.get("currency") or "").strip().upper()
        if (
            not expected_market
            or not expected_type
            or not expected_exchange
            or not expected_currency
        ):
            raise ValueError("ledger asset identity is incomplete for cost matching")
        if str(cost_identity["market"]).lower() != expected_market:
            raise ValueError("cost identity asset market mismatch")
        if str(cost_identity["instrument_type"]).lower() != expected_type:
            raise ValueError("cost identity asset instrument_type mismatch")
        if str(cost_identity["exchange"]).upper() != expected_exchange:
            raise ValueError("cost identity exchange mismatch")
        if str(cost_identity["currency"]).upper() != expected_currency:
            raise ValueError("cost identity currency mismatch")

    @classmethod
    def _cost_identity_covers_sessions(
        cls,
        cost_identity: Mapping[str, Any],
        *,
        entry_session: date,
        exit_session: date,
    ) -> bool:
        effective_from = cls._parse_iso_date(
            cost_identity["effective_from"],
            field="effective_from",
        )
        effective_to_text = str(cost_identity.get("effective_to") or "").strip()
        effective_to = (
            cls._parse_iso_date(effective_to_text, field="effective_to")
            if effective_to_text
            else None
        )
        if entry_session < effective_from or exit_session < effective_from:
            return False
        if effective_to is not None and (
            entry_session > effective_to or exit_session > effective_to
        ):
            return False
        return True

    @staticmethod
    def _parse_iso_date(value: Any, *, field: str) -> date:
        try:
            return date.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid cost identity field: {field}") from exc

    @staticmethod
    def _is_meta_opportunity(evidence_json: str) -> bool:
        try:
            evidence = json.loads(evidence_json)
        except (TypeError, ValueError):
            return False
        decision = evidence.get("canonical_decision") if isinstance(evidence, dict) else None
        if not isinstance(decision, dict):
            return False
        return (
            str(decision.get("action") or "").upper() == "WAIT"
            and str(decision.get("evidence_state") or "").upper() == "PROVEN"
            and decision.get("hard_veto") is False
        )

    @staticmethod
    def _finite(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _date_text(value: Any) -> Optional[str]:
        if isinstance(value, date):
            return value.isoformat()
        text = str(value or "").strip()
        return text or None
