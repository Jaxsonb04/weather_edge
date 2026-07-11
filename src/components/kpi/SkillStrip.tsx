import { Card } from "@heroui/react";
import { KPI, KPIGroup } from "@heroui-pro/react";
import { Icon } from "@iconify/react";
import { AnimatedNumber } from "../ui/AnimatedNumber";
import { Reveal } from "../ui/Reveal";
import { skillStatus, type ForecastData, type TradingSignal } from "../../lib/data";

interface Metric {
  icon: string;
  title: string;
  value: number | null;
  kind: "pct" | "count" | "temp";
  hint?: string;
}

export function SkillStrip({ forecast, signal }: { forecast: ForecastData; signal: TradingSignal }) {
  const c = signal.calibration;
  const metrics: Metric[] = [
    { icon: "solar:target-bold", title: "Brier skill", value: c?.brier_skill ?? null, kind: "pct", hint: "vs climatology" },
    { icon: "solar:ranking-bold", title: "Rank-prob. skill", value: c?.ranked_probability_skill ?? null, kind: "pct", hint: "ordered bins" },
    { icon: "solar:bullseye-bold", title: "Top-bin accuracy", value: c?.top_bin_accuracy ?? null, kind: "pct", hint: "modal bracket" },
    { icon: "solar:checklist-minimalistic-bold", title: "Settled bins", value: c?.n ?? null, kind: "count", hint: "scored out-of-sample" },
    { icon: "solar:calendar-bold", title: "History", value: forecast.n_years ?? null, kind: "count", hint: `${forecast.n_days_observed?.toLocaleString() ?? "—"} days` },
    { icon: "solar:graph-new-bold", title: "Forecast σ", value: forecast.lstm_sigma ?? null, kind: "temp", hint: "held-out residual" },
  ];

  return (
    <Reveal immediate className="-mt-9 mb-12 sm:-mt-12">
      <Card className="rounded-2xl ring-1 ring-border/70">
        <Card.Content className="p-2 sm:p-3">
          <KPIGroup className="flex-wrap">
            {metrics.map((m, i) => (
              <div key={m.title} className="contents">
                {i > 0 && <KPIGroup.Separator />}
                <Metric m={m} />
              </div>
            ))}
          </KPIGroup>
        </Card.Content>
      </Card>
    </Reveal>
  );
}

function Metric({ m }: { m: Metric }) {
  const isPct = m.kind === "pct";
  return (
    <KPI className="min-w-[9.5rem] flex-1 bg-transparent px-3 py-2 ring-0">
      <KPI.Header className="gap-1.5">
        <Icon icon={m.icon} className="size-3.5 text-accent" />
        <KPI.Title className="text-xs">{m.title}</KPI.Title>
      </KPI.Header>
      <KPI.Content className="mt-1">
        <div className="flex items-baseline gap-1">
          {m.value == null ? (
            <span className="font-display text-2xl font-semibold">—</span>
          ) : (
            <AnimatedNumber
              className="font-display text-2xl font-semibold"
              value={m.value}
              format={
                isPct
                  ? { style: "percent", maximumFractionDigits: 1 }
                  : { maximumFractionDigits: m.value < 10 ? 2 : 0 }
              }
            />
          )}
          {m.kind === "temp" && <span className="text-sm text-muted">°F</span>}
          {m.kind === "count" && m.title === "History" && <span className="text-sm text-muted">yrs</span>}
        </div>
        {isPct && m.value != null && (
          <KPI.Progress
            className="mt-2"
            value={Math.max(0, Math.min(100, m.value * 100))}
            status={skillStatus(m.value * 100)}
          />
        )}
        {m.hint && <p className="mt-1.5 text-[11px] text-muted">{m.hint}</p>}
      </KPI.Content>
    </KPI>
  );
}
