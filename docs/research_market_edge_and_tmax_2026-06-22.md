# Research — Weather Prediction-Market Trading Edge + Next-Day Tmax Accuracy

**Date:** 2026-06-22
**Method:** deep-research harness — 5 search angles → 21 sources fetched → 103 claims extracted → 25 adversarially verified (3-vote, 2-of-3 to kill) → **25 confirmed / 0 killed** → synthesized to 10 findings.
**Companion doc:** [calibration_edge_plan_2026-06-22.md](calibration_edge_plan_2026-06-22.md) (the implementation plan derived from this research).
**Scope chosen:** trading edge first (~65%), forecast accuracy second (~35%), bias toward forkable open-source.

---

## Executive summary

The edge in weather markets is **not** a better point forecast — it is converting a *calibrated probability distribution* over next-day Tmax into orders by: (1) edge = your probability − live order-book price, (2) de-vig the contract ladder into a clean distribution, (3) size with **fractional (shrunk) Kelly**, (4) gate every trade through an enumerable risk stack with hard caps and a drawdown circuit-breaker.

The single most important finding for WeatherEdge: **Kalshi weather/temperature markets are systematically over-confident at short horizons** — prices are *too extreme* relative to outcome frequencies, the opposite of political markets. That is the market-side mirror of our warm/hot anti-calibration loss, and there is a **drop-in transform to de-bias it**: `p* = p^θ/(p^θ+(1−p)^θ) = σ(θ·logit p)` with θ<1 at short horizons.

---

## Part 1 — Trading edge & engine mechanics (primary)

### 1.1 Forkable reference engine — OctagonAI/kalshi-trading-bot-cli
TypeScript, MIT, ~335★ — <https://github.com/OctagonAI/kalshi-trading-bot-cli>

Implements exactly the engine shape we run:
- **Edge = model probability − live order book** (not mid).
- **Half-Kelly sizing** (`kelly_multiplier` default 0.5).
- **5-gate risk engine** every trade must pass: **Kelly · Liquidity · Correlation · Concentration · Drawdown**.
- Hard caps: `max_drawdown 20%`, `max_positions 10`, `max_per_category 3`, `daily_loss_limit $200`.

⚠️ **Caveat:** it is a generic LLM/fundamental-research bot (probabilities from an AI research API, *not* a weather model); its published backtest (Brier 0.168, +12.5% skill) is marketing — ignore the numbers. The transferable part is the **architecture**: model-vs-orderbook edge + fractional Kelly + an enumerable multi-gate veto stack. *(verified 3-0 across three component claims)*

### 1.2 Fractional Kelly is formally correct, not a fudge factor
Baker & McHale (2013), *Decision Analysis* 10(3):189-199 — <https://pubsonline.informs.org/doi/abs/10.1287/deca.2013.0271>

Standard Kelly plugs *estimated* win-probabilities in for true ones, which **systematically overbets** and gives worse out-of-sample than in-sample performance; the optimal correction is to **shrink below full Kelly**, proportional to parameter uncertainty. Corroborated by MacLean/Ziemba/Thorp (overbetting far more punishing than underbetting). **Implication: when the model is less certain — e.g. warm/hot regimes — the Kelly fraction should shrink further, not stay fixed.** *(verified 3-0)*

### 1.3 ⭐ Weather markets are over-extreme at short horizons → fade toward climatology
Le (2026), arXiv 2602.19520 — <https://arxiv.org/pdf/2602.19520> (292M trades, 327k Kalshi+Polymarket contracts)

- **Weather is one of only two domains with a significantly *negative* calibration intercept** (−0.086, 95% CI [−0.115, −0.057], entirely below zero) vs Politics +0.151.
- Within 48h, recalibration slopes are **0.69–0.97 (b<1 = over-confident)**, flipping to under-confidence (1.20–1.37) only beyond ~2 days.
- Mechanism: traders **over-react to meteorological signals** ("storm/heat tomorrow → push *yes* too far"), made costly by weather's low 24% base rate (threshold-exceedance contracts).

**Drop-in fix** — a standard log-odds recalibration:
```
p* = p^θ / (p^θ + (1−p)^θ)   =   σ( θ · logit(p) )
```
θ<1 (short-horizon weather) pulls toward the middle (de-extremes); θ>1 pushes toward extremes. *(intercept 3-0; per-horizon slope magnitudes 1-1 / medium — see caveats)*

