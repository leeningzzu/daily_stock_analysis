# -*- coding: utf-8 -*-
"""Repository for append-only Prediction Ledger snapshots."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.storage import AnalysisHistory, DatabaseManager, PredictionLedgerRecord


class PredictionLedgerRepository:
    """Insert immutable snapshots without refreshing an existing prediction hash."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def insert_if_history_exists(self, fields: Dict[str, Any]) -> Tuple[Optional[int], bool]:
        history_id = int(fields["analysis_history_id"])
        prediction_hash = str(fields["prediction_hash"])

        def _insert(session):
            history_exists = session.execute(
                select(AnalysisHistory.id)
                .where(AnalysisHistory.id == history_id)
                .limit(1)
            ).scalar_one_or_none()
            if history_exists is None:
                return None, False

            statement = sqlite_insert(PredictionLedgerRecord).values(**fields)
            statement = statement.on_conflict_do_nothing(index_elements=["prediction_hash"])
            result = session.execute(statement)
            row_id = session.execute(
                select(PredictionLedgerRecord.id)
                .where(PredictionLedgerRecord.prediction_hash == prediction_hash)
                .limit(1)
            ).scalar_one()
            return int(row_id), bool(result.rowcount)

        return self.db._run_write_transaction(
            "insert prediction ledger snapshot",
            _insert,
        )

    def list_for_history(self, analysis_history_id: int) -> List[PredictionLedgerRecord]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(PredictionLedgerRecord)
                .where(PredictionLedgerRecord.analysis_history_id == int(analysis_history_id))
                .order_by(PredictionLedgerRecord.id)
            ).scalars().all()
            return list(rows)

    def get_by_prediction_hash(self, prediction_hash: str) -> Optional[PredictionLedgerRecord]:
        with self.db.get_session() as session:
            return session.execute(
                select(PredictionLedgerRecord)
                .where(PredictionLedgerRecord.prediction_hash == str(prediction_hash))
                .limit(1)
            ).scalar_one_or_none()

    def list_all(self) -> List[PredictionLedgerRecord]:
        with self.db.get_session() as session:
            return list(
                session.execute(
                    select(PredictionLedgerRecord).order_by(
                        PredictionLedgerRecord.decision_time,
                        PredictionLedgerRecord.id,
                    )
                ).scalars().all()
            )

    def list_dataset_candidates(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        feature_schema_version: str,
        feature_schema_hash: str,
    ) -> List[PredictionLedgerRecord]:
        """Return the frozen white-box opportunity denominator in session order."""
        with self.db.get_session() as session:
            return list(
                session.execute(
                    select(PredictionLedgerRecord)
                    .where(
                        PredictionLedgerRecord.market == "cn",
                        PredictionLedgerRecord.instrument_type == "stock",
                        PredictionLedgerRecord.strategy_id == str(strategy_id),
                        PredictionLedgerRecord.strategy_version == str(strategy_version),
                        PredictionLedgerRecord.feature_schema_version == str(feature_schema_version),
                        PredictionLedgerRecord.feature_schema_hash == str(feature_schema_hash),
                        PredictionLedgerRecord.canonical_action == "WAIT",
                    )
                    .order_by(
                        PredictionLedgerRecord.data_as_of,
                        PredictionLedgerRecord.decision_time,
                        PredictionLedgerRecord.id,
                    )
                ).scalars().all()
            )
