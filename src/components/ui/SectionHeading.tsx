import { Reveal } from "./Reveal";

interface SectionHeadingProps {
  eyebrow: string;
  title: string;
  sub?: string;
  index?: string;
}

/** Editorial section header: monospace index + eyebrow, display title, sub. */
export function SectionHeading({ eyebrow, title, sub, index }: SectionHeadingProps) {
  return (
    <Reveal className="mb-8">
      <div className="mb-2 flex items-center gap-2.5">
        {index && <span className="font-mono text-xs font-medium text-[color:var(--accent-text)]">{index}</span>}
        <span className="h-px w-6 bg-accent/50" />
        <span className="font-mono text-[11px] font-semibold uppercase tracking-[0.18em] text-[color:var(--accent-text)]">
          {eyebrow}
        </span>
      </div>
      <h2 className="max-w-3xl font-display text-[1.6rem] font-semibold tracking-tight text-balance sm:text-[2rem]">
        {title}
      </h2>
      {sub && <p className="mt-2 max-w-2xl text-sm leading-relaxed text-muted">{sub}</p>}
    </Reveal>
  );
}
