"""Train XGBoost forecasters for spot temperature and tomorrow's high."""

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import xgboost as xgb
import json
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.model_selection import TimeSeriesSplit
from pathlib import Path
from itertools import product

try:
    from .forecast_validation import chronological_unit_split_masks
except ImportError:  # Direct invocation: python research/xgboost_model.py
    from forecast_validation import chronological_unit_split_masks

FEATURES_PATH = "weather_features.csv"
PLOTS_DIR     = Path("plots")
MODELS_DIR    = Path("models")
SFO_TZ        = "America/Los_Angeles"
PLOTS_DIR.mkdir(exist_ok=True)
MODELS_DIR.mkdir(exist_ok=True)


EXCLUDE = {
    # Targets would leak the answer into training.
    "target_temp_next_24h", "target_temp_next_48h",
    "target_daily_high_next_day",

    # Unit-converted duplicates.
    "temp_c", "dew_point_c", "wet_bulb_c", "wind_speed_ms",

    # Redundant with humidity in this station's data.
    "dewpoint_depression",

    "station_id", "station_name", "latitude", "longitude", "elevation",

    # The sin/cos versions keep circular values continuous.
    "wind_dir",

    "sky_condition", "sky_oktas",
}


def select_features(df):
    candidates = df.select_dtypes(include="number").columns
    return [c for c in candidates if c not in EXCLUDE]


def load_features(path, target):
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df = df.dropna(subset=[target])
    return df


def split_data(df, feature_cols, target, ratios=(0.7, 0.15, 0.15)):
    X, y = df[feature_cols], df[target]
    masks = chronological_unit_split_masks(df.index, target, ratios)
    return {
        "train": (X.loc[masks["train"]], y.loc[masks["train"]]),
        "val":   (X.loc[masks["val"]],   y.loc[masks["val"]]),
        "test":  (X.loc[masks["test"]],  y.loc[masks["test"]]),
    }


def compute_sample_weights(y, alpha=2.0):
    """Upweight rare temperature extremes."""
    z = (y - y.mean()) / y.std()
    return 1.0 + alpha * (z ** 2)


def tune_hyperparameters(X_train, y_train, weights_train):
    """Grid search with expanding-window time-series CV."""
    grid = {
        "max_depth":        [6, 8],
        "learning_rate":    [0.05],
        "min_child_weight": [3, 7],
        "subsample":        [0.8],
        "colsample_bytree": [0.7, 0.9],
    }
    combos = list(product(*grid.values()))
    print(f"  testing {len(combos)} hyperparameter combos with 3-fold time-series cv...")

    tscv = TimeSeriesSplit(n_splits=3)
    best_score, best_params = float("inf"), None

    for i, combo in enumerate(combos, 1):
        params = dict(zip(grid.keys(), combo))
        fold_scores = []
        for train_idx, val_idx in tscv.split(X_train):
            X_tr_cv = X_train.iloc[train_idx]
            y_tr_cv = y_train.iloc[train_idx]
            w_tr_cv = weights_train.iloc[train_idx]
            X_va_cv = X_train.iloc[val_idx]
            y_va_cv = y_train.iloc[val_idx]

            m = xgb.XGBRegressor(
                n_estimators=600, early_stopping_rounds=30,
                objective="reg:absoluteerror",
                eval_metric="mae", verbosity=0, random_state=42,
                **params,
            )
            m.fit(X_tr_cv, y_tr_cv, sample_weight=w_tr_cv,
                  eval_set=[(X_va_cv, y_va_cv)], verbose=False)
            fold_scores.append(mean_absolute_error(y_va_cv, m.predict(X_va_cv)))

        mean_score = float(np.mean(fold_scores))
        if mean_score < best_score:
            best_score, best_params = mean_score, params
            print(f"    [{i:>2}/{len(combos)}]  new best  mae={mean_score:.3f}  {params}")
        else:
            print(f"    [{i:>2}/{len(combos)}]            mae={mean_score:.3f}  {params}")

    print(f"\n  best cv mae: {best_score:.3f}F  with {best_params}")
    return best_params


