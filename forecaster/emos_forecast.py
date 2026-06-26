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

from forecast_postproc_backtest import load_clisfo_truth, load_nwp_forecasts
from nwp_archive import (
    KSFO_LATITUDE,
    KSFO_LONGITUDE,
    NWP_MODELS,
    SETTLEMENT_TZ,
    NwpArchiveError,
    _http_get_json,
)
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


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS forecast_emos_daily_high (
            target_date TEXT NOT NULL,
            lead_days INTEGER NOT NULL,
            predicted_high_f REAL NOT NULL,
            sigma_f REAL NOT NULL,
            n_models INTEGER,
            fetched_at TEXT NOT NULL,
            method TEXT NOT NULL DEFAULT 'emos_ngr',
            source TEXT NOT NULL DEFAULT 'rolling_origin',
            actual_high_f REAL,
            PRIMARY KEY (target_date, lead_days, source)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_emos_target ON forecast_emos_daily_high(target_date)"
    )


def build_emos_archive(
    conn: sqlite3.Connection,
    *,
    lead_days: int = 1,
    fetched_at: str | None = None,
    source: str = DEFAULT_SOURCE,
    weight_mode: str = DEFAULT_WEIGHT_MODE,
) -> int:
    """Compute rolling-origin EMOS (mu, sigma) over the NWP archive and upsert.

    Each row is the out-of-sample prediction for its day (fit on strictly-prior
    days), with the CLISFO settlement joined in where available for scoring.
    """

    ensure_schema(conn)
    truth = load_clisfo_truth(conn)
    nwp_by_date = load_nwp_forecasts(conn, lead_days)
    predictions = emos_ngr_predictions(sorted(nwp_by_date), truth, nwp_by_date, weight_mode=weight_mode)
    stamp = fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    method = _method_tag(weight_mode)

    rows = [
        (
            target_date,
            lead_days,
            mu,
            sigma,
            len(nwp_by_date.get(target_date, {})),
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
            (target_date, lead_days, predicted_high_f, sigma_f, n_models,
             fetched_at, method, source, actual_high_f)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def load_emos_archive(conn: sqlite3.Connection, lead_days: int = 1) -> dict[str, tuple[float, float]]:
    """target_date -> (mu, sigma) from the persisted EMOS archive."""

    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='forecast_emos_daily_high'"
    ).fetchone() is None:
        return {}
    out: dict[str, tuple[float, float]] = {}
    for target_date, mu, sigma in conn.execute(
        "SELECT target_date, predicted_high_f, sigma_f FROM forecast_emos_daily_high "
        "WHERE lead_days = ?",
        (lead_days,),
    ):
        out[target_date] = (float(mu), float(sigma))
    return out


def fetch_live_model_forecasts(target_date: date, *, models: tuple[str, ...] = NWP_MODELS) -> dict[str, float]:
    """Current-run daily-max forecast for ``target_date`` per model (live path).

    For a future target the day-ahead forecast is the freshest model run, so this
    hits the regular forecast API (``temperature_2m_max``), NOT the previous_runs
    reconstruction used to build the historical archive. A model that does not
    cover the target is skipped (fail-soft), never silently zero-filled.
    """

    target_iso = target_date.isoformat()
    out: dict[str, float] = {}
    for model in models:
        params = urlencode(
            {
                "latitude": f"{KSFO_LATITUDE:.4f}",
                "longitude": f"{KSFO_LONGITUDE:.4f}",
                "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit",
                "timezone": SETTLEMENT_TZ,
                "forecast_days": "3",
                "models": model,
            }
        )
        try:
            payload = _http_get_json(f"{OPEN_METEO_FORECAST_URL}?{params}")
        except NwpArchiveError:
            continue
        daily = payload.get("daily") or {}
        times = daily.get("time") or []
        highs = daily.get("temperature_2m_max") or []
        if target_iso in times:
            value = highs[times.index(target_iso)]
            if value is not None:
                out[model] = float(value)
    return out


def serve_live_emos(
    conn: sqlite3.Connection,
    target_date: date,
    *,
    lead_days: int = 1,
    fetched_at: str | None = None,
    live_models: dict[str, float] | None = None,
    weight_mode: str = DEFAULT_WEIGHT_MODE,
) -> tuple[float, float] | None:
    """Fit EMOS on all settled history strictly before ``target_date`` and apply
    it to the current-run multi-model forecast, persisting (mu, sigma).

    CONSISTENCY NOTE: the fit is trained on the previous_day1 archive while the
    serve input is the current run -- biases are dominated by lead-invariant model
    offsets, but a future hardening step is to also archive the current-run
    forecast for tomorrow daily so train and serve share a lead.
    """

    ensure_schema(conn)
    truth = load_clisfo_truth(conn)
    nwp_by_date = load_nwp_forecasts(conn, lead_days)
    target_iso = target_date.isoformat()
    if target_iso in truth:
        # A settled day's "live" forecast is meaningless and would shadow the
        # rolling-origin row the leakage-safe rescore depends on -- refuse.
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

    forecasts = live_models if live_models is not None else fetch_live_model_forecasts(target_date)
    # Drop live models with no learned bias (absent from training history) so an
    # unseen or renamed model cannot enter the debiased mean uncorrected.
    forecasts = {model: value for model, value in forecasts.items() if model in params.biases}
    if len(forecasts) < MIN_MODELS:
        return None
    mu, sigma = apply_emos(params, forecasts)

    stamp = fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT OR REPLACE INTO forecast_emos_daily_high
            (target_date, lead_days, predicted_high_f, sigma_f, n_models,
             fetched_at, method, source, actual_high_f)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (target_iso, lead_days, mu, sigma, len(forecasts), stamp, _method_tag(weight_mode), LIVE_SOURCE, truth.get(target_iso)),
    )
    conn.commit()
    return mu, sigma


def _settlement_today() -> date:
    return (datetime.now(timezone.utc) - timedelta(hours=8)).date()


def _settlement_tomorrow() -> date:
    return _settlement_today() + timedelta(days=1)


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
    args = parser.parse_args(argv)
    if not (args.backfill or args.serve or args.serve_rolling):
        parser.error("nothing to do; pass --backfill, --serve, or --serve-rolling")

    with sqlite3.connect(args.db) as conn:
        if args.backfill:
            written = build_emos_archive(conn, lead_days=args.lead)
            scored = conn.execute(
                "SELECT COUNT(*) FROM forecast_emos_daily_high "
                "WHERE actual_high_f IS NOT NULL AND lead_days = ? AND source = ?",
                (args.lead, DEFAULT_SOURCE),
            ).fetchone()[0]
            print(f"wrote {written} EMOS forecasts (lead {args.lead}); {scored} have CLISFO truth")

        targets: list[date] = []
        if args.serve:
            targets.append(_settlement_tomorrow() if args.serve == "tomorrow" else date.fromisoformat(args.serve))
        if args.serve_rolling:
            targets.extend(_settlement_today() + timedelta(days=offset) for offset in range(ROLLING_SERVE_DAYS))

        served = 0
        for target in targets:
            result = serve_live_emos(conn, target, lead_days=args.lead)
            if result is None:
                print(f"live EMOS for {target.isoformat()}: unavailable (already settled or thin coverage)")
                continue
            mu, sigma = result
            served += 1
            print(f"live EMOS for {target.isoformat()} (lead {args.lead}): mu={mu:.2f}F sigma={sigma:.2f}F")
        # Fail loud only when an explicit single --serve produced nothing.
        if args.serve and not args.serve_rolling and served == 0:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
