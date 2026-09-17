# -*- coding: utf-8 -*-
"""Deterministic regressions for immutable PIT dataset manifests."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

import pytest

from src.config import Config
from src.repositories.pit_dataset_repo import PITDatasetRepository
from src.services.pit_dataset_service import PITDatasetService
from src.services.prediction_ledger_service import PREDICTION_FEATURE_SCHEMA_HASH
from src.services.prediction_outcome_service import (
    PREDICTION_OUTCOME_ENGINE_VERSION,
    PRIMARY_HORIZON_IDENTITY,
    PRIMARY_LABEL_IDENTITY,
)
from src.storage import DatabaseManager, PredictionLedgerRecord, PredictionOutcomeRecord


COST_HASH = "d" * 64
CODE_SHA = "a" * 40


@pytest.fixture()
def isolated_db(tmp_path):
    old_database_path = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = str(tmp_path / "pit_dataset.db")
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


def _seed_prediction(
    db: DatabaseManager,
    *,
    index: int,
    session_date: date,
    route: str = "SPECIFIED_CODES",
    pit_eligible: bool = True,
    universe_snapshot_id: str | None = None,
) -> str:
    prediction_hash = f"{index:064x}"
    evidence_json = json.dumps(
        {"canonical_decision": {"action": "WAIT", "evidence_state": "PROVEN", "hard_veto": False}},
        sort_keys=True,
    )
    with db.session_scope() as session:
        session.add(
            PredictionLedgerRecord(
                prediction_hash=prediction_hash,
                schema_version="prediction-ledger-v2",
                analysis_history_id=index + 1,
                market="cn",
                stock_code=f"60{index:04d}"[-6:],
                instrument_type="stock",
                decision_time=datetime.combine(session_date, datetime.min.time()).replace(hour=10),
                decision_timezone="Asia/Shanghai",
                data_as_of=session_date,
                available_at_max=datetime.combine(session_date, datetime.min.time()).replace(hour=9),
                strategy_id="stock_trend_quality_pullback_v1",
                strategy_version="stock_trend_quality_pullback_v1",
                canonical_action="WAIT",
                horizon="3d",
                feature_schema_version="stock-factor-evidence-v1",
                feature_schema_hash=PREDICTION_FEATURE_SCHEMA_HASH,
                evidence_hash=f"{index + 10_000:064x}",
                evidence_json=evidence_json,
                code_sha=CODE_SHA,
                provider_identity="AkshareFetcher",
                adjustment_basis="qfq",
                universe_snapshot_id=universe_snapshot_id,
                asset_identity_hash=f"{index + 20_000:064x}",
                asset_identity_json="{}",
                data_snapshot_identity=f"{index + 30_000:064x}",
                selection_source=route,
                selection_context_hash=f"{index + 40_000:064x}",
                selection_context_json="{}",
                pit_eligible=pit_eligible,
                pit_ineligibility_json="[]" if pit_eligible else '["TEST_GAP"]',
                durability_state="LOCAL_DB_ONLY",
                created_at=datetime.combine(session_date, datetime.min.time()).replace(hour=10, minute=1),
            )
        )
    return prediction_hash


def _seed_outcome(
    db: DatabaseManager,
    *,
    prediction_hash: str,
    decision_session: date,
    exit_session: date,
    available_at: datetime,
    suffix: int,
    supersedes: str | None = None,
) -> str:
    outcome_hash = f"{suffix + 50_000:064x}"
    with db.session_scope() as session:
        session.add(
            PredictionOutcomeRecord(
                outcome_hash=outcome_hash,
                root_identity_hash=f"{suffix + 60_000:064x}",
                prediction_hash=prediction_hash,
                label_identity=PRIMARY_LABEL_IDENTITY,
                horizon_identity=PRIMARY_HORIZON_IDENTITY,
                cost_identity_hash=COST_HASH,
                cost_identity_json="{}",
                evaluation_engine_version=PREDICTION_OUTCOME_ENGINE_VERSION,
                supersedes_outcome_hash=supersedes,
                correction_reason="test-correction" if supersedes else None,
                decision_session=decision_session,
                entry_session=decision_session + timedelta(days=1),
                exit_session=exit_session,
                execution_state="FILLED",
                entry_price=100.0,
                exit_price=101.0,
                gross_return_pct=1.0,
                net_return_pct=1.0,
                label_value=1,
                label_status="TAKE_SUCCESS",
                data_snapshot_identity=f"{suffix + 70_000:064x}",
                provider_identity="AkshareFetcher",
                adjustment_basis="qfq",
                available_at=available_at,
                created_at=available_at,
            )
        )
    return outcome_hash


def _seed_twenty_sessions(db: DatabaseManager, *, auto_screen: bool = False) -> list[str]:
    start = date(2026, 1, 1)
    hashes = []
    for index in range(20):
        day = start + timedelta(days=index)
        prediction_hash = _seed_prediction(
            db,
            index=index + 1,
            session_date=day,
            route="AUTO_SCREEN" if auto_screen else "SPECIFIED_CODES",
        )
        hashes.append(prediction_hash)
        _seed_outcome(
            db,
            prediction_hash=prediction_hash,
            decision_session=day,
            exit_session=day + timedelta(days=3),
            available_at=datetime.combine(day + timedelta(days=3), datetime.min.time()).replace(hour=8),
            suffix=index + 1,
        )
    return hashes


def test_manifest_is_chronological_grouped_sealed_and_idempotent(isolated_db) -> None:
    _seed_twenty_sessions(isolated_db)
    service = PITDatasetService(db_manager=isolated_db)

    first = service.build_manifest(cost_identity_hash=COST_HASH)
    second = service.build_manifest(cost_identity_hash=COST_HASH)

    assert first["dataset_hash"] == second["dataset_hash"]
    assert first["disposition"] == "created"
    assert second["disposition"] == "existing"
    manifest = first["manifest"]
    assert manifest["session_boundaries"] == {
        "train_first": "2026-01-01",
        "train_last": "2026-01-12",
        "validation_first": "2026-01-13",
        "validation_last": "2026-01-16",
        "final_test_first": "2026-01-17",
        "final_test_last": "2026-01-20",
    }
    assert manifest["final_test_state"] == "SEALED"
    assert manifest["counts"]["raw_by_fold"] == {"TRAIN": 12, "VALIDATION": 4, "FINAL_TEST": 4}
    assert manifest["counts"]["included_by_fold"]["TRAIN"] > 0
    assert manifest["counts"]["included_by_fold"]["VALIDATION"] > 0
    assert all("label_value" not in item for item in manifest["assignments"])
    assert all("net_return_pct" not in item for item in manifest["assignments"])
    assert PITDatasetRepository(isolated_db).get_by_hash(first["dataset_hash"]) is not None


def test_boundary_purge_and_late_correction_use_only_knowable_outcome(isolated_db) -> None:
    hashes = _seed_twenty_sessions(isolated_db)
    target = hashes[1]
    early_hash = f"{2 + 50_000:064x}"
    late_hash = _seed_outcome(
        isolated_db,
        prediction_hash=target,
        decision_session=date(2026, 1, 2),
        exit_session=date(2026, 1, 5),
        available_at=datetime(2026, 1, 18, 12, 0, 0),
        suffix=900,
        supersedes=early_hash,
    )

    manifest = PITDatasetService(db_manager=isolated_db).build_manifest(
        cost_identity_hash=COST_HASH
    )["manifest"]
    target_assignment = next(item for item in manifest["assignments"] if item["prediction_hash"] == target)
    assert target_assignment["outcome_hash"] == early_hash
    assert target_assignment["outcome_hash"] != late_hash
    purged = [item for item in manifest["assignments"] if item["status"] == "PURGED"]
    purge_reasons = {item["purge_or_exclusion_reason"] for item in purged}
    assert "LABEL_INTERVAL_CROSSES_BOUNDARY" in purge_reasons
    assert "OUTCOME_NOT_KNOWABLE_AT_CUTOFF" in purge_reasons


def test_training_admission_stays_blocked_for_local_unapproved_foundation(isolated_db) -> None:
    _seed_twenty_sessions(isolated_db)
    result = PITDatasetService(db_manager=isolated_db).build_manifest(
        cost_identity_hash=COST_HASH,
        cost_identity_approved=False,
        durable_references_admitted=False,
    )
    assert result["training_admission"] == "BLOCKED"
    assert "LOCAL_DB_ONLY_REFERENCES" in result["training_admission_reasons"]
    assert "DURABLE_REFERENCES_NOT_ADMITTED" in result["training_admission_reasons"]
    assert "COST_IDENTITY_NOT_APPROVED" in result["training_admission_reasons"]


def test_asset_level_auto_screen_does_not_require_universe_snapshot(isolated_db) -> None:
    _seed_twenty_sessions(isolated_db, auto_screen=True)
    result = PITDatasetService(db_manager=isolated_db).build_manifest(cost_identity_hash=COST_HASH)
    reasons = result["training_admission_reasons"]
    assert "UNIVERSE_SNAPSHOT_NOT_BOUND" not in reasons
    assert result["manifest"]["counts"]["pit_eligible"] == 20


def test_pit_ineligible_prediction_is_retained_as_gap_and_blocks_admission(isolated_db) -> None:
    _seed_twenty_sessions(isolated_db)
    gap_day = date(2026, 2, 1)
    gap_hash = _seed_prediction(
        isolated_db,
        index=999,
        session_date=gap_day,
        pit_eligible=False,
    )
    result = PITDatasetService(db_manager=isolated_db).build_manifest(cost_identity_hash=COST_HASH)
    assert "PIT_GAPS_PRESENT" in result["training_admission_reasons"]
    gap_assignment = next(
        item for item in result["manifest"]["assignments"] if item["prediction_hash"] == gap_hash
    )
    assert gap_assignment["status"] == "EXCLUDED"
    assert "LEDGER_PIT_INELIGIBLE" in gap_assignment["purge_or_exclusion_reason"]
