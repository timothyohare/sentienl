# Paper Ledger: Task List

See `tasks/plan.md` for the details, acceptance criteria and pre-registration.

## Phase 1: Futures fade, end to end
- [ ] Task 1: `paper:` config section and cost model (S)
- [ ] Task 2: `paper_trades` table; open fade trades from new futures signals (M)
- [ ] Task 3: Close due trades with net P&L, and expire unpriceable ones (S)
- [ ] Task 4: Wire into the `price_followup.py` loop; restart the service; verify live rows (S)

### Checkpoint: futures ledger live (human review of the first ~20 trades)

## Phase 2: ASX, reporting, monitoring
- [ ] Task 5: ASX `price_move` trades (session cutoff, ASX costs) (M)
- [ ] Task 6: `paper_ledger_report.py` with bootstrap CI and the pre-registered verdict (S)
- [ ] Task 7: Healthcheck "ledger stalled" check, CLAUDE.md and memory (S)

### Checkpoint: complete; evaluate on 2026-12-05 or at 150 closed gold trades
