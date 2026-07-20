import { Icon } from "@iconify/react/offline";
import "../../styles/pro-methodology.css";
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
import { ForecastPipeline } from "../methodology/ForecastPipeline";
import { ClimatologyChart } from "../charts/ClimatologyChart";
import { HistogramChart } from "../charts/HistogramChart";
import { CalibrationChart } from "../charts/CalibrationChart";
import { CohortChart } from "../charts/CohortChart";
import { DetailDisclosure } from "../ui/DetailDisclosure";

function ModelProofFinding({ diag }: { diag: Diagnostics }) {
  const { models, ab } = diag;
  const persistLift = Math.round((1 - models.lstm.mae / models.persistence.mae) * 100);
  const pStr = ab.p_diebold_mariano < 0.001 ? "p < 0.001" : `p = ${ab.p_diebold_mariano.toFixed(3)}`;
  return (
    <Finding>
      This is a San Francisco flagship extra, not the universal method: on {ab.n_days.toLocaleString()} held-out
      days the LSTM's MAE of <strong>{round1(models.lstm.mae)}°F</strong> beats the naive persistence baseline (
      {round1(models.persistence.mae)}°F) by <strong>{persistLift}%</strong> and the XGBoost challenger by{" "}
      <strong>{round1(ab.lift_pct)}%</strong>, winning {pct(ab.win_rate, 0)} of days head-to-head. A Diebold–Mariano test
      puts that gap at <strong>{pStr}</strong>
      {ab.significant
        ? " — a statistically significant edge, not a lucky sample, which is why the LSTM holds the flagship's production slot on top of the shared EMOS pipeline."
        : " — not yet significant, so the A/B keeps running on the flagship before anyone is promoted."}
    </Finding>
  );
}

function AccuracyFinding({ data }: { data: DashboardData }) {
  const { forecast, signal } = data;
  const cal = signal.calibration;
  if (!cal) return null;
  const cohorts = cal.cohorts ?? [];
  const best = [...cohorts].sort((a, b) => b.ranked_probability_skill - a.ranked_probability_skill)[0];
  const worst = [...cohorts].sort((a, b) => a.ranked_probability_skill - b.ranked_probability_skill)[0];
  return (
    <Finding>
      This is San Francisco's ten-year track record. Across <strong>{cal.n.toLocaleString()}</strong> scored San Francisco
      outcomes the probability engine carries a{" "}
      <strong>{pct(cal.ranked_probability_skill, 0)} ranked-probability skill</strong> over climatology and calls the exact
      settlement bin {pct(cal.top_bin_accuracy, 0)} of the time — against roughly a dozen 2°F-wide brackets. The calibration
      curve above is the check on this: predicted probabilities track the observed frequencies rather than overstating them.
      {best && worst && best.name !== worst.name && (
        <>
          {" "}
          Skill varies by regime — strongest in the <strong>{cohortLabel(best.name)}</strong> cohort (
          {pct(best.ranked_probability_skill, 0)}) and weakest in <strong>{cohortLabel(worst.name)}</strong> (
          {pct(worst.ranked_probability_skill, 0)}), which the risk gates account for when sizing positions. All of it rests
          on {forecast.n_days_observed?.toLocaleString() ?? "—"} observed KSFO days across {forecast.n_years} years — and the other
          fourteen cities run the same EMOS post-processing against their own settlement stations, just without a decade of
          scored live outcomes behind them yet.
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
        headingId="methodology-page-title"
        icon="solar:graph-up-bold"
        eyebrow="Methodology & diagnostics"
        title="How the forecast is built and tested"
        sub="The production method is one pipeline in every city: a leakage-free nine-model NWP ensemble, EMOS-calibrated per station, settled on each city's own NWS Climatological Report. San Francisco layers flagship extras — an LSTM, a Google blend, and marine-layer features — on top of that shared base."
      />
      <div className="mx-auto w-full max-w-6xl px-5 pb-28 sm:px-8">
        <section className="scroll-mt-24">
          <SectionHeading
            index="01"
            eyebrow="The production pipeline"
            title="One method, running in every city"
            sub="A nine-model NWP ensemble pulled leakage-free from Open-Meteo previous-runs, post-processed per city with rolling-origin EMOS into a calibrated Gaussian, then settled against each city's own official NWS Climatological Report."
          />
          <ForecastPipeline />
        </section>

        <section className="scroll-mt-24">
          <SectionHeading
            index="02"
            eyebrow="Model proof"
            title="The flagship's LSTM, held out-of-sample"
            sub="A San Francisco flagship extra — not the shared method — compared against an XGBoost challenger and a naive persistence baseline on days neither model trained on. The other fourteen cities trade on the Tier 1 EMOS pipeline alone."
          />
          {diag ? (
            <div className="space-y-5">
              <ModelProofFinding diag={diag} />
              <Reveal>
                <DetailDisclosure
                  id="held-out-model-evidence"
                  icon="solar:chart-square-bold"
                  title="Held-out model evidence"
                  note="MAE comparison, feature importance, significance test, and observed-vs-predicted scatter"
                >
                  <div className="grid gap-5 lg:grid-cols-2">
                    <ModelCompareChart diag={diag} />
                    <FeatureImportanceChart diag={diag} />
                  </div>
                  <div className="grid gap-5 lg:grid-cols-2">
                    <ABSignificance diag={diag} />
                    <HeldOutScatter diag={diag} />
                  </div>
                </DetailDisclosure>
              </Reveal>
            </div>
          ) : diagError ? (
            <div role="alert" className="flex h-48 items-center justify-center text-sm text-muted">
              Couldn't load diagnostics — {diagError}
            </div>
          ) : (
            <div role="status" aria-live="polite" className="flex h-48 items-center justify-center gap-2 text-muted">
              <Icon icon="solar:refresh-bold" className="size-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
              <span className="text-sm">Loading diagnostics…</span>
            </div>
          )}
        </section>

        <section className="scroll-mt-24">
          <SectionHeading
            index="03"
            eyebrow="Forecast accuracy"
            title="Ten years of San Francisco accuracy"
            sub={`${forecast.n_days_observed?.toLocaleString() ?? "—"} observed days across ${forecast.n_years} years anchor San Francisco's climatology, post-processing, and calibration — each of the other fourteen cities runs the same EMOS post-processing against its own settlement station, just without a decade of scored outcomes behind it yet.`}
          />
          <AccuracyFinding data={data} />
          <Reveal className="mt-5">
            <DetailDisclosure
              id="accuracy-evidence"
              icon="solar:graph-up-bold"
              title="Ten-year accuracy evidence"
              note="Climatology, observed distribution, calibration curve, and performance by temperature regime"
            >
              <ClimatologyChart forecast={forecast} />
              <div className="grid gap-5 lg:grid-cols-2">
                <HistogramChart story={story} forecast={forecast} />
                <CalibrationChart signal={signal} />
              </div>
              <CohortChart signal={signal} />
            </DetailDisclosure>
          </Reveal>
        </section>
      </div>
    </>
  );
}
