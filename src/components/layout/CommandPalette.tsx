import { useEffect } from "react";
import { Command } from "@heroui-pro/react/command";
import { Icon } from "@iconify/react/offline";
import { ROUTES, type Route } from "../../lib/useHashRoute";
import "../../styles/pro-command.css";

export interface CommandPaletteProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onToggleTheme: () => void;
  onNavigate: (route: Route) => void;
  repoUrl: string;
  liveUrl: string;
}

/** ⌘K / Ctrl-K command palette. Renders into a portal via Command.Backdrop. */
export function CommandPalette({ open, onOpenChange, onToggleTheme, onNavigate, repoUrl, liveUrl }: CommandPaletteProps) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        onOpenChange(!open);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onOpenChange]);

  const go = (r: Route) => {
    onNavigate(r);
    onOpenChange(false);
  };
  const openUrl = (url: string) => {
    window.open(url, "_blank", "noreferrer");
    onOpenChange(false);
  };

  return (
    <Command>
      <Command.Backdrop isOpen={open} onOpenChange={onOpenChange} variant="blur">
        <Command.Container size="md">
          <Command.Dialog>
            <Command.InputGroup>
              <Command.InputGroup.Prefix>
                <Icon icon="solar:magnifer-bold" className="size-4 text-muted" />
              </Command.InputGroup.Prefix>
              <Command.InputGroup.Input placeholder="Go to a view, open links, toggle theme…" />
              <Command.InputGroup.ClearButton />
            </Command.InputGroup>
            <Command.List>
              <Command.Group heading="Views">
                {ROUTES.map((r) => (
                  <Command.Item key={r.id} textValue={r.label} onAction={() => go(r.id)}>
                    <Icon icon={r.icon} className="size-4 text-muted" />
                    <span>{r.label}</span>
                  </Command.Item>
                ))}
              </Command.Group>
              <Command.Group heading="Actions">
                <Command.Item textValue="Toggle theme" onAction={() => { onToggleTheme(); onOpenChange(false); }}>
                  <Icon icon="solar:moon-stars-bold" className="size-4 text-muted" />
                  <span>Toggle light / dark</span>
                </Command.Item>
                <Command.Item
                  textValue="Copy flagship SFO market ticker"
                  onAction={() => { navigator.clipboard?.writeText("KXHIGHTSFO"); onOpenChange(false); }}
                >
                  <Icon icon="solar:copy-bold" className="size-4 text-muted" />
                  <span>Copy flagship ticker (SFO)</span>
                </Command.Item>
              </Command.Group>
              <Command.Group heading="Links">
                <Command.Item textValue="Open live dashboard" onAction={() => openUrl(liveUrl)}>
                  <Icon icon="solar:square-top-down-bold" className="size-4 text-muted" />
                  <span>Open live dashboard</span>
                </Command.Item>
                <Command.Item textValue="View source on GitHub" onAction={() => openUrl(repoUrl)}>
                  <Icon icon="solar:code-square-bold" className="size-4 text-muted" />
                  <span>View source on GitHub</span>
                </Command.Item>
              </Command.Group>
            </Command.List>
          </Command.Dialog>
        </Command.Container>
      </Command.Backdrop>
    </Command>
  );
}
