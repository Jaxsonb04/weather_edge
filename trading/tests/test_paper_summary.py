import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant.account import (
    LIVE_STABILITY_ACCOUNT_ID,
    RESEARCH_ACCOUNT_ID,
)
from sfo_kalshi_quant.config import SFO_TZ, StrategyConfig
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.research_policy import MOTION_POLICY, TARGET_POLICY
from sfo_kalshi_quant.summary import (
    build_paper_summary,
    write_paper_summary,
    write_paper_summary_csv,
)


def _decision(
    ticker: str,
    *,
    edge_lcb: float = 0.05,
    floor_strike: float = 66.0,
    cap_strike: float = 67.0,
) -> TradeDecision:
    return TradeDecision(
        ticker=ticker,
        label=f"{floor_strike:.0f}° to {cap_strike:.0f}°",
        action="BUY_YES",
        approved=True,
        probability=0.40,
        probability_lcb=0.30,
        yes_bid=0.20,
        yes_ask=0.25,
        spread=0.05,
        fee_per_contract=0.01,
        cost_per_contract=0.26,
        edge=0.14,
        edge_lcb=edge_lcb,
        kelly_fraction=0.01,
        recommended_contracts=10.0,
        expected_profit=1.4,
        reasons=[],
        strike_type="between",
        floor_strike=floor_strike,
        cap_strike=cap_strike,
    )


def _now_local() -> datetime:
    return datetime.now(UTC).astimezone(SFO_TZ)


def _seed_profile_order(
    store: PaperStore,
    target_date: str,
    decision: TradeDecision,
    *,
    risk_profile: str,
    account_id: str,
    research_sleeve: str | None = None,
    research_policy_version: str | None = None,
    policy_fingerprint: str | None = None,
) -> int:
    """Seed readable historical profile state without admitting archived risk."""

    order_id = store.record_paper_order(
        target_date,
        decision,
        risk_profile="live",
    )
    assert order_id is not None
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_orders SET risk_profile=?, account_id=?, "
            "research_sleeve=?, research_policy_version=?, policy_fingerprint=? "
            "WHERE id=?",
            (
                risk_profile,
                account_id,
                research_sleeve,
                research_policy_version,
                policy_fingerprint,
                order_id,
            ),
        )
        conn.execute(
            "UPDATE paper_account_ledger SET account_id=? WHERE order_id=?",
            (account_id, order_id),
        )
    return order_id


def test_paper_summary_attributes_pnl_to_resolution_day():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        forecaster_root = Path(tmp) / "forecaster"
        forecaster_root.mkdir()

        today = _now_local().date().isoformat()
        winner = store.record_paper_order(today, _decision("KXHIGHTSFO-TEST-B66.5"))
        loser = store.record_paper_order(
            today,
            _decision("KXHIGHTSFO-TEST-B68.5", floor_strike=68.0, cap_strike=69.0),
        )
        store.settle_paper_orders(today, 67.0)  # B66.5 YES wins, B68.5 YES loses

        payload = build_paper_summary(
            db_path=db_path,
            forecaster_root=forecaster_root,
            config=StrategyConfig(paper_bankroll=1000.0),
            days=7,
        )

        totals = payload["totals"]
        assert totals["trades_opened"] == 2
        assert totals["trades_settled"] == 2
        assert totals["wins"] == 1
        assert totals["losses"] == 1
        assert totals["hit_rate"] == 0.5
        assert payload["bankroll"] is None
        assert len(payload["days"]) == 7
        today_row = payload["days"][-1]
        assert today_row["date"] == today
        assert today_row["settled"] == 2
        assert today_row["realized_pnl"] != 0.0
        assert today_row["closing_equity"] is None
        assert today_row["closing_attributed_pnl"] == totals["cumulative_realized_pnl"]
        assert today_row["daily_realized_pnl"] == today_row["realized_pnl"]
        assert payload["biggest_winners"][0]["id"] == winner
        assert payload["biggest_losers"][0]["id"] == loser
        assert payload["learnings"]
        assert payload["recommended_changes"]


