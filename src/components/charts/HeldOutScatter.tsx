import { CartesianGrid, ReferenceLine, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis } from "recharts";
import { ChartTooltip, Widget } from "@heroui-pro/react";
import { heldOutSeries, type Diagnostics } from "../../lib/diagnostics";

/** Held-out predicted (LSTM) vs actual high — points hugging the dashed y=x line
    mean the model tracks reality across the full temperature range. */
export function HeldOutScatter({ diag }: { diag: Diagnostics }) {
  const data = heldOutSeries(diag);
  const lo = 35;
  const hi = 100;
  return (
    <Widget className="h-full w-full">
      <Widget.Header>
        <div>
          <Widget.Title>Predicted vs actual</Widget.Title>
          <Widget.Description>{data.length} held-out days · LSTM prediction against the settled high</Widget.Description>
        </div>
        <Widget.Legend>
          <Widget.LegendItem color="var(--accent)">held-out day</Widget.LegendItem>
          <Widget.LegendItem color="var(--color-muted)">perfect</Widget.LegendItem>
        </Widget.Legend>
      </Widget.Header>
      <Widget.Content>
        <div
          role="img"
          aria-label={`Scatter of ${data.length} held-out days: LSTM-predicted high versus the settled actual high, with points clustering tightly along the perfect-prediction diagonal.`}
        >
          <ResponsiveContainer width="100%" height={260}>
            <ScatterChart margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
              <CartesianGrid stroke="var(--color-border)" strokeOpacity={0.5} vertical={false} />
              <XAxis
                type="number"
                dataKey="actual"
                domain={[lo, hi]}
                tickFormatter={(v: number) => `${v}°`}
                tick={{ fill: "var(--color-muted)", fontSize: 12 }}
                stroke="var(--color-border)"
                tickMargin={8}
                name="Actual"
              />
              <YAxis
                type="number"
                dataKey="lstm"
                domain={[lo, hi]}
                width={40}
                tickFormatter={(v: number) => `${v}°`}
                tick={{ fill: "var(--color-muted)", fontSize: 12 }}
                stroke="var(--color-border)"
                name="Predicted"
              />
              <ReferenceLine
                segment={[{ x: lo, y: lo }, { x: hi, y: hi }]}
                stroke="var(--color-muted)"
                strokeDasharray="5 5"
                strokeWidth={1.5}
                ifOverflow="hidden"
              />
              <Tooltip
                cursor={{ stroke: "var(--color-border)" }}
                content={({ active, payload }) => {
                  if (!active || !payload?.length) return null;
                  const p = payload[0]?.payload as { actual: number; lstm: number };
                  const resid = Math.round((p.lstm - p.actual) * 10) / 10;
                  return (
                    <ChartTooltip>
                      <ChartTooltip.Header>Held-out day</ChartTooltip.Header>
                      <ChartTooltip.Item>
                        <ChartTooltip.Indicator color="var(--accent)" />
                        <ChartTooltip.Label>Predicted</ChartTooltip.Label>
                        <ChartTooltip.Value>{p.lstm}°F</ChartTooltip.Value>
                      </ChartTooltip.Item>
                      <ChartTooltip.Item>
                        <ChartTooltip.Label>Actual</ChartTooltip.Label>
                        <ChartTooltip.Value>{p.actual}°F</ChartTooltip.Value>
                      </ChartTooltip.Item>
                      <ChartTooltip.Item>
                        <ChartTooltip.Label>Residual</ChartTooltip.Label>
                        <ChartTooltip.Value>{resid > 0 ? "+" : ""}{resid}°F</ChartTooltip.Value>
                      </ChartTooltip.Item>
                    </ChartTooltip>
                  );
                }}
              />
              <Scatter data={data} fill="var(--accent)" fillOpacity={0.45} shape="circle" />
            </ScatterChart>
          </ResponsiveContainer>
        </div>
        <p className="mt-1 text-center text-xs text-muted">Actual high (°F) →</p>
      </Widget.Content>
    </Widget>
  );
}