def train_xgboost(X_train, y_train, X_val, y_val, weights_train, params):
    """Fit the final model with tuned params, sample weights, and MAE loss."""
    model = xgb.XGBRegressor(
        n_estimators=3000,
        objective="reg:absoluteerror",
        early_stopping_rounds=80, eval_metric="mae",
        reg_lambda=1.0, random_state=42, verbosity=0,
        **params,
    )
    model.fit(X_train, y_train, sample_weight=weights_train,
              eval_set=[(X_val, y_val)], verbose=False)
    print(f"  best iteration: {model.best_iteration}  val mae: {model.best_score:.3f}F")
    return model


def metrics(y_true, y_pred):
    return {
        "mae":  float(mean_absolute_error(y_true, y_pred)),
        "rmse": float(mean_squared_error(y_true, y_pred) ** 0.5),
    }


def persistence_baseline(df, target):
    """Simple baseline: repeat the latest relevant observed temperature."""
    if target == "target_temp_next_24h":
        return df["temp_f"]
    elif target == "target_daily_high_next_day":
        return df["temp_daily_high"]
    raise ValueError(target)


def climatology_baseline(df_train, df_test, target):
    """Predict the historical average for the date/time being forecast."""
    df_train = df_train.copy()
    df_test = df_test.copy()

    if target == "target_daily_high_next_day":
        train_dates = pd.Series(df_train.index.tz_convert(SFO_TZ).date,
                                index=df_train.index)
        test_dates = pd.Series(df_test.index.tz_convert(SFO_TZ).date,
                               index=df_test.index)
        df_train["target_doy"] = (
            pd.to_datetime(train_dates) + pd.Timedelta(days=1)
        ).dt.dayofyear
        df_test["target_doy"] = (
            pd.to_datetime(test_dates) + pd.Timedelta(days=1)
        ).dt.dayofyear
        climatology = df_train.groupby("target_doy")[target].mean()
        return df_test["target_doy"].map(climatology)

    train_time = df_train.index.tz_convert(SFO_TZ) + pd.Timedelta(hours=24)
    test_time = df_test.index.tz_convert(SFO_TZ) + pd.Timedelta(hours=24)
    df_train["target_doy"] = train_time.dayofyear
    df_train["target_hour"] = train_time.hour
    df_test["target_doy"] = test_time.dayofyear
    df_test["target_hour"] = test_time.hour
    climatology = df_train.groupby(["target_doy", "target_hour"])[target].mean()
    return df_test.apply(
        lambda r: climatology.get((r["target_doy"], r["target_hour"]), np.nan),
        axis=1,
    )


def evaluate(model, parts, df_full, target):
    X_test, y_test = parts["test"]
    X_train, _     = parts["train"]
    preds = model.predict(X_test)

    m_model = metrics(y_test, preds)

    base_pers = persistence_baseline(df_full, target).loc[X_test.index]
    v = base_pers.notna() & y_test.notna()
    m_pers = metrics(y_test[v], base_pers[v])

    base_clim = climatology_baseline(
        df_full.loc[X_train.index], df_full.loc[X_test.index], target)
    v = base_clim.notna() & y_test.notna()
    m_clim = metrics(y_test[v], base_clim[v])

    print(f"\ntest set results: {target}")
    print(f"  xgboost      mae {m_model['mae']:5.2f}F   rmse {m_model['rmse']:5.2f}F")
    print(f"  persistence  mae {m_pers['mae']:5.2f}F   rmse {m_pers['rmse']:5.2f}F   <- baseline")
    print(f"  climatology  mae {m_clim['mae']:5.2f}F   rmse {m_clim['rmse']:5.2f}F   <- baseline")
    imp = (m_pers["mae"] - m_model["mae"]) / m_pers["mae"] * 100
    print(f"  beats persistence by {imp:+.1f}%")
    return preds, m_model, m_pers, m_clim


