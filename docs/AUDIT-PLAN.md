# WeatherEdge — Full Codebase Audit & Remediation Plan

**Date:** 2026-07-10 · **HEAD at audit time:** `bb862f9e` · **Repo:** `/Users/jaxson/develop/WeatherEdge` (remote build Mac; production runtime on EC2 per `CLAUDE.md`)

**Who this is for:** an implementer with no prior context on this codebase. Every finding below is self-contained: location, evidence, an implementation-ready fix, a verification command, and blast radius. Read §1–§4 first; then execute batches in the §3 order.

**Method.** Six specialist auditors read the repo end-to-end (trading correctness, trading performance/structure, forecaster, SPA, deploy/infra, repo-wide dead-code + docs), grounded on a repo map, the systemd unit graph, the CLI dispatch registry, the import graph, and a 239 MB pull of the production SQLite DB. Every candidate finding was then re-derived by a separate fresh-context adversarial verifier instructed to disprove it. Result: **77 findings — 75 confirmed, 2 weakened and retained at reduced impact, 0 refuted.** Industry norms were researched separately with citations (§2). Test suites were run, not just read: trading targeted suites green (82+63+30 tests across runs), forecaster 72/72, SPA vitest 37/37, `tsc`/`oxlint` clean, `bash -n` clean on all 19 shell scripts.

**Evidence caveats the implementer must know:**
- The Mac copy of `trading/data/paper_trading.db` is a **stale pre-migration pull (data ends 2026-06-26/27)** despite its July file date. All schema/code claims are current; row-level claims are "state at pull time". Findings needing a live-box re-check say so explicitly (TP-2 is the main one).
- The scratch DB copies used during the audit (`/tmp/audit_paper.db`, `/tmp/audit_pristine.db` on the Mac) were polluted with a test index (`idx_ms`). **Re-copy from `trading/data/paper_trading.db` before re-running any measurement.**
- Two live-network probes were made during verification (NWS CLI product versions for SFO; IEM CLI JSON for KNYC) and are cited where used (FC-1).
- Nothing in the repo was modified. This document is the only file the audit adds.

---

## 1. Executive summary

**77 findings.** By corrected impact: **four at 8** (a cross-city timezone bug in the intraday trading model; an ungated data-deletion path that bypasses the brand-new archive gate; a hard-crash path in the public SPA; ~9× write amplification on the decision journal), **five at 7**, **eleven at 6**, and a long tail of verified medium/low items. Counts by category: 21 bugs/correctness, 13 performance, 12 structure, 9 dead/duplicate code or artifacts, 15 docs-staleness, 7 reliability/ops.

### 1.1 Headline themes

1. **Settlement-truth finality is the weakest correctness link.** The system stores NWS *preliminary* evening CLI reports as settled truth with no finality flag. That freezes the same-day EMOS serve every evening (~6.3 h for SFO — live-verified against the real NWS product feed), pollutes the trailing-bias recalibration window for ~24 h, and lets the trading engine permanently book PnL off a preliminary report that Kalshi may settle differently (FC-1, TB-2, TB-3, FC-5). These four findings interlock and should be fixed as one batch.
2. **The 15-city expansion left SFO-anchored assumptions in the same-day trading path.** The intraday probability model reads every city's clock as San Francisco time (TB-1, impact 8 — 12 of 15 cities systematically misprice same-day markets), and the monitor's model read structurally dies every afternoon for same-day positions by schedule design (TB-5).
3. **The EC2 migration (2026-07-10) is code-complete but operationally unfinished.** One genuinely dangerous leftover — a second, archive-gate-bypassing `paper-prune` invocation in the nightly backfill unit (DP-1) — plus two stale duplicate systemd templates tracked inside the Python package (DC-1/DP-2), a deploy README that still instructs "Use Amazon Lightsail, not full EC2" (DC-5/DP-3), a DB-pull script pointing at the decommissioned box (DP-5), and maintenance indexes defined in a deploy script that the production DB shows was never run (TP-2).
4. **The decision journal's write side re-introduced the bloat its July read-side fixes cured.** The new `diagnostics_json` column duplicates identical per-tick context across every bin×side row: ~800 B/row → ~6.2 KB/row, ≈64 MB/day of write volume on a 4 GB box (TP-1), while both publication cycles recompute a walk-forward calibration backtest ~480×/day for inputs that change once per day (TP-3).
5. **Structure debt is concentrated, not diffuse.** Four god modules (`strategy_research.py` 4,334 lines, `cli.py` 3,982, `db.py` 3,711, `google_weather_cache.py` 2,646 — all 2.6–4.3× the citable 1,000-line community norm), settlement bin-resolution logic in four copies that have **already diverged** on unknown-strike fallback (TP-11), and a forecaster whose live 30-minute serve transitively imports its own legacy research stack (FC-8, runtime-proven). Concrete, shim-safe decompositions are specified for each.
6. **The SPA is strong at the edges, brittle at the seams.** Lazy routes, defensive manifest layer, clean lint/tests — but zero error boundaries plus unguarded field access mean one malformed published JSON blanks the whole site (SP-3), and the landing route ships ~477 KB gzipped JS against the project's own 300 KB budget (SP-2).

### 1.2 Ranked findings table

Ordered by corrected impact (post-verification), then by category severity. Confidence: how the finding was verified — all findings passed independent adversarial re-derivation unless marked.

