import type { ReactNode } from "react";

interface PageHeaderProps {
  headingId: string;
  title: string;
  /** One short sentence. Anything longer belongs in an InfoTip. */
  sub?: ReactNode;
  /** Status chips shown above the title (freshness, paper-only, dates). */
  meta?: ReactNode;
  /** Right-aligned slot on wide screens (actions or a key figure). */
  aside?: ReactNode;
}

/** Compact route header for Methodology and the Strategy Lab: a status row, one
    heading and one line of context. No decoration, so the first number on the
    page sits above the fold. */
export function PageHeader({ headingId, title, sub, meta, aside }: PageHeaderProps) {
  return (
    <header className="mx-auto w-full max-w-6xl px-4 pb-6 pt-10 sm:px-6 sm:pt-14 lg:px-8">
      <div className="flex flex-col gap-6 lg:flex-row lg:items-end lg:justify-between">
        <div className="flex min-w-0 flex-col gap-3">
          {meta && <div className="flex flex-wrap items-center gap-2">{meta}</div>}
          <h1
            id={headingId}
            tabIndex={-1}
            className="scroll-mt-24 text-balance text-3xl font-semibold tracking-tight text-foreground focus:outline-none sm:text-4xl"
          >
            {title}
          </h1>
          {sub && <p className="max-w-2xl text-pretty text-sm leading-relaxed text-muted sm:text-base">{sub}</p>}
        </div>
        {aside && <div className="shrink-0">{aside}</div>}
      </div>
    </header>
  );
}
