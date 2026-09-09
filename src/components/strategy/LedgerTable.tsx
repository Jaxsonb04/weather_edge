import { Chip } from "@heroui/react/chip";
import { DataGrid, type DataGridColumn } from "@heroui-pro/react/data-grid";
import { cityForTicker, pct, qualityColor, signedPct } from "../../lib/data";
import {
  cents,
  closedLedger,
  money,
  resolutionTime,
  type ClosedPosition,
  type PositionStatusTone,
  type StrategyLab,
} from "../../lib/strategy";

const HEAD = "font-mono text-[11px] uppercase tracking-wider text-muted";

const mobileQuery = "(max-width: 767px)";
const subscribeMobile = (notify: () => void) => {
  const media = window.matchMedia(mobileQuery);
  media.addEventListener("change", notify);
  return () => media.removeEventListener("change", notify);
};
const getMobileSnapshot = () => window.matchMedia(mobileQuery).matches;

/** The runtime emits good/bad/warn; the HeroUI colour names are accepted
    aliases. Keyed off the union so a new tone fails type-check rather than
    silently falling through to a grey chip. */
const TONE_OUTCOME: Record<PositionStatusTone, { color: "success" | "danger" | "warning"; label: string }> = {
  good: { color: "success", label: "Win" },
  success: { color: "success", label: "Win" },
  bad: { color: "danger", label: "Loss" },
  danger: { color: "danger", label: "Loss" },
  warn: { color: "warning", label: "Flat" },
  warning: { color: "warning", label: "Flat" },
};

/** The published `position_status_label` is the constant "Resolved" on every
    row, which makes wins and losses visually identical. The runtime's own tone
    carries the outcome, so it decides both the colour and the word; the P&L sign
    is only a fallback for an artifact that publishes no tone. */
function outcomeFor(d: ClosedPosition) {
  const mapped = d.position_status_tone ? TONE_OUTCOME[d.position_status_tone] : undefined;
  if (mapped) return mapped;
  if (d.realized_pnl === 0) return { color: "warning", label: "Flat" } as const;
  return d.realized_pnl >= 0
    ? ({ color: "success", label: "Win" } as const)
    : ({ color: "danger", label: "Loss" } as const);
}

const resolvedFormatter = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZone: "UTC",
});

