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

yfinance is the sole data source for futures (1-min bars, ~10min delay). Alpaca does not support futures — its data API rejects futures symbols. Alpaca config and code remain in the collector for a potential stock/ETF monitoring pivot. Roll-date suppression is configured as a list of dates in `config.yaml` and checked on every poll cycle. Per-instrument `min_absolute_volume` floors prevent false positives on thin overnight sessions. DX-Y.NYB has zero intraday volume — may need daily-only monitoring or dropping.

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

Full suite is green (`pytest`): 583 passed, 10 skipped. The skipped tests are non-hermetic `test_polymarket.py` cases that would otherwise try to reach the live API (DNS-blocked here since Polymarket is ACMA-blocked in Australia).

**Harness wiring** (`.claude/harness.json`, added 2026-08-03): lint/typecheck/test/mutation are bound to the code-build-harness gates; `gate-ci --force` is green (lint + typecheck + test all pass). Lint fix history: `ruff --fix` handled 259 of an original 326 mechanically (import sorting/unused imports/`Optional[X]`→`X | None`/`timezone.utc`→`UTC`); the remaining 67 (mostly E402 in `*_runner.py` entrypoints where `sys.path.insert` must precede the import it enables, E501 in `dashboard/app.py`'s embedded HTML/CSS template, and a few genuine F841/SIM/B904 cases) were fixed by hand — see git history same day for the diffs. The two real mypy gaps (`polymarket.py`/`kalshi.py` odds-move arithmetic) were narrow type gaps, not bugs: `_is_odds_move()` already returns `False` when `previous is None`, so an `assert previous is not None` right after that guard documents the invariant mypy can't infer across the function-call boundary. **Gotcha hit and fixed**: `ruff check .` initially also swept mutmut's `mutants/` working-copy directory (not gitignored, not in ruff's default excludes) — inflated the count to 13k+ and autofixed files inside it mid-run. Fixed via `extend-exclude = ["mutants"]` in `pyproject.toml` and `mutants/` in `.gitignore`; if `mutants/` is ever renamed/moved, check both still match. `sentinel/scripts/mutation_gate.py` first-baseline score is 27.5% (killed=1433, survived=1856, no_tests=1919, total=5208) with `MIN_SCORE` deliberately left at 0.0 pending a real look at what's uncovered — never lower a threshold once set, to pass. **Mutation coverage pass (2026-08-03)**: after `do_not_mutate` excluded thin `*_runner.py`/`scripts/*` entrypoints and a round of targeted tests per module (prioritised by survivor count and business-logic weight — correlation_detector, config, alerter, db, kalshi, truth_social, futures_volume, dashboard/app; `polymarket.py` skipped as ACMA-blocked/deprecated), score went 32.2% → 52.5% (killed=2341, survived=943, no_tests=1171, total=4455). Per-module before→after: correlation_detector 47%→98% (real logic only; the `run()` loop is deliberately untested, same as `*_runner.py`), config 46%→93%, alerter ~30%→67%, db 52%→56%, kalshi 42%→54%, truth_social 46%→55%, futures_volume 23%→35%, dashboard/app's `_to_aest`/`_enrich_signal` fully covered. Remaining `no_tests` is mostly other `run()`-style main loops and I/O-boundary code (`_fetch_alpaca`/`_fetch_yfinance`, the Truth Social Playwright client) left deliberately untested at that boundary. One real bug was found and fixed along the way: `alerter.dispatch_signal`'s quiet-hours-suppressed branch returned `True` instead of `False` and no test caught it (only checked that ntfy wasn't called, never the return value) — `poll_once()`'s dispatched-count would have silently over-counted suppressed signals. A likely-dead-code path was found and documented (not changed): `futures_volume.process_instrument`'s `LOW` priority branch appears unreachable given the `spike_multiplier_quiet >= spike_multiplier` invariant — flagged in `test_futures_volume.py` for a human to confirm intent.

## PDF Processing
When extracting data from PDFs to CSV, read and process PDFs one at a time to minimize token usage. Save intermediate results after each PDF so progress isn't lost if the session is interrupted. Always confirm the output CSV format with the user before processing multiple files.
## Testing & Deployment
Always run the full test suite (`npm test` or equivalent) after multi-file changes before committing. Use TDD approach when adding new features — write tests first, then implement. After successful tests, commit and push unless told otherwise.
## Commit Workflow
After completing a feature or fix, always: 1) run typecheck/lint, 2) run tests, 3) commit with a descriptive message, 4) push to remote. Do not wait to be asked for each step.