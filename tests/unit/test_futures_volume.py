"""Unit tests for collectors/futures_volume.py."""

from datetime import time
from unittest.mock import MagicMock, patch

import pytest

from sentinel.collectors.futures_volume import (
    FuturesVolumeCollector,
    _canonical_ts,
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

    def test_rolling_avg_exactly_1_is_valid_not_treated_as_zero(self):
        spike = _detect_volume_spike(
            current_volume=10, rolling_avg=1, spike_multiplier=3.0, min_absolute_volume=1,
        )
        assert spike is not None
        assert spike["ratio"] == pytest.approx(10.0)


class TestCanonicalTs:
    def test_none_returns_none(self):
        assert _canonical_ts(None) is None

    def test_z_suffix_normalized(self):
        assert _canonical_ts("2026-03-27T14:00:00Z") == "2026-03-27T14:00:00+00:00"

    def test_offset_suffix_unchanged_semantically(self):
        assert _canonical_ts("2026-03-27T14:00:00+00:00") == "2026-03-27T14:00:00+00:00"

    def test_naive_timestamp_gets_utc(self):
        assert _canonical_ts("2026-03-27T14:00:00") == "2026-03-27T14:00:00+00:00"

    def test_unparseable_falls_back_to_str(self):
        assert _canonical_ts("not-a-timestamp") == "not-a-timestamp"

    def test_non_utc_offset_converted_to_utc(self):
        # +05:00 14:00 is 09:00 UTC
        assert _canonical_ts("2026-03-27T14:00:00+05:00") == "2026-03-27T09:00:00+00:00"


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

    def test_volume_history_capped_at_history_max_bars(self, collector):
        # History is bounded at HISTORY_MAX_BARS (the memory cap), not at
        # rolling_bars (which is only the averaging window). Add well past the
        # cap and assert it does not grow unbounded.
        from sentinel.collectors.futures_volume import HISTORY_MAX_BARS
        for i in range(HISTORY_MAX_BARS + 50):
            collector.add_volume_observation("CL=F", i * 100)
        history = collector._volume_history["CL=F"]
        assert len(history) == HISTORY_MAX_BARS

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
        # process_instrument receives now_time and today_str directly — it does
        # not call datetime.now() — so no patching is needed.
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


class TestTimestampNormalization:
    """The same instant in different ISO formats (Z vs +00:00) must dedupe to one
    signal — otherwise switching data source (Alpaca 'Z' vs yfinance '+00:00')
    re-processes the same bar."""

    def test_equivalent_timestamps_dedupe(self, collector, mock_db):
        instrument = mock_config_instrument("CL=F", "WTI Oil", 500)
        for _ in range(19):
            collector.add_volume_observation("CL=F", 500)
        bar_z = {"volume": 5000, "close": 76.0, "open": 75.0,
                 "timestamp": "2026-03-27T14:00:00Z"}
        bar_offset = {"volume": 5000, "close": 76.0, "open": 75.0,
                      "timestamp": "2026-03-27T14:00:00+00:00"}
        collector.process_instrument(instrument, bar_z, time(14, 0), "2026-03-27")
        collector.process_instrument(instrument, bar_offset, time(14, 0), "2026-03-27")
        spikes = [s for s in mock_db.get_recent_signals()
                  if s["signal_type"] == "volume_spike"]
        assert len(spikes) == 1


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


# ---------------------------------------------------------------------------
# Exact signal payload — process_instrument's payload dict has ~10 fields;
# existing tests only checked "a volume_spike signal exists".
# ---------------------------------------------------------------------------

class TestSignalPayloadExact:
    def test_exact_payload_fields(self, collector, mock_db):
        instrument = mock_config_instrument("CL=F", "WTI Oil", 500)
        for _ in range(19):
            collector.add_volume_observation("CL=F", 400)
        bar = {"volume": 2000, "close": 76.0, "open": 74.0,
               "timestamp": "2026-03-27T14:00:00+00:00"}
        collector.process_instrument(instrument, bar, time(14, 0), "2026-03-27")

        sig = next(
            s for s in mock_db.get_recent_signals() if s["signal_type"] == "volume_spike"
        )
        payload = sig["payload"]
        assert payload["ticker"] == "CL=F"
        assert payload["name"] == "WTI Oil"
        assert payload["current_volume"] == 2000
        assert payload["average_volume"] == pytest.approx(400.0)
        assert payload["ratio"] == pytest.approx(5.0, abs=0.01)
        assert payload["price"] == 76.0
        assert payload["price_change_pct"] == pytest.approx(
            (76.0 - 74.0) / 74.0 * 100, abs=0.01
        )
        assert payload["spike_multiplier_used"] == 3.0
        assert payload["in_active_window"] is True
        assert sig["source"] == "futures_oil"
        assert sig["summary"] == "Volume spike CL=F: 2,000 contracts (5.00x avg 400)"

    def test_source_mapped_per_ticker(self, collector, mock_db):
        instrument = mock_config_instrument("ES=F", "S&P 500", 200)
        for _ in range(19):
            collector.add_volume_observation("ES=F", 300)
        bar = {"volume": 2000, "close": 5000.0, "open": 4990.0}
        collector.process_instrument(instrument, bar, time(14, 0), "2026-03-27")
        sig = next(
            s for s in mock_db.get_recent_signals() if s["signal_type"] == "volume_spike"
        )
        assert sig["source"] == "futures_sp500"

    def test_open_price_zero_falls_back_to_close(self, collector, mock_db):
        """open=0 is falsy — `or close_price` must trigger (not just a
        missing key), otherwise price_change_pct divides by zero."""
        instrument = mock_config_instrument("CL=F", "WTI Oil", 500)
        for _ in range(19):
            collector.add_volume_observation("CL=F", 400)
        bar = {"volume": 2000, "close": 76.0, "open": 0}
        collector.process_instrument(instrument, bar, time(14, 0), "2026-03-27")
        sig = next(
            s for s in mock_db.get_recent_signals() if s["signal_type"] == "volume_spike"
        )
        # open falls back to close (76.0) -> price_change_pct == 0.0
        assert sig["payload"]["price_change_pct"] == 0.0


class TestPriorityBranches:
    @staticmethod
    def _instrument():
        return mock_config_instrument("CL=F", "WTI Oil", 500)

    def _fire(self, collector, mock_db, now_time):
        for _ in range(19):
            collector.add_volume_observation("CL=F", 400)
        # ratio well above spike_multiplier_quiet=5.0 -> always HIGH regardless
        bar = {"volume": 3000, "close": 76.0, "open": 75.0}
        collector.process_instrument(self._instrument(), bar, now_time, "2026-03-27")
        return next(
            s for s in mock_db.get_recent_signals() if s["signal_type"] == "volume_spike"
        )

    def test_ratio_above_quiet_multiplier_is_high(self, collector, mock_db):
        sig = self._fire(collector, mock_db, time(14, 0))
        assert sig["priority"] == "HIGH"

    def test_moderate_ratio_in_active_window_is_medium(self, collector, mock_db):
        for _ in range(19):
            collector.add_volume_observation("CL=F", 400)
        bar = {"volume": 1600, "close": 76.0, "open": 75.0}  # 4x, below quiet(5x)
        collector.process_instrument(self._instrument(), bar, time(14, 0), "2026-03-27")
        sig = next(
            s for s in mock_db.get_recent_signals() if s["signal_type"] == "volume_spike"
        )
        assert sig["priority"] == "MEDIUM"

    # The "LOW" priority branch was confirmed unreachable and removed from
    # process_instrument (2026-08-03): outside the active window,
    # get_spike_multiplier() returns thresholds.spike_multiplier_quiet, and
    # _detect_volume_spike only fires when ratio >= that same multiplier —
    # so any spike detected outside the window already satisfies the
    # `ratio >= spike_multiplier_quiet` HIGH-priority check. This holds
    # regardless of the relative ordering of spike_multiplier and
    # spike_multiplier_quiet, so there's no config under which LOW could
    # have fired.


class TestAddVolumeObservationEviction:
    def test_oldest_entry_evicted_when_over_cap(self, collector):
        """del history[0] must drop the OLDEST observation, not history[1]."""
        from sentinel.collectors.futures_volume import HISTORY_MAX_BARS
        for i in range(HISTORY_MAX_BARS):
            collector.add_volume_observation("CL=F", i)
        collector.add_volume_observation("CL=F", 99999)  # pushes over the cap
        history = collector._volume_history["CL=F"]
        assert len(history) == HISTORY_MAX_BARS
        assert history[0] == 1  # the original 0 was evicted, 1 is now oldest
        assert history[-1] == 99999


class TestFetchBarsAlpacaPriority:
    def test_alpaca_success_skips_yfinance(self, collector):
        collector._alpaca_api_key = "key"
        alpaca_bars = [{"volume": 100, "close": 1.0, "open": 1.0}]
        with (
            patch.object(collector, "_fetch_alpaca", return_value=alpaca_bars) as mock_alpaca,
            patch.object(collector, "_fetch_yfinance") as mock_yf,
        ):
            bars = collector.fetch_bars("CL=F")
        mock_alpaca.assert_called_once()
        mock_yf.assert_not_called()
        assert bars == alpaca_bars

    def test_alpaca_empty_falls_back_to_yfinance(self, collector):
        collector._alpaca_api_key = "key"
        yf_bars = [{"volume": 200, "close": 2.0, "open": 2.0}]
        with (
            patch.object(collector, "_fetch_alpaca", return_value=[]),
            patch.object(collector, "_fetch_yfinance", return_value=yf_bars) as mock_yf,
        ):
            bars = collector.fetch_bars("CL=F")
        mock_yf.assert_called_once()
        assert bars == yf_bars


class TestInitWiring:
    def test_config_stored_by_identity(self, collector, mock_config):
        assert collector.config is mock_config

    def test_poll_interval_from_config(self, collector):
        assert collector._poll_interval == 60

    def test_alpaca_credentials_from_config(self, mock_config, mock_db):
        mock_config.futures.alpaca_api_key = "ak"
        mock_config.futures.alpaca_api_secret = "as"
        c = FuturesVolumeCollector(config=mock_config, db=mock_db)
        assert c._alpaca_api_key == "ak"
        assert c._alpaca_api_secret == "as"
