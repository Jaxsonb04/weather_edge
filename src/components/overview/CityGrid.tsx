import { Card, Skeleton } from "@heroui/react";
import { round1, tempColor, useCitiesData, type City, type CityForecast } from "../../lib/data";

const HOUR_MS = 3_600_000;
const FRESH_GREEN_HOURS = 2;
const FRESH_AMBER_HOURS = 12;
const GRID = "grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5";

/** "2026-07-06" → "Jul 6" (date-only, timezone-safe via UTC). */
function shortDate(iso: string): string {
  const t = Date.parse(`${iso}T00:00:00Z`);
  if (Number.isNaN(t)) return iso;
  return new Date(t).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

/** The forecast the card leads with: the earliest target strictly after the
    newest settlement, else the earliest still-open date, else the last one. */
function nextForecast(city: City): CityForecast | null {
  const sorted = [...(city.forecasts ?? [])]
    .filter((f) => typeof f?.predicted_high_f === "number" && !!f?.target_date)
    .sort((a, b) => a.target_date.localeCompare(b.target_date));
  if (!sorted.length) return null;
  const settledDate = city.latest_settlement?.local_date;
  if (settledDate) {
    const next = sorted.find((f) => f.target_date > settledDate);
    if (next) return next;
  }
  const todayIso = new Date().toISOString().slice(0, 10);
  return sorted.find((f) => f.target_date >= todayIso) ?? sorted[sorted.length - 1];
}

function freshness(forecasts: CityForecast[]): { className: string; label: string } {
  let newest: number | null = null;
  for (const f of forecasts) {
    const t = Date.parse(f?.fetched_at ?? "");
    if (!Number.isNaN(t) && (newest == null || t > newest)) newest = t;
  }
  if (newest == null) return { className: "bg-danger", label: "No forecast fetch recorded" };
  const hrs = Math.max(0, (Date.now() - newest) / HOUR_MS);
  if (hrs < FRESH_GREEN_HOURS)
    return { className: "bg-success", label: `Forecasts refreshed ${Math.max(1, Math.round(hrs * 60))}m ago` };
  if (hrs < FRESH_AMBER_HOURS)
    return { className: "bg-warning", label: `Forecasts refreshed ${Math.round(hrs)}h ago` };
  return { className: "bg-danger", label: `Forecasts stale — last refreshed ${Math.round(hrs)}h ago` };
}

function CityCard({ city }: { city: City }) {
  const fc = nextForecast(city);
  const dot = freshness(city.forecasts ?? []);
  const settled = city.latest_settlement;
  const openPositions =
    (city.books?.live?.open_positions ?? 0) + (city.books?.research?.open_positions ?? 0);
  const scans = city.books?.decisions_24h ?? 0;

  return (
    <Card className="h-full rounded-2xl ring-1 ring-border/70">
      <Card.Content className="flex h-full flex-col gap-2.5 p-3.5">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="truncate font-display text-sm font-semibold text-foreground">{city.name}</p>
            <p className="font-mono text-[10px] uppercase tracking-wider text-muted">
              {city.station_id ?? "—"}
            </p>
          </div>
          <span
            role="img"
            aria-label={dot.label}
            title={dot.label}
            className={`mt-1 size-2 shrink-0 rounded-full ${dot.className}`}
          />
        </div>

        <div>
          <div className="flex items-baseline gap-1.5">
            {fc ? (
              <>
                <span
                  className="tnum font-display text-3xl font-bold leading-none"
                  style={{ color: tempColor(fc.predicted_high_f) }}
                >
                  {Math.round(fc.predicted_high_f)}°
                </span>
                {typeof fc.sigma_f === "number" && (
                  <span className="font-mono text-[11px] text-muted">±{round1(fc.sigma_f)}°</span>
                )}
              </>
            ) : (
              <span className="font-display text-3xl font-bold leading-none text-muted">—</span>
            )}
          </div>
          <p className="mt-1 font-mono text-[10px] uppercase tracking-wider text-muted">
            {fc ? `${shortDate(fc.target_date)} · ${fc.n_models ?? "—"} models` : "no live forecast"}
          </p>
        </div>

        <div className="mt-auto space-y-1 border-t border-border/50 pt-2 text-[11px] leading-snug text-muted">
          <p>
            {settled ? (
              <>
                Settled {shortDate(settled.local_date)}:{" "}
                <span className="tnum font-medium text-foreground">{settled.high_f}°</span>
              </>
            ) : (
              "No settlement yet"
            )}
          </p>
          <p>
            {openPositions > 0
              ? `${openPositions} open position${openPositions === 1 ? "" : "s"}`
              : `${scans.toLocaleString()} scans/24h`}
          </p>
        </div>

        {city.has_full_blend && (
          <span className="self-start rounded bg-accent-soft px-1.5 py-0.5 font-mono text-[10px] font-medium text-[color:var(--accent-text)]">
            flagship · full blend
          </span>
        )}
      </Card.Content>
    </Card>
  );
}

function EmptyNote() {
  return (
    <p className="rounded-2xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted">
      Multi-city data not yet published — the fifteen-city coverage artifact will appear here on the
      next pipeline run.
    </p>
  );
}

/** Compact per-city instrument cards fed by cities_data.json: next forecast as
    the hero number, settlement + book activity kept quiet underneath. */
export function CityGrid() {
  const { data, error } = useCitiesData();
  if (error) return <EmptyNote />;
  if (!data) {
    return (
      <div className={GRID} aria-hidden="true">
        {Array.from({ length: 15 }).map((_, i) => (
          <Skeleton key={i} className="h-44 rounded-2xl" />
        ))}
      </div>
    );
  }
  const cities = data.cities ?? [];
  if (!cities.length) return <EmptyNote />;
  return (
    <div className={GRID}>
      {cities.map((c) => (
        <CityCard key={c.slug ?? c.series_ticker} city={c} />
      ))}
    </div>
  );
}
