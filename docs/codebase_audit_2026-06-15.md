# WeatherEdge Codebase Audit — 2026-06-15

> **Audit snapshot from 2026-06-15, re-verified against the codebase on 2026-07-20.**
> Every finding below has been re-checked against current `main` and carries a
> status. **43 are fixed, 17 partially fixed, 25 remain open, 3 were superseded
> by later rewrites, 1 needs a data rerun to settle.** Of the 17 critical- and
> high-severity findings — the ones gating real money — **13 are fixed and most
> carry a named regression test.** See [Remediation status](#remediation-status)
> for the per-finding ledger. The prose in sections 1-4 is the original 2026-06-15
> text, left unedited so the record stays honest; the status tags are additions.

A full-repository review covering correctness/bugs, trade-engine scoring, and
dashboard design, with the explicit goal of (a) being safe enough to one day back
real money and (b) being presentable to a quant employer.

Method: every module was read by a dedicated reviewer, and every finding was
re-checked by a separate adversarial verifier that re-opened the cited code.
**106 findings were confirmed (1 critical, 16 high, 34 medium, 46 low, 9 nit);
9 candidate findings were rejected as false positives.** The review was
cross-referenced against the live published dashboard data (see below).

---

## Remediation status

Re-verified 2026-07-20 by re-opening every cited file on current `main`. A
finding is marked **FIXED** only on positive evidence in current source — a
guard that now exists, a corrected condition, a test that now covers it — not
on adjacency. The 106 original findings resolve into 89 independently
checkable claims (several bullets bundle multiple sub-issues).

| Section | Scope | Fixed | Partial | Open | Superseded | Needs rerun |
|---|---|---:|---:|---:|---:|---:|
| 1 | Critical + high — must-fix before real money | **13** | 2 | 2 | 0 | 0 |
| 2 | Trade-engine scoring | 10 | 7 | 2 | 0 | 1 |
| 3 | Dashboard / website | 9 | 5 | 3 | 3 | 0 |
| 4 | Condensed medium/low index | 11 | 3 | 18 | 0 | 0 |
| **Total** | | **43** | **17** | **25** | **3** | **1** |

The distribution is deliberate: the critical and high findings were worked
down first, and the open items cluster in the medium/low tail. Nothing here is
load-bearing for a real-money decision, because the system does not place
real-money orders — `live_execution.py` has no authenticated client and raises
`LiveTradingDisabled` on every non-dry-run call.

### Section 1 — Critical and high (13 fixed / 2 partial / 2 open)

| Finding | Status | Evidence |
|---|---|---|
| 1.1 **CRITICAL** — arbitrage legs dismantled at runtime | **FIXED** | `paper.py:880` tags legs with a shared `group_id`; `monitor.py:328,338-352` holds grouped legs to settlement. Test: `test_audit_fixes.py::test_monitor_holds_guaranteed_group_legs_to_settlement` |
| Kelly sized against frozen $1000, not live equity | **FIXED** | `_cli/scan.py:198-209` sizes off `store.paper_equity()`; `db.py:5648-5678`. Test: `test_dynamic_bankroll.py` |
| Zero displayed ask size leaves size uncapped | **FIXED** | `risk.py:107,114-118` rejects `ask_size < min_ask_size`; `config.py:81`. Test: `test_sizing_throttle.py`, `test_limit_orders.py` |
| Binding caps ambiguous; cheap legs under-sized | **PARTIAL** | `risk.py:279-284` now reports an explicit `binding_constraint`, but `config.py:113` still uses a raw contract cap (25, up from 10) rather than the per-leg notional cap the fix asked for |
| Settlement overwrites an already-closed order's PnL | **FIXED** | `db.py:4920` `BEGIN IMMEDIATE`; conditional `UPDATE` at `db.py:5038-5054`. Test: `test_audit_fixes.py::test_settlement_does_not_overwrite_a_closed_orders_pnl` |
| Take-profit exits mislabeled `CLOSE_STOP_LOSS` | **FIXED** | `monitor.py:521,569` classifies on a structured `exit_kind`, not string prefix. Test: `test_audit_fixes.py::test_take_profit_exit_is_labeled_take_profit` |
| Headline ROI/hit-rate use look-ahead sampling | **FIXED** | `strategy_lab/calibration.py:354-359` pins `sample_mode="entry-per-market-side"`. Test: `test_dashboard_backtest.py` |
| YES/NO snapshots double-count every market | **OPEN** | `store/scoring.py:144-156` still partitions dedup by `side`, so a YES leg and its complemented NO leg both survive into `brier_score`/`win_rate`. No canonical-side collapse anywhere. No test. |
| Closed W/L capped at 30 while other stats all-time | **FIXED** | `strategy_lab/paper_card.py:110-132` computes all metrics from the full terminal set; the `[:30]` slices apply only to display lists |
| "After-cost market backtest" gate unimplemented | **OPEN** | `dataset_research.py:785-793` still counts raw rows; no fee-adjusted matched-trade simulation exists. Mitigated — status is hardcoded `collect_only` (`:82,142,542`) so nothing is falsely promoted — but the naming still implies backtest rigor it does not have. |
| Lower-confidence bound ~3x too confident | **FIXED** | `probability.py:238-246,274` caps the SE sample at conditional support (`se_sample_n`) |
| Single-source Google fallback reports zero spread | **FIXED** | `_cli/scan.py:1282,1295-1299` refuses entry when `source_count < 2`. Test: `test_entry_target_gate.py` |
| Forecaster files under wrong settlement day in DST | **FIXED** | `google_api.py:107-114` routes through fixed-PST `local_standard_date()`. Test: `test_forecaster_dst.py` |
| Scoring truth ≠ settlement truth | **FIXED** | `blend_archive.py:546-554` prefers the archived CLISFO maximum, with provenance tracking and re-scoring on late arrival |
| Kalshi lookups catch only `URLError` | **FIXED** | `kalshi.py:24-28` adds `KalshiUnavailable(OSError)`; retry/backoff with 429 `Retry-After` at `:84-100`; all call sites updated |
| A/B test treats hourly rows as independent days | **PARTIAL** | Daily-high branch now collapses by calendar date (`forecast_validation.py:22-25`) and gained a Diebold–Mariano test. The `target_temp_next_24h` branch (`:26-27`) still returns hourly timestamps and is still published to `ab_test_results.json`. |
| Unfilled resting orders leak exposure forever | **FIXED** | `db.py:4778-4794` 15-minute maker TTL; settlement backstop at `:4935-4979`; `PAPER_EXPIRED` excluded from spend. Test: `test_audit_fixes.py`, `test_expired_requote.py` |

### Section 2 — Trade-engine scoring (10 fixed / 7 partial / 2 open / 1 needs rerun)

Fixed: primary-reason-only rejection counting (`summary.py:704-718` now tallies
every gate tripped); balanced silently inheriting conservative's floors (profile
taxonomy collapsed to `live`/`research`, `config.py:305-350`); posterior
pre-blended before edge measurement (`risk.py:164-179`, opt-in per profile);
ensemble uncertainty discarded (`probability.py:129-143` implements the proposed
`sigma_eff`); intraday upward mean bias (`probability.py:440-450`); 1-contract
fee rounding mismatch (`fees.py:39-41` rounds to a centicent); basket re-gating
reusing a negative floor (`tail_basket.py:284-292`); kill-switch scope
(`db.py:146-159` adds a 21-day rolling window and covers both profiles).

