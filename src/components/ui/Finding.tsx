import { Icon } from "@iconify/react";
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
    <aside className={`mt-5 flex gap-3 rounded-xl bg-surface-secondary/70 p-4 ring-1 ring-border/50 ${className ?? ""}`}>
      <span className="mt-0.5 grid size-6 shrink-0 place-items-center rounded-md bg-accent-soft text-accent ring-1 ring-accent/20">
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
