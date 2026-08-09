import { Card } from "@heroui/react/card";
import { Chip } from "@heroui/react/chip";
import { Icon } from "@iconify/react/offline";
import { useRef, useState, type KeyboardEvent } from "react";
import { pct } from "../../lib/data";
import {
  archivedProfiles,
  featuredArchivedProfiles,
  money,
  profileDisplayLabel,
  type AccountSnapshot,
  type DayRow,
  type ProfileEntry,
  type StrategyLab,
} from "../../lib/strategy";
import { Stat } from "../ui/Stat";
import { EquityCurve } from "./EquityCurve";

type ArchiveStory = {
  stage: string;
  title: string;
  note: string;
  icon: string;
  color: "accent" | "warning" | "default";
};

const ARCHIVE_STORIES: Record<string, ArchiveStory> = {
  "live-legacy": {
    stage: "Historical benchmark",
    title: "Prior readiness benchmark",
    note:
      "The earlier readiness strategy remains the long-run benchmark. Its attributed result stays separate from the shared historical account that also carried research activity.",
    icon: "solar:shield-check-bold",
    color: "default",
  },
  "research-target-v1": {
    stage: "Archived control",
    title: "Baseline control record",
    note:
      "This archived control is the comparison baseline retained beside later ROI policies. Its dated outcome and exact resolved count remain visible without treating the result as a forecast.",
    icon: "solar:compass-big-bold",
    color: "default",
  },
  "research-target-v3": {
    stage: "Rejected experiment",
    title: "Rejected ROI policy",
    note:
      "The v3 policy was rejected and frozen after evaluation. Its realized outcome remains visible as a discarded branch; the chart reports what happened, not proof of why the policy underperformed.",
    icon: "solar:close-circle-bold",
    color: "warning",
  },
  "research-target-v4": {
    stage: "Adopted revision",
    title: "Adopted ROI revision",
    note:
      "The v4 revision was adopted as the next policy step. Its exact small sample stays visible rather than being presented as proof of mechanism or a promised run rate.",
    icon: "solar:check-circle-bold",
    color: "accent",
  },
};

const dateFormatter = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
  timeZone: "UTC",
});

