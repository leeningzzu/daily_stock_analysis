# -*- coding: utf-8 -*-
"""Deterministic regressions for append-only PredictionOutcome research rows."""

from __future__ import annotations

import json
import os
from datetime import date, datetime

import pytest

from src.config import Config
from src.core.backtest_engine import BacktestEngine
from src.repositories.prediction_outcome_repo import PredictionOutcomeRepository
from src.services.prediction_outcome_service import (
    PREDICTION_OUTCOME_ENGINE_VERSION,
    PredictionOutcomeService,
)
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
        "schema_version": "cost-identity-v2",
        "market": "cn",
        "instrument_type": "stock",
        "exchange": "SH",
        "currency": "CNY",
        "effective_from": "2026-01-01",
        "effective_to": "2026-12-31",
        "source": "unit-test-fixture",
        "version": "test-v2",
        "cost_model_mode": "synthetic-test-only",
        "commission_basis": "ALL_IN_INCLUDES_EXCHANGE_HANDLING_AND_REGULATORY_LEVY_OTHER_IS_TRANSFER_ONLY",
        "minimum_fee_policy": "PER_SIDE_MAX_NOTIONAL_RATE_OR_MINIMUM_CNY",
        "buy_fee_rate": 0.0,
        "sell_fee_rate": 0.0,
        "sell_tax_rate": 0.0,
        "other_buy_rate": 0.0,
        "other_sell_rate": 0.0,
        "buy_slippage_bps": 0.0,
        "sell_slippage_bps": 0.0,
        "minimum_commission_cny": 0.0,
        "reference_entry_notional_cny": 100_000.0,
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
            asset_identity_hash="d" * 64,
            asset_identity_json=json.dumps(
                {
                    "version": "cn-stock-asset-v1",
                    "market": "cn",
                    "instrument_type": "stock",
                    "symbol": "600519",
                    "exchange": "SH",
                    "calendar": "XSHG",
                    "timezone": "Asia/Shanghai",
                    "currency": "CNY",
                },
                sort_keys=True,
            ),
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
        engine_version="prediction-outcome-fixed-horizon-v1",
    )
    second = service.evaluate_prediction(
        prediction_hash=prediction_hash,
        cost_identity=_cost_identity(),
    )
    assert PREDICTION_OUTCOME_ENGINE_VERSION == "prediction-outcome-fixed-horizon-v2"
    assert second["disposition"] == "created"
    assert second["outcome_hash"] != first["outcome_hash"]
    assert second["supersedes_outcome_hash"] is None
    rows = PredictionOutcomeRepository(isolated_db).list_for_prediction(prediction_hash)
    assert [row.evaluation_engine_version for row in rows] == [
        "prediction-outcome-fixed-horizon-v1",
        PREDICTION_OUTCOME_ENGINE_VERSION,
    ]


def test_minimum_commission_uses_frozen_reference_notional_on_both_sides() -> None:
    bars = [
        StockDaily(code="600519", date=date(2026, 9, 18), open=100.0, high=101.0, low=99.0, close=100.0),
        StockDaily(code="600519", date=date(2026, 9, 21), open=104.0, high=106.0, low=103.0, close=105.0),
        StockDaily(code="600519", date=date(2026, 9, 22), open=109.0, high=111.0, low=108.0, close=110.0),
    ]
    small = _cost_identity()
    small.update(
        buy_fee_rate=0.0001,
        sell_fee_rate=0.0001,
        minimum_commission_cny=5.0,
        reference_entry_notional_cny=10_000.0,
    )
    small_result = BacktestEngine.evaluate_fixed_horizon_take(
        forward_bars=bars,
        cost_identity=PredictionOutcomeService.normalize_cost_identity(small),
    )
    assert small_result["entry_notional_cny"] == pytest.approx(10_000.0)
    assert small_result["exit_notional_cny"] == pytest.approx(11_000.0)
    assert small_result["buy_commission_cny"] == pytest.approx(5.0)
    assert small_result["sell_commission_cny"] == pytest.approx(5.0)

    large = dict(small)
    large.update(
        buy_fee_rate=0.001,
        sell_fee_rate=0.001,
        reference_entry_notional_cny=1_000_000.0,
    )
    large_result = BacktestEngine.evaluate_fixed_horizon_take(
        forward_bars=bars,
        cost_identity=PredictionOutcomeService.normalize_cost_identity(large),
    )
    assert large_result["buy_commission_cny"] == pytest.approx(1_000.0)
    assert large_result["sell_commission_cny"] == pytest.approx(1_100.0)


def test_cost_identity_v2_rejects_invalid_model_fields() -> None:
    bad_basis = _cost_identity()
    bad_basis["commission_basis"] = "UNKNOWN"
    with pytest.raises(ValueError, match="commission_basis"):
        PredictionOutcomeService.normalize_cost_identity(bad_basis)

    ambiguous_all_in = _cost_identity()
    ambiguous_all_in["commission_basis"] = "ALL_IN_INCLUDES_EXCHANGE_HANDLING_AND_REGULATORY_LEVY"
    with pytest.raises(ValueError, match="commission_basis"):
        PredictionOutcomeService.normalize_cost_identity(ambiguous_all_in)

    bad_policy = _cost_identity()
    bad_policy["minimum_fee_policy"] = "UNSUPPORTED"
    with pytest.raises(ValueError, match="minimum_fee_policy"):
        PredictionOutcomeService.normalize_cost_identity(bad_policy)

    bad_minimum = _cost_identity()
    bad_minimum["minimum_commission_cny"] = -1.0
    with pytest.raises(ValueError, match="minimum_commission_cny"):
        PredictionOutcomeService.normalize_cost_identity(bad_minimum)

    zero_notional = _cost_identity()
    zero_notional["reference_entry_notional_cny"] = 0.0
    with pytest.raises(ValueError, match="reference_entry_notional_cny"):
        PredictionOutcomeService.normalize_cost_identity(zero_notional)


def test_cost_identity_asset_mismatch_is_rejected(isolated_db) -> None:
    _, prediction_hash = _seed_prediction(isolated_db)
    _seed_bars(isolated_db)
    bad_cost = _cost_identity()
    bad_cost["exchange"] = "SZ"
    with pytest.raises(ValueError, match="exchange mismatch"):
        PredictionOutcomeService(db_manager=isolated_db).evaluate_prediction(
            prediction_hash=prediction_hash,
            cost_identity=bad_cost,
        )


def test_cost_identity_validity_interval_fails_closed(isolated_db) -> None:
    _, prediction_hash = _seed_prediction(isolated_db)
    _seed_bars(isolated_db)
    expired = _cost_identity()
    expired["effective_to"] = "2026-09-21"

    result = PredictionOutcomeService(db_manager=isolated_db).evaluate_prediction(
        prediction_hash=prediction_hash,
        cost_identity=expired,
    )

    assert result["status"] == "UNLABELABLE"
    rows = PredictionOutcomeRepository(isolated_db).list_for_prediction(prediction_hash)
    assert len(rows) == 1
    assert rows[0].label_reason == "COST_IDENTITY_OUT_OF_RANGE"
    assert rows[0].execution_state == "EXECUTION_UNKNOWN"


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
