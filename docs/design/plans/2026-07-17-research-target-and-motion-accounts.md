# Research Target and Motion Accounts Implementation Plan

> **Design record.** A planning document, kept for the reasoning it captures
> rather than as a live task list. Its checkboxes were never updated as work
> landed, so they understated what shipped; they have been flattened to plain
> bullets. For what actually shipped, see the git history and the
> [audit remediation ledger](../../codebase_audit_2026-06-15.md#remediation-status).

**Goal:** Build two isolated $1,000 paper-research accounts: a target book with a hard $50 daily realized-P&L KPI and a one-contract motion book with no trade-count throttle.

**Architecture:** Keep all live code paths and fingerprints unchanged. Add explicit research-sleeve domain types, accounts, atomic admission, scenario-risk allocation, and reporting. The target and motion books share forecast/market snapshots but never cash, pauses, exposure, P&L, or promotion evidence.

**Tech Stack:** Python 3.13+, SQLite, pytest, React/TypeScript, HeroUI Pro, Vite

---

## File structure

- Create `trading/sfo_kalshi_quant/research_policy.py` — sleeve identities, fixed policies, clocks, and fingerprints.
- Create `trading/sfo_kalshi_quant/research_portfolio.py` — target/motion allocation and scenario risk.
- Create `trading/sfo_kalshi_quant/research_goals.py` — immutable daily goal state/statistics.
- Modify `trading/sfo_kalshi_quant/account.py` — additive account IDs and explicit routing only.
- Modify `trading/sfo_kalshi_quant/store/schema.py` — additive audit columns, accounts, goals, and account-scoped active index.
- Modify `trading/sfo_kalshi_quant/db.py` — atomic sleeve admission and account-scoped queries.
- Modify `trading/sfo_kalshi_quant/logical_positions.py` — sleeve/policy/execution identity validation.
- Modify `trading/sfo_kalshi_quant/paper.py` and `trading/sfo_kalshi_quant/_cli/scan.py` — independent plans and execution.
- Modify `trading/sfo_kalshi_quant/strategy_lab/build.py`, `profiles.py`, and `paper_card.py` — target/motion metrics.
- Modify `src/lib/strategy.ts` and Strategy Lab React components — three separately labeled books.
- Modify AWS runner/config templates — independent placement flags.
- Add focused tests named below.

### Task 1: Define research sleeve policies without changing live fingerprints

**Files:**
- Create: `trading/sfo_kalshi_quant/research_policy.py`
- Modify: `trading/sfo_kalshi_quant/account.py`
- Test: `trading/tests/test_research_sleeves.py`

- **Step 1: Write policy and live-identity tests**

```python
def test_research_policy_constants_are_fixed():
    assert TARGET_POLICY.account_id == "paper-research-target-v1"
    assert MOTION_POLICY.account_id == "paper-research-motion-v1"
    assert TARGET_POLICY.reference_equity == 1000.0
    assert TARGET_POLICY.target_pnl == 50.0

def test_live_account_and_fingerprint_are_unchanged():
    assert account_for_profile("live") == "paper-shared"
    config = strategy_config_for_profile("live")
    assert strategy_fingerprint(config, entry_mode="limit") == "a965c8280aca2b3621f0c312"
    assert strategy_fingerprint(config, entry_mode="market") == "73b10240c1c00a8937b5314f"
```

- **Step 2: Verify missing domain types fail**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_sleeves.py`

Expected: FAIL because the new module/constants do not exist.

- **Step 3: Implement immutable sleeve policies**

```python
class ResearchSleeve(str, Enum):
    TARGET = "target"
    MOTION = "motion"

@dataclass(frozen=True)
class ResearchSleevePolicy:
    sleeve: ResearchSleeve
    account_id: str
    policy_version: str
    reference_equity: float
    target_return: float
    max_position_risk_pct: float
    max_city_target_risk_pct: float
    max_region_day_risk_pct: float
    max_aggregate_risk_pct: float
    daily_loss_pause_pct: float
    min_lead_days: int
    one_contract: bool

    @property
    def target_pnl(self) -> float:
        return self.reference_equity * self.target_return
```

Define target as 3%/6%/12%/25%/10%, `min_lead_days=1`; define motion as one contract, 2%/4%/10%/5%, `min_lead_days=0`. Add `account_for_research_sleeve`; do not modify `StrategyConfig` or `LIVE_PROFILE_OVERRIDES`.

- **Step 4: Run policy/account tests**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_sleeves.py trading/tests/test_shared_account.py trading/tests/test_profile_migration.py`

Expected: PASS with the pre-existing live fingerprint unchanged.

- **Step 5: Commit Task 1**

```bash
git add trading/sfo_kalshi_quant/research_policy.py trading/sfo_kalshi_quant/account.py trading/tests/test_research_sleeves.py
git commit -m "feat: define isolated research sleeve policies"
```

### Task 2: Add additive sleeve/account/goal schema

**Files:**
- Modify: `trading/sfo_kalshi_quant/store/schema.py`
- Test: `trading/tests/test_research_sleeves.py`
- Test: `trading/tests/test_open_position_guard.py`

- **Step 1: Write migration and index tests**

Add three named tests: `test_init_bootstraps_both_research_accounts_without_rewriting_legacy`
asserts the two new account rows exist and the legacy shadow rows are byte-for-byte
unchanged; `test_target_and_motion_can_hold_same_market_but_same_account_cannot`
asserts the account-scoped active index; and
`test_new_research_write_requires_sleeve_policy_identity` asserts a missing sleeve,
policy version, or fingerprint is rejected.

- **Step 2: Verify tests fail against the two-profile schema**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_sleeves.py trading/tests/test_open_position_guard.py`

Expected: FAIL for missing columns/accounts/index.

- **Step 3: Add audit columns and daily goals**

Add nullable columns to `paper_orders`, `decision_snapshots`, and scan/monitor context tables:

```python
RESEARCH_IDENTITY_COLUMNS = {
    "research_sleeve": "TEXT",
    "research_policy_version": "TEXT",
    "policy_fingerprint": "TEXT",
    "objective_day": "TEXT",
    "lead_bucket": "TEXT",
    "scan_run_id": "TEXT",
    "reentry_fingerprint": "TEXT",
}
```

Create:

```sql
CREATE TABLE IF NOT EXISTS research_daily_goals (
    objective_day TEXT NOT NULL,
    account_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reference_equity REAL NOT NULL CHECK(reference_equity > 0),
    target_return REAL NOT NULL CHECK(target_return > 0),
    target_pnl REAL NOT NULL CHECK(target_pnl > 0),
    PRIMARY KEY(objective_day, account_id, policy_version)
);
```

Replace the active-order index with a partial unique index on `COALESCE(account_id,'paper-shared'), target_date, market_ticker, UPPER(COALESCE(side,'YES'))` for open/resting statuses. Validate existing duplicates before dropping the old index; fail new research closed if the new index cannot be built.

- **Step 4: Run migration tests against fresh and legacy fixtures**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_sleeves.py trading/tests/test_open_position_guard.py trading/tests/test_profile_migration.py`

Expected: PASS without rewriting `paper-research-shadow` history.

- **Step 5: Commit Task 2**

```bash
git add trading/sfo_kalshi_quant/store/schema.py trading/tests/test_research_sleeves.py trading/tests/test_open_position_guard.py
git commit -m "feat: add research account identity schema"
```

### Task 3: Make research admission atomic and account-scoped

**Files:**
- Modify: `trading/sfo_kalshi_quant/db.py`
- Test: `trading/tests/test_research_sleeves.py`
- Test: `trading/tests/test_paper_risk_pause.py`

- **Step 1: Add concurrency and isolation regressions**

Add four named tests covering concurrent cash admission, motion-loss pause
isolation, reservation isolation, and active-exposure release after close. Use
two independent SQLite connections plus a barrier for the concurrency test and
assert no account can reserve more than its own available cash.

- **Step 2: Verify current risk/profile scoping fails**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_sleeves.py trading/tests/test_paper_risk_pause.py`

Expected: FAIL because pause/capacity are profile-scoped and admission is check-then-insert.

- **Step 3: Add one atomic research-entry API**

```python
@dataclass(frozen=True)
class ResearchAdmission:
    account_id: str
    sleeve: ResearchSleeve
    policy_version: str
    policy_fingerprint: str
    objective_day: str
    scan_run_id: str
    reentry_fingerprint: str

def record_research_order_atomic(
    self,
    target_date: str,
    decision: TradeDecision,
    *,
    admission: ResearchAdmission,
    strategy_config: StrategyConfig,
) -> int | None:
    with self._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        self._assert_research_capacity(conn, decision, admission)
        order_id = self._insert_research_paper_order(
            conn,
            target_date=target_date,
            decision=decision,
            admission=admission,
            strategy_config=strategy_config,
        )
        self._record_research_reservation_or_fill(
            conn,
            order_id=order_id,
            account_id=admission.account_id,
            decision=decision,
        )
        conn.commit()
        return order_id
```

The capacity query reads only that account and sleeve, uses active scenario exposure rather than cumulative spend, and applies the research civil-day clock. Preserve the live `record_paper_order` path unchanged.

- **Step 4: Scope all research read paths**

Make equity, pause, active-entry, entries-for-side, capacity, open-risk, and account ledger APIs accept `account_id`. New research calls must never use the broad `risk_profile="research"` aggregate.

- **Step 5: Run DB/risk tests and commit**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_sleeves.py trading/tests/test_paper_risk_pause.py trading/tests/test_shared_account.py`

Expected: PASS.

```bash
git add trading/sfo_kalshi_quant/db.py trading/tests/test_research_sleeves.py trading/tests/test_paper_risk_pause.py
git commit -m "feat: admit research orders atomically"
```

### Task 4: Extend logical identity validation to research generations

**Files:**
- Modify: `trading/sfo_kalshi_quant/logical_positions.py`
- Modify: `trading/sfo_kalshi_quant/db.py`
- Test: `trading/tests/test_logical_positions.py`

- **Step 1: Add child-crossing identity tests**

Parameterize child mismatches for `research_sleeve`, `research_policy_version`, `policy_fingerprint`, `strategy_fingerprint`, and `execution_model_version`; each must invalidate the group.

- **Step 2: Run and verify the missing checks fail**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_logical_positions.py -k 'sleeve or policy or fingerprint or execution'`

Expected: FAIL.

- **Step 3: Add fields to exact-match validation**

```python
_EXACT_MATCH_FIELDS = (
    "market_ticker", "target_date", "side", "risk_profile", "account_id",
    "research_sleeve", "research_policy_version", "policy_fingerprint",
    "strategy_fingerprint", "execution_model_version",
)
```

Ensure partial-close children copy these fields from the root in one helper.

- **Step 4: Run logical/settlement tests and commit**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_logical_positions.py trading/tests/test_paper_settlement.py trading/tests/test_audit_2026_07_14.py`

Expected: PASS.

```bash
git add trading/sfo_kalshi_quant/logical_positions.py trading/sfo_kalshi_quant/db.py trading/tests/test_logical_positions.py
git commit -m "fix: validate research lot identity"
```

### Task 5: Implement independent target and motion allocation

**Files:**
- Create: `trading/sfo_kalshi_quant/research_portfolio.py`
- Modify: `trading/sfo_kalshi_quant/portfolio.py`
- Test: `trading/tests/test_portfolio_allocator.py`
- Test: `trading/tests/test_research_sleeves.py`

- **Step 1: Add target/motion policy tests**

Add named tests for: every positive-LCB day-ahead target candidate up to the
scenario cap; target rejection of same-day/negative-LCB rows; deterministic
one-contract motion ordering across every eligible candidate; settlement-scenario
loss for mutually exclusive brackets; and an infeasible `$50` target report that
does not loosen any gate.

- **Step 2: Verify the current generic research allocator fails**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_portfolio_allocator.py trading/tests/test_research_sleeves.py`

Expected: FAIL because there is one clipped research plan and a sampler.

- **Step 3: Implement scenario exposure**

```python
def city_target_worst_case_loss(legs: Sequence[PortfolioLeg], settlement_bins: Sequence[int]) -> float:
    return max(
        -sum(_leg_settlement_pnl(leg, settled_high) for leg in legs)
        for settled_high in settlement_bins
    )
```

Pending orders reserve full entry loss. Region loss is the sum of city-target maxima. Partial children are not separate legs.

- **Step 4: Implement the two-plan result**

```python
@dataclass(frozen=True)
class ResearchPlans:
    target: PortfolioPlan
    motion: PortfolioPlan
    target_pnl: float
    realized_today: float
    remaining_target: float
    available_conservative_expected_profit: float
    target_feasible_from_current_opportunity_set: bool
```

Target uses day-ahead, `edge_lcb >= 0`, conservative expected profit/worst-case-dollar then log-growth ordering. Motion evaluates every point-positive candidate, persists all dispositions, and places one contract in deterministic order until cash/scenario caps bind. Neither plan has a count cap.

- **Step 5: Run allocator tests and commit**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_portfolio_allocator.py trading/tests/test_joint_kelly.py trading/tests/test_research_sleeves.py`

Expected: PASS.

```bash
git add trading/sfo_kalshi_quant/research_portfolio.py trading/sfo_kalshi_quant/portfolio.py trading/tests/test_portfolio_allocator.py trading/tests/test_research_sleeves.py
git commit -m "feat: allocate target and motion research"
```

### Task 6: Execute both research plans without throttling motion

**Files:**
- Modify: `trading/sfo_kalshi_quant/paper.py`
- Modify: `trading/sfo_kalshi_quant/_cli/scan.py`
- Test: `trading/tests/test_research_sleeves.py`
- Test: `trading/tests/test_entry_target_gate.py`
- Test: `trading/tests/test_research_shadow.py`

- **Step 1: Add scan and re-entry tests**

Test one shared scan context producing both plans; target blocks same-day, motion places it; the 25% sampler is never called; a terminal motion trade can re-enter after price moves 1 cent, probability moves 2 points, or completeness changes; the identical fingerprint is rejected.

- **Step 2: Verify current orchestration fails**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_sleeves.py trading/tests/test_entry_target_gate.py trading/tests/test_research_shadow.py`

Expected: FAIL.

- **Step 3: Build once and evaluate twice**

In the research scan: fetch forecast, ladder, market snapshot, and probabilities once; pass immutable inputs to target and motion policy evaluators; persist both decision/disposition sets with explicit fingerprints; then atomically admit each independent plan.

Use this fingerprint payload:

```python
payload = {
    "account_id": policy.account_id,
    "sleeve": policy.sleeve.value,
    "scan_run_id": scan_run_id,
    "ticker": decision.ticker,
    "side": decision.side,
    "executable_price_cents": round(decision.ask * 100),
    "probability_bucket": round(decision.probability * 50),
    "observed_high_state": diagnostics.observed_high_state,
}
```

Target uses maker-or-taker after-fee LCB validity. Motion uses immediate visible-ask taker execution, one contract, exact fees, and no $5 minimum. Keep active duplicate guarding.

- **Step 4: Run scan, paper, fill, and fee tests**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_sleeves.py trading/tests/test_entry_target_gate.py trading/tests/test_research_shadow.py trading/tests/test_limit_orders.py trading/tests/test_maker_fills.py trading/tests/test_bins_and_fees.py`

Expected: PASS.

- **Step 5: Commit Task 6**

```bash
git add trading/sfo_kalshi_quant/paper.py trading/sfo_kalshi_quant/_cli/scan.py trading/tests/test_research_sleeves.py trading/tests/test_entry_target_gate.py trading/tests/test_research_shadow.py
git commit -m "feat: run target and motion research books"
```

### Task 7: Add immutable daily goals and honest statistics

**Files:**
- Create: `trading/sfo_kalshi_quant/research_goals.py`
- Modify: `trading/sfo_kalshi_quant/db.py`
- Modify: `trading/sfo_kalshi_quant/strategy_lab/build.py`
- Modify: `trading/sfo_kalshi_quant/strategy_lab/profiles.py`
- Modify: `trading/sfo_kalshi_quant/strategy_lab/paper_card.py`
- Test: `trading/tests/test_research_goals.py`
- Test: `trading/tests/test_strategy_research.py`

- **Step 1: Add Pacific-day, zero-day, partial-lot, and lock tests**

Add named tests asserting the frozen `$50` goal from original equity, explicit
zero-P&L calendar days, lot-date P&L with one logical decision, and target-only
new-risk lock after `$50` while motion continues.

- **Step 2: Verify goal state is absent**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_goals.py trading/tests/test_strategy_research.py`

Expected: FAIL.

- **Step 3: Implement immutable objective state**

```python
@dataclass(frozen=True)
class DailyGoalState:
    objective_day: date
    realized_pnl: float
    target_pnl: float
    remaining_pnl: float
    achieved: bool
    locked: bool
```

Use `America/Los_Angeles` civil dates only for research KPIs/loss windows. Keep live clock helpers untouched. Freeze one `research_daily_goals` row on first scan of each day.

- **Step 4: Add clustered summary metrics**

Return observed days, hit count/rate, mean, median, p25/p75, standard deviation, day-cluster bootstrap interval, max drawdown, log growth, independent city-target days, lead split, fee/fill/expiry metrics, and `target_feasible`.

- **Step 5: Run backend report tests and commit**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_goals.py trading/tests/test_strategy_research.py trading/tests/test_paper_summary.py trading/tests/test_readiness.py`

Expected: PASS with every research sleeve excluded from live readiness.

```bash
git add trading/sfo_kalshi_quant/research_goals.py trading/sfo_kalshi_quant/db.py trading/sfo_kalshi_quant/strategy_lab/build.py trading/sfo_kalshi_quant/strategy_lab/profiles.py trading/sfo_kalshi_quant/strategy_lab/paper_card.py trading/tests/test_research_goals.py trading/tests/test_strategy_research.py
git commit -m "feat: report the daily research target"
```

### Task 8: Render live, target, and motion separately

**Files:**
- Modify: `src/lib/strategy.ts`
- Modify: `src/components/strategy/ProfileComparison.tsx`
- Modify: `src/components/strategy/ProfileDashboard.tsx`
- Modify: `src/components/strategy/ProfileExplorer.tsx`
- Modify: `src/components/views/StrategyLabView.tsx`
- Test: `src/lib/strategy.test.ts`
- Test: `src/lib/strategy.more.test.ts`
- Test: `src/components/strategy/ProfileComparison.test.tsx`
- Test: `src/components/strategy/ProfileDashboard.test.tsx`

- **Step 1: Read required frontend skills before editing**

Read `frontend-design`, `ui-ux-pro-max`, `web-design-guidelines`, and `agent-browser` completely, per `AGENTS.md`.

- **Step 2: Add failing parser/render tests**

Assert that missing sleeve fields remain backward-compatible, live is first, target shows `$50` progress/feasibility, and motion is labeled excluded.

- **Step 3: Implement tolerant parsing and three-book rendering**

Do not hard-code data presence. Preserve the operational-instrument visual language. Display mean and median together, show independent days, and never imply a guaranteed return.

- **Step 4: Run frontend tests and build**

Run: `bun test && bun run build`

Expected: all tests pass and Vite builds `dist/`.

- **Step 5: Serve and browser-verify desktop/mobile**

Run: `python3 scripts/clear_local_runtime_state.py --confirm`, rebuild, serve `dist/`, capture desktop and 390px mobile screenshots, interact with each book selector, and read the DOM back to verify counts/labels.

- **Step 6: Commit Task 8**

```bash
git add src/lib/strategy.ts src/lib/strategy.test.ts src/lib/strategy.more.test.ts src/components/strategy/ProfileComparison.tsx src/components/strategy/ProfileComparison.test.tsx src/components/strategy/ProfileDashboard.tsx src/components/strategy/ProfileDashboard.test.tsx src/components/strategy/ProfileExplorer.tsx src/components/views/StrategyLabView.tsx
git commit -m "feat: show target and motion research books"
```

### Task 9: Add independent AWS placement flags

**Files:**
- Modify: `trading/deploy/aws/run_paper_scan_profiles.sh`
- Modify: `trading/deploy/aws/sfo-weather.env.example`
- Modify: `trading/deploy/aws/systemd/sfo-kalshi-paper-scan.service.in`
- Test: `trading/tests/test_aws_deploy.py`
- Test: `trading/tests/test_deploy_shell_behavior.py`

- **Step 1: Add flag-isolation tests**

Assert `PAPER_PLACE_LIVE`, `PAPER_PLACE_RESEARCH_TARGET`, and `PAPER_PLACE_RESEARCH_MOTION` independently control their account and no research flag can enable live.

- **Step 2: Implement explicit default-off flags**

Parse each flag separately; unknown/missing values are false. Continue using the single scan lock.

- **Step 3: Run deployment tests and shell syntax checks**

Run the focused pytest shell-contract tests and `bash -n` on modified scripts.

- **Step 4: Commit Task 9**

```bash
git add trading/deploy/aws/run_paper_scan_profiles.sh trading/deploy/aws/sfo-weather.env.example trading/deploy/aws/systemd/sfo-kalshi-paper-scan.service.in trading/tests/test_aws_deploy.py trading/tests/test_deploy_shell_behavior.py
git commit -m "feat: isolate research placement controls"
```

### Task 10: Research-system verification

- **Step 1: Run all research/account tests**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_sleeves.py trading/tests/test_research_goals.py trading/tests/test_open_position_guard.py trading/tests/test_paper_risk_pause.py trading/tests/test_portfolio_allocator.py trading/tests/test_entry_target_gate.py trading/tests/test_research_shadow.py trading/tests/test_readiness.py`

Expected: PASS.

- **Step 2: Run full Python/frontend/build suites**

Run: `./.venv-dev/bin/pytest -q`, `bun test`, `bun run lint`, and `bun run build`.

Expected: PASS.

- **Step 3: Run deterministic account reconciliation**

Seed live, legacy research, target, and motion accounts; verify cash + reserved + fills + proceeds reconcile independently to the cent and motion losses cannot change target/live equity.

- **Step 4: Generate and browser-check a local Strategy Lab artifact**

Confirm one logical row per trade, target-only KPI, separate motion card, correct hit/zero-day metrics, and no live-readiness contamination.
