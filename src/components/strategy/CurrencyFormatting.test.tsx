import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { StrategyLab, WinnerLoser } from "../../lib/strategy";

const chartState = vi.hoisted(() => ({
  tooltipRow: { equity: -5, pnl: 2, dailyPnl: 0 } as {
    equity: number;
    pnl: number;
    dailyPnl: number;
    accountBalance?: number;
  } | null,
  chartData: [] as Array<{
    equity: number;
    pnl: number;
    dailyPnl: number;
    accountBalance?: number;
  }>,
  tooltipProps: {} as { cursor?: unknown; isAnimationActive?: boolean },
}));

vi.mock("@iconify/react/offline", () => ({ Icon: () => null }));
vi.mock("recharts", () => ({ ReferenceLine: () => null }));
vi.mock("@heroui/react/card", () => {
  const Part = ({ children }: { children?: React.ReactNode }) => <div>{children}</div>;
  const Card = Part as typeof Part & { Header: typeof Part; Content: typeof Part; Title: typeof Part };
  Card.Header = Part;
  Card.Content = Part;
  Card.Title = Part;
  return { Card };
});
vi.mock("@heroui-pro/react/widget", () => {
  const Part = ({ children }: { children?: React.ReactNode }) => <div>{children}</div>;
  const Widget = Part as typeof Part & {
    Header: typeof Part;
    Title: typeof Part;
    Description: typeof Part;
    Legend: typeof Part & { displayName?: string };
    LegendItem: typeof Part;
    Content: typeof Part;
  };
  Widget.Header = Part;
  Widget.Title = Part;
  Widget.Description = Part;
  Widget.Legend = Part;
  Widget.LegendItem = Part;
  Widget.Content = Part;
  return { Widget };
});
vi.mock("@heroui-pro/react/chart-tooltip", () => {
  const Part = ({ children }: { children?: React.ReactNode }) => <div>{children}</div>;
  const Value = ({ children }: { children?: React.ReactNode }) => <span data-testid="tooltip-value">{children}</span>;
  const ChartTooltip = Part as typeof Part & {
    Header: typeof Part;
    Item: typeof Part;
    Indicator: () => null;
    Label: typeof Part;
    Value: typeof Value;
  };
  ChartTooltip.Header = Part;
  ChartTooltip.Item = Part;
  ChartTooltip.Indicator = () => null;
  ChartTooltip.Label = Part;
  ChartTooltip.Value = Value;
  return { ChartTooltip };
});
vi.mock("@heroui-pro/react/line-chart", () => {
  const Part = ({
    children,
    data,
  }: {
    children?: React.ReactNode;
    data?: Array<{ equity: number; pnl: number; dailyPnl: number; accountBalance?: number }>;
  }) => {
    if (data) chartState.chartData = data;
    return <div>{children}</div>;
  };
  const Tooltip = ({
    content,
    cursor,
    isAnimationActive,
  }: {
    content: (args: unknown) => React.ReactNode;
    cursor?: unknown;
    isAnimationActive?: boolean;
  }) => {
    chartState.tooltipProps = { cursor, isAnimationActive };
    const row = chartState.tooltipRow ?? chartState.chartData[0];
    return <div>{content({ active: true, label: "Jul 11", payload: [{ payload: row }] })}</div>;
  };
  const LineChart = Part as typeof Part & {
    Grid: () => null;
    XAxis: () => null;
    YAxis: () => null;
    Line: () => null;
    Tooltip: typeof Tooltip;
  };
  LineChart.Grid = () => null;
  LineChart.XAxis = () => null;
  LineChart.YAxis = () => null;
  LineChart.Line = () => null;
  LineChart.Tooltip = Tooltip;
  return { LineChart };
});

import { EquityCurve } from "./EquityCurve";
import { MoversCard } from "./MoversCard";

