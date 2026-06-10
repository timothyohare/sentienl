# Sentinel — Next Steps

_Updated 2 June 2026_

---

## Completed (MVP Implementation)

- [x] **SQLite schema + `db.py` access layer** — WAL mode enabled, `synchronous=NORMAL`, all tables with indexes
- [x] **`config.py` loader** — UTC-only time parsing, midnight-crossing window logic, full validation
- [x] **`config.yaml.example`** — all fields including `digest_time_utc`, Alpaca config, `quiet_suppress_below`, roll dates, fallback account ID
- [x] **`init_db.py` script** — idempotent, verifies WAL mode and expected tables
- [x] **Truth Social collector + state tracking** — exponential backoff, fallback account ID, keyword filter
- [x] **Alert dispatcher** — ntfy integration, SQLite polling (not Redis), quiet hours, rate limiting, daily digest
- [x] **Dashboard** — Flask app with home feed and `/health` endpoint
- [x] **systemd service files** — all 5 services with `After=network.target network-online.target`
- [x] **Polymarket collector** — large bet detection, odds move, volume spike (with absolute floor), wallet age cache in SQLite
- [x] **Futures volume collector** — Alpaca primary / yfinance fallback, absolute volume floors, roll date suppression
- [x] **Correlation detector** — SQL query for multi-source HIGH/CRITICAL events in 10-min window, fires CRITICAL alert
- [x] **`healthcheck.py` script** — checks all collectors are alive
- [x] **`test_alert.py` script** — sends a synthetic test notification via ntfy
- [x] **Unit tests** — full suite for all components
- [x] **`requirements.txt`** — pinned versions
- [x] **`.gitignore`** — covers `config.yaml`, `sentinel.db`, `venv/`, etc.
- [x] **Decided: SQLite polling** (not Redis) for the event bus — simpler, more reliable for single-server
- [x] **Decided: synchronous collectors** — `httpx` + `time.sleep()`, no `apscheduler`
- [x] **UTC midnight-crossing window** — implemented correctly in `config.py`
- [x] **All times in config are UTC** — AEST shown only as comments
- [x] **Dropped `web3` dependency** — Polygon RPC via plain HTTP calls
- [x] **Alpaca as primary futures data source** — yfinance as fallback
- [x] **Added BZ=F, NG=F, GC=F, DX-Y.NYB** to instruments
- [x] **CME roll date calendar in `config.yaml.example`** — next 12 months
- [x] **Post-signal price tracking** — `post_price_tracking` table in DB schema (`price_t0`, `price_t15`, `price_t60`, `price_t240`, `price_t1440`)
- [x] **Wallet age cache in SQLite** — keyed by address, persists across restarts
- [x] **Renamed `quiet_min_priority` → `quiet_suppress_below`**
- [x] **Hardcoded fallback Truth Social account ID** in `config.yaml.example`
- [x] **Kalshi collector** — replaces Polymarket as prediction market source (blocked in AU by ACMA). Signals: `large_bet` (HIGH), `odds_move` (MEDIUM), `volume_spike` (MEDIUM). Public API, no auth required for read-only data. 33 unit tests.
- [x] **Truth Social Playwright client** — Cloudflare blocks all direct HTTP to truthsocial.com. Added `truth_social_client.py` (headless Chromium login + in-browser fetch) and refactored collector to use pluggable client protocol. Credentials via `.env` file or `TS_USERNAME`/`TS_PASSWORD` env vars. 44 unit tests.

---

## Pre-Launch Spikes (All Complete)

- [x] **Spike: Validate Truth Social API from deployment machine** — Cloudflare blocks direct HTTP from this IP. Solved with Playwright headless browser: navigates to site (passes JS challenge), logs in via web UI, uses in-browser `fetch()` for API calls. Confirmed working: posts fetched successfully, repeated polling stable.
- [x] **~~Spike: Validate Polymarket gamma API~~** — **Blocked.** Polymarket is now classified as an illegal online gambling service in Australia by ACMA under the Interactive Gambling Act 2001. DNS-blocked nationally, not just from the dev machine. Replaced by Kalshi collector.
- [x] **Spike: Validate Kalshi API from deployment machine** — Public API confirmed working. Built full collector with `large_bet`, `odds_move`, `volume_spike` signals. No auth required for read-only endpoints. No geo-block from Australia. Remaining task: find and configure relevant geopolitical event tickers in `config.yaml` before going live.
- [x] **Spike: Validate Alpaca free-tier futures data** — **Alpaca does not support futures.** Data API returns "invalid symbol" for CL=F, ES=F, etc. Zero assets in `futures` asset class. Alpaca covers stocks, crypto, and options only. Paper trading account active (keys in `.env`) — keep for potential stock/ETF monitoring pivot. **yfinance (v1.4.1) is the sole futures data source:** all 6 instruments working, ~10min latency on 1-min bars, volume data present, no rate limiting at 60s cadence. Pinned v0.2.40 was broken; updated `requirements.txt` to `>=1.4.0`. DX-Y.NYB has zero intraday volume — consider daily-only or dropping.

