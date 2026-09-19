# -*- coding: utf-8 -*-
"""Selective immutable cross-run package chain for DSA research state.

V1 is intentionally CHECKPOINT_ONLY: every committed generation contains the
full allowlisted durable projection of Prediction Ledger, PredictionOutcome,
and PIT dataset manifest rows. The package is a transport artifact, never a
second writable runtime database.

No network or R2 SDK is required here. Object transport is represented by a
small protocol and a filesystem implementation used by deterministic tests.
A live R2 adapter remains a later effect/admission step.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple


PACKAGE_SCHEMA_VERSION = "research-state-package-v1"
MANIFEST_SCHEMA_VERSION = "research-state-manifest-v1"
PACKAGE_KIND = "CHECKPOINT_ONLY_V1"
DURABILITY_STATE = "R2_PACKAGE_CHAIN_V1"

PACKAGE_PREFIX = "research-state/v1/packages/sha256"
MANIFEST_PREFIX = "research-state/v1/manifests"

# Bounded-admission safety caps, not pricing promises.
# One standard S3 ListObjectsV2 page can enumerate at most 1000 manifest keys.
MAX_CHAIN_GENERATIONS = 1000
MAX_PACKAGE_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_OBJECT_CREATES_PER_GENERATION = 2
MAX_MANIFEST_READS_PER_DISCOVERY = MAX_CHAIN_GENERATIONS

REQUIRED_WRITER_CONCURRENCY_GROUP = "stock-analysis"
PUBLICATION_BARRIER = "PACKAGE_FIRST_MANIFEST_LAST"

RIGHTS_CLASSIFICATION_VALUES = {
    "NO_CONDITIONAL_VALUES",
    "SYNTHETIC_TEST_ONLY",
    "EXPLICITLY_ADMITTED",
}

SAFE_LOW_SENSITIVITY = "SAFE_LOW_SENSITIVITY"
RIGHTS_CONDITIONAL = "RIGHTS_CONDITIONAL"
FORBIDDEN = "FORBIDDEN"

_TABLE_ORDER = (
    "prediction_ledger",
    "prediction_outcomes",
    "pit_dataset_manifests",
)

_IDENTITY_COLUMN = {
    "prediction_ledger": "prediction_hash",
    "prediction_outcomes": "outcome_hash",
    "pit_dataset_manifests": "dataset_hash",
}

_JSON_COLUMNS = {
    "prediction_ledger": {
        "evidence_json",
        "asset_identity_json",
        "selection_context_json",
        "pit_ineligibility_json",
    },
    "prediction_outcomes": {
        "cost_identity_json",
        "execution_identity_json",
    },
    "pit_dataset_manifests": {
        "training_admission_reasons_json",
        "manifest_json",
    },
}

# Every current schema column is classified. FORBIDDEN means "never leaves the
# runtime DB in package V1", not that the column is invalid in the runtime DB.
COLUMN_CLASSIFICATION: Dict[str, Dict[str, str]] = {
    "prediction_ledger": {
        "id": FORBIDDEN,
        "prediction_hash": SAFE_LOW_SENSITIVITY,
        "schema_version": SAFE_LOW_SENSITIVITY,
        "analysis_history_id": FORBIDDEN,
        "decision_signal_id": FORBIDDEN,
        "trace_id": FORBIDDEN,
        "market": SAFE_LOW_SENSITIVITY,
        "stock_code": SAFE_LOW_SENSITIVITY,
        "instrument_type": SAFE_LOW_SENSITIVITY,
        "decision_time": SAFE_LOW_SENSITIVITY,
        "decision_timezone": SAFE_LOW_SENSITIVITY,
        "data_as_of": SAFE_LOW_SENSITIVITY,
        "available_at_max": SAFE_LOW_SENSITIVITY,
        "strategy_id": SAFE_LOW_SENSITIVITY,
        "strategy_version": SAFE_LOW_SENSITIVITY,
        "factor_contract_version": SAFE_LOW_SENSITIVITY,
        "canonical_action": SAFE_LOW_SENSITIVITY,
        "horizon": SAFE_LOW_SENSITIVITY,
        "decision_profile": SAFE_LOW_SENSITIVITY,
        "trigger_source": SAFE_LOW_SENSITIVITY,
        "source_type": FORBIDDEN,
        "entry_low": FORBIDDEN,
        "entry_high": FORBIDDEN,
        "stop_loss": FORBIDDEN,
        "target_price": FORBIDDEN,
        "feature_schema_version": SAFE_LOW_SENSITIVITY,
        "feature_schema_hash": SAFE_LOW_SENSITIVITY,
        "evidence_hash": SAFE_LOW_SENSITIVITY,
        "evidence_json": RIGHTS_CONDITIONAL,
        "code_sha": SAFE_LOW_SENSITIVITY,
        "provider_identity": SAFE_LOW_SENSITIVITY,
        "adjustment_basis": SAFE_LOW_SENSITIVITY,
        "universe_snapshot_id": SAFE_LOW_SENSITIVITY,
        "asset_identity_hash": SAFE_LOW_SENSITIVITY,
        "asset_identity_json": SAFE_LOW_SENSITIVITY,
        "data_snapshot_identity": SAFE_LOW_SENSITIVITY,
        "selection_source": SAFE_LOW_SENSITIVITY,
        "selection_context_hash": SAFE_LOW_SENSITIVITY,
        "selection_context_json": RIGHTS_CONDITIONAL,
        "pit_eligible": SAFE_LOW_SENSITIVITY,
        "pit_ineligibility_json": SAFE_LOW_SENSITIVITY,
        "durability_state": FORBIDDEN,
        "created_at": SAFE_LOW_SENSITIVITY,
    },
    "prediction_outcomes": {
        "id": FORBIDDEN,
        "outcome_hash": SAFE_LOW_SENSITIVITY,
        "root_identity_hash": SAFE_LOW_SENSITIVITY,
        "prediction_hash": SAFE_LOW_SENSITIVITY,
        "label_identity": SAFE_LOW_SENSITIVITY,
        "horizon_identity": SAFE_LOW_SENSITIVITY,
        "cost_identity_hash": SAFE_LOW_SENSITIVITY,
        "cost_identity_json": SAFE_LOW_SENSITIVITY,
        "execution_identity_hash": SAFE_LOW_SENSITIVITY,
        "execution_identity_json": SAFE_LOW_SENSITIVITY,
        "evaluation_engine_version": SAFE_LOW_SENSITIVITY,
        "supersedes_outcome_hash": SAFE_LOW_SENSITIVITY,
        "correction_reason": SAFE_LOW_SENSITIVITY,
        "decision_session": SAFE_LOW_SENSITIVITY,
        "entry_session": SAFE_LOW_SENSITIVITY,
        "exit_session": SAFE_LOW_SENSITIVITY,
        "execution_state": SAFE_LOW_SENSITIVITY,
        "entry_price": RIGHTS_CONDITIONAL,
        "exit_price": RIGHTS_CONDITIONAL,
        "gross_return_pct": RIGHTS_CONDITIONAL,
        "net_return_pct": RIGHTS_CONDITIONAL,
        "max_adverse_excursion_pct": RIGHTS_CONDITIONAL,
        "max_favorable_excursion_pct": RIGHTS_CONDITIONAL,
        "label_value": SAFE_LOW_SENSITIVITY,
        "label_status": SAFE_LOW_SENSITIVITY,
        "label_reason": SAFE_LOW_SENSITIVITY,
        "data_snapshot_identity": SAFE_LOW_SENSITIVITY,
        "provider_identity": SAFE_LOW_SENSITIVITY,
        "adjustment_basis": SAFE_LOW_SENSITIVITY,
        "available_at": SAFE_LOW_SENSITIVITY,
        "created_at": SAFE_LOW_SENSITIVITY,
    },
    "pit_dataset_manifests": {
        "id": FORBIDDEN,
        "dataset_hash": SAFE_LOW_SENSITIVITY,
        "schema_version": SAFE_LOW_SENSITIVITY,
        "dataset_purpose": SAFE_LOW_SENSITIVITY,
        "strategy_id": SAFE_LOW_SENSITIVITY,
        "strategy_version": SAFE_LOW_SENSITIVITY,
        "feature_schema_version": SAFE_LOW_SENSITIVITY,
        "feature_schema_hash": SAFE_LOW_SENSITIVITY,
        "label_identity": SAFE_LOW_SENSITIVITY,
        "horizon_identity": SAFE_LOW_SENSITIVITY,
        "cost_identity_hash": SAFE_LOW_SENSITIVITY,
        "evaluation_engine_version": SAFE_LOW_SENSITIVITY,
        "split_policy": SAFE_LOW_SENSITIVITY,
        "purge_policy": SAFE_LOW_SENSITIVITY,
        "embargo_policy": SAFE_LOW_SENSITIVITY,
        "selection_route_policy": SAFE_LOW_SENSITIVITY,
        "code_sha": SAFE_LOW_SENSITIVITY,
        "final_test_state": SAFE_LOW_SENSITIVITY,
        "training_admission": SAFE_LOW_SENSITIVITY,
        "training_admission_reasons_json": SAFE_LOW_SENSITIVITY,
        "manifest_json": SAFE_LOW_SENSITIVITY,
        "frozen_at": SAFE_LOW_SENSITIVITY,
        "created_at": SAFE_LOW_SENSITIVITY,
    },
}

_MANIFEST_KEY_RE = re.compile(r"^research-state/v1/manifests/(\d{20})\.json$")
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA64_RE = re.compile(r"^[0-9a-f]{64}$")


class ResearchStateError(RuntimeError):
    """Base fail-closed error for package-chain operations."""

    code = "RESEARCH_STATE_ERROR"


class SchemaClassificationError(ResearchStateError):
    code = "RESEARCH_STATE_SCHEMA_MISMATCH"


class RightsAdmissionRequired(ResearchStateError):
    code = "RESEARCH_STATE_RIGHTS_NOT_ADMITTED"


class PackageTooLarge(ResearchStateError):
    code = "RESEARCH_STATE_PACKAGE_CAP_EXCEEDED"


class ManifestChainError(ResearchStateError):
    code = "RESEARCH_STATE_MANIFEST_CHAIN_INVALID"


class ImportConflictError(ResearchStateError):
    code = "RESEARCH_STATE_IDENTITY_CONTENT_CONFLICT"


class ObjectConflictError(ResearchStateError):
    code = "RESEARCH_STATE_IMMUTABLE_OBJECT_CONFLICT"


class SimulatedCrashAfterPackage(ResearchStateError):
    code = "RESEARCH_STATE_SIMULATED_CRASH_AFTER_PACKAGE"


class ObjectStore(Protocol):
    def list_keys(self, prefix: str) -> List[str]:
        ...

    def get_bytes(self, key: str) -> bytes:
        ...

    def put_if_absent(self, key: str, payload: bytes) -> bool:
        ...


@dataclass(frozen=True)
class PackageArtifact:
    payload: bytes
    sha256: str
    key: str
    table_counts: Mapping[str, int]
    table_roots: Mapping[str, str]


@dataclass(frozen=True)
class ManifestRecord:
    generation: int
    key: str
    payload: bytes
    sha256: str
    document: Mapping[str, Any]


@dataclass(frozen=True)
class PublishReceipt:
    generation: int
    manifest_key: str
    manifest_sha256: str
    package_key: str
    package_sha256: str
    disposition: str


@dataclass(frozen=True)
class RestoreReceipt:
    generation: int
    inserted: Mapping[str, int]
    existing: Mapping[str, int]


class FilesystemObjectStore:
    """Filesystem-backed immutable object-store test double."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    @staticmethod
    def _validate_key(key: str) -> str:
        raw = str(key or "").replace("\\", "/")
        if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
            raise ValueError("absolute object key is forbidden")
        text = raw.strip("/")
        if not text or ".." in text.split("/"):
            raise ValueError("invalid object key")
        return text

    def _path(self, key: str) -> Path:
        return self.root.joinpath(*self._validate_key(key).split("/"))

    def list_keys(self, prefix: str) -> List[str]:
        prefix_text = self._validate_key(prefix)
        base = self.root.joinpath(*prefix_text.split("/"))
        if not base.exists():
            return []
        keys: List[str] = []
        for item in base.rglob("*"):
            if item.is_file():
                keys.append(item.relative_to(self.root).as_posix())
        return sorted(keys)

    def get_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        return path.read_bytes()

    def put_if_absent(self, key: str, payload: bytes) -> bool:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(payload)
            return True
        except FileExistsError:
            return False


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def exported_columns(table: str) -> Tuple[str, ...]:
    classes = COLUMN_CLASSIFICATION[table]
    return tuple(
        column
        for column, disposition in classes.items()
        if disposition != FORBIDDEN
    )


