"""
collectors/correlation_detector.py — Multi-source signal correlation detector.

Runs a pure SQL query every 5 minutes against the signals table to find
10-minute windows where HIGH/CRITICAL events fired from 2+ distinct sources.
If found, fires a CRITICAL "CORRELATED SIGNAL" alert via the signals table.

This is the single highest-leverage feature in Sentinel: a single-source alert
has low signal-to-noise; correlated alerts across Truth Social + Polymarket +
Futures are the pattern worth acting on.

The detector is designed to be run in a dedicated thread or process but also
exposes check_and_signal() for testing.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CORRELATION_SIGNAL_DEDUP_KEY = "correlation_last_fired_window"


class CorrelationDetector:
    """
    Polls the signals table every check_interval_seconds and creates a
    CRITICAL correlated_signal if multi-source correlation is found.
    """

    def __init__(
        self,
        config,
        db,
        window_minutes: int = 10,
        check_interval_seconds: int = 300,
    ):
        self.config = config
        self.db = db
        self.window_minutes = window_minutes
        self.check_interval_seconds = check_interval_seconds
        # Track which anchor signal IDs we've already correlated on
        self._fired_on_anchors: set = set()

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    def check_correlation(self) -> bool:
        """
        Query the DB for correlated multi-source HIGH/CRITICAL signals
        within the configured window.

        Returns True if at least one uncorrelated multi-source window was found.
        """
        windows = self.db.get_correlated_signals_in_window(minutes=self.window_minutes)
        if not windows:
            return False
        # Check if any of these are new (not yet correlated)
        for window in windows:
            anchor_id = window.get("anchor_id")
            if anchor_id not in self._fired_on_anchors:
                return True
        return False

    def check_and_signal(self) -> None:
        """
        Check for correlation and write a CRITICAL signal to the DB if found.
        Deduplicates: only fires once per anchor signal.
        """
        windows = self.db.get_correlated_signals_in_window(minutes=self.window_minutes)
        if not windows:
            return

        for window in windows:
            anchor_id = window.get("anchor_id")
            if anchor_id in self._fired_on_anchors:
                continue

            sources = window.get("sources", "multiple")
            source_count = window.get("source_count", 0)
            anchor_time = window.get("anchor_time", "")

            logger.warning(
                "CORRELATED SIGNAL: %d sources (%s) within %d-minute window at %s",
                source_count, sources, self.window_minutes, anchor_time,
            )
            self.db.insert_signal(
                source="correlation_detector",
                signal_type="correlated_signal",
                priority="CRITICAL",
                payload={
                    "sources": sources,
                    "source_count": source_count,
                    "window_minutes": self.window_minutes,
                    "anchor_signal_id": anchor_id,
                    "anchor_time": anchor_time,
                },
                summary=(
                    f"CORRELATED: {sources} within {self.window_minutes} min "
                    f"({source_count} sources)"
                ),
            )
            self._fired_on_anchors.add(anchor_id)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Main loop. Checks for correlation every check_interval_seconds.
        Designed to run as a background thread or dedicated process.
        """
        logger.info(
            "CorrelationDetector starting (window=%dm, interval=%ds)",
            self.window_minutes,
            self.check_interval_seconds,
        )
        while True:
            try:
                self.check_and_signal()
            except Exception as exc:
                logger.error("Correlation check error: %s", exc)
            time.sleep(self.check_interval_seconds)
