#!/usr/bin/env python3
"""
scripts/trade_size_study.py — does a big Kalshi trade predict the next move
better than an ordinary small trade on the same market?

The scorecard compares large_bet signals against random moments, which is
too easy a bar: any signal fired right after a trade beats long dead
stretches on a thin market (found 2026-11-07). This study uses each
market's own trade tape instead. For every trade, the price at t+h is the
last trade price at or before t+h, and the trade counts as "with taker" if
the price then moved the way the taker bet (YES taker: price up; NO taker:
price down). Large (>= --usd dollars paid) and small trades on the same
markets are then compared. Bid/ask bounce biases both groups the same way.

Markets studied: those with a >= --usd large_bet signal before --until.
Tapes are cached as JSON under --cache so reruns don't refetch.

Exit code is always 0 — this is a reporting tool, not a pass/fail gate.
"""

import argparse
import json
import logging
import os
import sys
import time
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("sentinel.trade_size_study")
logging.getLogger("httpx").setLevel(logging.WARNING)

KALSHI_API = "https://external-api.kalshi.com/trade-api/v2"
HORIZONS: dict[str, float] = {"t15": 15, "t60": 60, "t240": 240, "t1440": 1440}


@dataclass(frozen=True)
class Trade:
    ticker: str
    ts: float  # epoch seconds
    yes_price: float
    side: str  # taker side: "yes" or "no"
    count: float


def taker_cost(trade: Trade) -> float:
    """Dollars the taker paid: YES costs yes_price, NO costs 1 - yes_price."""
    price = 1.0 - trade.yes_price if trade.side == "no" else trade.yes_price
    return trade.count * price


