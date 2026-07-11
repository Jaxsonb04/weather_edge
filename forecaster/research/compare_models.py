"""Compare XGBoost and LSTM predictions on the held-out test set."""

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import xgboost as xgb
import json
from scipy import stats
from sklearn.metrics import mean_absolute_error, mean_squared_error
from pathlib import Path

try:
    from .forecast_validation import chronological_unit_split_masks
except ImportError:  # Direct invocation: python research/compare_models.py
    from forecast_validation import chronological_unit_split_masks

FEATURES_PATH = "weather_features.csv"
MODELS_DIR    = Path("models")
PLOTS_DIR     = Path("plots")


def metrics(y, p):
    return {"mae": float(mean_absolute_error(y, p)),
            "rmse": float(mean_squared_error(y, p) ** 0.5)}


def load_xgboost_preds(target, df):
    """reload trained xgboost and predict on the test slice."""
    feat_path  = MODELS_DIR / f"xgb_{target}_features.json"
    model_path = MODELS_DIR / f"xgb_{target}.json"
    features = json.loads(feat_path.read_text())

    model = xgb.XGBRegressor()
    model.load_model(model_path)

    df = df.dropna(subset=[target])
    masks = chronological_unit_split_masks(df.index, target)
    X_val, y_val   = df[features].loc[masks["val"]],  df[target].loc[masks["val"]]
    X_test, y_test = df[features].loc[masks["test"]], df[target].loc[masks["test"]]

    return {
        "val_pred":  pd.Series(model.predict(X_val),  index=y_val.index),
        "val_true":  y_val,
        "test_pred": pd.Series(model.predict(X_test), index=y_test.index),
        "test_true": y_test,
    }


def load_xgboost_model(target):
    features = json.loads((MODELS_DIR / f"xgb_{target}_features.json").read_text())
    model = xgb.XGBRegressor()
    model.load_model(MODELS_DIR / f"xgb_{target}.json")
    return model, features


def feature_importance_payload(target, top_n=20):
    model, features = load_xgboost_model(target)
    importance = pd.Series(model.feature_importances_, index=features)
    importance = importance.sort_values(ascending=False).head(top_n)
    return [
        {"feature": str(feature), "importance": float(value)}
        for feature, value in importance.items()
    ]


def load_lstm_preds(target):
    """Load cached LSTM predictions from CSV."""
    val_df  = pd.read_csv(MODELS_DIR / f"lstm_{target}_val_preds.csv",
                          index_col=0, parse_dates=True)
    test_df = pd.read_csv(MODELS_DIR / f"lstm_{target}_test_preds.csv",
                          index_col=0, parse_dates=True)
    return {
        "val_pred":  val_df["pred"],
        "val_true":  val_df["actual"],
        "test_pred": test_df["pred"],
        "test_true": test_df["actual"],
    }


def persistence_baseline(df_full, target, index):
    src = df_full["temp_f"] if target == "target_temp_next_24h" \
                            else df_full["temp_daily_high"]
    return src.loc[index]


def print_comparison(target, xgb_p, lstm_p, df_full):
    """side-by-side metrics on rows where both models made a prediction."""
    common = xgb_p["test_pred"].index.intersection(lstm_p["test_pred"].index)
    y = xgb_p["test_true"].loc[common]

    m_xgb  = metrics(y, xgb_p["test_pred"].loc[common])
    m_lstm = metrics(y, lstm_p["test_pred"].loc[common])

    base  = persistence_baseline(df_full, target, common)
    valid = base.notna() & y.notna()
    m_base = metrics(y[valid], base[valid])

    print(f"\nhead-to-head: {target}")
    print(f"  (test set, n={len(common):,} common rows)")
    print(f"   model          mae       rmse")
    print(f"   xgboost     {m_xgb['mae']:5.2f}F   {m_xgb['rmse']:5.2f}F")
    print(f"   lstm        {m_lstm['mae']:5.2f}F   {m_lstm['rmse']:5.2f}F")
    print(f"   persistence {m_base['mae']:5.2f}F   {m_base['rmse']:5.2f}F   <- baseline")

    winner = "lstm" if m_lstm["mae"] < m_xgb["mae"] else "xgboost"
    diff   = abs(m_lstm["mae"] - m_xgb["mae"])
    print(f"   {winner} wins by {diff:.3f}F mae")
    return common, m_xgb, m_lstm, m_base


