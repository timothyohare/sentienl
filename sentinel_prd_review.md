# Sentinel PRD — Senior Architect Review

_Reviewed 26 March 2026 · Architecture, reliability, and implementation concerns_

---

## What's Good

**1. Component isolation is correct.**
Each collector is an independent process. One collector failing or getting rate-limited doesn't cascade to the others. This is the right architecture for a polling system with heterogeneous external dependencies.

**2. SQLite as source of truth is the right call.**
No separate database process to manage, portable, trivially backupable. At the poll volumes Sentinel runs (one write per 8–60 seconds per collector), SQLite will never be a bottleneck.

**3. systemd for process management — good.**
Battle-tested, handles auto-restart, integrates with journald for logs. Correct choice over anything custom.

**4. HTMX + Flask for the dashboard — correct.**
No build chain, no JS framework to maintain. For a solo personal project read by one person, this is the right tool.

**5. Rate limiting in the dispatcher is well-designed.**
Priority tiers with separate batching rules, quiet hours, and per-source rate windows is thoughtful. Most personal projects skip this entirely and then abandon the system due to alert fatigue.

**6. Acceptance criteria in section 11 are concrete and measurable.**
Most personal project PRDs stop at "it works." The test matrix is unusually rigorous and will catch real bugs at integration time.

**7. Risk table is honest.**
Covers real risks (API deprecation, yfinance reliability, alert fatigue) rather than platitudes.

---

## Critical Issues — Fix Before Writing Code

### C1: Redis pub/sub is fire-and-forget — CRITICAL alerts can be silently lost

Redis pub/sub has no persistence. If the alerter process is restarting (which systemd restart has a delay for) when a CRITICAL event fires, the message is dropped. For a system where missing an alert during a fast-moving market event is the failure mode, this is a serious gap.

**Options:**
- Switch the event bus from pub/sub to **Redis Streams** (`XADD`/`XREADGROUP`), which persists messages and supports consumer groups with acknowledgement.
- Or eliminate Redis entirely — collectors write directly to SQLite, and the alerter polls SQLite for unprocessed signals (`WHERE alerted = false`). Simpler, more reliable, at the cost of slightly higher latency (acceptable at 1–5 second polling).

The Redis-only architecture adds a dependency that doesn't justify itself for a single-server, single-consumer system. **Consider removing Redis and polling SQLite instead.**

### C2: Truth Social API is unvalidated — core assumption unconfirmed

The Mastodon-compatible API endpoint is listed as the approach but never confirmed to be functional today. If it returns 401, requires auth, or is rate-limited to less than 1 request per 8 seconds per IP, the v1 core feature is dead before a line of code is written.

**Must do before any other work:** write a 10-line Python spike, hit the endpoint, document the actual response shape, response time, and whether repeated polling produces 429s.

### C3: Timezone inconsistency will cause bugs

The active window is stored in UTC in `config.yaml` (`start: "11:00"` / `end: "04:00"`), but quiet hours are stored in AEST (`start: "03:00"` / `end: "07:00"`). These two systems will interact incorrectly in the futures collector when it checks both. **All times in config must be in the same timezone (UTC). Convert display to AEST only in the dashboard.**

### C4: Active window UTC midnight crossing is broken

`start: "11:00"` and `end: "04:00"` — end is numerically less than start. A naive `start <= now <= end` comparison will never be true. The midnight-crossing logic must be explicitly handled:
```python
# Correct midnight-crossing check
if start_utc <= end_utc:
    active = start_utc <= now_utc <= end_utc
else:
    active = now_utc >= start_utc or now_utc <= end_utc
```
This is not an edge case — it fails every single day.

---

## Significant Gaps

### G1: No "monitor the monitor" — silent failure is undetected

If Sentinel itself goes down, nothing tells you. The `healthcheck.py` script is mentioned but not wired to anything. You will discover the system is down when you check your phone and notice you haven't had an alert in 6 hours.

**Mitigation:** Add a heartbeat cron job that sends a silent ntfy notification every 60 minutes confirming all collectors are alive. Absence of heartbeat = system down. The `/health` dashboard page alone is insufficient because you have to go and look at it.

### G2: No startup ordering in systemd — race conditions on boot