def forbidden_columns(table: str) -> Tuple[str, ...]:
    return tuple(
        column
        for column, disposition in COLUMN_CLASSIFICATION[table].items()
        if disposition == FORBIDDEN
    )


def rights_conditional_columns(table: str) -> Tuple[str, ...]:
    return tuple(
        column
        for column, disposition in COLUMN_CLASSIFICATION[table].items()
        if disposition == RIGHTS_CONDITIONAL
    )


def validate_current_schema(db_path: Path | str) -> None:
    with closing(_connect_ro(db_path)) as conn:
        for table in _TABLE_ORDER:
            actual = tuple(row[1] for row in conn.execute(f"PRAGMA table_info({table})"))
            if not actual:
                raise SchemaClassificationError(f"required table missing: {table}")
            classified = tuple(COLUMN_CLASSIFICATION[table].keys())
            if set(actual) != set(classified):
                missing = sorted(set(actual) - set(classified))
                stale = sorted(set(classified) - set(actual))
                raise SchemaClassificationError(
                    f"schema classification mismatch for {table}: "
                    f"unclassified={missing}; absent={stale}"
                )
            if _IDENTITY_COLUMN[table] not in exported_columns(table):
                raise SchemaClassificationError(
                    f"identity column cannot be forbidden: {table}.{_IDENTITY_COLUMN[table]}"
                )


