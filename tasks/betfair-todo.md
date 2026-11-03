# Betfair Exchange: Agent Task List

See `plans/06-betfair-exchange.md` for the PRD, architecture, acceptance
criteria and test plan. Human-only steps are in `tasks/betfair-human-todo.md`
(tagged H1…H9 below where they block something).

Every task is done only when `gate-ci --force` exits 0, tests were written
first, and the change is committed and pushed.

## Phase 0: Spikes
- [x] S4: Library choice → `betfairlightweight==2.24.0`, lightweight mode, explicit URIs (done 2026-11-03; plan §4.1)
- [ ] S1: Auth, endpoint, session TTL, delayed-key delay (2h). Blocked on H1, H2, (H4)
- [ ] S2: Capture fixtures, confirm event type IDs, weight limit, markets per racing day (2h). Blocked on S1
- [ ] S3: Historical data check: Melbourne Cup 2026 + one politics market (2h). Blocked on H1
- [ ] S5 (optional): Betfair vs Kalshi lead/lag on the US midterms (2h). Blocked on S3

### Checkpoint 0: decisions locked; fixtures committed; human go/no-go (H5, H6)

## Phase 1: One pinned market → signal in DB
- [ ] Task 1: `betfair:` config + `betfair_`-prefixed creds (S)
- [ ] Task 2: Read-only `BetfairClient` behind a protocol; login cap; weight batching; guard test (M)
- [ ] Task 3: Pinned-market `odds_move` end to end + runner (M)

### Checkpoint 1: 24h live on 1–3 pinned markets; human sample review (H8)

## Phase 2: Discovery and full signals
- [ ] Task 4: Market discovery + lifecycle registry, with integration replay (M)
- [ ] Task 5: `matched_surge` + `volume_spike` with tiering and dedup (M). Can run in parallel with T4
- [ ] Task 6: `correlation.exclude_sources`, racing excluded (S)

### Checkpoint 2: gate-ci green; mutation ≥ 60% on new modules

## Phase 3: Measurement and operations
- [ ] Task 7: Price follow-through + `BetfairPriceFetcher`; **restart price-followup** (M)
- [ ] Task 8: systemd unit, healthcheck entry, CLAUDE.md, memory (S)
- [ ] Task 9: Calibrate thresholds from S3 + first live week (S)

### Checkpoint 3: §1.5 success criteria with evidence; human review (H9)

## Phase 4 (optional)
- [ ] Streaming API for racing just before the jump
- [ ] Cross-venue divergence signal (Betfair vs Kalshi)
- [ ] Truth Social → Betfair politics mapping
