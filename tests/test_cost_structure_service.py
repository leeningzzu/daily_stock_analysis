# -*- coding: utf-8 -*-
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from src.services.cost_structure_service import build_cost_structure_context


def _history(periods: int = 70, *, source: str | None = "Fetcher") -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-02", periods=periods)
    rows = []
    price = 100.0
    for index, day in enumerate(dates):
        price += 0.2
        row = {
            "date": day,
            "high": price + 1.0,
            "low": price - 1.0,
            "close": price + 0.2,
            "volume": 1000.0 + index * 10.0,
        }
        if source is not None:
            row["data_source"] = source
        rows.append(row)
    return pd.DataFrame(rows)


def _chip(*, date: str, source: str = "Fetcher", low70: float = 100.0, high70: float = 120.0):
    return SimpleNamespace(
        date=date,
        source=source,
        profit_ratio=0.62,
        avg_cost=110.0,
        cost_70_low=low70,
        cost_70_high=high70,
        concentration_70=0.12,
        cost_90_low=95.0,
        cost_90_high=125.0,
        concentration_90=0.18,
    )


def test_completed_20_60_bar_reference_is_ready_and_not_holder_cost():
    history = _history()
    target = history.iloc[-1]["date"].date()
    context = build_cost_structure_context(
        stock_code="600519",
        history=history,
        chip_data=None,
        target_date=target,
        market="cn",
    )

    assert context["status"] == "READY"
    bar = context["bar_reference_cost"]
    assert bar["window_20"]["status"] == "READY"
    assert bar["window_60"]["status"] == "READY"
    assert bar["window_20"]["observations"] == 20
    assert bar["window_60"]["observations"] == 60
    assert bar["window_20"]["method"] == "HLC3_VOLUME_WEIGHTED_COMPLETED_DAILY_BARS"
    assert bar["window_60"]["semantics"] == "BAR_DERIVED_REFERENCE_PRICE_NOT_HOLDER_COST"
    assert context["composition"]["provider_vs_bar_relation"] == "SINGLE_SOURCE_ONLY"
    assert context["hard_veto"] is False


def test_provider_snapshot_is_current_only_and_exact_date_match_can_converge():
    history = _history()
    target = history.iloc[-1]["date"].date()
    bar_context = build_cost_structure_context(
        stock_code="600519",
        history=history,
        chip_data=None,
        target_date=target,
        market="cn",
    )
    ref20 = bar_context["bar_reference_cost"]["window_20"]["rolling_reference_price"]
    ref60 = bar_context["bar_reference_cost"]["window_60"]["rolling_reference_price"]
    chip = _chip(
        date=target.isoformat(),
        low70=min(ref20, ref60) - 1.0,
        high70=max(ref20, ref60) + 1.0,
    )

    context = build_cost_structure_context(
        stock_code="600519",
        history=history,
        chip_data=chip,
        target_date=target,
        market="cn",
    )

    provider = context["provider_chip_snapshot"]
    assert provider["status"] == "READY_CURRENT_ONLY"
    assert provider["historical_pit_authority"] == "CURRENT_ONLY"
    assert provider["historical_replay_eligible"] is False
    assert provider["institutional_intent_claim"] == "NOT_INFERRED"
    assert context["composition"]["provider_vs_bar_relation"] == "CONVERGENT"
    assert context["independent_action_authority"] is False


def test_provider_date_mismatch_never_becomes_historical_truth():
    history = _history()
    target = history.iloc[-1]["date"].date()
    chip = _chip(date=history.iloc[-2]["date"].date().isoformat())

    context = build_cost_structure_context(
        stock_code="600519",
        history=history,
        chip_data=chip,
        target_date=target,
        market="cn",
    )

    provider = context["provider_chip_snapshot"]
    assert provider["status"] == "PARTIAL"
    assert provider["reason"] == "PROVIDER_TARGET_DATE_MISMATCH"
    assert provider["historical_pit_authority"] == "CURRENT_ONLY"
    assert context["status"] == "READY"  # PIT-safe bar layer remains independently usable.


def test_missing_or_mixed_bar_source_degrades_and_future_rows_are_trimmed():
    history = _history(source=None)
    target = history.iloc[-2]["date"].date()
    context = build_cost_structure_context(
        stock_code="600519",
        history=history,
        chip_data=None,
        target_date=target,
        market="cn",
    )
    assert context["status"] == "PARTIAL"
    assert context["bar_reference_cost"]["window_60"]["source_alignment"] == "UNPROVEN"
    assert context["bar_reference_cost"]["window_60"]["end_date"] == target.isoformat()

    mixed = _history()
    mixed.loc[mixed.index[-1], "data_source"] = "OtherFetcher"
    mixed_context = build_cost_structure_context(
        stock_code="600519",
        history=mixed,
        chip_data=None,
        target_date=mixed.iloc[-1]["date"].date(),
        market="cn",
    )
    assert mixed_context["status"] == "PARTIAL"


def test_20_window_can_exist_without_60_and_non_positive_volume_fails_closed():
    short = _history(periods=30)
    target = short.iloc[-1]["date"].date()
    partial = build_cost_structure_context(
        stock_code="600519",
        history=short,
        chip_data=None,
        target_date=target,
        market="cn",
    )
    assert partial["status"] == "PARTIAL"
    assert partial["bar_reference_cost"]["window_20"]["status"] == "READY"
    assert partial["bar_reference_cost"]["window_60"]["status"] == "MISSING"

    bad = _history()
    bad.loc[bad.index[-1], "volume"] = 0.0
    unknown = build_cost_structure_context(
        stock_code="600519",
        history=bad,
        chip_data=None,
        target_date=bad.iloc[-1]["date"].date(),
        market="cn",
    )
    assert unknown["status"] == "UNKNOWN"
    assert unknown["bar_reference_cost"]["window_20"]["reason"] == "NON_POSITIVE_VOLUME"


def test_provider_and_bar_different_sources_are_not_compared():
    history = _history(source="DailyFetcher")
    target = history.iloc[-1]["date"].date()
    chip = _chip(date=target.isoformat(), source="ChipFetcher", low70=90.0, high70=140.0)

    context = build_cost_structure_context(
        stock_code="600519",
        history=history,
        chip_data=chip,
        target_date=target,
        market="cn",
    )

    assert context["provider_chip_snapshot"]["status"] == "READY_CURRENT_ONLY"
    assert context["bar_reference_cost"]["status"] == "READY"
    assert context["composition"]["provider_vs_bar_relation"] == "NOT_COMPARABLE"
    assert context["composition"]["basis"] == "PROVIDER_BAR_SOURCE_MISMATCH"


def test_provider_and_bar_can_be_divergent_only_via_provider_cost_band_not_arbitrary_threshold():
    history = _history()
    target = history.iloc[-1]["date"].date()
    chip = _chip(date=target.isoformat(), low70=10.0, high70=20.0)
    chip.cost_90_low = 10.0
    chip.cost_90_high = 30.0

    context = build_cost_structure_context(
        stock_code="600519",
        history=history,
        chip_data=chip,
        target_date=target,
        market="cn",
    )
    assert context["composition"]["provider_vs_bar_relation"] == "DIVERGENT"
    assert context["composition"]["basis"] == "BAR_REFERENCES_OUTSIDE_PROVIDER_90_COST_BAND_SAME_SIDE"
