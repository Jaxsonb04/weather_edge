import { useMemo, useState, type ReactNode } from "react";
import { Card } from "@heroui/react/card";
import { Chip } from "@heroui/react/chip";
import { Segment } from "@heroui-pro/react/segment";
import { TrendChip } from "@heroui-pro/react/trend-chip";
import { Icon } from "@iconify/react/offline";
import {
  cityFreshness,
  cityNextForecast,
  describeMethod,
  f1,
  predictedHigh,
  round1,
  selectCurrentTargets,
  shortDateUTC,
  targetLabel,
  tempColor,
  type City,
  type CityForecast,
  type Target,
} from "../../lib/data";
import { usePublication } from "../../lib/publication";
import { AnimatedNumber } from "../ui/AnimatedNumber";
import { DialScale } from "./DialScale";
import { SourceBlend } from "./SourceBlend";

/** The big temperature. Its digits roll to the new value when the day or city
    changes; screen readers get the settled figure once. */
function Reading({ value, color }: { value: number | null; color?: string }) {
  return (
    <p
      className="tnum flex items-start text-[5rem] font-semibold leading-none tracking-tighter sm:text-[5.5rem]"
      style={color ? { color } : undefined}
    >
      {value == null ? "—" : (
        <>
          <span className="sr-only">{Math.round(value)}</span>
          <span aria-hidden="true"><AnimatedNumber value={Math.round(value)} /></span>
        </>
      )}
      <span className="mt-1.5 text-2xl font-medium tracking-normal text-muted">°F</span>
    </p>
  );
}

function Figure({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="tnum text-base font-semibold text-foreground">{children}</dd>
    </div>
  );
}

function CityForecastDial({ city }: { city: City }) {
  const forecasts = useMemo(
    () =>
      [...(city.forecasts ?? [])]
        .filter((forecast): forecast is CityForecast =>
          typeof forecast?.predicted_high_f === "number" && Boolean(forecast?.target_date),
        )
        .sort((a, b) => a.target_date.localeCompare(b.target_date)),
    [city.forecasts],
  );
  const lead = cityNextForecast(city);
  const initialIndex = Math.max(0, forecasts.findIndex((forecast) => forecast.target_date === lead?.target_date));
  const [idx, setIdx] = useState(initialIndex);
  // The published list shrinks at local-day rollover and this card is keyed by
  // slug, so it is never remounted. A stale selection falls back to the lead in
  // the readout *and* in the control.
  const activeIdx = idx < forecasts.length ? idx : initialIndex;
  const forecast = forecasts[activeIdx] ?? lead ?? forecasts[0];
  const freshness = cityFreshness(city.forecasts);
  const freshnessColor = freshness.tone === "danger" ? "danger" : freshness.tone === "warning" ? "warning" : "success";

  return (
    <Card className="rounded-3xl">
      <Card.Content className="flex flex-col gap-5 p-5 sm:p-6">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-sm font-semibold text-foreground">{city.name} daily high</p>
            <p className="text-xs text-muted">Station {city.station_id ?? "—"}</p>
          </div>
          <Chip size="sm" variant="soft" color={freshnessColor}>
            <Chip.Label>{freshness.label}</Chip.Label>
          </Chip>
        </div>

        {forecasts.length > 1 && (
          <Segment
            aria-label={`${city.name} forecast day`}
            size="sm"
            selectedKey={String(activeIdx)}
            onSelectionChange={(key) => setIdx(Number(key))}
            className="self-start"
          >
            {forecasts.map((entry, index) => (
              <Segment.Item key={entry.target_date} id={String(index)}>
                {shortDateUTC(entry.target_date)}
              </Segment.Item>
            ))}
          </Segment>
        )}

        {forecast ? (
          <>
            <div>
              <p className="text-xs text-muted">Predicted high · {shortDateUTC(forecast.target_date)}</p>
              <Reading value={forecast.predicted_high_f} color={tempColor(forecast.predicted_high_f)} />
            </div>
            <DialScale value={forecast.predicted_high_f} />
            <dl className="grid grid-cols-4 gap-3 border-t border-separator pt-4">
              <Figure label="Uncertainty">{forecast.sigma_f == null ? "—" : `±${round1(forecast.sigma_f)}°`}</Figure>
              <Figure label="Models">{forecast.n_models ?? "—"}</Figure>
              <Figure label="Spread">{forecast.model_spread_f == null ? "—" : `${round1(forecast.model_spread_f)}°`}</Figure>
              <Figure label="Lead">{forecast.lead_days == null ? "—" : `${forecast.lead_days}d`}</Figure>
            </dl>
            <p className="text-xs text-muted">
              {describeMethod(forecast.method)} · last observed high{" "}
              <span className="tnum text-foreground">{city.latest_settlement ? `${round1(city.latest_settlement.high_f)}°F` : "—"}</span>
            </p>
          </>
        ) : (
          <div role="status" className="grid min-h-48 place-items-center text-center text-sm text-muted">
            No current forecast is published for {city.name}.
          </div>
        )}
      </Card.Content>
    </Card>
  );
}

