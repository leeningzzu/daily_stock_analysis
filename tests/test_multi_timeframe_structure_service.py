from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from src.services.multi_timeframe_structure_service import (
    CROSS_RUN_PERSISTENCE_POLICY,
    build_multi_timeframe_structure_context,
)


def _history(periods: int = 150, *, future: int = 0) -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-02", periods=periods + future)
    rows = []
    for index, day in enumerate(dates):
        close = 80.0 + index * 0.25 + ((index % 9) - 4) * 0.12
        rows.append(
            {
                "date": day,
                "open": close - 0.2,
                "high": close + 0.8,
                "low": close - 0.8,
                "close": close,
                "volume": 1_000_000 + index * 1000,
                "data_source": "FixtureFetcher",
            }
        )
    return pd.DataFrame(rows)


def _trend_result(label: str = "多头排列"):
    return SimpleNamespace(
        trend_status=SimpleNamespace(value=label),
        ma_alignment="多头排列 MA5>MA10>MA20" if "多头" in label else "均线缠绕",
        trend_strength=75.0,
        ma5=10.5,
        ma10=10.0,
        ma20=9.5,
        volume_status=SimpleNamespace(value="量能正常"),
        volume_ratio_5d=1.0,
        macd_status=SimpleNamespace(value="多头"),
        macd_signal="MACD DIF/DEA 均位于零轴上方，动量偏强",
        rsi_status=SimpleNamespace(value="中性"),
        rsi_signal="RSI中性",
    )


class _FakeTrendAnalyzer:
    def analyze(self, _frame, _code):
        return _trend_result()


def _daily_trend(_history: pd.DataFrame, _target_index: int):
    return _trend_result()


def test_weekly_ready_monthly_missing_and_intraday_stays_missing():
    history = _history()
    target_index = len(history) - 1
    target = history.iloc[target_index]["date"].date()
    weekly_end = history.iloc[target_index - 4]["date"].date()
    monthly_end = history.iloc[target_index - 20]["date"].date()

    def _completed(_market, _target, timeframe):
        return weekly_end if timeframe == "1w" else monthly_end if timeframe == "1mo" else None

    with patch(
        "src.services.multi_timeframe_structure_service.resolve_completed_timeframe_bar_date",
        side_effect=_completed,
    ):
        context = build_multi_timeframe_structure_context(
            stock_code="600519",
            history=history,
            target_date=target,
            market="cn",
            trend_analyzer=_FakeTrendAnalyzer(),
            daily_trend_result=_daily_trend(history, target_index),
            daily_price_structure_context={"historical_replay_eligible": True},
        )

    assert context["timeframes"]["weekly"]["status"] in {"READY", "PARTIAL"}
    assert context["timeframes"]["weekly"]["summary"]
    assert context["timeframes"]["monthly"]["status"] == "MISSING"
    assert context["timeframes"]["monthly"]["reason"] == "WARMUP_INSUFFICIENT"
    for key in ("60m", "30m", "15m", "5m"):
        assert context["timeframes"][key]["status"] == "MISSING"
        assert (
            context["timeframes"][key]["reason"]
            == "INTRADAY_COMPLETED_BAR_CONTRACT_NOT_READY"
        )
    assert context["independent_action_authority"] is False
    assert context["cross_timeframe_vote_counting"] is False
    assert context["cross_run_persistence_policy"] == CROSS_RUN_PERSISTENCE_POLICY
    assert context["cross_run_persistence_eligible"] is False
    assert context["cross_run_persistence_reason"] == "ADJUSTMENT_BASIS_NOT_PERSISTED"


def test_monthly_trend_projection_requires_26_completed_bars():
    rows = []
    for index in range(26):
        year = 2023 + index // 12
        month = index % 12 + 1
        day = pd.Timestamp(year=year, month=month, day=15)
        close = 50.0 + index
        rows.append(
            {
                "date": day,
                "open": close - 0.5,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 100_000 + index * 100,
                "data_source": "FixtureFetcher",
            }
        )
    history_26 = pd.DataFrame(rows)
    history_25 = history_26.iloc[1:].reset_index(drop=True)
    target = history_26.iloc[-1]["date"].date()

    def _completed(_market, _target, timeframe):
        return target if timeframe == "1mo" else None

    def _build(history):
        with patch(
            "src.services.multi_timeframe_structure_service.resolve_completed_timeframe_bar_date",
            side_effect=_completed,
        ):
            return build_multi_timeframe_structure_context(
                stock_code="600519",
                history=history,
                target_date=target,
                market="cn",
                trend_analyzer=_FakeTrendAnalyzer(),
                daily_trend_result=_trend_result(),
                daily_price_structure_context={"historical_replay_eligible": True},
            )

    monthly_25 = _build(history_25)["timeframes"]["monthly"]
    monthly_26 = _build(history_26)["timeframes"]["monthly"]

    assert monthly_25["observations"] == 25
    assert monthly_25["trend"]["status"] == "MISSING"
    assert monthly_26["observations"] == 26
    assert monthly_26["trend"]["status"] == "READY"


