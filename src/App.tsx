import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Icon } from "@iconify/react/offline";
import { useDashboardData } from "./lib/data";
import { useTheme } from "./lib/theme";
import { ROUTES, useHashRoute } from "./lib/useHashRoute";
import { TopBar } from "./components/layout/TopBar";
import { PublicationStatusBanner } from "./components/layout/PublicationStatusBanner";
import { Footer } from "./components/Footer";
import { LoadingState, ErrorState } from "./components/States";
import { ErrorBoundary } from "./components/ErrorBoundary";

const OverviewView = lazy(() =>
  import("./components/views/OverviewView").then((module) => ({ default: module.OverviewView })),
);
const MethodologyView = lazy(() => import("./components/views/MethodologyView"));
const StrategyLabView = lazy(() => import("./components/views/StrategyLabView"));
const CommandPalette = lazy(() =>
  import("./components/layout/CommandPalette").then((module) => ({ default: module.CommandPalette })),
);

const REPO = "https://github.com/Jaxsonb04/weather_edge";
const LIVE = "https://jaxsonb04.github.io/weather_edge/";
const DISCLAIMER = "Paper-trading research only. Live orders are disabled.";

function ViewLoader() {
  return (
    <div role="status" aria-live="polite" className="flex min-h-[60vh] items-center justify-center gap-2 text-muted">
      <Icon icon="solar:refresh-bold" className="size-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
      <span className="text-sm">Loading…</span>
    </div>
  );
}

export default function App() {
  const { data, error } = useDashboardData();
  const { mode, toggle } = useTheme();
  const { route, navigate } = useHashRoute();
  const [cmdOpen, setCmdOpen] = useState(false);
  // Once opened the palette stays mounted. Unmounting the overlay subtree in the
  // same commit that closes it tears down React Aria's focus scope before it can
  // restore focus, dropping focus to <body>. Command.Backdrop's isOpen is what
  // drives visibility, so a mounted-but-closed palette renders nothing.
  const [cmdMounted, setCmdMounted] = useState(false);
  const mainRef = useRef<HTMLElement>(null);
  const hasValidSignal = data != null && typeof data.signal === "object" && data.signal !== null;

  // SPA focus management: move focus to the route heading after navigation,
  // including after a lazy route chunk finishes rendering.
  const mounted = useRef(false);
  useEffect(() => {
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    // A first visit to a route waits on its chunk, so this poll can still be
    // running a second after navigation. Whoever holds focus when the heading
    // finally appears must have been put there by the navigation itself — if the
    // user has moved on (tabbed to the theme toggle, say), leave them alone.
    const origin = document.activeElement;
    let attempts = 0;
    let timer = 0;
    const focusHeading = () => {
      if (document.activeElement !== origin) return;
      const heading = document.getElementById(`${route}-page-title`);
      if (heading) heading.focus({ preventScroll: true });
      else if (attempts++ < 50) timer = window.setTimeout(focusHeading, 20);
    };
    timer = window.setTimeout(focusHeading, 0);
    return () => window.clearTimeout(timer);
  }, [route]);

  useEffect(() => {
    if (cmdOpen) setCmdMounted(true);
  }, [cmdOpen]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setCmdOpen((open) => !open);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <div className="grain flex min-h-screen flex-col bg-background text-foreground">
      <a
        href="#main-content"
        onClick={(event) => {
          event.preventDefault();
          mainRef.current?.focus({ preventScroll: true });
          mainRef.current?.scrollIntoView?.();
        }}
        className="fixed left-4 top-3 z-50 -translate-y-20 rounded-lg bg-foreground px-4 py-2 text-sm font-semibold text-background no-underline shadow-lg transition-transform duration-150 focus:translate-y-0 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--focus)] motion-reduce:transition-none"
      >
        Skip to main content
      </a>
      <TopBar
        mode={mode}
        onToggleTheme={toggle}
        onOpenCommand={() => setCmdOpen(true)}
        route={route}
        repoUrl={REPO}
        liveUrl={LIVE}
      />
      <PublicationStatusBanner />
      {cmdMounted && (
        <Suspense fallback={null}>
          <CommandPalette
            open={cmdOpen}
            onOpenChange={setCmdOpen}
            onToggleTheme={toggle}
            onNavigate={navigate}
            repoUrl={REPO}
            liveUrl={LIVE}
          />
        </Suspense>
      )}

      <main
        ref={mainRef}
        id="main-content"
        tabIndex={-1}
        aria-label={`${ROUTES.find((item) => item.id === route)?.label ?? "WeatherEdge"} content`}
        aria-labelledby={`${route}-page-title`}
        className="min-h-screen flex-1"
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
        ) : !hasValidSignal ? (
          <ErrorState message="Published trading signal is missing or invalid." />
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
      </main>

      <Footer disclaimer={data?.signal?.disclaimer ?? DISCLAIMER} repoUrl={REPO} liveUrl={LIVE} />
    </div>
  );
}
