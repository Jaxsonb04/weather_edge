import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Button } from "@heroui/react/button";
import { Modal } from "@heroui/react/modal";
import { Tooltip } from "@heroui/react/tooltip";
import { buttonVariants } from "@heroui/styles";
import { Segment } from "@heroui-pro/react/segment";
import { Icon } from "@iconify/react/offline";
import { ROUTES, type Route } from "../../lib/useHashRoute";
import type { ThemeMode } from "../../lib/theme";
import { useTemperature, type TempUnit } from "../../lib/units";

interface TopBarProps {
  mode: ThemeMode;
  onToggleTheme: () => void;
  onOpenCommand: () => void;
  menuOpen: boolean;
  onMenuOpenChange: (open: boolean) => void;
  route: Route;
  repoUrl: string;
  liveUrl: string;
}

const routeDescriptions: Record<Route, string> = {
  overview: "Today's forecasts by city",
  methodology: "How the forecast is built",
  lab: "Paper accounts and trades",
};

/** The three routes as a pill group; one indicator slides under the current
    route (a CSS transform transition, so it never waits on a frame callback). */
function PrimaryNav({ route }: { route: Route }) {
  const navRef = useRef<HTMLElement>(null);
  const pillRef = useRef<HTMLSpanElement>(null);
  const previous = useRef<{ x: number; w: number } | null>(null);
  const [pill, setPill] = useState<{ x: number; w: number } | null>(null);

  useLayoutEffect(() => {
    const nav = navRef.current;
    if (!nav) return;
    const measure = () => {
      const active = nav.querySelector<HTMLElement>('[aria-current="page"]');
      setPill(active ? { x: active.offsetLeft, w: active.offsetWidth } : null);
    };
    measure();
    // Webfont swaps and zoom change link widths after the first measurement.
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(nav);
    return () => observer?.disconnect();
  }, [route]);

  // The pill takes its new size at once and plays from the old one, so the
  // slide is transform-only. Without Web Animations it simply lands.
  useLayoutEffect(() => {
    const from = previous.current;
    previous.current = pill;
    const el = pillRef.current;
    if (!el?.animate || !from?.w || !pill?.w || (from.x === pill.x && from.w === pill.w)) return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    el.animate(
      [{ transform: `translateX(${from.x}px) scaleX(${from.w / pill.w})` }, { transform: `translateX(${pill.x}px)` }],
      { duration: 260, easing: getComputedStyle(el).getPropertyValue("--ease-enter") },
    );
  }, [pill]);

  return (
    <nav
      ref={navRef}
      aria-label="Primary navigation"
      className="relative hidden items-center rounded-full bg-default/70 p-1 md:flex"
    >
      <span
        ref={pillRef}
        aria-hidden="true"
        className="nav-indicator absolute left-0 top-1 h-[calc(100%-0.5rem)] rounded-full bg-segment shadow-surface"
        style={{
          width: pill?.w ?? 0,
          transform: `translateX(${pill?.x ?? 0}px)`,
          opacity: pill ? 1 : 0,
        }}
      />
      {ROUTES.map((item) => (
        <a
          key={item.id}
          href={`#/${item.id}`}
          aria-current={route === item.id ? "page" : undefined}
          className={`relative z-10 rounded-full px-4 py-1.5 text-sm font-medium no-underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--focus)] ${
            route === item.id ? "text-foreground" : "text-muted hover:text-foreground"
          }`}
        >
          {item.label}
        </a>
      ))}
    </nav>
  );
}

/** °F / °C switch. Every published figure is Fahrenheit; this only changes how
    temperatures are displayed, and the choice persists per browser. */
function UnitToggle() {
  const { unit, setUnit } = useTemperature();
  return (
    <Segment
      aria-label="Temperature unit"
      size="sm"
      selectedKey={unit}
      onSelectionChange={(key) => setUnit(key as TempUnit)}
      className="shrink-0"
    >
      <Segment.Item id="F" aria-label="Fahrenheit">°F</Segment.Item>
      <Segment.Item id="C" aria-label="Celsius">°C</Segment.Item>
    </Segment>
  );
}

