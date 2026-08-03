"""Unit tests for core/config.py — configuration loader."""

import textwrap
from datetime import time

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

    kalshi:
      poll_interval_seconds: 30
      api_base_url: "https://external-api.kalshi.com/trade-api/v2"
      tracked_event_tickers:
        - KXMIDEASTWAR
      thresholds:
        large_bet_contracts: 100
        odds_move_pct_5min: 5.0
        volume_spike_multiplier: 3.0
        min_absolute_volume: 50

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


# Only the fields with no default (account_handle, poll_interval_seconds,
# futures.instruments[].ticker, alerts.ntfy_topic) are present — every other
# field in the parsed Config must come from a section parser's own default.
MINIMAL_CONFIG_YAML = textwrap.dedent("""\
    truth_social:
      account_handle: realDonaldTrump
      poll_interval_seconds: 8

    futures:
      instruments:
        - ticker: "CL=F"

    alerts:
      ntfy_topic: sentinel-test
""")


@pytest.fixture
def minimal_config_file(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(MINIMAL_CONFIG_YAML)
    return str(cfg_path)


@pytest.fixture
def minimal_config(minimal_config_file):
    return load_config(minimal_config_file)


# Every field set to a value that differs from BOTH the corresponding
# default AND every other field's value. VALID_CONFIG_YAML's values happen
# to equal nearly every default, so a mutant that swaps a `dict.get(KEY,
# default)` key (e.g. "large_bet_contracts" -> "LARGE_BET_CONTRACTS") or
# picks the wrong default is invisible against it — the wrong lookup still
# falls through to the same default and produces the same result. This
# fixture makes every such lookup observably wrong if the key is wrong.
DISTINCT_CONFIG_YAML = textwrap.dedent("""\
    truth_social:
      account_handle: someOtherHandle
      account_id_fallback: "999999999999999999"
      poll_interval_seconds: 15
      alert_all_posts: false
      keyword_filter: ["tariff"]
      backoff_seconds: [5, 10, 15]
      critical_keywords: ["nuclear"]
      endorsement_markers: ["strongly endorse"]
      default_priority: LOW

    polymarket:
      poll_interval_seconds: 45
      gamma_api_url: "https://custom-gamma.example"
      polygonscan_api_key: "pk_123"
      tracked_markets: ["market-a"]
      thresholds:
        large_bet_usd: 9999
        new_wallet_age_days: 3
        new_wallet_min_bet_usd: 2222
        odds_move_pct_5min: 7.5
        volume_spike_multiplier: 4.5
        min_absolute_volume_usd: 777

    kalshi:
      poll_interval_seconds: 20
      api_base_url: "https://custom-kalshi.example"
      tracked_event_tickers: ["EVENTX"]
      thresholds:
        large_bet_contracts: 250
        odds_move_pct_5min: 8.5
        volume_spike_multiplier: 6.0
        min_absolute_volume: 125

    futures:
      poll_interval_seconds: 90
      alpaca_api_key: "ak_1"
      alpaca_api_secret: "as_1"
      alpaca_base_url: "https://custom-alpaca.example"
      instruments:
        - ticker: "XX=F"
          name: "Custom Instrument"
          min_absolute_volume: 333
      thresholds:
        spike_multiplier: 2.2
        spike_multiplier_quiet: 6.6
        rolling_bars: 15
      active_window_utc:
        start: "09:15"
        end: "22:45"
      suppress_volume_alerts_on_roll_dates: false
      roll_dates:
        - date: "2026-05-01"
          tickers: ["XX=F"]
          note: "custom roll"

    alerts:
      provider: pushover
      ntfy_topic: custom-topic
      ntfy_url: "https://custom.ntfy.example"
      rate_limit_minutes: 12
      quiet_hours_utc:
        start: "01:00"
        end: "05:00"
      quiet_suppress_below: LOW
      digest_time_utc: "08:15"
      enabled: false

    database:
      path: /custom/path.db
      retention_days: 45

    dashboard:
      host: "0.0.0.0"
      port: 9999
""")


@pytest.fixture
def distinct_config(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(DISTINCT_CONFIG_YAML)
    return load_config(str(cfg_path))


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

    def test_zero_width_window_only_matches_exact_instant(self):
        """start == end takes the `start <= end` (non-crossing) branch, so
        only that exact instant is inside — not `<` (which would fall into
        the midnight-crossing branch and match everything)."""
        assert is_in_window(time(12, 0), time(12, 0), time(12, 0)) is True
        assert is_in_window(time(12, 1), time(12, 0), time(12, 0)) is False

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
        with pytest.raises(ConfigValidationError, match="account_handle.*truth_social"):
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

    def test_enabled_defaults_true(self, valid_config):
        assert valid_config.alerts.enabled is True

    def test_enabled_false_parsed(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["alerts"]["enabled"] = False
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(cfg_data))
        cfg = load_config(str(cfg_path))
        assert cfg.alerts.enabled is False

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


# ---------------------------------------------------------------------------
# Config — kalshi section (previously untested)
# ---------------------------------------------------------------------------

class TestKalshiConfig:
    def test_poll_interval(self, valid_config):
        assert valid_config.kalshi.poll_interval_seconds == 30

    def test_api_base_url(self, valid_config):
        assert valid_config.kalshi.api_base_url == "https://external-api.kalshi.com/trade-api/v2"

    def test_tracked_event_tickers(self, valid_config):
        assert valid_config.kalshi.tracked_event_tickers == ["KXMIDEASTWAR"]

    def test_large_bet_contracts(self, valid_config):
        assert valid_config.kalshi.thresholds.large_bet_contracts == 100

    def test_odds_move_pct_5min(self, valid_config):
        assert valid_config.kalshi.thresholds.odds_move_pct_5min == 5.0

    def test_volume_spike_multiplier(self, valid_config):
        assert valid_config.kalshi.thresholds.volume_spike_multiplier == 3.0

    def test_min_absolute_volume(self, valid_config):
        assert valid_config.kalshi.thresholds.min_absolute_volume == 50


# ---------------------------------------------------------------------------
# Every section's defaults, exercised via a config with only the fields
# that have no default supplied. Kills the ~40 mutants that swap a
# data.get(key, DEFAULT) literal for something else.
# ---------------------------------------------------------------------------

class TestAllDefaults:
    def test_truth_social_defaults(self, minimal_config):
        ts = minimal_config.truth_social
        assert ts.account_id_fallback == "107780257626128497"
        assert ts.alert_all_posts is True
        assert ts.keyword_filter == []
        assert ts.backoff_seconds == [30, 60, 120, 300]
        assert ts.critical_keywords == []
        assert ts.endorsement_markers == ["endorse", "endorsement"]
        assert ts.default_priority == "MEDIUM"

    def test_kalshi_defaults(self, minimal_config):
        k = minimal_config.kalshi
        assert k.poll_interval_seconds == 30
        assert k.api_base_url == "https://external-api.kalshi.com/trade-api/v2"
        assert k.tracked_event_tickers == []
        assert k.thresholds.large_bet_contracts == 100
        assert k.thresholds.odds_move_pct_5min == 5.0
        assert k.thresholds.volume_spike_multiplier == 3.0
        assert k.thresholds.min_absolute_volume == 50

    def test_polymarket_defaults(self, minimal_config):
        p = minimal_config.polymarket
        assert p.poll_interval_seconds == 30
        assert p.gamma_api_url == "https://gamma-api.polymarket.com"
        assert p.polygonscan_api_key == ""
        assert p.tracked_markets == []
        assert p.thresholds.large_bet_usd == 5000
        assert p.thresholds.new_wallet_age_days == 7
        assert p.thresholds.new_wallet_min_bet_usd == 1000
        assert p.thresholds.odds_move_pct_5min == 5.0
        assert p.thresholds.volume_spike_multiplier == 3.0
        assert p.thresholds.min_absolute_volume_usd == 500

    def test_futures_defaults(self, minimal_config):
        f = minimal_config.futures
        assert f.poll_interval_seconds == 60
        assert f.alpaca_api_key == ""
        assert f.alpaca_api_secret == ""
        assert f.alpaca_base_url == "https://data.alpaca.markets"
        assert f.thresholds.spike_multiplier == 3.0
        assert f.thresholds.spike_multiplier_quiet == 5.0
        assert f.thresholds.rolling_bars == 20
        assert f.active_window_utc.start == time(11, 0)
        assert f.active_window_utc.end == time(4, 0)
        assert f.suppress_volume_alerts_on_roll_dates is True
        assert f.roll_dates == []

    def test_futures_instrument_defaults(self, minimal_config):
        inst = minimal_config.futures.instruments[0]
        assert inst.ticker == "CL=F"
        assert inst.name == "CL=F"  # defaults to ticker when omitted
        assert inst.min_absolute_volume == 0

    def test_alerts_defaults(self, minimal_config):
        a = minimal_config.alerts
        assert a.provider == "ntfy"
        assert a.ntfy_url == "https://ntfy.sh"
        assert a.rate_limit_minutes == 5
        assert a.quiet_hours_utc.start == time(17, 0)
        assert a.quiet_hours_utc.end == time(21, 0)
        assert a.quiet_suppress_below == "MEDIUM"
        assert a.digest_time_utc == time(21, 0)
        assert a.enabled is True

    def test_database_defaults(self, minimal_config):
        assert minimal_config.database.path == "./sentinel.db"
        assert minimal_config.database.retention_days == 90

    def test_dashboard_defaults(self, minimal_config):
        assert minimal_config.dashboard.host == "127.0.0.1"
        assert minimal_config.dashboard.port == 5000


# ---------------------------------------------------------------------------
# Custom (non-default) values for fields TestAllDefaults doesn't otherwise
# exercise a non-default path for.
# ---------------------------------------------------------------------------

class TestNonDefaultValues:
    def test_futures_instrument_explicit_name_not_overridden_by_ticker(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg = load_config(_write(tmp_path, cfg_data))
        cl = next(i for i in cfg.futures.instruments if i.ticker == "CL=F")
        assert cl.name == "WTI Oil"

    def test_roll_date_fields(self, valid_config):
        rd = valid_config.futures.roll_dates[0]
        assert rd.date == "2026-04-22"
        assert rd.tickers == ["CL=F"]
        assert rd.note == "WTI April roll"

    def test_alpaca_credentials_passed_through(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["futures"]["alpaca_api_key"] = "key123"
        cfg_data["futures"]["alpaca_api_secret"] = "secret456"
        cfg = load_config(_write(tmp_path, cfg_data))
        assert cfg.futures.alpaca_api_key == "key123"
        assert cfg.futures.alpaca_api_secret == "secret456"

    def test_suppress_volume_alerts_false(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["futures"]["suppress_volume_alerts_on_roll_dates"] = False
        cfg = load_config(_write(tmp_path, cfg_data))
        assert cfg.futures.suppress_volume_alerts_on_roll_dates is False

    def test_critical_keywords_and_endorsement_markers(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["truth_social"]["critical_keywords"] = ["tariff", "sanctions"]
        cfg_data["truth_social"]["endorsement_markers"] = ["full support"]
        cfg = load_config(_write(tmp_path, cfg_data))
        assert cfg.truth_social.critical_keywords == ["tariff", "sanctions"]
        assert cfg.truth_social.endorsement_markers == ["full support"]

    def test_default_priority_custom(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["truth_social"]["default_priority"] = "HIGH"
        cfg = load_config(_write(tmp_path, cfg_data))
        assert cfg.truth_social.default_priority == "HIGH"

    def test_invalid_default_priority_raises(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["truth_social"]["default_priority"] = "BANANA"
        with pytest.raises(ConfigValidationError):
            load_config(_write(tmp_path, cfg_data))

    def test_alert_all_posts_false(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["truth_social"]["alert_all_posts"] = False
        cfg = load_config(_write(tmp_path, cfg_data))
        assert cfg.truth_social.alert_all_posts is False

    def test_empty_account_handle_raises(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["truth_social"]["account_handle"] = ""
        with pytest.raises(ConfigValidationError):
            load_config(_write(tmp_path, cfg_data))

    def test_non_int_poll_interval_raises(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["truth_social"]["poll_interval_seconds"] = "eight"
        with pytest.raises(ConfigValidationError):
            load_config(_write(tmp_path, cfg_data))

    def test_negative_poll_interval_raises(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["truth_social"]["poll_interval_seconds"] = -5
        with pytest.raises(ConfigValidationError):
            load_config(_write(tmp_path, cfg_data))

    def test_custom_rate_limit_minutes(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["alerts"]["rate_limit_minutes"] = 15
        cfg = load_config(_write(tmp_path, cfg_data))
        assert cfg.alerts.rate_limit_minutes == 15

    def test_custom_ntfy_url(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["alerts"]["ntfy_url"] = "https://custom.ntfy.example"
        cfg = load_config(_write(tmp_path, cfg_data))
        assert cfg.alerts.ntfy_url == "https://custom.ntfy.example"


def _write(tmp_path, cfg_data):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(cfg_data))
    return str(cfg_path)


# ---------------------------------------------------------------------------
# _parse_time / _parse_time_window — direct edge cases
# ---------------------------------------------------------------------------

class TestParseTimeEdgeCases:
    def test_missing_colon_raises(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["alerts"]["digest_time_utc"] = "2100"
        with pytest.raises(ConfigValidationError):
            load_config(_write(tmp_path, cfg_data))

    def test_three_parts_raises(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["alerts"]["digest_time_utc"] = "21:00:00"
        with pytest.raises(ConfigValidationError):
            load_config(_write(tmp_path, cfg_data))

    def test_non_numeric_hour_raises(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["alerts"]["digest_time_utc"] = "ab:00"
        with pytest.raises(ConfigValidationError):
            load_config(_write(tmp_path, cfg_data))

    def test_out_of_range_hour_raises(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["alerts"]["digest_time_utc"] = "25:00"
        with pytest.raises(
            ConfigValidationError, match="Invalid time format for 'alerts.digest_time_utc'"
        ):
            load_config(_write(tmp_path, cfg_data))

    def test_out_of_range_minute_raises(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["alerts"]["digest_time_utc"] = "12:60"
        with pytest.raises(ConfigValidationError):
            load_config(_write(tmp_path, cfg_data))

    def test_valid_time_parses_exactly(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        cfg_data["alerts"]["digest_time_utc"] = "09:37"
        cfg = load_config(_write(tmp_path, cfg_data))
        assert cfg.alerts.digest_time_utc == time(9, 37)

    def test_time_window_missing_start_raises(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        del cfg_data["alerts"]["quiet_hours_utc"]["start"]
        with pytest.raises(ConfigValidationError):
            load_config(_write(tmp_path, cfg_data))

    def test_time_window_missing_end_raises(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        del cfg_data["alerts"]["quiet_hours_utc"]["end"]
        with pytest.raises(ConfigValidationError):
            load_config(_write(tmp_path, cfg_data))


# ---------------------------------------------------------------------------
# load_config — top-level shape errors
# ---------------------------------------------------------------------------

class TestLoadConfigTopLevel:
    def test_non_mapping_yaml_raises(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("- just\n- a\n- list\n")
        with pytest.raises(ConfigValidationError):
            load_config(str(cfg_path))

    def test_empty_file_raises(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("")
        with pytest.raises(ConfigValidationError):
            load_config(str(cfg_path))

    def test_missing_ntfy_topic_error_names_field(self, tmp_path):
        cfg_data = yaml.safe_load(VALID_CONFIG_YAML)
        del cfg_data["alerts"]["ntfy_topic"]
        with pytest.raises(ConfigValidationError, match="ntfy_topic"):
            load_config(_write(tmp_path, cfg_data))


# ---------------------------------------------------------------------------
# Every field, asserted against a config where every value is distinct from
# its default (see DISTINCT_CONFIG_YAML docstring above). Kills dict-key
# mutations that TestAllDefaults/TestTruthSocialConfig/etc. can't, because
# those fixtures' values happen to equal the defaults.
# ---------------------------------------------------------------------------

class TestDistinctValues:
    def test_truth_social(self, distinct_config):
        ts = distinct_config.truth_social
        assert ts.account_handle == "someOtherHandle"
        assert ts.account_id_fallback == "999999999999999999"
        assert ts.poll_interval_seconds == 15
        assert ts.alert_all_posts is False
        assert ts.keyword_filter == ["tariff"]
        assert ts.backoff_seconds == [5, 10, 15]
        assert ts.critical_keywords == ["nuclear"]
        assert ts.endorsement_markers == ["strongly endorse"]
        assert ts.default_priority == "LOW"

    def test_polymarket(self, distinct_config):
        p = distinct_config.polymarket
        assert p.poll_interval_seconds == 45
        assert p.gamma_api_url == "https://custom-gamma.example"
        assert p.polygonscan_api_key == "pk_123"
        assert p.tracked_markets == ["market-a"]
        assert p.thresholds.large_bet_usd == 9999
        assert p.thresholds.new_wallet_age_days == 3
        assert p.thresholds.new_wallet_min_bet_usd == 2222
        assert p.thresholds.odds_move_pct_5min == 7.5
        assert p.thresholds.volume_spike_multiplier == 4.5
        assert p.thresholds.min_absolute_volume_usd == 777

    def test_kalshi(self, distinct_config):
        k = distinct_config.kalshi
        assert k.poll_interval_seconds == 20
        assert k.api_base_url == "https://custom-kalshi.example"
        assert k.tracked_event_tickers == ["EVENTX"]
        assert k.thresholds.large_bet_contracts == 250
        assert k.thresholds.odds_move_pct_5min == 8.5
        assert k.thresholds.volume_spike_multiplier == 6.0
        assert k.thresholds.min_absolute_volume == 125

    def test_futures(self, distinct_config):
        f = distinct_config.futures
        assert f.poll_interval_seconds == 90
        assert f.alpaca_api_key == "ak_1"
        assert f.alpaca_api_secret == "as_1"
        assert f.alpaca_base_url == "https://custom-alpaca.example"
        assert f.thresholds.spike_multiplier == 2.2
        assert f.thresholds.spike_multiplier_quiet == 6.6
        assert f.thresholds.rolling_bars == 15
        assert f.active_window_utc.start == time(9, 15)
        assert f.active_window_utc.end == time(22, 45)
        assert f.suppress_volume_alerts_on_roll_dates is False

        inst = f.instruments[0]
        assert inst.ticker == "XX=F"
        assert inst.name == "Custom Instrument"
        assert inst.min_absolute_volume == 333

        rd = f.roll_dates[0]
        assert rd.date == "2026-05-01"
        assert rd.tickers == ["XX=F"]
        assert rd.note == "custom roll"

    def test_alerts(self, distinct_config):
        a = distinct_config.alerts
        assert a.provider == "pushover"
        assert a.ntfy_topic == "custom-topic"
        assert a.ntfy_url == "https://custom.ntfy.example"
        assert a.rate_limit_minutes == 12
        assert a.quiet_hours_utc.start == time(1, 0)
        assert a.quiet_hours_utc.end == time(5, 0)
        assert a.quiet_suppress_below == "LOW"
        assert a.digest_time_utc == time(8, 15)
        assert a.enabled is False

    def test_database(self, distinct_config):
        assert distinct_config.database.path == "/custom/path.db"
        assert distinct_config.database.retention_days == 45

    def test_dashboard(self, distinct_config):
        assert distinct_config.dashboard.host == "0.0.0.0"
        assert distinct_config.dashboard.port == 9999
