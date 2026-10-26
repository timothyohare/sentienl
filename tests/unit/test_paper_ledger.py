"""Unit tests for sentinel/paper/ledger.py — the simulated paper-trading
ledger (tasks/plan.md)."""

from datetime import UTC, datetime, timedelta

import pytest

from sentinel.core.config import PaperConfig, PaperCost
from sentinel.core.db import Database
from sentinel.paper.ledger import close_due_trades, open_trades, round_trip_cost_ret

NOW = datetime(2026, 10, 27, 14, 0, tzinfo=UTC)
MGC = PaperCost(tick_size=0.1, multiplier=10, commission_per_side=0.62, slippage_ticks=1)


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    database.init()
    yield database
    database.close()


@pytest.fixture
def cfg():
    return PaperConfig(
        enabled=True,
        signals=["futures_*:volume_spike"],
        costs={"GC=F": MGC, "CL=F": PaperCost(tick_size=0.01, multiplier=100)},
    )


class FakeFetcher:
    """Returns a canned price per instrument and records every call."""

    def __init__(self, prices: dict[str, float | None]):
        self.prices = prices
        self.calls: list[tuple[str, str]] = []

    def get_price(self, source, instrument):
        self.calls.append((source, instrument))
        return self.prices.get(instrument)


def _at(minutes_before_now: float) -> str:
    return (NOW - timedelta(minutes=minutes_before_now)).isoformat()


def _spike(db, minutes_ago=2.0, change=0.2, ticker="GC=F", source="futures_gold",
           signal_type="volume_spike", price=4000.0) -> int:
    payload = {"ticker": ticker, "price": price, "price_change_pct": change}
    return db.insert_signal(source, signal_type, "MEDIUM", payload, "spike",
                            created_at=_at(minutes_ago))


def _start(db, cfg, minutes_ago=60.0):
    """Set the forward-only cursor so earlier fixtures count as history."""
    db.state.set(f"paper_ledger:{cfg.strategy}:start_at", _at(minutes_ago))


def _trades(db):
    return db.execute_fetchall("SELECT * FROM paper_trades ORDER BY id")

# ---------------------------------------------------------------------------
# round_trip_cost_ret() — cost as a fraction of entry price
# ---------------------------------------------------------------------------


class TestRoundTripCostRet:
    def test_micro_futures_commission_and_slippage(self):
        # MGC: 10 oz, $0.10 tick = $1/tick. At 4000 the contract is $40,000.
        # Round trip = 2 x $0.62 commission + 2 x 1 tick ($1) = $3.24.
        mgc = PaperCost(tick_size=0.1, multiplier=10, commission_per_side=0.62,
                        slippage_ticks=1)
        assert round_trip_cost_ret(mgc, 4000.0) == pytest.approx(3.24 / 40_000)

    def test_asx_percentage_commission_hits_minimum(self):
        # 0.08% of $5,000 is $4, below the $6 minimum: 2 x $6 / $5,000 = 0.24%,
        # plus 2 x 1c slippage on a $40 share = 0.05%.
        asx = PaperCost(tick_size=0.01, commission_pct=0.08, commission_min=6.0,
                        notional=5000.0)
        assert round_trip_cost_ret(asx, 40.0) == pytest.approx(0.0024)
        asx.slippage_ticks = 1
        assert round_trip_cost_ret(asx, 40.0) == pytest.approx(0.0024 + 0.0005)

    def test_asx_percentage_commission_above_minimum(self):
        # 0.08% of $20,000 = $16 > $6, so the percentage applies: 0.16% round trip.
        asx = PaperCost(commission_pct=0.08, commission_min=6.0, notional=20_000.0)
        assert round_trip_cost_ret(asx, 40.0) == pytest.approx(0.0016)

    def test_non_positive_price_rejected(self):
        with pytest.raises(ValueError):
            round_trip_cost_ret(PaperCost(), 0.0)


# ---------------------------------------------------------------------------
# open_trades() — fade new qualifying signals at the first fetched price
# ---------------------------------------------------------------------------


