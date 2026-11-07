import type { ReactNode } from "react";
import { Chip } from "@heroui/react/chip";
import { Tabs } from "@heroui/react/tabs";
import { Skeleton } from "@heroui/react/skeleton";
import { Icon } from "@iconify/react/offline";
import "../../styles/pro-strategy.css";
import {
  activeProfiles,
  archivedProfiles,
  m1Book,
  money,
  researchDailyTarget,
  useStrategyLab,
  type StrategyLab,
} from "../../lib/strategy";
import { dayRange } from "../../lib/incidents";
import { PageHeader } from "../ui/PageHeader";
import { SectionHeading } from "../ui/SectionHeading";
import { InfoTip } from "../ui/InfoTip";
import { ProfileExplorer } from "../strategy/ProfileExplorer";
import { ReadinessPanel } from "../strategy/ReadinessPanel";
import { GateFunnel } from "../strategy/GateFunnel";
import { CalibrationCompare } from "../strategy/CalibrationCompare";
import { OpsHealth } from "../strategy/OpsHealth";
import { ExitPolicyCard } from "../strategy/ExitPolicyCard";
import { ExitStudy } from "../strategy/ExitStudy";
import { BacktestStats } from "../strategy/BacktestStats";
import { MoversCard } from "../strategy/MoversCard";
import { ResearchNotes } from "../strategy/ResearchNotes";
import { StrategyPublicationNotice } from "../strategy/StrategyPublicationNotice";
import { ArchivedPerformance } from "../strategy/ArchivedPerformance";
import { HybridResearchPanel } from "../strategy/HybridResearchPanel";
import { M1Trial } from "../strategy/M1Trial";
import { mergedAlerts, policyMarker } from "../../lib/lab-format";
import { useHashSub } from "../../lib/useHashRoute";

export type LabTab = "accounts" | "m1" | "research" | "archive" | "system";
const LAB_TABS: { id: LabTab; label: string }[] = [
  { id: "accounts", label: "Accounts" },
  { id: "m1", label: "M1 trial" },
  { id: "research", label: "Research" },
  { id: "archive", label: "Archive" },
  { id: "system", label: "System" },
];

/** `#/lab/research` opens that tab; anything else opens Accounts. */
const LAB_TAB_IDS = LAB_TABS.map((tab) => tab.id);

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

const dateFormatter = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });

/** Whole days between the deploy-time analysis and this publication. */
function analysisAgeDays(s: StrategyLab): number | null {
  const hours = s.real_money_readiness?.analysis_age_hours;
  if (typeof hours === "number" && Number.isFinite(hours)) return Math.floor(hours / 24);
  const analysis = Date.parse(s.analysis_generated_at ?? "");
  const published = Date.parse(s.generated_at ?? "");
  if (Number.isNaN(analysis) || Number.isNaN(published)) return null;
  return Math.floor((published - analysis) / 86_400_000);
}

function analysisIsStale(s: StrategyLab): boolean {
  const r = s.real_money_readiness;
  if (r?.status === "ANALYSIS_STALE") return true;
  const hours = r?.analysis_age_hours;
  const max = r?.max_analysis_age_hours ?? 36;
  return typeof hours === "number" && hours > max;
}

/** One reading of "is real money involved": the runtime's explicit flag wins;
    an older artifact without it falls back to its paper mode string. */
function executionState(s: StrategyLab): "paper" | "live" | "unverified" {
  if (s.live_orders_enabled === true) return "live";
  if (s.live_orders_enabled === false || (s.live_orders_enabled == null && /paper/i.test(s.mode ?? ""))) return "paper";
  return "unverified";
}

/** Status chips under the Lab title: paper-only state, publication time, and
    the date of the deploy-time analysis behind readiness and gate counts. */
