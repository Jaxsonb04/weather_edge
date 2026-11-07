import { useState } from "react";
import { Chip } from "@heroui/react/chip";
import { Skeleton } from "@heroui/react/skeleton";
import { Tabs } from "@heroui/react/tabs";
import "../../styles/pro-methodology.css";
import {
  fixtureCoverage,
  pct,
  round1,
  STATIC_FIXTURE_NOTE,
  useCitiesData,
  type Cohort,
  type DashboardData,
} from "../../lib/data";
import { useDiagnostics, type Diagnostics } from "../../lib/diagnostics";
import { useTemperature } from "../../lib/units";
import { PageHeader } from "../ui/PageHeader";
import { SectionHeading } from "../ui/SectionHeading";
import { InfoTip } from "../ui/InfoTip";
import { Stat } from "../ui/Stat";
import { ModelCompareChart } from "../charts/ModelCompareChart";
import { FeatureImportanceChart } from "../charts/FeatureImportanceChart";
import { HeldOutScatter } from "../charts/HeldOutScatter";
import { ABSignificance } from "../methodology/ABSignificance";
import { ForecastPipeline } from "../methodology/ForecastPipeline";
import { ClimatologyChart } from "../charts/ClimatologyChart";
import { HistogramChart } from "../charts/HistogramChart";
import { CalibrationChart } from "../charts/CalibrationChart";
import { CohortChart } from "../charts/CohortChart";

/** The scored ladder is fixed at six markets: four 2°F brackets spanning
    66–73°F plus an open-ended tail at each end. Every scored day below 60°F
    lands in the open-bottom tail by construction, so that cohort's perfect
    top-bin score is a property of the ladder, not of the model. */
const TAIL_COHORT = "cold_below_60f";
/** The one cohort the live San Francisco profile refuses to trade at all. */
const BLOCKED_COHORT = "hot_80f_plus";

const COHORT_LABELS: Record<string, string> = {
  cold_below_60f: "cold (<60°)",
  normal_60_69f: "normal (60–69°)",
  warm_70_79f: "warm (70–79°)",
  hot_80f_plus: "hot (80°+)",
};
const cohortLabel = (name: string) => COHORT_LABELS[name] ?? name.replace(/_/g, " ");

function ModelProof({ diag }: { diag: Diagnostics }) {
  const { models, ab } = diag;
  const persistLift = Math.round((1 - models.lstm.mae / models.persistence.mae) * 100);
  const pStr = ab.p_diebold_mariano < 0.001 ? "p < 0.001" : `p = ${ab.p_diebold_mariano.toFixed(3)}`;
  const f2 = (value: number) => value.toFixed(2);
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="grid grid-cols-2 gap-x-6 gap-y-4 rounded-3xl bg-surface p-5 shadow-surface sm:grid-cols-4">
        <Stat label="LSTM error (MAE)" value={`${f2(ab.mae_lstm)}°F`} note={`XGBoost ${f2(ab.mae_xgb)}°F`} />
        <Stat label="Error reduction" value={`${round1(ab.lift_pct)}%`} note="vs XGBoost" />
        <Stat label="Days won" value={pct(ab.win_rate, 0)} note={`of ${ab.n_days.toLocaleString()} held-out days`} />
        <Stat
          label="Significance"
          value={pStr}
          note={ab.significant ? "Diebold–Mariano" : "not yet significant"}
          info={
            <InfoTip label="the model proof" title="San Francisco research">
              This is SFO residual-model research, not the universal method. The cards above come from the held-out
              A/B test. The separate baseline summary and its chart put the LSTM's MAE at {f2(models.lstm.mae)}°F
              versus persistence at {f2(models.persistence.mae)}°F, a {persistLift}% reduction. The other cities use
              the shared EMOS point forecast.
            </InfoTip>
          }
        />
      </div>
      <div className="grid min-w-0 gap-4 lg:grid-cols-2">
        <ModelCompareChart diag={diag} />
        <FeatureImportanceChart diag={diag} />
      </div>
      <div className="grid min-w-0 gap-4 lg:grid-cols-[1.4fr_1fr] lg:items-start">
        <HeldOutScatter diag={diag} />
        <ABSignificance diag={diag} />
      </div>
    </div>
  );
}

