# The Breadth Playbook — how large Research ROI days happen

*Written 2026-07-31, after the roi-v4 era's first full day realized **+$30.17** (research)
and **+$9.68** (live) — versus the $1-3/day the book had shrunk to the week before.
Companion evidence: the 28-agent adversarially-verified regression investigation of
2026-07-29 (every counterfactual scorer validated 281-756/756 exact against engine
`realized_pnl`), and PRs #80 (v4 + live phantom-budget fix) and #81 (v5 scale-up).*

*Updated 2026-08-05 after Research ROI v6 realized **+$44.5025** in one Pacific
day. The dated audit is in
[Research ROI v6 case study — 2026-08-05](RESEARCH-ROI-V6-2026-08-05.md). Both
large days are paper-trading case studies, not promises of a daily return.*

## The one-sentence law

**Maker P&L = (number of concurrently resting quotes) × (tape that trades through them)
× (edge per contract). Per-order size only matters up to what the tape will fill; the
reservation each oversized quote charges against the shared caps is what silently
kills the first term.**

Every maker order this book places improves the bid by exactly 1 cent and rests with
zero queue ahead (verified on all 553 orders of the 07/19-29 window). Displayed depth
has **no causal path** to a maker fill — only public tape trading through the level
fills it. So volume is bought with *breadth in front of the tape*, not with size.

## What the 2026-07-30 winning day actually looked like

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
2. **Overnight placement can expose the book to later tape, but each maker request
   has a 15-minute lifetime.** The winning positions remained open for hours after
   filling; the individual quotes did not rest for 6-12 hours. Repeated five-minute
   scans create fresh, separately auditable requests.
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

## The v6 ceiling and evidence required for v7

When size evidence is strong, every dollar cap has to move together so the ratios
that fund breadth remain fixed. v5 moved to $45 position / $90 city / $180 region /
$375 aggregate with a $120 daily-loss pause. v6 moved to **$90 / $180 / $360 /
$750 with a $150 pause**, preserving about 8.3 position slots and two positions per
city. The fixed $1,000 reference equity and $50 paper-research KPI did not change.

v6 is the current ceiling, not an invitation to keep doubling. On the 2026-08-05
Houston winner, v6 requested 147 contracts at $0.61 but public tape filled only
94.1. A $45 v5 request would have filled 73; a $90 v6 request captured the extra
21.1; requests above v6 would still have filled only 94.1 while reserving more
shared capacity. Across the audited v6 trade-through fills, 2 of 18 positive-fill
requests reached the full requested amount (11.1%). That small denominator is a
study trigger, not activation authority.

Watch prospectively, in order of importance:

- full-request capture with both **positive-fill and all-request denominators**;
- requested contracts versus tape-capped fills, including every zero-fill expiry;
- concurrent resting breadth, city/region/aggregate capacity rejections, and cash
  reservations;
- leave-one-position and leave-one-day-out P&L so one event cannot authorize a
  policy; and
- the **$150 daily-loss pause and -60% catastrophic position floor**, which must
  not be loosened to chase the $50 KPI.

Scaling is easy to code but expensive to conclude correctly. A candidate v7 needs
a frozen policy, point-in-time tape replay that exactly reproduces 1.0× fills,
complete after-fee exit evidence, and a predeclared prospective window. The era
ledgers v1→v6 are the experiment log; one day of tape is never a promotion test.

## 2026-08-05 v6 replication evidence

Research ROI v6 realized +$44.5025, or 4.45025% of its fixed original $1,000
reference. It missed the $50 objective by $5.4975. Seven resolved decisions won,
but Houston supplied +$35.6927—80.20% of the day—and the position endured an
approximately -58% marked drawdown before the existing fresh-model stop veto and
hard -60% catastrophic floor resolved the path. Six prior-target settlements
supplied the remaining $8.8098.

The replication decision is therefore deliberately boring: **preserve v6 exactly**.
Keep day-ahead gates, one-cent maker improvement, five-minute scans, 15-minute quote
lifetimes, two-minute monitoring, partial-fill accounting, model-fair-value exits,
fresh-model veto rules, and the hard floor. Do not force more trades or scale from
this observation. The full position attribution, request curve, risk path, and
prospective protocol are in the
[dated v6 case study](RESEARCH-ROI-V6-2026-08-05.md).

## Honesty about the sample

The +$30.17 v4 day and +$44.5025 v6 day are each **n=1 events**. Through the
2026-08-05 snapshot, v6's six realized days were `-$0.0086, -$16.4757, +$6.4512,
+$1.2050, +$2.5645, +$44.5025`: mean +$6.3732, median +$1.8848, and a day-cluster
bootstrap 95% interval of -$6.7367 to +$23.5259. It hit $50 on 0 of 6 days.

The defensible statement is: **the breadth geometry has now captured two unusually
large paper days in separate policy eras, while v6 repeatability remains unproven.**
Keep every per-era ledger (v1…v6) intact. They are the only way a later reader can
separate mechanism, sizing, market opportunity, and luck.

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
