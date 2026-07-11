import { Icon } from "@iconify/react/offline";
import "../../styles/pro-strategy.css";
import { Chip } from "@heroui/react/chip";
import { pct } from "../../lib/data";
import {
  findProfile,
  gateCounts,
  money,
  openForProfile,
  pendingForProfile,
  useStrategyLab,
  type ProfileEntry,
  type ProfilePaperSummary,
  type StrategyLab,
} from "../../lib/strategy";
import { PageHeader } from "../ui/PageHeader";
import { SectionHeading } from "../ui/SectionHeading";
import { Reveal } from "../ui/Reveal";
import { Finding } from "../ui/Finding";
import { PnlHeader } from "../strategy/PnlHeader";
import { EquityCurve } from "../strategy/EquityCurve";
import { ReadinessVerdict, ReadinessPanel } from "../strategy/ReadinessPanel";
import { ProfileComparison } from "../strategy/ProfileComparison";
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

function TrackRecordFinding({ s }: { s: StrategyLab }) {
  const t = s.daily_summary?.totals;
  const side = s.daily_summary?.side_performance;
  if (!t) return null;
  const no = side?.NO;
  const yes = side?.YES;
  return (
    <Finding>
      Over the {s.daily_summary.window_days ?? "recent"}-day window the combined book realized{" "}
      <strong>{money(t.cumulative_realized_pnl)}</strong> ({t.roi != null ? pct(t.roi, 1) : "—"} ROI on resolved capital) at a{" "}
      <strong>{pct(t.hit_rate, 0)} hit rate</strong> — many small wins and a few larger losses.
      {no && yes && (
        <>
          {" "}
          The losses are concentrated on one side: NO positions netted <strong>{money(no.realized_pnl)}</strong> across {no.trades} trades,
          while the {yes.trades} YES trade{yes.trades === 1 ? "" : "s"} returned <strong>{money(yes.realized_pnl)}</strong> — the same
          split the books flag in their recommended changes.
        </>
      )}
    </Finding>
  );
}

function SelectivityFinding({ s }: { s: StrategyLab }) {
  const gate = s.daily_summary?.gate_behavior;
  if (!gate) return null;
  const { approved, total } = gateCounts(gate);
  const live = gate.by_profile?.find((g) => g.risk_profile === "live");
  const liveTop = live?.top_rejections?.[0];
  return (
    <Finding>
      Of <strong>{total.toLocaleString()}</strong> gate evaluations this window only{" "}
      <strong>{approved.toLocaleString()}</strong> ({pct(total ? approved / total : 0, 2)}) survived. The live book
      approved {live?.approved ?? 0} of {live?.signals?.toLocaleString() ?? "—"}
      {liveTop && (
        <>
          {" "}
          — its most common rejection is <strong>{liveTop.reason}</strong> ({liveTop.count.toLocaleString()} rejections), where it
          holds off because the forecast sources disagree
        </>
      )}
      . The strategy is built on being selective, not on trade volume.
    </Finding>
  );
}

function ReadinessFinding({ s }: { s: StrategyLab }) {
  const r = s.real_money_readiness;
  if (!r?.available) return null;
  const total = r.checks_total ?? r.checks?.length ?? 0;
  return (
    <Finding>
      Today the engine scores itself <strong>{r.checks_passed ?? 0}/{total} checks passed</strong> —{" "}
      {(r.verdict ?? "not ready").toLowerCase()} for real money. The open items are mostly about sample size: it won't treat a
      good week as proof until it has enough independent settlement days. The go/no-go decision is enforced in code and
      published as-is, not set by hand.
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
    <span className="flex items-center gap-2" title={paused ? reason : undefined}>
      <span className="text-xs font-medium text-foreground">{label}</span>
      <Chip size="sm" variant="soft" color={paused ? "default" : "success"}>
        <Chip.Label>{paused ? "Entry paused" : "Scanning"}</Chip.Label>
      </Chip>
      <span className="tnum text-xs text-muted">
        {open} open · {pending} resting
      </span>
    </span>
  );
}

/** Live trading-status strip: a real-time snapshot of the running engine — the
    heartbeat, each book's entry state and current book, and the paper-only
    disclaimer folded in as a muted note (replaces the old warning band). */
