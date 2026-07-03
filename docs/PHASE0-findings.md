# Phase 0 — Measurement Baseline (findings, 2026-07-02)

Zero behavior change. Two new measurement tools, both tested, run against real data.
These numbers drive the priorities for Phase 1a / 2a / 2b.

## Tooling added
- `trading/sfo_kalshi_quant/clv.py` (+ `trading/tests/test_clv.py`, 9 tests) — closing-line
  value + exit-drag over the paper journal. Read-only.
- `forecaster/forecast_postproc_backtest.py` — added `brier_by_cohort()` + printed table
  (+ 1 test). The block's own metric (cohort Brier) is now visible per predictor.

Dev env: `.venv-dev` on the Mac (numpy/pandas/scipy/scikit-learn/pytest). Run trading tests
with `ulimit -n 4096`. weather.db pulled from Lightsail to `/tmp/weather.db` (177 MB).

## Trading finding — the loss is mostly ENTRIES, not just exits
CLV report over 72 orders (Jun 10–26), `python -m sfo_kalshi_quant.clv`:

| bucket | orders | CLV total | realized PnL | exit drag |
|---|---|---|---|---|
| overall | 72 (71 CLV-covered) | **−40.1** | −39.8 | −17.1 (18 closed w/ known high) |
| PAPER_CLOSED | 57 | −44.6 | −44.8 | **−17.1** |
| PAPER_SETTLED | 14 | **+4.5** | +5.0 | — |
| profile: research | 65 | **−38.5** | −38.3 | −19.8 |
| profile: live | 7 | −1.6 | −1.5 | **+2.7** |
| cohort: warm_70_79f | 26 | **−14.8** | −15.1 | **−17.1** |
| cohort: cool_le_69f | 6 | +1.3 | +1.4 | −0.0 |

Reading:
- **Negative CLV (−40)** = the market moved *away* from our positions after entry. This is an
  entry-quality problem; exits (−17 drag) explain only ~38% of the closed-order loss.
- **We held the winners and closed the losers** (settled +4.5 vs closed −44.6) — backwards.
- **The loose `research` profile is the leak** (−38.5); the strict `live` profile is ~flat and
  its exits actually *helped* (+2.7). Confirms: loosening gates without calibration bleeds.
- **The warm cohort is the epicenter** — worst CLV *and* worst exit drag — and it is exactly the
  cohort the live profile hard-blocks.

Coverage caveat: only 7/17 target dates have an authoritative CLI settlement high, so exit-drag
covers 18 orders and 39 orders fall in an "unknown" cohort. CLV itself (entry vs closing mark)
covers 71/72 and needs no settlement, so the headline is robust. **Follow-up:** backfill the 10
missing June CLI highs from the IEM archive to sharpen the cohort/exit-drag split (also benefits
1a/2a). The obs-max in `dataset_station_observations` runs ~3°F below CLI, so it is NOT used.

## Forecast finding — EMOS already beats the blend; warm/hot calibration has headroom
`forecast_postproc_backtest.py --db /tmp/weather.db` (CLISFO truth 4181 days; NWP archive 915).

CRPS by settled cohort (lead 1) — lower is better:

| predictor | cold | normal | warm | hot |
|---|---|---|---|---|
| baseline_blend | nan | 0.90 | 1.70 | **10.24** |
| climatology | 2.94 | 2.46 | 2.65 | 10.13 |
| emos_wmean | 1.75 | 1.18 | **1.51** | **2.17** |
| emos_ngr | 1.86 | 1.20 | 1.62 | 2.37 |

