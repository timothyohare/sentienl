# Plan 06 — Betfair Exchange (Australia) collector

Status: **Draft for review** (2026-11-03). Nothing is built yet. Phase 0
(spikes) is blocked on the human tasks in `tasks/betfair-human-todo.md`.
The agent task list is in `tasks/betfair-todo.md`.

Facts marked **(verify: Sn)** come from memory of Betfair's public docs and
have not been confirmed against a live account yet. Spike *n* confirms or
corrects each one before any code depends on it.

---

## 1. PRD

### 1.1 Problem

Kalshi has no horse racing and no Australian sport. On 2026-11-03 the agent
searched all 15,663 Kalshi series and found no Melbourne Cup market. Polymarket
is ACMA-blocked. Betfair Australia (licensed in the Northern Territory) is the
only legal exchange available from Australia where prices come from trading,
like a prediction market, rather than being set by a bookmaker. It also lists
politics markets (Australian and US elections) that overlap with Kalshi. Those
give Sentinel a second, independent price for the same events.

### 1.2 Goals

| Goal | Measure |
|---|---|
| Detect unusual exchange activity on configured Betfair markets | `odds_move`, `matched_surge` and `volume_spike` signals land in `signals` with source `betfair_<domain>` |
| Cover short-lived markets (racing) as well as long-lived ones (politics) | Markets are found from config filters and stop being polled automatically once they close, with no hand-edited ticker lists |
| Measure whether the signals are worth anything | HIGH signals get price follow-through rows. `signal_scorecard.py` reports per-type effect sizes against the random baseline |
| No new failure modes for the rest of Sentinel | Racing signals never anchor a `correlated_signal`, and the new service has a healthcheck entry |
| Stay read-only | The code can't place, cancel or change bets, and a test enforces this |

### 1.3 Non-goals