def build_checkpoint_package(
    db_path: Path | str,
    *,
    rights_admitted: bool = False,
    max_package_bytes: int = MAX_PACKAGE_BYTES,
) -> PackageArtifact:
    validate_current_schema(db_path)
    tables: Dict[str, Any] = {}

    with closing(_connect_ro(db_path)) as conn:
        for table in _TABLE_ORDER:
            columns = exported_columns(table)
            identity = _IDENTITY_COLUMN[table]
            sql = (
                "SELECT "
                + ",".join(_quote_ident(column) for column in columns)
                + f" FROM {_quote_ident(table)} ORDER BY {_quote_ident(identity)}"
            )
            rows: List[Dict[str, Any]] = []
            for raw in conn.execute(sql):
                item = {
                    column: _canonicalize_db_value(table, column, value)
                    for column, value in zip(columns, raw)
                }
                if not rights_admitted:
                    blocked = [
                        column
                        for column in rights_conditional_columns(table)
                        if item.get(column) is not None
                    ]
                    if blocked:
                        raise RightsAdmissionRequired(
                            f"rights admission required for {table}: {','.join(blocked)}"
                        )
                rows.append(item)

            row_hashes = [sha256_bytes(canonical_json_bytes(row)) for row in rows]
            table_root = sha256_bytes(canonical_json_bytes(row_hashes))
            tables[table] = {
                "identity_column": identity,
                "columns": list(columns),
                "row_count": len(rows),
                "table_root_sha256": table_root,
                "rows": rows,
            }

    document = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "package_kind": PACKAGE_KIND,
        "durability_state_on_restore": DURABILITY_STATE,
        "tables": tables,
    }
    payload = canonical_json_bytes(document)
    if len(payload) > int(max_package_bytes):
        raise PackageTooLarge(
            f"package bytes {len(payload)} exceed cap {int(max_package_bytes)}"
        )
    digest = sha256_bytes(payload)
    return PackageArtifact(
        payload=payload,
        sha256=digest,
        key=f"{PACKAGE_PREFIX}/{digest}.json",
        table_counts={table: int(tables[table]["row_count"]) for table in _TABLE_ORDER},
        table_roots={table: str(tables[table]["table_root_sha256"]) for table in _TABLE_ORDER},
    )


