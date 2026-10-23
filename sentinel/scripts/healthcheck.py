#!/usr/bin/env python3
"""
scripts/healthcheck.py — Sentinel system health checker.

Checks that all collectors' systemd units are actually running, and that
each has filed recent signals, then optionally sends a heartbeat ntfy
notification. Designed for cron scheduling every 60 minutes.

The health check itself still runs every cron cycle (hourly) so a dead
collector is still caught within ~1hr, same as before. Only the *ntfy
message* is throttled: a routine "all healthy" heartbeat is sent at most
twice a day (`--heartbeat-hours`, default 8am/8pm AEST), while a DOWN
status is always sent immediately regardless of schedule — the throttle
exists to cut noise on the happy path, not to delay outage detection
(see the 2026-08-11 Truth Social incident below).

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

Plus two checks that catch collectors/alerter that are running but not
doing their job (both 2026-10-14 bugs passed the liveness check):
  - Alerter backlog — any signal still `alerted=0` more than
    `--backlog-minutes` (default 10) after insert means the alerter is
    stuck. The alerter marks every signal it handles, sent or suppressed,
    within seconds.
  - IB fallback — a collector recently fell back from IB Gateway to
    *fresh* yfinance bars (market open, IB returned nothing) with no IB
    success since. See futures_volume.record_ib_outcome().
Either one fails the check, like DOWN.

Cron entry (adjust paths):
  0 * * * * /home/timohare/dev/newdev/Sentinel/venv/bin/python \
    /home/timohare/dev/newdev/Sentinel/sentinel/scripts/healthcheck.py \
    --config /home/timohare/dev/newdev/Sentinel/config.yaml \
    --db /home/timohare/dev/newdev/Sentinel/sentinel.db \
    --heartbeat

Exit codes:
  0 — No collector unit is DOWN (QUIET is not a failure)
  1 — A collector unit is DOWN, the alerter is backlogged, an IB feed has
      fallen back to yfinance, or the DB is inaccessible
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
    "asx": "sentinel-asx.service",
}

STALE_THRESHOLD_MINUTES = 30  # a live unit with no signals in this long is QUIET
ALERTER_BACKLOG_MINUTES = 10  # an unalerted signal older than this means the alerter is stuck
IB_FALLBACK_WINDOW_MINUTES = 30  # only fallbacks this recent count

AEST_OFFSET = timedelta(hours=10)  # fixed offset, matches dashboard/app.py
HEARTBEAT_HOURS_AEST = (8, 20)  # routine "all healthy" heartbeat sent only at these hours


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


def alerter_backlog(db: Database, older_than_minutes: int) -> int:
    """Number of signals the alerter hasn't handled within the window."""
    cutoff = (datetime.now(UTC) - timedelta(minutes=older_than_minutes)).isoformat()
    return db.execute_scalar(
        "SELECT COUNT(*) FROM signals WHERE alerted=0 AND created_at < ?", (cutoff,)
    ) or 0


def ib_fallbacks(db: Database, window_minutes: int) -> list[str]:
    """`<source>:<ticker>` feeds whose most recent outcome (within the
    window) was a fallback from IB to fresh yfinance bars."""
    cutoff = datetime.now(UTC) - timedelta(minutes=window_minutes)
    rows = db.execute_fetchall(
        "SELECT key, value FROM state WHERE key LIKE 'ib_fallback:%' OR key LIKE 'ib_ok:%'"
    )
    latest = {row["key"]: datetime.fromisoformat(row["value"]) for row in rows}
    failing = []
    for key, fell_back_at in sorted(latest.items()):
        if not key.startswith("ib_fallback:") or fell_back_at < cutoff:
            continue
        feed = key.removeprefix("ib_fallback:")
        ok_at = latest.get(f"ib_ok:{feed}")
        if ok_at is None or ok_at < fell_back_at:
            failing.append(feed)
    return failing


def should_send_heartbeat(any_down: bool, heartbeat_hours: set[int], now_utc: datetime) -> bool:
    """A DOWN status always sends immediately; otherwise the routine
    'all healthy' heartbeat only sends during one of `heartbeat_hours`
    (AEST, matching the dashboard's display timezone)."""
    if any_down:
        return True
    return (now_utc + AEST_OFFSET).hour in heartbeat_hours


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
    parser.add_argument("--backlog-minutes", type=int, default=ALERTER_BACKLOG_MINUTES)
    parser.add_argument(
        "--heartbeat-hours", default=",".join(str(h) for h in HEARTBEAT_HOURS_AEST),
        help="Comma-separated AEST hours (0-23) at which the routine 'all "
             "healthy' heartbeat is sent, e.g. '8,20'. A DOWN status is "
             "always sent immediately regardless of this schedule.",
    )
    args = parser.parse_args()

    # Load DB
    if not os.path.exists(args.db):
        logger.error("Database not found: %s", args.db)
        sys.exit(1)

    db = Database(args.db)
    db.init()

    results = check_health(db, args.stale_minutes, SystemdUnitChecker())
    down = [s for s, r in results.items() if r["status"] == "DOWN"]
    backlog = alerter_backlog(db, args.backlog_minutes)
    fallbacks = ib_fallbacks(db, IB_FALLBACK_WINDOW_MINUTES)
    all_healthy = not down and not backlog and not fallbacks

    for source, status in results.items():
        logger.info(
            "[%s] %s — unit=%s signals=%d in last %dm",
            status["status"], source, status["unit"],
            status["signals_in_window"], args.stale_minutes,
        )
    logger.info("[%s] alerter — %d signal(s) unalerted after %dm",
                "STUCK" if backlog else "OK", backlog, args.backlog_minutes)
    logger.info("[%s] IB Gateway — fell back to yfinance: %s",
                "DEGRADED" if fallbacks else "OK", ", ".join(fallbacks) or "none")

    if args.heartbeat and os.path.exists(args.config):
        heartbeat_hours = {int(h) for h in args.heartbeat_hours.split(",") if h.strip()}
        if should_send_heartbeat(not all_healthy, heartbeat_hours, datetime.now(UTC)):
            try:
                config = load_config(args.config)
                lines = [f"{source}: {r['status']}" for source, r in results.items()]
                if backlog:
                    lines.append(f"alerter: STUCK ({backlog} unalerted >{args.backlog_minutes}m)")
                if fallbacks:
                    lines.append(f"IB fallback to yfinance: {', '.join(fallbacks)}")
                message = "Sentinel alive\n" + "\n".join(lines)
                send_heartbeat(config, message)
            except Exception as exc:
                logger.error("Heartbeat failed: %s", exc)
        else:
            logger.info(
                "Heartbeat suppressed (all healthy, outside %s AEST schedule)",
                sorted(heartbeat_hours),
            )

    db.close()

    if not all_healthy:
        logger.warning("Unhealthy — down units: %s, alerter backlog: %d, IB fallbacks: %s",
                       down, backlog, fallbacks)
        sys.exit(1)

    logger.info("All collector units up, alerter current, IB serving data")
    sys.exit(0)


if __name__ == "__main__":
    main()
