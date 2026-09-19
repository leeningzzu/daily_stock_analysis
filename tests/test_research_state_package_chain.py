# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import create_engine

from src.services.research_state_package_chain import (
    COLUMN_CLASSIFICATION,
    DURABILITY_STATE,
    FORBIDDEN,
    MANIFEST_PREFIX,
    MAX_CHAIN_GENERATIONS,
    MAX_MANIFEST_READS_PER_DISCOVERY,
    MAX_OBJECT_CREATES_PER_GENERATION,
    MAX_PACKAGE_BYTES,
    PACKAGE_PREFIX,
    PUBLICATION_BARRIER,
    REQUIRED_WRITER_CONCURRENCY_GROUP,
    FilesystemObjectStore,
    ImportConflictError,
    ManifestChainError,
    PackageTooLarge,
    RightsAdmissionRequired,
    SimulatedCrashAfterPackage,
    build_checkpoint_package,
    discover_manifest_chain,
    exported_columns,
    manifest_key,
    publish_checkpoint,
    restore_checkpoint,
    sha256_bytes,
    validate_current_schema,
)
from src.storage import Base


CODE_SHA = "a" * 40
NOW = datetime(2026, 9, 19, 4, 0, tzinfo=timezone.utc)


def _create_db(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def _seed_state(path: Path, *, extra_ledger: bool = False) -> None:
    conn = sqlite3.connect(path)
    try:
        ledger_rows = [
            (
                "1" * 64,
                "prediction-ledger-v2",
                101,
                201,
                "trace-local-only",
                "cn",
                "600519",
                "stock",
                "2026-09-18 10:00:00",
                "Asia/Shanghai",
                "2026-09-18",
                "2026-09-18 09:59:00",
                "stock_trend_quality_pullback_v1",
                "stock_trend_quality_pullback_v1",
                "factor-decision-v1",
                "WAIT",
                "3d",
                "balanced",
                "github_schedule",
                "analysis",
                100.0,
                101.0,
                95.0,
                110.0,
                "stock-factor-evidence-v1",
                "f" * 64,
                "e" * 64,
                json.dumps(
                    {
                        "canonical_decision": {
                            "action": "WAIT",
                            "evidence_state": "PROVEN",
                            "hard_veto": False,
                        },
                        "synthetic_feature": 1.25,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                CODE_SHA,
                "SyntheticProvider",
                "qfq",
                None,
                "2" * 64,
                json.dumps(
                    {
                        "version": "cn-stock-asset-v1",
                        "market": "cn",
                        "instrument_type": "stock",
                        "symbol": "600519",
                        "exchange": "SH",
                        "calendar": "XSHG",
                        "timezone": "Asia/Shanghai",
                        "currency": "CNY",
                        "identity_hash": "2" * 64,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "3" * 64,
                "SPECIFIED_CODES",
                "4" * 64,
                json.dumps(
                    {
                        "schema_version": "research-selection-context-v1",
                        "selection_source": "SPECIFIED_CODES",
                        "request_origin": "synthetic_test",
                        "selection_context_hash": "4" * 64,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                1,
                "[]",
                "LOCAL_DB_ONLY",
                "2026-09-18 10:01:00",
            )
        ]
        if extra_ledger:
            second = list(ledger_rows[0])
            second[0] = "5" * 64
            second[2] = 102
            second[3] = 202
            second[6] = "000001"
            second[26] = "6" * 64
            second[29] = "7" * 64
            second[31] = "8" * 64
            second[33] = "9" * 64
            second[37] = "2026-09-19 10:01:00"
            ledger_rows.append(tuple(second))

        conn.executemany(
            """INSERT INTO prediction_ledger (
                prediction_hash,schema_version,analysis_history_id,decision_signal_id,trace_id,
                market,stock_code,instrument_type,decision_time,decision_timezone,data_as_of,
                available_at_max,strategy_id,strategy_version,factor_contract_version,
                canonical_action,horizon,decision_profile,trigger_source,source_type,
                entry_low,entry_high,stop_loss,target_price,feature_schema_version,
                feature_schema_hash,evidence_hash,evidence_json,code_sha,provider_identity,
                adjustment_basis,universe_snapshot_id,asset_identity_hash,asset_identity_json,
                data_snapshot_identity,selection_source,selection_context_hash,
                selection_context_json,pit_eligible,pit_ineligibility_json,durability_state,
                created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ledger_rows,
        )

        outcomes = [
            (
                "a" * 64,
                "b" * 64,
                "1" * 64,
                "META_TAKE_NET_POSITIVE_NEXT_OPEN_3S_FIXED_CLOSE_V1",
                "XSHG_POSTMARKET_NEXT_OPEN_3_FORWARD_SESSIONS_FIXED_CLOSE_V1",
                "c" * 64,
                '{"schema_version":"cost-identity-v2"}',
                "d" * 64,
                '{"schema_version":"execution-identity-v1"}',
                "prediction-outcome-fixed-horizon-v3",
                None,
                None,
                "2026-09-18",
                "2026-09-19",
                "2026-09-23",
                "FILLED",
                100.0,
                101.0,
                1.0,
                0.8,
                0.5,
                1.5,
                1,
                "TAKE_SUCCESS",
                None,
                "3" * 64,
                "SyntheticProvider",
                "qfq",
                "2026-09-23 08:00:00",
                "2026-09-23 08:00:00",
            ),
            (
                "0" * 64,
                "b" * 64,
                "1" * 64,
                "META_TAKE_NET_POSITIVE_NEXT_OPEN_3S_FIXED_CLOSE_V1",
                "XSHG_POSTMARKET_NEXT_OPEN_3_FORWARD_SESSIONS_FIXED_CLOSE_V1",
                "c" * 64,
                '{"schema_version":"cost-identity-v2"}',
                "d" * 64,
                '{"schema_version":"execution-identity-v1","revision":2}',
                "prediction-outcome-fixed-horizon-v3",
                "a" * 64,
                "synthetic-correction",
                "2026-09-18",
                "2026-09-19",
                "2026-09-23",
                "FILLED",
                100.0,
                99.0,
                -1.0,
                -1.2,
                1.5,
                0.5,
                0,
                "TAKE_FAIL",
                None,
                "3" * 64,
                "SyntheticProvider",
                "qfq",
                "2026-09-23 09:00:00",
                "2026-09-23 09:00:00",
            ),
        ]
        conn.executemany(
            """INSERT INTO prediction_outcomes (
                outcome_hash,root_identity_hash,prediction_hash,label_identity,horizon_identity,
                cost_identity_hash,cost_identity_json,execution_identity_hash,execution_identity_json,
                evaluation_engine_version,supersedes_outcome_hash,correction_reason,decision_session,
                entry_session,exit_session,execution_state,entry_price,exit_price,gross_return_pct,
                net_return_pct,max_adverse_excursion_pct,max_favorable_excursion_pct,label_value,
                label_status,label_reason,data_snapshot_identity,provider_identity,adjustment_basis,
                available_at,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            outcomes,
        )

        manifest_payload = {
            "schema_version": "pit-dataset-manifest-v1",
            "assignments": [{"prediction_hash": "1" * 64, "status": "INCLUDED"}],
        }
        conn.execute(
            """INSERT INTO pit_dataset_manifests (
                dataset_hash,schema_version,dataset_purpose,strategy_id,strategy_version,
                feature_schema_version,feature_schema_hash,label_identity,horizon_identity,
                cost_identity_hash,evaluation_engine_version,split_policy,purge_policy,
                embargo_policy,selection_route_policy,code_sha,final_test_state,
                training_admission,training_admission_reasons_json,manifest_json,frozen_at,
                created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "9" * 64,
                "pit-dataset-manifest-v1",
                "ASSET_LEVEL_META_FILTER_ON_SELECTED_OPPORTUNITIES_V1",
                "stock_trend_quality_pullback_v1",
                "stock_trend_quality_pullback_v1",
                "stock-factor-evidence-v1",
                "f" * 64,
                "META_TAKE_NET_POSITIVE_NEXT_OPEN_3S_FIXED_CLOSE_V1",
                "XSHG_POSTMARKET_NEXT_OPEN_3_FORWARD_SESSIONS_FIXED_CLOSE_V1",
                "c" * 64,
                "prediction-outcome-fixed-horizon-v3",
                "XSHG_SESSION_GROUPED_CHRONO_60_20_20_PURGED_V1",
                "EXACT_LABEL_INTERVAL_AND_AVAILABLE_AT",
                "FORWARD_ONLY_ZERO_POST_BLOCK_V1",
                "AUTO_SCREEN_OR_SPECIFIED_CODES_V1",
                CODE_SHA,
                "SEALED",
                "BLOCKED",
                '["DURABLE_REFERENCES_NOT_ADMITTED"]',
                json.dumps(manifest_payload, sort_keys=True, separators=(",", ":")),
                "2026-09-23 09:00:00",
                "2026-09-23 09:00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    path = tmp_path / "source.db"
    _create_db(path)
    _seed_state(path)
    return path


def _read_package(store: FilesystemObjectStore, package_key: str) -> dict:
    return json.loads(store.get_bytes(package_key).decode("utf-8"))


def test_column_classification_covers_current_schema(seeded_db: Path) -> None:
    validate_current_schema(seeded_db)
    conn = sqlite3.connect(seeded_db)
    try:
        for table, classes in COLUMN_CLASSIFICATION.items():
            actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert actual == set(classes)
            assert all(value in {FORBIDDEN, "SAFE_LOW_SENSITIVITY", "RIGHTS_CONDITIONAL"} for value in classes.values())
    finally:
        conn.close()


def test_rights_conditional_fields_fail_closed_without_admission(seeded_db: Path) -> None:
    with pytest.raises(RightsAdmissionRequired):
        build_checkpoint_package(seeded_db)


def test_empty_state_is_packageable_without_rights_admission(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    _create_db(db)
    package = build_checkpoint_package(db)
    assert package.table_counts == {
        "prediction_ledger": 0,
        "prediction_outcomes": 0,
        "pit_dataset_manifests": 0,
    }


def test_roundtrip_is_deterministic_and_duplicate_restore_is_idempotent(
    seeded_db: Path,
    tmp_path: Path,
) -> None:
    store = FilesystemObjectStore(tmp_path / "objects")
    first_package = build_checkpoint_package(seeded_db, rights_admitted=True)
    second_package = build_checkpoint_package(seeded_db, rights_admitted=True)
    assert first_package.payload == second_package.payload
    assert first_package.sha256 == second_package.sha256

    published = publish_checkpoint(
        store,
        seeded_db,
        source_code_sha=CODE_SHA,
        created_at=NOW,
        rights_admitted=True,
        rights_classification="SYNTHETIC_TEST_ONLY",
    )
    target = tmp_path / "target.db"
    _create_db(target)

    restored = restore_checkpoint(store, target)
    assert restored.inserted == {
        "prediction_ledger": 1,
        "prediction_outcomes": 2,
        "pit_dataset_manifests": 1,
    }
    again = restore_checkpoint(store, target)
    assert again.existing == {
        "prediction_ledger": 1,
        "prediction_outcomes": 2,
        "pit_dataset_manifests": 1,
    }

    roundtrip = build_checkpoint_package(target, rights_admitted=True)
    assert roundtrip.payload == first_package.payload
    assert roundtrip.sha256 == published.package_sha256

    conn = sqlite3.connect(target)
    try:
        row = conn.execute(
            "SELECT analysis_history_id,decision_signal_id,durability_state "
            "FROM prediction_ledger WHERE prediction_hash=?",
            ("1" * 64,),
        ).fetchone()
        assert row == (0, None, DURABILITY_STATE)
        correction = conn.execute(
            "SELECT supersedes_outcome_hash,correction_reason "
            "FROM prediction_outcomes WHERE outcome_hash=?",
            ("0" * 64,),
        ).fetchone()
        assert correction == ("a" * 64, "synthetic-correction")
        pit = conn.execute(
            "SELECT manifest_json FROM pit_dataset_manifests WHERE dataset_hash=?",
            ("9" * 64,),
        ).fetchone()
        assert json.loads(pit[0])["assignments"][0]["prediction_hash"] == "1" * 64
    finally:
        conn.close()


def test_forbidden_columns_never_enter_package(seeded_db: Path) -> None:
    package = build_checkpoint_package(seeded_db, rights_admitted=True)
    doc = json.loads(package.payload.decode("utf-8"))
    ledger = doc["tables"]["prediction_ledger"]
    forbidden = {name for name, cls in COLUMN_CLASSIFICATION["prediction_ledger"].items() if cls == FORBIDDEN}
    assert forbidden.isdisjoint(ledger["columns"])
    assert forbidden.isdisjoint(ledger["rows"][0])
    assert {"analysis_history_id", "decision_signal_id", "entry_low", "target_price", "durability_state"} <= forbidden


def test_same_identity_different_durable_content_stops(seeded_db: Path, tmp_path: Path) -> None:
    store = FilesystemObjectStore(tmp_path / "objects")
    publish_checkpoint(
        store,
        seeded_db,
        source_code_sha=CODE_SHA,
        created_at=NOW,
        rights_admitted=True,
        rights_classification="SYNTHETIC_TEST_ONLY",
    )
    target = tmp_path / "target.db"
    _create_db(target)
    restore_checkpoint(store, target)

    conn = sqlite3.connect(target)
    try:
        conn.execute(
            "UPDATE prediction_outcomes SET label_status='UNLABELABLE' WHERE outcome_hash=?",
            ("a" * 64,),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ImportConflictError):
        restore_checkpoint(store, target)


def test_manifest_package_key_and_rights_classification_fail_closed(
    seeded_db: Path,
    tmp_path: Path,
) -> None:
    store = FilesystemObjectStore(tmp_path / "objects")
    receipt = publish_checkpoint(
        store,
        seeded_db,
        source_code_sha=CODE_SHA,
        created_at=NOW,
        rights_admitted=True,
        rights_classification="SYNTHETIC_TEST_ONLY",
    )
    manifest_path = store._path(receipt.manifest_key)
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    doc["package_key"] = f"{PACKAGE_PREFIX}/{'0' * 64}.json"
    manifest_path.write_text(
        json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    with pytest.raises(ManifestChainError):
        discover_manifest_chain(store)

    fresh_store = FilesystemObjectStore(tmp_path / "rights")
    with pytest.raises(RightsAdmissionRequired):
        publish_checkpoint(
            fresh_store,
            seeded_db,
            source_code_sha=CODE_SHA,
            created_at=NOW,
            rights_admitted=True,
            rights_classification="NO_CONDITIONAL_VALUES",
        )


def test_cross_table_lineage_and_pit_references_fail_closed(
    seeded_db: Path,
    tmp_path: Path,
) -> None:
    package = build_checkpoint_package(seeded_db, rights_admitted=True)
    doc = json.loads(package.payload.decode("utf-8"))

    bad_outcome = json.loads(json.dumps(doc))
    bad_outcome["tables"]["prediction_outcomes"]["rows"][0]["prediction_hash"] = "f" * 64
    _rehash_table(bad_outcome, "prediction_outcomes")
    bad_bytes = json.dumps(
        bad_outcome, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    store = FilesystemObjectStore(tmp_path / "bad-outcome")
    bad_sha = sha256_bytes(bad_bytes)
    bad_key = f"{PACKAGE_PREFIX}/{bad_sha}.json"
    assert store.put_if_absent(bad_key, bad_bytes)
    with pytest.raises(ManifestChainError):
        from src.services.research_state_package_chain import _validate_package_document
        _validate_package_document(bad_bytes)

    bad_pit = json.loads(json.dumps(doc))
    pit_manifest = json.loads(
        bad_pit["tables"]["pit_dataset_manifests"]["rows"][0]["manifest_json"]
    )
    pit_manifest["assignments"][0]["prediction_hash"] = "f" * 64
    bad_pit["tables"]["pit_dataset_manifests"]["rows"][0]["manifest_json"] = json.dumps(
        pit_manifest, sort_keys=True, separators=(",", ":")
    )
    _rehash_table(bad_pit, "pit_dataset_manifests")
    bad_pit_bytes = json.dumps(
        bad_pit, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    with pytest.raises(ManifestChainError):
        from src.services.research_state_package_chain import _validate_package_document
        _validate_package_document(bad_pit_bytes)


def test_package_corruption_stops_restore(seeded_db: Path, tmp_path: Path) -> None:
    store = FilesystemObjectStore(tmp_path / "objects")
    receipt = publish_checkpoint(
        store,
        seeded_db,
        source_code_sha=CODE_SHA,
        created_at=NOW,
        rights_admitted=True,
        rights_classification="SYNTHETIC_TEST_ONLY",
    )
    path = store._path(receipt.package_key)
    path.write_bytes(path.read_bytes() + b"x")
    target = tmp_path / "target.db"
    _create_db(target)
    with pytest.raises(ManifestChainError):
        restore_checkpoint(store, target)


def test_parent_hash_corruption_and_generation_fork_stop(
    seeded_db: Path,
    tmp_path: Path,
) -> None:
    store = FilesystemObjectStore(tmp_path / "objects")
    publish_checkpoint(
        store,
        seeded_db,
        source_code_sha=CODE_SHA,
        created_at=NOW,
        rights_admitted=True,
        rights_classification="SYNTHETIC_TEST_ONLY",
    )
    publish_checkpoint(
        store,
        seeded_db,
        source_code_sha=CODE_SHA,
        created_at=datetime(2026, 9, 20, 4, 0, tzinfo=timezone.utc),
        rights_admitted=True,
        rights_classification="SYNTHETIC_TEST_ONLY",
    )
    second = store._path(manifest_key(2))
    doc = json.loads(second.read_text(encoding="utf-8"))
    doc["parent_manifest_sha256"] = "0" * 64
    second.write_text(
        json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    with pytest.raises(ManifestChainError):
        discover_manifest_chain(store)

    # A second key that tries to encode generation 2 is not part of the exact
    # key contract and therefore fails closed instead of silently forking.
    fork = store._path(f"{MANIFEST_PREFIX}/00000000000000000002-fork.json")
    fork.parent.mkdir(parents=True, exist_ok=True)
    fork.write_text("{}", encoding="utf-8")
    with pytest.raises(ManifestChainError):
        discover_manifest_chain(store)


def test_orphan_and_crash_before_manifest_do_not_advance_chain(
    seeded_db: Path,
    tmp_path: Path,
) -> None:
    store = FilesystemObjectStore(tmp_path / "objects")
    package = build_checkpoint_package(seeded_db, rights_admitted=True)
    assert store.put_if_absent(package.key, package.payload) is True
    assert discover_manifest_chain(store) == []

    with pytest.raises(SimulatedCrashAfterPackage):
        publish_checkpoint(
            store,
            seeded_db,
            source_code_sha=CODE_SHA,
            created_at=NOW,
            rights_admitted=True,
            rights_classification="SYNTHETIC_TEST_ONLY",
            crash_after_package=True,
        )
    assert discover_manifest_chain(store) == []


def test_explicit_earlier_generation_restore_is_a_real_rollback(
    seeded_db: Path,
    tmp_path: Path,
) -> None:
    store = FilesystemObjectStore(tmp_path / "objects")
    publish_checkpoint(
        store,
        seeded_db,
        source_code_sha=CODE_SHA,
        created_at=NOW,
        rights_admitted=True,
        rights_classification="SYNTHETIC_TEST_ONLY",
    )

    # Add one more synthetic ledger row, then publish a second full checkpoint.
    _seed_extra_ledger_only(seeded_db)
    publish_checkpoint(
        store,
        seeded_db,
        source_code_sha=CODE_SHA,
        created_at=datetime(2026, 9, 20, 4, 0, tzinfo=timezone.utc),
        rights_admitted=True,
        rights_classification="SYNTHETIC_TEST_ONLY",
    )

    old_target = tmp_path / "old.db"
    new_target = tmp_path / "new.db"
    _create_db(old_target)
    _create_db(new_target)
    restore_checkpoint(store, old_target, generation=1)
    restore_checkpoint(store, new_target)

    assert _count(old_target, "prediction_ledger") == 1
    assert _count(new_target, "prediction_ledger") == 2


def test_package_byte_cap_fails_closed(seeded_db: Path) -> None:
    with pytest.raises(PackageTooLarge):
        build_checkpoint_package(
            seeded_db,
            rights_admitted=True,
            max_package_bytes=128,
        )


def test_chain_hard_cap_and_workflow_contract_are_bounded() -> None:
    assert MAX_CHAIN_GENERATIONS == 1000
    assert MAX_PACKAGE_BYTES == 8 * 1024 * 1024
    assert MAX_OBJECT_CREATES_PER_GENERATION == 2
    assert MAX_MANIFEST_READS_PER_DISCOVERY == 1000
    assert PUBLICATION_BARRIER == "PACKAGE_FIRST_MANIFEST_LAST"
    assert REQUIRED_WRITER_CONCURRENCY_GROUP == "stock-analysis"

    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "00-daily-analysis.yml").read_text(
        encoding="utf-8"
    )
    assert "group: stock-analysis" in workflow
    assert "cancel-in-progress: false" in workflow


def _rehash_table(document: dict, table: str) -> None:
    rows = document["tables"][table]["rows"]
    row_hashes = [
        sha256_bytes(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        for row in rows
    ]
    document["tables"][table]["table_root_sha256"] = sha256_bytes(
        json.dumps(row_hashes, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _seed_extra_ledger_only(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        source = conn.execute(
            "SELECT * FROM prediction_ledger WHERE prediction_hash=?",
            ("1" * 64,),
        ).fetchone()
        columns = [row[1] for row in conn.execute("PRAGMA table_info(prediction_ledger)")]
        item = dict(zip(columns, source))
        item.pop("id")
        item["prediction_hash"] = "5" * 64
        item["analysis_history_id"] = 102
        item["decision_signal_id"] = 202
        item["stock_code"] = "000001"
        item["evidence_hash"] = "6" * 64
        item["asset_identity_hash"] = "7" * 64
        item["data_snapshot_identity"] = "8" * 64
        item["selection_context_hash"] = "9" * 64
        item["created_at"] = "2026-09-19 10:01:00"
        columns2 = tuple(item)
        conn.execute(
            "INSERT INTO prediction_ledger ("
            + ",".join(columns2)
            + ") VALUES ("
            + ",".join("?" for _ in columns2)
            + ")",
            tuple(item[column] for column in columns2),
        )
        conn.commit()
    finally:
        conn.close()


def _count(path: Path, table: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()
