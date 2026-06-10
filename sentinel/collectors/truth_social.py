"""
collectors/truth_social.py — Truth Social polling collector.

Polls the Trump Truth Social account for new posts and writes signals directly
to SQLite. Runs in a synchronous loop with time.sleep().

Key behaviours:
- Resolves account ID on startup; falls back to hardcoded ID if lookup fails.
- Backfills missed posts on startup (compares last 20 posts against state).
- Exponential backoff on errors / empty responses.
- Writes CRITICAL priority signals to the signals table.
- Tracks last seen post ID in the state table.
- Uses a pluggable ``client`` for HTTP access. In production this is a
  Playwright-based browser client that bypasses Cloudflare; in tests it
  can be replaced with a simple mock.
"""

import logging
import re
import time
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://truthsocial.com"
ACCOUNT_ID_FALLBACK = "107780257626128497"

STATE_KEY_LAST_POST_ID = "truth_social_last_post_id"
STATE_KEY_ACCOUNT_ID = "truth_social_account_id"

DEFAULT_BACKOFF = [30, 60, 120, 300]


# ---------------------------------------------------------------------------
# Client protocol (for type-checking and testability)
# ---------------------------------------------------------------------------

class TruthSocialClientProtocol(Protocol):
    """Minimal interface that the collector needs from its HTTP client."""

    def fetch_posts(
        self, account_id: str, limit: int = 20
    ) -> List[Dict[str, Any]]: ...

    def resolve_account_id(self, handle: str) -> Optional[str]: ...


# ---------------------------------------------------------------------------
# HTML text extraction
# ---------------------------------------------------------------------------

