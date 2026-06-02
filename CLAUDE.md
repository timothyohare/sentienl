# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
Let's keep to under 200 lines.

## Common commands

```bash
# Virtual environment (required)
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium            # required for Truth Social collector
pip install -r requirements-dev.txt   # adds pytest, coverage

# Database
python sentinel/scripts/init_db.py                          # create/validate sentinel.db
python sentinel/scripts/init_db.py --db-path /custom/path  # custom location

# Tests
pytest                                                       # all tests
pytest tests/unit/test_config.py                            # single file
pytest tests/unit/test_config.py::TestIsInWindow            # single class
pytest --cov=sentinel --cov-report=term-missing             # with coverage

# Run individual components (each in its own terminal)
python -m sentinel.dispatcher.alerter_runner
python -m sentinel.collectors.truth_social_runner
python -m sentinel.collectors.polymarket_runner
python -m sentinel.collectors.kalshi_runner
python -m sentinel.collectors.futures_runner
python -m sentinel.dashboard.app                            # http://127.0.0.1:5000

# Utilities
python sentinel/scripts/test_alert.py    # send a test ntfy notification
python sentinel/scripts/healthcheck.py  # check all collectors are alive
```

Environment variables `SENTINEL_CONFIG` (default `./config.yaml`) and `SENTINEL_DB` (default `./sentinel.db`) override paths for all runners and scripts. The Truth Social runner also reads `SENTINEL_ENV` (default `./.env`) or env vars `TS_USERNAME`/`TS_PASSWORD` for credentials.

## Architecture

Sentinel is a set of independent processes sharing a single SQLite database. There is no message broker or inter-process communication — the database is the only shared state.

### Data flow

```
Collectors ──insert_signal()──► signals table ──poll(alerted=0)──► Alerter ──► ntfy
               │                                                        │
               └──► state table (last seen post ID, etc.)              └──► mark_alerted(id)
```

1. **Collectors** (`sentinel/collectors/`) run as independent loops. Each calls `db.insert_signal()` directly and tracks its own cursor state in `db.state` (key-value table). The Truth Social collector uses a Playwright headless browser (`truth_social_client.py`) to bypass Cloudflare — see section below.
2. **Alerter** (`sentinel/dispatcher/alerter.py`) polls `signals WHERE alerted=0` every 2 seconds, applies quiet-hours / rate-limit logic, sends ntfy HTTP requests, then calls `db.mark_alerted(id)`. Truth Social (`CRITICAL`) signals bypass both rate limiting and quiet hours.
3. **Correlation detector** (`sentinel/collectors/correlation_detector.py`) runs a SQL self-join query every 5 minutes looking for HIGH/CRITICAL events from 2+ distinct sources within any 10-minute window. If found, it inserts a CRITICAL `correlated_signal` — which the alerter then dispatches normally.
4. **Dashboard** (`sentinel/dashboard/app.py`) is a read-only Flask app that queries the signals table directly.

### Core modules

- `sentinel/core/config.py` — `load_config(path)` returns a typed `Config` dataclass. All times are parsed to `datetime.time` UTC. `is_in_window(now_utc, start, end)` handles midnight-crossing windows (e.g. 23:00–04:00).
- `sentinel/core/db.py` — `Database` class with sub-accessors: `db.state` (StateStore), `db.wallet_cache` (WalletCache), `db.price_tracking` (PostPriceTracking). WAL mode is enabled on every `db.init()`. The `payload` column is JSON; `get_unalerted_signals()` and related methods deserialise it automatically.

### Signal priorities

`INFO < LOW < MEDIUM < HIGH < CRITICAL`

Quiet-hours suppression (`quiet_suppress_below` in config) applies to signals below the configured level. Truth Social signals are always `CRITICAL` and are never suppressed.

### Truth Social collector (Playwright)

Cloudflare blocks all direct HTTP requests to truthsocial.com from this machine. The collector uses Playwright headless Chromium to bypass this:
- `truth_social_client.py` — browser client that navigates to the site, logs in via the web UI modal, and makes API calls via in-browser `page.evaluate(fetch(...))`.
- `truth_social.py` — collector logic with a pluggable `TruthSocialClientProtocol`. In production this is the Playwright client; in tests it's a mock.
- Credentials come from `.env` (username/password) or `TS_USERNAME`/`TS_PASSWORD` env vars.
- The bearer token is stored in `localStorage['truth:auth']` after browser login.
- The real OAuth endpoint is `/oauth/v2/token` (not the standard Mastodon `/oauth/token`).

### Kalshi collector specifics

Kalshi (`sentinel/collectors/kalshi.py`) is the primary prediction market data source, replacing Polymarket which is blocked in Australia by ACMA. It polls the public Kalshi REST API (`https://external-api.kalshi.com/trade-api/v2`) — no authentication required for read-only market and trade data.

Signal types: `large_bet` (HIGH), `odds_move` (MEDIUM), `volume_spike` (MEDIUM). No `new_wallet` equivalent (Kalshi is KYC'd). Volume spike baseline is calculated as lifetime volume divided by market age in days. Config section is `kalshi:` with `tracked_event_tickers` as a list of Kalshi event tickers.

### Polymarket collector (blocked in Australia)

Polymarket (`sentinel/collectors/polymarket.py`) was the original prediction market source but is now classified as an illegal online gambling service in Australia under the Interactive Gambling Act 2001 (ACMA block). The collector code remains in the codebase but should not be relied upon from Australian infrastructure. Use the Kalshi collector instead.

### Futures collector specifics

Alpaca is the primary data source (real-time 1-min bars); yfinance is the fallback. Roll-date suppression is configured as a list of dates in `config.yaml` and checked on every poll cycle. Per-instrument `min_absolute_volume` floors prevent false positives on thin overnight sessions.

### Config loading pattern

All runners load config via:
```python
import os
from sentinel.core.config import load_config
cfg = load_config(os.environ.get("SENTINEL_CONFIG", "config.yaml"))
```

Config is loaded once at startup and not reloaded. To apply config changes, restart the relevant process.

### Test patterns

Unit tests in `tests/unit/` use inline YAML fixtures rather than fixture files. See `test_config.py` for the `VALID_CONFIG_YAML` pattern used across test files. Tests do not hit real APIs or the filesystem (except for tempfile-based DB tests). The Truth Social tests mock the client at the protocol boundary (no Playwright needed to run tests).

**Known pre-existing test failures** (not related to recent changes): `test_db.py` (2), `test_correlation_detector.py` (1), `test_futures_volume.py` (2), `test_polymarket.py` (4). All truth social, config, alerter, and dashboard tests pass.

## PDF Processing
When extracting data from PDFs to CSV, read and process PDFs one at a time to minimize token usage. Save intermediate results after each PDF so progress isn't lost if the session is interrupted. Always confirm the output CSV format with the user before processing multiple files.
## Testing & Deployment
Always run the full test suite (`npm test` or equivalent) after multi-file changes before committing. Use TDD approach when adding new features — write tests first, then implement. After successful tests, commit and push unless told otherwise.
## Commit Workflow
After completing a feature or fix, always: 1) run typecheck/lint, 2) run tests, 3) commit with a descriptive message, 4) push to remote. Do not wait to be asked for each step.