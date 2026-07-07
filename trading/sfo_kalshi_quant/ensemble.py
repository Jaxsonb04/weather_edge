from __future__ import annotations

import json
import math
import statistics
from datetime import UTC, date, datetime
from typing import Any, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import EnsembleSnapshot
from .settlement_day import IANA_FIXED_PST


OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
KSFO_LATITUDE = 37.62
KSFO_LONGITUDE = -122.38
KSFO_ELEVATION_M = 5


class OpenMeteoEnsembleError(RuntimeError):
    pass


class SfoEnsembleClient:
    """Fetch GFS ensemble highs and align them to the SFO settlement forecast.

    Open-Meteo can snap the airport coordinates to very different nearby grid
    cells around the bay. The station-aligned forecast remains the center; this
    class uses ensemble members only for spread and bucket shape.
    """

    def __init__(
        self,
        *,
        timeout: float = 12.0,
        base_url: str = OPEN_METEO_ENSEMBLE_URL,
        cell_selections: tuple[str, ...] = ("nearest", "land"),
        city=None,
    ) -> None:
        # City geometry (default: the legacy KSFO cell). Elevation is only
        # meaningful where configured; Open-Meteo infers it otherwise.
        self.latitude = city.latitude if city is not None else KSFO_LATITUDE
        self.longitude = city.longitude if city is not None else KSFO_LONGITUDE
        self.elevation_m = KSFO_ELEVATION_M if city is None or city.slug == "sfo" else None
        self.settlement_tz = (
            city.settlement_tz_name if city is not None else IANA_FIXED_PST
        )
        self.timeout = timeout
        self.base_url = base_url
        self.cell_selections = cell_selections

    def station_aligned_snapshot(
        self,
        target: date,
        station_center_high_f: float,
    ) -> EnsembleSnapshot:
        candidates: list[EnsembleSnapshot] = []
        errors: list[str] = []
        for cell_selection in self.cell_selections:
            try:
                payload = self._fetch_payload(target, cell_selection)
                candidates.append(
                    parse_open_meteo_ensemble_payload(
                        payload,
                        target,
                        station_center_high_f,
                        cell_selection=cell_selection,
                    )
                )
            except Exception as exc:  # pragma: no cover - network errors vary by platform
                errors.append(f"{cell_selection}: {exc}")

        if not candidates:
            detail = "; ".join(errors) if errors else "no ensemble candidates returned"
            raise OpenMeteoEnsembleError(detail)

        return choose_station_aligned_candidate(candidates, station_center_high_f)

    def _fetch_payload(self, target: date, cell_selection: str) -> dict[str, Any]:
        params = {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "cell_selection": cell_selection,
            "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit",
            # The station's fixed standard time so the ensemble's daily max
            # covers the same window as NWS/Kalshi settlement, not the civil
            # calendar day.
            "timezone": self.settlement_tz,
            "start_date": target.isoformat(),
            "end_date": target.isoformat(),
            "models": "gfs_seamless",
        }
        if self.elevation_m is not None:
            params["elevation"] = self.elevation_m
        request = Request(f"{self.base_url}?{urlencode(params)}", headers={"accept": "application/json"})
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


def parse_open_meteo_ensemble_payload(
    payload: dict[str, Any],
    target: date,
    station_center_high_f: float,
    *,
    cell_selection: str,
) -> EnsembleSnapshot:
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        raise OpenMeteoEnsembleError("payload is missing daily ensemble data")

    times = daily.get("time")
    if not isinstance(times, list) or target.isoformat() not in times:
        raise OpenMeteoEnsembleError(f"payload has no daily row for {target.isoformat()}")
    day_idx = times.index(target.isoformat())

    raw_members = _daily_member_values(daily, day_idx)
    if len(raw_members) < 1:
        raise OpenMeteoEnsembleError("payload has no temperature_2m_max ensemble members")

    raw_mean = statistics.fmean(raw_members)
    station_bias = station_center_high_f - raw_mean
    station_members = tuple(value + station_bias for value in raw_members)
    warning = None
    if abs(station_bias) >= 4.0:
        warning = f"raw grid mean shifted by {station_bias:+.1f}F to match SFO station center"

    return EnsembleSnapshot(
        target_date=target,
        raw_member_highs_f=tuple(raw_members),
        station_member_highs_f=station_members,
        raw_mean_high_f=raw_mean,
        station_mean_high_f=statistics.fmean(station_members),
        raw_std_high_f=_pstdev(raw_members),
        station_std_high_f=_pstdev(station_members),
        station_bias_f=station_bias,
        grid_latitude=_maybe_float(payload.get("latitude")),
        grid_longitude=_maybe_float(payload.get("longitude")),
        grid_elevation_m=_maybe_float(payload.get("elevation")),
        cell_selection=cell_selection,
        fetched_at=datetime.now(UTC).isoformat(),
        warning=warning,
    )


def choose_station_aligned_candidate(
    candidates: Iterable[EnsembleSnapshot],
    station_center_high_f: float,
) -> EnsembleSnapshot:
    rows = list(candidates)
    if not rows:
        raise OpenMeteoEnsembleError("no ensemble candidates available")

    chosen = min(rows, key=lambda row: abs(row.raw_mean_high_f - station_center_high_f))
    warnings = [chosen.warning] if chosen.warning else []
    if len(rows) > 1:
        raw_means = [row.raw_mean_high_f for row in rows]
        span = max(raw_means) - min(raw_means)
        if span >= 6.0:
            labels = ", ".join(f"{row.cell_selection}={row.raw_mean_high_f:.1f}F" for row in rows)
            warnings.append(
                f"nearby Open-Meteo grid means differ by {span:.1f}F ({labels}); "
                f"using {chosen.cell_selection}"
            )
    if not warnings:
        return chosen
    return EnsembleSnapshot(
        target_date=chosen.target_date,
        raw_member_highs_f=chosen.raw_member_highs_f,
        station_member_highs_f=chosen.station_member_highs_f,
        raw_mean_high_f=chosen.raw_mean_high_f,
        station_mean_high_f=chosen.station_mean_high_f,
        raw_std_high_f=chosen.raw_std_high_f,
        station_std_high_f=chosen.station_std_high_f,
        station_bias_f=chosen.station_bias_f,
        grid_latitude=chosen.grid_latitude,
        grid_longitude=chosen.grid_longitude,
        grid_elevation_m=chosen.grid_elevation_m,
        cell_selection=chosen.cell_selection,
        fetched_at=chosen.fetched_at,
        source=chosen.source,
        warning="; ".join(warnings),
    )


def _daily_member_values(daily: dict[str, Any], day_idx: int) -> list[float]:
    control = _daily_value(daily, "temperature_2m_max", day_idx)
    member_keys = sorted(key for key in daily if key.startswith("temperature_2m_max_member"))
    values = [_daily_value(daily, key, day_idx) for key in member_keys]
    if control is not None and math.isfinite(control):
        values.insert(0, control)
    members = [value for value in values if value is not None and math.isfinite(value)]
    if members:
        return members
    return []


def _daily_value(daily: dict[str, Any], key: str, day_idx: int) -> float | None:
    series = daily.get(key)
    if not isinstance(series, list) or day_idx >= len(series):
        return None
    value = series[day_idx]
    if value is None:
        return None
    return float(value)


def _pstdev(values: Iterable[float]) -> float:
    rows = list(values)
    if len(rows) < 2:
        return 0.0
    return statistics.pstdev(rows)


def _maybe_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)
