# Betfair Exchange: Agent Task List

See `plans/06-betfair-exchange.md` for the PRD, architecture, acceptance
criteria and test plan. Human-only steps are in `tasks/betfair-human-todo.md`
(tagged H1…H9 below where they block something).

Every task is done only when `gate-ci --force` exits 0, tests were written
first, and the change is committed and pushed.

## Phase 0: Spikes
- [x] S4: Library choice → `betfairlightweight==2.24.0`, lightweight mode, explicit URIs (done 2026-11-03; plan §4.1)
- [x] S1: Interactive login via `identitysso.betfair.com.au` works; all three API hosts serve the same markets (use `api.betfair.com`). The new key gave `INVALID_APP_KEY` at first and worked ~15 min later. *Not measured:* real delay (a quiet market didn't trade in the 60s window) and session TTL (library assumes 12h)
- [x] S2: Fixtures in `tests/fixtures/betfair/`. Racing `7`, politics `2378961`. Weight limit 40 (best offers) / 10 (+traded), exactly. The delayed key gives market `totalMatched` but runner `totalMatched` = 0.0 and no `tradedVolume`. 37 AU WIN markets in the 21:00 AEDT catalogue
- [ ] S3: **Partly answered:** Betfair's free BSP CSVs (promo.betfair.com/betfairsp/prices) cover AU racing daily back to ~2018. Still to do: replay through the detectors (Task 9)
- [ ] S5 (optional): Betfair vs Kalshi lead/lag on the US midterms (2h). Blocked on S3

### Checkpoint 0: decisions locked; fixtures committed; human go/no-go (H5, H6)

## Phase 1: One pinned market → signal in DB
- [x] Task 1: `betfair:` config + `betfair_`-prefixed creds (S), in `827c2d6`
- [x] Task 2: Read-only `BetfairClient`; login cap; weight batching; guard test (M), in `827c2d6`. Smoke-tested live
- [x] Task 3: `odds_move` end to end + runner (M), in `3d7a2e7`. Live foreground run: login → 40 markets → 326 baselines over 6 cycles

### Checkpoint 1: 24h live on 1–3 pinned markets; human sample review (H8)

## Phase 2: Discovery and full signals
- [x] Task 4: Discovery + lifecycle registry, with integration replay (M), in `3d7a2e7`
- [x] Task 5: `matched_surge` at **market level** (the delayed key has no runner volume). `volume_spike` (rolling baseline) dropped for v1 (M), in `3d7a2e7`
- [x] Task 6: `correlation.exclude_sources`, racing excluded (S), in `bacf5f9`

### Checkpoint 2: gate-ci green; mutation ≥ 60% on new modules
- [x] Met 2026-11-03: `betfair.py` 89% (452/506, the 16 no-tests are `run()`), `betfair_client.py` 72% (162/226), `_parse_betfair` 92% (196/212). gate-ci green, 861 passed

## Phase 3: Measurement and operations
- [x] Task 7: `BetfairPriceFetcher` (M), in `c80b9b0`. Live-tested. **Still needs the price-followup restart** (see human list)
- [~] Task 8: Unit file, `sentinel.target`, healthcheck (lockout → DOWN), CLAUDE.md done. **Installing/enabling the unit was blocked for the agent**, so Tim needs to run it (see human list)
- [ ] Task 9: Calibrate thresholds from S3 + first live week (S)

### Checkpoint 3: §1.5 success criteria with evidence; human review (H9)

## Phase 4 (optional)
- [ ] Streaming API for racing just before the jump
- [ ] Cross-venue divergence signal (Betfair vs Kalshi)
- [ ] Truth Social → Betfair politics mapping
