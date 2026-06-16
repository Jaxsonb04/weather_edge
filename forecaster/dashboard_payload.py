from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from forecast_scoring import forecast_score_category, is_clean_next_day_forecast, parse_details_json


SFO_TZ = "America/Los_Angeles"
DB_PATH = Path("weather.db")


def load_json(path):
    return json.loads(Path(path).read_text())


def load_json_optional(path, default):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default


def load_daily_predictions(path, shift_days):
    import pandas as pd

    df = pd.read_csv(path, parse_dates=["timestamp"])
    local_time = df["timestamp"].dt.tz_convert(SFO_TZ)
    local_dates = pd.to_datetime(local_time.dt.date)
    df["forecast_date"] = (local_dates + pd.Timedelta(days=shift_days)).dt.date
    return (
        df.groupby("forecast_date", as_index=False)
        .agg(pred=("pred", "mean"), actual=("actual", "first"))
    )


def js(obj):
    return json.dumps(obj)


def table_exists(conn, table_name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def local_label(timestamp):
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp
    local = dt.astimezone(ZoneInfo(SFO_TZ))
    return f"{local.strftime('%b')} {local.day}, {local.strftime('%I:%M %p').lstrip('0')}"


def local_time_label(timestamp):
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp
    local = dt.astimezone(ZoneInfo(SFO_TZ))
    return local.strftime("%I:%M %p").lstrip("0")


def average(values):
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def success_rate(errors, tolerance):
    if not errors:
        return None
    return sum(error <= tolerance for error in errors) / len(errors) * 100


def load_forecast_success(path=DB_PATH):
    data = {
        "available": False,
        "sourceLabel": "Clean next-day blend",
        "eligibility": "last pre-midnight SFO snapshot from the day before target; observed lock/floor rows excluded",
        "scoredCount": 0,
        "allScoredCount": 0,
        "scoredDays": 0,
        "dailyScoredCount": 0,
        "pendingCount": 0,
        "pendingTotalCount": 0,
        "excludedOperationalCount": 0,
        "snapshotMae": None,
        "snapshotSuccessRate": None,
        "dailyMae": None,
        "dailySuccessRate": None,
        "overallMae": None,
        "ultimateSuccessRate": None,
        "toleranceRates": [],
        "refreshRows": [],
        "dailyRows": [],
        "recentRows": [],
        "sameDayContext": {
            "available": False,
            "count": 0,
            "recentRows": [],
        },
    }
    if not path.exists():
        data["reason"] = "weather.db not found yet."
        return data

    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            if not table_exists(conn, "forecast_blend_daily_high"):
                data["reason"] = "Blended forecast snapshots have not been archived yet."
                return data
            pending_rows = conn.execute(
                """
                SELECT target_date,
                       fetched_at,
                       details_json
                FROM forecast_blend_daily_high
                WHERE actual_high_f IS NULL
                """
            ).fetchall()
            data["pendingTotalCount"] = len(pending_rows)
            data["pendingCount"] = sum(
                1
                for row in pending_rows
                if is_clean_next_day_forecast(
                    row["target_date"],
                    row["fetched_at"],
                    row["details_json"],
                )
            )
            rows = conn.execute(
                """
                SELECT fetched_at,
                       target_date,
                       predicted_high_f,
                       actual_high_f,
                       abs_error_f,
                       calls_used_today,
                       source_count,
                       fresh_station_count,
                       station_adjustment_f,
                       details_json
                FROM forecast_blend_daily_high
                WHERE actual_high_f IS NOT NULL
                  AND abs_error_f IS NOT NULL
                ORDER BY fetched_at
                """
            ).fetchall()
    except sqlite3.Error as exc:
        data["reason"] = f"Could not read forecast backtest table: {exc}"
        return data

    if not rows:
        data["reason"] = "Waiting for completed NWS daily highs to score archived forecasts."
        return data

    parsed = []
    for row in rows:
        details = parse_details_json(row["details_json"])
        google_api = details.get("google_weather_api") if isinstance(details, dict) else {}
        refresh_slot = (
            google_api.get("refreshes_today")
            if isinstance(google_api, dict)
            else None
        )
        if refresh_slot is None:
            refresh_slot = row["calls_used_today"]
        refresh_slot = int(refresh_slot) if refresh_slot is not None else None
        refresh_time = local_time_label(row["fetched_at"])
        parsed.append(
            {
                "fetchedAt": row["fetched_at"],
                "fetchedLabel": local_label(row["fetched_at"]),
                "targetDate": row["target_date"],
                "predicted": round(float(row["predicted_high_f"]), 1),
                "actual": round(float(row["actual_high_f"]), 1),
                "error": round(float(row["abs_error_f"]), 2),
                "refreshSlot": refresh_slot,
                "refreshTimeLabel": refresh_time,
                "refreshLabel": f"R{refresh_slot} @ {refresh_time}" if refresh_slot is not None else refresh_time,
                "refreshDetail": (
                    f"Refresh {refresh_slot} @ {local_label(row['fetched_at'])}"
                    if refresh_slot is not None
                    else local_label(row["fetched_at"])
                ),
                "sourceCount": row["source_count"],
                "freshStationCount": row["fresh_station_count"],
                "stationAdjustment": row["station_adjustment_f"],
                "scoreCategory": forecast_score_category(
                    row["target_date"],
                    row["fetched_at"],
                    row["details_json"],
                ),
            }
        )

    clean_rows = [row for row in parsed if row["scoreCategory"] == "clean_next_day"]
    same_day_rows = [row for row in parsed if row["scoreCategory"] == "same_day_operational"]
    data["allScoredCount"] = len(parsed)
    data["excludedOperationalCount"] = len(same_day_rows)
    data["sameDayContext"] = {
        "available": bool(same_day_rows),
        "count": len(same_day_rows),
        "recentRows": list(reversed(same_day_rows[-6:])),
    }
    if not clean_rows:
        data["reason"] = (
            "Waiting for clean next-day forecast snapshots. "
            "Same-day observed lock/floor rows are tracked as operational context."
        )
        return data

    latest_by_day = {}
    for row in clean_rows:
        current = latest_by_day.get(row["targetDate"])
        if current is None or row["fetchedAt"] > current["fetchedAt"]:
            latest_by_day[row["targetDate"]] = row

    daily_rows = sorted(latest_by_day.values(), key=lambda row: row["targetDate"])
    snapshot_errors = [row["error"] for row in clean_rows]
    daily_errors = [row["error"] for row in daily_rows]
    data.update(
        {
            "available": True,
            "scoredCount": len(clean_rows),
            "scoredDays": len(daily_rows),
            "dailyScoredCount": len(daily_rows),
            "snapshotMae": round(average(snapshot_errors), 2),
            "snapshotSuccessRate": round(success_rate(snapshot_errors, 3), 1),
            "dailyMae": round(average(daily_errors), 2),
            "dailySuccessRate": round(success_rate(daily_errors, 3), 1),
            "overallMae": round(average(snapshot_errors), 2),
            "ultimateSuccessRate": round(success_rate(daily_errors, 3), 1),
            "toleranceRates": [
                {
                    "label": f"within {tol}°F",
                    "tolerance": tol,
                    "snapshotRate": round(success_rate(snapshot_errors, tol), 1),
                    "snapshotCount": sum(error <= tol for error in snapshot_errors),
                    "dailyRate": round(success_rate(daily_errors, tol), 1),
                    "dailyCount": sum(error <= tol for error in daily_errors),
                }
                for tol in (2, 3, 5)
            ],
            "dailyRows": list(reversed(daily_rows)),
            "recentRows": list(reversed(clean_rows[-8:])),
        }
    )

    refresh_numbers = sorted({
            row["refreshSlot"]
            for row in clean_rows
            if row["refreshSlot"] is not None
    })
    refresh_rows = []
    for refresh_number in refresh_numbers:
        slot_rows = [
            row
            for row in clean_rows
            if row["refreshSlot"] == refresh_number
        ]
        refresh_errors = [
            row["error"]
            for row in slot_rows
        ]
        latest = slot_rows[-1] if slot_rows else {}
        refresh_rows.append(
            {
                "refresh": refresh_number,
                "label": latest.get("refreshLabel") or f"R{refresh_number}",
                "timeLabel": latest.get("refreshTimeLabel"),
                "latestFetchedAt": latest.get("fetchedAt"),
                "latestFetchedLabel": latest.get("fetchedLabel"),
                "count": len(refresh_errors),
                "mae": round(average(refresh_errors), 2) if refresh_errors else None,
                "success3": round(success_rate(refresh_errors, 3), 1) if refresh_errors else None,
            }
        )
    data["refreshRows"] = refresh_rows
    return data


def load_nws_ground_truth(path=DB_PATH):
    data = {"available": False, "rows": []}
    if not path.exists():
        return data

    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            if not table_exists(conn, "nws_daily_high_ground_truth"):
                return data
            rows = conn.execute(
                """
                SELECT local_date,
                       high_f,
                       high_observed_at,
                       is_complete,
                       observation_count,
                       updated_at
                FROM nws_daily_high_ground_truth
                WHERE station_id = 'KSFO'
                ORDER BY local_date DESC
                LIMIT 14
                """
            ).fetchall()
    except sqlite3.Error:
        return data

    data["rows"] = [
        {
            "date": row["local_date"],
            "highF": round(float(row["high_f"]), 1) if row["high_f"] is not None else None,
            "isComplete": bool(row["is_complete"]),
            "observationCount": row["observation_count"],
            "observedAt": row["high_observed_at"],
            "updatedAt": row["updated_at"],
        }
        for row in rows
    ]
    data["available"] = bool(data["rows"])
    return data


def p_value(p):
    return "< 0.0001" if p < 1e-4 else f"{p:.4f}"


def prepare_dashboard_context():
    import pandas as pd

    daily = load_daily_predictions(
        "models/lstm_target_daily_high_next_day_test_preds.csv",
        shift_days=1,
    )
    daily["abs_error"] = (daily["pred"] - daily["actual"]).abs()

    bins = [40, 55, 65, 75, 85, 120]
    labels = ["40-55", "55-65", "65-75", "75-85", "85+"]
    daily["bucket"] = pd.cut(daily["actual"], bins=bins, labels=labels)
    bucket_mae = daily.groupby("bucket", observed=True)["abs_error"].mean().round(2)
    bucket_counts = daily.groupby("bucket", observed=True)["abs_error"].count()

    ab = load_json("ab_test_results.json")
    forecast = load_json("forecast_data.json")
    model_compare = load_json_optional("model_compare_results.json", {})
    weather_story = load_json_optional("weather_story_data.json", {})
    google_forecast = load_json_optional("google_weather_cache.json", {"available": False})
    forecast_success = load_forecast_success()
    nws_ground_truth = load_nws_ground_truth()
    ab_daily = ab["target_daily_high_next_day"]
    ab_spot = ab["target_temp_next_24h"]

    data_vars = f"""
    const dailyDates = {js([str(d) for d in daily["forecast_date"]])};
    const dailyPred = {js([round(float(v), 1) for v in daily["pred"]])};
    const dailyActual = {js([round(float(v), 1) for v in daily["actual"]])};
    const errLabels = {js([str(v) for v in bucket_mae.index])};
    const errMae = {js([float(v) for v in bucket_mae.values])};
    const errCounts = {js([int(v) for v in bucket_counts.values])};
    const FORECAST = {js(forecast)};
    let GOOGLE_FORECAST = {js(google_forecast)};
    window.CHART_DATA = {js({
        "abDailyHigh": ab_daily.get("chart", {}),
        "dailyHighImportance": model_compare.get(
            "target_daily_high_next_day", {}
        ).get("xgb_feature_importance", []),
        "weatherStory": weather_story,
        "forecastSuccess": forecast_success,
        "nwsGroundTruth": nws_ground_truth,
    })};
    const CHART_DATA = window.CHART_DATA;
"""

    replacements = {
        "__DATA_VARS__": data_vars,
        "__LSTM_MAE__": f"{ab_daily['mae_lstm']:.2f}",
        "__XGB_MAE__": f"{ab_daily['mae_xgb']:.2f}",
        "__LIFT__": f"{ab_daily['lift_pct']:.0f}",
        "__GAIN__": f"{ab_daily['mae_diff']:.2f}",
        "__CI_LO__": f"{ab_daily['ci_low']:.2f}",
        "__CI_HI__": f"{ab_daily['ci_high']:.2f}",
        "__P_VALUE__": p_value(ab_daily["p_ttest"]),
        "__WIN_RATE__": f"{ab_daily['win_rate'] * 100:.0f}",
        "__N_DAYS__": f"{ab_daily['n_days']}",
        "__SPOT_LSTM__": f"{ab_spot['mae_lstm']:.2f}",
        "__SPOT_XGB__": f"{ab_spot['mae_xgb']:.2f}",
        "__SPOT_CI_LO__": f"{ab_spot['ci_low']:.2f}",
        "__SPOT_CI_HI__": f"{ab_spot['ci_high']:.2f}",
        "__SPOT_P__": p_value(ab_spot["p_ttest"]),
        "__N_YEARS__": f"{forecast['n_years']}",
    }
    return {"data_vars": data_vars, "replacements": replacements}
