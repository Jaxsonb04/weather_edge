# Repository map and source authority

This map is the navigation index for the v7 audit. File locations describe
current source, while operational claims require a fresh AWS/public verification.
The October 5, 2026 baseline had fifteen deployed markets on backend `2a6432e3`;
current main-derived source registers twenty. A local ignored database or a
new branch's files cannot substitute for installed runtime provenance.

## Ownership and canonical entrypoints

| Area | Canonical owner/entrypoint | Contract |
|---|---|---|
| Python packaging | root `pyproject.toml` | Sole install manifest; owns `sfo-kalshi` and `sfo_kalshi_quant` |
| Forecast runtime | `forecaster/emos_forecast.py` | Per-station EMOS with coupled mean/spread and target lead |
| NWP history | `forecaster/nwp_archive.py` | Fixed-lead reconstructed archive; operational leads one/two, explicit research lead three |
| Original serving evidence | `forecaster/live_forecast_evidence.py` | New live member/retrieval evidence; unexposed initialization and hourly completeness remain NULL |
| Climate truth | `forecaster/city_truth.py`, `truth_store.py` | Station/date final CLI, separate from high-so-far observations |
| Forecast schedule | `sfo-forecaster-refresh.service.in` | Observations/truth then Google facade; orchestrator serves EMOS first |
| Provider runtime | `google_weather_store.py`, `apple_weatherkit.py` | Private, expiring values; provider and promotion boundaries apply |
| Offline model work | `forecaster/research/` | Local heavy training/research, no automatic production promotion |
| Trading command | `trading/sfo_kalshi_quant/cli.py` → `_cli/` | Stable public CLI with separated handlers |
| Scheduled paper entries | `run_paper_scan_profiles.sh` → `portfolio-scan` | Joint allocator, account/policy admission, fee and risk gates |
| Order lifecycle | `execution.py`, `maker_fills.py`, `paper.py`, `monitor.py` | Distinguish requested, resting, filled, closed and settled quantities |
| Economic account | `account.py`, `logical_positions.py`, `paper_pnl.py` | Separate bankrolls; partial lots remain one logical position where appropriate |
| Journal storage | `db.py` → `store/` | Stable facade over schema/scoring/diagnostic storage modules |
| Strategy artifact | `strategy_research.py` → `strategy_lab/` | Current paper state plus separately dated historical analysis |
| Release findings | `strategy_lab/release.py` | Versioned audit metadata; not a return forecast |
| Paper profit audit | `scripts/audit_paper_performance.py` | Fresh export only; independent accounts, unassigned history and calendar-day P&L |
| Forecast evidence audit | `scripts/audit_forecast_evidence.py` | Source-separated reconstructed diagnostics; no issued-vintage claim |
| Fixed bias pilot | `scripts/evaluate_v7_bias_pilot.py` | Production correction helpers and shared scores; strict paired dates, retained compressed lineage |
| Public SPA | root `src/`, `vite.config.ts`, `package.json` | React/HeroUI Pro app built to `dist/` |
| Runtime publication | `run_publication_cycle.sh`, `publish_forecaster_pages.sh` | AWS JSON overlaid on prebuilt SPA; manifest/parity gates |
| Source deployment | `sync_to_box.sh` | Clean exact revision, backup verification, quiescence, install, account checks and timer recovery |
| Web deployment | `deploy_web_app.sh` | Separate built-SPA release with publication coordination |
| Runtime schedule | `trading/deploy/aws/systemd/` | Installer-controlled templates and timer policy |
| Off-host freshness | `scripts/check_publication_freshness.py`, `.github/workflows/publication-freshness.yml` | Independent read-only freshness check, no trading mutations |

`analyze`, `tail-basket`, `arbitrage`, and research replays are separate diagnostic
paths; they do not replace scheduled portfolio admission. Script and package
facades intentionally keep existing callers stable.

## Directories

