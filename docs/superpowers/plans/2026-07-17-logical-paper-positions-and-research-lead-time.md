# Logical Paper Positions and Research Lead-Time Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve immutable execution lots while making all decision-level reporting count one logical paper position, isolating the live weekly objective, fixing partial-exit diagnostics, and moving same-day research to shadow-only evidence.

**Architecture:** Add a dependency-light logical-position projection that groups each root order with its `parent_order_id` children, validates the relationship, and exposes aggregate decision fields without rewriting the journal. Reporting consumers use terminal logical positions for counts and outcomes while retaining raw lots for exact P&L, capital, fees, and cash timing. The research entry gate becomes day-ahead-only, while the existing portfolio path continues recording blocked signals and research shadow rows.

**Tech Stack:** Python 3.12, SQLite, pytest, React 19, TypeScript 6, HeroUI Pro DataGrid, Vitest, Vite, bun.

---

## File map

### New files

- `trading/sfo_kalshi_quant/logical_positions.py` — canonical grouping,
  invariant validation, terminality, and aggregate logical-row projection.
- `trading/tests/test_logical_positions.py` — pure unit coverage for grouping,
  aggregation, incomplete partial exits, legacy rows, and corrupt parent data.
- `src/components/strategy/LedgerTable.test.tsx` — browser-DOM unit coverage for
  the multi-fill annotation and compatibility with legacy rows.

### Modified Python files

- `trading/sfo_kalshi_quant/store/scoring.py:21` — decision-level market paper
  summary with lot-level money totals.
- `trading/sfo_kalshi_quant/db.py:2500,2965` — pass executed quantity into
  outcome diagnostics and count distinct terminal roots in the entry breaker.
- `trading/sfo_kalshi_quant/store/diagnostics.py:175` — correct per-contract
  diagnostics for partial exits.
- `trading/sfo_kalshi_quant/posterior_kelly.py:170` — prevent partial exit lots
  from overweighting one decision in the live sizing model.
- `trading/sfo_kalshi_quant/clv.py:210` — report closing-line value and exit
  drag once per logical position.
- `trading/sfo_kalshi_quant/replay.py:640` — reuse the canonical grouping for
  readiness decision cohorts.
- `trading/sfo_kalshi_quant/strategy_lab/paper_card.py:27` — publish one closed
  row per logical position and calculate profile diagnostics from decisions.
- `trading/sfo_kalshi_quant/summary.py:15` — separate lot-timed money from
  logical opening/closing/outcome counts.
- `trading/sfo_kalshi_quant/strategy_lab/build.py:433` — exclude research
  profiles from the live weekly objective.
- `trading/sfo_kalshi_quant/_cli/scan.py:1059` — make actual paper entry
  day-ahead-only for research while preserving shadow evidence.

### Modified Python tests

- `trading/tests/test_paper_settlement.py` — market summary aggregation.
- `trading/tests/test_paper_risk_pause.py` — partial lots cannot satisfy the
  minimum independent-decision sample.
- `trading/tests/test_posterior_kelly.py` — sizing evidence counts one logical
  decision.
- `trading/tests/test_clv.py` — CLV and exit-drag reports collapse partial lots.
- `trading/tests/test_strategy_research.py` — Strategy Lab logical closed rows
  and profile totals.
- `trading/tests/test_paper_summary.py` — exact daily cash timing plus one
  terminal decision outcome.
- `trading/tests/test_audit_2026_07_14.py` — weekly live/research isolation and
  partial-exit diagnostic arithmetic.
- `trading/tests/test_entry_target_gate.py` — research same-day policy.
- `trading/tests/test_research_shadow.py` — blocked same-day signals remain
  shadow evidence and do not become paper positions.

### Modified SPA files

- `src/lib/strategy.ts:3` — optional logical-position and exit-fill fields.
- `src/components/strategy/LedgerTable.tsx:80` — compact `N fills` annotation.

### Design reference

- `docs/superpowers/specs/2026-07-17-logical-paper-positions-and-research-lead-time-design.md`

---

### Task 1: Build the canonical logical-position projection

**Files:**
- Create: `trading/sfo_kalshi_quant/logical_positions.py`
- Create: `trading/tests/test_logical_positions.py`

- [ ] **Step 1: Write pure failing tests for a four-fill terminal position**

Create `trading/tests/test_logical_positions.py` with mapping fixtures that do
not require a database:

```python
from __future__ import annotations

from sfo_kalshi_quant.logical_positions import group_logical_positions


def _row(
    order_id: int,
    *,
    parent_order_id: int | None = None,
    contracts: float = 2.0,
    status: str = "PAPER_CLOSED",
    realized_pnl: float | None = -0.16,
    exit_price: float | None = 0.86,
    closed_at: str | None = "2026-07-17T20:00:00+00:00",
) -> dict[str, object]:
    return {
        "id": order_id,
        "parent_order_id": parent_order_id,
        "created_at": "2026-07-17T19:00:00+00:00",
        "filled_at": "2026-07-17T19:01:00+00:00",
        "target_date": "2026-07-17",
        "market_ticker": "KXHIGHTPHX-26JUL17-T96",
        "label": "97° or above",
        "side": "NO",
        "risk_profile": "live",
        "account_id": "paper-shared",
        "entry_price": 0.93,
        "cost_per_contract": 0.93,
        "contracts": contracts,
        "status": status,
        "realized_pnl": realized_pnl,
        "exit_price": exit_price,
        "exit_fee_per_contract": 0.008,
        "closed_at": closed_at,
        "settled_at": None,
        "edge": 0.048,
        "resolved_yes": 1,
    }


def test_groups_four_exit_lots_into_one_terminal_position() -> None:
    rows = [
        _row(456, closed_at="2026-07-17T20:06:00+00:00"),
        _row(458, parent_order_id=456, closed_at="2026-07-17T20:00:00+00:00"),
        _row(459, parent_order_id=456, closed_at="2026-07-17T20:02:00+00:00"),
        _row(460, parent_order_id=456, closed_at="2026-07-17T20:04:00+00:00"),
    ]

    groups = group_logical_positions(rows)

    assert len(groups) == 1
    group = groups[0]
    assert group.valid is True
    assert group.terminal is True
    assert group.logical_order_id == 456
    assert group.child_order_ids == (458, 459, 460)
    projected = group.as_row()
    assert projected["contracts"] == 8.0
    assert projected["resolved_contracts"] == 8.0
    assert projected["open_contracts"] == 0.0
    assert projected["exit_fill_count"] == 4
    assert projected["realized_pnl"] == -0.64
    assert projected["capital_resolved"] == 7.44
    assert projected["exit_price"] == 0.86
    assert projected["logical_outcome"] == "loss"
```

- [ ] **Step 2: Add failing tests for incomplete, legacy, and corrupt groups**

Append:

```python
def test_partial_exit_remains_open_until_root_is_terminal() -> None:
    root = _row(
        456,
        contracts=6.0,
        status="PAPER_FILLED",
        realized_pnl=None,
        exit_price=None,
        closed_at=None,
    )
    child = _row(458, parent_order_id=456, contracts=2.0)

    group = group_logical_positions([root, child])[0]

    assert group.valid is True
    assert group.terminal is False
    assert group.as_row()["contracts"] == 8.0
    assert group.as_row()["resolved_contracts"] == 2.0
    assert group.as_row()["open_contracts"] == 6.0


def test_legacy_single_row_is_one_logical_position() -> None:
    group = group_logical_positions([_row(7)])[0]

    assert group.valid is True
    assert group.terminal is True
    assert group.logical_order_id == 7
    assert group.child_order_ids == ()


def test_orphan_child_is_retained_but_invalid_and_nonterminal() -> None:
    group = group_logical_positions([_row(8, parent_order_id=999)])[0]

    assert group.valid is False
    assert group.terminal is False
    assert group.as_row()["integrity_findings"] == ["missing root order 999"]


def test_mismatched_child_is_not_silently_merged() -> None:
    root = _row(10)
    child = _row(11, parent_order_id=10)
    child["market_ticker"] = "KXHIGHTNY-26JUL17-T96"

    group = group_logical_positions([root, child])[0]

    assert group.valid is False
    assert "market_ticker mismatch on child 11" in group.integrity_findings
```

- [ ] **Step 3: Run the new tests and confirm the import fails**

Run:

```bash
PYTHONPATH=trading python3 -m pytest trading/tests/test_logical_positions.py -q
```

Expected: collection fails with `ModuleNotFoundError` for
`sfo_kalshi_quant.logical_positions`.

- [ ] **Step 4: Implement the dependency-light projection**

Create `trading/sfo_kalshi_quant/logical_positions.py` with these public
interfaces and aggregation rules:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


TERMINAL_STATUSES = frozenset({"PAPER_CLOSED", "PAPER_SETTLED"})
OPEN_STATUSES = frozenset(
    {"PAPER_FILLED", "PAPER_PARTIALLY_FILLED", "PAPER_PARTIAL_EXPIRED"}
)
_MATCH_FIELDS = (
    "market_ticker",
    "target_date",
    "side",
    "risk_profile",
    "account_id",
)
_NUMERIC_MATCH_FIELDS = ("entry_price", "cost_per_contract")


def _dict_row(row: Mapping[str, Any] | object) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    keys = row.keys()  # type: ignore[attr-defined]
    return {key: row[key] for key in keys}  # type: ignore[index]


