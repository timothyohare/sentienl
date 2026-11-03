"""Unit tests for collectors/betfair_client.py (plan 06, Task 2).

The real betfairlightweight client is replaced by a fake at the factory
boundary, so these tests never touch the network.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from betfairlightweight.exceptions import APIError, LoginError

from sentinel.collectors.betfair_client import (
    BOOK_BATCH_SIZE,
    BetfairClient,
    LoginGuard,
    LoginLockout,
    batch_ids,
    load_credentials,
    read_env_file,
)
from sentinel.core.config import BetfairConfig

# ---------------------------------------------------------------------------
# Credentials (D9: betfair_-prefixed only; regression for the 2026-08-11
# .env collision that killed Truth Social for 3 days)
# ---------------------------------------------------------------------------

class TestLoadCredentials:
    def test_prefixed_keys_from_env_file(self):
        creds = load_credentials(
            {"betfair_username": "u", "betfair_password": "p", "betfair_app_key": "k",
             "betfair_cert_path": "/c"},
            {},
        )
        assert (creds.username, creds.password, creds.app_key, creds.cert_path) == (
            "u", "p", "k", "/c")

    def test_bare_username_is_not_used(self):
        with pytest.raises(ValueError, match="betfair_username"):
            load_credentials({"username": "ts", "password": "x", "betfair_app_key": "k"}, {})

    def test_environment_fallback(self):
        creds = load_credentials(
            {}, {"BETFAIR_USERNAME": "u", "BETFAIR_PASSWORD": "p", "BETFAIR_APP_KEY": "k"}
        )
        assert creds.username == "u" and creds.cert_path is None

    def test_env_file_wins_over_environment(self):
        creds = load_credentials(
            {"betfair_username": "file", "betfair_password": "p", "betfair_app_key": "k"},
            {"BETFAIR_USERNAME": "env"},
        )
        assert creds.username == "file"

    def test_missing_values_named_but_never_echoed(self):
        with pytest.raises(ValueError) as exc:
            load_credentials({"betfair_password": "s3cret"}, {})
        msg = str(exc.value)
        assert "betfair_username" in msg and "betfair_app_key" in msg
        assert "s3cret" not in msg


# ---------------------------------------------------------------------------
# Request-weight batching (S2: 40 markets max with EX_BEST_OFFERS, 41 fails)
# ---------------------------------------------------------------------------

class TestBatchIds:
    def test_batch_size_matches_measured_limit(self):
        assert BOOK_BATCH_SIZE == 40

    def test_splits_into_batches(self):
        ids = [str(i) for i in range(85)]
        batches = list(batch_ids(ids))
        assert [len(b) for b in batches] == [40, 40, 5]
        assert [i for b in batches for i in b] == ids

    def test_empty(self):
        assert list(batch_ids([])) == []

    def test_exact_multiple(self):
        assert [len(b) for b in batch_ids(["x"] * 80)] == [40, 40]


# ---------------------------------------------------------------------------
# Login backoff + hard cap (D11: repeated failed logins can lock the account)
# ---------------------------------------------------------------------------

class TestLoginGuard:
    def test_delays_grow_then_cap(self):
        g = LoginGuard(max_failures=10, base_delay=30, max_delay=600)
        delays = [g.record_failure() for _ in range(6)]
        assert delays == [30, 60, 120, 240, 480, 600]

    def test_locks_out_after_max_failures(self):
        g = LoginGuard(max_failures=3)
        g.record_failure()
        g.record_failure()
        with pytest.raises(LoginLockout):
            g.record_failure()
        assert g.locked_out

    def test_success_resets(self):
        g = LoginGuard(max_failures=3, base_delay=30)
        g.record_failure()
        g.record_failure()
        g.record_success()
        assert g.failures == 0 and g.record_failure() == 30


# ---------------------------------------------------------------------------
# BetfairClient (wrapper around a fake betfairlightweight.APIClient)
# ---------------------------------------------------------------------------

def _fake_api():
    api = MagicMock()
    api.session_token = None
    api.session_expired = False

    def login():
        api.session_token = "TOK"
    api.login_interactive.side_effect = login
    api.login.side_effect = login
    return api


@pytest.fixture
def api():
    return _fake_api()


@pytest.fixture
def sleeps():
    return []


def _client(api, sleeps, **cfg):
    creds = load_credentials(
        {"betfair_username": "u", "betfair_password": "p", "betfair_app_key": "k",
         "betfair_cert_path": "/certs"}, {})
    return BetfairClient(
        BetfairConfig(**cfg), creds, api_factory=lambda **kw: api, sleep=sleeps.append
    )


class TestClientConstruction:
    def test_uris_come_from_config_not_locale(self, sleeps):
        captured = {}

        def factory(**kw):
            captured.update(kw)
            return _fake_api()
        creds = load_credentials(
            {"betfair_username": "u", "betfair_password": "p", "betfair_app_key": "k"}, {})
        c = BetfairClient(BetfairConfig(api_url="https://x/exchange/"), creds,
                          api_factory=factory, sleep=sleeps.append)
        assert captured["lightweight"] is True
        assert captured["password"] == "p" and captured["app_key"] == "k"
        assert c.api.api_uri == "https://x/exchange/"
        assert c.api.identity_uri == "https://identitysso.betfair.com.au/api/"
        assert c.api.identity_cert_uri == "https://identitysso-cert.betfair.com.au/api/"


class TestSession:
    def test_logs_in_lazily_interactive(self, api, sleeps):
        c = _client(api, sleeps)
        api.betting.list_market_book.return_value = []
        c.list_market_book(["1.1"])
        api.login_interactive.assert_called_once()
        api.login.assert_not_called()

    def test_cert_login_mode(self, api, sleeps):
        c = _client(api, sleeps, login="cert")
        api.betting.list_market_book.return_value = []
        c.list_market_book(["1.1"])
        api.login.assert_called_once()

    def test_keep_alive_when_expired(self, api, sleeps):
        c = _client(api, sleeps)
        api.betting.list_market_book.return_value = []
        c.list_market_book(["1.1"])
        api.session_expired = True
        c.list_market_book(["1.1"])
        api.keep_alive.assert_called_once()
        assert api.login_interactive.call_count == 1

    def test_failed_keep_alive_relogs_in(self, api, sleeps):
        c = _client(api, sleeps)
        api.betting.list_market_book.return_value = []
        c.list_market_book(["1.1"])
        api.session_expired = True
        api.keep_alive.side_effect = Exception("nope")
        c.list_market_book(["1.1"])
        assert api.login_interactive.call_count == 2

    def test_login_failure_backs_off_and_returns_empty(self, api, sleeps):
        api.login_interactive.side_effect = LoginError({"loginStatus": "FAIL"})
        c = _client(api, sleeps)
        assert c.list_market_book(["1.1"]) == []
        assert sleeps == [30]
        api.betting.list_market_book.assert_not_called()

    def test_lockout_stops_all_login_attempts(self, api, sleeps):
        api.login_interactive.side_effect = LoginError({"loginStatus": "FAIL"})
        c = _client(api, sleeps)
        for _ in range(10):
            c.list_market_book(["1.1"])
        attempts = api.login_interactive.call_count
        assert c.locked_out
        c.list_market_book(["1.1"])
        assert api.login_interactive.call_count == attempts

    def test_invalid_session_relogs_in_and_retries_once(self, api, sleeps):
        c = _client(api, sleeps)
        api.betting.list_market_book.side_effect = [
            APIError(None, "listMarketBook", {}, "INVALID_SESSION_INFORMATION"),
            [{"marketId": "1.1"}],
        ]
        assert c.list_market_book(["1.1"]) == [{"marketId": "1.1"}]
        assert api.login_interactive.call_count == 2

    def test_other_api_error_returns_empty(self, api, sleeps):
        c = _client(api, sleeps)
        api.betting.list_market_book.side_effect = APIError(None, "m", {}, "TOO_MUCH_DATA")
        assert c.list_market_book(["1.1"]) == []


class TestMarketCalls:
    def test_book_batches_and_concatenates(self, api, sleeps):
        c = _client(api, sleeps)
        api.betting.list_market_book.side_effect = lambda market_ids, **kw: [
            {"marketId": m} for m in market_ids]
        ids = [f"1.{i}" for i in range(45)]
        out = c.list_market_book(ids)
        assert [b["marketId"] for b in out] == ids
        calls = api.betting.list_market_book.call_args_list
        assert [len(cl.kwargs["market_ids"]) for cl in calls] == [40, 5]
        assert calls[0].kwargs["price_projection"] == {"priceData": ["EX_BEST_OFFERS"]}

    def test_book_empty_ids_makes_no_call(self, api, sleeps):
        c = _client(api, sleeps)
        assert c.list_market_book([]) == []
        api.betting.list_market_book.assert_not_called()

    def test_catalogue_passes_filter(self, api, sleeps):
        c = _client(api, sleeps)
        api.betting.list_market_catalogue.return_value = [{"marketId": "1.1"}]
        out = c.list_market_catalogue({"eventTypeIds": ["7"]}, max_results=5)
        assert out == [{"marketId": "1.1"}]
        kw = api.betting.list_market_catalogue.call_args.kwargs
        assert kw["filter"] == {"eventTypeIds": ["7"]} and kw["max_results"] == 5
        assert "EVENT" in kw["market_projection"]
        assert "RUNNER_DESCRIPTION" in kw["market_projection"]


# ---------------------------------------------------------------------------
# Read-only guard (plan §1.3): Sentinel never places, cancels or changes bets.
# ---------------------------------------------------------------------------

ORDER_METHODS = re.compile(
    r"\b(place|cancel|replace|update)_?[Oo]rders\b"
)


def test_no_order_methods_anywhere_in_sentinel():
    root = Path(__file__).resolve().parents[2] / "sentinel"
    offenders = [
        str(p) for p in root.rglob("*.py") if ORDER_METHODS.search(p.read_text())
    ]
    assert offenders == []


def test_guard_pattern_catches_both_spellings():
    assert ORDER_METHODS.search("client.betting.place_orders(")
    assert ORDER_METHODS.search('"SportsAPING/v1.0/placeOrders"')
    assert not ORDER_METHODS.search("list_market_book")


class TestReadEnvFile:
    def test_parses_and_skips_comments_and_blanks(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("# comment\n\nbetfair_username = u \nnoequals\nbetfair_password=a=b\n")
        assert read_env_file(str(p)) == {"betfair_username": "u", "betfair_password": "a=b"}

    def test_missing_file_is_empty(self, tmp_path):
        assert read_env_file(str(tmp_path / "nope")) == {}
