# Implementation Plan — Calibration-Edge Upgrade for WeatherEdge

**Date:** 2026-06-22
**Status:** Phase 0 + 0.5 COMPLETE (branch `feat/calibration-edge-phase0`, 2026-06-22) — see the results section at the bottom; the data verdict redirects the plan. Phases 1–4 awaiting go-ahead.
**Research basis:** [research_market_edge_and_tmax_2026-06-22.md](research_market_edge_and_tmax_2026-06-22.md)
**Related:** [audit_2026-06-20.md](audit_2026-06-20.md) (warm/hot anti-calibration root cause), [trade_engine_overhaul_plan_2026-06-17.md](trade_engine_overhaul_plan_2026-06-17.md)

---

## Goal

Win more SFO daily-high (`KXHIGHTSFO`) trades by closing the warm/hot calibration gap from three sides at once:
1. **Fix the model's regime-dependent bias** (research §2.1 — Du & DiMego).
2. **Fade the over-confident market signal** the engine consumes (research §1.3 — arXiv 2602.19520).
3. **Size for residual uncertainty** (research §1.2 — Baker & McHale 2013).

**North star:** turn the warm/hot regime from a *fully-blocked liability* (today `blocked_forecast_cohorts=(warm,hot)` on **both** profiles — zero edge captured there) into a calibrated, conservatively-sized, walk-forward-validated opportunity. The −22.5% book came from warm/hot; that is precisely the regime to make tradeable *correctly*, not to abandon.

---

## Success metrics (how we'll know it worked)

Measured on walk-forward replay vs. the current production config, scored on **CLISFO** settlement truth:

| Metric | Source harness | Target |
|---|---|---|
| Warm/hot cohort **Brier-skill** vs climatology | `backtest.py`, `forecast_backtest.py` | > 0 (currently negative — anti-calibrated) |
| Warm/hot cohort **MAE** (point forecast) | `forecast_backtest.py` | strictly lower than production, no cold/normal regression |
| Blended-posterior **ECE** (esp. short-horizon) | `backtest.py` / `strategy_research.py` ECE | lower than baseline |
| **Log-growth per independent day** | `backtest_rescore.py` | ≥ baseline (whole book), with reduced variance |
| **Day-clustered ROI CI lower bound** | `backtest_rescore.py` | ≥ baseline; ≥ 0 for warm/hot before any re-enable |

---

## Decisions the code-trace locked (resolves the research's open questions)

| Question | Resolution | Why |
|---|---|---|
| θ-recal on model probs, market probs, or both? | **Market-implied only** | Model probs already get multi-stage calibration (`probability.py:139-244`); the market signal is raw (only `/sum` de-vig + weight haircut). θ on the model = double-correction. |
| Does θ double-correct the existing de-vig? | **No — orthogonal** | `/sum` removes the overround (preserves odds ratios); `σ(θ·logit p)` reshapes *confidence*. Must re-normalize after θ and re-tune `market_prior_weight`. |
| Where does regime bias go so the gate stays coherent? | **Upstream in the blend producer** (`forecaster/google_weather_cache.py:403`, writer of `forecast_blend_daily_high`) | Correcting the stored `predicted_high_f` means both the regime gate (`risk.py:83`) and `bucket_probabilities` see the corrected high. Prototyped in `exp_tail_lstm.py:101` (`fit_bias`/`apply_bias`). |
| Uncertainty-Kelly — new math? | **Generalize existing YES-only shrink** `_yes_sizing_factor` (`risk.py:475`) to both sides | `k=e²/(e²+(σ/(1-cost))²)` is already implemented and is exactly Baker & McHale (2013). |
| Stop-loss veto still dead? | **No — already fixed** | `exits.py` reads `model_side_probability` from `decision_snapshots` (`db.py:529`), written every scan tick. No action needed. |

---

## Current state that shapes the plan

- **Trades only `KXHIGHTSFO`** (SFO daily high; 197 refs, no other series). Concord/inland is a *predictor*, not a traded market.
- **warm/hot currently BLOCKED in both live and research** (`config.py:271,376`). So no warm/hot edge is captured anywhere today.
- **Truth = CLISFO settlement** (integer half-up, `clisfo.py`). NWS-hourly max is a known wrong-truth trap (`forecast_backtest.py:782` quantifies the divergence/bin-flips).
- **Three harnesses already exist and map ~1:1 to the three changes** — we reuse, not build:
  - point-forecast walk-forward: `forecaster/forecast_backtest.py` (PredictorFn A/B via `compare_forecasters` + `evaluate_acceptance`, with a fail-closed tail-regression gate).
  - probability calibration walk-forward: `trading/sfo_kalshi_quant/backtest.py` (refits `ResidualCalibrator` each step).
  - config-rescore P&L: `trading/sfo_kalshi_quant/backtest_rescore.py` (`run_rescore` + `compute_real_money_readiness`, day-clustered bootstrap ROI CI).
