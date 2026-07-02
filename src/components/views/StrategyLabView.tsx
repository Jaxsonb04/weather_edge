import { Icon } from "@iconify/react";
import { pct } from "../../lib/data";
import { money, useStrategyLab, type StrategyLab } from "../../lib/strategy";
import { PageHeader } from "../ui/PageHeader";
import { SectionHeading } from "../ui/SectionHeading";
import { Reveal } from "../ui/Reveal";
import { Finding } from "../ui/Finding";
import { PnlHeader } from "../strategy/PnlHeader";
import { EquityCurve } from "../strategy/EquityCurve";
import { TradesTable } from "../strategy/TradesTable";
import { MoversCard } from "../strategy/MoversCard";
import { Learnings } from "../strategy/Learnings";
import { BacktestStats } from "../strategy/BacktestStats";
import { ResearchNotes } from "../strategy/ResearchNotes";
import { ProfilesPanel } from "../strategy/ProfilesPanel";
import { GateFunnel } from "../strategy/GateFunnel";
import { SignalQualityPanel } from "../strategy/SignalQualityPanel";
import { CalibrationCompare } from "../strategy/CalibrationCompare";
import { ReadinessPanel } from "../strategy/ReadinessPanel";
import { OpsHealth } from "../strategy/OpsHealth";
import { ExitPolicyCard } from "../strategy/ExitPolicyCard";

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
      <strong>{pct(t.hit_rate, 0)} hit rate</strong> — frequent small wins, occasional larger losses.
      {no && yes && (
        <>
          {" "}
          The damage is one-sided: NO positions netted <strong>{money(no.realized_pnl)}</strong> across {no.trades} trades while
          the {yes.trades} YES trade{yes.trades === 1 ? "" : "s"} returned <strong>{money(yes.realized_pnl)}</strong> — exactly
          the asymmetry the engine calls out in its own recommended changes below.
        </>
      )}
    </Finding>
  );
}

function GauntletFinding({ s }: { s: StrategyLab }) {
  const gate = s.daily_summary?.gate_behavior;
  if (!gate) return null;
  const total = gate.approved + gate.rejected;
  const live = gate.by_profile?.find((g) => g.risk_profile === "live");
  const liveTop = live?.top_rejections?.[0];
  return (
    <Finding>
      Of <strong>{total.toLocaleString()}</strong> gate evaluations this window only{" "}
      <strong>{gate.approved.toLocaleString()}</strong> ({pct(total ? gate.approved / total : 0, 2)}) survived. The live book
      approved {live?.approved ?? 0} of {live?.signals.toLocaleString() ?? "—"}
      {liveTop && (
        <>
          {" "}
          — its dominant blocker is <strong>{liveTop.reason}</strong> ({liveTop.count.toLocaleString()} rejections), a deliberate
          stand-down rule when forecast sources disagree
        </>
      )}
      . Selectivity, not activity, is the strategy.
    </Finding>
  );
}

function ReadinessFinding({ s }: { s: StrategyLab }) {
  const r = s.real_money_readiness;
  if (!r?.available) return null;
  return (
    <Finding>
      Today the engine scores itself <strong>{r.checks_passed ?? 0}/{r.checks_total ?? 6} checks passed</strong> —{" "}
      {(r.verdict ?? "not ready").toLowerCase()} for real money. The blockers are sample-size honesty: it refuses to count a
      hot week as proof until it has enough independent settlement days. The go/no-go decision is enforced in code and
      published unedited, not decided by feel.
    </Finding>
  );
}

