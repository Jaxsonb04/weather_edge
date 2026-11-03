#!/usr/bin/env python3
"""Initialize and validate the active paper ledgers.

Live Stability and Research V7 are the two post-restart ledgers ($1,000 each);
the M1 market-recalibration trial is a third, separately capitalized paper
ledger ($10,000) that never pools with either.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys


# The deployer invokes this file directly from ``trading/``. Python otherwise
# puts only ``trading/deploy/aws`` on sys.path, so bootstrap the project package
# root before importing the validator's runtime dependencies.
TRADING_ROOT = Path(__file__).resolve().parents[2]
if str(TRADING_ROOT) not in sys.path:
    sys.path.insert(0, str(TRADING_ROOT))

from sfo_kalshi_quant.account import INITIAL_CAPITAL, LIVE_STABILITY_ACCOUNT_ID
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.research_m1 import M1_POLICY
from sfo_kalshi_quant.research_policy import TARGET_POLICY


# account id -> fixed initial capital and opening cash.
EXPECTED_ACTIVE_CAPITAL = {
    LIVE_STABILITY_ACCOUNT_ID: INITIAL_CAPITAL,
    TARGET_POLICY.account_id: INITIAL_CAPITAL,
    M1_POLICY.account_id: M1_POLICY.reference_equity,
}
EXPECTED_ACTIVE_ACCOUNTS = set(EXPECTED_ACTIVE_CAPITAL)
REQUIRED_ACTIVE_ACCOUNTS = EXPECTED_ACTIVE_ACCOUNTS - {M1_POLICY.account_id}


def validate_account_cutover(db_path: Path) -> None:
    """Run migrations and fail unless Live and V7 (and M1 if active) are valid.

    A rollback past M1 archives its ledger and the roll-forward leaves it
    archived on purpose. Failing the deploy here would leave the host quiesced,
    so an archived M1 only warns: its admission already fails closed.
    """

    store = PaperStore(db_path)
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT account_id, status, initial_capital, opening_cash "
            "FROM paper_accounts"
        ).fetchall()
    active = {
        str(row[0]): row
        for row in rows
        if str(row[1]) == "ACTIVE"
    }
    if not REQUIRED_ACTIVE_ACCOUNTS <= set(active) <= EXPECTED_ACTIVE_ACCOUNTS:
        raise RuntimeError(
            "active paper account set does not match the restart policy"
        )
    if M1_POLICY.account_id not in active:
        print(
            f"WARNING: M1 trial ledger {M1_POLICY.account_id} is not active; "
            "M1 admission stays blocked",
            file=sys.stderr,
        )
    for account_id, row in active.items():
        capital = EXPECTED_ACTIVE_CAPITAL[account_id]
        if not (
            math.isclose(
                float(row[2]),
                capital,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            and math.isclose(
                float(row[3]),
                capital,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        ):
            raise RuntimeError(
                f"active paper account capital is invalid: {account_id}"
            )

    # Deployment validation must be observational. Persisting a high-water mark
    # before reconciliation could turn temporary ledger corruption into a
    # permanent false drawdown even though this command correctly aborts.
    states = [
        store._account_state(account_id, persist_high_water=False)
        for account_id in sorted(active)
    ]
    if any(state is None for state in states):
        raise RuntimeError("active paper account state is unavailable")
    for state in states:
        if state["status"] != "ACTIVE":
            raise RuntimeError("active paper account state changed during validation")
        reconciliation = store.account_order_ledger_reconciliation(
            str(state["account_id"])
        )
        if reconciliation["status"] != "reconciled":
            raise RuntimeError(
                "active paper account order ledger does not reconcile: "
                f"{state['account_id']}"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    args = parser.parse_args()
    validate_account_cutover(args.db)
    print(
        "OK: the fixed-capital paper ledgers are active "
        "(Live Stability, Research V7, M1 trial unless archived)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
