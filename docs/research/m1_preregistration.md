# M1 market-recalibration paper trial: pre-registration

Registered 2026-11-02, before any code that can place an M1 order exists in the
repository. Everything below is fixed for the whole run. Changing any threshold,
coefficient, filter, stake, cap or stop rule means a new policy version and a new
paper account, never an edit to this one.

**Paper only.** Real money stays off. This trial cannot promote anything to real
money on its own, and it cannot satisfy the 30-date real-money readiness rule:
its account is a research ledger that the readiness clock excludes, and it runs a
different policy from the live candidate.

## 1. What is being tested

The Kalshi daily-high ladders price favorites too low and longshots too high the
day before the target date. M1 recalibrates the market's own same-scan de-vigged
probability with a frozen logistic map and buys the side whose recalibrated
probability beats the all-in taker cost by at least 2 cents. The weather model
does not gate entries; its agreement is recorded on every entry and tested as a
co-primary hypothesis.

The evidence behind the trial is the owner-approved verification decision of
2026-11-02 (`m1-verification-decision.json`, sha256
`bbd540895fcfdfbada5b6626fc42b0b73e1e040ae1ee148bc855507a384afce5`, kept
outside the repository with the research hand-off). Its own summary applies: this
is a test that can catch failure, not a validated edge.

## 2. Identity

| Item | Value |
|---|---|
| Paper account | `paper-research-m1-v1` (new, separate, never pooled with Live Stability, Research V6 or Research V7) |
| Initial capital | $10,000.00 (reference equity 10,000) |
| Policy version | `research-m1-market-recal-v1` |
| Coefficient set | `m1-deploy-coefs-2026-10-28` |
| Published profile key | `research-m1` |
| Placement switch | `PAPER_PLACE_RESEARCH_M1` / `--place-research-m1`, default off (shadow mode records every decision and places nothing) |

The policy fingerprint is a hash of every constant in sections 3 to 7, including
the coefficients, the stake and the loss stop. Any change produces a new
fingerprint, which the code refuses to admit under this account.

## 3. Frozen coefficients

The deployment refit on all data from 2026-07-19 to 2026-10-28. The verbatim
research file is committed beside this document as
`m1_deploy_market_recal_coefs.json` (sha256
`bd373ea3e925b767aa6cba8972407e7638841d2e6dd03e29c24c3ead927bce86`); the
trial uses its `refit_on_all_2026-07-19_to_10-28 (for deployment)` day-ahead
entries. No refit during the run.

| Bucket | Station civil local hour on D-1 | a | c |
|---|---|---|---|
| L1a | hour < 11 | 0.2991 | 1.2792 |
| L1b | 11 <= hour < 15 | 0.2160 | 1.2058 |
| L1c | 15 <= hour < 19 | 0.2247 | 1.2131 |
| L1d | 19 <= hour < 24 | 0.2378 | 1.2439 |

The local hour is hour plus minutes/60 in the station's civil time zone (Phoenix
has no daylight time), taken at the evaluating scan.

## 4. The entry rule

1. **Market probability.** `q_yes` is the scan's same-scan de-vigged ladder
   probability for YES: `market_probability` on a YES leg, `1 - market_probability`
   on a NO leg. Raw mids are never used.
2. **Recalibration.** `p_yes = 1 / (1 + exp(-(a + c * logit(clip(q_yes, 0.005, 0.995)))))`.
   `p_trade = p_yes` for YES and `1 - p_yes` for NO.
3. **Gate cost.** `cost = ask + ceil(0.07 * ask * (1 - ask) * 100) / 100`, the
   one-contract rounding of `fees.quadratic_fee_per_contract`. `edge = p_trade - cost`.
