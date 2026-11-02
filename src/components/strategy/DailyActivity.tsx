import { DataGrid, type DataGridColumn } from "@heroui-pro/react/data-grid";
import { gateDeferred, money, type DayRow, type StrategyLab } from "../../lib/strategy";
import { PAPER_INCIDENTS } from "../../lib/incidents";

const HEAD = "text-xs font-medium text-muted";

const incidentFor = (date: string) =>
  PAPER_INCIDENTS.find((incident) => incident.start <= date && incident.end >= date);

/** One row per day for ONE account: entries, exits, P&L and (where published)
    how the forecast verified. Pass that account's own `days`; the combined
    series sums separate accounts and is never rendered here. */
export function DailyActivity({ s, days }: { s: StrategyLab; days?: DayRow[] }) {
  const rows = [...(days ?? [])].sort((a, b) => b.date.localeCompare(a.date));
  if (!rows.length) return null;
  // The per-day scan/approval counters come from the decision-journal replay
  // the bounded public refresh defers; when deferred they show no value
  // instead of a stub zero beside a day that demonstrably opened trades.
  const scanCountsDeferred = gateDeferred(
    s.daily_summary?.gate_behavior,
    s.daily_summary?.decision_analytics,
  );
  const scanCount = (value: number | null | undefined) =>
    scanCountsDeferred ? "—" : value?.toLocaleString() ?? "—";
  const hasScans = !scanCountsDeferred && rows.some((d) => d.signals != null);
  const hasForecast = rows.some((d) => d.forecast_predicted_high_f != null);

  const columns: DataGridColumn<DayRow>[] = [
    {
      id: "date",
      header: "Date",
      isRowHeader: true,
      headerClassName: HEAD,
      accessorKey: "date",
      allowsSorting: true,
      cell: (d) => {
        const incident = incidentFor(d.date);
        return (
          <span className="tnum whitespace-nowrap text-xs">
            {d.date.slice(5)}
            {incident && <span className="ml-1.5 text-warning" title={incident.detail}>{incident.short}</span>}
          </span>
        );
      },
    },
    ...(hasScans
      ? ([
          { id: "signals", header: "Scans", align: "end", headerClassName: HEAD, cell: (d) => <span className="tnum text-muted">{scanCount(d.signals)}</span> },
          { id: "approved", header: "Approved", align: "end", headerClassName: HEAD, cell: (d) => <span className="tnum">{scanCount(d.approved_signals)}</span> },
        ] as DataGridColumn<DayRow>[])
      : []),
    {
      id: "opened",
      header: "Opened",
      align: "end",
      headerClassName: HEAD,
      cell: (d) => <span className="tnum">{d.opened ?? d.trades_opened ?? "—"}</span>,
    },
    {
      id: "closed",
      header: "Resolved",
      align: "end",
      headerClassName: HEAD,
      cell: (d) => <span className="tnum">{d.resolved ?? d.closed ?? "—"}</span>,
    },
    {
      id: "wl",
      header: "W–L",
      align: "end",
      headerClassName: HEAD,
      cell: (d) => <span className="tnum text-muted">{d.wins != null && d.losses != null ? `${d.wins}–${d.losses}` : "—"}</span>,
    },
    {
      id: "pnl",
      header: "Day P&L",
      align: "end",
      headerClassName: HEAD,
      cell: (d) => (
        <span className={`tnum font-medium ${(d.realized_pnl ?? 0) > 0 ? "text-success" : (d.realized_pnl ?? 0) < 0 ? "text-danger" : "text-muted"}`}>
          {d.realized_pnl == null ? "—" : money(d.realized_pnl)}
        </span>
      ),
    },
    {
      id: "balance",
      header: "Balance",
      align: "end",
      headerClassName: HEAD,
      cell: (d) => (
        <span className="tnum text-muted">
          {d.closing_equity == null ? "—" : money(d.closing_equity, { sign: "negative-only" })}
        </span>
      ),
    },
    ...(hasForecast
      ? ([
          {
            id: "forecast",
            header: "Forecast → Actual",
            align: "end",
            headerClassName: HEAD,
            cell: (d: DayRow) => {
              if (d.forecast_predicted_high_f == null) return <span className="text-xs text-muted">—</span>;
              const err = d.forecast_error_f;
              return (
                <span className="tnum text-xs text-muted">
                  {Math.round(d.forecast_predicted_high_f * 10) / 10}° →{" "}
                  {d.forecast_actual_high_f == null ? "—" : `${Math.round(d.forecast_actual_high_f * 10) / 10}°`}
                  {err != null && (
                    <span className={Math.abs(err) <= 2 ? "text-success" : "text-warning"}> ({err > 0 ? "+" : ""}{Math.round(err * 10) / 10}°)</span>
                  )}
                </span>
              );
            },
          },
        ] as DataGridColumn<DayRow>[])
      : []),
  ];

  return (
    <div
      className="w-full min-w-0 max-w-full overflow-x-auto overscroll-x-contain rounded-2xl"
      role="region"
      aria-label="Scrollable daily activity table"
      tabIndex={0}
    >
      <DataGrid
        aria-label="Daily activity for this account"
        columns={columns}
        data={rows}
        getRowId={(d) => d.date}
        variant="secondary"
        className="min-w-[36rem] rounded-2xl"
      />
    </div>
  );
}
