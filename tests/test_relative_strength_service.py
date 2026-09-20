# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pandas as pd

from src.services.relative_strength_service import (
    CN_BENCHMARK_PROXY,
    RelativeStrengthService,
    compute_relative_strength_context,
)


def _history(*, periods: int = 70, start_price: float = 100.0, end_price: float = 110.0) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-02", periods=periods)
    closes = [start_price + (end_price - start_price) * i / max(periods - 1, 1) for i in range(periods)]
    return pd.DataFrame({"date": dates, "close": closes})


def test_exact_shared_endpoint_relative_strength_is_ready_and_formula_is_auditable():
    benchmark = _history(periods=70, start_price=100.0, end_price=110.0)
    target = benchmark.iloc[-1]["date"].date()
    stock = benchmark.copy()
    stock["close"] = stock["close"] * pd.Series(
        [1.0 + 0.20 * i / (len(stock) - 1) for i in range(len(stock))]
    )

    context = compute_relative_strength_context(
        stock_code="600519",
        market="cn",
        stock_history=stock,
        benchmark_history=benchmark,
        benchmark={**CN_BENCHMARK_PROXY, "source": "test"},
        target_date=target,
    )

    assert context["status"] == "READY"
    assert context["benchmark"]["kind"] == "etf_proxy"
    assert context["benchmark"]["code"] == "510300"
    assert context["benchmark"]["observations"] == 61
    assert context["benchmark"]["end_date"] == target.isoformat()
    assert context["relative"]["state"] == "OUTPERFORMING"
    assert context["relative"]["relative_ratio_change_pct"] > 0
    assert context["relative"]["excess_return_pct_points"] > 0


def test_benchmark_warmup_and_exact_target_are_fail_closed():
    short_benchmark = _history(periods=60)
    target = short_benchmark.iloc[-1]["date"].date()
    missing = compute_relative_strength_context(
        stock_code="600519",
        market="cn",
        stock_history=short_benchmark,
        benchmark_history=short_benchmark,
        benchmark=CN_BENCHMARK_PROXY,
        target_date=target,
    )
    assert missing["status"] == "MISSING"
    assert missing["reason"] == "BENCHMARK_WARMUP_INSUFFICIENT"

    benchmark = _history(periods=70)
    future_target = benchmark.iloc[-1]["date"].date() + pd.Timedelta(days=1)
    target_missing = compute_relative_strength_context(
        stock_code="600519",
        market="cn",
        stock_history=benchmark,
        benchmark_history=benchmark,
        benchmark=CN_BENCHMARK_PROXY,
        target_date=future_target,
    )
    assert target_missing["status"] == "MISSING"
    assert target_missing["reason"] == "BENCHMARK_TARGET_DATE_MISSING"


def test_stock_must_have_same_benchmark_start_and_end_dates():
    benchmark = _history(periods=70)
    target = benchmark.iloc[-1]["date"].date()
    benchmark_window = benchmark.tail(61)
    shared_start = benchmark_window.iloc[0]["date"]
    stock = benchmark[benchmark["date"] != shared_start].copy()

    context = compute_relative_strength_context(
        stock_code="600519",
        market="cn",
        stock_history=stock,
        benchmark_history=benchmark,
        benchmark=CN_BENCHMARK_PROXY,
        target_date=target,
    )
    assert context["status"] == "MISSING"
    assert context["reason"] == "STOCK_SHARED_ENDPOINT_MISSING"


def test_future_rows_are_trimmed_and_underperformance_is_preserved():
    benchmark = _history(periods=70, start_price=100.0, end_price=120.0)
    target = benchmark.iloc[-2]["date"].date()
    stock = benchmark.copy()
    stock["close"] = [100.0 + 5.0 * i / (len(stock) - 1) for i in range(len(stock))]

    context = compute_relative_strength_context(
        stock_code="600519",
        market="cn",
        stock_history=stock,
        benchmark_history=benchmark,
        benchmark=CN_BENCHMARK_PROXY,
        target_date=target,
    )
    assert context["status"] == "READY"
    assert context["benchmark"]["end_date"] == target.isoformat()
    assert context["relative"]["state"] == "UNDERPERFORMING"
    assert context["relative"]["relative_ratio_change_pct"] < 0


def test_service_caches_one_benchmark_fetch_per_target_date_and_rejects_non_cn():
    benchmark = _history(periods=70)
    target = benchmark.iloc[-1]["date"].date()
    manager = MagicMock()
    manager.get_daily_data.return_value = (benchmark, "Fetcher")
    service = RelativeStrengthService(fetcher_manager=manager)

    stock_history = benchmark.assign(data_source="Fetcher")
    first = service.build_context(
        stock_code="600519", market="cn", stock_history=stock_history, target_date=target
    )
    second = service.build_context(
        stock_code="000001", market="cn", stock_history=stock_history, target_date=target
    )
    unsupported = service.build_context(
        stock_code="AAPL", market="us", stock_history=benchmark, target_date=target
    )

    assert first["status"] == "READY"
    assert second["status"] == "READY"
    assert unsupported["status"] == "NOT_SUPPORTED"
    manager.get_daily_data.assert_called_once_with(
        "510300", end_date=target.isoformat(), days=120
    )


def test_service_marks_cross_provider_price_return_as_partial_not_ready():
    benchmark = _history(periods=70)
    target = benchmark.iloc[-1]["date"].date()
    manager = MagicMock()
    manager.get_daily_data.return_value = (benchmark, "BenchmarkFetcher")
    service = RelativeStrengthService(fetcher_manager=manager)
    stock = benchmark.assign(data_source="StockFetcher")

    context = service.build_context(
        stock_code="600519", market="cn", stock_history=stock, target_date=target
    )
    assert context["status"] == "PARTIAL"
    assert context["reason"] == "SOURCE_ALIGNMENT_UNPROVEN"
    assert context["data_quality"]["source_alignment"] == "UNPROVEN"
    assert context["relative"]["state"] in {"OUTPERFORMING", "UNDERPERFORMING", "NEUTRAL"}


def test_service_benchmark_fetch_failure_is_unknown_not_fabricated():
    manager = MagicMock()
    manager.get_daily_data.side_effect = RuntimeError("provider unavailable")
    service = RelativeStrengthService(fetcher_manager=manager)
    stock = _history(periods=70)
    target = stock.iloc[-1]["date"].date()

    context = service.build_context(
        stock_code="600519", market="cn", stock_history=stock, target_date=target
    )
    assert context["status"] == "UNKNOWN"
    assert context["reason"] == "BENCHMARK_FETCH_UNAVAILABLE"
