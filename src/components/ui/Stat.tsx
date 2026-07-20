interface StatProps {
  label: string;
  value: string;
  tone?: "pos" | "neg" | "default";
}

export function Stat({ label, value, tone = "default" }: StatProps) {
  const toneClass = tone === "pos" ? "text-success" : tone === "neg" ? "text-danger" : "text-foreground";
  return (
    <div className="min-w-0">
      <p className="text-[11px] uppercase tracking-wide text-muted">{label}</p>
      <p className={`tnum font-display text-lg font-semibold ${toneClass}`}>{value}</p>
    </div>
  );
}
