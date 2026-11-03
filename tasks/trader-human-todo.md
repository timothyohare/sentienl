# Exchange trader: Human Task List (Tim, future project)

These need a person: money, legal judgement, Betfair correspondence or a
risk decision. Details are in `plans/07-exchange-trading-bot.md`.

## Blockers (Phase 0)
- [ ] **T-H1 (SP1): Ask Betfair AU in writing.** May an AU account bet via
      the API from a server in AWS Ireland or London? Is in-play racing betting
      allowed? Which premium/expert charge applies to AU accounts today?
      The agent can draft the email. **Nothing gets hosted overseas without a
      written yes.**
- [ ] **T-H2: Risk appetite.** Canary bankroll, daily loss cap, max stake.
      These go into config and the bot can never raise them.
- [ ] **T-H3: Spend approvals.** Small AWS spend for SP2 latency tests;
      one month of PRO historical data (SP5).

## Later
- [ ] **T-H4 (SP3/T10): Live app key.** Apply through the Betfair Australia
      activation page (£499, full KYC). Only after Checkpoint 3 (paper results).
- [ ] **T-H5: AWS account setup** in the chosen region, with billing alerts.
- [ ] **T-H6: Review at every checkpoint** (0–4). Each is a go/no-go, and
      "stop" is a valid answer.
