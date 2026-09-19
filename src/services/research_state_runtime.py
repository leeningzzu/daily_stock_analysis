# -*- coding: utf-8 -*-
"""Default-off runtime binding for durable research state."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Callable, Mapping, Optional

from src.services.r2_s3_object_store import R2S3ObjectStore
from src.services.research_state_package_chain import (
    PACKAGE_PREFIX,
    build_checkpoint_package,
    discover_manifest_chain,
    publish_checkpoint,
    restore_checkpoint,
    validate_current_schema,
)
from src.storage import DatabaseManager


ENABLE_ENV = "RESEARCH_STATE_DURABILITY_ENABLED"
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"", "0", "false", "no", "off"}
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_RESEARCH_TABLES = (
    "prediction_ledger",
    "prediction_outcomes",
    "pit_dataset_manifests",
)
SMOKE_MAX_PACKAGE_BYTES = 64 * 1024


class ResearchStateRuntimeError(RuntimeError):
    code = "RESEARCH_STATE_RUNTIME_ERROR"


class ResearchStateRuntimeConfigError(ResearchStateRuntimeError):
    code = "RESEARCH_STATE_RUNTIME_CONFIG_ERROR"


class ResearchStateBootstrapConflict(ResearchStateRuntimeError):
    code = "RESEARCH_STATE_BOOTSTRAP_CONFLICT"


class ResearchStateSmokeBoundaryError(ResearchStateRuntimeError):
    code = "RESEARCH_STATE_SMOKE_BOUNDARY_ERROR"


@dataclass(frozen=True)
class R2RuntimeConfig:
    endpoint_url: str
    bucket_name: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)


@dataclass(frozen=True)
class RuntimePhaseReceipt:
    phase: str
    enabled: bool
    disposition: str
    generation: Optional[int] = None

    def as_public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SmokePhaseReceipt:
    phase: str
    disposition: str
    generation: int
    manifest_key: str
    manifest_sha256: str
    package_key: str
    package_sha256: str
    source_code_sha: str
    rights_classification: str
    package_bytes: int
    table_counts: Mapping[str, int]
    inserted: Optional[Mapping[str, int]] = None
    existing: Optional[Mapping[str, int]] = None

    def as_public_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in asdict(self).items()
            if value is not None
        }


def durability_enabled(env: Mapping[str, str]) -> bool:
    raw = str(env.get(ENABLE_ENV, "") or "").strip().lower()
    if raw in _FALSE_VALUES:
        return False
    if raw in _TRUE_VALUES:
        return True
    raise ResearchStateRuntimeConfigError(
        f"{ENABLE_ENV} must be one of true/false, 1/0, yes/no, on/off"
    )


def load_r2_runtime_config(env: Mapping[str, str]) -> R2RuntimeConfig:
    return R2RuntimeConfig(
        endpoint_url=_required_env(env, "R2_ENDPOINT_URL"),
        bucket_name=_required_env(env, "R2_BUCKET_NAME"),
        access_key_id=_required_env(env, "R2_ACCESS_KEY_ID"),
        secret_access_key=_required_env(env, "R2_SECRET_ACCESS_KEY"),
    )


def resolve_database_path(env: Mapping[str, str]) -> Path:
    raw = str(env.get("DATABASE_PATH", "./data/stock_analysis.db") or "").strip()
    if not raw:
        raise ResearchStateRuntimeConfigError("DATABASE_PATH cannot be empty")
    if "://" in raw or raw == ":memory:":
        raise ResearchStateRuntimeConfigError(
            "research-state durability requires a file-backed SQLite DATABASE_PATH"
        )
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def ensure_database_schema(db_path: Path) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    DatabaseManager(db_url=f"sqlite:///{db_path.as_posix()}")
    validate_current_schema(db_path)
    return db_path


def build_r2_store(config: R2RuntimeConfig) -> R2S3ObjectStore:
    return R2S3ObjectStore(
        endpoint_url=config.endpoint_url,
        bucket_name=config.bucket_name,
        access_key_id=config.access_key_id,
        secret_access_key=config.secret_access_key,
    )


def restore_runtime_state(
    *,
    env: Optional[Mapping[str, str]] = None,
    store_factory: Callable[[R2RuntimeConfig], Any] = build_r2_store,
    ensure_schema: Callable[[Path], Path] = ensure_database_schema,
) -> RuntimePhaseReceipt:
    env_map = os.environ if env is None else env
    if not durability_enabled(env_map):
        return RuntimePhaseReceipt("restore", False, "DISABLED")

    config = load_r2_runtime_config(env_map)
    db_path = ensure_schema(resolve_database_path(env_map))
    store = store_factory(config)
    chain = discover_manifest_chain(store)

    if not chain:
        counts = research_state_counts(db_path)
        if any(counts.values()):
            raise ResearchStateBootstrapConflict(
                "remote research-state chain is empty while local research-state tables are non-empty"
            )
        return RuntimePhaseReceipt("restore", True, "BOOTSTRAP_EMPTY_CHAIN")

    receipt = restore_checkpoint(store, db_path)
    return RuntimePhaseReceipt("restore", True, "RESTORED", generation=receipt.generation)


def publish_runtime_state(
    *,
    env: Optional[Mapping[str, str]] = None,
    store_factory: Callable[[R2RuntimeConfig], Any] = build_r2_store,
    ensure_schema: Callable[[Path], Path] = ensure_database_schema,
    now: Optional[datetime] = None,
) -> RuntimePhaseReceipt:
    env_map = os.environ if env is None else env
    if not durability_enabled(env_map):
        return RuntimePhaseReceipt("publish", False, "DISABLED")

    config = load_r2_runtime_config(env_map)
    db_path = ensure_schema(resolve_database_path(env_map))
    counts = research_state_counts(db_path)
    if not any(counts.values()):
        return RuntimePhaseReceipt("publish", True, "NO_LOCAL_RESEARCH_STATE")

    source_code_sha = _required_source_code_sha(env_map)
    store = store_factory(config)
    package = build_checkpoint_package(db_path, rights_admitted=False)
    chain = discover_manifest_chain(store)
    if chain and chain[-1].document.get("package_sha256") == package.sha256:
        return RuntimePhaseReceipt(
            "publish", True, "UNCHANGED", generation=chain[-1].generation
        )

    receipt = publish_checkpoint(
        store,
        db_path,
        source_code_sha=source_code_sha,
        created_at=now or datetime.now(timezone.utc),
        rights_admitted=False,
        rights_classification="NO_CONDITIONAL_VALUES",
    )
    return RuntimePhaseReceipt("publish", True, "PUBLISHED", generation=receipt.generation)


def publish_empty_smoke_state(
    *,
    env: Optional[Mapping[str, str]] = None,
    store_factory: Callable[[R2RuntimeConfig], Any] = build_r2_store,
    ensure_schema: Callable[[Path], Path] = ensure_database_schema,
    now: Optional[datetime] = None,
) -> SmokePhaseReceipt:
    """Publish exactly one empty checkpoint into a fresh research-state prefix."""
    env_map = os.environ if env is None else env
    _require_smoke_enabled(env_map)
    config = load_r2_runtime_config(env_map)
    db_path = ensure_schema(resolve_database_path(env_map))
    counts = _require_empty_research_state(db_path)
    source_code_sha = _required_source_code_sha(env_map)
    store = store_factory(config)

    chain = discover_manifest_chain(store)
    package_keys = store.list_keys(PACKAGE_PREFIX)
    if chain or package_keys:
        raise ResearchStateSmokeBoundaryError(
            "empty-checkpoint publish requires fresh research-state package/manifest prefixes"
        )

    package = build_checkpoint_package(
        db_path,
        rights_admitted=False,
        max_package_bytes=SMOKE_MAX_PACKAGE_BYTES,
    )
    if dict(package.table_counts) != counts:
        raise ResearchStateSmokeBoundaryError(
            "empty-checkpoint package table counts do not match the fresh local database"
        )

    receipt = publish_checkpoint(
        store,
        db_path,
        source_code_sha=source_code_sha,
        created_at=now or datetime.now(timezone.utc),
        rights_admitted=False,
        rights_classification="NO_CONDITIONAL_VALUES",
        max_package_bytes=SMOKE_MAX_PACKAGE_BYTES,
    )
    if receipt.generation != 1 or receipt.package_sha256 != package.sha256:
        raise ResearchStateSmokeBoundaryError(
            "empty-checkpoint publish did not commit the expected generation-1 package"
        )

    return SmokePhaseReceipt(
        phase="smoke-publish-empty",
        disposition="PUBLISHED_EMPTY_CHECKPOINT",
        generation=receipt.generation,
        manifest_key=receipt.manifest_key,
        manifest_sha256=receipt.manifest_sha256,
        package_key=receipt.package_key,
        package_sha256=receipt.package_sha256,
        source_code_sha=source_code_sha,
        rights_classification="NO_CONDITIONAL_VALUES",
        package_bytes=len(package.payload),
        table_counts=counts,
    )


def restore_empty_smoke_state(
    *,
    env: Optional[Mapping[str, str]] = None,
    store_factory: Callable[[R2RuntimeConfig], Any] = build_r2_store,
    ensure_schema: Callable[[Path], Path] = ensure_database_schema,
) -> SmokePhaseReceipt:
    """Restore only a single generation-1 empty checkpoint into a fresh database."""
    env_map = os.environ if env is None else env
    _require_smoke_enabled(env_map)
    config = load_r2_runtime_config(env_map)
    db_path = ensure_schema(resolve_database_path(env_map))
    _require_empty_research_state(db_path)
    store = store_factory(config)

    chain = discover_manifest_chain(store)
    if len(chain) != 1 or chain[0].generation != 1:
        raise ResearchStateSmokeBoundaryError(
            "empty-checkpoint restore requires exactly one committed generation"
        )
    selected = chain[0]
    document = selected.document
    current_source_sha = _required_source_code_sha(env_map)
    manifest_source_sha = str(document.get("source_code_sha") or "").strip().lower()
    if manifest_source_sha != current_source_sha:
        raise ResearchStateSmokeBoundaryError(
            "empty-checkpoint restore requires the same source code SHA as publish"
        )
    zero_counts = {table: 0 for table in _RESEARCH_TABLES}
    if dict(document.get("table_counts") or {}) != zero_counts:
        raise ResearchStateSmokeBoundaryError(
            "empty-checkpoint restore refuses non-empty research-state content"
        )
    if document.get("rights_classification") != "NO_CONDITIONAL_VALUES":
        raise ResearchStateSmokeBoundaryError(
            "empty-checkpoint restore requires NO_CONDITIONAL_VALUES rights classification"
        )
    package_bytes = document.get("package_bytes")
    if not isinstance(package_bytes, int) or package_bytes > SMOKE_MAX_PACKAGE_BYTES:
        raise ResearchStateSmokeBoundaryError(
            "empty-checkpoint restore package exceeds the bounded smoke byte cap"
        )
    package_key = str(document.get("package_key") or "")
    package_sha = str(document.get("package_sha256") or "")
    if store.list_keys(PACKAGE_PREFIX) != [package_key]:
        raise ResearchStateSmokeBoundaryError(
            "empty-checkpoint restore requires exactly one referenced package object"
        )

    receipt = restore_checkpoint(store, db_path, generation=1)
    counts = _require_empty_research_state(db_path)
    return SmokePhaseReceipt(
        phase="smoke-restore-empty",
        disposition="RESTORED_EMPTY_CHECKPOINT",
        generation=receipt.generation,
        manifest_key=selected.key,
        manifest_sha256=selected.sha256,
        package_key=package_key,
        package_sha256=package_sha,
        source_code_sha=current_source_sha,
        rights_classification="NO_CONDITIONAL_VALUES",
        package_bytes=int(package_bytes),
        table_counts=counts,
        inserted=dict(receipt.inserted),
        existing=dict(receipt.existing),
    )


def research_state_counts(db_path: Path) -> dict[str, int]:
    validate_current_schema(db_path)
    connection = sqlite3.connect(str(db_path))
    try:
        return {
            table: int(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
            for table in _RESEARCH_TABLES
        }
    finally:
        connection.close()


def _require_smoke_enabled(env: Mapping[str, str]) -> None:
    if not durability_enabled(env):
        raise ResearchStateRuntimeConfigError(
            f"{ENABLE_ENV}=true is required for the explicit research-state smoke phase"
        )


def _require_empty_research_state(db_path: Path) -> dict[str, int]:
    counts = research_state_counts(db_path)
    if any(counts.values()):
        raise ResearchStateSmokeBoundaryError(
            "empty-checkpoint smoke refuses non-empty local research-state tables"
        )
    return counts


def _required_env(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name, "") or "").strip()
    if not value:
        raise ResearchStateRuntimeConfigError(
            f"{name} is required when durability is enabled"
        )
    return value


def _required_source_code_sha(env: Mapping[str, str]) -> str:
    value = str(env.get("GITHUB_SHA", "") or "").strip().lower()
    if not _SHA40_RE.fullmatch(value):
        raise ResearchStateRuntimeConfigError(
            "GITHUB_SHA must be an exact 40-hex commit when publishing durable research state"
        )
    return value


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="DSA research-state durability phase")
    parser.add_argument(
        "phase",
        choices=("restore", "publish", "smoke-publish-empty", "smoke-restore-empty"),
    )
    args = parser.parse_args(argv)
    try:
        if args.phase == "restore":
            receipt = restore_runtime_state()
        elif args.phase == "publish":
            receipt = publish_runtime_state()
        elif args.phase == "smoke-publish-empty":
            receipt = publish_empty_smoke_state()
        else:
            receipt = restore_empty_smoke_state()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "phase": args.phase,
                    "status": "ERROR",
                    "error_type": type(exc).__name__,
                    "error_code": getattr(exc, "code", None),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(receipt.as_public_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
