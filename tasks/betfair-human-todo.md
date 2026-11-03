# Betfair Exchange: Human Task List (Tim)

These need a person: an account, money, a legal or terms judgement, or a
decision. The agent can't do them. Each one lists what it unblocks. Details
are in `plans/06-betfair-exchange.md`.

## Before any spike can run
- [ ] **H1: Betfair Australia account.** *(2026-11-03: account created, login
      works, but every API call returns `INVALID_APP_KEY`, probably until
      identity verification and/or a first deposit is done. Check the account
      status page.)* Sign up or log in at betfair.com.au
      and finish identity verification. Check whether API access needs a
      funded account. → unblocks S1, S3
- [x] **H2: Delayed (free) app key.** *(Done 2026-11-03: app
      `sentinel-88ba46`, delayed version active, saved to `.env` as
      `betfair_app_key`. The name `sentinel` was taken: app names are unique
      across Betfair.)* While logged in, create a developer app
      key (Betfair's "Accounts API Visualiser" → `createDeveloperAppKeys`).
      Copy the *delayed* key. → unblocks S1
- [ ] **H3: Read Betfair's API / developer terms.** Confirm personal,
      non-commercial, read-only use is allowed. Record the answer in plan §1.3.
      → unblocks Checkpoint 0
- [ ] **H4: Upload the cert.** The agent generated it 2026-11-03. On desktop,
      open `https://myaccount.betfair.com.au/accountdetails/mysecurity?showAPI=1`
      → Automated Betting Program Access → upload
      `~/.config/sentinel/betfair/client-2048.crt` (the **.crt**, never the .key).
      Expires 2027-11-03. → unblocks cert login in S1/Task 2
- [ ] **H5: Put credentials in `.env`** with the `betfair_` prefix only:
      `betfair_username=`, `betfair_password=`, `betfair_app_key=`
      (+ `betfair_cert_path=` if H4). Don't reuse bare `username=`/`password=`
      (that collision caused the 2026-08-11 outage). Note whether 2FA is on.

## Decisions
- [ ] **H6: Scope.** Politics first (plan default), racing first, or both?
      Which events/meetings?
- [x] **H7: Live key.** *(Answered by Betfair's docs: live keys can't be used read-only, and cost £499. Stay on delayed.)* After S1 measures the delayed key's delay, decide
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

## Links to look at (saved 2026-11-03)
- [ ] https://www.gruss-software.co.uk/forum/viewtopic.php?t=9335. A 2015
      forum thread confirming AU racing markets offer Betfair SP. The agent
      couldn't see any attachment without a forum login.
- [ ] https://gofile.io/d/8Au45z6C. Contents unknown: the page needs
      JavaScript, and listing it needs a gofile guest account, which the agent
      was blocked from creating. Check what's in it before handing it over.
- [ ] https://promo.betfair.com/betfairsp/prices. **Free, from Betfair**:
      daily CSVs back to ~2018 for AU/UK/IRE/USA/FR/RSA/UAE racing (win and
      place). Per runner: BSP, pre-race/morning weighted average price,
      pre-race/in-play min/max, and traded volume. The agent checked today's
      AU file (551 rows, includes the Melbourne Cup card). Likely good enough
      for S3 threshold calibration on racing.
