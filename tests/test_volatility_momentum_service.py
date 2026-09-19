# -*- coding: utf-8 -*-
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from src.services.volatility_momentum_service import (
    _confirmed_divergence,
    build_volatility_momentum_context,
)


def _history(*, source: str | None = "Fetcher", periods: int = 80) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-02", periods=periods)
    rows = []
    for i, day in enumerate(dates):
        close = 100.0 + i * 0.35 + ((i % 7) - 3) * 0.22
        row = {
            "date": day,
            "open": close - 0.1,
            "high": close + 0.9,
            "low": close - 0.8,
            "close": close,
            "volume": 1_000_000 + i * 1000,
        }
        if source is not None:
            row["data_source"] = source
        rows.append(row)
    return pd.DataFrame(rows)


def _trend_projection():
    return SimpleNamespace(
        macd_status=SimpleNamespace(value="bullish"),
        rsi_status=SimpleNamespace(value="neutral"),
    )


def test_ready_context_reuses_existing_formulas_and_keeps_process_diagnostics_observational():
    history = _history()
    target = history.iloc[-1]["date"].date()
    trend = _trend_projection()

    context = build_volatility_momentum_context(
        stock_code="600519",
        history=history,
        target_date=target,
        market="cn",
        trend_result=trend,
        price_structure_context={"pivots": []},
    )

    assert context["status"] == "READY"
    assert context["hard_veto"] is False
    assert context["independent_action_authority"] is False
    assert context["timeframe"] == "1d"
    assert context["volatility"]["realized_volatility_20d_annualized_pct"] is not None
    assert context["volatility"]["true_range_sma_20_pct"] is not None
    assert context["volatility"]["true_range_semantics"] == "SIMPLE_MEAN_TRUE_RANGE_20_NOT_WILDER_ATR"
    assert context["momentum"]["roc_20_pct"] is not None
    assert context["momentum"]["roc_60_pct"] is not None
    assert context["momentum"]["macd"]["formula_owner"] == "StockTrendAnalyzer._calculate_macd"
    assert context["momentum"]["rsi"]["formula_owner"] == "StockTrendAnalyzer._calculate_rsi"
    diagnostics = context["process_diagnostics"]
    assert diagnostics["classification_authority"] == "OBSERVATIONAL_ONLY"
    assert diagnostics["world_classification"] == "NOT_PERFORMED"
    assert diagnostics["automatic_strategy_switching"] is False
    assert diagnostics["variance_scaling_semantics"].endswith("NOT_SIGNIFICANCE_TEST")
    assert "ADF_STATIONARITY" in diagnostics["deferred_models"]
    assert "GARCH" in diagnostics["deferred_models"]


def test_missing_mixed_source_and_target_date_fail_closed():
    missing_source = _history(source=None)
    target = missing_source.iloc[-1]["date"].date()
    trend = _trend_projection()
    partial = build_volatility_momentum_context(
        stock_code="600519", history=missing_source, target_date=target, trend_result=trend
    )
    assert partial["status"] == "PARTIAL"
    assert partial["reason"] == "SOURCE_ALIGNMENT_UNPROVEN"
    assert partial["historical_replay_eligible"] is False

    mixed = _history()
    mixed.loc[mixed.index[-1], "data_source"] = "OtherFetcher"
    mixed_trend = _trend_projection()
    mixed_context = build_volatility_momentum_context(
        stock_code="600519", history=mixed, target_date=target, trend_result=mixed_trend
    )
    assert mixed_context["status"] == "PARTIAL"

    future_target = (pd.Timestamp(target) + pd.Timedelta(days=1)).date()
    missing = build_volatility_momentum_context(
        stock_code="600519", history=mixed, target_date=future_target, trend_result=mixed_trend
    )
    assert missing["status"] == "MISSING"
    assert missing["reason"] == "TARGET_DATE_BAR_MISSING"


def test_target_date_trim_makes_full_history_equal_to_prefix_history():
    history = _history(periods=90)
    target_index = 70
    target = history.iloc[target_index]["date"].date()
    prefix = history.iloc[: target_index + 1].copy()
    price_structure = {
        "schema_version": "price-structure-v1",
        "status": "READY",
        "historical_replay_eligible": True,
        "pivots": [],
        "target_date": target.isoformat(),
    }
    full = build_volatility_momentum_context(
        stock_code="600519",
        history=history,
        target_date=target,
        trend_result=_trend_projection(),
        price_structure_context=price_structure,
    )
    prefix_only = build_volatility_momentum_context(
        stock_code="600519",
        history=prefix,
        target_date=target,
        trend_result=_trend_projection(),
        price_structure_context=price_structure,
    )
    for key in ("status", "reason", "observations", "start_date", "end_date", "source_alignment", "volatility", "momentum", "confirmed_divergence", "process_diagnostics", "historical_replay_eligible"):
        assert full[key] == prefix_only[key]


