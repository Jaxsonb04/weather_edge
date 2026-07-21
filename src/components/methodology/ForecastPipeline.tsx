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

const TIER1_STEPS: Step[] = [
  {
    icon: "solar:cloud-storm-bold",
    title: "Nine-model NWP ensemble",
    desc: "Pulled from Open-Meteo previous-runs — only model cycles that were actually available before the target, so nothing leaks from the future.",
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

const TIER2_EXTRAS: Step[] = [
  { icon: "solar:cpu-bolt-bold", title: "LSTM sequence model", desc: "A recurrent net trained on a decade of station history." },
  { icon: "solar:layers-bold", title: "Google Weather blend", desc: "A second commercial source folded into the point forecast." },
  { icon: "solar:waterdrops-bold", title: "Marine-layer features", desc: "Coastal fog signals the ensemble alone misses." },
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
  const modelSample = cities.map((c) => c.forecasts?.find((f) => typeof f?.n_models === "number")?.n_models).find((n) => typeof n === "number");

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
            note="Runs identically for every market, each against its own settlement station."
          />
          <div className="grid gap-3 sm:grid-cols-3">
            {TIER1_STEPS.map((s, i) => (
              <StepCard key={s.title} step={s} index={i} />
            ))}
          </div>
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
            note="Added on top of Tier 1 for San Francisco, where a decade of local history supports them."
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
        The same leakage-free, nine-model NWP ensemble runs in{" "}
        <strong>
          {typeof cityCount === "number" ? <span className="tnum">{cityCount}</span> : "every"}
        </strong>{" "}
        market, EMOS-calibrated per city
        {typeof modelSample === "number" && (
          <>
            {" "}
            (a recent run weighed <strong className="tnum">{modelSample}</strong> available members)
          </>
        )}{" "}
        and settled against each station's official NWS Climatological Report. The LSTM, Google blend, and
        marine-layer features are <strong>{flagshipName}-only extras</strong>, not the universal method. Be
        honest about the record: the multi-city EMOS pipeline is backtest-grade with only a short live
        history so far, so the fourteen non-flagship cities do not yet carry a long live track record.
      </Finding>

      <Reveal delay={0.05}>
        <DetailDisclosure
          id="city-method-matrix"
          icon="solar:map-point-bold"
          title="City-by-city station matrix"
          note="15 settlement stations, model counts, methods, and official climate reports"
        >
          <CityMethodTable />
        </DetailDisclosure>
      </Reveal>
    </div>
  );
}
