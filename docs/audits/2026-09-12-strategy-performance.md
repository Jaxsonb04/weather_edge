# Strategy Lab performance investigation — September 12, 2026

## Evidence and current performance

Read-only public snapshot: Strategy generated September 12 at 22:20:34 UTC;
publication manifest generated at 22:22:30 UTC. The freshly downloaded Strategy
and trading-signal files both match the manifest's SHA-256 hashes. Backend
provenance is clean revision `2a6432e3bdb29fa1798a4b07e5f5396685b5245b`.
Raw artifacts and access diagnostics remain in ignored local operator state.

These are **economically separate paper accounts**, each started with $1,000:

| Account | Realized equity | Marked equity | Realized return on initial capital | Open cost |
| --- | ---: | ---: | ---: | ---: |
| Live Stability | $1,053.75 | $1,053.65 | +5.37% | $12.28 |
| Research ROI | $1,029.87 | $1,022.67 | +2.99% | $130.41 |

Both public ledger reconciliations report zero difference. Live execution is
disabled. Public evidence cannot independently establish AWS service health.
SSH timed out; the local AWS CLI could not inspect EC2 or Systems Manager because
credentials were unavailable. No production source, order, timer, account,
credential, firewall, or environment setting was changed by this investigation.

Research ROI peaked at $1,120.82 in the reported account window, then realized
-$40.944 on September 11 and -$50.002 on September 12. The approximately $90.95
decline is **8.11% of that actual account peak**. The daily-goal diagnostic's
8.23% maximum drawdown uses its separate window seeded at the fixed reference;
it must not be substituted for the actual account-equity calculation.
September 12 is incomplete at the snapshot time.

The trailing 30-day Research ROI mean is $0.47/day, with a day-bootstrap interval
of approximately [-$6.21, +$7.20]. Its $50 daily objective was achieved once in
30 observed days. A 70.88% overall winning-position rate coexists with only
1.05% return on resolved capital; win rate alone hides asymmetric loss size.

## What explains the decline

Three NO positions lost $97.94 before offsets from other trades:

| Market / target | Entry cost at risk | Maximum settlement profit | Realized loss | Loss / entry cost | Exit fills |
| --- | ---: | ---: | ---: | ---: | ---: |
| Atlanta, September 11, above 88°F | $74.15 | $34.90 | -$45.27 | 61.05% | 6 |
| Atlanta, September 12, above 81°F | $89.30 | $5.70 | -$36.40 | 40.76% | 3 |
| Miami, September 12, outside 92–93°F | $30.40 | $7.60 | -$16.27 | 53.51% | 2 |

The active research policy allows $90 per position, $180 per city/target,
$360 per region/day, $750 aggregate risk and a $150 daily realized-loss pause.
The allocator expands structural research candidates to the position cap rather
than applying their conservative edge to the quantity. For Atlanta September
12, the entry cost was $0.94 and lower-bound win probability was 0.9435:
only $0.0035 lower-bound edge per contract, yet $89.30 was at risk for a maximum
$5.70 settlement profit. The same market's Live Stability position cost $4.68
and lost $1.97. Size materially amplified the research loss.

The 35% stop is a trigger, not a guaranteed fill price or maximum loss. These
positions exited through multiple fills at worse prices. Exact quote ages,
available depth, model-veto decisions and forecast revisions at each exit are
not exposed in the public artifact; AWS journal access is required to attribute
the overshoot among those mechanisms. These three loss positions have no
settlement truth/forecast-error values, so the specific weather-model failure
is **not established**. Disabling stops or calling every stopped trade a
forecast failure would be unsupported.

## Implemented safeguards and evidence corrections

The performance safeguards and execution-scaling revision are implemented locally.
Deployment and release verification are recorded below.

1. **Research entry sizing:** conservative quarter-Kelly using the lower-bound
   probability and canonical fee-inclusive cost, capped at the existing $90
   production position limit.
   The allocator and authoritative admission path enforce the limit. Invalid or
   non-positive conservative edge cannot fund an entry. This lets stronger opportunities scale while reducing thin-edge exposure;
   the fraction is a risk-design choice, not a profit-optimized parameter claim. Incident inputs yield
   whole-contract entry ceilings of $65.28, $14.10 and $25.60 respectively; these are quantity
   ceilings, not simulated realized P&L or guaranteed future fills.