def _number(row: Mapping[str, Any], key: str) -> float:
    value = row.get(key)
    return float(value) if value is not None else 0.0


def _resolved_at(row: Mapping[str, Any]) -> str | None:
    value = row.get("closed_at") or row.get("settled_at")
    return str(value) if value else None


@dataclass(frozen=True)
class LogicalPaperPosition:
    logical_order_id: int
    root: dict[str, Any]
    lots: tuple[dict[str, Any], ...]
    integrity_findings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.integrity_findings

    @property
    def terminal(self) -> bool:
        return (
            self.valid
            and self.root.get("parent_order_id") in (None, 0)
            and str(self.root.get("status")) in TERMINAL_STATUSES
            and self.root.get("realized_pnl") is not None
        )

    @property
    def child_order_ids(self) -> tuple[int, ...]:
        return tuple(
            int(row["id"])
            for row in self.lots
            if int(row["id"]) != self.logical_order_id
        )

    @property
    def resolved_lots(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            row
            for row in self.lots
            if str(row.get("status")) in TERMINAL_STATUSES
            and row.get("realized_pnl") is not None
        )

    @property
    def won(self) -> bool | None:
        pnl = sum(_number(row, "realized_pnl") for row in self.resolved_lots)
        if not self.terminal or abs(pnl) <= 1e-9:
            return None
        return pnl > 0.0

    def as_row(self) -> dict[str, Any]:
        row = dict(self.root)
        resolved = self.resolved_lots
        total_contracts = sum(_number(lot, "contracts") for lot in self.lots)
        resolved_contracts = sum(_number(lot, "contracts") for lot in resolved)
        realized_pnl = sum(_number(lot, "realized_pnl") for lot in resolved)
        capital = sum(
            _number(lot, "contracts") * _number(lot, "cost_per_contract")
            for lot in resolved
        )
        exits = [
            lot
            for lot in resolved
            if lot.get("exit_price") is not None and _number(lot, "contracts") > 0
        ]
        exit_contracts = sum(_number(lot, "contracts") for lot in exits)
        weighted_exit = (
            sum(_number(lot, "contracts") * _number(lot, "exit_price") for lot in exits)
            / exit_contracts
            if exit_contracts > 0
            else None
        )
        weighted_exit_fee = (
            sum(
                _number(lot, "contracts") * _number(lot, "exit_fee_per_contract")
                for lot in exits
            )
            / exit_contracts
            if exit_contracts > 0
            else None
        )
        resolution_times = [value for lot in resolved if (value := _resolved_at(lot))]
        row.update(
            logical_order_id=self.logical_order_id,
            parent_order_id=None,
            child_order_ids=list(self.child_order_ids),
            integrity_findings=list(self.integrity_findings),
            contracts=total_contracts,
            resolved_contracts=resolved_contracts,
            open_contracts=(0.0 if self.terminal else _number(self.root, "contracts")),
            resolved_lot_count=len(resolved),
            exit_fill_count=len(exits),
            realized_pnl=realized_pnl if resolved else None,
            capital_resolved=capital,
            exit_price=weighted_exit,
            exit_fee_per_contract=weighted_exit_fee,
            latest_resolved_at=max(resolution_times) if resolution_times else None,
            logical_outcome=(
                "win" if self.won is True else "loss" if self.won is False else "undecided"
            ),
        )
        return row


def _group_findings(
    root: Mapping[str, Any],
    children: list[dict[str, Any]],
    by_id: Mapping[int, Mapping[str, Any]],
) -> tuple[str, ...]:
    findings: list[str] = []
    root_id = int(root["id"])
    if root.get("parent_order_id") not in (None, 0):
        findings.append(f"order {root_id} is not a root")
    for child in children:
        child_id = int(child["id"])
        if int(child.get("parent_order_id") or 0) != root_id:
            findings.append(f"child {child_id} does not reference root {root_id}")
        parent = by_id.get(int(child.get("parent_order_id") or 0))
        if parent is not None and parent.get("parent_order_id") not in (None, 0):
            findings.append(f"child {child_id} references another child")
        for field in _MATCH_FIELDS:
            if child.get(field) != root.get(field):
                findings.append(f"{field} mismatch on child {child_id}")
        for field in _NUMERIC_MATCH_FIELDS:
            if abs(_number(child, field) - _number(root, field)) > 1e-9:
                findings.append(f"{field} mismatch on child {child_id}")
    return tuple(dict.fromkeys(findings))


def group_logical_positions(
    rows: Iterable[Mapping[str, Any] | object],
) -> list[LogicalPaperPosition]:
    materialized = [_dict_row(row) for row in rows]
    by_id = {int(row["id"]): row for row in materialized}
    children_by_parent: dict[int, list[dict[str, Any]]] = {}
    for row in materialized:
        parent_id = row.get("parent_order_id")
        if parent_id not in (None, 0):
            children_by_parent.setdefault(int(parent_id), []).append(row)

    groups: list[LogicalPaperPosition] = []
    consumed: set[int] = set()
    for row in materialized:
        if row.get("parent_order_id") not in (None, 0):
            continue
        root_id = int(row["id"])
        children = sorted(children_by_parent.get(root_id, []), key=lambda item: int(item["id"]))
        findings = _group_findings(row, children, by_id)
        lots = (row, *children)
        groups.append(LogicalPaperPosition(root_id, row, lots, findings))
        consumed.update(int(lot["id"]) for lot in lots)

    for row in materialized:
        order_id = int(row["id"])
        if order_id in consumed:
            continue
        parent_id = int(row.get("parent_order_id") or 0)
        groups.append(
            LogicalPaperPosition(
                parent_id or order_id,
                row,
                (row,),
                (f"missing root order {parent_id}",),
            )
        )
    return sorted(groups, key=lambda group: group.logical_order_id)
```

- [ ] **Step 5: Run the pure projection tests**

Run:

```bash
PYTHONPATH=trading python3 -m pytest trading/tests/test_logical_positions.py -q
```

Expected: `5 passed`.

- [ ] **Step 6: Commit the projection**

```bash
git add trading/sfo_kalshi_quant/logical_positions.py trading/tests/test_logical_positions.py
git commit -m "feat: project execution lots into logical positions"
```

---

### Task 2: Correct market summary and circuit-breaker decision counts

**Files:**
- Modify: `trading/sfo_kalshi_quant/store/scoring.py:21-86`
- Modify: `trading/sfo_kalshi_quant/db.py:2965-3045`
- Modify: `trading/sfo_kalshi_quant/replay.py:640-675`
- Modify: `trading/tests/test_paper_settlement.py`
- Modify: `trading/tests/test_paper_risk_pause.py`

- [ ] **Step 1: Add a failing market-summary regression**

Append to `trading/tests/test_paper_settlement.py`:

```python
def test_market_summary_counts_partial_exit_lots_as_one_trade():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B66.5",
            label="66° to 67°",
            action="BUY_YES",
            approved=True,
            probability=0.70,
            probability_lcb=0.60,
            yes_bid=0.48,
            yes_ask=0.50,
            spread=0.02,
            fee_per_contract=0.02,
            cost_per_contract=0.52,
            edge=0.18,
            edge_lcb=0.08,
            kelly_fraction=0.01,
            recommended_contracts=4.0,
            expected_profit=0.72,
            reasons=[],
            side="YES",
            entry_bid=0.48,
            entry_ask=0.50,
            entry_bid_size=10.0,
            entry_ask_size=10.0,
            strike_type="between",
            floor_strike=66.0,
            cap_strike=67.0,
        )
        order_id = store.record_paper_order(
            "2026-07-17",
            decision,
            risk_profile="live",
        )
        store.close_paper_order(order_id, 0.20, max_quantity=1.0)
        store.close_paper_order(order_id, 0.20, max_quantity=1.0)
        store.close_paper_order(order_id, 0.20)

        with store.connect() as conn:
            raw_pnl, raw_capital = conn.execute(
                "SELECT SUM(realized_pnl), SUM(contracts * cost_per_contract) "
                "FROM paper_orders WHERE id=? OR parent_order_id=?",
                (order_id, order_id),
            ).fetchone()

        summary = store.market_backtest_summary()

        assert summary["orders"] == 1.0
        assert summary["losses"] == 1.0
        assert summary["wins"] == 0.0
        assert summary["contracts"] == 4.0
        assert summary["realized_pnl"] == raw_pnl
        assert summary["capital_at_risk"] == raw_capital
```

- [ ] **Step 2: Add a failing circuit-breaker regression**

Append to `trading/tests/test_paper_risk_pause.py`:

```python
def test_partial_exit_lots_do_not_satisfy_resolved_trade_minimum():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        order_id = store.record_paper_order(
            "2026-07-17",
            _decision(),
            risk_profile="research",
        )
        for _ in range(4):
            store.close_paper_order(order_id, 0.01, max_quantity=1.0)

        reason = store.paper_entry_pause_reason(
            "research",
            bankroll=1000.0,
            target_date="2026-07-18",
            min_resolved_trades=5,
            max_resolved_roi=0.0,
            daily_loss_pct=1.0,
        )

        assert reason is None
```

The root still has six open contracts, so four realized child lots must count
as zero terminal decisions rather than four trades.

- [ ] **Step 3: Run both focused tests and verify raw-row count failures**

Run:

```bash
PYTHONPATH=trading python3 -m pytest \
  trading/tests/test_paper_settlement.py::test_market_summary_counts_partial_exit_lots_as_one_trade \
  trading/tests/test_paper_risk_pause.py::test_partial_exit_lots_do_not_satisfy_resolved_trade_minimum \
  -q
