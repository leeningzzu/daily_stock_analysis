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
_CANONICAL_WEAK_TRENDS = {"弱势多头", "弱势空头", "空头排列", "强势空头"}
_NEGATIVE_SIGNALS = {"卖出", "强烈卖出"}
_HARD_RISK_HINTS = ("重大利空", "重大风险", "退市", "放量跌破", "跌破关键支撑")
_MISSING_EVIDENCE_HINTS = ("数据不足", "无法完成分析", "无法判断")
_CANONICAL_AUTHORITY = "stock_trend_quality_pullback_v1"
_MARKET_SECTOR_REGIME_VERSION = "market-sector-regime-v1"
_TREND_RELATIVE_STRENGTH_VERSION = "trend-relative-strength-v1"
_SUPPLY_DEMAND_VOLUME_PRICE_VERSION = "supply-demand-volume-price-v1"


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

    suffix = f"（{metrics}）" if metrics else ""
    if market and market != "cn":
        return (
            f"估值：已取得基础估值数据{suffix}；"
            "缺少可靠历史分位和同行比较，首版暂不判断偏低、合理或偏高。"
        )
    return (
        f"估值：已取得基础估值数据{suffix}；"
        "缺少可靠历史分位和同行比较，暂不判断偏低、合理或偏高。"
    )


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


def _mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _market_light_mapping(daily_market_context: Any) -> Dict[str, Any]:
    if isinstance(daily_market_context, dict):
        return _mapping(daily_market_context.get("market_light"))
    return _mapping(getattr(daily_market_context, "market_light", None))


def _build_market_sector_regime_evidence(
    daily_market_context: Any,
    market_structure_context: Any,
) -> Dict[str, Any]:
    """Compose existing deterministic market/sector artifacts without refetching."""
    market_light = _market_light_mapping(daily_market_context)
    market_status = str(market_light.get("status") or "").strip().lower()
    market_quality = str(market_light.get("data_quality") or "").strip().lower()
    reason_codes: List[str] = []

    if market_status not in {"red", "yellow", "green"}:
        market_state = "UNKNOWN"
        market_evidence_state = "UNKNOWN"
    elif market_quality == "partial":
        market_state = "CAUTION" if market_status in {"red", "yellow"} else "PERMISSIVE"
        market_evidence_state = "PARTIAL"
        reason_codes.append(f"MARKET_REGIME_{market_status.upper()}_PARTIAL")
    elif market_quality == "ok":
        market_state = {"red": "RISK_OFF", "yellow": "CAUTION", "green": "PERMISSIVE"}[market_status]
        market_evidence_state = "READY"
        reason_codes.append(f"MARKET_REGIME_{market_status.upper()}")
    else:
        market_state = "UNKNOWN"
        market_evidence_state = "UNKNOWN"

    structure = _mapping(market_structure_context)
    structure_status = str(structure.get("status") or "").strip().lower()
    stock_position = _mapping(structure.get("stock_market_position"))
    primary_theme = _mapping(stock_position.get("primary_theme"))
    theme_phase = str(stock_position.get("theme_phase") or primary_theme.get("phase") or "").strip().lower()
    stock_role = str(stock_position.get("stock_role") or "").strip().lower()
    risk_tags = [
        str(_mapping(item).get("code"))
        for item in (stock_position.get("risk_tags") or [])
        if _mapping(item).get("code")
    ]

    if structure_status == "not_supported":
        sector_state = "NOT_SUPPORTED"
        sector_evidence_state = "NOT_SUPPORTED"
    elif theme_phase == "cooling":
        sector_state = "COOLING"
        sector_evidence_state = "READY" if structure_status == "ok" else "PARTIAL"
        reason_codes.append("SECTOR_THEME_COOLING")
    elif theme_phase in {"warming", "accelerating"}:
        sector_state = "SUPPORTIVE"
        sector_evidence_state = "READY" if structure_status == "ok" else "PARTIAL"
        reason_codes.append(f"SECTOR_THEME_{theme_phase.upper()}")
    elif structure_status in {"ok", "partial"}:
        sector_state = "UNKNOWN"
        sector_evidence_state = "PARTIAL"
    else:
        sector_state = "UNKNOWN"
        sector_evidence_state = "UNKNOWN"

    component_states = {market_evidence_state, sector_evidence_state}
    if "READY" in component_states and component_states <= {"READY", "NOT_SUPPORTED"}:
        evidence_state = "READY"
    elif component_states & {"READY", "PARTIAL"}:
        evidence_state = "PARTIAL"
    else:
        evidence_state = "UNKNOWN"

    hard_veto = market_state == "RISK_OFF" and market_evidence_state == "READY"
    return {
        "family": "market_sector_regime",
        "version": _MARKET_SECTOR_REGIME_VERSION,
        "evidence_state": evidence_state,
        "hard_veto": hard_veto,
        "veto_codes": ["MARKET_REGIME_RISK_OFF"] if hard_veto else [],
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "market": {
            "state": market_state,
            "status": market_status or None,
            "score": _safe_float(market_light.get("score")),
            "data_quality": market_quality or None,
        },
        "sector": {
            "state": sector_state,
            "status": structure_status or None,
            "primary_theme": primary_theme.get("name"),
            "theme_phase": theme_phase or None,
            "stock_role": stock_role or None,
            "risk_tags": risk_tags,
        },
    }