- Per-trade stats already present: Diebold-Mariano, day-clustered bootstrap CI, Brier/log-loss/Brier-skill, reliability buckets, ECE, cohort splits, pinball (in `exp_quantile_lstm.py`).

---

## Sequencing rationale

Implement in **dependency order**, not report-number order: **#2 regime de-bias → #1 θ market-recal → #3 uncertainty-Kelly**. The model signal must be trustworthy before computing edge against it (#1) or sizing on it (#3); #1 changes live behavior (the 0.45 market prior is always blended even on live); #3 is a refinement on already-corrected signals. Warm/hot re-enable is the gated payoff last.

Every knob ships **off/identity on `live`**, is validated in **research** walk-forward first, and flips live only after the readiness gate passes — the codebase's established pattern.

---

## Phase 0 — Plumbing & regression baseline *(no behavior change)*

- Add config fields, all **identity/no-op on `live`**: `theta_recalibration` (default 1.0), `regime_bias_*`, `cohort_kelly_multipliers`, near the `comfort_edge_*` block in `config.py:46`.
- Add a calibrator-factory seam to `backtest.py:73` (today it hardcodes `ResidualCalibrator(train, cfg)`).
- **Acceptance:** all three harnesses with defaults → bit-identical to current results (proves the plumbing is inert). Existing `trading/tests/test_*` stay green.

## Phase 0.5 — Data-sufficiency pre-flight *(decides whether 1–2 are even fittable)*

The research explicitly warns the warm/hot tail is **data-limited** — EVT/regime fits can't manufacture data. Before fitting anything:

- Count **settled warm (70–79°F) and hot (80°F+) days** in the CLISFO history, and the count of **historical market snapshots** in those cohorts (`decision_snapshots` joined to settlements).
- Decision rule:
  - If warm/hot settled-day counts are adequate (rule of thumb: ≥ ~30 per cohort for a shrunk fit, mirroring `exp_tail_lstm.py` `SHRINK_K=30`) → proceed with per-cohort fits.
  - If sparse → **widen the regime definition** (continuous synoptic features instead of hard cohort bins) and lean harder on shrink-to-global; document the reduced confidence.
- Output: a short data-readiness note appended to this doc. **No fitting proceeds until this gate is recorded.**

## Phase 1 — Regime-dependent bias correction *(research §2.1 — Du & DiMego; root cause, forecaster-side, lowest risk)*

- **What:** replace the single forecast-conditional offset with a **synoptic-regime-conditional** bias. Key the regime on the real drivers already in `features.py`, *not* the predicted temperature bin (avoids circularity):
  - `offshore_flow` / `offshore_flow_strength` (`features.py:160-170`) — the dominant SFO warm-vs-cool sea-breeze driver.
  - pressure tendency (`features.py:138-142`).
  - **inland-heat lead** (Concord leads SFO next-day heat, `features.py:183-196`) — the specific synoptic precursor of SFO warm/hot days, so it directly sharpens the regime that hurts us.
  - Shrink each regime's correction toward the global mean (port `fit_bias`/`apply_bias` from `exp_tail_lstm.py:101-113`, `SHRINK_K=30`).
- **Seam:** extend the existing rolling de-bias in `forecaster/google_weather_cache.py` (writer of `forecast_blend_daily_high:403`; it already runs walk-forward holdout gates ~lines 1464/1788). Regime label must use predictors known at forecast time only — no leakage.
- **Validate (existing harness):** write a new `PredictorFn` (`forecast_backtest.py:193`), run as `candidate` vs `make_production_predictor` through `compare_forecasters` (`:609`) + `evaluate_acceptance` (`:658`). The fail-closed **tail-regression gate** (`:702`) already protects warm/hot. Score on CLISFO.
- **Risk & mitigation:** overfitting sparse warm/hot bins → shrinkage + Phase-0.5 min-sample floors + rolling-origin only.
- **Acceptance:** warm/hot-cohort MAE & Brier-skill improve, **no regression** in cold/normal; Diebold-Mariano significant; tail-regression gate passes.

## Phase 2 — θ-recalibration of the market-implied signal *(research §1.3 — arXiv 2602.19520)*

