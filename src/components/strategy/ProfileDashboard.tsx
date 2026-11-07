import { useState } from "react";
import { Chip } from "@heroui/react/chip";
import { Widget } from "@heroui-pro/react/widget";
import { Icon } from "@iconify/react/offline";
import { pct } from "../../lib/data";
import { usePublication } from "../../lib/publication";
import { dayRange, incidentsForDays } from "../../lib/incidents";
import {
  decisionCountProvenance,
  ledgerByCity,
  ledgerForProfile,
  money,
  monitorForProfile,
  profileGate,
  profileGateCounts,
  researchDailyTarget,
  type ProfileEntry,
  type ResearchDailyTarget,
  type StrategyLab,
} from "../../lib/strategy";
import { Stat } from "../ui/Stat";
import { InfoTip } from "../ui/InfoTip";
import { SectionHeading } from "../ui/SectionHeading";
import { DetailDisclosure } from "../ui/DetailDisclosure";
import { EquityCurve } from "./EquityCurve";
import { CandidateScatter, EdgeByPriceChart } from "./SignalCharts";
import { ExitReasonBars, SidePerformanceList } from "./ExitPolicyCard";
import { OpenBook } from "./OpenBook";
import { MonitorLog } from "./MonitorLog";
import { LedgerTable } from "./LedgerTable";
import { DailyActivity } from "./DailyActivity";
import { percent, rejectionLabel } from "../../lib/lab-format";

function targetFeasibility(value: boolean | null | undefined) {
  if (value === true) return { label: "Feasible from current opportunities", color: "success" as const };
  if (value === false) return { label: "Not feasible from current opportunities", color: "warning" as const };
  return { label: "Feasibility unknown", color: "default" as const };
}

function objectiveDay(value: string | null | undefined) {
  if (!value) return null;
  const date = new Date(`${value}T12:00:00Z`);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "America/Los_Angeles",
  }).format(date);
}

/** The research account's fixed daily objective: today's progress plus how
    often the objective has actually been met. It reports feasibility, never a
    promise of return. */
export function DailyTargetEvidence({ target }: { target: ResearchDailyTarget }) {
  const targetPnl =
    target.target_pnl != null && Number.isFinite(target.target_pnl) && target.target_pnl > 0
      ? target.target_pnl
      : null;
  const realized =
    target.realized_pnl != null && Number.isFinite(target.realized_pnl)
      ? target.realized_pnl
      : 0;
  const remaining =
    target.remaining_pnl != null && Number.isFinite(target.remaining_pnl)
      ? target.remaining_pnl
      : targetPnl == null
        ? null
        : Math.max(targetPnl - realized, 0);
  const progressValue =
    targetPnl == null ? 0 : Math.min(targetPnl, Math.max(0, realized));
  const progress = targetPnl == null ? 0 : progressValue / targetPnl;
  const feasibility = targetFeasibility(target.target_feasible);
  const formattedDay = objectiveDay(target.objective_day);
  const disclaimer = /not a guaranteed return/i.test(target.disclaimer ?? "")
    ? target.disclaimer
    : "Hard paper-research objective; not a guaranteed return. Risk and edge gates remain binding.";

  return (
    <Widget>
      <Widget.Header className="flex-col items-start gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-1">
            <Widget.Title>Daily research objective</Widget.Title>
            <InfoTip label="the daily research objective" title="A research target, not a forecast">
              {disclaimer} Measured on the research account only{target.account_id ? ` (${target.account_id})` : ""}.
            </InfoTip>
          </div>
          <Widget.Description>
            {formattedDay ? `Objective day ${formattedDay}` : "Objective day unavailable"} · a research target, not a guaranteed return
          </Widget.Description>
        </div>
        <div className="flex flex-wrap gap-1.5">
          <Chip size="sm" variant="soft" color={target.achieved ? "success" : "default"}>
            <Chip.Label>{target.achieved ? "Hit" : "Not yet hit"}</Chip.Label>
          </Chip>
          <Chip size="sm" variant="soft" color={target.locked ? "warning" : "default"}>
            <Chip.Label>{target.locked ? "Target lock active" : "Target lock open"}</Chip.Label>
          </Chip>
          <Chip size="sm" variant="soft" color={feasibility.color}>
            <Chip.Label>{feasibility.label}</Chip.Label>
          </Chip>
        </div>
      </Widget.Header>
      <Widget.Content className="flex flex-col gap-5">
        <div>
          <p className="tnum text-2xl font-semibold tracking-tight text-foreground">
            {money(realized, { sign: "negative-only" })} of{" "}
            {targetPnl == null ? "objective unavailable" : money(targetPnl, { sign: "negative-only" })}
          </p>
          {targetPnl == null ? (
            <p role="status" className="mt-2 text-xs text-muted">Objective amount unavailable in this publication.</p>
          ) : (
            <div
              className="mt-3 h-1.5 overflow-hidden rounded-full bg-default"
              role="progressbar"
              aria-label="Daily research objective progress"
              aria-valuemin={0}
              aria-valuemax={targetPnl}
              aria-valuenow={progressValue}
            >
              <div className="bar-grow h-full rounded-full bg-accent" style={{ width: `${progress * 100}%` }} />
            </div>
          )}
        </div>
        <div className="grid grid-cols-2 gap-x-6 gap-y-4 md:grid-cols-3 xl:grid-cols-6">
          <Stat label="Remaining" value={money(remaining, { sign: "negative-only" })} />
          <Stat
            label="Mean / median"
            value={`${money(target.mean_daily_pnl)} / ${money(target.median_daily_pnl)}`}
          />
          <Stat label="Observed / hit days" value={`${target.observed_days ?? 0} / ${target.hit_count ?? 0}`} />
          <Stat label="Attainment rate" value={target.attainment_rate == null ? "—" : pct(target.attainment_rate, 1)} />
          <Stat label="Independent city-target days" value={`${target.independent_city_target_days ?? 0}`} />
          <Stat label="Resolution days" value={`${target.resolution_days ?? 0}`} />
        </div>
      </Widget.Content>
    </Widget>
  );
}