def manifest_key(generation: int) -> str:
    generation = int(generation)
    if generation < 1 or generation > MAX_CHAIN_GENERATIONS:
        raise ManifestChainError(f"generation out of range: {generation}")
    return f"{MANIFEST_PREFIX}/{generation:020d}.json"


def build_manifest(
    *,
    generation: int,
    parent: Optional[ManifestRecord],
    package: PackageArtifact,
    source_code_sha: str,
    created_at: datetime,
    rights_classification: str,
) -> bytes:
    generation = int(generation)
    source_sha = str(source_code_sha or "").strip().lower()
    if not _SHA40_RE.fullmatch(source_sha):
        raise ValueError("source_code_sha must be exact 40-hex Git SHA")
    if not isinstance(created_at, datetime) or created_at.tzinfo is None:
        raise ValueError("created_at must be timezone-aware")
    rights_text = str(rights_classification or "").strip()
    if rights_text not in RIGHTS_CLASSIFICATION_VALUES:
        raise RightsAdmissionRequired(
            "manifest rights_classification must be an admitted explicit value"
        )

    if generation == 1:
        if parent is not None:
            raise ManifestChainError("generation 1 cannot have a parent")
        parent_generation = None
        parent_sha = None
    else:
        if parent is None or parent.generation != generation - 1:
            raise ManifestChainError("generation must extend the exact current tip")
        parent_generation = parent.generation
        parent_sha = parent.sha256

    document = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generation": generation,
        "parent_generation": parent_generation,
        "parent_manifest_sha256": parent_sha,
        "package_kind": PACKAGE_KIND,
        "package_schema_version": PACKAGE_SCHEMA_VERSION,
        "package_key": package.key,
        "package_sha256": package.sha256,
        "package_bytes": len(package.payload),
        "source_code_sha": source_sha,
        "rights_classification": rights_text,
        "table_counts": dict(package.table_counts),
        "table_roots": dict(package.table_roots),
        "created_at": created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    payload = canonical_json_bytes(document)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ManifestChainError("manifest exceeds hard byte cap")
    return payload