def test_combined_summary_reports_attribution_without_synthetic_account_equity():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        forecaster_root = Path(tmp) / "forecaster"
        forecaster_root.mkdir()
        today = _now_local().date().isoformat()

        order_ids = [
            _seed_profile_order(
                store,
                today,
                _decision("KXHIGHTSFO-LIVE-B66.5"),
                risk_profile="live",
                account_id=LIVE_STABILITY_ACCOUNT_ID,
            ),
            _seed_profile_order(
                store,
                today,
                _decision("KXHIGHTSFO-LEGACY-RESEARCH-B67.5"),
                risk_profile="research",
                account_id=RESEARCH_ACCOUNT_ID,
            ),
            _seed_profile_order(
                store,
                today,
                _decision("KXHIGHTSFO-TARGET-B68.5"),
                risk_profile="research",
                account_id=TARGET_POLICY.account_id,
                research_sleeve=TARGET_POLICY.sleeve.value,
                research_policy_version=TARGET_POLICY.policy_version,
                policy_fingerprint=TARGET_POLICY.policy_fingerprint,
            ),
            _seed_profile_order(
                store,
                today,
                _decision("KXHIGHTSFO-MOTION-B69.5"),
                risk_profile="research",
                account_id=MOTION_POLICY.account_id,
                research_sleeve=MOTION_POLICY.sleeve.value,
                research_policy_version=MOTION_POLICY.policy_version,
                policy_fingerprint=MOTION_POLICY.policy_fingerprint,
            ),
        ]
        assert all(order_id is not None for order_id in order_ids)

        resolved_at = _now_local().isoformat()
        account_identities = (
            (LIVE_STABILITY_ACCOUNT_ID, None, None, None),
            (RESEARCH_ACCOUNT_ID, None, None, None),
            (
                TARGET_POLICY.account_id,
                TARGET_POLICY.sleeve.value,
                TARGET_POLICY.policy_version,
                TARGET_POLICY.policy_fingerprint,
            ),
            (
                MOTION_POLICY.account_id,
                MOTION_POLICY.sleeve.value,
                MOTION_POLICY.policy_version,
                MOTION_POLICY.policy_fingerprint,
            ),
        )
        with store.connect() as conn:
            for order_id, identity, pnl in zip(
                order_ids, account_identities, (1.0, 2.0, 3.0, 4.0), strict=True
            ):
                conn.execute(
                    "UPDATE paper_orders SET account_id=?, research_sleeve=?, "
                    "research_policy_version=?, policy_fingerprint=?, "
                    "status='PAPER_SETTLED', realized_pnl=?, settled_at=? "
                    "WHERE id=?",
                    (*identity, pnl, resolved_at, order_id),
                )

        payload = build_paper_summary(
            db_path=db_path,
            forecaster_root=forecaster_root,
            config=StrategyConfig(paper_bankroll=1000.0),
            days=1,
        )

        assert payload["schema_version"] == 2
        assert {
            row["risk_profile"] for row in payload["profiles"]
        } == {"live", "research", "research-target", "research-motion"}
        assert payload["equity_available"] is False
        assert payload["equity_basis"] == "attribution_only"
        assert "separate paper accounts" in payload["equity_unavailable_reason"]
        assert payload["bankroll"] is None
        assert payload["starting_bankroll"] is None
        assert payload["current_equity"] is None
        day = payload["days"][0]
        assert day["opening_equity"] is None
        assert day["closing_equity"] is None
        assert day["opening_attributed_pnl"] == 0.0
        assert day["closing_attributed_pnl"] == 10.0
        assert day["cumulative_realized"] == 10.0


def test_paper_summary_handles_empty_database():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        PaperStore(db_path)
        forecaster_root = Path(tmp) / "forecaster"
        forecaster_root.mkdir()

        payload = build_paper_summary(
            db_path=db_path,
            forecaster_root=forecaster_root,
            days=3,
        )

        assert payload["totals"]["trades_opened"] == 0
        assert payload["totals"]["hit_rate"] is None
        assert payload["totals"]["roi"] is None
        assert len(payload["days"]) == 3
        assert any("No resolved trades" in note for note in payload["learnings"])