export function LabStatusChips({ s }: { s: StrategyLab }) {
  const execution = executionState(s);
  const analysisDate = s.analysis_generated_at ? dateFormatter.format(new Date(s.analysis_generated_at)) : null;
  const stale = analysisIsStale(s);
  const age = analysisAgeDays(s);
  return (
    <>
      <Chip size="sm" variant="soft" color={execution === "live" ? "danger" : execution === "paper" ? "success" : "warning"}>
        <Chip.Label>
          {execution === "live" ? "Live orders enabled" : execution === "paper" ? "Paper only · $0 real money" : "Execution state unverified"}
        </Chip.Label>
      </Chip>
      <Chip size="sm" variant="soft">
        <Chip.Label>Updated {publishedAt(s.generated_at)}</Chip.Label>
      </Chip>
      {analysisDate && (
        <span className="inline-flex items-center gap-0.5">
          <Chip size="sm" variant="soft" color={stale ? "warning" : "default"}>
            <Chip.Label>
              Analysis {analysisDate}
              {stale && age != null ? ` · ${age} days old` : ""}
            </Chip.Label>
          </Chip>
          <InfoTip label="the analysis date" title="Two clocks">
            Balances, positions and trades refresh with every publication. Readiness, gate counts and the research
            rescore come from the deploy-time analysis, last run {analysisDate}.
          </InfoTip>
        </span>
      )}
    </>
  );
}

/** Kept for the System tab: says which numbers are current and which come from
    the older deploy-time analysis, without inventing freshness. */
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
    <p className="text-xs leading-relaxed text-muted">
      {analysisStatus} Paper balances and positions refresh with every publication; readiness evidence refreshes only
      with the deploy-time analysis.
    </p>
  );
}

function Notice({ tone, icon, title, children, info }: {
  tone: "danger" | "warning" | "neutral";
  icon: string;
  title: string;
  children: ReactNode;
  info?: ReactNode;
}) {
  const surface = tone === "danger" ? "bg-danger-soft" : tone === "warning" ? "bg-warning-soft" : "bg-surface-secondary";
  const iconTone = tone === "danger" ? "text-danger" : tone === "warning" ? "text-warning" : "text-muted";
  return (
    <div role={tone === "danger" ? "alert" : "status"} className={`flex min-w-0 items-start gap-3 rounded-2xl px-4 py-3 ${surface}`}>
      <Icon icon={icon} className={`mt-0.5 size-4 shrink-0 ${iconTone}`} aria-hidden="true" />
      <div className="min-w-0 flex-1 text-sm">
        <span className="font-semibold text-foreground">{title}</span> <span className="text-muted">{children}</span>
      </div>
      {info}
    </div>
  );
}

/** Which tab each known alert affects. An alert not listed here is a System
    matter, except a critical one, which shows on every tab. */
const ALERT_TABS: Record<string, LabTab> = {
  "settlement-pending": "accounts",
  "settlement-backlog": "accounts",
  "same-day-entry-blocked": "accounts",
  "emos-live-missing": "system",
};

/** Operational notices for the open tab: runtime alerts (for example positions
    stuck past settlement), each shown only where it applies. Silent when there
    is nothing to say. */
