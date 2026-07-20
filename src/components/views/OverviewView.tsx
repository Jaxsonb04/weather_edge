import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import {
  selectCurrentTargets,
  useCitiesData,
  type CitiesData,
  type City,
  type DashboardData,
} from "../../lib/data";
import { Hero } from "../hero/Hero";
import { TargetStatusWarning } from "../overview/TargetStatusWarning";

const OverviewBelowFold = lazy(() =>
  import("./OverviewBelowFold").then((module) => ({ default: module.OverviewBelowFold })),
);

interface BelowFoldBoundaryProps {
  data: DashboardData;
  citiesData?: CitiesData | null;
  citiesError?: string | null;
  selected?: string;
  onSelect?: (slug: string) => void;
}

export function BelowFoldBoundary({
  data,
  citiesData = null,
  citiesError = null,
  selected = "sfo",
  onSelect = () => {},
}: BelowFoldBoundaryProps) {
  const [ready, setReady] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    let revealTimer: number | undefined;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setReady(true);
          if (revealTimer !== undefined) window.clearTimeout(revealTimer);
          observer.disconnect();
        }
      },
      { rootMargin: "0px", threshold: 0 },
    );
    observer.observe(element);

    // A scrollbar drag, Page End, or restored scroll position can jump past a
    // one-pixel observer target without ever intersecting it. Keep the chunk
    // split, but guarantee the public content mounts shortly after first paint.
    revealTimer = window.setTimeout(() => {
      setReady(true);
      observer.disconnect();
    }, 500);

    return () => {
      if (revealTimer !== undefined) window.clearTimeout(revealTimer);
      observer.disconnect();
    };
  }, []);

  return (
    <div className={ready ? undefined : "relative min-h-[72rem]"} aria-busy={!ready}>
      {ready ? (
        <Suspense fallback={<BelowFoldSkeleton />}>
          <OverviewBelowFold
            data={data}
            citiesData={citiesData}
            citiesError={citiesError}
            selected={selected}
            onSelect={onSelect}
          />
        </Suspense>
      ) : (
        <>
          <BelowFoldSkeleton />
          <div
            ref={ref}
            data-testid="overview-below-fold-sentinel"
            className="pointer-events-none absolute inset-x-0 top-80 h-px"
            aria-hidden="true"
          />
        </>
      )}
    </div>
  );
}

function BelowFoldSkeleton() {
  return (
    <div role="status" aria-label="Loading overview instruments" className="mx-auto w-full max-w-6xl px-5 py-10 sm:px-8">
      <div className="h-[72rem] animate-pulse rounded-2xl bg-surface-secondary motion-reduce:animate-none" />
    </div>
  );
}

export function OverviewView({ data }: { data: DashboardData }) {
  const { data: citiesData, error: citiesError } = useCitiesData();
  const [selected, setSelected] = useState("sfo");
  const cities = useMemo(() => citiesData?.cities ?? [], [citiesData]);
  const activeCity = useMemo(() => resolveCity(cities, selected), [cities, selected]);
  const targets = useMemo(() => selectCurrentTargets(data.signal.targets ?? []), [data.signal.targets]);

  if (!targets.length) {
    return (
      <div className="mx-auto grid min-h-[60vh] w-full max-w-6xl content-center gap-4 px-5 sm:px-8">
        <TargetStatusWarning targets={data.signal.targets ?? []} />
        <p className="text-center text-sm text-muted">No current forecast targets are published right now.</p>
      </div>
    );
  }

  return (
    <>
      <Hero
        targets={targets}
        cities={cities}
        selectedCity={selected}
        activeCity={activeCity}
        onSelectCity={setSelected}
      />
      <BelowFoldBoundary
        data={data}
        citiesData={citiesData}
        citiesError={citiesError}
        selected={selected}
        onSelect={setSelected}
      />
    </>
  );
}

/** Resolve the active city without trusting artifact ordering. */
function resolveCity(cities: City[], selected: string): City | null {
  if (!cities.length) return null;
  return cities.find((city) => (city.slug ?? city.series_ticker) === selected)
    ?? cities.find((city) => city.slug === "sfo")
    ?? cities[0];
}
