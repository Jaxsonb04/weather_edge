# WeatherEdge — Independent Modeling Audit

**Question:** is WeatherEdge as strong as it can reasonably be at (1) weather prediction and probabilistic forecasting, and (2) Kalshi weather-market trading modeling? If not, what would materially improve it?

**Audited revision:** `d16448cf6ac872fbbb9fde5f44eb762074b5d776` (2026-07-27), read-only snapshot via `git archive HEAD`.
**Live revalidation:** the public production artifact `https://jaxsonb04.github.io/weather_edge/trading_signal.json`, fetched 2026-07-27, with target rows stamped `2026-07-27T21:42:00+00:00`. Four findings below are confirmed against it and are marked *live-revalidated*.
**Method:** direct reading of the forecasting, trading, research, storage, deployment and dashboard code; one numeric experiment on the repository's own archived prediction artifacts; twelve parallel independent area reviews followed by an adversarial refutation pass. Where refutation changed a conclusion, the corrected conclusion is what appears here (see §6.A.1).
**Boundaries observed:** nothing in the repository, git history, deployment, database or paper-account state was modified. No production writes, no trades, no connection to the EC2 runtime. Real-money execution treated as out of scope. Economically separate paper accounts are kept distinct throughout; no cross-account total is presented as one bankroll's return.

---

## 1. Verdict

**No — and the two halves fail for different reasons.**

**Forecasting.** The architecture is close to right and genuinely well built: a leakage-disciplined multi-model NWP archive, rolling-origin non-homogeneous Gaussian regression per station, a station/CLI settlement registry verified against live market rules, and a replay-gated trailing recalibration. What is missing is **verification of the object actually served**. Every calibration number the project publishes describes a different thing than the distribution the trader consumes — a different model (LSTM, not EMOS), a different truth source (station observations, not CLI), a different ladder (a static 69.5 °F ladder, not the traded one), a different lead, and without the market blend. The `source='live'` rows the trader reads have never been scored by anything.

**Trading.** The execution and accounting engineering is unusually good — a real queue-ahead maker-fill model against the public tape, double-entry paper ledgers, entry-frozen archived accounts, honest CLI settlement truth. The *modeling* is not. Across 34 config revisions in eight weeks the live approval bar has been driven down to roughly **$0.002–0.007 of after-fee edge per contract**. That number is smaller than the repository's own measured calibration gap (§4.1), smaller than a fee-rounding discrepancy inside its own fee model (§6.C.1), and smaller than the errors in its own lower-confidence bound — of which there are three independent ones, all permissive: the bound is inflated on the NO side by a clipping artifact (§6.C.3), it *narrows* 2.8× exactly when no historical analogue exists (§6.A.9), and it is not a confidence bound in the first place (§6.A.11). **At the current bar the system cannot distinguish a real edge from its own modeling error.**

**The most important finding is an absence.** In ~65k lines of source and ~65k lines of tests there is nowhere a comparison of the model's probabilities against the market's own de-vigged probabilities on settled outcomes. Climatology is the only baseline ever run (`backtest.py:_climatological_prior`). Beating climatology establishes that this is a weather model; the entire trading thesis is that it beats *the Kalshi ladder*, and that has never been measured. The data to measure it is already stored — `decision_snapshots` persists `model_probability` and `market_probability` side by side (`store/schema.py:160-161`) — and the rejected arm of it is on a 45-day deletion clock.

**Bottom line for whoever acts on this:** stop tuning. The parameter surface has not been the binding constraint for some time. Run the three measurements in §8.1. Each is roughly a day of work, each uses data you already have, and any one of them could invalidate a policy the book is running on today.

One thing this audit does *not* find: I found no evidence the project is deceiving itself deliberately. The README's "What is not proven yet" paragraph, the refusal of win rate as a success metric in June, the liquidity-ceiling analysis, and the account-separation rules are more honest than most published work. The problems below are the ones a well-run project accumulates when its scrutiny has gone almost entirely into engineering correctness (§9).

---

## 2. What standard I used, and why

I judged the system against what a competent operational probabilistic-forecasting and systematic-trading practice would require of itself — not against model-architecture fashion.

1. **The predictive distribution is the product.** For a system whose output is `P(settlement lands in bin)`, MAE is nearly irrelevant and calibration is nearly everything. PIT / reliability / tail-exceedance evidence outranks MAE and RMSE by a wide margin here.
2. **Verify the object you serve.** A calibration number computed on a different model, ladder, lead, or truth source is not evidence about the traded distribution.
3. **The baseline must be the competitor.** For a trading system that is the market. Climatology skill says nothing about edge.
4. **Edge must survive the frictions actually paid** — the real fee schedule with its real rounding, realistic fills, and the round trip.
5. **Evidence must be reproducible and multiplicity-aware.** A policy accepted on the sample that selected it is a hypothesis, not a result.
6. **Complexity is not improvement.** No credit for sophistication or novelty. Several recommendations below are to *delete* things.

Two things I deliberately did not do. I did not assess whether a fancier model class (GBM on ensemble features, neural post-processing, mixture density networks) would help — at 60–400 training days per city it almost certainly would not, and the binding problems are upstream of model class. And I did not treat the prior audits as settled: they are strong engineering audits, but three items they closed are still open (§6.G.7).

---

## 3. What I examined, and what I could not

**Examined in full or near-full.** Forecaster: `emos_forecast.py`, `postproc_models.py`, `emos_recalibration.py`, `recalibration_replay.py`, `nwp_archive.py`, `blend_sources.py`, `blend_learners.py`, `blend_archive.py`, `city_truth.py`, `nws_ground_truth.py`, `truth_store.py`, `scores.py`, `forecast_backtest.py`, `forecast_postproc_backtest.py`, `settlement_calendar.py`, `cities.py`, the Google runtime stack, `forecaster/research/`. Trading: `probability.py`, `risk.py`, `config.py`, `fees.py`, `execution.py`, `maker_fills.py`, `paper.py`, `settlement*.py`, `monitor.py`, `exits.py`, `portfolio.py`, `joint_kelly.py`, `posterior_kelly.py`, `backtest.py`, `backtest_rescore.py`, `restatement.py`, `replay.py`, `clv.py`, `archive.py`, the `research_*` family, `strategy_lab/`, `store/schema.py`, and the decision/research query paths of `db.py`. Also: the deploy tree and all 12 systemd units, the CI workflows, the React SPA data and presentation layers, and every tracked document under `docs/`.

