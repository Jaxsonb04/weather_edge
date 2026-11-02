import { Widget } from "@heroui-pro/react/widget";
import type { ExitReasonStats, StrategyLab } from "../../lib/strategy";
import { InfoTip } from "../ui/InfoTip";
import { EXIT_LABELS, percent } from "../../lib/lab-format";

/** Exit routes that resolve a filled position, in reading order. Each lists the
    all-time key first and the weekly `closed_*` spelling as a fallback. */
const RESOLVING: Array<[string, string?]> = [
  ["held_to_settlement"],
  ["take_profit", "closed_take_profit"],
  ["stop_loss", "closed_stop_loss"],
  ["unclassified", "closed_unclassified"],
  ["break_even", "closed_break_even"],
];

interface Row {
  key: string;
  label: string;
  resolved: number;
  wins: number | null;
  hitRate: number;
  roi: number | null;
}

function rows(byReason: Record<string, ExitReasonStats | undefined>): Row[] {
  return RESOLVING.flatMap(([primary, fallback]) => {
    const key = byReason[primary] ? primary : fallback ?? primary;
    const stats = byReason[key];
    const resolved = stats?.resolved ?? 0;
    if (!stats || resolved <= 0) return [];
    // The share is read off the counts printed beside it. The runtime's
    // hit_rate leaves flat outcomes out of its denominator, so pairing it with
    // "wins of resolved" would not add up (267 of 269 is 99.3%, not 99.6%).
    const wins = typeof stats.wins === "number" ? stats.wins : null;
    const hitRate = wins != null ? wins / resolved : typeof stats.hit_rate === "number" ? stats.hit_rate : null;
    if (hitRate == null) return [];
    return [{
      key,
      label: EXIT_LABELS[key] ?? key.replace(/_/g, " "),
      resolved,
      wins,
      hitRate,
      roi: typeof stats.roi === "number" ? stats.roi : null,
    }];
  });
}

/** How a position ends decides whether it makes money: the all-time record by
    exit route. Counts and ratios only; dollar totals would add separate paper
    accounts together. */
export function ExitStudy({ s }: { s: StrategyLab }) {
  const byReason = s.paper_trading?.diagnostics?.by_exit_reason;
  if (!byReason) return null;
  const data = rows(byReason);
  if (!data.length) return null;
  const settled = data.find((row) => row.key === "held_to_settlement");
  const stopped = data.find((row) => row.key === "stop_loss" || row.key === "closed_stop_loss");
  const expired = byReason.expired_unfilled?.positions ?? 0;

  return (
    <Widget className="min-w-0">
      <Widget.Header>
        <div className="min-w-0">
          <div className="flex items-center gap-1">
            <Widget.Title>How positions end</Widget.Title>
            <InfoTip label="the exit study" title="Pooled, all time">
              Every resolved paper position on record, across all accounts including archived ones. Shown as counts
              and rates only: dollar results stay with each account.
            </InfoTip>
          </div>
          <Widget.Description>Share of positions closed at a profit, by how they exited</Widget.Description>
        </div>
      </Widget.Header>
      <Widget.Content className="flex flex-col gap-5">
        {settled && stopped && (
          <p className="text-sm text-muted">
            Held to settlement, positions were profitable{" "}
            <strong className="tnum font-semibold text-foreground">{percent(settled.hitRate, 1)}</strong> of the time. Stop-loss
            exits were profitable <strong className="tnum font-semibold text-foreground">{percent(stopped.hitRate, 0)}</strong>.
          </p>
        )}
        <ul className="flex flex-col gap-3.5" aria-label="Profitable share by exit route">
          {data.map((row) => (
            <li key={row.key} className="min-w-0">
              <div className="mb-1.5 flex items-baseline justify-between gap-3 text-xs">
                <span className="font-medium text-foreground">{row.label}</span>
                <span className="tnum text-muted">
                  {row.wins != null && (
                    <>
                      {row.wins.toLocaleString()} of {row.resolved.toLocaleString()} ·{" "}
                    </>
                  )}
                  <span className="font-semibold text-foreground">{percent(row.hitRate, 1)}</span>
                  {row.roi != null && (
                    <span className={row.roi >= 0 ? "text-success" : "text-danger"}> · ROI {percent(row.roi, 1, "always")}</span>
                  )}
                </span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-default">
                <div
                  className={`h-full rounded-full ${row.hitRate >= 0.5 ? "bg-success" : "bg-danger"}`}
                  style={{ width: `${Math.max(row.hitRate * 100, 0.8)}%` }}
                />
              </div>
            </li>
          ))}
        </ul>
        {expired > 0 && (
          <p className="tnum text-xs text-muted">
            {expired.toLocaleString()} limit orders expired unfilled and never became positions.
          </p>
        )}
      </Widget.Content>
    </Widget>
  );
}
