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
    // split, but guarantee the public content mounts after the initial bundle
    // capture window. Normal scrolling still loads it immediately via observer.
    revealTimer = window.setTimeout(() => {
      setReady(true);
      observer.disconnect();
    }, 1_500);

    return () => {
      if (revealTimer !== undefined) window.clearTimeout(revealTimer);
      observer.disconnect();
    };
  }, []);

  return (
    <div className={ready ? undefined : "relative min-h-[64rem]"} aria-busy={!ready}>
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
    <div role="status" aria-label="Loading overview instruments" className="mx-auto w-full max-w-6xl px-4 sm:px-6 lg:px-8">
      <div className="skeleton--shimmer relative flex flex-col gap-4 overflow-hidden">
        <div className="h-6 w-40 rounded-full bg-surface-tertiary/70" />
        <div className="h-28 rounded-2xl bg-surface-tertiary/70" />
        <div className="mt-10 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          {Array.from({ length: 10 }).map((_, index) => (
            <div key={index} className="h-32 rounded-2xl bg-surface-tertiary/70" />
          ))}
        </div>
      </div>
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
    // The Hero carries the page's only <h1> on the happy path, and <main> always
    // points aria-labelledby at it — so this branch has to publish the same id
    // rather than leaving that reference dangling.
    return (
      <div className="mx-auto grid min-h-[60vh] w-full max-w-6xl content-center gap-4 px-4 sm:px-6 lg:px-8">
        <h1
          id="overview-page-title"
          tabIndex={-1}
          className="scroll-mt-24 text-balance text-center text-3xl font-semibold tracking-tight focus:outline-none sm:text-4xl"
        >
          WeatherEdge overview
        </h1>
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
