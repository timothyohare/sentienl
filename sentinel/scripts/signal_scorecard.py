#!/usr/bin/env python3
"""
scripts/signal_scorecard.py — real signal-to-noise scorecard from price
follow-through data (plans/05-price-follow-through.md).

Unlike signal_diagnostics.py's burst/correlation proxies, this uses the
`post_price_tracking` rows that price_followup.py backfills to compute an
actual event-study number: the percent price move after each HIGH/CRITICAL
signal, per (source, signal_type, horizon).

Baseline caveat: a rigorous baseline would compare against random
(non-signal-triggered) windows on the same instrument at the same time of
day. That needs a continuous price history this schema doesn't collect —
deliberately out of scope for plan 05 ("no schema change needed"). The
baseline used here is the pooled median effect size across all OTHER
tracked (source, signal_type) pairs at the same horizon. It's a real,
data-derived comparison bar (not a heuristic guess), but it is *not* a true
random-time null hypothesis — a signal type "beating baseline" here means
"moved more than other tracked signals typically do," not "moved more than
market noise typically does." Treat it as a lower bar to clear.

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

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("sentinel.signal_scorecard")

HORIZONS = ("price_t15", "price_t60", "price_t240", "price_t1440")


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


def build_scorecard(rows: list[dict]) -> list[dict]:
    """Per (source, signal_type, horizon): sample size, median effect size,
    and whether it beats the pooled baseline for that horizon."""
    sizes = compute_effect_sizes(rows)
    baseline = compute_baseline(sizes)

    scorecard = []
    for (source, signal_type, horizon), values in sorted(sizes.items()):
        baseline_value = baseline.get(horizon, 0.0)
        median_value = median(values)
        scorecard.append({
            "source": source,
            "signal_type": signal_type,
            "horizon": horizon,
            "n": len(values),
            "median_effect_size": median_value,
            "baseline_effect_size": baseline_value,
            "beats_baseline": median_value > baseline_value,
        })
    return scorecard


def main():
    parser = argparse.ArgumentParser(description="Sentinel price follow-through scorecard")
    parser.add_argument("--db", default=os.environ.get("SENTINEL_DB", "sentinel.db"))
    parser.add_argument(
        "--min-sample", type=int, default=5,
        help="Minimum sample size to report a group (smaller samples are noisy)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.db):
        logger.error("Database not found: %s", args.db)
        sys.exit(1)

    db = Database(args.db)
    db.init()
    rows = _fetch_tracked_rows(db)
    db.close()

    if not rows:
        logger.info("No price-tracked signals yet — nothing to score.")
        sys.exit(0)

    scorecard = build_scorecard(rows)

    logger.info("=== Signal-to-noise scorecard (price follow-through) ===")
    logger.info("Baseline = pooled median effect size across all tracked signal types at")
    logger.info("that horizon (not a true random-window baseline — see script docstring).")
    logger.info("")
    reported = 0
    for entry in scorecard:
        if entry["n"] < args.min_sample:
            continue
        reported += 1
        verdict = "beats baseline" if entry["beats_baseline"] else "at/below baseline"
        logger.info(
            "  %-14s %-14s %-11s n=%-4d median=%.3f%% baseline=%.3f%% (%s)",
            entry["source"], entry["signal_type"], entry["horizon"],
            entry["n"], entry["median_effect_size"] * 100,
            entry["baseline_effect_size"] * 100, verdict,
        )

    skipped = len(scorecard) - reported
    if skipped:
        logger.info("")
        logger.info("  (%d group(s) skipped: sample size below --min-sample=%d)",
                    skipped, args.min_sample)

    sys.exit(0)


if __name__ == "__main__":
    main()