function LiveStatusStrip({ s }: { s: StrategyLab }) {
  const fresh = s.generated_at ? `${s.generated_at.slice(0, 16).replace("T", " ")} UTC` : null;
  const disclaimer =
    s.disclaimer ?? "Paper-trading research only — no real-money orders are ever placed.";
  return (
    <div className="mb-6 rounded-xl bg-surface-secondary px-4 py-3 ring-1 ring-border/60">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2.5">
        <span className="flex items-center gap-2">
          <span className="relative flex size-2" aria-hidden="true">
            <span className="absolute inline-flex size-full animate-ping rounded-full bg-success opacity-60 motion-reduce:animate-none" />
            <span className="relative inline-flex size-2 rounded-full bg-success" />
          </span>
          <span className="text-xs font-semibold text-foreground">Paper engine live</span>
        </span>
        <BookState s={s} rp="live" label="Live candidate" />
        <BookState s={s} rp="research" label="Research" />
        {fresh && <span className="ml-auto font-mono text-[11px] text-muted">updated {fresh}</span>}
      </div>
      <p className="mt-2 flex items-center gap-1.5 text-[11px] leading-relaxed text-muted">
        <Icon icon="solar:shield-keyhole-bold" className="size-3.5 shrink-0" aria-hidden="true" />
        {disclaimer}
      </p>
    </div>
  );
}

/** The live candidate's own performance, surfaced as the section headline so the
    real-money book is judged on its own record — not the blended, research-dragged
    combined figure that leads the KPI strip. */
function LiveHero({ p, sum }: { p: ProfileEntry; sum: ProfilePaperSummary }) {
  const pnl = sum.realized_pnl ?? 0;
  const up = pnl >= 0;
  const win = p.daily_summary?.window_days ?? 7;
  return (
    <div className="rounded-2xl bg-accent-soft p-4 ring-1 ring-accent/30 sm:p-5">
      <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-4">
        <div className="min-w-0">
          <p className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-accent">
            <Icon icon="solar:shield-check-bold" className="size-3.5 shrink-0" aria-hidden="true" />
            Live candidate · real-money profile
          </p>
          <div className="mt-1.5 flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
            <span className={`font-display text-4xl font-bold tracking-tight ${up ? "text-success" : "text-danger"}`}>
              {money(pnl)}
            </span>
            <span className="text-xs text-muted">attributed realized P&amp;L · {win}-day window</span>
          </div>
        </div>
        <dl className="flex flex-wrap gap-x-7 gap-y-3">
          <HeroStat label="ROI · resolved" value={sum.roi == null ? "—" : pct(sum.roi, 1)} tone={(sum.roi ?? 0) > 0 ? "pos" : (sum.roi ?? 0) < 0 ? "neg" : undefined} />
          <HeroStat label="Hit rate" value={sum.hit_rate == null ? "—" : pct(sum.hit_rate, 1)} />
          <HeroStat label="Resolved" value={`${sum.closed_positions ?? 0} · ${sum.win_count ?? 0}–${sum.loss_count ?? 0}`} />
        </dl>
      </div>
    </div>
  );
}

/** Overview equity block: LIVE leads (hero stats + its own curve), RESEARCH follows on
    a separate, visually secondary curve. The two books' P&L never share a line. */
function OverviewEquity({ s }: { s: StrategyLab }) {
  const live = findProfile(s, "live");
  const research = findProfile(s, "research");
  const liveDays = live?.daily_summary?.days;
  const researchDays = research?.daily_summary?.days;
  const liveSum = live?.paper_trading?.summary;
  const readinessAvailable = !!s.real_money_readiness?.available;

  // Fall back to the combined curve only if the per-book series is missing.
  const liveCurve =
    live && liveDays?.length ? (
      <EquityCurve
        s={s}
        days={liveDays}
        startingBankroll={0}
        contributionMode
        windowDays={live.daily_summary?.window_days}
        emphasis="headline"
        eyebrow="Live candidate · real-money profile"
        title="Live candidate — cumulative P&L"
        description={`Realized P&L attributed to the live book · ${live.daily_summary?.window_days ?? liveDays.length}-day view`}
      />
    ) : (
      <EquityCurve s={s} emphasis="headline" eyebrow="Live candidate · real-money profile" title="Live candidate — cumulative P&L" />
    );

  return (
    <div className="space-y-4">
      {live && liveSum && <LiveHero p={live} sum={liveSum} />}
      {readinessAvailable ? (
        <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
          {liveCurve}
          <ReadinessVerdict s={s} />
        </div>
      ) : (
        liveCurve
      )}
      {research && !!researchDays?.length && (
        <EquityCurve
          s={s}
          days={researchDays}
          startingBankroll={0}
          contributionMode
          windowDays={research.daily_summary?.window_days}
          emphasis="secondary"
          eyebrow="Experimental book · tracked separately"
          title="Research — cumulative P&L"
          description={`Realized P&L attributed to the experimental book · ${research.daily_summary?.window_days ?? researchDays.length}-day view`}
        />
      )}
    </div>
  );
}

