import { useEffect, useState } from "react";
import { Tooltip } from "@heroui/react/tooltip";
import { Icon } from "@iconify/react/offline";
import { f1, num, type Target } from "../../lib/data";

const SOURCE_META: Record<string, { label: string; color: string }> = {
  google_high_f: { label: "Google", color: "var(--temp-warm)" },
  nws_high_f: { label: "NWS", color: "var(--temp-cold)" },
  open_meteo_high_f: { label: "Open-Meteo", color: "var(--temp-mild)" },
  history_high_f: { label: "SFO history", color: "var(--temp-hot)" },
};

/** Each live source plotted on a shared min–max temperature axis, so the
    spread between providers reads instantly (the gate's core input). */
export function SourceBlend({ target }: { target: Target }) {
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    const id = requestAnimationFrame(() => setMounted(true));
    return () => cancelAnimationFrame(id);
  }, []);

  const rawSources = target.forecast?.sources as unknown;
  const sources = (rawSources && typeof rawSources === "object" ? rawSources : {}) as Record<string, number>;
  const spread = num(target.forecast, "source_spread_f") ?? 0;
  const entries = Object.entries(SOURCE_META).filter(([k]) => typeof sources[k] === "number");
  if (!entries.length) return null;
  const vals = entries.map(([k]) => sources[k]);
  const lo = Math.min(...vals) - 1;
  const hi = Math.max(...vals) + 1;
  const wide = spread > 7;

  return (
    <div className="space-y-2.5">
      {entries.map(([k, meta], i) => {
        const v = sources[k];
        const left = ((v - lo) / (hi - lo)) * 100;
        return (
          <div key={k} className="flex items-center gap-3">
            <span className="w-20 shrink-0 text-xs text-muted">{meta.label}</span>
            <div className="relative h-1.5 flex-1 rounded-full bg-foreground/10">
              <span
                className={`pop absolute top-1/2 size-2.5 -translate-y-1/2 rounded-full ring-2 ring-surface ${mounted ? "is-in" : ""}`}
                style={{ left: `${left}%`, marginLeft: "-5px", background: meta.color, transitionDelay: `${0.1 + i * 0.08}s` }}
              />
            </div>
            <span className="tnum w-12 shrink-0 text-right text-xs font-medium">{f1(v)}</span>
          </div>
        );
      })}
      <Tooltip delay={0}>
        <button
          type="button"
          aria-label={`Source spread ${f1(spread)}. What is the spread gate?`}
          className={`flex w-fit items-center gap-1.5 pt-1 text-xs ${wide ? "text-warning" : "text-muted"}`}
        >
          <span className="tnum font-medium">Source spread {f1(spread)}</span>
          {wide && <span>· over 7° gate threshold</span>}
          <Icon icon="solar:info-circle-bold" className="size-3.5 opacity-70" />
        </button>
        <Tooltip.Content showArrow placement="top" className="max-w-[15rem]">
          <Tooltip.Arrow />
          <p className="text-xs">
            When providers disagree by more than 7°F the point blend is unreliable, so the engine refuses to size a trade.
          </p>
        </Tooltip.Content>
      </Tooltip>
    </div>
  );
}
