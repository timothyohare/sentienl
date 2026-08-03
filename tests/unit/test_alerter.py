"""Unit tests for dispatcher/alerter.py."""

from datetime import UTC, datetime, time
from unittest.mock import MagicMock, patch

import pytest
import responses as responses_lib

from sentinel.core.db import Database
from sentinel.dispatcher.alerter import (
    Alerter,
    AlertFormatter,
    RateLimiter,
    _priority_index,
    _priority_to_ntfy_priority,
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
        "created_at": datetime.now(UTC).isoformat(),
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
                "created_at": "2026-01-01T00:00:00Z",
            },
            summary="New Trump post [123]: We are winning big!",
        )
        title, body = AlertFormatter.format_signal(signal)
        assert title == "TRUTH SOCIAL — New Trump post"
        assert body == (
            "We are winning big!\n"
            "Full post: https://truthsocial.com/post/123\n"
            "Posted: 2026-01-01T00:00:00Z"
        )

    def test_format_truth_social_reblog_tag(self):
        signal = make_signal(
            source="truth_social",
            payload={"text": "x", "is_reblog": True, "has_media": False},
        )
        title, _ = AlertFormatter.format_signal(signal)
        assert title == "TRUTH SOCIAL — New Trump post [retruth]"

    def test_format_truth_social_media_tag(self):
        signal = make_signal(
            source="truth_social",
            payload={"text": "x", "is_reblog": False, "has_media": True},
        )
        title, _ = AlertFormatter.format_signal(signal)
        assert title == "TRUTH SOCIAL — New Trump post [media]"

    def test_format_truth_social_reblog_and_media_tags_ordered(self):
        signal = make_signal(
            source="truth_social",
            payload={"text": "x", "is_reblog": True, "has_media": True},
        )
        title, _ = AlertFormatter.format_signal(signal)
        assert title == "TRUTH SOCIAL — New Trump post [retruth] [media]"

    def test_format_truth_social_text_truncated_to_280_chars(self):
        long_text = "a" * 400
        signal = make_signal(source="truth_social", payload={"text": long_text})
        signal["created_at"] = ""
        _, body = AlertFormatter.format_signal(signal)
        assert body == "a" * 280

    def test_format_truth_social_no_url_omits_line(self):
        signal = make_signal(source="truth_social", payload={"text": "hi", "url": ""})
        signal["created_at"] = ""
        _, body = AlertFormatter.format_signal(signal)
        assert "Full post:" not in body
        assert body == "hi"

    def test_format_truth_social_no_created_at_omits_line(self):
        signal = make_signal(
            source="truth_social",
            payload={"text": "hi", "url": "http://x"},
        )
        signal["created_at"] = ""
        _, body = AlertFormatter.format_signal(signal)
        assert "Posted:" not in body

    def test_format_truth_social_falls_back_to_signal_summary_for_text(self):
        signal = make_signal(
            source="truth_social", payload={}, summary="fallback summary",
        )
        signal["created_at"] = ""
        _, body = AlertFormatter.format_signal(signal)
        assert body == "fallback summary"

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
        )
        title, body = AlertFormatter.format_signal(signal)
        assert title == "POLYMARKET — Large bet on US-Iran ceasefire by April 15"
        assert body == (
            "Type: Large bet\n"
            "Detail: $8,400 YES\n"
            "Market: US-Iran ceasefire by April 15\n"
            "https://polymarket.com/market/us-iran"
        )

    def test_format_polymarket_new_wallet(self):
        signal = make_signal(
            source="polymarket",
            signal_type="new_wallet",
            payload={
                "wallet_age_days": 2,
                "amount_usd": 1200,
                "outcome": "NO",
                "market_name": "M",
                "market_url": "http://u",
            },
        )
        title, body = AlertFormatter.format_signal(signal)
        assert title == "POLYMARKET — New wallet bet on M"
        assert body == (
            "Type: New wallet\n"
            "Detail: 2-day-old wallet bet $1,200 NO\n"
            "Market: M\n"
            "http://u"
        )

    def test_format_polymarket_odds_move_up(self):
        signal = make_signal(
            source="polymarket",
            signal_type="odds_move",
            payload={"change_pct": 6.25, "market_name": "M", "market_url": "http://u"},
        )
        title, body = AlertFormatter.format_signal(signal)
        assert title == "POLYMARKET — Odds move up on M"
        assert body == (
            "Type: Odds move\n"
            "Detail: 6.2pp up in 5 min\n"
            "Market: M\n"
            "http://u"
        )

    def test_format_polymarket_odds_move_down(self):
        signal = make_signal(
            source="polymarket",
            signal_type="odds_move",
            payload={"change_pct": -6.25, "market_name": "M", "market_url": "http://u"},
        )
        title, body = AlertFormatter.format_signal(signal)
        assert title == "POLYMARKET — Odds move down on M"
        assert "Detail: 6.2pp down in 5 min" in body

    def test_format_polymarket_odds_move_zero_is_down(self):
        """change_pct == 0 takes the `else` branch of `up if change > 0 else down`."""
        signal = make_signal(
            source="polymarket",
            signal_type="odds_move",
            payload={"change_pct": 0, "market_name": "M", "market_url": "http://u"},
        )
        title, _ = AlertFormatter.format_signal(signal)
        assert title == "POLYMARKET — Odds move down on M"

    def test_format_polymarket_volume_spike(self):
        signal = make_signal(
            source="polymarket",
            signal_type="volume_spike",
            payload={"multiplier": 4.567, "market_name": "M", "market_url": "http://u"},
        )
        title, body = AlertFormatter.format_signal(signal)
        assert title == "POLYMARKET — Volume spike on M"
        assert body == (
            "Type: Volume spike\n"
            "Detail: 4.6x 24hr average\n"
            "Market: M\n"
            "http://u"
        )

    def test_format_polymarket_unknown_signal_type_uses_summary(self):
        signal = make_signal(
            source="polymarket",
            signal_type="something_else",
            payload={"market_name": "M"},
            summary="raw summary",
        )
        title, body = AlertFormatter.format_signal(signal)
        assert title == "POLYMARKET SIGNAL — M"
        assert body == "raw summary"

    def test_format_polymarket_market_name_falls_back_to_market_key(self):
        signal = make_signal(
            source="polymarket", signal_type="large_bet",
            payload={
                "market": "fallback name", "amount_usd": 1,
                "outcome": "YES", "market_url": "",
            },
        )
        title, _ = AlertFormatter.format_signal(signal)
        assert title == "POLYMARKET — Large bet on fallback name"

    def test_format_polymarket_market_name_defaults_to_unknown(self):
        signal = make_signal(
            source="polymarket", signal_type="large_bet",
            payload={"amount_usd": 1, "outcome": "YES", "market_url": ""},
        )
        title, _ = AlertFormatter.format_signal(signal)
        assert title == "POLYMARKET — Large bet on Unknown market"

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
        )
        signal["created_at"] = "2026-01-01T00:00:00Z"
        title, body = AlertFormatter.format_signal(signal)
        assert title == "VOLUME SPIKE — WTI Oil (CL=F)"
        assert body == (
            "Current 1-min volume: 1,500 contracts\n"
            "20-bar avg: 400 contracts\n"
            "Ratio: 3.75x\n"
            "Price: 75.50 · Change: +1.20%\n"
            "Time: 2026-01-01T00:00:00Z"
        )

    def test_format_futures_negative_price_change(self):
        signal = make_signal(
            source="futures_sp500",
            payload={"ticker": "ES=F", "price": 100, "price_change_pct": -2.5},
        )
        _, body = AlertFormatter.format_signal(signal)
        assert "Change: -2.50%" in body

    def test_format_futures_name_defaults_to_ticker(self):
        signal = make_signal(source="futures_brent", payload={"ticker": "BZ=F"})
        title, _ = AlertFormatter.format_signal(signal)
        assert title == "VOLUME SPIKE — BZ=F (BZ=F)"

    @pytest.mark.parametrize(
        "source", ["futures_oil", "futures_sp500", "futures_brent",
                    "futures_natgas", "futures_gold", "futures_dxy"],
    )
    def test_all_futures_sources_route_to_futures_formatter(self, source):
        signal = make_signal(source=source, payload={"ticker": "X"})
        title, _ = AlertFormatter.format_signal(signal)
        assert title.startswith("VOLUME SPIKE")

    def test_format_correlated_signal(self):
        signal = make_signal(
            source="correlation_detector",
            signal_type="correlated_signal",
            priority="CRITICAL",
            payload={"sources": "truth_social,futures_oil", "window_minutes": 10},
            summary="tail summary",
        )
        title, body = AlertFormatter.format_signal(signal)
        assert title == "CORRELATED SIGNAL DETECTED"
        assert body == (
            "Multiple sources fired within 10 minutes:\n"
            "Sources: truth_social,futures_oil\n"
            "tail summary"
        )

    def test_format_correlated_defaults(self):
        signal = make_signal(source="correlation_detector", payload={}, summary="")
        _, body = AlertFormatter.format_signal(signal)
        assert body == "Multiple sources fired within 10 minutes:\nSources: multiple sources\n"

    def test_format_unknown_source_uses_summary(self):
        signal = make_signal(
            source="unknown_source",
            signal_type="unknown_type",
            priority="INFO",
            summary="Something happened",
        )
        title, body = AlertFormatter.format_signal(signal)
        assert title == "Something happened"
        assert body == "Something happened"

    def test_format_signal_default_summary_when_missing(self):
        signal = make_signal(source="unknown_source", summary=None)
        del signal["summary"]
        title, body = AlertFormatter.format_signal(signal)
        assert title == "Signal detected"
        assert body == "Signal detected"


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
    def test_send_ntfy_noop_when_alerts_disabled(self, alerter):
        alerter.config.alerts.enabled = False
        result = alerter.send_ntfy(
            title="Test",
            body="Body",
            priority="3",
            tags="",
        )
        assert result is True
        assert len(responses_lib.calls) == 0

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
        assert req.headers.get("Content-Type") == "text/plain; charset=utf-8"

    @responses_lib.activate
    def test_send_ntfy_url_is_ntfy_url_slash_topic(self, alerter):
        alerter.config.alerts.ntfy_url = "https://custom.example"
        alerter.config.alerts.ntfy_topic = "my-topic"
        responses_lib.add(responses_lib.POST, "https://custom.example/my-topic", status=200)
        result = alerter.send_ntfy(title="T", body="B", priority="1", tags="")
        assert result is True
        assert responses_lib.calls[0].request.url == "https://custom.example/my-topic"

    @responses_lib.activate
    def test_send_ntfy_body_sent_as_utf8_bytes(self, alerter):
        responses_lib.add(responses_lib.POST, "https://ntfy.sh/sentinel-test", status=200)
        alerter.send_ntfy(title="T", body="héllo wörld", priority="1", tags="")
        sent_body = responses_lib.calls[0].request.body
        assert sent_body == "héllo wörld".encode()

    @responses_lib.activate
    def test_send_ntfy_non_ascii_title_replaced(self, alerter):
        responses_lib.add(responses_lib.POST, "https://ntfy.sh/sentinel-test", status=200)
        alerter.send_ntfy(title="Tïtlé", body="B", priority="1", tags="")
        req = responses_lib.calls[0].request
        assert req.headers.get("Title") == "T?tl?"

    @responses_lib.activate
    def test_send_ntfy_status_299_is_success(self, alerter):
        responses_lib.add(responses_lib.POST, "https://ntfy.sh/sentinel-test", status=299)
        assert alerter.send_ntfy(title="T", body="B", priority="1", tags="") is True

    @responses_lib.activate
    def test_send_ntfy_status_300_is_failure(self, alerter):
        responses_lib.add(responses_lib.POST, "https://ntfy.sh/sentinel-test", status=300)
        assert alerter.send_ntfy(title="T", body="B", priority="1", tags="") is False

    @responses_lib.activate
    def test_send_ntfy_disabled_does_not_touch_session(self, alerter):
        """The enabled=False no-op must return before any network call — this
        is the data-collection-only mode's contract."""
        alerter.config.alerts.enabled = False
        alerter.send_ntfy(title="T", body="B", priority="1", tags="x")
        assert len(responses_lib.calls) == 0


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
            {
                "post_id": "123", "text": "Hello", "url": "http://x",
                "has_media": False, "is_reblog": False,
            },
            "New post"
        )
        signal = mock_db.get_unalerted_signals()[0]
        result = alerter.dispatch_signal(signal)
        assert result is True
        rows = mock_db.execute_fetchall("SELECT alerted FROM signals WHERE id=?", (signal_id,))
        assert rows[0]["alerted"] == 1

    @responses_lib.activate
    def test_dispatch_skipped_when_rate_limited(self, alerter, mock_db):
        mock_db.insert_signal(
            "polymarket", "large_bet", "HIGH", {}, "Big bet"
        )
        # Pre-fill the rate limiter
        alerter._rate_limiter.record_sent("polymarket")
        signal = mock_db.get_unalerted_signals()[0]
        alerter.dispatch_signal(signal)
        # Should return False (suppressed) but still not crash
        # The signal may remain unalerted (implementation may vary on suppressed behaviour)
        # At minimum, we check ntfy was not called
        assert len(responses_lib.calls) == 0

    @responses_lib.activate
    def test_dispatch_skipped_during_quiet_hours_low(self, alerter, mock_db):
        mock_db.insert_signal(
            "futures_oil", "volume_spike", "LOW", {}, "Low spike"
        )
        signal = mock_db.get_unalerted_signals()[0]
        # Quiet hours: 17:00–21:00 UTC; LOW is suppressed
        with patch("sentinel.dispatcher.alerter.datetime") as mock_dt:
            from datetime import datetime as real_datetime
            mock_dt.now.return_value = real_datetime(2026, 3, 27, 18, 0, 0, tzinfo=UTC)
            mock_dt.now.side_effect = None
            alerter.dispatch_signal(signal, now_utc=time(18, 0))
        assert len(responses_lib.calls) == 0

    def test_dispatch_calls_send_ntfy_with_formatted_title_body_priority_tags(self, alerter):
        sig = make_signal(
            source="polymarket", signal_type="large_bet", priority="HIGH", signal_id=5,
            payload={"amount_usd": 100, "outcome": "YES", "market_name": "M", "market_url": ""},
        )
        with patch.object(alerter, "send_ntfy", return_value=True) as mock_send:
            alerter.dispatch_signal(sig, now_utc=time(11, 0))
        mock_send.assert_called_once_with(
            title="POLYMARKET — Large bet on M",
            body="Type: Large bet\nDetail: $100 YES\nMarket: M\n",
            priority="4",
            tags="warning",
        )

    def test_dispatch_unknown_priority_tag_defaults_to_bell(self, alerter):
        sig = make_signal(source="unknown", priority="WEIRD", signal_id=6)
        with patch.object(alerter, "send_ntfy", return_value=True) as mock_send:
            alerter.dispatch_signal(sig, now_utc=time(11, 0))
        assert mock_send.call_args.kwargs["tags"] == "bell"
        assert mock_send.call_args.kwargs["priority"] == "3"

    def test_dispatch_success_marks_alerted_and_records_rate_limit(self, alerter):
        sig = make_signal(source="kalshi", priority="HIGH", signal_id=7)
        with (
            patch.object(alerter, "send_ntfy", return_value=True),
            patch.object(alerter.db, "mark_alerted") as mock_mark,
            patch.object(alerter._rate_limiter, "record_sent") as mock_record,
        ):
            result = alerter.dispatch_signal(sig, now_utc=time(11, 0))
        assert result is True
        mock_mark.assert_called_once_with(7)
        mock_record.assert_called_once_with("kalshi")

    def test_dispatch_failure_does_not_mark_alerted_or_rate_limit(self, alerter):
        sig = make_signal(source="kalshi", priority="HIGH", signal_id=8)
        with (
            patch.object(alerter, "send_ntfy", return_value=False),
            patch.object(alerter.db, "mark_alerted") as mock_mark,
            patch.object(alerter._rate_limiter, "record_sent") as mock_record,
        ):
            result = alerter.dispatch_signal(sig, now_utc=time(11, 0))
        assert result is False
        mock_mark.assert_not_called()
        mock_record.assert_not_called()


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

    def test_digest_exact_title_and_body_with_signals(self, alerter, mock_db):
        mock_db.insert_signal("truth_social", "new_post", "CRITICAL", {}, "P0")
        mock_db.insert_signal("truth_social", "new_post", "CRITICAL", {}, "P1")
        mock_db.insert_signal("kalshi", "large_bet", "HIGH", {}, "P2")
        with patch.object(alerter, "send_ntfy", return_value=True) as mock_send:
            alerter.send_daily_digest(since_hours=24)
        kwargs = mock_send.call_args.kwargs
        assert kwargs["title"] == "Sentinel Daily Digest — 3 signals in last 24h"
        assert kwargs["priority"] == "1"
        assert kwargs["tags"] == "calendar"
        assert "Signals by source:" in kwargs["body"]
        assert "truth_social / CRITICAL: 2" in kwargs["body"]
        assert "kalshi / HIGH: 1" in kwargs["body"]

    def test_digest_singular_signal_no_plural_s(self, alerter, mock_db):
        mock_db.insert_signal("truth_social", "new_post", "CRITICAL", {}, "P0")
        with patch.object(alerter, "send_ntfy", return_value=True) as mock_send:
            alerter.send_daily_digest(since_hours=24)
        assert mock_send.call_args.kwargs["title"] == "Sentinel Daily Digest — 1 signal in last 24h"

    def test_digest_zero_signals_exact_body(self, alerter):
        with patch.object(alerter, "send_ntfy", return_value=True) as mock_send:
            alerter.send_daily_digest(since_hours=24)
        kwargs = mock_send.call_args.kwargs
        assert kwargs["title"] == "Sentinel Daily Digest — 0 signals in last 24h"
        assert kwargs["body"] == "No signals in the last 24 hours. All quiet."

    def test_digest_since_hours_used_in_title_and_query(self, alerter, mock_db):
        with patch.object(alerter, "send_ntfy", return_value=True) as mock_send:
            alerter.send_daily_digest(since_hours=6)
        assert "last 6h" in mock_send.call_args.kwargs["title"]

    def test_digest_excludes_signals_outside_since_window(self, alerter, mock_db):
        from datetime import timedelta
        old_time = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        mock_db.execute(
            "INSERT INTO signals (source, signal_type, priority, payload, summary, "
            "alerted, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("truth_social", "new_post", "CRITICAL", "{}", "old", 0, old_time),
        )
        mock_db._conn.commit()
        with patch.object(alerter, "send_ntfy", return_value=True) as mock_send:
            alerter.send_daily_digest(since_hours=24)
        title = mock_send.call_args.kwargs["title"]
        assert title == "Sentinel Daily Digest — 0 signals in last 24h"


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

    def test_poll_once_returns_count_of_dispatched_only(self, alerter, mock_db):
        for i in range(3):
            mock_db.insert_signal(
                "truth_social", "new_post", "CRITICAL",
                {"post_id": str(i), "text": "x", "url": "", "has_media": False, "is_reblog": False},
                f"Post {i}",
            )
        with patch.object(alerter, "dispatch_signal", side_effect=[True, False, True]):
            dispatched = alerter.poll_once()
        assert dispatched == 2

    def test_poll_once_passes_now_time_through(self, alerter, mock_db):
        mock_db.insert_signal(
            "truth_social", "new_post", "CRITICAL",
            {"post_id": "1", "text": "x", "url": "", "has_media": False, "is_reblog": False},
            "Post",
        )
        with patch.object(alerter, "dispatch_signal", return_value=True) as mock_dispatch:
            alerter.poll_once()
        _, kwargs = mock_dispatch.call_args
        assert kwargs["now_utc"] is not None


