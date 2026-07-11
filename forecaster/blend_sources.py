#!/usr/bin/env python3
"""Public forecast sources and final SFO blend orchestration."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from blend_archive import latest_scored_blend_rows, table_columns, table_exists
from blend_learners import (
    apply_source_mos,
    blend_bias_for,
    cap_magnitude,
    compute_adaptive_blend_weights,
    compute_rolling_blend_residual_bias,
    compute_source_mos_corrections,
    normalize_weights,
    predicted_temperature_cohort,
)
from google_api import (
    finite,
    google_daily_api_high_for,
    load_usage,
    parse_google_timestamp,
    read_json,
    settlement_today_iso,
    target_date,
)
from settlement_calendar import (
    integer_settlement_high_f,
    local_standard_date,
    utc_window_for_local_standard_date,
)
from weather_cache_config import (
    AIRPORT_STATIONS,
    BLEND_WEIGHTS,
    DATASET_GUIDANCE_WEIGHT,
    DB_PATH,
    DURATION_RE,
    ENABLE_GOOGLE_CURRENT_CONDITIONS,
    ENABLE_GOOGLE_DAILY_FORECAST,
    FORECAST_DATA_PATH,
    FRESH_OBSERVATION_MINUTES,
    GOOGLE_DAILY_DISAGREEMENT_WARN_F,
    GOOGLE_DAILY_INTERNAL_WEIGHT,
    GOOGLE_WEATHER_MONTHLY_FREE_CAP,
    NWS_API_URL,
    NWS_USER_AGENT,
    OPEN_METEO_API_URL,
    SFO_POINT,
    SOURCE_MOS_CAP_F,
)


def adaptive_blend_weights():
    """Compatibility adapter: acquire scored rows, then run the pure learner."""
    cached = getattr(adaptive_blend_weights, "_cached", None)
    if cached is not None:
        return cached
    result = compute_adaptive_blend_weights(latest_scored_blend_rows())
    adaptive_blend_weights._cached = result
    return result


def source_mos_corrections():
    """Fail-open compatibility adapter around the pure source-MOS learner."""
    cached = getattr(source_mos_corrections, "_cached", None)
    if cached is not None:
        return cached
    try:
        result = compute_source_mos_corrections(latest_scored_blend_rows())
    except Exception as exc:
        result = (
            {},
            {
                "mode": "disabled",
                "reason": f"source MOS correction failed: {type(exc).__name__}: {exc}",
                "scored_days": 0,
                "cap_f": SOURCE_MOS_CAP_F,
                "eligibility": "clean next-day scored blend rows only",
            },
        )
    source_mos_corrections._cached = result
    return result


def rolling_blend_residual_bias():
    """Compatibility adapter for the pure rolling residual learner."""
    cached = getattr(rolling_blend_residual_bias, "_cached", None)
    if cached is not None:
        return cached
    result = compute_rolling_blend_residual_bias(latest_scored_blend_rows())
    rolling_blend_residual_bias._cached = result
    return result


def read_nws_json(url):
    request = Request(url, headers={"User-Agent": NWS_USER_AGENT})
    with urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def read_public_json(url):
    with urlopen(url, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_iso_duration(raw):
    match = DURATION_RE.match(raw or "PT1H")
    if not match:
        return timedelta(hours=1)
    days = int(match.group(1) or 0)
    hours = int(match.group(2) or 0)
    minutes = int(match.group(3) or 0)
    return timedelta(days=days, hours=hours, minutes=minutes)


def target_window_utc(target_iso):
    return utc_window_for_local_standard_date(target_iso)


def interval_touches_date(valid_time, target_iso):
    start_raw, _, duration_raw = valid_time.partition("/")
    start = parse_google_timestamp(start_raw)
    if not start:
        return False
    end = start + parse_iso_duration(duration_raw)
    target_start, target_end = target_window_utc(target_iso)
    return start.astimezone(timezone.utc) < target_end and end.astimezone(timezone.utc) > target_start


def nws_value_to_f(value, unit):
    if value is None:
        return None
    value = float(value)
    return value * 9 / 5 + 32 if "degC" in str(unit) else value


def load_nws_forecast_high(target_iso):
    point_url = f"{NWS_API_URL}/points/{SFO_POINT['lat']:.4f},{SFO_POINT['lon']:.4f}"
    point = read_nws_json(point_url)
    props = point.get("properties") or {}

    if props.get("forecastGridData"):
        grid = read_nws_json(props["forecastGridData"])
        layer = (grid.get("properties") or {}).get("maxTemperature") or {}
        highs = [
            nws_value_to_f(row.get("value"), layer.get("uom"))
            for row in layer.get("values") or []
            if interval_touches_date(row.get("validTime", ""), target_iso)
        ]
        highs = [value for value in highs if value is not None]
        if highs:
            return {
                "highF": round(max(highs), 2),
                "source": "NWS forecastGridData maxTemperature",
                "detail": props.get("gridId")
                and f"{props['gridId']} grid {props.get('gridX')},{props.get('gridY')}",
            }

    if props.get("forecastHourly"):
        hourly = read_nws_json(props["forecastHourly"])
        highs = []
        for period in (hourly.get("properties") or {}).get("periods") or []:
            start = parse_google_timestamp(period.get("startTime"))
            if not start or local_standard_date(start).isoformat() != target_iso:
                continue
            unit = period.get("temperatureUnit")
            temp = float(period["temperature"])
            highs.append(temp * 9 / 5 + 32 if unit == "C" else temp)
        if highs:
            return {
                "highF": round(max(highs), 2),
                "source": "NWS hourly forecast",
                "detail": "Hourly forecast fallback",
            }

    return {"highF": None, "source": "NWS", "error": "NWS forecast did not include target high"}


def load_open_meteo_forecast_high(target_iso):
    params = urlencode(
        {
            "latitude": f"{SFO_POINT['lat']:.4f}",
            "longitude": f"{SFO_POINT['lon']:.4f}",
            "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit",
            # Fixed PST (IANA POSIX sign) so the daily max covers the same
            # window as the NWS/Kalshi settlement day.
            "timezone": "Etc/GMT+8",
            "forecast_days": "4",
        }
    )
    data = read_public_json(f"{OPEN_METEO_API_URL}?{params}")
    dates = (data.get("daily") or {}).get("time") or []
    highs = (data.get("daily") or {}).get("temperature_2m_max") or []
    if target_iso in dates:
        value = highs[dates.index(target_iso)]
        if value is not None:
            return {
                "highF": round(float(value), 2),
                "source": "Open-Meteo daily forecast",
                "detail": "SFO coordinate daily high",
            }
    return {"highF": None, "source": "Open-Meteo", "error": "Open-Meteo did not include target high"}


def load_history_high(target_iso):
    if not FORECAST_DATA_PATH.exists():
        return {"highF": None, "source": "SFO history", "error": "forecast_data.json missing"}
    data = read_json(FORECAST_DATA_PATH, {})
    row = (data.get("table") or {}).get(target_iso[5:])
    if not row or row.get("mean") is None:
        return {"highF": None, "source": "SFO history", "error": "No climatology row"}
    return {
        "highF": round(float(row["mean"]), 2),
        "source": "SFO historical climatology",
        "detail": f"{data.get('n_years')} years, {row.get('n')} nearby-date samples",
    }


def load_promoted_dataset_guidance(target_iso, db_path=None, research_path=None):
    """Latest promoted compact dataset high-temperature feature for a target day."""

    raw_db_path = str(db_path) if db_path is not None else os.getenv("SFO_DATASET_DB")
    resolved_db = Path(raw_db_path) if raw_db_path else None
    resolved_research = (
        Path(research_path)
        if research_path is not None
        else Path(os.getenv("SFO_DATASET_RESEARCH_PATH", "dataset_research.json"))
    )
    metadata = {
        "mode": "collect_only",
        "db_path": str(resolved_db) if resolved_db is not None else None,
        "research_path": str(resolved_research),
        "promoted_count": 0,
        "available_unpromoted_count": 0,
    }
    if resolved_db is None or not resolved_db.exists():
        metadata["reason"] = "dataset DB is unavailable"
        return {"highF": None, "source": "Promoted dataset guidance", "metadata": metadata, "components": []}

    promoted_keys = _promoted_dataset_keys(resolved_research)
    metadata["promoted_count"] = len(promoted_keys)
    rows = _latest_dataset_feature_rows(resolved_db, target_iso)
    metadata["available_unpromoted_count"] = sum(
        1 for row in rows if _dataset_feature_key(row) not in promoted_keys
    )
    if not promoted_keys:
        metadata["reason"] = "no dataset source has passed the accuracy gate"
        return {"highF": None, "source": "Promoted dataset guidance", "metadata": metadata, "components": []}

    corrections = _dataset_guidance_corrections(resolved_db, target_iso, promoted_keys)
    components = []
    for row in rows:
        key = _dataset_feature_key(row)
        if key not in promoted_keys:
            continue
        correction = _dataset_correction_for(row, corrections)
        corrected = float(row["value"]) + correction
        components.append(
            {
                "dataset_key": key,
                "raw_high_f": round(float(row["value"]), 2),
                "correction_f": round(correction, 3),
                "corrected_high_f": round(corrected, 2),
                "issued_at": row["issued_at"],
                "source_url": row["source_url"],
            }
        )
    if not components:
        metadata["reason"] = "no promoted dataset feature matched the target date"
        return {"highF": None, "source": "Promoted dataset guidance", "metadata": metadata, "components": []}

    high = sum(component["corrected_high_f"] for component in components) / len(components)
    metadata.update(
        {
            "mode": "promoted",
            "matched_promoted_count": len(components),
            "correction": corrections.get("metadata", {}),
        }
    )
    return {
        "highF": round(high, 2),
        "source": "Promoted dataset guidance",
        "detail": ", ".join(component["dataset_key"] for component in components),
        "components": components,
        "metadata": metadata,
    }


def _promoted_dataset_keys(research_path):
    if not research_path.exists():
        return set()
    payload = read_json(research_path, {})
    rows = ((payload.get("accuracy_gate") or {}).get("candidates") or [])
    return {
        row.get("dataset_key")
        for row in rows
        if row.get("decision") == "accuracy_candidate" and row.get("dataset_key")
    }


def _latest_dataset_feature_rows(db_path, target_iso):
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            if not table_exists(conn, "dataset_forecast_features"):
                return []
            rows = conn.execute(
                """
                SELECT source, model, variable, lead_hours, target_date, value,
                       issued_at, valid_time, units, source_url
                FROM dataset_forecast_features
                WHERE target_date = ?
                  AND value IS NOT NULL
                  AND variable LIKE '%temperature_2m_max%'
                ORDER BY source, model, variable, lead_hours, target_date, issued_at
                """,
                (target_iso,),
            ).fetchall()
    except sqlite3.Error:
        return []

    latest = {}
    for row in rows:
        key = _dataset_feature_key(row)
        current = latest.get(key)
        if current is None or str(row["issued_at"]) > str(current["issued_at"]):
            latest[key] = row
    return list(latest.values())


def _dataset_feature_key(row):
    lead = row["lead_hours"] if "lead_hours" in row.keys() else None
    lead_token = "none" if lead is None else f"{float(lead):g}h"
    return f"{row['source']}/{row['model']}/{row['variable']}/{lead_token}"


def _dataset_guidance_corrections(db_path, target_iso, promoted_keys):
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            if not (
                table_exists(conn, "dataset_forecast_features")
                and (
                    table_exists(conn, "cli_settlements")
                    or table_exists(conn, "clisfo_settlements")
                )
            ):
                return {"metadata": {"mode": "disabled", "reason": "no CLI settlement table"}}
            if table_exists(conn, "cli_settlements"):
                final_join = (
                    " AND c.is_final = 1"
                    if "is_final" in table_columns(conn, "cli_settlements")
                    else ""
                )
                join_clause = (
                    "JOIN cli_settlements c ON c.local_date = f.target_date "
                    f"AND c.station_id = 'KSFO'{final_join}"
                )
            else:
                join_clause = "JOIN clisfo_settlements c ON c.local_date = f.target_date"
            rows = conn.execute(
                f"""
                SELECT f.source, f.model, f.variable, f.lead_hours, f.value, c.max_temperature_f
                FROM dataset_forecast_features f
                {join_clause}
                WHERE f.target_date < ?
                  AND f.value IS NOT NULL
                  AND c.max_temperature_f IS NOT NULL
                  AND f.variable LIKE '%temperature_2m_max%'
                """,
                (target_iso,),
            ).fetchall()
    except sqlite3.Error:
        return {"metadata": {"mode": "disabled", "reason": "dataset correction query failed"}}

    residuals = {}
    for row in rows:
        key = _dataset_feature_key(row)
        if key not in promoted_keys:
            continue
        actual = integer_settlement_high_f(row["max_temperature_f"])
        if actual is None or not finite(row["value"]):
            continue
        residuals.setdefault(key, []).append(float(actual) - float(row["value"]))
    corrections = {}
    for key, values in residuals.items():
        if len(values) >= 10:
            corrections[key] = cap_magnitude(sum(values) / len(values), SOURCE_MOS_CAP_F)
    return {
        "metadata": {
            "mode": "dataset_source_mos" if corrections else "disabled",
            "source_counts": {key: len(values) for key, values in residuals.items()},
            "cap_f": SOURCE_MOS_CAP_F,
        },
        "corrections": corrections,
    }


def _dataset_correction_for(row, corrections):
    key = _dataset_feature_key(row)
    return cap_magnitude(
        float((corrections.get("corrections") or {}).get(key, 0.0)),
        SOURCE_MOS_CAP_F,
    )


def load_station_observation(station_id):
    data = read_nws_json(f"{NWS_API_URL}/stations/{station_id}/observations/latest")
    props = data.get("properties") or {}
    temp = props.get("temperature") or {}
    observed_at = parse_google_timestamp(props.get("timestamp"))
    return {
        "station_id": station_id,
        "temp_f": nws_value_to_f(temp.get("value"), temp.get("unitCode")),
        "observed_at": observed_at,
    }


def station_adjustment():
    observations = []
    for station_id in AIRPORT_STATIONS:
        try:
            observations.append(load_station_observation(station_id))
        except Exception:
            continue

    now = datetime.now(timezone.utc)
    fresh = []
    for obs in observations:
        observed_at = obs.get("observed_at")
        if not observed_at or not finite(obs.get("temp_f")):
            continue
        age_minutes = (now - observed_at.astimezone(timezone.utc)).total_seconds() / 60
        if 0 <= age_minutes <= FRESH_OBSERVATION_MINUTES:
            fresh.append(obs)

    sfo = next((obs for obs in fresh if obs["station_id"] == "KSFO"), None)
    neighbors = [obs for obs in fresh if obs["station_id"] != "KSFO"]
    if not sfo or not neighbors:
        return {"value": 0.0, "fresh_station_count": len(fresh), "detail": "No fresh SFO plus neighbor context"}

    neighbor_avg = sum(obs["temp_f"] for obs in neighbors) / len(neighbors)
    offset = sfo["temp_f"] - neighbor_avg
    value = max(-1.0, min(1.0, offset * 0.04))
    return {
        "value": round(value, 2),
        "fresh_station_count": len(fresh),
        "detail": f"SFO offset {offset:.1f}F against {len(neighbors)} neighbors",
    }


def safe_source(loader, fallback_name):
    try:
        return loader()
    except Exception as exc:
        return {"highF": None, "source": fallback_name, "error": type(exc).__name__}

def target_summary(summary, target_iso):
    for row in summary.get("daily_highs") or []:
        if row.get("target_date") == target_iso:
            return row
    return summary if summary.get("target_date") == target_iso else None


def build_blend_snapshot(summary, target_iso):
    google_row = target_summary(summary, target_iso)
    if not google_row or not finite(google_row.get("highF")):
        return None

    fetched_at = google_row.get("fetched_at") or summary.get("fetched_at")
    google_hourly_high = float(google_row["highF"])
    google_daily_row = google_daily_api_high_for(summary, target_iso)
    google_daily_high = (
        float(google_daily_row["highF"])
        if google_daily_row and finite(google_daily_row.get("highF"))
        else None
    )
    google_internal_weight = (
        max(0.0, min(0.40, GOOGLE_DAILY_INTERNAL_WEIGHT))
        if google_daily_high is not None
        else 0.0
    )
    google_composite_high = (
        google_hourly_high * (1 - google_internal_weight)
        + google_daily_high * google_internal_weight
        if google_daily_high is not None
        else google_hourly_high
    )
    google_internal_gap = (
        round(google_daily_high - google_hourly_high, 2)
        if google_daily_high is not None
        else None
    )
    google_components = {
        "hourly_local_day_high_f": round(google_hourly_high, 2),
        "daily_endpoint_high_f": round(google_daily_high, 2) if google_daily_high is not None else None,
        "daily_internal_weight": google_internal_weight,
        "daily_minus_hourly_gap_f": google_internal_gap,
        "gap_warning_f": GOOGLE_DAILY_DISAGREEMENT_WARN_F,
        "current_conditions": summary.get("google_current_conditions"),
        "weather_events_used": summary.get("google_weather_events_used"),
    }
    google_detail = google_row.get("peak_hour_local")
    if google_daily_high is not None:
        google_detail = (
            f"{google_row.get('peak_hour_local')}; "
            f"daily endpoint {google_daily_high:.1f}F"
        )
    sources = {
        "google": {
            "highF": round(google_composite_high, 2),
            "lockHighF": round(max(google_hourly_high, google_daily_high or google_hourly_high), 2),
            "source": "Google Weather API forecast.hours + forecast.days",
            "detail": google_detail,
            "components": google_components,
            "warning": (
                f"Google hourly/daily gap {abs(google_internal_gap):.1f}F exceeds "
                f"{GOOGLE_DAILY_DISAGREEMENT_WARN_F:.1f}F"
                if google_internal_gap is not None
                and abs(google_internal_gap) > GOOGLE_DAILY_DISAGREEMENT_WARN_F
                else None
            ),
        },
        "nws": safe_source(lambda: load_nws_forecast_high(target_iso), "NWS"),
        "open_meteo": safe_source(lambda: load_open_meteo_forecast_high(target_iso), "Open-Meteo"),
        "history": load_history_high(target_iso),
    }
    dataset_guidance = load_promoted_dataset_guidance(target_iso)
    if finite(dataset_guidance.get("highF")):
        sources["dataset"] = {
            "highF": dataset_guidance["highF"],
            "lockHighF": dataset_guidance["highF"],
            "source": dataset_guidance.get("source"),
            "detail": dataset_guidance.get("detail"),
            "components": dataset_guidance.get("components", []),
        }
    adjustment = station_adjustment()
    blend_weights, weight_metadata = adaptive_blend_weights()
    if "dataset" in sources:
        blend_weights = normalize_weights({**blend_weights, "dataset": DATASET_GUIDANCE_WEIGHT})
        weight_metadata = {
            **weight_metadata,
            "dataset_guidance_weight": DATASET_GUIDANCE_WEIGHT,
            "weights": {key: round(value, 4) for key, value in blend_weights.items()},
        }
    source_mos_table, source_mos_metadata = source_mos_corrections()
    effective_sources, source_mos_report = apply_source_mos(sources, source_mos_table)

    available = {
        key: row
        for key, row in effective_sources.items()
        if finite(row.get("highF")) and blend_weights.get(key, 0) > 0
    }
    if not available:
        return None

    total_weight = sum(blend_weights[key] for key in available)
    normalized_weights = {
        key: blend_weights[key] / total_weight
        for key in effective_sources
        if key in available
    }
    weighted_high = sum(effective_sources[key]["highF"] * normalized_weights[key] for key in available)
    raw_predicted = weighted_high + adjustment["value"]
    bias_table, bias_metadata = rolling_blend_residual_bias()
    bias_value = blend_bias_for(raw_predicted, bias_table)
    # Apply the de-bias before the observed-high decision so a same-day lock/floor
    # still operates on the calibrated number.
    calibrated_predicted = raw_predicted + bias_value
    observed_decision = observed_high_decision(target_iso, effective_sources)
    predicted = calibrated_predicted
    method = "weighted Google + NWS + Open-Meteo + SFO history with capped station adjustment"
    if bias_value:
        method += " and rolling residual de-bias"
    if observed_decision:
        observed_high = observed_decision["highF"]
        if observed_decision["mode"] == "lock":
            predicted = observed_high
            method = f"official NWS observed high ({observed_decision['reason']})"
        else:
            predicted = max(calibrated_predicted, observed_high)
            method = "weighted blend floored by NWS observed high-so-far"

    return {
        "fetched_at": fetched_at,
        "target_date": target_iso,
        "lead_hours": google_row.get("lead_hours"),
        "method": method,
        "predicted_high_f": round(predicted, 2),
        "google_high_f": sources["google"].get("highF"),
        "nws_high_f": sources["nws"].get("highF"),
        "open_meteo_high_f": sources["open_meteo"].get("highF"),
        "history_high_f": sources["history"].get("highF"),
        "google_weight": round(normalized_weights.get("google", 0), 4),
        "nws_weight": round(normalized_weights.get("nws", 0), 4),
        "open_meteo_weight": round(normalized_weights.get("open_meteo", 0), 4),
        "history_weight": round(normalized_weights.get("history", 0), 4),
        "station_adjustment_f": adjustment["value"],
        "fresh_station_count": adjustment["fresh_station_count"],
        "source_count": len(available),
        "time_zone": google_row.get("time_zone") or summary.get("time_zone"),
        "max_calls_per_day": google_row.get("max_calls_per_day") or summary.get("max_calls_per_day"),
        "calls_used_today": google_row.get("calls_used_today") or summary.get("calls_used_today"),
        "details": {
            "sources": sources,
            "google_weather_api": {
                "monthly_free_cap": GOOGLE_WEATHER_MONTHLY_FREE_CAP,
                "monthly_event_budget": summary.get("max_google_events_per_month"),
                "monthly_events_used": summary.get("google_events_used_month"),
                "daily_event_budget": google_row.get("max_calls_per_day"),
                "daily_events_used": google_row.get("calls_used_today"),
                "refreshes_today": summary.get("google_refreshes_today"),
                "enabled_daily_forecast": ENABLE_GOOGLE_DAILY_FORECAST,
                "enabled_current_conditions": ENABLE_GOOGLE_CURRENT_CONDITIONS,
            },
            "station_adjustment": adjustment,
            "base_weights": BLEND_WEIGHTS,
            "blend_weighting": weight_metadata,
            "source_mos": source_mos_report,
            "dataset_sources": dataset_guidance,
            "postprocessor_metadata": {
                "source_mos": source_mos_metadata,
                "rolling_bias": bias_metadata,
            },
            "raw_weighted_prediction_f": round(raw_predicted, 2),
            "calibrated_prediction_f": round(calibrated_predicted, 2),
            "rolling_bias": {
                "value": round(bias_value, 3),
                "cohort": predicted_temperature_cohort(raw_predicted),
                "metadata": bias_metadata,
            },
            "observed_high_decision": observed_decision,
        },
    }


def load_nws_observed_high(target_iso):
    if not DB_PATH.exists():
        return None
    try:
        with sqlite3.connect(DB_PATH) as conn:
            if not table_exists(conn, "nws_daily_high_ground_truth"):
                return None
            row = conn.execute(
                """
                SELECT high_f,
                       high_observed_at,
                       observation_count,
                       is_complete,
                       updated_at
                FROM nws_daily_high_ground_truth
                WHERE station_id = 'KSFO'
                  AND local_date = ?
                  AND high_f IS NOT NULL
                """,
                (target_iso,),
            ).fetchone()
    except sqlite3.Error:
        return None

    if not row:
        return None
    return {
        "highF": round(float(row[0]), 2),
        "high_observed_at": row[1],
        "observation_count": row[2],
        "is_complete": bool(row[3]),
        "updated_at": row[4],
        "source": "NWS KSFO observed daily high",
    }


def observed_high_decision(target_iso, sources):
    # Same-day check on the settlement clock, so the observed-high lock/floor
    # applies to the right Kalshi day during the DST 00:00-01:00 window.
    if target_iso != settlement_today_iso():
        return None

    observed = load_nws_observed_high(target_iso)
    if not observed or not finite(observed.get("highF")):
        return None

    live_values = [
        row.get("lockHighF", row.get("highF"))
        for key, row in sources.items()
        if key != "history" and finite(row.get("lockHighF", row.get("highF")))
    ]
    max_live_forecast = max(live_values) if live_values else None

    decision = dict(observed)
    decision["max_live_forecast_f"] = (
        round(float(max_live_forecast), 2)
        if max_live_forecast is not None
        else None
    )

    if observed["is_complete"]:
        decision.update(
            {
                "mode": "lock",
                "reason": "completed local day",
            }
        )
        return decision

    if max_live_forecast is not None and observed["highF"] >= max_live_forecast - 0.25:
        decision.update(
            {
                "mode": "lock",
                "reason": "NWS high-so-far meets or exceeds live forecast highs",
            }
        )
        return decision

    decision.update(
        {
            "mode": "floor",
            "reason": "same-day forecast cannot go below observed KSFO high-so-far",
        }
    )
    return decision


def blend_targets(summary, primary_target_iso):
    targets = []
    today = settlement_today_iso()
    for row in summary.get("daily_highs") or []:
        target_iso = row.get("target_date")
        if target_iso in {today, primary_target_iso} and target_iso not in targets:
            targets.append(target_iso)
    if primary_target_iso not in targets:
        targets.append(primary_target_iso)
    return targets


def unavailable(reason):
    usage = load_usage()
    return {
        "available": False,
        "reason": reason,
        "target_date": target_date(),
        "max_calls_per_day": usage.get("daily_event_budget"),
        "calls_used_today": usage.get("daily_events"),
        "max_google_events_per_month": usage.get("monthly_event_budget"),
        "google_events_used_month": usage.get("monthly_events"),
        "fetched_at": None,
    }
