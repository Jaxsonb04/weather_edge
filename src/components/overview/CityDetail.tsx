import { Card } from "@heroui/react/card";
import { Chip } from "@heroui/react/chip";
import { Icon } from "@iconify/react/offline";
import {
  cityFreshness,
  cityNextForecast,
  describeMethod,
  pct,
  shortDateUTC,
  targetLabel,
  tempColor,
  type City,
  type CityBookSide,
  type CityForecast,
  type Target,
} from "../../lib/data";
import { usePublication } from "../../lib/publication";
import { useTemperature } from "../../lib/units";
import { money } from "../../lib/strategy";
import { Finding } from "../ui/Finding";
import { Reveal } from "../ui/Reveal";
import { Stat } from "../ui/Stat";
import { PipelineStepper } from "../pipeline/PipelineStepper";
import { ForecastInputs } from "../market/ForecastInputs";
import { DecisionCard } from "../market/DecisionCard";
import { EdgeChart } from "../market/EdgeChart";
import { MarketBook } from "../market/MarketBook";
import { DetailDisclosure } from "../ui/DetailDisclosure";
import { lockedHigh, type IntradayLock } from "./CityGrid";
import "../../styles/pro-city-detail.css";

const FRESH_TONE: Record<string, { dot: string; text: string }> = {
  success: { dot: "bg-success", text: "text-success" },
  warning: { dot: "bg-warning", text: "text-warning" },
  danger: { dot: "bg-danger", text: "text-danger" },
};

/** The coverage artifact's own method tag in the site's plain phrasing. The tag
    already names the intraday fold-in when the publisher applied one
    ("emos_wmean + intraday high-so-far update"), so no caller may append that a
    second time — and the old fallback printed the raw tag verbatim. */
const methodLabel = (m: string | undefined) => (m ? describeMethod(m) : "—");

/** `overround` is sum(raw ladder prices) − 1, so it measures the book's balance,
    not a cost: every market probability on this page is already divided by that
    raw total. It also goes negative on a thin ladder, which "cost to beat"
    phrasing renders as nonsense. */
function ladderBalance(overround: number | undefined) {
  if (typeof overround !== "number" || Number.isNaN(overround)) return null;
  const thin = overround < 0;
  return {
    parLabel: `${((1 + overround) * 100).toFixed(1)}%`,
    lead: thin ? "sum to only " : "sum to ",
    descriptor: thin ? "a sign of a thin or incomplete book" : "a measure of book balance",
  };
}

