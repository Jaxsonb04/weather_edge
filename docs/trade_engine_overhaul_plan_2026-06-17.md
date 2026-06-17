# WeatherEdge Trade-Engine Overhaul — Final Implementation Plan

> Produced by a 13-agent research+investigation workflow (subsystem map ×5, cited research ×3, bug hunt ×2, adversarial verification ×2, synthesis). All findings code-verified. Companion to `docs/trading_retune_validation_2026-06-17.md`.

## 1. Objective and honest framing

**Goal:** make the paper book produce *meaningful, realistic* data so the BALANCED (real-trading) profile can eventually be validated — bigger, case-by-case sizing; reachable exits; a dynamic compounding bankroll; and a dashboard that tells the truth.

**The honest objective is risk-adjusted geometric (log-wealth) growth after fees — NOT raw win-rate.** Three stated goals ("win more", "higher trade amount", "more winning trades") are *proxies* that can destroy EV:

- Win-rate is trivially maximized to ~99% by only betting deep favorites at tiny size — and that book can still be EV-negative after fees. (Kelly = max E[log wealth], not max win-rate.)
- Early take-profit on a *still-+EV favorite* is EV-NEGATIVE: hold-to-settlement EV = `p_model` and pays ZERO exit fee, while an early sale nets `bid − fee(bid)` and pays the exit fee a second time. Selling raises win-rate but lowers expectancy.
- Every change below is tuned to **after-fee EV**; win-rate stays on the dashboard only as a *calibration diagnostic*, never as the maximand.

**Paper vs real money (hard boundary):** All trading is PAPER (`live_orders_enabled=False`). Bigger size here is for *data realism only*. Real-money sizing stays gated behind walk-forward, after-fee, per-cohort validation with adequate independent-day samples. The conservative `StrategyConfig()` baseline stays frozen-notional as the reproducible control. The dashboard carries a "sizing basis: paper-realism, not validated" flag.

**Sample-size reality:** one SFO daily-high settlement resolves ALL brackets for that day jointly, so 5 bracket trades ≈ 1 independent observation. Effective sample = **independent settlement days**. Today: balanced ~2 days (n=4, empty), fast-feedback ~6 days (n=19, break-even). This overhaul builds the *machinery to collect and judge* data correctly.

## 2. Root causes (code-verified)

**(a) Trades are tiny — the binding constraint FLIPPED.** Old `max_position_risk_pct=0.005` ($5 cap) was the throttle; the retune lifted it to $20, so the cap is now dead weight. The *current* throttle is the **Kelly budget on a thin edge**: `kelly_budget ≈ 769·edge_lcb` dollars at cost ~0.87, so the $20 cap only engages once `edge_lcb > 0.026` while live edges cluster 0.01–0.03. Two multipliers shrink Kelly: `kelly_lcb_weight=1.0` sizes off the pure LCB (~4× smaller), and `fractional_kelly=0.10`. Below Kelly, `ask_size` (thin book) and the `int()`-floor cut further.

**(b) TP never triggers.** TP/SL are fixed %-of-cost. A binary caps `net_exit` at ~0.988, so for any NO favorite with cost > ~0.74 the target `cost·1.35 > $1.00` is **physically unreachable** (cost 0.86 → needs 1.161). NO favorites ride to settlement and forgo intraday convergence (0.40→0.65 never banked). The dashboard mirror already knows this (`take_profit_bid: null`); the live monitor has no guard. The YES 50% TP fires on a single 1-cent tick, scalping the convex upside.

**(c) Bankroll feels pointless.** Displayed bankroll is hardcoded `$1000` across all profiles; no live-equity field exists on the dashboard even though `db.paper_equity()` computes it. Sizing equity is dynamic on balanced only, but the effect is ~nil because the size throttle keeps PnL ~$0.

**(d) YES loses.** No YES-specific sizing path; all 3 live YES trades lost. Causes: cheap-tail gate ceiling 0.05 is *below* where losers trade (0.08–0.12); fee drag ~25% round-trip > EV margin; model anti-calibrated on warm/hot days (brier ~0.96) so the YES probability is unreliable and a tight-but-wrong model defeats uncertainty shrink.

## 3. Workstreams (P0 → P2)

### P0-0 — Extract shared `exits.py` module (DRY precondition)
Move side-threshold constants + exit math (duplicated verbatim across `cli.py` and `strategy_research.py`) into one module imported by both. **Risk:** Low. **TDD:** characterization test that dashboard `take_profit_bid`/`stop_loss_bid` are unchanged after extraction.