export function TopBar({ mode, onToggleTheme, onOpenCommand, menuOpen, onMenuOpenChange, route, repoUrl }: TopBarProps) {
  // The menu is a phone affordance; close it if the window grows to the
  // width where the inline navigation appears.
  useEffect(() => {
    if (!menuOpen) return;
    const desktop = window.matchMedia("(min-width: 768px)");
    const closeOnDesktop = () => { if (desktop.matches) onMenuOpenChange(false); };
    closeOnDesktop();
    desktop.addEventListener("change", closeOnDesktop);
    return () => desktop.removeEventListener("change", closeOnDesktop);
  }, [menuOpen, onMenuOpenChange]);

  const themeLabel = mode === "dark" ? "Switch to light theme" : "Switch to dark theme";

  return (
    <header className="sticky top-0 z-40 border-b border-separator bg-background/80 backdrop-blur-xl">
      <div className="mx-auto flex h-15 w-full max-w-6xl items-center gap-2 px-4 sm:px-6 lg:px-8">
        {/* aria-controls is conditional because the mobile <nav> it names only
            exists while the menu is open; a reference to a missing id is broken. */}
        <button
          type="button"
          aria-label={menuOpen ? "Close menu" : "Open menu"}
          aria-expanded={menuOpen}
          aria-haspopup="dialog"
          aria-controls={menuOpen ? "mobile-navigation" : undefined}
          className={buttonVariants({ variant: "ghost", isIconOnly: true, className: "-ml-2 md:hidden" })}
          onClick={() => onMenuOpenChange(!menuOpen)}
        >
          <span className={`menu-mark ${menuOpen ? "is-open" : ""}`} aria-hidden="true"><span /><span /></span>
        </button>

        <a href="#/overview" aria-label="WeatherEdge overview" className="mr-2 flex min-h-11 min-w-0 items-center gap-2.5 rounded-lg no-underline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[color:var(--focus)]">
          <img
            src={`${import.meta.env.BASE_URL}favicon.svg`}
            alt=""
            aria-hidden="true"
            width={28}
            height={28}
            className="size-7 shrink-0 rounded-lg"
          />
          <span className="hidden text-[15px] font-semibold tracking-tight text-foreground min-[360px]:inline">
            WeatherEdge
          </span>
        </a>

        <PrimaryNav route={route} />

        <div className="ml-auto flex min-w-0 items-center gap-1">
          <button
            type="button"
            onClick={onOpenCommand}
            className="hidden h-9 cursor-pointer items-center gap-2 rounded-full bg-default/70 pl-3 pr-1.5 text-sm text-muted transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--focus)] md:inline-flex"
          >
            <Icon icon="solar:magnifer-linear" className="size-4" aria-hidden="true" />
            <span>Search</span>
            <kbd className="rounded-full bg-surface px-2 py-0.5 font-sans text-xs text-muted">⌘K</kbd>
          </button>
          <Tooltip delay={600}>
            <Button isIconOnly variant="ghost" aria-label="Open command palette" className="md:hidden" onPress={onOpenCommand}>
              <Icon icon="solar:magnifer-linear" aria-hidden="true" />
            </Button>
            <Tooltip.Content>Search</Tooltip.Content>
          </Tooltip>
          <UnitToggle />
          <Tooltip delay={600}>
            <Button isIconOnly variant="ghost" aria-label={themeLabel} onPress={onToggleTheme}>
              <Icon icon={mode === "dark" ? "solar:sun-2-linear" : "solar:moon-linear"} aria-hidden="true" />
            </Button>
            <Tooltip.Content>{mode === "dark" ? "Light mode" : "Dark mode"}</Tooltip.Content>
          </Tooltip>
          <a
            href={repoUrl}
            target="_blank"
            rel="noreferrer"
            aria-label="WeatherEdge source on GitHub"
            className={buttonVariants({ variant: "ghost", className: "hidden gap-1.5 px-2.5 md:inline-flex" })}
          >
            <Icon icon="solar:code-square-linear" aria-hidden="true" />
            <span>Source</span>
          </a>
        </div>
      </div>

      {/* Keep the overlay mounted so React Aria can finish its exit animation
          and restore focus. Its portal also escapes the sticky header's blur. */}
      <Modal>
        <Modal.Backdrop isOpen={menuOpen} onOpenChange={onMenuOpenChange} variant="blur">
          <Modal.Container placement="top" size="lg" className="mobile-menu-container">
            <Modal.Dialog className="mobile-menu-dialog">
              <div className="flex items-center justify-between gap-3 px-4 pt-3">
                <Modal.Heading className="text-sm font-medium text-muted">Navigation</Modal.Heading>
                <button
                  type="button"
                  aria-label="Close menu"
                  className={buttonVariants({ variant: "ghost", isIconOnly: true })}
                  onClick={() => onMenuOpenChange(false)}
                >
                  <span className="menu-mark is-open" aria-hidden="true"><span /><span /></span>
                </button>
              </div>
              <Modal.Body className="m-0 p-2">
                <nav id="mobile-navigation" aria-label="Mobile navigation">
                  <ul className="grid gap-1">
                    {ROUTES.map((item) => (
                      <li key={item.id} className="mobile-menu-item">
                        <a
                          href={`#/${item.id}`}
                          aria-current={route === item.id ? "page" : undefined}
                          onClick={(event) => {
                            onMenuOpenChange(false);
                            if (item.id === route) {
                              event.preventDefault();
                            }
                          }}
                          className={`flex min-h-14 items-center gap-3 rounded-2xl px-3 py-2.5 no-underline transition-colors focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[color:var(--focus)] ${
                            route === item.id ? "bg-accent-soft text-[color:var(--accent-text)]" : "text-foreground hover:bg-default"
                          }`}
                        >
                          <Icon icon={item.icon} className="size-5 shrink-0" aria-hidden="true" />
                          <span className="min-w-0 flex-1">
                            <span className="block text-sm font-medium">{item.label}</span>
                            <span className="block text-xs text-muted" aria-hidden="true">{routeDescriptions[item.id]}</span>
                          </span>
                        </a>
                      </li>
                    ))}
                  </ul>
                  <div className="mt-1 border-t border-separator pt-1">
                    <a
                      href={repoUrl}
                      target="_blank"
                      rel="noreferrer"
                      className="flex min-h-11 items-center gap-3 rounded-2xl px-3 text-sm text-muted no-underline transition-colors hover:bg-default focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[color:var(--focus)]"
                    >
                      <Icon icon="solar:code-square-linear" className="size-5 shrink-0" aria-hidden="true" /> Source on GitHub
                    </a>
                  </div>
                </nav>
              </Modal.Body>
            </Modal.Dialog>
          </Modal.Container>
        </Modal.Backdrop>
      </Modal>
    </header>
  );
}
