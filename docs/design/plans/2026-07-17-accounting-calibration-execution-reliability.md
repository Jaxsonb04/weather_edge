# Accounting, Calibration, and Execution Reliability Implementation Plan

> **Design record.** A planning document, kept for the reasoning it captures
> rather than as a live task list. Its checkboxes were never updated as work
> landed, so they understated what shipped; they have been flattened to plain
> bullets. For what actually shipped, see the git history and the
> [audit remediation ledger](../../codebase_audit_2026-06-15.md#remediation-status).

**Goal:** Finish the logical-position migration and repair the profile-contaminated calibration sample, inside-spread queue model, and Google failure accounting before adding new research or weather behavior.

**Architecture:** Reuse `logical_positions.py` as the sole decision-level projection while retaining execution lots for exact money and timing. Partition decision samples by normalized profile until a source-neutral context replay exists. Centralize maker queue initialization so runtime and replay use identical price-level semantics, and replace batch-estimate Google usage with per-dispatch accounting.

**Tech Stack:** Python 3.13+, SQLite, pytest, standard-library dataclasses and mappings

---

## File structure

- Modify `trading/sfo_kalshi_quant/summary.py` — logical counts/rows and lot-exact money.
- Modify `trading/sfo_kalshi_quant/strategy_lab/paper_card.py` — logical Strategy Lab cards, ledgers, and diagnostics.
- Modify `trading/sfo_kalshi_quant/store/scoring.py` — profile-aware sample keys in SQL and Python fallbacks.
- Modify `trading/sfo_kalshi_quant/strategy_lab/calibration.py` — rescore each profile from its matching sample.
- Modify `trading/sfo_kalshi_quant/execution.py` — one queue-ahead policy helper.
- Modify `trading/sfo_kalshi_quant/db.py` and `trading/sfo_kalshi_quant/replay.py` — use the shared queue policy.
- Modify `forecaster/google_api.py` and `forecaster/google_weather_cache.py` — expose and reconcile per-dispatch events on exceptions.
- Test `trading/tests/test_paper_summary.py`, `trading/tests/test_strategy_research.py`, `trading/tests/test_paper_settlement.py`, `trading/tests/test_backtest_rescore.py`, `trading/tests/test_limit_orders.py`, `trading/tests/test_replay.py`, and `forecaster/tests/test_google_weather_cache.py`.
- Create `forecaster/tests/test_google_api.py` — transport dispatch-count and sanitized exception tests.

### Task 1: Make CLI paper summaries logical-position correct

**Files:**
- Modify: `trading/sfo_kalshi_quant/summary.py`
- Test: `trading/tests/test_paper_summary.py`

- **Step 1: Add a failing multi-lot summary regression**

Create one root that closes in three lots and assert that the daily/global/profile/side/exit/mover views report one resolved decision while P&L and capital equal all three lots:

```python
def test_partial_exit_lots_publish_one_logical_trade(tmp_path):
    store = _store_with_forecaster(tmp_path)
    root_id = _record_filled_order(store, contracts=6, risk_profile="live")
    _close_in_parts(store, root_id, quantities=(2, 2, 2), exit_price=0.86)

    payload = build_paper_summary(
        db_path=store.path,
        forecaster_root=tmp_path,
        days=7,
        now=datetime(2026, 7, 17, 18, tzinfo=UTC),
    )

    assert payload["totals"]["trades_closed"] == 1
    assert payload["totals"]["wins"] + payload["totals"]["losses"] == 1
    assert payload["profiles"][0]["resolved"] == 1
    assert payload["side_performance"]["NO"]["trades"] == 1
    assert sum(payload["exit_reasons"].values()) == 1
    assert payload["totals"]["realized_pnl"] == pytest.approx(
        sum(row["realized_pnl"] for row in store.paper_orders() if row["realized_pnl"] is not None)
    )
```

- **Step 2: Run the regression and verify the lot-count failure**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_paper_summary.py::test_partial_exit_lots_publish_one_logical_trade`

Expected: FAIL because profile/side/exit counts equal the number of child lots.

- **Step 3: Project decisions and lots separately**

Import `group_logical_positions`. Build these collections once in `build_paper_summary`:

```python
positions = group_logical_positions(orders)
valid_positions = [position for position in positions if position.valid]
terminal_positions = [position for position in valid_positions if position.terminal]
logical_rows = [position.as_row() for position in terminal_positions]
terminal_lots = [lot for position in terminal_positions for lot in position.resolved_lots]
```

Use `logical_rows` for opened/resolved/win/loss, profile, side, exit, city, and best/worst counts. Use `terminal_lots` for realized P&L, capital, fees, and the actual local day of each partial realization. Never count a nonterminal partial root as a win or loss.

- **Step 4: Run summary and neighboring regressions**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_paper_summary.py trading/tests/test_side_performance.py trading/tests/test_dynamic_bankroll.py`

Expected: PASS.

- **Step 5: Commit Task 1**

```bash
git add trading/sfo_kalshi_quant/summary.py trading/tests/test_paper_summary.py
git commit -m "fix: summarize logical paper positions"
```

### Task 2: Make Strategy Lab paper cards logical-position correct

**Files:**
- Modify: `trading/sfo_kalshi_quant/strategy_lab/paper_card.py`
- Test: `trading/tests/test_strategy_research.py`

- **Step 1: Add a failing full-card regression**

```python
def test_strategy_lab_collapses_partial_exit_lots_everywhere(tmp_path):
    db_path = _db_with_three_part_close(tmp_path)
    paper = paper_card._paper_payload(db_path)

    live = next(row for row in paper["profiles"] if row["risk_profile"] == "live")
    assert live["closed_positions"] == 1
    assert live["wins"] + live["losses"] == 1
    assert len(paper["closed_positions"]) == 1
    assert paper["diagnostics"]["resolved_positions"] == 1
    assert paper["closed_positions"][0]["exit_fill_count"] == 3
```

- **Step 2: Verify the raw-lot failure**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_strategy_research.py::test_strategy_lab_collapses_partial_exit_lots_everywhere`

Expected: FAIL with counts or closed rows equal to three.

- **Step 3: Replace raw closed queries with one journal projection**

Load all non-rejected rows with the columns required by `logical_positions.py`, call `group_logical_positions`, and pass logical rows plus terminal lots into small pure helpers:

```python
@dataclass(frozen=True)
class PaperJournalProjection:
    positions: tuple[LogicalPaperPosition, ...]
    terminal_rows: tuple[dict[str, Any], ...]
    terminal_lots: tuple[dict[str, Any], ...]
    open_rows: tuple[dict[str, Any], ...]

def _paper_journal_projection(rows: Iterable[sqlite3.Row]) -> PaperJournalProjection:
    positions = tuple(group_logical_positions(rows))
    valid = tuple(position for position in positions if position.valid)
    return PaperJournalProjection(
        positions=positions,
        terminal_rows=tuple(position.as_row() for position in valid if position.terminal),
        terminal_lots=tuple(lot for position in valid if position.terminal for lot in position.resolved_lots),
        open_rows=tuple(position.as_row() for position in valid if not position.terminal),
    )
```

Counts, ledgers, W-L, exit reasons, city chips, diagnostics, and best/worst rows consume `terminal_rows`; exact dollars consume `terminal_lots`.

- **Step 4: Run Strategy Lab tests**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_strategy_research.py trading/tests/test_strategy_lab_structure.py trading/tests/test_audit_2026_07_14.py`

Expected: PASS.

- **Step 5: Commit Task 2**

```bash
git add trading/sfo_kalshi_quant/strategy_lab/paper_card.py trading/tests/test_strategy_research.py
git commit -m "fix: publish logical Strategy Lab trades"
```

### Task 3: Partition calibration samples by profile

**Files:**
- Modify: `trading/sfo_kalshi_quant/store/scoring.py`
- Modify: `trading/sfo_kalshi_quant/strategy_lab/calibration.py`
- Test: `trading/tests/test_paper_settlement.py`
- Test: `trading/tests/test_backtest_rescore.py`

- **Step 1: Add insertion-order and profile regressions**

```python
@pytest.mark.parametrize("insertion_order", [("live", "research"), ("research", "live")])
def test_entry_sampler_keeps_one_row_per_profile(insertion_order, tmp_path):
    store = PaperStore(tmp_path / "paper.db")
    for profile in insertion_order:
        _record_snapshot(store, profile=profile, probability=0.91 if profile == "live" else 0.71)

    rows = store.sampled_decision_rows(sample_mode="entry-per-market-side")
    assert {(row["risk_profile"], row["probability"]) for row in rows} == {
        ("live", 0.91),
        ("research", 0.71),
    }
```

- **Step 2: Run both SQL and fallback tests and confirm failure**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_paper_settlement.py -k 'sampler and profile' trading/tests/test_backtest_rescore.py`

Expected: FAIL because the sample key omits profile.

- **Step 3: Normalize profile into every sample key**

Use the same key in SQL window partitions and Python fallback:

```python
def _sample_key(row: sqlite3.Row) -> tuple[str, str, str, str]:
    return (
        str(row["target_date"]),
        str(row["market_ticker"]),
        _row_side(row),
        normalize_risk_profile_name(row["risk_profile"] or "live"),
    )
```

SQL must partition by `target_date, market_ticker, UPPER(COALESCE(side,'YES')), COALESCE(risk_profile,'live')`.

- **Step 4: Rescore each configuration from matching rows**

```python
rows_by_profile = {
    name: [row for row in rows if normalize_risk_profile_name(row["risk_profile"] or "live") == name]
    for name in ("live", "research")
}
for name, profile_rows in rows_by_profile.items():
    by_profile[name] = run_rescore(
        profile_rows,
        settlements,
        strategy_config_for_profile(name),
        bankroll=1000.0,
        bootstrap_samples=2000,
        seed=0,
    )
```

Label the result `profile_specific_snapshot_replay`; do not call it source-neutral counterfactual evidence.

- **Step 5: Run focused tests and commit**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_paper_settlement.py trading/tests/test_backtest_rescore.py trading/tests/test_strategy_research.py`

Expected: PASS.

```bash
git add trading/sfo_kalshi_quant/store/scoring.py trading/sfo_kalshi_quant/strategy_lab/calibration.py trading/tests/test_paper_settlement.py trading/tests/test_backtest_rescore.py
git commit -m "fix: isolate profile calibration samples"
```

### Task 4: Correct inside-spread queue priority in runtime and replay

**Files:**
- Modify: `trading/sfo_kalshi_quant/execution.py`
- Modify: `trading/sfo_kalshi_quant/db.py`
- Modify: `trading/sfo_kalshi_quant/replay.py`
- Test: `trading/tests/test_limit_orders.py`
- Test: `trading/tests/test_replay.py`

- **Step 1: Add the exact .72/.73 reproduction**

```python
def test_inside_spread_limit_starts_ahead_of_old_bid_depth():
    assert initial_queue_ahead(limit_price=0.73, visible_bid=0.72, visible_bid_size=100) == 0.0
    assert initial_queue_ahead(limit_price=0.72, visible_bid=0.72, visible_bid_size=100) == 100.0
```

Add an integration assertion that a five-lot public trade at .73 fills a new .73 order rather than decrementing a phantom 100-lot queue.

- **Step 2: Verify the current failure**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_limit_orders.py trading/tests/test_replay.py -k 'inside_spread or queue'`

Expected: FAIL because queue starts at 100.

- **Step 3: Add one shared queue helper**

```python
def initial_queue_ahead(*, limit_price: float, visible_bid: float, visible_bid_size: float) -> float:
    if abs(float(limit_price) - float(visible_bid)) <= 1e-9:
        return max(0.0, float(visible_bid_size))
    if float(limit_price) > float(visible_bid) + 1e-9:
        return 0.0
    return max(0.0, float(visible_bid_size))
```

Call it when writing `queue_remaining` and when constructing `ReplayOrder`. Do not add an unmeasured hidden-queue penalty.

- **Step 4: Run execution, replay, and audit tests**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_limit_orders.py trading/tests/test_maker_fills.py trading/tests/test_replay.py trading/tests/test_audit_2026_07_13.py trading/tests/test_audit_2026_07_14.py`

Expected: PASS.

- **Step 5: Commit Task 4**

```bash
git add trading/sfo_kalshi_quant/execution.py trading/sfo_kalshi_quant/db.py trading/sfo_kalshi_quant/replay.py trading/tests/test_limit_orders.py trading/tests/test_replay.py
git commit -m "fix: model maker queue at the posted price"
```

### Task 5: Make legacy Google accounting exception-safe

**Files:**
- Modify: `forecaster/google_api.py`
- Modify: `forecaster/google_weather_cache.py`
- Create: `forecaster/tests/test_google_api.py`
- Test: `forecaster/tests/test_google_weather_cache.py`

- **Step 1: Add immediate and partial-failure tests**

Add two named tests: `test_refresh_reconciles_zero_dispatch_failure` makes the
transport raise before dispatch and asserts the reserved usage returns to zero;
`test_refresh_counts_dispatched_pages_when_later_page_fails` completes page one,
raises after page two dispatch, and asserts exactly two events remain consumed.

Assert that a pre-dispatch error consumes zero; after one successful/ambiguous dispatch and a second-page failure, usage conservatively consumes both dispatched events.

- **Step 2: Verify current estimated-batch failure**

Run: `./.venv-dev/bin/pytest -q forecaster/tests/test_google_weather_cache.py -k 'failure and usage'`

Expected: FAIL because the estimated reservation is left unchanged.

- **Step 3: Carry dispatch counts through exceptions**

```python
class GoogleFetchError(RuntimeError):
    def __init__(self, message: str, *, dispatched_events: int) -> None:
        super().__init__(message)
        self.dispatched_events = dispatched_events
```

Increment immediately before `urlopen`. Reconcile in `finally` from the returned count or exception count. Treat ambiguous post-dispatch failures as consumed.

- **Step 4: Run forecaster Google tests and commit**

Run: `./.venv-dev/bin/pytest -q forecaster/tests/test_google_api.py forecaster/tests/test_google_weather_cache.py`

Expected: PASS.

```bash
git add forecaster/google_api.py forecaster/google_weather_cache.py forecaster/tests/test_google_api.py forecaster/tests/test_google_weather_cache.py
git commit -m "fix: reconcile Google events on failures"
```

### Task 6: Reliability verification

**Files:**
- Modify only if a regression exposes an in-scope defect.

- **Step 1: Run all focused reliability tests**

Run: `./.venv-dev/bin/pytest -q trading/tests/test_logical_positions.py trading/tests/test_paper_summary.py trading/tests/test_strategy_research.py trading/tests/test_paper_settlement.py trading/tests/test_backtest_rescore.py trading/tests/test_limit_orders.py trading/tests/test_maker_fills.py trading/tests/test_replay.py forecaster/tests/test_google_api.py forecaster/tests/test_google_weather_cache.py`

Expected: PASS.

- **Step 2: Run the full Python suite outside restricted semaphore sandboxes**

Run: `./.venv-dev/bin/pytest -q`

Expected: all tests pass. If only the known semaphore/network-isolation tests fail, rerun those in an environment with semaphore access and installed build dependencies before accepting.

- **Step 3: Run frontend tests and lint**

Run: `bun test && bun run lint`

Expected: 224 frontend tests pass and lint exits zero.

- **Step 4: Reconcile a synthetic multi-lot journal**

Run the new fixture and assert raw-lot and logical realized P&L/capital are identical while all decision counts equal one.

- **Step 5: Record the verification commit if needed**

If verification requires a test-only correction, stage only the exact modified
test and implementation paths and commit them as `test: verify execution and
accounting reliability`. If no correction is needed, do not create an empty
verification commit.