- **Placing bets.** This is the same rule as PRD §2 ("observes and alerts
  only"). It is enforced by an architecture test (Task 2), not just by
  convention.
- Bookmaker odds (TAB, Sportsbet): they have no public API and their prices
  aren't set by trading.
- Streaming API in v1. Polling matches every other collector. Streaming is
  Phase 4, and only if Phase 3 data shows polling is too slow (§2.6).
- Per-bettor or per-trade data. Betfair doesn't expose who traded, or
  individual trades (verify: S2), so there is no `new_wallet` or true
  `large_bet` equivalent.
- Redistributing Betfair data. Single user only, per Betfair's API terms
  (verify: H3).

### 1.4 Users and scope

The only user is Tim. **Scope decision needed (H6):** racing, politics, or both
first. The plan's default is **politics first, racing second**:

- Politics fits the PRD's geopolitical theme and can be cross-checked against
  Kalshi on the same events.
- Racing was the original question (Melbourne Cup). Like ASX in 2026-08, it
  extends the PRD's scope without breaking any stated constraint. Its markets
  live for hours, not months, which tests the discovery and lifecycle code
  hardest.

The collector is domain-agnostic (it is driven by filters), so the scope
decision only changes config, not code.

### 1.5 Success criteria (whole plan)

- [ ] `sentinel-betfair.service` runs for 7 days with no unhandled restarts
      and no login lockouts.
- [ ] At least one racing card (if in scope) is discovered, polled and dropped
      on its own after the jump, with no config edits.
- [ ] Signal rate is in the same range as Kalshi's per tracked event, not a
      flood. Thresholds are calibrated from data (Task 9), not guessed.
- [ ] `signal_scorecard.py` shows a `betfair_*` row. Whether it beats the
      baseline is a result to record, not a pass/fail gate.
- [ ] `gate-ci --force` is green, and the mutation score on `betfair.py` is ≥
      60% (Kalshi is 54%).

---

## 2. Architecture

### 2.1 Where it fits

```
 Betfair API-NG (JSON-RPC, read-only calls only)
        │  login / keepAlive        listMarketCatalogue      listMarketBook
        ▼                                 │                        │
 BetfairClient (protocol) ◄───────────────┴────────────────────────┘
        │       real: betfairlightweight or httpx (S4)   tests: fake / fixture replay
        ▼
 BetfairCollector.run()
   every discovery_interval:  filters ──► MarketRegistry (open markets, lifecycle state)
   every poll_interval:       registry.active() ──batched by weight──► books
                              books ──► detectors (pure fns) ──► db.insert_signal()
                                                              └─► db.price_tracking (HIGH+)
                              cursor/baselines ──► db.state
        │
        ▼
 signals table ──► alerter (unchanged) / dashboard (unchanged)
               ──► correlation_detector (+ exclude_sources: betfair_racing)
 post_price_tracking ──► price_followup.py (+ BetfairPriceFetcher) ──► scorecard
```

There is no new IPC. The database is still the only shared state, matching
the CLAUDE.md architecture.

### 2.2 Dependency graph (build order)

```
S1 auth/endpoint ─┐
S2 data capture ──┼─► T1 config + creds ─► T2 client (protocol, read-only guard)
S4 library ───────┘                              │
                                                 ▼
                              T3 pinned-market odds_move, end to end (runner)
                                                 │
                         ┌───────────────────────┼─────────────────────┐
                         ▼                       ▼                     ▼
                T4 discovery + lifecycle  T5 matched_surge/       T8 systemd +
                         │                    volume_spike            healthcheck
                         ▼                       │
                T6 correlation exclusion ◄───────┘
                         │
                         ▼
                T7 price follow-through ─► T9 threshold calibration (S3 data)
```

### 2.3 Key decisions

| # | Decision | Why | Alternative rejected |
|---|---|---|---|
| D1 | The source name carries the domain: `betfair_politics`, `betfair_racing` (from the event type) | Same idea as `futures_*`. It lets the correlation detector and scorecard treat domains separately without parsing payloads | A single `betfair` source: racing would then mix into geopolitical correlation |
| D2 | **Racing is excluded from correlation** through a new `correlation.exclude_sources` config list | A HIGH racing move plus an unrelated futures spike would make a false CRITICAL. This is the same failure class that 551e1dd fixed for Truth Social | Capping racing at MEDIUM: that would also hide it from price follow-through |
| D3 | Markets come from config filters (event type, countries, market types, text query, `lookahead_hours`) plus an optional pinned `market_ids` list | Racing markets are created and settled daily, so a static ticker list like Kalshi's goes stale within hours | Static IDs only: unworkable for racing |
| D4 | The `MarketRegistry` lifecycle is `OPEN → (SUSPENDED ↔ OPEN) → CLOSED → dropped`. In-play polling is a config flag (default off) | Prices during a race are noise for this purpose, and the market closes minutes after the race | Polling until settlement: wastes API weight |
| D5 | Price = **implied probability** (`1 / lastPriceTraded`, falling back to the best back/lay midpoint). Moves are measured in percentage points | Makes `odds_move` comparable with Kalshi's (also in pp) and with the existing thresholds | Raw decimal odds: their moves aren't linear across the range |
| D6 | `matched_surge` = increase in a runner's `totalMatched` between polls (AUD) | Betfair has no trade prints (verify: S2). This is the closest thing to `large_bet` | — |
| D7 | Liquidity guard: no signal unless the back-lay spread is ≤ `max_spread_ticks` and market `totalMatched` ≥ `min_market_matched` | Thin exchange books jump on tiny stakes. This is the same lesson as the futures `min_absolute_volume` incident (2026-08-12) | — |
| D8 | Client behind `BetfairClientProtocol`. Tests mock at the protocol boundary | Same pattern as `TruthSocialClientProtocol`. Keeps the suite hermetic | — |
| D9 | Credentials go in `.env` with a **`betfair_` prefix** (`betfair_username`, `betfair_password`, `betfair_app_key`, `betfair_cert_path`) | The 2026-08-11 outage: unprefixed `username=` collided and killed Truth Social for 3 days | — |
| D10 | **Decided (S4, 2026-11-03): `betfairlightweight==2.24.0`** in `lightweight=True` mode (returns plain dicts), behind `BetfairClientProtocol`. Set `identity_uri`/`api_uri` explicitly from config instead of relying on its `locale` names. See §4.1 | It handles login, keepAlive, session expiry, cert login, the historic-data API (S3) and streaming (Phase 4). Plain dicts keep fixture tests simple | Raw httpx: matches Kalshi and is typed, but means re-implementing session handling, and S3/Phase 4 would still need a library. `flumine`: a betting framework, which conflicts with the read-only non-goal |
| D11 | Login failures back off exponentially and **stop after N** (no tight loop). The session token is cached in `db.state` | Repeated failed logins can lock the account (verify: S1). A locked account is a human-only fix | — |

### 2.4 Config sketch (`betfair:`)

```yaml
betfair:
  enabled: true
  api_endpoint: "https://api.betfair.com/exchange/betting/json-rpc/v1"  # vs api-au (S1)
  poll_interval_seconds: 30
  discovery_interval_seconds: 600
  poll_in_play: false
  filters:                       # each filter → its own domain/source name
    - domain: politics
      event_type_ids: ["2378961"]   # verify: S2
      text_query: "midterm"         # optional
      lookahead_hours: 8760
    - domain: racing
      event_type_ids: ["7"]
      market_countries: ["AU"]
      market_type_codes: ["WIN"]
      lookahead_hours: 6
  market_ids: []                 # pinned, always polled while open
  thresholds:
    odds_move_pp: 5.0
    odds_move_pp_high: 15.0
    matched_surge_aud: 5000
    matched_surge_aud_high: 50000
    volume_spike_multiplier: 5.0
    volume_spike_multiplier_high: 50.0
    max_spread_ticks: 3
    min_market_matched: 10000
correlation:
  exclude_sources: [betfair_racing]
```

All threshold numbers are placeholders until Task 9 calibrates them from
data, as was done for Kalshi's tiering and the ASX floors.

### 2.5 Price follow-through

- Instrument key: `"{market_id}:{selection_id}"`. Price: implied probability.
- `BetfairPriceFetcher` sits in `CompositePriceFetcher`. When a market is
  CLOSED or settled it returns `None`, so racing gets t15 at best and t60+ stays
  NULL. That's expected, and the scorecard must show it as "n/a (market
  closed)", not as a failure. Politics markets get every horizon.
