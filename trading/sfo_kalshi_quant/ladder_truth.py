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

Both windows are bounded by the station's own fixed-standard clock: the same-day
window ends when the climate day *closes*, not at a bare ``target_date + 2``
wall-clock date, so a post-resolution quote can never structurally become a
bin's scored view.

A re-run may only improve a quote, never degrade it
---------------------------------------------------
Retention (``prune_decision_snapshots``) keeps everything for ``full_days``,
then keeps only the last snapshot per (market, side, target_date, risk_profile)
plus every approved/signal-approved row, then beyond ``dedup_days`` only the
approved rows.  So a backfill re-run over an older window sees a *thinner and
approval-biased* journal than the first run did -- exactly the trade-filter
conditioning this ledger exists to escape.  Two defences:

* the upsert refuses to replace a ``day_ahead`` quote with a ``same_day`` one,
  or a later day-ahead close with an earlier one (see ``_QUOTE_IS_UPGRADE``),
  so re-running an eroded range cannot downgrade an already-correct row; and
* :func:`assess_ladder_coverage` reports every city-day whose ladder is thinner
  than the widest one in the same run, and every target date already past the
  full-fidelity retention horizon, so "the ledger silently thinned" becomes a
  printed number instead of a shrug.

The integrity guard
-------------------
CLI parse errors would contaminate every label, so the ledger cross-checks its
CLI value against an *independent* record of the same station-day.  There are
two, and they are used in rank order:

``dataset_kalshi_markets.expiration_value`` (rank 1)
    The °F the exchange actually settled the ladder on -- the strongest second
    opinion available, because it is the number that moved real money.  Measured
    over the 1,348 exchange-settled city-days that carry both values (target
    dates 2026-03-12 .. 2026-07-07) the two agreed **exactly** on every one.
    Tolerance :data:`LADDER_INTEGRITY_TOLERANCE_F`.

``nws_daily_high_ground_truth.high_f`` (rank 2)
    The station's own observation tape maximum, from ``weather.db``.  It is a
    coarser instrument than the CLI text -- the observation-derived high runs a
    degree or two low on some days, which is why it must never *settle* an order
    -- so it gets its own wider tolerance
    (:data:`LADDER_OBSERVED_TOLERANCE_F`) and is only consulted on days whose
    tape is dense enough to have seen the peak
    (:data:`LADDER_OBSERVED_MIN_OBSERVATIONS`).

Rank 2 exists because rank 1 does not cover the era being scored.  Finalized
``dataset_kalshi_markets`` rows stop at target date 2026-07-07 while retained
``decision_snapshots`` start at 2026-06-10, so on the 2026-08-18..09-01
validation window the exchange channel checks **0 of 225** station-days and the
guard the audit ordered could not fire at all.  On the same window the
observation channel checks **195 of 225** (the 30 it declines are KDEN and KNYC,
whose tapes carry ~25 observations a day rather than ~310).  Its false-positive
rate is measured, not assumed: over the 863 station-days from 2026-06-01 with a
dense tape, ``|CLI - observed|`` never exceeded 2 °F, so a 3 °F threshold flags
0 of 863.

Days no channel can check are recorded as ``unchecked`` -- never as agreement --
and every count of them is reported alongside the flagged count, because "0
flagged" and "nothing could be checked" must never look the same.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from .cities import CITIES, CityConfig, city_for_market_ticker, city_for_station
from .settlement_truth import (
    SettlementKey,
    integer_settlement_high_f,
    row_resolves_yes,
)
from .store.market_day_settlements import TRADED_STATUSES

logger = logging.getLogger(__name__)

# A CLI maximum this far from the exchange's own settlement value is a data
# defect, not a rounding difference. Any change to this number must also bump
# _LADDER_INTEGRITY_MIGRATION_KEY in store/schema.py, because integrity_status
# is a cached projection of this constant.
LADDER_INTEGRITY_TOLERANCE_F = 2.0

# The observation tape is a coarser instrument than the CLI text and runs a
# degree or two low on some days, so it needs its own, wider threshold. Measured
# over 863 dense-tape station-days from 2026-06-01, |CLI - observed| never
# exceeded 2 F, so this flags 0 of 863 while still catching the multi-degree
# parse error the guard exists for. Bump the migration key when it moves.
LADDER_OBSERVED_TOLERANCE_F = 3.0

