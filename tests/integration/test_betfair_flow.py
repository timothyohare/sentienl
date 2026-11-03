"""Integration: Betfair collector + real SQLite + correlation detector,
replaying one racing market from discovery to close (plan 06, Task 4).

Uses the real catalogue fixture captured on 2026-11-03; only the order-book
timeline is scripted.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sentinel.collectors.betfair import BetfairCollector
from sentinel.collectors.correlation_detector import CorrelationDetector
from sentinel.core.config import BetfairConfig, BetfairFilter, BetfairThresholds
from sentinel.core.db import Database

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "betfair"


class ReplayClient:
    def __init__(self, catalogue, timeline):
        self.catalogue = catalogue
        self.timeline = list(timeline)
        self.locked_out = False
        self.book_requests = 0

    def list_market_catalogue(self, market_filter, max_results):
        return self.catalogue[:max_results]

    def list_market_book(self, market_ids):
        self.book_requests += 1
        return self.timeline.pop(0) if self.timeline else []


def _book(mid, sel_prices, status="OPEN", inplay=False, total=40000.0):
    return {
        "marketId": mid, "status": status, "inplay": inplay, "totalMatched": total,
        "isMarketDataDelayed": True,
        "runners": [
            {"selectionId": s, "status": "ACTIVE", "lastPriceTraded": p,
             "ex": {"availableToBack": [{"price": round(p - 0.02, 2), "size": 50}],
                    "availableToLay": [{"price": round(p + 0.02, 2), "size": 50}]}}
            for s, p in sel_prices
        ],
    }


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "flow.db"))
    d.init()
    yield d
    d.close()


def test_racing_market_full_lifecycle(db):
    market = json.loads((FIXTURES / "catalogue_racing.json").read_text())[0]
    mid = market["marketId"]
    fav, second = (r["selectionId"] for r in market["runners"][:2])
    timeline = [
        [_book(mid, [(fav, 4.0), (second, 5.0)], total=40000)],          # baseline
        [_book(mid, [(fav, 2.5), (second, 5.0)], total=95000)],          # fav 25->40pp, +55k
        [_book(mid, [(fav, 2.5), (second, 5.0)], status="SUSPENDED")],   # at the jump
        [_book(mid, [(fav, 1.1), (second, 50.0)], inplay=True)],         # running: skipped
        [_book(mid, [(fav, 1.01), (second, 1000.0)], status="CLOSED")],  # settled
    ]
    client = ReplayClient([market], timeline)
    clock = iter(range(1_000_000, 1_000_000 + 30 * 20, 30))
    col = BetfairCollector(
        BetfairConfig(
            enabled=True,
            filters=[BetfairFilter(domain="racing", event_type_ids=["7"])],
            thresholds=BetfairThresholds(min_market_matched=1000),
        ),
        db, client, now=lambda: float(next(clock)),
    )

    for _ in range(len(timeline) + 1):
        col.tick()

    rows = db.execute(
        "SELECT source, signal_type, priority, payload FROM signals ORDER BY id").fetchall()
    kinds = [(r[0], r[1], r[2]) for r in rows]
    assert ("betfair_racing", "odds_move", "HIGH") in kinds
    assert ("betfair_racing", "matched_surge", "HIGH") in kinds
    assert len(rows) == 2, "suspended and in-play books must not add signals"
    move = next(json.loads(r[3]) for r in rows if r[1] == "odds_move")
    assert move["runner_name"] == market["runners"][0]["runnerName"]

    tracked = [tuple(r) for r in db.execute("SELECT instrument FROM post_price_tracking")]
    assert tracked == [(f"{mid}:{fav}",)]

    assert mid not in col.registry
    assert db.execute(
        "SELECT COUNT(*) FROM state WHERE key LIKE 'betfair_prev_%'").fetchone()[0] == 0
    # After close, nothing is left to poll.
    assert client.book_requests == len(timeline)


def test_racing_signals_never_anchor_a_correlation(db):
    market = json.loads((FIXTURES / "catalogue_racing.json").read_text())[0]
    mid = market["marketId"]
    sel = market["runners"][0]["selectionId"]
    client = ReplayClient([market], [
        [_book(mid, [(sel, 4.0)])],
        [_book(mid, [(sel, 2.0)])],
    ])
    clock = iter(range(0, 10_000, 30))
    col = BetfairCollector(
        BetfairConfig(enabled=True,
                      filters=[BetfairFilter(domain="racing", event_type_ids=["7"])],
                      thresholds=BetfairThresholds(min_market_matched=1000)),
        db, client, now=lambda: float(next(clock)),
    )
    col.tick()
    col.tick()
    db.insert_signal(source="futures_oil", signal_type="volume_spike", priority="HIGH",
                     payload={}, summary="unrelated oil spike")

    cfg = MagicMock()
    cfg.correlation.exclude_sources = ["betfair_racing"]
    CorrelationDetector(config=cfg, db=db).check_and_signal()
    assert db.execute(
        "SELECT COUNT(*) FROM signals WHERE source='correlation_detector'").fetchone()[0] == 0

    cfg.correlation.exclude_sources = []
    CorrelationDetector(config=cfg, db=db).check_and_signal()
    assert db.execute(
        "SELECT COUNT(*) FROM signals WHERE source='correlation_detector'").fetchone()[0] == 1
