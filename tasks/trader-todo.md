# Exchange trader: Agent Task List (future project)

See `plans/07-exchange-trading-bot.md`. This is **not Sentinel**: move this
file to the `exchange-trader` repo when Phase 1 starts. Human-only steps are
in `tasks/trader-human-todo.md`.

Every task is done only when its gate is green (lint, types, tests; mutation
≥ 80% on risk and maths modules), tests were written first, and the change is
committed and pushed.

## Phase 0: Spikes
- [ ] SP1 support: draft the email to Betfair AU (overseas hosting, in-play racing, premium/expert charge) for Tim to send
- [ ] SP2: Latency map from home, Sydney, London, Dublin (RTT, TLS, stream lag). Needs Tim's OK for AWS spend
- [ ] SP3 support: write down the AU live-key activation steps
- [ ] SP4: Framework bake-off, flumine vs NautilusTrader (betfair-rs as fallback), same toy scalp
- [ ] SP5: Evaluate historical data tiers; check simulator fidelity on purchased/recorded data
- [ ] SP6: Pre-registered edge backtest; size N
- [ ] SP7: Cost model (commission by state/code, premium/expert charge, transaction charges)
- [ ] SP8: Stream API session with the free delayed key: conflation, heartbeat, reconnect, order stream
- [ ] SP9 (deferred): Profile engine latency; Rust/io_uring only with evidence

### Checkpoint 0: go/no-go (SP1 answered + SP6 edge + SP7 costs)

## Phase 1: Recorder
- [ ] T1: Repo + CI harness skeleton
- [ ] T2: Stream client with journal + reconnect
- [ ] T3: Market cache from stream deltas, checked against listMarketBook

### Checkpoint 1: one week of recorded AU racing

## Phase 2: Backtest
- [ ] T4: Domain maths (ticks, hedging, P&L, costs) + property tests
- [ ] T5: Risk manager + kill switch (mutation ≥ 80%)
- [ ] T6: Queue-position fill simulator
- [ ] T7: Scalp strategy v1; pre-registered backtest verdict

### Checkpoint 2: go/no-go on net edge

## Phase 3: Paper trading
- [ ] T8: Order gateway, dry-run only
- [ ] T9: Ops: unit, ntfy alerts, read-only dashboard

### Checkpoint 3: 4 weeks paper within backtest band

## Phase 4: Live canary
- [ ] T10: Live key + hosting (region per SP1/SP2), secrets in AWS
- [ ] T11: Live gateway at minimum stakes; order-stream reconciliation
- [ ] T12: Kill-switch drill on live

### Checkpoint 4: 2 weeks live within paper band; Tim decides on scaling

## Phase 5 (evidence only)
- [ ] Rust hot path / NautilusTrader core / io_uring, only if SP9 shows the engine matters
