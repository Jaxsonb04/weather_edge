# Chronological Research Tuning and Promotion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the archived source-neutral scan context into leakage-resistant, execution-realistic evidence that can tune research challengers and promote only durable improvements into the target paper sleeve.

**Architecture:** Reconstruct every candidate from the immutable scan context available at decision time, split folds by station target-day, fit only on outcomes settled before each fold, and replay the exact exec-v3 lifecycle with point-in-time quotes, depth, fees, exits, and settlement. Challenger parameters are fixed before the test fold. Promotion is evidence-driven and paper-only: motion may generate hypotheses, the target sleeve supplies confirmatory evidence, and no result can enable or change the live profile automatically.

**Tech Stack:** Python 3.13+, SQLite, pytest, existing Gaussian PIT recalibration, exec-v3 replay, day-clustered bootstrap

---

## File structure

- Create `trading/sfo_kalshi_quant/research_walkforward.py` — source-neutral case loading, chronological folds, recalibration candidates, and paired scoring.
- Create `trading/sfo_kalshi_quant/research_promotion.py` — immutable challenger declarations, multiple-comparison control, and target-paper promotion gates.
- Modify `trading/sfo_kalshi_quant/store/schema.py` — additive research experiment/evidence tables.
- Modify `trading/sfo_kalshi_quant/db.py` — persist source-neutral scan identity and immutable evaluation evidence.
- Modify `trading/sfo_kalshi_quant/replay.py` — expose exact exec-v3 event construction for paired research replay.
- Modify `trading/sfo_kalshi_quant/forecast_scorecards.py` — publish paired forecast and promotion evidence.
- Modify `trading/sfo_kalshi_quant/_cli/main.py` — add research walk-forward/report commands without live activation.
- Create `trading/tests/test_research_walkforward.py` and `trading/tests/test_research_promotion.py`.
- Modify `trading/tests/test_replay.py` and `trading/tests/test_forecast_scorecards.py`.

### Task 1: Persist source-neutral scan contexts and immutable experiment declarations

**Files:**
- Modify: `trading/sfo_kalshi_quant/store/schema.py`
- Modify: `trading/sfo_kalshi_quant/db.py`
- Create: `trading/tests/test_research_walkforward.py`

- [ ] **Step 1: Add schema and write-path regressions**

Add named tests asserting one source context can feed multiple profile decisions,
the hash ignores profile/bankroll, experiment definitions become immutable after
their first evidence row, and incomplete point-in-time forecast/market/feature
payloads are rejected.

- [ ] **Step 2: Verify the current profile-scoped context fails**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_walkforward.py -k 'source_neutral or context or experiment'`

Expected: FAIL because contexts are written once per profile and no immutable experiment registry exists.

- [ ] **Step 3: Add normalized source contexts and experiment tables**

Add `source_context_hash TEXT`, `source_scan_run_id TEXT`, and `decision_policy_fingerprint TEXT` to the relevant snapshot tables. Create a unique source-context index over the canonical hash. Create:

```sql
CREATE TABLE IF NOT EXISTS research_experiments (
    experiment_id TEXT PRIMARY KEY,
    declared_at TEXT NOT NULL,
    hypothesis_family TEXT NOT NULL,
    candidate_key TEXT NOT NULL,
    candidate_version TEXT NOT NULL,
    parameter_json TEXT NOT NULL,
    evidence_role TEXT NOT NULL CHECK(evidence_role IN ('exploratory','confirmatory')),
    UNIQUE(hypothesis_family, candidate_key, candidate_version)
);

CREATE TABLE IF NOT EXISTS research_evidence (
    experiment_id TEXT NOT NULL REFERENCES research_experiments(experiment_id),
    fold_id TEXT NOT NULL,
    station_id TEXT NOT NULL,
    target_date TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    baseline_json TEXT NOT NULL,
    challenger_json TEXT NOT NULL,
    PRIMARY KEY(experiment_id, fold_id, station_id, target_date)
);
```

Reject updates to an experiment after its first evidence row. Store only normalized market/forecast input and derived Google challenger evidence; never copy expiring raw Google content into these tables.

- [ ] **Step 4: Canonicalize and reuse scan contexts**

```python
def source_context_hash(*, target_date, station_id, forecast, intraday, market, features) -> str:
    payload = {
        "target_date": target_date,
        "station_id": station_id,
        "forecast": forecast,
        "intraday": intraday,
        "market": market,
        "features": features,
    }
    return sha256(canonical_json(payload).encode()).hexdigest()
