# -*- coding: utf-8 -*-
"""Deterministic cost-structure evidence from current provider chips and completed bars.

The V1 contract deliberately keeps two evidence blocks separate:
- provider chip data is a provider-estimated *current/recent* snapshot;
- bar-derived reference cost is a PIT-safe completed-daily-bar price/volume proxy.

Neither block represents actual holder acquisition records or institutional intent.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

import pandas as pd


COST_STRUCTURE_SCHEMA_VERSION = "cost-structure-v1"
REFERENCE_WINDOWS = (20, 60)


def _safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not pd.notna(number):
        return None
    return number


def _normalize_history(history: Any, *, target_date: date) -> pd.DataFrame:
    required = ["date", "high", "low", "close", "volume"]
    if not isinstance(history, pd.DataFrame) or history.empty:
        return pd.DataFrame(columns=required)
    frame = history.copy()
    frame.columns = [str(column).lower() for column in frame.columns]
    if any(column not in frame.columns for column in required):
        return pd.DataFrame(columns=required)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    for column in ("high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=required)
    frame = frame[frame["date"] <= target_date]
    frame = frame.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    columns = required + (["data_source"] if "data_source" in frame.columns else [])
    return frame[columns].reset_index(drop=True)


def _bar_window(frame: pd.DataFrame, *, sessions: int) -> Dict[str, Any]:
    if len(frame) < sessions:
        return {
            "status": "MISSING",
            "reason": "WARMUP_INSUFFICIENT",
            "window_sessions": sessions,
            "observations": int(len(frame)),
            "historical_replay_eligible": False,
        }

    window = frame.tail(sessions).reset_index(drop=True)
    invalid_price = (
        (window["high"] <= 0)
        | (window["low"] <= 0)
        | (window["close"] <= 0)
        | (window["high"] < window["low"])
        | (window["close"] > window["high"])
        | (window["close"] < window["low"])
    )
    if bool(invalid_price.any()):
        return {
            "status": "UNKNOWN",
            "reason": "INVALID_OHLC",
            "window_sessions": sessions,
            "observations": sessions,
            "historical_replay_eligible": False,
        }
    if bool((window["volume"] <= 0).any()):
        return {
            "status": "UNKNOWN",
            "reason": "NON_POSITIVE_VOLUME",
            "window_sessions": sessions,
            "observations": sessions,
            "historical_replay_eligible": False,
        }

    denominator_volume = float(window["volume"].sum())
    if denominator_volume <= 0:
        return {
            "status": "UNKNOWN",
            "reason": "NON_POSITIVE_DENOMINATOR_VOLUME",
            "window_sessions": sessions,
            "observations": sessions,
            "historical_replay_eligible": False,
        }

    typical_price = (window["high"] + window["low"] + window["close"]) / 3.0
    reference_price = float((typical_price * window["volume"]).sum() / denominator_volume)
    target_close = float(window.iloc[-1]["close"])
    distance_pct = (target_close / reference_price - 1.0) * 100.0 if reference_price > 0 else None

    source_values: set[str] = set()
    source_rows_complete = False
    if "data_source" in window.columns:
        source_text = window["data_source"].map(
            lambda value: str(value).strip() if value not in (None, "") else ""
        )
        source_values = {value for value in source_text.tolist() if value}
        source_rows_complete = bool((source_text != "").all())
    source_alignment = "SINGLE_SOURCE" if len(source_values) == 1 and source_rows_complete else "UNPROVEN"
    status = "READY" if source_alignment == "SINGLE_SOURCE" else "PARTIAL"

    return {
        "status": status,
        "reason": "COMPLETED_BARS_SINGLE_SOURCE" if status == "READY" else "SOURCE_ALIGNMENT_UNPROVEN",
        "method": "HLC3_VOLUME_WEIGHTED_COMPLETED_DAILY_BARS",
        "semantics": "BAR_DERIVED_REFERENCE_PRICE_NOT_HOLDER_COST",
        "window_sessions": sessions,
        "observations": sessions,
        "start_date": window.iloc[0]["date"].isoformat(),
        "end_date": window.iloc[-1]["date"].isoformat(),
        "rolling_reference_price": round(reference_price, 6),
        "target_close": round(target_close, 6),
        "close_distance_pct": round(distance_pct, 6) if distance_pct is not None else None,
        "denominator_volume": round(denominator_volume, 6),
        "source_alignment": source_alignment,
        "sources": sorted(source_values),
        "historical_replay_eligible": status == "READY",
    }


def _provider_snapshot(chip_data: Any, *, target_date: date) -> Dict[str, Any]:
    if chip_data is None:
        return {
            "status": "MISSING",
            "reason": "PROVIDER_CHIP_SNAPSHOT_MISSING",
            "historical_pit_authority": "CURRENT_ONLY",
            "semantics": "PROVIDER_ESTIMATED_CHIP_DISTRIBUTION",
            "institutional_intent_claim": "NOT_INFERRED",
        }

    source = str(getattr(chip_data, "source", None) or "").strip()
    raw_date = str(getattr(chip_data, "date", None) or "").strip()
    parsed_date = pd.to_datetime(raw_date, errors="coerce")
    source_trade_date = None if pd.isna(parsed_date) else parsed_date.date()
    target_date_match = source_trade_date == target_date

    avg_cost = _safe_float(getattr(chip_data, "avg_cost", None))
    profit_ratio = _safe_float(getattr(chip_data, "profit_ratio", None))
    cost_70_low = _safe_float(getattr(chip_data, "cost_70_low", None))
    cost_70_high = _safe_float(getattr(chip_data, "cost_70_high", None))
    concentration_70 = _safe_float(getattr(chip_data, "concentration_70", None))
    cost_90_low = _safe_float(getattr(chip_data, "cost_90_low", None))
    cost_90_high = _safe_float(getattr(chip_data, "cost_90_high", None))
    concentration_90 = _safe_float(getattr(chip_data, "concentration_90", None))

    if avg_cost is None or avg_cost <= 0:
        status = "PARTIAL"
        reason = "PROVIDER_AVG_COST_MISSING"
    elif not source:
        status = "PARTIAL"
        reason = "PROVIDER_SOURCE_UNKNOWN"
    elif source_trade_date is None:
        status = "PARTIAL"
        reason = "PROVIDER_TRADE_DATE_UNKNOWN"
    elif not target_date_match:
        status = "PARTIAL"
        reason = "PROVIDER_TARGET_DATE_MISMATCH"
    else:
        status = "READY_CURRENT_ONLY"
        reason = "PROVIDER_SNAPSHOT_TARGET_DATE_MATCH"

    return {
        "status": status,
        "reason": reason,
        "provider": source or None,
        "source_trade_date": source_trade_date.isoformat() if source_trade_date else None,
        "target_date_match": target_date_match,
        "historical_pit_authority": "CURRENT_ONLY",
        "historical_replay_eligible": False,
        "provider_reference_avg_cost": round(avg_cost, 6) if avg_cost is not None else None,
        "provider_profit_ratio": round(profit_ratio, 6) if profit_ratio is not None else None,
        "provider_cost_70_low": round(cost_70_low, 6) if cost_70_low is not None else None,
        "provider_cost_70_high": round(cost_70_high, 6) if cost_70_high is not None else None,
        "provider_concentration_70": round(concentration_70, 6) if concentration_70 is not None else None,
        "provider_cost_90_low": round(cost_90_low, 6) if cost_90_low is not None else None,
        "provider_cost_90_high": round(cost_90_high, 6) if cost_90_high is not None else None,
        "provider_concentration_90": round(concentration_90, 6) if concentration_90 is not None else None,
        "semantics": "PROVIDER_ESTIMATED_CHIP_DISTRIBUTION",
        "institutional_intent_claim": "NOT_INFERRED",
    }


def _relation(provider: Dict[str, Any], bar_reference: Dict[str, Any]) -> Dict[str, Any]:
    refs = []
    bar_sources: set[str] = set()
    for key in ("window_20", "window_60"):
        window = bar_reference.get(key) if isinstance(bar_reference, dict) else None
        if isinstance(window, dict) and window.get("status") == "READY":
            value = _safe_float(window.get("rolling_reference_price"))
            if value is not None and value > 0:
                refs.append(value)
                bar_sources.update(str(source).strip() for source in (window.get("sources") or []) if str(source).strip())

    provider_ready = provider.get("status") == "READY_CURRENT_ONLY"
    if not provider_ready and not refs:
        return {"provider_vs_bar_relation": "NOT_COMPARABLE", "basis": "NO_COMPARABLE_EVIDENCE"}
    if not provider_ready or not refs:
        return {"provider_vs_bar_relation": "SINGLE_SOURCE_ONLY", "basis": "ONLY_ONE_EVIDENCE_LAYER_READY"}

    provider_source = str(provider.get("provider") or "").strip()
    if len(bar_sources) != 1 or provider_source not in bar_sources:
        return {"provider_vs_bar_relation": "NOT_COMPARABLE", "basis": "PROVIDER_BAR_SOURCE_MISMATCH"}

    cost_70_low = _safe_float(provider.get("provider_cost_70_low"))
    cost_70_high = _safe_float(provider.get("provider_cost_70_high"))
    cost_90_low = _safe_float(provider.get("provider_cost_90_low"))
    cost_90_high = _safe_float(provider.get("provider_cost_90_high"))

    if (
        cost_70_low is not None
        and cost_70_high is not None
        and 0 < cost_70_low <= cost_70_high
        and all(cost_70_low <= value <= cost_70_high for value in refs)
    ):
        return {"provider_vs_bar_relation": "CONVERGENT", "basis": "BAR_REFERENCE_INSIDE_PROVIDER_70_COST_BAND"}

    if cost_90_low is not None and cost_90_high is not None and 0 < cost_90_low <= cost_90_high:
        if all(cost_90_low <= value <= cost_90_high for value in refs):
            return {"provider_vs_bar_relation": "CONVERGENT", "basis": "BAR_REFERENCE_INSIDE_PROVIDER_90_COST_BAND"}
        all_below = all(value < cost_90_low for value in refs)
        all_above = all(value > cost_90_high for value in refs)
        if all_below or all_above:
            return {"provider_vs_bar_relation": "DIVERGENT", "basis": "BAR_REFERENCES_OUTSIDE_PROVIDER_90_COST_BAND_SAME_SIDE"}

    return {"provider_vs_bar_relation": "NOT_COMPARABLE", "basis": "NO_NON_ARBITRARY_COMPARISON_RULE"}


def build_cost_structure_context(
    *,
    stock_code: str,
    history: Any,
    chip_data: Any,
    target_date: Optional[date],
    market: Optional[str] = None,
) -> Dict[str, Any]:
    """Build V1 cost-structure evidence without fetching, persistence, or action authority."""
    if not isinstance(target_date, date):
        return {
            "schema_version": COST_STRUCTURE_SCHEMA_VERSION,
            "family": "cost_structure",
            "status": "UNKNOWN",
            "reason": "TARGET_DATE_UNKNOWN",
            "stock_code": stock_code,
            "market": str(market or "").lower() or None,
            "provider_chip_snapshot": _provider_snapshot(chip_data, target_date=date.min),
            "bar_reference_cost": {},
            "composition": {"provider_vs_bar_relation": "NOT_COMPARABLE", "basis": "TARGET_DATE_UNKNOWN"},
            "hard_veto": False,
            "independent_action_authority": False,
        }

    frame = _normalize_history(history, target_date=target_date)
    provider = _provider_snapshot(chip_data, target_date=target_date)
    if frame.empty:
        bar_reference = {
            "status": "MISSING",
            "reason": "COMPLETED_OHLCV_MISSING",
            "semantics": "BAR_DERIVED_REFERENCE_PRICE_NOT_HOLDER_COST",
        }
    elif frame.iloc[-1]["date"] != target_date:
        bar_reference = {
            "status": "MISSING",
            "reason": "TARGET_DATE_BAR_MISSING",
            "semantics": "BAR_DERIVED_REFERENCE_PRICE_NOT_HOLDER_COST",
        }
    else:
        window_20 = _bar_window(frame, sessions=20)
        window_60 = _bar_window(frame, sessions=60)
        if window_60.get("status") == "READY":
            bar_status = "READY"
            bar_reason = "WINDOW_20_60_READY"
        elif window_20.get("status") in {"READY", "PARTIAL"} or window_60.get("status") == "PARTIAL":
            bar_status = "PARTIAL"
            bar_reason = "PARTIAL_WINDOW_OR_SOURCE_ALIGNMENT"
        elif "UNKNOWN" in {window_20.get("status"), window_60.get("status")}:
            bar_status = "UNKNOWN"
            bar_reason = "BAR_REFERENCE_INVALID"
        else:
            bar_status = "MISSING"
            bar_reason = "BAR_REFERENCE_WARMUP_INSUFFICIENT"
        bar_reference = {
            "status": bar_status,
            "reason": bar_reason,
            "semantics": "BAR_DERIVED_REFERENCE_PRICE_NOT_HOLDER_COST",
            "window_20": window_20,
            "window_60": window_60,
        }

    relation = _relation(provider, bar_reference)
    if bar_reference.get("status") == "READY":
        status = "READY"
        reason = "PIT_SAFE_BAR_REFERENCE_READY"
    elif bar_reference.get("status") in {"PARTIAL", "UNKNOWN"}:
        status = bar_reference.get("status")
        reason = str(bar_reference.get("reason") or "BAR_REFERENCE_NOT_READY")
    elif provider.get("status") in {"READY_CURRENT_ONLY", "PARTIAL"}:
        status = "PARTIAL"
        reason = "PROVIDER_CURRENT_ONLY_WITHOUT_PIT_BAR_REFERENCE"
    else:
        status = "MISSING"
        reason = "COST_STRUCTURE_EVIDENCE_MISSING"

    return {
        "schema_version": COST_STRUCTURE_SCHEMA_VERSION,
        "family": "cost_structure",
        "status": status,
        "reason": reason,
        "stock_code": stock_code,
        "market": str(market or "").lower() or None,
        "target_date": target_date.isoformat(),
        "completed_bar_only": True,
        "provider_chip_snapshot": provider,
        "bar_reference_cost": bar_reference,
        "composition": {
            **relation,
            "hard_veto": False,
            "independent_action_authority": False,
        },
        "institutional_intent_inferred": False,
        "hard_veto": False,
        "independent_action_authority": False,
    }