- **What:** apply `p* = σ(θ·logit p)` with **θ<1 at short horizons** to de-extreme the over-confident market prob, then re-normalize across the ladder.
- **Seam:** `probability.py:579-582` (`_market_implied_probabilities`, the single de-vig chokepoint) **and** `consensus.py:124` (`build_market_consensus`, so `implied_high_f`/`stdev` used by the regime gate `risk.py:76` and guard `risk.py:612` stay consistent). Gated by `theta_recalibration`: identity on live, active in research first.
- **Fit θ on our own data — NOT the preprint's numbers** (it aggregates temp+precip+natural-events, no CIs). One-off script: logistic recalibration slope of market-implied prob vs **CLISFO-settled** realized YES, bucketed by horizon (≤24h / ≤48h) and cohort, over the `decision_snapshots` + settlements that `backtest_rescore` already replays.
- **Interaction note (important):** in **research**, `edge_gate_uses_model_probability=True` (`config.py:419`) — edge is computed vs the *pure model* prob, so θ-on-market reaches P&L mainly through the **regime gate/guard** and the **sizing LCB**, not the raw edge gate. On **live**, edge uses the *blended* posterior, so θ-on-market moves edge directly. Therefore: validate calibration with `backtest.py` (primary), and validate marginal P&L with `backtest_rescore` under **both** profiles.
- **Risk & mitigation:** θ compounds with the reliability weight-shrink (`probability.py:670`) → re-tune `market_prior_weight`; θ must be applied **before** re-normalization and a second `/sum` applied after; effect may be muted where the model dominates the blend → measure marginal P&L, not just calibration.
- **Acceptance:** blended-posterior ECE/Brier-skill improves (esp. warm/hot, short-horizon) with research-walk-forward ROI CI lower bound ≥ baseline.

## Phase 3 — Uncertainty-scaled Kelly *(research §1.2 — Baker & McHale 2013)*

- **What:** generalize the YES-only variance shrink `_yes_sizing_factor` (`risk.py:475`) into a **side-agnostic** confidence multiplier; shrink harder when σ is wide / cohort is miscalibrated.
- **Seam:** apply at `risk.py:235→236` (after `kelly_fraction_spent`, before `*fractional_kelly`); mirror in the YES branch (275-277). σ from `forecast_sigma_f` (already threaded, `cli.py:1064`); cohort miscalibration from the cohort Brier-skill that today only reaches the readiness gate (`strategy_research.py:871`) — wire it through config. Optional graded regime down-weight at `risk.py:254-271` (after gates, so it cannot resurrect blocked trades), hoisting cohort resolution out of the `if` at `risk.py:64`.
- **Config:** `cohort_kelly_multipliers` / `low_confidence_kelly_scale`, no-op on base, active live only after validation.
- **Validate:** `run_rescore` (`backtest_rescore.py:359`) per-profile (the `for name in ("live","research")` loop at `strategy_research.py:826` is the A/B template); diff `log_growth_per_independent_day` + `roi_ci95_day_clustered`; gate with `compute_real_money_readiness`.
- **Risk & mitigation:** over-shrinking kills edge capture (size → 0) → floor the multiplier; only down-weight, never resurrect blocked trades.
- **Acceptance:** research-walk-forward log-growth/day not worse, variance/drawdown reduced; readiness check passes.

## Phase 4 — Conditional warm/hot re-enable *(the payoff — most gated, last)*

Only after 1–3: relax `blocked_forecast_cohorts` in **research first** (`config.py:376`). Require walk-forward warm/hot cohort **Brier-skill > 0 AND day-clustered cohort ROI CI lower bound ≥ 0** over a meaningful sample (Phase-0.5 count) → paper-trade live at tiny size (research profile) → only then consider flipping the live block. Fail-closed throughout. This recovers the edge currently left on the table and is the riskiest step, hence last.

---

## A/B walk-forward harness (cross-cutting)

| Change | Seam to parameterize | Diff on | Gate |
|---|---|---|---|
| #2 regime bias | new `PredictorFn` → `run_forecast_backtest` | MAE/RMSE/bias by cohort, Brier-skill, within2/3 | `evaluate_acceptance` + tail-regression gate |
| #1 θ-recal | calibrator factory `backtest.py:73` / config flag | Brier, log-loss, ECE, per-bucket gap, cohort skill | + `backtest_rescore` marginal P&L |
| #3 unc-Kelly | `StrategyConfig` → `run_rescore` | log-growth/day, day-clustered ROI CI | `compute_real_money_readiness` |

New code = one θ-fitting script + one regime-bias helper (ported from `exp_tail_lstm`). Everything else reuses existing eval logic. **All scoring on CLISFO truth.**

---

## Risk register

| Risk | Mitigation |
|---|---|
| Sparse warm/hot data → overfit regime/θ | Phase 0.5 gate; shrink-to-global; widen to continuous regime if counts low; rolling-origin only |
| θ over-shrinks / double-shrinks with reliability weight | Re-normalize after θ; re-tune `market_prior_weight`; per-horizon CIs |
| Uncertainty-Kelly drives size to ~0 | Floor the multiplier; A/B log-growth before shipping |
| Live-behavior drift | Every knob identity/off on live; flip only after readiness |
| Warm/hot re-enable repeats the −22.5% loss | Phase 4 fail-closed gates; research → tiny live paper → live, never skipping |
| Scoring against wrong truth | CLISFO only; never NWS-hourly max |

## Rollback

