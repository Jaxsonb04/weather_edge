from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Iterable

from ._util import (
    _row_value as _shared_row_value,
)
from .config import StrategyConfig, normalize_risk_profile_name
from .account import (
    ACCOUNTING_POLICY_VERSION,
    INITIAL_CAPITAL,
    RESEARCH_ACCOUNT_ID,
    RESEARCH_VIRTUAL_CAPITAL,
    SHARED_ACCOUNT_ID,
    account_for_profile,
    policy_capacity,
    sleeve_for,
    strategy_fingerprint,
)
from .consensus import MarketConsensus
from .fees import (
    contracts_for_budget,
    quadratic_fee_average_per_contract,
    quadratic_fee_per_contract,
)
from .maker_fills import (
    EXECUTION_MODEL_VERSION,
    PublicAggressorTrade,
    RestingMakerOrder,
    allocate_maker_fills,
    apply_volume_claims,
    depth_observation_is_contemporaneous,
    normalize_public_trade,
)
from .models import BucketProbability, EventSnapshot, ForecastSnapshot, IntradaySnapshot, TradeDecision
from .prediction_features import build_prediction_feature_snapshot
from .settlement_truth import (
    integer_settlement_high_f as _integer_settlement_high_f,
    is_pre_resolution_decision as _is_pre_resolution_decision,
    normalize_settlement_truth,
    row_resolves_yes as _row_resolves_yes,
    settlement_for_market,
)
from .store.diagnostics import (
    _decision_diagnostics_payload,
    _decision_signal_payload,
    _entry_decision_ref_payload,
    _forecast_diagnostics_payload,
    _forecast_observed_high_mode,
    _intraday_diagnostics_payload,
    _json_optional_object,
    _json_text,
    _latest_entry_decision_snapshot,
    _market_close_time,
    _market_consensus_diagnostics_payload,
    _market_diagnostics_from_snapshot_json,
    _market_diagnostics_payload,
    _monitor_diagnostics_payload,
    _order_entry_diagnostics_payload,
    _order_entry_snapshot,
    _outcome_diagnostics_payload,
    _row_side,
    _strategy_config_snapshot,
    _win_loss_reason,
)
from .store.schema import (
    DECISION_AUDIT_COLUMNS,
    DECISION_SNAPSHOT_REPORT_INDEX,
    DECISION_SNAPSHOT_SAMPLE_INDEX,
    INDEXES,
    MONITOR_AUDIT_COLUMNS,
    OPEN_POSITION_GUARD_INDEX,
    PAPER_ORDER_AUDIT_COLUMNS,
    PROBABILITY_AUDIT_COLUMNS,
    RESEARCH_SHADOW_AUDIT_COLUMNS,
    SCAN_CONTEXT_AUDIT_COLUMNS,
    SCHEMA,
    _add_missing_columns,
    _migrate_legacy_profile_names,
    ensure_open_position_guard_index,
    init_store,
)
from .store.scoring import (
    _date_filters,
    _decision_row_pnl,
    _decision_row_position_won,
    _entry_decision_rows,
    _probability_stream_metrics,
    _quality_buckets,
    _row_optional_float,
    _row_position_decided,
    _row_position_won,
    _row_sort_time,
    _safe_div,
    _sample_decision_rows,
    market_backtest_summary,
    sampled_decision_rows,
    signal_backtest_summary,
)

logger = logging.getLogger(__name__)
_decision_row_resolves_yes = _row_resolves_yes
_row_value = partial(_shared_row_value, default_on_none=True)


# Fixed-PST settlement clock (UTC-8 year round) used for the daily-loss window so
# the breaker measures loss on the same day math the rest of trading settles on.
SETTLEMENT_TZ = timezone(timedelta(hours=-8))

# Rolling window (days) for the resolved-ROI circuit breaker, so a bad early
# cohort ages out and the pause can clear instead of latching off forever.
PAUSE_LOOKBACK_DAYS = 21

# Per-profile entry circuit breaker: (min_resolved_trades, max_resolved_roi,
# daily_loss_pct of bankroll). Keyed by the NORMALIZED profile name, so a key
# missing here silently disables the breaker -- both surviving profiles must be
# present. `research` keeps the tighter, earlier-tripping breaker of the two
# former collectors (fast-feedback's), since it is the tiny, loosest-gated book;
# `live` keeps the trading-intent breaker (the old balanced thresholds).
PAUSE_THRESHOLDS = {
    "live": (10, -0.35, 0.010),
    "research": (5, -0.25, 0.005),
}

