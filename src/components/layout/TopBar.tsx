import { useEffect } from "react";
import { Modal } from "@heroui/react/modal";
import { Icon } from "@iconify/react/offline";
import { LinkButton } from "../ui/LinkButton";
import { ROUTES, type Route } from "../../lib/useHashRoute";
import type { ThemeMode } from "../../lib/theme";

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

const iconButton =
  "inline-flex size-11 cursor-pointer items-center justify-center rounded-lg text-muted transition-colors duration-200 hover:bg-default hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--focus)] active:bg-default-hover";

const routeDescriptions = {
  overview: "Forecasts, stations & market signals",
  methodology: "Models, calibration & evidence",
  lab: "Paper accounts & position history",
};

export function TopBar({ mode, onToggleTheme, onOpenCommand, menuOpen, onMenuOpenChange, route, repoUrl, liveUrl }: TopBarProps) {
  useEffect(() => {
    if (!menuOpen) return;
    const desktop = window.matchMedia("(min-width: 1024px)");
    const closeOnDesktop = () => { if (desktop.matches) onMenuOpenChange(false); };
    closeOnDesktop();
    desktop.addEventListener("change", closeOnDesktop);
    return () => desktop.removeEventListener("change", closeOnDesktop);
  }, [menuOpen, onMenuOpenChange]);

  return (
    <header className="sticky top-0 z-40 border-b border-border/60 bg-background/90 backdrop-blur-xl">
      <div className="mx-auto flex h-16 w-full max-w-6xl items-center gap-3 px-5 sm:px-8">
        {/* aria-controls is conditional because the mobile <nav> it names only
            exists while the menu is open; a reference to a missing id is broken. */}
        <button
          type="button"
          aria-label={menuOpen ? "Close menu" : "Open menu"}
          aria-expanded={menuOpen}
          aria-haspopup="dialog"
          aria-controls={menuOpen ? "mobile-navigation" : undefined}
          className={`${iconButton} -ml-2 lg:hidden`}
          onClick={() => onMenuOpenChange(!menuOpen)}
        >
          <span className={`menu-mark ${menuOpen ? "is-open" : ""}`} aria-hidden="true"><span /><span /></span>
        </button>

        <a href="#/overview" aria-label="WeatherEdge overview" className="flex min-w-0 items-center gap-2.5 no-underline">
          <img
            src={`${import.meta.env.BASE_URL}favicon.svg`}
            alt=""
            aria-hidden="true"
            className="size-7 shrink-0 rounded-lg"
          />
          <span className="hidden font-display text-[15px] font-semibold tracking-tight text-foreground min-[360px]:inline">
            Weather<span className="temp-text">Edge</span>
          </span>
        </a>

        <nav aria-label="Primary navigation" className="ml-5 hidden items-center gap-1 lg:flex">
          {ROUTES.map((item) => (
            <a
              key={item.id}
              href={`#/${item.id}`}
              aria-current={route === item.id ? "page" : undefined}
              className={`rounded-lg px-3 py-2 text-sm no-underline transition-colors duration-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--focus)] ${
                route === item.id ? "bg-default text-foreground" : "text-muted hover:text-foreground"
              }`}
            >
              {item.label}
            </a>
          ))}
        </nav>

        <div className="ml-auto flex min-w-0 items-center gap-1 sm:gap-1.5">
          <button type="button" className={`${iconButton} lg:hidden`} aria-label="Open command palette" onClick={onOpenCommand}>
            <Icon icon="solar:magnifer-bold" className="size-4" aria-hidden="true" />
          </button>
          <button
            type="button"
            className="hidden h-10 cursor-pointer items-center gap-2 rounded-lg border border-border bg-transparent px-3 text-muted transition-colors duration-200 hover:bg-default hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--focus)] lg:inline-flex"
            onClick={onOpenCommand}
          >
            <Icon icon="solar:magnifer-bold" className="size-4" aria-hidden="true" />
            <span className="text-sm">Search</span>
            <kbd className="rounded border border-border bg-surface-secondary px-1.5 py-0.5 font-mono text-[10px] text-muted">⌘K</kbd>
          </button>

          <button
            type="button"
            className={iconButton}
            onClick={onToggleTheme}
            aria-label={mode === "dark" ? "Switch to light theme" : "Switch to dark theme"}
            title={mode === "dark" ? "Light mode" : "Dark mode"}
          >
            <Icon icon={mode === "dark" ? "solar:sun-2-bold" : "solar:moon-stars-bold"} className="size-4" aria-hidden="true" />
          </button>

          <LinkButton href={liveUrl} variant="ghost" size="sm" className="hidden min-h-10 gap-1.5 sm:inline-flex">
            <Icon icon="solar:square-top-down-bold" className="size-4" aria-hidden="true" /> Live
          </LinkButton>
          <LinkButton
            href={repoUrl}
            aria-label="WeatherEdge source on GitHub"
            variant="primary"
            size="sm"
            className="min-h-10 gap-1.5"
          >
            <Icon icon="solar:code-square-bold" className="size-4" aria-hidden="true" /> <span className="hidden sm:inline">Source</span>
          </LinkButton>
        </div>
      </div>

      {/* Keep the overlay mounted so React Aria can finish its exit animation
          and restore focus. Its portal also escapes the sticky header's blur. */}
      <Modal>
        <Modal.Backdrop isOpen={menuOpen} onOpenChange={onMenuOpenChange} variant="blur" className="mobile-menu-backdrop">
          <Modal.Container placement="top" size="lg" className="mobile-menu-container">
            <Modal.Dialog className="mobile-menu-dialog">
              <div className="flex items-center justify-between gap-3 border-b border-border/60 px-5 py-3">
                <Modal.Heading className="font-mono text-[11px] uppercase tracking-[0.16em] text-muted">Navigation</Modal.Heading>
                <button type="button" aria-label="Close menu" className={iconButton} onClick={() => onMenuOpenChange(false)}>
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
                          className={`group flex min-h-16 items-center gap-3 rounded-xl px-4 py-3 no-underline transition-colors duration-150 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[color:var(--focus)] ${
                            route === item.id ? "bg-accent-soft text-[color:var(--accent-text)]" : "text-foreground hover:bg-default"
                          }`}
                        >
                          <Icon icon={item.icon} className="size-5 shrink-0" aria-hidden="true" />
                          <span className="min-w-0 flex-1">
                            <span className="block text-sm font-medium">{item.label}</span>
                            <span className="mt-0.5 block text-xs leading-relaxed text-muted" aria-hidden="true">{routeDescriptions[item.id]}</span>
                          </span>
                          <Icon icon="solar:arrow-right-bold" className="size-4 shrink-0 text-muted transition-transform duration-200 group-hover:translate-x-0.5 motion-reduce:transition-none" aria-hidden="true" />
                        </a>
                      </li>
                    ))}
                  </ul>
                  <div className="mt-2 grid grid-cols-2 gap-1 border-t border-border/60 pt-2">
                    <a href={liveUrl} target="_blank" rel="noreferrer" className="flex min-h-11 items-center justify-center gap-2 rounded-lg px-2 py-2 text-xs text-muted no-underline transition-colors hover:bg-default focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[color:var(--focus)]">
                      <Icon icon="solar:square-top-down-bold" className="size-4 shrink-0" aria-hidden="true" /> Live dashboard
                    </a>
                    <a href={repoUrl} target="_blank" rel="noreferrer" className="flex min-h-11 items-center justify-center gap-2 rounded-lg px-2 py-2 text-xs text-muted no-underline transition-colors hover:bg-default focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[color:var(--focus)]">
                      <Icon icon="solar:code-square-bold" className="size-4 shrink-0" aria-hidden="true" /> Source on GitHub
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
