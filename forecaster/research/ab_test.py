"""Paired model comparison with confidence intervals and p-values."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import stats
from sklearn.metrics import mean_absolute_error

try:
    from .forecast_validation import (
        chronological_unit_split_masks,
        forecast_unit_dates as split_forecast_unit_dates,
    )
except ImportError:  # Direct invocation: python research/ab_test.py
    from forecast_validation import (
        chronological_unit_split_masks,
        forecast_unit_dates as split_forecast_unit_dates,
    )

FEATURES_PATH = "weather_features.csv"
MODELS_DIR = Path("models")
PLOTS_DIR = Path("plots")
SFO_TZ = "America/Los_Angeles"
N_BOOT = 10_000
RNG = np.random.default_rng(42)


def rounded(value, ndigits=3):
    return round(float(value), ndigits)


def histogram_payload(values, bins=60):
    counts, edges = np.histogram(values, bins=bins)
    centers = (edges[:-1] + edges[1:]) / 2
    return {
        "labels": [rounded(v, 3) for v in centers],
        "counts": [int(v) for v in counts],
    }


def diebold_mariano_test(loss_diff):
    """Newey-West Diebold-Mariano style test for paired forecast loss deltas.

    Positive loss_diff means model B lost more than model A. This project uses
    it as a diagnostic alongside bootstrap and Wilcoxon tests, not as the sole
    decision rule.
    """

    values = np.asarray(loss_diff, dtype=float)
    values = values[np.isfinite(values)]
    n = len(values)
    if n < 3:
        return {"stat": float("nan"), "p_value": float("nan"), "lags": 0}

    centered = values - values.mean()
    max_lag = min(n - 1, max(0, int(round(n ** (1 / 3)))))
    gamma0 = float(np.dot(centered, centered) / n)
    long_run_var = gamma0
    for lag in range(1, max_lag + 1):
        cov = float(np.dot(centered[lag:], centered[:-lag]) / n)
        weight = 1.0 - lag / (max_lag + 1.0)
        long_run_var += 2.0 * weight * cov

    if long_run_var <= 0:
        return {"stat": float("nan"), "p_value": float("nan"), "lags": max_lag}
    stat = float(values.mean() / np.sqrt(long_run_var / n))
    p_value = float(2.0 * stats.norm.sf(abs(stat)))
    return {"stat": stat, "p_value": p_value, "lags": max_lag}


def dashboard_payload(daily, delta, boot):
    rows = daily.reset_index()
    return {
        "daily": [
            {
                "date": str(row["date"]),
                "actual": rounded(row["y"], 2),
                "xgb": rounded(row["xgb"], 2),
                "lstm": rounded(row["lstm"], 2),
                "err_xgb": rounded(abs(row["xgb"] - row["y"]), 2),
                "err_lstm": rounded(abs(row["lstm"] - row["y"]), 2),
                "delta": rounded(row["delta"], 2),
            }
            for _, row in rows.iterrows()
        ],
        "delta_sorted": [rounded(v, 2) for v in np.sort(delta)[::-1]],
        "bootstrap_hist": histogram_payload(boot),
    }


def load_xgboost_test(target, df):
    features = json.loads((MODELS_DIR / f"xgb_{target}_features.json").read_text())
    model = xgb.XGBRegressor()
    model.load_model(MODELS_DIR / f"xgb_{target}.json")
    df = df.dropna(subset=[target])
    masks = chronological_unit_split_masks(df.index, target)
    X_test, y_test = df[features].loc[masks["test"]], df[target].loc[masks["test"]]
    return pd.Series(model.predict(X_test), index=y_test.index), y_test


def load_lstm_test(target):
    t = pd.read_csv(MODELS_DIR / f"lstm_{target}_test_preds.csv",
                    index_col=0, parse_dates=True)
    return t["pred"], t["actual"]


def forecast_unit_dates(index, target):
    return split_forecast_unit_dates(index, target)


def run_ab_test(target, df, label):
    print(f"\n{'='*64}\nA/B TEST: {label}\n{'='*64}")

    xgb_pred, _ = load_xgboost_test(target, df)
    lstm_pred, lstm_true = load_lstm_test(target)

    # Align predictions, then collapse hourly rows to forecasted local days.
    common = xgb_pred.index.intersection(lstm_pred.index)
    frame = pd.DataFrame({
        "y":    lstm_true.loc[common],
        "xgb":  xgb_pred.loc[common],
        "lstm": lstm_pred.loc[common],
    })
    frame["date"] = forecast_unit_dates(frame.index, target)
    daily = frame.groupby("date").mean(numeric_only=True)

    y = daily["y"].values
    err_lstm = np.abs(daily["lstm"].values - y)   # variant A error
    err_xgb = np.abs(daily["xgb"].values - y)     # variant B error
    daily["err_lstm"] = err_lstm
    daily["err_xgb"] = err_xgb
    daily["delta"] = err_xgb - err_lstm
    n = len(daily)

    mae_lstm = float(err_lstm.mean())
    mae_xgb = float(err_xgb.mean())
    delta = daily["delta"].values
    mae_diff = float(delta.mean())               # = mae_xgb - mae_lstm
    lift_pct = float(mae_diff / mae_xgb * 100)   # % error reduction vs XGBoost

    t_stat, p_t = stats.ttest_rel(err_xgb, err_lstm)
    dm = diebold_mariano_test(delta)

    try:
        w_stat, p_w = stats.wilcoxon(err_xgb, err_lstm)
    except ValueError:
        w_stat, p_w = float("nan"), float("nan")

    cohens_d = float(delta.mean() / delta.std(ddof=1))

    boot = np.empty(N_BOOT)
    idx = np.arange(n)
    for b in range(N_BOOT):
        s = RNG.choice(idx, size=n, replace=True)
        boot[b] = err_xgb[s].mean() - err_lstm[s].mean()
    ci_lo, ci_hi = np.percentile(boot, [2.5, 97.5])
    boot_p = float((boot <= 0).mean())

    win_rate = float((err_lstm < err_xgb).mean())

    significant = bool(p_t < 0.05 and ci_lo > 0)

    print(f"  experiment units (days)   n = {n}")
    print(f"  MAE  LSTM (A)               {mae_lstm:.3f} F")
    print(f"  MAE  XGBoost (B)            {mae_xgb:.3f} F")
    print(f"  MAE difference (B - A)      {mae_diff:+.3f} F")
    print(f"  lift (error reduction)      {lift_pct:+.1f} %")
    print(f"  LSTM win rate               {win_rate:.1%} of days")
    print(f"  paired t-test               t = {t_stat:.3f}   p = {p_t:.2e}")
    print(
        "  Diebold-Mariano style       "
        f"DM = {dm['stat']:.3f}   p = {dm['p_value']:.2e}   lags = {dm['lags']}"
    )
    print(f"  Wilcoxon signed-rank        W = {w_stat:.0f}   p = {p_w:.2e}")
    print(f"  Cohen's d (paired)          {cohens_d:.3f}")
    print(f"  bootstrap 95% CI on diff    [{ci_lo:+.3f}, {ci_hi:+.3f}] F")
    print(f"  verdict                     "
          f"{'SIGNIFICANT - LSTM wins' if significant else 'not significant'}")

    plot_ab(label, target, delta, boot, ci_lo, ci_hi, mae_diff, win_rate)

    return {
        "target": target,
        "label": label,
        "n_days": n,
        "mae_lstm": round(mae_lstm, 3),
        "mae_xgb": round(mae_xgb, 3),
        "mae_diff": round(mae_diff, 3),
        "lift_pct": round(lift_pct, 2),
        "win_rate": round(win_rate, 4),
        "t_stat": round(float(t_stat), 3),
        "p_ttest": float(p_t),
        "dm_stat": round(float(dm["stat"]), 3),
        "p_diebold_mariano": float(dm["p_value"]),
        "dm_lags": int(dm["lags"]),
        "w_stat": float(w_stat),
        "p_wilcoxon": float(p_w),
        "cohens_d": round(cohens_d, 3),
        "ci_low": round(float(ci_lo), 3),
        "ci_high": round(float(ci_hi), 3),
        "boot_p": boot_p,
        "significant": significant,
        "chart": dashboard_payload(daily, delta, boot),
    }


def plot_ab(label, target, delta, boot, ci_lo, ci_hi, mae_diff, win_rate):
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    fig.suptitle(f"A/B test — {label}", fontsize=14, fontweight="bold")

    # left: per-day error difference (XGB error - LSTM error)
    ax = axes[0]
    colors = ["#16a34a" if d > 0 else "#ef4444" for d in delta]
    ax.bar(range(len(delta)), np.sort(delta)[::-1],
           color=[c for _, c in sorted(zip(delta, colors), reverse=True)],
           width=1.0)
    ax.axhline(0, color="#0f172a", lw=1)
    ax.axhline(mae_diff, color="#3b82f6", lw=1.5, ls="--",
               label=f"mean diff {mae_diff:+.2f}°F")
    ax.set(xlabel="forecast day (sorted)",
           ylabel="XGBoost error − LSTM error (°F)",
           title=f"green = LSTM closer ({win_rate:.0%} of days)")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.25)

    # right: bootstrap distribution of the MAE difference
    ax = axes[1]
    ax.hist(boot, bins=60, color="#3b82f6", alpha=0.8)
    ax.axvline(0, color="#ef4444", lw=1.5, label="no difference")
    ax.axvline(ci_lo, color="#0f172a", lw=1, ls="--")
    ax.axvline(ci_hi, color="#0f172a", lw=1, ls="--",
               label=f"95% CI [{ci_lo:+.2f}, {ci_hi:+.2f}]")
    ax.set(xlabel="MAE difference: XGBoost − LSTM (°F)",
           ylabel="bootstrap resamples",
           title="10,000-sample bootstrap of the lift")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.25)

    plt.tight_layout()
    fname = f"12_ab_test_{target}.png"
    fig.savefig(PLOTS_DIR / fname, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  saved: plots/{fname}")


if __name__ == "__main__":
    df = pd.read_csv(FEATURES_PATH, index_col=0, parse_dates=True)
    results = {
        "target_daily_high_next_day": run_ab_test(
            "target_daily_high_next_day", df, "Tomorrow's daily high"),
        "target_temp_next_24h": run_ab_test(
            "target_temp_next_24h", df, "Spot temp 24h ahead"),
    }
    Path("ab_test_results.json").write_text(json.dumps(results, indent=2))
    print("\nwrote ab_test_results.json")
