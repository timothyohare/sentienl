"""
collectors/ibkr_asx_client.py — Interactive Brokers historical-bars fetcher
for ASX-listed equities, mirroring ibkr_futures_client.py's pattern for
futures.

Connects to IB Gateway per-call (no persistent connection — matches this
project's synchronous poll-loop collector architecture). Never raises: any
connection failure, timeout, API error, or unmapped ticker returns an empty
list / None, so callers fall through to their existing yfinance fallback
exactly as if IB simply weren't configured.

Requires IB Gateway running and logged in — a manual daily step (no
auto-relogin infrastructure exists here or in rotrade). Config's
`asx.ib_enabled` defaults to False.

UNVERIFIED CAVEAT (unlike ibkr_futures_client.py's CME/NYMEX/COMEX mappings,
which were confirmed live against IB Gateway before shipping): ASX equity
market data is a separate IBKR market-data subscription from futures, and
has not been confirmed to be entitled on this account. Leave `asx.ib_enabled`
false until that's checked — the yfinance fallback (~20min delayed) works
unconditionally either way.
"""

import logging
import threading
from datetime import UTC, datetime
from typing import Any

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper

logger = logging.getLogger(__name__)

# yfinance-style ticker (e.g. "BHP.AX") -> IB symbol. ASX tickers are the
# yfinance ticker with the ".AX" suffix stripped; kept as an explicit map
# (rather than a generic strip) so an untested ticker fails closed instead
# of silently guessing a symbol IB might not resolve the way we expect.
IB_SYMBOL_MAP: dict[str, str] = {
    "BHP.AX": "BHP",
    "CBA.AX": "CBA",
    "CSL.AX": "CSL",
    "FMG.AX": "FMG",
    "WBC.AX": "WBC",
    "NAB.AX": "NAB",
    "RIO.AX": "RIO",
    "WES.AX": "WES",
    "MQG.AX": "MQG",
    "WOW.AX": "WOW",
}

# Informational connection/data-farm status codes IB sends on every
# successful connection — not failures.
_INFO_ERROR_CODES = {2103, 2104, 2105, 2106, 2107, 2119, 2158}


def _asx_stock_contract(symbol: str) -> Contract:
    c = Contract()
    c.symbol = symbol
    c.secType = "STK"
    c.exchange = "ASX"
    c.currency = "AUD"
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
    """Fetch recent 1-min bars for a yfinance-style ASX ticker (e.g.
    "BHP.AX") via IB Gateway. Returns [] on any failure — unmapped ticker,
    connection refused, timeout, or an IB API error — never raises."""
    symbol = IB_SYMBOL_MAP.get(ticker)
    if symbol is None:
        logger.warning("No IB symbol mapping for ticker %r", ticker)
        return []
    client = _HistoricalBarsClient(_asx_stock_contract(symbol), duration, bar_size)

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
