import { Icon } from "@iconify/react/offline";
import { useCitiesData } from "../../lib/data";
import { Reveal } from "../ui/Reveal";
import { Finding } from "../ui/Finding";
import { CityMethodTable } from "./CityMethodTable";
import { DetailDisclosure } from "../ui/DetailDisclosure";

interface Step {
  icon: string;
  title: string;
  desc: string;
}

function tier1Steps(modelSample: number | null): Step[] {
  return [
    {
      icon: "solar:cloud-storm-bold",
      title: modelSample == null ? "Multi-model NWP ensemble" : `${modelSample}-member NWP ensemble`,
      desc: "Live serving normally uses each model's freshest current run for the target date, fetched per city from the forecast API. The published forecast identifies its served method.",
    },
    {
      icon: "solar:graph-up-bold",
      title: "Per-city EMOS post-processing",
      desc: "Rolling-origin ensemble model output statistics calibrate the members into one Gaussian (μ, σ) tuned to each station's own error history.",
    },
    {
      icon: "solar:documents-bold",
      title: "Settled on the official CLI",
      desc: "Every market resolves against that city's own NWS Climatological Report for its settlement station — never our own reading.",
    },
  ];
}

const TIER2_EXTRAS: Step[] = [
  { icon: "solar:cpu-bolt-bold", title: "LSTM calibration evidence", desc: "A held-out residual model trained on a decade of SFO station history." },
  { icon: "solar:layers-bold", title: "External-source research", desc: "Commercial weather inputs remain a research and compatibility capability; the scheduled EMOS forecast does not currently use them." },
  { icon: "solar:waterdrops-bold", title: "Marine-layer features", desc: "SFO-specific coastal signals retained as an additional evidence layer." },
];

function StepCard({ step, index }: { step: Step; index: number }) {
  return (
    <div className="relative flex h-full flex-col gap-2 rounded-xl bg-surface-secondary/60 p-4 ring-1 ring-border/60">
      <div className="flex items-center gap-2.5">
        <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-accent-soft text-accent ring-1 ring-accent/25">
          <Icon icon={step.icon} className="size-4" aria-hidden="true" />
        </span>
        <span className="font-mono text-[11px] font-semibold text-[color:var(--accent-text)]">
          {String(index + 1).padStart(2, "0")}
        </span>
      </div>
      <p className="font-display text-sm font-semibold text-foreground">{step.title}</p>
      <p className="text-xs leading-relaxed text-muted">{step.desc}</p>
    </div>
  );
}

function TierLabel({ tone, index, title, note }: { tone: "primary" | "extra"; index: string; title: string; note: string }) {
  const dot = tone === "primary" ? "bg-accent" : "bg-[color:var(--series-market)]";
  return (
    <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
      <span className="flex items-center gap-2">
        <span className={`size-2 rounded-full ${dot}`} aria-hidden="true" />
        <span className="font-mono text-[11px] font-semibold uppercase tracking-[0.18em] text-[color:var(--accent-text)]">
          {index}
        </span>
        <span className="font-display text-sm font-semibold text-foreground">{title}</span>
      </span>
      <span className="text-xs text-muted">{note}</span>
    </div>
  );
}

/** Section 01: the production multi-city pipeline as the method, with the SF
    flagship extras drawn as a layer on top — a stepped div diagram, the per-city
    method table, and an honest Finding. All figures degrade gracefully when the
    coverage artifact is absent. */
