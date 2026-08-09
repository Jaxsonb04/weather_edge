import { Cell } from "recharts";
import { BarChart } from "@heroui-pro/react/bar-chart";
import { ChartTooltip } from "@heroui-pro/react/chart-tooltip";
import { Widget } from "@heroui-pro/react/widget";
import {
  TEMP_RAMP_COLD,
  TEMP_RAMP_HOT,
  histogramBasis,
  histogramSeries,
  tempColor,
  type ForecastData,
  type WeatherStory,
} from "../../lib/data";

/** Bin edges sit on a 2.5° grid, so whole degrees and halves both occur; print
    the half only where there is one instead of rounding the grid away.
    (A category axis hands its formatter the raw cell value, so coerce.) */
const edgeTick = (value: number | string) => {
  const v = Number(value);
  if (!Number.isFinite(v)) return String(value);
  return Number.isInteger(v) ? `${v}` : v.toFixed(1);
};

export function HistogramChart({ story, forecast }: { story: WeatherStory; forecast: ForecastData }) {
  const series = histogramSeries(story);
  // The payload declares its own basis. An unmarked payload is the legacy build,
  // whose counts are raw hourly readings — ~24 per observed day — so calling
  // them daily highs would overstate the sample by more than an order of
  // magnitude and shift the whole distribution several degrees cold.
  const isDailyMax = histogramBasis(story) === "daily_max";
  const countLabel = isDailyMax ? "Days observed" : "Observations";

  return (
    <Widget className="h-full w-full">
      <Widget.Header className="flex-col items-start gap-2 sm:flex-row sm:items-center">
        <div>
          <Widget.Title>
            {isDailyMax ? "Observed-high distribution" : "Observed-temperature distribution"}
          </Widget.Title>
          <Widget.Description>
            {isDailyMax
              ? `Every recorded KSFO daily high · ${forecast.n_years}-year window`
              : `Every recorded KSFO hourly reading · ${forecast.n_years}-year window`}
          </Widget.Description>
        </div>
        <Widget.Legend className="shrink-0 flex-wrap">
          <Widget.LegendItem color={TEMP_RAMP_COLD}>cooler</Widget.LegendItem>
          <Widget.LegendItem color={TEMP_RAMP_HOT}>hotter</Widget.LegendItem>
        </Widget.Legend>
      </Widget.Header>
      <Widget.Content>
        <BarChart data={series} height={220}>
          <BarChart.Grid vertical={false} />
          {/* Ticks come off the bin EDGES: the published centres are rounded to a
              tenth, so labelling them produced an uneven 36/39/41/44 sequence. */}
          <BarChart.XAxis
            dataKey="lo"
            tickMargin={8}
            interval={3}
            tickFormatter={(v: number | string) => `${edgeTick(v)}°`}
          />
          <BarChart.YAxis width={40} tickFormatter={(v: number) => (v >= 1000 ? `${v / 1000}k` : `${v}`)} />
          {/* Per-bar fill keyed to the bin's temperature, so x-position and hue
              reinforce the same variable. */}
          <BarChart.Bar dataKey="count" name={countLabel} radius={[3, 3, 0, 0]}>
            {series.map((d) => (
              <Cell key={d.lo} fill={tempColor(d.temp)} />
            ))}
          </BarChart.Bar>
          <BarChart.Tooltip
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null;
              const row = payload[0]?.payload as { temp: number; lo: number; hi: number; count: number };
              return (
                <ChartTooltip>
                  <ChartTooltip.Header>
                    {edgeTick(row.lo)}–{edgeTick(row.hi)}°F
                  </ChartTooltip.Header>
                  <ChartTooltip.Item>
                    <ChartTooltip.Indicator color={tempColor(row.temp)} />
                    <ChartTooltip.Label>{countLabel}</ChartTooltip.Label>
                    <ChartTooltip.Value>{row.count.toLocaleString()}</ChartTooltip.Value>
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
