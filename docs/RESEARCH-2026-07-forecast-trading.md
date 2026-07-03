# WeatherEdge Research Report — Forecast Accuracy & Trading Engine (2026-07-01)

Deep-research sweep (5 angles, 22 sources fetched, 105 claims extracted, 25 adversarially
verified with 3-vote panels: 24 confirmed, 1 refuted) mapped to the two project goals:

1. **Goal A** — increase accuracy of today/next-day KSFO daily-high forecasts.
2. **Goal B** — make the Kalshi engine win more often AND trade more often.

Grounding facts from the current system (measured 2026-07-01):

- XGBoost next-24h temp MAE 2.47°F vs persistence 2.62°F (`forecaster/model_compare_results.json`) — ML adds ~6% over persistence.
- Paper journal (Jun 10–26, 17 target dates, 72 orders): 14 held-to-settlement orders **+$5.03**, 57 early-closed orders **−$44.80**. Exits, not entries, are the current PnL sink.
- Risk profiles: live `min_edge` 0.02 / fractional Kelly 0.30 / `kelly_lcb_weight` 0.6; research `min_edge` 0.005 / Kelly 0.12.

---

## Goal A — Forecast accuracy

### A1. Add the inputs NWS professionals use for this exact station (highest priority)

The NWS runs a **dedicated operational forecast product for SFO marine stratus**
(the MIT Lincoln Laboratory Marine Stratus Forecast System, operational since 2004,
now NWS-run at the Oakland CWSU). Its live discussion explicitly relies on:

- **National Blend of Models (NBM)** — not in our blend today
- **Ensemble guidance**
- **GOES-West visible imagery**
- **Real-time METAR/ASOS observations**

Verified 3-0 against MIT LL report ATC-319, peer-reviewed BAMS 2012 (Clark et al.),
and the live weather.gov/zoa/stratus product (fetched 2026-07-01).

**Actions:**
- Add NBM point guidance for KSFO as a blend member. Backtest it first against the
  existing SQLite CLI archive using the Iowa Environmental Mesonet NBM archive
  (open question — no public station-level KSFO NBM-vs-blend numbers exist).
- Add GOES-West low-cloud imagery + morning METAR ceiling/visibility as **same-day
  regime features** (stratus present / clearing / clear).

Sources: https://www.ll.mit.edu/r-d/publications/sfo-marine-stratus-forecast-system-documentation ·
https://journals.ametsoc.org/view/journals/bams/93/10/bams-d-11-00038.1.xml ·
https://www.weather.gov/zoa/stratus

### A2. Copy the MSFS blend architecture: regime-stratified weights + confidence gate

The 20-year production MSFS is a consensus of **four statistically independent models**
(1 boundary-layer physics model, COBEL, + 3 statistical regressions on field sensors,
regional obs, GOES-West), combined as a weighted average where weights come from
**each component's historical performance stratified by day type and initialization
hour**, gated by a **rule-based confidence indicator** (verified 3-0).

WeatherEdge's blend currently uses global weights. The professional template says:

- Stratify blend weights by **stratus regime** (e.g., clear day / early burn-off /
  late burn-off / no-clear) and by **forecast issue hour**.
- Emit a **confidence flag** from simple rules (component disagreement, regime
  ambiguity). This flag is dual-use: it also gates trade size (Goal B).

Caveat: MSFS predicts stratus **clearing time** for aviation, not Tmax; the Tmax
relevance is a physically sound inference corroborated by Iacobellis & Cayan 2013
(±20% daily coastal cloudiness ≈ 1°C surface temperature change). SFO stratus is a
warm-season (May–Oct) daily cycle: forms overnight, clears midmorning–early
afternoon under a ~1,500 ft marine layer, ~50–60 operationally impactful days/year —
so this regime machinery matters most exactly in the current trading season.

### A3. Post-processing upgrade path: boosted EMOS / hybrid analog-EMOS now, DRN later

Verified literature ladder for station-level T2m (CRPS, Rasp & Lerch 2018, MWR,
537 German stations, 48-h lead):

