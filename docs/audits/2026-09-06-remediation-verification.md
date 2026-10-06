# WeatherEdge — remediation verification and volume plan — 2026-09-06

**Audience: the engineering agent doing the next round. This verifies PRs #112–#115 against `docs/audits/2026-09-03-audit-remediation.md`, reports one new regression, and lays out the volume/frequency levers with measured numbers.**

Verified against: `origin/main` = `43628982d`; box runtime `source_sha 2a6432e3b` (PR #113 merge, synced 2026-09-05T04:27:51Z); checked-out branch `codex/apple-history-ml-evaluation` (`b308dcbc6`, draft PR #116). Box runtime files md5-match `2a6432e3b` for every file checked. The public bundle on GitHub Pages, `/opt/weatheredge/webdist`, and repo `dist/` are identical (`index-DQ1TOign.js`, built Sep 4 21:54 PDT). Full test suite on the Mac: **2,876 passed, 8 skipped, 117.8 s** (`ulimit -n 8192`, `TZ=America/Los_Angeles`).

Read-only throughout. Nothing was edited, deployed, restarted, or written to any DB.

---

## 0. Scorecard

| Section of the 9/3 brief | Items | Done & correct | Partial | Not done | Regressions |
|---|---|---|---|---|---|
| §1 Ops (OPS-1..7) | 7 | 3 (OPS-1, 5, 6) | 2 (OPS-2 one-time only, OPS-7 ANALYZE only) | 2 (**OPS-3 alerts**, OPS-7 journald) | 1 (see §1 below) |
| §2 Trading core (TC-1..15) | 15 | 10 | 2 (TC-6, **TC-12**) | 2 (TC-9, TC-15) | 1 (TC-12 half-fix) |
| §3 Forecaster (FC-1..11) | 11 | **0** | 1 (FC-9 fetch refactor only) | 10 | 0 |
| §4 Site (SITE-1..5) | 5 | 2 (SITE-1, SITE-2) | 1 (SITE-3) | 2 (SITE-4, SITE-5) | 0 |
| §6 Improvements (IMP-1..10) | 10 | 2 (IMP-5; IMP-4 via TC-4/5) | 1 (IMP-6 one-time) | **7** (IMP-1, 2, 3, 7, 8, 9, 10) | 0 |

**Production is materially healthier**: publish CPU 10 min → 1.5 s per run, CPU steal 72–78% → 0.0%, DB 29.9 → 19.2 GB (one supervised prune + `VACUUM INTO` on 9/4), disk 60% → 44%, deploy gate passing with 16.4 GB margin, freshness and health watchdogs 0 failures since deploy, false site banner gone, readiness panel honest, edge-reversal stop gone from the exit mix, heartbeat on with 0 `HOLD_NO_MODEL_READ`.

**But**: alerts still go nowhere, nightly deletion is still off so the gate re-fails ~Sep 17–18, the research scan tick now crashes nightly in a two-hour window, and none of the forecaster or strategy improvements exist. The Apple branch is research-only and remediates nothing.

---

## 1. NEW REGRESSION — fix first

### REG-1 `P1` — research scan unit crashes in the 05:00–07:00 UTC window (half-finished TC-12)

**Evidence (production):** 471 scan runs since deploy, **7 × `status=1/FAILURE`** (2026-09-04 23:20/23:25/23:30/23:35 PDT; 2026-09-05 23:00/23:10/23:20 PDT), all identical:
```
error: target research minimum lead is 1 station-standard day(s); got 0
```
Austin, target Sep 6, at exactly 06:00 UTC = Central-standard midnight while the Pacific civil day is still Sep 5. In every run `profile=live` had already completed; the unit exits 1 anyway, `OnFailure=` fires, the alert is a no-op, and the remaining cities/targets of that research tick are skipped. Repeats every 5-minute tick while the candidate stays approved.

**Mechanism:** PR #112 fixed the DB side of TC-12 but not the scanner side.
- `db.py:3733-3746` now computes `station_day = settlement_clock(self._research_clock(), city).date(); lead_days = (target_day - station_day).days` and raises plain `ValueError(f"... research minimum lead is {policy.min_lead_days} station-standard day(s); got {lead_days}")` (and a second `"research admission canonical lead bucket does not match target"` raise at `:3746`).
- `_cli/scan.py:627-628` still computes `lead_days = (target - objective_day).days` from `store.research_objective_day()` (Los Angeles civil day) and builds `ResearchOpportunity(decision, target.isoformat(), lead_days)`; `research_portfolio._target_rejection` (`:460`) admits on that value.
- Neither raise is a `ResearchEntryLimitError`, so `paper.py:473 except ResearchEntryLimitError` does not catch it; `cmd_portfolio_scan` (`scan.py:1870-1899`) catches only `ArbitrageContainmentError`/`ForecastDataError`; `cli.py:386-388 except Exception → return 1`; `run_paper_scan_profiles.sh` is `set -euo pipefail`.
- The new test `test_target_minimum_lead_uses_city_fixed_standard_day` codifies the raise (`pytest.raises(ValueError, match="minimum lead")`), so the suite is green while production fails.
- The new `canonical research entry rejected` skip path has fired **0** times in production.

**Window:** Eastern cities (MIA/ATL/NYC/PHL/BOS, EST) 05:00–07:00 UTC and Central (CHI/DAL/AUS/HOU/OKC, CST) 06:00–07:00 UTC, whenever a D+1 research target is approved (~12 admissions/month per the audit).

**Fix (both halves):**
1. In `_execute_research_scan_context` (`_cli/scan.py:~627`), compute `lead_days` with `settlement_clock(now, city).date()` so the candidate is rejected upstream as `"target requires day-ahead lead"` and never reaches the DB.
2. Make the three lead/objective raises in `db.py:3733-3746` `ResearchEntryLimitError` so `mark_research_decision_admission_blocked` records them instead of crashing the unit. (This also generalizes the OPS-4 fix, which only covered `db.py:3260/3321/3326`.)
3. Change the test to assert the upstream rejection reason, not the raise.

---

## 2. Still-open ops items (owner-level)

### OPS-3 — alerts still a no-op `unchanged`
`/etc/weatheredge.env:49 SFO_FRESHNESS_ALERT_URL=` empty; `send_systemd_failure_alert.sh` md5 = baseline; 48 `alert was not sent` lines in 7 days; all 7 REG-1 failures went unalerted. SESSION_MEMORY says "no real webhook was available; do not invent one". **Any webhook works** — a Discord/Slack incoming webhook, an ntfy.sh topic, or an SNS topic. This is the cheapest and most important open item.

### OPS-2 — retention deletion still off; gate re-fails ~Sep 17–18
- Code: `db.py:6504-6575 _materialized_batched_delete` is a real fix (materializes dedup candidate ids into a TEMP table, then PK-batch deletes). `run_archive_then_prune.sh:17` unchanged: `PRUNE_MODE="${SFO_PRUNE_MODE:-archive-only}"`; env still has no `SFO_PRUNE_MODE`.
- What happened: one supervised run on 9/4 via a transient `weatheredge-audit-prune.service` (`SFO_PRUNE_MODE=quiesced-delete`, 14:50→15:42 PDT, 12m51s CPU, 3.3 G peak), then `VACUUM INTO` swap, then `ANALYZE`. DB 29.93 → 17.96 GB. `decision_snapshots` 4.1 M → 1.97 M rows.
- Now: 19.22 GB, **growth 0.74 GB/day** (1.267 GB in 41.3 h; daily inflow still 105–126k rows). Nightly log 9/6 01:27: `DEGRADED: ... scheduled live-DB deletion skipped` (3m13s CPU, 1.3 G peak, deleting nothing). Gate margin 16.4 GB shrinks ~1.47 GB/day → **fails again ~2026-09-17/18**; 85% watchdog ~Oct 13.
- SESSION_MEMORY: "Scheduled live-DB deletion remains safe-off until writer quiescence can be automated."
- **Fix options:** (a) enable the bounded batched delete nightly (`_materialized_batched_delete` holds locks ≤2 s with `busy_timeout=30 s` — it was designed for this); (b) schedule the quiesced run weekly with the timers stopped for its ~50-minute window; (c) the blob-bloat reduction from the 9/3 brief (content-hash `market_snapshots.raw_json`, delta-only monitor diagnostics — ~250 MB/day, 40% of inflow) which has not been started. Without one of these, the next deploy after ~Sep 17 is blocked again.

### OPS-7 — journald, housekeeping `not done`
`ForwardToSyslog=yes` still effective (`/usr/lib/systemd/journald.conf.d/syslog.conf`), `SystemMaxUse=500M`; `/var/log/syslog.1` 380 MB; journal covers 4.5 days only because OPS-1 cut volume. `.cache/main` (17 MB, Jul 25) still present; orphan `paper_trading-20260828T030029Z.sqlite3-{shm,wal}` still in `backups/`; `.google_weather_usage.json` still Jul 19.

### OPS-1 residuals `low`
No periodic re-orphan of gh-pages (`git checkout --orphan` only on fetch failure, `publish_forecaster_pages.sh:230-233`) — history keeps growing on GitHub, harmless to the box now. Overview copy still says "publish … every 5 minutes" (`SystemHighlights.tsx:47`) — cadence is now 10.

---

## 3. Verification tables

### 3a. Ops

| Item | Code | Correct | Deployed | Live evidence |
|---|---|---|---|---|
| OPS-1 shallow clone + cadence + priority | `36fce7865`: `publish_forecaster_pages.sh:226 git fetch --depth=1`; timer `OnCalendar=*-*-* *:02,12,22,32,42,52`; publish/strategy-lab `Nice=10 CPUWeight=50`; scan/monitor `Nice=0` | Yes | Yes | `Consumed 1.456s CPU time` every run (was 10m13s); `sar -u %steal 0.00` all day; run 12:42:16→12:42:24 |
| OPS-2 | see §2 | prune code yes; deletion off | prune code yes | growth 0.74 GB/day |
| OPS-3 | none | — | — | env empty, 48 no-op alerts |
| OPS-4 research exception → skip | `db.py:166 class ResearchEntryLimitError(ValueError)`; raises at `db.py:3260/3321/3326`; `paper.py:466-478 except ResearchEntryLimitError → mark_research_decision_admission_blocked → continue` | **Only for that one exception** — see REG-1 | Yes | skip path fired 0×; unit still fails 7× on the new raise |
| OPS-5 freshness lock race | `check_forecast_db_freshness.sh:35-36, 87-100 exec 8>"$ARTIFACT_LOCK"; flock -w 120 8` around `validate_local_manifest` | Yes (same lock file the publish cycle holds) | Yes | 81 runs, 0 failures, 0 `checksum mismatch`, no `STALE_FORECAST` marker |
| OPS-6 health fragilities | `check_scheduler_health.sh:57` wait 10→120 s; `:232-233 curl --retry 2 --retry-delay 2 --retry-all-errors`; `:377-392` captures and prints `validation_output` | Yes | Yes | 0 failures; `OK: scheduler and publication health verified` |
| OPS-7 | ANALYZE done by hand 9/4 (`sqlite_stat1` 1,757,372 vs actual 1,971,920) | — | — | journald unchanged |

### 3b. Trading core

| Item | Changed | Correct | Notes |
|---|---|---|---|
| TC-1 arbitrage bypasses policy | `paper.py:904-907`, new `_fit_arbitrage_to_account_policy` `:1149-1171` | **Yes** — runs after `_fit_arbitrage_to_exposure`, before `_normalize_arbitrage_contracts` and `record_paper_order`; calls `account_policy_capacity` per ticker, takes `min(allowed)`, rescales | Skipped when `bankroll is None` (both scan call sites pass it). Multi-ticker ladders clamped to a single-position cap (over-conservative). Test covers only `allowed_spend=0`, not partial clamping. |
| TC-2 clv.py date-keyed | `clv.py:189 _authoritative_highs → load_cli_settlement_truth(weather_conn)`; `:223 settlement_for_market(highs, market_ticker, target_date)`; `:234 config.temperature_cohort` (4 buckets); `--weather-db` opened `?mode=ro` | **Yes** | Uses `cli_settlements` (the stated truth). |
| TC-3 daily budget per-tick | `db.py:2531-2552 paper_directional_spend`; `portfolio.py:98,105,186`; `scan.py:918-920` | **Yes, a real accumulator** — `SUM(contracts*cost_per_contract) WHERE target_date=? AND risk_profile=? AND parent_order_id IS NULL AND group_id IS NULL AND status NOT IN ('PAPER_EXPIRED','PAPER_CANCELLED')` seeds `directional_spend` | **Live only** (`_portfolio_scan_one_target` returns before `allocate_portfolio` for research). **This is a live behavior change, not "latent"**: the 8% ($80) per-target-date spend cap now counts resting orders, so three ~$28 resting quotes exhaust it while unfilled. Post-fix data shows 1 `directional risk budget is full` block. |
| TC-4 edge-reversal stop | `exits.py:247-282` branch deleted; `if tp_net is not None and net_exit >= tp_net and net_exit >= entry_cost: TAKE_PROFIT`, then stop floor with veto intact | **Yes (audit option b)** | Live TP unchanged (margin 0, `settlement_first` still `None` for live). Side effect: YES positions with a dead read now ride to the 25% YES floor (untested; book is 100% NO). Production: 0 "edge reversed" exits post-fix vs 2 live + 5 research pre-fix. |
| TC-5 research TP margin | `exits.py:42,148,166-169,189`; `monitor.py:101-108,161-163,470-500`; `parser.py:778-789`; `paper_card.py:1093,1109-1113,1144-1156,1263-1267,1282`; env `:171`; unit | **Yes** — distinct `margin` (adds): `min(1.0, p - buffer + margin)`; `convergence_buffer` untouched; `_take_profit_margin_for_order` returns `margin if research else 0.0`; `--research-take-profit-margin` / `PAPER_RESEARCH_TAKE_PROFIT_MARGIN` default 0.05; **lockstep with `paper_card` holds** | Box env has `PAPER_RESEARCH_TAKE_PROFIT_MARGIN=0.05`. Margin shows up as "≥ target" reason, not a distinct string. |
| TC-6 three dead controls + reason persistence | Only `scan.py:1507-1523` | Reason persistence **yes** (first `plan.reasons` entry matching the ticker → `entry_block_reason`). **The three controls untouched**: `account.py:66 DAILY_LOSS_PCT = 0.02` still checked; `:60 MAIN_SLEEVE_PCT = 0.16` same no-op algebra; `portfolio.py:84 yes_sleeve = bankroll*0.08*0.05` = $4 — and the new test `test_joint_kelly_cannot_resize_live_yes_past_its_sleeve` asserts `<= 4.0`, **cementing** the $4 sleeve. | Neither fixed, removed, nor documented as deferred. |
| TC-7 joint resize discards sleeves | `portfolio.py:186-188, 232-233, 282-290` re-check `max_daily_loss`, `yes_sleeve`, `explore_sleeve` after resize; worst-case against `max(0, max_daily_loss - existing_directional_spend)` | **Yes** | |
| TC-8 fee rounding | `fees.py:5,74` — `FEE_ROUNDING_UNIT`/`_ceil_position_plus_fee` deleted; all paths `ceil_to_cent(raw_fee)`; `arbitrage.py:285,408` pass `series_ticker` | **Yes** | Fees up ≤$0.0099/order → `edge_lcb` slightly lower everywhere (live behavior change). `FEE_SCHEDULE_VERSION="2026-09-04"`. Tests re-baselined 9.68→9.67, 7.58→7.57. |
| TC-9 shared maker tape | **none** | — | roi-v6 still competes with live; not listed as deferred. |
| TC-10 cross-profile model read | `db.py:2559-2618` `profile_clause = " AND risk_profile = ?"`; `monitor.py:470-474` passes it | **Yes on the decisions path** | Residual: `_latest_model_probability_from_snapshots` (`probability_snapshots`) has no profile column and is now fed by the heartbeat for both profiles; `latest_model_probability_read` returns `max(valid, key=timestamp)`. Low impact. |
| TC-11 range vs sigma | `scan.py:399-407, 432` `forecast_sigma_f = float(target_emos[1])` else `raw_emos["sigma"]` | **Yes** | Changes live comfort band and far-tail NO size boost — behavior change. |
| TC-12 lead-day clock | `db.py:3733-3746` only | **Half** — DB gate right, scanner unchanged → **REG-1** | |
| TC-13 heartbeat | env example `:178` → `true`; `migrate_weatheredge_env.py:24-27,71-103` flips exact `=false` and appends missing keys; invoked by `install_systemd.sh:99` | Yes | Box env `PAPER_SAME_DAY_MODEL_HEARTBEAT_ENABLED=true`. Monitor env, not `StrategyConfig` — does not rotate the fingerprint. 0 `HOLD_NO_MODEL_READ` post-fix. |
| TC-14 (funnel, informational) | — | — | See §6. |
| TC-15 `recommended_contracts` bookkeeping; `min_notional` | **none** | — | `execution.py:373` still `expected_profit=quote.edge * decision.recommended_contracts`; `config.py:552 limit_taker_cross_min_notional: 1.0`. Not listed as deferred. |

**Fingerprint / evidence clock:** `StrategyConfig` unchanged. `account.py:185` now hashes `"behavior_version": STRATEGY_BEHAVIOR_VERSION = "behavior-v2-audit-remediation-2026-09-04"` (`:29`), so **every fingerprint rotated** on the 9/5 deploy (live/limit `dceac4d4…→88e417a6…`, research/limit `89c0e326…→92934c13…`). `research_policy.policy_fingerprint` unchanged. `replay.py:1039` semantics string updated. The live readiness cohort restarted 2026-09-05 — correct and necessary given TC-3/4/8/11 change live behavior. **Process risk remains**: exit parameters are still outside `StrategyConfig`; future exit changes depend on someone bumping `STRATEGY_BEHAVIOR_VERSION` by hand.

Commit attribution: all TC changes in `36fce7865` (PR #112). `a3cd86397` changed `exit_audit.py` (`return _explicit_exit_reason(...) or "unclassified"`, exact-action alias map excluding `HOLD_MODEL_VETO`) — the §8 classifier defect, correctly fixed, 14 tests pass. `2f26883c9` deploy scripts only.

### 3c. Forecaster — nothing remediated

The only forecaster-path change on main is `forecaster/nwp_archive.py` (+79, new `_fetch_model_range_leads`: one HTTP request per model carrying all leads; docstring corrected). `emos_forecast.py`, `postproc_models.py`, `emos_recalibration.py`, `forecast.py`, `probability.py`, `datasets.py`, `dataset_research.py`, `apple_weatherkit.py`, `risk.py`, `config.py`, `execution.py`, `posterior_kelly.py`, `cities.py`, `clisfo.py` are **byte-identical** to the baseline.

| Item | Status | Current production evidence |
|---|---|---|
| FC-1 debias spread | **not done** (0/3 places; `max_source_spread_f` still 10.0 both profiles) | Live rows ≥9/5: KSFO mean `model_spread_f` **14.47** (max 24.8), KLAX **11.06**. `decision_snapshots` 9/5–9/6: SFO research **4,416/7,704** rows blocked on `source spread`, LAX 4,344/7,716. Orders since 9/5: SFO 3 research (2 expired, 1 partial 1 ctr), LAX 1 research, **live 0**. Same rate as before. |
| FC-2 lead-0 sigma | **not done** (`emos_forecast.py:462 lead_days=max(lead,1)`; `SERVE_RECAL_SIGMA=False`) | 8/1–9/4, n=525/lead: lead 0 z²=**0.549** σ=1.93; lead 1 0.797; lead 2 0.936. Calibrated lead-0 σ ≈ 1.43 °F. Still ~2× over-dispersed. |
| FC-3 / IMP-8 per-station dispersion | **not on serve path** | Offline only: `docs/research/2026-09-04-crps-pilot.md` — CRPS-fitted vs OLS EMOS, pooled −1.1% (CI [−0.0187, −0.0083]); PHL lead-2 +1.64%. A variance-coefficient refit, not per-station `sigma_scale`; doc says it "does not authorize serving". |
| FC-4 Apple/Google unconsumed | **not done** | Both timers `enabled/active`. Apple journal 3×/day "live trading weight remains 0". **Google still 4xx on 100% of requests and still billed**: `google_weather_usage_events` 9/2 142, 9/3 142, 9/4 118, 9/5 142, 9/6 91 (all `response_status_class=4`, `billable_events=1`); "14 attempted, 0 available; month: 873/8000". |
| FC-5 intraday drag | not done (`forecast.py:361-364`; `forecast_google_hourly` still last 2026-07-19) | display-only |
| FC-6 matched_lead_emos string | not done (low priority) | |
| FC-7 day-0 leak | not done (`dataset_research.py:183-191` no `lead_hours` predicate) | both locks still hold |
| FC-8 SFO lstm | not done (`parser.py:63` default `lstm`; box env `lstm`) | |
| FC-9 GraphCast/AIFS | **partial** — fetch refactor; `NWP_MODELS` unchanged (8, AIFS in, GraphCast deliberately out); all 8 models 240 rows each since 9/5 | **Residue**: `dataset_forecast_features` has **0 rows** for either AI model; `dataset_runs` since 9/5 collect only `best_match`, `gfs_hrrr`, `ncep_nbm_conus`. The PR #110 evaluation still has not started. |
| FC-10 non-final obs sigma | not done (`probability.py:332` = 0.6) | |
| FC-11 misc | not done (`nwp_archive.py:458` logic identical; `window_rows` untouched) | |

Forecaster production sanity since 9/5: `sfo-forecaster-refresh` 59 runs, 0 failures; freshness `OK: forecast DB fresh (0.1h old)`; `n_models` = 8 on all 135 live rows; no new tables in either DB.

### 3d. Site

| Item | Status | Live evidence |
|---|---|---|
| SITE-1 false banner | **fixed** — `publication.tsx:92-144` operational freshness now from `manifest.published_at` (`clock="publication"`), strategy from artifact time; threshold still 15 min | `published_at 2026-09-06T19:42:21Z`; "Published data" → no match; no `role=alert`. Margin thin (10-min cadence vs 15-min threshold); brief suggested ≥25. |
| SITE-2 readiness stale | **fixed (option b)** — `ReadinessPanel.tsx:19-22,37,45-47,60,76-78`; `StrategyLabView.tsx:180` | Renders "Readiness analysis has not been refreshed since the last deploy-time analysis run / ANALYSIS NOT REFRESHED / This does not mean the checks newly failed…". Hero strip still prints raw `ANALYSIS STALE · live orders disabled` (different component). |
| SITE-3 cached counters | **partial** — `strategy.ts:588-593`; `OpsHealth.tsx:82-83,99-103,113` "Historical counts as of …"; `GateFunnel.tsx:20-27,56,60-62` "Cached gate counts as of …" | Still unmarked: SelectivityFinding "Of 373,737 gate evaluations this window only 187,085 (50.06%) survived…" and ProfileDashboard "GATE APPROVALS · WINDOW 205 of 116,305" under "updated 2026-09-06 19:50 UTC". `gateDeferred()` (`strategy.ts:799`) still ignores `decision_analytics`. Grammar: "model-vs-market gap was across 211,221 snapshots". |
| SITE-4 six label/cross-account defects | **not done** — `CityGrid.tsx`, `cities_report.py`, `ProfileDashboard.tsx`, `SkillStrip.tsx`, `ForecastDial.tsx` have no commits; `summary.py:1384` still `reason[:48]` | Live: `CityGrid` still `live.open_positions + research.open_positions`; `cities_report.py:229,237` still counts `PAPER_LIMIT_RESTING`; "HIT RATE 87.2% · 95–14 / RESOLVED TRADES 109"; reason renders "…same-" (twice); TrackRecordFinding "+$27.90 … cross-profile total"; city cards "8 MODELS" on post-intraday highs; hero "History yrs · 3,419 KSFO days / Forecast σ °F" undated; hero method string raw. |
| SITE-5 label fixtures | **not done** — `fixture` absent from src/bundle | manifest still `2026-07-09T21:48:53` for both. |
| Copy drift from #114 | — | `SystemHighlights.tsx:30` "optional external inputs when fresh" contradicts Methodology's "does not currently use them"; `:47` "every 5 minutes"; Methodology "runs in 15 market" (missing plural). |

**Deployment note:** the frontend bundle was built at 21:54 PDT 9/4, 7 minutes **after** the #114 commit and 27 minutes after `synced_at`, so the #114 copy IS live while `build_info.json`/manifest provenance says `2a6432e3b` — the provenance stamp is inconsistent with what shipped. No backend file diverges.

### 3e. Improvements

| Item | Status |
|---|---|
| IMP-1 ladder ledger | **not implemented** — no module, no table (33 tables in `paper_trading.db`, 10 in `weather.db`, all pre-existing). |
| IMP-2 agreement gate | **not implemented** — `config.py` `max_model_market_gap` 0.20/0.25, `edge_gate_uses_model_probability: True`, `market_prior_weight 0.45` / `min_model_weight 0.35` unchanged; `risk.py` unchanged. |
| IMP-3 disagreement size multiplier | **not implemented**. |
| IMP-4 exits | done via TC-4/TC-5. |
| IMP-5 shallow clone | done (OPS-1). |
| IMP-6 retention | one-time only (OPS-2). |
| IMP-7 depth-aware cross | **not implemented** — `execution.py` unchanged; `_taker_cross_quote` (`:112`) single-level; `:149` `$1` notional floor; config 1.0. |
| IMP-8 per-station dispersion | offline pilot only. |
| IMP-9 LOW series | **not implemented** — `git grep "KXLOWT\|temperature_2m_min\|MINIMUM"` → empty. |
| IMP-10 posterior_kelly re-key | **not implemented** — `posterior_kelly.py:129 cohort = temperature_cohort(high)`, `:132 acc[0] += 1.0` per row. |

### 3f. The Apple branch (`codex/apple-history-ml-evaluation`, draft PR #116)
Three diagnostic pieces, zero serving changes: `forecaster/apple_history_probe.py` (one authenticated WeatherKit hourly GET, counts only, no values retained); `apple_weatherkit.py --probe-only/--baseline-db` (read-only pairing count; after merge every scheduled Apple refresh prints one extra line); `scripts/weather_ml_experiment.py` (offline `HistGradientBoostingRegressor` residual model on the 8-member NWP export, no Apple data, no DB writes). Measured on 2,658 paired cases Jun 6–Sep 3: ML vs bias-corrected EMOS MAE 1.6046 vs 1.6430 (−2.3%, CI includes 0); **CRPS 1.1849 vs 1.1820 (+0.25%, worse)**; 80% coverage 88.9% (over-wide); NYC CRPS +0.199 (CI entirely above 0). Author's conclusion: no promotion. ToS memo: a retained Apple archive is "not an authorized conclusion"; a draft inquiry to Apple is written, not sent. **Safe to merge (CI green, MERGEABLE), does not need a deploy, remediates nothing.**

---

## 4. What the fixes changed in production (9/5 04:30Z → 9/6 19:50Z)

Parent orders only. Notional = filled × limit. Equity = 1000 + cumulative realized + EOD unrealized.

**Live**

| UTC day | approved mkt-sides | orders | req | filled | fill % | filled $ | realized | equity EOD |
|---|---|---|---|---|---|---|---|---|
| 9/1 | 4 | 4 | 11 | 11 | 100 | 10.2 | +0.10 | 1051.83 |
| 9/2 | 5 | 6 | 78 | 13 | 17 | 12.1 | +0.15 | 1051.69 |
| 9/3 | 6 | 6 | 15 | 15 | 100 | 12.8 | +0.35 | 1052.21 |
| 9/4 | 9 | 8 | 61 | 29 | 48 | 26.5 | −0.25 | 1052.11 |
| **9/5** | 5 | 4 | 49 | 13 | 27 | 11.6 | **+1.24** | 1053.97 |
| **9/6** (partial) | 3 | 1 | 4 | 4 | 100 | 3.4 | **+1.64** | 1055.03 |

**Research**

| UTC day | approved | orders | req | filled | fill % | filled $ | realized | equity EOD |
|---|---|---|---|---|---|---|---|---|
| 9/1 | 18 | 43 | 4,217 | 183 | 4.3 | 169 | +0.74 | 1050.78 |
| 9/2 | 15 | 27 | 2,737 | 274 | 10.0 | 257 | −0.06 | 1066.58 |
| 9/3 | 17 | 23 | 1,736 | 254 | 14.6 | 222 | +10.59 | 1074.94 |
| 9/4 | 18 | 24 | 2,954 | 57 | 1.9 | 49 | +15.13 | 1093.53 |
| **9/5** | 17 | 26 | 2,481 | 78 | 3.1 | 60 | **+14.46** | 1100.45 |
| **9/6** (partial) | 14 | 15 | 1,404 | 4 | 0.3 | 3 | **−5.44** | 1091.96 |

- Live realized went from +$0.09/day (9/1–9/4) to +$1.44/day (9/5–9/6), all take-profits, zero stops. Order count (4–5/day) and fill rate unchanged. Since inception: **+$55.03 over 43 days = $1.28/day**.
- Research 9/5 is settlement of pre-fix entries; 9/6 −$5.44 is three floor/catastrophic stops on sub-0.70 entries. Fill rate collapsed to 3.1% and 0.3% (posted $2,000 and $1,257, filled $60 and $3). Since 8/1: **+$87.98 over 37 days = $2.38/day** (best +51.40, worst −41.74).
- Nothing in the fix set touches admission or fills, and the data shows no change there.

Exit mix: edge-reversal stops 2 live + 5 research pre-fix → **0** post-fix. Research NO favorites ≥0.73 hold to settlement by design (9 orders / 1,560 `HOLD_SETTLEMENT_FIRST` rows). `HOLD_NO_MODEL_READ` = 0 with the heartbeat on.

---

## 5. The 5%-per-day question

The owner's target is 5% of capital per day. On $1,052 that is **$52.62/day**. This section states what the data and the literature say, so the next agent does not spend effort chasing it.

### 5a. Compounding
| Horizon | Multiplier | $1,052 becomes |
|---|---|---|
| 14 days | 2.0× | $2,084 |
| 30 days | 4.3× | $4,548 |
| 90 days | 80.7× | $84,961 |
| 180 days | 6,517× | $6.86 M |
| 365 days | 54,211,842× | $57.05 B |

Doubling time 14.2 days. Renaissance Medallion, the best documented record in finance, averaged 66% gross per year 1988–2018 = **0.139%/day**. The target is 36× Medallion's daily rate, sustained.

### 5b. What documented prediction-market traders actually earn
| Source | Capital | $/day | %/day | Note |
|---|---|---|---|---|
| Gu et al., arXiv 2607.06166 (Jul 2026), live Kalshi taker bot, 26 days | $200 → $360.67 | $6.18 | 2.3% compounded | 2,657 shares total, $26.31 fees (16% of profit). The capacity ceiling is in plain sight. |
| Bürgi, Deng & Whelan (UCD, Jan 2026), 313,972 Kalshi contract prices | population | — | makers +2.6% per contract-cycle on 50c+; **+1–2% at 90–99c**; all makers −9.6%, all takers −31.5% | Std dev 33%. Weather has the smallest favorite-longshot bias of any category. |
| Polymarket weather leaderboard all-time (fetched 9/6) | undisclosed | ≤ ~$500/day upper bound | — | #1 +$322,910 on $4.69 M volume (6.9% of volume). |
| "automatedAItradingbot" Polymarket wallet | undisclosed | ~$145/day over ~610 days | — | +$88,481 on $3.3 M volume (2.7%). |
| Northlake Labs Kalshi weather postmortem (Feb 2026) | "a few hundred dollars" | negative | — | 0 wins / 32 losses buying <15c. |

No Kalshi weather market maker publishes a P&L ledger. The one advertorial claiming ~4%/day (WeatherBot.fi, "$700 to $85,000") has no dates, trade count, or wallet.

### 5c. Kelly bound
A perfect 5c edge on a 92c NO contract has full-Kelly growth of 2.19% per resolved bet (quarter-Kelly 0.80%). 5%/day requires several independent full-Kelly-sized fills every day; a book with a 7% fill rate on $2–20k/day city markets cannot supply that.

### 5d. Verdict
**5%/day is not a stretch target. It is not available on this exchange at this capital.** Realistic: **$5–15/day (0.5–1.5%) on ~$1,050** once fills improve; **$30–80/day on ~$10k**. $52/day becomes reachable at roughly **$10k of capital at 0.5%/day**, not at $1k. Even the low end of that is 4–10× Medallion's daily rate and should be expected to decay as the vertical grows (Kalshi weather volume +500% YoY) and bots arrive.

---

## 6. Where the volume actually is — measured 2026-09-06 19:50Z

Kalshi public API (`api.elections.kalshi.com/trade-api/v2`): 104 `KXHIGH*`/`KXLOW*` series; 576 open markets across 48 series (all 48 have both T and T+1 open); 58,272 trades pulled for the 15 traded series. Favorite side = best bid ≥ 0.70: 430 bins (403 NO, 27 YES).

| Universe | bins | 24h contracts | 24h $ | spread mean/median | no ask | depth ≤2c (fav side) |
|---|---|---|---|---|---|---|
| All 48 series | 430 | 760,795 | $713k | 2.6c / 1c | 188 | 3.01 M |
| 15 traded HIGH | 141 | 604,423 | $568k | 1.6c / 1c | 56 | 2.02 M |
| 15 traded, bid ≥0.90 | 113 | 510,040 | $494k | 1.3c / 1c | 56 | 2.02 M |
| 15 traded, 0.70–0.90 | 28 | 94,382 | $74k | 2.9c / 1c | 0 | 6,662 |
| LOW series (all) | 223 | 87,467 | $82k | 3.4c / 2c | 100 | 518k |
| 15 traded, **T (same-day)** | 74 | **578,544** | $544k | 1.8c | | |
| 15 traded, **T+1 (day-ahead)** | 67 | **25,879** | $24k | 1.4c | 9 | 29.1k |

**96% of the volume the book could touch is in same-day (T) events**, which live cannot enter (`min_lead_days=1`) and research never clears (0/360 same-day market-sides approved post-fix, all on the LCB floor). Day-ahead volume is front-loaded: 11.7k of 34k contracts in the first hour after the 14:00Z open. Raw depth is dominated by 0.98–0.99 queues (e.g. NY-T80: 11,168 contracts bid at 0.99) — not capturable size; the position cap binds everywhere.

**Maker spread-capture ceiling** (size = min(depth ≤2c, cap/price), capture = size × spread/2 × fill rate):

| Universe | cap | @7% fill | @30% fill |
|---|---|---|---|
| 15 traded HIGH | $30 | **$1.2/day** | $5.2 |
| 15 traded HIGH | $100 | $2.9 | $12.5 |
| All 48 series (incl. LOW, no model) | $30 | $9.0 | $38 |
| All 48 series | $100 | $19.8 | $85 |

**Edge-capture ceiling** (measured ¢/contract by band × every capturable contract in 0.70–0.98):

| Universe | cap | contracts | all captured | @7% | @30% |
|---|---|---|---|---|---|
| 15 traded HIGH | $30 | 2,286 | $123 | **$8.6/day** | $37 |
| 15 traded HIGH | $100 | 5,878 | $292 | $20.4 | $88 |
| 15 traded, ≥0.90 only | $30 | 1,425 | $38 | $2.7 | $11 |
| 15 traded, 0.70–0.90 only | $30 | 861 | $85 | $6.0 | $26 |

**Flow bound** (API tape, last 24h, day-ahead only, NO-seller prints ≥0.70 that can fill a resting NO bid): **6,954 contracts/day** in the calibrated bands. By series: LAX 2,028, BOS 1,668, MIA 1,228, NY 550, SFO 483, DEN 386, DAL 348, AUS 308, CHI 222, ATL 209, HOU 207, SEA 176, PHX 135, OKC 119, **PHIL 6**. At live ¢/c: $386/day at 100% capture, $39 at 10%, $97 at 25%. (T+1 had traded ~6 h at sweep time; scale by ~1.5–2× for a full day-ahead window.)

**What must be true simultaneously for $52/day at the measured ~6.3c/contract:** (a) ~830 contracts/day filled in the 0.70–0.98 NO bands (today: live 4–29, research 4–274) = ~$720/day filled notional = ~70% of the book turned every day (today live 1%, research 5–25%); (b) fill rate ≥25–30% on ≥$3k/day posted, or ~$3–4k of capital at 7% fills; (c) the ¢/contract edge holding at 10–60× current fill volume — it is measured on 907 (live) and 1,958 (research) contracts total, and marginal maker fills are the ones a seller chose to give you; (d) a 6–12% share of all day-ahead NO-seller flow across 15 cities, every day, first in queue; (e) no losing days (research's worst day −$41.74 = 80% of a target day).

### Per-contract economics (settled + closed since 7/6, NO side, slice-correct)

| band | live ¢/c (win%) | research ¢/c (win%) | post-fix live | post-fix research |
|---|---|---|---|---|
| 0.70–0.80 | **+12.4** (94) | +7.6 (74) | +14.3 | +21.6 |
| 0.80–0.90 | +7.5 (81) | **+11.9** (72) | +7.5 | +11.7 |
| 0.90–0.95 | +3.4 (90) | +1.5 (62) | +5.5 | +6.1 |
| 0.95–0.98 | +2.0 (92) | +4.1 (75) | +3.6 | −1.6 |
| <0.70 | — | **−3.3** (54) | — | **−21.7** |

Blended calibrated-band edge ≈ 6.1c/contract live, 6.3c research ≈ 7% of filled notional.

---

## 7. Levers to increase frequency and volume, ranked by conservative $/day at current capital

Levers 1–5 are execution/config and do not require a model change. 6–9 are structural. 10–12 are explicitly NOT levers.

| # | Lever | Mechanism / evidence | Conservative $/day | Clock |
|---|---|---|---|---|
| 1 | **Post where NO-seller flow exists** | Research posts ~$2k/day; 6 of 33 expired research orders sat in PHIL (6 contracts/day of seller flow) while LAX/BOS/MIA carry 70% of day-ahead NO-seller flow. Reweight research posting by measured per-series seller flow (the API tape or `dataset_kalshi_trades` inside windows). +2–4 pts of fill rate. | **+$1–3** | research only, none |
| 2 | **Quote at the 14:00Z day-ahead open** | A third of day-ahead volume prints in the first hour; the 5-min scan cadence and 15-min order life miss part of it. Trigger a scan at 14:00:xx Z and extend TTL for the opening hour. | **+$1–2** | none (timer) |
| 3 | **Fix REG-1 and the OPS-4 class** | Every research tick that crashes in the 05:00–07:00 UTC window skips remaining cities; that is the pre-open window where D+1 candidates are being admitted. | +$0.5–1 (avoided loss of ticks) | none |
| 4 | **Live position cap $30 → $100** (`max_position_risk_pct` 0.03 → 0.10) | Sizing is not what binds live (3–9 approvals/day) but tripling size on ~13 filled contracts/day at 6c is real money. Depth at best is 12 contracts, 38 one tick below. | **+$0.8–2** | resets live |
| 5 | **Add the 5 untraded US HIGH series with volume** | LV 32k, MIN 15k, DC 12k, SATX 10k, NOLA 8k contracts/day. Registry rows + backfill (settlement tokens verified 8/31: LV=CLILAS/KLAS, MIN=CLIMSP/KMSP, OKC done, NOLA=CLIMSY/KMSY, SATX=CLISAT/KSAT, DC=CLIDCA/KDCA). ≈ +30% of whatever the book makes. | **+$0.5–1.5** now, scales | none for research |
| 6 | **Taker fallback when edge > spread + fee + buffer** | 93% of signaled research volume never executes at bid+1. Taker fee at 92c is 0.52c vs measured 2–12c edge. Bürgi et al. eq. 7–8: agents with strong beliefs should take, not post. The only live academic Kalshi record (+80%/26 days) was a pure taker. Cap at edge ≥ 2× spread to limit adverse selection (arXiv 2502.18625: fills that come fast are the ones you did not want). This is IMP-7 (depth-aware cross, scoped p<0.95) generalized. | **+$1.5–4** | resets live; research first |
| 7 | **Horizon shift** | arXiv 2602.19520 (4.4 M Kalshi weather trades): calibration slope 0.69 at 0–1h, 0.87 at 6–12h, 0.97 at 24–48h, 1.20 at 2d–1w — prices too extreme inside 12h, too conservative at 2d+. Rest NO bids on T+1 as early as possible after the 14:00Z open (favorites underpriced); do not buy favorites inside 12h; inside 12h consider fading the extreme. Kalshi lists only T and T+1, so "2d+" is the T+1 opening window. | unquantified, likely +$1–3 | research first |
| 8 | **LOW series (IMP-9)** | 223 favorite bins, 64 with ≥5c spreads (87k contracts/day), 3–8× the spread of HIGH series. **$0 without a Tmin model.** Pipeline is variable-agnostic (one comparator in `reconstruct_daily_max`, `temperature_2m_min` in the Open-Meteo call, `MINIMUM` regex in `clisfo.py`, registry rows). Shadow 30 days first. | $7–8/day by the spread model at 7%/$30 **once modeled** | none in shadow |
| 9 | **Same-day markets** | 578k contracts/day = 96% of touchable volume. Research 0/360 approved on the LCB floor; live barred by `min_lead_days=1`. The only pool with $52/day-scale flow. Unlocking it is a **model** change (same-day probability with the running max + remaining-hours distribution, FC-2 lead-0 sigma, per-station non-final obs error FC-10), not an execution change, and it is where the calibration evidence says favorites are overpriced. Highest ceiling, highest risk. Requires IMP-1 (ladder ledger) to validate. | unquantified; the prize | resets |
| 10 | Walk one tick | **Not a lever.** Re-checked post-fix: 33 expired research orders (3,406 contracts), NO-seller prints at ≤ our price 7, at +1c 14, at +2c 97. Queue ahead was 0–10. The limiter is absence of seller flow at our price, not queue position. | +$0.2 | — |
| 11 | Relax live `edge_lcb ≥ 0` | 42 in-band mkt-sides/1.6 days with edge ≥0.012 but LCB <0 (≈25/day vs 3 approved). +$3–6/day **optimistic**, but the <0.70 band evidence (−3.3c, −21.7c post-fix) says loosened gates lose money, and the 9/3 brief §7 says do not. | do not | resets |
| 12 | Widen favorite band / remove daily pause / YES favorites | Measured 0 trades/day each (9/3 brief §7). YES favorites ≥0.90: 15 bins, all 0.98–0.99, model edge −0.21 to −0.32. | do not | — |

Also worth one check: **Kalshi's Liquidity Incentive Program** pays $10–$1,000/market/day pro-rata to resting size when both sides hold ≥ Target Size (100+ contracts), extended to Jan 1 2027 (CFTC filing Feb 11 2026). If any KXHIGH market carries the Rewards badge (kalshi.com/incentives rate-limited during research — unverified), two-sided ladder quoting becomes pure yield independent of edge. Verify before building.

---

## 8. Recommended work order

1. **REG-1** — scanner-side station-clock lead + `ResearchEntryLimitError` on the three raises + test change. ~1 hour.
2. **OPS-3** — set any webhook. Five minutes. Every failure since 9/3 was silent.
3. **OPS-2** — enable nightly bounded deletion (`_materialized_batched_delete` exists) or schedule the quiesced run weekly. Deadline ~Sep 17.
4. **FC-1** — debias the spread in all three places; re-derive `max_source_spread_f` from debiased units against `decision_snapshots`. Unblocks SFO/LAX (currently 0 live orders, ~56% of their rows vetoed). Resets the live clock — batch with 5.
5. **Lever 4 + Lever 6 (IMP-7)** — position cap and depth-aware/taker fallback in one live behavior release, bump `STRATEGY_BEHAVIOR_VERSION`.
6. **Levers 1, 2, 5** — research posting reweighting, 14:00Z open trigger, five new HIGH series. No live clock cost.
7. **IMP-1 ladder ledger** — prerequisite for anything same-day (lever 9) and for IMP-2/3.
8. **FC-4** — disable the Apple timer (no consumer, cannot be archived) and either fix or stop the non-SFO Google refresh (100% 4xx, still billed ~140 events/day).
9. **FC-2** lead-0 sigma, **IMP-10** trust-model re-key, **TC-6/9/15** — correctness backlog.
10. **SITE-3/4/5** and the #114 copy drift — one small frontend PR.
11. **IMP-9 LOW series** in shadow, 30 days, then decide.

### Data-quality notes for whoever measures next
- `dataset_kalshi_trades` is fetched only during resting-order windows (441 rows for 9/1–9/6); it is not a volume record (NY-T77: API 370 vs DB 2). Inside the windows it matches the API exactly, so the fill model is fed correctly.
- Partial exits write child `paper_orders` rows (`parent_order_id` set); use `parent_order_id IS NULL` for counts, `filled_contracts` for fills, `contracts` for slice P&L.
- `paper_monitor_snapshots`/`paper_orders` timestamps are ISO with `T` and `+00:00`; SQLite `datetime()` emits a space — string comparisons silently miss.
- Live `decision_snapshots` carry no `scan_run_id`/`lead_bucket`; day-ahead is inferred from the `min_lead_days=1` reason text.
- Depth snapshots (`market_orderbook_depth_snapshots`) exist only for tickers the research scan quotes — biased sample.
- The order-book sweep above is one Sunday 19:50Z snapshot; T+1 `volume_24h` covers ~6 h. Re-run against a weekday 15:00Z snapshot before prioritizing.