Still open:

- **Per-event spend cap bypassed on the tail-basket auto-trade path** — `risk.py:460`
  applies `_apply_event_risk_cap` only inside `rank()`, but `_cli/scan.py:1138-1218`
  reaches `place_approved()` directly, and `paper.py:568-653` has no event-level cap.
- **Intraday weight cap still exceedable** — `probability.py:467-477` caps the base at
  `intraday_probability_weight` (0.65) but then adds `intraday_boundary_weight_boost`
  (0.15) and clamps to 1.0, so effective weight still reaches 0.80.

Needs a data rerun: whether the forecast-sharpness EV ceiling has actually moved
is a property of the archive, not of code. The two mechanisms that would relieve
it are now built; the measurement has not been redone.

### Section 3 — Dashboard / website (9 fixed / 5 partial / 3 open / 3 superseded)

The entire legacy generated-HTML dashboard this section audits no longer exists;
it was replaced by the React SPA in `src/`. Findings were re-checked against the
new surface rather than waved off as superseded.

Notably fixed: the **"Restricted diagnostics / decrypted locally" copy that
overstated protection is gone, and the underlying data is now genuinely gated
server-side** — `strategy_lab/build.py:178-181,226-238` strips `_private_` keys
into a separate file and `publish_forecaster_pages.sh:33-37,122` publishes from
an allow-list that excludes it. Also fixed: browser-time `generated_at` on fetch
failure (`data.ts:193-198` sets only an error; `publication.tsx:71-109` is a real
staleness state machine), unlabeled timestamps (now explicit UTC), and divergent
headline highs between views (single `predictedHigh()` helper).

Still open: posterior decomposition is computed but never threaded to the
frontend (`grep` for `ensemble_probability`/`intraday_probability` in `src/`
returns nothing), so the per-candidate expander and the outcome-side
`quality_buckets` bar have no data to render; and the locked-"today" case
(`observed_high_is_final`) is not special-cased in any component.

### Section 4 — Condensed medium/low index (11 fixed / 3 partial / 18 open)

