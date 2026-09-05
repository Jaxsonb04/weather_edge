# Frozen offline ML weather challenger — September 5, 2026

The fixed ML candidate lowered pooled point error, but did **not establish better
probabilistic forecasts than bias-corrected EMOS**. It also worsened point error at
SFO and New York. Retain this as a reproducible offline research result; it does
not support changing served probabilities, trading gates, sizing, or live-money
settings.

## What was tested

One pooled `HistGradientBoostingRegressor` learns official daily-high truth minus
the unweighted eight-model ensemble mean. The final forecast adds the learned
residual back to that mean. Features are station as a native categorical variable,
lead 1/2, sine/cosine day of year, ensemble mean, sample standard deviation, and
each of the eight members' deviations from the mean. There are fourteen features.

The single configuration was fixed before computing this candidate's holdout
scores: squared-error loss, learning rate 0.05, 120 iterations, at most fifteen
leaves, sixty minimum cases per leaf, L2 regularization 10, 64 histogram bins,
seed 20260905, and `early_stopping=False`. There was no search, tuning, random
validation split, or post-result refit. Disabling early stopping avoids its
automatic validation behavior; station is explicitly categorical rather than
treated as an ordered numeric measurement. See the [official scikit-learn
estimator documentation](https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.HistGradientBoostingRegressor.html).

The existing fresh AWS export was reused locally: 119,019 forecast rows and 8,192
official final CLI rows, exported September 5 at 03:52:49 UTC. No new API or
production queries were made. The compressed export is 870,536 bytes; it contains
no Apple content. The fixed roster is NBM, ECMWF IFS, GFS, ICON, GEM, ECMWF AIFS,
JMA, and Meteo-France. Older GraphCast rows are excluded. Every eligible case
requires all eight members and matching truth; duplicate keys, unexpected
sources, and nonfinite values fail validation.

The final fit uses **11,101 complete station/lead/date cases**, with truth dates
March 5, 2025 through **June 3, 2026**. June 3 is the common conservative cutoff
before the June 6 evaluation start: maximum lead two days plus one additional
day. Neither means nor uncertainty change during evaluation.

## Predictive uncertainty and evaluation

The point learner is not itself a probabilistic model. Its Gaussian uncertainty
is estimated from forward predictions in the three complete calendar months
before evaluation:

| Prediction month | Latest eligible fit truth | Fit cases | Calibration cases |
|---|---|---:|---:|
| March 2026 | February 26 | 8,274 | 930 |
| April 2026 | March 29 | 9,204 | 817 |
| May 2026 | April 28 | 10,021 | 930 |

Only complete-roster cases enter these folds; missing April inputs are not
imputed. For each station/lead, let `S` be its sum of squared forward errors,
`n` its calibration count, and `M` the pooled mean squared forward error. The
fixed Gaussian sigma is `max(1.5, sqrt((S + 30*M)/(n + 30)))` degrees Fahrenheit.
This is RMSE about the predicted mean, including any remaining bias, with thirty
pooled pseudo-cases of shrinkage. The raw ensemble receives the same uncertainty
procedure using its own errors. No holdout outcomes set either scale. This
simple uncertainty method is deliberately explicit, but transferring spring
error scales to summer is an unverified assumption.

Evaluation uses **exactly the earlier experiment's 2,658 paired cases** over
**June 6–September 3, 2026**, across fifteen stations and both leads. There are
2,700 possible cases: June 6–8 each lack fourteen non-SFO lead-one cases; the
remaining 87 dates contain all thirty station/lead pairs. A missing ML input,
duplicate paired key, or mismatched truth fails rather than shrinking the
denominator. All four earlier EMOS arms are retained, including their existing
trailing bias correction. Their means, sigma, CRPS, and MAE were not refitted by
this script.

