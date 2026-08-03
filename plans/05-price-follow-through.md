# Plan 05 — Price Follow-Through (real signal-to-noise measurement)

**Goal:** replace the burst/correlation heuristics in `signal_diagnostics.py`
(cheap, but proxies) with an actual event-study measurement: does the
instrument's price move more after a HIGH/CRITICAL signal than it does in a
random baseline window? That's a defensible signal-to-noise number instead of
a guess.

**Why now:** the schema already exists and is unit-tested but was never
wired to anything — `post_price_tracking` table + `db.price_tracking`
(`PostPriceTracking` in `sentinel/core/db.py:134-179`), with `insert()`,
`update_price()`, `get_pending_updates()` all present. This plan is "wire it
up," not "design it."

## Scope

- Track HIGH/CRITICAL signals only, to start (LOW/MEDIUM triples the API
  load for signal types that are already lower-priority by design).
- Sources: `kalshi` (all three signal types), `futures_*` (volume_spike).
  Exclude `correlation_detector` (its signals are derived from other
  signals, not a primary source — tracking its own follow-through is
  circular) and `polymarket` (deprecated/ACMA-blocked, per `CLAUDE.md`).
- `truth_social`: needs a keyword-category → instrument mapping (e.g.
  tariff/trade-war keywords → `futures_oil`/`ES=F`) since a post itself
  has no instrument. Punt this to a follow-up — start with the two
  sources (`kalshi`, `futures_*`) that already carry an instrument/ticker
  in their payload.

## Data model (already exists, no schema change needed)

```
post_price_tracking(signal_id, source, instrument,
                     price_t0, price_t15, price_t60, price_t240, price_t1440,
                     created_at)
```

## New components

1. **Snapshot at signal time** — in `kalshi.py`/`futures_volume.py`, right
   after `insert_signal()` for a HIGH/CRITICAL signal, call
   `db.price_tracking.insert(signal_id, source, instrument, price_t0=...)`.
   The current price is already on hand in both collectors (it's what
   triggered the signal) — no extra fetch needed for `t0`.

2. **Backfill scheduler** (`sentinel/scripts/price_followup.py`, new) —
   runs on a timer (every 5–10 min is enough given the coarsest horizon is
   +15 min):
   - Call `db.price_tracking.get_pending_updates()`.
   - For each row where `created_at + N minutes` has passed and `price_tN`
     is still `NULL`, fetch the current price for `instrument` (reuse
     `KalshiCollector.fetch_market()` / the yfinance path in
     `futures_volume.py` — batch by unique instrument per run, not per
     row, to avoid redundant fetches) and call `update_price()`.
   - Horizons: 15/60/240/1440 minutes, matching the existing columns.

3. **Scorecard** (extend `signal_diagnostics.py` or a new
   `signal_scorecard.py`) — once `price_t60`+ data exists for a reasonable
   sample:
   - Per `(source, signal_type)`: distribution of
     `abs(price_tN - price_t0) / price_t0` at each horizon.
   - Compare against a baseline: the same calculation for random
     (non-signal-triggered) windows on the same instrument, same time-of-day
     distribution. Signals that don't beat baseline are noise by this
     measure, regardless of how "big" the raw trade/spike was.

## Known trap: don't backfill pre-fix history

The DB has three known-bad code eras baked into old rows: the 2026-06-02
pre-`551e1dd` Truth Social always-CRITICAL flood, the pre-`dd6d2ee`
(2026-06-10) Kalshi volume_spike missing-dedup flood, and the pre-`d23eb71`
(2026-08-03) Kalshi large_bet cold-start flood. None of that is worth
spending API calls on. Scope price-tracking to signals created **after**
this plan ships — don't backfill the historical `signals` table.

## Testing

- `price_followup.py`: mock the price-fetch client (protocol-based, same
  pattern as `truth_social.py`'s `TruthSocialClientProtocol`), verify the
  right column gets updated at the right age threshold, verify batching
  (one fetch per unique instrument per run, not per pending row).
- Scorecard math: synthetic `price_t0`/`price_tN` fixtures, verify percent-move
  and baseline-comparison arithmetic. No live network in tests, per existing
  convention.

## Deployment

Add a `sentinel-price-followup.service` (+ timer or internal sleep loop) to
`deploy/systemd/`, following the pattern in `plans/04-systemd-supervision.md`.
Lightweight — no browser, no continuous polling loop of its own beyond the
backfill cadence.

## Done criteria

`post_price_tracking` populates automatically for new HIGH/CRITICAL
`kalshi`/`futures_*` signals; a scheduled job fills in t15/t60/t240/t1440;
a scorecard script produces a real per-signal-type effect-size number;
`gate-ci --force` green; a short note added to `CLAUDE.md` describing the
pipeline once it's live.