Fixed: missing DB indexes and unbounded `decision_snapshots` (now indexed at
`store/schema.py:499-536` and pruned on a timer); no WAL and default busy timeout
(`db.py:579-580`); migrations re-running every start (`store/schema.py:24-53`
flock + completed-check); daily-report calibration crashing the public artifact
below `min_train` (`report.py:207-234`); locale-dependent `%b` ticker parsing
(`models.py:398-431` uses explicit month tables); missing 429/`Retry-After`
handling; unguarded monitor close path; CLISFO parser returning the first
`MAXIMUM` in the document (`clisfo.py:118-135` now anchors on the
`TEMPERATURE (F)` header); asymmetric one-sided market-implied value; and the
model-veto reading a market-blended posterior (`db.py:2204` `COALESCE`).

This is where the open items cluster — 18 of them. The substantive ones:

- **LSTM residual sigma is computed over ~24 autocorrelated hourly rows per day**
  (`research/forecast_tomorrow.py:48-52`) and feeds the published
  `forecast_data.json.lstm_sigma` shown on the site.
- **Per-day average model/market probability is biased toward 0.5** by summing
  across both YES and NO rows without side normalization (`summary.py:625-732`).
  The fix pattern (`_to_yes_frame`) exists in `backtest_rescore.py:176-183` but
  is not applied here.
- **Accuracy candidates can be promoted on as few as 8 holdout days with no
  significance test** (`dataset_research.py:17,19,498,532`); `research_significance.py`
  exists but is wired only into the separate `research_promotion.py` path.
- **Same-day reanalysis rows can masquerade as forecast edge** — `datasets.py:895,898`
  stamps `lead_hours=None` on reanalysis and candidate scoring applies no lead filter.
- **LSTM sequences are spliced by array position across data gaps**
  (`research/lstm_model.py:78-82`) with no timestamp-contiguity check.
- **`load_to_db` drops and rebuilds tables in place** with no atomic swap
  (`research/load_to_db.py:28,31`).

### Two systemic themes still open

Grouping the open findings surfaces two root causes that each account for
multiple entries above, and each is a single well-scoped fix:

1. **YES/NO double-counting.** The same complemented contract is counted as two
   independent observations in dedup (`store/scoring.py:144-156`) and in per-day
   probability averaging (`summary.py:625-732`). This biases published Brier
   score, win rate, and average probability toward 0.5. A canonical-side collapse
   before scoring closes both.
2. **Autocorrelated hourly rows treated as independent samples.** Present in the
   `target_temp_next_24h` A/B branch (`forecast_validation.py:26-27`) and in the
   LSTM residual sigma (`research/forecast_tomorrow.py:48-52`). The daily-high
   path already does this correctly — collapsing to one observation per calendar
   date — so the fix is to apply the same collapse in the two remaining places.

Until (1) lands, the published Brier and hit-rate figures should be read as
approximate. The headline daily-high accuracy numbers are unaffected: that path
collapses by day and is Diebold–Mariano tested.

---

## 0. Live production snapshot (pulled from the public dashboard, 2026-06-15 22:24 UTC)

This grounds the whole review in what the system is actually doing today:

- Paper book: **8 open, 13 closed, hit rate 46.2% (6W/7L), realized −$0.80,
  ROI −4.96%, unrealized +$1.13, open risk $13.63.** Sample is tiny.
- Gate behavior: **257 approved out of ~18,800 evaluated (0.83% approval).**
  Rejections are dominated by **spread: 10,115 (>50%)**, then edge 2,170,
  basket-guardrail 2,084, model/market gap 975, lower-bound edge 674, bid 626.
- Forecast quality: **mean absolute next-day error 4.15°F** — about one full
  Kalshi bin wide.
- Calibration (trading_signal.json): well-calibrated at low probability
  (0.0–0.3, thousands of samples, gaps < 0.01) but **overconfident at 0.4–0.6**
  (0.4–0.5 bucket predicts 0.444, resolves 0.346, gap −0.098; 0.5+ gap −0.286),
  which is exactly the YES-entry range. Matches the live result that the **YES
  side is 0/3** while NO is 6/10.
- A dashboard "learning" currently headlines that negative-LCB trades made
  +$0.48 while non-negative-LCB trades lost −$1.28 — the *opposite* of the
  project's core thesis. It is small-sample noise (the code correctly says "wait
  for 15 resolved"), but it is confusing on a money-facing page.

**Three takeaways the rest of this document expands on:** the binding constraint
on trading more is **spread**, not edge; the binding constraint on *edge* is
**forecast sharpness** (4.15°F error on 2°F bins); and the dashboard
under-reports sample sizes and over-reports certainty — the wrong direction for
a real-money decision.

---

## 1. Must-fix before real money (critical + high)

### 1.1 CRITICAL — Arbitrage/basket "guaranteed" payoffs are dismantled at runtime

