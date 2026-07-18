"""Canonical decision-level projection for paper execution lots."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from math import fsum, isfinite
from typing import Any


TERMINAL_STATUSES = frozenset({"PAPER_CLOSED", "PAPER_SETTLED"})
OPEN_STATUSES = frozenset(
    {
        "PAPER_FILLED",
        "PAPER_PARTIALLY_FILLED",
        "PAPER_PARTIAL_EXPIRED",
    }
)

LOGICAL_IDENTITY_FIELDS = (
    "market_ticker",
    "target_date",
    "side",
    "risk_profile",
    "account_id",
    "research_sleeve",
    "research_policy_version",
    "policy_fingerprint",
    "strategy_fingerprint",
    "execution_model_version",
)
_EXACT_MATCH_FIELDS = LOGICAL_IDENTITY_FIELDS
_NUMERIC_MATCH_FIELDS = ("entry_price", "cost_per_contract")
_OPTIONAL_NUMERIC_FIELDS = ("exit_price", "exit_fee_per_contract")
_NUMERIC_TOLERANCE = 1e-9


def _copy_row(row: object) -> dict[str, Any]:
    """Copy a mapping or sqlite3.Row-style object without importing sqlite3."""

    if isinstance(row, Mapping):
        return dict(row)
    keys = getattr(row, "keys", None)
    if not callable(keys):
        raise TypeError("paper order rows must be mapping-like objects")
    return {str(key): row[key] for key in keys()}  # type: ignore[index]


def _canonical_value(value: object) -> tuple[Any, ...]:
    """Return a stable, fully comparable representation of row content."""

    if isinstance(value, Mapping):
        return (
            "mapping",
            tuple(
                sorted(
                    (str(key), _canonical_value(item))
                    for key, item in value.items()
                )
            ),
        )
    if isinstance(value, (list, tuple)):
        return (
            "sequence",
            type(value).__qualname__,
            tuple(_canonical_value(item) for item in value),
        )
    if isinstance(value, (set, frozenset)):
        items = [_canonical_value(item) for item in value]
        return "set", tuple(sorted(items))
    if isinstance(value, float):
        if value != value:
            return "scalar", "float", "nan"
        if value == float("inf"):
            return "scalar", "float", "inf"
        if value == float("-inf"):
            return "scalar", "float", "-inf"
    return (
        "scalar",
        f"{type(value).__module__}.{type(value).__qualname__}",
        repr(value),
    )


def _integral_value(value: object) -> int | None:
    """Parse an integer without truncating fractional evidence."""

    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def _integral_id(value: object) -> int | None:
    parsed = _integral_value(value)
    return parsed if parsed is not None and parsed > 0 else None


def _parent_id(row: Mapping[str, Any]) -> tuple[bool, int | None]:
    raw_parent = row.get("parent_order_id")
    if raw_parent is None:
        return True, None
    parsed = _integral_value(raw_parent)
    if parsed is None:
        return False, None
    # Historical exports used both NULL and 0 for a row with no parent.
    if parsed == 0:
        return True, None
    return (True, parsed) if parsed > 0 else (False, None)


def _finite_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if isfinite(parsed) else None


def _required_finite_number(value: object) -> float:
    parsed = _finite_number(value)
    if parsed is None:
        raise ValueError("required numeric evidence is not finite")
    return parsed


def _numeric_values_match(left: object, right: object) -> bool:
    left_number = _finite_number(left)
    right_number = _finite_number(right)
    return (
        left_number is not None
        and right_number is not None
        and abs(left_number - right_number) <= _NUMERIC_TOLERANCE
    )


def _safe_product(left: float, right: float) -> float | None:
    try:
        result = left * right
    except OverflowError:
        return None
    return result if isfinite(result) else None


def _safe_fsum(values: Iterable[float]) -> float | None:
    try:
        result = fsum(values)
    except (OverflowError, ValueError):
        return None
    return result if isfinite(result) else None


@dataclass(frozen=True)
class _NormalizedLot:
    row: Mapping[str, Any]
    contracts: float
    realized_pnl: float | None
    exit_price: float | None
    capital: float
    exit_notional: float | None
    exit_fee_notional: float


@dataclass(frozen=True)
class _AggregateProjection:
    contracts: float
    resolved_contracts: float
    realized_pnl: float | None
    capital_resolved: float
    exit_price: float | None
    exit_fee_per_contract: float | None


def _row_evidence_label(row: Mapping[str, Any]) -> str:
    order_id = _integral_id(row.get("id"))
    return str(order_id) if order_id is not None else repr(row.get("id"))


def _normalize_lot(
    lot: Mapping[str, Any],
) -> tuple[_NormalizedLot | None, tuple[str, ...]]:
    findings: list[str] = []

    def add_finding(finding: str) -> None:
        if finding not in findings:
            findings.append(finding)

    label = _row_evidence_label(lot)
    if _integral_id(lot.get("id")) is None:
        add_finding(f"invalid order id {lot.get('id')!r}")
    parent_valid, _ = _parent_id(lot)
    if not parent_valid:
        add_finding(
            "invalid parent_order_id "
            f"{lot.get('parent_order_id')!r} on order {label}"
        )

    numeric_valid = True

    def required_number(field: str) -> float | None:
        nonlocal numeric_valid
        raw_value = lot.get(field)
        value = _finite_number(raw_value)
        if value is None:
            qualifier = "missing" if raw_value is None else "invalid"
            add_finding(f"{qualifier} {field} on order {label}")
            numeric_valid = False
        return value

    contracts = required_number("contracts")
    if contracts is not None and contracts <= 0:
        add_finding(f"contracts must be positive on order {label}")
        numeric_valid = False
    entry_price = required_number("entry_price")
    if entry_price is not None and not 0 < entry_price < 1:
        add_finding(f"entry_price must be > 0 and < 1 on order {label}")
        numeric_valid = False
    cost_per_contract = required_number("cost_per_contract")
    if cost_per_contract is not None and cost_per_contract <= 0:
        add_finding(f"cost_per_contract must be positive on order {label}")
        numeric_valid = False

    raw_pnl = lot.get("realized_pnl")
    realized_pnl = _finite_number(raw_pnl)
    if raw_pnl is not None and realized_pnl is None:
        add_finding(f"invalid realized_pnl on order {label}")
        numeric_valid = False
    elif raw_pnl is None and lot.get("status") in TERMINAL_STATUSES:
        add_finding(f"missing realized_pnl on order {label}")
        numeric_valid = False

    optional_values: dict[str, float | None] = {}
    for field in _OPTIONAL_NUMERIC_FIELDS:
        raw_value = lot.get(field)
        value = _finite_number(raw_value)
        if raw_value is not None and value is None:
            add_finding(f"invalid {field} on order {label}")
            numeric_valid = False
        optional_values[field] = value
    exit_price = optional_values["exit_price"]
    if exit_price is not None and not 0 < exit_price < 1:
        add_finding(f"exit_price must be > 0 and < 1 on order {label}")
        numeric_valid = False
    exit_fee = optional_values["exit_fee_per_contract"]
    if exit_fee is not None and exit_fee < 0:
        add_finding(
            f"exit_fee_per_contract must be nonnegative on order {label}"
        )
        numeric_valid = False

    capital: float | None = None
    exit_notional: float | None = None
    exit_fee_notional = 0.0
    if contracts is not None and contracts > 0:
        product_fields = {
            "cost_per_contract": cost_per_contract,
            "exit_price": optional_values["exit_price"],
            "exit_fee_per_contract": optional_values["exit_fee_per_contract"],
        }
        products: dict[str, float] = {}
        for field, value in product_fields.items():
            if value is None:
                continue
            product = _safe_product(contracts, value)
            if product is None:
                add_finding(
                    f"non-finite contracts * {field} on order {label}"
                )
                numeric_valid = False
            else:
                products[field] = product
        capital = products.get("cost_per_contract")
        exit_notional = products.get("exit_price")
        exit_fee_notional = products.get("exit_fee_per_contract", 0.0)

    if (
        not numeric_valid
        or contracts is None
        or cost_per_contract is None
        or capital is None
    ):
        return None, tuple(findings)
    return (
        _NormalizedLot(
            row=lot,
            contracts=contracts,
            realized_pnl=realized_pnl,
            exit_price=optional_values["exit_price"],
            capital=capital,
            exit_notional=exit_notional,
            exit_fee_notional=exit_fee_notional,
        ),
        tuple(findings),
    )


def _aggregate_projection(
    lots: tuple[dict[str, Any], ...],
) -> tuple[_AggregateProjection | None, tuple[str, ...]]:
    findings: list[str] = []

    def add_finding(finding: str) -> None:
        if finding not in findings:
            findings.append(finding)

    normalized_lots: list[_NormalizedLot] = []
    for lot in lots:
        normalized, lot_findings = _normalize_lot(lot)
        for finding in lot_findings:
            add_finding(finding)
        if normalized is not None:
            normalized_lots.append(normalized)

    order_ids = [
        order_id
        for lot in lots
        if (order_id := _integral_id(lot.get("id"))) is not None
    ]
    for order_id, count in Counter(order_ids).items():
        if count > 1:
            add_finding(f"duplicate order id {order_id}")

    if len(normalized_lots) != len(lots):
        return None, tuple(findings)

    aggregate_contracts = _safe_fsum(
        lot.contracts for lot in normalized_lots
    )
    if aggregate_contracts is None:
        add_finding("non-finite aggregate contracts")

    resolved_lots = [
        lot
        for lot in normalized_lots
        if lot.row.get("status") in TERMINAL_STATUSES
        and lot.realized_pnl is not None
    ]
    resolved_contracts = _safe_fsum(lot.contracts for lot in resolved_lots)
    if resolved_contracts is None:
        add_finding("non-finite aggregate resolved_contracts")
    realized_pnl = (
        _safe_fsum(
            lot.realized_pnl
            for lot in resolved_lots
            if lot.realized_pnl is not None
        )
        if resolved_lots
        else None
    )
    if resolved_lots and realized_pnl is None:
        add_finding("non-finite aggregate realized_pnl")
    capital_resolved = _safe_fsum(lot.capital for lot in resolved_lots)
    if capital_resolved is None:
        add_finding("non-finite aggregate capital_resolved")

    exit_lots = [lot for lot in resolved_lots if lot.exit_price is not None]
    exit_price: float | None = None
    exit_fee_per_contract: float | None = None
    if exit_lots:
        exit_weight = _safe_fsum(lot.contracts for lot in exit_lots)
        exit_notional = _safe_fsum(
            lot.exit_notional
            for lot in exit_lots
            if lot.exit_notional is not None
        )
        exit_fee_notional = _safe_fsum(
            lot.exit_fee_notional for lot in exit_lots
        )
        if exit_weight is None or exit_notional is None:
            add_finding("non-finite aggregate weighted exit_price")
        else:
            exit_price = exit_notional / exit_weight
            if not isfinite(exit_price):
                add_finding("non-finite aggregate weighted exit_price")
        if exit_weight is None or exit_fee_notional is None:
            add_finding("non-finite aggregate weighted exit_fee_per_contract")
        else:
            exit_fee_per_contract = exit_fee_notional / exit_weight
            if not isfinite(exit_fee_per_contract):
                add_finding(
                    "non-finite aggregate weighted exit_fee_per_contract"
                )

    if findings:
        return None, tuple(findings)
    assert aggregate_contracts is not None
    assert resolved_contracts is not None
    assert capital_resolved is not None
    return (
        _AggregateProjection(
            contracts=aggregate_contracts,
            resolved_contracts=resolved_contracts,
            realized_pnl=realized_pnl,
            capital_resolved=capital_resolved,
            exit_price=exit_price,
            exit_fee_per_contract=exit_fee_per_contract,
        ),
        (),
    )


def _position_intrinsic_validation(
    root: dict[str, Any], lots: tuple[dict[str, Any], ...]
) -> tuple[_AggregateProjection | None, tuple[str, ...]]:
    aggregate, findings = _aggregate_projection(lots)
    merged_findings = list(findings)
    if not any(lot is root for lot in lots):
        _, root_findings = _normalize_lot(root)
        for finding in root_findings:
            if finding not in merged_findings:
                merged_findings.append(finding)
    return aggregate, tuple(merged_findings)


def _relationship_findings(
    logical_order_id: int,
    root: Mapping[str, Any],
    lots: tuple[dict[str, Any], ...],
    *,
    referenced_parent_ids: set[int] | None = None,
) -> tuple[str, ...]:
    """Validate one root/children structure for grouped and direct callers."""

    findings: list[str] = []

    def add_finding(finding: str) -> None:
        if finding not in findings:
            findings.append(finding)

    root_id = _integral_id(root.get("id"))
    if root_id is None:
        return ()
    if (
        not isinstance(logical_order_id, int)
        or isinstance(logical_order_id, bool)
        or logical_order_id != root_id
    ):
        add_finding(
            f"logical_order_id {logical_order_id} "
            f"does not match root order {root_id}"
        )
    root_parent_valid, root_parent_id = _parent_id(root)
    if not root_parent_valid or root_parent_id is not None:
        add_finding(f"root order {root_id} is not an actual root")

    root_occurrences = sum(lot is root for lot in lots)
    if root_occurrences == 0:
        add_finding(f"root order {root_id} missing from lots")
    elif root_occurrences > 1:
        add_finding(f"root order {root_id} appears multiple times in lots")

    matching_roots = [
        lot
        for lot in lots
        if _integral_id(lot.get("id")) == root_id
        and _parent_id(lot) == (True, None)
    ]
    if len(matching_roots) > 1:
        add_finding(f"multiple root rows for order {root_id}")

    local_parent_ids = {
        parent_id
        for lot in lots
        if (parent_result := _parent_id(lot))[0]
        and (parent_id := parent_result[1]) is not None
    }
    parent_ids = (
        referenced_parent_ids
        if referenced_parent_ids is not None
        else local_parent_ids
    )
    for lot in lots:
        if lot is root:
            continue
        child_label = _row_evidence_label(lot)
        parent_valid, parent_id = _parent_id(lot)
        if not parent_valid or parent_id != root_id:
            raw_parent = lot.get("parent_order_id")
            displayed_parent = (
                str(parent_id)
                if parent_valid and parent_id is not None
                else repr(raw_parent)
            )
            add_finding(
                f"child {child_label} references parent order "
                f"{displayed_parent}, expected {root_id}"
            )
        if lot.get("status") not in TERMINAL_STATUSES:
            add_finding(f"child {child_label} is not terminal")

        child_id = _integral_id(lot.get("id"))
        if child_id is not None and child_id in parent_ids:
            add_finding(f"child {child_id} is parent of another child")
        for field in _EXACT_MATCH_FIELDS:
            if lot.get(field) != root.get(field):
                add_finding(f"{field} mismatch on child {child_label}")
        for field in _NUMERIC_MATCH_FIELDS:
            if not _numeric_values_match(lot.get(field), root.get(field)):
                add_finding(f"{field} mismatch on child {child_label}")
    return tuple(findings)


def _latest_resolved_at(lots: Iterable[Mapping[str, Any]]) -> object | None:
    resolved_times = [
        resolved_at
        for lot in lots
        if (resolved_at := lot.get("closed_at") or lot.get("settled_at"))
        is not None
    ]
    if not resolved_times:
        return None
    try:
        return max(resolved_times)
    except TypeError:
        return None


@dataclass(frozen=True)
class LogicalPaperPosition:
    """One originating paper order and all execution lots attributed to it."""

    logical_order_id: int
    root: dict[str, Any]
    lots: tuple[dict[str, Any], ...]
    integrity_findings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _, intrinsic_findings = _position_intrinsic_validation(
            self.root, self.lots
        )
        root_already_missing = any(
            finding == f"missing root order {self.logical_order_id}"
            or finding
            == f"parent order {self.logical_order_id} is itself a child"
            for finding in self.integrity_findings
        )
        relationship_findings = (
            ()
            if root_already_missing
            else _relationship_findings(
                self.logical_order_id, self.root, self.lots
            )
        )
        merged_findings = tuple(
            dict.fromkeys(
                (
                    *self.integrity_findings,
                    *intrinsic_findings,
                    *relationship_findings,
                )
            )
        )
        object.__setattr__(self, "integrity_findings", merged_findings)

    @property
    def valid(self) -> bool:
        return not self.integrity_findings

    @property
    def terminal(self) -> bool:
        root_id = _integral_id(self.root.get("id"))
        parent_valid, parent_id = _parent_id(self.root)
        return (
            self.valid
            and root_id == self.logical_order_id
            and parent_valid
            and parent_id is None
            and self.root.get("status") in TERMINAL_STATUSES
            and _finite_number(self.root.get("realized_pnl")) is not None
        )

    @property
    def child_order_ids(self) -> tuple[int, ...]:
        child_ids: list[int] = []
        for lot in self.lots:
            parent_valid, parent_id = _parent_id(lot)
            order_id = _integral_id(lot.get("id"))
            if parent_valid and parent_id is not None and order_id is not None:
                child_ids.append(order_id)
        return tuple(sorted(child_ids))

    @property
    def resolved_lots(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            lot
            for lot in self.lots
            if lot.get("status") in TERMINAL_STATUSES
            and _finite_number(lot.get("realized_pnl")) is not None
        )

    @property
    def won(self) -> bool | None:
        if not self.terminal:
            return None
        aggregate, _ = _position_intrinsic_validation(self.root, self.lots)
        if aggregate is None or aggregate.realized_pnl is None:
            return None
        realized_pnl = aggregate.realized_pnl
        if abs(realized_pnl) <= _NUMERIC_TOLERANCE:
            return None
        return realized_pnl > 0

    def as_row(self) -> dict[str, Any]:
        """Return root attributes plus decision-level and lot-level aggregates."""

        projected = dict(self.root)
        projected.update(
            {
                "logical_order_id": self.logical_order_id,
                "parent_order_id": None,
                "child_order_ids": list(self.child_order_ids),
                "integrity_findings": list(self.integrity_findings),
            }
        )
        aggregate, _ = _position_intrinsic_validation(self.root, self.lots)
        if not self.valid or aggregate is None:
            projected.update(
                {
                    "contracts": None,
                    "resolved_contracts": None,
                    "open_contracts": None,
                    "resolved_lot_count": None,
                    "exit_fill_count": None,
                    "realized_pnl": None,
                    "capital_resolved": None,
                    "exit_price": None,
                    "exit_fee_per_contract": None,
                    "latest_resolved_at": None,
                    "logical_outcome": "undecided",
                }
            )
            return projected

        resolved_lots = self.resolved_lots
        won = self.won
        projected.update(
            {
                "contracts": aggregate.contracts,
                "resolved_contracts": aggregate.resolved_contracts,
                "open_contracts": (
                    0.0
                    if self.terminal
                    else _finite_number(self.root.get("contracts"))
                ),
                "resolved_lot_count": len(resolved_lots),
                "exit_fill_count": sum(
                    lot.get("exit_price") is not None
                    and _required_finite_number(lot.get("contracts")) > 0
                    for lot in resolved_lots
                ),
                "realized_pnl": aggregate.realized_pnl,
                "capital_resolved": aggregate.capital_resolved,
                "exit_price": aggregate.exit_price,
                "exit_fee_per_contract": aggregate.exit_fee_per_contract,
                "latest_resolved_at": _latest_resolved_at(resolved_lots),
                "logical_outcome": (
                    "win"
                    if won is True
                    else "loss"
                    if won is False
                    else "undecided"
                ),
            }
        )
        return projected


@dataclass(frozen=True)
class _PreparedRow:
    row: dict[str, Any]
    order_id: int | None
    parent_valid: bool
    parent_id: int | None
    logical_order_id: int


def _prepared_sort_key(prepared: _PreparedRow) -> tuple[int, int | str]:
    if prepared.order_id is not None:
        return 0, prepared.order_id
    return 1, repr(prepared.row.get("id"))


def group_logical_positions(rows: Iterable[object]) -> list[LogicalPaperPosition]:
    """Group immutable paper-order rows into auditable logical positions."""

    copied_rows = [_copy_row(row) for row in rows]
    parsed_rows = [
        (row, _integral_id(row.get("id")), *_parent_id(row))
        for row in copied_rows
    ]
    synthetic_indexes = [
        index
        for index, (_, order_id, parent_valid, parent_id) in enumerate(
            parsed_rows
        )
        if not parent_valid or (order_id is None and parent_id is None)
    ]
    synthetic_ids = {
        index: -rank
        for rank, index in enumerate(
            sorted(
                synthetic_indexes,
                key=lambda index: _canonical_value(parsed_rows[index][0]),
            ),
            start=1,
        )
    }

    prepared_rows: list[_PreparedRow] = []
    synthetic_group_ids = set(synthetic_ids.values())
    for index, (row, order_id, parent_valid, parent_id) in enumerate(parsed_rows):
        if index in synthetic_ids:
            logical_order_id = synthetic_ids[index]
        elif parent_id is not None:
            logical_order_id = parent_id
        else:
            # order_id cannot be None here because that case received a
            # synthetic audit identifier above.
            assert order_id is not None
            logical_order_id = order_id
        prepared_rows.append(
            _PreparedRow(
                row=row,
                order_id=order_id,
                parent_valid=parent_valid,
                parent_id=parent_id,
                logical_order_id=logical_order_id,
            )
        )

    rows_by_id: dict[int, list[_PreparedRow]] = {}
    groups: dict[int, list[_PreparedRow]] = {}
    for prepared in prepared_rows:
        if prepared.order_id is not None:
            rows_by_id.setdefault(prepared.order_id, []).append(prepared)
        groups.setdefault(prepared.logical_order_id, []).append(prepared)
    parent_ids = {
        prepared.parent_id
        for prepared in prepared_rows
        if prepared.parent_valid and prepared.parent_id is not None
    }
    global_id_counts = Counter(
        prepared.order_id
        for prepared in prepared_rows
        if prepared.order_id is not None
    )
    globally_duplicated_ids = {
        order_id for order_id, count in global_id_counts.items() if count > 1
    }

    positions: list[LogicalPaperPosition] = []
    for logical_order_id in sorted(groups):
        group_rows = groups[logical_order_id]
        findings: list[str] = []

        def add_finding(finding: str) -> None:
            if finding not in findings:
                findings.append(finding)

        for order_id in sorted(
            {
                prepared.order_id
                for prepared in group_rows
                if prepared.order_id in globally_duplicated_ids
            }
        ):
            add_finding(f"duplicate order id {order_id}")

        actual_roots = sorted(
            (
                prepared
                for prepared in group_rows
                if prepared.order_id == logical_order_id
                and prepared.parent_valid
                and prepared.parent_id is None
            ),
            key=_prepared_sort_key,
        )
        if len(actual_roots) == 1:
            root = actual_roots[0]
        elif len(actual_roots) > 1:
            root = actual_roots[0]
            add_finding(f"duplicate root order {logical_order_id}")
        else:
            root = min(group_rows, key=_prepared_sort_key)
            if logical_order_id not in synthetic_group_ids:
                referenced_rows = rows_by_id.get(logical_order_id, [])
                if referenced_rows and any(
                    prepared.parent_valid and prepared.parent_id is not None
                    for prepared in referenced_rows
                ):
                    add_finding(
                        f"parent order {logical_order_id} is itself a child"
                    )
                else:
                    add_finding(f"missing root order {logical_order_id}")

        other_rows = sorted(
            (prepared for prepared in group_rows if prepared is not root),
            key=_prepared_sort_key,
        )
        position_lots = (
            root.row,
            *(prepared.row for prepared in other_rows),
        )
        if actual_roots:
            for finding in _relationship_findings(
                logical_order_id,
                root.row,
                position_lots,
                referenced_parent_ids=parent_ids,
            ):
                add_finding(finding)
        positions.append(
            LogicalPaperPosition(
                logical_order_id=logical_order_id,
                root=root.row,
                lots=position_lots,
                integrity_findings=tuple(findings),
            )
        )

    return positions
