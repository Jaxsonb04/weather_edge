# WeatherEdge Production Trading Incident and Codebase Audit — 2026-07-24

> **Status:** local remediation validated; production deployment not performed
>
> **Audit generated:** 2026-07-24T18:03:11Z
>
> **Incident window:** 2026-07-22T17:08:50Z through
> 2026-07-24T17:08:50Z (2026-07-22 10:08:50 PDT through
> 2026-07-24 10:08:50 PDT)
>
> **Production source revision:** `ac21ec486012a92ef351f0f4742bc4b9a4dc42a2`
>
> **Safety:** no real-money order, credential change, infrastructure mutation,
> database mutation, or AWS deployment was performed.

This report is redacted. It does not contain host addresses, instance
identifiers, credentials, account secrets, private database rows, or raw
production logs. Throughout the report:

- **Observed** means directly present in an authoritative AWS-generated public
  artifact, Git/GitHub state, production-provenance manifest, or inspected
  source.
- **Calculated** means derived from the cited observations.
- **Inferred** means the most likely explanation, with the limitation stated.

## 1. Executive conclusion

The incident premise was false: the active `live` paper profile did not go
trade-free during the preceding 48 hours.

**Observed:** one new `live` paper order was created, filled, and profitably
closed inside the exact incident window. A second order that had been opened
before the window also closed profitably inside it. The new order was a
Chicago-station 82–83°F `BUY_NO` position:

| Event | UTC timestamp |
|---|---|
| Created | 2026-07-23T00:15:35.591528Z |
| Filled | 2026-07-23T00:21:17.505807Z |
| Closed | 2026-07-23T12:47:09.464771Z |

It entered as a maker limit at 0.94 under
`maker_trade_through_required`, filled 29.65 contracts with $27.87 at risk,
and exited across six fills at 0.97. Its model posterior was 0.9812, its
conservative lower confidence bound was 0.9600, point edge was 0.0412, and
lower-bound edge was 0.0200. Realized P&L was **+$0.83**, or **+2.97%** on
capital at risk.

The pre-window order closed at 2026-07-22T17:57:20Z for **+$2.95**.
Accordingly, the `live` profile's boundary aggregates changed from 77 to 79
terminal outcomes, 59 to 61 wins, $41.71 to $45.49 realized outcome P&L,
$699.58 to $735.35 resolved capital, and one to zero open orders.

**Observed:** real-money orders were zero by design. Production reported
`mode=paper_research_only` and `live_orders_enabled=false`; the real execution
path is intentionally fail-closed and has no authenticated order client.

**Conclusion:** this was not a scheduler-wide, ingestion-wide, or execution
outage. It was primarily **expected selective strategy behavior combined with
misleading profile observability**:

1. The `live` profile admitted a small number of high-confidence paper trades.
2. Most repeated evaluations failed conservative edge, uncertainty, timing,
   spread, favorite-price, model/market-gap, or liquidity gates.
3. A report query capped global monitor rows before filtering by profile. The
   high-volume motion sleeve could therefore evict live/target monitor evidence
   and make a healthy profile appear as `monitor-not-recording`.

The audit also confirmed three high-confidence model/data-integrity defects:
an EMOS point forecast could be calibrated with a different residual law,
collect-only dataset candidates could be consumed as if promoted, and
post-processing backtests could train on truth unavailable at the forecast
horizon. All three are fixed and tested locally. They are not a sufficient
explanation for a zero-trade incident, because zero trades did not occur.
The dataset-promotion path was dormant in the production snapshot, and the
successful Chicago order did not depend on the SFO EMOS path.

These model-integrity fixes can change paper signal selection. They have **not**
been deployed and require explicit confirmation before production deployment.

## 2. Incident window and production evidence

### 2.1 Exact window

| Zone | Start | End | Duration |
|---|---|---|---:|
| UTC | 2026-07-22T17:08:50Z | 2026-07-24T17:08:50Z | 48 hours |
| America/Los_Angeles | 2026-07-22 10:08:50 PDT | 2026-07-24 10:08:50 PDT | 48 hours |

### 2.2 Authority and evidence hierarchy

The ignored local runtime database and JSON artifacts were not used as
production truth. Production conclusions came from:

1. AWS-generated dashboard artifacts on the `gh-pages` publication branch.
2. The embedded publication manifest, source revision, sync time, generated
   times, execution-model version, and accounting-model version.
3. Boundary snapshots immediately before/after the incident window.
4. Timestamped public order, fill, close, settlement, candidate, rejection,
   health, forecast, scorecard, and research rows.
5. GitHub branch, protection, Actions, Pages, pull-request, collaborator,
   deploy-key, and alert metadata.
6. Local source inspection, replay-oriented fixtures, regression tests,
   dependency audits, Semgrep, lint, build, and the repository verification
   suite.

Boundary artifacts:

| Role | Publication commit | Artifact generated at | Relation to window |
|---|---|---|---|
| Baseline | `e6540dfc802a0b1df8e31e4c9ffc47032d447736` | 2026-07-22T16:56:25.777769Z | 12m24s before start |
| Endpoint | `64d0585854c75a08f3682231728b959f519582bc` | 2026-07-24T17:07:15.109492Z | 1m35s before end |

Both manifests identified deployed source
`ac21ec486012a92ef351f0f4742bc4b9a4dc42a2`, synced at
2026-07-22T09:45:16Z with `source_dirty=false`. Both identified execution
model `exec-v4-2026-07-17` and accounting model
`acct-v4-account-scoped-2026-07-14`. Current manifest hashes matched the
downloaded artifact bytes.