def _market_sector_regime_summary(evidence: Dict[str, Any]) -> str:
    market = _mapping(evidence.get("market"))
    sector = _mapping(evidence.get("sector"))
    if evidence.get("evidence_state") == "UNKNOWN":
        return "市场/板块：确定性证据不足，暂不据此升级或否决。"
    parts = [f"市场状态 {market.get('state', 'UNKNOWN')}"]
    if sector.get("primary_theme"):
        parts.append(f"主关联板块 {sector['primary_theme']}（{sector.get('theme_phase') or 'unknown'}）")
    elif sector.get("state") == "NOT_SUPPORTED":
        parts.append("板块证据当前不支持")
    return "市场/板块：" + "；".join(parts) + "。"


def _build_trend_relative_strength_evidence(
    trend_result: Any,
    relative_strength_context: Any,
) -> Dict[str, Any]:
    """Compose completed daily trend evidence with an optional exact-date RS context."""
    trend_status = _enum_value(getattr(trend_result, "trend_status", None))
    alignment = str(getattr(trend_result, "ma_alignment", None) or "").strip()
    strength = _safe_float(getattr(trend_result, "trend_strength", None))
    rs = _mapping(relative_strength_context)
    rs_status = str(rs.get("status") or "").strip().upper()

    if not trend_status:
        evidence_state = "UNKNOWN"
    elif rs_status == "READY":
        evidence_state = "READY"
    else:
        evidence_state = "PARTIAL"

    return {
        "family": "trend_relative_strength",
        "version": _TREND_RELATIVE_STRENGTH_VERSION,
        "evidence_state": evidence_state,
        "absolute_trend": {
            "status": trend_status or None,
            "alignment": alignment or None,
            "strength": strength,
        },
        "relative_strength": rs or {
            "status": "MISSING",
            "reason": "RELATIVE_STRENGTH_CONTEXT_MISSING",
        },
        "hard_veto": False,
        "veto_codes": [],
    }


def _trend_relative_strength_summary(evidence: Dict[str, Any]) -> str:
    absolute = _mapping(evidence.get("absolute_trend"))
    relative = _mapping(evidence.get("relative_strength"))
    trend_text = str(absolute.get("status") or "趋势不明")
    if str(relative.get("status") or "").upper() != "READY":
        return f"趋势/相对强弱：{trend_text}；基准相对强弱证据尚未 READY。"
    benchmark = _mapping(relative.get("benchmark"))
    rel = _mapping(relative.get("relative"))
    ratio = _safe_float(rel.get("relative_ratio_change_pct"))
    ratio_text = f"{ratio:+.2f}%" if ratio is not None else "未知"
    return (
        f"趋势/相对强弱：{trend_text}；相对{benchmark.get('name') or benchmark.get('code') or '基准'}"
        f"的 {relative.get('horizon_sessions', 60)} 个交易时段比率变化 {ratio_text}，"
        f"状态 {rel.get('state') or 'UNKNOWN'}。"
    )


def _build_supply_demand_volume_price_evidence(
    trend_result: Any,
    supply_demand_context: Any,
) -> Dict[str, Any]:
    """Compose completed-bar supply/demand evidence without adding action authority."""
    context = _mapping(supply_demand_context)
    context_status = str(context.get("status") or "").strip().upper()
    legacy_status = _enum_value(getattr(trend_result, "volume_status", None))
    legacy_ratio = _safe_float(getattr(trend_result, "volume_ratio_5d", None))

    if context_status == "READY" and legacy_status:
        evidence_state = "READY"
    elif context_status in {"READY", "PARTIAL"} or legacy_status:
        evidence_state = "PARTIAL"
    else:
        evidence_state = "UNKNOWN"

    return {
        "family": "supply_demand_volume_price",
        "version": _SUPPLY_DEMAND_VOLUME_PRICE_VERSION,
        "evidence_state": evidence_state,
        "observable_only": True,
        "institutional_intent_inferred": False,
        "legacy_volume": {
            "status": legacy_status or None,
            "volume_ratio_5d": legacy_ratio,
            "correlation_group": "relative_volume",
        },
        "completed_bar_context": context or {
            "status": "MISSING",
            "reason": "SUPPLY_DEMAND_CONTEXT_MISSING",
        },
        "hard_veto": False,
        "veto_codes": [],
        "authority_note": "Existing HEAVY_VOLUME_DOWN canonical veto remains owned by legacy volume_status; this family does not add a second veto.",
    }


