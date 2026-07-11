import { Card } from "@heroui/react/card";
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

function PositionList({ rows, kind }: { rows: OpenPosition[]; kind: "open" | "pending" }) {
  if (!rows.length) {
    return (
      <div className="flex flex-col items-center gap-2 py-8 text-center">
        <Icon icon="solar:moon-sleep-bold" className="size-6 text-muted/70" aria-hidden="true" />
        <p className="max-w-sm text-sm text-muted">
          {kind === "open"
            ? "No open paper positions right now — everything has been closed or settled. New entries appear here the moment a scan clears the gates."
            : "No pending limit orders. The engine posts limits only when a gate-approved price isn't immediately fillable."}
        </p>
      </div>
    );
  }
  return (
    <ul className="divide-y divide-border/50">
      {rows.map((r) => {
        const city = cityForTicker(r.ticker ?? "");
        return (
        <li key={r.id} className="flex items-center justify-between gap-3 py-2.5">
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
              {kind === "open" ? money(r.unrealized_pnl) : r.risk != null ? `$${r.risk.toFixed(2)} risk` : "—"}
            </p>
            {kind === "open" && r.current_bid != null && (
              <p className="font-mono text-[11px] text-muted">bid {cents(r.current_bid)}</p>
            )}
          </div>
        </li>
        );
      })}
    </ul>
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

  return (
    <div className="space-y-5">
      {profile ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="Open positions" value={currentStateAvailable ? `${open.length}` : "Unavailable"} />
          <Stat label="Open risk" value={currentStateAvailable ? `$${sumRisk(open).toFixed(2)}` : "Unavailable"} />
          <Stat label="Pending limits" value={currentStateAvailable ? `${pending.length}` : "Unavailable"} />
          <Stat label="Pending risk" value={currentStateAvailable ? `$${sumRisk(pending).toFixed(2)}` : "Unavailable"} />
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <Stat label="Open positions" value={currentStateAvailable ? `${sum?.open_positions ?? open.length}` : "Unavailable"} />
          <Stat label="Open risk" value={currentStateAvailable ? (sum?.open_risk != null ? `$${sum.open_risk.toFixed(2)}` : "$0.00") : "Unavailable"} />
          <Stat label="Pending limits" value={currentStateAvailable ? `${sum?.pending_limit_orders ?? pending.length}` : "Unavailable"} />
          <Stat label="Pending risk" value={currentStateAvailable ? (sum?.pending_limit_risk != null ? `$${sum.pending_limit_risk.toFixed(2)}` : "$0.00") : "Unavailable"} />
          <Stat label="Capital at risk · window" value={currentStateAvailable ? (sum?.capital_at_risk != null ? `$${sum.capital_at_risk.toFixed(2)}` : "—") : "Unavailable"} />
          <Stat label="Last monitor action" value={relTime(sum?.latest_monitor_action_at)} />
        </div>
      )}

      {currentStateAvailable ? (
        <div className="grid gap-5 lg:grid-cols-2">
          <Card className="h-full rounded-2xl ring-1 ring-border/70">
            <Card.Header className="flex flex-row items-center gap-2">
              <Icon icon="solar:folder-open-bold" className="size-4 text-accent" aria-hidden="true" />
              <Card.Title className="text-base">Open positions</Card.Title>
            </Card.Header>
            <Card.Content className="pt-0">
              <PositionList rows={open} kind="open" />
            </Card.Content>
          </Card>
          <Card className="h-full rounded-2xl ring-1 ring-border/70">
            <Card.Header className="flex flex-row items-center gap-2">
              <Icon icon="solar:hourglass-line-bold" className="size-4 text-accent" aria-hidden="true" />
              <Card.Title className="text-base">Pending limit orders</Card.Title>
            </Card.Header>
            <Card.Content className="pt-0">
              <PositionList rows={pending} kind="pending" />
            </Card.Content>
          </Card>
        </div>
      ) : (
        <div role="status" className="rounded-2xl border border-dashed border-border/70 bg-surface-secondary/60 px-4 py-8 text-center">
          <Icon icon="solar:clock-circle-bold" className="mx-auto mb-2 size-5 text-warning" aria-hidden="true" />
          <p className="text-sm font-medium text-foreground">Current open and pending book state is unavailable.</p>
          <p className="mt-1 text-xs text-muted">Counts and position lists will return when Strategy Lab publication recovers.</p>
        </div>
      )}
    </div>
  );
}
