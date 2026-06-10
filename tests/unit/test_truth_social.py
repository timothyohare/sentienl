"""Unit tests for collectors/truth_social.py."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import pytest

from sentinel.collectors.truth_social import (
    TruthSocialCollector,
    ACCOUNT_ID_FALLBACK,
    BASE_URL,
    _extract_text,
    _build_summary,
)
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
    cfg.truth_social.account_handle = "realDonaldTrump"
    cfg.truth_social.account_id_fallback = ACCOUNT_ID_FALLBACK
    cfg.truth_social.poll_interval_seconds = 8
    cfg.truth_social.alert_all_posts = True
    cfg.truth_social.keyword_filter = []
    cfg.truth_social.backoff_seconds = [30, 60, 120, 300]
    cfg.truth_social.critical_keywords = ["tariff", "china", "war", "fed"]
    cfg.truth_social.endorsement_markers = ["endorse", "endorsement"]
    cfg.truth_social.default_priority = "MEDIUM"
    return cfg


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.fetch_posts.return_value = []
    client.resolve_account_id.return_value = None
    return client


@pytest.fixture
def collector(mock_config, mock_db, mock_client):
    return TruthSocialCollector(config=mock_config, db=mock_db, client=mock_client)


# Fake API responses
FAKE_POST_1 = {
    "id": "110000000000000001",
    "created_at": "2026-03-27T10:00:00.000Z",
    "content": "<p>We are winning!</p>",
    "url": "https://truthsocial.com/@realDonaldTrump/110000000000000001",
    "media_attachments": [],
    "reblog": None,
}

FAKE_POST_2 = {
    "id": "110000000000000002",
    "created_at": "2026-03-27T10:05:00.000Z",
    "content": "<p>Tariffs are great for America. We <strong>must</strong> act now!</p>",
    "url": "https://truthsocial.com/@realDonaldTrump/110000000000000002",
    "media_attachments": [{"type": "image"}],
    "reblog": None,
}

FAKE_REBLOG = {
    "id": "110000000000000003",
    "created_at": "2026-03-27T10:10:00.000Z",
    "content": "",
    "url": "https://truthsocial.com/@realDonaldTrump/110000000000000003",
    "media_attachments": [],
    "reblog": {"id": "99999", "content": "<p>Original post</p>"},
}


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

class TestExtractText:
    def test_strips_html_tags(self):
        html = "<p>Hello <strong>world</strong></p>"
        assert _extract_text(html) == "Hello world"

    def test_empty_string(self):
        assert _extract_text("") == ""

    def test_no_html(self):
        assert _extract_text("Plain text") == "Plain text"

    def test_nested_tags(self):
        html = "<p>We are <em>winning</em>!</p>"
        assert _extract_text(html) == "We are winning!"

    def test_br_tag_replaced_with_space(self):
        html = "<p>Line one<br>Line two</p>"
        result = _extract_text(html)
        assert "Line one" in result
        assert "Line two" in result

    def test_truncation_at_280_chars(self):
        long_text = "A" * 400
        html = f"<p>{long_text}</p>"
        result = _extract_text(html, max_chars=280)
        assert len(result) <= 283  # 280 + '...'

    def test_no_truncation_under_limit(self):
        short = "Short text"
        assert _extract_text(f"<p>{short}</p>", max_chars=280) == short


class TestBuildSummary:
    def test_new_post_summary(self):
        summary = _build_summary(FAKE_POST_1, text="We are winning!")
        assert "We are winning!" in summary

    def test_summary_includes_source(self):
        summary = _build_summary(FAKE_POST_1, text="Hello")
        assert "truth" in summary.lower() or "trump" in summary.lower() or "new post" in summary.lower()

    def test_truncated_summary(self):
        long_text = "W" * 300
        summary = _build_summary(FAKE_POST_1, text=long_text)
        assert len(summary) < 400  # should be reasonably sized


# ---------------------------------------------------------------------------
# Account ID resolution
# ---------------------------------------------------------------------------

class TestAccountIdResolution:
    def test_resolve_account_id_success(self, collector, mock_client):
        mock_client.resolve_account_id.return_value = "107780257626128497"
        account_id = collector.resolve_account_id()
        assert account_id == "107780257626128497"

    def test_resolve_account_id_falls_back_on_none(self, collector, mock_client):
        mock_client.resolve_account_id.return_value = None
        account_id = collector.resolve_account_id()
        assert account_id == ACCOUNT_ID_FALLBACK

    def test_resolve_account_id_falls_back_on_exception(self, collector, mock_client):
        mock_client.resolve_account_id.side_effect = Exception("Network error")
        account_id = collector.resolve_account_id()
        assert account_id == ACCOUNT_ID_FALLBACK

    def test_resolve_account_id_no_client(self, mock_config, mock_db):
        collector = TruthSocialCollector(config=mock_config, db=mock_db, client=None)
        account_id = collector.resolve_account_id()
        assert account_id == ACCOUNT_ID_FALLBACK


# ---------------------------------------------------------------------------
# Fetching posts
# ---------------------------------------------------------------------------

class TestFetchPosts:
    def test_fetch_posts_returns_list(self, collector, mock_client):
        mock_client.fetch_posts.return_value = [FAKE_POST_1, FAKE_POST_2]
        posts = collector.fetch_posts("107780257626128497")
        assert len(posts) == 2

    def test_fetch_posts_returns_empty_on_empty(self, collector, mock_client):
        mock_client.fetch_posts.return_value = []
        posts = collector.fetch_posts("107780257626128497")
        assert posts == []

    def test_fetch_posts_passes_params(self, collector, mock_client):
        mock_client.fetch_posts.return_value = [FAKE_POST_1]
        collector.fetch_posts("107780257626128497", limit=20)
        mock_client.fetch_posts.assert_called_once_with("107780257626128497", limit=20)

    def test_fetch_posts_handles_exception(self, collector, mock_client):
        mock_client.fetch_posts.side_effect = ConnectionError("Network error")
        posts = collector.fetch_posts("107780257626128497")
        assert posts == []

    def test_fetch_posts_no_client(self, mock_config, mock_db):
        collector = TruthSocialCollector(config=mock_config, db=mock_db, client=None)
        posts = collector.fetch_posts("107780257626128497")
        assert posts == []


# ---------------------------------------------------------------------------
# New post detection
# ---------------------------------------------------------------------------

class TestNewPostDetection:
    def test_no_last_post_id_all_posts_are_new(self, collector):
        posts = [FAKE_POST_1, FAKE_POST_2]
        new_posts = collector.filter_new_posts(posts, last_post_id=None)
        assert len(new_posts) == 2

    def test_filters_already_seen_posts(self, collector):
        posts = [FAKE_POST_2, FAKE_POST_1]  # newest first
        new_posts = collector.filter_new_posts(
            posts, last_post_id=FAKE_POST_1["id"]
        )
        assert len(new_posts) == 1
        assert new_posts[0]["id"] == FAKE_POST_2["id"]

    def test_no_new_posts_when_all_seen(self, collector):
        posts = [FAKE_POST_2, FAKE_POST_1]
        new_posts = collector.filter_new_posts(
            posts, last_post_id=FAKE_POST_2["id"]
        )
        assert new_posts == []

    def test_empty_posts_returns_empty(self, collector):
        new_posts = collector.filter_new_posts([], last_post_id="123")
        assert new_posts == []


# ---------------------------------------------------------------------------
# Signal creation
# ---------------------------------------------------------------------------

class TestSignalCreation:
    def test_process_post_writes_to_db(self, collector):
        initial_count = len(collector.db.execute_fetchall("SELECT * FROM signals"))
        collector.process_post(FAKE_POST_1)
        final_count = len(collector.db.execute_fetchall("SELECT * FROM signals"))
        assert final_count == initial_count + 1

    def test_process_post_correct_source(self, collector):
        collector.process_post(FAKE_POST_1)
        rows = collector.db.execute_fetchall(
            "SELECT source FROM signals ORDER BY id DESC LIMIT 1"
        )
        assert rows[0]["source"] == "truth_social"

    def test_process_post_critical_priority(self, collector):
        # A market-moving post (tariffs) is classified CRITICAL
        collector.process_post(FAKE_POST_2)
        rows = collector.db.execute_fetchall(
            "SELECT priority FROM signals ORDER BY id DESC LIMIT 1"
        )
        assert rows[0]["priority"] == "CRITICAL"

    def test_process_post_payload_contains_post_id(self, collector):
        collector.process_post(FAKE_POST_1)
        rows = collector.db.execute_fetchall(
            "SELECT payload FROM signals ORDER BY id DESC LIMIT 1"
        )
        payload = json.loads(rows[0]["payload"])
        assert payload["post_id"] == FAKE_POST_1["id"]

    def test_process_post_payload_has_media_flag(self, collector):
        collector.process_post(FAKE_POST_2)
        rows = collector.db.execute_fetchall(
            "SELECT payload FROM signals ORDER BY id DESC LIMIT 1"
        )
        payload = json.loads(rows[0]["payload"])
        assert payload["has_media"] is True

    def test_process_post_no_media_flag_false(self, collector):
        collector.process_post(FAKE_POST_1)
        rows = collector.db.execute_fetchall(
            "SELECT payload FROM signals ORDER BY id DESC LIMIT 1"
        )
        payload = json.loads(rows[0]["payload"])
        assert payload["has_media"] is False

    def test_process_reblog_marked_in_payload(self, collector):
        collector.process_post(FAKE_REBLOG)
        rows = collector.db.execute_fetchall(
            "SELECT payload FROM signals ORDER BY id DESC LIMIT 1"
        )
        payload = json.loads(rows[0]["payload"])
        assert payload.get("is_reblog") is True


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

class TestStateTracking:
    def test_get_last_post_id_none_initially(self, collector):
        assert collector.get_last_post_id() is None

    def test_set_and_get_last_post_id(self, collector):
        collector.set_last_post_id("110000000000000001")
        assert collector.get_last_post_id() == "110000000000000001"

    def test_set_last_post_id_updates(self, collector):
        collector.set_last_post_id("100")
        collector.set_last_post_id("200")
        assert collector.get_last_post_id() == "200"


# ---------------------------------------------------------------------------
# Backfill on startup
# ---------------------------------------------------------------------------

class TestBackfillOnStartup:
    def test_backfill_processes_missed_posts(self, collector, mock_client):
        collector.set_last_post_id(FAKE_POST_1["id"])
        mock_client.fetch_posts.return_value = [FAKE_POST_2, FAKE_POST_1]
        processed = collector.backfill("107780257626128497")
        assert processed == 1  # only FAKE_POST_2 is new

    def test_backfill_no_last_id_processes_all(self, collector, mock_client):
        mock_client.fetch_posts.return_value = [FAKE_POST_2, FAKE_POST_1]
        processed = collector.backfill("107780257626128497")
        assert processed == 2

    def test_backfill_updates_last_post_id(self, collector, mock_client):
        collector.set_last_post_id(FAKE_POST_1["id"])
        mock_client.fetch_posts.return_value = [FAKE_POST_2, FAKE_POST_1]
        collector.backfill("107780257626128497")
        assert collector.get_last_post_id() == FAKE_POST_2["id"]


# ---------------------------------------------------------------------------
# Rate limiting / backoff
# ---------------------------------------------------------------------------

class TestBackoffLogic:
    def test_backoff_sequence(self, collector):
        assert collector._backoff_seconds == [30, 60, 120, 300]

    def test_get_backoff_delay_first(self, collector):
        assert collector.get_backoff_delay(attempt=0) == 30

    def test_get_backoff_delay_second(self, collector):
        assert collector.get_backoff_delay(attempt=1) == 60

    def test_get_backoff_delay_caps_at_last(self, collector):
        assert collector.get_backoff_delay(attempt=99) == 300


# ---------------------------------------------------------------------------
# Keyword filtering
# ---------------------------------------------------------------------------

class TestKeywordFilter:
    def test_no_filter_matches_all(self, collector):
        assert collector._matches_keyword_filter("anything") is True

    def test_filter_matches(self, mock_config, mock_db, mock_client):
        mock_config.truth_social.keyword_filter = ["tariff", "oil"]
        mock_config.truth_social.alert_all_posts = False
        c = TruthSocialCollector(config=mock_config, db=mock_db, client=mock_client)
        assert c._matches_keyword_filter("New tariff announcement") is True

    def test_filter_no_match(self, mock_config, mock_db, mock_client):
        mock_config.truth_social.keyword_filter = ["tariff", "oil"]
        mock_config.truth_social.alert_all_posts = False
        c = TruthSocialCollector(config=mock_config, db=mock_db, client=mock_client)
        assert c._matches_keyword_filter("Good morning America") is False

    def test_filter_skips_post_in_process(self, mock_config, mock_db, mock_client):
        mock_config.truth_social.keyword_filter = ["tariff"]
        mock_config.truth_social.alert_all_posts = False
        c = TruthSocialCollector(config=mock_config, db=mock_db, client=mock_client)
        result = c.process_post(FAKE_POST_1)  # "We are winning!" — no match
        assert result is None


# ---------------------------------------------------------------------------
# Priority classification
# ---------------------------------------------------------------------------

from sentinel.collectors.truth_social import classify_priority

CRIT_KW = ["tariff", "china", "war", "fed"]
ENDORSE = ["endorse", "endorsement"]


class TestClassifyPriority:
    def test_market_keyword_is_critical(self):
        assert classify_priority(
            "Tariffs are great for America!", CRIT_KW, ENDORSE, "MEDIUM"
        ) == "CRITICAL"

    def test_endorsement_is_low(self):
        assert classify_priority(
            "It is my Great Honor to endorse Congressman Tom Kean!",
            CRIT_KW, ENDORSE, "MEDIUM",
        ) == "LOW"

    def test_neutral_post_is_default(self):
        assert classify_priority(
            "Beautiful day in Florida. MAGA!", CRIT_KW, ENDORSE, "MEDIUM"
        ) == "MEDIUM"

    def test_keyword_wins_over_endorsement(self):
        # A post that endorses but also mentions a market keyword stays CRITICAL
        assert classify_priority(
            "I endorse this tough new China tariff!", CRIT_KW, ENDORSE, "MEDIUM"
        ) == "CRITICAL"

    def test_empty_text_is_default(self):
        # Media-only post: unknown content -> default, never CRITICAL
        assert classify_priority("", CRIT_KW, ENDORSE, "MEDIUM") == "MEDIUM"

    def test_case_insensitive(self):
        assert classify_priority("FED RATE DECISION", CRIT_KW, ENDORSE, "MEDIUM") == "CRITICAL"


class TestProcessPostPriority:
    def test_endorsement_post_written_as_low(self, collector, mock_db):
        post = dict(FAKE_POST_1)
        post["content"] = "<p>It is my Great Honor to endorse Tom Kean!</p>"
        collector.process_post(post)
        sig = mock_db.get_recent_signals()[0]
        assert sig["priority"] == "LOW"

    def test_tariff_post_written_as_critical(self, collector, mock_db):
        collector.process_post(FAKE_POST_2)  # contains "Tariffs"
        sig = mock_db.get_recent_signals()[0]
        assert sig["priority"] == "CRITICAL"

    def test_neutral_post_written_as_medium(self, collector, mock_db):
        collector.process_post(FAKE_POST_1)  # "We are winning!"
        sig = mock_db.get_recent_signals()[0]
        assert sig["priority"] == "MEDIUM"
