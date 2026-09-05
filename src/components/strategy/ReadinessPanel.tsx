import { Card } from "@heroui/react/card";
import { Chip } from "@heroui/react/chip";
import { Icon } from "@iconify/react/offline";
import {
  deferralReason,
  money,
  type ReadinessCheck,
  type RealMoneyReadiness,
  type StrategyLab,
} from "../../lib/strategy";
import { Stat } from "../ui/Stat";
import { DetailDisclosure } from "../ui/DetailDisclosure";

/** The bounded public refresh defers the per-check evidence but still publishes
    the runtime's own status. That answer is only meaningful if it is shown. */
const hasPublishedStatus = (r: RealMoneyReadiness) =>
  Boolean(r.status || r.verdict || r.status_reasons?.length);

const statusText = (r: RealMoneyReadiness) =>
  r.status === "ANALYSIS_STALE"
    ? "ANALYSIS NOT REFRESHED"
    : (r.status ?? r.verdict ?? "").replace(/_/g, " ").trim() || "STATUS UNPUBLISHED";

const riskLimit = (dollars?: number, fraction?: number) => {
  const amount = money(dollars, { sign: "negative-only" });
  if (fraction == null || !Number.isFinite(fraction)) return amount;
  return `${amount} · ${new Intl.NumberFormat("en-US", {
    style: "percent",
    maximumFractionDigits: 1,
  }).format(fraction)}`;
};

/** Degraded verdict: the published status carried unchanged, with the reasons
    the runtime gave, and no invented check count. */
function DeferredVerdict({ r, compact = false }: { r: RealMoneyReadiness; compact?: boolean }) {
  const status = statusText(r);
  const deployDeferred = r.status === "ANALYSIS_STALE";
  const ready = r.ready === true || /^READY$/i.test(status);
  const reasons = r.status_reasons?.length ? r.status_reasons : [deferralReason(r)];
  return (
    <Card className="h-full rounded-2xl">
      <Card.Header>
        <Card.Title className="text-base">{compact ? "Go-live readiness" : "Verdict"}</Card.Title>
        <Card.Description className="text-sm text-muted">
          {deployDeferred
            ? "Readiness analysis has not been refreshed since the last deploy-time analysis run"
            : "Published status only — the per-check evidence is deferred from this refresh"}
        </Card.Description>
      </Card.Header>
      <Card.Content className="space-y-4 pt-0">
        <div className="flex flex-wrap items-center gap-3">
          <span
            className={`font-display font-bold tracking-tight ${compact ? "text-2xl" : "text-3xl"} ${
              ready ? "text-success" : "text-danger"
            }`}
          >
            {status}
          </span>
          <Chip size="sm" variant="soft" color="warning">
            <Chip.Label>{deployDeferred ? "Deploy-time analysis" : "Checklist deferred"}</Chip.Label>
          </Chip>
        </div>
        <ul className="space-y-2">
          {reasons.map((reason) => (
            <li key={reason} className="flex gap-2 text-sm leading-relaxed text-muted">
              <Icon
                icon="solar:hourglass-line-bold"
                className="mt-0.5 size-4 shrink-0 text-warning"
                aria-hidden="true"
              />
              <span>{reason}</span>
            </li>
          ))}
        </ul>
        <p className="text-xs leading-relaxed text-muted">
          {deployDeferred
            ? "This does not mean the checks newly failed. No check count is shown because the recurring public refresh does not rerun the deploy-time readiness analysis."
            : "This is the status the runtime published, carried unchanged. No check count is shown because the checklist behind it is not part of this artifact."}
        </p>
      </Card.Content>
    </Card>
  );
}

function CheckRow({ c }: { c: ReadinessCheck }) {
  const progress = Math.max(0, Math.min(1, c.progress ?? 0));
  return (
    <li className="flex items-start gap-3 py-2">
      <Icon
        icon={c.passed ? "solar:check-circle-bold" : "solar:close-circle-bold"}
        className={`mt-0.5 size-4.5 shrink-0 ${c.passed ? "text-success" : "text-danger/70"}`}
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-3">
          <p className="text-sm font-medium text-foreground">{c.label}</p>
          <p className="shrink-0 font-mono text-[11px] text-muted">{c.passed ? "Pass" : "Fail"}</p>
        </div>
        <p className="mt-1 text-xs text-muted">{c.detail}</p>
        <div className="mt-1 h-1 overflow-hidden rounded-full bg-foreground/10" aria-hidden="true">
          <div
            className={`h-full rounded-full ${c.passed ? "bg-success" : "bg-danger/60"}`}
            style={{ width: `${Math.max(progress * 100, c.passed ? 100 : 2)}%` }}
          />
        </div>
      </div>
    </li>
  );
}

/** Compact go-live verdict for the book overview — the headline number, a
    progress bar, and the one-line summary. The full checklist is ReadinessPanel. */
