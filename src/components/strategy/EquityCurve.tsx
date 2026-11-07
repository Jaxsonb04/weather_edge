import { useId } from "react";
import { ReferenceArea, ReferenceLine } from "recharts";
import { useChartDraw } from "../../lib/useChartDraw";
import { ChartTooltip } from "@heroui-pro/react/chart-tooltip";
import { LineChart } from "@heroui-pro/react/line-chart";
import { Widget } from "@heroui-pro/react/widget";
import { equitySeries, equitySeriesFromDays, money, type DayRow, type StrategyLab } from "../../lib/strategy";
import type { PaperIncident } from "../../lib/incidents";

type Emphasis = "headline" | "secondary" | "normal";

interface EquityCurveProps {
  s: StrategyLab;
  /** override the day series (e.g. a single profile's days) */
  days?: DayRow[];
  /** starting bankroll for the override series */
  startingBankroll?: number;
  windowDays?: number;
  title?: string;
  description?: string;
  contributionMode?: boolean;
  /** small label above the title */
  eyebrow?: string;
  /** visual weight: headline = tall, secondary = compact, normal = default */
  emphasis?: Emphasis;
  /** override the plot height in px (defaults keyed off emphasis) */
  height?: number;
  className?: string;
  /** Dated outages shaded on the plot, so a flat stretch reads as downtime. */
  incidents?: PaperIncident[];
}

const EMPHASIS_HEIGHT: Record<Emphasis, number> = { headline: 280, secondary: 168, normal: 236 };

/** −$80 / $0 / +$3 — whole-dollar money for axis ticks + aria text. */
const axisMoney = (v: number, signed = false) =>
  money(v, { digits: 0, sign: signed ? "except-zero" : "negative-only" });