def _supply_demand_volume_price_summary(evidence: Dict[str, Any]) -> str:
    context = _mapping(evidence.get("completed_bar_context"))
    status = str(context.get("status") or "").upper()
    if status not in {"READY", "PARTIAL"}:
        return "供需/量价：completed OHLCV 证据不足，暂不推断供需状态。"
    relative = _mapping(context.get("relative_volume"))
    directional = _mapping(context.get("directional_volume"))
    close_location = _mapping(context.get("close_location_flow"))
    ratio = _safe_float(relative.get("volume_ratio_20d"))
    balance = _safe_float(directional.get("signed_volume_balance"))
    cmf = _safe_float(close_location.get("cmf_20"))
    state_label = {
        "DEMAND_PRESSURE": "需求压力占优",
        "SUPPLY_PRESSURE": "供应压力占优",
        "BALANCED": "供需大致平衡",
        "CONFLICT": "量价信号冲突",
    }.get(str(context.get("state") or "").upper(), "供需状态不明")
    parts = [state_label]
    if ratio is not None:
        parts.append(f"20日相对量 {ratio:.2f}x")
    if balance is not None:
        parts.append(f"方向量能平衡 {balance:+.2f}")
    if cmf is not None:
        parts.append(f"CMF20 {cmf:+.2f}")
    if status == "PARTIAL":
        parts.append("数据源一致性未充分证明")
    return "供需/量价：" + "；".join(parts) + "；仅描述可观察压力，不推断机构意图。"


