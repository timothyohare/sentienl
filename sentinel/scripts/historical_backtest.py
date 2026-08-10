#!/usr/bin/env python3
"""
scripts/historical_backtest.py — sanity-check the futures volume-spike
thresholds against known historical shocks, per TODO.md's "Validate Signal
Logic Against History" item: Soleimani assassination (Jan 3 2020), the
Russia-Ukraine invasion (Feb 24 2022), and the Gaza war's outbreak
(Oct 7 2023).

Caveat: the live collector (futures_volume.py) evaluates spike_multiplier
against a rolling window of 1-MINUTE bars (rolling_bars=20). Yahoo/yfinance
only serves intraday bars within a recent rolling window (1m: last 30
days, 5m: 60 days, 60m: 730 days) — for events this old, only DAILY bars
are available. This script therefore measures daily volume-vs-baseline and
daily price move as a coarse proxy, NOT a literal replay of the live
minute-level algorithm. A clear "yes" reading (daily volume and price both
far outside normal range) is a meaningful sanity check that the event was
a genuine, detectable anomaly. A "no" reading is NOT proof the live
algorithm would have missed it — a huge single-minute spike can hide
inside an unremarkable daily total. Treat results as directional evidence
for calibration discussions, not a pass/fail gate.

Exit code is always 0 — this is a reporting tool, not a pass/fail gate.
"""

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from statistics import mean

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sentinel.core.config import load_config

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("sentinel.historical_backtest")

# (ticker, event_date, label, reported_move) — known geopolitical shocks,
# per TODO.md's "Validate Signal Logic Against History" item. Event dates
# are the first trading day the news was reflected in the market.
KNOWN_EVENTS = [
    ("CL=F", date(2020, 1, 3), "Soleimani assassination", "WTI +4%"),
    ("BZ=F", date(2022, 2, 24), "Russia-Ukraine invasion", "Brent +8%"),
    ("CL=F", date(2023, 10, 9), "Gaza war outbreak (first trading day after Oct 7)", "oil +4%"),
]


# ---------------------------------------------------------------------------
# Pure math (no network — directly testable)
# ---------------------------------------------------------------------------

def daily_baseline_volume(
    bars: list[dict], event_date: date, lookback_days: int
) -> float | None:
    """Mean volume of the `lookback_days` trading days strictly before
    event_date. None if `bars` doesn't have enough prior history."""
    prior = [b["volume"] for b in sorted(bars, key=lambda b: b["date"]) if b["date"] < event_date]
    if lookback_days <= 0 or len(prior) < lookback_days:
        return None
    return mean(prior[-lookback_days:])


def compute_event_stats(
    bars: list[dict], event_date: date, lookback_days: int = 20
) -> dict | None:
    """Volume ratio and price move for the trading day matching event_date
    (or the next trading day if the market was closed that day), vs. the
    prior lookback_days' average daily volume. None if data is missing."""
    sorted_bars = sorted(bars, key=lambda b: b["date"])
    on_or_after = [b for b in sorted_bars if b["date"] >= event_date]
    if not on_or_after:
        return None
    event_bar = on_or_after[0]

    baseline = daily_baseline_volume(sorted_bars, event_bar["date"], lookback_days)
    if baseline is None or baseline <= 0:
        return None

    prior_closes = [b["close"] for b in sorted_bars if b["date"] < event_bar["date"]]
    if not prior_closes:
        return None
    prev_close = prior_closes[-1]

    return {
        "event_date": event_bar["date"],
        "event_volume": event_bar["volume"],
        "baseline_volume": baseline,
        "volume_ratio": event_bar["volume"] / baseline,
        "close_to_close_pct": (event_bar["close"] - prev_close) / prev_close * 100,
        "open_to_close_pct": (event_bar["close"] - event_bar["open"]) / event_bar["open"] * 100,
    }


# ---------------------------------------------------------------------------
# Live data fetch
# ---------------------------------------------------------------------------

def fetch_daily_bars(ticker: str, start: date, end: date) -> list[dict]:
    """Fetch daily OHLCV bars for `ticker` between start and end (inclusive)."""
    import yfinance as yf
    df = yf.Ticker(ticker).history(
        start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(), interval="1d"
    )
    return [
        {
            "date": idx.date(),
            "open": float(row["Open"]),
            "close": float(row["Close"]),
            "volume": float(row["Volume"]),
        }
        for idx, row in df.iterrows()
    ]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Backtest futures volume-spike thresholds against known historical events"
    )
    parser.add_argument("--config", default=os.environ.get("SENTINEL_CONFIG", "config.yaml"))
    parser.add_argument("--lookback-days", type=int, default=20)
    args = parser.parse_args()

    cfg = load_config(args.config)
    min_volume_by_ticker = {i.ticker: i.min_absolute_volume for i in cfg.futures.instruments}
    spike_multiplier = cfg.futures.thresholds.spike_multiplier
    spike_multiplier_quiet = cfg.futures.thresholds.spike_multiplier_quiet

    logger.info("=== Historical backtest: futures volume-spike thresholds ===")
    logger.info("DAILY-bar proxy, not a literal replay of the live 1-minute")
    logger.info("algorithm — directional evidence only, see script docstring.")
    logger.info(
        "Configured: spike_multiplier=%.1fx (active window) "
        "spike_multiplier_quiet=%.1fx (outside it)",
        spike_multiplier, spike_multiplier_quiet,
    )
    logger.info("")

    for ticker, event_date, label, reported_move in KNOWN_EVENTS:
        window_start = event_date - timedelta(days=args.lookback_days * 3)
        window_end = event_date + timedelta(days=5)
        bars = fetch_daily_bars(ticker, window_start, window_end)
        stats = compute_event_stats(bars, event_date, args.lookback_days)

        if stats is None:
            logger.info("%-45s %-6s NO DATA available for this window", label, ticker)
            continue

        min_volume = min_volume_by_ticker.get(ticker)
        floor_note = ""
        if min_volume is not None and stats["event_volume"] < min_volume:
            floor_note = f"  [below min_absolute_volume={min_volume:,.0f} floor]"

        logger.info(
            "%-45s %-6s %s (reported: %s)",
            label, ticker, stats["event_date"].isoformat(), reported_move,
        )
        logger.info(
            "    volume=%.0f baseline=%.0f ratio=%.2fx "
            "close-to-close=%+.2f%% open-to-close=%+.2f%%%s",
            stats["event_volume"], stats["baseline_volume"], stats["volume_ratio"],
            stats["close_to_close_pct"], stats["open_to_close_pct"], floor_note,
        )
        logger.info(
            "    daily ratio clears spike_multiplier(%.1fx)=%s  "
            "spike_multiplier_quiet(%.1fx)=%s",
            spike_multiplier, stats["volume_ratio"] >= spike_multiplier,
            spike_multiplier_quiet, stats["volume_ratio"] >= spike_multiplier_quiet,
        )
        logger.info("")

    sys.exit(0)


if __name__ == "__main__":
    main()
