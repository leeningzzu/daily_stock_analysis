# -*- coding: utf-8 -*-
from __future__ import annotations

import json

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

import src.services.research_state_runtime as runtime
from src.services.research_state_package_chain import FilesystemObjectStore, PACKAGE_PREFIX
from src.services.research_state_runtime import (
    R2RuntimeConfig,
    ResearchStateBootstrapConflict,
    ResearchStateRuntimeConfigError,
    ResearchStateSmokeBoundaryError,
    durability_enabled,
    publish_empty_smoke_state,
    publish_runtime_state,
    restore_empty_smoke_state,
    restore_runtime_state,
)
from src.storage import Base


ENABLED_ENV = {
    "RESEARCH_STATE_DURABILITY_ENABLED": "true",
    "R2_ENDPOINT_URL": "https://example.r2.cloudflarestorage.com",
    "R2_BUCKET_NAME": "synthetic-research-state",
    "R2_ACCESS_KEY_ID": "synthetic-access",
    "R2_SECRET_ACCESS_KEY": "synthetic-secret",
    "GITHUB_SHA": "a" * 40,
}


def _create_db(path: Path) -> Path:
    engine = create_engine(f"sqlite:///{path}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    return path


def _ensure(path: Path) -> Path:
    return _create_db(path)


def test_default_off_path_has_zero_store_or_schema_effect(tmp_path: Path) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled path must not touch durability dependencies")

    env = {"DATABASE_PATH": str(tmp_path / "never-created.db")}
    assert durability_enabled(env) is False
    assert restore_runtime_state(
        env=env, store_factory=forbidden, ensure_schema=forbidden
    ).disposition == "DISABLED"
    assert publish_runtime_state(
        env=env, store_factory=forbidden, ensure_schema=forbidden
    ).disposition == "DISABLED"
    assert not (tmp_path / "never-created.db").exists()


def test_invalid_enable_or_missing_config_fails_closed_without_secret_value() -> None:
    with pytest.raises(ResearchStateRuntimeConfigError):
        durability_enabled({"RESEARCH_STATE_DURABILITY_ENABLED": "maybe"})

    secret = "do-not-leak-secret"
    env = dict(ENABLED_ENV)
    env.pop("R2_BUCKET_NAME")
    env["R2_SECRET_ACCESS_KEY"] = secret
    with pytest.raises(ResearchStateRuntimeConfigError) as excinfo:
        restore_runtime_state(env=env)
    assert secret not in str(excinfo.value)


def test_runtime_config_repr_redacts_credentials() -> None:
    config = R2RuntimeConfig(
        endpoint_url=ENABLED_ENV["R2_ENDPOINT_URL"],
        bucket_name=ENABLED_ENV["R2_BUCKET_NAME"],
        access_key_id=ENABLED_ENV["R2_ACCESS_KEY_ID"],
        secret_access_key=ENABLED_ENV["R2_SECRET_ACCESS_KEY"],
    )
    text = repr(config)
    assert ENABLED_ENV["R2_ACCESS_KEY_ID"] not in text
    assert ENABLED_ENV["R2_SECRET_ACCESS_KEY"] not in text


def test_empty_remote_chain_allows_only_empty_local_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    env = dict(ENABLED_ENV, DATABASE_PATH=str(db_path))
    fake_store = object()
    monkeypatch.setattr(runtime, "discover_manifest_chain", lambda store: [])

    receipt = restore_runtime_state(
        env=env, store_factory=lambda config: fake_store, ensure_schema=_ensure
    )
    assert receipt.disposition == "BOOTSTRAP_EMPTY_CHAIN"

    monkeypatch.setattr(
        runtime,
        "research_state_counts",
        lambda path: {
            "prediction_ledger": 1,
            "prediction_outcomes": 0,
            "pit_dataset_manifests": 0,
        },
    )
    with pytest.raises(ResearchStateBootstrapConflict):
        restore_runtime_state(
            env=env,
            store_factory=lambda config: fake_store,
            ensure_schema=lambda path: path,
        )


def test_existing_chain_restore_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    env = dict(ENABLED_ENV, DATABASE_PATH=str(db_path))
    events: list[str] = []
    fake_store = object()

    def ensure(path: Path) -> Path:
        events.append("schema")
        return _create_db(path)

    def discover(store):
        events.append("discover")
        return [SimpleNamespace(generation=3, document={"package_sha256": "x"})]

    def restore(store, path):
        events.append("restore")
        return SimpleNamespace(generation=3)

    monkeypatch.setattr(runtime, "discover_manifest_chain", discover)
    monkeypatch.setattr(runtime, "restore_checkpoint", restore)
    receipt = restore_runtime_state(
        env=env, store_factory=lambda config: fake_store, ensure_schema=ensure
    )
    assert receipt.disposition == "RESTORED"
    assert receipt.generation == 3
    assert events == ["schema", "discover", "restore"]


def test_publish_with_no_local_state_makes_zero_store_calls(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    env = dict(ENABLED_ENV, DATABASE_PATH=str(db_path))

    def forbidden(config):
        raise AssertionError("empty local state must not construct the R2 store")

    receipt = publish_runtime_state(
        env=env, store_factory=forbidden, ensure_schema=_ensure
    )
    assert receipt.disposition == "NO_LOCAL_RESEARCH_STATE"


def test_publish_success_uses_fail_closed_rights_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    env = dict(ENABLED_ENV, DATABASE_PATH=str(db_path))
    fake_store = object()

    monkeypatch.setattr(
        runtime,
        "research_state_counts",
        lambda path: {
            "prediction_ledger": 1,
            "prediction_outcomes": 0,
            "pit_dataset_manifests": 0,
        },
    )
    monkeypatch.setattr(
        runtime,
        "build_checkpoint_package",
        lambda *args, **kwargs: SimpleNamespace(sha256="new-sha"),
    )
    monkeypatch.setattr(runtime, "discover_manifest_chain", lambda store: [])

    def publish(store, path, **kwargs):
        assert kwargs["rights_admitted"] is False
        assert kwargs["rights_classification"] == "NO_CONDITIONAL_VALUES"
        assert kwargs["source_code_sha"] == "a" * 40
        return SimpleNamespace(generation=1)

    monkeypatch.setattr(runtime, "publish_checkpoint", publish)
    receipt = publish_runtime_state(
        env=env, store_factory=lambda config: fake_store, ensure_schema=_ensure
    )
    assert receipt.disposition == "PUBLISHED"
    assert receipt.generation == 1


def test_publish_skips_unchanged_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    env = dict(ENABLED_ENV, DATABASE_PATH=str(db_path))
    fake_store = object()
    monkeypatch.setattr(
        runtime,
        "research_state_counts",
        lambda path: {
            "prediction_ledger": 1,
            "prediction_outcomes": 0,
            "pit_dataset_manifests": 0,
        },
    )
    monkeypatch.setattr(
        runtime,
        "build_checkpoint_package",
        lambda *args, **kwargs: SimpleNamespace(sha256="same-sha"),
    )
    monkeypatch.setattr(
        runtime,
        "discover_manifest_chain",
        lambda store: [SimpleNamespace(generation=7, document={"package_sha256": "same-sha"})],
    )
    monkeypatch.setattr(
        runtime,
        "publish_checkpoint",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unchanged projection must not publish")
        ),
    )
    receipt = publish_runtime_state(
        env=env, store_factory=lambda config: fake_store, ensure_schema=_ensure
    )
    assert receipt.disposition == "UNCHANGED"
    assert receipt.generation == 7


def test_publish_requires_exact_source_sha_when_state_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    env = dict(ENABLED_ENV, DATABASE_PATH=str(db_path), GITHUB_SHA="short")
    monkeypatch.setattr(
        runtime,
        "research_state_counts",
        lambda path: {
            "prediction_ledger": 1,
            "prediction_outcomes": 0,
            "pit_dataset_manifests": 0,
        },
    )
    with pytest.raises(ResearchStateRuntimeConfigError, match="GITHUB_SHA"):
        publish_runtime_state(
            env=env,
            store_factory=lambda config: object(),
            ensure_schema=_ensure,
        )


def test_empty_smoke_publish_then_restore_is_identity_bound(tmp_path: Path) -> None:
    store = FilesystemObjectStore(tmp_path / "objects")
    publish_db = tmp_path / "publish.db"
    publish_env = dict(ENABLED_ENV, DATABASE_PATH=str(publish_db))

    published = publish_empty_smoke_state(
        env=publish_env,
        store_factory=lambda config: store,
        ensure_schema=_ensure,
    )
    zero_counts = {
        "prediction_ledger": 0,
        "prediction_outcomes": 0,
        "pit_dataset_manifests": 0,
    }
    assert published.disposition == "PUBLISHED_EMPTY_CHECKPOINT"
    assert published.generation == 1
    assert dict(published.table_counts) == zero_counts
    assert published.manifest_key.endswith("00000000000000000001.json")
    assert published.package_key.startswith(f"{PACKAGE_PREFIX}/")
    assert published.source_code_sha == "a" * 40
    assert published.rights_classification == "NO_CONDITIONAL_VALUES"
    assert 0 < published.package_bytes <= runtime.SMOKE_MAX_PACKAGE_BYTES

    restore_db = tmp_path / "restore.db"
    restore_env = dict(ENABLED_ENV, DATABASE_PATH=str(restore_db))
    restored = restore_empty_smoke_state(
        env=restore_env,
        store_factory=lambda config: store,
        ensure_schema=_ensure,
    )
    assert restored.disposition == "RESTORED_EMPTY_CHECKPOINT"
    assert restored.generation == published.generation
    assert restored.manifest_key == published.manifest_key
    assert restored.manifest_sha256 == published.manifest_sha256
    assert restored.package_key == published.package_key
    assert restored.package_sha256 == published.package_sha256
    assert restored.source_code_sha == published.source_code_sha
    assert restored.rights_classification == published.rights_classification
    assert restored.package_bytes == published.package_bytes
    assert dict(restored.table_counts) == zero_counts
    assert dict(restored.inserted or {}) == zero_counts
    assert dict(restored.existing or {}) == zero_counts

    with pytest.raises(ResearchStateSmokeBoundaryError, match="same source code SHA"):
        restore_empty_smoke_state(
            env=dict(restore_env, GITHUB_SHA="b" * 40, DATABASE_PATH=str(tmp_path / "mismatch.db")),
            store_factory=lambda config: store,
            ensure_schema=_ensure,
        )


def test_empty_smoke_refuses_nonempty_local_or_remote_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = dict(ENABLED_ENV, DATABASE_PATH=str(tmp_path / "state.db"))
    monkeypatch.setattr(
        runtime,
        "research_state_counts",
        lambda path: {
            "prediction_ledger": 1,
            "prediction_outcomes": 0,
            "pit_dataset_manifests": 0,
        },
    )
    with pytest.raises(ResearchStateSmokeBoundaryError, match="non-empty local"):
        publish_empty_smoke_state(
            env=env,
            store_factory=lambda config: (_ for _ in ()).throw(
                AssertionError("non-empty local state must fail before store construction")
            ),
            ensure_schema=_ensure,
        )

    monkeypatch.undo()
    store = FilesystemObjectStore(tmp_path / "objects")
    store.put_if_absent(f"{PACKAGE_PREFIX}/orphan.json", b"{}")
    with pytest.raises(ResearchStateSmokeBoundaryError, match="fresh research-state"):
        publish_empty_smoke_state(
            env=env,
            store_factory=lambda config: store,
            ensure_schema=_ensure,
        )


def test_workflow_binds_restore_before_analysis_and_publish_after_success() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "00-daily-analysis.yml"
    ).read_text(encoding="utf-8")

    restore_marker = "- name: 恢复研究状态（默认关闭）"
    analysis_marker = "- name: 执行股票分析"
    publish_marker = "- name: 发布研究状态（默认关闭）"
    artifact_marker = "- name: 上传分析报告"

    assert workflow.index(restore_marker) < workflow.index(analysis_marker)
    assert workflow.index(analysis_marker) < workflow.index(publish_marker)
    assert workflow.index(publish_marker) < workflow.index(artifact_marker)
    assert "group: stock-analysis" in workflow
    assert "cancel-in-progress: false" in workflow

    restore_block = workflow[workflow.index(restore_marker):workflow.index(analysis_marker)]
    publish_block = workflow[workflow.index(publish_marker):workflow.index(artifact_marker)]
    assert "vars.RESEARCH_STATE_DURABILITY_ENABLED == 'true'" in restore_block
    assert "github.event.inputs.mode != 'market-only'" in restore_block
    assert "continue-on-error" not in restore_block
    assert "python -m src.services.research_state_runtime restore" in restore_block
    analysis_block = workflow[
        workflow.index(analysis_marker):workflow.index(publish_marker)
    ]
    assert (
        "RESEARCH_STATE_DURABILITY_ENABLED: "
        "${{ vars.RESEARCH_STATE_DURABILITY_ENABLED || 'false' }}"
        in analysis_block
    )
    assert "success()" in publish_block
    assert "vars.RESEARCH_STATE_DURABILITY_ENABLED == 'true'" in publish_block
    assert "github.event.inputs.mode != 'market-only'" in publish_block
    assert "continue-on-error" not in publish_block
    assert "python -m src.services.research_state_runtime publish" in publish_block

    assert "- research-state-smoke" in workflow
    smoke_marker = "- name: 研究状态空检查点 Smoke（仅人工）"
    smoke_block = workflow[workflow.index(smoke_marker):workflow.index(restore_marker)]
    assert "github.event_name == 'workflow_dispatch'" in smoke_block
    assert "github.event.inputs.mode == 'research-state-smoke'" in smoke_block
    assert "RESEARCH_STATE_DURABILITY_ENABLED: 'true'" in smoke_block
    assert "DATABASE_PATH: ./data/research_state_smoke.db" in smoke_block
    assert 'python -m src.services.research_state_runtime "smoke-$PHASE"' in smoke_block
    assert "main.py" not in smoke_block
    assert "上传研究状态 Smoke Receipt" in smoke_block
    assert "research-state-smoke-receipt-${{ github.run_id }}" in smoke_block
    assert "retention-days: 1" in smoke_block
    assert "research-state-smoke" in restore_block
    assert "research-state-smoke" in analysis_block
    assert "research-state-smoke" in publish_block