export function ReadinessVerdict({ s }: { s: StrategyLab }) {
  if (s.accounting?.available === false) return null;
  const r = s.real_money_readiness;
  if (!r) return null;
  if (!r.available) return hasPublishedStatus(r) ? <DeferredVerdict r={r} compact /> : null;
  const checks = r.checks ?? [];
  const passed = r.checks_passed ?? checks.filter((c) => c.passed).length;
  const total = r.checks_total ?? checks.length;
  const ready = r.ready === true;
  const progress = r.readiness_pct != null ? r.readiness_pct : total ? (passed / total) * 100 : 0;

  return (
    <Card className="h-full rounded-2xl">
      <Card.Header className="flex flex-row items-center gap-2">
        <Icon icon="solar:shield-keyhole-bold" className="size-4 text-accent" aria-hidden="true" />
        <div>
          <Card.Title className="text-base">Go-live readiness</Card.Title>
          <Card.Description className="text-sm text-muted">Recomputed on every refresh · enforced in code</Card.Description>
        </div>
      </Card.Header>
      <Card.Content className="space-y-4 pt-0">
        <div className="flex items-center gap-3">
          <span className={`font-display text-2xl font-bold tracking-tight ${ready ? "text-success" : "text-danger"}`}>
            {r.verdict ?? (ready ? "READY" : "NOT READY")}
          </span>
          <Chip size="sm" variant="soft" color={ready ? "success" : "danger"}>
            <Chip.Label>
              <span className="tnum">{passed}/{total}</span> checks
            </Chip.Label>
          </Chip>
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-foreground/10" role="img" aria-label={`${passed} of ${total} go-live checks passed (${Math.round(progress)}%).`}>
          <div className={`h-full rounded-full ${ready ? "bg-success" : "bg-accent"}`} style={{ width: `${Math.max(progress, 2)}%` }} />
        </div>
        {r.summary && <p className="text-sm leading-relaxed text-muted">{r.summary}</p>}
      </Card.Content>
    </Card>
  );
}

/** The full go-live checks: the engine's own answer to "would you trade real
    money with this?" — enforced in code, published unedited. */
export function ReadinessPanel({ s }: { s: StrategyLab }) {
  if (s.accounting?.available === false) return null;
  const r = s.real_money_readiness;
  if (!r) return null;
  if (!r.available) return hasPublishedStatus(r) ? <DeferredVerdict r={r} /> : null;
  const checks = r.checks ?? [];
  const passed = r.checks_passed ?? checks.filter((c) => c.passed).length;
  const total = r.checks_total ?? checks.length;
  const ready = r.ready === true;
  const policy = r.live_policy;

  const verdictCard = (
    <Card className="h-full rounded-2xl">
        <Card.Header>
          <Card.Title className="text-base">Verdict</Card.Title>
          <Card.Description className="text-sm text-muted">Recomputed on every AWS refresh</Card.Description>
        </Card.Header>
        <Card.Content className="space-y-4 pt-0">
          <div className="flex items-center gap-3">
            <span className={`font-display text-3xl font-bold tracking-tight ${ready ? "text-success" : "text-danger"}`}>
              {r.verdict ?? (ready ? "READY" : "NOT READY")}
            </span>
            <Chip size="sm" variant="soft" color={ready ? "success" : "danger"}>
              <Chip.Label>
                {passed}/{total} checks
              </Chip.Label>
            </Chip>
          </div>
          {r.summary && <p className="text-sm leading-relaxed text-muted">{r.summary}</p>}
          <p className="text-xs leading-relaxed text-muted">
            Passing every check would only permit considering a future pilot; authenticated real-money execution is not implemented.
          </p>

          {policy && (
            <div>
              <p className="mb-2 text-xs font-medium text-muted">Standing pilot policy (if it ever goes live)</p>
              <div className="grid grid-cols-2 gap-4">
                <Stat label="Live orders" value={policy.enabled ? "Enabled" : "Disabled"} tone={policy.enabled ? "pos" : "default"} />
                <Stat label="Dry run" value={policy.dry_run ? "On" : "Off"} />
                {policy.risk_capital != null && (
                  <Stat label="Risk capital" value={money(policy.risk_capital, { sign: "negative-only" })} />
                )}
                <Stat label="Per-order risk" value={riskLimit(policy.per_trade_risk, policy.per_trade_risk_pct)} />
                <Stat label="Daily loss cap" value={riskLimit(policy.daily_loss, policy.daily_loss_pct)} />
              </div>
              {r.pilot_loss_remaining != null && policy.pilot_max_loss != null && (
                <p className="mt-2 text-xs text-muted">
                  Pilot kill-switch: {policy.pilot_max_loss_pct != null
                    ? `${new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 }).format(policy.pilot_max_loss_pct)} of the configured risk capital (${money(policy.pilot_max_loss, { sign: "negative-only" })})`
                    : `${money(policy.pilot_max_loss, { sign: "negative-only" })} of losses`} ({money(r.pilot_loss_remaining, { sign: "negative-only" })} remaining).
                </p>
              )}
            </div>
          )}
        </Card.Content>
    </Card>
  );

  const checklist = (
    <ul className="divide-y divide-border/50">
      {checks.map((c) => (
        <CheckRow key={c.name} c={c} />
      ))}
    </ul>
  );

  if (checks.length > 5) {
    return (
      <div className="space-y-6">
        {verdictCard}
        <DetailDisclosure
          id="go-live-checklist"
          icon="solar:clipboard-list-bold"
          title="Go-live checklist"
          note={`${passed}/${total} checks passed · ${checks.length} technical rows`}
        >
          {checklist}
        </DetailDisclosure>
      </div>
    );
  }

  const checklistCard = (
    <Card className="h-full rounded-2xl">
      <Card.Header>
        <Card.Title className="text-base">Go-live checklist</Card.Title>
        <Card.Description className="text-sm text-muted">
          All {total} must pass before a future pilot could be considered; authenticated execution is not implemented
        </Card.Description>
      </Card.Header>
      <Card.Content className="pt-0">{checklist}</Card.Content>
    </Card>
  );

  return <div className="grid gap-6 lg:grid-cols-[0.9fr_1.1fr]">{verdictCard}{checklistCard}</div>;
}