`cli.py cmd_paper_monitor` (≈1857–2026) iterates open orders one at a time
(`open_paper_orders`, db.py 943–957 has no group awareness) and applies per-leg
take-profit/stop-loss, calling `close_paper_order` per leg (cli.py 2018).
`place_arbitrage` records each leg as a separate row (paper.py 283–290). Nothing
tags arbitrage/basket legs or closes them as a group, so when one leg of a
YES+NO box or one rung of a full ladder crosses an exit band, **that leg is
closed and the structure becomes a naked directional bet that can lose the full
remaining stake.** The module's central promise ("settlement payout is fixed by
construction") is false in operation, and no test exercises an arbitrage leg in
the monitor.
**Fix:** persist a `group_id` (or at minimum filter `action LIKE 'ARBITRAGE_BUY_%'`)
and have the monitor either skip intraday exits for guaranteed-payoff groups or
close the whole group atomically. Add a regression test that an open box
survives a leg crossing the take-profit/stop-loss band.

### 1.2 Sizing and bankroll accounting

- **Kelly and every % cap measure against a frozen $1000 notional, not live
  equity** (risk.py 136–138, 221, 384; all 12 `bankroll=` call sites pass
  `config.paper_bankroll`). After losses you keep betting fractions of the
  original $1000, which is precisely the Kelly mis-sizing the parameter-audit
  doc warns about. *Fix:* add a live-equity accessor (start + realized PnL ±
  mark-to-market) and pass it as `bankroll`; keep an explicit notional-vs-equity
  switch so paper runs stay reproducible.
- **Zero displayed ask size leaves order size uncapped** (risk.py 144–145: the
  `ask_size` cap is skipped when `ask_size == 0`, and `_as_float` defaults to
  0.0 when Kalshi omits the field). There is an exit-side min-bid gate but **no
  entry-side min-ask-liquidity gate.** Same pattern in paper.py 39 and
  tail_basket.py 244–245. *Fix:* treat `ask_size == 0` as zero liquidity (reject
  or `contracts = 0`) across all three entry paths.
- **The binding caps are not the ones you think.** For representative SFO
  binaries the fractional-Kelly budget (~$7–15) exceeds the 0.5% per-position
  cap ($5), so `max_position_risk_pct` binds and `fractional_kelly` is inert;
  separately `max_contracts_per_market = 10` caps a 5¢ YES to $0.50 of the $5
  budget. Cheap good legs are systematically under-sized. *Fix:* decide which
  cap is the governor, document it, and replace the raw contract cap with a
  per-leg notional cap.

### 1.3 Data integrity on the exact numbers a real-money/employer decision reads

- **Settlement can overwrite an already-closed order's PnL** (db.py
  `settle_paper_orders` 874–908 issues `UPDATE … WHERE id = ?` with no status
  guard). The settle timer (:10/:40) and monitor timer (every 2 min) are
  separate processes on bare `sqlite3.connect` autocommit connections; if a
  monitor close commits between settle's read and write, settle silently
  rewrites the closed row. *Fix:* make the UPDATE conditional
  (`AND status='PAPER_FILLED' AND settled_at IS NULL AND closed_at IS NULL`),
  return real rowcount, and serialize under `BEGIN IMMEDIATE`.
- **Every take-profit exit is mislabeled `CLOSE_STOP_LOSS`** (cli.py 1918 builds
  `reason = "{side} take-profit …"`, but 2005 classifies via
  `reason.startswith('take-profit')`, which is always False because of the
  `YES `/`NO ` prefix). PnL is correct, but the dashboard shows winning exits as
  stop-losses, and exit analytics are corrupted. One-line fix; add the missing
  `CLOSE_TAKE_PROFIT` regression test.
- **Headline "approved" ROI/PnL/hit-rate use look-ahead sampling** (strategy_research.py
  `_signal_backtest_payload` hardcodes `sample_mode='latest-per-market-side'`,
  line 790). The codebase *provides* `entry-per-market-side` for exactly this and
  its own docstring warns the latest snapshot "can look very different from the
  entry the scanner actually traded." The approved numbers overstate how good
  the entry decisions were. *Fix:* use `entry-per-market-side` for approved
  PnL/ROI/hit-rate; reserve `latest-per-market-side` for calibration only.
- **YES/NO snapshots double-count every market** (db.py `signal_backtest_summary`
  /`_probability_stream_metrics`; `analyze --side both` writes a YES leg and a
  complemented NO leg per market, both survive dedup). This inflates calibration
  sample size and the weather-model-vs-market alpha significance by ~2×, and the
  pairs are perfectly anti-correlated (non-i.i.d.). *Fix:* dedupe to one
  canonical side per (date, market) before scoring; report n as distinct-market
  count. This also fixes the biased per-day avg model/market probabilities.
- **Closed W/L is capped at 30 while realized PnL/closed-count/hit-rate are
  all-time** (strategy_research.py 939–947 vs 1122–1130), so once >30 orders
  have closed the all-profiles card shows an internally inconsistent scorecard
  and contradicts the per-profile tabs. *Fix:* compute win/loss from the same
  all-time population.
- **The "after-cost market backtest" gate is unimplemented** (dataset_research.py
  `_profitability_gate` 519–537 only counts raw `dataset_kalshi_trades` rows;
  there is no matched-trade, fee-adjusted simulation despite the docstring,
  `promotion_rule`, and reason strings all claiming one). It overstates rigor on
  a page meant for employers. *Fix:* implement a real point-in-time after-fee
  matched backtest, or rename the gate to "trade-tape coverage" until one exists.

