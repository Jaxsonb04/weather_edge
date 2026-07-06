import { Icon } from "@iconify/react";
import { pct, round1, type DashboardData } from "../../lib/data";
import { useDiagnostics, type Diagnostics } from "../../lib/diagnostics";
import { PageHeader } from "../ui/PageHeader";
import { SectionHeading } from "../ui/SectionHeading";
import { Finding } from "../ui/Finding";
import { Reveal } from "../ui/Reveal";
import { ModelCompareChart } from "../charts/ModelCompareChart";
import { FeatureImportanceChart } from "../charts/FeatureImportanceChart";
import { HeldOutScatter } from "../charts/HeldOutScatter";
import { ABSignificance } from "../methodology/ABSignificance";
import { ClimatologyChart } from "../charts/ClimatologyChart";
import { HistogramChart } from "../charts/HistogramChart";
import { CalibrationChart } from "../charts/CalibrationChart";
import { CohortChart } from "../charts/CohortChart";

function ModelProofFinding({ diag }: { diag: Diagnostics }) {
  const { models, ab } = diag;
  const persistLift = Math.round((1 - models.lstm.mae / models.persistence.mae) * 100);
  const pStr = ab.p_diebold_mariano < 0.001 ? "p < 0.001" : `p = ${ab.p_diebold_mariano.toFixed(3)}`;
  return (
    <Finding>
      On {ab.n_days.toLocaleString()} held-out days the production LSTM's MAE of{" "}
      <strong>{round1(models.lstm.mae)}°F</strong> beats the naive persistence baseline (
      {round1(models.persistence.mae)}°F) by <strong>{persistLift}%</strong> and the XGBoost challenger by{" "}
      <strong>{round1(ab.lift_pct)}%</strong>, winning {pct(ab.win_rate, 0)} of days head-to-head. A Diebold–Mariano test
      puts that gap at <strong>{pStr}</strong>
      {ab.significant
        ? " — a statistically significant edge, not a lucky sample, which is why the LSTM holds the production slot."
        : " — not yet significant, so the A/B keeps running before anyone is promoted."}
    </Finding>
  );
}

function AccuracyFinding({ data }: { data: DashboardData }) {
  const { forecast, signal } = data;
  const cal = signal.calibration;
  const cohorts = cal.cohorts ?? [];
  const best = [...cohorts].sort((a, b) => b.ranked_probability_skill - a.ranked_probability_skill)[0];
  const worst = [...cohorts].sort((a, b) => a.ranked_probability_skill - b.ranked_probability_skill)[0];
  return (
    <Finding>
      Across <strong>{cal.n.toLocaleString()}</strong> scored outcomes the probability engine carries a{" "}
      <strong>{pct(cal.ranked_probability_skill, 0)} ranked-probability skill</strong> over climatology and calls the exact
      settlement bin {pct(cal.top_bin_accuracy, 0)} of the time — against roughly a dozen 2°F-wide brackets. The calibration
      curve above is the honesty check: predicted probabilities track observed frequencies instead of overclaiming.
      {best && worst && best.name !== worst.name && (
        <>
          {" "}
          Skill is regime-dependent — sharpest in the <strong>{cohortLabel(best.name)}</strong> cohort (
          {pct(best.ranked_probability_skill, 0)}) and most humbled in <strong>{cohortLabel(worst.name)}</strong> (
          {pct(worst.ranked_probability_skill, 0)}), knowledge the risk gates use when sizing anything at all. All of it rests
          on {forecast.n_days_observed.toLocaleString()} observed KSFO days across {forecast.n_years} years.
        </>
      )}
    </Finding>
  );
}

const COHORT_LABELS: Record<string, string> = {
  cold_below_60f: "cold (<60°)",
  normal_60_69f: "normal (60–69°)",
  warm_70_79f: "warm (70–79°)",
  hot_80f_plus: "hot (80°+)",
};
const cohortLabel = (name: string) => COHORT_LABELS[name] ?? name.replace(/_/g, " ");

export default function MethodologyView({ data }: { data: DashboardData }) {
  const { forecast, story, signal } = data;
  const { data: diag, error: diagError } = useDiagnostics();

  return (
    <>
      <PageHeader
        icon="solar:graph-up-bold"
        eyebrow="Methodology & diagnostics"
        title="How the forecast earns its trust"
        sub="A decade of KSFO observations, two models held out-of-sample, and the calibration that turns a temperature distribution into honest probabilities."
      />
      <main className="mx-auto w-full max-w-6xl px-5 pb-28 sm:px-8">
        <section className="scroll-mt-24">
          <SectionHeading
            index="01"
            eyebrow="Model proof"
            title="LSTM in production, held out-of-sample"
            sub="Compared against an XGBoost challenger and a naive persistence baseline on days neither model trained on."
          />
          {diag ? (
            <div className="space-y-5">
              <div className="grid gap-5 lg:grid-cols-2">
                <Reveal delay={0.04}>
                  <ModelCompareChart diag={diag} />
                </Reveal>
                <Reveal delay={0.08}>
                  <FeatureImportanceChart diag={diag} />
                </Reveal>
              </div>
              <div className="grid gap-5 lg:grid-cols-2">
                <Reveal delay={0.04}>
                  <ABSignificance diag={diag} />
                </Reveal>
                <Reveal delay={0.08}>
                  <HeldOutScatter diag={diag} />
                </Reveal>
              </div>
              <ModelProofFinding diag={diag} />
            </div>
          ) : diagError ? (
            <div role="alert" className="flex h-48 items-center justify-center text-sm text-muted">
              Couldn't load diagnostics — {diagError}
            </div>
          ) : (
            <div role="status" aria-live="polite" className="flex h-48 items-center justify-center gap-2 text-muted">
              <Icon icon="solar:refresh-linear" className="size-4 animate-spin" aria-hidden="true" />
              <span className="text-sm">Loading diagnostics…</span>
            </div>
          )}
        </section>

        <section className="scroll-mt-24">
          <SectionHeading
            index="02"
            eyebrow="Forecast accuracy"
            title="Ten years of KSFO, distilled"
            sub={`${forecast.n_days_observed.toLocaleString()} observed days across ${forecast.n_years} years anchor the climatology, post-processing, and calibration.`}
          />
          <Reveal className="mb-5">
            <ClimatologyChart forecast={forecast} />
          </Reveal>
          <div className="grid gap-5 lg:grid-cols-2">
            <Reveal delay={0.05}>
              <HistogramChart story={story} forecast={forecast} />
            </Reveal>
            <Reveal delay={0.1}>
              <CalibrationChart signal={signal} />
            </Reveal>
          </div>
          <Reveal className="mt-5">
            <CohortChart signal={signal} />
          </Reveal>
          <AccuracyFinding data={data} />
        </section>
      </main>
    </>
  );
}
