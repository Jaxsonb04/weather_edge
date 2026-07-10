import { useMemo, useState } from "react";
import { selectCurrentTargets, useCitiesData, type City, type DashboardData } from "../../lib/data";
import { Hero } from "../hero/Hero";
import { SkillStrip } from "../kpi/SkillStrip";
import { SystemHighlights } from "../overview/SystemHighlights";
import { CityGrid } from "../overview/CityGrid";
import { CityDetail } from "../overview/CityDetail";
import { CitySelect } from "../overview/CitySelect";
import { TargetStatusWarning } from "../overview/TargetStatusWarning";
import { SectionHeading } from "../ui/SectionHeading";
import { Reveal } from "../ui/Reveal";

const DEFAULT_CITY = "sfo";

/** Resolve the selected city with graceful fallbacks: exact slug, then the SFO
    flagship, then the first published city. */
function resolveCity(cities: City[], selected: string): City | null {
  if (!cities.length) return null;
  return (
    cities.find((c) => (c.slug ?? c.series_ticker) === selected) ??
    cities.find((c) => c.slug === DEFAULT_CITY) ??
    cities[0]
  );
}

export function OverviewView({ data }: { data: DashboardData }) {
  const { forecast, signal } = data;
  const { data: citiesData, error: citiesError } = useCitiesData();
  const [selected, setSelected] = useState(DEFAULT_CITY);

  const cities = useMemo(() => citiesData?.cities ?? [], [citiesData]);
  const activeCity = useMemo(() => resolveCity(cities, selected), [cities, selected]);
  const currentTargets = useMemo(() => selectCurrentTargets(signal.targets ?? []), [signal.targets]);
  const hasPastDueTargets = useMemo(
    () => (signal.targets ?? []).some((target) => target.target_status === "past"),
    [signal.targets],
  );

  if (!currentTargets.length) {
    return (
      <div className="mx-auto grid min-h-[60vh] w-full max-w-6xl content-center gap-4 px-5 sm:px-8">
        <TargetStatusWarning targets={signal.targets ?? []} />
        <p className="text-center text-sm text-muted">No current forecast targets are published right now.</p>
      </div>
    );
  }

  // The bracket-level market surfaces are San-Francisco-only (trading_signal.json).
  const flagshipTarget = currentTargets[0];

  return (
    <>
      <Hero targets={currentTargets} />
      <main className="mx-auto w-full max-w-6xl px-5 pb-28 sm:px-8">
        {hasPastDueTargets && (
          <div className="pt-5">
            <TargetStatusWarning targets={signal.targets ?? []} />
          </div>
        )}
        <SkillStrip forecast={forecast} signal={signal} />

        <section id="cities" className="scroll-mt-24">
          <SectionHeading
            index="01"
            eyebrow="Coverage"
            title="Fifteen city markets, one calibrated engine"
            sub="Every market settles on its own official NWS climate report and runs the same NWP/EMOS forecast. Select any city to drill into its call — San Francisco is the flagship, with the full market microstructure."
          />
          <Reveal>
            <CityGrid data={citiesData} error={citiesError} selected={selected} onSelect={setSelected} />
          </Reveal>
        </section>

        <section id="today" className="scroll-mt-24">
          <SectionHeading
            index="02"
            eyebrow="Today's forecast"
            title={activeCity ? `${activeCity.name} — today's forecast and market` : "Today's forecast"}
            sub="The selected city's next high, its official settlement, and recent paper-trading activity. San Francisco also shows bracket-level model-vs-market detail when the current data is verified."
          />
          {cities.length > 1 && (
            <Reveal className="mb-5 flex flex-wrap items-center gap-3">
              <span className="text-xs uppercase tracking-wide text-muted">Active city</span>
              <CitySelect cities={cities} selected={selected} onSelect={setSelected} />
            </Reveal>
          )}
          {activeCity ? (
            <CityDetail
              city={activeCity}
              flagshipTarget={flagshipTarget}
              approvedCount={signal.summary.approved_signal_count}
            />
          ) : (
            <Reveal>
              <p className="rounded-2xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted">
                Per-city detail will appear once the fifteen-city coverage artifact is published.
              </p>
            </Reveal>
          )}
        </section>

        <section id="system" className="scroll-mt-24">
          <SectionHeading
            index="03"
            eyebrow="Engineering"
            title="What's running behind the page"
            sub="The pieces that produce everything above: a forecasting stack, a market pricing engine, and the production setup that keeps it running unattended."
          />
          <Reveal>
            <SystemHighlights />
          </Reveal>
        </section>
      </main>
    </>
  );
}
