import { Cell } from "recharts";
import { BarChart, ChartTooltip, Widget } from "@heroui-pro/react";
import { modelCompareSeries, type Diagnostics } from "../../lib/diagnostics";

const BAR_COLOR: Record<string, string> = {
  LSTM: "var(--accent)",
  XGBoost: "var(--series-market)",
  Persistence: "var(--color-muted)",
};

/** Held-out MAE by model — the production LSTM vs the XGBoost challenger and a
    naive persistence baseline. Lower is better. */
export function ModelCompareChart({ diag }: { diag: Diagnostics }) {
  const series = modelCompareSeries(diag);
  const label = `Held-out mean absolute error by model, lower is better: ${series.map((d) => `${d.model} ${d.mae} degrees`).join(", ")}.`;
  return (
    <Widget className="h-full w-full">
      <Widget.Header>
        <div>
          <Widget.Title>Forecast error by model</Widget.Title>
          <Widget.Description>Held-out mean absolute error (°F) · lower is better · LSTM in production</Widget.Description>
        </div>
        <Widget.Legend>
          <Widget.LegendItem color="var(--accent)">LSTM</Widget.LegendItem>
          <Widget.LegendItem color="var(--series-market)">XGBoost</Widget.LegendItem>
        </Widget.Legend>
      </Widget.Header>
      <Widget.Content>
        <div role="img" aria-label={label}>
        <BarChart data={series} height={220} layout="vertical" margin={{ left: 8, right: 24, top: 4, bottom: 0 }}>
          <BarChart.Grid horizontal={false} />
          <BarChart.XAxis type="number" tickFormatter={(v: number) => `${v}°`} domain={[0, "dataMax"]} />
          <BarChart.YAxis type="category" dataKey="model" width={92} tickMargin={6} />
          <BarChart.Bar dataKey="mae" name="MAE" radius={[0, 6, 6, 0]} barSize={26}>
            {series.map((d) => (
              <Cell key={d.model} fill={BAR_COLOR[d.model] ?? "var(--color-muted)"} />
            ))}
          </BarChart.Bar>
          <BarChart.Tooltip
            content={({ active, payload, label }) => {
              if (!active || !payload?.length) return null;
              const row = payload[0]?.payload as { mae: number; rmse: number };
              return (
                <ChartTooltip>
                  <ChartTooltip.Header>{label}</ChartTooltip.Header>
                  <ChartTooltip.Item>
                    <ChartTooltip.Indicator color={BAR_COLOR[String(label)] ?? "var(--color-muted)"} />
                    <ChartTooltip.Label>MAE</ChartTooltip.Label>
                    <ChartTooltip.Value>{row.mae}°F</ChartTooltip.Value>
                  </ChartTooltip.Item>
                  <ChartTooltip.Item>
                    <ChartTooltip.Label>RMSE</ChartTooltip.Label>
                    <ChartTooltip.Value>{row.rmse}°F</ChartTooltip.Value>
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
