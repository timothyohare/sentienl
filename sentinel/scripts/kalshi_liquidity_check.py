#!/usr/bin/env python3
"""
scripts/kalshi_liquidity_check.py — Weekly liquidity health-check for
tracked Kalshi event tickers.

The Kalshi collector only detects activity on the event tickers listed in
config.yaml's kalshi.tracked_event_tickers. A ticker whose underlying
markets go quiet wastes a tracking slot without producing signals — this is
exactly what happened to KXDEBTGROWTH-28DEC31 and KXGOVTCUTS-28 (found to
have 24h volume of ~11 and ~74 respectively on 2026-08-09, replaced with
more liquid macro tickers). This script re-checks each tracked ticker
against Kalshi's public API and flags any that have gone quiet or lost
their open markets entirely, so a human can swap them out before they sit
dead for weeks.

Run weekly via cron (adjust paths):
  0 9 * * 1 /home/timohare/dev/newdev/Sentinel/venv/bin/python \
    /home/timohare/dev/newdev/Sentinel/sentinel/scripts/kalshi_liquidity_check.py \
    --config /home/timohare/dev/newdev/Sentinel/config.yaml --notify

Exit codes:
  0 — all tracked tickers meet the minimum liquidity bar
  1 — one or more tracked tickers are quiet, dead, or have no open markets
"""

import argparse
import logging
import os
import sys
from typing import Any, Protocol

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sentinel.collectors.kalshi import KALSHI_API_BASE
from sentinel.core.config import Config, load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("sentinel.kalshi_liquidity_check")

DEFAULT_MIN_24H_VOLUME = 100.0


# ---------------------------------------------------------------------------
# Pure helpers (no network — directly testable)
# ---------------------------------------------------------------------------

def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def summarize_markets(markets: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate lifetime/24h volume across one event ticker's open markets."""
    return {
        "market_count": len(markets),
        "volume_total": sum(_to_float(m.get("volume_fp")) for m in markets),
        "volume_24h": sum(_to_float(m.get("volume_24h_fp")) for m in markets),
    }


def classify(summary: dict[str, Any], min_24h_volume: float) -> str:
    """Return 'OK', 'QUIET', or 'NO_MARKETS' for a ticker's liquidity summary."""
    if summary["market_count"] == 0:
        return "NO_MARKETS"
    if summary["volume_24h"] < min_24h_volume:
        return "QUIET"
    return "OK"


def check_tickers(
    tickers: list[str],
    fetcher: "MarketsFetcherProtocol",
    min_24h_volume: float,
) -> dict[str, dict[str, Any]]:
    """Fetch + classify each tracked ticker. Takes a fetcher so tests can fake the network."""
    results: dict[str, dict[str, Any]] = {}
    for ticker in tickers:
        summary = summarize_markets(fetcher.fetch_markets(ticker))
        summary["status"] = classify(summary, min_24h_volume)
        results[ticker] = summary
    return results


# ---------------------------------------------------------------------------
# Live market fetcher
# ---------------------------------------------------------------------------

class MarketsFetcherProtocol(Protocol):
    def fetch_markets(self, event_ticker: str) -> list[dict[str, Any]]: ...


class KalshiMarketsFetcher:
    """Fetches open markets for one event ticker from the public Kalshi API."""

    def __init__(self, api_base: str = KALSHI_API_BASE):
        self._client = httpx.Client(timeout=15.0, follow_redirects=True)
        self._api_base = api_base

    def fetch_markets(self, event_ticker: str) -> list[dict[str, Any]]:
        try:
            resp = self._client.get(
                f"{self._api_base}/markets",
                params={"event_ticker": event_ticker, "status": "open", "limit": 100},
            )
            if resp.status_code != 200:
                logger.warning("Kalshi API returned HTTP %d for event %r",
                               resp.status_code, event_ticker)
                return []
            return resp.json().get("markets", [])
        except Exception as exc:
            logger.error("Failed to fetch Kalshi markets for %r: %s", event_ticker, exc)
            return []


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def send_notification(config: Config, flagged: dict[str, dict[str, Any]]) -> None:
    """Send an ntfy alert listing the tickers that failed the liquidity bar."""
    try:
        import requests
        lines = [
            f"{ticker}: {summary['status']} (24h vol={summary['volume_24h']:.0f})"
            for ticker, summary in flagged.items()
        ]
        message = "Kalshi tracked tickers going quiet:\n" + "\n".join(lines)
        url = f"{config.alerts.ntfy_url}/{config.alerts.ntfy_topic}"
        requests.post(
            url,
            data=message.encode("utf-8"),
            headers={
                "Priority": "3",
                "Tags": "warning",
                "Title": "Sentinel — Kalshi ticker liquidity check",
                "Content-Type": "text/plain",
            },
            timeout=10,
        )
    except Exception as exc:
        logger.error("Liquidity notification send failed: %s", exc)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Kalshi tracked-ticker liquidity check")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--min-24h-volume", type=float, default=DEFAULT_MIN_24H_VOLUME,
                        help="24h volume below this is flagged as QUIET")
    parser.add_argument("--notify", action="store_true",
                        help="Send an ntfy notification if any ticker is flagged")
    args = parser.parse_args()

    config = load_config(args.config)
    fetcher = KalshiMarketsFetcher(api_base=config.kalshi.api_base_url)
    results = check_tickers(config.kalshi.tracked_event_tickers, fetcher, args.min_24h_volume)

    flagged = {}
    for ticker, summary in results.items():
        logger.info(
            "[%s] %s — markets=%d vol_24h=%.0f vol_total=%.0f",
            summary["status"], ticker, summary["market_count"],
            summary["volume_24h"], summary["volume_total"],
        )
        if summary["status"] != "OK":
            flagged[ticker] = summary

    if flagged:
        logger.warning("Quiet/dead tracked tickers: %s", list(flagged))
        if args.notify:
            send_notification(config, flagged)
        sys.exit(1)

    logger.info("All tracked tickers meet the liquidity bar (>= %.0f 24h volume)",
                args.min_24h_volume)
    sys.exit(0)


if __name__ == "__main__":
    main()
