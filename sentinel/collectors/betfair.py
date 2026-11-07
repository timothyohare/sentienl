"""
collectors/betfair.py — Betfair Exchange collector (plan 06).

Finds markets from config filters (plus pinned market IDs), polls their
order books, and writes signals with source betfair_<domain> (for example
betfair_politics, betfair_racing):

  - odds_move:     a runner's implied probability moved >= odds_move_pp
                   percentage points since the previous poll (MEDIUM; HIGH at
                   odds_move_pp_high). Thin books (wide back/lay spread or low
                   market volume) are ignored.
  - matched_surge: the market's totalMatched grew by >= matched_surge since
                   the previous poll (MEDIUM; HIGH at matched_surge_high).
                   Market level only: the delayed app key reports runner
                   totalMatched as 0.0 and no traded volume (S2, 2026-11-03).

Markets are followed OPEN -> (SUSPENDED) -> CLOSED, then dropped with their
state. In-play markets are skipped unless poll_in_play is set.

HIGH odds_move signals get a price_t0 snapshot (probability 0-1, instrument
"<market_id>:<selection_id>") for price_followup.py.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sentinel.collectors.betfair_client import BetfairClientProtocol
from sentinel.core.config import BetfairConfig, BetfairFilter

logger = logging.getLogger(__name__)

STATE_PREV_PP = "betfair_prev_pp_{market}_{selection}"
STATE_PREV_MATCHED = "betfair_prev_matched_{market}"
STATE_LOCKED_OUT = "betfair_login_locked_out"

# A previous value older than this is not compared against: after downtime,
# a long in-play gap, or a market that dropped out of discovery and came
# back, the "move" would span hours and isn't a signal.
PREV_MAX_AGE_SECONDS = 600


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def implied_pp(price: float | None) -> float | None:
    """Decimal odds -> implied probability in percentage points (0-100)."""
    if price is None or price <= 1.0:
        return None
    return 100.0 / price


def _best(runner: dict[str, Any], side: str) -> float | None:
    offers = (runner.get("ex") or {}).get(side) or []
    return offers[0].get("price") if offers else None


def runner_pp(runner: dict[str, Any]) -> float | None:
    """A runner's implied probability in pp.

    Uses the last traded price only while it sits inside the current
    back/lay quotes. On thin markets one stray trade far from the quotes
    made the value flip every poll (midterm night, 2026-11-04), so the
    back/lay midpoint wins then. With a one-sided book the LTP is kept.
    """
    ltp = implied_pp(runner.get("lastPriceTraded"))
    back = implied_pp(_best(runner, "availableToBack"))
    lay = implied_pp(_best(runner, "availableToLay"))
    if back is None or lay is None:
        return ltp
    if ltp is not None and lay <= ltp <= back:
        return ltp
    return (back + lay) / 2


def spread_pp(runner: dict[str, Any]) -> float | None:
    """Back/lay spread in percentage points (back implies the higher pp)."""
    back = implied_pp(_best(runner, "availableToBack"))
    lay = implied_pp(_best(runner, "availableToLay"))
    if back is None or lay is None:
        return None
    return back - lay


def tier(value: float, low: float, high: float) -> str | None:
    size = abs(value)
    if size >= high:
        return "HIGH"
    if size >= low:
        return "MEDIUM"
    return None


def build_market_filter(f: BetfairFilter, now: float) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if f.event_type_ids:
        out["eventTypeIds"] = list(f.event_type_ids)
    if f.market_countries:
        out["marketCountries"] = list(f.market_countries)
    if f.market_type_codes:
        out["marketTypeCodes"] = list(f.market_type_codes)
    if f.text_query:
        out["textQuery"] = f.text_query
    if f.lookahead_hours is not None:
        until = datetime.fromtimestamp(now + f.lookahead_hours * 3600, UTC)
        out["marketStartTime"] = {"to": until.strftime("%Y-%m-%dT%H:%M:%SZ")}
    return out


@dataclass
class MarketInfo:
    market_id: str
    domain: str
    market_name: str = ""
    event_name: str = ""
    start_time: str = ""
    runners: dict[int, str] = field(default_factory=dict)

    @classmethod
    def from_catalogue(cls, cat: dict[str, Any], domain: str) -> "MarketInfo":
        return cls(
            market_id=cat["marketId"],
            domain=domain,
            market_name=cat.get("marketName", ""),
            event_name=(cat.get("event") or {}).get("name", ""),
            start_time=cat.get("marketStartTime", ""),
            runners={r["selectionId"]: r.get("runnerName", "")
                     for r in cat.get("runners") or []},
        )


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class BetfairCollector:
    def __init__(
        self,
        config: BetfairConfig,
        db: Any,
        client: BetfairClientProtocol,
        now: Callable[[], float] = time.time,
    ):
        self.config = config
        self.db = db
        self.client = client
        self._now = now
        self.registry: dict[str, MarketInfo] = {}
        self._closed: set[str] = set()
        self._last_discovery: float | None = None

    # -- discovery -------------------------------------------------------

    def discover(self) -> None:
        now = self._now()
        for f in self.config.filters:
            for cat in self.client.list_market_catalogue(
                build_market_filter(f, now), f.max_markets
            ):
                mid = cat["marketId"]
                if mid not in self._closed and mid not in self.registry:
                    self.registry[mid] = MarketInfo.from_catalogue(cat, f.domain)

        pinned = {m: d for m, d in self.config.pinned_markets.items() if m not in self._closed}
        if pinned:
            found = {c["marketId"]: c for c in self.client.list_market_catalogue(
                {"marketIds": list(pinned)}, len(pinned))}
            for mid, domain in pinned.items():
                if mid in found:
                    self.registry[mid] = MarketInfo.from_catalogue(found[mid], domain)
                else:
                    self.registry.setdefault(mid, MarketInfo(market_id=mid, domain=domain))
        self._last_discovery = now
        logger.info("Betfair discovery: %d markets tracked", len(self.registry))

    # -- state helpers ---------------------------------------------------

    def _get_fresh(self, key: str, now: float) -> float | None:
        raw = self.db.state.get(key)
        if not raw:
            return None
        try:
            value, ts = raw.split("|")
            if now - float(ts) > PREV_MAX_AGE_SECONDS:
                return None
            return float(value)
        except ValueError:
            return None

    def _set(self, key: str, value: float, now: float) -> None:
        self.db.state.set(key, f"{value}|{now}")

    def _drop(self, mid: str, book: dict[str, Any]) -> None:
        self.registry.pop(mid, None)
        self._closed.add(mid)
        self.db.state.delete(STATE_PREV_MATCHED.format(market=mid))
        for r in book.get("runners") or []:
            self.db.state.delete(STATE_PREV_PP.format(market=mid, selection=r["selectionId"]))
        logger.info("Betfair market %s closed; no longer tracked", mid)

    # -- polling ---------------------------------------------------------

    def poll(self) -> None:
        if not self.registry:
            return
        now = self._now()
        for book in self.client.list_market_book(list(self.registry)):
            mid = book.get("marketId", "")
            info = self.registry.get(mid)
            if info is None:
                continue
            status = book.get("status")
            if status == "CLOSED":
                self._drop(mid, book)
                continue
            if status != "OPEN" or (book.get("inplay") and not self.config.poll_in_play):
                continue
            self._check_matched(info, book, now)
            for runner in book.get("runners") or []:
                if runner.get("status") == "ACTIVE":
                    self._check_odds(info, book, runner, now)

    def _check_odds(
        self, info: MarketInfo, book: dict[str, Any], runner: dict[str, Any], now: float
    ) -> None:
        current = runner_pp(runner)
        if current is None:
            return
        sel = runner["selectionId"]
        key = STATE_PREV_PP.format(market=info.market_id, selection=sel)
        previous = self._get_fresh(key, now)
        self._set(key, current, now)
        if previous is None:
            return

        t = self.config.thresholds
        spread = spread_pp(runner)
        total = float(book.get("totalMatched") or 0.0)
        if total < t.min_market_matched or spread is None or spread > t.max_spread_pp:
            return
        change = current - previous
        priority = tier(change, t.odds_move_pp, t.odds_move_pp_high)
        if priority is None:
            return

        name = info.runners.get(sel) or str(sel)
        source = f"betfair_{info.domain}"
        signal_id = self.db.insert_signal(
            source=source,
            signal_type="odds_move",
            priority=priority,
            payload={
                "market_id": info.market_id,
                "selection_id": sel,
                "runner_name": name,
                "market_name": info.market_name,
                "event_name": info.event_name,
                "previous_pp": previous,
                "current_pp": current,
                "change_pp": change,
                "last_price_traded": runner.get("lastPriceTraded"),
                "spread_pp": spread,
                "market_total_matched": total,
                "is_delayed": bool(book.get("isMarketDataDelayed")),
            },
            summary=(f"{name} {previous:.1f}% -> {current:.1f}% ({change:+.1f}pp) "
                     f"— {info.event_name} / {info.market_name}"),
        )
        if priority == "HIGH":
            self.db.price_tracking.insert(
                signal_id, source, f"{info.market_id}:{sel}", price_t0=current / 100.0)

    def _check_matched(self, info: MarketInfo, book: dict[str, Any], now: float) -> None:
        total = float(book.get("totalMatched") or 0.0)
        key = STATE_PREV_MATCHED.format(market=info.market_id)
        previous = self._get_fresh(key, now)
        self._set(key, total, now)
        t = self.config.thresholds
        if previous is None or total < t.min_market_matched:
            return
        delta = total - previous
        if delta <= 0:
            return
        priority = tier(delta, t.matched_surge, t.matched_surge_high)
        if priority is None:
            return
        self.db.insert_signal(
            source=f"betfair_{info.domain}",
            signal_type="matched_surge",
            priority=priority,
            payload={
                "market_id": info.market_id,
                "market_name": info.market_name,
                "event_name": info.event_name,
                "previous_matched": previous,
                "total_matched": total,
                "matched_delta": delta,
                "is_delayed": bool(book.get("isMarketDataDelayed")),
            },
            summary=(f"{delta:,.0f} matched since last poll "
                     f"— {info.event_name} / {info.market_name}"),
        )

    # -- loop ------------------------------------------------------------

    def tick(self) -> None:
        if getattr(self.client, "locked_out", False):
            if self.db.state.get(STATE_LOCKED_OUT) is None:
                self.db.state.set(STATE_LOCKED_OUT, datetime.now(UTC).isoformat())
                logger.error("Betfair login locked out; collector idle until restart")
            return
        now = self._now()
        if (self._last_discovery is None
                or now - self._last_discovery >= self.config.discovery_interval_seconds):
            self.discover()
        self.poll()

    def run(self) -> None:
        logger.info("BetfairCollector starting: %d filters, %d pinned markets",
                    len(self.config.filters), len(self.config.pinned_markets))
        self.db.state.delete(STATE_LOCKED_OUT)
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("Betfair poll cycle failed")
            time.sleep(self.config.poll_interval_seconds)