- **Restart `sentinel-price-followup.service` after Task 7 ships.** This is
  the fourth time this rule applies (see CLAUDE.md, ASX 2026-08-26). It is in
  the task's acceptance criteria rather than left to memory.

### 2.6 Rate and data limits (verify: S1/S2)

- Each `listMarketBook` request has a data-weight budget (believed to be 200
  points per request: number of markets × projection weight). The collector
  batches market IDs to stay under it, and a unit test enforces the cap.
- Racing: one AU card is about 8–10 races; a full day across tracks is a few
  hundred markets. With a 6-hour lookahead and in-play off, the active set
  stays small. S2 measures the real numbers.
- The delayed (free) app key returns delayed prices (believed to be a variable
  delay). That's fine for minute-scale signals, but it limits racing's
  usefulness right before the jump. Whether to pay for a live key is a human
  decision (H2), made from S1's delay measurement.

---

## 3. Validation: testing pyramid

```
            ▲   E2E / live (few)      service on real markets; acceptance checklist; scorecard
           ▲▲▲  Integration (some)    collector + tmp SQLite + replayed market timeline
         ▲▲▲▲▲▲ Contract (thin)       recorded fixtures ↔ code's field access; opt-in live check
      ▲▲▲▲▲▲▲▲▲▲ Unit (most)          pure detectors, registry, config, creds, batching, guard
```

