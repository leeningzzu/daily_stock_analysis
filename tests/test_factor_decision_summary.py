from types import SimpleNamespace

import pandas as pd

from src.analyzer import AnalysisResult
from src.services.factor_decision_summary import (
    apply_canonical_decision_to_result,
    assert_canonical_consumer_consistency,
    build_stock_factor_decision_summary,
    canonical_explanation_degradation_eligible,
)
from src.stock_analyzer import StockTrendAnalyzer, TrendAnalysisResult, VolumeStatus


def _enum(value: str):
    return SimpleNamespace(value=value)


def _trend(**overrides):
    data = {
        "signal_score": 82,
        "trend_status": _enum("多头排列"),
        "buy_signal": _enum("买入"),
        "ma_alignment": "MA5 > MA10 > MA20",
        "trend_strength": 78,
        "current_price": 10.5,
        "support_levels": [10.0, 10.2],
        "resistance_levels": [11.3],
        "volume_status": _enum("缩量回调"),
        "volume_ratio_5d": 0.62,
        "volume_trend": "缩量回调，卖压收缩；是否属于洗盘需后续确认",
        "macd_signal": "MACD多头结构",
        "rsi_signal": "RSI中性偏强",
        "risk_factors": [],
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_summary_reuses_existing_score_without_inventing_probability_or_win_rate():
    summary = build_stock_factor_decision_summary(
        _trend(),
        fundamental_context={
            "market": "cn",
            "valuation": {"data": {"pe_ratio": 20.0, "pb_ratio": 3.0}},
        },
        chip_data=SimpleNamespace(avg_cost=10.0, concentration_90=0.18, profit_ratio=0.64),
    )

    assert summary["composite_score"] == 82
    assert summary["historical_reference"] == {
        "available": False,
        "display": "样本不足，暂不展示",
    }
    assert summary["current_probability"] == {
        "available": False,
        "display": "暂不提供（尚未完成独立校准）",
    }
    assert "不代表胜率或概率" in summary["score_note"]
    assert "canonical_decision" not in summary
    assert "PE 20.0" in summary["valuation"]
    assert "PB 3.0" in summary["valuation"]
    assert "暂不判断偏低、合理或偏高" in summary["valuation"]
    assert "筹码平均成本参考约 10.00" in summary["cost_structure"]
    assert "主力" not in str(summary)


def test_static_pe_pb_without_percentile_or_peers_does_not_claim_relative_valuation():
    summary = build_stock_factor_decision_summary(
        _trend(signal_score=68),
        fundamental_context={
            "market": "cn",
            "valuation": {"data": {"pe_ratio": 75.0, "pb_ratio": 9.0}},
        },
    )

    assert "PE 75.0" in summary["valuation"]
    assert "PB 9.0" in summary["valuation"]
    assert "缺少可靠历史分位和同行比较" in summary["valuation"]
    assert "暂不判断偏低、合理或偏高" in summary["valuation"]
    assert "静态估值偏高" not in summary["valuation"]
    assert "历史相对低位" not in summary["valuation"]
    assert "合理价值" not in summary["valuation"]
    assert "内在价值" not in summary["valuation"]


def test_volume_price_language_marks_shakeout_only_as_candidate():
    summary = build_stock_factor_decision_summary(_trend())
    volume_text = summary["sections"]["volume_price"]

    assert "洗盘候选特征" in volume_text
    assert "后续转强确认" in volume_text
    assert "主力" not in volume_text


def test_heavy_volume_down_cannot_be_described_as_shakeout():
    summary = build_stock_factor_decision_summary(
        _trend(
            signal_score=45,
            volume_status=_enum("放量下跌"),
            volume_ratio_5d=1.9,
            trend_status=_enum("弱势空头"),
        )
    )

    assert "不能解释为洗盘" in summary["sections"]["volume_price"]
    assert "当前不适合新增仓位" in summary["conclusion"]


def test_stock_analyzer_shrink_volume_wording_no_longer_asserts_main_force_intent():
    analyzer = StockTrendAnalyzer()
    result = TrendAnalysisResult(code="000001")
    df = pd.DataFrame(
        {
            "close": [10.0, 10.1, 10.2, 10.3, 10.4, 10.2],
            "volume": [100.0, 100.0, 100.0, 100.0, 100.0, 50.0],
        }
    )

    analyzer._analyze_volume(df, result)

    assert result.volume_status == VolumeStatus.SHRINK_VOLUME_DOWN
    assert "卖压收缩" in result.volume_trend
    assert "需后续确认" in result.volume_trend
    assert "主力" not in result.volume_trend


def _llm_result(*, advice="买入", action="buy", decision_type="buy"):
    return AnalysisResult(
        code="600519",
        name="贵州茅台",
        sentiment_score=96,
        trend_prediction="强烈看多",
        operation_advice=advice,
        decision_type=decision_type,
        action=action,
        action_label="买入",
        analysis_summary="LLM 建议立即买入",
        buy_reason="LLM 看多并建议买入",
        dashboard={
            "action": action,
            "action_label": "买入",
            "operation_advice": advice,
            "decision_type": decision_type,
            "analysis_summary": "LLM 建议立即买入",
            "buy_reason": "LLM 看多并建议买入",
            "core_conclusion": {
                "one_sentence": "立即买入",
                "position_advice": {"no_position": "买入", "has_position": "加仓"},
            },
            "phase_decision": {"immediate_action": "现在买入"},
            "strategy_synthesis": {"final_signal": "buy"},
            "battle_plan": {
                "sniper_points": {"ideal_buy": "10.00"},
                "action_checklist": ["买入"],
            },
            "decision_score_calibration": {"final_action": "buy"},
            "decision_stability": {"final_action": "buy", "reason": "LLM"},
        },
    )


def test_p0_canonical_wait_overrides_conflicting_llm_buy_in_every_action_slot():
    summary = build_stock_factor_decision_summary(
        _trend(signal_score=99), include_canonical=True
    )
    result = apply_canonical_decision_to_result(_llm_result(), summary)

    assert summary["canonical_decision"] == {
        "authority": "stock_trend_quality_pullback_v1",
        "action": "WAIT",
        "public_action": "watch",
        "evidence_state": "PROVEN",
        "hard_veto": False,
        "reason_codes": ["CONDITIONAL_OBSERVATION_ONLY"],
    }
    assert result.action == "watch"
    assert result.decision_type == "hold"
    assert result.dashboard["strategy_synthesis"]["final_signal"] == "hold"
    assert result.dashboard["decision_score_calibration"]["final_action"] == "watch"
    assert result.dashboard["decision_stability"]["final_action"] == "watch"
    assert "P0 不生成买卖点" in result.dashboard["battle_plan"]["sniper_points"]["ideal_buy"]
    assert_canonical_consumer_consistency(result)


def test_p0_canonical_wait_overrides_unsupported_llm_sell():
    summary = build_stock_factor_decision_summary(_trend(), include_canonical=True)
    result = _llm_result(advice="卖出", action="sell", decision_type="sell")

    apply_canonical_decision_to_result(result, summary)

    assert result.action == "watch"
    assert result.operation_advice.startswith("观望")
    assert result.dashboard["phase_decision"]["immediate_action"].startswith("观望")
    assert_canonical_consumer_consistency(result)


def test_p0_hard_veto_overrides_high_score_and_positive_llm_text():
    summary = build_stock_factor_decision_summary(
        _trend(signal_score=99, volume_status=_enum("放量下跌")),
        include_canonical=True,
    )
    result = apply_canonical_decision_to_result(_llm_result(), summary)

    assert summary["canonical_decision"]["action"] == "PASS"
    assert summary["canonical_decision"]["hard_veto"] is True
    assert "HEAVY_VOLUME_DOWN" in summary["canonical_decision"]["reason_codes"]
    assert result.action == "avoid"
    assert result.operation_advice.startswith("回避")
    assert_canonical_consumer_consistency(result)


def test_p0_missing_required_evidence_fails_closed_to_unknown_wait():
    summary = build_stock_factor_decision_summary(None, include_canonical=True)
    result = apply_canonical_decision_to_result(_llm_result(), summary)

    assert summary["composite_score"] is None
    assert summary["canonical_decision"]["action"] == "WAIT"
    assert summary["canonical_decision"]["evidence_state"] == "UNKNOWN"
    assert summary["canonical_decision"]["reason_codes"] == ["MISSING_TREND_RESULT"]
    assert result.operation_advice.startswith("观望：必需证据不足")
    assert_canonical_consumer_consistency(result)


def test_market_regime_red_ready_is_canonical_veto_but_green_never_upgrades_wait():
    red = build_stock_factor_decision_summary(
        _trend(signal_score=99),
        daily_market_context={"market_light": {"status": "red", "score": 25, "data_quality": "ok"}},
        include_canonical=True,
    )
    evidence = red["market_sector_regime"]
    assert evidence["market"]["state"] == "RISK_OFF"
    assert evidence["hard_veto"] is True
    assert red["canonical_decision"]["action"] == "PASS"
    assert "MARKET_REGIME_RISK_OFF" in red["canonical_decision"]["reason_codes"]

    green = build_stock_factor_decision_summary(
        _trend(signal_score=99),
        daily_market_context={"market_light": {"status": "green", "score": 82, "data_quality": "ok"}},
        include_canonical=True,
    )
    assert green["market_sector_regime"]["market"]["state"] == "PERMISSIVE"
    assert green["market_sector_regime"]["hard_veto"] is False
    assert green["canonical_decision"]["action"] == "WAIT"


def test_partial_red_and_sector_cooling_are_caution_not_hard_veto():
    summary = build_stock_factor_decision_summary(
        _trend(signal_score=88),
        daily_market_context={"market_light": {"status": "red", "score": 30, "data_quality": "partial"}},
        market_structure_context={
            "status": "partial",
            "stock_market_position": {
                "status": "partial",
                "primary_theme": {"name": "机器人概念", "phase": "cooling"},
                "theme_phase": "cooling",
                "stock_role": "edge",
                "risk_tags": [{"code": "theme_data_partial"}],
            },
        },
        include_canonical=True,
    )
    evidence = summary["market_sector_regime"]
    assert evidence["evidence_state"] == "PARTIAL"
    assert evidence["market"]["state"] == "CAUTION"
    assert evidence["sector"]["state"] == "COOLING"
    assert evidence["hard_veto"] is False
    assert "SECTOR_THEME_COOLING" in evidence["reason_codes"]
    assert summary["canonical_decision"]["action"] == "WAIT"


def test_market_sector_regime_missing_inputs_stay_unknown_without_changing_legacy_decision():
    summary = build_stock_factor_decision_summary(_trend(signal_score=82), include_canonical=True)
    evidence = summary["market_sector_regime"]
    assert evidence["evidence_state"] == "UNKNOWN"
    assert evidence["hard_veto"] is False
    assert summary["canonical_decision"]["action"] == "WAIT"
    assert summary["canonical_decision"]["reason_codes"] == ["CONDITIONAL_OBSERVATION_ONLY"]

    unqualified_red = build_stock_factor_decision_summary(
        _trend(signal_score=99),
        daily_market_context={"market_light": {"status": "red", "score": 25}},
        include_canonical=True,
    )
    assert unqualified_red["market_sector_regime"]["market"]["state"] == "UNKNOWN"
    assert unqualified_red["market_sector_regime"]["hard_veto"] is False
    assert unqualified_red["canonical_decision"]["action"] == "WAIT"


def test_relative_strength_is_first_class_evidence_but_never_independently_upgrades_or_vetoes():
    positive = build_stock_factor_decision_summary(
        _trend(signal_score=99),
        relative_strength_context={
            "status": "READY",
            "horizon_sessions": 60,
            "benchmark": {"code": "510300", "name": "华泰柏瑞沪深300ETF", "kind": "etf_proxy"},
            "relative": {"state": "OUTPERFORMING", "relative_ratio_change_pct": 8.5},
        },
        include_canonical=True,
    )
    evidence = positive["trend_relative_strength"]
    assert evidence["evidence_state"] == "READY"
    assert evidence["relative_strength"]["benchmark"]["kind"] == "etf_proxy"
    assert evidence["hard_veto"] is False
    assert positive["canonical_decision"]["action"] == "WAIT"

    negative = build_stock_factor_decision_summary(
        _trend(signal_score=99),
        relative_strength_context={
            "status": "READY",
            "horizon_sessions": 60,
            "benchmark": {"code": "510300", "name": "华泰柏瑞沪深300ETF", "kind": "etf_proxy"},
            "relative": {"state": "UNDERPERFORMING", "relative_ratio_change_pct": -12.0},
        },
        include_canonical=True,
    )
    assert negative["trend_relative_strength"]["relative_strength"]["relative"]["state"] == "UNDERPERFORMING"
    assert negative["trend_relative_strength"]["hard_veto"] is False
    assert negative["canonical_decision"]["action"] == "WAIT"


def test_missing_relative_strength_preserves_legacy_decision_and_weak_completed_trend_still_vetoes():
    missing = build_stock_factor_decision_summary(_trend(signal_score=82), include_canonical=True)
    assert missing["trend_relative_strength"]["evidence_state"] == "PARTIAL"
    assert missing["trend_relative_strength"]["relative_strength"]["status"] == "MISSING"
    assert missing["canonical_decision"]["reason_codes"] == ["CONDITIONAL_OBSERVATION_ONLY"]

    weak = build_stock_factor_decision_summary(
        _trend(signal_score=99, trend_status=_enum("空头排列")),
        relative_strength_context={
            "status": "READY",
            "benchmark": {"code": "510300", "kind": "etf_proxy"},
            "relative": {"state": "OUTPERFORMING", "relative_ratio_change_pct": 5.0},
        },
        include_canonical=True,
    )
    assert weak["canonical_decision"]["action"] == "PASS"
    assert "WEAK_TREND" in weak["canonical_decision"]["reason_codes"]


def test_supply_demand_is_first_class_observable_evidence_without_new_action_authority():
    summary = build_stock_factor_decision_summary(
        _trend(signal_score=99, volume_status=_enum("放量上涨"), volume_ratio_5d=1.8),
        supply_demand_context={
            "status": "READY",
            "state": "DEMAND_PRESSURE",
            "relative_volume": {"volume_ratio_20d": 1.6},
            "directional_volume": {"signed_volume_balance": 0.35},
            "close_location_flow": {"cmf_20": 0.18},
            "institutional_intent_claim": "NOT_INFERRED",
        },
        include_canonical=True,
    )
    evidence = summary["supply_demand_volume_price"]
    assert evidence["evidence_state"] == "READY"
    assert evidence["observable_only"] is True
    assert evidence["institutional_intent_inferred"] is False
    assert evidence["hard_veto"] is False
    assert summary["canonical_decision"]["action"] == "WAIT"
    assert "不推断机构意图" in summary["sections"]["supply_demand_volume_price"]
    assert "主力" not in str(evidence)
    assert "吸筹" not in str(evidence)
    assert "出货" not in str(evidence)


def test_supply_demand_partial_or_supply_pressure_does_not_duplicate_existing_volume_veto():
    summary = build_stock_factor_decision_summary(
        _trend(signal_score=99, volume_status=_enum("量能正常"), volume_ratio_5d=1.0),
        supply_demand_context={
            "status": "PARTIAL",
            "state": "SUPPLY_PRESSURE",
            "relative_volume": {"volume_ratio_20d": 1.7},
            "directional_volume": {"signed_volume_balance": -0.5},
            "close_location_flow": {"cmf_20": -0.3},
            "reason": "SOURCE_ALIGNMENT_UNPROVEN",
        },
        include_canonical=True,
    )
    assert summary["supply_demand_volume_price"]["evidence_state"] == "PARTIAL"
    assert summary["supply_demand_volume_price"]["hard_veto"] is False
    assert summary["canonical_decision"]["action"] == "WAIT"
    assert summary["canonical_decision"]["reason_codes"] == ["CONDITIONAL_OBSERVATION_ONLY"]

    legacy_veto = build_stock_factor_decision_summary(
        _trend(signal_score=99, volume_status=_enum("放量下跌"), volume_ratio_5d=1.9),
        supply_demand_context={"status": "READY", "state": "SUPPLY_PRESSURE"},
        include_canonical=True,
    )
    assert legacy_veto["canonical_decision"]["action"] == "PASS"
    assert legacy_veto["canonical_decision"]["reason_codes"].count("HEAVY_VOLUME_DOWN") == 1


def test_cost_structure_is_first_class_but_never_independent_action_authority():
    summary = build_stock_factor_decision_summary(
        _trend(signal_score=99),
        chip_data=SimpleNamespace(avg_cost=10.0, concentration_90=0.18, profit_ratio=0.64),
        cost_structure_context={
            "status": "READY",
            "provider_chip_snapshot": {
                "status": "READY_CURRENT_ONLY",
                "provider_reference_avg_cost": 10.0,
                "provider_profit_ratio": 0.64,
                "historical_pit_authority": "CURRENT_ONLY",
            },
            "bar_reference_cost": {
                "status": "READY",
                "window_20": {"status": "READY", "rolling_reference_price": 10.2},
                "window_60": {"status": "READY", "rolling_reference_price": 9.8},
            },
            "composition": {"provider_vs_bar_relation": "CONVERGENT"},
        },
        include_canonical=True,
    )
    evidence = summary["cost_structure_evidence"]
    assert evidence["evidence_state"] == "READY"
    assert evidence["hard_veto"] is False
    assert evidence["independent_action_authority"] is False
    assert summary["canonical_decision"]["action"] == "WAIT"
    assert "20日历史量价参考" in summary["sections"]["cost_structure"]
    assert "不代表真实持仓成本" in summary["sections"]["cost_structure"]
    assert "主力" not in str(evidence)
    assert "吸筹" not in str(evidence)
    assert "出货" not in str(evidence)


def test_provider_only_or_divergent_cost_structure_does_not_veto_or_upgrade():
    provider_only = build_stock_factor_decision_summary(
        _trend(signal_score=99),
        chip_data=SimpleNamespace(avg_cost=10.0, concentration_90=0.18, profit_ratio=0.64),
        include_canonical=True,
    )
    assert provider_only["cost_structure_evidence"]["evidence_state"] == "PARTIAL"
    assert provider_only["canonical_decision"]["action"] == "WAIT"

    divergent = build_stock_factor_decision_summary(
        _trend(signal_score=99),
        cost_structure_context={
            "status": "READY",
            "provider_chip_snapshot": {"status": "READY_CURRENT_ONLY", "provider_reference_avg_cost": 8.0},
            "bar_reference_cost": {
                "status": "READY",
                "window_20": {"status": "READY", "rolling_reference_price": 12.0},
                "window_60": {"status": "READY", "rolling_reference_price": 11.5},
            },
            "composition": {"provider_vs_bar_relation": "DIVERGENT"},
        },
        include_canonical=True,
    )
    assert divergent["cost_structure_evidence"]["hard_veto"] is False
    assert divergent["canonical_decision"]["action"] == "WAIT"


def test_p0_non_conflicting_explanation_is_retained_but_not_action_authority():
    summary = build_stock_factor_decision_summary(
        _trend(signal_score=68), include_canonical=True
    )
    result = _llm_result(advice="观望", action="watch", decision_type="hold")
    result.technical_analysis = "均线结构改善，但仍需确认。"

    apply_canonical_decision_to_result(result, summary)

    assert result.technical_analysis == "均线结构改善，但仍需确认。"
    assert result.action == "watch"
    assert_canonical_consumer_consistency(result)

# ASSET_RESEARCH_BRIEF_PAYLOAD_V1_R002_TESTS
from types import SimpleNamespace as _AssetBriefPayloadNamespace
from src.services.factor_decision_summary import (
    _build_asset_research_brief_v1 as _asset_brief_payload_builder,
)


def test_asset_research_brief_payload_v1_is_daily_first_and_fail_closed():
    trend = _AssetBriefPayloadNamespace(
        current_price=10.5,
        support_levels=[10.0, 10.2],
        resistance_levels=[11.3],
    )
    summary = {
        "sections": {
            "trend": "\u8d8b\u52bf\uff1a\u5747\u7ebf\u7ed3\u6784\u504f\u5f3a",
            "volume_price": "\u91cf\u4ef7\uff1a\u7f29\u91cf\u56de\u8c03",
            "cost_structure": "\u6210\u672c/\u7b79\u7801\uff1a90%\u7b79\u7801\u96c6\u4e2d\u5ea6 12.00\uff0c\u83b7\u5229\u7b79\u7801\u7ea6 68%",
            "price_structure": "\u7ed3\u6784\uff1a\u4e3b\u8981\u652f\u6491\u4ecd\u6709\u6548",
            "valuation": "\u4f30\u503c\uff1a\u6570\u636e\u4e0d\u8db3",
        },
        "conclusion": "\u504f\u5f3a\uff0c\u7b49\u5f85\u786e\u8ba4\u3002",
        "action_condition": "\u653e\u91cf\u7ad9\u7a33\u538b\u529b\u4f4d\u518d\u5347\u7ea7\u3002",
        "invalidation_condition": "\u6709\u6548\u8dcc\u7834\u652f\u6491\u5219\u5931\u6548\u3002",
        "risks": ["\u7a81\u7834\u91cf\u80fd\u4ecd\u9700\u786e\u8ba4\u3002"],
        "canonical_decision": {
            "authority": "stock_trend_quality_pullback_v1",
            "action": "PASS",
            "public_action": "watch",
            "evidence_state": "PROVEN",
            "hard_veto": False,
            "reason_codes": [],
        },
    }

    brief = _asset_brief_payload_builder(trend, summary)

    assert brief["one_line_conclusion"] == summary["conclusion"]
    assert brief["canonical"] == summary["canonical_decision"]
    assert brief["coverage"] == {
        "monthly": "MISSING",
        "weekly": "MISSING",
        "daily": "PARTIAL_CURRENT",
        "60m": "MISSING",
        "30m": "MISSING",
        "15m": "MISSING",
        "5m": "MISSING",
    }
    assert "\u5f53\u524d\u4ef7 10.50 \u5143" in brief["fused_paragraph"]
    assert "90%\u7b79\u7801\u96c6\u4e2d\u5ea6 12.00" in brief["fused_paragraph"]
    assert "\u5f53\u524d\u8bc1\u636e\u8986\u76d6" in brief["coverage_text"]
    assert brief["historical_reference"]["available"] is False
    assert brief["current_probability"]["available"] is False
    assert brief["key_levels"]["support_label"] == "结构支撑"
    assert brief["key_levels"]["resistance_label"] == "结构压力"
    assert brief["timeframe_thesis"]["daily"]["role"] == "PRIMARY_SETUP"
    assert brief["short_term_execution_panel"] == {
        "status": "MISSING",
        "state": "DATA_INSUFFICIENT",
        "30m": {"status": "MISSING", "role": "PRIMARY_STRUCTURE"},
        "15m": {"status": "MISSING", "role": "TRIGGER_CONFIRMATION"},
        "5m": {"status": "MISSING", "role": "MICRO_TIMING"},
        "summary": None,
    }
    assert brief["scenario"]["preferred"]["status"] == "READY"
    assert brief["scenario"]["alternative"]["status"] == "MISSING"
    assert brief["scenario"]["invalidation"]["status"] == "READY"
    assert (
        brief["evidence_policy"]["legacy_signal_score_role"]
        == "REFERENCE_ONLY_NOT_CANONICAL_VOTE_COUNT"
    )
    assert (
        brief["evidence_policy"]["correlation_rule"]
        == "SAME_UNDERLYING_SWING_ONE_FAMILY_CONFIRMATION_OR_CONFLICT"
    )


def test_asset_research_brief_payload_v1_missing_values_are_not_invented():
    trend = _AssetBriefPayloadNamespace(
        current_price=None,
        support_levels=[],
        resistance_levels=[],
    )
    brief = _asset_brief_payload_builder(
        trend,
        {
            "sections": {},
            "conclusion": "\u6570\u636e\u4e0d\u8db3\uff0c\u6682\u4e0d\u5224\u65ad\u3002",
            "canonical_decision": {
                "authority": "stock_trend_quality_pullback_v1",
                "action": "WAIT",
                "public_action": "watch",
                "evidence_state": "UNKNOWN",
                "hard_veto": True,
                "reason_codes": ["DATA_INSUFFICIENT"],
            },
        },
    )

    assert brief["current_price"]["source_state"] == "MISSING"
    assert brief["key_levels"] == {
        "support": None,
        "resistance": None,
        "support_label": "结构支撑",
        "resistance_label": "结构压力",
    }
    assert brief["valuation"]["status"] == "MISSING"
    assert brief["historical_reference"]["available"] is False
    assert brief["historical_reference"].get("n") is None
    assert brief["current_probability"]["available"] is False
    assert brief["current_probability"].get("value") is None



def test_explanation_degradation_requires_proven_canonical_evidence_and_valid_brief():
    proven = build_stock_factor_decision_summary(_trend(), include_canonical=True)
    assert canonical_explanation_degradation_eligible(proven) is True

    unknown = build_stock_factor_decision_summary(None, include_canonical=True)
    assert canonical_explanation_degradation_eligible(unknown) is False

    missing_brief = dict(proven)
    missing_brief["investor_brief"] = None
    assert canonical_explanation_degradation_eligible(missing_brief) is False


def test_lower_timeframe_schema_cannot_override_higher_level_canonical_veto():
    summary = build_stock_factor_decision_summary(
        _trend(trend_status=_enum("空头排列")),
        include_canonical=True,
    )
    assert summary["canonical_decision"]["action"] == "PASS"
    brief = summary["investor_brief"]
    brief["short_term_execution_panel"] = {
        "status": "READY",
        "state": "TRIGGERED",
        "30m": {"status": "READY", "role": "PRIMARY_STRUCTURE"},
        "15m": {"status": "READY", "role": "TRIGGER_CONFIRMATION"},
        "5m": {"status": "READY", "role": "MICRO_TIMING"},
        "summary": "低周期转强，仅作时点。",
    }
    assert summary["canonical_decision"]["action"] == "PASS"
