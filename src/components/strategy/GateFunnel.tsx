import { Card } from "@heroui/react/card";
import { Icon } from "@iconify/react/offline";
import { pct } from "../../lib/data";
import {
  deferralReason,
  gateCounts,
  gateDeferred,
  type ProfileGateStats,
  type StrategyLab,
} from "../../lib/strategy";

const CATEGORY_LABELS: Record<string, string> = {
  edge: "Edge & pricing gates",
  no_data: "Source disagreement / no data",
  other: "Other",
};

/** Shared by the measured funnel and its deferred stand-in so the section keeps
    one identity in both states. */
function FunnelHeader({ countsAsOf }: { countsAsOf?: string | null }) {
  return (
    <Card.Header>
      <Card.Title className="text-base">Signal filtering</Card.Title>
      <Card.Description className="text-sm text-muted">
        {countsAsOf
          ? `Cached gate counts as of ${countsAsOf}; they are not current-runtime totals`
          : "Every scheduled scan re-checks each published bracket and side against the full filter set"}
      </Card.Description>
    </Card.Header>
  );
}

/** The whole card is a count of gate evaluations, so when the artifact publishes
    that section unpopulated there is no funnel to draw — only the deferral to
    report. Rendering the stub's zeros would claim a 0.00% survival rate on the
    same page that shows 52 trades opened. */
function DeferredFunnel({ reason }: { reason: string }) {
  return (
    <Card className="w-full min-w-0 max-w-full rounded-2xl">
      <FunnelHeader />
      <Card.Content className="min-w-0 pt-0">
        <p role="status" className="flex gap-2 text-sm leading-relaxed text-muted">
          <Icon icon="solar:hourglass-line-bold" className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
          <span>Gate evaluation counts are not published in this artifact, so no approval or rejection rate is shown. {reason}</span>
        </p>
      </Card.Content>
    </Card>
  );
}

/** Why almost everything gets rejected: the window's gate evaluations, the
    approval sliver, and the top global rejection reasons. Per-book detail
    lives in the profile explorer. */
export function GateFunnel({ s }: { s: StrategyLab }) {
  const gate = s.daily_summary?.gate_behavior;
  const analytics = s.daily_summary?.decision_analytics;
  if (!gate) return null;
  const { approved, total } = gateCounts(gate);
  if (gateDeferred(gate) || total === 0) return <DeferredFunnel reason={deferralReason(gate)} />;
  const approvedPct = approved / total;
  const countsAsOf = analytics?.status === "cached"
    ? analytics.counts_stale_from ?? analytics.analysis_generated_at?.slice(0, 10) ?? "the last deploy-time analysis"
    : null;
  const cats = Object.entries(aggregateCategories(gate.by_profile ?? []));
  const rejections = (gate.top_rejections_all?.length ? gate.top_rejections_all : gate.top_rejections ?? []).slice(0, 8);
  const max = rejections[0]?.count ?? 1;
  const half = Math.ceil(rejections.length / 2);
  const columns = [rejections.slice(0, half), rejections.slice(half)];

  return (
    <Card className="w-full min-w-0 max-w-full rounded-2xl">
      <FunnelHeader countsAsOf={countsAsOf} />
      <Card.Content className="min-w-0 space-y-6 pt-0">
        <div>
          <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
            <p>
              <span className="tnum font-display text-2xl font-semibold">{total.toLocaleString()}</span>{" "}
              <span className="text-sm text-muted">gate evaluations this window</span>
            </p>
            <p>
              <span className="tnum font-display text-2xl font-semibold text-success">{approved.toLocaleString()}</span>{" "}
              <span className="text-sm text-muted">approved · {pct(approvedPct, 2)}</span>
            </p>
          </div>
          <div
            className="mt-2 flex h-2.5 overflow-hidden rounded-full bg-foreground/8"
            role="img"
            aria-label={`${approved.toLocaleString()} of ${total.toLocaleString()} gate evaluations approved (${pct(approvedPct, 2)}).`}
          >
            <div className="h-full rounded-full bg-success" style={{ width: `${Math.max(approvedPct * 100, 0.6)}%` }} />
          </div>
          {!!cats.length && (
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
              {cats.map(([k, v]) => (
                <span key={k}>
                  <span className="tnum font-medium text-foreground">{v.toLocaleString()}</span> {CATEGORY_LABELS[k] ?? k}
                </span>
              ))}
            </div>
          )}
        </div>

        {!!rejections.length && (
          <div className="grid min-w-0 grid-cols-1 gap-x-6 gap-y-3 border-t border-border/50 pt-6 md:grid-cols-2">
            {columns.map((col, ci) => (
              <ul key={ci} className="min-w-0 space-y-2">
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
