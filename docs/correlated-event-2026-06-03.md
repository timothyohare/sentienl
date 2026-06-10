# Correlated Event — 2026-06-03 00:13 UTC

> Investigation of the single `correlated_signal` (CRITICAL) that Sentinel's
> correlation detector emitted. Reconstructed from `sentinel.db` on 2026-06-10.

## What the alert claimed

```
id:        2545
source:    correlation_detector
priority:  CRITICAL
summary:   CORRELATED: truth_social,kalshi,futures_sp500,futures_gold within 10 min (4 sources)
payload:   {"sources": "truth_social,kalshi,futures_sp500,futures_gold",
            "source_count": 4,
            "window_minutes": 10,
            "anchor_signal_id": 2531,
            "anchor_time": "2026-06-03T00:09:41Z"}
created:   2026-06-03T00:13:16Z
```

The anchor (signal 2531) is a **Trump Truth Social post** at `00:09:39Z` —
media-only, no text captured (`has_media: true, text: ""`).

## ⚠️ Headline correction: it was NOT a real 4-source event

The alert is **a false positive**. Two of the four claimed sources were stale.

**All futures signals in the entire database are from June 2:**

| Time (UTC)     | Source        | Detail                                  |
|----------------|---------------|------------------------------------------|
| 06-02 12:18    | futures_sp500 | ES=F 309 contracts (11.14x)             |
| 06-02 17:19    | futures_gold  | GC=F 215 contracts (91.49x)             |
| 06-02 17:20    | futures_gold  | GC=F 121 contracts (9.24x)              |
| 06-02 17:20    | futures_sp500 | ES=F 896 contracts (18.12x)             |
| 06-02 21:31–36 | futures_sp500 | ES=F 2,369 contracts (same event ×6)    |

The **latest** futures signal is `06-02 21:36` — roughly **2.5 hours before**
the anchor at `06-03 00:09`. They were nowhere near the claimed 10-minute
window, yet the detector counted `futures_sp500` and `futures_gold` as
contributing sources.

**Conclusion: the correlation detector's time-window logic is broken.** It is
matching signals far outside `window_minutes`, which is exactly what inflates a
2-source cluster into a CRITICAL "4 sources" alert. This bug needs fixing before
any correlation alert can be trusted. (See `correlation_detector.py` — the
self-join's time bound is the prime suspect.)

## What *actually* happened in the window (00:00–00:20 UTC)

Stripping out the stale futures, the genuine activity around the anchor was a
**2-source cluster, both pointing at Taiwan**:

1. **Kalshi — sustained volume spike (≈28x)** on
   *"Will the U.S. State Department issue a Level 4 warning for Taiwan before
   Jul 1, 2026?"* — elevated for the entire 20-minute window.

2. **Kalshi — odds move** at `00:09:45`, seconds after the anchor post:
   *"Will the U.S. State Department issue a Level 4 warning for Taiwan before
   Jan 1, 2030?"* — **YES 56% → 49% (-7pp)**.

3. **Truth Social — 3 Trump posts in ~90 seconds**: `00:08:20`, `00:08:32`,
   `00:09:39` (the anchor). **All media-only, no text** — content not in the DB.
   URLs:
   - https://truthsocial.com/@realDonaldTrump/116683263212441795
   - https://truthsocial.com/@realDonaldTrump/116683263969861999
   - https://truthsocial.com/@realDonaldTrump/116683268398932119

   **The anchor post's image (confirmed manually) is a candidate-endorsement
   graphic: "Congressman (NJ-07) Tom Kean Wins, endorsed by President Trump" —
   nothing to do with Taiwan.** These are routine endorsement posts, the same
   genre as the 58 that flooded in on first run.

### So the "kernel" is also coincidental

Once the anchor is known to be a Tom Kean endorsement, the cluster collapses
entirely. There was **no real correlation at all** — just two unrelated things
happening within 90 seconds:

- Trump posting routine endorsement graphics, and
- Independent, ongoing Kalshi activity on Taiwan travel-warning markets.

The detector chained them together because (a) it counted 2.5h-stale futures and
(b) it treats *every* Trump post as a high-priority anchor. The Taiwan market
activity is still real and interesting on its own — but it was **not** connected
to the Trump posts. This is a pure false positive, top to bottom.

## The Taiwan thread across the whole run

The Taiwan Level 4 warning markets were the most active story in the dataset.
All four `odds_move` signals in the entire DB are Taiwan markets:

| Time (UTC) | Market                          | Move                |
|------------|----------------------------------|---------------------|
| 00:09:45   | Level 4 Taiwan before Jan 1 2030 | YES 56% → 49% (-7pp)|
| 07:41:52   | Level 4 Taiwan before Jan 1 2030 | YES 49% → 54% (+5pp)|
| 14:13:21   | Level 4 Taiwan before Jan 1 2030 | YES 54% → 47% (-7pp)|
| 20:38:20   | Level 4 Taiwan before Jan 1 2029 | YES 45% → 38% (-7pp)|

Plus `large_bet`s on *"Level 4 warning for Taiwan before Jan 1, 2030"*
(176–179 contracts, YES). There was genuine, sustained money moving on Taiwan
travel-warning risk on June 3 — the most coherent signal in the run.

## Takeaways

- **The flagship feature fired a false CRITICAL — fully spurious.** The anchor
  was a Tom Kean endorsement graphic, two of the four "sources" were 2.5h-stale
  futures, and the Taiwan market activity was unrelated to the Trump posts.
  Nothing here actually correlated.
- **Two independent bugs combined to manufacture it:** (1) the time-window bound
  counts signals far outside `window_minutes`, and (2) every Trump post is
  treated as a high-priority anchor, so routine endorsements seed "events."
- **The Taiwan thread is still genuinely interesting on its own** — sustained
  Kalshi volume + odds moves + large bets on travel-warning markets. That's
  worth surfacing; it just wasn't a cross-source correlation.
- **Capture Trump post media/text.** The post was media-only and we logged no
  text — we only learned it was a Tom Kean endorsement by opening it manually.
  Need OCR or at least to store/caption media so the detector isn't anchoring on
  unknown content.
