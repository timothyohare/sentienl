"""
Unit tests for collectors/polymarket.py.

NOTE: The Polymarket gamma-api.polymarket.com API was DNS-blocked from the
development machine and could not be verified. All tests use mocked HTTP
responses. Verify API availability from the deployment server before relying
on this collector in production. See human_todo.md for steps.
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import responses as responses_lib

from sentinel.collectors.polymarket import (
    PolymarketCollector,
    GAMMA_API_BASE,
    _calculate_volume_spike,
    _is_new_wallet,
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
    cfg.polymarket.poll_interval_seconds = 30
    cfg.polymarket.gamma_api_url = GAMMA_API_BASE
    cfg.polymarket.polygonscan_api_key = ""
    cfg.polymarket.tracked_markets = ["us-iran-ceasefire-2026", "us-attack-iran"]
    cfg.polymarket.thresholds.large_bet_usd = 5000
    cfg.polymarket.thresholds.new_wallet_age_days = 7
    cfg.polymarket.thresholds.new_wallet_min_bet_usd = 1000
    cfg.polymarket.thresholds.odds_move_pct_5min = 5.0
    cfg.polymarket.thresholds.volume_spike_multiplier = 3.0
    cfg.polymarket.thresholds.min_absolute_volume_usd = 500
    return cfg


@pytest.fixture
def collector(mock_config, mock_db):
    return PolymarketCollector(config=mock_config, db=mock_db)


# Sample market data
SAMPLE_MARKET = {
    "slug": "us-iran-ceasefire-2026",
    "question": "Will there be a US-Iran ceasefire by April 15, 2026?",
    "conditionId": "0xabc123",
    "active": True,
    "closed": False,
    "volume": "50000",
    "volume24hr": "5000",
    "outcomePrices": ["0.65", "0.35"],
    "outcomes": '["YES", "NO"]',
    "url": "https://polymarket.com/event/us-iran-ceasefire-2026",
}

SAMPLE_TRADE = {
    "id": "trade-001",
    "transactionHash": "0xdeadbeef",
    "timestamp": "2026-03-27T10:00:00Z",
    "maker": "0xwallet123",
    "taker": "0xwallet456",
    "side": "BUY",
    "outcomeIndex": 0,
    "price": "0.65",
    "size": "10000",
    "usdcSize": "6500",
}


# ---------------------------------------------------------------------------
# Signal detection helpers
# ---------------------------------------------------------------------------

class TestIsLargeBet:
    def test_large_bet_above_threshold(self):
        assert _is_large_bet(6500.0, threshold=5000) is True

    def test_large_bet_at_threshold(self):
        assert _is_large_bet(5000.0, threshold=5000) is True

    def test_large_bet_below_threshold(self):
        assert _is_large_bet(4999.99, threshold=5000) is False

    def test_large_bet_zero(self):
        assert _is_large_bet(0.0, threshold=5000) is False


class TestIsNewWallet:
    def test_new_wallet_within_age_limit(self):
        assert _is_new_wallet(age_days=3, min_age=7, min_bet=1000, bet_usd=1500) is True

    def test_new_wallet_exactly_at_limit(self):
        assert _is_new_wallet(age_days=7, min_age=7, min_bet=1000, bet_usd=1000) is True

    def test_not_new_wallet_too_old(self):
        assert _is_new_wallet(age_days=30, min_age=7, min_bet=1000, bet_usd=5000) is False

    def test_not_new_wallet_bet_too_small(self):
        assert _is_new_wallet(age_days=2, min_age=7, min_bet=1000, bet_usd=500) is False

    def test_new_wallet_unknown_age_returns_false(self):
        assert _is_new_wallet(age_days=None, min_age=7, min_bet=1000, bet_usd=1500) is False


class TestIsOddsMove:
    def test_odds_move_above_threshold(self):
        assert _is_odds_move(previous=0.50, current=0.56, threshold_pct=5.0) is True

    def test_odds_move_exactly_at_threshold(self):
        assert _is_odds_move(previous=0.50, current=0.55, threshold_pct=5.0) is True

    def test_odds_move_below_threshold(self):
        assert _is_odds_move(previous=0.50, current=0.54, threshold_pct=5.0) is False

    def test_odds_move_negative(self):
        # Downward move of 5pp
        assert _is_odds_move(previous=0.60, current=0.55, threshold_pct=5.0) is True

    def test_no_previous_price_returns_false(self):
        assert _is_odds_move(previous=None, current=0.65, threshold_pct=5.0) is False


class TestVolumeSpike:
    def test_volume_spike_above_threshold(self):
        result = _calculate_volume_spike(
            current_volume=1500, baseline_volume=400, multiplier=3.0, min_absolute=500
        )
        assert result is not None
        assert result["ratio"] > 3.0

    def test_volume_spike_below_multiplier(self):
        result = _calculate_volume_spike(
            current_volume=1100, baseline_volume=400, multiplier=3.0, min_absolute=500
        )
        assert result is None

    def test_volume_spike_below_absolute_minimum(self):
        # Even if ratio is high, volume too low
        result = _calculate_volume_spike(
            current_volume=300, baseline_volume=50, multiplier=3.0, min_absolute=500
        )
        assert result is None

    def test_volume_spike_returns_ratio(self):
        result = _calculate_volume_spike(
            current_volume=2000, baseline_volume=400, multiplier=3.0, min_absolute=500
        )
        assert result is not None
        assert result["ratio"] == pytest.approx(5.0)

    def test_volume_spike_zero_baseline(self):
        # Avoid division by zero
        result = _calculate_volume_spike(
            current_volume=1000, baseline_volume=0, multiplier=3.0, min_absolute=500
        )
        assert result is None


# ---------------------------------------------------------------------------
# Market fetching
# ---------------------------------------------------------------------------

class TestFetchMarket:
    @responses_lib.activate
    def test_fetch_market_success(self, collector):
        responses_lib.add(
            responses_lib.GET,
            f"{GAMMA_API_BASE}/markets",
            json=[SAMPLE_MARKET],
            status=200,
        )
        market = collector.fetch_market("us-iran-ceasefire-2026")
        assert market is not None
        assert market["slug"] == "us-iran-ceasefire-2026"

    @responses_lib.activate
    def test_fetch_market_not_found_returns_none(self, collector):
        responses_lib.add(
            responses_lib.GET,
            f"{GAMMA_API_BASE}/markets",
            json=[],
            status=200,
        )
        market = collector.fetch_market("nonexistent-market")
        assert market is None

    @responses_lib.activate
    def test_fetch_market_api_error_returns_none(self, collector):
        responses_lib.add(
            responses_lib.GET,
            f"{GAMMA_API_BASE}/markets",
            status=500,
        )
        market = collector.fetch_market("us-iran-ceasefire-2026")
        assert market is None

    @responses_lib.activate
    def test_fetch_market_network_error_returns_none(self, collector):
        responses_lib.add(
            responses_lib.GET,
            f"{GAMMA_API_BASE}/markets",
            body=ConnectionError("DNS resolution failed"),
        )
        market = collector.fetch_market("us-iran-ceasefire-2026")
        assert market is None


# ---------------------------------------------------------------------------
# Trade fetching
# ---------------------------------------------------------------------------

class TestFetchRecentTrades:
    @responses_lib.activate
    def test_fetch_trades_success(self, collector):
        condition_id = "0xabc123"
        responses_lib.add(
            responses_lib.GET,
            f"{GAMMA_API_BASE}/trades",
            json=[SAMPLE_TRADE],
            status=200,
        )
        trades = collector.fetch_recent_trades(condition_id)
        assert len(trades) == 1

    @responses_lib.activate
    def test_fetch_trades_empty_returns_empty(self, collector):
        condition_id = "0xabc123"
        responses_lib.add(
            responses_lib.GET,
            f"{GAMMA_API_BASE}/trades",
            json=[],
            status=200,
        )
        trades = collector.fetch_recent_trades(condition_id)
        assert trades == []

    @responses_lib.activate
    def test_fetch_trades_error_returns_empty(self, collector):
        condition_id = "0xabc123"
        responses_lib.add(
            responses_lib.GET,
            f"{GAMMA_API_BASE}/trades",
            status=503,
        )
        trades = collector.fetch_recent_trades(condition_id)
        assert trades == []


# ---------------------------------------------------------------------------
# Resolved market handling
# ---------------------------------------------------------------------------

class TestResolvedMarket:
    def test_resolved_market_is_detected(self, collector):
        resolved_market = dict(SAMPLE_MARKET)
        resolved_market["closed"] = True
        resolved_market["active"] = False
        assert collector.is_market_resolved(resolved_market) is True

    def test_active_market_is_not_resolved(self, collector):
        assert collector.is_market_resolved(SAMPLE_MARKET) is False


# ---------------------------------------------------------------------------
# Wallet age lookup (mocked Polygonscan)
# ---------------------------------------------------------------------------

class TestWalletAgeLookup:
    @responses_lib.activate
    def test_get_wallet_age_no_api_key_returns_none(self, collector):
        # No Polygonscan API key configured
        age = collector.get_wallet_age_days("0xwallet123")
        assert age is None

    @responses_lib.activate
    def test_get_wallet_age_from_cache(self, collector, mock_db):
        # Pre-populate wallet cache
        mock_db.wallet_cache.set("0xcached", "2026-03-20T00:00:00+00:00")
        age = collector.get_wallet_age_days("0xcached")
        # Should return from cache without HTTP call
        assert age is not None
        assert len(responses_lib.calls) == 0

    @responses_lib.activate
    def test_get_wallet_age_cache_miss_with_api_key(self, collector):
        collector._polygonscan_api_key = "test-key"
        responses_lib.add(
            responses_lib.GET,
            "https://api.polygonscan.com/api",
            json={
                "status": "1",
                "result": [
                    {
                        "timeStamp": "1711488000",  # 2024-03-27
                        "hash": "0xhash",
                    }
                ],
            },
            status=200,
        )
        age = collector.get_wallet_age_days("0xnewwallet")
        assert age is not None
        assert age > 0


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

class TestPolymarketState:
    def test_get_last_trade_id_none_initially(self, collector):
        assert collector.get_last_trade_id("us-iran-ceasefire-2026") is None

    def test_set_and_get_last_trade_id(self, collector):
        collector.set_last_trade_id("us-iran-ceasefire-2026", "trade-001")
        assert collector.get_last_trade_id("us-iran-ceasefire-2026") == "trade-001"

    def test_get_previous_odds_none_initially(self, collector):
        assert collector.get_previous_odds("us-iran-ceasefire-2026") is None

    def test_set_and_get_previous_odds(self, collector):
        collector.set_previous_odds("us-iran-ceasefire-2026", 0.65)
        assert collector.get_previous_odds("us-iran-ceasefire-2026") == pytest.approx(0.65)


# ---------------------------------------------------------------------------
# Signal generation (integration through process_market)
# ---------------------------------------------------------------------------

class TestProcessMarket:
    @responses_lib.activate
    def test_large_bet_creates_signal(self, collector, mock_db):
        large_trade = dict(SAMPLE_TRADE)
        large_trade["usdcSize"] = "7500"  # > $5000 threshold
        responses_lib.add(
            responses_lib.GET,
            f"{GAMMA_API_BASE}/trades",
            json=[large_trade],
            status=200,
        )
        collector.process_market(SAMPLE_MARKET)
        signals = mock_db.get_recent_signals()
        assert any(s["signal_type"] == "large_bet" for s in signals)

    @responses_lib.activate
    def test_no_signal_for_small_trade(self, collector, mock_db):
        small_trade = dict(SAMPLE_TRADE)
        small_trade["usdcSize"] = "100"  # < $5000 threshold
        responses_lib.add(
            responses_lib.GET,
            f"{GAMMA_API_BASE}/trades",
            json=[small_trade],
            status=200,
        )
        collector.process_market(SAMPLE_MARKET)
        signals = mock_db.get_recent_signals()
        large_bet_signals = [s for s in signals if s["signal_type"] == "large_bet"]
        assert len(large_bet_signals) == 0

    @responses_lib.activate
    def test_resolved_market_skipped(self, collector, mock_db):
        resolved = dict(SAMPLE_MARKET)
        resolved["closed"] = True
        resolved["active"] = False
        # No trades call should be made
        collector.process_market(resolved)
        signals = mock_db.get_recent_signals()
        assert len(signals) == 0
        # No HTTP calls to trades endpoint
        assert len(responses_lib.calls) == 0
