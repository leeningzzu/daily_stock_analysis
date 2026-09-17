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


PREDICTION_OUTCOME_ENGINE_VERSION = "prediction-outcome-fixed-horizon-v1"
PRIMARY_LABEL_IDENTITY = "META_TAKE_NET_POSITIVE_NEXT_OPEN_3S_FIXED_CLOSE_V1"
PRIMARY_HORIZON_IDENTITY = "XSHG_POSTMARKET_NEXT_OPEN_3_FORWARD_SESSIONS_FIXED_CLOSE_V1"

_COST_TEXT_FIELDS = (
    "market",
    "instrument_type",
    "currency",
    "effective_date",
    "source",
    "version",
    "cost_model_mode",
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
        for key in _COST_NUMERIC_FIELDS:
            raw = value.get(key)
            try:
                number = float(raw)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"missing or invalid cost identity field: {key}") from exc
            if not math.isfinite(number) or number < 0:
                raise ValueError(f"missing or invalid cost identity field: {key}")
            normalized[key] = number
        return normalized

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