# A sparse tape has probably missed the peak, so it is not evidence about the
# CLI. KDEN and KNYC report ~25 observations a day against ~310 elsewhere; every
# |delta| >= 2 F in the 2026-08-18..09-01 window came from a tape thinner than
# this, and none of them was a CLI defect.
LADDER_OBSERVED_MIN_OBSERVATIONS = 200

# Mirrors prune_decision_snapshots(full_days=...): the horizon beyond which
# unapproved decision rows may already have been deleted, so an offered-bin
# population older than this is approval-biased and cannot be called complete.
# test_the_retention_horizon_matches_the_pruner pins the two together.
LADDER_RETENTION_FULL_DAYS = 7

INTEGRITY_OK = "ok"
INTEGRITY_UNCHECKED = "unchecked"
INTEGRITY_FLAGGED = "flagged"

INTEGRITY_SOURCE_EXCHANGE = "dataset_kalshi_markets"
INTEGRITY_SOURCE_OBSERVED = "nws_daily_high_ground_truth"

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
    traded_profiles TEXT,
    exchange_settlement_high_f REAL,
    observed_settlement_high_f REAL,
    truth_delta_f REAL,
    integrity_source TEXT,
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

# Columns added after the table's first shape. A journal created by an earlier
# revision of this branch still has the old five-column integrity block, and
# CREATE TABLE IF NOT EXISTS will not widen it, so init() runs these through
# _add_missing_columns exactly as it does for every other audit column set.
LADDER_BIN_OUTCOME_AUDIT_COLUMNS = {
    "traded_profiles": "TEXT",
    "observed_settlement_high_f": "REAL",
    "integrity_source": "TEXT",
}

_INSERT_COLUMNS = (
    "market_ticker", "target_date", "side", "series_ticker", "station_id",
    "recorded_at", "updated_at",
    "strike_type", "floor_strike", "cap_strike",
    "quote_lead", "quote_created_at", "quote_snapshot_id", "quote_risk_profile",
    "snapshot_count", "model_probability", "market_probability",
    "entry_bid", "entry_ask",
    "settlement_high_f", "truth_source", "resolved_yes", "side_won",
    "traded", "traded_profiles",
    "exchange_settlement_high_f", "observed_settlement_high_f",
    "truth_delta_f", "integrity_source", "integrity_status",
)

# The label block: everything derived from truth that arrived after the quote.
# It always refreshes, because CLI truth and both integrity channels can land
# days later and a stale `unchecked` is exactly the silence the guard exists to
# break.
_LABEL_COLUMNS = (
    "updated_at", "series_ticker", "station_id",
    "settlement_high_f", "truth_source", "resolved_yes", "side_won",
    "traded", "traded_profiles",
    "exchange_settlement_high_f", "observed_settlement_high_f",
    "truth_delta_f", "integrity_source", "integrity_status",
)

# The quote block: the representative pre-outcome view. It refreshes only on an
# upgrade, see below.
_QUOTE_UPSERT_COLUMNS = (
    "strike_type", "floor_strike", "cap_strike",
    "quote_lead", "quote_created_at", "quote_snapshot_id", "quote_risk_profile",
    "snapshot_count", "model_probability", "market_probability",
    "entry_bid", "entry_ask",
)

# Retention deletes exactly the unapproved rows this ledger reads, so a re-run
# over an older range sees a thinner journal than the first run did: the
# day-ahead close may be gone while a same-day row survives. Without this
# predicate the upsert would silently DOWNGRADE a correct day_ahead row to a
# later, easier same_day quote and nothing in the row would show it happened.
# A day_ahead quote therefore outranks a same_day one; within a class, the rule
# that chose the quote wins (last day-ahead, first same-day). Ties refresh, so
# re-running an unchanged range is still a full no-op on the quote.
_QUOTE_IS_UPGRADE = """(
        (ladder_bin_outcomes.quote_lead = 'same_day'
         AND excluded.quote_lead = 'day_ahead')
     OR (excluded.quote_lead = ladder_bin_outcomes.quote_lead
         AND ((excluded.quote_lead = 'day_ahead'
               AND excluded.quote_created_at >= ladder_bin_outcomes.quote_created_at)
           OR (excluded.quote_lead = 'same_day'
               AND excluded.quote_created_at <= ladder_bin_outcomes.quote_created_at)))
    )"""

