import { Widget } from "@heroui-pro/react/widget";
import { pct } from "../../lib/data";
import { money, type SideStats, type StrategyLab } from "../../lib/strategy";
import { EXIT_LABELS } from "../../lib/lab-format";
import { InfoTip } from "../ui/InfoTip";
import { Stat } from "../ui/Stat";

/** How positions exited, as proportional bars (empty reasons are dropped). */
export function ExitReasonBars({ reasons, emptyNote }: { reasons?: Record<string, number>; emptyNote?: string }) {
  const rows = Object.entries(reasons ?? {}).filter(([, v]) => v > 0);
  const total = rows.reduce((acc, [, v]) => acc + v, 0);
  if (!total) {
    return <p className="text-sm text-muted">{emptyNote ?? "No exits recorded in this window."}</p>;
  }
  const max = Math.max(...rows.map(([, v]) => v));
  return (
    <ul className="flex flex-col gap-2.5">
      {rows
        .sort(([, a], [, b]) => b - a)
        .map(([k, v]) => (
          <li key={k} className="grid grid-cols-[minmax(0,8.5rem)_1fr_auto] items-center gap-3">
            <span className="truncate text-xs text-foreground">{EXIT_LABELS[k] ?? k.replace(/_/g, " ")}</span>
            <div className="h-1.5 overflow-hidden rounded-full bg-default">
              <div className="bar-grow h-full rounded-full bg-accent" style={{ width: `${(v / max) * 100}%` }} />
            </div>
            <span className="tnum w-6 text-right text-xs text-muted">{v}</span>
          </li>
        ))}
    </ul>
  );
}

/** YES/NO performance rows; a side with no trades is dropped. */
export function SidePerformanceList({ side, emptyNote }: { side?: Record<string, SideStats>; emptyNote?: string }) {
  const rows = Object.entries(side ?? {}).filter(([, v]) => (v?.trades ?? 0) > 0);
  if (!rows.length) {
    return <p className="text-sm text-muted">{emptyNote ?? "No resolved trades to split by side yet."}</p>;
  }
  return (
    <ul className="flex flex-col divide-y divide-separator">
      {rows.map(([name, v]) => (
        <li key={name} className="flex items-center justify-between gap-3 py-2 first:pt-0 last:pb-0">
          <span className="text-xs font-semibold text-foreground">{name}</span>
          <span className="tnum text-xs text-muted">
            {v.trades} trades · {v.hit_rate == null ? "—" : pct(v.hit_rate, 0)} profitable ·{" "}
            <span className={`font-medium ${v.realized_pnl >= 0 ? "text-success" : "text-danger"}`}>
              {money(v.realized_pnl)}
            </span>{" "}
            on {money(v.capital, { sign: "negative-only" })}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** The monitor's standing exit rules, as configured on the runtime. */
export function ExitPolicyCard({ s }: { s: StrategyLab }) {
  const m = s.paper_trading?.monitor;
  if (!m) return null;
  return (
    <Widget className="min-w-0">
      <Widget.Header>
        <div className="min-w-0">
          <div className="flex items-center gap-1">
            <Widget.Title>Exit rules</Widget.Title>
            <InfoTip label="exit rules">
              A monitor re-marks every open position each cycle and closes it when a rule fires. Positions that hit
              no rule are held to settlement.
            </InfoTip>
          </div>
          <Widget.Description>Take-profit closes when the prediction market price approaches the model's estimate, falling back to a percentage of entry cost only without a fresh model read. Stop-loss is a share of entry cost.</Widget.Description>
        </div>
      </Widget.Header>
      <Widget.Content className="flex flex-col gap-4">
        <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
          <Stat label="YES take-profit" value={m.yes_take_profit_pct != null ? `+${m.yes_take_profit_pct}%` : "—"} />
          <Stat label="YES stop-loss" value={m.yes_stop_loss_pct != null ? `−${m.yes_stop_loss_pct}%` : "—"} />
          <Stat label="NO take-profit" value={m.no_take_profit_pct != null ? `+${m.no_take_profit_pct}%` : "—"} />
          <Stat label="NO stop-loss" value={m.no_stop_loss_pct != null ? `−${m.no_stop_loss_pct}%` : "—"} />
        </div>
        {m.model_veto_buffer != null && (
          <p className="text-xs text-muted">
            On a NO stop, a model estimate at least {Math.round(m.model_veto_buffer * 100)} percentage points above
            the exit value and at or above entry cost holds the position instead of selling, until the loss reaches {m.model_veto_max_loss_pct ?? "—"}%.
          </p>
        )}
      </Widget.Content>
    </Widget>
  );
}