**Comparison asymmetry:** the ML point model and uncertainty are frozen before
June 6, whereas the existing EMOS arms expand their training and update their
correction using earlier holdout truth at the recorded conservative lag. This
compares these explicit forecasting procedures, not just the algorithms under an
identical update schedule. The period had already been inspected in the earlier
EMOS experiment; it is not a newly untouched confirmatory test set.

## Results

Lower MAE and Gaussian CRPS are better. Both scores and sigma are in °F. Coverage
is the fraction inside `mu ± 1.28155*sigma`, a nominal 80% central interval.

| Method | MAE | Gaussian CRPS | 80% interval coverage | Mean sigma |
|---|---:|---:|---:|---:|
| Frozen ML residual candidate | 1.604579 | 1.184925 | 88.90% | 2.5878 |
| Raw unweighted ensemble, pretest-calibrated sigma | 2.317668 | 1.631941 | 88.79% | 3.4905 |
| Existing raw EMOS | 1.671399 | 1.202340 | 83.15% | 2.3215 |
| Existing bias-corrected EMOS | 1.642986 | 1.181990 | 83.90% | 2.3215 |
| Earlier raw CRPS-fitted EMOS challenger | 1.658586 | 1.188033 | 81.98% | 2.2135 |
| Earlier bias-corrected CRPS-fitted EMOS challenger | 1.632072 | 1.168693 | 82.66% | 2.2135 |

Against bias-corrected EMOS, ML reduced MAE by **0.038407°F (2.34%)**, with a
paired seven-day date-block 95% interval **[-0.080685, +0.008867]**. Its CRPS was
**0.002935°F worse (+0.25%)**, interval **[-0.023373, +0.032125]**. Both intervals
include zero. Against the earlier bias-corrected CRPS challenger, ML CRPS was
**0.016232°F worse (+1.39%)**, interval **[-0.011388, +0.046376]**. ML used broader
intervals and its lower pooled point error did not translate into a better
distribution score. High coverage alone is not better calibration: 88.9% exceeds
the 80% target.

Intervals use 5,000 circular seven-calendar-day block resamples, seed 20260905.
All stations and leads on a sampled date stay together. Each draw divides total
paired score differences by total cases, preserving the case-weighted estimand
despite the first three dates' smaller denominator. Fourteen-day blocks give
CRPS intervals **[-0.026360, +0.035437]** against bias-corrected EMOS and
**[-0.013241, +0.048372]** against the corrected CRPS challenger; the interpretation
does not change. These intervals describe this observed period, not future
performance guarantees.

| Lead | Cases | ML MAE | Corrected EMOS MAE | ML CRPS | Corrected EMOS CRPS |
|---|---:|---:|---:|---:|---:|
| 1 | 1,308 | 1.478591 | 1.516550 | 1.099447 | 1.085222 |
| 2 | 1,350 | 1.726648 | 1.765488 | 1.267743 | 1.275747 |

All thirty station/lead results follow. CRPS differences are ML minus
bias-corrected EMOS; negative favors ML. Subgroup intervals are exploratory and
unadjusted for multiple comparisons. There is no selection of cities for serving
based on these results.