```

Profile strategy, bankroll, sleeve, and account identity belong to decision rows, not the source hash. An identical scan context may therefore be evaluated by live, target, and motion policies without selecting an insertion-order winner.

- [ ] **Step 5: Run schema/write-path tests and commit**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_walkforward.py trading/tests/test_paper_settlement.py trading/tests/test_profile_migration.py`

Expected: PASS.

```bash
git add trading/sfo_kalshi_quant/store/schema.py trading/sfo_kalshi_quant/db.py trading/tests/test_research_walkforward.py
git commit -m "feat: persist source-neutral research contexts"
```

### Task 2: Build leakage-resistant chronological folds

**Files:**
- Create: `trading/sfo_kalshi_quant/research_walkforward.py`
- Modify: `trading/tests/test_research_walkforward.py`

- [ ] **Step 1: Add fold-boundary and embargo regressions**

Add named tests for settlement-before-decision training membership, indivisible
station target-day folds, cross-profile deduplication, the one-day embargo, and
fold ordering that is invariant to database insertion order.

- [ ] **Step 2: Verify no suitable grouped fold generator exists**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_walkforward.py -k 'training or fold or embargo'`

Expected: FAIL.

- [ ] **Step 3: Implement station-day chronological folds**

```python
@dataclass(frozen=True)
class ResearchCase:
    station_id: str
    target_date: date
    decision_at: datetime
    settled_at: datetime
    lead_days: int
    source_context_hash: str
    baseline_mu: float
    baseline_sigma: float
    actual_high_f: float


@dataclass(frozen=True)
class WalkForwardFold:
    fold_id: str
    decision_at: datetime
    train: tuple[ResearchCase, ...]
    test: tuple[ResearchCase, ...]
```

Group test rows by `(station_id, target_date)`. A training row is eligible only when `settled_at < min(test.decision_at)` and its station target-day does not overlap the configurable one-day embargo. Duplicate profile/sleeve views of the same source hash are removed before folding.

- [ ] **Step 4: Make insufficient history explicit**

Return an unavailable fold reason rather than silently falling back to future data. Pooling order is exact station/lead, station/all-leads, climate-region/lead, then global/lead; every fallback is recorded in fold evidence.

- [ ] **Step 5: Run fold tests and commit**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_walkforward.py`

Expected: PASS.

```bash
git add trading/sfo_kalshi_quant/research_walkforward.py trading/tests/test_research_walkforward.py
git commit -m "feat: build chronological research folds"
```

### Task 3: Evaluate fixed Gaussian and Google-conditioned challengers

**Files:**
- Modify: `trading/sfo_kalshi_quant/research_walkforward.py`
- Modify: `trading/tests/test_research_walkforward.py`
- Test: `trading/tests/test_recalibration.py`

- [ ] **Step 1: Add training-only parameter tests**

Add named tests asserting training-only Gaussian fitting, recorded shrinkage and
pool fallback, predeclared identity/Google candidates, and that mutating a test
outcome cannot change the parameters used to score that case.

- [ ] **Step 2: Verify candidate evaluation is missing**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_walkforward.py -k 'recalibration or candidate or parameters'`

Expected: FAIL.

- [ ] **Step 3: Reuse the existing parametric PIT implementation**

Call `recalibration.fit_recalibration(training_triples, shrinkage_k=40.0)` for each declared cohort. Do not create per-bin isotonic maps. Evaluate these predeclared arms:

1. `active-identity-v1` — unchanged archived baseline distribution.
2. `gaussian-pit-station-lead-v1` — training-only shrunk mean shift and sigma scale.
3. `google-runtime-fixed-v1` — the fixed 15%/±1.5°F challenger only where the derived paired evidence exists; never backfill raw Google observations.

```python
def fit_fold_candidates(fold: WalkForwardFold) -> tuple[FittedCandidate, ...]:
    triples = [(row.baseline_mu, row.baseline_sigma, row.actual_high_f) for row in fold.train]
    recalibration = fit_recalibration(triples, shrinkage_k=40.0)
    return (
        FittedCandidate.identity(),
        FittedCandidate.gaussian_pit(recalibration, training_count=len(triples)),
    )
```

- [ ] **Step 4: Score distributions and bracket probabilities**

For every paired test case compute CRPS, ranked probability score, log score, interval coverage, PIT, bracket Brier, maximum calibration-bucket gap, and point error. Persist the candidate version and exact fitted parameters beside each fold score.

- [ ] **Step 5: Run candidate tests and commit**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_walkforward.py trading/tests/test_recalibration.py trading/tests/test_forecast_scorecards.py`

