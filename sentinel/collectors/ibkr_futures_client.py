"""
collectors/ibkr_futures_client.py — Interactive Brokers historical-bars
fetcher for futures instruments, used ahead of yfinance's ~10min-delayed
1-min bars (see TODO.md's "Interactive Brokers as a futures price source").

Connects to IB Gateway per-call (no persistent connection — matches this
project's synchronous poll-loop collector architecture, and the sibling
`rotrade` project's own proven IB-with-Yahoo-fallback pattern in
scripts/ibkr_historical.py). Never raises: any connection failure, timeout,
API error, or unmapped ticker returns an empty list / None, so callers fall
through to their existing yfinance fallback exactly as if IB simply weren't
configured.

Requires IB Gateway running and logged in — a manual daily step (no
auto-relogin infrastructure exists here or in rotrade). Config's
`futures.ib_enabled` defaults to False, so this module is never touched
unless explicitly opted into.

Uses CONTFUT (continuous front-month) contracts — always resolves to
whichever contract is currently active, no expiry-month management needed
for live polling. Contract specs (symbol, exchange) below were verified
live against IB Gateway before shipping this.
"""

import logging
import threading
from datetime import UTC, datetime
from typing import Any

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper

logger = logging.getLogger(__name__)

# yfinance-style ticker -> (IB symbol, IB exchange).
IB_CONTRACT_MAP: dict[str, tuple[str, str]] = {
    "CL=F": ("CL", "NYMEX"),
    "BZ=F": ("BZ", "NYMEX"),
    "NG=F": ("NG", "NYMEX"),
    "GC=F": ("GC", "COMEX"),
    "ES=F": ("ES", "CME"),
    "DX-Y.NYB": ("DX", "NYBOT"),
}

# Informational connection/data-farm status codes IB sends on every
# successful connection — not failures.
_INFO_ERROR_CODES = {2103, 2104, 2105, 2106, 2107, 2119, 2158}


def _contfut_contract(symbol: str, exchange: str) -> Contract:
    """Continuous front-month futures contract."""
    c = Contract()
    c.symbol = symbol
    c.secType = "CONTFUT"
    c.exchange = exchange
    c.currency = "USD"
    return c


class _HistoricalBarsClient(EWrapper, EClient):
    def __init__(self, contract: Contract, duration: str, bar_size: str) -> None:
        EClient.__init__(self, self)
        self.contract = contract
        self.duration = duration
        self.bar_size = bar_size
        self.done = threading.Event()
        self.bars: list[dict[str, Any]] = []
        self.error_detail: str | None = None

    def error(self, reqId, errorTime, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode in _INFO_ERROR_CODES:
            return
        self.error_detail = f"[{errorCode}] {errorString}"
        self.done.set()

    def historicalData(self, reqId, bar) -> None:
        try:
            timestamp = datetime.fromtimestamp(int(bar.date), tz=UTC).isoformat()
        except (ValueError, TypeError):
            timestamp = str(bar.date)
        self.bars.append({
            "volume": float(bar.volume),
            "close": float(bar.close),
            "open": float(bar.open),
            "timestamp": timestamp,
        })

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:
        self.done.set()

    def nextValidId(self, orderId: int) -> None:
        self.reqHistoricalData(
            1, self.contract, "", self.duration, self.bar_size,
            "TRADES", 0, 2, False, [],
        )


def fetch_bars(
    ticker: str,
    host: str,
    port: int,
    client_id: int,
    duration: str = "3600 S",
    bar_size: str = "1 min",
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Fetch recent 1-min bars for a yfinance-style futures ticker via IB
    Gateway. Returns [] on any failure — unmapped ticker, connection
    refused, timeout, or an IB API error — never raises, so callers can
    treat this exactly like the existing yfinance/Alpaca fetchers and fall
    through to their own fallback on an empty result."""
    mapping = IB_CONTRACT_MAP.get(ticker)
    if mapping is None:
        logger.warning("No IB contract mapping for ticker %r", ticker)
        return []
    symbol, exchange = mapping
    client = _HistoricalBarsClient(_contfut_contract(symbol, exchange), duration, bar_size)

    try:
        client.connect(host, port, client_id)
        thread = threading.Thread(target=client.run, daemon=True)
        thread.start()
        finished = client.done.wait(timeout=timeout)
    except Exception as exc:
        logger.warning("IB Gateway fetch failed for %s: %s", ticker, exc)
        return []
    finally:
        client.disconnect()

    if not finished:
        logger.warning("IB Gateway timed out fetching %s", ticker)
        return []
    if client.error_detail is not None:
        logger.warning("IB Gateway error fetching %s: %s", ticker, client.error_detail)
        return []
    return client.bars


def fetch_latest_price(ticker: str, host: str, port: int, client_id: int) -> float | None:
    """Latest close price for a ticker, or None (Gateway down, no data,
    unmapped ticker) — thin wrapper over fetch_bars() for
    price_followup.py's single-price backfill use case."""
    bars = fetch_bars(ticker, host, port, client_id, duration="900 S")
    if not bars:
        return None
    return bars[-1]["close"]
