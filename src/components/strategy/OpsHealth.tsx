import { Chip } from "@heroui/react/chip";
import { Widget } from "@heroui-pro/react/widget";
import { dayRange } from "../../lib/incidents";
import { decisionCountProvenance, type StrategyLab } from "../../lib/strategy";
import { InfoTip } from "../ui/InfoTip";
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
      name: "Settlement reports (NWS CLI)",
      detail: `${(c.rows ?? 0).toLocaleString()} rows · ${c.lag_days ?? "?"}d behind (max ${c.max_lag_days ?? "?"}d)`,
      ok: (c.lag_days ?? 99) <= (c.max_lag_days ?? 0),
    });
  }
  const n = fh.nws_ground_truth;
  if (n?.available) {
    rows.push({
      name: "NWS observations",
      // The observation date can be ahead of the reference day; that is current, not -1d.
      detail: `latest ${n.latest_date ?? "—"} · ${n.lag_days == null ? "lag unknown" : n.lag_days <= 0 ? "current" : `${n.lag_days}d lag`}`,
      ok: (n.lag_days ?? 99) <= 1,
    });
  }
  const e = fh.emos;
  if (e?.available && e.live_targets?.length) {
    const freshest = Math.min(...e.live_targets.map((t) => t.latest_age_hours));
    const models = e.live_targets[0]?.n_models;
    rows.push({
      name: "EMOS forecasts",
      detail: `${e.live_targets.length} live targets · ${models ?? "?"} members · newest ${freshest.toFixed(1)}h old`,
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

/** Feed freshness and how much the runtime has collected. Alerts are shown at
    the top of the Strategy Lab, so they are not repeated here. */
export function OpsHealth({ s }: { s: StrategyLab }) {
  const collected = s.daily_summary?.data_collected;
  const rows = freshnessRows(s);
  const mvm = s.daily_summary?.model_vs_market;
  const counts = decisionCountProvenance(s);
  // The counts are rows created since the summary window opened, not all-time totals.
  const { window_start: from, window_end: to } = s.daily_summary ?? {};
  // Cached counts cover an older window than the one published, so name no dates for them.
  const window = from && to && !counts.cached ? dayRange(from, to) : null;

  return (
    <div className="grid min-w-0 gap-4 lg:grid-cols-2">
      <Widget className="min-w-0">
        <Widget.Header>
          <div className="min-w-0">
            <div className="flex items-center gap-1">
              <Widget.Title>Pipeline health</Widget.Title>
              <InfoTip label="pipeline health">
                {s.status?.automation_status ??
                  "AWS timers generate the forecast, public signal, Strategy Lab JSON, paper scans, and monitor state."}{" "}
                Statuses come from {(s.source_of_truth ?? "AWS runtime artifacts").toLowerCase()}.
              </InfoTip>
            </div>
            <Widget.Description>Upstream feeds behind every forecast</Widget.Description>
          </div>
        </Widget.Header>
        <Widget.Content>
          {rows.length ? (
            <ul className="flex flex-col divide-y divide-separator">
              {rows.map((r) => (
                <li key={r.name} className="flex items-center justify-between gap-4 py-2.5 first:pt-0 last:pb-0">
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-foreground">{r.name}</p>
                    <p className="tnum text-xs text-muted">{r.detail}</p>
                  </div>
                  <Chip size="sm" variant="soft" color={r.ok ? "success" : "warning"}>
                    <Chip.Label>{r.ok ? "Fresh" : "Check"}</Chip.Label>
                  </Chip>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted">Feed freshness is not published in this artifact.</p>
          )}
        </Widget.Content>
      </Widget>

      <Widget className="min-w-0">
        <Widget.Header>
          <div className="flex min-w-0 flex-col gap-0.5">
            <Widget.Title>Data collected</Widget.Title>
            <Widget.Description>
              Rows journaled {window ? `in the window ${window}` : "in the recent reporting window"}, not all-time totals
            </Widget.Description>
          </div>
        </Widget.Header>
        <Widget.Content className="flex flex-col gap-4">
          {collected ? (
            <>
              {counts.cached && (
                <p role="status" className="text-xs font-medium text-warning">
                  Window counts as of {counts.asOf}; feed status is current.
                </p>
              )}
              <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3">
                {COLLECTED_LABELS.filter(([k]) => collected[k] != null).map(([k, label]) => (
                  <Stat key={k} label={label} value={collected[k].toLocaleString()} />
                ))}
              </div>
            </>
          ) : (
            <p className="text-sm text-muted">Collection counts are not published in this artifact.</p>
          )}
          {mvm?.samples != null && (
            <p className="text-xs text-muted">
              {counts.cached
                ? "As of that cutoff, the model-vs-market gap had been tracked"
                : "Model-vs-market gap tracked"}{" "}
              across <span className="tnum font-medium text-foreground">{mvm.samples.toLocaleString()}</span>{" "}
              snapshots · mean absolute gap {Math.round((mvm.mean_abs_gap ?? 0) * 1000) / 10}pp.
            </p>
          )}
        </Widget.Content>
      </Widget>
    </div>
  );
}
