#!/usr/bin/env python3
"""
scripts/price_followup.py — Backfill scheduler for post-signal price tracking.

kalshi.py / futures_volume.py snapshot price_t0 at signal time (see
plans/05-price-follow-through.md). This script fills in the later horizons
(price_t15/t60/t240/t1440) once enough real time has passed, by fetching the
current price for each pending (source, instrument) pair — batched so a run
with many pending rows on the same instrument only hits the network once for
that instrument, not once per row/column.

Run on a timer (every 5-10 min is enough — the coarsest horizon that can go
stale from a slow cadence is +15 min).

Each run also snapshots the current price for every instrument that has
ever been signal-tracked, into `price_samples` — independent of whether a
signal fired this run. That continuous, non-signal-triggered history is
what `signal_scorecard.py` uses to build a true random-window baseline
(plan 05's originally deferred scope), instead of comparing signal types
only against each other.
"""

import argparse
import logging
import os
import sys
import time as time_module
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sentinel.collectors.kalshi import KALSHI_API_BASE
from sentinel.core.db import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("sentinel.price_followup")

POLL_INTERVAL_SECONDS = 300  # 5 min

# (column, minutes-after-t0) — matches the post_price_tracking schema
HORIZONS = (
    ("price_t15", 15),
    ("price_t60", 60),
    ("price_t240", 240),
    ("price_t1440", 1440),
)


# ---------------------------------------------------------------------------
# Price-fetch client protocol (for type-checking and testability)
# ---------------------------------------------------------------------------

class PriceFetcherProtocol(Protocol):
    def get_price(self, source: str, instrument: str) -> float | None: ...


# ---------------------------------------------------------------------------
# Pure helpers (no network — directly testable)
# ---------------------------------------------------------------------------

def due_updates(
    pending: list[dict[str, Any]], now: datetime
) -> list[tuple[dict[str, Any], str]]:
    """Return (row, column) pairs whose horizon has elapsed but are still NULL."""
    due = []
    for row in pending:
        try:
            created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
        except (ValueError, TypeError, KeyError):
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age_minutes = (now - created).total_seconds() / 60
        for column, horizon_minutes in HORIZONS:
            if row.get(column) is None and age_minutes >= horizon_minutes:
                due.append((row, column))
    return due


def group_by_instrument(
    due: list[tuple[dict[str, Any], str]],
) -> dict[tuple[str, str], list[tuple[dict[str, Any], str]]]:
    """Group due (row, column) pairs by (source, instrument) for batched fetching."""
    groups: dict[tuple[str, str], list[tuple[dict[str, Any], str]]] = {}
    for row, column in due:
        key = (row["source"], row["instrument"])
        groups.setdefault(key, []).append((row, column))
    return groups


def random_window_effect_sizes(
    samples: list[dict[str, Any]],
    horizon_minutes: float,
    tolerance_minutes: float | None = None,
) -> list[float]:
    """True random-window baseline math: given time-ordered (oldest-first)
    price samples for one instrument, return abs(price_delta)/price for
    every sample pair whose time gap falls within horizon_minutes +/-
    tolerance. This measures how much the instrument moves over N minutes
    when nothing signal-worthy happened, from routine backfill-cadence
    sampling — not another signal-triggered measurement.
    """
    tolerance = tolerance_minutes if tolerance_minutes is not None else horizon_minutes * 0.2
    lower, upper = horizon_minutes - tolerance, horizon_minutes + tolerance
    sizes = []
    for i, s1 in enumerate(samples):
        t1 = datetime.fromisoformat(str(s1["sampled_at"]).replace("Z", "+00:00"))
        for s2 in samples[i + 1:]:
            t2 = datetime.fromisoformat(str(s2["sampled_at"]).replace("Z", "+00:00"))
            gap_minutes = (t2 - t1).total_seconds() / 60
            if gap_minutes > upper:
                break  # samples are time-ordered: no later pair will be closer
            if gap_minutes >= lower:
                sizes.append(abs(s2["price"] - s1["price"]) / abs(s1["price"]))
    return sizes


# ---------------------------------------------------------------------------
# Live price fetchers
# ---------------------------------------------------------------------------

