# Sentinel — Next Steps

_Updated 28 March 2026_

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

---

## Still To Do — Blockers Before Running Live

- [ ] **Spike: Validate Truth Social API from deployment machine** — hit `https://truthsocial.com/api/v1/accounts/:id/statuses` from the actual server, confirm unauthenticated JSON works, measure round-trip, test 10 rapid requests for 429 behaviour
- [ ] **Spike: Validate Polymarket gamma API from deployment machine** — the gamma API was DNS-blocked from the dev machine (noted in `config.yaml.example`). Must confirm access from the server before relying on this collector.
- [ ] **Spike: Validate Alpaca free-tier futures data** — confirm real-time 1-min bar latency, check that CL=F / ES=F are available on free tier, verify rate limits at 60-second polling cadence

---

## Operational Setup (Before Going Live)

- [ ] **Wire `healthcheck.py` to cron** — add a cron entry: `0 * * * * /path/to/venv/bin/python /path/to/Sentinel/sentinel/scripts/healthcheck.py` (hourly, sends silent ntfy ping confirming collectors are alive; absence of heartbeat = system is down)
- [ ] **Smoke test end-to-end** — run `python sentinel/scripts/test_alert.py` to confirm ntfy delivery works before starting the real collectors
- [ ] **48-hour burn-in run** — run all services, suppress LOW/MEDIUM alerts, review signal/noise ratio daily

---

## Calibration Plan (Post-Launch)

- [ ] Run a 1-week silent period after launch — log everything, suppress LOW/MEDIUM alerts, review signal/noise ratio daily
- [ ] Document calibration process: which thresholds to adjust, what a "good" week of signals looks like

---

## Reconsider for v1.1 (Not v2)

- [ ] **Correlation detector** — Polymarket AND futures moving together in the same 10-minute window is the highest-signal pattern described in the PRD motivation. It's currently buried in v2. Consider pulling it to v1.1 as it's a pure signal-aggregation layer on top of already-collected data.

---

## Finance Practitioner — Remaining Items

_Completed items moved to the Completed section above._

### Validate Signal Logic Against History

- [ ] **Run historical backtest on known events** — using yfinance *historical* data, check whether the futures volume spike algorithm would have fired on: (1) Soleimani assassination Jan 3 2020 (WTI +4%), (2) Russia-Ukraine invasion Feb 24 2022 (Brent +8%), (3) Gaza Oct 7 2023 (oil +4%). If not, thresholds need revisiting before going live.

### Add Kalshi as Second Prediction Market Source

- [ ] **Spike: Validate Kalshi API** — Kalshi is US-regulated, covers geopolitical events (military action, executive orders, sanctions), and is used by sophisticated participants who may avoid Polymarket. Document the API endpoint, market structure, and whether trade-level data is accessible. If viable, add it as an optional second collector.

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
