"""Persist EMOS (mu, sigma) day-high forecasts for the trade engine (Phase 2).

The trade engine consumes a predictive distribution, not a point. This module fits
an EMOS Gaussian to station-specific NWP history and writes that distribution
to a ``forecast_emos_daily_high`` table -- the same forecaster -> trading handoff
contract as ``forecast_blend_daily_high`` (both live in the shared weather.db) --
so the trading ``ResidualCalibrator`` can read (mu, sigma) and build bucket
probabilities directly from the EMOS distribution behind its inert config flag.

Historical rows enforce a prior-truth boundary, but their ``previous_dayN``
members are stitched fixed-lead reconstructions, not a single run available at a
trading decision time. They support weather-skill research, not executable-edge
claims. Live serving uses current-run named model maxima. Separate append-only
vintage tables preserve each served distribution, its members and training
lineage without overwriting the current legacy handoff row. Provider model
initialization and hourly completeness remain unknown for daily-only responses.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import apple_weatherkit
from cities import CITIES, CityConfig, get_city, parse_city_slugs
from emos_recalibration import correction_for_serve
from emos_sources import ROLLING_ORIGIN_V2_SOURCE
from live_forecast_evidence import LiveModelForecast, record_live_forecast
from nwp_archive import (
    NWP_MODELS,
    NwpArchiveError,
    _http_get_json,
)

DEFAULT_CITY = get_city("sfo")
from postproc_models import (
    EMOS_MIN_TRAIN,
    MIN_MODELS,
    apply_emos,
    debiased_range,
    emos_ngr_predictions_with_spread,
    fit_emos,
)
from scores import SIGMA_FLOOR_F
from truth_store import load_clisfo_truth, load_nwp_forecasts

DB_PATH = Path(__file__).resolve().parent / "weather.db"
# inv_var = inverse-error-variance model weighting (Phase 4 winner: beat the
# equal-weight emos_ngr out-of-sample, DM -3.60, lower CRPS in every cohort).
DEFAULT_WEIGHT_MODE = "inv_var"
DEFAULT_SOURCE = ROLLING_ORIGIN_V2_SOURCE


def _method_tag(weight_mode: str) -> str:
    return "emos_wmean" if weight_mode == "inv_var" else "emos_ngr"
LIVE_SOURCE = "live"
# Total seconds one serve run may spend on WeatherKit calls (slow or failing).
APPLE_TIME_BUDGET_SECONDS = 60
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
# Per-city socket wait. Normal answers take <1 s; when Open-Meteo stalls, the
# 45 s archive default x 20 cities overran the refresh's 300 s baseline cap
# and lost every city. 20 x 10 s + the Apple budget stays under that cap.
LIVE_FETCH_TIMEOUT_SECONDS = 10.0

# Serve-time trailing recalibration toggles (emos_recalibration.py). Bias
# passed the rolling-origin replay acceptance gate (pooled CRPS -1.3% over the
# last 60 scored days, no city worse than +2.6%); the sigma dispersion rescale
# FAILED its own gate (pooled CRPS +0.4%, BOS lead-2 +5.2%) and stays off. Both
# remain separately toggleable in recalibration_replay.py.
SERVE_RECAL_BIAS = True
SERVE_RECAL_SIGMA = False

# Same-day (lead 0) dispersion. The NWP archive only holds leads >= 1, so a
# lead-0 EMOS cannot be fit: the same-day serve borrows the lead-1 fit, whose
# irreducible-error intercept ``var_c`` is a lead-1 quantity that never shrinks
# for the shorter horizon. SERVE_RECAL_SIGMA is off, so nothing downstream
# corrects it, and probability.py takes the sigma essentially unmodified.
#
# Measured on the append-only serve log (paper_trading.forecast_snapshots,
# 2026-07-06..2026-09-07, every 5-minute serve rather than the last write of the
# day) joined to final CLI settlements, with the debiased spread of FC-1 already
# applied. Pooled lead-0 mean z^2 = 0.762 against an ideal of 1.0, so the
# same-day Gaussian is on average 1.15x too wide; lead 1 sits at 0.865.
#
# THE POOLED NUMBER IS NOT A USABLE SCALE. Per-station lead-0 z^2 spans 0.39
# (KAUS) to 1.17 (KHOU): a single pooled multiplier sharpens the stations that
# are ALREADY too sharp, and an over-confident sigma over-prices the favorite
# bin and over-sizes the stake -- the one direction that costs money. Measured
# floor-aware effect of a pooled 0.886 on the same serve log: KOKC 1.145 ->
# 1.401, KBOS 1.087 -> 1.345, KHOU 1.166 -> 1.287, KNYC 0.960 -> 1.194,
# KSFO 0.924 -> 1.177. So the scale is PER STATION, and each station's scale is
# min(1.0, sqrt(z^2_station)): a station whose same-day Gaussian is already at
# or past calibration is left exactly alone (identity), and only genuinely
# over-dispersed stations are sharpened. Every corrected station's z^2 rises --
# that IS the correction -- but under this table none is pushed PAST 1.0: the
# post-change per-station maximum is KHOU's own unchanged 1.166, and the three
# stations that already sat above 1.0 (KBOS 1.087, KHOU 1.166, KOKC 1.145) are
# bit-identical no-ops. The stations the pooled constant would have made
# over-confident land exactly on 1.000 instead (KNYC 0.960 -> 1.000, KSFO
# 0.924 -> 1.000, KPHL 0.796 -> 1.000). Pooled moves 0.762 -> 0.903
# (floor-aware; 0.919 under the rejected pooled constant, so pooled calibration
# is not materially given up).
#
# PRECISION. n = 107,491 counts ~100 five-minute serves of the same station-day;
# the independent unit is the station-day, of which there are 742 pooled and
# only 38-55 per station. SE(mean z^2) ~ sqrt(2/days) ~ 0.20 per station, so a
# station scale is pinned to roughly +/-10% and the pooled day-equal-weighted
# figure (0.811, not 0.762) is the honest central estimate. The 0.75 lower bound
# below exists BECAUSE of that noise: it caps how far one 50-day estimate may
# sharpen a served Gaussian.
#
# NOTE the population matters: the 2026-09-03 audit read z^2 = 0.55 from
# forecast_emos_daily_high, which keeps only the LAST write per target
# (INSERT OR REPLACE) -- the sharpest, end-of-day serve. That understates the
# dispersion the book actually trades against all day.
#
# SCOPE / FC-3 INTERACTION. This calibrates lead 0 to z^2 = 1 per station, which
# also absorbs the share of the over-dispersion lead 1 has and keeps (pooled
# 0.865). Lead 1 is FC-3's per-station serve recalibration, not FC-2's. Whoever
# lands FC-3 must exclude lead 0 from a per-station sigma correction or the two
# will compose and double-correct.
#
# UNANALYZED PRODUCTION CONSUMERS (2026-09-13 re-check). Everything above
# scores the served Gaussian against settlement; none of FC-2's commit
# messages (7a577d075, 75b6978d3, da80ac438) analyzed what reads this row
# back. Two trading paths do, both through
# SfoForecasterAdapter._latest_emos_snapshot, which takes the FRESHEST
# forecast_emos_daily_high row for the target across all leads (source
# precedence, then fetched_at). For today's target that is normally this
# scaled lead-0 row once the same-day serve has run; until it has, an
# unscaled lead-1 row still inside max_forecast_age_hours can back the read.
#  1. Before the 14:00 same-day entry cutoff the scanner still scans today's
#     event (_cli/scan.py _rolling_live_event_targets), reads the row through
#     latest_blend / latest_live_forecast (SFO reaches it only when its legacy
#     blend is stale or missing), prices the bins from its sigma_f
#     (matching_emos_distribution, which refuses the target when the profile
#     has emos_distribution_enabled off; config_for_city forces it on for
#     every city without the SFO blend), makes same-day ENTRY decisions on
#     those probabilities and journals them (PaperStore.record_probabilities).
#  2. After the cutoff, with the same-day model heartbeat on (it was on in
#     production at the 2026-09-06 verification),
#     monitor._refresh_same_day_model_reads loads the row through
#     latest_emos_snapshot, prices the open event's bins with it
#     (ResidualCalibrator.bucket_probabilities, emos_mu_sigma) and journals
#     them.
# run_paper_monitor reads the latest journaled probability from either path
# back (PaperStore.latest_model_probability_read) as the model-side
# probability exits.decide_exit uses for the NO-side model veto
# (HOLD_MODEL_VETO) and for the take-profit fair value (plus the
# research-only take-profit margin). A sharper sigma generally makes those
# bin probabilities more extreme, so this table changes same-day entries,
# which stops are vetoed and where take-profits fire, not just calibration.
# A replay against this table is still OWED, and it requires production
# database access: the journaled model reads, scan decisions, open positions
# and exit decisions exist only in the production paper database. It must
# cover same-day scan entries and exits before 14:00 as well as the monitor
# heartbeat reads after it, and, because probability_snapshots does not
# record which lead backed a read, it must join each journaled read to the
# lead_days of the forecast_emos_daily_high row that backed it. Until that
# replay is done, treat the table as validated for calibration only, not for
# its effect on entries or exits.
#
# min(1.0, sqrt(measured lead-0 z^2)) as measured; LEAD0_SIGMA_SCALE_BOUNDS
# below clamps the four entries that fall under 0.75 at the point of use.
LEAD0_SIGMA_SCALE_BY_STATION = {
    "KATL": 0.951,
    "KAUS": 0.627,
    "KBOS": 1.000,
    "KDEN": 0.685,
    "KDFW": 0.875,
    "KHOU": 1.000,
    "KLAX": 0.629,
    "KMDW": 0.772,
    "KMIA": 0.875,
    "KNYC": 0.980,
    "KOKC": 1.000,
    "KPHL": 0.892,
    "KPHX": 0.665,
    "KSEA": 0.809,
    "KSFO": 0.961,
}
# A station with no measured same-day dispersion (a new city, or one whose serve
# log has not accumulated settled days) keeps the borrowed lead-1 width. That is
# the over-dispersed, under-sized direction -- the safe one to be wrong in.
LEAD0_SIGMA_SCALE_DEFAULT = 1.0
# Hard guard on every entry above and on any future retune: a borrowed
# longer-lead dispersion may only be sharpened (never widened -- a shorter
# horizon cannot be more uncertain than the fit it borrows), and never below
# 0.75, which bounds how much a single 38-55 day per-station estimate is allowed
# to move a served Gaussian. Four stations (KAUS, KDEN, KLAX, KPHX) measure
# below 0.75 and are deliberately under-corrected by this clamp.
LEAD0_SIGMA_SCALE_BOUNDS = (0.75, 1.0)


def _borrowed_lead_sigma_scale(station: str, stored_lead: int, fit_lead: int) -> float:
    """Dispersion correction for a serve that borrows a longer lead's fit.

    Identity whenever the serve is fit at its own lead; only the same-day serve
    borrows today (see LEAD0_SIGMA_SCALE_BY_STATION).
    """

    if stored_lead != 0 or fit_lead <= stored_lead:
        return 1.0
    scale = LEAD0_SIGMA_SCALE_BY_STATION.get(station, LEAD0_SIGMA_SCALE_DEFAULT)
    low, high = LEAD0_SIGMA_SCALE_BOUNDS
    return min(max(scale, low), high)


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
            source TEXT NOT NULL DEFAULT 'rolling_origin_v2',
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
            source TEXT NOT NULL DEFAULT 'rolling_origin_v2',
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


def _model_spread_f(forecasts: dict[str, float], biases: dict[str, float]) -> float | None:
    """Cross-model disagreement (max - min) over *debiased* members -- the
    multi-city analogue of the blend's source_spread_f uncertain-day gate.

    The per-model biases must come off first. Without them the statistic mostly
    measures which coarse grids resolve the station as ocean rather than how
    uncertain the day is, and the gate it feeds
    (``StrategyConfig.max_source_spread_f``) vetoed the KSFO and KLAX books
    outright.
    """

    if len(forecasts) < 2:
        return None
    return debiased_range(forecasts, biases)


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

    Each row is the out-of-sample prediction for its day, replaying the truth
    that would have been available at that lead's live serve. Version 2 uses a
    distinct source label so historical scoreboards remain comparable.
    """

    ensure_schema(conn)
    station = city.nws_station_id
    truth = load_clisfo_truth(conn, station)
    nwp_by_date = load_nwp_forecasts(conn, lead_days, station)
    predictions = emos_ngr_predictions_with_spread(
        sorted(nwp_by_date),
        truth,
        nwp_by_date,
        weight_mode=weight_mode,
        truth_lag_days=lead_days,
    )
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
            spread_f,
            stamp,
            method,
            source,
            truth.get(target_date),
        )
        for target_date, (mu, sigma, spread_f) in predictions.items()
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

    return fetch_live_model_forecasts_multi(city=city, models=models).get(target_date, {})