def test_builder_refuses_price_structure_from_a_different_target_date():
    history = _history()
    target = history.iloc[-1]["date"].date()
    context = build_volatility_momentum_context(
        stock_code="600519",
        history=history,
        target_date=target,
        trend_result=_trend_projection(),
        price_structure_context={
            "schema_version": "price-structure-v1",
            "status": "READY",
            "historical_replay_eligible": True,
            "target_date": history.iloc[-2]["date"].date().isoformat(),
            "pivots": [],
        },
    )
    assert context["confirmed_divergence"]["status"] == "MISSING"
    assert context["confirmed_divergence"]["reason"] == "PRICE_STRUCTURE_TARGET_DATE_UNPROVEN"


def test_confirmed_divergence_normalizes_macd_and_rsi_on_the_same_swing():
    dates = pd.bdate_range("2026-01-02", periods=8)
    indicators = pd.DataFrame(
        {
            "date": dates,
            "MACD_DIF": [-0.4, -0.3, -0.2, -0.1, -0.1, 0.1, 0.2, 0.3],
            "RSI_12": [30, 32, 34, 36, 38, 45, 48, 50],
        }
    )
    price_structure = {
        "schema_version": "price-structure-v1",
        "status": "READY",
        "historical_replay_eligible": True,
        "pivots": [
            {
                "kind": "LOW",
                "price": 10.0,
                "origin_time": dates[1].date().isoformat(),
                "confirmed_at": dates[3].date().isoformat(),
                "provisional": False,
            },
            {
                "kind": "LOW",
                "price": 9.0,
                "origin_time": dates[5].date().isoformat(),
                "confirmed_at": dates[7].date().isoformat(),
                "provisional": False,
            },
        ]
    }

    divergence = _confirmed_divergence(indicators, price_structure)

    assert divergence["state"] == "BULLISH_DIVERGENCE"
    assert divergence["confirmed_at"] == dates[7].date().isoformat()
    assert divergence["provisional"] is False
    assert {item["oscillator"] for item in divergence["signals"]} == {"MACD_DIF", "RSI_12"}
    assert len({item["correlation_group"] for item in divergence["signals"]}) == 1
    assert divergence["independent_confirmation_group_count"] == 1
    assert divergence["same_swing_double_counting"] == "PROHIBITED"


def test_divergence_refuses_unproven_price_structure_authority():
    dates = pd.bdate_range("2026-01-02", periods=8)
    indicators = pd.DataFrame({"date": dates, "MACD_DIF": range(8), "RSI_12": range(30, 38)})
    unproven = {
        "schema_version": "price-structure-v1",
        "status": "PARTIAL",
        "historical_replay_eligible": False,
        "pivots": [
            {"kind": "LOW", "price": 10.0, "origin_time": dates[1].date().isoformat(), "confirmed_at": dates[3].date().isoformat(), "provisional": False},
            {"kind": "LOW", "price": 9.0, "origin_time": dates[5].date().isoformat(), "confirmed_at": dates[7].date().isoformat(), "provisional": False},
        ],
    }
    divergence = _confirmed_divergence(indicators, unproven)
    assert divergence["status"] == "MISSING"
    assert divergence["state"] == "NONE"
    assert divergence["independent_confirmation_group_count"] == 0


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


def _final_factor_builder_forwards_vm_context(source: str) -> bool:
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
    return len(calls) == 1 and _forwards_named_keyword(
        calls[0], "volatility_momentum_context"
    )


def test_pipeline_wires_one_context_into_normal_and_agent_factor_paths():
    source = (Path(__file__).resolve().parents[1] / "src" / "core" / "pipeline.py").read_text(encoding="utf-8")
    assert "from src.services.volatility_momentum_service import build_volatility_momentum_context" in source
    assert "volatility_momentum_context = build_volatility_momentum_context(" in source

    tree = ast.parse(source)
    attach_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _call_named(node, "_attach_factor_decision_summary")
    ]
    assert len(attach_calls) >= 2
    assert all(
        _forwards_named_keyword(call, "volatility_momentum_context")
        for call in attach_calls
    )
    assert _final_factor_builder_forwards_vm_context(source)


def test_pipeline_final_factor_builder_handoff_guard_detects_missing_vm_keyword():
    source = (Path(__file__).resolve().parents[1] / "src" / "core" / "pipeline.py").read_text(encoding="utf-8")
    assert _final_factor_builder_forwards_vm_context(source)

    tree = ast.parse(source)
    attach = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_attach_factor_decision_summary"
    )
    calls = [
        node
        for node in ast.walk(attach)
        if isinstance(node, ast.Call)
        and _call_named(node, "build_stock_factor_decision_summary")
    ]
    assert len(calls) == 1
    calls[0].keywords = [
        kw for kw in calls[0].keywords if kw.arg != "volatility_momentum_context"
    ]
    broken = ast.unparse(tree)
    assert not _final_factor_builder_forwards_vm_context(broken)