---

## Operational Setup (Before Going Live)

- [x] **Wire `healthcheck.py` to cron** — installed: `7 * * * * .../healthcheck.py --heartbeat`. Runs hourly at :07, sends ntfy heartbeat. Monitored sources: `truth_social`, `kalshi`, `futures_oil`. Stale threshold: 30 minutes.
- [x] **Smoke test end-to-end** — `test_alert.py` sent successfully, ntfy notification received on phone. Topic: `sentinel-timohare-2026`.
- [ ] **48-hour burn-in run** — run all services, suppress LOW/MEDIUM alerts, review signal/noise ratio daily

---

## Calibration Plan (Post-Launch)

- [ ] Run a 1-week silent period after launch — log everything, suppress LOW/MEDIUM alerts, review signal/noise ratio daily
- [ ] Document calibration process: which thresholds to adjust, what a "good" week of signals looks like

---

## ~~Reconsider for v1.1~~ (Done — already in MVP)

- [x] **Correlation detector** — Already implemented in `sentinel/collectors/correlation_detector.py`. Runs a SQL self-join every 5 minutes looking for HIGH/CRITICAL signals from 2+ distinct sources within a 10-minute window. Fires a CRITICAL `correlated_signal`. Works automatically with all sources (Truth Social, Kalshi, futures) — no configuration needed. The docstring references Polymarket but the SQL query is source-agnostic.

---

## Finance Practitioner — Remaining Items

_Completed items moved to the Completed section above._

### Validate Signal Logic Against History

- [ ] **Run historical backtest on known events** — using yfinance *historical* data, check whether the futures volume spike algorithm would have fired on: (1) Soleimani assassination Jan 3 2020 (WTI +4%), (2) Russia-Ukraine invasion Feb 24 2022 (Brent +8%), (3) Gaza Oct 7 2023 (oil +4%). If not, thresholds need revisiting before going live.

### ~~Add Kalshi as Second Prediction Market Source~~ (Done)

- [x] **Spike: Validate Kalshi API** — Completed. Kalshi is CFTC-regulated, public read-only API at `https://external-api.kalshi.com/trade-api/v2`. No auth for markets/trades endpoints. No geo-block from Australia. Trade-level data accessible via `/markets/trades` (fields: `trade_id`, `count_fp`, `yes_price_dollars`, `taker_side`, `created_time`). Market data via `/markets` (fields: `last_price_dollars`, `volume_fp`, `volume_24h_fp`, `open_interest_fp`). Events grouped by ticker (e.g. `KXMIDEASTWAR`). Categories include World, Politics, Economics, Financials. Geopolitical coverage thinner than Polymarket (more US-focused) but adequate for Sentinel's use case.
- [x] **Build Kalshi collector** — Implemented in `sentinel/collectors/kalshi.py`. Signals: `large_bet` (HIGH), `odds_move` (MEDIUM), `volume_spike` (MEDIUM). No `new_wallet` equivalent (KYC platform). Runner: `python -m sentinel.collectors.kalshi_runner`. Source `"kalshi"` feeds into correlation detector.

### Define the Execution Pipeline

- [ ] **Map the alert-to-execution workflow** — document the realistic time from push notification received to order fill. Time this process during market hours with a practice order. Determine whether alerts need broker deep-links or pre-configured watchlists.
- [ ] **Pre-configure broker watchlists** — set up watchlists for all monitored instruments before going live.

### Legal and Regulatory

- [ ] **Expand the legal section in the PRD** — obtain advice on: (a) ASIC insider trading provisions (s1043A Corporations Act); (b) ATO reporting for futures/derivatives profits; (c) record-keeping for sophisticated investor classification.
- [ ] **Add a signal-to-trade log** — manually record which signal prompted consideration, what decision was made, what trade was placed, and the outcome. Primary defence against regulatory inquiry; also the data needed to evaluate alpha.

### Options Flow (Low Priority, High Signal)

- [ ] **Investigate Unusual Whales API** — unusual options activity on USO, XOP, and CL options frequently precedes futures moves. Free tier API. Even as a manual daily scan rather than automated monitoring, it adds meaningful signal quality.

---

## Signal Quality Principles (Reference)

Before the system is considered production-ready, validate against these practitioner standards:

- **The correlation detector must be live** — a single-source alert has low signal-to-noise; multi-source correlated alerts are the only alerts worth acting on at size
- **Data latency must be measured and documented** — if futures data is 15 minutes late, the system cannot claim to be a pre-move detector
- **Every threshold must have an absolute floor** — relative multipliers without absolute minimums will fire on low-liquidity noise
- **The system must know when it is wrong** — post-signal price tracking is not optional for a system you intend to use for trading decisions
