import type { ReactNode } from "react";
import { Button } from "@heroui/react/button";
import { Popover } from "@heroui/react/popover";
import { Icon } from "@iconify/react/offline";

interface InfoTipProps {
  /** What the explanation is about, for the trigger's accessible name. */
  label: string;
  children: ReactNode;
  /** Optional bold first line inside the popover. */
  title?: string;
  placement?: "top" | "bottom" | "left" | "right";
  className?: string;
}

/** The explanation behind a number, one tap away. Caveats and definitions that
    used to sit on screen as paragraphs live here, so the screen leads with the
    figure itself. A Popover (not a Tooltip) so it also opens on touch. */
export function InfoTip({ label, children, title, placement = "top", className }: InfoTipProps) {
  return (
    <Popover>
      <Button
        isIconOnly
        size="sm"
        variant="ghost"
        aria-label={`About ${label}`}
        className={`size-6 min-w-6 text-muted hover:text-foreground ${className ?? ""}`.trim()}
      >
        <Icon icon="solar:info-circle-linear" aria-hidden="true" />
      </Button>
      <Popover.Content placement={placement} className="max-w-72">
        <Popover.Dialog className="p-3">
          <Popover.Arrow />
          {title && <Popover.Heading className="mb-1 text-sm font-semibold text-foreground">{title}</Popover.Heading>}
          <div className="text-xs leading-relaxed text-muted [&_strong]:font-semibold [&_strong]:text-foreground">
            {children}
          </div>
        </Popover.Dialog>
      </Popover.Content>
    </Popover>
  );
}
