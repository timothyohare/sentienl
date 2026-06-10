"""
core/config.py — Configuration loader for Sentinel.

Loads config.yaml, validates all required fields, and returns a typed Config
object. All times are stored as UTC datetime.time objects.

Midnight-crossing window check:
    is_in_window(now, start, end) handles the case where start > end (e.g. 11:00–04:00).
"""

import logging
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

VALID_PRIORITIES = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
VALID_ALERT_PROVIDERS = ("ntfy", "pushover", "telegram")


class ConfigValidationError(ValueError):
    """Raised when a required config field is missing or invalid."""


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------

def is_in_window(now_utc: time, start: time, end: time) -> bool:
    """
    Return True if now_utc falls within [start, end].

    Handles midnight-crossing windows (start > end) correctly.
    For example: start=11:00, end=04:00 covers 11:00 through 23:59 and 00:00 through 04:00.
    """
    if start <= end:
        return start <= now_utc <= end
    else:
        # Crosses midnight
        return now_utc >= start or now_utc <= end


# ---------------------------------------------------------------------------
# Dataclasses — one per config section
# ---------------------------------------------------------------------------

@dataclass
class TruthSocialConfig:
    account_handle: str
    account_id_fallback: str
    poll_interval_seconds: int
    alert_all_posts: bool
    keyword_filter: List[str]
    backoff_seconds: List[int]
    critical_keywords: List[str] = field(default_factory=list)
    endorsement_markers: List[str] = field(default_factory=list)
    default_priority: str = "MEDIUM"


@dataclass
class KalshiThresholds:
    large_bet_contracts: float
    odds_move_pct_5min: float
    volume_spike_multiplier: float
    min_absolute_volume: float


@dataclass
class KalshiConfig:
    poll_interval_seconds: int
    api_base_url: str
    tracked_event_tickers: List[str]
    thresholds: KalshiThresholds


@dataclass
class PolymarketThresholds:
    large_bet_usd: float
    new_wallet_age_days: int
    new_wallet_min_bet_usd: float
    odds_move_pct_5min: float
    volume_spike_multiplier: float
    min_absolute_volume_usd: float


@dataclass
class PolymarketConfig:
    poll_interval_seconds: int
    gamma_api_url: str
    polygonscan_api_key: str
    tracked_markets: List[str]
    thresholds: PolymarketThresholds


@dataclass
class FuturesInstrument:
    ticker: str
    name: str
    min_absolute_volume: int


@dataclass
class FuturesThresholds:
    spike_multiplier: float
    spike_multiplier_quiet: float
    rolling_bars: int


@dataclass
class TimeWindow:
    start: time
    end: time


@dataclass
class RollDate:
    date: str
    tickers: List[str]
    note: str


@dataclass
class FuturesConfig:
    poll_interval_seconds: int
    alpaca_api_key: str
    alpaca_api_secret: str
    alpaca_base_url: str
    instruments: List[FuturesInstrument]
    thresholds: FuturesThresholds
    active_window_utc: TimeWindow
    suppress_volume_alerts_on_roll_dates: bool
    roll_dates: List[RollDate]


@dataclass
class AlertsConfig:
    provider: str
    ntfy_topic: str
    ntfy_url: str
    rate_limit_minutes: int
    quiet_hours_utc: TimeWindow
    quiet_suppress_below: str
    digest_time_utc: time


@dataclass
class DatabaseConfig:
    path: str
    retention_days: int


@dataclass
class DashboardConfig:
    host: str
    port: int


@dataclass
class Config:
    truth_social: TruthSocialConfig
    polymarket: PolymarketConfig
    kalshi: KalshiConfig
    futures: FuturesConfig
    alerts: AlertsConfig
    database: DatabaseConfig
    dashboard: DashboardConfig


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _require(data: Dict, key: str, section: str) -> Any:
    """Raise ConfigValidationError if key is missing from data."""
    if key not in data:
        raise ConfigValidationError(
            f"Missing required config field '{key}' in section '{section}'"
        )
    return data[key]


def _parse_time(value: str, field_name: str) -> time:
    """Parse an 'HH:MM' string into a datetime.time object."""
    try:
        parts = value.strip().split(":")
        if len(parts) != 2:
            raise ValueError
        return time(int(parts[0]), int(parts[1]))
    except (ValueError, AttributeError):
        raise ConfigValidationError(
            f"Invalid time format for '{field_name}': {value!r} — expected 'HH:MM'"
        )


