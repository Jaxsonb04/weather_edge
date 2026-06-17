from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .config import normalize_risk_profile_name
from .fees import (
    contracts_for_budget,
    quadratic_fee_average_per_contract,
    quadratic_fee_per_contract,
)
from .models import BucketProbability, EventSnapshot, ForecastSnapshot, IntradaySnapshot, TradeDecision


SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    target_date TEXT NOT NULL,
    predicted_high_f REAL NOT NULL,
    fetched_at TEXT,
    method TEXT,
    source_spread_f REAL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    target_date TEXT,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS probability_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    target_date TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    label TEXT NOT NULL,
    probability REAL NOT NULL,
    lower_confidence REAL NOT NULL,
    empirical_probability REAL NOT NULL,
    normal_probability REAL NOT NULL,
    effective_n REAL NOT NULL,
    residual_probability REAL,
    ensemble_probability REAL,
	    model_probability REAL,
	    market_probability REAL,
	    observed_high_f REAL,
	    intraday_probability REAL,
	    remaining_heat_risk REAL
	);

CREATE TABLE IF NOT EXISTS decision_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    target_date TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    label TEXT NOT NULL,
    action TEXT NOT NULL,
    side TEXT NOT NULL,
    approved INTEGER NOT NULL,
    probability REAL NOT NULL,
    probability_lcb REAL NOT NULL,
    model_probability REAL,
    market_probability REAL,
    residual_probability REAL,
    ensemble_probability REAL,
    intraday_probability REAL,
    remaining_heat_risk REAL,
    yes_bid REAL NOT NULL,
    yes_ask REAL NOT NULL,
    entry_bid REAL,
    entry_ask REAL,
    entry_bid_size REAL,
    entry_ask_size REAL,
    spread REAL NOT NULL,
    fee_per_contract REAL NOT NULL,
    cost_per_contract REAL NOT NULL,
    edge REAL NOT NULL,
    edge_lcb REAL NOT NULL,
    kelly_fraction REAL NOT NULL,
    recommended_contracts REAL NOT NULL,
    recommended_spend REAL NOT NULL,
    expected_profit REAL NOT NULL,
    trade_quality_score REAL NOT NULL,
    strike_type TEXT,
    floor_strike REAL,
    cap_strike REAL,
    event_ticker TEXT,
    market_status TEXT,
    market_close_time TEXT,
    forecast_fetched_at TEXT,
    forecast_method TEXT,
    forecast_observed_high_mode TEXT,
    intraday_observed_high_f REAL,
    intraday_latest_observed_at TEXT,
    intraday_is_complete INTEGER NOT NULL DEFAULT 0,
    intraday_observed_high_source TEXT,
    reasons_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    target_date TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    label TEXT NOT NULL,
    action TEXT NOT NULL,
    risk_profile TEXT,
    group_id TEXT,
    side TEXT NOT NULL DEFAULT 'YES',
    contracts REAL NOT NULL,
    yes_ask REAL NOT NULL,
    entry_price REAL,
    entry_bid REAL,
    entry_bid_size REAL,
    entry_ask_size REAL,
    strike_type TEXT,
    floor_strike REAL,
    cap_strike REAL,
    entry_mode TEXT NOT NULL DEFAULT 'market',
    limit_price REAL,
    limit_fee_per_contract REAL,
    limit_cost_per_contract REAL,
    limit_edge REAL,
    limit_edge_lcb REAL,
    fee_per_contract REAL NOT NULL,
    cost_per_contract REAL NOT NULL,
    probability REAL NOT NULL,
    probability_lcb REAL NOT NULL,
    edge REAL NOT NULL,
    edge_lcb REAL NOT NULL,
    trade_quality_score REAL NOT NULL DEFAULT 0,
    expected_profit REAL NOT NULL,
    status TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    settled_at TEXT,
    settlement_high_f REAL,
    resolved_yes INTEGER,
    realized_pnl REAL,
    closed_at TEXT,
    exit_price REAL,
    exit_fee_per_contract REAL
);

