"""
paper/ledger.py — simulated paper-trading ledger (tasks/plan.md).

Forward-only and broker-free: when a configured signal fires, open a
hypothetical position that fades the signal bar's move, close it after a
fixed horizon, and record the return net of realistic costs. Runs inside
price_followup.py's loop, which already holds the IB→yfinance fetchers.

Rules (pre-registered in tasks/plan.md — change them only under a new
strategy name):
  - direction = -sign(signal bar % change); a flat bar is skipped.
  - entry = the first price fetched after the signal, within
    max_entry_delay_minutes, else skipped. The payload's own price is the
    trigger bar's close (stale on yfinance), kept only as signal_price.
  - one position per instrument: a signal arriving while an earlier trade
    on that instrument is still held is skipped, not stacked.
  - signals created before the strategy's first run are never traded.
"""

import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from sentinel.core.config import PaperConfig, PaperCost
from sentinel.core.db import Database

logger = logging.getLogger(__name__)


class PriceFetcherProtocol(Protocol):
    def get_price(self, source: str, instrument: str) -> float | None: ...


class _PassCache:
    """Fetches each (source, instrument) at most once per ledger pass."""

    def __init__(self, fetcher: PriceFetcherProtocol):
        self._fetcher = fetcher
        self._prices: dict[tuple[str, str], float | None] = {}

    def get_price(self, source: str, instrument: str) -> float | None:
        key = (source, instrument)
        if key not in self._prices:
            self._prices[key] = self._fetcher.get_price(source, instrument)
        return self._prices[key]


def round_trip_cost_ret(cost: PaperCost, price: float) -> float:
    """Round-trip cost (entry + exit) as a fraction of the entry price.

    Notional is the configured fixed size (ASX) or one contract (futures:
    price x multiplier). Commission per side is the flat fee plus the
    percentage, floored at the minimum. Slippage is slippage_ticks ticks
    against us on each side.
    """
    if price <= 0:
        raise ValueError(f"price must be positive, got {price!r}")
    notional = cost.notional if cost.notional is not None else price * cost.multiplier
    commission = max(
        cost.commission_per_side + notional * cost.commission_pct / 100,
        cost.commission_min,
    )
    slippage = cost.slippage_ticks * cost.tick_size / price
    return 2 * (commission / notional + slippage)


def _parse_ts(value: str) -> datetime:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _bar_change(payload: dict[str, Any]) -> float | None:
    """The signal bar's % change: futures volume_spike uses price_change_pct,
    ASX price_move uses change_pct."""
    for key in ("price_change_pct", "change_pct"):
        if payload.get(key) is not None:
            return float(payload[key])
    return None


def _holding_trade(
    positions: list[dict[str, Any]], signal_at: datetime
) -> dict[str, Any] | None:
    """The trade (if any) holding the instrument when a signal arrived. A
    position counts from its own signal time, so two signals handled in the
    same pass can't both open."""
    for trade in positions:
        held_from = _parse_ts(trade["signal_at"])
        held_to = _parse_ts(trade["exit_at"]) if trade["exit_at"] else None
        if held_from <= signal_at and (held_to is None or signal_at < held_to):
            return trade
    return None


def open_trades(
    db: Database, cfg: PaperConfig, fetcher: PriceFetcherProtocol, now: datetime
) -> dict[str, int]:
    """Open a fade trade (or record a skip) for each new qualifying signal.
    Signals still waiting for a first price are left for the next pass."""
    counts = {"opened": 0, "skipped": 0}
    cursor_key = f"paper_ledger:{cfg.strategy}:start_at"
    since = db.state.get(cursor_key)
    if since is None:
        db.state.set(cursor_key, now.isoformat())
        logger.info("Paper ledger %s starts at %s (forward-only)", cfg.strategy, now)
        return counts

    rules = [(source, kind) for source, kind in (r.split(":", 1) for r in cfg.signals)]
    prices = fetcher if isinstance(fetcher, _PassCache) else _PassCache(fetcher)
    for signal in db.paper_trades.new_signals(cfg.strategy, since, rules):
        payload = signal["payload"]
        instrument = str(payload.get("ticker", ""))
        signal_at = _parse_ts(signal["created_at"])
        row = {
            "signal_id": signal["id"],
            "strategy": cfg.strategy,
            "source": signal["source"],
            "signal_type": signal["signal_type"],
            "instrument": instrument,
            "signal_at": signal["created_at"],
            "signal_price": payload.get("price"),
        }

        def skip(note: str, row: dict[str, Any] = row) -> None:
            db.paper_trades.insert(**row, status="skipped", note=note)
            counts["skipped"] += 1

        change = _bar_change(payload)
        cost = cfg.costs.get(instrument)
        holder = _holding_trade(db.paper_trades.get_positions(cfg.strategy, instrument),
                                signal_at)
        if change is None:
            skip("no bar change in payload")
        elif change == 0:
            skip("flat bar")
        elif cost is None:
            skip(f"no cost model for {instrument}")
        elif holder is not None:
            skip(f"position already held by trade #{holder['id']}")
        elif (now - signal_at).total_seconds() > cfg.max_entry_delay_minutes * 60:
            skip(f"no entry price within {cfg.max_entry_delay_minutes} min")
        else:
            price = prices.get_price(signal["source"], instrument)
            if price is None:
                continue  # retry next pass, until max_entry_delay_minutes
            db.paper_trades.insert(
                **row,
                direction=-1 if change > 0 else 1,
                entry_price=price,
                entry_at=now.isoformat(),
                cost_ret=round_trip_cost_ret(cost, price),
                status="open",
            )
            counts["opened"] += 1
    return counts