```

Expected: the summary reports three rows and/or the breaker counts partial lots
toward its threshold.

- [ ] **Step 4: Make `market_backtest_summary` use logical outcomes**

Import `group_logical_positions` and replace raw-row counts with:

```python
groups = group_logical_positions(rows)
valid_groups = [group for group in groups if group.valid]
terminal = [group for group in valid_groups if group.terminal]
realized_lots = [
    row
    for row in rows
    if row["realized_pnl"] is not None and row["status"] != "PAPER_EXPIRED"
]
open_roots = [
    group.root
    for group in valid_groups
    if not group.terminal
    and str(group.root.get("status")) in {
        "PAPER_FILLED",
        "PAPER_PARTIALLY_FILLED",
        "PAPER_PARTIAL_EXPIRED",
    }
    and group.root.get("realized_pnl") is None
]
contracts = sum(float(row["contracts"]) for row in realized_lots)
capital = sum(
    float(row["contracts"]) * float(row["cost_per_contract"])
    for row in realized_lots
)
pnl = sum(float(row["realized_pnl"]) for row in realized_lots)
decided = [group for group in terminal if group.won is not None]
wins = sum(group.won is True for group in decided)
losses = sum(group.won is False for group in decided)
open_capital = sum(
    float(row["contracts"]) * float(row["cost_per_contract"])
    for row in open_roots
)
return {
    "orders": float(len(terminal)),
    "contracts": contracts,
    "capital_at_risk": capital,
    "realized_pnl": pnl,
    "roi": pnl / capital if capital else 0.0,
    "hit_rate": wins / len(decided) if decided else 0.0,
    "wins": float(wins),
    "losses": float(losses),
    "avg_edge": (
        sum(float(group.root.get("edge") or 0.0) for group in terminal) / len(terminal)
        if terminal
        else 0.0
    ),
    "open_orders": float(len(open_roots)),
    "open_capital_at_risk": open_capital,
}
```

Do not use the old `if not realized_rows` early return; a partially exited open
root can have realized cash but zero resolved decisions.

- [ ] **Step 5: Make the breaker count terminal roots while summing lots**

Replace the resolved query in `paper_entry_pause_reason` with:

```sql
SELECT
    COUNT(DISTINCT root.id) AS trades,
    COALESCE(SUM(lot.realized_pnl), 0) AS pnl,
    COALESCE(SUM(lot.contracts * lot.cost_per_contract), 0) AS capital
FROM paper_orders AS lot
JOIN paper_orders AS root
  ON root.id = COALESCE(lot.parent_order_id, lot.id)
WHERE lot.realized_pnl IS NOT NULL
  AND lot.status != 'REJECTED'
  AND lot.status != 'PAPER_EXPIRED'
  AND root.status IN ('PAPER_SETTLED', 'PAPER_CLOSED')
  AND root.realized_pnl IS NOT NULL
  AND COALESCE(root.risk_profile, 'live') = ?
  AND COALESCE(lot.closed_at, lot.settled_at) >= ?
```

Keep the daily-loss query lot-based and unchanged.

- [ ] **Step 6: Reuse the canonical groups in readiness replay**

Import `group_logical_positions` and replace
`_verified_resolved_decision_groups` with:

```python
def _verified_resolved_decision_groups(
    orders: list[sqlite3.Row],
    verified_order_ids: set[int],
) -> dict[int, list[sqlite3.Row]]:
    verified: dict[int, list[sqlite3.Row]] = {}
    for group in group_logical_positions(orders):
        if not group.terminal:
            continue
        lots = list(group.resolved_lots)
        if lots and all(int(row["id"]) in verified_order_ids for row in lots):
            verified[group.logical_order_id] = lots
    return verified
```

The existing readiness regression in
`test_audit_2026_07_14.py::test_readiness_aggregates_verified_partial_close_lots_into_one_decision`
must keep passing.

- [ ] **Step 7: Run the focused and neighboring suites**

```bash
PYTHONPATH=trading python3 -m pytest \
  trading/tests/test_logical_positions.py \
  trading/tests/test_paper_settlement.py \
  trading/tests/test_paper_risk_pause.py \
  trading/tests/test_audit_2026_07_14.py::test_readiness_aggregates_verified_partial_close_lots_into_one_decision \
  -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit summary and breaker corrections**

```bash
git add trading/sfo_kalshi_quant/store/scoring.py trading/sfo_kalshi_quant/db.py \
  trading/sfo_kalshi_quant/replay.py \
  trading/tests/test_paper_settlement.py trading/tests/test_paper_risk_pause.py
git commit -m "fix: count partial exits as one paper decision"
```

---

### Task 3: Prevent partial lots from biasing sizing and CLV evidence

**Files:**
- Modify: `trading/sfo_kalshi_quant/posterior_kelly.py:170-220`
- Modify: `trading/sfo_kalshi_quant/clv.py:210-265`
- Modify: `trading/tests/test_posterior_kelly.py`
- Modify: `trading/tests/test_clv.py`

- [ ] **Step 1: Extend the posterior fixture schema with logical-order fields**

Replace the `CREATE TABLE` in `_seed_orders` with a backward-compatible test
schema that still accepts the existing insert column list:

```python
conn.execute(
    "CREATE TABLE paper_orders ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, parent_order_id INTEGER, "
    "market_ticker TEXT DEFAULT 'KXHIGHTSFO-TEST', "
    "side TEXT, probability REAL, cost_per_contract REAL, "
    "resolved_yes INTEGER, settlement_high_f REAL, settled_at TEXT, status TEXT, "
    "target_date TEXT, strike_type TEXT, floor_strike REAL, cap_strike REAL, "
    "risk_profile TEXT DEFAULT 'live', account_id TEXT DEFAULT 'paper-shared', "
    "entry_price REAL DEFAULT 0.85, contracts REAL DEFAULT 1.0, "
    "realized_pnl REAL DEFAULT 0.0, closed_at TEXT, exit_price REAL, "
    "exit_fee_per_contract REAL DEFAULT 0.0)"
)
```

- [ ] **Step 2: Add a failing posterior-Kelly decision-count test**

Append to `trading/tests/test_posterior_kelly.py`:

```python
def test_partial_close_children_count_once_in_posterior_model():
    conn = sqlite3.connect(":memory:")
    _seed_orders(
        conn,
        [
            (
                "NO", 0.92, 0.85, 0, 66.0, "t", "PAPER_SETTLED",
                "2026-06-15", "less", None, 70.0,
            ),
            (
                "YES", 0.70, 0.55, None, None, None, "PAPER_CLOSED",
                "2026-06-15", "range", 70.0, 72.0,
            ),
        ],
    )
    root_id = conn.execute("SELECT MAX(id) FROM paper_orders").fetchone()[0]
    for _ in range(2):
        conn.execute(
            "INSERT INTO paper_orders (parent_order_id, market_ticker, side, probability, "
            "cost_per_contract, status, target_date, strike_type, floor_strike, "
            "cap_strike, contracts, realized_pnl, closed_at, exit_price) "
            "SELECT ?, market_ticker, side, probability, cost_per_contract, status, "
            "target_date, strike_type, floor_strike, cap_strike, 1.0, -0.55, 't', 0.01 "
            "FROM paper_orders WHERE id=?",
            (root_id, root_id),
        )
    conn.commit()

    model = load_posterior_kelly_model(
        conn,
        include_counterfactual_closed=True,
        prior_strength=20.0,
        floor=0.2,
        min_cohort_n=8,
    )

    assert model.overall.n == 2
```

One settled anchor plus one closed decision must produce two observations, not
four rows.

- [ ] **Step 3: Add a failing CLV logical-position test**

Add the needed imports and append to `trading/tests/test_clv.py`:

```python
def test_load_order_clv_collapses_partial_exit_children():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B66.5",
            label="66° to 67°",
            action="BUY_YES",
            approved=True,
            probability=0.70,
            probability_lcb=0.60,
            yes_bid=0.48,
            yes_ask=0.50,
            spread=0.02,
            fee_per_contract=0.02,
            cost_per_contract=0.52,
            edge=0.18,
            edge_lcb=0.08,
            kelly_fraction=0.01,
            recommended_contracts=4.0,
            expected_profit=0.72,
            reasons=[],
            side="YES",
            entry_bid=0.48,
            entry_ask=0.50,
            entry_bid_size=10.0,
            entry_ask_size=10.0,
            strike_type="between",
            floor_strike=66.0,
            cap_strike=67.0,
        )
        order_id = store.record_paper_order("2026-07-17", decision, risk_profile="live")
        store.close_paper_order(order_id, 0.40, max_quantity=1.0)
        store.close_paper_order(order_id, 0.30, max_quantity=1.0)
        store.close_paper_order(order_id, 0.20)
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET settlement_high_f=67 WHERE id=?",
                (order_id,),
            )
            records = load_order_clv(conn)

        assert len(records) == 1
        assert records[0].order_id == order_id
        assert records[0].contracts == 4.0
        assert records[0].realized_pnl == approx(
            store.market_backtest_summary()["realized_pnl"]
        )
```

Import `Path`, `TemporaryDirectory`, `PaperStore`, `TradeDecision`, and
`load_order_clv`.

