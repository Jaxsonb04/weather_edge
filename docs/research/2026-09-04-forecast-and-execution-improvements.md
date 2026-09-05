# Forecast and execution improvements — 2026-09-04

This review combines current primary-source research with the checked-out model
code. It does **not** establish a new production performance result. Existing
production facts below are dated session-memory snapshots; AWS runtime data must
be freshly inspected before quoting current accuracy, account equity, or fills.
Live Stability and Research ROI remain economically separate paper accounts.

The best use of existing resources is to establish exactly what forecasts were
available at each decision, test small probabilistic challengers on those same
decisions, and improve capture of already eligible liquidity. Increasing order
count, contract size, and win rate are separate objectives; none alone establishes
better after-fee returns. No finding here supports weakening an entry, loss,
exposure, liquidity, or readiness gate.

## What already exists

- Eight NWP source identifiers, including NBM, IFS, and AIFS Single, are in
  `forecaster/nwp_archive.py`. Adding basic NBM or AIFS again is not an upgrade.
- `postproc_models.py` already implements bias correction, EMOS, optional
  inverse-error-variance weights, and an analog baseline. `emos_forecast.py`
  uses inverse-variance weights and trailing bias correction. Its comments record
  an earlier failed sigma-rescaling experiment; do not silently re-enable it.
- The Google runtime path already has a fixed, capped challenger and paired
  evidence in `google_runtime_blend.py` and `google_paired_evidence.py`. Evaluate
  that evidence before buying more similar point forecasts.
- Apple WeatherKit is a zero-weight shadow source. The documented storage and
  promotion restrictions in `docs/APPLE-WEATHERKIT.md` remain unresolved;
  do not treat a working API credential as permission for durable model training.
- The August 28 memory snapshot records a $1 executable-notional floor and a
  bounded Research ROI partial-depth crossing path. Those are already deployed
  changes, not fresh recommendations. The same snapshot records 194 Research
  signal approvals but no final entries because after-fee edge failed.
- The September 4 incident exposed CPU-credit throttling and repeated expensive
  journal/publication work. More background training or faster repeated scans
  would compete with the system's actual operational bottleneck.

## Ranked work

| Priority | Work | Why it fits | Evidence needed before promotion |
| --- | --- | --- | --- |
| 0 | Audit fixed-issuance forecast history and model-version changes | Protects every later comparison; mostly data work | Every input and truth was available by decision time; report reconstruction gaps |
| 1 | Test CRPS-fitted, regularized EMOS against current OLS EMOS | Small parameter count; existing sources and CPU tools | Paired rolling-origin CRPS and bucket scores improve without harmful city/lead regressions |
| 2 | Score the existing Google challenger and source ablations | Uses resources already collected and budgeted | Same issuance, same target, same truth, explicit missing-source cohorts |
| 3 | Test a pooled quantile forest using a small predictor set | Can capture nonlinear station/regime effects with bounded CPU training | Better calibrated bucket probabilities on untouched future dates, including tails |
| 4 | Add one new ensemble feed in shadow, starting with access feasibility | Potentially new uncertainty information | Freshness, station-target match, retention permission, cost, and incremental skill |
| 5 | Validate fill probability, adverse selection, and scan timing | More useful eligible fills, not larger nominal orders | Forward paper results and conservative replay at unchanged economic gates |

These are priorities, not measured benefit rankings. No new candidate was trained
or deployed by this literature review.

## 1. Fix the comparison's information boundary first