| Layer | What | How | Gate |
|---|---|---|---|
| **Unit** | `implied_prob()`, tick ladder and spread, `matched_delta()`, tiering, liquidity guard, weight-batching, registry state transitions, config parsing and defaults, `betfair_`-prefixed cred loading, login backoff and cap | `tests/unit/test_betfair.py`. Pure functions plus a fake client. Inline YAML config (repo convention) | `gate-ci.mjs` |
| **Read-only guard** | No reference to `placeOrders`, `cancelOrders`, `replaceOrders` or `updateOrders` anywhere under `sentinel/` | Test greps the source tree and fails on a match | `gate-ci.mjs` |
| **Contract** | The code reads only fields that exist in real responses | JSON fixtures captured in S2 (redacted) in `tests/fixtures/betfair/`. Unit tests parse them. An opt-in `@pytest.mark.live` test hits the real API and checks the same fields. It is skipped by default like the non-hermetic Polymarket tests, and run by hand weekly or on any "No markets" warning | `gate-ci.mjs` (fixtures). Live is manual |
| **Integration** | Collector + real tmp SQLite + a fake client replaying a market's timeline (pre-race → suspended → closed). Assert the right signals, state, price_tracking rows and correlation exclusion, and that the market is dropped after close | `tests/integration/test_betfair_flow.py`, the first real use of `tests/integration/` | `gate-ci.mjs` |
| **Mutation** | Do the tests catch regressions in detector and registry logic? | `mutation_gate.py`. Target ≥ 60% on `betfair.py`. `run()` excluded, like the other collectors | `gate-mutation.mjs` |
| **E2E / live** | Service up on real markets for 24h (checkpoint) and then 7 days. Racing card dropped on its own. Healthcheck OK or QUIET. Scorecard row exists | Manual checklist in Task 8/9, with the evidence (journal counts, SQL) pasted into the checkpoint | Human sign-off |
| **Backtest** | Calibrate thresholds before trusting live tiering | S3 historical data replayed through the same detector functions | Task 9 |

`gate-verify.mjs` has no boot binding in this repo (`harness.json` has no
`boot`). The live E2E checklist stands in for it, as it does for the other
collectors.

---

## 4. Spikes (time-boxed, before Phase 1)

| ID | Question | Output | Time box | Blocked on |
|---|---|---|---|---|
| **S1** | Auth and endpoint: does a bot need cert login, or is interactive login allowed? Which endpoint serves AU racing and politics (`api.betfair.com` vs `api-au`; both answer from here)? Session TTL and keepAlive? How does 2FA interact? What is the delayed key's actual delay (API vs website, 10 samples)? | Short findings note + a working `login → keepAlive → listEventTypes` script in the scratchpad | 2h | H1, H2 (delayed key), H4 if cert |
| **S2** | Data shape and limits: capture `listEventTypes`, `listMarketCatalogue` (AU WIN racing + politics) and `listMarketBook` (`EX_BEST_OFFERS`, `EX_TRADED`). Confirm event type IDs, whether any trade-level data exists, the request-weight limit, and how many markets a racing day has | Redacted fixtures → `tests/fixtures/betfair/`. Numbers feed §2.6 | 2h | S1 |
| **S3** | Is Betfair's free historical data (BASIC tier, believed to be 1-minute) good enough to calibrate thresholds? Pull Melbourne Cup 2026 (today's race) and one politics market | Yes/no + one replay through draft detectors | 2h | H1 |
| **S4** | Library: `betfairlightweight` vs raw httpx. Check typing (mypy strictness), dependency weight, cert login support, maintenance | Decision recorded in D10 | 1h | none (can start now) |
| **S5** (optional) | Value check: does Betfair politics lead or lag Kalshi on the same event (US midterms, tonight)? | Cross-venue lag on one event from S3 data and Kalshi's DB rows | 2h | S3 |
| S6 (deferred) | Streaming vs polling latency | Only if Phase 3 shows polling misses moves before the jump | — | Phase 3 |

### 4.1 S4 findings (done 2026-11-03, no credentials needed)

Installed in a scratch venv and exercised offline against mocked HTTP:
login → `listMarketBook` returned plain dicts. The request carried
`X-Application`/`X-Authentication` and the RPC method was
`SportsAPING/v1.0/listMarketBook`.

- **Maintenance and licence:** MIT, from the betcode-org GitHub org. Latest
  release 2.24.0 on 2026-09-04, with regular releases before that.
- **Dependencies:** only `requests<2.35.0`. The project venv already has
  requests 2.31.0 (through yfinance, alpaca-py and responses), so nothing new
  gets pulled in. Pin `betfairlightweight==2.24.0` in `requirements.txt`.
