#!/usr/bin/env python3
"""
scripts/healthcheck.py — Sentinel system health checker.

Checks that all collectors' systemd units are actually running, and that
each has filed recent signals, then optionally sends a heartbeat ntfy
notification. Designed for cron scheduling every 60 minutes.

Two independent checks per source, combined into one status:
  - Unit liveness (`systemctl --user is-active <unit>`) — authoritative.
    A unit that isn't active is DOWN regardless of signal history. This is
    what actually catches a crash-looped/dead collector (see the 2026-08-11
    Truth Social incident: the process was dead for 3 days, but the old
    signal-only check would only have flagged it during the exact windows
    Trump happened not to post anyway).
  - Signal recency — secondary/informational. A unit can be alive and
    correctly polling but legitimately have nothing to report (e.g. Kalshi
    between large bets, or futures overnight). That's QUIET, not a failure
    — only DOWN fails the health check and the exit code.

Cron entry (adjust paths):
  0 * * * * /home/timohare/dev/newdev/Sentinel/venv/bin/python \
    /home/timohare/dev/newdev/Sentinel/sentinel/scripts/healthcheck.py \
    --config /home/timohare/dev/newdev/Sentinel/config.yaml \
    --db /home/timohare/dev/newdev/Sentinel/sentinel.db \
    --heartbeat

Exit codes:
  0 — No collector unit is DOWN (QUIET is not a failure)
  1 — One or more collector units are DOWN, or the DB is inaccessible
"""

import argparse
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Protocol

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sentinel.core.config import load_config
from sentinel.core.db import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("sentinel.healthcheck")

# Sources that must have a live systemd unit to be considered healthy,
# mapped to the unit that produces them.
MONITORED_UNITS: dict[str, str] = {
    "truth_social": "sentinel-truth-social.service",
    "kalshi": "sentinel-kalshi.service",
    "futures_oil": "sentinel-futures.service",
}

STALE_THRESHOLD_MINUTES = 30  # a live unit with no signals in this long is QUIET


# ---------------------------------------------------------------------------
# Unit liveness (systemd --user)
# ---------------------------------------------------------------------------

class UnitCheckerProtocol(Protocol):
    def is_active(self, unit: str) -> bool: ...


class SystemdUnitChecker:
    """Checks `systemctl --user is-active <unit>`. Cron runs with a minimal
    environment that lacks XDG_RUNTIME_DIR, which breaks `--user` bus
    access — set it explicitly from the current uid if not already
    present rather than relying on the caller's environment."""

    def is_active(self, unit: str) -> bool:
        env = dict(os.environ)
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", unit],
                env=env, capture_output=True, text=True, timeout=5, check=False,
            )
            return result.stdout.strip() == "active"
        except Exception as exc:
            logger.error("systemctl check failed for %s: %s", unit, exc)
            return False


# ---------------------------------------------------------------------------
# Health evaluation
# ---------------------------------------------------------------------------

def check_health(
    db: Database,
    stale_threshold_minutes: int,
    unit_checker: UnitCheckerProtocol,
) -> dict:
    """Return health status for each monitored source: DOWN (unit not
    active — the only failing status), OK (active, recent signal), or
    QUIET (active, no signal in the window — not a failure)."""
    now = datetime.now(UTC)
    cutoff = (now - timedelta(minutes=stale_threshold_minutes)).isoformat()
    results = {}

    for source, unit in MONITORED_UNITS.items():
        unit_active = unit_checker.is_active(unit)
        count = db.execute_scalar(
            "SELECT COUNT(*) FROM signals WHERE source=? AND created_at >= ?",
            (source, cutoff),
        ) or 0

        if not unit_active:
            status = "DOWN"
        elif count > 0:
            status = "OK"
        else:
            status = "QUIET"

        results[source] = {
            "status": status,
            "unit": unit,
            "unit_active": unit_active,
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

    results = check_health(db, args.stale_minutes, SystemdUnitChecker())
    down = [s for s, r in results.items() if r["status"] == "DOWN"]
    all_healthy = not down

    for source, status in results.items():
        logger.info(
            "[%s] %s — unit=%s signals=%d in last %dm",
            status["status"], source, status["unit"],
            status["signals_in_window"], args.stale_minutes,
        )

    if args.heartbeat and os.path.exists(args.config):
        try:
            config = load_config(args.config)
            lines = [f"{source}: {r['status']}" for source, r in results.items()]
            message = "Sentinel alive\n" + "\n".join(lines)
            send_heartbeat(config, message)
        except Exception as exc:
            logger.error("Heartbeat failed: %s", exc)

    db.close()

    if not all_healthy:
        logger.warning("Down collector units: %s", down)
        sys.exit(1)

    logger.info("All collector units up")
    sys.exit(0)


if __name__ == "__main__":
    main()
