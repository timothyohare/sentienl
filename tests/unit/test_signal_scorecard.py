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
    effect_size,
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
        scorecard = build_scorecard(rows, random_baseline={"price_t15": 0.20})
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
        scorecard = build_scorecard(rows, random_baseline={"price_t60": 0.01})
        entries = [e for e in scorecard if e["horizon"] == "price_t15"]
        assert entries
        assert all(e["baseline_source"] == "pooled_proxy" for e in entries)


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
        assert "price_t15" not in baseline

    def test_includes_horizon_once_min_samples_reached(self, tmp_db):
        # 6 samples, 15min apart -> 5 consecutive pairs at price_t15.
        prices = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        for i, price in enumerate(prices):
            tmp_db.price_samples.insert("kalshi", "T1", price, sampled_at=_ts(i * 15))

        baseline = compute_random_baseline(tmp_db, {("kalshi", "T1")}, min_samples=5)
        assert "price_t15" in baseline
        assert baseline["price_t15"] > 0

    def test_pools_across_multiple_instruments(self, tmp_db):
        # 3 pairs from each of two instruments = 6 pooled pairs, clears
        # the default min_samples=5 even though neither instrument alone does.
        for i, price in enumerate([100.0, 101.0, 102.0, 103.0]):
            tmp_db.price_samples.insert("kalshi", "T1", price, sampled_at=_ts(i * 15))
        for i, price in enumerate([50.0, 51.0, 52.0, 53.0]):
            tmp_db.price_samples.insert("kalshi", "T2", price, sampled_at=_ts(i * 15))

        baseline = compute_random_baseline(tmp_db, {("kalshi", "T1"), ("kalshi", "T2")})
        assert "price_t15" in baseline
