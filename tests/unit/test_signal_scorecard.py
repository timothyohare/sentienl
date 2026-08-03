"""
Unit tests for scripts/signal_scorecard.py.

Synthetic price_t0/price_tN fixtures only — no live network, no real db
required for the pure math (effect size, baseline, scorecard assembly).
"""

import pytest

from sentinel.scripts.signal_scorecard import (
    build_scorecard,
    compute_baseline,
    compute_effect_sizes,
    effect_size,
)


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