def test_target_date_trim_preserves_prefix_equivalence():
    prefix = _history(periods=150)
    full = _history(periods=150, future=15)
    target_index = len(prefix) - 1
    target = prefix.iloc[-1]["date"].date()
    weekly_end = prefix.iloc[-5]["date"].date()
    monthly_end = prefix.iloc[-21]["date"].date()

    def _completed(_market, _target, timeframe):
        return weekly_end if timeframe == "1w" else monthly_end if timeframe == "1mo" else None

    kwargs = dict(
        stock_code="600519",
        target_date=target,
        market="cn",
        trend_analyzer=_FakeTrendAnalyzer(),
        daily_trend_result=_daily_trend(prefix, target_index),
        daily_price_structure_context={"historical_replay_eligible": True},
    )
    with patch(
        "src.services.multi_timeframe_structure_service.resolve_completed_timeframe_bar_date",
        side_effect=_completed,
    ):
        full_context = build_multi_timeframe_structure_context(history=full, **kwargs)
        prefix_context = build_multi_timeframe_structure_context(history=prefix, **kwargs)

    assert full_context == prefix_context


def test_missing_source_and_unproven_period_fail_closed():
    history = _history().drop(columns=["data_source"])
    target = history.iloc[-1]["date"].date()
    with patch(
        "src.services.multi_timeframe_structure_service.resolve_completed_timeframe_bar_date",
        return_value=None,
    ):
        context = build_multi_timeframe_structure_context(
            stock_code="600519",
            history=history,
            target_date=target,
            market="cn",
            trend_analyzer=_FakeTrendAnalyzer(),
            daily_trend_result=SimpleNamespace(
                trend_status=SimpleNamespace(value="盘整"), ma_alignment="均线缠绕"
            ),
            daily_price_structure_context={"historical_replay_eligible": False},
        )
    assert context["status"] == "PARTIAL"
    assert context["source_alignment"]["status"] == "UNPROVEN"
    assert context["timeframes"]["weekly"]["reason"] == "COMPLETED_PERIOD_UNPROVEN"
    assert context["timeframes"]["monthly"]["reason"] == "COMPLETED_PERIOD_UNPROVEN"


def test_completed_history_identity_is_prefix_safe_and_changes_with_consumed_bytes():
    prefix = _history(periods=150)
    prefix["data_source"] = "AkshareFetcher"
    full = _history(periods=150, future=8)
    full["data_source"] = "AkshareFetcher"
    target = prefix.iloc[-1]["date"].date()
    observed_at = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)

    kwargs = dict(
        stock_code="600519",
        target_date=target,
        market="cn",
        trend_analyzer=_FakeTrendAnalyzer(),
        daily_trend_result=_trend_result(),
        daily_price_structure_context={"historical_replay_eligible": True},
        snapshot_observed_at=observed_at,
    )
    with patch(
        "src.services.multi_timeframe_structure_service.resolve_completed_timeframe_bar_date",
        return_value=None,
    ):
        prefix_context = build_multi_timeframe_structure_context(history=prefix, **kwargs)
        full_context = build_multi_timeframe_structure_context(history=full, **kwargs)
        changed = prefix.copy()
        changed.loc[10, "close"] = float(changed.loc[10, "close"]) + 1.0
        changed_context = build_multi_timeframe_structure_context(history=changed, **kwargs)

    assert prefix_context["data_snapshot_identity"] == full_context["data_snapshot_identity"]
    assert prefix_context["data_snapshot_identity"] != changed_context["data_snapshot_identity"]
    assert prefix_context["provider_identity"] == "AkshareFetcher"
    assert prefix_context["adjustment_basis"] == "qfq"
    assert prefix_context["available_at_max"] == "2026-09-17T10:00:00"