def _parse_time_window(data: Dict, section: str) -> TimeWindow:
    start_str = _require(data, "start", section)
    end_str = _require(data, "end", section)
    return TimeWindow(
        start=_parse_time(start_str, f"{section}.start"),
        end=_parse_time(end_str, f"{section}.end"),
    )


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------

def _parse_truth_social(data: Dict) -> TruthSocialConfig:
    sec = "truth_social"
    handle = _require(data, "account_handle", sec)
    if not handle:
        raise ConfigValidationError(f"'{sec}.account_handle' must not be empty")
    poll = _require(data, "poll_interval_seconds", sec)
    if not isinstance(poll, int) or poll < 1:
        raise ConfigValidationError(
            f"'{sec}.poll_interval_seconds' must be a positive integer, got {poll!r}"
        )
    default_priority = data.get("default_priority", "MEDIUM")
    if default_priority not in VALID_PRIORITIES:
        raise ConfigValidationError(
            f"'{sec}.default_priority' must be one of {VALID_PRIORITIES}, "
            f"got {default_priority!r}"
        )
    return TruthSocialConfig(
        account_handle=handle,
        account_id_fallback=str(data.get("account_id_fallback", "107780257626128497")),
        poll_interval_seconds=poll,
        alert_all_posts=bool(data.get("alert_all_posts", True)),
        keyword_filter=list(data.get("keyword_filter", [])),
        backoff_seconds=list(data.get("backoff_seconds", [30, 60, 120, 300])),
        critical_keywords=list(data.get("critical_keywords", [])),
        endorsement_markers=list(data.get("endorsement_markers", ["endorse", "endorsement"])),
        default_priority=default_priority,
    )


def _parse_kalshi(data: Dict) -> KalshiConfig:
    thresholds_raw = data.get("thresholds", {})
    thresholds = KalshiThresholds(
        large_bet_contracts=float(thresholds_raw.get("large_bet_contracts", 100)),
        odds_move_pct_5min=float(thresholds_raw.get("odds_move_pct_5min", 5.0)),
        volume_spike_multiplier=float(thresholds_raw.get("volume_spike_multiplier", 3.0)),
        min_absolute_volume=float(thresholds_raw.get("min_absolute_volume", 50)),
    )
    return KalshiConfig(
        poll_interval_seconds=int(data.get("poll_interval_seconds", 30)),
        api_base_url=str(data.get("api_base_url", "https://external-api.kalshi.com/trade-api/v2")),
        tracked_event_tickers=list(data.get("tracked_event_tickers", [])),
        thresholds=thresholds,
    )


def _parse_polymarket(data: Dict) -> PolymarketConfig:
    sec = "polymarket"
    thresholds_raw = data.get("thresholds", {})
    thresholds = PolymarketThresholds(
        large_bet_usd=float(thresholds_raw.get("large_bet_usd", 5000)),
        new_wallet_age_days=int(thresholds_raw.get("new_wallet_age_days", 7)),
        new_wallet_min_bet_usd=float(thresholds_raw.get("new_wallet_min_bet_usd", 1000)),
        odds_move_pct_5min=float(thresholds_raw.get("odds_move_pct_5min", 5.0)),
        volume_spike_multiplier=float(thresholds_raw.get("volume_spike_multiplier", 3.0)),
        min_absolute_volume_usd=float(thresholds_raw.get("min_absolute_volume_usd", 500)),
    )
    return PolymarketConfig(
        poll_interval_seconds=int(data.get("poll_interval_seconds", 30)),
        gamma_api_url=str(data.get("gamma_api_url", "https://gamma-api.polymarket.com")),
        polygonscan_api_key=str(data.get("polygonscan_api_key", "")),
        tracked_markets=list(data.get("tracked_markets", [])),
        thresholds=thresholds,
    )


