# -*- coding: utf-8 -*-
"""Replay-safe Pattern/Trigger V1 over DSA's confirmed Pivot/Swing owner.

This module owns only higher-order geometry and lifecycle composition.  It never
recomputes Pivot/Swing or breakout/retest/failed-breakout states, never fetches
market data, and never creates independent action authority.
"""

from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
from math import isfinite
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


PATTERN_TRIGGER_SCHEMA_VERSION = "pattern-trigger-v1"
ALGORITHM_VERSION = "pattern-trigger-v1"
MIN_OBSERVATIONS = 30
SOURCE_ALIGNMENT_WINDOW = 60
PRIOR_TREND_WINDOW = 20
PRIOR_TREND_MIN_OBSERVATIONS = 10
CUP_RIM_TOLERANCE_PCT = 8.0
CUP_MIN_DEPTH_PCT = 8.0
CUP_MAX_DEPTH_PCT = 50.0
CUP_MIN_DURATION_SESSIONS = 10
CUP_MAX_DURATION_SESSIONS = 90
CUP_BOTTOM_POSITION_MIN = 0.20
CUP_BOTTOM_POSITION_MAX = 0.80
HANDLE_MAX_DEPTH_PCT = 15.0
DOUBLE_BOTTOM_LOW_TOLERANCE_PCT = 8.0
DOUBLE_BOTTOM_MIN_SEPARATION_SESSIONS = 5
DOUBLE_BOTTOM_MAX_SEPARATION_SESSIONS = 70
DOUBLE_BOTTOM_MIN_REBOUND_PCT = 5.0
VCP_MIN_SWINGS = 4
VCP_CONTRACTION_RATIO_MAX = 0.90
FLAT_BASE_WINDOW = 25
FLAT_BASE_MAX_RANGE_PCT = 15.0
TIGHT_CONSOLIDATION_WINDOW = 10
TIGHT_CONSOLIDATION_MAX_RANGE_PCT = 8.0
VOLUME_CONFIRMATION_RATIO = 1.20
MAX_PATTERNS = 5

