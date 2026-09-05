import { Chip } from "@heroui/react/chip";
import { Icon } from "@iconify/react/offline";
import { LinkButton } from "../ui/LinkButton";
import { Reveal } from "../ui/Reveal";
import { ForecastDial } from "./ForecastDial";
import { CitySelect } from "../overview/CitySelect";
import type { City, Target } from "../../lib/data";

interface HeroProps {
  targets: Target[];
  cities: City[];
  selectedCity: string;
  activeCity: City | null;
  onSelectCity: (slug: string) => void;
}

export function Hero({ targets, cities, selectedCity, activeCity, onSelectCity }: HeroProps) {
  const cityCount = cities.length || 15;
  return (
    <header className="hero-glow relative overflow-hidden border-b border-border/60">
      <div className="grid-lines pointer-events-none absolute inset-0 opacity-50" />
      <div className="relative mx-auto grid w-full max-w-6xl gap-10 px-5 py-16 sm:px-8 lg:grid-cols-[1.08fr_0.92fr] lg:py-24">
        <div className="flex flex-col justify-center">
          <Reveal immediate className="mb-5 flex flex-wrap items-center gap-2">
            <Chip size="sm" variant="soft" color="warning">
              <Chip.Label>Paper-trading research</Chip.Label>
            </Chip>
            <Chip size="sm" variant="soft">
              <Chip.Label>Station-aligned · EMOS-calibrated</Chip.Label>
            </Chip>
            <Chip size="sm" variant="soft">
              <Chip.Label>{cityCount} city markets</Chip.Label>
            </Chip>
          </Reveal>

          <Reveal immediate delay={0.08}>
            <h1 id="overview-page-title" tabIndex={-1} className="scroll-mt-24 font-display text-[2.6rem] font-bold leading-[1.02] tracking-tight text-balance focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[color:var(--focus)] sm:text-6xl">
              Forecasting <span className="temp-text">daily highs in {cityCount} cities</span>, priced on prediction markets.
            </h1>
          </Reveal>

          <Reveal immediate delay={0.16}>
            <p className="mt-5 max-w-xl text-pretty text-base leading-relaxed text-muted">
              An NWP ensemble and per-station EMOS layer price daily-high brackets across {cityCount} US city markets,
              each settling on its own NWS station. San Francisco is the flagship, with additional residual-calibration
              and marine-layer research alongside the shared model. Every paper decision remains fee-, liquidity-, and risk-gated.
            </p>
          </Reveal>

          <Reveal immediate delay={0.24} className="mt-7 flex flex-wrap items-center gap-3">
            <LinkButton href="#/lab" external={false} variant="primary" className="gap-2">
              Open the Strategy Lab <Icon icon="solar:arrow-right-bold" className="size-4" />
            </LinkButton>
            <LinkButton href="#/methodology" external={false} variant="outline" className="gap-2">
              <Icon icon="solar:graph-up-bold" className="size-4" /> See the methodology
            </LinkButton>
          </Reveal>
        </div>

        <Reveal immediate delay={0.18} className="flex items-center">
          <div className="w-full">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-3 px-1">
              <div>
                <p className="font-mono text-[10px] font-medium uppercase tracking-[0.16em] text-muted">Forecast desk</p>
                <p className="mt-0.5 text-xs text-muted">Choose any of the {cityCount} station-aligned city markets.</p>
              </div>
              {cities.length > 0 && (
                <CitySelect cities={cities} selected={selectedCity} onSelect={onSelectCity} />
              )}
            </div>
            <ForecastDial key={activeCity?.slug ?? "sfo"} targets={targets} city={activeCity} />
          </div>
        </Reveal>
      </div>
    </header>
  );
}
