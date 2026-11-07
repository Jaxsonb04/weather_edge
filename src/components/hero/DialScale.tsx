import { useTemperature } from "../../lib/units";

const LOW_F = 0;
const HIGH_F = 110;
const LABELS_F = [0, 20, 40, 60, 80, 100];
/** Celsius labels placed at their Fahrenheit position: -10°C is 14°F … 40°C is 104°F. */
const LABELS_C = [-10, 0, 10, 20, 30, 40];

const at = (tempF: number) => `${(Math.min(HIGH_F, Math.max(LOW_F, tempF)) - LOW_F) / (HIGH_F - LOW_F) * 100}%`;

/** A fixed temperature scale under the big reading. It never rescales, so the
    needle's position is the temperature itself: it sweeps up on arrival and
    between days and cities, and the gap to the market tick is the model-market
    gap. Values past the ends sit on the end of the scale. Positions are always
    computed in Fahrenheit; only the printed labels follow the chosen unit. */
export function DialScale({ value, market }: { value: number; market?: number | null }) {
  const temperature = useTemperature();
  const labels = temperature.unit === "C"
    ? LABELS_C.map((c) => ({ text: c, f: (c * 9) / 5 + 32 }))
    : LABELS_F.map((f) => ({ text: f, f }));
  const span = `${temperature.temp(LOW_F, 0)} to ${temperature.temp(HIGH_F, 0, true)}`;
  return (
    <div
      role="img"
      aria-label={`Scale from ${span}: model ${temperature.temp(value, 0)}${market == null ? "" : `, market ${temperature.temp(market, 0)}`}`}
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
        {labels.map((label) => (
          <span
            key={label.text}
            className={`tnum absolute ${label.f <= LOW_F ? "" : "-translate-x-1/2"}`}
            style={{ left: at(label.f) }}
          >
            {label.text}
          </span>
        ))}
      </div>
    </div>
  );
}
