import { Chip } from "@heroui/react/chip";
import { Icon } from "@iconify/react/offline";
import { LinkButton } from "../ui/LinkButton";
import { Reveal } from "../ui/Reveal";
import { DotPattern } from "../magicui/DotPattern";
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
    <header className="overview-intro hero-glow relative overflow-hidden border-b border-border/60">
      <DotPattern />
      <div className="relative mx-auto grid w-full max-w-6xl gap-8 px-5 py-10 sm:px-8 sm:py-16 lg:grid-cols-[1.08fr_0.92fr] lg:gap-12 lg:py-20">
        <div className="flex flex-col justify-center">
          <Reveal immediate className="overview-enter mb-5 flex flex-wrap items-center gap-3">
            <Chip size="sm" variant="soft" color="warning">
              <Chip.Label>Paper-trading research</Chip.Label>
            </Chip>
            <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-muted">Station-aligned · EMOS</span>
          </Reveal>

          <Reveal immediate delay={0.08} className="overview-enter">
            <h1 id="overview-page-title" tabIndex={-1} className="scroll-mt-24 font-display text-[2.4rem] font-bold leading-[1.06] tracking-tight text-balance focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[color:var(--focus)] sm:text-5xl">
              Daily highs.<br /><span className="temp-text">{cityCount} city markets.</span>
            </h1>
          </Reveal>

          <Reveal immediate delay={0.16} className="overview-enter">
            <p className="mt-5 max-w-xl text-pretty text-base leading-relaxed text-muted">
              Station-aligned forecasts and calibrated probabilities for prediction markets.
              San Francisco leads our research across {cityCount} city markets.
            </p>
            <div className="overview-rule mt-5 h-px w-16" aria-hidden="true" />
            <p className="mt-3 max-w-xl text-xs leading-relaxed text-muted">Paper decisions are gated by fees, liquidity, and risk.</p>
          </Reveal>

          <Reveal immediate delay={0.24} className="overview-enter mt-6 flex flex-wrap items-center gap-3">
            <LinkButton href="#/lab" external={false} variant="primary" className="gap-2">
              Strategy Lab <Icon icon="solar:arrow-right-bold" className="size-4" aria-hidden="true" />
            </LinkButton>
            <LinkButton href="#/methodology" external={false} variant="outline" className="gap-2">
              <Icon icon="solar:graph-up-bold" className="size-4" aria-hidden="true" /> Methodology
            </LinkButton>
          </Reveal>
        </div>

        <Reveal immediate delay={0.28} className="overview-enter overview-instrument flex items-center">
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