def stratified_eval(y_test, preds, target):
    err = pd.DataFrame({"y": y_test.values, "pred": preds})
    err["abs_err"]    = (err["y"] - err["pred"]).abs()
    err["signed_err"] = err["y"] - err["pred"]

    def report(label, mask):
        if mask.sum() == 0:
            print(f"  {label:18s}  (no rows)")
            return
        sub = err[mask]
        print(f"  {label:18s}  n={mask.sum():>5,}   "
              f"mae {sub['abs_err'].mean():5.2f}F   "
              f"signed {sub['signed_err'].mean():+5.2f}F")

    print(f"\nstratified eval: {target}")
    print("  (positive signed err = model under-predicted)")
    report("cold snap <50F",  err["y"] < 50)
    report("normal 50-70F",  (err["y"] >= 50) & (err["y"] < 70))
    report("warm 70-80F",    (err["y"] >= 70) & (err["y"] < 80))
    report("hot 80-90F",     (err["y"] >= 80) & (err["y"] < 90))
    report("extreme 90F+",    err["y"] >= 90)


def plot_predictions(y_test, preds, target):
    residuals = y_test.values - preds
    fig, axes = plt.subplots(2, 2, figsize=(15, 10))
    fig.suptitle(f"prediction analysis - {target}", fontsize=13)

    n = min(720, len(y_test))
    axes[0, 0].plot(y_test.index[:n], y_test.values[:n], color="#378ADD", lw=1, label="actual")
    axes[0, 0].plot(y_test.index[:n], preds[:n], color="#D85A30", lw=1, label="predicted", alpha=0.8)
    axes[0, 0].set(ylabel="temp (F)", title=f"first {n//24} days of test set")
    axes[0, 0].legend(fontsize=9); axes[0, 0].grid(alpha=0.3)
    axes[0, 0].tick_params(axis="x", rotation=20)

    lim = [min(y_test.min(), preds.min()), max(y_test.max(), preds.max())]
    axes[0, 1].scatter(y_test, preds, alpha=0.1, s=5, color="#378ADD")
    axes[0, 1].plot(lim, lim, color="#D85A30", lw=1.5, label="perfect")
    axes[0, 1].set(xlabel="actual (F)", ylabel="predicted (F)", title="predicted vs actual")
    axes[0, 1].legend(fontsize=9); axes[0, 1].grid(alpha=0.3)

    axes[1, 0].hist(residuals, bins=60, color="#7F77DD", alpha=0.8, edgecolor="white")
    axes[1, 0].axvline(0, color="#D85A30", lw=1.5, label=f"mean = {residuals.mean():+.2f}F")
    axes[1, 0].set(xlabel="residual (F)", ylabel="count", title="residual distribution")
    axes[1, 0].legend(fontsize=9); axes[1, 0].grid(alpha=0.3)

    axes[1, 1].scatter(y_test.index, residuals, alpha=0.2, s=4, color="#1D9E75")
    axes[1, 1].axhline(0, color="#D85A30", lw=1)
    axes[1, 1].set(ylabel="residual (F)", title="residuals over test period")
    axes[1, 1].grid(alpha=0.3); axes[1, 1].tick_params(axis="x", rotation=20)

    plt.tight_layout()
    fig.savefig(PLOTS_DIR / f"7_predictions_{target}.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"saved: plots/7_predictions_{target}.png")


def plot_feature_importance(model, feature_cols, target, top_n=25):
    importance = pd.Series(model.feature_importances_, index=feature_cols)
    importance = importance.sort_values(ascending=True).tail(top_n)
    fig, ax = plt.subplots(figsize=(9, max(6, top_n * 0.25)))
    importance.plot(kind="barh", ax=ax, color="#378ADD", alpha=0.85)
    ax.set(title=f"top {top_n} features - {target}", xlabel="importance score")
    ax.grid(axis="x", alpha=0.3)
    plt.tight_layout()
    fig.savefig(PLOTS_DIR / f"8_importance_{target}.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"saved: plots/8_importance_{target}.png")


def plot_error_breakdown(y_test, preds, target):
    local_index = y_test.index.tz_convert(SFO_TZ)
    err = pd.DataFrame({
        "abs_err": np.abs(y_test.values - preds),
        "hour":    local_index.hour,
        "month":   local_index.month,
        "actual":  y_test.values,
    })
    fig, axes = plt.subplots(1, 3, figsize=(18, 4))
    fig.suptitle(f"error breakdown - {target}", fontsize=12)

    by_hour = err.groupby("hour")["abs_err"].mean()
    axes[0].bar(by_hour.index, by_hour.values, color="#378ADD", alpha=0.85)
    axes[0].set(xlabel="hour of day (Pacific)", ylabel="mae (F)", title="mae by hour")
    axes[0].grid(axis="y", alpha=0.3)

    by_month = err.groupby("month")["abs_err"].mean()
    axes[1].bar(by_month.index, by_month.values, color="#D85A30", alpha=0.85)
    axes[1].set(xlabel="month", ylabel="mae (F)", title="mae by month")
    axes[1].grid(axis="y", alpha=0.3)

    err["bin"] = pd.cut(err["actual"], bins=[30, 50, 60, 70, 80, 90, 110],
                        labels=["<50", "50-60", "60-70", "70-80", "80-90", "90+"])
    by_bin = err.groupby("bin", observed=True)["abs_err"].mean()
    axes[2].bar(by_bin.index.astype(str), by_bin.values, color="#7F77DD", alpha=0.85)
    axes[2].set(xlabel="actual temp (F)", ylabel="mae (F)", title="mae by temp bin")
    axes[2].grid(axis="y", alpha=0.3)

    plt.tight_layout()
    fig.savefig(PLOTS_DIR / f"9_errors_{target}.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"saved: plots/9_errors_{target}.png")


def save_model(model, feature_cols, target):
    model_path = MODELS_DIR / f"xgb_{target}.json"
    feat_path  = MODELS_DIR / f"xgb_{target}_features.json"
    model.save_model(model_path)
    feat_path.write_text(json.dumps(feature_cols, indent=2))
    print(f"saved: {model_path} + features")


def run_pipeline(target, tune=True):
    print(f"\ntarget: {target}")

    df = load_features(FEATURES_PATH, target)
    features = select_features(df)
    print(f"\nloaded {len(df):,} rows  {len(features)} features")

    parts = split_data(df, features, target)
    X_tr, y_tr = parts["train"]
    X_va, y_va = parts["val"]
    X_te, y_te = parts["test"]

    for name, (Xp, yp) in parts.items():
        print(f"  {name:5s}  {len(Xp):>6,} rows   "
              f"{Xp.index.min().date()} to {Xp.index.max().date()}")

    w_tr = compute_sample_weights(y_tr, alpha=2.0)
    print(f"\n  sample weights: mean={w_tr.mean():.2f}  "
          f"max={w_tr.max():.2f}  "
          f"(weights >5 applied to {(w_tr > 5).sum()} rows)")

    if tune:
        print("\nhyperparameter tuning")
        best_params = tune_hyperparameters(X_tr, y_tr, w_tr)
    else:
        best_params = {"max_depth": 7, "learning_rate": 0.03,
                       "min_child_weight": 3, "subsample": 0.8,
                       "colsample_bytree": 0.7}

    print("\ntraining final model")
    model = train_xgboost(X_tr, y_tr, X_va, y_va, w_tr, best_params)

    preds, *_ = evaluate(model, parts, df, target)
    stratified_eval(y_te, preds, target)

    print("\nplots")
    plot_predictions(y_te, preds, target)
    plot_feature_importance(model, features, target)
    plot_error_breakdown(y_te, preds, target)

    print("\nsaving")
    save_model(model, features, target)

    return model, preds


if __name__ == "__main__":
    run_pipeline("target_temp_next_24h", tune=True)
    run_pipeline("target_daily_high_next_day", tune=True)

    print("\nall done.")
