"""Outcome ledger for every bin the ladder offered, not just the traded ones.

Why this module exists
----------------------
``market_day_settlements`` records the outcome of every market-day the book
*traded*.  That is roughly nineteen rows.  Every calibration statement in this
project is therefore conditioned on the trade filter: we can only ever measure
the decisions the gates already liked, which is exactly the population that
cannot tell us whether the gates are right.

The labels are free.  ``weather.db.cli_settlements`` holds ~17,900 final NWS CLI
maxima across all fifteen stations back to 2015, and ``decision_snapshots``
already stores ``strike_type`` / ``floor_strike`` / ``cap_strike`` plus the
model and market probabilities for **every offered bin**, traded or not.  The
outcome of a bin is a pure function of the CLI integer and the bin edges.  This
module joins those two and writes one row per offered bin per market-day into
``ladder_bin_outcomes``.

It is measurement infrastructure.  Nothing in the trading path reads this table
and nothing here changes a decision, a size, a gate, or a fee, so it does not
touch the live evidence clock.

The representative quote
------------------------
A single bin is re-evaluated every scan tick -- 200+ ``decision_snapshots`` rows
for one bin on one day -- and those rows are not interchangeable: a quote taken
an hour before settlement is nearly resolved and scores far better than the same
bin priced the night before.  Averaging over them silently weights each bin by
how long it stayed interesting.  So the ledger stores exactly one quote per bin
and says which one it took:

``day_ahead``
    The **last** snapshot strictly before the target settlement day opens on the
    station's own fixed-standard clock (:mod:`settlement_day`).  This is the
    day-ahead closing view: the most informed forecast that still predates any
    of the day's observations, and the only quote that is comparable across
    cities in different time zones.

``same_day``
    The **first** snapshot of a bin that was never offered day-ahead, so the
    ladder stays complete.  Same-day quotes are strictly easier -- they see part
    of the day's heating -- and must not be pooled with day-ahead rows, which is
    why the class is stored rather than inferred.

The integrity guard
-------------------
CLI parse errors would contaminate every label, so the ledger cross-checks its
CLI value against the exchange's own settlement number
(``dataset_kalshi_markets.expiration_value``, the °F the market settled on) and
flags any station-day where the two disagree by
:data:`LADDER_INTEGRITY_TOLERANCE_F` or more instead of accepting it silently.
Measured over the 1,348 exchange-settled city-days that carry both numbers
(target dates 2026-03-12 .. 2026-07-07), the two agreed **exactly** on every
one; the guard exists for the failure that has not happened yet.  Days with no
exchange record are recorded as ``unchecked`` -- never as agreement.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from .cities import CITIES, CityConfig, city_for_market_ticker
from .settlement_truth import (
    SettlementKey,
    integer_settlement_high_f,
    row_resolves_yes,
)
from .store.market_day_settlements import TRADED_STATUSES

# A CLI maximum this far from the exchange's own settlement value is a data
# defect, not a rounding difference. Any change to this number must also bump
# _LADDER_INTEGRITY_MIGRATION_KEY in store/schema.py, because integrity_status
# is a cached projection of this constant.
LADDER_INTEGRITY_TOLERANCE_F = 2.0

INTEGRITY_OK = "ok"
INTEGRITY_UNCHECKED = "unchecked"
INTEGRITY_FLAGGED = "flagged"

QUOTE_LEAD_DAY_AHEAD = "day_ahead"
QUOTE_LEAD_SAME_DAY = "same_day"

# The ledger's labels come from one instrument only. Unlike
# market_day_settlements there is no authority ladder here: an untraded bin has
# no booked settlement to defer to, so the CLI archive is both the only source
# and the thing being guarded.
TRUTH_SOURCE_CLI_SETTLEMENT = "cli_settlement"

LADDER_BIN_OUTCOME_SCHEMA = """
CREATE TABLE IF NOT EXISTS ladder_bin_outcomes (
    market_ticker TEXT NOT NULL,
    target_date TEXT NOT NULL,
    side TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    station_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    strike_type TEXT,
    floor_strike REAL,
    cap_strike REAL,
    quote_lead TEXT NOT NULL,
    quote_created_at TEXT NOT NULL,
    quote_snapshot_id INTEGER,
    quote_risk_profile TEXT,
    snapshot_count INTEGER NOT NULL DEFAULT 0,
    model_probability REAL,
    market_probability REAL,
    entry_bid REAL,
    entry_ask REAL,
    settlement_high_f REAL NOT NULL,
    truth_source TEXT NOT NULL,
    resolved_yes INTEGER NOT NULL,
    side_won INTEGER NOT NULL,
    traded INTEGER NOT NULL DEFAULT 0,
    exchange_settlement_high_f REAL,
    truth_delta_f REAL,
    integrity_status TEXT NOT NULL,
    PRIMARY KEY (market_ticker, target_date, side)
);
"""

LADDER_BIN_OUTCOME_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_ladder_bin_outcomes_target
ON ladder_bin_outcomes (target_date, series_ticker);
CREATE INDEX IF NOT EXISTS idx_ladder_bin_outcomes_integrity
ON ladder_bin_outcomes (integrity_status, target_date);
"""