### 1.4 Real-trader practitioner sources
No **audited P&L** from live weather traders surfaced (a real gap). On-topic practitioner writeups worth reading:
- northlakelabs — Kalshi weather postmortem & pivot: <https://www.northlakelabs.com/max/blog/kalshi-weather-postmortem-and-pivot/>
- northlakelabs — what I learned from 32 losing Kalshi trades: <https://www.northlakelabs.com/max/blog/what-i-learned-from-32-losing-kalshi-trades/>
- polymarketweather.com — strategy: <https://polymarketweather.com/blog/polymarket-weather-strategy>
- polymarketweather.com — weather bots: <https://polymarketweather.com/blog/weather-bots-polymarket>
- polymarketweather.com — ColdMath: <https://polymarketweather.com/blog/coldmath-polymarket>
- open-source bots: <https://github.com/suislanchez/polymarket-kalshi-weather-bot>, <https://github.com/Oalkhadra/prediction-market-trading>
- de-vig math (sports, applicable): <https://www.datawisebets.com/blog/devigging-sportsbook-odds>

---

## Part 2 — Forecast accuracy for next-day Tmax (secondary)

### 2.1 ⭐ Root cause of warm/hot miscalibration: regime-dependent bias
Du & DiMego (NOAA/NCEP), AMS 2008, Paper 133196 — <https://ams.confex.com/ams/88Annual/webprogram/Paper133196.html>

A model can be **warm-biased under high pressure but cold-biased under low pressure at the same station**, so a single global Tmax offset is structurally inadequate. Weighting past errors *by regime* improved NCEP's SREF ensemble mean. Corroborated by COMET/MetEd ("MOS cannot address regime-dependent biases… bias-corrected values can *degrade* the forecast during regime change") and ECMWF Newsletter 157 (bias sign-flips). **This is the accuracy-side root cause of our warm/hot problem — it argues for regime-aware de-biasing, not one global offset.** *(verified 3-0)*

### 2.2 NBM — free off-the-shelf calibrated blend, but not turnkey
NOAA National Blend of Models — <https://registry.opendata.aws/noaa-nbm/> (free on AWS Open Data, GRIB2 via S3, no subscription; v5.0 as of 2026-05)

A calibrated multi-model MOS blend. **But its Tmax has a documented warm-regime *cold* bias**: per NOAA's own algorithm doc (<https://vlab.noaa.gov/documents/6609493/7858320/Description_of_Field-Selected_Algorithms_for_National_Blend_of_Models.pdf>), the QMD temperature system trains on only the **previous 60 days with no climatology anchor**, so *"when the first heat wave of spring comes, the Blend's QMD temperature is usually too cold."* **Use NBM as a free benchmark/input, but it still needs local warm-tail correction.** *(verified 3-0)*

### 2.3 Neural-net distributional postprocessing beats classical MOS — forkable
slerch/ppnn (MIT) — <https://github.com/slerch/ppnn> + Rasp & Lerch 2018, MWR — <https://journals.ametsoc.org/view/journals/mwre/146/11/mwr-d-18-0187.1.xml>

NN postprocessing reached **CRPS 0.82 vs 1.16 raw ensemble (−29%)** for 48h 2m-temperature, beating boosted EMOS (0.85). The two dominant levers: **station embeddings** and **auxiliary (non-temperature) predictors**. Outputs a full predictive distribution — exactly what pricing range/bucket contracts needs. *(verified 3-0; caveat: 2m-temp not Tmax, 2018 benchmark, needs input adaptation)*

### 2.4 Warm/hot TAIL calibration needs deliberate training-time intervention
Wessel et al. 2025, arXiv 2506.13687 — <https://arxiv.org/pdf/2506.13687>

**All** SOTA probabilistic models are **not tail-calibrated**, and added flexibility doesn't fix it. Two fixes: (a) a **tail-weighted scoring rule (twCRPS)**, or (b) a **tail-miscalibration regularizer** (tail extension of Wilks 2018). 🔴 **Hard trade-off:** both improve tail calibration *at the expense of overall skill* (tail calibration up >60% while overall calibration ~187% worse in their study). Gate behind A/B walk-forward. *(verified 3-0; caveat: study is on 10m wind speed, not temperature)*

