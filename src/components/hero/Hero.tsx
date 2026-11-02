import { Chip } from "@heroui/react/chip";
import { Icon } from "@iconify/react/offline";
import { LinkButton } from "../ui/LinkButton";
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

/** Overview opener: what the project is in one heading and one sentence, with
    the selected city's forecast beside it. The city count is read from the
    published coverage artifact, never assumed. */
export function Hero({ targets, cities, selectedCity, activeCity, onSelectCity }: HeroProps) {
  const cityCount = cities.length;
  return (
    <header className="mx-auto w-full max-w-6xl px-4 pb-12 pt-10 sm:px-6 sm:pt-16 lg:px-8">
      <div className="grid gap-10 lg:grid-cols-[1.05fr_0.95fr] lg:items-center lg:gap-14">
        <div className="flex min-w-0 flex-col gap-5">
          <div className="flex flex-wrap items-center gap-2">
            <Chip size="sm" variant="soft" color="accent">
              <Chip.Label>Paper research</Chip.Label>
            </Chip>
            <Chip size="sm" variant="soft">
              <Chip.Label>Settled on official NWS reports</Chip.Label>
            </Chip>
          </div>
          <h1
            id="overview-page-title"
            tabIndex={-1}
            className="scroll-mt-24 text-balance text-4xl font-semibold leading-[1.05] tracking-tight text-foreground focus:outline-none sm:text-5xl"
          >
            Daily high forecasts{cityCount > 0 ? <> for <span className="text-[color:var(--accent-text)]">{cityCount} U.S.&nbsp;cities</span></> : null}
          </h1>
          <p className="max-w-xl text-pretty text-base leading-relaxed text-muted">
            Calibrated probabilities for each city's high, checked against its official climate report and tested
            with paper trades in prediction markets.
          </p>
          <div className="flex flex-wrap items-center gap-3 pt-1">
            <LinkButton href="#/lab" external={false} variant="primary">
              Open the Strategy Lab <Icon icon="solar:arrow-right-linear" aria-hidden="true" />
            </LinkButton>
            <LinkButton href="#/methodology" external={false} variant="tertiary">
              How it works
            </LinkButton>
          </div>
        </div>

        <div className="flex min-w-0 flex-col gap-3">
          {cities.length > 0 && (
            <div className="flex items-center justify-between gap-3">
              <p className="text-sm font-medium text-foreground">Forecast</p>
              <CitySelect cities={cities} selected={selectedCity} onSelect={onSelectCity} />
            </div>
          )}
          <ForecastDial key={activeCity?.slug ?? "sfo"} targets={targets} city={activeCity} />
        </div>
      </div>
    </header>
  );
}
