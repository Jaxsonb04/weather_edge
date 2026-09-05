# WeatherEdge audit and remediation brief — 2026-09-03

**Audience: an engineering agent tasked with fixing these findings. Read the whole "Constraints" section before touching anything.**

Runtime audited: `c3cbf3195` (tip of `main`, and the exact revision deployed to production).
Production box: `i-06b30e0c893b2597a`, us-west-1, t4g.medium, TZ `America/Los_Angeles`.
Method: six audit agents (trading core, forecaster, ops/AWS, public site, production-DB forensics, external research), then 33 verification and adjudication agents whose instructions were to **refute** each claim against live code and current production data.

Every claimed *mechanism* below was confirmed to exist in the code. Roughly half of the originally-claimed *impacts* were cut to near zero by the refute pass. Where that happened it is stated explicitly, because acting on the original claim would waste effort. Findings carry one of:

- `CONFIRMED` — mechanism and consequence both hold.
- `PARTLY_TRUE` — mechanism holds, stated consequence corrected inline.
- `REFUTED` — see §7, do not act.

Nothing in production was modified during the audit. No file edited, no DB written, no unit restarted, no deploy attempted.

---

## 0. Constraints — read before any change

1. **Deploys are impossible right now.** `trading/deploy/aws/backup_paper_db.sh:109-121` requires `available >= db_bytes + 1 GiB`. Measured 2026-09-03 17:41 PDT: needs 31.01 GB, has 26.37 GB, **short 4.64 GB**, widening ~1.35 GB/day. Every fix below is blocked behind OPS-2 or a manual file sync. Do not assume you can ship.
2. **The box is CPU-throttled** (72–78% steal since 09:50 PDT 2026-09-03). Anything you run there is slow and competes with the live trader. Bound every DB query with a `created_at` predicate and `timeout`; open the DB read-only (`file:...?mode=ro`). Two unbounded scans during the audit drove load to ~10 and had to be killed.
3. **Paper-only.** `SFO_LIVE_TRADING_ENABLED=0`, `SFO_LIVE_TRADING_DRY_RUN=1`, no authenticated order client. Do not change that without the owner.
4. **The two paper accounts are economically separate** (`paper-live-stability-v1`, `paper-research-roi-v6`). Never combine their P&L into one bankroll. `motion-v1` and `target-v1` are archived.
5. **Evidence-clock hazard.** `account.py:173 strategy_fingerprint()` hashes `asdict(StrategyConfig)` + `entry_mode` + account policy. **Exit parameters are in neither fingerprint** (`StrategyConfig` has zero exit fields; `research_policy.py:43-65 policy_fingerprint()` has none). So an exit change will *not* rotate the hash and will *not* visibly reset the readiness cohort — it will silently blend two regimes into one cohort that looks continuous. Treat any live-account behavioral change as a reset even when nothing forces one, and prefer research-only changes.
   - Already contaminated: PR #111 changed `probability.py` (live approvals ~2/day → 58/day) and `probability.py` is not in `StrategyConfig`, so the current 4-day cohort already spans two strategies.
   - Reset cost is at a 6-week low: live fingerprint `5556f8e1ce1a` starts 2026-08-28 with 4 target days / 19 orders against a 30-day bar. The book has never reached 30.
6. **`backtest_rescore.run_rescore` is exit-blind** — it scores pure held-to-settlement P&L and models no stop, veto, or take-profit. It would certify research on +$279 of settlement P&L while the account realized +$67. Do not use it to validate an exit change.
7. Access follows the documented operator hop; the production box is not reachable directly. Keep workstation hostnames and absolute local paths in ignored operator state.

---

## 1. P0 operational — active incident, owner action required

### OPS-1 `CONFIRMED` — CPU credits exhausted; publish job re-clones full gh-pages history every 5 minutes
**Files:** `trading/deploy/aws/publish_forecaster_pages.sh:134-155`, `:223`, `:227`; `trading/deploy/aws/systemd/sfo-operational-publish.timer`

`git init` into a fresh `mktemp -d`, then `git fetch origin gh-pages` with **no `--depth`**, then `git checkout -B` — every cycle. Measured on the box:

| Quantity | Value |
|---|---|
| CPU per run | 10m12s–10m52s (throttled); 128–130s unthrottled |
| Of which `git index-pack` | 97–153% CPU for 400+s, `--pack_header=2,109353` |
| Pack downloaded per run | 339,579,420 bytes |
| Egress | 2693.6 GiB over 43.65 days uptime = **61.7 GiB/day** |
| Share of instance | 8.3 of ~11 CPU-hours/day |
| Cadence | `OnUnitActiveSec=5min` vs 388–478s runtime → runs back-to-back, 245 runs/24h |

t4g.medium baseline is 20% average across 2 vCPU. Measured steady state ~23% → credits drained → exhausted 09:50 PDT 2026-09-03, `%steal` 71–78% every interval since (was 0.00 the previous day).

Downstream today: `sfo-strategy-lab-refresh` 5 timeouts (`TimeoutStartSec=120`; CPU/run 11.6s → 87.5s), `sfo-scheduler-health` "scheduler repair failed", `sfo-forecast-freshness` STALE, one SSH timeout.

**Fix:** `git fetch --depth=1 origin gh-pages`, or keep a persistent clone under `/opt/weatheredge/.cache/pages` and fetch incrementally. Additionally re-orphan the branch periodically — history grows +6 objects per publish, 288 publishes/day. Expected: index-pack is >90% of the job, so 8.3 CPU-h/day → <1, instance 23% → ~7% (under baseline, credits refill), egress → <0.5 GiB/day, wall time 7min → <1min.

**Also change:** `OnUnitActiveSec=5min` → `OnCalendar=*:02,12,22,32,42,52` (10-minute, offset from scan and strategy-lab which both fire at `:00/:05/...`). Give trading units priority: scan/monitor `Nice=0`, publish/strategy-lab `Nice=10` + `CPUWeight=50`. Currently all share `Nice=5`, so the dashboard publisher competes evenly with the trading tick.

**Owner-only immediate mitigation:** switch the instance to `unlimited` credit mode in the AWS console (~$2–3/month at the current ~3% over baseline). The instance role cannot read CloudWatch and the Mac has no AWS credentials, so an agent cannot do this.

### OPS-2 `CONFIRMED` — retention never deletes; deploy gate already failing
**Files:** `trading/deploy/aws/run_archive_then_prune.sh:17`; `/etc/weatheredge.env`

`PRUNE_MODE="${SFO_PRUNE_MODE:-archive-only}"` and `SFO_PRUNE_MODE` is **unset** in `/etc/weatheredge.env`. The env does set `SFO_PRUNE_FULL_DAYS=1`, `SFO_PRUNE_DEDUP_DAYS=45`, `SFO_ARCHIVE_KEEP_DAYS=30` — all dead. Nightly log (2026-09-02, 09-03): `prune gate ok` → `foreign key audit ok` → `DEGRADED: archive/upload/gate/FK complete; scheduled live-DB deletion skipped`. The unit still costs 15min wall / 3m27s CPU / 1.7 GB peak per night to delete nothing.

| Quantity | Value |
|---|---|
| DB | 29,934,735,360 B (was 18.7 GB on 8/16) |
| Growth | 0.674 GB/day |
| Free list | **0 pages** — `compact_paper_db.sh` would reclaim nothing |
| Deploy gate | needs 31.01 GB, has 26.37 GB, short 4.64 GB, widening 1.35 GB/day |
| 85% watchdog | ~24.6 days (≈ 2026-09-28) |
| Volume full | ~39 days |
| Reclaimable elsewhere | ~1.5 GB (logs 999 MB, apt 321 MB, archive ring 733 MB) — not enough |

Row inflow per UTC day: `decision_snapshots` 125,640; `probability_snapshots` 30,312; `scan_context_snapshots` 10,470; `paper_monitor_snapshots` 7,292; `market_snapshots` 5,052; `forecast_snapshots` 5,052. Live table sizes: `decision_snapshots` 4,102,302 rows since 2026-06-10, probability 1.79M, monitor 631k, scan_context 380k, market 299k.

**Fix (needs an owner decision on what evidence is kept — every archived row is verified in S3 first, so deletion is recoverable):** set `SFO_PRUNE_MODE` to the bounded delete mode. `db.py:6317+` already batches deletes with 2s lock holds and `busy_timeout=30s`. With `full_days=1 / dedup_days=45` the dedup keeps one row per `(market, side, day, profile)` — measured 3,000 → 348 rows, 88% reduction → growth 0.67 → ~0.15 GB/day. Then a one-time `VACUUM INTO` brings the file to ~8–10 GB. **Chicken-and-egg:** compaction needs free space equal to the DB, which does not exist — route it through the archive-restore path or a temporary EBS grow.

**Secondary (blob bloat, do after the prune):** `market_snapshots.raw_json` is 18.3 KB/row × 5,052/day = 92 MB/day of the *same* Kalshi event payload re-stored per tick per city; `paper_monitor_snapshots.diagnostics_json` 11.5 KB/row = 84 MB/day; `scan_context_snapshots` ~7.8 KB/row = 82 MB/day. Content-hash market payloads once per `(event, payload)` and reference by id — the scan already dedups scan-context by `source_context_hash`. Cap monitor diagnostics to deltas. ~250 MB/day (≈40%) less inflow before pruning.

### OPS-3 `CONFIRMED` — every failure alert is a no-op
**File:** `trading/deploy/aws/send_systemd_failure_alert.sh:8-11`

`SFO_FRESHNESS_ALERT_URL=` is empty, so the script exits 0 with `warning: ... alert was not sent`. All 14 `OnFailure=sfo-alert@%n.service` hooks alert nobody, including OPS-1 and OPS-2. The journal shows exactly that line for every invocation in the last 7 days. **Fix:** set a real webhook. This is the cheapest change in the document and the reason the incident ran unnoticed.

### OPS-4 `CONFIRMED` — scan unit fails on an unhandled research exception
**Files:** `trading/sfo_kalshi_quant/db.py:3275`, `:3280`; `cli.py:384-386`; `trading/deploy/aws/run_paper_scan_profiles.sh:2`

Four failures (2026-09-02 07:00:20, 14:40:46; 09-03 07:21:02, 07:30:45), all identical:
```
NO forecast source spread 11.0F exceeds max 10.0F; point blend is unreliable
error: research strategy entry limits are violated
sfo-kalshi-paper-scan.service: Main process exited, code=exited, status=1/FAILURE
```
A research leg passes the allocator, then fails the canonical-quote entry-limit re-check, and `db.py` **raises** instead of skipping the leg. `cli.py:384-386` maps any exception to exit 1, and `set -euo pipefail` with research running second fails the whole unit even though the live tick already completed successfully.

Ruled out: zero sqlite lock/exit-75 lines in 7 days; zero OOM kills in 14 days of `journalctl -k`; `MemoryPeak` 30 MB vs `MemoryHigh=400M`; swap 53 MB of 2 GB.

**Fix:** skip the offending leg with a recorded reason rather than raising; or run the two profiles independently so one profile's failure cannot mask the other's success.

### OPS-5 `CONFIRMED` — freshness watchdog races the artifact swap
**Files:** `trading/deploy/aws/check_forecast_db_freshness.sh:73-86`, `:112-115`; `run_publication_cycle.sh:41-55`

