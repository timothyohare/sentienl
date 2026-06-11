# Plan 02 — Code Review Follow-ups (code-reviewer)

**Goal:** close the open items from the static review of `551e1dd`/`ed66fba` that
were not already fixed (W1/L1/M1 are done in `bbd8625`).

TDD: write a failing/character-pinning test first where there's behaviour to
assert (W3), then implement. Doc/verification items (W4, L3) need no test.

## Tasks

### 2.1 — W3: `check_correlation()` ignores the cooldown (parity bug)
`check_and_signal()` skips an anchor if it's within `window_minutes` of
`_last_fired_time` (the cooldown), but `check_correlation()` only checks
`_fired_on_anchors`. So the two methods can disagree: `check_correlation()`
reports True for a cluster that `check_and_signal()` would suppress.

**Fix:** extract a single predicate `_is_fireable(window) -> bool` encapsulating
both the anchor-dedup and the cooldown checks. `check_correlation()` becomes
`any(self._is_fireable(w) for w in windows)`. `check_and_signal()` uses the same
predicate (and still records `_fired_on_anchors` / persists `_last_fired_time`
when it fires, and marks anchors seen when the cooldown suppresses them).

**Test first** (`test_correlation_detector.py`): after `check_and_signal()` fires
on a cluster, a subsequent `check_correlation()` for an overlapping anchor inside
the cooldown returns False (i.e. the two methods agree).

### 2.2 — M2: futures bar timestamp format mismatch breaks dedup across sources
`_fetch_alpaca` stores `timestamp = b.get("t")` (e.g. `...Z`), `_fetch_yfinance`
stores `idx.isoformat()` (`...+00:00`). The dedup key is `str(timestamp)`, so the
same logical bar gets different keys if the source switches → reprocessed,
re-polluting the rolling average and re-firing.

**Fix:** add a module helper `_canonical_ts(ts) -> Optional[str]` that parses any
ISO-8601 form (incl. trailing `Z`) to a canonical UTC isoformat string, falling
back to `str(ts)` if unparseable. Apply it at the dedup chokepoint in
`process_instrument` (source-agnostic, single place).

**Test first** (`test_futures_volume.py`): two bars with the same instant in
different formats (`...Z` vs `...+00:00`) dedupe to a single signal.

### 2.3 — W4: stale alerter docstring
`alerter.py` module docstring line 9 says *"Truth Social (CRITICAL) signals are
NEVER rate-limited or quiet-hour-suppressed."* — false since the priority-tiering
change (TS posts are now LOW/MEDIUM/CRITICAL; only CRITICAL bypasses). Update to
describe the priority-based rule generically. No test (doc only).

### 2.4 — L3: confirm dashboard CSS-class dependency
The templates render `badge-{{ signal.priority }}`. Verify all five priority
classes exist in the inline CSS (`badge-CRITICAL/HIGH/MEDIUM/LOW/INFO`).
**Verified present (app.py:84–88) — no change required;** record the confirmation.

## Done criteria
New tests for 2.1 and 2.2 pass; full suite still green; docstring updated.
Run code-quality agent on the diff.