Each phase is a single profile-gated config flag (`theta_recalibration`→1.0, `regime_bias_*`→off, `cohort_kelly_multipliers`→identity). Reverting any change to live = set its flag back to identity; no data migration. The regime de-bias (Phase 1) is forecaster-side — revert the predictor and re-run the blend producer.

---

## File-by-file change map

| File | Phase | Change |
|---|---|---|
| `trading/sfo_kalshi_quant/config.py` | 0 | add `theta_recalibration`, `regime_bias_*`, `cohort_kelly_multipliers` (identity defaults) |
| `trading/sfo_kalshi_quant/backtest.py` | 0 | calibrator-factory seam (`:73`) |
| `forecaster/google_weather_cache.py` | 1 | regime-conditional rolling de-bias in the blend producer (~`:403`, `:1464/1788`) |
| `forecaster/forecast_backtest.py` | 1 | new regime `PredictorFn` as `candidate` arm |
| `trading/sfo_kalshi_quant/probability.py` | 2 | θ on market-implied at `:579-582` + re-normalize |
| `trading/sfo_kalshi_quant/consensus.py` | 2 | θ on consensus ladder at `:124` |
| *(new)* θ-fit script | 2 | logistic slope of market-implied vs CLISFO-settled YES, per horizon/cohort |
| `trading/sfo_kalshi_quant/risk.py` | 3 | side-agnostic uncertainty shrink at `:235→236` (+ YES mirror `:275-277`); optional regime down-weight `:254-271` |
| `trading/sfo_kalshi_quant/config.py` | 4 | relax `blocked_forecast_cohorts` (research first) once gates pass |

---

## Deferred (larger; gated behind the calibration fixes landing)

From research Part 2, worth doing *after* Phases 1–3 prove out: DRN station-embedding postprocessing (ppnn §2.3), tail-EVT `gbex` head (§2.5), NBM ingestion as a free benchmark/input (§2.2), and the tail-calibration training penalty (§2.4, with its skill trade-off).

---

## Phase 0 + 0.5 — Results & plan adjustment (2026-06-22)

Branch `feat/calibration-edge-phase0`. **Delivered:** a reusable readiness audit — `trading/sfo_kalshi_quant/data_readiness.py` (run `python -m sfo_kalshi_quant.data_readiness` from `trading/`; honors `SFO_KALSHI_DB` / `SFO_FORECASTER_ROOT` so it can point at prod). Core test gate green: **342 passed / 0 failed** (3 forecaster-feature test files need pandas/numpy/torch absent from the local 3.11 env; CI on 3.13 runs all 48). **No existing code changed.**

**Phase 0 plumbing — COMPLETED 2026-06-22 (correcting an earlier deferral).** The inert config fields + the calibrator-factory seam were initially deferred (YAGNI); per sticking to the plan they are now in place:
- `config.py` (after the comfort_edge block): `theta_recalibration=1.0`, `regime_bias_enabled=False` / `regime_bias_shrink_k=30.0` / `regime_bias_cap_f=1.5`, `cohort_kelly_multipliers=()` — all identity/no-op.
- `backtest.py`: `run_walk_forward_calibration_backtest(..., calibrator_factory=...)` defaulting to `ResidualCalibrator`.
- **Acceptance verified INERT:** calibration backtest bit-identical (n=262, Brier 0.5314, log-loss 1.1293), `forecast_backtest` bit-identical (17 days, MAE 2.564), `backtest-rescore` runs; stdlib suite **345/0** (added `tests/test_calibration_plumbing.py` guarding the inert defaults + seam transparency).

### Data verdict (LOCAL DBs, 2026-06-22) — the Kalshi-correct truth is the bottleneck

| Truth source | Total | warm | hot | Use |
|---|---|---|---|---|
| CLISFO settlements (Kalshi-correct) | 26 | 13 | 1 | settlement truth; since 2026-05-24 |
| Clean-blend scored outcomes (CLISFO) | 16 | 9 | 1 | calibration-backtest input |
| Trading days ∩ CLISFO | 11 | 8 | 1 | θ-fit / P&L validation |
| Approved-trade days ∩ CLISFO | 8 | 6 | 1 | P&L validation |
| LSTM outcomes (obs truth) | 442 | 76 | 9 | obs-truth dev only |
| Obs daily highs (`weather` table) | 3796 | 1009 | 952 | LSTM training; obs truth |

Per-change verdict:
- **[WAIT] CLISFO-truth calibration backtest** — 16 scored days vs `min_train=180`. Runs only on obs-truth LSTM outcomes today.
- **[PASS] Regime-bias FIT (Phase 1)** — fittable on the obs record (warm 1009 / hot 952); the LSTM trains on obs anyway.
- **[WAIT] Regime-bias VALIDATION on CLISFO** — warm 9 / hot 1.
- **[WAIT] θ market-recal FIT (Phase 2)** — warm 8 / hot 1 settled trading days. Fitting θ per horizon/cohort now = pure overfit. Hold θ=1.

### The redirect

