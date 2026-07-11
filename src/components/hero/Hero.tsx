import { Chip } from "@heroui/react/chip";
import { Icon } from "@iconify/react/offline";
import { LinkButton } from "../ui/LinkButton";
import { Reveal } from "../ui/Reveal";
import { ForecastDial } from "./ForecastDial";
import type { Target } from "../../lib/data";

interface HeroProps {
  targets: Target[];
}

export function Hero({ targets }: HeroProps) {
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
              <Chip.Label>15 city markets</Chip.Label>
            </Chip>
          </Reveal>

          <Reveal immediate delay={0.08}>
            <h1 className="font-display text-[2.6rem] font-bold leading-[1.02] tracking-tight text-balance sm:text-6xl">
              Forecasting <span className="temp-text">daily highs in fifteen cities</span>, priced on prediction markets.
            </h1>
          </Reveal>

          <Reveal immediate delay={0.16}>
            <p className="mt-5 max-w-xl text-pretty text-base leading-relaxed text-muted">
              One calibrated NWP/EMOS engine prices daily-high brackets across fifteen US city markets,
              each settling on its own NWS station. San Francisco is the flagship — Google&nbsp;Weather,
              NWS, Open-Meteo and a decade of KSFO history feed its full blend — and every trade is
              converted to fee-aware edge and gated.
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
            <ForecastDial targets={targets} />
          </div>
        </Reveal>
      </div>
    </header>
  );
}
