# Offline constrained-CRPS EMOS comparison — September 4, 2026

This is a **fixed-lead weather-skill experiment**, not a trading-time replay, a production forecast validation, or evidence of increased wins, fills, volume, position size, or profit. Production forecasts, sigma, and risk gates were not changed.

## Data and evaluation design

One indexed read-only export from the authoritative AWS weather database was taken at **2026-09-05 03:52:49 UTC**. The export contains 119,019 canonical `openmeteo_previous_runs` forecast rows and 8,192 official final CLI truth rows, beginning March 5, 2025. No paper database was queried. The compressed local export is 870,536 bytes. All fitting and evaluation ran locally.

The comparison uses all fifteen stations, leads 1 and 2, and a fixed common roster of eight models: NBM, ECMWF IFS, GFS, ICON, GEM, ECMWF AIFS, JMA, and Meteo-France. A history or evaluation row must have every roster member; both arms receive identical rows. This complete-roster restriction and the limited eighteen-month training window differ from the full-history, potentially missing-member production fit. **The baseline is an operational-fitting-method comparison, not an exact reconstruction of the deployed forecast.**

Before inspecting outcomes, the evaluation window was set to the latest ninety final calendar days: **June 6–September 3, 2026**. Expanding training for target `D` at lead `L` uses only truth dates `<= D-L-1`, with at least sixty complete training days. There are **2,658 paired station/lead/day cases**, versus 2,700 possible. June 6–8 lack fourteen non-SFO lead-1 cases each; all remaining 87 calendar dates have all thirty station/lead pairs. No city or losing result was removed after scoring.

The raw baseline uses the repository's current `fit_emos(..., weight_mode='inv_var')` and `apply_emos`: per-member bias and inverse-error-variance weights, an affine ensemble mean, and squared-error regression for predictive variance. The challenger holds those member biases, weights, and input spread definition fixed, and instead minimizes mean Gaussian CRPS over the affine-mean and variance coefficients with nonnegative slope/variance coefficients. Both keep the same **1.5°F sigma floor**. The challenger falls back to the baseline if optimization fails or its training CRPS is worse. All 2,658 evaluation fits converged; 127 cases (4.8%) took the training-objective fallback. Maximum optimizer iterations: 48.

Both raw fits and a secondary operational-style trailing bias correction were prespecified. Each arm's correction is estimated from its own earlier uncorrected out-of-sample errors, using the repository's existing shrinkage/significance-deadband rule and **no sigma correction**. For target `D` at lead `L`, that error window uses truth dates `[D-L-45, D-L-1]`. Thus the fit and trailing correction both use the same strict earlier-day truth boundary. Fifty additional earlier prediction days warm the correction before evaluation.

## Results

Lower CRPS and MAE are better. CRPS values and differences below are in °F.

| Evaluation | Baseline CRPS | Challenger CRPS | Relative change | Baseline MAE | Challenger MAE |
|---|---:|---:|---:|---:|---:|
| Raw fits, pooled | 1.202340 | 1.188033 | -1.190% | 1.671399 | 1.658586 |
| With trailing bias correction, pooled | 1.181990 | 1.168693 | -1.125% | 1.642986 | 1.632072 |
| Corrected lead 1 | 1.085222 | 1.075102 | -0.932% | 1.516550 | 1.503708 |
| Corrected lead 2 | 1.275747 | 1.259371 | -1.284% | 1.765488 | 1.756442 |

For the bias-corrected pooled comparison, the case-weighted paired CRPS difference is **-0.013297°F**, with a circular seven-day date-block bootstrap 95% interval **[-0.018739, -0.008346]**. Every sampled date carries all available stations and leads together, preserving their same-date dependence; 5,000 replicates use seed 20260905. Fourteen-day blocks give **[-0.018541, -0.008959]**. An equal-date-weight calculation gives -0.013106°F with interval [-0.018520, -0.008232]; this is a different estimand because the first three dates have fewer available pairs.

A clearly labeled robustness check restricted to the **87 fully populated calendar dates**, without selecting on outcomes, retains 2,610 cases: difference **-0.013523°F**, seven-day interval **[-0.018768, -0.008508]**, fourteen-day interval [-0.018690, -0.009352]. The pooled direction survives these checks. The absolute MAE improvement is only about **0.011°F**, so statistical evidence of a small score improvement should not be described as a large accuracy breakthrough.

The improvement is not uniform. Corrected **Philadelphia lead 2 worsens 1.64%**, with its date-block paired-difference interval wholly above zero ([+0.00510, +0.04005]). Small corrected lead-1 regressions also occur for Philadelphia (+0.25%), Chicago (+0.17%), and Phoenix (+0.04%). Several city intervals cross zero. Those subgroup intervals are exploratory and are not adjusted for thirty comparisons. Every city/lead result is retained in `comparison_results.json`.

## Validation and limitations

Independent arithmetic checks compared all **10,632** stored Gaussian CRPS values to the repository's scalar scorer (maximum difference 1.78e-15), exactly reproduced sixteen baseline fits, verified the common model roster and target truth joins, and checked the actual optimizer's analytic gradients against central differences (maximum discrepancy 2.25e-9). Main comparison runtime was about 34 seconds on the local development environment. A second agent independently numerically integrated Gaussian-CDF CRPS for six saved forecasts (maximum discrepancy 6.7e-16), recomputed the pooled relative improvement, and reproduced negative seven-/fourteen-day bootstrap intervals using a separate seed and 10,000 draws; that read-only review found no numerical or cutoff error.

Important limitations:

1. **Feature vintage is unresolved.** Open-Meteo Previous Runs uses a fixed lead for each valid hour. A daily maximum can combine forecasts issued at different times, so it is not a single run available at a historical trading decision. The experiment cannot support decision-time economic promotion. Official source: [Open-Meteo Previous Runs documentation](https://open-meteo.com/en/docs/previous-runs-api).
2. The model roster/history restriction means these are not exact deployed baseline probabilities; the trailing correction is regenerated from this experiment's histories rather than copied from the operational rolling-origin archive.
3. Truth uses current final CLI records. The conservative target-date lag does not reconstruct original CLI publication times or historical revisions, particularly for forecasts notionally served soon after local midnight.
4. This is one prespecified recent ninety-day evaluation. The variant was not tested across independent future seasons. Bootstrap intervals are conditional on this observed sample, not an assurance of future performance.
5. The complete eight-model roster does **not** prove complete twenty-four-hour inputs. The existing daily-high reconstruction accepts partial hourly coverage, and the source table/export lacks hourly coverage metadata. That preexisting limitation remains.
6. We tested neither bin/side calibration nor actual order-book timing, executable fees, fills, exits, risk limits, or policy lineage. A lower weather CRPS can coexist with worse trading returns.

The result supports retaining a **CRPS-fitted EMOS shadow challenger for further evaluation**, including Philadelphia regression investigation and a proper Single Runs/live-input archive. It does not authorize serving this challenger or loosening any trading gate.

## Reproducible operator artifacts

- `export.py` and `fresh-export.json.gz`: the single fresh read-only export.
- `run_comparison.py` and `run.log`: frozen comparison implementation and execution log.
- `paired_scores.csv`: every paired evaluation observation.
- `comparison_results.json`: full pooled, lead, city, coverage, and optimizer results.
- `validate_comparison.py` and `validation_results.json`: independent arithmetic/gradient and bootstrap robustness checks.

Run locally with the repository's `.venv-dev/bin/python`; no production connection is required after export. The experiment implementation, export, row-level outputs and checksums remain in gitignored `.local/forecast-experiment/` operator state. This report records the method and results; no production forecast parameters were changed.
