import { useReducedMotion } from "motion/react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { ChartTooltip } from "@heroui-pro/react/chart-tooltip";
import { Widget } from "@heroui-pro/react/widget";
import { pct, signedPct } from "../../lib/data";
import type { EdgeBucket, ScatterPoint } from "../../lib/strategy";

const AXIS_TICK = { fill: "var(--color-muted)", fontSize: 12 };

/** Model vs market probability per candidate signal — points off the dashed
    diagonal are the brackets where the book disagrees with the crowd. */
export function CandidateScatter({ points, targetDate }: { points: ScatterPoint[]; targetDate?: string }) {
  // Recharts grows series in on mount; reduced motion draws them in place.
  const reduceMotion = useReducedMotion();
  const yes = points.filter((p) => p.side === "YES");
  const no = points.filter((p) => p.side !== "YES");
  return (
    <Widget className="h-full w-full">
      <Widget.Header>
        <div className="flex min-w-0 flex-col gap-0.5">
          <Widget.Title>Model vs market</Widget.Title>
          <Widget.Description>
            {points.length} current candidates{targetDate ? ` · target ${targetDate}` : ""} · off the line means disagreement
          </Widget.Description>
        </div>
        <Widget.Legend>
          <Widget.LegendItem color="var(--accent)">YES side</Widget.LegendItem>
          <Widget.LegendItem color="var(--series-market)">NO side</Widget.LegendItem>
        </Widget.Legend>
      </Widget.Header>
      <Widget.Content>
        <div
          role="img"
          aria-label={`Scatter of ${points.length} candidate signals: market-implied probability versus model probability. Points above the dashed diagonal are brackets where the model is more confident than the market.`}
        >
          <ResponsiveContainer width="100%" height={260}>
            <ScatterChart margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
              <CartesianGrid stroke="var(--color-separator)" vertical={false} />
              <XAxis
                type="number"
                dataKey="x"
                domain={[0, 1]}
                tickFormatter={(v: number) => `${Math.round(v * 100)}%`}
                tick={AXIS_TICK}
                stroke="var(--color-border)"
                tickMargin={8}
                name="Market"
              />
              <YAxis
                type="number"
                dataKey="y"
                domain={[0, 1]}
                width={44}
                tickFormatter={(v: number) => `${Math.round(v * 100)}%`}
                tick={AXIS_TICK}
                stroke="var(--color-border)"
                name="Model"
              />
              <ReferenceLine
                segment={[{ x: 0, y: 0 }, { x: 1, y: 1 }]}
                stroke="var(--color-muted)"
                strokeDasharray="5 5"
                strokeWidth={1.5}
                ifOverflow="hidden"
              />
              <Tooltip
                cursor={{ stroke: "var(--color-border)" }}
                content={({ active, payload }) => {
                  if (!active || !payload?.length) return null;
                  const p = payload[0]?.payload as ScatterPoint;
                  return (
                    <ChartTooltip>
                      <ChartTooltip.Header>
                        {p.label} · {p.side}
                      </ChartTooltip.Header>
                      <ChartTooltip.Item>
                        <ChartTooltip.Indicator color={p.side === "YES" ? "var(--accent)" : "var(--series-market)"} />
                        <ChartTooltip.Label>Model</ChartTooltip.Label>
                        <ChartTooltip.Value>{pct(p.y, 1)}</ChartTooltip.Value>
                      </ChartTooltip.Item>
                      <ChartTooltip.Item>
                        <ChartTooltip.Label>Market</ChartTooltip.Label>
                        <ChartTooltip.Value>{pct(p.x, 1)}</ChartTooltip.Value>
                      </ChartTooltip.Item>
                      <ChartTooltip.Item>
                        <ChartTooltip.Label>Raw edge</ChartTooltip.Label>
                        <ChartTooltip.Value>{signedPct(p.y - p.x, 1)}</ChartTooltip.Value>
                      </ChartTooltip.Item>
                    </ChartTooltip>
                  );
                }}
              />
              <Scatter isAnimationActive={!reduceMotion} data={yes} fill="var(--accent)" fillOpacity={0.65} shape="circle" />
              <Scatter isAnimationActive={!reduceMotion} data={no} fill="var(--series-market)" fillOpacity={0.65} shape="circle" />
            </ScatterChart>
          </ResponsiveContainer>
        </div>
        <p className="mt-1 text-center text-xs text-muted">Market-implied probability →</p>
      </Widget.Content>
    </Widget>
  );
}

/** Average model-minus-market edge grouped by 20¢ market-price buckets. */
export function EdgeByPriceChart({ buckets }: { buckets: EdgeBucket[] }) {
  // Recharts grows series in on mount; reduced motion draws them in place.
  const reduceMotion = useReducedMotion();
  const data = buckets.map((b) => ({ range: `${b.range}¢`, edge: Math.round(b.avg_edge * 1000) / 10, count: b.count }));
  return (
    <Widget className="h-full w-full">
      <Widget.Header>
        <div className="flex min-w-0 flex-col gap-0.5">
          <Widget.Title>Edge by market price</Widget.Title>
          <Widget.Description>Average model minus market, in 20¢ price buckets</Widget.Description>
        </div>
        <Widget.Legend>
          <Widget.LegendItem color="var(--accent)">model above market</Widget.LegendItem>
          <Widget.LegendItem color="var(--series-market)">model below market</Widget.LegendItem>
        </Widget.Legend>
      </Widget.Header>
      <Widget.Content>
        <div
          role="img"
          aria-label="Average model-minus-market edge for candidates grouped into 20-cent market-price buckets."
        >
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
              <CartesianGrid stroke="var(--color-separator)" vertical={false} />
              <XAxis dataKey="range" tick={AXIS_TICK} stroke="var(--color-border)" tickMargin={8} />
              <YAxis width={44} tickFormatter={(v: number) => `${v}%`} tick={AXIS_TICK} stroke="var(--color-border)" />
              <ReferenceLine y={0} stroke="var(--color-muted)" strokeWidth={1.25} />
              <Tooltip
                cursor={{ fill: "var(--color-border)", fillOpacity: 0.25 }}
                content={({ active, payload, label }) => {
                  if (!active || !payload?.length) return null;
                  const row = payload[0]?.payload as { edge: number; count: number };
                  return (
                    <ChartTooltip>
                      <ChartTooltip.Header>{label} market price</ChartTooltip.Header>
                      <ChartTooltip.Item>
                        <ChartTooltip.Indicator color={row.edge >= 0 ? "var(--accent)" : "var(--series-market)"} />
                        <ChartTooltip.Label>Avg edge</ChartTooltip.Label>
                        <ChartTooltip.Value>{row.edge >= 0 ? "+" : ""}{row.edge}%</ChartTooltip.Value>
                      </ChartTooltip.Item>
                      <ChartTooltip.Item>
                        <ChartTooltip.Label>Candidates</ChartTooltip.Label>
                        <ChartTooltip.Value>{row.count}</ChartTooltip.Value>
                      </ChartTooltip.Item>
                    </ChartTooltip>
                  );
                }}
              />
              <Bar isAnimationActive={!reduceMotion} dataKey="edge" name="Avg edge" radius={[4, 4, 0, 0]} barSize={28}>
                {data.map((d) => (
                  <Cell key={d.range} fill={d.edge >= 0 ? "var(--accent)" : "var(--series-market)"} fillOpacity={0.85} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Widget.Content>
    </Widget>
  );
}
