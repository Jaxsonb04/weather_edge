# Multi-Bin Arbitrage Implementation Plan

> **Design record.** A planning document, kept for the reasoning it captures
> rather than as a live task list. Its checkboxes were never updated as work
> landed, so they understated what shipped; they have been flattened to plain
> bullets. For what actually shipped, see the git history and the
> [audit remediation ledger](../../codebase_audit_2026-06-15.md#remediation-status).

> Historical plan dated 2026-06-12. Deployment steps below refer to the former
> Lightsail host; current operations use EC2 and `sync_to_box.sh`.

**Goal:** Add a paper-only arbitrage scanner that evaluates all active SFO temperature bins for same-bin and full-ladder guaranteed-payoff portfolios, then deploy it to the Lightsail paper engine.

**Architecture:** Add `sfo_kalshi_quant/arbitrage.py` as the portfolio calculator. Reuse existing `TradeDecision` rows for each leg so database, settlement, reporting, and paper PnL continue to work without schema changes. Add a grouped placement method to `PaperTrader` so paired/multi-bin legs are sized and exposure-capped together.

**Tech Stack:** Python dataclasses, existing Kalshi market models, existing fee model, existing CLI and paper-order SQLite store.

---

### Task 1: Portfolio Arbitrage Calculator

**Files:**
- Create: `trading/sfo_kalshi_quant/arbitrage.py`
- Test: `trading/tests/test_arbitrage.py`

- Write failing tests for same-bin YES/NO box arbitrage, full-ladder YES arbitrage, full-ladder NO arbitrage, incomplete-ladder rejection, and whole-contract budget sizing.
- Run `python3 trading/tests/test_arbitrage.py` or the project test runner and confirm the new tests fail because `sfo_kalshi_quant.arbitrage` does not exist.
- Implement `ArbitrageOpportunity`, `ArbitrageLeg`, `build_arbitrage_opportunities`, ladder coverage checks, and equal-contract sizing.
- Run the focused tests and confirm they pass.

### Task 2: Grouped Paper Placement

**Files:**
- Modify: `trading/sfo_kalshi_quant/paper.py`
- Test: `trading/tests/test_paper_settlement.py`

- Write failing tests proving same-market YES+NO arbitrage legs are placed together, while unrelated existing positions still block grouped arbitrage placement.
- Run the focused paper tests and confirm they fail on the current side-agnostic open-position block.
- Add `PaperTrader.place_arbitrage` with atomic preflight, grouped exposure fitting, whole-contract preservation, and normal per-side re-entry checks.
- Run the focused paper tests and confirm they pass.

### Task 3: CLI And AWS Scanner Wiring

**Files:**
- Modify: `trading/sfo_kalshi_quant/cli.py`
- Modify: `trading/deploy/aws/run_paper_scan_profiles.sh`
- Modify: `trading/docs/strategy.md`
- Modify: `docs/operational_runbook.md`

- Write failing CLI tests or focused command checks for a new `arbitrage` subcommand.
- Add `arbitrage` CLI flags: `--target-date`, `--offline-events`, `--max-arb-spend`, `--min-profit`, `--place-paper`, `--skip-context-snapshots`, `--no-ensemble`, `--calibration-source`, and `--ensemble-timeout`.
- Print ranked arbitrage opportunities with kind, leg count, contracts, spend, guaranteed payout, guaranteed profit, return on spend, and guardrail reasons.
- Add optional AWS scan execution controlled by `SFO_PAPER_SCAN_ARBITRAGE_ENABLED`, default on.
- Update docs with the math and paper-only safety constraints.

### Task 4: Verification And Deployment

**Files:**
- No source files unless verification exposes a defect.

- Run `PYTHONPATH=trading:forecaster python3 -m pytest trading/tests forecaster/tests -q`.
- Run `bash scripts/verify_project.sh`.
- Run a local no-live-market or offline command check for the new arbitrage CLI if available.
- Historical: sync to the then-current Lightsail host when credentials are available.
- Re-run or restart the paper-scan service on Lightsail if SSH credentials are available.
