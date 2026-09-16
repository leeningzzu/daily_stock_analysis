# -*- coding: utf-8 -*-
from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd

from src.services.pattern_trigger_service import (
    _dedupe_patterns,
    build_pattern_trigger_context,
)


def _history(*, periods: int = 90, source: str | None = "Fetcher") -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-02", periods=periods)
    rows = []
    for i, day in enumerate(dates):
        close = 80.0 + i * 0.6
        row = {
            "date": day,
            "open": close - 0.2,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1_000_000 + i * 1000,
        }
        if source is not None:
            row["data_source"] = source
        rows.append(row)
    return pd.DataFrame(rows)


def _pivot(history: pd.DataFrame, index: int, kind: str, price: float, *, confirm_lag: int = 2) -> dict:
    return {
        "kind": kind,
        "price": price,
        "origin_time": history.iloc[index]["date"].date().isoformat(),
        "confirmed_at": history.iloc[index + confirm_lag]["date"].date().isoformat(),
        "provisional": False,
        "algorithm_version": "confirmed-pivot-v1",
        "config_hash": "price-structure-config",
    }


def _price_context(history: pd.DataFrame, pivots: list[dict], *, target_index: int = -1, event: dict | None = None) -> dict:
    target = history.iloc[target_index]["date"].date()
    return {
        "schema_version": "price-structure-v1",
        "status": "READY",
        "target_date": target.isoformat(),
        "historical_replay_eligible": True,
        "source_alignment": {"status": "SINGLE_SOURCE", "sources": ["Fetcher"], "rows_complete": True},
        "pivots": pivots,
        "structure_event": event or {"state": "NONE", "provisional": False},
    }


def _supply_context(history: pd.DataFrame, *, target_index: int = -1, ratio: float = 1.4) -> dict:
    return {
        "schema_version": "supply-demand-volume-price-v1",
        "family": "supply_demand_volume_price",
        "status": "READY",
        "target_date": history.iloc[target_index]["date"].date().isoformat(),
        "state": "DEMAND_PRESSURE",
        "relative_volume": {"volume_ratio_20d": ratio},
    }


def _build(history: pd.DataFrame, price_context: dict, *, target_index: int = -1) -> dict:
    return build_pattern_trigger_context(
        stock_code="600519",
        history=history,
        target_date=history.iloc[target_index]["date"].date(),
        market="cn",
        price_structure_context=price_context,
        supply_demand_context=_supply_context(history, target_index=target_index),
    )


def _pattern(context: dict, pattern_type: str, subtype: str | None = None) -> dict:
    return next(
        item
        for item in context["patterns"]
        if item["pattern_type"] == pattern_type and (subtype is None or item.get("subtype") == subtype)
    )


def test_double_bottom_forming_confirmed_and_failed_reuse_price_structure_event_owner():
    history = _history()
    first = _pivot(history, 35, "LOW", 80.0)
    middle = _pivot(history, 43, "HIGH", 94.0)
    second = _pivot(history, 52, "LOW", 78.5)
    pivots = [first, middle, second]

    forming = _build(history, _price_context(history, pivots))
    pattern = _pattern(forming, "DOUBLE_BOTTOM_BASE")
    assert pattern["method_profile"] == "ONEIL_BULLISH_CONTINUATION_BASE"
    assert pattern["geometry_state"] == "READY"
    assert pattern["lifecycle"] == "FORMING"
    assert pattern["confirmed_at"] is None
    assert forming["hard_veto"] is False
    assert forming["independent_action_authority"] is False

    breakout_event = {
        "state": "UP_BREAKOUT",
        "level": middle["price"],
        "level_kind": "HIGH",
        "level_origin_time": middle["origin_time"],
        "level_confirmed_at": middle["confirmed_at"],
        "event_time": history.iloc[65]["date"].date().isoformat(),
        "provisional": False,
        "invalidation": "COMPLETED_CLOSE_BELOW_BROKEN_RESISTANCE",
        "algorithm_version": "confirmed-pivot-v1",
        "config_hash": "price-structure-config",
    }
    confirmed = _build(history, _price_context(history, pivots, event=breakout_event))
    confirmed_pattern = _pattern(confirmed, "DOUBLE_BOTTOM_BASE")
    assert confirmed_pattern["lifecycle"] == "CONFIRMED"
    assert confirmed_pattern["confirmed_at"] == breakout_event["event_time"]
    assert confirmed_pattern["price_structure_trigger_ref"]["owner"] == "price-structure-v1"
    assert confirmed_pattern["price_structure_trigger_ref"]["state"] == "UP_BREAKOUT"

    failed_event = dict(breakout_event, state="FAILED_UP_BREAKOUT", event_time=history.iloc[70]["date"].date().isoformat())
    failed = _build(history, _price_context(history, pivots, event=failed_event))
    failed_pattern = _pattern(failed, "DOUBLE_BOTTOM_BASE")
    assert failed_pattern["lifecycle"] == "FAILED"
    assert failed_pattern["geometry_state"] == "INVALIDATED"
    assert failed_pattern["invalidated_at"] == failed_event["event_time"]
    assert failed_pattern["confirmed_at"] is None


