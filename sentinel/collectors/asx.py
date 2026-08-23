"""
collectors/asx.py — ASX equity volume/price-move collector.

Polls 1-minute OHLCV bars for a configured watchlist of ASX-listed equities
and generates HIGH/MEDIUM signals on two conditions:
  - volume_spike: current bar volume exceeds the rolling average by a
    configured multiplier (identical algorithm to futures_volume.py — the
    rolling-average/spike helpers are imported from there rather than
    duplicated, since the logic is genuinely the same, not just similar).
  - price_move: bar close has moved >= a configured percentage since the
    previously processed bar's close (mirrors kalshi.py's odds_move, adapted
    to a bar close rather than a poll-to-poll price read).

Data source:
  - yfinance is the default/fallback source (1-min bars, ASX ticker suffix
    ".AX", ~20 min delay).
  - IB Gateway (`ibkr_asx_client.py`) is used ahead of yfinance when
    `asx.ib_enabled` is true. Unlike futures' IB path, ASX market-data
    entitlement on the IB account has not been confirmed — leave
    `ib_enabled` false until that's checked; yfinance works either way.

Instruments: a configurable large-cap watchlist (BHP.AX, CBA.AX, CSL.AX,
FMG.AX, WBC.AX, NAB.AX, RIO.AX, WES.AX, MQG.AX, WOW.AX by default) — general
market-movers, not tied to any one geopolitical theme, matching Sentinel's
general-purpose-alerting design.
"""

