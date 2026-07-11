import { ChartTooltip } from "@heroui-pro/react/chart-tooltip";
import { LineChart } from "@heroui-pro/react/line-chart";
import { Widget } from "@heroui-pro/react/widget";
import { calibrationSeries, pct, type TradingSignal } from "../../lib/data";

export function CalibrationChart({ signal }: { signal: TradingSignal }) {
  const series = calibrationSeries(signal);
  const calibration = signal.calibration;
  return (
    <Widget className="h-full w-full">
      <Widget.Header>
        <div>
          <Widget.Title>Probability calibration</Widget.Title>
          <Widget.Description>
            Reliability over {calibration?.n ?? 0} settled bins · Brier skill {pct(calibration?.brier_skill, 1)}
          </Widget.Description>
        </div>
        <Widget.Legend>
          <Widget.LegendItem color="var(--series-market)">observed</Widget.LegendItem>
          <Widget.LegendItem color="var(--color-muted)">ideal</Widget.LegendItem>
        </Widget.Legend>
      </Widget.Header>
      <Widget.Content>
        <LineChart data={series} height={220}>
          <LineChart.Grid vertical={false} />
          <LineChart.XAxis dataKey="predicted" tickMargin={8} tickFormatter={(v: number) => `${v}%`} />
          <LineChart.YAxis width={40} tickFormatter={(v: number) => `${v}%`} domain={[0, 100]} />
          <LineChart.Line dataKey="ideal" name="Ideal" stroke="var(--color-muted)" strokeWidth={1.5} strokeDasharray="4 4" dot={false} type="linear" />
          <LineChart.Line dataKey="observed" name="Observed" stroke="var(--series-market)" strokeWidth={2.5} type="monotone" />
          <LineChart.Tooltip
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null;
              const row = payload[0]?.payload as { predicted: number; observed: number; count: number };
              return (
                <ChartTooltip>
                  <ChartTooltip.Header>Predicted {row.predicted}%</ChartTooltip.Header>
                  <ChartTooltip.Item>
                    <ChartTooltip.Indicator color="var(--series-market)" />
                    <ChartTooltip.Label>Observed</ChartTooltip.Label>
                    <ChartTooltip.Value>{row.observed}%</ChartTooltip.Value>
                  </ChartTooltip.Item>
                  <ChartTooltip.Item>
                    <ChartTooltip.Label>Samples</ChartTooltip.Label>
                    <ChartTooltip.Value>{row.count}</ChartTooltip.Value>
                  </ChartTooltip.Item>
                </ChartTooltip>
              );
            }}
          />
        </LineChart>
      </Widget.Content>
    </Widget>
  );
}