export default function StrategyLabView() {
  const { data: s, error } = useStrategyLab();

  return (
    <>
      <PageHeader
        icon="solar:test-tube-bold"
        eyebrow="Strategy Lab"
        title="The paper book, with nothing hidden"
        sub="Two isolated risk profiles, every closed position, the gate funnel that rejects 99% of signals, and the go-live checklist the engine has to pass before real money is even possible — published straight from the AWS runtime."
      />
      <main className="mx-auto w-full max-w-6xl px-5 pb-28 sm:px-8 pt-10">
        {error && (
          <div role="alert" className="grid h-48 place-items-center text-sm text-muted">Could not load the lab — {error}</div>
        )}
        {!error && !s && (
          <div role="status" aria-live="polite" className="flex h-48 items-center justify-center gap-2 text-muted">
            <Icon icon="solar:refresh-linear" className="size-4 animate-spin" aria-hidden="true" />
            <span className="text-sm">Loading paper-trading research…</span>
          </div>
        )}
        {s && (
          <>
            <Reveal immediate className="mb-6 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-xl bg-warning-soft px-4 py-2.5 text-sm text-foreground ring-1 ring-warning/25">
              <span className="flex items-center gap-2">
                <Icon icon="solar:shield-keyhole-bold" className="size-4 shrink-0 text-warning" />
                <span>{s.disclaimer ?? "Paper-trading research only — no live orders are ever placed."}</span>
              </span>
              {s.generated_at && (
                <span className="font-mono text-[11px] text-muted">refreshed {s.generated_at.slice(0, 16).replace("T", " ")} UTC</span>
              )}
            </Reveal>

            <PnlHeader s={s} />

            <section className="scroll-mt-24">
              <SectionHeading index="01" eyebrow="Track record" title="Paper equity over the window" sub="Cumulative realized P&L against the starting bankroll — both books combined." />
              <Reveal>
                <EquityCurve s={s} />
              </Reveal>
              <TrackRecordFinding s={s} />
              <Reveal className="mt-5">
                <MoversCard s={s} />
              </Reveal>
            </section>

            <section className="scroll-mt-24">
              <SectionHeading
                index="02"
                eyebrow="Risk profiles"
                title="Two books, one engine"
                sub="The same signals flow through two isolated risk profiles: a strict real-money candidate and a loose experimental collector. Comparing them is the experiment."
              />
              <Reveal>
                <ProfilesPanel s={s} />
              </Reveal>
              <Finding>
                The headline P&L is dominated by the <strong>research</strong> book, which intentionally trades marginal
                signals at $1–5 stakes to buy data. The <strong>live</strong> candidate book trades the same engine with
                real-money gates and has barely resolved any positions — by design it stays statistically quiet until its
                stricter rules find genuinely clean setups.
              </Finding>
            </section>

            <section className="scroll-mt-24">
              <SectionHeading index="03" eyebrow="Ledger" title="Recent closed positions" sub="The most recent settled paper trades, newest first, tagged by book." />
              <Reveal>
                <TradesTable s={s} />
              </Reveal>
            </section>

            <section className="scroll-mt-24">
              <SectionHeading
                index="04"
                eyebrow="Signal pipeline"
                title="From 46,000 scans to a handful of trades"
                sub="Every 15 minutes the AWS runtime re-prices every bracket and side, then the gate stack rejects nearly everything."
              />
              <Reveal className="mb-5">
                <GateFunnel s={s} />
              </Reveal>
              <Reveal>
                <SignalQualityPanel s={s} />
              </Reveal>
              <GauntletFinding s={s} />
            </section>

            <section className="scroll-mt-24">
              <SectionHeading
                index="05"
                eyebrow="Self-critique"
                title="What the window taught the engine"
                sub="Auto-generated learnings, the changes the strategy recommends to itself, and the champion/challenger rule that decides which calibration is allowed to execute."
              />
              <Reveal>
                <Learnings s={s} />
              </Reveal>
              <Reveal className="mt-5">
                <CalibrationCompare s={s} />
              </Reveal>
            </section>

            <section className="scroll-mt-24">
              <SectionHeading
                index="06"
                eyebrow="Go-live gate"
                title="Would you trade real money with this? The engine says no."
                sub="A six-check readiness gate recomputed on every refresh. Until all six pass, live orders stay disabled in code."
              />
              <Reveal>
                <ReadinessPanel s={s} />
              </Reveal>
              <ReadinessFinding s={s} />
            </section>

            <section className="scroll-mt-24">
              <SectionHeading
                index="07"
                eyebrow="Operations"
                title="The machine that runs itself"
                sub="Unattended AWS timers, tens of thousands of snapshots, monitored exits, and health checks on every upstream feed."
              />
              <Reveal className="mb-5">
                <OpsHealth s={s} />
              </Reveal>
              <Reveal>
                <ExitPolicyCard s={s} />
              </Reveal>
            </section>

            <section className="scroll-mt-24">
              <SectionHeading index="08" eyebrow="Backtest" title="From raw scans to approved trades" sub="The dedup funnel behind the metrics, plus a glossary for reading them honestly." />
              <Reveal className="mb-5">
                <BacktestStats s={s} />
              </Reveal>
              <Reveal>
                <ResearchNotes s={s} />
              </Reveal>
            </section>
          </>
        )}
      </main>
    </>
  );
}
