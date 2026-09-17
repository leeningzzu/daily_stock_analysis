# -*- coding: utf-8 -*-
"""Immutable chronological PIT dataset/split manifests over Ledger + Outcome."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import select

from src.repositories.pit_dataset_repo import PITDatasetRepository
from src.repositories.prediction_ledger_repo import PredictionLedgerRepository
from src.services.pit_identity import canonical_json, sha256_payload
from src.services.prediction_ledger_service import (
    PREDICTION_FEATURE_SCHEMA_HASH,
    PREDICTION_FEATURE_SCHEMA_VERSION,
)
from src.services.prediction_outcome_service import (
    PREDICTION_OUTCOME_ENGINE_VERSION,
    PRIMARY_HORIZON_IDENTITY,
    PRIMARY_LABEL_IDENTITY,
)
from src.storage import DatabaseManager, PredictionLedgerRecord, PredictionOutcomeRecord


PIT_DATASET_SCHEMA_VERSION = "pit-dataset-manifest-v1"
DATASET_PURPOSE_V1 = "ASSET_LEVEL_META_FILTER_ON_SELECTED_OPPORTUNITIES_V1"
SPLIT_POLICY_ID = "XSHG_SESSION_GROUPED_CHRONO_60_20_20_PURGED_V1"
PURGE_POLICY_ID = "EXACT_LABEL_INTERVAL_AND_AVAILABLE_AT"
EMBARGO_POLICY_ID = "FORWARD_ONLY_ZERO_POST_BLOCK_V1"
SELECTION_ROUTE_POLICY_ID = "AUTO_SCREEN_OR_SPECIFIED_CODES_V1"
STRATEGY_ID_V1 = "stock_trend_quality_pullback_v1"
FINAL_TEST_STATE = "SEALED"
TRAINING_ADMISSION_BLOCKED = "BLOCKED"
_ROUTES = {"AUTO_SCREEN", "SPECIFIED_CODES"}


class PITDatasetService:
    """Freeze a no-shuffle split manifest without exposing label values."""

    def __init__(
        self,
        *,
        ledger_repo: Optional[PredictionLedgerRepository] = None,
        dataset_repo: Optional[PITDatasetRepository] = None,
        db_manager: Optional[DatabaseManager] = None,
    ):
        self.db = db_manager or DatabaseManager.get_instance()
        self.ledger_repo = ledger_repo or PredictionLedgerRepository(self.db)
        self.dataset_repo = dataset_repo or PITDatasetRepository(self.db)

    def build_manifest(
        self,
        *,
        cost_identity_hash: str,
        cost_identity_approved: bool = False,
        durable_references_admitted: bool = False,
    ) -> Dict[str, Any]:
        cost_hash = str(cost_identity_hash or "").strip().lower()
        if len(cost_hash) != 64 or any(ch not in "0123456789abcdef" for ch in cost_hash):
            raise ValueError("cost_identity_hash must be a 64-hex SHA-256 identity")

        rows = self.ledger_repo.list_dataset_candidates(
            strategy_id=STRATEGY_ID_V1,
            strategy_version=STRATEGY_ID_V1,
            feature_schema_version=PREDICTION_FEATURE_SCHEMA_VERSION,
            feature_schema_hash=PREDICTION_FEATURE_SCHEMA_HASH,
        )
        denominator = [row for row in rows if self._is_white_box_opportunity(row)]
        eligible: List[PredictionLedgerRecord] = []
        pre_split_exclusions: List[Dict[str, Any]] = []
        for row in denominator:
            reasons = self._pit_gap_reasons(row)
            if reasons:
                pre_split_exclusions.append(
                    self._assignment(row, fold=None, outcome=None, status="EXCLUDED", reason="|".join(reasons))
                )
            else:
                eligible.append(row)

        sessions = sorted({row.data_as_of for row in eligible if row.data_as_of is not None})
        train_end = int(len(sessions) * 0.60)
        validation_end = int(len(sessions) * 0.80)
        train_sessions = sessions[:train_end]
        validation_sessions = sessions[train_end:validation_end]
        final_sessions = sessions[validation_end:]
        fold_by_session = {
            **{session: "TRAIN" for session in train_sessions},
            **{session: "VALIDATION" for session in validation_sessions},
            **{session: "FINAL_TEST" for session in final_sessions},
        }
        validation_start = validation_sessions[0] if validation_sessions else None
        final_start = final_sessions[0] if final_sessions else None
        validation_cutoff = self._fold_cutoff(eligible, validation_start)
        final_cutoff = self._fold_cutoff(eligible, final_start)

        assignments: List[Dict[str, Any]] = list(pre_split_exclusions)
        included_counts = {"TRAIN": 0, "VALIDATION": 0, "FINAL_TEST": 0}
        purge_count = 0
        unlabelable_count = 0
        missing_outcome_count = 0
        outcome_times: List[datetime] = []

        for row in eligible:
            fold = fold_by_session.get(row.data_as_of)
            if fold is None:
                assignments.append(
                    self._assignment(row, fold=None, outcome=None, status="EXCLUDED", reason="SPLIT_NOT_ASSIGNED")
                )
                continue
            cutoff = validation_cutoff if fold == "TRAIN" else final_cutoff if fold == "VALIDATION" else None
            boundary = validation_start if fold == "TRAIN" else final_start if fold == "VALIDATION" else None
            outcome = self._effective_outcome(row.prediction_hash, cost_hash, cutoff=cutoff)
            if outcome is None:
                latest_outcome = (
                    self._effective_outcome(row.prediction_hash, cost_hash, cutoff=None)
                    if cutoff is not None
                    else None
                )
                if latest_outcome is not None:
                    purge_count += 1
                    outcome_times.append(latest_outcome.available_at)
                    assignments.append(
                        self._assignment(
                            row,
                            fold=fold,
                            outcome=latest_outcome,
                            status="PURGED",
                            reason="OUTCOME_NOT_KNOWABLE_AT_CUTOFF",
                        )
                    )
                    continue
                missing_outcome_count += 1
                assignments.append(
                    self._assignment(
                        row,
                        fold=fold,
                        outcome=None,
                        status="EXCLUDED",
                        reason="OUTCOME_NOT_AVAILABLE",
                    )
                )
                continue
            outcome_times.append(outcome.available_at)
            if outcome.label_status == "UNLABELABLE" or outcome.label_value not in (0, 1):
                unlabelable_count += 1
                assignments.append(
                    self._assignment(row, fold=fold, outcome=outcome, status="EXCLUDED", reason="OUTCOME_UNLABELABLE")
                )
                continue
            if boundary is not None and (outcome.exit_session is None or outcome.exit_session >= boundary):
                purge_count += 1
                assignments.append(
                    self._assignment(row, fold=fold, outcome=outcome, status="PURGED", reason="LABEL_INTERVAL_CROSSES_BOUNDARY")
                )
                continue
            assignments.append(
                self._assignment(row, fold=fold, outcome=outcome, status="INCLUDED", reason=None)
            )
            included_counts[fold] += 1

        assignments.sort(
            key=lambda item: (
                item.get("decision_session") or "9999-99-99",
                item.get("prediction_hash") or "",
                item.get("fold") or "",
            )
        )
        raw_counts = {
            "TRAIN": sum(1 for row in eligible if fold_by_session.get(row.data_as_of) == "TRAIN"),
            "VALIDATION": sum(1 for row in eligible if fold_by_session.get(row.data_as_of) == "VALIDATION"),
            "FINAL_TEST": sum(1 for row in eligible if fold_by_session.get(row.data_as_of) == "FINAL_TEST"),
        }
        code_shas = sorted({str(row.code_sha) for row in eligible if row.code_sha})
        code_sha = code_shas[0] if len(code_shas) == 1 else None
        reference_times = [row.created_at for row in denominator if row.created_at is not None] + outcome_times
        frozen_at = max(reference_times) if reference_times else datetime(1970, 1, 1)

        admission_reasons: List[str] = []
        if not denominator:
            admission_reasons.append("NO_WHITE_BOX_OPPORTUNITIES")
        if pre_split_exclusions:
            admission_reasons.append("PIT_GAPS_PRESENT")
        if any(row.durability_state == "LOCAL_DB_ONLY" for row in denominator):
            admission_reasons.append("LOCAL_DB_ONLY_REFERENCES")
        if not durable_references_admitted:
            admission_reasons.append("DURABLE_REFERENCES_NOT_ADMITTED")
        if not cost_identity_approved:
            admission_reasons.append("COST_IDENTITY_NOT_APPROVED")
        if code_sha is None:
            admission_reasons.append("UNIFORM_CODE_SHA_NOT_BOUND")
        if not train_sessions or not validation_sessions or not final_sessions:
            admission_reasons.append("CHRONOLOGICAL_SPLIT_BLOCK_EMPTY")
        if any(included_counts[fold] == 0 for fold in ("TRAIN", "VALIDATION", "FINAL_TEST")):
            admission_reasons.append("POST_PURGE_SPLIT_BLOCK_EMPTY")
        if missing_outcome_count or unlabelable_count:
            admission_reasons.append("OUTCOME_COVERAGE_INCOMPLETE")
        admission_reasons = sorted(set(admission_reasons))

        payload = {
            "schema_version": PIT_DATASET_SCHEMA_VERSION,
            "dataset_purpose": DATASET_PURPOSE_V1,
            "strategy_id": STRATEGY_ID_V1,
            "strategy_version": STRATEGY_ID_V1,
            "feature_schema_version": PREDICTION_FEATURE_SCHEMA_VERSION,
            "feature_schema_hash": PREDICTION_FEATURE_SCHEMA_HASH,
            "label_identity": PRIMARY_LABEL_IDENTITY,
            "horizon_identity": PRIMARY_HORIZON_IDENTITY,
            "cost_identity_hash": cost_hash,
            "evaluation_engine_version": PREDICTION_OUTCOME_ENGINE_VERSION,
            "selection_route_policy": SELECTION_ROUTE_POLICY_ID,
            "split_policy": SPLIT_POLICY_ID,
            "purge_policy": PURGE_POLICY_ID,
            "embargo_policy": EMBARGO_POLICY_ID,
            "session_boundaries": {
                "train_first": self._date_text(train_sessions[0]) if train_sessions else None,
                "train_last": self._date_text(train_sessions[-1]) if train_sessions else None,
                "validation_first": self._date_text(validation_sessions[0]) if validation_sessions else None,
                "validation_last": self._date_text(validation_sessions[-1]) if validation_sessions else None,
                "final_test_first": self._date_text(final_sessions[0]) if final_sessions else None,
                "final_test_last": self._date_text(final_sessions[-1]) if final_sessions else None,
            },
            "counts": {
                "denominator": len(denominator),
                "pit_eligible": len(eligible),
                "pre_split_excluded": len(pre_split_exclusions),
                "raw_by_fold": raw_counts,
                "included_by_fold": included_counts,
                "purged": purge_count,
                "unlabelable": unlabelable_count,
                "missing_outcome": missing_outcome_count,
            },
            "code_sha": code_sha,
            "final_test_state": FINAL_TEST_STATE,
            "training_admission": TRAINING_ADMISSION_BLOCKED,
            "training_admission_reasons": admission_reasons,
            "frozen_at": frozen_at,
            "assignments": assignments,
        }
        dataset_hash = sha256_payload(payload)
        manifest_json = canonical_json(payload)
        stored_hash, disposition = self.dataset_repo.persist_manifest(
            {
                "dataset_hash": dataset_hash,
                "schema_version": PIT_DATASET_SCHEMA_VERSION,
                "dataset_purpose": DATASET_PURPOSE_V1,
                "strategy_id": STRATEGY_ID_V1,
                "strategy_version": STRATEGY_ID_V1,
                "feature_schema_version": PREDICTION_FEATURE_SCHEMA_VERSION,
                "feature_schema_hash": PREDICTION_FEATURE_SCHEMA_HASH,
                "label_identity": PRIMARY_LABEL_IDENTITY,
                "horizon_identity": PRIMARY_HORIZON_IDENTITY,
                "cost_identity_hash": cost_hash,
                "evaluation_engine_version": PREDICTION_OUTCOME_ENGINE_VERSION,
                "split_policy": SPLIT_POLICY_ID,
                "purge_policy": PURGE_POLICY_ID,
                "embargo_policy": EMBARGO_POLICY_ID,
                "selection_route_policy": SELECTION_ROUTE_POLICY_ID,
                "code_sha": code_sha,
                "final_test_state": FINAL_TEST_STATE,
                "training_admission": TRAINING_ADMISSION_BLOCKED,
                "training_admission_reasons_json": canonical_json(admission_reasons),
                "manifest_json": manifest_json,
                "frozen_at": frozen_at,
            }
        )
        return {
            "dataset_hash": stored_hash,
            "disposition": disposition,
            "final_test_state": FINAL_TEST_STATE,
            "training_admission": TRAINING_ADMISSION_BLOCKED,
            "training_admission_reasons": admission_reasons,
            "manifest": json.loads(manifest_json),
        }

    @staticmethod
    def _is_white_box_opportunity(row: PredictionLedgerRecord) -> bool:
        try:
            evidence = json.loads(row.evidence_json or "{}")
        except (TypeError, ValueError):
            return False
        decision = evidence.get("canonical_decision") if isinstance(evidence, dict) else None
        return bool(
            isinstance(decision, dict)
            and str(decision.get("action") or "").upper() == "WAIT"
            and str(decision.get("evidence_state") or "").upper() == "PROVEN"
            and decision.get("hard_veto") is False
        )

    @staticmethod
    def _pit_gap_reasons(row: PredictionLedgerRecord) -> List[str]:
        reasons: List[str] = []
        if row.pit_eligible is not True:
            reasons.append("LEDGER_PIT_INELIGIBLE")
        if row.data_as_of is None or row.decision_time is None or not row.decision_timezone:
            reasons.append("DECISION_IDENTITY_GAP")
        if not row.asset_identity_hash or not row.data_snapshot_identity:
            reasons.append("DATA_IDENTITY_GAP")
        if str(row.selection_source or "").upper() not in _ROUTES:
            reasons.append("SELECTION_ROUTE_GAP")
        if not row.selection_context_hash:
            reasons.append("SELECTION_CONTEXT_GAP")
        if not row.code_sha:
            reasons.append("CODE_SHA_GAP")
        return sorted(set(reasons))

    def _effective_outcome(
        self,
        prediction_hash: str,
        cost_identity_hash: str,
        *,
        cutoff: Optional[datetime],
    ) -> Optional[PredictionOutcomeRecord]:
        with self.db.get_session() as session:
            stmt = select(PredictionOutcomeRecord).where(
                PredictionOutcomeRecord.prediction_hash == str(prediction_hash),
                PredictionOutcomeRecord.label_identity == PRIMARY_LABEL_IDENTITY,
                PredictionOutcomeRecord.horizon_identity == PRIMARY_HORIZON_IDENTITY,
                PredictionOutcomeRecord.cost_identity_hash == str(cost_identity_hash),
                PredictionOutcomeRecord.evaluation_engine_version == PREDICTION_OUTCOME_ENGINE_VERSION,
            )
            if cutoff is not None:
                stmt = stmt.where(PredictionOutcomeRecord.available_at < cutoff)
            stmt = stmt.order_by(
                PredictionOutcomeRecord.available_at.desc(),
                PredictionOutcomeRecord.id.desc(),
            ).limit(1)
            return session.execute(stmt).scalar_one_or_none()

    @staticmethod
    def _fold_cutoff(
        rows: Sequence[PredictionLedgerRecord],
        session_date: Optional[date],
    ) -> Optional[datetime]:
        if session_date is None:
            return None
        times = [row.decision_time for row in rows if row.data_as_of == session_date and row.decision_time is not None]
        return min(times) if times else None

    @staticmethod
    def _assignment(
        row: PredictionLedgerRecord,
        *,
        fold: Optional[str],
        outcome: Optional[PredictionOutcomeRecord],
        status: str,
        reason: Optional[str],
    ) -> Dict[str, Any]:
        item: Dict[str, Any] = {
            "prediction_hash": row.prediction_hash,
            "decision_session": PITDatasetService._date_text(row.data_as_of),
            "selection_source": str(row.selection_source or "").upper() or None,
            "data_snapshot_identity": row.data_snapshot_identity,
            "evidence_hash": row.evidence_hash,
            "fold": fold,
            "status": status,
            "purge_or_exclusion_reason": reason,
            "outcome_hash": outcome.outcome_hash if outcome is not None else None,
            "outcome_data_snapshot_identity": outcome.data_snapshot_identity if outcome is not None else None,
            "outcome_available_at": outcome.available_at if outcome is not None else None,
            "exit_session": PITDatasetService._date_text(outcome.exit_session) if outcome is not None else None,
        }
        return item

    @staticmethod
    def _date_text(value: Optional[date]) -> Optional[str]:
        return value.isoformat() if isinstance(value, date) else None