| Directory | What belongs here |
|---|---|
| `forecaster/` | Lightweight serving, truth, provider adapters and forecast tests |
| `forecaster/research/` | Offline SFO preparation/training and retained comparison methods |
| `trading/sfo_kalshi_quant/` | Probability, market, portfolio, lifecycle, account and research modules |
| `trading/tests/` | Backend, accounting, execution, security and deploy regressions |
| `trading/deploy/aws/` | Runtime operations and systemd source templates |
| `src/` | Public SPA components, tolerant artifact types, charts and frontend tests |
| `scripts/` | Repository health/verification, local runtime cleanup, bundle capture and standalone research |
| `requirements/` | Hashed dependency inputs for production/verification |
| `docs/` | Current guides, dated evidence, audits, plans and historical handoffs |
| `public/` | Vite static fallback fixtures, public diagnostics, notices and assets |
| `.local/`, `.gstack/`, virtualenvs, `dist/` | Ignored private/disposable workstation state, not project source |

## Intentional mirrors and compatibility paths

An exact-byte scan of the inherited tracked checkout found four duplicate
pairs. None was an independent redundant algorithm:

| Pair | Why both paths exist |
|---|---|
| `forecaster/cities.py` / `trading/sfo_kalshi_quant/cities.py` | Independent deployed import roots; registry parity test prevents drift |
| `forecaster/forecast_data.json` / `public/forecast_data.json` | Deploy input and Vite fallback fixture; neither is current live-data authority |
| `forecaster/weather_story_data.json` / `public/weather_story_data.json` | Same deploy/Vite fixture boundary |
| `public/licenses/magicui.txt` / `src/components/magicui/LICENSE.md` | Public distribution notice and component source license |

The forecaster/trading source-selection helpers and recalibration functions also
serve independent runtime/import boundaries. Their parity/numerical boundary
tests are a reason to keep or carefully package them, not evidence to delete one.

`sync_to_lightsail.sh` is a forwarding-only deprecated wrapper for `sync_to_box.sh`.
`sync_forecaster_source.sh` is a disabled compatibility tombstone: it refuses a
partial release that could split runtime provenance. `systemd/not-installed/`
contains deliberately unscheduled research units. These are labeled contracts,
not a second active deployment or trading system. Consolidating them requires
verified caller migration and compatibility tests.

## Documentation roles

- Root README: project scope, evidence limits, setup and links.
- Forecaster/trading READMEs: domain commands and input/lifecycle boundaries.
- `architecture.md`: system data-flow and account/source contracts.
- `aws_deployment.md` and `operational_runbook.md`: guarded deployment/recovery.
- `data_and_artifacts.md`: runtime versus fixture/retained evidence ownership.
- `SESSION_MEMORY.md`: rolling dated handoff, refreshed after material operations.
- `research/`, `audits/`, dated plans and handoffs: retained historical evidence.
  Their dated findings are not silently rewritten into current operational facts.

Historical studies are retained even when a later experiment supersedes them.
Use the v7 audit as the current performance investigation, and follow the
original source/sample/date when comparing older results. Preserve AI-assisted
attribution, third-party notices and contributor history.

## Verification and safe cleanup

From the repository root, `bash scripts/run_tests.sh` runs backend and forecaster
tests; `bash scripts/verify_project.sh` adds health, Semgrep and compilation.
Use Python 3.12/3.13. The full Python gate does not validate React. Web work needs
licensed artifacts, frontend tests, lint/icons, `bun run build`, browser-observed
bundle budgets, desktop/mobile screenshots and driven DOM checks.

Local dashboard experiments begin with the documented disposable-runtime cleanup:
`python3 scripts/clear_local_runtime_state.py --confirm`. Production weather
and paper journals stay on AWS; downloaded evidence stays private and immutable.
A runtime cleanup must not delete trade histories, rename account eras, remove
final truth or erase unreviewed worktrees to make the directory look tidy.

Before branch/worktree cleanup, inspect each checkout's `git status`, commits
relative to `origin/main`, and merged PR identity. Preserve unique or uncommitted
work until incorporated or snapshotted. Do not reset the inherited dirty checkout
to make it resemble GitHub. The v7 work is isolated from it.

For a file move/deletion: prove equivalent behavior, locate every caller/import,
update references, run the relevant contract tests, and review the diff. Naming
similarity is insufficient. For a release: verify backups, exact source revision,
installed dependencies/units, distinct account reconciliation, safety flags,
fresh artifacts and public hashes. A passing source suite alone is insufficient.
