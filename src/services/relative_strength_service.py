# -*- coding: utf-8 -*-
"""Deterministic completed-bar relative-strength evidence.

The service deliberately reuses DSA's existing daily-data route.  It does not
own screening, ranking, persistence, or action authority.  For the first CN
stock slice, 510300 is used explicitly as an ETF proxy for CSI 300; it is never
represented as raw index history.
"""

from __future__ import annotations

import logging
import threading
from datetime import date
from typing import Any, Dict, Mapping, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

RELATIVE_STRENGTH_SCHEMA_VERSION = "relative-strength-v1"
DEFAULT_HORIZON_SESSIONS = 60
BENCHMARK_FETCH_DAYS = 120
CN_BENCHMARK_PROXY: Dict[str, Any] = {
    "code": "510300",
    "name": "华泰柏瑞沪深300ETF",
    "kind": "etf_proxy",
    "tracks": "CSI300",
}


def _status_payload(
    *,
    status: str,
    market: str,
    stock_code: str,
    reason: str,
    benchmark: Optional[Mapping[str, Any]] = None,
    horizon_sessions: int = DEFAULT_HORIZON_SESSIONS,
) -> Dict[str, Any]:
    return {
        "schema_version": RELATIVE_STRENGTH_SCHEMA_VERSION,
        "family": "trend_relative_strength",
        "status": status,
        "market": market,
        "stock_code": stock_code,
        "horizon_sessions": horizon_sessions,
        "return_basis": "provider_price_return_proxy",
        "reason": reason,
        "benchmark": dict(benchmark or {}),
        "stock": {},
        "relative": {},
        "data_quality": {},
    }


def _normalize_history(history: Any, *, target_date: date) -> pd.DataFrame:
    if not isinstance(history, pd.DataFrame) or history.empty:
        return pd.DataFrame(columns=["date", "close"])

    frame = history.copy()
    frame.columns = [str(column).lower() for column in frame.columns]
    if "date" not in frame.columns or "close" not in frame.columns:
        return pd.DataFrame(columns=["date", "close"])

    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["date", "close"])
    frame = frame[(frame["close"] > 0) & (frame["date"] <= target_date)]
    frame = frame.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    columns = ["date", "close"]
    if "data_source" in frame.columns:
        columns.append("data_source")
    return frame[columns].reset_index(drop=True)


def compute_relative_strength_context(
    *,
    stock_code: str,
    market: str,
    stock_history: pd.DataFrame,
    benchmark_history: pd.DataFrame,
    benchmark: Mapping[str, Any],
    target_date: date,
    horizon_sessions: int = DEFAULT_HORIZON_SESSIONS,
    require_source_alignment: bool = False,
) -> Dict[str, Any]:
    """Compare stock and benchmark on the benchmark's exact completed-session endpoints."""
    benchmark_meta = dict(benchmark)
    required_observations = horizon_sessions + 1
    stock = _normalize_history(stock_history, target_date=target_date)
    bench = _normalize_history(benchmark_history, target_date=target_date)

    if len(bench) < required_observations:
        return _status_payload(
            status="MISSING",
            market=market,
            stock_code=stock_code,
            reason="BENCHMARK_WARMUP_INSUFFICIENT",
            benchmark=benchmark_meta,
            horizon_sessions=horizon_sessions,
        )
    if bench.empty or bench.iloc[-1]["date"] != target_date:
        return _status_payload(
            status="MISSING",
            market=market,
            stock_code=stock_code,
            reason="BENCHMARK_TARGET_DATE_MISSING",
            benchmark=benchmark_meta,
            horizon_sessions=horizon_sessions,
        )

    bench_window = bench.tail(required_observations).reset_index(drop=True)
    start_date = bench_window.iloc[0]["date"]
    end_date = bench_window.iloc[-1]["date"]
    stock_window = stock[(stock["date"] >= start_date) & (stock["date"] <= end_date)]
    stock_by_date = stock.set_index("date")["close"] if not stock.empty else pd.Series(dtype=float)

    if start_date not in stock_by_date.index or end_date not in stock_by_date.index:
        return _status_payload(
            status="MISSING",
            market=market,
            stock_code=stock_code,
            reason="STOCK_SHARED_ENDPOINT_MISSING",
            benchmark={
                **benchmark_meta,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "observations": int(len(bench_window)),
            },
            horizon_sessions=horizon_sessions,
        )

    stock_start = float(stock_by_date.loc[start_date])
    stock_end = float(stock_by_date.loc[end_date])
    benchmark_start = float(bench_window.iloc[0]["close"])
    benchmark_end = float(bench_window.iloc[-1]["close"])
    if min(stock_start, stock_end, benchmark_start, benchmark_end) <= 0:
        return _status_payload(
            status="UNKNOWN",
            market=market,
            stock_code=stock_code,
            reason="NON_POSITIVE_ENDPOINT_PRICE",
            benchmark=benchmark_meta,
            horizon_sessions=horizon_sessions,
        )

    stock_return = (stock_end / stock_start - 1.0) * 100.0
    benchmark_return = (benchmark_end / benchmark_start - 1.0) * 100.0
    excess_return = stock_return - benchmark_return
    ratio_change = ((stock_end / stock_start) / (benchmark_end / benchmark_start) - 1.0) * 100.0
    if abs(ratio_change) <= 1e-12:
        state = "NEUTRAL"
    elif ratio_change > 0:
        state = "OUTPERFORMING"
    else:
        state = "UNDERPERFORMING"

    stock_start_row = stock[stock["date"] == start_date].iloc[-1]
    stock_end_row = stock[stock["date"] == end_date].iloc[-1]
    stock_endpoint_sources = {
        str(value).strip()
        for value in (
            stock_start_row.get("data_source"),
            stock_end_row.get("data_source"),
        )
        if value not in (None, "") and str(value).strip()
    }
    benchmark_source = str(benchmark_meta.get("source") or "").strip()
    if not require_source_alignment:
        source_alignment = "NOT_REQUIRED"
        status = "READY"
        reason = "EXACT_SHARED_ENDPOINTS"
    elif benchmark_source and stock_endpoint_sources == {benchmark_source}:
        source_alignment = "MATCHED"
        status = "READY"
        reason = "EXACT_SHARED_ENDPOINTS_SOURCE_ALIGNED"
    else:
        source_alignment = "UNPROVEN"
        status = "PARTIAL"
        reason = "SOURCE_ALIGNMENT_UNPROVEN"

    return {
        "schema_version": RELATIVE_STRENGTH_SCHEMA_VERSION,
        "family": "trend_relative_strength",
        "status": status,
        "market": market,
        "stock_code": stock_code,
        "horizon_sessions": horizon_sessions,
        "return_basis": "provider_price_return_proxy",
        "reason": reason,
        "benchmark": {
            **benchmark_meta,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "observations": int(len(bench_window)),
            "return_pct": round(benchmark_return, 6),
        },
        "stock": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "observations": int(len(stock_window)),
            "return_pct": round(stock_return, 6),
        },
        "relative": {
            "state": state,
            "excess_return_pct_points": round(excess_return, 6),
            "relative_ratio_change_pct": round(ratio_change, 6),
        },
        "data_quality": {
            "source_alignment": source_alignment,
            "benchmark_source": benchmark_source or None,
            "stock_endpoint_sources": sorted(stock_endpoint_sources),
        },
    }