export function LabNotices({ s, tab }: { s: StrategyLab; tab: LabTab }) {
  const backlog = s.paper_trading?.summary as { unresolved_past_targets?: Array<{ open_orders?: number; target_date?: string }> } | undefined;
  const past = backlog?.unresolved_past_targets?.filter((row) => row?.target_date) ?? [];
  const alerts = mergedAlerts(s).filter((alert) => {
    const home = ALERT_TABS[alert.code ?? ""];
    return home ? home === tab : alert.level === "critical" || alert.level === "danger" || tab === "system";
  });
  if (!alerts.length) return null;
  // mergedAlerts keeps one alert per code; name every station that is missing.
  const stations = [...new Set(
    [...(s.forecast_health?.warnings ?? []), ...(s.status?.alerts ?? [])]
      .filter((alert) => alert.code === "emos-live-missing")
      .map((alert) => alert.detail?.match(/^(\w+) has no live/)?.[1])
      .filter(Boolean),
  )];

  return (
    <div className="flex flex-col gap-2 pt-4">
      {alerts.map((alert) => {
        const count = past.reduce((sum, row) => sum + (row.open_orders ?? 0), 0);
        const dates = past.map((row) => dayRange(row.target_date!, row.target_date!)).join(", ");
        const plain: { title: string; text: string } | null =
          alert.code === "settlement-backlog" && past.length > 0
            ? { title: alert.title, text: `${count} open position${count === 1 ? "" : "s"} from ${dates} ${count === 1 ? "has" : "have"} not settled. Their marks are stale.` }
            : alert.code === "same-day-entry-blocked"
              ? { title: "Next-day entries only", text: "Live and V7 only enter next-day markets, so same-day signals are research-only." }
              : alert.code === "emos-live-missing"
                ? { title: "Live forecast missing", text: `${stations.length ? stations.join(", ") : "Some stations"} ${stations.length === 1 ? "has" : "have"} no live forecast distribution yet and use a simpler fallback.` }
                : null;
        return (
          <Notice
            key={alert.code ?? alert.title}
            tone={alert.level === "critical" || alert.level === "danger" ? "danger" : alert.level === "info" ? "neutral" : "warning"}
            icon={alert.level === "info" ? "solar:info-circle-linear" : "solar:danger-triangle-linear"}
            title={plain?.title ?? alert.title}
            info={
              !plain && (alert.detail || alert.action) ? (
                <InfoTip label={alert.title} title={alert.title} placement="bottom">
                  {alert.detail} {alert.action}
                </InfoTip>
              ) : undefined
            }
          >
            {plain?.text ?? alert.detail}
          </Notice>
        );
      })}
    </div>
  );
}

const LINEAGE_NOTES: Record<string, string> = {
  "research-target-v1": "Archived control",
  "research-target-v3": "Rejected",
  "research-target-v4": "Adopted",
  "research-target-v5": "Superseded scale probe",
};

/** The research policy's version history, oldest first, ending at the policy
    the research account runs now. Rows come from the published archive, so a
    version joins the trail on the day it is archived. */
export function PolicyLineage({ s }: { s: StrategyLab }) {
  const research = activeProfiles(s).find((profile) => profile.risk_profile === "research-target");
  const current = policyMarker(research ? researchDailyTarget(s, research)?.policy_version : undefined);
  const rows = archivedProfiles(s).flatMap((profile) => {
    const key = profile.risk_profile;
    const version = key.startsWith("research-target-") ? policyMarker(key) : null;
    if (!version || version === current) return [];
    const resolved = profile.paper_trading?.summary?.closed_positions;
    const sample = key === "research-target-v5" && resolved != null ? ` · n=${resolved.toLocaleString()}` : "";
    return [{ version, note: `${LINEAGE_NOTES[key] ?? "Archived"}${sample}`, current: false }];
  });
  if (current) rows.push({ version: current, note: "Active research policy", current: true });
  if (!rows.length) return null;
  return (
    <ol className="flex min-w-0 flex-wrap items-center gap-2" aria-label="Research policy versions">
      {rows.map((row, index) => (
        <li key={`${row.version}-${row.note}`} className="flex items-center gap-2">
          <span
            className={`inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-xs ${
              row.current ? "bg-accent-soft text-[color:var(--accent-text)]" : "bg-surface-secondary text-muted"
            }`}
          >
            <span className="font-semibold text-foreground">{row.version}</span>
            {row.note}
          </span>
          {index < rows.length - 1 && <Icon icon="solar:alt-arrow-right-linear" className="size-3.5 text-muted" aria-hidden="true" />}
        </li>
      ))}
    </ol>
  );
}

