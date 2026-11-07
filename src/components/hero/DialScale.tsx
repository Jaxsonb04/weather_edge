const LOW_F = 0;
const HIGH_F = 110;
const LABELS = [0, 20, 40, 60, 80, 100];

const at = (tempF: number) => `${(Math.min(HIGH_F, Math.max(LOW_F, tempF)) - LOW_F) / (HIGH_F - LOW_F) * 100}%`;

/** A fixed Fahrenheit scale under the big reading. It never rescales, so the
    needle's position is the temperature itself: it sweeps up on arrival and
    between days and cities, and the gap to the market tick is the model-market
    gap. Values past the ends sit on the end of the scale. */
export function DialScale({ value, market }: { value: number; market?: number | null }) {
  return (
    <div
      role="img"
      aria-label={`Scale from ${LOW_F} to ${HIGH_F}°F: model ${Math.round(value)}°${market == null ? "" : `, market ${Math.round(market)}°`}`}
    >
      <div className="dial-scale relative h-6 overflow-hidden">
        {market != null && (
          <span className="absolute inset-y-1 left-0 w-full" style={{ transform: `translateX(${at(market)})` }}>
            <span className="block h-full w-0.5 -translate-x-1/2 rounded-full bg-[color:var(--series-market)]" />
          </span>
        )}
        <span className="dial-needle absolute inset-y-0 left-0 w-full" style={{ "--at": at(value) } as React.CSSProperties}>
          <span className="block h-full w-0.5 -translate-x-1/2 rounded-full bg-accent" />
        </span>
      </div>
      <div className="relative mt-1 h-4 text-xs text-muted" aria-hidden="true">
        {LABELS.map((label) => (
          <span key={label} className="tnum absolute -translate-x-1/2 first:translate-x-0" style={{ left: at(label) }}>
            {label}
          </span>
        ))}
      </div>
    </div>
  );
}
