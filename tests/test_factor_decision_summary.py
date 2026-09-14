from types import SimpleNamespace

import pandas as pd

from src.analyzer import AnalysisResult
from src.services.factor_decision_summary import (
    apply_canonical_decision_to_result,
    assert_canonical_consumer_consistency,
    build_stock_factor_decision_summary,
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
    assert "未显示明显高估" in summary["valuation"]
    assert "筹码平均成本参考约 10.00" in summary["cost_structure"]
    assert "主力" not in str(summary)


def test_high_static_valuation_is_reference_not_precise_intrinsic_value():
    summary = build_stock_factor_decision_summary(
        _trend(signal_score=68),
        fundamental_context={
            "market": "cn",
            "valuation": {"data": {"pe_ratio": 75.0, "pb_ratio": 9.0}},
        },
    )

    assert "静态估值偏高" in summary["valuation"]
    assert "仅作参考" in summary["valuation"]
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
    }
    assert "\u5f53\u524d\u4ef7 10.50 \u5143" in brief["fused_paragraph"]
    assert brief["historical_reference"]["available"] is False
    assert brief["current_probability"]["available"] is False


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
    assert brief["key_levels"] == {"support": None, "resistance": None}
    assert brief["valuation"]["status"] == "MISSING"
    assert brief["historical_reference"]["available"] is False
    assert brief["historical_reference"].get("n") is None
    assert brief["current_probability"]["available"] is False
    assert brief["current_probability"].get("value") is None
