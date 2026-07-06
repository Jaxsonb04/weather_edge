import { f1, pct, targetLabel, type DashboardData } from "../../lib/data";
import { Hero } from "../hero/Hero";
import { SkillStrip } from "../kpi/SkillStrip";
import { PipelineStepper } from "../pipeline/PipelineStepper";
import { ForecastInputs } from "../market/ForecastInputs";
import { DecisionCard } from "../market/DecisionCard";
import { EdgeChart } from "../market/EdgeChart";
import { MarketBook } from "../market/MarketBook";
import { SystemHighlights } from "../overview/SystemHighlights";
import { SectionHeading } from "../ui/SectionHeading";
import { Finding } from "../ui/Finding";
import { Reveal } from "../ui/Reveal";

export function OverviewView({ data }: { data: DashboardData }) {
  const { forecast, signal } = data;
  if (!signal.targets.length) {
    return <div className="grid min-h-[60vh] place-items-center text-sm text-muted">No active forecast targets right now.</div>;
  }
  const today = signal.targets[0];
  const mc = today.market_consensus;

  return (
    <>
      <Hero targets={signal.targets} />
      <main className="mx-auto w-full max-w-6xl px-5 pb-28 sm:px-8">
        <SkillStrip forecast={forecast} signal={signal} />

        <section id="today" className="scroll-mt-24">
          <SectionHeading
            index="01"
            eyebrow="Today's call"
            title="The engine prices every bracket — and shows its work"
            sub="Forecast distribution → prediction-market bin probabilities → fee- and liquidity-aware edge, gated before any (paper) order."
          />
          <PipelineStepper />
          <div className="mt-5 grid gap-5 lg:grid-cols-[1.02fr_0.98fr]">
            <Reveal delay={0.05}>
              <ForecastInputs target={today} />
            </Reveal>
            <Reveal delay={0.1}>
              <DecisionCard target={today} approvedCount={signal.summary.approved_signal_count} />
            </Reveal>
          </div>
        </section>

        <section id="edge" className="scroll-mt-24">
          <SectionHeading
            index="02"
            eyebrow="Model vs market"
            title="Where the engine sees a mispricing"
            sub={`The model's bin distribution overlaid on the market-implied one for ${targetLabel(today.target_date).toLowerCase()} — the gap is the edge, before fees and gates.`}
          />
          <Reveal>
            <EdgeChart target={today} />
          </Reveal>
          {mc?.available && (
            <Finding>
              Right now the model reads the {targetLabel(today.target_date).toLowerCase()} high at{" "}
              <strong>{f1(mc.model_high_f)}</strong> while the market implies <strong>{f1(mc.implied_high_f)}</strong> — a{" "}
              <strong>{mc.model_minus_market_f > 0 ? "+" : ""}{Math.round(mc.model_minus_market_f * 10) / 10}°F</strong> disagreement.
              The crowd's favorite bracket is <strong>{mc.modal_bin_label}</strong> at {pct(mc.modal_probability, 0)}, and the
              book carries a {pct(mc.overround, 1)} overround — the tax any edge must beat before it is real. The engine
              approved <strong>{signal.summary.approved_signal_count}</strong> signal
              {signal.summary.approved_signal_count === 1 ? "" : "s"} on the latest scan; when the gap doesn't clear fees and
              gates, standing down is the correct trade.
            </Finding>
          )}
        </section>

        <section id="book" className="scroll-mt-24">
          <SectionHeading
            index="03"
            eyebrow="Market book"
            title={`Every active bracket · ${targetLabel(today.target_date)}`}
            sub={today.event_title}
          />
          <Reveal>
            <MarketBook target={today} />
          </Reveal>
        </section>

        <section id="system" className="scroll-mt-24">
          <SectionHeading
            index="04"
            eyebrow="Under the hood"
            title="A trading system, not a script"
            sub="What's actually running behind this page: a forecasting stack, a market microstructure engine, and the production discipline that keeps it honest."
          />
          <Reveal>
            <SystemHighlights />
          </Reveal>
        </section>
      </main>
    </>
  );
}
