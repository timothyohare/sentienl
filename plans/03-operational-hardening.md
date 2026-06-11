# Plan 03 — Operational Hardening (karen reality-check)

**Goal:** close the non-supervisor operational gaps karen found. (The supervisor
itself is Plan 04.)

## Tasks

### 3.1 — `init_db.py` ignores `SENTINEL_DB` (doc/behaviour mismatch)
CLAUDE.md states `SENTINEL_DB` "overrides paths for all runners and scripts," but
`init_db.py` only honours `--db-path` (default `sentinel.db`) and ignores the env
var — so it can initialise a *different* DB than the collectors read. All other
runners honour it via `load_config`.

**Fix:** make the `--db-path` default fall back to the `SENTINEL_DB` env var:
`default=os.environ.get("SENTINEL_DB", "sentinel.db")`. Explicit `--db-path` still
wins. This matches the documented contract and the other entrypoints.

**Test first** (`tests/unit/test_init_db.py`, new): invoke `init_db.main()` with
`SENTINEL_DB` set (and `sys.argv` with no `--db-path`) under a tmp dir; assert the
DB is created at the env-var path. Use monkeypatch for env + argv.

### 3.2 — Kalshi `large_bet` threshold review (tuning concern)
karen observed a cold-start firehose (~39 signals in 18s; ~3,392 over the prior
33h run) dominated by Kalshi. The priority/dedup work doesn't fully tame this.

This is a **tuning** decision that ideally wants live data, so scope it
conservatively and reversibly:
- Read the current `kalshi:` thresholds in `config.yaml`.
- Document the current values and the observed volume in this plan's notes.
- If `large_bet` / volume-spike thresholds are obviously low for the firehose
  observed, raise them by a documented factor in `config.yaml` (config-only,
  no code change, trivially revertible). Otherwise leave a `# TODO(tuning)`
  comment with the rationale and the observed counts so the next live run can
  calibrate.

No code/test change unless the collector lacks a configurable threshold (verify
during execution).

## Done criteria
`test_init_db.py` passes; full suite green; config change (if any) documented
here. Run code-quality agent on the diff.
