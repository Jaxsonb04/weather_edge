import { useMemo, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { Card } from "@heroui/react/card";
import { Chip } from "@heroui/react/chip";
import { Separator } from "@heroui/react/separator";
import { Segment } from "@heroui-pro/react/segment";
import { TrendChip } from "@heroui-pro/react/trend-chip";
import { Icon } from "@iconify/react/offline";
import {
  cityFreshness,
  cityNextForecast,
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
import { SourceBlend } from "./SourceBlend";

const methodLabel = (method: string | undefined) =>
  method === "emos_wmean" ? "EMOS weighted mean" : method?.replaceAll("_", " ") ?? "Calibrated ensemble";

function CityForecastDial({ city }: { city: City }) {
  const reduce = useReducedMotion();
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
  const forecast = forecasts[idx] ?? lead ?? forecasts[0];
  const freshness = cityFreshness(city.forecasts);
  const freshnessColor = freshness.tone === "danger" ? "danger" : freshness.tone === "warning" ? "warning" : "success";

  return (
    <Card className="overflow-hidden rounded-3xl ring-1 ring-border/70">
      <Card.Content className="p-6">
        <div className="mb-4 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <span className="font-mono text-[11px] font-medium uppercase tracking-[0.16em] text-muted">
              {city.name} daily high · forecast
            </span>
            <p className="mt-1 text-xs text-muted">
              Station <span className="font-mono text-foreground">{city.station_id ?? "—"}</span>
            </p>
          </div>
          <Chip size="sm" variant="soft" color={freshnessColor}>
            <Chip.Label>{freshness.label}</Chip.Label>
          </Chip>
        </div>

        {forecasts.length > 1 && (
          <Segment
            aria-label={`${city.name} forecast day`}
            size="sm"
            selectedKey={String(Math.min(idx, forecasts.length - 1))}
            onSelectionChange={(key) => setIdx(Number(key))}
            className="mb-5"
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
            <AnimatePresence mode="popLayout" initial={false}>
              <motion.div
                key={`${city.slug}-${forecast.target_date}`}
                initial={reduce ? false : { opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={reduce ? undefined : { opacity: 0, y: -8 }}
                transition={{ duration: 0.24, ease: [0.16, 1, 0.3, 1] }}
                className="flex flex-wrap items-end justify-between gap-5"
              >
                <div>
                  <p className="text-xs text-muted">Predicted high · {shortDateUTC(forecast.target_date)}</p>
                  <p
                    className="tnum font-display text-[5.5rem] font-bold leading-[0.88]"
                    style={{ color: tempColor(forecast.predicted_high_f) }}
                  >
                    {Math.round(forecast.predicted_high_f)}
                    <span className="align-top font-sans text-2xl font-semibold text-muted">°F</span>
                  </p>
                </div>
                <dl className="grid grid-cols-2 gap-x-6 gap-y-3 pb-1 text-right">
                  <div>
                    <dt className="text-[10px] uppercase tracking-wide text-muted">Uncertainty</dt>
                    <dd className="tnum font-display text-lg font-semibold">
                      {forecast.sigma_f == null ? "—" : `±${round1(forecast.sigma_f)}°`}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-[10px] uppercase tracking-wide text-muted">Models</dt>
                    <dd className="tnum font-display text-lg font-semibold">{forecast.n_models ?? "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-[10px] uppercase tracking-wide text-muted">Spread</dt>
                    <dd className="tnum font-display text-lg font-semibold">
                      {forecast.model_spread_f == null ? "—" : `${round1(forecast.model_spread_f)}°`}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-[10px] uppercase tracking-wide text-muted">Lead</dt>
                    <dd className="tnum font-display text-lg font-semibold">
                      {forecast.lead_days == null ? "—" : `${forecast.lead_days}d`}
                    </dd>
                  </div>
                </dl>
              </motion.div>
            </AnimatePresence>

            <Separator className="my-5" />
            <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-muted">
              <span>{methodLabel(forecast.method)} · station-aligned settlement</span>
              <span>
                Last observed high {city.latest_settlement ? `${round1(city.latest_settlement.high_f)}°F` : "—"}
              </span>
            </div>
          </>
        ) : (
          <div role="status" className="grid min-h-56 place-items-center text-center text-sm text-muted">
            No current forecast is published for {city.name}.
          </div>
        )}
      </Card.Content>
    </Card>
  );
}

export function ForecastDial({ targets, city }: { targets: Target[]; city?: City | null }) {
  const reduce = useReducedMotion();
  const { signal } = usePublication();
  const currentStateAvailable = signal.state === "fresh";
  const [idx, setIdx] = useState(0);
  const displayTargets = useMemo(() => selectCurrentTargets(targets), [targets]);
  const target = displayTargets[idx] ?? displayTargets.at(0);

  if (city && city.slug !== "sfo") return <CityForecastDial city={city} />;

  if (!target) {
    return (
      <Card className="rounded-3xl ring-1 ring-danger/30">
        <Card.Content className="flex items-start gap-3 p-6" role="alert">
          <Icon icon="solar:danger-triangle-bold" className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden="true" />
          <p className="text-sm text-muted">No settlement-day or upcoming prediction-market target is published.</p>
        </Card.Content>
      </Card>
    );
  }
  const high = predictedHigh(target);
  const mc = target.market_consensus;
  const intraday = target.intraday;
  const delta = mc?.model_minus_market_f ?? null;

  return (
    <Card className="overflow-hidden rounded-3xl ring-1 ring-border/70">
      <Card.Content className="p-6">
        <div className="mb-4 flex items-center justify-between gap-3">
          <span className="font-mono text-[11px] font-medium uppercase tracking-[0.16em] text-muted">
            {city?.name ?? "San Francisco"} daily high · forecast
          </span>
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
            selectedKey={String(idx)}
            onSelectionChange={(k) => setIdx(Number(k))}
            className="mb-5"
          >
            {displayTargets.map((t, i) => (
              <Segment.Item key={i} id={String(i)}>
                {targetLabel(t.target_date)}
              </Segment.Item>
            ))}
          </Segment>
        )}

        <div className="flex items-end justify-between gap-4">
          <AnimatePresence mode="popLayout" initial={false}>
            <motion.div
              key={idx}
              initial={reduce ? false : { opacity: 0, y: 12, filter: "blur(4px)" }}
              animate={{ opacity: 1, y: 0, filter: "blur(0px)" }}
              exit={reduce ? undefined : { opacity: 0, y: -12, filter: "blur(4px)" }}
              transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
              className="leading-none"
            >
              <p className="temp-text font-display text-[5.5rem] font-bold leading-[0.85] tnum">
                {high == null ? "—" : Math.round(high)}
                <span className="align-top font-sans text-2xl font-semibold text-muted">°F</span>
              </p>
            </motion.div>
          </AnimatePresence>

          {currentStateAvailable && delta != null && mc && (
            <div className="mb-1 text-right">
              <TrendChip trend={delta > 0.2 ? "up" : delta < -0.2 ? "down" : "neutral"} size="sm">
                {delta > 0 ? "+" : ""}{f1(delta)}
                <TrendChip.Suffix>vs market</TrendChip.Suffix>
              </TrendChip>
              <p className="mt-1.5 text-xs text-muted">
                market implies <span className="tnum font-medium text-foreground">{f1(mc.implied_high_f)}</span>
              </p>
            </div>
          )}
        </div>

        {currentStateAvailable && intraday && !intraday.is_complete && (
          <div className="mt-5 rounded-2xl bg-surface-secondary px-4 py-3 ring-1 ring-border/50">
            <div className="flex items-center justify-between text-xs">
              <span className="flex items-center gap-2 text-muted">
                <span className="relative inline-flex size-2 text-success">
                  <span className="pulse-dot absolute inset-0 rounded-full" />
                  <span className="relative size-2 rounded-full bg-success" />
                </span>
                Live · {intraday.observation_count} obs
              </span>
              <span className="tnum text-muted">now {f1(intraday.latest_temp_f)}</span>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="text-xs text-muted">Observed high so far</span>
              <span className="tnum font-display text-xl font-semibold">{f1(intraday.observed_high_f)}</span>
              <Icon icon="solar:arrow-right-up-bold" className="size-3.5 text-success" />
            </div>
          </div>
        )}

        <Separator className="my-5" />
        <p className="mb-3 font-mono text-[11px] font-medium uppercase tracking-[0.16em] text-muted">
          Source blend · {targetLabel(target.target_date).toLowerCase()}
        </p>
        <SourceBlend target={target} />
      </Card.Content>
    </Card>
  );
}
