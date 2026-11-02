import { Widget } from "@heroui-pro/react/widget";
import { Icon } from "@iconify/react/offline";
import { pct } from "../../lib/data";
import {
  decisionCountProvenance,
  deferralReason,
  gateCounts,
  gateDeferred,
  profileDisplayLabel,
  researchDailyTarget,
  type StrategyLab,
} from "../../lib/strategy";
import { InfoTip } from "../ui/InfoTip";
import { rejectionLabel } from "../../lib/lab-format";

/** Shared by the measured funnel and its deferred stand-in so the section keeps
    one identity in both states. */
function FunnelHeader({ countsAsOf }: { countsAsOf?: string | null }) {
  return (
    <Widget.Header>
      <div className="min-w-0">
        <div className="flex items-center gap-1">
          <Widget.Title>Gate filter</Widget.Title>
          <InfoTip label="the gate filter">
            Every scheduled scan re-checks each bracket and side against the full filter set. Only candidates that
            clear every gate can become paper orders.
          </InfoTip>
        </div>
        <Widget.Description>
          {countsAsOf
            ? `Cached gate counts as of ${countsAsOf}; not current-runtime totals`
            : "Gate evaluations in this window"}
        </Widget.Description>
      </div>
    </Widget.Header>
  );
}

/** When the artifact publishes the gate section unpopulated there is no funnel
    to draw, only the deferral to report. Rendering the stub's zeros would claim
    a 0.00% survival rate on the same page that shows trades opening. */
function DeferredFunnel({ reason }: { reason: string }) {
  return (
    <Widget className="w-full min-w-0 max-w-full">
      <FunnelHeader />
      <Widget.Content>
        <p role="status" className="flex gap-2 text-sm leading-relaxed text-muted">
          <Icon icon="solar:hourglass-line-linear" className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
          <span>Gate counts are not published in this artifact, so no approval rate is shown. {reason}</span>
        </p>
      </Widget.Content>
    </Widget>
  );
}

/** How selective each account is: approvals per account, then the most common
    reasons a candidate is passed over. */
export function GateFunnel({ s }: { s: StrategyLab }) {
  const gate = s.daily_summary?.gate_behavior;
  const analytics = s.daily_summary?.decision_analytics;
  if (!gate) return null;
  const { approved, total } = gateCounts(gate);
  if (gateDeferred(gate, analytics) || total === 0) return <DeferredFunnel reason={deferralReason(gate)} />;
  const provenance = decisionCountProvenance(s);
  const countsAsOf = provenance.cached ? provenance.asOf : null;
  const books = (gate.by_profile ?? []).filter((book) => (book.signals ?? 0) > 0);
  const rejections = (gate.top_rejections_all?.length ? gate.top_rejections_all : gate.top_rejections ?? []).slice(0, 8);
  const max = rejections[0]?.count ?? 1;
  const profiles = new Map((s.profiles ?? []).map((profile) => [profile.risk_profile, profile] as const));

  return (
    <Widget className="w-full min-w-0 max-w-full">
      <FunnelHeader countsAsOf={countsAsOf} />
      <Widget.Content className="flex min-w-0 flex-col gap-6">
        <div>
          <p className="text-sm text-muted">
            <span className="tnum text-2xl font-semibold tracking-tight text-foreground">{approved.toLocaleString()}</span>{" "}
            approved of <span className="tnum font-medium text-foreground">{total.toLocaleString()}</span>{" "}
            {countsAsOf ? `gate evaluations in the window ending ${countsAsOf}` : "gate evaluations this window"}
          </p>
          {books.length > 0 && (
            <ul className="mt-4 flex flex-col gap-3" aria-label="Approvals by account">
              {books.map((book) => {
                const signals = book.signals ?? 0;
                const ok = book.approved ?? 0;
                const share = signals ? ok / signals : 0;
                const profile = profiles.get(book.risk_profile);
                return (
                  <li key={book.risk_profile}>
                    <div className="mb-1 flex items-baseline justify-between gap-3 text-xs">
                      <span className="font-medium text-foreground">
                        {profile ? profileDisplayLabel(profile, researchDailyTarget(s, profile)?.policy_version) : book.risk_profile}
                      </span>
                      <span className="tnum text-muted">
                        {ok.toLocaleString()} of {signals.toLocaleString()} · {pct(share, 2)}
                      </span>
                    </div>
                    <div className="h-1.5 overflow-hidden rounded-full bg-default">
                      <div className="h-full rounded-full bg-accent" style={{ width: `${Math.max(share * 100, 0.6)}%` }} />
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {!!rejections.length && (
          <div className="border-t border-separator pt-5">
            <p className="mb-3 text-xs text-muted">Most common reasons to pass · a candidate can fail several gates</p>
            <ul className="grid min-w-0 gap-x-8 gap-y-2.5 md:grid-cols-2">
              {rejections.map((r) => (
                <li key={r.reason} className="min-w-0">
                  <div className="mb-1 flex items-baseline justify-between gap-3">
                    <span className="min-w-0 truncate text-xs text-foreground" title={r.reason}>
                      {rejectionLabel(r.reason)}
                    </span>
                    <span className="tnum shrink-0 text-xs text-muted">{r.count.toLocaleString()}</span>
                  </div>
                  <div className="h-1 overflow-hidden rounded-full bg-default">
                    <div className="h-full rounded-full bg-accent/70" style={{ width: `${Math.max((r.count / max) * 100, 1.5)}%` }} />
                  </div>
                </li>
              ))}
            </ul>
          </div>
        )}
      </Widget.Content>
    </Widget>
  );
}
