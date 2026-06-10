"""
collectors/kalshi.py — Kalshi prediction market collector.

Polls the Kalshi public REST API for unusual trading activity in configured
event markets. Writes signals directly to SQLite.

Signal types generated:
  - large_bet:     Single trade > configured contract threshold
  - odds_move:     Price shift >= configured percentage points since last poll
  - volume_spike:  24hr volume exceeds baseline by configured multiplier

Kalshi API docs: https://docs.kalshi.com
Base URL: https://external-api.kalshi.com/trade-api/v2

The public read-only endpoints (markets, events, trades) do not require
authentication. Only order placement and portfolio queries require API keys.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KALSHI_API_BASE = "https://external-api.kalshi.com/trade-api/v2"

STATE_KEY_LAST_TRADE = "kalshi_last_trade_{ticker}"
STATE_KEY_PREV_PRICE = "kalshi_prev_price_{ticker}"
STATE_KEY_VOLUME_SPIKE = "kalshi_vol_spike_{ticker}"

DEFAULT_BACKOFF = [30, 60, 120, 300]


# ---------------------------------------------------------------------------
# Signal detection helpers (pure functions, easily testable)
# ---------------------------------------------------------------------------

def _is_large_bet(count_fp: float, threshold: float) -> bool:
    """Return True if the trade contract count exceeds the threshold."""
    return count_fp >= threshold


def _is_odds_move(
    previous: Optional[float],
    current: float,
    threshold_pct: float,
) -> bool:
    """Return True if price has moved by >= threshold_pct percentage points."""
    if previous is None:
        return False
    # Kalshi prices are in dollars (0.00–1.00), so multiply by 100 for pp
    change_pp = abs(current - previous) * 100
    return change_pp >= threshold_pct


def _calculate_volume_spike(
    current_volume: float,
    baseline_volume: float,
    multiplier: float,
    min_absolute: float,
) -> Optional[Dict[str, float]]:
    """
    Return spike info dict if current_volume exceeds baseline by multiplier
    and meets the absolute minimum threshold. Returns None if no spike.
    """
    if baseline_volume <= 0:
        return None
    if current_volume < min_absolute:
        return None
    ratio = current_volume / baseline_volume
    if ratio >= multiplier:
        return {"ratio": ratio, "current": current_volume, "baseline": baseline_volume}
    return None


# ---------------------------------------------------------------------------
# Collector class
# ---------------------------------------------------------------------------

class KalshiCollector:
    """
    Synchronous Kalshi polling collector.

    Polls the Kalshi public API for each configured event ticker and
    generates signals for unusual activity.
    """

    def __init__(self, config, db):
        self.config = config
        self.db = db
        k_cfg = config.kalshi
        self._poll_interval = k_cfg.poll_interval_seconds
        self._tracked_events = list(k_cfg.tracked_event_tickers)
        self._api_base = k_cfg.api_base_url
        self._thresholds = k_cfg.thresholds
        self._client = httpx.Client(timeout=15.0, follow_redirects=True)
        self._consecutive_errors = 0

    # ------------------------------------------------------------------
    # API fetching
    # ------------------------------------------------------------------

    def fetch_event_markets(self, event_ticker: str) -> List[Dict[str, Any]]:
        """
        Fetch all open markets for a given event ticker.
        Returns empty list on error.
        """
        url = f"{self._api_base}/markets"
        try:
            resp = self._client.get(
                url, params={"event_ticker": event_ticker, "status": "open", "limit": 100}
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("markets", [])
            logger.warning("Kalshi markets API returned HTTP %d for event %r",
                           resp.status_code, event_ticker)
        except Exception as exc:
            logger.error("Failed to fetch Kalshi markets for %r: %s", event_ticker, exc)
        return []

    def fetch_recent_trades(
        self, ticker: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Fetch recent trades for a specific market ticker.
        Returns empty list on error.
        """
        url = f"{self._api_base}/markets/trades"
        try:
            resp = self._client.get(
                url, params={"ticker": ticker, "limit": limit}
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("trades", [])
            logger.warning("Kalshi trades API returned HTTP %d for %r",
                           resp.status_code, ticker)
        except Exception as exc:
            logger.error("Failed to fetch Kalshi trades for %r: %s", ticker, exc)
        return []

    # ------------------------------------------------------------------
    # State tracking
    # ------------------------------------------------------------------

    def get_last_trade_id(self, ticker: str) -> Optional[str]:
        key = STATE_KEY_LAST_TRADE.format(ticker=ticker)
        return self.db.state.get(key)

    def set_last_trade_id(self, ticker: str, trade_id: str) -> None:
        key = STATE_KEY_LAST_TRADE.format(ticker=ticker)
        self.db.state.set(key, trade_id)

    def get_previous_price(self, ticker: str) -> Optional[float]:
        key = STATE_KEY_PREV_PRICE.format(ticker=ticker)
        val = self.db.state.get(key)
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def set_previous_price(self, ticker: str, price: float) -> None:
        key = STATE_KEY_PREV_PRICE.format(ticker=ticker)
        self.db.state.set(key, str(price))

    # ------------------------------------------------------------------
    # Signal processing
    # ------------------------------------------------------------------

    def _process_trades(
        self,
        market: Dict[str, Any],
        trades: List[Dict[str, Any]],
    ) -> None:
        """Analyse trades and write signals to DB as appropriate."""
        ticker = market.get("ticker", "unknown")
        market_title = market.get("title", ticker)
        last_trade_id = self.get_last_trade_id(ticker)

        # Process only new trades (ID-based deduplication)
        new_trades = []
        for trade in trades:
            trade_id = trade.get("trade_id", "")
            if trade_id == last_trade_id:
                break
            new_trades.append(trade)

        for trade in reversed(new_trades):  # process oldest first
            trade_id = trade.get("trade_id", "")
            try:
                count_fp = float(trade.get("count_fp", 0))
            except (ValueError, TypeError):
                count_fp = 0.0

            try:
                yes_price = float(trade.get("yes_price_dollars", 0))
            except (ValueError, TypeError):
                yes_price = 0.0

            taker_side = trade.get("taker_side", "?")

            # --- Large bet signal ---
            if _is_large_bet(count_fp, self._thresholds.large_bet_contracts):
                logger.info("Large bet detected: %.0f contracts on %s", count_fp, ticker)
                self.db.insert_signal(
                    source="kalshi",
                    signal_type="large_bet",
                    priority="HIGH",
                    payload={
                        "trade_id": trade_id,
                        "contracts": count_fp,
                        "yes_price": yes_price,
                        "taker_side": taker_side,
                        "ticker": ticker,
                        "market_title": market_title,
                    },
                    summary=f"Large bet {count_fp:,.0f} contracts ({taker_side}) on {market_title}",
                )

            self.set_last_trade_id(ticker, trade_id)

    def _check_odds_move(self, market: Dict[str, Any]) -> None:
        """Check if YES price has moved significantly since last poll."""
        ticker = market.get("ticker", "unknown")
        market_title = market.get("title", ticker)

        try:
            current_yes = float(market.get("last_price_dollars", 0))
        except (ValueError, TypeError):
            return

        if current_yes <= 0:
            return

        previous_yes = self.get_previous_price(ticker)
        if _is_odds_move(previous_yes, current_yes, self._thresholds.odds_move_pct_5min):
            change_pct = (current_yes - previous_yes) * 100
            logger.info("Odds move detected on %s: %.1f%% -> %.1f%%",
                        ticker, previous_yes * 100, current_yes * 100)
            self.db.insert_signal(
                source="kalshi",
                signal_type="odds_move",
                priority="MEDIUM",
                payload={
                    "ticker": ticker,
                    "market_title": market_title,
                    "previous_yes": previous_yes,
                    "current_yes": current_yes,
                    "change_pct": change_pct,
                },
                summary=(
                    f"Odds move {change_pct:+.1f}pp on {market_title} "
                    f"(YES: {previous_yes*100:.0f}% -> {current_yes*100:.0f}%)"
                ),
            )
        self.set_previous_price(ticker, current_yes)

    def _check_volume_spike(self, market: Dict[str, Any]) -> None:
        """Check for unusual volume vs. 24hr baseline.

        Deduplication: only fires once per ticker per spike. A new signal
        is emitted only if (a) the ratio has doubled since the last alert,
        or (b) 6+ hours have passed since the last alert for this ticker.
        """
        ticker = market.get("ticker", "unknown")
        market_title = market.get("title", ticker)

        try:
            volume_24h = float(market.get("volume_24h_fp", 0))
            volume_total = float(market.get("volume_fp", 0))
        except (ValueError, TypeError):
            return

        if volume_total <= 0:
            return

        # Use lifetime daily average as baseline
        try:
            created = market.get("created_time", "")
            if created:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age_days = max((datetime.now(timezone.utc) - created_dt).days, 1)
                daily_avg = volume_total / age_days
            else:
                return
        except (ValueError, TypeError):
            return

        spike = _calculate_volume_spike(
            current_volume=volume_24h,
            baseline_volume=daily_avg,
            multiplier=self._thresholds.volume_spike_multiplier,
            min_absolute=self._thresholds.min_absolute_volume,
        )
        if spike:
            # Dedup: check if we already fired for this ticker recently
            state_key = STATE_KEY_VOLUME_SPIKE.format(ticker=ticker)
            prev = self.db.state.get(state_key)
            now_ts = time.time()
            if prev:
                try:
                    parts = prev.split("|")
                    prev_ratio = float(parts[0])
                    prev_ts = float(parts[1])
                    hours_elapsed = (now_ts - prev_ts) / 3600
                    # Only re-fire if ratio doubled or 6+ hours passed
                    if spike["ratio"] < prev_ratio * 2 and hours_elapsed < 6:
                        return
                except (ValueError, IndexError):
                    pass  # corrupted state, re-fire

            logger.info("Volume spike on %s: %.1fx baseline", ticker, spike["ratio"])
            self.db.insert_signal(
                source="kalshi",
                signal_type="volume_spike",
                priority="MEDIUM",
                payload={
                    "ticker": ticker,
                    "market_title": market_title,
                    "volume_24h": volume_24h,
                    "daily_avg": daily_avg,
                    "ratio": spike["ratio"],
                },
                summary=(
                    f"Volume spike {spike['ratio']:.1f}x on {market_title} "
                    f"(24h: {volume_24h:,.0f}, avg: {daily_avg:,.0f})"
                ),
            )
            self.db.state.set(state_key, f"{spike['ratio']}|{now_ts}")

    def process_market(self, market: Dict[str, Any]) -> None:
        """Process a single market: check trades, odds, and volume."""
        ticker = market.get("ticker", "")
        status = market.get("status", "")

        if status != "active":
            logger.debug("Market %r status=%s — skipping", ticker, status)
            return

        trades = self.fetch_recent_trades(ticker)
        if trades:
            self._process_trades(market, trades)

        self._check_odds_move(market)
        self._check_volume_spike(market)

    # ------------------------------------------------------------------
    # Main polling loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main polling loop. Blocks forever."""
        logger.info("KalshiCollector starting up — tracking %d events", len(self._tracked_events))
        error_attempt = 0

        while True:
            try:
                for event_ticker in self._tracked_events:
                    markets = self.fetch_event_markets(event_ticker)
                    for market in markets:
                        self.process_market(market)
                    if not markets:
                        logger.warning("No open markets for event %r", event_ticker)
                error_attempt = 0
            except Exception as exc:
                error_attempt += 1
                delay = DEFAULT_BACKOFF[min(error_attempt - 1, len(DEFAULT_BACKOFF) - 1)]
                logger.error(
                    "Kalshi poll error (attempt %d): %s — retrying in %ds",
                    error_attempt, exc, delay,
                )
                time.sleep(delay)
                continue

            time.sleep(self._poll_interval)
