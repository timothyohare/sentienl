#!/usr/bin/env python3
"""
scripts/signal_scorecard.py — real signal-to-noise scorecard from price
follow-through data (plans/05-price-follow-through.md).

Unlike signal_diagnostics.py's burst/correlation proxies, this uses the
`post_price_tracking` rows that price_followup.py backfills to compute an
actual event-study number: the percent price move after each HIGH/CRITICAL
signal, per (source, signal_type, horizon).

Baseline: `price_followup.py`'s backfill cadence also drops a continuous,
non-signal-triggered price sample per tracked instrument into
`price_samples` (plan 05's originally-deferred "true random-window
baseline" scope). Where enough of those samples exist for a horizon, this
script uses their pooled median effect size as the baseline — a real
"how much does this instrument normally move over N minutes" null. Until
enough samples accumulate for a horizon (t1440 needs about a day of 5-min
sampling before any pair even exists), that horizon falls back to the
pooled median effect size across all OTHER tracked (source, signal_type)
pairs — a proxy, not a true null hypothesis: "moved more than other
tracked signals typically do," not "moved more than market noise
typically does." Each printed line is tagged `[random]` or `[proxy]` so
it's clear which bar was actually cleared.

Two sources need special handling or their baseline is ~0 and every
signal "beats" it (found 2026-10-14):
  - ASX is shut ~18h a day, so most 5-min samples are unchanged. Only
    samples taken inside the Sydney trading session are paired.
  - Kalshi prices are unchanged in ~99% of sample pairs (thin markets), so
    the median move is exactly 0 at every horizon. Wherever a random-window
    median is 0, the comparison switches to move *rate* — the share of
    windows where the price moved at all, after a signal vs at random —
    tagged `[rate]`.

Exit code is always 0 — this is a reporting tool, not a pass/fail gate.
"""

import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, time
from statistics import median
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sentinel.core.db import Database
from sentinel.scripts.price_followup import HORIZONS as HORIZON_MINUTES
from sentinel.scripts.price_followup import random_window_effect_sizes

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("sentinel.signal_scorecard")

HORIZONS = tuple(column for column, _minutes in HORIZON_MINUTES)

ASX_TZ = ZoneInfo("Australia/Sydney")
ASX_OPEN, ASX_CLOSE = time(10, 0), time(16, 0)  # continuous trading session


def in_asx_session(moment: datetime) -> bool:
    """True if `moment` (tz-aware) falls in ASX continuous trading,
    10:00-16:00 Sydney time, Monday-Friday. Public holidays aren't
    excluded; they only add a few flat days a year."""
    local = moment.astimezone(ASX_TZ)
    return local.weekday() < 5 and ASX_OPEN <= local.time() < ASX_CLOSE


def _baseline_samples(db: Database, source: str, instrument: str) -> list[dict]:
    samples = db.price_samples.get_samples(source, instrument)
    if source == "asx":
        samples = [
            s for s in samples
            if in_asx_session(datetime.fromisoformat(str(s["sampled_at"]).replace("Z", "+00:00")))
        ]
    return samples


def _fetch_tracked_rows(db: Database) -> list[dict]:
    return db.execute_fetchall(
        "SELECT pt.signal_id, pt.source, pt.instrument, s.signal_type, "
        "pt.price_t0, pt.price_t15, pt.price_t60, pt.price_t240, pt.price_t1440 "
        "FROM post_price_tracking pt "
        "JOIN signals s ON s.id = pt.signal_id "
        "WHERE pt.price_t0 IS NOT NULL AND pt.price_t0 != 0"
    )


def effect_size(price_t0: float, price_tn: float) -> float:
    """Absolute fractional price move from t0 to the given horizon."""
    return abs(price_tn - price_t0) / abs(price_t0)


def compute_effect_sizes(rows: list[dict]) -> dict[tuple[str, str, str], list[float]]:
    """Map (source, signal_type, horizon) -> list of effect sizes."""
    sizes: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for row in rows:
        for horizon in HORIZONS:
            value = row.get(horizon)
            if value is None:
                continue
            sizes[(row["source"], row["signal_type"], horizon)].append(
                effect_size(row["price_t0"], value)
            )
    return sizes


def compute_baseline(sizes: dict[tuple[str, str, str], list[float]]) -> dict[str, float]:
    """Pooled median effect size per horizon, across all (source, signal_type)
    groups — see module docstring for why this isn't a true random-window
    baseline."""
    by_horizon: dict[str, list[float]] = defaultdict(list)
    for (_source, _signal_type, horizon), values in sizes.items():
        by_horizon[horizon].extend(values)
    return {horizon: median(values) for horizon, values in by_horizon.items() if values}


def compute_random_baseline_stats(
    db: Database, instruments: set[tuple[str, str]], min_samples: int = 5
) -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], float]]:
    """True random-window baseline: from `price_samples` pairs (see
    `price_followup.random_window_effect_sizes`), partitioned by
    (source, horizon), return (median effect size, move rate) — move rate
    being the share of pairs whose price changed at all. Pooling
    instruments of the *same* source (e.g. multiple Kalshi tickers) is
    fine, but pooling across different sources is not: Kalshi's
    mostly-flat thin markets vastly outnumber futures' sample pairs and
    drag a globally-pooled median toward zero, understating what "normal"
    movement looks like for a liquid futures instrument. A (source,
    horizon) is included only once it has at least `min_samples` pooled
    data points — otherwise `build_scorecard()` falls back to the
    pooled-proxy baseline for that (source, horizon)."""
    by_source_horizon: dict[tuple[str, str], list[float]] = defaultdict(list)
    for source, instrument in instruments:
        samples = _baseline_samples(db, source, instrument)
        for column, horizon_minutes in HORIZON_MINUTES:
            by_source_horizon[(source, column)].extend(
                random_window_effect_sizes(samples, horizon_minutes)
            )
    medians, move_rates = {}, {}
    for key, values in by_source_horizon.items():
        if len(values) >= min_samples:
            medians[key] = median(values)
            move_rates[key] = _move_rate(values)
    return medians, move_rates