# recorded_at is the first sighting and never moves; everything derived from a
# later pass refreshes. Re-running the same day is therefore a no-op on the
# provenance and an update everywhere else.
_UPSERT_SQL = """
INSERT INTO ladder_bin_outcomes (
    market_ticker, target_date, side, series_ticker, station_id,
    recorded_at, updated_at,
    strike_type, floor_strike, cap_strike,
    quote_lead, quote_created_at, quote_snapshot_id, quote_risk_profile,
    snapshot_count, model_probability, market_probability, entry_bid, entry_ask,
    settlement_high_f, truth_source, resolved_yes, side_won, traded,
    exchange_settlement_high_f, truth_delta_f, integrity_status
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(market_ticker, target_date, side) DO UPDATE SET
    updated_at = excluded.updated_at,
    series_ticker = excluded.series_ticker,
    station_id = excluded.station_id,
    strike_type = excluded.strike_type,
    floor_strike = excluded.floor_strike,
    cap_strike = excluded.cap_strike,
    quote_lead = excluded.quote_lead,
    quote_created_at = excluded.quote_created_at,
    quote_snapshot_id = excluded.quote_snapshot_id,
    quote_risk_profile = excluded.quote_risk_profile,
    snapshot_count = excluded.snapshot_count,
    model_probability = excluded.model_probability,
    market_probability = excluded.market_probability,
    entry_bid = excluded.entry_bid,
    entry_ask = excluded.entry_ask,
    settlement_high_f = excluded.settlement_high_f,
    truth_source = excluded.truth_source,
    resolved_yes = excluded.resolved_yes,
    side_won = excluded.side_won,
    traded = excluded.traded,
    exchange_settlement_high_f = excluded.exchange_settlement_high_f,
    truth_delta_f = excluded.truth_delta_f,
    integrity_status = excluded.integrity_status
"""

_TRADED_PLACEHOLDERS = ", ".join("?" for _ in TRADED_STATUSES)


@dataclass(frozen=True)
class LadderQuote:
    """One bin's representative pre-outcome view, before any label is attached."""

    market_ticker: str
    target_date: str
    side: str
    series_ticker: str
    station_id: str
    strike_type: str | None
    floor_strike: float | None
    cap_strike: float | None
    quote_lead: str
    quote_created_at: str
    quote_snapshot_id: int | None
    quote_risk_profile: str | None
    snapshot_count: int
    model_probability: float | None
    market_probability: float | None
    entry_bid: float | None
    entry_ask: float | None


@dataclass(frozen=True)
class LadderBinOutcome:
    """A resolved ladder bin: the quote plus its label and integrity verdict."""

    quote: LadderQuote
    settlement_high_f: float
    truth_source: str
    resolved_yes: bool
    side_won: bool
    traded: bool
    exchange_settlement_high_f: float | None
    truth_delta_f: float | None
    integrity_status: str