### 1.4 Probability / forecast correctness

- **The lower-confidence bound is ~3× too confident** (probability.py 167, 195).
  The binomial SE `sqrt(p(1−p)/effective_n)` is attached to a *market-blended
  posterior* (not an empirical frequency), and `effective_n` blends the small
  conditional window (~35) with the large global count (~475), giving
  effective_n ≈ 328 and an SE ~3× too small exactly when conditioning is
  weakest. The `edge_lcb ≥ 0` gate is your primary real-money defense; it is
  weaker than it looks. *Fix:* base the SE on the conditional sample size
  (Wilson/Jeffreys), and propagate model/market/ensemble disagreement as
  variance, not just small additive penalties.
- **Single-source Google fallback reports zero spread** (forecast.py 67–82). When
  weather.db has no blend row, the snapshot has only Google + predicted high, so
  `source_spread_f` returns 0.0 — the disagreement gate can't fire, sigma isn't
  widened, and the market-weight tilt doesn't engage, so the least-corroborated
  forecast looks maximally consistent. There is no `source_count < 2` guard in
  the live trade path. *Fix:* block live trading (or apply a conservative
  synthetic spread penalty) when fewer than two source highs exist.
- **The forecaster files forecasts under the wrong settlement day during the DST
  00:00–01:00 window** (google_weather_cache.py `now_sfo`/`target_date`/
  `blend_targets`/`observed_high_decision` use civil `America/Los_Angeles`, while
  the trader uses fixed-PST). The forecaster-refresh timer fires at 00:40 every
  summer night and archives under the wrong day; the trader then finds no
  matching blend and silently skips. This is the exact bug `settlement_day.py`
  was created to prevent, reintroduced in the forecaster. *Fix:* route forecaster
  date logic through the fixed-PST settlement clock.
- **Scoring/learning ground truth ≠ settlement ground truth** (nws_ground_truth.py
  219 takes `max` over hourly api.weather.gov obs, but the engine settles on the
  CLISFO Daily Climate Report MAXIMUM parsed in settlement.py 88–98). A
  systematic ~1°F divergence flips bin membership at the rounding boundary and
  the adaptive blend weights are trained against the wrong target. *Fix:* archive
  the CLISFO max per date and use it as the scoring/learning truth (store both
  and measure divergence).

### 1.5 Robustness / ops

- **Kalshi lookups catch only `URLError`** (cli.py five handlers; kalshi.py 34).
  Read-phase timeouts and connection resets are `OSError`/`TimeoutError`, not
  `URLError`, so a transient blip escapes to the top-level handler and aborts the
  whole multi-target scan with exit 1. *Fix:* catch `(URLError, OSError)` (plus
  `json.JSONDecodeError`) and add bounded retry/backoff + 429 `Retry-After`
  handling inside the client.
- **A/B test treats thousands of autocorrelated hourly rows as independent
  "days"** (`forecaster/research/ab_test.py` 124–178 with `forecast_unit_dates` returning per-hour
  timestamps for the 24h target), so the groupby is a no-op and the paired
  t-test / Wilcoxon / i.i.d. bootstrap report inflated significance that reaches
  the dashboard as `__N_DAYS__`/`__SPOT_*__`. *Fix:* evaluate at true daily
  resolution or use a block bootstrap / HAC variance, and make the unit count
  honest.
- **Unfilled resting limit orders leak exposure and block re-entry forever**
  (paper.py 185–197; db.py — no cancel/expire path; settle only touches
  `PAPER_FILLED`). Latent today (default entry mode is market) but activates the
  moment `PAPER_ENTRY_MODE=limit`. *Fix:* add a `PAPER_EXPIRED`/cancel transition
  and exclude resting orders from the exposure sum.

---

## 2. Scoring more — and better — trades

### 2.1 Why the engine under-trades (and where it is correctly idle)

Most of the under-trading is *correct*: the market is efficient (average edge of
evaluated rows is negative), and the engine is right to refuse the negative-LCB
tails that historically won 3/190. But several gates reject good trades for
reasons unrelated to edge:

1. **Spread is the dominant blocker** (>50% of live rejections). On
   high-disagreement days the single `source_spread` scalar short-circuits every
   market in the event, and the gate-rejection report only counts the *first*
   reason (`summary.py` `_primary_reason`), so "spread" masks the real
   downstream gates. **First fix:** tally *all* reasons, and bucket
   source-spread/no-market as a "no-data" category separate from edge gates, so
   under-trading is correctly attributed before you touch any threshold.
