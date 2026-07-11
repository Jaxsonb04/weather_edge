import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

def env_int(name, default):
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name, default):
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_bool(name, default):
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


SFO_TZ = ZoneInfo("America/Los_Angeles")
SFO_POINT = {"lat": 37.6213, "lon": -122.3790}
HOURLY_API_URL = "https://weather.googleapis.com/v1/forecast/hours:lookup"
DAILY_API_URL = "https://weather.googleapis.com/v1/forecast/days:lookup"
CURRENT_API_URL = "https://weather.googleapis.com/v1/currentConditions:lookup"
NWS_API_URL = "https://api.weather.gov"
OPEN_METEO_API_URL = "https://api.open-meteo.com/v1/forecast"
API_KEY_ENV = "GOOGLE_WEATHER_API_KEY"
CACHE_PATH = Path("google_weather_cache.json")
USAGE_PATH = Path(".google_weather_usage.json")
DB_PATH = Path("weather.db")
FORECAST_DATA_PATH = Path("forecast_data.json")
SFO_WEATHER_STATION_ID = "USW00023234"
GOOGLE_WEATHER_MONTHLY_FREE_CAP = 10000
GOOGLE_WEATHER_MONTHLY_EVENT_BUDGET = env_int("GOOGLE_WEATHER_MONTHLY_EVENT_BUDGET", 8000)
GOOGLE_WEATHER_DAILY_EVENT_BUDGET = env_int("GOOGLE_WEATHER_DAILY_EVENT_BUDGET", 260)
ENABLE_GOOGLE_DAILY_FORECAST = env_bool("ENABLE_GOOGLE_DAILY_FORECAST", True)
ENABLE_GOOGLE_CURRENT_CONDITIONS = env_bool("ENABLE_GOOGLE_CURRENT_CONDITIONS", True)
GOOGLE_DAILY_INTERNAL_WEIGHT = env_float("GOOGLE_DAILY_INTERNAL_WEIGHT", 0.15)
GOOGLE_DAILY_DISAGREEMENT_WARN_F = env_float("GOOGLE_DAILY_DISAGREEMENT_WARN_F", 2.5)
HOURLY_LOOKAHEAD_HOURS = 72
HOURLY_PAGE_SIZE = 24
MIN_HOURS_FOR_DAILY_HIGH = 18
NWS_USER_AGENT = "SFO Weather Forecaster student project"
FRESH_OBSERVATION_MINUTES = 180
BLEND_WEIGHTS = {
    "google": 0.38,
    "nws": 0.36,
    "open_meteo": 0.18,
    "history": 0.08,
}
# 5 scored days was too little evidence to shift weights for a trading edge;
# learning now also has to beat the base blend on a walk-forward holdout.
ADAPTIVE_WEIGHT_MIN_SCORED_DAYS = 15
ADAPTIVE_WEIGHT_MAX_LEARNED_SHARE = 0.60
ADAPTIVE_WEIGHT_HOLDOUT_MIN_DAYS = 5
ADAPTIVE_SOURCE_COLUMNS = {
    "google": "google_high_f",
    "nws": "nws_high_f",
    "open_meteo": "open_meteo_high_f",
    "history": "history_high_f",
}
# Post-hoc rolling residual de-bias on the final blend (lowers the clean miss
# without touching source weights). Cohort-aware and capped so a noisy week
# cannot blow up the forecast; gated on a walk-forward holdout like the weights.
ENABLE_ROLLING_BLEND_BIAS = env_bool("ENABLE_ROLLING_BLEND_BIAS", True)
# Higher bar than the source-weight learner (15): the de-bias shifts the
# trade-relevant warm/hot tail, so it stays off until there are enough clean
# CLISFO-settled days to (a) match the backtest harness's >=30-day acceptance
# evidence bar and (b) give the per-cohort holdout guard real samples.
ROLLING_BIAS_MIN_SCORED_DAYS = env_int("SFO_ROLLING_BIAS_MIN_SCORED_DAYS", 30)
ROLLING_BIAS_WINDOW_DAYS = 45
ROLLING_BIAS_CAP_F = 1.5
ROLLING_BIAS_COHORT_SHRINK_K = 10.0
ROLLING_BIAS_HOLDOUT_MIN_DAYS = ADAPTIVE_WEIGHT_HOLDOUT_MIN_DAYS
# A cohort must have at least this many holdout days before its no-regression
# guard can fire -- below it the cohort MAE is too noisy to judge.
ROLLING_BIAS_COHORT_HOLDOUT_MIN_SAMPLES = 4
# Tail cohorts (warm/hot) are where the blend is anti-calibrated and where the
# bot trades; they get a zero-tolerance no-regression guard, others a small one.
ROLLING_BIAS_TAIL_COHORTS = ("warm", "hot")
ROLLING_BIAS_COHORT_REGRESSION_TOL_F = 0.25
ENABLE_SOURCE_MOS_CORRECTION = env_bool("ENABLE_SOURCE_MOS_CORRECTION", True)
SOURCE_MOS_MIN_SCORED_DAYS = env_int("SFO_SOURCE_MOS_MIN_SCORED_DAYS", 30)
SOURCE_MOS_HOLDOUT_MIN_DAYS = ADAPTIVE_WEIGHT_HOLDOUT_MIN_DAYS
SOURCE_MOS_CAP_F = env_float("SFO_SOURCE_MOS_CAP_F", 1.5)
SOURCE_MOS_COHORT_SHRINK_K = 10.0
DATASET_GUIDANCE_WEIGHT = env_float("SFO_DATASET_GUIDANCE_WEIGHT", 0.12)
AIRPORT_STATIONS = ("KSFO", "KOAK", "KSJC", "KSQL", "KPAO", "KHAF")
DURATION_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")