# recorded_at is the first sighting and never moves; everything derived from a
# later pass refreshes, subject to the upgrade rule above. Re-running the same
# day is therefore a no-op on the provenance and an update everywhere else.
_UPSERT_SQL = (
    "INSERT INTO ladder_bin_outcomes (\n    "
    + ", ".join(_INSERT_COLUMNS)
    + "\n)\nVALUES ("
    + ", ".join("?" for _ in _INSERT_COLUMNS)
    + ")\nON CONFLICT(market_ticker, target_date, side) DO UPDATE SET\n    "
    + ",\n    ".join(
        [f"{column} = excluded.{column}" for column in _LABEL_COLUMNS]
        + [
            f"{column} = CASE WHEN {_QUOTE_IS_UPGRADE} "
            f"THEN excluded.{column} ELSE ladder_bin_outcomes.{column} END"
            for column in _QUOTE_UPSERT_COLUMNS
        ]
    )
)

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
    traded_profiles: str | None
    exchange_settlement_high_f: float | None
    observed_settlement_high_f: float | None
    truth_delta_f: float | None
    integrity_source: str | None
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
    exchange_settlement_high_f: float | None = None,
    observed_settlement_high_f: float | None = None,
    *,
    tolerance_f: float = LADDER_INTEGRITY_TOLERANCE_F,
    observed_tolerance_f: float = LADDER_OBSERVED_TOLERANCE_F,
) -> tuple[str, float | None, str | None]:
    """Cross-check the CLI label against whichever independent record exists.

    Returns ``(status, delta, source)`` where ``delta`` is ``CLI - source`` in
    °F and ``source`` names the channel that produced the verdict.  The
    exchange's own settlement value wins when it exists; the station's
    observation tape is the fallback that actually covers the current era.  With
    neither, the status is ``unchecked``: absence of a contradiction is not
    evidence of agreement, and recording it as ``ok`` would make the flagged
    count meaningless.
    """

    channels = (
        (exchange_settlement_high_f, tolerance_f, INTEGRITY_SOURCE_EXCHANGE),
        (observed_settlement_high_f, observed_tolerance_f, INTEGRITY_SOURCE_OBSERVED),
    )
    for reference, tolerance, source in channels:
        if reference is None:
            continue
        delta = float(settlement_high_f) - float(reference)
        if not math.isfinite(delta):
            continue
        if abs(delta) >= float(tolerance):
            return INTEGRITY_FLAGGED, delta, source
        return INTEGRITY_OK, delta, source
    return INTEGRITY_UNCHECKED, None, None


def derive_integrity_verdict(
    settlement_high_f: object,
    exchange_settlement_high_f: object,
    observed_settlement_high_f: object = None,
    *,
    tolerance_f: float = LADDER_INTEGRITY_TOLERANCE_F,
    observed_tolerance_f: float = LADDER_OBSERVED_TOLERANCE_F,
) -> tuple[str, float | None, str | None]:
    """Row-shaped wrapper used by the schema migration to re-derive a verdict."""

    if settlement_high_f is None:
        return INTEGRITY_UNCHECKED, None, None
    return assess_integrity(
        float(settlement_high_f),
        None if exchange_settlement_high_f is None else float(exchange_settlement_high_f),
        None if observed_settlement_high_f is None else float(observed_settlement_high_f),
        tolerance_f=tolerance_f,
        observed_tolerance_f=observed_tolerance_f,
    )


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


def settlement_day_close_utc(city: CityConfig, target_date: date) -> str:
    """The instant the target settlement day *closes* at the station, in UTC ISO.

    The same-day window ends here rather than at a bare ``target_date + N``
    wall-clock date.  A date-string bound is up to sixteen hours late for a
    UTC-8 station, which structurally permits a post-resolution quote to become
    a bin's scored view -- the one thing that would make these labels worthless.
    """

    return day_ahead_cutoff_utc(city, target_date + timedelta(days=1))


