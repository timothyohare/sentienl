"""
Unit tests for collectors/kalshi.py.

All tests use unittest.mock to patch the httpx client directly.
"""

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from sentinel.collectors.kalshi import (
    KALSHI_API_BASE,
    KalshiCollector,
    _calculate_volume_spike,
    _is_large_bet,
    _is_odds_move,
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
    cfg.kalshi.poll_interval_seconds = 30
    cfg.kalshi.api_base_url = KALSHI_API_BASE
    cfg.kalshi.tracked_event_tickers = ["KXMIDEASTWAR", "KXTRUMPTARIFF"]
    cfg.kalshi.thresholds.large_bet_contracts = 100
    cfg.kalshi.thresholds.odds_move_pct_5min = 5.0
    cfg.kalshi.thresholds.volume_spike_multiplier = 3.0
    cfg.kalshi.thresholds.min_absolute_volume = 50
    return cfg


@pytest.fixture
def collector(mock_config, mock_db):
    return KalshiCollector(config=mock_config, db=mock_db)


# Sample data matching Kalshi API response structure
SAMPLE_MARKET = {
    "ticker": "KXMIDEASTWAR-26JUN15",
    "event_ticker": "KXMIDEASTWAR",
    "title": "Will there be a major military conflict in the Middle East by June 15?",
    "status": "active",
    "last_price_dollars": "0.3500",
    "yes_bid_dollars": "0.3400",
    "yes_ask_dollars": "0.3600",
    "volume_fp": "5000.00",
    "volume_24h_fp": "800.00",
    "open_interest_fp": "1200.00",
    "created_time": (datetime.now(UTC) - timedelta(days=30)).isoformat(),
}

SAMPLE_TRADE = {
    "trade_id": "abc-123-def",
    "ticker": "KXMIDEASTWAR-26JUN15",
    "yes_price_dollars": "0.3500",
    "no_price_dollars": "0.6500",
    "count_fp": "150.00",
    "taker_side": "yes",
    "taker_book_side": "bid",
    "taker_outcome_side": "yes",
    "created_time": "2026-06-02T10:00:00.000Z",
}


# ---------------------------------------------------------------------------
# Signal detection helpers
# ---------------------------------------------------------------------------

class TestIsLargeBet:
    def test_above_threshold(self):
        assert _is_large_bet(150.0, threshold=100) is True

    def test_at_threshold(self):
        assert _is_large_bet(100.0, threshold=100) is True

    def test_below_threshold(self):
        assert _is_large_bet(99.9, threshold=100) is False

    def test_zero(self):
        assert _is_large_bet(0.0, threshold=100) is False


class TestIsOddsMove:
    def test_above_threshold(self):
        assert _is_odds_move(previous=0.30, current=0.36, threshold_pct=5.0) is True

    def test_exactly_at_threshold(self):
        # Use values that don't have floating point precision issues
        assert _is_odds_move(previous=0.50, current=0.55, threshold_pct=5.0) is True

    def test_below_threshold(self):
        assert _is_odds_move(previous=0.30, current=0.34, threshold_pct=5.0) is False

    def test_negative_move(self):
        assert _is_odds_move(previous=0.40, current=0.34, threshold_pct=5.0) is True

    def test_no_previous_returns_false(self):
        assert _is_odds_move(previous=None, current=0.35, threshold_pct=5.0) is False


class TestVolumeSpike:
    def test_above_threshold(self):
        result = _calculate_volume_spike(
            current_volume=600, baseline_volume=150, multiplier=3.0, min_absolute=50
        )
        assert result is not None
        assert result["ratio"] >= 3.0

    def test_below_multiplier(self):
        result = _calculate_volume_spike(
            current_volume=400, baseline_volume=150, multiplier=3.0, min_absolute=50
        )
        assert result is None

    def test_below_absolute_minimum(self):
        result = _calculate_volume_spike(
            current_volume=30, baseline_volume=5, multiplier=3.0, min_absolute=50
        )
        assert result is None

    def test_zero_baseline(self):
        result = _calculate_volume_spike(
            current_volume=100, baseline_volume=0, multiplier=3.0, min_absolute=50
        )
        assert result is None

    def test_returns_ratio(self):
        result = _calculate_volume_spike(
            current_volume=500, baseline_volume=100, multiplier=3.0, min_absolute=50
        )
        assert result is not None
        assert result["ratio"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Helper to create a mock httpx response
# ---------------------------------------------------------------------------

def _mock_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    return resp


# ---------------------------------------------------------------------------
# API fetching
# ---------------------------------------------------------------------------

class TestFetchEventMarkets:
    def test_fetch_markets_success(self, collector):
        collector._client.get = MagicMock(
            return_value=_mock_response(200, {"markets": [SAMPLE_MARKET], "cursor": ""})
        )
        markets = collector.fetch_event_markets("KXMIDEASTWAR")
        assert len(markets) == 1
        assert markets[0]["ticker"] == "KXMIDEASTWAR-26JUN15"

    def test_fetch_markets_empty(self, collector):
        collector._client.get = MagicMock(
            return_value=_mock_response(200, {"markets": [], "cursor": ""})
        )
        markets = collector.fetch_event_markets("KXNONEXISTENT")
        assert markets == []

    def test_fetch_markets_api_error(self, collector):
        collector._client.get = MagicMock(
            return_value=_mock_response(500)
        )
        markets = collector.fetch_event_markets("KXMIDEASTWAR")
        assert markets == []

    def test_fetch_markets_network_error(self, collector):
        collector._client.get = MagicMock(
            side_effect=ConnectionError("Connection refused")
        )
        markets = collector.fetch_event_markets("KXMIDEASTWAR")
        assert markets == []


class TestFetchRecentTrades:
    def test_fetch_trades_success(self, collector):
        collector._client.get = MagicMock(
            return_value=_mock_response(200, {"trades": [SAMPLE_TRADE], "cursor": ""})
        )
        trades = collector.fetch_recent_trades("KXMIDEASTWAR-26JUN15")
        assert len(trades) == 1

    def test_fetch_trades_empty(self, collector):
        collector._client.get = MagicMock(
            return_value=_mock_response(200, {"trades": [], "cursor": ""})
        )
        trades = collector.fetch_recent_trades("KXMIDEASTWAR-26JUN15")
        assert trades == []

    def test_fetch_trades_error(self, collector):
        collector._client.get = MagicMock(
            return_value=_mock_response(503)
        )
        trades = collector.fetch_recent_trades("KXMIDEASTWAR-26JUN15")
        assert trades == []


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

class TestKalshiState:
    def test_get_last_trade_id_none_initially(self, collector):
        assert collector.get_last_trade_id("KXMIDEASTWAR-26JUN15") is None

    def test_set_and_get_last_trade_id(self, collector):
        collector.set_last_trade_id("KXMIDEASTWAR-26JUN15", "abc-123")
        assert collector.get_last_trade_id("KXMIDEASTWAR-26JUN15") == "abc-123"

    def test_get_previous_price_none_initially(self, collector):
        assert collector.get_previous_price("KXMIDEASTWAR-26JUN15") is None

    def test_set_and_get_previous_price(self, collector):
        collector.set_previous_price("KXMIDEASTWAR-26JUN15", 0.35)
        assert collector.get_previous_price("KXMIDEASTWAR-26JUN15") == pytest.approx(0.35)


# ---------------------------------------------------------------------------
# Signal generation (integration through process_market)
# ---------------------------------------------------------------------------

class TestProcessMarket:
    def _mock_trades(self, collector, trades):
        """Helper to mock the fetch_recent_trades method."""
        collector.fetch_recent_trades = MagicMock(return_value=trades)

    def test_large_bet_creates_signal(self, collector, mock_db):
        large_trade = dict(SAMPLE_TRADE)
        large_trade["count_fp"] = "200.00"  # > 100 threshold
        self._mock_trades(collector, [large_trade])
        collector.process_market(SAMPLE_MARKET)
        signals = mock_db.get_recent_signals()
        assert any(s["signal_type"] == "large_bet" and s["source"] == "kalshi" for s in signals)

    def test_no_signal_for_small_trade(self, collector, mock_db):
        small_trade = dict(SAMPLE_TRADE)
        small_trade["count_fp"] = "10.00"  # < 100 threshold
        self._mock_trades(collector, [small_trade])
        collector.process_market(SAMPLE_MARKET)
        signals = mock_db.get_recent_signals()
        large_bet_signals = [s for s in signals if s["signal_type"] == "large_bet"]
        assert len(large_bet_signals) == 0

    def test_inactive_market_skipped(self, collector, mock_db):
        inactive = dict(SAMPLE_MARKET)
        inactive["status"] = "closed"
        collector.process_market(inactive)
        signals = mock_db.get_recent_signals()
        assert len(signals) == 0

    def test_odds_move_creates_signal(self, collector, mock_db):
        ticker = SAMPLE_MARKET["ticker"]
        collector.set_previous_price(ticker, 0.25)

        moved_market = dict(SAMPLE_MARKET)
        moved_market["last_price_dollars"] = "0.3500"  # +10pp move

        self._mock_trades(collector, [])
        collector.process_market(moved_market)
        signals = mock_db.get_recent_signals()
        assert any(s["signal_type"] == "odds_move" and s["source"] == "kalshi" for s in signals)

    def test_no_odds_move_below_threshold(self, collector, mock_db):
        ticker = SAMPLE_MARKET["ticker"]
        collector.set_previous_price(ticker, 0.33)

        same_market = dict(SAMPLE_MARKET)
        same_market["last_price_dollars"] = "0.3500"  # only +2pp

        self._mock_trades(collector, [])
        collector.process_market(same_market)
        signals = mock_db.get_recent_signals()
        odds_signals = [s for s in signals if s["signal_type"] == "odds_move"]
        assert len(odds_signals) == 0

    def test_volume_spike_creates_signal(self, collector, mock_db):
        spiked = dict(SAMPLE_MARKET)
        created_30d_ago = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        spiked["created_time"] = created_30d_ago
        spiked["volume_fp"] = "3000.00"  # lifetime = 3000, daily avg = 100
        spiked["volume_24h_fp"] = "500.00"  # 5x daily avg

        self._mock_trades(collector, [])
        collector.process_market(spiked)
        signals = mock_db.get_recent_signals()
        assert any(s["signal_type"] == "volume_spike" and s["source"] == "kalshi" for s in signals)

    def test_trade_deduplication(self, collector, mock_db):
        """Second poll with same trade ID should not create duplicate signals."""
        large_trade = dict(SAMPLE_TRADE)
        large_trade["count_fp"] = "200.00"

        self._mock_trades(collector, [large_trade])
        collector.process_market(SAMPLE_MARKET)

        # Second poll — same trade
        self._mock_trades(collector, [large_trade])
        collector.process_market(SAMPLE_MARKET)

        signals = mock_db.get_recent_signals()
        large_bets = [s for s in signals if s["signal_type"] == "large_bet"]
        assert len(large_bets) == 1

    def test_large_bet_signal_payload(self, collector, mock_db):
        """Verify the signal payload has the expected fields."""
        large_trade = dict(SAMPLE_TRADE)
        large_trade["count_fp"] = "200.00"
        self._mock_trades(collector, [large_trade])
        collector.process_market(SAMPLE_MARKET)
        signals = mock_db.get_recent_signals()
        sig = next(s for s in signals if s["signal_type"] == "large_bet")
        payload = sig["payload"]
        assert payload["trade_id"] == "abc-123-def"
        assert payload["contracts"] == 200.0
        assert payload["ticker"] == "KXMIDEASTWAR-26JUN15"
        assert "market_title" in payload

    def test_status_open_not_active_is_skipped(self, collector, mock_db):
        """status check is an exact == 'active', not just any non-closed value."""
        open_market = dict(SAMPLE_MARKET)
        open_market["status"] = "open"
        collector.process_market(open_market)
        assert mock_db.get_recent_signals() == []

    def test_status_check_is_case_sensitive(self, collector, mock_db):
        upper_market = dict(SAMPLE_MARKET)
        upper_market["status"] = "ACTIVE"
        collector.process_market(upper_market)
        assert mock_db.get_recent_signals() == []


# ---------------------------------------------------------------------------
# Odds move — exact payload and edge cases
# ---------------------------------------------------------------------------

class TestCheckOddsMoveDetail:
    def test_exact_payload_values(self, collector, mock_db):
        ticker = SAMPLE_MARKET["ticker"]
        collector.set_previous_price(ticker, 0.25)
        market = dict(SAMPLE_MARKET)
        market["last_price_dollars"] = "0.3500"
        collector._check_odds_move(market)
        sig = next(
            s for s in mock_db.get_recent_signals() if s["signal_type"] == "odds_move"
        )
        payload = sig["payload"]
        assert payload["previous_yes"] == pytest.approx(0.25)
        assert payload["current_yes"] == pytest.approx(0.35)
        assert payload["change_pct"] == pytest.approx(10.0)
        assert payload["ticker"] == ticker
        assert sig["priority"] == "MEDIUM"
        assert sig["source"] == "kalshi"

    def test_zero_current_price_skips_entirely(self, collector, mock_db):
        ticker = SAMPLE_MARKET["ticker"]
        collector.set_previous_price(ticker, 0.25)
        market = dict(SAMPLE_MARKET)
        market["last_price_dollars"] = "0.0"
        collector._check_odds_move(market)
        assert mock_db.get_recent_signals() == []
        # Zero price also must not overwrite the stored previous price
        assert collector.get_previous_price(ticker) == pytest.approx(0.25)

    def test_negative_current_price_skips(self, collector, mock_db):
        market = dict(SAMPLE_MARKET)
        market["last_price_dollars"] = "-0.1"
        collector._check_odds_move(market)
        assert mock_db.get_recent_signals() == []

    def test_previous_price_always_updated_after_check(self, collector):
        ticker = SAMPLE_MARKET["ticker"]
        market = dict(SAMPLE_MARKET)
        market["last_price_dollars"] = "0.42"
        collector._check_odds_move(market)
        assert collector.get_previous_price(ticker) == pytest.approx(0.42)

    def test_non_numeric_price_returns_without_updating_state(self, collector, mock_db):
        ticker = SAMPLE_MARKET["ticker"]
        collector.set_previous_price(ticker, 0.25)
        market = dict(SAMPLE_MARKET)
        market["last_price_dollars"] = "not-a-number"
        collector._check_odds_move(market)
        assert mock_db.get_recent_signals() == []
        assert collector.get_previous_price(ticker) == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Volume spike dedup — untested branch: re-fire only if ratio doubled or
# 6+ hours elapsed since the last spike alert for this ticker.
# ---------------------------------------------------------------------------

class TestVolumeSpikeDedup:
    @staticmethod
    def _spiking_market(volume_fp="3000.00", volume_24h_fp="500.00", days_old=30):
        m = dict(SAMPLE_MARKET)
        m["created_time"] = (datetime.now(UTC) - timedelta(days=days_old)).isoformat()
        m["volume_fp"] = volume_fp
        m["volume_24h_fp"] = volume_24h_fp
        return m

    def test_second_check_same_ratio_soon_after_suppressed(self, collector, mock_db):
        market = self._spiking_market()
        collector._check_volume_spike(market)
        collector._check_volume_spike(market)  # immediately again, same ratio
        signals = [
            s for s in mock_db.get_recent_signals() if s["signal_type"] == "volume_spike"
        ]
        assert len(signals) == 1

    def test_doubled_ratio_refires_immediately(self, collector, mock_db):
        market = self._spiking_market(volume_24h_fp="500.00")  # 5x
        collector._check_volume_spike(market)
        bigger = self._spiking_market(volume_24h_fp="1100.00")  # ~11x, >2x prior ratio
        collector._check_volume_spike(bigger)
        signals = [
            s for s in mock_db.get_recent_signals() if s["signal_type"] == "volume_spike"
        ]
        assert len(signals) == 2

    def test_six_hours_elapsed_refires_even_without_doubling(self, collector, mock_db):
        ticker = SAMPLE_MARKET["ticker"]
        market = self._spiking_market()
        collector._check_volume_spike(market)
        # Manually push the stored timestamp back 7 hours to simulate elapsed time
        from sentinel.collectors.kalshi import STATE_KEY_VOLUME_SPIKE
        key = STATE_KEY_VOLUME_SPIKE.format(ticker=ticker)
        stored = collector.db.state.get(key)
        ratio_str, _ = stored.split("|")
        old_ts = time.time() - 7 * 3600
        collector.db.state.set(key, f"{ratio_str}|{old_ts}")

        collector._check_volume_spike(market)  # same ratio, but 7h later
        signals = [
            s for s in mock_db.get_recent_signals() if s["signal_type"] == "volume_spike"
        ]
        assert len(signals) == 2

    def test_corrupted_dedup_state_refires(self, collector, mock_db):
        ticker = SAMPLE_MARKET["ticker"]
        from sentinel.collectors.kalshi import STATE_KEY_VOLUME_SPIKE
        collector.db.state.set(STATE_KEY_VOLUME_SPIKE.format(ticker=ticker), "garbage")
        market = self._spiking_market()
        collector._check_volume_spike(market)
        signals = [
            s for s in mock_db.get_recent_signals() if s["signal_type"] == "volume_spike"
        ]
        assert len(signals) == 1

    def test_missing_created_time_skips(self, collector, mock_db):
        market = self._spiking_market()
        del market["created_time"]
        collector._check_volume_spike(market)
        assert mock_db.get_recent_signals() == []

    def test_age_days_floors_at_1_for_same_day_market(self, collector, mock_db):
        """A market created today (age_days would be 0) must use daily_avg =
        volume_total / 1, not divide by zero."""
        market = self._spiking_market(days_old=0, volume_fp="100.00", volume_24h_fp="500.00")
        collector._check_volume_spike(market)  # must not raise ZeroDivisionError
        sig = next(
            s for s in mock_db.get_recent_signals() if s["signal_type"] == "volume_spike"
        )
        assert sig["payload"]["daily_avg"] == pytest.approx(100.0)

    def test_zero_volume_total_skips(self, collector, mock_db):
        market = self._spiking_market(volume_fp="0")
        collector._check_volume_spike(market)
        assert mock_db.get_recent_signals() == []

    def test_dedup_state_stores_ratio_and_timestamp(self, collector, mock_db):
        ticker = SAMPLE_MARKET["ticker"]
        from sentinel.collectors.kalshi import STATE_KEY_VOLUME_SPIKE
        market = self._spiking_market()
        collector._check_volume_spike(market)
        stored = collector.db.state.get(STATE_KEY_VOLUME_SPIKE.format(ticker=ticker))
        ratio_str, ts_str = stored.split("|")
        assert float(ratio_str) == pytest.approx(5.0)
        assert float(ts_str) == pytest.approx(time.time(), abs=5)


# ---------------------------------------------------------------------------
# Fetch — exact request params and response-key handling
# ---------------------------------------------------------------------------

class TestFetchParams:
    def test_fetch_markets_request_params(self, collector):
        collector._client.get = MagicMock(
            return_value=_mock_response(200, {"markets": []})
        )
        collector.fetch_event_markets("KXFOO")
        _, kwargs = collector._client.get.call_args
        assert kwargs["params"] == {"event_ticker": "KXFOO", "status": "open", "limit": 100}

    def test_fetch_markets_missing_key_defaults_empty(self, collector):
        collector._client.get = MagicMock(return_value=_mock_response(200, {}))
        assert collector.fetch_event_markets("KXFOO") == []

    def test_fetch_trades_request_params(self, collector):
        collector._client.get = MagicMock(
            return_value=_mock_response(200, {"trades": []})
        )
        collector.fetch_recent_trades("TICKER-1", limit=25)
        _, kwargs = collector._client.get.call_args
        assert kwargs["params"] == {"ticker": "TICKER-1", "limit": 25}

    def test_fetch_trades_default_limit_50(self, collector):
        collector._client.get = MagicMock(
            return_value=_mock_response(200, {"trades": []})
        )
        collector.fetch_recent_trades("TICKER-1")
        _, kwargs = collector._client.get.call_args
        assert kwargs["params"]["limit"] == 50

    def test_fetch_trades_missing_key_defaults_empty(self, collector):
        collector._client.get = MagicMock(return_value=_mock_response(200, {}))
        assert collector.fetch_recent_trades("TICKER-1") == []


# ---------------------------------------------------------------------------
# __init__ — wiring from config
# ---------------------------------------------------------------------------

class TestKalshiInit:
    def test_poll_interval_from_config(self, collector, mock_config):
        assert collector._poll_interval == mock_config.kalshi.poll_interval_seconds

    def test_tracked_events_from_config(self, collector):
        assert collector._tracked_events == ["KXMIDEASTWAR", "KXTRUMPTARIFF"]

    def test_api_base_from_config(self, collector):
        assert collector._api_base == KALSHI_API_BASE

    def test_thresholds_from_config(self, collector, mock_config):
        assert collector._thresholds is mock_config.kalshi.thresholds

    def test_consecutive_errors_starts_zero(self, collector):
        assert collector._consecutive_errors == 0