def plot_comparison(target, xgb_p, lstm_p, common):
    y         = xgb_p["test_true"].loc[common]
    xgb_test  = xgb_p["test_pred"].loc[common]
    lstm_test = lstm_p["test_pred"].loc[common]

    fig, axes = plt.subplots(1, 2, figsize=(13, 5.5))
    fig.suptitle(f"xgboost vs lstm - {target}", fontsize=13)

    lim = [min(y.min(), xgb_test.min(), lstm_test.min()),
           max(y.max(), xgb_test.max(), lstm_test.max())]

    for ax, preds, name in [(axes[0], xgb_test, "xgboost"),
                             (axes[1], lstm_test, "lstm")]:
        ax.scatter(y, preds, alpha=0.1, s=4, color="#378ADD")
        ax.plot(lim, lim, color="#D85A30", lw=1.5, label="perfect")
        mae = mean_absolute_error(y, preds)
        ax.set(xlabel="actual (F)", ylabel="predicted (F)",
               title=f"{name}  mae {mae:.2f}F")
        ax.legend(fontsize=9); ax.grid(alpha=0.3)
        ax.set_xlim(lim); ax.set_ylim(lim)

    plt.tight_layout()
    fname = f"10_compare_{target}.png"
    fig.savefig(PLOTS_DIR / fname, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  saved: plots/{fname}")


def calibrate(val_pred, val_true):
    """estimate residual distribution from val set. returns (mean, std).
    use val not test to keep the test score unbiased."""
    residuals = val_true - val_pred
    return float(residuals.mean()), float(residuals.std())


def prob_exceeds(point_pred, threshold, mu, sigma):
    """P(actual > threshold) given point prediction and residual distribution."""
    return 1.0 - stats.norm.cdf(threshold, loc=point_pred + mu, scale=sigma)


def evaluate_calibration(test_pred, test_true, mu, sigma, n_bins=10):
    """reliability diagram: bin by predicted probability, compare to observed frequency."""
    threshold = float(np.median(test_true))

    probs   = np.array([prob_exceeds(p, threshold, mu, sigma) for p in test_pred])
    actuals = (test_true.values > threshold).astype(int)

    bin_edges = np.linspace(0, 1, n_bins + 1)
    bin_idx   = np.clip(np.digitize(probs, bin_edges) - 1, 0, n_bins - 1)

    rows = []
    for b in range(n_bins):
        mask = bin_idx == b
        if mask.sum() < 5:
            continue
        rows.append({
            "bin":         (bin_edges[b] + bin_edges[b+1]) / 2,
            "pred_prob":   float(probs[mask].mean()),
            "actual_freq": float(actuals[mask].mean()),
            "count":       int(mask.sum()),
        })
    return threshold, pd.DataFrame(rows)


def plot_calibration(target, xgb_calib, lstm_calib, xgb_sigma, lstm_sigma):
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.5))
    fig.suptitle(f"calibration - {target}", fontsize=13)

    for ax, (threshold, calib_df), name, sigma in [
        (axes[0], xgb_calib,  "xgboost", xgb_sigma),
        (axes[1], lstm_calib, "lstm",    lstm_sigma),
    ]:
        ax.plot([0, 1], [0, 1], color="#D85A30", lw=1.5, label="perfect calibration")
        ax.plot(calib_df["pred_prob"], calib_df["actual_freq"],
                marker="o", color="#378ADD", lw=2, label=name)

        for _, r in calib_df.iterrows():
            ax.scatter(r["pred_prob"], r["actual_freq"],
                       s=r["count"] / 5, alpha=0.3, color="#378ADD")

        ax.set(xlabel="predicted P(actual > threshold)",
               ylabel="observed frequency",
               title=f"{name}  threshold={threshold:.1f}F  sigma={sigma:.2f}F",
               xlim=(0, 1), ylim=(0, 1))
        ax.legend(fontsize=9); ax.grid(alpha=0.3)

    plt.tight_layout()
    fname = f"11_calibration_{target}.png"
    fig.savefig(PLOTS_DIR / fname, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  saved: plots/{fname}")


def run_target(target, df_full):
    print(f"\ncomparing: {target}")

    xgb_p  = load_xgboost_preds(target, df_full)
    lstm_p = load_lstm_preds(target)

    common, m_xgb, m_lstm, m_base = print_comparison(target, xgb_p, lstm_p, df_full)
    plot_comparison(target, xgb_p, lstm_p, common)

    xgb_mu,  xgb_sigma  = calibrate(xgb_p["val_pred"],  xgb_p["val_true"])
    lstm_mu, lstm_sigma = calibrate(lstm_p["val_pred"], lstm_p["val_true"])
    print(f"  xgboost residuals (val):  mean={xgb_mu:+.3f}  sigma={xgb_sigma:.3f}F")
    print(f"  lstm residuals    (val):  mean={lstm_mu:+.3f}  sigma={lstm_sigma:.3f}F")

    xgb_calib  = evaluate_calibration(xgb_p["test_pred"].loc[common],
                                      xgb_p["test_true"].loc[common],
                                      xgb_mu, xgb_sigma)
    lstm_calib = evaluate_calibration(lstm_p["test_pred"].loc[common],
                                      lstm_p["test_true"].loc[common],
                                      lstm_mu, lstm_sigma)
    plot_calibration(target, xgb_calib, lstm_calib, xgb_sigma, lstm_sigma)

    print("\nexample probability estimates (lstm)")
    example_pred = float(lstm_p["test_pred"].mean())
    print(f"  using mean test prediction: {example_pred:.1f}F")
    for thresh in [example_pred - 5, example_pred, example_pred + 5, example_pred + 10]:
        p = prob_exceeds(example_pred, thresh, lstm_mu, lstm_sigma)
        print(f"    P(actual > {thresh:5.1f}F) = {p:.1%}")

    return {
        "xgboost": m_xgb,
        "lstm": m_lstm,
        "persistence": m_base,
        "xgb_sigma": xgb_sigma,
        "lstm_sigma": lstm_sigma,
        "xgb_feature_importance": feature_importance_payload(target),
    }


if __name__ == "__main__":
    df = pd.read_csv(FEATURES_PATH, index_col=0, parse_dates=True)
    results = {}
    for target in ["target_temp_next_24h", "target_daily_high_next_day"]:
        results[target] = run_target(target, df)

    print("\nfinal scoreboard")
    print(f"  {'target':<35} {'xgboost':>10} {'lstm':>10} {'baseline':>10}")
    for t, r in results.items():
        print(f"  {t:<35} "
              f"{r['xgboost']['mae']:>7.2f}F  "
              f"{r['lstm']['mae']:>7.2f}F  "
              f"{r['persistence']['mae']:>7.2f}F")

    Path("model_compare_results.json").write_text(json.dumps(results, indent=2))
    print("\nwrote model_compare_results.json")