function Accuracy({ data, otherCityCount }: { data: DashboardData; otherCityCount: number | null }) {
  const { forecast, signal, story } = data;
  const fixtureYears = fixtureCoverage(forecast);
  const cal = signal.calibration;
  const cohorts = cal?.cohorts ?? [];
  const tail: Cohort | null = cohorts.find((c) => c.name === TAIL_COHORT) ?? null;
  const blocked: Cohort | null = cohorts.find((c) => c.name === BLOCKED_COHORT) ?? null;
  // Rank on top-bin accuracy (RPS skill flatters rare regimes); the open-bottom
  // tail is excluded because it is unbeatable by construction.
  const offTail = cohorts.filter((c) => c.name !== TAIL_COHORT);
  const ranked = [...offTail].sort((a, b) => b.top_bin_accuracy - a.top_bin_accuracy);
  const best: Cohort | null = ranked[0] ?? null;
  const worst: Cohort | null = ranked.length > 1 ? ranked[ranked.length - 1] ?? null : null;
  const offTailDays = offTail.reduce((sum, c) => sum + c.count, 0);
  const offTailAccuracy =
    offTailDays > 0 ? offTail.reduce((sum, c) => sum + c.count * c.top_bin_accuracy, 0) / offTailDays : null;
  const otherCities = otherCityCount != null && otherCityCount > 0 ? `the other ${otherCityCount} cities` : "the other cities";

  return (
    <div className="flex min-w-0 flex-col gap-4">
      {cal && (
        <div className="grid grid-cols-2 gap-x-6 gap-y-4 rounded-3xl bg-surface p-5 shadow-surface sm:grid-cols-4">
          <Stat label="Rank-prob. skill" value={pct(cal.ranked_probability_skill, 0)} note="vs climatology" />
          <Stat label="Top-bin accuracy" value={pct(cal.top_bin_accuracy, 0)} note="all scored days" />
          <Stat
            label="Accuracy, open tail excluded"
            value={offTailAccuracy == null ? "—" : pct(offTailAccuracy, 0)}
            note={offTailDays ? `${offTailDays.toLocaleString()} days` : undefined}
            info={
              <InfoTip label="the open-tail adjustment" title="Why exclude the tail">
                The scored ladder has four 2°F brackets spanning 66–73°F plus an open tail at each end.
                {tail ? ` All ${tail.count.toLocaleString()} days below 60°F land in the bottom tail by construction and score ${pct(tail.top_bin_accuracy, 0)}.` : ""}
              </InfoTip>
            }
          />
          <Stat label="Scored days" value={cal.n.toLocaleString()} note={fixtureYears ? `KSFO ${fixtureYears}` : "KSFO"} />
        </div>
      )}
      {cal && (best || blocked) && (
        <p className="text-sm leading-relaxed text-muted">
          {best && worst && best.name !== worst.name && (
            <>
              Strongest in <strong className="font-semibold text-foreground">{cohortLabel(best.name)}</strong> at{" "}
              {pct(best.top_bin_accuracy, 0)}, weakest in <strong className="font-semibold text-foreground">{cohortLabel(worst.name)}</strong> at{" "}
              {pct(worst.top_bin_accuracy, 0)}.{" "}
            </>
          )}
          {blocked && (
            <>
              Live Stability never trades a San Francisco target forecast into <strong className="font-semibold text-foreground">{cohortLabel(blocked.name)}</strong>{" "}
              ({blocked.count.toLocaleString()} settled days in the study); the block is lifted for {otherCities}.
            </>
          )}
          {!!cal.warnings?.length && " The publication flags calibration limits in one or more probability buckets."}
        </p>
      )}
      <ClimatologyChart forecast={forecast} />
      <div className="grid min-w-0 gap-4 lg:grid-cols-2">
        <HistogramChart story={story} forecast={forecast} />
        <CalibrationChart signal={signal} />
      </div>
      <CohortChart signal={signal} />
      <p className="text-xs text-muted">
        Based on {forecast.n_days_observed?.toLocaleString() ?? "—"} observed KSFO days across {forecast.n_years} years
        {fixtureYears ? ` (${fixtureYears})` : ""}, a {STATIC_FIXTURE_NOTE}.
      </p>
    </div>
  );
}