def test_cup_base_uses_ordered_confirmed_pivots_and_records_handle_state():
    history = _history()
    pivots = [
        _pivot(history, 30, "HIGH", 110.0),
        _pivot(history, 40, "LOW", 88.0),
        _pivot(history, 52, "HIGH", 108.0),
        _pivot(history, 58, "LOW", 102.0),
    ]
    context = _build(history, _price_context(history, pivots))
    cup = _pattern(context, "CUP_BASE")
    assert cup["handle_status"] == "COMPLETE"
    assert cup["geometry_state"] == "READY"
    assert cup["lifecycle"] == "FORMING"
    assert [item["kind"] for item in cup["ordered_pivots"]] == ["HIGH", "LOW", "HIGH", "LOW"]
    assert cup["prior_trend"]["passes_required_state"] is True
    assert cup["metrics"]["cup_depth_pct"] > 8.0
    assert cup["effective_parameters"]["parameter_policy"] == "VERSIONED_CANDIDATE_PARAMETERS_NOT_UNIVERSAL_CONSTANTS"
    assert cup["volume_evidence_ref"]["confirmation"] == "CONFIRMED"


def test_vcp_contraction_uses_decreasing_confirmed_swings_without_recomputing_breakout():
    history = _history()
    pivots = [
        _pivot(history, 30, "HIGH", 120.0),
        _pivot(history, 35, "LOW", 90.0),
        _pivot(history, 42, "HIGH", 110.0),
        _pivot(history, 48, "LOW", 100.0),
        _pivot(history, 54, "HIGH", 107.0),
        _pivot(history, 60, "LOW", 103.0),
    ]
    context = _build(history, _price_context(history, pivots))
    vcp = _pattern(context, "CONTRACTION_BASE", "VCP")
    assert vcp["geometry_state"] == "READY"
    assert vcp["lifecycle"] == "FORMING"
    assert len(vcp["metrics"]["swing_contraction_pct"]) == 4
    assert all(
        later < earlier
        for earlier, later in zip(vcp["metrics"]["swing_contraction_pct"], vcp["metrics"]["swing_contraction_pct"][1:])
    )
    assert vcp["price_structure_trigger_ref"] == {"status": "MISSING"}


def test_flat_base_and_tight_consolidation_subtypes_are_reachable_without_vcp_overlap():
    flat_history = _history()
    flat_pivots = [
        _pivot(flat_history, 68, "HIGH", 123.0),
        _pivot(flat_history, 75, "LOW", 121.0),
        _pivot(flat_history, 82, "HIGH", 128.0),
    ]
    flat = _build(flat_history, _price_context(flat_history, flat_pivots))
    flat_base = _pattern(flat, "CONTRACTION_BASE", "FLAT_BASE")
    assert flat_base["metrics"]["window_sessions"] == 25
    assert flat_base["metrics"]["range_pct"] <= 15.0
    assert flat_base["lifecycle"] == "FORMING"

    tight_history = _history(periods=100)
    tight_history.loc[tight_history.index[75], "low"] = 90.0
    tight_pivots = [
        _pivot(tight_history, 91, "HIGH", 136.0),
        _pivot(tight_history, 94, "LOW", 134.0),
        _pivot(tight_history, 97, "HIGH", 138.0),
    ]
    tight = _build(tight_history, _price_context(tight_history, tight_pivots))
    tight_base = _pattern(tight, "CONTRACTION_BASE", "TIGHT_CONSOLIDATION")
    assert tight_base["metrics"]["window_sessions"] == 10
    assert tight_base["metrics"]["range_pct"] <= 8.0
    assert tight_base["lifecycle"] == "FORMING"


def test_near_miss_missing_and_source_mismatch_fail_closed_without_canonical_pattern_claim():
    history = _history()
    history.loc[history.index[-25:], "high"] = 180.0
    history.loc[history.index[-25:], "low"] = 70.0
    pivots = [
        _pivot(history, 35, "LOW", 80.0),
        _pivot(history, 43, "HIGH", 84.0),
        _pivot(history, 52, "LOW", 100.0),
    ]
    no_pattern = _build(history, _price_context(history, pivots))
    assert no_pattern["status"] == "READY"
    assert no_pattern["reason"] == "NO_SUPPORTED_PATTERN"
    assert no_pattern["patterns"] == []

    mixed = _history()
    mixed.loc[mixed.index[-1], "data_source"] = "OtherFetcher"
    partial = _build(mixed, _price_context(mixed, pivots=[]))
    assert partial["status"] == "PARTIAL"
    assert partial["reason"] == "SOURCE_ALIGNMENT_UNPROVEN"
    assert partial["historical_replay_eligible"] is False

    missing_target = build_pattern_trigger_context(
        stock_code="600519",
        history=history,
        target_date=(history.iloc[-1]["date"] + pd.Timedelta(days=1)).date(),
        price_structure_context=_price_context(history, []),
    )
    assert missing_target["status"] == "MISSING"
    assert missing_target["reason"] == "TARGET_DATE_BAR_MISSING"