# ---------------------------------------------------------------------------
# CRITICAL bypass — full dispatch pipeline (not just the predicates in isolation)
# ---------------------------------------------------------------------------

class TestCriticalBypassEndToEnd:
    def test_critical_bypasses_quiet_hours_and_rate_limit(self, alerter):
        # Arm the rate limiter for the source AND put the clock inside quiet
        # hours (17:00–21:00). A CRITICAL signal must still be dispatched.
        sig = make_signal(source="truth_social", priority="CRITICAL", signal_id=1)
        alerter._rate_limiter.record_sent("truth_social")
        with patch.object(alerter, "send_ntfy", return_value=True) as mock_send:
            sent = alerter.dispatch_signal(sig, now_utc=time(18, 0))
        assert sent is True
        mock_send.assert_called_once()

    def test_non_critical_blocked_under_same_conditions(self, alerter):
        # Control: a HIGH signal under the same armed rate limiter is suppressed,
        # proving the bypass above is a real effect and not a vacuous assertion.
        sig = make_signal(
            source="futures_oil", signal_type="volume_spike",
            priority="HIGH", signal_id=2,
        )
        alerter._rate_limiter.record_sent("futures_oil")
        with patch.object(alerter, "send_ntfy", return_value=True) as mock_send:
            sent = alerter.dispatch_signal(sig, now_utc=time(11, 0))
        assert sent is False
        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# Quiet hours with a midnight-crossing window (e.g. 23:00–06:00)
# ---------------------------------------------------------------------------

class TestMidnightCrossingQuietHours:
    @staticmethod
    def _alerter_with_quiet(db, start, end):
        cfg = MagicMock()
        cfg.alerts.ntfy_topic = "sentinel-test"
        cfg.alerts.ntfy_url = "https://ntfy.sh"
        cfg.alerts.rate_limit_minutes = 5
        cfg.alerts.quiet_hours_utc.start = start
        cfg.alerts.quiet_hours_utc.end = end
        cfg.alerts.quiet_suppress_below = "MEDIUM"
        return Alerter(config=cfg, db=db)

    def test_low_suppressed_inside_overnight_window(self, mock_db):
        a = self._alerter_with_quiet(mock_db, time(23, 0), time(6, 0))
        # 01:00 is inside the overnight window
        assert a.is_suppressed_by_quiet_hours("LOW", time(1, 0)) is True

    def test_low_sent_outside_overnight_window(self, mock_db):
        a = self._alerter_with_quiet(mock_db, time(23, 0), time(6, 0))
        # 10:00 is outside the overnight window
        assert a.is_suppressed_by_quiet_hours("LOW", time(10, 0)) is False
