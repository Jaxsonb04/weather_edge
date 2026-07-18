from __future__ import annotations

from collections.abc import KeysView
from copy import deepcopy

import pytest

from sfo_kalshi_quant.logical_positions import (
    LogicalPaperPosition,
    OPEN_STATUSES,
    TERMINAL_STATUSES,
    group_logical_positions,
)


class _RowLike:
    """Dependency-free stand-in for keyed rows such as sqlite3.Row."""

    def __init__(self, values: dict[str, object]) -> None:
        self._values = dict(values)

    def keys(self) -> KeysView[str]:
        return self._values.keys()

    def __getitem__(self, key: str) -> object:
        return self._values[key]


def _paper_order(
    order_id: object,
    *,
    parent_order_id: object = None,
    contracts: object = 2,
    status: str = "PAPER_CLOSED",
    realized_pnl: object = -0.16,
    exit_price: object = 0.86,
    closed_at: str | None = "2026-07-15T21:00:00+00:00",
    market_ticker: str = "KXHIGHPHX-26JUL15-T97",
    research_sleeve: str | None = None,
    research_policy_version: str | None = None,
    policy_fingerprint: str | None = None,
    strategy_fingerprint: str | None = None,
    execution_model_version: str | None = None,
) -> dict[str, object]:
    return {
        "id": order_id,
        "parent_order_id": parent_order_id,
        "created_at": "2026-07-15T15:00:00+00:00",
        "filled_at": "2026-07-15T15:01:00+00:00",
        "target_date": "2026-07-15",
        "market_ticker": market_ticker,
        "label": "97 degrees or above",
        "side": "NO",
        "risk_profile": "live",
        "account_id": "paper-shared",
        "research_sleeve": research_sleeve,
        "research_policy_version": research_policy_version,
        "policy_fingerprint": policy_fingerprint,
        "strategy_fingerprint": strategy_fingerprint,
        "execution_model_version": execution_model_version,
        "entry_price": 0.93,
        "cost_per_contract": 0.93,
        "contracts": contracts,
        "status": status,
        "realized_pnl": realized_pnl,
        "exit_price": exit_price,
        "exit_fee_per_contract": 0.01 if exit_price is not None else None,
        "closed_at": closed_at,
        "settled_at": None,
        "edge": 0.08,
        "resolved_yes": 1,
    }


def test_groups_four_terminal_exit_fills_into_one_logical_position() -> None:
    rows = [
        _paper_order(456),
        _paper_order(458, parent_order_id=456),
        _paper_order(459, parent_order_id=456),
        _paper_order(460, parent_order_id=456),
    ]

    positions = group_logical_positions(rows)

    assert len(positions) == 1
    position = positions[0]
    assert position.valid is True
    assert position.terminal is True
    assert position.logical_order_id == 456
    assert position.child_order_ids == (458, 459, 460)
    assert position.won is False
    projected = position.as_row()
    assert projected["contracts"] == 8
    assert projected["resolved_contracts"] == 8
    assert projected["open_contracts"] == 0
    assert projected["exit_fill_count"] == 4
    assert projected["realized_pnl"] == pytest.approx(-0.64)
    assert projected["capital_resolved"] == pytest.approx(7.44)
    assert projected["exit_price"] == pytest.approx(0.86)
    assert projected["logical_outcome"] == "loss"


def test_public_status_collections_are_immutable() -> None:
    assert TERMINAL_STATUSES == frozenset({"PAPER_CLOSED", "PAPER_SETTLED"})
    assert OPEN_STATUSES == frozenset(
        {
            "PAPER_FILLED",
            "PAPER_PARTIALLY_FILLED",
            "PAPER_PARTIAL_EXPIRED",
        }
    )
    assert isinstance(TERMINAL_STATUSES, frozenset)
    assert isinstance(OPEN_STATUSES, frozenset)


def test_logical_position_constructor_defaults_to_no_integrity_findings() -> None:
    root = _paper_order(7, realized_pnl=0.24)

    position = LogicalPaperPosition(7, root, (root,))

    assert position.integrity_findings == ()
    assert position.valid is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("contracts", float("nan")),
        ("realized_pnl", "not-a-number"),
    ],
)
def test_direct_constructor_applies_intrinsic_validation(
    field: str, value: object
) -> None:
    root = _paper_order(7, realized_pnl=0.24)
    root[field] = value

    position = LogicalPaperPosition(7, root, (root,))

    assert f"invalid {field} on order 7" in position.integrity_findings
    _assert_invalid_aggregates_fail_closed(position)


