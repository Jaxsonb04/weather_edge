import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { selectCurrentTargets, type DashboardData } from "../../lib/data";
import { Hero } from "../hero/Hero";
import { TargetStatusWarning } from "../overview/TargetStatusWarning";

const OverviewBelowFold = lazy(() =>
  import("./OverviewBelowFold").then((module) => ({ default: module.OverviewBelowFold })),
);

export function BelowFoldBoundary({ data }: { data: DashboardData }) {
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
      { rootMargin: "0px", threshold: 0 },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  return (
    <div className={ready ? undefined : "relative min-h-[72rem]"} aria-busy={!ready}>
      {ready ? (
        <Suspense fallback={<BelowFoldSkeleton />}>
          <OverviewBelowFold data={data} />
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
      <Hero targets={targets} />
      <BelowFoldBoundary data={data} />
    </>
  );
}
