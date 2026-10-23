"""
Unit tests for scripts/healthcheck.py.

Unit liveness is faked at the UnitCheckerProtocol boundary (same pattern
as kalshi_liquidity_check.py's MarketsFetcherProtocol) — no real systemctl
call in tests. Uses a real tmp_path-backed Database, same convention as
test_db.py, since check_health() queries `signals` directly.
"""

from datetime import UTC, datetime, timedelta

import pytest

from sentinel.core.db import Database
from sentinel.scripts.healthcheck import (
    MONITORED_UNITS,
    alerter_backlog,
    check_health,
    ib_fallbacks,
    should_send_heartbeat,
)


class FakeUnitChecker:
    """Returns canned liveness per unit name; defaults to active."""

    def __init__(self, active: dict[str, bool] | None = None):
        self._active = active or {}

    def is_active(self, unit: str) -> bool:
        return self._active.get(unit, True)


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test_sentinel.db"))
    database.init()
    yield database
    database.close()


def _insert_signal(db: Database, source: str, created_at: datetime):
    db.insert_signal(
        source=source,
        signal_type="test",
        priority="MEDIUM",
        summary="test",
        payload={},
        created_at=created_at.isoformat(),
    )


class TestCheckHealth:
    def test_down_when_unit_not_active_regardless_of_signals(self, db):
        _insert_signal(db, "kalshi", datetime.now(UTC))
        checker = FakeUnitChecker({"sentinel-kalshi.service": False})

        results = check_health(db, stale_threshold_minutes=30, unit_checker=checker)

        assert results["kalshi"]["status"] == "DOWN"
        assert results["kalshi"]["unit_active"] is False

    def test_ok_when_unit_active_and_recent_signal(self, db):
        _insert_signal(db, "kalshi", datetime.now(UTC))
        checker = FakeUnitChecker()

        results = check_health(db, stale_threshold_minutes=30, unit_checker=checker)

        assert results["kalshi"]["status"] == "OK"

    def test_quiet_when_unit_active_but_no_recent_signal(self, db):
        checker = FakeUnitChecker()

        results = check_health(db, stale_threshold_minutes=30, unit_checker=checker)

        assert results["kalshi"]["status"] == "QUIET"
        assert results["kalshi"]["signals_in_window"] == 0

    def test_stale_signal_outside_window_is_quiet_not_ok(self, db):
        _insert_signal(db, "kalshi", datetime.now(UTC) - timedelta(minutes=45))
        checker = FakeUnitChecker()

        results = check_health(db, stale_threshold_minutes=30, unit_checker=checker)

        assert results["kalshi"]["status"] == "QUIET"

    def test_checks_every_monitored_source(self, db):
        checker = FakeUnitChecker()

        results = check_health(db, stale_threshold_minutes=30, unit_checker=checker)

        assert set(results.keys()) == set(MONITORED_UNITS.keys())

    def test_down_takes_priority_over_having_a_recent_signal(self, db):
        for source in MONITORED_UNITS:
            _insert_signal(db, source, datetime.now(UTC))
        checker = FakeUnitChecker({unit: False for unit in MONITORED_UNITS.values()})

        results = check_health(db, stale_threshold_minutes=30, unit_checker=checker)

        assert all(r["status"] == "DOWN" for r in results.values())


class TestShouldSendHeartbeat:
    def test_down_always_sends_regardless_of_hour(self):
        # 03:00 UTC = 13:00 AEST, not in {8, 20}
        now_utc = datetime(2026, 8, 12, 3, 0, tzinfo=UTC)

        assert should_send_heartbeat(True, {8, 20}, now_utc) is True

    def test_healthy_sends_only_during_scheduled_aest_hour(self):
        # 22:00 UTC = 08:00 AEST next day — inside the schedule
        now_utc = datetime(2026, 8, 11, 22, 0, tzinfo=UTC)

        assert should_send_heartbeat(False, {8, 20}, now_utc) is True

    def test_healthy_suppressed_outside_scheduled_aest_hour(self):
        # 03:00 UTC = 13:00 AEST — outside the schedule
        now_utc = datetime(2026, 8, 12, 3, 0, tzinfo=UTC)

        assert should_send_heartbeat(False, {8, 20}, now_utc) is False


class TestAlerterBacklog:
    """Signals still alerted=0 long after insert mean the alerter is stuck
    (2026-09-28 -> 10-14: 3,754 signals, unnoticed because alerts were off)."""

    def test_old_unalerted_signal_counts(self, db):
        _insert_signal(db, "kalshi", datetime.now(UTC) - timedelta(minutes=30))
        assert alerter_backlog(db, older_than_minutes=10) == 1

    def test_recent_unalerted_signal_does_not_count(self, db):
        _insert_signal(db, "kalshi", datetime.now(UTC) - timedelta(minutes=2))
        assert alerter_backlog(db, older_than_minutes=10) == 0

    def test_alerted_signal_does_not_count(self, db):
        _insert_signal(db, "kalshi", datetime.now(UTC) - timedelta(minutes=30))
        signal_id = db.execute_scalar("SELECT MAX(id) FROM signals")
        db.mark_alerted(signal_id)
        assert alerter_backlog(db, older_than_minutes=10) == 0


class TestIbFallbacks:
    def _set(self, db, key, minutes_ago):
        db.state.set(key, (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat())

    def test_recent_fallback_with_no_ib_success_flags(self, db):
        self._set(db, "ib_fallback:futures:CL=F", 5)
        assert ib_fallbacks(db, window_minutes=30) == ["futures:CL=F"]

    def test_fallback_older_than_last_ib_success_is_fine(self, db):
        self._set(db, "ib_fallback:futures:CL=F", 10)
        self._set(db, "ib_ok:futures:CL=F", 2)
        assert ib_fallbacks(db, window_minutes=30) == []

    def test_fallback_newer_than_last_ib_success_flags(self, db):
        self._set(db, "ib_ok:asx:BHP.AX", 20)
        self._set(db, "ib_fallback:asx:BHP.AX", 1)
        assert ib_fallbacks(db, window_minutes=30) == ["asx:BHP.AX"]

    def test_fallback_outside_window_is_ignored(self, db):
        self._set(db, "ib_fallback:futures:CL=F", 120)
        assert ib_fallbacks(db, window_minutes=30) == []
