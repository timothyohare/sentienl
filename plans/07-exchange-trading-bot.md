# Plan 07 — Automated Betfair Exchange trading bot (future, separate project)

Status: **Research plan only, nothing built** (written 2026-11-03, for later).
Task trackers: `tasks/trader-todo.md` (agent) and `tasks/trader-human-todo.md`
(Tim).

> **This is not part of Sentinel.** Sentinel's PRD (§2) makes it observe-only,
> and plan 06 enforces that with a test. A bot that places real-money bets
> needs its own repo, its own credentials (a *live* app key) and its own risk
> controls. The plan lives here only until that repo exists. Move it there
> when Phase 1 starts. Working name: **`exchange-trader`**.

Facts marked **(verify)** are from secondary sources or memory. The spike
named next to each one confirms or corrects it before anything depends on it.

---

## 1. What we're building, and what we're not

Gruss Betting Assistant and Bet Angel are desktop trading front ends for the
Betfair Exchange. They offer a one-click price ladder, Excel integration,
stop loss / trailing stop, green-up (hedging), dutching, and rule-based
automation (Bet Angel's "Guardian"). We **don't copy them**: not their code,
UI or branding. We use their feature lists as a requirements checklist, and
build only the slice an *unattended* bot needs:

| Their feature | v1? | Why |
|---|---|---|
| Rule-based automated entry/exit | **Yes** | The core of the request |
| Stop loss (fixed ticks), trailing stop | **Yes** | Requested. Risk control |
| Green-up / hedge to equal profit | **Yes** | How "back high, lay low" banks a profit |
| Fill-or-kill, persistence (lapse at the off) | **Yes** | Required for safe pre-race trading |
| One-click ladder GUI | No | Unattended bot. A read-only web dashboard later |
| Excel integration | No | Strategies are code, version-controlled and tested |
| Dutching | Later | Not needed for scalping |
| In-play trading | Later | Bet delay + legal check (§3.2) first |

**Strategy family for v1: pre-race scalping.** "Back high, lay low": back at
price *P*, offer a lay *k* ticks lower. If both match, hedge (green up) for a
locked-in profit on every outcome. If the price moves *m* ticks against the
position, the stop loss closes it at the market. Everything is flat and every
unmatched order is cancelled before the race starts.

---

## 2. PRD

### 2.1 Goals
| Goal | Measure |
|---|---|
| Prove (or disprove) an edge **before** risking money | Pre-registered backtest + paper-trade verdict, net of commission and charges (§6) |
| Never lose more than the configured limits | Zero risk-limit breaches in simulation, paper and live. Limits enforced in code, and tested |
| React fast enough for the strategy | Measured tick-to-order latency within the budget set by SP2 (not a guess) |
| Run unattended | Survives stream disconnects, session expiry and restarts without leaving open risk |
| Fully auditable | Every market update, decision and order event journaled. Any day can be replayed exactly |

### 2.2 Non-goals (v1)
GUI ladder; Excel; in-play; sports other than racing; multiple accounts;
anything that hides activity from Betfair (VPN tricks or IP masking, §3.2).

### 2.3 Success criteria by stage
- **Backtest:** pre-registered strategy; mean net P&L per market > 0 with a
  bootstrap 95% CI excluding 0, over ≥ N markets (N set in SP6).
- **Paper (live data, simulated fills):** same verdict on forward data for
  ≥ 4 weeks.
- **Canary live:** minimum stakes, hard daily loss cap. Realised P&L within
  the paper-trading confidence band. Zero limit breaches. Zero orphaned
  positions at the off.
- Any stage failing means **stop**, not tune-until-it-passes (the same rule
  as Sentinel's `fade60` paper ledger).

---

## 3. Reality checks that drive the design

### 3.1 Where latency actually comes from
| Component | Rough size | Lever |
|---|---|---|
| Australia ↔ Betfair API (believed to be hosted near AWS Dublin, eu-west-1) | ~250–300 ms round trip **(verify: SP2)** | Hosting region |
| AWS Dublin ↔ Betfair | reported < 1 ms, and once 13 ms during a 2025 incident ([AWS re:Post](https://repost.aws/questions/QUHinIWIYVSeScKNuTZp-arg/higher-network-latency-since-1st-of-may)) **(verify: SP2)** | Region choice |
| Betfair in-play bet delay | seconds on in-play markets; 0 pre-race (`betDelay: 0` in our fixtures) | Trade pre-race only |
| Delayed app key | 1–180 s snapshots (Betfair docs) | **Must use a live key** |
| Our engine: decide + serialise | microseconds (Python ~10–100 µs, Rust ~1–10 µs) | Language, design |
| Kernel networking (io_uring vs epoll) | single-digit µs per message | io_uring |

**Conclusion:** network distance and Betfair's own processing take
milliseconds, while io_uring saves microseconds. **io_uring is not worth its
complexity for v1.** Rust is worth considering for correctness (types,
no GC pauses), not raw speed. The real latency wins are, in order: (1) host
in the right region, (2) use the Exchange Stream API with `conflateMs: 0`
instead of polling, (3) keep HTTP connections warm, (4) only then the
engine. SP9 measures whether (4) ever matters.

### 3.2 Legal and terms (the biggest blocker)
- **AU account from an overseas server:** Betfair has separate UK and AUS
  exchange endpoints, and an AU account is tied to the AUS exchange
  ([Betfair docs](https://betfair-developer-docs.atlassian.net/wiki/x/5Tkp)).
  Secondary sources warn that accessing an AU account from outside
  Australia can breach the terms and get the account restricted or funds
  frozen. **Running in AWS Ireland is unconfirmed and possibly not allowed.**
  SP1 must get Betfair's answer *in writing* before any non-AU hosting. The
  fallback is AWS Sydney (ap-southeast-2), which is in Australia but far
  from Betfair's servers, so the latency is worse.
- **Automated betting itself** is allowed with a live app key (Betfair sells
  it for this purpose; £499 activation and full KYC, per the Application
  Keys page). The AU activation process is separate.
- **In-play racing online:** permitted in AU for racing but not most sport
  **(verify: SP1)**. v1 avoids the question by not trading in-play.

### 3.3 Costs that eat a thin edge
- **Commission:** 5–10% of *net market winnings* on AU racing, varying by
  state and code (our captured AU market rules show an 8% base rate).
  Scalps make small wins per market, so commission takes a large share.
- **Premium / Expert charges:** AU's Premium Charge has applied to
  consistently profitable accounts whose commission paid is a low share of
  gross profit. The UK replaced it with an "Expert Fee" in 2025. **What
  applies to an AU account now: verify, SP7.** It can make a strategy that
  is profitable gross into one that isn't, net.
- **Transaction charges:** betfairlightweight's metadata notes 5,000
  transactions/hour free, then £0.002 each **(verify: SP7)**. That matters
  for strategies that cancel and replace a lot.
- **Live key** £499 one-off. **AWS** ~US$30–150/month for a small always-on
  instance. **Historical data:** PRO tier (full stream) is paid.

### 3.4 Is there an edge at all?
Pre-race scalping is crowded: professionals and the tools above already
compete for the same ticks. Treat "an edge exists" as **the hypothesis
Phase 2 tests**, not a given. The cheapest possible "no" (a backtest)
comes before any expensive build or live key.

---

## 4. Architecture

```
            ┌──────────────── exchange-trader (one process per strategy) ────────────────┐
 Betfair    │                                                                             │
 Stream API │  MarketStream ──► MarketCache (ladders, LTP, volume, clk) ─┐                │
 (ESA, TLS) │  conflateMs=0, heartbeat, reconnect with clk/initialClk    │                │
            │                                                            ▼                │
            │  OrderStream ──► OrderBook (our orders, fills) ───► Strategy (pure, per mkt)│
            │                                                            │ intents        │
            │                                                            ▼                │
 Betfair    │  OrderGateway ◄── RiskManager (pre-trade checks, limits, kill switch) ◄─────┤
 REST API   │  placeOrders / cancelOrders / replaceOrders, warm keep-alive connections    │
            │                                                                             │
            │  Journal: append-only log of every input + decision + order event ──► disk  │
            │  Clock: chrony/PTP-synced; exchange timestamps (pt) kept alongside local    │
            └──────────────┬─────────────────────────────────────────────┬────────────────┘
                           │ metrics/heartbeats                          │ journal files
                           ▼                                             ▼
                 Alerts (ntfy, as in Sentinel)              Replay / Backtest harness
                 + read-only dashboard                      (same Strategy + Risk code,
                                                             simulated fills)
```

**Rules that make it testable and safe:**
1. **Strategy is a pure function:** `(market state, our orders, clock) →
   intents`. It does no I/O, so the identical code runs in backtest, paper
   and live.
2. **Every order goes through RiskManager.** The gateway rejects anything
   that hasn't been checked. Kill switch = RiskManager refuses all new
   orders and issues cancel-all plus flatten.
3. **Event-sourced:** the journal is the source of truth. Replaying a
   journal must reproduce the same decisions bit for bit (a test).
4. **Fail flat:** on stream loss, session loss, journal write failure or a
   risk breach, cancel unmatched orders and stop trading. Positions near the
   off are hedged or cancelled before `marketTime − safety margin`. Orders
   use persistence `LAPSE` so Betfair cancels them at in-play turn anyway.

### 4.1 Domain maths (unit-tested first)
- **Tick ladder:** 1.01–2 step 0.01; 2–3 step 0.02; 3–4 step 0.05; 4–6
  step 0.1; 6–10 step 0.2; 10–20 step 0.5; 20–30 step 1; 30–50 step 2;
  50–100 step 5; 100–1000 step 10. Every order price must sit on the
  ladder. Moving "k ticks" means k ladder steps, not k × a fixed increment.
- **Green-up:** after backing stake *B* at *Pb*, lay stake to equalise =
  *B·Pb / Pl*. Profit on every outcome = *B·Pb/Pl − B* (before commission).
- **Liability:** lay liability = stake × (price − 1). Exposure is tracked
  per market and per day across matched and unmatched orders.
- **Commission:** charged on net market winnings at that market's rate.
  P&L is reported both gross and net.

### 4.2 Technology choices and tradeoffs

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Python + flumine** (betcode-org, built on betfairlightweight) | Mature and used for real money. Built-in backtesting on historical stream files. Fast to a working bot | Python GC/latency (µs–ms tails). Its own framework conventions | **Strong candidate for Phases 1–4** |
| **NautilusTrader** (Rust core, Python strategies, Betfair adapter `nautilus-betfair`) | Rust-speed core plus Python ergonomics. Serious event-driven backtester | Large framework, steeper learning curve. Betfair is one adapter of many | **Strong candidate.** SP4 bake-off |
| **Pure Rust** (tokio; `betfair-rs` 0.3.3 or our own client) | Full control, no GC, strong types | Most build effort. We'd write our own backtester. `betfair-rs` maturity unknown | Only if SP4/SP9 show the frameworks can't meet the budget |
| **io_uring** (tokio-uring / monoio / glommio) | Fewer syscalls, µs-level gains | Linux-only. Immature ecosystem for TLS clients. Saves µs against ms network (§3.1) | **No** for v1. Revisit only with SP9 profiling evidence |
| Kernel-bypass (DPDK etc.) | Lowest latency | Absurd for a TLS/HTTP API over the internet | No |

| Hosting | Latency to Betfair | Terms risk | Verdict |
|---|---|---|---|
| AWS eu-west-1 (Dublin) | Lowest (reported < 1 ms) | **Unresolved for an AU account** (§3.2) | Only with Betfair's written OK |
| AWS eu-west-2 (London) | Low (a few ms, verify) | Same as above | Fallback to Dublin |
| AWS ap-southeast-2 (Sydney) | ~250–300 ms (verify) | Lowest | **Default until SP1 answers** |
| Home server (Sentinel box) | Same as Sydney plus home-network jitter | Lowest | Fine for Phases 1–3 (record + paper) |

Instance: a small compute-optimised instance (e.g. c7i.large or c7g.large),
chrony with the Amazon Time Sync Service, secrets in SSM Parameter Store /
Secrets Manager (not `.env`), and the journal on EBS with S3 backup.

---

## 5. Spikes (Phase 0)

| ID | Question | Output | Time box | Who |
|---|---|---|---|---|
| **SP1** | Legal and terms: may an AU account bet via API from an overseas (AWS Ireland/London) server? Is in-play racing allowed? Which premium/expert charge applies to AU? | Betfair's **written** answer, saved in the repo | days (waiting on Betfair) | **Tim** (agent drafts the email) |
| **SP2** | Latency map: RTT and TLS handshake to `api.betfair.com` and `stream-api.betfair.com` from home, Sydney, London, Dublin. Plus stream lag (exchange `pt` vs local receive time) | Table + chosen region | 3h (+ small AWS spend) | Agent; Tim approves spend |
| **SP3** | Live key for an AU account: process, fee, requirements, timeline | Steps written down | 1h | Tim + agent |
| **SP4** | Framework bake-off: the same toy scalp in flumine and in NautilusTrader (betfair-rs if either fails), run on the same historical day | Dev time, correctness, throughput, latency tails → decision | 2 days | Agent |
| **SP5** | Historical data: tiers and costs (BASIC free 1-min; ADVANCED/PRO paid). Buy one month of PRO AU racing, or record our own (Phase 1) | Data in hand + simulator-fidelity notes | 3h + purchase | Tim buys, agent evaluates |
| **SP6** | Edge feasibility: does a simple pre-registered scalp beat commission and charges in backtest? Size N for significance | Pre-registered verdict | 2 days | Agent |
| **SP7** | Full cost model: commission by state/code, premium/expert charge, transaction charges | A tested `costs` module + doc | 3h | Agent (+ Tim asks Betfair) |
| **SP8** | Stream API behaviour: `conflateMs: 0`, heartbeats, reconnect with `clk`/`initialClk`, segmentation, order stream. Can be tried with the free delayed key (2 connections, 200 markets) | Recorded session + notes | 3h | Agent |
| SP9 (deferred) | Profile the engine: does our own processing ever show up in tick-to-order latency? Only then consider Rust hot paths or io_uring | Flame graphs | 1 day | Agent |

### Checkpoint 0: go/no-go
Go only if SP1 is answered, SP6 shows a plausible net edge, and SP7's costs
are included. Otherwise **stop**. The recorder from Phase 1 can still feed
Sentinel research.

---

## 6. Validation: testing pyramid

```
                 ▲  Live canary        min stakes, daily loss cap, kill-switch drill
                ▲▲▲ Paper trading      live stream, simulated fills, ≥ 4 weeks
              ▲▲▲▲▲ Replay/backtest    historical + own journals; pre-registered verdict
            ▲▲▲▲▲▲▲ Integration        fake exchange (stream + REST) incl. faults
         ▲▲▲▲▲▲▲▲▲▲ Property tests     green-up equalises, limits never exceeded
     ▲▲▲▲▲▲▲▲▲▲▲▲▲▲ Unit               ticks, hedging, P&L, commission, state machines
```

| Layer | What it proves | Notes |
|---|---|---|
| Unit | Tick ladder rounding and stepping. Hedge stake. P&L gross and net. Order state machine for every transition (placed, partially matched, matched, lapsed, cancelled, rejected, each Betfair error code) | Written first (TDD) |
| Property-based (hypothesis / proptest) | For any prices and stakes: green-up profit is equal on all outcomes (± rounding). Risk checks never allow liability above the cap. Prices are always on the ladder | Catches edge cases unit tests miss |
| Integration (fake exchange) | Stream disconnect mid-position → flat. Session expiry → re-login without double orders. Partial fills, late fills after a cancel, rejected orders, clock skew, journal write failure → kill switch | Deterministic fake that serves stream + REST |
| Replay determinism | Re-running a journal gives byte-identical decisions | Guards the event-sourcing design |
| Backtest | Net P&L with a realistic fill model (queue position: only the volume traded *after* our order joins the queue fills it) | Pre-registered. No parameter fishing |
| Paper trading | The same verdict on forward live data | Uses the live stream, places no orders |
| Live canary | Real fills match the paper model within its band. Limits hold. Kill switch works under a forced fault (a drill) | Minimum stakes, hard daily loss cap |
| Mutation testing | The risk and maths modules' tests catch regressions | Target ≥ 80% on risk/maths (higher than Sentinel; real money) |
| Latency harness | Tick-to-order p50/p99 within the budget | Measured per release, like Sentinel's perf gate |

---

## 7. Phases and tasks (vertical slices; each leaves a working system)

### Phase 1: Recorder (no betting, works with the delayed key)
- **T1 Repo + harness.** `exchange-trader` repo, CI gates (lint, types, tests,
  mutation), and a `harness.json` binding. *Accept:* `gate-ci --force --full`
  green on an empty skeleton.
- **T2 Stream client.** Market subscription by filter, heartbeat,
  reconnect from `clk`, journal to disk. *Accept:* an 8-hour session with a
  forced disconnect replays into an identical ladder state. *Uses:* SP8.
- **T3 Market cache.** Ladders, LTP, traded volume and status from stream
  deltas. *Accept:* replaying a recorded journal matches `listMarketBook`
  snapshots taken at the same time.
**Checkpoint 1:** a week of recorded AU racing, with no gaps in heartbeat logs.

### Phase 2: Strategy + risk in backtest
- **T4 Domain maths.** Ticks, hedging, P&L, the commission/charges model
  (SP7). Unit + property tests. *Accept:* the properties in §6 hold over
  10k generated cases.
- **T5 Risk manager.** Max stake, per-market and daily liability, max open
  orders, max orders/second, daily loss cap, the off-time cutoff, and the kill
  switch. *Accept:* the property "no sequence of intents can exceed a limit".
  Mutation score ≥ 80%.
- **T6 Simulator.** A queue-position fill model over journals and
  historical files. *Accept:* fills on a hand-built scenario match
  expectations exactly.
- **T7 Scalp strategy v1** (pure). Entry rule, take-profit k ticks, stop
  loss m ticks, time stop before the off, green-up. *Accept:* the
  pre-registered backtest is run and **the verdict recorded either way**.
**Checkpoint 2 (go/no-go):** net edge with a CI that excludes 0? If not, stop.

### Phase 3: Paper trading (live data, no orders)
- **T8 Order gateway (dry-run mode).** Builds real `placeOrders` payloads,
  logs them and simulates fills. Can't send unless a separate live flag
  *and* a live key are present.
- **T9 Ops.** systemd or container unit, ntfy alerts (heartbeat, kill
  switch, loss-cap hit), a read-only dashboard.
**Checkpoint 3:** 4 weeks of paper results within the backtest band.

### Phase 4: Live canary
- **T10 Live key + hosting** (after SP1/SP3). Region per SP1/SP2.
  Secrets in AWS.
- **T11 Live gateway.** Real orders at minimum stakes, persistence
  `LAPSE`, and reconciliation of the order stream against our own order book.
- **T12 Kill-switch drill.** Pull the network mid-position on the live canary
  and prove the bot goes flat.
**Checkpoint 4:** 2 weeks live within the paper band, zero breaches. Tim
decides on scaling.

### Phase 5: Optimise (only with evidence)
- Move hot paths to Rust, or adopt NautilusTrader's Rust core, **only if
  SP9 profiling shows the engine is a meaningful share of tick-to-order
  latency**. io_uring stays off unless the same evidence points at syscalls.

---

## 8. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Overseas hosting breaches AU terms → account restricted, funds frozen | **Critical** | SP1 written answer first. Default to Sydney/home |
| No real edge after costs | High | Backtest and paper before any live key. Pre-registered stop rule |
| Bug places runaway orders | **Critical** | RiskManager on every order. Max orders/s. Kill switch. Loss cap. Minimum stakes in canary. Property tests. Mutation ≥ 80% |
| Stream disconnect with an open position | High | Fail flat. `LAPSE` persistence. Heartbeat watchdog. Drill in T12 |
| Simulator overstates fills | High | Queue-position model. Paper stage compares against reality before money |
| Premium/expert charge wipes out profit | High | SP7 cost model inside the backtest verdict |
| Over-engineering for speed (io_uring, Rust) before an edge exists | Med | Phase ordering. SP9 evidence gate |
| Clock skew misorders events | Med | chrony. Exchange `pt` timestamps journaled |

Real-money automated trading can lose money fast. The bankroll and daily
loss limit are Tim's decision (H-list), enforced in code, and never raised
by the bot itself.

## 9. Open questions for Tim
1. Bankroll for the canary, and the daily loss cap?
2. If Betfair says no to overseas hosting, is Sydney latency acceptable, or
   does the project stop?
3. Racing only, or also sport (sport in-play is restricted online in AU)?
4. OK to buy a month of PRO historical data (SP5), and a small AWS spend for
   SP2?

## Sources
- [AWS re:Post: Betfair API latency from Dublin](https://repost.aws/questions/QUHinIWIYVSeScKNuTZp-arg/higher-network-latency-since-1st-of-may)
- [Betfair developer docs: separate UK and AUS exchange endpoints](https://betfair-developer-docs.atlassian.net/wiki/x/5Tkp)
- [Betfair developer docs: Application Keys](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687105/Application+Keys)
- [Betfair Exchange Stream API docs](https://betfair-developer-docs.atlassian.net/wiki/x/DAwp)
- [NautilusTrader Betfair adapter (stream conflation, rate buckets)](https://nautilustrader.io/docs/python-api-latest/adapters/betfair.html)
- [nautilus-betfair crate](https://docs.rs/nautilus-betfair)
- [betfair-rs crate](https://docs.rs/betfair-rs)
- [Betfair Data Scientists: commission](https://betfair-datascientists.github.io/wagering/commission)
- [Racing Post: Expert Fee replaces Premium Charge (2025)](https://racingpost.com/news/britain/betfair-exchange-to-introduce-new-commission-system-for-2025-as-premium-charge-is-dropped-a7wbg0v4GCAJ)
- [Before You Bet: Betfair commissions and charges (AU)](https://www.beforeyoubet.com.au/betfair-commissions-and-charges)