CREATE TABLE IF NOT EXISTS paper_monitor_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    order_id INTEGER NOT NULL,
    target_date TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT,
    market_status TEXT,
    live_bid REAL,
    exit_fee_per_contract REAL,
    net_exit_per_contract REAL,
    unrealized_pnl REAL,
    unrealized_roi REAL
);
"""

# Created after column migrations in init() so they can reference late-added
# columns (e.g. group_id) on databases that predate them.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_paper_orders_market_side
    ON paper_orders (target_date, market_ticker, side, status);
CREATE INDEX IF NOT EXISTS idx_paper_orders_lifecycle
    ON paper_orders (status, settled_at, closed_at);
CREATE INDEX IF NOT EXISTS idx_paper_orders_group
    ON paper_orders (group_id);
CREATE INDEX IF NOT EXISTS idx_decision_snapshots_market
    ON decision_snapshots (target_date, market_ticker, created_at);
CREATE INDEX IF NOT EXISTS idx_probability_snapshots_market
    ON probability_snapshots (target_date, market_ticker, created_at);
CREATE INDEX IF NOT EXISTS idx_monitor_snapshots_order
    ON paper_monitor_snapshots (order_id, created_at);
"""

PAPER_ORDER_AUDIT_COLUMNS = {
    "settled_at": "TEXT",
    "settlement_high_f": "REAL",
    "resolved_yes": "INTEGER",
    "realized_pnl": "REAL",
    "closed_at": "TEXT",
    "exit_price": "REAL",
    "exit_fee_per_contract": "REAL",
    "trade_quality_score": "REAL NOT NULL DEFAULT 0",
    "side": "TEXT NOT NULL DEFAULT 'YES'",
    "entry_price": "REAL",
    "entry_bid": "REAL",
    "entry_bid_size": "REAL",
    "entry_ask_size": "REAL",
    "strike_type": "TEXT",
    "floor_strike": "REAL",
    "cap_strike": "REAL",
    "entry_mode": "TEXT NOT NULL DEFAULT 'market'",
    "limit_price": "REAL",
    "limit_fee_per_contract": "REAL",
    "limit_cost_per_contract": "REAL",
    "limit_edge": "REAL",
    "limit_edge_lcb": "REAL",
    "risk_profile": "TEXT",
    "group_id": "TEXT",
}

# Fixed-PST settlement clock (UTC-8 year round) used for the daily-loss window so
# the breaker measures loss on the same day math the rest of trading settles on.
SETTLEMENT_TZ = timezone(timedelta(hours=-8))

# Rolling window (days) for the resolved-ROI circuit breaker, so a bad early
# cohort ages out and the pause can clear instead of latching off forever.
PAUSE_LOOKBACK_DAYS = 21

# Per-profile entry circuit breaker: (min_resolved_trades, max_resolved_roi,
# daily_loss_pct of bankroll). fast-feedback keeps its original aggressive trip;
# the trading-intent profiles get a looser breaker so the real-money-candidate
# profile is no longer the only one without downside protection.
PAUSE_THRESHOLDS = {
    "fast-feedback": (5, -0.25, 0.005),
    "exploratory": (8, -0.40, 0.010),
    "balanced": (10, -0.35, 0.010),
    "conservative": (10, -0.35, 0.010),
}

PROBABILITY_AUDIT_COLUMNS = {
    "residual_probability": "REAL",
    "ensemble_probability": "REAL",
    "model_probability": "REAL",
    "market_probability": "REAL",
    "observed_high_f": "REAL",
    "intraday_probability": "REAL",
    "remaining_heat_risk": "REAL",
}

