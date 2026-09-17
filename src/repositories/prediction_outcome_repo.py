# -*- coding: utf-8 -*-
"""Append-only repository for immutable PredictionOutcome research rows."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import desc, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.storage import DatabaseManager, PredictionOutcomeRecord


class PredictionOutcomeRepository:
    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def get_by_hash(self, outcome_hash: str) -> Optional[PredictionOutcomeRecord]:
        with self.db.get_session() as session:
            return session.execute(
                select(PredictionOutcomeRecord)
                .where(PredictionOutcomeRecord.outcome_hash == str(outcome_hash))
                .limit(1)
            ).scalar_one_or_none()

    def get_latest_for_root(self, root_identity_hash: str) -> Optional[PredictionOutcomeRecord]:
        with self.db.get_session() as session:
            return session.execute(
                select(PredictionOutcomeRecord)
                .where(PredictionOutcomeRecord.root_identity_hash == str(root_identity_hash))
                .order_by(desc(PredictionOutcomeRecord.id))
                .limit(1)
            ).scalar_one_or_none()

    def list_for_prediction(self, prediction_hash: str) -> List[PredictionOutcomeRecord]:
        with self.db.get_session() as session:
            return list(
                session.execute(
                    select(PredictionOutcomeRecord)
                    .where(PredictionOutcomeRecord.prediction_hash == str(prediction_hash))
                    .order_by(PredictionOutcomeRecord.id)
                ).scalars().all()
            )

    def persist_terminal(
        self,
        fields: Dict[str, Any],
        *,
        correction_reason: Optional[str] = None,
    ) -> Tuple[Dict[str, Optional[str]], str]:
        outcome_hash = str(fields["outcome_hash"])
        root_identity_hash = str(fields["root_identity_hash"])

        def _write(session):
            exact = session.execute(
                select(PredictionOutcomeRecord)
                .where(PredictionOutcomeRecord.outcome_hash == outcome_hash)
                .limit(1)
            ).scalar_one_or_none()
            if exact is not None:
                return {
                    "outcome_hash": exact.outcome_hash,
                    "supersedes_outcome_hash": exact.supersedes_outcome_hash,
                }, "existing"

            latest = session.execute(
                select(PredictionOutcomeRecord)
                .where(PredictionOutcomeRecord.root_identity_hash == root_identity_hash)
                .order_by(desc(PredictionOutcomeRecord.id))
                .limit(1)
            ).scalar_one_or_none()
            row_fields = dict(fields)
            if latest is not None:
                reason = str(correction_reason or "").strip()
                if not reason:
                    raise ValueError(
                        "correction_reason is required when superseding a terminal outcome"
                    )
                row_fields["supersedes_outcome_hash"] = latest.outcome_hash
                row_fields["correction_reason"] = reason

            statement = sqlite_insert(PredictionOutcomeRecord).values(**row_fields)
            statement = statement.on_conflict_do_nothing(index_elements=["outcome_hash"])
            session.execute(statement)
            row = session.execute(
                select(PredictionOutcomeRecord)
                .where(PredictionOutcomeRecord.outcome_hash == outcome_hash)
                .limit(1)
            ).scalar_one()
            return {
                "outcome_hash": row.outcome_hash,
                "supersedes_outcome_hash": row.supersedes_outcome_hash,
            }, "superseded" if latest is not None else "created"

        return self.db._run_write_transaction("persist prediction outcome", _write)
