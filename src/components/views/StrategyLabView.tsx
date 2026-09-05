import { Icon } from "@iconify/react/offline";
import "../../styles/pro-strategy.css";
import { Accordion } from "@heroui/react/accordion";
import { Chip } from "@heroui/react/chip";
import { pct } from "../../lib/data";
import {
  activeProfiles,
  archivedProfiles,
  deferralReason,
  findProfile,
  gateCounts,
  gateDeferred,
  money,
  openForProfile,
  pendingForProfile,
  profileDisplayLabel,
  researchDailyTarget,
  useStrategyLab,
  type ProfileEntry,
  type ProfilePaperSummary,
  type StrategyLab,
} from "../../lib/strategy";
import { PageHeader } from "../ui/PageHeader";
import { SectionHeading } from "../ui/SectionHeading";
import { Reveal } from "../ui/Reveal";
import { Finding } from "../ui/Finding";
import { EquityCurve } from "../strategy/EquityCurve";
import { ReadinessPanel } from "../strategy/ReadinessPanel";
import { ProfileExplorer } from "../strategy/ProfileExplorer";
import { GateFunnel } from "../strategy/GateFunnel";
import { MoversCard } from "../strategy/MoversCard";
import { CalibrationCompare } from "../strategy/CalibrationCompare";
import { OpsHealth } from "../strategy/OpsHealth";
import { ExitPolicyCard } from "../strategy/ExitPolicyCard";
import { BacktestStats } from "../strategy/BacktestStats";
import { ResearchNotes } from "../strategy/ResearchNotes";
import { DailyActivity } from "../strategy/DailyActivity";
import { StrategyPublicationNotice } from "../strategy/StrategyPublicationNotice";
import { ArchivedPerformance } from "../strategy/ArchivedPerformance";

export function TrackRecordFinding({ s }: { s: StrategyLab }) {
  const t = s.daily_summary?.totals;
  const side = s.daily_summary?.side_performance;
  if (!t) return null;
  const no = side?.NO;
  const yes = side?.YES;
  const hasObservedSideSplit = (no?.trades ?? 0) > 0 && (yes?.trades ?? 0) > 0;
  return (
    <Finding>
      Across archive-inclusive, economically separate published profile attribution in the {s.daily_summary.window_days ?? "recent"}-day window, the journal recorded{" "}
      <strong>{money(t.realized_pnl)}</strong> ({t.roi != null ? pct(t.roi, 1) : "—"} ROI on resolved capital) at a{" "}
      <strong>{pct(t.hit_rate, 0)} hit rate</strong>.
      {no && yes && hasObservedSideSplit && (
        <>
          {" "}By side, NO positions netted <strong>{money(no.realized_pnl)}</strong> across {no.trades} trades,
          while {yes.trades} YES trade{yes.trades === 1 ? "" : "s"} returned <strong>{money(yes.realized_pnl)}</strong>.
        </>
      )}
      {" "}That cross-profile total is strategy attribution, not one account&apos;s balance.
    </Finding>
  );
}

const SELECTIVITY_DESIGN_NOTE =
  "Live Stability keeps those gates binding; Research ROI takes more bounded paper risk without contributing to real-money readiness.";

function SelectivityFinding({ s }: { s: StrategyLab }) {
  const gate = s.daily_summary?.gate_behavior;
  if (!gate) return null;
  const { approved, total } = gateCounts(gate);
  // The bounded public refresh publishes the gate section unpopulated. Its zeros
  // mean "not evaluated in this artifact", not "nothing survived", so the
  // deferral is stated in place of any survival rate.
  if (gateDeferred(gate) || total === 0) {
    return (
      <Finding label="Deferred" icon="solar:hourglass-line-bold">
        Gate evaluation counts for this window are not published in this artifact, so no approval or rejection rate
        is claimed here. {deferralReason(gate)} {SELECTIVITY_DESIGN_NOTE}
      </Finding>
    );
  }
  const live = gate.by_profile?.find((g) => g.risk_profile === "live");
  const liveSignals = live?.signals ?? 0;
  const liveTop = live?.top_rejections?.[0];
  return (
    <Finding>
      Of <strong>{total.toLocaleString()}</strong> gate evaluations this window only{" "}
      <strong>{approved.toLocaleString()}</strong> ({pct(approved / total, 2)}) survived.
      {liveSignals > 0 && (
        <>
          {" "}The live book approved <strong>{(live?.approved ?? 0).toLocaleString()}</strong> of{" "}
          {liveSignals.toLocaleString()}
          {liveTop && (
            <>
              {" "}
              — its most common published rejection is <strong>{liveTop.reason}</strong> ({liveTop.count.toLocaleString()} rejections)
            </>
          )}
          .
        </>
      )}
      {" "}{SELECTIVITY_DESIGN_NOTE}
    </Finding>
  );
}