### P0-A — Reachable case-by-case EXITS (edge/probability, not %-of-cost)
Replace `pnl_pct >= take_profit` / `<= -stop_loss` with two boundaries:
- **Take-profit / convergence — POINT estimate:** EXIT when `bid − fee_exit(bid) >= p_model` (HOLD while `p_model − bid > −fee_exit(bid)`). Slightly-negative threshold, reachable, holds a still-0.97 favorite to settlement, books the 0.40→0.66 convergence at fair value. Using `p_lcb` here would double-penalize and cut winners early.
- **Stop-loss / deterioration — `p_lcb`:** EXIT when `p_lcb <= bid` (margin ≤ 1× fee). A *probability* stop on genuine model deterioration, not a %-PnL stop.
- `fee_exit` reuses `fees.quadratic_fee_per_contract`. Reasons: `edge_captured` / `edge_reversed`. De-trust the deterioration stop on miscalibrated (warm/hot) cohorts.

**Files:** `cli.py:1962-2032`, `exits.py`, `strategy_research.py:1838-1877`. **Risk:** Medium (every open position); convergence boundary is provably ≥ hold-to-settlement EV so it can't be worse than status quo; ship behind a flag. **TDD:** NO@0.86/model-0.97 holds until bid≥0.98; NO@0.40/model-0.66 exits ~0.65; deteriorated p_lcb triggers `edge_reversed`; no target exceeds `net_exit(0.99)`; monitor and dashboard agree.

### P0-B — Real SIZING throttle fix + safe bigger paper size
1. Lower `kelly_lcb_weight` 1.0 → 0.5–0.7 (stop double-counting uncertainty). Biggest, safest lever.
2. `round()` instead of `int()` on the **paper path** (`risk.py:165-166`, `paper.py:332`), gated on an `is_paper` flag (Kalshi needs integers live).
3. Use `contracts_for_budget(ask, spend_budget)` (already in `fees.py`) instead of `spend_budget/cost`.
4. Emit a per-trade `binding_constraint` tag.
5. **Hold `fractional_kelly` at 0.10–0.15** for now; step toward quarter-Kelly only after ≥30 independent-day samples + a passing walk-forward. `fk≤1` structurally cannot overbet full Kelly.

Keep the `edge_lcb >= 0` APPROVAL floor untouched. **Files:** `risk.py:149-168`, `paper.py:332`, `config.py:140-141`. **Risk:** Medium; no scaling on warm/hot cohort, paper-only, per-event/day caps stay.

### P0-C — DYNAMIC bankroll / compounding (display + sizing)
- **Display:** add per-profile `current_equity = store.paper_equity(...)`; **relabel `bankroll` → `starting_bankroll`** so old readers don't break.
- **Sizing:** keep `size_against_live_equity=True` on balanced; enable per research-profile with a **clamp band** `min(max(equity, 0.5·start), 2.0·start)`. Keep conservative frozen.
- **CLI:** thread `--risk-profile` into `cmd_backtest_market` ending_bankroll.
- Caveat: `paper_equity` is realized-only — label accordingly. **Risk:** Medium; clamp band + frozen baseline bound it.

### P1-D — Optimal smaller case-by-case YES sizing
Insert a YES-specific path after the cheap-tail block: raise `cheap_tail_max_ask` to ~0.15; hard-gate `edge_lcb>0` for YES; **Baker-McHale shrink** `k_shrink = edge_e²/(edge_e² + (sigma/(1−c))²)`, `sigma=(p−p_lcb)/1.96`; `payout_scale=c`; hard cap ~0.3–0.5% equity; require point prob ≥ 2× cost for sub-0.15 YES; wide/no TP on longshots. **HARD precondition:** block warm/hot (70–79°F) YES until cohort brier < 0.25 (shrink alone fails on confidently-wrong cohorts). **Research:** Baker & McHale (2013); Kalshi favorite-longshot bias.

### P1-E — Dashboard add/remove (honest diagnostics)
**REMOVE/FIX:** the vacuous Approved $0/0% block (dedup-zero lineage; contradicts the real +$1.05/60.9% book) — fix the lineage FIRST; relabel bankroll static + add live equity; demote `raw_signals=23664`; collapse all-zero position scaffolding.
**ADD:** YES-vs-NO performance split (present, never rendered); exit-reason breakdown; binding-constraint diagnostic; warm/hot calibration warning pill+banner; capital-deployment %; independent-day count beside every hit-rate/ROI; effective Kelly + EV-after-fees; paper-realism flag; win-rate-vs-EV caveat. **Files:** `forecaster/templates/strategy-lab.html` + payload builders in `strategy_research.py`. Browser-check dark mode + Chart.js (mutate `config.options`) before push.