2. **Projected daily loss:** current open/pending cost is charged against the
   remaining daily loss budget across all target dates. Realized profits do not
   expand this budget. The transaction checks capacity again before admission.
   Existing exposure can still lose; tightening entries cannot undo fills.
3. **Truthful status:** historical analysis older than its existing 36-hour
   bound, invalid analysis timestamps, and failed account validation cannot
   produce the top-level healthy headline. An account-specific warning appears
   at 5% realized drawdown in the reported account window. This alert threshold
   is informational, not an execution gate. Research losses are never assigned
   to Live Stability or measured against a combined bankroll.
4. **Dependent evidence:** bootstrap draws retain all cities sharing a calendar
   target date. Confidence intervals, current promotion significance and
   persisted prior-family significance use the same grouping. Metric coverage
   still counts station-day folds; independent resampling clusters count dates.
   Existing multiple-comparison and promotion thresholds remain binding.
5. **Partial-exit loss memory:** model-veto dollar-loss checks now add persisted
   realized child-lot P&L to the remaining position's unrealized P&L. A fresh
   monitor process cannot forget the earlier loss merely because a position was
   sliced. Four pre-fix regressions reproduced a stop changing back to
   `HOLD_MODEL_VETO` or `HOLD_NO_MODEL_READ`; the fixed path continues to respect
   displayed depth and missing quotes. A genuine price recovery can still
   change the decision. This source-level defect is reproduced, but its exact
   contribution to the observed Atlanta/Miami losses needs AWS monitor evidence.

The statistical defect is reproducible: ten dates with seven +1 and three -1
deltas gave an unclustered p-value near 0.154; duplicating each date over three
perfectly correlated cities reduced it to about 0.019. Copying a weather outcome
must not manufacture evidence. Calendar-date grouping prevents this inflation.
It does not address serial dependence between successive dates or make repeated
adaptive hypothesis testing valid indefinitely.

The existing economic account and frozen daily-goal history are retained.
Execution behavior must carry a new fingerprint so old/new policy outcomes
remain distinguishable. No challenger is promoted by this work.

The entry limit bounds **initial entry cost including entry fees**, not lifetime
loss after every possible exit fee. Extremely small fractional fills can incur
a rounded exit fee above their gross proceeds. Existing pending orders keep
their original admitted quantities; a deployment must reconcile recorded tape
and drain or expire these orders before claiming the new ceiling covers all
subsequent fills. The inspected public snapshot had no pending target orders,
but this must be rechecked at cutover. The daily-risk guard uses net daily P&L:
recovering losses can restore room, while profits cannot expand the original
daily budget.

## Online research and next improvements