- **Typing:** no `py.typed`. `pyproject.toml` already sets
  `ignore_missing_imports = true`, so mypy stays green, but calls into the
  library are untyped. The protocol wrapper (D8) gives the typed boundary.
- **AU endpoint gotcha:** `locale="australia"` silently falls back to the
  **.com** endpoints. Only `locale="new_zealand"` maps to `.com.au`
  (identity `identitysso.betfair.com.au`, API `api.betfair.com.au`). Cert
  login has no `.com.au` mapping in any locale. That's why D10 sets the URIs
  explicitly. Which host an AU account actually needs is still S1's question;
  `api.betfair.com`, `api-au.betfair.com` and `api.betfair.com.au` all answer
  from this server.
- **Session:** the library tracks a 12h session timeout (`session_expired`),
  with `keep_alive()` available. S1 checks whether AU uses the same TTL.
- **Timeouts:** connect 3.05s, read 16s per request. That's fine.
- **Request weight:** the library documents the limits (`metadata.py`, a cap
  of 200 per request) but does **not** batch for you, so Task 2 still owns
  batching. `EX_BEST_OFFERS` alone weighs 5 (→ 40 markets per call), while
  `EX_BEST_OFFERS + EX_TRADED` weighs 20 (→ 10 per call). Runner-level
  `totalMatched` should be enough for `matched_surge` without `EX_TRADED`
  (S2 confirms), which gives 4× the markets per call.
- **Quirk:** when the password or app key is `None`, it quietly reads env
  vars named `<username>password` / `<username>`. Always pass both explicitly,
  in keeping with D9.
- **Read-only:** the library exposes `place_orders` and friends. The Task 2
  guard test greps `sentinel/` for those names, so the wrapper can never
  call them.

### 4.2 Docs findings (2026-11-03, from Betfair's Application Keys and Non-Interactive login pages)

- **The delayed key does not return `totalMatched` from `listMarketBook`.** It
  also returns only 3 price levels and no `EX_ALL_OFFERS`, and the delay is
  "variable between 1-180 second snapshots". That **breaks D6
  (`matched_surge`) and the `volume_spike` baseline as designed**. S2 must
  check whether `EX_TRADED`/`tradedVolume` comes back on the delayed key. If
  it doesn't, v1 is `odds_move` only, and Task 5 shrinks or is dropped.
- **There's no live key for Sentinel.** Betfair says "read-only access using
  the Live App Key isn't permitted". It also costs £499 and needs full KYC.
  Since D1/§1.3 makes Sentinel read-only, **H7 is effectively answered: we
  stay on the delayed key.** Racing just before the jump (where seconds
  matter) is therefore weak. That favours politics-first (H6 default).
- **AU endpoints:** cert login is `https://identitysso-cert.betfair.com.au/api/certlogin`.
  The account security page is `myaccount.betfair.com.au/accountdetails/mysecurity?showAPI=1`.
  2FA doesn't affect cert login.
- App names must be unique (`APP_KEY_CREATION_FAILED`) and must not contain
  your username (`UNEXPECTED_ERROR`). Each account gets one app with a
  delayed and a live version.
- `INVALID_APP_KEY` on a newly created delayed key isn't documented. The
  stated prerequisite, "funded and fully verified", is the leading suspect.
- Cert created 2026-11-03 at `~/.config/sentinel/betfair/client-2048.{crt,key}`
  (outside the repo, key mode 600, **expires 2027-11-03**: renew before then).

**Go/no-go after Phase 0:** stop if S1 finds automated access needs a paid
live key Tim won't buy *and* the delayed key is unusable. Also stop if S2/S3
show that markets which pass the liquidity guard almost never produce signals.

---

## 5. Tasks

Sizes follow the skill's scale. Every task's Definition of Done includes
`node ~/dev/newdev/code-build-harness/harness/gates/ci.mjs --force` exiting 0,
tests written first (TDD, per CLAUDE.md), then commit and push.

### Phase 0: Spikes
S1, S2, S4 (S3 and S5 can run alongside Phase 1). See §4.

#### Checkpoint 0: decisions locked
- [ ] Endpoint, login method, key type and library recorded in §2.3/§2.6 (the
      "verify" tags resolved)