**Observed:** 469 publication commits occurred within the exact 48-hour
window. Publication intervals had a 329-second median, 897-second p95, and a
1,135-second maximum; none exceeded 20 minutes. At the endpoint, Strategy Lab
was generated at 17:07:15Z, trading data at 17:07:43Z, city data at 17:07:47Z,
the publication commit landed at 17:08:08Z, and the CDN artifact was observed
at 17:08:31Z.

**Inference:** these observations strongly reject a broad scheduler or
publication outage. They prove publication continuity, not every individual
timer activation.

### 2.3 Access limitation

Private AWS runtime inspection could not be completed. The configured AWS CLI
session had expired, and a direct read-only SSH connection timed out. No
credential refresh was attempted because credentials and account settings were
outside the authorized mutation scope.

Consequently, this audit could not directly read private SQLite rows, systemd
timer invocation history, service journals, environment values, alarm state,
restart counters, or private per-cycle provider errors. The exact
timer-by-timer, city-by-city census requested for all nominal scan and monitor
cycles therefore remains **blocked by production access**, not silently
assumed. Public artifacts provide exact lifecycle timestamps for retained
orders and aggregate/repeated evaluation rows, but retention means they cannot
reconstruct every gross cycle.

Nominal cadence implies 576 five-minute scan ticks and 1,440 two-minute monitor
ticks over 48 hours. Those are schedule expectations, not claimed observed
completion counts.

## 3. Root-cause analysis and rejection funnel

### 3.1 End-to-end path

The audited production path was:

```text
weather providers and observations
  -> normalized/cache snapshots
  -> city and horizon features
  -> forecast point/distribution
  -> calibration and contract buckets
  -> prediction-market discovery/mapping
  -> profile filters and after-cost edge
  -> risk/capital/liquidity/timing checks
  -> paper maker order
  -> acknowledgement/fill monitoring
  -> close or settlement
  -> account-scoped reporting/publication
```

The retained counters between boundary artifacts increased by:

| Retained counter | Net increase |
|---|---:|
| Raw/pre-resolution signals | 121,574 |
| Deduplicated signals | 389 |
| Decision snapshots | 120,070 |
| Monitor snapshots | 98,509 |
| Paper orders | 162 |

Forecast rows decreased by 109, market rows by 109, and probability rows by
654 because retention pruning occurred during the same period. These are net
retained-counter deltas, not gross cycle volumes.

### 3.2 Exact live order outcomes in the window

| Lifecycle state | Count |
|---|---:|
| Created in-window | 1 |
| Filled in-window | 1 |
| Closed in-window | 2 |
| Settled in-window | 0 |
| Real-money submissions | 0 |

One close belonged to an order created before the boundary. No live paper
order was silently lost in the retained lifecycle evidence.

### 3.3 Published seven-day live funnel

The only consistent public gate funnel spans the trailing seven days and
contains repeated scan evaluations rather than independent opportunities:

| Stage | Count | Share of evaluations |
|---|---:|---:|
| Evaluation rows | 108,111 | 100.000% |
| Signal-approved rows | 1,106 | 1.023% |
| Rejected rows | 107,005 | 98.977% |
| Rejected: edge family | 90,012 | 83.259% of all rows; 84.120% of rejects |
| Rejected: no data | 16,992 | 15.717% of all rows; 15.879% of rejects |
| Rejected: other | 1 | <0.001% |
| Positions opened | 21 | not directly comparable to repeated rows |

The edge family is multi-gate and overlapping. Its leading reasons were:

- conservative lower-confidence-bound edge;
- raw point edge;
- same-day horizon blocked by `min_lead_days=1`;
- excessive source spread;
- posterior floor;
- market spread;
- insufficient bid size.

Because one candidate can fail multiple gates, reason counts must not be added
as if mutually exclusive.

### 3.4 Representative reconstructed non-trades

**Denver day-ahead — expected uncertainty rejection**

| Field | Value |
|---|---:|
| Posterior | 0.8670 |
| Lower confidence bound | 0.7340 |
| Ask / bid | 0.84 / 0.83 |
| Point edge | +0.0175 |
| Lower-bound edge | -0.1155 |

The point estimate was mildly favorable, but the conservative after-uncertainty
edge was negative. Rejecting it was consistent with the configured risk model.

**Same-day candidate — multiple intentional gates**

Point edge was +0.1269, but lower-bound edge was -0.1694, model/market gap was
0.315 versus a 0.200 maximum, price was outside the configured favorite band,
and same-day entries were disabled. The apparently attractive point edge did
not survive uncertainty or eligibility checks.

**SFO candidate — no safe entry**

Point edge was +0.0925, but forecast-source spread was 12.5°F versus a 10°F
maximum, price 0.42 was outside the 0.70–0.97 favorite band, model/market gap
was 0.295 versus a 0.200 maximum, and lower-bound edge was -0.2323.

At the endpoint all 24 displayed live candidates were no-trades. Twelve were
same-day horizon blocks; day-ahead rows were predominantly blocked by the
favorite-price band, conservative edge, and liquidity.

### 3.5 Root-cause classification