- **Shrink uncertain bets and evaluate drawdown explicitly.** Baker and McHale,
  [Optimal Betting Under Parameter Uncertainty](https://pubsonline.informs.org/doi/abs/10.1287/deca.2013.0271),
  supports accounting for probability-estimation uncertainty when sizing.
  Busseti, Ryu and Boyd's
  [Risk-Constrained Kelly Gambling](https://www.web.stanford.edu/~boyd/papers/kelly.html)
  explicitly trades growth against drawdown. Neither establishes that this
  project's chosen quarter-Kelly fraction or $90 ceiling is optimal. The present
  patch is not an implementation of the paper's drawdown-probability guarantee.
- **Preserve dependence.** Politis and Romano's
  [The Stationary Bootstrap](https://users.ssc.wisc.edu/~behansen/718/Politis%20Romano.pdf)
  motivates dependence-preserving resampling. The implemented date grouping
  addresses same-day city dependence; preregister consecutive-day block lengths
  and sensitivity checks before making claims with serially correlated weather.
- **Score probabilities, not only point forecasts.**
  [Gneiting and Raftery](https://sites.stat.washington.edu/raftery/Research/PDF/Gneiting2007jasa.pdf)
  motivates proper scoring rules. Retain paired CRPS, bracket Brier/log loss,
  coverage and market-prior baselines by city, lead, side and probability band.
  The prior ML challenger improved MAE but worsened CRPS; it remains unpromoted.
- **Replay the actual strategy.** The offline adapter is currently YES-only and
  has no subsequent maker tape, while the observed losses are NO/maker-heavy.
  Preserve named forecast initialization/availability times, both sides of
  executable books, later trades, queue state, partial fills, cancellations and
  exit evidence. Complete off-host archive coverage for maker tables before
  claiming replay parity. Open-Meteo distinguishes
  [fixed-lead Previous Runs](https://open-meteo.com/en/docs/previous-runs-api)
  from [initialization-specific Single Runs](https://open-meteo.com/en/docs/single-runs-api).
  Reconstructed weather is not evidence of what was available at entry.
- **Prevent selection bias.** Keep a fixed declared family and confirmation
  window. Report every tried variant. The
  [Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)
  describes selection and non-normality effects; it is not a reason to replace
  the existing economic, calibration and replay gates with one score.
- **Repair posterior sizing evidence before relying on it.** The separate
  posterior loader still pools accounts/policy eras, counts execution rows, and
  trains temperature cohorts using settled rather than forecast highs. These
  are established source-level gaps, not proven causes of this research loss:
  the target allocator bypassed this sizing path. Account/policy isolation,
  root/event grouping, decision-time cohort labels and complete authoritative
  truth should precede a future promotion of that sizing estimator.
- **Refresh historical analysis deliberately.** Its production timestamp was
  185.7 hours old. The recurring publisher intentionally avoids full-journal
  analysis. Restore AWS access and run the verified-snapshot analysis workflow;
  design a measured off-host cadence instead of adding heavy journal rescoring
  to the five-minute publisher.

## Execution scaling follow-up — September 12, 17:50 PDT snapshot

A second fresh fetch matched the 17:52 PDT manifest for both Strategy and signal.
Research realized equity remained $1,029.87, marked equity was $1,024.06, and
open cost was $132.76. Live Stability remained a separate account with $1,053.75
realized equity, $1,053.71 marked equity and $14.63 open cost. Both ledgers
reconciled; neither account had pending orders in this snapshot. Full historical
analysis still dated September 4 at 21:36 PDT, more than seven days old.

The 30-day execution summary requested 47,717 contracts and filled 2,787.14
(**5.84%**). The report counts 437 limit-mode orders, including orders that
filled immediately by crossing the visible quote; 317 orders expired and 64
partially filled. Reported fees on resolved lots totaled $18.23. These counts do not establish fill quality or a causal
weather-model diagnosis. A larger requested quantity alone does not prove more
trading. The scanner is configured to run every five minutes and the monitor every two.
The target uses active-duplicate, scan-fingerprint and exposure checks rather
than the generic strategy's three-entry setting.

The initial local containment design used a uniform $30 cap. The user's scaling
request supersedes that undeployed choice: the final design retains the current
production $90 ceiling and uses conservative quarter-Kelly to select actual
entry risk. The three incident price/probability examples remain limited to
$65.28, $14.10 and $25.60. A high-confidence opportunity can use
more than $30, but current production's $90 maximum is not being increased.

Two execution bottlenecks are addressed:

- Structural Research ROI taker candidates can be sized beyond the generic
  25-contract scanner placeholder, bounded by fresh displayed ask depth, the
  canonical fee schedule, conservative sizing and portfolio capacity. Expansion
  happens before initial execution quoting; later allocator reductions are
  retained. A controlled regression with a $0.76 ask, 100 displayed contracts
  and 0.90 lower-bound probability increases the quote from 25 contracts
  ($19.32 including fees) to 100 ($77.28 including fees). This is a code-path
  test, not a measured production opportunity or fill. Atomic admission independently checks the same limits.
- If the usual best-bid-plus-one-tick quote fails its edge floor, the target may
  quote at a valid reservation price no more than one tick behind the best bid.
  Both existing point and lower-bound probability floors remain binding, as do
  spread, depth, duplicate, expiry and finite-tape queue checks. More resting
  orders are not a claim of more filled trades.

The planner also receives daily spending capacity derived from current account
exposure and recomputes rounded fees after quantity reductions, so a capacity-clipped order remains valid at
final admission instead of being rejected for stale size/cost assumptions.

The latest public candidates do not establish an immediate lift: among 24 rows,
12 pass structural checks, two already have active duplicates and ten fail the
lower-bound quote floor outside the bounded fallback. The earlier Denver
reservation-price example also has an active duplicate. Furthermore, about $50
in current-day realized loss plus $132.76 open cost exhausts the new projected
$150 daily-risk allowance. New risk correctly waits for capacity to reopen.

Research supports the execution design, with specific limits:

- [Official order amendment documentation](https://docs.kalshi.com/api-reference/orders/amend-order-v2)
  states that increasing quantity or repricing loses queue priority. Retain valid
  resting orders rather than blindly replacing them at every scan. The existing
  15-minute expiry remains; this release does not stretch forecast staleness to
  manufacture fills.
- [Avellaneda and Stoikov](https://math.nyu.edu/inmemoriam/avellaneda/HighFrequencyTrading.pdf)
  models inventory and execution-arrival tradeoffs. Weather contracts additionally
  share city/day settlement outcomes; existing concentration limits stay binding.
- [Distributional Robust Kelly Gambling](https://web.stanford.edu/~boyd/papers/robust_kelly.html)
  studies growth with uncertain probabilities. Using a lower-bound probability
  here is a conservative design choice, not a complete robust optimization model.
- [Official order-book documentation](https://docs.kalshi.com/getting_started/orderbook_responses)
  describes complementary YES/NO bids. The current three-level snapshots are
  captured after placement, so they cannot justify pre-entry multi-level fills.
  A future depth-walking implementation needs fresh pre-entry ladders, level-wise
  prices/fees, and replay/accounting parity before deployment.

Evaluate the frozen execution version using independent opportunities, placed
orders, filled contracts, filled dollars, after-fee P&L, inventory duration and
post-fill markouts. Date-clustered uncertainty and unchanged promotion gates
remain necessary. Neither additional scan approvals nor requested notional is a
profit metric, and no release can guarantee that market losses never recur.

## Validation and deployment boundary

The release was isolated from current main, excluding the unrelated
Apple/ML draft and retaining newer frontend/dependency fixes. At initial revision `28c9c311c`, the full Python
3.13 suite passed **2,928 tests with eight skips in 130 seconds**, including the
network-enabled isolated installer regression. Compilation and diff checks
passed. The project health check passed source, configuration and secret checks;
its only warning concerned disposable test-generated local runtime state, which
was not used to diagnose production. The final execution-focused set passed
415 tests with eight skips after a shallow-book follow-up; the independent statistics/status review passed
236 focused tests. The initial GitHub run passed both Python versions and Semgrep, but the web
job exposed licensed-installer drift: hpsetup 4.7 upgraded the pinned UI package
and rewrote dependencies before the frozen install, producing an incompatible
import. The release pins the version-preserving 4.5 installer, installs locked
stubs first and rejects manifest changes afterward. Dependency versions remain
unchanged. Local validation passed 85 deployment tests, 178 frontend tests,
build/lint/icons and bundle limits. A fresh GitHub run must validate the actual
licensed bootstrap and all required checks before merge.

Direct reproduction against the original allocator and the patched allocator
changed requested quantities from 132/95/112 to 96/15/32 contracts for the three
incident price/LCB pairs. Actual historical fills were partial in two cases;
these calculations establish the entry-sizing change, not a P&L backtest.
Restart tests reproduced and corrected loss-memory reset after partial exits,
including zero-depth and dry-run cases. Statistical regressions cover current
promotion and persisted family history, unequal date coverage and determinism.
Independent code review confirmed atomic admission checks and identified the
grandfathered-order and fractional-exit-fee limitations documented above.

Additional regressions verify that a 112-contract stale-fee plan exceeding
remaining capacity by approximately $0.0014 becomes an exact 111-contract order
at $85.78 that passes persisted admission. Allocation cannot silently change
execution mode. Shallow structural taker orders receive the same exact
repricing: a $14.687 budget admits 18 contracts for $13.91, avoiding a
19-contract order whose exact $14.69 cost would fail. Scheduled planning counts filled cost and unfilled reservations
once, including partial fills, and shrinks a $90 desired entry to fit $50
remaining daily capacity instead of losing the whole order at admission.

Fresh public input replay changes the prior healthy status to separate
stale-analysis and Research ROI drawdown warnings. Deployment requires restored
operator access, exact reviewed source, the canonical backup/install gates, and
post-install paper-account, publication and timer verification. No production
fix is claimed until those steps complete.
