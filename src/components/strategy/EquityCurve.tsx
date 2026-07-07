import { ReferenceLine } from "recharts";
import { ChartTooltip, LineChart, Widget } from "@heroui-pro/react";
import { equitySeries, equitySeriesFromDays, type DayRow, type StrategyLab } from "../../lib/strategy";

interface EquityCurveProps {
  s: StrategyLab;
  /** override the day series (e.g. a single profile's days) */
  days?: DayRow[];
  /** starting bankroll for the override series */
  startingBankroll?: number;
  windowDays?: number;
  title?: string;
  description?: string;
}

export function EquityCurve({ s, days, startingBankroll, windowDays, title, description }: EquityCurveProps) {
  const start = startingBankroll ?? s.daily_summary.starting_bankroll ?? 1000;
  const series = days ? equitySeriesFromDays(days, start) : equitySeries(s);
  const last = series[series.length - 1]?.equity ?? start;
  const win = windowDays ?? s.daily_summary.window_days ?? series.length;
  const up = last >= start;
  const stroke = up ? "var(--color-success)" : "var(--color-danger)";
  const label = `${title ?? "Paper equity curve"} over ${series.length} days, from $${start} to $${last} (${up ? "up" : "down"} over the window).`;

  return (
    <Widget className="w-full">
      <Widget.Header>
        <div>
          <Widget.Title>{title ?? "Paper equity curve"}</Widget.Title>
          <Widget.Description>
            {description ?? `Cumulative realized P&L over the reporting window · ${win}-day view`}
          </Widget.Description>
        </div>
        <Widget.Legend>
          <Widget.LegendItem color={stroke}>equity</Widget.LegendItem>
          <Widget.LegendItem color="var(--color-muted)">start</Widget.LegendItem>
        </Widget.Legend>
      </Widget.Header>
      <Widget.Content>
        <div role="img" aria-label={label}>
        <LineChart data={series} height={240}>
          <defs>
            <linearGradient id="equity-fill" x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor={stroke} stopOpacity={0.18} />
              <stop offset="100%" stopColor={stroke} stopOpacity={0.01} />
            </linearGradient>
          </defs>
          <LineChart.Grid vertical={false} />
          <LineChart.XAxis dataKey="date" tickMargin={8} />
          <LineChart.YAxis width={52} tickFormatter={(v: number) => `$${v}`} domain={["dataMin - 20", "dataMax + 20"]} />
          <ReferenceLine y={start} stroke="var(--color-muted)" strokeDasharray="5 5" strokeWidth={1.25} />
          <LineChart.Line dataKey="equity" name="Equity" stroke={stroke} strokeWidth={2.5} type="monotone" fill="url(#equity-fill)" />
          <LineChart.Tooltip
            content={({ active, label, payload }) => {
              if (!active || !payload?.length) return null;
              const row = payload[0]?.payload as { equity: number; pnl: number };
              return (
                <ChartTooltip>
                  <ChartTooltip.Header>{label}</ChartTooltip.Header>
                  <ChartTooltip.Item>
                    <ChartTooltip.Indicator color={stroke} />
                    <ChartTooltip.Label>Equity</ChartTooltip.Label>
                    <ChartTooltip.Value>${row.equity.toLocaleString()}</ChartTooltip.Value>
                  </ChartTooltip.Item>
                  <ChartTooltip.Item>
                    <ChartTooltip.Label>Cum. P&L</ChartTooltip.Label>
                    <ChartTooltip.Value>{row.pnl >= 0 ? "+" : ""}${row.pnl.toFixed(2)}</ChartTooltip.Value>
                  </ChartTooltip.Item>
                </ChartTooltip>
              );
            }}
          />
        </LineChart>
        </div>
      </Widget.Content>
    </Widget>
  );
}
