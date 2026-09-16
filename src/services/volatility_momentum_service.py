# -*- coding: utf-8 -*-
"""Deterministic volatility/momentum evidence over completed daily bars.

V1 reuses DSA's existing MACD/RSI formulas and the confirmed Pivot/Swing owner.
Process diagnostics are observational only: they do not classify a market "world",
automatically switch strategies, or create independent action authority.
"""

from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
from math import isfinite
from typing import Any, Dict, List, Optional

import pandas as pd


VOLATILITY_MOMENTUM_SCHEMA_VERSION = "volatility-momentum-v1"
ALGORITHM_VERSION = "volatility-momentum-v1"
MIN_OBSERVATIONS = 21
READY_OBSERVATIONS = 61
SOURCE_ALIGNMENT_WINDOW = 61
DIAGNOSTIC_RETURN_WINDOW = 60
VARIANCE_SCALE = 5

_CONFIG = {
    "algorithm_version": ALGORITHM_VERSION,
    "minimum_observations": MIN_OBSERVATIONS,
    "ready_observations": READY_OBSERVATIONS,
    "source_alignment_window": SOURCE_ALIGNMENT_WINDOW,
    "diagnostic_return_window": DIAGNOSTIC_RETURN_WINDOW,
    "variance_scale": VARIANCE_SCALE,
    "roc_windows": [20, 60],
    "realized_volatility_window": 20,
    "true_range_sma_window": 20,
    "divergence_oscillators": ["MACD_DIF", "RSI_12"],
}
CONFIG_HASH = sha256(
    json.dumps(_CONFIG, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


def _safe_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


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
    frame = frame.dropna(subset=required)
    frame = frame[frame["date"] <= target_date]
    frame = frame.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    columns = required + (["data_source"] if "data_source" in frame.columns else [])
    return frame[columns].reset_index(drop=True)


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
    text = window["data_source"].map(
        lambda value: str(value).strip() if value not in (None, "") else ""
    )
    sources = sorted({value for value in text.tolist() if value})
    complete = bool((text != "").all())
    return {
        "status": "SINGLE_SOURCE" if len(sources) == 1 and complete else "UNPROVEN",
        "sources": sources,
        "rows_complete": complete,
    }


def _indicator_frame(frame: pd.DataFrame) -> pd.DataFrame:
    # Reuse the exact Production report formulas instead of introducing a parallel TA implementation.
    from src.stock_analyzer import StockTrendAnalyzer

    analyzer = StockTrendAnalyzer()
    calculated = analyzer._calculate_macd(frame.copy())
    return analyzer._calculate_rsi(calculated)


def _roc_pct(close: pd.Series, sessions: int) -> Optional[float]:
    values = pd.to_numeric(close, errors="coerce").dropna()
    if len(values) <= sessions:
        return None
    base = float(values.iloc[-sessions - 1])
    latest = float(values.iloc[-1])
    if base <= 0:
        return None
    return (latest / base - 1.0) * 100.0


def _realized_volatility_20d_pct(close: pd.Series) -> Optional[float]:
    returns = pd.to_numeric(close, errors="coerce").pct_change().dropna().tail(20)
    if len(returns) < 2:
        return None
    value = float(returns.std(ddof=1)) * (252 ** 0.5) * 100.0
    return value if isfinite(value) else None


def _true_range_sma_20_pct(frame: pd.DataFrame) -> Optional[float]:
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1)
    tr_sma = true_range.tail(20).dropna().mean()
    last_close = _safe_float(close.dropna().iloc[-1]) if not close.dropna().empty else None
    if pd.isna(tr_sma) or last_close is None or last_close <= 0:
        return None
    return float(tr_sma) / last_close * 100.0


def _lag1_corr(series: pd.Series) -> Optional[float]:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if len(values) < 3:
        return None
    current = values.iloc[1:].reset_index(drop=True)
    previous = values.iloc[:-1].reset_index(drop=True)
    value = current.corr(previous)
    return _safe_float(value)


def _variance_scaling_ratio_5(close: pd.Series) -> Optional[float]:
    values = pd.to_numeric(close, errors="coerce").dropna()
    one = values.pct_change().dropna().tail(DIAGNOSTIC_RETURN_WINDOW)
    five = values.pct_change(VARIANCE_SCALE).dropna().tail(DIAGNOSTIC_RETURN_WINDOW)
    if len(one) < 10 or len(five) < 5:
        return None
    one_var = float(one.var(ddof=1))
    five_var = float(five.var(ddof=1))
    if not isfinite(one_var) or not isfinite(five_var) or one_var <= 0:
        return None
    return five_var / (VARIANCE_SCALE * one_var)


def _process_diagnostics(close: pd.Series) -> Dict[str, Any]:
    returns = pd.to_numeric(close, errors="coerce").pct_change().dropna().tail(DIAGNOSTIC_RETURN_WINDOW)
    autocorr = _lag1_corr(returns)
    abs_autocorr = _lag1_corr(returns.abs())
    skew = _safe_float(returns.skew()) if len(returns) >= 3 else None
    excess_kurtosis = _safe_float(returns.kurt()) if len(returns) >= 4 else None
    tail_event_count = None
    if len(returns) >= 10:
        std = _safe_float(returns.std(ddof=1))
        mean = _safe_float(returns.mean())
        if std is not None and mean is not None and std > 0:
            tail_event_count = int(((returns - mean).abs() > 3.0 * std).sum())
    return {
        "timeframe": "1d",
        "window_returns": int(len(returns)),
        "return_autocorr_lag1": autocorr,
        "variance_scaling_ratio_5": _variance_scaling_ratio_5(close),
        "variance_scaling_semantics": "DESCRIPTIVE_OVERLAPPING_5_SESSION_RATIO_NOT_SIGNIFICANCE_TEST",
        "abs_return_autocorr_lag1": abs_autocorr,
        "skew_60": skew,
        "excess_kurtosis_60": excess_kurtosis,
        "tail_event_count_abs_3sigma": tail_event_count,
        "classification_authority": "OBSERVATIONAL_ONLY",
        "world_classification": "NOT_PERFORMED",
        "automatic_strategy_switching": False,
        "deferred_models": ["ADF_STATIONARITY", "OU_HALF_LIFE", "GARCH", "HMM_REGIME_SWITCHING"],
    }


def _pivot_pair(price_structure_context: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(price_structure_context, dict):
        return None
    if str(price_structure_context.get("schema_version") or "") != "price-structure-v1":
        return None
    if str(price_structure_context.get("status") or "").upper() != "READY":
        return None
    if price_structure_context.get("historical_replay_eligible") is not True:
        return None
    candidates: List[Dict[str, Any]] = []
    pivots = [item for item in price_structure_context.get("pivots", []) if isinstance(item, dict)]
    for kind in ("LOW", "HIGH"):
        same_kind = [
            item
            for item in pivots
            if item.get("kind") == kind
            and not bool(item.get("provisional"))
            and item.get("origin_time")
            and item.get("confirmed_at")
            and _safe_float(item.get("price")) is not None
        ]
        same_kind.sort(key=lambda item: (str(item["confirmed_at"]), str(item["origin_time"])))
        if len(same_kind) >= 2:
            candidates.append({"kind": kind, "first": same_kind[-2], "second": same_kind[-1]})
    if not candidates:
        return None
    return max(candidates, key=lambda item: str(item["second"]["confirmed_at"]))


def _confirmed_divergence(indicators: pd.DataFrame, price_structure_context: Any) -> Dict[str, Any]:
    pair = _pivot_pair(price_structure_context)
    if pair is None:
        return {
            "status": "MISSING",
            "state": "NONE",
            "reason": "CONFIRMED_SAME_KIND_PIVOT_PAIR_MISSING",
            "signals": [],
            "independent_confirmation_group_count": 0,
        }
    first = pair["first"]
    second = pair["second"]
    first_date = pd.to_datetime(first["origin_time"], errors="coerce").date()
    second_date = pd.to_datetime(second["origin_time"], errors="coerce").date()
    indexed = indicators.copy()
    indexed["date"] = pd.to_datetime(indexed["date"], errors="coerce").dt.date
    first_rows = indexed[indexed["date"] == first_date]
    second_rows = indexed[indexed["date"] == second_date]
    if first_rows.empty or second_rows.empty:
        return {
            "status": "MISSING",
            "state": "NONE",
            "reason": "PIVOT_ORIGIN_INDICATOR_VALUE_MISSING",
            "signals": [],
            "independent_confirmation_group_count": 0,
        }

    first_price = float(first["price"])
    second_price = float(second["price"])
    if pair["kind"] == "LOW":
        price_extension = second_price < first_price
        divergence_state = "BULLISH_DIVERGENCE"
        oscillator_diverges = lambda a, b: b > a
    else:
        price_extension = second_price > first_price
        divergence_state = "BEARISH_DIVERGENCE"
        oscillator_diverges = lambda a, b: b < a

    group = (
        f"same_swing:{pair['kind'].lower()}:"
        f"{first['origin_time']}:{second['origin_time']}"
    )
    signals: List[Dict[str, Any]] = []
    if price_extension:
        for oscillator in ("MACD_DIF", "RSI_12"):
            first_value = _safe_float(first_rows.iloc[-1].get(oscillator))
            second_value = _safe_float(second_rows.iloc[-1].get(oscillator))
            if first_value is None or second_value is None:
                continue
            if oscillator_diverges(first_value, second_value):
                signals.append(
                    {
                        "oscillator": oscillator,
                        "state": divergence_state,
                        "first_value": first_value,
                        "second_value": second_value,
                        "correlation_group": group,
                    }
                )
    return {
        "status": "READY",
        "state": divergence_state if signals else "NONE",
        "price_pivot_kind": pair["kind"],
        "first_origin_time": first["origin_time"],
        "second_origin_time": second["origin_time"],
        "confirmed_at": second["confirmed_at"],
        "provisional": False,
        "correlation_group": group,
        "signals": signals,
        "independent_confirmation_group_count": 1 if signals else 0,
        "same_swing_double_counting": "PROHIBITED",
    }


def _empty_payload(base: Dict[str, Any], *, status: str, reason: str, observations: int = 0) -> Dict[str, Any]:
    return {
        **base,
        "status": status,
        "reason": reason,
        "observations": observations,
        "volatility": {"status": "MISSING"},
        "momentum": {"status": "MISSING"},
        "confirmed_divergence": {"status": "MISSING", "state": "NONE", "signals": []},
        "process_diagnostics": {"status": "MISSING", "classification_authority": "NONE"},
        "historical_replay_eligible": False,
    }


def build_volatility_momentum_context(
    *,
    stock_code: str,
    history: Any,
    target_date: Optional[date],
    market: Optional[str] = None,
    trend_result: Any = None,
    price_structure_context: Any = None,
) -> Dict[str, Any]:
    """Build daily volatility/momentum evidence without fetching or action authority."""
    base = {
        "schema_version": VOLATILITY_MOMENTUM_SCHEMA_VERSION,
        "family": "volatility_momentum",
        "stock_code": stock_code,
        "market": str(market or "").lower() or None,
        "timeframe": "1d",
        "completed_bar_only": True,
        "algorithm_version": ALGORITHM_VERSION,
        "config_hash": CONFIG_HASH,
        "hard_veto": False,
        "independent_action_authority": False,
        "automatic_strategy_switching": False,
    }
    if not isinstance(target_date, date):
        return _empty_payload(base, status="UNKNOWN", reason="TARGET_DATE_UNKNOWN")

    frame = _normalize_history(history, target_date=target_date)
    base["target_date"] = target_date.isoformat()
    if frame.empty:
        return _empty_payload(base, status="MISSING", reason="COMPLETED_OHLC_MISSING")
    if frame.iloc[-1]["date"] != target_date:
        return _empty_payload(
            base, status="MISSING", reason="TARGET_DATE_BAR_MISSING", observations=int(len(frame))
        )
    if len(frame) < MIN_OBSERVATIONS:
        return _empty_payload(
            base, status="MISSING", reason="WARMUP_INSUFFICIENT", observations=int(len(frame))
        )
    if _invalid_ohlc(frame):
        return _empty_payload(
            base, status="UNKNOWN", reason="INVALID_OHLC", observations=int(len(frame))
        )

    alignment = _source_alignment(frame)
    indicators = _indicator_frame(frame)
    close = indicators["close"]
    realized_vol = _realized_volatility_20d_pct(close)
    tr_sma_pct = _true_range_sma_20_pct(indicators)
    roc20 = _roc_pct(close, 20)
    roc60 = _roc_pct(close, 60)

    macd_status = getattr(getattr(trend_result, "macd_status", None), "value", None)
    rsi_status = getattr(getattr(trend_result, "rsi_status", None), "value", None)
    latest = indicators.iloc[-1]
    momentum = {
        "status": "READY" if trend_result is not None else "PARTIAL",
        "roc_20_pct": roc20,
        "roc_60_pct": roc60,
        "macd": {
            "dif": _safe_float(latest.get("MACD_DIF")),
            "dea": _safe_float(latest.get("MACD_DEA")),
            "bar": _safe_float(latest.get("MACD_BAR")),
            "status": macd_status,
            "formula_owner": "StockTrendAnalyzer._calculate_macd",
        },
        "rsi": {
            "rsi_6": _safe_float(latest.get("RSI_6")),
            "rsi_12": _safe_float(latest.get("RSI_12")),
            "rsi_24": _safe_float(latest.get("RSI_24")),
            "status": rsi_status,
            "formula_owner": "StockTrendAnalyzer._calculate_rsi",
        },
    }
    volatility = {
        "status": "READY" if realized_vol is not None and tr_sma_pct is not None else "PARTIAL",
        "realized_volatility_20d_annualized_pct": realized_vol,
        "true_range_sma_20_pct": tr_sma_pct,
        "true_range_semantics": "SIMPLE_MEAN_TRUE_RANGE_20_NOT_WILDER_ATR",
        "legacy_screening_alias_note": "screening atr_20_pct uses the same TR-SMA/close formula",
    }
    process = _process_diagnostics(close)
    process["status"] = "READY" if len(frame) >= READY_OBSERVATIONS else "PARTIAL"
    structure_target = (
        str(price_structure_context.get("target_date") or "")
        if isinstance(price_structure_context, dict)
        else ""
    )
    if structure_target != target_date.isoformat():
        divergence = {
            "status": "MISSING",
            "state": "NONE",
            "reason": "PRICE_STRUCTURE_TARGET_DATE_UNPROVEN",
            "signals": [],
            "independent_confirmation_group_count": 0,
        }
    else:
        divergence = _confirmed_divergence(indicators, price_structure_context)

    if alignment["status"] != "SINGLE_SOURCE":
        status = "PARTIAL"
        reason = "SOURCE_ALIGNMENT_UNPROVEN"
    elif len(frame) < READY_OBSERVATIONS:
        status = "PARTIAL"
        reason = "READY_WARMUP_INSUFFICIENT"
    elif trend_result is None:
        status = "PARTIAL"
        reason = "CANONICAL_TREND_PROJECTION_MISSING"
    else:
        status = "READY"
        reason = "VOLATILITY_MOMENTUM_READY"

    return {
        **base,
        "status": status,
        "reason": reason,
        "observations": int(len(frame)),
        "start_date": frame.iloc[0]["date"].isoformat(),
        "end_date": frame.iloc[-1]["date"].isoformat(),
        "source_alignment": alignment,
        "volatility": volatility,
        "momentum": momentum,
        "confirmed_divergence": divergence,
        "process_diagnostics": process,
        "historical_replay_eligible": status == "READY",
    }
