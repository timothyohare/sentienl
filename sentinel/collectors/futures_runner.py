"""Entry point for the Futures Volume collector service."""

import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sentinel.core.config import load_config
from sentinel.core.db import Database
from sentinel.collectors.futures_volume import FuturesVolumeCollector


def main():
    config_path = os.environ.get("SENTINEL_CONFIG", "config.yaml")
    db_path = os.environ.get("SENTINEL_DB", "sentinel.db")
    config = load_config(config_path)
    db = Database(db_path)
    db.init()
    collector = FuturesVolumeCollector(config=config, db=db)
    collector.run()


if __name__ == "__main__":
    main()