class _HTMLStripper(HTMLParser):
    """Minimal HTML → plain-text converter."""

    def __init__(self):
        super().__init__()
        self._parts: List[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("br", "p"):
            self._parts.append(" ")

    def get_text(self) -> str:
        text = "".join(self._parts)
        # Collapse multiple whitespace
        return re.sub(r"\s+", " ", text).strip()


def _extract_text(html: str, max_chars: int = 280) -> str:
    """Strip HTML tags from a post's content field and return plain text."""
    if not html:
        return ""
    stripper = _HTMLStripper()
    stripper.feed(html)
    text = stripper.get_text()
    if len(text) > max_chars:
        return text[:max_chars] + "..."
    return text


def _build_summary(post: Dict[str, Any], text: str) -> str:
    """Build a one-line human-readable summary for the signals table."""
    post_id = post.get("id", "?")
    short_text = text[:120] + "..." if len(text) > 120 else text
    return f"New Trump post [{post_id}]: {short_text}"


# ---------------------------------------------------------------------------
# Collector class
# ---------------------------------------------------------------------------

class TruthSocialCollector:
    """
    Synchronous Truth Social polling collector.

    Typical usage:
        client = TruthSocialClient(username, password)
        client.start()
        collector = TruthSocialCollector(config, db, client)
        collector.run()  # blocks forever
    """

    def __init__(self, config, db, client: Optional[TruthSocialClientProtocol] = None):
        self.config = config
        self.db = db
        self.client = client
        ts_cfg = config.truth_social
        self._poll_interval = ts_cfg.poll_interval_seconds
        self._account_handle = ts_cfg.account_handle
        self._account_id_fallback = str(ts_cfg.account_id_fallback)
        self._backoff_seconds = list(ts_cfg.backoff_seconds)
        self._alert_all_posts = ts_cfg.alert_all_posts
        self._keyword_filter = list(ts_cfg.keyword_filter)
        self._consecutive_errors = 0

    # ------------------------------------------------------------------
    # Account ID resolution
    # ------------------------------------------------------------------

    def resolve_account_id(self) -> str:
        """
        Resolve the account ID from the handle via the client.
        Falls back to the hardcoded ID on any error.
        """
        if self.client is None:
            logger.warning("No client — using fallback account ID %s", self._account_id_fallback)
            return self._account_id_fallback
        try:
            resolved_id = self.client.resolve_account_id(self._account_handle)
            if resolved_id:
                logger.info("Resolved account ID %s for @%s", resolved_id, self._account_handle)
                return resolved_id
        except Exception as exc:
            logger.warning("Account ID lookup failed (%s)", exc)
        logger.warning("Using fallback account ID %s", self._account_id_fallback)
        return self._account_id_fallback

    # ------------------------------------------------------------------
    # Post fetching
    # ------------------------------------------------------------------

    def fetch_posts(self, account_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Fetch recent posts for the given account ID.
        Returns an empty list on any error.
        """
        if self.client is None:
            logger.error("No client configured — cannot fetch posts")
            return []
        try:
            posts = self.client.fetch_posts(account_id, limit=limit)
            if posts:
                self._consecutive_errors = 0
            return posts
        except Exception as exc:
            logger.error("Truth Social fetch exception: %s", exc)
        return []

    # ------------------------------------------------------------------
    # Post filtering
    # ------------------------------------------------------------------

    def filter_new_posts(
        self, posts: List[Dict[str, Any]], last_post_id: Optional[str]
    ) -> List[Dict[str, Any]]:
        """
        Return posts with IDs numerically greater than last_post_id.
        Posts are assumed to be ordered newest-first from the API.
        Returns oldest-first (for chronological processing).
        """
        if not posts:
            return []
        if last_post_id is None:
            return list(reversed(posts))
        try:
            last_id_int = int(last_post_id)
        except (ValueError, TypeError):
            return list(reversed(posts))
        new = [p for p in posts if int(p["id"]) > last_id_int]
        return list(reversed(new))  # oldest first

    def _matches_keyword_filter(self, text: str) -> bool:
        """Return True if the post should be alerted (matches keyword filter or filter is empty)."""
        if not self._keyword_filter:
            return True
        text_lower = text.lower()
        return any(kw.lower() in text_lower for kw in self._keyword_filter)

    # ------------------------------------------------------------------
    # Signal creation
    # ------------------------------------------------------------------

    def process_post(self, post: Dict[str, Any]) -> Optional[int]:
        """
        Write a new_post signal to the database for the given post.
        Returns the signal ID.
        """
        content_html = post.get("content", "")
        text = _extract_text(content_html)
        is_reblog = post.get("reblog") is not None
        has_media = len(post.get("media_attachments", [])) > 0

        if not self._alert_all_posts and not self._matches_keyword_filter(text):
            logger.debug("Post %s skipped — does not match keyword filter", post.get("id"))
            return None

        payload = {
            "post_id": post.get("id"),
            "created_at": post.get("created_at"),
            "url": post.get("url"),
            "text": text,
            "has_media": has_media,
            "is_reblog": is_reblog,
        }
        summary = _build_summary(post, text)
        signal_id = self.db.insert_signal(
            source="truth_social",
            signal_type="new_post",
            priority="CRITICAL",
            payload=payload,
            summary=summary,
        )
        logger.info("Signal %d created for post %s", signal_id, post.get("id"))
        return signal_id

    # ------------------------------------------------------------------
    # State tracking
    # ------------------------------------------------------------------

    def get_last_post_id(self) -> Optional[str]:
        return self.db.state.get(STATE_KEY_LAST_POST_ID)

    def set_last_post_id(self, post_id: str) -> None:
        self.db.state.set(STATE_KEY_LAST_POST_ID, post_id)

    # ------------------------------------------------------------------
    # Backfill
    # ------------------------------------------------------------------

    def backfill(self, account_id: str) -> int:
        """
        Fetch the last 20 posts and process any that haven't been seen yet.
        Returns the count of newly processed posts.
        """
        last_post_id = self.get_last_post_id()
        posts = self.fetch_posts(account_id, limit=20)
        new_posts = self.filter_new_posts(posts, last_post_id)
        processed = 0
        for post in new_posts:
            self.process_post(post)
            self.set_last_post_id(post["id"])
            processed += 1
        if processed:
            logger.info("Backfill: processed %d missed posts", processed)
        return processed

    # ------------------------------------------------------------------
    # Backoff
    # ------------------------------------------------------------------

    def get_backoff_delay(self, attempt: int) -> int:
        """Return backoff delay for the given attempt index (0-based)."""
        idx = min(attempt, len(self._backoff_seconds) - 1)
        return self._backoff_seconds[idx]

    # ------------------------------------------------------------------
    # Main polling loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Main polling loop. Blocks forever. Intended to run as a systemd service.
        On errors, applies exponential backoff.
        """
        logger.info("TruthSocialCollector starting up")
        account_id = self.resolve_account_id()
        logger.info("Using account ID: %s", account_id)

        logger.info("Running startup backfill...")
        self.backfill(account_id)

        error_attempt = 0

        while True:
            try:
                posts = self.fetch_posts(account_id)
                if posts:
                    last_post_id = self.get_last_post_id()
                    new_posts = self.filter_new_posts(posts, last_post_id)
                    for post in new_posts:
                        self.process_post(post)
                        self.set_last_post_id(post["id"])
                        logger.info("Processed new post: %s", post.get("id"))
                    error_attempt = 0
                else:
                    error_attempt += 1
                    delay = self.get_backoff_delay(error_attempt - 1)
                    logger.warning(
                        "No posts returned (attempt %d) — waiting %ds before retry",
                        error_attempt,
                        delay,
                    )
                    time.sleep(delay)
                    continue

            except Exception as exc:
                error_attempt += 1
                delay = self.get_backoff_delay(error_attempt - 1)
                logger.error(
                    "Unexpected error in polling loop (attempt %d): %s — retrying in %ds",
                    error_attempt,
                    exc,
                    delay,
                )
                time.sleep(delay)
                continue

            time.sleep(self._poll_interval)