def side_wins(side: str, resolved_yes: bool) -> bool:
    """Whether the recorded side wins given the bin's own YES/NO outcome.

    Kept separate from ``resolved_yes`` on purpose: conflating the market's
    outcome with the position's is the exact defect
    ``schema._migrate_closed_row_position_won`` had to repair on
    ``paper_orders``.
    """

    return resolved_yes if str(side or "YES").strip().upper() == "YES" else not resolved_yes


def assess_integrity(
    settlement_high_f: float,
    exchange_settlement_high_f: float | None,
    *,
    tolerance_f: float = LADDER_INTEGRITY_TOLERANCE_F,
) -> tuple[str, float | None]:
    """Compare the CLI label against the exchange's own settlement value.

    Returns ``(status, delta)`` where ``delta`` is ``CLI - exchange`` in °F.
    With no exchange record the status is ``unchecked``: absence of a
    contradiction is not evidence of agreement, and recording it as ``ok``
    would make the flagged count meaningless.
    """

    if exchange_settlement_high_f is None:
        return INTEGRITY_UNCHECKED, None
    delta = float(settlement_high_f) - float(exchange_settlement_high_f)
    if not math.isfinite(delta):
        return INTEGRITY_UNCHECKED, None
    if abs(delta) >= float(tolerance_f):
        return INTEGRITY_FLAGGED, delta
    return INTEGRITY_OK, delta


def derive_integrity_status(
    settlement_high_f: object,
    exchange_settlement_high_f: object,
    *,
    tolerance_f: float = LADDER_INTEGRITY_TOLERANCE_F,
) -> str:
    """Row-shaped wrapper used by the schema migration to re-derive a status."""

    if exchange_settlement_high_f is None or settlement_high_f is None:
        return INTEGRITY_UNCHECKED
    status, _ = assess_integrity(
        float(settlement_high_f),
        float(exchange_settlement_high_f),
        tolerance_f=tolerance_f,
    )
    return status


def day_ahead_cutoff_utc(city: CityConfig, target_date: date) -> str:
    """The instant the target settlement day opens at the station, in UTC ISO.

    The NWS climate day runs midnight-to-midnight *standard* time year round
    (see :mod:`settlement_day`), so this is the boundary a quote must predate to
    be genuinely day-ahead.  Comparing the returned string against
    ``decision_snapshots.created_at`` is a plain lexicographic comparison: both
    are ISO-8601 UTC with the same offset spelling.
    """

    opens = datetime.combine(target_date, time(0, 0), tzinfo=city.fixed_standard_timezone())
    return opens.astimezone(timezone.utc).isoformat()


_QUOTE_COLUMNS = (
    "market_ticker, side, id, created_at, strike_type, floor_strike, cap_strike, "
    "model_probability, market_probability, entry_bid, entry_ask, risk_profile"
)

# Bare columns beside an aggregate resolve to the row that produced the
# min/max -- a documented SQLite guarantee, and the reason this needs no window
# function or correlated subquery. Served by idx_decision_snapshots_market
# (target_date, market_ticker, created_at).
_LAST_DAY_AHEAD_SQL = f"""
SELECT {_QUOTE_COLUMNS}, MAX(created_at) AS chosen_at, COUNT(*) AS snapshot_count
FROM decision_snapshots
WHERE target_date = ?
  AND market_ticker LIKE ?
  AND created_at >= ?
  AND created_at < ?
GROUP BY market_ticker, side
"""

_FIRST_SAME_DAY_SQL = f"""
SELECT {_QUOTE_COLUMNS}, MIN(created_at) AS chosen_at, COUNT(*) AS snapshot_count
FROM decision_snapshots
WHERE target_date = ?
  AND market_ticker LIKE ?
  AND created_at >= ?
  AND created_at < ?
GROUP BY market_ticker, side
"""

# How far back a day-ahead scan can have started. The book opens a target one
# day ahead, so three days is generous headroom that still keeps the scan
# bounded on a multi-gigabyte decision table.
_QUOTE_LOOKBACK_DAYS = 3
# How far past the settlement day a snapshot can still be attributed to it.
_QUOTE_LOOKAHEAD_DAYS = 2