def test_smoke_cli_emits_machine_readable_identity_receipt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {
        "phase": "smoke-publish-empty",
        "disposition": "PUBLISHED_EMPTY_CHECKPOINT",
        "generation": 1,
        "source_code_sha": "a" * 40,
        "manifest_key": "research-state/v1/manifests/00000000000000000001.json",
        "manifest_sha256": "b" * 64,
        "package_key": f"{PACKAGE_PREFIX}/{'c' * 64}.json",
        "package_sha256": "c" * 64,
        "package_bytes": 1024,
        "rights_classification": "NO_CONDITIONAL_VALUES",
        "table_counts": {
            "prediction_ledger": 0,
            "prediction_outcomes": 0,
            "pit_dataset_manifests": 0,
        },
    }
    monkeypatch.setattr(
        runtime,
        "publish_empty_smoke_state",
        lambda: SimpleNamespace(as_public_dict=lambda: payload),
    )
    assert runtime.main(["smoke-publish-empty"]) == 0
    assert json.loads(capsys.readouterr().out) == payload


def test_public_cli_error_receipt_does_not_echo_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "never-print-this-secret"
    monkeypatch.setenv("RESEARCH_STATE_DURABILITY_ENABLED", "true")
    monkeypatch.setenv("R2_ENDPOINT_URL", ENABLED_ENV["R2_ENDPOINT_URL"])
    monkeypatch.delenv("R2_BUCKET_NAME", raising=False)
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "private-access")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", secret)
    assert runtime.main(["restore"]) == 1
    captured = capsys.readouterr()
    assert secret not in captured.err
    assert "private-access" not in captured.err
    assert "ResearchStateRuntimeConfigError" in captured.err
