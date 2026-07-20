import { Icon } from "@iconify/react/offline";
import type { ReactNode } from "react";

interface FindingProps {
  children: ReactNode;
  /** eyebrow label, defaults to "Finding" */
  label?: string;
  icon?: string;
  className?: string;
}

/** Interpretive callout placed after a chart or section: states in plain words
    what the data shows, with the key numbers inline. `<strong>` inside is
    promoted to foreground so the load-bearing figures pop. */
export function Finding({ children, label = "Finding", icon = "solar:document-text-bold", className }: FindingProps) {
  return (
    <aside className={`flex gap-3 border-l-2 border-accent/40 py-1 pl-4 ${className ?? ""}`}>
      <span className="mt-0.5 grid size-6 shrink-0 place-items-center rounded-md bg-accent-soft text-accent">
        <Icon icon={icon} className="size-3.5" aria-hidden="true" />
      </span>
      <div className="min-w-0">
        <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.18em] text-[color:var(--accent-text)]">
          {label}
        </p>
        <div className="mt-1 text-sm leading-relaxed text-muted [&_strong]:font-semibold [&_strong]:text-foreground">
          {children}
        </div>
      </div>
    </aside>
  );
}
