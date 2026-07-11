import { useEffect, useRef, useState } from "react";
import { Icon } from "@iconify/react/offline";
import { LinkButton } from "../ui/LinkButton";
import { ROUTES, type Route } from "../../lib/useHashRoute";
import type { ThemeMode } from "../../lib/theme";

interface TopBarProps {
  mode: ThemeMode;
  onToggleTheme: () => void;
  onOpenCommand: () => void;
  route: Route;
  repoUrl: string;
  liveUrl: string;
}

const iconButton =
  "inline-flex size-11 cursor-pointer items-center justify-center rounded-lg text-muted transition-colors duration-200 hover:bg-default hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--focus)] active:bg-default-hover";

export function TopBar({ mode, onToggleTheme, onOpenCommand, route, repoUrl, liveUrl }: TopBarProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const mobileNavRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const nav = mobileNavRef.current;
    const links = nav ? [...nav.querySelectorAll<HTMLAnchorElement>("a[href]")] : [];
    links[0]?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setMenuOpen(false);
        menuButtonRef.current?.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [menuOpen]);

  return (
    <header className="sticky top-0 z-40 border-b border-border/60 bg-background/90 backdrop-blur-xl">
      <div className="mx-auto flex h-16 w-full max-w-6xl items-center gap-3 px-5 sm:px-8">
        <button
          ref={menuButtonRef}
          type="button"
          aria-label={menuOpen ? "Close menu" : "Open menu"}
          aria-expanded={menuOpen}
          aria-controls="mobile-navigation"
          className={`${iconButton} -ml-2 lg:hidden`}
          onClick={() => setMenuOpen((open) => !open)}
        >
          <Icon icon={menuOpen ? "solar:close-circle-bold" : "solar:widget-5-bold"} className="size-5" aria-hidden="true" />
        </button>

        <a href="#/overview" aria-label="WeatherEdge overview" className="flex min-w-0 items-center gap-2.5 no-underline">
          <span className="relative grid size-7 shrink-0 place-items-center rounded-lg bg-accent-soft text-accent ring-1 ring-accent/25">
            <Icon icon="solar:temperature-bold" className="size-4" aria-hidden="true" />
          </span>
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

      {menuOpen && (
        <nav ref={mobileNavRef} id="mobile-navigation" aria-label="Mobile navigation" className="border-t border-border/60 px-5 py-3 lg:hidden">
          <ul className="mx-auto grid w-full max-w-6xl gap-1">
            {ROUTES.map((item) => (
              <li key={item.id}>
                <a
                  href={`#/${item.id}`}
                  aria-current={route === item.id ? "page" : undefined}
                  onClick={(event) => {
                    setMenuOpen(false);
                    if (item.id === route) {
                      event.preventDefault();
                      menuButtonRef.current?.focus();
                    }
                  }}
                  className={`flex min-h-11 items-center gap-3 rounded-xl px-3 py-2.5 text-base font-medium no-underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--focus)] ${
                    route === item.id ? "bg-accent-soft text-[color:var(--accent-text)]" : "text-foreground hover:bg-default"
                  }`}
                >
                  <Icon icon={item.icon} className="size-5" aria-hidden="true" />
                  {item.label}
                </a>
              </li>
            ))}
            <li className="mt-1 border-t border-border/60 pt-1">
              <a href={liveUrl} target="_blank" rel="noreferrer" className="flex min-h-11 items-center gap-3 rounded-xl px-3 py-2.5 text-base text-muted no-underline hover:bg-default focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--focus)]">
                <Icon icon="solar:square-top-down-bold" className="size-5" aria-hidden="true" /> Live dashboard
              </a>
            </li>
            <li>
              <a href={repoUrl} target="_blank" rel="noreferrer" className="flex min-h-11 items-center gap-3 rounded-xl px-3 py-2.5 text-base text-muted no-underline hover:bg-default focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--focus)]">
                <Icon icon="solar:code-square-bold" className="size-5" aria-hidden="true" /> Source on GitHub
              </a>
            </li>
          </ul>
        </nav>
      )}
    </header>
  );
}
