from types import SimpleNamespace

import pandas as pd

from src.services.factor_decision_summary import build_stock_factor_decision_summary
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