| Candidate cause | Classification | Evidence |
|---|---|---|
| No real-money orders | Expected | Paper-only mode; real path fail-closed |
| No `live` paper trades | Disproved | One create/fill/close plus one additional close |
| Scheduler-wide failure | Not supported | 469 publications; no >20m gap; large counter growth |
| Forecast ingestion outage | Not supported | 15/15 cities; fresh NWP and truth |
| Strategy selectivity | Confirmed | 98.98% repeated-row reject rate; reconstructed gates |
| Risk-control lockout | Not supported | A live order passed and filled |
| Silent order loss | Not supported in retained rows | Complete lifecycle for in-window order |
| Misleading monitor status | Confirmed defect | Global truncation before profile filtering |
| EMOS distribution mismatch | Confirmed defect, incident-adjacent | SFO path; not the successful Chicago order |
| Dataset candidate promotion | Confirmed dormant defect | Production candidate count was zero |
| Post-processing truth leakage | Confirmed research-validity defect | Horizon cutoff omitted from challengers |

An independent-frequency rescore found 251 candidate approvals among 986
considered rows over 22 independent days, about 11.4 approvals per day. The
production diagnostic classified frequency as `ABOVE_TARGET_REVIEW_RISK`.
Weakening gates merely to increase trade count is therefore not justified.

## 4. Three-profile scorecard

### 4.1 Profile identity and isolation

The two selectable risk configurations are `live` and `research`. The three
active published profiles are:

| Published profile | Risk configuration | Isolated account/policy |
|---|---|---|
| `live` | `live` | live paper account |
| `research-target` | `research` | `paper-research-target-v1` |
| `research-motion` | `research` | `paper-research-motion-v1` |

The research sleeves share a configuration family but use separate canonical
account/sleeve/version/fingerprint identities. The legacy profile named
`research` is pre-migration history and is excluded from the active
three-profile comparison.

### 4.2 Comparable trailing-seven-day outcomes

| Metric | live | research-target | research-motion |
|---|---:|---:|---:|
| Resolved | 22 | 57 | 373 |
| Wins / losses | 21 / 1 | 49 / 8 | 177 / 196 |
| Hit rate | 95.45% | 85.96% | 47.45% |
| Realized P&L | +$31.40 | +$42.12 | +$1.15 |
| Resolved capital | $239.13 | $331.59 | approximately $274 |
| ROI | 13.13% | 12.70% | 0.42% |
| Open positions | 0 | 10 | 66 |
| Open risk | $0.00 | $61.21 | $58.08 |

All live-profile outcomes were on the `NO` side. Motion's side split is the
most actionable performance finding:

| Motion side | Wins / losses | P&L | ROI |
|---|---:|---:|---:|
| NO | 171 / 138 | +$4.49 | +1.72% |
| YES | 6 / 58 | -$3.33 | -22.51% |

The common underlying forecast archive had a seven-day mean absolute error of
4.21°F. That is not profile-specific skill and must not be attributed to one
sleeve.

Account scopes are intentionally different. Live account equity was $958.18
with all-account P&L of -$41.82, while the separately published
legacy-inclusive outcome cohort showed 79 resolved, 61 wins, 18 losses,
+$45.49, and 6.19% ROI. These figures must not be mixed. Target realized equity
was $1,042.12 and marked equity $1,046.60. Motion realized equity was $1,001.15
and marked equity $1,001.18.

### 4.3 Boundary changes over the incident span

The boundary artifacts span 48h10m49s rather than exactly 48h; use these as
near-window aggregate deltas:

| Profile | Resolved delta | W/L delta | Realized P&L delta | Other |
|---|---:|---:|---:|---|
| live | +2 | +2 / 0 | +$3.78 | open 1 -> 0; capital +$35.77 |
| research-target | +26 | +23 / +3 | +$21.01 | open 21 -> 10; capital +$156.26 |
| research-motion | +135 | +70 / +65 | +$0.94 | open 56 -> 66 |

Exact retained lifecycle events inside the 48-hour timestamps:

| Profile | Created | Filled | Resting | Closed | Settled |
|---|---:|---:|---:|---:|---:|
| live | 1 | 1 | 0 | 2 | 0 |
| research-target | 9 | 8 | 1 | 5 | 21 |
| research-motion | 106 | 106 | 0 | 76 | 58 |

Target's boundary execution counters increased by 86 maker quotes, 2,013
requested contracts, 112.999 filled contracts, 60 expirations, 7 partial fills,
and $0.201946 in fees, with no taker executions. Equivalent complete
fee/spread/slippage attribution was not public for all three profiles, so no
false comparable estimate is supplied.

### 4.4 What worked

- **live:** conservative selection produced two profitable terminal outcomes in
  the window and no open exposure at the endpoint.
- **target:** maker-only execution filled eight of nine new orders while
  preserving one resting order and posting strong, though small-sample, return.
- **motion:** generated sufficient volume to expose a sharp and actionable
  YES/NO asymmetry; the NO sleeve remained positive.
- **all profiles:** account/sleeve identities were isolated; the paper path
  retained full order lifecycle evidence; real execution stayed disabled;
  forecast artifacts covered all 15 cities with fresh source data.

### 4.5 What should improve

1. **High impact, high confidence:** deploy the model/data-integrity guards only
   after explicit review and approval.
2. **High impact, moderate confidence:** investigate or suspend motion YES
   promotion using a time-ordered replay; the observed -22.51% ROI is material,
   but the sleeve has only a short history.
3. **Medium impact, high confidence:** publish exact profile-scoped monitor
   health so a high-volume sleeve cannot hide another sleeve's evidence.
4. **Medium impact, high confidence:** add persisted cycle IDs and mutually
   exclusive primary reason codes to make timer completion and rejection
   funnels reconstructable without private journal access.
