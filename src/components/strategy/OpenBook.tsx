import { useState } from "react";
import { Card } from "@heroui/react/card";
import { Chip } from "@heroui/react/chip";
import { Icon } from "@iconify/react/offline";
import { cityForTicker } from "../../lib/data";
import { shortDay } from "../../lib/incidents";
import { cents, findProfile, money, openForProfile, pendingForProfile, type OpenPosition, type StrategyLab } from "../../lib/strategy";
import { usePublication } from "../../lib/publication";
import { Stat } from "../ui/Stat";

/** A position whose market date is at least two days gone should have settled
    already; its bid is a last mark, not a live price. One day is normal: the
    official report lands the next morning. */
function pastTarget(targetDate: string | undefined, now: number): boolean {
  if (!targetDate) return false;
  const target = Date.parse(`${targetDate.slice(0, 10)}T00:00:00Z`);
  return !Number.isNaN(target) && now - target >= 2 * 86_400_000;
}

function relTime(iso?: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const hrs = Math.max(0, (Date.now() - then) / 3600000);
  if (hrs < 1) return `${Math.round(hrs * 60)}m ago`;
  if (hrs < 48) return `${Math.round(hrs)}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

function PositionList({ rows, total, kind, scope }: { rows: OpenPosition[]; total?: number; kind: "open" | "pending"; scope: string }) {
  const [expanded, setExpanded] = useState(false);
  const listId = `${kind}-list-${scope}`;
  const noun = kind === "open" ? "open positions" : "pending limits";
  const omitted = total != null && total > rows.length;
  const notice = omitted ? (
    <p role="status" className="py-3 text-xs text-muted">
      Showing {rows.length} of {total} {noun}. Only the latest rows across paper accounts are published.
    </p>
  ) : null;
  if (!rows.length) {
    if (omitted) return notice;
    if (total == null) return <p role="status" className="py-3 text-xs text-muted">No {noun} rows published.</p>;
    if (kind === "pending") {
      return (
        <div role="status" className="flex items-center gap-2 py-3 text-left">
          <Icon icon="solar:moon-sleep-linear" className="size-4 shrink-0 text-muted" aria-hidden="true" />
          <p className="text-xs text-muted">No pending limit orders.</p>
        </div>
      );
    }
    return (
      <div className="flex flex-col items-center gap-2 py-6 text-center">
        <Icon icon="solar:moon-sleep-linear" className="size-6 text-muted" aria-hidden="true" />
        <p className="max-w-sm text-sm text-muted">No open paper positions. Everything has closed or settled.</p>
      </div>
    );
  }
  const overflow = rows.slice(5);
  const shown = expanded ? rows : rows.slice(0, 5);
  return (
    <>
      {notice}
      <ul id={listId} className="divide-y divide-separator">
        <PositionRows rows={shown} kind={kind} />
      </ul>
      {overflow.length > 0 && (
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={listId}
          onClick={() => setExpanded((v) => !v)}
          className="flex min-h-11 w-full items-center justify-center gap-1.5 border-t border-separator text-xs font-medium text-muted transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[color:var(--focus)] motion-reduce:transition-none"
        >
          {expanded ? `Show fewer ${noun}` : `Show ${overflow.length} more ${noun}`}
          <Icon
            icon="solar:alt-arrow-down-linear"
            className={`size-3.5 transition-transform duration-200 ${expanded ? "rotate-180" : ""} motion-reduce:transition-none`}
            aria-hidden="true"
          />
        </button>
      )}
    </>
  );
}

function PositionRows({ rows, kind }: { rows: OpenPosition[]; kind: "open" | "pending" }) {
  const now = Date.now();
  return (
    <>
      {rows.map((r) => {
        const city = cityForTicker(r.ticker ?? "");
        const late = kind === "open" && pastTarget(r.target_date, now);
        const pnl = r.unrealized_pnl ?? 0;
        return (
          <li key={r.id} className="flex items-center justify-between gap-3 py-2.5">
            <div className="min-w-0">
              <p className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-sm font-medium text-foreground">
                <span className="truncate">{r.label ?? r.ticker ?? `#${r.id}`}</span>
                {r.side && <span className="text-xs font-semibold text-muted">{r.side}</span>}
                {late && (
                  <Chip size="sm" variant="soft" color="warning">
                    <Chip.Label>Awaiting settlement</Chip.Label>
                  </Chip>
                )}
              </p>
              <p className="tnum text-xs text-muted">
                {city?.name ?? "Unknown city"} · {r.target_date ? shortDay(r.target_date) : "—"} · {r.contracts ?? "—"} @{" "}
                {cents(kind === "open" ? r.entry_price : r.limit_price)}
              </p>
            </div>
            <div className="shrink-0 text-right">
              <p
                className={`tnum text-sm font-semibold ${
                  late ? "text-muted" : pnl > 0 ? "text-success" : pnl < 0 ? "text-danger" : "text-foreground"
                }`}
              >
                {kind === "open" ? money(r.unrealized_pnl) : r.risk != null ? `${money(r.risk, { sign: "negative-only" })} risk` : "—"}
              </p>
              {kind === "open" && r.current_bid != null && (
                <p className="tnum text-xs text-muted">{late ? "last mark" : "bid"} {cents(r.current_bid)}</p>
              )}
            </div>
          </li>
        );
      })}
    </>
  );
}

