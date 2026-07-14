import { Chip } from "@heroui/react/chip";
import { DataGrid, type DataGridColumn } from "@heroui-pro/react/data-grid";
import { cents, money, type MonitorAction, type StrategyLab } from "../../lib/strategy";
import { pct } from "../../lib/data";

const HEAD = "font-mono text-[11px] uppercase tracking-wider text-muted";

const STATUS_META: Record<string, { label: string; color: "success" | "danger" | "warning" | "default" }> = {
  CLOSE_TAKE_PROFIT: { label: "Take-profit", color: "success" },
  CLOSE_STOP_LOSS: { label: "Stop-loss", color: "danger" },
  CLOSE_MODEL_VETO: { label: "Model veto", color: "warning" },
  PAPER_CLOSED: { label: "Closed", color: "default" },
  PAPER_SETTLED: { label: "Settled", color: "default" },
  HOLD: { label: "Hold · unrealized", color: "default" },
  OPEN: { label: "Open", color: "default" },
  LIMIT_RESTING: { label: "Limit resting", color: "default" },
};

function statusMeta(status?: string) {
  if (!status) return STATUS_META.PAPER_CLOSED;
  return STATUS_META[status] ?? { label: status.replace(/^CLOSE_/, "").replace(/_/g, " ").toLowerCase(), color: "default" as const };
}

/** Each close is journaled twice: a generic PAPER_CLOSED marker plus the
    specific exit-rule entry with the reasoning. Collapse to one row per
    position, keeping the informative one. */
function collapseActions(raw: MonitorAction[]): MonitorAction[] {
  const byId = new Map<number, MonitorAction>();
  for (const a of raw) {
    const prev = byId.get(a.id);
    if (!prev || (prev.status === "PAPER_CLOSED" && a.status !== "PAPER_CLOSED")) byId.set(a.id, a);
  }
  return [...byId.values()].sort((a, b) => (b.time ?? "").localeCompare(a.time ?? ""));
}

/** Monitor decisions and executed closes, with unrealized marks labeled. */
export function MonitorLog({
  s,
  rows: rowsProp,
  hideProfile = false,
  emptyNote,
}: {
  s: StrategyLab;
  /** override the raw action set (e.g. one profile's) — collapsed internally */
  rows?: MonitorAction[];
  hideProfile?: boolean;
  emptyNote?: string;
}) {
  const rows = collapseActions(rowsProp ?? s.paper_trading?.recent_monitor_actions ?? []);
  if (!rows.length) {
    return emptyNote ? (
      <p className="rounded-2xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted">{emptyNote}</p>
    ) : null;
  }

  const columns: DataGridColumn<MonitorAction>[] = [
    {
      id: "time",
      header: "When",
      headerClassName: HEAD,
      accessorKey: "time",
      allowsSorting: true,
      cell: (d) => (
        <span className="tnum font-mono text-xs text-muted">{d.time ? d.time.slice(5, 16).replace("T", " ") : "—"}</span>
      ),
    },
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
    ...(hideProfile
      ? []
      : ([
          {
            id: "book",
            header: "Book",
            headerClassName: HEAD,
            cell: (d) => (
              <span className={`rounded px-1.5 py-0.5 font-mono text-[10px] font-medium uppercase ${d.risk_profile === "live" ? "bg-accent-soft text-[color:var(--accent-text)]" : "bg-foreground/8 text-muted"}`}>
                {d.risk_profile}
              </span>
            ),
          },
        ] as DataGridColumn<MonitorAction>[])),
    { id: "qty", header: "Qty", align: "end", headerClassName: HEAD, cell: (d) => <span className="tnum">{d.contracts ?? "—"}</span> },
    {
      id: "fill",
      header: "Entry → Exit / mark",
      align: "end",
      headerClassName: HEAD,
      cell: (d) => (
        <span className="tnum text-muted">
          {cents(d.entry_price)} → {cents(d.exit_price)}{d.unrealized ? " mark" : ""}
        </span>
      ),
    },
    {
      id: "pnl",
      header: "P&L / mark",
      align: "end",
      headerClassName: HEAD,
      accessorKey: "realized_pnl",
      allowsSorting: true,
      cell: (d) => (
        <span className={`tnum font-medium ${(d.realized_pnl ?? 0) > 0 ? "text-success" : (d.realized_pnl ?? 0) < 0 ? "text-danger" : "text-muted"}`}>
          {d.unrealized ? "Unrealized " : ""}{d.realized_pnl == null ? "—" : money(d.realized_pnl)}
        </span>
      ),
    },
    {
      id: "roi",
      header: "ROI",
      align: "end",
      headerClassName: HEAD,
      cell: (d) => <span className="tnum text-muted">{d.realized_roi == null ? "—" : pct(d.realized_roi, 1)}</span>,
    },
    {
      id: "rule",
      header: "Decision",
      headerClassName: HEAD,
      cell: (d) => {
        const meta = statusMeta(d.status);
        return (
          <div className="flex items-center gap-2">
            <Chip size="sm" variant="soft" color={meta.color}>
              <Chip.Label>{meta.label}</Chip.Label>
            </Chip>
            {d.note && d.note !== "closed by monitor" && (
              <span className="max-w-56 truncate text-xs text-muted" title={d.note}>
                {d.note}
              </span>
            )}
          </div>
        );
      },
    },
  ];

  return (
    <div className="w-full min-w-0 max-w-full overflow-x-auto overscroll-x-contain rounded-2xl" role="region" aria-label="Scrollable monitor decisions" tabIndex={0}>
    <DataGrid
      aria-label="Recent monitor decisions and executed closes"
      columns={columns}
      data={rows}
      getRowId={(d) => d.id}
      variant="secondary"
      className="min-w-[48rem] rounded-2xl"
    />
    </div>
  );
}