5. **Medium impact, moderate confidence:** collect profile-comparable fees,
   spread-at-decision, realized slippage, latency, and counterfactual outcomes.
6. **Do not loosen safety gates:** current frequency is not evidence of an
   opportunity shortage, and representative point edges disappear under
   uncertainty.

Precision, recall, and opportunity-capture are not reported because the
public data lacks a complete, independent counterfactual opportunity set and
contains repeated scan rows. Fabricating those metrics would be misleading.

## 5. Codebase findings

No new critical security or financial-safety vulnerability was confirmed.
The following findings are ranked by severity and confidence.

### High severity, high confidence — fixed locally, not deployed

#### H1. EMOS point forecast could use a mismatched residual law

- **Components:** `trading/sfo_kalshi_quant/forecast.py`,
  `trading/sfo_kalshi_quant/_cli/scan.py`, and
  `trading/sfo_kalshi_quant/report.py`
- **Evidence:** SFO's legacy blend stopped refreshing and the adapter could
  return a fresh EMOS point fallback, while the live profile disabled the EMOS
  distribution and configured LSTM residual calibration.
- **Impact:** the point estimate and contract probabilities could describe
  different distributions, corrupting edge and uncertainty.
- **Root cause:** forecast-source identity was not coupled to the calibration
  distribution consumed by the scan.
- **Fix:** scan and daily-report paths share one guard. An EMOS point now
  requires a finite, positive, same-row `(mu, sigma)` distribution whose mean
  matches the point. Missing, disabled, invalid, or mismatched data fails
  closed.
- **Tests:** focused fail-closed and exact-row propagation tests in
  `trading/tests/test_portfolio_cli.py` and
  `trading/tests/test_daily_report.py`.
- **Incident relevance:** not the successful Chicago order; potentially
  relevant to rejected SFO candidates.

#### H2. Collect-only dataset candidates could receive live blend weight

- **Components:** `forecaster/blend_sources.py`,
  `trading/sfo_kalshi_quant/dataset_research.py`
- **Evidence:** the producer labeled candidates `collect_only`, but the
  consumer treated `accuracy_candidate` as equivalent to promoted and could
  apply a 12% blend weight. Rows with absent/zero lead could be accepted, and
  reanalysis issue times could be synthesized.
- **Impact:** research-only data could influence a production forecast without
  an after-cost promotion decision; point-in-time validity was not guaranteed.
- **Root cause:** promotion state and data availability were implicit rather
  than an explicit consumer contract.
- **Fix:** require an explicit top-level `live_promotion.decision=approved`,
  `after_cost_approved=true`, and dataset allowlist; require a positive lead and
  issue time before the target; scope live guidance and historical corrections
  to KSFO; publish a fail-closed live-promotion record.
- **Tests:** producer/consumer promotion and point-in-time regressions in
  `trading/tests/test_dataset_research.py` and
  `trading/tests/test_clean_forecast_scoring.py`.
- **Incident relevance:** dormant; the production artifact reported zero
  accuracy candidates.

#### H3. Post-processing validation used truth unavailable at forecast time

- **Components:** `forecaster/forecast_postproc_backtest.py`,
  `forecaster/postproc_models.py`
- **Evidence:** `lead_days` was not applied to EMOS/weighted, analog, and
  recalibration training windows. Day D-1 truth could be used when simulating a
  day-ahead forecast before D-1 had settled.
- **Impact:** optimistic backtest scores, biased challenger selection, and
  potentially invalid promotion evidence.
- **Root cause:** training was sliced by target date, not truth-availability
  date.
- **Fix:** train only on dates through `target_date - lead_days - 1 day` and
  pass the horizon into every challenger.
- **Tests:** poison-row regressions in
  `forecaster/tests/test_postproc_backtest.py` and
  `forecaster/tests/test_postproc_models.py`.
- **Incident relevance:** model-governance defect, not evidence of scheduler or
  execution failure.

### Medium severity

#### M1. Future-dated forecast snapshots were accepted as fresh — fixed locally

- **Components:** scan and report freshness guards in
  `trading/sfo_kalshi_quant/_cli/scan.py`,
  `trading/sfo_kalshi_quant/cli.py`, and
  `trading/sfo_kalshi_quant/report.py`.
- **Impact:** clock skew or malformed provider timestamps could bypass stale
  data rejection.
- **Fix:** allow up to three minutes of expected skew and reject snapshots more
  than five minutes in the future.
- **Tests:** scan/adapter/report freshness regressions.

#### M2. Profile monitor health was computed after a global row cap — fixed locally

- **Components:** `strategy_lab/paper_card.py` and
  `strategy_lab/profiles.py`.
- **Evidence:** the report selected the latest monitor row per order, globally
  sliced to 12 rows, and only then filtered for a profile. Motion activity
  displaced live/target evidence. Baseline live had one open order but appeared
  `monitor-not-recording`; later target had ten open orders with the same
  misleading state.
- **Impact:** false operational alerts and a plausible source of the incident
  perception.
- **Fix:** query current open order IDs before global/display caps, compute
  exact per-profile monitor health, and consume that map in profile reporting.
- **Tests:** a target row remains healthy with 13 newer motion rows and 5,001
  unrelated newer snapshots in `trading/tests/test_strategy_research.py`.

#### M3. Live-paper capacity admission is not atomic — open

- **Components:** `trading/sfo_kalshi_quant/paper.py` and database admission
  helpers.
- **Evidence:** live paper capacity is read before an insert that occurs on a
  separate connection, without an in-transaction recheck.
