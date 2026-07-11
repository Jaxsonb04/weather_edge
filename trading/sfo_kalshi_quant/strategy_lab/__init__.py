"""Strategy Lab artifact domains."""

from datetime import timedelta

ACTIVE_CALIBRATION_SOURCE = "lstm"
CHALLENGER_CALIBRATION_SOURCE = "clean-blend/combined"
MIN_CLEAN_WINNER_SAMPLE = 60
DEFAULT_MODEL_VETO_MAX_LOSS_PCT = 60.0
DEFAULT_MODEL_VETO_BUFFER = 0.08
PRIMARY_PROFILE = "live"
EXPERIMENTAL_PROFILES = {"research"}
FORECAST_HEALTH_ROLLING_DAYS = 3
FORECAST_HEALTH_MIN_NWP_MODELS = 6
FORECAST_HEALTH_MAX_NWP_AGE = timedelta(hours=36)
FORECAST_HEALTH_MAX_EMOS_AGE = timedelta(hours=6)
FORECAST_HEALTH_MAX_CLISFO_LAG_DAYS = 2
FORECAST_LEAD_MODE_LABELS = {
    "day_ahead": "Day-ahead forecast",
    "same_day_prelock": "Same-day pre-lock forecast",
    "intraday_high_so_far": "Intraday observed-high edge",
    "post_resolution_excluded": "Post-resolution excluded",
    "unknown": "Unknown lead mode",
}
