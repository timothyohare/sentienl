"""Unit tests for scripts/trade_size_study.py.

Question: does a Kalshi trade worth >= $5,000 tell you more about where the
price goes next than an ordinary small trade on the same market? Synthetic
trade tapes only; no network.
"""

import pytest

from sentinel.scripts.trade_size_study import (
    Trade,
    follow_through,
    market_wins,
    parse_trade,
    taker_cost,
)


def _t(t, yes, side="yes", count=10.0, ticker="M"):
    return Trade(ticker=ticker, ts=float(t), yes_price=yes, side=side, count=count)


class TestTakerCost:
    def test_yes_taker_pays_yes_price(self):
        assert taker_cost(_t(0, 0.30, "yes", 100)) == pytest.approx(30.0)

    def test_no_taker_pays_one_minus_yes(self):
        assert taker_cost(_t(0, 0.30, "no", 100)) == pytest.approx(70.0)


class TestParseTrade:
    def test_parses_api_row(self):
        row = {"ticker": "X", "created_time": "2026-11-04T05:02:35.528011Z",
               "yes_price_dollars": "0.9990", "taker_side": "no", "count_fp": "37.00"}
        t = parse_trade(row)
        assert (t.ticker, t.yes_price, t.side, t.count) == ("X", 0.999, "no", 37.0)
        assert t.ts == pytest.approx(1793768555.528011)

    def test_bad_row_is_none(self):
        assert parse_trade({"ticker": "X"}) is None


class TestFollowThrough:
    def test_price_at_horizon_is_last_trade_at_or_before_it(self):
        # Large yes buy at 0.40, then trades at +5 min (0.45) and +20 min (0.50).
        tape = [_t(0, 0.40, "yes", 20000), _t(300, 0.45), _t(1200, 0.50)]
        out = follow_through(tape, {"t15": 15}, usd_threshold=5000, end_ts=10_000)
        large = out["large"]["t15"]
        assert large["n"] == 1
        assert large["moved"] == 1 and large["with_taker"] == 1 and large["against"] == 0
        assert large["moves_pp"] == [pytest.approx(5.0)]

    def test_no_taker_counts_a_fall_as_with_taker(self):
        tape = [_t(0, 0.60, "no", 20000), _t(60, 0.55)]
        out = follow_through(tape, {"t15": 15}, usd_threshold=5000, end_ts=10_000)
        assert out["large"]["t15"]["with_taker"] == 1

    def test_against_taker(self):
        tape = [_t(0, 0.60, "yes", 20000), _t(60, 0.55)]
        out = follow_through(tape, {"t15": 15}, usd_threshold=5000, end_ts=10_000)
        assert out["large"]["t15"]["against"] == 1

    def test_no_later_trade_means_unmoved(self):
        tape = [_t(0, 0.40, "yes", 20000), _t(5000, 0.90)]
        out = follow_through(tape, {"t15": 15}, usd_threshold=5000, end_ts=10_000)
        assert out["large"]["t15"]["moved"] == 0

    def test_horizon_past_end_of_tape_is_skipped(self):
        tape = [_t(0, 0.40, "yes", 20000), _t(60, 0.41)]
        out = follow_through(tape, {"t15": 15}, usd_threshold=5000, end_ts=600)
        assert out["large"]["t15"]["n"] == 0
        assert out["small"]["t15"]["n"] == 0

    def test_splits_large_and_small_by_dollars_paid(self):
        tape = [
            _t(0, 0.50, "yes", 10000),   # $5,000 -> large
            _t(10, 0.50, "yes", 9998),   # $4,999 -> small
            _t(20, 0.005, "yes", 50000),  # $250 of penny contracts -> small
        ]
        out = follow_through(tape, {"t15": 15}, usd_threshold=5000, end_ts=10_000)
        assert out["large"]["t15"]["n"] == 1
        assert out["small"]["t15"]["n"] == 2

    def test_markets_are_kept_separate(self):
        tape = [_t(0, 0.40, "yes", 20000, "A"), _t(60, 0.90, ticker="B")]
        out = follow_through(tape, {"t15": 15}, usd_threshold=5000, end_ts=10_000)
        assert out["large"]["t15"]["moved"] == 0


class TestMarketWins:
    def test_counts_markets_where_large_beats_small(self):
        # Market A: large follows through, small doesn't. Market B: reverse.
        tape = [
            _t(0, 0.40, "yes", 20000, "A"), _t(60, 0.45, ticker="A"),
            _t(2000, 0.45, "yes", 10, "A"), _t(2060, 0.40, ticker="A"),
            _t(0, 0.40, "yes", 20000, "B"), _t(60, 0.35, ticker="B"),
            _t(2000, 0.35, "yes", 10, "B"), _t(2060, 0.40, ticker="B"),
        ]
        wins, compared = market_wins(tape, "t15", 15, usd_threshold=5000, end_ts=10_000)
        assert (wins, compared) == (1, 2)