def parse_trade(row: dict[str, Any]) -> Trade | None:
    try:
        ts = datetime.fromisoformat(row["created_time"].replace("Z", "+00:00")).timestamp()
        return Trade(
            ticker=row["ticker"],
            ts=ts,
            yes_price=float(row["yes_price_dollars"]),
            side=row["taker_side"],
            count=float(row["count_fp"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _empty() -> dict[str, Any]:
    return {"n": 0, "moved": 0, "with_taker": 0, "against": 0, "moves_pp": []}


def _by_market(trades: list[Trade]) -> dict[str, list[Trade]]:
    markets: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        markets[t.ticker].append(t)
    for tape in markets.values():
        tape.sort(key=lambda t: t.ts)
    return markets


def _market_follow_through(
    tape: list[Trade], minutes: float, usd_threshold: float, end_ts: float,
    out: dict[str, dict[str, Any]],
) -> None:
    times = [t.ts for t in tape]
    for trade in tape:
        target = trade.ts + minutes * 60
        if target > end_ts:
            continue
        later = tape[bisect_right(times, target) - 1]
        move = later.yes_price - trade.yes_price
        direction = 1 if trade.side == "yes" else -1
        stats = out["large" if taker_cost(trade) >= usd_threshold else "small"]
        stats["n"] += 1
        stats["moved"] += move != 0
        stats["with_taker"] += move * direction > 0
        stats["against"] += move * direction < 0
        stats["moves_pp"].append(abs(move) * 100)


def follow_through(
    trades: list[Trade], horizons: dict[str, float], usd_threshold: float, end_ts: float,
) -> dict[str, dict[str, dict[str, Any]]]:
    """group ('large'/'small') -> horizon -> counts and per-trade |move| in pp."""
    result: dict[str, dict[str, dict[str, Any]]] = {
        group: {h: _empty() for h in horizons} for group in ("large", "small")
    }
    for tape in _by_market(trades).values():
        for name, minutes in horizons.items():
            per = {"large": result["large"][name], "small": result["small"][name]}
            _market_follow_through(tape, minutes, usd_threshold, end_ts, per)
    return result


def market_wins(
    trades: list[Trade], horizon: str, minutes: float, usd_threshold: float, end_ts: float,
) -> tuple[int, int]:
    """(markets where large trades' with-taker rate beats small trades',
    markets with both kinds). A same-market check, so a few busy markets
    can't dominate the pooled numbers."""
    wins = compared = 0
    for tape in _by_market(trades).values():
        per = {"large": _empty(), "small": _empty()}
        _market_follow_through(tape, minutes, usd_threshold, end_ts, per)
        if per["large"]["n"] and per["small"]["n"]:
            compared += 1
            large_rate = per["large"]["with_taker"] / per["large"]["n"]
            small_rate = per["small"]["with_taker"] / per["small"]["n"]
            wins += large_rate > small_rate
    return wins, compared


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def fetch_tape(ticker: str, min_ts: int, max_ts: int) -> list[dict[str, Any]]:
    import httpx

    rows: list[dict[str, Any]] = []
    cursor = ""
    with httpx.Client(timeout=30.0) as client:
        while True:
            params: dict[str, Any] = {
                "ticker": ticker, "limit": 1000, "min_ts": min_ts, "max_ts": max_ts}
            if cursor:
                params["cursor"] = cursor
            resp = client.get(f"{KALSHI_API}/markets/trades", params=params)
            if resp.status_code == 429:
                time.sleep(min(float(resp.headers.get("Retry-After", 1)), 10))
                continue
            resp.raise_for_status()
            body = resp.json()
            rows.extend(body.get("trades", []))
            cursor = body.get("cursor") or ""
            if not cursor:
                return rows
            time.sleep(0.2)


def _study_tickers(db_path: str, usd: float, until: str) -> list[str]:
    import sqlite3

    tickers = set()
    with sqlite3.connect(db_path) as conn:
        for (payload,) in conn.execute(
            "SELECT payload FROM signals WHERE source='kalshi' AND signal_type='large_bet' "
            "AND created_at < ?", (until,)):
            p = json.loads(payload)
            trade = Trade(p["ticker"], 0, p.get("yes_price") or 0.0,
                          p.get("taker_side", "yes"), p["contracts"])
            if taker_cost(trade) >= usd:
                tickers.add(p["ticker"])
    return sorted(tickers)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=os.environ.get("SENTINEL_DB", "sentinel.db"))
    parser.add_argument("--usd", type=float, default=5000.0)
    parser.add_argument("--since", default="2026-06-02T00:00:00+00:00")
    parser.add_argument("--until", default="2026-11-03T12:00:00+00:00",
                        help="Stop before midterm results (default)")
    parser.add_argument("--cache", default="trade_tapes")
    args = parser.parse_args()

    min_ts = int(datetime.fromisoformat(args.since).timestamp())
    end_ts = int(datetime.fromisoformat(args.until).timestamp())
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)

    trades: list[Trade] = []
    for ticker in _study_tickers(args.db, args.usd, args.until):
        path = cache / f"{ticker}.json"
        if path.exists():
            rows = json.loads(path.read_text())
        else:
            rows = fetch_tape(ticker, min_ts, end_ts)
            path.write_text(json.dumps(rows))
        parsed = [t for t in map(parse_trade, rows) if t is not None]
        logger.info("%-34s %7d trades", ticker, len(parsed))
        trades.extend(parsed)

    result = follow_through(trades, HORIZONS, args.usd, end_ts)
    logger.info("")
    logger.info("%-6s %-6s %8s %7s %8s %8s %8s  %s", "group", "horiz", "n", "moved",
                "with", "against", "med pp", "markets large>small")
    for name, minutes in HORIZONS.items():
        wins, compared = market_wins(trades, name, minutes, args.usd, end_ts)
        for group in ("large", "small"):
            s = result[group][name]
            if not s["n"]:
                continue
            logger.info(
                "%-6s %-6s %8d %6.0f%% %7.0f%% %7.0f%% %8.2f  %s",
                group, name, s["n"], 100 * s["moved"] / s["n"],
                100 * s["with_taker"] / s["n"], 100 * s["against"] / s["n"],
                median(s["moves_pp"]), f"{wins}/{compared}" if group == "large" else "",
            )


if __name__ == "__main__":
    main()