_QUOTE_COLUMNS = (
    "market_ticker, side, id, created_at, strike_type, floor_strike, cap_strike, "
    "model_probability, market_probability, entry_bid, entry_ask, risk_profile"
)

# The series segment of a market ticker: everything before the first '-'
# (``KXHIGHNY-26JUL06-B79.5`` -> ``KXHIGHNY``). UPPER() keeps the old
# case-insensitive LIKE semantics; a ticker with no '-' yields '' and matches no
# city, which is the correct answer for a retired or foreign series.
_SERIES_SEGMENT_SQL = "UPPER(substr(market_ticker, 1, instr(market_ticker, '-') - 1))"

# Bare columns beside an aggregate resolve to the row that produced the
# min/max -- a documented SQLite guarantee, and the reason this needs no window
# function or correlated subquery.
#
# ONE pass per lead per target date, not one per city. The planner serves
# `target_date = ?` from idx_decision_snapshots_retention_dedup and converts
# neither a `market_ticker LIKE ?` prefix nor the created_at range into an index
# range, so a per-city loop rescanned the same date partition fifteen times over
# (measured on production: 7.6 s for thirty passes against 0.7 s for one). Each
# city's own fixed-standard boundary is carried into the single pass as a CASE
# over the ticker's series segment instead.
_LAST_DAY_AHEAD_SQL = """
SELECT {columns}, MAX(created_at) AS chosen_at, COUNT(*) AS snapshot_count
FROM decision_snapshots
WHERE target_date = ?
  AND created_at >= ?
  AND created_at < {opens}
GROUP BY market_ticker, side
"""

_FIRST_SAME_DAY_SQL = """
SELECT {columns}, MIN(created_at) AS chosen_at, COUNT(*) AS snapshot_count
FROM decision_snapshots
WHERE target_date = ?
  AND created_at >= {opens}
  AND created_at < {closes}
GROUP BY market_ticker, side
"""

# How far back a day-ahead scan can have started. The book opens a target one
# day ahead, so three days is generous headroom that still keeps the scan
# bounded on a multi-gigabyte decision table.
_QUOTE_LOOKBACK_DAYS = 3


def _float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _row_get(row: object, key: str) -> Any:
    """Read an optional column from a dict or a ``sqlite3.Row`` alike.

    ``sqlite3.Row`` raises ``IndexError`` for an unknown key and has no
    ``.get``; plain mappings raise ``KeyError``.  Scoring accepts both.
    """

    try:
        return row[key]  # type: ignore[index]
    except (KeyError, IndexError):
        return None