export function ForecastDial({ targets, city }: { targets: Target[]; city?: City | null }) {
  const { signal } = usePublication();
  const currentStateAvailable = signal.state === "fresh";
  const [idx, setIdx] = useState(0);
  const displayTargets = useMemo(() => selectCurrentTargets(targets), [targets]);
  const activeIdx = idx < displayTargets.length ? idx : 0;
  const target = displayTargets[activeIdx];

  if (city && city.slug !== "sfo") return <CityForecastDial city={city} />;

  if (!target) {
    return (
      <Card className="rounded-3xl">
        <Card.Content className="flex items-start gap-3 p-6" role="alert">
          <Icon icon="solar:danger-triangle-linear" className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden="true" />
          <p className="text-sm text-muted">No settlement-day or upcoming prediction-market target is published.</p>
        </Card.Content>
      </Card>
    );
  }
  const high = predictedHigh(target);
  const mc = target.market_consensus;
  const intraday = target.intraday;
  const delta = mc?.model_minus_market_f ?? null;
  const forecastFields = (target.forecast ?? {}) as Record<string, unknown>;
  const sourceFields = forecastFields.sources && typeof forecastFields.sources === "object"
    ? forecastFields.sources as Record<string, unknown>
    : {};
  const hasPublishedSourceBlend = Object.values(sourceFields).some((value) => typeof value === "number");
  const currentMethod = describeMethod(forecastFields.method);
  const modelCount = typeof forecastFields.source_count === "number"
    ? forecastFields.source_count
    : typeof target.ensemble?.member_count === "number"
      ? target.ensemble.member_count
      : null;

  return (
    <Card className="rounded-3xl">
      <Card.Content className="flex flex-col gap-5 p-5 sm:p-6">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-sm font-semibold text-foreground">{city?.name ?? "San Francisco"} daily high</p>
            <p className="text-xs text-muted">Station {city?.station_id ?? "KSFO"} · flagship market</p>
          </div>
          <Chip size="sm" variant="soft" color={currentStateAvailable && target.market_available ? "success" : "default"}>
            <Chip.Label>
              {currentStateAvailable
                ? target.market_available
                  ? "Market live"
                  : "No market"
                : "Current status unavailable"}
            </Chip.Label>
          </Chip>
        </div>

        {displayTargets.length > 1 && (
          <Segment
            aria-label="Forecast day"
            size="sm"
            selectedKey={String(activeIdx)}
            onSelectionChange={(k) => setIdx(Number(k))}
            className="self-start"
          >
            {displayTargets.map((t, i) => (
              <Segment.Item key={i} id={String(i)}>
                {targetLabel(t.target_date)}
              </Segment.Item>
            ))}
          </Segment>
        )}

        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <p className="text-xs text-muted">Predicted high · {targetLabel(target.target_date)}</p>
            <Reading value={high} color={high == null ? undefined : tempColor(high)} />
          </div>
          {currentStateAvailable && delta != null && mc && (
            <div className="mb-1 text-right">
              <TrendChip trend={delta > 0.2 ? "up" : delta < -0.2 ? "down" : "neutral"} size="sm">
                {delta > 0 ? "+" : ""}{f1(delta)}
                <TrendChip.Suffix>vs market</TrendChip.Suffix>
              </TrendChip>
              <p className="mt-1.5 flex items-center justify-end gap-1.5 text-xs text-muted">
                <span className="h-3 w-0.5 rounded-full bg-[color:var(--series-market)]" aria-hidden="true" />
                <span>market implies <span className="tnum font-medium text-foreground">{f1(mc.implied_high_f)}</span></span>
              </p>
            </div>
          )}
        </div>

        {high != null && <DialScale value={high} market={currentStateAvailable ? mc?.implied_high_f : null} />}

        {currentStateAvailable && intraday && !intraday.is_complete && (
          <div className="rounded-2xl bg-surface-secondary px-4 py-3">
            <p className="flex items-center gap-2 text-xs text-muted">
              <span className="relative inline-flex size-2 text-success">
                <span key={intraday.observation_count} className="live-dot absolute inset-0 rounded-full" />
                <span className="relative size-2 rounded-full bg-success" />
              </span>
              Live · {intraday.observation_count} obs
            </p>
            <dl className="mt-2 grid grid-cols-2 gap-3">
              <div>
                <dt className="text-xs text-muted">Now</dt>
                <dd className="tnum text-sm font-semibold text-foreground">{f1(intraday.latest_temp_f)}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted">High so far</dt>
                <dd className="tnum text-sm font-semibold text-foreground">{f1(intraday.observed_high_f)}</dd>
              </div>
            </dl>
          </div>
        )}

        <div className="border-t border-separator pt-4">
          <p className="mb-3 text-xs text-muted">
            {hasPublishedSourceBlend ? "Source blend" : "Method"} · {targetLabel(target.target_date).toLowerCase()}
          </p>
          {hasPublishedSourceBlend ? (
            <SourceBlend target={target} />
          ) : (
            <p className="text-xs text-muted">
              <span className="font-medium text-foreground">{currentMethod}</span>
              {modelCount != null && <> · <span className="tnum">{modelCount}</span> NWP members</>}
            </p>
          )}
        </div>
      </Card.Content>
    </Card>
  );
}