def test_direct_constructor_rejects_missing_root_lot() -> None:
    root = _paper_order(10)

    position = LogicalPaperPosition(10, root, ())

    assert "root order 10 missing from lots" in position.integrity_findings
    _assert_invalid_aggregates_fail_closed(position)


def test_direct_constructor_rejects_mismatched_logical_order_id() -> None:
    root = _paper_order(10)

    position = LogicalPaperPosition(99, root, (root,))

    assert (
        "logical_order_id 99 does not match root order 10"
        in position.integrity_findings
    )
    _assert_invalid_aggregates_fail_closed(position)


def test_direct_constructor_rejects_child_with_wrong_parent() -> None:
    root = _paper_order(10)
    child = _paper_order(11, parent_order_id=20)

    position = LogicalPaperPosition(10, root, (root, child))

    assert (
        "child 11 references parent order 20, expected 10"
        in position.integrity_findings
    )
    _assert_invalid_aggregates_fail_closed(position)


def test_direct_constructor_rejects_child_attribution_mismatch() -> None:
    root = _paper_order(10)
    child = _paper_order(
        11,
        parent_order_id=10,
        market_ticker="DIFFERENT-MARKET",
    )

    position = LogicalPaperPosition(10, root, (root, child))

    assert "market_ticker mismatch on child 11" in position.integrity_findings
    _assert_invalid_aggregates_fail_closed(position)


def test_direct_constructor_accepts_valid_root_and_children() -> None:
    root = _paper_order(10, contracts=1, realized_pnl=0.1)
    child = _paper_order(
        11,
        parent_order_id=10,
        contracts=3,
        realized_pnl=0.3,
    )

    position = LogicalPaperPosition(10, root, (root, child))

    assert position.valid is True
    assert position.terminal is True
    assert position.child_order_ids == (11,)
    assert position.as_row()["contracts"] == 4
    assert position.as_row()["realized_pnl"] == pytest.approx(0.4)


def test_keeps_incomplete_partial_exit_open() -> None:
    rows = [
        _paper_order(
            456,
            contracts=6,
            status="PAPER_FILLED",
            realized_pnl=None,
            exit_price=None,
            closed_at=None,
        ),
        _paper_order(458, parent_order_id=456),
    ]

    position = group_logical_positions(rows)[0]

    assert position.valid is True
    assert position.terminal is False
    projected = position.as_row()
    assert projected["contracts"] == 8
    assert projected["resolved_contracts"] == 2
    assert projected["open_contracts"] == 6
    assert projected["logical_outcome"] == "undecided"


def test_projects_legacy_terminal_row_as_one_logical_position() -> None:
    position = group_logical_positions([_paper_order(7, realized_pnl=0.24)])[0]

    assert position.valid is True
    assert position.terminal is True
    assert position.logical_order_id == 7
    assert position.child_order_ids == ()
    assert position.won is True
    assert position.as_row()["logical_outcome"] == "win"


def test_retains_orphan_child_as_invalid_nonterminal_group() -> None:
    position = group_logical_positions(
        [_paper_order(10, parent_order_id=999)]
    )[0]

    assert position.logical_order_id == 999
    assert position.valid is False
    assert position.terminal is False
    assert position.integrity_findings == ("missing root order 999",)
    assert position.as_row()["integrity_findings"] == ["missing root order 999"]


def test_rejects_child_with_mismatched_market_ticker() -> None:
    rows = [
        _paper_order(10),
        _paper_order(
            11,
            parent_order_id=10,
            market_ticker="KXHIGHPHX-26JUL15-T98",
        ),
    ]

    position = group_logical_positions(rows)[0]

    assert position.valid is False
    assert position.terminal is False
    assert "market_ticker mismatch on child 11" in position.integrity_findings


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("research_sleeve", "motion"),
        ("research_policy_version", "research-motion-v1"),
        ("policy_fingerprint", "motion-policy-fingerprint"),
        ("strategy_fingerprint", "different-strategy-fingerprint"),
        ("execution_model_version", "exec-v3-2026-07-14"),
    ],
)
def test_rejects_child_with_cross_generation_identity(
    field: str,
    value: object,
) -> None:
    research_identity = {
        "research_sleeve": "target",
        "research_policy_version": "research-target-v1",
        "policy_fingerprint": "target-policy-fingerprint",
        "strategy_fingerprint": "strategy-fingerprint",
        "execution_model_version": "exec-v4-2026-07-17",
    }
    root = _paper_order(10, **research_identity)
    child = _paper_order(11, parent_order_id=10, **research_identity)
    child[field] = value

    position = group_logical_positions([root, child])[0]

    assert position.valid is False
    assert position.terminal is False
    assert f"{field} mismatch on child 11" in position.integrity_findings
    _assert_invalid_aggregates_fail_closed(position)


