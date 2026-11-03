"""Unit tests for collectors/betfair.py (plan 06, Tasks 3-5).

The client is faked at BetfairClientProtocol; the database is a real temp
SQLite file. Fixture JSON in tests/fixtures/betfair/ was captured live from
the delayed app key on 2026-11-03.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from sentinel.collectors.betfair import (
    PREV_MAX_AGE_SECONDS,
    BetfairCollector,
    build_market_filter,
    implied_pp,
    runner_pp,
    spread_pp,
    tier,
)
from sentinel.core.config import BetfairConfig, BetfairFilter, BetfairThresholds
from sentinel.core.db import Database

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "betfair"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestImpliedPp:
    def test_even_money(self):
        assert implied_pp(2.0) == 50.0

    def test_long_odds(self):
        assert implied_pp(1000.0) == pytest.approx(0.1)

    @pytest.mark.parametrize("bad", [None, 0, 1.0, 0.5, -3])
    def test_invalid_prices(self, bad):
        assert implied_pp(bad) is None


def _runner(sel=1, ltp=None, back=None, lay=None, status="ACTIVE"):
    ex: dict[str, list] = {"availableToBack": [], "availableToLay": [], "tradedVolume": []}
    if back:
        ex["availableToBack"].append({"price": back, "size": 10})
    if lay:
        ex["availableToLay"].append({"price": lay, "size": 10})
    r: dict[str, Any] = {"selectionId": sel, "status": status, "ex": ex, "totalMatched": 0.0}
    if ltp is not None:
        r["lastPriceTraded"] = ltp
    return r


class TestRunnerPp:
    def test_uses_last_price_traded(self):
        assert runner_pp(_runner(ltp=4.0, back=3.9, lay=4.1)) == 25.0

    def test_falls_back_to_back_lay_midpoint(self):
        assert runner_pp(_runner(back=2.0, lay=2.5)) == pytest.approx((50 + 40) / 2)

    def test_none_without_prices(self):
        assert runner_pp(_runner()) is None
        assert runner_pp(_runner(back=2.0)) is None

    def test_fixture_runner(self):
        r = load_fixture("listMarketBook_best.json")[0]["runners"][0]
        assert runner_pp(r) == pytest.approx(100 / r["lastPriceTraded"])


class TestSpreadPp:
    def test_spread(self):
        assert spread_pp(_runner(back=2.0, lay=2.5)) == pytest.approx(10.0)

    def test_missing_side(self):
        assert spread_pp(_runner(back=2.0)) is None
        assert spread_pp(_runner(lay=2.0)) is None

    def test_fixture_spread_is_tight(self):
        r = load_fixture("listMarketBook_best.json")[0]["runners"][0]
        assert 0 <= spread_pp(r) < 1


class TestTier:
    @pytest.mark.parametrize("value,expected", [
        (4.99, None), (5.0, "MEDIUM"), (-5.0, "MEDIUM"), (14.99, "MEDIUM"),
        (15.0, "HIGH"), (-20.0, "HIGH"), (0.0, None),
    ])
    def test_boundaries(self, value, expected):
        assert tier(value, 5.0, 15.0) == expected


class TestBuildMarketFilter:
    def test_full_filter(self):
        f = BetfairFilter(domain="racing", event_type_ids=["7"], market_countries=["AU"],
                          market_type_codes=["WIN"], text_query="Cup", lookahead_hours=6)
        out = build_market_filter(f, now=0.0)
        assert out["eventTypeIds"] == ["7"]
        assert out["marketCountries"] == ["AU"]
        assert out["marketTypeCodes"] == ["WIN"]
        assert out["textQuery"] == "Cup"
        assert out["marketStartTime"] == {"to": "1970-01-01T06:00:00Z"}

    def test_minimal_filter_omits_empty_keys(self):
        out = build_market_filter(BetfairFilter(domain="politics", event_type_ids=["2378961"]),
                                  now=0.0)
        assert out == {"eventTypeIds": ["2378961"]}


# ---------------------------------------------------------------------------
# Collector harness
# ---------------------------------------------------------------------------

class FakeClient:
    def __init__(self):
        self.catalogues: dict[str, list[dict]] = {}
        self.books: list[list[dict]] = []
        self.catalogue_calls: list[dict] = []
        self.book_calls: list[list[str]] = []
        self.locked_out = False

    def list_market_catalogue(self, market_filter, max_results):
        self.catalogue_calls.append(market_filter)
        if "marketIds" in market_filter:
            return [m for cat in self.catalogues.values() for m in cat
                    if m["marketId"] in market_filter["marketIds"]]
        return self.catalogues.get(",".join(market_filter.get("eventTypeIds", [])), [])[
            :max_results]

    def list_market_book(self, market_ids):
        self.book_calls.append(list(market_ids))
        return self.books.pop(0) if self.books else []


def _cat(mid, runners=((1, "Alpha"), (2, "Beta")), event="Event", name="Win"):
    return {"marketId": mid, "marketName": name, "event": {"name": event},
            "marketStartTime": "2026-11-04T00:00:00.000Z",
            "runners": [{"selectionId": s, "runnerName": n} for s, n in runners]}


def _book(mid, runners, total=50000.0, status="OPEN", inplay=False):
    return {"marketId": mid, "status": status, "inplay": inplay, "totalMatched": total,
            "isMarketDataDelayed": True, "runners": runners}


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.init()
    yield d
    d.close()


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def clock():
    return Clock()


def _collector(db, client, clock, **cfg):
    cfg.setdefault("filters", [BetfairFilter(domain="politics", event_type_ids=["2378961"])])
    cfg.setdefault("thresholds", BetfairThresholds(min_market_matched=1000))
    return BetfairCollector(BetfairConfig(enabled=True, **cfg), db, client, now=clock)


def _signals(db):
    rows = db.execute(
        "SELECT source, signal_type, priority, payload, summary FROM signals ORDER BY id"
    ).fetchall()
    return [{"source": r[0], "type": r[1], "priority": r[2], "payload": json.loads(r[3]),
             "summary": r[4]} for r in rows]


def _poll_prices(col, client, clock, *ltps, mid="1.1", total=50000.0, step=30):
    """Poll once per LTP for runner 1 (runner 2 fixed at 4.0)."""
    for ltp in ltps:
        client.books.append([_book(mid, [_runner(1, ltp, ltp - 0.02, ltp + 0.02),
                                         _runner(2, 4.0, 3.95, 4.05)], total=total)])
        col.poll()
        clock.t += step


# ---------------------------------------------------------------------------
# Discovery and lifecycle (Task 4)
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_registers_markets_with_domain_and_names(self, db, client, clock):
        client.catalogues["2378961"] = load_fixture("catalogue_politics.json")
        col = _collector(db, client, clock)
        col.discover()
        first = load_fixture("catalogue_politics.json")[0]
        info = col.registry[first["marketId"]]
        assert info.domain == "politics"
        assert info.market_name == first["marketName"]
        assert info.event_name == first["event"]["name"]
        assert info.runners[first["runners"][0]["selectionId"]] == first["runners"][0]["runnerName"]

    def test_respects_max_markets(self, db, client, clock):
        client.catalogues["7"] = [_cat(f"1.{i}") for i in range(5)]
        col = _collector(db, client, clock, filters=[
            BetfairFilter(domain="racing", event_type_ids=["7"], max_markets=2)])
        col.discover()
        assert len(col.registry) == 2

    def test_pinned_market_registered_even_if_catalogue_misses_it(self, db, client, clock):
        col = _collector(db, client, clock, filters=[], pinned_markets={"1.99": "politics"})
        col.discover()
        assert col.registry["1.99"].domain == "politics"

    def test_pinned_market_gets_names_from_catalogue(self, db, client, clock):
        client.catalogues["x"] = [_cat("1.99", event="Midterms", name="House")]
        col = _collector(db, client, clock, filters=[], pinned_markets={"1.99": "politics"})
        col.discover()
        assert col.registry["1.99"].event_name == "Midterms"

    def test_rediscovery_keeps_existing_and_adds_new(self, db, client, clock):
        client.catalogues["2378961"] = [_cat("1.1")]
        col = _collector(db, client, clock)
        col.discover()
        client.catalogues["2378961"] = [_cat("1.2")]
        col.discover()
        assert set(col.registry) == {"1.1", "1.2"}

    def test_discovery_failure_keeps_registry(self, db, client, clock):
        client.catalogues["2378961"] = [_cat("1.1")]
        col = _collector(db, client, clock)
        col.discover()
        client.catalogues["2378961"] = []
        col.discover()
        assert "1.1" in col.registry


class TestLifecycle:
    def _setup(self, db, client, clock, **cfg):
        client.catalogues["2378961"] = [_cat("1.1")]
        col = _collector(db, client, clock, **cfg)
        col.discover()
        return col

    def test_closed_market_dropped_and_state_cleared(self, db, client, clock):
        col = self._setup(db, client, clock)
        _poll_prices(col, client, clock, 2.0)
        assert db.state.get("betfair_prev_pp_1.1_1") is not None
        client.books.append([_book("1.1", [_runner(1, 2.0), _runner(2, 4.0)], status="CLOSED")])
        col.poll()
        assert "1.1" not in col.registry
        assert db.state.get("betfair_prev_pp_1.1_1") is None
        assert db.state.get("betfair_prev_matched_1.1") is None

    def test_closed_market_not_rediscovered_into_polling(self, db, client, clock):
        col = self._setup(db, client, clock)
        client.books.append([_book("1.1", [_runner(1, 2.0)], status="CLOSED")])
        col.poll()
        col.discover()  # catalogue still lists it
        assert "1.1" not in col.registry

    def test_suspended_market_skipped_without_state_update(self, db, client, clock):
        col = self._setup(db, client, clock)
        _poll_prices(col, client, clock, 2.0)
        before = db.state.get("betfair_prev_pp_1.1_1")
        client.books.append([_book("1.1", [_runner(1, 1.5, 1.49, 1.51)], status="SUSPENDED")])
        col.poll()
        assert db.state.get("betfair_prev_pp_1.1_1") == before
        assert "1.1" in col.registry

    def test_in_play_skipped_by_default(self, db, client, clock):
        col = self._setup(db, client, clock)
        _poll_prices(col, client, clock, 2.0)
        client.books.append([_book("1.1", [_runner(1, 1.2, 1.19, 1.21)], inplay=True)])
        col.poll()
        assert _signals(db) == []

    def test_in_play_polled_when_enabled(self, db, client, clock):
        col = self._setup(db, client, clock, poll_in_play=True)
        _poll_prices(col, client, clock, 2.0)
        client.books.append([_book("1.1", [_runner(1, 1.2, 1.19, 1.21),
                                           _runner(2, 4.0, 3.95, 4.05)], inplay=True)])
        col.poll()
        assert [s["type"] for s in _signals(db)] == ["odds_move"]

    def test_unknown_market_in_book_ignored(self, db, client, clock):
        col = self._setup(db, client, clock)
        client.books.append([_book("9.9", [_runner(1, 2.0)])])
        col.poll()
        assert _signals(db) == []

    def test_books_matched_by_id_not_order(self, db, client, clock):
        client.catalogues["2378961"] = [_cat("1.1"), _cat("1.2")]
        col = _collector(db, client, clock)
        col.discover()
        client.books.append([_book("1.2", [_runner(1, 2.0, 1.98, 2.02)]),
                             _book("1.1", [_runner(1, 3.0, 2.98, 3.02)])])
        col.poll()
        assert float(db.state.get("betfair_prev_pp_1.2_1").split("|")[0]) == 50.0

    def test_no_registry_no_book_call(self, db, client, clock):
        col = _collector(db, client, clock, filters=[])
        col.poll()
        assert client.book_calls == []


# ---------------------------------------------------------------------------
# odds_move (Task 3)
# ---------------------------------------------------------------------------

class TestOddsMove:
    def _col(self, db, client, clock, **cfg):
        client.catalogues["2378961"] = [_cat("1.1", event="Midterms", name="House")]
        col = _collector(db, client, clock, **cfg)
        col.discover()
        return col

    def test_first_poll_sets_baseline_only(self, db, client, clock):
        col = self._col(db, client, clock)
        _poll_prices(col, client, clock, 2.0)
        assert _signals(db) == []

    def test_medium_move(self, db, client, clock):
        col = self._col(db, client, clock)
        _poll_prices(col, client, clock, 2.0, 100 / 60)   # 50pp -> 60pp
        (s,) = _signals(db)
        assert (s["source"], s["type"], s["priority"]) == (
            "betfair_politics", "odds_move", "MEDIUM")
        p = s["payload"]
        assert p["market_id"] == "1.1" and p["selection_id"] == 1
        assert p["runner_name"] == "Alpha" and p["event_name"] == "Midterms"
        assert p["previous_pp"] == pytest.approx(50.0)
        assert p["current_pp"] == pytest.approx(60.0)
        assert p["change_pp"] == pytest.approx(10.0)
        assert p["is_delayed"] is True
        assert "Alpha" in s["summary"] and "+10.0pp" in s["summary"]

    def test_high_move_is_price_tracked(self, db, client, clock):
        col = self._col(db, client, clock)
        _poll_prices(col, client, clock, 2.0, 100 / 70)   # +20pp
        (s,) = _signals(db)
        assert s["priority"] == "HIGH"
        row = db.execute(
            "SELECT source, instrument, price_t0 FROM post_price_tracking").fetchone()
        assert row[0] == "betfair_politics" and row[1] == "1.1:1"
        assert row[2] == pytest.approx(0.70)

    def test_medium_move_not_price_tracked(self, db, client, clock):
        col = self._col(db, client, clock)
        _poll_prices(col, client, clock, 2.0, 100 / 60)
        assert db.execute("SELECT COUNT(*) FROM post_price_tracking").fetchone()[0] == 0

    def test_small_move_ignored(self, db, client, clock):
        col = self._col(db, client, clock)
        _poll_prices(col, client, clock, 2.0, 100 / 54)
        assert _signals(db) == []

    def test_compares_against_previous_poll_not_first(self, db, client, clock):
        col = self._col(db, client, clock)
        _poll_prices(col, client, clock, 2.0, 100 / 54, 100 / 58)  # +4 then +4
        assert _signals(db) == []

    def test_thin_spread_suppressed(self, db, client, clock):
        col = self._col(db, client, clock)
        client.books.append([_book("1.1", [_runner(1, 2.0, 1.9, 2.1)])])
        col.poll()
        clock.t += 30
        client.books.append([_book("1.1", [_runner(1, 1.5, 1.3, 1.8)])])  # spread ~21pp
        col.poll()
        assert _signals(db) == []

    def test_low_market_volume_suppressed(self, db, client, clock):
        col = self._col(db, client, clock)
        _poll_prices(col, client, clock, 2.0, 100 / 70, total=500.0)
        assert _signals(db) == []

    def test_stale_previous_price_not_compared(self, db, client, clock):
        col = self._col(db, client, clock)
        _poll_prices(col, client, clock, 2.0, step=PREV_MAX_AGE_SECONDS + 1)
        _poll_prices(col, client, clock, 100 / 70)
        assert _signals(db) == []

    def test_inactive_runner_ignored(self, db, client, clock):
        col = self._col(db, client, clock)
        _poll_prices(col, client, clock, 2.0)
        client.books.append([_book("1.1", [_runner(1, 1.2, 1.19, 1.21, status="REMOVED")])])
        col.poll()
        assert _signals(db) == []

    def test_racing_domain_source(self, db, client, clock):
        client.catalogues["7"] = [_cat("1.1")]
        col = _collector(db, client, clock, filters=[
            BetfairFilter(domain="racing", event_type_ids=["7"])])
        col.discover()
        _poll_prices(col, client, clock, 2.0, 100 / 70)
        assert _signals(db)[0]["source"] == "betfair_racing"


# ---------------------------------------------------------------------------
# matched_surge (Task 5, market level: the delayed key has no runner volume)
# ---------------------------------------------------------------------------

class TestMatchedSurge:
    def _col(self, db, client, clock):
        client.catalogues["2378961"] = [_cat("1.1")]
        col = _collector(db, client, clock, thresholds=BetfairThresholds(
            min_market_matched=1000, matched_surge=5000, matched_surge_high=50000))
        col.discover()
        return col

    def _poll_totals(self, col, client, clock, *totals):
        for t in totals:
            client.books.append([_book("1.1", [_runner(1, 2.0, 1.98, 2.02)], total=t)])
            col.poll()
            clock.t += 30

    def _surges(self, db):
        return [s for s in _signals(db) if s["type"] == "matched_surge"]

    def test_first_poll_no_signal(self, db, client, clock):
        col = self._col(db, client, clock)
        self._poll_totals(col, client, clock, 100000)
        assert self._surges(db) == []

    def test_medium_surge(self, db, client, clock):
        col = self._col(db, client, clock)
        self._poll_totals(col, client, clock, 100000, 106000)
        (s,) = self._surges(db)
        assert s["priority"] == "MEDIUM"
        assert s["payload"]["matched_delta"] == pytest.approx(6000)
        assert s["payload"]["total_matched"] == pytest.approx(106000)

    def test_high_surge(self, db, client, clock):
        col = self._col(db, client, clock)
        self._poll_totals(col, client, clock, 100000, 160000)
        assert self._surges(db)[0]["priority"] == "HIGH"

    def test_small_increase_and_decrease_ignored(self, db, client, clock):
        col = self._col(db, client, clock)
        self._poll_totals(col, client, clock, 100000, 104000, 90000)
        assert self._surges(db) == []

    def test_stale_baseline_ignored(self, db, client, clock):
        col = self._col(db, client, clock)
        self._poll_totals(col, client, clock, 100000)
        clock.t += PREV_MAX_AGE_SECONDS
        self._poll_totals(col, client, clock, 200000)
        assert self._surges(db) == []


# ---------------------------------------------------------------------------
# run loop pieces
# ---------------------------------------------------------------------------

class TestTick:
    def test_discovers_on_first_tick_then_on_interval(self, db, client, clock):
        client.catalogues["2378961"] = [_cat("1.1")]
        col = _collector(db, client, clock, discovery_interval_seconds=600)
        col.tick()
        col.tick()
        assert len(client.catalogue_calls) == 1
        clock.t += 600
        col.tick()
        assert len(client.catalogue_calls) == 2

    def test_lockout_recorded_in_state(self, db, client, clock):
        col = _collector(db, client, clock)
        client.locked_out = True
        col.tick()
        assert db.state.get("betfair_login_locked_out") is not None


# ---------------------------------------------------------------------------
# Mutation-driven additions (2026-11-03): full payloads, inclusive limits,
# tracking row linked to its signal, runner-name fallback.
# ---------------------------------------------------------------------------

class TestPayloadsAndBoundaries:
    def _col(self, db, client, clock, **thr):
        client.catalogues["2378961"] = [_cat("1.1", event="Ev", name="Mk")]
        t = dict(min_market_matched=1000, max_spread_pp=5.0,
                 matched_surge=5000, matched_surge_high=50000)
        t.update(thr)
        col = _collector(db, client, clock, thresholds=BetfairThresholds(**t))
        col.discover()
        return col

    def test_odds_move_payload_is_complete(self, db, client, clock):
        col = self._col(db, client, clock)
        _poll_prices(col, client, clock, 2.0, 100 / 70, total=50000.0)
        (s,) = [x for x in _signals(db) if x["type"] == "odds_move"]
        p = s["payload"]
        assert p["market_name"] == "Mk" and p["event_name"] == "Ev"
        assert p["last_price_traded"] == pytest.approx(100 / 70)
        assert p["spread_pp"] == pytest.approx(
            100 / (100 / 70 - 0.02) - 100 / (100 / 70 + 0.02))
        assert p["market_total_matched"] == 50000.0
        assert p["is_delayed"] is True

    def test_tracking_row_points_at_its_signal(self, db, client, clock):
        col = self._col(db, client, clock)
        _poll_prices(col, client, clock, 2.0, 100 / 70)
        sig_id = db.execute("SELECT id FROM signals WHERE signal_type='odds_move'").fetchone()[0]
        assert db.execute("SELECT signal_id FROM post_price_tracking").fetchone()[0] == sig_id

    def test_unnamed_runner_uses_selection_id(self, db, client, clock):
        client.catalogues["2378961"] = [_cat("1.1", runners=())]
        col = _collector(db, client, clock)
        col.discover()
        _poll_prices(col, client, clock, 2.0, 100 / 70)
        assert _signals(db)[0]["payload"]["runner_name"] == "1"

    def test_volume_exactly_at_minimum_is_allowed(self, db, client, clock):
        col = self._col(db, client, clock, min_market_matched=50000)
        _poll_prices(col, client, clock, 2.0, 100 / 70, total=50000.0)
        assert [s["type"] for s in _signals(db)] == ["odds_move"]

    def test_spread_exactly_at_maximum_is_allowed(self, db, client, clock):
        col = self._col(db, client, clock, max_spread_pp=10.0)
        client.books.append([_book("1.1", [_runner(1, 4.0, 3.9, 4.1)])])
        col.poll()
        clock.t += 30
        client.books.append([_book("1.1", [_runner(1, 2.25, 2.0, 2.5)])])  # spread exactly 10pp
        col.poll()
        assert [s["type"] for s in _signals(db)] == ["odds_move"]

    def test_matched_surge_payload_and_minimum_inclusive(self, db, client, clock):
        col = self._col(db, client, clock, min_market_matched=106000)
        for total in (100000, 106000):
            client.books.append([_book("1.1", [_runner(1, 2.0, 1.98, 2.02)], total=total)])
            col.poll()
            clock.t += 30
        (s,) = [x for x in _signals(db) if x["type"] == "matched_surge"]
        p = s["payload"]
        assert (p["market_id"], p["market_name"], p["event_name"]) == ("1.1", "Mk", "Ev")
        assert p["previous_matched"] == 100000 and p["is_delayed"] is True

    def test_missing_total_matched_counts_as_zero(self, db, client, clock):
        col = self._col(db, client, clock)
        book = _book("1.1", [_runner(1, 2.0, 1.98, 2.02)])
        del book["totalMatched"]
        client.books.append([book])
        col.poll()
        assert float(db.state.get("betfair_prev_matched_1.1").split("|")[0]) == 0.0