function dateOnly(value?: string | null) {
  if (!value) return null;
  const date = new Date(`${value.slice(0, 10)}T12:00:00Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

function dateRange(start?: string | null, end?: string | null) {
  const first = dateOnly(start);
  const last = dateOnly(end);
  if (!first && !last) return "Date range unavailable";
  if (!first || !last || first.getTime() === last.getTime()) {
    return dateFormatter.format(first ?? last!);
  }
  return dateFormatter.formatRange(first, last);
}

function formattedDate(value: string) {
  const parsed = dateOnly(value);
  return parsed ? dateFormatter.format(parsed) : value;
}

function activityDays(days: DayRow[]) {
  return days.filter(
    (day) =>
      (day.opened ?? day.trades_opened ?? 0) > 0 ||
      (day.closed ?? 0) > 0 ||
      (day.resolved ?? 0) > 0 ||
      (day.wins ?? 0) > 0 ||
      (day.losses ?? 0) > 0 ||
      Math.abs(day.realized_pnl ?? 0) > 0.0001 ||
      (day.signals ?? 0) > 0 ||
      (day.approved_signals ?? 0) > 0,
  );
}

function hasPublishedActivityFields(day: DayRow) {
  return [
    day.opened,
    day.trades_opened,
    day.closed,
    day.resolved,
    day.wins,
    day.losses,
    day.realized_pnl,
    day.signals,
    day.approved_signals,
  ].some((value) => value != null);
}

function resolvedLabel(resolved?: number) {
  return resolved == null ? "Resolved n unavailable" : `${resolved.toLocaleString()} resolved`;
}

function recordText(profile: ProfileEntry) {
  const summary = profile.paper_trading?.summary;
  if (summary?.closed_positions == null) return "—";
  if (summary.win_count == null || summary.loss_count == null) {
    return summary.closed_positions.toLocaleString();
  }
  return `${summary.closed_positions.toLocaleString()} · ${summary.win_count.toLocaleString()}–${summary.loss_count.toLocaleString()}`;
}

function exactAccountFor(
  accounts: Array<AccountSnapshot & { profile_key?: string; attribution_profile_key?: string }>,
  riskProfile: string,
) {
  // An attribution key can point into a shared economic ledger. Only an exact
  // profile-key match proves this archive owned the published account balance.
  return accounts.find((account) => account.profile_key === riskProfile);
}

function ArchiveSparkline({ days }: { days: DayRow[] }) {
  const values = days
    .map((day) => day.cumulative_realized)
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  if (!values.length) return null;
  const low = Math.min(...values, 0);
  const high = Math.max(...values, 0);
  const span = Math.max(high - low, 1);
  const width = 56;
  const height = 18;
  // The stroke is centred on the path and does not scale, so a point mapped
  // flush to 0 or to `height` loses its outer half to the viewBox edge. Inset
  // the plot band by half the stroke to keep the extremes fully drawn.
  const inset = 1;
  const points = values.map((value, index) => {
    const x = values.length === 1 ? width / 2 : (index / (values.length - 1)) * width;
    const y = height - inset - ((value - low) / span) * (height - inset * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const last = values.at(-1) ?? 0;
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      className={`h-[18px] w-14 shrink-0 ${last > 0 ? "text-success" : last < 0 ? "text-danger" : "text-muted"}`}
      aria-hidden="true"
    >
      <polyline
        points={points.join(" ")}
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

function DailyEvidence({ days, title }: { days: DayRow[]; title: string }) {
  if (!days.length) return null;
  const rows = [...days].sort((a, b) => b.date.localeCompare(a.date));
  return (
    <details className="group min-w-0 border-t border-border/60">
      <summary className="flex min-h-12 cursor-pointer touch-manipulation list-none items-center justify-between gap-3 px-4 text-xs font-semibold text-foreground focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[color:var(--focus)] [&::-webkit-details-marker]:hidden">
        <span className="min-w-0">Inspect {title} daily evidence</span>
        <span className="flex shrink-0 items-center gap-2 font-normal text-muted">
          {rows.length} dated row{rows.length === 1 ? "" : "s"}
          <Icon
            icon="solar:alt-arrow-down-linear"
            className="size-3.5 transition-transform group-open:rotate-180 motion-reduce:transition-none"
            aria-hidden="true"
          />
        </span>
      </summary>
      <div
        role="region"
        aria-label={`${title} scrollable daily evidence table`}
        tabIndex={0}
        className="w-full min-w-0 max-w-full overflow-x-auto overscroll-x-contain border-t border-border/60 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[color:var(--focus)]"
      >
        <table className="w-full min-w-[34rem] border-collapse text-left text-xs">
          <caption className="sr-only">{title} daily attribution evidence</caption>
          <thead className="bg-surface-secondary text-[10px] uppercase tracking-wide text-muted">
            <tr>
              <th scope="col" className="px-4 py-2 font-medium">Date</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Day P&amp;L</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Cumulative</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Resolved</th>
              <th scope="col" className="px-4 py-2 text-right font-medium">W–L</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/50">
            {rows.map((day) => {
              const daily = typeof day.realized_pnl === "number" && Number.isFinite(day.realized_pnl)
                ? day.realized_pnl
                : null;
              const cumulative = typeof day.cumulative_realized === "number" && Number.isFinite(day.cumulative_realized)
                ? day.cumulative_realized
                : null;
              const wins = typeof day.wins === "number" ? day.wins : null;
              const losses = typeof day.losses === "number" ? day.losses : null;
              const resolved = typeof day.resolved === "number"
                ? day.resolved
                : wins != null && losses != null
                  ? wins + losses
                  : null;
              return (
                <tr key={day.date}>
                  <th scope="row" className="whitespace-nowrap px-4 py-2.5 font-mono font-medium text-foreground">
                    {formattedDate(day.date)}
                  </th>
                  <td className={`tnum px-3 py-2.5 text-right font-medium ${(daily ?? 0) > 0 ? "text-success" : (daily ?? 0) < 0 ? "text-danger" : "text-muted"}`}>
                    {daily == null ? "—" : money(daily)}
                  </td>
                  <td className="tnum px-3 py-2.5 text-right text-foreground">
                    {cumulative == null ? "—" : money(cumulative)}
                  </td>
                  <td className="tnum px-3 py-2.5 text-right text-muted">
                    {resolved ?? "—"}
                  </td>
                  <td className="tnum whitespace-nowrap px-4 py-2.5 text-right text-muted">
                    {wins != null && losses != null ? `${wins}–${losses}` : "—"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function ArchiveEvidenceCard({
  s,
  profile,
  story,
  account,
}: {
  s: StrategyLab;
  profile: ProfileEntry;
  story: ArchiveStory;
  account?: AccountSnapshot;
}) {
  const summary = profile.paper_trading?.summary;
  const pnl = summary?.realized_pnl;
  const days = [...(profile.daily_summary?.days ?? [])]
    .filter((day) => Boolean(day?.date))
    .sort((a, b) => a.date.localeCompare(b.date));
  const activeDays = activityDays(days);
  const activityFieldsPublished = days.some(hasPublishedActivityFields);
  const evidenceStart = profile.daily_summary?.window_start ?? days[0]?.date;
  const evidenceEnd = profile.daily_summary?.window_end ?? days.at(-1)?.date;
  const activity = activeDays.length
    ? `Visible activity ${dateRange(activeDays[0].date, activeDays.at(-1)?.date)}`
    : activityFieldsPublished
      ? "No activity recorded across this published run"
      : "Activity fields unavailable for this published run";
  const open = summary?.open_positions ?? 0;
  const pending = summary?.pending_limit_orders ?? 0;
  const settling = open > 0 || pending > 0;

  return (
    <Card
      id={`archive-panel-${profile.risk_profile}`}
      role="tabpanel"
      aria-labelledby={`archive-tab-${profile.risk_profile}`}
      tabIndex={0}
      data-archive-profile={profile.risk_profile}
      className="w-full min-w-0 max-w-full overflow-hidden rounded-2xl border border-border/70 bg-surface/90 shadow-sm focus-visible:ring-2 focus-visible:ring-[color:var(--focus)]"
    >
        <Card.Header className="flex min-w-0 flex-col items-stretch gap-4 border-b border-border/55 p-4 sm:flex-row sm:items-start sm:justify-between sm:p-5">
          <div className="flex min-w-0 items-start gap-3">
            <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-surface-secondary text-accent ring-1 ring-border/60">
              <Icon icon={story.icon} className="size-5" aria-hidden="true" />
            </span>
            <div className="min-w-0">
              <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.14em] text-[color:var(--accent-text)]">
                {story.stage}
              </p>
              <Card.Title className="mt-1 break-words text-balance text-lg leading-tight">
                {story.title}
              </Card.Title>
              <p className="mt-1 break-words text-xs leading-relaxed text-muted">{profileDisplayLabel(profile)}</p>
            </div>
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2 sm:justify-end">
            <Chip size="sm" variant="soft" color={story.color}>
              <Chip.Label>{resolvedLabel(summary?.closed_positions)}</Chip.Label>
            </Chip>
            <Chip size="sm" variant="soft" color={settling ? "warning" : "default"}>
              <Chip.Label>{settling ? "Settling" : "Frozen"}</Chip.Label>
            </Chip>
          </div>
        </Card.Header>

        <Card.Content className="min-w-0 space-y-5 p-4 sm:p-5">
          <div className="grid min-w-0 gap-5 xl:grid-cols-[minmax(0,1.35fr)_minmax(17rem,0.65fr)] xl:items-start">
            <div className="min-w-0">
              <EquityCurve
                s={s}
                days={days}
                startingBankroll={0}
                contributionMode
                windowDays={profile.daily_summary?.window_days}
                emphasis="secondary"
                height={176}
                eyebrow="Attribution only"
                title={`${story.title} — full-run P&L`}
                description={`${dateRange(evidenceStart, evidenceEnd)} · this experiment's full run, attribution not account equity`}
                className="min-w-0 max-w-full"
              />
            </div>

            <div className="min-w-0 space-y-5">
              <div>
                <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.14em] text-muted">
                  Evidence window
                </p>
                <p className="mt-1 text-sm font-semibold text-foreground">
                  {dateRange(evidenceStart, evidenceEnd)}
                </p>
                <p className="mt-1 text-xs leading-relaxed text-muted">{activity}</p>
              </div>

              <div className="grid grid-cols-1 gap-x-5 gap-y-4 min-[360px]:grid-cols-2">
                <Stat
                  label="Attributed P&L"
                  value={pnl == null ? "—" : money(pnl)}
                  tone={pnl == null ? "default" : pnl > 0 ? "pos" : pnl < 0 ? "neg" : "default"}
                />
                <Stat label="Resolved · W–L" value={recordText(profile)} />
                <Stat
                  label="Hit rate"
                  value={summary?.hit_rate == null ? "—" : pct(summary.hit_rate, 1)}
                />
                <Stat
                  label="ROI · resolved"
                  value={summary?.roi == null ? "—" : pct(summary.roi, 1)}
                  tone={(summary?.roi ?? 0) > 0 ? "pos" : (summary?.roi ?? 0) < 0 ? "neg" : "default"}
                />
                <Stat
                  label="Days traded"
                  value={activeDays.length ? `${activeDays.length} of ${days.length}` : "—"}
                />
                <Stat label="Open · resting" value={`${open} · ${pending}`} />
              </div>
            </div>
          </div>

          <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(18rem,auto)] lg:items-start">
            <p className="text-sm leading-relaxed text-muted">
              <strong className="font-semibold text-foreground">What changed:</strong>{" "}
              {story.note}
            </p>
            <p className="flex min-w-0 items-start gap-2 rounded-xl bg-surface-secondary px-3 py-2.5 text-xs leading-relaxed text-muted ring-1 ring-border/50">
              <Icon
                icon={account ? "solar:safe-circle-bold" : "solar:shield-warning-bold"}
                className="mt-0.5 size-4 shrink-0 text-accent"
                aria-hidden="true"
              />
              <span>
                {account ? (
                  <>
                    Independent economic ledger ·{" "}
                    <strong className="font-semibold text-foreground">
                      {money(account.realized_equity, { sign: "negative-only" })} realized balance
                    </strong>
                    {account.reconciliation_status
                      ? ` · ${account.reconciliation_status}`
                      : ""}
                  </>
                ) : profile.risk_profile === "live-legacy" ? (
                  "Shared historical ledger: no strategy-specific balance can be assigned to this attribution."
                ) : (
                  "No dated economic-balance series is published for this era; the curve remains attribution only."
                )}
              </span>
            </p>
          </div>

          {settling && (
            <p className="flex items-start gap-2 rounded-xl border border-warning/25 bg-warning/5 px-3 py-2.5 text-xs leading-relaxed text-warning">
              <Icon icon="solar:refresh-circle-bold" className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
              {open} preserved open position{open === 1 ? "" : "s"}
              {pending > 0 ? ` and ${pending} resting order${pending === 1 ? "" : "s"}` : ""} still
              settling; this achieved record can move.
            </p>
          )}
        </Card.Content>

        <DailyEvidence days={days} title={story.title} />
    </Card>
  );
}

