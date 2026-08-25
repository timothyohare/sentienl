# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
Let's keep to under 200 lines.

## Common commands

```bash
# Virtual environment (required)
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium            # required for Truth Social collector
pip install -r requirements-dev.txt   # adds pytest, coverage, ruff, mypy, mutmut

# Quality gates (see ~/.claude/CLAUDE.md for the harness contract this binds to)
node ~/dev/newdev/code-build-harness/harness/gates/ci.mjs --force  # lint + typecheck + tests
python sentinel/scripts/mutation_gate.py                           # mutation score (mutmut)

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
python -m sentinel.collectors.asx_runner
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
2. **Alerter** (`sentinel/dispatcher/alerter.py`) polls `signals WHERE alerted=0` every 2 seconds, applies quiet-hours / rate-limit logic, sends ntfy HTTP requests, then calls `db.mark_alerted(id)`. `CRITICAL` signals bypass both rate limiting and quiet hours — including Truth Social posts that classify as `CRITICAL` (see Signal priorities). `alerts.enabled` in config.yaml (default `true`) gates the actual ntfy send: when `false`, `send_ntfy()` no-ops (still marks signals alerted, so bookkeeping stays consistent) — this is the **data-collection-only mode** used for backtesting, since collectors and the correlation detector keep writing to the DB regardless of this flag.
3. **Correlation detector** (`sentinel/collectors/correlation_detector.py`) runs a SQL self-join query every 5 minutes looking for HIGH/CRITICAL events from 2+ distinct sources within any 10-minute window. If found, it inserts a CRITICAL `correlated_signal` — which the alerter then dispatches normally.
4. **Dashboard** (`sentinel/dashboard/app.py`) is a read-only Flask app that queries the signals table directly.

### Core modules

- `sentinel/core/config.py` — `load_config(path)` returns a typed `Config` dataclass. All times are parsed to `datetime.time` UTC. `is_in_window(now_utc, start, end)` handles midnight-crossing windows (e.g. 23:00–04:00).
- `sentinel/core/db.py` — `Database` class with sub-accessors: `db.state` (StateStore), `db.wallet_cache` (WalletCache), `db.price_tracking` (PostPriceTracking), `db.price_samples` (PriceSamples — continuous baseline history, see Price follow-through below). WAL mode is enabled on every `db.init()`. The `payload` column is JSON; `get_unalerted_signals()` and related methods deserialise it automatically.

### Signal priorities

`INFO < LOW < MEDIUM < HIGH < CRITICAL`

Quiet-hours suppression (`quiet_suppress_below` in config) applies to signals below the configured level.

Truth Social posts are priority-tiered by `classify_priority()` in `truth_social.py` (not blanket-`CRITICAL`): any market-moving keyword → `CRITICAL`; routine endorsement language with no keyword → `LOW`; everything else (incl. media-only posts) → the configured default (`MEDIUM`). Only the `CRITICAL` (keyword-matched) posts bypass rate limiting and quiet hours; `LOW`/`MEDIUM` Truth Social posts are subject to normal suppression. This tiering (commit `551e1dd`) replaced the old always-`CRITICAL` behaviour, which flooded the alerter with routine candidate endorsements and anchored false correlations.

### Truth Social collector (Playwright)

Cloudflare blocks all direct HTTP requests to truthsocial.com from this machine. The collector uses Playwright headless Chromium to bypass this:
- `truth_social_client.py` — browser client that navigates to the site, logs in via the web UI modal, and makes API calls via in-browser `page.evaluate(fetch(...))`.
- `truth_social.py` — collector logic with a pluggable `TruthSocialClientProtocol`. In production this is the Playwright client; in tests it's a mock.
- Credentials come from `.env` (username/password) or `TS_USERNAME`/`TS_PASSWORD` env vars.
- The bearer token is stored in `localStorage['truth:auth']` after browser login.
- The real OAuth endpoint is `/oauth/v2/token` (not the standard Mastodon `/oauth/token`).

### Kalshi collector specifics

Kalshi (`sentinel/collectors/kalshi.py`) is the primary prediction market data source, replacing Polymarket which is blocked in Australia by ACMA. It polls the public Kalshi REST API (`https://external-api.kalshi.com/trade-api/v2`) — no authentication required for read-only market and trade data.