def _assert_invalid_aggregates_fail_closed(
    position: LogicalPaperPosition,
) -> None:
    assert position.valid is False
    assert position.terminal is False
    projected = position.as_row()
    assert projected["contracts"] is None
    assert projected["resolved_contracts"] is None
    assert projected["open_contracts"] is None
    assert projected["resolved_lot_count"] is None
    assert projected["exit_fill_count"] is None
    assert projected["realized_pnl"] is None
    assert projected["capital_resolved"] is None
    assert projected["exit_price"] is None
    assert projected["exit_fee_per_contract"] is None
    assert projected["latest_resolved_at"] is None
    assert projected["logical_outcome"] == "undecided"


@pytest.mark.parametrize("realized_pnl", ["bad-pnl", float("nan"), float("inf")])
def test_rejects_present_invalid_pnl_on_nonterminal_root(
    realized_pnl: object,
) -> None:
    position = group_logical_positions(
        [
            _paper_order(
                10,
                status="PAPER_FILLED",
                realized_pnl=realized_pnl,
                exit_price=None,
                closed_at=None,
            )
        ]
    )[0]

    assert "invalid realized_pnl on order 10" in position.integrity_findings
    _assert_invalid_aggregates_fail_closed(position)


def test_rejects_malformed_terminal_realized_pnl_without_coercing_to_zero() -> None:
    position = group_logical_positions(
        [_paper_order(10, realized_pnl="not-a-number")]
    )[0]

    assert "invalid realized_pnl on order 10" in position.integrity_findings
    _assert_invalid_aggregates_fail_closed(position)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("contracts", float("nan")),
        ("entry_price", float("nan")),
        ("cost_per_contract", float("inf")),
        ("exit_price", float("nan")),
        ("exit_fee_per_contract", float("-inf")),
    ],
)
def test_rejects_non_finite_numeric_evidence(field: str, value: float) -> None:
    row = _paper_order(10)
    row[field] = value

    position = group_logical_positions([row])[0]

    assert f"invalid {field} on order 10" in position.integrity_findings
    _assert_invalid_aggregates_fail_closed(position)


@pytest.mark.parametrize(
    ("field", "value", "finding"),
    [
        ("entry_price", -0.1, "entry_price must be > 0 and < 1 on order 10"),
        ("entry_price", 0, "entry_price must be > 0 and < 1 on order 10"),
        ("entry_price", 1, "entry_price must be > 0 and < 1 on order 10"),
        ("entry_price", 1.1, "entry_price must be > 0 and < 1 on order 10"),
        ("exit_price", -0.1, "exit_price must be > 0 and < 1 on order 10"),
        ("exit_price", 0, "exit_price must be > 0 and < 1 on order 10"),
        ("exit_price", 1, "exit_price must be > 0 and < 1 on order 10"),
        ("exit_price", 1.1, "exit_price must be > 0 and < 1 on order 10"),
        (
            "cost_per_contract",
            -0.1,
            "cost_per_contract must be positive on order 10",
        ),
        (
            "cost_per_contract",
            0,
            "cost_per_contract must be positive on order 10",
        ),
        (
            "exit_fee_per_contract",
            -0.01,
            "exit_fee_per_contract must be nonnegative on order 10",
        ),
    ],
)
def test_rejects_numeric_evidence_outside_producer_domain(
    field: str, value: float, finding: str
) -> None:
    row = _paper_order(10)
    row[field] = value

    position = group_logical_positions([row])[0]

    assert finding in position.integrity_findings
    _assert_invalid_aggregates_fail_closed(position)


def test_accepts_zero_exit_fee() -> None:
    row = _paper_order(10)
    row["exit_fee_per_contract"] = 0.0

    position = group_logical_positions([row])[0]

    assert position.valid is True
    assert position.terminal is True
    assert position.as_row()["exit_fee_per_contract"] == 0.0


@pytest.mark.parametrize("contracts", [0, -1, -0.5])
def test_rejects_non_positive_contract_quantities(contracts: float) -> None:
    position = group_logical_positions(
        [_paper_order(10, contracts=contracts)]
    )[0]

    assert "contracts must be positive on order 10" in position.integrity_findings
    _assert_invalid_aggregates_fail_closed(position)


