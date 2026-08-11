"""
Unit tests for collectors/ibkr_futures_client.py.

The live-socket class (_HistoricalBarsClient) and fetch_bars()/
fetch_latest_price() are deliberately left untested here — same convention
already applied to _fetch_yfinance/_fetch_alpaca in futures_volume.py and
the Truth Social Playwright client (see CLAUDE.md's mutation-coverage
notes): I/O-boundary code, not business logic, requires a live IB Gateway
to exercise meaningfully. Only the pure contract-building/mapping surface
is tested.
"""

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