def test_target_date_prefix_equivalence_and_future_confirmation_never_backfills():
    history = _history(periods=100)
    target_index = 75
    target = history.iloc[target_index]["date"].date()
    prefix = history.iloc[: target_index + 1].copy()
    pivots = [
        _pivot(history, 35, "LOW", 80.0),
        _pivot(history, 43, "HIGH", 94.0),
        _pivot(history, 52, "LOW", 78.5),
    ]
    price_context = _price_context(history, pivots, target_index=target_index)
    supply = _supply_context(history, target_index=target_index)
    full = build_pattern_trigger_context(
        stock_code="600519",
        history=history,
        target_date=target,
        price_structure_context=price_context,
        supply_demand_context=supply,
    )
    prefix_only = build_pattern_trigger_context(
        stock_code="600519",
        history=prefix,
        target_date=target,
        price_structure_context=price_context,
        supply_demand_context=supply,
    )
    for key in (
        "status", "reason", "observations", "start_date", "end_date", "source_alignment",
        "volume_evidence_ref", "patterns", "primary_pattern", "pattern_count", "historical_replay_eligible",
    ):
        assert full[key] == prefix_only[key]

    future_pivot = dict(
        pivots[-1],
        confirmed_at=history.iloc[target_index + 5]["date"].date().isoformat(),
    )
    future_context = dict(price_context, pivots=[*pivots[:-1], future_pivot])
    refused = build_pattern_trigger_context(
        stock_code="600519",
        history=history,
        target_date=target,
        price_structure_context=future_context,
        supply_demand_context=supply,
    )
    assert refused["status"] == "UNKNOWN"
    assert refused["reason"] == "PRICE_STRUCTURE_FUTURE_CONFIRMATION"
    assert refused["patterns"] == []


def test_same_swing_dedup_keeps_one_canonical_interpretation():
    common = {
        "correlation_group": "same_swing:demo",
        "pattern_id": "demo",
        "geometry_confirmed_at": "2026-01-20",
        "lifecycle": "FORMING",
    }
    weaker = {**common, "pattern_type": "CONTRACTION_BASE", "subtype": "TIGHT_CONSOLIDATION"}
    stronger = {**common, "pattern_type": "CUP_BASE", "subtype": None}
    deduped = _dedupe_patterns([weaker, stronger])
    assert len(deduped) == 1
    assert deduped[0]["pattern_type"] == "CUP_BASE"


def _call_named(node: ast.Call, name: str) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Name) and func.id == name
    ) or (
        isinstance(func, ast.Attribute) and func.attr == name
    )


def _forwards_named_keyword(call: ast.Call, keyword: str) -> bool:
    item = next((kw for kw in call.keywords if kw.arg == keyword), None)
    return (
        item is not None
        and isinstance(item.value, ast.Name)
        and item.value.id == keyword
    )


def _final_factor_builder_forwards_pattern_context(source: str) -> bool:
    tree = ast.parse(source)
    attach = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_attach_factor_decision_summary"
        ),
        None,
    )
    if attach is None:
        return False
    calls = [
        node
        for node in ast.walk(attach)
        if isinstance(node, ast.Call)
        and _call_named(node, "build_stock_factor_decision_summary")
    ]
    return len(calls) == 1 and _forwards_named_keyword(calls[0], "pattern_trigger_context")


def test_pipeline_wires_one_pattern_context_into_normal_and_agent_final_consumers():
    source = (Path(__file__).resolve().parents[1] / "src" / "core" / "pipeline.py").read_text(encoding="utf-8")
    assert "from src.services.pattern_trigger_service import build_pattern_trigger_context" in source
    assert "pattern_trigger_context = build_pattern_trigger_context(" in source
    tree = ast.parse(source)
    attach_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _call_named(node, "_attach_factor_decision_summary")
    ]
    assert len(attach_calls) >= 2
    assert all(_forwards_named_keyword(call, "pattern_trigger_context") for call in attach_calls)
    assert _final_factor_builder_forwards_pattern_context(source)


def test_pipeline_final_factor_builder_handoff_guard_detects_missing_pattern_keyword():
    source = (Path(__file__).resolve().parents[1] / "src" / "core" / "pipeline.py").read_text(encoding="utf-8")
    assert _final_factor_builder_forwards_pattern_context(source)
    exact_final_hop = (
        "                pattern_trigger_context=pattern_trigger_context,\n"
        "                include_canonical=self.p0_bounded_trial,"
    )
    assert exact_final_hop in source
    broken = source.replace(
        exact_final_hop,
        "                include_canonical=self.p0_bounded_trial,",
        1,
    )
    assert not _final_factor_builder_forwards_pattern_context(broken)