export function ArchivedPerformance({ s }: { s: StrategyLab }) {
  const allProfiles = archivedProfiles(s);
  const profiles = featuredArchivedProfiles(s);
  const defaultKey = profiles.find((profile) => profile.risk_profile === "research-target-v4")?.risk_profile
    ?? profiles.at(-1)?.risk_profile
    ?? "";
  const [selectedKey, setSelectedKey] = useState(defaultKey);
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const accounts = (s.accounting?.archived_accounts ?? []) as Array<
    AccountSnapshot & { profile_key?: string; attribution_profile_key?: string }
  >;
  if (!profiles.length) return null;

  const effectiveKey = profiles.some((profile) => profile.risk_profile === selectedKey)
    ? selectedKey
    : defaultKey;
  const selectedIndex = Math.max(
    0,
    profiles.findIndex((profile) => profile.risk_profile === effectiveKey),
  );
  const selected = profiles[selectedIndex];
  const selectedStory = ARCHIVE_STORIES[selected.risk_profile];

  const handleTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      nextIndex = (index + 1) % profiles.length;
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      nextIndex = (index - 1 + profiles.length) % profiles.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = profiles.length - 1;
    }
    if (nextIndex == null) return;
    event.preventDefault();
    setSelectedKey(profiles[nextIndex].risk_profile);
    tabRefs.current[nextIndex]?.focus();
  };

  return (
    <div className="min-w-0 space-y-4">
      <div className="grid min-w-0 gap-3 rounded-2xl bg-surface-secondary/60 p-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:p-5">
        <div className="flex min-w-0 items-start gap-3">
          <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-accent-soft text-accent ring-1 ring-accent/25">
            <Icon icon="solar:archive-check-bold" className="size-5" aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <h3 id="archived-strategy-attribution" className="text-base font-semibold text-foreground">
              Research decision trail
            </h3>
            <p className="mt-1 max-w-3xl text-xs leading-relaxed text-muted">
              Choose an evidence-bearing era to inspect its dated graph, exact record, and policy decision. One detail panel keeps the lineage compact without hiding the audit trail.
            </p>
          </div>
        </div>
        <div className="flex flex-wrap gap-2 sm:justify-end">
          <Chip size="sm" variant="soft" color="accent">
            <Chip.Label>{profiles.length} featured eras</Chip.Label>
          </Chip>
          <Chip size="sm" variant="soft" color="default">
            <Chip.Label>{allProfiles.length} frozen profiles retained</Chip.Label>
          </Chip>
        </div>
      </div>

      <section
        aria-labelledby="archived-strategy-attribution"
        className="grid min-w-0 gap-3 lg:grid-cols-[minmax(14rem,0.48fr)_minmax(0,1.52fr)] lg:items-start"
      >
        <div
          role="tablist"
          aria-label="Featured strategy eras"
          className="grid min-w-0 grid-cols-2 gap-2 lg:grid-cols-1"
        >
          {profiles.map((profile, index) => {
            const story = ARCHIVE_STORIES[profile.risk_profile];
            if (!story) return null;
            const summary = profile.paper_trading?.summary;
            // Only the selected era's panel is mounted, so an unselected tab must
            // not advertise aria-controls for an element that does not exist.
            const selectedTab = profile.risk_profile === effectiveKey;
            return (
              <button
                key={profile.risk_profile}
                ref={(node) => { tabRefs.current[index] = node; }}
                id={`archive-tab-${profile.risk_profile}`}
                type="button"
                role="tab"
                aria-selected={selectedTab}
                aria-controls={selectedTab ? `archive-panel-${profile.risk_profile}` : undefined}
                tabIndex={selectedTab ? 0 : -1}
                data-archive-option={profile.risk_profile}
                onClick={() => setSelectedKey(profile.risk_profile)}
                onKeyDown={(event) => handleTabKeyDown(event, index)}
                className={`group min-h-24 min-w-0 touch-manipulation rounded-xl border px-3 py-3 text-left transition-colors focus-visible:ring-2 focus-visible:ring-[color:var(--focus)] motion-reduce:transition-none ${
                  selectedTab
                    ? "border-accent/45 bg-accent-soft text-foreground shadow-sm"
                    : "border-border/60 bg-surface/65 text-muted hover:border-border hover:bg-surface-secondary"
                }`}
              >
                <span className="flex min-w-0 items-center justify-between gap-2 text-[10px] font-semibold text-[color:var(--accent-text)]">
                  <span>{String(index + 1).padStart(2, "0")} · {story.stage}</span>
                  <Icon
                    icon={story.icon}
                    className="size-4 shrink-0"
                    aria-hidden="true"
                  />
                </span>
                <span className="mt-1.5 block break-words text-sm font-semibold leading-snug text-foreground">
                  {story.title}
                </span>
                <span className="tnum mt-2 flex min-w-0 items-end justify-between gap-2 text-[11px] leading-tight">
                  <span className="min-w-0">
                    <span className={`block ${(summary?.realized_pnl ?? 0) >= 0 ? "text-success" : "text-danger"}`}>
                      {summary?.realized_pnl == null ? "P&L —" : money(summary.realized_pnl)}
                    </span>
                    <span className="mt-0.5 block text-muted">{resolvedLabel(summary?.closed_positions)}</span>
                  </span>
                  <ArchiveSparkline days={profile.daily_summary?.days ?? []} />
                </span>
              </button>
            );
          })}
        </div>

        {selectedStory && (
          <ArchiveEvidenceCard
            key={selected.risk_profile}
            s={s}
            profile={selected}
            story={selectedStory}
            account={exactAccountFor(accounts, selected.risk_profile)}
          />
        )}
      </section>

      <aside className="flex min-w-0 items-start gap-2 px-1 text-xs leading-relaxed text-muted">
        <Icon icon="solar:filter-bold" className="mt-0.5 size-4 shrink-0 text-accent" aria-hidden="true" />
        <p>
          <strong className="font-semibold text-foreground">Curated, not deleted.</strong>{" "}
          Zero-trade, superseded short-run, execution-only, and non-comparable legacy profiles remain in the public audit artifact but are intentionally omitted from this recruiter-facing lineage.
        </p>
      </aside>
    </div>
  );
}
