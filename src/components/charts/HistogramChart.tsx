import { Cell } from "recharts";
import { BarChart } from "@heroui-pro/react/bar-chart";
import { ChartTooltip } from "@heroui-pro/react/chart-tooltip";
import { Widget } from "@heroui-pro/react/widget";
import { histogramSeries, tempColor, type ForecastData, type WeatherStory } from "../../lib/data";

export function HistogramChart({ story, forecast }: { story: WeatherStory; forecast: ForecastData }) {
  const series = histogramSeries(story);
  return (
    <Widget className="h-full w-full">
      <Widget.Header>
        <div>
          <Widget.Title>Observed-high distribution</Widget.Title>
          <Widget.Description>Every recorded KSFO daily high · {forecast.n_years}-year window</Widget.Description>
        </div>
        <Widget.Legend>
          <Widget.LegendItem color="var(--temp-cold)">cooler</Widget.LegendItem>
          <Widget.LegendItem color="var(--temp-hot)">hotter</Widget.LegendItem>
        </Widget.Legend>
      </Widget.Header>
      <Widget.Content>
        <BarChart data={series} height={220}>
          <BarChart.Grid vertical={false} />
          <BarChart.XAxis dataKey="temp" tickMargin={8} interval={3} tickFormatter={(v: number) => `${v}°`} />
          <BarChart.YAxis width={40} tickFormatter={(v: number) => (v >= 1000 ? `${v / 1000}k` : `${v}`)} />
          {/* Per-bar fill keyed to the bin's temperature, so x-position and hue
              reinforce the same variable. */}
          <BarChart.Bar dataKey="count" name="Days" radius={[3, 3, 0, 0]}>
            {series.map((d) => (
              <Cell key={d.temp} fill={tempColor(d.temp)} />
            ))}
          </BarChart.Bar>
          <BarChart.Tooltip
            content={({ active, label, payload }) => {
              if (!active || !payload?.length) return null;
              return (
                <ChartTooltip>
                  <ChartTooltip.Header>{label}°F high</ChartTooltip.Header>
                  <ChartTooltip.Item>
                    <ChartTooltip.Indicator color={tempColor(Number(label))} />
                    <ChartTooltip.Label>Days observed</ChartTooltip.Label>
                    <ChartTooltip.Value>{Number(payload[0].value).toLocaleString()}</ChartTooltip.Value>
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