The publication cycle takes `SFO_ARTIFACT_GENERATION_LOCK`, `mv`s the new `strategy_research.json` (`:48`), then rebuilds the manifest (`:53`). The freshness check validates artifact-vs-manifest checksums **without taking that lock**, so it can read the new file against the old manifest → `checksum mismatch: strategy_research.json` → false STALE, which also writes the `STALE_FORECAST` marker and fires the (dead) alert. Observed 2026-09-01 20:45:39, 09-02 17:15:38. **Fix:** take the artifact lock in the validator.

### OPS-6 `CONFIRMED` — scheduler health has three separate fragilities
**File:** `trading/deploy/aws/check_scheduler_health.sh:57`, `:199`, `:232-235`, `:376-390`

- Waits only `SFO_SCHEDULER_VALIDATION_LOCK_WAIT_SECONDS=10` for the artifact lock while the publish job holds it for its whole 7-minute build → `artifact validation lock is busy` (2026-09-02 01:33:21). Fixing OPS-1 largely resolves this; also raise the wait.
- A single GitHub Pages `503` with no retry is treated as a scheduler failure (2026-09-01 07:18:16). Add a bounded retry.
- On failure it prints only `app-privileged artifact validation failed (status=1)`; the inner script's stderr survives only because it happens not to be redirected. Propagate the captured output.

### OPS-7 `low` — housekeeping
- `sqlite_stat1` says `decision_snapshots` has 914,768 rows; actual is 4,102,302 (4.5×). Run `ANALYZE` after the OPS-2 prune. Plans still pick indexes today, but cost estimates for the strategy-lab `GROUP BY` shapes are 4× off.
- `journald.conf.d` sets `ForwardToSyslog=yes` → everything written twice (syslog 260 MB + 128 MB rotated, journal 561 MB). Scan alone logs ~1,228 lines / 184 KB per run = 35.5 MB/day. Set `ForwardToSyslog=no`, raise `SystemMaxUse` to 1.5 G so the journal covers ≥7 days instead of 2.6 (a "4 days ago" root-cause was impossible during this audit).
- `/opt/weatheredge/.cache/main` is a stale checkout at `5bc9113` (2026-07-25); runtime is rsynced, so it is dead weight.
- `forecaster/.google_weather_usage.json` frozen at 2026-07-19; the real ledger is `google_weather_usage_events` in `weather.db`.
- Orphan `-shm`/`-wal` sidecars from 2026-08-28 in `trading/data/backups/`.

**Verified clean, do not re-audit:** box runtime == git `c3cbf3195` byte-for-byte (md5 of all 30 `forecaster/*.py` and all 156 `trading/{sfo_kalshi_quant,deploy}` files: 0 differences); 29 canonical units byte-identical to templates; `build_info.json` and `publication_manifest.json` provenance both carry the deployed sha.

---

## 2. Trading-core defects

### TC-1 `CONFIRMED` — `place_arbitrage` bypasses the entire account risk policy
**File:** `trading/sfo_kalshi_quant/paper.py:842-935`; `db.py:4045-4074`
**Severity: highest latent dollar exposure in this document.**

`account_policy_capacity` has exactly one production caller — `paper.py:743` inside `_fit_to_account_policy`, reached only from `place_approved` (`paper.py:706`). `place_arbitrage` records straight through `store.record_paper_order(...)` after only a pause check (`:857`) and a per-target exposure check (`:891`). `record_paper_order` verifies only that the account row is `ACTIVE` with matching capital.

Skipped: the 15% drawdown pause, the position cap `min($30, 3%·equity)`, `AGGREGATE_RISK_PCT` (20%), `CITY_TARGET_PCT` (5%), `REGION_DAY_PCT` (8%), and `available_cash`. Orders are written with `status=None` → `PAPER_FILLED` immediately at the ask.

Failure scenario: equity down 16% so `policy_capacity` would return `allowed_spend=0.0`, but the day's realized P&L is only −$3 so the profile breaker has not tripped. With `SFO_PORTFOLIO_MAX_ARB_SPEND=12` per opportunity and `_target_exposure_remaining` = 18% of bankroll per city-day, `PAPER_CITIES=all` (15) × `PAPER_ROLLING_TARGETS=3` → up to **~$8,100 of new exposure on a $1,000 book while the account is supposed to be paused**, with cash free to go negative.

Latent, not dormant by design: `SFO_PAPER_SCAN_ARBITRAGE_ENABLED=1` in production, but `SELECT ... FROM paper_orders WHERE group_id IS NOT NULL` returns **0 rows** — no arbitrage group has ever priced through, which is why nobody noticed.

**Fix:** route `place_arbitrage` through `_fit_to_account_policy`, or call `account_policy_capacity` and clamp before recording.

### TC-2 `CONFIRMED` — the exit-drag tool resolves settlements by date alone on a 15-city book
**File:** `trading/sfo_kalshi_quant/clv.py:189-197`, `:49-56`

```python
rows = conn.execute("SELECT target_date, settlement_high_f FROM paper_orders "
                    "WHERE settlement_high_f IS NOT NULL").fetchall()
highs: dict[str, float] = {}
for target_date, high in rows:
    highs[target_date] = float(high)      # last row for the date wins
```

`settlement_truth.py`'s own docstring states the violated rule: "The same calendar date can settle fifteen different markets. Every lookup is therefore keyed by `(series_ticker, target_date)`." `posterior_kelly._date_settlement_highs` (`posterior_kelly.py:156`) and `store/market_day_settlements.py:425` both do it correctly.

Measured on production: **37 of 53 settled dates carry more than one distinct city high, spreads up to 44 °F.** Every `won`, `counterfactual_hold_pnl`, `exit_drag` and `cohort` in that report is computed against an arbitrary city's temperature.

Two compounding defects in the same file:
- `clv.temperature_cohort` (`:49-56`) claims to mirror `config.temperature_cohort` but has **3** buckets (`<=69`, `<=79`) versus config's **4** (`<60`, `<70`, `<80` → cold/normal/warm/hot). `by_cohort` cannot be compared to any gate keyed on config cohorts.
- `_authoritative_highs` reads only `paper_orders.settlement_high_f`, stamped only on rows that *reached* settlement — 233 of 468 traded market-days, biased toward days where something was held. It ignores `market_day_settlements`, built expressly for this.

**Why this matters most:** this is the instrument the exit policy (`PAPER_*_TAKE_PROFIT_PCT`, `MODEL_VETO_*`) was tuned with. Fix it before trusting any exit analysis. **Use `weather.db:cli_settlements` as truth** (complete, 465/465 station-days per month, all final) via `settlement_truth.settlement_key_for_market`.

### TC-3 `CONFIRMED` — the allocator's "daily" loss budget refreshes ~12,960 times a day
**Files:** `trading/sfo_kalshi_quant/portfolio.py:83`, `:157`; `_cli/scan.py:900`, `:1811`, `:1842`

```python
max_daily_loss=bankroll * 0.08,                                    # :83
if sleeve != "arbitrage" and directional_spend + spend > limits.max_daily_loss + 1e-9:
    reasons.append(f"{decision.ticker} skipped: directional risk budget is full")   # :157
```

`directional_spend` is a local initialized to `0.0` at the top of each `allocate_portfolio` call; `allocate_portfolio` runs once per `(city, target_date)` inside `_portfolio_scan_one_target`, which `cmd_portfolio_scan` invokes in a `for city … for target …` loop. `_worst_case_loss(selected)` is likewise scoped to one plan. The `config.py` comment justifying the `max_position_risk_pct` 0.08→0.03 change reasons explicitly in daily terms ("the same $84 daily budget admits ~2-3 legs"). In reality: 15 cities × 3 targets × 288 ticks ≈ **12,960 refreshes/day**. Nothing in `portfolio.py` enforces the daily cap it is named after.

**Fix:** persist the day's directional spend per profile per settlement day and check against that, or rename the constant and move the real cap to the account policy.

### TC-4 `CONFIRMED` — the "edge reversed" branch exits winners as stop-losses
**File:** `trading/sfo_kalshi_quant/exits.py:245-268`

When `tp_net is not None and net_exit >= tp_net` but `net_exit < entry_cost`, the code returns `ExitSignal("STOP_LOSS", "edge reversed: ...")` — **before** the configured stop floor is ever evaluated.

Settlement-truth measurement across both accounts: **33 exits, 33/33 would have won at settlement, cost $55.57.** 100% wrong-sided.

Note: the claim that this "bypasses the model veto" is technically true but vacuous — in that branch `p = tp_net <= net_exit < entry_cost <= veto_floor`, and `_validate_monitor_args` (`monitor.py:100`) forbids a negative buffer, so the veto could never have held the position. Skipping it changes no decision. The *stop floor* bypass is the real defect.

**Fix:** this is the one exit change that needs no further evidence. Require a minimum reversal magnitude and a fresh model read (cap read age at ~30 min for this branch, versus the 90-minute default) before firing, or route it through the ordinary stop floor.

### TC-5 `CONFIRMED` — take-profit sells at the model's own fair value with a zero buffer; live has no settlement-first guard
**Files:** `trading/sfo_kalshi_quant/exits.py:236-268` (`convergence_buffer: float = 0.0` at `:184-186`, `:158`); `monitor.py:149-153`, `:480-495`

`tp_net = convergence_take_profit_net(model_side_probability, buffer=convergence_buffer)` and `monitor.py`'s sole call site never passes the buffer. Production reasons read literally `edge captured: net exit 0.957 >= fair value 0.957`.

`_settlement_first_no_min_cost_for_order` returns `DEFAULT_RESEARCH_NO_SETTLEMENT_FIRST_MIN_COST` (0.73) for research and **`None` for live**, so the `HOLD_SETTLEMENT_FIRST` branch (`exits.py:247-257`) is dead code on live. Since 2026-09-01: research logged **2,840** `HOLD_SETTLEMENT_FIRST` and 0 take-profits; live logged **0** settlement-first holds and 9 take-profits. Over 30 days all 72 live position exits were early closes (71 at entry cost ≥ 0.73 — the exact cohort the guard protects) against 2 orders that reached settlement.

**Do NOT extend the settlement-first guard to live.** This was the original recommendation and adjudication reversed it:
- Coverage is backwards: **0 of 47** research take-profit exits are at cost ≥ 0.73 (the guard already removes those), while **73 of 74** live take-profit exits are — so it is inert where the leak is and blankets the account where holding is roughly break-even.
- Live's model is calibrated to mildly over-confident at its band (contract-weighted model p 0.9483 vs settle rate 0.9056); research's is under-confident by 13–22 points at 0.4–0.7. **Hold beats take-profit iff `P_true > p`** — true for research, false for live.
- It would delete the exits that truncated live's tail: order 2191 (HOU 26AUG10, NO @0.782 ×38, actual high 87 °F so NO was worthless) turned **−$29.73 into +$7.05**.

**Fix, research only:** add a positive exit margin `tp_net = min(1.0, model_p + margin)` with `margin = 0.05`, wired through a per-profile resolver returning `None`/`0.0` for live exactly like the existing settlement-first threshold, and exposed as an env-backed CLI flag. Do **not** overload the existing `convergence_buffer`: it *subtracts*, so reusing it requires a negative value its docstring does not contemplate.

**Lockstep requirement:** the per-profile resolver is duplicated at `monitor.py:149` and `strategy_lab/paper_card.py:1128-1134` (call site `:1269`). Both must move together or the dashboard shows an exit target the monitor will never act on.

