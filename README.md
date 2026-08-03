# Sentinel

Geopolitical signal monitoring system. Watches Truth Social posts, Kalshi prediction market activity, and futures volume for anomalies, then sends push notifications via ntfy.

Polymarket was the original prediction-market source but is classified as an illegal online gambling service in Australia under the Interactive Gambling Act 2001 (ACMA-blocked). The collector code remains in the repo but is not used from Australian infrastructure — Kalshi replaces it.

## Requirements

- Python 3.11+
- ntfy account (free at ntfy.sh, or self-hosted)
- Optional: Alpaca Markets free API key (Alpaca does **not** support futures — kept only for a potential stock/ETF pivot; yfinance is the actual futures data source)

## Setup

### 1. Clone and create a virtual environment

```bash
cd /home/timohare/dev/newdev/Sentinel
python3 -m venv venv
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium   # required for Truth Social collector
```

For development (adds pytest, coverage, ruff, mypy, mutmut):

```bash
pip install -r requirements-dev.txt
```

### 3. Configure

```bash
cp config.yaml.example config.yaml
chmod 600 config.yaml   # ntfy topic is a secret — restrict permissions
```

Edit `config.yaml` and fill in:
- `alerts.ntfy_topic` — your private ntfy topic name
- `kalshi.tracked_event_tickers` — Kalshi event tickers to monitor (no API key needed; public read-only endpoints)
- `futures.alpaca_api_key` / `alpaca_api_secret` — optional, Alpaca doesn't support futures so this is inert unless you pivot to stocks/ETFs
- `polymarket.*` — present for completeness only; not used from Australian infrastructure

### 4. Set up Truth Social credentials

The Truth Social collector requires a registered account. Create a `.env` file in the project root:

```bash
# .env — Truth Social credentials (this file is in .gitignore)
username=your_truthsocial_username
password=your_truthsocial_password
```

Alternatively, set environment variables `TS_USERNAME` and `TS_PASSWORD`.

