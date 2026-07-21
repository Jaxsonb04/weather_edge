import { useState } from "react";
import { Icon } from "@iconify/react/offline";
import { cityForTicker } from "../../lib/data";
import { cents, money, openForProfile, pendingForProfile, type OpenPosition, type StrategyLab } from "../../lib/strategy";
import { usePublication } from "../../lib/publication";
import { Stat } from "../ui/Stat";

const sumRisk = (rows: OpenPosition[]) => rows.reduce((acc, r) => acc + (r.risk ?? 0), 0);

function relTime(iso?: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const hrs = Math.max(0, (Date.now() - then) / 3600000);
  if (hrs < 1) return `${Math.round(hrs * 60)}m ago`;
  if (hrs < 48) return `${Math.round(hrs)}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

function PositionList({ rows, kind, scope }: { rows: OpenPosition[]; kind: "open" | "pending"; scope: string }) {
  const [expanded, setExpanded] = useState(false);
  const listId = `${kind}-list-${scope}`;
  if (!rows.length) {
    return (
      <div className="flex flex-col items-center gap-2 py-6 text-center">
        <Icon icon="solar:moon-sleep-bold" className="size-6 text-muted/70" aria-hidden="true" />
        <p className="max-w-sm text-sm text-muted">
          {kind === "open"
            ? "No open paper positions right now — everything has been closed or settled. New entries appear here the moment a scan clears the gates."
            : "No pending limit orders. The engine posts limits only when a gate-approved price isn't immediately fillable."}
        </p>
      </div>
    );
  }
  const overflow = rows.slice(5);
  const shown = expanded ? rows : rows.slice(0, 5);
  const noun = kind === "open" ? "open positions" : "pending limits";
  return (
    <>
      <ul id={listId} className="divide-y divide-border/50">
        <PositionRows rows={shown} kind={kind} />
      </ul>
      {overflow.length > 0 && (
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={listId}
          onClick={() => setExpanded((v) => !v)}
          className="flex min-h-11 w-full items-center justify-center gap-1.5 border-t border-border/50 text-xs font-medium text-muted transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[color:var(--focus)] motion-reduce:transition-none"
        >
          {expanded ? `Show fewer ${noun}` : `Show ${overflow.length} more ${noun}`}
          <Icon
            icon="solar:alt-arrow-down-linear"
            className={`size-3.5 transition-transform ${expanded ? "rotate-180" : ""} motion-reduce:transition-none`}
            aria-hidden="true"
          />
        </button>
      )}
    </>
  );
}

function PositionRows({ rows, kind }: { rows: OpenPosition[]; kind: "open" | "pending" }) {
  return (
    <>
      {rows.map((r) => {
        const city = cityForTicker(r.ticker ?? "");
        return (
        <li key={r.id} className="flex items-center justify-between gap-3 py-2">
          <div className="min-w-0">
            <p className="flex items-center gap-2 text-sm font-medium text-foreground">
              <span className="truncate">{r.label ?? r.ticker ?? `#${r.id}`}</span>
              {city && (
                <span
                  title={city.name}
                  className="rounded bg-foreground/8 px-1.5 py-0.5 font-mono text-[10px] font-medium uppercase text-muted"
                >
                  {city.slug}
                </span>
              )}
              {r.side && (
                <span className="rounded bg-foreground/8 px-1.5 py-0.5 font-mono text-[10px] font-medium uppercase text-muted">
                  {r.side}
                </span>
              )}
              {r.risk_profile && (
                <span className={`rounded px-1.5 py-0.5 font-mono text-[10px] font-medium uppercase ${r.risk_profile === "live" ? "bg-accent-soft text-[color:var(--accent-text)]" : "bg-foreground/8 text-muted"}`}>
                  {r.risk_profile}
                </span>
              )}
            </p>
            <p className="font-mono text-[11px] text-muted">
              {r.target_date?.slice(5) ?? "—"} · {r.contracts ?? "—"} @ {cents(kind === "open" ? r.entry_price : r.limit_price)}
            </p>
          </div>
          <div className="shrink-0 text-right">
            <p className={`tnum text-sm font-semibold ${(r.unrealized_pnl ?? 0) > 0 ? "text-success" : (r.unrealized_pnl ?? 0) < 0 ? "text-danger" : "text-foreground"}`}>
              {kind === "open" ? money(r.unrealized_pnl) : r.risk != null ? `${money(r.risk, { sign: "negative-only" })} risk` : "—"}
            </p>
            {kind === "open" && r.current_bid != null && (
              <p className="font-mono text-[11px] text-muted">bid {cents(r.current_bid)}</p>
            )}
          </div>
        </li>
        );
      })}
    </>
  );
}