export function ReadinessFinding({ s }: { s: StrategyLab }) {
  if (s.accounting?.available === false) return null;
  const r = s.real_money_readiness;
  // This finding exists only to report the check tally. When the checklist is
  // deferred there is no tally to report, and inventing one would be the defect
  // this page is trying to avoid — ReadinessPanel below carries the runtime's
  // published status instead.
  if (!r?.available) return null;
  const total = r.checks_total ?? r.checks?.length ?? 0;
  return (
    <Finding>
      In this publication, the engine reports <strong>{r.checks_passed ?? 0}/{total} checks passed</strong> —{" "}
      {(r.verdict ?? "not ready").toLowerCase()} for real money. The checklist is recomputed from runtime evidence and
      published as-is; this page does not override the verdict.
    </Finding>
  );
}

function HeroStat({ label, value, tone }: { label: string; value: string; tone?: "pos" | "neg" }) {
  const toneClass = tone === "pos" ? "text-success" : tone === "neg" ? "text-danger" : "text-foreground";
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-[11px] uppercase tracking-wide text-muted">{label}</dt>
      <dd className={`tnum font-display text-lg font-semibold ${toneClass}`}>{value}</dd>
    </div>
  );
}

/** One book's current entry state: paused (daily-loss breaker) vs scanning, plus
    its live open/resting counts — read straight from the published feed. */
function BookState({ s, rp, label }: { s: StrategyLab; rp: string; label: string }) {
  const p = findProfile(s, rp);
  const reason = p?.status?.entry_scanner_reason ?? "";
  const paused = /pause/i.test(reason);
  const sum = p?.paper_trading?.summary;
  const open = sum?.open_positions ?? openForProfile(s, rp).length;
  const pending = sum?.pending_limit_orders ?? pendingForProfile(s, rp).length;
  return (
    <div className="min-w-0">
      <dt className="flex items-center gap-1.5">
        <span
          className={`size-1.5 shrink-0 rounded-full ${paused ? "bg-warning" : "bg-success"}`}
          aria-hidden="true"
        />
        <span className="min-w-0 truncate text-xs font-semibold text-foreground" title={label}>
          {label}
        </span>
        <span className={`shrink-0 text-[11px] ${paused ? "text-warning" : "text-muted"}`}>
          {paused ? "Entry paused" : "Scanning"}
        </span>
      </dt>
      <dd className="tnum mt-1 text-[11px] text-muted">
        {open} open · {pending} resting
      </dd>
      {paused && reason && <dd className="mt-1 text-[11px] leading-relaxed text-warning/90">{reason}</dd>}
    </div>
  );
}

export function AnalysisFreshness({ s }: { s: StrategyLab }) {
  if (!s.publication_mode) return null;
  const stamp = s.analysis_generated_at
    ? `${s.analysis_generated_at.slice(0, 16).replace("T", " ")} UTC`
    : null;
  const analysisStatus =
    s.publication_mode === "full_research"
      ? `Historical rescore completed ${stamp ?? "on this refresh"}.`
      : stamp
        ? `Historical rescore cached from ${stamp}.`
        : "Historical rescore deferred until the deploy-time analysis job completes.";

  return (
    <p className="mt-4 text-[11px] leading-relaxed text-muted">
      {analysisStatus} Current paper state is refreshed on every publication; readiness evidence is refreshed only by the deploy-time analysis job.
    </p>
  );
}

