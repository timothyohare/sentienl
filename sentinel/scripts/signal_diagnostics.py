#!/usr/bin/env python3
"""
scripts/signal_diagnostics.py — Rough signal-to-noise diagnostic.

There's no ground-truth label for "was this signal actually market-moving"
anywhere in the DB (that requires price follow-through — see
docs/price-follow-through-plan.md for a real implementation of that). This
script computes two cheap proxies instead, both derivable from data that
already exists:

  1. Burst detection — groups signals by (source, signal_type, per-minute
     bucket) and flags buckets whose count exceeds --burst-threshold. A
     one-poll-cycle burst from a single source is the exact signature of a
     cold-start/backlog bug (see the 2026-08-03 Kalshi _process_trades fix:
     63 large_bet signals in ~7 seconds, then silence) rather than organic
     market activity, which arrives one trade/post/bar at a time.
  2. Correlation confirmation rate — what fraction of HIGH/CRITICAL signals
     (excluding the correlation detector's own output) were ever the anchor
     of a correlated_signal. Corroboration across independent sources is a
     weak but free proxy for "this was real" vs. "fired once, unconfirmed."
     Note this only checks the anchor_signal_id the detector records, not
     every signal inside a correlated window, so it understates the true
     confirmation rate.

Exit code is always 0 — this is a reporting tool, not a pass/fail gate.
"""

import argparse
import json
import logging
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sentinel.core.db import Database

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("sentinel.signal_diagnostics")

DEFAULT_BURST_THRESHOLD = 5  # signals from one (source, signal_type) in one minute


def _fetch_all_signals(db: Database) -> list[dict]:
    return db.execute_fetchall(
        "SELECT id, source, signal_type, priority, payload, created_at "
        "FROM signals ORDER BY created_at"
    )


def find_bursts(signals: list[dict], threshold: int) -> list[dict]:
    """Group by (source, signal_type, minute) and flag buckets over threshold."""
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for sig in signals:
        minute = sig["created_at"][:16]  # "...T14:17" — truncate to the minute
        key = (sig["source"], sig["signal_type"], minute)
        buckets[key].append(sig)

    bursts = []
    for (source, signal_type, minute), members in buckets.items():
        if len(members) > threshold:
            bursts.append({
                "source": source,
                "signal_type": signal_type,
                "minute": minute,
                "count": len(members),
                "excess": len(members) - 1,  # treat 1 signal/bucket as plausibly real
            })
    bursts.sort(key=lambda b: b["excess"], reverse=True)
    return bursts


def correlation_confirmation_rate(signals: list[dict]) -> dict:
    """Fraction of HIGH/CRITICAL non-correlation-detector signals that anchored
    a correlated_signal."""
    anchored_ids = set()
    for sig in signals:
        if sig["signal_type"] != "correlated_signal":
            continue
        try:
            payload = json.loads(sig["payload"])
        except (json.JSONDecodeError, TypeError):
            continue
        anchor_id = payload.get("anchor_signal_id")
        if anchor_id is not None:
            anchored_ids.add(anchor_id)

    candidates = [
        s for s in signals
        if s["priority"] in ("HIGH", "CRITICAL") and s["source"] != "correlation_detector"
    ]
    confirmed = [s for s in candidates if s["id"] in anchored_ids]
    return {
        "total_high_critical": len(candidates),
        "confirmed_by_correlation": len(confirmed),
        "rate": (len(confirmed) / len(candidates)) if candidates else 0.0,
    }


def summarize_by_source_type(signals: list[dict]) -> list[dict]:
    counts: dict[tuple, int] = defaultdict(int)
    for sig in signals:
        counts[(sig["source"], sig["signal_type"], sig["priority"])] += 1
    return [
        {"source": s, "signal_type": t, "priority": p, "count": c}
        for (s, t, p), c in sorted(counts.items(), key=lambda kv: -kv[1])
    ]


def main():
    parser = argparse.ArgumentParser(description="Sentinel signal-to-noise diagnostic")
    parser.add_argument("--db", default=os.environ.get("SENTINEL_DB", "sentinel.db"))
    parser.add_argument("--burst-threshold", type=int, default=DEFAULT_BURST_THRESHOLD,
                        help="Flag (source, signal_type, minute) buckets with more "
                             "signals than this as a likely burst/noise artifact")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        logger.error("Database not found: %s", args.db)
        sys.exit(1)

    db = Database(args.db)
    db.init()
    signals = _fetch_all_signals(db)
    db.close()

    logger.info("=== Signal counts by source / type / priority ===")
    for row in summarize_by_source_type(signals):
        logger.info("  %-20s %-16s %-9s %5d",
                     row["source"], row["signal_type"], row["priority"], row["count"])

    logger.info("")
    logger.info("=== Burst detection (>%d signals in one source/type/minute) ===",
                args.burst_threshold)
    bursts = find_bursts(signals, args.burst_threshold)
    if not bursts:
        logger.info("  None found.")
    else:
        total_excess = sum(b["excess"] for b in bursts)
        for b in bursts:
            logger.info("  %s / %s at %s: %d signals (likely noise: ~%d)",
                        b["source"], b["signal_type"], b["minute"], b["count"], b["excess"])
        logger.info("  Total signals in burst buckets flagged as likely noise: %d / %d (%.1f%%)",
                    total_excess, len(signals), 100 * total_excess / len(signals) if signals else 0)

    logger.info("")
    logger.info("=== Correlation confirmation rate (HIGH/CRITICAL only) ===")
    conf = correlation_confirmation_rate(signals)
    logger.info("  %d / %d HIGH/CRITICAL signals were the anchor of a correlated_signal (%.1f%%)",
                conf["confirmed_by_correlation"], conf["total_high_critical"],
                100 * conf["rate"])
    logger.info("  Note: only counts anchor signals, not every signal inside a")
    logger.info("  correlated window — this understates true confirmation rate.")

    sys.exit(0)


if __name__ == "__main__":
    main()
