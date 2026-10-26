"""
paper/ledger.py — simulated paper-trading ledger (tasks/plan.md).

Forward-only and broker-free: when a configured signal fires, open a
hypothetical position that fades the signal bar's move, close it after a
fixed horizon, and record the return net of realistic costs. Runs inside
price_followup.py's loop, which already holds the IB→yfinance fetchers.
"""

from sentinel.core.config import PaperCost


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
