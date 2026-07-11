"""Persist EMOS (mu, sigma) day-high forecasts for the trade engine (Phase 2).

The trade engine consumes a predictive distribution, not a point. Phase 1 proved
the rolling-origin EMOS post-processor produces a calibrated Gaussian that beats
both climatology and the heuristic blend on CRPS. This module writes that Gaussian
to a ``forecast_emos_daily_high`` table -- the same forecaster -> trading handoff
contract as ``forecast_blend_daily_high`` (both live in the shared weather.db) --
so the trading ``ResidualCalibrator`` can read (mu, sigma) and build bucket
probabilities directly from the EMOS distribution behind its inert config flag.

Rolling-origin is preserved end to end: every archived (mu, sigma) is the
out-of-sample prediction Phase 1 validated (fit on strictly-prior days only), so a
backtest that reads this table is leakage-safe by construction.

LIVE note: for *tomorrow* the day-ahead forecast is the current model run, not the
``previous_day1`` reconstruction used for the historical archive. The live path
(fetch current multi-model forecasts -> fit on all history -> apply) is a separate
follow-up; this module ships the research/backtest archive the calibrator gate is
validated against first.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from cities import CITIES, CityConfig, get_city, parse_city_slugs
from emos_recalibration import correction_for_serve
from forecast_postproc_backtest import load_clisfo_truth, load_nwp_forecasts
from nwp_archive import (
    NWP_MODELS,
    NwpArchiveError,
    _http_get_json,
)

DEFAULT_CITY = get_city("sfo")
from postproc_models import EMOS_MIN_TRAIN, MIN_MODELS, apply_emos, emos_ngr_predictions, fit_emos

DB_PATH = Path(__file__).resolve().parent / "weather.db"
# inv_var = inverse-error-variance model weighting (Phase 4 winner: beat the
# equal-weight emos_ngr out-of-sample, DM -3.60, lower CRPS in every cohort).
DEFAULT_WEIGHT_MODE = "inv_var"
DEFAULT_SOURCE = "rolling_origin"


def _method_tag(weight_mode: str) -> str:
    return "emos_wmean" if weight_mode == "inv_var" else "emos_ngr"
LIVE_SOURCE = "live"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Serve-time trailing recalibration toggles (emos_recalibration.py). Bias
# passed the rolling-origin replay acceptance gate (pooled CRPS -1.3% over the
# last 60 scored days, no city worse than +2.6%); the sigma dispersion rescale
# FAILED its own gate (pooled CRPS +0.4%, BOS lead-2 +5.2%) and stays off. Both
# remain separately toggleable in recalibration_replay.py.
SERVE_RECAL_BIAS = True
SERVE_RECAL_SIGMA = False


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS forecast_emos_daily_high (
            station_id TEXT NOT NULL DEFAULT 'KSFO',
            target_date TEXT NOT NULL,
            lead_days INTEGER NOT NULL,
            predicted_high_f REAL NOT NULL,
            sigma_f REAL NOT NULL,
            n_models INTEGER,
            model_spread_f REAL,
            fetched_at TEXT NOT NULL,
            method TEXT NOT NULL DEFAULT 'emos_ngr',
            source TEXT NOT NULL DEFAULT 'rolling_origin',
            actual_high_f REAL,
            PRIMARY KEY (station_id, target_date, lead_days, source)
        )
        """
    )
    _migrate_station_key(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_emos_station_target "
        "ON forecast_emos_daily_high(station_id, target_date)"
    )


def _migrate_station_key(conn: sqlite3.Connection) -> None:
    """Rebuild a pre-multi-city table (no station_id) in place, once."""

    columns = {row[1] for row in conn.execute("PRAGMA table_info(forecast_emos_daily_high)")}
    if "station_id" in columns:
        return
    conn.execute("ALTER TABLE forecast_emos_daily_high RENAME TO forecast_emos_daily_high_legacy")
    conn.execute(
        """
        CREATE TABLE forecast_emos_daily_high (
            station_id TEXT NOT NULL DEFAULT 'KSFO',
            target_date TEXT NOT NULL,
            lead_days INTEGER NOT NULL,
            predicted_high_f REAL NOT NULL,
            sigma_f REAL NOT NULL,
            n_models INTEGER,
            model_spread_f REAL,
            fetched_at TEXT NOT NULL,
            method TEXT NOT NULL DEFAULT 'emos_ngr',
            source TEXT NOT NULL DEFAULT 'rolling_origin',
            actual_high_f REAL,
            PRIMARY KEY (station_id, target_date, lead_days, source)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO forecast_emos_daily_high
            (station_id, target_date, lead_days, predicted_high_f, sigma_f,
             n_models, model_spread_f, fetched_at, method, source, actual_high_f)
        SELECT 'KSFO', target_date, lead_days, predicted_high_f, sigma_f,
               n_models, NULL, fetched_at, method, source, actual_high_f
        FROM forecast_emos_daily_high_legacy
        """
    )
    conn.execute("DROP TABLE forecast_emos_daily_high_legacy")
    conn.commit()


