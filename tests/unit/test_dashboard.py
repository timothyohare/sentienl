"""Basic tests for the Flask dashboard."""

import json
import textwrap

import pytest

from sentinel.core.db import Database


@pytest.fixture
def mock_db(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.init()
    yield db
    db.close()


@pytest.fixture
def config_file(tmp_path):
    cfg_content = textwrap.dedent("""\
        truth_social:
          account_handle: realDonaldTrump
          account_id_fallback: "107780257626128497"
          poll_interval_seconds: 8
          alert_all_posts: true
          keyword_filter: []
          backoff_seconds: [30, 60, 120, 300]
        polymarket:
          poll_interval_seconds: 30
          gamma_api_url: "https://gamma-api.polymarket.com"
          polygonscan_api_key: ""
          tracked_markets: []
          thresholds:
            large_bet_usd: 5000
            new_wallet_age_days: 7
            new_wallet_min_bet_usd: 1000
            odds_move_pct_5min: 5.0
            volume_spike_multiplier: 3.0
            min_absolute_volume_usd: 500
        futures:
          poll_interval_seconds: 60
          alpaca_api_key: ""
          alpaca_api_secret: ""
          alpaca_base_url: "https://data.alpaca.markets"
          instruments:
            - ticker: "CL=F"
              name: "WTI Oil"
              min_absolute_volume: 500
          thresholds:
            spike_multiplier: 3.0
            spike_multiplier_quiet: 5.0
            rolling_bars: 20
          active_window_utc:
            start: "11:00"
            end: "04:00"
          suppress_volume_alerts_on_roll_dates: true
          roll_dates: []
        alerts:
          provider: ntfy
          ntfy_topic: sentinel-test
          ntfy_url: https://ntfy.sh
          rate_limit_minutes: 5
          quiet_hours_utc:
            start: "17:00"
            end: "21:00"
          quiet_suppress_below: MEDIUM
          digest_time_utc: "21:00"
        database:
          path: ./sentinel.db
          retention_days: 90
        dashboard:
          host: "127.0.0.1"
          port: 5000
    """)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(cfg_content)
    return str(cfg_path)


@pytest.fixture
def app(mock_db, config_file):
    from sentinel.dashboard.app import create_app
    application = create_app(db=mock_db, config_path=config_file)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


class TestToAest:
    def test_z_suffix_converted(self):
        from sentinel.dashboard.app import _to_aest
        assert _to_aest("2026-03-27T00:00:00Z") == "27 Mar 2026 10:00:00 AEST"

    def test_offset_suffix_converted(self):
        from sentinel.dashboard.app import _to_aest
        assert _to_aest("2026-03-27T00:00:00+00:00") == "27 Mar 2026 10:00:00 AEST"

    def test_naive_treated_as_utc(self):
        from sentinel.dashboard.app import _to_aest
        assert _to_aest("2026-03-27T00:00:00") == "27 Mar 2026 10:00:00 AEST"

    def test_unparseable_returns_original_string(self):
        from sentinel.dashboard.app import _to_aest
        assert _to_aest("not-a-timestamp") == "not-a-timestamp"

    def test_offset_is_added_not_subtracted(self):
        from sentinel.dashboard.app import _to_aest
        # 23:00 UTC + 10h = 09:00 AEST the next day
        assert _to_aest("2026-03-27T23:00:00+00:00") == "28 Mar 2026 09:00:00 AEST"


class TestEnrichSignal:
    def test_adds_created_at_aest(self):
        from sentinel.dashboard.app import _enrich_signal
        result = _enrich_signal({"created_at": "2026-03-27T00:00:00Z", "priority": "HIGH"})
        assert result["created_at_aest"] == "27 Mar 2026 10:00:00 AEST"

    def test_missing_created_at_defaults_empty(self):
        from sentinel.dashboard.app import _enrich_signal
        result = _enrich_signal({"priority": "HIGH"})
        assert result["created_at_aest"] == ""

    def test_colour_per_priority(self):
        from sentinel.dashboard.app import _enrich_signal
        assert _enrich_signal({"priority": "CRITICAL"})["colour"] == "#dc2626"
        assert _enrich_signal({"priority": "HIGH"})["colour"] == "#ea580c"
        assert _enrich_signal({"priority": "MEDIUM"})["colour"] == "#ca8a04"
        assert _enrich_signal({"priority": "LOW"})["colour"] == "#2563eb"
        assert _enrich_signal({"priority": "INFO"})["colour"] == "#6b7280"

    def test_missing_priority_defaults_to_info_colour(self):
        from sentinel.dashboard.app import _enrich_signal
        assert _enrich_signal({})["colour"] == "#6b7280"

    def test_unknown_priority_defaults_to_grey(self):
        from sentinel.dashboard.app import _enrich_signal
        assert _enrich_signal({"priority": "BOGUS"})["colour"] == "#6b7280"

    def test_dict_payload_pretty_printed_json(self):
        from sentinel.dashboard.app import _enrich_signal
        result = _enrich_signal({"payload": {"a": 1}})
        assert result["payload_json"] == json.dumps({"a": 1}, indent=2)

    def test_non_dict_payload_stringified(self):
        from sentinel.dashboard.app import _enrich_signal
        result = _enrich_signal({"payload": "raw string"})
        assert result["payload_json"] == "raw string"

    def test_missing_payload_defaults_empty_json_object(self):
        from sentinel.dashboard.app import _enrich_signal
        result = _enrich_signal({})
        assert result["payload_json"] == "{}"

    def test_does_not_mutate_input_dict(self):
        from sentinel.dashboard.app import _enrich_signal
        original = {"priority": "HIGH"}
        _enrich_signal(original)
        assert "colour" not in original


class TestDashboardRoutes:
    def test_home_returns_200(self, client):
        response = client.get("/")
        assert response.status_code == 200

    def test_signals_returns_200(self, client):
        response = client.get("/signals")
        assert response.status_code == 200

    def test_health_returns_200(self, client):
        response = client.get("/health")
        assert response.status_code == 200

    def test_truth_returns_200(self, client):
        response = client.get("/truth")
        assert response.status_code == 200

    def test_polymarket_returns_200(self, client):
        response = client.get("/polymarket")
        assert response.status_code == 200

    def test_home_contains_sentinel(self, client):
        response = client.get("/")
        assert b"Sentinel" in response.data or b"sentinel" in response.data

    def test_health_returns_json_for_api(self, client):
        response = client.get("/health?format=json")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "status" in data

    def test_signals_page_shows_signals(self, client, mock_db):
        mock_db.insert_signal(
            "truth_social", "new_post", "CRITICAL",
            {"post_id": "1", "text": "Test post", "url": "http://x",
             "has_media": False, "is_reblog": False},
            "New Trump post [1]: Test post"
        )
        response = client.get("/signals")
        assert response.status_code == 200
        assert b"truth_social" in response.data or b"Test post" in response.data

    def test_truth_page_shows_posts(self, client, mock_db):
        mock_db.insert_signal(
            "truth_social", "new_post", "CRITICAL",
            {"post_id": "99", "text": "Hello from truth social", "url": "http://x",
             "has_media": False, "is_reblog": False},
            "New Trump post [99]: Hello from truth social"
        )
        response = client.get("/truth")
        assert response.status_code == 200

    def test_404_for_unknown_route(self, client):
        response = client.get("/nonexistent-route")
        assert response.status_code == 404