class TestOpenTrades:
    def test_up_bar_opens_short_at_fetched_price(self, db, cfg):
        _start(db, cfg)
        signal_id = _spike(db, change=0.2, price=4000.0)
        open_trades(db, cfg, FakeFetcher({"GC=F": 4001.0}), NOW)
        [trade] = _trades(db)
        assert trade["signal_id"] == signal_id
        assert trade["status"] == "open"
        assert trade["direction"] == -1
        assert trade["signal_price"] == 4000.0
        assert trade["entry_price"] == 4001.0
        assert trade["entry_at"] == NOW.isoformat()
        assert trade["strategy"] == "fade60"
        assert (trade["source"], trade["instrument"]) == ("futures_gold", "GC=F")
        assert trade["cost_ret"] == pytest.approx(round_trip_cost_ret(MGC, 4001.0))

    def test_down_bar_opens_long(self, db, cfg):
        _start(db, cfg)
        _spike(db, change=-0.2)
        open_trades(db, cfg, FakeFetcher({"GC=F": 4001.0}), NOW)
        assert _trades(db)[0]["direction"] == 1

    def test_first_run_sets_cursor_and_ignores_history(self, db, cfg):
        _spike(db, minutes_ago=2)
        open_trades(db, cfg, FakeFetcher({"GC=F": 4001.0}), NOW)
        assert _trades(db) == []
        assert db.state.get("paper_ledger:fade60:start_at") == NOW.isoformat()

    def test_signal_before_cursor_never_opened(self, db, cfg):
        _start(db, cfg, minutes_ago=1)
        _spike(db, minutes_ago=2)
        open_trades(db, cfg, FakeFetcher({"GC=F": 4001.0}), NOW)
        assert _trades(db) == []

    def test_second_run_is_a_no_op(self, db, cfg):
        _start(db, cfg)
        _spike(db)
        fetcher = FakeFetcher({"GC=F": 4001.0})
        open_trades(db, cfg, fetcher, NOW)
        open_trades(db, cfg, fetcher, NOW + timedelta(minutes=5))
        assert len(_trades(db)) == 1

    def test_signal_on_instrument_with_open_position_is_skipped(self, db, cfg):
        _start(db, cfg)
        first = _spike(db, minutes_ago=4)
        second = _spike(db, minutes_ago=2)
        open_trades(db, cfg, FakeFetcher({"GC=F": 4001.0}), NOW)
        trades = {t["signal_id"]: t for t in _trades(db)}
        assert trades[first]["status"] == "open"
        assert trades[second]["status"] == "skipped"
        assert f"#{trades[first]['id']}" in trades[second]["note"]
        assert trades[second]["entry_price"] is None

    def test_other_instrument_still_opens(self, db, cfg):
        _start(db, cfg)
        _spike(db, minutes_ago=4)
        _spike(db, minutes_ago=2, ticker="CL=F", source="futures_oil")
        open_trades(db, cfg, FakeFetcher({"GC=F": 4001.0, "CL=F": 60.0}), NOW)
        assert [t["status"] for t in _trades(db)] == ["open", "open"]

    def test_flat_bar_is_skipped(self, db, cfg):
        _start(db, cfg)
        _spike(db, change=0.0)
        open_trades(db, cfg, FakeFetcher({"GC=F": 4001.0}), NOW)
        [trade] = _trades(db)
        assert (trade["status"], trade["note"]) == ("skipped", "flat bar")

    def test_missing_cost_model_is_skipped(self, db, cfg):
        _start(db, cfg)
        _spike(db, ticker="ES=F", source="futures_sp500")
        open_trades(db, cfg, FakeFetcher({"ES=F": 6000.0}), NOW)
        [trade] = _trades(db)
        assert (trade["status"], trade["note"]) == ("skipped", "no cost model for ES=F")

    def test_non_matching_signal_types_and_sources_ignored(self, db, cfg):
        _start(db, cfg)
        _spike(db, signal_type="price_move")
        _spike(db, source="asx", ticker="CBA.AX")
        fetcher = FakeFetcher({"GC=F": 4001.0, "CBA.AX": 160.0})
        open_trades(db, cfg, fetcher, NOW)
        assert _trades(db) == []
        assert fetcher.calls == []

    def test_no_price_leaves_signal_for_next_pass(self, db, cfg):
        _start(db, cfg)
        _spike(db, minutes_ago=2)
        open_trades(db, cfg, FakeFetcher({}), NOW)
        assert _trades(db) == []
        open_trades(db, cfg, FakeFetcher({"GC=F": 4002.0}), NOW + timedelta(minutes=5))
        assert _trades(db)[0]["entry_price"] == 4002.0

    def test_signal_too_old_to_enter_is_skipped_without_fetching(self, db, cfg):
        _start(db, cfg)
        _spike(db, minutes_ago=cfg.max_entry_delay_minutes + 1)
        fetcher = FakeFetcher({"GC=F": 4001.0})
        open_trades(db, cfg, fetcher, NOW)
        [trade] = _trades(db)
        assert trade["status"] == "skipped"
        assert trade["note"] == "no entry price within 15 min"
        assert fetcher.calls == []

    def test_price_fetched_once_per_instrument_per_pass(self, db, cfg):
        _start(db, cfg)
        _spike(db, minutes_ago=4)
        _spike(db, minutes_ago=2)
        fetcher = FakeFetcher({"GC=F": 4001.0})
        open_trades(db, cfg, fetcher, NOW)
        assert fetcher.calls == [("futures_gold", "GC=F")]

    def test_returns_counts(self, db, cfg):
        _start(db, cfg)
        _spike(db, minutes_ago=4)
        _spike(db, minutes_ago=2)
        result = open_trades(db, cfg, FakeFetcher({"GC=F": 4001.0}), NOW)
        assert result == {"opened": 1, "skipped": 1}


