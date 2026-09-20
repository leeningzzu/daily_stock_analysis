# -*- coding: utf-8 -*-
"""Append-only repository for immutable PIT dataset manifests."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.storage import DatabaseManager, PITDatasetManifestRecord


class PITDatasetRepository:
    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def get_by_hash(self, dataset_hash: str) -> Optional[PITDatasetManifestRecord]:
        with self.db.get_session() as session:
            return session.execute(
                select(PITDatasetManifestRecord)
                .where(PITDatasetManifestRecord.dataset_hash == str(dataset_hash))
                .limit(1)
            ).scalar_one_or_none()

    def persist_manifest(self, fields: Dict[str, Any]) -> Tuple[str, str]:
        dataset_hash = str(fields["dataset_hash"])

        def _write(session):
            exact = session.execute(
                select(PITDatasetManifestRecord)
                .where(PITDatasetManifestRecord.dataset_hash == dataset_hash)
                .limit(1)
            ).scalar_one_or_none()
            if exact is not None:
                return exact.dataset_hash, "existing"

            statement = sqlite_insert(PITDatasetManifestRecord).values(**fields)
            statement = statement.on_conflict_do_nothing(index_elements=["dataset_hash"])
            session.execute(statement)
            row = session.execute(
                select(PITDatasetManifestRecord)
                .where(PITDatasetManifestRecord.dataset_hash == dataset_hash)
                .limit(1)
            ).scalar_one()
            return row.dataset_hash, "created"

        return self.db._run_write_transaction("persist PIT dataset manifest", _write)
