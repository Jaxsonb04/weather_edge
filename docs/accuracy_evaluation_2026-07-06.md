# Forecast accuracy evaluation — 2026-07-06

Point-in-time evaluation of the production forecast candidates on the live
Lightsail archive (`/opt/weatheredge/forecaster/weather.db`), run with
`forecast_postproc_backtest.py` on 2026-07-06 (UTC). All predictors are
rolling-origin (no look-ahead): each day is scored with parameters fit only on
strictly earlier days, against the integer CLISFO/Kalshi settlement high.
Overlap with the baseline is date-matched, n = 31 settled days.

## Head-to-head vs `baseline_blend` (Diebold-Mariano CRPS gate)

A predictor **WINS** only if the DM statistic is negative *and* the bootstrap
CI excludes zero. Negative `mean_delta` = lower (better) CRPS per day.

Lead 1 (next-day, the primary market):

| predictor        | mean ΔCRPS/day | DM stat | p     | 95% CI            | verdict |
|------------------|---------------:|--------:|-------|-------------------|---------|
| climatology      | +0.758         | +2.19   | 0.029 | [+0.157, +1.451]  | loses   |
| nwp_consensus    | +1.254         | +7.19   | 0.000 | [+0.878, +1.547]  | loses   |
| emos_ngr         | −0.511         | −2.34   | 0.019 | [−1.032, −0.160]  | WINS    |
| analog_ens       | −0.418         | −2.12   | 0.034 | [−0.853, −0.069]  | WINS    |
| **emos_wmean**   | −0.433         | −1.93   | 0.053 | [−0.963, −0.090]  | WINS    |
| emos_anen_blend  | −0.498         | −2.52   | 0.012 | [−0.956, −0.178]  | WINS    |

Lead 2 (2-day-out market): emos_ngr, analog_ens, and emos_anen_blend WIN;
emos_wmean ties (mean Δ −0.340, CI [−0.859, +0.021]).

## Per-cohort CRPS at lead 1 (settled cohorts)

| predictor      | cold  | normal | warm  | hot    |
|----------------|-------|--------|-------|--------|
| climatology    | 2.939 | 2.458  | 2.652 | 10.134 |
| baseline_blend | n/a   | 0.903  | 1.644 | 10.242 |
| emos_wmean     | 1.747 | 1.178  | 1.502 | 2.171  |

The hot cohort is where EMOS matters most: CRPS 2.17 vs the blend's 10.24.
Warm (the dominant summer regime) improves 1.64 → 1.50 and its shared-sigma
Brier (0.903) is in line with cold/normal — consistent with the 2026-07-02
decision to unblock warm-cohort trading while keeping hot gated by size.

## What is served in production

`emos_forecast.py --serve-rolling` serves **emos_wmean** (inverse-variance
weighting) at each target's true lead; the trader consumes it via
`load_emos_mu_sigma(lead_days=None)` with the blend as fallback. This
evaluation confirms that choice remains sound at lead 1.

## Hypotheses not acted on (insufficient evidence)

- `emos_anen_blend` is the only variant that wins at *both* leads and has the
  strongest lead-1 DM stat. But the EMOS variants' CIs overlap heavily at
  n = 31; switching the served predictor on this sample is not defensible.
  Re-run this comparison at n ≥ 60 settled days (≈ September 2026).
- Per-cohort Gaussian recalibration was already evaluated 2026-07-02
  (docs/PHASE0-findings.md): statistically indistinguishable from emos_wmean.
  Still true in this run (`emos_wmean_recal` ≈ `emos_wmean` everywhere).

## Limitations

- n = 31 settled days, one season (early summer); marine-layer regimes
  dominate the sample. Winter behavior is unmeasured.
- The DM gate treats days as exchangeable; weather errors are serially
  correlated, so effective n is somewhat lower than 31.
- `baseline_blend` had no settled cold-cohort days in the overlap window
  (its cold cell is empty), so cold-cohort comparisons rest on the other
  predictors' internal consistency only.