2. **Balanced silently inherits the conservative cheap-tail floors.**
   `BALANCED_PROFILE_OVERRIDES` never sets `cheap_tail_*` or relaxes posterior
   beyond 0.10, so balanced runs `cheap_tail_min_yes_bid_size=25`,
   `cheap_tail_min_probability_lcb=0.12`, `cheap_tail_min_edge_lcb=0.07`. On a
   ~10-bin ladder this rejects nearly every mid-priced bin. **Fix:** give
   balanced its own params (e.g. posterior ≈0.07, cheap-tail bid-size ≈10,
   cheap-tail LCB ≈0.03) while keeping `min_edge_lcb = 0.00` — more mid-ladder
   trades without re-admitting the failure mode.
3. **The posterior is pre-blended toward the market before edge is measured**
   (probability.py 186: `p = model_weight·model_p + (1−model_weight)·market_p`
   with `market_prior_weight=0.45`), then `edge = p − cost`. Because
   `cost ≈ market_p`, edge collapses to `model_weight·(model_p − market_p) − fee`
   — the blend erases the model's disagreement exactly on the liquid markets you
   most want to trade. **This is the highest-leverage change:** measure edge
   against `model_p` (or a less market-shrunk posterior) for the *gate*, while
   keeping the fully-blended, LCB-weighted probability for *sizing*. Ship to
   fast-feedback/exploratory first and require a walk-forward after-fee backtest
   before promoting to balanced.
4. **Hard volume caps unrelated to edge:** `max_targets=2`,
   `max_entries_per_market_side=1`, opposite-side blocked on the same bucket,
   same-day cutoff at hour 14. The engine has a tiny fixed inventory of
   (date, bucket) slots and burns each once. **Fix:** raise rolling targets to
   3–4, allow profile-aware intraday re-entry for the research profiles, and make
   the cutoff hour profile-aware.

### 2.2 Why scored trades aren't more profitable (edge quality)

- **Forecast sharpness is the binding EV constraint.** With a 4.3°F residual
  sigma on 2°F bins, the model *cannot* assign more than ~0.29 to any single bin
  (reproduced on the 475-row archive); forcing sigma down to 1.5°F still never
  reaches 0.50 and the Brier *worsens* below ~3.5°F. The model is correctly
  humble, so confident single-bin YES bets are essentially unjustifiable — real
  edge must come from **(a) NO bets on bins the market overprices, (b) multi-bin
  baskets, and (c) the late-day observed-high signal.**
- **Today's ensemble uncertainty is discarded.** `ensemble.station_std_high_f` is
  computed and only *printed* (cli.py 2308); the residual sigma is a static
  historical scalar. **Feed the ensemble spread into the residual sigma**
  (`sigma_eff = sqrt(w·σ_resid² + (1−w)·σ_ens²)` with a floor) so the model
  sharpens on calm, predictable days — where genuine single-bin edge lives.
- **Lumpy histogram tails emit spurious 0.00s.** The ~35-sample conditional
  histogram and ~31-member ensemble histogram routinely produce exact-zero tail
  bins (and dominate the smooth normal at `empirical_weight=0.75`). **Smooth them**
  (small kernel or parametric tail) before integrating.
- **The intraday model has an upward bias** (probability.py 300–303 takes the
  `max` of several upward estimates, then right-truncates to [observed, ∞)),
  which over-prices high-temp YES tails — a plausible driver of the 8.7%-modeled
  / 1.9%-realized cheap-tail failure. **Center on a principled conditional mean,
  keep observed only as a support bound.**
- **The calibration you display is not the calibration you trade.** The
  walk-forward backtest scores the weather-model-only ladder, never the
  market-blended/ensemble/intraday posterior that is actually traded.
  `_probability_stream_metrics` already implements the right three-stream
  (weather_model vs market_prior vs traded) comparison — run it over the
  historical archive, not just the ~24 live signals, so you can prove whether the
  model beats the market prior before risking real money.

### 2.3 Other trade-engine fixes

- The 1-contract ceil-to-cent gate fee diverges from the average fee used to
  record/settle, pessimizing multi-contract cheap tails (fees.py 39 vs 57).
- The per-event spend cap is bypassed on the auto-trade tail-basket path
  (risk.py `_apply_event_risk_cap` only runs from `rank`).
- Basket re-gating reuses `config.min_edge_lcb` (negative for
  exploratory/fast-feedback), allowing negative-LCB legs (tail_basket.py 271–276).
- The kill switch only protects fast-feedback and its resolved-ROI query has no
  time window, so a bad early sample can latch it off forever; balanced (the
  trading-intent profile) has no automatic circuit breaker. Extend a looser,
  auto-clearing breaker to balanced and fix the daily-loss keying (it keys on
  settlement date, not the calendar day the loss was incurred).
- The intraday weight cap is violated (the +0.15 boundary boost pushes effective
  weight to 0.80 past the 0.65 config cap), and `_market_implied_yes_value` is
  asymmetric on one-sided books (probability.py 527–529).

### 2.4 Before any real dollar

