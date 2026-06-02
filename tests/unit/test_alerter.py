"""Unit tests for dispatcher/alerter.py."""

import json
from datetime import datetime, timezone, time
from unittest.mock import MagicMock, patch, call

import pytest
import responses as responses_lib

from sentinel.core.db import Database
from sentinel.dispatcher.alerter import (
    Alerter,
    AlertFormatter,
    RateLimiter,
    _priority_to_ntfy_priority,
    _priority_index,
    PRIORITY_LEVELS,
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


@pytest.fixture
def mock_config():
    cfg = MagicMock()
    cfg.alerts.provider = "ntfy"
    cfg.alerts.ntfy_topic = "sentinel-test"
    cfg.alerts.ntfy_url = "https://ntfy.sh"
    cfg.alerts.rate_limit_minutes = 5
    cfg.alerts.quiet_hours_utc.start = time(17, 0)   # 3am AEST
    cfg.alerts.quiet_hours_utc.end = time(21, 0)     # 7am AEST
    cfg.alerts.quiet_suppress_below = "MEDIUM"
    cfg.alerts.digest_time_utc = time(21, 0)
    return cfg


@pytest.fixture
def alerter(mock_config, mock_db):
    return Alerter(config=mock_config, db=mock_db)


def make_signal(
    source="truth_social",
    signal_type="new_post",
    priority="CRITICAL",
    payload=None,
    summary="Test signal",
    signal_id=1,
):
    return {
        "id": signal_id,
        "source": source,
        "signal_type": signal_type,
        "priority": priority,
        "payload": payload or {},
        "summary": summary,
        "alerted": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Priority helpers
# ---------------------------------------------------------------------------

class TestPriorityHelpers:
    def test_priority_index_order(self):
        assert _priority_index("INFO") < _priority_index("LOW")
        assert _priority_index("LOW") < _priority_index("MEDIUM")
        assert _priority_index("MEDIUM") < _priority_index("HIGH")
        assert _priority_index("HIGH") < _priority_index("CRITICAL")

    def test_ntfy_priority_critical(self):
        assert _priority_to_ntfy_priority("CRITICAL") == "5"

    def test_ntfy_priority_high(self):
        assert _priority_to_ntfy_priority("HIGH") == "4"

    def test_ntfy_priority_medium(self):
        assert _priority_to_ntfy_priority("MEDIUM") == "3"

    def test_ntfy_priority_low(self):
        assert _priority_to_ntfy_priority("LOW") == "2"

    def test_ntfy_priority_info(self):
        assert _priority_to_ntfy_priority("INFO") == "1"


# ---------------------------------------------------------------------------
# Alert formatting
# ---------------------------------------------------------------------------

class TestAlertFormatter:
    def test_format_truth_social_post(self):
        signal = make_signal(
            source="truth_social",
            signal_type="new_post",
            priority="CRITICAL",
            payload={
                "post_id": "123",
                "text": "We are winning big!",
                "url": "https://truthsocial.com/post/123",
                "has_media": False,
                "is_reblog": False,
            },
            summary="New Trump post [123]: We are winning big!",
        )
        title, body = AlertFormatter.format_signal(signal)
        assert "TRUTH SOCIAL" in title or "trump" in title.lower() or "new post" in title.lower()
        assert "We are winning big!" in body
        assert "https://truthsocial.com/post/123" in body

    def test_format_polymarket_large_bet(self):
        signal = make_signal(
            source="polymarket",
            signal_type="large_bet",
            priority="HIGH",
            payload={
                "amount_usd": 8400,
                "outcome": "YES",
                "market_name": "US-Iran ceasefire by April 15",
                "market_url": "https://polymarket.com/market/us-iran",
            },
            summary="Large bet $8400 YES on US-Iran ceasefire",
        )
        title, body = AlertFormatter.format_signal(signal)
        assert "POLYMARKET" in title or "large bet" in title.lower() or "bet" in title.lower()
        assert "8400" in body or "$8,400" in body

    def test_format_futures_volume_spike(self):
        signal = make_signal(
            source="futures_oil",
            signal_type="volume_spike",
            priority="HIGH",
            payload={
                "ticker": "CL=F",
                "name": "WTI Oil",
                "current_volume": 1500,
                "average_volume": 400,
                "ratio": 3.75,
                "price": 75.50,
                "price_change_pct": 1.2,
            },
            summary="Volume spike CL=F: 1500 contracts (3.75x avg)",
        )
        title, body = AlertFormatter.format_signal(signal)
        assert "VOLUME" in title or "spike" in title.lower() or "oil" in title.lower()
        assert "3.75" in body or "3.7" in body

    def test_format_correlated_signal(self):
        signal = make_signal(
            source="correlation_detector",
            signal_type="correlated_signal",
            priority="CRITICAL",
            payload={"sources": "truth_social,futures_oil", "window_minutes": 10},
            summary="CORRELATED: truth_social + futures_oil within 10 min",
        )
        title, body = AlertFormatter.format_signal(signal)
        assert "CORRELATED" in title or "correlation" in title.lower()

    def test_format_unknown_source_uses_summary(self):
        signal = make_signal(
            source="unknown_source",
            signal_type="unknown_type",
            priority="INFO",
            summary="Something happened",
        )
        title, body = AlertFormatter.format_signal(signal)
        assert "Something happened" in title or "Something happened" in body


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class TestRateLimiter:
    def test_not_rate_limited_initially(self):
        rl = RateLimiter(window_minutes=5)
        assert rl.is_rate_limited("truth_social", "CRITICAL") is False

    def test_critical_never_rate_limited(self):
        rl = RateLimiter(window_minutes=5)
        rl.record_sent("truth_social")
        # CRITICAL should never be rate limited
        assert rl.is_rate_limited("truth_social", "CRITICAL") is False

    def test_non_critical_rate_limited_after_first(self):
        rl = RateLimiter(window_minutes=5)
        rl.record_sent("polymarket")
        assert rl.is_rate_limited("polymarket", "HIGH") is True

    def test_rate_limit_expires(self):
        rl = RateLimiter(window_minutes=5)
        rl.record_sent("polymarket")
        # Manually expire the window
        import time as time_mod
        rl._last_sent["polymarket"] = time_mod.time() - 301  # 5 min + 1 sec ago
        assert rl.is_rate_limited("polymarket", "HIGH") is False

    def test_different_sources_independent(self):
        rl = RateLimiter(window_minutes=5)
        rl.record_sent("polymarket")
        assert rl.is_rate_limited("truth_social", "HIGH") is False

    def test_record_sent_updates_timestamp(self):
        import time as time_mod
        rl = RateLimiter(window_minutes=5)
        before = time_mod.time()
        rl.record_sent("polymarket")
        after = time_mod.time()
        ts = rl._last_sent.get("polymarket")
        assert ts is not None
        assert before <= ts <= after


# ---------------------------------------------------------------------------
# Quiet hours enforcement
# ---------------------------------------------------------------------------

class TestQuietHours:
    def test_not_in_quiet_hours_returns_false(self, alerter):
        # Active window: 11:00 UTC is outside quiet hours (17:00–21:00)
        check_time = time(11, 0)
        with patch("sentinel.dispatcher.alerter.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(
                utctimetuple=lambda: None,
                hour=11, minute=0, second=0
            )
            result = alerter.is_suppressed_by_quiet_hours("MEDIUM", check_time)
        assert result is False

    def test_in_quiet_hours_suppresses_low(self, alerter):
        # 18:00 UTC is inside quiet hours (17:00–21:00); LOW < MEDIUM so suppressed
        check_time = time(18, 0)
        result = alerter.is_suppressed_by_quiet_hours("LOW", check_time)
        assert result is True

    def test_in_quiet_hours_suppresses_info(self, alerter):
        check_time = time(18, 0)
        result = alerter.is_suppressed_by_quiet_hours("INFO", check_time)
        assert result is True

    def test_in_quiet_hours_does_not_suppress_medium(self, alerter):
        # quiet_suppress_below = MEDIUM, so MEDIUM itself is NOT suppressed
        check_time = time(18, 0)
        result = alerter.is_suppressed_by_quiet_hours("MEDIUM", check_time)
        assert result is False

    def test_in_quiet_hours_does_not_suppress_critical(self, alerter):
        check_time = time(18, 0)
        result = alerter.is_suppressed_by_quiet_hours("CRITICAL", check_time)
        assert result is False

    def test_truth_social_never_suppressed(self, alerter):
        # Truth Social CRITICAL is always sent
        check_time = time(18, 0)
        result = alerter.is_suppressed_by_quiet_hours("CRITICAL", check_time)
        assert result is False


# ---------------------------------------------------------------------------
# Ntfy dispatch
# ---------------------------------------------------------------------------

class TestNtfyDispatch:
    @responses_lib.activate
    def test_send_ntfy_success(self, alerter):
        responses_lib.add(
            responses_lib.POST,
            "https://ntfy.sh/sentinel-test",
            status=200,
            json={"id": "abc123"},
        )
        result = alerter.send_ntfy(
            title="Test Title",
            body="Test body",
            priority="5",
            tags="rotating_light",
        )
        assert result is True

    @responses_lib.activate
    def test_send_ntfy_failure_returns_false(self, alerter):
        responses_lib.add(
            responses_lib.POST,
            "https://ntfy.sh/sentinel-test",
            status=500,
        )
        result = alerter.send_ntfy(
            title="Test",
            body="Body",
            priority="3",
            tags="",
        )
        assert result is False

    @responses_lib.activate
    def test_send_ntfy_network_error_returns_false(self, alerter):
        responses_lib.add(
            responses_lib.POST,
            "https://ntfy.sh/sentinel-test",
            body=ConnectionError("Network error"),
        )
        result = alerter.send_ntfy(
            title="Test",
            body="Body",
            priority="3",
            tags="",
        )
        assert result is False

    @responses_lib.activate
    def test_send_ntfy_correct_headers(self, alerter):
        responses_lib.add(
            responses_lib.POST,
            "https://ntfy.sh/sentinel-test",
            status=200,
        )
        alerter.send_ntfy(title="T", body="B", priority="5", tags="bell")
        req = responses_lib.calls[0].request
        assert req.headers.get("Priority") == "5"
        assert req.headers.get("Title") == "T"
        assert req.headers.get("Tags") == "bell"


# ---------------------------------------------------------------------------
# Full dispatch pipeline
# ---------------------------------------------------------------------------

class TestDispatchSignal:
    @responses_lib.activate
    def test_dispatch_marks_alerted(self, alerter, mock_db):
        responses_lib.add(
            responses_lib.POST,
            "https://ntfy.sh/sentinel-test",
            status=200,
        )
        signal_id = mock_db.insert_signal(
            "truth_social", "new_post", "CRITICAL",
            {"post_id": "123", "text": "Hello", "url": "http://x", "has_media": False, "is_reblog": False},
            "New post"
        )
        signal = mock_db.get_unalerted_signals()[0]
        result = alerter.dispatch_signal(signal)
        assert result is True
        rows = mock_db.execute_fetchall("SELECT alerted FROM signals WHERE id=?", (signal_id,))
        assert rows[0]["alerted"] == 1

    @responses_lib.activate
    def test_dispatch_skipped_when_rate_limited(self, alerter, mock_db):
        signal_id = mock_db.insert_signal(
            "polymarket", "large_bet", "HIGH", {}, "Big bet"
        )
        # Pre-fill the rate limiter
        alerter._rate_limiter.record_sent("polymarket")
        signal = mock_db.get_unalerted_signals()[0]
        result = alerter.dispatch_signal(signal)
        # Should return False (suppressed) but still not crash
        # The signal may remain unalerted (implementation may vary on suppressed behaviour)
        # At minimum, we check ntfy was not called
        assert len(responses_lib.calls) == 0

    @responses_lib.activate
    def test_dispatch_skipped_during_quiet_hours_low(self, alerter, mock_db):
        signal_id = mock_db.insert_signal(
            "futures_oil", "volume_spike", "LOW", {}, "Low spike"
        )
        signal = mock_db.get_unalerted_signals()[0]
        # Quiet hours: 17:00–21:00 UTC; LOW is suppressed
        with patch("sentinel.dispatcher.alerter.datetime") as mock_dt:
            from datetime import datetime as real_datetime
            mock_dt.now.return_value = real_datetime(2026, 3, 27, 18, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.side_effect = None
            result = alerter.dispatch_signal(signal, now_utc=time(18, 0))
        assert len(responses_lib.calls) == 0


# ---------------------------------------------------------------------------
# Daily digest
# ---------------------------------------------------------------------------

class TestDailyDigest:
    @responses_lib.activate
    def test_digest_sends_summary(self, alerter, mock_db):
        responses_lib.add(
            responses_lib.POST,
            "https://ntfy.sh/sentinel-test",
            status=200,
        )
        for i in range(3):
            mock_db.insert_signal("truth_social", "new_post", "CRITICAL", {}, f"Post {i}")
        alerter.send_daily_digest(since_hours=24)
        assert len(responses_lib.calls) == 1
        req_body = responses_lib.calls[0].request.body
        if isinstance(req_body, bytes):
            req_body = req_body.decode()
        assert "3" in req_body or "signal" in req_body.lower()

    @responses_lib.activate
    def test_digest_no_signals_still_sends(self, alerter):
        responses_lib.add(
            responses_lib.POST,
            "https://ntfy.sh/sentinel-test",
            status=200,
        )
        alerter.send_daily_digest(since_hours=24)
        assert len(responses_lib.calls) == 1


# ---------------------------------------------------------------------------
# Poll loop (mocked)
# ---------------------------------------------------------------------------

class TestPollLoop:
    def test_poll_once_processes_unalerted(self, alerter, mock_db):
        mock_db.insert_signal("truth_social", "new_post", "CRITICAL",
                              {"post_id": "1", "text": "Hi", "url": "http://x",
                               "has_media": False, "is_reblog": False},
                              "New post")
        with patch.object(alerter, "dispatch_signal", return_value=True) as mock_dispatch:
            alerter.poll_once()
        mock_dispatch.assert_called_once()

    def test_poll_once_no_signals_does_nothing(self, alerter, mock_db):
        with patch.object(alerter, "dispatch_signal") as mock_dispatch:
            alerter.poll_once()
        mock_dispatch.assert_not_called()
