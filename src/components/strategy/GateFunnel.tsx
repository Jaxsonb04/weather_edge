import { Card } from "@heroui/react";
import { Icon } from "@iconify/react";
import { pct } from "../../lib/data";
import type { ProfileGateStats, StrategyLab } from "../../lib/strategy";

const CATEGORY_LABELS: Record<string, string> = {
  edge: "Edge & pricing gates",
  no_data: "Source disagreement / no data",
  other: "Other",
};

/** Why almost everything gets rejected: the window's gate evaluations, the
    approval sliver, and the top global rejection reasons. Per-book detail
    lives in the profile explorer. */
export function GateFunnel({ s }: { s: StrategyLab }) {
  const gate = s.daily_summary?.gate_behavior;
  if (!gate) return null;
  const total = gate.approved + gate.rejected;
  const approvedPct = total ? gate.approved / total : 0;
  const cats = Object.entries(aggregateCategories(gate.by_profile ?? []));
  const rejections = (gate.top_rejections_all?.length ? gate.top_rejections_all : gate.top_rejections ?? []).slice(0, 8);
  const max = rejections[0]?.count ?? 1;
  const half = Math.ceil(rejections.length / 2);
  const columns = [rejections.slice(0, half), rejections.slice(half)];

  return (
    <Card className="rounded-2xl ring-1 ring-border/70">
      <Card.Header className="flex flex-row items-center justify-between gap-3">
        <div>
          <Card.Title className="text-base">The approval gauntlet</Card.Title>
          <Card.Description className="text-sm text-muted">
            Every 15-minute scan re-evaluates every bracket and side against the full gate stack
          </Card.Description>
        </div>
        <Icon icon="solar:filter-bold" className="size-4 shrink-0 text-accent" aria-hidden="true" />
      </Card.Header>
      <Card.Content className="space-y-5 pt-0">
        <div>
          <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
            <p>
              <span className="tnum font-display text-2xl font-semibold">{total.toLocaleString()}</span>{" "}
              <span className="text-sm text-muted">gate evaluations this window</span>
            </p>
            <p>
              <span className="tnum font-display text-2xl font-semibold text-success">{gate.approved.toLocaleString()}</span>{" "}
              <span className="text-sm text-muted">approved · {pct(approvedPct, 2)}</span>
            </p>
          </div>
          <div
            className="mt-3 flex h-2.5 overflow-hidden rounded-full bg-foreground/8"
            role="img"
            aria-label={`${gate.approved.toLocaleString()} of ${total.toLocaleString()} gate evaluations approved (${pct(approvedPct, 2)}).`}
          >
            <div className="h-full rounded-full bg-success" style={{ width: `${Math.max(approvedPct * 100, 0.6)}%` }} />
          </div>
          {!!cats.length && (
            <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted">
              {cats.map(([k, v]) => (
                <span key={k}>
                  <span className="tnum font-medium text-foreground">{v.toLocaleString()}</span> {CATEGORY_LABELS[k] ?? k}
                </span>
              ))}
            </div>
          )}
        </div>

        {!!rejections.length && (
          <div className="grid gap-x-6 gap-y-2.5 border-t border-border/50 pt-5 md:grid-cols-2">
            {columns.map((col, ci) => (
              <ul key={ci} className="space-y-2.5">
                {col.map((r) => (
                  <li key={r.reason}>
                    <div className="mb-1 flex items-baseline justify-between gap-3">
                      <span className="min-w-0 truncate text-xs text-muted" title={r.reason}>
                        {r.reason}
                      </span>
                      <span className="tnum shrink-0 font-mono text-[11px] text-muted">{r.count.toLocaleString()}</span>
                    </div>
                    <div className="h-1.5 overflow-hidden rounded-full bg-foreground/8">
                      <div
                        className="h-full rounded-full bg-accent/80"
                        style={{ width: `${Math.max((r.count / max) * 100, 1.5)}%` }}
                      />
                    </div>
                  </li>
                ))}
              </ul>
            ))}
          </div>
        )}
      </Card.Content>
    </Card>
  );
}

function aggregateCategories(byProfile: ProfileGateStats[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const g of byProfile) {
    for (const [k, v] of Object.entries(g.rejection_categories ?? {})) {
      if (v > 0) out[k] = (out[k] ?? 0) + v;
    }
  }
  return out;
}
