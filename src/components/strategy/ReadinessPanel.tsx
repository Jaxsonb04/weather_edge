import { Chip } from "@heroui/react/chip";
import { Widget } from "@heroui-pro/react/widget";
import { Icon } from "@iconify/react/offline";
import {
  deferralReason,
  money,
  type ReadinessCheck,
  type RealMoneyReadiness,
  type StrategyLab,
} from "../../lib/strategy";
import { InfoTip } from "../ui/InfoTip";
import { Stat } from "../ui/Stat";
import { DetailDisclosure } from "../ui/DetailDisclosure";

/** The bounded public refresh defers the per-check evidence but still publishes
    the runtime's own status. That answer is only meaningful if it is shown. */
const hasPublishedStatus = (r: RealMoneyReadiness) =>
  Boolean(r.status || r.verdict || r.status_reasons?.length);

const statusText = (r: RealMoneyReadiness) => {
  if (r.status === "ANALYSIS_STALE") return "Analysis not refreshed";
  const raw = (r.status ?? r.verdict ?? "").replace(/_/g, " ").trim();
  if (!raw) return "Status unpublished";
  return raw === raw.toUpperCase() ? raw[0] + raw.slice(1).toLowerCase() : raw;
};

const riskLimit = (dollars?: number, fraction?: number) => {
  const amount = money(dollars, { sign: "negative-only" });
  if (fraction == null || !Number.isFinite(fraction)) return amount;
  return `${amount} · ${new Intl.NumberFormat("en-US", {
    style: "percent",
    maximumFractionDigits: 1,
  }).format(fraction)}`;
};

const ageLine = (r: RealMoneyReadiness) => {
  const hours = r.analysis_age_hours;
  if (typeof hours !== "number" || !Number.isFinite(hours)) return null;
  const days = Math.floor(hours / 24);
  const limit = r.max_analysis_age_hours;
  return `The deploy-time analysis is ${days >= 2 ? `${days} days` : `${Math.round(hours)} hours`} old${
    typeof limit === "number" ? `; the limit is ${limit} hours` : ""
  }.`;
};

/** Degraded verdict: the runtime's published status, unchanged, with its
    reasons and no invented check count. */
function DeferredVerdict({ r, live }: { r: RealMoneyReadiness; live: boolean | undefined }) {
  const status = statusText(r);
  const deployDeferred = r.status === "ANALYSIS_STALE";
  const ready = r.ready === true || /^ready$/i.test(status);
  const reasons = r.status_reasons?.length ? r.status_reasons : [deferralReason(r)];
  const age = ageLine(r);
  return (
    <Widget className="min-w-0">
      <Widget.Header>
        <div className="min-w-0">
          <div className="flex items-center gap-1">
            <Widget.Title>Real-money readiness</Widget.Title>
            <InfoTip label="the readiness status" title="Published as-is">
              {reasons.join(" ")} This page never overrides the runtime's verdict.
            </InfoTip>
          </div>
          <Widget.Description>
            {deployDeferred ? "Not re-run since the last deploy-time analysis" : "Status only; the checklist is deferred"}
          </Widget.Description>
        </div>
      </Widget.Header>
      <Widget.Content className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className={`text-2xl font-semibold tracking-tight ${ready ? "text-success" : "text-warning"}`}>{status}</span>
          <Chip size="sm" variant="soft" color={live ? "danger" : "default"}>
            <Chip.Label>
              {live === true ? "Live orders enabled" : live === false ? "Live orders disabled" : "Live-order flag not published"}
            </Chip.Label>
          </Chip>
        </div>
        {(age ?? !deployDeferred) && <p className="text-sm text-muted">{age ?? reasons[0]}</p>}
        <p className="text-xs text-muted">
          {deployDeferred
            ? "This does not mean the checks newly failed; the public refresh does not re-run them."
            : "No check count is shown because the checklist is not part of this artifact."}
        </p>
      </Widget.Content>
    </Widget>
  );
}

function CheckRow({ c }: { c: ReadinessCheck }) {
  const progress = Math.max(0, Math.min(1, c.progress ?? 0));
  return (
    <li className="flex items-start gap-3 py-2.5">
      <Icon
        icon={c.passed ? "solar:check-circle-linear" : "solar:close-circle-linear"}
        className={`mt-0.5 size-4 shrink-0 ${c.passed ? "text-success" : "text-danger"}`}
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-3">
          <p className="text-sm font-medium text-foreground">{c.label}</p>
          <p className="shrink-0 text-xs text-muted">{c.passed ? "Pass" : "Fail"}</p>
        </div>
        <p className="mt-0.5 text-xs text-muted">{c.detail}</p>
        <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-default" aria-hidden="true">
          <div
            className={`bar-grow h-full rounded-full ${c.passed ? "bg-success" : "bg-danger/70"}`}
            style={{ width: `${Math.max(progress * 100, c.passed ? 100 : 2)}%` }}
          />
        </div>
      </div>
    </li>
  );
}

