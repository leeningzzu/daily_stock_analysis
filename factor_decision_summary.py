# -*- coding: utf-8 -*-
"""Thin deterministic stock factor-decision summary.

This module intentionally reuses the existing ``TrendAnalysisResult`` score and
observable context.  It does not create a second screening engine, invent a win
rate, or convert a heuristic score into a probability.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple


_STRONG_TRENDS = {"强势多头", "多头排列"}
_WEAK_TRENDS = {"空头排列", "强势空头"}


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip()


def _safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _nearest_levels(trend_result: Any) -> Tuple[Optional[float], Optional[float]]:
    price = _safe_float(getattr(trend_result, "current_price", None))
    supports = [
        item
        for item in (_safe_float(v) for v in (getattr(trend_result, "support_levels", None) or []))
        if item is not None and item > 0
    ]
    resistances = [
        item
        for item in (_safe_float(v) for v in (getattr(trend_result, "resistance_levels", None) or []))
        if item is not None and item > 0
    ]
    if price is None or price <= 0:
        return (max(supports) if supports else None, min(resistances) if resistances else None)

    valid_supports = [v for v in supports if v <= price * 1.02]
    valid_resistances = [v for v in resistances if v >= price * 0.98]
    support = max(valid_supports) if valid_supports else (max(supports) if supports else None)
    resistance = min(valid_resistances) if valid_resistances else (min(resistances) if resistances else None)
    return support, resistance


def _format_price(value: Optional[float]) -> str:
    return f"{value:.2f}" if value is not None and value > 0 else ""


def _valuation_summary(fundamental_context: Any) -> str:
    if not isinstance(fundamental_context, dict):
        return "估值：数据不足，暂不判断高低。"

    market = str(fundamental_context.get("market") or "").lower()
    block = fundamental_context.get("valuation")
    if not isinstance(block, dict):
        return "估值：数据不足，暂不判断高低。"
    data = block.get("data")
    if not isinstance(data, dict):
        return "估值：数据不足，暂不判断高低。"

    pe = _safe_float(data.get("pe_ratio"))
    pb = _safe_float(data.get("pb_ratio"))
    parts: List[str] = []
    if pe is not None and pe > 0:
        parts.append(f"PE {pe:.1f}")
    if pb is not None and pb > 0:
        parts.append(f"PB {pb:.1f}")
    metrics = "、".join(parts)

    if pe is None and pb is None:
        return "估值：数据不足，暂不判断高低。"
    if market and market != "cn":
        suffix = f"（{metrics}）" if metrics else ""
        return f"估值：已取得基础估值数据{suffix}，首版不使用统一阈值跨市场判断高低。"

    high = (pe is not None and pe > 60) or (pb is not None and pb > 8)
    moderate = (
        pe is not None
        and pe > 0
        and pe <= 25
        and pb is not None
        and pb > 0
        and pb <= 4
    )
    suffix = f"（{metrics}）" if metrics else ""
    if high:
        return f"估值：静态估值偏高{suffix}，需要更强的盈利增长兑现；仅作参考。"
    if moderate:
        return f"估值：当前 PE/PB 未显示明显高估{suffix}；是否低估仍需结合历史分位和同行。"
    return f"估值：当前静态估值大致处于中间区域{suffix}；需结合历史分位和同行。"


def _cost_structure_summary(chip_data: Any) -> str:
    if chip_data is None:
        return "成本/筹码：数据不足。"

    avg_cost = _safe_float(getattr(chip_data, "avg_cost", None))
    concentration = _safe_float(getattr(chip_data, "concentration_90", None))
    profit_ratio = _safe_float(getattr(chip_data, "profit_ratio", None))
    parts: List[str] = []
    if avg_cost is not None and avg_cost > 0:
        parts.append(f"筹码平均成本参考约 {_format_price(avg_cost)}")
    if concentration is not None and concentration > 0:
        parts.append(f"90%筹码集中度 {concentration:.2f}")
    if profit_ratio is not None and 0 < profit_ratio <= 1:
        parts.append(f"获利筹码约 {profit_ratio * 100:.0f}%")
    if not parts:
        return "成本/筹码：数据不足。"
    return "成本/筹码：" + "；".join(parts) + "。"


def _volume_price_summary(trend_result: Any) -> str:
    status = _enum_value(getattr(trend_result, "volume_status", None))
    ratio = _safe_float(getattr(trend_result, "volume_ratio_5d", None))
    ratio_text = f"，当日量约为5日均量的 {ratio:.2f} 倍" if ratio is not None and ratio > 0 else ""
    if status == "缩量回调":
        return f"量价：缩量回调，卖压有所收缩{ratio_text}；可视为洗盘候选特征，但仍需支撑与后续转强确认。"
    if status == "放量下跌":
        return f"量价：放量下跌{ratio_text}，供应压力增加，不能解释为洗盘。"
    if status == "放量上涨":
        return f"量价：放量上涨{ratio_text}，需求增强，但仍需观察后续承接。"
    if status == "缩量上涨":
        return f"量价：缩量上涨{ratio_text}，上行动能不足。"
    text = str(getattr(trend_result, "volume_trend", None) or "量能正常").strip()
    return f"量价：{text}{ratio_text}。"


def _trend_summary(trend_result: Any) -> str:
    status = _enum_value(getattr(trend_result, "trend_status", None)) or "趋势不明"
    alignment = str(getattr(trend_result, "ma_alignment", None) or "").strip()
    strength = _safe_float(getattr(trend_result, "trend_strength", None))
    parts = [status]
    if alignment:
        parts.append(alignment)
    if strength is not None:
        parts.append(f"趋势强度 {strength:.0f}/100")
    return "趋势：" + "；".join(parts) + "。"


def _structure_summary(trend_result: Any) -> str:
    support, resistance = _nearest_levels(trend_result)
    if support is not None and resistance is not None:
        return f"结构：主要支撑参考 {_format_price(support)}，上方压力参考 {_format_price(resistance)}。"
    if support is not None:
        return f"结构：主要支撑参考 {_format_price(support)}，上方压力暂未形成可靠数值。"
    if resistance is not None:
        return f"结构：上方压力参考 {_format_price(resistance)}，下方支撑暂未形成可靠数值。"
    return "结构：当前可用数据不足以给出可靠支撑/压力参考。"


def _momentum_summary(trend_result: Any) -> str:
    macd = str(getattr(trend_result, "macd_signal", None) or "").strip()
    rsi = str(getattr(trend_result, "rsi_signal", None) or "").strip()
    pieces = [p for p in (macd, rsi) if p and p != "数据不足"]
    if not pieces:
        return "动量：数据不足。"
    return "动量：" + "；".join(pieces) + "。"


def _conclusion(trend_result: Any, score: int) -> str:
    trend = _enum_value(getattr(trend_result, "trend_status", None))
    signal = _enum_value(getattr(trend_result, "buy_signal", None))
    volume = _enum_value(getattr(trend_result, "volume_status", None))

    if volume == "放量下跌" or trend in _WEAK_TRENDS:
        return "偏弱或风险升高，当前不适合新增仓位；优先等待趋势和量价修复。"
    if score >= 80 and (trend in _STRONG_TRENDS or signal in {"强烈买入", "买入"}):
        return "偏强，值得关注；当前更适合等待回踩或量价确认，不建议追高。"
    if score >= 65:
        return "中性偏强，已有一定趋势与量价基础；等待结构确认后再行动。"
    if score >= 50:
        return "中性，现有信号仍不充分；以观察和等待确认为主。"
    return "偏弱，当前不适合新增仓位；优先等待趋势修复。"


def _risk_notes(trend_result: Any) -> List[str]:
    risks: List[str] = []
    for raw in getattr(trend_result, "risk_factors", None) or []:
        text = str(raw or "").strip()
        if not text:
            continue
        text = text.replace("❌ ", "").replace("⚠️ ", "").replace("⚠ ", "")
        if "主力" in text:
            text = text.replace("主力洗盘", "洗盘候选")
        if text not in risks:
            risks.append(text)
        if len(risks) >= 3:
            break
    return risks or ["需继续关注市场环境、行业变化及关键支撑失效风险。"]


def build_stock_factor_decision_summary(
    trend_result: Any,
    *,
    fundamental_context: Optional[Dict[str, Any]] = None,
    chip_data: Any = None,
) -> Dict[str, Any]:
    """Build a deterministic, human-readable stock summary for report rendering.

    The 0-100 score is the existing ``StockTrendAnalyzer.signal_score``.  The
    function deliberately leaves historical win rate and current opportunity
    probability unavailable until the exact same strategy/outcome contract is
    actually bound and calibrated.
    """

    if trend_result is None:
        raise ValueError("trend_result is required")

    raw_score = _safe_float(getattr(trend_result, "signal_score", 0))
    score = int(max(0, min(100, round(raw_score or 0))))
    support, _ = _nearest_levels(trend_result)

    valuation = _valuation_summary(fundamental_context)
    cost_structure = _cost_structure_summary(chip_data)
    sections = {
        "trend": _trend_summary(trend_result),
        "volume_price": _volume_price_summary(trend_result),
        "price_structure": _structure_summary(trend_result),
        "momentum": _momentum_summary(trend_result),
        "cost_structure": cost_structure,
        "valuation": valuation,
    }

    why: List[str] = []
    for key in ("trend", "volume_price", "price_structure", "valuation"):
        text = sections[key]
        if text not in why:
            why.append(text)

    if support is not None:
        action_condition = (
            f"若价格在主要支撑 {_format_price(support)} 上方企稳，并出现量价重新转强，可升级为买入候选。"
        )
        invalidation_condition = (
            f"若放量有效跌破主要支撑 {_format_price(support)}，则取消原判断。"
        )
    else:
        action_condition = "若回调后止跌并出现量价重新转强，可重新评估买入条件。"
        invalidation_condition = "若趋势转弱并伴随放量下跌，则取消原判断。"

    return {
        "strategy_id": "stock_trend_quality_pullback_v1",
        "contract_version": "1.0",
        "composite_score": score,
        "score_note": "沿用现有确定性技术规则，用于排序和解释，不代表胜率或概率。",
        "historical_reference": {
            "available": False,
            "display": "样本不足，暂不展示",
        },
        "current_probability": {
            "available": False,
            "display": "暂不提供（尚未完成独立校准）",
        },
        "conclusion": _conclusion(trend_result, score),
        "why": why[:4],
        "action_condition": action_condition,
        "invalidation_condition": invalidation_condition,
        "valuation": valuation,
        "cost_structure": cost_structure,
        "risk_notes": _risk_notes(trend_result),
        "sections": sections,
    }
