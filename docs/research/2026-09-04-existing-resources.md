# Existing resource use and the next research boundary — September 4, 2026

At **2026-09-05 04:11:31 UTC**, one indexed read-only aggregate query inspected `nwp_model_forecasts` for fifteen stations, leads 1/2, and targets since January 1, 2024. Query time: **0.44 seconds**. No paper DB, production writes, collection, or backfill. Everything else below used the earlier fresh export or source-code inspection.

- **Source mixing is latent in the inspected data.** All **124,804** inspected rows have the single source name `openmeteo_previous_runs`. No alternate source coexists within this scope. The source-agnostic training reader still needs a source boundary before introducing another provider or archive vintage, but it is not evidence of currently mixed-source model fits. Dates before January 2024 and lead3 were outside this query.
- **No post-warmup unseen-member introduction was found in the fresh eighteen-month inputs.** The local replay checked **12,163 eligible station/lead/target rows** with the same `>=3` model history rule, minimum sixty earlier settled days, and target-minus-lead-minus-one truth cutoff; zero contained an unseen member. Historical/live unseen-member handling is inconsistent in code, but the inspected eligible targets do not trigger it. This is a rollout-hardening task before adding models, rather than an identified cause of current trade scarcity. The earlier SFO introductions before the export window were not replayed.
- **All eight serving members have current archive coverage.** For every one of the thirty station/lead series, the latest archived target is September 5 and contains all eight serving models. This measures archive coverage, not the paused live-serving heartbeat. AIFS already exists across all fifteen stations; its first inspected SFO target is February 17, 2025, while non-SFO histories begin in June 2025. It should not be proposed as a missing new resource.
- **GraphCast is historical-only as intended.** The archive retains 7,393 lead1/2 GraphCast rows across fifteen stations, ending **May 21, 2026**. `NWP_MODELS` deliberately excludes it from serving. Because `load_nwp_forecasts` reads historical models without a roster filter, retained GraphCast values can still contribute to earlier training rows; current live members are only the eight configured ones.
- **Partial-hour prevalence is not measurable from these stored records.** The daily NWP table has a high, fetched timestamp, model/lead/source keys, but no valid-hour count, complete-window flag, original issue initialization, or retained hourly series. The code accepts any non-null hourly subset as a daily maximum. We cannot honestly claim that a specific production day is affected or that complete eight-member coverage proves a full twenty-four-hour window. Add prospective coverage metadata before using this as a repair justification.

## Which existing feeds actually contribute

| Resource | Role shown by current code |
|---|---|
| Open-Meteo NBM, ECMWF IFS, GFS, ICON, GEM, ECMWF AIFS, JMA, Meteo-France | Current ensemble inputs to live EMOS; historical canonical rows provide model bias/weight/mean/variance fitting. |
| Official station CLI settlements | Final target truth for fitting, trailing correction and settlement evaluation. |
| NWS station observations | Intraday observed-high context/conditioning; not an additional EMOS training member. |
| Retained GraphCast history | Historical fitting rows only; deliberately excluded from current serving list. |
| Google Weather | TTL runtime fetch/cache. The orchestrator first runs the independent all-city EMOS baseline, then fetches Google. Google values do not enter `serve_live_emos`. Paired-evidence and bracket-shadow helper modules exist, but a repository-wide non-test search finds no caller that actually invokes the paired-evidence producer or shadow entrypoint from the scheduled path. Do not describe that shadow evaluation as running merely because helpers exist. |
| Apple WeatherKit | Expiring runtime cache. `AppleRuntimeCache.active_highs` has no non-test caller outside its defining module; no EMOS fitting or live forecast reader uses it. Existing module policy prohibits building a secondary historical database from Apple weather content. |
| Legacy SFO LSTM/blend calibration | Separate compatibility/calibration paths, not members of the eight-model EMOS fit. Current coupling guards refuse mixing an EMOS point with an unrelated disabled residual distribution. |

