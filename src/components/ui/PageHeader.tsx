import { Icon } from "@iconify/react";
import { Reveal } from "./Reveal";

interface PageHeaderProps {
  eyebrow: string;
  title: string;
  sub: string;
  icon: string;
}

/** Hero-less page header for the secondary routes (Methodology / Strategy Lab). */
export function PageHeader({ eyebrow, title, sub, icon }: PageHeaderProps) {
  return (
    <header className="hero-glow relative overflow-hidden border-b border-border/60">
      <div className="grid-lines pointer-events-none absolute inset-0 opacity-40" />
      <div className="relative mx-auto w-full max-w-6xl px-5 py-14 sm:px-8 lg:py-16">
        <Reveal immediate>
          <div className="mb-3 flex items-center gap-2.5">
            <span className="grid size-7 place-items-center rounded-lg bg-accent-soft text-accent ring-1 ring-accent/25">
              <Icon icon={icon} className="size-4" />
            </span>
            <span className="font-mono text-[11px] font-semibold uppercase tracking-[0.18em] text-[color:var(--accent-text)]">
              {eyebrow}
            </span>
          </div>
          <h1 className="max-w-3xl font-display text-[2.2rem] font-bold leading-[1.05] tracking-tight text-balance sm:text-5xl">
            {title}
          </h1>
          <p className="mt-4 max-w-2xl text-pretty text-base leading-relaxed text-muted">{sub}</p>
        </Reveal>
      </div>
    </header>
  );
}