/** Live trading-status strip: a real-time snapshot of the running engine — the
    heartbeat, each book's entry state and current book, and the paper-only
    disclaimer folded in as a muted note (replaces the old warning band). */
export function LiveStatusStrip({ s }: { s: StrategyLab }) {
  const fresh = s.generated_at ? `${s.generated_at.slice(0, 16).replace("T", " ")} UTC` : null;
  const disclaimer = s.live_orders_enabled === false
    ? "Paper-trading research only — no real-money orders are placed. Forecasts use per-city NWP/EMOS; SFO publishes its served method and can add optional residual-calibration inputs."
    : s.disclaimer ?? "Paper-trading research only — execution state is not published.";
  const publicationUnavailable = !s.available;
  if (publicationUnavailable || s.accounting?.available === false) {
    const title = publicationUnavailable
      ? "Strategy publication unavailable"
      : "Paper account state unavailable";
    return (
      <div
        role="alert"
        aria-label={title}
        className="rounded-xl border border-warning/25 bg-warning/5 px-4 py-3"
      >
        <div className="grid gap-x-4 gap-y-1 sm:grid-cols-[1fr_auto] sm:items-baseline">
          <span className="flex items-center gap-2 text-xs font-semibold text-warning">
            <Icon icon="solar:shield-warning-bold" className="size-4 shrink-0" aria-hidden="true" />
            {title}
          </span>
          {fresh && (
            <span className="tnum font-mono text-[11px] text-muted sm:justify-self-end">
              checked {fresh}
            </span>
          )}
        </div>
        <p className="mt-2 text-xs leading-relaxed text-muted">
          {publicationUnavailable
            ? s.reason ?? "The Strategy publication did not report an available artifact."
            : s.accounting?.reason ?? "Fresh active paper ledgers did not pass accounting validation."} Active
          profiles and readiness are suppressed until a valid reconciled publication is available.
        </p>
        <p className="mt-3 flex items-start gap-1.5 text-[11px] leading-relaxed text-muted">
          <Icon icon="solar:shield-keyhole-bold" className="mt-px size-3.5 shrink-0" aria-hidden="true" />
          {disclaimer}
        </p>
      </div>
    );
  }
  const profiles = activeProfiles(s);
  return (
    <div
      role="status"
      aria-label="Paper trading runtime snapshot"
      className="rounded-xl bg-surface-secondary px-4 py-3"
    >
      <div className="grid gap-x-4 gap-y-1 sm:grid-cols-[1fr_auto] sm:items-baseline">
        <span className="flex items-center gap-2">
          <span className="size-2 shrink-0 rounded-full bg-success" aria-hidden="true" />
          <span className="text-xs font-semibold text-foreground">Paper runtime snapshot</span>
        </span>
        {fresh && (
          <span className="tnum font-mono text-[11px] text-muted sm:justify-self-end">updated {fresh}</span>
        )}
      </div>
      <dl className="mt-4 grid grid-cols-[repeat(auto-fit,minmax(11.5rem,1fr))] gap-x-6 gap-y-4">
        {profiles.map((profile) => (
          <BookState key={profile.risk_profile} s={s} rp={profile.risk_profile} label={profileDisplayLabel(profile)} />
        ))}
      </dl>
      <AnalysisFreshness s={s} />
      <p className="mt-4 flex items-start gap-1.5 text-[11px] leading-relaxed text-muted">
        <Icon icon="solar:shield-keyhole-bold" className="mt-px size-3.5 shrink-0" aria-hidden="true" />
        {disclaimer}
      </p>
    </div>
  );
}

const timestampFormatter = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZone: "UTC",
  timeZoneName: "short",
});

function publishedAt(value?: string | null) {
  if (!value) return "Publication time unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : timestampFormatter.format(date);
}

function policyName(value?: string | null) {
  if (!value) return "Version unavailable";
  const roi = value.match(/roi-v(\d+)$/i);
  if (roi) return `ROI v${roi[1]}`;
  const version = value.match(/v(\d+)$/i);
  return version ? `Research v${version[1]}` : value;
}