def _canonical_decision(
    trend_result: Any,
    market_sector_regime: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Produce the conservative P0 WAIT/PASS decision from deterministic inputs."""
    reason_codes: List[str] = []
    if trend_result is None:
        reason_codes.append("MISSING_TREND_RESULT")
        return {
            "authority": _CANONICAL_AUTHORITY,
            "action": "WAIT",
            "public_action": "watch",
            "evidence_state": "UNKNOWN",
            "hard_veto": False,
            "reason_codes": reason_codes,
        }

    trend = _enum_value(getattr(trend_result, "trend_status", None))
    signal = _enum_value(getattr(trend_result, "buy_signal", None))
    volume = _enum_value(getattr(trend_result, "volume_status", None))
    score = _safe_float(getattr(trend_result, "signal_score", None))
    risk_factors = [
        str(item or "").strip()
        for item in (getattr(trend_result, "risk_factors", None) or [])
        if str(item or "").strip()
    ]

    if not trend:
        reason_codes.append("MISSING_TREND_STATE")
    if not signal:
        reason_codes.append("MISSING_BUY_SIGNAL")
    if not volume:
        reason_codes.append("MISSING_VOLUME_STATE")
    if score is None:
        reason_codes.append("MISSING_SIGNAL_SCORE")
    if any(any(hint in text for hint in _MISSING_EVIDENCE_HINTS) for text in risk_factors):
        reason_codes.append("REQUIRED_EVIDENCE_UNAVAILABLE")

    if reason_codes:
        return {
            "authority": _CANONICAL_AUTHORITY,
            "action": "WAIT",
            "public_action": "watch",
            "evidence_state": "UNKNOWN",
            "hard_veto": False,
            "reason_codes": reason_codes,
        }

    hard_veto_codes: List[str] = []
    if volume == "放量下跌":
        hard_veto_codes.append("HEAVY_VOLUME_DOWN")
    if trend in _CANONICAL_WEAK_TRENDS:
        hard_veto_codes.append("WEAK_TREND")
    if signal in _NEGATIVE_SIGNALS:
        hard_veto_codes.append("NEGATIVE_TREND_SIGNAL")
    if any(
        text.startswith("❌") or any(hint in text for hint in _HARD_RISK_HINTS)
        for text in risk_factors
    ):
        hard_veto_codes.append("DETERMINISTIC_HARD_RISK")

    if isinstance(market_sector_regime, dict) and market_sector_regime.get("hard_veto"):
        hard_veto_codes.extend(
            str(code)
            for code in (market_sector_regime.get("veto_codes") or [])
            if str(code).strip()
        )

    if hard_veto_codes:
        return {
            "authority": _CANONICAL_AUTHORITY,
            "action": "PASS",
            "public_action": "avoid",
            "evidence_state": "PROVEN",
            "hard_veto": True,
            "reason_codes": list(dict.fromkeys(hard_veto_codes)),
        }

    return {
        "authority": _CANONICAL_AUTHORITY,
        "action": "WAIT",
        "public_action": "watch",
        "evidence_state": "PROVEN",
        "hard_veto": False,
        "reason_codes": ["CONDITIONAL_OBSERVATION_ONLY"],
    }


def _canonical_public_text(summary: Dict[str, Any]) -> Dict[str, str]:
    decision = summary["canonical_decision"]
    if decision["evidence_state"] == "UNKNOWN":
        return {
            "label": "观望",
            "advice": "观望：必需证据不足，暂不采取买卖动作。",
            "signal_type": "🟡数据不足 / 观望",
            "no_position": "不新增仓位；等待必需趋势、评分与量价证据完整。",
            "has_position": "不由本次 P0 结论推导买卖动作；按既有风险计划管理。",
        }
    if decision["action"] == "PASS":
        return {
            "label": "回避",
            "advice": "回避：确定性风险或弱势条件尚未解除。",
            "signal_type": "⚠️风险否决 / 回避",
            "no_position": "不新增仓位；等待风险或弱势条件解除后再评估。",
            "has_position": "不由本次 P0 结论生成卖出指令；按既有风险计划处理。",
        }
    return {
        "label": "观望",
        "advice": "观望：仅保留条件化观察，等待操作条件成立。",
        "signal_type": "🟡条件观察 / 观望",
        "no_position": "等待操作条件成立后再评估，不追高、不抢跑。",
        "has_position": "本次 P0 不生成加减仓指令；按既有风险计划管理。",
    }


def apply_canonical_decision_to_result(result: Any, summary: Dict[str, Any]) -> Any:
    """Make the deterministic P0 decision the sole public action authority."""
    decision = summary.get("canonical_decision") if isinstance(summary, dict) else None
    if not isinstance(decision, dict):
        raise ValueError("factor_decision.canonical_decision is required")
    if decision.get("action") not in {"WAIT", "PASS"}:
        raise ValueError("P0 canonical action must be WAIT or PASS")
    if decision.get("public_action") not in {"watch", "avoid"}:
        raise ValueError("P0 canonical public_action must be watch or avoid")

    text = _canonical_public_text(summary)
    conclusion = str(summary.get("conclusion") or text["advice"]).strip()
    reason_codes = [str(item) for item in decision.get("reason_codes") or []]
    reason_text = "、".join(reason_codes) or "CONDITIONAL_OBSERVATION_ONLY"

    result.action = decision["public_action"]
    result.action_label = text["label"]
    result.operation_advice = text["advice"]
    result.decision_type = "hold"
    result.analysis_summary = conclusion
    result.buy_reason = f"确定性 P0 权威：{reason_text}；不构成买入或卖出指令。"

    dashboard = result.dashboard if isinstance(getattr(result, "dashboard", None), dict) else {}
    result.dashboard = dashboard
    dashboard["action"] = result.action
    dashboard["action_label"] = result.action_label
    dashboard["operation_advice"] = result.operation_advice
    dashboard["decision_type"] = result.decision_type
    dashboard["analysis_summary"] = result.analysis_summary
    dashboard["buy_reason"] = result.buy_reason
    dashboard["factor_decision"] = summary

    dashboard["core_conclusion"] = {
        "one_sentence": conclusion,
        "signal_type": text["signal_type"],
        "time_sensitivity": "不急；等待确定性条件或既有风险计划触发",
        "position_advice": {
            "no_position": text["no_position"],
            "has_position": text["has_position"],
        },
    }

    dashboard["phase_decision"] = {
        "action_window": "P0 有界验收：仅观察，不执行买卖动作",
        "immediate_action": text["advice"],
        "watch_conditions": [
            summary.get("action_condition", "等待操作条件完整"),
            summary.get("invalidation_condition", "风险条件触发则继续回避"),
        ],
        "next_check_time": "下一次具备完整确定性证据时",
        "confidence_reason": (
            "必需证据不足，P0 按 UNKNOWN 失败关闭。"
            if decision.get("evidence_state") == "UNKNOWN"
            else "P0 仅依据确定性趋势、评分、量价与硬风险规则。"
        ),
        "data_limitations": (
            ["必需证据不完整；LLM 仅作解释，不拥有动作权限。"]
            if decision.get("evidence_state") == "UNKNOWN"
            else ["P0 只允许 WAIT/PASS；不生成 BUY/HOLD/EXIT。"]
        ),
    }

    dashboard["strategy_synthesis"] = {
        "authority": _CANONICAL_AUTHORITY,
        "canonical_public_action": decision["public_action"],
        "final_signal": "hold",
        "consensus_level": (
            "insufficient" if decision.get("evidence_state") == "UNKNOWN" else "medium"
        ),
        "conflict_severity": "none",
        "conflict_count": 0,
        "confidence": 0.0 if decision.get("evidence_state") == "UNKNOWN" else 1.0,
        "supporting_skills": [],
        "opposing_skills": [],
        "conflicts": [],
        "summary_params": {"opinion_count": 1, "invalid_opinion_count": 0},
    }

    not_applicable = "P0 不生成买卖点；以确定性 WAIT/PASS 为准"
    dashboard["battle_plan"] = {
        "sniper_points": {
            "ideal_buy": not_applicable,
            "secondary_buy": not_applicable,
            "stop_loss": "P0 不生成新止损位；沿用既有风险计划",
            "take_profit": "P0 不生成新止盈位；沿用既有风险计划",
        },
        "position_strategy": {
            "suggested_position": "不新增仓位",
            "entry_plan": summary.get("action_condition", not_applicable),
            "risk_control": summary.get("invalidation_condition", not_applicable),
        },
        "action_checklist": [
            f"当前确定性动作：{decision['action']} / {text['label']}",
            f"操作条件：{summary.get('action_condition', '待补充')}",
            f"失效条件：{summary.get('invalidation_condition', '待补充')}",
        ],
    }

    calibration = dashboard.get("decision_score_calibration")
    calibration = dict(calibration) if isinstance(calibration, dict) else {}
    calibration["final_action"] = decision["public_action"]
    calibration["guardrail_reason"] = f"p0_canonical:{reason_text}"
    dashboard["decision_score_calibration"] = calibration
    stability = dashboard.get("decision_stability")
    if isinstance(stability, dict):
        stability = dict(stability)
        stability["final_action"] = decision["public_action"]
        stability["reason"] = f"p0_canonical:{reason_text}"
        dashboard["decision_stability"] = stability
    return result


def assert_canonical_consumer_consistency(result: Any) -> None:
    """Fail closed if any public action slot diverges from canonical P0 output."""
    dashboard = result.dashboard if isinstance(getattr(result, "dashboard", None), dict) else {}
    summary = dashboard.get("factor_decision")
    decision = summary.get("canonical_decision") if isinstance(summary, dict) else None
    if not isinstance(decision, dict):
        raise ValueError("canonical decision missing from result")
    text = _canonical_public_text(summary)
    expected = {
        "result.action": (getattr(result, "action", None), decision["public_action"]),
        "result.action_label": (getattr(result, "action_label", None), text["label"]),
        "result.operation_advice": (getattr(result, "operation_advice", None), text["advice"]),
        "result.decision_type": (getattr(result, "decision_type", None), "hold"),
        "result.analysis_summary": (getattr(result, "analysis_summary", None), summary["conclusion"]),
        "result.buy_reason": (
            getattr(result, "buy_reason", None),
            f"确定性 P0 权威：{'、'.join(str(item) for item in decision.get('reason_codes') or []) or 'CONDITIONAL_OBSERVATION_ONLY'}；不构成买入或卖出指令。",
        ),
        "dashboard.action": (dashboard.get("action"), decision["public_action"]),
        "dashboard.action_label": (dashboard.get("action_label"), text["label"]),
        "dashboard.operation_advice": (dashboard.get("operation_advice"), text["advice"]),
        "dashboard.decision_type": (dashboard.get("decision_type"), "hold"),
        "dashboard.analysis_summary": (dashboard.get("analysis_summary"), summary["conclusion"]),
        "dashboard.buy_reason": (dashboard.get("buy_reason"), getattr(result, "buy_reason", None)),
    }
    core = dashboard.get("core_conclusion") or {}
    phase = dashboard.get("phase_decision") or {}
    strategy = dashboard.get("strategy_synthesis") or {}
    battle = dashboard.get("battle_plan") or {}
    expected.update(
        {
            "dashboard.core_conclusion.one_sentence": (core.get("one_sentence"), summary["conclusion"]),
            "dashboard.core_conclusion.signal_type": (core.get("signal_type"), text["signal_type"]),
            "dashboard.core_conclusion.position_advice": (
                core.get("position_advice"),
                {"no_position": text["no_position"], "has_position": text["has_position"]},
            ),
            "dashboard.phase_decision.immediate_action": (phase.get("immediate_action"), text["advice"]),
            "dashboard.strategy_synthesis.final_signal": (strategy.get("final_signal"), "hold"),
            "dashboard.strategy_synthesis.canonical_public_action": (
                strategy.get("canonical_public_action"), decision["public_action"]
            ),
            "dashboard.decision_score_calibration.final_action": (
                (dashboard.get("decision_score_calibration") or {}).get("final_action"),
                decision["public_action"],
            ),
        }
    )
    stability = dashboard.get("decision_stability")
    if isinstance(stability, dict):
        expected["dashboard.decision_stability.final_action"] = (
            stability.get("final_action"),
            decision["public_action"],
        )
    not_applicable = "P0 不生成买卖点；以确定性 WAIT/PASS 为准"
    expected["dashboard.battle_plan.sniper_points"] = (
        battle.get("sniper_points") if isinstance(battle, dict) else None,
        {
            "ideal_buy": not_applicable,
            "secondary_buy": not_applicable,
            "stop_loss": "P0 不生成新止损位；沿用既有风险计划",
            "take_profit": "P0 不生成新止盈位；沿用既有风险计划",
        },
    )
    expected["dashboard.battle_plan.position_strategy"] = (
        battle.get("position_strategy") if isinstance(battle, dict) else None,
        {
            "suggested_position": "不新增仓位",
            "entry_plan": summary.get("action_condition", not_applicable),
            "risk_control": summary.get("invalidation_condition", not_applicable),
        },
    )
    expected["dashboard.battle_plan.action_checklist"] = (
        battle.get("action_checklist") if isinstance(battle, dict) else None,
        [
            f"当前确定性动作：{decision['action']} / {text['label']}",
            f"操作条件：{summary.get('action_condition', '待补充')}",
            f"失效条件：{summary.get('invalidation_condition', '待补充')}",
        ],
    )
    conflicts = [name for name, (actual, wanted) in expected.items() if actual != wanted]
    if conflicts:
        raise ValueError("canonical consumer conflict: " + ", ".join(conflicts))


def canonical_explanation_degradation_eligible(summary: Any) -> bool:
    """Return True only when P0 deterministic evidence can stand without LLM prose."""
    if not isinstance(summary, dict):
        return False
    decision = summary.get("canonical_decision")
    brief = summary.get("investor_brief")
    if not isinstance(decision, dict) or not isinstance(brief, dict):
        return False
    if decision.get("authority") != _CANONICAL_AUTHORITY:
        return False
    if decision.get("evidence_state") != "PROVEN":
        return False
    if decision.get("action") not in {"WAIT", "PASS"}:
        return False
    if decision.get("public_action") not in {"watch", "avoid"}:
        return False
    if _safe_float(summary.get("composite_score")) is None:
        return False
    if brief.get("schema_version") != "investor-brief-v1":
        return False
    if not str(brief.get("one_line_conclusion") or "").strip():
        return False
    if not str(brief.get("fused_paragraph") or "").strip():
        return False
    return True


# ASSET_RESEARCH_BRIEF_PAYLOAD_V1_R003
def _asset_brief_v1_num(value):
    try:
        if value is None:
            return None
        number = float(value)
        return number if number == number else None
    except (TypeError, ValueError):
        return None


def _asset_brief_v1_price(value):
    number = _asset_brief_v1_num(value)
    if number is None or number <= 0:
        return None
    return f"{number:.2f}"


def _asset_brief_v1_text(value, prefixes=()):
    text = str(value or "").strip()
    for prefix in prefixes:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    return text.rstrip(" \u3002\uff1b;")


def _asset_brief_v1_usable(value):
    text = str(value or "").strip()
    if not text:
        return False
    blockers = (
        "\u6570\u636e\u4e0d\u8db3",
        "\u6682\u4e0d\u5224\u65ad",
        "\u672a\u5f62\u6210\u53ef\u9760",
        "\u5c1a\u672a\u5f62\u6210\u53ef\u9760",
    )
    return not any(blocker in text for blocker in blockers)


def _build_asset_research_brief_v1(trend_result, summary):
    summary = summary if isinstance(summary, dict) else {}
    sections = summary.get("sections")
    sections = sections if isinstance(sections, dict) else {}

    conclusion = str(summary.get("conclusion") or "").strip()
    trigger = str(summary.get("action_condition") or "").strip()
    invalidation = str(summary.get("invalidation_condition") or "").strip()

    risks = summary.get("risk_notes")
    if not isinstance(risks, list):
        risks = summary.get("risks")
    if not isinstance(risks, list):
        risks = []
    risks = [str(x).strip() for x in risks if str(x).strip()][:2]

    canonical = summary.get("canonical_decision")
    canonical = dict(canonical) if isinstance(canonical, dict) else {
        "authority": None,
        "action": None,
        "public_action": None,
        "evidence_state": "NOT_BOUND",
        "hard_veto": False,
        "reason_codes": [],
    }

    price = _asset_brief_v1_num(getattr(trend_result, "current_price", None))
    price_text = _asset_brief_v1_price(price)

    support_values = []
    for raw in (getattr(trend_result, "support_levels", None) or []):
        value = _asset_brief_v1_num(raw)
        if value is not None and value > 0:
            support_values.append(value)

    resistance_values = []
    for raw in (getattr(trend_result, "resistance_levels", None) or []):
        value = _asset_brief_v1_num(raw)
        if value is not None and value > 0:
            resistance_values.append(value)

    support = max(support_values) if support_values else None
    resistance = min(resistance_values) if resistance_values else None

    trend = _asset_brief_v1_text(
        sections.get("trend"), ("\u8d8b\u52bf\uff1a", "\u8d8b\u52bf:")
    )
    volume_price = _asset_brief_v1_text(
        sections.get("volume_price"), ("\u91cf\u4ef7\uff1a", "\u91cf\u4ef7:")
    )
    structure = _asset_brief_v1_text(
        sections.get("price_structure"), ("\u7ed3\u6784\uff1a", "\u7ed3\u6784:")
    )
    valuation = _asset_brief_v1_text(
        sections.get("valuation"), ("\u4f30\u503c\uff1a", "\u4f30\u503c:")
    )
    cost = _asset_brief_v1_text(
        sections.get("cost_structure"),
        ("\u6210\u672c/\u7b79\u7801\uff1a", "\u6210\u672c/\u7b79\u7801:"),
    )
    momentum = _asset_brief_v1_text(
        sections.get("momentum"), ("\u52a8\u91cf\uff1a", "\u52a8\u91cf:")
    )

    clauses = []
    if price_text and trend:
        clauses.append(f"\u5f53\u524d\u4ef7 {price_text} \u5143\uff0c\u65e5\u7ebf{trend}")
    elif price_text:
        clauses.append(f"\u5f53\u524d\u4ef7 {price_text} \u5143")
    elif trend:
        clauses.append(f"\u65e5\u7ebf{trend}")

    # First-screen causal thesis prioritizes observable supply/demand and chip-cost
    # evidence before valuation/shape detail; it never infers institutional intent.
    for candidate in (volume_price, cost, valuation, structure, momentum):
        if _asset_brief_v1_usable(candidate) and candidate not in clauses:
            clauses.append(candidate)
        if len(clauses) >= 4:
            break

    daily_components = [
        item
        for item in (trend, volume_price, structure, momentum)
        if _asset_brief_v1_usable(item)
    ]
    daily_thesis = "\uff1b".join(daily_components[:3]) or None

    paragraph = "\uff1b".join(clauses[:4]).strip()
    if paragraph:
        paragraph += "\u3002"
    paragraph += f"\u7efc\u5408\u6765\u770b\uff0c{conclusion}"

    return {
        "schema_version": "investor-brief-v1",
        "report_mode": "ASSET_RESEARCH_BRIEF",
        "report_version": "asset-research-brief-v1",
        "coverage": {
            "monthly": "MISSING",
            "weekly": "MISSING",
            "daily": "PARTIAL_CURRENT",
            "60m": "MISSING",
            "30m": "MISSING",
            "15m": "MISSING",
            "5m": "MISSING",
        },
        "coverage_text": (
            "\u5f53\u524d\u8bc1\u636e\u8986\u76d6\uff1a\u65e5\u7ebf\u5df2\u5206\u6790\uff1b"
            "\u6708\u7ebf\u3001\u5468\u7ebf\u300160\u5206\u949f\u300130\u5206\u949f\u300115\u5206\u949f\u548c5\u5206\u949f"
            "\u5c1a\u672a\u8fdb\u5165\u751f\u4ea7\u5224\u65ad\u3002"
        ),
        "timeframe_thesis": {
            "monthly": {"status": "MISSING", "role": "LONG_TERM_CONTEXT", "summary": None},
            "weekly": {"status": "MISSING", "role": "PRIMARY_TREND_CONTEXT", "summary": None},
            "daily": {
                "status": "PARTIAL_CURRENT",
                "role": "PRIMARY_SETUP",
                "summary": daily_thesis,
            },
            "60m": {"status": "MISSING", "role": "OPTIONAL_BRIDGE", "summary": None},
        },
        "short_term_execution_panel": {
            "status": "MISSING",
            "state": "DATA_INSUFFICIENT",
            "30m": {"status": "MISSING", "role": "PRIMARY_STRUCTURE"},
            "15m": {"status": "MISSING", "role": "TRIGGER_CONFIRMATION"},
            "5m": {"status": "MISSING", "role": "MICRO_TIMING"},
            "summary": None,
        },
        "scenario": {
            "preferred": {
                "status": "READY" if trigger else "MISSING",
                "condition": trigger or None,
            },
            "alternative": {
                "status": "MISSING",
                "condition": None,
                "reason": "尚未形成独立确定性备选情景。",
            },
            "invalidation": {
                "status": "READY" if invalidation else "MISSING",
                "condition": invalidation or None,
            },
        },
        "evidence_policy": {
            "legacy_signal_score_role": "REFERENCE_ONLY_NOT_CANONICAL_VOTE_COUNT",
            "correlation_rule": "SAME_UNDERLYING_SWING_ONE_FAMILY_CONFIRMATION_OR_CONFLICT",
            "timeframe_rule": "CROSS_TIMEFRAME_CONFIRMATION_NOT_INDEPENDENT_VOTES",
        },
        "canonical": canonical,
        "one_line_conclusion": conclusion,
        "fused_paragraph": paragraph,
        "current_price": {
            "value": price,
            "source_state": "AVAILABLE" if price_text else "MISSING",
        },
        "key_levels": {
            "support": _asset_brief_v1_price(support),
            "resistance": _asset_brief_v1_price(resistance),
            "support_label": "结构支撑",
            "resistance_label": "结构压力",
        },
        "trigger": trigger,
        "invalidation": invalidation,
        "risk_notes": risks,
        "valuation": {
            "status": "PARTIAL_CURRENT" if _asset_brief_v1_usable(valuation) else "MISSING",
            "summary": valuation if _asset_brief_v1_usable(valuation) else "",
            "uncertainty": (
                "\u5c1a\u672a\u7ed1\u5b9a\u53ef\u9760\u5408\u7406\u4ef7\u683c\u533a\u95f4"
                "\u3001\u5386\u53f2\u5206\u4f4d\u4e0e\u540c\u884c\u6bd4\u8f83\u3002"
            ),
        },
        "historical_reference": {
            "available": False,
            "reason": (
                "\u7f3a\u5c11\u540c\u7b56\u7565/\u540c\u671f\u9650/PIT\u4e00\u81f4"
                "\u4e14\u6210\u719f\u7684\u6837\u672c\u3002"
            ),
        },
        "current_probability": {
            "available": False,
            "reason": "\u5c1a\u672a\u5b8c\u6210\u72ec\u7acb\u6821\u51c6\u3002",
        },
        "detail_refs": ["factor_decision.sections", "trend_result"],
    }


def build_stock_factor_decision_summary(
    trend_result: Any,
    *,
    fundamental_context: Optional[Dict[str, Any]] = None,
    chip_data: Any = None,
    daily_market_context: Any = None,
    market_structure_context: Optional[Dict[str, Any]] = None,
    relative_strength_context: Optional[Dict[str, Any]] = None,
    supply_demand_context: Optional[Dict[str, Any]] = None,
    include_canonical: bool = False,
) -> Dict[str, Any]:
    """Build a deterministic, human-readable stock summary for report rendering.

    The 0-100 score is the existing ``StockTrendAnalyzer.signal_score``.  The
    function deliberately leaves historical win rate and current opportunity
    probability unavailable until the exact same strategy/outcome contract is
    actually bound and calibrated.
    """

    if trend_result is None and not include_canonical:
        raise ValueError("trend_result is required")

    market_sector_regime = _build_market_sector_regime_evidence(
        daily_market_context,
        market_structure_context,
    )
    trend_relative_strength = _build_trend_relative_strength_evidence(
        trend_result,
        relative_strength_context,
    )
    supply_demand_volume_price = _build_supply_demand_volume_price_evidence(
        trend_result,
        supply_demand_context,
    )
    canonical_decision = (
        _canonical_decision(trend_result, market_sector_regime)
        if include_canonical
        else None
    )
    raw_score = _safe_float(getattr(trend_result, "signal_score", None))
    score = (
        int(max(0, min(100, round(raw_score))))
        if raw_score is not None
        else None if include_canonical else 0
    )
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
        "market_sector_regime": _market_sector_regime_summary(market_sector_regime),
        "trend_relative_strength": _trend_relative_strength_summary(trend_relative_strength),
        "supply_demand_volume_price": _supply_demand_volume_price_summary(supply_demand_volume_price),
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

    if canonical_decision and canonical_decision["evidence_state"] == "UNKNOWN":
        conclusion = "数据不足，暂不采取买卖动作；等待必需趋势、评分与量价证据完整。"
    elif canonical_decision and canonical_decision["action"] == "PASS":
        conclusion = "风险或弱势条件触发，当前回避新增仓位；等待条件修复后再评估。"
    else:
        conclusion = _conclusion(trend_result, score if score is not None else 0)

    summary = {
        "strategy_id": "stock_trend_quality_pullback_v1",
        "contract_version": "1.0",
        "composite_score": score,
        "score_note": (
            "现有技术参考分，仅用于排序和解释；不作为 canonical 独立证据投票，"
            "也不代表胜率或概率。"
        ),
        "historical_reference": {
            "available": False,
            "display": "样本不足，暂不展示",
        },
        "current_probability": {
            "available": False,
            "display": "暂不提供（尚未完成独立校准）",
        },
        "conclusion": conclusion,
        "why": why[:4],
        "action_condition": action_condition,
        "invalidation_condition": invalidation_condition,
        "valuation": valuation,
        "cost_structure": cost_structure,
        "risk_notes": _risk_notes(trend_result),
        "sections": sections,
        "market_sector_regime": market_sector_regime,
        "trend_relative_strength": trend_relative_strength,
        "supply_demand_volume_price": supply_demand_volume_price,
    }
    if canonical_decision is not None:
        summary["canonical_decision"] = canonical_decision
    summary["investor_brief"] = _build_asset_research_brief_v1(
        trend_result,
        summary,
    )
    return summary