def _float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _quote_from_row(
    row: sqlite3.Row, *, city: CityConfig, target_date: str, quote_lead: str
) -> LadderQuote:
    return LadderQuote(
        market_ticker=str(row["market_ticker"]),
        target_date=target_date,
        side=str(row["side"] or "YES").strip().upper(),
        series_ticker=city.series_ticker,
        station_id=city.nws_station_id,
        strike_type=None if row["strike_type"] is None else str(row["strike_type"]),
        floor_strike=_float_or_none(row["floor_strike"]),
        cap_strike=_float_or_none(row["cap_strike"]),
        quote_lead=quote_lead,
        quote_created_at=str(row["chosen_at"]),
        quote_snapshot_id=None if row["id"] is None else int(row["id"]),
        quote_risk_profile=None if row["risk_profile"] is None else str(row["risk_profile"]),
        snapshot_count=int(row["snapshot_count"] or 0),
        model_probability=_float_or_none(row["model_probability"]),
        market_probability=_float_or_none(row["market_probability"]),
        entry_bid=_float_or_none(row["entry_bid"]),
        entry_ask=_float_or_none(row["entry_ask"]),
    )


def offered_ladder_quotes(
    conn: sqlite3.Connection,
    *,
    target_date: str,
    cities: Sequence[CityConfig] = CITIES,
) -> list[LadderQuote]:
    """One representative quote per offered bin on ``target_date``.

    Two bounded passes per city: the day-ahead close, then the same-day opener
    for bins the day-ahead pass never saw.  Both are keyed on
    ``target_date`` + a ticker prefix + a ``created_at`` range, so neither can
    degrade into a scan of the whole decision table.
    """

    day = date.fromisoformat(str(target_date))
    lower = (day - timedelta(days=_QUOTE_LOOKBACK_DAYS)).isoformat()
    upper = (day + timedelta(days=_QUOTE_LOOKAHEAD_DAYS)).isoformat()
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    quotes: dict[tuple[str, str], LadderQuote] = {}
    try:
        for city in cities:
            prefix = f"{city.series_ticker}-%"
            cutoff = day_ahead_cutoff_utc(city, day)
            day_ahead = conn.execute(
                _LAST_DAY_AHEAD_SQL, (str(target_date), prefix, lower, cutoff)
            ).fetchall()
            for row in day_ahead:
                quote = _quote_from_row(
                    row, city=city, target_date=str(target_date),
                    quote_lead=QUOTE_LEAD_DAY_AHEAD,
                )
                quotes[(quote.market_ticker, quote.side)] = quote
            same_day = conn.execute(
                _FIRST_SAME_DAY_SQL, (str(target_date), prefix, cutoff, upper)
            ).fetchall()
            for row in same_day:
                quote = _quote_from_row(
                    row, city=city, target_date=str(target_date),
                    quote_lead=QUOTE_LEAD_SAME_DAY,
                )
                quotes.setdefault((quote.market_ticker, quote.side), quote)
    finally:
        conn.row_factory = previous_factory
    return [quotes[key] for key in sorted(quotes)]


def traded_bins(conn: sqlite3.Connection, *, target_date: str) -> set[tuple[str, str]]:
    """``(market_ticker, side)`` the book actually held on ``target_date``.

    Reuses ``market_day_settlements.TRADED_STATUSES`` so "traded" means the same
    thing in both ledgers: a quote that rested and expired took no market risk.
    """

    rows = conn.execute(
        "SELECT DISTINCT market_ticker, side FROM paper_orders "
        f"WHERE target_date = ? AND status IN ({_TRADED_PLACEHOLDERS})",
        (str(target_date), *TRADED_STATUSES),
    ).fetchall()
    return {(str(ticker), str(side or "YES").strip().upper()) for ticker, side in rows}


