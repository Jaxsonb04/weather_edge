import { Icon } from "@iconify/react/offline";
import type { ReactNode } from "react";

interface FindingProps {
  children: ReactNode;
  /** Short label above the text, defaults to "Finding". */
  label?: string;
  icon?: string;
  className?: string;
}

/** A compact interpretive note: what the data shows, in one or two sentences,
    with the key numbers in `<strong>`. */
export function Finding({ children, label = "Finding", icon = "solar:lightbulb-bolt-linear", className }: FindingProps) {
  return (
    <aside className={`flex gap-3 rounded-2xl bg-surface-secondary px-4 py-3 ${className ?? ""}`.trim()}>
      <Icon icon={icon} className="mt-0.5 size-4 shrink-0 text-[color:var(--accent-text)]" aria-hidden="true" />
      <div className="min-w-0">
        <p className="text-xs font-medium text-foreground">{label}</p>
        <div className="mt-0.5 text-sm leading-relaxed text-muted [&_strong]:font-semibold [&_strong]:text-foreground">
          {children}
        </div>
      </div>
    </aside>
  );
}