/** The account's book as of this publication: exposure figures, then open and
    pending orders (an empty book is a real state and says so). With `profile`,
    everything is scoped to one account; totals include rows omitted by the publisher. */
export function OpenBook({ s, profile }: { s: StrategyLab; profile?: string }) {
  const { strategy } = usePublication();
  const currentStateAvailable = strategy.state === "fresh";
  const sum = s.paper_trading?.summary;
  const profileSummary = profile ? findProfile(s, profile)?.paper_trading?.summary : undefined;
  const open = profile ? openForProfile(s, profile) : s.paper_trading?.open_positions ?? [];
  const pending = profile ? pendingForProfile(s, profile) : s.paper_trading?.pending_limit_orders ?? [];
  const scope = profile ?? "all";
  const unavailable = "Unavailable";

  return (
    <div className="flex min-w-0 flex-col gap-4">
      {profile ? (
        <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
          <Stat label="Open positions" value={currentStateAvailable && profileSummary?.open_positions != null ? `${profileSummary.open_positions}` : unavailable} />
          <Stat label="Open risk" value={currentStateAvailable ? money(profileSummary?.open_risk, { sign: "negative-only" }) : unavailable} />
          <Stat label="Pending limits" value={currentStateAvailable && profileSummary?.pending_limit_orders != null ? `${profileSummary.pending_limit_orders}` : unavailable} />
          <Stat label="Pending risk" value={currentStateAvailable ? money(profileSummary?.pending_limit_risk, { sign: "negative-only" }) : unavailable} />
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3 lg:grid-cols-6">
          <Stat label="Open positions" value={currentStateAvailable ? `${sum?.open_positions ?? open.length}` : unavailable} />
          <Stat label="Open risk" value={currentStateAvailable ? money(sum?.open_risk ?? 0, { sign: "negative-only" }) : unavailable} />
          <Stat label="Pending limits" value={currentStateAvailable ? `${sum?.pending_limit_orders ?? pending.length}` : unavailable} />
          <Stat label="Pending risk" value={currentStateAvailable ? money(sum?.pending_limit_risk ?? 0, { sign: "negative-only" }) : unavailable} />
          <Stat label="Capital at risk · window" value={currentStateAvailable ? money(sum?.capital_at_risk, { sign: "negative-only" }) : unavailable} />
          <Stat label="Last monitor action" value={relTime(sum?.latest_monitor_action_at)} />
        </div>
      )}

      {currentStateAvailable ? (
        <Card className="rounded-3xl">
          <Card.Content>
            <div className="grid gap-y-6 lg:grid-cols-2 lg:divide-x lg:divide-separator">
              <section aria-labelledby={`open-${scope}`} className="min-w-0 lg:pr-8">
                <h5 id={`open-${scope}`} className="mb-1 text-sm font-semibold text-foreground">
                  Open positions
                </h5>
                <PositionList rows={open} total={profile ? profileSummary?.open_positions : sum?.open_positions} kind="open" scope={scope} />
              </section>
              <section aria-labelledby={`pending-${scope}`} className="min-w-0 lg:pl-8">
                <h5 id={`pending-${scope}`} className="mb-1 text-sm font-semibold text-foreground">
                  Pending limit orders
                </h5>
                <PositionList rows={pending} total={profile ? profileSummary?.pending_limit_orders : sum?.pending_limit_orders} kind="pending" scope={scope} />
              </section>
            </div>
          </Card.Content>
        </Card>
      ) : (
        <div role="status" className="rounded-3xl bg-surface-secondary px-4 py-6 text-center">
          <Icon icon="solar:clock-circle-linear" className="mx-auto mb-2 size-5 text-warning" aria-hidden="true" />
          <p className="text-sm font-medium text-foreground">Current open and pending book state is unavailable.</p>
          <p className="mt-1 text-xs text-muted">It returns with the next current Strategy Lab publication.</p>
        </div>
      )}
    </div>
  );
}
