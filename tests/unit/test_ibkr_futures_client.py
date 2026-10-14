"""
Unit tests for collectors/ibkr_futures_client.py.

The live-socket class (_HistoricalBarsClient) and fetch_bars()/
fetch_latest_price() are deliberately left untested here — same convention
already applied to _fetch_yfinance/_fetch_alpaca in futures_volume.py and
the Truth Social Playwright client (see CLAUDE.md's mutation-coverage
notes): I/O-boundary code, not business logic, requires a live IB Gateway
to exercise meaningfully. Only the pure contract-building/mapping surface
and the wrapper's error() classification (no socket needed) are tested.
"""

import pytest
from ibapi.contract import Contract

from sentinel.collectors import ibkr_asx_client, ibkr_futures_client
from sentinel.collectors.ibkr_futures_client import IB_CONTRACT_MAP, _contfut_contract


class TestIbContractMap:
    def test_covers_all_six_tracked_instruments(self):
        assert set(IB_CONTRACT_MAP.keys()) == {
            "CL=F", "BZ=F", "NG=F", "GC=F", "ES=F", "DX-Y.NYB",
        }

    def test_every_mapping_has_nonempty_symbol_and_exchange(self):
        for ticker, (symbol, exchange) in IB_CONTRACT_MAP.items():
            assert symbol, f"{ticker} has an empty IB symbol"
            assert exchange, f"{ticker} has an empty IB exchange"


class TestContfutContract:
    def test_builds_continuous_futures_contract(self):
        contract = _contfut_contract("CL", "NYMEX")
        assert contract.symbol == "CL"
        assert contract.secType == "CONTFUT"
        assert contract.exchange == "NYMEX"
        assert contract.currency == "USD"


class TestHistoricalBarsClientErrorHandling:
    """IB's 2100-2199 range is warnings, not failures. Treating 2172 (Gateway
    version deprecation notice, sent on every connection from 2026-10-13) as
    fatal silently discarded every IB bar and degraded futures, ASX and
    price-followup to yfinance-only."""

    @pytest.mark.parametrize("module", [ibkr_futures_client, ibkr_asx_client])
    @pytest.mark.parametrize("code", [2104, 2158, 2172, 2100, 2199])
    def test_warning_codes_are_not_fatal(self, module, code):
        client = module._HistoricalBarsClient(Contract(), "3600 S", "1 min")
        client.error(-1, 0, code, "warning text")
        assert client.error_detail is None
        assert not client.done.is_set()

    @pytest.mark.parametrize("module", [ibkr_futures_client, ibkr_asx_client])
    @pytest.mark.parametrize("code", [162, 200, 354, 504, 10197, 2200])
    def test_real_errors_are_fatal(self, module, code):
        client = module._HistoricalBarsClient(Contract(), "3600 S", "1 min")
        client.error(1, 0, code, "boom")
        assert client.error_detail == f"[{code}] boom"
        assert client.done.is_set()
