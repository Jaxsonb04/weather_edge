import type { ReactNode } from "react";

interface StatProps {
  label: string;
  value: string;
  tone?: "pos" | "neg" | "default";
  /** A small second line under the value (sample size, date). */
  note?: string;
  info?: ReactNode;
}

/** A labelled figure: quiet label, prominent tabular value. */
export function Stat({ label, value, tone = "default", note, info }: StatProps) {
  const toneClass = tone === "pos" ? "text-success" : tone === "neg" ? "text-danger" : "text-foreground";
  return (
    <div className="min-w-0">
      <p className="flex items-center gap-0.5 text-xs text-muted">
        {label}
        {info}
      </p>
      <p className={`tnum mt-0.5 text-lg font-semibold tracking-tight ${toneClass}`}>{value}</p>
      {note && <p className="tnum text-xs text-muted">{note}</p>}
    </div>
  );
}