@pytest.mark.parametrize(
    ("field", "contracts", "value"),
    [
        ("cost_per_contract", 1e308, 1e308),
        ("exit_price", 1e308, 1e308),
        ("exit_fee_per_contract", 1e308, 1e308),
    ],
)
def test_rejects_non_finite_derived_lot_values(
    field: str, contracts: float, value: float
) -> None:
    row = _paper_order(10, contracts=contracts)
    row["cost_per_contract"] = 1e-308
    row["exit_price"] = 1e-308
    row["exit_fee_per_contract"] = 1e-308
    row[field] = value

    position = group_logical_positions([row])[0]

    assert (
        f"non-finite contracts * {field} on order 10"
        in position.integrity_findings
    )
    _assert_invalid_aggregates_fail_closed(position)


def test_rejects_overflowed_aggregate_contracts() -> None:
    rows = [
        _paper_order(10, contracts=1e308, exit_price=1e-308),
        _paper_order(
            11,
            parent_order_id=10,
            contracts=1e308,
            exit_price=1e-308,
        ),
    ]
    for row in rows:
        row["cost_per_contract"] = 1e-308
        row["entry_price"] = 1e-308
        row["exit_fee_per_contract"] = None

    position = group_logical_positions(rows)[0]

    assert "non-finite aggregate contracts" in position.integrity_findings
    _assert_invalid_aggregates_fail_closed(position)


def test_rejects_overflowed_weighted_exit_numerator() -> None:
    rows = [
        _paper_order(10, contracts=1e308, exit_price=0.9),
        _paper_order(
            11,
            parent_order_id=10,
            contracts=1e308,
            exit_price=0.9,
        ),
    ]
    for row in rows:
        row["cost_per_contract"] = 1e-154
        row["entry_price"] = 1e-154
        row["exit_fee_per_contract"] = None

    position = group_logical_positions(rows)[0]

    assert "non-finite aggregate weighted exit_price" in position.integrity_findings
    _assert_invalid_aggregates_fail_closed(position)


def test_rejects_terminal_root_with_unresolved_child_lot() -> None:
    rows = [
        _paper_order(10),
        _paper_order(
            11,
            parent_order_id=10,
            status="PAPER_FILLED",
            realized_pnl=None,
            exit_price=None,
            closed_at=None,
        ),
    ]

    position = group_logical_positions(rows)[0]

    assert "child 11 is not terminal" in position.integrity_findings
    _assert_invalid_aggregates_fail_closed(position)


def test_retains_fractional_order_id_as_an_invalid_audit_group() -> None:
    position = group_logical_positions([_paper_order(10.5)])[0]

    assert isinstance(position.logical_order_id, int)
    assert position.root["id"] == 10.5
    assert "invalid order id 10.5" in position.integrity_findings
    _assert_invalid_aggregates_fail_closed(position)


@pytest.mark.parametrize("order_id", [0, -1, "-2"])
def test_rejects_non_positive_order_ids(order_id: object) -> None:
    position = group_logical_positions([_paper_order(order_id)])[0]

    assert f"invalid order id {order_id!r}" in position.integrity_findings
    _assert_invalid_aggregates_fail_closed(position)


def test_rejects_negative_parent_order_id() -> None:
    position = group_logical_positions(
        [_paper_order(10, parent_order_id="-2")]
    )[0]

    assert (
        "invalid parent_order_id '-2' on order 10"
        in position.integrity_findings
    )
    _assert_invalid_aggregates_fail_closed(position)


def test_rejects_duplicate_order_ids_without_dropping_either_row() -> None:
    position = group_logical_positions(
        [_paper_order(10), _paper_order(10, realized_pnl=0.24)]
    )[0]

    assert len(position.lots) == 2
    assert "duplicate order id 10" in position.integrity_findings
    _assert_invalid_aggregates_fail_closed(position)


def test_rejects_duplicate_order_id_across_root_and_child_groups() -> None:
    rows = [
        _paper_order(10),
        _paper_order(20),
        _paper_order(10, parent_order_id=20),
    ]

    forward = group_logical_positions(rows)
    reversed_positions = group_logical_positions(list(reversed(rows)))

    assert [position.logical_order_id for position in forward] == [10, 20]
    assert sum(len(position.lots) for position in forward) == 3
    for positions in (forward, reversed_positions):
        assert [position.logical_order_id for position in positions] == [10, 20]
        for position in positions:
            assert position.valid is False
            assert position.terminal is False
            assert "duplicate order id 10" in position.integrity_findings
            _assert_invalid_aggregates_fail_closed(position)


