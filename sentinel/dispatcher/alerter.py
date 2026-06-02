"""
dispatcher/alerter.py — Alert dispatcher for Sentinel.

Polls SQLite for alerted=0 signals every 2 seconds, formats them, applies
rate limiting and quiet hours enforcement, then sends push notifications via
ntfy. Updates alerted=1 after successful send.

Priority levels (ascending): INFO < LOW < MEDIUM < HIGH < CRITICAL
Truth Social (CRITICAL) signals are NEVER rate-limited or quiet-hour-suppressed.
"""

import logging
import time as time_mod
from datetime import datetime, time, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from sentinel.core.config import is_in_window

logger = logging.getLogger(__name__)

# Priority ordering (index = severity, higher = more severe)
PRIORITY_LEVELS = ["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]

# ntfy priority string (1=min, 5=max)
_NTFY_PRIORITY_MAP = {
    "CRITICAL": "5",
    "HIGH": "4",
    "MEDIUM": "3",
    "LOW": "2",
    "INFO": "1",
}

# ntfy tags per priority
_NTFY_TAGS_MAP = {
    "CRITICAL": "rotating_light",
    "HIGH": "warning",
    "MEDIUM": "bell",
    "LOW": "information_source",
    "INFO": "white_check_mark",
}

POLL_INTERVAL_SECONDS = 2


def _priority_index(priority: str) -> int:
    """Return the numeric index of a priority string (higher = more severe)."""
    try:
        return PRIORITY_LEVELS.index(priority)
    except ValueError:
        return 0


def _priority_to_ntfy_priority(priority: str) -> str:
    """Convert a Sentinel priority string to an ntfy numeric priority string."""
    return _NTFY_PRIORITY_MAP.get(priority, "3")


# ---------------------------------------------------------------------------
# Alert formatter
# ---------------------------------------------------------------------------

class AlertFormatter:
    """Formats signals into (title, body) pairs for ntfy dispatch."""

    @staticmethod
    def format_signal(signal: Dict[str, Any]) -> Tuple[str, str]:
        source = signal.get("source", "")
        signal_type = signal.get("signal_type", "")
        priority = signal.get("priority", "INFO")
        payload = signal.get("payload", {})
        summary = signal.get("summary", "Signal detected")

        if source == "truth_social":
            return AlertFormatter._format_truth_social(signal, payload, priority)
        elif source == "polymarket":
            return AlertFormatter._format_polymarket(signal, payload, signal_type)
        elif source in ("futures_oil", "futures_sp500", "futures_brent",
                        "futures_natgas", "futures_gold", "futures_dxy"):
            return AlertFormatter._format_futures(signal, payload)
        elif source == "correlation_detector":
            return AlertFormatter._format_correlated(signal, payload)
        else:
            return summary, summary

    @staticmethod
    def _format_truth_social(
        signal: Dict, payload: Dict, priority: str
    ) -> Tuple[str, str]:
        post_id = payload.get("post_id", "?")
        text = payload.get("text", signal.get("summary", ""))
        url = payload.get("url", "")
        has_media = payload.get("has_media", False)
        is_reblog = payload.get("is_reblog", False)
        created_at = payload.get("created_at", signal.get("created_at", ""))

        title = f"TRUTH SOCIAL — New Trump post"
        if is_reblog:
            title += " [retruth]"
        if has_media:
            title += " [media]"

        body_lines = [
            text[:280],
            f"Full post: {url}" if url else "",
            f"Posted: {created_at}" if created_at else "",
        ]
        body = "\n".join(line for line in body_lines if line)
        return title, body

    @staticmethod
    def _format_polymarket(
        signal: Dict, payload: Dict, signal_type: str
    ) -> Tuple[str, str]:
        market_name = payload.get("market_name", payload.get("market", "Unknown market"))
        market_url = payload.get("market_url", "")

        if signal_type == "large_bet":
            amount = payload.get("amount_usd", 0)
            outcome = payload.get("outcome", "?")
            title = f"POLYMARKET — Large bet on {market_name}"
            body = (
                f"Type: Large bet\n"
                f"Detail: ${amount:,.0f} {outcome}\n"
                f"Market: {market_name}\n"
                f"{market_url}"
            )
        elif signal_type == "new_wallet":
            wallet_age = payload.get("wallet_age_days", "?")
            amount = payload.get("amount_usd", 0)
            outcome = payload.get("outcome", "?")
            title = f"POLYMARKET — New wallet bet on {market_name}"
            body = (
                f"Type: New wallet\n"
                f"Detail: {wallet_age}-day-old wallet bet ${amount:,.0f} {outcome}\n"
                f"Market: {market_name}\n"
                f"{market_url}"
            )
        elif signal_type == "odds_move":
            change = payload.get("change_pct", 0)
            direction = "up" if change > 0 else "down"
            title = f"POLYMARKET — Odds move {direction} on {market_name}"
            body = (
                f"Type: Odds move\n"
                f"Detail: {abs(change):.1f}pp {direction} in 5 min\n"
                f"Market: {market_name}\n"
                f"{market_url}"
            )
        elif signal_type == "volume_spike":
            multiplier = payload.get("multiplier", 0)
            title = f"POLYMARKET — Volume spike on {market_name}"
            body = (
                f"Type: Volume spike\n"
                f"Detail: {multiplier:.1f}x 24hr average\n"
                f"Market: {market_name}\n"
                f"{market_url}"
            )
        else:
            title = f"POLYMARKET SIGNAL — {market_name}"
            body = signal.get("summary", "Signal detected")

        return title, body

    @staticmethod
    def _format_futures(signal: Dict, payload: Dict) -> Tuple[str, str]:
        ticker = payload.get("ticker", "?")
        name = payload.get("name", ticker)
        current_vol = payload.get("current_volume", 0)
        avg_vol = payload.get("average_volume", 0)
        ratio = payload.get("ratio", 0)
        price = payload.get("price", 0)
        price_change_pct = payload.get("price_change_pct", 0)
        ts = signal.get("created_at", "")

        title = f"VOLUME SPIKE — {name} ({ticker})"
        body = (
            f"Current 1-min volume: {current_vol:,.0f} contracts\n"
            f"20-bar avg: {avg_vol:,.0f} contracts\n"
            f"Ratio: {ratio:.2f}x\n"
            f"Price: {price:.2f} · Change: {price_change_pct:+.2f}%\n"
            f"Time: {ts}"
        )
        return title, body

    @staticmethod
    def _format_correlated(signal: Dict, payload: Dict) -> Tuple[str, str]:
        sources = payload.get("sources", "multiple sources")
        window = payload.get("window_minutes", 10)
        title = "CORRELATED SIGNAL DETECTED"
        body = (
            f"Multiple sources fired within {window} minutes:\n"
            f"Sources: {sources}\n"
            f"{signal.get('summary', '')}"
        )
        return title, body


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """
    Simple in-memory rate limiter: one alert per source per window_minutes.
    CRITICAL priority is never rate-limited.
    """

    def __init__(self, window_minutes: int = 5):
        self._window_seconds = window_minutes * 60
        self._last_sent: Dict[str, float] = {}

    def is_rate_limited(self, source: str, priority: str) -> bool:
        """Return True if this source should be suppressed due to rate limiting."""
        if priority == "CRITICAL":
            return False
        last = self._last_sent.get(source)
        if last is None:
            return False
        elapsed = time_mod.time() - last
        return elapsed < self._window_seconds

    def record_sent(self, source: str) -> None:
        """Record that an alert was sent for the given source."""
        self._last_sent[source] = time_mod.time()


# ---------------------------------------------------------------------------
# Alerter
# ---------------------------------------------------------------------------

class Alerter:
    """
    Polls SQLite for unalerted signals and dispatches push notifications.

    Usage:
        alerter = Alerter(config, db)
        alerter.run()  # blocks forever
    """

    def __init__(self, config, db):
        self.config = config
        self.db = db
        self._rate_limiter = RateLimiter(
            window_minutes=config.alerts.rate_limit_minutes
        )
        self._session = requests.Session()
        self._last_digest_date: Optional[str] = None

    # ------------------------------------------------------------------
    # Quiet hours
    # ------------------------------------------------------------------

    def is_suppressed_by_quiet_hours(
        self, priority: str, now_time: Optional[time] = None
    ) -> bool:
        """
        Return True if the signal should be suppressed during quiet hours.

        Signals with priority >= quiet_suppress_below are never suppressed.
        CRITICAL is never suppressed regardless of quiet hours.
        """
        if priority == "CRITICAL":
            return False
        quiet_cfg = self.config.alerts.quiet_hours_utc
        suppress_below = self.config.alerts.quiet_suppress_below
        # If priority >= suppress threshold, do not suppress
        if _priority_index(priority) >= _priority_index(suppress_below):
            return False
        # Check if we're in quiet hours
        check_time = now_time or datetime.now(timezone.utc).time()
        return is_in_window(check_time, quiet_cfg.start, quiet_cfg.end)

    # ------------------------------------------------------------------
    # ntfy dispatch
    # ------------------------------------------------------------------

    def send_ntfy(
        self,
        title: str,
        body: str,
        priority: str,
        tags: str,
    ) -> bool:
        """
        Send a push notification via ntfy. Returns True on success.
        """
        url = f"{self.config.alerts.ntfy_url}/{self.config.alerts.ntfy_topic}"
        headers = {
            "Priority": priority,
            "Tags": tags,
            "Title": title,
            "Content-Type": "text/plain; charset=utf-8",
        }
        try:
            resp = self._session.post(url, data=body.encode("utf-8"), headers=headers, timeout=10)
            if resp.status_code < 300:
                logger.debug("ntfy sent OK: %s", title)
                return True
            logger.warning("ntfy returned HTTP %d for %r", resp.status_code, title)
            return False
        except Exception as exc:
            logger.error("ntfy send failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Dispatch pipeline
    # ------------------------------------------------------------------

    def dispatch_signal(
        self,
        signal: Dict[str, Any],
        now_utc: Optional[time] = None,
    ) -> bool:
        """
        Attempt to dispatch a single signal. Returns True if sent.

        Applies rate limiting and quiet hours before sending.
        Marks the signal as alerted regardless of send outcome to prevent
        repeated dispatch attempts on transient failures (caller responsibility
        to retry if needed — signals that fail to send are logged).
        """
        source = signal["source"]
        priority = signal["priority"]
        signal_id = signal["id"]

        # Rate limit check (except CRITICAL)
        if self._rate_limiter.is_rate_limited(source, priority):
            logger.info(
                "Signal %d from %s suppressed (rate limited, priority=%s)",
                signal_id, source, priority,
            )
            return False

        # Quiet hours check
        if self.is_suppressed_by_quiet_hours(priority, now_utc):
            logger.info(
                "Signal %d suppressed by quiet hours (priority=%s)", signal_id, priority
            )
            return False

        title, body = AlertFormatter.format_signal(signal)
        ntfy_priority = _priority_to_ntfy_priority(priority)
        tags = _NTFY_TAGS_MAP.get(priority, "bell")

        success = self.send_ntfy(title=title, body=body, priority=ntfy_priority, tags=tags)
        if success:
            self.db.mark_alerted(signal_id)
            self._rate_limiter.record_sent(source)
            logger.info("Dispatched signal %d (%s/%s)", signal_id, source, priority)
        else:
            logger.error("Failed to send signal %d — will retry on next poll", signal_id)
        return success

    # ------------------------------------------------------------------
    # Daily digest
    # ------------------------------------------------------------------

    def send_daily_digest(self, since_hours: int = 24) -> bool:
        """
        Send a daily digest summarising signals from the last `since_hours` hours.
        """
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        since = (now - timedelta(hours=since_hours)).isoformat()
        rows = self.db.execute_fetchall(
            "SELECT source, priority, COUNT(*) as cnt FROM signals "
            "WHERE created_at >= ? GROUP BY source, priority ORDER BY priority DESC",
            (since,),
        )
        total = sum(r["cnt"] for r in rows)
        title = f"Sentinel Daily Digest — {total} signal{'s' if total != 1 else ''} in last {since_hours}h"
        if rows:
            lines = [f"  {r['source']} / {r['priority']}: {r['cnt']}" for r in rows]
            body = "Signals by source:\n" + "\n".join(lines)
        else:
            body = "No signals in the last 24 hours. All quiet."
        return self.send_ntfy(title=title, body=body, priority="1", tags="calendar")

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    def poll_once(self) -> int:
        """
        Process all unalerted signals. Returns count of signals dispatched.
        """
        signals = self.db.get_unalerted_signals()
        dispatched = 0
        now_time = datetime.now(timezone.utc).time()
        for signal in signals:
            result = self.dispatch_signal(signal, now_utc=now_time)
            if result:
                dispatched += 1
        return dispatched

    def _should_send_digest(self) -> bool:
        """Return True if it's time to send the daily digest."""
        now = datetime.now(timezone.utc)
        digest_time = self.config.alerts.digest_time_utc
        today_str = now.strftime("%Y-%m-%d")
        if (now.hour == digest_time.hour
                and now.minute == digest_time.minute
                and self._last_digest_date != today_str):
            self._last_digest_date = today_str
            return True
        return False

    def run(self) -> None:
        """
        Main alerter loop. Polls every 2 seconds. Blocks forever.
        """
        logger.info("Alerter starting up")
        while True:
            try:
                if self._should_send_digest():
                    logger.info("Sending daily digest")
                    self.send_daily_digest()
                self.poll_once()
            except Exception as exc:
                logger.error("Alerter loop error: %s", exc)
            time_mod.sleep(POLL_INTERVAL_SECONDS)
