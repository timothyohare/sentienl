# Sentinel Health & Signal-Quality Analysis — 2026-06-10

> Analysis of `sentinel.db` after the first run. Prompted by the concern that
> "the signals aren't strong enough." The data says the opposite problem:
> thresholds are too *loose* and there's no de-duplication, so real signal got
> buried under thousands of duplicate alerts — and the pipeline has been dead
> for a week.

## 1. Sentinel is not running (and hasn't been for ~7 days)

- **No collector / dispatcher / dashboard processes** are currently running.
- Last signal written: **2026-06-03 20:38 UTC**. DB last modified **June 4 07:07**.
- It ran for only **~33 hours** (Jun 2 11:42 → Jun 3 20:38), then stopped.

The impression of "nothing useful coming through" is partly just silence:
there has been no pipeline at all for a week. Nothing is supervising the
processes (the `healthcheck.py` script exists but isn't wired to restart
anything).

## 2. When it ran, noise drowned the signal

3,463 signals in ~33 hours, and **every single one was alerted** (`alerted=1`
for all). Breakdown:

| Source / type                  | Count | Distinct | Problem                                    |
|--------------------------------|------:|---------:|--------------------------------------------|
| kalshi `volume_spike` (MEDIUM) | 3,116 | **12**   | **260× re-emission** — same 12 markets re-fired every poll |
| kalshi `large_bet` (HIGH)      |   272 | 243      | threshold low; "large" = tiny notional      |
| truth_social `new_post` (CRIT) |    58 | 57       | all fired at once on first run; endorsements marked CRITICAL |
| futures `volume_spike` (HIGH)  |     6+2| ~3 events| same spike re-fired as rolling avg decays   |
| kalshi `odds_move` (MEDIUM)    |     4 | 4        | genuinely useful (all Taiwan)               |
| correlation (CRITICAL)         |     3 | 1        | one event, emitted 3×; **and it was a false positive** |

### The core bug: no de-duplication / cooldown

The `volume_spike` number is the smoking gun: **3,116 signals from only 12
distinct markets — ~260× re-emission each.** Once a market crosses the 5×
threshold it re-alerts on *every poll cycle* (~37s apart) for hours. Same bug
visible in futures: ES=F 2,369 contracts fired 6 times (21:31–21:36) as the
multiplier mechanically decayed 20× → 10× → 6.67× → 5× → 4× → 3.33× while the
rolling average absorbed the single spike.

### Per-post / per-source issues

- **Truth Social is blanket CRITICAL.** 58 alerts fired at once on first run
  (initial backfill), mostly routine candidate *endorsements* ("It is my Great
  Honor to endorse…"). The genuinely interesting posts in the run were
  **media-only with no text** — we logged nothing usable.
- **`large_bet` threshold too low.** 200–280 contract bets flagged HIGH; that's
  ~$100–300 notional on Kalshi. Config says `large_bet_contracts: 500` but
  many sub-500 bets are present — worth auditing whether the threshold is
  applied per-market or being bypassed.

## 3. The flagship feature produced a false positive

The single correlation alert claimed **"4 sources within 10 min"** but it was
**fully spurious**:

- Two of the four sources (`futures_sp500`, `futures_gold`) were **2.5 hours
  stale** — all futures signals are from June 2; the anchor is June 3 00:09. The
  detector's time-window bound is broken (counts signals outside `window_minutes`).
- The anchor itself was a **Tom Kean (NJ-07) endorsement graphic** (confirmed by
  opening the media-only post manually) — a routine endorsement, not a
  market-moving event.
- The remaining Kalshi Taiwan activity was real but **unrelated** to the Trump
  post — two independent things landing in the same 90 seconds.

So nothing actually correlated. The detector manufactured a CRITICAL from
(1) a broken window and (2) treating every Trump post as a high-priority anchor.
Full write-up: [`correlated-event-2026-06-03.md`](./correlated-event-2026-06-03.md).

The Taiwan travel-warning thread (volume spike + odds moves + large bets) is
still genuinely interesting on its own — so the underlying *data* has signal —
but the correlation detector cannot be trusted until both bugs are fixed.

## 4. Diagnosis

The concern ("signals aren't strong enough") is **backwards**. The thresholds
are too *loose* and there's no de-duplication, producing **alert fatigue**, not
weak signal. The system caught a coherent Taiwan cluster — it just also told you
3,000 times that the same 12 markets were busy, and fired a false CRITICAL.

## 5. Recommended fixes (in priority order)

1. **De-dup / cooldown** per `(source, market, signal_type)` — e.g. one
   `volume_spike` per market per N hours. Kills ~95% of the noise on its own.
2. **Fix the correlation detector's time window** — require contributing
   signals to actually fall within `window_minutes` of the anchor. Until then,
   correlation alerts are noise.
3. **Stop treating every Trump post as CRITICAL** — downgrade/filter routine
   endorsements; capture media URLs (and consider OCR) for media-only posts.
4. **Supervise the processes** — wire `healthcheck.py` to restart dead
   collectors (systemd, a cron watchdog, or a supervisor loop) so it doesn't
   silently die again.
5. **Raise / enforce thresholds** — verify `large_bet_contracts` is actually
   applied; add an absolute floor so 5× of a tiny base doesn't fire.

Net: this is fixable, and the underlying idea works. Fix #1 first — it's the
difference between "this doesn't work" and "this works."
