"""
Unit tests for scripts/price_followup.py.

Price fetchers are mocked at the protocol boundary (same pattern as
truth_social.py's TruthSocialClientProtocol) — no live network in tests.
"""

from datetime import UTC, datetime, timedelta

import pytest

from sentinel.core.db import Database
from sentinel.scripts.price_followup import (
    CompositePriceFetcher,
    due_updates,
    group_by_instrument,
    run_once,
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
