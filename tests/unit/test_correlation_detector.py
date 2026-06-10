"""Unit tests for collectors/correlation_detector.py."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

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

def insert_signal_now(db, source, priority="HIGH"):
    now = datetime.now(timezone.utc).isoformat()
    return db.execute(
        "INSERT INTO signals (source, signal_type, priority, payload, summary, alerted, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source, "volume_spike", priority, "{}", f"Signal from {source}", 0, now),
    ).lastrowid


def insert_signal_at(db, source, created_at, priority="HIGH"):
    """Insert a signal at an explicit ISO8601 timestamp."""
    return db.execute(
        "INSERT INTO signals (source, signal_type, priority, payload, summary, alerted, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
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
        now = datetime.now(timezone.utc)
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
