# Win-trades volatility retune + deploy reliability + honest readiness (2026-06-18)

Follow-up to `two_profile_comfort_edge_readiness_2026-06-18.md`. A read-only audit
(8 dimensions, strategy-critical claims adversarially re-verified by re-running the
walk-forward backtest) confirmed what was real, corrected some framing, and drove
the changes below. Goal priority from the owner: **win trades**, then **deploy more
capital / more volatility (stop making $1-2)**, then **rational, proven, correctly
implemented strategy**. Everything stays paper-only; no real-money order path exists.

## What the audit confirmed
- **Correct, untouched:** the 4->2 profile collapse + idempotent DB migration; the
  comfort-edge gate/sizing math; the readiness fail-closed logic; comfort-edge IS
  live on the production `analyze` path.
- **The "$1-2" P&L is mostly the tail-basket's hardcoded `$5/$1` stakes**, not Kelly
  under-sizing — on real `analyze` trades the $50 per-position cap bound below
  quarter-Kelly's $87-128 want.
- **Forecaster beats climatology** (re-run walk-forward: overall Brier 0.55 vs the
  climatological prior 0.76; normal cohort 0.54 vs 0.66). The honest problem is not
  "no edge" — it is that the readiness gate's flat `Brier < 0.25` bar was unreachable
  on interior 2°F bins by any calibrated model, so it judged the wrong thing.
- **Real bugs:** tail-basket bypassed the warm/hot regime block; readiness tagged
  cohorts by settled-high while the live block gates on forecast-high; the gh-pages
  publish raced between two timers; no after-fee EV is validated and the journal was
  never pulled locally.

## Changes

### Deploy reliability (WS1)
- `publish_forecaster_pages.sh`: `flock` serializer + bounded re-fetch/retry loop;
  "lost the race" is success, not a failed unit.
- Atomic artifact writes (temp + `os.replace`) in `report.py`, `strategy_research.py`,
  `build_dashboard.py` so the publisher never copies a half-written file.
- `run_paper_scan_profiles.sh`: `flock -n` overlap guard (skip-if-running).

### Confirmed code bugs (WS2)
- `tail_basket.py`: thread `forecast_high_f`/`forecast_sigma_f` into both legs so the
  live warm/hot regime block (and comfort) actually bind.
- `backtest_rescore.py`: readiness now scopes per-cohort/side checks by the **forecast**
  cohort (`by_forecast_cohort`), so a forecast-normal/settles-warm day no longer pins
  READY unreachable; added **per-cohort and per-side after-fee ROI > 0** checks.
- `backtest.py` + readiness: replaced the flat `Brier < 0.25` bar with **Brier Skill
  Score > 0 vs a climatological prior** per traded cohort (`backtest.py` now computes a
  per-bin climatological prior and BSS).
- `config.py`: `research.max_forecast_age_hours` set explicitly to 30h (was silently
  inheriting live's 12h); `comfort_edge_sigma_floor_f` 2.46 -> 3.0 (full-confidence
  distance ~7.5°F ≈ p93 of realized error, so a calm day can't collapse the band).
- `risk.py`: comfort-edge now also blocks **near-forecast YES coin-flips** (live only),
  mirroring the NO block; YES is never size-boosted.

### Sizing & volatility (WS3) — all PENDING walk-forward validation
- Tail-basket legs size off the risk budget (`--basket-sizing kelly`, default in the
  scan) instead of a fixed $5; basket bounded by raised `MAX_SPEND=60 / MAX_WORST_LOSS=50`
  and still a non-negative lower-bound edge per leg.
- `live` caps raised: `max_position_risk_pct` 0.05->0.08, `max_event_risk_pct`
  0.08->0.12, `max_target_exposure_pct` 0.12->0.18; `fractional_kelly` 0.25->0.30.
  Worst-case single-day loss bounded to ~18% of equity. The positive after-fee
  `edge_lcb >= 0` floor is UNCHANGED, so bigger size is never negative-EV.

### Validation + honest readiness (WS4)
- New `trading/deploy/aws/pull_paper_db.sh` pulls the live journal down (the only
  inbound half; `sync_to_box.sh` excludes the DB) so `backtest-rescore` +
  `compute_real_money_readiness` can run on real settled data.
- Readiness card text + checks now reflect the skill + per-side/per-cohort-ROI gate.

## Timer cadence design (evidence-backed)
The only timer *defect* was the publish race (now fixed in the script, so cadences need
not change to dodge it). Cadences are otherwise tuned for the owner's win/volume goal —
faster where it catches entries, not slowed for marginal efficiency.

| Timer | Setting | Why (evidence) |
|---|---|---|
| dataset-backfill | daily 02:25 | once/day consolidation, off-peak. |
| forecaster-refresh | 2×/hr day, 1×/hr night | matches ~hourly source model runs; ~190 Google events/day < 260 budget; inside the 12h staleness gate. |
| paper-monitor | every 2 min | 1 `get_market`/open position; timely stop/take-profit; 401/403 re-raise prevents stranded positions. |
| **paper-scan** | **every 5 min (kept)** | the entry-maker. Forecast refreshes only 2×/hr and entries dedupe per market/side, so 10-min would be *marginally* cheaper — but 5-min catches a newly-listed bracket and its far-tail NO sooner, which serves the win/volume goal. Now overlap-guarded by `flock`. Useless after the 14:00 same-day cutoff (rolls to next day). |
| paper-settle | every 30 min (kept) | settlement is a once-daily CLISFO event; 30-min is cheap and promptly catches the publish. |
| strategy-lab-refresh | every 5 min (kept) | dashboard freshness; the publish race it caused is now fixed by flock+retry in the publish script, so no schedule change needed. |

## Honest status
- READY is correctly **unreachable today**: ~6 independent days vs the 30 floor, empty
  local journal. The path: `pull_paper_db.sh` -> `backtest-rescore` under `live` ->
  accumulate >=30 independent settled days (the `research` collector feeds calibration
  data) -> the skill + ROI gate flips READY only when genuinely earned.
- The sizing/volatility numbers are deliberate proposals; validate on the first
  walk-forward after-fee rescore before treating any as final, and before real money.