function DossierStat({
  label,
  value,
  note,
  tone,
}: {
  label: string;
  value: string;
  note: string;
  tone?: "pos" | "neg";
}) {
  const toneClass = tone === "pos" ? "text-success" : tone === "neg" ? "text-danger" : "text-foreground";
  return (
    <div className="min-w-0 border-l border-border/70 pl-3 sm:pl-4">
      <dt className="font-mono text-[10px] font-semibold uppercase tracking-[0.14em] text-muted">{label}</dt>
      <dd className={`tnum mt-1 break-words font-display text-xl font-semibold leading-tight ${toneClass}`}>{value}</dd>
      <dd className="mt-1 break-words text-[11px] leading-relaxed text-muted">{note}</dd>
    </div>
  );
}

/** Recruiter-facing evidence cover sheet. Every value comes from the active
    public Strategy artifact; version and balance fields disappear rather than
    being inferred when an older artifact omits them. */
export function EvidenceDossier({ s }: { s: StrategyLab }) {
  const publicationUnavailable = !s.available;
  if (publicationUnavailable || s.accounting?.available === false) {
    return (
      <section
        role="alert"
        aria-labelledby="strategy-evidence-dossier"
        className="relative min-w-0 overflow-hidden rounded-3xl border border-warning/30 bg-warning/5 p-5 shadow-lg sm:p-7"
      >
        <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-warning/80 to-transparent" aria-hidden="true" />
        <div className="flex min-w-0 items-start gap-4">
          <span className="grid size-11 shrink-0 place-items-center rounded-xl bg-warning/10 text-warning ring-1 ring-warning/25">
            <Icon icon="solar:shield-warning-bold" className="size-5" aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <Chip size="sm" variant="soft" color="warning">
                <Chip.Label>Evidence withheld</Chip.Label>
              </Chip>
              <span className="tnum font-mono text-[10px] uppercase tracking-[0.12em] text-muted">
                {publishedAt(s.generated_at)}
              </span>
            </div>
            <h2 id="strategy-evidence-dossier" className="mt-4 text-balance font-display text-2xl font-semibold tracking-tight text-foreground sm:text-3xl">
              Strategy evidence unavailable
            </h2>
            <p className="mt-3 max-w-3xl text-sm leading-relaxed text-muted">
              {publicationUnavailable
                ? s.reason ?? "The Strategy publication did not report an available artifact."
                : s.accounting?.reason ?? "Fresh active paper ledgers did not pass accounting validation."}
            </p>
            <p className="mt-2 max-w-3xl text-xs leading-relaxed text-muted">
              Active balances, policy claims, and readiness evidence are suppressed until a valid reconciled publication is available.
            </p>
          </div>
        </div>
      </section>
    );
  }

  const currentProfiles = activeProfiles(s);
  const live = currentProfiles.find((profile) => profile.risk_profile === "live");
  const research = currentProfiles.find(
    (profile) => profile.risk_profile === "research-target" || profile.risk_profile === "research",
  );
  const hasLedgerPair = Boolean(live && research);
  const target = research ? researchDailyTarget(s, research) : undefined;
  const liveSummary = live?.paper_trading?.summary;
  const researchSummary = research?.paper_trading?.summary;
  const liveBalance = live?.daily_summary?.current_equity;
  const researchBalance = research?.daily_summary?.current_equity;
  const policy = policyName(target?.policy_version);
  const policyMarker = target?.policy_version?.match(/(?:^|-)v(\d+)(?:$|-)/i)?.[1];
  const checks = s.real_money_readiness;
  // A deferred checklist still publishes the runtime's own status; that answer
  // is more informative than degrading the whole field to "Not published".
  const publishedStatus = (checks?.status ?? checks?.verdict ?? "").replace(/_/g, " ").trim();
  const readiness = checks?.available
    ? `${checks.checks_passed ?? 0}/${checks.checks_total ?? checks.checks?.length ?? 0} checks`
    : publishedStatus || "Not published";
  const explicitLiveOrders = s.live_orders_enabled;
  const paperOnly = explicitLiveOrders === false
    || (explicitLiveOrders == null && /paper/i.test(s.mode));
  const executionState = explicitLiveOrders === true
    ? "Live enabled"
    : paperOnly
      ? "Paper only"
      : "Unverified";
  const executionNote = explicitLiveOrders === true
    ? `${readiness} · runtime reports live orders enabled`
    : paperOnly
      ? `${readiness} · live orders disabled`
      : `${readiness} · execution flag unavailable`;
  const archiveByKey = new Map(
    archivedProfiles(s).map((profile) => [profile.risk_profile, profile] as const),
  );
  const lineageRows: Array<[string, string]> = [
    ["research-target-v1", "Archived control"],
    ["research-target-v3", "Rejected policy"],
    ["research-target-v4", "Adopted revision"],
    ["research-target-v5", "Superseded scale probe"],
  ].flatMap(([riskProfile, note]) => {
    const profile = archiveByKey.get(riskProfile);
    if (!profile) return [];
    const version = riskProfile.match(/v\d+$/i)?.[0] ?? riskProfile;
    const resolved = profile.paper_trading?.summary?.closed_positions;
    const sample = riskProfile === "research-target-v5" && resolved != null
      ? ` · n=${resolved.toLocaleString()}`
      : "";
    return [[version, `${note}${sample}`] as [string, string]];
  });
  if (target?.policy_version) {
    lineageRows.push([
      policyMarker ? `v${policyMarker}` : "Now",
      "Active research policy",
    ]);
  }

  return (
    <section
      aria-labelledby="strategy-evidence-dossier"
      className="relative min-w-0 overflow-hidden rounded-3xl border border-border/70 bg-surface p-5 shadow-lg sm:p-7"
    >
      <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/80 to-transparent" aria-hidden="true" />
      <div className="min-w-0">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Chip size="sm" variant="soft" color="accent">
              <Chip.Label>Published evidence</Chip.Label>
            </Chip>
            <span className="tnum font-mono text-[10px] uppercase tracking-[0.12em] text-muted">
              {publishedAt(s.generated_at)}
            </span>
          </div>
          <h2 id="strategy-evidence-dossier" className="mt-4 max-w-3xl text-balance font-display text-2xl font-semibold tracking-tight text-foreground sm:text-3xl">
            {hasLedgerPair
              ? "Two paper ledgers. One public evidence trail."
              : "Published strategy evidence"}
          </h2>
          <p className="mt-3 max-w-3xl text-pretty text-sm leading-relaxed text-muted">
            {hasLedgerPair
              ? <>Live Stability is the only readiness cohort. {policy} is the active bounded research policy. Their account balances, positions, and P&amp;L remain separate; cross-profile attribution is labeled explicitly.</>
              : "This publication does not expose both canonical active paper ledgers. Available fields remain visible without substituting archived profiles or inferring missing balances."}
          </p>

          <dl className="mt-6 grid min-w-0 grid-cols-2 gap-x-4 gap-y-6 lg:grid-cols-4">
            <DossierStat
              label="Active policy"
              value={policy}
              note={target?.policy_version ?? "No policy version in this artifact"}
            />
            <DossierStat
              label="Readiness paper balance"
              value={liveBalance == null ? "—" : money(liveBalance, { sign: "negative-only" })}
              note={liveSummary ? `${money(liveSummary.realized_pnl)} P&L · ${liveSummary.closed_positions} resolved` : "Readiness record unavailable"}
              tone={(liveSummary?.realized_pnl ?? 0) > 0 ? "pos" : (liveSummary?.realized_pnl ?? 0) < 0 ? "neg" : undefined}
            />
            <DossierStat
              label="Research paper balance"
              value={researchBalance == null ? "—" : money(researchBalance, { sign: "negative-only" })}
              note={researchSummary ? `${money(researchSummary.realized_pnl)} P&L · ${researchSummary.closed_positions} resolved` : "Research record unavailable"}
              tone={(researchSummary?.realized_pnl ?? 0) > 0 ? "pos" : (researchSummary?.realized_pnl ?? 0) < 0 ? "neg" : undefined}
            />
            <DossierStat
              label="Real-money state"
              value={executionState}
              note={executionNote}
              tone={explicitLiveOrders === true ? "neg" : undefined}
            />
          </dl>
        </div>

        <div className="mt-6 min-w-0 border-t border-border/60 pt-4">
          <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
            <p className="text-xs font-semibold text-foreground">Policy decision trail</p>
            <p className="text-[11px] leading-relaxed text-muted">
              Archived eras stay entry-frozen; preserved positions settle normally.
            </p>
          </div>
          {lineageRows.length ? (
          <ol className="mt-3 grid min-w-0 grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-5">
            {lineageRows.map(([version, note], index, rows) => (
              <li
                key={`${version}-${note}`}
                className={`relative flex min-w-0 items-center gap-2 ${index === rows.length - 1 ? "col-span-2 sm:col-span-1" : ""}`}
              >
                <span className={`grid size-7 shrink-0 place-items-center rounded-full border font-mono text-[10px] font-semibold ${
                  index === rows.length - 1
                    ? "border-accent/40 bg-accent-soft text-[color:var(--accent-text)]"
                    : "border-border bg-background text-foreground"
                }`}>
                  {version}
                </span>
                <span className="min-w-0 text-[11px] leading-snug text-muted">{note}</span>
                {index < rows.length - 1 && (
                  <Icon
                    icon="solar:arrow-right-linear"
                    className="ml-auto hidden size-3.5 shrink-0 text-muted/60 sm:block"
                    aria-hidden="true"
                  />
                )}
              </li>
            ))}
          </ol>
          ) : (
            <p className="mt-3 text-xs leading-relaxed text-muted">
              No versioned policy lineage was published in this artifact.
            </p>
          )}
        </div>
      </div>
    </section>
  );
}