### P1-F — Explore(fast-feedback) → exploit(balanced) loop + warm/hot regime gating
1. **Regime gate** (highest-EV change available now): block/de-size balanced entries whose forecast high lands 70–79°F until that cohort's brier < 0.25. Cohorts already computed in `backtest.py:138-161`.
2. Tag every fast-feedback trade with balanced gate-pass booleans so the explorer's journal can be filtered to "trades balanced would also take."
3. Per-cohort recalibration (isotonic/Platt) once cohort ≥30 outcomes & global >180; validate via walk-forward dual brier/log-loss. Promotion stays MANUAL.
4. Report sample size in **independent days**; shrink event cap by effective-N.

### P2-G — After-fee per-cohort backtests + `backtest-rescore` (RUN ON AWS)
Add a walk-forward, after-fee, per-side, per-cohort, **independent-day** backtest and a `backtest-rescore` command that replays the labeled journal under candidate config/exit/sizing values, reporting realized after-fee **log-growth per independent day** (NOT win-rate). Develop/unit-test locally with fixtures; **run on AWS** against real settled history; read results from published JSON. Prerequisite: the EXPIRED-pollution denominator bugs must be fixed first.

## 4. Bug list (severity-ranked)

**CRITICAL**
1. NO-side take-profit mathematically unreachable for any favorite (cost > ~0.74) → favorites silently ride to settlement. *Fix in P0-A.*
2. `PAPER_EXPIRED` rows (pnl=0.0) pollute win-rate/ROI/`paper_equity`/circuit-breaker denominators. *Fix: allowlist `status IN ('PAPER_SETTLED','PAPER_CLOSED')` in `db.py:1074,1118-1130,1174-1186` and `summary.py`.*

**HIGH** (selected): TP/SL keyed to cost not achievable range (asymmetric: stops live, favorite TPs dead); sizing on pure LCB halves Kelly; "risk sizing produced zero contracts" masks a gate/sizing split (Kelly-zero); cheap-tail ceiling 0.05 below the 0.08–0.12 losers; static displayed bankroll; NULL `market_close_time` defeats the pre-resolution guard; `settle_paper_orders` TOCTOU read-before-lock; `market_backtest_summary` hit_rate `if hits` short-circuit masks 0-for-N; circuit-breaker ROI diluted by EXPIRED rows; YES posterior LCB collapses to 0 on low-prob bins.

**MEDIUM/LOW** (selected): NO model-veto suppresses reachable stops; int-floor truncates 13–41% of stake; sizing divides by single-contract cost not `contracts_for_budget`; monitor swallows all fetch exceptions into one `FETCH_FAILED` (expired API key silently HOLDs); `close_paper_order` doesn't store `resolved_yes` (break-even misclassified as loss); `_normalize_weather_probabilities` returns un-normalized zero list silently.

## 5. Sequencing + AWS validation

**Order:** (1) prerequisite bug fixes (EXPIRED-pollution; misleading zero-contracts reason) → (2) P0-0 shared module → (3) P0-A exits + P0-B sizing + P0-C bankroll → (4) P1-D YES + P1-E dashboard + P1-F regime gate/data loop → (5) P2-G backtests + AWS rescore.

**Test plan:** every workstream ships unit tests FIRST (RED→GREEN) per the worked numeric examples; shared-module invariant tests keep `cli.py` and `strategy_research.py` in lockstep; fixed settled-history fixtures keep dynamic-sizing tests deterministic; conservative `StrategyConfig()` stays frozen.

**AWS validation (local stays clean):** develop + unit-test locally against fixtures; render + Playwright-check the dashboard before push; deploy and run the walk-forward / `backtest-rescore` on AWS against real settled history; read results from published `strategy_research.json`.

**Real-money gate (unchanged):** no real-money step-up until the AWS backtest shows positive after-fee log-growth per independent day, per side and per cohort, over ≥30 independent settlement days, with warm/hot brier < 0.25.

## 6. Decisions adopted (research-backed defaults)

- **fractional_kelly:** hold 0.10–0.15 now; step-up needs explicit sign-off after a passing AWS walk-forward.
- **Dynamic-bankroll clamp band:** 0.5×–2.0× of starting notional for research profiles.
- **Warm/hot regime gate:** BALANCED hard-blocks/de-sizes 70–79°F entries (safety); FAST-FEEDBACK keeps exploring them at tiny size (so the cohort that most needs recalibration still collects data). This matches the explore→exploit design.
- **Money mode:** paper-only; real money out of scope for this overhaul, gated on the AWS walk-forward above.
- **AWS execution:** run the new backtests on AWS via provided `LIGHTSAIL_IP`+key; local stays clean.
