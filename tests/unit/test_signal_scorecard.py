"""
Unit tests for scripts/signal_scorecard.py.

Synthetic price_t0/price_tN fixtures only — no live network. The
random-window baseline touches a real (tmp_path) sqlite db, same pattern as
test_price_followup.py's mock_db, since it reads price_samples directly.
"""

from datetime import UTC, datetime, timedelta

import pytest

from sentinel.core.db import Database
from sentinel.scripts.signal_scorecard import (
    build_scorecard,
    compute_baseline,
    compute_effect_sizes,
    compute_random_baseline,
    compute_random_baseline_stats,
    effect_size,
    in_asx_session,
)


@pytest.fixture
def tmp_db(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.init()
    yield db
    db.close()


_BASE_TIME = datetime(2026, 8, 1, tzinfo=UTC)


def _ts(minutes: float) -> str:
    return (_BASE_TIME + timedelta(minutes=minutes)).isoformat()


class TestEffectSize:
    def test_no_move_is_zero(self):
        assert effect_size(100.0, 100.0) == 0.0

    def test_positive_move(self):
        assert effect_size(100.0, 110.0) == 0.10

    def test_negative_move_uses_absolute_value(self):
        assert effect_size(100.0, 90.0) == 0.10

    def test_negative_base_price_uses_absolute_denominator(self):
        # Kalshi prices are always positive, but the formula should still
        # behave sanely if it ever sees a negative t0.
        assert effect_size(-50.0, -55.0) == 0.10


class TestComputeEffectSizes:
    def test_groups_by_source_type_horizon(self):
        rows = [
            {"source": "kalshi", "signal_type": "large_bet",
             "price_t0": 0.30, "price_t15": 0.33, "price_t60": None,
             "price_t240": None, "price_t1440": None},
        ]
        sizes = compute_effect_sizes(rows)
        assert sizes[("kalshi", "large_bet", "price_t15")] == pytest.approx([0.10])
        assert ("kalshi", "large_bet", "price_t60") not in sizes

    def test_null_horizon_skipped(self):
        rows = [
            {"source": "kalshi", "signal_type": "large_bet",
             "price_t0": 0.30, "price_t15": None, "price_t60": None,
             "price_t240": None, "price_t1440": None},
        ]
        sizes = compute_effect_sizes(rows)
        assert sizes == {}

    def test_multiple_rows_accumulate(self):
        rows = [
            {"source": "kalshi", "signal_type": "large_bet",
             "price_t0": 0.30, "price_t15": 0.33, "price_t60": None,
             "price_t240": None, "price_t1440": None},
            {"source": "kalshi", "signal_type": "large_bet",
             "price_t0": 0.50, "price_t15": 0.45, "price_t60": None,
             "price_t240": None, "price_t1440": None},
        ]
        sizes = compute_effect_sizes(rows)
        assert sizes[("kalshi", "large_bet", "price_t15")] == pytest.approx([0.10, 0.10])


class TestComputeBaseline:
    def test_pools_across_all_groups_at_same_horizon(self):
        sizes = {
            ("kalshi", "large_bet", "price_t15"): [0.10, 0.20],
            ("futures_oil", "volume_spike", "price_t15"): [0.02, 0.04],
        }
        baseline = compute_baseline(sizes)
        # median of [0.10, 0.20, 0.02, 0.04] == 0.07
        assert baseline["price_t15"] == 0.07

    def test_horizons_kept_separate(self):
        sizes = {
            ("kalshi", "large_bet", "price_t15"): [0.10],
            ("kalshi", "large_bet", "price_t60"): [0.50],
        }
        baseline = compute_baseline(sizes)
        assert baseline["price_t15"] == 0.10
        assert baseline["price_t60"] == 0.50

    def test_empty_input_yields_empty_baseline(self):
        assert compute_baseline({}) == {}


class TestBuildScorecard:
    def test_group_beats_pooled_baseline(self):
        rows = [
            {"source": "kalshi", "signal_type": "large_bet",
             "price_t0": 0.30, "price_t15": 0.45, "price_t60": None,
             "price_t240": None, "price_t1440": None},  # 50% move
            {"source": "futures_oil", "signal_type": "volume_spike",
             "price_t0": 75.0, "price_t15": 75.1, "price_t60": None,
             "price_t240": None, "price_t1440": None},  # ~0.13% move
        ]
        scorecard = build_scorecard(rows)
        entry = next(
            e for e in scorecard
            if e["source"] == "kalshi" and e["signal_type"] == "large_bet"
        )
        assert entry["n"] == 1
        assert entry["median_effect_size"] == pytest.approx(0.50)
        assert entry["beats_baseline"] is True

    def test_group_at_or_below_baseline_does_not_beat_it(self):
        rows = [
            {"source": "kalshi", "signal_type": "large_bet",
             "price_t0": 100.0, "price_t15": 100.0, "price_t60": None,
             "price_t240": None, "price_t1440": None},  # 0% move
            {"source": "futures_oil", "signal_type": "volume_spike",
             "price_t0": 100.0, "price_t15": 110.0, "price_t60": None,
             "price_t240": None, "price_t1440": None},  # 10% move
        ]
        scorecard = build_scorecard(rows)
        entry = next(
            e for e in scorecard
            if e["source"] == "kalshi" and e["signal_type"] == "large_bet"
        )
        assert entry["beats_baseline"] is False

    def test_no_random_baseline_arg_defaults_to_proxy_for_all(self):
        rows = [
            {"source": "kalshi", "signal_type": "large_bet",
             "price_t0": 0.30, "price_t15": 0.45, "price_t60": None,
             "price_t240": None, "price_t1440": None},
        ]
        scorecard = build_scorecard(rows)
        assert all(e["baseline_source"] == "pooled_proxy" for e in scorecard)

    def test_uses_random_baseline_when_available_for_horizon(self):
        rows = [
            {"source": "kalshi", "signal_type": "large_bet",
             "price_t0": 0.30, "price_t15": 0.45, "price_t60": None,
             "price_t240": None, "price_t1440": None},  # 50% move
        ]
        scorecard = build_scorecard(rows, random_baseline={("kalshi", "price_t15"): 0.20})
        entry = scorecard[0]
        assert entry["baseline_source"] == "random_window"
        assert entry["baseline_effect_size"] == pytest.approx(0.20)
        assert entry["beats_baseline"] is True

    def test_falls_back_to_proxy_for_horizon_missing_from_random_baseline(self):
        rows = [
            {"source": "kalshi", "signal_type": "large_bet",
             "price_t0": 0.30, "price_t15": 0.45, "price_t60": None,
             "price_t240": None, "price_t1440": None},
            {"source": "futures_oil", "signal_type": "volume_spike",
             "price_t0": 75.0, "price_t15": 75.1, "price_t60": None,
             "price_t240": None, "price_t1440": None},
        ]
        # random_baseline covers price_t60, not price_t15 — price_t15 entries
        # must still fall back to the pooled proxy.
        scorecard = build_scorecard(
            rows, random_baseline={("kalshi", "price_t60"): 0.01}
        )
        entries = [e for e in scorecard if e["horizon"] == "price_t15"]
        assert entries
        assert all(e["baseline_source"] == "pooled_proxy" for e in entries)

    def test_random_baseline_is_isolated_per_source(self):
        # kalshi and futures_oil each get their own random-window baseline
        # at the same horizon — one group beating baseline must not depend
        # on the other source's baseline value (the pooling bug this
        # partitioning fixes: a thin/flat source dragging a liquid source's
        # comparison point toward zero).
        rows = [
            {"source": "kalshi", "signal_type": "large_bet",
             "price_t0": 100.0, "price_t15": 100.5, "price_t60": None,
             "price_t240": None, "price_t1440": None},  # 0.5% move
            {"source": "futures_oil", "signal_type": "volume_spike",
             "price_t0": 100.0, "price_t15": 100.5, "price_t60": None,
             "price_t240": None, "price_t1440": None},  # 0.5% move
        ]
        random_baseline = {
            ("kalshi", "price_t15"): 0.0,     # kalshi mostly flat -> beats it
            ("futures_oil", "price_t15"): 0.01,  # futures moves more normally -> does not beat it
        }
        scorecard = build_scorecard(rows, random_baseline=random_baseline)
        kalshi_entry = next(e for e in scorecard if e["source"] == "kalshi")
        futures_entry = next(e for e in scorecard if e["source"] == "futures_oil")
        assert kalshi_entry["baseline_effect_size"] == pytest.approx(0.0)
        assert kalshi_entry["beats_baseline"] is True
        assert futures_entry["baseline_effect_size"] == pytest.approx(0.01)
        assert futures_entry["beats_baseline"] is False


class TestComputeRandomBaseline:
    def test_no_instruments_returns_empty(self, tmp_db):
        assert compute_random_baseline(tmp_db, set()) == {}

    def test_excludes_horizon_below_min_samples(self, tmp_db):
        # 3 samples, 15min apart -> 2 pairs at price_t15 (default tolerance
        # 20% = 3min), below the default min_samples=5.
        tmp_db.price_samples.insert("kalshi", "T1", 100.0, sampled_at=_ts(0))
        tmp_db.price_samples.insert("kalshi", "T1", 101.0, sampled_at=_ts(15))
        tmp_db.price_samples.insert("kalshi", "T1", 102.0, sampled_at=_ts(30))

        baseline = compute_random_baseline(tmp_db, {("kalshi", "T1")})
        assert ("kalshi", "price_t15") not in baseline

    def test_includes_horizon_once_min_samples_reached(self, tmp_db):
        # 6 samples, 15min apart -> 5 consecutive pairs at price_t15.
        prices = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        for i, price in enumerate(prices):
            tmp_db.price_samples.insert("kalshi", "T1", price, sampled_at=_ts(i * 15))

        baseline = compute_random_baseline(tmp_db, {("kalshi", "T1")}, min_samples=5)
        assert ("kalshi", "price_t15") in baseline
        assert baseline[("kalshi", "price_t15")] > 0

    def test_pools_across_multiple_instruments_of_the_same_source(self, tmp_db):
        # 3 pairs from each of two same-source instruments = 6 pooled pairs,
        # clears the default min_samples=5 even though neither instrument
        # alone does.
        for i, price in enumerate([100.0, 101.0, 102.0, 103.0]):
            tmp_db.price_samples.insert("kalshi", "T1", price, sampled_at=_ts(i * 15))
        for i, price in enumerate([50.0, 51.0, 52.0, 53.0]):
            tmp_db.price_samples.insert("kalshi", "T2", price, sampled_at=_ts(i * 15))

        baseline = compute_random_baseline(tmp_db, {("kalshi", "T1"), ("kalshi", "T2")})
        assert ("kalshi", "price_t15") in baseline

    def test_does_not_pool_across_different_sources(self, tmp_db):
        # Regression test for the pooling bug: a source whose samples never
        # move (e.g. a thin/flat Kalshi market) must not drag down the
        # baseline computed for a different, unrelated source (e.g. a
        # liquid futures instrument) at the same horizon.
        for i in range(6):
            tmp_db.price_samples.insert("kalshi", "T1", 100.0, sampled_at=_ts(i * 15))
        for i, price in enumerate([100.0, 105.0, 110.0, 115.0, 120.0, 125.0]):
            tmp_db.price_samples.insert("futures_oil", "CL=F", price, sampled_at=_ts(i * 15))

        baseline = compute_random_baseline(
            tmp_db, {("kalshi", "T1"), ("futures_oil", "CL=F")}, min_samples=5
        )
        assert baseline[("kalshi", "price_t15")] == pytest.approx(0.0)
        assert baseline[("futures_oil", "price_t15")] > 0.0


class TestInAsxSession:
    def test_weekday_midday_sydney_is_in_session(self):
        # 2026-08-03 is a Monday; 12:00 AEST = 02:00 UTC
        assert in_asx_session(datetime(2026, 8, 3, 2, 0, tzinfo=UTC))

    def test_open_is_inclusive_close_is_exclusive(self):
        assert in_asx_session(datetime(2026, 8, 3, 0, 0, tzinfo=UTC))      # 10:00 AEST
        assert not in_asx_session(datetime(2026, 8, 3, 6, 0, tzinfo=UTC))  # 16:00 AEST

    def test_overnight_is_out_of_session(self):
        assert not in_asx_session(datetime(2026, 8, 3, 12, 0, tzinfo=UTC))  # 22:00 AEST

    def test_weekend_is_out_of_session(self):
        assert not in_asx_session(datetime(2026, 8, 1, 2, 0, tzinfo=UTC))  # Saturday

    def test_daylight_saving_is_respected(self):
        # 2026-11-02 (Mon) is AEDT (UTC+11): 23:30 UTC Sunday = 10:30 Monday
        assert in_asx_session(datetime(2026, 11, 1, 23, 30, tzinfo=UTC))


class TestAsxBaselineIgnoresClosedMarket:
    def test_overnight_flat_samples_do_not_zero_the_baseline(self, tmp_db):
        # One trading session of moving prices (10:00-15:55 AEST, 2026-08-03),
        # then many flat overnight samples — like the live data, where ~80%
        # of ASX samples are unchanged because the market is shut.
        start = datetime(2026, 8, 3, 0, 0, tzinfo=UTC)
        for i in range(72):  # 6h of 5-min samples, alternating price
            tmp_db.price_samples.insert(
                "asx", "BHP.AX", 40.0 + (i % 2) * 0.4,
                sampled_at=(start + timedelta(minutes=5 * i)).isoformat())
        for i in range(72, 288):  # rest of the day, closed and flat
            tmp_db.price_samples.insert(
                "asx", "BHP.AX", 40.0,
                sampled_at=(start + timedelta(minutes=5 * i)).isoformat())

        baseline = compute_random_baseline(tmp_db, {("asx", "BHP.AX")})
        assert baseline[("asx", "price_t15")] > 0


class TestRandomMoveRate:
    def test_move_rate_is_fraction_of_nonzero_pairs(self, tmp_db):
        # 15-min samples -> 6 consecutive 15-min pairs, only one of which moved
        prices = [100.0, 100.0, 100.0, 100.0, 101.0, 101.0, 101.0]
        for i, p in enumerate(prices):
            tmp_db.price_samples.insert("kalshi", "T1", p, sampled_at=_ts(i * 15))

        medians, rates = compute_random_baseline_stats(tmp_db, {("kalshi", "T1")})
        assert medians[("kalshi", "price_t15")] == pytest.approx(0.0)
        assert rates[("kalshi", "price_t15")] == pytest.approx(1 / 6)

    def test_scorecard_compares_move_rates_when_baseline_median_is_zero(self):
        rows = [
            {"source": "kalshi", "signal_type": "large_bet", "price_t0": 0.30,
             "price_t15": p, "price_t60": None, "price_t240": None, "price_t1440": None}
            for p in (0.30, 0.30, 0.31)  # moved 1 in 3; median move is 0
        ]
        beats = build_scorecard(
            rows, random_baseline={("kalshi", "price_t15"): 0.0},
            random_move_rate={("kalshi", "price_t15"): 0.10})[0]
        assert beats["baseline_source"] == "random_move_rate"
        assert beats["move_rate"] == pytest.approx(1 / 3)
        assert beats["baseline_move_rate"] == pytest.approx(0.10)
        assert beats["beats_baseline"] is True

        loses = build_scorecard(
            rows, random_baseline={("kalshi", "price_t15"): 0.0},
            random_move_rate={("kalshi", "price_t15"): 0.50})[0]
        assert loses["beats_baseline"] is False

    def test_nonzero_baseline_median_still_uses_effect_size(self):
        rows = [{"source": "futures_oil", "signal_type": "volume_spike", "price_t0": 100.0,
                 "price_t15": 101.0, "price_t60": None, "price_t240": None,
                 "price_t1440": None}]
        entry = build_scorecard(
            rows, random_baseline={("futures_oil", "price_t15"): 0.005},
            random_move_rate={("futures_oil", "price_t15"): 0.99})[0]
        assert entry["baseline_source"] == "random_window"
        assert entry["beats_baseline"] is True