def discover_manifest_chain(
    store: ObjectStore,
    *,
    max_generation: Optional[int] = None,
) -> List[ManifestRecord]:
    keys = store.list_keys(MANIFEST_PREFIX)
    if len(keys) > MAX_CHAIN_GENERATIONS:
        raise ManifestChainError("manifest count exceeds hard chain cap")

    records: List[ManifestRecord] = []
    for key in sorted(keys):
        match = _MANIFEST_KEY_RE.fullmatch(key)
        if not match:
            raise ManifestChainError(f"unexpected manifest key: {key}")
        generation = int(match.group(1))
        if max_generation is not None and generation > int(max_generation):
            continue
        raw = store.get_bytes(key)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ManifestChainError(f"manifest exceeds cap: {key}")
        document = _parse_canonical_json(raw, label=f"manifest {key}")
        _validate_manifest_document(document, generation=generation, key=key)
        records.append(
            ManifestRecord(
                generation=generation,
                key=key,
                payload=raw,
                sha256=sha256_bytes(raw),
                document=document,
            )
        )

    for index, record in enumerate(records):
        expected_generation = index + 1
        if record.generation != expected_generation:
            raise ManifestChainError("manifest chain is not contiguous from generation 1")
        if index == 0:
            if record.document.get("parent_generation") is not None:
                raise ManifestChainError("generation 1 parent_generation must be null")
            if record.document.get("parent_manifest_sha256") is not None:
                raise ManifestChainError("generation 1 parent hash must be null")
        else:
            parent = records[index - 1]
            if record.document.get("parent_generation") != parent.generation:
                raise ManifestChainError("manifest parent generation mismatch")
            if record.document.get("parent_manifest_sha256") != parent.sha256:
                raise ManifestChainError("manifest parent hash mismatch")
    return records


def publish_checkpoint(
    store: ObjectStore,
    db_path: Path | str,
    *,
    source_code_sha: str,
    created_at: datetime,
    rights_admitted: bool = False,
    rights_classification: str = "",
    crash_after_package: bool = False,
    max_package_bytes: int = MAX_PACKAGE_BYTES,
) -> PublishReceipt:
    chain = discover_manifest_chain(store)
    if len(chain) >= MAX_CHAIN_GENERATIONS:
        raise ManifestChainError("chain generation hard cap reached")
    parent = chain[-1] if chain else None
    generation = 1 if parent is None else parent.generation + 1

    rights_text = str(rights_classification or "").strip()
    if rights_admitted:
        if rights_text not in {"SYNTHETIC_TEST_ONLY", "EXPLICITLY_ADMITTED"}:
            raise RightsAdmissionRequired(
                "rights_admitted=True requires SYNTHETIC_TEST_ONLY or EXPLICITLY_ADMITTED"
            )
    elif rights_text != "NO_CONDITIONAL_VALUES":
        raise RightsAdmissionRequired(
            "rights_admitted=False requires NO_CONDITIONAL_VALUES"
        )

    package = build_checkpoint_package(
        db_path,
        rights_admitted=rights_admitted,
        max_package_bytes=max_package_bytes,
    )
    _put_exact(store, package.key, package.payload)
    if crash_after_package:
        raise SimulatedCrashAfterPackage("simulated crash after package before manifest")

    manifest_payload = build_manifest(
        generation=generation,
        parent=parent,
        package=package,
        source_code_sha=source_code_sha,
        created_at=created_at,
        rights_classification=rights_text,
    )
    key = manifest_key(generation)
    created = _put_exact(store, key, manifest_payload)
    manifest_sha = sha256_bytes(manifest_payload)

    # Re-read and validate the just-committed chain. Manifest creation is the
    # durability commit point.
    verified = discover_manifest_chain(store)
    tip = verified[-1]
    if tip.generation != generation or tip.sha256 != manifest_sha:
        raise ManifestChainError("published manifest is not the verified chain tip")

    return PublishReceipt(
        generation=generation,
        manifest_key=key,
        manifest_sha256=manifest_sha,
        package_key=package.key,
        package_sha256=package.sha256,
        disposition="created" if created else "existing",
    )


