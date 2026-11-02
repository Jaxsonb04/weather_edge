import type { ReactNode } from "react";

interface SectionHeadingProps {
  title: string;
  /** One line at most; explanations go in `info`. */
  sub?: ReactNode;
  /** An InfoTip (or other small control) placed beside the title. */
  info?: ReactNode;
  /** Right-aligned controls: a Segment, a Select, a link. */
  action?: ReactNode;
  id?: string;
  /** Heading level; sections inside a tab panel use h3. */
  as?: "h2" | "h3";
  className?: string;
}

/** Section header: a short title, an optional single line, and controls on the
    same row. Replaces the old numbered eyebrow + paragraph pattern. */
export function SectionHeading({ title, sub, info, action, id, as: Tag = "h2", className }: SectionHeadingProps) {
  return (
    <div className={`mb-4 flex flex-wrap items-end justify-between gap-x-6 gap-y-3 ${className ?? ""}`.trim()}>
      <div className="flex min-w-0 flex-col gap-1">
        <div className="flex items-center gap-1">
          <Tag id={id} className="text-lg font-semibold tracking-tight text-foreground sm:text-xl">
            {title}
          </Tag>
          {info}
        </div>
        {sub && <p className="text-sm text-muted">{sub}</p>}
      </div>
      {action && <div className="flex shrink-0 items-center gap-2">{action}</div>}
    </div>
  );
}