def _cutoff_case(boundaries: Sequence[tuple[str, str]]) -> tuple[str, list[str]]:
    """A CASE mapping each city's series segment to its own UTC boundary."""

    whens = " ".join("WHEN ? THEN ?" for _ in boundaries)
    sql = f"CASE {_SERIES_SEGMENT_SQL} {whens} END"
    return sql, [value for pair in boundaries for value in pair]


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

    Two bounded passes over the date -- the day-ahead close, then the same-day
    opener for bins the day-ahead pass never saw -- each carrying every city's
    own settlement-day boundary as a CASE so neither pass has to be repeated per
    city.  Both are keyed on ``target_date`` plus a ``created_at`` range, so
    neither can degrade into a scan of the whole decision table.
    """

    day = date.fromisoformat(str(target_date))
    lower = (day - timedelta(days=_QUOTE_LOOKBACK_DAYS)).isoformat()
    by_series = {city.series_ticker: city for city in cities}
    if not by_series:
        return []
    opens_sql, opens_params = _cutoff_case(
        [(city.series_ticker, day_ahead_cutoff_utc(city, day)) for city in by_series.values()]
    )
    closes_sql, closes_params = _cutoff_case(
        [
            (city.series_ticker, settlement_day_close_utc(city, day))
            for city in by_series.values()
        ]
    )
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    quotes: dict[tuple[str, str], LadderQuote] = {}
    try:
        passes = (
            (
                _LAST_DAY_AHEAD_SQL.format(columns=_QUOTE_COLUMNS, opens=opens_sql),
                (str(target_date), lower, *opens_params),
                QUOTE_LEAD_DAY_AHEAD,
            ),
            (
                _FIRST_SAME_DAY_SQL.format(
                    columns=_QUOTE_COLUMNS, opens=opens_sql, closes=closes_sql
                ),
                (str(target_date), *opens_params, *closes_params),
                QUOTE_LEAD_SAME_DAY,
            ),
        )
        for sql, params, quote_lead in passes:
            for row in conn.execute(sql, params).fetchall():
                city = city_for_market_ticker(str(row["market_ticker"]))
                if city is None or city.series_ticker not in by_series:
                    continue
                quote = _quote_from_row(
                    row, city=city, target_date=str(target_date), quote_lead=quote_lead
                )
                # setdefault, so the day-ahead pass always wins the bin.
                quotes.setdefault((quote.market_ticker, quote.side), quote)
    finally:
        conn.row_factory = previous_factory
    return [quotes[key] for key in sorted(quotes)]


def traded_bins(conn: sqlite3.Connection, *, target_date: str) -> dict[tuple[str, str], str]:
    """``(market_ticker, side) -> the books that actually held it`` on a date.

    Reuses ``market_day_settlements.TRADED_STATUSES`` so "traded" means the same
    thing in both ledgers: a quote that rested and expired took no market risk.

    The value is a comma-joined list of ``risk_profile`` values rather than a
    bare flag because there are two economically separate paper books, and a bin
    held only by the research sleeve is not the same evidence as one the live
    book held.  Legacy rows with no profile record ``live``, matching
    ``_paper_profile_filter``'s ``COALESCE(risk_profile, 'live')``.
    """

    rows = conn.execute(
        "SELECT DISTINCT market_ticker, side, COALESCE(risk_profile, 'live') "
        "FROM paper_orders "
        f"WHERE target_date = ? AND status IN ({_TRADED_PLACEHOLDERS})",
        (str(target_date), *TRADED_STATUSES),
    ).fetchall()
    held: dict[tuple[str, str], set[str]] = {}
    for ticker, side, profile in rows:
        key = (str(ticker), str(side or "YES").strip().upper())
        held.setdefault(key, set()).add(str(profile))
    return {key: ",".join(sorted(profiles)) for key, profiles in held.items()}


def exchange_settlement_highs(conn: sqlite3.Connection) -> dict[SettlementKey, float]:
    """The exchange's own settlement °F per ``(series_ticker, target_date)``.

    ``dataset_kalshi_markets.expiration_value`` is the number Kalshi settled the
    ladder on, and it is constant across a city-day (verified: 0 city-days out
    of 1,350 carry more than one distinct value).  This is the strongest source
    in the journal that is independent of the NWS CLI text we parse.

    That invariant is re-checked here rather than trusted.  A city-day carrying
    two different settlement values is a contradiction *inside the guard's own
    reference*, so it cannot serve as a second opinion about anything: the day
    is dropped and logged, and the ledger falls through to the observation
    channel or records ``unchecked``.  Silently keeping whichever row SQLite
    returned first would be the same swallow-the-contradiction defect the rest
    of this module refuses.
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
    contradictions: set[SettlementKey] = set()
    for ticker, target_date, value in rows:
        city = city_for_market_ticker(str(ticker))
        if city is None:
            continue
        number = _float_or_none(value)
        if number is None:
            continue
        key = (city.series_ticker, str(target_date))
        existing = highs.get(key)
        if existing is not None and existing != number:
            contradictions.add(key)
            continue
        highs[key] = number
    for key in contradictions:
        logger.warning(
            "exchange settlement value is not constant across the city-day; "
            "dropping it from the integrity guard: %s", key
        )
        highs.pop(key, None)
    return highs