/** The timestamp the ledger is ordered by, so the ordering is checkable. */
function resolvedAt(d: ClosedPosition) {
  const raw = resolutionTime(d);
  if (!raw) return null;
  const parsed = new Date(raw);
  return Number.isNaN(parsed.getTime()) ? raw : resolvedFormatter.format(parsed);
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
  const mobile = useSyncExternalStore(subscribeMobile, getMobileSnapshot, () => false);
  const [showDetails, setShowDetails] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const [scrollState, setScrollState] = useState({ left: false, right: false });
  const base = rowsProp ?? closedLedger(s);
  const rows = limit ? base.slice(0, limit) : base;

  useEffect(() => {
    const scroller = root.current?.querySelector<HTMLElement>('[data-slot="table-scroll-container"]');
    if (!scroller) return;
    const update = () => {
      const left = scroller.scrollLeft > 1;
      const right = scroller.scrollWidth - scroller.clientWidth - scroller.scrollLeft > 1;
      setScrollState((current) => current.left === left && current.right === right ? current : { left, right });
    };
    update();
    scroller.addEventListener("scroll", update, { passive: true });
    const observer = new ResizeObserver(update);
    observer.observe(scroller);
    if (scroller.firstElementChild) observer.observe(scroller.firstElementChild);
    return () => { scroller.removeEventListener("scroll", update); observer.disconnect(); };
  }, [mobile, showDetails, rows.length]);

  const scrollColumns = (direction: number) => {
    const scroller = root.current?.querySelector<HTMLElement>('[data-slot="table-scroll-container"]');
    scroller?.scrollBy({
      left: direction * scroller.clientWidth * 0.65,
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth",
    });
  };
  if (!rows.length) {
    return (
      <p className="rounded-2xl border border-dashed border-border/70 px-4 py-6 text-center text-sm text-muted">
        {emptyNote ?? "No closed positions in this slice yet."}
      </p>
    );
  }

  if (mobile) {
    return (
      <div className="ledger-mobile">
        <p className="px-4 pb-2 pt-4 text-xs text-muted">{rows.length} records · tap a position for trade details</p>
        <Accordion aria-label="Closed paper positions" allowsMultipleExpanded hideSeparator>
          {rows.map((d) => {
            const city = cityForTicker(d.ticker ?? "");
            const outcome = outcomeFor(d);
            return (
              <Accordion.Item key={d.id} id={String(d.id)} className="ledger-record">
                <Accordion.Heading level={5}>
                  <Accordion.Trigger className="ledger-record-trigger">
                    <span className="min-w-0 flex-1 text-left">
                      <span className="mb-1 block text-xs font-normal text-muted">{city?.name ?? "Unmapped city"} · {d.target_date ?? "Target unavailable"}</span>
                      <span className="block text-sm font-semibold text-foreground">{d.label} <span className="ml-1 font-mono text-xs text-muted">{d.side}</span></span>
                      {!hideProfile && <span className="mt-1 block text-xs font-normal text-muted">{d.risk_profile}</span>}
                    </span>
                    <span className="flex shrink-0 flex-col items-end gap-1.5">
                      <span className={`tnum font-display text-base font-semibold ${d.realized_pnl > 0 ? "text-success" : d.realized_pnl < 0 ? "text-danger" : "text-muted"}`}>{money(d.realized_pnl)}</span>
                      <Chip size="sm" variant="soft" color={outcome.color}><Chip.Label>{outcome.label}</Chip.Label></Chip>
                    </span>
                    <Accordion.Indicator aria-hidden="true" className="ml-1 shrink-0 text-muted" />
                  </Accordion.Trigger>
                </Accordion.Heading>
                <Accordion.Panel>
                  <Accordion.Body className="px-4 pb-4 pt-1">
                    <dl className="ledger-record-details">
                      <div><dt>Entry → Exit</dt><dd>{cents(d.entry_price)} → {cents(d.exit_price)}</dd></div>
                      <div><dt>Contracts</dt><dd>{d.contracts}</dd></div>
                      <div><dt>Resolved · UTC</dt><dd>{resolvedAt(d) ?? "—"}</dd></div>
                      <div><dt>Return on risk</dt><dd>{d.realized_roi == null ? "—" : pct(d.realized_roi, 1)}</dd></div>
                      {detailed && <>
                        <div><dt>Edge at entry</dt><dd>{d.edge == null ? "—" : signedPct(d.edge, 1)}</dd></div>
                        <div><dt>Quality</dt><dd>{d.quality_score == null ? "—" : Math.round(d.quality_score)}</dd></div>
                        <div><dt>Settled high</dt><dd>{d.settlement_high_f == null ? "—" : `${d.settlement_high_f}°F`}</dd></div>
                      </>}
                    </dl>
                    <p className="mt-3 text-xs leading-relaxed text-muted">{d.outcome_reason ?? d.position_status_label ?? "Resolution details not published."}</p>
                  </Accordion.Body>
                </Accordion.Panel>
              </Accordion.Item>
            );
          })}
        </Accordion>
      </div>
    );
  }

  const columns: DataGridColumn<ClosedPosition>[] = [
    {
      id: "bracket",
      header: "Bracket",
      isRowHeader: true,
      pinned: "start",
      width: 190,
      headerClassName: HEAD,
      cell: (d) => (
        <div className="min-w-0">
          <span className="block text-[11px] text-muted">{cityForTicker(d.ticker ?? "")?.name ?? "Unmapped city"}</span>
          <span className="font-medium text-foreground">{d.label}</span>{" "}
          <span className="font-mono text-[11px] font-medium text-muted">{d.side}</span>
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
    ...(detailed
      ? ([
          {
            id: "resolved",
            header: "Resolved · UTC",
            headerClassName: HEAD,
            cell: (d) => <span className="tnum whitespace-nowrap text-xs text-muted">{resolvedAt(d) ?? "—"}</span>,
          },
        ] as DataGridColumn<ClosedPosition>[])
      : []),
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
      pinned: "end",
      width: 104,
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
      cell: (d) => {
        const outcome = outcomeFor(d);
        return (
          <span title={d.outcome_reason ?? d.position_status_label ?? undefined}>
            <Chip size="sm" variant="soft" color={outcome.color}>
              <Chip.Label>{outcome.label}</Chip.Label>
            </Chip>
          </span>
        );
      },
    },
  ];

  // Keep the pinned result at the physical end of the table, including when
  // secondary evidence columns are added or removed.
  const visibleColumns = columns.filter((column) => column.id !== "city" && column.id !== "pnl" &&
    (showDetails || !["edge", "quality", "settle", "roi"].includes(column.id)));
  visibleColumns.push(columns.find((column) => column.id === "pnl")!);

  return (
    <div ref={root} className="ledger-desktop w-full min-w-0 max-w-full">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
        <p className="text-xs text-muted">{rows.length} published records <span className="text-foreground/30">/</span> Paper execution</p>
        {detailed && <Button variant="secondary" size="sm" className="min-h-11 text-foreground" aria-pressed={showDetails} onPress={() => setShowDetails((value) => !value)}>{showDetails ? "Essential columns" : "All trade details"}</Button>}
      </div>
      {(scrollState.left || scrollState.right) && <div className="flex items-center justify-between gap-3 border-t border-border/50 px-4 py-2">
        <p className="text-xs text-muted">Bracket &amp; P&amp;L stay in view</p>
        <div className="flex gap-2">
          <Button variant="ghost" size="sm" className="min-h-11" isDisabled={!scrollState.left} onPress={() => scrollColumns(-1)}>← Previous columns</Button>
          <Button variant="ghost" size="sm" className="min-h-11" isDisabled={!scrollState.right} onPress={() => scrollColumns(1)}>Next columns →</Button>
        </div>
      </div>}
      <div role="region" aria-label="Closed-position table; use left and right arrow keys to scroll columns" tabIndex={0} className="focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[color:var(--focus)]" onKeyDown={(event) => {
        if (event.target === event.currentTarget && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
          event.preventDefault(); scrollColumns(event.key === "ArrowLeft" ? -1 : 1);
        }
      }}>
        <DataGrid
          aria-label={detailed ? "Full closed paper-trade ledger" : "Recent closed paper trades"}
          columns={visibleColumns}
          data={rows}
          getRowId={(d) => d.id}
          variant="secondary"
          contentClassName={showDetails ? "min-w-[76rem]" : "min-w-[52rem]"}
          scrollContainerClassName="ledger-scroll"
        />
      </div>
    </div>
  );
}
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Accordion } from "@heroui/react/accordion";
import { Button } from "@heroui/react/button";
