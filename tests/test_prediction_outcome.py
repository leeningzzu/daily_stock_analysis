# -*- coding: utf-8 -*-
"""Deterministic regressions for append-only PredictionOutcome research rows."""

from __future__ import annotations

import json
import os
from datetime import date, datetime

import pytest

from src.config import Config
from src.repositories.prediction_outcome_repo import PredictionOutcomeRepository
from src.services.prediction_outcome_service import PredictionOutcomeService
from src.storage import (
    AnalysisHistory,
    DatabaseManager,
    PredictionLedgerRecord,
    PredictionOutcomeRecord,
    StockDaily,
)


@pytest.fixture()
def isolated_db(tmp_path):
    old_database_path = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = str(tmp_path / "prediction_outcome.db")
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


def _cost_identity() -> dict:
    return {
        "market": "cn",
        "instrument_type": "stock",
        "currency": "CNY",
        "effective_date": "TEST_ONLY",
        "source": "unit-test-fixture",
        "version": "test-v1",
        "cost_model_mode": "synthetic-test-only",
        "minimum_fee_policy": "NOT_MODELED_TEST_ONLY",
        "buy_fee_rate": 0.0,
        "sell_fee_rate": 0.0,
        "sell_tax_rate": 0.0,
        "other_buy_rate": 0.0,
        "other_sell_rate": 0.0,
        "buy_slippage_bps": 0.0,
        "sell_slippage_bps": 0.0,
    }


def _seed_prediction(
    db: DatabaseManager,
    prediction_hash: str = "a" * 64,
) -> tuple[int, str]:
    with db.session_scope() as session:
        history = AnalysisHistory(
            query_id="prediction-outcome-history",
            code="600519",
            report_type="simple",
            created_at=datetime(2026, 9, 17, 11, 0, 0),
        )
        session.add(history)
        session.flush()
        evidence_json = json.dumps(
            {
                "canonical_decision": {
                    "action": "WAIT",
                    "evidence_state": "PROVEN",
                    "hard_veto": False,
                }
            },
            sort_keys=True,
        )
        ledger = PredictionLedgerRecord(
            prediction_hash=prediction_hash,
            schema_version="prediction-ledger-v2",
            analysis_history_id=history.id,
            market="cn",
            stock_code="600519",
            instrument_type="stock",
            decision_time=datetime(2026, 9, 17, 10, 5, 0),
            decision_timezone="Asia/Shanghai",
            data_as_of=date(2026, 9, 17),
            strategy_id="stock_trend_quality_pullback_v1",
            strategy_version="stock_trend_quality_pullback_v1",
            canonical_action="WAIT",
            horizon="3d",
            feature_schema_version="stock-factor-evidence-v1",
            feature_schema_hash="b" * 64,
            evidence_hash="c" * 64,
            evidence_json=evidence_json,
            pit_eligible=True,
            pit_ineligibility_json="[]",
            durability_state="LOCAL_DB_ONLY",
        )
        session.add(ledger)
        session.flush()
        return int(history.id), prediction_hash


def _seed_bars(
    db: DatabaseManager,
    closes=(102.0, 104.0, 106.0),
    first_open=100.0,
) -> None:
    days = (date(2026, 9, 18), date(2026, 9, 21), date(2026, 9, 22))
    with db.session_scope() as session:
        for index, (day, close) in enumerate(zip(days, closes)):
            open_price = first_open if index == 0 else float(close) - 1.0
            session.add(
                StockDaily(
                    code="600519",
                    date=day,
                    open=open_price,
                    high=max(open_price, float(close)) + 1.0,
                    low=min(open_price, float(close)) - 1.0,
                    close=float(close),
                    volume=1_000_000 + index,
                    data_source="AkshareFetcher",
                )
            )


def test_wait_proven_opportunity_uses_next_open_and_third_close(isolated_db) -> None:
    _, prediction_hash = _seed_prediction(isolated_db)
    _seed_bars(isolated_db)

    result = PredictionOutcomeService(db_manager=isolated_db).evaluate_prediction(
        prediction_hash=prediction_hash,
        cost_identity=_cost_identity(),
    )

    assert result["status"] == "TAKE_SUCCESS"
    assert result["label_value"] == 1
    rows = PredictionOutcomeRepository(isolated_db).list_for_prediction(prediction_hash)
    assert len(rows) == 1
    row = rows[0]
    assert row.entry_session == date(2026, 9, 18)
    assert row.exit_session == date(2026, 9, 22)
    assert row.entry_price == pytest.approx(100.0)
    assert row.exit_price == pytest.approx(106.0)
    assert row.net_return_pct == pytest.approx(6.0)
    assert row.adjustment_basis == "qfq"


