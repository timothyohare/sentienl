"""Unit tests for collectors/correlation_detector.py."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from sentinel.collectors.correlation_detector import CorrelationDetector
from sentinel.core.db import Database

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.init()
    yield db
    db.close()


@pytest.fixture
def mock_config():
    cfg = MagicMock()
    cfg.alerts.ntfy_topic = "sentinel-test"
    return cfg


@pytest.fixture
def detector(mock_config, mock_db):
    return CorrelationDetector(config=mock_config, db=mock_db, window_minutes=10)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INSERT_SIGNAL_SQL = (
    "INSERT INTO signals "
    "(source, signal_type, priority, payload, summary, alerted, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


def insert_signal_now(db, source, priority="HIGH"):
    now = datetime.now(UTC).isoformat()
    return db.execute(
        _INSERT_SIGNAL_SQL,
        (source, "volume_spike", priority, "{}", f"Signal from {source}", 0, now),
    ).lastrowid


def insert_signal_at(db, source, created_at, priority="HIGH"):
    """Insert a signal at an explicit ISO8601 timestamp."""
    return db.execute(
        _INSERT_SIGNAL_SQL,
        (source, "volume_spike", priority, "{}", f"Signal from {source}", 0, created_at),
    ).lastrowid


# ---------------------------------------------------------------------------
# Correlation detection
# ---------------------------------------------------------------------------

class TestCorrelationDetection:
    def test_no_signals_no_correlation(self, detector):
        result = detector.check_correlation()
        assert result is False

    def test_single_source_no_correlation(self, detector, mock_db):
        insert_signal_now(mock_db, "truth_social")
        insert_signal_now(mock_db, "truth_social")
        mock_db._conn.commit()
        result = detector.check_correlation()
        assert result is False

    def test_two_sources_triggers_correlation(self, detector, mock_db):
        insert_signal_now(mock_db, "truth_social", "HIGH")
        insert_signal_now(mock_db, "futures_oil", "HIGH")
        mock_db._conn.commit()
        result = detector.check_correlation()
        assert result is True

    def test_three_sources_triggers_correlation(self, detector, mock_db):
        insert_signal_now(mock_db, "truth_social", "CRITICAL")
        insert_signal_now(mock_db, "futures_oil", "HIGH")
        insert_signal_now(mock_db, "polymarket", "HIGH")
        mock_db._conn.commit()
        result = detector.check_correlation()
        assert result is True

    def test_low_priority_signals_not_counted(self, detector, mock_db):
        insert_signal_now(mock_db, "truth_social", "LOW")
        insert_signal_now(mock_db, "futures_oil", "INFO")
        mock_db._conn.commit()
        result = detector.check_correlation()
        assert result is False

    def test_stale_signal_outside_window_not_correlated(self, detector, mock_db):
        """A second-source signal hours earlier must NOT correlate with a recent
        anchor (regression for the T-vs-space window bug that pulled in the whole
        previous UTC day)."""
        from datetime import timedelta
        now = datetime.now(UTC)
        # Anchor: recent. Other source: ~3 hours earlier (well outside 10-min window)
        insert_signal_at(mock_db, "truth_social", now.isoformat(), "HIGH")
        insert_signal_at(
            mock_db, "futures_oil",
            (now - timedelta(hours=3)).isoformat(), "HIGH",
        )
        mock_db._conn.commit()
        result = detector.check_correlation()
        assert result is False

    def test_detector_does_not_correlate_on_its_own_output(self, detector, mock_db):
        """A prior correlated_signal (CRITICAL) must not count as a source and
        seed a feedback loop."""
        insert_signal_now(mock_db, "truth_social", "HIGH")
        insert_signal_now(mock_db, "correlation_detector", "CRITICAL")
        mock_db._conn.commit()
        # Only one *real* source (truth_social) — should not correlate
        result = detector.check_correlation()
        assert result is False


# ---------------------------------------------------------------------------
# Signal creation
# ---------------------------------------------------------------------------

class TestCorrelatedSignalCreation:
    def test_correlated_signal_written_to_db(self, detector, mock_db):
        insert_signal_now(mock_db, "truth_social", "HIGH")
        insert_signal_now(mock_db, "futures_oil", "HIGH")
        mock_db._conn.commit()
        detector.check_and_signal()
        signals = mock_db.get_recent_signals()
        correlated = [s for s in signals if s["signal_type"] == "correlated_signal"]
        assert len(correlated) >= 1

    def test_correlated_signal_is_critical(self, detector, mock_db):
        insert_signal_now(mock_db, "truth_social", "HIGH")
        insert_signal_now(mock_db, "polymarket", "HIGH")
        mock_db._conn.commit()
        detector.check_and_signal()
        signals = mock_db.get_recent_signals()
        correlated = [s for s in signals if s["signal_type"] == "correlated_signal"]
        assert all(s["priority"] == "CRITICAL" for s in correlated)

    def test_no_duplicate_correlated_signals(self, detector, mock_db):
        insert_signal_now(mock_db, "truth_social", "HIGH")
        insert_signal_now(mock_db, "futures_oil", "HIGH")
        mock_db._conn.commit()
        detector.check_and_signal()
        detector.check_and_signal()  # second call — should not create duplicate
        signals = mock_db.get_recent_signals()
        correlated = [s for s in signals if s["signal_type"] == "correlated_signal"]
        assert len(correlated) == 1


# ---------------------------------------------------------------------------
# Check interval
# ---------------------------------------------------------------------------

class TestCheckInterval:
    def test_default_check_interval(self, detector):
        assert detector.check_interval_seconds == 300  # 5 minutes

    def test_custom_check_interval(self, mock_config, mock_db):
        d = CorrelationDetector(config=mock_config, db=mock_db, check_interval_seconds=60)
        assert d.check_interval_seconds == 60


# ---------------------------------------------------------------------------
# Window boundary
# ---------------------------------------------------------------------------

class TestCooldownParity:
    def test_check_correlation_agrees_with_check_and_signal_during_cooldown(
        self, detector, mock_db
    ):
        """check_correlation() must apply the same cooldown as check_and_signal()
        — otherwise the two disagree (read says 'correlation!' while the writer
        suppresses it)."""
        insert_signal_now(mock_db, "truth_social", "HIGH")
        insert_signal_now(mock_db, "futures_oil", "HIGH")
        mock_db._conn.commit()
        detector.check_and_signal()  # first fire arms the cooldown

        # A third source arrives inside the cooldown window — a brand-new anchor
        # that check_and_signal() would suppress via the cooldown.
        insert_signal_now(mock_db, "polymarket", "HIGH")
        mock_db._conn.commit()

        # Both views must agree: nothing new is reported or fired.
        assert detector.check_correlation() is False
        detector.check_and_signal()
        correlated = [
            s for s in mock_db.get_recent_signals()
            if s["signal_type"] == "correlated_signal"
        ]
        assert len(correlated) == 1


class TestWindowBoundary:
    def test_correlates_at_exact_window_boundary(self, detector, mock_db):
        """Two sources exactly window_minutes apart still correlate (the SQL
        bound is inclusive: datetime(s2) <= datetime(anchor, '+N minutes'))."""
        from datetime import timedelta
        now = datetime.now(UTC)
        insert_signal_at(
            mock_db, "truth_social",
            (now - timedelta(minutes=10)).isoformat(), "HIGH",
        )
        insert_signal_at(mock_db, "futures_oil", now.isoformat(), "HIGH")
        mock_db._conn.commit()
        assert detector.check_correlation() is True


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------

class TestInit:
    def test_default_window_minutes(self, mock_config, mock_db):
        d = CorrelationDetector(config=mock_config, db=mock_db)
        assert d.window_minutes == 10

    def test_custom_window_minutes(self, mock_config, mock_db):
        d = CorrelationDetector(config=mock_config, db=mock_db, window_minutes=25)
        assert d.window_minutes == 25

    def test_config_is_stored_by_identity(self, mock_config, mock_db):
        d = CorrelationDetector(config=mock_config, db=mock_db)
        assert d.config is mock_config

    def test_fired_on_anchors_starts_empty(self, detector):
        assert detector._fired_on_anchors == set()

    def test_last_fired_time_loaded_from_state_key(self, mock_config, mock_db):
        anchor_dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_db.state.set("correlation_last_fired_window", anchor_dt.isoformat())
        d = CorrelationDetector(config=mock_config, db=mock_db)
        assert d._last_fired_time == anchor_dt

    def test_last_fired_time_none_when_state_empty(self, detector):
        assert detector._last_fired_time is None


# ---------------------------------------------------------------------------
# _parse_dt
# ---------------------------------------------------------------------------

class TestParseDt:
    def test_empty_string_returns_none(self):
        assert CorrelationDetector._parse_dt("") is None

    def test_malformed_string_returns_none(self):
        assert CorrelationDetector._parse_dt("not-a-timestamp") is None

    def test_naive_timestamp_gets_utc_tzinfo(self):
        result = CorrelationDetector._parse_dt("2026-01-01T12:00:00")
        assert result == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        assert result.tzinfo is UTC

    def test_aware_timestamp_preserves_offset(self):
        result = CorrelationDetector._parse_dt("2026-01-01T12:00:00+00:00")
        assert result == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# _is_fireable
# ---------------------------------------------------------------------------

class TestIsFireable:
    def test_already_fired_anchor_not_fireable(self, detector):
        detector._fired_on_anchors.add(42)
        assert detector._is_fireable({"anchor_id": 42, "anchor_time": ""}) is False

    def test_new_anchor_no_prior_fire_is_fireable(self, detector):
        assert detector._last_fired_time is None
        window = {"anchor_id": 1, "anchor_time": datetime.now(UTC).isoformat()}
        assert detector._is_fireable(window) is True

    def test_unparseable_anchor_time_bypasses_cooldown(self, detector):
        detector._last_fired_time = datetime.now(UTC)
        window = {"anchor_id": 1, "anchor_time": "garbage"}
        assert detector._is_fireable(window) is True

    def test_inside_cooldown_not_fireable(self, detector):
        from datetime import timedelta
        now = datetime.now(UTC)
        detector._last_fired_time = now
        window = {
            "anchor_id": 1,
            "anchor_time": (now + timedelta(minutes=5)).isoformat(),
        }
        assert detector._is_fireable(window) is False

    def test_exactly_at_cooldown_boundary_not_fireable(self, detector):
        from datetime import timedelta
        now = datetime.now(UTC)
        detector._last_fired_time = now
        window = {
            "anchor_id": 1,
            "anchor_time": (now + timedelta(minutes=detector.window_minutes)).isoformat(),
        }
        assert detector._is_fireable(window) is False

    def test_just_outside_cooldown_boundary_is_fireable(self, detector):
        from datetime import timedelta
        now = datetime.now(UTC)
        detector._last_fired_time = now
        window = {
            "anchor_id": 1,
            "anchor_time": (
                now + timedelta(minutes=detector.window_minutes, seconds=1)
            ).isoformat(),
        }
        assert detector._is_fireable(window) is True

    def test_anchor_before_last_fired_uses_absolute_diff(self, detector):
        """A window with anchor_time earlier than _last_fired_time still
        respects the cooldown (abs() of the difference, not raw subtraction)."""
        from datetime import timedelta
        now = datetime.now(UTC)
        detector._last_fired_time = now
        window = {
            "anchor_id": 1,
            "anchor_time": (now - timedelta(minutes=5)).isoformat(),
        }
        assert detector._is_fireable(window) is False


# ---------------------------------------------------------------------------
# check_and_signal — exact call arguments (mocked db)
# ---------------------------------------------------------------------------

class TestCheckAndSignalMocked:
    @pytest.fixture
    def mocked_detector(self, mock_config):
        db = MagicMock()
        db.state.get.return_value = None
        d = CorrelationDetector(config=mock_config, db=db, window_minutes=10)
        return d, db

    def test_no_windows_does_not_insert(self, mocked_detector):
        d, db = mocked_detector
        db.get_correlated_signals_in_window.return_value = []
        d.check_and_signal()
        db.insert_signal.assert_not_called()

    def test_fireable_window_inserts_with_exact_payload(self, mocked_detector):
        d, db = mocked_detector
        anchor_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC).isoformat()
        db.get_correlated_signals_in_window.return_value = [
            {
                "anchor_id": 7,
                "anchor_time": anchor_time,
                "sources": "truth_social,futures_oil",
                "source_count": 2,
            }
        ]
        d.check_and_signal()

        db.insert_signal.assert_called_once_with(
            source="correlation_detector",
            signal_type="correlated_signal",
            priority="CRITICAL",
            payload={
                "sources": "truth_social,futures_oil",
                "source_count": 2,
                "window_minutes": 10,
                "anchor_signal_id": 7,
                "anchor_time": anchor_time,
            },
            summary="CORRELATED: truth_social,futures_oil within 10 min (2 sources)",
        )

    def test_fireable_window_marks_anchor_as_fired(self, mocked_detector):
        d, db = mocked_detector
        db.get_correlated_signals_in_window.return_value = [
            {
                "anchor_id": 7,
                "anchor_time": datetime.now(UTC).isoformat(),
                "sources": "a,b",
                "source_count": 2,
            }
        ]
        d.check_and_signal()
        assert 7 in d._fired_on_anchors

    def test_fireable_window_updates_last_fired_time_and_persists(self, mocked_detector):
        d, db = mocked_detector
        anchor_dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        db.get_correlated_signals_in_window.return_value = [
            {
                "anchor_id": 7,
                "anchor_time": anchor_dt.isoformat(),
                "sources": "a,b",
                "source_count": 2,
            }
        ]
        d.check_and_signal()
        assert d._last_fired_time == anchor_dt
        db.state.set.assert_called_once_with(
            "correlation_last_fired_window", anchor_dt.isoformat()
        )

    def test_not_fireable_window_records_anchor_without_inserting(self, mocked_detector):
        d, db = mocked_detector
        d._fired_on_anchors.add(99)
        db.get_correlated_signals_in_window.return_value = [
            {
                "anchor_id": 99,
                "anchor_time": datetime.now(UTC).isoformat(),
                "sources": "a,b",
                "source_count": 2,
            }
        ]
        d.check_and_signal()
        db.insert_signal.assert_not_called()
        assert 99 in d._fired_on_anchors

    def test_missing_sources_defaults_to_multiple(self, mocked_detector):
        d, db = mocked_detector
        db.get_correlated_signals_in_window.return_value = [
            {"anchor_id": 1, "anchor_time": datetime.now(UTC).isoformat()}
        ]
        d.check_and_signal()
        _, kwargs = db.insert_signal.call_args
        assert kwargs["payload"]["sources"] == "multiple"
        assert kwargs["payload"]["source_count"] == 0

    def test_second_overlapping_window_collapses_into_cooldown(self, mocked_detector):
        """Two near-simultaneous anchors (one real-world cluster seen from two
        sources) collapse to a single insert — the second falls inside the
        cooldown the first just armed."""
        d, db = mocked_detector
        now = datetime.now(UTC).isoformat()
        db.get_correlated_signals_in_window.return_value = [
            {"anchor_id": 1, "anchor_time": now, "sources": "a,b", "source_count": 2},
            {"anchor_id": 2, "anchor_time": now, "sources": "c,d", "source_count": 2},
        ]
        d.check_and_signal()
        assert db.insert_signal.call_count == 1
        assert {1, 2} <= d._fired_on_anchors

    def test_two_windows_outside_each_others_cooldown_both_fire(self, mocked_detector):
        d, db = mocked_detector
        from datetime import timedelta
        t1 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        t2 = t1 + timedelta(hours=1)
        db.get_correlated_signals_in_window.return_value = [
            {"anchor_id": 1, "anchor_time": t1.isoformat(), "sources": "a,b", "source_count": 2},
            {"anchor_id": 2, "anchor_time": t2.isoformat(), "sources": "c,d", "source_count": 2},
        ]
        d.check_and_signal()
        assert db.insert_signal.call_count == 2
        assert {1, 2} <= d._fired_on_anchors

    def test_not_fireable_window_does_not_abort_remaining_windows(self, mocked_detector):
        """The not-fireable branch must `continue`, not `break` — an
        unfireable anchor early in the list must not swallow later fireable
        anchors."""
        d, db = mocked_detector
        d._fired_on_anchors.add(1)
        db.get_correlated_signals_in_window.return_value = [
            {
                "anchor_id": 1, "anchor_time": datetime.now(UTC).isoformat(),
                "sources": "a", "source_count": 2,
            },
            {
                "anchor_id": 2, "anchor_time": datetime.now(UTC).isoformat(),
                "sources": "b", "source_count": 2,
            },
        ]
        d.check_and_signal()
        db.insert_signal.assert_called_once()
        assert db.insert_signal.call_args.kwargs["payload"]["anchor_signal_id"] == 2

    def test_missing_anchor_time_key_defaults_to_empty_string_in_payload(self, mocked_detector):
        d, db = mocked_detector
        db.get_correlated_signals_in_window.return_value = [
            {"anchor_id": 1, "sources": "a,b", "source_count": 2}
        ]
        d.check_and_signal()
        assert db.insert_signal.call_args.kwargs["payload"]["anchor_time"] == ""

    def test_log_message_contains_actual_sources_and_anchor_time(self, mocked_detector, caplog):
        d, db = mocked_detector
        anchor_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC).isoformat()
        db.get_correlated_signals_in_window.return_value = [
            {
                "anchor_id": 1,
                "anchor_time": anchor_time,
                "sources": "truth_social,kalshi",
                "source_count": 2,
            }
        ]
        with caplog.at_level("WARNING"):
            d.check_and_signal()
        assert caplog.records[0].getMessage() == (
            f"CORRELATED SIGNAL: 2 sources (truth_social,kalshi) "
            f"within 10-minute window at {anchor_time}"
        )
