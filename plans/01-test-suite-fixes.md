# Plan 01 — Test Suite Fixes (test-engineer audit)

**Goal:** get the suite to a fully green, hermetic baseline and close the
highest-value coverage gaps. Source: test-engineer audit (7/10 overall).

Approach is TDD-flavoured: for *bugs in tests*, first confirm the failure, fix
the test/impl, confirm green. For *missing coverage*, write the new test first
(watch it pass/fail meaningfully against current code), then keep it.

## Baseline
`298 passed, 8 failed`. The 8 failures: test_db ×1, test_futures_volume ×2,
test_polymarket ×5.

## Tasks

### 1.1 — Fix `test_db.py::test_all_tables_created` (test bug)
`execute_fetchall()` returns dict rows, so `row[0]` raises KeyError. Change to
`row["name"]`. (db.py:47)

### 1.2 — Fix `test_futures_volume.py::test_volume_history_capped_at_rolling_bars` (test bug)
The test asserts history caps at `rolling_bars + 5`, but the design caps at
`HISTORY_MAX_BARS` (100) — `rolling_bars` is only the *averaging* window. Rewrite
to assert the real bound: add >100 observations and assert
`len(history) == HISTORY_MAX_BARS`. This makes the test meaningful (verifies the
memory bound actually holds) instead of asserting a non-existent feature.

### 1.3 — Fix `test_futures_volume.py::test_create_signal_on_spike` (test bug)
The `with patch(...datetime)` block sets `mock_dt.now.return_value` to a *real*
datetime, then assigns `.time.return_value` on a builtin method → AttributeError.
`process_instrument` doesn't call `datetime.now()` at all (it receives `now_time`
and `today_str` as args), so the patch is both broken and pointless. Remove the
patch block; call `process_instrument` directly.

### 1.4 — Fix `test_polymarket.py::test_odds_move_negative` (real float bug)
`_is_odds_move(0.60, 0.55, 5.0)` computes `abs(0.55-0.60)*100 = 4.999999…` due to
float representation, so `>= 5.0` is False and a genuine 5pp move is missed. Fix
the **implementation** (`polymarket.py::_is_odds_move`) to round the percentage-
point delta before comparison (`round(change_pp, 9)`). This is a correctness fix,
not just a test fix; the test then passes legitimately.

### 1.5 — Make the 4 live-network `test_polymarket` tests hermetic or skip
`test_fetch_market_success`, `test_fetch_trades_success`,
`test_get_wallet_age_cache_miss_with_api_key`, `test_large_bet_creates_signal`
use the `responses` library, which only patches `requests` — the collector uses
`httpx`, so calls escape the mock and hit the (DNS-blocked) live API.
Polymarket is **deprecated** (ACMA-blocked, replaced by Kalshi). Decision:
mark these 4 with `@pytest.mark.skip(reason=...)` documenting that Polymarket is
deprecated and the tests are non-hermetic (live httpx calls). Rationale: not
worth building httpx mock infra for a deprecated collector; skipping keeps the
suite green and honest. (Pure-logic Polymarket tests stay active.)

### 1.6 — Add: CRITICAL bypasses quiet hours AND rate limit end-to-end (gap)
New test in `test_alerter.py`: build a CRITICAL signal, arm the rate limiter for
its source, set `now_utc` inside quiet hours, mock `send_ntfy` → assert it is
called and the signal is marked alerted. Verifies the full `dispatch_signal`
pipeline, not just the two predicates in isolation.

### 1.7 — Add: midnight-crossing quiet hours (gap)
New test: configure `quiet_hours_utc` as an overnight window (e.g. 23:00–06:00),
assert a LOW signal is suppressed at 01:00 (inside) and sent at 10:00 (outside).
Exercises `is_in_window` midnight-crossing via the alerter path.

### 1.8 — Add: correlation window boundary (gap)
New test in `test_correlation_detector.py`: anchor at T, second source at exactly
T+`window_minutes`, assert it correlates (inclusive boundary, matches the SQL
`<=`). Documents the boundary contract.

## Out of scope (noted, not done)
- Consolidating trivial helper tests (low value, risks churn).
- Dedup tests for TS/Kalshi post IDs (collectors already track cursor state;
  separate effort).

## Done criteria
`pytest -q` → `0 failed` (skips allowed for deprecated Polymarket network tests),
new tests present and passing. Run code-quality agent on the diff.