The current archive takes the maximum of `temperature_2m_previous_dayN` across
the settlement day. Before this audit, its introductory comment described this
as known at trade time; that claim is now corrected in the module. Open-Meteo's documentation instead defines previous-day offsets relative
to **each valid hour**: day 1 is the forecast 24 hours before that hour. The
result can combine successive model runs rather than one issuance. A forecast
for 20:00 tomorrow may incorporate a run that did not exist at 08:00 today.
Therefore the reconstructed daily maximum is not proof of what was available at
an arbitrary earlier decision. This is a verified mismatch between the code's
claim and the provider's documented semantics; the size and direction of its
effect on WeatherEdge scores remain unmeasured. It does not establish that
recorded live paper decisions used future inputs.
[Open-Meteo Previous Runs](https://open-meteo.com/en/docs/previous-runs-api).

Use the Single Runs API, which preserves each initialization, for a bounded
comparison window. It documents most models from April 2, 2026 and IFS HRES from
March 2024. Initialization time is not publication time: global model output
typically arrives hours later. Preserve both and use actual first-seen time for
forward evidence. Do not relabel a historical fetch timestamp as historical
availability. [Open-Meteo Single Runs](https://open-meteo.com/en/docs/single-runs-api).

Proposed replay record: settlement station, target day, fixed-standard day
window, model and version, initialization, provider availability, actual
first-seen time, complete-hour coverage, and derivation version. The eligible
forecast must be available before the paper decision; final CLI truth must be
available before any later refit that uses it. Keep reconstructed research
history and actually captured forecast vintages distinguishable.

Also score model-upgrade cohorts. ECMWF reports that AIFS Single and AIFS ENS
were upgraded to v2 on May 12, 2026. A stable API model identifier does not imply
unchanged errors across that boundary. A short recent window, expanding history,
and a shrinkage combination are reasonable predeclared alternatives; select
among them on earlier folds only.
[ECMWF AIFS dataset](https://www.ecmwf.int/en/forecasts/datasets/aifs-machine-learning-data).

## 2. Upgrade distribution fitting before increasing model complexity

Current EMOS fits its mean with OLS and its variance by regressing in-sample
squared residuals on raw model spread. This is a deliberately inexpensive
approximation. The foundational EMOS paper instead develops minimum-CRPS
estimation, directly fitting the predictive distribution to a proper score.
That gives a concrete, small challenger rather than a wholesale architecture
replacement. [Gneiting et al., 2005](https://journals.ametsoc.org/abstract/journals/mwre/133/5/mwr2904.1.xml).

Experiment: keep the current mean/model-weight baseline initially, optimize a
small set of mean and nonnegative variance parameters with the existing sigma
floor, and use shrinkage toward the baseline when samples are thin. Compare
against the unchanged OLS implementation under the corrected issuance boundary.
Fit on an immutable offline dataset, export a small versioned parameter file,
and keep serving cheap. CRPS optimization could improve or worsen WeatherEdge;
only paired out-of-sample evidence can decide.

For a nonlinear challenger, quantile regression forests estimate conditional
quantiles without imposing a Gaussian shape.
[Meinshausen, 2006](https://jmlr.org/papers/v7/meinshausen06a.html).
Start with model highs/missingness, lead, issue hour, season, station metadata,
and a few already captured weather-regime predictors. Pool stations with
station information and regularization; do not pretend fifteen cities on the
same weather day create fifteen independent weather regimes. Fit medians and
tails jointly through a monotone empirical distribution, then map that
distribution to the exact settlement buckets. Raw tree samples have poor
extrapolation beyond observed extremes, so compare a calibrated/shrunken
distribution against EMOS explicitly on heat and cold tails.

The broader motivation for pooling station and meteorological information is
supported by Rasp and Lerch's station-temperature postprocessing study. Its
German surface-temperature results are not a measured gain for US daily
settlement maxima. A small distributional neural network can follow if the
forest and EMOS comparisons justify further complexity.
[Rasp and Lerch, 2018](https://arxiv.org/abs/1805.09091).

Keep the existing analog implementation as a baseline. It currently searches
ensemble mean/spread and reduces the resulting observations to a Gaussian.
One bounded experiment is residual analogs, centered on today's calibrated
mean, with season/regime features computed only from past history. This is a
project-specific hypothesis, not a new result claimed by the literature.

Conformal methods are useful initially as a rolling coverage monitor at 50%,
80%, and 95% intervals. Adaptive conformal inference addresses distribution
shift through online updates; its coverage property should not be presented
as a guarantee for each individual day or each city. An interval alone does
not define the probability of every temperature bucket. Do not turn its
endpoints into a Gaussian sigma without separate probability validation.
[Gibbs and Candès, 2021](https://proceedings.neurips.cc/paper/2021/hash/0d441de75945e5acbc865406fc9a2559-Abstract.html).

## 3. New feeds worth a bounded shadow comparison

**WeatherNext 3 is the most relevant newly published candidate.** The September 3,
2026 preprint specifically addresses raw observations, hourly initialization,
and station-targeted temperature forecasts. These are closer to WeatherEdge's
station-max problem than generic medium-range global benchmarks, but the paper
does not establish WeatherEdge settlement or trading performance.
[Rasp et al., 2026 preprint](https://arxiv.org/abs/2609.03582).

The paper's latency-adjusted evaluation assumes seven hours between nominal
initialization and operational availability. Hourly initialization therefore
does not mean a forecast initialized this hour is available now. Record observed
delivery times when comparing against NBM, IFS, or the existing Google API.
[WeatherNext 3 evaluation methodology](https://arxiv.org/html/2609.03582v1).

Google's current guide lists 64 ensemble members, hourly output, and a 0.05°
station temperature/dewpoint output. Interim hourly runs cover 48 hours;
six-hourly cycles reach 15 days. Historical 2026 data is listed, with older
years being backfilled. Verify actual dates and training cutoffs before treating
backfilled forecasts as an out-of-sample track record.
[WeatherNext 3 model guide](https://developers.google.com/weathernext/guides/models).

Access is allowlisted; Google quotes a typical 5–7 business day review. An
existing Maps Weather API key does not demonstrate entitlement to these
datasets. BigQuery/Earth Engine expose surface statistics; GCS provides the
full ensemble. Start with a cost-bounded station extraction design once access
and applicable data terms are established. No access form was submitted and
no billing was enabled in this review.
[WeatherNext access guide](https://developers.google.com/weathernext/guides/access-forecast).

**AIFS ENS and NOAA AIGEFS/HGEFS are useful diversity candidates.** AIFS Single
is already present, so the incremental question is whether ensemble information
improves calibrated daily-max uncertainty. NOAA's implementation notice dates
AIGFS, AIGEFS, and HGEFS operations to December 17, 2025.
[NOAA implementation notice](https://www.weather.gov/media/notification/pdf_2025/scn25-89_AIGFS_AIGEFS_and_HGEFS.pdf).
Open-Meteo currently lists AIFS and AIGEFS ensembles plus WeatherNext 2. It retains
individual-member history for only a short period, so historical evaluation may
require a new prospective archive. API call-equivalent usage can exceed raw HTTP
request count; query only needed stations, horizons, and variables.
[Open-Meteo Ensemble API](https://open-meteo.com/en/docs/ensemble-api).

ECMWF also distributes open IFS/AIFS data with attribution requirements. Its
documented AIFS temporal steps are six hours, which matters when deriving
daily maxima. [ECMWF Open Data](https://www.ecmwf.int/en/forecasts/datasets/open-data).

For any ensemble, compute the fixed-standard daily maximum **within each
member's trajectory first**, then form the distribution of those maxima.
The maximum of hourly ensemble means is not the mean of daily maxima;
hourly marginal percentiles cannot reconstruct their joint daily-max
distribution. Hourly interpolation of six-hourly native output does not create
new temperature observations or restore an unresolved afternoon peak. Retain
temporal-resolution provenance and station-calibrate the derived daily target.

Do not count 51 AIFS members as 51 independent forecast sources beside eight
deterministic models. Summarize or calibrate each model family, then evaluate
its incremental contribution. NBM, IFS, AIFS, and provider blends share inputs;
more feeds can add cost with little independent information.

## 4. Repositories with practical reuse value

These repositories were inspected online. Maintenance evidence is a snapshot,
not an endorsement of future support. Pin and evaluate dependencies offline
before making them production requirements.

| Repository | Verified practical use | Maintenance / boundary |
| --- | --- | --- |
| [zillow/quantile-forest](https://github.com/zillow/quantile-forest) | Scikit-learn-compatible quantile forests; arbitrary quantiles without refitting | [Release notes](https://zillow.github.io/quantile-forest/releases/changes.html) list 1.4.2 on June 21, 2026; good bounded CPU challenger |
| [frazane/scoringrules](https://github.com/frazane/scoringrules) | Independent CRPS, Brier, log-score and ensemble-score reference | [PyPI history](https://pypi.org/project/scoringrules/) lists 0.11.0 on June 6, 2026; current repository requires Python 3.12+; use in research or numerical cross-checks |
| [scikit-learn-contrib/MAPIE](https://github.com/scikit-learn-contrib/MAPIE) | Time-series/conformal uncertainty experiments | [Releases](https://github.com/scikit-learn-contrib/MAPIE/releases) show v1.4.1; current README describes 2026 adaptive methods; avoid random temporal splits |
| [ecmwf/ecmwf-opendata](https://github.com/ecmwf/ecmwf-opendata) | Official selective forecast downloader | [Releases](https://github.com/ecmwf/ecmwf-opendata/releases) show active retry and IFS-cycle compatibility work; whole-world GRIB retrieval is unnecessary for fifteen stations |
| [google-deepmind/weathernext](https://github.com/google-deepmind/weathernext) | Official WN2/GraphCast/GenCast reference and feed links | Updated model-family code; README recommends accelerators, including H100 for full GPU models; use forecast feeds instead of hosting inference on EC2 |
| [nkaz001/hftbacktest](https://github.com/nkaz001/hftbacktest) | Queue, latency, and partial-fill simulation concepts | [Release history](https://github.com/nkaz001/hftbacktest/releases) lists Rust 0.9.4 / Python 2.4.4 with converter fixes; crypto-oriented interfaces need adaptation, and current WeatherEdge snapshots may not support faithful tick replay |

The old [slerch/ppnn](https://github.com/slerch/ppnn) repository is a useful
research replication reference, not a newly maintained production dependency.
Public weather-bot repositories with unsupported win-rate screenshots are not
evidence of transferable edge.

## 5. More useful trading activity from the existing engine

First publish a compact funnel per account, city, horizon, and side: distinct
candidate roots → signal approvals → after-fee approvals → order submissions →
any fill → filled contracts/notional → settlements. Show the dominant rejection
reason and displayed eligible depth. A repeated five-minute rescore of the same
contract is not another trade opportunity or an independent accuracy sample.

Use the existing order/depth archive to compare maker resting versus the
already deployed partial-crossing rule at the same approved opportunities.
Estimate partial-fill probability, time to fill, cancel/replace delay, adverse
price movement after a fill, and exact fee-adjusted results. Queue-reactive
research motivates conditioning execution on book state, but its high-frequency
order-book assumptions do not transfer automatically to sparse snapshots.
[Huang, Lehalle and Rosenbaum](https://arxiv.org/abs/1312.0563).

HftBacktest explicitly notes that replay cannot change historical markets,
which makes some liquidity-taking simulations unrealistic. Use observed depth
as a cap, do not reuse the same displayed liquidity across simulated orders,
and report pessimistic/central fill scenarios where queue information is absent.
[HftBacktest order-fill documentation](https://hftbacktest.readthedocs.io/en/latest/order_fill.html).

Evaluate scans after new forecast availability and meaningful book changes;
retain required monitoring while caching unchanged computations. Measure actual
decision-to-order latency before adding scan frequency. Export narrow,
immutable research tables and run fitting/replay off the constrained production
host. Reuse model fits once per new forecast/truth vintage rather than once per
market side or repeated scan.

For a purchased binary contract with probability p and price c, expected
profit before other costs is p − c. A 95% win rate at a 97-cent purchase price
has negative expected value before fees. Judge changes on after-fee outcomes,
calibration, and risk; higher win rate and larger sizing can both make a weak
strategy look better while worsening economics. More fills are valuable only
when the same economic and risk gates still pass.

## Evaluation and rollout contract

1. Freeze the current paper baseline and configuration/version fingerprint.
   Export authoritative AWS evidence to an immutable research dataset; do not
   train from stale ignored Mac runtime files.
2. Audit issuance, first-seen time, target-window coverage, official CLI truth,
   finality availability, and provider model versions. Label missing or
   reconstructed evidence explicitly. Retain rejected candidates for scoring.
3. Use rolling temporal folds with earlier-only preprocessing and tuning.
   Keep an untouched final date block. Compare challengers on identical cases
   and also report operational coverage so missing bad days cannot win by
   selection. Restrict the number of variants and record all attempted variants.
4. Report CRPS, bucket Brier and log loss, calibration/interval coverage, MAE,
   and forecast availability by city, lead, issue time, season, and tail cohort.
   Use paired confidence intervals clustered by date; use multi-day blocks when
   residual dependence persists. Never bootstrap raw repeated scan rows as if
   independent.
5. Separately replay the identical execution policy with exact fees, available
   depth, realistic fills, and each account's unchanged limits. Report approved
   roots, fill share, notional, net P&L, worst day, and drawdown separately for
   Live Stability and Research ROI. An improved forecast does not guarantee
   improved trading P&L.
6. Register practical acceptance thresholds before opening the final holdout.
   Keep the candidate in paper shadow until forward evidence corroborates the
   historical result. Fit challenger parameters offline and export small,
   versioned artifacts with an inexpensive baseline fallback.

Publication recovery remains operational work: a fresh website should show
current `published_at`, while cached analysis retains its own honest cutoff.
Suppressing a stale-data warning without restoring successful publication is
not an improvement. This research report does not claim current website
freshness, a production deployment, or a profitable model promotion.
