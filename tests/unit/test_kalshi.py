"""
Unit tests for collectors/kalshi.py.

All tests use unittest.mock to patch the httpx client directly.
"""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from sentinel.collectors.kalshi import (
    KalshiCollector,
    KALSHI_API_BASE,
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
    "created_time": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
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
        created_30d_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
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