import logging
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sentinel.collectors.futures_volume import (
    _canonical_ts,
    _compute_rolling_average,
    _detect_volume_spike,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HISTORY_MAX_BARS = 100  # cap in-memory history (keeps memory bounded)
STATE_KEY_LAST_BAR = "asx_last_bar_{ticker}"
STATE_KEY_PREV_CLOSE = "asx_prev_close_{ticker}"
SOURCE = "asx"


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------

def _detect_price_move(
    previous_close: float | None,
    current_close: float,
    threshold_pct: float,
) -> float | None:
    """
    Return the signed percentage change from previous_close to
    current_close if its magnitude is >= threshold_pct. Returns None if
    there's no prior close to compare against, or the move doesn't clear
    the threshold.
    """
    if previous_close is None or previous_close <= 0:
        return None
    change_pct = (current_close - previous_close) / previous_close * 100
    if abs(change_pct) >= threshold_pct:
        return change_pct
    return None


# ---------------------------------------------------------------------------
# Collector class
# ---------------------------------------------------------------------------

class AsxCollector:
    """
    Synchronous ASX equity volume/price-move collector.

    Fetches 1-minute bars for each configured instrument, maintains a
    rolling volume history and last-seen close price, and fires signals
    when unusual volume or a significant price move is detected.
    """

    def __init__(self, config, db):
        self.config = config
        self.db = db
        asx_cfg = config.asx
        self._poll_interval = asx_cfg.poll_interval_seconds
        self._instruments = asx_cfg.instruments
        self._thresholds = asx_cfg.thresholds
        self._active_window = asx_cfg.active_window_utc
        self._ib_enabled = asx_cfg.ib_enabled
        self._ib_host = asx_cfg.ib_host
        self._ib_port = asx_cfg.ib_port
        self._ib_client_id_base = asx_cfg.ib_client_id_base
        # In-memory volume history keyed by ticker
        self._volume_history: dict[str, list[float | None]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Window helpers
    # ------------------------------------------------------------------

    def is_in_active_window(self, now_time) -> bool:
        """Return True if now_time falls within the configured active UTC window."""
        from sentinel.core.config import is_in_window
        return is_in_window(now_time, self._active_window.start, self._active_window.end)

    def get_spike_multiplier(self, now_time) -> float:
        """Return the appropriate spike multiplier for the current time."""
        if self.is_in_active_window(now_time):
            return self._thresholds.spike_multiplier
        return self._thresholds.spike_multiplier_quiet

    # ------------------------------------------------------------------
    # Volume history
    # ------------------------------------------------------------------

    def add_volume_observation(self, ticker: str, volume: float | None) -> None:
        """Add a volume observation to the rolling history for a ticker."""
        history = self._volume_history[ticker]
        history.append(volume)
        # Keep memory bounded
        if len(history) > HISTORY_MAX_BARS:
            del history[0]

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def _fetch_yfinance(self, ticker: str) -> list[dict[str, Any]]:
        """
        Fetch 1-minute bars from Yahoo Finance via yfinance.
        Returns a list of bar dicts with keys: volume, close, open.
        """
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            df = t.history(period="1d", interval="1m")
            if df is None or df.empty:
                logger.warning("yfinance returned empty data for %s", ticker)
                return []
            bars = []
            for idx, row in df.iterrows():
                bars.append({
                    "volume": row.get("Volume"),
                    "close": row.get("Close"),
                    "open": row.get("Open"),
                    "timestamp": idx.isoformat() if hasattr(idx, "isoformat") else str(idx),
                })
            return bars
        except Exception as exc:
            logger.error("yfinance fetch failed for %s: %s", ticker, exc)
            return []

    def _fetch_ibkr(self, ticker: str) -> list[dict[str, Any]]:
        """
        Fetch 1-minute bars from IB Gateway. Requires Gateway running and
        logged in, plus ASX market-data entitlement (unconfirmed — see
        ibkr_asx_client.py's module docstring). Returns empty list on any
        failure, same contract as _fetch_yfinance.
        """
        from sentinel.collectors.ibkr_asx_client import fetch_bars as ib_fetch_bars
        client_id = self._ib_client_id_base + hash(ticker) % 1000
        return ib_fetch_bars(ticker, self._ib_host, self._ib_port, client_id)

    def fetch_bars(self, ticker: str) -> list[dict[str, Any]]:
        """
        Fetch 1-minute bars for a ticker. Tries IB Gateway first (if
        enabled), falling back to yfinance.
        """
        if self._ib_enabled:
            bars = self._fetch_ibkr(ticker)
            if bars:
                return bars
            logger.info("IB Gateway returned no data for %s — falling back to yfinance", ticker)
        return self._fetch_yfinance(ticker)

    # ------------------------------------------------------------------
    # Signal processing
    # ------------------------------------------------------------------

    def _maybe_track_price(self, signal_id: int, priority: str, ticker: str, price: float) -> None:
        """Snapshot price_t0 for HIGH/CRITICAL signals so price_followup.py
        can backfill the later horizons and measure real follow-through."""
        if priority in ("HIGH", "CRITICAL") and price > 0:
            self.db.price_tracking.insert(signal_id, SOURCE, ticker, price_t0=price)

    def process_instrument(
        self,
        instrument,
        latest_bar: dict[str, Any],
        now_time,
    ) -> None:
        """
        Process the latest bar for one instrument. May create a
        volume_spike signal, a price_move signal, both, or neither.
        """
        ticker = instrument.ticker
        name = instrument.name
        min_absolute = instrument.min_absolute_volume

        # Bar-level dedup: yfinance/IB hand us the latest bar every poll, but
        # the bar repeats for several polls until the next minute closes.
        # Process each bar timestamp exactly once, otherwise we re-pollute
        # the rolling average / re-compare the same close against itself.
        bar_ts = _canonical_ts(latest_bar.get("timestamp"))
        if bar_ts is not None:
            state_key = STATE_KEY_LAST_BAR.format(ticker=ticker)
            if self.db.state.get(state_key) == bar_ts:
                logger.debug("%s: bar %s already processed — skipping", ticker, bar_ts)
                return
            self.db.state.set(state_key, bar_ts)

        current_volume = latest_bar.get("volume")
        close_price = latest_bar.get("close", 0.0) or 0.0

        if current_volume is not None:
            self.add_volume_observation(ticker, current_volume)

        self._check_volume_spike(ticker, name, min_absolute, current_volume, close_price, now_time)
        if close_price > 0:
            self._check_price_move(ticker, name, close_price)

    def _check_volume_spike(
        self,
        ticker: str,
        name: str,
        min_absolute: int,
        current_volume: float | None,
        close_price: float,
        now_time,
    ) -> None:
        history_without_current = self._volume_history[ticker][:-1]
        rolling_avg = _compute_rolling_average(
            history_without_current, self._thresholds.rolling_bars
        )

        spike_multiplier = self.get_spike_multiplier(now_time)
        spike = _detect_volume_spike(
            current_volume=current_volume,
            rolling_avg=rolling_avg,
            spike_multiplier=spike_multiplier,
            min_absolute_volume=min_absolute,
        )
        if spike is None:
            return

        ratio = spike["ratio"]
        priority = "HIGH" if ratio >= self._thresholds.spike_multiplier_quiet else "MEDIUM"

        logger.info(
            "Volume spike %s: %.0f shares (%.2fx avg %.0f) price=%.2f",
            ticker, current_volume, ratio, rolling_avg, close_price,
        )
        signal_id = self.db.insert_signal(
            source=SOURCE,
            signal_type="volume_spike",
            priority=priority,
            payload={
                "ticker": ticker,
                "name": name,
                "current_volume": current_volume,
                "average_volume": rolling_avg,
                "ratio": round(ratio, 3),
                "price": close_price,
                "spike_multiplier_used": spike_multiplier,
                "in_active_window": self.is_in_active_window(now_time),
            },
            summary=(
                f"Volume spike {ticker}: {current_volume:,.0f} shares "
                f"({ratio:.2f}x avg {rolling_avg:,.0f})"
            ),
        )
        self._maybe_track_price(signal_id, priority, ticker, close_price)

    def _check_price_move(self, ticker: str, name: str, close_price: float) -> None:
        state_key = STATE_KEY_PREV_CLOSE.format(ticker=ticker)
        prev_raw = self.db.state.get(state_key)
        previous_close = None
        if prev_raw is not None:
            try:
                previous_close = float(prev_raw)
            except (ValueError, TypeError):
                previous_close = None

        change_pct = _detect_price_move(
            previous_close, close_price, self._thresholds.price_move_pct
        )
        if change_pct is not None:
            priority = (
                "HIGH" if abs(change_pct) >= self._thresholds.price_move_pct_high else "MEDIUM"
            )
            logger.info("Price move %s: %.2f%% -> %.2f (priority=%s)",
                        ticker, change_pct, close_price, priority)
            signal_id = self.db.insert_signal(
                source=SOURCE,
                signal_type="price_move",
                priority=priority,
                payload={
                    "ticker": ticker,
                    "name": name,
                    "previous_close": previous_close,
                    "current_close": close_price,
                    "change_pct": round(change_pct, 4),
                },
                summary=f"Price move {change_pct:+.2f}% on {name} (${close_price:.2f})",
            )
            self._maybe_track_price(signal_id, priority, ticker, close_price)

        self.db.state.set(state_key, str(close_price))

    # ------------------------------------------------------------------
    # Main polling loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main polling loop. Blocks forever."""
        logger.info("AsxCollector starting up")
        logger.info("Monitoring: %s", [i.ticker for i in self._instruments])
        error_attempt = 0

        while True:
            try:
                now = datetime.now(UTC)
                now_time = now.time()

                for instrument in self._instruments:
                    try:
                        bars = self.fetch_bars(instrument.ticker)
                        if not bars:
                            logger.warning("No bars returned for %s", instrument.ticker)
                            continue
                        latest_bar = bars[-1]
                        self.process_instrument(instrument, latest_bar, now_time)
                    except Exception as exc:
                        logger.error("Error processing %s: %s", instrument.ticker, exc)

                error_attempt = 0
            except Exception as exc:
                error_attempt += 1
                delay = [30, 60, 120, 300][min(error_attempt - 1, 3)]
                logger.error(
                    "Asx poll error (attempt %d): %s — retrying in %ds",
                    error_attempt, exc, delay,
                )
                time.sleep(delay)
                continue

            time.sleep(self._poll_interval)
