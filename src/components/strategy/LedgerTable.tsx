import { Chip } from "@heroui/react/chip";
import { DataGrid, type DataGridColumn } from "@heroui-pro/react/data-grid";
import { cityForTicker, pct, qualityColor, signedPct } from "../../lib/data";
import { cents, closedLedger, money, type ClosedPosition, type StrategyLab } from "../../lib/strategy";

const HEAD = "font-mono text-[11px] uppercase tracking-wider text-muted";

function toneColor(tone?: string): "success" | "danger" | "warning" | "default" {
  if (tone === "success") return "success";
  if (tone === "danger") return "danger";
  if (tone === "warning") return "warning";
  return "default";
}

interface LedgerTableProps {
  s: StrategyLab;
  /** cap the number of rows (omit for the full ledger) */
  limit?: number;
  /** show the extra detail columns (edge at entry, quality, exit) */
  detailed?: boolean;
  /** override the row set (e.g. a single profile's closed positions) */
  rows?: ClosedPosition[];
  /** drop the Book column (when the ledger is already scoped to one profile) */
  hideProfile?: boolean;
  /** shown when there are no rows */
  emptyNote?: string;
}

/** The closed-positions ledger. Compact (recent trades) or detailed (trading
    desk) via props, one source of truth for both. */
export function LedgerTable({ s, limit, detailed = false, rows: rowsProp, hideProfile = false, emptyNote }: LedgerTableProps) {
  const base = rowsProp ?? closedLedger(s);
  const rows = limit ? base.slice(0, limit) : base;
  if (!rows.length) {
    return (
      <p className="rounded-2xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted">
        {emptyNote ?? "No closed positions in this slice yet."}
      </p>
    );
  }

  const columns: DataGridColumn<ClosedPosition>[] = [
    {
      id: "bracket",
      header: "Bracket",
      isRowHeader: true,
      headerClassName: HEAD,
      cell: (d) => (
        <div className="flex items-center gap-2">
          <span className="font-medium text-foreground">{d.label}</span>
          <span className="rounded bg-foreground/8 px-1.5 py-0.5 font-mono text-[10px] font-medium uppercase text-muted">{d.side}</span>
        </div>
      ),
    },
    {
      id: "city",
      header: "City",
      headerClassName: HEAD,
      cell: (d) => {
        const city = cityForTicker(d.ticker ?? "");
        return city ? (
          <span title={city.name} className="font-mono text-[11px] font-medium uppercase text-muted">
            {city.slug}
          </span>
        ) : (
          <span className="text-xs text-muted">—</span>
        );
      },
    },
    ...(hideProfile
      ? []
      : ([
          {
            id: "profile",
            header: "Book",
            headerClassName: HEAD,
            cell: (d) => (
              <span
                className={`rounded px-1.5 py-0.5 font-mono text-[10px] font-medium uppercase ${
                  d.risk_profile === "live" ? "bg-accent-soft text-[color:var(--accent-text)]" : "bg-foreground/8 text-muted"
                }`}
              >
                {d.risk_profile}
              </span>
            ),
          },
        ] as DataGridColumn<ClosedPosition>[])),
    { id: "date", header: "Target", accessorKey: "target_date", allowsSorting: detailed, headerClassName: HEAD, cell: (d) => <span className="tnum text-muted">{d.target_date ? d.target_date.slice(5) : "—"}</span> },
    { id: "contracts", header: "Qty", align: "end", headerClassName: HEAD, cell: (d) => <span className="tnum">{d.contracts}</span> },
    {
      id: "fill",
      header: detailed ? "Entry → Exit" : "Entry",
      align: "end",
      headerClassName: HEAD,
      cell: (d) => (
        <span className="tnum text-muted">
          {detailed ? `${cents(d.entry_price)} → ${cents(d.exit_price)}` : cents(d.entry_price)}
        </span>
      ),
    },
    ...(detailed
      ? ([
          {
            id: "edge",
            header: "Edge @ entry",
            align: "end",
            headerClassName: HEAD,
            cell: (d) => (
              <span className={`tnum text-xs ${(d.edge ?? 0) >= 0 ? "text-success" : "text-danger"}`}>
                {d.edge == null ? "—" : signedPct(d.edge, 1)}
              </span>
            ),
          },
          {
            id: "quality",
            header: "Quality",
            align: "end",
            headerClassName: HEAD,
            accessorKey: "quality_score",
            allowsSorting: true,
            cell: (d) =>
              d.quality_score == null ? (
                <span className="text-xs text-muted">—</span>
              ) : (
                <span className="tnum text-xs font-medium" style={{ color: qualityColor(d.quality_score) }}>
                  {Math.round(d.quality_score)}
                </span>
              ),
          },
          {
            id: "settle",
            header: "Settled high",
            align: "end",
            headerClassName: HEAD,
            cell: (d) => (
              <span className="tnum text-xs text-muted">{d.settlement_high_f == null ? "—" : `${d.settlement_high_f}°`}</span>
            ),
          },
        ] as DataGridColumn<ClosedPosition>[])
      : []),
    {
      id: "pnl",
      header: "P&L",
      align: "end",
      allowsSorting: true,
      accessorKey: "realized_pnl",
      headerClassName: HEAD,
      cell: (d) => (
        <span className={`tnum font-medium ${d.realized_pnl > 0 ? "text-success" : d.realized_pnl < 0 ? "text-danger" : "text-muted"}`}>
          {money(d.realized_pnl)}
        </span>
      ),
    },
    ...(detailed
      ? ([
          {
            id: "roi",
            header: "ROI",
            align: "end",
            headerClassName: HEAD,
            cell: (d) => <span className="tnum text-xs text-muted">{d.realized_roi == null ? "—" : pct(d.realized_roi, 1)}</span>,
          },
        ] as DataGridColumn<ClosedPosition>[])
      : []),
    {
      id: "outcome",
      header: "Outcome",
      align: "end",
      headerClassName: HEAD,
      cell: (d) => (
        <Chip size="sm" variant="soft" color={toneColor(d.position_status_tone)}>
          <Chip.Label>{d.position_status_label ?? (d.realized_pnl >= 0 ? "Win" : "Loss")}</Chip.Label>
        </Chip>
      ),
    },
  ];

  return (
    <div className="w-full min-w-0 max-w-full overflow-x-auto overscroll-x-contain rounded-2xl" role="region" aria-label="Scrollable closed-position ledger" tabIndex={0}>
    <DataGrid
      aria-label={detailed ? "Full closed paper-trade ledger" : "Recent closed paper trades"}
      columns={columns}
      data={rows}
      getRowId={(d) => d.id}
      variant="secondary"
      className="min-w-[54rem] rounded-2xl"
    />
    </div>
  );
}