def _model_spread_f(forecasts: dict[str, float]) -> float | None:
    """Cross-model disagreement (max - min raw forecast) -- the multi-city
    analogue of the blend's source_spread_f uncertain-day gate."""

    if len(forecasts) < 2:
        return None
    values = list(forecasts.values())
    return round(max(values) - min(values), 2)


def build_emos_archive(
    conn: sqlite3.Connection,
    *,
    city: CityConfig = DEFAULT_CITY,
    lead_days: int = 1,
    fetched_at: str | None = None,
    source: str = DEFAULT_SOURCE,
    weight_mode: str = DEFAULT_WEIGHT_MODE,
) -> int:
    """Compute rolling-origin EMOS (mu, sigma) over the NWP archive and upsert.

    Each row is the out-of-sample prediction for its day (fit on strictly-prior
    days), with the CLI settlement joined in where available for scoring.
    """

    ensure_schema(conn)
    station = city.nws_station_id
    truth = load_clisfo_truth(conn, station)
    nwp_by_date = load_nwp_forecasts(conn, lead_days, station)
    predictions = emos_ngr_predictions(sorted(nwp_by_date), truth, nwp_by_date, weight_mode=weight_mode)
    stamp = fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    method = _method_tag(weight_mode)

    rows = [
        (
            station,
            target_date,
            lead_days,
            mu,
            sigma,
            len(nwp_by_date.get(target_date, {})),
            _model_spread_f(nwp_by_date.get(target_date, {})),
            stamp,
            method,
            source,
            truth.get(target_date),
        )
        for target_date, (mu, sigma) in predictions.items()
    ]
    conn.executemany(
        """
        INSERT OR REPLACE INTO forecast_emos_daily_high
            (station_id, target_date, lead_days, predicted_high_f, sigma_f, n_models,
             model_spread_f, fetched_at, method, source, actual_high_f)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def load_emos_archive(
    conn: sqlite3.Connection, lead_days: int = 1, station_id: str = "KSFO"
) -> dict[str, tuple[float, float]]:
    """target_date -> (mu, sigma) from the persisted EMOS archive."""

    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='forecast_emos_daily_high'"
    ).fetchone() is None:
        return {}
    ensure_schema(conn)
    out: dict[str, tuple[float, float]] = {}
    for target_date, mu, sigma in conn.execute(
        "SELECT target_date, predicted_high_f, sigma_f FROM forecast_emos_daily_high "
        "WHERE lead_days = ? AND station_id = ?",
        (lead_days, station_id),
    ):
        out[target_date] = (float(mu), float(sigma))
    return out


def fetch_live_model_forecasts(
    target_date: date,
    *,
    city: CityConfig = DEFAULT_CITY,
    models: tuple[str, ...] = NWP_MODELS,
) -> dict[str, float]:
    """Current-run daily-max forecast for ``target_date`` per model (live path).

    For a future target the day-ahead forecast is the freshest model run, so this
    hits the regular forecast API (``temperature_2m_max``), NOT the previous_runs
    reconstruction used to build the historical archive. A model that does not
    cover the target is skipped (fail-soft), never silently zero-filled.
    """

    target_iso = target_date.isoformat()
    out: dict[str, float] = {}
    # One batched request for all models: the response carries one
    # ``temperature_2m_max_<model>`` array per model. At 15 cities on a
    # 30-minute refresh this is the difference between ~720 and ~6500
    # Open-Meteo calls per day. A model missing from the response (or null at
    # the target) is simply skipped -- same fail-soft contract as before.
    params = urlencode(
        {
            "latitude": f"{city.latitude:.4f}",
            "longitude": f"{city.longitude:.4f}",
            "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit",
            "timezone": city.settlement_tz_name,
            "forecast_days": "3",
            "models": ",".join(models),
        }
    )
    try:
        payload = _http_get_json(f"{OPEN_METEO_FORECAST_URL}?{params}")
    except NwpArchiveError:
        return out
    daily = payload.get("daily") or {}
    times = daily.get("time") or []
    if target_iso not in times:
        return out
    index = times.index(target_iso)
    for model in models:
        highs = daily.get(f"temperature_2m_max_{model}")
        # Single-model responses use the bare key; keep reading it so a
        # one-model call (tests, ad-hoc) still works.
        if highs is None and len(models) == 1:
            highs = daily.get("temperature_2m_max")
        if not highs or index >= len(highs):
            continue
        value = highs[index]
        if value is not None:
            out[model] = float(value)
    return out


def serve_live_emos(
    conn: sqlite3.Connection,
    target_date: date,
    *,
    city: CityConfig = DEFAULT_CITY,
    lead_days: int = 1,
    fetched_at: str | None = None,
    live_models: dict[str, float] | None = None,
    weight_mode: str = DEFAULT_WEIGHT_MODE,
    store_lead_days: int | None = None,
    recalibrate: bool = True,
) -> tuple[float, float] | None:
    """Fit EMOS on all settled history strictly before ``target_date`` and apply
    it to the current-run multi-model forecast, persisting (mu, sigma).

    ``lead_days`` selects the NWP archive lead the fit trains on;
    ``store_lead_days`` (default: same) is the lead recorded on the persisted
    row. The same-day serve passes ``lead_days=1, store_lead_days=0``: lead 0
    has no archive of its own, and the lead-1 per-model biases/weights are the
    closest learned coefficients for the current-run forecast of today.

    ``recalibrate`` applies the serve-time trailing recalibration
    (emos_recalibration.py) as a post-process on the EMOS output. Rolling-origin
    rows are never touched -- they stay the uncorrected record the correction
    window is computed from.

    CONSISTENCY NOTE: the fit is trained on the previous_day1 archive while the
    serve input is the current run -- biases are dominated by lead-invariant model
    offsets, but a future hardening step is to also archive the current-run
    forecast for tomorrow daily so train and serve share a lead.
    """

    ensure_schema(conn)
    station = city.nws_station_id
    truth = load_clisfo_truth(conn, station)
    nwp_by_date = load_nwp_forecasts(conn, lead_days, station)
    stored_lead = lead_days if store_lead_days is None else store_lead_days
    target_iso = target_date.isoformat()
    if target_date < _settlement_today(city):
        # A fully elapsed settlement day's "live" forecast is meaningless and
        # would shadow the rolling-origin row the leakage-safe rescore depends
        # on. A current-day CLI row may still be preliminary, so its mere
        # presence must not freeze the same-day serve.
        return None
    history = [
        (nwp_by_date[d], truth[d])
        for d in sorted(nwp_by_date)
        if d < target_iso and d in truth and len(nwp_by_date[d]) >= MIN_MODELS
    ]
    if len(history) < EMOS_MIN_TRAIN:
        return None
    params = fit_emos(history, weight_mode=weight_mode)
    if params is None:
        return None

    forecasts = (
        live_models
        if live_models is not None
        else fetch_live_model_forecasts(target_date, city=city)
    )
    # Drop live models with no learned bias (absent from training history) so an
    # unseen or renamed model cannot enter the debiased mean uncorrected.
    forecasts = {model: value for model, value in forecasts.items() if model in params.biases}
    if len(forecasts) < MIN_MODELS:
        return None
    mu, sigma = apply_emos(params, forecasts)

    if recalibrate and (SERVE_RECAL_BIAS or SERVE_RECAL_SIGMA):
        # The serve happens on the day `stored_lead` days before the target;
        # the correction window may only use truth published before that day.
        serve_date = target_date - timedelta(days=stored_lead)
        correction = correction_for_serve(
            conn,
            station,
            max(lead_days, 1),
            serve_date,
            apply_bias=SERVE_RECAL_BIAS,
            apply_sigma=SERVE_RECAL_SIGMA,
        )
        mu, sigma = correction.apply(mu, sigma)

    stamp = fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT OR REPLACE INTO forecast_emos_daily_high
            (station_id, target_date, lead_days, predicted_high_f, sigma_f, n_models,
             model_spread_f, fetched_at, method, source, actual_high_f)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            station,
            target_iso,
            stored_lead,
            mu,
            sigma,
            len(forecasts),
            _model_spread_f(forecasts),
            stamp,
            _method_tag(weight_mode),
            LIVE_SOURCE,
            truth.get(target_iso),
        ),
    )
    conn.commit()
    return mu, sigma


def _settlement_today(city: CityConfig = DEFAULT_CITY) -> date:
    return (
        datetime.now(timezone.utc) + timedelta(hours=city.standard_utc_offset_hours)
    ).date()


def _settlement_tomorrow(city: CityConfig = DEFAULT_CITY) -> date:
    return _settlement_today(city) + timedelta(days=1)


# The scheduled paper scan trades a rolling window (today .. today+2); serve EMOS
# for each open target so the research book has a distribution for every market.
ROLLING_SERVE_DAYS = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--lead", type=int, default=1)
    parser.add_argument("--backfill", action="store_true", help="(re)build the rolling-origin archive")
    parser.add_argument("--serve", metavar="DATE", help="serve live EMOS for a target ('tomorrow' or YYYY-MM-DD)")
    parser.add_argument(
        "--serve-rolling",
        action="store_true",
        help="serve live EMOS for today..today+2 (the scan's rolling window)",
    )
    parser.add_argument("--cities", default="all", help="'all' or comma slugs (e.g. sfo,nyc)")
    args = parser.parse_args(argv)
    if not (args.backfill or args.serve or args.serve_rolling):
        parser.error("nothing to do; pass --backfill, --serve, or --serve-rolling")

    cities = parse_city_slugs(args.cities)
    with sqlite3.connect(args.db) as conn:
        if args.backfill:
            for city in cities:
                written = build_emos_archive(conn, city=city, lead_days=args.lead)
                scored = conn.execute(
                    "SELECT COUNT(*) FROM forecast_emos_daily_high "
                    "WHERE actual_high_f IS NOT NULL AND lead_days = ? AND source = ? "
                    "AND station_id = ?",
                    (args.lead, DEFAULT_SOURCE, city.nws_station_id),
                ).fetchone()[0]
                print(
                    f"[{city.slug}] wrote {written} EMOS forecasts (lead {args.lead}); "
                    f"{scored} have CLI truth"
                )

        served = 0
        total_targets = 0
        for city in cities:
            today = _settlement_today(city)
            # Serve each target at its TRUE lead so the EMOS fit's per-model
            # biases match the forecast horizon (next-day -> lead 1, 2-day-out
            # -> lead 2). The NWP archive only holds leads >= 1, so the
            # same-day target (lead 0) has no training history of its own; it
            # is served with the lead-1 fit (per-model biases/weights and
            # sigma) applied to the CURRENT-run forecast for today, stored at
            # lead_days=0 so every 30-minute tick refreshes the same-day
            # market's distribution instead of leaving it on a pre-midnight
            # mean all day.
            serve_targets: list[tuple[date, int]] = []
            if args.serve:
                target = (
                    _settlement_tomorrow(city)
                    if args.serve == "tomorrow"
                    else date.fromisoformat(args.serve)
                )
                serve_targets.append((target, (target - today).days))
            if args.serve_rolling:
                serve_targets.extend(
                    (today + timedelta(days=offset), offset)
                    for offset in range(ROLLING_SERVE_DAYS)
                )
            total_targets += len(serve_targets)

            for target, lead in serve_targets:
                result = (
                    serve_live_emos(
                        conn,
                        target,
                        city=city,
                        # Lead 0 reuses the lead-1 coefficients (see comment
                        # above) and records the row at its true lead 0.
                        lead_days=max(lead, 1),
                        store_lead_days=lead,
                    )
                    if lead >= 0
                    else None
                )
                if result is None:
                    print(
                        f"live EMOS [{city.slug}] {target.isoformat()} (lead {lead}): "
                        "unavailable (already settled or thin coverage)"
                    )
                    continue
                mu, sigma = result
                served += 1
                print(
                    f"live EMOS [{city.slug}] {target.isoformat()} (lead {lead}): "
                    f"mu={mu:.2f}F sigma={sigma:.2f}F"
                )
        if args.serve_rolling:
            print(
                f"live EMOS rolling summary: served={served} targets={total_targets} "
                f"cities={len(cities)} leads=0..{ROLLING_SERVE_DAYS - 1}"
            )
        # Fail loud only when an explicit single --serve produced nothing.
        if args.serve and not args.serve_rolling and served == 0:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
