import { DataGrid, type DataGridColumn } from "@heroui-pro/react/data-grid";
import { gateDeferred, money, type DayRow, type StrategyLab } from "../../lib/strategy";

const HEAD = "font-mono text-[11px] uppercase tracking-wider text-muted";

/** One row per day of the reporting window: scanning activity, entries, exits,
    P&L, and how the forecast actually verified. `days` overrides the combined
    series (e.g. a single profile's days). */
export function DailyActivity({ s, days }: { s: StrategyLab; days?: DayRow[] }) {
  const rows = [...(days ?? s.daily_summary?.days ?? [])].sort((a, b) => b.date.localeCompare(a.date));
  if (!rows.length) return null;
  // The per-day scan/approval counters come from the same decision-journal
  // replay the bounded public refresh defers. When that section is unpopulated
  // every day reports 0 scans beside a day that demonstrably opened trades, so
  // those two columns report "no measurement" instead of a stub zero.
  const scanCountsDeferred = gateDeferred(s.daily_summary?.gate_behavior);
  const scanCount = (value: number | null | undefined) =>
    scanCountsDeferred ? "—" : value?.toLocaleString() ?? "—";

  const columns: DataGridColumn<DayRow>[] = [
    {
      id: "date",
      header: "Date",
      isRowHeader: true,
      headerClassName: HEAD,
      accessorKey: "date",
      allowsSorting: true,
      cell: (d) => <span className="tnum font-mono text-xs">{d.date.slice(5)}</span>,
    },
    {
      id: "signals",
      header: "Scans",
      align: "end",
      headerClassName: HEAD,
      cell: (d) => <span className="tnum text-muted">{scanCount(d.signals)}</span>,
    },
    {
      id: "approved",
      header: "Approved",
      align: "end",
      headerClassName: HEAD,
      cell: (d) => <span className="tnum">{scanCount(d.approved_signals)}</span>,
    },
    {
      id: "opened",
      header: "Opened",
      align: "end",
      headerClassName: HEAD,
      cell: (d) => <span className="tnum">{d.opened ?? d.trades_opened ?? 0}</span>,
    },
    {
      id: "closed",
      header: "Closed",
      align: "end",
      headerClassName: HEAD,
      cell: (d) => <span className="tnum">{d.closed ?? 0}</span>,
    },
    {
      id: "wl",
      header: "W–L",
      align: "end",
      headerClassName: HEAD,
      cell: (d) => (
        <span className="tnum text-muted">
          {(d.wins ?? 0)}–{(d.losses ?? 0)}
        </span>
      ),
    },
    {
      id: "pnl",
      header: "Day P&L",
      align: "end",
      headerClassName: HEAD,
      cell: (d) => (
        <span className={`tnum font-medium ${(d.realized_pnl ?? 0) > 0 ? "text-success" : (d.realized_pnl ?? 0) < 0 ? "text-danger" : "text-muted"}`}>
          {money(d.realized_pnl ?? 0)}
        </span>
      ),
    },
    {
      id: "cum",
      header: "Cumulative",
      align: "end",
      headerClassName: HEAD,
      cell: (d) => (
        <span className={`tnum ${d.cumulative_realized > 0 ? "text-success" : d.cumulative_realized < 0 ? "text-danger" : "text-muted"}`}>
          {money(d.cumulative_realized)}
        </span>
      ),
    },
    {
      id: "forecast",
      header: "Forecast → Actual",
      align: "end",
      headerClassName: HEAD,
      cell: (d) => {
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
  ];

  return (
    <div className="min-w-0 space-y-2">
      <div className="w-full min-w-0 max-w-full overflow-x-auto overscroll-x-contain rounded-2xl" role="region" aria-label="Scrollable daily activity table" tabIndex={0}>
        <DataGrid
          aria-label="Daily trading and forecast activity"
          columns={columns}
          data={rows}
          getRowId={(d) => d.date}
          variant="secondary"
          className="min-w-[48rem] rounded-2xl"
        />
      </div>
      {scanCountsDeferred && (
        <p className="text-[11px] leading-relaxed text-muted">
          Scans and Approved are deferred from this bounded public refresh and show no value. Entries, exits, P&amp;L
          and forecast verification are measured.
        </p>
      )}
    </div>
  );
}
