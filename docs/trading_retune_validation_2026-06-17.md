# WeatherEdge Balanced Retune — Walk-Forward, After-Fee Validation Report

**Date:** 2026-06-17 · **Scope:** Validate the 2026-06-16 BALANCED sizing/gates retune (`config.py` `BALANCED_PROFILE_OVERRIDES`) as real-money-ready · **Verdict: NOT VALIDATED**

> Method note: produced by an 11-agent validation workflow (harness correctness audit ×4, local + live evidence ×2, statistical power analysis, three adversarial skeptics, synthesis). Live numbers are pulled from the AWS-published `strategy_research.json`; the local `paper_trading.db` is empty of trade history. Forecaster source data used for the local calibration run is ~3 days stale (dated 2026-06-14) — treat calibration cohort figures as directional.

## 1. TL;DR

The retuned balanced caps are **not validated** and **must not be treated as real-money-ready**. No existing backtest harness can validate them: the only true walk-forward (`backtest-calibration`) is *provably invariant* to the entire retune (conservative vs balanced output is byte-identical — brier 0.5279, log_loss 1.1383, top_bin_accuracy 0.602), while the two price-aware tools (`backtest-signals`, `backtest-market`) never re-decide under a passed-in config — they replay the *recorded* `approved` flag / `realized_pnl` from the OLD live config. The live balanced evidence (+8.49% ROI) is statistically meaningless: it is 4 trades that collapse to **2 correlated, same-regime weather days**, with the single largest retune-enabled bet (6 contracts @0.78, 44% of risk) expiring **unscored at $0**. There is both a **DATA gap** (local DB empty; history lives on AWS) and a **COUNTERFACTUAL gap** (no tool re-scores history under the retuned config). The path forward is a thin config-parameterized re-scoring backtest plus accumulation to the repo's own >=30 after-cost-trade gate.

## 2. What the retune changed, and what "validation" must prove

The 2026-06-16 balanced retune (`config.py` `BALANCED_PROFILE_OVERRIDES`):

- `max_position_risk_pct` 0.005 -> 0.02 (the binding per-position dollar throttle)
- `max_event_risk_pct` 0.03 -> 0.04, `max_target_exposure_pct` -> 0.06, `max_contracts_per_market` -> 40, `fractional_kelly` 0.15 -> 0.10
- `size_against_live_equity` False -> True
- fast-feedback (research-only): `min_edge_lcb` -0.03 -> -0.07
- Conservative baseline (StrategyConfig defaults) left unchanged as the strict reference.

**Validation must prove:** the retuned balanced SIZING and GATES improve **risk-adjusted, AFTER-FEE return out-of-sample** — i.e. on a walk-forward / held-out basis, re-deciding the historical opportunity set under the *new* StrategyConfig, netting Kalshi entry (and where applicable exit) fees, and measuring whether the larger caps add value rather than just amplifying variance. Probability calibration alone is necessary but **not sufficient**: the retune touches sizing and approval gates, not the forecaster.

## 3. Can the existing harnesses validate it?

| Harness | What it measures | Validates retune? | Walk-forward? | After-fee? | Re-scores under config? |
|---|---|---|---|---|---|
| `backtest-calibration` (`backtest.py:44-105`) | Forecaster probability quality (Brier, log-loss, top-bin acc, calibration buckets, temp cohorts) | **No** | **Yes** (leakage-free; train=`outcomes[:idx]`, test=`outcomes[idx]`) | No (no price/PnL by design) | **No** — config flows only into `ResidualCalibrator`; sizing/gate keys never read |
| `backtest-signals` (`db.py:signal_backtest_summary` ~1220) | Recorded decision snapshots vs official KSFO highs (Brier/log-loss + approved-as-recorded PnL) | **No** | **No** (single in-sample pass) | Yes (entry fee correct; exit fee omitted) | **No** — reads `int(row["approved"])`, `float(row["recommended_spend"])` off snapshots |
| `backtest-market` (`db.py:market_backtest_summary` ~1060) | Forward-realized PnL tally of the live paper book | **Partially** (only via fills already placed) | **No** (no train/test split) | **Yes** (entry fee on fills, exit fee on early close, none on held settlement) | **No** — sums recorded `realized_pnl` |

