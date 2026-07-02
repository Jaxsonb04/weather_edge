import { Chip } from "@heroui/react";
import { DataGrid, type DataGridColumn } from "@heroui-pro/react";
import { cents, money, type MonitorAction, type StrategyLab } from "../../lib/strategy";
import { pct } from "../../lib/data";

const HEAD = "font-mono text-[11px] uppercase tracking-wider text-muted";

const STATUS_META: Record<string, { label: string; color: "success" | "danger" | "warning" | "default" }> = {
  CLOSE_TAKE_PROFIT: { label: "Take-profit", color: "success" },
  CLOSE_STOP_LOSS: { label: "Stop-loss", color: "danger" },
  CLOSE_MODEL_VETO: { label: "Model veto", color: "warning" },
  PAPER_CLOSED: { label: "Closed", color: "default" },
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

/** The monitor's most recent closes — the audit trail of rule-based exits. */
export function MonitorLog({ s }: { s: StrategyLab }) {
  const rows = collapseActions(s.paper_trading?.recent_monitor_actions ?? []);
  if (!rows.length) return null;

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
    { id: "qty", header: "Qty", align: "end", headerClassName: HEAD, cell: (d) => <span className="tnum">{d.contracts}</span> },
    {
      id: "fill",
      header: "Entry → Exit",
      align: "end",
      headerClassName: HEAD,
      cell: (d) => (
        <span className="tnum text-muted">
          {cents(d.entry_price)} → {cents(d.exit_price)}
        </span>
      ),
    },
    {
      id: "pnl",
      header: "P&L",
      align: "end",
      headerClassName: HEAD,
      accessorKey: "realized_pnl",
      allowsSorting: true,
      cell: (d) => (
        <span className={`tnum font-medium ${d.realized_pnl > 0 ? "text-success" : d.realized_pnl < 0 ? "text-danger" : "text-muted"}`}>
          {money(d.realized_pnl)}
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
      header: "Exit rule",
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
    <DataGrid
      aria-label="Recent monitor exits"
      columns={columns}
      data={rows}
      getRowId={(d) => d.id}
      variant="secondary"
      className="rounded-2xl"
    />
  );
}
