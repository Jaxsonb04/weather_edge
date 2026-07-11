"""Create exploratory plots from the engineered weather features."""

import json
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path

try:
    from .features import load_data, engineer_features
except ImportError:  # Direct invocation: python research/eda.py
    from features import load_data, engineer_features

FEATURES_PATH = "weather_features.csv"
PLOTS_DIR     = Path("plots")
PLOTS_DIR.mkdir(exist_ok=True)


def get_features(use_cache=True):
    """Load engineered features, building them if the cache is missing."""
    cache = Path(FEATURES_PATH)
    if use_cache and cache.exists():
        print(f"loading cached features from {FEATURES_PATH}...")
        df = pd.read_csv(cache, index_col=0, parse_dates=True)
    else:
        print("building features from scratch...")
        df = engineer_features(load_data())
        df.to_csv(cache)
    print(f"  {len(df):,} rows x {df.shape[1]} columns")
    return df


def plot_temp_trend(df):
    fig, axes = plt.subplots(3, 1, figsize=(14, 9), sharex=True)
    fig.suptitle("SFO temperature trends 2016-2026", fontsize=13)

    axes[0].plot(df.index, df["temp_f"], color="#378ADD", lw=0.4, label="raw hourly")
    axes[0].set_ylabel("Temp (F)")
    axes[0].legend(fontsize=9); axes[0].grid(alpha=0.3)

    axes[1].plot(df.index, df["temp_roll_mean_6h"],  color="#1D9E75", lw=0.8, label="6h rolling mean")
    axes[1].plot(df.index, df["temp_roll_mean_24h"], color="#D85A30", lw=1.0, label="24h rolling mean")
    axes[1].set_ylabel("Temp (F)")
    axes[1].legend(fontsize=9); axes[1].grid(alpha=0.3)

    axes[2].plot(df.index, df["temp_roll_std_6h"], color="#7F77DD", lw=0.6, label="6h volatility")
    axes[2].set_ylabel("Std Dev (F)")
    axes[2].set_xlabel("Date")
    axes[2].legend(fontsize=9); axes[2].grid(alpha=0.3)

    plt.tight_layout()
    fig.savefig(PLOTS_DIR / "1_temp_trend.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("saved: plots/1_temp_trend.png")


def plot_patterns(df):
    fig, axes = plt.subplots(1, 3, figsize=(16, 4))

    local_hour = df.index.tz_convert("America/Los_Angeles").hour
    hourly = df.groupby(local_hour)["temp_f"].mean()
    axes[0].bar(hourly.index, hourly.values, color="#378ADD", alpha=0.85)
    axes[0].set(xlabel="Hour of day (Pacific)", ylabel="Avg temp (F)",
                title="Diurnal cycle")
    axes[0].grid(axis="y", alpha=0.3)

    daily = df.groupby("day_of_week")["temp_f"].mean()
    days  = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    axes[1].bar(days, daily.values, color="#1D9E75", alpha=0.85)
    axes[1].set(xlabel="Day of week", ylabel="Avg temp (F)",
                title="Weekly pattern")
    axes[1].grid(axis="y", alpha=0.3)

    monthly = df.groupby("month")["temp_f"].mean()
    axes[2].bar(monthly.index, monthly.values, color="#D85A30", alpha=0.85)
    axes[2].set(xlabel="Month", ylabel="Avg temp (F)",
                title="Annual cycle")
    axes[2].grid(axis="y", alpha=0.3)

    plt.tight_layout()
    fig.savefig(PLOTS_DIR / "2_patterns.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("saved: plots/2_patterns.png")


def plot_correlation_heatmap(df):
    cols = [c for c in [
        "temp_f", "humidity", "wind_speed_mph", "pressure_hpa",
        "dew_point_f", "dewpoint_depression", "cloud_cover_pct",
        "temp_lag_1h", "temp_lag_3h", "temp_lag_6h",
        "temp_lag_24h", "temp_lag_72h",
        "temp_roll_mean_6h", "temp_roll_mean_24h",
        "pressure_change_24h", "humidity_lag_24h",
        "hour_sin", "hour_cos", "month_sin", "month_cos",
        "target_temp_next_24h",
    ] if c in df.columns]

    corr = df[cols].corr()
    mask = np.triu(np.ones_like(corr, dtype=bool))

    fig, ax = plt.subplots(figsize=(13, 11))
    sns.heatmap(
        corr, mask=mask, annot=True, fmt=".2f",
        cmap="RdBu_r", center=0, vmin=-1, vmax=1,
        linewidths=0.4, ax=ax, annot_kws={"size": 7},
    )
    ax.set_title("Feature correlation heatmap", fontsize=13, pad=12)
    plt.tight_layout()
    fig.savefig(PLOTS_DIR / "3_correlation_heatmap.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("saved: plots/3_correlation_heatmap.png")


def plot_lag_scatters(df):
    lags = ["temp_lag_1h", "temp_lag_3h", "temp_lag_6h", "temp_lag_24h"]
    fig, axes = plt.subplots(1, 4, figsize=(16, 4))
    fig.suptitle("target (next-24h temp) vs lag features", fontsize=12)

    for ax, lag in zip(axes, lags):
        sub = df[[lag, "target_temp_next_24h"]].dropna()
        corr_val = sub[lag].corr(sub["target_temp_next_24h"])
        ax.scatter(sub[lag], sub["target_temp_next_24h"],
                   alpha=0.1, s=5, color="#378ADD")
        ax.set(xlabel=lag, ylabel="next 24h temp" if ax is axes[0] else "",
               title=f"r = {corr_val:.3f}")
        ax.grid(alpha=0.3)

    plt.tight_layout()
    fig.savefig(PLOTS_DIR / "4_lag_scatters.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("saved: plots/4_lag_scatters.png")


def plot_annual_overlay(df):
    fig, ax = plt.subplots(figsize=(14, 5))

    # Skip sparse weeks so missing data does not draw misleading flat lines.
    weekly = df["temp_f"].resample("W").agg(["mean", "count"])
    weekly = weekly[weekly["count"] >= 24 * 3]
    weekly["year"] = weekly.index.year
    weekly["week"] = weekly.index.isocalendar().week

    years   = sorted(weekly["year"].unique())
    palette = sns.color_palette("viridis", n_colors=len(years))

    for color, year in zip(palette, years):
        sub = weekly[weekly["year"] == year].sort_values("week")
        ax.plot(sub["week"], sub["mean"], color=color, lw=1.2,
                alpha=0.8, label=str(year))

    ax.set(xlabel="Week of year", ylabel="Mean temp (F)",
           title="Annual temperature cycle overlaid by year")
    ax.legend(fontsize=8, ncol=2, loc="upper left")
    ax.grid(alpha=0.3)
    plt.tight_layout()
    fig.savefig(PLOTS_DIR / "5_annual_overlay.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("saved: plots/5_annual_overlay.png")


def plot_temp_distribution(df):
    season_map = {12:"DJF", 1:"DJF", 2:"DJF",
                  3:"MAM", 4:"MAM", 5:"MAM",
                  6:"JJA", 7:"JJA", 8:"JJA",
                  9:"SON",10:"SON",11:"SON"}
    df = df.copy()
    df["season"] = df["month"].map(season_map)

    fig, axes = plt.subplots(1, 2, figsize=(14, 4.5))

    axes[0].hist(df["temp_f"].dropna(), bins=60, color="#378ADD",
                 alpha=0.8, edgecolor="white")
    axes[0].axvline(df["temp_f"].mean(), color="#D85A30", lw=1.5,
                    label=f"mean = {df['temp_f'].mean():.1f}F")
    axes[0].set(xlabel="Temp (F)", ylabel="Count",
                title="Overall temperature distribution")
    axes[0].legend(fontsize=9); axes[0].grid(alpha=0.3)

    order  = ["DJF", "MAM", "JJA", "SON"]
    colors = ["#378ADD", "#1D9E75", "#D85A30", "#7F77DD"]
    data   = [df[df["season"] == s]["temp_f"].dropna() for s in order]
    bp = axes[1].boxplot(data, tick_labels=order, patch_artist=True,
                         medianprops={"color": "black"})
    for patch, c in zip(bp["boxes"], colors):
        patch.set_facecolor(c); patch.set_alpha(0.7)
    axes[1].set(ylabel="Temp (F)", title="Temperature by season")
    axes[1].grid(axis="y", alpha=0.3)

    plt.tight_layout()
    fig.savefig(PLOTS_DIR / "6_distribution.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("saved: plots/6_distribution.png")


def write_dashboard_weather_data(df):
    temp = df["temp_f"].dropna()
    counts, edges = np.histogram(temp, bins=np.arange(35, 106, 2.5))
    centers = (edges[:-1] + edges[1:]) / 2

    monthly = df.groupby("month")["temp_f"].agg(["mean", "min", "max"]).round(2)

    payload = {
        "temperature_histogram": {
            "labels": [round(float(v), 1) for v in centers],
            "counts": [int(v) for v in counts],
        },
        "monthly_temperature": [
            {
                "month": int(month),
                "mean": float(row["mean"]),
                "min": float(row["min"]),
                "max": float(row["max"]),
            }
            for month, row in monthly.iterrows()
        ],
    }
    Path("weather_story_data.json").write_text(json.dumps(payload, indent=2))
    print("wrote weather_story_data.json")


def print_summary(df):
    print("\ndataset summary")
    print(f"rows:       {len(df):,}")
    print(f"features:   {df.shape[1]} columns")
    print(f"date range: {df.index.min()} to {df.index.max()}")
    print(f"years:      {sorted(df.index.year.unique())}")

    targets = [
        "target_temp_next_24h",
        "target_temp_next_48h",
        "target_daily_high_next_day",
    ]
    for target in targets:
        if target not in df.columns:
            continue
        top = (
            df.select_dtypes(include="number").corr()[target]
            .drop(targets, errors="ignore")
            .abs().sort_values(ascending=False)
            .head(10)
        )
        print(f"\ntop 10 features by correlation with {target}")
        print(top.round(3).to_string())


if __name__ == "__main__":
    df = get_features(use_cache=True)

    print_summary(df)

    print("\ngenerating plots...")
    plot_temp_trend(df)
    plot_patterns(df)
    plot_correlation_heatmap(df)
    plot_lag_scatters(df)
    plot_annual_overlay(df)
    plot_temp_distribution(df)
    write_dashboard_weather_data(df)

    print(f"\nall plots saved to {PLOTS_DIR}/")