There is no live execution path today — `KalshiPublicClient` is read-only with no
auth and the base URL is hardcoded to PROD. That is the correct safe state, but
the safety rests entirely on "no order method exists," which is fragile. Build a
separate, opt-in `live_execution.py` with signed-order placement, separate
credentials, a global + per-market notional kill switch, a DEMO/PROD switch, and
exchange position reconciliation before the first live order.

---

## 3. Dashboard / website design

The front end is genuinely strong — a committed "meteorological instrument"
aesthetic (Fraunces + IBM Plex, cold-blue/warm-amber axis), real accessibility
scaffolding, thoughtful mobile reflow, and a logical information architecture.
It reads as a credible quant instrument, not a toy. The work needed is
**trust/correctness and completeness, not a redesign.** The page's core failing
is that it **under-reports sample sizes and over-reports certainty** — the wrong
direction for a real-money decision.

### 3.1 Important data that already exists but is NOT shown

- **Per-bin calibration reliability table** (stated probability vs observed
  frequency vs gap, per bin, for both models) — computed in
  `_calibration_payload['buckets']`, rendered nowhere. This is THE calibration
  artifact a quant looks at first; only a coarse 4-cohort temperature table is
  shown. **Highest-value missing view.**
- **`avg_edge_lcb`, `approved_hit_rate`, `approval_rate`, and the outcome-side
  `quality_buckets`** — all computed in `_signal_backtest_payload`, all dropped.
  These are precisely the numbers that corroborate (or refute) the project's own
  headline negative-LCB finding.