/** The paper readiness candidate's own performance, surfaced as the section headline so the
    real-money book is judged on its own record — not the blended, research-dragged
    combined figure that leads the KPI strip. */
function LiveHero({ p, sum }: { p: ProfileEntry; sum: ProfilePaperSummary }) {
  const pnl = sum.realized_pnl ?? 0;
  const up = pnl >= 0;
  const win = p.daily_summary?.window_days ?? 7;
  return (
    <div>
      <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-4">
        <div className="min-w-0">
          <p className="flex items-center gap-1.5 font-mono text-[10px] font-semibold uppercase tracking-[0.18em] text-[color:var(--accent-text)]">
            <Icon icon="solar:shield-check-bold" className="size-3.5 shrink-0" aria-hidden="true" />
            Live Stability · paper readiness profile
          </p>
          <div className="mt-1.5 flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
            <span className={`font-display text-4xl font-bold tracking-tight ${up ? "text-success" : "text-danger"}`}>
              {money(pnl)}
            </span>
            <span className="text-xs text-muted">attributed realized P&amp;L · {win}-day window</span>
          </div>
        </div>
        <dl className="flex flex-wrap gap-x-6 gap-y-4">
          <HeroStat
            label="Realized paper balance"
            value={
              p.daily_summary?.current_equity == null
                ? "—"
                : money(p.daily_summary.current_equity, { sign: "negative-only" })
            }
          />
          <HeroStat label="ROI · resolved" value={sum.roi == null ? "—" : pct(sum.roi, 1)} tone={(sum.roi ?? 0) > 0 ? "pos" : (sum.roi ?? 0) < 0 ? "neg" : undefined} />
          <HeroStat label="Hit rate" value={sum.hit_rate == null ? "—" : pct(sum.hit_rate, 1)} />
          <HeroStat label="Resolved" value={`${sum.closed_positions ?? 0} · ${sum.win_count ?? 0}–${sum.loss_count ?? 0}`} />
        </dl>
      </div>
    </div>
  );
}

