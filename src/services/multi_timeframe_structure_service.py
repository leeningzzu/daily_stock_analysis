# -*- coding: utf-8 -*-
"""Coverage-aware multi-timeframe structure over canonical completed daily history.

V1 reuses the existing daily history owner, StockTrendAnalyzer and confirmed
Pivot/Swing owner. It does not fetch data, create action authority or pretend
that intraday bars exist. Weekly/monthly bars are deterministic OHLCV
aggregates of already-completed daily bars; the current partial higher-timeframe
period is excluded unless the exchange calendar proves it complete.
"""

from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
from typing import Any, Dict, Optional

import pandas as pd

from src.core.trading_calendar import resolve_completed_timeframe_bar_date
from src.services.price_structure_service import build_price_structure_context


SCHEMA_VERSION = "multi-timeframe-structure-v1"
ALGORITHM_VERSION = "completed-daily-resample-v1"
TREND_MIN_BARS = 26
CROSS_RUN_PERSISTENCE_POLICY = "BLOCK_UNTIL_ADJUSTMENT_BASIS_PERSISTED"

_CONFIG = {
    "algorithm_version": ALGORITHM_VERSION,
    "trend_min_bars": TREND_MIN_BARS,
    "cross_run_persistence_policy": CROSS_RUN_PERSISTENCE_POLICY,
    "weekly_period": "W-FRI",
    "monthly_period": "M",
    "intraday_policy": "MISSING_UNTIL_COMPLETED_BAR_CONTRACT",
}
CONFIG_HASH = sha256(
    json.dumps(_CONFIG, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()

_ROLE = {
    "monthly": "LONG_TERM_CONTEXT",
    "weekly": "PRIMARY_TREND_CONTEXT",
    "daily": "PRIMARY_SETUP",
    "60m": "OPTIONAL_BRIDGE",
    "30m": "PRIMARY_STRUCTURE",
    "15m": "TRIGGER_CONFIRMATION",
    "5m": "MICRO_TIMING",
}


def _normalize_daily_history(history: Any, *, target_date: date) -> pd.DataFrame:
    required = ["date", "open", "high", "low", "close", "volume"]
    if not isinstance(history, pd.DataFrame) or history.empty:
        return pd.DataFrame(columns=required)
    frame = history.copy()
    frame.columns = [str(column).lower() for column in frame.columns]
    if any(column not in frame.columns for column in required):
        return pd.DataFrame(columns=required)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=required)
    frame = frame[frame["date"] <= target_date]
    frame = frame.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    columns = required + (["data_source"] if "data_source" in frame.columns else [])
    return frame[columns].reset_index(drop=True)


def _invalid_ohlcv(frame: pd.DataFrame) -> bool:
    if frame.empty:
        return False
    return bool(
        (
            (frame["open"] <= 0)
            | (frame["high"] <= 0)
            | (frame["low"] <= 0)
            | (frame["close"] <= 0)
            | (frame["volume"] < 0)
            | (frame["high"] < frame["low"])
            | (frame["high"] < frame["open"])
            | (frame["high"] < frame["close"])
            | (frame["low"] > frame["open"])
            | (frame["low"] > frame["close"])
        ).any()
    )


def _source_alignment(frame: pd.DataFrame) -> Dict[str, Any]:
    if "data_source" not in frame.columns:
        return {"status": "UNPROVEN", "sources": [], "rows_complete": False}
    text = frame["data_source"].map(
        lambda value: str(value).strip() if value not in (None, "") else ""
    )
    sources = sorted({value for value in text.tolist() if value})
    complete = bool((text != "").all())
    return {
        "status": "SINGLE_SOURCE" if len(sources) == 1 and complete else "UNPROVEN",
        "sources": sources,
        "rows_complete": complete,
    }


def _aggregate_completed_bars(
    frame: pd.DataFrame,
    *,
    completed_through: date,
    period_freq: str,
) -> pd.DataFrame:
    eligible = frame[frame["date"] <= completed_through].copy()
    if eligible.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    eligible["_period"] = pd.to_datetime(eligible["date"]).dt.to_period(period_freq)
    records = []
    for _, group in eligible.groupby("_period", sort=True):
        group = group.sort_values("date")
        record = {
            "date": group.iloc[-1]["date"],
            "open": float(group.iloc[0]["open"]),
            "high": float(group["high"].max()),
            "low": float(group["low"].min()),
            "close": float(group.iloc[-1]["close"]),
            "volume": float(group["volume"].sum()),
        }
        if "data_source" in group.columns:
            sources = [
                str(value).strip()
                for value in group["data_source"].tolist()
                if str(value).strip()
            ]
            if sources and len(sources) == len(group) and len(set(sources)) == 1:
                record["data_source"] = sources[0]
        records.append(record)
    return pd.DataFrame(records)


def _trend_direction(trend_status: Any) -> str:
    text = str(getattr(trend_status, "value", trend_status) or "")
    if "多头" in text:
        return "BULLISH"
    if "空头" in text:
        return "BEARISH"
    if "盘整" in text:
        return "NEUTRAL"
    return "UNKNOWN"


def _trend_projection(result: Any) -> Dict[str, Any]:
    return {
        "status": "READY",
        "trend_status": str(getattr(getattr(result, "trend_status", None), "value", "")) or None,
        "direction": _trend_direction(getattr(result, "trend_status", None)),
        "ma_alignment": str(getattr(result, "ma_alignment", "") or "") or None,
        "trend_strength": getattr(result, "trend_strength", None),
        "ma5": getattr(result, "ma5", None),
        "ma10": getattr(result, "ma10", None),
        "ma20": getattr(result, "ma20", None),
        "volume_status": str(getattr(getattr(result, "volume_status", None), "value", "")) or None,
        "volume_ratio_5bar": getattr(result, "volume_ratio_5d", None),
        "macd_status": str(getattr(getattr(result, "macd_status", None), "value", "")) or None,
        "macd_signal": str(getattr(result, "macd_signal", "") or "") or None,
        "rsi_status": str(getattr(getattr(result, "rsi_status", None), "value", "")) or None,
        "rsi_signal": str(getattr(result, "rsi_signal", "") or "") or None,
        "independent_action_authority": False,
        "signal_score_consumed": False,
        "buy_signal_consumed": False,
    }


def _structure_text(context: Dict[str, Any]) -> Optional[str]:
    event = context.get("structure_event") if isinstance(context, dict) else None
    state = str((event or {}).get("state") or "") if isinstance(event, dict) else ""
    labels = {
        "UP_BREAKOUT": "结构向上突破已确认",
        "UP_BREAKOUT_RETEST_HOLD": "突破后回踩仍守住结构位",
        "FAILED_UP_BREAKOUT": "向上突破失败并回到结构位下方",
        "DOWN_BREAKOUT": "结构向下跌破已确认",
        "DOWN_BREAKOUT_RETEST_HOLD": "跌破后反抽未收复结构位",
        "FAILED_DOWN_BREAKOUT": "向下跌破失败并重新收复结构位",
    }
    return labels.get(state)


def _human_summary(trend: Optional[Dict[str, Any]], structure: Dict[str, Any]) -> Optional[str]:
    parts = []
    if isinstance(trend, dict):
        if trend.get("ma_alignment"):
            parts.append(str(trend["ma_alignment"]))
        if trend.get("volume_status"):
            parts.append(str(trend["volume_status"]))
        signal = str(trend.get("macd_signal") or "").strip()
        if signal and signal != "数据不足":
            parts.append(signal.lstrip("⭐⚡✅❌⚠✓ "))
    structure_text = _structure_text(structure)
    if structure_text:
        parts.append(structure_text)
    return "；".join(parts[:3]) or None


def _empty_timeframe(key: str, *, status: str, reason: str) -> Dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "role": _ROLE[key],
        "summary": None,
        "independent_action_authority": False,
    }


