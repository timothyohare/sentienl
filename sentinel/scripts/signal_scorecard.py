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

Exit code is always 0 — this is a reporting tool, not a pass/fail gate.
"""

import argparse
import logging
import os
import sys
from collections import defaultdict
from statistics import median

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sentinel.core.db import Database
from sentinel.scripts.price_followup import HORIZONS as HORIZON_MINUTES
from sentinel.scripts.price_followup import random_window_effect_sizes

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("sentinel.signal_scorecard")

HORIZONS = tuple(column for column, _minutes in HORIZON_MINUTES)


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


def compute_random_baseline(
    db: Database, instruments: set[tuple[str, str]], min_samples: int = 5
) -> dict[str, float]:
    """True random-window baseline: pooled median effect size from
    `price_samples` pairs (see `price_followup.random_window_effect_sizes`),
    across every tracked (source, instrument), per horizon. A horizon is
    included only once it has at least `min_samples` pooled data points —
    otherwise `build_scorecard()` falls back to the pooled-proxy baseline
    for that horizon."""
    by_horizon: dict[str, list[float]] = defaultdict(list)
    for source, instrument in instruments:
        samples = db.price_samples.get_samples(source, instrument)
        for column, horizon_minutes in HORIZON_MINUTES:
            by_horizon[column].extend(random_window_effect_sizes(samples, horizon_minutes))
    return {
        horizon: median(values)
        for horizon, values in by_horizon.items()
        if len(values) >= min_samples
    }


def build_scorecard(
    rows: list[dict], random_baseline: dict[str, float] | None = None
) -> list[dict]:
    """Per (source, signal_type, horizon): sample size, median effect size,
    and whether it beats the baseline for that horizon. Prefers the true
    random-window baseline (see compute_random_baseline) where enough
    price_samples data exists; falls back to the pooled-proxy baseline
    (median across other tracked signal types — see module docstring) for
    any horizon without it yet."""
    random_baseline = random_baseline or {}
    sizes = compute_effect_sizes(rows)
    proxy_baseline = compute_baseline(sizes)

    scorecard = []
    for (source, signal_type, horizon), values in sorted(sizes.items()):
        if horizon in random_baseline:
            baseline_value = random_baseline[horizon]
            baseline_source = "random_window"
        else:
            baseline_value = proxy_baseline.get(horizon, 0.0)
            baseline_source = "pooled_proxy"
        median_value = median(values)
        scorecard.append({
            "source": source,
            "signal_type": signal_type,
            "horizon": horizon,
            "n": len(values),
            "median_effect_size": median_value,
            "baseline_effect_size": baseline_value,
            "baseline_source": baseline_source,
            "beats_baseline": median_value > baseline_value,
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
    random_baseline = compute_random_baseline(db, instruments)
    db.close()

    if not rows:
        logger.info("No price-tracked signals yet — nothing to score.")
        sys.exit(0)

    scorecard = build_scorecard(rows, random_baseline)

    logger.info("=== Signal-to-noise scorecard (price follow-through) ===")
    logger.info("[random] = true random-window baseline from price_samples.")
    logger.info("[proxy]  = pooled median across other tracked signal types")
    logger.info("           (not a true null — see script docstring), used until")
    logger.info("           enough price_samples accumulate for that horizon.")
    logger.info("")
    reported = 0
    for entry in scorecard:
        if entry["n"] < args.min_sample:
            continue
        reported += 1
        tag = "[random]" if entry["baseline_source"] == "random_window" else "[proxy] "
        verdict = "beats baseline" if entry["beats_baseline"] else "at/below baseline"
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
