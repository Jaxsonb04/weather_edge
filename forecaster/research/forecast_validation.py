from __future__ import annotations

from collections.abc import Iterable

import pandas as pd


# Fixed PST: forecast units must match the settlement-day labels built in
# research/features.py (local_standard_date), or rows from the 23:00-24:00 PST hour
# leak across split boundaries during DST.
SETTLEMENT_TZ = "Etc/GMT+8"


def forecast_unit_dates(index: pd.DatetimeIndex, target: str) -> pd.Series:
    """Return the atomic forecast unit used for train/validation/test splits.

    For tomorrow's daily-high target, every hourly row from the forecast day maps
    to the same target date. Splitting by raw row count can put the same target
    date in both train and test, which leaks the answer into model selection.
    """

    local = index.tz_convert(SETTLEMENT_TZ)
    if target == "target_daily_high_next_day":
        local_dates = pd.Series(local.date, index=index)
        return (pd.to_datetime(local_dates) + pd.Timedelta(days=1)).dt.date
    if target == "target_temp_next_24h":
        return pd.Series(local + pd.Timedelta(hours=24), index=index)
    return pd.Series(local, index=index)


def chronological_unit_split_masks(
    index: pd.DatetimeIndex,
    target: str,
    ratios: Iterable[float] = (0.7, 0.15, 0.15),
) -> dict[str, pd.Series]:
    ratios = tuple(ratios)
    if len(ratios) != 3:
        raise ValueError("ratios must be train/val/test")
    if any(value <= 0 for value in ratios):
        raise ValueError("split ratios must be positive")

    units = forecast_unit_dates(index, target)
    unique_units = pd.Index(pd.unique(units)).sort_values()
    if len(unique_units) < 3:
        raise ValueError("at least three forecast units are required for chronological split")

    total = sum(ratios)
    train_end = max(1, min(len(unique_units) - 2, int(len(unique_units) * ratios[0] / total)))
    val_end = max(train_end + 1, int(len(unique_units) * (ratios[0] + ratios[1]) / total))
    val_end = min(len(unique_units) - 1, val_end)

    train_units = set(unique_units[:train_end])
    val_units = set(unique_units[train_end:val_end])
    test_units = set(unique_units[val_end:])
    return {
        "train": units.isin(train_units),
        "val": units.isin(val_units),
        "test": units.isin(test_units),
    }
