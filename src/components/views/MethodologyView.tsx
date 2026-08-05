import { Icon } from "@iconify/react/offline";
import "../../styles/pro-methodology.css";
import { pct, round1, useCitiesData, type DashboardData } from "../../lib/data";
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
  const f2 = (value: number) => value.toFixed(2);
  return (
    <Finding>
      This is SFO residual-model research, not the universal method. Across <strong>{ab.n_days.toLocaleString()}</strong> paired held-out days,
      LSTM MAE was <strong>{f2(ab.mae_lstm)}°F</strong> versus XGBoost at <strong>{f2(ab.mae_xgb)}°F</strong>,
      a <strong>{round1(ab.lift_pct)}%</strong> reduction; the LSTM won {pct(ab.win_rate, 0)} of days. A Diebold–Mariano test
      reports <strong>{pStr}</strong>. In the separate baseline summary, LSTM MAE was {f2(models.lstm.mae)}°F versus
      persistence at {f2(models.persistence.mae)}°F, a {persistLift}% reduction
      {ab.significant
        ? " — evidence supporting the LSTM as an SFO residual-calibration layer alongside the shared EMOS point forecast."
        : " — not yet significant, so the A/B keeps running on the flagship before anyone is promoted."}
    </Finding>
  );
}

function AccuracyFinding({ data, otherCityCount }: { data: DashboardData; otherCityCount: number | null }) {
  const { forecast, signal } = data;
  const cal = signal.calibration;
  if (!cal) return null;
  const cohorts = cal.cohorts ?? [];
  const best = [...cohorts].sort((a, b) => b.ranked_probability_skill - a.ranked_probability_skill)[0];
  const worst = [...cohorts].sort((a, b) => a.ranked_probability_skill - b.ranked_probability_skill)[0];
  return (
    <Finding>
      This is held-out San Francisco probability research, not a live trading track record. Across <strong>{cal.n.toLocaleString()}</strong> scored outcomes,
      the probability engine carries a{" "}
      <strong>{pct(cal.ranked_probability_skill, 0)} ranked-probability skill</strong> over climatology and calls the exact
      settlement bin {pct(cal.top_bin_accuracy, 0)} of the time — against roughly a dozen 2°F-wide brackets. The reliability
      curve shows where predicted probabilities match or miss observed frequencies.
      {!!cal.warnings?.length && <> The current publication flags calibration limitations in one or more probability buckets.</>}
      {best && worst && best.name !== worst.name && (
        <>
          {" "}
          Skill varies by regime — strongest in the <strong>{cohortLabel(best.name)}</strong> cohort (
          {pct(best.ranked_probability_skill, 0)}) and weakest in <strong>{cohortLabel(worst.name)}</strong> (
          {pct(worst.ranked_probability_skill, 0)}), which the risk gates account for when sizing positions. All of it rests
          on {forecast.n_days_observed?.toLocaleString() ?? "—"} observed KSFO days across {forecast.n_years} years.
          {otherCityCount != null && otherCityCount > 0 && (
            <> The other {otherCityCount} cities run the same EMOS post-processing against their own settlement stations, without the same SFO-specific held-out study.</>
          )}
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
  const { data: coverage } = useCitiesData();
  const cities = coverage?.cities ?? [];
  const cityCount = coverage?.city_count ?? (cities.length || 15);
  const modelSample = cities
    .map((city) => city.forecasts?.find((row) => typeof row?.n_models === "number")?.n_models)
    .find((count) => typeof count === "number");
  const ensemblePhrase = modelSample == null ? "a multi-model NWP ensemble" : `an ${modelSample}-member NWP ensemble`;
  const ensembleSentence = modelSample == null ? "A multi-model NWP ensemble" : `An ${modelSample}-member NWP ensemble`;
  const otherCityCount = Math.max(cityCount - 1, 0);

  return (
    <>
      <PageHeader
        headingId="methodology-page-title"
        icon="solar:graph-up-bold"
        eyebrow="Methodology & diagnostics"
        title="How the forecast is built and tested"
        sub={`The current ${cityCount}-city coverage artifact uses ${ensemblePhrase}, leakage-free and EMOS-calibrated per station, then settles against each city's NWS Climatological Report. San Francisco is blend-capable, with residual-calibration, marine-layer, and optional external evidence; its served point forecast can fall back to EMOS.`}
      />
      <div className="mx-auto w-full max-w-6xl px-5 pb-20 pt-12 sm:px-8">
        <section className="scroll-mt-24">
          <SectionHeading
            index="01"
            eyebrow="The production pipeline"
            title="One method, running in every city"
            sub={`${ensembleSentence} is pulled leakage-free from Open-Meteo previous-runs, post-processed per city with rolling-origin EMOS into a calibrated Gaussian, then settled against each city's official NWS Climatological Report.`}
          />
          <ForecastPipeline />
        </section>

        <section className="mt-14 scroll-mt-24">
          <SectionHeading
            index="02"
            eyebrow="Model proof"
            title="The flagship's LSTM, held out-of-sample"
            sub={`Static held-out San Francisco research — not runtime health — comparing the residual LSTM with XGBoost and persistence on days none of the models trained on. The other ${otherCityCount} cities use the shared EMOS point-forecast path.`}
          />
          {diag ? (
            <div className="space-y-6">
              <ModelProofFinding diag={diag} />
              <Reveal>
                <DetailDisclosure
                  id="held-out-model-evidence"
                  icon="solar:chart-square-bold"
                  title="Held-out model evidence"
                  note="MAE comparison, feature importance, significance test, and observed-vs-predicted scatter"
                >
                  <div className="grid gap-6 lg:grid-cols-2">
                    <ModelCompareChart diag={diag} />
                    <FeatureImportanceChart diag={diag} />
                  </div>
                  <div className="grid gap-6 lg:grid-cols-2">
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

        <section className="mt-14 scroll-mt-24">
          <SectionHeading
            index="03"
            eyebrow="Forecast accuracy"
            title="Ten years of San Francisco observations"
            sub={`${forecast.n_days_observed?.toLocaleString() ?? "—"} observed days across ${forecast.n_years} years anchor the SFO climatology and held-out calibration study. The current operational point forecast is reported separately above.`}
          />
          <div className="space-y-6">
            <AccuracyFinding data={data} otherCityCount={otherCityCount} />
            <Reveal>
              <DetailDisclosure
                id="accuracy-evidence"
                icon="solar:graph-up-bold"
                title="Held-out probability evidence"
                note="Climatology, observed distribution, calibration curve, and performance by temperature regime"
              >
                <ClimatologyChart forecast={forecast} />
                <div className="grid gap-6 lg:grid-cols-2">
                  <HistogramChart story={story} forecast={forecast} />
                  <CalibrationChart signal={signal} />
                </div>
                <CohortChart signal={signal} />
              </DetailDisclosure>
            </Reveal>
          </div>
        </section>
      </div>
    </>
  );
}
