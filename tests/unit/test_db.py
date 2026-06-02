"""Unit tests for core/db.py — SQLite access layer."""

import json
import sqlite3
import tempfile
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sentinel.core.db import Database, Signal, StateStore, WalletCache, PostPriceTracking


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary database for each test."""
    db_path = str(tmp_path / "test_sentinel.db")
    db = Database(db_path)
    db.init()
    yield db
    db.close()


class TestDatabaseInit:
    def test_init_creates_file(self, tmp_path):
        db_path = str(tmp_path / "new_sentinel.db")
        db = Database(db_path)
        db.init()
        assert os.path.exists(db_path)
        db.close()

    def test_init_idempotent(self, tmp_db):
        """Calling init twice does not raise or corrupt the DB."""
        tmp_db.init()  # second call — should be a no-op

    def test_wal_mode_enabled(self, tmp_db):
        result = tmp_db.execute_scalar("PRAGMA journal_mode")
        assert result == "wal"

    def test_synchronous_normal(self, tmp_db):
        result = tmp_db.execute_scalar("PRAGMA synchronous")
        # 1 = NORMAL
        assert result == 1

    def test_all_tables_created(self, tmp_db):
        tables = {row[0] for row in tmp_db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "signals" in tables
        assert "state" in tables
        assert "wallet_cache" in tables
        assert "post_price_tracking" in tables


class TestSignalInsert:
    def test_insert_signal_returns_id(self, tmp_db):
        signal_id = tmp_db.insert_signal(
            source="truth_social",
            signal_type="new_post",
            priority="CRITICAL",
            payload={"post_id": "123", "text": "Hello world"},
            summary="New Trump post: Hello world",
        )
        assert isinstance(signal_id, int)
        assert signal_id > 0

    def test_insert_signal_persisted(self, tmp_db):
        signal_id = tmp_db.insert_signal(
            source="polymarket",
            signal_type="large_bet",
            priority="HIGH",
            payload={"amount_usd": 7500, "market": "us-iran"},
            summary="Large bet $7500 on us-iran",
        )
        rows = tmp_db.execute_fetchall("SELECT * FROM signals WHERE id=?", (signal_id,))
        assert len(rows) == 1
        row = rows[0]
        assert row["source"] == "polymarket"
        assert row["signal_type"] == "large_bet"
        assert row["priority"] == "HIGH"
        assert row["alerted"] == 0
        payload = json.loads(row["payload"])
        assert payload["amount_usd"] == 7500

    def test_insert_signal_created_at_utc(self, tmp_db):
        signal_id = tmp_db.insert_signal(
            source="futures_oil",
            signal_type="volume_spike",
            priority="HIGH",
            payload={},
            summary="Volume spike WTI",
        )
        rows = tmp_db.execute_fetchall("SELECT created_at FROM signals WHERE id=?", (signal_id,))
        created_at_str = rows[0]["created_at"]
        # Should be parseable as UTC ISO8601
        dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        assert dt.tzinfo is not None or "T" in created_at_str

    def test_insert_signal_accepts_all_valid_sources(self, tmp_db):
        sources = [
            "truth_social", "polymarket", "futures_oil", "futures_sp500",
            "futures_brent", "futures_natgas", "futures_gold", "futures_dxy",
        ]
        for source in sources:
            sid = tmp_db.insert_signal(
                source=source,
                signal_type="volume_spike",
                priority="INFO",
                payload={},
                summary=f"Test signal from {source}",
            )
            assert sid > 0

    def test_insert_signal_payload_is_json(self, tmp_db):
        payload_dict = {"key": "value", "num": 42, "nested": {"a": 1}}
        signal_id = tmp_db.insert_signal(
            source="truth_social",
            signal_type="new_post",
            priority="CRITICAL",
            payload=payload_dict,
            summary="Test",
        )
        rows = tmp_db.execute_fetchall("SELECT payload FROM signals WHERE id=?", (signal_id,))
        loaded = json.loads(rows[0]["payload"])
        assert loaded == payload_dict


class TestSignalQuery:
    def test_get_unalerted_signals_empty(self, tmp_db):
        signals = tmp_db.get_unalerted_signals()
        assert signals == []

    def test_get_unalerted_signals_returns_new(self, tmp_db):
        tmp_db.insert_signal("truth_social", "new_post", "CRITICAL", {}, "Post 1")
        tmp_db.insert_signal("polymarket", "large_bet", "HIGH", {}, "Big bet")
        signals = tmp_db.get_unalerted_signals()
        assert len(signals) == 2

    def test_get_unalerted_signals_excludes_alerted(self, tmp_db):
        sid = tmp_db.insert_signal("truth_social", "new_post", "CRITICAL", {}, "Post 1")
        tmp_db.mark_alerted(sid)
        signals = tmp_db.get_unalerted_signals()
        assert signals == []

    def test_get_recent_signals(self, tmp_db):
        for i in range(5):
            tmp_db.insert_signal("truth_social", "new_post", "CRITICAL", {}, f"Post {i}")
        signals = tmp_db.get_recent_signals(limit=3)
        assert len(signals) == 3

    def test_get_recent_signals_ordered_newest_first(self, tmp_db):
        for i in range(3):
            tmp_db.insert_signal("truth_social", "new_post", "CRITICAL", {}, f"Post {i}")
        signals = tmp_db.get_recent_signals(limit=3)
        ids = [s["id"] for s in signals]
        assert ids == sorted(ids, reverse=True)

    def test_get_signals_by_source(self, tmp_db):
        tmp_db.insert_signal("truth_social", "new_post", "CRITICAL", {}, "Post A")
        tmp_db.insert_signal("polymarket", "large_bet", "HIGH", {}, "Bet B")
        ts_signals = tmp_db.get_signals_by_source("truth_social")
        assert len(ts_signals) == 1
        assert ts_signals[0]["source"] == "truth_social"


class TestMarkAlerted:
    def test_mark_alerted_sets_flag(self, tmp_db):
        sid = tmp_db.insert_signal("truth_social", "new_post", "CRITICAL", {}, "Post")
        tmp_db.mark_alerted(sid)
        rows = tmp_db.execute_fetchall("SELECT alerted FROM signals WHERE id=?", (sid,))
        assert rows[0]["alerted"] == 1

    def test_mark_alerted_nonexistent_id_does_not_raise(self, tmp_db):
        tmp_db.mark_alerted(99999)  # should not raise


class TestStateStore:
    def test_get_state_missing_returns_none(self, tmp_db):
        value = tmp_db.state.get("nonexistent_key")
        assert value is None

    def test_get_state_missing_returns_default(self, tmp_db):
        value = tmp_db.state.get("nonexistent_key", default="fallback")
        assert value == "fallback"

    def test_set_and_get_state(self, tmp_db):
        tmp_db.state.set("truth_social_last_post_id", "12345")
        value = tmp_db.state.get("truth_social_last_post_id")
        assert value == "12345"

    def test_set_state_updates_existing(self, tmp_db):
        tmp_db.state.set("mykey", "first")
        tmp_db.state.set("mykey", "second")
        value = tmp_db.state.get("mykey")
        assert value == "second"

    def test_set_state_updates_updated_at(self, tmp_db):
        tmp_db.state.set("mykey", "value")
        rows = tmp_db.execute_fetchall("SELECT updated_at FROM state WHERE key='mykey'")
        assert len(rows) == 1
        assert rows[0]["updated_at"]  # non-empty timestamp

    def test_delete_state(self, tmp_db):
        tmp_db.state.set("mykey", "value")
        tmp_db.state.delete("mykey")
        assert tmp_db.state.get("mykey") is None

    def test_delete_nonexistent_does_not_raise(self, tmp_db):
        tmp_db.state.delete("does_not_exist")


class TestWalletCache:
    def test_get_wallet_missing_returns_none(self, tmp_db):
        result = tmp_db.wallet_cache.get("0xdeadbeef")
        assert result is None

    def test_set_and_get_wallet(self, tmp_db):
        address = "0xabc123"
        first_tx = "2026-01-15T00:00:00+00:00"
        tmp_db.wallet_cache.set(address, first_tx)
        result = tmp_db.wallet_cache.get(address)
        assert result is not None
        assert result["first_tx_date"] == first_tx
        assert result["address"] == address

    def test_set_wallet_with_none_first_tx(self, tmp_db):
        address = "0xunknown"
        tmp_db.wallet_cache.set(address, None)
        result = tmp_db.wallet_cache.get(address)
        assert result is not None
        assert result["first_tx_date"] is None

    def test_wallet_cache_upsert(self, tmp_db):
        address = "0xabc"
        tmp_db.wallet_cache.set(address, "2026-01-01T00:00:00+00:00")
        tmp_db.wallet_cache.set(address, "2026-02-01T00:00:00+00:00")
        result = tmp_db.wallet_cache.get(address)
        assert result["first_tx_date"] == "2026-02-01T00:00:00+00:00"


class TestPostPriceTracking:
    def test_insert_price_tracking(self, tmp_db):
        sid = tmp_db.insert_signal("truth_social", "new_post", "CRITICAL", {}, "Post")
        tmp_db.price_tracking.insert(
            signal_id=sid,
            source="truth_social",
            instrument="CL=F",
            price_t0=75.50,
        )
        rows = tmp_db.execute_fetchall(
            "SELECT * FROM post_price_tracking WHERE signal_id=?", (sid,)
        )
        assert len(rows) == 1
        assert rows[0]["price_t0"] == 75.50
        assert rows[0]["price_t15"] is None

    def test_update_price_tracking(self, tmp_db):
        sid = tmp_db.insert_signal("truth_social", "new_post", "CRITICAL", {}, "Post")
        tmp_db.price_tracking.insert(sid, "truth_social", "CL=F", price_t0=75.50)
        tmp_db.price_tracking.update_price(sid, "CL=F", "price_t15", 76.10)
        rows = tmp_db.execute_fetchall(
            "SELECT price_t15 FROM post_price_tracking WHERE signal_id=?", (sid,)
        )
        assert rows[0]["price_t15"] == 76.10

    def test_get_pending_price_updates(self, tmp_db):
        sid = tmp_db.insert_signal("truth_social", "new_post", "CRITICAL", {}, "Post")
        tmp_db.price_tracking.insert(sid, "truth_social", "CL=F", price_t0=75.50)
        pending = tmp_db.price_tracking.get_pending_updates()
        assert any(r["signal_id"] == sid for r in pending)


class TestRetentionCleanup:
    def test_delete_old_signals(self, tmp_db):
        # Insert a signal with old created_at
        tmp_db.execute(
            "INSERT INTO signals (source, signal_type, priority, payload, summary, alerted, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("truth_social", "new_post", "CRITICAL", "{}", "Old post", 1,
             "2020-01-01T00:00:00+00:00"),
        )
        tmp_db.execute(
            "INSERT INTO signals (source, signal_type, priority, payload, summary, alerted, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("truth_social", "new_post", "CRITICAL", "{}", "New post", 0,
             datetime.now(timezone.utc).isoformat()),
        )
        tmp_db.delete_signals_older_than_days(30)
        rows = tmp_db.execute_fetchall("SELECT summary FROM signals")
        summaries = [r["summary"] for r in rows]
        assert "Old post" not in summaries
        assert "New post" in summaries


class TestCorrelationQuery:
    def test_get_correlated_signals_empty(self, tmp_db):
        result = tmp_db.get_correlated_signals_in_window(minutes=10)
        assert result == []

    def test_get_correlated_signals_single_source_not_correlated(self, tmp_db):
        now = datetime.now(timezone.utc).isoformat()
        tmp_db.execute(
            "INSERT INTO signals (source, signal_type, priority, payload, summary, alerted, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("truth_social", "new_post", "HIGH", "{}", "Post 1", 0, now),
        )
        tmp_db.execute(
            "INSERT INTO signals (source, signal_type, priority, payload, summary, alerted, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("truth_social", "new_post", "HIGH", "{}", "Post 2", 0, now),
        )
        result = tmp_db.get_correlated_signals_in_window(minutes=10)
        assert result == []

    def test_get_correlated_signals_multi_source_detected(self, tmp_db):
        now = datetime.now(timezone.utc).isoformat()
        tmp_db.execute(
            "INSERT INTO signals (source, signal_type, priority, payload, summary, alerted, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("truth_social", "new_post", "HIGH", "{}", "Post", 0, now),
        )
        tmp_db.execute(
            "INSERT INTO signals (source, signal_type, priority, payload, summary, alerted, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("futures_oil", "volume_spike", "HIGH", "{}", "Spike", 0, now),
        )
        result = tmp_db.get_correlated_signals_in_window(minutes=10)
        assert len(result) > 0