- **Impact:** two overlapping writers could exceed a position/capital limit.
- **Mitigation:** the production wrapper uses `flock`, reducing ordinary timer
  overlap. Research admission already uses `BEGIN IMMEDIATE`.
- **Recommended action:** mirror the research path's atomic admission and add a
  concurrent-writer test. This was not changed because transaction surgery
  exceeds the smallest safe incident fix.

#### M4. Publication branch lacks protection — open

- **Component:** GitHub `gh-pages`.
- **Evidence:** the branch is unprotected and a write-capable deploy key exists;
  repository hygiene checks flag the arrangement.
- **Impact:** compromise of the publisher host/key could deface or replace
  public operational artifacts.
- **Recommended action:** introduce a restricted publisher identity and
  tested ruleset/branch protection without breaking the timer publisher. No
  rule was changed during the incident.

#### M5. Production dependency advisories — fixed locally

- **Components:** `package.json`, `bun.lock`.
- **Evidence:** `tar` 7.5.20 had a moderate advisory and DOMPurify 3.4.11 had a
  low advisory. No first-party reachable tar extraction hook, unsafe HTML path,
  or DOMPurify misuse was found.
- **Fix:** overrides to `tar` 7.5.21 and DOMPurify 3.4.12.
- **Validation:** `bun audit --production` reports no vulnerabilities; web
  tests, lint, and build pass.
