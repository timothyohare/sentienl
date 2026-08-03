# Sentinel — Architecture

## ASCII Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           SENTINEL                                           │
│                  Geopolitical Signal Monitoring System                       │
└─────────────────────────────────────────────────────────────────────────────┘

  External Sources                Collectors                    SQLite DB
  ────────────────        ─────────────────────────        ──────────────────
                          │                       │        │                 │
  truthsocial.com ───────>│  truth_social.py      │        │   signals       │
  (Mastodon API,          │  - Poll every 8s      │───────>│   state         │
   via Playwright)        │  - Backfill on start  │        │   wallet_cache  │
                          │  - Exp backoff 429    │        │   post_price_   │
                          └───────────────────────┘        │   tracking      │
                                                           │                 │
  external-api.           ┌───────────────────────┐        └────────┬────────┘
  kalshi.com  ───────────>│  kalshi.py             │                │
  (public, no auth)       │  - Poll every 30s      │───────>────────┤
                          │  - large_bet (HIGH)    │                │
                          │  - odds_move (MEDIUM)  │                │
                          │  - volume_spike (MED)  │                │
                          └───────────────────────┘                │
                                                                    │
  gamma-api.polymarket.com ── DNS-blocked in Australia (ACMA) ──── │
  polymarket.py still in the repo, not run from AU infrastructure  │
                                                                    │
  Yahoo Finance (yfinance)┌───────────────────────┐                │
  Alpaca inert (no       ─>│  futures_volume.py     │───────>────────┤
  futures support)        │  - Poll every 60s      │                │
                          │  - CL=F, BZ=F, NG=F   │                │
                          │  - GC=F, ES=F, DXY    │                │
                          │  - Roll date suppress  │                │
                          └───────────────────────┘                │
                                                                    │
                          ┌───────────────────────┐                │
                          │  correlation_detector  │<──────<────────┤
                          │  - SQL every 5 min    │                │
                          │  - 2+ sources/10 min  │───────>────────┤
                          └───────────────────────┘                │
                                                                    │
                          ┌───────────────────────┐                │
  Kalshi REST /           │  price_followup.py     │<──────<────────┤
  yfinance (backfill) ───>│  - runs every 5 min    │───────>────────┤
                          │  - fills t15/60/240/1440
                          └───────────────────────┘                │
                                                                    │
                                                            ────────┘
                                                                    │
  Alerter (polls SQLite)          Phone                            │
  ─────────────────────        ─────────────                       │
  ┌───────────────────────┐    │           │                       │
  │  alerter.py            │<──<│  SQLite   │<──────────────────────┘
  │  - Poll every 2s       │    │  alerted=0│
  │  - Rate limit 5 min    │    └───────────┘
  │  - Quiet hours UTC     │
  │  - Daily digest        │───────> ntfy.sh ──────> iPhone/Android
  │  - Priority format     │         (push notification, gated by
  └───────────────────────┘          alerts.enabled in config.yaml)

  Dashboard (Flask)
  ─────────────────
  ┌───────────────────────┐
  │  dashboard/app.py     │<─── SQLite (read-only)
  │  /                    │
  │  /signals             │──── http://localhost:5000
  │  /truth               │     (LAN only, no auth)
  │  /polymarket          │     Note: /health's monitored-source list still
  │  /health              │     includes "polymarket" (always stale/warn now)
  │  HTMX auto-refresh    │     but does not include "kalshi" — a known gap,
  │  Times in AEST        │     not yet fixed.
  └───────────────────────┘

  Process Management (systemd --user, deploy/systemd/)
  ──────────────────
  sentinel-truth-social.service   → truth_social.py
  sentinel-kalshi.service         → kalshi.py
  sentinel-futures.service        → futures_volume.py
  sentinel-alerter.service        → alerter.py + correlation_detector.py (thread)
  sentinel-price-followup.service → price_followup.py
  (all under sentinel.target; no root required; Polymarket has no unit)