### TC-6 `CONFIRMED` — three risk controls that can never fire
- **`account.py:62`, `:104-105` — the 2% live-account daily loss pause is unreachable.** `place_approved` (`paper.py:592-599`) calls `store.paper_entry_pause_reason(...)` and returns before any capacity check. That breaker uses `PAUSE_THRESHOLDS["live"] = (10, -0.35, 0.010)` (`db.py:202`) = 1.0% of `_sizing_bankroll` = `clamp(equity, 500, 2000)`, against the same rows, same TZ boundary, same predicate. At every equity level the profile breaker trips at exactly half the account breaker's threshold (today: −$10.52 vs −$21.05). `DAILY_LOSS_PCT`, its reason string, and its feeding query (`db.py:2136-2146`) are dead code.
- **`account.py:112`, `:136-138` — `MAIN_SLEEVE_PCT` (16%) is algebraically a no-op.** `active_rows` (`db.py:2147-2161`) binds only `paper-shared` and `paper-live-stability-v1`, but every research order lives in a separate account since the v3 cutover, so `research_risk` is structurally 0 and `sleeve_room = 0.16E − aggregate + 0.04E = 0.20E − aggregate = total_room`. Verified: zero open `paper-shared` rows, zero research-profile rows in the live account. The only binding cap is `AGGREGATE_RISK_PCT`.
- **`portfolio.py:84`, `:143-145` — the live YES sleeve is $4.00 across all 15 cities.** `yes_sleeve = bankroll * 0.08 * 0.05` = 0.4% of bankroll, versus research's `0.25 * 0.20` = $50. Live's per-position budget is ~$30, so any normally-sized YES leg is dropped on the first evaluation. Masked because no live YES candidate has ever been signal-approved (89,508 YES decision rows since 2026-09-01, 0 approved; `paper_orders` for the live account is 204 rows, 100% NO, for the account's entire life). **The moment the upstream YES gate is relaxed this becomes an invisible ceiling.**
  - Invisible because `_portfolio_decisions_for_recording` (`_cli/scan.py:1476-1502`) rewrites every allocator-dropped reason to the generic `"portfolio not allocated by shared risk budget"`. "YES sleeve is full" cannot be distinguished from "directional risk budget is full" in the journal. **Fix this too** — it blinds every future allocator diagnosis.

### TC-7 `CONFIRMED` — joint-Kelly resize discards the sleeve accounting it was sized under
**File:** `trading/sfo_kalshi_quant/portfolio.py:223-277` (`joint_kelly_enabled: True` for live, `config.py:427`)

`_joint_resize_directional` replaces each leg's contract count with `fractions[key] * bankroll` and then re-checks **only** `_worst_case_loss(candidate) > max_daily_loss`. The `yes_sleeve` (`:143`), `explore_sleeve` (`:146-153`) and running `directional_spend` (`:157`) checks are not re-applied, despite the docstring claiming the result is bounded by them. A YES leg admitted at $3.90 can be resized to $60 with a single-scenario worst case still under $80. Currently bounded only because `_fit_to_account_policy` re-clamps to $30 at placement — the account cap, not the sleeve, applied after the allocator already spent its budget on pre-resize numbers.

### TC-8 `CONFIRMED` — fee rounding uses centicents on every production path
**File:** `trading/sfo_kalshi_quant/fees.py:6`, `:39-41`, `:44-53`, `:81`

`quadratic_fee_total` takes `_ceil_position_plus_fee` (rounds to `FEE_ROUNDING_UNIT = 0.0001`) **whenever `series_ticker is not None`** — which is every production call site (`paper.py:754`, `execution.py:85/136/175/258`, `monitor.py:441`, `db.py:3994`, `exits.py:89`). `ceil_to_cent`, which implements the documented cent rule, is reachable only when `series_ticker` is omitted.

Example: 5 contracts at $0.85 → raw fee $0.0446. Documented rule → $0.05. Actual → $0.0446. Understated by $0.0054/order, up to $0.0099 worst case, on both entry and exit. Live has 122 `ENTRY_FILL` + 133 `EXIT_PROCEEDS` events (~$2.5); research ~4×. Small absolutely, but **the after-fee `edge_lcb >= 0` floor is the book's stated EV guarantee and it is measured against fees the exchange would not charge.**

Secondary: `arbitrage.py:266-274` (`_leg_for_contracts`) and `:394-407` (`_group_spend`) call the fee functions **without** `series_ticker`, so the guaranteed-profit approval test uses the cent rule while `record_paper_order` re-derives cost with the centicent rule — they disagree by up to $0.01 per contract-set on exactly the quantity `min_profit = $0.01` is testing.

### TC-9 `CONFIRMED` — independent paper books compete for one finite public tape
**File:** `trading/sfo_kalshi_quant/db.py:4897-4904`

Only `paper-research-shadow` is treated as counterfactual; `roi-v6`, `target-v1`, `motion-v1` and live all land in `capital_orders`. `claims` (`db.py:4820-4841`) aggregates maker volume claims per `market_ticker` **across every account**, so a research fill permanently removes tape from the live book's future passes. `maker_fills._priority` sorts `(-limit_price, placed_at, order_id)` across accounts, and the research target sleeve's `max_position_risk_pct` is 0.09 vs live's 0.03, so research outranks live at lower prices.

Claimed tape to date: non-live capital books **3,793 contracts** vs live **193**. Realized damage is small so far (only 4 trades split across accounts; 0 live expired orders overlapped a non-live allocation within TTL), but two contended cases show the mechanism: trade `228409e4…` gave research order 1902 41 contracts and live order 1901 only the 6-contract residual. **The live book's fill rate — the number a real-money decision rests on — is not measured in isolation from books that would not exist.**

### TC-10 `CONFIRMED, low impact` — cross-profile contamination of the exit model read
**File:** `trading/sfo_kalshi_quant/db.py:2551-2586`, caller `monitor.py:460`

`_latest_model_probability_from_decisions` selects the newest `decision_snapshots` row for `(target_date, market_ticker)` with **no `risk_profile` predicate**, even though the order row's own `risk_profile` is used two lines above for fee config. The monitor iterates both profiles' open orders in one unit. It is not a race: live scans first and research writes ~9–10s later on every tick, so a **live** position's take-profit fair value and NO-side stop-veto floor are **always** set by the **research** profile's probability.

Impact today is $0: live holds no SFO positions (last live SFO order 2026-07-15) and the six open live positions are in cities where the cross-profile probability difference is 0.0003–0.0018. It only bites on SFO, where live uses the residual blend and research uses EMOS (mean 6.2¢/contract apart, max 10.3¢). **Fix anyway** — add the profile predicate; it is a one-line correctness fix guarding a latent SFO re-entry.

### TC-11 `CONFIRMED, low impact` — comfort band scaled by a range, not a sigma
**Files:** `_cli/scan.py:421`; `risk.py:649`

`_cli/scan.py:421` passes `forecast.source_spread_f` under the keyword `forecast_sigma_f`; `risk.py:649` consumes it as a standard deviation. `source_spread_f` is a max-minus-min **range** in both meanings (across blend sources for SFO, across 8 NWP models elsewhere). The calibrated EMOS sigma sits unused in the same object (`forecast.raw["emos"]["sigma"]`) and is already a live local three lines above the offending dict.

Measured 2026-09-02: 34,130 of 60,624 live decision rows carried a comfort-edge block, 14,214 of them attributable to range-vs-sigma scaling — but **0 of 34,130 had comfort-edge as their sole rejection reason**, so this costs 0 trades/day and $0/day today. The far-tail NO size boost also runs at 1.204× against a 1.417× design intent. Fix as correctness, not as a volume lever.

### TC-12 `CONFIRMED` — research lead-day check uses the Los Angeles civil day
**File:** `trading/sfo_kalshi_quant/db.py:3685`

`lead_days = (target_day - civil_day).days` where `civil_day = _research_objective_day()` = `now.astimezone(ZoneInfo("America/Los_Angeles")).date()`, while `_rolling_live_event_targets` and `_paper_entry_gate_for_target` both use `settlement_clock(now, city)` (the station's fixed-standard offset). Whenever a station's clock has rolled past midnight but LA's has not, a same-day market is stamped `day-ahead` and passes `min_lead_days=1`. The window is `(station_standard_offset − LA_civil_offset)` hours, not a fixed 3.

Measured 2026-08-01..09-03: **12 of 552 orders (2.2%)** in `roi-v6` admitted as day-ahead while same-day at the station. Nine resolved for **−$10.11** (avg −$1.12/order) against +$73.95 (avg +$0.43) for the 172 genuine day-ahead orders — about −$0.30/day, cutting sleeve realized P&L from $73.95 to $63.84. n=9, so the per-order gap is not individually significant, but the sign matches the project's prior finding that same-day entries underperform.

### TC-13 `CONFIRMED` — same-day exit blindness after the 14:00 cutoff
**Files:** `_cli/scan.py:285-286`; `db.py:2530`

After each city's fixed-standard 14:00 cutoff the scanner sets `min_target` to tomorrow, so today's `decision_snapshots` stop (measured 2026-09-02: 4,176/hr through 18:00 UTC → 720 at 21:00 → **zero from 22:00 UTC**). `latest_model_probability_read`'s 90-minute window then expires, so from ~15:30 station-standard until market close every open same-day position is evaluated with `model_side_probability=None`. That is 5.5 hours for Pacific cities, 8.5 for Eastern. Measured: 2,214/2,214 such evaluations had a null model read; **3,402 of 22,186 exit evaluations (15%) since 2026-09-01 occur in the blind window and produce zero exit decisions**, versus 23 exits from the 18,784 read-regime evaluations.

In that state the convergence take-profit is replaced by an unreachable `cost*(1+0.35)` target (unreachable for 100% of the 3,393 affected NO positions), and the NO stop returns `HOLD_NO_MODEL_READ` below the catastrophic floor.

**No money has been lost to it yet:** all 71 orders that rode the entire unmanaged window (2026-08-02..09-03) settled as winners for +$166.33 with no losers, and `HOLD_NO_MODEL_READ` produced zero rows in that period. This is the window where the high is already set and the bid sits near 0.99. The YES limb has zero production exposure (the book is 100% NO).

**Fix:** `PAPER_SAME_DAY_MODEL_HEARTBEAT_ENABLED` already exists (`monitor.py:351`) and production sets it `false` (`sfo-weather.env.example:178` ships it false). Enabling it is the intended remedy. Verify it does not rotate the strategy fingerprint before enabling. Do this **before** TC-14 or any same-day admission change.

### TC-14 — live volume: what actually binds (do not act on the wrong lever)
Measured funnel, live, day-ahead, distinct `(target_date, market, side)`, 2026-09-01..03:

| Stage | NO | YES |
|---|---|---|
| all candidates | 336 | 336 |
| tradeable (ask/bid/sizes/cost) | 328 | 328 |
| + favorite band [0.70, 0.97] | 256 (−72) | 5 (−323) |
| + spread, model/market gap, posterior | 253 | 4 |
| + `min_edge >= 0.012` | 112 (−141) | 0 |
| + `edge_lcb >= 0` | **15 (−97)** | 0 |
| approved | 14 | 0 |

- **`edge_lcb >= 0` kills 87% of survivors** and is the dominant limiter. `min_edge` is second at 56%.
- **Favorite band costs exactly 0 trades/day** — all 72 of its NO removals fail `min_edge`/`edge_lcb` anyway (verified: rows outside the band satisfying every other numeric gate = 0 on every day). YES dies at `min_edge` (0 of 328), not at the band.
- **Daily-loss pause costs 0 trades/day.** Blocked rows 2026-09-01..04: 0. Over 43 live settlement days since 2026-07-01 exactly one day exceeded −$10 (2026-07-10, −$12.42); next worst −$2.20. Removing it buys nothing and deletes a drawdown control.
- **Lead rule costs ~1 trade/day.** Rows where it was the *only* block: 1, 2, 7 over three days = 1 distinct opportunity/day. (The "384 rows with `edge_lcb >= 0`" figure reproduces but is a snapshot count, and those rows average 26–28¢ point edge because they are late-day rows where the intraday high has already resolved the market — which is what the nonfinal-certainty gate exists to catch.)
- **Approval→order conversion is ~100%** (4/5/6 distinct approvals → 4/6/6 orders). The "79 approvals/day" figure is a 15-minute snapshot count, not opportunities.
- **The real size limiter is top-of-book depth**: mean 6.4 contracts displayed against a ~90-contract recommendation, across 158 live orderbook snapshots since 2026-09-01. Within 1¢ there are 34.2 (5.3×), within 2¢ 68.9 (10.8×), across 3 levels 207.2.

**Do NOT widen the favorite band and do NOT loosen `edge_lcb >= 0`.** The only production measurement of the cohort they guard is research's 0.70–0.80 band since 2026-08-01: **58 orders, 626 contracts, −$41.74 = −6.7¢/contract**, realized win rate 62.1% against a modelled 70.0%. Widening would reset the evidence clock to buy a measured loss.

### TC-15 `PARTLY_TRUE` — live discards the un-crossed remainder (impact much smaller than first claimed)
**Files:** `execution.py:131` (`_taker_cross_quote`), re-applied by `paper.py:1245-1254` (`_clamp_to_displayed_ask`), gated by `paper.py:690` (`if quote.would_cross`)

The clamp is real: `contracts = floor(min(contracts, ask_size))` and the remainder is dropped with no resting leg. All 14 live taker fills since 2026-09-01 show `requested_contracts == floor(entry_ask_size)`.

**Corrections to the original claim, which overstated this ~10×:**
- The originally cited location (`execution.py:275-295`) is `target_research_quote`, the **research** path. Live's clamp is at `execution.py:131`.
- Resting maker quotes are deliberately *not* ask-capped and keep full size (2 of 16 orders in the window, at 32 and 33 contracts).
- The 1,483 → 104 → 39 arithmetic is right but the attribution is not. `min(NORMAL_POSITION_CAP=$30, 3%·equity)` caps every position independently and downstream, so the account's own ceiling for those 16 orders was ~528 contracts (~$480), not 1,483. Over the 2026-08-01 window the **$30 cap removes 3,501 of 4,934 discarded contracts (71%)** and the ask clamp removes 1,433 (29%).
- Realistic value of resting the remainder at the live book's own measured resting fill rate (8.5%): **~$0.16/day**, not "4× the book". The 100%-fill upper bound is ~$1.90/day. Trades/day are unchanged either way.

Related real defect: `execution.py:373` (`with_buy_limit`) sets `expected_profit = quote.edge * decision.recommended_contracts` and never lowers `recommended_contracts`, so live decision snapshots record ~90 contracts / ~$84 for an order that will be 2 contracts / $1.77 — **live `expected_profit` is overstated ~25×**. The research path does this correctly (`recommended_contracts=quote.contracts`, `binding_constraint="visible_ask_depth"`). Fix the bookkeeping regardless of the sizing decision.

**Preferred fix is not "rest the remainder" — see IMP-7.** Live maker orders have filled 0 of 65 contracts; research maker orders 844 of 11,538 (7.3%). Resting parks reserved capital in orders that die.

Separate small defect: `limit_taker_cross_min_notional = 1.0` refuses any 1-contract cross below ~99¢, because 1 × $0.916 < $1.00. Orders **2611** and **2616** (`entry_ask_size = 1.0`, recommendation 32/33) were refused a guaranteed 1-contract fill and fell through to the maker path as 32- and 33-contract resting orders that filled **zero**. 2 of 16 orders. Drop the floor to 0.

---

## 3. Forecaster defects

### FC-1 `CONFIRMED` — the disagreement statistic never subtracts the known per-model biases
**Files:** `forecaster/emos_forecast.py:129-136` (`_model_spread_f`, max−min); `forecaster/postproc_models.py:171`, `:183` (`_spread`, a **stdev** — the original claim called it a range; substance unaffected since per-model biases differ)

Biases are computed at `postproc_models.py:148` and applied to the mean via `_weighted_debiased_mean`; the disagreement statistic two lines later is taken over **raw** member values. Re-measured on the **current** production forecast DB (not the July snapshot):

| Station | mean cross-model range, raw | debiased | worst member bias |
|---|---|---|---|
| KSFO | 15.1 °F | 6.1 °F | −12.3 °F |
| KLAX | 15.3 °F | 4.9 °F | +11.5 °F |
| others | 5.1–9.3 °F | 3.9–5.3 °F | — |

Coarse-grid members resolve SFO and LAX as ocean. The raw value is published as `model_spread_f`, read at `forecast.py:563` into `source_spread_override_f`, returned by `ForecastSnapshot.source_spread_f` (`models.py:59-60`), and compared against `max_source_spread_f = 10.0` in `risk.py:97-104` (both profiles, no per-city override). It also inflates the EMOS sigma via `variance = var_c + var_d*spread²`.

Trading effect: the gate vetoed 100% of SFO and 97% of LAX candidate decisions over 2026-08-25..28; on 2026-09-02 it was the sole blocker on 763/4,584 SFO rows and 136/9,216 LAX rows. SFO placed **7** and LAX **3** paper orders over 2026-08-13..09-03 against a 24–49 band for eleven peer cities. Also a genuinely wrong published number: KLAX sigma is ~1.0 °F too wide (2.70 published vs 1.73 realized RMSE, 56% over-dispersion), pulling every LAX probability toward 0.5.

**Fix:** subtract `params.biases` before the range/stdev in all three places. **Then re-derive `max_source_spread_f`** — 10.0 was tuned against the raw statistic and will be wrong in debiased units.

### FC-2 `CONFIRMED` — same-day forecasts are served with the day-ahead uncertainty
**File:** `forecaster/emos_forecast.py:462` — `lead_days=max(lead, 1)` with `store_lead_days=lead`

`apply_emos` computes `variance = var_c + var_d*spread²`; only the spread term reflects today, while `var_c` (the irreducible-error intercept) stays at its lead-1 value and never shrinks. The one component that could correct it is off: `SERVE_RECAL_SIGMA = False`. The sigma reaches the trading probability essentially unmodified, because `config_for_city` forces `emos_distribution_enabled` on for every city except SFO and `probability.py:153` sets `sigma = max(emos_sigma, 0.1)`.

Measured (live rows joined to final CLI):

| stored lead | n | bias | MAE | mean σ | mean z² | cov80 |
|---|---|---|---|---|---|---|
| 0 | 240 | −0.18 | 1.26 | 1.98 | **0.65** | 0.89 |
| 1 | 296 | +0.22 | 1.62 | 2.12 | 0.91 | 0.84 |
| 2 | 279 | +0.12 | 1.91 | 2.44 | 1.07 | 0.78 |

Correct lead-0 sigma ≈ 1.98·√0.65 ≈ 1.6 °F. Effect: an over-wide sigma flattens the predictive distribution, so the favorite bin is under-priced and the tails over-priced. On a 2 °F Kalshi bin at trade-time numbers (2.03 served vs ~1.80 calibrated), the favorite bin comes out 0.378 instead of 0.421 — **understated by ~4.4 points**, ~2–4 points after the intraday blend dilutes it.

**Fix:** fit and serve a lead-0 EMOS. Caveat: live rows keep only the last write per target (`INSERT OR REPLACE`), so hour-of-serve conditioning needs an append-only serve log first.

### FC-3 `CONFIRMED` — per-station dispersion is badly heterogeneous, and the archive to fix it already exists
`forecaster/weather.db:forecast_emos_daily_high` holds **13,086 lead-1 and 13,067 lead-2 rows across all 15 stations back to 2024-03**, with `actual_high_f` populated on 12,100+. Pooled, the served Gaussian is nearly perfect: `mean_abs_z = 0.796` at lead 1 against the ideal `sqrt(2/π) = 0.798`. Per station it is not, and the spread is 1.7×:

`KSEA 1.152 | KMDW 1.088 | KNYC 1.082 | KOKC 1.055 | … | KLAX 0.801 | KPHX 0.798 | KDEN 0.693`

KSEA's sigma is ~15% too narrow (over-confident → over-sized); KDEN's is ~30% too wide (under-confident → never clears a gate, never trades). Meanwhile `probability.py:ResidualCalibrator` runs on `min_conditional_samples = 35` / `shrinkage_samples = 70` and `emos_recalibration.correction_for_serve` uses a short trailing window — both starved next to ~800 rows per station.

**This refutes the standing "all calibration is small-sample" premise for the *distribution*.** Standardized residuals are dimensionless, so pooling across stations for the *shape* while fitting a per-station *scale* is statistically clean.

**Fix:** widen `forecaster/emos_recalibration.py:correction_for_serve`'s `window_lead_days`/`TRAILING_WINDOW_DAYS` to draw on the full `rolling_origin_v2` series per station and let `SHRINKAGE_K` do the small-sample work. `trading/.../recalibration.py:fit_recalibration` (bias + `sigma_scale`, guarded to [0.5, 3.0]) is already the right function and is already exercised by `research_candidates.py` as `gaussian-pit-station-lead-v1` — it is simply not on the serve path. Use an exponentially-weighted window: two years spans a regime change.

### FC-4 `CONFIRMED` — two paid forecast services are refreshed on timers and read by nothing
- **Apple WeatherKit** (`weatheredge-apple-refresh.timer`, 4×/day): `AppleRuntimeCache.active_highs` has **zero callers** anywhere outside `apple_weatherkit.py`; the word "apple" appears in no other production `.py`; its own service log prints "live trading weight remains 0"; and the 10-minute purge deletes the cache about an hour after each 6-hour refresh, so the data does not exist ~83% of the time.
- **Google non-SFO refresh** (`weatheredge-google-nonsfo-refresh.timer`, 11:05 UTC): writes only to the TTL runtime store at `/run/weatheredge/google_runtime.db`; the chain that could consume it (`google_runtime_blend` → `google_paired_evidence` → `google_challenger_research_baseline` → `forecast.latest_google_challenger_baseline`) terminates before reaching the served forecast.

Cost: ~7,541 billable Google Weather events/month (August: 5,805 SFO + 1,736 non-SFO against an 8,000 budget) and ~1,800 WeatherKit requests/month, landing in stores nothing reads. **Google has been returning 4xx for every request for three days with no observable degradation anywhere**, which bounds the value of the data at exactly nothing.

**Fix:** either wire them into EMOS as scored members (archive the derived station-day high into `nwp_model_forecasts` as `model='google_daily'` at issue time and let EMOS weight it — note Apple's ToS forbids archiving, so it can only be a live-only member with a learned bias), or disable the timers. Do not leave them as-is.

### FC-5 `PARTLY_TRUE` — the intraday update drags the point forecast down (display-only)
**File:** `trading/sfo_kalshi_quant/forecast.py:353-364`

`adjusted_high = max(anchor, w*anchor + (1-w)*predicted_high)` with `w` rising by hour (0.35 <10:00, 0.50 <12:00, 0.65 <15:00). `remaining_forecast_high_f` comes only from `forecast_google_hourly`, whose `MAX(fetched_at)` in the production forecast DB is **2026-07-19T09:44:30**, and which is only queried for SFO (`if self.city.has_full_blend`). So the anchor is the morning running max alone.

Measured over 6,000+ production snapshots 2026-09-01..04: drag reaches **9.27 °F (Denver)**, per-city averages 1.4–6.2 °F; Phoenix max 6.59 / avg 3.62.

**Correction — no trading impact.** For the 14 EMOS cities `probability.py:152` sets `bias = emos_mu - predicted_high_f` so the traded Gaussian is re-centred exactly on the raw EMOS mean; `:117` centres the residual window on `emos_mu`; `:205` likewise. SFO is skipped outright by the live book and re-centred in research. The cohort gate is empty for every trading city/profile combination, and the comfort band is enabled only in the profile that forbids same-day entries. Over 2026-09-01..04 the drag was present on **1 of 236 approved decisions** ($1.42 of $19,262 recommended spend), and that one was still priced off the EMOS-centred Gaussian.

**Fix as a display/consistency bug:** use `max(observed_high, mu)` for EMOS snapshots (what `probability.py:455` already does internally) instead of the convex pull. The published `cities_data.json` point forecast is currently below the model's own mean during morning hours.

### FC-6 `PARTLY_TRUE` — `matched_lead_emos` promotion is blocked by a hard-coded string (deprioritize)
**Files:** `forecast_challengers.py:171-183` (`statistical_gates_passed = not reasons` at `:175`, then unconditionally appends `"paired after-fee trading replay is not recorded"` at `:176`, `promotion_eligible: False` hard-coded at `:181`); `forecast_scorecards.py:266`, `:271`, `:216`

The code is exactly as described and production publishes `matched_lead_emos` with `statistical_gates_passed: true`, 12,796 cases over 894 days, paired CRPS delta −0.0153, day-clustered CI [−0.0238, −0.0068] entirely below zero, coverage 0.804, and that single block reason.

**Corrections that make this low priority:**
- The flag **gates nothing**. Its only non-test consumer is a report field; the built `dist/` bundle contains zero references to `matched_lead_emos`, `shadow_challengers`, or the block string, so no reader sees it and no pipeline reads it. Promoting a forecast model here is a code change to `SERVE_RECAL_BIAS`/`SERVE_RECAL_SIGMA` in `forecaster/emos_forecast.py`, which the operator has already done once.
- The advertised improvement is measured against the *uncorrected archive* and is **already realized on the live serve path** (box-measured, lead 1, 60 days: MAE 1.4968 live vs 1.5363 archive; mean bias +0.128 °F vs +0.311 °F). The only unrealized delta is the sigma rescale, which prior paired evidence measured as **worse** (pooled CRPS +0.4%).
- `research_experiments` and `research_evidence` are both 0 rows — the evidence step exists in the repo and has never been run in production.

**If you touch it:** wire it as a `research_candidates` entry through `research_operate`/`research_replay` (which already perform paired after-fee walk-forward replay) and record `decision.paired_case_count`, rather than deleting the string. But FC-3 is the better use of the same effort.

### FC-7 `PARTLY_TRUE` — a day-0 analysis feature reaches the dataset accuracy gate
**Files:** `datasets.py:842`, `:1564-1567`, `:1017`, `:1004-1008`, `:1162`, `:1187`; `dataset_research.py:183-191`, `:236-237`

The plain `temperature_2m` series is requested alongside the previous-run series; it is the freshest run, its lead hours are recorded as `0.0`, and it is renamed to `temperature_2m_max`. The candidate query applies **no lead filter** and promotes anything beating the baseline holdout MAE by 0.25 °F — an analysis-time maximum always passes. The day-0 row also carries a fabricated `issued_at` (midnight local of the target day) that is part of the table's PRIMARY KEY, so nightly re-fetches over the 10-day lookback silently overwrite values in place with fresher runs and no record of the true model vintage survives. `backfill_nbm` (`:1162`) and `backfill_hrrr` (`:1187`) pass `previous_days=0`, so **those two sources' entire contribution is the day-0 leak**.

**Correction — zero impact today, two independent locks.** There are 0 matched rows against a baseline whose window ended 2026-05-18 (13 days before the feature window begins), and the only consumer of a promotion (`blend_sources.load_promoted_dataset_guidance` at `DATASET_GUIDANCE_WEIGHT=0.12`) has not executed since 2026-07-19 because the SFO legacy blend path is dead. Fix it before either lock is lifted: add a `lead_hours >= 12` predicate in `_load_forecast_feature_candidates` and drop the plain variable from the previous-runs request.

### FC-8 `PARTLY_TRUE` — SFO's LSTM calibration artifact is frozen at 2026-05-18
**Files:** `/etc/weatheredge.env` `SFO_TRADING_SIGNAL_CALIBRATION_SOURCE=lstm`; `forecast.py:650-665 load_lstm_outcomes`; `forecaster/ab_test_results.json`

Every artifact fact checks out: `"lstm"` is also the hardcoded default in all four `_default_calibration_source()` copies; SFO is the only `has_full_blend=True` city so it alone takes this branch; the file was last written 2026-06-19 by the manual `forecaster/research/ab_test.py`; its 442 daily rows stop at 2026-05-18; no timer regenerates it.

**Correction — SFO does not price its bins with it.** When the Google runtime migration stopped regenerating SFO's legacy `forecast_blend_daily_high` rows (they end at target 2026-07-20 while EMOS runs to 2026-09-05), SFO's point forecast became an EMOS row, and under `emos_active` `p_emp = p_norm`. Mispriced trades from the stale residual law: **zero**. The residue is `edge_lcb` over-conservatism on SFO research entries (`se_sample_n` 96 instead of ~118 at mu=70 → ~0.01 probability units of extra caution) across ~0.6 orders/day.

**Fix:** point SFO at `load_emos_outcomes` like every other city, or enable `emos_distribution_enabled` for SFO. Low priority, but it removes a confusing dead path.

### FC-9 `PARTLY_TRUE` — GraphCast is not in the serving model list
`forecaster/nwp_archive.py:58-67 NWP_MODELS` omits `gfs_graphcast025` while `datasets.py:48` lists it.

**Corrections:** (1) **AIFS is not missing** — `ecmwf_aifs025_single` has been in `NWP_MODELS` since well before PR #110, has 20,244 rows current through target 2026-09-04, and is one of the 8 members of every live EMOS forecast. (2) GraphCast's absence is a **deliberate documented delisting** (commit `89fd9c09f`, 2026-07-10): Open-Meteo publishes it on the previous-runs API with a ~7-week lag and its archive rows end 2026-05-21. (3) PR #110 touched exactly one file for 7 lines and never went near the serving path.

Real residue: 3 days post-merge, `gfs_graphcast025` and `ecmwf_aifs025_single` have **0 collected rows and 0 matched settlements** in the research preset, so the AI-NWP evaluation PR #110 was meant to start has not started. Check the collector actually runs.

### FC-10 `low` — non-final observation error is SFO-fit
**File:** `probability.py:332` — `NONFINAL_OBSERVED_HIGH_SIGMA_F = 0.6`, used by `_nonfinal_bin_feasibility` (`:353-395`) and as the truncation floor at `:518`.

`nws_daily_high_ground_truth.high_f` (max of api.weather.gov obs, ≥20 obs/day) vs final CLI max: **KDEN +0.97 °F (CLI higher on 65% of days), KNYC +0.55 (52%)**, KSFO +0.21 (37%), most others ±0.2. Scenario: Denver 15:00, METAR max 90.0, CLI will read 91 — the bin `[90,92)` is damped as "at the running max" and market-conditioning at `:229-237` double-counts it. **Fix:** per-station sigma or additive offset from exactly that join.

### FC-11 `low` — misc
- `nwp_archive.py:427` `end = today + 1` stores tomorrow's row as `lead_days=1` when it is really ~lead-2. No forecast impact (overwritten later, excluded from serve), but `coverage_report`/`--verify` mislabel it.
- `emos_recalibration.window_rows` (`:151-152`) assumes D−1 truth exists at every serve on D, but the final CLI for D−1 is issued 01:30–04:40 local, so 00:40–~04:00 ticks run with a shorter window than `recalibration_replay.py` validated.

**Verified correct, do not re-audit:** Open-Meteo honors `timezone=Etc/GMT+5` (response `utc_offset_seconds -18000`, 24 hourly stamps in local standard time; `reconstruct_daily_max` groups by `stamp[:10]`); all 15 station/CLI/lat-lon mappings in `forecaster/cities.py` (KNYC 40.7833/−73.9667, KMDW, KHOU Hobby, KDFW, KDEN); unit conversions in `google_api.temp_to_f`, `nws_ground_truth.temp_to_f`, `apple_weatherkit._c_to_f`, Open-Meteo requested in °F; CLI finality (`is_preliminary` regex correctly matches `VALID TODAY AS OF 0400 PM LOCAL TIME.` and the 2:26 AM final has no such line); rolling-origin `truth_lag_days` is leak-safe; prior audit item E.9 is fixed at `emos_forecast.py:489-490`.

---

## 4. Public site defects

The **data is correct**. All 15 city forecasts, sigmas, `n_models`, settled values, the SFO hero, both account balances (live $1,052.40 = cash 1,037.38 + open 15.02; research $1,063.84 = cash 587.22 + reservations 179.04 + open 297.57), open positions, and W-L records reconcile to the production DBs exactly. No console messages, all requests 200, versioned `?v=sha256` fetches working, manifest polled every 60s, deployed bundle current. Methodology claims match backend behavior and contain no stale references to the reverted 8/16 changes.

What is wrong is **freshness presentation**.

### SITE-1 `CONFIRMED` — false "Published data is behind" banner ~50% of the time
**File:** `src/lib/publication.tsx:61` (`OPERATIONAL_MAX_AGE_MINUTES = 15`), `:112` (age measured from `artifact.generated_at`), used at `:226`, `:253`, `:272`

`generated_at` is stamped at the **start** of a publication cycle that takes 6.5–13.7 minutes, and each published manifest carries the **previous** cycle's artifacts. Over a continuously observed 137.3-minute production window (2026-09-03/04) the public age exceeded 15 minutes for **46.8–53.7% of wall time**, peaking at 27.8 minutes, with every cycle logging success and the delivery gate clearing in 0–2s.

While it shows, a red `role="alert"` banner appears and `open_positions`, `open_exposure` and "Eligible decisions · 24h" render as the literal string "Unavailable" across all 15 city cards.

The SPA threshold is also **below the backend's own declared budget**: `check_scheduler_health.sh:66` sets `SFO_PUBLICATION_MAX_PUBLIC_OPERATIONAL_AGE_MINUTES` to 20 with a comment explaining that a tighter public ceiling is unmeetable by construction.

**Fix:** measure age from `published_at` rather than `generated_at`, or raise the threshold to at least two cycles (≥25 min) to match the backend budget. Fixing OPS-1 also shortens the cycle, which helps but does not remove the off-by-one-cycle basis error.

### SITE-2 `CONFIRMED` — readiness is structurally always "analysis stale"
**Files:** `strategy_lab/readiness.py:14` (`MAX_READINESS_ANALYSIS_AGE_HOURS = 36.0`), `:76-82` (staleness short-circuits before any check); `trading/deploy/aws/run_publication_cycle.sh:24` (unconditionally exports `SFO_STRATEGY_FAST_PUBLICATION=1`); `strategy_lab/build.py:189-197`; `sync_to_box.sh:423`

The only producer of `strategy_analysis_cache.json` is `refresh_strategy_analysis_cache.sh`, invoked from exactly one place: the operator deploy script. Zero systemd units and zero cron entries reference it, and a test actively asserts it is absent from the service unit. The recurring timer forces fast mode, which skips the readiness checks.

Measured from gh-pages history: since the gate shipped 2026-08-16T12:04 through 2026-09-04T01:36 (445.5 hours), the cache was refreshed **3 times** = 108 fresh hours. The public "Go-live readiness" panel read "ANALYSIS STALE / Checklist deferred" for roughly **337 of 445 hours (76%)**, worst single run 279.4h (11.6 days). It currently publishes `ANALYSIS_STALE` at 66.7h.

**Fix — needs an owner decision, do not just add a timer.** A recurring refresh converts the gate from "a deploy happened within 36h" to "a timer succeeded within 36h", auto-satisfying a real-money readiness gate without human action, and converts a deploy-only `sudo -n systemd-run` into a standing daily escalation. Options: (a) owner-approved refresh timer with the escalation reviewed, (b) render a distinct "analysis not refreshed since last deploy" state instead of implying the checks failed, (c) raise the threshold. (b) is the honest minimum.

### SITE-3 `CONFIRMED` — Strategy Lab renders September 1 cached counters under a current timestamp
**Files:** `summary.py:331-336` (`_data_collected_from_analysis`, cached `gate_behavior` when `allow_decision_scan=False`); `_mark_deferred_decision_counts`; `src/components/.../OpsHealth.tsx:78-98`, `GateFunnel.tsx`, `StrategyLabView.tsx:87-96`, `:168`

Production runs `publication_mode: fast_public` against a cache generated 2026-09-01T06:52:46Z. The artifact carries `daily_summary.decision_analytics = {status: "cached", ...}` and `counts_stale_from: "2026-08-31"` with a plain-English reason — but **no SPA component and no string in the built `dist` bundle reads either field**. Because the cached branch of `_mark_deferred_decision_counts` is an `elif` that only annotates and nulls per-day rows, the aggregate sections survive untouched and `gateDeferred()` waves them through since their totals are non-zero.

Wrong numbers currently shown under "updated 2026-09-03 23:51 UTC":

| Displayed | Actual for the displayed window | Matches |
|---|---|---|
| Paper orders 29 | 142 | the window ending 2026-09-01 |
| Gate evaluations 848,868 / 22.06% approved | — | cached window |
| Forecast snapshots 34,653 | 33,613 | cached window |
| Monitor snapshots 5,969 | 26,499 | cached window |

**Fix:** read `decision_analytics.status` / `counts_stale_from` in `OpsHealth.tsx` and `GateFunnel.tsx` and render an explicit "counts as of <date>" state instead of live-looking figures.

### SITE-4 `medium` — cross-account and label defects
- `CityGrid.tsx:84` sums live + research open positions into one number (22), which contradicts the project rule that the accounts are economically separate; `cities_report.py:229` also counts `PAPER_LIMIT_RESTING` as an open position, so Overview says 22 while the Lab says "21 open · 2 resting".
- The first Finding block in `StrategyLabView.tsx` renders a cross-profile P&L total (−$21.61) despite the artifact's own `daily_summary.equity_unavailable_reason: "Combined profile P&L spans separate paper accounts"`.
- "HIT RATE 87.0% · 80–12" and "RESOLVED TRADES 92" (`ProfileDashboard.tsx:251-252`, `StrategyLabView.tsx:524`, `summary.py:150-195`): numerically right, but 84 of the 92 are monitor take-profit/stop closes and only **8** ever settled on the CLI. Wins are `realized_pnl > 0` on any terminal order. Relabel as "closed or settled", or split the two.
- `summary.py:1377` truncates the rejection reason with `reason[:48]`, so the site shows `"live paper entry requires min_lead_days=1; same-"`.
- `CityGrid.tsx` labels `predicted_high_f` (post-intraday) as an 8-model forecast; on 2026-09-03 it showed Dallas 93° where the EMOS 8-model mean was 90.7 (the 93.2 is the observed high so far). `predicted_high_f_pre_intraday` is in the artifact and unused. See FC-5.
- The hero KPI "Forecast σ 4.66 °F · SFO held-out residual" and "History 10 yrs · 3,419 KSFO days" (`SkillStrip.tsx:27-28`, `MethodologyView.tsx:109`, `:203`) come from the static June LSTM artifact; the operational EMOS σ today is 2.31 °F. No "as of" date is shown.
- The hero method string surfaces an internal tag verbatim: `"emos wmean (live NWP ensemble) [SFO operational fallback] + intraday…"`.

### SITE-5 `not a bug` — the two July 9 artifacts
`forecast_data.json` and `weather_story_data.json` are **intentionally committed fixtures**, documented at `docs/data_and_artifacts.md:8-11`; git 4d61b0ed8 (2026-06-11), box mtime 2026-06-02. Their producers are offline research scripts (`forecaster/research/forecast_tomorrow.py:151`, `eda.py:256`) that were never on a timer, and `sync_to_box.sh:269-272` actively deletes stale copies from the box. The 2026-07-09T21:48:53 stamp is not a generation time: `publication.py:160-185` carries forward the previous manifest's `generated_at` when the sha256 is unchanged. The SPA still loads both (`data.ts:297-298`) for the climatology chart and two hero KPIs, and freshness logic deliberately ignores them (`publication.tsx:226-229`). **Only action needed:** label them as static fixtures in the UI.

---

## 5. The strategic finding — the market out-forecasts the model

An agent built the **full-ladder outcome ledger that does not exist in the codebase**: `decision_snapshots` (which stores `model_probability`, `market_probability`, `strike_type`, `floor_strike`, `cap_strike`) joined to `weather.db:cli_settlements`, scoring **every offered bin** rather than the traded subset. Result: **1,344 scored day-ahead NO day-markets, all 15 cities, 2026-08-18 → 09-01**, produced in about five minutes of box time.

**The Kalshi price is the better forecaster.** Brier: market **0.1194**, blend 0.1213, model **0.1230**. Log-loss: market 0.3749, model 0.3829.

**Model-market disagreement predicts loss, in both directions.** Bucketed by `model_NO − market_NO`, maker EV per contract at bid+1 after fees:

| gap | n | model | market | realized | EV/contract |
|---|---|---|---|---|---|
| −0.15 | 58 | 0.732 | 0.878 | 0.914 | +3.4¢ |
| −0.10 | 110 | 0.754 | 0.849 | 0.818 | −3.3¢ |
| −0.05 | 209 | 0.819 | 0.867 | 0.861 | −0.7¢ |
| **0.00** | **378** | 0.903 | **0.901** | **0.902** | −0.2¢ |
| +0.05 | 156 | 0.800 | 0.752 | 0.724 | −3.0¢ |
| **+0.10** | **90** | **0.719** | 0.620 | **0.533** | **−8.6¢** |
| +0.15 | 50 | 0.706 | 0.558 | 0.540 | −1.8¢ |

Where they agree (68% of the ladder) realized frequency equals the market to within 0.1 points. Where they disagree by ≥0.10 the model is wrong by 9–19 points and the market by half that.

**The engine assumes the opposite.** `risk.py:TradeEvaluator.evaluate_market` computes edge as `p_model − cost` under `edge_gate_uses_model_probability`, and `max_model_market_gap` (0.20 live / 0.25 research) treats disagreement as a *tolerance*. Kelly then grows size with the claimed edge, so the largest positions land in the worst-calibrated cells: approved live decisions with gap > 0.10 carry `avg_edge` 0.108 and requested **3,166 contracts**.

Corroborating realized data: August settled NO orders, p ≤ 0.70 lost **−$95.55** on 785 contracts from only 4 distinct day-markets; p ≥ 0.85 made **+$134**.

**What survives:** the deep-favorite tail (realized 0.993 vs market mid 0.963 at n=305) and maker spread capture. Both are execution-shaped, not forecast-shaped.

---

## 6. Ranked improvements

Ordering is by expected value against implementation risk. IMP-1 gates the validation of IMP-2, 3, 4 and 9.

### IMP-1 — Build the full-ladder outcome ledger `do this first`
Score every offered bin nightly, not just traded ones. `cli_settlements` has 17,890 rows across 15 stations back to 2015; `decision_snapshots` carries the strike edges; the outcome is a pure function of the CLI integer and the bin edges.
**Code:** new module beside `settlement_day.py` (e.g. `ladder_truth.py`) plus a nightly CLI subcommand writing `(ticker, target_date, side, model_probability, market_probability, entry_bid, entry_ask, outcome)` to a new table. Station mapping exists in `cities.py` (`nws_station_id`); reuse `settlement_truth.settlement_key_for_market`.
**Effect:** ~100× the calibration evidence; prerequisite for validating everything below in days rather than months. **Risk:** CLI parse errors contaminating labels — measured, our CLI value agreed with Kalshi's actual settlement **699 of 700** settled city-days (the miss, KMIA 2026-08-29, was a 5 °F CLI outlier). Add a hard guard flagging any |CLI − Kalshi-implied bin| ≥ 2 °F. **Clock:** no reset (read-only scoring).

### IMP-2 — Gate on agreement with the price, not tolerance of disagreement `research first`
Make the market probability the traded probability and demote the model to a same-sign confirmation filter.
**Code:** `risk.py:TradeEvaluator.evaluate_market` — the `edge_gate_uses_model_probability` branch (`edge_probability = model_probability`) is backwards; compute edge against `market_probability` when one exists. `config.py`: `RESEARCH_PROFILE_OVERRIDES["edge_gate_uses_model_probability"] = False`; `max_model_market_gap` 0.20 → 0.05 live, 0.25 → 0.05 research (the gate already exists, only the value changes). `probability.py:_model_weight` (`:747`): `market_prior_weight` 0.45 / `min_model_weight` 0.35 — the 0.35 floor is what keeps the blend worse than the price; enable `market_consensus_anchor_enabled` with `anchor_min_model_weight → 0.05`.
**Effect:** removes the −8.6¢ cell entirely; retains 28/150 live and 54/135 research approvals at mean ask 0.944/0.971. On the August book this converts −$95.55 of p ≤ 0.70 losses into non-trades while keeping +$134 of p ≥ 0.85. **≈ +$3–4/day**, mostly loss avoidance — the highest-confidence dollar in this document.
**Risk:** approvals drop 60–80% on an already liquidity-bound book; the 0.05 threshold is fitted on 15 days. Mitigate with IMP-9 (breadth), not by loosening it. **Validation:** re-run the IMP-1 ledger with the new gate as a filter over existing `decision_snapshots` — no production code needs to run. Require the surviving cohort's realized frequency ≥ market probability over ≥30 target days and ≥5 cities. **Clock:** resets on live; free on research.

### IMP-3 — Make size shrink with disagreement instead of growing with it
**Code:** in `risk.py:evaluate_market`, after `spend_budget` is chosen, add a multiplier on `abs(model_probability − market_probability)`: 1.0 at gap ≤ 0.03 tapering to ~0.1 at gap ≥ 0.12. Mirror the `comfort_size_multiplier` pattern exactly — multiply `spend_budget`, set `budget_label`, never append to `reasons`. Precedent exists at `risk.py:676` (`_consensus_guard_assessment`), which fires only on a °F-scale gap, the wrong axis.
**Effect:** the August p 0.55–0.675 cohort placed 709 contracts and lost $88; a 0.1 multiplier caps that near −$9. **≈ +$3/day avoided loss, blocking zero trades** — compatible with keeping the research collector's opportunity set intact. **Risk:** same signal as IMP-2 applied twice; if both ship the surviving book is very small. IMP-3 alone is the strictly safer half. **Validation:** replay `paper_orders` with the multiplier applied to `contracts`, recompute realized P&L — pure arithmetic on settled rows. **Clock:** resets on live (new `StrategyConfig` field); free on research.

### IMP-4 — Fix the exit branch that sells winners, and add a research take-profit margin
See TC-4 (unconditional) and TC-5 (research-only margin of 0.05, per-profile resolver, do not touch live). **Effect:** TC-4 recovers $55.57 of demonstrated wrong-sided exits; the margin recaptures a large share of research's take-profit leak while degrading gracefully if the under-confidence closes. **Validation:** for every `CLOSE_TAKE_PROFIT` and "edge reversed" lot, score the counterfactual via `clv.side_won` + settlement truth — **but fix TC-2 first**, or the tool lies on 70% of dates. **Clock:** exit params are in neither fingerprint; see Constraint 5.

### IMP-5 — Shallow-clone the Pages branch
See OPS-1. **Effect:** 8.3 → <1 CPU-hours/day, 61.7 → <0.5 GiB/day, resolves the throttling incident and the health-check lock contention. **Risk:** low. Must reach the box by manual file sync while OPS-2 is unresolved.

### IMP-6 — Turn retention deletion on, then compact
See OPS-2. **Effect:** restores deployability; growth 0.67 → ~0.15 GB/day; file 30 → 8–10 GB. **Risk:** owner decision on retained evidence; compaction needs the archive-restore path or a temporary volume grow.

### IMP-7 — Make the crossing order depth-aware
**Code:** `execution.py:_taker_cross_quote` (lines ~112–160) — let the cross consume up to 2 price levels while the after-fee `edge_lcb` stays ≥ 0 at the *worse* price; drop `limit_taker_cross_min_notional` from 1.0 to 0 in the same change.
**Grounding:** 158 live orderbook snapshots since 2026-09-01 — mean top-of-book 6.4 contracts, within 1¢ **34.2 (5.3×)**, within 2¢ 68.9, across 3 levels 207.2. Only 9/16 live orders carry `edge_lcb ≥ 0.01` (room for one tick) and 5/16 ≥ 0.02. Hand-checked 8 orders against their ladders: 3 had large usable depth (`KXHIGHTHOU-26SEP04-B93.5` NO got 2 at 0.86, with 19 more at 0.87 and 255 at 0.88, both LCB-positive at +6.9¢/+6.0¢), 4 had none, 1 breakeven.
**Effect:** ~same trades, ~6× size → **+$1.5 to +$4.0/day** against a live book earning $0.20/day. This bypasses no decision gate: the candidate is already approved and the LCB floor is re-enforced at the worse price. **Scope it to `probability < 0.95`** — that keeps the extra size in the two bands with genuinely positive settlement-held edge (+11.4¢ at 0.90–0.95, +11.5¢ at 0.80–0.90) and out of 0.95–0.98, where settlement-held EV is **−5.3¢** and the band only looks profitable because exits close early.
**Do NOT instead "rest the remainder"** (IMP-7 alternative 1c): live maker orders have filled **0 of 65 contracts**; research 844 of 11,538 (7.3%). Resting parks ~$400/day of reserved capital in orders that die, and the fills you get are adversely selected. **Clock:** resets (`limit_taker_cross_*` are `StrategyConfig` fields).

### IMP-8 — Fit dispersion per station from the existing archive
See FC-3. **Effect:** removes KSEA-type over-sizing and turns KDEN-type dead cities into quoting cities. **≈ +$0.5–1.5/day** plus variance reduction. **Validation:** walk-forward per station on `weather.db` — fit on ≤T, score `mean_abs_z` and CRPS on T+1 over the 2026 season. Entirely offline. **Clock:** resets (changes served probabilities); validate offline first.

### IMP-9 — Take breadth from spread width, not city count
Seven live daily **LOW** series carry 3–8× the spread of the traded HIGH series at comparable depth:

| series | median 2-sided spread | median depth | 24h volume |
|---|---|---|---|
| Traded HIGH (14 series) | 0.010–0.040 | 1–28 | 29k–891k |
| KXLOWTSFO | **0.080** | 5 | 4,303 |
| KXLOWTPHIL / KXLOWTDC | 0.060 | 4–6 | 6.7k / 7.7k |
| KXLOWTHOU | 0.055 | 6 | 16,210 |
| KXLOWTDAL / KXLOWTATL | 0.050 | 6 | 5.1k |
| KXLOWTSATX | 0.045 | 6 | 4,204 |

A bid+1 quote into a 6¢ spread captures ~4× per fill what it captures in the 1.5¢ HIGH book, and should fill more readily because a crosser is paying 6¢ to trade.
**Code (cheaper than it looks — the pipeline is variable-agnostic):** `forecaster/nwp_archive.py:reconstruct_daily_max` (`:236`) — one comparison operator, parameterize as `reconstruct_daily_extreme(payload, lead_days, agg=max|min)`. `forecaster/emos_forecast.py:233` already requests `"daily": "temperature_2m_max"`; add `temperature_2m_min` to the same call, same models, same fit. `forecaster/clisfo.py:126` anchors `MAXIMUM\s+(-?\d{1,3})` inside the TEMPERATURE section — the same window contains `MINIMUM`. `cities.py` — add LOW series tickers; five of seven (SFO, HOU, DAL, ATL, PHL) already have verified station/CLI identities, DC and SATX need new rows. A `metric` column on `nwp_model_forecasts`/`forecast_emos_daily_high`. **Trading side needs no change: bins are bins.**
**Effect:** ~+50% quoting opportunities at ~4× spread capture. **≈ +$2–5/day, the only proposal with real upside rather than loss avoidance.**
**Risk:** (a) overnight minima have a different error structure — radiative-cooling nights are bimodal and Gaussian EMOS may be badly dispersed; the climate-day window matters *more* for minima since they sit near the midnight boundary (`standard_utc_offset_hours` already handles it). (b) Volume is 5–20× lower, so a resting quote may never trade. **Validation:** shadow only for 30 days — score EMOS minima against `cli_settlements` MINIMUM and record `decision_snapshots` without placement. Ship only if minimum-temperature `mean_abs_z` lands near 0.798 like the highs do **and** the shadow maker quote's modeled fill rate exceeds the HIGH book's 4–13%. **Clock:** no reset in shadow.

### IMP-10 — Debias the spread statistic and re-key the trust model
FC-1 (debias) plus two defects in `posterior_kelly.py:_accumulate`, the one mechanism designed to protect size:
- **Wrong axis.** Cohorts are `temperature_cohort(high)` — cold/normal/warm/hot. The failure is on **probability band and side**: a single hot day carries both a p=0.97 far-tail NO and a p=0.63 near-forecast NO, and the temperature cohort cannot separate them. All the loss is in one band, all the profit in another, and the trust multiplier averages them into `posterior_mean_kelly_floor = 0.4` — no signal.
- **Pseudo-replication.** `acc[0] += 1.0` per **order row**, not per independent outcome. Measured: `KXHIGHAUS-26AUG16` contributed **28 rows at an identical price and probability**. Distribution of orders per `(day, market, side)`: 28/10/8/8/6/6/5/4×8/3×8/2×38/1×67. With `prior_strength = 20`, one lucky city-day outweighs the entire prior.
**Code:** key on `(side, round(cost*20)/20)` instead of `temperature_cohort(high)`; accumulate one row per `(target_date, market_ticker, side)` group (the caller already has `group_id`/`parent_order_id` to collapse partial-fill children). `risk.py` passes `cohort = temperature_cohort(forecast_high_f)` into `size_multiplier`; that call site changes to the price band.
**Effect:** the mechanism starts working — the p ≤ 0.70 band's trust falls to the floor within days while p ≥ 0.90 climbs toward 1.0. Self-correcting and survives regime change. **Risk:** 12 price bands × ~180 settled rows is thin; `resolve_record` already falls back to the pooled record below `min_cohort_n = 8`. Combine with IMP-1 to score untraded outcomes into the same cohorts. **Clock:** no `StrategyConfig` change, but it changes served sizes — treat as behavioral.

---

## 7. Do NOT do these

Each was proposed by an audit agent and then refuted by verification or adjudication.

1. **Do not extend the settlement-first hold guard to live.** See TC-5. Coverage is backwards (0/47 research TP exits vs 73/74 live TP exits are in its range), live's model is calibrated where it trades, and it would delete the exits that truncated live's left tail.
2. **Do not "fix" live's take-profit to hold longer.** Live TP: 74 exits, realized +$34.19 vs +$15.99 held. It is the reason a −$29.73 Houston position became +$7.05.
3. **Do not widen the favorite band.** Marginal cost is measured at exactly 0 trades/day and $0/day — it only removes candidates `min_edge`/`edge_lcb` already remove. It cannot add volume unless you also relax the LCB floor, and the cohort it guards measures **−6.7¢/contract over 626 contracts**.
4. **Do not loosen `edge_lcb >= 0` to buy volume.** The 21,590 blocked positive-point-edge research rows sit in the p 0.90–0.95 band where realized edge is +1¢ against a −7pt calibration gap; at −0.01 the expected realized edge is ≈0 after fees.
5. **Do not remove the daily-loss pause.** It fired once in 43 live settlement days (2026-07-10, −$12.42; next worst −$2.20). Removing it buys 0 trades/day and deletes a drawdown control. (Note the limit is not a flat −$10: `_sizing_bankroll` feeds clamped realized equity, so production stamped it at −$9.91 and it floats over [$5, $20]; at a drawdown the breaker gets *tighter*.)
6. **Do not restrict admission of the research 0.5–0.7 probability band.** The "44% true vs 61% model" figure is **lot-weighted**, counting one Miami weather day up to six times. Deduplicated to independent city-days over 2026-07-25..09-03: 21 events, model p 0.616, true win rate **0.571** — a 4.5pt gap against SE ≈ 10.6pt = **0.42 SE, i.e. nothing**. The band's positive hold-counterfactual (+$65.33) is also entirely one order (2214, +$89.24 on 151 contracts). Both its apparent edge and its apparent brokenness are one position and one weather day. It is also the worst possible clock cost: it thins the NO side, where the entire book lives.
7. **Do not trade lead 2+ to exploit the market's under-confidence at longer horizons.** Kalshi lists only T and T+1 (confirmed live for KXHIGHNY and KXHIGHCHI; **0 lead-2 rows in 185,886 decisions** over 3 days). `PAPER_ROLLING_TARGETS=3` is already asking and the exchange has nothing to give.
8. **Do not chase "maker fills suffer adverse selection".** Not supported at matched probability: p 0.85 band taker +7.1¢ vs maker +6.6¢; p 0.95 taker +4.2¢ vs maker +4.4¢. The account-level gap is entirely composition.
9. **Do not treat `comfort_edge` as blocking the profitable near-forecast band.** It would at the default `block_sigma_mult = 1.25`, but live overrides it to **0.4** (≈1.2 °F), so it is not binding.
10. **Do not act on "live has no working maker path".** False: since 2026-08-01 the live book placed 96 entry orders, **41 (43%) via the maker path** at bid+1, 9 of which filled for +$4.53. The escape hatch is the `limit_taker_cross_min_notional` floor, not `ask_size < 1`. The measured cost of live's taker crossing is ~$0.50 total over 2026-08-25..09-03 (~$0.05/day), and 9 of 17 crosses were on one-tick spreads where crossing costs nothing extra.
11. **Do not pursue the `p≈0.80 / price≈0.57` "+17.8¢/contract, 30/30 wins" cohort.** It is one market on one day (`KXHIGHAUS-26AUG16`) split across 28 partial-fill rows. This is the same pseudo-replication defect as IMP-10, and it is a warning about every "n = 56" claim in this document: **the effective sample size is day-markets, not order rows.**
12. **Do not conclude our settlement truth diverges from Kalshi's.** The rules text now names The Weather Company rather than the NWS CLI, which looks alarming, but over **700 settled city-days CLI agreed with Kalshi's settlement 699 times**. Worth one outlier guard (IMP-1), not a project.

---

## 8. Measurement problems — resolve before arguing from any of these numbers

### The exit counterfactuals disagree by a factor that changes the sign
Four independent analyses computed research-account closed-position P&L. They agree on realized and disagree wildly on the counterfactual:

| Analysis | Realized | If held to settlement | Conclusion |
|---|---|---|---|
| Production forensics | −$101 | +$24 | exits leak modestly |
| Take-profit adjudicator | −$98 | +$279 | exits leak badly |
| Stop-loss adjudicator | +$67 | +$280 | exits cost $213 |
| Strategy critic | −$101 | −$343 | exits **saved** $256 |

**Causes, all fixable:**
1. `market_day_settlements` holds **19 rows** and is not truth — each analysis reconstructed it differently. Use `weather.db:cli_settlements`.
2. `trading/sfo_kalshi_quant/exit_audit.py:33` — for closes lacking an explicit reason: `return "take_profit" if pnl > 0 else "stop_loss"`. **Any losing early close is labeled a stop**, which makes "stops lose money" partly tautological in any report built on that classifier. It also maps `HOLD_MODEL_VETO` text → `"stop_loss"` at `:60`.
3. TC-2 (`clv.py` keyed by date alone).
4. Partial-fill child lots counted as independent observations.

**Do not change any exit rule on these numbers until IMP-1 and TC-2 land.**

### "Stop loss" is three rules with opposite signs
Attributing child lots to parents and classifying by the monitor's own reason text, both accounts, since 2026-08-01:

| Rule | n | realized | held | exit cost | would win |
|---|---|---|---|---|---|
| Ordinary %-of-cost floor | 5 | −7.11 | −17.33 | **−10.22 (saved)** | 0/5 |
| Edge-reversal branch (TC-4) | 33 | −12.99 | +42.58 | **+55.57 (cost)** | **33/33** |
| Catastrophic floor (60%) | 30 | −210.10 | −101.46 | **+108.63 (cost)** | 9/30 |

The August study measured rule 1 (protective — correct). The September forensics summed all three and attributed the total to "stops".

**The actual damage mechanism**, proved from `paper_monitor_snapshots`: (1) price crosses the ordinary NO stop floor at 35% of cost; (2) `HOLD_MODEL_VETO` suppresses the stop for tens to hundreds of monitor passes spanning ROI −0.35 → −0.60 (order 2204: **280** veto holds; 2415: 139; 2501: 97; 2356: 57); (3) the catastrophic floor (`PAPER_MODEL_VETO_MAX_LOSS_PCT=60`) fires at **−0.59 to −0.93**, often far below the last vetoed mark because the close walks a collapsing book over repeated partial fills (order 2356: 0.08 → 0.07 → 0.05 → **0.03, −93%**). **The ordinary 35% stop never acted on a single one of these.**

Concentration: research stops at independent city-day level — entry cost ≥ $0.55, 23 events, stops **saved $1.00**; entry cost < $0.55, 10 events, stops **cost $160.95**, of which **Miami 2026-08-11 alone is $155.87 (97%)**. Excluding Miami 8/10, 8/11, 8/14, the catastrophic floor **saved $69.39** across 23 exits with only 2/23 would-have-won.

**The separating variable is stop distance in cents, not probability**: 35% of a $0.41 contract is 14¢; 35% of a $0.90 contract is 32¢. Intraday NO marks on a hot day routinely traverse 14¢ of noise before the high is set. **If you gate anything, gate on stop distance.**

**Recommended (in priority order):** (1) fix TC-4 — unconditional; (2) bound the veto in the same units as the stop, or make the catastrophic exit liquidity-aware instead of repeatedly crossing a collapsing book; (3) do **not** widen or remove stops, and do **not** restrict the 0.5–0.7 band. Sample-size caveat stated plainly: the "cheap positions" conclusion rests on 10 events, one of which is 97% of the effect.

### Live vs research calibration is opposite, and the earlier "over-confident at 0.90–0.95" claim is inverted for live
Live NO positions, all resolved orders since 2026-08-01:

| p band | n | model p | realized | cost | settle edge | realized $/ctr |
|---|---|---|---|---|---|---|
| 0.80–0.90 | 16 | 0.884 | 0.875 | 0.760 | +11.5¢ | +3.4¢ |
| 0.90–0.95 | 24 | 0.930 | **0.958** | 0.844 | +11.4¢ | **+10.5¢** |
| 0.95–0.98 | 37 | 0.969 | **0.865** | 0.918 | **−5.3¢** | +3.9¢ |
| ≥ 0.98 | 12 | 0.985 | 0.917 | 0.943 | −2.6¢ | +3.7¢ |
| 0.70–0.80 (research) | 58 | 0.700 | 0.621 | 0.480 | +14.0¢ | **−6.7¢** |

The over-confidence is at **0.95–0.98 (−10.4 points), not 0.90–0.95** — which is live's *best* band and mildly under-confident. **11 of the 16 recent live orders sit in 0.95–0.98**, which has negative settlement-held EV and only shows +3.9¢ because the exit machinery closes early. Scaling that band 10× scales an exit-timing artifact, not an edge. This is why IMP-7 is scoped to `probability < 0.95`.

Contract-weighted, take-profit populations: **live model p 0.9483 vs settle rate 0.9056 (−4.3pt, over-confident); research 0.7563 vs 0.9813 (+22.5pt, under-confident).** Hold beats take-profit iff `P_true > p` — hence research-only margins.

### Other data-quality findings
- **Reservation not released after fill (research).** Ledger `RESERVE −38,212.14` vs `RESERVATION_RELEASE +38,035.79`; order 2675 (MIA B93.5, 102 ctr @0.88 = $89.76) was `PAPER_FILLED` with `ENTRY_FILL` posted yet still reserved. Not a P&L error, but intraday free cash reads low and could throttle the allocator's aggregate-risk math.
- **`dataset_kalshi_markets` has zero rows after July** (270 rows, all 2026-07). Any job depending on Kalshi `result`/`expiration_value` for Aug/Sep is silently empty.
- `ux_paper_orders_open_market_side_profile` does not cover `PAPER_EXPIRED`, so nothing enforces one-per-market on the 411 expired rows. Harmless, but naive "open position" queries pick them up. **This index also blocks IMP-7's split-order variant** — it would need a `parent_order_id IS NULL` predicate, and getting that partial-index predicate wrong allows genuine duplicate positions.
- The exit-depth "VERIFIED" stamp is vacuous: `monitor.py:359-361` fetches quotes once before the loop, `:628` stamps `observed_at` at close time, and `db.close_paper_order` (`db.py:5990-6010`) checks that stamp against `closed_at`. The freshness check measures nothing.

**Verified clean — do not re-spend effort:** ledger integrity exact across all 9 accounts (`Σledger + open_cost + reservations == opening_cash + Σrealized_pnl` to 4dp; live `1037.3786 + 15.0217 + 0 = 1052.4003 = 1000 + 52.4003`; `RESERVE`/`RESERVATION_RELEASE` net to exactly $0.00 per account). Partial-close child lots do not double-count (`_insert_partial_close_lot`, `db.py:6170-6225`). No duplicate parent orders once child exit legs are excluded. All 415 rows in `paper_settlement_verifications` are `MATCH`. `settled_position_pnl`/`closed_position_pnl` correct and fee-inclusive. `position_won` agrees with CLI truth on all 419 settled rows. No prices outside [0,1], no overfills, no future timestamps, no rows both closed and settled, cash never negative, realized-P&L identity holds on 100% of closed rows. `initial_queue_ahead` correct. `latest_model_probability_read` is age-bounded and returns pure model probability (261,096 rows checked, never NULL).

---

## 9. Suggested execution order

1. **OPS-3** (alert URL) — one line, and the reason this incident ran unnoticed.
2. **OPS-1 / IMP-5** (shallow clone + timer cadence) — resolves the throttling. Must go to the box by manual file sync while OPS-2 is unresolved.
3. **OPS-2 / IMP-6** (retention + compaction) — owner decision required; unblocks every subsequent deploy.
4. **TC-2** (`clv.py` settlement keying) and **IMP-1** (ladder ledger) — the measurement layer. Everything after this is arguable from evidence rather than a selected sample.
5. **TC-4** (edge-reversal branch) — unconditional, 33/33 wrong-sided.
6. **SITE-1, SITE-2, SITE-3** — small, contained, and they stop the public page misreporting a healthy system. Ship together once deploys work.
7. **TC-1, TC-3, TC-6, TC-7** — latent risk-control correctness. No behavior change on the current book, so no clock cost.
8. **FC-1** (debias spread) + re-derive `max_source_spread_f` — unblocks SFO and LAX.
9. **IMP-2 / IMP-3** on research only, validated against the IMP-1 ledger before shipping.
10. **IMP-7** (depth-aware cross, scoped to p < 0.95), **FC-2**/**IMP-8** (dispersion), **IMP-9** (LOW series, shadow first).

### Validation tooling that already exists
`replay.py` (replays TTL, queue-ahead and crossing against the recorded public tape), `backtest.py` (per-cohort Brier), `backtest_rescore.py` (rebuilds `BucketProbability`, re-runs `TradeEvaluator` — but see Constraint 6: it is exit-blind), `research_replay.py` (`_TTL_MINUTES = 15`), `exit_audit.py` (but see §8 defect 2), `forecast_postproc_backtest.py`, `recalibration_replay.py`, `research_operate.EvaluationRun`.

### Test suite
Full local run is 2,796 passed / 8 skipped. Run with `ulimit -n 8192` — the default 256 produces ~30 phantom failures from leaked fds. Verify under both `TZ=UTC` and `TZ=America/Los_Angeles`; a timezone-dependent test has previously been red in CI for ~7h/day while local runs passed. Always check `gh pr checks`, not just local counts.
