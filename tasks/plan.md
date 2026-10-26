# Implementation Plan: Simulated Paper-Trading Ledger

Status: **draft for review** (2026-10-24). No code written yet.

## Overview

A forward-only, broker-free paper ledger inside Sentinel. When a futures
`volume_spike` or an ASX `price_move` signal fires, the ledger opens a
hypothetical position that **fades** the signal bar's move. It closes the
position after a fixed 60-minute horizon and records the P&L net of realistic
costs. The goal is to find out whether the mild mean-reversion seen in
historical data (2026-10-24 directional test) survives trading costs on
forward data before anyone builds the much larger rotrade integration. Rotrade
is not touched.

## Pre-registration (fixed before any forward data exists)

The rule is fixed now and not tuned afterwards. Changing it means starting a
new strategy name and a fresh sample.

- **Strategy `fade60`:** direction = −sign(signal bar's % change). Entry is the
  first price the ledger fetches after the signal. Exit is the first fetched
  price at least 60 minutes after entry. Size is 1 contract (futures) or a fixed
  notional (ASX).
- **Primary hypothesis:** `futures_gold` `fade60` has mean net return > 0.
  Gold is the only instrument whose historical edge (~0.05–0.11%) cleared
  estimated costs. Every other instrument is a secondary hypothesis.
- **Evaluation:** the earlier of 6 weeks (2026-12-05) or 150 closed gold
  trades. Pass = mean net return > 0 **and** the bootstrap 95% CI excludes 0.
  Anything else is a fail, and the result is recorded either way.
- Historical (pre-start) signals are never opened, so nothing in the
  evaluation is in-sample.

## Architecture Decisions

- **It runs inside `price_followup.py`'s loop, not as a new service.** That
  loop already holds the IB→yfinance fetchers (futures and ASX), runs every
  5 minutes and is supervised by systemd. A new service would mean another unit
  that can go stale, and that has happened three times. The ledger step gets
  its own `try/except` so a ledger bug can't break price backfill.
- **Trades are opened from `signals`, not `post_price_tracking`.** Only HIGH
  signals get tracked, but half the futures spikes and 43 of 47 recent ASX
  `price_move` signals are MEDIUM. The ledger reads the bar direction straight
  from the signal payload (`price_change_pct` for futures, `change_pct` for
  ASX).
- **Entry is the first price fetched after the signal, not the payload
  price.** The payload price is the trigger bar's close. On yfinance fallback
  that bar is 10–20 minutes old, so filling at it would be look-ahead. The
  payload price is stored as `signal_price`, so the cost of the delay stays
  visible.
- **One open position per instrument.** Spikes cluster, and stacking
  overlapping trades would inflate n and the apparent significance. A new
  signal on an instrument that already has an open trade is recorded as
  `skipped` with a reason, not silently dropped.
- **A forward-only cursor.** A `paper_ledger:start_at` key in `db.state` is set
  on first run, and signals created before it are ignored.
- **Costs are config, per instrument.** The defaults assume IBKR micro
  futures and an ASX fixed commission with its minimum, and are stored on each
  trade (`cost_ret`) so the report can show gross and net side by side.
- **Trades that can't be priced expire.** If no exit price is available
  within 30 minutes of the exit being due (market closed, IB down), the trade
  is marked `expired` and excluded from P&L, so it never closes at a stale
  price. ASX trades are not opened if the exit would fall after the 16:00
  Sydney close.

## Dependency Graph

```
config `paper:` section + cost model (pure)
        │
paper_trades table + db.paper_trades accessor
        │
open_trades()  ──────────┐
        │                │
close_due_trades()       │  (both pure-ish, fake fetcher in tests)
        │                │
wire into price_followup loop + deploy (restart service)
        │
        ├── ASX price_move rules (session cutoff, ASX costs)
        ├── paper_ledger_report.py (gross/net, bootstrap CI)
        └── healthcheck "ledger stalled" check + docs
```

## Task List

### Phase 1: Futures fade, end to end

#### Task 1: Config section and cost model
**Description:** Add a `paper:` config section: enabled flag, strategy name,
horizon in minutes, expiry grace period, the signal types it applies to, and
per-instrument cost entries (tick size, multiplier, commission per side,
slippage ticks, or notional for ASX). Add a pure function
`round_trip_cost_ret(cost, price)` that returns the cost as a fraction of
entry price.

**Acceptance criteria:**
- [ ] `load_config` parses `paper:` into a typed `PaperConfig`. If the section
  is missing, the ledger is disabled and existing configs still load.
- [ ] `round_trip_cost_ret` is correct for a micro-futures cost (commission and
  slippage ticks) and for an ASX cost (a percentage commission with an AUD
  minimum).

**Verification:**
- [ ] `pytest tests/unit/test_config.py tests/unit/test_paper_ledger.py`
- [ ] `node ~/dev/newdev/code-build-harness/harness/gates/ci.mjs --force`

**Dependencies:** None
**Files likely touched:** `sentinel/core/config.py`, `sentinel/paper/ledger.py`
(new), `tests/unit/test_config.py`, `tests/unit/test_paper_ledger.py` (new),
`config.yaml` (gitignored; also add the section to any example config)
**Estimated scope:** S

#### Task 2: Open fade trades from new futures signals
**Description:** Add the `paper_trades` table and a `db.paper_trades`
accessor following the existing `PriceSamples` pattern. Its columns are id,
signal_id (unique), strategy, source, instrument, direction, signal_price,
entry_price, entry_at, exit_price, exit_at, gross_ret, cost_ret, net_ret,
status (open/closed/expired/skipped) and note. Add
`open_trades(db, cfg, fetcher, now)`. It reads qualifying signals created
after the forward cursor, works out direction = −sign(bar % change), and does
one of three things: opens a trade at the fetched price, records `skipped`
(an open position already exists on that instrument, or the change is 0), or
leaves the signal for the next pass if there's no price yet.

**Acceptance criteria:**
- [ ] A new futures `volume_spike` with a positive bar change opens a SHORT
  (−1) at the fetched price, and a negative change opens a LONG.
- [ ] Signals from before the cursor are never opened, and a signal is never
  opened twice. Running `open_trades` twice is a no-op the second time.
- [ ] A second signal on an instrument that already has an open trade is
  recorded `skipped`, with a note.

**Verification:**
- [ ] `pytest tests/unit/test_paper_ledger.py` (tmp_path DB and a fake fetcher,
  the same pattern as `test_price_followup.py`)
- [ ] CI gate green

**Dependencies:** Task 1
**Files likely touched:** `sentinel/core/db.py`, `sentinel/paper/ledger.py`,
`tests/unit/test_paper_ledger.py`, `tests/unit/test_db.py`
**Estimated scope:** M

#### Task 3: Close due trades with net P&L, and expire unpriceable ones
**Description:** Add `close_due_trades(db, cfg, fetcher, now)`. For each open
trade whose entry_at + horizon ≤ now, it fetches the price once per
instrument (batched, like `run_once`), then records exit_price and exit_at
along with these returns:
- gross_ret = direction × (exit − entry) / entry
- cost_ret, from the Task 1 cost model
- net_ret = gross_ret − cost_ret

A trade still unpriced after the grace period becomes `expired`.

**Acceptance criteria:**
- [ ] A SHORT whose price falls closes with a positive gross return, and its
  net return equals gross minus cost.
- [ ] A trade isn't closed before its horizon, and the recorded exit_at is the
  actual close time, not entry + 60.
- [ ] A trade with no price past the grace period becomes `expired` with
  null P&L.

**Verification:**
- [ ] `pytest tests/unit/test_paper_ledger.py`
- [ ] CI gate green

**Dependencies:** Task 2
**Files likely touched:** `sentinel/paper/ledger.py`,
`tests/unit/test_paper_ledger.py`
**Estimated scope:** S

#### Task 4: Wire into the price-followup loop and deploy
**Description:** Call `close_due_trades` then `open_trades` on every
`price_followup.py` pass (and with `--once`), each in its own `try/except`
with error logging, and log a one-line summary. Then deploy: restart
`sentinel-price-followup.service` (the restart rule in CLAUDE.md) and check
for real rows.

**Acceptance criteria:**
- [ ] An exception in the ledger is logged and doesn't stop backfill or
  baseline sampling in the same pass (covered by a test).
- [ ] With `paper.enabled: false` the ledger does nothing.
- [ ] Live: within 2 hours of a futures spike during market hours, a
  `paper_trades` row goes from `open` to `closed` with a non-null `net_ret`.

**Verification:**
- [ ] `pytest tests/unit/test_price_followup.py tests/unit/test_paper_ledger.py`
- [ ] CI gate green
- [ ] `python sentinel/scripts/price_followup.py --once` runs clean against
  the live DB
- [ ] Manual: query `paper_trades` after the service restart

**Dependencies:** Task 3
**Files likely touched:** `sentinel/scripts/price_followup.py`,
`tests/unit/test_price_followup.py`
**Estimated scope:** S

### Checkpoint: Futures ledger live
- [ ] CI gate green (ruff, mypy, pytest)
- [ ] At least one real futures trade has closed with plausible numbers
  (a sanity check of entry/exit against the IB price)
- [ ] **Human review before Phase 2:** look at the first ~20 live trades.
  Are costs sensible? Is the skip rate reasonable?

### Phase 2: ASX, reporting, monitoring

#### Task 5: ASX price_move trades
**Description:** Extend the ledger to ASX `price_move` signals, using
`change_pct` for direction, the ASX fixed-notional commission model, and a
session rule: don't open if entry + horizon falls after 16:00 Sydney or on a
weekend. This reuses the `in_asx_session` helper from `signal_scorecard.py`;
move it somewhere shared first, such as `sentinel/core/markets.py`.

**Acceptance criteria:**
- [ ] An ASX price_move at 11:00 Sydney opens a trade, while one at 15:30 is
  `skipped` with the note "exit after close".
- [ ] ASX net return includes the commission minimum. At the default
  notional, cost_ret is at least 0.24% round trip.

**Verification:**
- [ ] `pytest tests/unit/test_paper_ledger.py tests/unit/test_signal_scorecard.py`
- [ ] CI gate green; restart `sentinel-price-followup.service`

**Dependencies:** Task 4
**Files likely touched:** `sentinel/paper/ledger.py`,
`sentinel/scripts/signal_scorecard.py`, `sentinel/core/markets.py` (new),
tests
**Estimated scope:** M

#### Task 6: Ledger report
**Description:** Add `sentinel/scripts/paper_ledger_report.py`. For each
(strategy, source, signal_type) it shows closed/open/expired/skipped counts,
hit rate, mean and median gross and net return, cumulative net return, and a
bootstrap 95% CI on mean net return (numpy, seeded). It also prints the
pre-registered verdict for the primary hypothesis: PASS, FAIL or "not yet
evaluable" with n and the days remaining. Exit code is always 0, matching
`signal_scorecard.py`.

**Acceptance criteria:**
- [ ] On a fixture ledger, all stats match hand-computed values, and the CI
  is reproducible (seeded).
- [ ] The verdict logic follows the pre-registration exactly: n ≥ 150 or the
  date reached, mean > 0 and the CI lower bound > 0.

**Verification:**
- [ ] `pytest tests/unit/test_paper_ledger_report.py`
- [ ] Manual: run against the live DB

**Dependencies:** Task 3 (it can be built in parallel with Tasks 4–5 on
fixture data)
**Files likely touched:** `sentinel/scripts/paper_ledger_report.py` (new),
`tests/unit/test_paper_ledger_report.py` (new)
**Estimated scope:** S

#### Task 7: Healthcheck "ledger stalled" check and docs
**Description:** Silent failure is this project's recurring pattern, so add a
healthcheck failure for an open paper trade more than 2 hours past due, which
means the ledger step is stuck. Document the ledger in CLAUDE.md (a short
section linking here) and record the pre-registration and evaluation date in
memory.

**Acceptance criteria:**
- [ ] The healthcheck fails and names the trade when an open trade is more
  than 2 hours past due, and passes otherwise.
- [ ] CLAUDE.md stays under 200 lines.

**Verification:**
- [ ] `pytest tests/unit/test_healthcheck.py`
- [ ] CI gate green; `python sentinel/scripts/healthcheck.py` shows the new
  line

**Dependencies:** Task 4
**Files likely touched:** `sentinel/scripts/healthcheck.py`,
`tests/unit/test_healthcheck.py`, `CLAUDE.md`
**Estimated scope:** S

### Checkpoint: Complete
- [ ] CI gate green; `python sentinel/scripts/mutation_gate.py` run on the
  new module, with surviving mutants in `ledger.py`'s P&L math addressed
- [ ] The report runs on live data
- [ ] Calendar reminder for evaluation on 2026-12-05 (or when n = 150)

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| The 5-min loop cadence adds up to 5 min of entry delay and dulls a 60-min edge | Med | `signal_price` is stored, so the delay cost is measured. If it's material, run the ledger step on a 1-min sub-loop later, after the evaluation, as a new strategy name |
| yfinance fallback makes fills stale | Med | Store which source priced entry and exit (if the fetcher exposes it cheaply), and the healthcheck already flags IB fallback |
| Cost defaults are wrong | High (it decides the verdict) | Open question 2. Costs are stored per trade, so the report can recompute net under corrected costs without new data |
| Peeking and re-tuning mid-sample | High | Pre-registration above. Any rule change uses a new strategy name and a fresh sample |
| IB Gateway desupport on 2026-12-15 lands near the evaluation date | Med | The evaluation date falls before it; the Gateway upgrade is tracked separately |
| Overlapping or clustered trades overstate significance | Med | One position per instrument; skipped trades are counted and reported |

## Open Questions

1. **Horizon:** 60 minutes only, or also log a 240-minute variant as a
   separate pre-registered strategy? Gold's historical edge was larger at
   t240 and t1440. It's cheap to add now and can't be added honestly later.
2. **Costs:** OK to start with these defaults and refine from your IBKR
   statements?
   - Micro futures: about USD 0.62 per side, plus 1 tick of slippage per side.
     Contracts are MGC (gold), MCL (oil), MES (S&P), the NYMEX micro Henry Hub
     for natgas, and the full DX contract for the dollar index (there's no
     micro).
   - ASX: 0.08% with an AUD 6 minimum, on a $5,000 notional.
3. **DX-Y.NYB:** include it? Only the full-size ICE contract exists, which
   needs about USD 100k notional, so the result has no practical meaning at
   your account size.
4. **ASX at all?** At a $5,000 notional the commission minimum (about 0.24%
   round trip) is roughly half the typical `price_move` follow-through, and
   there are only about 47 signals a month. Keep it as a secondary
   measurement, or drop Task 5?
