# -*- coding: utf-8 -*-
"""Deterministic tests for the append-only Prediction Ledger V1 foundation."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import inspect

from src.config import Config
from src.core.pipeline import StockAnalysisPipeline
from src.repositories.prediction_ledger_repo import PredictionLedgerRepository
from src.services.prediction_ledger_service import (
    PREDICTION_FEATURE_SCHEMA_HASH,
    PREDICTION_FEATURE_SCHEMA_VERSION,
    PREDICTION_LEDGER_SCHEMA_VERSION,
    PredictionLedgerService,
)
from src.services.pit_identity import build_specified_codes_selection_context
from src.storage import AnalysisHistory, DatabaseManager, PredictionLedgerRecord


@pytest.fixture()
def isolated_db(tmp_path):
    old_database_path = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = str(tmp_path / "prediction_ledger.db")
    Config.reset_instance()
    DatabaseManager.reset_instance()
    db = DatabaseManager.get_instance()
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        if old_database_path is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = old_database_path


def _add_history(db: DatabaseManager, code: str = "600519") -> int:
    with db.session_scope() as session:
        row = AnalysisHistory(query_id="ledger-query", code=code, report_type="simple")
        session.add(row)
        session.flush()
        return int(row.id)


def _result(*, score: int = 67):
    factor_decision = {
        "strategy_id": "stock_trend_quality_pullback_v1",
        "contract_version": "1.0",
        "composite_score": score,
        "canonical_decision": {
            "action": "WAIT",
            "evidence_state": "PROVEN",
            "hard_veto": False,
        },
        "market_sector_regime": {"status": "READY", "schema_version": "market-sector-regime-v1"},
        "trend_relative_strength": {"status": "READY", "schema_version": "trend-relative-strength-v1"},
        "supply_demand_volume_price": {"status": "READY", "schema_version": "supply-demand-volume-price-v1"},
        "cost_structure_evidence": {"status": "READY", "schema_version": "cost-structure-v1"},
        "price_structure_evidence": {"status": "READY", "schema_version": "price-structure-v1"},
        "volatility_momentum_evidence": {"status": "READY", "schema_version": "volatility-momentum-v1"},
        "pattern_trigger_evidence": {"status": "FORMING", "schema_version": "pattern-trigger-v1"},
        "multi_timeframe_structure_context": {
            "schema_version": "multi-timeframe-structure-v1",
            "cross_run_persistence_eligible": False,
            "cross_run_persistence_reason": "ADJUSTMENT_BASIS_NOT_PERSISTED",
        },
        "investor_brief": {"human_only": "must not enter feature payload"},
    }
    return SimpleNamespace(
        code="600519",
        dashboard={"factor_decision": factor_decision},
    )


def _signal() -> dict:
    return {
        "id": 17,
        "stock_code": "600519",
        "market": "cn",
        "source_type": "analysis",
        "trace_id": "trace-ledger-1",
        "decision_profile": "balanced",
        "trigger_source": "system",
        "action": "watch",
        "horizon": "3d",
        "entry_low": 1600.0,
        "entry_high": 1650.0,
        "stop_loss": 1550.0,
        "target_price": 1750.0,
        "metadata": {"market_phase_summary": {"session_date": "2026-09-17"}},
    }


def test_schema_is_append_only_identity_surface(isolated_db) -> None:
    inspector = inspect(isolated_db._engine)
    unique_constraints = inspector.get_unique_constraints("prediction_ledger")
    indexes = {item["name"] for item in inspector.get_indexes("prediction_ledger")}

    assert any(
        item["name"] == "uix_prediction_ledger_hash"
        and item["column_names"] == ["prediction_hash"]
        for item in unique_constraints
    )
    assert "ix_prediction_ledger_stock_time" in indexes
    assert "ix_prediction_ledger_strategy_time" in indexes
    assert "ix_prediction_ledger_pit_time" in indexes


def test_service_freezes_factor_payload_idempotently_and_excludes_human_brief(isolated_db) -> None:
    history_id = _add_history(isolated_db)
    service = PredictionLedgerService(db_manager=isolated_db)
    code_sha = "1" * 40

    first = service.persist(
        analysis_history_id=history_id,
        result=_result(),
        decision_signal=_signal(),
        code_sha=code_sha,
    )
    repeated = service.persist(
        analysis_history_id=history_id,
        result=_result(),
        decision_signal=_signal(),
        code_sha=code_sha,
    )

    assert first is not None and first["created"] is True
    assert repeated is not None and repeated["created"] is False
    assert repeated["id"] == first["id"]
    rows = PredictionLedgerRepository(isolated_db).list_for_history(history_id)
    assert len(rows) == 1
    row = rows[0]
    payload = json.loads(row.evidence_json)
    assert "investor_brief" not in payload
    assert payload["composite_score"] == 67
    assert row.schema_version == PREDICTION_LEDGER_SCHEMA_VERSION
    assert row.feature_schema_version == PREDICTION_FEATURE_SCHEMA_VERSION
    assert row.feature_schema_hash == PREDICTION_FEATURE_SCHEMA_HASH
    assert row.code_sha == code_sha
    assert row.canonical_action == "WAIT"
    assert row.decision_signal_id == 17
    assert row.data_as_of.isoformat() == "2026-09-17"


def test_changed_evidence_creates_new_snapshot_without_mutating_old_row(isolated_db) -> None:
    history_id = _add_history(isolated_db)
    service = PredictionLedgerService(db_manager=isolated_db)

    first = service.persist(
        analysis_history_id=history_id,
        result=_result(score=67),
        decision_signal=_signal(),
        code_sha="2" * 40,
    )
    second = service.persist(
        analysis_history_id=history_id,
        result=_result(score=72),
        decision_signal=_signal(),
        code_sha="2" * 40,
    )

    assert first is not None and second is not None
    assert first["id"] != second["id"]
    rows = PredictionLedgerRepository(isolated_db).list_for_history(history_id)
    assert [json.loads(row.evidence_json)["composite_score"] for row in rows] == [67, 72]


def test_current_identity_gaps_fail_closed_for_pit_training(isolated_db) -> None:
    history_id = _add_history(isolated_db)
    outcome = PredictionLedgerService(db_manager=isolated_db).persist(
        analysis_history_id=history_id,
        result=_result(),
        decision_signal=_signal(),
        code_sha="3" * 40,
    )

    assert outcome is not None
    assert outcome["pit_eligible"] is False
    assert set(outcome["pit_ineligibility_reasons"]) >= {
        "AVAILABLE_AT_NOT_BOUND",
        "ADJUSTMENT_BASIS_NOT_PERSISTED",
        "DECISION_TIMEZONE_NOT_BOUND",
        "DATA_SNAPSHOT_IDENTITY_NOT_BOUND",
        "SELECTION_SOURCE_NOT_BOUND",
    }
    row = PredictionLedgerRepository(isolated_db).list_for_history(history_id)[0]
    assert row.pit_eligible is False
    assert row.durability_state == "LOCAL_DB_ONLY"


def test_bound_first_slice_identities_can_be_semantically_pit_eligible(isolated_db) -> None:
    history_id = _add_history(isolated_db)
    result = _result()
    result.dashboard["factor_decision"]["multi_timeframe_structure_context"].update(
        {
            "data_snapshot_identity": "a" * 64,
            "provider_identity": "AkshareFetcher",
            "adjustment_basis": "qfq",
            "available_at_max": "2026-09-17T10:00:00+00:00",
        }
    )
    result.diagnostic_context_snapshot = {
        "market_phase_summary": {
            "session_date": "2026-09-17",
            "market_local_time": "2026-09-17T18:00:00+08:00",
        },
        "research_decision_time_utc": "2026-09-17T10:05:00+00:00",
    }

    outcome = PredictionLedgerService(db_manager=isolated_db).persist(
        analysis_history_id=history_id,
        result=result,
        decision_signal=_signal(),
        code_sha="5" * 40,
        selection_context=build_specified_codes_selection_context(
            raw_selection_source="manual",
            query_source="api",
        ),
    )

    assert outcome is not None
    assert outcome["pit_eligible"] is True
    assert outcome["pit_ineligibility_reasons"] == []
    row = PredictionLedgerRepository(isolated_db).list_for_history(history_id)[0]
    assert row.schema_version == "prediction-ledger-v2"
    assert row.decision_timezone == "Asia/Shanghai"
    assert row.asset_identity_hash
    assert row.data_snapshot_identity == "a" * 64
    assert row.selection_source == "SPECIFIED_CODES"
    assert row.selection_context_hash
    assert row.universe_snapshot_id is None


def test_history_deletion_keeps_prediction_ledger_snapshot(isolated_db) -> None:
    history_id = _add_history(isolated_db)
    assert PredictionLedgerService(db_manager=isolated_db).persist(
        analysis_history_id=history_id,
        result=_result(),
        decision_signal=_signal(),
        code_sha="4" * 40,
    ) is not None

    assert isolated_db.delete_analysis_history_records([history_id]) == 1
    with isolated_db.get_session() as session:
        rows = session.query(PredictionLedgerRecord).all()
        assert len(rows) == 1
        assert rows[0].analysis_history_id == history_id


def test_missing_canonical_signal_identity_or_history_creates_no_snapshot(isolated_db) -> None:
    history_id = _add_history(isolated_db)
    service = PredictionLedgerService(db_manager=isolated_db)

    assert service.persist(
        analysis_history_id=history_id,
        result=SimpleNamespace(code="600519", dashboard={}),
        decision_signal=_signal(),
    ) is None

    missing_canonical = _result()
    del missing_canonical.dashboard["factor_decision"]["canonical_decision"]
    assert service.persist(
        analysis_history_id=history_id,
        result=missing_canonical,
        decision_signal=_signal(),
    ) is None

    signal_without_horizon = _signal()
    signal_without_horizon.pop("horizon")
    assert service.persist(
        analysis_history_id=history_id,
        result=_result(),
        decision_signal=signal_without_horizon,
    ) is None

    assert service.persist(
        analysis_history_id=history_id + 999,
        result=_result(),
        decision_signal=_signal(),
    ) is None
    with isolated_db.get_session() as session:
        assert session.query(PredictionLedgerRecord).count() == 0


def test_pipeline_helper_uses_same_database_and_fails_open() -> None:
    pipeline = object.__new__(StockAnalysisPipeline)
    pipeline.db = MagicMock()
    service = MagicMock()
    service.persist.side_effect = RuntimeError("ledger unavailable")

    with patch(
        "src.services.prediction_ledger_service.PredictionLedgerService",
        return_value=service,
    ) as service_class:
        pipeline._persist_prediction_ledger_after_history_save(
            result=_result(),
            analysis_history_id=42,
            decision_signal={"item": _signal()},
        )

    service_class.assert_called_once_with(db_manager=pipeline.db)
    service.persist.assert_called_once()
    assert service.persist.call_args.kwargs["analysis_history_id"] == 42