Signal types: `large_bet` (always HIGH), `odds_move`, `volume_spike`. The latter two are tiered (2026-08-10): MEDIUM by default, HIGH once the move/ratio crosses `odds_move_pct_high`/`volume_spike_multiplier_high` in config — same pattern as `futures_volume.py`'s `spike_multiplier`/`spike_multiplier_quiet`. Defaults (15.0pp / 50.0x) were picked from the live DB's ~90th percentile so tiering flags a real minority as HIGH, not most of the volume. No `new_wallet` equivalent (Kalshi is KYC'd). Volume spike baseline is calculated as lifetime volume divided by market age in days. Config section is `kalshi:` with `tracked_event_tickers` as a list of Kalshi event tickers.

### Polymarket collector (blocked in Australia)

Polymarket (`sentinel/collectors/polymarket.py`) was the original prediction market source but is now classified as an illegal online gambling service in Australia under the Interactive Gambling Act 2001 (ACMA block). The collector code remains in the codebase but should not be relied upon from Australian infrastructure. Use the Kalshi collector instead.

### Futures collector specifics

IB Gateway is the default data source as of 2026-08-12 (`futures.ib_enabled: true`, paper account, 127.0.0.1:4002) — real-time, no ~10min delay, and real per-minute exchange volume. `fetch_bars()` tries Alpaca (dead — its API rejects futures symbols; config/code kept for a potential stock/ETF pivot) → IB Gateway → yfinance, so yfinance is now the fallback, used automatically whenever Gateway isn't running/logged in (no code change needed, just silently degrades). Gateway requires a manual daily login — no auto-relogin infrastructure exists. Roll-date suppression is configured as a list of dates in `config.yaml` and checked on every poll cycle. Per-instrument `min_absolute_volume` floors prevent false positives on thin overnight sessions. DX-Y.NYB's zero-intraday-volume issue is fixed by IB Gateway — confirmed 2026-08-23, real per-minute volume (e.g. 7, 55 contracts/bar) now flows through `sentinel-futures.service`'s live `fetch_bars()` path.

**The "~19% yfinance fetch failure rate" flagged 2026-08-12 turned out to be a non-issue** — investigated further same day before switching to IB: journalctl showed CL=F failures were 1302/2610 (50%) on a single day, Aug 8 2026, a Saturday. Cross-checking yfinance daily bars confirmed Aug 8-9 have no trading data at all (weekend), and a 5-day 1-min pull showed the only real gaps were the expected weekly close/reopen and each day's ~1hr CME maintenance break — the collector polls every 60s regardless of market hours, so it logs a WARNING every single minute the market is legitimately closed. Weekday-only failure rate (Aug 11-12) was <1%. No evidence any real spike was missed — historical max-volume bars those days (up to ~8700 contracts) were well above both the old and new `min_absolute_volume` floors. IB Gateway doesn't change this dynamic (market-closed is market-closed for any vendor); the switch was made for latency/data-quality reasons, not to fix this.

**`min_absolute_volume` must be set relative to yfinance's actual reported volume, not real exchange volume** (2026-08-12): CL=F (oil) had fired zero signals ever despite 9 days of collector uptime. Root cause was the floor (500), set from real WTI NYMEX daily volume assumptions, while yfinance's delayed 1-min bar feed for CL=F reports only ~98 contracts/bar on average — a 5.1x floor/mean ratio, so only ~2% of bars could ever clear it. Compare GC=F's 1.4x ratio (fires normally, 6 signals). Lowered CL=F to 150 and NG=F to 55 (both ~1.5x mean, matching GC=F) — NG=F had the identical problem (5.34x ratio, only 1 signal in 9 days). `DX-Y.NYB` is a separate, unfixable-by-threshold issue: yfinance reports **zero volume on every bar**, always — not a calibration problem. Checked Kalshi's `odds_move_pct_high`/`volume_spike_multiplier_high` (2026-08-12) too: only 2 days of data since the 2026-08-10 tiering shipped, closest approach was 13.0pp/15.0pp and 33.4x/50.0x — plausibly fine, not enough evidence yet to retune. If a tracked instrument goes suspiciously quiet, check `floor / mean(yfinance 1-min volume)` before assuming the collector itself is broken.

**`sentinel-price-followup.service` was running on a stale config for 13 days, and yfinance's futures feed has since broken outright** (found 2026-08-23): `price_followup.py`'s own `FuturesPriceFetcher` (used for the t15/t60/t240/t1440 backfill and the `price_samples` random-baseline snapshots — a separate code path from `futures_volume.py`'s collector) never picked up `ib_enabled: true`, because the service process has been running continuously since 2026-08-10 20:48 AEST, before that flag was flipped live on 2026-08-12 — `sentinel-futures.service` got restarted that day (per the doc above) but `sentinel-price-followup` didn't, so it's been silently going straight to yfinance the whole time (the "restart every service you touch" rule from the price-follow-through section below applies here too). Separately, yfinance itself started throwing `possibly delisted; no price data found` for **all five** tracked futures tickers (not just DX-Y.NYB) beginning ~2026-08-23T03:58 UTC — a harder failure than the old "zero volume, but a price" state. Net effect: no new `price_samples` baseline rows and no t15+ backfill for any futures signal since that timestamp, until `sentinel-price-followup.service` is restarted onto the current config.

### ASX collector specifics

`sentinel/collectors/asx.py` (added 2026-08-23) — a general-purpose large-cap equity watchlist (BHP/CBA/CSL/FMG/WBC/NAB/RIO/WES/MQG/WOW, `.AX` tickers), not tied to any one geopolitical theme; this is a deliberate scope extension beyond the PRD's original futures/prediction-market/Truth-Social remit (no equities non-goal is actually written in `sentinel_prd.md`, so this doesn't violate a stated constraint — just extends it). Structurally a near-clone of `futures_volume.py`: same rolling-average volume-spike detection (imported from there rather than duplicated — genuinely the same algorithm, not just similar), plus a new `price_move` signal type (bar-close-to-previous-bar-close %, mirroring `kalshi.py`'s `odds_move`) since equities react to news via price gaps more than futures do. Source is a single flat `"asx"` (like `kalshi`, not per-instrument like `futures_*`). `min_absolute_volume` per ticker was calibrated 2026-08-23 from real 5-day yfinance 1-min volume history (~60th percentile of nonzero bars, not a guess) — ASX per-minute volume is heavily right-skewed by open/close auction prints, so a mean-based floor (the futures convention) would sit above nearly every intraday bar. Wired into `price_followup.py` via a new `AsxPriceFetcher` (same IB-then-yfinance pattern as `FuturesPriceFetcher`) so HIGH-priority signals get the same t15/t60/t240/t1440 backfill as kalshi/futures.

**IB Gateway ASX entitlement confirmed 2026-08-26**: `reqHistoricalData` for BHP.AX (STK/ASX/AUD) returned 60 real 1-min bars, only benign "data farm connection OK" callbacks (2104/2106/2158) — no entitlement error (354/10197/200). `asx.ib_enabled` is now `true` (real-time, same as futures). `sentinel-asx.service` is enabled and running.

**Found the same day: IB Gateway had been silently stuck for 2 days** (since 2026-08-24T05:00 AEST) on an `NS_AUTH_START` failure — IBKR's routine security-token-expiry event ("please manually enter your username and password"), which IBC's stored-credential auto-login can't clear itself. Port 4002 wasn't listening at all; `sentinel-futures.service` and `sentinel-price-followup.service` were both silently degraded to yfinance-only that whole time (same failure class as the Aug 10-23 stale-price-followup incident above, but this time the root cause was Gateway itself). Fix was a plain `systemctl --user restart ibgateway.service` — this time a restart re-sent fresh credentials rather than the stale cached token and passed; not guaranteed to always work (a future hit on this could require actual GUI/2FA interaction). Futures/price-followup self-healed on their next poll (they open an IB connection per-call, not a held-open one) — no restart needed for them. **Gap**: nothing currently monitors "IB Gateway process is up but port 4002 isn't listening" or "collector silently fell back to yfinance for N consecutive polls" — this class of outage can run indefinitely unnoticed. Worth a healthcheck addition.

### Price follow-through (plans/05-price-follow-through.md)

Real event-study measurement of signal-to-noise, replacing the burst/correlation proxies in `signal_diagnostics.py`. `kalshi.py`/`futures_volume.py` snapshot `price_t0` into `db.price_tracking` (`post_price_tracking` table) for HIGH/CRITICAL signals only, at signal-insert time — no extra fetch needed since the triggering price is already on hand. `sentinel/scripts/price_followup.py` runs on a timer (default 5 min; `--once` for a single pass) and backfills `price_t15/t60/t240/t1440` once each horizon has elapsed, batching fetches so each `(source, instrument)` pair hits the network at most once per run regardless of how many pending rows/columns reference it. `sentinel/scripts/signal_scorecard.py` then computes per-`(source, signal_type, horizon)` effect sizes. `truth_social` is not tracked yet (no instrument on the payload — needs a keyword→instrument mapping, punted to a follow-up). Deployed as `sentinel-price-followup.service` in `deploy/systemd/`.

**True random-window baseline (2026-08-10)** — the originally-deferred part of plan 05's scope, now built: every `price_followup.py` run also drops a price sample for each distinct tracked instrument into `db.price_samples` (`price_samples` table), independent of whether a signal fired that run. `signal_scorecard.py`'s `compute_random_baseline()` pairs those samples up within each horizon (±20% tolerance) to get a real "how much does this instrument move over N minutes when nothing signal-worthy happened" distribution, and `build_scorecard()` prefers it over the old pooled-proxy baseline (median across other tracked signal types — never a true null) per horizon once ≥5 pooled pairs exist. Output lines are tagged `[random]`/`[proxy]` so it's visible which bar was actually cleared; cold-start horizons (t1440 needs ~a day of 5-min sampling) stay `[proxy]` until then.

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

Full suite is green (`pytest`): 655 passed, 10 skipped. The skipped tests are non-hermetic `test_polymarket.py` cases that would otherwise try to reach the live API (DNS-blocked here since Polymarket is ACMA-blocked in Australia).

**Harness wiring** (`.claude/harness.json`, added 2026-08-03): lint/typecheck/test/mutation are bound to the code-build-harness gates; `gate-ci --force` is green (lint + typecheck + test all pass). Lint fix history: `ruff --fix` handled 259 of an original 326 mechanically (import sorting/unused imports/`Optional[X]`→`X | None`/`timezone.utc`→`UTC`); the remaining 67 (mostly E402 in `*_runner.py` entrypoints where `sys.path.insert` must precede the import it enables, E501 in `dashboard/app.py`'s embedded HTML/CSS template, and a few genuine F841/SIM/B904 cases) were fixed by hand — see git history same day for the diffs. The two real mypy gaps (`polymarket.py`/`kalshi.py` odds-move arithmetic) were narrow type gaps, not bugs: `_is_odds_move()` already returns `False` when `previous is None`, so an `assert previous is not None` right after that guard documents the invariant mypy can't infer across the function-call boundary. **Gotcha hit and fixed**: `ruff check .` initially also swept mutmut's `mutants/` working-copy directory (not gitignored, not in ruff's default excludes) — inflated the count to 13k+ and autofixed files inside it mid-run. Fixed via `extend-exclude = ["mutants"]` in `pyproject.toml` and `mutants/` in `.gitignore`; if `mutants/` is ever renamed/moved, check both still match. `sentinel/scripts/mutation_gate.py` first-baseline score is 27.5% (killed=1433, survived=1856, no_tests=1919, total=5208) with `MIN_SCORE` deliberately left at 0.0 pending a real look at what's uncovered — never lower a threshold once set, to pass. **Mutation coverage pass (2026-08-03)**: after `do_not_mutate` excluded thin `*_runner.py`/`scripts/*` entrypoints and a round of targeted tests per module (prioritised by survivor count and business-logic weight — correlation_detector, config, alerter, db, kalshi, truth_social, futures_volume, dashboard/app; `polymarket.py` skipped as ACMA-blocked/deprecated), score went 32.2% → 52.5% (killed=2341, survived=943, no_tests=1171, total=4455). Per-module before→after: correlation_detector 47%→98% (real logic only; the `run()` loop is deliberately untested, same as `*_runner.py`), config 46%→93%, alerter ~30%→67%, db 52%→56%, kalshi 42%→54%, truth_social 46%→55%, futures_volume 23%→35%, dashboard/app's `_to_aest`/`_enrich_signal` fully covered. Remaining `no_tests` is mostly other `run()`-style main loops and I/O-boundary code (`_fetch_alpaca`/`_fetch_yfinance`, the Truth Social Playwright client) left deliberately untested at that boundary. One real bug was found and fixed along the way: `alerter.dispatch_signal`'s quiet-hours-suppressed branch returned `True` instead of `False` and no test caught it (only checked that ntfy wasn't called, never the return value) — `poll_once()`'s dispatched-count would have silently over-counted suppressed signals. A likely-dead-code path was found and documented (not changed): `futures_volume.process_instrument`'s `LOW` priority branch appears unreachable given the `spike_multiplier_quiet >= spike_multiplier` invariant — flagged in `test_futures_volume.py` for a human to confirm intent.

## PDF Processing
When extracting data from PDFs to CSV, read and process PDFs one at a time to minimize token usage. Save intermediate results after each PDF so progress isn't lost if the session is interrupted. Always confirm the output CSV format with the user before processing multiple files.
## Testing & Deployment
Always run the full test suite (`npm test` or equivalent) after multi-file changes before committing. Use TDD approach when adding new features — write tests first, then implement. After successful tests, commit and push unless told otherwise.
## Commit Workflow
After completing a feature or fix, always: 1) run typecheck/lint, 2) run tests, 3) commit with a descriptive message, 4) push to remote. Do not wait to be asked for each step.