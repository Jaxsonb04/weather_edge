import { Skeleton } from "@heroui/react/skeleton";
import { describeMethod, useCitiesData, type City } from "../../lib/data";

const TH = "px-3 py-2 text-left text-xs font-medium text-muted";
const TD = "px-3 py-2.5 align-top";

/** The artifact's own method tag in plain English. The local variant fell
    through to the raw tag for anything that was not exactly "emos_wmean", which
    on a settlement day printed "emos wmean + intraday high-so-far update"
    verbatim in this table. */
const methodLabel = (method: string | undefined): string =>
  method ? describeMethod(method) : "—";

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
    <tr className={flagship ? "bg-accent-soft" : undefined}>
      <th scope="row" className={`${TD} text-left`}>
        <span className="flex items-center gap-2">
          <span className="font-medium text-foreground">{city.name ?? "—"}</span>
          {flagship && (
            <span className="rounded-full bg-surface px-2 py-0.5 text-xs font-medium text-[color:var(--accent-text)]">
              Flagship
            </span>
          )}
        </span>
      </th>
      <td className={`${TD} text-muted`}>
        {city.station_id ?? "—"}
      </td>
      <td className={`${TD} text-muted`}>
        <span className="text-foreground">{methodLabel(method)}</span>
        {flagship && <span className="text-muted"> · plus optional SFO evidence</span>}
      </td>
      <td className={`${TD} text-right tnum text-muted`}>{models ?? "—"}</td>
      <td className={`${TD} text-muted`}>{settlementLabel(city.settlement_source)}</td>
    </tr>
  );
}

function EmptyNote() {
  return (
    <p className="rounded-2xl bg-surface-secondary px-4 py-8 text-center text-sm text-muted">
      The station matrix appears once the coverage artifact is published.
    </p>
  );
}

/** Reference table of the production post-processing per settlement station:
    City · Station · Method · Members · Settlement report. The blend-capable
    flagship is flagged. Fed by cities_data.json; tolerant of missing fields and of the
    artifact not being published yet. Scrolls horizontally on small screens. */
export function CityMethodTable() {
  const { data, error } = useCitiesData();
  if (error && !data) return <EmptyNote />;
  if (!data) return <Skeleton className="h-72 w-full rounded-2xl" />;
  const cities = data.cities ?? [];
  if (!cities.length) return <EmptyNote />;

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] border-collapse text-sm">
        <caption className="sr-only">
          Production forecasting method, ensemble size, and settlement report per city
        </caption>
        <thead>
          <tr className="border-b border-separator">
            <th scope="col" className={TH}>City</th>
            <th scope="col" className={TH}>Station</th>
            <th scope="col" className={TH}>Method</th>
            <th scope="col" className={`${TH} text-right`}>Members</th>
            <th scope="col" className={TH}>Settlement report</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-separator">
          {cities.map((c) => (
            <CityRow key={c.slug ?? c.series_ticker ?? c.name} city={c} />
          ))}
        </tbody>
      </table>
    </div>
  );
}
