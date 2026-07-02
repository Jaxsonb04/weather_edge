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
import { ChartTooltip, Widget } from "@heroui-pro/react";
import { pct, signedPct } from "../../lib/data";
import type { ScatterPoint, StrategyLab } from "../../lib/strategy";

const AXIS_TICK = { fill: "var(--color-muted)", fontSize: 12 };

function CandidateScatter({ points, targetDate }: { points: ScatterPoint[]; targetDate?: string }) {
  const yes = points.filter((p) => p.side === "YES");
  const no = points.filter((p) => p.side !== "YES");
  return (
    <Widget className="h-full w-full">
      <Widget.Header>
        <div>
          <Widget.Title>Model vs market, per candidate</Widget.Title>
          <Widget.Description>
            {points.length} latest candidates{targetDate ? ` · target ${targetDate}` : ""} · off the diagonal = disagreement
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
              <CartesianGrid stroke="var(--color-border)" strokeOpacity={0.5} vertical={false} />
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
              <Scatter data={yes} fill="var(--accent)" fillOpacity={0.65} shape="circle" />
              <Scatter data={no} fill="var(--series-market)" fillOpacity={0.65} shape="circle" />
            </ScatterChart>
          </ResponsiveContainer>
        </div>
        <p className="mt-1 text-center text-xs text-muted">Market-implied probability →</p>
      </Widget.Content>
    </Widget>
  );
}

function EdgeByPriceChart({ s }: { s: StrategyLab }) {
  const buckets = s.signal_quality?.charts?.edge_by_market_bucket ?? [];
  const data = buckets.map((b) => ({ range: `${b.range}¢`, edge: Math.round(b.avg_edge * 1000) / 10, count: b.count }));
  return (
    <Widget className="h-full w-full">
      <Widget.Header>
        <div>
          <Widget.Title>Average edge by market price</Widget.Title>
          <Widget.Description>Where in the price ladder the model disagrees, latest candidates</Widget.Description>
        </div>
        <Widget.Legend>
          <Widget.LegendItem color="var(--color-success)">model above market</Widget.LegendItem>
          <Widget.LegendItem color="var(--color-danger)">model below market</Widget.LegendItem>
        </Widget.Legend>
      </Widget.Header>
      <Widget.Content>
        <div
          role="img"
          aria-label="Average model-minus-market edge for candidates grouped into 20-cent market-price buckets."
        >
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
              <CartesianGrid stroke="var(--color-border)" strokeOpacity={0.5} vertical={false} />
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
                        <ChartTooltip.Indicator color={row.edge >= 0 ? "var(--color-success)" : "var(--color-danger)"} />
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
              <Bar dataKey="edge" name="Avg edge" radius={[4, 4, 0, 0]} barSize={28}>
                {data.map((d) => (
                  <Cell key={d.range} fill={d.edge >= 0 ? "var(--color-success)" : "var(--color-danger)"} fillOpacity={0.75} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Widget.Content>
    </Widget>
  );
}

/** The live signal surface: every candidate the engine is watching right now,
    and where in the price ladder its disagreements sit. */
export function SignalQualityPanel({ s }: { s: StrategyLab }) {
  const q = s.signal_quality;
  const points = q?.charts?.probability_vs_market ?? [];
  if (!q?.available || !points.length) return null;
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <CandidateScatter points={points} targetDate={q.latest_target_date} />
      <EdgeByPriceChart s={s} />
    </div>
  );
}