export function ForecastPipeline() {
  const { data } = useCitiesData();
  const cities = data?.cities ?? [];
  const cityCount = data?.city_count ?? (cities.length || null);
  const flagshipName = cities.find((c) => c.has_full_blend)?.name ?? "San Francisco";
  const modelSample = cities.map((c) => c.forecasts?.find((f) => typeof f?.n_models === "number")?.n_models).find((n) => typeof n === "number") ?? null;
  const nonFlagshipCount = typeof cityCount === "number" ? Math.max(cityCount - 1, 0) : null;

  return (
    <div className="space-y-5">
      <Reveal className="space-y-6">
        <section aria-labelledby="tier1-heading">
          <h3 id="tier1-heading" className="sr-only">
            Tier one — production pipeline for every city
          </h3>
          <TierLabel
            tone="primary"
            index="Tier 1"
            title="Production · all cities"
            note="The same architecture runs in every market, fitted and settled per station."
          />
          <div className="grid gap-3 sm:grid-cols-3">
            {tier1Steps(modelSample).map((s, i) => (
              <StepCard key={s.title} step={s} index={i} />
            ))}
          </div>
          {/* The serving feed and the fitting feed are deliberately different
              endpoints; conflating them reads as "the live forecast is stale by
              design", which is the opposite of what the code does. */}
          <p className="mt-3 text-xs leading-relaxed text-muted">
            <span className="font-semibold text-foreground">Live inputs and historical evidence.</span> Serving reads the freshest
            current run. Fitting uses earlier target dates from Open-Meteo's Previous Runs archive, which combines
            fixed leads for individual hours. That archive does not establish a single forecast vintage available
            at an earlier trading decision.
          </p>
        </section>

        <div className="flex items-center gap-3" aria-hidden="true">
          <span className="h-px flex-1 bg-border/60" />
          <span className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.18em] text-muted">
            <Icon icon="solar:arrow-down-bold" className="size-3.5" />
            layered on top for the flagship only
          </span>
          <span className="h-px flex-1 bg-border/60" />
        </div>

        <section aria-labelledby="tier2-heading">
          <h3 id="tier2-heading" className="sr-only">
            Tier two — flagship extras, San Francisco only
          </h3>
          <TierLabel
            tone="extra"
            index="Tier 2"
            title={`Flagship extras · ${flagshipName} only`}
            note="Available as SFO-specific evidence; the served point forecast can fall back to EMOS when optional inputs are absent."
          />
          <div className="grid gap-3 sm:grid-cols-3">
            {TIER2_EXTRAS.map((s) => (
              <div
                key={s.title}
                className="flex flex-col gap-1.5 rounded-xl bg-[color:var(--series-market)]/8 p-4 ring-1 ring-[color:var(--series-market)]/25"
              >
                <span className="flex items-center gap-2 text-[color:var(--series-market)]">
                  <Icon icon={s.icon} className="size-4" aria-hidden="true" />
                  <span className="font-display text-sm font-semibold text-foreground">{s.title}</span>
                </span>
                <p className="text-xs leading-relaxed text-muted">{s.desc}</p>
              </div>
            ))}
          </div>
        </section>
      </Reveal>

      <Finding>
        The same {modelSample == null ? "multi-model" : <><strong className="tnum">{modelSample}</strong>-member</>} NWP ensemble runs in{" "}
        <strong>
          {typeof cityCount === "number" ? <span className="tnum">{cityCount}</span> : "every"}
        </strong>{" "}
        market, EMOS-calibrated per city using rolling-origin historical fits and settled against each station's
        official NWS Climatological Report. The
        LSTM calibration study and marine-layer features are <strong>{flagshipName} research layers</strong>.
        External-source integration remains a research and compatibility capability. The current SFO publication may serve the shared EMOS weighted mean as an operational fallback.
        {nonFlagshipCount != null && nonFlagshipCount > 0 && (
          <> The other <strong className="tnum">{nonFlagshipCount}</strong> cities have only a short operational record so far.</>
        )}
      </Finding>

      <Reveal delay={0.05}>
        <DetailDisclosure
          id="city-method-matrix"
          icon="solar:map-point-bold"
          title="City-by-city station matrix"
          note={`${cityCount ?? "Published"} settlement stations, member counts, methods, and official climate reports`}
        >
          <CityMethodTable />
        </DetailDisclosure>
      </Reveal>
    </div>
  );
}
