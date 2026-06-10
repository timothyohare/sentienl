"""Unit tests for collectors/futures_volume.py."""

from datetime import date, time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from sentinel.collectors.futures_volume import (
    FuturesVolumeCollector,
    _compute_rolling_average,
    _detect_volume_spike,
    _is_roll_date,
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
    cfg.futures.poll_interval_seconds = 60
    cfg.futures.alpaca_api_key = ""
    cfg.futures.alpaca_api_secret = ""
    cfg.futures.alpaca_base_url = "https://data.alpaca.markets"
    cfg.futures.instruments = [
        MagicMock(ticker="CL=F", name="WTI Oil", min_absolute_volume=500),
        MagicMock(ticker="ES=F", name="S&P 500", min_absolute_volume=200),
    ]
    cfg.futures.thresholds.spike_multiplier = 3.0
    cfg.futures.thresholds.spike_multiplier_quiet = 5.0
    cfg.futures.thresholds.rolling_bars = 20
    cfg.futures.active_window_utc.start = time(11, 0)
    cfg.futures.active_window_utc.end = time(4, 0)
    cfg.futures.suppress_volume_alerts_on_roll_dates = True
    cfg.futures.roll_dates = [
        MagicMock(date="2026-04-22", tickers=["CL=F"], note="WTI April roll"),
    ]
    return cfg


@pytest.fixture
def collector(mock_config, mock_db):
    return FuturesVolumeCollector(config=mock_config, db=mock_db)


# ---------------------------------------------------------------------------
# Rolling average computation
# ---------------------------------------------------------------------------

class TestComputeRollingAverage:
    def test_rolling_average_basic(self):
        volumes = [100] * 20
        avg = _compute_rolling_average(volumes, bars=20)
        assert avg == 100.0

    def test_rolling_average_uses_last_n_bars(self):
        volumes = [0] * 5 + [100] * 20
        avg = _compute_rolling_average(volumes, bars=20)
        assert avg == 100.0

    def test_rolling_average_insufficient_bars(self):
        volumes = [100, 200, 300]
        avg = _compute_rolling_average(volumes, bars=20)
        assert avg == pytest.approx(200.0)

    def test_rolling_average_empty_returns_zero(self):
        avg = _compute_rolling_average([], bars=20)
        assert avg == 0.0

    def test_rolling_average_mixed(self):
        volumes = [100, 200, 300, 400, 500]
        avg = _compute_rolling_average(volumes, bars=5)
        assert avg == pytest.approx(300.0)

    def test_rolling_average_ignores_none_values(self):
        volumes = [100, None, 300]
        avg = _compute_rolling_average(volumes, bars=20)
        assert avg > 0  # should handle None gracefully


# ---------------------------------------------------------------------------
# Volume spike detection
# ---------------------------------------------------------------------------

class TestDetectVolumeSpike:
    def test_spike_detected_above_multiplier(self):
        spike = _detect_volume_spike(
            current_volume=1500,
            rolling_avg=400,
            spike_multiplier=3.0,
            min_absolute_volume=500,
        )
        assert spike is not None
        assert spike["ratio"] > 3.0

    def test_no_spike_below_multiplier(self):
        spike = _detect_volume_spike(
            current_volume=1100,
            rolling_avg=400,
            spike_multiplier=3.0,
            min_absolute_volume=500,
        )
        assert spike is None

    def test_no_spike_below_absolute_minimum(self):
        spike = _detect_volume_spike(
            current_volume=200,  # well below min_absolute_volume=500
            rolling_avg=50,
            spike_multiplier=3.0,
            min_absolute_volume=500,
        )
        assert spike is None

    def test_spike_ratio_correct(self):
        spike = _detect_volume_spike(
            current_volume=2000,
            rolling_avg=400,
            spike_multiplier=3.0,
            min_absolute_volume=500,
        )
        assert spike is not None
        assert spike["ratio"] == pytest.approx(5.0)

    def test_no_spike_zero_rolling_avg(self):
        spike = _detect_volume_spike(
            current_volume=1000,
            rolling_avg=0,
            spike_multiplier=3.0,
            min_absolute_volume=500,
        )
        assert spike is None

    def test_no_spike_none_current_volume(self):
        spike = _detect_volume_spike(
            current_volume=None,
            rolling_avg=400,
            spike_multiplier=3.0,
            min_absolute_volume=500,
        )
        assert spike is None


# ---------------------------------------------------------------------------
# Roll date suppression
# ---------------------------------------------------------------------------

class TestIsRollDate:
    def test_is_roll_date_match(self):
        roll_dates = [MagicMock(date="2026-04-22", tickers=["CL=F"])]
        assert _is_roll_date("CL=F", "2026-04-22", roll_dates) is True

    def test_is_roll_date_no_match_ticker(self):
        roll_dates = [MagicMock(date="2026-04-22", tickers=["CL=F"])]
        assert _is_roll_date("ES=F", "2026-04-22", roll_dates) is False

    def test_is_roll_date_no_match_date(self):
        roll_dates = [MagicMock(date="2026-04-22", tickers=["CL=F"])]
        assert _is_roll_date("CL=F", "2026-04-23", roll_dates) is False

    def test_is_roll_date_empty_list(self):
        assert _is_roll_date("CL=F", "2026-04-22", []) is False

    def test_is_roll_date_multi_ticker(self):
        roll_dates = [MagicMock(date="2026-06-19", tickers=["ES=F", "CL=F"])]
        assert _is_roll_date("ES=F", "2026-06-19", roll_dates) is True
        assert _is_roll_date("CL=F", "2026-06-19", roll_dates) is True
        assert _is_roll_date("BZ=F", "2026-06-19", roll_dates) is False


# ---------------------------------------------------------------------------
# Active window detection
# ---------------------------------------------------------------------------

class TestActiveWindow:
    def test_in_active_window(self, collector):
        # Active window: 11:00–04:00 UTC
        assert collector.is_in_active_window(time(14, 0)) is True

    def test_in_active_window_after_midnight(self, collector):
        assert collector.is_in_active_window(time(2, 0)) is True

    def test_outside_active_window(self, collector):
        assert collector.is_in_active_window(time(6, 0)) is False

    def test_at_window_start(self, collector):
        assert collector.is_in_active_window(time(11, 0)) is True

    def test_at_window_end(self, collector):
        assert collector.is_in_active_window(time(4, 0)) is True


# ---------------------------------------------------------------------------
# Spike threshold selection
# ---------------------------------------------------------------------------

class TestThresholdSelection:
    def test_active_window_uses_normal_multiplier(self, collector):
        multiplier = collector.get_spike_multiplier(time(14, 0))
        assert multiplier == 3.0

    def test_outside_window_uses_quiet_multiplier(self, collector):
        multiplier = collector.get_spike_multiplier(time(6, 0))
        assert multiplier == 5.0


# ---------------------------------------------------------------------------
# Data fetching (mocked yfinance)
# ---------------------------------------------------------------------------

class TestFetchBarsYfinance:
    def test_fetch_bars_returns_list(self, collector):
        mock_bars = [
            {"volume": 500, "close": 75.0, "open": 74.5},
            {"volume": 600, "close": 75.5, "open": 75.0},
        ]
        with patch.object(collector, "_fetch_yfinance", return_value=mock_bars):
            bars = collector.fetch_bars("CL=F")
        assert len(bars) == 2

    def test_fetch_bars_empty_returns_empty(self, collector):
        with patch.object(collector, "_fetch_yfinance", return_value=[]):
            bars = collector.fetch_bars("CL=F")
        assert bars == []

    def test_fetch_bars_falls_back_to_yfinance_when_no_alpaca_key(self, collector):
        collector._alpaca_api_key = ""
        with patch.object(collector, "_fetch_yfinance", return_value=[]) as mock_yf:
            collector.fetch_bars("CL=F")
        mock_yf.assert_called_once()


# ---------------------------------------------------------------------------
# Volume history tracking
# ---------------------------------------------------------------------------

class TestVolumeHistory:
    def test_add_volume_observation(self, collector):
        collector.add_volume_observation("CL=F", 500)
        assert len(collector._volume_history["CL=F"]) == 1

    def test_volume_history_capped_at_rolling_bars(self, collector):
        for i in range(30):
            collector.add_volume_observation("CL=F", i * 100)
        history = collector._volume_history["CL=F"]
        assert len(history) <= collector._rolling_bars + 5  # small buffer allowed

    def test_volume_history_independent_per_ticker(self, collector):
        collector.add_volume_observation("CL=F", 500)
        collector.add_volume_observation("ES=F", 300)
        assert len(collector._volume_history["CL=F"]) == 1
        assert len(collector._volume_history["ES=F"]) == 1


# ---------------------------------------------------------------------------
# Signal creation
# ---------------------------------------------------------------------------

class TestSignalCreation:
    def test_create_signal_on_spike(self, collector, mock_db):
        bars = [{"volume": 500, "close": 75.0, "open": 74.5}] * 19
        bars.append({"volume": 5000, "close": 76.0, "open": 75.0})
        # Pre-populate history
        for bar in bars[:-1]:
            collector.add_volume_observation("CL=F", bar["volume"])

        instrument = mock_config_instrument("CL=F", "WTI Oil", 500)
        with patch("sentinel.collectors.futures_volume.datetime") as mock_dt:
            from datetime import datetime as real_datetime
            mock_dt.now.return_value = real_datetime(2026, 3, 27, 14, 0, 0)
            mock_dt.now.return_value.time.return_value = time(14, 0)
            mock_dt.now.return_value.strftime.return_value = "2026-03-27"

            collector.process_instrument(instrument, bars[-1], time(14, 0), "2026-03-27")

        signals = mock_db.get_recent_signals()
        assert any(s["signal_type"] == "volume_spike" for s in signals)

    def test_no_signal_on_roll_date(self, collector, mock_db):
        bars = [{"volume": 500, "close": 75.0, "open": 74.5}] * 19
        bars.append({"volume": 5000, "close": 76.0, "open": 75.0})
        for bar in bars[:-1]:
            collector.add_volume_observation("CL=F", bar["volume"])
        instrument = mock_config_instrument("CL=F", "WTI Oil", 500)
        # Roll date for CL=F is 2026-04-22
        collector.process_instrument(instrument, bars[-1], time(14, 0), "2026-04-22")
        signals = mock_db.get_recent_signals()
        assert not any(s["signal_type"] == "volume_spike" for s in signals)

    def test_no_signal_below_absolute_minimum(self, collector, mock_db):
        # Volume is 3x average but below absolute minimum (500)
        bars = [{"volume": 50, "close": 75.0, "open": 74.5}] * 19
        bars.append({"volume": 200, "close": 76.0, "open": 75.0})  # 4x but < 500
        for bar in bars[:-1]:
            collector.add_volume_observation("CL=F", bar["volume"])
        instrument = mock_config_instrument("CL=F", "WTI Oil", 500)
        collector.process_instrument(instrument, bars[-1], time(14, 0), "2026-03-27")
        signals = mock_db.get_recent_signals()
        assert not any(s["signal_type"] == "volume_spike" for s in signals)


class TestBarDeduplication:
    """A repeated (delayed) bar must not be re-added to history or re-fired.

    Regression for the futures re-emission bug: yfinance returns the whole day
    and bars[-1] is the same delayed bar every poll, re-polluting the rolling
    average and re-firing the spike.
    """

    def test_same_timestamp_bar_processed_once(self, collector, mock_db):
        instrument = mock_config_instrument("CL=F", "WTI Oil", 500)
        for _ in range(19):
            collector.add_volume_observation("CL=F", 500)
        spike_bar = {"volume": 5000, "close": 76.0, "open": 75.0,
                     "timestamp": "2026-03-27T14:00:00+00:00"}
        # Process the identical bar three times (simulating repeat polls)
        for _ in range(3):
            collector.process_instrument(instrument, spike_bar, time(14, 0), "2026-03-27")
        signals = mock_db.get_recent_signals()
        spikes = [s for s in signals if s["signal_type"] == "volume_spike"]
        assert len(spikes) == 1

    def test_new_timestamp_bar_processed(self, collector, mock_db):
        instrument = mock_config_instrument("CL=F", "WTI Oil", 500)
        for _ in range(19):
            collector.add_volume_observation("CL=F", 500)
        bar1 = {"volume": 5000, "close": 76.0, "open": 75.0,
                "timestamp": "2026-03-27T14:00:00+00:00"}
        bar2 = {"volume": 5000, "close": 76.0, "open": 75.0,
                "timestamp": "2026-03-27T14:01:00+00:00"}
        collector.process_instrument(instrument, bar1, time(14, 0), "2026-03-27")
        collector.process_instrument(instrument, bar2, time(14, 0), "2026-03-27")
        signals = mock_db.get_recent_signals()
        spikes = [s for s in signals if s["signal_type"] == "volume_spike"]
        assert len(spikes) == 2

    def test_timestampless_bar_not_deduped(self, collector, mock_db):
        """Bars without a timestamp fall back to per-poll processing (no dedup)."""
        instrument = mock_config_instrument("CL=F", "WTI Oil", 500)
        for _ in range(19):
            collector.add_volume_observation("CL=F", 500)
        bar = {"volume": 5000, "close": 76.0, "open": 75.0}
        collector.process_instrument(instrument, bar, time(14, 0), "2026-03-27")
        signals = mock_db.get_recent_signals()
        assert any(s["signal_type"] == "volume_spike" for s in signals)


def mock_config_instrument(ticker, name, min_vol):
    inst = MagicMock()
    inst.ticker = ticker
    inst.name = name
    inst.min_absolute_volume = min_vol
    return inst


# ---------------------------------------------------------------------------
# Data gap handling
# ---------------------------------------------------------------------------

class TestDataGapHandling:
    def test_none_volume_bar_handled_gracefully(self, collector, mock_db):
        instrument = mock_config_instrument("CL=F", "WTI Oil", 500)
        # Should not raise
        bar_with_none = {"volume": None, "close": 75.0, "open": 74.5}
        collector.process_instrument(instrument, bar_with_none, time(14, 0), "2026-03-27")

    def test_missing_close_handled_gracefully(self, collector, mock_db):
        instrument = mock_config_instrument("CL=F", "WTI Oil", 500)
        bar_incomplete = {"volume": 500}
        collector.process_instrument(instrument, bar_incomplete, time(14, 0), "2026-03-27")
