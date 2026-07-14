import { Card } from "@heroui/react/card";
import { KPI } from "@heroui-pro/react/kpi";
import { KPIGroup } from "@heroui-pro/react/kpi-group";
import { Icon } from "@iconify/react/offline";
import { AnimatedNumber } from "../ui/AnimatedNumber";
import { Reveal } from "../ui/Reveal";
import { money, type StrategyLab } from "../../lib/strategy";

export function PnlHeader({ s }: { s: StrategyLab }) {
  const sum = s.paper_trading.summary;
  const account = s.accounting;
  const allTimePnl = account?.all_time_realized_pnl ?? sum.realized_pnl;
  const windowPnl = account?.window_realized_pnl ?? s.daily_summary.totals?.realized_pnl ?? 0;
  const weekly = account?.goal;
  const weeklyHint = weekly
    ? `5% research objective · ${money(weekly.remaining_pnl)} remaining · ${weekly.current_week_evidence_qualified ? `${weekly.completed_week_success_streak} verified-week streak` : "full exec-v3 week pending"}`
    : "5% research objective";
  const pnlTone = allTimePnl >= 0 ? "text-success" : "text-danger";

  return (
    <Reveal immediate>
      <Card className="rounded-2xl ring-1 ring-border/70">
        <Card.Content className="p-2 sm:p-3">
          <KPIGroup className="flex-wrap">
            <Kpi icon="solar:dollar-minimalistic-bold" title="Realized P&L" hint="all time">
              <AnimatedNumber
                className={`font-display text-2xl font-semibold ${pnlTone}`}
                value={allTimePnl}
                format={{ style: "currency", currency: "USD", maximumFractionDigits: 2 }}
              />
            </Kpi>
            <KPIGroup.Separator />
            <Kpi icon="solar:calendar-bold" title="Weekly realized P&L" hint="paper-shared · Mon 00:00 PT">
              <AnimatedNumber
                className={`font-display text-2xl font-semibold ${(weekly?.weekly_realized_pnl ?? windowPnl) >= 0 ? "text-success" : "text-danger"}`}
                value={weekly?.weekly_realized_pnl ?? windowPnl}
                format={{ style: "currency", currency: "USD", maximumFractionDigits: 2 }}
              />
            </Kpi>
            <KPIGroup.Separator />
            <Kpi icon="solar:wallet-bold" title="Realized equity" hint={`from ${money(account?.initial_capital ?? s.daily_summary.starting_bankroll ?? 1000, { digits: 0, sign: "negative-only" })}`}>
              <AnimatedNumber
                className="font-display text-2xl font-semibold"
                value={account?.realized_equity ?? s.daily_summary.current_equity}
                format={{ style: "currency", currency: "USD", maximumFractionDigits: 2 }}
              />
            </Kpi>
            <KPIGroup.Separator />
            <Kpi icon="solar:chart-2-bold" title="Weekly return" hint={weeklyHint}>
              {weekly?.weekly_realized_return == null ? <span className="font-display text-2xl font-semibold">—</span> : (
                <AnimatedNumber
                  className={`font-display text-2xl font-semibold ${weekly.weekly_realized_return >= weekly.target_return ? "text-success" : weekly.weekly_realized_return < 0 ? "text-danger" : "text-foreground"}`}
                  value={weekly.weekly_realized_return}
                  format={{ style: "percent", maximumFractionDigits: 2 }}
                />
              )}
            </Kpi>
            <KPIGroup.Separator />
            <Kpi icon="solar:pie-chart-2-bold" title="ROI" hint="on resolved capital">
              {account?.roi_on_resolved_capital == null ? <span className="font-display text-2xl font-semibold">—</span> : (
                <AnimatedNumber
                  className={`font-display text-2xl font-semibold ${account.roi_on_resolved_capital >= 0 ? "text-success" : "text-danger"}`}
                  value={account.roi_on_resolved_capital}
                  format={{ style: "percent", maximumFractionDigits: 1 }}
                />
              )}
            </Kpi>
            <KPIGroup.Separator />
            <Kpi icon="solar:graph-up-bold" title="Marked equity" hint={account?.mark_coverage?.replaceAll("_", " ") ?? "mark coverage unknown"}>
              {account?.marked_equity == null ? <span className="font-display text-2xl font-semibold">—</span> : (
                <AnimatedNumber
                  className="font-display text-2xl font-semibold"
                  value={account.marked_equity}
                  format={{ style: "currency", currency: "USD", maximumFractionDigits: 2 }}
                />
              )}
            </Kpi>
          </KPIGroup>
        </Card.Content>
      </Card>
    </Reveal>
  );
}

function Kpi({ icon, title, hint, children }: { icon: string; title: string; hint?: string; children: React.ReactNode }) {
  return (
    <KPI className="min-w-[9.5rem] flex-1 bg-transparent px-3 py-2 ring-0">
      <KPI.Header className="gap-1.5">
        <Icon icon={icon} className="size-3.5 text-accent" aria-hidden="true" />
        <KPI.Title className="text-xs">{title}</KPI.Title>
      </KPI.Header>
      <KPI.Content className="mt-1">
        <div className="flex items-baseline gap-1">{children}</div>
        {hint && <p className="mt-1.5 text-[11px] text-muted">{hint}</p>}
      </KPI.Content>
    </KPI>
  );
}
