import type { HealthAlert, StrategyLab } from "./strategy";

/** "v6" from "research-target-roi-v6"; null when no version is published. */
export function policyMarker(version?: string | null): string | null {
  const match = version?.match(/(?:^|-)v(\d+)(?:$|-)/i)?.[1];
  return match ? `v${match}` : null;
}

/** Exit routes as the runtime names them, in plain words. The weekly exit
    counts use `closed_*` keys; the all-time diagnostics use bare ones. */
export const EXIT_LABELS: Record<string, string> = {
  closed_take_profit: "Take-profit",
  take_profit: "Take-profit",
  closed_stop_loss: "Stop-loss",
  stop_loss: "Stop-loss",
  closed_unclassified: "Reason unavailable",
  unclassified: "Reason unavailable",
  held_to_settlement: "Held to settlement",
  closed_break_even: "Break-even",
  break_even: "Break-even",
  expired_unfilled: "Expired unfilled",
};

/** A ratio as a percentage with a true minus sign, matching money(). */
export function percent(
  n: number | null | undefined,
  digits = 1,
  sign: "always" | "negative-only" = "negative-only",
): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const body = `${(Math.abs(n) * 100).toFixed(digits)}%`;
  if (n < 0 && Number((Math.abs(n) * 100).toFixed(digits)) !== 0) return `−${body}`;
  return sign === "always" && n > 0 ? `+${body}` : body;
}

/** Gate names as the runtime publishes them, in plain words. The publisher
    truncates long reasons (the same-day rule arrives cut off mid-word), so
    callers keep the raw string available as a title. Unknown reasons fall back
    to the raw text with a capital letter. */
const REJECTION_LABELS: Record<string, string> = {
  "lower-bound edge": "Lower-bound edge",
  edge: "Edge",
  edge_lcb: "Edge (lower confidence bound)",
  "source spread": "Source spread",
  spread: "Market spread",
  "posterior probability": "Posterior probability",
  "bid size": "Bid size",
  bid: "Bid",
  "NO ask is not tradeable": "NO ask not tradeable",
  "1c/2c tail requires exceptional support": "1–2¢ tail support",
  "all-in cost": "All-in cost",
  "model/market gap": "Model/market gap",
};

export function rejectionLabel(reason: string): string {
  if (REJECTION_LABELS[reason]) return REJECTION_LABELS[reason];
  if (/^live paper entry requires min_lead_days=1/i.test(reason)) return "Same-day entry (live needs 1-day lead)";
  const trimmed = reason.trim();
  return trimmed ? trimmed[0].toUpperCase() + trimmed.slice(1) : "Unlabelled gate";
}

/** Published forecast warnings and runtime alerts, de-duplicated, minus "ok". */
export function mergedAlerts(s: StrategyLab): HealthAlert[] {
  const seen = new Set<string>();
  const out: HealthAlert[] = [];
  for (const a of [...(s.forecast_health?.warnings ?? []), ...(s.status?.alerts ?? [])]) {
    const key = a.code ?? a.title;
    if ((a.level ?? "warning") === "ok" || seen.has(key)) continue;
    seen.add(key);
    out.push(a);
  }
  return out;
}