type EvidenceTab = "model" | "accuracy";

export default function MethodologyView({ data }: { data: DashboardData }) {
  const { data: diag, error: diagError } = useDiagnostics();
  const { data: coverage } = useCitiesData();
  const { unit } = useTemperature();
  const [tab, setTab] = useState<EvidenceTab>("model");
  const cities = coverage?.cities ?? [];
  const cityCount = coverage?.city_count ?? (cities.length || null);
  const modelSample = cities
    .map((city) => city.forecasts?.find((row) => typeof row?.n_models === "number")?.n_models)
    .find((count) => typeof count === "number");
  const otherCityCount = cityCount != null ? Math.max(cityCount - 1, 0) : null;

  return (
    <>
      <PageHeader
        headingId="methodology-page-title"
        title="How the forecast works"
        sub={`${modelSample == null ? "A multi-model" : `An ${modelSample}-member`} NWP ensemble, calibrated per station with EMOS, then settled against each city's official NWS climate report.${unit === "C" ? " Figures on this page stay in °F because the contracts settle in whole °F." : ""}`}
        meta={
          <>
            {cityCount != null && (
              <Chip size="sm" variant="soft" color="accent">
                <Chip.Label>{cityCount} cities</Chip.Label>
              </Chip>
            )}
            <Chip size="sm" variant="soft">
              <Chip.Label>San Francisco flagship</Chip.Label>
            </Chip>
          </>
        }
      />
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-14 px-4 pb-20 sm:px-6 lg:px-8">
        <section aria-labelledby="pipeline-title" className="min-w-0 scroll-mt-24">
          <SectionHeading
            id="pipeline-title"
            title="The pipeline"
            sub="One method in every city, with research layers on the flagship"
            info={
              <InfoTip label="live versus historical inputs" title="Live inputs and history">
                Serving reads each model's freshest current run. Fitting uses earlier target dates from Open-Meteo's
                Previous Runs archive, which combines fixed leads for individual hours, so it does not establish a
                single forecast vintage available at an earlier trading decision.
              </InfoTip>
            }
          />
          <ForecastPipeline />
        </section>

        <section aria-labelledby="evidence-title" className="min-w-0 scroll-mt-24">
          <SectionHeading
            id="evidence-title"
            title="Evidence"
            sub="Held-out San Francisco research, not a live trading record"
          />
          <Tabs selectedKey={tab} onSelectionChange={(key) => setTab(String(key) as EvidenceTab)} className="min-w-0">
            <Tabs.ListContainer className="self-start">
              <Tabs.List aria-label="Evidence">
                <Tabs.Tab id="model">
                  Model proof
                  <Tabs.Indicator />
                </Tabs.Tab>
                <Tabs.Tab id="accuracy">
                  Accuracy
                  <Tabs.Indicator />
                </Tabs.Tab>
              </Tabs.List>
            </Tabs.ListContainer>
            <Tabs.Panel id="model" className="pt-4">
              <div className="fade-in">
                {diag ? (
                  <ModelProof diag={diag} />
                ) : diagError ? (
                  <p role="alert" className="rounded-2xl bg-danger-soft px-4 py-6 text-center text-sm text-muted">
                    Couldn't load the model diagnostics: {diagError}
                  </p>
                ) : (
                  <>
                    <p role="status" aria-live="polite" className="sr-only">Loading diagnostics…</p>
                    <div className="skeleton--shimmer relative grid gap-4 overflow-hidden lg:grid-cols-2" aria-hidden="true">
                      <Skeleton animationType="none" className="h-28 rounded-3xl lg:col-span-2" />
                      <Skeleton animationType="none" className="h-72 rounded-3xl" />
                      <Skeleton animationType="none" className="h-72 rounded-3xl" />
                    </div>
                  </>
                )}
              </div>
            </Tabs.Panel>
            <Tabs.Panel id="accuracy" className="pt-4">
              <div className="fade-in">
                <Accuracy data={data} otherCityCount={otherCityCount} />
              </div>
            </Tabs.Panel>
          </Tabs>
        </section>
      </div>
    </>
  );
}