def restore_checkpoint(
    store: ObjectStore,
    db_path: Path | str,
    *,
    generation: Optional[int] = None,
) -> RestoreReceipt:
    chain = discover_manifest_chain(store, max_generation=generation)
    if not chain:
        raise ManifestChainError("no committed manifest generation available")
    selected = chain[-1]
    if generation is not None and selected.generation != int(generation):
        raise ManifestChainError(f"requested generation not found: {generation}")

    document = selected.document
    package_key_value = str(document.get("package_key") or "")
    package_sha = str(document.get("package_sha256") or "")
    package = store.get_bytes(package_key_value)
    if len(package) != int(document.get("package_bytes") or -1):
        raise ManifestChainError("package byte-size mismatch")
    if sha256_bytes(package) != package_sha:
        raise ManifestChainError("package SHA-256 mismatch")

    package_doc = _validate_package_document(package)
    if package_doc.get("package_kind") != document.get("package_kind"):
        raise ManifestChainError("manifest/package kind mismatch")
    for table in _TABLE_ORDER:
        table_doc = package_doc["tables"][table]
        if int(document["table_counts"][table]) != int(table_doc["row_count"]):
            raise ManifestChainError(f"manifest/package row-count mismatch: {table}")
        if str(document["table_roots"][table]) != str(table_doc["table_root_sha256"]):
            raise ManifestChainError(f"manifest/package table-root mismatch: {table}")

    validate_current_schema(db_path)
    inserted = {table: 0 for table in _TABLE_ORDER}
    existing = {table: 0 for table in _TABLE_ORDER}

    conn = sqlite3.connect(str(Path(db_path)))
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in _TABLE_ORDER:
            table_doc = package_doc["tables"][table]
            identity = _IDENTITY_COLUMN[table]
            columns = tuple(table_doc["columns"])
            expected_columns = exported_columns(table)
            if columns != expected_columns:
                raise ManifestChainError(f"package column contract mismatch: {table}")
            for row in table_doc["rows"]:
                key_value = row[identity]
                current = _read_projection_by_identity(
                    conn,
                    table=table,
                    identity_column=identity,
                    identity_value=key_value,
                    columns=columns,
                )
                if current is not None:
                    if _canonical_projection(table, current) != _canonical_projection(table, row):
                        raise ImportConflictError(
                            f"same identity with different durable content: {table}:{key_value}"
                        )
                    existing[table] += 1
                    continue

                fields = dict(row)
                if table == "prediction_ledger":
                    # Imported rows intentionally drop run-local weak references
                    # and receive durable status only after a committed manifest.
                    fields["analysis_history_id"] = 0
                    fields["decision_signal_id"] = None
                    fields["durability_state"] = DURABILITY_STATE
                _insert_row(conn, table, fields)
                inserted[table] += 1

        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise ResearchStateError("SQLite integrity_check failed after import")
        foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign:
            raise ResearchStateError("SQLite foreign_key_check failed after import")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return RestoreReceipt(
        generation=selected.generation,
        inserted=inserted,
        existing=existing,
    )