class PaperStore:
    def __init__(self, db_path: Path, *, init: bool = True) -> None:
        self.db_path = Path(db_path)
        if init:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.init()

    def connect(self) -> sqlite3.Connection:
        # Five+ systemd units (scan, monitor, settle, strategy-lab, forecaster)
        # touch this database concurrently. WAL plus a real busy_timeout lets
        # readers and a single writer coexist instead of failing fast with
        # "database is locked" on the default 5s rollback-journal connection.
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.DatabaseError:
            # Non-file databases (e.g. :memory:) ignore WAL; never block init.
            pass
        return conn

    def init(self) -> None:
        init_store(self)

    def _ensure_open_position_guard_index(self, conn: sqlite3.Connection) -> None:
        ensure_open_position_guard_index(self, conn)

    def foreign_key_violations(
        self,
        *,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        """Explicit, output-capped FK audit without modifying journal data."""

        if limit < 1:
            raise ValueError("foreign key audit limit must be positive")
        with self.connect() as conn:
            violations: list[dict[str, object]] = []
            for table, rowid, parent, fk_id in conn.execute(
                "PRAGMA foreign_key_check"
            ):
                violations.append({
                    "table": str(table),
                    "rowid": rowid,
                    "parent": str(parent),
                    "foreign_key_id": int(fk_id),
                })
                if len(violations) >= limit:
                    break
            return violations

    def _ensure_research_paper_account(self, conn: sqlite3.Connection) -> None:
        """Bootstrap the research shadow account (audit AC-01).

        Research experiments book against their own VIRTUAL ledger so their
        losses can never reduce live available cash, trip the live daily-loss
        pause, or deepen live drawdown. The virtual bankroll mirrors the live
        initial capital so research percentage caps keep their historical
        meaning; the accounting-policy transition is recorded once as an
        immutable ledger event on the shared account.
        """

        research_exists = conn.execute(
            "SELECT 1 FROM paper_accounts WHERE account_id = ?", (RESEARCH_ACCOUNT_ID,)
        ).fetchone()
        if not research_exists:
            created_at = _now()
            conn.execute(
                "INSERT OR IGNORE INTO paper_accounts "
                "(account_id, created_at, initial_capital, opening_cash, high_water_equity, cutover_note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    RESEARCH_ACCOUNT_ID,
                    created_at,
                    RESEARCH_VIRTUAL_CAPITAL,
                    RESEARCH_VIRTUAL_CAPITAL,
                    RESEARCH_VIRTUAL_CAPITAL,
                    f"research shadow ledger cutover ({ACCOUNTING_POLICY_VERSION})",
                ),
            )
            self._record_ledger_event(
                conn,
                account_id=RESEARCH_ACCOUNT_ID,
                order_id=None,
                event_type="OPENING_CASH",
                amount=RESEARCH_VIRTUAL_CAPITAL,
                idempotency_key=f"{RESEARCH_ACCOUNT_ID}:opening",
                details={"accounting_policy_version": ACCOUNTING_POLICY_VERSION},
            )
        self._record_ledger_event(
            conn,
            account_id=SHARED_ACCOUNT_ID,
            order_id=None,
            event_type="ACCOUNTING_POLICY_TRANSITION",
            amount=0.0,
            idempotency_key=f"policy:{ACCOUNTING_POLICY_VERSION}",
            details={
                "accounting_policy_version": ACCOUNTING_POLICY_VERSION,
                "research_account_id": RESEARCH_ACCOUNT_ID,
                "note": (
                    "new research orders are shadow-only; historical shared "
                    "rows are preserved unmodified"
                ),
            },
        )
        self._record_ledger_event(
            conn,
            account_id=SHARED_ACCOUNT_ID,
            order_id=None,
            event_type="EXECUTION_SEMANTICS_TRANSITION",
            amount=0.0,
            idempotency_key=f"execution:{EXECUTION_MODEL_VERSION}",
            details={
                "execution_model_version": EXECUTION_MODEL_VERSION,
                "accounting_policy_version": ACCOUNTING_POLICY_VERSION,
                "note": "exec-v3 replayable tape, conserved queue volume, and partial fills",
            },
        )

    def _ensure_shared_paper_account(self, conn: sqlite3.Connection) -> None:
        self._ensure_research_paper_account(conn)
        if conn.execute(
            "SELECT 1 FROM paper_accounts WHERE account_id = ?", (SHARED_ACCOUNT_ID,)
        ).fetchone():
            return
        active = conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE status IN "
            "('PAPER_FILLED', 'PAPER_LIMIT_RESTING', 'PAPER_PARTIALLY_FILLED', "
            "'PAPER_PARTIAL_EXPIRED') AND settled_at IS NULL AND closed_at IS NULL"
        ).fetchone()
        if active and int(active[0] or 0) > 0:
            # Cutover must happen flat.  Existing monitoring/settlement remains
            # available, but account_policy_capacity() will block new entries.
            return
        realized = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM paper_orders "
            "WHERE status IN ('PAPER_SETTLED', 'PAPER_CLOSED')"
        ).fetchone()
        opening_cash = INITIAL_CAPITAL + float(realized[0] or 0.0)
        created_at = _now()
        # INSERT OR IGNORE + the idempotent ledger key make the bootstrap safe
        # even if two initializers slip past the init lock (audit DB-01).
        conn.execute(
            "INSERT OR IGNORE INTO paper_accounts "
            "(account_id, created_at, initial_capital, opening_cash, high_water_equity, cutover_note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                SHARED_ACCOUNT_ID,
                created_at,
                INITIAL_CAPITAL,
                opening_cash,
                opening_cash,
                "flat-book shared-account v2 cutover",
            ),
        )
        self._record_ledger_event(
            conn,
            account_id=SHARED_ACCOUNT_ID,
            order_id=None,
            event_type="OPENING_CASH",
            amount=opening_cash,
            idempotency_key=f"{SHARED_ACCOUNT_ID}:opening",
            details={"initial_capital": INITIAL_CAPITAL, "legacy_realized_pnl": opening_cash - INITIAL_CAPITAL},
        )

    def _expire_pre_v3_resting_orders(self, conn: sqlite3.Connection) -> None:
        """Cancel pre-v3 quotes whose historical queue cannot be reconstructed."""

        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM paper_orders WHERE status='PAPER_LIMIT_RESTING' "
            "AND COALESCE(execution_model_version,'legacy-pre-exec-v3') != ?",
            (EXECUTION_MODEL_VERSION,),
        ).fetchall()
        cancelled_at = _now()
        for row in rows:
            conn.execute(
                "UPDATE paper_orders SET status='PAPER_EXPIRED', cancelled_at=?, "
                "remaining_contracts=0, queue_remaining=0, reserved_cost=0, "
                "outcome_diagnostics_json=? WHERE id=? AND status='PAPER_LIMIT_RESTING'",
                (
                    cancelled_at,
                    json.dumps(
                        {
                            "event": "execution_model_cutover",
                            "reason": "pre-v3 maker queue state is not safely replayable",
                            "previous_execution_model_version": row[
                                "execution_model_version"
                            ],
                            "execution_model_version": EXECUTION_MODEL_VERSION,
                        },
                        sort_keys=True,
                    ),
                    int(row["id"]),
                ),
            )
            if row["account_id"]:
                self._record_ledger_event(
                    conn,
                    account_id=str(row["account_id"]),
                    order_id=int(row["id"]),
                    event_type="RESERVATION_RELEASE",
                    amount=float(row["reserved_cost"] or 0.0),
                    idempotency_key=f"order:{row['id']}:exec-v3-cutover-release",
                    details={"execution_model_version": EXECUTION_MODEL_VERSION},
                )

    @staticmethod
    def _record_ledger_event(
        conn: sqlite3.Connection,
        *,
        account_id: str,
        order_id: int | None,
        event_type: str,
        amount: float,
        idempotency_key: str,
        details: dict[str, object] | None = None,
    ) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO paper_account_ledger "
            "(created_at, account_id, order_id, event_type, amount, idempotency_key, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _now(), account_id, order_id, event_type, float(amount), idempotency_key,
                json.dumps(details or {}, sort_keys=True),
            ),
        )

    def shared_account_state(self) -> dict[str, object] | None:
        return self._account_state(SHARED_ACCOUNT_ID)

    def research_account_state(self) -> dict[str, object] | None:
        """The research shadow account's own virtual ledger state (AC-01)."""

        return self._account_state(RESEARCH_ACCOUNT_ID)

    def _account_state(self, account_id: str) -> dict[str, object] | None:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_accounts'"
            ).fetchone() is None:
                return None
            account = conn.execute(
                "SELECT * FROM paper_accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
            if account is None:
                return None
            cash = float(conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM paper_account_ledger WHERE account_id = ?",
                (account_id,),
            ).fetchone()[0] or 0.0)
            risk = conn.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN status IN "
                "('PAPER_FILLED','PAPER_PARTIALLY_FILLED','PAPER_PARTIAL_EXPIRED') "
                "AND settled_at IS NULL AND closed_at IS NULL "
                "THEN contracts * cost_per_contract ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN status IN "
                "('PAPER_LIMIT_RESTING','PAPER_PARTIALLY_FILLED') "
                "AND settled_at IS NULL AND closed_at IS NULL "
                "THEN reserved_cost ELSE 0 END), 0) FROM paper_orders WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            open_cost = float(risk[0] or 0.0)
            reservations = float(risk[1] or 0.0)
            cash_balance = cash + reservations
            realized_equity = cash_balance + open_cost
            high_water = max(float(account["high_water_equity"]), realized_equity)
            if high_water > float(account["high_water_equity"]):
                conn.execute(
                    "UPDATE paper_accounts SET high_water_equity=? WHERE account_id=?",
                    (high_water, account_id),
                )
            return {
                "account_id": account_id,
                "initial_capital": float(account["initial_capital"]),
                "opening_cash": float(account["opening_cash"]),
                "cash_balance": cash_balance,
                "open_cost_basis": open_cost,
                "reservations": reservations,
                "available_cash": cash,
                "realized_equity": realized_equity,
                "high_water_equity": high_water,
                "drawdown": (high_water - realized_equity) / high_water if high_water > 0 else 0.0,
                "status": account["status"],
            }

    def account_policy_capacity(
        self,
        *,
        target_date: str,
        market_ticker: str,
        risk_profile: str | None,
        requested_spend: float,
    ) -> dict[str, object]:
        """Maximum safe new notional under the paper account policy.

        Live entries are governed entirely by the shared production-intent
        account. Research entries (audit AC-01) keep their historical
        percentage caps against live equity, but their cash constraint,
        drawdown pause, and daily-loss pause come from the research shadow
        account's own virtual ledger, so research losses can never pause or
        shrink live entries and vice-versa research keeps its own discipline.
        """

        state = self.shared_account_state()
        if state is None:
            return {"allowed_spend": 0.0, "reason": "shared account cutover requires a flat book"}
        entry_account = account_for_profile(risk_profile)
        if entry_account != SHARED_ACCOUNT_ID:
            research_state = self.research_account_state()
            if research_state is None:
                return {"allowed_spend": 0.0, "reason": "research shadow account is not initialized"}
            state = {
                **state,
                "available_cash": research_state["available_cash"],
                "drawdown": research_state["drawdown"],
            }
        today_start = datetime.now(SETTLEMENT_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        with self.connect() as conn:
            daily_pnl = float(conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM paper_orders "
                "WHERE status IN ('PAPER_SETTLED', 'PAPER_CLOSED') "
                "AND COALESCE(closed_at, settled_at) >= ? "
                "AND COALESCE(account_id, ?) = ?",
                (
                    today_start.astimezone(UTC).isoformat(),
                    SHARED_ACCOUNT_ID,
                    entry_account,
                ),
            ).fetchone()[0] or 0.0)
            active = conn.execute(
                "SELECT market_ticker, target_date, COALESCE(risk_profile, 'live'), "
                "CASE WHEN status='PAPER_LIMIT_RESTING' THEN reserved_cost "
                "WHEN status='PAPER_PARTIALLY_FILLED' THEN "
                "contracts * cost_per_contract + reserved_cost "
                "ELSE contracts * cost_per_contract END AS risk "
                "FROM paper_orders WHERE account_id IN (?, ?) AND status IN "
                "('PAPER_FILLED','PAPER_LIMIT_RESTING','PAPER_PARTIALLY_FILLED',"
                "'PAPER_PARTIAL_EXPIRED') AND settled_at IS NULL AND closed_at IS NULL",
                (
                    SHARED_ACCOUNT_ID,
                    entry_account if entry_account != SHARED_ACCOUNT_ID else SHARED_ACCOUNT_ID,
                ),
            ).fetchall()
        return policy_capacity(
            state=state,
            active_rows=active,
            daily_pnl=daily_pnl,
            target_date=target_date,
            market_ticker=market_ticker,
            risk_profile=risk_profile,
            requested_spend=requested_spend,
        )

    def record_forecast(self, forecast: ForecastSnapshot) -> int:
        created_at = _now()
        raw = {
            **forecast.raw,
            "lead_hours": forecast.lead_hours,
            "google_high_f": forecast.google_high_f,
            "nws_high_f": forecast.nws_high_f,
            "open_meteo_high_f": forecast.open_meteo_high_f,
            "history_high_f": forecast.history_high_f,
            "google_weight": forecast.google_weight,
            "nws_weight": forecast.nws_weight,
            "open_meteo_weight": forecast.open_meteo_weight,
            "history_weight": forecast.history_weight,
            "station_adjustment_f": forecast.station_adjustment_f,
            "fresh_station_count": forecast.fresh_station_count,
            "max_calls_per_day": forecast.max_calls_per_day,
            "calls_used_today": forecast.calls_used_today,
        }
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO forecast_snapshots (
                    created_at, target_date, station_id, predicted_high_f, fetched_at, method, source_spread_f, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    forecast.target_date.isoformat(),
                    forecast.station_id,
                    forecast.predicted_high_f,
                    forecast.fetched_at,
                    forecast.method,
                    forecast.source_spread_f,
                    json.dumps(raw, sort_keys=True),
                ),
            )
            return int(cursor.lastrowid)

    def record_market(self, event: EventSnapshot) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO market_snapshots (created_at, event_ticker, target_date, raw_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    _now(),
                    event.event_ticker,
                    event.target_date.isoformat() if event.target_date else None,
                    json.dumps(event.raw, sort_keys=True),
                ),
            )
            return int(cursor.lastrowid)

    def latest_market_snapshot(
        self,
        target_date: str,
        *,
        event_ticker: str | None = None,
    ) -> EventSnapshot | None:
        """Reconstruct the most recent stored Kalshi ladder for a target date.

        ``record_market`` persists the full Kalshi event payload (the same
        ``with_nested_markets`` body that ``EventSnapshot.from_kalshi`` parses) as
        ``raw_json``, so the freshest snapshot round-trips losslessly back into an
        ``EventSnapshot`` -- bid/ask ladder and all. The Strategy Lab builder uses
        this to distill the market consensus offline (it never touches live
        Kalshi). Returns None when no snapshot was ever stored for the target or
        when the stored payload is unparseable.
        """

        filters = ["target_date = ?"]
        params: list[object] = [target_date]
        if event_ticker is not None:
            filters.append("event_ticker = ?")
            params.append(event_ticker)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT raw_json
                FROM market_snapshots
                WHERE {' AND '.join(filters)}
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        if row is None or not row[0]:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return EventSnapshot.from_kalshi(payload)

    def record_probabilities(self, target_date: str, probabilities: Iterable[BucketProbability]) -> None:
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO probability_snapshots (
                    created_at, target_date, market_ticker, label, probability,
                    lower_confidence, empirical_probability, normal_probability, effective_n,
	                    residual_probability, ensemble_probability,
	                    model_probability, market_probability, observed_high_f,
	                    intraday_probability, remaining_heat_risk
	                )
	                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        _now(),
                        target_date,
                        probability.ticker,
                        probability.label,
                        probability.probability,
                        probability.lower_confidence,
                        probability.empirical_probability,
                        probability.normal_probability,
                        probability.effective_n,
                        probability.residual_probability,
                        probability.ensemble_probability,
	                        probability.model_probability,
	                        probability.market_probability,
	                        probability.observed_high_f,
	                        probability.intraday_probability,
	                        probability.remaining_heat_risk,
	                    )
                    for probability in probabilities
                ],
            )

    def latest_model_probability(
        self,
        target_date: str,
        market_ticker: str,
        *,
        max_age_minutes: float = 90.0,
    ) -> float | None:
        """Most recent pure weather-model YES probability, or None if stale.

        The paper monitor uses this to veto stop-loss exits that the model
        still expects to win at settlement; a stale snapshot must not veto.
        This intentionally reads the model_probability column (the weather
        model alone), not the market-blended posterior, so the veto reflects
        the model's own conviction rather than the book it is trying to beat.
        Older rows that predate model_probability fall back to the blend.

        Candidates come from ``decision_snapshots`` (written on EVERY scan tick)
        and ``probability_snapshots`` (including monitor heartbeats). The newest
        valid timestamp wins across both journals.
        ``probability_snapshots`` is only written on the first command of the
        first profile per tick (the ``--skip-context-snapshots`` dedup); when a
        scan run never reaches that path the context tables can flatline while
        the decision journal keeps flowing. Conversely, after the same-day entry
        cutoff the heartbeat can be newer than the last decision row. Comparing
        timestamps prevents either journal from shadowing fresher model state.
        """

        read = self.latest_model_probability_read(
            target_date, market_ticker, max_age_minutes=max_age_minutes
        )
        return None if read is None else read[1]

    def latest_model_probability_read(
        self,
        target_date: str,
        market_ticker: str,
        *,
        max_age_minutes: float = 90.0,
    ) -> tuple[datetime, float] | None:
        """Like ``latest_model_probability`` but keeps the snapshot timestamp.

        The monitor persists the timestamp and age of the model read behind
        every exit decision so stale-read incidents are auditable (RK-01).
        """

        candidates = [
            self._latest_model_probability_from_decisions(
                target_date, market_ticker, max_age_minutes=max_age_minutes
            ),
            self._latest_model_probability_from_snapshots(
                target_date, market_ticker, max_age_minutes=max_age_minutes
            ),
        ]
        valid = [candidate for candidate in candidates if candidate is not None]
        if not valid:
            return None
        return max(valid, key=lambda candidate: candidate[0])

    def _latest_model_probability_from_decisions(
        self,
        target_date: str,
        market_ticker: str,
        *,
        max_age_minutes: float,
    ) -> tuple[datetime, float] | None:
        """Latest valid decision model read, normalized to the YES frame."""

        now = datetime.now(UTC)
        lower = (now - timedelta(minutes=max_age_minutes)).isoformat()
        upper = (now + timedelta(minutes=5)).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT created_at, side, COALESCE(model_probability, probability)
                FROM decision_snapshots
                WHERE target_date = ? AND market_ticker = ?
                  AND julianday(created_at) >= julianday(?)
                  AND julianday(created_at) <= julianday(?)
                ORDER BY julianday(created_at) DESC, id DESC
                """,
                (target_date, market_ticker, lower, upper),
            ).fetchall()
        for row in rows:
            if row[2] is None:
                continue
            created = self._valid_snapshot_timestamp(row[0], max_age_minutes)
            if created is None:
                continue
            value = float(row[2])
            side = str(row[1]).upper()
            yes_probability = value if side == "YES" else 1.0 - value
            return created, max(0.0, min(1.0, yes_probability))
        return None

    def _latest_model_probability_from_snapshots(
        self,
        target_date: str,
        market_ticker: str,
        *,
        max_age_minutes: float,
    ) -> tuple[datetime, float] | None:
        now = datetime.now(UTC)
        lower = (now - timedelta(minutes=max_age_minutes)).isoformat()
        upper = (now + timedelta(minutes=5)).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT created_at, COALESCE(model_probability, probability)
                FROM probability_snapshots
                WHERE target_date = ? AND market_ticker = ?
                  AND julianday(created_at) >= julianday(?)
                  AND julianday(created_at) <= julianday(?)
                ORDER BY julianday(created_at) DESC, id DESC
                """,
                (target_date, market_ticker, lower, upper),
            ).fetchall()
        for row in rows:
            if row[1] is None:
                continue
            created = self._valid_snapshot_timestamp(row[0], max_age_minutes)
            if created is not None:
                return created, max(0.0, min(1.0, float(row[1])))
        return None

    @staticmethod
    def _valid_snapshot_timestamp(
        created_at: object,
        max_age_minutes: float,
        *,
        max_future_minutes: float = 5.0,
    ) -> datetime | None:
        try:
            created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except ValueError:
            return None
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        created = created.astimezone(UTC)
        age_minutes = (datetime.now(UTC) - created).total_seconds() / 60.0
        if age_minutes > max_age_minutes or age_minutes < -max_future_minutes:
            return None
        return created

    @staticmethod
    def _snapshot_is_fresh(created_at: object, max_age_minutes: float) -> bool:
        return (
            PaperStore._valid_snapshot_timestamp(created_at, max_age_minutes)
            is not None
        )

    def record_decisions(
        self,
        target_date: str,
        decisions: Iterable[TradeDecision],
        *,
        forecast: ForecastSnapshot | None = None,
        intraday: IntradaySnapshot | None = None,
        event: EventSnapshot | None = None,
        market_consensus: MarketConsensus | None = None,
        risk_profile: str | None = None,
        bankroll: float | None = None,
        strategy_config: StrategyConfig | None = None,
        forecast_snapshot_id: int | None = None,
        market_snapshot_id: int | None = None,
    ) -> None:
        created_at = _now()
        rows = []
        markets_by_ticker = {}
        if event is not None:
            markets_by_ticker = {market.ticker: market for market in event.markets}
        market_payloads = {
            ticker: _market_diagnostics_payload(market, event)
            for ticker, market in markets_by_ticker.items()
        }
        observed_high_mode = _forecast_observed_high_mode(forecast)
        prediction_features = build_prediction_feature_snapshot(
            forecast,
            market_consensus=market_consensus,
            intraday=intraday,
        )
        prediction_features_json = json.dumps(
            prediction_features,
            sort_keys=True,
        )
        for decision in decisions:
            spend = decision.recommended_contracts * decision.cost_per_contract
            market = markets_by_ticker.get(decision.ticker)
            diagnostics_json = json.dumps(
                {
                    "schema_version": 2,
                    "kind": "trade_decision_signal",
                    "signal": _decision_signal_payload(decision),
                },
                sort_keys=True,
            )
            rows.append(
                (
                    created_at,
                    target_date,
                    decision.ticker,
                    decision.label,
                    decision.action,
                    decision.side,
                    1 if decision.approved else 0,
                    1
                    if (
                        decision.signal_approved
                        if decision.signal_approved is not None
                        else decision.approved
                    )
                    else 0,
                    decision.entry_block_reason,
                    decision.probability,
                    decision.probability_lcb,
                    decision.model_probability,
                    decision.market_probability,
                    decision.residual_probability,
                    decision.ensemble_probability,
                    decision.intraday_probability,
                    decision.remaining_heat_risk,
                    decision.yes_bid,
                    decision.yes_ask,
                    decision.bid,
                    decision.ask,
                    decision.bid_size,
                    decision.ask_size,
                    decision.spread,
                    decision.fee_per_contract,
                    decision.cost_per_contract,
                    decision.edge,
                    decision.edge_lcb,
                    decision.kelly_fraction,
                    decision.recommended_contracts,
                    spend,
                    decision.expected_profit,
                    decision.trade_quality_score,
                    decision.strike_type,
                    decision.floor_strike,
                    decision.cap_strike,
                    event.event_ticker if event is not None else None,
                    market.status if market is not None else None,
                    _market_close_time(market.raw) if market is not None else None,
                    forecast.fetched_at if forecast is not None else None,
                    forecast.method if forecast is not None else None,
                    observed_high_mode,
                    intraday.observed_high_f if intraday is not None else None,
                    intraday.latest_observed_at if intraday is not None else None,
                    1 if intraday is not None and intraday.is_complete else 0,
                    intraday.observed_high_source if intraday is not None else None,
                    forecast.predicted_high_f if forecast is not None else None,
                    forecast.source_spread_f if forecast is not None else None,
                    forecast.lead_hours if forecast is not None else None,
                    risk_profile,
                    bankroll,
                    forecast_snapshot_id,
                    market_snapshot_id,
                    None,
                    diagnostics_json,
                    json.dumps(decision.reasons),
                )
            )
        with self.connect() as conn:
            context = conn.execute(
                """
                INSERT INTO scan_context_snapshots (
                    created_at, target_date, risk_profile, station_id, event_ticker, bankroll,
                    forecast_snapshot_id, market_snapshot_id, forecast_json,
                    intraday_json, market_json, market_consensus_json,
                    prediction_features_json,
                    strategy_config_json, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    created_at,
                    target_date,
                    risk_profile,
                    forecast.station_id if forecast is not None else None,
                    event.event_ticker if event is not None else None,
                    bankroll,
                    forecast_snapshot_id,
                    market_snapshot_id,
                    _json_text(_forecast_diagnostics_payload(forecast)),
                    _json_text(_intraday_diagnostics_payload(intraday)),
                    (
                        _json_text(market_payloads)
                        if market_snapshot_id is None or event is None or not event.raw
                        else None
                    ),
                    _json_text(_market_consensus_diagnostics_payload(market_consensus)),
                    prediction_features_json,
                    _json_text(_strategy_config_snapshot(strategy_config)),
                ),
            )
            scan_context_id = int(context.lastrowid)
            conn.executemany(
                """
                INSERT INTO decision_snapshots (
                    scan_context_id,
                    created_at, target_date, market_ticker, label, action, side,
                    approved, signal_approved, entry_block_reason,
                    probability, probability_lcb, model_probability,
                    market_probability, residual_probability, ensemble_probability,
                    intraday_probability, remaining_heat_risk, yes_bid, yes_ask,
                    entry_bid, entry_ask, entry_bid_size, entry_ask_size, spread,
                    fee_per_contract, cost_per_contract, edge, edge_lcb,
                    kelly_fraction, recommended_contracts, recommended_spend,
                    expected_profit, trade_quality_score, strike_type, floor_strike,
                    cap_strike, event_ticker, market_status, market_close_time,
                    forecast_fetched_at, forecast_method, forecast_observed_high_mode,
                    intraday_observed_high_f, intraday_latest_observed_at,
                    intraday_is_complete, intraday_observed_high_source,
                    forecast_predicted_high_f, forecast_source_spread_f,
                    forecast_lead_hours, risk_profile, bankroll,
                    forecast_snapshot_id, market_snapshot_id,
                    prediction_features_json, diagnostics_json, reasons_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ((scan_context_id, *row) for row in rows),
            )

    def record_paper_order(
        self,
        target_date: str,
        decision: TradeDecision,
        *,
        risk_profile: str | None = None,
        status: str | None = None,
        entry_mode: str = "market",
        group_id: str | None = None,
        strategy_config: StrategyConfig | None = None,
    ) -> int | None:
        contracts = float(decision.recommended_contracts)
        entry_price = float(decision.limit_price if decision.limit_price is not None else decision.ask)
        normalized_status = status or ("PAPER_FILLED" if decision.approved else "REJECTED")
        fee_per_contract = (
            float(decision.limit_fee_per_contract)
            if decision.limit_fee_per_contract is not None
            else quadratic_fee_average_per_contract(
                entry_price,
                contracts,
                maker=normalized_status == "PAPER_LIMIT_RESTING",
                series_ticker=decision.ticker,
            )
        )
        cost_per_contract = entry_price + fee_per_contract
        edge = float(decision.limit_edge) if decision.limit_edge is not None else float(decision.edge)
        edge_lcb = (
            float(decision.limit_edge_lcb)
            if decision.limit_edge_lcb is not None
            else float(decision.edge_lcb)
        )
        expected_profit = edge * contracts
        profile = normalize_risk_profile_name(risk_profile) if risk_profile else None
        # Research orders book against the shadow ledger (audit AC-01) unless
        # the shared-capital experiment mode is explicitly enabled.
        entry_account = account_for_profile(profile)
        created_at = _now()
        filled_at = created_at if normalized_status == "PAPER_FILLED" else None
        expires_at = (
            (datetime.fromisoformat(created_at) + timedelta(minutes=15)).isoformat()
            if normalized_status == "PAPER_LIMIT_RESTING"
            else None
        )
        reserved_cost = contracts * cost_per_contract if normalized_status == "PAPER_LIMIT_RESTING" else 0.0
        fingerprint = strategy_fingerprint(strategy_config, entry_mode=entry_mode)
        sleeve = sleeve_for(profile, list(decision.reasons), decision.side)
        quote_snapshot_json = json.dumps(
            {
                "side": decision.side,
                "bid": decision.bid,
                "ask": decision.ask,
                "limit_price": decision.limit_price,
                "contracts": contracts,
                "fee_per_contract": fee_per_contract,
                "cost_per_contract": cost_per_contract,
            },
            sort_keys=True,
        )
        fill_model = (
            "maker_trade_through_required"
            if normalized_status == "PAPER_LIMIT_RESTING"
            else "immediate_visible_quote"
        )
        with self.connect() as conn:
            entry_decision = _latest_entry_decision_snapshot(
                conn,
                target_date,
                decision,
                risk_profile=profile,
            )
            diagnostics_json = json.dumps(
                _order_entry_diagnostics_payload(
                    target_date,
                    decision,
                    created_at=created_at,
                    kind="paper_order",
                    risk_profile=profile,
                    status=normalized_status,
                    entry_mode=entry_mode,
                    group_id=group_id,
                    strategy_config=strategy_config,
                    sample_probability=None,
                    sampled=None,
                    entry_decision=entry_decision,
                ),
                sort_keys=True,
            )
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO paper_orders (
                        created_at, target_date, market_ticker, label, action, risk_profile,
                        group_id,
                        side, contracts, yes_ask, entry_price, entry_bid, entry_bid_size, entry_ask_size,
                        strike_type, floor_strike, cap_strike, entry_mode,
                        limit_price, limit_fee_per_contract, limit_cost_per_contract, limit_edge, limit_edge_lcb,
                        fee_per_contract, cost_per_contract, probability,
                        probability_lcb, edge, edge_lcb, trade_quality_score,
                        expected_profit, status, entry_decision_snapshot_id,
                        diagnostics_json, reasons_json, account_id,
                        strategy_fingerprint, sleeve, filled_at, expires_at,
                        reserved_cost, quote_snapshot_json, fill_model,
                        requested_contracts, filled_contracts, remaining_contracts,
                        queue_remaining, execution_model_version
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        created_at,
                        target_date,
                        decision.ticker,
                        decision.label,
                        decision.action,
                        profile,
                        group_id,
                        decision.side,
                        contracts,
                        entry_price,
                        entry_price,
                        decision.bid,
                        decision.bid_size,
                        decision.ask_size,
                        decision.strike_type,
                        decision.floor_strike,
                        decision.cap_strike,
                        entry_mode,
                        decision.limit_price,
                        decision.limit_fee_per_contract,
                        decision.limit_cost_per_contract,
                        decision.limit_edge,
                        decision.limit_edge_lcb,
                        fee_per_contract,
                        cost_per_contract,
                        decision.probability,
                        decision.probability_lcb,
                        edge,
                        edge_lcb,
                        decision.trade_quality_score,
                        expected_profit,
                        normalized_status,
                        _row_value(entry_decision, "id") if entry_decision is not None else None,
                        diagnostics_json,
                        json.dumps(decision.reasons),
                        entry_account,
                        fingerprint,
                        sleeve,
                        filled_at,
                        expires_at,
                        reserved_cost,
                        quote_snapshot_json,
                        fill_model,
                        contracts,
                        contracts if normalized_status == "PAPER_FILLED" else 0.0,
                        contracts if normalized_status == "PAPER_LIMIT_RESTING" else 0.0,
                        float(decision.bid_size or 0.0)
                        if normalized_status == "PAPER_LIMIT_RESTING"
                        else 0.0,
                        EXECUTION_MODEL_VERSION,
                    ),
                )
            except sqlite3.IntegrityError:
                # The open-position guard index rejected a second OPEN order on
                # this market/side/profile -- a check-then-insert race the
                # application guard (has_active_paper_entry) did not catch. The
                # existing open order stands; signal "not recorded" to the caller.
                return None
            order_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT OR IGNORE INTO strategy_versions "
                "(fingerprint, created_at, config_json, status) VALUES (?, ?, ?, 'PAPER')",
                (
                    fingerprint,
                    created_at,
                    json.dumps(_strategy_config_snapshot(strategy_config) or {}, sort_keys=True),
                ),
            )
            if normalized_status == "PAPER_LIMIT_RESTING":
                self._record_ledger_event(
                    conn,
                    account_id=entry_account,
                    order_id=order_id,
                    event_type="RESERVE",
                    amount=-reserved_cost,
                    idempotency_key=f"order:{order_id}:reserve",
                    details={"expires_at": expires_at},
                )
            elif normalized_status == "PAPER_FILLED":
                self._record_ledger_event(
                    conn,
                    account_id=entry_account,
                    order_id=order_id,
                    event_type="ENTRY_FILL",
                    amount=-(contracts * cost_per_contract),
                    idempotency_key=f"order:{order_id}:entry-fill",
                )
            return order_id

    def record_research_shadow_order(
        self,
        target_date: str,
        decision: TradeDecision,
        *,
        risk_profile: str | None,
        sample_probability: float,
        sampled: bool,
        linked_paper_order_id: int | None = None,
        strategy_config: StrategyConfig | None = None,
    ) -> int:
        contracts = float(decision.recommended_contracts)
        entry_price = float(decision.limit_price if decision.limit_price is not None else decision.ask)
        fee_per_contract = (
            float(decision.limit_fee_per_contract)
            if decision.limit_fee_per_contract is not None
            else quadratic_fee_average_per_contract(entry_price, contracts)
        )
        cost_per_contract = entry_price + fee_per_contract
        edge = float(decision.limit_edge) if decision.limit_edge is not None else float(decision.edge)
        edge_lcb = (
            float(decision.limit_edge_lcb)
            if decision.limit_edge_lcb is not None
            else float(decision.edge_lcb)
        )
        expected_profit = edge * contracts
        profile = normalize_risk_profile_name(risk_profile) if risk_profile else None
        created_at = _now()
        with self.connect() as conn:
            entry_decision = _latest_entry_decision_snapshot(
                conn,
                target_date,
                decision,
                risk_profile=profile,
            )
            diagnostics_json = json.dumps(
                _order_entry_diagnostics_payload(
                    target_date,
                    decision,
                    created_at=created_at,
                    kind="research_shadow_order",
                    risk_profile=profile,
                    status="SHADOW_OPEN",
                    entry_mode="shadow",
                    group_id=None,
                    strategy_config=strategy_config,
                    sample_probability=sample_probability,
                    sampled=sampled,
                    entry_decision=entry_decision,
                ),
                sort_keys=True,
            )
            cursor = conn.execute(
                """
                INSERT INTO research_shadow_orders (
                    created_at, target_date, market_ticker, label, action,
                    risk_profile, side, contracts, yes_ask, entry_price,
                    entry_bid, entry_bid_size, entry_ask_size, strike_type,
                    floor_strike, cap_strike, fee_per_contract,
                    cost_per_contract, probability, probability_lcb, edge,
                    edge_lcb, trade_quality_score, expected_profit,
                    sample_probability, sampled, linked_paper_order_id,
                    status, entry_decision_snapshot_id, diagnostics_json,
                    reasons_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    target_date,
                    decision.ticker,
                    decision.label,
                    decision.action,
                    profile,
                    decision.side,
                    contracts,
                    decision.yes_ask,
                    entry_price,
                    decision.bid,
                    decision.bid_size,
                    decision.ask_size,
                    decision.strike_type,
                    decision.floor_strike,
                    decision.cap_strike,
                    fee_per_contract,
                    cost_per_contract,
                    decision.probability,
                    decision.probability_lcb,
                    edge,
                    edge_lcb,
                    decision.trade_quality_score,
                    expected_profit,
                    sample_probability,
                    1 if sampled else 0,
                    linked_paper_order_id,
                    "SHADOW_OPEN",
                    _row_value(entry_decision, "id") if entry_decision is not None else None,
                    diagnostics_json,
                    json.dumps(decision.reasons),
                ),
            )
            return int(cursor.lastrowid)

    def link_research_shadow_order(self, shadow_order_id: int, paper_order_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE research_shadow_orders
                SET linked_paper_order_id = ?, sampled = 1
                WHERE id = ?
                """,
                (paper_order_id, shadow_order_id),
            )

    def research_shadow_orders(
        self,
        limit: int = 50,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> list[sqlite3.Row]:
        filters, params = _date_filters(since, until)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                f"SELECT * FROM research_shadow_orders {where} ORDER BY created_at DESC, id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()

    def research_shadow_sample_spend_for_target(
        self,
        target_date: str,
        *,
        risk_profile: str | None = None,
    ) -> float:
        profile_filter, profile_params = _paper_profile_filter(risk_profile)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT COALESCE(SUM(p.contracts * p.cost_per_contract), 0)
                FROM research_shadow_orders s
                JOIN paper_orders p ON p.id = s.linked_paper_order_id
                WHERE s.target_date = ?
                  AND s.sampled = 1
                  AND p.status != 'REJECTED'
                  {profile_filter.replace('COALESCE(risk_profile,', 'COALESCE(p.risk_profile,')}
                """,
                (target_date, *profile_params),
            ).fetchone()
        return float(row[0] or 0.0)

    def has_losing_closed_negative_lcb_research_entry(
        self,
        target_date: str,
        market_ticker: str,
        side: str,
    ) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM paper_orders
                WHERE target_date = ?
                  AND market_ticker = ?
                  AND UPPER(COALESCE(side, 'YES')) = ?
                  AND COALESCE(risk_profile, 'live') = 'research'
                  AND status = 'PAPER_CLOSED'
                  AND settled_at IS NULL
                  AND closed_at IS NOT NULL
                  AND edge_lcb < 0
                  AND COALESCE(realized_pnl, 0) < 0
                LIMIT 1
                """,
                (target_date, market_ticker, side.upper()),
            ).fetchone()
        return row is not None

    def record_manual_buy(
        self,
        *,
        target_date: str,
        market_ticker: str,
        label: str,
        amount: float,
        entry_price: float,
        side: str = "YES",
        action: str,
        reason: str,
        strike_type: str | None = None,
        floor_strike: float | None = None,
        cap_strike: float | None = None,
    ) -> int:
        side = side.upper()
        if side not in {"YES", "NO"}:
            raise ValueError("side must be YES or NO")
        if amount <= 0:
            raise ValueError("amount must be positive")
        if entry_price <= 0 or entry_price >= 1:
            raise ValueError("entry price must be between 0.01 and 0.99")
        # Kalshi trades whole contracts. Fractional dust also breaks the
        # ceil-to-cent fee model (a 0.01-contract order would carry a $1.00
        # per-contract fee), so manual paper buys round down to whole contracts.
        contracts = float(int(contracts_for_budget(entry_price, amount)))
        if contracts < 1:
            raise ValueError(
                f"amount ${amount:.2f} cannot buy one whole contract at {entry_price:.2f} plus fees"
            )
        fee = quadratic_fee_average_per_contract(entry_price, contracts)
        cost = entry_price + fee
        decision = TradeDecision(
            ticker=market_ticker,
            label=label,
            action=action,
            approved=True,
            probability=0.0,
            probability_lcb=0.0,
            yes_bid=0.0,
            yes_ask=entry_price,
            spread=0.0,
            fee_per_contract=fee,
            cost_per_contract=cost,
            edge=0.0,
            edge_lcb=0.0,
            kelly_fraction=0.0,
            recommended_contracts=contracts,
            expected_profit=0.0,
            reasons=[reason],
            side=side,
            entry_bid=0.0,
            entry_ask=entry_price,
            strike_type=strike_type,
            floor_strike=floor_strike,
            cap_strike=cap_strike,
        )
        order_id = self.record_paper_order(target_date, decision)
        if order_id is None:
            raise ValueError(
                "an open paper position already exists for this market/side/profile"
            )
        return order_id

    def record_manual_yes_buy(
        self,
        *,
        target_date: str,
        market_ticker: str,
        label: str,
        amount: float,
        entry_price: float,
        action: str,
        reason: str,
    ) -> int:
        return self.record_manual_buy(
            target_date=target_date,
            market_ticker=market_ticker,
            label=label,
            amount=amount,
            entry_price=entry_price,
            side="YES",
            action=action,
            reason=reason,
        )

    def paper_spend_for_target(
        self,
        target_date: str,
        *,
        risk_profile: str | None = None,
        series_ticker: str | None = None,
    ) -> float:
        """Capital already deployed for one settlement target.

        ``series_ticker`` scopes the cap to one city's event (multi-city scans
        cap per city-day, not across all fifteen cities sharing one date).
        """

        profile_filter, profile_params = _paper_profile_filter(risk_profile)
        series_filter = ""
        series_params: tuple = ()
        if series_ticker:
            series_filter = " AND market_ticker LIKE ?"
            series_params = (f"{series_ticker}-%",)
        with self.connect() as conn:
            # Exclude PAPER_EXPIRED: those are resting limit orders that never
            # crossed and deployed ZERO capital, so they must not consume the
            # per-target exposure cap. Counting their intended-but-never-filled
            # notional inflated cumulative spend and blocked valid re-entries on
            # the next scan -- exactly the cap-freeing the settle path documents
            # when it expires them. (REJECTED was never placed.)
            row = conn.execute(
                f"""
                SELECT COALESCE(SUM(contracts * cost_per_contract), 0)
                FROM paper_orders
                WHERE target_date = ? AND status NOT IN ('REJECTED', 'PAPER_EXPIRED')
                {series_filter}
                {profile_filter}
                """,
                (target_date, *series_params, *profile_params),
            ).fetchone()
        return float(row[0] or 0.0)

    def remaining_daily_budget(
        self,
        target_date: str,
        daily_budget: float,
        *,
        risk_profile: str | None = None,
    ) -> float:
        if daily_budget < 0:
            raise ValueError("daily budget cannot be negative")
        spent = self.paper_spend_for_target(target_date, risk_profile=risk_profile)
        return max(0.0, daily_budget - spent)

    def entries_for_market_side(
        self,
        target_date: str,
        market_ticker: str,
        side: str,
        *,
        risk_profile: str | None = None,
    ) -> int:
        """Count all recorded paper entries for a market/side and target date.

        Unlike has_open_paper_position, this also counts closed and settled
        orders, so a stop-loss exit cannot be followed by an immediate re-buy
        on the next scheduled scan.

        PAPER_EXPIRED is excluded alongside REJECTED: an expired resting maker
        quote never filled, deployed zero capital, and carries no position or
        realized outcome. Counting it against max_entries_per_market_side
        (live=1) permanently blocked that market-side for the whole target
        date after one unfilled 15-minute quote, so an approved edge could
        never re-quote (e.g. expired order #109 blocked 32 later approved
        snapshots). Resting quotes still count while they rest, and
        filled/closed/settled entries still count forever.
        """

        profile_filter, profile_params = _paper_profile_filter(risk_profile)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM paper_orders
                WHERE target_date = ?
                  AND market_ticker = ?
                  AND UPPER(COALESCE(side, 'YES')) = ?
                  AND status NOT IN ('REJECTED', 'PAPER_EXPIRED')
                  AND parent_order_id IS NULL
                  {profile_filter}
                """,
                (target_date, market_ticker, side.upper(), *profile_params),
            ).fetchone()
        return int(row[0] or 0)

    def has_open_paper_position(
        self,
        target_date: str,
        market_ticker: str,
        side: str | None = None,
        *,
        risk_profile: str | None = None,
    ) -> bool:
        filters = [
            "target_date = ?",
            "market_ticker = ?",
            "status IN ('PAPER_FILLED', 'PAPER_PARTIALLY_FILLED', 'PAPER_PARTIAL_EXPIRED')",
            "settled_at IS NULL",
            "closed_at IS NULL",
        ]
        params: list[object] = [target_date, market_ticker]
        if side is not None:
            filters.append("UPPER(side) = ?")
            params.append(side.upper())
        if risk_profile is not None:
            filters.append("COALESCE(risk_profile, 'live') = ?")
            params.append(normalize_risk_profile_name(risk_profile))
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT 1
                FROM paper_orders
                WHERE {' AND '.join(filters)}
                LIMIT 1
                """,
                params,
            ).fetchone()
        return row is not None

    def has_active_paper_entry(
        self,
        target_date: str,
        market_ticker: str,
        *,
        risk_profile: str | None = None,
    ) -> bool:
        filters = [
            "target_date = ?",
            "market_ticker = ?",
            "status IN ('PAPER_FILLED', 'PAPER_LIMIT_RESTING', "
            "'PAPER_PARTIALLY_FILLED', 'PAPER_PARTIAL_EXPIRED')",
            "settled_at IS NULL",
            "closed_at IS NULL",
        ]
        params: list[object] = [target_date, market_ticker]
        if risk_profile is not None:
            filters.append("COALESCE(risk_profile, 'live') = ?")
            params.append(normalize_risk_profile_name(risk_profile))
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT 1
                FROM paper_orders
                WHERE {' AND '.join(filters)}
                LIMIT 1
                """,
                params,
            ).fetchone()
        return row is not None

    def resting_limit_orders(
        self,
        target_date: str,
        market_ticker: str,
        side: str,
        *,
        risk_profile: str | None = None,
    ) -> list[sqlite3.Row]:
        filters = [
            "target_date = ?",
            "market_ticker = ?",
            "UPPER(COALESCE(side, 'YES')) = ?",
            "status IN ('PAPER_LIMIT_RESTING', 'PAPER_PARTIALLY_FILLED')",
            "settled_at IS NULL",
            "closed_at IS NULL",
        ]
        params: list[object] = [target_date, market_ticker, side.upper()]
        if risk_profile is not None:
            filters.append("COALESCE(risk_profile, 'live') = ?")
            params.append(normalize_risk_profile_name(risk_profile))
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                f"""
                SELECT *
                FROM paper_orders
                WHERE {' AND '.join(filters)}
                ORDER BY created_at, id
                """,
                params,
            ).fetchall()

    def fill_resting_limit_order(
        self, order_id: int, *, evidence: dict[str, object] | None = None
    ) -> sqlite3.Row | None:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM paper_orders WHERE id=? AND status='PAPER_LIMIT_RESTING'",
                (order_id,),
            ).fetchone()
            if row is None:
                return self._order(order_id)
            filled_at = _now()
            cursor = conn.execute(
                """
                UPDATE paper_orders
                SET status = 'PAPER_FILLED', filled_at = ?, reserved_cost = 0,
                    fill_evidence_json = ?
                WHERE id = ? AND status = 'PAPER_LIMIT_RESTING'
                """,
                (filled_at, json.dumps(evidence or {}, sort_keys=True), order_id),
            )
            if cursor.rowcount:
                reserved = float(row["reserved_cost"] or 0.0)
                if row["account_id"]:
                    self._record_ledger_event(
                        conn, account_id=row["account_id"],
                        order_id=order_id, event_type="RESERVATION_RELEASE", amount=reserved,
                        idempotency_key=f"order:{order_id}:fill-release",
                    )
                    self._record_ledger_event(
                        conn, account_id=row["account_id"],
                        order_id=order_id, event_type="ENTRY_FILL",
                        amount=-(float(row["contracts"]) * float(row["cost_per_contract"])),
                        idempotency_key=f"order:{order_id}:entry-fill",
                        details=evidence,
                    )
                self._record_maker_volume_claims(conn, row, evidence or {})
        return self._order(order_id)

    def apply_maker_trade_batch(
        self,
        market_ticker: str,
        trade_payloads: Iterable[dict[str, object]],
    ) -> list[dict[str, object]]:
        """Atomically journal tape and advance every resting order on a ticker.

        Network pagination finishes before this method is called. The writer
        lock then makes tape archival, finite-volume allocation, queue progress,
        partial fills, reservations, and ledger charges one indivisible state
        transition.
        """

        payloads = list(trade_payloads)
        # Public APIs can repeat the boundary trade across pages. A trade id is
        # one finite event, so collapse duplicates before the allocator sees
        # them; the allocation table's uniqueness constraint is too late once
        # duplicate quantities have already been summed in memory.
        normalized_by_id: dict[
            str, tuple[dict[str, object], PublicAggressorTrade]
        ] = {}
        for payload in payloads:
            trade = normalize_public_trade(payload)
            if trade is not None:
                normalized_by_id.setdefault(trade.trade_id, (payload, trade))
        normalized_pairs = list(normalized_by_id.values())
        if not normalized_pairs:
            return []
        all_trades = [trade for _, trade in normalized_pairs]
        trade_by_id = {trade.trade_id: trade for trade in all_trades}
        applied_at = _now()
        updates: list[dict[str, object]] = []

        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            for payload, trade in normalized_pairs:
                conn.execute(
                    """
                    INSERT INTO dataset_kalshi_trades (
                        trade_id, ticker, created_time, count, yes_price,
                        no_price, is_block_trade, taker_book_side, maker_side,
                        raw_json, fetched_at, last_seen_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trade_id) DO UPDATE SET
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        trade.trade_id,
                        market_ticker,
                        trade.created_at.isoformat(),
                        float(trade.quantity),
                        float(trade.yes_price),
                        float(Decimal(1) - trade.yes_price),
                        1 if payload.get("is_block_trade") else 0,
                        str(payload.get("taker_book_side") or ""),
                        trade.maker_side,
                        json.dumps(payload, sort_keys=True),
                        applied_at,
                        applied_at,
                    ),
                )

            rows = conn.execute(
                """
                SELECT *
                FROM paper_orders
                WHERE market_ticker = ?
                  AND status IN ('PAPER_LIMIT_RESTING', 'PAPER_PARTIALLY_FILLED')
                  AND settled_at IS NULL
                  AND closed_at IS NULL
                ORDER BY created_at, id
                """,
                (market_ticker,),
            ).fetchall()
            if not rows:
                return []

            claims: dict[str, float] = {}
            for trade_id, quantity in conn.execute(
                "SELECT trade_id, COALESCE(SUM(quantity), 0) "
                "FROM maker_volume_claims WHERE market_ticker=? GROUP BY trade_id",
                (market_ticker,),
            ).fetchall():
                claims[str(trade_id)] = claims.get(str(trade_id), 0.0) + float(
                    quantity or 0.0
                )
            for trade_id, quantity in conn.execute(
                """
                SELECT trade_id,
                       COALESCE(SUM(queue_quantity + fill_quantity), 0)
                FROM paper_maker_allocations
                WHERE market_ticker=? AND counterfactual=0
                GROUP BY trade_id
                """,
                (market_ticker,),
            ).fetchall():
                claims[str(trade_id)] = claims.get(str(trade_id), 0.0) + float(
                    quantity or 0.0
                )

            capital_orders: list[RestingMakerOrder] = []
            shadow_orders: list[RestingMakerOrder] = []
            rows_by_id: dict[int, sqlite3.Row] = {}
            for row in rows:
                created_at = datetime.fromisoformat(
                    str(row["created_at"]).replace("Z", "+00:00")
                )
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
                limit_price = (
                    row["limit_price"]
                    if row["limit_price"] is not None
                    else row["entry_price"]
                )
                remaining = float(
                    row["remaining_contracts"]
                    if row["remaining_contracts"] is not None
                    else row["contracts"]
                )
                if limit_price is None or remaining <= 0:
                    continue
                order = RestingMakerOrder(
                    order_id=int(row["id"]),
                    side=str(row["side"] or "YES").upper(),  # type: ignore[arg-type]
                    limit_price=Decimal(str(round(float(limit_price), 6))),
                    quantity=Decimal(str(remaining)),
                    queue_ahead=Decimal(
                        str(max(0.0, float(row["queue_remaining"] or 0.0)))
                    ),
                    placed_at=created_at,
                )
                if str(row["account_id"] or "") == RESEARCH_ACCOUNT_ID:
                    shadow_orders.append(order)
                else:
                    capital_orders.append(order)
                rows_by_id[order.order_id] = row

            allocations = allocate_maker_fills(
                apply_volume_claims(all_trades, claims), capital_orders
            )
            for order in shadow_orders:
                shadow_claims = {
                    str(trade_id): float(quantity or 0.0)
                    for trade_id, quantity in conn.execute(
                        """
                        SELECT trade_id,
                               COALESCE(SUM(queue_quantity + fill_quantity), 0)
                        FROM paper_maker_allocations
                        WHERE order_id=? AND counterfactual=1
                        GROUP BY trade_id
                        """,
                        (order.order_id,),
                    ).fetchall()
                }
                allocations[order.order_id] = allocate_maker_fills(
                    apply_volume_claims(all_trades, shadow_claims), [order]
                )[order.order_id]

            shadow_ids = {order.order_id for order in shadow_orders}
            for order_id, allocation in allocations.items():
                consumption = allocation.consumption_by_trade()
                if not consumption:
                    continue
                row = rows_by_id[order_id]
                counterfactual = order_id in shadow_ids
                queue_delta = 0.0
                fill_delta = 0.0
                novel: dict[str, dict[str, float]] = {}
                for trade_id, amounts in consumption.items():
                    trade = trade_by_id[trade_id]
                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO paper_maker_allocations (
                            created_at, execution_model_version, market_ticker,
                            trade_id, order_id, trade_created_at, maker_side,
                            side_price, queue_quantity, fill_quantity,
                            counterfactual, evidence_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            applied_at,
                            EXECUTION_MODEL_VERSION,
                            market_ticker,
                            trade_id,
                            order_id,
                            trade.created_at.isoformat(),
                            trade.maker_side,
                            float(trade.side_price(str(row["side"] or "YES"))),
                            amounts["queue_quantity"],
                            amounts["fill_quantity"],
                            1 if counterfactual else 0,
                            json.dumps(amounts, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount == 0:
                        continue
                    novel[trade_id] = amounts
                    queue_delta += amounts["queue_quantity"]
                    fill_delta += amounts["fill_quantity"]
                if not novel:
                    continue

                requested = float(row["requested_contracts"] or row["contracts"])
                previous_filled = float(row["filled_contracts"] or 0.0)
                previous_remaining = float(
                    row["remaining_contracts"]
                    if row["remaining_contracts"] is not None
                    else requested
                )
                new_filled = min(requested, previous_filled + fill_delta)
                new_remaining = max(0.0, previous_remaining - fill_delta)
                new_queue = max(0.0, float(row["queue_remaining"] or 0.0) - queue_delta)
                if new_remaining <= 1e-9:
                    new_status = "PAPER_FILLED"
                elif new_filled > 1e-9:
                    new_status = "PAPER_PARTIALLY_FILLED"
                else:
                    new_status = "PAPER_LIMIT_RESTING"
                new_reserved = new_remaining * float(row["cost_per_contract"])

                try:
                    evidence = json.loads(row["fill_evidence_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    evidence = {}
                if not isinstance(evidence, dict):
                    evidence = {}
                evidence.update(
                    {
                        "model": "maker_allocator_price_time_v3",
                        "execution_model_version": EXECUTION_MODEL_VERSION,
                        "requested_quantity": requested,
                        "filled_quantity": new_filled,
                        "remaining_quantity": new_remaining,
                        "queue_remaining": new_queue,
                        "research_shadow": counterfactual,
                        "counterfactual": counterfactual,
                    }
                )
                cumulative = evidence.get("consumptions")
                if not isinstance(cumulative, dict):
                    cumulative = {}
                for trade_id, amounts in novel.items():
                    cumulative[trade_id] = amounts
                evidence["consumptions"] = cumulative
                evidence["allocations"] = {
                    trade_id: float(amounts.get("fill_quantity", 0.0))
                    for trade_id, amounts in cumulative.items()
                    if float(amounts.get("fill_quantity", 0.0)) > 0
                }
                evidence["trade_ids"] = sorted(cumulative)

                conn.execute(
                    """
                    UPDATE paper_orders
                    SET status=?,
                        contracts=?,
                        filled_contracts=?,
                        remaining_contracts=?,
                        queue_remaining=?,
                        reserved_cost=?,
                        filled_at=CASE
                            WHEN ? > 0 THEN COALESCE(filled_at, ?)
                            ELSE filled_at
                        END,
                        execution_model_version=?,
                        fill_evidence_json=?
                    WHERE id=?
                    """,
                    (
                        new_status,
                        new_filled if new_filled > 0 else requested,
                        new_filled,
                        new_remaining,
                        new_queue,
                        new_reserved,
                        fill_delta,
                        applied_at,
                        EXECUTION_MODEL_VERSION,
                        json.dumps(evidence, sort_keys=True),
                        order_id,
                    ),
                )

                if fill_delta > 0 and row["account_id"]:
                    for trade_id, amounts in novel.items():
                        trade_fill = float(amounts["fill_quantity"])
                        if trade_fill <= 0:
                            continue
                        cost = trade_fill * float(row["cost_per_contract"])
                        suffix = f"{EXECUTION_MODEL_VERSION}:{trade_id}"
                        self._record_ledger_event(
                            conn,
                            account_id=str(row["account_id"]),
                            order_id=order_id,
                            event_type="RESERVATION_RELEASE",
                            amount=cost,
                            idempotency_key=f"order:{order_id}:maker-release:{suffix}",
                            details={"filled_quantity": trade_fill},
                        )
                        self._record_ledger_event(
                            conn,
                            account_id=str(row["account_id"]),
                            order_id=order_id,
                            event_type="ENTRY_FILL",
                            amount=-cost,
                            idempotency_key=f"order:{order_id}:maker-fill:{suffix}",
                            details={
                                "filled_quantity": trade_fill,
                                "execution_model_version": EXECUTION_MODEL_VERSION,
                                "trade_id": trade_id,
                            },
                        )
                updates.append(
                    {
                        "order_id": order_id,
                        "previous_status": str(row["status"]),
                        "status": new_status,
                        "queue_consumed": queue_delta,
                        "filled_quantity": fill_delta,
                        "total_filled_quantity": new_filled,
                        "remaining_quantity": new_remaining,
                        "counterfactual": counterfactual,
                    }
                )
        return updates

    @staticmethod
    def _record_maker_volume_claims(
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        evidence: dict[str, object],
    ) -> None:
        """Persist how much of each public trade this fill consumed.

        Later monitor passes subtract these claims from the available trade
        volume before allocating to still-resting orders, so a trade's finite
        volume can never be credited again once an order filled from it
        (audit EX-01 cross-pass conservation). Research shadow fills are
        counterfactual and never claim public volume.
        """

        if evidence.get("research_shadow") or evidence.get("counterfactual"):
            return
        allocations = evidence.get("allocations")
        if not isinstance(allocations, dict):
            return
        created_at = _now()
        for trade_id, quantity in allocations.items():
            try:
                amount = float(quantity)
            except (TypeError, ValueError):
                continue
            if amount <= 0:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO maker_volume_claims "
                "(created_at, market_ticker, trade_id, order_id, quantity) "
                "VALUES (?, ?, ?, ?, ?)",
                (created_at, str(row["market_ticker"]), str(trade_id), int(row["id"]), amount),
            )

    def maker_volume_claims_for_ticker(self, market_ticker: str) -> dict[str, float]:
        """Total public volume already claimed per trade id on this market."""

        with self.connect() as conn:
            claims: dict[str, float] = {}
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='maker_volume_claims'"
            ).fetchone() is not None:
                for trade_id, total in conn.execute(
                    "SELECT trade_id, COALESCE(SUM(quantity), 0) "
                    "FROM maker_volume_claims WHERE market_ticker = ? GROUP BY trade_id",
                    (market_ticker,),
                ).fetchall():
                    claims[str(trade_id)] = claims.get(str(trade_id), 0.0) + float(
                        total or 0.0
                    )
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='paper_maker_allocations'"
            ).fetchone() is not None:
                for trade_id, total in conn.execute(
                    """
                    SELECT trade_id,
                           COALESCE(SUM(queue_quantity + fill_quantity), 0)
                    FROM paper_maker_allocations
                    WHERE market_ticker = ? AND counterfactual = 0
                    GROUP BY trade_id
                    """,
                    (market_ticker,),
                ).fetchall():
                    claims[str(trade_id)] = claims.get(str(trade_id), 0.0) + float(
                        total or 0.0
                    )
        return claims

    def cancel_resting_limit_order(self, order_id: int, *, reason: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM paper_orders WHERE id=? "
                "AND status IN ('PAPER_LIMIT_RESTING', 'PAPER_PARTIALLY_FILLED')",
                (order_id,),
            ).fetchone()
            if row is None:
                return self._order(order_id)
            cancelled_at = _now()
            filled = float(row["filled_contracts"] or 0.0)
            next_status = "PAPER_PARTIAL_EXPIRED" if filled > 0 else "PAPER_EXPIRED"
            conn.execute(
                "UPDATE paper_orders SET status=?, cancelled_at=?, "
                "remaining_contracts=0, queue_remaining=0, reserved_cost=0, "
                "outcome_diagnostics_json=? WHERE id=?",
                (
                    next_status,
                    cancelled_at,
                    json.dumps(
                        {
                            "event": "cancellation",
                            "reason": reason,
                            "unfilled_quantity_cancelled": float(
                                row["remaining_contracts"] or 0.0
                            ),
                            "filled_quantity_retained": filled,
                        },
                        sort_keys=True,
                    ),
                    order_id,
                ),
            )
            if row["account_id"]:
                self._record_ledger_event(
                    conn, account_id=row["account_id"],
                    order_id=order_id, event_type="RESERVATION_RELEASE",
                    amount=float(row["reserved_cost"] or 0.0),
                    idempotency_key=f"order:{order_id}:cancel-release",
                    details={"reason": reason},
                )
        return self._order(order_id)

    def mark_arbitrage_group_degraded(
        self,
        order_ids: list[int],
        *,
        group_id: str,
        reason: str,
    ) -> None:
        """Keep partial-box compensation explicit in the order audit trail."""

        if not order_ids:
            return
        degraded_group_id = f"DEGRADED-{group_id}"
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in order_ids)
            rows = conn.execute(
                f"SELECT id, status, outcome_diagnostics_json FROM paper_orders "
                f"WHERE id IN ({placeholders})",
                order_ids,
            ).fetchall()
            for row in rows:
                try:
                    details = json.loads(row["outcome_diagnostics_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    details = {}
                if not isinstance(details, dict):
                    details = {}
                details.update(
                    {
                        "event": "arbitrage_compensation",
                        "arbitrage_group_status": "DEGRADED",
                        "original_group_id": group_id,
                        "reason": reason,
                        "compensated_status": row["status"],
                    }
                )
                conn.execute(
                    "UPDATE paper_orders SET group_id=?, outcome_diagnostics_json=? WHERE id=?",
                    (degraded_group_id, json.dumps(details, sort_keys=True), row["id"]),
                )

    def expire_stale_resting_orders(self, *, now: str | None = None) -> int:
        cutoff = now or _now()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM paper_orders WHERE status IN "
                "('PAPER_LIMIT_RESTING', 'PAPER_PARTIALLY_FILLED') "
                "AND expires_at IS NOT NULL AND expires_at <= ? ORDER BY expires_at, id",
                (cutoff,),
            ).fetchall()
        expired = 0
        for (order_id,) in rows:
            row = self.cancel_resting_limit_order(int(order_id), reason="15-minute maker TTL expired")
            expired += int(
                row is not None
                and row["status"] in {"PAPER_EXPIRED", "PAPER_PARTIAL_EXPIRED"}
            )
        return expired

    def record_monitor_snapshot(
        self,
        order: sqlite3.Row,
        *,
        side: str,
        action: str,
        reason: str | None = None,
        market_status: str | None = None,
        live_bid: float | None = None,
        exit_fee_per_contract: float | None = None,
        net_exit_per_contract: float | None = None,
        unrealized_pnl: float | None = None,
        unrealized_roi: float | None = None,
        model_read: dict[str, object] | None = None,
    ) -> int:
        created_at = _now()
        diagnostics_json = json.dumps(
            _monitor_diagnostics_payload(
                order,
                created_at=created_at,
                side=side,
                action=action,
                reason=reason,
                market_status=market_status,
                live_bid=live_bid,
                exit_fee_per_contract=exit_fee_per_contract,
                net_exit_per_contract=net_exit_per_contract,
                unrealized_pnl=unrealized_pnl,
                unrealized_roi=unrealized_roi,
                model_read=model_read,
            ),
            sort_keys=True,
        )
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO paper_monitor_snapshots (
                    created_at, order_id, target_date, market_ticker, side,
                    action, reason, market_status, live_bid,
                    exit_fee_per_contract, net_exit_per_contract,
                    unrealized_pnl, unrealized_roi, diagnostics_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    int(order["id"]),
                    order["target_date"],
                    order["market_ticker"],
                    side.upper(),
                    action,
                    reason,
                    market_status,
                    live_bid,
                    exit_fee_per_contract,
                    net_exit_per_contract,
                    unrealized_pnl,
                    unrealized_roi,
                    diagnostics_json,
                ),
            )
            return int(cursor.lastrowid)

    def paper_orders(
        self,
        limit: int = 50,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> list[sqlite3.Row]:
        filters, params = _date_filters(since, until)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                f"SELECT * FROM paper_orders {where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()

    def settle_paper_orders(
        self,
        target_date: str,
        settlement_high_f: float,
        *,
        series_ticker: str | None = None,
    ) -> int:
        # Resolve bins against the integer °F Kalshi settles on, never a
        # fractional NWS/provisional high. Without this, a true high of 65.4
        # would settle the 65-or-below bin differently than Kalshi does, and a
        # value straddling a half-degree bin edge (e.g. 65.5 -> 66) flips the
        # YES/NO outcome. This single chokepoint covers manual --settlement-high,
        # CLISFO, and the WeatherEdge ground-truth fallback.
        settlement_high_f = _integer_settlement_high_f(settlement_high_f)
        settled_at = _now()
        settled = 0
        series_filter = ""
        series_params: tuple = ()
        if series_ticker:
            # A settlement high is one station's number; it must never resolve
            # another city's bins that happen to share the calendar date.
            series_filter = " AND market_ticker LIKE ?"
            series_params = (f"{series_ticker}-%",)
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            # Reserve the writer slot BEFORE reading the unsettled rows so the
            # snapshot and the conditional UPDATEs are one atomic transaction.
            # The read used to run on its own connection (_unsettled_orders),
            # closing before this writer opened — a TOCTOU window in which the
            # monitor (every ~2 minutes) could close a position between read and
            # write. BEGIN IMMEDIATE takes SQLite's RESERVED lock now, so a
            # concurrent close either blocks on busy_timeout until we commit or
            # commits first (and is then visible in our snapshot). The per-row
            # status/closed_at guard below stays as defense-in-depth.
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT *
                FROM paper_orders
                WHERE target_date = ?
                  AND status IN (
                      'PAPER_FILLED',
                      'PAPER_PARTIALLY_FILLED',
                      'PAPER_PARTIAL_EXPIRED'
                  )
                  AND settled_at IS NULL
                """ + series_filter,
                (target_date, *series_params),
            ).fetchall()
            # Resting limit orders that never crossed before this target
            # resolved can never fill now. Expire them (zero PnL) so they stop
            # consuming the per-target exposure cap, blocking re-entry, and
            # showing as perpetual pending exposure on the dashboard.
            resting_rows = conn.execute(
                """
                SELECT *
                FROM paper_orders
                WHERE target_date = ?
                  AND status = 'PAPER_LIMIT_RESTING'
                  AND settled_at IS NULL
                  AND closed_at IS NULL
                """ + series_filter,
                (target_date, *series_params),
            ).fetchall()
            for row in resting_rows:
                outcome_json = json.dumps(
                    _outcome_diagnostics_payload(
                        row,
                        event="expiration",
                        resolved_at=settled_at,
                        settlement_high_f=settlement_high_f,
                        resolved_yes=None,
                        position_won=None,
                        realized_pnl=0.0,
                    ),
                    sort_keys=True,
                )
                conn.execute(
                    """
                    UPDATE paper_orders
                    SET status = 'PAPER_EXPIRED',
                        settled_at = ?,
                        cancelled_at = ?,
                        settlement_high_f = ?,
                        realized_pnl = 0.0,
                        reserved_cost = 0,
                        outcome_diagnostics_json = ?
                    WHERE id = ?
                      AND status = 'PAPER_LIMIT_RESTING'
                      AND settled_at IS NULL
                      AND closed_at IS NULL
                    """,
                    (settled_at, settled_at, settlement_high_f, outcome_json, row["id"]),
                )
                if row["account_id"]:
                    self._record_ledger_event(
                        conn, account_id=row["account_id"],
                        order_id=int(row["id"]), event_type="RESERVATION_RELEASE",
                        amount=float(row["reserved_cost"] or 0.0),
                        idempotency_key=f"order:{row['id']}:settlement-expire-release",
                    )
            for row in rows:
                if str(row["status"]) == "PAPER_PARTIALLY_FILLED":
                    # Resolution ends the unfilled quote, but the already
                    # executed position still settles at its actual quantity.
                    if row["account_id"]:
                        self._record_ledger_event(
                            conn,
                            account_id=row["account_id"],
                            order_id=int(row["id"]),
                            event_type="RESERVATION_RELEASE",
                            amount=float(row["reserved_cost"] or 0.0),
                            idempotency_key=(
                                f"order:{row['id']}:settlement-partial-release"
                            ),
                            details={
                                "unfilled_quantity_cancelled": float(
                                    row["remaining_contracts"] or 0.0
                                )
                            },
                        )
                    conn.execute(
                        "UPDATE paper_orders SET remaining_contracts=0, "
                        "queue_remaining=0, reserved_cost=0, cancelled_at=? "
                        "WHERE id=?",
                        (settled_at, row["id"]),
                    )
                resolved_yes = _row_resolves_yes(row, settlement_high_f)
                side = _row_side(row)
                position_wins = resolved_yes if side == "YES" else not resolved_yes
                cost = float(row["cost_per_contract"])
                contracts = float(row["contracts"])
                realized_pnl = contracts * ((1.0 - cost) if position_wins else -cost)
                outcome_json = json.dumps(
                    _outcome_diagnostics_payload(
                        row,
                        event="settlement",
                        resolved_at=settled_at,
                        settlement_high_f=settlement_high_f,
                        resolved_yes=resolved_yes,
                        position_won=position_wins,
                        realized_pnl=realized_pnl,
                    ),
                    sort_keys=True,
                )
                # The status/closed_at guard makes settlement a no-op on a row a
                # concurrent monitor close already flipped, instead of silently
                # overwriting its realized_pnl. Count real row changes, not the
                # read size.
                cursor = conn.execute(
                    """
                    UPDATE paper_orders
                    SET
                        settled_at = ?,
                        settlement_high_f = ?,
                        resolved_yes = ?,
                        realized_pnl = ?,
                        status = 'PAPER_SETTLED',
                        outcome_diagnostics_json = ?
                    WHERE id = ?
                      AND status IN (
                          'PAPER_FILLED',
                          'PAPER_PARTIALLY_FILLED',
                          'PAPER_PARTIAL_EXPIRED'
                      )
                      AND settled_at IS NULL
                      AND closed_at IS NULL
                    """,
                    (
                        settled_at,
                        settlement_high_f,
                        1 if resolved_yes else 0,
                        realized_pnl,
                        outcome_json,
                        row["id"],
                    ),
                )
                if cursor.rowcount:
                    proceeds = contracts if position_wins else 0.0
                    if row["account_id"]:
                        self._record_ledger_event(
                            conn, account_id=row["account_id"],
                            order_id=int(row["id"]), event_type="SETTLEMENT_PROCEEDS",
                            amount=proceeds,
                            idempotency_key=f"order:{row['id']}:settlement-proceeds",
                            details={"position_won": position_wins},
                        )
                settled += cursor.rowcount
        return settled

    def verify_paper_settlements(
        self,
        settlements,
        *,
        intervals: dict[str, tuple[str, str]],
    ) -> dict:
        """Audit booked settlements against final truth without changing orders."""

        truth = normalize_settlement_truth(settlements)
        checked_at = _now()
        checked: list[dict] = []
        missing_truth = 0
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            clauses = []
            params: list[str] = []
            for series, (lower, upper) in intervals.items():
                clauses.append("(market_ticker LIKE ? AND target_date BETWEEN ? AND ?)")
                params.extend((f"{series}-%", lower, upper))
            if not clauses:
                return {"checked": [], "mismatches": 0, "missing_truth": 0}
            rows = conn.execute(
                "SELECT id, market_ticker, target_date, settlement_high_f "
                "FROM paper_orders WHERE status='PAPER_SETTLED' "
                "AND settled_at IS NOT NULL AND settlement_high_f IS NOT NULL "
                f"AND ({' OR '.join(clauses)}) ORDER BY target_date, id",
                params,
            ).fetchall()
            for row in rows:
                final_high = settlement_for_market(
                    truth, str(row["market_ticker"]), str(row["target_date"])
                )
                booked_high = _integer_settlement_high_f(row["settlement_high_f"])
                if final_high is None:
                    missing_truth += 1
                    status = "MISSING_FINAL"
                else:
                    final_high = _integer_settlement_high_f(final_high)
                    status = "MATCH" if booked_high == final_high else "MISMATCH"
                result = {
                    "order_id": int(row["id"]),
                    "market_ticker": str(row["market_ticker"]),
                    "target_date": str(row["target_date"]),
                    "booked_high_f": booked_high,
                    "final_high_f": final_high,
                    "verification_status": status,
                }
                checked.append(result)
                conn.execute(
                    """
                    INSERT INTO paper_settlement_verifications
                        (order_id, checked_at, market_ticker, target_date,
                         booked_high_f, final_high_f, verification_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(order_id) DO UPDATE SET
                        checked_at=excluded.checked_at,
                        market_ticker=excluded.market_ticker,
                        target_date=excluded.target_date,
                        booked_high_f=excluded.booked_high_f,
                        final_high_f=excluded.final_high_f,
                        verification_status=excluded.verification_status
                    """,
                    (
                        result["order_id"],
                        checked_at,
                        result["market_ticker"],
                        result["target_date"],
                        booked_high,
                        final_high,
                        status,
                    ),
                )
        return {
            "checked": checked,
            "mismatches": sum(
                row["verification_status"] == "MISMATCH" for row in checked
            ),
            "missing_truth": missing_truth,
        }

    def close_paper_order(
        self,
        order_id: int,
        exit_price: float,
        *,
        max_quantity: float | None = None,
        liquidity_evidence: dict[str, object] | None = None,
    ) -> sqlite3.Row:
        """Close an open paper position at ``exit_price``, depth-aware.

        ``max_quantity`` is the quantity the recorded liquidity supports (the
        displayed top-bid size at decision time). When it covers the whole
        position the order closes fully, as before. When it is smaller, only
        the supported quantity is realized as its own immutable PAPER_CLOSED
        lot row (``parent_order_id`` links it back) and the remainder stays
        open on the original order -- an exit can never book more quantity at
        a quote than the recorded liquidity supports (audit EX-02). The
        executed quantity, requested quantity, and liquidity evidence are
        persisted in ``outcome_diagnostics_json["exit_execution"]``.
        """

        if exit_price <= 0 or exit_price >= 1:
            raise ValueError("exit price must be between 0.01 and 0.99")
        row = self._open_order(order_id)
        if row is None:
            raise ValueError(f"no open paper order found with id {order_id}")
        contracts = float(row["contracts"])
        executed = contracts if max_quantity is None else min(contracts, float(max_quantity))
        if executed <= 0:
            raise ValueError(
                f"paper order {order_id} has no executable quantity at the recorded liquidity"
            )
        entry_cost = float(row["cost_per_contract"])
        exit_fee = quadratic_fee_average_per_contract(
            exit_price, executed, series_ticker=str(row["market_ticker"])
        )
        realized_pnl = executed * (exit_price - exit_fee - entry_cost)
        # Persist resolved_yes on close so a closed order is classified by the
        # same resolved_yes-driven path as a settled order (db.py settle path),
        # not the realized_pnl > 0 fallback in _row_position_won. A break-even
        # close (realized_pnl == 0) is recorded as resolved_yes = NULL so it is
        # treated as undecided rather than silently bucketed as a loss.
        side = _row_side(row)
        if abs(realized_pnl) < 1e-9:
            resolved_yes: int | None = None
        else:
            position_won = realized_pnl > 0.0
            resolved_yes = 1 if (position_won if side == "YES" else not position_won) else 0
        closed_at = _now()
        payload = _outcome_diagnostics_payload(
            row,
            event="close",
            resolved_at=closed_at,
            settlement_high_f=None,
            resolved_yes=bool(resolved_yes) if resolved_yes is not None else None,
            position_won=None if abs(realized_pnl) < 1e-9 else realized_pnl > 0.0,
            realized_pnl=realized_pnl,
            exit_price=exit_price,
            exit_fee_per_contract=exit_fee,
        )
        evidence = dict(liquidity_evidence or {})
        depth_value = evidence.get(
            "displayed_depth", evidence.get("displayed_bid_size")
        )
        try:
            displayed_depth = float(depth_value)
        except (TypeError, ValueError):
            displayed_depth = 0.0
        depth_is_fresh = depth_observation_is_contemporaneous(
            evidence.get("observed_at"), closed_at
        )
        if displayed_depth + 1e-9 < executed and displayed_depth > 0:
            verification_status = "INSUFFICIENT"
        elif displayed_depth >= executed > 0 and not depth_is_fresh:
            verification_status = "STALE"
        elif (
            max_quantity is not None
            and displayed_depth >= executed > 0
            and bool(evidence.get("source"))
            and depth_is_fresh
        ):
            verification_status = "VERIFIED"
        else:
            verification_status = "UNVERIFIED"
        payload["exit_execution"] = {
            **evidence,
            "requested_quantity": contracts,
            "executed_quantity": executed,
            "vwap": exit_price,
            "fee_per_contract": exit_fee,
            "execution_model_version": EXECUTION_MODEL_VERSION,
            "displayed_depth": displayed_depth if displayed_depth > 0 else None,
            "verification_status": verification_status,
        }
        outcome_json = json.dumps(payload, sort_keys=True)
        partial = executed < contracts - 1e-9
        result_id = order_id
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            open_statuses = (
                "PAPER_FILLED",
                "PAPER_PARTIALLY_FILLED",
                "PAPER_PARTIAL_EXPIRED",
            )
            # Guard the close on the same open-state predicate settle uses, plus
            # an unchanged-contracts check, then require it to have actually
            # changed a row. Between _open_order() above and this UPDATE, a
            # concurrent settle or close (the q2min monitor and the settle path
            # race on one DB) can flip this order to PAPER_SETTLED or shrink it.
            # A bare WHERE id = ? would then overwrite the true settlement
            # outcome with an intraday exit price, permanently corrupting the
            # paper PnL ledger, equity curve, and circuit breaker.
            if partial:
                cursor = conn.execute(
                    """
                    UPDATE paper_orders
                    SET contracts = ?,
                        status = CASE
                            WHEN status = 'PAPER_PARTIALLY_FILLED'
                            THEN 'PAPER_PARTIAL_EXPIRED'
                            ELSE status
                        END,
                        remaining_contracts = CASE
                            WHEN status = 'PAPER_PARTIALLY_FILLED' THEN 0
                            ELSE remaining_contracts
                        END,
                        queue_remaining = CASE
                            WHEN status = 'PAPER_PARTIALLY_FILLED' THEN 0
                            ELSE queue_remaining
                        END,
                        reserved_cost = CASE
                            WHEN status = 'PAPER_PARTIALLY_FILLED' THEN 0
                            ELSE reserved_cost
                        END,
                        cancelled_at = CASE
                            WHEN status = 'PAPER_PARTIALLY_FILLED' THEN ?
                            ELSE cancelled_at
                        END
                    WHERE id = ?
                      AND status IN (?, ?, ?)
                      AND settled_at IS NULL
                      AND closed_at IS NULL
                      AND contracts = ?
                    """,
                    (
                        contracts - executed,
                        closed_at,
                        order_id,
                        *open_statuses,
                        contracts,
                    ),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE paper_orders
                    SET
                        status = 'PAPER_CLOSED',
                        closed_at = ?,
                        exit_price = ?,
                        exit_fee_per_contract = ?,
                        resolved_yes = ?,
                        realized_pnl = ?,
                        outcome_diagnostics_json = ?,
                        remaining_contracts = 0,
                        queue_remaining = 0,
                        reserved_cost = 0,
                        cancelled_at = CASE
                            WHEN status = 'PAPER_PARTIALLY_FILLED' THEN ?
                            ELSE cancelled_at
                        END
                    WHERE id = ?
                      AND status IN (?, ?, ?)
                      AND settled_at IS NULL
                      AND closed_at IS NULL
                      AND contracts = ?
                    """,
                    (
                        closed_at,
                        exit_price,
                        exit_fee,
                        resolved_yes,
                        realized_pnl,
                        outcome_json,
                        closed_at,
                        order_id,
                        *open_statuses,
                        contracts,
                    ),
                )
            if cursor.rowcount == 0:
                # Already settled/closed/resized concurrently. Raise instead of
                # returning the resolved row so the caller does not double-book
                # it; the paper-monitor loop catches ValueError/RuntimeError per
                # order and keeps inspecting the rest of the book.
                raise ValueError(
                    f"paper order {order_id} was resolved concurrently before close"
                )
            if partial:
                result_id = self._insert_partial_close_lot(
                    conn,
                    row,
                    executed=executed,
                    closed_at=closed_at,
                    exit_price=exit_price,
                    exit_fee=exit_fee,
                    realized_pnl=realized_pnl,
                    resolved_yes=resolved_yes,
                    outcome_json=outcome_json,
                )
            if row["status"] == "PAPER_PARTIALLY_FILLED" and row["account_id"]:
                self._record_ledger_event(
                    conn,
                    account_id=row["account_id"],
                    order_id=order_id,
                    event_type="RESERVATION_RELEASE",
                    amount=float(row["reserved_cost"] or 0.0),
                    idempotency_key=f"order:{order_id}:close-partial-release",
                    details={
                        "reason": "position close cancelled unfilled maker quantity",
                        "unfilled_quantity_cancelled": float(
                            row["remaining_contracts"] or 0.0
                        ),
                    },
                )
            net_proceeds = executed * (exit_price - exit_fee)
            if row["account_id"]:
                self._record_ledger_event(
                    conn, account_id=row["account_id"], order_id=result_id,
                    event_type="EXIT_PROCEEDS", amount=net_proceeds,
                    idempotency_key=f"order:{result_id}:exit-proceeds",
                    details={
                        "exit_price": exit_price,
                        "exit_fee_per_contract": exit_fee,
                        "executed_quantity": executed,
                        **({"partial_close_of": order_id} if partial else {}),
                    },
                )
        closed = self._order(result_id)
        if closed is None:
            raise RuntimeError(f"paper order {result_id} disappeared after close")
        return closed

    @staticmethod
    def _insert_partial_close_lot(
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        executed: float,
        closed_at: str,
        exit_price: float,
        exit_fee: float,
        realized_pnl: float,
        resolved_yes: int | None,
        outcome_json: str,
    ) -> int:
        """Materialize the executed slice of a partial close as its own row.

        The original journal row stays immutable apart from its remaining
        quantity; the executed lot becomes a PAPER_CLOSED child row that keeps
        entry attribution (``parent_order_id``) without duplicating the maker
        fill evidence, so per-trade volume accounting stays single-counted.
        """

        values = {key: row[key] for key in row.keys() if key != "id"}
        values.update(
            {
                "contracts": executed,
                "status": "PAPER_CLOSED",
                "closed_at": closed_at,
                "exit_price": exit_price,
                "exit_fee_per_contract": exit_fee,
                "resolved_yes": resolved_yes,
                "realized_pnl": realized_pnl,
                "outcome_diagnostics_json": outcome_json,
                "reserved_cost": 0,
                "requested_contracts": executed,
                "filled_contracts": executed,
                "remaining_contracts": 0,
                "queue_remaining": 0,
                "fill_evidence_json": None,
                "parent_order_id": int(row["id"]),
            }
        )
        columns = ", ".join(values.keys())
        placeholders = ", ".join("?" for _ in values)
        cursor = conn.execute(
            f"INSERT INTO paper_orders ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        return int(cursor.lastrowid)

    def open_paper_order(self, order_id: int) -> sqlite3.Row | None:
        return self._open_order(order_id)

    def resting_paper_orders(self, limit: int | None = None) -> list[sqlite3.Row]:
        """Every live resting maker limit order, for the monitor's fill pass."""

        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            query = """
                SELECT *
                FROM paper_orders
                WHERE status IN ('PAPER_LIMIT_RESTING', 'PAPER_PARTIALLY_FILLED')
                  AND settled_at IS NULL
                  AND closed_at IS NULL
                ORDER BY created_at, id
                """
            params: tuple[object, ...] = ()
            if limit is not None:
                query += " LIMIT ?"
                params = (limit,)
            return conn.execute(query, params).fetchall()

    def open_paper_orders(self, limit: int | None = None) -> list[sqlite3.Row]:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            query = """
                SELECT *
                FROM paper_orders
                WHERE status IN (
                    'PAPER_FILLED', 'PAPER_PARTIALLY_FILLED', 'PAPER_PARTIAL_EXPIRED'
                )
                  AND settled_at IS NULL
                  AND closed_at IS NULL
                ORDER BY created_at DESC
                """
            params: tuple[object, ...] = ()
            if limit is not None:
                query += " LIMIT ?"
                params = (limit,)
            return conn.execute(query, params).fetchall()

    def open_no_basket_orders(
        self,
        target_date: str,
        *,
        risk_profile: str | None = None,
    ) -> list[sqlite3.Row]:
        filters = [
            "target_date = ?",
            "status IN ('PAPER_FILLED', 'PAPER_PARTIALLY_FILLED', 'PAPER_PARTIAL_EXPIRED')",
            "settled_at IS NULL",
            "closed_at IS NULL",
            "UPPER(COALESCE(side, 'YES')) = 'NO'",
        ]
        params: list[object] = [target_date]
        if risk_profile is not None:
            filters.append("COALESCE(risk_profile, 'live') = ?")
            params.append(normalize_risk_profile_name(risk_profile))
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                f"""
                SELECT *
                FROM paper_orders
                WHERE {' AND '.join(filters)}
                ORDER BY created_at, id
                """,
                params,
            ).fetchall()

    def prune_decision_snapshots(
        self,
        *,
        full_days: int = 7,
        dedup_days: int = 45,
    ) -> dict[str, int]:
        """Retention for the highest-volume table on the disk-bound box.

        Fifteen cities at a 5-minute scan write ~60k rejection snapshots
        (~0.5 GB) per day; unbounded growth filled the old single-city box and
        thrashed the strategy-lab pass. Policy: everything stays full-fidelity
        for ``full_days``; between ``full_days`` and ``dedup_days`` only the
        LAST snapshot per (market, side, target_date) survives -- the
        end-of-day context of why the book said what it said -- plus every
        approved/signal-approved row; beyond ``dedup_days`` only approved
        rows remain. Approved rows are never deleted. The scheduled caller
        runs the archive gate first; after decision pruning, archived context
        rows are removed only when no retained decision references them.
        """

        if full_days < 1 or dedup_days <= full_days:
            raise ValueError("need dedup_days > full_days >= 1")
        with self.connect() as conn:
            dedup_cursor = conn.execute(
                """
                DELETE FROM decision_snapshots
                WHERE created_at < datetime('now', ?)
                  AND created_at >= datetime('now', ?)
                  AND COALESCE(approved, 0) = 0
                  AND COALESCE(signal_approved, 0) = 0
                  AND id NOT IN (
                      SELECT MAX(id) FROM decision_snapshots
                      GROUP BY market_ticker, side, target_date
                  )
                """,
                (f"-{full_days} days", f"-{dedup_days} days"),
            )
            drop_cursor = conn.execute(
                """
                DELETE FROM decision_snapshots
                WHERE created_at < datetime('now', ?)
                  AND COALESCE(approved, 0) = 0
                  AND COALESCE(signal_approved, 0) = 0
                """,
                (f"-{dedup_days} days",),
            )
            context_cursor = conn.execute(
                """
                DELETE FROM scan_context_snapshots
                WHERE created_at < datetime('now', ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM decision_snapshots
                      WHERE decision_snapshots.scan_context_id = scan_context_snapshots.id
                  )
                """,
                (f"-{full_days} days",),
            )
            return {
                "deduped": dedup_cursor.rowcount,
                "dropped": drop_cursor.rowcount,
                "contexts_dropped": context_cursor.rowcount,
            }

    def open_paper_target_dates(self, *, series_ticker: str | None = None) -> list[str]:
        query = """
            SELECT DISTINCT target_date
            FROM paper_orders
            WHERE status IN (
                'PAPER_FILLED', 'PAPER_LIMIT_RESTING',
                'PAPER_PARTIALLY_FILLED', 'PAPER_PARTIAL_EXPIRED'
            )
              AND settled_at IS NULL
              AND closed_at IS NULL
        """
        params: tuple = ()
        if series_ticker:
            query += " AND market_ticker LIKE ?"
            params = (f"{series_ticker}-%",)
        query += " ORDER BY target_date"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [str(row[0]) for row in rows]

    def market_backtest_summary(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, float]:
        return market_backtest_summary(self, since=since, until=until)

    def paper_equity(self, starting_bankroll: float, *, risk_profile: str | None = None) -> float:
        """Live paper equity = starting bankroll + realized PnL to date.

        Kelly and the percentage risk caps should fraction CURRENT wealth, not a
        frozen notional. This is the realized-equity base used when
        size_against_live_equity is enabled (open-position mark-to-market is left
        out so the value is deterministic for a given settled history).
        """

        profile_filter, profile_params = _paper_profile_filter(risk_profile)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT COALESCE(SUM(realized_pnl), 0)
                FROM paper_orders
                WHERE realized_pnl IS NOT NULL
                  AND status != 'REJECTED'
                  AND status != 'PAPER_EXPIRED'
                  {profile_filter}
                """,
                tuple(profile_params),
            ).fetchone()
        return float(starting_bankroll) + float(row[0] or 0.0)

    def paper_entry_pause_reason(
        self,
        risk_profile: str | None,
        *,
        bankroll: float,
        target_date: str,
        min_resolved_trades: int | None = None,
        max_resolved_roi: float | None = None,
        daily_loss_pct: float | None = None,
        lookback_days: int = PAUSE_LOOKBACK_DAYS,
        now: datetime | None = None,
    ) -> str | None:
        """Circuit breaker for paper entries, per profile.

        Extends the original fast-feedback-only breaker to the trading-intent
        profiles (the ones that could one day fund real money) with looser
        thresholds. The resolved-ROI gate now uses a rolling lookback window so
        an unlucky early cohort can age out and the pause can clear, and the
        daily-loss gate measures loss realized on the current fixed-PST
        settlement day (via closed_at/settled_at) rather than loss attributable
        to whichever target date happens to be settling.
        """

        profile = normalize_risk_profile_name(risk_profile)
        thresholds = PAUSE_THRESHOLDS.get(profile)
        if thresholds is None:
            return None
        d_min, d_roi, d_daily = thresholds
        min_resolved_trades = d_min if min_resolved_trades is None else min_resolved_trades
        max_resolved_roi = d_roi if max_resolved_roi is None else max_resolved_roi
        daily_loss_pct = d_daily if daily_loss_pct is None else daily_loss_pct

        now_utc = now.astimezone(UTC) if now is not None else datetime.now(UTC)
        window_start = (now_utc - timedelta(days=lookback_days)).isoformat()
        pst_now = now_utc.astimezone(SETTLEMENT_TZ)
        day_start = (
            pst_now.replace(hour=0, minute=0, second=0, microsecond=0)
            .astimezone(UTC)
            .isoformat()
        )

        with self.connect() as conn:
            resolved = conn.execute(
                """
                SELECT
                    COUNT(DISTINCT root.id) AS trades,
                    COALESCE(SUM(lot.realized_pnl), 0) AS pnl,
                    COALESCE(SUM(lot.contracts * lot.cost_per_contract), 0) AS capital
                FROM paper_orders lot
                JOIN paper_orders root
                  ON root.id = COALESCE(lot.parent_order_id, lot.id)
                WHERE root.status IN ('PAPER_SETTLED', 'PAPER_CLOSED')
                  AND root.realized_pnl IS NOT NULL
                  AND lot.realized_pnl IS NOT NULL
                  AND lot.status != 'REJECTED'
                  AND lot.status != 'PAPER_EXPIRED'
                  AND COALESCE(root.risk_profile, 'live') = ?
                  AND COALESCE(root.closed_at, root.settled_at) >= ?
                """,
                (profile, window_start),
            ).fetchone()
            daily = conn.execute(
                """
                SELECT COALESCE(SUM(realized_pnl), 0) AS pnl
                FROM paper_orders
                WHERE realized_pnl IS NOT NULL
                  AND status != 'REJECTED'
                  AND status != 'PAPER_EXPIRED'
                  AND COALESCE(risk_profile, 'live') = ?
                  AND COALESCE(closed_at, settled_at) >= ?
                """,
                (profile, day_start),
            ).fetchone()

        trades = int(resolved[0] or 0)
        pnl = float(resolved[1] or 0.0)
        capital = float(resolved[2] or 0.0)
        roi = pnl / capital if capital > 0 else 0.0
        if trades >= min_resolved_trades and roi <= max_resolved_roi:
            return (
                f"{profile} paused: resolved ROI {roi:.1%} across "
                f"{trades} paper trade(s) in the last {lookback_days}d is below "
                f"{max_resolved_roi:.0%}; recording near-misses only"
            )

        daily_pnl = float(daily[0] or 0.0)
        daily_loss_limit = -abs(float(bankroll) * daily_loss_pct)
        if daily_pnl <= daily_loss_limit:
            return (
                f"{profile} paused: daily loss ${daily_pnl:.2f} reached "
                f"${daily_loss_limit:.2f}; recording near-misses only"
            )
        return None

    def sampled_decision_rows(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        approved_only: bool = False,
        min_quality: float | None = None,
        pre_resolution_only: bool = True,
        sample_mode: str = "entry-per-market-side",
    ) -> list[sqlite3.Row]:
        return sampled_decision_rows(
            self,
            since=since,
            until=until,
            approved_only=approved_only,
            min_quality=min_quality,
            pre_resolution_only=pre_resolution_only,
            sample_mode=sample_mode,
        )

    def signal_backtest_summary(
        self,
        settlements: dict[object, float],
        *,
        since: str | None = None,
        until: str | None = None,
        approved_only: bool = False,
        min_quality: float | None = None,
        pre_resolution_only: bool = True,
        sample_mode: str = "latest-per-market-side",
        sampled_rows: list[sqlite3.Row] | None = None,
    ) -> dict[str, object]:
        return signal_backtest_summary(
            self,
            settlements,
            since=since,
            until=until,
            approved_only=approved_only,
            min_quality=min_quality,
            pre_resolution_only=pre_resolution_only,
            sample_mode=sample_mode,
            sampled_rows=sampled_rows,
        )

    def _open_order(self, order_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                """
                SELECT *
                FROM paper_orders
                WHERE id = ?
                    AND status IN (
                        'PAPER_FILLED', 'PAPER_PARTIALLY_FILLED', 'PAPER_PARTIAL_EXPIRED'
                    )
                    AND settled_at IS NULL
                    AND closed_at IS NULL
                """,
                (order_id,),
            ).fetchone()

    def paper_order(self, order_id: int) -> sqlite3.Row | None:
        """Public read of one stored paper order, as booked."""

        return self._order(order_id)

    def _order(self, order_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM paper_orders WHERE id = ?", (order_id,)).fetchone()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _paper_profile_filter(risk_profile: str | None) -> tuple[str, tuple[str, ...]]:
    if risk_profile is None:
        return "", ()
    return (
        "AND COALESCE(risk_profile, 'live') = ?",
        (normalize_risk_profile_name(risk_profile),),
    )
