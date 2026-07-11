import { useMemo, useState } from "react";
import { Chip } from "@heroui/react/chip";
import { DataGrid, type DataGridColumn } from "@heroui-pro/react/data-grid";
import { pct, qualityColor, signedPct, type Decision, type Target } from "../../lib/data";
import { BracketSheet } from "./BracketSheet";

const HEAD = "font-mono text-[11px] uppercase tracking-wider text-muted";

export function MarketBook({ target }: { target: Target }) {
  const [active, setActive] = useState<Decision | null>(null);
  const [open, setOpen] = useState(false);

  const rows = useMemo(
    () => [...target.decisions].sort((a, b) => b.trade_quality_score - a.trade_quality_score),
    [target.decisions],
  );
  const rowId = (d: Decision) => `${d.ticker}-${d.side}`;

  const columns: DataGridColumn<Decision>[] = [
    {
      id: "bracket",
      header: "Bracket",
      isRowHeader: true,
      headerClassName: HEAD,
      cell: (d) => (
        <div className="flex items-center gap-2">
          <span className="font-medium text-foreground">{d.label}</span>
          <span className="rounded bg-foreground/8 px-1.5 py-0.5 font-mono text-[10px] font-medium uppercase text-muted">
            {d.side}
          </span>
        </div>
      ),
    },
    { id: "model", header: "Model", align: "end", allowsSorting: true, accessorKey: "model_probability", headerClassName: HEAD, cell: (d) => <span className="tnum">{pct(d.model_probability)}</span> },
    { id: "market", header: "Market", align: "end", allowsSorting: true, accessorKey: "market_probability", headerClassName: HEAD, cell: (d) => <span className="tnum text-muted">{pct(d.market_probability)}</span> },
    {
      id: "edge",
      header: "Edge",
      align: "end",
      allowsSorting: true,
      accessorKey: "edge",
      headerClassName: HEAD,
      cell: (d) => (
        <span className={`tnum font-medium ${d.edge > 0.001 ? "text-success" : d.edge < -0.001 ? "text-danger" : "text-muted"}`}>
          {signedPct(d.edge, 1)}
        </span>
      ),
    },
    {
      id: "quality",
      header: "Quality",
      align: "end",
      allowsSorting: true,
      accessorKey: "trade_quality_score",
      headerClassName: HEAD,
      cell: (d) => (
        <div className="ml-auto flex w-24 items-center gap-2">
          <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-foreground/10">
            <div
              className="h-full rounded-full"
              style={{ width: `${Math.min(100, Math.max(0, d.trade_quality_score))}%`, background: qualityColor(d.trade_quality_score) }}
            />
          </div>
          <span className="tnum w-6 text-right text-xs">{Math.round(d.trade_quality_score)}</span>
        </div>
      ),
    },
    {
      id: "call",
      header: "Call",
      align: "end",
      headerClassName: HEAD,
      cell: (d) => (
        <Chip size="sm" variant="soft" color={d.decision === "TRADE" ? "success" : "default"}>
          <Chip.Label>{d.decision === "TRADE" ? "Trade" : "No trade"}</Chip.Label>
        </Chip>
      ),
    },
  ];

  return (
    <>
      <DataGrid
        aria-label={`Prediction-market brackets for ${target.target_date}`}
        columns={columns}
        data={rows}
        getRowId={rowId}
        variant="secondary"
        className="rounded-2xl"
        onRowAction={(key) => {
          const d = rows.find((r) => rowId(r) === String(key));
          if (d) {
            setActive(d);
            setOpen(true);
          }
        }}
      />
      <p className="mt-2 px-1 text-xs text-muted">Select any bracket for its full gate trace.</p>
      <BracketSheet decision={active} isOpen={open} onOpenChange={setOpen} />
    </>
  );
}