def observed_settlement_highs(
    conn: sqlite3.Connection,
    *,
    min_observations: int = LADDER_OBSERVED_MIN_OBSERVATIONS,
) -> dict[SettlementKey, float]:
    """Station observation-tape maxima per ``(series_ticker, target_date)``.

    Reads ``nws_daily_high_ground_truth`` from ``weather.db`` -- the second
    integrity channel, and the only one that covers the era being scored.  Days
    whose tape carries fewer than ``min_observations`` readings are omitted
    entirely: a sparse tape has probably missed the peak, so it is evidence
    about the *tape*, not about the CLI, and flagging on it would manufacture
    false contradictions (every |delta| >= 2 °F in the 2026-08-18..09-01 window
    came from a thin tape).

    ``is_complete`` is deliberately NOT used as the density test. It has been 0
    for every station-day since 2026-08-15 on production, so gating on it would
    make this channel inert in exactly the era it exists to cover -- the same
    failure as the exchange channel.
    """

    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'nws_daily_high_ground_truth'"
    ).fetchone()
    if present is None:
        return {}
    rows = conn.execute(
        "SELECT station_id, local_date, high_f FROM nws_daily_high_ground_truth "
        "WHERE high_f IS NOT NULL AND observation_count >= ?",
        (int(min_observations),),
    ).fetchall()
    highs: dict[SettlementKey, float] = {}
    for station_id, local_date, high in rows:
        try:
            city = city_for_station(str(station_id))
        except KeyError:
            continue
        number = _float_or_none(high)
        if number is None:
            continue
        highs[(city.series_ticker, str(local_date))] = number
    return highs


