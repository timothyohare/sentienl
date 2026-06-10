"""
collectors/truth_social_client.py — Playwright-based HTTP client for Truth Social.

Cloudflare blocks direct HTTP requests (httpx/curl) to truthsocial.com.
This module uses a headless Chromium browser to:
  1. Navigate to truthsocial.com (passes Cloudflare JS challenge)
  2. Log in via the web UI to obtain a bearer token
  3. Make API calls via in-browser fetch()

The browser session is kept alive for the lifetime of the collector process.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from playwright.sync_api import sync_playwright, Playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

BASE_URL = "https://truthsocial.com"

# The web app's own OAuth client credentials (embedded in the SPA JS).
# These are public — they identify the app, not the user.
_APP_CLIENT_ID = "9X1Fdd-pxNsAgEDNi_SfhJWi8T-vLuV2WVzKIbkTCw4"
_APP_CLIENT_SECRET = "ozF8jzI4968oTKFkEnsBC-UbLPCdrSv0MkXGQu2o_-M"


class TruthSocialClient:
    """
    Playwright-based client that authenticates with Truth Social and
    provides methods for API access via in-browser fetch().
    """

    def __init__(self, username: str, password: str):
        self._username = username
        self._password = password
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._bearer_token: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch browser, navigate to Truth Social, and log in."""
        logger.info("Launching headless browser...")
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        self._context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
                "Gecko/20100101 Firefox/128.0"
            ),
            viewport={"width": 1280, "height": 720},
            locale="en-US",
        )
        self._page = self._context.new_page()

        logger.info("Navigating to truthsocial.com...")
        self._page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
        self._page.wait_for_timeout(3000)

        self._login()

    def stop(self) -> None:
        """Close browser and Playwright."""
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None
        self._page = None
        self._context = None
        self._bearer_token = None

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def _login(self) -> None:
        """Log in via the web UI modal and extract the bearer token."""
        page = self._page
        assert page is not None

        # Dismiss cookie banner if present
        page.evaluate(
            "document.getElementById('cookiescript_injected_wrapper')?.remove()"
        )
        page.wait_for_timeout(500)

        # Click "Sign In" to open the login modal
        page.click('text="Sign In"', timeout=10000)
        page.wait_for_timeout(2000)

        # Fill and submit the login form
        page.fill('input[name="username"]', self._username)
        page.fill('input[name="password"]', self._password)
        page.click('button:has-text("Sign In"):visible')
        page.wait_for_timeout(8000)

        # Extract bearer token from localStorage
        auth_raw = page.evaluate("() => localStorage.getItem('truth:auth')")
        if not auth_raw:
            raise RuntimeError(
                "Login failed — no auth data in localStorage. "
                "Check username/password."
            )
        auth_data = json.loads(auth_raw)
        tokens = auth_data.get("tokens", {})
        if not tokens:
            raise RuntimeError(
                "Login failed — no tokens in auth data. "
                "Check username/password."
            )
        self._bearer_token = list(tokens.keys())[0]
        logger.info("Successfully logged in to Truth Social")

    # ------------------------------------------------------------------
    # API access
    # ------------------------------------------------------------------

    def api_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """
        Make a GET request to the Truth Social API using in-browser fetch().

        Returns parsed JSON on success, None on error.
        """
        page = self._page
        token = self._bearer_token
        if page is None or token is None:
            logger.error("Client not started — call start() first")
            return None

        # Build query string
        qs = ""
        if params:
            parts = [f"{k}={v}" for k, v in params.items()]
            qs = "?" + "&".join(parts)

        url = f"{path}{qs}"

        try:
            result = page.evaluate(
                """async ([url, token]) => {
                    try {
                        const resp = await fetch(url, {
                            headers: {'Authorization': 'Bearer ' + token}
                        });
                        return {ok: resp.ok, status: resp.status, body: await resp.text()};
                    } catch (e) {
                        return {ok: false, status: 0, body: e.message};
                    }
                }""",
                [url, token],
            )
        except Exception as exc:
            logger.error("Browser fetch failed: %s", exc)
            return None

        if not result["ok"]:
            logger.warning(
                "API %s returned %d: %s",
                path,
                result["status"],
                result["body"][:200],
            )
            return None

        try:
            return json.loads(result["body"])
        except json.JSONDecodeError:
            logger.error("Invalid JSON from %s", path)
            return None

    def fetch_posts(
        self, account_id: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Fetch recent posts for the given account ID."""
        data = self.api_get(
            f"/api/v1/accounts/{account_id}/statuses",
            params={"limit": limit, "exclude_replies": "true"},
        )
        if isinstance(data, list):
            return data
        return []

    def resolve_account_id(self, handle: str) -> Optional[str]:
        """Resolve a handle to an account ID via the lookup endpoint."""
        data = self.api_get(
            "/api/v1/accounts/lookup",
            params={"acct": handle},
        )
        if isinstance(data, dict):
            return str(data.get("id", ""))
        return None
