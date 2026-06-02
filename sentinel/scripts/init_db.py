#!/usr/bin/env python3
"""
scripts/init_db.py — Sentinel database initialiser.

Creates (or validates) the sentinel.db SQLite database with:
  - WAL journal mode
  - synchronous=NORMAL
  - All required tables with indexes

Safe to run multiple times — uses IF NOT EXISTS throughout.

Usage:
    python scripts/init_db.py [--db-path /path/to/sentinel.db]
"""

import argparse
import logging
import os
import sys

# Allow running directly from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sentinel.core.db import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("sentinel.init_db")


def main():
    parser = argparse.ArgumentParser(description="Initialise the Sentinel SQLite database")
    parser.add_argument(
        "--db-path",
        default="sentinel.db",
        help="Path to the SQLite database file (default: sentinel.db)",
    )
    args = parser.parse_args()

    db_path = args.db_path
    logger.info("Initialising database at: %s", db_path)

    db = Database(db_path)
    db.init()

    # Verify the schema
    tables = {row["name"] for row in db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    expected = {"signals", "state", "wallet_cache", "post_price_tracking"}
    missing = expected - tables
    if missing:
        logger.error("Schema missing tables: %s", missing)
        sys.exit(1)

    wal_mode = db.execute_scalar("PRAGMA journal_mode")
    if wal_mode != "wal":
        logger.warning("WAL mode not confirmed: got %r", wal_mode)
    else:
        logger.info("WAL mode confirmed")

    logger.info("Database initialised successfully. Tables: %s", sorted(tables))
    db.close()
    print(f"OK — database ready at {db_path}")


if __name__ == "__main__":
    main()
