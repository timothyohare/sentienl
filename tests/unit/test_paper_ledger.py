"""Unit tests for sentinel/paper/ledger.py — the simulated paper-trading
ledger (tasks/plan.md)."""

import pytest

from sentinel.core.config import PaperCost
from sentinel.paper.ledger import round_trip_cost_ret

# ---------------------------------------------------------------------------
# round_trip_cost_ret() — cost as a fraction of entry price
# ---------------------------------------------------------------------------


class TestRoundTripCostRet:
    def test_micro_futures_commission_and_slippage(self):
        # MGC: 10 oz, $0.10 tick = $1/tick. At 4000 the contract is $40,000.
        # Round trip = 2 x $0.62 commission + 2 x 1 tick ($1) = $3.24.
        mgc = PaperCost(tick_size=0.1, multiplier=10, commission_per_side=0.62,
                        slippage_ticks=1)
        assert round_trip_cost_ret(mgc, 4000.0) == pytest.approx(3.24 / 40_000)

    def test_asx_percentage_commission_hits_minimum(self):
        # 0.08% of $5,000 is $4, below the $6 minimum: 2 x $6 / $5,000 = 0.24%,
        # plus 2 x 1c slippage on a $40 share = 0.05%.
        asx = PaperCost(tick_size=0.01, commission_pct=0.08, commission_min=6.0,
                        notional=5000.0)
        assert round_trip_cost_ret(asx, 40.0) == pytest.approx(0.0024)
        asx.slippage_ticks = 1
        assert round_trip_cost_ret(asx, 40.0) == pytest.approx(0.0024 + 0.0005)

    def test_asx_percentage_commission_above_minimum(self):
        # 0.08% of $20,000 = $16 > $6, so the percentage applies: 0.16% round trip.
        asx = PaperCost(commission_pct=0.08, commission_min=6.0, notional=20_000.0)
        assert round_trip_cost_ret(asx, 40.0) == pytest.approx(0.0016)

    def test_non_positive_price_rejected(self):
        with pytest.raises(ValueError):
            round_trip_cost_ret(PaperCost(), 0.0)