/** The engine's own answer to "would you trade real money with this?",
    published unedited, with the standing pilot limits. */
export function ReadinessPanel({ s }: { s: StrategyLab }) {
  if (s.accounting?.available === false) return null;
  const r = s.real_money_readiness;
  if (!r) return null;
  if (!r.available) return hasPublishedStatus(r) ? <DeferredVerdict r={r} live={s.live_orders_enabled} /> : null;
  const checks = r.checks ?? [];
  const passed = r.checks_passed ?? checks.filter((c) => c.passed).length;
  const total = r.checks_total ?? checks.length;
  const ready = r.ready === true;
  const policy = r.live_policy;

  const verdictCard = (
    <Widget className="min-w-0">
      <Widget.Header>
        <div className="flex min-w-0 flex-col gap-0.5">
          <Widget.Title>Real-money readiness</Widget.Title>
          <Widget.Description>Recomputed by the runtime on every refresh</Widget.Description>
        </div>
      </Widget.Header>
      <Widget.Content className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className={`text-2xl font-semibold tracking-tight ${ready ? "text-success" : "text-danger"}`}>
            {r.verdict ?? (ready ? "READY" : "NOT READY")}
          </span>
          <Chip size="sm" variant="soft" color={ready ? "success" : "danger"}>
            <Chip.Label>
              <span className="tnum">{passed}/{total}</span> checks
            </Chip.Label>
          </Chip>
        </div>
        {r.summary && <p className="text-sm text-muted">{r.summary}</p>}
        <p className="text-xs text-muted">
          Passing every check would only permit considering a future pilot; authenticated real-money execution is not implemented.
        </p>
        {policy && (
          <div className="border-t border-separator pt-4">
            <p className="mb-3 text-xs text-muted">Standing pilot limits (if a pilot is ever approved)</p>
            <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3">
              <Stat label="Live orders" value={policy.enabled ? "Enabled" : "Disabled"} />
              <Stat label="Dry run" value={policy.dry_run ? "On" : "Off"} />
              {policy.risk_capital != null && (
                <Stat label="Risk capital" value={money(policy.risk_capital, { sign: "negative-only" })} />
              )}
              <Stat label="Per-order risk" value={riskLimit(policy.per_trade_risk, policy.per_trade_risk_pct)} />
              <Stat label="Daily loss cap" value={riskLimit(policy.daily_loss, policy.daily_loss_pct)} />
            </div>
            {r.pilot_loss_remaining != null && policy.pilot_max_loss != null && (
              <p className="mt-3 text-xs text-muted">
                Pilot kill-switch: {policy.pilot_max_loss_pct != null
                  ? `${new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 }).format(policy.pilot_max_loss_pct)} of the configured risk capital (${money(policy.pilot_max_loss, { sign: "negative-only" })})`
                  : `${money(policy.pilot_max_loss, { sign: "negative-only" })} of losses`} ({money(r.pilot_loss_remaining, { sign: "negative-only" })} remaining).
              </p>
            )}
          </div>
        )}
      </Widget.Content>
    </Widget>
  );

  const checklist = (
    <ul className="flex flex-col divide-y divide-separator">
      {checks.map((c) => (
        <CheckRow key={c.name} c={c} />
      ))}
    </ul>
  );

  if (checks.length > 5) {
    return (
      <div className="flex flex-col gap-4">
        {verdictCard}
        <DetailDisclosure
          id="go-live-checklist"
          icon="solar:clipboard-list-linear"
          title="Go-live checklist"
          note={`${passed}/${total} checks passed`}
        >
          {checklist}
        </DetailDisclosure>
      </div>
    );
  }

  return (
    <div className="grid min-w-0 gap-4 lg:grid-cols-[0.9fr_1.1fr]">
      {verdictCard}
      <Widget className="min-w-0">
        <Widget.Header>
          <div className="flex min-w-0 flex-col gap-0.5">
            <Widget.Title>Go-live checklist</Widget.Title>
            <Widget.Description>All {total} must pass before a pilot could be considered</Widget.Description>
          </div>
        </Widget.Header>
        <Widget.Content>{checklist}</Widget.Content>
      </Widget>
    </div>
  );
}