class KalshiPriceFetcher:
    """Fetches the current YES price for a single Kalshi market ticker."""

    def __init__(self, api_base: str = KALSHI_API_BASE):
        self._client = httpx.Client(timeout=15.0, follow_redirects=True)
        self._api_base = api_base

    def get_price(self, source: str, instrument: str) -> float | None:
        if source != "kalshi":
            return None
        try:
            resp = self._client.get(f"{self._api_base}/markets/{instrument}")
            if resp.status_code != 200:
                logger.warning("Kalshi market API returned HTTP %d for %r",
                               resp.status_code, instrument)
                return None
            market = resp.json().get("market", {})
            price = float(market.get("last_price_dollars", 0))
            return price or None
        except Exception as exc:
            logger.error("Failed to fetch Kalshi price for %r: %s", instrument, exc)
            return None


class FuturesPriceFetcher:
    """Fetches the latest 1-min close price for a yfinance futures ticker."""

    def get_price(self, source: str, instrument: str) -> float | None:
        if not source.startswith("futures_"):
            return None
        try:
            import yfinance as yf
            df = yf.Ticker(instrument).history(period="1d", interval="1m")
            if df is None or df.empty:
                logger.warning("yfinance returned empty data for %s", instrument)
                return None
            price = float(df["Close"].iloc[-1])
            return price or None
        except Exception as exc:
            logger.error("yfinance fetch failed for %s: %s", instrument, exc)
            return None


class CompositePriceFetcher:
    """Dispatches to the first fetcher that recognises the row's `source`."""

    def __init__(self, fetchers: list[PriceFetcherProtocol]):
        self._fetchers = fetchers

    def get_price(self, source: str, instrument: str) -> float | None:
        for fetcher in self._fetchers:
            price = fetcher.get_price(source, instrument)
            if price is not None:
                return price
        return None


# ---------------------------------------------------------------------------
# Backfill logic
# ---------------------------------------------------------------------------

def run_once(
    db: Database, fetcher: PriceFetcherProtocol, now: datetime | None = None
) -> int:
    """Run one backfill pass. Returns the number of columns updated."""
    now = now or datetime.now(UTC)
    pending = db.price_tracking.get_pending_updates()
    due = due_updates(pending, now)
    if not due:
        return 0

    updated = 0
    for (source, instrument), members in group_by_instrument(due).items():
        price = fetcher.get_price(source, instrument)
        if price is None:
            logger.warning("No price available for %s/%s — skipping %d pending column(s)",
                           source, instrument, len(members))
            continue
        for row, column in members:
            db.price_tracking.update_price(row["signal_id"], instrument, column, price)
            updated += 1
    return updated


def sample_baseline_prices(db: Database, fetcher: PriceFetcherProtocol) -> int:
    """Snapshot the current price for every instrument that has ever been
    price-tracked, regardless of whether a signal fired right now — this is
    what makes the resulting price_samples rows a random-window baseline
    instead of another signal-triggered measurement. Returns the number of
    instruments sampled.
    """
    sampled = 0
    for source, instrument in db.price_tracking.distinct_instruments():
        price = fetcher.get_price(source, instrument)
        if price is not None:
            db.price_samples.insert(source, instrument, price)
            sampled += 1
    return sampled


def main():
    parser = argparse.ArgumentParser(
        description="Sentinel post-signal price follow-through backfill"
    )
    parser.add_argument("--db", default=os.environ.get("SENTINEL_DB", "sentinel.db"))
    parser.add_argument("--once", action="store_true", help="Run a single pass and exit")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL_SECONDS)
    args = parser.parse_args()

    db = Database(args.db)
    db.init()
    fetcher = CompositePriceFetcher([KalshiPriceFetcher(), FuturesPriceFetcher()])

    if args.once:
        updated = run_once(db, fetcher)
        sampled = sample_baseline_prices(db, fetcher)
        logger.info("Backfilled %d price column(s), sampled %d baseline price(s)",
                    updated, sampled)
        db.close()
        return

    logger.info("Price follow-through backfill starting up (interval=%ds)", args.interval)
    while True:
        try:
            updated = run_once(db, fetcher)
            sampled = sample_baseline_prices(db, fetcher)
            if updated or sampled:
                logger.info("Backfilled %d price column(s), sampled %d baseline price(s)",
                            updated, sampled)
        except Exception as exc:
            logger.error("Backfill pass failed: %s", exc)
        time_module.sleep(args.interval)


if __name__ == "__main__":
    main()