def _validate_package_document(payload: bytes) -> Mapping[str, Any]:
    if len(payload) > MAX_PACKAGE_BYTES:
        raise PackageTooLarge("package exceeds hard byte cap")
    document = _parse_canonical_json(payload, label="package")
    if document.get("schema_version") != PACKAGE_SCHEMA_VERSION:
        raise ManifestChainError("package schema mismatch")
    if document.get("package_kind") != PACKAGE_KIND:
        raise ManifestChainError("package kind mismatch")
    if document.get("durability_state_on_restore") != DURABILITY_STATE:
        raise ManifestChainError("package durability-state mismatch")
    tables = document.get("tables")
    if not isinstance(tables, dict) or set(tables) != set(_TABLE_ORDER):
        raise ManifestChainError("package table set mismatch")

    for table in _TABLE_ORDER:
        table_doc = tables[table]
        if not isinstance(table_doc, dict):
            raise ManifestChainError(f"invalid package table document: {table}")
        columns = tuple(table_doc.get("columns") or [])
        if columns != exported_columns(table):
            raise ManifestChainError(f"package column contract mismatch: {table}")
        if table_doc.get("identity_column") != _IDENTITY_COLUMN[table]:
            raise ManifestChainError(f"package identity column mismatch: {table}")
        rows = table_doc.get("rows")
        if not isinstance(rows, list):
            raise ManifestChainError(f"package rows must be a list: {table}")
        identities = [row.get(_IDENTITY_COLUMN[table]) for row in rows]
        if identities != sorted(identities):
            raise ManifestChainError(f"package rows are not identity-sorted: {table}")
        if len(identities) != len(set(identities)):
            raise ManifestChainError(f"duplicate identity inside package: {table}")
        row_hashes = [sha256_bytes(canonical_json_bytes(row)) for row in rows]
        expected_root = sha256_bytes(canonical_json_bytes(row_hashes))
        if expected_root != table_doc.get("table_root_sha256"):
            raise ManifestChainError(f"table-root mismatch: {table}")
        if len(rows) != int(table_doc.get("row_count") or 0):
            raise ManifestChainError(f"row-count mismatch: {table}")

    _validate_cross_table_invariants(document)
    return document


def _validate_manifest_document(
    document: Mapping[str, Any],
    *,
    generation: int,
    key: str,
) -> None:
    if document.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ManifestChainError(f"manifest schema mismatch: {key}")
    if int(document.get("generation") or 0) != int(generation):
        raise ManifestChainError(f"manifest generation/key mismatch: {key}")
    if document.get("package_kind") != PACKAGE_KIND:
        raise ManifestChainError(f"manifest package kind mismatch: {key}")
    if document.get("package_schema_version") != PACKAGE_SCHEMA_VERSION:
        raise ManifestChainError(f"manifest package schema mismatch: {key}")

    package_sha = str(document.get("package_sha256") or "")
    if not _SHA64_RE.fullmatch(package_sha):
        raise ManifestChainError(f"manifest package SHA invalid: {key}")
    if document.get("package_key") != f"{PACKAGE_PREFIX}/{package_sha}.json":
        raise ManifestChainError(f"manifest package key/hash mismatch: {key}")

    package_bytes = document.get("package_bytes")
    if not isinstance(package_bytes, int) or package_bytes < 0 or package_bytes > MAX_PACKAGE_BYTES:
        raise ManifestChainError(f"manifest package byte bound invalid: {key}")

    source_sha = str(document.get("source_code_sha") or "")
    if not _SHA40_RE.fullmatch(source_sha):
        raise ManifestChainError(f"manifest source code SHA invalid: {key}")
    if document.get("rights_classification") not in RIGHTS_CLASSIFICATION_VALUES:
        raise ManifestChainError(f"manifest rights classification invalid: {key}")

    counts = document.get("table_counts")
    roots = document.get("table_roots")
    if not isinstance(counts, dict) or set(counts) != set(_TABLE_ORDER):
        raise ManifestChainError(f"manifest table-count contract mismatch: {key}")
    if not isinstance(roots, dict) or set(roots) != set(_TABLE_ORDER):
        raise ManifestChainError(f"manifest table-root contract mismatch: {key}")
    for table in _TABLE_ORDER:
        count = counts[table]
        root = str(roots[table] or "")
        if not isinstance(count, int) or count < 0:
            raise ManifestChainError(f"manifest negative/invalid row count: {table}")
        if not _SHA64_RE.fullmatch(root):
            raise ManifestChainError(f"manifest table-root SHA invalid: {table}")

    created_at = str(document.get("created_at") or "")
    if not created_at.endswith("Z"):
        raise ManifestChainError(f"manifest created_at must be UTC Z: {key}")


