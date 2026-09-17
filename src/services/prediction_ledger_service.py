# -*- coding: utf-8 -*-
"""Append-only Prediction Ledger snapshots over the existing DSA decision pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from typing import Any, Dict, Mapping, Optional

from src.repositories.prediction_ledger_repo import PredictionLedgerRepository
from src.storage import DatabaseManager


PREDICTION_LEDGER_SCHEMA_VERSION = "prediction-ledger-v1"
PREDICTION_FEATURE_SCHEMA_VERSION = "stock-factor-evidence-v1"

_FACTOR_EVIDENCE_KEYS = (
    "strategy_id",
    "contract_version",
    "composite_score",
    "canonical_decision",
    "market_sector_regime",
    "trend_relative_strength",
    "supply_demand_volume_price",
    "cost_structure_evidence",
    "price_structure_evidence",
    "volatility_momentum_evidence",
    "pattern_trigger_evidence",
    "multi_timeframe_structure_context",
)

PREDICTION_FEATURE_SCHEMA_HASH = hashlib.sha256(
    json.dumps(
        {
            "schema_version": PREDICTION_FEATURE_SCHEMA_VERSION,
            "fields": list(_FACTOR_EVIDENCE_KEYS),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()

_SHA40_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class PredictionLedgerService:
    """Freeze one current-time factor decision without mutating prior snapshots."""

    def __init__(
        self,
        *,
        repo: Optional[PredictionLedgerRepository] = None,
        db_manager: Optional[DatabaseManager] = None,
    ):
        self.db = db_manager or DatabaseManager.get_instance()
        self.repo = repo or PredictionLedgerRepository(self.db)

    def persist(
        self,
        *,
        analysis_history_id: int,
        result: Any,
        decision_signal: Optional[Mapping[str, Any]] = None,
        code_sha: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        history_id = self._positive_int(analysis_history_id, "analysis_history_id")
        history = self.db.get_analysis_history_by_id(history_id)
        if history is None or history.created_at is None:
            return None

        dashboard = self._mapping(getattr(result, "dashboard", None))
        factor_decision = self._mapping(dashboard.get("factor_decision"))
        strategy_id = self._text(factor_decision.get("strategy_id"))
        if not strategy_id:
            return None

        evidence_payload = {
            key: factor_decision.get(key)
            for key in _FACTOR_EVIDENCE_KEYS
        }
        evidence_json = self._canonical_json(evidence_payload)
        evidence_hash = self._sha256_text(evidence_json)

        signal = self._signal_item(decision_signal)
        metadata = self._mapping(signal.get("metadata"))
        canonical_decision = self._mapping(factor_decision.get("canonical_decision"))
        canonical_action = self._text(canonical_decision.get("action"))
        signal_id = self._optional_positive_int(signal.get("id"))
        signal_horizon = self._text(signal.get("horizon"))
        signal_market = self._text(signal.get("market"))
        if not canonical_action or signal_id is None or not signal_horizon or not signal_market:
            return None
        multi_timeframe = self._mapping(
            factor_decision.get("multi_timeframe_structure_context")
        )

        bound_code_sha = self._normalize_code_sha(code_sha or os.getenv("GITHUB_SHA"))
        data_as_of = self._parse_date(
            self._mapping(metadata.get("market_phase_summary")).get("session_date")
        )
        available_at_max = self._parse_datetime(multi_timeframe.get("available_at_max"))
        adjustment_basis = self._text(multi_timeframe.get("adjustment_basis"))
        provider_identity = self._text(
            multi_timeframe.get("provider_identity") or multi_timeframe.get("provider")
        )
        universe_snapshot_id = self._text(metadata.get("universe_snapshot_id"))

        pit_reasons = []
        if available_at_max is None:
            pit_reasons.append("AVAILABLE_AT_NOT_BOUND")
        if not adjustment_basis:
            pit_reasons.append("ADJUSTMENT_BASIS_NOT_PERSISTED")
        if not universe_snapshot_id:
            pit_reasons.append("UNIVERSE_SNAPSHOT_NOT_BOUND")
        if not bound_code_sha:
            pit_reasons.append("CODE_SHA_NOT_BOUND")
        # AnalysisHistory.created_at is currently persisted as a naive datetime.
        pit_reasons.append("DECISION_TIMEZONE_NOT_BOUND")
        mtf_reason = self._text(multi_timeframe.get("cross_run_persistence_reason"))
        if (
            multi_timeframe.get("cross_run_persistence_eligible") is False
            and mtf_reason
            and mtf_reason not in pit_reasons
        ):
            pit_reasons.append(mtf_reason)

        decision_time = history.created_at
        action = canonical_action
        strategy_version = strategy_id
        factor_contract_version = self._text(factor_decision.get("contract_version"))

        prediction_identity = {
            "schema_version": PREDICTION_LEDGER_SCHEMA_VERSION,
            "market": signal_market,
            "stock_code": self._text(signal.get("stock_code")) or self._text(getattr(result, "code", None)),
            "decision_time": decision_time.isoformat(),
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "canonical_action": action,
            "horizon": signal_horizon,
            "decision_profile": self._text(signal.get("decision_profile")),
            "trigger_source": self._text(signal.get("trigger_source")),
            "evidence_hash": evidence_hash,
            "feature_schema_hash": PREDICTION_FEATURE_SCHEMA_HASH,
            "code_sha": bound_code_sha,
        }
        prediction_hash = self._sha256_text(self._canonical_json(prediction_identity))

        fields = {
            "prediction_hash": prediction_hash,
            "schema_version": PREDICTION_LEDGER_SCHEMA_VERSION,
            "analysis_history_id": history_id,
            "decision_signal_id": signal_id,
            "trace_id": self._text(signal.get("trace_id")),
            "market": prediction_identity["market"] or "unknown",
            "stock_code": prediction_identity["stock_code"] or str(history.code or "").strip(),
            "instrument_type": "stock",
            "decision_time": decision_time,
            "data_as_of": data_as_of,
            "available_at_max": available_at_max,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "factor_contract_version": factor_contract_version,
            "canonical_action": action,
            "horizon": signal_horizon,
            "decision_profile": self._text(signal.get("decision_profile")),
            "trigger_source": self._text(signal.get("trigger_source")),
            "source_type": self._text(signal.get("source_type")),
            "entry_low": self._finite_float(signal.get("entry_low")),
            "entry_high": self._finite_float(signal.get("entry_high")),
            "stop_loss": self._finite_float(signal.get("stop_loss")),
            "target_price": self._finite_float(signal.get("target_price")),
            "feature_schema_version": PREDICTION_FEATURE_SCHEMA_VERSION,
            "feature_schema_hash": PREDICTION_FEATURE_SCHEMA_HASH,
            "evidence_hash": evidence_hash,
            "evidence_json": evidence_json,
            "code_sha": bound_code_sha,
            "provider_identity": provider_identity,
            "adjustment_basis": adjustment_basis,
            "universe_snapshot_id": universe_snapshot_id,
            "pit_eligible": not pit_reasons,
            "pit_ineligibility_json": self._canonical_json(pit_reasons),
            "durability_state": "LOCAL_DB_ONLY",
        }
        if not fields["stock_code"] or fields["market"] == "unknown":
            return None

        row_id, created = self.repo.insert_if_history_exists(fields)
        if row_id is None:
            return None
        return {
            "id": row_id,
            "created": created,
            "prediction_hash": prediction_hash,
            "evidence_hash": evidence_hash,
            "feature_schema_hash": PREDICTION_FEATURE_SCHEMA_HASH,
            "pit_eligible": not pit_reasons,
            "pit_ineligibility_reasons": pit_reasons,
            "durability_state": "LOCAL_DB_ONLY",
        }

    @staticmethod
    def _signal_item(value: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        item = value.get("item")
        if isinstance(item, Mapping):
            return dict(item)
        return dict(value)

    @staticmethod
    def _mapping(value: Any) -> Dict[str, Any]:
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _text(value: Any) -> Optional[str]:
        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _positive_int(value: Any, field_name: str) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be a positive integer") from exc
        if isinstance(value, bool) or parsed <= 0:
            raise ValueError(f"{field_name} must be a positive integer")
        return parsed

    @classmethod
    def _optional_positive_int(cls, value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            return cls._positive_int(value, "value")
        except ValueError:
            return None

    @staticmethod
    def _finite_float(value: Any) -> Optional[float]:
        if value in (None, "") or isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed != parsed or parsed in (float("inf"), float("-inf")):
            return None
        return parsed

    @staticmethod
    def _normalize_code_sha(value: Any) -> Optional[str]:
        text = str(value or "").strip().lower()
        return text if _SHA40_RE.fullmatch(text) else None

    @staticmethod
    def _parse_date(value: Any) -> Optional[date]:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                return value.astimezone(timezone.utc).replace(tzinfo=None)
            return value
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    @staticmethod
    def _market_from_result(result: Any) -> Optional[str]:
        code = str(getattr(result, "code", "") or "").strip().upper()
        if code.startswith(("SH", "SZ")) or (code.isdigit() and len(code) == 6):
            return "cn"
        if code.startswith("HK"):
            return "hk"
        return None

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=PredictionLedgerService._json_default,
        )

    @staticmethod
    def _json_default(value: Any) -> Any:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        enum_value = getattr(value, "value", None)
        if enum_value is not None:
            return enum_value
        raise TypeError(f"unsupported prediction ledger value: {type(value).__name__}")

    @staticmethod
    def _sha256_text(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