- [ ] **Step 4: Run both focused tests and confirm inflated evidence counts**

```bash
PYTHONPATH=trading python3 -m pytest \
  trading/tests/test_posterior_kelly.py::test_partial_close_children_count_once_in_posterior_model \
  trading/tests/test_clv.py::test_load_order_clv_collapses_partial_exit_children \
  -q
```

Expected: posterior `n` and CLV record count include each child row.

- [ ] **Step 5: Build posterior evidence from terminal logical roots**

In `load_posterior_kelly_model`, set `conn.row_factory = sqlite3.Row`, fetch all
paper rows, and replace both raw SQL outcome loops with:

```python
paper_rows = conn.execute(
    "SELECT * FROM paper_orders WHERE status != 'REJECTED' ORDER BY id"
).fetchall()
positions = [
    group.as_row()
    for group in group_logical_positions(paper_rows)
    if group.terminal
]
rows: list[tuple[float, float, bool, float]] = []
highs = _date_settlement_highs(conn)
for position in positions:
    side = str(position.get("side") or "YES").upper()
    claimed = float(position.get("probability") or 0.0)
    cost = float(position.get("cost_per_contract") or 0.0)
    status = str(position.get("status"))
    if (
        status == "PAPER_SETTLED"
        and position.get("resolved_yes") is not None
        and position.get("settlement_high_f") is not None
    ):
        resolved_yes = int(position["resolved_yes"])
        won = resolved_yes == 1 if side == "YES" else resolved_yes == 0
        rows.append((claimed, cost, won, float(position["settlement_high_f"])))
        continue
    if include_counterfactual_closed and status == "PAPER_CLOSED":
        ticker = str(position.get("market_ticker") or "KXHIGHTSFO")
        target_date = str(position.get("target_date"))
        key = settlement_key_for_market(ticker, target_date)
        high = highs.get(key) if key is not None else None
        if high is None:
            continue
        won = side_won(
            side,
            position.get("strike_type"),
            position.get("floor_strike"),
            position.get("cap_strike"),
            high,
        )
        rows.append((claimed, cost, won, high))
```

Import `group_logical_positions`. Preserve `_accumulate`, priors, floors, and
cohort calculations unchanged.

- [ ] **Step 6: Build CLV records from aggregate logical rows**

In `load_order_clv`, fetch `SELECT *`, group, and iterate valid logical rows:

```python
conn.row_factory = sqlite3.Row
paper_rows = conn.execute(
    "SELECT * FROM paper_orders WHERE status != 'REJECTED' ORDER BY id"
).fetchall()
positions = [
    group.as_row()
    for group in group_logical_positions(paper_rows)
    if group.valid
]
records: list[OrderCLV] = []
for row in positions:
    target_date = str(row["target_date"])
    high = highs.get(target_date)
    contracts = float(row.get("contracts") or 0.0)
    cost = float(row.get("cost_per_contract") or 0.0)
    won = None
    counterfactual = None
    cohort = None
    if high is not None:
        won = side_won(
            str(row.get("side") or "YES"),
            row.get("strike_type"),
            row.get("floor_strike"),
            row.get("cap_strike"),
            high,
        )
        counterfactual = counterfactual_pnl(contracts, cost, won)
        cohort = temperature_cohort(high)
    root_id = int(row.get("logical_order_id") or row["id"])
    records.append(
        OrderCLV(
            order_id=root_id,
            target_date=target_date,
            status=str(row["status"]),
            side=str(row.get("side") or "YES"),
            risk_profile=row.get("risk_profile"),
            contracts=contracts,
            entry_cost=cost,
            realized_pnl=(
                None if row.get("realized_pnl") is None else float(row["realized_pnl"])
            ),
            closing_mark=marks.get(root_id),
            settlement_high_f=high,
            cohort=cohort,
            won=won,
            counterfactual_hold_pnl=counterfactual,
        )
    )
return records
```

Import `group_logical_positions`.

- [ ] **Step 7: Run sizing, risk, and CLV suites**

```bash
PYTHONPATH=trading python3 -m pytest \
  trading/tests/test_posterior_kelly.py \
  trading/tests/test_posterior_kelly_sizing.py \
  trading/tests/test_clv.py \
  trading/tests/test_strategy_gates.py \
  -q
```

Expected: all tests pass with one sizing/CLV observation per decision.

- [ ] **Step 8: Commit the sizing and diagnostic correction**

```bash
git add trading/sfo_kalshi_quant/posterior_kelly.py trading/sfo_kalshi_quant/clv.py \
  trading/tests/test_posterior_kelly.py trading/tests/test_clv.py
git commit -m "fix: dedupe partial exits in sizing evidence"
```

---

### Task 4: Publish logical positions in Strategy Lab

**Files:**
- Modify: `trading/sfo_kalshi_quant/strategy_lab/paper_card.py:27-430,762-990`
- Modify: `trading/tests/test_strategy_research.py`

- [ ] **Step 1: Add a failing Strategy Lab closed-ledger test**

Add a test using the existing forecaster fixtures and `PaperStore` setup in
`trading/tests/test_strategy_research.py`:

```python
def test_strategy_research_collapses_partial_exit_lots_into_one_closed_position():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)
        _write_settlement(root)
        store = PaperStore(db_path)
        decision = replace(_approved_decision(), recommended_contracts=4.0)
        order_id = store.record_paper_order(
            "2026-06-03", decision, risk_profile="live"
        )
        store.close_paper_order(order_id, 0.20, max_quantity=1.0)
        store.close_paper_order(order_id, 0.30, max_quantity=1.0)
        store.close_paper_order(order_id, 0.40)

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        closed = payload["paper_trading"]["closed_positions"]
        assert len(closed) == 1
        assert closed[0]["logical_order_id"] == order_id
        assert closed[0]["contracts"] == 4.0
        assert closed[0]["exit_fill_count"] == 3
        assert closed[0]["child_order_ids"]
        assert payload["paper_trading"]["summary"]["closed_positions"] == 1
        live = next(row for row in payload["profiles"] if row["risk_profile"] == "live")
        assert live["paper_trading"]["summary"]["closed_positions"] == 1
```

Import `replace` from `dataclasses` if the test module does not already import
it.

- [ ] **Step 2: Run the focused test and confirm duplicate rows**

```bash
PYTHONPATH=trading python3 -m pytest \
  trading/tests/test_strategy_research.py::test_strategy_research_collapses_partial_exit_lots_into_one_closed_position \
  -q
```

Expected: `closed_positions` contains three rows and the summary count is three.

- [ ] **Step 3: Materialize all groups before applying the recent-row limit**

In `_paper_payload`, fetch all non-rejected order rows once, group them, and
derive logical closed rows before slicing:

```python
all_order_rows = conn.execute(
    "SELECT * FROM paper_orders WHERE status != 'REJECTED' ORDER BY id"
).fetchall()
logical_groups = group_logical_positions(all_order_rows)
invalid_groups = [group for group in logical_groups if not group.valid]
closed_rows = sorted(
    (group.as_row() for group in logical_groups if group.terminal),
    key=lambda row: str(
        row.get("closed_at")
        or row.get("settled_at")
        or row.get("created_at")
        or ""
    ),
    reverse=True,
)[:30]
```

Import `group_logical_positions`. Remove the old raw `closed_rows LIMIT 30`
query and the raw resolved profile aggregate query.

- [ ] **Step 4: Add a logical profile-summary helper**

Add:

```python
def _logical_profile_summaries(
    groups,
    open_rows: list[sqlite3.Row],
    pending_rows: list[sqlite3.Row],
) -> list[dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}

    def bucket(name: str) -> dict[str, Any]:
        return profiles.setdefault(
            name,
            {
                "risk_profile": name,
                "orders": 0,
                "resolved": 0,
                "wins": 0,
                "losses": 0,
                "realized_pnl": 0.0,
                "capital_resolved": 0.0,
                "open_positions": 0,
                "open_risk": 0.0,
                "pending_limit_orders": 0,
                "pending_limit_risk": 0.0,
            },
        )

    for group in groups:
        if not group.terminal:
            continue
        row = group.as_row()
        stats = bucket(str(row.get("risk_profile") or "unknown"))
        stats["orders"] += 1
        stats["resolved"] += 1
        stats["realized_pnl"] += float(row.get("realized_pnl") or 0.0)
        stats["capital_resolved"] += float(row.get("capital_resolved") or 0.0)
        stats["wins"] += group.won is True
        stats["losses"] += group.won is False
    for row in open_rows:
        stats = bucket(_row_risk_profile(row) or "unknown")
        stats["open_positions"] += 1
        stats["open_risk"] += _to_float(row["contracts"]) * _to_float(row["cost_per_contract"])
    for row in pending_rows:
        stats = bucket(_row_risk_profile(row) or "unknown")
        stats["pending_limit_orders"] += 1
        stats["pending_limit_risk"] += _to_float(row["reserved_cost"])
    return [_profile_summary_mapping(row) for _, row in sorted(profiles.items())]
```

Add this mapping formatter and use it before `_profiles_with_scanners`:

```python
def _profile_summary_mapping(row: Mapping[str, Any]) -> dict[str, Any]:
    resolved = int(row.get("resolved") or 0)
    wins = int(row.get("wins") or 0)
    losses = int(row.get("losses") or 0)
    pnl = _to_float(row.get("realized_pnl"))
    capital = _to_float(row.get("capital_resolved"))
    return {
        "risk_profile": str(row.get("risk_profile") or "unknown"),
        "orders": int(row.get("orders") or 0),
        "resolved": resolved,
        "wins": wins,
        "losses": losses,
        "hit_rate": _round(wins / (wins + losses), 4) if (wins + losses) else None,
        "realized_pnl": _round(pnl, 2),
        "roi": _round(pnl / capital, 4) if capital > 0 else None,
        "open_positions": int(row.get("open_positions") or 0),
        "open_risk": _round(_to_float(row.get("open_risk")), 2),
        "pending_limit_orders": int(row.get("pending_limit_orders") or 0),
        "pending_limit_risk": _round(
            _to_float(row.get("pending_limit_risk")), 2
        ),
    }
```

- [ ] **Step 5: Make diagnostics operate on aggregate logical rows**

Replace `_paper_diagnostics` with a group-backed implementation and update
helper type hints from `sqlite3.Row` to `Mapping[str, Any]`:

```python
def _paper_diagnostics(db_path: Path) -> dict[str, Any]:
    if not db_path.exists() or not _db_table_exists(db_path, "paper_orders"):
        return _empty_paper_diagnostics()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM paper_orders WHERE status != 'REJECTED'"
        ).fetchall()
    resolved = [
        group.as_row()
        for group in group_logical_positions(rows)
        if group.terminal
    ]
    return {
        "resolved_positions": len(resolved),
        "by_profile": _paper_group_diagnostics(
            resolved, lambda row: _row_risk_profile(row) or "unknown"
        ),
        "by_side": _paper_group_diagnostics(resolved, _side_from_row),
        "by_exit_reason": _paper_group_diagnostics(resolved, _paper_exit_reason),
        "worst_segments": _worst_paper_segments(resolved),
    }
```

The existing `_paper_order_won` and `_paper_order_decided` helpers must first
read `logical_outcome` when present:

```python
outcome = _sqlite_row_value(row, "logical_outcome")
if outcome in {"win", "loss"}:
    return outcome == "win"
```

Publish integrity status alongside the summary:

```python
"logical_position_integrity": {
    "valid": not invalid_groups,
    "invalid_groups": [
        {
            "logical_order_id": group.logical_order_id,
            "findings": list(group.integrity_findings),
        }
        for group in invalid_groups
    ],
},
```

- [ ] **Step 6: Make `_paper_row` accept the projected mapping fields**

Change its type to `Mapping[str, Any]`, keep existing fields, and add:

```python
"logical_order_id": _sqlite_row_value(row, "logical_order_id", row["id"]),
"child_order_ids": list(_sqlite_row_value(row, "child_order_ids", []) or []),
"exit_fill_count": int(_sqlite_row_value(row, "exit_fill_count", 1) or 0),
"integrity_findings": list(_sqlite_row_value(row, "integrity_findings", []) or []),
```

Use `capital_resolved` as the realized ROI denominator when present; otherwise
retain `contracts * cost_per_contract` for legacy and open rows.

- [ ] **Step 7: Run Strategy Lab and structure tests**

```bash
PYTHONPATH=trading python3 -m pytest \
  trading/tests/test_strategy_research.py \
  trading/tests/test_strategy_lab_structure.py \
  -q
```

Expected: all tests pass, including the new one-row ledger assertion.

- [ ] **Step 8: Commit Strategy Lab logical publication**

```bash
git add trading/sfo_kalshi_quant/strategy_lab/paper_card.py \
  trading/tests/test_strategy_research.py
git commit -m "fix: publish one row per logical paper position"
```

---

### Task 5: Separate daily cash timing from logical trade outcomes

**Files:**
- Modify: `trading/sfo_kalshi_quant/summary.py:15-230,313-470,835-850`
- Modify: `trading/tests/test_paper_summary.py`

- [ ] **Step 1: Add a failing cross-day partial-exit summary test**

Append to `trading/tests/test_paper_summary.py`:

```python
def test_paper_summary_keeps_lot_day_pnl_but_counts_one_terminal_trade():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        forecaster_root = Path(tmp) / "forecaster"
        forecaster_root.mkdir()
        store = PaperStore(db_path)
        local_now = _now_local()
        target = local_now.date().isoformat()
        order_id = store.record_paper_order(
            target,
            replace(_decision("KXHIGHTSFO-TEST-B66.5"), recommended_contracts=4.0),
            risk_profile="live",
        )
        child = store.close_paper_order(order_id, 0.40, max_quantity=2.0)
        root = store.close_paper_order(order_id, 0.20)
        prior_day = (local_now - timedelta(days=1)).astimezone(UTC).isoformat()
        final_day = local_now.astimezone(UTC).isoformat()
        with store.connect() as conn:
            conn.execute("UPDATE paper_orders SET closed_at=? WHERE id=?", (prior_day, child["id"]))
            conn.execute("UPDATE paper_orders SET closed_at=? WHERE id=?", (final_day, root["id"]))

        payload = build_paper_summary(
            db_path=db_path,
            forecaster_root=forecaster_root,
            config=StrategyConfig(paper_bankroll=1000.0),
            days=7,
            now=local_now.astimezone(UTC),
        )

        assert payload["totals"]["trades_opened"] == 1
        assert payload["totals"]["trades_closed"] == 1
        assert payload["totals"]["losses"] == 1
        assert len(payload["biggest_losers"]) == 1
        assert payload["biggest_losers"][0]["contracts"] == 4.0
        rows = {row["date"]: row for row in payload["days"]}
        assert rows[(local_now.date() - timedelta(days=1)).isoformat()]["realized_pnl"] != 0
        assert rows[local_now.date().isoformat()]["closed"] == 1
```

Import `replace` from `dataclasses`.

- [ ] **Step 2: Run the focused test and confirm duplicate opening/outcome counts**

```bash
PYTHONPATH=trading python3 -m pytest \
  trading/tests/test_paper_summary.py::test_paper_summary_keeps_lot_day_pnl_but_counts_one_terminal_trade \
  -q
```

Expected: two openings and/or two closed trades are reported.

- [ ] **Step 3: Load parent metadata and build both views**

Add these fields in `_load_orders`:

```python
"parent_order_id": _row_value(row, "parent_order_id"),
"exit_price": _row_value(row, "exit_price"),
"exit_fee_per_contract": _row_value(row, "exit_fee_per_contract"),
"account_id": _row_value(row, "account_id"),
```

At the top of `build_paper_summary`, derive:

```python
orders = _load_orders(db_path)
logical_groups = group_logical_positions(orders)
valid_groups = [group for group in logical_groups if group.valid]
logical_positions = [group.as_row() for group in valid_groups]
terminal_positions = [group.as_row() for group in valid_groups if group.terminal]
open_orders = [
    group.root
    for group in valid_groups
    if not group.terminal
    and str(group.root.get("status")) in {
        "PAPER_FILLED",
        "PAPER_PARTIALLY_FILLED",
        "PAPER_PARTIAL_EXPIRED",
    }
    and group.root.get("realized_pnl") is None
]
```

- [ ] **Step 4: Split the existing combined loop into three explicit loops**

Use one aggregate logical row per root for openings so a root that was later
shrunk by partial exits retains its original filled quantity:

```python
for order in logical_positions:
    opened_at = order.get("filled_at")
    if opened_at is None and str(order.get("status")) in {
        "PAPER_FILLED",
        "PAPER_PARTIALLY_FILLED",
        "PAPER_PARTIAL_EXPIRED",
        "PAPER_SETTLED",
        "PAPER_CLOSED",
    }:
        opened_at = order.get("created_at")
    opened_day = _local_day(opened_at) if opened_at else None
    if opened_day in per_day and str(order.get("status")) != "PAPER_EXPIRED":
        day = per_day[opened_day]
        day["opened"] += 1
        spend = float(order["contracts"]) * float(order["cost_per_contract"])
        day["opened_spend"] += spend
        profile = str(order.get("risk_profile") or "unknown")
        profile_day = _day_profile(day, profile)
        profile_day["opened"] += 1
        profile_day["opened_spend"] += spend
```

Use execution lots for exact money timing:

```python
for lot in orders:
    resolved_at = lot.get("closed_at") or lot.get("settled_at")
    resolved_day = _local_day(resolved_at) if resolved_at else None
    pnl = lot.get("realized_pnl")
    if pnl is None or lot.get("status") == "PAPER_EXPIRED":
        continue
    total_realized_all_time += float(pnl)
    if resolved_day is not None and resolved_day < window_start.isoformat():
        realized_before_window += float(pnl)
    if resolved_day in per_day:
        spend = float(lot["contracts"]) * float(lot["cost_per_contract"])
        day = per_day[resolved_day]
        day["realized_pnl"] += float(pnl)
        day["resolved_spend"] += spend
        profile_day = _day_profile(day, str(lot.get("risk_profile") or "unknown"))
        profile_day["realized_pnl"] += float(pnl)
        profile_day["resolved_spend"] += spend
```

Use terminal logical rows for decision outcomes:

```python
for position in terminal_positions:
    resolved_at = position.get("closed_at") or position.get("settled_at")
    resolved_day = _local_day(resolved_at) if resolved_at else None
    if resolved_day not in per_day:
        continue
    day = per_day[resolved_day]
    profile_day = _day_profile(day, str(position.get("risk_profile") or "unknown"))
    if position.get("closed_at"):
        day["closed"] += 1
        profile_day["closed"] += 1
    else:
        day["settled"] += 1
        profile_day["settled"] += 1
    profile_day["resolved"] += 1
    if position.get("logical_outcome") == "win":
        day["wins"] += 1
        profile_day["wins"] += 1
    elif position.get("logical_outcome") == "loss":
        day["losses"] += 1
        profile_day["losses"] += 1
```

When finalizing each day and profile, calculate hit rate over decided outcomes,
not all resolved positions:

```python
decided = day["wins"] + day["losses"]
day["hit_rate"] = day["wins"] / decided if decided else None
for profile_stats in day["profiles"].values():
    profile_decided = profile_stats["wins"] + profile_stats["losses"]
    profile_stats["hit_rate"] = (
        profile_stats["wins"] / profile_decided if profile_decided else None
    )
```

- [ ] **Step 5: Use raw window lots for money and logical positions for outcomes**

Define the two window views explicitly:

```python
window_lots = [
    lot
    for lot in orders
    if lot["realized_pnl"] is not None
    and lot["status"] in {"PAPER_SETTLED", "PAPER_CLOSED"}
    and (resolved := lot["closed_at"] or lot["settled_at"]) is not None
    and _local_day(resolved) >= window_start.isoformat()
]
window_positions = [
    position
    for position in terminal_positions
    if (resolved := position.get("closed_at") or position.get("settled_at"))
    and _local_day(resolved) >= window_start.isoformat()
]
window_pnl = sum(float(lot["realized_pnl"]) for lot in window_lots)
window_spend = sum(
    float(lot["contracts"]) * float(lot["cost_per_contract"])
    for lot in window_lots
)
window_wins = sum(position.get("logical_outcome") == "win" for position in window_positions)
window_losses = sum(position.get("logical_outcome") == "loss" for position in window_positions)
ranked = sorted(
    window_positions,
    key=lambda position: float(position.get("realized_pnl") or 0.0),
    reverse=True,
)
```

Replace `_side_performance` with:

```python
def _side_performance(
    positions: list[dict[str, Any]],
    lots: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    sides = {
        side: {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "realized_pnl": 0.0,
            "capital": 0.0,
        }
        for side in ("YES", "NO")
    }
    for position in positions:
        side = str(position.get("side") or "YES").upper()
        if side not in sides:
            continue
        sides[side]["trades"] += 1
        sides[side]["wins"] += position.get("logical_outcome") == "win"
        sides[side]["losses"] += position.get("logical_outcome") == "loss"
    for lot in lots:
        side = str(lot.get("side") or "YES").upper()
        if side not in sides:
            continue
        sides[side]["realized_pnl"] += float(lot.get("realized_pnl") or 0.0)
        sides[side]["capital"] += float(lot["contracts"]) * float(
            lot["cost_per_contract"]
        )
    for bucket in sides.values():
        decided = bucket["wins"] + bucket["losses"]
        bucket["hit_rate"] = round(bucket["wins"] / decided, 4) if decided else None
        bucket["roi"] = (
            round(bucket["realized_pnl"] / bucket["capital"], 4)
            if bucket["capital"] > 0
            else None
        )
        bucket["realized_pnl"] = round(bucket["realized_pnl"], 2)
        bucket["capital"] = round(bucket["capital"], 2)
    return sides
```

Replace `_window_profile_totals` with a three-view implementation:

```python
def _window_profile_totals(
    positions: list[dict[str, Any]],
    lots: list[dict[str, Any]],
    open_orders: list[dict[str, Any]],
    scanning_profiles: list[str] | None = None,
) -> list[dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}

    def bucket(name: str) -> dict[str, Any]:
        return profiles.setdefault(
            name,
            {
                "risk_profile": name,
                "resolved": 0,
                "wins": 0,
                "losses": 0,
                "realized_pnl": 0.0,
                "capital_resolved": 0.0,
                "open_positions": 0,
                "open_risk": 0.0,
            },
        )

    for position in positions:
        stats = bucket(str(position.get("risk_profile") or "unknown"))
        stats["resolved"] += 1
        stats["wins"] += position.get("logical_outcome") == "win"
        stats["losses"] += position.get("logical_outcome") == "loss"
    for lot in lots:
        stats = bucket(str(lot.get("risk_profile") or "unknown"))
        stats["realized_pnl"] += float(lot.get("realized_pnl") or 0.0)
        stats["capital_resolved"] += float(lot["contracts"]) * float(
            lot["cost_per_contract"]
        )
    for order in open_orders:
        stats = bucket(str(order.get("risk_profile") or "unknown"))
        stats["open_positions"] += 1
        stats["open_risk"] += float(order["contracts"]) * float(
            order["cost_per_contract"]
        )
    for name in scanning_profiles or []:
        bucket(name)

    output: list[dict[str, Any]] = []
    for name in sorted(profiles):
        stats = profiles[name]
        decided = stats["wins"] + stats["losses"]
        capital = stats["capital_resolved"]
        output.append(
            {
                **stats,
                "realized_pnl": round(stats["realized_pnl"], 2),
                "capital_resolved": round(capital, 2),
                "open_risk": round(stats["open_risk"], 2),
                "hit_rate": round(stats["wins"] / decided, 4) if decided else None,
                "roi": round(stats["realized_pnl"] / capital, 4) if capital else None,
            }
        )
    return output
```

Call these as `_side_performance(window_positions, window_lots)` and
`_window_profile_totals(window_positions, window_lots, open_orders,
scanning_profiles)`. Use aggregate logical positions for exit reasons,
learnings, and rankings. Add these fields to `_order_brief`:

```python
"logical_order_id": order.get("logical_order_id", order["id"]),
"exit_fill_count": int(order.get("exit_fill_count") or 0),
```

- [ ] **Step 6: Run paper summary tests**

```bash
PYTHONPATH=trading python3 -m pytest trading/tests/test_paper_summary.py -q
```

Expected: all tests pass and the cross-day test shows one terminal outcome.

- [ ] **Step 7: Commit daily summary semantics**

```bash
git add trading/sfo_kalshi_quant/summary.py trading/tests/test_paper_summary.py
git commit -m "fix: separate paper cash timing from trade outcomes"
```

---

### Task 6: Isolate the live weekly goal and correct exit diagnostics

**Files:**
- Modify: `trading/sfo_kalshi_quant/strategy_lab/build.py:433-525`
- Modify: `trading/sfo_kalshi_quant/store/diagnostics.py:175-250`
- Modify: `trading/sfo_kalshi_quant/db.py:2280-2550`
- Modify: `trading/tests/test_audit_2026_07_14.py`

- [ ] **Step 1: Add a failing live weekly-attribution test**

Append to `trading/tests/test_audit_2026_07_14.py`:

```python
def test_weekly_goal_excludes_legacy_research_in_shared_account() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        now_pt = datetime.now().astimezone(ZoneInfo("America/Los_Angeles"))
        this_monday = (now_pt - timedelta(days=now_pt.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        resolved_at = (this_monday + timedelta(days=1)).astimezone(UTC).isoformat()
        live_id = store.record_paper_order("2026-07-17", _decision(), risk_profile="live")
        research_id = store.record_paper_order(
            "2026-07-17", _decision(), risk_profile="research"
        )
        legacy_id = store.record_paper_order(
            "2026-07-17", _decision(), risk_profile="live"
        )
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET account_id='paper-shared', status='PAPER_SETTLED', "
                "realized_pnl=10, settled_at=? WHERE id=?",
                (resolved_at, live_id),
            )
            conn.execute(
                "UPDATE paper_orders SET account_id='paper-shared', status='PAPER_SETTLED', "
                "realized_pnl=25, settled_at=? WHERE id=?",
                (resolved_at, research_id),
            )
            conn.execute(
                "UPDATE paper_orders SET account_id='paper-shared', risk_profile=NULL, "
                "status='PAPER_SETTLED', realized_pnl=5, settled_at=? WHERE id=?",
                (resolved_at, legacy_id),
            )

        goal = _weekly_goal_payload(store, {"realized_equity": 1040.0})

        assert goal["weekly_realized_pnl"] == 15.0
        assert "all research profiles" in goal["disclaimer"].lower()
```

- [ ] **Step 2: Add a failing partial-exit diagnostic test**

Append:

```python
def test_partial_exit_outcome_diagnostics_use_executed_quantity() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        order_id = store.record_paper_order(
            "2026-07-17",
            replace(_decision(), recommended_contracts=8.0),
            risk_profile="live",
        )

        child = store.close_paper_order(order_id, 0.20, max_quantity=2.0)
        payload = json.loads(child["outcome_diagnostics_json"])

        assert payload["outcome"]["executed_quantity"] == 2.0
        assert payload["outcome"]["pnl_per_contract"] == pytest.approx(
            float(child["realized_pnl"]) / 2.0
        )
        assert payload["exit_execution"]["executed_quantity"] == 2.0
```

Add `json`, `pytest`, and `replace` imports only if absent.

- [ ] **Step 3: Run both tests and verify attribution/arithmetic failures**

```bash
PYTHONPATH=trading python3 -m pytest \
  trading/tests/test_audit_2026_07_14.py::test_weekly_goal_excludes_legacy_research_in_shared_account \
  trading/tests/test_audit_2026_07_14.py::test_partial_exit_outcome_diagnostics_use_executed_quantity \
  -q
```

