import { useId } from "react";
import { ReferenceLine } from "recharts";
import { ChartTooltip } from "@heroui-pro/react/chart-tooltip";
import { LineChart } from "@heroui-pro/react/line-chart";
import { Widget } from "@heroui-pro/react/widget";
import { equitySeries, equitySeriesFromDays, money, type DayRow, type StrategyLab } from "../../lib/strategy";

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
  /** small uppercase kicker above the title */
  eyebrow?: string;
  /** visual weight: headline = tall + larger title, secondary = compact, normal = default */
  emphasis?: Emphasis;
  /** override the plot height in px (defaults keyed off emphasis) */
  height?: number;
  className?: string;
}

const EMPHASIS_HEIGHT: Record<Emphasis, number> = { headline: 288, secondary: 168, normal: 240 };

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
}: EquityCurveProps) {
  // Unique gradient id per instance — several equity curves now share a page and a
  // duplicated SVG id would make later charts inherit the first chart's fill colour.
  const gid = `eq-fill-${useId().replace(/:/g, "")}`;

  const rendersContribution =
    contributionMode ||
    (!days && s.daily_summary.equity_basis === "attribution_only");
  const start =
    startingBankroll ??
    (rendersContribution ? 0 : (s.daily_summary.starting_bankroll ?? 1000));
  const series = days ? equitySeriesFromDays(days, start) : equitySeries(s);
  const lineKey = rendersContribution ? "pnl" : "equity";
  if (!series.length) {
    return (
      <Widget className={`w-full min-w-0 max-w-full ${className ?? ""}`.trim()}>
        <Widget.Header className="flex-col items-start gap-2 py-1 sm:flex-row sm:justify-between">
          <div className="min-w-0">
            {eyebrow && (
              <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-accent">{eyebrow}</span>
            )}
            <Widget.Title className={emphasis === "headline" ? "text-base" : undefined}>
              {title ?? "Paper equity curve"}
            </Widget.Title>
            <Widget.Description>{description ?? "No dated series was published for this artifact."}</Widget.Description>
          </div>
        </Widget.Header>
        <Widget.Content>
          <div role="status" className="grid h-40 place-items-center rounded-xl border border-dashed border-border/70 bg-surface-secondary/50 px-4 text-center text-xs text-muted">
            No dated curve is available in this publication.
          </div>
        </Widget.Content>
      </Widget>
    );
  }
  const last = series[series.length - 1]?.[lineKey] ?? start;
  const win = windowDays ?? s.daily_summary.window_days ?? series.length;
  const up = last >= start;
  // Adaptive y-domain: pad proportional to the actual DATA swing (with a small
  // floor) so a book that barely moved still shows its shape. Padding keys off the
  // data range — not data∪break-even — so a book sitting entirely on one side of
  // break-even fills its plot instead of leaving a dead band up to the reference
  // line. The break-even line stays visible, but flush, with no overshoot past it.
  const eqs = series.map((d) => d[lineKey]);
  const dataLo = Math.min(...eqs);
  const dataHi = Math.max(...eqs);
  const pad = Math.max((dataHi - dataLo) * 0.15, 3);
  const lo = Math.min(dataLo - pad, start);
  const hi = Math.max(dataHi + pad, start);
  const yDomain: [number, number] = [Math.floor(lo), Math.ceil(hi)];
  const stroke = up ? "var(--color-success)" : "var(--color-danger)";
  const valueName = rendersContribution ? "P&L contribution" : "Equity";
  const chartH = height ?? EMPHASIS_HEIGHT[emphasis];
  const label = rendersContribution
    ? `${title ?? "Paper P&L curve"} with ${series.length} published daily points, ending at ${axisMoney(last, true)} cumulative attribution; zero is the break-even reference.`
    : `${title ?? "Paper equity curve"} over ${series.length} days, from ${axisMoney(start)} to ${axisMoney(last)} (${up ? "up" : "down"} over the window).`;

  return (
    <Widget className={`w-full min-w-0 max-w-full ${className ?? ""}`.trim()}>
      <Widget.Header className="flex-col items-start gap-3 py-1 sm:flex-row sm:justify-between">
        <div className="flex min-w-0 flex-col gap-0.5">
          {eyebrow && (
            <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-accent">{eyebrow}</span>
          )}
          <Widget.Title className={emphasis === "headline" ? "text-base" : undefined}>
            {title ?? "Paper equity curve"}
          </Widget.Title>
          <Widget.Description>
            {description ?? `Cumulative realized P&L over the reporting window · ${win}-day view`}
          </Widget.Description>
        </div>
        <Widget.Legend className="flex shrink-0 flex-wrap self-start pt-0.5">
          <Widget.LegendItem color={stroke}>{rendersContribution ? "P&L" : "equity"}</Widget.LegendItem>
          <Widget.LegendItem color="var(--color-muted)">{rendersContribution ? "break-even" : "start"}</Widget.LegendItem>
        </Widget.Legend>
      </Widget.Header>
      <Widget.Content>
        <div role="img" aria-label={label}>
          <LineChart data={series} height={chartH} margin={{ top: 8, right: 14, bottom: 0, left: 0 }}>
            <defs>
              <linearGradient id={gid} x1="0" x2="0" y1="0" y2="1">
                <stop offset="0%" stopColor={stroke} stopOpacity={0.18} />
                <stop offset="100%" stopColor={stroke} stopOpacity={0.01} />
              </linearGradient>
            </defs>
            <LineChart.Grid vertical={false} />
            <LineChart.XAxis dataKey="date" tickMargin={8} />
            <LineChart.YAxis width={56} tickFormatter={(v: number) => axisMoney(v, rendersContribution)} domain={yDomain} allowDecimals={false} />
            <ReferenceLine y={start} stroke="var(--color-muted)" strokeDasharray="5 5" strokeWidth={1.25} />
            <LineChart.Line dataKey={lineKey} name={valueName} stroke={stroke} strokeWidth={2.5} type="monotone" fill={`url(#${gid})`} />
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