- **The posterior decomposition per candidate** (residual/ensemble/intraday
  probability + `remaining_heat_risk`) — the product's whole thesis (`P_trade =
  model_weight·P_weather + market_weight·P_market`) is invisible; only `model_p`
  and `market_p` are shown.
- **Sample size (N) on every rate.** No hit-rate, win-rate, ROI, or calibration
  figure shows its denominator, and none are suppressed below a minimum N — the
  exact "3/190" failure mode. Add `N` inline and a Wilson interval or "not enough
  data" state.
- **Data-provenance counts** (decision/market/monitor snapshots per window),
  **model-vs-market gap stats**, **per-day avg model/market probability**, and
  **`unresolved_past_targets`** are all computed and discarded — the cheapest
  credibility wins and the fastest way to spot a silently-dead timer.

### 3.2 Noise to remove or demote

- **`trading_signal.json` is injected into the public landing and details pages
  but rendered nowhere** (`const TRADING_SIGNAL` / `window.CHART_DATA.tradingSignal`,
  zero references). It is dead weight **and** leaks the live paper-trading signal
  and per-source forecasts into forecast-only pages. Remove it or render it.
- The input-side **quality-distribution** chart (counts per quality band) proves
  nothing about whether high-quality signals win — pair it with, or replace it
  by, the outcome-side `quality_buckets`.
- **Duplicated "Open risk" readouts** appear in four places; consolidate to one
  authoritative readout per section.
- The **"Restricted diagnostics / decrypted locally"** copy overstates protection
  in the default public build (the full unencrypted JSON is fetched
  client-side). Either gate it for real or soften the copy.

### 3.3 Trust bugs on a money-facing surface

- **Fetch-failure fallback stamps `generated_at` with the current browser time**
  (strategy-lab.html 2665), so a failed/stale load renders as fresh. Combined
  with **no relative age / staleness coloring** on "Updated," a frozen pipeline
  looks identical to a live one. Highest-trust fix.
- **The calibration "winner" pill paints the healthy state (lstm leading) amber
  "warn."**
- **Empty charts render blank axes instead of an explicit "not enough samples"
  state** — in the current low-sample regime the analytical heart of the page
  looks broken. (details.html already has `missingChart()` to reuse.)
- **Timestamps render in the viewer's timezone with no label**, on a fixed-PST
  settlement-clock system — show PT explicitly.
- **Landing and details can show different headline highs for the same day**
  (landing uses `predicted_high_f` or a 2-source mini-blend; details rebuilds a
  4-source blend + station adjustment + lock). Make the headline high a single
  source of truth.
- The first screen is missing two of its own stated priorities — **Google budget
  status** and **observed-high-so-far** — both already computed in details.js.
- On a locked "today," the probability panel collapses to `std=1` and presents a
  near-step function as smooth calibrated percentages; replace with an "observed
  high locked" state.

### 3.4 Beautiful + animated (employer-facing polish)

Add, all gated behind `prefers-reduced-motion`:
- count-up animation on the key metrics (PnL, ROI, MAE, temp);
- a freshness "heartbeat" dot tied to the staleness threshold;
- in-place Chart.js updates (`chart.update()`) instead of destroy/recreate, so
  the 5-minute refresh and profile switches cross-fade instead of flicker;
- a **calibration reliability chart** (stated vs observed with the 45° ideal
  line) and an **outcome-side quality-bucket bar**;
- scroll-reveal for the numbered sections, skeleton-shimmer loaders, and a
  per-candidate expander that reveals the posterior decomposition;
- a **dark theme** (all three pages are hard-locked to `color-scheme: light`).
Also: extract the three drifting `:root` token blocks into one shared partial,
and split the ~1.2k lines of inline CSS / 1.5k lines of inline JS into cached
assets with `defer`-ed Chart.js.

---

## 4. Condensed index of the remaining medium/low findings

Beyond the items above, the verified list includes (by module):

- **db-layer:** no indexes on any table (full scans on an unbounded, never-pruned
  `decision_snapshots`); SQLite not configured for concurrent multi-process
  access (no WAL, default busy_timeout, migrations re-run every start).
- **forecaster-ingest:** Google event-budget file write is non-atomic and
  unlocked (corruptible on crash, double-spend on concurrent refresh);
  daily-high marked `is_complete` purely by date, ignoring observation coverage;
  adaptive blend weights refit on all days after a holdout that only validated
  train-fitted weights; `load_to_db` rebuilds the whole table with no atomic swap.
- **forecaster-models:** LSTM residual sigma computed over ~24 autocorrelated
  hourly rows per day (inflated band); reliability diagram only tests
  P(actual > median), never the tail bins traded; `temp_daily_high` is a trailing
  24h rolling max, not the settlement-day high; LSTM sequences sliced by array
  position can splice across time gaps.
- **datasets-research:** accuracy candidate can be declared on as few as 8 holdout
  days with a bare point comparison (no significance test); doc-mandated 60/50
  gate values are unreachable (code hardcodes 30/30); SQLite connections never
  closed; near-perfect reanalysis `temperature_2m_max` can masquerade as a huge
  (lookahead-like) edge; candidate sort raises `TypeError` on mixed None/numeric
  deltas and blanks the whole panel.
- **strategy-research:** top-level signal backtest blends all profiles incl.
  fast-feedback despite the "excludes experimental" claim; 24-row display cap can
  hide today's entry-block reason and flip the scanner status to "active";
  per-profile `unresolved_past_targets` hardcoded to `[]` so the settlement-backlog
  alert can never fire per profile.
- **reporting-backtest:** reporting uses civil DST time, not the fixed-PST clock;
  daily-report calibration crashes the whole public artifact when history is below
  `min_train`; per-day avg model/market probability biased toward 0.5 by the
  YES/NO doubling.
- **cli-orchestration:** locale-dependent `%b` month abbreviation can break
  Kalshi ticker build/parse under non-English `LC_TIME`; no 429/Retry-After
  handling; the monitor close path is unguarded (a mid-loop failure aborts the
  rest and the summary).
- **probability-engine:** the CLISFO max parser returns the first `MAXIMUM <n>` in
  the whole document with no section anchoring; one-sided market-implied value is
  asymmetric.
- **paper-journal:** model-veto reads the market-blended posterior despite being
  described as a "model snapshot."
- **arb-basket:** `min_profit` is an absolute $0.01 floor with no
  return-on-spend / per-contract margin floor.
- **forecast-blend:** clean-blend dedup orders `fetched_at` by lexicographic string
  comparison; observed intraday high is applied through three coupled channels
  with no anti-double-count guard.

---

## 5. Suggested order of work

1. **Correctness/trust quick wins (low risk, high credibility):** take-profit
   mislabel; settle-vs-close race guard; YES/NO double-count dedupe; closed-W/L
   all-time consistency; broaden Kalshi exception handling; calibration crash
   guard; dashboard freshness honesty (stop faking `generated_at`, add staleness
   coloring + N on every rate + PT timestamps); remove the dead/leaky
   `trading_signal.json` injection.
2. **Real-money-blocking correctness:** arbitrage/basket group close (critical);
   live-equity sizing; single-source-fallback guard; the forecaster DST settlement
   bug; CLISFO-vs-hourly ground-truth alignment; LCB standard-error fix.
3. **Score more/better trades (each behind a walk-forward after-fee backtest):**
   separate the spread block in reporting; give balanced its own cheap-tail/
   posterior params; measure edge against `model_p` for the gate; feed ensemble
   spread into sigma; smooth histogram tails; remove the intraday upward bias;
   add an adjacent-bin basket mode; expand the tradeable universe; harden/extend
   the kill switch.
4. **Surface what you already compute:** per-bin reliability table + 45° chart;
   `avg_edge_lcb`/`approved_hit_rate`/`approval_rate`; posterior decomposition;
   quality-bucket outcome curve; data-provenance strip; a standing "known failure
   modes" + paper-only disclaimer.
5. **Polish:** count-up metrics, freshness heartbeat, in-place chart updates,
   scroll-reveal, skeletons, dark theme, shared token partial, asset splitting.
6. **Only then** stand up the separate, opt-in live-execution module.

> No parameter or gate change should be promoted to the balanced (trading-intent)
> profile without a point-in-time, after-fee, out-of-sample walk-forward backtest
> — the same discipline `docs/trade_engine_parameter_audit.md` already insists on.
