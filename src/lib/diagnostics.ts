import { useResource } from "./data";

export interface ModelMetric {
  mae: number;
  rmse: number;
}
export interface Diagnostics {
  models: {
    lstm: ModelMetric;
    xgboost: ModelMetric;
    persistence: ModelMetric;
    lstm_sigma: number;
    xgb_sigma: number;
  };
  feature_importance: { feature: string; importance: number }[];
  ab: {
    n_days: number;
    mae_lstm: number;
    mae_xgb: number;
    lift_pct: number;
    win_rate: number;
    p_diebold_mariano: number;
    cohens_d: number;
    ci_low: number;
    ci_high: number;
    significant: boolean;
  };
  held_out: { date: string; actual: number; lstm: number; xgb: number }[];
  bootstrap: { labels: number[]; counts: number[] };
}

export const useDiagnostics = () => useResource<Diagnostics>("diagnostics.json");

const round2 = (n: number) => Math.round(n * 100) / 100;

/** Production (LSTM) vs challenger (XGBoost) vs persistence baseline — MAE/RMSE. */
export function modelCompareSeries(d: Diagnostics) {
  const m = d.models;
  return [
    { model: "Persistence", mae: round2(m.persistence.mae), rmse: round2(m.persistence.rmse) },
    { model: "XGBoost", mae: round2(m.xgboost.mae), rmse: round2(m.xgboost.rmse) },
    { model: "LSTM", mae: round2(m.lstm.mae), rmse: round2(m.lstm.rmse) },
  ];
}

const FEATURE_LABELS: Record<string, string> = {
  temp_daily_high: "Daily high",
  temp_roll_mean_24h: "24h mean",
  next_doy_cos: "Day-of-year (cos)",
  month_sin: "Month (sin)",
  doy_cos: "Day-of-year",
  cloud_cover_pct: "Cloud cover",
  temp_max_168h: "7-day max",
  temp_max_72h: "3-day max",
};
export function featureSeries(d: Diagnostics) {
  return [...d.feature_importance]
    .sort((a, b) => a.importance - b.importance)
    .map((f) => ({
      feature: FEATURE_LABELS[f.feature] ?? f.feature.replace(/_/g, " "),
      importance: Math.round(f.importance * 1000) / 10, // percent of total gain
    }));
}

/** Held-out predicted (LSTM) vs actual — for the calibration-of-fit scatter. */
export function heldOutSeries(d: Diagnostics) {
  return d.held_out.map((p) => ({ actual: p.actual, lstm: p.lstm }));
}
