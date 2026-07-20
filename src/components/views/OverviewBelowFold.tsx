import { lazy, Suspense, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Skeleton } from "@heroui/react/skeleton";
import { selectCurrentTargets, type CitiesData, type City, type DashboardData } from "../../lib/data";
import { SkillStrip } from "../kpi/SkillStrip";
import { SystemHighlights } from "../overview/SystemHighlights";
import { CityGrid } from "../overview/CityGrid";
import { TargetStatusWarning } from "../overview/TargetStatusWarning";
import { SectionHeading } from "../ui/SectionHeading";
import { Reveal } from "../ui/Reveal";
import "../../styles/pro-overview.css";

const DEFAULT_CITY = "sfo";
const CityDetail = lazy(() =>
  import("../overview/CityDetail").then((module) => ({ default: module.CityDetail })),
);

function DeferredOverviewSlab({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setReady(true);
          observer.disconnect();
        }
      },
      { rootMargin: "600px 0px", threshold: 0 },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  return (
    <div ref={ref} className="min-h-[44rem] lg:min-h-[36rem]">
      {ready ? children : <DetailSkeleton />}
    </div>
  );
}

function DetailSkeleton() {
  return (
    <div role="status" aria-label="Loading city forecast detail" className="grid gap-5 lg:grid-cols-2">
      <Skeleton className="h-[23rem] rounded-2xl" />
      <Skeleton className="h-[23rem] rounded-2xl" />
      <Skeleton className="h-64 rounded-2xl lg:col-span-2" />
    </div>
  );
}

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

interface OverviewBelowFoldProps {
  data: DashboardData;
  citiesData: CitiesData | null;
  citiesError: string | null;
  selected: string;
  onSelect: (slug: string) => void;
}

export function OverviewBelowFold({ data, citiesData, citiesError, selected, onSelect }: OverviewBelowFoldProps) {
  const { forecast, signal } = data;

  const cities = useMemo(() => citiesData?.cities ?? [], [citiesData]);
  const activeCity = useMemo(() => resolveCity(cities, selected), [cities, selected]);
  const currentTargets = useMemo(() => selectCurrentTargets(signal.targets ?? []), [signal.targets]);
  const hasPastDueTargets = useMemo(
    () => (signal.targets ?? []).some((target) => target.target_status === "past"),
    [signal.targets],
  );

  // The bracket-level market surfaces are San-Francisco-only (trading_signal.json).
  const flagshipTarget = currentTargets[0];

  return (
      <div className="mx-auto w-full max-w-6xl px-5 pb-28 sm:px-8">
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
            <CityGrid data={citiesData} error={citiesError} selected={selected} onSelect={onSelect} />
          </Reveal>
        </section>

        <section id="today" className="scroll-mt-24">
          <SectionHeading
            index="02"
            eyebrow="Today's forecast"
            title={activeCity ? `${activeCity.name} — today's forecast and market` : "Today's forecast"}
            sub="The selected city's next high, its official settlement, and recent paper-trading activity. San Francisco also shows bracket-level model-vs-market detail when the current data is verified."
          />
          {activeCity ? (
            <DeferredOverviewSlab>
              <Suspense fallback={<DetailSkeleton />}>
                <CityDetail
                  city={activeCity}
                  flagshipTarget={flagshipTarget}
                  approvedCount={signal.summary.approved_signal_count}
                />
              </Suspense>
            </DeferredOverviewSlab>
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
      </div>
  );
}
