import { Card } from "@heroui/react";
import { Icon } from "@iconify/react";
import type { HealthAlert, StrategyLab } from "../../lib/strategy";
import { Stat } from "../ui/Stat";

const COLLECTED_LABELS: [string, string][] = [
  ["decision_snapshots", "Decision snapshots"],
  ["probability_snapshots", "Probability snapshots"],
  ["paper_monitor_snapshots", "Monitor snapshots"],
  ["forecast_snapshots", "Forecast snapshots"],
  ["market_snapshots", "Market snapshots"],
  ["paper_orders", "Paper orders"],
];

interface FreshnessRow {
  name: string;
  detail: string;
  ok: boolean;
}

function freshnessRows(s: StrategyLab): FreshnessRow[] {
  const fh = s.forecast_health;
  if (!fh?.available) return [];
  const rows: FreshnessRow[] = [];
  const c = fh.clisfo;
  if (c?.available) {
    rows.push({
      name: "CLISFO settlement feed",
      detail: `${(c.rows ?? 0).toLocaleString()} rows · ${c.lag_days ?? "?"}d behind (max ${c.max_lag_days ?? "?"}d)`,
      ok: (c.lag_days ?? 99) <= (c.max_lag_days ?? 0),
    });
  }
  const n = fh.nws_ground_truth;
  if (n?.available) {
    rows.push({
      name: "NWS ground truth",
      detail: `latest obs ${n.latest_date ?? "—"} · ${n.lag_days ?? "?"}d lag`,
      ok: (n.lag_days ?? 99) <= 1,
    });
  }
  const e = fh.emos;
  if (e?.available && e.live_targets?.length) {
    const freshest = Math.min(...e.live_targets.map((t) => t.latest_age_hours));
    const models = e.live_targets[0]?.n_models;
    rows.push({
      name: "EMOS ensemble blend",
      detail: `${e.live_targets.length} live targets · ${models ?? "?"} members · freshest ${freshest.toFixed(1)}h old`,
      ok: freshest <= (e.max_stale_hours ?? 6),
    });
  }
  const w = fh.nwp;
  if (w?.available && w.recent_targets?.length) {
    const minModels = Math.min(...w.recent_targets.map((t) => t.model_count));
    rows.push({
      name: "NWP model archive",
      detail: `${w.recent_targets.length} recent target-leads · ${minModels}+ models each (floor ${w.min_healthy_models ?? "?"})`,
      ok: minModels >= (w.min_healthy_models ?? 6),
    });
  }
  return rows;
}

function mergedAlerts(s: StrategyLab): HealthAlert[] {
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

/** What the AWS box does on its own, how much it has collected, and whether
    every upstream feed is fresh — published warnings included. */
export function OpsHealth({ s }: { s: StrategyLab }) {
  const collected = s.daily_summary?.data_collected;
  const rows = freshnessRows(s);
  const alerts = mergedAlerts(s);
  const mvm = s.daily_summary?.model_vs_market;

  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Card className="h-full rounded-2xl ring-1 ring-border/70">
        <Card.Header className="flex flex-row items-center gap-2">
          <Icon icon="solar:server-square-bold" className="size-4 text-accent" aria-hidden="true" />
          <Card.Title className="text-base">Autonomous runtime</Card.Title>
        </Card.Header>
        <Card.Content className="space-y-4 pt-0">
          <p className="text-sm leading-relaxed text-muted">
            {s.status?.automation_status ??
              "AWS timers generate the forecast, public signal, Strategy Lab JSON, paper scans, and monitor state."}{" "}
            Everything on this page is that machine's own output — {(s.source_of_truth ?? "AWS runtime artifacts").toLowerCase()}.
          </p>
          {collected && (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              {COLLECTED_LABELS.filter(([k]) => collected[k] != null).map(([k, label]) => (
                <Stat key={k} label={label} value={collected[k].toLocaleString()} />
              ))}
            </div>
          )}
          {mvm?.samples != null && (
            <p className="text-xs text-muted">
              Model-vs-market gap tracked across <span className="tnum font-medium text-foreground">{mvm.samples.toLocaleString()}</span>{" "}
              snapshots · mean absolute gap {Math.round((mvm.mean_abs_gap ?? 0) * 1000) / 10}pp.
            </p>
          )}
        </Card.Content>
      </Card>

      <Card className="h-full rounded-2xl ring-1 ring-border/70">
        <Card.Header className="flex flex-row items-center gap-2">
          <Icon icon="solar:heart-pulse-bold" className="size-4 text-accent" aria-hidden="true" />
          <Card.Title className="text-base">Pipeline health</Card.Title>
        </Card.Header>
        <Card.Content className="pt-0">
          <ul className="divide-y divide-border/50">
            {rows.map((r) => (
              <li key={r.name} className="flex items-center justify-between gap-3 py-2.5">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-foreground">{r.name}</p>
                  <p className="text-xs text-muted">{r.detail}</p>
                </div>
                <span
                  className={`flex shrink-0 items-center gap-1.5 font-mono text-[10px] font-semibold uppercase ${r.ok ? "text-success" : "text-warning"}`}
                >
                  <span className={`size-1.5 rounded-full ${r.ok ? "bg-success" : "bg-warning"}`} aria-hidden="true" />
                  {r.ok ? "fresh" : "check"}
                </span>
              </li>
            ))}
          </ul>
          {!!alerts.length && (
            <ul className="mt-3 space-y-2">
              {alerts.map((a) => (
                <li
                  key={a.code ?? a.title}
                  className="flex gap-2.5 rounded-lg bg-warning-soft p-3 text-xs text-foreground ring-1 ring-warning/25"
                >
                  <Icon icon="solar:danger-triangle-bold" className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden="true" />
                  <span>
                    <span className="font-semibold">{a.title}.</span> {a.detail} {a.action}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card.Content>
      </Card>
    </div>
  );
}