1. **Highest-leverage near-term action: backfill CLISFO history** (NWS CLI archive / IEM Mesonet) + keep the daily scrape. Everything is bottlenecked on ~26 days of Kalshi-correct truth. This was flagged earlier as "pending live backfill"; it is now the critical path. ⚠️ obs ≠ CLISFO (documented wrong-truth divergence), so the abundant obs record **cannot** substitute for settlement validation.
2. **Phase 1 (regime bias) is codeable now** as a forecaster-side, obs-truth forecast-quality improvement (fit + validate on obs walk-forward via `forecast_backtest.py`). Its *Kalshi-settled* P&L impact stays "pending" until CLISFO accrues. This is the right next code step that does **not** depend on the truth bottleneck.
3. **Phase 2 (θ) is data-gated** — build the fitting harness, keep θ=1 identity, re-run `data_readiness` and unblock once warm/hot ≥ ~30 each per horizon (or immediately after a CLISFO backfill makes that true).
4. **Phase 3 (uncertainty-Kelly)** — theory-driven (Baker-McHale), conservative (shrinks size); adoptable with a sensible default, but P&L validation is data-limited (8–11 days) → refine as truth accrues.
5. Re-run the audit on **prod** DBs before finalizing (CLISFO may have accrued further there) — though scraping only began ~2026-05-24, so prod is also ~30 days; the backfill remains the unblock.

**Recommended next step:** either **(a)** start the CLISFO backfill (unblocks Phases 2–3 validation), or **(b)** start Phase 1 regime-bias on obs truth (codeable now, independent of the bottleneck). (b) makes progress regardless; (a) is what ultimately lets warm/hot be re-enabled with confidence.

### Update — CLISFO backfill EXECUTED (2026-06-22)

Chose (a). **Decisive feasibility proof:** NCEI GHCN-Daily TMAX for KSFO (station `USW00023234`, `units=standard`) matches the live CLISFO scrape **exactly on all 26 overlapping days** (diff distribution `{0: 26}`), across 63–91°F including the 91°F heat spike. GHCN-Daily = the archived official daily max the CLI report rounds; for a major-airport ASOS they are the same value.

**Tool:** `forecaster/backfill_clisfo_from_ghcn.py` (dry-run by default; `--apply` to write; idempotent; reversible). Safety: adds a nullable `source` column (the live scrape INSERT names its columns, so it is unaffected); GHCN rows tagged `source='ghcn_backfill'` and inserted `ON CONFLICT(local_date) DO NOTHING` so a real scrape is **never** overwritten.

**Applied to LOCAL `forecaster/weather.db`** (gitignored — not a tracked artifact): `clisfo_settlements` 26 → **4,168 rows** (+4,142 `ghcn_backfill`; the 26 `clisfo_scrape` rows verified unchanged). Kalshi-correct warm/hot truth: 14 → **1,530** (warm 1,265 / hot 265).

**Unblock confirmed** by re-running `data_readiness`:
- **[PASS] CLISFO-truth calibration backtest** — 442 LSTM forecast-days now pair with CLISFO truth (was 16; min_train 180).
- **[PASS] Regime-bias FIT** (obs warm 1009 / hot 952).
- **[~] Regime-bias VALIDATION on CLISFO** — warm **90** (clears ≥30), hot **18** (close; use shrinkage or wait ~12 days).
- **[WAIT] θ market-recal** — still gated on a **Kalshi market-history backfill** (we have only ~13 days of market snapshots; the realized-YES side is now available for all history, but historical Kalshi *prices* are not). This is the next domino for Phase 2.

**Still open:**
1. **Deploy the backfill to prod** — run `backfill_clisfo_from_ghcn.py --apply` against the prod `weather.db` (separate, explicit step; live scrape stays authoritative for recent dates).
2. **Consume the calibration unblock** — re-score the LSTM/blend forecasts against the deepened `clisfo_settlements` (small Phase 1 wiring) so the calibration backtest actually runs on CLISFO truth.
3. **Kalshi market-history backfill** — pull historical KXHIGHTSFO candlesticks (Kalshi API) to unblock θ; bounded by series age (~since early 2026).

### Update — Kalshi market-history backfill EXECUTED (2026-06-22)

**Feasibility proven:** the existing `KalshiPublicClient` hits the `historical/...` endpoints **unauthenticated**. `historical/markets` returns **588 finalized KXHIGHTSFO markets across 98 event-days (2026-01-14 → 04-21)**, and `historical/markets/{ticker}/candlesticks` returns hourly **yes_bid/yes_ask OHLC** — exactly the market-implied price series θ needs.

**Executed** via the existing CLI (no new code): `python -m sfo_kalshi_quant.cli dataset-backfill --source kalshi-history --start-date 2026-01-01 --end-date 2026-06-22 --kalshi-candles` → **588 market rows + 23,760 candle rows** into LOCAL `trading/data/paper_trading.db` (gitignored).