A practical next step is to connect a permitted, bounded Google shadow-evidence consumer and measure incremental skill versus the existing independent baseline—or deliberately pause spending on that unused feed while retaining the EMOS baseline producer. **Do not disable the whole main forecaster/Google orchestration unit:** it also runs the all-city EMOS baseline. WeatherKit likewise needs a supported use case or deliberate pause. These are recommendations, not actions performed during this check.

Gitignored operator evidence files in `.local/forecast-experiment/`: `production_prevalence.json`, `prevalence_assessment.json`, `export_prevalence.py`, `assess_prevalence.py`. Relevant source: `forecaster/nwp_archive.py`, `forecaster/truth_store.py`, `forecaster/emos_forecast.py`, `forecaster/google_multicity_refresh.py`, `forecaster/apple_weatherkit.py`, `trading/sfo_kalshi_quant/google_challenger_shadow.py`, and `trading/sfo_kalshi_quant/forecast.py`.


## Use existing decision records before collecting more inputs

`forecast_snapshots` retains the actual served mean, method, fetched/recorded
timestamps and raw EMOS parameters (`mu`, `sigma`, `n_models`, `model_spread_f`,
`lead_days`). Decisions link through scan contexts to these forecast parents,
market observations, intraday timestamps and strategy configuration. Those
records can support evaluation or residual recalibration of the forecasts
actually served, using one deduplicated observation per chosen decision/vintage.
The exact archive coverage and sampling policy still need verification.

They do not contain named per-model highs for a new ensemble fit. A future
member-level challenger should capture those missing values once per new
forecast vintage with provider/first-seen provenance and hourly completeness,
rather than copying them into every repeated scan. Dataset `issued_at` values
synthesized from midnight minus lead are not original provider availability;
upserted `fetched_at` values are not immutable first-seen history.

## Move historical work across the existing cache boundary

The historical Strategy analysis cache already separates expensive research
from the bounded AWS publisher. Keep current accounting, scans, settlement and
publication on AWS; use the Mac or CI for historical comparisons and scoring.
A small exporter, section-specific runner and verified cache importer could
implement this boundary. No runtime or byte-saving claim has been measured.

| Analysis | Required bundle |
|---|---|
| Forecast scoring and fitting | Canonical NWP rows, served daily EMOS rows, final station CLI truth, with model/source/lead, distribution parameters and timestamps |
| Candidate rescoring | Full rows selected by the existing first-approved-otherwise-first pre-resolution sampler, settlements, and separate unsampled raw-count aggregates |
| Execution replay | Complete relevant orders including parent/child lots and diagnostics; account-ledger semantics/entry-fill records; matching public trade tape; maker allocations and volume claims |
| Research shadow | Shadow orders and linked paper-order outcomes |
| Daily analysis | Required trailing decision columns plus original collected-row counts |

Existing `archive/features.db` contains market/side/profile/day features for
exploratory calibration and liquidity analysis. Manifest-verified daily JSONL
archives can restore selected days and referenced forecast/market/context rows
locally, avoiding an indiscriminate full-journal download.

These existing stores do not establish complete replay by themselves:

- The nightly full-table archive omits `paper_maker_allocations` and
  `maker_volume_claims`, which current execution replay reads. Export both
  explicitly and verify their relationships before claiming equivalence.
- Feature rollups collapse intraday behavior and lack account/policy
  fingerprints. They cannot replace execution replay or readiness evidence.
- Sampled decision rows need separate raw-count aggregates so volume reporting
  does not mistake the sample size for the number of collected signals.
- Imported cache/evidence must match source/config fingerprints and input
  hashes, establish complete-day coverage, and declare an explicit input cutoff.
  A newly computed timestamp alone does not make an old export current.
- Fresh account state and production readiness must continue to use complete
  authoritative AWS inputs; incomplete offline bundles must not generate a
  complete production artifact.

The present task performed read-only assessment and the bounded CRPS pilot;
it did not switch providers, schedule a new shadow consumer, import offline
cache evidence, change billing, or promote new forecast/sizing parameters.
The [research survey](2026-09-04-forecast-and-execution-improvements.md) lists
primary papers and maintained repositories, and the
[CRPS pilot](2026-09-04-crps-pilot.md) records the measured exploratory result.
