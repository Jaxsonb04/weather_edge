import { Card } from "@heroui/react";
import { KPI, KPIGroup } from "@heroui-pro/react";
import { Icon } from "@iconify/react";
import { AnimatedNumber } from "../ui/AnimatedNumber";
import { Reveal } from "../ui/Reveal";
import type { StrategyLab } from "../../lib/strategy";

export function PnlHeader({ s }: { s: StrategyLab }) {
  const sum = s.paper_trading.summary;
  const eq = s.daily_summary;
  const pnlTone = sum.realized_pnl >= 0 ? "text-success" : "text-danger";

  return (
    <Reveal immediate>
      <Card className="rounded-2xl ring-1 ring-border/70">
        <Card.Content className="p-2 sm:p-3">
          <KPIGroup className="flex-wrap">
            <Kpi icon="solar:dollar-minimalistic-bold" title="Realized P&L">
              <AnimatedNumber
                className={`font-display text-2xl font-semibold ${pnlTone}`}
                value={sum.realized_pnl}
                format={{ style: "currency", currency: "USD", maximumFractionDigits: 2 }}
              />
            </Kpi>
            <KPIGroup.Separator />
            <Kpi icon="solar:chart-2-bold" title="ROI" hint="on capital resolved">
              <AnimatedNumber
                className={`font-display text-2xl font-semibold ${sum.roi >= 0 ? "text-success" : "text-danger"}`}
                value={sum.roi}
                format={{ style: "percent", maximumFractionDigits: 1 }}
              />
            </Kpi>
            <KPIGroup.Separator />
            <Kpi icon="solar:target-bold" title="Hit rate" hint={`${sum.win_count}–${sum.loss_count} W–L`}>
              <AnimatedNumber className="font-display text-2xl font-semibold" value={sum.hit_rate} format={{ style: "percent", maximumFractionDigits: 1 }} />
            </Kpi>
            <KPIGroup.Separator />
            <Kpi icon="solar:checklist-minimalistic-bold" title="Closed" hint="settled positions">
              <AnimatedNumber className="font-display text-2xl font-semibold" value={sum.closed_positions} />
            </Kpi>
            <KPIGroup.Separator />
            <Kpi icon="solar:wallet-bold" title="Paper equity" hint={`from $${eq.starting_bankroll?.toLocaleString()}`}>
              <AnimatedNumber
                className="font-display text-2xl font-semibold"
                value={eq.current_equity}
                format={{ style: "currency", currency: "USD", maximumFractionDigits: 0 }}
              />
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
        <Icon icon={icon} className="size-3.5 text-accent" />
        <KPI.Title className="text-xs">{title}</KPI.Title>
      </KPI.Header>
      <KPI.Content className="mt-1">
        <div className="flex items-baseline gap-1">{children}</div>
        {hint && <p className="mt-1.5 text-[11px] text-muted">{hint}</p>}
      </KPI.Content>
    </KPI>
  );
}
