# Betfair Exchange: Human Task List (Tim)

These need a person: an account, money, a legal or terms judgement, or a
decision. The agent can't do them. Each one lists what it unblocks. Details
are in `plans/06-betfair-exchange.md`.

## Before any spike can run
- [ ] **H1: Betfair Australia account.** Sign up or log in at betfair.com.au
      and finish identity verification. Check whether API access needs a
      funded account. → unblocks S1, S3
- [ ] **H2: Delayed (free) app key.** While logged in, create a developer app
      key (Betfair's "Accounts API Visualiser" → `createDeveloperAppKeys`).
      Copy the *delayed* key. → unblocks S1
- [ ] **H3: Read Betfair's API / developer terms.** Confirm personal,
      non-commercial, read-only use is allowed. Record the answer in plan §1.3.
      → unblocks Checkpoint 0
- [ ] **H4 (only if S1 says cert login is needed): Upload a cert.** The agent
      generates a self-signed cert and key. You upload the `.crt` in Betfair
      account security settings. → unblocks S1/Task 2
- [ ] **H5: Put credentials in `.env`** with the `betfair_` prefix only:
      `betfair_username=`, `betfair_password=`, `betfair_app_key=`
      (+ `betfair_cert_path=` if H4). Don't reuse bare `username=`/`password=`
      (that collision caused the 2026-08-11 outage). Note whether 2FA is on.

## Decisions
- [ ] **H6: Scope.** Politics first (plan default), racing first, or both?
      Which events/meetings?
- [ ] **H7: Live key.** After S1 measures the delayed key's delay, decide
      whether to pay for a live key (check Betfair's current AU price), or stay
      on delayed data.
- [ ] Open question: should racing signals ever reach the alerter once
      `alerts.enabled` goes back to `true`?
- [ ] Open question: Melbourne Cup today. Is a historical replay enough, or do
      you want a live capture? A live capture needs H1, H2 and H5 done within
      hours.

## Reviews
- [ ] **Approve this plan** (or mark it up) before Task 1 starts
- [ ] **Checkpoint 0: go/no-go** on the spike findings
- [ ] **H8: Checkpoint 1.** Review about 24h of pinned-market signals: are
      they sensible?
- [ ] **H9: Checkpoint 3.** Sign off on the success criteria evidence and
      calibrated thresholds
