# Sentinel

Geopolitical signal monitoring system. Watches Truth Social posts, Polymarket prediction market activity, and futures volume for anomalies, then sends push notifications via ntfy.

## Requirements

- Python 3.11+
- ntfy account (free at ntfy.sh, or self-hosted)
- Optional: Alpaca Markets free API key for real-time futures data

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

For development (includes pytest, coverage):

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
- `futures.alpaca_api_key` / `alpaca_api_secret` — from https://alpaca.markets (free)
- `polymarket.polygonscan_api_key` — optional, for wallet age lookups

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
# Alert dispatcher (reads signals from DB, sends ntfy notifications)
python -m sentinel.dispatcher.alerter_runner

# Truth Social collector
python -m sentinel.collectors.truth_social_runner

# Polymarket collector
python -m sentinel.collectors.polymarket_runner

# Futures volume collector
python -m sentinel.collectors.futures_runner

# Dashboard (http://127.0.0.1:5000)
python -m sentinel.dashboard.app
```

### Run with systemd (production)

The `systemd/` directory contains service files for all 5 components. To install:

```bash
# Copy service files
sudo cp systemd/*.service /etc/systemd/system/

# Edit WorkingDirectory and User in each file to match your setup
sudo nano /etc/systemd/system/sentinel-alerter.service
# (repeat for each service)

# Reload and enable
sudo systemctl daemon-reload
sudo systemctl enable sentinel-alerter sentinel-truth sentinel-polymarket sentinel-futures sentinel-dashboard
sudo systemctl start sentinel-alerter sentinel-truth sentinel-polymarket sentinel-futures sentinel-dashboard
```

Check status:

```bash
sudo systemctl status sentinel-truth
journalctl -u sentinel-alerter -f
```

---

## Health check

```bash
python sentinel/scripts/healthcheck.py
```

Optionally wire to cron for passive monitoring (hourly heartbeat to ntfy):

```bash
crontab -e
# Add:
0 * * * * /home/timohare/dev/newdev/Sentinel/venv/bin/python /home/timohare/dev/newdev/Sentinel/sentinel/scripts/healthcheck.py
```

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
    db.py              — SQLite access layer
  collectors/
    truth_social.py         — Truth Social post monitor (collector logic)
    truth_social_client.py  — Playwright browser client for Truth Social API
    polymarket.py           — Polymarket trade/odds monitor
    futures_volume.py  — CME futures volume monitor
    correlation_detector.py — multi-source signal correlator
    *_runner.py        — entrypoints for each collector
  dispatcher/
    alerter.py         — ntfy alert dispatcher
    alerter_runner.py  — entrypoint
  dashboard/
    app.py             — Flask dashboard
  scripts/
    init_db.py         — database initialiser
    healthcheck.py     — collector liveness check
    test_alert.py      — send a test ntfy notification
systemd/               — systemd service files
tests/
  unit/                — unit tests
  integration/         — integration tests
config.yaml.example    — annotated config template
```