Expected: PASS.

```bash
git add trading/sfo_kalshi_quant/research_walkforward.py trading/tests/test_research_walkforward.py
git commit -m "feat: score fixed research forecast challengers"
```

### Task 4: Replay candidates through exact exec-v3 mechanics

**Files:**
- Modify: `trading/sfo_kalshi_quant/replay.py`
- Modify: `trading/sfo_kalshi_quant/research_walkforward.py`
- Modify: `trading/tests/test_replay.py`
- Modify: `trading/tests/test_research_walkforward.py`

- [ ] **Step 1: Add point-in-time execution parity tests**

Add named tests asserting no pre-decision quote/trade use, exec-v3 parity for
queue/partial-fill/fee/exit math, promotion blocking on incomplete market history,
and identical event streams for paired baseline/challenger replay.

- [ ] **Step 2: Verify the replay cannot yet consume a source-neutral candidate**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_walkforward.py -k 'replay or quote or event_stream' trading/tests/test_replay.py`

Expected: FAIL.

- [ ] **Step 3: Extract a shared replay-event constructor**

Expose a pure function in `replay.py` that turns archived point-in-time market snapshots, trade deltas, monitor snapshots, and settlement truth into the same ordered `ReplayEvent` stream used by chronological account replay. Preserve queue-ahead policy, partial fills, maker/taker fees, reservations, exits, cash, and settlement.

```python
events = build_exec_v3_events(
    decision_at=case.decision_at,
    market_snapshots=case.market_snapshots,
    trade_snapshots=case.trade_snapshots,
    monitor_snapshots=case.monitor_snapshots,
    settlement=case.settlement,
)
baseline_result = replay_events(baseline_orders, events, starting_cash=1000.0)
challenger_result = replay_events(challenger_orders, events, starting_cash=1000.0)
```

- [ ] **Step 4: Fail closed on incomplete execution history**

Set `promotion_eligible=False` with structured block reasons for missing initial quote, missing side depth, time-traveling events, missing settlement, unknown partial-fill ordering, or non-flat replay end. A research report may show diagnostic EV, but it must not label it realized P&L.

- [ ] **Step 5: Run replay parity tests and commit**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_replay.py trading/tests/test_research_walkforward.py trading/tests/test_limit_orders.py`

Expected: PASS.

```bash
git add trading/sfo_kalshi_quant/replay.py trading/sfo_kalshi_quant/research_walkforward.py trading/tests/test_replay.py trading/tests/test_research_walkforward.py
git commit -m "feat: replay research candidates through exec v3"
```

### Task 5: Compute paired daily and capacity evidence

**Files:**
- Modify: `trading/sfo_kalshi_quant/research_walkforward.py`
- Modify: `trading/tests/test_research_walkforward.py`

- [ ] **Step 1: Add paired-statistic regressions**

Add named tests asserting weather-day clustering, bootstrap resampling by
independent station-day rather than trade row, complete KPI/capacity fields, and
retention of zero-fill days in daily statistics.

- [ ] **Step 2: Verify the report lacks daily KPI/capacity evidence**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_walkforward.py -k 'daily or bootstrap or capacity'`

Expected: FAIL.

- [ ] **Step 3: Build paired evidence from account replays**

Report baseline, challenger, and paired delta for realized P&L/day, mean, median, standard deviation, positive-day rate, `$50` hit rate, after-fee ROI, log growth/day, maximum drawdown, turnover, fills, rejection reasons, contracts filled, dollars at risk, and capacity under the target/motion account limits. Include forecast scores from Task 3.

- [ ] **Step 4: Add deterministic day-clustered bootstrap**

Resample independent `(station_id, target_date)` clusters with a fixed seed and 10,000 draws. Publish percentile 95% intervals for paired realized P&L/day, log growth/day, ROI, CRPS, and Brier deltas. Keep exploratory motion results separate from confirmatory target results.

- [ ] **Step 5: Run statistics tests and commit**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_walkforward.py`

Expected: PASS.

```bash
git add trading/sfo_kalshi_quant/research_walkforward.py trading/tests/test_research_walkforward.py
git commit -m "feat: measure paired research performance"
```

### Task 6: Gate paper-target promotion and control repeated experiments

**Files:**
- Create: `trading/sfo_kalshi_quant/research_promotion.py`
- Create: `trading/tests/test_research_promotion.py`

