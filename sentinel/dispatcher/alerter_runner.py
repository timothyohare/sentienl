"""Entry point for the Alert Dispatcher service."""

import logging
import os
import sys
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sentinel.collectors.correlation_detector import CorrelationDetector
from sentinel.core.config import load_config
from sentinel.core.db import Database
from sentinel.dispatcher.alerter import Alerter


def main():
    config_path = os.environ.get("SENTINEL_CONFIG", "config.yaml")
    db_path = os.environ.get("SENTINEL_DB", "sentinel.db")
    config = load_config(config_path)
    db = Database(db_path)
    db.init()

    # Run correlation detector in a background thread, on its own
    # connection: sqlite3 connections aren't safe to share across threads
    # (one thread's rollback/commit lands on the other's transaction).
    detector_db = Database(db_path)
    detector_db.init()
    detector = CorrelationDetector(config=config, db=detector_db)
    detector_thread = threading.Thread(
        target=detector.run, daemon=True, name="correlation-detector"
    )
    detector_thread.start()

    alerter = Alerter(config=config, db=db)
    alerter.run()


if __name__ == "__main__":
    main()
