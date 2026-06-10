# Signal & Correlation Improvement Plans — 2026-06-10

Three approaches to make Sentinel's signals and correlations stronger, derived
from the analysis in [`sentinel-analysis-2026-06-10.md`](./sentinel-analysis-2026-06-10.md)
and [`correlated-event-2026-06-03.md`](./correlated-event-2026-06-03.md).

The three are complementary, not alternatives — together they take the system
from "3,000 duplicate alerts + one false CRITICAL" to "one signal per real
event, trustworthy correlations, CRITICAL reserved for what matters."

---

## Approach 1 — De-duplication: one real-world event → one signal

**Problem.** Collectors re-emit the same event on every poll cycle. Kalshi
`volume_spike` got a cooldown (commit `dd6d2ee`), but **futures still re-fires**:
`_fetch_yfinance` returns the whole day's bars and `latest_bar = bars[-1]` keeps
re-processing the same delayed bar each minute — re-polluting the rolling average
and re-firing the spike (this is the ES=F ×6 re-emission seen on June 2).

**Plan.**
1. Tag each fetched bar with its source timestamp (`timestamp` key) in both
   `_fetch_yfinance` (DataFrame index) and `_fetch_alpaca` (bar `t`).
2. In `process_instrument`, read/write `futures_last_bar_{ticker}` in `db.state`.
   If the latest bar's timestamp equals the last processed one, return early —
   **before** `add_volume_observation` — so neither the history nor a signal is
   produced from a stale repeat.
3. TDD: feeding the identical bar twice yields exactly one observation and one
   signal; a genuinely newer bar is processed normally.

**Files:** `sentinel/collectors/futures_volume.py`, `tests/unit/test_futures_volume.py`.

---

## Approach 2 — Correlation accuracy: fix the window, stop the feedback loop

**Problem.** `Database.get_correlated_signals_in_window` compares raw ISO-8601
timestamps (`2026-06-03T00:09:41+00:00`, a `T` separator) against the output of
SQLite `datetime()` (`2026-06-03 00:09:41`, a space). Because `'T'` (0x54) sorts
after `' '` (0x20), the bounds are wrong: same-day signals fail the upper bound
while *earlier-day* signals pass the lower bound. Net effect — the detector
correlates an anchor with **the entire previous UTC day**, which is exactly how
2.5-hour-stale June 2 futures produced the false "4 sources" CRITICAL on June 3.
It can also count its own `correlated_signal` (CRITICAL) outputs as a source.

**Plan.**
1. Normalise both sides: wrap `s1.created_at` and `s2.created_at` in
   `datetime(...)` so the comparison is space-format vs space-format.
2. Exclude `source = 'correlation_detector'` from both the anchor and the join
   to prevent self-correlation feedback.
3. TDD: two distinct sources *within* the window correlate; a signal *outside*
   the window does not; the detector never counts its own output.

This also fixes the 5 currently-failing window tests (`test_correlation_detector`
×4, `test_db` ×1).

**Files:** `sentinel/core/db.py`, `tests/unit/test_db.py`, `tests/unit/test_correlation_detector.py`.

---

## Approach 3 — Signal quality: priority tiering for Truth Social

**Problem.** Every Trump post is hard-coded `CRITICAL`. On first run that fired
58 alerts at once — almost all routine candidate *endorsements* ("It is my Great
Honor to endorse…"), which never move markets. Worse, because correlation only
counts HIGH/CRITICAL, every endorsement becomes a valid correlation anchor — the
June 3 false positive was anchored on a Tom Kean endorsement graphic.

**Plan.**
1. Add a pure `classify_priority(text)`:
   - market-relevant keywords (tariff, China, Iran, Fed, sanction, war, …) →
     `CRITICAL`;
   - routine endorsement language with no market keyword → `LOW`;
   - everything else → `MEDIUM` (a real post worth knowing, but rate-limitable).
2. Drive the keyword lists from config (`truth_social.critical_keywords`,
   `endorsement_markers`) with sensible defaults so behaviour is tunable without
   code changes.
3. Apply the classified priority in `process_post` instead of the constant.
4. TDD: tariff post → CRITICAL; endorsement post → LOW; neutral post → MEDIUM.

Downstream effect: endorsements drop below the correlation threshold (no longer
anchor false correlations) and become rate-limitable/quiet-hour-suppressible in
the alerter, while genuinely market-moving posts stay CRITICAL and bypass both.

**Files:** `sentinel/collectors/truth_social.py`, `sentinel/core/config.py`,
`config.yaml`, `tests/unit/test_truth_social.py`.

---

## Execution order

2 → 1 → 3 (smallest/highest-confidence first; 2 also turns failing tests green).
Each approach is committed only after the full suite is green (modulo the 6
genuinely pre-existing failures: `test_polymarket` ×4 live-DNS, `test_db`
`test_all_tables_created`, `test_futures_volume` ×2 broken mocks).