def compute_random_baseline(
    db: Database, instruments: set[tuple[str, str]], min_samples: int = 5
) -> dict[tuple[str, str], float]:
    """Median-only view of compute_random_baseline_stats()."""
    return compute_random_baseline_stats(db, instruments, min_samples)[0]


def _move_rate(values: list[float]) -> float:
    return sum(1 for v in values if v > 0) / len(values)


def build_scorecard(
    rows: list[dict],
    random_baseline: dict[tuple[str, str], float] | None = None,
    random_move_rate: dict[tuple[str, str], float] | None = None,
) -> list[dict]:
    """Per (source, signal_type, horizon): sample size, median effect size,
    and whether it beats the baseline for that horizon. Prefers the true
    random-window baseline (see compute_random_baseline), keyed by
    (source, horizon) so a group is only ever compared against its own
    source's noise floor, where enough price_samples data exists; falls
    back to the pooled-proxy baseline (median across other tracked signal
    types — see module docstring) for any (source, horizon) without it
    yet. Where the random-window median is 0 and a move rate is known,
    the verdict compares move rates instead (see module docstring)."""
    random_baseline = random_baseline or {}
    random_move_rate = random_move_rate or {}
    sizes = compute_effect_sizes(rows)
    proxy_baseline = compute_baseline(sizes)

    scorecard = []
    for (source, signal_type, horizon), values in sorted(sizes.items()):
        if (source, horizon) in random_baseline:
            baseline_value = random_baseline[(source, horizon)]
            baseline_source = "random_window"
        else:
            baseline_value = proxy_baseline.get(horizon, 0.0)
            baseline_source = "pooled_proxy"
        median_value = median(values)
        move_rate = _move_rate(values)
        baseline_move_rate = random_move_rate.get((source, horizon))
        if (baseline_source == "random_window" and baseline_value == 0
                and baseline_move_rate is not None):
            baseline_source = "random_move_rate"
            beats = move_rate > baseline_move_rate
        else:
            beats = median_value > baseline_value
        scorecard.append({
            "source": source,
            "signal_type": signal_type,
            "horizon": horizon,
            "n": len(values),
            "median_effect_size": median_value,
            "baseline_effect_size": baseline_value,
            "baseline_source": baseline_source,
            "move_rate": move_rate,
            "baseline_move_rate": baseline_move_rate,
            "beats_baseline": beats,
        })
    return scorecard


def main():
    parser = argparse.ArgumentParser(description="Sentinel price follow-through scorecard")
    parser.add_argument("--db", default=os.environ.get("SENTINEL_DB", "sentinel.db"))
    parser.add_argument(
        "--min-sample", type=int, default=3,
        help="Minimum sample size to report a group (smaller samples are noisy)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.db):
        logger.error("Database not found: %s", args.db)
        sys.exit(1)

    db = Database(args.db)
    db.init()
    rows = _fetch_tracked_rows(db)
    instruments = {(r["source"], r["instrument"]) for r in rows}
    random_baseline, random_move_rate = compute_random_baseline_stats(db, instruments)
    db.close()

    if not rows:
        logger.info("No price-tracked signals yet — nothing to score.")
        sys.exit(0)

    scorecard = build_scorecard(rows, random_baseline, random_move_rate)

    logger.info("=== Signal-to-noise scorecard (price follow-through) ===")
    logger.info("[random] = true random-window baseline from price_samples.")
    logger.info("[rate]   = random-window median move is 0, so compares how often")
    logger.info("           the price moved at all: after signal vs at random.")
    logger.info("[proxy]  = pooled median across other tracked signal types")
    logger.info("           (not a true null — see script docstring), used until")
    logger.info("           enough price_samples accumulate for that horizon.")
    logger.info("")
    reported = 0
    for entry in scorecard:
        if entry["n"] < args.min_sample:
            continue
        reported += 1
        tag = {"random_window": "[random]", "random_move_rate": "[rate]  "}.get(
            entry["baseline_source"], "[proxy] ")
        verdict = "beats baseline" if entry["beats_baseline"] else "at/below baseline"
        if entry["baseline_source"] == "random_move_rate":
            logger.info(
                "  %-14s %-14s %-11s n=%-4d moved=%.0f%% baseline moved=%.1f%% %s (%s)",
                entry["source"], entry["signal_type"], entry["horizon"],
                entry["n"], entry["move_rate"] * 100,
                entry["baseline_move_rate"] * 100, tag, verdict,
            )
            continue
        logger.info(
            "  %-14s %-14s %-11s n=%-4d median=%.3f%% baseline=%.3f%% %s (%s)",
            entry["source"], entry["signal_type"], entry["horizon"],
            entry["n"], entry["median_effect_size"] * 100,
            entry["baseline_effect_size"] * 100, tag, verdict,
        )

    skipped = len(scorecard) - reported
    if skipped:
        logger.info("")
        logger.info("  (%d group(s) skipped: sample size below --min-sample=%d)",
                    skipped, args.min_sample)

    sys.exit(0)


if __name__ == "__main__":
    main()