def test_unmatured_prediction_writes_no_terminal_outcome(isolated_db) -> None:
    _, prediction_hash = _seed_prediction(isolated_db)
    _seed_bars(isolated_db, closes=(102.0, 104.0))

    result = PredictionOutcomeService(db_manager=isolated_db).evaluate_prediction(
        prediction_hash=prediction_hash,
        cost_identity=_cost_identity(),
    )
    assert result["status"] == "UNMATURED"
    with isolated_db.get_session() as session:
        assert session.query(PredictionOutcomeRecord).count() == 0


def test_exact_retry_is_idempotent_and_correction_appends(isolated_db) -> None:
    _, prediction_hash = _seed_prediction(isolated_db)
    _seed_bars(isolated_db)
    service = PredictionOutcomeService(db_manager=isolated_db)

    first = service.evaluate_prediction(
        prediction_hash=prediction_hash,
        cost_identity=_cost_identity(),
    )
    repeated = service.evaluate_prediction(
        prediction_hash=prediction_hash,
        cost_identity=_cost_identity(),
    )
    assert repeated["outcome_hash"] == first["outcome_hash"]
    assert repeated["disposition"] == "existing"

    with isolated_db.session_scope() as session:
        row = session.query(StockDaily).filter(StockDaily.date == date(2026, 9, 22)).one()
        row.close = 95.0
        row.low = 94.0

    with pytest.raises(ValueError, match="correction_reason"):
        service.evaluate_prediction(
            prediction_hash=prediction_hash,
            cost_identity=_cost_identity(),
        )

    corrected = service.evaluate_prediction(
        prediction_hash=prediction_hash,
        cost_identity=_cost_identity(),
        correction_reason="provider_data_correction",
    )
    rows = PredictionOutcomeRepository(isolated_db).list_for_prediction(prediction_hash)
    assert len(rows) == 2
    assert rows[0].outcome_hash == first["outcome_hash"]
    assert rows[1].supersedes_outcome_hash == first["outcome_hash"]
    assert rows[1].correction_reason == "provider_data_correction"
    assert corrected["status"] == "TAKE_FAIL"


def test_engine_version_creates_independent_root(isolated_db) -> None:
    _, prediction_hash = _seed_prediction(isolated_db)
    _seed_bars(isolated_db)
    service = PredictionOutcomeService(db_manager=isolated_db)
    first = service.evaluate_prediction(
        prediction_hash=prediction_hash,
        cost_identity=_cost_identity(),
    )
    second = service.evaluate_prediction(
        prediction_hash=prediction_hash,
        cost_identity=_cost_identity(),
        engine_version="prediction-outcome-fixed-horizon-v2-test",
    )
    assert second["disposition"] == "created"
    assert second["outcome_hash"] != first["outcome_hash"]
    assert second["supersedes_outcome_hash"] is None


def test_invalid_entry_is_unlabelable_not_silently_filled(isolated_db) -> None:
    _, prediction_hash = _seed_prediction(isolated_db)
    _seed_bars(isolated_db, first_open=0.0)
    result = PredictionOutcomeService(db_manager=isolated_db).evaluate_prediction(
        prediction_hash=prediction_hash,
        cost_identity=_cost_identity(),
    )
    assert result["status"] == "UNLABELABLE"
    assert result["label_value"] is None
    row = PredictionOutcomeRepository(isolated_db).list_for_prediction(prediction_hash)[0]
    assert row.execution_state == "EXECUTION_UNKNOWN"


def test_cost_identity_is_mandatory_and_history_cleanup_preserves_research_rows(
    isolated_db,
) -> None:
    history_id, prediction_hash = _seed_prediction(isolated_db)
    _seed_bars(isolated_db)
    service = PredictionOutcomeService(db_manager=isolated_db)
    bad_cost = _cost_identity()
    bad_cost.pop("sell_tax_rate")
    with pytest.raises(ValueError, match="sell_tax_rate"):
        service.evaluate_prediction(
            prediction_hash=prediction_hash,
            cost_identity=bad_cost,
        )

    service.evaluate_prediction(
        prediction_hash=prediction_hash,
        cost_identity=_cost_identity(),
    )
    assert isolated_db.delete_analysis_history_records([history_id]) == 1
    with isolated_db.get_session() as session:
        assert session.query(PredictionLedgerRecord).count() == 1
        assert session.query(PredictionOutcomeRecord).count() == 1