export function ReleaseFindings({ s }: { s: StrategyLab }) {
  const release = s.release;
  const findings = release?.findings?.filter((finding) => finding?.title || finding?.impact) ?? [];
  const linkClass =
    "inline-flex min-h-11 items-center gap-1.5 text-sm font-medium text-[color:var(--accent-text)] underline-offset-4 hover:underline focus-visible:rounded focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--focus)]";
  if (!release || !findings.length) return (
    <section aria-label="Dated V7 research audit" className="flex min-w-0 flex-col gap-1 rounded-3xl bg-surface-secondary p-5">
      <h3 className="text-sm font-semibold text-foreground">V7 audit and preserved results</h3>
      <p className="text-sm text-muted">The October 5, 2026 baseline: account histories, losses and data limits.</p>
      <a href="https://github.com/Jaxsonb04/weather_edge/blob/main/docs/research/2026-10-05-v7-audit.md" className={linkClass}>
        Read the dated audit <Icon icon="solar:arrow-right-up-linear" className="size-4" aria-hidden="true" />
      </a>
    </section>
  );
  const reportUrl = release.baseline_report_url?.startsWith("https://github.com/Jaxsonb04/weather_edge/")
    ? release.baseline_report_url : undefined;
  return (
    <section aria-labelledby="release-findings-title" className="flex min-w-0 flex-col gap-4 rounded-3xl bg-surface-secondary p-5">
      <div>
        <div className="flex items-center gap-1">
          <h3 id="release-findings-title" className="text-sm font-semibold text-foreground">
            {release.version ? `${release.version.toUpperCase()} research audit` : "Published research audit"}
          </h3>
          <InfoTip label="the research audit">
            Each finding separates measured history, tested corrections and remaining validation. A code fix does not
            establish future profit.
          </InfoTip>
        </div>
        {release.comparison_note && <p className="mt-1 text-sm text-muted">{release.comparison_note}</p>}
        {release.evidence_snapshot_at && (
          <p className="mt-1 text-xs text-muted">
            Baseline captured {release.evidence_snapshot_at.slice(0, 16).replace("T", " ")} UTC; the current runtime is reported separately.
          </p>
        )}
      </div>
      <ul className="grid min-w-0 gap-3 sm:grid-cols-2">
        {findings.map((finding, index) => (
          <li key={finding.code ?? index} className="min-w-0 rounded-2xl bg-surface p-4 shadow-surface">
            <h4 className="break-words text-sm font-semibold text-foreground">{finding.title ?? "Research finding"}</h4>
            {finding.evidence && <p className="mt-1 text-xs text-muted">Evidence: {finding.evidence}</p>}
            {finding.impact && <p className="mt-2 break-words text-xs leading-relaxed text-muted">{finding.impact}</p>}
            {finding.state && <p className="mt-2 text-xs font-medium text-[color:var(--accent-text)]">{finding.state}</p>}
          </li>
        ))}
      </ul>
      {reportUrl && (
        <a href={reportUrl} className={linkClass}>
          Read the dated baseline and validation report <Icon icon="solar:arrow-right-up-linear" className="size-4" aria-hidden="true" />
        </a>
      )}
    </section>
  );
}

function LabSkeleton() {
  return (
    <div className="skeleton--shimmer relative flex flex-col gap-6 overflow-hidden" aria-hidden="true">
      <Skeleton animationType="none" className="h-10 w-80 max-w-full rounded-full" />
      <div className="grid gap-3 sm:grid-cols-2">
        <Skeleton animationType="none" className="h-44 rounded-2xl" />
        <Skeleton animationType="none" className="h-44 rounded-2xl" />
      </div>
      <Skeleton animationType="none" className="h-72 rounded-3xl" />
    </div>
  );
}

/** Starting capital shared by both active accounts, when they share one. */
function sharedStart(s: StrategyLab): number | null {
  const starts = activeProfiles(s).map((profile) => profile.daily_summary?.starting_bankroll);
  if (starts.length < 2 || starts.some((value) => typeof value !== "number")) return null;
  return starts.every((value) => value === starts[0]) ? (starts[0] as number) : null;
}

