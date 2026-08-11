"""
collectors/futures_volume.py — Futures volume spike collector.

Polls 1-minute OHLCV data for configured futures instruments and generates
HIGH/MEDIUM signals when volume exceeds the rolling average by a configured
multiplier.

Data source:
  - yfinance is the default/fallback source (1-min bars, ~10 min delay for
    futures).
  - IB Gateway (`ibkr_futures_client.py`) is used ahead of yfinance when
    `futures.ib_enabled` is true — real-time 1-min bars, no delay. Requires
    Gateway running and logged in (manual daily step); silently falls back
    to yfinance whenever it isn't. See CLAUDE.md's Futures collector
    section for the operational caveat.
  - Alpaca does NOT support futures (its data API rejects futures symbols).
    The Alpaca config/code remains inert in this collector for a potential
    stock/ETF monitoring pivot — it is not used for futures.

Instruments: CL=F, BZ=F, NG=F, GC=F, ES=F, DX-Y.NYB

Roll date suppression: volume alerts are silenced on configured roll dates
to avoid false positives from front-month/second-month volume rotation.
"""

import logging
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HISTORY_MAX_BARS = 100  # cap in-memory history (keeps memory bounded)
STATE_KEY_LAST_BAR = "futures_last_bar_{ticker}"
SOURCE_MAP = {
    "CL=F": "futures_oil",
    "BZ=F": "futures_brent",
    "NG=F": "futures_natgas",
    "GC=F": "futures_gold",
    "ES=F": "futures_sp500",
    "DX-Y.NYB": "futures_dxy",
}


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------

