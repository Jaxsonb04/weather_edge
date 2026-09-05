# Trading measurement follow-up — 2026-09-04

Scope: current local trading source after the September remediation. No runtime
database, order, ledger, timer, or production file was modified in this sub-audit.
Production performance figures in the September 3 report remain dated snapshots.
Live Stability and Research ROI remain economically separate paper accounts.

## Corrected locally: exit reports inferred causes from outcomes

`exit_audit.audited_exit_reason` called every profitable legacy close a
take-profit and every losing close a stop-loss, regardless of recorded trigger.
Its substring matching also classified `HOLD_MODEL_VETO` as a stop-loss, even
though that action says a stop was suppressed. This biases the reports used to
judge whether exit rules improve returns.

The classifier now requires a terminal close and an explicit recognized exit
action. Missing or conflicting causes are `unclassified`; P&L sign does not
invent an explanation. Settlement and unfilled-expiry classifications remain.
Summary data publishes `closed_unclassified`, and research goal reports retain
unknown-cause lots and their P&L. A partially filled expiry retains its filled
open position instead of being labeled unfilled. Economic P&L and win/loss calculations are
unchanged. Regression tests cover profitable, losing, zero-P&L and missing-P&L
legacy closes, hold actions, conflicting evidence, open rows and explicit causes.

## Remaining findings, in priority order

1. **P1 — posterior sizing evidence is not independent or policy-scoped.**
   `posterior_kelly.load_posterior_kelly_model` loads all settled/closed order
   rows without account, profile or strategy-fingerprint filtering.
   `_accumulate` adds one observation per execution lot, so splitting one
   city-market outcome into many partial-close children increases `n` and can
   increase the sizing multiplier. `scan._build_sizing_model` passes no profile
   identity. Its temperature cohorts also do not distinguish side or probability
   band. This is a confirmed mechanism, not a fresh estimate of dollar impact.
   Before increasing stakes, evaluate a policy-scoped, independent-event model
   against the current model in shadow; aggregate lots before fitting, evaluate
   uncertainty by city/target day, and preserve the current risk caps. Serving a
   changed model is a behavioral change and requires new policy lineage.

2. **P1 — exit depth freshness evidence still measures close time.**
   `monitor.cmd_paper_monitor` fetches quotes before looping over positions but
   writes `observed_at=datetime.now(UTC)` when it closes each order.
   `PaperStore.close_paper_order` compares that timestamp with `closed_at` when
   declaring displayed depth `VERIFIED`. A slow fetch/loop can therefore certify
   an old quote. Capture the actual observation/request time per quote; retain it
   through execution and test delayed batches. This audit did not modify the
   monitor or weaken its displayed-depth requirement. Historical `VERIFIED`
   labels alone cannot establish true quote freshness.

3. **P2 — posterior closed-trade outcomes use selected settlement coverage.**
   `_date_settlement_highs` keys cities correctly, but reads highs only from
   orders with a recorded settlement temperature. A city/day where all positions
   closed early contributes no counterfactual outcome unless another order held
   through settlement. Use the existing authoritative CLI settlement adapter
   for complete city/day truth, with explicit missing/conflict diagnostics.
   Avoid treating held-to-settlement-only coverage as an unbiased sizing sample.

## Implication for higher volume and forecast accuracy

The next useful evaluation is complete, point-in-time scoring of every captured
market bucket, including rejected candidates, against authoritative settlement
truth. Existing snapshot replay, public-tape replay, logical-position projection
and settlement-key tooling can support it. Evaluate forecast calibration and
after-fee executable P&L separately, with independent-day uncertainty, policy
fingerprints and liquidity limits. Settlement-only rescoring does not validate
an exit-rule change. Higher win rate, higher volume and higher profit are
different objectives; no gate or bankroll change was made to force activity.

## Local validation

- New classifier regressions: 9 failures reproduced before implementation,
  plus a separately reproduced partial-expiry misclassification.
- Focused classifier, summary, side-performance, research-goal and Strategy Lab
  report suite: 127 passed in both UTC and America/Los_Angeles.
- Deployment and public website validation belong to the parent operational
  audit; this document makes no deployment claim.