class RelativeStrengthService:
    """Build one fail-closed CN stock-vs-benchmark RS context per completed date."""

    def __init__(self, *, fetcher_manager: Any) -> None:
        self.fetcher_manager = fetcher_manager
        self._benchmark_cache: Dict[str, Tuple[Optional[pd.DataFrame], Optional[str], Optional[str]]] = {}
        self._benchmark_cache_lock = threading.Lock()

    def build_context(
        self,
        *,
        stock_code: str,
        market: Optional[str],
        stock_history: Optional[pd.DataFrame],
        target_date: Optional[date],
    ) -> Dict[str, Any]:
        normalized_market = str(market or "").strip().lower()
        benchmark_meta = dict(CN_BENCHMARK_PROXY)
        if normalized_market != "cn":
            return _status_payload(
                status="NOT_SUPPORTED",
                market=normalized_market or "unknown",
                stock_code=stock_code,
                reason="MARKET_NOT_SUPPORTED_V1",
                benchmark=benchmark_meta,
            )
        if not isinstance(target_date, date):
            return _status_payload(
                status="UNKNOWN",
                market="cn",
                stock_code=stock_code,
                reason="TARGET_DATE_UNKNOWN",
                benchmark=benchmark_meta,
            )
        if not isinstance(stock_history, pd.DataFrame) or stock_history.empty:
            return _status_payload(
                status="MISSING",
                market="cn",
                stock_code=stock_code,
                reason="STOCK_COMPLETED_HISTORY_MISSING",
                benchmark=benchmark_meta,
            )

        benchmark_history, source, error = self._load_benchmark(target_date)
        if error is not None or benchmark_history is None:
            return _status_payload(
                status="UNKNOWN",
                market="cn",
                stock_code=stock_code,
                reason="BENCHMARK_FETCH_UNAVAILABLE",
                benchmark={**benchmark_meta, "source": source},
            )

        return compute_relative_strength_context(
            stock_code=stock_code,
            market="cn",
            stock_history=stock_history,
            benchmark_history=benchmark_history,
            benchmark={**benchmark_meta, "source": source},
            target_date=target_date,
            require_source_alignment=True,
        )

    def _load_benchmark(
        self,
        target_date: date,
    ) -> Tuple[Optional[pd.DataFrame], Optional[str], Optional[str]]:
        cache_key = target_date.isoformat()
        with self._benchmark_cache_lock:
            cached = self._benchmark_cache.get(cache_key)
            if cached is not None:
                frame, source, error = cached
                return (frame.copy() if isinstance(frame, pd.DataFrame) else None, source, error)

            try:
                frame, source = self.fetcher_manager.get_daily_data(
                    CN_BENCHMARK_PROXY["code"],
                    end_date=target_date.isoformat(),
                    days=BENCHMARK_FETCH_DAYS,
                )
                stored = frame.copy() if isinstance(frame, pd.DataFrame) else None
                result = (stored, str(source or "unknown"), None)
            except Exception as exc:
                logger.warning("benchmark proxy history unavailable for %s: %s", target_date, exc)
                result = (None, None, str(exc))

            self._benchmark_cache[cache_key] = result
            frame, source, error = result
            return (frame.copy() if isinstance(frame, pd.DataFrame) else None, source, error)