def _canonical_ts(ts: Any) -> str | None:
    """
    Normalise a bar timestamp to a canonical UTC ISO-8601 string for dedup.

    Sources disagree on format — Alpaca emits `...Z`, yfinance emits `...+00:00`
    for the same instant — so the raw string can't be used as a dedup key across
    sources. Parse to an aware UTC datetime and re-serialise. Falls back to
    str(ts) if the value is missing or unparseable.
    """
    if ts is None:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return str(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _compute_rolling_average(volumes: list[float | None], bars: int) -> float:
    """
    Return the mean of the last `bars` non-None volumes.
    Returns 0.0 if the list is empty or contains only None values.
    """
    if not volumes:
        return 0.0
    valid = [v for v in volumes[-bars:] if v is not None]
    if not valid:
        return 0.0
    return sum(valid) / len(valid)


def _detect_volume_spike(
    current_volume: float | None,
    rolling_avg: float,
    spike_multiplier: float,
    min_absolute_volume: float,
) -> dict[str, float] | None:
    """
    Return a dict with spike info if the current volume is anomalous.

    Conditions that must ALL be met:
      - current_volume is not None
      - rolling_avg > 0
      - current_volume >= min_absolute_volume
      - current_volume / rolling_avg >= spike_multiplier

    Returns None if no spike detected.
    """
    if current_volume is None:
        return None
    if rolling_avg <= 0:
        return None
    if current_volume < min_absolute_volume:
        return None
    ratio = current_volume / rolling_avg
    if ratio >= spike_multiplier:
        return {"ratio": ratio, "current": current_volume, "average": rolling_avg}
    return None


def _is_roll_date(ticker: str, date_str: str, roll_dates: list) -> bool:
    """
    Return True if `date_str` (YYYY-MM-DD) is a configured roll date for `ticker`.
    """
    return any(rd.date == date_str and ticker in rd.tickers for rd in roll_dates)


# ---------------------------------------------------------------------------
# Collector class
# ---------------------------------------------------------------------------

class FuturesVolumeCollector:
    """
    Synchronous futures volume spike collector.

    Fetches 1-minute bars for each configured instrument, maintains a rolling
    volume history, and fires signals when unusual volume is detected.
    """

    def __init__(self, config, db):
        self.config = config
        self.db = db
        fut_cfg = config.futures
        self._poll_interval = fut_cfg.poll_interval_seconds
        self._instruments = fut_cfg.instruments
        self._thresholds = fut_cfg.thresholds
        self._active_window = fut_cfg.active_window_utc
        self._suppress_on_roll = fut_cfg.suppress_volume_alerts_on_roll_dates
        self._roll_dates = fut_cfg.roll_dates
        self._rolling_bars = fut_cfg.thresholds.rolling_bars
        self._alpaca_api_key = fut_cfg.alpaca_api_key
        self._alpaca_api_secret = fut_cfg.alpaca_api_secret
        self._alpaca_base_url = fut_cfg.alpaca_base_url
        self._ib_enabled = fut_cfg.ib_enabled
        self._ib_host = fut_cfg.ib_host
        self._ib_port = fut_cfg.ib_port
        self._ib_client_id_base = fut_cfg.ib_client_id_base
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

    def _fetch_alpaca(self, ticker: str) -> list[dict[str, Any]]:
        """
        Fetch 1-minute bars from Alpaca Markets.
        Returns a list of bar dicts with keys: volume, close, open.
        Returns empty list if Alpaca is unavailable or not configured.
        """
        if not self._alpaca_api_key:
            return []
        try:
            import httpx
            # Map Yahoo-style tickers to Alpaca symbols (Alpaca uses different symbols for futures)
            # Alpaca free tier may not have all futures — yfinance is the practical fallback
            url = f"{self._alpaca_base_url}/v2/stocks/{ticker}/bars"
            headers = {
                "APCA-API-KEY-ID": self._alpaca_api_key,
                "APCA-API-SECRET-KEY": self._alpaca_api_secret,
            }
            resp = httpx.get(
                url,
                headers=headers,
                params={"timeframe": "1Min", "limit": self._rolling_bars + 5},
                timeout=10.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                bars = data.get("bars", [])
                return [
                    {
                        "volume": float(b.get("v", 0)),
                        "close": float(b.get("c", 0)),
                        "open": float(b.get("o", 0)),
                        "timestamp": b.get("t"),
                    }
                    for b in bars
                ]
            logger.warning("Alpaca returned HTTP %d for %s", resp.status_code, ticker)
        except Exception as exc:
            logger.warning("Alpaca fetch failed for %s: %s", ticker, exc)
        return []

    def _fetch_yfinance(self, ticker: str) -> list[dict[str, Any]]:
        """
        Fetch 1-minute bars from Yahoo Finance via yfinance.
        Note: may have 10–20 min delay for futures data.
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
                # idx is the bar's timestamp (a pandas Timestamp); use it as the
                # dedup key so the same delayed bar isn't reprocessed each poll.
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
        Fetch 1-minute bars from IB Gateway (real-time, no ~10min delay).
        Requires Gateway running and logged in — see CLAUDE.md's Futures
        collector section. Returns empty list on any failure, same
        contract as _fetch_alpaca/_fetch_yfinance.
        """
        from sentinel.collectors.ibkr_futures_client import fetch_bars as ib_fetch_bars
        client_id = self._ib_client_id_base + hash(ticker) % 1000
        return ib_fetch_bars(ticker, self._ib_host, self._ib_port, client_id)

    def fetch_bars(self, ticker: str) -> list[dict[str, Any]]:
        """
        Fetch 1-minute bars for a ticker. Tries Alpaca first (dead in
        practice — Alpaca doesn't support futures), then IB Gateway (if
        enabled), falling back to yfinance last.
        """
        if self._alpaca_api_key:
            bars = self._fetch_alpaca(ticker)
            if bars:
                return bars
            logger.info("Alpaca returned no data for %s — falling back to yfinance", ticker)
        if self._ib_enabled:
            bars = self._fetch_ibkr(ticker)
            if bars:
                return bars
            logger.info("IB Gateway returned no data for %s — falling back to yfinance", ticker)
        return self._fetch_yfinance(ticker)

    # ------------------------------------------------------------------
    # Signal processing
    # ------------------------------------------------------------------

    def process_instrument(
        self,
        instrument,
        latest_bar: dict[str, Any],
        now_time,
        today_str: str,
    ) -> None:
        """
        Process the latest bar for one instrument.
        Creates a volume_spike signal if thresholds are exceeded.
        """
        ticker = instrument.ticker
        name = instrument.name
        min_absolute = instrument.min_absolute_volume

        # Suppress on roll dates
        if self._suppress_on_roll and _is_roll_date(ticker, today_str, self._roll_dates):
            logger.debug("Roll date suppression active for %s on %s", ticker, today_str)
            return

        # Bar-level dedup: yfinance/Alpaca hand us the latest bar every poll, but
        # the bar is only delayed ~10 min — so the same bar repeats for several
        # polls. Process each bar timestamp exactly once, otherwise we re-pollute
        # the rolling average and re-fire the same spike. Bars without a
        # timestamp can't be deduped and fall through to per-poll processing.
        bar_ts = _canonical_ts(latest_bar.get("timestamp"))
        if bar_ts is not None:
            state_key = STATE_KEY_LAST_BAR.format(ticker=ticker)
            if self.db.state.get(state_key) == bar_ts:
                logger.debug("%s: bar %s already processed — skipping", ticker, bar_ts)
                return
            self.db.state.set(state_key, bar_ts)

        current_volume = latest_bar.get("volume")
        close_price = latest_bar.get("close", 0.0) or 0.0
        open_price = latest_bar.get("open", close_price) or close_price

        if current_volume is not None:
            self.add_volume_observation(ticker, current_volume)

        # Compute rolling average from history (excluding the current bar)
        history_without_current = self._volume_history[ticker][:-1]
        rolling_avg = _compute_rolling_average(history_without_current, self._rolling_bars)

        spike_multiplier = self.get_spike_multiplier(now_time)
        spike = _detect_volume_spike(
            current_volume=current_volume,
            rolling_avg=rolling_avg,
            spike_multiplier=spike_multiplier,
            min_absolute_volume=min_absolute,
        )

        if spike is None:
            logger.debug("%s: volume=%.0f avg=%.0f — no spike",
                         ticker, current_volume or 0, rolling_avg)
            return

        # Determine priority based on ratio. Outside the active window,
        # detection itself already requires ratio >= spike_multiplier_quiet
        # (see get_spike_multiplier), so any spike fired there is HIGH by
        # construction — a below-quiet-threshold spike can only occur inside
        # the active window, where it's MEDIUM.
        ratio = spike["ratio"]
        priority = "HIGH" if ratio >= self._thresholds.spike_multiplier_quiet else "MEDIUM"

        # Calculate price change estimate
        price_change_pct = (
            ((close_price - open_price) / open_price * 100)
            if open_price > 0 else 0.0
        )

        source = SOURCE_MAP.get(ticker, "futures_oil")
        logger.info(
            "Volume spike %s: %.0f contracts (%.2fx avg %.0f) price=%.2f",
            ticker, current_volume, ratio, rolling_avg, close_price,
        )
        signal_id = self.db.insert_signal(
            source=source,
            signal_type="volume_spike",
            priority=priority,
            payload={
                "ticker": ticker,
                "name": name,
                "current_volume": current_volume,
                "average_volume": rolling_avg,
                "ratio": round(ratio, 3),
                "price": close_price,
                "price_change_pct": round(price_change_pct, 4),
                "spike_multiplier_used": spike_multiplier,
                "in_active_window": self.is_in_active_window(now_time),
            },
            summary=(
                f"Volume spike {ticker}: {current_volume:,.0f} contracts "
                f"({ratio:.2f}x avg {rolling_avg:,.0f})"
            ),
        )
        # Price follow-through tracking (plans/05-price-follow-through.md):
        # HIGH/CRITICAL only — price_followup.py backfills t15/t60/t240/t1440.
        if priority in ("HIGH", "CRITICAL") and close_price > 0:
            self.db.price_tracking.insert(signal_id, source, ticker, price_t0=close_price)

    # ------------------------------------------------------------------
    # Main polling loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main polling loop. Blocks forever."""
        logger.info("FuturesVolumeCollector starting up")
        logger.info("Monitoring: %s", [i.ticker for i in self._instruments])
        error_attempt = 0

        while True:
            try:
                now = datetime.now(UTC)
                now_time = now.time()
                today_str = now.strftime("%Y-%m-%d")

                for instrument in self._instruments:
                    try:
                        bars = self.fetch_bars(instrument.ticker)
                        if not bars:
                            logger.warning("No bars returned for %s", instrument.ticker)
                            continue
                        latest_bar = bars[-1]
                        self.process_instrument(instrument, latest_bar, now_time, today_str)
                    except Exception as exc:
                        logger.error("Error processing %s: %s", instrument.ticker, exc)

                error_attempt = 0
            except Exception as exc:
                error_attempt += 1
                delay = [30, 60, 120, 300][min(error_attempt - 1, 3)]
                logger.error(
                    "FuturesVolume poll error (attempt %d): %s — retrying in %ds",
                    error_attempt, exc, delay,
                )
                time.sleep(delay)
                continue

            time.sleep(self._poll_interval)