# ---------------------------------------------------------------------------
# close_due_trades() — exit after the horizon, record gross/net, or expire
# ---------------------------------------------------------------------------


def _open_one(db, cfg, change=0.2, entry=4000.0):
    """Open a gold trade at NOW and return its row."""
    _start(db, cfg)
    _spike(db, change=change)
    open_trades(db, cfg, FakeFetcher({"GC=F": entry}), NOW)
    return _trades(db)[0]


class TestCloseDueTrades:
    def test_short_with_falling_price_closes_with_positive_net(self, db, cfg):
        trade = _open_one(db, cfg, change=0.2, entry=4000.0)
        exit_at = NOW + timedelta(minutes=62)
        result = close_due_trades(db, cfg, FakeFetcher({"GC=F": 3990.0}), exit_at)
        [closed] = _trades(db)
        assert result == {"closed": 1, "expired": 0}
        assert closed["status"] == "closed"
        assert closed["exit_price"] == 3990.0
        assert closed["exit_at"] == exit_at.isoformat()
        assert closed["gross_ret"] == pytest.approx(10 / 4000)
        assert closed["net_ret"] == pytest.approx(10 / 4000 - trade["cost_ret"])

    def test_long_with_falling_price_loses(self, db, cfg):
        _open_one(db, cfg, change=-0.2, entry=4000.0)
        close_due_trades(db, cfg, FakeFetcher({"GC=F": 3990.0}), NOW + timedelta(minutes=60))
        assert _trades(db)[0]["gross_ret"] == pytest.approx(-10 / 4000)

    def test_not_closed_before_horizon(self, db, cfg):
        _open_one(db, cfg)
        fetcher = FakeFetcher({"GC=F": 3990.0})
        result = close_due_trades(db, cfg, fetcher, NOW + timedelta(minutes=59))
        assert result == {"closed": 0, "expired": 0}
        assert _trades(db)[0]["status"] == "open"
        assert fetcher.calls == []

    def test_no_price_within_grace_stays_open(self, db, cfg):
        _open_one(db, cfg)
        close_due_trades(db, cfg, FakeFetcher({}), NOW + timedelta(minutes=60 + 29))
        assert _trades(db)[0]["status"] == "open"

    def test_no_price_past_grace_expires_with_null_pnl(self, db, cfg):
        _open_one(db, cfg)
        at = NOW + timedelta(minutes=60 + 31)
        result = close_due_trades(db, cfg, FakeFetcher({}), at)
        [trade] = _trades(db)
        assert result == {"closed": 0, "expired": 1}
        assert trade["status"] == "expired"
        assert trade["exit_at"] == at.isoformat()
        assert (trade["exit_price"], trade["gross_ret"], trade["net_ret"]) == (None, None, None)
        assert trade["note"] == "no exit price within 30 min of due"

    def test_one_fetch_per_instrument(self, db, cfg):
        _start(db, cfg)
        _spike(db, minutes_ago=2)
        _spike(db, minutes_ago=1, ticker="CL=F", source="futures_oil", price=60.0)
        open_trades(db, cfg, FakeFetcher({"GC=F": 4000.0, "CL=F": 60.0}), NOW)
        fetcher = FakeFetcher({"GC=F": 3990.0, "CL=F": 61.0})
        assert close_due_trades(db, cfg, fetcher, NOW + timedelta(minutes=61))["closed"] == 2
        assert sorted(fetcher.calls) == [("futures_gold", "GC=F"), ("futures_oil", "CL=F")]

    def test_closed_position_frees_instrument_for_later_signal(self, db, cfg):
        _open_one(db, cfg)
        close_due_trades(db, cfg, FakeFetcher({"GC=F": 3990.0}), NOW + timedelta(minutes=61))
        later = NOW + timedelta(minutes=70)
        db.insert_signal("futures_gold", "volume_spike", "HIGH",
                         {"ticker": "GC=F", "price": 3991.0, "price_change_pct": -0.1},
                         "spike", created_at=(later - timedelta(minutes=1)).isoformat())
        open_trades(db, cfg, FakeFetcher({"GC=F": 3992.0}), later)
        assert [t["status"] for t in _trades(db)] == ["closed", "open"]