def test_paper_summary_includes_clean_forecast_error():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        PaperStore(db_path)
        forecaster_root = Path(tmp) / "forecaster"
        forecaster_root.mkdir()

        target = _now_local().date()
        fetched = (
            datetime.combine(target - timedelta(days=1), datetime.min.time(), tzinfo=SFO_TZ)
            + timedelta(hours=18)
        )
        with sqlite3.connect(forecaster_root / "weather.db") as conn:
            conn.execute(
                """
                CREATE TABLE forecast_blend_daily_high (
                    target_date TEXT,
                    predicted_high_f REAL,
                    actual_high_f REAL,
                    abs_error_f REAL,
                    fetched_at TEXT,
                    details_json TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO forecast_blend_daily_high VALUES (?, ?, ?, ?, ?, ?)",
                (target.isoformat(), 66.2, 68.0, 1.8, fetched.isoformat(), "{}"),
            )

        payload = build_paper_summary(
            db_path=db_path,
            forecaster_root=forecaster_root,
            days=7,
        )

        today_row = payload["days"][-1]
        assert today_row["forecast_predicted_high_f"] == 66.2
        assert today_row["forecast_actual_high_f"] == 68.0
        assert today_row["forecast_error_f"] == 1.8
        assert payload["totals"]["mean_abs_forecast_error_f"] == 1.8


def test_paper_summary_writers_produce_files():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        PaperStore(db_path)
        forecaster_root = Path(tmp) / "forecaster"
        forecaster_root.mkdir()

        payload = build_paper_summary(db_path=db_path, forecaster_root=forecaster_root, days=2)
        json_path = Path(tmp) / "out" / "summary.json"
        csv_path = Path(tmp) / "out" / "summary.csv"
        write_paper_summary(json_path, payload)
        write_paper_summary_csv(csv_path, payload)

        assert json_path.exists()
        text = csv_path.read_text()
        assert text.count("\n") >= 3  # header + 2 day rows
        assert "realized_pnl" in text


def test_paper_summary_splits_results_by_risk_profile():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        forecaster_root = Path(tmp) / "forecaster"
        forecaster_root.mkdir()

        today = _now_local().date().isoformat()
        store.record_paper_order(
            today, _decision("KXHIGHTSFO-TEST-B66.5"), risk_profile="live"
        )
        _seed_profile_order(
            store,
            today,
            _decision("KXHIGHTSFO-TEST-B68.5", floor_strike=68.0, cap_strike=69.0),
            risk_profile="research",
            account_id=RESEARCH_ACCOUNT_ID,
        )
        store.settle_paper_orders(today, 67.0)  # balanced wins, fast-feedback loses

        payload = build_paper_summary(
            db_path=db_path,
            forecaster_root=forecaster_root,
            config=StrategyConfig(paper_bankroll=1000.0),
            days=7,
        )

        profiles = {row["risk_profile"]: row for row in payload["profiles"]}
        assert set(profiles) == {"live", "research"}
        assert profiles["live"]["wins"] == 1
        assert profiles["live"]["realized_pnl"] > 0
        assert profiles["research"]["losses"] == 1
        assert profiles["research"]["realized_pnl"] < 0
        day_profiles = next(
            day["profiles"] for day in payload["days"] if day["date"] == today.split("T")[0]
        )
        assert day_profiles["live"]["realized_pnl"] > 0
        assert day_profiles["research"]["realized_pnl"] < 0


def test_paper_summary_counts_three_close_lots_as_one_logical_position():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        forecaster_root = Path(tmp) / "forecaster"
        forecaster_root.mkdir()

        local_now = _now_local()
        opened_date = local_now.date() - timedelta(days=2)
        first_close_date = opened_date
        second_close_date = opened_date + timedelta(days=1)
        final_close_date = opened_date + timedelta(days=2)
        root_id = store.record_paper_order(
            final_close_date.isoformat(),
            _decision("KXHIGHTPHX-TEST-B110.5"),
            risk_profile="live",
        )
        first_lot = store.close_paper_order(root_id, 0.70, max_quantity=2.0)
        second_lot = store.close_paper_order(root_id, 0.70, max_quantity=3.0)
        final_lot = store.close_paper_order(root_id, 0.70)

        def local_noon(day) -> str:
            return (
                datetime.combine(day, datetime.min.time(), tzinfo=SFO_TZ)
                + timedelta(hours=12)
            ).isoformat()

        close_days_by_id = {
            int(first_lot["id"]): first_close_date,
            int(second_lot["id"]): second_close_date,
            int(final_lot["id"]): final_close_date,
        }
        with store.connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "UPDATE paper_orders SET created_at=?, filled_at=? "
                "WHERE id=? OR parent_order_id=?",
                (
                    local_noon(opened_date),
                    local_noon(opened_date),
                    root_id,
                    root_id,
                ),
            )
            for order_id, close_day in close_days_by_id.items():
                conn.execute(
                    "UPDATE paper_orders SET closed_at=? WHERE id=?",
                    (local_noon(close_day), order_id),
                )
            lots = conn.execute(
                "SELECT id, contracts, cost_per_contract, realized_pnl, closed_at "
                "FROM paper_orders WHERE id=? OR parent_order_id=? ORDER BY id",
                (root_id, root_id),
            ).fetchall()

        expected_pnl = sum(float(lot["realized_pnl"]) for lot in lots)
        expected_capital = sum(
            float(lot["contracts"]) * float(lot["cost_per_contract"])
            for lot in lots
        )
        expected_by_day = {
            day.isoformat(): {
                "pnl": sum(
                    float(lot["realized_pnl"])
                    for lot in lots
                    if datetime.fromisoformat(lot["closed_at"]).date() == day
                ),
                "capital": sum(
                    float(lot["contracts"]) * float(lot["cost_per_contract"])
                    for lot in lots
                    if datetime.fromisoformat(lot["closed_at"]).date() == day
                ),
            }
            for day in (first_close_date, second_close_date, final_close_date)
        }

        payload = build_paper_summary(
            db_path=db_path,
            forecaster_root=forecaster_root,
            config=StrategyConfig(paper_bankroll=1000.0),
            days=7,
            now=local_now,
        )

        totals = payload["totals"]
        assert totals["trades_opened"] == 1
        assert totals["trades_closed"] == 1
        assert totals["trades_settled"] == 0
        assert totals["wins"] == 1
        assert totals["losses"] == 0
        assert totals["realized_pnl"] == round(expected_pnl, 2)
        assert totals["capital_resolved"] == round(expected_capital, 2)

        profile = next(row for row in payload["profiles"] if row["risk_profile"] == "live")
        assert profile["resolved"] == 1
        assert profile["wins"] == 1
        assert profile["losses"] == 0
        assert profile["realized_pnl"] == round(expected_pnl, 2)
        assert profile["capital_resolved"] == round(expected_capital, 2)

        yes_side = payload["side_performance"]["YES"]
        assert yes_side["trades"] == 1
        assert yes_side["wins"] == 1
        assert yes_side["losses"] == 0
        assert yes_side["realized_pnl"] == round(expected_pnl, 2)
        assert yes_side["capital"] == round(expected_capital, 2)
        assert payload["exit_reasons"]["closed_unclassified"] == 1

        assert len(payload["biggest_winners"]) == 1
        assert payload["biggest_winners"][0]["id"] == root_id
        assert payload["biggest_winners"][0]["contracts"] == 10.0
        assert payload["biggest_winners"][0]["realized_pnl"] == round(expected_pnl, 2)

        days = {row["date"]: row for row in payload["days"]}
        for close_date, expected in expected_by_day.items():
            assert days[close_date]["realized_pnl"] == round(expected["pnl"], 2)
            assert days[close_date]["resolved_spend"] == round(expected["capital"], 2)
        assert days[opened_date.isoformat()]["opened"] == 1
        assert days[first_close_date.isoformat()]["closed"] == 0
        assert days[second_close_date.isoformat()]["closed"] == 0
        assert days[final_close_date.isoformat()]["closed"] == 1
        assert days[final_close_date.isoformat()]["wins"] == 1


def test_paper_summary_learnings_use_window_lots_once_per_logical_position():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        forecaster_root = Path(tmp) / "forecaster"
        forecaster_root.mkdir()
        local_now = _now_local()
        before_window = local_now.date() - timedelta(days=2)
        today = local_now.date()

        live_root = store.record_paper_order(
            today.isoformat(),
            _decision("KXHIGHTPHX-WINDOW-LIVE-B110.5"),
            risk_profile="live",
        )
        live_old_lot = store.close_paper_order(live_root, 0.70, max_quantity=2.0)
        live_window_lot = store.close_paper_order(live_root, 0.50)

        research_root = _seed_profile_order(
            store,
            today.isoformat(),
            _decision("KXHIGHTPHX-WINDOW-RESEARCH-B112.5"),
            risk_profile="research",
            account_id=RESEARCH_ACCOUNT_ID,
        )
        research_window_lot = store.close_paper_order(
            research_root,
            0.10,
            max_quantity=2.0,
        )

        def local_noon(day: date) -> str:
            return datetime.combine(
                day,
                datetime.min.time(),
                tzinfo=SFO_TZ,
            ).replace(hour=12).isoformat()

        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET closed_at=? WHERE id=?",
                (local_noon(before_window), int(live_old_lot["id"])),
            )
            conn.executemany(
                "UPDATE paper_orders SET closed_at=? WHERE id=?",
                [
                    (local_noon(today), int(live_window_lot["id"])),
                    (local_noon(today), int(research_window_lot["id"])),
                ],
            )
            live_window_pnl = float(
                conn.execute(
                    "SELECT realized_pnl FROM paper_orders WHERE id=?",
                    (int(live_window_lot["id"]),),
                ).fetchone()[0]
            )
            research_window_pnl = float(
                conn.execute(
                    "SELECT realized_pnl FROM paper_orders WHERE id=?",
                    (int(research_window_lot["id"]),),
                ).fetchone()[0]
            )
            live_lifetime_pnl = float(
                conn.execute(
                    "SELECT SUM(realized_pnl) FROM paper_orders "
                    "WHERE id=? OR parent_order_id=?",
                    (live_root, live_root),
                ).fetchone()[0]
            )

        payload = build_paper_summary(
            db_path=db_path,
            forecaster_root=forecaster_root,
            config=StrategyConfig(paper_bankroll=1000.0),
            days=2,
            now=local_now,
        )

        profile_note = next(
            note for note in payload["learnings"] if note.startswith("Profile split:")
        )
        assert payload["totals"]["realized_pnl"] == round(
            live_window_pnl + research_window_pnl,
            2,
        )
        assert f"live 1/1 net positive (${live_window_pnl:+.2f})" in profile_note
        assert f"research 0/1 net positive (${research_window_pnl:+.2f})" in profile_note
        assert f"${live_lifetime_pnl:+.2f}" not in profile_note


def test_paper_summary_keeps_partially_realized_open_root_undecided():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        forecaster_root = Path(tmp) / "forecaster"
        forecaster_root.mkdir()

        local_now = _now_local()
        today = local_now.date().isoformat()
        root_id = store.record_paper_order(
            today,
            _decision("KXHIGHTPHX-TEST-B111.5"),
            risk_profile="live",
        )
        store.close_paper_order(root_id, 0.70, max_quantity=2.0)
        with store.connect() as conn:
            realized_pnl, capital = conn.execute(
                "SELECT SUM(realized_pnl), SUM(contracts * cost_per_contract) "
                "FROM paper_orders WHERE parent_order_id=?",
                (root_id,),
            ).fetchone()

        payload = build_paper_summary(
            db_path=db_path,
            forecaster_root=forecaster_root,
            config=StrategyConfig(paper_bankroll=1000.0),
            days=7,
            now=local_now,
        )

        totals = payload["totals"]
        assert totals["trades_opened"] == 1
        assert totals["trades_closed"] == 0
        assert totals["trades_settled"] == 0
        assert totals["open_positions"] == 1
        assert totals["wins"] == 0
        assert totals["losses"] == 0
        assert totals["hit_rate"] is None
        assert totals["realized_pnl"] == round(realized_pnl, 2)
        assert totals["capital_resolved"] == round(capital, 2)

        profile = next(row for row in payload["profiles"] if row["risk_profile"] == "live")
        assert profile["resolved"] == 0
        assert profile["wins"] == 0
        assert profile["losses"] == 0
        assert profile["realized_pnl"] == round(realized_pnl, 2)
        assert profile["capital_resolved"] == round(capital, 2)

        yes_side = payload["side_performance"]["YES"]
        assert yes_side["trades"] == 0
        assert yes_side["wins"] == 0
        assert yes_side["losses"] == 0
        assert yes_side["realized_pnl"] == round(realized_pnl, 2)
        assert yes_side["capital"] == round(capital, 2)
        assert sum(payload["exit_reasons"].values()) == 0
        assert payload["biggest_winners"] == []
        assert payload["biggest_losers"] == []

        today_row = payload["days"][-1]
        assert today_row["date"] == today
        assert today_row["opened"] == 1
        assert today_row["closed"] == 0
        assert today_row["settled"] == 0
        assert today_row["wins"] == 0
        assert today_row["losses"] == 0
        assert today_row["realized_pnl"] == round(realized_pnl, 2)
        assert today_row["resolved_spend"] == round(capital, 2)


def test_paper_summary_excludes_malformed_exit_evidence_without_crashing():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        forecaster_root = Path(tmp) / "forecaster"
        forecaster_root.mkdir()

        today = _now_local().date().isoformat()
        order_id = store.record_paper_order(
            today,
            _decision("KXHIGHTPHX-TEST-B112.5"),
            risk_profile="live",
        )
        store.close_paper_order(order_id, 0.70)
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders "
                "SET exit_price='not-a-price', "
                "exit_fee_per_contract='not-a-fee' WHERE id=?",
                (order_id,),
            )

        payload = build_paper_summary(
            db_path=db_path,
            forecaster_root=forecaster_root,
            config=StrategyConfig(paper_bankroll=1000.0),
            days=7,
        )

        assert payload["totals"]["trades_opened"] == 0
        assert payload["totals"]["trades_closed"] == 0
        assert payload["totals"]["wins"] == 0
        assert payload["totals"]["realized_pnl"] == 0.0
        assert payload["totals"]["capital_resolved"] == 0.0
        assert payload["current_equity"] is None
        assert payload["days"][-1]["closing_attributed_pnl"] == 0.0
