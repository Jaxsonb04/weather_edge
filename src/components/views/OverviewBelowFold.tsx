import { lazy, Suspense, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Skeleton } from "@heroui/react/skeleton";
import { selectCurrentTargets, type CitiesData, type City, type DashboardData } from "../../lib/data";
import { SkillStrip } from "../kpi/SkillStrip";
import { SystemHighlights } from "../overview/SystemHighlights";
import { CityGrid, flagshipIntradayLock } from "../overview/CityGrid";
import { TargetStatusWarning } from "../overview/TargetStatusWarning";
import { SectionHeading } from "../ui/SectionHeading";
import { Reveal } from "../ui/Reveal";
import { InfoTip } from "../ui/InfoTip";
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
    <div ref={ref} className="min-h-[40rem] lg:min-h-[28rem]">
      {ready ? children : <DetailSkeleton />}
    </div>
  );
}

function DetailSkeleton() {
  return (
    <div role="status" aria-label="Loading city forecast detail" className="skeleton--shimmer relative grid gap-4 overflow-hidden lg:grid-cols-2">
      <Skeleton animationType="none" className="h-[22rem] rounded-3xl" />
      <Skeleton animationType="none" className="h-[22rem] rounded-3xl" />
      <Skeleton animationType="none" className="h-16 rounded-2xl lg:col-span-2" />
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
  // That same artifact republishes SFO's settlement-day high after the intraday
  // update, so the coverage grid and the city drill-down reconcile against it
  // rather than showing the plain EMOS issue the hero contradicts.
  const intradayLock = useMemo(() => flagshipIntradayLock(currentTargets), [currentTargets]);

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-14 px-4 pb-20 sm:px-6 lg:px-8">
      {hasPastDueTargets && <TargetStatusWarning targets={signal.targets ?? []} />}
      <Reveal>
        <SkillStrip forecast={forecast} signal={signal} />
      </Reveal>

      <section id="cities" aria-labelledby="cities-title" className="min-w-0 scroll-mt-24">
        <SectionHeading
          id="cities-title"
          title="Cities"
          sub={cities.length ? `${cities.length} markets · choose one for its detail` : "Choose a city for its detail"}
          info={
            <InfoTip label="city coverage">
              Every market settles on its own NWS Climatological Report and runs the same multi-model EMOS forecast.
              On a settlement day the high is updated with the observed high so far; the tile then also shows the
              original EMOS issue. San Francisco is the flagship, with full market detail.
            </InfoTip>
          }
        />
        <Reveal>
          <CityGrid
            data={citiesData}
            error={citiesError}
            selected={selected}
            onSelect={onSelect}
            intradayLock={intradayLock}
          />
        </Reveal>
      </section>

      <section id="today" aria-labelledby="today-title" className="min-w-0 scroll-mt-24">
        <SectionHeading
          id="today-title"
          title={activeCity?.name ?? "Selected city"}
          sub="Forecast, settlement and paper activity"
        />
        {activeCity ? (
          <DeferredOverviewSlab>
            <Suspense fallback={<DetailSkeleton />}>
              <CityDetail
                city={activeCity}
                flagshipTarget={flagshipTarget}
                intradayLock={intradayLock}
              />
            </Suspense>
          </DeferredOverviewSlab>
        ) : citiesError ? (
          /* A failed fetch is not a pre-publication state — say which one it is. */
          <p role="alert" className="rounded-2xl bg-danger-soft px-4 py-6 text-center text-sm text-muted">
            Couldn't load city coverage, so city detail is unavailable. Reload the page to try again.
          </p>
        ) : (
          <p className="rounded-2xl bg-surface-secondary px-4 py-6 text-center text-sm text-muted">
            City detail appears once the coverage artifact is published.
          </p>
        )}
      </section>

      <section id="system" aria-labelledby="system-title" className="min-w-0 scroll-mt-24">
        <SectionHeading id="system-title" title="How it works" sub="Three steps, running unattended on AWS" />
        <Reveal>
          <SystemHighlights />
        </Reveal>
      </section>
    </div>
  );
}
