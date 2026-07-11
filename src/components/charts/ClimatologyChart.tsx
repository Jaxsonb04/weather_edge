import { AreaChart } from "@heroui-pro/react/area-chart";
import { ChartTooltip } from "@heroui-pro/react/chart-tooltip";
import { Widget } from "@heroui-pro/react/widget";
import { climatologySeries, type ForecastData } from "../../lib/data";

export function ClimatologyChart({ forecast }: { forecast: ForecastData }) {
  const series = climatologySeries(forecast);
  return (
    <Widget className="w-full">
      <Widget.Header>
        <div>
          <Widget.Title>SFO daily-high climatology</Widget.Title>
          <Widget.Description>Mean with the 10th–90th percentile seasonal band</Widget.Description>
        </div>
        <Widget.Legend>
          <Widget.LegendItem color="var(--temp-warm)">p10–p90</Widget.LegendItem>
          <Widget.LegendItem color="var(--accent)">mean</Widget.LegendItem>
        </Widget.Legend>
      </Widget.Header>
      <Widget.Content>
        <AreaChart data={series} height={260}>
          <defs>
            <linearGradient id="band-fill" x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor="var(--temp-warm)" stopOpacity={0.24} />
              <stop offset="100%" stopColor="var(--temp-warm)" stopOpacity={0.04} />
            </linearGradient>
          </defs>
          <AreaChart.Grid vertical={false} />
          <AreaChart.XAxis dataKey="label" tickMargin={8} interval={0} />
          <AreaChart.YAxis width={36} tickFormatter={(v: number) => `${v}°`} />
          <AreaChart.Area dataKey="p10" stackId="band" stroke="none" fill="transparent" type="monotone" dot={false} />
          <AreaChart.Area dataKey="band" name="p10–p90" stackId="band" stroke="none" fill="url(#band-fill)" type="monotone" dot={false} />
          <AreaChart.Area dataKey="mean" name="Mean high" stroke="var(--accent)" strokeWidth={2.75} fill="none" type="monotone" dot={false} />
          <AreaChart.Tooltip
            content={({ active, label, payload }) => {
              if (!active || !payload?.length) return null;
              const row = payload[0]?.payload as { mean: number; p10: number; p90: number };
              return (
                <ChartTooltip>
                  <ChartTooltip.Header>{label || "—"}</ChartTooltip.Header>
                  <ChartTooltip.Item>
                    <ChartTooltip.Indicator color="var(--accent)" />
                    <ChartTooltip.Label>Mean</ChartTooltip.Label>
                    <ChartTooltip.Value>{row.mean}°F</ChartTooltip.Value>
                  </ChartTooltip.Item>
                  <ChartTooltip.Item>
                    <ChartTooltip.Indicator color="var(--temp-warm)" />
                    <ChartTooltip.Label>p10 – p90</ChartTooltip.Label>
                    <ChartTooltip.Value>{row.p10}° – {row.p90}°</ChartTooltip.Value>
                  </ChartTooltip.Item>
                </ChartTooltip>
              );
            }}
          />
        </AreaChart>
      </Widget.Content>
    </Widget>
  );
}
