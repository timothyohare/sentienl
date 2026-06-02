"""
core/db.py — SQLite access layer for Sentinel.

Provides a Database class with sub-accessors for each logical table:
  - db.insert_signal() / db.get_unalerted_signals() / db.mark_alerted()
  - db.state — StateStore (key/value persistence)
  - db.wallet_cache — WalletCache (Polygon wallet age cache)
  - db.price_tracking — PostPriceTracking (post-signal price history)

WAL mode and synchronous=NORMAL are set on every connection open.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# DDL — all tables created with IF NOT EXISTS for idempotent init
_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT    NOT NULL,
    signal_type TEXT    NOT NULL,
    priority    TEXT    NOT NULL,
    payload     TEXT    NOT NULL,
    summary     TEXT    NOT NULL,
    alerted     INTEGER DEFAULT 0,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_alerted    ON signals (alerted);
CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals (created_at);
CREATE INDEX IF NOT EXISTS idx_signals_source     ON signals (source);
CREATE INDEX IF NOT EXISTS idx_signals_priority   ON signals (priority);

CREATE TABLE IF NOT EXISTS state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wallet_cache (
    address      TEXT PRIMARY KEY,
    first_tx_date TEXT,
    queried_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS post_price_tracking (
    signal_id   INTEGER REFERENCES signals(id),
    source      TEXT    NOT NULL,
    instrument  TEXT    NOT NULL,
    price_t0    REAL,
    price_t15   REAL,
    price_t60   REAL,
    price_t240  REAL,
    price_t1440 REAL,
    created_at  TEXT    NOT NULL,
    PRIMARY KEY (signal_id, instrument)
);
"""

_VALID_PRICE_COLUMNS = {"price_t0", "price_t15", "price_t60", "price_t240", "price_t1440"}


def _utcnow() -> str:
    """Return current UTC time as ISO8601 string."""
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    """Key-value state persistence backed by the `state` table."""

    def __init__(self, conn_factory):
        self._conn = conn_factory

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Return the value for key, or default if not found."""
        conn = self._conn()
        row = conn.execute(
            "SELECT value FROM state WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        """Insert or update a state key."""
        conn = self._conn()
        conn.execute(
            "INSERT INTO state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, _utcnow()),
        )
        conn.commit()

    def delete(self, key: str) -> None:
        """Remove a state key. No-op if the key does not exist."""
        conn = self._conn()
        conn.execute("DELETE FROM state WHERE key=?", (key,))
        conn.commit()


class WalletCache:
    """Cache for Polygon wallet first-transaction dates."""

    def __init__(self, conn_factory):
        self._conn = conn_factory

    def get(self, address: str) -> Optional[Dict[str, Any]]:
        """Return cached wallet record or None."""
        conn = self._conn()
        row = conn.execute(
            "SELECT address, first_tx_date, queried_at FROM wallet_cache WHERE address=?",
            (address,),
        ).fetchone()
        return dict(row) if row else None

    def set(self, address: str, first_tx_date: Optional[str]) -> None:
        """Insert or update a wallet cache entry."""
        conn = self._conn()
        conn.execute(
            "INSERT INTO wallet_cache (address, first_tx_date, queried_at) VALUES (?, ?, ?) "
            "ON CONFLICT(address) DO UPDATE SET first_tx_date=excluded.first_tx_date, "
            "queried_at=excluded.queried_at",
            (address, first_tx_date, _utcnow()),
        )
        conn.commit()


class PostPriceTracking:
    """Post-signal price history for signal performance evaluation."""

    def __init__(self, conn_factory):
        self._conn = conn_factory

    def insert(
        self,
        signal_id: int,
        source: str,
        instrument: str,
        price_t0: Optional[float] = None,
    ) -> None:
        """Create a new price tracking record for a signal."""
        conn = self._conn()
        conn.execute(
            "INSERT OR IGNORE INTO post_price_tracking "
            "(signal_id, source, instrument, price_t0, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (signal_id, source, instrument, price_t0, _utcnow()),
        )
        conn.commit()

    def update_price(self, signal_id: int, instrument: str, column: str, price: float) -> None:
        """Update a specific price column (price_t15, price_t60, etc.)."""
        if column not in _VALID_PRICE_COLUMNS:
            raise ValueError(f"Invalid price column: {column!r}. Must be one of {_VALID_PRICE_COLUMNS}")
        conn = self._conn()
        conn.execute(
            f"UPDATE post_price_tracking SET {column}=? "
            "WHERE signal_id=? AND instrument=?",
            (price, signal_id, instrument),
        )
        conn.commit()

    def get_pending_updates(self) -> List[Dict[str, Any]]:
        """Return rows where not all price columns are filled in yet."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM post_price_tracking "
            "WHERE price_t15 IS NULL OR price_t60 IS NULL "
            "   OR price_t240 IS NULL OR price_t1440 IS NULL"
        ).fetchall()
        return [dict(r) for r in rows]


