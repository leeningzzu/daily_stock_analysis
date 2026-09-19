# -*- coding: utf-8 -*-
"""Pure completed-bar supply/demand and volume-price evidence.

The first slice intentionally uses only persisted OHLCV bars. It does not fetch
providers, infer institutional intent, persist state, or own action authority.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

import pandas as pd


SUPPLY_DEMAND_SCHEMA_VERSION = "supply-demand-volume-price-v1"
WINDOW_SESSIONS = 20
REQUIRED_OBSERVATIONS = WINDOW_SESSIONS + 1


def _status_payload(*, status: str, reason: str, stock_code: str, target_date: Optional[date]) -> Dict[str, Any]:
    return {
        "schema_version": SUPPLY_DEMAND_SCHEMA_VERSION,
        "family": "supply_demand_volume_price",
        "status": status,
        "reason": reason,
        "stock_code": stock_code,
        "target_date": target_date.isoformat() if isinstance(target_date, date) else None,
        "window_sessions": WINDOW_SESSIONS,
        "state": "UNKNOWN",
        "relative_volume": {},
        "directional_volume": {},
        "close_location_flow": {},
        "data_quality": {},
        "institutional_intent_claim": "NOT_INFERRED",
    }


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


def build_supply_demand_context(
    *,
    stock_code: str,
    history: Any,
    target_date: Optional[date],
) -> Dict[str, Any]:
    """Build auditable 20-session pressure metrics from completed OHLCV bars."""
    if not isinstance(target_date, date):
        return _status_payload(status="UNKNOWN", reason="TARGET_DATE_UNKNOWN", stock_code=stock_code, target_date=None)

    frame = _normalize_history(history, target_date=target_date)
    if frame.empty:
        return _status_payload(status="MISSING", reason="COMPLETED_OHLCV_MISSING", stock_code=stock_code, target_date=target_date)
    if frame.iloc[-1]["date"] != target_date:
        return _status_payload(status="MISSING", reason="TARGET_DATE_BAR_MISSING", stock_code=stock_code, target_date=target_date)
    if len(frame) < REQUIRED_OBSERVATIONS:
        return _status_payload(status="MISSING", reason="WARMUP_INSUFFICIENT", stock_code=stock_code, target_date=target_date)

    window = frame.tail(REQUIRED_OBSERVATIONS).reset_index(drop=True)
    metric_window = window.tail(WINDOW_SESSIONS).reset_index(drop=True)
    previous_20 = window.iloc[:-1].tail(WINDOW_SESSIONS)

    invalid_price = (
        (metric_window["high"] <= 0)
        | (metric_window["low"] <= 0)
        | (metric_window["close"] <= 0)
        | (metric_window["high"] < metric_window["low"])
        | (metric_window["close"] > metric_window["high"])
        | (metric_window["close"] < metric_window["low"])
    )
    if bool(invalid_price.any()) or bool((metric_window["volume"] < 0).any()):
        return _status_payload(status="UNKNOWN", reason="INVALID_OHLCV", stock_code=stock_code, target_date=target_date)
    if bool((metric_window["volume"] <= 0).any()) or float(previous_20["volume"].sum()) <= 0:
        return _status_payload(status="UNKNOWN", reason="NON_POSITIVE_VOLUME", stock_code=stock_code, target_date=target_date)

    previous_mean_volume = float(previous_20["volume"].mean())
    latest_volume = float(window.iloc[-1]["volume"])
    volume_ratio_20d = latest_volume / previous_mean_volume if previous_mean_volume > 0 else None

    recent_5_avg = float(window["volume"].tail(5).mean())
    prior_5_avg = float(window["volume"].iloc[-10:-5].mean())
    recent_vs_prior_5d_pct = (
        (recent_5_avg / prior_5_avg - 1.0) * 100.0 if prior_5_avg > 0 else None
    )

    close_diff = window["close"].diff().iloc[1:]
    directional_volume = window["volume"].iloc[1:]
    up_volume = float(directional_volume[close_diff > 0].sum())
    down_volume = float(directional_volume[close_diff < 0].sum())
    directional_total = up_volume + down_volume
    signed_volume_balance = (
        (up_volume - down_volume) / directional_total if directional_total > 0 else 0.0
    )
    up_count = int((close_diff > 0).sum())
    down_count = int((close_diff < 0).sum())
    avg_up_volume = up_volume / up_count if up_count else 0.0
    avg_down_volume = down_volume / down_count if down_count else 0.0

    spread = metric_window["high"] - metric_window["low"]
    multiplier = pd.Series(0.0, index=metric_window.index)
    nonzero_spread = spread > 0
    multiplier.loc[nonzero_spread] = (
        ((metric_window.loc[nonzero_spread, "close"] - metric_window.loc[nonzero_spread, "low"])
         - (metric_window.loc[nonzero_spread, "high"] - metric_window.loc[nonzero_spread, "close"]))
        / spread.loc[nonzero_spread]
    )
    total_volume = float(metric_window["volume"].sum())
    cmf_20 = float((multiplier * metric_window["volume"]).sum() / total_volume) if total_volume > 0 else 0.0

    if signed_volume_balance > 0 and cmf_20 > 0:
        state = "DEMAND_PRESSURE"
    elif signed_volume_balance < 0 and cmf_20 < 0:
        state = "SUPPLY_PRESSURE"
    elif signed_volume_balance == 0 and cmf_20 == 0:
        state = "BALANCED"
    else:
        state = "CONFLICT"

    source_values: set[str] = set()
    source_rows_complete = False
    if "data_source" in window.columns:
        source_text = window["data_source"].map(lambda value: str(value).strip() if value not in (None, "") else "")
        source_values = {value for value in source_text.tolist() if value}
        source_rows_complete = bool((source_text != "").all())
    source_alignment = "SINGLE_SOURCE" if len(source_values) == 1 and source_rows_complete else "UNPROVEN"
    status = "READY" if source_alignment == "SINGLE_SOURCE" else "PARTIAL"
    reason = "COMPLETED_OHLCV_SINGLE_SOURCE" if status == "READY" else "SOURCE_ALIGNMENT_UNPROVEN"

    return {
        "schema_version": SUPPLY_DEMAND_SCHEMA_VERSION,
        "family": "supply_demand_volume_price",
        "status": status,
        "reason": reason,
        "stock_code": stock_code,
        "target_date": target_date.isoformat(),
        "window_sessions": WINDOW_SESSIONS,
        "state": state,
        "relative_volume": {
            "volume_ratio_20d": round(volume_ratio_20d, 6) if volume_ratio_20d is not None else None,
            "recent_5d_avg": round(recent_5_avg, 6),
            "prior_5d_avg": round(prior_5_avg, 6),
            "recent_vs_prior_5d_pct": round(recent_vs_prior_5d_pct, 6) if recent_vs_prior_5d_pct is not None else None,
            "correlation_group": "relative_volume",
        },
        "directional_volume": {
            "signed_volume_balance": round(signed_volume_balance, 6),
            "avg_up_day_volume": round(avg_up_volume, 6),
            "avg_down_day_volume": round(avg_down_volume, 6),
            "up_days": up_count,
            "down_days": down_count,
            "correlation_group": "directional_volume",
        },
        "close_location_flow": {
            "cmf_20": round(cmf_20, 6),
            "correlation_group": "close_location_flow",
        },
        "data_quality": {
            "source_alignment": source_alignment,
            "sources": sorted(source_values),
            "observations": len(window),
            "start_date": window.iloc[0]["date"].isoformat(),
            "end_date": window.iloc[-1]["date"].isoformat(),
        },
        "institutional_intent_claim": "NOT_INFERRED",
    }
