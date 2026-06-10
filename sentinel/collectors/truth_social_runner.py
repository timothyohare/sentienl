"""Entry point for the Truth Social collector service."""

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
from sentinel.collectors.truth_social import TruthSocialCollector
from sentinel.collectors.truth_social_client import TruthSocialClient


def _load_env(path: str) -> dict:
    """Read a simple KEY=VALUE .env file. Lines without '=' are skipped."""
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, val = line.split("=", 1)
                    env[key.strip()] = val.strip()
    except FileNotFoundError:
        pass
    return env


def main():
    config_path = os.environ.get("SENTINEL_CONFIG", "config.yaml")
    db_path = os.environ.get("SENTINEL_DB", "sentinel.db")
    env_path = os.environ.get("SENTINEL_ENV", ".env")

    config = load_config(config_path)
    db = Database(db_path)
    db.init()

    # Load Truth Social credentials from .env or environment variables
    env = _load_env(env_path)
    ts_username = os.environ.get("TS_USERNAME") or env.get("username", "")
    ts_password = os.environ.get("TS_PASSWORD") or env.get("password", "")

    if not ts_username or not ts_password:
        logging.error(
            "Truth Social credentials not found. "
            "Set TS_USERNAME/TS_PASSWORD env vars or add username/password to %s",
            env_path,
        )
        sys.exit(1)

    client = TruthSocialClient(username=ts_username, password=ts_password)
    client.start()

    try:
        collector = TruthSocialCollector(config=config, db=db, client=client)
        collector.run()
    finally:
        client.stop()


if __name__ == "__main__":
    main()
