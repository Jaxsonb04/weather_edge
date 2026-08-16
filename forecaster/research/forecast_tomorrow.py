"""Build the static climatology table used by the dashboard forecast widget."""

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

try:  # package import (pytest adds forecaster/ to pythonpath)
    from .forecast_validation import forecast_unit_dates
except ImportError:  # direct `python research/forecast_tomorrow.py`
    from forecast_validation import forecast_unit_dates

COMBINED = "combined_weather.csv"
MODELS_DIR = Path("models")
TARGET = "target_daily_high_next_day"
WINDOW_DAYS = 7   # +/- this many calendar days, to pool enough samples per date
SFO_TZ = "America/Los_Angeles"
SFO_STATION_ID = "USW00023234"


def daily_highs():
    """Return one row per SFO local date with that day's high temperature."""
    df = pd.read_csv(COMBINED, usecols=["timestamp", "station_id", "temp_f"],
                     parse_dates=["timestamp"])
    df = df[df["station_id"] == SFO_STATION_ID].copy()
    df = df.dropna(subset=["temp_f"])
    if df.empty:
        raise ValueError(f"no SFO rows found in {COMBINED}")
    # Fixed PST settlement-day bucketing, matching the NWS climate report.
    df["local_date"] = df["timestamp"].dt.tz_convert("Etc/GMT+8").dt.date
    d = (
        df.groupby("local_date")["temp_f"]
        .agg(high="max", observations="count")
        .reset_index()
        .rename(columns={"local_date": "date"})
    )
    d = d[d["observations"] >= 18]
    d["date"] = pd.to_datetime(d["date"])
    d["climo_day"] = d["date"].map(climatology_day)
    d = d.dropna(subset=["climo_day"])
    d["climo_day"] = d["climo_day"].astype(int)
    return d


def climatology_day(date):
    """Map a real date to a 365-day non-leap climatology calendar."""
    date = pd.Timestamp(date)
    if date.month == 2 and date.day == 29:
        return np.nan
    return pd.Timestamp(2001, date.month, date.day).dayofyear


def lstm_residual_sigma():
    """Validation residual spread for the trained daily-high LSTM, per DAY.

    The held-out prediction file is on the hourly grid: ~24 rows share one
    forecast target, so ``actual`` is literally repeated down the column and
    consecutive residuals are near-identical. Pooling them treated ~505
    independent forecast days as ~11.9k observations -- it inflated the point
    estimate (the pooled spread picks up within-day prediction jitter on top of
    the day-level error) and shrank the standard error of sigma by ~sqrt(24).

    Collapse to one residual per forecast unit using the same day mapping the
    train/val/test splitter already uses, so the published sigma is the spread
    of the daily-high forecast error over held-out DAYS.
    """

    v = pd.read_csv(
        MODELS_DIR / "lstm_target_daily_high_next_day_val_preds.csv",
        index_col=0,
        parse_dates=True,
    )
    index = pd.DatetimeIndex(v.index)
    if index.tz is None:
        index = index.tz_localize("UTC")
    units = forecast_unit_dates(index, TARGET)

    daily = (
        v.assign(_unit=units.values)
        .groupby("_unit", sort=True)
        .agg(actual=("actual", "first"), pred=("pred", "mean"), rows=("pred", "size"))
    )
    if len(daily) < 2:
        raise ValueError(
            "need at least two held-out forecast days to estimate a residual spread"
        )

    conflicts = int(
        (v.assign(_unit=units.values).groupby("_unit")["actual"].nunique() > 1).sum()
    )
    if conflicts:
        print(
            f"  WARNING: {conflicts} forecast day(s) carry more than one target value."
            "  The held-out prediction file predates the fixed-PST settlement-day"
            "  change in research/features.py -- regenerate it with"
            "  `python -m research.lstm_model` before trusting this sigma."
        )

    resid = daily["actual"] - daily["pred"]
    return float(resid.std()), float(resid.mean()), int(len(resid))


def build_table(d):
    """Summarize highs around each month/day in a circular calendar window."""
    days = d["climo_day"].to_numpy()
    highs = d["high"].to_numpy()
    table = {}
    for ref in pd.date_range("2001-01-01", "2001-12-31", freq="D"):
        center = ref.dayofyear
        diff = np.abs(days - center)
        mask = np.minimum(diff, 365 - diff) <= WINDOW_DAYS
        vals = highs[mask]
        if vals.size == 0:
            continue
        table[ref.strftime("%m-%d")] = {
            "mean": round(float(np.mean(vals)), 1),
            "std": round(float(np.std(vals)), 1),
            "p10": round(float(np.percentile(vals, 10)), 1),
            "p90": round(float(np.percentile(vals, 90)), 1),
            "record_high": round(float(np.max(vals)), 1),
            "record_low": round(float(np.min(vals)), 1),
            "n": int(vals.size),
        }
    return table


def main():
    d = daily_highs()
    sigma, bias, sigma_days = lstm_residual_sigma()
    # Standard error of a sample sd, over the DAY-level residuals that are the
    # actual independent units. Published so the tile can stop implying a
    # ~11.9k-row precision it never had.
    sigma_se = sigma / math.sqrt(2.0 * (sigma_days - 1))
    table = build_table(d)

    years = sorted(d["date"].dt.year.unique().tolist())
    out = {
        "lstm_sigma": round(sigma, 2),
        "lstm_bias": round(bias, 2),
        # Independent held-out forecast DAYS behind lstm_sigma (not hourly rows).
        "lstm_sigma_days": sigma_days,
        "lstm_sigma_se": round(sigma_se, 3),
        "n_years": len(years),
        "years": years,
        "n_days_observed": int(d["date"].nunique()),
        "window_days": WINDOW_DAYS,
        "table": table,
    }
    Path("forecast_data.json").write_text(json.dumps(out, indent=2))
    print(f"wrote forecast_data.json")
    print(f"  month/day slots:     {len(table)}")
    print(f"  years of data:       {years[0]}-{years[-1]} ({len(years)} yrs)")
    print(
        f"  LSTM residual sigma: {sigma:.2f} +/- {sigma_se:.2f} F   "
        f"bias: {bias:+.2f} F   ({sigma_days} held-out days)"
    )
    for key in ("05-31", "08-01", "01-01"):
        t = table[key]
        print(f"  {key}: mean {t['mean']:.1f}F  "
              f"band [{t['p10']:.0f},{t['p90']:.0f}]  n={t['n']}")


if __name__ == "__main__":
    main()