| Method | CRPS |
|---|---|
| Raw ECMWF ensemble | 1.16 |
| Local EMOS | 0.90 |
| Quantile regression forest | 0.95 |
| **Boosted local EMOS** | **0.85** |
| **DRN (NN + auxiliary predictors + station embeddings)** | **0.82** |

- DRN is ~29% better than raw ensemble and ~3.5% better than the best EMOS — but
  Rasp & Lerch explicitly note benchmark methods (boosted EMOS, QRF) **tend to beat
  neural nets at coastal stations with short training records** — which is KSFO with
  a months-scale archive exactly. **DRN is the when-the-archive-grows move (≥1–2 yr),
  not the now move.**
- **Now move:** add auxiliary predictors to EMOS via gradient boosting (boosted EMOS),
  and/or a **hybrid analog-EMOS** (~11% CRPS gain over plain EMOS in post-2013
  literature). An analog ensemble alone needs only 12–15 months of archive
  (Delle Monache et al. 2013, MWR) but for common temperature events it *matches*,
  not beats, well-tuned EMOS — useful as a cheap second opinion.
- Refuted in verification (1-2): "basic postprocessing captures most of the available
  gain before ML adds more" — do **not** assume the calibration layer is near ceiling.

Sources: https://journals.ametsoc.org/view/journals/mwre/146/11/mwr-d-18-0187.1.xml ·
https://journals.ametsoc.org/view/journals/mwre/150/1/MWR-D-21-0150.1.xml (wind gusts, method-ranking corroboration) ·
https://journals.ametsoc.org/view/journals/mwre/141/10/mwr-d-12-00281.1.xml

### A4. Candidate providers to add: Foreca, Microsoft (medium confidence)

ForecastAdvisor (independent verification site, fetched 2026-07-01) ranks exactly
**Google, Foreca, Microsoft** as the "Superior" tier for zip 94128 / station 360
(KSFO area). Google is already in the blend; Foreca and Microsoft are concrete
candidates. Medium confidence: single source, point-in-time, tier blends four metrics
of which high-temp accuracy is only one. NWS/AccuWeather/TWC do **not** make the
top tier there.

Source: https://www.forecastadvisor.com/detail/California/SanFrancisco/94128/

### A5. Make the target variable settlement-exact

Kalshi settles **exclusively** on the NWS CLI Daily Climate Report (CFTC-filed NHIGH
terms: no fallback source; METAR only as a consistency check that can delay
determination to 11 AM ET). The climate day is defined in **local standard time**, so
during PDT the qualifying window is **1:00 AM – 12:59 AM the next day**. Forecast
verification and training labels should target the CLI value under that window (SFO
highs are almost always afternoon, so the shift rarely bites — but "rarely" is where
bin-edge losses live). Settlement fixes at the CLI value documented at expiration;
post-expiration corrections are ignored.

Sources: https://help.kalshi.com/en/articles/13823837-weather-markets ·
https://kalshi-public-docs.s3.amazonaws.com/contract_terms/NHIGH.pdf

---

## Goal B — Win more, trade more often

### B1. Replace ad-hoc fractional Kelly with posterior-mean (shrunken) Kelly

Verified theory chain (all 3-0):

- Probability-estimation error measurably erodes Kelly returns; shrunken Kelly beats
  raw Kelly in simulation and on real betting data (Baker & McHale 2013, Decision
  Analysis; Metel 2018).
- With a Beta(a,b) prior and the Binomial win record from the paper journal, the
  **Bayes-optimal fraction under log-growth loss is exactly Kelly evaluated at the
  posterior mean** p̂ = (x+a)/(n+a+b) (Chu, Wu & Swartz 2018, JQAS). Worked example:
  Beta(50,50) prior → f = 0.048 vs raw Kelly 0.089 (≈ half-Kelly, giving the heuristic
  a decision-theoretic basis); in simulations, modified Kelly profitable in 65% of
  runs vs 53% raw.

**Why this serves "trade more often":** stakes are small while the record is short and
**automatically grow as calibration is demonstrated** — the gate loosens itself with
evidence instead of a hand-tuned `fractional_kelly = 0.15/0.30/0.12` constant.
Corollary (Baker & McHale): more trades must come from **better-calibrated
probabilities**, not from removing shrinkage.