**Coverage verified:** 98 event-days, all joinable to CLISFO truth; median **41 candles/market**; **100% of markets have a day-ahead (T-18..30h) candle** → the day-ahead horizon θ targets is fully covered. θ-fit sample (candle ∩ CLISFO): **warm 22 / hot 9** (was 8/1 — ~10×). Still under ≥30/cohort individually, so θ is *partially* unblocked: an aggregate short-horizon θ and a warm-cohort θ (with shrinkage) are fittable; a confident **hot** θ is still thin.

**Limitation:** the historical archive **lags ~2 months** — recent summer warm/hot days (Jun 10 = 79°F, Jun 11 = **91°F**) return 404 (not yet archived). They mature over ~2 months; the regular (non-historical) candlestick endpoint could pull them sooner (small client addition, untested).

**Operational gap:** the daily `kalshi-history` collector runs **without** `--kalshi-candles` (markets only), so candle history does NOT accrue automatically. To grow warm/hot coverage: add `--kalshi-candles` to the daily job, or schedule a monthly `dataset-backfill --kalshi-candles` sweep.

The readiness gate (`data_readiness.py`) was updated to report the θ sample from candle coverage (warm 22 / hot 9), replacing the 13-day live-paper proxy.

### Consolidated status & next steps (end of 2026-06-22 session)

**Data foundation is now in place** (locally): Kalshi-correct truth 26 → 4,168 days; calibration backtest unblocked (442 pairs); regime-bias validatable on CLISFO (warm 90); θ sample warm 22 / hot 9.

Open, in rough priority:
1. **Phase 1 regime-bias** — highest-leverage forecast lever; now CLISFO-validatable. Includes the small re-score wiring so the calibration backtest consumes CLISFO truth.
2. **Phase 2 θ-fit harness** — fit per-horizon logistic recalibration slope of market-implied prob vs CLISFO-settled YES over the candle history; hold θ=1 live until warm/hot ≥ 30/cohort.
3. **Grow warm/hot coverage** — enable candles in the daily collector (or monthly sweep); optionally add the regular candlestick endpoint for recent months.
4. **Deploy both backfills to prod** (`weather.db` + `paper_trading.db`) and **commit** the Phase 0 artifacts (readiness gate, CLISFO backfill tool, docs).

### Phase 1 progress — CLISFO-truth re-score shipped (2026-06-22)

The regime de-bias *model change* is gated on the torch/ML env (442-day LSTM) or blend accrual (17 days → ~early July), so the runnable Phase 1 increment was the **re-score**: score the model's calibration on the truth Kalshi settles on.

**Shipped (TDD, stdlib, branch `feat/calibration-edge-phase0`):**
- `SfoForecasterAdapter.load_lstm_outcomes(clisfo_truth=True)` + `_settlement_truth_map()` — re-scores the 442-day LSTM A/B predictions against CLISFO settlement truth (CLISFO integer preferred, floored-NWS fallback; CLISFO-driven so it spans the full backfill, unlike the NWS-driven `load_ksfo_daily_highs`).
- CLI: `backtest-calibration --truth {stored,clisfo}`.
- Test-first: `test_load_lstm_outcomes_rescores_against_clisfo_truth` (RED→GREEN); full stdlib suite **343 passed / 0 failed**.

**CLISFO-truth calibration baseline** (`backtest-calibration --source lstm --truth clisfo --min-train 180`, n=262):

| Cohort | n | Brier | top-hit | obs-truth top-hit (was) |
|---|---|---|---|---|
| cold | 43 | 0.013 | 100% | 100% |
| normal | 133 | 0.510 | 60% | 63% |
| **warm 70–79** | 70 | **0.876** | **20%** | 12% |
| hot 80+ | 16 | 0.661 | **62%** | 12% |

**Finding that reshapes the thesis:** on the correct (CLISFO) truth, the **hot** cohort is far less broken than obs-truth diagnostics implied (top-hit 12%→62%) — it was largely a wrong-truth artifact. The genuine anti-calibration target is **warm 70–79** (top-hit 20%, Brier 0.876). The regime de-bias and θ work should prioritize the warm cohort; hot is thinner-but-healthier than believed. This is the baseline any de-bias must beat.

### Phase 1 VERDICT — regime de-bias REFUTED (adversarially verified, 2026-06-22)

The plan's Phase 1 hypothesis (a synoptic-regime de-bias fixes the warm anti-calibration) was tested on the 442-day LSTM (CLISFO truth, rolling-origin, leakage-safe prior-day regime) via `forecaster/regime_debias_backtest.py`, then adversarially verified by 4 independent auditors (leakage, correctness, statistical, completeness) — **all 4 confirm the negative result at HIGH confidence.**

