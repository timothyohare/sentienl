"""
collectors/betfair_client.py — read-only Betfair Exchange API client.

Wraps betfairlightweight (plan 06, D10) behind BetfairClientProtocol so the
collector and its tests never depend on the library directly. Only market
data calls are wrapped. Sentinel never places, cancels or changes bets, and
test_betfair_client.py fails if any order method name appears under sentinel/.

Facts measured on 2026-11-03 with the delayed app key (plan 06 §4):
  - api.betfair.com, api.betfair.com.au and api-au.betfair.com serve the same
    markets; login for an AU account is identitysso.betfair.com.au.
  - listMarketBook with EX_BEST_OFFERS weighs 5/market: 40 markets per call is
    the most that works, 41 returns TOO_MUCH_DATA.
  - The delayed key returns market-level totalMatched but runner-level
    totalMatched is 0.0 and tradedVolume is empty.
"""

import functools
import logging
import os
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from sentinel.core.config import BetfairConfig

logger = logging.getLogger(__name__)

BOOK_BATCH_SIZE = 40
BOOK_PRICE_PROJECTION = {"priceData": ["EX_BEST_OFFERS"]}
CATALOGUE_PROJECTION = ["EVENT", "MARKET_START_TIME", "RUNNER_DESCRIPTION"]
CERT_IDENTITY_URL_AU = "https://identitysso-cert.betfair.com.au/api/"


class BetfairClientProtocol(Protocol):
    def list_market_catalogue(
        self, market_filter: dict[str, Any], max_results: int
    ) -> list[dict[str, Any]]: ...

    def list_market_book(self, market_ids: list[str]) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BetfairCredentials:
    username: str
    password: str
    app_key: str
    cert_path: str | None = None

    def __repr__(self) -> str:  # never leak secrets into logs
        return f"BetfairCredentials(username={self.username!r}, password=***, app_key=***)"


def load_credentials(
    env_file: Mapping[str, str], environ: Mapping[str, str] | None = None
) -> BetfairCredentials:
    """Read betfair_-prefixed keys from a parsed .env, falling back to
    BETFAIR_* environment variables. Bare username/password keys are
    deliberately ignored (they belong to Truth Social; see CLAUDE.md)."""
    environ = os.environ if environ is None else environ

    def get(name: str) -> str | None:
        return env_file.get(f"betfair_{name}") or environ.get(f"BETFAIR_{name.upper()}")

    missing = [f"betfair_{n}" for n in ("username", "password", "app_key") if not get(n)]
    if missing:
        raise ValueError(f"Betfair credentials missing: {', '.join(missing)}")
    return BetfairCredentials(
        username=get("username") or "",
        password=get("password") or "",
        app_key=get("app_key") or "",
        cert_path=get("cert_path"),
    )


def read_env_file(path: str) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file. Missing file → empty dict."""
    env: dict[str, str] = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                env[key.strip()] = val.strip()
    except FileNotFoundError:
        pass
    return env


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def batch_ids(ids: list[str], size: int = BOOK_BATCH_SIZE) -> Iterator[list[str]]:
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


class LoginLockout(Exception):
    """Raised once login has failed max_failures times in a row."""


class LoginGuard:
    """Exponential backoff for failed logins, with a hard stop. Repeated
    failed logins can get a Betfair account locked, which only a human can
    fix, so after max_failures we stop trying until the process restarts."""

    def __init__(self, max_failures: int = 5, base_delay: float = 30, max_delay: float = 600):
        self.max_failures = max_failures
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.failures = 0

    @property
    def locked_out(self) -> bool:
        return self.failures >= self.max_failures

    def record_failure(self) -> float:
        self.failures += 1
        if self.locked_out:
            raise LoginLockout(f"Betfair login failed {self.failures} times in a row")
        return float(min(self.base_delay * 2 ** (self.failures - 1), self.max_delay))

    def record_success(self) -> None:
        self.failures = 0


def _default_api_factory(**kwargs: Any) -> Any:
    import betfairlightweight  # local import keeps tests independent of it

    return betfairlightweight.APIClient(**kwargs)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class BetfairClient:
    """Read-only, session-managing Betfair client."""

    def __init__(
        self,
        config: BetfairConfig,
        creds: BetfairCredentials,
        api_factory: Callable[..., Any] = _default_api_factory,
        sleep: Callable[[float], None] = time.sleep,
        guard: LoginGuard | None = None,
    ):
        self.config = config
        self._sleep = sleep
        self.guard = guard or LoginGuard()
        kwargs: dict[str, Any] = {
            "username": creds.username,
            "password": creds.password,
            "app_key": creds.app_key,
            "lightweight": True,
        }
        if creds.cert_path:
            kwargs["certs"] = creds.cert_path
        self.api = api_factory(**kwargs)
        # Set every URI explicitly: the library's locale="australia" silently
        # falls back to the .com endpoints (S4, plan 06 §4.1).
        self.api.identity_uri = config.identity_url
        self.api.identity_cert_uri = CERT_IDENTITY_URL_AU
        self.api.api_uri = config.api_url

    @property
    def locked_out(self) -> bool:
        return self.guard.locked_out

    # -- session ---------------------------------------------------------

    def _login(self) -> bool:
        if self.guard.locked_out:
            return False
        try:
            if self.config.login == "cert":
                self.api.login()
            else:
                self.api.login_interactive()
        except Exception as exc:
            try:
                delay = self.guard.record_failure()
            except LoginLockout:
                logger.error("Betfair login locked out after %d failures (%s); "
                             "not retrying until restart", self.guard.failures, exc)
                return False
            logger.warning("Betfair login failed (%s); backing off %.0fs", exc, delay)
            self._sleep(delay)
            return False
        self.guard.record_success()
        logger.info("Betfair login OK (%s)", self.config.login)
        return True

    def _ensure_session(self) -> bool:
        if not self.api.session_token:
            return self._login()
        if self.api.session_expired:
            try:
                self.api.keep_alive()
                return True
            except Exception as exc:
                logger.warning("Betfair keepAlive failed (%s); logging in again", exc)
                return self._login()
        return True

    def _call(self, fn: Callable[[], list[dict[str, Any]]], what: str) -> list[dict[str, Any]]:
        if not self._ensure_session():
            return []
        for attempt in (1, 2):
            try:
                return fn()
            except Exception as exc:
                if attempt == 1 and "INVALID_SESSION" in str(exc):
                    logger.info("Betfair session invalid during %s; logging in again", what)
                    if self._login():
                        continue
                    return []
                logger.warning("Betfair %s failed: %s", what, str(exc).splitlines()[0])
                return []
        return []

    # -- market data -----------------------------------------------------

    def list_market_catalogue(
        self, market_filter: dict[str, Any], max_results: int
    ) -> list[dict[str, Any]]:
        return self._call(
            lambda: self.api.betting.list_market_catalogue(
                filter=market_filter,
                market_projection=CATALOGUE_PROJECTION,
                max_results=max_results,
                sort="MAXIMUM_TRADED",
            ),
            "listMarketCatalogue",
        )

    def list_market_book(self, market_ids: list[str]) -> list[dict[str, Any]]:
        books: list[dict[str, Any]] = []
        for batch in batch_ids(market_ids):
            books.extend(self._call(
                functools.partial(
                    self.api.betting.list_market_book,
                    market_ids=batch,
                    price_projection=BOOK_PRICE_PROJECTION,
                ),
                "listMarketBook",
            ))
        return books
