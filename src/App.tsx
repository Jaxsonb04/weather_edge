import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Icon } from "@iconify/react";
import { useDashboardData } from "./lib/data";
import { useTheme } from "./lib/theme";
import { useHashRoute } from "./lib/useHashRoute";
import { TopBar } from "./components/layout/TopBar";
import { PublicationStatusBanner } from "./components/layout/PublicationStatusBanner";
import { CommandPalette } from "./components/layout/CommandPalette";
import { Footer } from "./components/Footer";
import { LoadingState, ErrorState } from "./components/States";
import { ErrorBoundary } from "./components/ErrorBoundary";

const OverviewView = lazy(() =>
  import("./components/views/OverviewView").then((module) => ({ default: module.OverviewView })),
);
const MethodologyView = lazy(() => import("./components/views/MethodologyView"));
const StrategyLabView = lazy(() => import("./components/views/StrategyLabView"));

const REPO = "https://github.com/Jaxsonb04/weather_edge";
const LIVE = "https://jaxsonb04.github.io/weather_edge/";
const DISCLAIMER = "Paper-trading research only. No live orders are ever placed.";

function ViewLoader() {
  return (
    <div role="status" aria-live="polite" className="flex min-h-[60vh] items-center justify-center gap-2 text-muted">
      <Icon icon="solar:refresh-linear" className="size-4 animate-spin" aria-hidden="true" />
      <span className="text-sm">Loading…</span>
    </div>
  );
}

export default function App() {
  const { data, error } = useDashboardData();
  const { mode, toggle } = useTheme();
  const { route, navigate } = useHashRoute();
  const [cmdOpen, setCmdOpen] = useState(false);

  // SPA focus management: move focus to the new view on route change (not on first mount).
  const contentRef = useRef<HTMLDivElement>(null);
  const mounted = useRef(false);
  useEffect(() => {
    if (mounted.current) contentRef.current?.focus({ preventScroll: true });
    else mounted.current = true;
  }, [route]);

  return (
    <div className="grain flex min-h-screen flex-col bg-background text-foreground">
      <TopBar
        mode={mode}
        onToggleTheme={toggle}
        onOpenCommand={() => setCmdOpen(true)}
        route={route}
        repoUrl={REPO}
        liveUrl={LIVE}
      />
      <PublicationStatusBanner />
      <CommandPalette
        open={cmdOpen}
        onOpenChange={setCmdOpen}
        onToggleTheme={toggle}
        onNavigate={navigate}
        repoUrl={REPO}
        liveUrl={LIVE}
      />

      <div
        ref={contentRef}
        tabIndex={-1}
        className="flex-1 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[color:var(--focus)]"
      >
        {route === "lab" ? (
          <ErrorBoundary key={route}>
            <Suspense fallback={<ViewLoader />}>
              <StrategyLabView />
            </Suspense>
          </ErrorBoundary>
        ) : error && !data ? (
          <ErrorState message={error} />
        ) : !data ? (
          <LoadingState />
        ) : route === "methodology" ? (
          <ErrorBoundary key={route}>
            <Suspense fallback={<ViewLoader />}>
              <MethodologyView data={data} />
            </Suspense>
          </ErrorBoundary>
        ) : (
          <ErrorBoundary key={route}>
            <Suspense fallback={<ViewLoader />}>
              <OverviewView data={data} />
            </Suspense>
          </ErrorBoundary>
        )}
      </div>

      <Footer disclaimer={data?.signal.disclaimer ?? DISCLAIMER} repoUrl={REPO} liveUrl={LIVE} />
    </div>
  );
}
