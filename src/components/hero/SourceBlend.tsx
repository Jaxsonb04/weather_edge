import { useEffect, useState } from "react";
import { InfoTip } from "../ui/InfoTip";
import { num, type Target } from "../../lib/data";
import { useTemperature } from "../../lib/units";

const SOURCE_META: Record<string, { label: string; color: string }> = {
  google_high_f: { label: "Google", color: "var(--temp-warm)" },
  nws_high_f: { label: "NWS", color: "var(--temp-cold)" },
  open_meteo_high_f: { label: "Open-Meteo", color: "var(--temp-mild)" },
  history_high_f: { label: "SFO history", color: "var(--temp-hot)" },
};

/** Mirrors `max_source_spread_f` on BOTH shipped profiles
    (trading/sfo_kalshi_quant/config.py, LIVE_PROFILE_OVERRIDES and
    RESEARCH_PROFILE_OVERRIDES). Re-derived 2026-09-07 from 10 when the
    disagreement statistic became the range over BIAS-CORRECTED forecasts: a
    provider or model that is reliably several degrees off is a known offset the
    blend already corrects, not evidence that today is uncertain, so the number
    it is compared against had to move with it. Keep this in step with the
    profiles -- it is published as a factual claim about the engine. */
const SOURCE_SPREAD_GATE_F = 7.3;

/** Each live source plotted on a shared min–max temperature axis, so the
    spread between providers reads instantly (the gate's core input). */
export function SourceBlend({ target }: { target: Target }) {
  const temperature = useTemperature();
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
  const wide = spread > SOURCE_SPREAD_GATE_F;

  return (
    <div className="flex flex-col gap-2.5">
      {entries.map(([k, meta], i) => {
        const v = sources[k];
        const left = ((v - lo) / (hi - lo)) * 100;
        return (
          <div key={k} className="flex items-center gap-3">
            <span className="w-20 shrink-0 text-xs text-muted">{meta.label}</span>
            <div className="relative h-1.5 flex-1 rounded-full bg-default">
              <span
                className={`pop absolute top-1/2 size-2.5 -translate-y-1/2 rounded-full ring-2 ring-surface ${mounted ? "is-in" : ""}`}
                style={{ left: `${left}%`, marginLeft: "-5px", background: meta.color, transitionDelay: `${0.1 + i * 0.08}s` }}
              />
            </div>
            <span className="tnum w-12 shrink-0 text-right text-xs font-medium">{temperature.temp(v)}</span>
          </div>
        );
      })}
      <div className={`flex w-fit items-center gap-1 pt-1 text-xs ${wide ? "text-warning" : "text-muted"}`}>
        <span className="tnum font-medium">Source spread {temperature.diff(spread)}</span>
        {wide && <span>· over the {temperature.diff(SOURCE_SPREAD_GATE_F)} gate</span>}
        <InfoTip label="the source spread gate">
          Disagreement is measured after each source's known standing offset is removed. When what is left exceeds{" "}
          {temperature.diff(SOURCE_SPREAD_GATE_F, 1, true)}, the paper engine refuses to size a new position.
        </InfoTip>
      </div>
    </div>
  );
}
