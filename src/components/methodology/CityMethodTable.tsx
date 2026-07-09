import { Skeleton } from "@heroui/react";
import { useCitiesData, type City } from "../../lib/data";

const TH = "px-3 py-2 text-left font-mono text-[10px] font-semibold uppercase tracking-wider text-muted";
const TD = "px-3 py-2.5 align-top";

/** "emos_wmean" → "EMOS · weighted mean". Tolerant of any string / missing. */
function methodLabel(method: string | undefined): string {
  if (!method) return "—";
  if (method === "emos_wmean") return "EMOS · weighted mean";
  return method.replace(/_/g, " ");
}

/** Strip the verbose "NWS Climatological Report (…)" wrapper down to the CLI
    station + WFO already inside the parens; fall back to the raw string. */
function settlementLabel(source: string | undefined): string {
  if (!source) return "—";
  const inner = source.match(/\(([^)]+)\)/)?.[1];
  return inner ?? (source.replace(/^NWS Climatological Report\s*/i, "").trim() || source);
}

/** The number of ensemble members backing a city's latest forecast (first
    forecast carrying an n_models count). */
function modelCount(city: City): number | null {
  for (const f of city.forecasts ?? []) {
    if (typeof f?.n_models === "number") return f.n_models;
  }
  return null;
}

function CityRow({ city }: { city: City }) {
  const flagship = city.has_full_blend === true;
  const method = city.forecasts?.find((f) => f?.method)?.method;
  const models = modelCount(city);
  return (
    <tr className={flagship ? "bg-accent-soft" : "odd:bg-foreground/[0.02]"}>
      <th scope="row" className={`${TD} text-left`}>
        <span className="flex items-center gap-2">
          <span className="font-medium text-foreground">{city.name ?? "—"}</span>
          {flagship && (
            <span className="rounded bg-accent-soft px-1.5 py-0.5 font-mono text-[9px] font-medium uppercase tracking-wide text-[color:var(--accent-text)]">
              flagship
            </span>
          )}
        </span>
      </th>
      <td className={`${TD} font-mono text-[11px] uppercase tracking-wider text-muted`}>
        {city.station_id ?? "—"}
      </td>
      <td className={`${TD} text-muted`}>
        <span className="text-foreground">{methodLabel(method)}</span>
        {flagship && <span className="text-muted"> + LSTM · source blend</span>}
      </td>
      <td className={`${TD} text-right tnum text-muted`}>{models ?? "—"}</td>
      <td className={`${TD} text-muted`}>{settlementLabel(city.settlement_source)}</td>
    </tr>
  );
}

function EmptyNote() {
  return (
    <p className="rounded-2xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted">
      Per-city method table appears once the fifteen-city coverage artifact is published on the next
      pipeline run.
    </p>
  );
}

/** Reference table of the production post-processing per settlement station:
    City · Station · Method · Models · Settlement report. Flagship (full blend)
    is flagged. Fed by cities_data.json; tolerant of missing fields and of the
    artifact not being published yet. Scrolls horizontally on small screens. */
export function CityMethodTable() {
  const { data, error } = useCitiesData();
  if (error && !data) return <EmptyNote />;
  if (!data) return <Skeleton className="h-72 w-full rounded-2xl" />;
  const cities = data.cities ?? [];
  if (!cities.length) return <EmptyNote />;

  return (
    <div className="overflow-x-auto rounded-2xl bg-surface-secondary/70 ring-1 ring-border/70">
      <table className="w-full min-w-[640px] border-collapse text-sm">
        <caption className="sr-only">
          Production forecasting method, ensemble size, and settlement report per city
        </caption>
        <thead>
          <tr className="border-b border-border/60">
            <th scope="col" className={TH}>City</th>
            <th scope="col" className={TH}>Station</th>
            <th scope="col" className={TH}>Method</th>
            <th scope="col" className={`${TH} text-right`}>Models</th>
            <th scope="col" className={TH}>Settlement report</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border/40">
          {cities.map((c) => (
            <CityRow key={c.slug ?? c.series_ticker ?? c.name} city={c} />
          ))}
        </tbody>
      </table>
    </div>
  );
}
