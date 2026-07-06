import { BarChart, ChartTooltip, Widget } from "@heroui-pro/react";
import { cohortSeries, type TradingSignal } from "../../lib/data";

/** Per-temperature-regime skill — the honest story that the model is razor-sharp
    on cold days and humbled on rare hot ones. */
export function CohortChart({ signal }: { signal: TradingSignal }) {
  const series = cohortSeries(signal);
  if (!series.length) return null;
  return (
    <Widget className="h-full w-full">
      <Widget.Header>
        <div>
          <Widget.Title>Skill by temperature regime</Widget.Title>
          <Widget.Description>Ranked-probability skill vs climatology, per settled cohort</Widget.Description>
        </div>
      </Widget.Header>
      <Widget.Content>
        <BarChart data={series} height={220} layout="vertical" margin={{ left: 8, right: 16, top: 4, bottom: 0 }}>
          <BarChart.Grid horizontal={false} />
          <BarChart.XAxis type="number" tickFormatter={(v: number) => `${v}%`} domain={[0, 100]} />
          <BarChart.YAxis type="category" dataKey="name" width={108} tickMargin={6} />
          <BarChart.Bar dataKey="skill" name="RPS skill" fill="var(--accent)" radius={[0, 6, 6, 0]} barSize={18} />
          <BarChart.Tooltip
            content={({ active, payload, label }) => {
              if (!active || !payload?.length) return null;
              const row = payload[0]?.payload as { skill: number; topBin: number; count: number };
              return (
                <ChartTooltip>
                  <ChartTooltip.Header>{label}</ChartTooltip.Header>
                  <ChartTooltip.Item>
                    <ChartTooltip.Indicator color="var(--accent)" />
                    <ChartTooltip.Label>RPS skill</ChartTooltip.Label>
                    <ChartTooltip.Value>{row.skill}%</ChartTooltip.Value>
                  </ChartTooltip.Item>
                  <ChartTooltip.Item>
                    <ChartTooltip.Label>Top-bin acc.</ChartTooltip.Label>
                    <ChartTooltip.Value>{row.topBin}%</ChartTooltip.Value>
                  </ChartTooltip.Item>
                  <ChartTooltip.Item>
                    <ChartTooltip.Label>Settled days</ChartTooltip.Label>
                    <ChartTooltip.Value>{row.count}</ChartTooltip.Value>
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