Brier by settled cohort (lead 1; shared sigma; the block's own metric) — lower is better:

| predictor | cold | normal | warm | hot |
|---|---|---|---|---|
| baseline_blend | nan | 0.818 | 0.963 | 1.185 |
| climatology | 1.068 | 1.030 | 0.963 | 1.185 |
| emos_wmean | 0.926 | 0.870 | **0.905** | **1.014** |
| emos_ngr | 0.926 | 0.874 | 0.914 | 1.039 |

Head-to-head CRPS gate vs baseline_blend (lead 1): emos_ngr, emos_wmean, analog_ens,
emos_anen_blend all **WIN** (DM<0, ci_high<0). Lead 2 holds the same ordering (emos_wmean 1.55).

Reading:
- The config blocks warm/hot citing "forecaster anti-calibrated, cohort Brier ~0.96" — that is the
  **blend's** number (warm 0.963 / hot 1.185). **EMOS already improves both** (warm 0.905, hot 1.014).
- But EMOS is still only ~at the no-skill line on hot (~1.01) and modestly better on warm — **residual
  miscalibration remains**, so a recalibration layer (Phase 1a) has real headroom.
- The `research` profile already trades warm/hot *with* EMOS enabled and still lost (CLV −38.5),
  which is the evidence that **EMOS-alone is not enough to safely unblock** — Phase 1a must land
  before Phase 2a. (Research losses conflate calibration + loose gates + exit drag, so the cleaner
  signal is the cohort Brier above.)

## Implication for the plan (unchanged ordering, sharper targets)
1. **Phase 1a (isotonic recalibration)** — target warm/hot bin probabilities; the residual Brier gap
   (hot ~1.01) is the headroom. A/B it on the `research` profile, which already trades those cohorts.
2. **Phase 2a (auto-unblock)** — gate on the *recalibrated* per-cohort Brier (now measurable) showing
   walk-forward skill, then lift the live-profile block.
3. **Phase 2b (posterior-mean Kelly)** — the research profile's −38.5 CLV is exactly what a
   record-shrunk stake would have sized down; sizing off the realized cohort win-rate is the fix.
4. Exits are a real but secondary lever (−17 drag): the bigger loss is entry quality / calibration.

---

# Phase 1a — Per-cohort recalibration: EVALUATED, NEGATIVE RESULT (do not ship)

Built a per-cohort Gaussian recalibration (bias + dispersion scale, shrunk toward
identity), added a rolling-origin (leakage-safe) `emos_wmean_recal` predictor to the
backtest, and tested it head-to-head vs `emos_wmean` on the real archive.

Result (lead 1, reference=emos_wmean, 836 days):

| predictor | CRPS | Brier* | warm CRPS | hot CRPS | warm Brier* | hot Brier* |
|---|---|---|---|---|---|---|
| emos_wmean | 1.414 | 0.883 | 1.506 | 2.171 | 0.895 | 0.958 |
| emos_wmean_recal | 1.411 | 0.882 | 1.509 | 2.214 | 0.894 | 0.963 |

Head-to-head CRPS gate: **TIE** (mean_delta −0.0028, DM −0.38, p=0.70, CI [−0.017,+0.011]).
Per-cohort it is identical on warm and slightly WORSE on hot.

**Conclusion:** `emos_wmean` (inv-var-weighted EMOS, rolling-origin) is already about as
calibrated as a Gaussian post-processor gets on this archive. The residual warm/hot Brier
is NOT a fixable bias/dispersion error — it is genuine hot-day difficulty (hot CRPS ~2.2 vs
normal ~1.2 is real higher variance, not miscalibration) plus non-Gaussian shape that the
few hot days cant fit safely. Parametric recalibration does not earn its place. Retained

---

# Phase 1a — Per-cohort recalibration: EVALUATED, NEGATIVE RESULT (do not ship)

Built a per-cohort Gaussian recalibration (bias + dispersion scale, shrunk toward
identity), added a rolling-origin (leakage-safe) `emos_wmean_recal` predictor to the
backtest, and tested it head-to-head vs `emos_wmean` on the real archive.

Result (lead 1, reference=emos_wmean, 836 days):

| predictor | CRPS | Brier* | warm CRPS | hot CRPS | warm Brier* | hot Brier* |
|---|---|---|---|---|---|---|
| emos_wmean | 1.414 | 0.883 | 1.506 | 2.171 | 0.895 | 0.958 |
| emos_wmean_recal | 1.411 | 0.882 | 1.509 | 2.214 | 0.894 | 0.963 |

Head-to-head CRPS gate: **TIE** (mean_delta -0.0028, DM -0.38, p=0.70, CI [-0.017,+0.011]).
Per-cohort it is identical on warm and slightly WORSE on hot.

Note: the shared-sigma Brier is computed under the *reference* arm's cohort sigmas, so its
absolute value shifts with `--baseline` (hot Brier reads 1.014 under baseline_blend, 0.958
under emos_wmean). It is a relative calibration comparison, not a fixed target.

**Conclusion:** `emos_wmean` (inv-var-weighted EMOS, rolling-origin) is already about as
calibrated as a Gaussian post-processor gets on this archive. The residual warm/hot Brier is
NOT a fixable bias/dispersion error -- it is genuine hot-day difficulty (hot CRPS ~2.2 vs
normal ~1.2 is real higher variance, not miscalibration) plus non-Gaussian shape the few hot
days cannot fit safely. Parametric recalibration does not earn its place. Retained
`recalibration.py` + the backtest predictor as documented experimentation infra (and for a
future isotonic-CDF attempt once the archive is larger), NOT wired into the live engine.

**Key pivot -- the warm block looks stale, and the real lever is trading, not forecasting:**
- Under emos_wmean's own sigma, WARM Brier (0.895) is comparable to cold (0.903) / normal
  (0.862) -- warm is NOT specially miscalibrated. The block dates from the blend era (blend
  warm Brier 0.926-0.963) and/or pre-emos-true-lead. Warm is likely unblockable NOW with
  emos_wmean + proper sizing (Phase 2a), no recalibration needed.
- HOT stays genuinely hard (CRPS 2.2) -- gate it by SIZE (its wider sigma already shrinks
  Kelly) rather than a hard block.
- The live trader already serves emos_wmean (DEFAULT_WEIGHT_MODE=inv_var) -- confirmed.
- The research-profile CLV loss (-38.5) was sizing/gate/exit, NOT a fixable forecast error.

**Revised priority order (evidence-based):**
1. Phase 2b -- posterior-mean Kelly from the journal (size off realized cohort win-rate). HIGHEST.
2. Phase 2a -- test unblocking WARM (not hot) via the readiness gate on emos_wmean calibration.
3. Phase 1b -- regime confidence flag for gating/sizing (hot-day size throttle).
4. Phase 2c/2d, Phase 3. Phase 1a recalibration: parked (validated not-useful).

---

# Phase 2b — Posterior-mean Kelly: BUILT, tested, default-OFF (ready to enable)

New `trading/sfo_kalshi_quant/posterior_kelly.py`: per-cohort *trust* multiplier on the base
fractional-Kelly, learned from the settled journal. Beta prior centered on breakeven, so a
short record shrinks size toward a floor and it grows only as a real edge is demonstrated
(Baker & McHale 2013; Chu, Wu & Swartz 2018). Wired into `risk.py` (constructor-injected,
applied at the `kelly *= fractional_kelly` point) and `cli.py` (built from the journal at the
3 paper sites). Config: `posterior_mean_kelly_enabled` (default False), `_prior_strength` (20),
`_floor` (0.2), `_min_cohort_n` (8). 25 new tests; full suite 461 pass (1 pre-existing dash fail).

Strict no-op when disabled or no model injected -- bit-identical to the frozen baseline.

Size multipliers from the CURRENT journal (what enabling would apply):

| scope | n | multiplier |
|---|---|---|
| overall | 14 | 0.89 |
| warm_70_79f | 8 | 0.71 |
| normal_60_69f | 5 (->pooled) | 0.89 |
| hot_80f_plus | 1 (->pooled) | 0.89 |

**Known limitation (important):** the model reads `settled_at IS NOT NULL`, so it currently
sees only the 14 HELD winners (14/14) -- NOT the 57 early-CLOSED losers that caused the -44.8
bleed. So as-built it applies only a mild 0.71-0.89x haircut. The real fix is to score the
closed orders by their would-have-settled outcome (reuse the Phase 0 `clv.py` counterfactual),
which needs authoritative settlement highs (7/17 dates now; IEM backfill would extend it).
That refinement makes the model reflect the ACTUAL record, not just the held winners.

**Enablement (needs a decision -- real-money-intent behavior change):**
- Recommended: enable on the `research` profile first (paper-only data collector, floor 0.2)
  so it self-limits losing cohorts while filling the journal; keep `live`/`balanced` OFF until
  a walk-forward validates (blocked on more settled trades).
- Do NOT enable live without the counterfactual refinement + a walk-forward -- with only 14
  settled trades the record is too thin to size real-money-intent stakes off.