Expected before the fix: weekly P&L is 40 and `pnl_per_contract` divides by
eight.

- [ ] **Step 4: Filter weekly rows through profile normalization**

Select `risk_profile` with each resolved row:

```sql
SELECT COALESCE(closed_at,settled_at), COALESCE(realized_pnl,0), risk_profile
FROM paper_orders
WHERE account_id=?
  AND status IN ('PAPER_SETTLED','PAPER_CLOSED')
  AND COALESCE(closed_at,settled_at) IS NOT NULL
ORDER BY COALESCE(closed_at,settled_at), id
```

Before attributing a row:

```python
for resolved_at, realized_pnl, risk_profile in resolved_rows:
    try:
        normalized_profile = normalize_risk_profile_name(
            str(risk_profile) if risk_profile else "live"
        )
    except ValueError:
        continue
    if normalized_profile != "live":
        continue
```

Import `normalize_risk_profile_name`. Add `all research profiles` to the payload
`excludes` list and disclaimer without changing the actual shared-account
starting-equity calculation.

- [ ] **Step 5: Thread executed quantity into outcome diagnostics**

Add a required keyword argument:

```python
def _outcome_diagnostics_payload(
    row: sqlite3.Row,
    *,
    event: str,
    resolved_at: str,
    settlement_high_f: float | None,
    resolved_yes: bool | None,
    position_won: bool | None,
    realized_pnl: float,
    executed_quantity: float,
    exit_price: float | None = None,
    exit_fee_per_contract: float | None = None,
) -> dict[str, object]:
```

Write:

```python
"executed_quantity": _round_number(executed_quantity),
"pnl_per_contract": _round_number(
    realized_pnl / executed_quantity if executed_quantity > 0 else None
),
```

Pass `executed_quantity=float(row["contracts"])` from settlement/full-resolution
callers and `executed_quantity=executed` from `close_paper_order`.

- [ ] **Step 6: Run audit and diagnostics tests**

```bash
PYTHONPATH=trading python3 -m pytest \
  trading/tests/test_audit_2026_07_14.py \
  trading/tests/test_paper_settlement.py \
  -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit attribution and diagnostics fixes**

```bash
git add trading/sfo_kalshi_quant/strategy_lab/build.py \
  trading/sfo_kalshi_quant/store/diagnostics.py trading/sfo_kalshi_quant/db.py \
  trading/tests/test_audit_2026_07_14.py
git commit -m "fix: isolate live results and partial-exit diagnostics"
```

---

### Task 7: Move same-day research to shadow-only evidence

**Files:**
- Modify: `trading/sfo_kalshi_quant/_cli/scan.py:1059-1100`
- Modify: `trading/tests/test_entry_target_gate.py`
- Modify: `trading/tests/test_research_shadow.py`

- [ ] **Step 1: Replace the old same-day research allowance test**

Replace `test_research_same_day_entry_gate_allows_observed_high_lock_before_peak_window`
with:

```python
def test_research_same_day_entry_is_shadow_only_even_before_peak_window():
    target = date(2026, 6, 8)
    now = datetime(2026, 6, 8, 2, 39, tzinfo=SFO_TZ)
    forecast = _forecast(target, {"observed_high_decision": {"mode": "lock"}})

    allowed, reason = _paper_entry_gate_for_target(
        target, forecast, None, now=now, risk_profile="research"
    )

    assert allowed is False
    assert reason == (
        "same-day entry disabled: research paper requires min_lead_days=1; "
        "same-day signals are shadow-only"
    )
```

Update the fixed-time and complete-intraday research tests to expect this same
policy reason. Keep the later-target behavior unchanged, and update the live
test's reason suffix from `research-only` to `shadow-only` so the recorded gate
copy remains truthful.

- [ ] **Step 2: Add a failing shadow-evidence test**

Append to `trading/tests/test_research_shadow.py`:

```python
def test_blocked_same_day_research_signal_is_recorded_without_paper_position() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(store, StrategyConfig(), risk_profile="research")
        reason = (
            "same-day entry disabled: research paper requires min_lead_days=1; "
            "same-day signals are shadow-only"
        )
        decision = _research_explore_decision(contracts=3.0, cost=0.42)
        blocked = decision.__class__(
            **{
                **decision.__dict__,
                "approved": False,
                "signal_approved": True,
                "entry_block_reason": reason,
                "reasons": [reason, *decision.reasons],
            }
        )

        shadow_ids = trader.record_research_shadow_candidates(
            "2026-07-17", [blocked], sampled=False
        )

        assert len(shadow_ids) == 1
        assert store.paper_orders(10) == []
        row = store.research_shadow_orders(10)[0]
        assert row["sampled"] == 0
        assert row["linked_paper_order_id"] is None
        assert reason in row["reasons_json"]
```

- [ ] **Step 3: Run the gate tests and confirm the old allowance fails**

```bash
PYTHONPATH=trading python3 -m pytest \
  trading/tests/test_entry_target_gate.py \
  trading/tests/test_research_shadow.py::test_blocked_same_day_research_signal_is_recorded_without_paper_position \
  -q
```

Expected: the gate still allows early same-day research.

- [ ] **Step 4: Implement the profile-specific same-day block**

In `_paper_entry_gate_for_target`, keep the single-source check first and later
targets allowed. Replace the same-day profile/cutoff branch with:

```python
profile = normalize_risk_profile_name(risk_profile)
if profile == "live":
    return (
        False,
        "live paper entry requires min_lead_days=1; same-day signals are shadow-only",
    )
return (
    False,
    (
        "same-day entry disabled: research paper requires min_lead_days=1; "
        "same-day signals are shadow-only"
    ),
)
```

Do not modify the existing portfolio branch that calls
`record_research_shadow_candidates` for blocked research plans. It is the
approved evidence-preservation path.

- [ ] **Step 5: Run entry, shadow, portfolio, and status tests**

```bash
PYTHONPATH=trading python3 -m pytest \
  trading/tests/test_entry_target_gate.py \
  trading/tests/test_research_shadow.py \
  trading/tests/test_portfolio_cli.py \
  trading/tests/test_strategy_research.py \
  -q
```

Expected: all tests pass; day-ahead behavior remains unchanged.

- [ ] **Step 6: Commit the research lead-time tune**

```bash
git add trading/sfo_kalshi_quant/_cli/scan.py \
  trading/tests/test_entry_target_gate.py trading/tests/test_research_shadow.py
git commit -m "fix: keep same-day research in the shadow ledger"
```

---

### Task 8: Explain multi-fill logical rows in the SPA

**Files:**
- Modify: `src/lib/strategy.ts:3-35`
- Modify: `src/components/strategy/LedgerTable.tsx:80-115`
- Create: `src/components/strategy/LedgerTable.test.tsx`

- [ ] **Step 1: Add a failing ledger rendering test**

Create `src/components/strategy/LedgerTable.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ClosedPosition, StrategyLab } from "../../lib/strategy";
import { LedgerTable } from "./LedgerTable";

const logicalRow: ClosedPosition = {
  id: 456,
  logical_order_id: 456,
  child_order_ids: [458, 459, 460],
  exit_fill_count: 4,
  ticker: "KXHIGHTPHX-26JUL17-T96",
  label: "97° or above",
  side: "NO",
  contracts: 8,
  entry_price: 0.93,
  exit_price: 0.86,
  realized_pnl: -0.63,
  realized_roi: -0.084,
  quality_score: 37,
  risk_profile: "live",
  target_date: "2026-07-17",
  closed_at: "2026-07-17T20:06:00+00:00",
};

const strategy = {
  paper_trading: { closed_positions: [logicalRow] },
} as StrategyLab;

