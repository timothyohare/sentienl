# SENTINEL
## Geopolitical Signal Monitoring System

_Product Requirements Document · Technical Specification · Verification Plan_

---

| Field | Detail |
|---|---|
| Document version | 0.1 — Draft |
| Status | Pre-development |
| Target platform | Home Linux server (Ubuntu 22.04+) |
| Author | Tim O'Hare |
| Last updated | 26 March 2026 |

> ⚠️ **Disclaimer:** This document describes a personal informational monitoring tool. Nothing in this system constitutes financial advice or insider trading. All data sources are publicly accessible. The system observes and alerts only — all trading decisions remain the user's own.

---

## Table of Contents

1. [Purpose & Background](#1-purpose--background)
2. [Goals & Non-Goals](#2-goals--non-goals)
3. [System Architecture](#3-system-architecture)
4. [Collector Specifications](#4-collector-specifications)
5. [Alert Dispatcher](#5-alert-dispatcher)
6. [Signal Database](#6-signal-database-sqlite)
7. [Local Dashboard](#7-local-dashboard)
8. [Project Structure](#8-project-structure)
9. [Configuration](#9-configuration-configyaml)
10. [Installation Plan](#10-installation-plan)
11. [Verification & Acceptance Testing](#11-verification--acceptance-testing)
12. [Known Risks & Mitigations](#12-known-risks--mitigations)
13. [Roadmap](#13-roadmap)

---

## 1. Purpose & Background

In March 2026 it was widely reported that traders were consistently executing large positions in oil futures and prediction market contracts 10–15 minutes before President Trump published market-moving posts on Truth Social. Polymarket wallets placed bets on the US-Iran ceasefire before the announcement was public. Oil futures saw $580M in unusual volume in a single minute before a 14% price move.

Whether those traders had genuine insider access or were simply faster at processing public signals, the episode makes clear there is material value in compressing reaction time to public information. Sentinel is a personal monitoring system designed to:

- Detect new Trump Truth Social posts within seconds of publication
- Detect unusual Polymarket betting activity (new wallets, outsized positions, sudden volume in inactive markets)
- Detect anomalous pre-market volume spikes in WTI oil and S&P 500 futures
- Deliver consolidated, actionable alerts to a phone via push notification
- Run 24/7 on a home Linux server with minimal maintenance

---

## 2. Goals & Non-Goals

### Goals

| Goal | Target |
|---|---|
| **Speed** | Truth Social alerts within 10 seconds of post publication |
| **Reliability** | 99%+ uptime during active monitoring windows (9pm–2am AEST) |
| **Low noise** | Alerts only on signals that meet defined thresholds — no spam |
| **Self-hosted** | Runs entirely on home Linux server, no paid cloud services required |
| **Maintainable** | Simple enough to debug and modify solo |
| **Observable** | Local dashboard to review signal history and tune thresholds |

### Non-Goals

- Automated trading — Sentinel observes and alerts only, never places trades
- Comprehensive financial data terminal — this is a signal detector, not a Bloomberg terminal
- Machine learning or predictive modelling in v1
- Mobile app — push notifications via Ntfy or Pushover is sufficient
- Multi-user — single user only

---

## 3. System Architecture

### 3.1 High-Level Overview

Sentinel consists of four independent polling services (collectors), a central event bus, an alert dispatcher, a SQLite database for signal history, and a lightweight local web dashboard.

| Component | Role | Technology |
|---|---|---|
| Truth Social Collector | Poll for new posts | Python + httpx |
| Polymarket Collector | Watch for unusual bet activity | Python + Polymarket API |
| Futures Volume Collector | Detect pre-market volume spikes | Python + yfinance / Alpaca |
| Event Bus | Route signals to alerter + DB | Redis pub/sub (local) |
| Alert Dispatcher | Send push notifications | Python + Ntfy.sh |
| Signal Database | Persist all events for review | SQLite |
| Dashboard | View signal history + config | Flask + HTMX (local only) |
| Process Manager | Keep all services running | systemd |

### 3.2 Data Flow

1. Each collector runs on its own poll interval (configurable per source)
2. When a signal threshold is crossed, the collector publishes a structured event to Redis
3. The Alert Dispatcher subscribes to all events, formats a push notification, and sends it
4. All events (signal or not) are written to SQLite for threshold tuning and history
5. The Dashboard reads from SQLite and displays a timeline of recent signals

### 3.3 Server Requirements

| Requirement | Minimum |
|---|---|
| OS | Ubuntu 22.04 LTS or Debian 12 |
| RAM | 512MB minimum — 1GB recommended |
| Disk | 10GB free |
| CPU | Any — polling is not CPU intensive |
| Network | Stable home broadband |
| Python | 3.11+ |
| Redis | 7.x (`apt install redis-server`) |

---

## 4. Collector Specifications

### 4.1 Truth Social Collector

**Objective:** Detect new posts from @realDonaldTrump within 10 seconds of publication.

**Approach:** Truth Social is built on Mastodon-compatible software and exposes a public API. The account's public timeline can be polled without authentication.

| Parameter | Value | Notes |
|---|---|---|
| Endpoint | `https://truthsocial.com/api/v1/accounts/:id/statuses` | Account ID resolved at startup |
| Poll interval | 8 seconds | Below 10s target; respectful of server |
| Auth required | No | Public timeline is unauthenticated |
| State tracking | Last seen post ID stored in SQLite | Avoids re-alerting on same post |
| Signal condition | Any new post from target account | All posts trigger alert in v1 |
| Payload captured | Post ID, timestamp, full text, media flag | Stored to DB |

**Alert format:**
```
🚨 TRUTH SOCIAL — New Trump post · [timestamp]
[First 280 characters of post text]
Full post: [URL]
```

> 📌 In v2, a keyword filter layer can classify posts by topic (Iran, oil, tariffs, China) and adjust alert priority accordingly.

---

### 4.2 Polymarket Collector

**Objective:** Detect unusual betting activity in tracked geopolitical markets — new wallets placing outsized bets, sudden volume increases in previously quiet markets, and rapid odds movement.

**Approach:** Polymarket runs on Polygon blockchain. All trades are on-chain and queryable. Polymarket also exposes a REST API for market data.

| Parameter | Value | Notes |
|---|---|---|
| Primary API | `https://gamma-api.polymarket.com` | REST — markets, positions, trades |
| Secondary | Polygon RPC (public) | On-chain verification of wallet age |
| Poll interval | 30 seconds | Trade data latency acceptable at 30s |
| Markets tracked | Configurable list of market slugs | Seeded with current geopolitical markets |
| Signal: Large bet | Single bet > $5,000 USDC | Configurable threshold |
| Signal: New wallet | Wallet age < 7 days + bet > $1,000 | High-signal pattern |
| Signal: Odds move | 5+ percentage point shift in < 5 min | Rapid repricing |
| Signal: Volume spike | Trade volume 3x 24hr average in 10 min window | Configurable multiplier |

**Alert format:**
```
📊 POLYMARKET SIGNAL — [Market name]
Type: [Large bet / New wallet / Odds move / Volume spike]
Detail: New wallet (3 days old) bet $8,400 YES on US-Iran ceasefire by April 15
Current odds: YES [x]¢ · NO [x]¢ · [Market URL]
```

> 📌 Wallet age is determined by querying the Polygon blockchain for the wallet's first transaction. This is free using a public RPC endpoint.

---

### 4.3 Futures Volume Collector

**Objective:** Detect anomalous pre-market volume spikes in WTI Crude Oil and S&P 500 e-Mini futures, similar to the pattern observed before the Trump Iran post on 24 March 2026.

**Approach:** Use Yahoo Finance (free, no API key) or Alpaca Markets (free tier, more reliable) to fetch 1-minute OHLCV data for futures proxies.

| Parameter | Value | Notes |
|---|---|---|
| WTI Oil proxy | `CL=F` (Yahoo Finance ticker) | Front-month WTI futures |
| S&P 500 proxy | `ES=F` (Yahoo Finance ticker) | e-Mini S&P 500 futures |
| Poll interval | 60 seconds | 1-min candle resolution sufficient |
| Signal condition | Current 1-min volume > 3x rolling 20-bar average | Configurable multiplier |
| Active window | 9pm–2am AEST (11am–4pm UTC) | US pre-market + market open |
| Outside window | Collector still runs, threshold raised to 5x | Reduce noise overnight |
| Data source | `yfinance` Python library (free) | Falls back to Alpaca if unavailable |

**Alert format:**
```
🛢️ VOLUME SPIKE — [WTI Oil / S&P 500 futures]
Current 1-min volume: [X] contracts · 20-bar avg: [Y] contracts · Ratio: [Z]x
Price: [current] · Change: [+/- %] · Time: [UTC]
```

> ⚠️ Yahoo Finance data has occasional gaps and delays of 1–2 minutes for futures. For tighter latency, Alpaca Markets (free account) or Tradier API provide more reliable data feeds.

---

## 5. Alert Dispatcher

### 5.1 Push Notification Stack

Sentinel uses Ntfy for push notifications. Ntfy is free, open-source, and has iOS and Android apps. Messages are sent via a simple HTTP POST.

| Option | Cost | Setup complexity | Recommended for |
|---|---|---|---|
| ntfy.sh (cloud) | Free | Minimal — just install the app | Starting out |
| ntfy (self-hosted) | Free | Medium — Docker on same server | Privacy-conscious |
| Pushover | $5 one-time | Low | Alternative if ntfy not preferred |
| Telegram bot | Free | Low — BotFather setup | Alternative option |

### 5.2 Alert Priority Levels

| Priority | Condition | Notification behaviour |
|---|---|---|
| CRITICAL | Truth Social new post | Immediate push, sound, no batching |
| HIGH | Polymarket new wallet + large bet | Immediate push, sound |
| HIGH | Futures volume > 4x average | Immediate push, sound |
| MEDIUM | Polymarket odds move > 5pp | Push within 60 seconds, may batch |
| LOW | Futures volume > 3x average | Push within 2 minutes, batched |
| INFO | Market opens/closes, system health | Silent notification |

### 5.3 Alert Rate Limiting

To prevent alert fatigue, the dispatcher enforces:

- Maximum 1 alert per source per 5-minute window (except CRITICAL priority)
- Truth Social alerts are never rate-limited — every post fires immediately
- A daily digest at 7:00am AEST summarises all signals from the prior 24 hours
- Quiet hours configurable (e.g. 3am–7am AEST) for LOW and INFO priority only

---

## 6. Signal Database (SQLite)

### 6.1 Schema

**`signals` table**

| Column | Type | Description |
|---|---|---|
| id | INTEGER PK | Auto-increment |
| source | TEXT | `truth_social` \| `polymarket` \| `futures_oil` \| `futures_sp500` |
| signal_type | TEXT | `new_post` \| `large_bet` \| `new_wallet` \| `odds_move` \| `volume_spike` |
| priority | TEXT | `CRITICAL` \| `HIGH` \| `MEDIUM` \| `LOW` \| `INFO` |
| payload | TEXT (JSON) | Full structured data from collector |
| summary | TEXT | Human-readable one-line summary |
| alerted | BOOLEAN | Whether push notification was sent |
| created_at | DATETIME | UTC timestamp of detection |

**`state` table**

| Column | Type | Description |
|---|---|---|
| key | TEXT PK | e.g. `truth_social_last_post_id` |
| value | TEXT | Serialised state value |
| updated_at | DATETIME | Last updated |

### 6.2 Retention

- Signal records kept for 90 days (configurable)
- Nightly cleanup job removes records older than retention window
- SQLite file backed up weekly to a local directory

---

## 7. Local Dashboard

### 7.1 Purpose

A minimal web UI accessible only on the local network (`http://localhost:5000`) for reviewing signal history, tuning thresholds, and monitoring system health. Not exposed to the internet.

### 7.2 Pages

| Page | Content |
|---|---|
| `/` (Home) | Last 20 signals in reverse chronological order, colour-coded by priority |
| `/signals` | Full searchable/filterable signal log with JSON payload viewer |
| `/config` | Edit threshold values and save to `config.yaml` |
| `/health` | Collector status: last poll time, error count, latency per source |
| `/truth` | Timeline of all captured Trump posts with full text |
| `/polymarket` | List of tracked markets with current odds and recent activity |

### 7.3 Technology

- **Backend:** Flask (Python) — minimal, well-documented, easy to modify
- **Frontend:** HTMX + minimal CSS — no JavaScript framework, no build step
- **Auto-refresh:** Dashboard home page auto-refreshes every 15 seconds
- **Access:** `http://localhost:5000` — LAN access only, no auth required in v1

---

## 8. Project Structure

```
~/sentinel/
├── collectors/
│   ├── truth_social.py
│   ├── polymarket.py
│   └── futures_volume.py
├── core/
│   ├── bus.py          # Redis pub/sub helpers
│   ├── db.py           # SQLite access layer
│   └── config.py       # Load config.yaml
├── dispatcher/
│   └── alerter.py      # Subscribe to bus, send ntfy
├── dashboard/
│   ├── app.py          # Flask app
│   └── templates/
├── scripts/
│   ├── install.sh      # One-shot setup script
│   └── healthcheck.py  # Cron-friendly status check
├── config.yaml         # All thresholds + secrets
├── sentinel.db         # SQLite database
├── requirements.txt
└── systemd/
    ├── sentinel-truth.service
    ├── sentinel-polymarket.service
    ├── sentinel-futures.service
    ├── sentinel-alerter.service
    └── sentinel-dashboard.service
```

---

## 9. Configuration (`config.yaml`)

All thresholds and secrets live in a single `config.yaml` file. No secrets should be committed to version control.

```yaml
# config.yaml — Sentinel configuration

truth_social:
  account_handle: realDonaldTrump
  poll_interval_seconds: 8
  alert_all_posts: true
  keyword_filter: []           # empty = alert on all posts

polymarket:
  poll_interval_seconds: 30
  tracked_markets:
    - us-iran-ceasefire-2026
    - us-attack-iran
    - trump-executive-order-april
  thresholds:
    large_bet_usd: 5000
    new_wallet_age_days: 7
    new_wallet_min_bet_usd: 1000
    odds_move_pct_5min: 5.0
    volume_spike_multiplier: 3.0

futures:
  poll_interval_seconds: 60
  tickers:
    oil: "CL=F"
    sp500: "ES=F"
  thresholds:
    spike_multiplier: 3.0
    spike_multiplier_quiet: 5.0
  active_window_utc:
    start: "11:00"
    end:   "04:00"

alerts:
  provider: ntfy               # ntfy | pushover | telegram
  ntfy_topic: sentinel-tim     # your private topic name
  ntfy_url: https://ntfy.sh    # or self-hosted URL
  rate_limit_minutes: 5
  quiet_hours_aest:
    start: "03:00"
    end:   "07:00"
  quiet_min_priority: MEDIUM

database:
  path: ./sentinel.db
  retention_days: 90

dashboard:
  host: "127.0.0.1"
  port: 5000
```

---

## 10. Installation Plan

### 10.1 Prerequisites

1. Ubuntu 22.04 LTS installed on your home server
2. SSH access to the server from your main machine
3. Python 3.11+: `sudo apt install python3.11 python3.11-venv`
4. Redis: `sudo apt install redis-server && sudo systemctl enable redis`
5. Git: `sudo apt install git`
6. Ntfy app installed on your phone (iOS or Android, free)

### 10.2 Setup Steps

1. Clone the Sentinel repo to `~/sentinel`
2. Create a Python virtual environment: `python3 -m venv venv && source venv/bin/activate`
3. Install dependencies: `pip install -r requirements.txt`
4. Copy `config.yaml.example` to `config.yaml` and fill in your ntfy topic name
5. Run the database initialiser: `python scripts/init_db.py`
6. Copy systemd service files to `/etc/systemd/system/`
7. Enable and start all services: `sudo systemctl enable --now sentinel-*.service`
8. Verify dashboard at `http://localhost:5000`
9. Send a test alert: `python scripts/test_alert.py`

### 10.3 Key Python Dependencies

| Package | Purpose |
|---|---|
| `httpx` | Async HTTP client for all collectors |
| `yfinance` | Futures OHLCV data from Yahoo Finance |
| `redis` | Event bus client |
| `flask` | Dashboard web server |
| `pyyaml` | Config file parsing |
| `apscheduler` | Per-collector scheduling |
| `web3` | Polygon blockchain queries (wallet age) |

---

## 11. Verification & Acceptance Testing

Each collector, the alert dispatcher, and the dashboard must pass these checks before the system is considered production-ready.

### 11.1 Truth Social Collector

| Test ID | Test | Expected result | Pass criteria |
|---|---|---|---|
| TS-01 | Startup — resolve account ID | Account ID logged to console on start | ID is non-null integer |
| TS-02 | Poll returns data | HTTP 200, JSON array of posts returned | No exception, at least 1 post returned |
| TS-03 | New post detection | Set known post ID as last-seen, inject newer one via test fixture | Alert fired within 10 seconds |
| TS-04 | No duplicate alert | Same post ID seen across two polls | Alert fires only once |
| TS-05 | State persistence on restart | Restart service after seeing a post | Does not re-alert on already-seen posts |
| TS-06 | API error handling | Simulate 503 response | Collector logs error, waits 30s, retries — does not crash |

### 11.2 Polymarket Collector

| Test ID | Test | Expected result | Pass criteria |
|---|---|---|---|
| PM-01 | Market list loads | Fetch tracked markets on startup | All configured markets return valid data |
| PM-02 | Large bet signal | Inject synthetic trade of $6,000 into test harness | MEDIUM+ alert fires within 60 seconds |
| PM-03 | New wallet signal | Inject synthetic trade from wallet created 2 days ago for $1,500 | HIGH alert fires within 60 seconds |
| PM-04 | Odds move signal | Simulate 6pp swing in test market | MEDIUM alert fires within 60 seconds |
| PM-05 | Rate limiting | Trigger two signals in same market within 5 minutes | Second alert suppressed, logged as rate-limited |
| PM-06 | Unknown market slug | Configure non-existent market slug | Error logged, other markets unaffected |

### 11.3 Futures Volume Collector

| Test ID | Test | Expected result | Pass criteria |
|---|---|---|---|
| FV-01 | Data fetch | Fetch 1-min OHLCV for `CL=F` and `ES=F` | Returns data with < 3 minute lag |
| FV-02 | Rolling average | Provide 25 candles of synthetic data | 20-bar average computed correctly |
| FV-03 | Spike detection | Inject candle with volume = 3.5x average | Alert fires within 90 seconds |
| FV-04 | Quiet hours threshold | Trigger 3.5x spike during configured quiet hours | Alert suppressed (threshold is 5x in quiet hours) |
| FV-05 | Data gap handling | Simulate missing candle in series | Collector skips gracefully, no crash |
| FV-06 | Both instruments | Verify oil and S&P 500 both polled independently | Both logged to DB separately |

### 11.4 Alert Dispatcher

| Test ID | Test | Expected result | Pass criteria |
|---|---|---|---|
| AD-01 | Test alert end-to-end | Run `python scripts/test_alert.py` | Push notification arrives on phone within 15 seconds |
| AD-02 | Priority formatting | Send CRITICAL and LOW alerts | CRITICAL plays sound, LOW is silent |
| AD-03 | Rate limit enforcement | Publish 3 MEDIUM events from same source in 2 minutes | Only first notification sent; others logged as suppressed |
| AD-04 | Daily digest | Trigger digest manually via script | Summary notification arrives with correct count |
| AD-05 | Ntfy topic isolation | Use a unique topic name | Notifications only received by subscribed device |

### 11.5 Dashboard

| Test ID | Test | Expected result | Pass criteria |
|---|---|---|---|
| DB-01 | Dashboard loads | Navigate to `http://localhost:5000` | Page loads, shows signal feed |
| DB-02 | Signal appears | Fire a test signal | Signal visible in dashboard within 20 seconds |
| DB-03 | Health page | Navigate to `/health` | All collectors show last-polled timestamp < 2 minutes ago |
| DB-04 | Config save | Change a threshold on `/config` page and save | `config.yaml` updated, collector picks up new value on restart |
| DB-05 | LAN access | Access from another device on home network | Dashboard accessible via server LAN IP |

### 11.6 System Acceptance Criteria

The system is considered ready for use when **all** of the following are true:

- [ ] All tests in sections 11.1–11.5 pass
- [ ] System has run for 48 hours continuously without a collector crash
- [ ] At least one real Truth Social post has been detected and alerted in production
- [ ] Dashboard `/health` page shows all collectors green for 24 hours
- [ ] At least one test push notification received on phone within 10 seconds of dispatch
- [ ] SQLite database contains >0 real signal records

---

## 12. Known Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Truth Social API endpoint changes or blocks scrapers | Medium | High | Monitor for 401/403 errors; backup via RSS or third-party mirror |
| Yahoo Finance rate limits or deprecates yfinance | Medium | Medium | Fallback to Alpaca free tier; documented in config |
| Home server goes offline during active trading window | Low | High | systemd auto-restart; optional UPS; healthcheck alert if server unreachable for >10 min |
| Polymarket API changes | Low | Medium | Pin API version; write versioned client |
| Ntfy.sh cloud outage | Low | Medium | Self-host ntfy as fallback; Telegram bot as secondary channel |
| False positives — alert fatigue | Medium | Medium | Rate limiting, threshold tuning, 1-week calibration period before relying on alerts |
| CGT / legal complexity of acting on signals | Low | High | System is observational only; consult accountant re: trading record-keeping |

---

## 13. Roadmap

### v1.0 — MVP _(Target: 2–3 weeks build time)_

- Truth Social collector — all posts, no filtering
- Polymarket collector — large bet + new wallet signals on tracked markets
- Futures volume collector — WTI oil and S&P 500
- Ntfy push alerts
- SQLite logging
- Basic Flask dashboard (home feed + health page)
- systemd service files for all components
- Manual install process documented

### v1.1 — Stability & Tuning _(2 weeks post-launch)_

- One-shot `install.sh` script
- Threshold tuning based on 2 weeks of real signal data
- Daily digest notification
- Quiet hours configuration
- Config editor in dashboard

### v2.0 — Intelligence _(Future)_

- Trump post keyword classifier — tag by topic (Iran, China, tariffs, oil) and raise/lower priority
- Correlation detector — alert when Polymarket AND futures both move within the same 10-minute window
- Telegram integration as secondary alert channel
- Optional: webhook to pre-open a broker CFD ticket on CRITICAL alert
- Prometheus + Grafana metrics

---

_Sentinel — Personal use only. Not for distribution. Not financial advice._