```

## Data Flow

1. **Collector polls** external API on its configured interval
2. **Signal threshold crossed** → collector calls `db.insert_signal()`
3. **Signal written** to `signals` table with `alerted=0`
   - For HIGH/CRITICAL signals from `kalshi`/`futures_*`, the collector also
     snapshots `price_t0` into `post_price_tracking` at the same time
4. **Alerter polls** SQLite every 2 seconds for `alerted=0` records
5. **Rate limit + quiet hours** checks applied (CRITICAL bypasses both)
6. **ntfy POST** sent with formatted title + body (no-ops if `alerts.enabled: false` — data-collection-only mode)
7. **Signal marked** `alerted=1` after successful send
8. **Dashboard reads** from SQLite for display (independent of alerter)
9. **price_followup.py**, on its own 5-min timer, backfills `price_t15/t60/t240/t1440` for tracked signals once each horizon has elapsed
10. **signal_scorecard.py**, run on demand, computes real per-signal-type effect sizes from that price history

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Event bus | No Redis — SQLite polling | Simpler, at-least-once delivery, no extra dependency |
| Concurrency | Synchronous + `time.sleep()` | Predictable, debuggable, no async complexity |
| Alert times | UTC internally, AEST in UI | Single source of truth; display conversion at boundary |
| Correlation | Pure SQL query | Zero extra code, uses existing signals table |
| Wallet age | HTTP to Polygonscan (optional) | Avoids 20MB web3 dependency; Polymarket-specific, unused while ACMA-blocked |
| Prediction market | Kalshi (public API) | Polymarket is ACMA-blocked in Australia (Interactive Gambling Act 2001); Kalshi is CFTC-regulated and geo-accessible |
| Futures data source | yfinance | Alpaca doesn't support futures (rejects futures symbols); Alpaca config/code kept inert for a possible stock/ETF pivot |
| Price follow-through baseline | Pooled effect size across tracked signal types | A true random-window baseline needs a continuous price-history table — deliberately out of schema scope (see `plans/05-price-follow-through.md`) |
| Process supervision | `systemd --user`, `Restart=always` | No root required; survives crashes; `loginctl enable-linger` survives logout/reboot |

## File Structure

```
Sentinel/
├── sentinel/
│   ├── core/
│   │   ├── db.py           # SQLite access layer (Database, StateStore, WalletCache, PostPriceTracking)
│   │   └── config.py       # Config loader (typed dataclasses, validation)
│   ├── collectors/
│   │   ├── truth_social.py           # Truth Social Mastodon API poller (via Playwright client)
│   │   ├── truth_social_client.py    # Playwright browser client
│   │   ├── kalshi.py                 # Kalshi prediction market poller (primary source)
│   │   ├── polymarket.py             # Polymarket poller (ACMA-blocked, inert in AU)
│   │   ├── futures_volume.py         # yfinance futures volume poller (Alpaca inert)
│   │   ├── correlation_detector.py   # SQL-based multi-source correlation
│   │   ├── truth_social_runner.py    # entry point
│   │   ├── kalshi_runner.py          # entry point
│   │   ├── polymarket_runner.py      # entry point (not run in AU)
│   │   └── futures_runner.py         # entry point
│   ├── dispatcher/
│   │   ├── alerter.py                # ntfy dispatcher + rate limiter + quiet hours
│   │   └── alerter_runner.py         # entry point
│   ├── dashboard/
│   │   └── app.py                    # Flask dashboard (inline templates)
│   └── scripts/
│       ├── init_db.py                # Database initialiser
│       ├── healthcheck.py            # Liveness checker (cron/ad-hoc)
│       ├── test_alert.py             # Smoke test ntfy
│       ├── signal_diagnostics.py     # Cheap signal-to-noise proxies (burst/correlation)
│       ├── price_followup.py         # Post-signal price backfill scheduler
│       ├── signal_scorecard.py       # Real signal-to-noise scorecard (price follow-through)
│       └── mutation_gate.py          # Mutation-testing quality gate
├── tests/
│   ├── unit/                         # Mocked HTTP tests (pytest) — the full suite
│   ├── integration/                  # Empty placeholder, no tests yet
│   └── e2e/                          # Empty placeholder, no tests yet
├── deploy/systemd/                   # systemd --user service files (see deploy/systemd/README.md)
├── docs/
│   └── architecture.md              # This file
├── plans/                            # Design docs for shipped/in-progress work (01-05)
├── config.yaml.example              # Template config (commit this)
├── config.yaml                      # Real config (gitignored)
├── requirements.txt
├── requirements-dev.txt
└── pyproject.toml
```

## Signal Schema

Each signal record captures:
- `source`: which collector fired (`truth_social` | `kalshi` | `polymarket` | `futures_*` | `correlation_detector`)
- `signal_type`: event type (`new_post` | `large_bet` | `odds_move` | `volume_spike` | `correlated_signal`; Polymarket additionally has `new_wallet`, Kalshi does not — it's a KYC'd platform)
- `priority`: CRITICAL | HIGH | MEDIUM | LOW | INFO
- `payload`: full JSON from collector (post text, trade amounts, volume ratios, etc.)
- `summary`: one-line human-readable description
- `alerted`: 0/1 (whether ntfy notification was sent)
- `created_at`: UTC ISO8601 timestamp

`post_price_tracking` (see `plans/05-price-follow-through.md`) additionally
records `price_t0/t15/t60/t240/t1440` per HIGH/CRITICAL `kalshi`/`futures_*`
signal, keyed by `(signal_id, instrument)`.

## Midnight-Crossing Window Logic

The active window (11:00–04:00 UTC) crosses midnight. The `is_in_window()` function handles this:

```python
def is_in_window(now_utc: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= now_utc <= end   # normal window
    else:
        return now_utc >= start or now_utc <= end  # crosses midnight
```

This is used for both the futures active window and alert quiet hours.