export default function StrategyLabView() {
  const { data: s, error } = useStrategyLab();
  const [tab, selectTab] = useHashSub("lab", LAB_TAB_IDS, "accounts");
  const publicationUnavailable = !!s && !s.available;
  const start = s ? sharedStart(s) : null;
  const pair = !!s && activeProfiles(s).length >= 2;

  return (
    <>
      <PageHeader
        headingId="lab-page-title"
        title="Strategy Lab"
        sub={
          publicationUnavailable
            ? "Strategy evidence is withheld while the runtime publication is unavailable."
            : pair
              ? `Two separate ${start != null ? `${money(start, { digits: 0, sign: "negative-only" })} ` : ""}paper accounts trade the forecasts${
                  s && m1Book(s)?.available ? ", and the M1 trial runs as a third book" : ""
                }. They are never added together.`
              : "Paper accounts trade the forecasts. Every figure comes from the AWS runtime."
        }
        // The chips wrap to three rows on a phone; holding that space stops the
        // heading jumping down when the data lands.
        meta={s?.available ? <LabStatusChips s={s} /> : !s && !error ? <div className="h-20 min-[430px]:h-13 md:h-6" /> : undefined}
      />
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-4 pb-20 sm:px-6 lg:px-8">
        <StrategyPublicationNotice generatedAt={s?.generated_at} />
        {error && (
          <div role="alert" className="flex items-start gap-3 rounded-2xl bg-danger-soft px-4 py-4 text-sm">
            <Icon icon="solar:cloud-cross-linear" className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden="true" />
            <p className="text-muted">
              <span className="font-semibold text-foreground">Couldn't load the Strategy Lab.</span> {error}{" "}
              <button
                type="button"
                onClick={() => window.location.reload()}
                className="font-medium text-[color:var(--accent-text)] underline-offset-4 hover:underline focus-visible:rounded focus-visible:outline-2 focus-visible:outline-[color:var(--focus)]"
              >
                Retry
              </button>
            </p>
          </div>
        )}
        {!error && !s && (
          <>
            <p role="status" aria-live="polite" className="sr-only">Loading paper-trading research…</p>
            <LabSkeleton />
          </>
        )}
        {publicationUnavailable && s && (
          <div role="alert" aria-label="Strategy publication unavailable" className="rounded-2xl bg-warning-soft px-4 py-4 text-sm">
            <strong className="block font-semibold text-foreground">Strategy publication unavailable</strong>
            <span className="mt-1 block text-muted">{s.reason ?? "The runtime did not publish an available Strategy artifact."}</span>
          </div>
        )}
        {s?.available && (
          <div className="fade-in flex min-w-0 flex-col gap-6">
            <Tabs selectedKey={tab} onSelectionChange={(key) => selectTab(String(key) as LabTab)} className="min-w-0">
              <Tabs.ListContainer className="max-w-full self-start">
                <Tabs.List aria-label="Strategy Lab sections">
                  {LAB_TABS.map((item) => (
                    <Tabs.Tab key={item.id} id={item.id}>
                      {item.label}
                      <Tabs.Indicator />
                    </Tabs.Tab>
                  ))}
                </Tabs.List>
              </Tabs.ListContainer>
              <LabNotices s={s} tab={tab} />

              <Tabs.Panel id="accounts" className="pt-4">
                <div className="fade-in">
                  <ProfileExplorer s={s} />
                </div>
              </Tabs.Panel>

              <Tabs.Panel id="m1" className="pt-4">
                <div className="fade-in">
                  <M1Trial s={s} />
                </div>
              </Tabs.Panel>

              <Tabs.Panel id="research" className="pt-4">
                <div className="fade-in flex min-w-0 flex-col gap-4">
                  <ExitStudy s={s} />
                  <GateFunnel s={s} />
                  <CalibrationCompare s={s} />
                  <MoversCard s={s} />
                  <BacktestStats s={s} />
                </div>
              </Tabs.Panel>

              <Tabs.Panel id="archive" className="pt-4">
                <div className="fade-in flex min-w-0 flex-col gap-6">
                  <div>
                    <SectionHeading
                      as="h2"
                      title="Policy history"
                      sub="Each research policy ran on its own account; frozen versions keep their records."
                    />
                    <PolicyLineage s={s} />
                  </div>
                  <ArchivedPerformance s={s} />
                  <ReleaseFindings s={s} />
                  <HybridResearchPanel s={s} />
                </div>
              </Tabs.Panel>

              <Tabs.Panel id="system" className="pt-4">
                <div className="fade-in flex min-w-0 flex-col gap-4">
                  <ReadinessPanel s={s} />
                  <AnalysisFreshness s={s} />
                  <OpsHealth s={s} />
                  <ExitPolicyCard s={s} />
                  <ResearchNotes s={s} />
                </div>
              </Tabs.Panel>
            </Tabs>
          </div>
        )}
      </div>
    </>
  );
}