def exchange_settlement_highs(conn: sqlite3.Connection) -> dict[SettlementKey, float]:
    """The exchange's own settlement °F per ``(series_ticker, target_date)``.

    ``dataset_kalshi_markets.expiration_value`` is the number Kalshi settled the
    ladder on, and it is constant across a city-day (verified: 0 city-days out
    of 1,350 carry more than one distinct value).  This is the only source in
    the journal that is independent of the NWS CLI text we parse, which is what
    makes it usable as the integrity guard's second opinion.
    """

    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'dataset_kalshi_markets'"
    ).fetchone()
    if present is None:
        return {}
    rows = conn.execute(
        "SELECT ticker, target_date, expiration_value FROM dataset_kalshi_markets "
        "WHERE market_status = 'finalized' AND expiration_value IS NOT NULL"
    ).fetchall()
    highs: dict[SettlementKey, float] = {}
    for ticker, target_date, value in rows:
        city = city_for_market_ticker(str(ticker))
        if city is None:
            continue
        number = _float_or_none(value)
        if number is None:
            continue
        highs.setdefault((city.series_ticker, str(target_date)), number)
    return highs


def resolve_ladder_quotes(
    quotes: Iterable[LadderQuote],
    *,
    settlement_highs: Mapping[SettlementKey, float],
    exchange_highs: Mapping[SettlementKey, float] | None = None,
    traded: set[tuple[str, str]] | None = None,
    tolerance_f: float = LADDER_INTEGRITY_TOLERANCE_F,
) -> tuple[list[LadderBinOutcome], list[LadderQuote]]:
    """Label every quote that has final CLI truth; return the rest unlabelled.

    Bins whose station-day has no final CLI maximum are returned separately
    rather than guessed, mirroring ``backfill_market_day_settlements``'
    "unrecoverable" contract.
    """

    exchange = exchange_highs or {}
    held = traded or set()
    resolved: list[LadderBinOutcome] = []
    unlabelled: list[LadderQuote] = []
    for quote in quotes:
        key = (quote.series_ticker, quote.target_date)
        raw_high = settlement_highs.get(key)
        if raw_high is None:
            unlabelled.append(quote)
            continue
        high = integer_settlement_high_f(raw_high)
        yes = row_resolves_yes(
            {
                "strike_type": quote.strike_type,
                "floor_strike": quote.floor_strike,
                "cap_strike": quote.cap_strike,
                "label": "",
            },
            high,
        )
        exchange_high = exchange.get(key)
        status, delta = assess_integrity(high, exchange_high, tolerance_f=tolerance_f)
        resolved.append(
            LadderBinOutcome(
                quote=quote,
                settlement_high_f=high,
                truth_source=TRUTH_SOURCE_CLI_SETTLEMENT,
                resolved_yes=yes,
                side_won=side_wins(quote.side, yes),
                traded=(quote.market_ticker, quote.side) in held,
                exchange_settlement_high_f=exchange_high,
                truth_delta_f=delta,
                integrity_status=status,
            )
        )
    return resolved, unlabelled


def record_ladder_outcomes(
    conn: sqlite3.Connection,
    outcomes: Sequence[LadderBinOutcome],
    *,
    recorded_at: str,
) -> int:
    """Upsert resolved bins. Idempotent: the same truth rewrites the same row."""

    written = 0
    for outcome in outcomes:
        quote = outcome.quote
        conn.execute(
            _UPSERT_SQL,
            (
                quote.market_ticker,
                quote.target_date,
                quote.side,
                quote.series_ticker,
                quote.station_id,
                recorded_at,
                recorded_at,
                quote.strike_type,
                quote.floor_strike,
                quote.cap_strike,
                quote.quote_lead,
                quote.quote_created_at,
                quote.quote_snapshot_id,
                quote.quote_risk_profile,
                quote.snapshot_count,
                quote.model_probability,
                quote.market_probability,
                quote.entry_bid,
                quote.entry_ask,
                outcome.settlement_high_f,
                outcome.truth_source,
                1 if outcome.resolved_yes else 0,
                1 if outcome.side_won else 0,
                1 if outcome.traded else 0,
                outcome.exchange_settlement_high_f,
                outcome.truth_delta_f,
                outcome.integrity_status,
            ),
        )
        written += 1
    return written