**Result: no post-hoc residual de-bias fixes warm.** Every variant — production's predicted-temp-cohort, prior-day **offshore-flow** regime, and the stronger prior-day **inland-heat (Concord/KCCR)** regime — pulls warm MAE 4.40→~3.8 but degrades **cold** 2.21→~2.9, for a **net wash** (overall delta +0.04°F, p=0.39; a statistical tie, not a win). A 81-cell sweep (strategy × window × shrink × cap), continuous-OLS corrections, Concord−SFO gradient, combined regimes, and persistence — **none** produced a statistically significant net gain or got warm < 3.7°F without pushing cold above raw.

**Root cause (oracle-bounded):** the warm under-prediction is **large (mean −4.05°F) AND noisy (stdev 3.95°F)**. A post-hoc additive shift can move the mean but cannot cut the scatter — an *oracle* (truth-peeking) correction only reaches warm MAE 3.224. And warm days are under-predicted into the **"normal" predicted bin** ("disguised-warm"), so no leakage-safe signal isolates them (inland_hot regime is still only 63% warm; offshore only 8%).

**Implications / redirect (still inside Phase 1's intent — fixing warm):**
1. **Do NOT ship a forecaster-side regime/residual de-bias** — it cannot fix warm and risks net-degrading via cold whipsaw. (It also suggests production's existing temp-cohort blend de-bias may be a net wash on the LSTM; its value on the 17-day blend is unverified — separate item.)
2. **The warm fix must be in the LSTM itself** — reduce the warm under-prediction's *variance*, not shift its mean. This is the `exp_inland_lstm` / `exp_marine_lstm` / `exp_tail_lstm` work (torch env); inland features already gave hot MAE −2.3°F in prior work, which is the right lever (better warm representation, not a post-hoc patch).
3. **On the trading side, lean on the market, not the model, for warm** — Phase 2 (θ-recalibration) exploits the *overconfident market* on warm days; keep the warm/hot block until the LSTM's warm variance improves.

**Artifact:** `forecaster/regime_debias_backtest.py` (reusable stdlib validation harness: regime/temp/global de-bias comparison, CLISFO-scored, leakage-safe).

### Phase 2 step 1 — θ fit on OUR data (2026-06-22)

Fit the recalibration slope θ on our own KXHIGHTSFO history (98 event-days, 1,176 de-vigged candle-prob vs CLISFO-YES pairs) via `trading/sfo_kalshi_quant/theta_fit.py` — a 2-param logistic `P(YES)=σ(α+θ·logit p)` by horizon and cohort, cluster-bootstrapped over event-days. **Do NOT use the preprint's numbers.**

| Slice | θ | 95% CI (cluster-bootstrap) | reading |
|---|---|---|---|
| **≤24h day-ahead** | 0.89 | **[0.73, 1.11] — spans 1** | over-confident direction, *not robust alone* |
| 24–48h | 1.41 | — | under-confident (flips beyond ~2 days) |
| **day-ahead warm 70–79** | **0.49** | **[0.25, 0.84] — robustly < 1** | **strongly over-confident — the real edge** |
| day-ahead cold / hot | 1.18 / 2.11 | — | under-confident |
| Overall (all) | 1.05 | — | ~calibrated (washes out) |

**Findings:**
- The research replicates on our data (short-horizon over-confidence, negative intercept α=−0.12, flips to under-confidence beyond ~2 days) — **but only per-cohort**; the aggregate washes out (θ≈1), which is why a single global θ is wrong.
- The **statistically-robust, exploitable signal is the warm regime** (θ=0.49, CI entirely < 1). This is the perfect complement to Phase 1: on warm days the *model* can't be trusted (Phase 1) AND the *market* is most over-confident — so the warm edge is **fading the over-extreme market toward climatology**, not trusting either forecast.
- Sanity verified: de-vig sums to 1.000, exactly 1 YES/event-day (mutually-exclusive ladder), candle is pre-settlement (leakage-safe). The settled-cohort fit is diagnostic; the definitive test is the downstream walk-forward P&L.

**Implication for the wiring step (Phase 2 step 2):** do **not** apply a single scalar θ. Key θ on **horizon + a trade-time regime proxy** — the settled cohort is unknown at trade time, but the **market-implied high** (consensus `implied_high_f`) *is* known, so re-fit θ by market-implied-high cohort (actionable) and apply the warm fade (θ≈0.5) when the market implies warm. Then wire into `probability.py:579-582` + `consensus.py:124` gated by the `theta_recalibration` field (added in Phase 0), identity on live / active in research, and validate via `run_walk_forward_calibration_backtest` (ECE/Brier) + `backtest_rescore` (P&L).

**Artifact:** `trading/sfo_kalshi_quant/theta_fit.py` (reusable stdlib θ-fitter: per-horizon/cohort logistic recalibration slope, CLISFO-scored, cluster-bootstrap CIs).

### Phase 2 VERDICT — θ-recalibration REJECTED by out-of-sample validation (2026-06-22)

Phase 2 step 2 made the θ **actionable** (cohort by the *market-implied* high, trade-time-known — the settled cohort isn't) and validated it **out-of-sample** (leakage-safe walk-forward: fit θ on prior event-days, apply forward, score recalibrated vs raw market prob). Result: **don't wire it.**

- **The warm edge evaporates when made actionable.** By *market-implied* cohort: warm **θ=1.00** (calibrated), cold 0.87, normal 0.84. The settled-warm θ=0.49 (step 1) was real but keyed on the *outcome* — the settled-warm days the market mis-priced are days the market *implied normal* (it missed them, like the model), so there's no tradeable warm signal.
- **No θ variant helps OOS.** Walk-forward Brier Δ (recal−raw): θ-only +0.0010, α+θ +0.0004, fixed θ=0.85 −0.0004 (log-loss worse), fixed θ=0.49 +0.0064 — **all CIs span zero**, recal improves <half of days. The day-ahead market is ~calibrated conditioned on tradeable info; fading it is neutral-to-harmful.

**Decision:** do **not** apply θ in `probability.py` / `consensus.py`. Keep `theta_recalibration=1.0` (identity). The Phase 0 scaffolding stays inert; revisit only if a much larger sample (esp. summer warm market history) later shows a robust, *actionable* over-confidence.

### Cross-phase conclusion (Phases 1 + 2)

Both "calibration-edge" interventions from the research — forecaster regime de-bias (Phase 1) and market θ-recalibration (Phase 2) — are **rejected by rigorous out-of-sample / leakage-safe validation on our own data.** The research's phenomena (regime-dependent bias; short-horizon market over-confidence) are *real* but **not exploitable** at the actionable horizon/key. This is the value of validate-before-ship: it stopped two non-working changes. The remaining real levers:
1. **Phase 3 — uncertainty-scaled Kelly:** sizing discipline that needs no calibration edge (sizes existing edges better / shrinks where uncertain). Still worth doing.
2. **Improve the LSTM warm *variance*** (the `exp_*_lstm` work, torch env) — the only path shown to actually move warm accuracy.
3. **Keep the existing warm/hot block** — the data supports it (neither forecast nor market is exploitable warm).

### Phase 3 — uncertainty-scaled Kelly: BUILT (inert; empirical validation pending data) — 2026-06-22

The one plan move that survived (risk *discipline*, needs no calibration edge). **Built, tested, inert.**

- **What:** extracted the YES-only Baker-McHale variance shrink into a side-agnostic `_confidence_shrink` (`risk.py`) and applied it to the main Kelly for **both** sides, gated by `uncertainty_kelly_enabled` (default off). Refactored `_yes_sizing_factor` to reuse it (DRY, bit-identical: `= shrink × payout`).
- **Behavior (demonstrated):** identity (×1.0) for cheap longshots *and* when p == p_lcb; strong shrink for **expensive favorites with an honest lower-bound gap** (cost 0.85 → ×0.04, 0.90 → ×0.001). That is exactly the NO-favorite over-sizing regime — it gives NO the estimation-error discipline YES already had. Complements `min_probability_uncertainty` (which clamps the degenerate p == p_lcb == 1.0 case).
- **Tests:** +6 (`tests/test_uncertainty_kelly.py`); full suite **351/0**; off = identity (no live-behavior change).
- **NOT enabled live.** The plan's empirical acceptance (rescore log-growth *not worse* + drawdown *reduced*) is **data-blocked** — `backtest_rescore` settles ~0 of the 13 recorded decision-days. Baker-McHale (peer-reviewed, verified) supports it as strictly risk-reducing, but per discipline it stays OFF until the rescore A/B confirms on adequate settled data. **Enable research-first then.**
- **Deferred:** `cohort_kelly_multipliers` (the Phase 0 field) — low value while warm/hot is blocked (the miscalibrated cohorts aren't traded); revisit with any warm/hot re-enable.
- **Artifact:** `risk.py:_confidence_shrink`, config `uncertainty_kelly_enabled`.

### Final plan status (2026-06-22)

| Phase | Outcome |
|---|---|
| 0 — Plumbing | ✅ Shipped, inert |
| 1 — Regime de-bias | ❌ Built + **rejected** (OOS; oracle ceiling) |
| 2 — θ-recalibration | ❌ Built + **rejected** (OOS; outcome-conditioned) |
| 3 — Uncertainty-Kelly | ✅ Built, inert; **enable after rescore validates** on more data |
| 4 — Warm/hot re-enable | ⛔ Moot — no warm edge exists; keep the block |

**Net:** the calibration-*edge* thesis didn't hold on our data (Phases 1–2 rejected). The durable wins are the **CLISFO truth backfill** (fixed the wrong-truth diagnostics; deploy to prod), the **reusable harnesses** (ready for a summer re-test), and **Phase 3's risk discipline** (ready to enable once validatable). The real warm lever remains the **LSTM model** (variance reduction, torch env).