- **Advisories:** [GHSA-r292-9mhp-454m](https://github.com/advisories/GHSA-r292-9mhp-454m),
  [GHSA-c2j3-45gr-mqc4](https://github.com/advisories/GHSA-c2j3-45gr-mqc4).

### Low severity, high confidence — fixed locally

#### L1. Legacy signed and hyphenated temperature labels were ambiguous

- **Component:** `trading/sfo_kalshi_quant/settlement_truth.py`.
- **Impact:** below-zero terminal buckets could lose their sign, while an
  over-broad signed-number regex could reinterpret the separator in `68-69` as
  a negative sign.
- **Fix:** parse range separators separately from numeric signs and preserve
  documented `66F to 67F`, degree-symbol, positive-hyphen, and negative-range
  formats. Typed modern rows were already safe.

### Validation-risk proposal — not labeled a confirmed defect

The backtest's fixed 70°F synthetic contract ladder may differ from
date-dependent production ladders. Before using that backtest as a readiness
gate, archive and replay the actual entry-time ladder. This remains a proposal
because no production mispricing was demonstrated.

### 5.1 Strong non-findings

- Profile state is isolated by canonical account, sleeve, version, and
  fingerprint. No shared mutable profile leak was found.
- Research admission uses `BEGIN IMMEDIATE`; SQLite enables WAL, foreign keys,
  busy timeout, and relevant indexes.
- Real execution is unimplemented/fail-closed.
- No production `shell=True`, `os.system`, dynamic `eval`, unsafe pickle/YAML
  deserialization, or untrusted command construction was found.
- Dynamic network calls were to fixed/trusted provider hosts; broad Semgrep URL
  candidates were not exploitable first-party SSRF.
- A 28,858-blob history scan found only intentionally invalid test fixtures;
  GitHub reported zero open secret-scanning alerts.
- GitHub Actions dependencies are SHA-pinned and workflows use read-only
  permissions; lockfiles are hashed.
- No critical/high dependency vulnerability remained after the lock update.

### 5.2 First-party subsystem inventory

| Area inspected | Main evidence | Result |
|---|---|---|
| Weather ingestion/cache | provider adapters, freshness/provenance, public forecast health | 15/15 cities healthy; future-time guard fixed |
| Forecast models | ensemble, EMOS, LSTM fallback, post-processing | EMOS coupling and horizon leakage fixed |
| Calibration/probability | residual law, bucket probabilities, uncertainty bounds | fail-closed source/distribution coupling added |
| Dataset research/promotion | producer, consumer, point-in-time fields | explicit live gate added |
| Market discovery/mapping | date/horizon, bucket labels, side normalization | no incident-stopping defect; legacy signed label fixed |
| Signal/profile filters | edge, LCB, spread, price band, lead, gap | conservative behavior confirmed |
| Risk/capital | bankroll, limits, exposure, duplicate checks | no lockout; live capacity race remains |
| Paper execution | quote, fill model, expiry, partial fill, close | lifecycle complete; no taker/live execution |
| Monitor/settlement | monitor rows, close, settlement truth | profile truncation fixed |
| Research/backtest | rescore, rolling history, promotion gates | leakage fixed; short history blocks promotion |
| Database/schema | WAL, locks, indexes, retention, transaction boundaries | generally sound; one admission TOCTOU open |
| Publication/frontend | artifact allowlist, manifests, Vite build | continuous; no UI source change |
| Deployment/operations | timer docs, wrapper lock, source provenance | source reconciled; private runtime access blocked |
| CI/security/dependencies | Actions, Semgrep, history, audits | green locally; hardening gaps recorded |
| Documentation/maintainability | architecture, runbook, prior audit, report paths | incident record added |

## 6. Forecasting and trading experiments

No treatment is promoted by this audit. The controlled evidence is too short
for a 30- or 90-day profile comparison, and every reported return confidence
interval crosses zero.

The available profile-specific snapshot rescore is useful for diagnosis but is
not a chronological account replay. It uses each profile's configuration on
the retained opportunity set:

| Profile | Independent days | Considered | Approved candidates | Settled | Candidate P&L | Risk | ROI | 95% CI | Max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---|---:|
| live | 22 | 986 | 251 | 242 | $248.386 | $18,531.614 | 1.3403% | [-3.1729%, 5.6086%] | 38.93% |
| research-motion | 5 | 465 | 103 | 89 | $39.452 | $864.548 | 4.5633% | [-8.1314%, 13.9656%] | 1.7215% |
| research-target | 4 | 756 | 20 | 11 | $17.3801 | $146.6199 | 11.8538% | [-7.7994%, 43.2654%] | 0.7092% |

Motion candidate-side split in this rescore:

- YES: -$25.7151, -22.8142% ROI.
- NO: +$65.1671, +8.6678% ROI.

Target's result is dominated by a single YES trade (+$19.1975); its NO subset
was -$1.8174. The recorded live book's 5.8535% ROI is a selected/account-scoped
cohort and is not comparable to the candidate replay.

Readiness was 46.1% (5 of 12 checks), with status `REPLAY REQUIRED`. The
readiness boundary was 2026-07-19T09:44:15.582161Z, after which only two
independent decisions/days existed; calibration gap was 0.210 versus a 0.100
maximum. No profile was promotion-eligible.

The target sleeve's nominal $50/day objective is unsupported: only six observed
calendar days existed, zero reached $50, and mean realized P&L was about
$7.019/day. There is no defensible 90-day dataset and not even a complete
independent 30-day cohort. The correct action is to collect more data, not tune
to the short sample.

The code fixes in this audit were validated with leakage/poisoning and
fail-closed tests. Those tests prove invariant enforcement; they do not prove a
forecast or P&L improvement. A proper treatment experiment should:

1. archive actual entry-time forecasts, distributions, market ladders, quotes,
   fees, and profile configuration;
2. use rolling-origin, city/horizon-specific splits;
3. train only on truth available before each simulated forecast;
4. use the same opportunity universe and maker fill semantics;
5. report city/horizon calibration, MAE/CRPS/Brier, after-cost P&L, drawdown,
   side splits, fill rate, and uncertainty;
6. require enough independent days for a predeclared confidence threshold.

## 7. Changes and validation

### 7.1 Local changes

| Change | Files |
|---|---|
| Couple EMOS point and distribution in scan and daily report; fail closed | `forecast.py`, `_cli/scan.py`, `report.py` |
| Reject materially future forecast timestamps | `_cli/scan.py`, `cli.py`, `report.py` |
| Require explicit after-cost dataset promotion, point-in-time rows, and KSFO scope | `forecaster/blend_sources.py`, `trading/sfo_kalshi_quant/dataset_research.py` |
| Enforce horizon-aware truth availability | `forecaster/forecast_postproc_backtest.py`, `forecaster/postproc_models.py` |
| Preserve private helper compatibility boundary | `forecaster/google_weather_cache.py` |
| Compute monitor health per profile before caps | `strategy_lab/paper_card.py`, `strategy_lab/profiles.py` |
| Parse signed legacy temperature labels | `settlement_truth.py` |
| Patch production dependencies | `package.json`, `bun.lock` |
| Add focused regression coverage | forecaster and trading test modules |

No frontend component or styling source changed, so browser visual verification
was not applicable. The production dependency update was validated by the
existing web test/lint/build pipeline.

### 7.2 Validation results

| Check | Result |
|---|---|
| Full Python suite before changes | 2,368 passed |
| Full Python suite after final fixes | 2,396 passed in 73.73s |
| Focused affected suite | 185 passed |
| Web unit tests | 36 files, 138 passed |
| Web lint | passed |
| Production web build | passed; 2,207 modules transformed |
| Python compileall | passed |
| Semgrep project rules | 3 rules, 512 files, 0 findings |
| `bun audit --production` | no vulnerabilities |
| Python locked-dependency audit | no known vulnerabilities |
| Diff whitespace validation | passed |

All behavioral tests were fixture/replay/offline tests. No command contacted a
broker order endpoint and no live or paper production order was attempted.

## 8. Local, GitHub, CI, AWS, database, scheduler, configuration, and deployment state

### 8.1 Reconciliation at audit start

| Surface | Revision/state |
|---|---|
| Local `main` | `ac21ec486012a92ef351f0f4742bc4b9a4dc42a2`, initially clean |
| `origin/main` | same revision |
| GitHub default branch | `main`, same revision |
| AWS-generated artifact source | same revision; `source_dirty=false` |
| Active execution/accounting models | `exec-v4-2026-07-17` / `acct-v4-account-scoped-2026-07-14` |
| Real execution | disabled; paper research only |

The production source was therefore synchronized across local Git, GitHub, and
the published AWS-generated artifact at incident start. The remediation
worktree intentionally diverges until its review branch is merged and an
authorized deployment occurs.

### 8.2 GitHub and CI

- `main` has strict required checks for Python 3.12, Python 3.13, and Web (bun);
  administrators are included.
- Baseline Verify run
  [29904535960](https://github.com/Jaxsonb04/weather_edge/actions/runs/29904535960)
  succeeded for `ac21ec486012a92ef351f0f4742bc4b9a4dc42a2`.
- The latest observed Pages run
  [30112787999](https://github.com/Jaxsonb04/weather_edge/actions/runs/30112787999)
  succeeded for publication revision
  `eec8d71b7bf0fc75567e98eddbe9b81ca822e371`; its embedded production source
  remained `ac21ec486012a92ef351f0f4742bc4b9a4dc42a2`.
- One pre-existing PR,
  [#29](https://github.com/Jaxsonb04/weather_edge/pull/29), was open, stale,
  conflicted, 226 commits behind, and 2 commits ahead. It was not modified.
- No tags or releases existed.
- `main` required no approving review and the repository had no CODEOWNERS or
  CodeQL configuration. These are governance hardening gaps, not incident
  causes.
- `gh-pages` was moving normally but unprotected, as recorded in M4.

### 8.3 AWS/runtime limits

Public forecast health was clean: 15 of 15 cities, no artifact warnings,
eight-model NWP age 0.41 hours, truth lag at most one day versus a two-day
maximum, and 11,656 matched scorecard cases across 15 cities.

Private service/timer status, schema version, live environment-variable values,
restart history, disk/storage state, alarms, and journal errors remain
unverified because the AWS session was expired and SSH timed out. No attempt
was made to infer those values from stale local runtime files.

No AWS runtime or published dashboard artifact was changed by this audit.

## 9. Remaining risks, blockers, approvals, and priorities

### 9.1 Approval required before production deployment

The EMOS coupling, dataset promotion contract, truth-availability cutoff, and
future-timestamp guard can change which paper signals are generated or
accepted. Before deploying them:

1. review the PR diff and passing CI;
2. replay current production artifacts in a non-ordering environment;
3. confirm the expected behavior:
   - SFO EMOS rows without their exact distribution stop instead of falling
     back to LSTM residuals;
   - research dataset rows remain excluded until explicitly after-cost approved;
   - post-processing challengers train on less but valid history;
   - materially future-dated snapshots stop instead of appearing fresh;
4. approve a controlled paper-only deployment;
5. monitor scan completion, reason codes, profile orders, forecast freshness,
   and publication for at least one full forecast cycle.

**Rollback:** redeploy production source
`ac21ec486012a92ef351f0f4742bc4b9a4dc42a2` or revert the remediation commit,
rebuild, and republish. Database rollback is not required because these changes
do not migrate or mutate production data.

**Risk assessment:** fail-closed guards can reduce or pause SFO paper
opportunities when upstream distribution metadata is absent. The horizon
cutoff can change research rankings by removing leaked rows. Those are intended
safety effects, but they are material signal behavior and therefore not
deployed autonomously.

### 9.2 Blockers

- Exact per-timer/per-city/per-contract 48-hour census: blocked by expired AWS
  session and SSH timeout.
- Direct database/schema/service/alarm reconciliation: same access blocker.
- Statistically defensible 30-/90-day profile ranking: blocked by insufficient
  independent history.
- Complete fee/spread/slippage/latency and counterfactual metrics: not present
  consistently in public artifacts.

### 9.3 Recommended priorities

1. Review and, after explicit confirmation, deploy the fail-closed
   model-integrity fixes to paper production.
2. Restore read-only AWS incident access and rerun service/timer/database
   checks; do not rotate credentials as part of this PR.
3. Add persisted cycle IDs, completion state, primary/secondary gate reasons,
   and profile/city/horizon dimensions.
4. Make live-paper admission atomic.
5. Run a predeclared, time-ordered motion YES-vs-NO replay before any promotion.
6. Protect the publication path with a tested restricted publisher/ruleset.
7. Add CODEOWNERS, required approving review, and CodeQL if compatible with the
   project's student/solo-maintainer workflow.

## 10. Reproduction and verification commands

These commands are local/read-only or produce only local build/test artifacts:

```bash
git status --short
git rev-parse HEAD
git remote get-url origin

bash scripts/run_tests.sh
bun run test
bun run lint
bun run build
python3 -m compileall forecaster trading/sfo_kalshi_quant trading/tests scripts
bash scripts/run_semgrep.sh
bun audit --production
pip-audit -r requirements/production.lock --disable-pip
git diff --check

gh repo view --json nameWithOwner,url,isPrivate,defaultBranchRef
gh run view 29904535960 --json url,headSha,conclusion,name,status
gh run view 30112787999 --json url,headSha,conclusion,name,status
gh pr view 29 --json url,title,state,isDraft,mergeable,headRefName
```

The production-artifact analysis used read-only downloads and Git history at
the two boundary revisions. Raw downloaded artifacts and private identifiers
are intentionally not committed.

## 11. Identifiers

| Identifier | Value |
|---|---|
| Production source during incident | `ac21ec486012a92ef351f0f4742bc4b9a4dc42a2` |
| Baseline publication | `e6540dfc802a0b1df8e31e4c9ffc47032d447736` |
| Endpoint publication | `64d0585854c75a08f3682231728b959f519582bc` |
| Latest observed Pages publication during audit | `eec8d71b7bf0fc75567e98eddbe9b81ca822e371` |
| Execution model | `exec-v4-2026-07-17` |
| Accounting model | `acct-v4-account-scoped-2026-07-14` |
| Remediation code commit | `08770865ef1a57e04898d9d332cdde1db8ac2064` |
| Performance follow-up code commit | `ac737d275792701b24c0a1072bf019ba4db32cd3` |
| Remediation branch | `codex/weatheredge-production-incident-audit` |
| Baseline Verify run | [29904535960](https://github.com/Jaxsonb04/weather_edge/actions/runs/29904535960) |
| Pages run observed during audit | [30112787999](https://github.com/Jaxsonb04/weather_edge/actions/runs/30112787999) |
| Performance follow-up Verify run | [30124648811](https://github.com/Jaxsonb04/weather_edge/actions/runs/30124648811) |
| Pre-existing unrelated PR | [#29](https://github.com/Jaxsonb04/weather_edge/pull/29) |
| Remediation PR | Draft [#52](https://github.com/Jaxsonb04/weather_edge/pull/52) |
| Deployment | None created |

## 12. Performance follow-up

This follow-up was observed at `2026-07-24T20:28:46Z`
(`2026-07-24T13:28:46-0700`, America/Los_Angeles). The public Strategy Lab
artifact used below was generated at `2026-07-24T20:13:00.094346+00:00`.
The requested local preview PIDs `82899` and `82900` had already exited when
the exact `kill` was attempted; a subsequent exact-PID check confirmed that
neither process remained.

### 12.1 Trading-performance conclusion

No production admission, exit, sizing, threshold, or risk behavior was changed.
The current motion evidence identifies a useful challenger but is not long
enough to justify a profit-seeking policy change:

| Evidence | YES | NO | Independence limitation |
|---|---:|---:|---|
| Current motion snapshot rescore | 19 trades, 4W/15L, -$25.7151, -22.8142% ROI | 70 trades, 43W/27L, +$65.1671, +8.6678% ROI | five target days |
| Current published motion book | 69 resolved, 6W/63L, -$3.80, -23.18% ROI | 318 resolved, 172W/146L, +$3.12, +1.17% ROI | six target days |

The visible motion-YES rows also showed substantial apparent overconfidence,
but repeated entries within the same city/target cluster are correlated.
Neither the raw trade count nor the in-sample avoided loss is an honest
promotion statistic. The minimum next decision remains a predeclared
chronological replay with at least 30 independent station-target folds, at
least 10 distinct target dates, after-fee confidence bounds, and calibration
gates.

To make that decision measurable without changing orders, the branch adds the
fixed report-only challenger `motion-yes-lcb0-v1`. It compares the current
motion snapshot rescore with a treatment that retains NO rows and admits a YES
row only when its persisted point-in-time `edge_lcb >= 0`. Missing YES values
fail closed. The output reports baseline/treatment P&L, capital, ROI,
target-day-clustered intervals, and station-target counts. It is emitted only
for `research-motion`, is always `promotion_eligible=false`, and does not alter
policy fingerprints or order selection.

### 12.2 Forecast-research correction

The previously published `matched_lead_emos` result must be regenerated. Its
reported CRPS changed from `1.313314` to `1.297243`, with paired delta
`-0.016071` and 95% interval `[-0.025515, -0.006692]`, but the evaluator:

1. made each prior target available immediately, so a lead-two forecast for
   target D could train on D-1 truth that was unavailable at the D-2 serve;
2. evaluated a 60-case bias-plus-dispersion arm while production serves a
   45-calendar-day, 1.5-standard-error-deadband, bias-only correction; and
3. could silently overweight parallel method/source rows for one
   station/lead/target.

The branch now enforces `history_target_date < forecast_serve_date`, matches
the production recalibration constants, deduplicates identical cases, and
fails closed on conflicting rows until one reference method/source is selected.
The challenger remains shadow-only and cannot activate a forecast or trading
change.

### 12.3 Report-runtime improvement

Strategy Lab previously ranked `SELECT *` over every append-only monitor row,
carrying large `diagnostics_json` payloads through a SQLite window sort every
refresh. The replacement resolves `MAX(created_at)` per order, then `MAX(id)`
at that timestamp using the existing `(order_id, created_at)` index, and fetches
only the winning rows. A regression fixture covers tied timestamps and
later-inserted older timestamps.

A local synthetic benchmark with 60,000 rows, 300 orders, and 2,048-byte
diagnostic payloads produced the same 300 row IDs:

| Query | Five-run median |
|---|---:|
| Wide window baseline | 169.08 ms |
| Indexed aggregate plus primary-key fetch | 6.22 ms |
| Improvement | 27.2x |

A separate 120,000-row/600-order benchmark measured 492.98 ms versus 11.75 ms
(about 42x). These are synthetic query benchmarks, not a claim about end-to-end
AWS refresh latency; production impact still requires timing on a redacted
runtime database copy.

### 12.4 Follow-up validation and deployment boundary

Focused validation passed:

- full Python suite: 2,402 passed in 73.28 seconds;
- 76 tests across config rescore and Strategy Lab;
- 23 tests across forecast challengers and scorecards;
- 61 tests for the full Strategy Lab research module;
- project health and secret-pattern checks, with the expected ignored-local-
  runtime warning;
- Python compilation for the changed modules; and
- `git diff --check`.

GitHub Verify
[30124648811](https://github.com/Jaxsonb04/weather_edge/actions/runs/30124648811)
passed for the follow-up code commit on Python 3.12, Python 3.13, and Web. The
Python 3.13 full verification gate included the pinned Semgrep project rules.

No follow-up command contacted a broker endpoint or attempted a live or
production-paper order. No AWS or Pages deployment was made. The query and
report-only changes do not affect order behavior. Any future deployment that
includes the earlier signal/model-integrity remediation remains subject to the
approval and rollback boundary in Section 9.1.

The aggregate local verification wrapper could not run its Semgrep stage
because the optional CLI was absent from this workstation; the pinned,
successful GitHub gate is the authoritative Semgrep result for this revision.

## Security-review limitation

This security review is an AI-assisted first pass, not a substitute for an
assessment by a qualified security professional. Language models can miss
vulnerabilities or misclassify reachability. Engage a professional reviewer
before enabling real-money execution or materially increasing the sensitivity
of production systems.