_CONFIG = {
    "algorithm_version": ALGORITHM_VERSION,
    "minimum_observations": MIN_OBSERVATIONS,
    "source_alignment_window": SOURCE_ALIGNMENT_WINDOW,
    "prior_trend_window": PRIOR_TREND_WINDOW,
    "prior_trend_min_observations": PRIOR_TREND_MIN_OBSERVATIONS,
    "cup_rim_tolerance_pct": CUP_RIM_TOLERANCE_PCT,
    "cup_min_depth_pct": CUP_MIN_DEPTH_PCT,
    "cup_max_depth_pct": CUP_MAX_DEPTH_PCT,
    "cup_min_duration_sessions": CUP_MIN_DURATION_SESSIONS,
    "cup_max_duration_sessions": CUP_MAX_DURATION_SESSIONS,
    "cup_bottom_position_min": CUP_BOTTOM_POSITION_MIN,
    "cup_bottom_position_max": CUP_BOTTOM_POSITION_MAX,
    "handle_max_depth_pct": HANDLE_MAX_DEPTH_PCT,
    "double_bottom_low_tolerance_pct": DOUBLE_BOTTOM_LOW_TOLERANCE_PCT,
    "double_bottom_min_separation_sessions": DOUBLE_BOTTOM_MIN_SEPARATION_SESSIONS,
    "double_bottom_max_separation_sessions": DOUBLE_BOTTOM_MAX_SEPARATION_SESSIONS,
    "double_bottom_min_rebound_pct": DOUBLE_BOTTOM_MIN_REBOUND_PCT,
    "vcp_min_swings": VCP_MIN_SWINGS,
    "vcp_contraction_ratio_max": VCP_CONTRACTION_RATIO_MAX,
    "flat_base_window": FLAT_BASE_WINDOW,
    "flat_base_max_range_pct": FLAT_BASE_MAX_RANGE_PCT,
    "tight_consolidation_window": TIGHT_CONSOLIDATION_WINDOW,
    "tight_consolidation_max_range_pct": TIGHT_CONSOLIDATION_MAX_RANGE_PCT,
    "volume_confirmation_ratio": VOLUME_CONFIRMATION_RATIO,
    "method_semantics": "STOCKCHARTS_ONEIL_METHOD_REUSE_20260916",
    "parameter_policy": "VERSIONED_CANDIDATE_PARAMETERS_NOT_UNIVERSAL_CONSTANTS",
}
CONFIG_HASH = sha256(
    json.dumps(_CONFIG, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()

_PATTERN_PRIORITY = {
    ("CUP_BASE", None): 40,
    ("DOUBLE_BOTTOM_BASE", None): 40,
    ("CONTRACTION_BASE", "VCP"): 30,
    ("CONTRACTION_BASE", "FLAT_BASE"): 20,
    ("CONTRACTION_BASE", "TIGHT_CONSOLIDATION"): 10,
}


def _safe_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _normalize_history(history: Any, *, target_date: date) -> pd.DataFrame:
    required = ["date", "open", "high", "low", "close"]
    if not isinstance(history, pd.DataFrame) or history.empty:
        return pd.DataFrame(columns=required)
    frame = history.copy()
    frame.columns = [str(column).lower() for column in frame.columns]
    if any(column not in frame.columns for column in required):
        return pd.DataFrame(columns=required)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "volume" in frame.columns:
        frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
    frame = frame.dropna(subset=required)
    frame = frame[frame["date"] <= target_date]
    frame = frame.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    optional = [column for column in ("volume", "data_source") if column in frame.columns]
    return frame[required + optional].reset_index(drop=True)


def _invalid_ohlc(frame: pd.DataFrame) -> bool:
    return bool(
        (
            (frame["open"] <= 0)
            | (frame["high"] <= 0)
            | (frame["low"] <= 0)
            | (frame["close"] <= 0)
            | (frame["high"] < frame["low"])
            | (frame["high"] < frame["open"])
            | (frame["high"] < frame["close"])
            | (frame["low"] > frame["open"])
            | (frame["low"] > frame["close"])
        ).any()
    )


def _source_alignment(frame: pd.DataFrame) -> Dict[str, Any]:
    window = frame.tail(SOURCE_ALIGNMENT_WINDOW)
    if "data_source" not in window.columns:
        return {"status": "UNPROVEN", "sources": [], "rows_complete": False}
    values = window["data_source"].map(
        lambda value: str(value).strip() if value not in (None, "") else ""
    )
    sources = sorted({value for value in values.tolist() if value})
    complete = bool((values != "").all())
    return {
        "status": "SINGLE_SOURCE" if len(sources) == 1 and complete else "UNPROVEN",
        "sources": sources,
        "rows_complete": complete,
    }


def _base_payload(*, stock_code: str, market: Optional[str], target_date: Optional[date]) -> Dict[str, Any]:
    return {
        "schema_version": PATTERN_TRIGGER_SCHEMA_VERSION,
        "family": "pattern_trigger",
        "stock_code": stock_code,
        "market": str(market or "").lower() or None,
        "timeframe": "1d",
        "target_date": target_date.isoformat() if isinstance(target_date, date) else None,
        "completed_bar_only": True,
        "algorithm_version": ALGORITHM_VERSION,
        "config_hash": CONFIG_HASH,
        "effective_parameters": dict(_CONFIG),
        "method_profile": {
            "cup_base": "STOCKCHARTS_AND_ONEIL_METHOD_SEMANTICS",
            "double_bottom_base": "ONEIL_BULLISH_CONTINUATION_BASE_ONLY",
            "deferred_double_bottom_profile": "STOCKCHARTS_PRIOR_DOWNTREND_REVERSAL",
            "contraction_base": "VCP_FLAT_BASE_TIGHT_CONSOLIDATION_INTERNAL_COMPOSE",
        },
        "hard_veto": False,
        "independent_action_authority": False,
        "automatic_strategy_switching": False,
        "institutional_intent_inferred": False,
        "same_swing_double_counting": "PROHIBITED",
    }


def _empty_payload(
    base: Dict[str, Any],
    *,
    status: str,
    reason: str,
    observations: int = 0,
    source_alignment: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        **base,
        "status": status,
        "reason": reason,
        "observations": observations,
        "source_alignment": source_alignment or {"status": "UNPROVEN", "sources": [], "rows_complete": False},
        "patterns": [],
        "primary_pattern": None,
        "pattern_count": 0,
        "historical_replay_eligible": False,
    }


def _pivot_ref(pivot: Dict[str, Any]) -> Dict[str, Any]:
    price = _safe_float(pivot.get("price"))
    kind = str(pivot.get("kind") or "")
    origin_time = str(pivot.get("origin_time") or "")
    confirmed_at = str(pivot.get("confirmed_at") or "")
    pivot_id = f"{kind}:{origin_time}:{confirmed_at}:{price:.6f}" if price is not None else f"{kind}:{origin_time}:{confirmed_at}"
    return {
        "pivot_id": pivot_id,
        "kind": kind,
        "price": price,
        "origin_time": origin_time,
        "confirmed_at": confirmed_at,
        "provisional": bool(pivot.get("provisional")),
        "algorithm_version": pivot.get("algorithm_version"),
        "config_hash": pivot.get("config_hash"),
    }


def _validate_price_structure(
    context: Any,
    *,
    target_date: date,
    history_alignment: Dict[str, Any],
) -> Tuple[Optional[List[Dict[str, Any]]], str, Dict[str, Any], Dict[str, Any]]:
    if not isinstance(context, dict):
        return None, "PRICE_STRUCTURE_CONTEXT_MISSING", {}, {"state": "NONE"}
    if str(context.get("schema_version") or "") != "price-structure-v1":
        return None, "PRICE_STRUCTURE_SCHEMA_UNPROVEN", {}, {"state": "NONE"}
    if str(context.get("status") or "").upper() != "READY":
        return None, "PRICE_STRUCTURE_NOT_READY", {}, context.get("structure_event") or {"state": "NONE"}
    if context.get("historical_replay_eligible") is not True:
        return None, "PRICE_STRUCTURE_REPLAY_UNPROVEN", {}, context.get("structure_event") or {"state": "NONE"}
    if str(context.get("target_date") or "") != target_date.isoformat():
        return None, "PRICE_STRUCTURE_TARGET_DATE_MISMATCH", {}, context.get("structure_event") or {"state": "NONE"}

    structure_alignment = context.get("source_alignment") if isinstance(context.get("source_alignment"), dict) else {}
    if history_alignment.get("status") != "SINGLE_SOURCE" or structure_alignment.get("status") != "SINGLE_SOURCE":
        return None, "SOURCE_ALIGNMENT_UNPROVEN", structure_alignment, context.get("structure_event") or {"state": "NONE"}
    if sorted(history_alignment.get("sources") or []) != sorted(structure_alignment.get("sources") or []):
        return None, "SOURCE_ALIGNMENT_MISMATCH", structure_alignment, context.get("structure_event") or {"state": "NONE"}

    pivots: List[Dict[str, Any]] = []
    for raw in context.get("pivots") or []:
        if not isinstance(raw, dict) or bool(raw.get("provisional")):
            continue
        origin = _parse_date(raw.get("origin_time"))
        confirmed = _parse_date(raw.get("confirmed_at"))
        price = _safe_float(raw.get("price"))
        if origin is None or confirmed is None or price is None or price <= 0:
            return None, "PRICE_STRUCTURE_PIVOT_PROVENANCE_INVALID", structure_alignment, context.get("structure_event") or {"state": "NONE"}
        if origin > target_date or confirmed > target_date:
            return None, "PRICE_STRUCTURE_FUTURE_CONFIRMATION", structure_alignment, context.get("structure_event") or {"state": "NONE"}
        pivots.append(dict(raw))
    pivots.sort(key=lambda item: (str(item.get("origin_time")), str(item.get("confirmed_at"))))
    return pivots, "READY", structure_alignment, context.get("structure_event") or {"state": "NONE"}


def _date_index(frame: pd.DataFrame) -> Dict[date, int]:
    return {item: index for index, item in enumerate(frame["date"].tolist())}


def _prior_trend(frame: pd.DataFrame, *, origin_time: str) -> Dict[str, Any]:
    origin = _parse_date(origin_time)
    if origin is None:
        return {"status": "UNKNOWN", "reason": "PATTERN_ORIGIN_UNKNOWN"}
    positions = _date_index(frame)
    origin_index = positions.get(origin)
    if origin_index is None:
        return {"status": "UNKNOWN", "reason": "PATTERN_ORIGIN_NOT_IN_HISTORY"}
    prior = frame.iloc[:origin_index].tail(PRIOR_TREND_WINDOW)
    if len(prior) < PRIOR_TREND_MIN_OBSERVATIONS:
        return {
            "status": "UNKNOWN",
            "reason": "PRIOR_TREND_WARMUP_INSUFFICIENT",
            "observations": int(len(prior)),
        }
    first = float(prior.iloc[0]["close"])
    last = float(prior.iloc[-1]["close"])
    mean_close = float(prior["close"].mean())
    change_pct = (last / first - 1.0) * 100.0 if first > 0 else None
    if change_pct is None:
        state = "UNKNOWN"
    elif change_pct > 0 and last >= mean_close:
        state = "UPTREND"
    elif change_pct < 0 and last <= mean_close:
        state = "DOWNTREND"
    else:
        state = "MIXED"
    return {
        "status": "READY",
        "state": state,
        "window_sessions": int(len(prior)),
        "start_date": prior.iloc[0]["date"].isoformat(),
        "end_date": prior.iloc[-1]["date"].isoformat(),
        "return_pct": round(change_pct, 6) if change_pct is not None else None,
        "last_close_vs_window_mean": round(last / mean_close - 1.0, 6) if mean_close > 0 else None,
        "required_state": "UPTREND",
        "passes_required_state": state == "UPTREND",
    }


def _sessions_between(frame: pd.DataFrame, start: str, end: str) -> Optional[int]:
    positions = _date_index(frame)
    start_date = _parse_date(start)
    end_date = _parse_date(end)
    if start_date not in positions or end_date not in positions:
        return None
    return positions[end_date] - positions[start_date]


def _volume_evidence_ref(context: Any, *, target_date: date) -> Dict[str, Any]:
    if not isinstance(context, dict):
        return {"status": "MISSING", "reason": "SUPPLY_DEMAND_CONTEXT_MISSING"}
    if str(context.get("target_date") or "") != target_date.isoformat():
        return {"status": "MISSING", "reason": "SUPPLY_DEMAND_TARGET_DATE_MISMATCH"}
    status = str(context.get("status") or "").upper()
    relative = context.get("relative_volume") if isinstance(context.get("relative_volume"), dict) else {}
    ratio = _safe_float(relative.get("volume_ratio_20d"))
    confirmation = "MISSING"
    if status == "READY" and ratio is not None:
        confirmation = "CONFIRMED" if ratio >= VOLUME_CONFIRMATION_RATIO else "NOT_CONFIRMED"
    return {
        "family": "supply_demand_volume_price",
        "status": status or "MISSING",
        "target_date": context.get("target_date"),
        "state": context.get("state"),
        "volume_ratio_20d": ratio,
        "confirmation": confirmation,
        "confirmation_threshold": VOLUME_CONFIRMATION_RATIO,
        "correlation_group": "relative_volume",
    }


def _event_matches_trigger(event: Dict[str, Any], trigger: Dict[str, Any]) -> bool:
    if not isinstance(event, dict) or not isinstance(trigger, dict):
        return False
    if str(event.get("level_kind") or "") != "HIGH":
        return False
    if str(event.get("level_origin_time") or "") != str(trigger.get("origin_time") or ""):
        return False
    event_confirmed = str(event.get("level_confirmed_at") or "")
    trigger_confirmed = str(trigger.get("confirmed_at") or "")
    return not event_confirmed or event_confirmed == trigger_confirmed


def _apply_lifecycle(candidate: Dict[str, Any], *, structure_event: Dict[str, Any], target_date: date) -> Dict[str, Any]:
    trigger = candidate.pop("_trigger_pivot", None)
    candidate["price_structure_trigger_ref"] = {"status": "MISSING"}
    candidate["lifecycle"] = "FORMING"
    candidate["confirmed_at"] = None
    candidate["invalidated_at"] = candidate.get("invalidated_at")
    if not isinstance(trigger, dict) or not _event_matches_trigger(structure_event, trigger):
        return candidate

    event_time = _parse_date(structure_event.get("event_time"))
    if event_time is None or event_time > target_date:
        return candidate
    public_event = {
        key: value
        for key, value in structure_event.items()
        if not str(key).startswith("_")
    }
    candidate["price_structure_trigger_ref"] = {
        "status": "READY",
        "owner": "price-structure-v1",
        **public_event,
    }
    state = str(structure_event.get("state") or "")
    if state in {"UP_BREAKOUT", "UP_BREAKOUT_RETEST_HOLD"}:
        candidate["lifecycle"] = "CONFIRMED"
        candidate["confirmed_at"] = event_time.isoformat()
        candidate["reason_codes"] = list(dict.fromkeys([*candidate.get("reason_codes", []), "PRICE_STRUCTURE_BREAKOUT_CONFIRMED"]))
    elif state == "FAILED_UP_BREAKOUT":
        candidate["lifecycle"] = "FAILED"
        candidate["geometry_state"] = "INVALIDATED"
        candidate["invalidated_at"] = event_time.isoformat()
        candidate["reason_codes"] = list(dict.fromkeys([*candidate.get("reason_codes", []), "PRICE_STRUCTURE_FAILED_BREAKOUT"]))
    return candidate


def _pattern_candidate(
    *,
    pattern_type: str,
    subtype: Optional[str],
    ordered_pivots: List[Dict[str, Any]],
    trigger_pivot: Dict[str, Any],
    prior_trend: Dict[str, Any],
    metrics: Dict[str, Any],
    reason_codes: List[str],
    volume_ref: Dict[str, Any],
    geometry_state: str = "READY",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    refs = [_pivot_ref(item) for item in ordered_pivots]
    group = "same_swing:" + ":".join(item["pivot_id"] for item in refs)
    identity_payload = {
        "pattern_type": pattern_type,
        "subtype": subtype,
        "pivot_ids": [item["pivot_id"] for item in refs],
        "config_hash": CONFIG_HASH,
    }
    pattern_id = sha256(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    candidate = {
        "pattern_id": pattern_id,
        "pattern_type": pattern_type,
        "subtype": subtype,
        "geometry_state": geometry_state,
        "lifecycle": "FORMING",
        "origin_time": refs[0]["origin_time"],
        "geometry_confirmed_at": max(item["confirmed_at"] for item in refs),
        "confirmed_at": None,
        "invalidated_at": None,
        "provisional": False,
        "ordered_pivots": refs,
        "prior_trend": prior_trend,
        "metrics": metrics,
        "effective_parameters": dict(_CONFIG),
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "correlation_group": group,
        "volume_evidence_ref": volume_ref,
        "price_structure_trigger_ref": {"status": "MISSING"},
        "algorithm_version": ALGORITHM_VERSION,
        "config_hash": CONFIG_HASH,
        "method_profile": (
            "ONEIL_BULLISH_CONTINUATION_BASE"
            if pattern_type == "DOUBLE_BOTTOM_BASE"
            else "STOCKCHARTS_ONEIL_METHOD_TRANSLATION"
        ),
        "_trigger_pivot": trigger_pivot,
    }
    if extra:
        candidate.update(extra)
    return candidate


def _detect_cups(
    frame: pd.DataFrame,
    pivots: List[Dict[str, Any]],
    *,
    volume_ref: Dict[str, Any],
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    recent = pivots[-12:]
    current_close = float(frame.iloc[-1]["close"])
    for index in range(len(recent) - 2):
        left, bottom, right = recent[index : index + 3]
        if [left.get("kind"), bottom.get("kind"), right.get("kind")] != ["HIGH", "LOW", "HIGH"]:
            continue
        left_price = _safe_float(left.get("price"))
        bottom_price = _safe_float(bottom.get("price"))
        right_price = _safe_float(right.get("price"))
        if None in {left_price, bottom_price, right_price}:
            continue
        rim_mean = (left_price + right_price) / 2.0
        rim_diff_pct = abs(left_price - right_price) / rim_mean * 100.0
        depth_pct = (rim_mean - bottom_price) / rim_mean * 100.0
        duration = _sessions_between(frame, str(left.get("origin_time")), str(right.get("origin_time")))
        bottom_offset = _sessions_between(frame, str(left.get("origin_time")), str(bottom.get("origin_time")))
        if duration is None or bottom_offset is None or duration <= 0:
            continue
        bottom_position = bottom_offset / duration
        if not (
            rim_diff_pct <= CUP_RIM_TOLERANCE_PCT
            and CUP_MIN_DEPTH_PCT <= depth_pct <= CUP_MAX_DEPTH_PCT
            and CUP_MIN_DURATION_SESSIONS <= duration <= CUP_MAX_DURATION_SESSIONS
            and CUP_BOTTOM_POSITION_MIN <= bottom_position <= CUP_BOTTOM_POSITION_MAX
        ):
            continue
        prior = _prior_trend(frame, origin_time=str(left.get("origin_time")))
        if prior.get("passes_required_state") is not True:
            continue

        ordered = [left, bottom, right]
        handle_status = "NONE"
        handle_depth_pct = None
        next_pivot = recent[index + 3] if index + 3 < len(recent) else None
        if isinstance(next_pivot, dict) and next_pivot.get("kind") == "LOW":
            low_price = _safe_float(next_pivot.get("price"))
            if low_price is None:
                continue
            handle_depth_pct = (right_price - low_price) / right_price * 100.0
            if handle_depth_pct > HANDLE_MAX_DEPTH_PCT:
                continue
            handle_status = "COMPLETE"
            ordered.append(next_pivot)
        elif current_close < right_price:
            prospective_depth = (right_price - current_close) / right_price * 100.0
            if prospective_depth <= HANDLE_MAX_DEPTH_PCT:
                handle_status = "FORMING"

        candidates.append(
            _pattern_candidate(
                pattern_type="CUP_BASE",
                subtype=None,
                ordered_pivots=ordered,
                trigger_pivot=right,
                prior_trend=prior,
                metrics={
                    "rim_difference_pct": round(rim_diff_pct, 6),
                    "cup_depth_pct": round(depth_pct, 6),
                    "duration_sessions": duration,
                    "bottom_position_ratio": round(bottom_position, 6),
                    "handle_depth_pct": round(handle_depth_pct, 6) if handle_depth_pct is not None else None,
                },
                reason_codes=["PRIOR_UPTREND_READY", "CUP_GEOMETRY_READY"],
                volume_ref=volume_ref,
                extra={"handle_status": handle_status},
            )
        )
    return candidates


def _detect_double_bottoms(
    frame: pd.DataFrame,
    pivots: List[Dict[str, Any]],
    *,
    volume_ref: Dict[str, Any],
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    recent = pivots[-12:]
    for index in range(len(recent) - 2):
        first, middle, second = recent[index : index + 3]
        if [first.get("kind"), middle.get("kind"), second.get("kind")] != ["LOW", "HIGH", "LOW"]:
            continue
        first_price = _safe_float(first.get("price"))
        middle_price = _safe_float(middle.get("price"))
        second_price = _safe_float(second.get("price"))
        if None in {first_price, middle_price, second_price}:
            continue
        low_mean = (first_price + second_price) / 2.0
        low_diff_pct = abs(first_price - second_price) / low_mean * 100.0
        separation = _sessions_between(frame, str(first.get("origin_time")), str(second.get("origin_time")))
        rebound_pct = (middle_price / max(first_price, second_price) - 1.0) * 100.0
        if separation is None:
            continue
        if not (
            low_diff_pct <= DOUBLE_BOTTOM_LOW_TOLERANCE_PCT
            and DOUBLE_BOTTOM_MIN_SEPARATION_SESSIONS <= separation <= DOUBLE_BOTTOM_MAX_SEPARATION_SESSIONS
            and rebound_pct >= DOUBLE_BOTTOM_MIN_REBOUND_PCT
        ):
            continue
        prior = _prior_trend(frame, origin_time=str(first.get("origin_time")))
        if prior.get("passes_required_state") is not True:
            continue
        candidates.append(
            _pattern_candidate(
                pattern_type="DOUBLE_BOTTOM_BASE",
                subtype=None,
                ordered_pivots=[first, middle, second],
                trigger_pivot=middle,
                prior_trend=prior,
                metrics={
                    "low_difference_pct": round(low_diff_pct, 6),
                    "separation_sessions": separation,
                    "middle_rebound_pct": round(rebound_pct, 6),
                    "second_low_undercut_pct": round((second_price / first_price - 1.0) * 100.0, 6),
                },
                reason_codes=["PRIOR_UPTREND_READY", "ONEIL_CONTINUATION_DOUBLE_BOTTOM_READY"],
                volume_ref=volume_ref,
            )
        )
    return candidates


def _window_range_pct(frame: pd.DataFrame, sessions: int) -> Optional[float]:
    window = frame.tail(sessions)
    if len(window) < sessions:
        return None
    low = float(window["low"].min())
    high = float(window["high"].max())
    if low <= 0:
        return None
    return (high / low - 1.0) * 100.0


def _detect_contraction(
    frame: pd.DataFrame,
    pivots: List[Dict[str, Any]],
    *,
    volume_ref: Dict[str, Any],
) -> List[Dict[str, Any]]:
    if len(pivots) < 2:
        return []
    recent = pivots[-8:]
    subtype: Optional[str] = None
    metrics: Dict[str, Any] = {}
    ordered: List[Dict[str, Any]] = []

    swing_changes: List[float] = []
    for start, end in zip(recent, recent[1:]):
        start_price = _safe_float(start.get("price"))
        end_price = _safe_float(end.get("price"))
        if start_price is None or end_price is None or start_price <= 0:
            continue
        swing_changes.append(abs(end_price / start_price - 1.0) * 100.0)
    if len(swing_changes) >= VCP_MIN_SWINGS:
        last = swing_changes[-VCP_MIN_SWINGS:]
        if all(next_value <= prior_value * VCP_CONTRACTION_RATIO_MAX for prior_value, next_value in zip(last, last[1:])):
            subtype = "VCP"
            ordered = recent[-(VCP_MIN_SWINGS + 1):]
            metrics = {
                "swing_contraction_pct": [round(value, 6) for value in last],
                "contraction_ratio_max": VCP_CONTRACTION_RATIO_MAX,
            }

    if subtype is None:
        flat_range = _window_range_pct(frame, FLAT_BASE_WINDOW)
        flat_start = frame.iloc[-FLAT_BASE_WINDOW]["date"] if len(frame) >= FLAT_BASE_WINDOW else None
        flat_pivots = [
            item for item in recent
            if flat_start is not None and _parse_date(item.get("origin_time")) is not None and _parse_date(item.get("origin_time")) >= flat_start
        ]
        if flat_range is not None and flat_range <= FLAT_BASE_MAX_RANGE_PCT and len(flat_pivots) >= 2:
            subtype = "FLAT_BASE"
            ordered = flat_pivots
            metrics = {"window_sessions": FLAT_BASE_WINDOW, "range_pct": round(flat_range, 6)}

    if subtype is None:
        tight_range = _window_range_pct(frame, TIGHT_CONSOLIDATION_WINDOW)
        tight_start = frame.iloc[-TIGHT_CONSOLIDATION_WINDOW]["date"] if len(frame) >= TIGHT_CONSOLIDATION_WINDOW else None
        tight_pivots = [
            item for item in recent
            if tight_start is not None and _parse_date(item.get("origin_time")) is not None and _parse_date(item.get("origin_time")) >= tight_start
        ]
        if tight_range is not None and tight_range <= TIGHT_CONSOLIDATION_MAX_RANGE_PCT and len(tight_pivots) >= 2:
            subtype = "TIGHT_CONSOLIDATION"
            ordered = tight_pivots
            metrics = {"window_sessions": TIGHT_CONSOLIDATION_WINDOW, "range_pct": round(tight_range, 6)}

    if subtype is None or len(ordered) < 2:
        return []
    prior = _prior_trend(frame, origin_time=str(ordered[0].get("origin_time")))
    if prior.get("passes_required_state") is not True:
        return []
    highs = [item for item in ordered if item.get("kind") == "HIGH"]
    if not highs:
        return []
    trigger = highs[-1]
    return [
        _pattern_candidate(
            pattern_type="CONTRACTION_BASE",
            subtype=subtype,
            ordered_pivots=ordered,
            trigger_pivot=trigger,
            prior_trend=prior,
            metrics=metrics,
            reason_codes=["PRIOR_UPTREND_READY", f"{subtype}_GEOMETRY_READY"],
            volume_ref=volume_ref,
        )
    ]


def _pattern_priority(pattern: Dict[str, Any]) -> int:
    return _PATTERN_PRIORITY.get((pattern.get("pattern_type"), pattern.get("subtype")), 0)


def _dedupe_patterns(patterns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep one canonical interpretation per identical ordered Pivot/Swing group."""
    by_group: Dict[str, Dict[str, Any]] = {}
    for pattern in patterns:
        group = str(pattern.get("correlation_group") or pattern.get("pattern_id") or "")
        existing = by_group.get(group)
        if existing is None or _pattern_priority(pattern) > _pattern_priority(existing):
            by_group[group] = pattern
    return list(by_group.values())


def _primary_pattern(patterns: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not patterns:
        return None
    lifecycle_rank = {"FORMING": 1, "CONFIRMED": 2, "FAILED": 3}
    return max(
        patterns,
        key=lambda item: (
            str(item.get("geometry_confirmed_at") or ""),
            lifecycle_rank.get(str(item.get("lifecycle") or ""), 0),
            _pattern_priority(item),
        ),
    )


def build_pattern_trigger_context(
    *,
    stock_code: str,
    history: Any,
    target_date: Optional[date],
    market: Optional[str] = None,
    price_structure_context: Any = None,
    supply_demand_context: Any = None,
) -> Dict[str, Any]:
    """Build Pattern/Trigger evidence without fetching or owning breakout/action logic."""
    base = _base_payload(stock_code=stock_code, market=market, target_date=target_date)
    if not isinstance(target_date, date):
        return _empty_payload(base, status="UNKNOWN", reason="TARGET_DATE_UNKNOWN")

    frame = _normalize_history(history, target_date=target_date)
    if frame.empty:
        return _empty_payload(base, status="MISSING", reason="COMPLETED_OHLC_MISSING")
    if frame.iloc[-1]["date"] != target_date:
        return _empty_payload(base, status="MISSING", reason="TARGET_DATE_BAR_MISSING", observations=int(len(frame)))
    if len(frame) < MIN_OBSERVATIONS:
        return _empty_payload(base, status="MISSING", reason="WARMUP_INSUFFICIENT", observations=int(len(frame)))
    if _invalid_ohlc(frame):
        return _empty_payload(base, status="UNKNOWN", reason="INVALID_OHLC", observations=int(len(frame)))

    history_alignment = _source_alignment(frame)
    pivots, structure_reason, structure_alignment, structure_event = _validate_price_structure(
        price_structure_context,
        target_date=target_date,
        history_alignment=history_alignment,
    )
    if pivots is None:
        status = "UNKNOWN" if structure_reason in {"PRICE_STRUCTURE_FUTURE_CONFIRMATION", "PRICE_STRUCTURE_PIVOT_PROVENANCE_INVALID"} else "PARTIAL"
        return _empty_payload(
            base,
            status=status,
            reason=structure_reason,
            observations=int(len(frame)),
            source_alignment=history_alignment,
        )

    volume_ref = _volume_evidence_ref(supply_demand_context, target_date=target_date)
    raw_patterns = [
        *_detect_cups(frame, pivots, volume_ref=volume_ref),
        *_detect_double_bottoms(frame, pivots, volume_ref=volume_ref),
        *_detect_contraction(frame, pivots, volume_ref=volume_ref),
    ]
    patterns = _dedupe_patterns(raw_patterns)
    patterns = [
        _apply_lifecycle(item, structure_event=structure_event, target_date=target_date)
        for item in patterns
    ]
    patterns.sort(key=lambda item: (str(item.get("geometry_confirmed_at") or ""), _pattern_priority(item)))
    patterns = patterns[-MAX_PATTERNS:]
    primary = _primary_pattern(patterns)
    reason = "PATTERN_TRIGGER_READY" if patterns else "NO_SUPPORTED_PATTERN"
    return {
        **base,
        "status": "READY",
        "reason": reason,
        "observations": int(len(frame)),
        "start_date": frame.iloc[0]["date"].isoformat(),
        "end_date": frame.iloc[-1]["date"].isoformat(),
        "source_alignment": structure_alignment,
        "history_source_alignment": history_alignment,
        "volume_evidence_ref": volume_ref,
        "patterns": patterns,
        "primary_pattern": primary,
        "pattern_count": len(patterns),
        "historical_replay_eligible": True,
    }
