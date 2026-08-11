"""
Unit tests for scripts/kalshi_liquidity_check.py.

Market fetches are mocked at the protocol boundary (same pattern as
price_followup.py's PriceFetcherProtocol) — no live network in tests.
"""

from sentinel.scripts.kalshi_liquidity_check import (
    check_tickers,
    classify,
    summarize_markets,
)


class FakeFetcher:
    """Records calls and returns canned markets per event ticker."""

    def __init__(self, markets: dict[str, list[dict]]):
        self._markets = markets
        self.calls: list[str] = []

    def fetch_markets(self, event_ticker):
        self.calls.append(event_ticker)
        return self._markets.get(event_ticker, [])


def _market(volume_fp=0, volume_24h_fp=0):
    return {"volume_fp": str(volume_fp), "volume_24h_fp": str(volume_24h_fp)}


# ---------------------------------------------------------------------------
# summarize_markets() — pure aggregation
# ---------------------------------------------------------------------------

class TestSummarizeMarkets:
    def test_sums_volume_across_multiple_markets(self):
        markets = [_market(1000, 100), _market(2000, 50)]
        summary = summarize_markets(markets)
        assert summary["market_count"] == 2
        assert summary["volume_total"] == 3000
        assert summary["volume_24h"] == 150

    def test_empty_markets_list(self):
        summary = summarize_markets([])
        assert summary == {"market_count": 0, "volume_total": 0, "volume_24h": 0}

    def test_tolerates_missing_or_malformed_volume_fields(self):
        markets = [{}, {"volume_fp": "not-a-number", "volume_24h_fp": None}]
        summary = summarize_markets(markets)
        assert summary["market_count"] == 2
        assert summary["volume_total"] == 0
        assert summary["volume_24h"] == 0


# ---------------------------------------------------------------------------
# classify() — pure threshold logic
# ---------------------------------------------------------------------------

class TestClassify:
    def test_no_open_markets_is_flagged(self):
        summary = {"market_count": 0, "volume_total": 0, "volume_24h": 0}
        assert classify(summary, min_24h_volume=100) == "NO_MARKETS"

    def test_below_threshold_is_quiet(self):
        summary = {"market_count": 1, "volume_total": 500, "volume_24h": 11}
        assert classify(summary, min_24h_volume=100) == "QUIET"

    def test_at_threshold_is_ok(self):
        summary = {"market_count": 1, "volume_total": 500, "volume_24h": 100}
        assert classify(summary, min_24h_volume=100) == "OK"

    def test_above_threshold_is_ok(self):
        summary = {"market_count": 1, "volume_total": 500, "volume_24h": 3167}
        assert classify(summary, min_24h_volume=100) == "OK"


# ---------------------------------------------------------------------------
# check_tickers() — orchestration against a fake fetcher
# ---------------------------------------------------------------------------

class TestCheckTickers:
    def test_classifies_each_tracked_ticker(self):
        fetcher = FakeFetcher({
            "LIQUID": [_market(500000, 3167)],
            "DEAD": [_market(78244, 11)],
            "GONE": [],
        })
        results = check_tickers(["LIQUID", "DEAD", "GONE"], fetcher, min_24h_volume=100)

        assert results["LIQUID"]["status"] == "OK"
        assert results["DEAD"]["status"] == "QUIET"
        assert results["GONE"]["status"] == "NO_MARKETS"
        assert fetcher.calls == ["LIQUID", "DEAD", "GONE"]

    def test_empty_ticker_list_returns_empty_results(self):
        fetcher = FakeFetcher({})
        assert check_tickers([], fetcher, min_24h_volume=100) == {}
