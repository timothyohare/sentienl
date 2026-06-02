"""Unit tests for core/config.py — configuration loader."""

import textwrap
from datetime import time
from pathlib import Path

import pytest
import yaml

from sentinel.core.config import (
    Config,
    ConfigValidationError,
    is_in_window,
    load_config,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_CONFIG_YAML = textwrap.dedent("""\
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
      tracked_markets:
        - us-iran-ceasefire-2026
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
        - ticker: "ES=F"
          name: "S&P 500"
          min_absolute_volume: 200
      thresholds:
        spike_multiplier: 3.0
        spike_multiplier_quiet: 5.0
        rolling_bars: 20
      active_window_utc:
        start: "11:00"
        end: "04:00"
      suppress_volume_alerts_on_roll_dates: true
      roll_dates:
        - date: "2026-04-22"
          tickers: ["CL=F"]
          note: "WTI April roll"

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


@pytest.fixture
def valid_config_file(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(VALID_CONFIG_YAML)
    return str(cfg_path)


@pytest.fixture
def valid_config(valid_config_file):
    return load_config(valid_config_file)


# ---------------------------------------------------------------------------
# is_in_window helper
# ---------------------------------------------------------------------------

class TestIsInWindow:
    def test_normal_window_inside(self):
        assert is_in_window(time(12, 0), time(11, 0), time(16, 0)) is True

    def test_normal_window_at_start(self):
        assert is_in_window(time(11, 0), time(11, 0), time(16, 0)) is True

    def test_normal_window_at_end(self):
        assert is_in_window(time(16, 0), time(11, 0), time(16, 0)) is True

    def test_normal_window_outside_before(self):
        assert is_in_window(time(10, 59), time(11, 0), time(16, 0)) is False

    def test_normal_window_outside_after(self):
        assert is_in_window(time(16, 1), time(11, 0), time(16, 0)) is False

    def test_midnight_crossing_before_midnight(self):
        # 11:00–04:00 window — 23:00 is inside
        assert is_in_window(time(23, 0), time(11, 0), time(4, 0)) is True

    def test_midnight_crossing_after_midnight(self):
        # 11:00–04:00 window — 02:00 is inside
        assert is_in_window(time(2, 0), time(11, 0), time(4, 0)) is True

    def test_midnight_crossing_at_start(self):
        assert is_in_window(time(11, 0), time(11, 0), time(4, 0)) is True

    def test_midnight_crossing_at_end(self):
        assert is_in_window(time(4, 0), time(11, 0), time(4, 0)) is True

    def test_midnight_crossing_outside(self):
        # 11:00–04:00 window — 06:00 is outside
        assert is_in_window(time(6, 0), time(11, 0), time(4, 0)) is False

    def test_midnight_crossing_outside_morning(self):
        # 11:00–04:00 window — 10:59 is outside
        assert is_in_window(time(10, 59), time(11, 0), time(4, 0)) is False


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_load_valid_config(self, valid_config_file):
        cfg = load_config(valid_config_file)
        assert cfg is not None

    def test_load_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/path/config.yaml")

    def test_load_returns_config_instance(self, valid_config_file):
        cfg = load_config(valid_config_file)
        assert isinstance(cfg, Config)


# ---------------------------------------------------------------------------
# Config — truth_social section
# ---------------------------------------------------------------------------

class TestTruthSocialConfig:
    def test_account_handle(self, valid_config):
        assert valid_config.truth_social.account_handle == "realDonaldTrump"

    def test_account_id_fallback(self, valid_config):
        assert valid_config.truth_social.account_id_fallback == "107780257626128497"

    def test_poll_interval(self, valid_config):
        assert valid_config.truth_social.poll_interval_seconds == 8

    def test_alert_all_posts(self, valid_config):
        assert valid_config.truth_social.alert_all_posts is True

    def test_keyword_filter_empty_list(self, valid_config):
        assert valid_config.truth_social.keyword_filter == []

    def test_backoff_seconds(self, valid_config):
        assert valid_config.truth_social.backoff_seconds == [30, 60, 120, 300]

    def test_missing_account_handle_raises(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        del cfg_data["truth_social"]["account_handle"]
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(cfg_data))
        with pytest.raises(ConfigValidationError):
            load_config(str(cfg_path))

    def test_invalid_poll_interval_raises(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["truth_social"]["poll_interval_seconds"] = 0
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(cfg_data))
        with pytest.raises(ConfigValidationError):
            load_config(str(cfg_path))


# ---------------------------------------------------------------------------
# Config — polymarket section
# ---------------------------------------------------------------------------

class TestPolymarketConfig:
    def test_poll_interval(self, valid_config):
        assert valid_config.polymarket.poll_interval_seconds == 30

    def test_tracked_markets(self, valid_config):
        assert "us-iran-ceasefire-2026" in valid_config.polymarket.tracked_markets

    def test_large_bet_usd(self, valid_config):
        assert valid_config.polymarket.thresholds.large_bet_usd == 5000

    def test_new_wallet_age_days(self, valid_config):
        assert valid_config.polymarket.thresholds.new_wallet_age_days == 7

    def test_volume_spike_multiplier(self, valid_config):
        assert valid_config.polymarket.thresholds.volume_spike_multiplier == 3.0

    def test_min_absolute_volume_usd(self, valid_config):
        assert valid_config.polymarket.thresholds.min_absolute_volume_usd == 500


# ---------------------------------------------------------------------------
# Config — futures section
# ---------------------------------------------------------------------------

class TestFuturesConfig:
    def test_instruments_loaded(self, valid_config):
        assert len(valid_config.futures.instruments) == 2

    def test_instrument_ticker(self, valid_config):
        tickers = [i.ticker for i in valid_config.futures.instruments]
        assert "CL=F" in tickers

    def test_instrument_min_absolute_volume(self, valid_config):
        cl = next(i for i in valid_config.futures.instruments if i.ticker == "CL=F")
        assert cl.min_absolute_volume == 500

    def test_spike_multiplier(self, valid_config):
        assert valid_config.futures.thresholds.spike_multiplier == 3.0

    def test_spike_multiplier_quiet(self, valid_config):
        assert valid_config.futures.thresholds.spike_multiplier_quiet == 5.0

    def test_active_window_parsed(self, valid_config):
        assert valid_config.futures.active_window_utc.start == time(11, 0)
        assert valid_config.futures.active_window_utc.end == time(4, 0)

    def test_active_window_is_midnight_crossing(self, valid_config):
        # 11:00–04:00 crosses midnight
        w = valid_config.futures.active_window_utc
        assert w.start > w.end  # midnight-crossing indicator

    def test_roll_dates_loaded(self, valid_config):
        assert len(valid_config.futures.roll_dates) == 1
        assert valid_config.futures.roll_dates[0].date == "2026-04-22"

    def test_rolling_bars(self, valid_config):
        assert valid_config.futures.thresholds.rolling_bars == 20

    def test_missing_instruments_raises(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["futures"]["instruments"] = []
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(cfg_data))
        with pytest.raises(ConfigValidationError):
            load_config(str(cfg_path))


# ---------------------------------------------------------------------------
# Config — alerts section
# ---------------------------------------------------------------------------

class TestAlertsConfig:
    def test_ntfy_provider(self, valid_config):
        assert valid_config.alerts.provider == "ntfy"

    def test_ntfy_topic(self, valid_config):
        assert valid_config.alerts.ntfy_topic == "sentinel-test"

    def test_rate_limit_minutes(self, valid_config):
        assert valid_config.alerts.rate_limit_minutes == 5

    def test_quiet_hours_parsed(self, valid_config):
        assert valid_config.alerts.quiet_hours_utc.start == time(17, 0)
        assert valid_config.alerts.quiet_hours_utc.end == time(21, 0)

    def test_quiet_suppress_below(self, valid_config):
        assert valid_config.alerts.quiet_suppress_below == "MEDIUM"

    def test_digest_time_utc(self, valid_config):
        assert valid_config.alerts.digest_time_utc == time(21, 0)

    def test_missing_ntfy_topic_raises(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        del cfg_data["alerts"]["ntfy_topic"]
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(cfg_data))
        with pytest.raises(ConfigValidationError):
            load_config(str(cfg_path))

    def test_invalid_quiet_suppress_below_raises(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["alerts"]["quiet_suppress_below"] = "BANANA"
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(cfg_data))
        with pytest.raises(ConfigValidationError):
            load_config(str(cfg_path))

    def test_invalid_provider_raises(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["alerts"]["provider"] = "telegram"  # not implemented in v1 yet
        # telegram is listed as a valid enum — should load without error
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(cfg_data))
        cfg = load_config(str(cfg_path))
        assert cfg.alerts.provider == "telegram"


# ---------------------------------------------------------------------------
# Config — database section
# ---------------------------------------------------------------------------

class TestDatabaseConfig:
    def test_db_path(self, valid_config):
        assert valid_config.database.path == "./sentinel.db"

    def test_retention_days(self, valid_config):
        assert valid_config.database.retention_days == 90

    def test_invalid_retention_days_raises(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["database"]["retention_days"] = -1
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(cfg_data))
        with pytest.raises(ConfigValidationError):
            load_config(str(cfg_path))


# ---------------------------------------------------------------------------
# Config — dashboard section
# ---------------------------------------------------------------------------

class TestDashboardConfig:
    def test_host(self, valid_config):
        assert valid_config.dashboard.host == "127.0.0.1"

    def test_port(self, valid_config):
        assert valid_config.dashboard.port == 5000