def _build_higher_timeframe(
    *,
    key: str,
    timeframe: str,
    period_freq: str,
    stock_code: str,
    market: Optional[str],
    target_date: date,
    daily_frame: pd.DataFrame,
    trend_analyzer: Any,
) -> Dict[str, Any]:
    completed_through = resolve_completed_timeframe_bar_date(market, target_date, timeframe)
    if completed_through is None:
        return _empty_timeframe(key, status="MISSING", reason="COMPLETED_PERIOD_UNPROVEN")
    bars = _aggregate_completed_bars(
        daily_frame,
        completed_through=completed_through,
        period_freq=period_freq,
    )
    if bars.empty:
        return _empty_timeframe(key, status="MISSING", reason="COMPLETED_BARS_MISSING")
    latest_bar_date = bars.iloc[-1]["date"]
    base = {
        "role": _ROLE[key],
        "timeframe": timeframe,
        "completed_bar_only": True,
        "completed_through": completed_through.isoformat(),
        "latest_bar_date": latest_bar_date.isoformat(),
        "observations": int(len(bars)),
        "source_alignment": _source_alignment(bars),
        "algorithm_version": ALGORITHM_VERSION,
        "config_hash": CONFIG_HASH,
        "independent_action_authority": False,
    }
    if latest_bar_date != completed_through:
        return {
            **base,
            "status": "PARTIAL",
            "reason": "COMPLETED_PERIOD_END_BAR_MISSING",
            "summary": None,
            "historical_replay_eligible": False,
        }
    if base["source_alignment"]["status"] != "SINGLE_SOURCE":
        return {
            **base,
            "status": "PARTIAL",
            "reason": "SOURCE_ALIGNMENT_UNPROVEN",
            "summary": None,
            "historical_replay_eligible": False,
        }

    structure = build_price_structure_context(
        stock_code=stock_code,
        history=bars,
        target_date=latest_bar_date,
        market=market,
    )
    trend = None
    if len(bars) >= TREND_MIN_BARS:
        trend = _trend_projection(trend_analyzer.analyze(bars.copy(), stock_code))

    structure_ready = structure.get("status") == "READY"
    trend_ready = trend is not None
    if trend_ready and structure_ready:
        status, reason = "READY", "TIMEFRAME_READY"
    elif trend_ready or structure_ready:
        status, reason = "PARTIAL", "TIMEFRAME_PARTIAL_EVIDENCE"
    else:
        status, reason = "MISSING", "WARMUP_INSUFFICIENT"
    return {
        **base,
        "status": status,
        "reason": reason,
        "trend": trend or {"status": "MISSING", "reason": "WARMUP_INSUFFICIENT"},
        "price_structure": structure,
        "summary": _human_summary(trend, structure),
        "historical_replay_eligible": bool(
            structure.get("historical_replay_eligible") and status in {"READY", "PARTIAL"}
        ),
    }