class Database:
    """
    Central SQLite access layer.

    Usage:
        db = Database("sentinel.db")
        db.init()
        signal_id = db.insert_signal(...)
        db.mark_alerted(signal_id)
        db.close()
    """

    def __init__(self, path: str):
        self._path = path
        self._conn: Optional[sqlite3.Connection] = None
        # Sub-accessors wired to this instance's connection
        self.state = StateStore(self._get_conn)
        self.wallet_cache = WalletCache(self._get_conn)
        self.price_tracking = PostPriceTracking(self._get_conn)

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialised. Call db.init() first.")
        return self._conn

    def init(self) -> None:
        """Open the database and apply the schema. Safe to call multiple times."""
        if self._conn is None:
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        # Apply schema (idempotent via IF NOT EXISTS)
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()
        logger.info("Database initialised at %s", self._path)

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a SQL statement and return the cursor."""
        conn = self._get_conn()
        return conn.execute(sql, params)

    def execute_fetchall(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """Execute a SELECT and return all rows as dicts."""
        conn = self._get_conn()
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def execute_scalar(self, sql: str, params: tuple = ()) -> Any:
        """Execute a SELECT and return the first column of the first row."""
        conn = self._get_conn()
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # Signal table
    # ------------------------------------------------------------------

    def insert_signal(
        self,
        source: str,
        signal_type: str,
        priority: str,
        payload: Dict[str, Any],
        summary: str,
        alerted: bool = False,
        created_at: Optional[str] = None,
    ) -> int:
        """
        Insert a new signal record. Returns the new row ID.

        Args:
            source:      Collector source identifier.
            signal_type: Type of signal event.
            priority:    CRITICAL | HIGH | MEDIUM | LOW | INFO
            payload:     Arbitrary dict serialised to JSON.
            summary:     Human-readable one-line description.
            alerted:     Whether the alert has already been dispatched.
            created_at:  UTC ISO8601 timestamp; defaults to now.
        """
        conn = self._get_conn()
        payload_json = json.dumps(payload, ensure_ascii=False)
        ts = created_at or _utcnow()
        cursor = conn.execute(
            "INSERT INTO signals (source, signal_type, priority, payload, summary, alerted, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source, signal_type, priority, payload_json, summary, int(alerted), ts),
        )
        conn.commit()
        logger.debug("Inserted signal id=%d source=%s type=%s priority=%s",
                     cursor.lastrowid, source, signal_type, priority)
        return cursor.lastrowid

    def mark_alerted(self, signal_id: int) -> None:
        """Mark a signal as alerted (alerted=1)."""
        conn = self._get_conn()
        conn.execute("UPDATE signals SET alerted=1 WHERE id=?", (signal_id,))
        conn.commit()

    def get_unalerted_signals(
        self, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Return signals where alerted=0, ordered oldest-first."""
        rows = self.execute_fetchall(
            "SELECT * FROM signals WHERE alerted=0 ORDER BY created_at ASC LIMIT ?",
            (limit,),
        )
        # Deserialise payload JSON for convenience
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def get_recent_signals(
        self, limit: int = 20, source: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return recent signals, newest first."""
        if source:
            rows = self.execute_fetchall(
                "SELECT * FROM signals WHERE source=? ORDER BY created_at DESC LIMIT ?",
                (source, limit),
            )
        else:
            rows = self.execute_fetchall(
                "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def get_signals_by_source(
        self, source: str, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Return signals filtered by source, newest first."""
        return self.get_recent_signals(limit=limit, source=source)

    def get_signals_in_range(
        self, start_utc: str, end_utc: str, min_priority: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return signals within a UTC time range, optionally filtered by priority."""
        priority_levels = ["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
        if min_priority:
            if min_priority not in priority_levels:
                raise ValueError(f"Invalid priority: {min_priority!r}")
            idx = priority_levels.index(min_priority)
            allowed = priority_levels[idx:]
            placeholders = ",".join("?" * len(allowed))
            rows = self.execute_fetchall(
                f"SELECT * FROM signals WHERE created_at >= ? AND created_at <= ? "
                f"AND priority IN ({placeholders}) ORDER BY created_at ASC",
                (start_utc, end_utc, *allowed),
            )
        else:
            rows = self.execute_fetchall(
                "SELECT * FROM signals WHERE created_at >= ? AND created_at <= ? "
                "ORDER BY created_at ASC",
                (start_utc, end_utc),
            )
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def delete_signals_older_than_days(self, days: int) -> int:
        """Delete signals older than `days` days. Returns number of rows deleted."""
        conn = self._get_conn()
        cutoff = (
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
        )
        from datetime import timedelta
        cutoff = cutoff - timedelta(days=days)
        cutoff_str = cutoff.isoformat()
        cursor = conn.execute(
            "DELETE FROM signals WHERE created_at < ?", (cutoff_str,)
        )
        conn.commit()
        logger.info("Retention cleanup: deleted %d signals older than %d days",
                    cursor.rowcount, days)
        return cursor.rowcount

    def get_correlated_signals_in_window(self, minutes: int = 10) -> List[Dict[str, Any]]:
        """
        Find HIGH/CRITICAL signals from 2+ distinct sources within the last `minutes` minutes.

        Returns a list of dicts describing the correlated windows found.
        Uses a pure SQL approach: for each recent HIGH/CRITICAL signal, count distinct sources
        within ±minutes/2 of that signal's timestamp.
        """
        conn = self._get_conn()
        sql = """
            SELECT
                s1.id        AS anchor_id,
                s1.created_at AS anchor_time,
                COUNT(DISTINCT s2.source) AS source_count,
                GROUP_CONCAT(DISTINCT s2.source) AS sources
            FROM signals s1
            JOIN signals s2
              ON s2.created_at >= datetime(s1.created_at, '-' || ? || ' minutes')
             AND s2.created_at <= datetime(s1.created_at, '+' || ? || ' minutes')
             AND s2.priority IN ('HIGH', 'CRITICAL')
            WHERE s1.priority IN ('HIGH', 'CRITICAL')
              AND s1.created_at >= datetime('now', '-' || ? || ' minutes')
            GROUP BY s1.id
            HAVING COUNT(DISTINCT s2.source) >= 2
            ORDER BY s1.created_at DESC
        """
        rows = conn.execute(sql, (minutes, minutes, minutes * 2)).fetchall()
        return [dict(r) for r in rows]

    def count_signals_since(
        self, source: str, since_utc: str, signal_type: Optional[str] = None
    ) -> int:
        """Count signals from a source since a given UTC timestamp."""
        if signal_type:
            return self.execute_scalar(
                "SELECT COUNT(*) FROM signals WHERE source=? AND signal_type=? AND created_at>=?",
                (source, signal_type, since_utc),
            ) or 0
        return self.execute_scalar(
            "SELECT COUNT(*) FROM signals WHERE source=? AND created_at>=?",
            (source, since_utc),
        ) or 0


# Convenience type aliases for external imports
Signal = Dict[str, Any]
