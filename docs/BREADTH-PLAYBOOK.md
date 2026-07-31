# The Breadth Playbook — why 2026-07-30 made $30 in a day, and how to keep it

*Written 2026-07-31, after the roi-v4 era's first full day realized **+$30.17** (research)
and **+$9.68** (live) — versus the $1-3/day the book had shrunk to the week before.
Companion evidence: the 28-agent adversarially-verified regression investigation of
2026-07-29 (every counterfactual scorer validated 281-756/756 exact against engine
`realized_pnl`), and PRs #80 (v4 + live phantom-budget fix) and #81 (v5 scale-up).*

## The one-sentence law

**Maker P&L = (number of concurrently resting quotes) × (tape that trades through them)
× (edge per contract). Per-order size only matters up to what the tape will fill; the
reservation each oversized quote charges against the shared caps is what silently
kills the first term.**

Every maker order this book places improves the bid by exactly 1 cent and rests with
zero queue ahead (verified on all 553 orders of the 07/19-29 window). Displayed depth
has **no causal path** to a maker fill — only public tape trading through the level
fills it. So volume is bought with *breadth in front of the tape*, not with size.

## What the winning day actually looked like

The four orders that produced most of the $30.17, all `maker_trade_through_required`,
all NO-side, all placed **overnight** (00:30-02:25 UTC) at **cheap-favorite** limits,
all **filled to the full request**, all exited intraday 17-25 cents higher:

| ticker | limit | filled | exit | P&L |
|---|---|---|---|---|
| KXHIGHTBOS-26JUL30-T68 | 0.56 | 53/53 | 0.81 | +$12.68 |
| KXHIGHTHOU-26JUL30-T96 | 0.68 | 40/40 | 0.93 | +$9.94 |
| KXHIGHTDAL-26JUL30-T100 | 0.72 | 41/41 | 0.89 | +$6.69 |
| KXHIGHTHOU-26JUL30-T96 | 0.68 | 44/44 | 0.93 | +$0.86 |

Three structural facts to preserve:

1. **The cheap-favorite band (0.55-0.75) carries the big per-contract upside.** A
   56-cent entry has ~44 cents of room; a 92-cent deep favorite has 8. The deep
   0.92+ band is fine volume filler but cannot produce large motions.
2. **Overnight resting into the morning tape is where full fills happen.** The
   winners rested for 6-12 hours and were swallowed by tape bursts.
3. **Every winner was request-truncated** — the tape carried more than we asked for
   (BOS traded 154 contracts through the window; we captured 53. DAL traded 262; we
   took 41). In the v1 era too, 19/59 fills hit the full request. On burst days the
   position budget, not the market, caps the capture.

## The two mistakes that made the book "conservative" ($1-3/day), never again

1. **v3 (07/26-29): size up without scaling the shared caps.** 8% position budget
   turned every request into ~90 contracts reserving ~$81, which exhausted the
   city/region/aggregate caps and cut concurrent resting quotes 9.6 → 4.3. Measured
   cost of the knob alone: $2.70/day (95% CI $0.30-6.15). Size went up, money went
   down. **Never raise the position budget without raising the shared caps by the
   same factor.**
2. **Live phantom budget (fixed in #80): budgeting risk you never place.** The
   allocator debited the pre-clamp $84 recommendation against the $84 daily budget
   while placement clamped every leg to $30 — one leg per allocation, 1103/1106
   measured. After the fix: 0 budget-skips across 106 approvals and a $9.68 day.

## How to scale from here (the v5 rule)

When the evidence says size binds — the signal is a **high request-truncation rate**
(fills hitting the full request) plus **shared-cap capacity hits** in the decision
log — scale **every dollar knob by the same factor** so the ratios that fund breadth
are preserved. v5 (PR #81) is exactly this: position $30→$45, city $60→$90, region
$120→$180, aggregate $250→$375, pause 10%→12%, ratios identical (~8.3 concurrent
quotes, 2 per city), 5%/day KPI unchanged.

Watch after each scale step, in order of importance:
- **request-truncation rate on filled orders** (still high → room to scale again;
  collapsed toward v3-style 0.4% fill-through → step back),
- **"research account capacity below requested spend" count** (should stay near
  zero on ordinary days; 30/day was the v4 busy-day reading),
- **concurrent resting quotes** (must hold ~8+; if it sags, the caps are binding
  and the geometry has drifted toward v3),
- the **daily loss pause** is the designed downside bound ($120 at v5) — large
  motions are the goal, and this is the one brake that must never be loosened in
  the same change that raises size.

Scaling is cheap to evaluate: each step is one policy constant set (a new frozen
`TARGET_POLICY_Vn`, account cutover self-bootstraps) and one day of tape gives a
read. The ledger history of v1→v5 is the experiment log.

## Honesty about the sample

The +$30.17 day is **n=1**. The v1 era is the larger evidence base for this geometry:
six trading days at $5.07–$10.98, mean ~$8/day. So the defensible statement today is
"this geometry reliably produces high-single-digit days, and produced one $30 day once
the caps were also raised" — not "it produces $30/day." Each scale step needs its own
days of tape before its mean is real. Keep the per-era ledgers (v1…v5) intact: they are
the only thing that lets a later reader tell a mechanism from a lucky Tuesday.

## Operational trap that cost a trading day (2026-07-31)

`sync_to_box.sh` **disables every timer at the start** and only restores them on success.
So a deploy that fails repeatedly leaves the box **quiesced — trading stopped and
`weather.db` going stale** — and the deploy's own final gate is the forecast-freshness
watchdog, which then fails because of the staleness the failed deploys caused. That is a
deadlock: retrying alone can never clear it.

Break it manually, in this order:
1. Stop the retry loop and the in-flight `sync_to_box.sh` (safe if it is still in the
   pre-rsync backup/integrity phase — check for the `PRAGMA integrity_check` process).
2. Delete the orphaned same-day snapshot and `.restore-check.*` dir it left behind
   (~11 GB each; they space-block the next attempt).
3. `systemctl start sfo-forecaster-refresh.service` to make `weather.db` fresh.
4. Re-enable the canonical 12-timer set (the list is in `install_systemd.sh`).
5. Let `sfo-operational-publish` run once, then confirm the freshness watchdog returns
   `success`.

Related trap: because the deploy *captures* the enabled timer set at start, running a
deploy from an already-quiesced box records an **empty** policy and would leave the box
dead on success. Always restore the 12 timers before the next deploy.