- [ ] Fixtures committed
- [ ] Human go/no-go and scope (H6) answered

### Phase 1: Thinnest vertical slice (one pinned market → signal in DB)

**Task 1: `betfair:` config and credentials (S)**
Add the `BetfairConfig` dataclass, `_parse_betfair()`, `correlation.exclude_sources`,
`config.yaml.example` and `betfair_`-prefixed `.env` loading.
- [ ] `load_config` parses the §2.4 sketch. Missing section → `enabled: false` with defaults
- [ ] Creds load from `.env` or env vars. An unprefixed `username=` is **not** picked up (regression test for the 2026-08-11 collision)
- Verify: `pytest tests/unit/test_config.py tests/unit/test_betfair.py -k config`
- Files: `sentinel/core/config.py`, `config.yaml.example`, `tests/unit/test_config.py`

**Task 2: Read-only `BetfairClient` behind a protocol (M)**
Protocol with `login`, `keep_alive`, `list_market_catalogue` and `list_market_book`.
Real implementation uses the S4 library. Login backoff with a hard cap (D11),
session token cached in `db.state`, and batching that respects the request
weight limit.
- [ ] Fixture-driven tests parse S2 responses into typed dicts the collector uses
- [ ] Login stops after N failures and logs ERROR (no tight loop). Batching never goes over the weight cap
- [ ] Read-only guard test: there are no order-placement method names under `sentinel/`
- Verify: `pytest tests/unit/test_betfair_client.py`. Plus S1's script re-run through the real client by hand
- Files: `sentinel/collectors/betfair_client.py`, `tests/unit/test_betfair_client.py`, `tests/fixtures/betfair/*`, `requirements.txt`
- Deps: T1, S1, S2, S4

**Task 3: Pinned-market `odds_move`, end to end (M)**
`BetfairCollector` polls `market_ids`, computes implied probability, applies the
liquidity guard, inserts tiered `odds_move` signals with source
`betfair_<domain>`, and stores the previous price in `db.state`. Adds
`betfair_runner.py`.
- [ ] A pinned market whose price moves ≥ threshold produces exactly one signal with the right tier. A thin-spread market produces none
- [ ] `python -m sentinel.collectors.betfair_runner` runs against a real pinned market and writes rows (manual)
- Verify: `pytest tests/unit/test_betfair.py`. SQL: `select count(*) from signals where source like 'betfair_%'`
- Files: `sentinel/collectors/betfair.py`, `sentinel/collectors/betfair_runner.py`, `tests/unit/test_betfair.py`
- Deps: T2

#### Checkpoint 1: one market, live
- [ ] gate-ci green
- [ ] Runner up for 24h on 1–3 pinned politics markets with no crash, and the session survives keepAlive
- [ ] Human looks over the signal sample (H8)

### Phase 2: Discovery and full signal set

**Task 4: Market discovery and lifecycle registry (M)**
`MarketRegistry` runs the filters every `discovery_interval`, tracks OPEN,
SUSPENDED and CLOSED, drops closed markets and their `db.state` keys, and
honours `poll_in_play`.
- [ ] Replayed racing timeline: discovered → polled → suspended (not polled) → closed → dropped, with no config edit
- [ ] Domain → source name mapping is correct for each filter
- Verify: `pytest tests/integration/test_betfair_flow.py -k lifecycle`
- Files: `sentinel/collectors/betfair.py`, `tests/integration/test_betfair_flow.py`
- Deps: T3

**Task 5: `matched_surge` and `volume_spike` (M)**
Pure detectors with MEDIUM/HIGH tiering, the same pattern as Kalshi. Volume
baseline is a rolling matched-rate per market kept in `db.state`.
- [ ] Each detector has boundary tests at the threshold, at the HIGH threshold, below the liquidity guard, and on the first poll (no baseline → no signal)
- [ ] Dedup: a sustained surge does not fire every poll (same intent as `TestVolumeSpikeDedup` for Kalshi)
- Verify: `pytest tests/unit/test_betfair.py -k "surge or spike"`
- Deps: T3 (can run in parallel with T4)