def _validate_cross_table_invariants(document: Mapping[str, Any]) -> None:
    tables = document["tables"]
    ledger_rows = tables["prediction_ledger"]["rows"]
    outcome_rows = tables["prediction_outcomes"]["rows"]
    pit_rows = tables["pit_dataset_manifests"]["rows"]

    ledger_ids = {str(row["prediction_hash"]) for row in ledger_rows}
    outcome_by_hash = {str(row["outcome_hash"]): row for row in outcome_rows}

    for row in outcome_rows:
        prediction_hash = str(row.get("prediction_hash") or "")
        if prediction_hash not in ledger_ids:
            raise ManifestChainError(
                f"outcome references missing prediction: {prediction_hash}"
            )
        supersedes = str(row.get("supersedes_outcome_hash") or "")
        if supersedes:
            parent = outcome_by_hash.get(supersedes)
            if parent is None:
                raise ManifestChainError(
                    f"outcome supersedes missing outcome: {supersedes}"
                )
            if parent.get("root_identity_hash") != row.get("root_identity_hash"):
                raise ManifestChainError(
                    "outcome supersedes lineage crosses root identity"
                )

    for row in pit_rows:
        try:
            manifest = json.loads(str(row.get("manifest_json") or "{}"))
        except json.JSONDecodeError as exc:
            raise ManifestChainError("PIT manifest_json is invalid JSON") from exc
        for assignment in manifest.get("assignments") or []:
            if not isinstance(assignment, dict):
                raise ManifestChainError("PIT assignment must be an object")
            prediction_hash = str(assignment.get("prediction_hash") or "")
            if prediction_hash and prediction_hash not in ledger_ids:
                raise ManifestChainError(
                    f"PIT assignment references missing prediction: {prediction_hash}"
                )
            outcome_hash = str(assignment.get("outcome_hash") or "")
            if outcome_hash and outcome_hash not in outcome_by_hash:
                raise ManifestChainError(
                    f"PIT assignment references missing outcome: {outcome_hash}"
                )


def _put_exact(store: ObjectStore, key: str, payload: bytes) -> bool:
    created = store.put_if_absent(key, payload)
    if created:
        return True
    existing = store.get_bytes(key)
    if existing != payload:
        raise ObjectConflictError(f"immutable object conflict: {key}")
    return False


def _parse_canonical_json(payload: bytes, *, label: str) -> Mapping[str, Any]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestChainError(f"{label} is not valid UTF-8 canonical JSON") from exc
    if not isinstance(document, dict):
        raise ManifestChainError(f"{label} root must be an object")
    if canonical_json_bytes(document) != payload:
        raise ManifestChainError(f"{label} is not canonical JSON")
    return document


def _canonicalize_db_value(table: str, column: str, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        raise ResearchStateError(f"non-finite float not packageable: {table}.{column}")
    if column in _JSON_COLUMNS.get(table, set()):
        try:
            parsed = json.loads(str(value))
        except json.JSONDecodeError as exc:
            raise ResearchStateError(f"invalid JSON in {table}.{column}") from exc
        return json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    if isinstance(value, (str, int, float)):
        return value
    if isinstance(value, bytes):
        raise ResearchStateError(f"binary value not allowed in {table}.{column}")
    return str(value)


def _canonical_projection(table: str, row: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        column: _canonicalize_db_value(table, column, row.get(column))
        for column in exported_columns(table)
    }


def _read_projection_by_identity(
    conn: sqlite3.Connection,
    *,
    table: str,
    identity_column: str,
    identity_value: Any,
    columns: Sequence[str],
) -> Optional[Dict[str, Any]]:
    sql = (
        "SELECT "
        + ",".join(_quote_ident(column) for column in columns)
        + f" FROM {_quote_ident(table)} WHERE {_quote_ident(identity_column)}=? LIMIT 1"
    )
    raw = conn.execute(sql, (identity_value,)).fetchone()
    if raw is None:
        return None
    return {column: value for column, value in zip(columns, raw)}


def _insert_row(conn: sqlite3.Connection, table: str, fields: Mapping[str, Any]) -> None:
    columns = tuple(fields)
    placeholders = ",".join("?" for _ in columns)
    sql = (
        f"INSERT INTO {_quote_ident(table)} ("
        + ",".join(_quote_ident(column) for column in columns)
        + f") VALUES ({placeholders})"
    )
    conn.execute(sql, tuple(fields[column] for column in columns))


def _connect_ro(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _quote_ident(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"invalid SQL identifier: {value!r}")
    return '"' + value + '"'