The five service files are listed but no `After=` or `Requires=` dependencies are specified. If `sentinel-alerter` starts before Redis is up (or if Redis isn't even listed as a dependency), it will crash and systemd will retry it with backoff. On a fresh boot, this creates a window where the first few events are missed.

**Required:** All sentinel services must declare `After=redis.service` (or `After=network.target` if Redis is removed). Services should also declare `Requires=` for hard dependencies.

### G3: Missed posts on restart — no backfill on startup

After a restart or outage, the Truth Social collector loads `last_post_id` from SQLite and polls from there. But it doesn't check for posts published *during* the downtime. If there's a 15-minute crash window and Trump posts during it, the system silently misses it.

**Recommendation:** On startup, fetch the last N posts (e.g., 20) and compare against `last_post_id`. If any have a higher ID than last seen, alert on them immediately (flagged as "recovered"). This is the difference between "alert was delayed" and "alert was silently dropped."

### G4: Polymarket wallet age queries will queue up under load

The spec calls for a Polygon RPC query on every new wallet to determine age. A single Polygon RPC call takes 100–500ms. During a fast-moving market event, many new wallets may appear in the same poll window. If 10 new wallets appear in one 30-second poll, the collector either blocks for 5 seconds of sequential RPC calls or drops the queue.

**Mitigation:** Cache wallet ages in SQLite keyed by wallet address (if you've seen a wallet before, you already know its age). For new wallets, fire the RPC call async and process results in the next poll if needed. Also consider using the Polygonscan HTTP API instead of raw RPC — simpler JSON response, no Web3 dependency.

### G5: `web3` dependency is disproportionate

`web3.py` is 20MB+ with heavy Ethereum dependencies (cryptography, eth-abi, eth-account). It's used solely to determine wallet first-transaction date. This can be replaced with a simple HTTP call to the Polygon public RPC:
```python
# No web3 needed — plain JSON-RPC
requests.post(polygon_rpc, json={"method": "eth_getTransactionCount", "params": [wallet, "earliest"]})
```
Or query Polygonscan's free API. Eliminate the `web3` dependency entirely.

### G6: Config hot-reload is implicit and misleading

The dashboard `/config` page saves to `config.yaml`, but the spec says "collector picks up new value on restart." The `/config` page implies threshold changes take effect, but they silently don't until you manually restart the service. This will cause confusion when threshold tuning.

**Options:**
- Make the dashboard `/config` page trigger a `systemctl restart sentinel-*.service` after saving (requires the Flask process to have the right permissions), or
- Make the `/config` page explicitly say "Changes take effect on next service restart" and add a "Restart collectors" button.

### G7: Data flow ownership is ambiguous

The PRD says "all events are written to SQLite for threshold tuning and history" but never specifies *which component* writes them. Does each collector write to SQLite directly? Does the alerter write to SQLite? Both?

**Clarify:** Collectors should write to SQLite directly at detection time (authoritative record). The alerter updates `alerted = true` when it dispatches. This way, if the alerter fails, the event is still recorded. The current architecture diagram doesn't make this clear.

---

## Clarity Issues

### CL1: `quiet_min_priority` is ambiguous

The config key `quiet_min_priority: MEDIUM` could mean "suppress alerts below MEDIUM priority during quiet hours" or "suppress MEDIUM and above during quiet hours." These are opposite behaviours. Rename to `quiet_suppress_below: MEDIUM` and add a comment.

### CL2: `apscheduler` isn't the right tool

The dependency list includes `apscheduler`, but each collector is its own process running a single polling loop. `apscheduler` is designed for in-process job scheduling across multiple jobs. A simple `asyncio` loop with `await asyncio.sleep(interval)` is cleaner and has no external dependencies. Drop `apscheduler`.

### CL3: Alpaca fallback is underspecified

The PRD mentions Alpaca as a fallback for Yahoo Finance but doesn't specify where the API key goes in `config.yaml`, how the fallback is triggered (automatic? manual config change?), or what the free tier rate limits are. If Alpaca is a legitimate fallback path, it needs its own config section and a sentence on how to obtain a free API key.

### CL4: Truth Social 8-second polling — connection overhead not addressed

8 seconds × 24 hours = 10,800 HTTP requests/day from a single IP to a public API. With no auth. This is the footprint of a scraper. The PRD should acknowledge this and specify connection keep-alive (httpx has persistent connections by default) and whether to expect rate limiting at this cadence. If Truth Social returns 429, the current spec says nothing about exponential backoff.

---

## Minor Issues

- **No `.gitignore` specified.** `config.yaml` with ntfy topic, `sentinel.db`, `venv/` should all be gitignored. Add a sample `.gitignore` to the project structure or install plan.
- **Polymarket market list maintenance.** When a market resolves (e.g., `us-iran-ceasefire-2026` in April), the collector will start hitting an endpoint that returns resolved market data. Define what happens: log a warning and skip? Auto-remove from tracking list? Require manual config update?
- **The daily digest time (7:00am AEST) is hardcoded** in section 5.3 prose but not in the `config.yaml` example. Add `digest_time_aest` to the config.
- **`init_db.py` is referenced in the install plan** but not in the project structure or scripts directory. Add it.
- **`config.yaml.example` is referenced in the install plan** ("Copy `config.yaml.example` to `config.yaml`") but is also absent from the project structure tree. Add it.
- **Dashboard LAN access with no auth should be an explicit accepted risk** in the risk table, not just implicit in the spec.

---

## Additional Issues Not Yet Addressed

### A1: SQLite concurrent writes will cause `database is locked` errors

Four collectors run as separate processes and all write to the same `sentinel.db`. SQLite's default journal mode serialises writes with file-level locks. Under normal conditions this is fine, but during a fast-moving event where all four collectors fire within the same second, write contention will cause `OperationalError: database is locked`.

**Fix:** Enable WAL (Write-Ahead Logging) mode in `init_db.py`:
```python
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")
```
WAL allows concurrent reads alongside writes and dramatically reduces lock contention. This is a one-line fix but must be done before any production data is written.

### A2: yfinance futures data delay is understated

The PRD states Yahoo Finance data has "occasional gaps and delays of 1–2 minutes." In practice, Yahoo Finance's free tier for futures (`CL=F`, `ES=F`) has delays of **10–20 minutes** outside US market hours and can have gaps during low-liquidity periods. Relying on 1-minute candles from yfinance for a spike detection system may result in alerts arriving 15+ minutes after the actual event.

**Implication:** If the futures collector is meant to precede or correlate with a Truth Social post (the stated motivation), a 15-minute data delay makes it a lagging indicator, not a leading one. The Alpaca fallback should be treated as the primary source, not a fallback.

### A3: Futures contract roll dates will trigger monthly false positives

`CL=F` and `ES=F` are continuous front-month contract symbols. On roll dates (when the front month expires and the next contract becomes front-month), volume patterns are highly anomalous — the expiring contract volume collapses while the new front-month surges. This will fire a volume spike alert every single month regardless of any market-moving news.

**Mitigation:** Add roll date detection to the futures collector. The CME publishes expiry schedules; alternatively, detect roll dates by monitoring a sudden change in open interest alongside volume. At minimum, document the known monthly roll dates in the config and suppress or flag spike alerts on those days.

### A4: Async/sync consistency across collectors is unspecified

The PRD lists `httpx` as the HTTP client for all collectors and recommends dropping `apscheduler` in favour of `asyncio.sleep()`. However, it never specifies whether collectors use the `httpx` async client (`httpx.AsyncClient`) or the sync client. If collectors use async, they each need their own event loop (`asyncio.run()`). If they use sync, `asyncio.sleep()` is the wrong primitive (use `time.sleep()`).

**Must specify:** Either all collectors are `asyncio`-based (async httpx, `asyncio.sleep`) or all are synchronous (sync httpx, `time.sleep`). Mixing them within a collector causes subtle bugs. For a simple polling loop, synchronous is easier to reason about and debug.

### A5: Truth Social account ID resolution has no documented fallback

Test TS-01 confirms the account ID is resolved on startup from the handle, but this is a network call that can fail. If Truth Social is unreachable at startup, the collector crashes before it has ever polled. No fallback is specified.

**Recommendation:** Hardcode the known account ID for `@realDonaldTrump` as the default value in `config.yaml`. Attempt to resolve it on startup to verify it hasn't changed; if the resolution call fails, log a warning and use the hardcoded value. This avoids a startup dependency on Truth Social being reachable.

### A6: No version pinning strategy for requirements.txt

The dependencies table lists packages but no versions. `yfinance` in particular has a history of breaking changes between minor versions, and `web3` (if kept) is notorious for this. Without pinned versions, `pip install -r requirements.txt` six months from now may install incompatible versions.

**Recommendation:** Add a note to the install plan to run `pip freeze > requirements.txt` after the initial working install, and commit that pinned lockfile. Consider adding a separate `requirements-dev.txt` for testing dependencies.

---

## Architecture Recommendation

The current architecture is:

```
Collector → Redis pub/sub → Alerter (sends ntfy) + DB writes
```

A simpler and more reliable architecture for this use case:

```
Collector → SQLite (write event, alerted=false)
Alerter → SQLite poll (read unalerted events, send ntfy, update alerted=true)
```

This eliminates Redis, eliminates message loss, and simplifies debugging (your entire system state is in one SQLite file). The latency tradeoff is 1–3 seconds on alert dispatch — negligible versus the existing 8–60 second collection intervals.

**If Redis is kept:** switch from pub/sub to Redis Streams for delivery guarantees.

---

## Priority Summary

| Issue | Priority | Effort |
|---|---|---|
| Validate Truth Social API (C2) | MUST DO FIRST | 1 hour spike |
| Fix midnight-crossing window bug (C4) | Critical | 30 mins |
| Fix timezone inconsistency (C3) | Critical | 1 hour |
| Decide Redis vs SQLite polling (C1) | Critical | Architecture decision |
| Add systemd service dependencies (G2) | High | 30 mins |
| Add heartbeat/watchdog (G1) | High | 2 hours |
| Handle backfill on restart (G3) | High | 2 hours |
| Cache wallet ages / drop web3 dep (G4, G5) | Medium | 3 hours |
| Clarify data flow ownership (G7) | Medium | PRD update only |
| Config hot-reload behaviour (G6) | Medium | 1 hour |
| Drop apscheduler (CL2) | Low | 1 hour |
| Alpaca fallback spec (CL3) | Low | PRD update only |
| Enable SQLite WAL mode (A1) | High | 15 mins |
| Treat Alpaca as primary data source, not fallback (A2) | High | PRD update + config |
| Handle futures contract roll dates (A3) | Medium | 2 hours |
| Specify async vs sync consistency (A4) | Medium | PRD update only |
| Hardcode fallback account ID (A5) | Low | 30 mins |
| Pin requirements.txt versions (A6) | Low | 15 mins |

---

_The PRD is solid for a personal project. Fix the four critical issues before writing code. The architecture decision on Redis vs SQLite polling is the highest-leverage call — getting it right now avoids a refactor later._

---

## Senior Futures Trader Review — Market Practitioner Perspective

_The architecture review above addresses whether the system will run. This section addresses whether it will be useful when it does._

---

### T1: 1-minute candle resolution is too coarse — this is a lagging system, not a leading one

The $580M WTI spike mentioned in the PRD motivation occurred in a single minute. By the time a 1-minute candle closes and yfinance delivers it (with a 10–20 minute free-tier delay), you are 11–21 minutes behind the event. The move is already in — you are not catching the signal, you are documenting the aftermath.

**What you actually need:** Tick-level or 1-second trade data. CME Globex provides this via CME DataMine (paid) or via broker APIs. Alpaca Markets (free tier) provides sub-second trade data for US equities and futures. If the purpose of the futures collector is to detect unusual activity *before* a news catalyst, the data source must deliver in real time, not minutes later.

**Consequence if not fixed:** The futures collector, as specced, will alert you that something already happened. It will not help you act before the crowd.

---

### T2: Instrument coverage is too narrow — WTI and S&P 500 miss most of the geopolitical trade

The March 2026 US-Iran episode had price impact across at minimum: Brent crude (often leads WTI for Middle East events), natural gas (pipeline/LNG disruption risk), defence stocks (LMT, RTX, NOC — move on escalation), DXY (dollar strengthens on safe-haven flows), gold, and tanker/shipping equities (FRO, STNG, DHT). Watching only WTI and ES means the system misses the multi-leg opportunity that practitioners actually trade on geopolitical events.

**Minimum additions to justify building this:**
- `BZ=F` — Brent crude (better proxy for Middle East supply risk than WTI)
- `NG=F` — natural gas
- `GC=F` — gold (safe-haven indicator, confirms risk-off)
- `DX-Y.NYB` — dollar index (confirms risk-off / risk-on)

**Optional but high-value:** unusual options activity on USO, XOP (oil ETF and oil producers ETF). Heavy call buying before price moves is often detectable via options flow services like Unusual Whales (has a free API tier).

---

### T3: The correlation detector is in v2 — it should be in v1

The entire stated motivation for this system is that Polymarket AND futures moved together before the Trump post. The core thesis is: *correlated multi-market anomalies are the signal; single-market anomalies are noise.* The correlation detector is currently buried in v2 as a future feature. This inverts the product logic.

A system that alerts on Polymarket volume spikes and futures volume spikes independently will generate noise. A system that only fires a CRITICAL alert when both are moving within the same 10-minute window generates signal. This is not a machine learning problem — it is a simple time-window join on the signals table (`WHERE created_at > NOW() - 10 minutes AND source IN ('polymarket', 'futures_oil')`). It is half a day of work. Move it to v1.

---

### T4: No options market monitoring — options flow precedes underlying moves

In CME crude oil markets, large directional bets frequently appear in options (CL options, USO options) before the underlying futures move. Institutional traders and alleged insiders tend to use options because: (a) leverage is higher, (b) position is less visible in aggregate volume data, (c) premium paid is bounded loss. The PRD monitors only futures volume but ignores the options market entirely.

**Free monitoring options:**
- Unusual Whales API (free tier) — flags unusual options activity by instrument
- Market Chameleon (free) — unusual volume scan for options
- CBOE LiveVol — more expensive, more comprehensive

Adding `unusual_options_activity` as a fourth collector signal type — or even as a manual nightly check — would significantly increase the system's signal quality.

---

### T5: The Polymarket signal logic monitors buying — it should also monitor hedging patterns

The current Polymarket collector flags: large bets, new wallets, odds moves, volume spikes. These are the obvious signals. Less obvious but higher-signal: wallets that place *both sides* of a market in close succession (YES and NO on the same event within minutes). This is a pattern consistent with a trader hedging an existing off-chain position or testing liquidity before a larger move. It appears in on-chain data and is detectable.

Additionally, the spec monitors *Polymarket only*. Kalshi (US-regulated prediction market) now covers geopolitical events including military actions, executive orders, and sanctions. Smart money that avoids Polymarket for regulatory reasons uses Kalshi. Consider adding Kalshi to the collector list — it has a documented REST API.

---

### T6: The alert-to-execution pipeline is undefined — and it is the whole point

The PRD says "all trading decisions remain the user's own." That is correct. But it says nothing about what the user is supposed to do with an alert at 1:30am when WTI futures are spiking. There is no defined process from: alert received → decision made → order placed.

The realistic timeline from push notification to confirmed fill is:
1. Wake up / see notification — 30–120 seconds (assuming phone is nearby)
2. Open broker app — 10–20 seconds
3. Navigate to instrument — 10–20 seconds
4. Assess the situation, size the trade — 30–120 seconds (assuming no analysis paralysis)
5. Submit order — 5–10 seconds
6. Confirm fill — 5–30 seconds

Total: **1.5–5 minutes minimum** from alert to fill. Given that the March 2026 WTI spike lasted approximately 1 minute before reaching its extreme, this system will not get you into the initial spike move. It might get you into the second leg or a mean-reversion trade. The PRD should acknowledge this honestly: Sentinel is a *reaction aid*, not a front-running tool.

**Practical recommendation:** Pre-configure broker watchlists and saved orders. The alert format should include a direct deep link to open the relevant instrument in the broker app. Some brokers (Interactive Brokers, for example) support URL schemes that pre-populate orders.

---

### T7: CME Globex session structure is not accounted for

WTI crude (CL) and S&P e-Mini (ES) both trade on CME Globex, which operates nearly 23 hours/day. However, liquidity is highly non-uniform:

| Session | Time (US Eastern) | WTI Liquidity |
|---|---|---|
| Globex overnight | 6pm–9am ET | Thin — wide spreads |
| Floor pre-open | 7am–9am ET | Building |
| Pit session open | 9am ET | Surge in volume |
| Regular session | 9am–2:30pm ET | Full liquidity |
| Post-close | 2:30pm–6pm ET | Declining |

A 3x volume spike during the Globex overnight session (which is 9am–2am AEST, exactly the "active window" in the PRD) can be triggered by very low absolute volume. 1,000 contracts at 3am ET will produce a 3x spike versus a 333-contract baseline — and that is not a meaningful signal. The volume spike threshold must account for absolute volume, not just relative multiplier. **Add `min_absolute_volume_contracts` to the futures config.**

---

### T8: No historical backtesting against known geopolitical events

The motivation cites one event (March 2026). Before building and running this system live, validate that the proposed signals would have actually fired on historical geopolitical events where documented price moves occurred:

| Event | Date | Instruments affected |
|---|---|---|
| Soleimani assassination | Jan 3, 2020 | WTI +4%, gold +1.5%, defence +2% |
| Russia-Ukraine escalation | Feb 24, 2022 | Brent +8%, NG +10%, wheat +5% |
| Gaza escalation (Oct 7, 2023) | Oct 7, 2023 | Oil +4%, gold +1%, defence +3% |
| Trump Iran tariff tweet (2019) | Multiple | WTI volatile |

If the futures volume collector would not have fired on the Soleimani assassination, it will not fire on the next comparable event. Historical OHLCV data for these events is freely available via yfinance (historical data, not delayed). Run a one-day backtest before committing to the threshold values.

---

### T9: Legal and regulatory section is materially incomplete

The current risk table has a single row: "CGT / legal complexity" rated as Low likelihood. This understates the real risk profile. In Australia, the relevant regulatory considerations include:

- **ASIC market manipulation provisions (Corporations Act s1041A):** Trading in a manner that creates a misleading appearance of market activity is prohibited. Rapid response trading to Polymarket signals could raise questions if positions are large enough to move the market.
- **Insider trading (s1043A):** If any of the Polymarket signals or Truth Social leads turn out to be derived from material non-public information (MNPI) — even if the user didn't know — civil and criminal liability can attach to the trades. The disclaimer says "nothing in this system constitutes financial advice" but says nothing about insider trading risk.
- **Record-keeping obligations:** Sophisticated investors trading futures on CME from Australia may have ATO reporting obligations under the foreign income reporting rules and CFD/derivatives disclosure rules.

**Recommendation:** Add a dedicated Legal/Regulatory section to the PRD (not just a row in the risk table) with the specific statutory provisions and a note that this should be reviewed with a securities lawyer before live use.

---

### T10: No consideration of signal decay or position exit

Sentinel is designed to get you *into* a trade. No thought has been given to when to exit. Geopolitical events frequently partially reverse within hours or days as initial reactions are digested. A system that alerts you to enter but gives no signal for exit (or even a framework for thinking about exit) is incomplete for practical trading use.

**Minimum addition:** Add a "signal decay" concept — the system should track how the monitored instruments performed *after* each signal fired and store this in the database. Over time, this builds a personal signal performance log that can be reviewed to calibrate both thresholds and holding periods.

---

### Finance Practitioner Priority Summary

| Issue | Priority | Effort |
|---|---|---|
| Move correlation detector to v1 (T3) | CRITICAL | Half-day |
| Add Brent, natgas, gold, DXY as instruments (T2) | High | 2 hours config + collector update |
| Add absolute volume floor to futures thresholds (T7) | High | 30 minutes config + logic |
| Define alert-to-execution pipeline (T6) | High | PRD update + broker setup |
| Fix data latency — evaluate Alpaca/real-time source (T1) | High | 1 day investigation |
| Add historical backtest validation (T8) | Medium | Half-day analysis |
| Add Kalshi as prediction market source (T5) | Medium | 1 day collector addition |
| Expand legal/regulatory section (T9) | Medium | PRD update + legal review |
| Add options flow monitoring (T4) | Low | 2 days (new collector) |
| Add signal decay tracking to DB schema (T10) | Low | 2 hours |

---

_A technically perfect system built on the wrong data resolution and the wrong instrument set will not help you trade geopolitical events. Fix T1, T2, T3, and T7 before anything else — they determine whether the system has genuine edge or is just an expensive notification app._