def build_ladder_outcomes_for_date(
    conn: sqlite3.Connection,
    *,
    target_date: str,
    settlement_highs: Mapping[SettlementKey, float],
    exchange_highs: Mapping[SettlementKey, float] | None = None,
    cities: Sequence[CityConfig] = CITIES,
    tolerance_f: float = LADDER_INTEGRITY_TOLERANCE_F,
) -> tuple[list[LadderBinOutcome], list[LadderQuote]]:
    """Assemble one target date's resolved ladder from the journal."""

    quotes = offered_ladder_quotes(conn, target_date=target_date, cities=cities)
    return resolve_ladder_quotes(
        quotes,
        settlement_highs=settlement_highs,
        exchange_highs=exchange_highs,
        traded=traded_bins(conn, target_date=target_date),
        tolerance_f=tolerance_f,
    )


def target_dates_in_range(start: str, end: str) -> list[str]:
    first = date.fromisoformat(str(start))
    last = date.fromisoformat(str(end))
    if last < first:
        raise ValueError(f"end date {end!r} precedes start date {start!r}")
    span = (last - first).days
    return [(first + timedelta(days=offset)).isoformat() for offset in range(span + 1)]


def previous_complete_settlement_day(
    now: datetime | None = None, city: CityConfig | None = None
) -> str:
    """The most recent settlement day that has certainly closed everywhere.

    The nightly pass runs against the slowest station, not the local one: a day
    is only complete for the ledger once it has closed at every city on the
    ladder, and the westernmost station (fixed UTC-8) closes last.
    """

    from .settlement_day import settlement_today

    slowest = city or min(CITIES, key=lambda item: item.standard_utc_offset_hours)
    return (settlement_today(now, slowest) - timedelta(days=1)).isoformat()


# ---------------------------------------------------------------------------
# Scoring -- the reason the ledger exists
# ---------------------------------------------------------------------------


def _brier(rows: Sequence[tuple[float, float]]) -> float | None:
    if not rows:
        return None
    return sum((p - outcome) ** 2 for p, outcome in rows) / len(rows)


def _log_loss(rows: Sequence[tuple[float, float]]) -> float | None:
    if not rows:
        return None
    eps = 1e-9
    total = 0.0
    for p, outcome in rows:
        clipped = min(max(p, eps), 1.0 - eps)
        total -= outcome * math.log(clipped) + (1.0 - outcome) * math.log(1.0 - clipped)
    return total / len(rows)


def score_ladder_outcomes(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Brier/log-loss for the model, the market, and their even blend.

    Scored on the *recorded side's* outcome, so a NO book's probabilities are
    compared against whether NO won -- the same convention the audit's
    model-versus-market comparison used.
    """

    model: list[tuple[float, float]] = []
    market: list[tuple[float, float]] = []
    blend: list[tuple[float, float]] = []
    scored = 0
    day_markets: set[tuple[str, str]] = set()
    cities: set[str] = set()
    dates: set[str] = set()
    flagged = 0
    traded = 0
    for row in rows:
        status = str(row["integrity_status"])
        if status == INTEGRITY_FLAGGED:
            flagged += 1
            continue
        outcome = 1.0 if int(row["side_won"]) else 0.0
        model_p = _float_or_none(row["model_probability"])
        market_p = _float_or_none(row["market_probability"])
        if model_p is None or market_p is None:
            continue
        scored += 1
        day_markets.add((str(row["market_ticker"]), str(row["target_date"])))
        cities.add(str(row["series_ticker"]))
        dates.add(str(row["target_date"]))
        traded += 1 if int(row["traded"] or 0) else 0
        model.append((model_p, outcome))
        market.append((market_p, outcome))
        blend.append((0.5 * (model_p + market_p), outcome))
    return {
        "scored_bins": scored,
        "day_markets": len(day_markets),
        "cities": len(cities),
        "target_dates": len(dates),
        "traded_bins": traded,
        "flagged_bins_excluded": flagged,
        "brier_model": _brier(model),
        "brier_market": _brier(market),
        "brier_blend": _brier(blend),
        "log_loss_model": _log_loss(model),
        "log_loss_market": _log_loss(market),
        "realized_frequency": (
            sum(outcome for _, outcome in market) / len(market) if market else None
        ),
    }