export function EquityCurve({
  s,
  days,
  startingBankroll,
  windowDays,
  title,
  description,
  contributionMode = false,
  eyebrow,
  emphasis = "normal",
  height,
  className,
  incidents = [],
}: EquityCurveProps) {
  // Unique gradient id per instance: several curves can share a page, and a
  // duplicated SVG id would make later charts inherit the first chart's fill.
  const gid = `eq-fill-${useId().replace(/:/g, "")}`;
  const draw = useChartDraw();

  const rendersContribution =
    contributionMode ||
    (!days && s.daily_summary?.equity_basis === "attribution_only");
  const start =
    startingBankroll ??
    (rendersContribution ? 0 : (s.daily_summary?.starting_bankroll ?? 2000));
  const series = days ? equitySeriesFromDays(days, start) : equitySeries(s);
  const lineKey = rendersContribution ? "pnl" : "equity";
  const header = (fallbackDescription: string) => (
    <Widget.Header className="flex-col items-start gap-3 sm:flex-row sm:justify-between">
      <div className="flex min-w-0 flex-col gap-0.5">
        {eyebrow && <span className="text-xs font-medium text-[color:var(--accent-text)]">{eyebrow}</span>}
        <Widget.Title>{title ?? "Paper equity curve"}</Widget.Title>
        <Widget.Description>{description ?? fallbackDescription}</Widget.Description>
      </div>
      {series.length > 0 && (
        <Widget.Legend className="flex shrink-0 flex-wrap self-start pt-0.5">
          <Widget.LegendItem color={stroke}>{rendersContribution ? "P&L" : "equity"}</Widget.LegendItem>
          <Widget.LegendItem color="var(--color-muted)">{rendersContribution ? "break-even" : "start"}</Widget.LegendItem>
        </Widget.Legend>
      )}
    </Widget.Header>
  );

  const last = series[series.length - 1]?.[lineKey] ?? start;
  const up = last >= start;
  const stroke = up ? "var(--color-success)" : "var(--color-danger)";

  if (!series.length) {
    return (
      <Widget className={`w-full min-w-0 max-w-full ${className ?? ""}`.trim()}>
        {header("No dated series was published for this artifact.")}
        <Widget.Content>
          <div role="status" className="grid h-40 place-items-center rounded-2xl bg-surface-secondary px-4 text-center text-xs text-muted">
            No dated curve is available in this publication.
          </div>
        </Widget.Content>
      </Widget>
    );
  }
  const win = windowDays ?? s.daily_summary?.window_days ?? series.length;
  // Adaptive y-domain: pad proportional to the actual DATA swing (with a small
  // floor) so a book that barely moved still shows its shape. Break-even stays
  // in view, flush, with no overshoot past it.
  const eqs = series.map((d) => d[lineKey]);
  const dataLo = Math.min(...eqs);
  const dataHi = Math.max(...eqs);
  const pad = Math.max((dataHi - dataLo) * 0.15, 3);
  const lo = Math.min(dataLo - pad, start);
  const hi = Math.max(dataHi + pad, start);
  const yDomain: [number, number] = [Math.floor(lo), Math.ceil(hi)];
  const valueName = rendersContribution ? "P&L contribution" : "Equity";
  const chartH = height ?? EMPHASIS_HEIGHT[emphasis];
  const label = rendersContribution
    ? `${title ?? "Paper P&L curve"} with ${series.length} published daily points, ending at ${axisMoney(last, true)} cumulative attribution; zero is the break-even reference.`
    : `${title ?? "Paper equity curve"} over ${series.length} days, from ${axisMoney(start)} to ${axisMoney(last)} (${up ? "up" : "down"} over the window).`;

  // Shade each incident across the part of it this series actually covers.
  const firstDate = series[0]?.date;
  const lastDate = series.at(-1)?.date;
  const bands = incidents
    .map((incident) => {
      const x1 = incident.start.slice(5) < (firstDate ?? "") ? firstDate : incident.start.slice(5);
      const x2 = incident.end.slice(5) > (lastDate ?? "") ? lastDate : incident.end.slice(5);
      return x1 && x2 && x1 <= x2 ? { id: incident.id, x1, x2, label: incident.short } : null;
    })
    .filter((band): band is { id: string; x1: string; x2: string; label: string } => band != null);
  const incidentNote = bands.length
    ? ` Shaded: ${incidents.map((incident) => incident.title.toLowerCase()).join(", ")}.`
    : "";

  return (
    <Widget className={`w-full min-w-0 max-w-full ${className ?? ""}`.trim()}>
      {header(`Cumulative realized P&L over the reporting window · ${win}-day view`)}
      <Widget.Content>
        <div role="img" aria-label={`${label}${incidentNote}`} className="fade-in">
          <LineChart data={series} height={chartH} margin={{ top: 8, right: 14, bottom: 0, left: 0 }}>
            <defs>
              <linearGradient id={gid} x1="0" x2="0" y1="0" y2="1">
                <stop offset="0%" stopColor={stroke} stopOpacity={0.18} />
                <stop offset="100%" stopColor={stroke} stopOpacity={0.01} />
              </linearGradient>
            </defs>
            <LineChart.Grid vertical={false} />
            {bands.map((band) => (
              <ReferenceArea
                key={band.id}
                x1={band.x1}
                x2={band.x2}
                fill="var(--color-warning)"
                fillOpacity={0.09}
                stroke="none"
                ifOverflow="extendDomain"
                label={{ value: band.label, position: "insideTop", fill: "var(--color-muted)", fontSize: 12 }}
              />
            ))}
            <LineChart.XAxis dataKey="date" tickMargin={8} />
            <LineChart.YAxis width={56} tickFormatter={(v: number) => axisMoney(v, rendersContribution)} domain={yDomain} allowDecimals={false} />
            <ReferenceLine y={start} stroke="var(--color-muted)" strokeDasharray="4 4" strokeWidth={1} />
            <LineChart.Line
              dataKey={lineKey}
              name={valueName}
              stroke={stroke}
              strokeWidth={2.25}
              type="monotone"
              fill={`url(#${gid})`}
              {...draw}
            />
            <LineChart.Tooltip
              cursor={false}
              isAnimationActive={false}
              content={({ active, label, payload }) => {
                if (!active || !payload?.length) return null;
                const row = payload[0]?.payload as {
                  equity: number;
                  pnl: number;
                  dailyPnl: number;
                  accountBalance?: number;
                };
                return (
                  <ChartTooltip>
                    <ChartTooltip.Header>{label}</ChartTooltip.Header>
                    <ChartTooltip.Item>
                      <ChartTooltip.Indicator color={stroke} />
                      <ChartTooltip.Label>{valueName}</ChartTooltip.Label>
                      <ChartTooltip.Value>{money(row[lineKey])}</ChartTooltip.Value>
                    </ChartTooltip.Item>
                    {!rendersContribution && (
                      <ChartTooltip.Item>
                        <ChartTooltip.Label>Cum. P&L</ChartTooltip.Label>
                        <ChartTooltip.Value>{money(row.pnl)}</ChartTooltip.Value>
                      </ChartTooltip.Item>
                    )}
                    <ChartTooltip.Item>
                      <ChartTooltip.Label>Daily P&L</ChartTooltip.Label>
                      <ChartTooltip.Value>{money(row.dailyPnl)}</ChartTooltip.Value>
                    </ChartTooltip.Item>
                    {rendersContribution && row.accountBalance != null && (
                      <ChartTooltip.Item>
                        <ChartTooltip.Label>Account balance</ChartTooltip.Label>
                        <ChartTooltip.Value>
                          {money(row.accountBalance, { sign: "negative-only" })}
                        </ChartTooltip.Value>
                      </ChartTooltip.Item>
                    )}
                  </ChartTooltip>
                );
              }}
            />
          </LineChart>
        </div>
      </Widget.Content>
    </Widget>
  );
}
