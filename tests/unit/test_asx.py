"""Unit tests for collectors/asx.py."""

from datetime import time
from unittest.mock import MagicMock, patch

import pytest

from sentinel.collectors.asx import AsxCollector, _detect_price_move
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
    cfg.asx.poll_interval_seconds = 60
    cfg.asx.ib_enabled = False
    cfg.asx.ib_host = "127.0.0.1"
    cfg.asx.ib_port = 4002
    cfg.asx.ib_client_id_base = 5000
    cfg.asx.instruments = [
        MagicMock(ticker="BHP.AX", name="BHP Group", min_absolute_volume=10000),
        MagicMock(ticker="CBA.AX", name="Commonwealth Bank", min_absolute_volume=3000),
    ]
    cfg.asx.thresholds.spike_multiplier = 3.0
    cfg.asx.thresholds.spike_multiplier_quiet = 5.0
    cfg.asx.thresholds.rolling_bars = 20
    cfg.asx.thresholds.price_move_pct = 1.0
    cfg.asx.thresholds.price_move_pct_high = 2.5
    cfg.asx.active_window_utc.start = time(23, 45)
    cfg.asx.active_window_utc.end = time(6, 15)
    return cfg


@pytest.fixture
def collector(mock_config, mock_db):
    return AsxCollector(config=mock_config, db=mock_db)


def mock_instrument(ticker, name, min_vol):
    inst = MagicMock()
    inst.ticker = ticker
    inst.name = name
    inst.min_absolute_volume = min_vol
    return inst


# ---------------------------------------------------------------------------
# Price move detection (pure helper)
# ---------------------------------------------------------------------------

class TestDetectPriceMove:
    def test_move_above_threshold_detected(self):
        change = _detect_price_move(previous_close=100.0, current_close=102.0, threshold_pct=1.0)
        assert change == pytest.approx(2.0)

    def test_move_below_threshold_not_detected(self):
        change = _detect_price_move(previous_close=100.0, current_close=100.5, threshold_pct=1.0)
        assert change is None

    def test_negative_move_detected_by_magnitude(self):
        change = _detect_price_move(previous_close=100.0, current_close=97.0, threshold_pct=1.0)
        assert change == pytest.approx(-3.0)

    def test_no_previous_close_returns_none(self):
        change = _detect_price_move(previous_close=None, current_close=100.0, threshold_pct=1.0)
        assert change is None

    def test_zero_previous_close_returns_none(self):
        change = _detect_price_move(previous_close=0.0, current_close=100.0, threshold_pct=1.0)
        assert change is None

    def test_move_exactly_at_threshold_detected(self):
        change = _detect_price_move(previous_close=100.0, current_close=101.0, threshold_pct=1.0)
        assert change == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Active window / threshold selection (identical semantics to futures_volume)
# ---------------------------------------------------------------------------

class TestActiveWindow:
    def test_in_active_window(self, collector):
        assert collector.is_in_active_window(time(2, 0)) is True

    def test_outside_active_window(self, collector):
        assert collector.is_in_active_window(time(12, 0)) is False


class TestThresholdSelection:
    def test_active_window_uses_normal_multiplier(self, collector):
        assert collector.get_spike_multiplier(time(2, 0)) == 3.0

    def test_outside_window_uses_quiet_multiplier(self, collector):
        assert collector.get_spike_multiplier(time(12, 0)) == 5.0


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

class TestFetchBars:
    def test_fetch_bars_uses_yfinance_when_ib_disabled(self, collector):
        collector._ib_enabled = False
        with (
            patch.object(collector, "_fetch_ibkr") as mock_ib,
            patch.object(collector, "_fetch_yfinance", return_value=[{"volume": 1}]),
        ):
            bars = collector.fetch_bars("BHP.AX")
        mock_ib.assert_not_called()
        assert bars == [{"volume": 1}]

    def test_ib_used_when_enabled_and_returns_bars(self, collector):
        collector._ib_enabled = True
        mock_bars = [{"volume": 500, "close": 45.0, "open": 44.5}]
        with (
            patch.object(collector, "_fetch_ibkr", return_value=mock_bars) as mock_ib,
            patch.object(collector, "_fetch_yfinance") as mock_yf,
        ):
            bars = collector.fetch_bars("BHP.AX")
        mock_ib.assert_called_once()
        mock_yf.assert_not_called()
        assert bars == mock_bars

    def test_falls_back_to_yfinance_when_ib_returns_empty(self, collector):
        collector._ib_enabled = True
        with (
            patch.object(collector, "_fetch_ibkr", return_value=[]),
            patch.object(collector, "_fetch_yfinance", return_value=[]) as mock_yf,
        ):
            collector.fetch_bars("BHP.AX")
        mock_yf.assert_called_once()


# ---------------------------------------------------------------------------
# Volume history tracking
# ---------------------------------------------------------------------------

class TestVolumeHistory:
    def test_add_volume_observation(self, collector):
        collector.add_volume_observation("BHP.AX", 10000)
        assert len(collector._volume_history["BHP.AX"]) == 1

    def test_history_independent_per_ticker(self, collector):
        collector.add_volume_observation("BHP.AX", 10000)
        collector.add_volume_observation("CBA.AX", 3000)
        assert len(collector._volume_history["BHP.AX"]) == 1
        assert len(collector._volume_history["CBA.AX"]) == 1


# ---------------------------------------------------------------------------
# Signal creation — volume_spike
# ---------------------------------------------------------------------------

