# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

import src.services.research_state_runtime as runtime
from src.services.research_state_runtime import (
    R2RuntimeConfig,
    ResearchStateBootstrapConflict,
    ResearchStateRuntimeConfigError,
    durability_enabled,
    publish_runtime_state,
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
