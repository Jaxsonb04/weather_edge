import { BarChart } from "@heroui-pro/react/bar-chart";
import { ChartTooltip } from "@heroui-pro/react/chart-tooltip";
import { Widget } from "@heroui-pro/react/widget";
import { featureSeries, type Diagnostics } from "../../lib/diagnostics";

/** XGBoost feature attribution — what the model leans on most. */
export function FeatureImportanceChart({ diag }: { diag: Diagnostics }) {
  const series = featureSeries(diag);
  const top = series[series.length - 1];
  const label = top
    ? `XGBoost feature importance by share of gain; the leading feature is ${top.feature} at ${top.importance} percent.`
    : "XGBoost feature importance.";
  return (
    <Widget className="h-full w-full">
      <Widget.Header>
        <div>
          <Widget.Title>What drives the forecast</Widget.Title>
          <Widget.Description>XGBoost feature importance · share of total gain</Widget.Description>
        </div>
      </Widget.Header>
      <Widget.Content>
        <div role="img" aria-label={label}>
        <BarChart data={series} height={240} layout="vertical" margin={{ left: 8, right: 20, top: 4, bottom: 0 }}>
          <BarChart.Grid horizontal={false} />
          <BarChart.XAxis type="number" tickFormatter={(v: number) => `${v}%`} />
          <BarChart.YAxis type="category" dataKey="feature" width={116} tickMargin={6} />
          <BarChart.Bar dataKey="importance" name="Importance" fill="var(--accent)" radius={[0, 6, 6, 0]} barSize={14} />
          <BarChart.Tooltip
            content={({ active, payload, label }) => {
              if (!active || !payload?.length) return null;
              return (
                <ChartTooltip>
                  <ChartTooltip.Header>{label}</ChartTooltip.Header>
                  <ChartTooltip.Item>
                    <ChartTooltip.Indicator color="var(--accent)" />
                    <ChartTooltip.Label>Importance</ChartTooltip.Label>
                    <ChartTooltip.Value>{payload[0].value}%</ChartTooltip.Value>
                  </ChartTooltip.Item>
                </ChartTooltip>
              );
            }}
          />
        </BarChart>
        </div>
      </Widget.Content>
    </Widget>
  );
}