/** The book as it stands this refresh: exposure KPIs plus open and pending
    orders (honest empty states included — flat is a position too). Pass
    `profile` to scope everything to one book; exposure is recomputed from the
    filtered rows. */
export function OpenBook({ s, profile }: { s: StrategyLab; profile?: string }) {
  const { strategy } = usePublication();
  const currentStateAvailable = strategy.state === "fresh";
  const sum = s.paper_trading?.summary;
  const open = profile ? openForProfile(s, profile) : s.paper_trading?.open_positions ?? [];
  const pending = profile ? pendingForProfile(s, profile) : s.paper_trading?.pending_limit_orders ?? [];
  const scope = profile ?? "all";

  return (
    <div className="space-y-6">
      {profile ? (
        <div className="grid grid-cols-2 gap-x-6 gap-y-4 rounded-xl bg-surface-secondary px-4 py-3 sm:grid-cols-4">
          <Stat label="Open positions" value={currentStateAvailable ? `${open.length}` : "Unavailable"} />
          <Stat label="Open risk" value={currentStateAvailable ? money(sumRisk(open), { sign: "negative-only" }) : "Unavailable"} />
          <Stat label="Pending limits" value={currentStateAvailable ? `${pending.length}` : "Unavailable"} />
          <Stat label="Pending risk" value={currentStateAvailable ? money(sumRisk(pending), { sign: "negative-only" }) : "Unavailable"} />
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-x-6 gap-y-4 rounded-xl bg-surface-secondary px-4 py-3 sm:grid-cols-3 lg:grid-cols-6">
          <Stat label="Open positions" value={currentStateAvailable ? `${sum?.open_positions ?? open.length}` : "Unavailable"} />
          <Stat label="Open risk" value={currentStateAvailable ? money(sum?.open_risk ?? 0, { sign: "negative-only" }) : "Unavailable"} />
          <Stat label="Pending limits" value={currentStateAvailable ? `${sum?.pending_limit_orders ?? pending.length}` : "Unavailable"} />
          <Stat label="Pending risk" value={currentStateAvailable ? money(sum?.pending_limit_risk ?? 0, { sign: "negative-only" }) : "Unavailable"} />
          <Stat label="Capital at risk · window" value={currentStateAvailable ? money(sum?.capital_at_risk, { sign: "negative-only" }) : "Unavailable"} />
          <Stat label="Last monitor action" value={relTime(sum?.latest_monitor_action_at)} />
        </div>
      )}

      {currentStateAvailable ? (
        <div className="grid gap-x-8 gap-y-6 lg:grid-cols-2 lg:divide-x lg:divide-border/50">
          <section aria-labelledby={`open-${scope}`} className="min-w-0">
            <h5 id={`open-${scope}`} className="mb-2 font-display text-sm font-semibold text-foreground">
              Open positions
            </h5>
            <PositionList rows={open} kind="open" scope={scope} />
          </section>
          <section aria-labelledby={`pending-${scope}`} className="min-w-0 lg:pl-8">
            <h5 id={`pending-${scope}`} className="mb-2 font-display text-sm font-semibold text-foreground">
              Pending limit orders
            </h5>
            <PositionList rows={pending} kind="pending" scope={scope} />
          </section>
        </div>
      ) : (
        <div role="status" className="rounded-2xl border border-dashed border-border/70 bg-surface-secondary/60 px-4 py-6 text-center">
          <Icon icon="solar:clock-circle-bold" className="mx-auto mb-2 size-5 text-warning" aria-hidden="true" />
          <p className="text-sm font-medium text-foreground">Current open and pending book state is unavailable.</p>
          <p className="mt-1 text-xs text-muted">Counts and position lists will return when Strategy Lab publication recovers.</p>
        </div>
      )}
    </div>
  );
}