/** Shown in place of the readiness equity curve when the per-book series is missing
    and a research book is present in the artifact: the combined (all-account)
    series would plot research activity under the "Live candidate" label, so
    this reports the gap honestly instead of showing a mislabeled number. */
function LiveCurveUnavailable() {
  return (
    <div
      role="status"
      className="flex h-[288px] flex-col items-center justify-center gap-2 rounded-2xl border border-dashed border-border/70 bg-surface-secondary/60 px-4 text-center"
    >
      <Icon icon="solar:clock-circle-bold" className="size-5 text-warning" aria-hidden="true" />
      <p className="text-sm font-medium text-foreground">Readiness equity curve unavailable.</p>
      <p className="max-w-sm text-xs text-muted">
        Per-book accounting isn't published for this artifact, and a research book is present, so the combined total
        isn't shown under the readiness label.
      </p>
    </div>
  );
}

/** Overview equity block: LIVE leads (hero stats + its own curve), RESEARCH follows on
    a separate, visually secondary curve. The two books' P&L never share a line. */
export function OverviewEquity({ s }: { s: StrategyLab }) {
  const profiles = activeProfiles(s);
  const live = profiles.find((profile) => profile.risk_profile === "live");
  const liveDays = live?.daily_summary?.days;
  const liveSum = live?.paper_trading?.summary;
  // The combined (all-account) series only stands in for the live book when no
  // research book is published — otherwise it would plot research activity
  // under the "Live candidate" label.
  const hasResearchBook = (s.profiles ?? []).some(
    (profile) => profile.risk_profile !== "live" && profile.risk_profile !== "live-legacy",
  );

  // Fall back to the combined curve only if the per-book series is missing AND
  // no research book exists to contaminate it. Otherwise show an explicit
  // unavailable state rather than mislabeled all-account numbers.
  const liveCurve =
    live && liveDays?.length ? (
      <EquityCurve
        s={s}
        days={liveDays}
        startingBankroll={0}
        contributionMode
        windowDays={live.daily_summary?.window_days}
        emphasis="headline"
        eyebrow="Live Stability · paper readiness profile"
        title="Live Stability — cumulative P&L"
        description={`Daily and cumulative realized P&L with the account balance on hover · ${live.daily_summary?.window_days ?? liveDays.length}-day view`}
      />
    ) : hasResearchBook ? (
      <LiveCurveUnavailable />
    ) : (
      <EquityCurve s={s} emphasis="headline" eyebrow="Live Stability · paper readiness profile" title="Live Stability — cumulative P&L" />
    );

  return (
    <div className="space-y-4">
      {liveCurve}
      {live && liveSum && <LiveHero p={live} sum={liveSum} />}
    </div>
  );
}

