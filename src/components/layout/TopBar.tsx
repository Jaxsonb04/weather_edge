import { Navbar } from "@heroui-pro/react";
import { Button, Tooltip } from "@heroui/react";
import { Icon } from "@iconify/react";
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

export function TopBar({ mode, onToggleTheme, onOpenCommand, route, repoUrl, liveUrl }: TopBarProps) {
  return (
    <Navbar
      position="sticky"
      maxWidth="xl"
      className="border-b border-border/60 bg-background/70 backdrop-blur-xl"
    >
      <Navbar.Header>
        <Navbar.Brand className="gap-2.5">
          <a href="#/overview" className="flex items-center gap-2.5 no-underline">
            <span className="relative grid size-7 place-items-center rounded-lg bg-accent-soft text-accent ring-1 ring-accent/25">
              <Icon icon="solar:temperature-bold" className="size-4" />
            </span>
            <span className="font-display text-[15px] font-semibold tracking-tight text-foreground">
              Weather<span className="temp-text">Edge</span>
            </span>
          </a>
        </Navbar.Brand>

        <Navbar.Content className="ml-6 hidden lg:flex">
          {ROUTES.map((r) => (
            <Navbar.Item key={r.id} href={`#/${r.id}`} isCurrent={route === r.id} className="no-underline">
              {r.label}
            </Navbar.Item>
          ))}
        </Navbar.Content>

        <Navbar.Spacer />

        <div className="flex items-center gap-1.5">
          <Button isIconOnly variant="ghost" size="sm" className="lg:hidden" aria-label="Open command palette" onPress={onOpenCommand}>
            <Icon icon="solar:magnifer-linear" className="size-4" />
          </Button>
          <Button variant="outline" size="sm" className="hidden gap-2 text-muted lg:inline-flex" onPress={onOpenCommand}>
            <Icon icon="solar:magnifer-linear" className="size-4" />
            <span className="text-sm">Search</span>
            <kbd className="rounded border border-border bg-surface-secondary px-1.5 py-0.5 font-mono text-[10px] text-muted">⌘K</kbd>
          </Button>

          <Tooltip delay={0}>
            <Button isIconOnly variant="ghost" size="sm" onPress={onToggleTheme} aria-label="Toggle theme">
              <Icon icon={mode === "dark" ? "solar:sun-2-bold" : "solar:moon-stars-bold"} className="size-4" />
            </Button>
            <Tooltip.Content showArrow placement="bottom">
              <Tooltip.Arrow />
              <p className="text-xs">{mode === "dark" ? "Light mode" : "Dark mode"}</p>
            </Tooltip.Content>
          </Tooltip>

          <LinkButton href={liveUrl} variant="ghost" size="sm" className="hidden gap-1.5 sm:inline-flex">
            <Icon icon="solar:square-top-down-linear" className="size-4" /> Live
          </LinkButton>
          <LinkButton href={repoUrl} variant="primary" size="sm" className="gap-1.5">
            <Icon icon="mdi:github" className="size-4" /> <span className="hidden sm:inline">Source</span>
          </LinkButton>
        </div>
      </Navbar.Header>
    </Navbar>
  );
}
