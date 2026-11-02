import { Icon } from "@iconify/react/offline";
import { useCitiesData } from "../../lib/data";
import { Finding } from "../ui/Finding";
import { InfoTip } from "../ui/InfoTip";
import { CityMethodTable } from "./CityMethodTable";
import { DetailDisclosure } from "../ui/DetailDisclosure";

interface Step {
  icon: string;
  title: string;
  desc: string;
}

function productionSteps(modelSample: number | null): Step[] {
  return [
    {
      icon: "solar:cloud-storm-linear",
      title: modelSample == null ? "Multi-model NWP ensemble" : `${modelSample}-member NWP ensemble`,
      desc: "Each model's freshest run for the target date, fetched per city.",
    },
    {
      icon: "solar:graph-up-linear",
      title: "Per-city EMOS",
      desc: "Rolling-origin fits turn the members into one Gaussian (μ, σ) per station.",
    },
    {
      icon: "solar:documents-linear",
      title: "Settled on the official report",
      desc: "Each market resolves on its city's NWS Climatological Report, never our own reading.",
    },
  ];
}

const FLAGSHIP_EXTRAS: Step[] = [
  { icon: "solar:cpu-bolt-linear", title: "LSTM calibration study", desc: "A held-out residual model trained on a decade of SFO station history." },
  { icon: "solar:layers-linear", title: "External-source research", desc: "Commercial weather inputs stay a research capability; the scheduled EMOS forecast does not use them." },
  { icon: "solar:waterdrops-linear", title: "Marine-layer features", desc: "SFO-specific coastal signals kept as an extra evidence layer." },
];

/** The production method for every city, then the San Francisco research
    layers, then the per-station matrix. Figures degrade gracefully when the
    coverage artifact is absent. */
export function ForecastPipeline() {
  const { data } = useCitiesData();
  const cities = data?.cities ?? [];
  const cityCount = data?.city_count ?? (cities.length || null);
  const flagshipName = cities.find((c) => c.has_full_blend)?.name ?? "San Francisco";
  const modelSample = cities.map((c) => c.forecasts?.find((f) => typeof f?.n_models === "number")?.n_models).find((n) => typeof n === "number") ?? null;
  const nonFlagshipCount = typeof cityCount === "number" ? Math.max(cityCount - 1, 0) : null;

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <section aria-labelledby="tier1-heading">
        <h3 id="tier1-heading" className="sr-only">Production pipeline for every city</h3>
        <ol className="grid gap-3 md:grid-cols-3">
          {productionSteps(modelSample).map((step, index) => (
            <li key={step.title} className="relative flex min-w-0 flex-col gap-3 rounded-3xl bg-surface p-5 shadow-surface">
              <div className="flex items-center justify-between">
                <span className="grid size-9 place-items-center rounded-full bg-accent-soft text-[color:var(--accent-text)]">
                  <Icon icon={step.icon} className="size-5" aria-hidden="true" />
                </span>
                <span className="tnum text-xs text-muted">Step {index + 1}</span>
              </div>
              <p className="text-sm font-semibold text-foreground">{step.title}</p>
              <p className="text-sm leading-relaxed text-muted">{step.desc}</p>
            </li>
          ))}
        </ol>
      </section>

      <section aria-labelledby="tier2-heading" className="rounded-3xl bg-surface-secondary p-5">
        <h3 id="tier2-heading" className="text-sm font-semibold text-foreground">
          {flagshipName} research layers
        </h3>
        <p className="mt-0.5 text-xs text-muted">SFO-only evidence on top; the served forecast can fall back to EMOS.</p>
        <ul className="mt-4 flex flex-wrap gap-2">
          {FLAGSHIP_EXTRAS.map((step) => (
            <li key={step.title} className="flex items-center gap-1.5 rounded-full bg-surface py-1 pl-3 pr-1 text-sm text-foreground shadow-surface">
              <Icon icon={step.icon} className="size-4 text-[color:var(--series-market)]" aria-hidden="true" />
              {step.title}
              <InfoTip label={step.title}>{step.desc}</InfoTip>
            </li>
          ))}
        </ul>
      </section>

      <Finding label="One method, many markets">
        The same {modelSample == null ? "multi-model" : <><strong className="tnum">{modelSample}</strong>-member</>} ensemble runs in{" "}
        <strong>{typeof cityCount === "number" ? <span className="tnum">{cityCount}</span> : "every"}</strong>{" "}
        {typeof cityCount === "number" && cityCount !== 1 ? "markets" : "market"}, calibrated per city and settled on each
        station's NWS report. The LSTM study and marine-layer features are <strong>{flagshipName} research layers</strong>; the
        SFO publication may serve the shared EMOS mean as a fallback.
        {nonFlagshipCount != null && nonFlagshipCount > 0 && (
          <> The other <strong className="tnum">{nonFlagshipCount}</strong> cities have a short operational record so far.</>
        )}
      </Finding>

      <DetailDisclosure
        id="city-method-matrix"
        icon="solar:map-point-linear"
        title="Station matrix"
        note={`${cityCount ?? "Published"} settlement stations, methods and climate reports`}
      >
        <CityMethodTable />
      </DetailDisclosure>
    </div>
  );
}