**The two gaps, stated plainly:**

- **DATA gap.** Local `trading/data/paper_trading.db` is empty: 0 `decision_snapshots`, 0 `paper_orders`, 0 `forecast_snapshots` (only 35 raw `market_snapshots`); a local run printed `official_settlement_days:0`. Every real number in this report comes from AWS-published `strategy_research.json`. No validator can be exercised against fresh local data today.
- **COUNTERFACTUAL gap.** *No tool re-scores history under the retuned config.* `signal_backtest_summary` has no `StrategyConfig` parameter and reads the recorded approval/size flags written by whatever config was live at scan time; `cmd_backtest_signals` (`cli.py:2226-2239`) ignores `args.risk_profile` for scoring; `market_backtest_summary` just tallies booked `realized_pnl`. So the retune changes neither which rows count as approved nor their sizes in any existing tool — it can only report what the OLD config decided.

A confounding artifact in the published signal replay: `approved_pre_resolution_signals = 290` collapses to `approved_signals = 0` after the default `latest-per-market-side` dedup (`db.py:1406-1412`), which keeps the LAST pre-close scan per market-side (edge has decayed to ~0 by then) and discards 100% of the actually-approved entries. The flagship `approved_paper_pnl/roi/hit_rate` are therefore all 0.0 — a dedup artifact, not performance. Likewise the published `avg_edge_lcb = -0.1877` is the **pre-gate universe** statistic over all 108 settled deduped signals, *not* a statement about trades taken (the approved set is empty).

## 4. The live evidence

### Per-profile rollup (AWS-published, `paper_research_only`, live orders disabled, AWS execution calibration LOCKED)

| Metric | balanced | fast-feedback |
|---|---|---|
| n closed | 4 | 19 |
| settled / early-closed / expired | 3 / 0 / 1 | 9 / 10 / 0 |
| wins / losses / zeros | 3 / 0 / 1 | 11 / 8 / 0 |
| realized PnL | **+$0.92** | **+$0.13** |
| capital at risk | $10.84 | $24.36 |
| ROI | **+8.49%** | **+0.53%** |
| hit rate | 1.000 (3/3 decided) | 0.5789 (11/19) |
| distinct weather DAYS | **2** | **6** |

### Balanced — full per-trade list (the damning table)

| date | side | ct | entry | PnL | ROI | risk | status | high°F | edge_lcb | prob |
|---|---|---|---|---|---|---|---|---|---|---|
| 06-15 | NO | 1 | 0.80 | +0.18 | +21.9% | 0.82 | SETTLED | 69 | +0.0355 | 0.9277 |
| 06-15 | NO | 1 | 0.90 | +0.09 | +9.9% | 0.91 | SETTLED | 69 | +0.0131 | 0.9615 |
| 06-16 | NO | **6** | **0.78** | **+0.00** | 0.0% | 4.76 | **EXPIRED** | 71 | +0.0325 | 0.9129 |
| 06-16 | NO | 5 | 0.86 | +0.65 | +14.9% | 4.35 | SETTLED | 71 | +0.0171 | 0.9435 |

All 4 trades are NO-side favorites (entry 0.78–0.90) on only 2 target dates (06-15, 06-16). The largest position — 6 contracts @0.78, **$4.76 = 43.9% of the profile's $10.84 risk** — was a resting limit that never filled and was force-expired to $0 PnL; it neither won nor lost (and would have *won*, since 71 < 73.5). So the +$0.92 rests on **3 decided bets across 2 days**, with one trade (5-ct @0.86 = +$0.65) supplying **70.7%** of the profit.

**The counterfactual is decisive.** Fast-feedback traded the bad day balanced skipped: on 06-14 two confident *positive*-edge_lcb NO bets (+0.0785, +0.0060) lost **−$0.91 and −$0.78 simultaneously**. A single such day in balanced's 2-day window erases its entire +$0.92. Balanced's clean record is **survivorship**, not skill.

## 5. Statistical power

Same-day NO-favorite brackets are mechanically correlated — one SFO daily high settles every NO bet that day together — so the independent unit is the **weather day**, not the contract.