/** The lead the header foregrounds + a small table of the 1–3 published dates. */
function ForecastPanel({ city, intradayLock }: { city: City; intradayLock: IntradayLock | null }) {
  const temperature = useTemperature();
  const lead = cityNextForecast(city);
  const leads = [...(city.forecasts ?? [])]
    .filter((f) => typeof f?.predicted_high_f === "number" && !!f?.target_date)
    .sort((a, b) => a.target_date.localeCompare(b.target_date));
  // The flagship signal republishes SFO's settlement-day high after the intraday
  // update, so this panel must show the same number the hero dial does.
  const display = lead ? lockedHigh(city.slug, lead, intradayLock) : null;
  // The flagship number can replace the coverage one, so the label follows whichever is shown.
  const fromMarket = display?.source === "flagship"
    ? intradayLock?.market === true
    : lead?.forecast_source === "market";

  return (
    <Card className="h-full min-w-0 rounded-3xl">
      <Card.Header className="flex flex-row flex-wrap items-start justify-between gap-3">
        <div>
          <Card.Title className="text-base">Forecast</Card.Title>
          <Card.Description className="text-sm text-muted">
            {lead ? `Next settlement · ${shortDateUTC(lead.target_date)}` : "No forecast published"}
          </Card.Description>
        </div>
        <Chip size="sm" variant="soft" className="h-auto min-w-0 max-w-full">
          {/* The artifact's own tag, in plain English. It already carries the
              intraday fold-in, so appending "+ intraday" here printed it twice. */}
          <Chip.Label className="whitespace-normal">{fromMarket ? "Market-implied" : methodLabel(lead?.method)}</Chip.Label>
        </Chip>
      </Card.Header>
      <Card.Content className="flex flex-col gap-4 pt-0">
        {lead && display ? (
          <>
            <div className="flex flex-wrap items-end gap-x-6 gap-y-2">
              <div>
                <p className="text-xs text-muted">{fromMarket ? "Market-implied high" : "Predicted high"}</p>
                <p
                  className="tnum text-5xl font-semibold leading-none tracking-tight"
                  style={{ color: tempColor(display.highF) }}
                >
                  {temperature.temp(display.highF, 0)}
                </p>
                {fromMarket && typeof lead.model_high_f === "number" && (
                  <p className="tnum mt-1 text-xs text-muted">
                    Model {temperature.temp(lead.model_high_f, 1, true)} · {methodLabel(lead.method)}
                  </p>
                )}
              </div>
              <dl className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
                <div>
                  <dt className="text-xs text-muted">± sigma</dt>
                  <dd className="tnum font-medium">{temperature.diff(lead.sigma_f)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">Model spread</dt>
                  <dd className="tnum font-medium">{temperature.diff(lead.model_spread_f)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">Members</dt>
                  <dd className="tnum font-medium">{lead.n_models ?? "—"}</dd>
                </div>
              </dl>
            </div>

            {display.baselineF != null && !fromMarket && (
              <p className="max-w-prose rounded-2xl bg-surface-secondary px-3 py-2.5 text-xs leading-relaxed text-muted">
                {/* Only San Francisco has a flagship market signal. The other
                    nineteen cities get their intraday fold-in from the coverage
                    artifact, so the sentence must name the right publisher. */}
                Intraday-updated:{" "}
                {display.source === "flagship"
                  ? "the flagship market signal republishes this high after folding in the day's observed high so far"
                  : "the coverage forecast folds the day's observed high so far into the published high"}
                , so it reads{" "}
                <span className="tnum font-medium text-foreground">{temperature.temp(display.highF)}</span>{" "}
                rather than the{" "}
                <span className="tnum font-medium text-foreground">{temperature.temp(display.baselineF)}</span>{" "}
                EMOS issue. The sigma, spread and member count above describe that EMOS issue.
              </p>
            )}

            {leads.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-separator text-left text-xs text-muted">
                      <th className="py-2 pr-3 font-medium">Target</th>
                      <th className="py-2 pr-3 text-right font-medium">High</th>
                      <th className="py-2 pr-3 text-right font-medium">±σ</th>
                      <th className="py-2 text-right font-medium">Lead</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-separator">
                    {leads.map((f: CityForecast) => {
                      const active = f.target_date === lead.target_date;
                      const row = lockedHigh(city.slug, f, intradayLock);
                      return (
                        <tr key={`${f.target_date}-${f.lead_days ?? ""}`} className={active ? "font-medium" : ""}>
                          <td className="py-2 pr-3 text-foreground">
                            {shortDateUTC(f.target_date)}
                            {row.baselineF != null && (
                              <span className="ml-1.5 text-xs text-muted">intraday</span>
                            )}
                          </td>
                          <td className="py-2 pr-3 text-right">
                            {/* The thermal ramp is tuned for the headline
                                figures, which are large text and clear the 3:1
                                floor. At 14px/500 this cell is normal text
                                needing 4.5:1, so it reads in foreground ink —
                                the column of numbers already carries the
                                comparison the colour would have added. */}
                            <span className="tnum font-medium text-foreground">
                              {temperature.temp(row.highF)}
                            </span>
                          </td>
                          <td className="tnum py-2 pr-3 text-right text-muted">{temperature.diff(f.sigma_f)}</td>
                          <td className="tnum py-2 text-right text-muted">
                            {f.lead_days == null ? "—" : `${f.lead_days}d`}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </>
        ) : (
          <p className="text-sm text-muted">No forecast has been published for this city yet.</p>
        )}
      </Card.Content>
    </Card>
  );
}

/** Settlement + provenance + paper-book activity for one city. */
function BookPanel({ city, currentStateAvailable }: { city: City; currentStateAvailable: boolean }) {
  const temperature = useTemperature();
  const fresh = cityFreshness(city.forecasts);
  const tone = FRESH_TONE[fresh.tone] ?? FRESH_TONE.danger;
  const settled = city.latest_settlement;
  const books = city.books ?? {};
  const live: CityBookSide = books.live ?? {};
  const research: CityBookSide = books.research ?? {};

  const rows: { k: string; live: string; research: string }[] = [
    {
      k: "Open positions",
      live: currentStateAvailable ? String(live.open_positions ?? 0) : "Unavailable",
      research: currentStateAvailable ? String(research.open_positions ?? 0) : "Unavailable",
    },
    {
      k: "Resting orders",
      live: currentStateAvailable ? String(live.resting_orders ?? 0) : "Unavailable",
      research: currentStateAvailable ? String(research.resting_orders ?? 0) : "Unavailable",
    },
    {
      // Exposure deliberately spans filled cost plus the capital a resting order
      // has reserved (cities_report.py), so a book can show 0 open positions
      // beside a non-zero figure. Name both in the label rather than let the row
      // read as a contradiction.
      k: "Open + resting exposure",
      live: currentStateAvailable ? money(live.open_exposure) : "Unavailable",
      research: currentStateAvailable ? money(research.open_exposure) : "Unavailable",
    },
    {
      k: "Settled orders",
      live: String(live.settled_orders ?? 0),
      research: String(research.settled_orders ?? 0),
    },
    {
      k: "Settled P&L",
      live: money(live.settled_pnl),
      research: money(research.settled_pnl),
    },
  ];

  return (
    <Card className="h-full min-w-0 rounded-3xl">
      <Card.Header className="flex flex-row items-start justify-between gap-3">
        <div>
          <Card.Title className="text-base">Settlement and paper books</Card.Title>
          <Card.Description className="text-sm text-muted">
            {city.settlement_source ?? "Official NWS climate report"}
          </Card.Description>
        </div>
        <span className={`flex items-center gap-1.5 text-xs ${tone.text}`}>
          <span className={`size-1.5 rounded-full ${tone.dot}`} aria-hidden="true" />
          <span className="text-muted">{fresh.label}</span>
        </span>
      </Card.Header>
      <Card.Content className="flex flex-col gap-4 pt-0">
        <div className="grid grid-cols-2 gap-x-6 gap-y-4">
          <Stat
            label={`Last settlement · ${shortDateUTC(settled?.local_date)}`}
            value={temperature.temp(settled?.high_f)}
          />
          <Stat label="Eligible decisions · 24h" value={currentStateAvailable ? String(books.approved_24h ?? 0) : "Unavailable"} />
        </div>

        <div className="overflow-x-auto">
          <table className="w-full min-w-[20rem] text-sm">
            <thead>
              <tr className="border-b border-separator text-left text-xs text-muted">
                <th className="py-2 pr-3 font-medium">Book</th>
                <th className="py-2 pr-3 text-right font-medium">Live</th>
                <th className="py-2 text-right font-medium">Research</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-separator">
              {rows.map((r) => (
                <tr key={r.k}>
                  <td className="py-2 pr-3 text-muted">{r.k}</td>
                  <td className="tnum py-2 pr-3 text-right font-medium text-foreground">{r.live}</td>
                  <td className="tnum py-2 text-right text-muted">{r.research}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <p className="text-xs text-muted">
          {currentStateAvailable ? (
            <>
              <span className="tnum font-medium text-foreground">{(books.decisions_24h ?? 0).toLocaleString()}</span>{" "}
              evaluations in the last 24h
            </>
          ) : (
            "Current activity unavailable until publication recovers"
          )}{" "}
          · station <span className="text-foreground">{city.station_id ?? "—"}</span>
        </p>
      </Card.Content>
    </Card>
  );
}

interface CityDetailProps {
  city: City;
  /** Status-selected SF flagship market target — only meaningful for SFO. */
  flagshipTarget?: Target;
  /** Flagship reconciliation, so this panel never contradicts the hero dial. */
  intradayLock?: IntradayLock | null;
}

/** The selected-city drill-down. Every city publishes its calibrated forecast,
    settlement and paper-book activity; the San Francisco flagship additionally
    publishes the full bracket-level market microstructure. */
export function CityDetail({ city, flagshipTarget, intradayLock = null }: CityDetailProps) {
  const { operational } = usePublication();
  const temperature = useTemperature();
  const currentStateAvailable = operational.state === "fresh";
  // The bracket-level signal artifact is San-Francisco-only, so gate on the
  // flagship blend AND the SFO slug — never attach SF's book to another city.
  const isFlagship = !!city.has_full_blend && city.slug === "sfo" && !!flagshipTarget;
  const showBrackets = isFlagship && currentStateAvailable;
  const mc = flagshipTarget?.market_consensus;
  const ladder = ladderBalance(mc?.overround);
  const approvedCount = flagshipTarget?.decisions?.filter((decision) => decision.approved).length ?? 0;

  return (
    <div className="flex flex-col gap-4">
      <Reveal>
        <div className="grid gap-4 lg:grid-cols-[1.02fr_0.98fr]">
          <ForecastPanel city={city} intradayLock={intradayLock} />
          <BookPanel city={city} currentStateAvailable={currentStateAvailable} />
        </div>
      </Reveal>

      {isFlagship && !currentStateAvailable ? (
        <Reveal>
          <div role="status" className="flex items-start gap-3 rounded-2xl bg-surface-secondary px-4 py-3">
            <Icon icon="solar:clock-circle-linear" className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
            <p className="text-sm leading-relaxed text-muted">
              Live bracket prices and gate decisions return with the next current publication. The forecast and settled
              history above stay visible.
            </p>
          </div>
        </Reveal>
      ) : showBrackets && flagshipTarget ? (
        <Reveal>
          <DetailDisclosure
            id="flagship-market-detail"
            icon="solar:chart-square-linear"
            title="Market detail"
            note="Pricing steps, bracket edges and gate traces for the flagship market"
          >
            <PipelineStepper />

            <div className="grid gap-4 lg:grid-cols-[1.02fr_0.98fr]">
              <ForecastInputs target={flagshipTarget} />
              <DecisionCard target={flagshipTarget} approvedCount={approvedCount} />
            </div>

            <EdgeChart target={flagshipTarget} />
            {mc?.available && (
              <Finding label="Model versus market">
                Model <strong>{temperature.temp(mc.model_high_f)}</strong>, market <strong>{temperature.temp(mc.implied_high_f)}</strong>: a{" "}
                <strong>
                  {mc.model_minus_market_f > 0 ? "+" : ""}
                  {temperature.diff(mc.model_minus_market_f, 1, true)}
                </strong>{" "}
                gap for the {targetLabel(flagshipTarget.target_date).toLowerCase()} high. The market's likeliest bracket is{" "}
                <strong>{temperature.bracket(mc.modal_bin_label)}</strong> at {pct(mc.modal_probability, 0)};{" "}
                <strong>{approvedCount}</strong> candidate{approvedCount === 1 ? " is" : "s are"} eligible.
                {ladder && (
                  <>
                    {" "}Raw ladder prices {ladder.lead}<strong>{ladder.parLabel}</strong> of par, {ladder.descriptor}; probabilities shown are normalized.
                  </>
                )}
              </Finding>
            )}

            <MarketBook target={flagshipTarget} />
          </DetailDisclosure>
        </Reveal>
      ) : (
        <Reveal>
          <p className="rounded-2xl bg-surface-secondary px-4 py-3 text-sm text-muted">
            Bracket-level market detail is published for San Francisco, the flagship market.
          </p>
        </Reveal>
      )}
    </div>
  );
}
