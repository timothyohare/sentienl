"""Entry point for the Betfair Exchange collector service (plan 06)."""

import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sentinel.collectors.betfair import BetfairCollector
from sentinel.collectors.betfair_client import BetfairClient, load_credentials, read_env_file
from sentinel.core.config import load_config
from sentinel.core.db import Database

logger = logging.getLogger("sentinel.collectors.betfair_runner")


def main() -> int:
    config = load_config(os.environ.get("SENTINEL_CONFIG", "config.yaml"))
    if not config.betfair.enabled:
        logger.error("betfair.enabled is false in config; exiting")
        return 0
    creds = load_credentials(read_env_file(os.environ.get("SENTINEL_ENV", ".env")))
    db = Database(os.environ.get("SENTINEL_DB", "sentinel.db"))
    db.init()
    client = BetfairClient(config.betfair, creds)
    BetfairCollector(config.betfair, db, client).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
