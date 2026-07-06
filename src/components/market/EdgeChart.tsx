import { BarChart, ChartTooltip, Widget } from "@heroui-pro/react";
import { f1, marketModelSeries, type Target } from "../../lib/data";

/** The engine's core view: where the model's bin probabilities diverge from the
    market-implied ones — i.e. where the edge (if any) lives. */
export function EdgeChart({ target }: { target: Target }) {
  const series = marketModelSeries(target);
  const mc = target.market_consensus;
  if (!series.length || !mc) return null;
  return (
    <Widget className="w-full">
      <Widget.Header>
        <div>
          <Widget.Title>Model vs market — bin probabilities</Widget.Title>
          <Widget.Description>
            model high {f1(mc.model_high_f)} · market implies {f1(mc.implied_high_f)} · modal bin {mc.modal_bin_label}
          </Widget.Description>
        </div>
        <Widget.Legend>
          <Widget.LegendItem color="var(--accent)">model</Widget.LegendItem>
          <Widget.LegendItem color="var(--series-market)">market</Widget.LegendItem>
        </Widget.Legend>
      </Widget.Header>
      <Widget.Content>
        <BarChart data={series} height={240} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <BarChart.Grid vertical={false} />
          <BarChart.XAxis dataKey="label" tickMargin={8} interval={0} />
          <BarChart.YAxis width={40} tickFormatter={(v: number) => `${v}%`} />
          <BarChart.Bar dataKey="model" name="Model" fill="var(--accent)" radius={[4, 4, 0, 0]} barSize={14} />
          <BarChart.Bar dataKey="market" name="Market" fill="var(--series-market)" radius={[4, 4, 0, 0]} barSize={14} />
          <BarChart.Tooltip
            content={({ active, label, payload }) => {
              if (!active || !payload?.length) return null;
              return (
                <ChartTooltip>
                  <ChartTooltip.Header>{label}°F bin</ChartTooltip.Header>
                  <ChartTooltip.Item>
                    <ChartTooltip.Indicator color="var(--accent)" />
                    <ChartTooltip.Label>Model</ChartTooltip.Label>
                    <ChartTooltip.Value>{payload.find((p) => p.dataKey === "model")?.value ?? 0}%</ChartTooltip.Value>
                  </ChartTooltip.Item>
                  <ChartTooltip.Item>
                    <ChartTooltip.Indicator color="var(--series-market)" />
                    <ChartTooltip.Label>Market</ChartTooltip.Label>
                    <ChartTooltip.Value>{payload.find((p) => p.dataKey === "market")?.value ?? 0}%</ChartTooltip.Value>
                  </ChartTooltip.Item>
                </ChartTooltip>
              );
            }}
          />
        </BarChart>
      </Widget.Content>
    </Widget>
  );
}