### 2.5 EVT / analogue templates for the warm tail
- **gbex** — <https://github.com/JVelthoen/gbex> (Extremes 2023, <https://arxiv.org/abs/2103.00808>): fits a **covariate-dependent Generalized Pareto distribution above a high threshold via gradient boosting** — true peaks-over-threshold EVT (not ordinary quantile regression, which "performs poorly… data in the tail are too scarce"). R package → Python port. *(verified 3-0)*
- **ecPoint** — <https://github.com/ecmwf/ecPoint> (Nature CEE 2021, Apache-2.0): conditional gridbox-analogue postprocessor. ⚠️ It is a *rainfall* method; its "drop location" result is precip-specific — **do not** use it to justify dropping per-station (SFO marine) calibration; the temperature literature says those station biases genuinely help. The *template* transfers; the location-dropping does not. *(global-aggregation claim 2-1)*

---

## Part 3 — Caveats (what this research does NOT prove)

1. **The headline trading finding rests on one non-peer-reviewed preprint.** arXiv 2602.19520 is single-author, no CIs on per-horizon slopes, and its "Weather" bucket aggregates temperature + precip + natural-events. Treat the **direction** (short-horizon temp prices too extreme → fade) as solid; treat the **exact θ values as provisional → re-estimate θ on our own SFO trade history before going live.**
2. **No audited live track records surfaced.** The "documented edges/P&L from real weather traders" part is largely *unanswered* by verifiable sources — only loss-postmortems, not quantified wins.
3. **Several accuracy results are on adjacent variables:** ppnn = 2m-temp (German stations, 2018); tail paper = wind speed; ecPoint = rainfall. Techniques transfer; the specific CRPS numbers will not carry to SFO Tmax.
4. **Tail-calibration penalties degrade common-case skill** — never ship without A/B walk-forward.
5. **NBM** is v5.0 while the authoritative algorithm doc is v4.1 — mechanism appears unchanged but verify; NBM's own warm-regime cold bias means it is not a turnkey fix.

---

## Open questions (carried into the plan)

1. Are there verifiable, quantified track records (win rate, ROI, Sharpe, drawdown) from real Kalshi/Polymarket Tmax traders? None surfaced.
2. Do the overconfidence slopes / θ hold for **temperature contracts in isolation** (and for SFO specifically) vs the aggregate "Weather" bucket? Re-estimate on our own market data before going live.
3. Does the recalibration transform interact correctly with our existing market-consensus de-vig ladder — target the model probs, the market-implied signal, or both? (Resolved in the plan: **market-implied only.**)
4. For the warm/hot tail, which yields the best skill-vs-effort: tail-weighted CRPS / Wilks penalty on the LSTM, a separate POT/EVT head (gbex), or a regime-conditioned bias correction (Du & DiMego) on the forecast — and can any be validated with enough exceedances given the data-limited hot tail?

---

## Source list (21 fetched; quality/angle)

| Source | Quality | Angle |
|---|---|---|
| github.com/OctagonAI/kalshi-trading-bot-cli | primary | risk engineering |
| pubsonline.informs.org/doi/abs/10.1287/deca.2013.0271 (Baker & McHale) | primary | risk engineering |
| arxiv.org/pdf/2602.19520 (Le 2026) | primary | risk engineering |
| ams.confex.com/ams/88Annual/webprogram/Paper133196.html (Du & DiMego) | primary | risk engineering |
| vlab.noaa.gov/.../National_Blend_of_Models.pdf | primary | forecast accuracy |
| registry.opendata.aws/noaa-nbm/ | primary | forecast accuracy |
| github.com/slerch/ppnn | primary | forecast accuracy |
| journals.ametsoc.org/.../mwr-d-18-0187.1.xml (Rasp & Lerch) | primary | forecast accuracy |
| arxiv.org/pdf/2506.13687 (Wessel et al.) | primary | forecast accuracy |
| nature.com/articles/s43247-021-00185-9 (ecPoint) | primary | forecast accuracy |
| github.com/ecmwf/ecPoint | primary | ML methods |
| arxiv.org/abs/2103.00808 (gbex) | primary | ML methods |
| github.com/JVelthoen/gbex | primary | ML methods |
| github.com/Oalkhadra/prediction-market-trading | blog | practitioner |
| northlakelabs.com/max/blog/kalshi-weather-postmortem-and-pivot/ | blog | practitioner |
| northlakelabs.com/max/blog/what-i-learned-from-32-losing-kalshi-trades/ | blog | real-world P&L |
| github.com/suislanchez/polymarket-kalshi-weather-bot | blog | practitioner |
| datawisebets.com/blog/devigging-sportsbook-odds | blog | practitioner |
| polymarketweather.com/blog/coldmath-polymarket | blog | real-world P&L |
| polymarketweather.com/blog/weather-bots-polymarket | blog | real-world P&L |
| polymarketweather.com/blog/polymarket-weather-strategy | blog | real-world P&L |
