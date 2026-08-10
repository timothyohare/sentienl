"""
Unit tests for scripts/historical_backtest.py.

Synthetic daily-bar fixtures only — no live network (yfinance is only
exercised by fetch_daily_bars, which isn't unit tested here, same
convention as the live-fetcher classes in test_price_followup.py).
"""

from datetime import date, timedelta

import pytest

from sentinel.scripts.historical_backtest import (
    compute_event_stats,
    daily_baseline_volume,
)


def _bars(start: date, volumes: list[float], closes: list[float] | None = None) -> list[dict]:
    """Build consecutive daily bars starting at `start`, one per entry."""
    closes = closes or [100.0] * len(volumes)
    return [
        {"date": start + timedelta(days=i), "open": closes[i], "close": closes[i], "volume": v}
        for i, v in enumerate(volumes)
    ]


class TestDailyBaselineVolume:
    def test_averages_lookback_window_before_event(self):
        bars = _bars(date(2020, 1, 1), [100, 200, 300, 900])  # last bar is the event day
        baseline = daily_baseline_volume(bars, date(2020, 1, 4), lookback_days=3)
        assert baseline == pytest.approx(200.0)  # mean(100, 200, 300)

    def test_none_when_insufficient_history(self):
        bars = _bars(date(2020, 1, 1), [100, 200])
        assert daily_baseline_volume(bars, date(2020, 1, 3), lookback_days=3) is None

    def test_uses_most_recent_lookback_days_only(self):
        # event day (Jan 5) excluded from "prior"; the old 1000 outlier
        # (Jan 1) falls outside the 3-day lookback window and is excluded.
        bars = _bars(date(2020, 1, 1), [1000, 100, 200, 300, 900])
        baseline = daily_baseline_volume(bars, date(2020, 1, 5), lookback_days=3)
        assert baseline == pytest.approx(200.0)  # mean(100, 200, 300)

    def test_non_positive_lookback_returns_none(self):
        bars = _bars(date(2020, 1, 1), [100, 200, 300])
        assert daily_baseline_volume(bars, date(2020, 1, 4), lookback_days=0) is None

    def test_unsorted_input_still_correct(self):
        ordered = _bars(date(2020, 1, 1), [100, 200, 300])
        shuffled = [ordered[2], ordered[0], ordered[1]]
        baseline = daily_baseline_volume(shuffled, date(2020, 1, 4), lookback_days=3)
        assert baseline == pytest.approx(200.0)


class TestComputeEventStats:
    def test_computes_ratio_and_price_moves(self):
        bars = _bars(
            date(2020, 1, 1),
            volumes=[100, 100, 100, 500],
            closes=[60.0, 60.0, 60.0, 63.0],
        )
        bars[-1]["open"] = 61.0  # event day opened at 61, closed at 63
        stats = compute_event_stats(bars, date(2020, 1, 4), lookback_days=3)

        assert stats["event_date"] == date(2020, 1, 4)
        assert stats["volume_ratio"] == pytest.approx(5.0)
        assert stats["close_to_close_pct"] == pytest.approx(5.0)  # (63-60)/60 * 100
        assert stats["open_to_close_pct"] == pytest.approx((63 - 61) / 61 * 100)

    def test_none_when_no_bar_on_or_after_event_date(self):
        bars = _bars(date(2020, 1, 1), [100, 100, 100])
        assert compute_event_stats(bars, date(2020, 1, 10), lookback_days=3) is None

    def test_none_when_no_baseline_history(self):
        bars = _bars(date(2020, 1, 1), [100, 500])
        assert compute_event_stats(bars, date(2020, 1, 2), lookback_days=3) is None

    def test_none_when_lookback_not_positive(self):
        bars = _bars(date(2020, 1, 1), [100, 200, 500])
        assert compute_event_stats(bars, date(2020, 1, 3), lookback_days=0) is None

    def test_falls_forward_to_next_trading_day_when_event_date_is_closed(self):
        # No bar exactly on event_date (e.g. a weekend) — should use the
        # next available trading day's bar instead.
        bars = _bars(
            date(2020, 1, 1),
            volumes=[100, 100, 100, 500],  # bars land on Jan 1-3 + Jan 6 (skip weekend)
        )
        bars[3]["date"] = date(2020, 1, 6)
        stats = compute_event_stats(bars, date(2020, 1, 4), lookback_days=3)  # Jan 4 = Saturday
        assert stats["event_date"] == date(2020, 1, 6)