def _parse_futures(data: Dict) -> FuturesConfig:
    sec = "futures"
    instruments_raw = data.get("instruments", [])
    if not instruments_raw:
        raise ConfigValidationError(
            f"'{sec}.instruments' must contain at least one instrument"
        )
    instruments = [
        FuturesInstrument(
            ticker=_require(inst, "ticker", f"{sec}.instruments[]"),
            name=inst.get("name", inst.get("ticker", "")),
            min_absolute_volume=int(inst.get("min_absolute_volume", 0)),
        )
        for inst in instruments_raw
    ]
    thresholds_raw = data.get("thresholds", {})
    thresholds = FuturesThresholds(
        spike_multiplier=float(thresholds_raw.get("spike_multiplier", 3.0)),
        spike_multiplier_quiet=float(thresholds_raw.get("spike_multiplier_quiet", 5.0)),
        rolling_bars=int(thresholds_raw.get("rolling_bars", 20)),
    )
    window_raw = data.get("active_window_utc", {"start": "11:00", "end": "04:00"})
    active_window = _parse_time_window(window_raw, f"{sec}.active_window_utc")
    roll_dates_raw = data.get("roll_dates", [])
    roll_dates = [
        RollDate(
            date=str(rd["date"]),
            tickers=list(rd.get("tickers", [])),
            note=str(rd.get("note", "")),
        )
        for rd in roll_dates_raw
    ]
    return FuturesConfig(
        poll_interval_seconds=int(data.get("poll_interval_seconds", 60)),
        alpaca_api_key=str(data.get("alpaca_api_key", "")),
        alpaca_api_secret=str(data.get("alpaca_api_secret", "")),
        alpaca_base_url=str(data.get("alpaca_base_url", "https://data.alpaca.markets")),
        instruments=instruments,
        thresholds=thresholds,
        active_window_utc=active_window,
        suppress_volume_alerts_on_roll_dates=bool(
            data.get("suppress_volume_alerts_on_roll_dates", True)
        ),
        roll_dates=roll_dates,
    )


def _parse_alerts(data: Dict) -> AlertsConfig:
    sec = "alerts"
    ntfy_topic = data.get("ntfy_topic")
    if not ntfy_topic:
        raise ConfigValidationError(
            f"'{sec}.ntfy_topic' is required and must not be empty"
        )
    quiet_suppress = data.get("quiet_suppress_below", "MEDIUM")
    if quiet_suppress not in VALID_PRIORITIES:
        raise ConfigValidationError(
            f"'{sec}.quiet_suppress_below' must be one of {VALID_PRIORITIES}, "
            f"got {quiet_suppress!r}"
        )
    quiet_raw = data.get("quiet_hours_utc", {"start": "17:00", "end": "21:00"})
    quiet_window = _parse_time_window(quiet_raw, f"{sec}.quiet_hours_utc")
    digest_str = data.get("digest_time_utc", "21:00")
    digest_time = _parse_time(digest_str, f"{sec}.digest_time_utc")
    provider = data.get("provider", "ntfy")
    return AlertsConfig(
        provider=provider,
        ntfy_topic=ntfy_topic,
        ntfy_url=str(data.get("ntfy_url", "https://ntfy.sh")),
        rate_limit_minutes=int(data.get("rate_limit_minutes", 5)),
        quiet_hours_utc=quiet_window,
        quiet_suppress_below=quiet_suppress,
        digest_time_utc=digest_time,
    )


def _parse_database(data: Dict) -> DatabaseConfig:
    sec = "database"
    retention = int(data.get("retention_days", 90))
    if retention < 1:
        raise ConfigValidationError(
            f"'{sec}.retention_days' must be a positive integer, got {retention!r}"
        )
    return DatabaseConfig(
        path=str(data.get("path", "./sentinel.db")),
        retention_days=retention,
    )


def _parse_dashboard(data: Dict) -> DashboardConfig:
    return DashboardConfig(
        host=str(data.get("host", "127.0.0.1")),
        port=int(data.get("port", 5000)),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def load_config(path: str) -> Config:
    """
    Load and validate config.yaml from the given path.

    Raises:
        FileNotFoundError: if the file does not exist.
        ConfigValidationError: if a required field is missing or invalid.
        yaml.YAMLError: if the file is not valid YAML.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with config_path.open("r") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ConfigValidationError("config.yaml must be a YAML mapping at the top level")

    cfg = Config(
        truth_social=_parse_truth_social(raw.get("truth_social", {})),
        polymarket=_parse_polymarket(raw.get("polymarket", {})),
        kalshi=_parse_kalshi(raw.get("kalshi", {})),
        futures=_parse_futures(raw.get("futures", {})),
        alerts=_parse_alerts(raw.get("alerts", {})),
        database=_parse_database(raw.get("database", {})),
        dashboard=_parse_dashboard(raw.get("dashboard", {})),
    )
    logger.info("Config loaded from %s", path)
    return cfg