/** One account's detail: this week's P&L, open and closed positions, how trades
    closed, then signals and the monitor log behind disclosures. Everything is
    filtered to this account; nothing is summed across accounts. */
export function ProfileDashboard({ s, p }: { s: StrategyLab; p: ProfileEntry }) {
  const [closedPositionsExpanded, setClosedPositionsExpanded] = useState(false);
  const { strategy } = usePublication();
  const currentStateAvailable = strategy.state === "fresh";
  const rp = p.risk_profile;
  const sum = p.paper_trading?.summary;
  const resolved = s.paper_trading?.diagnostics?.by_profile?.[rp];
  const gate = profileGate(s, rp);
  const gateCount = profileGateCounts(gate);
  const countsProvenance = decisionCountProvenance(s);
  const target = researchDailyTarget(s, p);
  const charts = p.signal_quality?.charts;
  const days = p.daily_summary?.days;
  const week = p.daily_summary?.totals;
  const weekPnl = week?.realized_pnl;
  // `trades_closed` counts early exits only; settled positions are separate.
  const weekResolved = week?.wins != null && week?.losses != null
    ? (week.trades_closed ?? 0) + (week.trades_settled ?? 0) || week.wins + week.losses
    : null;
  const windowStart = p.daily_summary?.window_start ?? days?.[0]?.date;
  const windowEnd = p.daily_summary?.window_end ?? days?.at(-1)?.date;
  const windowLabel = windowStart && windowEnd ? dayRange(windowStart, windowEnd) : `${p.daily_summary?.window_days ?? 7}-day window`;
  const incidents = incidentsForDays(days);
  const rejections = (gate?.top_rejections_all?.length ? gate.top_rejections_all : gate?.top_rejections ?? []).slice(0, 5);
  const maxRejection = rejections[0]?.count ?? 1;

  const ledger = ledgerForProfile(s, rp);
  const byCity = ledgerByCity(ledger);
  const allTimeClosed = sum?.closed_positions ?? 0;
  const monitorRows = monitorForProfile(s, rp);

  return (
    <div className="flex min-w-0 flex-col gap-10">
      <section aria-labelledby={`week-title-${rp}`} className="min-w-0">
        <SectionHeading
          as="h3"
          id={`week-title-${rp}`}
          title="This week"
          sub={`${windowLabel} · ${currentStateAvailable ? p.status?.paper_trading_status ?? "status unavailable" : "Current status unavailable"}`}
        />
        <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(0,1.65fr)_minmax(0,1fr)]">
          {days?.length ? (
            <EquityCurve
              s={s}
              days={days}
              startingBankroll={0}
              windowDays={p.daily_summary?.window_days}
              title="Cumulative P&L"
              description={`Realized, ${windowLabel}; hover a day for the account balance`}
              contributionMode
              incidents={incidents}
            />
          ) : (
            <div role="status" className="grid min-h-48 place-items-center rounded-3xl bg-surface-secondary px-4 text-center">
              <div>
                <Icon icon="solar:chart-2-linear" className="mx-auto mb-2 size-5 text-muted" aria-hidden="true" />
                <p className="text-sm font-medium text-foreground">No daily history published for this account.</p>
              </div>
            </div>
          )}
          <Widget className="min-w-0">
            <Widget.Header>
              <Widget.Title>Account stats</Widget.Title>
            </Widget.Header>
            <Widget.Content className="grid grid-cols-2 content-start gap-x-6 gap-y-5">
              <Stat
                label="Week P&L"
                value={money(weekPnl)}
                tone={(weekPnl ?? 0) > 0 ? "pos" : (weekPnl ?? 0) < 0 ? "neg" : "default"}
                note={weekResolved != null ? `${weekResolved} resolved · ${week?.wins ?? 0}–${week?.losses ?? 0}` : undefined}
              />
              <Stat
                label="ROI · resolved"
                value={percent(sum?.roi, 1)}
                tone={(sum?.roi ?? 0) > 0 ? "pos" : (sum?.roi ?? 0) < 0 ? "neg" : "default"}
                note="all time"
                info={
                  <InfoTip label="ROI on resolved capital">
                    Realized P&amp;L divided by the capital of every position that has closed or settled, since the account opened.
                  </InfoTip>
                }
              />
              <Stat label="Capital resolved" value={money(resolved?.capital_resolved, { sign: "negative-only" })} note="all time" />
              <Stat
                label="Profitable exits"
                value={sum?.hit_rate == null ? "—" : pct(sum.hit_rate, 1)}
                note={sum?.win_count != null ? `${sum.win_count}–${sum.loss_count ?? 0} all time` : undefined}
                info={
                  <InfoTip label="profitable exits">
                    Any closed or settled position that realized a gain. Most exits are monitor take-profit or stop
                    closes, so this is an execution record, not a settlement hit rate.
                  </InfoTip>
                }
              />
              <Stat label="Open risk" value={currentStateAvailable ? money(sum?.open_risk ?? 0, { sign: "negative-only" }) : "Unavailable"} />
              <Stat label="Candidates now" value={currentStateAvailable ? `${p.status?.latest_signal_count ?? 0}` : "Unavailable"} />
            </Widget.Content>
          </Widget>
        </div>
      </section>

      {rp === "research-target" && target && <DailyTargetEvidence target={target} />}

      {/* Positions are primary account evidence: the first five rows stay
          visible and only the overflow folds. */}
      <section aria-labelledby={`positions-title-${rp}`} className="flex min-w-0 flex-col gap-6">
        <SectionHeading
          as="h3"
          id={`positions-title-${rp}`}
          title="Positions"
          sub={`${allTimeClosed.toLocaleString()} closed or settled since the account opened`}
        />
        <OpenBook s={s} profile={rp} />

        <div className="min-w-0">
          <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
            <h4 className="text-sm font-semibold text-foreground">Closed positions</h4>
            <p className="tnum text-xs text-muted">
              {ledger.length} published this month · showing {closedPositionsExpanded ? ledger.length : Math.min(ledger.length, 5)}
            </p>
          </div>
          <div className="overflow-hidden rounded-3xl bg-surface shadow-surface">
            <div id={`closed-positions-${rp}`}>
              <LedgerTable
                s={s}
                rows={ledger}
                limit={closedPositionsExpanded ? undefined : 5}
                detailed
                hideProfile
                emptyNote={`No closed positions were published for this account this month; ${allTimeClosed} have closed since it opened.`}
              />
            </div>
            {ledger.length > 5 && (
              <button
                type="button"
                aria-expanded={closedPositionsExpanded}
                aria-controls={`closed-positions-${rp}`}
                onClick={() => setClosedPositionsExpanded((expanded) => !expanded)}
                className="flex min-h-11 w-full items-center justify-center gap-1.5 border-t border-separator text-xs font-medium text-muted transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[color:var(--focus)] motion-reduce:transition-none"
              >
                {closedPositionsExpanded ? "Show fewer closed positions" : `Show ${ledger.length - 5} more closed positions`}
                <Icon
                  icon="solar:alt-arrow-down-linear"
                  className={`size-3.5 transition-transform duration-200 ${closedPositionsExpanded ? "rotate-180" : ""} motion-reduce:transition-none`}
                  aria-hidden="true"
                />
              </button>
            )}
          </div>
          {byCity.length > 0 && (
            <details className="mt-3">
              <summary className="flex min-h-11 cursor-pointer items-center gap-2 rounded-xl text-xs text-muted hover:text-foreground focus-visible:outline-2 focus-visible:outline-[color:var(--focus)]">
                <Icon icon="solar:map-point-linear" className="size-4" aria-hidden="true" />
                By settlement city <span className="tnum">· {byCity.length} cities</span>
                <Icon icon="solar:alt-arrow-down-linear" className="disclosure-chevron ml-auto size-4" aria-hidden="true" />
              </summary>
              <ul className="fade-in grid gap-x-6 gap-y-2 pb-2 pt-1 text-xs sm:grid-cols-2 lg:grid-cols-3" aria-label="Closed positions grouped by settlement city">
                {byCity.map((c) => (
                  <li key={c.slug} className="flex items-center justify-between gap-2 rounded-xl bg-surface-secondary px-3 py-2">
                    <span className="min-w-0 truncate font-medium text-foreground">{c.name}</span>
                    <span className="tnum shrink-0 text-muted">
                      {c.trades} · {c.wins}W ·{" "}
                      <span className={c.pnl > 0 ? "text-success" : c.pnl < 0 ? "text-danger" : "text-muted"}>{money(c.pnl)}</span>
                    </span>
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
      </section>

      <section aria-labelledby={`exits-title-${rp}`} className="min-w-0">
        <SectionHeading as="h3" id={`exits-title-${rp}`} title="How this week's trades closed" />
        <div className="grid min-w-0 gap-4 md:grid-cols-2">
          <Widget>
            <Widget.Header>
              <Widget.Title>Exit reasons</Widget.Title>
            </Widget.Header>
            <Widget.Content>
              <ExitReasonBars reasons={p.daily_summary?.exit_reasons} emptyNote="No exits this week." />
            </Widget.Content>
          </Widget>
          <Widget>
            <Widget.Header>
              <Widget.Title>By side</Widget.Title>
            </Widget.Header>
            <Widget.Content>
              <SidePerformanceList side={p.daily_summary?.side_performance} emptyNote="No resolved trades this week." />
            </Widget.Content>
          </Widget>
        </div>
      </section>

      {((gate && gateCount.signals > 0) || charts?.probability_vs_market?.length) ? (
        <DetailDisclosure
          id={`signal-evidence-${rp}`}
          icon="solar:chart-square-linear"
          title="Signals and gates"
          note={countsProvenance.cached ? `Candidate scatter is current · gate counts as of ${countsProvenance.asOf}` : "Candidate scatter and gate counts"}
        >
          {gate && gateCount.signals > 0 && (
            <div className="grid gap-x-8 gap-y-6 md:grid-cols-2">
              <div>
                <p className="text-xs text-muted">
                  {countsProvenance.cached ? `Gate approvals · as of ${countsProvenance.asOf}` : "Gate approvals · window"}
                </p>
                <p className="tnum mt-1 text-2xl font-semibold tracking-tight text-foreground">
                  {gateCount.approved.toLocaleString()}
                  <span className="text-sm font-normal text-muted"> of {gateCount.signals.toLocaleString()}</span>
                </p>
                <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-default">
                  <div className="bar-grow h-full rounded-full bg-accent" style={{ width: `${Math.max((gateCount.approved / gateCount.signals) * 100, 0.75)}%` }} />
                </div>
                <p className="mt-2 text-xs text-muted">
                  {pct(gateCount.approved / gateCount.signals, 2)} approved
                  {countsProvenance.cached
                    ? ` in the cached window ending ${countsProvenance.asOf}; not current-runtime totals.`
                    : " this window."}
                </p>
              </div>
              <div>
                <p className="mb-2 text-xs text-muted">Most common reasons to pass</p>
                <ul className="flex flex-col gap-2">
                  {rejections.map((r) => (
                    <li key={r.reason} className="grid grid-cols-[minmax(0,9rem)_1fr_auto] items-center gap-2.5">
                      <span className="min-w-0 text-xs text-foreground" title={r.reason}>{rejectionLabel(r.reason)}</span>
                      <div className="h-1 overflow-hidden rounded-full bg-default">
                        <div className="bar-grow h-full rounded-full bg-accent/70" style={{ width: `${Math.max((r.count / maxRejection) * 100, 2)}%` }} />
                      </div>
                      <span className="tnum text-xs text-muted">{r.count.toLocaleString()}</span>
                    </li>
                  ))}
                </ul>
                <p className="mt-2 text-xs text-muted">A candidate can fail several gates at once.</p>
              </div>
            </div>
          )}
          {charts?.probability_vs_market?.length ? (
            <div className="grid min-w-0 gap-4 lg:grid-cols-2">
              <CandidateScatter points={charts.probability_vs_market} targetDate={p.signal_quality?.latest_target_date} />
              {charts.edge_by_market_bucket?.length ? <EdgeByPriceChart buckets={charts.edge_by_market_bucket} /> : null}
            </div>
          ) : null}
        </DetailDisclosure>
      ) : null}

      {days?.length ? (
        <DetailDisclosure
          id={`daily-log-${rp}`}
          icon="solar:calendar-linear"
          title="Daily log"
          note={`Entries, exits and balance by day · ${windowLabel}`}
        >
          <DailyActivity s={s} days={days} />
        </DetailDisclosure>
      ) : null}

      {monitorRows.length > 0 && (
        <DetailDisclosure
          id={`execution-detail-${rp}`}
          icon="solar:history-linear"
          title="Monitor log"
          note="Every hold, take-profit and stop decision"
        >
          <MonitorLog s={s} rows={monitorRows} hideProfile />
        </DetailDisclosure>
      )}
    </div>
  );
}
