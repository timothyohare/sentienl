# Sentinel — Architecture

## ASCII Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           SENTINEL v1.0                                      │
│                  Geopolitical Signal Monitoring System                       │
└─────────────────────────────────────────────────────────────────────────────┘

  External Sources                Collectors                    SQLite DB
  ────────────────        ─────────────────────────        ──────────────────
                          │                       │        │                 │
  truthsocial.com ───────>│  truth_social.py      │        │   signals       │
  (Mastodon API)          │  - Poll every 8s      │───────>│   state         │
                          │  - Backfill on start  │        │   wallet_cache  │
                          │  - Exp backoff 429    │        │   post_price_   │
                          └───────────────────────┘        │   tracking      │
                                                           │                 │
  gamma-api.             ┌───────────────────────┐        └────────┬────────┘
  polymarket.com ───────>│  polymarket.py         │                │
  Polygonscan API        │  - Poll every 30s      │───────>────────┤
                         │  - large_bet           │                │
                         │  - new_wallet          │                │
                         │  - odds_move           │                │
                         └───────────────────────┘                │
                                                                   │
  Alpaca Markets/        ┌───────────────────────┐                │
  Yahoo Finance ────────>│  futures_volume.py     │───────>────────┤
                         │  - Poll every 60s      │                │
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
                                                           ────────┘
                                                                   │
  Alerter (polls SQLite)          Phone                            │
  ─────────────────────        ─────────────                       │
  ┌───────────────────────┐    │           │                       │
  │  alerter.py           │<──<│  SQLite   │<──────────────────────┘
  │  - Poll every 2s      │    │  alerted=0│
  │  - Rate limit 5 min   │    └───────────┘
  │  - Quiet hours UTC    │
  │  - Daily digest       │───────> ntfy.sh ──────> iPhone/Android
  │  - Priority format    │         (push notification)
  └───────────────────────┘

  Dashboard (Flask)
  ─────────────────
  ┌───────────────────────┐
  │  dashboard/app.py     │<─── SQLite (read-only)
  │  /                    │
  │  /signals             │──── http://localhost:5000
  │  /truth               │     (LAN only, no auth)
  │  /polymarket          │
  │  /health              │
  │  HTMX auto-refresh    │
  │  Times in AEST        │
  └───────────────────────┘

  Process Management
  ──────────────────
  systemd services:
    sentinel-truth.service      → truth_social.py
    sentinel-polymarket.service → polymarket.py
    sentinel-futures.service    → futures_volume.py
    sentinel-alerter.service    → alerter.py + correlation_detector.py (thread)
    sentinel-dashboard.service  → dashboard/app.py
```

## Data Flow

1. **Collector polls** external API on its configured interval
2. **Signal threshold crossed** → collector calls `db.insert_signal()`
3. **Signal written** to `signals` table with `alerted=0`
4. **Alerter polls** SQLite every 2 seconds for `alerted=0` records
5. **Rate limit + quiet hours** checks applied
6. **ntfy POST** sent with formatted title + body
7. **Signal marked** `alerted=1` after successful send
8. **Dashboard reads** from SQLite for display (independent of alerter)

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Event bus | No Redis — SQLite polling | Simpler, at-least-once delivery, no extra dependency |
| Concurrency | Synchronous + `time.sleep()` | Predictable, debuggable, no async complexity |
| Alert times | UTC internally, AEST in UI | Single source of truth; display conversion at boundary |
| Correlation | Pure SQL query | Zero extra code, uses existing signals table |
| Wallet age | HTTP to Polygonscan (optional) | Avoids 20MB web3 dependency |
| Data source | Alpaca primary, yfinance fallback | Alpaca: real-time; yfinance: 10-20min delay on futures |

## File Structure

```
Sentinel/
├── sentinel/
│   ├── core/
│   │   ├── db.py           # SQLite access layer (Database, StateStore, WalletCache, PostPriceTracking)
│   │   └── config.py       # Config loader (typed dataclasses, validation)
│   ├── collectors/
│   │   ├── truth_social.py           # Truth Social Mastodon API poller
│   │   ├── polymarket.py             # Polymarket gamma API poller
│   │   ├── futures_volume.py         # Alpaca/yfinance futures volume poller
│   │   ├── correlation_detector.py   # SQL-based multi-source correlation
│   │   ├── truth_social_runner.py    # systemd entry point
│   │   ├── polymarket_runner.py      # systemd entry point
│   │   └── futures_runner.py         # systemd entry point
│   ├── dispatcher/
│   │   ├── alerter.py                # ntfy dispatcher + rate limiter + quiet hours
│   │   └── alerter_runner.py         # systemd entry point
│   ├── dashboard/
│   │   └── app.py                    # Flask dashboard (inline templates)
│   └── scripts/
│       ├── init_db.py                # Database initialiser
│       ├── healthcheck.py            # Cron health checker
│       └── test_alert.py             # Smoke test ntfy
├── tests/
│   ├── unit/                         # Mocked HTTP tests (pytest + responses)
│   └── integration/                  # Real SQLite temp DB tests
├── systemd/                          # systemd service files
├── docs/
│   └── architecture.md              # This file
├── config.yaml.example              # Template config (commit this)
├── config.yaml                      # Real config (gitignored)
├── requirements.txt
├── requirements-dev.txt
└── pyproject.toml
```

## Signal Schema

Each signal record captures:
- `source`: which collector fired (truth_social | polymarket | futures_* | correlation_detector)
- `signal_type`: event type (new_post | large_bet | new_wallet | odds_move | volume_spike | correlated_signal)
- `priority`: CRITICAL | HIGH | MEDIUM | LOW | INFO
- `payload`: full JSON from collector (post text, trade amounts, volume ratios, etc.)
- `summary`: one-line human-readable description
- `alerted`: 0/1 (whether ntfy notification was sent)
- `created_at`: UTC ISO8601 timestamp

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