- **Balanced: ~2 effective independent observations.** 4 trades -> 2 days (06-15 @69°F, 06-16 @71°F), both warm-regime, both settling NO-favorites correctly. Wilson 95% CI on hit rate = **[0.44, 1.00]** (3/3) — cannot even rule out a coin flip. A real ROI CI cannot be formed from 2 clusters; the day-clustered bootstrap is degenerate (toggles between the two days' ROIs 0.0714 / 0.1561). The analytic SE (0.0462) is spuriously tight because the expired trade enters as exactly 0.0 ROI, shrinking variance.
- **Fast-feedback (the more honest sample): 6 days, break-even.** ROI +0.53% with a day-clustered bootstrap CI of **[−25.8%, +16.6%]**; hit-rate Wilson CI [0.36, 0.77] straddles 0.50; **mean per-trade ROI is NEGATIVE (−11.78%)** — the portfolio looks positive only because a few large winners outweigh many small-stake losers (a sizing artifact, not a robust edge).
- **Required sample (alpha 0.05, power 0.8).** Treating a NO-favorite as a bet bought at ~0.835 (win pays +0.198, loss pays −1.0; loss ~5x win), you need ~**57** independent trades if true win-prob is 0.93, ~**168** at 0.90, ~**410** at 0.88, exploding past **1500** at 0.86. That maps to roughly **40–300+ distinct weather days (≈2–12 months of live trading)**. The current live balanced sample is off by one to two orders of magnitude.

## 6. Harness correctness issues (severity-ranked)

- **CRITICAL — No counterfactual re-scoring.** `signal_backtest_summary` (`db.py:1220-1296`) has no config parameter and reads the recorded `approved`/`recommended_spend`; it structurally cannot answer "how would the retuned gates have performed." Same for `cmd_backtest_signals` and `market_backtest_summary`.
- **CRITICAL — Default dedup discards 100% of traded entries.** `latest-per-market-side` (`db.py:1406-1412`) collapses `approved_pre_resolution 290 -> approved_signals 0`, zeroing the headline PnL/ROI/hit_rate. The correct `entry-per-market-side` mode (`db.py:1415-1435`) exists but is not the default.
- **HIGH — Calibration backtest is config-invariant to the entire retune.** Empty key intersection between `BALANCED_PROFILE_OVERRIDES` and fields `backtest.py` reads; conservative vs balanced output byte-identical.
- **HIGH — Forecaster anti-calibrated on warm/hot days.** Cohort Brier: cold (<60°F) 0.020 (near-perfect), normal (60–69°F) 0.523, **warm (70–79°F) 0.963**, **hot (80°F+) 0.960 (n=10)** — both warm/hot exceed the 0.9 flag threshold (worse than a coin flip). The balanced NO-favorites live on exactly these warm days (69°F, 71°F). Loosening the edge gate (`min_edge_lcb` -> -0.07) in this regime is especially dangerous. *(Caveat: forecaster source data is ~3 days stale, dated 2026-06-14; treat as directional.)*
- **HIGH — Latest-row sampling maximizes look-ahead bias** for the published calibration metrics (Brier 0.0372/0.0403); the pre-resolution guard (`_is_pre_resolution_decision`, `db.py:1442-1450`) depends on a nullable `market_close_time` (returns `True` on NULL) and demonstrably never fired (`excluded_post_resolution_signals = 0`).
- **HIGH — Largest retune-enabled bet never filled.** The 6@0.78 bet (the one the `max_position_risk_pct` raise actually unlocked) expired at $0, so the headline sizing increase is essentially untested and was never stress-tested by an adverse outcome.
- **MEDIUM — EXPIRED non-positions pollute denominators** (counted in `orders` and hit-rate denominator though no capital was deployed; `db.py:1074,1096-1103`). **MEDIUM — `size_against_live_equity=True` couples the metric to future sizing** (`db.py:1109-1130`). **MEDIUM — Signal PnL omits the early-exit fee** (`db.py:1569-1572` models only entry fee + held settlement), understating cost for trades the monitor would close early.
- **LOW — Default `--source lstm`** (not the live clean-blend path); static synthetic ladder; `hit_rate` ternary guards on hits not denominator.

*Positive note:* the after-fee math that exists is correct and side-correct (entry fee = ceil-to-cent quadratic taker fee on the correct side ask; held-to-resolution winners settle at $1 fee-free; early closes charge both entry and exit fees), verified against live evidence (balanced 5@0.86 NO -> 5*(1−0.87) = $0.65). The problem is it is applied to the wrong (old-config, recorded) decision set.

## 7. Verdict + recommendation

**Verdict: NOT VALIDATED.** With ~2 effective independent observations, +8.49% is noise dressed as a result. No harness re-scores history under the retuned StrategyConfig; the one true walk-forward is invariant to the retune; the live sample is ~10x below the project's own promotion gate; and the forecaster is anti-calibrated precisely on the warm days these NO-favorites live on.

**Recommendation:** Keep the retuned balanced profile **PAPER-ONLY**. Do **NOT** scale to real money and do **NOT** treat the larger caps as real-money-ready. The conservative baseline remains the safe reference. Any cap increase is especially dangerous given the warm/hot anti-calibration — add cohort-conditional gating (suppress or tighten on 70°F+ days) before promotion is even considered.

## 8. The path to real validation

1. **Build the missing re-scoring backtest (`backtest-rescore`).** For each `entry-per-market-side` snapshot (`db.py:1415-1435`, NOT the look-ahead `latest` default), reconstruct a `MarketBin` (`models.py:125`) and `BucketProbability` (`models.py:272`) from already-persisted snapshot fields (entry_bid/ask + sizes, yes_bid/ask, spread, model/market/residual/ensemble/intraday probabilities, probability_lcb, strike_type, floor/cap_strike, market_status, market_close_time), call the existing `RiskEngine(config).evaluate_market(...)` (`risk.py:16-27`) under the BALANCED `StrategyConfig` so all gates and Kelly sizing re-run from scratch, then settle vs the official KSFO high (`load_ksfo_daily_highs`) with the existing after-fee kernel `contracts*((1-cost) if won else -cost)`, `cost = entry_price + quadratic_fee_average_per_contract` (`db.py:1569-1572`, `fees.py:18-54`) — and additionally charge the exit fee for any position the monitor's stop band would close early. Wrap in a rolling walk-forward ordered by `target_date`; report after-fee PnL/ROI/hit-rate with **day-clustered** CIs. The scoring engine and fee kernel already exist; only the snapshot->model reconstruction and day-clustered rollup are new.
2. **Pull the AWS `decision_snapshots` journal locally** (local DB is empty; this re-scorer cannot run offline today even once built).
3. **Accumulate to the threshold.** The repo's own gate is `DEFAULT_MIN_AFTER_COST_TRADES = 30` (`dataset_research.py:19,525-531`), but on **independent weather days**, not contracts. Minimum bar: >=30 settled balanced-eligible decisions over >=30 distinct days; full statistical confidence needs the 40–300+ day range from the power analysis. The challenger calibration is separately blocked (`outcome_count = 13 <= min_train = 180`, "not_enough_clean_data").

Only after (1)–(3) — a walk-forward, after-fee, gate-aware re-score on a sample that clears the codified threshold — can the retuned balanced caps be reconsidered for real money.

**Key files:** `trading/sfo_kalshi_quant/backtest.py` (calibration walk-forward; cohort bins ~140-143), `trading/sfo_kalshi_quant/db.py` (signal/market summaries ~1220/1060; fee kernel 1569-1572; dedup 1401-1435), `trading/sfo_kalshi_quant/risk.py` (RiskEngine.evaluate_market 16-27), `trading/sfo_kalshi_quant/fees.py` (quadratic taker fee 18-54), `trading/sfo_kalshi_quant/config.py` (BALANCED_PROFILE_OVERRIDES 142-167), `trading/sfo_kalshi_quant/dataset_research.py` (DEFAULT_MIN_AFTER_COST_TRADES=30, ~19/525-531), `trading/sfo_kalshi_quant/models.py` (MarketBin 125, BucketProbability 272).