def build_multi_timeframe_structure_context(
    *,
    stock_code: str,
    history: Any,
    target_date: Optional[date],
    market: Optional[str],
    trend_analyzer: Any,
    daily_trend_result: Any = None,
    daily_price_structure_context: Any = None,
) -> Dict[str, Any]:
    """Build one coverage-aware M/W/D structure carrier from completed daily bars."""
    base = {
        "schema_version": SCHEMA_VERSION,
        "family": "multi_timeframe_structure",
        "stock_code": stock_code,
        "market": str(market or "").lower() or None,
        "completed_bar_only": True,
        "algorithm_version": ALGORITHM_VERSION,
        "config_hash": CONFIG_HASH,
        "independent_action_authority": False,
        "cross_timeframe_vote_counting": False,
        # StockDaily V1 stores provider name but not the exact adjustment basis.
        # Per-run evidence can still be replay-safe, but cross-run persistent
        # history must remain ineligible until adjustment_basis is explicit.
        "cross_run_persistence_policy": CROSS_RUN_PERSISTENCE_POLICY,
        "cross_run_persistence_eligible": False,
        "cross_run_persistence_reason": "ADJUSTMENT_BASIS_NOT_PERSISTED",
    }
    missing_intraday = {
        key: _empty_timeframe(
            key,
            status="MISSING",
            reason="INTRADAY_COMPLETED_BAR_CONTRACT_NOT_READY",
        )
        for key in ("60m", "30m", "15m", "5m")
    }
    if not isinstance(target_date, date):
        return {
            **base,
            "status": "UNKNOWN",
            "reason": "TARGET_DATE_UNKNOWN",
            "timeframes": {
                "monthly": _empty_timeframe("monthly", status="MISSING", reason="TARGET_DATE_UNKNOWN"),
                "weekly": _empty_timeframe("weekly", status="MISSING", reason="TARGET_DATE_UNKNOWN"),
                "daily": _empty_timeframe("daily", status="MISSING", reason="TARGET_DATE_UNKNOWN"),
                **missing_intraday,
            },
            "historical_replay_eligible": False,
        }

    frame = _normalize_daily_history(history, target_date=target_date)
    base["target_date"] = target_date.isoformat()
    if frame.empty or frame.iloc[-1]["date"] != target_date:
        reason = "COMPLETED_DAILY_HISTORY_MISSING" if frame.empty else "TARGET_DATE_BAR_MISSING"
        return {
            **base,
            "status": "MISSING",
            "reason": reason,
            "timeframes": {
                "monthly": _empty_timeframe("monthly", status="MISSING", reason=reason),
                "weekly": _empty_timeframe("weekly", status="MISSING", reason=reason),
                "daily": _empty_timeframe("daily", status="MISSING", reason=reason),
                **missing_intraday,
            },
            "historical_replay_eligible": False,
        }
    if _invalid_ohlcv(frame):
        return {
            **base,
            "status": "UNKNOWN",
            "reason": "INVALID_OHLCV",
            "timeframes": {
                "monthly": _empty_timeframe("monthly", status="UNKNOWN", reason="INVALID_OHLCV"),
                "weekly": _empty_timeframe("weekly", status="UNKNOWN", reason="INVALID_OHLCV"),
                "daily": _empty_timeframe("daily", status="UNKNOWN", reason="INVALID_OHLCV"),
                **missing_intraday,
            },
            "historical_replay_eligible": False,
        }

    alignment = _source_alignment(frame)
    daily_summary = None
    daily_direction = "UNKNOWN"
    if daily_trend_result is not None:
        daily_direction = _trend_direction(getattr(daily_trend_result, "trend_status", None))
        daily_summary = str(getattr(daily_trend_result, "ma_alignment", "") or "") or None
    daily_structure = (
        daily_price_structure_context
        if isinstance(daily_price_structure_context, dict)
        else {}
    )
    daily = {
        "status": "READY"
        if daily_trend_result is not None and alignment["status"] == "SINGLE_SOURCE"
        else "PARTIAL",
        "reason": "DAILY_REFERENCE_READY"
        if daily_trend_result is not None
        else "DAILY_TREND_MISSING",
        "role": _ROLE["daily"],
        "timeframe": "1d",
        "completed_bar_only": True,
        "completed_through": target_date.isoformat(),
        "latest_bar_date": target_date.isoformat(),
        "observations": int(len(frame)),
        "source_alignment": alignment,
        "trend": {"status": "READY", "direction": daily_direction}
        if daily_trend_result is not None
        else {"status": "MISSING"},
        "price_structure": daily_structure,
        "summary": daily_summary,
        "independent_action_authority": False,
        "historical_replay_eligible": bool(
            daily_structure.get("historical_replay_eligible")
        ),
    }
    monthly = _build_higher_timeframe(
        key="monthly",
        timeframe="1mo",
        period_freq="M",
        stock_code=stock_code,
        market=market,
        target_date=target_date,
        daily_frame=frame,
        trend_analyzer=trend_analyzer,
    )
    weekly = _build_higher_timeframe(
        key="weekly",
        timeframe="1w",
        period_freq="W-FRI",
        stock_code=stock_code,
        market=market,
        target_date=target_date,
        daily_frame=frame,
        trend_analyzer=trend_analyzer,
    )

    directions = []
    for item in (monthly, weekly, daily):
        trend = item.get("trend") if isinstance(item, dict) else None
        direction = (
            str((trend or {}).get("direction") or "UNKNOWN")
            if isinstance(trend, dict)
            else "UNKNOWN"
        )
        if direction in {"BULLISH", "BEARISH", "NEUTRAL"}:
            directions.append(direction)
    if not directions:
        alignment_state = "DATA_INSUFFICIENT"
    elif len(set(directions)) == 1:
        alignment_state = f"ALIGNED_{directions[0]}"
    elif len(directions) == 1:
        alignment_state = "SINGLE_TIMEFRAME"
    else:
        alignment_state = "MIXED"

    ready_high = [
        item for item in (monthly, weekly) if item.get("status") in {"READY", "PARTIAL"}
    ]
    return {
        **base,
        "status": "READY" if ready_high else "PARTIAL",
        "reason": "MTF_HIGHER_TIMEFRAME_AVAILABLE"
        if ready_high
        else "HIGHER_TIMEFRAME_NOT_READY",
        "source_alignment": alignment,
        "timeframes": {
            "monthly": monthly,
            "weekly": weekly,
            "daily": daily,
            **missing_intraday,
        },
        "cross_timeframe": {
            "trend_alignment": alignment_state,
            "rule": "CONFIRMATION_OR_CONFLICT_NOT_INDEPENDENT_VOTES",
        },
        "historical_replay_eligible": bool(
            daily.get("historical_replay_eligible")
            and all(item.get("historical_replay_eligible") for item in ready_high)
        )
        if ready_high
        else False,
    }
