# WeatherEdge Research Improvement Review

> Research record dated 2026-06-12. This captures the SFO research state at
> that date; see `MULTICITY-2026-07.md` and the current trading guide for the
> fifteen-city, two-profile maker-first system.

This note translates a literature pass into project-specific work. WeatherEdge
is still paper trading only; the goal is better calibration, more useful paper
trade data, and fewer hidden look-ahead or execution assumptions.

## Best Immediate Changes

1. Make the paper scanner collect more examples with small risk, not bigger
   risk. The CLI now defaults to the `live` profile (the stricter,
   real-trading-candidate book, paper-only until a readiness gate passes), and
   `--risk-profile research` is the single loosest-gate, smallest-size data
   collector that records the full opportunity set. The strict
   `StrategyConfig()` baseline remains internal for tests only.
2. Keep rejecting no-bid tails. Large systematic desks do not treat cheap as
   the same thing as liquid. The active paper profile only loosens small 1c/2c
   markets when there is visible bid support, model/market agreement, and
   positive lower-bound edge.
3. Treat every market decision as a forecast-verification row. This is now
   implemented through `decision_snapshots` and `backtest-signals`, which join
   recorded decisions to official SFO settlement highs when available. The
   signal backtest now defaults to pre-resolution rows and de-duplicates
   repeated scans by target date, market ticker, and side.

## Testing Upgrades

- Use `backtest-signals` for the first point-in-time market signal backtest. It
  scores every recorded decision row, including rejected rows, against official
  settled highs. Repeated 5-minute scans are correlated, so the default sample
  is the latest pre-resolution row per market side, not every raw row. The
  current `backtest-calibration --source clean-blend` remains the right
  probability-only backtest for archived blend forecasts.
- Report paired forecast comparisons, not only aggregate MAE. Use rolling-origin
  evaluation by target date, bootstrap confidence intervals by day, and a
  Diebold-Mariano style test for paired loss differences when comparing model
  families.
- Score probability distributions with proper scores: Brier for binary bucket
  events, log loss for the resolved bucket, and CRPS/PIT diagnostics if the
  continuous high-temperature distribution is exposed.
- Add cohort reports: normal days, warm tails, same-day observed-high locks,
  source-disagreement days, and top trade-quality deciles. Temperature cohorts
  now exist in `backtest-calibration`; quality buckets now exist in
  `backtest-signals`. Same-day lock and source-disagreement cohorts remain good
  next refinements.

## Weather ML Upgrades

- Do not jump straight from the current station LSTM to a giant global model.
  GraphCast, Pangu-Weather, GenCast, and ECMWF AIFS show that ML weather models
  are now serious, but they are trained on global reanalysis fields. For
  WeatherEdge, the practical upgrade is to ingest their public/API outputs as
  additional forecast sources when available, then station-calibrate them to
  KSFO.
- The best next local model is probabilistic, not another point model. Add
  quantile or distributional regression over the current feature set, then
  calibrate intervals with rolling residuals or conformal-style coverage checks.
- Keep the LSTM and XGBoost comparison, but add a lightweight sequence baseline:
  temporal convolution or attention over 48-168 hour station windows. It should
  only ship if it wins in point-in-time validation and improves tail calibration.

## Blend Upgrades

- Replace fixed source weights with a rolling, scored blend. Use prior-day
  archived forecasts only, update weights by recent proper score, and shrink
  toward the configured base weights when sample size is small.
- Calibrate the API blend like an ensemble forecast, not a raw average. BMA or
  EMOS-style post-processing is a good model for bias-correcting each source and
  estimating underdispersion.
- Keep the market price as a prior, but make its weight reliability-aware:
  lower weight when spread is wide, top-of-book size is thin, or the book is
  internally inconsistent; higher weight when the market is liquid. Spread,
  depth, and reciprocal-book consistency now affect the market prior weight.

## Trading Upgrades

- The large-quant analogue here is not high-frequency trading; it is disciplined
  systematic research: signal generation, point-in-time validation, transaction
  cost modeling, risk budgeting, and execution/liquidity filters.
- Run both sides by default in research scans because a binary market can have
  edge on YES or NO. This is now the CLI default for `analyze` and
  `daily-report`.
- Size with fractional Kelly plus hard event risk caps. Fractional Kelly is
  still appropriate because the largest risk is probability miscalibration, not
  arithmetic EV.
- Keep a paper-trading exploration layer separate from any future live layer.
  The `live` profile is the real-money-intent candidate book, but it stays
  paper-only until a readiness gate passes, plus an audited market PnL backtest
  and manual deployment review.

## Sources

- Brier, "Verification of Forecasts Expressed in Terms of Probability":
  https://journals.ametsoc.org/doi/10.1175/1520-0493%281950%29078%3C0001%3AVOFEIT%3E2.0.CO%3B2
- Gneiting, Balabdaoui, and Raftery, "Probabilistic forecasts, calibration and
  sharpness": https://sites.stat.washington.edu/raftery/Research/PDF/Gneiting2007jrssb.pdf
- Gneiting and Raftery, "Strictly Proper Scoring Rules, Prediction, and
  Estimation": https://sites.stat.washington.edu/raftery/Research/PDF/Gneiting2007jasa.pdf
- Raftery et al., "Using Bayesian Model Averaging to Calibrate Forecast
  Ensembles": https://journals.ametsoc.org/view/journals/mwre/133/5/mwr2906.1.xml
- DeepMind, "GraphCast: Learning skillful medium-range global weather
  forecasting": https://deepmind.google/research/publications/22598/
- Price et al., "Probabilistic weather forecasting with machine learning"
  (GenCast): https://www.nature.com/articles/s41586-024-08252-9
- ECMWF, "ECMWF's ensemble AI forecasts become operational":
  https://www.ecmwf.int/en/about/media-centre/news/2025/ecmwfs-ensemble-ai-forecasts-become-operational
- Bi et al., "Accurate medium-range global weather forecasting with 3D neural
  networks" (Pangu-Weather): https://www.nature.com/articles/s41586-023-06185-3
- Diebold and Mariano, "Comparing Predictive Accuracy":
  https://www.nber.org/papers/t0169
- Wolfers and Zitzewitz, "Prediction Markets":
  https://www.aeaweb.org/articles?id=10.1257%2F0895330041371321
- Wolfers and Zitzewitz, "Interpreting Prediction Market Prices as
  Probabilities": https://www.nber.org/papers/w12200
- Moskowitz, Ooi, and Pedersen, "Time Series Momentum":
  https://www.aqr.com/insights/research/journal-article/time-series-momentum
- Man AHL, "AHL Explains" research index, including limit order books,
  execution, and signal diversification:
  https://www.man.com/maninstitute/ahl-explains
