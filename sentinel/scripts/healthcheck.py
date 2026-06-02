#!/usr/bin/env python3
"""
scripts/healthcheck.py — Sentinel system health checker.

Checks that all collectors have filed recent signals and optionally sends
a heartbeat ntfy notification. Designed for cron scheduling every 60 minutes.

Cron entry (adjust paths):
  0 * * * * /home/timohare/dev/newdev/Sentinel/venv/bin/python \
    /home/timohare/dev/newdev/Sentinel/sentinel/scripts/healthcheck.py \
    --config /home/timohare/dev/newdev/Sentinel/config.yaml \
    --db /home/timohare/dev/newdev/Sentinel/sentinel.db

Exit codes:
  0 — All collectors healthy
  1 — One or more collectors stale or DB inaccessible
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sentinel.core.db import Database
from sentinel.core.config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("sentinel.healthcheck")

# Sources that must have recent activity to be considered healthy
MONITORED_SOURCES = [
    "truth_social",
    "futures_oil",
    # Polymarket excluded by default as API may be DNS-blocked
]

STALE_THRESHOLD_MINUTES = 30  # collector considered stale if no signals in 30 min


def check_health(db: Database, stale_threshold_minutes: int) -> dict:
    """Return health status for each monitored source."""
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=stale_threshold_minutes)).isoformat()
    results = {}

    for source in MONITORED_SOURCES:
        count = db.execute_scalar(
            "SELECT COUNT(*) FROM signals WHERE source=? AND created_at >= ?",
            (source, cutoff),
        ) or 0
        results[source] = {
            "healthy": count > 0,
            "signals_in_window": count,
            "window_minutes": stale_threshold_minutes,
        }

    return results


def send_heartbeat(config, message: str) -> None:
    """Send a heartbeat notification via ntfy."""
    try:
        import requests
        url = f"{config.alerts.ntfy_url}/{config.alerts.ntfy_topic}"
        requests.post(
            url,
            data=message.encode("utf-8"),
            headers={
                "Priority": "1",
                "Tags": "heartbeat",
                "Title": "Sentinel Heartbeat",
                "Content-Type": "text/plain",
            },
            timeout=10,
        )
    except Exception as exc:
        logger.error("Heartbeat send failed: %s", exc)


def main():
    parser = argparse.ArgumentParser(description="Sentinel health checker")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--db", default="sentinel.db")
    parser.add_argument("--heartbeat", action="store_true",
                        help="Send a heartbeat ntfy notification")
    parser.add_argument("--stale-minutes", type=int, default=STALE_THRESHOLD_MINUTES)
    args = parser.parse_args()

    # Load DB
    if not os.path.exists(args.db):
        logger.error("Database not found: %s", args.db)
        sys.exit(1)

    db = Database(args.db)
    db.init()

    results = check_health(db, args.stale_minutes)
    all_healthy = all(r["healthy"] for r in results.values())

    for source, status in results.items():
        level = "OK" if status["healthy"] else "STALE"
        logger.info(
            "[%s] %s — %d signals in last %dm",
            level, source, status["signals_in_window"], args.stale_minutes,
        )

    if args.heartbeat and os.path.exists(args.config):
        try:
            config = load_config(args.config)
            lines = [f"{source}: {'OK' if r['healthy'] else 'STALE'}" for source, r in results.items()]
            message = "Sentinel alive\n" + "\n".join(lines)
            send_heartbeat(config, message)
        except Exception as exc:
            logger.error("Heartbeat failed: %s", exc)

    db.close()

    if not all_healthy:
        stale = [s for s, r in results.items() if not r["healthy"]]
        logger.warning("Stale collectors: %s", stale)
        sys.exit(1)

    logger.info("All collectors healthy")
    sys.exit(0)


if __name__ == "__main__":
    main()