DECISION_AUDIT_COLUMNS = {
    "model_probability": "REAL",
    "market_probability": "REAL",
    "residual_probability": "REAL",
    "ensemble_probability": "REAL",
    "intraday_probability": "REAL",
    "remaining_heat_risk": "REAL",
    "event_ticker": "TEXT",
    "market_status": "TEXT",
    "market_close_time": "TEXT",
    "forecast_fetched_at": "TEXT",
    "forecast_method": "TEXT",
    "forecast_observed_high_mode": "TEXT",
    "intraday_observed_high_f": "REAL",
    "intraday_latest_observed_at": "TEXT",
    "intraday_is_complete": "INTEGER NOT NULL DEFAULT 0",
    "intraday_observed_high_source": "TEXT",
    "forecast_predicted_high_f": "REAL",
    "forecast_source_spread_f": "REAL",
    "forecast_lead_hours": "REAL",
    "risk_profile": "TEXT",
    "bankroll": "REAL",
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
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.DatabaseError:
            # Non-file databases (e.g. :memory:) ignore WAL; never block init.
            pass
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            existing = {
                row[1]
                for row in conn.execute("PRAGMA table_info(paper_orders)").fetchall()
            }
            for column, column_type in PAPER_ORDER_AUDIT_COLUMNS.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE paper_orders ADD COLUMN {column} {column_type}")
            existing_probability = {
                row[1]
                for row in conn.execute("PRAGMA table_info(probability_snapshots)").fetchall()
            }
            for column, column_type in PROBABILITY_AUDIT_COLUMNS.items():
                if column not in existing_probability:
                    conn.execute(f"ALTER TABLE probability_snapshots ADD COLUMN {column} {column_type}")
            existing_decision = {
                row[1]
                for row in conn.execute("PRAGMA table_info(decision_snapshots)").fetchall()
            }
            for column, column_type in DECISION_AUDIT_COLUMNS.items():
                if column not in existing_decision:
                    conn.execute(f"ALTER TABLE decision_snapshots ADD COLUMN {column} {column_type}")
            conn.executescript(INDEXES)

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
                    created_at, target_date, predicted_high_f, fetched_at, method, source_spread_f, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    forecast.target_date.isoformat(),
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
        """

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT created_at, COALESCE(model_probability, probability)
                FROM probability_snapshots
                WHERE target_date = ? AND market_ticker = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (target_date, market_ticker),
            ).fetchone()
        if row is None or row[1] is None:
            return None
        try:
            created = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        except ValueError:
            return None
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age_minutes = (datetime.now(UTC) - created).total_seconds() / 60.0
        if age_minutes > max_age_minutes:
            return None
        return float(row[1])

    def record_decisions(
        self,
        target_date: str,
        decisions: Iterable[TradeDecision],
        *,
        forecast: ForecastSnapshot | None = None,
        intraday: IntradaySnapshot | None = None,
        event: EventSnapshot | None = None,
        risk_profile: str | None = None,
        bankroll: float | None = None,
    ) -> None:
        created_at = _now()
        rows = []
        markets_by_ticker = {}
        if event is not None:
            markets_by_ticker = {market.ticker: market for market in event.markets}
        observed_high_mode = _forecast_observed_high_mode(forecast)
        for decision in decisions:
            spend = decision.recommended_contracts * decision.cost_per_contract
            market = markets_by_ticker.get(decision.ticker)
            rows.append(
                (
                    created_at,
                    target_date,
                    decision.ticker,
                    decision.label,
                    decision.action,
                    decision.side,
                    1 if decision.approved else 0,
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
                    json.dumps(decision.reasons),
                )
            )
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO decision_snapshots (
                    created_at, target_date, market_ticker, label, action, side,
                    approved, probability, probability_lcb, model_probability,
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
                    forecast_lead_hours, risk_profile, bankroll, reasons_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
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
        normalized_status = status or ("PAPER_FILLED" if decision.approved else "REJECTED")
        with self.connect() as conn:
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
                    expected_profit, status, reasons_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now(),
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
                    json.dumps(decision.reasons),
                ),
            )
            return int(cursor.lastrowid)

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
        return self.record_paper_order(target_date, decision)

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

    def paper_spend_for_target(self, target_date: str, *, risk_profile: str | None = None) -> float:
        profile_filter, profile_params = _paper_profile_filter(risk_profile)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT COALESCE(SUM(contracts * cost_per_contract), 0)
                FROM paper_orders
                WHERE target_date = ? AND status != 'REJECTED'
                {profile_filter}
                """,
                (target_date, *profile_params),
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
                  AND status != 'REJECTED'
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
            "status = 'PAPER_FILLED'",
            "settled_at IS NULL",
            "closed_at IS NULL",
        ]
        params: list[object] = [target_date, market_ticker]
        if side is not None:
            filters.append("UPPER(side) = ?")
            params.append(side.upper())
        if risk_profile is not None:
            filters.append("COALESCE(risk_profile, 'balanced') = ?")
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
            "status IN ('PAPER_FILLED', 'PAPER_LIMIT_RESTING')",
            "settled_at IS NULL",
            "closed_at IS NULL",
        ]
        params: list[object] = [target_date, market_ticker]
        if risk_profile is not None:
            filters.append("COALESCE(risk_profile, 'balanced') = ?")
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
            "status = 'PAPER_LIMIT_RESTING'",
            "settled_at IS NULL",
            "closed_at IS NULL",
        ]
        params: list[object] = [target_date, market_ticker, side.upper()]
        if risk_profile is not None:
            filters.append("COALESCE(risk_profile, 'balanced') = ?")
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

    def fill_resting_limit_order(self, order_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                UPDATE paper_orders
                SET status = 'PAPER_FILLED'
                WHERE id = ? AND status = 'PAPER_LIMIT_RESTING'
                """,
                (order_id,),
            )
        return self._order(order_id)

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
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO paper_monitor_snapshots (
                    created_at, order_id, target_date, market_ticker, side,
                    action, reason, market_status, live_bid,
                    exit_fee_per_contract, net_exit_per_contract,
                    unrealized_pnl, unrealized_roi
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now(),
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

    def settle_paper_orders(self, target_date: str, settlement_high_f: float) -> int:
        rows = self._unsettled_orders(target_date)
        settled_at = _now()
        updates = []
        for row in rows:
            resolved_yes = _row_resolves_yes(row, settlement_high_f)
            side = _row_side(row)
            position_wins = resolved_yes if side == "YES" else not resolved_yes
            cost = float(row["cost_per_contract"])
            contracts = float(row["contracts"])
            realized_pnl = contracts * ((1.0 - cost) if position_wins else -cost)
            updates.append(
                (
                    settled_at,
                    settlement_high_f,
                    1 if resolved_yes else 0,
                    realized_pnl,
                    row["id"],
                )
            )
        settled = 0
        with self.connect() as conn:
            # Resting limit orders that never crossed before this target
            # resolved can never fill now. Expire them (zero PnL) so they stop
            # consuming the per-target exposure cap, blocking re-entry, and
            # showing as perpetual pending exposure on the dashboard.
            conn.execute(
                """
                UPDATE paper_orders
                SET status = 'PAPER_EXPIRED',
                    settled_at = ?,
                    settlement_high_f = ?,
                    realized_pnl = 0.0
                WHERE target_date = ?
                  AND status = 'PAPER_LIMIT_RESTING'
                  AND settled_at IS NULL
                  AND closed_at IS NULL
                """,
                (settled_at, settlement_high_f, target_date),
            )
            for params in updates:
                # Guard against the settle/monitor race: the monitor (every 2
                # minutes) can close a position between this read and write. The
                # status/closed_at guard makes settlement a no-op on a row the
                # monitor already closed, instead of silently overwriting its
                # realized_pnl. Count real row changes, not the read size.
                cursor = conn.execute(
                    """
                    UPDATE paper_orders
                    SET
                        settled_at = ?,
                        settlement_high_f = ?,
                        resolved_yes = ?,
                        realized_pnl = ?,
                        status = 'PAPER_SETTLED'
                    WHERE id = ?
                      AND status = 'PAPER_FILLED'
                      AND settled_at IS NULL
                      AND closed_at IS NULL
                    """,
                    params,
                )
                settled += cursor.rowcount
        return settled

    def close_paper_order(self, order_id: int, exit_price: float) -> sqlite3.Row:
        if exit_price <= 0 or exit_price >= 1:
            raise ValueError("exit price must be between 0.01 and 0.99")
        row = self._open_order(order_id)
        if row is None:
            raise ValueError(f"no open paper order found with id {order_id}")
        contracts = float(row["contracts"])
        entry_cost = float(row["cost_per_contract"])
        exit_fee = quadratic_fee_average_per_contract(exit_price, contracts)
        realized_pnl = contracts * (exit_price - exit_fee - entry_cost)
        closed_at = _now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE paper_orders
                SET
                    status = 'PAPER_CLOSED',
                    closed_at = ?,
                    exit_price = ?,
                    exit_fee_per_contract = ?,
                    realized_pnl = ?
                WHERE id = ?
                """,
                (closed_at, exit_price, exit_fee, realized_pnl, order_id),
            )
        closed = self._order(order_id)
        if closed is None:
            raise RuntimeError(f"paper order {order_id} disappeared after close")
        return closed

    def open_paper_order(self, order_id: int) -> sqlite3.Row | None:
        return self._open_order(order_id)

    def open_paper_orders(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                """
                SELECT *
                FROM paper_orders
                WHERE status = 'PAPER_FILLED'
                  AND settled_at IS NULL
                  AND closed_at IS NULL
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def open_paper_target_dates(self) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT target_date
                FROM paper_orders
                WHERE status = 'PAPER_FILLED'
                  AND settled_at IS NULL
                  AND closed_at IS NULL
                ORDER BY target_date
                """
            ).fetchall()
        return [str(row[0]) for row in rows]

    def market_backtest_summary(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, float]:
        filters, params = _date_filters(since, until)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM paper_orders {where}",
                params,
            ).fetchall()
        # PAPER_EXPIRED rows are resting limits that never filled; they carry
        # realized_pnl=0.0 but deployed no capital and resolved no position, so
        # they must not count as orders, dilute the capital/ROI denominator, or
        # drag the hit-rate denominator as phantom losses.
        realized_rows = [
            row
            for row in rows
            if row["realized_pnl"] is not None and row["status"] != "PAPER_EXPIRED"
        ]
        open_rows = [
            row
            for row in rows
            if row["status"] == "PAPER_FILLED" and row["realized_pnl"] is None
        ]
        open_capital = sum(float(row["contracts"]) * float(row["cost_per_contract"]) for row in open_rows)
        if not realized_rows:
            return {
                "orders": 0,
                "contracts": 0.0,
                "capital_at_risk": 0.0,
                "realized_pnl": 0.0,
                "roi": 0.0,
                "hit_rate": 0.0,
                "avg_edge": 0.0,
                "open_orders": float(len(open_rows)),
                "open_capital_at_risk": open_capital,
            }
        contracts = sum(float(row["contracts"]) for row in realized_rows)
        capital = sum(float(row["contracts"]) * float(row["cost_per_contract"]) for row in realized_rows)
        pnl = sum(float(row["realized_pnl"]) for row in realized_rows)
        hits = sum(1 for row in realized_rows if _row_position_won(row))
        return {
            "orders": float(len(realized_rows)),
            "contracts": contracts,
            "capital_at_risk": capital,
            "realized_pnl": pnl,
            "roi": pnl / capital if capital else 0.0,
            # realized_rows is non-empty here (guarded above); a 0-for-N losing
            # streak must report 0.0, not be masked by an `if hits` short-circuit.
            "hit_rate": hits / len(realized_rows),
            "avg_edge": sum(float(row["edge"]) for row in realized_rows) / len(realized_rows),
            "open_orders": float(len(open_rows)),
            "open_capital_at_risk": open_capital,
        }

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
                    COUNT(*) AS trades,
                    COALESCE(SUM(realized_pnl), 0) AS pnl,
                    COALESCE(SUM(contracts * cost_per_contract), 0) AS capital
                FROM paper_orders
                WHERE realized_pnl IS NOT NULL
                  AND status != 'REJECTED'
                  AND status != 'PAPER_EXPIRED'
                  AND COALESCE(risk_profile, 'balanced') = ?
                  AND COALESCE(closed_at, settled_at) >= ?
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
                  AND COALESCE(risk_profile, 'balanced') = ?
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
    ) -> dict[str, object]:
        if sample_mode not in {"latest-per-market-side", "entry-per-market-side", "all"}:
            raise ValueError(
                "sample_mode must be latest-per-market-side, entry-per-market-side, or all"
            )
        normalized_settlements = {str(key): float(value) for key, value in settlements.items()}
        filters, params = _date_filters(since, until)
        if approved_only:
            filters.append("approved = 1")
        if min_quality is not None:
            filters.append("trade_quality_score >= ?")
            params.append(str(float(min_quality)))
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM decision_snapshots {where} ORDER BY target_date, created_at",
                params,
            ).fetchall()

        pre_resolution_rows = [
            row for row in rows if not pre_resolution_only or _is_pre_resolution_decision(row)
        ]
        sampled_rows = _sample_decision_rows(pre_resolution_rows, sample_mode)
        settled_rows = [
            row for row in sampled_rows if str(row["target_date"]) in normalized_settlements
        ]
        if not settled_rows:
            return {
                "signals": float(len(sampled_rows)),
                "raw_signals": float(len(rows)),
                "pre_resolution_signals": float(len(pre_resolution_rows)),
                "settled_signals": 0.0,
                "approved_signals": float(sum(1 for row in sampled_rows if int(row["approved"]))),
                "approved_raw_signals": float(sum(1 for row in rows if int(row["approved"]))),
                "approved_pre_resolution_signals": float(
                    sum(1 for row in pre_resolution_rows if int(row["approved"]))
                ),
                "approval_rate": _safe_div(sum(1 for row in sampled_rows if int(row["approved"])), len(sampled_rows)),
                "excluded_post_resolution_signals": float(len(rows) - len(pre_resolution_rows)),
                "sample_mode": sample_mode,
                "pre_resolution_only": pre_resolution_only,
                "brier_score": 0.0,
                "log_loss": 0.0,
                "win_rate": 0.0,
                "avg_probability": 0.0,
                "avg_edge": 0.0,
                "avg_edge_lcb": 0.0,
                "avg_quality": 0.0,
                "approved_paper_pnl": 0.0,
                "approved_capital_at_risk": 0.0,
                "approved_roi": 0.0,
                "approved_hit_rate": 0.0,
                "quality_buckets": [],
                "probability_streams": {},
            }

        outcomes = []
        for row in settled_rows:
            settlement = normalized_settlements[str(row["target_date"])]
            position_won = _decision_row_position_won(row, settlement)
            probability = float(row["probability"])
            outcomes.append((row, 1.0 if position_won else 0.0, probability))

        approved = [(row, outcome, probability) for row, outcome, probability in outcomes if int(row["approved"])]
        capital = sum(float(row["recommended_spend"]) for row, _, _ in approved)
        pnl = sum(_decision_row_pnl(row, bool(outcome)) for row, outcome, _ in approved)
        return {
            "signals": float(len(sampled_rows)),
            "raw_signals": float(len(rows)),
            "pre_resolution_signals": float(len(pre_resolution_rows)),
            "settled_signals": float(len(settled_rows)),
            "approved_signals": float(sum(1 for row in sampled_rows if int(row["approved"]))),
            "approved_raw_signals": float(sum(1 for row in rows if int(row["approved"]))),
            "approved_pre_resolution_signals": float(
                sum(1 for row in pre_resolution_rows if int(row["approved"]))
            ),
            "approval_rate": _safe_div(sum(1 for row in sampled_rows if int(row["approved"])), len(sampled_rows)),
            "excluded_post_resolution_signals": float(len(rows) - len(pre_resolution_rows)),
            "sample_mode": sample_mode,
            "pre_resolution_only": pre_resolution_only,
            "brier_score": sum((probability - outcome) ** 2 for _, outcome, probability in outcomes) / len(outcomes),
            "log_loss": sum(
                -math.log(max(1e-12, probability if outcome else 1.0 - probability))
                for _, outcome, probability in outcomes
            )
            / len(outcomes),
            "win_rate": sum(outcome for _, outcome, _ in outcomes) / len(outcomes),
            "avg_probability": sum(probability for _, _, probability in outcomes) / len(outcomes),
            "avg_edge": sum(float(row["edge"]) for row, _, _ in outcomes) / len(outcomes),
            "avg_edge_lcb": sum(float(row["edge_lcb"]) for row, _, _ in outcomes) / len(outcomes),
            "avg_quality": sum(float(row["trade_quality_score"]) for row, _, _ in outcomes) / len(outcomes),
            "approved_paper_pnl": pnl,
            "approved_capital_at_risk": capital,
            "approved_roi": pnl / capital if capital else 0.0,
            "approved_hit_rate": _safe_div(sum(outcome for _, outcome, _ in approved), len(approved)),
            "quality_buckets": _quality_buckets(outcomes),
            "probability_streams": _probability_stream_metrics(outcomes),
        }

    def _unsettled_orders(self, target_date: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                """
                SELECT *
                FROM paper_orders
                WHERE target_date = ? AND status = 'PAPER_FILLED' AND settled_at IS NULL
                """,
                (target_date,),
            ).fetchall()

    def _open_order(self, order_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                """
                SELECT *
                FROM paper_orders
                WHERE id = ?
                    AND status = 'PAPER_FILLED'
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


def _forecast_observed_high_mode(forecast: ForecastSnapshot | None) -> str | None:
    if forecast is None or not isinstance(forecast.raw, dict):
        return None
    decision = forecast.raw.get("observed_high_decision")
    if not isinstance(decision, dict):
        return None
    mode = decision.get("mode")
    return str(mode).lower() if mode else None


def _market_close_time(raw: dict | None) -> str | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get("close_time") or raw.get("expected_expiration_time") or raw.get("expiration_time")
    return str(value) if value else None


def _date_filters(since: str | None, until: str | None) -> tuple[list[str], list[str]]:
    filters: list[str] = []
    params: list[str] = []
    if since:
        filters.append("target_date >= ?")
        params.append(since)
    if until:
        filters.append("target_date <= ?")
        params.append(until)
    return filters, params


def _sample_decision_rows(rows: list[sqlite3.Row], sample_mode: str) -> list[sqlite3.Row]:
    if sample_mode == "all":
        return list(rows)
    if sample_mode == "entry-per-market-side":
        return _entry_decision_rows(rows)
    latest = {}
    for row in rows:
        key = (str(row["target_date"]), str(row["market_ticker"]), _row_side(row))
        current = latest.get(key)
        if current is None or _row_sort_time(row) >= _row_sort_time(current):
            latest[key] = row
    return sorted(latest.values(), key=lambda row: (str(row["target_date"]), str(row["created_at"]), int(row["id"])))


def _entry_decision_rows(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """First approved snapshot per market/side — the decision that opened the
    position — falling back to the first snapshot when nothing was approved.

    The latest pre-resolution snapshot can look very different from the entry
    the scanner actually traded, so backtests of the entry decision must not
    sample it.
    """

    entries: dict[tuple[str, str, str], sqlite3.Row] = {}
    for row in rows:
        key = (str(row["target_date"]), str(row["market_ticker"]), _row_side(row))
        current = entries.get(key)
        if current is None:
            entries[key] = row
            continue
        row_rank = (0 if int(row["approved"]) else 1, _row_sort_time(row))
        current_rank = (0 if int(current["approved"]) else 1, _row_sort_time(current))
        if row_rank < current_rank:
            entries[key] = row
    return sorted(entries.values(), key=lambda row: (str(row["target_date"]), str(row["created_at"]), int(row["id"])))


def _row_sort_time(row: sqlite3.Row) -> tuple[str, int]:
    return (str(row["created_at"]), int(row["id"]))


def _is_pre_resolution_decision(row: sqlite3.Row) -> bool:
    if _row_value(row, "intraday_is_complete", 0):
        return False

    created_at = _parse_timestamp(_row_value(row, "created_at"))
    close_time = _parse_timestamp(_row_value(row, "market_close_time"))
    if created_at is not None and close_time is not None and created_at >= close_time:
        return False
    return True


def _row_value(row: sqlite3.Row, key: str, default=None):
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _parse_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _label_resolves_yes(label: str, settlement_high_f: float) -> bool:
    if "or below" in label:
        match = re.search(r"(\d+)", label)
        if not match:
            return False
        return settlement_high_f <= float(match.group(1))
    if "or above" in label:
        match = re.search(r"(\d+)", label)
        if not match:
            return False
        return settlement_high_f >= float(match.group(1))
    match = re.search(r"(\d+).+?(\d+)", label)
    if match:
        lo, hi = float(match.group(1)), float(match.group(2))
        return lo <= settlement_high_f <= hi
    return False


def _row_resolves_yes(row: sqlite3.Row, settlement_high_f: float) -> bool:
    try:
        strike_type = row["strike_type"]
        floor_strike = row["floor_strike"]
        cap_strike = row["cap_strike"]
    except (IndexError, KeyError):
        strike_type = floor_strike = cap_strike = None
    if strike_type:
        normalized = str(strike_type).lower()
        floor_value = float(floor_strike) if floor_strike is not None else None
        cap_value = float(cap_strike) if cap_strike is not None else None
        if normalized == "less":
            return cap_value is not None and settlement_high_f < cap_value
        if normalized == "greater":
            return floor_value is not None and settlement_high_f > floor_value
        if normalized == "between":
            return (
                floor_value is not None
                and cap_value is not None
                and floor_value <= settlement_high_f <= cap_value
            )
    return _label_resolves_yes(row["label"], settlement_high_f)


def _row_side(row: sqlite3.Row) -> str:
    try:
        side = row["side"]
    except (IndexError, KeyError):
        side = None
    if side:
        normalized = str(side).upper()
        if normalized in {"YES", "NO"}:
            return normalized
    try:
        action = str(row["action"]).upper()
    except (IndexError, KeyError):
        return "YES"
    return "NO" if "NO" in action else "YES"


def _row_position_won(row: sqlite3.Row) -> bool:
    try:
        resolved_yes = row["resolved_yes"]
    except (IndexError, KeyError):
        resolved_yes = None
    if resolved_yes is None:
        return float(row["realized_pnl"] or 0.0) > 0.0
    side = _row_side(row)
    return bool(resolved_yes) if side == "YES" else not bool(resolved_yes)


def _decision_row_position_won(row: sqlite3.Row, settlement_high_f: float) -> bool:
    resolved_yes = _decision_row_resolves_yes(row, settlement_high_f)
    side = str(row["side"]).upper()
    return resolved_yes if side == "YES" else not resolved_yes


def _decision_row_resolves_yes(row: sqlite3.Row, settlement_high_f: float) -> bool:
    strike_type = row["strike_type"]
    floor_strike = row["floor_strike"]
    cap_strike = row["cap_strike"]
    if strike_type:
        normalized = str(strike_type).lower()
        floor_value = float(floor_strike) if floor_strike is not None else None
        cap_value = float(cap_strike) if cap_strike is not None else None
        if normalized == "less":
            return cap_value is not None and settlement_high_f < cap_value
        if normalized == "greater":
            return floor_value is not None and settlement_high_f > floor_value
        if normalized == "between":
            return (
                floor_value is not None
                and cap_value is not None
                and floor_value <= settlement_high_f <= cap_value
            )
    return _label_resolves_yes(row["label"], settlement_high_f)


def _decision_row_pnl(row: sqlite3.Row, position_won: bool) -> float:
    contracts = float(row["recommended_contracts"])
    cost = float(row["cost_per_contract"])
    return contracts * ((1.0 - cost) if position_won else -cost)


def _probability_stream_metrics(
    rows: list[tuple[sqlite3.Row, float, float]],
) -> dict[str, dict[str, float]]:
    """Score the weather model, market prior, and traded posterior separately.

    The traded probability blends the market prior in, so its calibration can
    look good even when the weather model adds nothing. Comparing the streams
    on the same settled rows shows whether the model is real alpha or just
    market agreement.
    """

    streams = {
        "traded": lambda row: float(row["probability"]),
        "weather_model": lambda row: _row_optional_float(row, "model_probability"),
        "market_prior": lambda row: _row_optional_float(row, "market_probability"),
    }
    output: dict[str, dict[str, float]] = {}
    for name, extract in streams.items():
        scored = [
            (outcome, probability)
            for row, outcome, _ in rows
            if (probability := extract(row)) is not None
        ]
        if not scored:
            continue
        output[name] = {
            "settled": float(len(scored)),
            "brier_score": sum((probability - outcome) ** 2 for outcome, probability in scored)
            / len(scored),
            "log_loss": sum(
                -math.log(max(1e-12, probability if outcome else 1.0 - probability))
                for outcome, probability in scored
            )
            / len(scored),
            "avg_probability": sum(probability for _, probability in scored) / len(scored),
            "win_rate": sum(outcome for outcome, _ in scored) / len(scored),
        }
    return output


def _row_optional_float(row: sqlite3.Row, key: str) -> float | None:
    value = _row_value(row, key)
    return None if value is None else float(value)


def _quality_buckets(rows: list[tuple[sqlite3.Row, float, float]]) -> list[dict[str, float]]:
    buckets = [
        ("0-20", 0.0, 20.0),
        ("20-40", 20.0, 40.0),
        ("40-60", 40.0, 60.0),
        ("60-80", 60.0, 80.0),
        ("80-100", 80.0, 100.000001),
    ]
    output: list[dict[str, float]] = []
    for label, lower, upper in buckets:
        bucket = [
            (row, outcome, probability)
            for row, outcome, probability in rows
            if lower <= float(row["trade_quality_score"]) < upper
        ]
        if not bucket:
            continue
        approved = [item for item in bucket if int(item[0]["approved"])]
        capital = sum(float(row["recommended_spend"]) for row, _, _ in approved)
        pnl = sum(_decision_row_pnl(row, bool(outcome)) for row, outcome, _ in approved)
        output.append(
            {
                "range": label,
                "count": float(len(bucket)),
                "approved": float(len(approved)),
                "avg_probability": sum(probability for _, _, probability in bucket) / len(bucket),
                "win_rate": sum(outcome for _, outcome, _ in bucket) / len(bucket),
                "brier_score": sum((probability - outcome) ** 2 for _, outcome, probability in bucket) / len(bucket),
                "approved_pnl": pnl,
                "approved_roi": pnl / capital if capital else 0.0,
            }
        )
    return output


def _safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _paper_profile_filter(risk_profile: str | None) -> tuple[str, tuple[str, ...]]:
    if risk_profile is None:
        return "", ()
    return (
        "AND COALESCE(risk_profile, 'balanced') = ?",
        (normalize_risk_profile_name(risk_profile),),
    )
