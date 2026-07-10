"""Serve-time trailing recalibration for live EMOS forecasts.

The rolling-origin EMOS archive is the honest, uncorrected training/eval
record. This module layers a thin, leakage-safe post-process on the LIVE serve
only: a per-station-per-lead trailing bias subtraction and a trailing sigma
dispersion rescale, both estimated from the trailing window of SCORED
rolling-origin rows.

Why: the 30-day scoreboard shows persistent per-city mean errors the global
EMOS affine fit is too slow to absorb (e.g. NYC/CHI/LAX running +1.0..+1.5F
warm), and coverage says several cities are over-dispersed. A short trailing
window tracks these regime offsets; shrinkage toward the no-op keeps a thin or
noisy window from over-steering.

Leakage discipline mirrors the rest of the forecaster:

* The window for a forecast served on day S uses only rows whose target date is
  strictly before S (their CLI truth is published by S), never S itself.
* Truth is joined from ``cli_settlements`` at read time rather than trusting
  the archive's ``actual_high_f`` column, so a stale rescore cannot skew the
  window.
* Rolling-origin rows are never modified: they stay the uncorrected record the
  correction window itself is computed from, so the estimate cannot feed back
  into itself.

Validated by ``recalibration_replay.py`` (rolling-origin replay over real
archived data) before shipping; unit-tested in
``tests/test_emos_recalibration.py``. Pure standard library.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import fmean, stdev

# Trailing window length (days of scored rolling-origin history) for both the
# bias and dispersion estimates. ~45 days is long enough to average over
# synoptic noise but short enough to track a seasonal regime shift.
TRAILING_WINDOW_DAYS = 45

# Shrinkage constant: estimates are scaled by n / (n + K), so a thin window
# leans toward the no-op (bias 0, factor 1) and a full 45-day window keeps
# ~82% of the raw estimate.
SHRINKAGE_K = 10.0

# The sigma rescale is clipped so a pathological window can neither collapse
# the distribution (overconfidence) nor blow it up (uninformative).
SIGMA_FACTOR_FLOOR = 0.75
SIGMA_FACTOR_CEIL = 1.5

# Significance deadband (soft threshold) on the bias correction: only the part
# of the trailing mean error beyond BIAS_DEADBAND_T standard errors is
# corrected. Replay evidence (recalibration_replay.py, 60-day rolling-origin
# replay on real archive data): a plain shrunk trailing mean helps cities with
# a genuine persistent offset (NYC/CHI/LAX/AUS, +1.0..+1.5F) but *hurts*
# cities whose small bias flips sign mid-season (ATL/DAL/SEA/OKC), because a
# trailing estimate is systematically wrong-signed right after a regime flip.
# The soft threshold keeps the large, statistically solid corrections and
# zeroes the noise-chasing ones.
BIAS_DEADBAND_T = 1.5


@dataclass(frozen=True)
class Correction:
    """A serve-time (mu, sigma) correction; the default is an exact no-op."""

    bias_f: float = 0.0  # shrunk trailing mean(mu - truth); subtracted from mu
    sigma_factor: float = 1.0  # shrunk+clipped trailing dispersion multiplier
    n_window: int = 0

    def apply(self, mu: float, sigma: float) -> tuple[float, float]:
        return mu - self.bias_f, sigma * self.sigma_factor


def load_scored_series(
    conn: sqlite3.Connection,
    station_id: str,
    lead_days: int,
    *,
    source: str = "rolling_origin",
) -> list[tuple[date, float, float, float]]:
    """Chronological (target_date, mu, sigma, truth) for one station x lead.

    Truth comes from ``cli_settlements`` (the settlement instrument) joined at
    read time. Missing tables degrade to an empty series -- the correction then
    becomes a no-op rather than an error, matching the serve path's fail-soft
    contract.
    """

    for table in ("forecast_emos_daily_high", "cli_settlements"):
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None:
            return []
    rows = conn.execute(
        """
        SELECT f.target_date, f.predicted_high_f, f.sigma_f, c.max_temperature_f
        FROM forecast_emos_daily_high AS f
        JOIN cli_settlements AS c
          ON c.station_id = f.station_id AND c.local_date = f.target_date
        WHERE f.station_id = ? AND f.lead_days = ? AND f.source = ?
          AND c.max_temperature_f IS NOT NULL
        ORDER BY f.target_date
        """,
        (station_id, lead_days, source),
    ).fetchall()
    return [
        (date.fromisoformat(target), float(mu), float(sigma), float(truth))
        for target, mu, sigma, truth in rows
    ]


def window_rows(
    series: list[tuple[date, float, float, float]],
    serve_date: date,
    *,
    window_days: int = TRAILING_WINDOW_DAYS,
) -> list[tuple[float, float, float]]:
    """(mu, sigma, truth) rows inside the trailing window for a serve day.

    A forecast served on ``serve_date`` may use targets in
    [serve_date - window_days, serve_date - 1]: truth for serve_date itself (or
    later) is not published at serve time, so including it would leak.
    """

    start = serve_date - timedelta(days=window_days)
    end = serve_date - timedelta(days=1)
    return [
        (mu, sigma, truth)
        for target, mu, sigma, truth in series
        if start <= target <= end
    ]


def compute_correction(
    rows: list[tuple[float, float, float]],
    *,
    k: float = SHRINKAGE_K,
    apply_bias: bool = True,
    apply_sigma: bool = True,
    sigma_factor_floor: float = SIGMA_FACTOR_FLOOR,
    sigma_factor_ceil: float = SIGMA_FACTOR_CEIL,
    sigma_net_of_bias: bool = True,
    bias_deadband_t: float = BIAS_DEADBAND_T,
) -> Correction:
    """Estimate the trailing correction from (mu, sigma, truth) window rows.

    Bias: soft-thresholded, shrunk trailing mean forecast error. With m =
    mean(mu - truth) and se = stdev(errors)/sqrt(n), only the excess beyond
    the significance deadband is corrected::

        bias = n/(n+k) * sign(m) * max(0, |m| - bias_deadband_t * se)

    and the result is subtracted from the served mu. A window whose mean error
    is indistinguishable from noise therefore corrects nothing.

    Sigma: the served sigma is scaled by sqrt(mean(z^2)) where z is the
    window's standardised residual -- computed net of the bias correction
    actually being applied (``sigma_net_of_bias``), because after debiasing the
    forecast no longer pays the bias^2 term. The factor is shrunk toward 1 by
    the same n/(n+k) and clipped to [floor, ceil].

    An empty window (or all-degenerate sigmas) returns the exact no-op.
    """

    n = len(rows)
    if n == 0 or not (apply_bias or apply_sigma):
        return Correction()
    shrink = n / (n + k)

    bias = 0.0
    if apply_bias and n >= 3:
        errors = [mu - truth for mu, _sigma, truth in rows]
        raw_bias = fmean(errors)
        stderr = stdev(errors) / math.sqrt(n)
        excess = max(0.0, abs(raw_bias) - bias_deadband_t * stderr)
        bias = shrink * math.copysign(excess, raw_bias)

    factor = 1.0
    if apply_sigma:
        applied_bias = bias if sigma_net_of_bias else 0.0
        z_sq = [
            ((truth - (mu - applied_bias)) / sigma) ** 2
            for mu, sigma, truth in rows
            if sigma > 0.0
        ]
        if z_sq:
            raw_factor = math.sqrt(fmean(z_sq))
            factor = 1.0 + shrink * (raw_factor - 1.0)
            factor = min(max(factor, sigma_factor_floor), sigma_factor_ceil)

    return Correction(bias_f=bias, sigma_factor=factor, n_window=n)


def correction_for_serve(
    conn: sqlite3.Connection,
    station_id: str,
    window_lead_days: int,
    serve_date: date,
    *,
    window_days: int = TRAILING_WINDOW_DAYS,
    k: float = SHRINKAGE_K,
    apply_bias: bool = True,
    apply_sigma: bool = True,
    bias_deadband_t: float = BIAS_DEADBAND_T,
) -> Correction:
    """Trailing correction for a live serve happening on ``serve_date``.

    ``window_lead_days`` is the lead of the rolling-origin history the window
    reads. The same-day (lead 0) serve has no rolling-origin record of its own
    and reuses the lead-1 window, mirroring its reuse of the lead-1 EMOS fit.
    """

    series = load_scored_series(conn, station_id, window_lead_days)
    rows = window_rows(series, serve_date, window_days=window_days)
    return compute_correction(
        rows,
        k=k,
        apply_bias=apply_bias,
        apply_sigma=apply_sigma,
        bias_deadband_t=bias_deadband_t,
    )