| # | id | category | subsystem | impact | confidence | one-line |
|---|----|----------|-----------|--------|------------|----------|
| 1 | TB-1 | bug | trading | 8 | high (verifier re-traced end-to-end) | Intraday model reads every city's clock as San Francisco time — 12/15 cities misprice same-day probabilities |
| 2 | DP-1 | bug/data-loss | deploy | 8 | high (unit+CLI+git timeline) | Second `paper-prune` path in the backfill unit bypasses the archive gate; deletes unexported data exactly when the gate is blocking |
| 3 | SP-3 | bug | spa | 8 | high (code + zero-boundary grep) | Missing-field access hard-crashes Overview/Methodology to a blank page; zero error boundaries in the app |
| 4 | TP-1 | perf | trading | 8 | high (measured on DB) | `diagnostics_json` write amplification: identical per-tick context duplicated across ~20 rows → ~64 MB/day |
| 5 | DC-4 | docs-stale | docs | 8 | high | `docs/aws_lightsail.md` documents the decommissioned host as production |
| 6 | DC-5 | docs-stale | docs | 8 | high | Deploy README says "Use Amazon Lightsail, not full EC2" + false "committed models/" claim |
| 7 | FC-1 | bug | forecaster | 7 | high (live-verified vs NWS/IEM) | Preliminary evening CLI reports stored as settled truth: evening lead-0 serve freeze + recalibration contamination |
| 8 | TP-8 | structure | trading | 7 | high (full read) | `db.py` is a 3,711-line god module mixing schema, account policy, lifecycle, analytics, diagnostics |
| 9 | DC-1 | duplicate | trading pkg | 7 | high (diffed) | Two stale Lightsail-era systemd templates tracked inside `sfo_kalshi_quant/` |
| 10 | SP-2 | perf | spa | 7 | high (measured build) | Landing route ≈477 KB gz JS vs 300 KB budget; CSS 78 KB vs 30 KB |
| 11 | DC-7 | docs-stale | docs | 7 | high | `trading/README.md` (514 lines) still describes a single-market SFO engine |
| 12 | TB-2 | bug | trading | 6 (was 7) | med-high | Auto-settle can permanently book PnL off a preliminary evening CLI report (winter-concentrated) |
| 13 | FC-2 | bug | forecaster | 6 | high | Nightly rebuild re-stamps `fetched_at`; scanner reads uncorrected rolling-origin mu ~15 min nightly |
| 14 | TP-2 | perf | trading | 6 | high on pull; conditional on box | Maintenance indexes + ANALYZE defined but never applied to the production DB |
| 15 | TP-3 | perf | trading | 6 | high (reproduced 0.43 s) | Walk-forward calibration backtest recomputed ~480×/day for once-a-day inputs |
| 16 | TP-9 | structure | trading | 6 | high | `cli.py` 3,982 lines: argparse + print formatting + core execution logic in command handlers |
| 17 | TP-10 | structure | trading | 6 | high | `strategy_research.py` 4,334 lines spanning eight separable artifact domains |
| 18 | TP-11 | structure/correctness | trading | 6 (was 5) | high (verifier: divergence proven) | Settlement bin-resolution in 4 copies that already diverge; 7 copies of `_table_exists` |
| 19 | DP-3 | docs-stale | deploy | 6 | high | Canonical deploy runbook provisions the wrong platform |
| 20 | DC-6 | docs-stale | docs | 6 | high | Root README: "Lightsail box" + wrong strategy-lab cadence (says 5 min, is 15) |
| 21 | DC-10 | docs-stale | docs | 6 | high | 2026-06-15 audit doc needs a historical marker (its snapshot/line-cites predate July overhauls) |
| 22 | TB-5 | bug | trading | 5 | high (mechanism) | Monitor's model read structurally dies every afternoon for same-day positions |
| 23 | FC-5 | bug/ops | forecaster | 5 | high (grep-verified) | `city_truth.refresh_live` wired into nothing — 14 cities' truth rides one nightly pull |
| 24 | FC-8 | structure | forecaster | 5 | high (runtime-proven) | Live 30-min serve transitively imports the 2,646-line SFO legacy module; cross-package import breaks stated boundary |
| 25 | DP-2 | hygiene | deploy | 5 | high | (Same files as DC-1 — deploy-side statement; merged fix) |
| 26 | DP-9 | reliability | deploy | 5 | high | No `OnFailure=` on any of 9 units; no disk check; archive-gate failure is silent |
| 27 | SP-4 | perf/robustness | spa | 5 | high | All icons fetched from api.iconify.design at runtime; offline/blocked → icon-less UI |
| 28 | DC-9 | docs-stale | docs | 5 | high | `lightsail_dataset_plan.md` is an obsolete plan presented as current |
| 29 | DC-12 | docs-stale | docs | 5 | high | `data_and_artifacts.md`: Lightsail refs; archive layer (the newest safety-critical data path) missing |
| 30 | TB-4 | bug | trading | 4 (was 5) | high | `except URLError` misses `KalshiUnavailable`: one brownout kills the whole 15-city scan tick |
| 31 | TB-6 | bug | trading | 4 (was 5) | med | Partial arbitrage box recordable via preflight/guard-index predicate gap (research profiles) |
| 32 | TB-7 | correctness | trading | 4 | med | Archive `id_floor` assumes id order == day order; midnight inversion silently drops rows prune later deletes |
| 33 | FC-3 | bug/eval | forecaster | 4 | high mechanism, magnitude unquantified | Rolling-origin archive fit on 1–2 more truth days than live serves get — ship-gate numbers slightly optimistic |
| 34 | FC-4 | perf | forecaster | 4 | high | `--serve-rolling` makes 3 identical Open-Meteo calls per city per tick (1,710/day vs ~570) |
| 35 | TP-4 | perf | trading | 4 | high (measured) | Strategy build re-reads the full decision journal into Python every 15 min |
| 36 | DP-5 | reliability | deploy | 4 | high | `pull_paper_db.sh` still hard-requires `LIGHTSAIL_*` — will pull from the decommissioned box |
| 37 | DP-6 | reliability | deploy | 4 | high | `sync_to_lightsail.sh`: stale target + exclude-list drift that can clobber live artifacts |
| 38 | DP-7 | reliability | deploy | 4 | high | `run_dataset_backfill.sh` exits 0 even when every source fails |
| 39 | SP-1 | dead-dep | spa | 4 | high (3-way verified) | `heroui-native` (React Native lib) shipped as a web dependency; stale `trustedDependencies` |
| 40 | SP-11 | test-gap | spa | 4 | high | `data.ts`/`strategy.ts` business rules (incl. the Today/Tomorrow anchor) largely untested |
| 41 | DC-2 | orphan | spa | 4 | high | `Learnings.tsx` orphaned by the Lab overhaul yet copy-polished on 7/10 — delete or re-mount decision |
| 42 | DC-8 | docs-stale | docs | 4 | high | forecaster README: 3× Lightsail; File Map omits the entire production path |
| 43 | DC-11 | docs-stale | docs | 4 | high | `trading/docs/user_guide.md` + 2 research notes: single-market framing, undated |
| 44 | DC-14 | dead-config | config | 4 | high | Root/trading `.env.example` missing all July control knobs |
| 45 | TB-3 | bug | trading | 3 (was 6) | high (weakened) | CLI parser can't read negative/single-digit maxima — affected winter days settle ~1 day late via IEM fallback |
| 46 | FC-7 | dead | forecaster | 3 | high (swept) | Dead/stale inventory: retired-dashboard leftovers, dead symbols, one unconsumed tracked artifact |
| 47 | FC-9 | structure | forecaster | 3 | high | `google_weather_cache.py`: 2,646 lines, 7 responsibilities (budget mechanics verified sound) |
| 48 | FC-10 | bug/config | forecaster | 3 | high | Source sync excludes two *committed inputs* — box copies frozen at seed time |
| 49 | TP-5 | perf | trading | 3 | med-high | Per-target re-loading inside the scan loop (EMOS archive, Kelly model, pause reason ×90) |
| 50 | TP-6 | perf | trading | 3 | high | Monitor N+1 Kalshi calls per 2-min cycle |
| 51 | TP-12 | structure | packaging | 3 | high | Two pyproject.toml files both install `sfo-kalshi` under different project names |
| 52 | DP-4 | dead-config | deploy | 3 | high | `SFO_ENABLE_LIGHTSAIL_FORECASTER_REFRESH` documented but read nowhere |
| 53 | DP-8 | reliability | deploy | 3 | high | Cross-unit locks live in /tmp; PrivateTmp or tmp cleaners would silently void serialization |
| 54 | DP-10 | config-gap | deploy | 3 | high | 15+ env vars read by code but undocumented — including the five real-money gates |
| 55 | SP-8 | perf | spa | 3 | high | 3 font families / 10 weights, render-blocking, third-party |
| 56 | SP-9 | perf | spa | 3 | high mechanism | Whole app re-renders every 60 s from the publication poll clock |
| 57 | SP-10 | reproducibility | spa | 3 | high | 26 `>=` version ranges; recharts already resolved to an unpromised major |
| 58 | DC-3 | artifact | forecaster | 2* | med-high | `model_compare_results.json` tracked with no code consumer (provenance for a manual step — document, don't delete) |
| 59 | DC-13 | artifact | repo | 3 | high | `docs/prompts/` untracked — commit or ignore (owner decision) |
| 60 | DC-16 | artifact | local | 3 | high | Untracked leftovers of retired pipelines on the dev Mac (3 HTML files, empty egg-info) |
| 61 | TB-9 | correctness | trading | 2 | high | Monitor decides exits with a differently-rounded fee than the close books (≤1¢/contract) |
| 62 | FC-6 | perf | forecaster | 2 | high | Lead-3 NWP fetched nightly for all cities; nothing consumes it |
| 63 | TP-7 | perf | trading | 2 | high | `market_snapshots` has no index at all (bounded table; trivial fix) |
| 64 | TP-13 | test-gap | trading | 2 | med-high | Positive finding: no material coverage gap on settlement/sizing/archive-gate logic |
| 65 | DP-11 | hygiene | repo | 2 | high | Tracked runtime-artifact fixtures internally inconsistent with .gitignore siblings |
| 66 | DP-12 | reliability | deploy | 2 | high facts | Mixed-timezone timers + `set-timezone || true` can silently shift the whole daytime schedule |
| 67 | DP-13 | bug/portability | deploy | 2 | high | `${1,,}` (bash-4-only) in a script that claims macOS support — repo's own tests ban this idiom elsewhere |
| 68 | DP-14 | hygiene | deploy | 2 | high | `deploy_web_app.sh` clobbers `$USER`, space-unsafe `$SSH`, cwd assumption |
| 69 | SP-5 | dead-code | spa | 2 | high | (Same file as DC-2; SPA-side statement — merged fix) |
| 70 | SP-7 | bug | spa | 2 | high | Dark-theme flash for light-mode users (hardcoded `class="dark"`) |
| 71 | SP-12 | structure | spa | 2 | high | Two `money` formatters with different sign semantics |
| 72 | SP-13 | content | spa | 2 | high | index.html meta description still SFO-era; no social meta |
| 73 | DC-15 | dead-config | config | 2 | high | Minor dead config: `.semgrepignore`/gitignore entries for retired files; `.venv-dev` not ignored |
| 74 | DC-17 | informational | repo | 2 | high | Repo/git size posture: lean; 78 MB pack across 16 packs (optional `git gc`) |
| 75 | DC-18 | informational | repo | 2 | high | Negative finding: zero TODO/FIXME debt, no commented-out code blocks |
| 76 | TB-8 | correctness | trading | 1 (was 3) | high (weakened) | SQL sampling partitions by raw `side`; affected legacy-row population is currently zero |
| 77 | SP-6 | dead-code | spa | 1 | high | Dead assets: `src/assets/*` (3 files) + orphaned `public/icons.svg` |

*DC-3 scored 2 for action urgency; its risk is in deleting it carelessly, not keeping it.

Merged pairs (same underlying defect found independently by two auditors — treat as one work item): **DC-1 = DP-2** (stray unit templates) and **DC-2 = SP-5** (`Learnings.tsx`). The table lists both ids for traceability; §5 details them once.
---

## 2. Architecture assessment

Norms below were researched from the public record during this audit (citations inline). Contested points that must NOT be treated as defects: src-vs-flat Python layout, exact module line counts (linter convention, not law), event-driven vs scheduled-batch design for daily-settlement markets, exact React folder taxonomy.

### 2.1 Forecaster vs the NWP post-processing norm

**The norm** (WMO-No. 1254 *Guidelines on Ensemble Prediction System Postprocessing*, <https://library.wmo.int/idurl/4/57510>; Gneiting et al. 2005 EMOS, <https://journals.ametsoc.org/view/journals/mwre/133/5/mwr2904.1.xml>; NOAA MDL MOS, <https://vlab.noaa.gov/web/mdl/mos>; CRAN `ensembleMOS`, <https://cran.r-project.org/web/packages/ensembleMOS/index.html>; Vannitsem et al. 2021, <https://arxiv.org/abs/2004.06582>): a staged pipeline — ingest → archive → fit → serve → verify — with a hard temporal train/serve boundary (every coefficient for day D derivable exclusively from data available before D), fitting state as a deterministic function of a dated archive, verification as a separate surface consuming settlement truth after the fact, and backtests that replay the exact live fitting procedure walk-forward.

**Where WeatherEdge conforms:** the EMOS core is genuinely walk-forward-clean — verified this audit: `emos_ngr_predictions` appends history strictly after predicting; recalibration windows end at serve_date−1; corrections apply only to `source='live'` rows while windows read `source='rolling_origin'`; fixed-standard-time settlement handling is consistent across the pipeline (all initially suspected DST traps were disproved). The archive-as-state-of-record pattern is present (`forecast_emos_daily_high` with per-date rows).

**Where it diverges, for real reasons to fix:**
- **Truth finality is not modeled** (FC-1): `cli_settlements` conflates "a CLI product exists" with "the day is settled" — the exact leakage-adjacent defect the WMO hygiene guidance exists for. Preliminary evening values sit in training/recalibration windows as truth for ~24 h.
- **The rolling-origin record is not a faithful replay of the live serve** (FC-3): fits see 1–2 more settled days than a live serve could have at the same moment, so ship-gate numbers (e.g. the replay's −1.3 % CRPS) are upper bounds. The norm is "backtest = replayed live procedure".
- **Stage separation is violated at import time** (FC-8): the live 30-minute serve transitively imports the SFO legacy blend/research stack (`emos_forecast` → `forecast_postproc_backtest` → `forecast_backtest` → `google_weather_cache`, runtime-proven), and one research arm imports the *trading* package, contradicting the repo's own stated no-cross-import rule. MDL/WMO norm: fit/serve/verify are separable code paths.
- **Layout:** `forecaster/` is ~25 loose top-level scripts, not a package. The Packaging User Guide treats package-with-submodules as the uncontested norm at this size (<https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/>; <https://docs.python-guide.org/writing/structure/>) — but the deploy constraint (systemd runs `python <file>.py` in `__FORECASTER_DIR__`) means entry files must stay put; the target layout in FC-8 respects that.

### 2.2 Trading engine vs the quant-system layering norm

**The norm** (QuantConnect Lean Algorithm Framework, <https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/overview>; NautilusTrader architecture, <https://nautilustrader.io/docs/latest/concepts/architecture/>; freqtrade bot loop, <https://www.freqtrade.io/en/stable/bot-basics/>; Hummingbot connectors, <https://hummingbot.org/connectors/connectors/architecture/>; ledger norms: Modern Treasury, <https://www.moderntreasury.com/journal/enforcing-immutability-in-your-double-entry-ledger>): distinct layers with typed hand-offs (signal → sizing/risk → execution → settlement/accounting), risk gates in a layer the strategy cannot bypass, one code path between research and live, all venue I/O behind an adapter, and idempotent append-only settlement. A scheduled/batch cadence is explicitly legitimate for slow daily-settlement markets — do not penalize the absence of an event bus.

**Where WeatherEdge conforms:** the module inventory *names* the right layers (`risk.py`, `execution.py`, `exits.py`, `fees.py`, `settlement*.py`, `kalshi.py` as the single venue adapter — verified: all Kalshi I/O goes through `KalshiPublicClient`); settlement UPDATEs are guarded and idempotent (`BEGIN IMMEDIATE` + status-conditional writes, verified as the fix to the June audit's race); risk caps (shared-account policy, position caps) sit outside strategy code in `account.py`/`db.py`; paper/live parity is structurally aided by the maker-fill proxy living against real market data.

**Where it diverges, for real reasons to fix:**
- **Execution-engine logic lives inside the CLI module** (TP-9): the queue-ahead maker fill model and the monitor exit orchestration — the code that decides real exits and fills — are functions of `cli.py`, reachable only through command handlers. Norm: the CLI is parsing/wiring; the engine is an importable, unit-testable module (Click complex-apps pattern, <https://click.palletsprojects.com/en/stable/complex/>).
- **The data layer embeds business policy** (TP-8): `account_policy_capacity` (sleeves, region caps, drawdown pause) and ~900 lines of diagnostics serialization live in `db.py` alongside locking-sensitive lifecycle SQL. Norm: repository seam.
- **Settlement semantics have four homes that already disagree** (TP-11): `models.MarketBin.resolves_yes` vs two near-twins in `db.py` vs `archive._resolves_yes`, with verifier-proven divergence on unknown-strike fallback. This is the "one code path" norm violated inside a single subsystem.
- **Packaging:** two `pyproject.toml` files install the same package + console script under two project names (TP-12) — against the single-authoritative-manifest norm (<https://packaging.python.org/en/latest/guides/writing-pyproject-toml/>).

### 2.3 SPA vs the React dashboard norm

**The norm** (react.dev `lazy`/error boundaries, <https://react.dev/reference/react/lazy>, <https://react.dev/reference/react/Component#catching-rendering-errors-with-an-error-boundary>; Bulletproof React structure, <https://github.com/alan2207/bulletproof-react/blob/master/docs/project-structure.md>; TanStack Query server-state rationale, <https://tanstack.com/query/latest/docs/framework/react/overview>; web.dev budgets ~170–250 KB compressed initial JS, <https://web.dev/articles/incorporate-performance-budgets-into-your-build-tools>): route-level code splitting, error boundaries at deliberate granularity, a typed fetch layer, quantified bundle budgets enforced in the build.

**Where WeatherEdge conforms:** all three views are `React.lazy`; the fetch layer is typed and centralized (`data.ts`/`strategy.ts`/`publication.tsx`) with a defensive, well-tested manifest/freshness system; no barrel files; a11y basics are genuinely present. At three views, the absence of TanStack Query and feature folders is fine — do not restructure for fashion.

**Where it diverges:** zero error boundaries (SP-3 — one bad published JSON blanks the app; react.dev explicitly prescribes per-region boundaries); landing JS ~477 KB gz vs the project's own 300 KB budget and web.dev's ~170–250 KB anchor (SP-2); a runtime third-party icon dependency (SP-4) on a codebase whose own rules say self-host critical deps.

### 2.4 Oversized-file / module-boundary map

Pylint's `too-many-lines` default (1,000 lines, <https://pylint.readthedocs.io/en/stable/user_guide/configuration/all-options.html>) is the citable community threshold; the defect is responsibility concentration, not the number itself.

| file | lines | × norm | responsibilities mixed | decomposition |
|---|---|---|---|---|
| trading/sfo_kalshi_quant/strategy_research.py | 4,334 | 4.3× | 8 artifact domains + private util tail | TP-10 |
| trading/sfo_kalshi_quant/cli.py | 3,982 | 4.0× | argparse (805 lines) + print formatting (~540) + scan/monitor engine logic | TP-9 |
| trading/sfo_kalshi_quant/db.py | 3,711 | 3.7× | schema, account policy, lifecycle, analytics, diagnostics builders (~900) | TP-8 |
| forecaster/google_weather_cache.py | 2,646 | 2.6× | Google client+budget, fetchers, blend, 3 learners, archive schema, CLISFO refresh | FC-9 |
| trading/sfo_kalshi_quant/datasets.py | 1,591 | 1.6× | acceptable (single domain: dataset ingestion) — no action |
| forecaster/forecast_backtest.py | 1,074 | 1.1× | SFO blend scoreboard; problem is who imports it (FC-8), not its size |

**Import-graph observations** (built this audit): `cli.py` imports ~30 of the package's 44 modules (expected for a dispatcher, but it also *hosts* engine logic — TP-9); `strategy_research.py` imports 15+ modules and re-implements JSON/row utilities privately (TP-11); the forecaster's live-serve chain `emos_forecast → forecast_postproc_backtest → forecast_backtest → google_weather_cache` couples serving to research/legacy code (FC-8), with one cross-package import (`forecast_postproc_backtest.py:394` → `sfo_kalshi_quant.recalibration`) violating the stated package boundary; `postproc_models` imports a 1,074-line backtest module solely for one constant (`SIGMA_FLOOR_F`).

### 2.5 Recommended target structure (the parts that should move)

Full move lists live in the findings; summary:

```
trading/sfo_kalshi_quant/
├── cli.py                  # thin shim → cli/ package (parser.py, scan.py, paper.py, backtest.py, format.py)  [TP-9]
├── monitor.py              # NEW top-level: exit orchestration + queue-ahead maker fill model              [TP-9]
├── db.py                   # facade → store/ package (schema.py, diagnostics.py, scoring.py)               [TP-8]
├── settlement_truth.py     # + single home for ALL bin-resolution semantics                                [TP-11]
├── _util.py                # NEW: shared json/time/row helpers (kills 7× _table_exists etc.)               [TP-11]
├── account.py              # + account_policy_capacity policy math (pure function)                         [TP-8]
├── strategy_research.py    # thin shim → strategy_lab/ package (8 domain modules)                          [TP-10]
└── (root) pyproject.toml   # the ONLY installable manifest; trading/pyproject.toml deleted                 [TP-12]

forecaster/
├── scores.py               # NEW ~60 lines: SIGMA_FLOOR_F, gaussian_crps, normal pdf/cdf                   [FC-8]
├── truth_store.py          # truth + NWP loaders out of the backtest modules                               [FC-8]
├── emos_forecast.py        # entry point, unchanged path; import chain to google_weather_cache severed     [FC-8]
├── google_weather_cache.py # entry point → orchestrates google_api / blend_sources / blend_learners /
│                           #   blend_archive splits                                                        [FC-9]
├── backtests/              # research CLIs, never imported by live code                                    [FC-8]
└── research/               # offline train stack + bootstrap tools (+ the two trading-test path updates)   [FC-7]
```

Constraint respected throughout: systemd invokes `python <file>.py` (forecaster) and `python -m sfo_kalshi_quant.cli` (trading) — every entry file keeps its path and import surface via shims; no systemd/deploy file changes are required by the refactors.

---

## 3. Suggested execution order

Eight batches. Within a batch, items are independent unless stated. **Run the full test suites after every batch** (`.venv-dev/bin/pytest trading/tests forecaster/tests -q` from repo root; `bun run test` for SPA batches). Batches A–C are where the money-correctness risk lives; D–H can proceed in parallel with them by a second implementer, except where conflicts are marked.

**Batch A — Start here (lowest risk, highest value; all S effort, all independent):**
1. DP-1 — delete the ungated `paper-prune` ExecStart line (+ guard test). *The single most urgent change in this plan.*
2. DC-1/DP-2 — `git rm` the two stray unit templates.
3. TB-4 — widen one exception clause.
4. FC-2 — reader-precedence fix (`live` over `rolling_origin`).
5. TP-2 — run the existing index script on the EC2 box (+ init-time warning; verify live state first — see finding).
6. SP-3 — field guards + one error boundary component.
7. TB-3 — adopt the forecaster's sign-safe CLI regex in trading.
8. TB-9 — pass `series_ticker` to the monitor's fee computation.
9. TB-8 — normalize `side` in the SQL partition.

**Batch B — Settlement-truth finality (sequenced; do in this order):**
1. FC-1 fix 1 (serve-gate: refuse only fully-elapsed days) — standalone, restores evening lead-0 serving.
2. FC-1 fix 2 (finality column `is_final` in `cli_settlements`; preliminary never overwrites final; recal/training filters require final).
3. FC-5 (wire `city_truth.py --refresh` into the refresh unit) — **depends on B2** (otherwise it ingests preliminaries faster).
4. TB-2 (settle grace window + `paper-resettle --verify` flag-only sweep) — **depends on B2** for the finality signal; B3 shortens its exposure window.

**Batch C — 15-city intraday correctness:**
1. TB-1 (thread city timezone into the intraday model) — M effort, highest-impact bug; do before any cli.py refactor (conflicts with TP-9).
2. TB-5 (monitor model-read heartbeat for open same-day positions) — touches live exit behavior; gate behind a flag.
3. TB-6 (arb preflight predicate + compensation path).
4. TB-7 (archive gate row-count cross-check).

**Batch D — Box performance (conflicts with Batch H on db.py; do D before H):**
1. TP-1 (scan-context normalization for `diagnostics_json`) — schema change; touches archive.py export lists (coordinate with TB-7).
2. TP-3 (calibration backtest cache), TP-4 (hoisted per-build reads), TP-5 (per-city hoisting in scan), TP-6 (batched Kalshi calls), TP-7 (market_snapshots index).
3. FC-4 (one Open-Meteo fetch per city per tick), FC-6 (drop lead-3 from the daily fetch).

**Batch E — Migration-debris & docs sweep (one long commit; grep `-i lightsail` is the worklist):**
DP-3 + DC-4 + DC-5 (incl. VN-2 garbled sentence) + DC-6 + DC-8 + DC-12 + VN-1 (pyproject `[train]` comment) + DP-4 (dead var) + DC-9/DC-10/DC-11 (historical headers) + DC-7 (trading README reframe — the one L-ish item; a dated banner is the cheap fallback) + DP-5 (pull_paper_db EC2 support) + DP-6 (sync_to_lightsail retire-or-generalize) + DC-14 (env examples) + DP-10 (document the live-gate/archive vars) + SP-13 (meta description) + DC-13/DC-15/DC-16 (hygiene).

**Batch F — SPA robustness & performance:**
1. SP-1 (drop heroui-native + fix trustedDependencies), SP-10 (bounded ranges) — same package.json edit.
2. SP-2 (lazy-load charts; per-component CSS if available — see unproven lead), SP-4 (offline icon bundle), SP-7 (theme pre-hydration script), SP-9 (tick-context split), SP-8 (fonts).
3. SP-11 (data/strategy test tables — fold SP-3's cases in), SP-12 (single money formatter), DC-2/SP-5 (Learnings.tsx: **owner decision** delete vs re-mount), SP-6 (dead assets).

**Batch G — Reliability hardening:**
DP-9 (OnFailure alert unit + disk check) · DP-7 (backfill non-zero exit on failed sources) · DP-8 (locks out of /tmp) · DP-12 (timezone-setup fail-loud or UTC timers) · DP-13 (`${1,,}` → `tr`) · DP-14 (deploy_web_app nits) · FC-10 (drop the two committed-input excludes).

**Batch H — Structural refactors (LAST; each is mechanical-but-wide; run the artifact-diff verification after each):**
1. TP-11 (+_util.py + settlement-semantics single home) — do FIRST; it creates the homes TP-8 moves code into. The four-way bin-logic merge needs case-by-case diff review (verifier: they already diverge on unknown-strike fallback — pick `models.MarketBin.resolves_yes` as canonical and add regression tests for the divergent cases).
2. TP-8 (db.py → store/ package with facade re-exports).
3. TP-9 (cli.py → cli/ package + monitor.py extraction) — **after C1 (TB-1)** lands.
4. TP-10 (strategy_research.py → strategy_lab/ package).
5. FC-8 steps 1–3 (scores.py, truth_store.py, sever the google_weather_cache chain, remove the cross-package import) — independent of trading refactors.
6. FC-9 (google_weather_cache split) — after FC-8.
7. TP-12 (delete trading/pyproject.toml).
8. FC-7 (forecaster dead-code cleanup + research/ segregation — updates two trading tests that import by path).

**Conflict summary:** TB-1 ↔ TP-9 (same functions in cli.py; bug first, refactor after) · TP-1 ↔ TP-8 (same file; perf first, split after) · TP-1 ↔ TB-7 (both touch archive.py export/gate; coordinate in one PR or sequence) · FC-1/FC-5/TB-2 are order-dependent as listed · DP-6 ↔ DP-11 (the fixture decision changes what the sync script may clobber) · DC-7 ↔ everything in Batch E (one docs PR avoids rebase churn).

---

## 4. Considered and rejected (verified false positives — do not re-litigate)

Every item below was suspected, investigated with the cited evidence, and **disproved**. The implementer should not rediscover these.

**Trading engine:**
- Bin-boundary off-by-one in settle/probability/portfolio — bin semantics verified against real strike rows in the DB (`between` inclusive both ends; `greater` strict >; `less` strict <; round-half-up integer settlement consistent across paths).
- `_label_resolves_yes` mis-parsing negative-temperature labels — unreachable: every settle path prefers strike-typed fields; label fallback is legacy-row-only.
- SQLite connections never `.close()`d — CPython refcounting closes them at scope exit in short-lived timer processes; measured no accumulation. Style, not leak.
- `expire_stale_resting_orders` string-timestamp comparison — both sides are fixed-format UTC ISO strings; lexicographic compare is correct.
- `round_contracts` rounding up past the Kelly budget — documented and deliberate (counters 13–41 % under-staking); bounded by hard caps.
- Comfort-boost exceeding per-position risk cap; event-cap leaving fractional contracts; maker queue-ahead using pre-improvement depth (deliberately conservative); `prune_decision_snapshots` dedup subquery; `_worst_case_loss` scenario grid; fixed-PST account-day convention — all verified correct/intentional.
- `_market_implied_yes_value` one-sided-book asymmetry (June audit item) — fixed; bid-only books now midpoint `[bid, 1]`.
- Five June-audit CRITICAL/HIGHs re-verified as genuinely fixed in current code: arbitrage group-close (`group_id` + `HOLD_GUARANTEED_LEG`), settle-overwrite guard (status-conditional UPDATE under `BEGIN IMMEDIATE`), take-profit mislabel (classifies by `exit_kind`), frozen-$1000 Kelly (`size_against_live_equity` on live/research profiles), ask_size==0 sizing (min-ask gate + displayed-ask clamp on taker fills; resting maker quotes deliberately un-capped per 166082b5).
- `PaperStore.init()` migration overhead per CLI start — measured 1.6 ms; short-circuits work.
- pandas/numpy hot loops in the trading engine — the engine imports neither; pure stdlib + sqlite.
- DB free-page bloat — freelist is 0; deleted pages reused; no VACUUM needed.
- `cities_report` per-city N+1 — it aggregates all 15 cities in two passes; only the missing index (TP-2) hurts it.
- Connection-per-call churn — measured ~0.2 ms per connect; noise.
- Missing busy_timeout in read-only direct connections — WAL readers don't block on writers.

**Forecaster:**
- EMOS target-day leakage — clean: history appended strictly after predicting (test-covered).
- `window_rows` off-by-one; recalibration feeding back into itself; sigma-rescale ship state (correctly OFF after failing its own gate) — all clean.
- DST/climate-day handling — consistent fixed-standard-time everywhere, including the 00:00–01:00 PDT trap and Phoenix; the June audit's forecaster-DST finding is fixed.
- GraphCast delisting side effects — handled (historical rows retained; member floor adjusted).
- Google budget arithmetic — within limits (~190 events/day vs 260 cap; reserve-then-adjust ledger; write-before-blend).
- `fit_emos` numerics (degenerate inputs, variance floors) — guarded.
- Memory on the 4 GB box — the live forecaster path is stdlib-only; all units carry MemoryHigh/Max.
- `eda.py` and `forecast_tomorrow.py` as dead code — **they are the sole producers of two published site artifacts** (`weather_story_data.json`, `forecast_data.json`).
- `ab_test_results.json` (2.3 MB tracked) as dead weight — it is a live production calibration input (`forecast.py:72`).
- cities.py / settlement_calendar.py / clisfo.py duplication across packages — intentional, parity-tested, documented.

**SPA / deps:**
- The entire tiptap/shiki/embla/streamdown/marked/react-markdown/react-resizable-panels/react-aria-components/tailwind-* suite as removable dead deps — **all are required non-optional peers of `@heroui-pro/react@1.0.0-beta.6`**, and all are verified zero-cost at runtime (no fingerprints in any built chunk; tree-shaking works). Install-weight only.
- `targetLabel` timezone logic broken for 15 cities — flagship-gated (`city.slug === "sfo"`); non-flagship cities never consult the browser clock (tested).
- "Kalshi" in site copy — clean; the only JSON hit is an unrendered `dataset_research` fixture field.
- Views not code-split; `dist/` committed — both false.
- `useDashboardData` duplicating `useResource` — deliberate `Promise.all` single-load semantics; cosmetic.

**Deploy / infra:**
- Installer missing freshness/prune units — false; both installers render all 9 service+timer pairs.
- Force-push or history clobber in the pages publish — false; bounded re-fetch retry, dedicated deploy key, temp clone with trap cleanup.
- Half-failed publish serving a torn artifact set — false; manifest validation + immutable snapshot copy make it fail-closed (test-covered).
- Unbound-variable crash paths in `$SFO_TRADING_PYTHON` handling; publish holding the artifact lock on early exit; mktemp leak; monitor needing its own overlap lock; `paper_trading.db.local-backup` tracked; committed secrets/IPs — all false (secrets pattern-scan of tracked files clean).
- Repo-wide orphaned Python modules — **none exist**: all 69 forecaster+trading modules are reachable (the full per-module verdict table with per-surface evidence is in the audit working notes; 24 additional looked-dead candidates were saved by cli-dispatch, systemd units, `python -m` invocations in shell scripts, lazy imports, cross-package imports, documented manual entry points, or being sole producers of published artifacts).
---

## 5. Findings in full

Grouped by subsystem. Impact scores are post-verification. "Verified:" states how the independent adversarial verifier confirmed it. Line numbers reference HEAD `bb862f9e`.

### 5.1 Trading engine — correctness (TB)

---

#### TB-1 · Intraday model reads every city's clock as San Francisco time
- **category:** bug (timezone) · **subsystem:** trading · **impact: 8** — same-day probabilities are mispriced for 12 of 15 cities; the intraday model's hour-of-day drives remaining-heat climatology, sigma, and the observed-high blend weight, which feed every same-day entry/exit probability. · **confidence:** high — verifier independently re-traced the full chain end-to-end: timestamps are UTC at source, no city parameter exists anywhere in the chain, no disabling override, no pinning test. · **effort:** M
- **location:** `trading/sfo_kalshi_quant/probability.py:501-509` (`_local_decimal_hour`); `trading/sfo_kalshi_quant/forecast.py:686-702` (`_intraday_weight`)
- **evidence:**
  ```python
  # probability.py:508              # forecast.py:691
  local = observed_dt.astimezone(SFO_TZ)   local_hour = observed_dt.astimezone(SFO_TZ).hour
  ```
  `SFO_TZ = ZoneInfo("America/Los_Angeles")` (config.py:11). `local_hour` feeds `_climatological_remaining_heat_f` (probability.py:512-534), `_intraday_sigma_f` (537-561), `_intraday_blend_weight` (564-583), and the observed-high anchor weighting (forecast.py:694-702). None receive a city. The intraday snapshot is built per city (`forecast.py:108-197`) and passed into `bucket_probabilities` on every same-day scan (`cli.py:1471-1474, 1516-1524`; gate at `cli.py:1949-1971`).
- **root_cause:** the intraday model predates the 15-city expansion (7/06); its clock was never city-parameterized.
- **why_it_matters:** a 14:00 EDT observation in NYC is treated as 11:00 — the model believes ~2.2 °F of climatological upside remains (vs ~1.4/0.8), uses sigma 1.10 (vs 0.90/0.70) and blend weight 0.40 (vs 0.55/0.65). Eastern cities systematically under-condition on the observed high → overpriced above-observed tails and underweighted "high already set" evidence, on both sides. Central/Mountain cities off 1–2 h in the same direction.
- **fix:** thread `CityConfig` (or its `fixed_standard_timezone()`) into `bucket_probabilities` → `_intraday_probability_model` → `_local_decimal_hour`, and into `apply_intraday_update` → `_intraday_weight`. Use the station's standard-time zone (the hour tables describe the standard-time-anchored diurnal cycle; the entry cutoff already uses `settlement_clock(city)`). Pass SFO's zone for the default city so SFO behavior stays bit-identical.
- **verification:** new unit test: `IntradaySnapshot` with `latest_observed_at = "2026-07-10T18:00:00+00:00"` (14:00 EDT) → assert the NYC path uses hour 13.0 (EST standard), not 11.0, and that blend weight/sigma shift accordingly; then `pytest trading/tests/test_multi_city.py trading/tests/test_forecast_dates.py`.
- **risk:** low-medium — intentionally changes live same-day probabilities for non-SFO cities; SFO unchanged. · **conflicts_with:** TP-9 (same cli.py region) — land this first.

---

#### TB-2 · Auto-settle can permanently book PnL off a preliminary evening CLI report
- **category:** correctness (settlement truth precedence) · **subsystem:** trading · **impact: 6** (finder said 7; verifier corrected — during DST the target only becomes settle-eligible at 01:00 civil, after most finals post, so the race concentrates in winter/standard time) · **confidence:** high on mechanism — the repo's own test fixture proves an "AS OF 0500 PM" preliminary parses to a settleable date+max, and no resettle path exists. · **effort:** M
- **location:** `trading/sfo_kalshi_quant/settlement.py:20-45` (`fetch_recent_cli_settlements`); `cli.py:3074-3158` (`cmd_paper_auto_settle`), `cli.py:3161-3176` (`_completed_open_target_dates`); `db.py:2079-2102` (settled rows immutable)
- **evidence:** `fetch_recent_cli_settlements` scans product versions newest-first and takes the first parse per report date; `parse_clisfo` extracts only `CLIMATE SUMMARY FOR <date>` + `MAXIMUM <nn>` — no check for the "AS OF <time>"/preliminary marker distinguishing the ~4–6 PM partial-day report from the after-midnight final. `_completed_open_target_dates` makes a target eligible the moment `target < settlement_today(city)`; the settle timer fires :10/:40. At the first tick after station-standard midnight the newest CLI version covering yesterday can still be the evening preliminary (live-verified for SFO: preliminary issued ~17:32 PDT, final at 01:34 — see FC-1). Once rows flip to PAPER_SETTLED, the `WHERE … status='PAPER_FILLED'` guard (db.py:2090) means a later final can never correct them.
- **root_cause:** "corrected finals shadow preliminaries" (settlement.py docstring) only holds if the final exists at settle time; there is no wait-for-final or re-verification pass.
- **why_it_matters:** Kalshi settles on the final report. Any city-day whose high lands after the preliminary issuance (or gets a next-morning correction) is booked wrong permanently and silently — corrupting equity, breakers, Strategy Lab, and the posterior-Kelly trust record.
- **fix (either, prefer 1+2 together):** (1) respect finality: skip a report whose text contains the "AS OF"/preliminary block unless its issuance time is after the station's standard midnight for that report date — after Batch B2 this is simply "require `is_final=1`"; (2) settle grace window: only settle a target once `settlement_clock(city) ≥ 06:00` the next standard day, plus a `paper-resettle --verify` sweep that re-reads the final CLI for the last N days and *flags* (never overwrites) mismatches.
- **verification:** unit test feeding two versions of a CLI product (preliminary MAXIMUM 68 "AS OF 4 PM", final MAXIMUM 71) asserting the settle path refuses/waits; integration test that a PAPER_SETTLED row mismatching the final is flagged.
- **risk:** low — delays settlement by a few hours; monitor/exposure paths already tolerate open orders overnight. · **depends_on:** FC-1 fix 2 (finality column) for the clean variant; FC-5 shortens the exposure window.

---

#### TB-3 · CLI parser cannot read negative or single-digit maxima
- **category:** bug (parsing) · **subsystem:** trading · **impact: 3** (WEAKENED from 6 — verifier proved the auto-settle fallback reads weather.db `cli_settlements`, fed nightly by the IEM JSON backfill which is numeric and sign-safe, so affected winter days settle ~1 day late rather than never) · **confidence:** high — repro executed both by finder and verifier: `MAXIMUM -2 …` → `None`; `MAXIMUM 8 …` → `None`. · **effort:** S
- **location:** `trading/sfo_kalshi_quant/settlement.py:141-150` (`_parse_max_temperature`)
- **evidence:** patterns `r"MAXIMUM\s+(\d{2,3})\b"` etc. — no optional sign, no single digit. Good news: the anchor requires digits immediately after `MAXIMUM`, so the failure mode is no-parse (delayed settle), never wrong-parse. The forecaster's equivalent parser is **already fixed** (`forecaster/clisfo.py:114`: `(-?\d{1,3})` + section anchoring).
- **root_cause:** written for SFO where 2-digit positives always hold; 15-city expansion added CHI/DEN/BOS/NYC winters.
- **why_it_matters:** first cold snap → those city-days settle a day late off IEM instead of the primary path; the open cost meanwhile consumes shared-account risk caps (`account_policy_capacity` sums active orders, db.py:714-733) → mild entry starvation, and the primary/fallback disagreement is invisible.
- **fix:** adopt the forecaster's pattern: change the capture to `(-?\d{1,3})` in all three regexes.
- **verification:** extend `trading/tests/test_forecaster_clisfo.py` with `MAXIMUM -2` and `MAXIMUM 8` fixtures; `pytest trading/tests/test_forecaster_clisfo.py`.
- **risk:** trivial.

---

#### TB-4 · `except URLError` misses `KalshiUnavailable`: one brownout kills the whole multi-city scan tick
- **category:** bug (exception taxonomy) · **subsystem:** trading · **impact: 4** (verifier: a brownout's graceful counterfactual also places nothing — the real loss is the decision journal + research shadows for that tick, and the noisy exit-1) · **confidence:** high. · **effort:** S
- **location:** `trading/sfo_kalshi_quant/cli.py:1149-1172` (`_resolve_analysis_targets`) vs `kalshi.py:21-27, 81-92`
- **evidence:** cli.py:1155 is the only remaining `except URLError:`-bare handler (grep-verified). `KalshiPublicClient.get_json` deliberately raises `KalshiUnavailable(OSError)` after retries (kalshi.py:89) so existing `(URLError, OSError)` handlers treat exhausted retries as transient — but `KalshiUnavailable` is not a `URLError`, so it escapes to main()'s catch-all (cli.py:105-107) and aborts `cmd_portfolio_scan` for all cities/profiles that tick. Every other Kalshi call site catches `(URLError, OSError)` or wider (cli.py:1311, 1494, 1680, 1802, 2112, 2778, 2990). No test covers this path.
- **root_cause:** handler written before `KalshiUnavailable` existed; missed in the widening sweep.
- **fix:** cli.py:1155 → `except (URLError, OSError) as exc:`.
- **verification:** unit test monkeypatching `list_event_snapshots` to raise `KalshiUnavailable`, asserting the command returns `([], {})` with a warning instead of raising.
- **risk:** none meaningful.

---

#### TB-5 · Monitor's model read structurally dies every afternoon for same-day positions
- **category:** correctness (design vs schedule) · **subsystem:** trading · **impact: 5** — after the same-day entry cutoff (14:00 station-standard) + 90 min, every open same-day position loses its model read *by construction*: edge-based take-profit silently degrades to the unreachable legacy %-target and NO stops become permanent `HOLD_NO_MODEL_READ`, precisely in the hours the high resolves. Caveat (verifier): settlement-first holds for expensive NO favorites are by design (0e0328ec) — the unintended damage is to YES and cheap-NO exits. · **confidence:** high on mechanism. · **effort:** M
- **location:** `cli.py:1253-1257` (`_rolling_live_event_targets`: after `local_now.hour >= 14` today's markets stop being scanned, so `decision_snapshots`/`probability_snapshots` stop for them); `db.py:921-955` (`latest_model_probability`, `max_age_minutes=90`); `exits.py:238-262` (`HOLD_NO_MODEL_READ` framed as a data-source *failure*); `cli.py:2826-2845` (monitor wiring)
- **evidence:** the only writers of the model read (scan ticks) exclude today's target after the cutoff, so from ~15:30 station-standard until settlement the read is >90 min old and returns None — a *daily scheduled state*, though the exits.py comment describes it as the stale-data failure mode.
- **root_cause:** the model-read freshness contract and the entry-cutoff target rotation were designed independently; nothing keeps a model-read heartbeat for markets with OPEN positions.
- **fix:** in the monitor, before falling back: compute a model read on demand for open positions (reuse last EMOS mu/sigma via `load_emos_mu_sigma` + observed high), or keep scanning today's event journal-only after the cutoff so snapshots continue while positions are open; at minimum rename the monitor reason so afternoon holds are distinguishable from genuine pipeline staleness.
- **verification:** integration test: place a same-day order, advance clock past cutoff+90 min with snapshots stopped, assert the monitor still produces a model-based exit decision (post-fix) instead of `HOLD_NO_MODEL_READ`.
- **risk:** medium — touches live exit behavior; gate behind a flag and compare monitor actions for a few days. · **conflicts_with:** TP-9 (monitor extraction) — land this first or fold into the extraction PR with its new unit test.

---

#### TB-6 · `place_arbitrage` can record a partial box via a preflight/guard-index predicate gap
- **category:** bug (check-then-act) · **subsystem:** trading · **impact: 4** (verifier: research-profiles-only, small stakes; mechanism concrete) · **confidence:** medium-high — predicate gap verified concretely (guard index covers `PAPER_FILLED`+`PAPER_LIMIT_RESTING`; preflight checks `PAPER_FILLED` only; research allows `max_entries_per_market_side: 3`). · **effort:** M
- **location:** `paper.py:376-465`; `db.py:1709-1741` (`has_open_paper_position` — FILLED only); `db.py:764-789, 1318-1323` (guard index incl. RESTING); `config.py:482`
- **evidence:** a resting maker order on a leg's market/side passes preflight (paper.py:407-422) but trips the unique open-position index at insert → `record_paper_order` returns None → paper.py:455 raises AFTER earlier legs committed (each insert its own transaction; comment at paper.py:461-464 admits no partial-rollback API). The RuntimeError also aborts the rest of the city/target loop (only `ForecastDataError` is caught per-target, cli.py:1113-1132). The partial box is then *deliberately held to settlement* as a "guaranteed-payoff group" (`HOLD_GUARANTEED_LEG`, cli.py:2746-2761) — a naked directional position with no exit management.
- **fix:** (a) preflight with `has_active_paper_entry` (FILLED+RESTING, already exists at db.py:1743) per leg ticker and side; (b) on mid-box `order_id is None`, compensate (cancel resting legs / mark the group degraded) instead of raising; (c) catch the RuntimeError in `_portfolio_scan_one_target` so one box cannot kill the tick.
- **verification:** unit test: seed a PAPER_LIMIT_RESTING YES order on ticker T (research profile), submit an approved box including T, assert zero legs recorded and no exception.
- **risk:** low.

---

#### TB-7 · Archive `id_floor` assumes id order == created_at order; a midnight inversion silently drops rows the prune then deletes
- **category:** correctness (data-loss guarantee weaker than documented) · **subsystem:** trading · **impact: 4** (verifier note: UTC midnight = 4–5 PM PT — peak scan activity, so the inversion window is likelier than it sounds; the trigger still needs a concurrent manual scan bypassing the flock) · **confidence:** medium — logic proven; live incident unobserved. · **effort:** M
- **location:** `archive.py:203-256` (`_day_select` id_floor), `archive.py:417-433` (floor seeding), `archive.py:466-489` (gate checks file existence, not row coverage); `db.py:1040-1168` (`record_decisions` stamps `created_at` early, inserts late — a window of hundreds of ms)
- **evidence:** comment at archive.py:218-224 claims "id order matches day order". Writer A stamping 23:59:59.9 (day D−1) but inserting after writer B's 00:00:00.1 (day D) row gets the higher id and lands in D−1's file; B's row (day D, smaller id) is excluded from D's export by `id > floor` and never archived; the prune later deletes it. Mitigation today: the scan wrapper serializes profiles under flock — but a *manual* `portfolio-scan` (common per project practice) bypasses it.
- **fix:** make the gate catch omissions instead of trusting the floor: add a row-count cross-check (`SELECT COUNT(*) WHERE created_at in day` vs manifest rows) to `--check-gate` so any omission fails the gate; optionally seed `id_floor` with slack (`MAX(max_id) − K`).
- **verification:** unit test constructing an id/created_at inversion across midnight, asserting export+gate either includes the row or refuses to pass.
- **risk:** low (gate-side check is read-only). · **conflicts_with:** TP-1 (both touch archive.py) — coordinate.

---

#### TB-8 · SQL sampling partitions by raw `side`; legacy NULL-side rows collapse YES+NO into one sample
- **category:** correctness (refactor parity) · **subsystem:** trading · **impact: 1** (WEAKENED from 3 — verifier measured 0 NULL-side rows among 102,656 in the journal; the divergence is real in code but currently affects nobody) · **confidence:** high on divergence. · **effort:** S
- **location:** `db.py:2574-2591` (SQL window path: `PARTITION BY target_date, market_ticker, side`) vs `db.py:3363-3397` (Python fallback normalizes via `_row_side`)
- **fix:** partition by `UPPER(COALESCE(side, CASE WHEN instr(UPPER(action),'NO')>0 THEN 'NO' ELSE 'YES' END))` to mirror `_row_side`. Note also the SQL pre-filter compares `created_at < market_close_time` as raw strings — correct only while both stay UTC-rendered; add a comment or normalize.
- **verification:** unit test with two NULL-side rows (BUY_YES/BUY_NO) on one market asserting both survive `entry-per-market-side` sampling.
- **risk:** none.

---

#### TB-9 · Monitor decides exits with a differently-rounded fee than the close books
- **category:** correctness (numeric consistency) · **subsystem:** trading · **impact: 2** — near-threshold exits can differ from the displayed reason by up to ~1¢/contract; decision threshold is slightly more conservative than booked PnL. · **confidence:** high. · **effort:** S
- **location:** `cli.py:2813` (monitor computes `exit_fee` with no `series_ticker` → whole-cent ceil) vs `db.py:2124-2126` (close recomputes with `series_ticker` → centicent rounding, fees.py:41-44); same pattern in `paper.py:49` (`with_paper_stake`, non-bankroll path only)
- **fix:** pass `series_ticker=str(row["market_ticker"])` (and config rates) at cli.py:2813; pass config rates + series in `with_paper_stake`.
- **verification:** boundary-case assertion in `test_exits.py` that `decide_exit`'s net-exit equals the value implied by the booked `exit_fee_per_contract`.
- **risk:** none.
### 5.2 Forecaster (FC)

---

#### FC-1 · Preliminary evening CLI reports are stored as settled truth — evening lead-0 freeze + recalibration contamination
- **category:** bug (correctness / train-serve skew) · **subsystem:** forecaster · **impact: 7** — touches the same-day market (the highest-frequency target of the 7/10 recalibration ship) in every city every day, and injects provisional truth into the bias window that moves live mu. Mitigated one layer down by trading's observed-high conditioning, else 8–9. · **confidence:** high — **live-verified during this audit**: at 17:33 PDT the real SFO CLI version feed showed the evening preliminary ("532 PM PDT … VALID TODAY AS OF 0500 PM LOCAL TIME") with the final arriving 01:34 next day; at 20:33 EDT, IEM already served a KNYC row for the in-progress climate day. Schema has no finality column. · **effort:** S (fix 1) + M (fix 2)
- **location:** `forecaster/emos_forecast.py:303-306` (refusal gate); `forecaster/city_truth.py:8-13, 144-149` (docstrings admitting preliminary ingestion; last-write-wins); `forecaster/clisfo.py:31-49`; `forecaster/google_weather_cache.py:602-627` (`refresh_clisfo_settlements` — every 30-min tick for SFO); `trading/deploy/aws/systemd/sfo-dataset-backfill.service.in` (`--backfill-iem` 02:25 UTC for all 15)
- **evidence:**
  ```python
  # emos_forecast.py:303
  if target_iso in truth:   # refuse to serve — but "truth" includes today's PRELIMINARY
      return None
  ```
  `truth = load_clisfo_truth()` = every `cli_settlements` row, final and preliminary alike. SFO's preliminary lands within ≤30 min of ~17:30-17:45 PDT issue; for the other 14 cities the 02:25 UTC IEM backfill stores the afternoon preliminary for a day still in progress (02:25 UTC = 21:25 EST … 18:25 PST).
- **root_cause:** `cli_settlements` conflates "a CLI product exists for this date" with "this date is settled."
- **why_it_matters:** (1) evening lead-0 freeze: SFO's same-day EMOS distribution stops refreshing ~17:30 PDT → midnight (~6.3 h of open market, daily; live-verified timing); other cities freeze from 02:25 UTC to local midnight. The shape above the observed high stays whatever the last pre-freeze serve said. (2) Preliminary highs sit in training/recal windows as truth for ~24 h (SFO self-corrects in ≤30 min; others wait for the next nightly): on evening-peak days the bias window sees too-low truth for yesterday and mis-corrects today. (3) The refusal note ("would shadow the rolling-origin row") is wrong for lead 0 — no lead-0 rolling-origin rows exist.
- **fix:** (1) in `serve_live_emos`, replace the `target_iso in truth` gate with a settled-day gate: refuse only when `target_date < _settlement_today(city)`. This alone restores evening lead-0 serving. (2) Track finality: add `is_final INTEGER DEFAULT 1` to `cli_settlements`; classify a report preliminary when its issuance time precedes the climate-day end; in `city_truth.upsert_settlement` never let a preliminary overwrite a final; require `is_final=1` in `load_scored_series` (emos_recalibration.py:99-110) and the serve-history filter. Cheap interim variant: in `backfill_iem`/`refresh_clisfo_settlements`, skip rows where `local_date >= today_local_standard(city_tz)`.
- **verification:** NEW test (the existing refusal test at `forecaster/tests/test_emos_forecast.py:86` covers a *past* day and stays green — verifier-checked): insert a `cli_settlements` row for *today*, call `serve_live_emos(..., target=today)` with stubbed `live_models` → returns None today, (mu, sigma) after fix 1. On the box, evening check: `SELECT fetched_at FROM forecast_emos_daily_high WHERE lead_days=0 AND target_date=date('now','localtime')` — stamp should keep advancing after the preliminary arrives.
- **risk:** fix 1 is gate-only, low. Fix 2 adds a column read by trading (`archive.py`, `cities_report.py` read `cli_settlements`) — additive column keeps them working. · **depended_on_by:** FC-5, TB-2.

---

#### FC-2 · Nightly rolling-origin rebuild re-stamps `fetched_at` on every row — trading reads the uncorrected mu for ~15 min nightly
- **category:** bug (consumption seam) · **subsystem:** forecaster/trading seam · **impact: 6** — every night, 02:25→~02:40 UTC, the 5-min scan and 2-min monitor read a mu/sigma for open targets that has no serve-time bias recalibration (recal bias runs ~1.0–1.5 °F for the shipped cities — enough to flip an edge decision on a 2 °F bucket) and comes from the previous-runs reconstruction, not the current run. · **confidence:** high — code path verified end-to-end by finder and verifier (stale-live-stamp detail: last live tick is 01:40 UTC). · **effort:** S
- **location:** `forecaster/emos_forecast.py:158-176` (`build_emos_archive`: one `stamp` applied to ALL rows, including future targets); `trading/sfo_kalshi_quant/forecast.py:336-343` (`_latest_emos_snapshot`: `ORDER BY fetched_at DESC LIMIT 1`, no source filter); `forecast.py:576-614` (`load_emos_mu_sigma`: "later (fresher) rows win", called with `source=None` from the live scan at cli.py:1329/1514/1820); timers: backfill 02:25 vs next serve 02:40
- **evidence:** the rebuild `INSERT OR REPLACE`s the full history *including tomorrow* (NWP `--daily` covers `end=today+1`, nwp_archive.py:438), stamping every rolling-origin row with tonight's time; both trading readers resolve (station, target) by max `fetched_at` across sources, so the rolling-origin row outranks the 01:40 live row until 02:40. Paper scans run 02:25/:30/:35.
- **root_cause:** `fetched_at` means "rebuild time" on rolling-origin rows but is consumed as issue-time recency across sources.
- **fix (pick one; first is smallest):** (1) trading-side precedence: `ORDER BY CASE source WHEN 'live' THEN 0 ELSE 1 END, fetched_at DESC` in both readers (dict variant: iterate rolling_origin first so live overwrites). (2) forecaster-side: preserve original `fetched_at` for existing keys, or stop writing rolling-origin rows for `target_date >= settlement_today`. (3) weakest band-aid: append a `--serve-rolling` ExecStart to the nightly unit.
- **verification:** unit test inserting both rows and asserting `_latest_emos_snapshot` picks live; on the box after 02:25: `SELECT source, fetched_at FROM forecast_emos_daily_high WHERE station_id='KNYC' AND target_date=date('now','+1 day') ORDER BY fetched_at DESC` shows live on top post-fix.
- **risk:** option 1 is a pure precedence change; option 2's skip-unsettled variant doesn't affect scored reads (they filter `actual_high_f IS NOT NULL`).

---

#### FC-3 · Rolling-origin archive is fit on truth the live serve cannot have — ship-gate numbers are slight upper bounds
- **category:** bug (train/eval skew — not target leakage) · **subsystem:** forecaster · **impact: 4** — the evaluation record that gates ship decisions (inv-var DM −3.60, replay pooled −1.3 % CRPS) uses 1 extra settled day at lead 1 and 2 at lead 2 vs what live serving can see; measured skill is a mild upper bound, growing with lead. No leakage of the target day itself (verified: history appended strictly after predicting). · **confidence:** high mechanism; magnitude unquantified. · **effort:** S–M
- **location:** `forecaster/postproc_models.py:197-210` (rolling-origin fit for target D uses all settled days < D); `forecaster/emos_forecast.py:307-311` (live serve at S=D−lead can have at most ≤ S−1); input-side asymmetry documented in-repo (emos_forecast.py:291-295) so only the evaluation consequence is flagged
- **fix:** add optional `truth_lag_days: int = 0` to `emos_ngr_predictions`; `build_emos_archive` passes `lead_days` (history for target D restricted to `d <= D − lead − 1`). Cheaper evaluation-only alternative: run the one-off A/B (scratch DB, `recalibration_replay.py --days 60` with/without the lag, one city) and if the delta is negligible, document as a known limitation instead of changing the archive.
- **verification:** the A/B above; if changing fits, label rows `source='rolling_origin_v2'` for scoreboard continuity.
- **risk:** changing the archive's fits shifts every downstream scoreboard number once.

---

#### FC-4 · `--serve-rolling` makes 3 identical Open-Meteo requests per city per tick
- **category:** perf-runtime · **subsystem:** forecaster · **impact: 4** — ~1,710 calls/day vs ~570 needed; the in-code budget comment ("~720/day") is 3× off; ~30 avoidable sequential 20 s-timeout round trips per tick inside a `TimeoutStartSec=900` oneshot shared with the Google refresh. · **confidence:** high — params verified target-independent (`forecast_days=3`, response indexed per target). · **effort:** S
- **location:** `forecaster/emos_forecast.py:210-262` (`fetch_live_model_forecasts`), `emos_forecast.py:438-458` (main loop calls `serve_live_emos` per target with `live_models=None` → re-fetch per target)
- **fix:** fetch once per city: new `fetch_live_model_forecasts_multi(city)` returning `{date_iso: {model: value}}` (same response, iterate `times`), pass `live_models=payload_by_target.get(target.isoformat(), {})` per target. Keep the single-target function for tests.
- **verification:** stub `urlopen` in `forecaster/tests/test_emos_forecast.py` and count calls per tick (15 expected, not 45); fix the stale in-code comment.
- **risk:** none meaningful.

---

#### FC-5 · `city_truth.refresh_live` is wired into nothing — 14 cities' settlement truth rides one nightly IEM pull
- **category:** bug (deploy gap) + perf-model · **subsystem:** forecaster/deploy · **impact: 5** — non-SFO truth is up to ~24 h stale (keeps FC-1's preliminary contamination window open all day; delays the EMOS training append; single point of failure: one failed nightly = 15 cities' truth ages 48 h while the DB-mtime watchdog stays green, since other writers keep touching weather.db). · **confidence:** high — grep-verified: zero invocation surfaces pass `--refresh`; the module docstring claims "two writers keep it current" but the deployed system runs one. · **effort:** S
- **location:** `forecaster/city_truth.py:101-125`; `trading/deploy/aws/systemd/sfo-dataset-backfill.service.in:19`; `sfo-forecaster-refresh.service.in` (no city_truth call); `trading/deploy/aws/check_forecast_db_freshness.sh:36-44` (file-mtime check only)
- **fix:** add to `sfo-forecaster-refresh.service.in` (with `-` prefix, before the EMOS serve): `ExecStart=-__FORECASTER_DIR__/.venv/bin/python __FORECASTER_DIR__/city_truth.py --db __FORECASTER_DIR__/weather.db --refresh --cities all`. If 15×6 product fetches per 30-min tick is too chatty, use a separate 2-hourly timer or `versions=3`. **Land after FC-1 fix 2** so added freshness doesn't just ingest preliminaries faster. (Verifier note: this wiring is also the cheap mitigation for TB-2 and TB-3 — finals arrive intraday instead of at 02:25.)
- **verification:** after wiring: `SELECT station_id, MAX(local_date), MAX(fetched_at) FROM cli_settlements GROUP BY station_id` — non-SFO `fetched_at` advances intraday; yesterday's final appears shortly after local midnight.
- **risk:** forecast.weather.gov rate limiting — keep fail-soft per-city behavior and the `-` prefix. · **depends_on:** FC-1 fix 2.

---

#### FC-6 · Lead-3 NWP archive fetched nightly for all cities; nothing consumes it
- **category:** perf-runtime / product decision · **subsystem:** forecaster · **impact: 2** — 120 previous-runs requests/night (a third of the nightly NWP fetch) building data reachable only via a research CLI; no lead-3 EMOS is built and `--serve-rolling` covers leads 0–2. · **confidence:** high (`LEAD_DAYS = (1, 2, 3)` nwp_archive.py:83; unit builds leads 1–2 only; `ROLLING_SERVE_DAYS = 3` = offsets 0..2). · **effort:** S
- **fix:** drop lead 3 from the daily fetch (`archive_range(..., leads=(1,2))` in `_cmd_daily`; keep for `--backfill`), or deliberately extend serving to lead 3. The two-line (a) is recommended; previous-runs API is historical, so backfill can always restore.
- **verification:** nightly journal `daily[...]` row count drops ~1/3; coverage report unchanged for leads 1–2.

---

#### FC-7 · Forecaster dead/stale inventory (reference-swept)
- **category:** dead code/artifacts · **subsystem:** forecaster · **impact: 3** aggregate · **confidence:** high — every claim swept repo-wide across .py/.sh/.in/.toml/.md + `git ls-files` + rsync manifests; verifier re-ran the decisive greps. · **effort:** S–M
- **items:**
  - **Delete (untracked local leftovers of the retired HTML dashboard, ignored by the ROOT .gitignore):** `forecaster/details.html` (229 KB), `index.html` (152 KB), `strategy-lab.html` (165 KB), `strategy_research.protected.json` — zero code refs; dev-Mac `rm`, not a commit.
  - **`git rm forecaster/model_compare_results.json`** — see DC-3 for the nuance (provenance; document instead of delete is also acceptable).
  - **Dead symbols in live modules:** `emos_forecast.load_emos_archive` (:190-207, test-only, and lacks a `source` filter — a trap for future callers); `nwp_archive.SETTLEMENT_TZ`, `KSFO_LATITUDE/LONGITUDE` (:50-56, zero refs); `clisfo.fetch_recent_clisfo_settlements` + `_recent_clisfo_urls` (:58-87, SFO-hardcoded duplicates of the generic function — fold into `fetch_recent_cli_settlements("MTR","SFO")`); `fallback_sigma` "retained for API stability" (forecast_postproc_backtest.py:242, nothing reads it).
  - **Segregate, don't delete:** `forecast_tomorrow.py`, `load_to_db.py`, `combine_psv.py`, `eda.py`, `lstm_model.py`, `xgboost_model.py`, `ab_test.py`, `compare_models.py`, `features.py`, `forecast_validation.py`, `fetch_inland_history.py` (zero refs but the documented producer of the inland rows the feature pipeline needs) → `forecaster/research/`. **Two trading tests import these by path** (`test_inland_leakage.py` → features, `test_clean_forecast_scoring.py` → forecast_validation) — update both when moving.
  - `forecaster/tests/run_tests.py` — redundant homegrown runner (pytest owns both suites via root pyproject); delete unless CI uses it (grep found only docs refs).
  - `ab_test_results.json` (2.3 MB tracked) is LIVE (trading reads it) — keep, but consider regenerating a slimmed artifact (the reader needs date/pred/actual rows, not chart payloads).
- **verification:** `pytest forecaster/tests trading/tests -q` (the two path-coupled trading tests are the canary); `rsync -n` dry-run of `sync_forecaster_source.sh` to confirm the box tree is unchanged.
- **risk:** low. · **conflicts_with:** FC-8/FC-9 (same files) — fold into the Batch H forecaster PR.

---

#### FC-8 · The live 30-minute serve transitively imports the 2,646-line SFO legacy module; forecaster imports the trading package
- **category:** structure · **subsystem:** forecaster · **impact: 5** — blast-radius risk with precedent (db706010: "source-MOS crash took down the whole refresh"). **Runtime-proven this audit:** importing `emos_forecast` pulls `google_weather_cache` + `forecast_backtest` into `sys.modules`; on bare python3.9 the import *crashes inside google_weather_cache* (`float | None` at :642 without future-annotations) — a concrete demonstration that SFO-legacy code can kill the 15-city serve at import time (and an accidental Python ≥3.10 hard requirement). · **confidence:** high. · **effort:** M
- **evidence (import chain):** `emos_forecast` → `forecast_postproc_backtest` (for two 10-line truth loaders) → `forecast_backtest` (1,074 lines) → `google_weather_cache` (2,646 lines); `postproc_models` → `forecast_backtest` **only for `SIGMA_FLOOR_F`** (postproc_models.py:39). Cross-package break: `forecast_postproc_backtest.py:394` does `from sfo_kalshi_quant.recalibration import fit_by_cohort` — contradicting the stated rule ("the two packages do not import each other", cities.py:20-23); lazy/research-arm-only, but it binds the forecaster venv to the trading package. Duplication beyond the intentional trio: `gaussian_crps` ×2 in-forecaster, `_settlement_today` ×2, `_table_exists` ×3+.
- **fix (migration order that never moves a systemd-invoked file):** (1) create `forecaster/scores.py` (~60 lines: `SIGMA_FLOOR_F`, `gaussian_crps`, `_normal_cdf/pdf`, integer-bin Brier helpers); flip `postproc_models` + both backtests to it. (2) Move `load_clisfo_truth`/`load_nwp_forecasts` into `city_truth.py` (or new `truth_store.py`); re-point `emos_forecast`; keep thin re-export shims in `forecast_postproc_backtest`. (3) Replace the `sfo_kalshi_quant` lazy import by duplicating `fit_by_cohort` into the forecaster with a parity test (the repo's established pattern, exactly like cities.py) — or delete the `emos_wmean_recal` research arm if concluded. (4) Only then optionally shuffle research CLIs into `backtests/` and the train stack into `research/` (FC-7). Steps 1–3 already sever the live chain.
- **verification:** in a clean venv: `cd forecaster && python -c "import emos_forecast, sys; assert 'google_weather_cache' not in sys.modules"`; 72 forecaster tests + trading tests green.
- **risk:** import-shim mistakes — mitigated by shims + tests; no systemd paths change.

---

#### FC-9 · `google_weather_cache.py`: 2,646 lines, 7 responsibilities (budget mechanics verified sound)
- **category:** structure · **subsystem:** forecaster · **impact: 3** — works and is well-commented, but it is the file whose crash took down the whole refresh once (db706010), and its size is why: Google client + event-budget ledger, NWS/Open-Meteo fetchers, blend + three learned post-processors, observed-high lock, SQLite archive/migrations/scoring, CLISFO refresh. 3.3× the repo's own 800-line guidance. · **confidence:** high (full structure map read). · **effort:** M–L
- **budget assessment (audit-requested):** sound — ~5 events/refresh estimated pre-fetch, reserve-then-adjust ledger with daily (260) and monthly (8,000/10,000 free) gates, write-before-blend, civil-local budget day deliberately separate from the settlement clock. ~38 refreshes/day → ~190 events/day, ~5.7 K/month. Two nits: the unit's second pass (plain re-blend) repeats `update_scores` incl. `refresh_clisfo_settlements` (up to 10 sequential 20 s-timeout fetches — a slow forecast.weather.gov stalls the tick twice); legacy `usage["limit"]/["calls"]` aliases maintained forever — check whether anything still reads them, then drop.
- **fix:** split along existing function-cluster seams: `google_api.py` (fetch/parse/budget), `blend_sources.py`, `blend_learners.py` (pure functions), `blend_archive.py` (schema/scoring), thin `google_weather_cache.py` entry point (preserves the systemd invocation). Do after FC-8 steps 1–2. Add learner-gate unit tests during the split (the walk-forward gates at :1677-1698 and :2093-2140 are prime targets; current test file has only 4 tests for 2,646 lines).
- **risk:** medium (live SFO blend path) — move code verbatim; keep the CLI byte-compatible. · **depends_on:** FC-8.

---

#### FC-10 · Source sync excludes two committed inputs — box copies frozen at seed time
- **category:** bug (config drift) · **subsystem:** deploy/forecaster seam · **impact: 3** — `forecast_data.json` and `weather_story_data.json` are tracked inputs (blend "history" source at 8 % weight, google_weather_cache.py:1292-1304; required publish artifacts, publish_forecaster_pages.sh:33-38) but excluded from the git→box rsync, so regenerating them in git silently never reaches production. · **confidence:** high (exclude list + `git ls-files` verified). · **effort:** S
- **location:** `trading/deploy/aws/sync_forecaster_source.sh:61-74`
- **root_cause:** the exclude list conflates "artifacts the box produces" (correct to exclude) with "static inputs the box consumes that sit next to them."
- **fix:** remove the two excludes. · **verification:** on the box: `md5sum /opt/weatheredge/forecaster/forecast_data.json` vs `git show main:forecaster/forecast_data.json | md5sum` — mismatch today proves drift; equal after fix.
- **risk:** one-time content refresh on the box. · **conflicts_with:** DP-11 (if the fixtures become gitignored, this finding's fix changes shape — decide DP-11 first).
### 5.3 Trading engine — performance & structure (TP)

DB numbers were measured on a byte copy of the 239 MB production pull (M-series Mac; multiply ~2–3× for the t4g.medium ARM box). **The pull's data ends 2026-06-27** — row-level claims are "state at pull time"; TP-2 carries the explicit live-box re-check. Baseline: page_size 4096, WAL, freelist 0, **no `sqlite_stat1` (ANALYZE never run)**; decision_snapshots 102,656 rows/125.4 MB; ~9,216 decision rows/day at 15-city/2-profile/5-min cadence.

---

#### TP-1 · `diagnostics_json` write amplification: identical per-tick context duplicated across every row (~9×, ≈64 MB/day)
- **category:** perf · **subsystem:** trading · **impact: 8** — dominant DB write volume on a disk/RAM-bound box; the same disease the July fixes (91a714d9 sampling, bc43d62d retention) treated on the read side, re-introduced on the write side. · **confidence:** high — measured; verifier independently proved that within one 12-row tick the `forecast`/`consensus`/`strategy_config`/`prediction_features` sub-payloads have DISTINCT count = 1, and that 91a714d9 is read-side-only. One correction: the finder's "450 MB steady-state" assumed CLI-default `--full-days 7`, but the deployed gated prune runs `--full-days 1` — steady state is smaller; **the ~64 MB/day write volume stands**. · **effort:** M
- **location:** `db.py:1040-1170` (`record_decisions`); payload builders `db.py:2800-3100`
- **evidence:** 6/20–6/26: ~800 B/row JSON, diagnostics_json NULL. 6/27 (first populated day): `AVG(LENGTH(diagnostics_json)) = 6,198 B` on every row, approved or rejected. 9,216 rows/day × ~7 KB ≈ 64 MB/day of WAL+table churn — vs ~7 MB/day before. The blob embeds the full forecast, market ladder, consensus, prediction features, and the entire StrategyConfig per row, all identical for the ~20 bin×side rows of one scan tick, while normalized refs (`forecast_snapshot_id`, `market_snapshot_id`) already exist on the row.
- **fix:** (1) new `scan_context_snapshots` table — one row per city-tick (created_at, target_date, risk_profile, forecast/intraday/market/consensus/prediction_features/strategy_config JSON) written once per `record_decisions` call; store `scan_context_id` on each decision row. (2) Shrink per-row `diagnostics_json` to the decision-specific signal payload (or drop it — `_decision_signal_payload` mostly duplicates existing columns; net-new fields like bid sizes/binding_constraint can become columns). (3) Same for `prediction_features_json` (identical per tick). (4) Update `_entry_decision_ref_payload`/`_order_entry_diagnostics_payload` (db.py:3183-3255) to join the context row; update archive.py export column lists; monitor snapshots inherit the fix (their `entry_diagnostics` re-embed goes through the same builders). Do it behind a `schema_version` bump in the payload.
- **verification:** post-change on the box: `SELECT SUM(LENGTH(diagnostics_json)+LENGTH(COALESCE(prediction_features_json,'')))/COUNT(DISTINCT substr(created_at,1,10)) FROM decision_snapshots WHERE created_at >= datetime('now','-2 days')` drops ~10–20×; daily `PRAGMA page_count` growth flattens.
- **risk:** archive/export and Strategy-Lab consumers read these blobs — they must resolve the ref; schema-version-gate it. · **conflicts_with:** TP-8 (same file — do TP-1 first), TB-7 (both touch archive.py).

---

#### TP-2 · Maintenance indexes + ANALYZE defined but never applied to the production DB
- **category:** perf · **subsystem:** trading/deploy · **impact: 6** · **confidence:** high for the pulled file (indexes absent; reproduced: 24 h aggregation `SCAN decision_snapshots` 0.379 s cold / 29 ms warm → `<1 ms` with `idx_decision_snapshots_created_market`; sampling query 0.138→0.042 s with the pre-entry index; **no sqlite_stat1 anywhere**). **CONDITIONAL for the live EC2 box** — the pull predates the migration; one command settles it (below). · **effort:** S
- **location:** consumers: `cities_report.py:131-141` (24 h aggregation, every 5-min publish), `db.py:2560-2591` (`sampled_decision_rows`, ≥2× per 15-min strategy cycle), `archive.py:203-256` (day export). Script: `trading/deploy/aws/create_decision_snapshot_index.sh` — **operator-manual only; wired into no installer** (verifier-confirmed). Deliberate init() exclusion at db.py:337-357.
- **evidence gap the fix closes:** `test_cities_report.py:199` asserts the covering-index plan against a fresh test DB; nothing checks the deployed DB.
- **fix:** (1) on the box: `sqlite3 /opt/weatheredge/trading/data/paper_trading.db "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='decision_snapshots'"` — if the two maintenance indexes are missing, run `bash trading/deploy/aws/create_decision_snapshot_index.sh` (timers paused per its README). (2) Add an init()-time `logger.warning` when `decision_snapshots` exists but `idx_decision_snapshots_created_market` is missing, so the omission is loud forever. (3) Extend the script's `ANALYZE decision_snapshots` to a whole-DB `ANALYZE`.
- **verification:** the sqlite_master query lists 3 indexes; re-run the EXPLAIN. Warm-vs-cold caveat: steady-state CPU saving is modest (29 ms warm) — the wins are cold-start, the growing approved set, archive day-exports, and planner statistics.
- **risk:** index build takes a write lock; the script already guards for that.

---

#### TP-3 · Walk-forward calibration backtest recomputed ~480×/day for once-a-day inputs
- **category:** perf (cross-cycle redundancy) · **subsystem:** trading · **impact: 6** — the largest single CPU item in both publication cycles, directly in the path of the 15-min strategy build that previously timed out (the known prune-bloat→stale-Strategy-Lab failure mode). · **confidence:** high — verifier independently reproduced 0.432 s/run at n=619, min_train=180; no caching exists; call sites + cadence verified (288 ops-cycle runs + 192 strategy-cycle runs/day ≈ 8–12 CPU-min/day, ~2–3× that on ARM). · **effort:** M
- **location:** `report.py:59-61` (`build_daily_report` → `calibration_diagnostics` → `run_walk_forward_calibration_backtest`, every 5-min cycle); `strategy_research.py:98-112` (×2 per 15-min build); engine `backtest.py:59+` (O(days × train × bins); re-slices `outcomes[:idx]` and rebuilds `ResidualCalibrator`/`_climatological_prior` per iteration)
- **fix:** (1) persistent cache keyed by `(city, source, len(outcomes), outcomes[-1].local_date, outcomes[-1].actual_high, min_train, config-fingerprint)` stored as JSON next to the artifacts (each CLI invocation is a fresh process); `calibration_diagnostics` returns the stored `CalibrationBacktestResult` dict on key match, with a `"cache_hit": true` field. (2) In-loop wins while there: make `_climatological_prior` an incremental counter; pass `train` as an index bound instead of slicing.
- **verification:** `time … daily-report` on the box before/after; second same-day run shows the calibration section served from cache.
- **risk:** stale cache after a truth backfill — the `actual_high` key component covers it; cache file is regenerable.

---

#### TP-4 · Strategy build re-reads the full decision journal into Python every 15 min
- **category:** perf · **subsystem:** trading · **impact: 4** · **confidence:** high — measured: `_decision_lead_mode_counts` 0.237 s (SELECT with no date bound → all 102 k rows into Python), `sampled_decision_rows` 0.142 s ×2 per build, `signal_backtest_summary` 0.198 s (+2 full-scan COUNTs); `adapter.load_cli_settlement_truth()` loaded **4×** per build (verifier count). · **effort:** S–M
- **location:** `strategy_research.py:2810-2840`, `strategy_research.py:977` + `db.py:2560`, `db.py:2634-2661`
- **fix:** (1) push `_decision_lead_mode_counts` into SQL (GROUP BY over a CASE) or at minimum bound it (`created_at >= datetime('now','-45 days')` — the prune horizon — so it stops scaling with the permanent approved set); (2) hoist one `sampled_decision_rows(...)` result and one `load_cli_settlement_truth()` in `build_strategy_research` and pass into the payload builders; (3) let `signal_backtest_summary` accept precomputed rows.
- **verification:** time the build's DB phase before/after; `diff <(jq -S .) <(jq -S .)` on strategy_research.json built from the same DB copy — byte-identical content.
- **risk:** low — diagnostics-only plumbing.

---

#### TP-5 · Per-target re-loading inside the 5-min scan loop
- **category:** perf · **subsystem:** trading · **impact: 3** — 15 cities × ≤3 targets × 2 profiles ≈ 90 repetitions of: `load_emos_mu_sigma(lead_days=None)` (unbounded read of the station's whole EMOS archive, then ONE key used), `load_posterior_kelly_model` (settled-journal re-read), `paper_entry_pause_reason` (2 aggregate queries). · **confidence:** medium-high (code-verified; weather.db not on the Mac to time). · **effort:** S
- **location:** `cli.py:1512-1515` (+ same at :1341), `cli.py:1529`, `cli.py:1578-1586`
- **fix:** hoist per city before the target loop in `cmd_portfolio_scan` (cli.py:1079): one `emos_lookup`, one `sizing_model`; memoize `pause_reason` per **(profile, target_date)** (verifier correction — it takes `target_date`). All three are trivially threadable through `_portfolio_scan_one_target`'s signature.
- **verification:** count weather.db opens per scan tick before/after (fs_usage/strace or a counter log line).
- **risk:** pause_reason staleness within a 5-min cycle — acceptable; document.

---

#### TP-6 · Monitor N+1 Kalshi calls per 2-min cycle
- **category:** perf (metered API) · **subsystem:** trading · **impact: 3** — fine at ~10 open orders; scales linearly; each call is a 20 s-timeout round trip on the cycle's critical path; duplicate open orders on one market fetch it twice. · **confidence:** high. · **effort:** S–M
- **location:** `cli.py:2768` (`get_market` per open order); `cli.py:2969-3040` (`get_trades(limit=1000)` per resting order, even when several share one market)
- **fix:** collect tickers once per monitor run; add a `get_markets(tickers=[...])` batch helper to kalshi.py (confirm the batch endpoint's response shape against current Kalshi docs first — flagged as an unproven lead); memoize `get_trades` per (ticker, min-created_at bucket) within the cycle. Keep the per-order fallback.
- **verification:** log outbound request count for one monitor run with >1 order on the same market, before/after.

---

#### TP-7 · `market_snapshots` has no index at all
- **category:** perf · **subsystem:** trading · **impact: 2** — retention-bounded (~4 k rows; measured 9 ms cold) but every `latest_market_snapshot` is a full scan + temp b-tree sort, called every 15 min. · **effort:** S
- **fix:** add `CREATE INDEX IF NOT EXISTS idx_market_snapshots_target ON market_snapshots (target_date, created_at)` to the INDEXES block (db.py:317).
- **verification:** EXPLAIN shows `SEARCH market_snapshots USING INDEX`.

---

#### TP-8 · `db.py` is a 3,711-line god module
- **category:** structure · **subsystem:** trading · **impact: 7** — every trading change routes through one file; pure risk-policy math (`account_policy_capacity`: sleeves, region caps, drawdown pause) and ~900 lines of diagnostics serialization interleave with locking-sensitive lifecycle SQL. · **confidence:** high (full read; verifier spot-checked the move plan's function locations and import stability). · **effort:** L
- **line map:** 1–60 imports + `_integer_settlement_high_f` (dup of forecast.py, admits circular-import workaround); 62–460 SCHEMA/INDEXES/audit dicts; 455–700 connect/init/migrations + shared account + `account_policy_capacity`; 700–2050 snapshot writers, order recording, entry guards, resting lifecycle, monitor snapshot; 2050–2300 settle/close/prune; 2300–2790 read-side analytics; 2790–3711 diagnostics payload builders + JSON/row helpers + profile-rename migration.
- **fix — new package `sfo_kalshi_quant/store/`, function-level mechanical moves, `db.py` stays the facade with re-exports so zero caller changes:**
  1. `store/schema.py`: SCHEMA, INDEXES, index DDL constants, audit-column dicts, `_migrate_legacy_profile_names`, init/guard-index bodies.
  2. `store/diagnostics.py`: all `_*_payload` builders + `_json_object/_json_list/_drop_none/_round_number/_json_safe_value/_optional_float`.
  3. `store/scoring.py`: `sampled_decision_rows` SQL, `signal_backtest_summary`, `market_backtest_summary`, sampling/row helpers, `_quality_buckets`, `_probability_stream_metrics`.
  4. Bin-resolution semantics → the TP-11 single home (settlement_truth.py), not store/.
  5. `account.py` (exists): `account_policy_capacity`'s policy math as a pure `policy_capacity(state, active_rows, …)`; PaperStore keeps the two SQL reads and delegates.
  - Order: diagnostics (pure) → scoring → schema → account-policy; run `test_shared_account.py test_paper_settlement.py test_trade_diagnostics.py` after each step.
- **verification:** `grep -rn "from .db import\|from sfo_kalshi_quant.db import"` — no caller changes; full `pytest trading/tests`.
- **risk:** low with re-exports; keep `_record_ledger_event` a PaperStore staticmethod (used inside transactions). · **depends_on:** TP-11 first (creates the shared-util and settlement homes); do after TP-1 (same file).

---

#### TP-9 · `cli.py`: 3,982 lines — argparse + print formatting + engine logic in command handlers
- **category:** structure · **subsystem:** trading · **impact: 6** · **confidence:** high. · **effort:** L
- **evidence:** `build_parser` spans :145–950 (805 lines); print/format helpers span :3386–3927 (~540 lines); the **queue-ahead maker fill model** (`_fill_resting_orders_against_live_book`, :2969–3041) and the monitor exit orchestration (`cmd_paper_monitor`, :2724–2969) — code that decides real fills and exits — live only inside CLI handlers; `_analyze_one_target`/`_portfolio_scan_one_target` (:1270–1656) are scan orchestration.
- **fix — target layout (cli/ package + top-level monitor.py):** `cli/parser.py` (per-domain `register_*` functions); `cli/scan.py` (analyze/portfolio-scan/tail-basket/arbitrage + target-resolution + sizing helpers); `cli/paper.py` (buy/close/settle/auto-settle/prune/archive/features); **`monitor.py` top-level module** (fill model + per-order inspect/close loop + veto helpers; `cmd_paper_monitor` becomes a thin wrapper — this most deserves module status and a direct unit test); `cli/backtest.py`; `cli/format.py`. Import stability: systemd calls `python -m sfo_kalshi_quant.cli` — keep `cli.py` as the module (moving code out beneath it) or make `cli/` a package re-exporting `main`; **verify `python -m sfo_kalshi_quant.cli --help` on the box before switching timers**.
- **order:** format.py (zero risk) → monitor.py (+ unit test around the moved fill loop) → scan.py → parser split last.
- **verification:** `--help` output byte-identical; `test_portfolio_cli.py test_limit_orders.py test_monitor_model_veto.py` green.
- **depends_on:** TB-1, TB-5 land first (same code regions).

---

#### TP-10 · `strategy_research.py`: 4,334 lines of eight separable artifact domains
- **category:** structure · **subsystem:** trading · **impact: 6** · **confidence:** high (function map verified). · **effort:** L
- **fix:** `sfo_kalshi_quant/strategy_lab/` package: `build.py` (orchestrator; current public API re-exported from a `strategy_research.py` shim), `profiles.py`, `calibration.py`, `readiness.py`, `paper_card.py`, `forecast_health.py`, `dataset_summary.py`, `status_alerts.py`, `consensus_offline.py`; the private JSON/rounding tail (:4276–4334) dies in favor of the TP-11 `_util.py`.
- **order:** forecast_health first (self-contained, ~640 lines), then paper_card, then the rest; `test_strategy_research.py` after each.
- **verification:** build strategy_research.json before/after on the same DB copy; `diff <(jq -S .) <(jq -S .)` byte-identical.
- **depends_on:** TP-11.

---

#### TP-11 · Duplicated core logic — settlement bin-resolution in 4 copies that ALREADY diverge
- **category:** structure/correctness · **subsystem:** trading · **impact: 6** (raised from 5: the verifier proved the four bin-resolution copies **already diverge on unknown-strike fallback** — between vs label-parse vs False — so this is latent correctness drift, not just hygiene; also found the duplication understated: **3** copies of `_integer_settlement_high_f`, **7** of `_table_exists`). · **confidence:** high. · **effort:** M
- **evidence:** (1) bin resolution ×4: `models.MarketBin.resolves_yes` (models.py:241, canonical), `db._row_resolves_yes` (db.py:3480), `db._decision_row_resolves_yes` (db.py:3560), `archive._resolves_yes` (archive.py:684). (2) `_integer_settlement_high_f` ×3. (3) json/time/row helpers re-implemented across db.py, strategy_research.py, summary.py, forecast.py, cities_report.py (7× `_table_exists`). (4) `_analyze_one_target` vs `_portfolio_scan_one_target`: ~270 duplicated lines of context-building out of ~390. (5) `db._is_pre_resolution_decision` vs `strategy_research._is_strategy_pre_resolution` — same rule, two homes.
- **fix:** (a) new `sfo_kalshi_quant/_util.py` (json/time/row coercion); (b) fold ALL bin-resolution into `settlement_truth.py` with row-shaped adapters, **canonical = `models.MarketBin.resolves_yes`**, and add regression tests for the divergent unknown-strike cases before unifying (each copy's behavior diff-reviewed case-by-case: less/greater/between + label fallback); (c) extract a `ScanContext` dataclass + `build_scan_context(...)` used by both scan functions — the ~112-line placement tails stay separate.
- **verification:** `pytest trading/tests`; `grep -rn "def _json_object\|def _table_exists" sfo_kalshi_quant | wc -l` → 1 each; new divergence-case tests green.
- **risk:** low-medium — the 4-way merge is where a mistake would change settlement semantics; the pre-merge regression tests are the guard. · **depended_on_by:** TP-8, TP-10.

---

#### TP-12 · Two pyproject.toml files both install `sfo-kalshi` under different project names
- **category:** structure/packaging · **subsystem:** packaging · **impact: 3** — `pip install .` at root (project `weatheredge`, `where=["trading"]`) and at trading/ (project `sfo-kalshi-quant`) produce two dists owning the same module path + console script; whichever installs second silently wins; the trading/ variant drops the box-safety extras. · **confidence:** high. · **effort:** S
- **fix:** keep the root pyproject as the only installable project; delete `trading/pyproject.toml` (its pytest config is already duplicated at root; deploy scripts run `python -m` from venvs — grep found no pip-install-from-trading/).
- **verification:** `pip install -e .` at root; `sfo-kalshi --help`; `pip list | grep -i -e weatheredge -e sfo` shows one dist. Run `pytest` from repo root to confirm testpaths still resolve.

---

#### TP-13 · Test coverage on critical trading logic: no material gap (positive finding)
- **category:** test-gap · **impact: 2** · settlement, sizing, maker fill/expiry, and the archive gate all have named, real test files (verified by grep, not coverage run). Two follow-ons: port the exit-loop assertions into a direct unit test when TP-9 extracts monitor.py; TP-2's init-time warning closes the "plan asserted only on fresh DBs" loophole.
### 5.4 Deploy & infrastructure (DP)

---

#### DP-1 · Ungated second `paper-prune` path bypasses the archive gate
- **category:** bug (data-loss ordering violation) · **subsystem:** deploy · **impact: 8** — the archive layer's single safety property ("prune may ONLY run after verified lossless export") has a second deletion path that ignores it. Verifier sharpened the scenario: on healthy days the ungated prune (7/45 retention) is a near-no-op because the gated prune (1/45) ran five minutes earlier — **its only marginal effect fires exactly when the archive gate is blocking**, i.e. the failure the gate was built for; from day 8 of a quietly-broken archive it permanently deletes unexported journal ticks. · **confidence:** high — unit + CLI + installer + git timeline all verified twice. · **effort:** S
- **location:** `trading/deploy/aws/systemd/sfo-dataset-backfill.service.in:12-13`
- **evidence:** `ExecStart=-…cli --no-color paper-prune` (bare, CLI defaults `--full-days 7 --dedup-days 45`); `cmd_paper_prune` (cli.py:3058-3063) calls `store.prune_decision_snapshots(...)` directly — **no archive-gate check exists in the CLI**; the gate is ordering-only, enforced solely inside `run_archive_then_prune.sh` (steps 4→5). Timeline: bare line added 7/06 (8c04880b); archive gate + dedicated gated prune unit added 7/10 (bc43d62d, 550e282d); the old path was never removed. Timing overlap in PDT: gated 09:20–09:25 UTC, backfill 09:25–09:27 UTC. Test gap: `test_paper_prune_unit_is_installed_and_archive_gated` asserts only the prune unit's wiring, not the absence of other invokers.
- **fix:** delete lines 12–13 from the unit template; re-render/install on the box (`bash trading/deploy/aws/install_systemd.sh` or hand-edit `/etc/systemd/system/sfo-dataset-backfill.service` + `daemon-reload`). Add to test_aws_deploy.py: the only unit text containing `paper-prune`/`run_archive_then_prune` is the prune unit. **Also resolve the retention split-brain the verifier flagged:** the gated path runs `--full-days ${SFO_PRUNE_FULL_DAYS:-1}` while the CLI default is 7 — pick one intent (document `SFO_PRUNE_FULL_DAYS=1` in the env example, or change the CLI default) so a future bare invocation is at least consistent.
- **verification:** `grep -n "paper-prune" trading/deploy/aws/systemd/*.service.in` → only the prune unit; on box `systemctl cat sfo-dataset-backfill.service | grep -c paper-prune` → 0.
- **risk:** low — retention fully owned by the gated unit since 550e282d.

---

#### DP-2 · (= DC-1) Two stale Lightsail-era `.service.in` duplicates tracked inside the Python package
See DC-1 in §5.6 — single work item: `git rm trading/sfo_kalshi_quant/sfo-dataset-backfill.service.in trading/sfo_kalshi_quant/sfo-forecaster-refresh.service.in`. Deploy-side evidence adds: the stray forecaster-refresh copy encodes the retired publish-inside-refresh architecture and 1 GB memory caps (3× too low for the EC2 box); installers render only from `deploy/aws/systemd/`; `pip install -e trading` would even ship the strays inside the package. **impact: 7 (merged)** · **effort:** S

---

#### DP-3 · Canonical deploy README instructs provisioning the wrong platform
- **category:** docs-stale (critical path) · **subsystem:** deploy · **impact: 6** — `trading/deploy/aws/README.md` is the runbook next to the scripts; following it in a disaster-recovery scenario re-provisions Lightsail 1 GB. Details and full corrected-claims list under DC-5 (merged fix); deploy-side extras: lines 241-243 tell the operator to set the dead var (DP-4); line ~238 contains a garbled self-contradicting sentence (VN-2). · **effort:** M (one rewrite, see Batch E)

---

#### DP-4 · `SFO_ENABLE_LIGHTSAIL_FORECASTER_REFRESH` is dead config
- **category:** dead-config · **impact: 3** · **confidence:** high — repo-wide grep: 2 hits, both documentation (`sfo-weather.env.example:7`, deploy README:243); zero code reads. · **fix:** delete both lines. · **verification:** `git grep -c SFO_ENABLE_LIGHTSAIL_FORECASTER_REFRESH` → 0. · **effort:** S

---

#### DP-5 · `pull_paper_db.sh` still targets the decommissioned Lightsail box only
- **category:** reliability (migration leftover in an active workflow) · **impact: 4** — the inbound half of the readiness-gate/backtest-rescore loop hard-requires `LIGHTSAIL_IP/KEY` (:15-26) unlike the already-migrated `deploy_web_app.sh` (`HOST_IP="${EC2_IP:-${LIGHTSAIL_IP:-}}"`, :28-29). Post-decommission it fails — or worse, during the window the old box still answers, silently pulls a stale DB and rescores against it. · **confidence:** high. · **effort:** S
- **fix:** copy deploy_web_app.sh's dual-name pattern: default env file `.local/ec2.env`, accept `EC2_IP/EC2_KEY` with `LIGHTSAIL_*` fallback; update the header comment and the `docs/win_trades_volatility_reliability_2026-06-18.md:61` reference.
- **verification:** `bash -n`; operator dry run: `set -a; source .local/ec2.env; set +a; bash trading/deploy/aws/pull_paper_db.sh` pulls from the EC2 IP.

---

#### DP-6 · `sync_to_lightsail.sh`: stale target AND exclude-list drift that can clobber live artifacts
- **category:** reliability/hygiene · **impact: 4** · **confidence:** high on the drift (line-diffed); medium on blast radius (manifest validation likely fails closed → transient publish outage rather than wrong data). · **effort:** S–M
- **evidence:** the full-deployment push script (a) knows only `LIGHTSAIL_IP/KEY` (:4-17); (b) its forecaster exclude list (:44-63) drifted from `sync_forecaster_source.sh` (:56, 63-69): it does NOT exclude `forecast_data.json`, `weather_story_data.json`, `STALE_FORECAST`, or `models/` — pushing would overwrite the box's live copies with the June-2 tracked fixture and could plant a stale STALE_FORECAST marker.
- **fix:** either retire the script (README points at sync_forecaster_source + deploy_web_app + installers) or rename to `sync_to_box.sh` with EC2/legacy dual-name handling AND a single shared exclude file sourced by both sync scripts (the DRY fix).
- **verification:** `diff` of the two scripts' exclude lists shows only intentional differences. · **conflicts_with:** DP-11 (fixture decision changes what may be clobbered — decide first).

---

#### DP-7 · `run_dataset_backfill.sh` exits 0 even when every source fails
- **category:** reliability (silent staleness) · **impact: 4** — per-source failures are collected and WARNed (:101-109) but the exit code stays 0 unless the final research step throws; a night where all 8 sources fail shows the unit green, and the freshness watchdog doesn't cover dataset tables. With no OnFailure anywhere (DP-9), unit state is the only signal. · **confidence:** high. · **effort:** S
- **fix:** after the research step: `if (( ${#failed_sources[@]} > 0 )); then exit 1; fi` (or exit 1 only when failed==total for a softer policy) — successful sources have already committed their data either way.
- **verification:** `SFO_DATASET_SOURCES=bogus bash trading/deploy/aws/run_dataset_backfill.sh; echo $?` → non-zero.

---

#### DP-8 · Cross-unit locks live in /tmp
- **category:** reliability · **impact: 3** — `SFO_PAGES_LOCK` (publish_forecaster_pages.sh:146) serializes the gh-pages push between TWO units; the scan lock (run_paper_scan_profiles.sh:9) also lives in /tmp. Works today (no `PrivateTmp=` in any unit — verified), but enabling PrivateTmp or a tmp-cleaner unlink silently voids cross-unit serialization (two lockers on different inodes both proceed). The artifact-generation lock already demonstrates the right home: `/opt/weatheredge/.locks/`. · **effort:** S
- **fix:** default both to `/opt/weatheredge/.locks/…` (mkdir -p like the artifact lock); document `SFO_PAGES_LOCK`/`SFO_PAPER_SCAN_LOCK` in the env example.
- **verification:** two concurrent manual scan runs → second prints the still-running skip with the new path.

---

#### DP-9 · No failure alerting on any unit; archive-gate failure is silent until the site breaks
- **category:** reliability · **impact: 5** — zero `OnFailure=` across all 9 units (grep-verified); the only alert path in the deploy layer is the freshness watchdog's `SFO_FRESHNESS_ALERT_URL` POST, which watches forecast/manifest age — not retention, not disk. The archive-then-prune chain is internally correct (verified: hard-fail export → independent `--check-gate` → prune; S3/features/cleanup non-fatal by design) but its failure artifact is just a red unit nobody is told about. Consequence chain already seen in production: prune blocked → decision_snapshots bloat → strategy-research timeout → stale Strategy Lab. Disk-fill has the same signature and **nothing measures disk**. · **confidence:** high. · **effort:** M
- **fix:** (1) templated `sfo-alert@.service` that POSTs `%i failed` to `SFO_FRESHNESS_ALERT_URL`; add `OnFailure=sfo-alert@%n.service` to every `.service.in` + installers. (2) Add a disk check to `check_forecast_db_freshness.sh` (fail + alert when `df /opt/weatheredge` >85 %) — it already runs every 30 min with alert plumbing.
- **verification:** `systemctl start sfo-alert@test.service` posts; force-fail a unit (`SFO_ARCHIVE_DIR=/nonexistent systemctl start sfo-kalshi-paper-prune.service`) → alert received.

---

#### DP-10 · 15+ env vars read by code but absent from `sfo-weather.env.example`
- **category:** config documentation gap · **impact: 3** — the example IS the file installed to `/etc/weatheredge.env` on a fresh box (install_systemd.sh:39-42). Most important omissions: **the five real-money gates** (`SFO_LIVE_TRADING_ENABLED`, `SFO_LIVE_TRADING_DRY_RUN`, `SFO_LIVE_PILOT_MAX_LOSS`, `SFO_LIVE_DAILY_LOSS`, `SFO_LIVE_PER_TRADE_RISK` — live_execution.py:24-28; defaults safe-off) and the archive/S3 family (`SFO_ARCHIVE_S3_BUCKET/PREFIX/AWS_CLI`, `SFO_PRUNE_FULL_DAYS`, `SFO_PRUNE_DEDUP_DAYS`, `SFO_ARCHIVE_KEEP_DAYS`, `SFO_ARCHIVE_DIR`); plus `SFO_WEBDIST_DIR`, `SFO_PAGES_LOCK`, `SFO_PAPER_SCAN_LOCK`, `SFO_PAGES_PUSH_ATTEMPTS`, `PAPER_ROLLING_TARGETS`, `PAPER_SAME_DAY_ENTRY_CUTOFF_HOUR`, `SFO_DISABLE_CLISFO`, `SFO_DATASET_RESEARCH_PATH`, `SFO_FORECAST_DB`, `SFO_FORECAST_STALE_MARKER`, `SFO_TRADING_ROOT/PYTHON`. No unsafe defaults found. · **fix:** append a commented "advanced / archive / live-gates" section. · **effort:** S

---

#### DP-11 · Tracked runtime-artifact fixtures are internally inconsistent
- **category:** hygiene · **impact: 2** — `forecaster/forecast_data.json` + `weather_story_data.json` + 4 `public/*.json` are tracked while their siblings are gitignored; a dev forecaster run dirties the tree; the stale tracked copies are what DP-6 would push. They are deliberate fixtures (health check requires them; single-commit history). · **fix (pick one):** (a) ignore the two forecaster files like their siblings and teach `weatheredge_health_check.py` to accept placeholders (concept exists at :85-89); or (b) keep as fixtures with a .gitignore comment block + `git update-index --skip-worktree` guidance. (a) is cleaner. Decide BEFORE DP-6/FC-10. · **effort:** S

---

#### DP-12 · Mixed-timezone timers + fallible timezone setup
- **category:** reliability · **impact: 2** — 8 of 9 timers use LOCAL OnCalendar; the box TZ is set by `timedatectl set-timezone America/Los_Angeles || true` (install_systemd.sh:21) — silently tolerated failure would shift the forecaster's 05..18 daytime window overnight and break Google budget shaping. Only the prune timer pins UTC (which is why DP-1's collision is DST-dependent). · **fix:** drop `|| true` (fail loudly), or pin all timers UTC with an LA-hours comment map. · **effort:** S

---

#### DP-13 · `${1,,}` (bash-4-only) in a script that claims macOS support
- **category:** portability bug · **impact: 2** — run_paper_scan_profiles.sh:33 `truthy()` uses `${1,,}`; macOS system bash 3.2 → "bad substitution". The repo already bans the idiom elsewhere (test_aws_deploy.py:242-243 enforces the `tr` idiom for run_dataset_backfill.sh). · **fix:** same `tr '[:upper:]' '[:lower:]'` helper; extend the existing test to this script. · **effort:** S

---

#### DP-14 · `deploy_web_app.sh` nits
- **category:** hygiene · **impact: 2** — `USER="${REMOTE_USER:-ubuntu}"` clobbers the standard env var (:32); `SSH="ssh -i $HOST_KEY …"` used unquoted (breaks on spacey paths, :34+); cwd-assumption for `.local/ec2.env` and `bun run build` (:16, 37). · **fix:** rename to REMOTE_USER_NAME; use an `SSH_OPTS=(…)` array; `cd "$(dirname "${BASH_SOURCE[0]}")/../../.."` at top. · **effort:** S
### 5.5 SPA (SP)

Toolchain baseline verified: `tsc -p tsconfig.app.json --noEmit` clean, `oxlint` 0/0 (85 files), `vitest` 37/37, all three views lazy-loaded, `dist/` ignored. Bundle numbers measured from a real production build (verifier rebuilt independently: byte-identical chunk hashes and gzip sizes).

---

#### SP-3 · Missing-field access hard-crashes the app; zero error boundaries
- **category:** bug · **subsystem:** spa · **impact: 8** — violates the repo's own AGENTS.md rule ("keep `src/lib/data.ts`/`strategy.ts` parsing tolerant of missing fields"). One malformed/partial published JSON unmounts the entire React tree → blank site — the exact publisher-hiccup class the manifest/banner system was built to survive. `grep -rn "ErrorBoundary|componentDidCatch|getDerivedStateFromError" src` → zero hits. Verifier precision: the three `data.ts` series helpers crash the **Methodology** route (MethodologyView.tsx:142-149); the Overview vector is `SkillStrip`; with no boundary anywhere, either blanks the whole app. · **confidence:** high (twice-verified). · **effort:** S
- **location / evidence:** `src/components/kpi/SkillStrip.tsx:17-25` (`const c = signal.calibration;` then `c.brier_skill`, `forecast.n_days_observed.toLocaleString()` — unguarded; renders on Overview at OverviewView.tsx:60); `src/lib/data.ts` `calibrationSeries` (`signal.calibration.buckets.map`), `histogramSeries` (`story.temperature_histogram` destructure), `climatologySeries` (`Object.keys(forecast.table)`); `StrategyLabView.tsx` `SelectivityFinding` (`live?.signals.toLocaleString()`; `gate.approved + gate.rejected` → NaN). Contrast: `strategy.ts` and `publication.tsx` are exemplary — the discipline exists; these paths predate it.
- **root_cause:** the TS interfaces declare the fields required, satisfying tsc, but the runtime contract is owned by a separate Python pipeline.
- **fix:** (1) guard the helpers (`signal.calibration?.buckets ?? []`; `story.temperature_histogram ?? {labels:[],counts:[]}`; `forecast.table ?? {}`; SkillStrip: `if (!c) return null;`, `(forecast.n_days_observed ?? 0)`); (2) one ~30-line class ErrorBoundary rendering the existing `ErrorState`, wrapped around each `<Suspense>` view block in App.tsx so a view crash degrades to that view; (3) `live?.signals?.toLocaleString() ?? "—"` and guard the gate sum.
- **verification:** vitest cases feeding `calibrationSeries`/`histogramSeries`/SkillStrip a signal without `calibration` (assert fallback render, not throw); `bun run test`.
- **risk:** minimal — additive guards.

---

#### SP-2 · Landing route ≈477 KB gzipped JS vs 300 KB budget; CSS 78 KB vs 30 KB
- **category:** perf · **subsystem:** spa · **impact: 7** — the owner's own budgets (app <300 KB gz JS; CSS <50 KB app/<30 KB landing) exceeded ~1.6× JS / ~1.6–2.6× CSS on a public dashboard. web.dev's general anchor is ~170–250 KB. · **confidence:** high — measured twice (identical results): entry 238,204 gz + OverviewView 29,665 + Stat/recharts 109,824 + AnimatedNumber/number-flow+Pro-slab 98,980 + bar-chart 430 ≈ 477 KB on the default `#/` route; CSS 78,430 gz (776 KB raw). Verifier precision: recharts rides the lazy OverviewView chunk (via its static chart imports), not the entry chunk — same landing payload. Good news confirmed: the unused HeroUI peer deps (tiptap/shiki/embla/…) are fully tree-shaken — zero fingerprints in any output chunk. · **effort:** M
- **location:** `src/index.css:1-4` (`@import "tailwindcss"; @import "@heroui/styles"; @import "@heroui-pro/react/css";` — the entire Pro BEM surface incl. components never rendered); `src/App.tsx` chunking; Overview's static chart imports.
- **fix (descending value):** (1) take recharts off the landing path: `React.lazy` the below-the-fold chart components inside OverviewView (`const ClimatologyChart = lazy(() => import("../charts/ClimatologyChart"))` etc. under `<Suspense>` skeletons) — −110 KB gz. (2) CSS: if `@heroui-pro/react` exposes per-component CSS entrypoints, import only the used components (Navbar, Command, Chip, Card, Table, Skeleton, Toast, Tooltip…) targeting ≤40 KB gz; if only the monolith exists (unproven lead — enumerate the package's exports first), a PurgeCSS-style pass is the riskier fallback. (3) `modulepreload` hint for the default route chunk to remove the dynamic-import waterfall hop.
- **verification:** rebuild to /tmp, re-run the gzip loop; landing set ≤300 KB gz, CSS ≤40 KB gz; `bun run test`; screenshot 320/768/1440 in both themes after the CSS change (per the repo's testing rules).
- **risk:** medium for the CSS split (missing-component CSS regressions); low for chart lazying.

---

#### SP-1 · `heroui-native` (React Native library) shipped as a web dependency; stale `trustedDependencies`
- **category:** dead-dep · **impact: 4** — never imported (0 hits), not a peer of `@heroui-pro/react` (peer list inspected), no dependents in bun.lock; drags an RN peer tree into every install and grants postinstall trust (`trustedDependencies`) to `"heroui-pro"` and `"heroui-native-pro"` — packages that aren't even installed. · **confidence:** high (3-way verified, twice). · **effort:** S
- **fix:** delete `"heroui-native": "^1.0.4"` from dependencies; prune `trustedDependencies` to `["@heroui-pro/react"]`; `bun install`.
- **verification:** `bun install && bun run test && vite build --outDir /tmp/…` — build output byte-identical.

---

#### SP-4 · All icons fetched from api.iconify.design at runtime
- **category:** perf/robustness · **impact: 5** — 37 files import `@iconify/react` with no `addCollection` (grep: zero); the built entry chunk contains the literal `api.iconify` endpoint. Offline/ad-blocked/API-down → icon-less UI + pop-in; a third-party runtime dependency on a codebase whose own rules say self-host critical deps. `public/icons.svg` (5 KB) is an abandoned attempt at exactly this fix, referenced nowhere but shipped to dist. · **confidence:** high (twice-verified). · **effort:** M
- **fix:** generate a local bundle of the ~40 used glyphs (`@iconify/json` + a small script, or `iconify-icon` offline mode); `addCollection(...)` once in `main.tsx`; delete `public/icons.svg` (or wire it as the sprite instead).
- **verification:** `vite preview` with network blocked: icons render; zero requests to api.iconify.design.

---

#### SP-5 · (= DC-2) `Learnings.tsx` dead code — see DC-2. Single work item; owner decision (delete vs re-mount).

---

#### SP-6 · Dead assets
- **category:** dead-code · **impact: 1** — `src/assets/hero.png` (13 KB), `react.svg`, `vite.svg` (Vite template leftovers): zero references; `public/icons.svg` per SP-4. · **fix:** delete four files (SP-4 decides icons.svg). · **verification:** build green; no icons.svg in dist. · **effort:** S

---

#### SP-7 · Dark-theme flash for light-mode users
- **category:** bug (cosmetic) · **impact: 2** — `index.html:2` hardcodes `class="dark"`; `src/lib/theme.ts` applies the stored mode only in a post-mount `useEffect`; a returning light-mode user gets a dark first paint that flips — and the flash window sits on a ~477 KB landing path. · **fix:** 3-line inline script in `<head>`: `try{if(localStorage.getItem("weatheredge-theme")==="light")document.documentElement.classList.remove("dark")}catch{}`. · **verification:** light theme + hard reload on throttled network — no flash. · **effort:** S

---

#### SP-8 · Fonts: 3 families / 10 weights, render-blocking, third-party
- **category:** perf · **impact: 3** — Inter(4)+Space Grotesk(3)+JetBrains Mono(3) via a render-blocking Google Fonts stylesheet (index.html:12-16; `display=swap` present); repo rules: max two families, preload only the critical weight, prefer self-hosting. · **fix:** self-host two families as subset woff2 with `<link rel="preload" as="font">` for the headline weight; system-stack the mono (used only for small ticker/kbd chrome). · **verification:** Lighthouse render-blocking audit; visual diff 320/1440. · **effort:** M

---

#### SP-9 · Whole app re-renders every 60 s from the publication poll clock
- **category:** perf · **impact: 3** — `PublicationProvider` calls `setNow(Date.now())` every poll tick; `now` is a dep of the context `useMemo`, so a new context value publishes every 60 s; `useDashboardData` (consumed in App) → the entire mounted tree re-renders every minute idle. · **confidence:** high mechanism (code-read; not profiled). · **fix:** split the tick out of the identity context — compute `ageMinutes` inside the two banner components from `generatedAt` with their own interval (or a second TickContext) so the primary context changes only when the manifest changes. Careful: `publication.test.tsx:87` pins that freshness advances — keep it green. · **verification:** React DevTools Profiler: idle app, no commits outside banners across two poll intervals. · **effort:** M

---

#### SP-10 · 26 `>=` version ranges on production deps
- **category:** reproducibility · **impact: 3** — `recharts: ">=2.0.0"` already resolves to 3.9.0 (an unpromised major); the ranges mirror the HeroUI Pro beta's peer declarations verbatim. bun.lock pins everything today, but any `bun update`/lockfile regen/non-frozen CI install may leap majors silently. · **fix:** bounded ranges that still satisfy the peers (`"recharts": "^3.9.0"`, `"shiki": "^4.3.0"`, `"@tiptap/core": "^3.27.1"`, …); use `bun install --frozen-lockfile` in any CI/publish path. · **verification:** `bun install` → lockfile versions unchanged; test + build. · **effort:** S

---

#### SP-11 · `data.ts`/`strategy.ts` business rules largely untested
- **category:** test-gap · **impact: 4** — 37 tests are well-aimed but narrow: `strategy.ts` (514 lines) has 3 tests (equity series only); `data.ts` tests cover 2 functions. Untested pure functions that encode business rules: `targetLabel` (the SFO settlement-day Today/Tomorrow anchor — exactly the tz logic that regresses silently), `cityForTicker` longest-prefix matching over 15 overlapping tickers, `cityFreshness` thresholds, the three series helpers (SP-3's guard targets), `ledgerByCity`, `profileGate`, `closedLedger` null-`closed_at` sort, `money`/`cents` sign rendering. · **fix:** table-driven `data.more.test.ts` + `strategy.more.test.ts`; fake timers + TZ pinning for `targetLabel` (assert "Today" flips at America/Los_Angeles midnight, not UTC); fold in SP-3's missing-field cases. · **verification:** `bun run test`; optional `vitest run --coverage` on the two lib files. · **effort:** M

---

#### SP-12 · Two `money` formatters with different sign semantics
- **category:** structure · **impact: 2** — `CityDetail.tsx:34` local `money` renders `$1.23` (no `+`); `lib/strategy.ts` `money` renders `+$1.23`. Same value type displays differently between the city Book panel and the Strategy Lab. · **fix:** delete the local copy; import from `lib/strategy` (explicit `+` version). · **verification:** `tsc --noEmit`; visual check of the Book panel (settled P&L gains a `+` — intended). · **effort:** S

---

#### SP-13 · index.html meta is SFO-era; no social meta
- **category:** content · **impact: 2** — title says "Fifteen-City…" but `meta description` still reads "A station-aligned SFO daily-high forecaster…"; no `og:*`/`twitter:*` on a publicly shared dashboard. · **fix:** rewrite description around fifteen cities + SFO flagship; add og:title/description/type/url + a static og:image screenshot in `public/`. · **verification:** view-source of built index.html; a link-preview checker. · **effort:** S
### 5.6 Dead code, artifacts & documentation staleness (DC)

Method note: every dead claim was swept across all dynamic-dispatch surfaces (python imports incl. relative; cli.py `add_parser`/`set_defaults` registry; systemd `.in` units incl. the strays; every shell script; both pyproject `[project.scripts]`; importlib/getattr; tests; SPA lazy imports; index.html) — and the sweep's headline is negative: **zero orphaned Python modules out of 69** (11 live via documented offline/manual entry points), zero TODO/FIXME debt, no commented-out blocks. The dead mass of this repo is documentation.

---

#### DC-1 (= DP-2) · Two stale Lightsail-era systemd unit templates tracked inside the Python package
- **category:** duplicate/stale · **impact: 7** — the two files most likely to mislead: they describe the retired 1 GB no-swap box and the retired publish-inside-refresh architecture; the stray backfill copy lacks the paper-prune line and TimeoutStartSec and carries `MemoryHigh=280M/MemoryMax=360M` (vs canonical 600/800M); an operator or agent grepping "the backfill unit" can install/reason from the wrong copy — duplicate-config drift is exactly how the DP-1 class of bug happens again. · **confidence:** high — diffed by three agents; installers render exclusively from `deploy/aws/systemd/`; zero references anywhere; strays last touched c6c5d6ca (7/06), canonical updated since. · **effort:** S
- **location:** `trading/sfo_kalshi_quant/sfo-dataset-backfill.service.in`, `trading/sfo_kalshi_quant/sfo-forecaster-refresh.service.in` (both git-tracked)
- **fix:** `git rm` both. · **verification:** `git ls-files | grep -c "sfo_kalshi_quant/.*service.in"` → 0; `pytest trading/tests/test_aws_deploy.py` green (reads only deploy/aws/systemd/).

---

#### DC-2 (= SP-5) · Orphaned SPA component `Learnings.tsx`
- **category:** orphan · **impact: 4** — zero importers (the only `Learnings` occurrence in src/ is its own definition; `ProfileDashboard.tsx:187` renders `p.learnings` data itself, not the component). Orphaned by the Strategy Lab overhaul (`git log -S`: import removed in 91e26e62) — yet its copy was polished in b05255ee on 7/10, i.e. someone believed it still shipped. · **effort:** S
- **fix (owner decision):** delete the file, or re-mount the "what this window showed" card in `StrategyLabView.tsx` if it was dropped accidentally. Keep the `learnings` data field either way (ProfileDashboard uses it).
- **verification:** grep + `bun run build` + visual pass of the Lab.

---

#### DC-3 · `forecaster/model_compare_results.json` — tracked artifact with no code consumer
- **category:** artifact · **impact: 2** — zero code consumers (writer: compare_models.py:256). Nuance: the SPA Methodology page renders `public/diagnostics.json`, which appears to be a hand-derived digest of this file + ab_test_results.json — so it is provenance for a manual step, not pure dead weight. · **fix:** keep, but document the chain in forecaster/README's File Map ("public/diagnostics.json is regenerated by hand from model_compare_results.json + ab_test_results.json after retraining"); or gitignore it and script the digest. **Do not silently delete.** · **effort:** S

---

#### DC-4 · `docs/aws_lightsail.md` — production is EC2 since 2026-07-10
- **category:** docs-stale · **impact: 8** — the primary infra doc, linked from README/architecture/data_and_artifacts, still gives `LIGHTSAIL_IP`/`.local/lightsail.env` env instructions, Lightsail-console recovery steps, and 1 GB swap advice for a host being decommissioned. Timer-cadence content verified still correct. · **effort:** M (45 min)
- **fix:** retitle "AWS Deployment (EC2)" (rename to `docs/aws_deployment.md` and update the 3 inbound links, or keep the path); swap host sections (t4g.medium, us-east-1, SG, ubuntu/arm64, same /opt/weatheredge layout, `.local/ec2.env`, `EC2_IP/EC2_KEY`); add "history: ran on Lightsail 1 GB until 2026-07-10".
- **verification:** `git grep -in "lightsail" docs/ | grep -v historical` → only intentional history notes.

---

#### DC-5 · `trading/deploy/aws/README.md` — "Use Amazon Lightsail, not full EC2" + factually wrong "committed models/" claim
- **category:** docs-stale · **impact: 8** — the provisioning runbook recommends the opposite of production (line 47; "1 GB bundle" line 52; "will OOM on the 1 GB box" line 240) and one claim is wrong on any date: line 233 says the box serves "the committed `models/` artifacts" — **`forecaster/models/` is gitignored with zero tracked files**; what is committed and served is `ab_test_results.json`. Also: `install_systemd_notimers.sh` exists but is documented nowhere; **VN-2:** a garbled self-contradicting sentence at ~:238 ("NEVER run `pip install .[forecaster]` is now safe"); references the dead var (DP-4). · **effort:** M (1 h)
- **fix:** retitle + rewrite provisioning for EC2; correct the models/ sentence; document the notimers installer; fix VN-2; drop the dead-var instruction.
- **verification:** `git grep -il "use amazon lightsail" trading/deploy` → empty; `git ls-files forecaster/models | wc -l` → 0 stays true in the text.

---

#### DC-6 · Root `README.md` — two concrete stale claims
- **category:** docs-stale · **impact: 6** — front door: (1) "Production serves the prebuilt app from /opt/weatheredge/webdist on the **Lightsail box**" → EC2; (2) "`sfo-strategy-lab-refresh.timer` republishes … **every five minutes**" → `OnUnitActiveSec=15min` (five minutes is the operational-publish cadence). Everything else spot-checked current. · **fix:** two-line edit + point the sync section at the renamed infra doc. · **effort:** S

---

#### DC-7 · `trading/README.md` (514 lines, frozen 2026-06-19) — pre-multicity, pre-maker-first
- **category:** docs-stale · **impact: 7** — the largest module README still frames a single-market SFO/KXHIGHTSFO engine (zero occurrences of "fifteen"/"multi-city"/any other city slug): presents maker-limit entry as an optional simulation when `PAPER_ENTRY_MODE=limit` has been the production default since 7/06 (sfo-weather.env.example:70); "research: the single data collector" predates the shared-account two-profile P&L-attribution model; settlement wording is SFO-specific. · **fix:** reframe intro/profiles/entry-mode for the 15-city maker reality with SFO as the flagship walkthrough — or the 10-minute fallback: a dated banner "Examples below are SFO-flavored; the engine is multi-city — see docs/architecture.md." · **effort:** M–L (or S for the banner)

---

#### DC-8 · `forecaster/README.md` — 3× Lightsail; File Map omits the entire production path
- **category:** docs-stale · **impact: 4** — otherwise current (7/09), but the File Map documents only the legacy SFO stack: missing `cities.py`, `city_truth.py`, `clisfo.py`, `emos_forecast.py`, `emos_recalibration.py`, `recalibration_replay.py`, `nwp_archive.py`, `postproc_models.py`, both backtest modules, `settlement_calendar.py`, `features.py`, `fetch_inland_history.py`, `forecast_validation.py` — the map that exists to explain the directory omits everything production runs. · **fix:** s/Lightsail/EC2 ×3; extend the File Map one line per module (also add the DC-3 provenance line). · **verification:** File Map entries == `ls forecaster/*.py`. · **effort:** S–M

---

#### DC-9 · `docs/lightsail_dataset_plan.md` — obsolete plan presented as current
- **category:** docs-stale (OBSOLETE) · **impact: 5** — prescriptive plan whose subject shipped (datasets.py + nightly unit) and whose host is retired. · **fix:** prepend `> Historical planning document (2026-06-11). The dataset layer described here shipped as sfo_kalshi_quant/datasets.py + the nightly sfo-dataset-backfill unit; the host is now EC2. Kept as design record.` · **effort:** S

---

#### DC-10 · `docs/codebase_audit_2026-06-15.md` — needs a historical marker
- **category:** docs-stale · **impact: 6** — 106 findings dated 25 days and two architecture overhauls ago; its live-production snapshot and many file:line cites (e.g. strategy-lab.html) no longer exist. This audit re-verified its CRITICAL/HIGH items (see §4: all fixed except the residuals that became TB-4/TB-6). · **fix:** prepend `> Point-in-time audit (2026-06-15). Line numbers and the production snapshot predate the July multi-city/maker-first/EC2 overhauls; see docs/AUDIT-PLAN.md (2026-07-10) for current status.` · **effort:** S

---

#### DC-11 · `trading/docs/user_guide.md` + `strategy.md` + `research_yes_no_strategy.md` — single-market framing, undated
- **category:** docs-stale · **impact: 4** — the beginner entry point (391 lines, 6/11) frames the whole system as the SFO market; the two research notes carry no date/status. Command walkthroughs still work. · **fix:** short "since this was written" preface on user_guide (15 cities, two profiles, maker-first); dated "research record" headers on the other two (+ same for `docs/research_improvement_review.md` and `docs/trade_engine_parameter_audit.md`, whose parameters have been retuned twice since). · **effort:** S

---

#### DC-12 · `docs/data_and_artifacts.md` — Lightsail refs + the archive layer missing
- **category:** docs-stale · **impact: 5** — the data-inventory doc omits the newest safety-critical data path: the 7/10 archive-gated retention layer (lossless day export + manifest + prune gate + `market_side_day` features). Its "five required JSONs" list is verified correct. · **fix:** add an "Archive layer" subsection (export dir, manifest, gate semantics, S3 env gating) + update the two Lightsail sentences. · **verification:** cross-check flags against run_archive_then_prune.sh. · **effort:** S

---

#### DC-13 · `docs/prompts/` is untracked
- **category:** artifact · **impact: 3** — the repo's only untracked path; contains two working prompts (no secrets — checked). · **fix (owner decision):** `git add docs/prompts/` (recommended) or gitignore the dir. · **effort:** S

---

#### DC-14 · Root and trading `.env.example` missing the July control knobs
- **category:** dead-config/stale example · **impact: 4** — root example (6/19) has only `PAPER_TAKE_PROFIT_PCT/STOP_LOSS_PCT`; production example additionally documents `PAPER_ENTRY_MODE`, `PAPER_CITIES`, per-side exits, `PAPER_MODEL_VETO_*`, `PAPER_RISK_PROFILES`, DB paths. `trading/.env.example` is a third, thinner variant. A fresh local setup gets pre-July behavior. · **fix:** sync both to the sfo-weather.env.example var set, or replace with a pointer to it. Note `KALSHI_ENV=prod` default is a mild footgun (inert while key id is blank) — comment it. · **effort:** S

---

#### DC-15 · Minor dead config
- **impact: 2** — (1) `.semgrepignore` lists `forecaster/index.html` (retired artifact, untracked); (2) root `.gitignore`'s "Dashboard runtime cache" block ignores the three retired HTML files — prune once DC-16 deletes the local leftovers; (3) `.venv-dev/` is invisible only because the venv writes its own internal gitignore — add it to root `.gitignore`; (4) tsconfig/vite/vitest/oxlint configs checked: no unused keys. · **fix:** three one-line edits. · **effort:** S

---

#### DC-16 · Untracked working-tree leftovers of retired pipelines (dev-Mac hygiene, not git)
- **impact: 3** — `forecaster/{index,details,strategy-lab}.html`, `forecaster/strategy_research.protected.json`, empty `trading/weatheredge.egg-info/`, assorted `__pycache__`/`.pytest_cache` — clutter that keeps the retired dashboard "present" in every recursive grep. The 110 MB raw-data dir and `models/` are untracked-by-design — keep. · **fix:** local `rm` on the Mac (operator action, NOT a commit). · **effort:** S

---

#### DC-17 · Repo/git size posture (informational)
- **impact: 2** — tracked payload is lean; biggest tracked artifact `ab_test_results.json` 2.3 MB is a live input (slim per FC-7 if desired); pack 78 MB across 16 packs (`git gc` would consolidate); `public/*.json` fixtures are June-28 vintage (dev-mode realism lead in SP notes). No action required.

---

#### DC-18 · TODO/commented-code inventory: clean (positive finding)
- **impact: 2** — `grep -rn "TODO|FIXME|XXX|HACK"` across all source → only mktemp templates; no `if False:`; no commented-out bodies. The codebase carries no marker debt.

---

### 5.7 Verifier-new items (found during adversarial verification)

- **VN-1** · `pyproject.toml` `[train]` extra comment still says "must NEVER be installed on the **1 GB box**" — stale sizing reference; reword to "the production box" during Batch E. (docs-stale, impact 1, S)
- **VN-2** · Garbled sentence in `trading/deploy/aws/README.md` ~:238 — folded into DC-5.
- **VN-3** · Bin-resolution fallback divergence proven — folded into TP-11 (raised its impact).
- **VN-4** · Retention split-brain (gated `--full-days 1` vs CLI default 7) — folded into DP-1's fix.

---

## 6. Appendix — unproven leads (worth follow-up; NOT findings; most need box access)

Labelled unproven per the audit's evidence bar. Each names the one step that would settle it.

1. **Live EC2 DB state** (TP-2's conditional half): `sqlite3 /opt/weatheredge/trading/data/paper_trading.db "SELECT name FROM sqlite_master WHERE type='index'; SELECT COUNT(*), MAX(created_at) FROM decision_snapshots;"` — also re-anchors TP-1's write-rate on current data.
2. **Strategy build end-to-end runtime on the box**: `time bash trading/deploy/aws/build_strategy_research.sh`; if >60 s, TP-3/TP-4 are the first knobs.
3. **`.local/ec2-west1.env`** (modified the migration day) alongside `ec2.env` — a second box or region move not reflected in CLAUDE.md? If real, `deploy_web_app.sh`'s default env file may target the wrong host. Contents not read (credential policy) — operator should reconcile.
4. **Silent EMOS staleness via `-` ExecStart lines**: whether the 6adb23ba station-keyed health checks actually alarm when `--serve-rolling` fails persistently for non-SFO cities (weather.db mtime stays fresh from other writers). Trace `check_forecast_db_freshness.sh` + health-check coverage end-to-end on the box.
5. **Deploy-key blast radius**: the same `SFO_PAGES_DEPLOY_KEY` fetches main and pushes gh-pages, and the README says "Allow write access" — a compromised box could push to main. Split fetch/push keys or use a fine-grained deploy key.
6. **Kalshi fee-rounding unit**: fees.py rounds position+fee up to a centicent per the "July 7, 2026" schedule when `series_ticker` is passed; if Kalshi's schedule actually rounds to the next cent per order, paper fees are understated ≤$0.0099/order. Check the live fee schedule doc.
7. **`get_trades` field contract** (`taker_book_side`, `count_fp`, `*_price_dollars`): only self-authored fixtures assert these names; an API rename would silently zero all maker fills. Cheap invariant: alert when N consecutive monitor passes see resting orders but zero qualifying trades across all cities.
8. **Evening-preliminary frequency** (TB-2/FC-1 severity): quantify from IEM how often a city's final CLI max exceeds the ~5 PM preliminary; sets the empirical priority of Batch B.
9. **Frozen-evening right-tail pricing**: after FC-1's freeze begins, how the stale sigma/right tail prices YES buys on not-yet-reached buckets in the last hours (probability.py:176-228 with a frozen-mu scenario).
10. **Kalshi `GET /markets?tickers=` batch endpoint** response shape (TP-6's prerequisite).
11. **HeroUI Pro per-component CSS entrypoints** (SP-2 fix 2's prerequisite): enumerate the package's exports; if only the monolith exists, fall back to a purge pass.
12. **Entry-chunk anatomy (238 KB gz)**: `--sourcemap` build + source-map-explorer; if the Command palette/Toast/Navbar slab dominates, lazy-mount CommandPalette on first ⌘K.
13. **`strategy_research.json` production payload size** post-prune (local fixture is 580 KB raw — Lab route parse cost).
14. **`usage.json` legacy keys** (`limit`, `calls`) maintained forever in google_weather_cache — check nothing reads them, then drop.
15. **WAL growth during long strategy builds** (readers holding snapshots while scan/monitor write): `ls -la *.db-wal` on the box during a strategy cycle.
16. **Kalshi-by-data passthrough**: the no-"Kalshi" site rule is enforced only by backend discipline for free-form rendered strings; a one-line render-boundary scrub would make the SPA self-defending (today's fixtures are clean).
17. **`public/*.json` dev fixtures** are June-28 and lack `publication_manifest.json`/`cities_data.json`, so local `vite dev` always shows a banner error and an empty city grid — refresh if dev-mode realism matters.
18. **"Paper engine live" pulsing green dot** (StrategyLabView LiveStatusStrip) renders unconditionally even when the strategy artifact is hours old — tone by freshness state.
19. **`paper_account_ledger` unbounded growth** (~3 rows/order, SUMmed on every capacity check) — indexed and tiny for years; a yearly rollup caps it.
20. **IEM preliminary semantics**: whether IEM's JSON distinguishes preliminary vs final rows (would simplify FC-1 fix 2's classification for the 14 non-SFO cities).

---

*End of audit plan. 77 findings; every one independently re-verified against the code at HEAD `bb862f9e` before inclusion. The working evidence trail (per-agent reports, verifier verdicts, module-verdict and docs-verdict tables) lives in the audit session's scratch directory and can be regenerated from the locations cited here.*
