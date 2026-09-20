import ast
from pathlib import Path

from types import SimpleNamespace

from src.services.factor_decision_summary import (
    apply_canonical_decision_to_result,
    assert_canonical_consumer_consistency,
    build_stock_factor_decision_summary,
)


def _trend(**overrides):
    values = {
        "current_price": 10.50,
        "support_levels": [10.00],
        "resistance_levels": [11.20],
        "trend_status": "多头排列",
        "buy_signal": "买入",
        "volume_status": "缩量回调",
        "signal_score": 78,
        "risk_factors": [],
        "ma_alignment": "MA5>MA10>MA20",
        "trend_strength": 78,
        "volume_ratio_5d": 0.82,
        "volume_trend": "缩量回调",
        "macd_signal": "MACD多头结构",
        "rsi_signal": "RSI偏强",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _volatility_with_bearish_divergence():
    return {
        "status": "READY",
        "volatility": {
            "status": "READY",
            "realized_volatility_20d_annualized_pct": 22.5,
            "true_range_sma_20_pct": 2.1,
        },
        "momentum": {
            "status": "READY",
            "roc_20_pct": 5.2,
            "roc_60_pct": 12.8,
            "macd": {"status": "多头但动能放缓"},
            "rsi": {"status": "偏强"},
        },
        "confirmed_divergence": {
            "status": "READY",
            "state": "BEARISH_DIVERGENCE",
            "signals": ["MACD", "RSI"],
        },
        "process_diagnostics": {
            "status": "READY",
            "classification_authority": "OBSERVATIONAL_ONLY",
        },
    }


def _confirmed_double_bottom():
    primary = {
        "pattern_type": "DOUBLE_BOTTOM_BASE",
        "subtype": None,
        "geometry_state": "READY",
        "lifecycle": "CONFIRMED",
        "origin_time": "2026-01-05",
        "geometry_confirmed_at": "2026-02-02",
        "confirmed_at": "2026-02-06",
        "volume_evidence_ref": {"confirmation": "CONFIRMED", "volume_ratio_20d": 1.4},
        "price_structure_trigger_ref": {"status": "READY", "state": "UP_BREAKOUT"},
    }
    return {
        "status": "READY",
        "historical_replay_eligible": True,
        "patterns": [primary],
        "primary_pattern": primary,
    }


def _mtf_context():
    return {
        "timeframes": {
            "monthly": {
                "status": "READY",
                "role": "LONG_TERM_CONTEXT",
                "summary": "月线保持上升通道，回调量能温和",
            },
            "weekly": {
                "status": "READY",
                "role": "PRIMARY_TREND_CONTEXT",
                "summary": "周线多头排列，相对强势",
            },
            "60m": {
                "status": "MISSING",
                "role": "OPTIONAL_BRIDGE",
                "summary": None,
            },
        }
    }


def _result():
    return SimpleNamespace(
        action="buy",
        action_label="买入",
        operation_advice="旧建议",
        decision_type="buy",
        analysis_summary="旧结论",
        buy_reason="旧理由",
        dashboard={},
    )


def test_phase_a_stock_human_synthesis_renders_material_events_with_exact_timeframe():
    summary = build_stock_factor_decision_summary(
        _trend(),
        relative_strength_context={
            "status": "READY",
            "horizon_sessions": 60,
            "benchmark": {"code": "510300", "name": "沪深300ETF"},
            "relative": {"state": "OUTPERFORMING", "relative_ratio_change_pct": 6.5},
        },
        volatility_momentum_context=_volatility_with_bearish_divergence(),
        pattern_trigger_context=_confirmed_double_bottom(),
        multi_timeframe_structure_context=_mtf_context(),
        include_canonical=True,
    )

    brief = summary["investor_brief"]
    fused = brief["fused_paragraph"]
    assert "月线保持上升通道" in fused
    assert "周线多头排列" in fused
    assert "日线顶背离已经确认" in fused
    assert "日线双底基底已经放量突破确认" in fused
    assert brief["material_events"].count("日线顶背离已经确认") == 1
    assert fused.count("顶背离") == 1
    assert "缩量回调" in fused
    assert brief["coverage"]["30m"] == "MISSING"
    assert brief["coverage"]["15m"] == "MISSING"
    assert brief["coverage"]["5m"] == "MISSING"
    for forbidden in ("Price Structure", "独立动作权限", "机构意图", "READY"):
        assert forbidden not in fused


def test_phase_a_stable_state_does_not_emit_no_event_checklist():
    summary = build_stock_factor_decision_summary(
        _trend(),
        multi_timeframe_structure_context=_mtf_context(),
        include_canonical=True,
    )
    fused = summary["investor_brief"]["fused_paragraph"]
    assert "无背离" not in fused
    assert "无VCP" not in fused
    assert "无金叉" not in fused


def test_phase_a_etf_uses_etf_authority_and_does_not_fake_stock_or_etf_specific_fields():
    summary = build_stock_factor_decision_summary(
        _trend(),
        multi_timeframe_structure_context=_mtf_context(),
        include_canonical=True,
        asset_type="etf",
    )
    brief = summary["investor_brief"]

    assert summary["strategy_id"] == "etf_relative_strength_rotation_v1"
    assert summary["canonical_decision"]["authority"] == "etf_relative_strength_rotation_v1"
    assert summary["canonical_decision"]["evidence_state"] == "UNKNOWN"
    assert summary["canonical_decision"]["action"] == "WAIT"
    assert summary["canonical_decision"]["reason_codes"] == [
        "ETF_SPECIFIC_EVIDENCE_INCOMPLETE"
    ]
    assert brief["asset_type"] == "etf"
    assert brief["valuation"]["label"] == "底层估值"
    assert brief["valuation"]["status"] == "MISSING"
    assert "筹码" not in brief["fused_paragraph"]
    assert set(brief["asset_specific"]) == {
        "underlying_valuation",
        "premium_discount",
        "liquidity_spread",
        "tracking_quality",
    }
    assert all(item["status"] == "MISSING" for item in brief["asset_specific"].values())
    assert "可升级为买入候选" not in summary["action_condition"]
    assert "ETF专属估值与交易质量证据完整" in summary["action_condition"]
    assert "ETF专属估值与交易质量证据尚未完整" in summary["conclusion"]


def test_phase_a_production_canonical_scope_has_no_p0_user_language_and_is_consistent():
    summary = build_stock_factor_decision_summary(_trend(), include_canonical=True)
    result = apply_canonical_decision_to_result(_result(), summary, scope="production")

    assert result.action == summary["canonical_decision"]["public_action"]
    assert "P0" not in result.buy_reason
    assert "P0" not in result.dashboard["phase_decision"]["action_window"]
    assert_canonical_consumer_consistency(result, scope="production")


def test_phase_a_p0_default_scope_keeps_existing_p0_boundary():
    summary = build_stock_factor_decision_summary(_trend(), include_canonical=True)
    result = apply_canonical_decision_to_result(_result(), summary)

    assert "P0" in result.buy_reason
    assert "P0" in result.dashboard["phase_decision"]["action_window"]
    assert "P0" in result.dashboard["phase_decision"]["confidence_reason"]
    assert result.dashboard["battle_plan"]["position_strategy"]["suggested_position"] == "不新增仓位"
    assert_canonical_consumer_consistency(result)


def test_phase_a_valuation_never_invents_reasonable_range_from_basic_pe_pb():
    summary = build_stock_factor_decision_summary(
        _trend(),
        fundamental_context={
            "market": "cn",
            "valuation": {"data": {"pe_ratio": 18.0, "pb_ratio": 2.1}},
        },
        include_canonical=True,
    )
    brief = summary["investor_brief"]
    assert "PE 18.0" in summary["valuation"]
    assert "PB 2.1" in summary["valuation"]
    assert "合理估值区间" not in summary["valuation"]
    assert brief["valuation"]["status"] == "PARTIAL_CURRENT"
    assert "PE 18.0" in brief["valuation"]["summary"]
    assert "PB 2.1" in brief["valuation"]["summary"]


def test_phase_a_pipeline_wiring_keeps_etf_p0_boundary_and_normal_canonical_consistency():
    source = Path("src/core/pipeline.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    attach = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_attach_factor_decision_summary"
    )
    calls = [node for node in ast.walk(attach) if isinstance(node, ast.Call)]

    builder = next(
        node
        for node in calls
        if isinstance(node.func, ast.Name)
        and node.func.id == "build_stock_factor_decision_summary"
    )
    builder_keywords = {item.arg: item.value for item in builder.keywords if item.arg}
    assert isinstance(builder_keywords["include_canonical"], ast.Constant)
    assert builder_keywords["include_canonical"].value is True
    assert isinstance(builder_keywords["asset_type"], ast.Name)
    assert builder_keywords["asset_type"].id == "asset_type"

    apply_call = next(
        node
        for node in calls
        if isinstance(node.func, ast.Name)
        and node.func.id == "apply_canonical_decision_to_result"
    )
    consistency_call = next(
        node
        for node in calls
        if isinstance(node.func, ast.Name)
        and node.func.id == "assert_canonical_consumer_consistency"
    )
    for call in (apply_call, consistency_call):
        keywords = {item.arg: item.value for item in call.keywords if item.arg}
        assert isinstance(keywords["scope"], ast.Name)
        assert keywords["scope"].id == "canonical_scope"

    rendered = ast.unparse(attach)
    assert "is_market_index = is_us_index_code(normalized_code)" in rendered
    assert "if is_market_index" in rendered
    assert "if is_index_or_etf and self.p0_bounded_trial" in rendered
    assert "asset_type = 'etf' if is_index_or_etf else 'stock'" in rendered
