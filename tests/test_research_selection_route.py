# -*- coding: utf-8 -*-
"""Regressions for request-origin vs research-route provenance."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from src.services.pit_identity import (
    build_auto_screen_selection_context,
    build_specified_codes_selection_context,
)
from src.services.task_queue import AnalysisTaskQueue, TaskInfo


def test_specified_codes_keeps_ui_origin_separate_from_research_route() -> None:
    context = build_specified_codes_selection_context(
        raw_selection_source="autocomplete",
        query_source="api",
    )
    assert context["selection_source"] == "SPECIFIED_CODES"
    assert context["request_origin"] == "autocomplete"
    assert context["query_source"] == "api"
    assert context["selection_context_hash"]


def test_specified_codes_watchlist_envelope_is_identity_bound() -> None:
    context = build_specified_codes_selection_context(
        query_source="cli",
        delivery_envelope="ASSET_RESEARCH_BRIEF_WATCHLIST",
    )
    assert context["selection_source"] == "SPECIFIED_CODES"
    assert context["delivery_envelope"] == "ASSET_RESEARCH_BRIEF_WATCHLIST"
    assert context["selection_context_hash"]


def test_auto_screen_context_reuses_existing_provenance_without_universe_guess() -> None:
    context = build_auto_screen_selection_context(
        {
            "selection_source": "auto_screen",
            "strategy": "momentum_quality",
            "strategy_version": "1.1",
            "run_id": "screen-1",
            "snapshot_count": 5206,
            "snapshot_source": "em_datacenter",
            "selected_candidates": [
                {
                    "rank": 1,
                    "group_rank": 1,
                    "product_group": "AUTO_STOCK_FOCUS",
                    "asset_type": "stock",
                    "code": "600519",
                    "score": 77.2,
                }
            ],
        }
    )
    assert context["selection_source"] == "AUTO_SCREEN"
    assert context["delivery_envelope"] == "ASSET_RESEARCH_BRIEF_AUTO"
    assert context["screening"]["run_id"] == "screen-1"
    assert context["screening"]["snapshot_source"] == "em_datacenter"
    candidate = context["screening"]["selected_candidates"][0]
    assert candidate["product_group"] == "AUTO_STOCK_FOCUS"
    assert candidate["asset_type"] == "stock"
    assert "universe_snapshot_id" not in context


def test_task_queue_forwards_request_origin_to_analysis_service() -> None:
    queue = object.__new__(AnalysisTaskQueue)
    queue._data_lock = threading.Lock()
    queue._tasks = {
        "task-1": TaskInfo(
            task_id="task-1",
            stock_code="600519",
            selection_source="manual",
            query_source="api",
        )
    }
    queue._analyzing_stocks = {}
    queue._broadcast_event = MagicMock()
    queue._cleanup_old_tasks = MagicMock(return_value=0)

    service = MagicMock()
    service.analyze_stock.return_value = {"stock_name": "贵州茅台"}
    with patch("src.services.analysis_service.AnalysisService", return_value=service):
        result = queue._execute_task(
            task_id="task-1",
            stock_code="600519",
            report_type="detailed",
            force_refresh=False,
            notify=False,
        )

    assert result is not None
    assert service.analyze_stock.call_args.kwargs["selection_source"] == "manual"