class TestVolumeSpikeSignal:
    def test_create_signal_on_spike(self, collector, mock_db):
        instrument = mock_instrument("BHP.AX", "BHP Group", 10000)
        for _ in range(19):
            collector.add_volume_observation("BHP.AX", 10000)
        bar = {"volume": 100000, "close": 45.0, "open": 44.8}
        collector.process_instrument(instrument, bar, time(2, 0))
        signals = mock_db.get_recent_signals()
        assert any(s["signal_type"] == "volume_spike" for s in signals)

    def test_no_signal_below_absolute_minimum(self, collector, mock_db):
        instrument = mock_instrument("BHP.AX", "BHP Group", 10000)
        for _ in range(19):
            collector.add_volume_observation("BHP.AX", 1000)
        bar = {"volume": 4000, "close": 45.0, "open": 44.8}  # 4x avg but < 10000 floor
        collector.process_instrument(instrument, bar, time(2, 0))
        signals = mock_db.get_recent_signals()
        assert not any(s["signal_type"] == "volume_spike" for s in signals)

    def test_high_priority_above_quiet_multiplier(self, collector, mock_db):
        instrument = mock_instrument("BHP.AX", "BHP Group", 10000)
        for _ in range(19):
            collector.add_volume_observation("BHP.AX", 10000)
        bar = {"volume": 200000, "close": 45.0, "open": 44.8}  # 20x avg
        collector.process_instrument(instrument, bar, time(2, 0))
        spikes = [s for s in mock_db.get_recent_signals() if s["signal_type"] == "volume_spike"]
        assert spikes[0]["priority"] == "HIGH"


# ---------------------------------------------------------------------------
# Signal creation — price_move
# ---------------------------------------------------------------------------

class TestPriceMoveSignal:
    def test_no_signal_on_first_bar_seen(self, collector, mock_db):
        instrument = mock_instrument("BHP.AX", "BHP Group", 10000)
        bar = {"volume": 5000, "close": 45.0, "open": 44.8}
        collector.process_instrument(instrument, bar, time(2, 0))
        signals = mock_db.get_recent_signals()
        assert not any(s["signal_type"] == "price_move" for s in signals)

    def test_signal_fires_on_second_bar_with_big_move(self, collector, mock_db):
        instrument = mock_instrument("BHP.AX", "BHP Group", 10000)
        bar1 = {"volume": 5000, "close": 45.0, "open": 44.8,
                "timestamp": "2026-08-23T00:00:00+00:00"}
        bar2 = {"volume": 5000, "close": 46.0, "open": 45.0,  # +2.2%
                "timestamp": "2026-08-23T00:01:00+00:00"}
        collector.process_instrument(instrument, bar1, time(2, 0))
        collector.process_instrument(instrument, bar2, time(2, 0))
        moves = [s for s in mock_db.get_recent_signals() if s["signal_type"] == "price_move"]
        assert len(moves) == 1

    def test_no_signal_when_move_below_threshold(self, collector, mock_db):
        instrument = mock_instrument("BHP.AX", "BHP Group", 10000)
        bar1 = {"volume": 5000, "close": 45.00, "open": 44.8,
                "timestamp": "2026-08-23T00:00:00+00:00"}
        bar2 = {"volume": 5000, "close": 45.10, "open": 45.00,  # +0.22%
                "timestamp": "2026-08-23T00:01:00+00:00"}
        collector.process_instrument(instrument, bar1, time(2, 0))
        collector.process_instrument(instrument, bar2, time(2, 0))
        moves = [s for s in mock_db.get_recent_signals() if s["signal_type"] == "price_move"]
        assert moves == []

    def test_high_priority_above_high_threshold(self, collector, mock_db):
        instrument = mock_instrument("BHP.AX", "BHP Group", 10000)
        bar1 = {"volume": 5000, "close": 45.0, "open": 44.8,
                "timestamp": "2026-08-23T00:00:00+00:00"}
        bar2 = {"volume": 5000, "close": 48.0, "open": 45.0,  # +6.7%
                "timestamp": "2026-08-23T00:01:00+00:00"}
        collector.process_instrument(instrument, bar1, time(2, 0))
        collector.process_instrument(instrument, bar2, time(2, 0))
        moves = [s for s in mock_db.get_recent_signals() if s["signal_type"] == "price_move"]
        assert moves[0]["priority"] == "HIGH"


# ---------------------------------------------------------------------------
# Bar deduplication
# ---------------------------------------------------------------------------

class TestBarDeduplication:
    def test_same_timestamp_bar_processed_once(self, collector, mock_db):
        instrument = mock_instrument("BHP.AX", "BHP Group", 10000)
        for _ in range(19):
            collector.add_volume_observation("BHP.AX", 10000)
        spike_bar = {"volume": 100000, "close": 45.0, "open": 44.8,
                     "timestamp": "2026-08-23T00:00:00+00:00"}
        for _ in range(3):
            collector.process_instrument(instrument, spike_bar, time(2, 0))
        spikes = [s for s in mock_db.get_recent_signals() if s["signal_type"] == "volume_spike"]
        assert len(spikes) == 1


# ---------------------------------------------------------------------------
# Data gap handling
# ---------------------------------------------------------------------------

class TestDataGapHandling:
    def test_none_volume_bar_handled_gracefully(self, collector, mock_db):
        instrument = mock_instrument("BHP.AX", "BHP Group", 10000)
        bar_with_none = {"volume": None, "close": 45.0, "open": 44.8}
        collector.process_instrument(instrument, bar_with_none, time(2, 0))

    def test_missing_close_handled_gracefully(self, collector, mock_db):
        instrument = mock_instrument("BHP.AX", "BHP Group", 10000)
        bar_incomplete = {"volume": 5000}
        collector.process_instrument(instrument, bar_incomplete, time(2, 0))
