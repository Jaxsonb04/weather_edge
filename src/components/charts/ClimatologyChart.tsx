import { AreaChart } from "@heroui-pro/react/area-chart";
import { ChartTooltip } from "@heroui-pro/react/chart-tooltip";
import { Widget } from "@heroui-pro/react/widget";
import { climatologySeries, monthDayLabel, type ForecastData } from "../../lib/data";

/** Headroom in °F above and below the band, and the grid the bounds snap out
    to. Snapping to a multiple of four keeps recharts' five default ticks on
    whole degrees whatever the data does. */
const Y_GRID_F = 4;

export function ClimatologyChart({ forecast }: { forecast: ForecastData }) {
  const series = climatologySeries(forecast);
  // The band is drawn as a stack (an invisible p10 area carrying the p90−p10
  // band), so the series' own data domain reaches all the way down to the
  // stack's zero baseline — which is how the axis ended up anchored at 0°F,
  // flattening a ~18° annual swing. Recharts widens a numeric domain back out to
  // the data unless overflow is allowed, so bound the axis to the visible band
  // and let the invisible base clip.
  const lo = series.length ? Math.min(...series.map((d) => Math.min(d.p10, d.mean))) : 0;
  const hi = series.length ? Math.max(...series.map((d) => Math.max(d.p90, d.mean))) : 100;
  const yDomain: [number, number] = [
    Math.floor((lo - Y_GRID_F) / Y_GRID_F) * Y_GRID_F,
    Math.ceil((hi + Y_GRID_F) / Y_GRID_F) * Y_GRID_F,
  ];

  return (
    <Widget className="w-full">
      <Widget.Header className="flex-col items-start gap-2 sm:flex-row sm:items-center">
        <div>
          <Widget.Title>SFO daily-high climatology</Widget.Title>
          <Widget.Description>Mean with the 10th–90th percentile seasonal band</Widget.Description>
        </div>
        <Widget.Legend className="shrink-0 flex-wrap">
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
          <AreaChart.YAxis
            width={36}
            domain={yDomain}
            allowDataOverflow
            tickFormatter={(v: number) => `${v}°`}
          />
          <AreaChart.Area dataKey="p10" stackId="band" stroke="none" fill="transparent" type="monotone" dot={false} />
          <AreaChart.Area dataKey="band" name="p10–p90" stackId="band" stroke="none" fill="url(#band-fill)" type="monotone" dot={false} />
          <AreaChart.Area dataKey="mean" name="Mean high" stroke="var(--accent)" strokeWidth={2.75} fill="none" type="monotone" dot={false} />
          <AreaChart.Tooltip
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null;
              // Only one sampled point per month carries an axis label, so the
              // hovered row's own day-of-year key is the honest header — the
              // axis label was blank on 110 of the 122 hover positions.
              const row = payload[0]?.payload as { key: string; mean: number; p10: number; p90: number };
              return (
                <ChartTooltip>
                  <ChartTooltip.Header>{monthDayLabel(row.key)}</ChartTooltip.Header>
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