Sources: https://pubsonline.informs.org/doi/abs/10.1287/deca.2013.0271 ·
https://www.sfu.ca/~tswartz/papers/kelly.pdf · https://arxiv.org/abs/1701.02814

### B2. Joint Kelly across all mutually exclusive bins (not per-bin sizing)

For mutually exclusive integer-°F bins, the growth-optimal allocation must be solved
**jointly across all bins simultaneously** — positions on different bins hedge each
other across states of the world (Whelan 2025, Bulletin of Economic Research;
closed form in Smoczynski & Tomkins 2010; standard since Kelly 1956 / Cover & Thomas
ch. 6). This is the mathematically correct route to **more bins per market and larger
aggregate exposure without increasing ruin risk**. Qualification: joint Kelly extracts
more from existing edge; it does not create edge.

Source: https://www.karlwhelan.com/sports-betting-kelly-criterion-multiple-outcomes/

### B3. Fix exits before loosening entries (internal evidence, not literature)

The journal says entries are roughly fine and exits are the sink: settled orders
+$5.03, early-closed orders −$44.80. No public Kalshi-microstructure claims survived
verification, so this is an internal empirical lever: audit `exits.py` closure reasons,
consider hold-to-settlement as the default with exits only on regime invalidation
(e.g., observed-high lock makes the bin impossible), and measure a closing-line-value
analog (entry price vs final pre-settlement price) per trade to separate bad exits
from bad entries.

### B4. Nightly recalibration loop (general theory; area-5 claims mostly didn't survive)

The verified guiding principle: **maximize sharpness subject to calibration**
(Gneiting et al. 2007). Practical, tuning-free tooling that appeared in fetch but was
budget-dropped before final verification (treat as standard-but-unverified-here):
CORP reliability diagrams / isotonic recalibration via pool-adjacent-violators
(Dimitriadis, Gneiting & Jordan, PNAS 2021), PIT-density reweighting (Rumack et al.,
PLOS Comp Bio 2022). With a months-scale archive, prefer isotonic/parametric shrinkage
over flexible ML recalibrators, and re-fit posterior-mean Kelly (B1) from the same
nightly job.

### B5. Model settlement mechanics explicitly in near-settlement trading

Determination is **delayed** when the CLI high is inconsistent with METAR 6-hr/24-hr
highs or when the final report reads lower than a preliminary one — an officially
acknowledged discrepancy channel. Near-settlement strategies (observed-high lock,
late-day bin trading) should carry a preliminary-vs-final CLI risk term instead of
treating the evening METAR high as settled truth.

---

## Unverified inspiration (no claims survived; read the code, don't trust the READMEs)

- https://github.com/Oalkhadra/prediction-market-trading — systematic Kalshi
  temperature strategy: boosted decision tree over ~30 features from multiple
  independent weather providers. Closest public analog to WeatherEdge.
- https://github.com/suislanchez/polymarket-kalshi-weather-bot — Polymarket/Kalshi
  weather bot.

## Open questions → concrete next experiments

1. **NBM vs current blend at KSFO**: backtest NBM point guidance (IEM archive) against
   the SQLite CLI archive. This is the single cheapest potentially-large accuracy win.
2. **Kalshi microstructure**: no public data survived; measure fees/spread/liquidity
   and time-of-day mispricing from our own `dataset_kalshi_candles` /
   `market_snapshots` tables.
3. **Burn-off predictor value**: how much Tmax MAE does a GOES-West + morning-METAR
   clearing-time feature buy? (MSFS proves the sub-problem is tractable; no source
   quantifies the downstream Tmax gain.)
4. **Recalibration method at small n**: isotonic vs Platt vs parametric shrinkage on
   a few months of outcomes; at what archive length does DRN become worthwhile?

## Verification stats

5 search angles · 22 sources fetched · 105 claims extracted · 25 verified by 3-vote
adversarial panels · 24 confirmed / 1 refuted · 9 merged findings. Refuted and
therefore excluded: "basic postprocessing captures most of the available CRPS gain
before ML adds more" (1-2 vote).