export default function StrategyLabView() {
  const { data: s, error } = useStrategyLab();

  return (
    <>
      <PageHeader
        headingId="lab-page-title"
        icon="solar:test-tube-bold"
        eyebrow="Strategy Lab"
        title="Paper-trading results"
        sub="Two risk profiles run in parallel on the same signals: a strict real-money candidate and a looser experimental book. Below is each book's performance, the filters that reject most signals, per-book signal quality and exits, and the checklist the system must pass before any real capital is allowed. Generated directly by the AWS runtime."
      />
      <div className="mx-auto w-full max-w-6xl px-5 pb-28 pt-10 sm:px-8">
        <StrategyPublicationNotice generatedAt={s?.generated_at} />
        {error && <div role="alert" className="grid h-48 place-items-center text-sm text-muted">Couldn't load the Strategy Lab — {error}</div>}
        {!error && !s && (
          <div role="status" aria-live="polite" className="flex h-48 items-center justify-center gap-2 text-muted">
            <Icon icon="solar:refresh-bold" className="size-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
            <span className="text-sm">Loading paper-trading research…</span>
          </div>
        )}
        {s && (
          <>
            <Reveal immediate>
              <LiveStatusStrip s={s} />
            </Reveal>

            {/* ---- Book overview: live candidate leads, research separate, combined for context ---- */}
            <section className="scroll-mt-24">
              <SectionHeading
                index="01"
                eyebrow="Book overview"
                title="Live candidate performance"
                sub="The real-money candidate is shown first, on its own equity curve. The experimental book follows on a separate curve — the two books' P&L are never combined. The shared-account totals appear below for reference."
              />
              <Reveal>
                <OverviewEquity s={s} />
              </Reveal>
              <div className="mt-7">
                <p className="mb-2 flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-muted">
                  <Icon icon="solar:wallet-money-bold" className="size-3.5 text-accent" aria-hidden="true" />
                  Shared paper account · both books combined
                </p>
                <PnlHeader s={s} />
              </div>
              <Reveal className="mt-5">
                <ProfileComparison s={s} />
              </Reveal>
              <TrackRecordFinding s={s} />
              <Reveal className="mt-5">
                <GateFunnel s={s} />
              </Reveal>
              <SelectivityFinding s={s} />
              <Reveal className="mt-5">
                <MoversCard s={s} />
              </Reveal>
            </section>

            {/* ---- Per-book diagnostics: one book at a time, in full ---- */}
            <section className="scroll-mt-24">
              <SectionHeading
                index="02"
                eyebrow="Per-book diagnostics"
                title="Each book in detail"
                sub="Switch between the live candidate and the research book. Each one shows its own equity, filtering, signal quality, exits, notes, current exposure, and closed trades."
              />
              <Reveal>
                <ProfileExplorer s={s} />
              </Reveal>
            </section>

            {/* ---- System-level governance & operations (combined-fidelity) ---- */}
            <section className="scroll-mt-24">
              <SectionHeading
                index="03"
                eyebrow="Governance & operations"
                title="System-level checks"
                sub="Diagnostics that cover the whole system: the full go-live checklist, the active-vs-challenger calibration comparison, the unattended runtime and feed health, and the backtest coverage behind the metrics."
              />
              <Reveal>
                <ReadinessPanel s={s} />
              </Reveal>
              <ReadinessFinding s={s} />
              <Reveal className="mt-5">
                <CalibrationCompare s={s} />
              </Reveal>
              <Reveal className="mt-5">
                <OpsHealth s={s} />
              </Reveal>
              <Reveal className="mt-5">
                <ExitPolicyCard s={s} />
              </Reveal>
              <Reveal className="mt-5">
                <BacktestStats s={s} />
              </Reveal>
              <Reveal className="mt-5">
                <DailyActivity s={s} />
              </Reveal>
              <Reveal className="mt-5">
                <ResearchNotes s={s} />
              </Reveal>
            </section>
          </>
        )}
      </div>
    </>
  );
}