def fetch_live_model_forecasts_multi(
    *,
    city: CityConfig = DEFAULT_CITY,
    models: tuple[str, ...] = NWP_MODELS,
) -> dict[date, dict[str, float]]:
    """Current-run forecasts for every returned target in one three-day call.

    A model or target missing from the response is omitted. The single-target
    helper above remains the compatibility API and selects one date from this
    fail-soft mapping.
    """

    # One request per city/tick for every model and all three rolling targets:
    # 15 cities therefore make 15 calls, rather than 45 target-specific calls.
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
        payload = _http_get_json(
            f"{OPEN_METEO_FORECAST_URL}?{params}", timeout=LIVE_FETCH_TIMEOUT_SECONDS
        )
    except NwpArchiveError:
        return {}
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    daily = payload.get("daily") or {}
    times = daily.get("time") or []
    out: dict[date, dict[str, float]] = {}
    for index, target_iso in enumerate(times):
        try:
            target = date.fromisoformat(str(target_iso))
        except ValueError:
            continue
        values: dict[str, float] = {}
        for model in models:
            highs = daily.get(f"temperature_2m_max_{model}")
            if highs is None and len(models) == 1:
                highs = daily.get("temperature_2m_max")
            if not highs or index >= len(highs):
                continue
            value = highs[index]
            if type(value) in (int, float) and math.isfinite(value):
                values[model] = float(value)
        if values:
            out[target] = LiveModelForecast(values, retrieved_at=retrieved_at)
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
    apple_high_f: float | None = None,
    now: datetime | None = None,
) -> tuple[float, float] | None:
    """Fit EMOS on all settled history strictly before ``target_date`` and apply
    it to the current-run multi-model forecast, persisting (mu, sigma).

    ``lead_days`` selects the NWP archive lead the fit trains on;
    ``store_lead_days`` (default: same) is the lead recorded on the persisted
    row. The same-day serve passes ``lead_days=1, store_lead_days=0``: lead 0
    has no archive of its own, and the lead-1 per-model biases/weights are the
    closest learned coefficients for the current-run forecast of today. Its
    *dispersion* is not borrowed unchanged -- see
    LEAD0_SIGMA_SCALE_BY_STATION.

    ``recalibrate`` applies the serve-time trailing recalibration
    (emos_recalibration.py) as a post-process on the EMOS output. Rolling-origin
    rows are never touched -- they stay the uncorrected record the correction
    window is computed from.

    CONSISTENCY NOTE: the fit is trained on the previous_day1 archive while the
    serve input is the current run -- biases are dominated by lead-invariant model
    offsets. The immutable live evidence now preserves current-run inputs so a
    future replay can measure this train/serve mismatch. It does not remove it.

    ``apple_high_f`` (an Apple WeatherKit station-day high, when available)
    nudges the final mean of a future-day target with the fixed rule in
    ``apple_weatherkit.apple_nudge_f``; the nudge is recorded in the live evidence so
    ``--apple-score`` can compare with/without.

    After that, an evening carry-over (``_carry_adjustment``) corrects a next-day
    mean from today's observed high-so-far; ``now`` is the injectable clock.
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
    training = [
        (d, nwp_by_date[d], truth[d])
        for d in sorted(nwp_by_date)
        if d < target_iso and d in truth and len(nwp_by_date[d]) >= MIN_MODELS
    ]
    history = [(models, actual) for _day, models, actual in training]
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
    raw_forecasts = forecasts
    forecasts = {model: value for model, value in forecasts.items()
                 if model in params.biases and type(value) in (int, float) and math.isfinite(value)}
    if len(forecasts) < MIN_MODELS:
        return None
    mu, sigma = apply_emos(params, forecasts)
    # A serve that borrows a longer lead's fit also borrows its dispersion; give
    # the same-day horizon its own (FC-2). The floor apply_emos imposed has to be
    # re-imposed after the rescale: a horizon correction must not push a served
    # Gaussian below the absolute sharpness limit every other path respects.
    sigma = max(
        sigma * _borrowed_lead_sigma_scale(station, stored_lead, lead_days), SIGMA_FLOOR_F
    )

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

    serve_policy = {
        "bias_recalibration": recalibrate and SERVE_RECAL_BIAS,
        "sigma_recalibration": recalibrate and SERVE_RECAL_SIGMA,
        "borrowed_lead_sigma_scale": _borrowed_lead_sigma_scale(station, stored_lead, lead_days),
    }
    # Same-day targets never take Apple (checked now, so a run that crosses
    # settlement midnight cannot nudge what has become today's market).
    if apple_high_f is not None and target_date > _settlement_today(city):
        # 50% toward Apple, capped at 3.5F, ignored on a 7F+ disagreement. Sigma unchanged.
        nudge = apple_weatherkit.apple_nudge_f(mu, apple_high_f)
        mu += nudge or 0.0
        serve_policy["apple_adjustment_f"] = nudge or 0.0
        serve_policy["apple_action"] = "skipped_disagreement" if nudge is None else "applied"

    carry, carry_policy = _carry_adjustment(
        conn, city, target_date, stored_lead, now or datetime.now(timezone.utc), truth
    )
    mu += carry
    serve_policy.update(carry_policy)

    stamp = fetched_at or datetime.now(timezone.utc).isoformat(timespec="microseconds")
    record_live_forecast(
        conn, city=city, target_date=target_date, recorded_at=stamp,
        inputs=raw_forecasts, used_models=set(forecasts), mu=mu, sigma=sigma,
        fit_lead_days=lead_days, legacy_lead_days=stored_lead, method=_method_tag(weight_mode),
        training=training,
        fit_params={name: getattr(params, name) for name in params.__slots__},
        serve_policy=serve_policy,
    )
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
            _model_spread_f(forecasts, params.biases),
            stamp,
            _method_tag(weight_mode),
            LIVE_SOURCE,
            truth.get(target_iso),
        ),
    )
    conn.commit()
    return mu, sigma


CARRY_GAIN = 0.20
CARRY_CLIP_F = 1.5  # guard against a bad observation; should rarely bind
CARRY_FROM_HOUR_LST = 17
CARRY_OFFSET_DAYS = 60
CARRY_MIN_OFFSET_DAYS = 20


def _carry_adjustment(
    conn: sqlite3.Connection,
    city: CityConfig,
    target_date: date,
    stored_lead: int,
    now: datetime,
    truth: dict[str, float],
) -> tuple[float, dict]:
    """Evening carry-over for a next-day mean: -0.20 * (yesterday's served mean for
    today - (today's high so far + trailing offset)). Never raises: any data
    problem is a recorded skip."""

    def skip(reason: str) -> tuple[float, dict]:
        return 0.0, {"carry_adjustment_f": 0.0, "carry_action": f"skipped_{reason}"}

    tz = city.fixed_standard_timezone()
    now_lst = now.astimezone(tz)
    # Today comes from the injected clock, like the hour gate and the observation cutoff.
    if stored_lead != 1 or target_date != now_lst.date() + timedelta(days=1):
        return skip("not_next_day")
    if now_lst.hour < CARRY_FROM_HOUR_LST:
        return skip("before_17_lst")
    today = target_date - timedelta(days=1)
    station = city.nws_station_id
    try:
        row = conn.execute(
            "SELECT predicted_high_f, serve_policy_json FROM forecast_emos_live_vintages "
            "WHERE station_id=? AND target_date=? AND lead_days=1 "
            "ORDER BY recorded_at DESC LIMIT 1",
            (station, today.isoformat()),
        ).fetchone()
        if row is None:
            return skip("no_yesterday_forecast")
        # Strip yesterday's Apple nudge and own carry so the error measures the model.
        yesterday = json.loads(row[1])
        today_mu = row[0] - (yesterday.get("apple_adjustment_f") or 0.0) - (
            yesterday.get("carry_adjustment_f") or 0.0
        )

        # Offset and today's high-so-far share one cutoff: reports at or before
        # this same local standard clock time on each day.
        temps: dict[str, list[float]] = {}
        afternoon: dict[str, int] = {}
        for local_date, observed_at, temp in conn.execute(
            "SELECT local_date, observed_at, temp_f FROM nws_station_observations "
            "WHERE station_id=? AND local_date BETWEEN ? AND ? AND temp_f IS NOT NULL",
            (station, (today - timedelta(days=CARRY_OFFSET_DAYS)).isoformat(), today.isoformat()),
        ):
            seen = datetime.fromisoformat(observed_at)
            if seen.astimezone(tz).time() <= now_lst.time() and (
                local_date != today.isoformat() or seen <= now
            ):
                temps.setdefault(local_date, []).append(temp)
                afternoon[local_date] = afternoon.get(local_date, 0) + (seen.astimezone(tz).hour >= 12)

        def hsf(day: str) -> float | None:
            ok = len(temps.get(day, ())) >= 10 and afternoon.get(day, 0) >= 3
            return max(temps[day]) if ok else None

        today_hsf = hsf(today.isoformat())
        if today_hsf is None:
            return skip("thin_observations")
        gaps = [
            truth[d] - h
            for i in range(1, CARRY_OFFSET_DAYS + 1)
            if (d := (today - timedelta(days=i)).isoformat()) in truth
            and (h := hsf(d)) is not None
        ]
        if len(gaps) < CARRY_MIN_OFFSET_DAYS:
            return skip("thin_offset_history")
        offset = sum(gaps) / len(gaps)
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        return skip("data_error")

    e = today_mu - (today_hsf + offset)
    adjustment = max(-CARRY_CLIP_F, min(CARRY_CLIP_F, -CARRY_GAIN * e))
    return adjustment, {
        "carry_adjustment_f": adjustment, "carry_action": "applied", "carry_e_f": e,
        "carry_hsf_f": today_hsf, "carry_offset_f": offset, "carry_today_mu_f": today_mu,
        "carry_hour_lst": round(now_lst.hour + now_lst.minute / 60, 2),
    }


def _settlement_today(city: CityConfig = DEFAULT_CITY) -> date:
    return (
        datetime.now(timezone.utc) + timedelta(hours=city.standard_utc_offset_hours)
    ).date()


def _settlement_tomorrow(city: CityConfig = DEFAULT_CITY) -> date:
    return _settlement_today(city) + timedelta(days=1)


def scored_history_days(
    conn: sqlite3.Connection, city: CityConfig, lead_days: int = 1
) -> int:
    """Settlement days with CLI truth AND a >= MIN_MODELS NWP archive row at
    ``lead_days`` -- the training set a live serve for ``city`` can see."""

    station = city.nws_station_id
    truth = load_clisfo_truth(conn, station)
    nwp_by_date = load_nwp_forecasts(conn, lead_days, station)
    return sum(
        1
        for target_iso, models in nwp_by_date.items()
        if target_iso in truth and len(models) >= MIN_MODELS
    )


def is_onboarded(conn: sqlite3.Connection, city: CityConfig) -> bool:
    """A station is onboarded once forecast_emos_daily_high holds ANY row for it.

    Both writers respect the same floor: build_emos_archive only emits a
    rolling-origin row once the station has EMOS_MIN_TRAIN scored days, and
    serve_live_emos only emits a live row under the same floor. A registry row
    whose post-deploy backfill (trading/deploy/aws/README.md, "Adding A City")
    has not run therefore has no row of any source.
    """

    ensure_schema(conn)
    row = conn.execute(
        "SELECT 1 FROM forecast_emos_daily_high WHERE station_id = ? LIMIT 1",
        (city.nws_station_id,),
    ).fetchone()
    return row is not None


def awaiting_onboarding(conn: sqlite3.Connection, city: CityConfig) -> int | None:
    """Scored lead-1 day count when ``city`` cannot serve yet AND never has.

    Returns ``None`` for every station that is onboarded (see is_onboarded) or
    already has EMOS_MIN_TRAIN scored days. A station that once served or was
    scored but is now below the floor is NOT awaiting onboarding: that is a
    real outage and must keep failing the scheduled serve.
    """

    if is_onboarded(conn, city):
        return None
    days = scored_history_days(conn, city, lead_days=1)
    if days >= EMOS_MIN_TRAIN:
        return None
    return days


# The scheduled paper scan trades a rolling window (today .. today+2); serve EMOS
# for each open target so the research book has a distribution for every market.
ROLLING_SERVE_DAYS = 3


def apple_score(
    conn: sqlite3.Connection, key: str = "apple_adjustment_f"
) -> dict[int, dict[str, float]]:
    """Settled MAE per lead of adjusted live forecasts vs their own baselines.

    ``key`` is the serve_policy adjustment (Apple nudge or evening carry-over).
    Every vintage served with one is paired with itself minus its adjustment, so both arms share issuance, target and truth. Paired errors are
    averaged within each (station, target, lead) first, so a station-day counts
    once however many refreshes it had.
    """

    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='forecast_emos_live_vintages'"
    ).fetchone():
        return {}
    vintages: dict[tuple[str, str, int], list[tuple[float, float]]] = {}
    for station, target, lead, mu, policy in conn.execute(
        "SELECT station_id, target_date, lead_days, predicted_high_f, serve_policy_json "
        "FROM forecast_emos_live_vintages WHERE serve_policy_json LIKE ?",
        (f"%{key}%",),
    ):
        adjustment = json.loads(policy).get(key)
        if adjustment is not None:
            vintages.setdefault((station, target, lead), []).append((mu, adjustment))
    truth_by_station: dict[str, dict[str, float]] = {}
    out: dict[int, dict[str, float]] = {}
    for (station, target, lead), pairs in vintages.items():
        if station not in truth_by_station:
            truth_by_station[station] = load_clisfo_truth(conn, station)
        truth = truth_by_station[station]
        if target not in truth:
            continue
        actual = truth[target]
        row = out.setdefault(lead, {"n": 0, "applied": 0, "mae_without": 0.0, "mae_with": 0.0})
        row["n"] += 1
        row["applied"] += any(adjustment != 0.0 for _mu, adjustment in pairs)
        row["mae_without"] += sum(abs(mu - adj - actual) for mu, adj in pairs) / len(pairs)
        row["mae_with"] += sum(abs(mu - actual) for mu, _adj in pairs) / len(pairs)
    for row in out.values():
        row["mae_without"] /= row["n"]
        row["mae_with"] /= row["n"]
    return out


def print_apple_score(
    conn: sqlite3.Connection, key: str = "apple_adjustment_f", label: str = "apple"
) -> None:
    scores = apple_score(conn, key)
    if not scores:
        print(f"{label} score: no settled {label}-adjusted forecasts yet")
    for lead, row in sorted(scores.items()):
        print(
            f"{label} score lead {lead}: n={row['n']} applied={row['applied']} "
            f"MAE without={row['mae_without']:.2f}F with={row['mae_with']:.2f}F"
        )


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
    parser.add_argument(
        "--apple-score",
        action="store_true",
        help="print settled MAE of live forecasts with vs without the Apple nudge",
    )
    parser.add_argument(
        "--carry-score",
        action="store_true",
        help="print settled MAE of live forecasts with vs without the evening carry-over",
    )
    args = parser.parse_args(argv)
    if args.apple_score or args.carry_score:
        with sqlite3.connect(args.db, timeout=30) as conn:
            if args.apple_score:
                print_apple_score(conn)
            if args.carry_score:
                print_apple_score(conn, "carry_adjustment_f", "carry")
        return 0
    if not (args.backfill or args.serve or args.serve_rolling):
        parser.error("nothing to do; pass --backfill, --serve, or --serve-rolling")

    cities = parse_city_slugs(args.cities)
    # Onboarding backfills run beside the 30-minute refresh and the nightly
    # dataset chain: wait out a concurrent writer instead of failing after
    # sqlite3's 5 s default.
    with sqlite3.connect(args.db, timeout=30) as conn:
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
        # Freshly registered cities are excluded from the serve's health
        # accounting until their onboarding backfill exists. Without this, a
        # registry expansion turned every scheduled sfo-forecaster-refresh tick
        # red (exit 1 -> OnFailure alert, 38x/day) for the ~60 days the nightly
        # 5-day NWP window needs to reach EMOS_MIN_TRAIN, while the other
        # cities' rows were written normally.
        awaiting: list[str] = []
        # Apple is fetched in-process (its data expires within about an hour, so
        # a separate refresh cache is empty most of the time). A short timeout and
        # a total time budget keep the serve inside its 300 s subprocess deadline.
        apple_client = (
            apple_weatherkit.client_from_env(max_timeout_seconds=5)
            if args.serve or args.serve_rolling
            else None
        )
        apple_spent = 0.0  # only time inside Apple calls counts, not NWP or fitting
        apple_cities = 0
        for city in cities:
            if args.serve or args.serve_rolling:
                pending = awaiting_onboarding(conn, city)
                if pending is not None:
                    awaiting.append(city.slug)
                    print(
                        f"live EMOS [{city.slug}] awaiting onboarding backfill: "
                        f"{pending}/{EMOS_MIN_TRAIN} scored lead-1 days and no EMOS "
                        "row of any source; excluded from serve health "
                        "(trading/deploy/aws/README.md, 'Adding A City')"
                    )
                    continue
            today = _settlement_today(city)
            # Serve each target at its TRUE lead so the EMOS fit's per-model
            # biases match the forecast horizon (next-day -> lead 1, 2-day-out
            # -> lead 2). The NWP archive only holds leads >= 1, so the
            # same-day target (lead 0) has no training history of its own; it
            # is served with the lead-1 fit (per-model biases/weights) applied
            # to the CURRENT-run forecast for today, with the borrowed
            # dispersion rescaled to the same-day horizon, stored at
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
            live_models_by_target = (
                fetch_live_model_forecasts_multi(city=city)
                if any(lead >= 0 for _, lead in serve_targets)
                else {}
            )
            apple_highs: dict[str, float] = {}
            if apple_client is not None and apple_spent < APPLE_TIME_BUDGET_SECONDS:
                apple_started = time.monotonic()
                try:
                    apple_highs = apple_weatherkit.live_highs(apple_client, city)
                    apple_cities += bool(apple_highs)
                except apple_weatherkit.WeatherKitError:
                    print(f"apple weather [{city.slug}]: unavailable, serving without it")
                apple_spent += time.monotonic() - apple_started

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
                        live_models=live_models_by_target.get(target, {}),
                        apple_high_f=apple_highs.get(target.isoformat()),
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
                f"cities={len(cities)} leads=0..{ROLLING_SERVE_DAYS - 1} "
                f"awaiting={len(awaiting)} apple_cities={apple_cities}"
            )
        # A scheduled serve is only healthy when every requested city/target
        # was refreshed. Partial coverage is still a forecast outage: a single
        # successful row must not hide dozens of missing live distributions.
        # Cities awaiting onboarding are not targets, but serving NOTHING is
        # never healthy either: a DB wiped of every station's history must not
        # pass as "all cities awaiting onboarding".
        if (args.serve or args.serve_rolling) and (
            served != total_targets or total_targets == 0
        ):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