- [ ] **Step 1: Add promotion and family-wise-error tests**

Add named tests asserting motion has proposal-only authority, promotion requires
30 independent confirmatory days, Holm adjustment blocks marginal repeated
hypotheses, forecast/drawdown regressions block a profitable candidate, and no
promotion can change live configuration or real-order flags.

- [ ] **Step 2: Verify no research promotion authority exists**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_promotion.py`

Expected: FAIL.

- [ ] **Step 3: Implement explicit gates**

Require all of:

- at least 30 independent confirmatory target days;
- lower 95% paired after-fee ROI interval above zero;
- lower 95% paired log-growth/day interval above zero;
- no worse maximum drawdown than the declared tolerance;
- no CRPS, Brier, or maximum calibration-gap regression beyond declared tolerances;
- complete exec-v3 replay evidence and a flat end state;
- Holm-adjusted significance within the predeclared hypothesis family.

The `$50/day` hit rate is reported and optimized but is not allowed to override these solvency, calibration, or evidence gates.

- [ ] **Step 4: Restrict output authority**

```python
@dataclass(frozen=True)
class PromotionDecision:
    experiment_id: str
    eligible_for_target_paper: bool
    block_reasons: tuple[str, ...]
    live_activation_allowed: bool = False
```

Promotion writes a versioned target-paper candidate proposal. It cannot edit `LIVE_PROFILE_OVERRIDES`, live fingerprints, `LIVE_ORDERS_ENABLED`, dry-run flags, or AWS real-order units.

- [ ] **Step 5: Run promotion tests and commit**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_promotion.py trading/tests/test_shared_account.py trading/tests/test_profile_migration.py`

Expected: PASS.

```bash
git add trading/sfo_kalshi_quant/research_promotion.py trading/tests/test_research_promotion.py
git commit -m "feat: gate research paper promotions"
```

### Task 7: Publish and operate the evidence loop

**Files:**
- Modify: `trading/sfo_kalshi_quant/forecast_scorecards.py`
- Modify: `trading/sfo_kalshi_quant/cli.py`
- Modify: `trading/sfo_kalshi_quant/_cli/parser.py`
- Modify: `trading/tests/test_forecast_scorecards.py`
- Modify: `trading/tests/test_research_promotion.py`

- [ ] **Step 1: Add CLI/report structure tests**

Add named tests asserting exploratory/confirmatory separation, observed-not-guaranteed
`$50` language, and read-only evaluation versus paper-target-only proposal authority.

- [ ] **Step 2: Verify report/CLI fields are missing**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_forecast_scorecards.py trading/tests/test_research_promotion.py -k 'report or cli or kpi'`

Expected: FAIL.

- [ ] **Step 3: Add commands and report payloads**

Add read-only `research-evaluate` and paper-only `research-propose-target` commands. Publish fold coverage, replay completeness, paired statistics, adjusted promotion gates, candidate version, and immutable experiment identity. Clearly label `$50/day` as a hard research KPI and show observed hit rate/shortfall.

- [ ] **Step 4: Run focused and full verification**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_research_walkforward.py trading/tests/test_research_promotion.py trading/tests/test_replay.py trading/tests/test_forecast_scorecards.py`

Expected: PASS.

Run: `./.venv-dev/bin/pytest -q trading/tests`

Expected: PASS except explicitly documented environment-only skips.

- [ ] **Step 5: Commit Task 7**

```bash
git add trading/sfo_kalshi_quant/forecast_scorecards.py trading/sfo_kalshi_quant/cli.py trading/sfo_kalshi_quant/_cli/parser.py trading/tests/test_forecast_scorecards.py trading/tests/test_research_promotion.py
git commit -m "feat: publish chronological research evidence"
```

## Final verification

- [ ] `./.venv-dev/bin/pytest -q trading/tests/test_research_walkforward.py trading/tests/test_research_promotion.py trading/tests/test_replay.py trading/tests/test_recalibration.py trading/tests/test_forecast_scorecards.py`
- [ ] `./.venv-dev/bin/pytest -q trading/tests`
- [ ] Confirm live limit/market fingerprints remain `a965c8280aca2b3621f0c312` and `73b10240c1c00a8937b5314f`.
- [ ] Confirm all real-money flags remain disabled and dry-run remains enabled.
- [ ] Confirm an experiment can affect only the target paper sleeve after explicit promotion evidence.
- [ ] Confirm the report never claims the `$50/day` KPI was guaranteed or achieved when it was not.
