import { Card } from "@heroui/react";
import { Icon } from "@iconify/react";
import { pct } from "../../lib/data";
import { money, type SideStats, type StrategyLab } from "../../lib/strategy";
import { Stat } from "../ui/Stat";

const EXIT_LABELS: Record<string, string> = {
  closed_take_profit: "Take-profit",
  closed_stop_loss: "Stop-loss",
  held_to_settlement: "Held to settlement",
  closed_break_even: "Break-even",
  expired_unfilled: "Expired unfilled",
};

/** How positions exited, as proportional bars. Reused per profile and combined. */
export function ExitReasonBars({ reasons, emptyNote }: { reasons?: Record<string, number>; emptyNote?: string }) {
  const rows = Object.entries(reasons ?? {});
  const total = rows.reduce((acc, [, v]) => acc + v, 0);
  if (!total) {
    return <p className="text-sm text-muted">{emptyNote ?? "No exits recorded in this window."}</p>;
  }
  const max = Math.max(...rows.map(([, v]) => v));
  return (
    <ul className="space-y-2">
      {rows
        .sort(([, a], [, b]) => b - a)
        .map(([k, v]) => (
          <li key={k} className="flex items-center gap-3">
            <span className="w-36 shrink-0 truncate text-xs text-muted">{EXIT_LABELS[k] ?? k.replace(/_/g, " ")}</span>
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-foreground/8">
              <div className="h-full rounded-full bg-accent/80" style={{ width: `${(v / max) * 100}%` }} />
            </div>
            <span className="tnum w-6 shrink-0 text-right font-mono text-[11px] text-muted">{v}</span>
          </li>
        ))}
    </ul>
  );
}

/** YES/NO performance rows. Reused per profile and combined. */
export function SidePerformanceList({ side, emptyNote }: { side?: Record<string, SideStats>; emptyNote?: string }) {
  const rows = Object.entries(side ?? {});
  if (!rows.length) {
    return <p className="text-sm text-muted">{emptyNote ?? "No resolved trades to split by side yet."}</p>;
  }
  return (
    <ul className="divide-y divide-border/50">
      {rows.map(([name, v]) => (
        <li key={name} className="flex items-center justify-between gap-3 py-2">
          <span className="rounded bg-foreground/8 px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase text-muted">
            {name}
          </span>
          <span className="text-xs text-muted">
            {v.trades} trades · {v.hit_rate == null ? "—" : pct(v.hit_rate, 0)} hit ·{" "}
            <span className={`tnum font-medium ${v.realized_pnl >= 0 ? "text-success" : "text-danger"}`}>
              {money(v.realized_pnl)}
            </span>{" "}
            on ${v.capital.toFixed(2)}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** The monitor's standing exit rules plus how exits actually played out (both books). */
export function ExitPolicyCard({ s }: { s: StrategyLab }) {
  const m = s.paper_trading?.monitor;
  return (
    <Card className="rounded-2xl ring-1 ring-border/70">
      <Card.Header className="flex flex-row items-center gap-2">
        <Icon icon="solar:route-bold" className="size-4 text-accent" aria-hidden="true" />
        <div>
          <Card.Title className="text-base">Exit discipline</Card.Title>
          <Card.Description className="text-sm text-muted">
            A monitor re-marks every open position each cycle and closes it by rule, not by mood
          </Card.Description>
        </div>
      </Card.Header>
      <Card.Content className="space-y-5 pt-0">
        {m && (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="YES take-profit" value={m.yes_take_profit_pct != null ? `+${m.yes_take_profit_pct}%` : "—"} tone="pos" />
            <Stat label="YES stop-loss" value={m.yes_stop_loss_pct != null ? `−${m.yes_stop_loss_pct}%` : "—"} tone="neg" />
            <Stat label="NO take-profit" value={m.no_take_profit_pct != null ? `+${m.no_take_profit_pct}%` : "—"} tone="pos" />
            <Stat label="NO stop-loss" value={m.no_stop_loss_pct != null ? `−${m.no_stop_loss_pct}%` : "—"} tone="neg" />
          </div>
        )}
        {m?.model_veto_buffer != null && (
          <p className="text-xs leading-relaxed text-muted">
            Model veto: if the live model moves {Math.round(m.model_veto_buffer * 100)}pp+ against a position, the monitor can
            cut it early, capped at a {m.model_veto_max_loss_pct ?? "—"}% loss of cost.
          </p>
        )}
        <div className="grid gap-6 border-t border-border/50 pt-4 md:grid-cols-2">
          <div>
            <p className="mb-2 text-[11px] uppercase tracking-wide text-muted">How the window's positions exited</p>
            <ExitReasonBars reasons={s.daily_summary?.exit_reasons} />
          </div>
          <div>
            <p className="mb-2 text-[11px] uppercase tracking-wide text-muted">Performance by side</p>
            <SidePerformanceList side={s.daily_summary?.side_performance} />
          </div>
        </div>
      </Card.Content>
    </Card>
  );
}