function DisclosureHeading({ icon, title, note }: { icon: string; title: string; note: string }) {
  return (
    <Accordion.Heading>
      <Accordion.Trigger className="group flex min-h-16 w-full touch-manipulation items-center gap-3 px-4 py-3 text-left focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[color:var(--focus)]">
        <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-surface-secondary text-accent">
          <Icon icon={icon} className="size-4.5" aria-hidden="true" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block font-display text-sm font-semibold text-foreground">{title}</span>
          <span className="mt-0.5 block text-xs text-muted">{note}</span>
        </span>
        <Accordion.Indicator aria-hidden="true" />
      </Accordion.Trigger>
    </Accordion.Heading>
  );
}

export default function StrategyLabView() {
  const { data: s, error } = useStrategyLab();
  const canonicalTargetPublished = !!s && activeProfiles(s).some((profile) => profile.risk_profile === "research-target");
  const publicationUnavailable = !!s && !s.available;

  return (
    <>
      <PageHeader
        headingId="lab-page-title"
        icon="solar:test-tube-bold"
        eyebrow="Strategy Lab"
        title="Strategy research, in public"
        sub={publicationUnavailable
          ? "Strategy evidence is withheld when the runtime publication is unavailable; stale local balances and profiles are never substituted."
          : canonicalTargetPublished
            ? "Two independent paper ledgers, one readiness profile, and a versioned ROI research book. Open and closed positions, dated evidence, and rejected experiments remain inspectable."
            : "The paper readiness cohort and published legacy research evidence remain separate. No missing account or balance is inferred; every number comes from the AWS runtime."}
      />
      <div className="mx-auto w-full max-w-6xl px-5 pb-20 pt-12 sm:px-8">
        <StrategyPublicationNotice generatedAt={s?.generated_at} />
        {error && <div role="alert" className="grid h-48 place-items-center text-sm text-muted">Couldn't load the Strategy Lab — {error}</div>}
        {!error && !s && (
          <div role="status" aria-live="polite" className="flex h-48 items-center justify-center gap-2 text-muted">
            <Icon icon="solar:refresh-bold" className="size-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
            <span className="text-sm">Loading paper-trading research…</span>
          </div>
        )}
        {publicationUnavailable && s && (
          <div role="alert" aria-label="Strategy publication unavailable" className="rounded-2xl border border-warning/30 bg-warning/5 p-5 text-sm leading-relaxed text-muted">
            <strong className="block font-semibold text-warning">Strategy publication unavailable</strong>
            <span className="mt-2 block">{s.reason ?? "The runtime did not publish an available Strategy artifact."}</span>
          </div>
        )}
        {s?.available && (
          <>
            <Reveal immediate className="mb-6">
              <EvidenceDossier s={s} />
            </Reveal>
            <Reveal immediate className="mb-10">
              <LiveStatusStrip s={s} />
            </Reveal>

            {/* ---- One profile at a time, including its open and closed book. ---- */}
            <section id="active-books" className="scroll-mt-24">
              <SectionHeading
                index="01"
                eyebrow="Active books"
                title="Inspect the running ledgers"
                sub="Choose Live Stability or Research ROI. Each view keeps its balance, dated curve, gates, open positions, pending limits, and closed-position evidence in one account-scoped workbench."
              />
              <Reveal>
                <ProfileExplorer s={s} />
              </Reveal>
            </section>

            <section id="research-lineage" className="mt-14 scroll-mt-24">
              <SectionHeading
                index="02"
                eyebrow="Research lineage"
                title="Experiments that changed the system"
                sub="A compact switchboard of evidence-bearing experiments. Choose an era to inspect its dated attribution curve, exact resolved record, and the decision it informed; superseded profiles remain in the public artifact."
              />
              <ArchivedPerformance s={s} />
            </section>

            {/* ---- System-wide results and conclusions after profile inspection. ---- */}
            <section id="validation-controls" className="mt-14 scroll-mt-24">
              <SectionHeading
                index="03"
                eyebrow="Validation & controls"
                title="What the evidence permits"
                sub="The cross-profile conclusions, readiness verdict, and supporting evidence. High-value outcomes stay visible; deeper trading, model, and operations diagnostics unfold on demand."
              />
              <div className="space-y-6">
                <TrackRecordFinding s={s} />
                <SelectivityFinding s={s} />
                <ReadinessFinding s={s} />
              </div>
              <Reveal className="mt-6">
                <ReadinessPanel s={s} />
              </Reveal>
              <Reveal className="mt-6 space-y-6">
                <CalibrationCompare s={s} />
                <GateFunnel s={s} />
                <OpsHealth s={s} />
              </Reveal>
              <Reveal className="mt-6">
                <Accordion variant="surface" hideSeparator className="overflow-hidden rounded-2xl">
                  <Accordion.Item id="model-evidence">
                    <DisclosureHeading
                      icon="solar:chart-square-bold"
                      title="Supporting model detail"
                      note="Secondary movers and backtest coverage"
                    />
                    <Accordion.Panel>
                      <Accordion.Body className="space-y-6 px-4 pb-4 pt-3">
                        <MoversCard s={s} />
                        <BacktestStats s={s} />
                      </Accordion.Body>
                    </Accordion.Panel>
                  </Accordion.Item>
                  <Accordion.Item id="runtime-controls">
                    <DisclosureHeading
                      icon="solar:settings-minimalistic-bold"
                      title="Execution policy detail"
                      note="Exit rules and daily activity history"
                    />
                    <Accordion.Panel>
                      <Accordion.Body className="space-y-6 px-4 pb-4 pt-3">
                        <ExitPolicyCard s={s} />
                        <DailyActivity s={s} />
                      </Accordion.Body>
                    </Accordion.Panel>
                  </Accordion.Item>
                  <Accordion.Item id="glossary">
                    <DisclosureHeading
                      icon="solar:notebook-bold"
                      title="Research glossary"
                      note="Definitions and caveats for interpreting the published numbers"
                    />
                    <Accordion.Panel>
                      <Accordion.Body className="px-4 pb-4 pt-3">
                        <ResearchNotes s={s} />
                      </Accordion.Body>
                    </Accordion.Panel>
                  </Accordion.Item>
                </Accordion>
              </Reveal>
            </section>
          </>
        )}
      </div>
    </>
  );
}
