import { Chip } from "@heroui/react";
import { DataGrid, type DataGridColumn } from "@heroui-pro/react";
import { recentTrades, type ClosedPosition, type StrategyLab } from "../../lib/strategy";

const HEAD = "font-mono text-[11px] uppercase tracking-wider text-muted";
const cents = (p: number | null) => (p == null ? "—" : `${Math.round(p * 100)}¢`);
const money = (n: number) => `${n >= 0 ? "+" : "−"}$${Math.abs(n).toFixed(2)}`;

function toneColor(tone?: string): "success" | "danger" | "warning" | "default" {
  if (tone === "success") return "success";
  if (tone === "danger") return "danger";
  if (tone === "warning") return "warning";
  return "default";
}

export function TradesTable({ s }: { s: StrategyLab }) {
  const rows = recentTrades(s, 12);

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
    { id: "date", header: "Target", accessorKey: "target_date", headerClassName: HEAD, cell: (d) => <span className="tnum text-muted">{d.target_date.slice(5)}</span> },
    { id: "contracts", header: "Qty", align: "end", headerClassName: HEAD, cell: (d) => <span className="tnum">{d.contracts}</span> },
    { id: "entry", header: "Entry", align: "end", headerClassName: HEAD, cell: (d) => <span className="tnum text-muted">{cents(d.entry_price)}</span> },
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
    <DataGrid
      aria-label="Recent closed paper trades"
      columns={columns}
      data={rows}
      getRowId={(d) => d.id}
      variant="secondary"
      className="rounded-2xl"
    />
  );
}