describe("LedgerTable logical exit fills", () => {
  it("renders one logical row with a compact fill count", () => {
    render(<LedgerTable s={strategy} rows={[logicalRow]} detailed hideProfile />);

    expect(screen.getAllByText("97° or above")).toHaveLength(1);
    expect(screen.getByText("4 fills")).toBeInTheDocument();
    expect(screen.getByText("8")).toBeInTheDocument();
  });

  it("does not annotate a legacy one-row position", () => {
    render(
      <LedgerTable
        s={strategy}
        rows={[{ ...logicalRow, id: 7, exit_fill_count: undefined }]}
        detailed
        hideProfile
      />,
    );

    expect(screen.queryByText(/fills$/)).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run the test and verify TypeScript/rendering failure**

```bash
bun run test -- src/components/strategy/LedgerTable.test.tsx
```

Expected: TypeScript does not recognize the logical fields or `4 fills` is not
rendered.

- [ ] **Step 3: Add tolerant optional fields to `ClosedPosition`**

Add:

```typescript
logical_order_id?: number;
child_order_ids?: number[];
exit_fill_count?: number;
integrity_findings?: string[];
```

All fields remain optional so old cached/public artifacts continue to render.

- [ ] **Step 4: Render the fill annotation beneath the detailed exit price**

Replace the detailed fill cell contents with:

```tsx
<div className="flex flex-col items-end gap-0.5">
  <span className="tnum text-muted">
    {detailed ? `${cents(d.entry_price)} → ${cents(d.exit_price)}` : cents(d.entry_price)}
  </span>
  {detailed && (d.exit_fill_count ?? 0) > 1 && (
    <span className="font-mono text-[10px] uppercase tracking-wide text-muted">
      {d.exit_fill_count} fills
    </span>
  )}
</div>
```

Do not add a new column; the ledger is already horizontally dense on mobile.

- [ ] **Step 5: Run focused and full SPA checks**

```bash
bun run test -- src/components/strategy/LedgerTable.test.tsx src/lib/strategy.test.ts
bun run lint
bun run build
```

Expected: all commands exit zero.

- [ ] **Step 6: Commit the UI explanation**

```bash
git add src/lib/strategy.ts src/components/strategy/LedgerTable.tsx \
  src/components/strategy/LedgerTable.test.tsx
git commit -m "feat: label multi-fill paper positions"
```

---

### Task 9: Reconcile authoritative data and run the full completion audit

**Files:**
- Modify only if verification finds a defect in files already listed above.
- Do not commit generated runtime JSON, copied databases, `dist/`, screenshots,
  or temporary audit artifacts.

- [ ] **Step 1: Run every focused regression together**

```bash
PYTHONPATH=trading python3 -m pytest \
  trading/tests/test_logical_positions.py \
  trading/tests/test_paper_settlement.py \
  trading/tests/test_paper_risk_pause.py \
  trading/tests/test_posterior_kelly.py \
  trading/tests/test_clv.py \
  trading/tests/test_paper_summary.py \
  trading/tests/test_strategy_research.py \
  trading/tests/test_audit_2026_07_14.py \
  trading/tests/test_entry_target_gate.py \
  trading/tests/test_research_shadow.py \
  -q
```

Expected: all focused Python tests pass.

- [ ] **Step 2: Run the complete repository suites**

```bash
bash scripts/run_tests.sh
bun run test
bun run lint
bun run build
python3 -m compileall -q forecaster trading/sfo_kalshi_quant trading/tests scripts
```

Expected: every command exits zero.

- [ ] **Step 3: Run health and security checks**

Use the isolated Semgrep environment created during investigation if it still
exists; otherwise create another isolated environment outside the repository:

```bash
python3 -m venv /tmp/weatheredge-semgrep-venv
/tmp/weatheredge-semgrep-venv/bin/pip install semgrep
PATH="/tmp/weatheredge-semgrep-venv/bin:$PATH" bash scripts/verify_project.sh
```

Expected: project health, Semgrep, tests, and compile checks pass. The local
runtime placeholder notice is informational after cleanup.

- [ ] **Step 4: Copy authoritative databases to an isolated local audit directory**

Read `.local/ec2.env` without printing it, create a validated temporary
directory, and use SQLite's online backup API on the server so both copied
databases are transactionally consistent:

```bash
audit_dir="$(mktemp -d /tmp/weatheredge-logical-audit.XXXXXX)"
set -a
. .local/ec2.env
set +a
remote_audit_db="/tmp/weatheredge-logical-audit-${USER}.db"
remote_weather_db="/tmp/weatheredge-weather-audit-${USER}.db"
ssh -o BatchMode=yes -i "$EC2_KEY" "${REMOTE_USER:-ubuntu}@$EC2_IP" \
  "python3 -c \"import sqlite3; src=sqlite3.connect('/opt/weatheredge/trading/data/paper_trading.db'); dst=sqlite3.connect('$remote_audit_db'); src.backup(dst); dst.close(); src.close()\""
ssh -o BatchMode=yes -i "$EC2_KEY" "${REMOTE_USER:-ubuntu}@$EC2_IP" \
  "python3 -c \"import sqlite3; src=sqlite3.connect('/opt/weatheredge/forecaster/weather.db'); dst=sqlite3.connect('$remote_weather_db'); src.backup(dst); dst.close(); src.close()\""
mkdir -p "$audit_dir/forecaster"
scp -q -i "$EC2_KEY" "${REMOTE_USER:-ubuntu}@$EC2_IP:$remote_audit_db" "$audit_dir/paper_trading.db"
scp -q -i "$EC2_KEY" "${REMOTE_USER:-ubuntu}@$EC2_IP:$remote_weather_db" "$audit_dir/forecaster/weather.db"
scp -q -i "$EC2_KEY" "${REMOTE_USER:-ubuntu}@$EC2_IP:/opt/weatheredge/forecaster/ab_test_results.json" "$audit_dir/forecaster/ab_test_results.json"
ssh -o BatchMode=yes -i "$EC2_KEY" "${REMOTE_USER:-ubuntu}@$EC2_IP" \
  "rm -f '$remote_audit_db' '$remote_weather_db'"
```

The only removals are the two exact temporary server files created by the
preceding commands.

- [ ] **Step 5: Reconcile raw lots against logical positions on the copied DB**

Run a read-only inline audit with the new module:

```bash
PYTHONPATH=trading python3 - "$audit_dir/paper_trading.db" <<'PY'
import sqlite3, sys
from sfo_kalshi_quant.logical_positions import group_logical_positions

db = sys.argv[1]
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
rows = conn.execute("SELECT * FROM paper_orders WHERE status != 'REJECTED'").fetchall()
groups = group_logical_positions(rows)
invalid = [group for group in groups if not group.valid]
assert invalid == [], [(group.logical_order_id, group.integrity_findings) for group in invalid]
terminal = [group for group in groups if group.terminal]
raw_pnl = sum(float(row["realized_pnl"] or 0) for row in rows if row["realized_pnl"] is not None and row["status"] != "PAPER_EXPIRED")
logical_pnl = sum(float(group.as_row()["realized_pnl"] or 0) for group in groups)
raw_capital = sum(float(row["contracts"] or 0) * float(row["cost_per_contract"] or 0) for row in rows if row["realized_pnl"] is not None and row["status"] != "PAPER_EXPIRED")
logical_capital = sum(float(group.as_row()["capital_resolved"] or 0) for group in groups)
assert abs(raw_pnl - logical_pnl) < 1e-9
assert abs(raw_capital - logical_capital) < 1e-9
live = [group for group in terminal if (group.root.get("risk_profile") or "live") == "live"]
wins = sum(group.won is True for group in live)
losses = sum(group.won is False for group in live)
print({"logical_live": len(live), "wins": wins, "losses": losses, "pnl": round(sum(group.as_row()["realized_pnl"] for group in live), 2)})
PY
```

Expected at the investigation snapshot: 57 logical live positions, 40 wins, 17
losses, and unchanged live P&L. New trades settling after the snapshot may
increase counts; P&L/capital reconciliation must remain exact.

- [ ] **Step 6: Generate an isolated Strategy Lab artifact from the copied DB**

Use the copied AWS forecaster inputs and an explicit temporary output. Never
read ignored local runtime artifacts or overwrite them:

```bash
PYTHONPATH=trading python3 -m sfo_kalshi_quant.cli \
  --no-color \
  --forecaster-root "$audit_dir/forecaster" \
  --db-path "$audit_dir/paper_trading.db" \
  strategy-research \
  --calibration-min-train 180 \
  --output "$audit_dir/strategy_research.json"
jq '.paper_trading.closed_positions[] | select(.logical_order_id == 456)' \
  "$audit_dir/strategy_research.json"
```

Expected: one Phoenix object with `contracts: 8`, `exit_fill_count: 4`, three
child IDs, and aggregate P&L.

- [ ] **Step 7: Verify weekly attribution from the isolated artifact**

```bash
jq '.accounting.goal' \
  "$audit_dir/strategy_research.json"
```

Expected at the investigation snapshot: live weekly realized P&L is about
`16.58`, not `17.42`, and exclusion metadata names all research profiles.

- [ ] **Step 8: Clear stale local runtime state and rebuild the SPA**

```bash
python3 scripts/clear_local_runtime_state.py --confirm
bun run build
cp "$audit_dir/strategy_research.json" dist/strategy_research.json
```

The corrected artifact is copied only into ignored generated `dist/` for visual
verification.

- [ ] **Step 9: Use the required frontend/browser skills for desktop and mobile verification**

Read and follow `frontend-design`, `ui-ux-pro-max`, `web-design-guidelines`, and
`agent-browser`. Serve `dist/`, open `#/lab`, and verify:

- Desktop Strategy Lab renders one Phoenix row with `4 fills`.
- Phoenix quantity, P&L, ROI, and city aggregate match the logical position.
- Philadelphia renders one aggregate row.
- Profile summary count and hit rate match logical outcomes.
- A real mobile viewport keeps the annotation readable without adding a new
  column or clipping the ledger's horizontal scroll region.
- DOM text contains no public prediction-market venue name.
- The book selector, scrolling ledger, navigation, search, theme control, and
  mobile navigation remain interactive.

Capture desktop and mobile screenshots and read the DOM state back after each
interaction. Stop the local server when verification is complete.

- [ ] **Step 10: Inspect final changes and repository state**

```bash
git diff --check
git status --short
git log --oneline --decorate -10
```

Expected: no uncommitted source changes, no generated runtime artifacts staged,
and one focused commit per completed task.

- [ ] **Step 11: Perform the completion audit against the approved spec**

Read the completion criteria in
`docs/superpowers/specs/2026-07-17-logical-paper-positions-and-research-lead-time-design.md`
and record evidence for every item:

- Immutable journal preserved
- Decision-level consumers use logical positions
- Raw and logical money reconcile
- Phoenix and Philadelphia collapse correctly
- Live weekly attribution excludes research
- Same-day research records evidence but places no paper position
- Full test/security/build checks pass
- Desktop and mobile browser verification pass
- No unresolved integrity or runtime-health finding remains

Do not mark the goal complete if any item lacks direct evidence.