**One numeric experiment I ran** (read-only, on the repository's own artifacts): the empirical distribution of SFO next-day daily-high forecast residuals, collapsed to one row per date, from `forecaster/models/lstm_target_daily_high_next_day_{test,val}_preds.csv`. Reported in §4.2 and reproducible from the command in §10.

**What I could not evaluate.**

- **`forecaster/weather.db` does not exist on the development machine.** The NWP archive, the rolling-origin EMOS archive and `cli_settlements` live only on the EC2 runtime. Every question of the form "what is the empirical calibration of the served distribution" is therefore stated below as a *specified experiment*, not an answered one.
- **`trading/data/paper_trading.db` on the development machine is a 311 KB freshly-initialised file.** No performance claim here derives from it, and per repository instruction I did not diagnose production behavior from ignored local runtime artifacts. Where a claim needed production state I revalidated against the *public published artifact* or marked it open.
- **Kalshi's current fee schedule and the maker-fee status of the `KXHIGH*` series.** Two findings (§6.C.1, §6.C.2) turn on this and are stated as discrepancies needing an external check, not as proven errors.
- **`docs/AUDIT-PLAN*.md` is gitignored** (`.gitignore:65`). The 77-finding July audit that set much of the current policy is workstation-local; I read it for cross-checking but it is not part of the repository's evidence (§6.D.2).
- I did not connect to EC2, query the production database, or run anything against production.

**Verification coverage, stated honestly — including where this report was wrong.** Nine of twelve area reviews were put through an independent adversarial refutation pass. The remaining three — the probability→bin conversion, operational infrastructure, and the data/retention layer — I verified myself, by re-reading each cited code path and by running the measurements shown inline; an independent refutation pass on the operational and retention claims was still running when this document was finalised, and its results are not reflected here.

That verification changed this report materially, in both directions:

- **Added** after the first version was written: A.9 (the safety band narrows 2.8× when conditioning fails), A.10 (forced ladder normalisation inflates the market prior above the ask), A.11, A.12, A.13, and the quantified fee table in C.1.
- **Withdrawn or downgraded** once I found the account-layer risk ladder (`account.py:47-152`, §5.12), which I had missed: **C.8** ("no control bounds same-day exposure across cities" — false; there are 20% aggregate, 5% per-city-day and 8% per-region-day caps), **C.9** (the 8%-per-city portfolio budget is never the operative bound), **C.10** ("documentation disagrees with code" — false; the memory's "2% daily-loss breaker" is `account.DAILY_LOSS_PCT` exactly, and I had read only the `db.py` breaker), and **C.5** (masked in the current configuration by a `$30` per-position placement cap). A working note's claim that the blended posterior sums to ~1.14 was also withdrawn; the measured figure is −0.57% (A.13).
- **Corrected** by the refutation pass: §6.G.1 (I had said the LSTM is in no production path; it is in one, as the residual-calibration source) and a sigma-direction claim that turned out to be the two halves of one fat-tail defect (§4.2).
- **Kept against a refutation that was itself wrong**: §6.F.2, where a reviewer argued the observation-derived-certainty path was unreachable; the reachability condition is a calendar comparison, so it is reachable, and the finding is now stronger than first drafted.

Where a finding is marked CONFIRMED at high or critical severity, I either read the code path myself or ran a measurement. The measurements are shown inline (§4.1, §4.2, A.9, A.10, A.13, C.1, C.4) and reproducible from §10.

---

## 4. The evidence for the verdict

Four measurements, ordered by how much they should change your mind.

### 4.1 The system's own published reliability table shows its near-certainties are over-confident — by about the size of the edge it trades

Fetched live from production today, and byte-identical to the tracked fixture `public/trading_signal.json` (that identity is itself a finding — §6.B.4). SFO, n = 262 scored days:

| model probability band | bin-observations | mean modelled p | observed frequency | gap |
|---|---:|---:|---:|---:|
| 0.0 – 0.1 | 810 | 0.0304 | **0.0370** | **+0.0066** |
| 0.1 – 0.2 | 371 | 0.1518 | 0.1429 | −0.0090 |
| 0.2 – 0.3 | 215 | 0.2274 | 0.2651 | +0.0377 |
| 0.3 – 0.4 | 14 | 0.3525 | 0.1429 | −0.2096 |
| 0.4 – 0.5 | 23 | 0.4591 | 0.3478 | −0.1113 |
| 0.5 – 0.6 | 18 | 0.5444 | 0.4444 | −0.1000 |
| 0.6 – 0.7 | 14 | 0.6475 | 0.5000 | −0.1475 |
| 0.7 – 0.8 | 14 | 0.7481 | 0.7143 | −0.0338 |
| 0.8 – 0.9 | 23 | 0.8541 | 0.9565 | +0.1024 |
| 0.9 – 1.0 | 70 | 0.9670 | **0.9286** | **−0.0384** |

The live book buys NO on bins whose YES probability sits in the 0.0–0.1 band. There the model says the bin happens 3.04% of the time and it happened 3.70% — **the NO side is over-stated by 0.0066**. The complementary high band is over-stated by 0.0384. Both extremes lean the same way.

`docs/SESSION_MEMORY.md:66-70` records that after the 2026-07-27 change the approved live candidates carried after-fee lower-bound edges of **0.002–0.007**. The low-band calibration gap sits *inside* that range; the high-band gap is five to nineteen times it.

**The caveat is the finding.** Binomial SE at p = 0.037, n = 810 is 0.0066; at p = 0.929, n = 70 it is 0.031. The bin-observations are clustered inside ~262 days, so effective errors are larger still. **Neither gap is statistically distinguishable from zero — which is exactly the point.** The traded edge is smaller than the calibration uncertainty of the model generating it. No further gate tuning changes that; only a better-measured distribution or a bigger edge does.

### 4.2 The predictive distribution is Gaussian; the residuals are not

`postproc_models.apply_emos:181-187` returns a Gaussian `(mu, sigma)`; `probability.py:159` integrates it over each bin. I measured the empirical residual distribution from the repository's own archived SFO next-day predictions, collapsed to one row per date:

| artifact | n days | skew | kurtosis | P(\|z\|>1) | P(\|z\|>2) | P(\|z\|>2.5) | P(\|z\|>3) |
|---|---:|---:|---:|---:|---:|---:|---:|
| `lstm_..._test_preds.csv` | 473 | +0.862 | 4.335 | 0.2664 | 0.0465 | **0.0254** | **0.0169** |
| `lstm_..._val_preds.csv` | 504 | +0.824 | 5.287 | 0.2520 | 0.0516 | **0.0278** | **0.0198** |
| Gaussian | — | 0 | 3.0 | 0.3173 | 0.0455 | 0.0124 | **0.0027** |

Peaked-centre, fat-tailed: *lighter* than Gaussian at 1σ, **2× heavier at 2.5σ and 6–7× heavier at 3σ**, with a pronounced warm skew — the model under-predicts hot days.

This resolves an apparent contradiction inside the repository. `emos_recalibration.py:11-14` records that "coverage says several cities are over-dispersed", i.e. sigma too *wide*. Both are true and they are the two halves of the same defect: **a Gaussian moment-matched to a fat-tailed residual distribution is over-dispersed in the centre and under-dispersed in the far tail.** An 80% coverage check sees the former; the far-tail exceedance sees the latter. The system's entire economics live in the latter — `comfort_edge` (`risk.py:622-673`) explicitly *blocks* near-forecast bins and *size-boosts* far-tail NO bins, which is exactly the region where the Gaussian is most wrong and most optimistic.

*Status: the residual measurement is CONFIRMED; the inference to EMOS is INFERRED.* Caveats: LSTM not EMOS, 2024–25 SFO, and EMOS's flow-dependent variance will reduce unconditional kurtosis somewhat. The test that settles it is §8.1.2.

### 4.3 The reported trading result is a selection, not a measurement

`docs/SESSION_MEMORY.md:60-76` reports the execution-bar change moving live from "58 positions / 87.9% win / $1.33/day" to "144 positions / 92.4% win / $3.22/day (day-clustered 95% CI +$0.27..+$5.94)". Four independent reasons this cannot bear the weight placed on it:

1. **The threshold was chosen on the days used to evaluate it.** `config.py:494-519` records the justification: the population the 0.02 bar refused "returned +$69.45 over 20 target days ... day-clustered 95% CI +$0.80..+$6.05", and the bar was then set to admit exactly that population. No holdout.
2. **A 92.4% win rate inside a 0.70–0.97 price band is break-even by construction.** Buying at cost `c` breaks even at a win rate of exactly `c` (`favorite_band_min_cost = 0.70`, `favorite_band_max_cost = 0.97`, `config.py:337-338`). So the break-even rate for this book lies between 70% and 97%, and 92.4% is *uninformative* without the cost-weighted mean entry price — which is never reported beside it. The repository knows this: `README.md:106-108` cites `docs/trade_engine_overhaul_plan_2026-06-17.md` for "why win-rate was refused as a success metric (it is trivially maximized by betting deep favorites into an EV-negative book)". The favorite band shipped a month later and the win rate now leads the session brief. `posterior_kelly.CohortRecord(n, wins, mean_claimed_prob, mean_cost)` gets this right *internally*; the published `hit_rate` (`strategy_lab/paper_card.py:127, 717, 814`) carries no `mean_cost`.
3. **The day-clustered CI has ~10 clusters.** The distinct-approved-days list at `SESSION_MEMORY.md:136` is ten days. A percentile bootstrap over ten clusters has no usable coverage; a lower bound of +$0.27 from ten days is not meaningfully different from zero.
4. **The per-day figures are assumed-fill simulations, not realized performance.** The underlying simulation sizes at displayed depth and books `pnl = size × ((1 if win else 0) − cost)` — i.e. it assumes the order fills. The live record is dominated by expired-unfilled resting orders (`SESSION_MEMORY.md:141-145`: "live maker quotes filled under 20% (46/49 expired 07-18; 0/3 on 07-22)"). A per-day number computed at 100% fill is not comparable to realized paper P&L and should never be quoted as one.

Separately: **the analyses that produced every headline are not in the repository.** They live as ~200 untracked throwaway scripts on the build machine. The repository *contains* a rigorous pre-registered, Holm-corrected, day-clustered promotion framework (`research_promotion.py`, `research_significance.py`, `research_bootstrap.py`, ~6,000 lines) whose gate requires ≥30 independent confirmatory target days, paired after-fee ROI and log-growth CIs above zero, no CRPS/Brier/calibration-gap regression, and family-wise significance. **No deploy script, systemd unit or CI workflow invokes it** (`grep -rn 'research-evaluate' trading/deploy .github scripts` → zero hits), and no promotion verdict artifact is committed. Meanwhile `config.py` has **34 commits since 2026-06-01**, each editing `LIVE_PROFILE_OVERRIDES` with a prose rationale.

### 4.4 The metric that governs a live trading gate is computed using the realized outcome

`forecast_postproc_backtest.py:238` sets `settled_cohort=predicted_temperature_cohort(actual)` — the cohort derives from the **realized** value. That cohort then *selects the predictive sigma used to score the day's Brier*:

- `forecast_postproc_backtest.py:252` — `sigma = shared_sigmas.get(day.settled_cohort) ...`
- `forecast_postproc_backtest.py:274` — same, per cohort
- `forecast_backtest.py:685` — `sigma = sigma_by_cohort.get(record["settled_cohort"], overall_sigma)`

A day that turned out hot is scored with the hot-cohort sigma; a day that turned out cold with the cold sigma. The distribution being scored is not one that could have been issued. This inflates measured Brier skill by construction.

This is not academic. `config.py:382-390` unblocked the warm cohort on live citing "walk-forward shared-sigma Brier 0.895, on par with cold 0.903 / normal 0.862 — see docs/PHASE0-findings.md", and `forecast_postproc_backtest.py:265-269` says in its own docstring that the cohort Brier row "is what tells us whether a post-processor actually earns a blocked cohort back". **A leaky metric is the stated justification for a live trading gate.**

There is a second, independent defect in the same place: the evidence is bucketed by the **settled** cohort while the live gate applies on the **forecast** cohort (`risk.py:87-96` → `temperature_cohort(forecast_high_f)`). The gate and its evidence are indexed on different variables, so even a leakage-free version of that table would not license the gate.

---

## 5. What is strong, and should be preserved

Load-bearing and better than the norm for a project of this size. Do not refactor these away while fixing the rest.

1. **The city / settlement registry** (`cities.py`, deliberately duplicated in both packages with a parity test). Station identities verified against live market rules, the series `settlement_sources` URL, and the actual CLI product header, with the real traps documented and correct: Houston on KHOU not IAH, Dallas on KDFW not Love, Chicago on KMDW not O'Hare, New York on KNYC. The climate day is defined in each station's **fixed standard time** end to end, including inside the Open-Meteo query strings. This is the most commonly botched thing in weather markets and it is right here.
2. **Settlement truth.** `cli_settlements` is station-keyed, sourced from the real NWS CLI product (live plus the IEM archive of the same product), carries an explicit `is_final` flag with preliminary detection, and `load_cli_settlement_truth` **fails closed** on legacy schemas lacking finality. The legacy SFO-only table was migrated and dropped, so there is exactly one truth store. The bin→settlement mapping is exactly consistent between `MarketBin.continuous_interval()` and `bin_resolves_yes()` for all three strike types — I checked every boundary.
3. **Leakage discipline on the NWP archive and the EMOS fit.** `nwp_archive.py` uses Open-Meteo's *previous-runs* API specifically to avoid analysis leakage; `emos_ngr_predictions` carries an explicit `truth_lag_days` availability boundary so a lead-*L* replay sees only truth published by *D−L−1*; three distinct leakage unit tests pin it. Correct discipline, correctly tested.
4. **The maker fill model** (`maker_fills.py`, `exec-v4`). Single-aggressor normalisation from Kalshi's documented `taker_book_side` semantics, price-time priority, queue-ahead attached to the price where the queue was observed, per-trade volume claims persisted so consumed tape can never be double-credited across passes or restarts, and an explicit refusal to invent a fill from a payload that cannot prove direction. The optimistic "quote moved past my price" shortcut exists in `db.fill_resting_limit_order` but is called only from tests. This is materially better than most retail backtests — and better than this repository's own env-file description of it (§6.G.5).
5. **Paper accounting arithmetic.** Fees charged once per leg, settlement paying $1.00 with no settlement fee (correct for Kalshi), double-entry journalling with idempotency keys, partial closes as immutable child lots, `restatement.py` opening the database `mode=ro`, archived accounts hard entry-frozen at the write boundary. Exits price at the **bid** minus a **taker** fee and are capped at displayed bid depth (`exits.net_exit_per_contract`), and a TAKE_PROFIT can only be labelled when the net exit clears the fee-inclusive entry cost — so profit-banking is round-trip positive by construction. These are the conservative, correct choices.
6. **Decision-time state is preserved on the trading side.** `forecast_snapshots` is append-only and stores the full `raw_json`, which on the EMOS path carries `mu` and `sigma` (`db.py:2281-2296`, `forecast.py:503-511`), and `decision_snapshots` links to it plus a `scan_context_id` and a `decision_policy_fingerprint`. Trading replay of a historical decision is therefore sound. This bounds the severity of §6.F.9.
7. **`joint_kelly.py`.** Correctly models same-ladder mutual exclusivity by maximising expected log wealth over the full scenario set rather than stacking independent per-bin Kelly bets — the right treatment of the dominant correlation inside an event.
8. **The research framework's *design*.** Pre-declared immutable hypotheses with tolerances stored before evidence exists, same-case pairing with explicit coverage exclusions, day-clustered bootstrap, Holm-Bonferroni across the family, and a gate that cannot grant live activation by construction. The design is right; the problem is that it is not what governs the live book (§4.3, §6.D.1).
9. **Operational safety plumbing.** A per-city fixed-standard settlement clock, an installer that hard-gates the host timezone, archive-before-prune ordering with an independent gate, finality-gated settlement with persisted verification rows, and a publication manifest that refuses to re-stamp an unchanged artifact as fresh. Several of these close failure modes the project actually hit.
10. **The safety posture and the honesty of the caveats.** `live_execution.py` raises `LiveTradingDisabled` and holds no authenticated client; five independent env gates default off; readiness has never passed and the project says so publicly. `README.md:52-55` states the paper book's lifetime result is a small negative, and `SESSION_MEMORY.md:441-443` reports the legacy shared account's true all-time realized P&L (−$41.62) separately from the legacy live strategy's attribution (+$45.70). Keeping those two numbers apart is a real discipline and the reason §6.D.11 is not a severe finding.
11. **`posterior_kelly.py`.** Judges realized win rate against `mean_cost` with a prior centred on break-even — the correct frame, and the one the published `hit_rate` lacks.
12. **The account-layer risk ladder** (`account.py:47-152`, enforced on the placement path via `paper.py:738-755`). Layered caps against *current equity*: per position `min($30, 3%)`; per (city, target_date) 5%; per (region, target_date) 8% across a complete 15-city → 8-region map; live sleeve 16%; aggregate across all accounts 20%; a 2% daily-loss pause; a 15% drawdown pause; and the position cap halved at 10% drawdown. A `$5` executable-notional floor rejects unplaceable dust. This is a genuinely well-constructed limit system and it is what actually bounds the book — three findings in an earlier version of this report were withdrawn or downgraded once I found it (C.8, C.9, C.10). It is also the reason the profile-level exposure percentages in `config.py` are never the operative bound, which is its one cost (C.9).

---

## 6. Findings

Severity is mine, weighted by effect on the two audit questions. Status is CONFIRMED (read in code or measured), INFERRED (strong reasoning from code, not directly observed running), or HYPOTHESIS.

### 6.A The predictive distribution

**A.1 — Gaussian predictive distribution against fat-tailed, right-skewed residuals; over-dispersed in the centre, under-dispersed in the tail. HIGH / INFERRED.**
Location: `forecaster/postproc_models.py:181-187`, `trading/sfo_kalshi_quant/probability.py:159`, `risk.py:622-673`.
Evidence and the centre/tail synthesis: §4.2. A contributing mechanism is that the variance stage regresses **in-sample** residuals (`postproc_models.py:161-178`: `biases`, `weights`, `mu_a`, `mu_b` are all fit on `history`, then `resid` is computed on those same days; the `d < 0` fallback sets `var_c = fmean(residual_sq)`, literally the in-sample MSE), with no (n−p) correction and `EMOS_MIN_TRAIN = 60`. An adversarial check correctly noted that 60 is a floor rather than the operating point and that unconditional coverage points the *other* way — which is why this appears here as one finding about distributional *shape* rather than two contradictory ones about sigma's level.
Impact: far-tail NO probabilities — the entire book — are systematically over-stated; the modelling error is plausibly several times the traded edge.
Response: §8.1.2 measures it. If confirmed, the fix is a heavier-tailed predictive distribution or the PIT recalibration candidate that already exists (§8.1.3) — not a bigger sigma, which would worsen the centre.

**A.2 — Residual skew is ignored by a symmetric comfort band. MEDIUM-HIGH / CONFIRMED.**
Location: `risk.py:606-619` (`_interval_gap_f`), `:622-673`.
Evidence: `_interval_gap_f` returns an unsigned distance, so a bin 7.5 °F *above* the forecast is treated as exactly as safe as one 7.5 °F below. Measured residual skew is +0.82…+0.86 (§4.2) — the hot tail is materially longer.
Impact: NO bets above the forecast are systematically riskier than NO bets below and are sized identically.
Response: make the band asymmetric, fit from the same residual archive.

**A.3 — The variance regressor is the RAW cross-model spread, so sigma responds to which models were present. HIGH / CONFIRMED.**
Location: `forecaster/postproc_models.py:171, 183`.
Evidence: `_spread(list(models_for_day.values()))` in both `fit_emos` and `apply_emos` — the per-model biases computed two lines earlier are never subtracted. `MIN_MODELS = 3` permits days with 3 of 8 models present.
Impact: raw spread mixes genuine day-to-day disagreement (signal) with fixed inter-model bias offsets (nuisance). A day on which a chronically-warm model drops out changes sigma for reasons unrelated to predictability.
Response: take the spread of the **debiased** values; add member count as a regressor or restrict the variance fit to constant-member days; verify with a spread–skill diagnostic (bin by spread decile, plot binned mean |error|).

**A.4 — EMOS is fit by staged OLS moment-matching rather than by minimising CRPS. HIGH / CONFIRMED.**
Location: `forecaster/postproc_models.py:119-187`.
Evidence: three separate closed-form steps; the variance stage regresses squared residuals (an approximately χ²₁ response, extremely right-skewed) on squared spread by OLS. The docstring markets this as "EMOS / NGR … Gneiting et al. 2005".
Impact: the functional form is right, the estimator is not the validated one; the variance slope has very large sampling variance and a handful of big-error days dominate it.
Response: keep the NGR form; replace stage 3 with a joint minimum-CRPS fit of (a, b, c, d) — the closed-form Gaussian CRPS already exists at `scores.py:18-27`, the optimisation is four-dimensional and needs no SciPy — or at minimum a maximum-likelihood variance fit. Do this *after* A.1 is measured: a better-estimated Gaussian is still a Gaussian.

**A.5 — Sigma conditions on an integer lead bucket only; the same-day market is served the lead-1 sigma. HIGH / CONFIRMED.**
Location: `forecaster/emos_forecast.py:279-297, 424-468`; `emos_recalibration.correction_for_serve`.
Evidence: lead 0 is served with `lead_days=max(lead,1)` — the lead-1 per-model biases, weights **and** sigma, and the lead-1 recalibration window. The docstring states it explicitly.
Impact: forecast uncertainty collapses monotonically through the day; by late afternoon the high is usually realised. A lead-1 sigma (order 2–3 °F) on a market hours from settlement makes the distribution grossly over-dispersed and flattens bin probabilities. Live blocks same-day entry; the research collector does not.
Response: archive a genuine lead-0 series and fit its own EMOS, or make sigma an explicit function of hours-to-settlement fit on scored live rows.

**A.6 — The serve-time bias correction is estimated on rolling-origin forecasts but applied to live forecasts. HIGH / CONFIRMED.**
Location: `forecaster/emos_forecast.py:55-61, 334-346`; `emos_recalibration.load_scored_series:106-131`; `recalibration_replay.py:111-113`.
Evidence: `SERVE_RECAL_BIAS = True`. The correction window is drawn exclusively from `source='rolling_origin_v2'` rows (`preferred_rolling_origin_source` can never return `'live'`) while the correction is applied to a mu produced from the *current-run* forecast. The acceptance replay that shipped it also ran only on rolling-origin rows. `emos_forecast.py:293-297` contains the project's own CONSISTENCY NOTE acknowledging the train/serve mismatch.
Impact: a bias correction is valid only for the estimand it was measured on; if the live current-run mu has a different mean error, this subtracts the wrong number.
Response: estimate the trailing correction from scored **live** rows (rolling-origin only during warm-up) and re-run the acceptance replay against live rows before leaving `SERVE_RECAL_BIAS` on.

**A.7 — The dispersion recalibration was rejected on the wrong metric. HIGH / INFERRED.**
Location: `forecaster/emos_forecast.py:60-62`; `emos_recalibration.compute_correction`.
Evidence: `SERVE_RECAL_SIGMA = False` because "pooled CRPS +0.4%, BOS lead-2 +5.2%". `compute_correction` already computes `sqrt(mean(z²))` — exactly the dispersion statistic — and discards it.
Impact: CRPS is dominated by the centre of the distribution; this system's economics are entirely in bin-boundary tail probabilities at 0.90–0.99. A dispersion correction that slightly worsens pooled CRPS can substantially improve the calibration of the probabilities actually traded. Rejecting it on pooled CRPS is the wrong decision criterion for this system. (Given A.1, a single scalar rescale is probably the wrong *fix* too — but `mean(z²)` is the right *diagnostic* and should be reported regardless of the ship decision.)
Response: re-run the acceptance gate on a trading-relevant loss — reliability of modelled p in the 0.90–0.99 band, or after-fee EV of the traded population — and publish `mean(z²)` per city and lead.

**A.8 — SFO's traded distribution pairs an EMOS mean with a different model's residual spread. HIGH / CONFIRMED (live-revalidated).**
Location: `config.py:228` (`emos_distribution_enabled = False` on base and absent from `LIVE_PROFILE_OVERRIDES`), `config.py:668-686` (`config_for_city` forces it on only for `not has_full_blend`), `cities.py:153` (SFO `has_full_blend=True`), `probability.py:110-159`, `forecast.py:604-628`, `monitor.py:50-51`, `sfo-weather.env.example` (`SFO_TRADING_SIGNAL_CALIBRATION_SOURCE=lstm`).
Evidence: the live artifact reports SFO's method as `"emos_wmean (live NWP ensemble) [SFO operational fallback] + intraday high-so-far update"`, `source_count=8`, with `google_high_f`, `history_high_f`, `nws_high_f`, `open_meteo_high_f` **all null** — SFO's point forecast is the EMOS mean. But on the live profile SFO keeps `emos_distribution_enabled = False`, so the **EMOS sigma is discarded**, and the residual `bias` and `sigma` come from `load_lstm_outcomes()`. At `probability.py:159` the normal component is centred at `predicted_high_f + bias` — the EMOS mean shifted by the **LSTM's** mean residual (measured at +0.43 to +1.09 °F, §4.2) and widened by the **LSTM's** unconditional residual spread (~4.2–4.4 °F) instead of EMOS's flow-dependent sigma.
Impact: the flagship city's traded distribution is a mismatched composite; the correctly-calibrated per-day sigma is computed, persisted, passed into the calibrator, and then ignored.
Response: the highest-value single decision in this audit — either enable `emos_distribution_enabled` for SFO on live (after a rescore) or restore a real blend. Do not leave the mismatch.

**A.9 — The `edge_lcb` safety band NARROWS 2.8× exactly when the conditional window finds no historical analogue — the opposite of its documented intent. HIGH / CONFIRMED (measured through the real code path).**
Location: `probability.py:81-90` (`conditional_stats`), `:238-246` (`se_sample_n`), `:274` (`standard_error`).
Evidence: `conditional_stats` widens its window through (2, 3, 5, 8, 12) °F and, if no window reaches `min_conditional_samples`, `return self.global_stats`. The band is then sized by `se_sample_n = min(cond.n, effective_n)`, carrying the comment "Cap the SE sample size at the conditional support so the band widens when conditioning is thin." **In the fallback branch `cond` *is* `glob`, so `cond.n` is the full archive count and the `min()` no longer caps anything.** Measured by constructing a 400-outcome archive and calling the real `ResidualCalibrator`:

| forecast | fell back? | `cond.n` | `effective_n` | `se_sample_n` | 1.96·SE at p = 0.95 |
|---:|:---:|---:|---:|---:|---:|
| 65 / 70 / 75 °F | no | 50 | 254.2 | 50.0 | **0.0604** |
| 120 / 150 °F | yes | 400 | 400.0 | 400.0 | **0.0214** |

Ratio 0.354 — the band collapses to about a third of its normal width. (The general factor is `sqrt(N_archive / min_conditional_samples)`.)
Impact: the fallback fires precisely on days with no historical analogue — regime breaks, record heat, and the first weeks of a newly added city. Those are the highest-error days, and on them the gate that `config.py:96-103` and `risk.py:51-61` both call the primary defense becomes ~2.8× more permissive. This is directly reachable today: `conditional_stats` is called on `emos_mu` when EMOS is active, and the 14 cities added 2026-07-06 have short residual archives. It is also an *inverted guard* — the `min()` was introduced specifically to widen the band in this situation (§6.G.7 records the June audit closing that item as FIXED) and it silently does nothing in the one branch that matters.
Response: in the fallback branch, cap `se_sample_n` at `min_conditional_samples` rather than at the global count — or better, stop deriving the band from a residual count at all (A.11).

**Addendum 2026-08-30 — the prescribed cap was shipped and reverted; do not reintroduce it as written.** The cap landed 2026-08-16 (`c82a67e`, PR #96) and was reverted 2026-08-30 (PR #109). Measured in production over 8/16–8/30: charging the fallback's ~n=400 global estimate the standard error of an n=35 sample produced `edge_lcb` deductions of 0.16–0.22 at mid-range p (honest global support gives ~0.05), pushed every candidate — including those with +2 to +5¢ point edges — below the `edge_lcb ≥ 0` floor, and cut paper entries from ~13/day to ~1/day within hours of the deploy while Kalshi public tape volume was unchanged (60k–210k contracts/day/city, July-comparable). §8.2's "expect the repaired bound to approve fewer trades" framing masked the shutdown as the fix working. The inversion this finding measures is real, but it is a *bias* exposure (no-analogue days may run hotter residuals), not a sampling-error deficit: in the fallback branch the served probability *is* the global estimate, and its sampling error rests on the global support. Any future remediation must price missing-analogue risk as an explicit additive penalty sized from replay evidence — or resolve A.11 — never as a synthetic small n.

**A.10 — Forced ladder normalisation of the market prior is not de-vigging; on the one real book in the repository it inflates `market_p` above the ask. HIGH / CONFIRMED (measured on the captured book).**
Location: `probability.py:667-678` (`_market_implied_probabilities`), `:681-705`; `consensus.py:121-128`.
Evidence: `total = sum(raw.values()); return {ticker: value/total …}`, with no check on the direction or magnitude of the deviation. Run against the repository's own captured ladder `trading/research/kalshi_kxhightsfo_open_markets.json`, event `KXHIGHTSFO-26JUN03` (all six bins active, 1¢ spreads):

raw implied values sum to **0.9700**, so every bin is scaled by **1/0.970 = 1.0309**. Resulting `market_p − yes_ask` per bin: −0.0048, −0.0039, −0.0008, **+0.0038, +0.0029, +0.0029**.
Impact: forcing the mid vector to sum to 1 is de-vigging only when the raw sum *exceeds* 1. When a ladder's implied values sum **below** 1 — which the only real book in the repository does — the same line inflates every market probability, and on the three largest bins the inflated `market_p` exceeds that bin's own ask. Since `market_p` is blended into the traded posterior at weight `1 − model_weight` (0.45 on live at full reliability), an inflation of +0.003 to +0.004 propagates into the posterior at roughly half that — squarely inside the 0.002–0.007 after-fee edge band the live profile documents as its approval population.
Response: normalise only when the raw sum exceeds 1 (a genuine overround), and record the raw sum so a sub-1 ladder is visible rather than silently rescaled. Report the overround alongside every `market_p`.

**A.11 — `edge_lcb` is not a confidence bound. HIGH / CONFIRMED.**
Location: `probability.py:151-153, 246-247, 266, 270-282`; `config.py:173, 181, 213`.
Evidence: `standard_error = sqrt(p(1−p) / se_sample_n)` — a *binomial* standard error — and then `lower_confidence = max(0, p − 1.96·SE − model_risk_penalty − disagreement_penalty − ensemble_disagreement_penalty)`. When EMOS is active, lines 151-153 replace `bias` and `sigma` outright, so `cond` contributes nothing to `p` except its **count**: the band's width is set by the size of a residual window that no longer participates in the estimate. The three subtracted penalties are hand-set constants (0.08 cap, 0.35, 0.20) with no evidentiary basis in the repository.
Impact: the quantity gated at `edge_lcb ≥ 0` is presented throughout (`config.py:96-103, 356-363, 509-517`; `risk.py:51-61`) as a statistical lower bound and is the sole real-money readiness floor. It is in fact a point estimate minus 1.96 binomial standard errors of a *different* estimator, minus three constants. It may still be a usefully conservative number — `config.py:509-517` reports realized frequency (0.9467) beating the mean modelled bound (0.9050) — but it is not a confidence bound, and its width does not respond to the actual uncertainty of the EMOS Gaussian that produced `p`.
Response: derive the band from the predictive distribution that actually generated `p` — e.g. propagate the EMOS parameter uncertainty, or bootstrap the bin probability over the EMOS fit — and keep the penalties only if each earns its value against evidence. Do not lower the `≥ 0` bar while doing this (§8.4).

**A.12 — The market prior is re-conditioned on an observed high the market has already priced. MEDIUM-HIGH / CONFIRMED.**
Location: `probability.py:227-237`.
Evidence: `market_probs = _market_implied_probabilities(markets)` is followed by `_condition_on_observed_high(...)` applied to those same market-implied values.
Impact: a live Kalshi book on a same-day market already reflects the running station maximum — that is *why* the low bins are quoted at a cent. Applying the model's own feasibility damping and renormalisation on top double-counts the observation and pushes the "market" prior toward the upper bins, so the quantity the model measures its disagreement against is no longer the market's view. This compounds A.10 on exactly the same code path.
Response: condition the model distribution on the observation; leave the market's own quotes alone.

**A.13 — The blended posterior is not renormalised, so it does not sum to one when bin reliabilities differ. MEDIUM / CONFIRMED (measured).**
Location: `probability.py:249-273` (weight computed per market at `:260-264`), `:740-766`, `:769-806`.
Evidence: `model_weight` is computed **inside** the per-market loop and scales by `_market_prior_reliability(market, config)`, which depends on that single bin's spread, depth and quote consistency — so the weight varies bin to bin. `sum(p) = 1 + Σ wᵢ(modelᵢ − marketᵢ)`, which is 1 only if `w` is constant. There is **no renormalisation after the loop** — the code goes straight to `results[market.ticker] = BucketProbability(...)`.
Measured magnitude: on the captured June book every bin has depth ≥ 25 and a 1¢ spread, so reliability saturates at 1.0 for all six bins, `w` is constant at 0.55, and **the defect does not bite**. On a ladder built to the July regime the memory documents (displayed asks of 4–6 contracts, heterogeneous depth), reliability spans 0.20–1.00, `w` spans 0.55–0.91, and the posterior sums to **0.9943 (−0.57%)**, a per-bin deviation of up to ~0.002 against a properly normalised vector.
Impact: real but small in the current regime, and its **sign is not fixed** — it depends on where the model disagrees with the market relative to where the weight is high. It matters because `joint_kelly` (enabled on live) allocates across these values as if they were a distribution over mutually exclusive scenarios, and because ~0.002 is the low end of the traded edge band. A working note quoted a much larger figure (~14%); I could not reproduce it and it is withdrawn.
Response: renormalise after the blend, or hoist the weight out of the loop. One line either way.

### 6.B Evaluation integrity

**B.1 — Brier scores select their predictive sigma using the realized outcome. CRITICAL / CONFIRMED.** See §4.4.
Response: bucket by the *forecast* cohort, or drop cohort-conditional sigma entirely and score every day with the sigma the model issued. Then re-derive `blocked_forecast_cohorts` from scratch. Until it is re-derived, both the current HOT block and the July warm-unblock rest on a leaky number.

**B.2 — The evaluation scoreboard fits EMOS with more truth than the live serve has. HIGH / CONFIRMED.**
Location: `forecaster/forecast_postproc_backtest.py:351-357` vs `forecaster/emos_forecast.py:159-165`.
Evidence: `emos_ngr_predictions` accepts `truth_lag_days`; the production archive builder passes `truth_lag_days=lead_days`; the scoreboard that produced `docs/accuracy_evaluation_2026-07-06.md` does not pass it, so it defaults to 0. Every EMOS arm in that document is fit with `lead` more days of truth than the live serve had.
Response: pass `truth_lag_days=lead` and re-run. The fix is one argument.

**B.3 — The published calibration backtest never uses the traded ladder and never applies the market blend. HIGH / CONFIRMED.**
Location: `trading/sfo_kalshi_quant/backtest.py:94`; call sites `report.py:208`, `_cli/backtest.py:48`, `strategy_lab/calibration.py:87`, `synthetic_blend.py:312`, `strategy_research.py:203`.
Evidence: `ladder = markets or standard_sfo_bins("KXHIGHTSFO-BACKTEST")`, and **no call site ever passes `markets=`**. `standard_sfo_bins` is a fixed ladder centred at 69.5 °F with `status="paper"` and no quotes; because `status != "active"`, `_market_implied_probabilities` returns `{}`, so `market_p` is `None` and neither the market blend nor market-prior reliability ever runs.
Impact: (a) the published reliability/Brier/RPS numbers describe the **pure model on a static ladder**, not the market-blended posterior that trades; (b) Kalshi re-centres its ladder daily — as `fallback_bins`' own docstring notes — while this one never moves, so warm and cold days fall into open-ended catch-all bins. The published cohorts show the artefact plainly: warm has RPS skill 0.5085 but top-bin accuracy 0.1231, while normal has RPS skill 0.1803 and top-bin 0.6331.
Response: pass the archived per-day market ladder. This is the same change that unlocks §8.1.1.

**B.4 — The public forecast-accuracy panel is a build-time static fixture that cannot update. HIGH / CONFIRMED (live-revalidated).**
Location: `public/diagnostics.json` (tracked), `src/lib/diagnostics.ts:31`, `trading/deploy/aws/publish_forecaster_pages.sh:33-39, 176-181`.
Evidence: `diagnostics.json` ships inside the prebuilt SPA and is **not** in `REQUIRED_JSON_ARTIFACTS`, the list of files overlaid with freshly-generated data at publish. Its `held_out` series ends **2026-05-18** and it carries no vintage field. Separately, the *calibration* block fetched live today is byte-identical to the tracked 2026-06-28 fixture — because it is a pure function of `ab_test_results.json`, which no systemd unit regenerates, and `backtest.py` caches on an outcomes hash.
Impact: two of the site's three quantitative forecast claims are frozen and presented as current.
Response: stamp every published metric with the vintage of the data behind it; regenerate or retire.

**B.5 — The forecasts the trader consumes are never scored anywhere. HIGH / CONFIRMED.**
Location: `forecaster/emos_recalibration.py:106-131`; `emos_sources.preferred_rolling_origin_source:15-23`; `recalibration_replay.py:44`.
Evidence: every scoreboard reads `source='rolling_origin_v2'`. The `source='live'` rows written at `emos_forecast.py:366` — including every lead-0 row — can never be selected.
Impact: the rolling-origin archive and the live serve are different objects (previous-runs reconstruction vs current run, different effective lead, and lead 0 exists only live). Every calibration number in the repository describes the archive; the traded distribution has never been verified.
Response: §8.1.2.

**B.6 — The only probabilistic scoreboard with a climatology baseline and a significance gate is hard-wired to SFO. HIGH / CONFIRMED.**
Location: `forecaster/forecast_postproc_backtest.py:343-345, 484-502`; `truth_store.py:20, 33`.
Evidence: `evaluate()` calls `load_clisfo_truth(conn)` and `load_nwp_forecasts(conn, lead_days)` with no station argument; both default to `KSFO`. `main()` exposes `--db`, `--lead`, `--baseline` and no city flag.
Impact: **14 of the 15 traded cities have never been shown to beat climatology at any lead.** The `docs/accuracy_evaluation_2026-07-06.md` evidence is SFO, n = 31 days, one season.
Response: parameterise it over the city registry — it imports nothing SFO-specific but the default station and the absolute-°F cohorts — and publish a 15-city table with the existing DM + moving-block-bootstrap gate.

**B.7 — Live adaptive learners refit on the holdout that validated them, behind a zero-margin gate. HIGH / CONFIRMED.**
Location: `forecaster/blend_learners.py:75-78, 103-113, 254-266, 563-601`; `weather_cache_config.py:125, 147, 153-155`.
Impact: a zero-margin acceptance check re-evaluated many times a day will accept noise with near-certainty.
Response: this path is currently unreachable in production (§6.E.1); resolve its status before it becomes reachable again.

**B.8 — The A/B test scores hourly rows as independent days. MEDIUM / CONFIRMED — previously flagged, still open.**
Location: `forecaster/ab_test_results.json` `target_temp_next_24h`; `forecaster/research/forecast_validation.py:26-27`.
Evidence: `n_days: 9897` for an hourly-sampled target, with `t_stat 5.99`, `p_ttest 2.2e-9`, DM `p = 0.029`. `docs/codebase_audit_2026-06-15.md` marked this **PARTIAL** in June; unchanged at HEAD.
Impact: strongly autocorrelated rows treated as independent inflate significance by roughly √24.
Response: collapse to daily, as the sibling branch already does.

**B.9 — The published per-cohort CRPS table compares arms on different, unstated day sets with no cell counts. HIGH / CONFIRMED.**
Location: `docs/accuracy_evaluation_2026-07-06.md:8, 29-40`; `forecast_postproc_backtest.py:212-217, 410-438`.
Response: publish per-cell n; restrict every arm to the intersection of days all arms cover.

### 6.C Trading economics

**C.1 — The production fee path is not the fee schedule the repository documents. CRITICAL / CONFIRMED.**
Location: `fees.py:39-41, 56-82`; call sites `risk.py:154-161`, `execution.py:85-93, 135-143, 180-188, 265-273, 293-301`, `paper.py:58, 252-266, 758-766`, `db.py:3503, 3933, 4161, 4353, 5589`, `monitor.py:410`, `exits.py:90`.
Evidence: `trading/docs/research_yes_no_strategy.md:80-86` states the model is `fee = round_up(0.07 × contracts × price × (1−price))` and "rounds up to the next cent". `quadratic_fee_total` has two paths: `ceil_to_cent(raw_fee)` when `series_ticker is None`, and `_ceil_position_plus_fee` — which rounds `position + fee` up to `FEE_ROUNDING_UNIT = 0.0001`, one **centicent** — when a series is supplied. **Every production call site supplies the series ticker**, so the live decision engine, the execution layer *and* the paper P&L ledger all take the centicent path. `docs/codebase_audit_2026-06-15.md:76` records this as a deliberate June change. No test pins the series path against a known Kalshi fee: `test_bins_and_fees.py:53-58` exercises only the (correct) `ceil_to_cent` path and `:20-22` passes trivially.
Impact: the discrepancy is up to $0.01 per order. Live orders in the current regime are ~3–14 contracts at a modelled after-fee edge of $0.002–0.007 per contract, i.e. $0.006–$0.10 of modelled edge per order. **A systematic under-charge of up to one cent per order is between 10% and 160% of the entire modelled edge.** It affects the approval decision, the execution bar and the recorded paper P&L identically, so the paper record cannot detect it.
**Quantified: the rounding choice alone moves the effective gate boundary by ~0.7 points of price.** Evaluating the live gate at its own LCB ceiling (`min_probability_uncertainty = 0.04` ⇒ `probability_lcb ≤ 0.96`) with the real fee function:

| ask | production fee (centicent) | cost | `edge_lcb` | documented fee (`ceil_to_cent`) | cost | `edge_lcb` |
|---:|---:|---:|---:|---:|---:|---:|
| 0.950 | 0.00340 | 0.95340 | **+0.00660** | 0.0100 | 0.9600 | **±0.00000** |
| 0.957 | 0.00290 | 0.95990 | **+0.00010** | 0.0100 | 0.9670 | **−0.00700** |
| 0.960 | 0.00270 | 0.96270 | −0.00270 | 0.0100 | 0.9700 | −0.01000 |

Under the production path the highest ask that can clear is **0.957**; under the schedule the repository documents it is **0.950**. So the rounding convention is not a rounding detail — it decides whether a whole slice of the traded price range is approved or rejected, and it shifts every `edge_lcb` in the favorite band by roughly 0.007, which is the **top of the entire 0.002–0.007 edge band the live book currently trades**.
Uncertainty: I could not verify Kalshi's current rounding rule from inside the repository. What is certain is that the code and the project's own documentation disagree, and that the disagreement is larger than the edge being traded.
Response: **resolve this before anything else in §8.2.** Verify against a real Kalshi fill receipt; pin the production path with a test against a known value; and until then treat the centicent path as an optimistic assumption and re-derive the 2026-07-27 execution-bar decision under `ceil_to_cent` — on the table above, that decision's entire approved population may sit on the wrong side of the bar.

**C.2 — The maker fee is modelled as exactly zero for weather series. HIGH / CONFIRMED, external verification required.**
Location: `fees.py:10-36, 74-82`; `test_bins_and_fees.py:20-22`.
Evidence: `_MAKER_ONE` excludes `KXHIGH*`, so `fee_multipliers("KXHIGHNY-…") == (0.0, 1.0)` and `quadratic_fee_total(0.5, 100, maker=True, series_ticker="KXHIGHNY") == 0.0` is asserted as correct.
Impact: the entire maker-first strategy's economics rest on this. If Kalshi charges any maker fee on `KXHIGH*`, every resting-entry EV in the repository is overstated. Note the interaction with C.6: because the modelled maker fee is zero and the taker fee is not, the fee-model uncertainty now bears almost entirely on the taker path, which is the dominant live path.
Response: verify against the current published schedule and an actual maker fill; add a dated provenance note beside `FEE_SCHEDULE_VERSION`.

**C.3 — The NO-side lower bound is inflated exactly where the book trades. HIGH / CONFIRMED (live-revalidated).**
Location: `probability.py:276-282`; `risk.py:735-739`.
Evidence: `lower_confidence = max(0.0, p − z·SE − penalties)`. `_side_probability_lcb` for NO computes `1 − p − (p − lcb)`, which when `lcb` clips to zero equals exactly `1 − 2p`. The symmetric-correct value is `1 − p − (z·SE + penalties)`, and the clip *proves* `z·SE + penalties ≥ p`, so the computed NO lower bound is ≥ the correct one. Live confirmation from today's artifact: `KXHIGHTSFO-26JUL27-B68.5` YES shows `p = 0.1214, lcb = 0.0000`; the same bin's NO row shows `lcb = 0.7572 = 1 − 2(0.1214)` exactly.
Impact: the gate the repository calls "the primary real-money defense" is systematically optimistic on the far-tail NO population that dominates the book. A worked case at p = 0.05, n = 100 gives 0.900 instead of 0.887 — two to six times the traded edge, in the flattering direction.
Response: compute the NO lower bound directly as `1 − p − deduction` rather than by reflecting a clipped YES bound.

**Addendum 2026-08-31 — the prescribed direct bound was shipped and reverted; read the settlement evidence before re-attempting.** The `1 − upper_confidence` form landed 2026-08-16 (`c82a67e`, PR #96) and was reverted 2026-08-31 (PR #111) after a settlement replay against canonical CLI truth. The clip this finding calls an inflation artifact is exactly where the book's profit lived: 1,163 of 1,370 pre-8/16 approved NO decisions sat in the affected regime (`p_yes < deduction`) and won **91.2–92.5%** at settlement (+2.8–3.3¢/contract after fees), while the unaffected remainder was near-breakeven (51.2%, +0.6¢). The 92 post-8/16 markets that only the direct bound blocked would have won **91.3%**. Per-side measurement shows the direct bound raised the NO haircut 0.1145 → 0.1455 with YES unchanged and cut approvals 767 → 55; §8.2's "expect the repaired bound to approve fewer trades" framing masked that shutdown as the fix working. Interpretation: at small `p_yes` the additive deduction exceeds the entire YES probability, which measured calibration refutes; the clip prices model risk multiplicatively (YES may be up to double the estimate) and is empirically supported. The symmetric form remains correct as *algebra* — the defect it exposes is the width of the deduction itself, i.e. **A.11**. Any re-attempt must ship together with an A.11-grade band, with a settlement replay, never alone.
Related: this is one of three independent defects in the same bound. See also **A.9** (the band narrows 2.8× when conditioning fails) and **A.11** (the band is not a confidence bound at all). All three push in the permissive direction, and all three sit on the gate the repository calls its primary real-money defense.

**C.4 — The top of the documented favorite band is unreachable. MEDIUM / CONFIRMED (measured).**
Location: `risk.py:62-65, 179-188`; `config.py:428, 338`.
Evidence: `min_probability_uncertainty = 0.04` on live caps `side_probability_lcb` at 0.96, and `edge_lcb = lcb − cost ≥ 0` is required. Evaluated with the real fee function (table in C.1), the highest ask that can ever clear is **0.957**, so the slice **(0.957, 0.97]** of the documented band `[0.70, 0.97]` is unreachable — about 5% of the band's width. Under the fee schedule the repository documents, the ceiling falls to 0.950 and the unreachable slice becomes (0.950, 0.97], about 7%.
Impact: smaller than my working note suggested — this is the top ~1.3¢ of the band, not "the upper half". It still means two configured constants assert different bounds and any analysis reasoning about the 0.96–0.97 band reasons about an empty set.
Response: reconcile the two constants and state which is the real bound.

**C.5 — Joint Kelly discards the fractional and posterior-trust multipliers on every leg it re-sizes. HIGH / CONFIRMED.**
Location: `portfolio.py:177-186, 223-277` (esp. `:252-257`), `:69-86`; `joint_kelly.py:81-135`; `config.py:416`.
Evidence: `joint_kelly_enabled = True` on live. `_joint_resize_directional` calls `joint_kelly_fractions(positions, scenario_probs, total_fraction_cap=cap)` and then sets each leg's spend to `fractions[key] * bankroll` — the **raw growth-optimal fraction**. `fractional_kelly = 0.30` and the posterior-trust multiplier were applied upstream in `risk.py` to produce the per-leg counts, and the joint re-size *replaces* those counts outright; neither multiplier is applied to the joint solution.
Two real mitigations, which bound this and must be stated with it: `total_fraction_cap = max_daily_loss / bankroll`, which for the live profile is `bankroll × 0.08 / bankroll = 0.08` (`portfolio_limits_for_profile`, `:80-86`); and a fail-safe that reverts to the greedy fractional-Kelly sizing if `_worst_case_loss(candidate) > max_daily_loss` (`:274-276`).
A third mitigation, found on re-checking and material enough to change the severity: the account layer independently caps every position at `min(NORMAL_POSITION_CAP, NORMAL_POSITION_PCT × equity)` = `min($30, 3% × equity)` at placement (`account.py:47-56, 105-152`), i.e. **~$30 on the $1,000 bankroll** — far below the portfolio's 8%-per-city budget. So in the current configuration the joint re-size cannot actually produce an oversized order; the $30 placement cap binds first.
Impact: **downgraded to MEDIUM on that basis.** The defect is real and should be fixed — the intended conservatism (`fractional_kelly`, the posterior-trust multiplier) is discarded on the joint path, and the posterior-mean safety valve that is supposed to keep unproven cohorts small is bypassed on exactly the multi-leg events where it matters. But it is currently masked by an unrelated cap, which means it would surface the moment `NORMAL_POSITION_CAP` is raised or the bankroll grows past ~$1,000. That is a latent defect, not an active one. My working note's "roughly 3× the intended stake" was withdrawn for the same reason.
Response: apply `fractional_kelly` and the trust multiplier to the joint solution before the liquidity and worst-case-loss caps — and do it *before* raising the position cap, not after.

**C.6 — The live taker-cross bar is numerically identical to the approval gate it sits behind, and now preempts the maker path unconditionally. HIGH / CONFIRMED.**
Location: `execution.py:68-84, 112-159`; `risk.py:154-188`; `config.py:493, 518`.
Evidence: the approval gate computes `fee = quadratic_fee_per_contract(ask, …, series_ticker=…)`, `cost = ask + fee`, `edge_lcb = probability_lcb − cost`, and rejects below `min_edge_lcb = 0.00`. `_taker_cross_quote` computes the same quantity at the same price with `quadratic_fee_average_per_contract(price, contracts, …)` — whose per-contract fee is *≤* the one-contract fee under centicent amortisation — and rejects below `limit_taker_cross_min_edge_lcb = 0.0`. So the crossing test cannot reject anything the approval gate accepted (given ≥1 contract of depth and ≥$5 notional). Separately, commit `ca81c397` moved the taker attempt from `if not crosses` to unconditional, so the taker quote now preempts the maker quote at every spread width.
Impact: this is a design choice, not a bug — the commit says as much ("this only stops discarding trades those gates already approved") — but its consequence should be stated plainly: **there is now no execution-level EV check distinct from the approval gate.** And because the modelled maker fee is zero (C.2) while the taker fee is not, crossing gives up ≈`0.07·p·(1−p)` per contract versus a maker fill at the same price — 0.0063 at p = 0.90, 0.0033 at 0.95 — which is the same magnitude as the entire modelled edge. Whether crossing is right depends on the maker fill probability: at the observed <20% fill rate a rough EV comparison favours crossing, but narrowly, and **that comparison appears nowhere in the code or the analysis**. See also C.7, which biases the fill-rate measurement that justified the change.
Response: compute and record maker-vs-taker EV per candidate rather than preempting; re-derive the decision under both fee-rounding conventions (C.1).

**C.7 — Stale resting orders are expired *before* the public tape is allocated. MEDIUM-HIGH / CONFIRMED.**
Location: `monitor.py:313-314`; `db.py:5176-5192`.
Evidence: `expired = store.expire_stale_resting_orders()` runs before `filled = _fill_resting_orders_against_live_book(...)`. Orders past `expires_at` are cancelled with reason "15-minute maker TTL expired" and leave the resting set before the allocator sees the tape.
Impact: any tape printed between the last monitor pass and the expiry instant is never allocated. Under the normal 2-minute monitor cadence the exposure is small; under a monitor gap — a deploy quiesce, a missed tick, or the `flock` skip in C.13 — it is the whole gap, and this month contained several multi-hour quiesces. **This biases the measured maker fill rate downward, and that fill rate ("<20%", 46/49 expired on 07-18) is the primary justification for the entire execution-capture release.**
Response: allocate tape first, then expire; and re-measure the maker fill rate afterwards before treating the taker-cross decision as settled.

**C.8 — Cross-city correlation is controlled by a hand-assigned region partition, not by any estimated correlation. MEDIUM / CONFIRMED.**
Location: `account.py:47-80` (constants and `REGION_BY_SERIES`), `:82-152` (`policy_capacity`); `paper.py:738-755` (`_fit_to_account_policy`); `db.py:2006, 2103`; `joint_kelly.py:148-176`.
**Correction to an earlier version of this finding.** I initially reported that nothing bounds same-day exposure across cities. That was wrong, and the correction matters enough to state plainly. `policy_capacity` is enforced on the live placement path and caps, all against current equity: per position `min($30, 3%)`; per **(city, target_date) 5%**; per **(region, target_date) 8%**; live sleeve 16%; **aggregate across all accounts 20%**; plus a 2% daily-loss pause, a 15% drawdown pause, and a halving of the position cap at 10% drawdown. All fifteen cities are mapped to eight regions with no unmapped leakage. So the system *does* have an explicit same-day geographic correlation control, and a real aggregate ceiling. That belongs in §5, and it is why the portfolio-layer numbers in C.9 are not the binding constraint.
What survives: the region partition is **hand-assigned, not estimated**. No cross-city residual correlation is computed anywhere in the repository, so the eight buckets are an unvalidated prior about which cities move together — and a continental ridge spans several of them (texas + southern-plains + southwest is three buckets, 24% of equity, above the 20% aggregate, so the aggregate is what actually binds rather than any correlation reasoning). `joint_kelly` still models only within-event exclusivity, so the *sizing* is correlation-aware inside a ladder and correlation-blind across cities; only the *caps* are geographic.
Impact: MEDIUM, not the tail-risk hole I first described. The book is bounded; what is missing is evidence that the bounds are placed where the correlation actually is.
Response: estimate the cross-city daily-high residual correlation matrix from the EMOS archive (cheap, and it is the input a portfolio-level Kelly would need anyway), and check the eight region buckets against it. Keep the caps regardless.

**C.9 — `PortfolioLimits.max_daily_loss` is a per-(city, target-date) scan cap, not a daily cap. HIGH / CONFIRMED.**
Location: `portfolio.py:69-86` (`portfolio_limits_for_profile`), `:157, 167`; `_cli/scan.py:851-858`; `account.py:53-62, 105-152`.
Evidence: `portfolio_limits_for_profile("live")` returns `max_daily_loss = bankroll * 0.08` — $80 on $1,000 — and `allocate_portfolio` is invoked **inside the per-city, per-target scan path** (`_cli/scan.py:851`), so that "daily" budget is granted once per city per target date, not once per day.
Impact: **downgraded to LOW-MEDIUM.** This is a naming and comprehension defect rather than a risk hole: the account layer's 5% per-(city, target_date) and 20% aggregate caps (C.8) bind well below the portfolio layer's 8%-per-city budget, so the misleading field never becomes the operative bound. But the system now has risk limits expressed at two layers, in different units, with one of them named as though it were the day-level bound when it is not — and the profile-level `max_target_exposure_pct = 0.18` is a third number for the same concept that is also never binding. Anyone reasoning about exposure from `config.py` alone will get the wrong answer.
Response: rename `PortfolioLimits.max_daily_loss` to reflect its real scope, and document the effective bound in one place — the account layer's ladder is the real policy and `config.py`'s exposure percentages are dead numbers for the live profile.

**C.10 — There are two independent daily-loss breakers, at two layers, with different thresholds. LOW-MEDIUM / CONFIRMED.**
Location: `db.py:159-167` — `PAUSE_THRESHOLDS["live"] = (10, -0.35, 0.010)`, i.e. **1.0%** of bankroll, checked in `paper_entry_pause_reason`. And `account.py:61, 102` — `DAILY_LOSS_PCT = 0.02`, i.e. **2.0%** of equity, checked in `policy_capacity` with the message `"2% live-account daily loss pause"`.
**Correction to an earlier version of this finding.** I initially reported this as a documentation error, on the grounds that `SESSION_MEMORY.md`'s repeated references to "the 2% daily-loss breaker" matched no configured value. They match `account.DAILY_LOSS_PCT` exactly; the memory is right and I was reading only the `db.py` breaker.
What survives: the system has **two** daily-loss pauses that fire on different quantities (bankroll vs current equity), at different thresholds (1% vs 2%), in different layers (entry-scan gate vs placement capacity), and nothing in the code or the docs states which is intended to govern or why they differ. The tighter one (1%) will always fire first on a flat bankroll, making the 2% pause — the one the project's own narrative cites — effectively unreachable.
Impact: not a safety hole, but a real comprehension hazard: the number the project reasons with in prose is not the number that will actually stop the book.
Response: state the intended breaker and its layer in one place; make the second one either strictly redundant or delete it.

**C.11 — The daily-loss window is a fixed-PST day for all 15 cities. MEDIUM / CONFIRMED.**
Location: `db.py:152` — `SETTLEMENT_TZ = timezone(timedelta(hours=-8))`, used at `:2074` and `:6167`.
Impact: the breaker's day boundary does not match a non-Pacific city's settlement day, so losses can be attributed to the wrong day and the breaker can trip late.
Response: use each city's own fixed-standard clock, or state explicitly that this is an account-level Pacific-day control by design.

**C.12 — `min_ask_size = 1.0` approves candidates the $5 executable minimum can never place, and this reframes the liquidity-ceiling claim. HIGH / CONFIRMED.**
Location: `config.py:77-81`; `risk.py:114-118`; `account.py:47, 146-149`; `execution.py:149-150`.
Evidence: the risk gate admits a candidate with as little as **1 contract** of displayed ask depth, while `policy_capacity` returns `allowed_spend = 0` with reason `"recommendation below $5 executable minimum"` whenever `requested_spend < MIN_EXECUTABLE_NOTIONAL = 5.0`, and `_taker_cross_quote` separately requires `contracts × cost ≥ limit_taker_cross_min_notional = 5.0`.
Impact, quantified: at a favorite price of 0.85, $5 of notional requires **≥ 6 contracts** of displayed depth. `docs/SESSION_MEMORY.md:79-83` reports a **median displayed ask of 5 contracts** in the current regime. So the median approved candidate is not merely "depth-bound" — it is **below the executable minimum entirely and could never have been placed at any size**. This matters directly to the headline ceiling claim: the "97.4% of live approved candidates are depth-bound … ~$5/day capped at the depth actually shown" figure is computed over a denominator that includes a large share of structurally unplaceable candidates, so both the 97.4% and the $5/day are likely overstated — the $5/day in the *optimistic* direction, since it credits capped fills to candidates that would have been rejected at placement.
Response: raise the liquidity gate to the notional floor (`min_ask_size` such that `ask × min_ask_size ≥ 5`), then re-derive the ceiling. This is a prerequisite for trusting the ceiling analysis, which is otherwise the most credible quantitative work in the repository (§8.3).

**C.13 — Taker fills assume 100% execution at a never-revalidated ask, and the engine assumes it is the only participant. HIGH / CONFIRMED.**
Location: `execution.py:131, 287`; `paper.py:1222-1245`; `_cli/scan.py:145-149, 250-257, 1577`; `models.py:160-201`; `store/schema.py:78-84`.
Evidence: the scan fetches the event snapshot **once per city** and reuses it across up to three rolling targets; `MarketBin` carries no observation timestamp and `market_snapshots` stores only `created_at` plus `raw_json`, so quote staleness at execution is unmeasurable from the journal. Entry takes `floor(min(contracts, ask_size))` of the displayed top-of-book instantly at one price with no impact, no queue competition, and no re-quote.
Impact: the paper record is optimistic relative to reality by an amount that cannot currently be bounded from the data. Combined with C.6 (crossing is now the dominant path) this is the largest paper-vs-real gap in the system.
Response: record the quote observation time on every decision; re-validate the ask immediately before a simulated cross; and treat displayed depth as an upper bound on fillable size, not the fillable size.

**C.14 — `expected_profit` is computed on the pre-clamp size after a taker downsize. LOW-MEDIUM / CONFIRMED.**
Location: `execution.py:131, 344`.
Evidence: `_taker_cross_quote` downsizes to `floor(min(contracts, ask_size))` and returns it as `quote.contracts`; `with_buy_limit` discards that and computes `expected_profit = quote.edge × decision.recommended_contracts`.
Impact: reported expected profit per decision is overstated whenever depth binds — 97.4% of the time by the project's own measurement. `paper._clamp_to_displayed_ask` fixes the *order*, not the recorded expectation.
Response: propagate `quote.contracts`.

**C.15 — Gate provenance, enumerated. MEDIUM / CONFIRMED.**
Of the ~97 `StrategyConfig` fields, the ones with a traceable evidence trail are `min_edge_lcb ≥ 0` (n = 190 negative-LCB trades, 3 wins) and the favorite band (cited to external literature — Burgi, Deng & Whelan, SSRN 5502658). The rest — `fractional_kelly 0.30`, `max_position_risk_pct 0.08`, `max_event_risk_pct 0.12`, `max_target_exposure_pct 0.18`, `min_edge 0.012`, `min_posterior_probability 0.05`, `comfort_edge_block_sigma_mult 0.4`, `max_source_spread_f 10.0`, `min_probability_uncertainty 0.04`, `NONFINAL_OBSERVED_HIGH_SIGMA_F 0.6`, and the entire intraday hour-of-day table — are hand-set, several carrying an explicit in-code "PENDING walk-forward validation" that never resolved (`config.py:363, 404, 451`).

### 6.D Evidence and statistical validity

**D.1 — The rigorous promotion framework is not what governs the live book. CRITICAL / CONFIRMED.** See §4.3. Response: §8.2.1.

**D.2 — Every headline claim comes from analyses that are not in the repository. CRITICAL / CONFIRMED.**
Evidence: the `SESSION_MEMORY.md` headlines are attributable to ~200 untracked throwaway scripts on the build machine, not to any tracked module. `docs/AUDIT-PLAN*.md` is gitignored (`.gitignore:65`).
Impact: the evidence base for current policy is unreproducible; a different engineer cannot check, re-run or extend any of it, and it will not survive a machine change.
Response: any analysis that changes a live parameter must land as a tracked module with a committed result artifact. This is the cheapest high-value process change available.

**D.3 — No multiplicity control in the analyses that accepted or rejected policy variants. CRITICAL / CONFIRMED.**
Evidence: `SESSION_MEMORY.md:94-118` lists roughly ten rejected variants plus several accepted changes, all evaluated on the same short settled window. The repository's own `holm_bonferroni_significant` is used by nothing outside `research_promotion`.
Impact: with a few hundred settled orders over ~10–40 days and dozens of variants tested, the probability that at least one accepted change is noise is high.
Response: apply Holm across the whole family retrospectively, or re-test the accepted changes on data collected *after* they were selected.

**D.4 — The bootstrap p-value is degenerate at this sample size. HIGH / CONFIRMED.**
Location: `research_significance.py:53-62`.
Evidence: `one_sided_bootstrap_p_value` returns the fraction of resample means ≤ 0. A window whose day-level values are all positive returns exactly `p = 0.0`, which passes Holm at any family size.
Impact: the multiplicity control can be defeated by a small all-positive window — precisely the situation in the current record.
Response: floor the p-value at `1/draws` or use a studentised/BCa interval; require a minimum cluster count before the gate can pass.

**D.5 — Day-clustered CIs are percentile bootstraps over ~9–10 clusters. HIGH / CONFIRMED.** See §4.3(3).
Response: report the cluster count beside every interval; treat any interval with fewer than ~25 clusters as descriptive, not inferential.

**D.6 — The promotion bootstrap resamples `(station, day)` as independent clusters and calls the count "independent days". HIGH / CONFIRMED.**
Location: `research_bootstrap.py:99-106, 220-266`; `research_promotion.py:182-198`.
Impact: 15 cities on one day are 15 clusters, not 15 independent observations; same-day cross-city weather is correlated. The ≥30-"independent-days" floor can be met in two calendar days, and the CI is too narrow.
Response: cluster on calendar day, or estimate the effective number of independent days from the cross-city residual correlation.

**D.7 — `backtest_rescore` re-decides the entire journal under the *current* config and is published as validation. HIGH / CONFIRMED.**
Location: `backtest_rescore.py:1-34, 355-411`; `strategy_lab/calibration.py:219-292`.
Impact: pure in-sample re-scoring; it can only confirm the parameters it was given.
Response: label it a diagnostic. It is not a walk-forward and must not be cited as one.

**D.8 — "settlement_truth reproduces realized P&L on 283/283 settled orders" is a self-consistency check. HIGH / CONFIRMED.**
Location: `settlement_truth.py:32-84`; `db.py:85, 5410-5413`; `restatement.py:1215-1300`; `_cli/paper.py:404-462`.
Evidence: the engine settles through `row_resolves_yes` and the reconciliation re-derives the outcome with the same function; the automated `paper-auto-settle` verification compares the booked high to the value it just settled from.
Impact: reproducing your own engine's arithmetic with your own settlement rule is a tautology. Nothing validates against Kalshi's actual published settlement result for the same market.
Response: fetch and store Kalshi's settled `result` per market ticker and reconcile against it. Cheap, and the only thing that can catch a bin-boundary or station misassignment.

**D.9 — CLV, the best low-noise skill metric available here, is used by nothing — and is currently wrong for 15 cities. HIGH / CONFIRMED.**
Location: `clv.py:167-196, 218-253`; `archive.py:1522, 1770-1807`.
Evidence: CLV is computed and archived; no decision path, promotion gate, readiness check or dashboard surface consumes it. Its own docstring calls it "the robust headline number" because it does not depend on a single settlement outcome. But `_authoritative_highs` keys settlement by `target_date` **alone** across 15 cities (last row wins), and `load_order_clv` sums P&L across all seven paper ledgers — so it both mis-resolves settlement and violates the account-separation invariant. It has no test coverage.
Impact: every policy decision is made on realized settled P&L over 10–40 days — the noisiest available signal — while the low-noise one sits unused and broken.
Response: §8.1.3.

**D.10 — The measurement window is the program's best window. HIGH / INFERRED.**
Evidence: the analyses underlying the headline restrict to `created_at >= '2026-07-18'` / `target_date BETWEEN '2026-07-19' AND '2026-07-26'`, and the published performance table at `SESSION_MEMORY.md:417-424` begins at 2026-07-20, the highest day.
Impact: window selection is itself a free parameter and was not controlled.
Response: report the full history alongside any window, always.

**D.11 — The paper program is net negative all-time; only a slice is headlined. MEDIUM / CONFIRMED from tracked statements.**
Evidence: `README.md:52-55` — "The paper book's lifetime result is a small negative". `SESSION_MEMORY.md:441-443` — the legacy shared live account's true all-time realized P&L is **−$41.62**, while the legacy live *strategy attribution* over the same period is **+$45.70**.
Impact: none if the distinction is preserved — and the repository's own safety rules require preserving it. The risk is that the attribution figure is the one that gets quoted.
Response: keep doing what you are doing. This is a genuine strength and the reason this finding is not more severe.

### 6.E Multi-city generalization

**E.1 — The Google integration produces no forecast, no trade and no evidence, and costs ~246 paid API events/day. CRITICAL / CONFIRMED (live-revalidated).**
Location: `forecaster/google_paired_evidence.py:87`, `google_challenger_shadow.py:72`, `google_multicity_refresh.py`, `weatheredge-google-runtime-purge.timer`, `sfo-forecaster-refresh.service.in:46`, `google_weather_cache.py:106-108, 115, 214`, `blend_archive.py:602`.
Evidence: `derive_and_record_paired_evidence` and `run_sfo_google_shadow` are referenced only by their own definitions and their tests. Fetched data lands in a runtime store with a 1-hour TTL which a 10-minute purge timer deletes. `forecast_blend_daily_high` — the only table carrying `google_high_f` — is written only by `google_weather_cache.main()`'s legacy no-`--cities` branch, and systemd always passes `--cities sfo`. Live confirmation: today's published SFO forecast reports all four blend sources as null.
Impact: roughly 10,500 lines of source and tests plus a recurring API bill against ~95% of the daily quota, for a component with no output — and the largest recent investment of forecasting effort went here rather than into calibration verification.
Response: decide explicitly. Either wire the derivation into the refresh cycle *before the TTL expires* and pre-register the evaluation, or decommission the fetchers, the store, the purge timer and the key. Do not leave a paid pipe feeding a purge.

**E.2 — The Google challenger is unavailable exactly on the days it could matter. HIGH / CONFIRMED.**
Location: `google_runtime_blend.py:44-49, 110-122`; `research_candidates.py:371-387`; `research_promotion.py:654-690`.
Evidence: `GOOGLE_CHALLENGER_CORROBORATION_BLOCK_GAP_F = 7.0` marks the challenger unavailable whenever Google disagrees with EMOS by ≥ 7 °F; incomplete coverage then blocks promotion outright.
Impact: the hypothesis is structurally unpromotable — it can only be evaluated on days it carries no independent information.
Response: test the clean question instead (score the Google-derived station-day high against CLI truth, paired against EMOS, with no share/block policy), or redesign the blend policy first.

**E.3 — `config_for_city` varies 2 of ~97 strategy parameters. HIGH / CONFIRMED.**
Location: `config.py:668-686`. Only `emos_distribution_enabled` and `blocked_forecast_cohorts` are city-aware; 14 cities trade on a parameter set fitted to San Francisco. The three constants that actually matter are E.4, E.5 and E.6.

**E.4 — `source_spread_f` silently means two different statistics, gated by the same absolute-degree thresholds. HIGH / CONFIRMED (live-revalidated).**
Location: `models.py:38-64`; `forecast.py:498-501`; `emos_forecast.py:129-136`; `probability.py:126-131, 247`; `risk.py:97-104`; `config.py:269, 461`.
Evidence: for SFO's legacy blend it is the range across ≤ 4 point sources; for the EMOS path it is the range across up to 8 NWP models. `E[range] ≈ 2.06σ` for k = 4 and `≈ 2.85σ` for k = 8 — mechanically ~38% larger at equal true uncertainty. The same absolute thresholds (3.0 °F sigma widening, the 0.08 LCB penalty ramp, `max_source_spread_f` 6.0/10.0) apply to both. **Live confirmation:** today's published SFO rows report `source_spread_f = 10.1` (07-27) and `12.3` (07-28) against `max_source_spread_f = 10.0`, and **every SFO decision on both target days is rejected** with "forecast source spread exceeds max; point blend is unreliable". Both the sigma widening (capped at ×1.35) and `model_risk_penalty` (0.0698 of its 0.08 cap) are simultaneously saturated.
Impact: high, and currently binding in production — the flagship city is switched off by a threshold calibrated for a different statistic.
Response: express the threshold in units of the day's own EMOS sigma, or set a separate NWP-range threshold calibrated to the 8-model distribution.

**E.5 — The comfort-edge band is scaled by the cross-model range while the calibrated per-city EMOS sigma sits unused in the same snapshot. HIGH / CONFIRMED.**
Location: `_cli/scan.py:399` (`"forecast_sigma_f": forecast.source_spread_f`), `backtest_rescore.py:410`, `tail_basket.py:129, 149`, `risk.py:649`.
Evidence: the parameter named `forecast_sigma_f` and documented as "a multiple of the day's forecast sigma" is fed a *range*. The true EMOS sigma is present in the same snapshot (`forecast.raw["emos"]["sigma"]`) and is passed separately as `emos_mu_sigma`.
Impact: the block band and the far-tail size boost — the rule that concentrates the book — are scaled by an uncalibrated statistic that also scales differently between SFO and the other 14 cities.
Response: pass the EMOS sigma. One line, direct effect on sizing — re-validate, do not ship blind.

**E.6 — The intraday diurnal model is a table of SFO constants applied to all 15 climates. HIGH / CONFIRMED.**
Location: `probability.py:577-599, 602-626, 629-648`.
Evidence: `_climatological_remaining_heat_f`, `_intraday_sigma_f` and `_intraday_blend_weight` are hard-coded hour-of-day tables, unchanged since before the multi-city expansion.
Impact: San Francisco's marine-layer diurnal curve is applied to Phoenix in July, Denver, Miami and Seattle. "12 °F still to come before 06:00" and "0.15 °F after 18:00" are climate-specific numbers.
Response: fit per-city (or per-climate-region) tables from `dataset_station_observations`, which already holds the hourly data.

**E.7 — SFO's intraday clock is civil Pacific while its settlement clock is fixed standard. LOW-MEDIUM / CONFIRMED.**
Location: `config.intraday_timezone_for_city:16-27`. Deliberate and documented as a backward-compatibility exception; a one-hour offset all summer between the hour-of-day table and the settlement day, in the flagship city.
Response: fix during the per-city refit in E.6; not worth a standalone change.

**E.8 — Per-city trading performance is never bucketed, measured or gated. HIGH / CONFIRMED.**
Location: `backtest_rescore.py:498-640, 740-945`.
Impact: with 15 cities and one shared parameter set, a systematically unprofitable city is invisible. The cited per-city permutation test (p = 0.538) is an underpowered null on ~10 days, not evidence of homogeneity.
Response: add per-city P&L, CLV and calibration to the scorecard before adding any more cities.

**E.9 — The production forecast for all 15 cities is a fail-soft subprocess launched from inside the Google research fetcher, and a total failure reports success. HIGH / CONFIRMED.**
Location: `google_multicity_refresh.py:140-168, 326`; `emos_forecast.py:486-487`; `sfo-forecaster-refresh.service.in:37-46`.
Evidence: `_default_archive_baseline` shells `emos_forecast.py --serve-rolling --cities all` with `capture_output=True`, wrapped in a bare `except Exception` that returns a failure record and never re-raises. `emos_forecast.main` returns 0 on `--serve-rolling` even when `served == 0` — "fail loud only when an explicit single `--serve` produced nothing".
Impact: the only production forecast in the system is a side effect of a component that produces nothing (E.1), with stdout/stderr discarded, a bare except swallowing every exception class, and a success exit code on total failure. **This is the highest-consequence silent-degradation path in the system.**
Response: restore `emos_forecast.py --serve-rolling --cities all` as its own `ExecStart` without the `-` prefix so failures alert; make `served == 0` a non-zero exit on the rolling path; stop discarding the subprocess output.

### 6.F Settlement, accounting, data and operations

**F.1 — A position settled against a value the NWS later revises is never corrected. HIGH / CONFIRMED.**
Location: `db.py:5316-5330, 5455-5468, 5476-5555`. `settle_paper_orders` skips any row with `settled_at IS NOT NULL`; `verify_paper_settlements` is explicitly non-mutating. CLI values *are* revised; booked P&L, ledger and equity stay permanently wrong, silently.
Response: allow a supervised re-settlement path keyed on `is_final` transitions, with an audit trail.

**F.2 — Settlement "finality" is a calendar condition, not a provenance condition, so exact 0/1 posteriors are created from station observations. HIGH / CONFIRMED.**
Location: `nws_ground_truth.py:195-231` (esp. `:207-208`); `forecast.py:255-281`; `probability.py:177, 346-387, 431-438`; `risk.py:76-86`.
Evidence, traced end to end:
 (a) `nws_daily_high_ground_truth` is built **from `nws_station_observations`** and stamped `source = "NWS station observations"` (`nws_ground_truth.py:195-231`).
 (b) `is_complete = 1 if local_date < now_local_date else 0` (`:207-208`) — completeness is set purely because the local calendar day has ended. Nothing checks that the official CLI value was obtained.
 (c) `forecast.py:260-268` sets `IntradaySnapshot.is_complete` from that flag and takes `observed_high_f` from that observation-derived row.
 (d) `probability.py:177` sets `observed_high_is_final = bool(intraday.is_complete)`, and `_condition_on_observed_high(..., is_final=True)` (`:370-382`) then **hard-zeroes bins and can create an exact 1.0 point mass**; `probability.py:431-438` independently returns a full-weight point mass on the same flag.
 (e) `risk.py:76-86`, the `nonfinal_certainty_gate`, fires only when `observed_high_is_final is not True` — so the guard is switched **off** exactly here.
Impact: once the local day ends, a station-observation-derived high is treated as the settlement value **with certainty**, bypassing `NONFINAL_OBSERVED_HIGH_SIGMA_F = 0.6` and the certainty gate. This is precisely the audit MD-01 failure mode those controls were built for (raw 87.8 °F settled 87 °F, order 188, −$11.63), and `clv.py`'s own docstring warns "the observation high runs a few degrees below the CLI value … enough to flip a bin". There is also no range/spike/persistence QC anywhere in `nws_ground_truth.py`, so a single bad METAR produces false certainty.
Note: an adversarial reviewer argued this path was unreachable. It is reachable — the reachability condition is a calendar comparison, not the arrival of CLI truth.
Response: gate `is_final` on the *provenance* of the value (true only when it came from the CLI product with `is_final=1`), not on the calendar. Keep the observation-error model on every non-CLI value. The constant 0.6 is itself unvalidated and should be fit per station.

**F.3 — SFO's calibration record is scored against observed station maxima, not the CLI settlement, and is frozen. HIGH / CONFIRMED.**
Location: `forecaster/research/features.py:233-235`; `forecast.py:587-602`; `ab_test_results.json` (no regeneration timer, §6.B.4).
Impact: SFO's residual bias and sigma are calibrated against the wrong truth, from a frozen artifact. Response: fold into A.8.

**F.4 — `paper-buy` writes ungated orders straight into the Live Stability ledger. HIGH / CONFIRMED.**
Location: `db.py:4323-4386`; `_cli/paper.py:194`. Manual orders that passed no gate publish as "live" performance.
Response: tag manual orders and exclude them from published performance and readiness.

**F.5 — Live Kelly sizing and every percentage risk cap are driven by a P&L sum spanning the archived shared ledger and Live Stability. HIGH / CONFIRMED.**
Location: `_cli/scan.py:198-209`; `db.py:6098-6128`; `config.py:468` (`size_against_live_equity = True`).
Impact: exactly the cross-account contamination the repository's own safety rules forbid — and it feeds *sizing*, not just reporting.
Response: scope the equity query to the active account.

**F.6 — Published readiness pools the archived shared account with Live Stability into one $1,000 equity curve. HIGH / CONFIRMED.**
Location: `replay.py:49-51, 868-978`. Response: same as F.5.

**F.7 — The published replay/readiness cohort is filtered to restatement-VERIFIED orders. HIGH / CONFIRMED.**
Location: `replay.py:586-600, 1078-1110`; `restatement.py:1219-1226`.
Impact: a settlement mismatch removes a trade from the published record — a selection filter that plausibly correlates with the trades most worth seeing.
Response: report the excluded count and its P&L alongside every readiness figure.

**F.8 — The retention blocker is architectural, and the recommended remediation would not fix it. HIGH / CONFIRMED — RESOLVED 2026-07-27.**
Location: `archive.py:931-1043` (with `:483-489`, `:782-803`); `db.py:5946-6060` (esp. `:5975-5988`, `:614-636`); `store/schema.py:519-528`; `run_archive_then_prune.sh:37-42`; `backup_paper_db.sh:96`.
Evidence, four independent cost sources in the same 30-minute window:
 (a) `gate_missing_days` re-verifies **every** UTC day from journal genesis on every run — its own docstring says so ("this reaches back to journal genesis") — and because approved rows are never deleted (`db.py:5961`) the oldest surviving row never ages out, so the day loop grows by one day forever. Each (table, day) iteration issues two queries predicated on `created_at >= ? AND created_at < ?`: `_source_day_count` (`:483-489`) and `_surviving_ids_are_covered` (`:782-803`), the latter iterating **every row** in the day. I audited index coverage for a leading-`created_at` range scan on the six `STREAM_TABLES` (`store/schema.py:519-533`): `probability_snapshots` has only `(target_date, market_ticker, created_at)`, `paper_monitor_snapshots` only `(order_id, created_at)`, `market_snapshots` only `(target_date, created_at)`, `scan_context_snapshots` only a partial index on `source_context_hash`, and `forecast_snapshots` has **no index at all** — in none of these is `created_at` the leading column, so every one of those per-day checks is a full table scan. Only `decision_snapshots` can be served, and only via `idx_decision_snapshots_created_market`, which `store/schema.py:549-553` deliberately does *not* create on existing journals (it is built once out-of-band by `create_decision_snapshot_index.sh` with the scanners paused), so whether it exists in production is an operational fact I could not verify. Net cost is O(days × table size) and grows by roughly ten additional full scans per calendar day. **This is why a larger `TimeoutStartSec` cannot be the fix: whatever value you choose is exceeded again a few weeks later.**
 (b) **Three** of the seven statements carry an unbounded whole-table anti-join, not one. The dedup `DELETE` filters candidates to a date window, but its subquery — `id NOT IN (SELECT MAX(id) FROM decision_snapshots GROUP BY market_ticker, side, target_date)` — has **no date predicate**, so it groups the entire 909k-row table every night; no index supports that GROUP BY (`idx_decision_snapshots_market` is `(target_date, market_ticker, created_at)` — wrong order, no `side`; `idx_decision_snapshots_pre_entry` is partial; `idx_decision_snapshots_created_market` lacks `side`). The **`forecast_snapshots`** delete (`db.py:6023-6035`) and the **`market_snapshots`** delete (`:6037-6049`) then do the same thing again: `id NOT IN (SELECT … FROM scan_context_snapshots UNION SELECT … FROM decision_snapshots)`, materialising a UNION across the whole decision journal. Verified against the live database: **there is no index on `decision_snapshots.forecast_snapshot_id` or `.market_snapshot_id`**, and **`forecast_snapshots` carries no index at all** — its live index list is empty. Each of those two statements is therefore a full scan of a 909k-row table plus a full scan of its own target. Together with (a), this is what produces 21 minutes of wall clock against 5 minutes of CPU.
 (c) `paper-check-foreign-keys` runs a whole-database `PRAGMA foreign_key_check` in the same window.
 (d) The prune is **one all-or-nothing transaction** with no `LIMIT`, batching or resumability, so a timeout commits nothing and the next run is strictly slower.
And separately: there is **no `VACUUM` anywhere in the repository**, so even a fully successful prune cannot shrink the file and therefore cannot clear the deploy preflight, which needs 2× file size + 1 GiB. Measured on the live database today: `page_size = 4096`, `page_count = 2,646,891`, **`freelist_count = 0`**, `auto_vacuum = 0`. Zero free pages means the ordering is load-bearing and both halves are required — a `VACUUM` run *now* would reclaim nothing, and a successful prune *without* a following `VACUUM` would leave the file at its current size. Rows must be deleted **and then** the file truncated. The prunable population is already queued: `decision_snapshots` holds **909,368 rows** spanning **2026-06-10 → 2026-07-27**, of which **433,148** are neither `approved` nor `signal_approved` and are therefore deletion candidates under the current policy.
**Live confirmation (read-only production check, 2026-07-27).** `sfo-kalshi-paper-prune.service` is in `failed` state — the only failed unit on the box — and the last run's journal reads: archive/S3 upload steps all completed by 02:29:01, then `start operation timed out. Terminating.` at 02:50:56, `Failed with result 'timeout'`, with `Consumed 5min 12.406s CPU time, 1.1G memory peak`. Two things in that line are diagnostic: it burned **21 minutes of wall clock against only 5 minutes of CPU**, i.e. it was **I/O-bound, not compute-bound** — the signature of unindexed full scans against a 10.8 GB file rather than of expensive computation; and its **1.1 GB memory peak sits right at the `MemoryHigh=1200M` throttle**, consistent with materialising the large `NOT IN` id set from (b). Current file size is **10,841,407,488 bytes (10.84 GB)**, and the failure lands after archiving, exactly where `SESSION_MEMORY.md:16-47` hypothesised.
Impact: this is the blocker described at `SESSION_MEMORY.md:16-47`, which has cascaded into three failed deploy preflights — and it is **still failing today**. It is also, per §12, the gating item for any further deployment.
Response: bound the dedup subquery to the same window (strictly safe — it can only retain extra rows, never delete more); add an index on `(market_ticker, side, target_date, id)`; bound `gate_missing_days` to a rolling horizon; batch the deletes with a `LIMIT` and make the job resumable; and add a `VACUUM` (or `auto_vacuum=INCREMENTAL`) step. **Do not raise `TimeoutStartSec` as the fix** — the memory's own step (2) was the right instinct and this confirms it.

**RESOLVED 2026-07-27** — PR #73, commit `d1d2d6eb`, all three CI checks green; run against production the same day. Two parts of the response above needed correcting once implemented:

1. **The recommended index does not work.** `(market_ticker, side, target_date, id)` cannot serve the dedup step, for two reasons found by measurement. (a) The query groups by `COALESCE(risk_profile, '')` once F.9's key change is applied, and SQLite will **not** match a plain-column index to an expression — it silently falls back to `USE TEMP B-TREE FOR GROUP BY`, the very full-table sort the index was meant to remove. (b) More fundamentally, the `id NOT IN (SELECT MAX(id) … GROUP BY …)` **shape** falls back to a temp B-tree *whatever* index is offered, because SQLite will not walk a group-ordered index for the grouping while a selective `created_at` range is also in play. Indexing alone cannot fix it. The dedup test was therefore re-expressed as "a newer row exists in this group" (`EXISTS … AND n.id > d.id`) — an equivalent predicate that *is* index-seekable. That temp B-tree was the 1.1 GB memory peak.
2. **Leading the new index with `market_ticker` steals another plan.** It outranked the covering `created_at` index for the cities-report `GROUP BY market_ticker` aggregation, turning that report's date-range seek into a full index scan on the Strategy Lab path — caught by `test_cities_report.py`. Leading with `target_date` fixes it at no cost, since the probe constrains all four group keys by equality.

Ten indexes were added, including the `scan_context_snapshots` foreign-key columns — the original response missed that **both** referencing tables are probed, not just `decision_snapshots`. All seven retention `DELETE`s now have index-supported plans with no temp B-tree and no full scan, asserted by a test that captures the statements the prune actually issues.

**Measured on production (2026-07-27, timers quiesced):** index build 559s for all ten plus `ANALYZE`; rejected-arm export (F.9 / §8.1.1 precondition) 436,998 rows in 97s; **prune 767s** deleting 334,328 deduped + 72 beyond-45d + 16,676 contexts + 2,382 probabilities + 2,128 monitor snapshots + 397 orphan forecasts + 368 orphan markets, with **approved rows untouched** (477,770 before and after); `VACUUM INTO` 499s. File **10,864,291,840 → 9,614,307,328 bytes** (1.41 GB reclaimed).

At the measured rate the nightly incremental run — roughly one day of the 47-day backlog — is on the order of tens of seconds, against a 1800s budget it previously exhausted. `TimeoutStartSec` was **not** raised.

A `VACUUM` was added as `deploy/aws/compact_paper_db.sh` rather than a step in the nightly chain: that chain already nearly exhausts its budget, and a multi-minute exclusive-lock rewrite inside it would guarantee the timeout this work removes. It uses `VACUUM INTO` so the original stays readable and intact until a compacted copy has passed `integrity_check`, `foreign_key_check` and row-count parity across all 29 tables.

**Not done here:** bounding `gate_missing_days` to a rolling horizon (item (a) of the evidence above). The new leading-`created_at` indexes on all six `STREAM_TABLES` remove the full scans that made each per-day check expensive, but the day loop still grows by one day forever and should still be bounded.

**F.9 — Fixing the prune will silently destroy evidence unless three things are changed first. HIGH / CONFIRMED.**
Location: `db.py:5977-5988, 5998-6009`; `store/scoring.py:144-165, 363-374`; `archive.py:60-83`.
Evidence: (a) the dedup `GROUP BY (market_ticker, side, target_date)` omits `risk_profile`/`account_id`, while every analysis sampler partitions **by profile** — so one book's end-of-day rejection evidence disappears; (b) pruning `scan_context_snapshots` collapses the walk-forward research case population to approved-linked observations; (c) nine tables — including the entire pre-registration/paired-evidence spine and the fill-attribution ledger — are outside `STREAM_TABLES`/`FULL_TABLES` archive coverage, so they are not protected by the archive gate at all. Separately, rejected `decision_snapshots` rows beyond `SFO_PRUNE_DEDUP_DAYS=45` are deleted — and those are exactly the population the "measured and REJECTED" analyses depend on.
Response: **extract and commit the model-vs-market evidence table (§8.1.1) before fixing the prune.** Then add `risk_profile` to the dedup key, exempt `source_context_hash`-linked contexts, and extend archive coverage.

**PARTIALLY RESOLVED 2026-07-27.** `risk_profile` is now in the dedup key (PR #73). The rejected arm was exported before the first successful prune, per this item's own escape clause: `rejected_arm_20260728T002437Z.csv.gz`, **436,998 rows**, sha256 `7256f84267aacb34076148745bab41ec073c4cfa49fa3065fe2706854bbdea37`, held both on the box and off it. 100% carry `model_probability` and 99.97% `market_probability`, and the split is **live 236,348 / research 200,650** — which is the concrete measure of what the old profile-blind key would have destroyed: roughly one book's entire end-of-day rejection record. Still open: exempting `source_context_hash`-linked contexts, extending archive coverage to the nine uncovered tables, and §8.1.1 itself (the scoring analysis; the data it needs is now preserved).

**F.10 — Degradation is systematically invisible to the evidence record. HIGH / CONFIRMED.**
Location: `_cli/scan.py:1586-1591, 1625-1631, 1753-1758, 1798-1802`; `run_paper_scan_profiles.sh:12-19`; `check_forecast_db_freshness.sh:51-62`; `send_systemd_failure_alert.sh:6-11`; `sfo-weather.env.example:14`.
Evidence — four independent paths, each read verbatim:
 (a) All four scan-skip handlers (stale forecast, unavailable calibration, no Kalshi event) do exactly `print(color.yellow(...), file=sys.stderr)` followed by `continue`. **No row is written to any table**, so nothing downstream can know the city-day was skipped.
 (b) A scan in which **zero of fifteen cities** produced a target still returns success: `if not scanned_any: print("no city produced an analyzable target"); return 0` (`:1630-1632`), and the placement path returns `1 if fatal_containment else 0` (`:1803-1805`), i.e. 0 when nothing scanned.
 (c) The `flock` skip is `echo "previous paper scan still running; skipping this tick"; exit 0` (`run_paper_scan_profiles.sh:15-18`), so systemd records a lost tick as success and `OnFailure` never fires.
 (d) `SFO_FRESHNESS_ALERT_URL` ships **empty** (`sfo-weather.env.example:14`, installed verbatim by `install_systemd.sh`) and `send_systemd_failure_alert.sh:8-11` is `if [[ -z "$ALERT_URL" ]]; then echo "warning: … was not sent" >&2; exit 0; fi` — so even when `OnFailure` does fire, the alert chain is a no-op under the shipped configuration.
 (e) The freshness watchdog reads `weather.db` **file mtime** (`check_forecast_db_freshness.sh:55`), which other `ExecStart` steps in the same unit refresh every tick, so a per-city or per-table freeze is invisible to it.
Impact: this is the worst class of defect for a research system — the failures that bias performance statistics are precisely the ones that leave no trace. There are three independent ways for a completely unproductive scan cycle to be recorded as a success with no database row and no alert. If skips correlate with weather regime — plausible, since source disagreement, stale feeds and missing Kalshi events all cluster on active synoptic days — then **every performance statistic in this system is silently conditioned on quiet weather**, and that conditioning cannot be measured after the fact because the skips were never recorded. This is the finding that most limits what any future analysis can establish.
Response: write a skip row with a reason code for every non-scan (this is the precondition for trusting any later performance number); make a zero-target scan and the flock skip exit non-zero or emit a counter; give the freshness watchdog a per-table `MAX(created_at)` check rather than file mtime; set the alert URL.

**F.11 — No NWP run/initialisation time is recorded anywhere. MEDIUM-HIGH / CONFIRMED.**
Location: `emos_forecast.py:212-262`; `nwp_archive.py:58-67`.
Evidence: the live fetch requests only `daily=temperature_2m_max` and reads back the values; no run/reference/init time is stored.
Impact: the 5-minute scan cadence cannot be validated against model-update times, and "how stale was the forecast behind this decision" is unanswerable. A system that trades a forecast should know which model cycle produced it.
Response: record the model run time per source; it is available from the API and is one column.

**F.12 — `forecast_emos_daily_high` is last-write-wins. MEDIUM / CONFIRMED.**
Location: `emos_forecast.py:66-81, 351-372`. `PRIMARY KEY (station_id, target_date, lead_days, source)` excludes `fetched_at`, and the serve runs ~38×/day with `INSERT OR REPLACE`.
Impact: bounded — trading replay is safe because `forecast_snapshots` on the trading side is append-only and carries the raw EMOS payload (§5.6). What is destroyed is the forecaster-side time series of intraday EMOS revisions, which is exactly the data the hours-to-settlement sigma study (A.5, §8.1.2) needs.
Response: add `fetched_at` to the key, or write a separate append-only serve log.

**F.13 — A recording-regime change inside the retention window corrupts every per-day rate. MEDIUM / CONFIRMED.**
Evidence: `SESSION_MEMORY.md:130-136` documents that `decision_snapshots` recording changed twice (research full-ladder 07-19, live 07-24: 43 → 64,872 rows/day). The memory correctly warns about approval *rates*; the same hazard applies to every raw-tick counter — `summary._decision_stats` (`summary.py:670-806`) builds `per_day['signals'] += 1`, `['approved'] += 1` and the rejection-reason tallies straight from row counts, and `strategy_lab/calibration.py:511-55x` does the same.
Response: store a recording-regime version column so the boundary is queryable rather than remembered.

**F.14 — The suite is green and deterministic; the apparent failures were a local file-descriptor limit. MEDIUM / CORRECTED 2026-07-27.**
Location: `trading/tests/test_deploy_shell_behavior.py:1098-1140` (`test_real_legacy_editable_upgrade_leaves_one_owner_and_console_script`); `scripts/run_tests.sh`.
Evidence, measured on the build machine at HEAD:
 (a) That test performs a **real network package install** — `python -m pip install --require-hashes -r <tmp>/requirements/production.lock` into a pytest tmpdir — observed running live in `pgrep`. It carries **no network marker and no skip guard**; the only `skipif`s in that file gate on the `sqlite3` CLI. So every full-suite run resolves and downloads the production dependency set.
 (b) Other tests shell out to real `systemctl` (`… /systemctl is-enabled --quiet sfo-forecaster-refresh.timer`, also observed live), so results depend on host service state.
 (c) **This item's original conclusion was wrong, and is retracted.** I first measured ~27 failing tests on clean `main` with a shifting failure set across runs, and concluded the suite was non-deterministic and could not serve as a regression gate. That conclusion does not survive checking. The root cause is `PaperStore.connect` (`db.py:584-598`), which returns a **bare `sqlite3.Connection`**: `with self.connect() as conn:` commits or rolls back on exit but **never closes**, so every call leaks a file descriptor. The build Mac's default `ulimit -n` is **256**. Once a full run exhausts that, whichever tests happen to be executing fail — which is exactly why the failing set moved between runs and why it clustered in the most connection-hungry module (`test_strategy_research.py`, 22 of ~30 both on `main` and on the branch).
 Re-measured with the limit raised, one run each, sequentially on the same host:

| tree | `ulimit -n 256` | `ulimit -n 8192` |
|---|---|---|
| `main` @ `d16448cf` | 24 failed, 6 errors | **2141 passed, 8 skipped, 0 failed** |
| `fix/retention-prune-and-vacuum` | 29 failed, 2 errors | **2150 passed, 8 skipped, 0 failed** |

 The branch's +9 are the tests added with that fix. GitHub Actions has been **green on every recent `main` push** (runs 30289800225, 30289029915, 30253639126, …, ~2m35s each), which is consistent: Linux runners do not have a 256-descriptor limit, so CI never hit the leak. **A trustworthy regression gate does exist** — CI, and locally any run with a raised descriptor limit.
Impact: much smaller than first stated, but not zero. The tracked "suite green" claim **is** verifiable, and the §11 program does have a regression signal. What remains real is (a) the descriptor leak itself, which is a genuine latent defect — a long-lived process that opens many connections will exhaust its own limit — and (b) the network-installing and `systemctl`-dependent tests, which make a local full run slow and host-dependent even though they do not make it non-deterministic. The retention fix was subsequently certified regression-free at full-suite level on this basis: `main` 2141 passed / branch 2150 passed, zero failures on both, plus all three CI checks passing on PR #73.
Response: **DONE 2026-07-28.** The descriptor leak is fixed at the class level, not just in `PaperStore`. `sfo_kalshi_quant/_sqlite.py` provides a `ClosingConnection` whose `__exit__` commits *and* closes, and all **33** `with sqlite3.connect(...)` sites across 12 modules now use it, along with `PaperStore.connect`. The nine bare-assignment sites are deliberately untouched — several bind a connection and then re-enter it with a nested `with conn:` transaction block, where closing on exit would end the transaction early.

Measured effect at the **default** `ulimit -n 256` on the build Mac:

| | failures at `ulimit -n 256` |
|---|---:|
| before | ~30, shifting between runs |
| `PaperStore.connect` fixed only | 1 |
| whole class fixed | **0 — 2156 passed, 8 skipped** |

So the workaround is retired: the suite no longer needs a raised descriptor limit, and a local run can no longer manufacture failures that look like flaky tests. Green baseline is now **2156 passed, 8 skipped** at stock limits. Still outstanding and unchanged: one test performs a real network `pip install` and others shell out to real `systemctl`, which makes a local full run slow and host-dependent — but not non-deterministic. None of this blocks the §11 program.

### 6.G Claims, documentation and the dashboard

**G.1 — The public headline forecast-accuracy claim advertises the LSTM's point-forecast MAE for a role the LSTM does not perform. HIGH / CONFIRMED.**
Location: `README.md:24-33`; `src/lib/diagnostics.ts:35`; `src/components/charts/ModelCompareChart.tsx:13`; `MethodologyView.tsx:26-32`.
Evidence: "**LSTM** (production) | **3.3 °F**", presented as the system's forecast accuracy. The LSTM is **not** the production point forecast — SFO's live point forecast is the EMOS mean (§6.A.8, live-revalidated). It *is* in production, but in a different role: as SFO's residual-calibration source, supplying the `bias` and `sigma` of the traded distribution (`cli.py:386-393`, `monitor.py:50-51`, `forecast.py:604-628`). So the advertised number measures the LSTM's ability to predict a temperature, which is not what it does in production, while the quantity that *is* production-critical — the calibration of the residual distribution it supplies — is not published at all. Additionally the record behind it is scored against station observations rather than CLI settlement (F.3), from a fixture ending 2026-05-18 (B.4), and the README's figures (3.3 / 3.9) do not match the artifact they derive from (3.123 / 3.71).
Note: an earlier draft of this finding said the LSTM was in no production path. That was too strong and an adversarial review correctly refuted it; the corrected statement is above.
Impact: the most prominent quantitative claim about the system describes a role its subject no longer performs, with a stale number and the wrong truth source.
Response: publish a CLI-scored, vintage-stamped EMOS number per city for point accuracy, and publish the residual-model calibration (§8.1.2) as the claim that actually matters. Relabel the LSTM's role honestly.

**G.2 — The public methodology page advertises a Google blend the pipeline no longer produces. HIGH / CONFIRMED.**
Location: `ForecastPipeline.tsx:34, 150`; `MethodologyView.tsx:86`; `Hero.tsx:44`; `PipelineStepper.tsx:7`. See E.1.
Response: describe the shipped pipeline; move the blend to a clearly labelled retired section; remove or gate the Google budget tile.

**G.3 — Pooled headline metrics hide that the operationally dominant regime is weak. HIGH / CONFIRMED.**
Evidence: the published cohorts show `warm_70_79f` with top-bin accuracy 0.1231 (n = 65) against `normal_60_69f` at 0.6331 (n = 139), while the headline reports a single pooled 56.1%. Warm is the dominant summer regime and the one live was unblocked to trade.
Response: publish per-cohort beside pooled — noting that the cohort split is itself currently indexed on the realized outcome (B.1).

**G.4 — Every published cohort has `brier_skill: null`, yet a readiness gate reads it. MEDIUM / CONFIRMED (live-revalidated).**
Location: `backtest_rescore.py:862-869`; live artifact `calibration.cohorts`.
Evidence: the readiness condition evaluates `skill is not None and skill > thresholds.min_cohort_brier_skill`; the published value is null for all four cohorts, so as published that condition can never pass. Consistent with readiness sitting at 5 of 12.
Response: fix the producer or remove the check. A permanently unsatisfiable gate is indistinguishable from a broken one.

**G.5 — The env-file documentation of the fill model is stale and understates it. LOW / CONFIRMED.**
Location: `sfo-weather.env.example`, `PAPER_ENTRY_MODE` comment — "fills are simulated when the visible ask crosses the limit — a stated proxy, not exchange truth". The actual model is `maker_fills.py` exec-v4 (§5.4), which is considerably better.
Response: update. This one is a strength being under-sold.

**G.6 — The pre-registration document that ~2,500 lines of code cite as authority is not in the repository. HIGH / CONFIRMED.**
Location: `forecast.py:107` and ~40 other comments cite `docs/superpowers/plans/2026-07-17-multicity-google-runtime-weather.md`; no such path exists, tracked or untracked, and no ignore rule covers it.
Impact: for a component whose defensibility rests on pre-registration, the pre-registration artifact is unavailable.
Response: commit it, or rewrite the comments to stand alone.

**G.7 — Three prior-audit items marked closed are not. MEDIUM / CONFIRMED.**
Evidence: `docs/codebase_audit_2026-06-15.md:76` lists "1-contract fee rounding mismatch (`fees.py:39-41` rounds to a centicent)" as **Fixed** — that change is C.1. The same document lists the hourly-rows A/B defect as **PARTIAL**; it is unchanged (B.8). `:60` lists "Lower-confidence bound ~3× too confident — **FIXED**" via `se_sample_n`; the NO-side reflection defect (C.3) in the same code path was never addressed.
Response: re-open all three.

---

## 7. Questions this repository currently cannot answer

State these as open. Do not let them be answered by assertion.

1. **Does the model beat the market?** No measurement exists. The data to answer it is stored today; the rejected arm is on a 45-day clock (F.9).
2. **Is the served predictive distribution calibrated?** Every calibration number describes the rolling-origin archive, on a static SFO ladder, without the market blend, from a frozen LSTM residual record. The `source='live'` rows the trader consumes have never been scored (B.5).
3. **How fat are the EMOS tails?** Requires the rolling-origin archive in `weather.db` (EC2-only). §4.2 establishes the phenomenon for one model; it does not establish the magnitude for the served one.
4. **Do the 14 non-SFO cities beat climatology?** Never measured (B.6).
5. **Is the after-fee edge real at the current bar?** Undeterminable while the fee model and the documented schedule disagree by an amount comparable to the edge (C.1), and while the lower bound is inflated in the traded population (C.3).
6. **What is the true maker fill rate?** The measured "<20%" is biased low by the expire-before-allocate ordering (C.7), and it is the number that justified the execution-capture release.
7. **Would walking deeper into the ladder lift the liquidity ceiling?** Correctly identified as unanswerable by the project itself. The depth-capture instrumentation (PR #71) is the right response; it is merged and undeployed.
8. **Is the paper record a fair proxy for a real record?** Partly. The maker-fill model, exit pricing and per-leg fee accounting are honest. Against that: the fee rounding (C.1), the assumed-zero maker fee (C.2), the take-all-displayed-depth-instantly-at-one-price taker model with no quote revalidation (C.13), and the VERIFIED-only publication filter (F.7) all lean optimistic, by an amount that cannot currently be bounded from the stored data.
9. **Was the 2026-07-27 execution-bar change an improvement?** It was selected and evaluated on the same 20 days (§4.3). It may well be right; the evidence does not establish it.
10. **Do scan skips correlate with weather regime?** No skip rows are written (F.10), so this cannot be tested — and if they do, every performance statistic is conditioned on quiet weather.

---

## 8. What to do, in order

### 8.1 Do these three first

Cheap, they use data you already have, and each can invalidate a policy the book is running on.

**8.1.1 — Score the model against the market on settled outcomes.** *(≈1 day. The single highest-value piece of work in the repository.)*
From `decision_snapshots`, join `model_probability`, `market_probability` and the blended `probability` to the settled outcome via `bin_resolves_yes`, and compute Brier and log score for all three — overall and split by lead, city and price band. The market is the benchmark; climatology is not. Land it as a tracked module with a committed result artifact.
- If the blended posterior does not beat the de-vigged market out-of-sample, the trading thesis is unsupported and no gate change rescues it.
- If it does, the size of the gap is the true edge budget, and it says immediately whether a 0.002–0.007 bar is sane.
- **Do this before fixing the prune (F.8), or extract the rejected arm first** — the prune fix will begin deleting rows that currently survive only because the job is broken.

**8.1.2 — Publish a PIT / dispersion / tail-exceedance diagnostic for the *served* distribution.** *(≈1 day.)*
Join `forecast_emos_daily_high WHERE source='live'` to `cli_settlements` and report, per (station, stored lead, hours-to-close bucket): CRPS, ladder RPS, `mean(z²)`, 80% and 95% coverage, PIT KS gap, and empirical `P(|z| > 2)`, `P(|z| > 2.5)`, `P(|z| > 3)` against Gaussian. Run it on the nightly dataset unit and alert on drift. This one diagnostic answers A.1, A.3, A.5, A.7 and B.5, and it is the missing feedback loop for the entire forecasting side.

**8.1.3 — Make CLV the primary skill metric, and run the PIT recalibration candidate that already exists.** *(≈2 days.)*
`clv.py` is written; fix its settlement keying and account scoping (D.9), give `load_order_clv` test coverage, and wire it into the scorecard and the promotion evidence. Separately, `research_candidates.gaussian_pit_candidate` (`gaussian-pit-station-lead-v1`) is already declared and implements exactly the training-only PIT recalibration A.1 calls for — run `research-evaluate` on it. Do not build new machinery; run the machinery that exists.

### 8.2 Then, in this order

1. **Route live policy changes through the promotion gate.** Add a `research-evaluate` invocation to the nightly unit, commit the verdict artifact, and adopt the rule that a `LIVE_PROFILE_OVERRIDES` edit requires one. Retrospectively Holm-correct the family at `SESSION_MEMORY.md:94-118`. Fix the degenerate p-value floor (D.4) and the clustering unit (D.6) *first*, or the gate will pass things it should not.
2. **Resolve the fee question** (C.1, C.2) against a real Kalshi fill receipt; pin the production path with a test; re-derive the 2026-07-27 bar under the pessimistic rounding.
3. **Repair the three defects in the lower-confidence bound (C.3, A.9, A.11) — as one piece of work.** *[2026-08-30/31: BOTH few-line fixes prescribed below were shipped 2026-08-16 and reverted (A.9 on 2026-08-30, C.3 on 2026-08-31) after they zeroed paper entries for two weeks; the C.3 settlement replay showed the blocked cohort won 91–92.5%. Read both addenda — the remaining valid path here is A.11.]* All three sit on the gate the repository calls its primary defense and all three are permissive. C.3 and A.9 are each a few lines and should be done immediately: compute the NO bound directly rather than reflecting a clipped YES bound, and cap `se_sample_n` at `min_conditional_samples` in the fallback branch instead of at the global archive count. A.11 — deriving the band from the distribution that actually produced `p` — is a design change and should follow 8.1.2, which will tell you how wide the band ought to be. **Do not lower the `edge_lcb ≥ 0` bar while doing any of this** (§8.4); expect the repaired bound to approve *fewer* trades, and treat that as the fix working.
4. **Fix the leaky cohort Brier** (B.1) and re-derive `blocked_forecast_cohorts` from scratch. Until then treat both the HOT block and the July warm-unblock as unjustified.
5. **Fix the retention job properly** (F.8) — bounded subquery, index, bounded gate horizon, batched resumable deletes, and a VACUUM step — after doing 8.1.1 and the F.9 preparations. Do not raise the timeout.
6. **Restore the forecast serve as its own systemd `ExecStart` and make total failure exit non-zero** (E.9). Highest-consequence silent-degradation path.
7. **Write a skip row with a reason code for every non-scan** (F.10). This is the precondition for trusting any future performance statistic.
8. **Resolve SFO's model mismatch (A.8) and the Google decision (E.1).** Both are "decide and act", not "investigate".
9. **Estimate the cross-city residual correlation matrix from the EMOS archive and check the eight hand-assigned region buckets against it** (C.8). The caps themselves already exist and should be kept; what is missing is evidence that they are placed where the correlation is.
10. **Pass the EMOS sigma to the comfort band (E.5) and re-express the source-spread thresholds in sigma units (E.4).** E.4 is currently switching SFO off. Re-validate; do not ship blind.
11. **Scope live equity to the active account** (F.5, F.6).
12. **Reconcile settlement against Kalshi's published result** (D.8) and allow supervised re-settlement (F.1).
13. **Allocate tape before expiring resting orders (C.7), then re-measure the maker fill rate.**
14. **Fit per-city intraday tables** (E.6) from data you already store.
15. **Vintage-stamp or retire every published metric** (B.4, G.1, G.2, G.3, G.4).

### 8.3 Do not do these

- **Do not add a more sophisticated forecast model** — gradient boosting on ensemble features, neural post-processing, mixture density networks. At 60–400 training days per city the NGR form is not the binding constraint; the estimator, the tail shape and the verification are. A more flexible model would overfit faster and be harder to diagnose.
- **Do not add cities.** Fourteen are already unverified against climatology and running on SFO's tuning.
- **Do not tune another gate on the current evidence base.** Thirty-four config revisions in eight weeks on a few hundred settled orders is already deep into multiple-comparison territory.
- **Do not chase a daily-dollar target.** The project's own liquidity-ceiling analysis is the most credible quantitative work in `SESSION_MEMORY.md`, and its conclusion — that displayed depth binds, not the gates — is consistent with everything I found. The correct response is more markets or deeper books, not looser gates.
- **Do not raise `TimeoutStartSec` on the prune job** (F.8).

### 8.4 Do not change these without new evidence

- **The `edge_lcb ≥ 0` floor.** The one gate with a real evidence trail (n = 190 negative-LCB trades, 3 wins). Fix how the bound is *computed* (C.3); do not lower the bar.
- **The fixed-standard-time settlement day and the station registry** (§5.1, §5.2).
- **The `truth_lag_days` rolling-origin boundary and the previous-runs archive** (§5.3).
- **The `maker_fills.py` execution model and its volume-claim conservation** (§5.4) — change the *ordering* in C.7, not the allocator.
- **The archived-account entry freeze, the `mode=ro` restatement, and the account-separation invariants** (§5.5). F.5/F.6 are violations *of* this principle, to be fixed toward it.
- **The append-only `forecast_snapshots` / `decision_snapshots` decision record** (§5.6). It is what makes replay sound and what makes §8.1.1 possible.
- **The five live-execution safety gates and the `LiveTradingDisabled` fail-closed boundary** (§5.10).
- **The `nonfinal_certainty_gate` and the nonfinal observation-error model.** `NONFINAL_OBSERVED_HIGH_SIGMA_F = 0.6` is unvalidated as a *value*, but the mechanism came from a real diagnosed loss and is correct in kind. Fit the constant; keep the guard.
- **The separation of true account balance from strategy attribution** in every published surface (§5.10, D.11).

---

## 9. Where the effort has gone

The engineering, operational and accounting layers of this project are stronger than the modeling and evidence layers by a wide margin. Both prior audits (June and July) were thorough — and both were engineering audits: their P0 lists are execution semantics, ledger reconciliation, deployment integrity and dashboard truthfulness. That work paid off; it is why §5 is as long as it is, and several of the controls it produced are better than what I would expect from a funded team.

The consequence is that the parts nobody has audited are the parts that decide whether the system makes money: whether the distribution is calibrated *where it is traded*, whether the edge survives the *real* fee schedule, and whether the model beats *the market* at all. Those three questions have never been asked in this repository. All three are answerable this week, from data it already stores.

---

## 10. Deployment readiness — measured, not assumed

The owner's stated next step is to implement and deploy these findings. I checked the live runtime read-only on 2026-07-27 (no writes, no service actions) because a dated snapshot cannot answer whether a deploy is currently possible. It was not, and the reason was one of this audit's own findings.

> **STATUS: CLEARED, 2026-07-27.** The critical path below (steps 1-5) was executed the same day. `backup_paper_db.sh preflight` now **passes**, a full `sync_to_box.sh` deploy has run, and `sfo-kalshi-paper-prune.service` completes in **255s against its unchanged `TimeoutStartSec=1800`** where it had been killed at exactly 30:00. The measurements below are retained as the *pre-fix* state, because they are what the diagnosis rests on. See F.8 for the corrected remediation — two parts of the recommendation immediately below turned out to be wrong when implemented.

**Measured state.**

| quantity | value |
|---|---:|
| data volume size | 40,483,942,400 B (37.7 GiB) |
| used | 18,890,969,088 B (17.6 GiB) |
| **available** | **21,576,196,096 B (20.1 GiB)** |
| `paper_trading.db` | 10,841,407,488 B (10.1 GiB) |
| local `backups/` | **0 B** — already empty |
| local `archive/` | 1,486,045,344 B (1.38 GiB) |
| `page_size` / `page_count` / **`freelist_count`** / `auto_vacuum` | 4096 / 2,646,891 / **0** / 0 |
| `decision_snapshots` rows | **909,368** over 2026-06-10 → 2026-07-27 |
| …of which neither `approved` nor `signal_approved` | **433,148** |
| failed units | **1** — `sfo-kalshi-paper-prune.service` |
| timers present | 12 of 12 |

**The deploy preflight fails today, by 1.10 GiB.** `backup_paper_db.sh:97` requires `database_bytes × 2 + 1 GiB` = 22,756,556,800 B; available is 21,576,196,096 B. Nothing is left to reclaim in `backups/` — the prior sessions' manual snapshot prunes already emptied it.

**And the obvious lever does not work.** `freelist_count = 0`: the database has zero free pages, so a `VACUUM` run right now would reclaim nothing. Space can only be recovered by *first* deleting rows and *then* truncating the file. Both halves are missing — the prune has never succeeded (F.8) and there is no `VACUUM` anywhere in the repository (F.8e).

**So the critical path to any deployment was F.8, and only F.8** — all five steps are now done:

1. Repair the retention job — bound the dedup subquery to its own window, add the `(market_ticker, side, target_date, id)` index, bound `gate_missing_days` to a rolling horizon, batch the deletes with a `LIMIT` and make the job resumable (F.8a–d).
2. Protect the evidence that the repair would otherwise destroy — extract the model-vs-market table (§8.1.1) and add `risk_profile` to the dedup key — *before* the first successful run (F.9).
3. Run the prune. 433,148 rejection rows across 47 days are queued behind it.
4. `VACUUM` (or enable `auto_vacuum=INCREMENTAL` and run `incremental_vacuum`) to actually truncate the file. A full `VACUUM` needs scratch space of roughly the file size; 20.1 GiB available against a 10.1 GiB file, so it is feasible today.
5. Re-check the preflight, then deploy.

**Outcome, measured 2026-07-27 (PR #73, merged as `6ee2c235`).** Step 1's recommended index does not work and step 4's `VACUUM` needed a different shape; both corrections are documented in F.8. Step 2's evidence extraction produced 436,998 rejected rows before anything was deleted. Step 3 deleted 334,328 + 72 decision rows plus 21,951 dependent rows in 767s with approved rows untouched (477,770 before and after). Step 4 reclaimed 1.41 GB via `VACUUM INTO` with row-count parity verified across all 29 tables. Step 5 passed.

| quantity | before | after |
|---|---:|---:|
| `paper_trading.db` | 10,864,291,840 B | **9,614,307,328 B** |
| `decision_snapshots` rows | 914,768 | **580,368** |
| …approved / signal-approved | 477,770 | **477,770** (untouched) |
| `freelist_count` | 0 | **0** (fully compacted) |
| disk used | 48% | **44%** |
| `backup_paper_db.sh preflight` | fails by 1.10 GiB | **passes** |
| nightly prune unit | killed at 1800s | **success in 255s** |
| archive gate step | 8m05s | **41s** |
| failed systemd units | 1 | **0** |

Headroom was real but not generous, and it turned out to be worse than "not generous". **Measured 2026-07-28, 24 hours later: the gate had already failed again**, short by 53 MB, with `freelist_count = 0` so a VACUUM could reclaim nothing.

The arithmetic is unforgiving because *both sides move with the database*: growing it consumes free space **and** raises the requirement, so a `2x + 1 GiB` gate tightens three times as fast as the file grows. Solving `available >= 2*db + 1 GiB` against the live volume gave a hard ceiling of **10.30 GB**, against a journal at 10.32 GB growing **~690 MB/day** that compaction only returns to ~9.6 GB. That is *roughly one deployable day per compaction* — a treadmill, not a margin.

**Fixed 2026-07-28 (PR #78).** The `2x` existed solely because the new snapshot and the downloaded restore copy sat on the volume simultaneously. Once the snapshot is in S3 the local copy is redundant, so it is now dropped before the restore copy is pulled — `snapshot -> verify -> upload -> delete local -> download -> verify` — and the verified round-tripped file is moved into the snapshot path for the caller. Peak is one copy, the requirement is `db + 1 GiB`, and the ceiling rises from 10.30 GB to **~15.4 GB**: about a week of growth rather than a day. It is also strictly stronger, since the caller now receives the copy that provably survived the round trip rather than the one that was merely uploaded.

Because `sync_to_box.sh` *streams* this helper to the box rather than using the box's copy, the fix took effect immediately — verified by streaming it to the live box, where it passed a preflight that the box's own copy had just failed.

The EBS resize is still the durable answer; at ~690 MB/day the 15.4 GB ceiling is roughly seven weeks out, not indefinite.

**A second, structurally identical deadlock sits in the backup gate itself, and it is not yet fixed.** `backup_paper_db.sh` prunes old local snapshots with `find … -mtime "+$KEEP_DAYS" -delete` (`:168`), which runs only in `backup` mode — but the free-space check (`:96-102`) runs in *both* modes and *before* it. So the snapshot a deploy leaves behind (9.6 GB, `SFO_DATABASE_BACKUP_KEEP_DAYS=1`) occupies the very space the next deploy's preflight demands, and the prune that would reclaim it is unreachable because preflight fails first. Observed directly: immediately after the 2026-07-27 deploy, available fell to 13 GB against a 20.3 GB requirement. It was cleared by deleting the local snapshot — safe, because the gate had already uploaded it to `s3://…/database-snapshots/` and verified a *restored* copy end to end. Left alone it is a self-inflicted block on the next deploy, with the same shape as F.8: the mechanism that reclaims the space cannot run until the space is reclaimed. Fix: move the retention sweep ahead of the free-space check, and make it also drop the current snapshot once the off-host copy is verified.

**Deploys are expensive at this journal size.** The gate runs `PRAGMA integrity_check` on the 9.6 GB snapshot, `foreign_key_check`, then downloads the S3 copy and `integrity_check`s that too — roughly 10 MB/s each on this t4g.medium. The 2026-07-27 deploy took **~65 minutes with every timer quiesced**, on top of the ~85-minute retention maintenance window. That is sound engineering (it proves the backup restores before production source changes) but it is a real operational cost, and a further argument for keeping the journal small.

The emergency alternative — deleting the 1.38 GiB local `archive/` ring buffer, which is redundant now that the S3 uploads in the prune journal are succeeding — would clear the 1.10 GiB shortfall immediately but fixes nothing and removes the only local copy. That is a data-deletion decision for the owner, not a workaround to reach for.

**Two things in this audit I cannot implement, and they should not be quietly skipped:**

- **C.1 / C.2, the fee schedule.** Resolving whether the production centicent rounding or the documented `ceil_to_cent` rounding is correct requires a fact from outside the repository — Kalshi's current published schedule and, ideally, an actual fill receipt showing the fee charged on a `KXHIGH*` order. I cannot authenticate to an exchange or create an account. This is the highest-leverage item in the audit (C.1 shows it moves the gate boundary by more than the entire traded edge) and it needs the owner. Everything downstream of the approval bar is provisional until it is settled.
- **An EBS volume resize**, if the owner prefers headroom over the retention fix. The instance role is correctly scoped to backup-only S3 access and cannot even `ec2:DescribeVolumes`, so this needs the owner's own AWS credentials.

**What should not be deployed before the §8.1 measurements exist**, regardless of implementation order: A.11 (redesigning the uncertainty band — 8.1.2 tells you how wide it should be), E.4 and E.5 (re-expressing the source-spread thresholds and the comfort band in sigma units — currently switching SFO off, so the fix is wanted, but its magnitude must be validated not guessed), and any re-derivation of `blocked_forecast_cohorts` (B.1 — the metric behind it is leaky, so the replacement needs a clean measurement first). Shipping these on judgment alone would repeat the pattern §4.3 documents: 34 config revisions in eight weeks, each justified by a same-sample counterfactual.

---

## 11. Complete finding inventory and implementation backlog

**What exists.** The twelve area reviews produced **322 findings**: 15 critical, 77 high, 119 medium, 99 low, 12 informational. Sections 4–6 publish the 92 critical/high plus the findings I measured myself. The remaining 230 were then triaged against the code into deduplicated engineering work items, so that "implement the audit" is a defined program rather than a reading exercise.

**Triage result** (six of seven areas complete; the operational/data-layer area was still triaging at finalisation, so its ~41 medium/low findings are not yet folded in — its critical/high items are already published as F.8–F.13 and E.9):

| change class | items | est. effort |
|---|---:|---:|
| SAFE_MECHANICAL — no live decision, forecast value or published number moves | 48 | 136 h |
| BEHAVIOR_CHANGING — alters a decision, probability, size, gate or published metric | 45 | 144 h |
| MEASUREMENT_GATED — must not ship before a named measurement exists | 15 | 68 h |
| EXTERNAL_VERIFICATION — needs a fact from outside the repository | 6 | 18 h |
| DOC_ONLY | 5 | 10 h |
| WONTFIX — real but not worth doing | 3 | — |
| **total** | **122** | **≈376 h** |

30 items touch a data-deletion path, a live timer, or paper-account state and are flagged risky. Triage also removed 20 findings as duplicates of published items and rejected several outright on re-reading the code — for example a "two SFO coordinate pairs" finding whose two pairs differ by ~150 m and which conceded its own harmlessness, and a claim that published win rates blend resolution accuracy with exit timing, which did not survive.

**The honest headline for anyone planning this work: ≈376 hours, roughly nine to ten person-weeks, and 45 of the items change what the live book does.** Those 45 cannot be batched into one release. Each alters a decision, a probability, a size or a published number, and the whole argument of §4.3 is that this project has already changed live policy 34 times in eight weeks without a holdout. Shipping 45 more the same way would compound exactly the problem this audit identifies. They need to go out in small groups, each with a stated expected direction and a re-validation, through the promotion gate that already exists (§8.2.1).

**Six items need the owner and cannot be done from inside the repository:**

1. **The `KXHIGH*` fee schedule** — maker and taker multipliers and the rounding unit (cent vs centicent), from Kalshi's dated published schedule and ideally a real fill receipt. This gates C.1/C.2 and therefore the entire approval bar (§10).
2. **Real CLI product text** from all fifteen WFOs — one final and one preliminary product per station — as test fixtures for the settlement parser.
3. **The observation unit and precision** returned by `api.weather.gov` for these stations (tenths of a degree Celsius via the ASOS T-group, or whole degrees), which determines how F.2's observation-error model should be specified.
4. **An observed maker fill rate and time-to-fill** for bid-improving quotes on Kalshi temperature markets, to replace the modelled queue assumption (C.13).
5. **Whether the Google response's `timeZone.id`** matches the requested city per configured city — one live response each.
6. **Two vendor facts about ladder composition** — the distinct set of `strike_type` values, and how often an event presents a mixed active/inactive bin set — to size the fail-closed guard that B.3 and A.13 both depend on.

**Suggested sequencing**, which differs from a naive severity ordering because the dependencies matter more:

1. **F.8 first, alone.** It is the only thing standing between the current state and any deployment at all (§10), and nothing else can ship until it lands.
2. **The 48 SAFE_MECHANICAL items**, in whatever order is convenient. They carry no re-validation burden, and several — persisting the served sigma, recording skip rows, adding `recorded_at`, persisting EMOS coefficients — are prerequisites that make later measurements possible.
3. **§8.1's three measurements.** These convert 15 MEASUREMENT_GATED items from blocked to actionable and tell you how large several of the BEHAVIOR_CHANGING effects actually are.
4. **The BEHAVIOR_CHANGING items, in small groups through the promotion gate**, starting with the ones whose direction is unambiguously stricter (the three lower-confidence-bound defects, the exposure-cap fitting, the multiplicity controls). Leave anything whose direction is "unknown a priori" until a measurement exists.
5. **DOC_ONLY last**, so the documentation describes what actually shipped.

---

## 12. Reproducing the measurements in this audit

All five are read-only and need no production access.

**A.10 — the market prior is inflated by forced normalisation.** From `trading/`, load the repository's own captured ladder and inspect the raw implied sum:

```bash
python3 -c "
import json,sys; sys.path.insert(0,'.')
from sfo_kalshi_quant.models import MarketBin
from sfo_kalshi_quant.probability import _market_implied_probabilities, _market_implied_yes_value
rows=[m for m in json.load(open('research/kalshi_kxhightsfo_open_markets.json'))['markets']
      if m.get('event_ticker')=='KXHIGHTSFO-26JUN03']
b=sorted((MarketBin.from_kalshi(r) for r in rows), key=lambda x:x.sort_key)
print('raw implied sum =', round(sum(max(0.0,_market_implied_yes_value(x)) for x in b if x.status=='active'),4))
mp=_market_implied_probabilities(b)
for x in b: print(x.ticker[-8:], 'market_p', round(mp[x.ticker],4), 'ask', x.yes_ask, 'diff', round(mp[x.ticker]-x.yes_ask,4))
"
```
Expect a raw sum of **0.9700** and `market_p − ask` of **+0.0038 / +0.0029 / +0.0029** on the three largest bins.

**A.9 — the safety band narrows when conditioning fails.** Build a synthetic archive whose predictions cluster in one range, then call `ResidualCalibrator.conditional_stats` both inside and outside that range and compare `min(cond.n, effective_n)`. A forecast with an analogue gives `se_sample_n = min_conditional_samples`; one without gives the full archive count, and `1.96·SE` falls by `sqrt(N/min_conditional_samples)`.

**A.13 — the blended posterior does not sum to one.** Build two ladders — one with uniform depth ≥ 25 and 1¢ spreads (reliability saturates, weight constant, sum = 1) and one with heterogeneous depth of 2–60 contracts as documented for the July regime — then compare `sum(w_i·model_i + (1−w_i)·market_i)` across them. Expect 1.000000 and **0.9943**.

**C.1 / C.4 — the fee-rounding convention moves the gate boundary.** Evaluate `edge_lcb = 0.96 − (ask + fee)` across asks 0.90–0.97 using `quadratic_fee_per_contract(ask, series_ticker=…)` (production path) and then `ceil_to_cent(0.07·ask·(1−ask))` (the documented schedule). The highest clearing ask is **0.957** under the first and **0.950** under the second.

**§4.2 — forecast residuals are fat-tailed and right-skewed.** Run on the machine holding `forecaster/models/` (read-only):

```bash
python3 - <<'PY'
import csv, statistics as st
for name in ("models/lstm_target_daily_high_next_day_test_preds.csv",
             "models/lstm_target_daily_high_next_day_val_preds.csv"):
    byday = {}
    with open(name) as f:
        for row in csv.DictReader(f):
            byday.setdefault(row["timestamp"][:10], (float(row["pred"]), float(row["actual"])))
    res = [a - p for p, a in byday.values()]
    n, m, s = len(res), st.fmean(res), st.pstdev(res)
    z = [(r - m) / s for r in res]
    exc = lambda t: sum(1 for v in z if abs(v) > t) / n
    print(f"{name}\n  n={n} skew={sum(v**3 for v in z)/n:.3f} kurt={sum(v**4 for v in z)/n:.3f}"
          f"\n  P(|z|>1)={exc(1):.4f} P(|z|>2)={exc(2):.4f} P(|z|>2.5)={exc(2.5):.4f} P(|z|>3)={exc(3):.4f}")
PY
```

The published reliability table in §4.1 is the `calibration` block of `https://jaxsonb04.github.io/weather_edge/trading_signal.json`, and is also present in the tracked fixture `public/trading_signal.json`.