const mover = (realized_pnl: number, label: string): WinnerLoser => ({
  label,
  side: "YES",
  ticker: `TEST-${label}`,
  target_date: "2026-07-11",
  realized_pnl,
  quality_score: 80,
});

beforeEach(() => {
  chartState.tooltipRow = { equity: -5, pnl: 2, dailyPnl: 0 };
  chartState.chartData = [];
  chartState.tooltipProps = {};
});

describe("strategy component currency formatting", () => {
  it("uses canonical positive, negative, and zero money strings for movers", () => {
    const s = {
      daily_summary: {
        biggest_winners: [mover(2, "positive"), mover(0, "zero")],
        biggest_losers: [mover(-2, "negative")],
      },
    } as StrategyLab;

    render(<MoversCard s={s} />);

    expect(screen.getByText("+$2.00")).toBeInTheDocument();
    expect(screen.getByText("+$0.00")).toBeInTheDocument();
    expect(screen.getByText("−$2.00")).toBeInTheDocument();
  });

  it("derives each equity tooltip sign from the value being formatted", () => {
    const s = { daily_summary: { starting_bankroll: 0, window_days: 1 } } as StrategyLab;

    render(
      <EquityCurve
        s={s}
        days={[{ date: "2026-07-11", cumulative_realized: -5 }]}
        startingBankroll={0}
      />,
    );

    expect(screen.getAllByTestId("tooltip-value").map((node) => node.textContent)).toEqual([
      "−$5.00",
      "+$2.00",
      "+$0.00",
    ]);
  });

  it("labels a combined attribution-only series as P&L instead of account equity", () => {
    chartState.tooltipRow = null;
    const s = {
      daily_summary: {
        equity_available: false,
        equity_basis: "attribution_only",
        starting_bankroll: null,
        current_equity: null,
        window_days: 1,
        days: [{ date: "2026-07-11", cumulative_realized: -5, closing_equity: null }],
      },
    } as unknown as StrategyLab;

    render(<EquityCurve s={s} />);

    expect(screen.getByText("P&L contribution")).toBeInTheDocument();
    expect(screen.getByText("break-even")).toBeInTheDocument();
    expect(screen.queryByText("Equity")).not.toBeInTheDocument();
    expect(screen.queryByText("Account balance")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("tooltip-value").map((node) => node.textContent)).toEqual([
      "−$5.00",
      "+$0.00",
    ]);
  });

  it("tracks rapid pointer movement without an animated tooltip or independent cursor", () => {
    const s = { daily_summary: { starting_bankroll: 0, window_days: 1 } } as StrategyLab;

    render(
      <EquityCurve
        s={s}
        days={[{ date: "2026-07-11", cumulative_realized: -5 }]}
        startingBankroll={0}
        contributionMode
      />,
    );

    expect(chartState.tooltipProps).toEqual({
      cursor: false,
      isAnimationActive: false,
    });
  });

  it("keeps attributed P&L separate from a published account closing balance", () => {
    chartState.tooltipRow = null;
    const s = { daily_summary: { starting_bankroll: 0, window_days: 1 } } as StrategyLab;

    render(
      <EquityCurve
        s={s}
        days={[{
          date: "2026-07-11",
          cumulative_realized: 5,
          realized_pnl: 2,
          closing_equity: 1005,
        }]}
        startingBankroll={0}
        contributionMode
      />,
    );

    expect(screen.getByRole("img")).toHaveAttribute(
      "aria-label",
      expect.stringContaining("from $0 to +$5"),
    );
    expect(screen.getByText("P&L contribution")).toBeInTheDocument();
    expect(screen.getByText("Daily P&L")).toBeInTheDocument();
    expect(screen.getByText("Account balance")).toBeInTheDocument();
    expect(screen.queryByText("Cum. P&L")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("tooltip-value").map((node) => node.textContent)).toEqual([
      "+$5.00",
      "+$2.00",
      "$1,005.00",
    ]);
  });
});