| Station | Lead | Cases | ML MAE | Corrected EMOS MAE | CRPS difference | 95% date-block interval | ML 80% coverage |
|---|---:|---:|---:|---:|---:|---|---:|
| KATL | 1 | 87 | 1.3200 | 1.4073 | -0.0552 | [-0.1095, +0.0018] | 96.6% |
| KATL | 2 | 90 | 1.6950 | 1.7681 | -0.1115 | [-0.1868, -0.0266] | 87.8% |
| KAUS | 1 | 87 | 1.1711 | 1.1593 | +0.0531 | [-0.0377, +0.1463] | 98.9% |
| KAUS | 2 | 90 | 1.3830 | 1.3556 | +0.0206 | [-0.0738, +0.1060] | 91.1% |
| KBOS | 1 | 87 | 1.7673 | 1.9018 | +0.0293 | [-0.0311, +0.0840] | 96.6% |
| KBOS | 2 | 90 | 2.1034 | 2.1224 | +0.0564 | [-0.0773, +0.1926] | 97.8% |
| KDEN | 1 | 87 | 1.3232 | 1.3069 | +0.0282 | [-0.0215, +0.0804] | 96.6% |
| KDEN | 2 | 90 | 1.7427 | 1.8067 | -0.0570 | [-0.1413, +0.0284] | 93.3% |
| KDFW | 1 | 87 | 1.3312 | 1.5097 | -0.0590 | [-0.1637, +0.0419] | 87.4% |
| KDFW | 2 | 90 | 1.4848 | 1.6891 | -0.1235 | [-0.2240, -0.0261] | 91.1% |
| KHOU | 1 | 87 | 1.2971 | 1.5025 | -0.0525 | [-0.1760, +0.0701] | 87.4% |
| KHOU | 2 | 90 | 1.5116 | 1.6960 | -0.0442 | [-0.1940, +0.0928] | 84.4% |
| KLAX | 1 | 87 | 1.2546 | 1.4480 | -0.0536 | [-0.1275, +0.0188] | 94.3% |
| KLAX | 2 | 90 | 1.6536 | 1.6218 | +0.0331 | [-0.1031, +0.1730] | 88.9% |
| KMDW | 1 | 87 | 1.4001 | 1.3566 | +0.1133 | [+0.0045, +0.2112] | 92.0% |
| KMDW | 2 | 90 | 1.7288 | 1.6393 | +0.1420 | [+0.0258, +0.2452] | 93.3% |
| KMIA | 1 | 87 | 0.9976 | 1.1070 | -0.0254 | [-0.1023, +0.0438] | 92.0% |
| KMIA | 2 | 90 | 1.0046 | 1.0994 | -0.0364 | [-0.0958, +0.0200] | 96.7% |
| KNYC | 1 | 87 | 1.9340 | 1.6462 | +0.1987 | [+0.1032, +0.3137] | 80.5% |
| KNYC | 2 | 90 | 2.2223 | 1.8599 | +0.2324 | [+0.0997, +0.3893] | 90.0% |
| KOKC | 1 | 87 | 1.9658 | 1.9191 | +0.0084 | [-0.1080, +0.1293] | 86.2% |
| KOKC | 2 | 90 | 2.1031 | 2.2596 | -0.0960 | [-0.1903, +0.0132] | 76.7% |
| KPHL | 1 | 87 | 1.3287 | 1.4794 | -0.0679 | [-0.1279, -0.0066] | 85.1% |
| KPHL | 2 | 90 | 1.6908 | 1.8387 | -0.0576 | [-0.1328, +0.0144] | 90.0% |
| KPHX | 1 | 87 | 1.2080 | 1.1302 | +0.0691 | [+0.0047, +0.1377] | 86.2% |
| KPHX | 2 | 90 | 1.4115 | 1.3710 | +0.0297 | [-0.0332, +0.0946] | 82.2% |
| KSEA | 1 | 87 | 1.7601 | 1.8083 | -0.0376 | [-0.2001, +0.1258] | 83.9% |
| KSEA | 2 | 90 | 1.7944 | 1.9972 | -0.1699 | [-0.3167, -0.0360] | 83.3% |
| KSFO | 1 | 90 | 2.0987 | 2.0476 | +0.0629 | [-0.1019, +0.2108] | 80.0% |
| KSFO | 2 | 90 | 2.3702 | 2.3576 | +0.0618 | [-0.0833, +0.2004] | 77.8% |

## Validation and reproducibility

Nine focused tests passed in 9.33 seconds. They verify future-label poisoning
cannot change means or uncertainty, conservative fold boundaries including
early-month starts, exact paired-denominator failures, duplicate/source checks,
Gaussian CRPS against independent numerical CDF integration, case-weighted block
resampling, deterministic repetition, and reconciled subgroup totals. Optional
training dependencies are skipped explicitly in lightweight CI environments;
all nine ran locally with them installed. Production dependency files did not
change.

