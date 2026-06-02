#!/usr/bin/env python3
"""
scripts/test_alert.py — Send a test push notification via ntfy.

Verifies that:
  1. config.yaml is loadable
  2. ntfy_url and ntfy_topic are configured
  3. ntfy accepts a POST request
  4. Notification arrives on your phone within 15 seconds

Usage:
    python sentinel/scripts/test_alert.py --config config.yaml
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sentinel.core.config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("sentinel.test_alert")


def main():
    parser = argparse.ArgumentParser(description="Send a test ntfy alert")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--message", default="Sentinel test alert — if you see this, ntfy is working!")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        logger.error("Config file not found: %s", args.config)
        sys.exit(1)

    try:
        config = load_config(args.config)
    except Exception as exc:
        logger.error("Failed to load config: %s", exc)
        sys.exit(1)

    ntfy_url = config.alerts.ntfy_url
    ntfy_topic = config.alerts.ntfy_topic

    if not ntfy_topic or ntfy_topic == "sentinel-CHANGEME":
        logger.error(
            "ntfy_topic is not configured. Edit config.yaml and set alerts.ntfy_topic "
            "to your private ntfy topic name."
        )
        sys.exit(1)

    url = f"{ntfy_url}/{ntfy_topic}"
    logger.info("Sending test alert to %s", url)

    try:
        import requests
        resp = requests.post(
            url,
            data=args.message.encode("utf-8"),
            headers={
                "Priority": "3",
                "Tags": "white_check_mark",
                "Title": "Sentinel Test Alert",
                "Content-Type": "text/plain",
            },
            timeout=15,
        )
        if resp.status_code < 300:
            logger.info("Test alert sent successfully (HTTP %d)", resp.status_code)
            print(f"\nOK — Test alert sent to ntfy topic '{ntfy_topic}'")
            print("Check your phone for the notification within 15 seconds.")
        else:
            logger.error("ntfy returned HTTP %d: %s", resp.status_code, resp.text)
            sys.exit(1)
    except Exception as exc:
        logger.error("Failed to send test alert: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
