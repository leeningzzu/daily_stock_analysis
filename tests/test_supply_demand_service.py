# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import timedelta

import pandas as pd

from src.services.supply_demand_service import build_supply_demand_context


def _history(*, periods: int = 30, source: str | None = "Fetcher", direction: int = 1) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-02", periods=periods)
    rows = []
    price = 100.0
    for index, day in enumerate(dates):
        price += direction * (0.8 if index % 3 != 0 else -0.2)
        high = price + 1.0
        low = price - 1.0
        close = high - 0.2 if direction > 0 else low + 0.2
        row = {
            "date": day,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1000.0 + index * 25.0,
        }
        if source is not None:
            row["data_source"] = source
        rows.append(row)
    return pd.DataFrame(rows)


def test_completed_single_source_demand_pressure_is_ready_and_auditable():
    history = _history(direction=1)
    target = history.iloc[-1]["date"].date()
    context = build_supply_demand_context(stock_code="600519", history=history, target_date=target)

    assert context["status"] == "READY"
    assert context["state"] == "DEMAND_PRESSURE"
    assert context["relative_volume"]["volume_ratio_20d"] > 1
    assert context["directional_volume"]["signed_volume_balance"] > 0
    assert context["close_location_flow"]["cmf_20"] > 0
    assert context["data_quality"]["observations"] == 21
    assert context["institutional_intent_claim"] == "NOT_INFERRED"


def test_supply_pressure_is_observable_without_asserting_institutional_intent():
    history = _history(direction=-1)
    target = history.iloc[-1]["date"].date()
    context = build_supply_demand_context(stock_code="000001", history=history, target_date=target)

    assert context["status"] == "READY"
    assert context["state"] == "SUPPLY_PRESSURE"
    assert context["directional_volume"]["signed_volume_balance"] < 0
    assert context["close_location_flow"]["cmf_20"] < 0
    text = str(context)
    assert "吸筹" not in text
    assert "出货" not in text
    assert "主力" not in text


def test_warmup_and_target_date_fail_closed_and_future_rows_are_trimmed():
    short = _history(periods=20)
    target = short.iloc[-1]["date"].date()
    missing = build_supply_demand_context(stock_code="600519", history=short, target_date=target)
    assert missing["status"] == "MISSING"
    assert missing["reason"] == "WARMUP_INSUFFICIENT"

    history = _history(periods=30)
    exact_target = history.iloc[-2]["date"].date()
    trimmed = build_supply_demand_context(stock_code="600519", history=history, target_date=exact_target)
    assert trimmed["status"] == "READY"
    assert trimmed["data_quality"]["end_date"] == exact_target.isoformat()

    future_target = history.iloc[-1]["date"].date() + timedelta(days=1)
    absent = build_supply_demand_context(stock_code="600519", history=history, target_date=future_target)
    assert absent["status"] == "MISSING"
    assert absent["reason"] == "TARGET_DATE_BAR_MISSING"


def test_missing_or_mixed_source_is_partial_not_ready():
    history = _history(source=None)
    target = history.iloc[-1]["date"].date()
    missing_source = build_supply_demand_context(stock_code="600519", history=history, target_date=target)
    assert missing_source["status"] == "PARTIAL"
    assert missing_source["reason"] == "SOURCE_ALIGNMENT_UNPROVEN"

    mixed = _history()
    mixed.loc[mixed.index[-1], "data_source"] = "OtherFetcher"
    mixed_source = build_supply_demand_context(stock_code="600519", history=mixed, target_date=target)
    assert mixed_source["status"] == "PARTIAL"
    assert mixed_source["data_quality"]["source_alignment"] == "UNPROVEN"


def test_non_positive_volume_fails_closed_instead_of_claiming_balance():
    history = _history()
    history.loc[history.index[-1], "volume"] = 0.0
    target = history.iloc[-1]["date"].date()
    context = build_supply_demand_context(stock_code="600519", history=history, target_date=target)
    assert context["status"] == "UNKNOWN"
    assert context["reason"] == "NON_POSITIVE_VOLUME"
    assert context["state"] == "UNKNOWN"


def test_zero_spread_bar_does_not_create_divide_by_zero_or_intent_claim():
    history = _history()
    history.loc[history.index[-1], "high"] = history.loc[history.index[-1], "close"]
    history.loc[history.index[-1], "low"] = history.loc[history.index[-1], "close"]
    target = history.iloc[-1]["date"].date()
    context = build_supply_demand_context(stock_code="600519", history=history, target_date=target)
    assert context["status"] == "READY"
    assert -1 <= context["close_location_flow"]["cmf_20"] <= 1
    assert context["institutional_intent_claim"] == "NOT_INFERRED"