The actual full experiment completed in **3.21 seconds** locally, measured with
`/usr/bin/time -p`, including input loading and result writing but excluding the
already completed AWS export. A second independent CLI invocation produced
byte-identical `results.json`, `scored_cases.csv`, and `calibration_cases.csv`.
All 2,658 input keys retained their original order, and all 21,264 recomputed
baseline MAE/CRPS values (four arms, two scores per case) matched the original CSV
exactly. Separate numerical CDF integration of six actual ML forecasts matched
their CRPS within 2.0e-15°F.

An independent reviewer also recomputed all 15,948 Gaussian scores (maximum
difference 2.66e-15°F), all sixty pretest uncertainty estimates, and a separate
10,000-replicate calendar-block bootstrap with a different seed. Its seven-day
MAE interval was [-0.08294, +0.00870]°F and CRPS interval [-0.02443, +0.03201]°F,
again including no improvement. Both fourteen-day intervals also crossed zero.
The independent review found no blocking implementation error or holdout-label
leakage, while retaining the methodological limitations below.

Run from the repository root with the existing local training environment:

```bash
.venv-dev/bin/python scripts/weather_ml_experiment.py \
  --export .local/forecast-experiment/fresh-export.json.gz \
  --baseline .local/forecast-experiment/paired_scores.csv \
  --output-dir .local/forecast-experiment/ml-challenger-new-run

.venv-dev/bin/python -m pytest forecaster/tests/test_weather_ml_experiment.py -q
```

The output directory must be new. The script performs no network access, creates
no trained pickle, and caps numerical thread pools at two during fitting. The
local run used Python 3.13.0, NumPy 2.5.0, SciPy 1.18.0, and scikit-learn 1.9.0.
Cross-version binary reproducibility is not promised. Inputs and detailed
outputs remain in ignored operator state; the script and this sanitized report
are tracked. SHA-256 provenance:

| Artifact | SHA-256 |
|---|---|
| Fresh compressed export | `d3c6fab518fed03c1f5a9da3d8976bf5f1931348cd1d6b6456df676f5df38b8e` |
| Existing paired baseline CSV | `66274104775116ea47a5c76365cac32ac3206dcff1b763c1ec5cb39671223db2` |
| Experiment script at execution | `77f519cb4c96b6fc9d44df5cab0ab806185bc03abf8ff6bfa829a311d083989a` |
| Result JSON | `980fcd28d3c628287756770ab8f030e0b3d44d4cf38c0a7b93bb5afafed60183` |
| Scored cases CSV | `4eed5f59ef680fc178011c178805adcef87c6d922a5c67f78622a29c8364ee92` |
| Pretest calibration cases CSV | `bdf4d8d7d12c7e4986b8016c655f6a1f332978b396ec5ee18304dce7e7fd46f5` |

## Limits and next research boundary

This export contains fixed-lead daily maxima, not one model initialization
available at an actual historical trading decision. It also lacks hourly
completeness metadata. Current final CLI values and the truth-date lag do not
reconstruct original publication/revision timestamps. The [Open-Meteo Previous
Runs documentation](https://open-meteo.com/en/docs/previous-runs-api) describes
the per-valid-hour lead convention; decision-time validation needs archived live
inputs or appropriately selected Single Runs data.

This experiment demonstrates a useful resource boundary: a compact weather-only
export can support local ML work without downloading the paper journal or adding
training dependencies to AWS. The measured runtime belongs to this experiment,
not a general production speed claim.

The next defensible research step is to predeclare an uncertainty model and a
fresh future or independent-season evaluation, with particular attention to SFO,
New York, Chicago, and seasonal transfer. Do not tune the reported holdout until
its winner changes. Neither weather score establishes bin/side calibration,
executable quotes, fees, fills, exits, more volume, larger positions, wins, or
profit. Production behavior was unchanged by this experiment.