**Task 6: Exclude racing from correlation (S)**
`correlation_detector` ignores `exclude_sources`.
- [ ] HIGH `betfair_racing` + HIGH `futures_oil` in the same window → no `correlated_signal`. HIGH `betfair_politics` + HIGH `kalshi` → one
- Verify: `pytest tests/unit/test_correlation_detector.py -k exclude`
- Files: `sentinel/collectors/correlation_detector.py`, its test
- Deps: T1

#### Checkpoint 2: full collector
- [ ] gate-ci green. Integration replay covers a whole racing card
- [ ] Mutation gate on `betfair.py` and `betfair_client.py` reported (target ≥ 60%)

### Phase 3: Measurement and operations

**Task 7: Price follow-through (M)**
Snapshot `price_t0` for HIGH+ signals. Add `BetfairPriceFetcher` to
`CompositePriceFetcher`. Closed market → `None` → "n/a (closed)" in the scorecard.
- [ ] Unit: fetcher returns implied probability, and `None` for closed or settled markets
- [ ] **`sentinel-price-followup.service` restarted**, and the journal shows a real Betfair fetch (not a silent "No price available" with no fetcher log line, the 2026-08-26 symptom)
- Files: `sentinel/scripts/price_followup.py`, `sentinel/scripts/signal_scorecard.py`, tests
- Deps: T3

**Task 8: systemd, healthcheck and docs (S)**
`deploy/systemd/sentinel-betfair.service` + `sentinel.target`, add it to
`healthcheck.MONITORED_UNITS`, a CLAUDE.md section (kept short; the file has a
200-line goal), and memory.
- [ ] `systemctl --user is-active sentinel-betfair` → active. Healthcheck lists it as OK or QUIET
- [ ] Healthcheck flags "login cap reached" as DOWN, not QUIET
- Deps: T3, T4

**Task 9: Calibrate thresholds from data (S)**
Replay S3 historical data and the first week live through the detectors. Set
thresholds near the 90th percentile, like Kalshi's tiering (2026-08-10).
- [ ] Thresholds in config have a comment citing the percentile and sample size
- [ ] Signal rate per market is within 2× of Kalshi's per-event rate
- Deps: T5, S3

#### Checkpoint 3: complete
- [ ] All §1.5 success criteria checked, with evidence pasted
- [ ] Human review (H9)

### Phase 4: Optional, only if Phase 3 earns it
- Streaming API for racing in the final minutes before the jump (S6)
- Cross-venue divergence signal: Betfair politics vs Kalshi on the same event (from S5)
- Truth Social → Betfair politics instrument mapping (closes the follow-up punted in plan 05)

---

## 6. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Automated access needs a paid live key or cert login Tim doesn't set up | High (blocks) | S1 first. Delayed key fallback. Go/no-go at Checkpoint 0 |
| Failed-login loop locks the Betfair account | High | D11 hard cap + backoff. Unit test. Healthcheck DOWN on cap |
| Racing pollutes geopolitical correlation | High | D1 + D2 + Task 6 test |
| Thin books produce noise floods | Med | D7 liquidity guard. Task 9 calibration. Alerts are off anyway (`alerts.enabled: false`) |
| API shape differs from memory | Med | Every such fact is tagged "verify". S2 fixtures + a live contract test |
| Price-followup runs stale code again | Med | Restart is an acceptance criterion of T7 |
| The order-placement API is reachable with the same key | Med | Read-only guard test. Only read endpoints are wrapped |
| Credential collision in `.env` | Med | D9 prefix + regression test |
| Scope creep beyond the PRD | Low | Scope is config-only (H6). Recorded as an extension, like ASX |

## 7. Open questions (for Tim)

1. Politics first, racing first, or both together? (H6)
2. Is a paid live key on the table if the delayed key's delay is too long for racing? (H2)
3. Should racing ever reach the alerter once `alerts.enabled` goes back to
   `true`, or stay collection-only?
4. Melbourne Cup 2026 is today. Is a one-off historical replay (S3) enough, or
   is a live capture wanted? A live capture needs H1/H2 done within hours.