def resolve_ladder_quotes(
    quotes: Iterable[LadderQuote],
    *,
    settlement_highs: Mapping[SettlementKey, float],
    exchange_highs: Mapping[SettlementKey, float] | None = None,
    observed_highs: Mapping[SettlementKey, float] | None = None,
    traded: Mapping[tuple[str, str], str] | None = None,
    tolerance_f: float = LADDER_INTEGRITY_TOLERANCE_F,
    observed_tolerance_f: float = LADDER_OBSERVED_TOLERANCE_F,
) -> tuple[list[LadderBinOutcome], list[LadderQuote]]:
    """Label every quote that has final CLI truth; return the rest unlabelled.

    Bins whose station-day has no final CLI maximum are returned separately
    rather than guessed, mirroring ``backfill_market_day_settlements``'
    "unrecoverable" contract.
    """

    exchange = exchange_highs or {}
    observed = observed_highs or {}
    held = traded or {}
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
        observed_high = observed.get(key)
        status, delta, source = assess_integrity(
            high,
            exchange_high,
            observed_high,
            tolerance_f=tolerance_f,
            observed_tolerance_f=observed_tolerance_f,
        )
        profiles = held.get((quote.market_ticker, quote.side))
        resolved.append(
            LadderBinOutcome(
                quote=quote,
                settlement_high_f=high,
                truth_source=TRUTH_SOURCE_CLI_SETTLEMENT,
                resolved_yes=yes,
                side_won=side_wins(quote.side, yes),
                traded=profiles is not None,
                traded_profiles=profiles,
                exchange_settlement_high_f=exchange_high,
                observed_settlement_high_f=observed_high,
                truth_delta_f=delta,
                integrity_source=source,
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
                outcome.traded_profiles,
                outcome.exchange_settlement_high_f,
                outcome.observed_settlement_high_f,
                outcome.truth_delta_f,
                outcome.integrity_source,
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
    observed_highs: Mapping[SettlementKey, float] | None = None,
    cities: Sequence[CityConfig] = CITIES,
    tolerance_f: float = LADDER_INTEGRITY_TOLERANCE_F,
    observed_tolerance_f: float = LADDER_OBSERVED_TOLERANCE_F,
) -> tuple[list[LadderBinOutcome], list[LadderQuote]]:
    """Assemble one target date's resolved ladder from the journal.

    Pure reads. The caller opens the write transaction *after* this returns:
    these scans cost seconds on a multi-gigabyte journal and the 2-minute paper
    monitor waits on the write lock with a 30 s busy_timeout.
    """

    quotes = offered_ladder_quotes(conn, target_date=target_date, cities=cities)
    return resolve_ladder_quotes(
        quotes,
        settlement_highs=settlement_highs,
        exchange_highs=exchange_highs,
        observed_highs=observed_highs,
        traded=traded_bins(conn, target_date=target_date),
        tolerance_f=tolerance_f,
        observed_tolerance_f=observed_tolerance_f,
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
# Completeness -- the ledger's population is only as honest as its coverage
# ---------------------------------------------------------------------------


def assess_ladder_coverage(
    offered_bins: Mapping[tuple[str, str], int],
    *,
    today: str,
    full_days: int = LADDER_RETENTION_FULL_DAYS,
) -> dict[str, Any]:
    """Say out loud where the offered-bin population is not complete.

    ``offered_bins`` maps ``(series_ticker, target_date)`` to the number of
    distinct bins the journal still holds for that city-day.

    Two independent signals, because retention deletes exactly the rows this
    ledger reads (``COALESCE(approved,0)=0 AND COALESCE(signal_approved,0)=0``):

    ``retention_incomplete_dates``
        Target dates already older than the full-fidelity horizon.  Beyond it
        the surviving population is approval-biased -- the trade filter this
        table exists to escape -- whether or not the bin *count* looks right.

    ``thin_city_days``
        City-days offering fewer bins than the widest ladder in the same run.
        On production this separates the eroded era cleanly: every city-day from
        2026-07-19 onward carries 6 bins, while June and early July carry 1-5.
    """

    counts = {key: int(value) for key, value in offered_bins.items()}
    expected = max(counts.values(), default=0)
    thin = [
        {
            "series_ticker": series,
            "target_date": target_date,
            "offered_bins": count,
            "expected_bins": expected,
        }
        for (series, target_date), count in sorted(counts.items())
        if count < expected
    ]
    horizon = (date.fromisoformat(str(today)) - timedelta(days=int(full_days))).isoformat()
    incomplete = sorted(
        {target_date for _, target_date in counts if str(target_date) < horizon}
    )
    return {
        "city_days": len(counts),
        "expected_bins_per_city_day": expected,
        "thin_city_days": thin,
        "retention_full_fidelity_since": horizon,
        "retention_incomplete_dates": incomplete,
    }


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

    Every row that leaves the population is counted on its way out.  A metric
    over silently fewer bins than the caller asked for is the failure mode this
    whole module is trying to remove, so ``unchecked_bins``,
    ``flagged_bins_excluded`` and ``unscorable_bins`` all come back with the
    numbers, and the ``quote_leads`` / ``risk_profiles`` mixes come back too:
    ``quote_lead`` is ~98% confounded with ``risk_profile`` in production, so a
    lead mix read as a book comparison is a wrong answer waiting to happen.
    """

    model: list[tuple[float, float]] = []
    market: list[tuple[float, float]] = []
    blend: list[tuple[float, float]] = []
    scored = 0
    day_markets: set[tuple[str, str]] = set()
    cities: set[str] = set()
    dates: set[str] = set()
    flagged = 0
    unchecked = 0
    unscorable = 0
    traded = 0
    leads: dict[str, int] = {}
    profiles: dict[str, int] = {}
    for row in rows:
        status = str(row["integrity_status"])
        if status == INTEGRITY_FLAGGED:
            flagged += 1
            continue
        outcome = 1.0 if int(row["side_won"]) else 0.0
        model_p = _float_or_none(row["model_probability"])
        market_p = _float_or_none(row["market_probability"])
        if model_p is None or market_p is None:
            unscorable += 1
            continue
        scored += 1
        if status == INTEGRITY_UNCHECKED:
            unchecked += 1
        day_markets.add((str(row["market_ticker"]), str(row["target_date"])))
        cities.add(str(row["series_ticker"]))
        dates.add(str(row["target_date"]))
        traded += 1 if int(row["traded"] or 0) else 0
        lead = _row_get(row, "quote_lead")
        leads[str(lead)] = leads.get(str(lead), 0) + 1
        profile = _row_get(row, "quote_risk_profile")
        profiles[str(profile)] = profiles.get(str(profile), 0) + 1
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
        "unchecked_bins": unchecked,
        "unscorable_bins": unscorable,
        "quote_leads": leads,
        "risk_profiles": profiles,
        "brier_model": _brier(model),
        "brier_market": _brier(market),
        "brier_blend": _brier(blend),
        "log_loss_model": _log_loss(model),
        "log_loss_market": _log_loss(market),
        "realized_frequency": (
            sum(outcome for _, outcome in market) / len(market) if market else None
        ),
    }
