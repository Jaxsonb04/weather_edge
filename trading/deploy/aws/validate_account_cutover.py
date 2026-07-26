#!/usr/bin/env python3
"""Initialize and validate the two active post-restart paper ledgers."""

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
from sfo_kalshi_quant.research_policy import TARGET_POLICY


EXPECTED_ACTIVE_ACCOUNTS = {
    LIVE_STABILITY_ACCOUNT_ID,
    TARGET_POLICY.account_id,
}


def validate_account_cutover(db_path: Path) -> None:
    """Run migrations and fail unless exactly two fixed-capital ledgers are active."""

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
    if set(active) != EXPECTED_ACTIVE_ACCOUNTS:
        raise RuntimeError(
            "active paper account set does not match the restart policy"
        )
    for account_id, row in active.items():
        if not (
            math.isclose(
                float(row[2]),
                INITIAL_CAPITAL,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            and math.isclose(
                float(row[3]),
                INITIAL_CAPITAL,
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
    live = store._account_state(
        LIVE_STABILITY_ACCOUNT_ID,
        persist_high_water=False,
    )
    research = store._account_state(
        TARGET_POLICY.account_id,
        persist_high_water=False,
    )
    if live is None or research is None:
        raise RuntimeError("active paper account state is unavailable")
    for state in (live, research):
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
    print("OK: exactly two fixed-capital paper ledgers are active")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
