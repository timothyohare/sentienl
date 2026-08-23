"""
Unit tests for scripts/price_followup.py.

Price fetchers are mocked at the protocol boundary (same pattern as
truth_social.py's TruthSocialClientProtocol) — no live network in tests.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from sentinel.core.db import Database
from sentinel.scripts.price_followup import (
    AsxPriceFetcher,
    CompositePriceFetcher,
    FuturesPriceFetcher,
    due_updates,
    group_by_instrument,
    random_window_effect_sizes,
    run_once,
    sample_baseline_prices,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.init()
    yield db
    db.close()


class FakeFetcher:
    """Records calls and returns a canned price per (source, instrument)."""

    def __init__(self, prices: dict[tuple[str, str], float]):
        self._prices = prices
        self.calls: list[tuple[str, str]] = []

    def get_price(self, source, instrument):
        self.calls.append((source, instrument))
        return self._prices.get((source, instrument))


def _iso(minutes_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


# ---------------------------------------------------------------------------
# due_updates() — pure horizon-threshold logic
# ---------------------------------------------------------------------------

class TestDueUpdates:
    def test_column_due_when_horizon_elapsed(self):
        now = datetime.now(UTC)
        row = {
            "signal_id": 1, "source": "kalshi", "instrument": "T1",
            "created_at": (now - timedelta(minutes=20)).isoformat(),
            "price_t15": None, "price_t60": None, "price_t240": None, "price_t1440": None,
        }
        due = due_updates([row], now)
        columns = {c for _, c in due}
        assert columns == {"price_t15"}  # 20min elapsed: only t15 (15min) is due

    def test_column_not_due_before_horizon(self):
        now = datetime.now(UTC)
        row = {
            "signal_id": 1, "source": "kalshi", "instrument": "T1",
            "created_at": (now - timedelta(minutes=5)).isoformat(),
            "price_t15": None, "price_t60": None, "price_t240": None, "price_t1440": None,
        }
        due = due_updates([row], now)
        assert due == []

    def test_already_filled_column_not_due(self):
        now = datetime.now(UTC)
        row = {
            "signal_id": 1, "source": "kalshi", "instrument": "T1",
            "created_at": (now - timedelta(minutes=100)).isoformat(),
            "price_t15": 0.5, "price_t60": None, "price_t240": None, "price_t1440": None,
        }
        due = due_updates([row], now)
        columns = {c for _, c in due}
        assert columns == {"price_t60"}  # t15 already filled, skip it

    def test_multiple_horizons_due_at_once(self):
        now = datetime.now(UTC)
        row = {
            "signal_id": 1, "source": "kalshi", "instrument": "T1",
            "created_at": (now - timedelta(minutes=300)).isoformat(),
            "price_t15": None, "price_t60": None, "price_t240": None, "price_t1440": None,
        }
        due = due_updates([row], now)
        columns = {c for _, c in due}
        assert columns == {"price_t15", "price_t60", "price_t240"}

    def test_missing_created_at_skipped_without_error(self):
        now = datetime.now(UTC)
        row = {"signal_id": 1, "source": "kalshi", "instrument": "T1"}
        assert due_updates([row], now) == []


# ---------------------------------------------------------------------------
# group_by_instrument() — batching
# ---------------------------------------------------------------------------

class TestGroupByInstrument:
    def test_groups_same_instrument_together(self):
        row = {"signal_id": 1, "source": "kalshi", "instrument": "T1"}
        due = [(row, "price_t15"), (row, "price_t60")]
        groups = group_by_instrument(due)
        assert groups == {("kalshi", "T1"): due}

    def test_different_instruments_separate_groups(self):
        row1 = {"signal_id": 1, "source": "kalshi", "instrument": "T1"}
        row2 = {"signal_id": 2, "source": "kalshi", "instrument": "T2"}
        due = [(row1, "price_t15"), (row2, "price_t15")]
        groups = group_by_instrument(due)
        assert set(groups.keys()) == {("kalshi", "T1"), ("kalshi", "T2")}


# ---------------------------------------------------------------------------
# random_window_effect_sizes() — true random-window baseline math (pure)
# ---------------------------------------------------------------------------

class TestRandomWindowEffectSizes:
    def test_pair_at_exact_horizon_included(self):
        samples = [
            {"price": 100.0, "sampled_at": _iso(30)},
            {"price": 110.0, "sampled_at": _iso(15)},  # 15min gap
        ]
        sizes = random_window_effect_sizes(samples, horizon_minutes=15)
        assert sizes == pytest.approx([0.10])

    def test_pair_outside_tolerance_excluded(self):
        samples = [
            {"price": 100.0, "sampled_at": _iso(60)},
            {"price": 110.0, "sampled_at": _iso(15)},  # 45min gap, horizon=15 tolerance=3
        ]
        assert random_window_effect_sizes(samples, horizon_minutes=15) == []

    def test_gap_below_lower_bound_excluded(self):
        samples = [
            {"price": 100.0, "sampled_at": _iso(20)},
            {"price": 110.0, "sampled_at": _iso(15)},  # 5min gap, horizon=15 tolerance=3
        ]
        assert random_window_effect_sizes(samples, horizon_minutes=15) == []

    def test_multiple_pairs_from_three_samples(self):
        samples = [
            {"price": 100.0, "sampled_at": _iso(30)},
            {"price": 105.0, "sampled_at": _iso(15)},  # 15min gap from sample 0
            {"price": 110.0, "sampled_at": _iso(0)},   # 15min from sample 1, 30min from sample 0
        ]
        sizes = random_window_effect_sizes(samples, horizon_minutes=15)
        # (0,1): |105-100|/100=0.05  (1,2): |110-105|/105≈0.0476  (0,2): 30min gap excluded
        assert sizes == pytest.approx([0.05, 5.0 / 105])

    def test_single_sample_returns_empty(self):
        assert random_window_effect_sizes([{"price": 100.0, "sampled_at": _iso(0)}], 15) == []

    def test_empty_samples_returns_empty(self):
        assert random_window_effect_sizes([], 15) == []

    def test_custom_tolerance(self):
        samples = [
            {"price": 100.0, "sampled_at": _iso(70)},
            {"price": 110.0, "sampled_at": _iso(0)},  # 70min gap, horizon=60
        ]
        assert random_window_effect_sizes(samples, horizon_minutes=60, tolerance_minutes=5) == []
        assert random_window_effect_sizes(
            samples, horizon_minutes=60, tolerance_minutes=15
        ) == pytest.approx([0.10])


# ---------------------------------------------------------------------------
# sample_baseline_prices() — continuous baseline sampling
# ---------------------------------------------------------------------------

class TestSampleBaselinePrices:
    def test_samples_every_distinct_tracked_instrument(self, mock_db):
        sid1 = mock_db.insert_signal(
            source="kalshi", signal_type="large_bet", priority="HIGH", payload={}, summary="x",
        )
        sid2 = mock_db.insert_signal(
            source="futures_gold", signal_type="volume_spike", priority="HIGH",
            payload={}, summary="y",
        )
        mock_db.price_tracking.insert(sid1, "kalshi", "T1", price_t0=0.30)
        mock_db.price_tracking.insert(sid2, "futures_gold", "GC=F", price_t0=2400.0)

        fetcher = FakeFetcher({("kalshi", "T1"): 0.35, ("futures_gold", "GC=F"): 2410.0})
        sampled = sample_baseline_prices(mock_db, fetcher)

        assert sampled == 2
        assert set(fetcher.calls) == {("kalshi", "T1"), ("futures_gold", "GC=F")}
        assert [s["price"] for s in mock_db.price_samples.get_samples("kalshi", "T1")] == [0.35]

    def test_no_tracked_instruments_makes_no_calls(self, mock_db):
        fetcher = FakeFetcher({})
        assert sample_baseline_prices(mock_db, fetcher) == 0
        assert fetcher.calls == []

    def test_missing_price_is_skipped_without_error(self, mock_db):
        sid = mock_db.insert_signal(
            source="kalshi", signal_type="large_bet", priority="HIGH", payload={}, summary="x",
        )
        mock_db.price_tracking.insert(sid, "kalshi", "T1", price_t0=0.30)

        fetcher = FakeFetcher({})  # no price available
        sampled = sample_baseline_prices(mock_db, fetcher)

        assert sampled == 0
        assert mock_db.price_samples.get_samples("kalshi", "T1") == []


# ---------------------------------------------------------------------------
# run_once() — integration against a real (tmp) db, fake fetcher
# ---------------------------------------------------------------------------

class TestRunOnce:
    def test_updates_due_column(self, mock_db):
        signal_id = mock_db.insert_signal(
            source="kalshi", signal_type="large_bet", priority="HIGH",
            payload={}, summary="x",
        )
        mock_db.price_tracking.insert(signal_id, "kalshi", "T1", price_t0=0.30)
        mock_db.execute(
            "UPDATE post_price_tracking SET created_at=? WHERE signal_id=?",
            (_iso(20), signal_id),
        )

        fetcher = FakeFetcher({("kalshi", "T1"): 0.35})
        updated = run_once(mock_db, fetcher)

        assert updated == 1
        rows = mock_db.execute_fetchall(
            "SELECT price_t15 FROM post_price_tracking WHERE signal_id=?", (signal_id,)
        )
        assert rows[0]["price_t15"] == 0.35

    def test_no_pending_rows_makes_no_fetch_calls(self, mock_db):
        fetcher = FakeFetcher({})
        updated = run_once(mock_db, fetcher)
        assert updated == 0
        assert fetcher.calls == []

    def test_fetches_once_per_instrument_across_multiple_due_columns(self, mock_db):
        signal_id = mock_db.insert_signal(
            source="kalshi", signal_type="large_bet", priority="HIGH",
            payload={}, summary="x",
        )
        mock_db.price_tracking.insert(signal_id, "kalshi", "T1", price_t0=0.30)
        mock_db.execute(
            "UPDATE post_price_tracking SET created_at=? WHERE signal_id=?",
            (_iso(300), signal_id),  # t15, t60, t240 all due
        )

        fetcher = FakeFetcher({("kalshi", "T1"): 0.40})
        updated = run_once(mock_db, fetcher)

        assert updated == 3
        assert fetcher.calls == [("kalshi", "T1")]  # one fetch, not three

    def test_fetches_once_per_instrument_across_multiple_rows(self, mock_db):
        sid1 = mock_db.insert_signal(
            source="kalshi", signal_type="large_bet", priority="HIGH",
            payload={}, summary="x",
        )
        sid2 = mock_db.insert_signal(
            source="kalshi", signal_type="large_bet", priority="HIGH",
            payload={}, summary="y",
        )
        mock_db.price_tracking.insert(sid1, "kalshi", "T1", price_t0=0.30)
        mock_db.price_tracking.insert(sid2, "kalshi", "T1", price_t0=0.32)
        for sid in (sid1, sid2):
            mock_db.execute(
                "UPDATE post_price_tracking SET created_at=? WHERE signal_id=?",
                (_iso(20), sid),
            )

        fetcher = FakeFetcher({("kalshi", "T1"): 0.40})
        updated = run_once(mock_db, fetcher)

        assert updated == 2
        assert fetcher.calls == [("kalshi", "T1")]

    def test_missing_price_skips_that_instrument_without_error(self, mock_db):
        signal_id = mock_db.insert_signal(
            source="kalshi", signal_type="large_bet", priority="HIGH",
            payload={}, summary="x",
        )
        mock_db.price_tracking.insert(signal_id, "kalshi", "T1", price_t0=0.30)
        mock_db.execute(
            "UPDATE post_price_tracking SET created_at=? WHERE signal_id=?",
            (_iso(20), signal_id),
        )

        fetcher = FakeFetcher({})  # no price available
        updated = run_once(mock_db, fetcher)

        assert updated == 0
        rows = mock_db.execute_fetchall(
            "SELECT price_t15 FROM post_price_tracking WHERE signal_id=?", (signal_id,)
        )
        assert rows[0]["price_t15"] is None


# ---------------------------------------------------------------------------
# FuturesPriceFetcher — IB Gateway (real-time) ahead of yfinance (delayed)
# ---------------------------------------------------------------------------

class TestFuturesPriceFetcher:
    def test_ignores_non_futures_sources(self):
        fetcher = FuturesPriceFetcher(ib_enabled=True)
        with patch.object(fetcher, "_get_price_ibkr") as mock_ib:
            assert fetcher.get_price("kalshi", "T1") is None
        mock_ib.assert_not_called()

    def test_ib_skipped_when_disabled(self):
        fetcher = FuturesPriceFetcher(ib_enabled=False)
        with (
            patch.object(fetcher, "_get_price_ibkr") as mock_ib,
            patch.object(fetcher, "_get_price_yfinance", return_value=75.0),
        ):
            price = fetcher.get_price("futures_oil", "CL=F")
        mock_ib.assert_not_called()
        assert price == 75.0

    def test_ib_used_when_enabled_and_it_returns_a_price(self):
        fetcher = FuturesPriceFetcher(ib_enabled=True)
        with (
            patch.object(fetcher, "_get_price_ibkr", return_value=76.5) as mock_ib,
            patch.object(fetcher, "_get_price_yfinance") as mock_yf,
        ):
            price = fetcher.get_price("futures_oil", "CL=F")
        mock_ib.assert_called_once()
        mock_yf.assert_not_called()
        assert price == 76.5

    def test_falls_back_to_yfinance_when_ib_returns_none(self):
        fetcher = FuturesPriceFetcher(ib_enabled=True)
        with (
            patch.object(fetcher, "_get_price_ibkr", return_value=None),
            patch.object(fetcher, "_get_price_yfinance", return_value=75.0) as mock_yf,
        ):
            price = fetcher.get_price("futures_oil", "CL=F")
        mock_yf.assert_called_once()
        assert price == 75.0


# ---------------------------------------------------------------------------
# AsxPriceFetcher — IB Gateway (unconfirmed entitlement) ahead of yfinance
# ---------------------------------------------------------------------------

class TestAsxPriceFetcher:
    def test_ignores_non_asx_sources(self):
        fetcher = AsxPriceFetcher(ib_enabled=True)
        with patch.object(fetcher, "_get_price_ibkr") as mock_ib:
            assert fetcher.get_price("futures_oil", "CL=F") is None
        mock_ib.assert_not_called()

    def test_ib_skipped_when_disabled(self):
        fetcher = AsxPriceFetcher(ib_enabled=False)
        with (
            patch.object(fetcher, "_get_price_ibkr") as mock_ib,
            patch.object(fetcher, "_get_price_yfinance", return_value=45.0),
        ):
            price = fetcher.get_price("asx", "BHP.AX")
        mock_ib.assert_not_called()
        assert price == 45.0

    def test_ib_used_when_enabled_and_it_returns_a_price(self):
        fetcher = AsxPriceFetcher(ib_enabled=True)
        with (
            patch.object(fetcher, "_get_price_ibkr", return_value=46.5) as mock_ib,
            patch.object(fetcher, "_get_price_yfinance") as mock_yf,
        ):
            price = fetcher.get_price("asx", "BHP.AX")
        mock_ib.assert_called_once()
        mock_yf.assert_not_called()
        assert price == 46.5

    def test_falls_back_to_yfinance_when_ib_returns_none(self):
        fetcher = AsxPriceFetcher(ib_enabled=True)
        with (
            patch.object(fetcher, "_get_price_ibkr", return_value=None),
            patch.object(fetcher, "_get_price_yfinance", return_value=45.0) as mock_yf,
        ):
            price = fetcher.get_price("asx", "BHP.AX")
        mock_yf.assert_called_once()
        assert price == 45.0


# ---------------------------------------------------------------------------
# CompositePriceFetcher — dispatch by source
# ---------------------------------------------------------------------------

class TestCompositePriceFetcher:
    def test_dispatches_to_first_fetcher_that_returns_a_price(self):
        class Nope:
            def get_price(self, source, instrument):
                return None

        class Yep:
            def get_price(self, source, instrument):
                return 42.0

        fetcher = CompositePriceFetcher([Nope(), Yep()])
        assert fetcher.get_price("kalshi", "T1") == 42.0

    def test_returns_none_if_no_fetcher_matches(self):
        class Nope:
            def get_price(self, source, instrument):
                return None

        fetcher = CompositePriceFetcher([Nope(), Nope()])
        assert fetcher.get_price("kalshi", "T1") is None