4. **Signal.** `edge >= 0.02`.
5. **Day-ahead only.** The target is the next civil day at the station
   (lead exactly 1 on the station's civil calendar).
6. **Sampling: the first research-profile scan of each UTC hour.** A leg
   (target date, market, side) is evaluated for entry only at the first
   research-profile scan in each UTC hour that evaluates it. This is the
   validated definition: the research build marked as the sample the lowest
   journal id per (target date, market, side, UTC hour) among research-profile
   rows, with no minute window. The live and research profiles run back to back
   every five minutes, so the research scan of a city often starts several
   minutes into the hour; a strict minute 0-4 window would skip many hours in
   production and is not what was validated.
7. **First crossing, once.** Enter at the first sampled scan where the leg passes
   every filter, risk gate and the signal. At most one entry per (target date,
   market, side), ever: no re-entry and no averaging. YES and NO are evaluated
   independently.
8. **Filters.** Skip the Kalshi listing hour, D-1 10:00-10:59 America/New_York
   (14 UTC under EDT, 15 UTC under EST). Entry bid >= 0.01. 0 < entry ask <= 0.99.
   Side spread <= 10 cents compared in integer cents, `round((ask - bid) * 100) <= 10`.
   Cost < 1. Displayed ask size >= 1 contract. Market active and before its close
   time.
9. **Execution.** Taker: buy the side at the displayed ask on the deciding scan,
   booked as an immediate visible-quote fill. No resting orders.
10. **Sizing.** $10 notional per entry including the order fee: `C` is the
    largest integer with `C * ask + ceil(0.07 * C * ask * (1 - ask) * 100) / 100 <= 10`,
    then `C = min(C, floor(displayed ask size), 25)`. No Kelly scaling.
11. **Fees in P&L.** Kalshi's per-order fee, `ceil(0.07 * C * P * (1 - P) * 100) / 100`,
    on every fill.
12. **Exits.** None. Hold to settlement. The monitor never stops, takes profit,
    holds on a model veto, or catastrophic-exits an M1 position. Settlement runs
    through the normal account-scoped paper settle.

## 5. Risk limits and their dollar values

For this account only, the M1 edge gate replaces the model-based signal gates
(the model point-edge floor, the posterior lower-bound floors and the target
sleeve's lower-bound rule). Every risk limit stays. Percentages apply to this
account's own $10,000 reference equity.

| Limit | Value |
|---|---|
| Stake per entry | $10.00 including the order fee |
| Contracts per market | 25 |
| Position cap (1.5%) | $150 per market and side |
| Event cap (3%) | $300 per city ladder and target date |
| Target-date cap (5%) | $500 per target date across all cities |
| Account capacity | available cash (starts at $10,000.00) |
| Daily-loss pause (15%, the active research sleeve's percentage) | $1,500 of realized loss in one Pacific objective day |
| Cumulative loss stop | -$300.00 of realized P&L (stop rule 2) |
| Max spread | 10 cents (the 0.10 research limit, compared in integer cents) |
| Bid size minimum | 1 contract |
| Model/market gap | at most 0.25 |
| Forecast source spread | at most 7.3 F |
| Forecast age | at most 30 hours |
| Corroborated forecast | entries blocked on a single-source forecast, as for every research entry |
| Duplicate entry | one active position per market and side, and one M1 entry per leg ever |

With $10 stakes the percentage caps and the daily-loss pause do not bind in
practice; the -$300 cumulative stop is the binding loss control. The stake and
the loss stop were calibrated to $10 entries; the larger capital only stops the
percentage caps from throttling the trial.

Research-profile gates not applied to this account: the model point-edge and
lower-bound floors, the posterior floor and the nonfinal-certainty gate (all
model-based signal gates), the cheap-tail support gate (a model-support gate for
asks of 15 cents or less), and the spread-as-a-share-of-ask gate (an exit stop-band
guard; with spreads of at most 10 cents it can only bind below a 20-cent ask,
and the frozen map raises a side's probability above the market's only when that
side's `q` is above about 0.26 for YES or 0.73 for NO, so a sub-20-cent signal
needs an ask several cents below the de-vigged probability).

## 6. Hypotheses and analysis

ROI is total realized P&L over total stake (contracts times ask plus the order
fee) across settled entries. Inference uses a bootstrap that resamples whole
target dates (10,000 resamples, fixed seed). One-sided family-wise alpha 0.05,
split by Bonferroni:

| Hypothesis | Statement | Alpha |
|---|---|---|
| H1 (primary) | M1 ROI > 0 | 0.025 |
| H2a (co-primary) | ROI of the agree subset (`p_model >= cost` at the entry scan) > 0 | 0.0125 |
| H2b (co-primary) | agree-subset ROI minus disagree-subset ROI > 0 | 0.0125 |

The p-value is the share of resampled statistics at or below zero. The agree flag
is recorded on every evaluation and every entry; it is a pre-registered
hypothesis inside the traded book, not a gate.

Descriptive only, no inference: two shadow arms computed online with no paper
fills, (a) the same rule at every five-minute research scan (first crossing per
leg) and (b) the joint first crossing of the hourly rule with the model veto
`p_model >= cost`; results by side, bucket and month; and the date-clustered
confidence intervals.

## 7. Horizon and stop rules

All stop rules halt new entries only. They never close positions, never resume
automatically, and are recorded as account events and published.

1. **Fidelity halt.** A daily reconciliation replays the previous UTC day's
   recorded M1 evaluations through the same rule and compares them with the paper
   entries (same leg, same UTC hour, price within 1 cent). Halt if, in any rolling
   seven-day window, two or more entries fail that match, or if any entry is in
   the ET listing hour, has a spread above 10 cents, has lead other than 1, or
   re-enters a leg.
2. **Loss stop.** Halt when cumulative realized M1 P&L is at or below -$300.
3. **Futility.** After at least 400 settled entries, stop when ROI is at or
   below -5%.
4. **Premise.** Pause when the pooled day-ahead recalibration slope, refit on the
   trailing 30 settled target dates of sampled evaluations, falls below 1.05
   (monthly slopes in the research were 1.16 to 1.29). The check starts once 30
   settled target dates exist.
5. **Final read.** Stop new entries at 1,000 entries or 60 trading days (distinct
   target dates with at least one entry), whichever comes first.

At the final read: continue only if ROI > 0 and no fidelity breach occurred, and
report agree versus disagree under the alpha split above. Nothing is promoted to
real money from this run alone.

## 8. Expected result

Full M1 book under paper fills (hourly, exact fee, clamped to displayed size):
about +3.8% before a multiple-comparisons haircut. After the haircut expect
roughly 0% to +2.5%, central about +1% (about 0.5 points lower at the five-minute
cadence). The date-clustered sampling error is about 2.7 to 3.2 points per 1,000
entries, so a 60-day run can detect failure but cannot separate +1% from zero. A
read of at least +5% over 1,000 or more entries would be needed before anyone
calls it validation. Expected volume is about 15 entries and $110 to $170 of new
stake per day.

## 9. Recorded evidence

The account keeps its own durable record, independent of the decision journal
that production prunes after about a day:

- every sampled evaluation of every day-ahead leg: `q_yes`, `p_yes`, `p_trade`,
  bucket, edge, ask, bid, displayed sizes, spread, `p_model`, the agree flag, the
  coefficient set, the decision and its reason;
- the first crossing of each shadow arm;
- each final CLI high used to resolve recorded legs;
- each daily reconciliation report and every halt.

## 10. Deviations from the validated research, and why

- The listing-hour rule is D-1 10:00-10:59 America/New_York, not UTC 14, which
  was correct only under EDT (the first day-ahead scan for 2026-11-02 was at 15:00
  UTC).
- The spread filter compares integer cents; the float comparison dropped exact
  10-cent spreads.
- P&L books the exact per-order fee, integer contracts and the displayed-size
  clamp; the gate keeps the one-contract fee that the threshold was chosen with.
- The existing research risk limits in section 5 stay binding (the model/market
  gap alone removes about 2 to 9% of entries).
- Initial capital is $10,000 rather than the decision's $1,000, by owner decision,
  so the percentage caps do not throttle the trial.
- The daily fidelity check (section 7.1) replays M1's own recorded evaluations,
  not the research decision journal the decision named, because production
  prunes that journal after about a day. It catches admission errors (wrong
  leg, hour or price, listing hour, spread, lead, re-entry) but cannot catch
  drift between the journal and M1's sample. To keep the two on one clock, M1
  rows carry the time the scan read its quotes, not the time M1 ran.

## 11. Implementation record

Added after the implementation was written; it records how the code realizes
sections 1 to 10 and changes none of them.

- Policy fingerprint `458ecd9f808b595c74356529` (pinned in
  `trading/tests/test_research_m1_math.py`). It hashes only the constants above,
  so release-wide version changes (execution model, strategy behavior) never
  rotate the trial's identity; M1 lots record the release's execution model
  version like every other order.
- Code: `trading/sfo_kalshi_quant/research_m1.py` (frozen policy, rule, replay,
  statistics), `trading/sfo_kalshi_quant/research_m1_store.py` (scan engine,
  admission, reconciliation, stop rules, published payload),
  `trading/sfo_kalshi_quant/store/m1_trial.py` (tables and account bootstrap).
- Sampling: the `m1_evaluations` table's unique key (target date, market,
  side, UTC hour) admits exactly one sampled evaluation per leg per hour, the
  first research-profile scan to evaluate it.
- Placement switch `PAPER_PLACE_RESEARCH_M1` (`--place-research-m1`), default
  off. Daily upkeep runs inside `paper-auto-settle` on the existing settle
  timer; `paper-m1-reconcile` and `paper-m1-status` (`--final-read` for the
  10,000-resample read) run the same steps by hand.
- Halts are zero-amount `M1_HALT` events in the account ledger, one per rule.
- The stop rules, the premise check included, also run while placement is off,
  so a halt recorded in shadow mode keeps placement from ever entering. A leg
  whose first crossing was recorded in shadow mode is never entered later
  (`ALREADY_CROSSED`).
- Published as `accounting.accounts.research_m1` in `strategy_research.json`.