def test_treats_sub_tolerance_aggregate_pnl_as_undecided() -> None:
    position = group_logical_positions(
        [_paper_order(10, realized_pnl=-1e-10)]
    )[0]

    assert position.valid is True
    assert position.terminal is True
    assert position.won is None
    assert position.as_row()["realized_pnl"] == pytest.approx(-1e-10)
    assert position.as_row()["logical_outcome"] == "undecided"


def test_uses_precise_summation_for_aggregate_pnl() -> None:
    rows = [
        _paper_order(10, contracts=1, realized_pnl=1e16),
        _paper_order(11, parent_order_id=10, contracts=1, realized_pnl=1.0),
        _paper_order(12, parent_order_id=10, contracts=1, realized_pnl=-1e16),
    ]

    position = group_logical_positions(rows)[0]

    assert position.as_row()["realized_pnl"] == 1.0
    assert position.won is True


def test_treats_parent_order_id_zero_as_legacy_root_sentinel() -> None:
    position = group_logical_positions(
        [_paper_order(7, parent_order_id=0, realized_pnl=0.24)]
    )[0]

    assert position.logical_order_id == 7
    assert position.valid is True
    assert position.terminal is True
    assert position.child_order_ids == ()


def test_accepts_non_mapping_row_like_input() -> None:
    source = _paper_order(7, realized_pnl=0.24)

    position = group_logical_positions([_RowLike(source)])[0]

    assert position.valid is True
    assert position.terminal is True
    assert position.as_row()["realized_pnl"] == pytest.approx(0.24)


def test_does_not_mutate_caller_owned_mappings() -> None:
    rows = [_paper_order(10), _paper_order(11, parent_order_id=10)]
    original = deepcopy(rows)

    position = group_logical_positions(rows)[0]
    position.as_row()

    assert rows == original


def test_malformed_row_audit_ids_are_stable_across_input_permutations() -> None:
    first_row = _paper_order("bad-a")
    second_row = _paper_order("bad-b", market_ticker="SECOND")

    forward = group_logical_positions([first_row, second_row])
    reversed_positions = group_logical_positions([second_row, first_row])

    forward_ids = {
        position.root["id"]: position.logical_order_id for position in forward
    }
    reversed_ids = {
        position.root["id"]: position.logical_order_id
        for position in reversed_positions
    }
    assert forward_ids == reversed_ids
    assert [position.logical_order_id for position in forward] == [
        position.logical_order_id for position in reversed_positions
    ]


def test_duplicate_malformed_rows_are_each_preserved_once() -> None:
    malformed = _paper_order("bad-id")

    positions = group_logical_positions([malformed, deepcopy(malformed)])

    assert len(positions) == 2
    assert sum(len(position.lots) for position in positions) == 2


def test_sorts_shuffled_children_and_weights_exit_evidence_by_contracts() -> None:
    root = _paper_order(10, contracts=1, exit_price=0.8)
    child_11 = _paper_order(11, parent_order_id=10, contracts=2, exit_price=0.9)
    child_12 = _paper_order(12, parent_order_id=10, contracts=3, exit_price=0.7)
    root["exit_fee_per_contract"] = 0.01
    child_11["exit_fee_per_contract"] = 0.02
    child_12["exit_fee_per_contract"] = 0.03

    position = group_logical_positions([child_12, root, child_11])[0]
    projected = position.as_row()

    assert position.child_order_ids == (11, 12)
    assert projected["exit_price"] == pytest.approx(
        (1 * 0.8 + 2 * 0.9 + 3 * 0.7) / 6
    )
    assert projected["exit_fee_per_contract"] == pytest.approx(
        (1 * 0.01 + 2 * 0.02 + 3 * 0.03) / 6
    )


def test_weighted_exit_fee_uses_exit_fill_population_and_zero_missing_fee() -> None:
    root = _paper_order(10, contracts=1, exit_price=0.8)
    child = _paper_order(11, parent_order_id=10, contracts=3, exit_price=0.7)
    root["exit_fee_per_contract"] = 0.01
    child["exit_fee_per_contract"] = None

    projected = group_logical_positions([root, child])[0].as_row()

    assert projected["exit_price"] == pytest.approx((1 * 0.8 + 3 * 0.7) / 4)
    assert projected["exit_fee_per_contract"] == pytest.approx(0.0025)