**Why Playwright?** Cloudflare blocks direct HTTP requests (httpx, curl) to
truthsocial.com. The collector uses a headless Chromium browser to navigate
the site (which passes Cloudflare's JS challenge), logs in via the web UI,
then makes API calls using in-browser `fetch()`. The browser session stays
alive for the lifetime of the collector process.

**How it works:**
1. Playwright launches headless Chromium and navigates to `truthsocial.com`
2. The Cloudflare JS challenge is solved automatically by the real browser engine
3. The collector clicks "Sign In", fills the login modal, and submits
4. A bearer token is extracted from `localStorage` after successful login
5. All subsequent API calls (`/api/v1/accounts/:id/statuses`) run via
   `page.evaluate(fetch(...))` inside the browser context
6. The polling loop runs normally — the browser session is reused across polls

### 5. Initialise the database

```bash
python sentinel/scripts/init_db.py
# Or specify a custom path:
python sentinel/scripts/init_db.py --db-path /path/to/sentinel.db
```

### 6. Test the alert pipeline

Sends a test notification to your ntfy topic to confirm delivery works before starting real collectors:

```bash
python sentinel/scripts/test_alert.py
```

---

## Running

All components expect the virtual environment to be active (`source venv/bin/activate`) or use the venv Python directly.

### Run components individually (development / testing)

Each component is a standalone process. Run each in its own terminal:

```bash
# Alert dispatcher (reads signals from DB, sends ntfy notifications; also runs the correlation detector)
python -m sentinel.dispatcher.alerter_runner

# Truth Social collector
python -m sentinel.collectors.truth_social_runner

# Kalshi collector (primary prediction-market source)
python -m sentinel.collectors.kalshi_runner

# Futures volume collector
python -m sentinel.collectors.futures_runner

# Price follow-through backfill (fills in post-signal price history)
python sentinel/scripts/price_followup.py

# Dashboard (http://127.0.0.1:5000)
python -m sentinel.dashboard.app
```

`polymarket_runner` also exists but shouldn't be run from Australian infrastructure — see above.

### Run with systemd (production)

Service files live in `deploy/systemd/` — `systemd --user` units (no root required), covering the alerter, Truth Social, Kalshi, futures, and price-follow-through. Full install/operate instructions are in `deploy/systemd/README.md`; short version:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/*.service deploy/systemd/*.target ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sentinel.target
loginctl enable-linger $USER   # so it survives logout/reboot
```

Check status:

```bash
systemctl --user status sentinel.target
journalctl --user -u sentinel-kalshi -f
```

---

## Health check

```bash
python sentinel/scripts/healthcheck.py
```

Checks that each monitored source has written a signal recently; exits non-zero if any collector looks stale. `systemd`'s `Restart=always` already recovers from a crashed process, but not from a collector that's alive yet silently stuck — `healthcheck.py` catches that case. It supports an ntfy heartbeat mode for cron:

```bash
crontab -e
# Add:
7 * * * * /home/timohare/dev/newdev/Sentinel/venv/bin/python /home/timohare/dev/newdev/Sentinel/sentinel/scripts/healthcheck.py --config /home/timohare/dev/newdev/Sentinel/config.yaml --db /home/timohare/dev/newdev/Sentinel/sentinel.db --heartbeat
```

---

## Signal-quality diagnostics

```bash
python sentinel/scripts/signal_diagnostics.py     # cheap proxies: burst detection, correlation confirmation rate
python sentinel/scripts/signal_scorecard.py       # real event-study effect size vs. baseline, from price follow-through data
```

`signal_scorecard.py` only has something to report once `price_followup.py` has been running long enough to backfill the `t1440` (24h) horizon for a reasonable sample of signals.

---

## Dashboard

Navigate to `http://127.0.0.1:5000` for the signal feed and `http://127.0.0.1:5000/health` for system status.

The dashboard binds to `127.0.0.1` by default — it is not exposed to the network and has no authentication. Do not change the host to `0.0.0.0` without adding auth.

---

## Tests

```bash
source venv/bin/activate
pytest
```

Run with coverage:

```bash
pytest --cov=sentinel --cov-report=term-missing
```

Quality gates (lint, typecheck, tests, mutation score) are wired through the shared harness — see `CLAUDE.md`.

---

## Environment variables

The service files use environment variables for paths. You can also set these when running manually:

| Variable | Default | Description |
|---|---|---|
| `SENTINEL_CONFIG` | `./config.yaml` | Path to config file |
| `SENTINEL_DB` | `./sentinel.db` | Path to SQLite database |
| `SENTINEL_ENV` | `./.env` | Path to `.env` file (Truth Social credentials) |
| `TS_USERNAME` | *(from .env)* | Truth Social username (overrides `.env`) |
| `TS_PASSWORD` | *(from .env)* | Truth Social password (overrides `.env`) |

---

## Project structure

```
sentinel/
  core/
    config.py          — config loader and validation
    db.py              — SQLite access layer (Database, StateStore, WalletCache, PostPriceTracking)
  collectors/
    truth_social.py         — Truth Social post monitor (collector logic)
    truth_social_client.py  — Playwright browser client for Truth Social API
    kalshi.py                — Kalshi prediction market monitor (primary source)
    polymarket.py           — Polymarket trade/odds monitor (ACMA-blocked, inert in AU)
    futures_volume.py  — futures volume monitor (yfinance; Alpaca inert, no futures support)
    correlation_detector.py — multi-source signal correlator
    *_runner.py        — entrypoints for each collector
  dispatcher/
    alerter.py         — ntfy alert dispatcher
    alerter_runner.py  — entrypoint
  dashboard/
    app.py             — Flask dashboard
  scripts/
    init_db.py             — database initialiser
    healthcheck.py         — collector liveness check (cron/ad-hoc)
    test_alert.py          — send a test ntfy notification
    signal_diagnostics.py  — cheap signal-to-noise proxies (burst/correlation)
    price_followup.py      — post-signal price backfill scheduler
    signal_scorecard.py    — real signal-to-noise scorecard from price follow-through
    mutation_gate.py       — mutation-testing quality gate
deploy/systemd/        — systemd --user service files (see deploy/systemd/README.md)
tests/
  unit/                — unit tests (the full suite; 548 passed, 10 skipped as of 2026-08-03)
  integration/, e2e/   — empty placeholders, no tests yet
config.yaml.example    — annotated config template
```
