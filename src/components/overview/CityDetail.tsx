import { Card } from "@heroui/react/card";
import { Chip } from "@heroui/react/chip";
import { Icon } from "@iconify/react/offline";
import {
  cityFreshness,
  cityNextForecast,
  f1,
  pct,
  round1,
  shortDateUTC,
  targetLabel,
  tempColor,
  type City,
  type CityBookSide,
  type CityForecast,
  type Target,
} from "../../lib/data";
import { usePublication } from "../../lib/publication";
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

const methodLabel = (m: string | undefined) =>
  m === "emos_wmean" ? "EMOS weighted mean" : m ? m.replace(/_/g, " ") : "—";

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
  const lead = cityNextForecast(city);
  const leads = [...(city.forecasts ?? [])]
    .filter((f) => typeof f?.predicted_high_f === "number" && !!f?.target_date)
    .sort((a, b) => a.target_date.localeCompare(b.target_date));
  // The flagship signal republishes SFO's settlement-day high after the intraday
  // update, so this panel must show the same number the hero dial does.
  const display = lead ? lockedHigh(city.slug, lead, intradayLock) : null;

  return (
    <Card className="h-full min-w-0 rounded-2xl">
      <Card.Header className="flex flex-row items-start justify-between gap-3">
        <div>
          <Card.Title className="text-base">Calibrated forecast</Card.Title>
          <Card.Description className="text-sm text-muted">
            {lead ? `Next settlement · ${shortDateUTC(lead.target_date)}` : "No forecast published"}
          </Card.Description>
        </div>
        <Chip size="sm" variant="soft">
          {/* The method the coverage artifact names is the EMOS issue; say so
              plainly when the displayed high is the intraday-updated one. */}
          <Chip.Label>
            {display?.baselineF == null ? methodLabel(lead?.method) : `${methodLabel(lead?.method)} + intraday`}
          </Chip.Label>
        </Chip>
      </Card.Header>
      <Card.Content className="space-y-4 pt-0">
        {lead && display ? (
          <>
            <div className="flex flex-wrap items-end gap-x-6 gap-y-2">
              <div>
                <p className="text-[11px] uppercase tracking-wide text-muted">Predicted high</p>
                <p
                  className="tnum font-display text-5xl font-bold leading-none"
                  style={{ color: tempColor(display.highF) }}
                >
                  {Math.round(display.highF)}°
                </p>
              </div>
              <dl className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
                <div>
                  <dt className="text-[11px] uppercase tracking-wide text-muted">± sigma</dt>
                  <dd className="tnum font-medium">{lead.sigma_f == null ? "—" : `${round1(lead.sigma_f)}°`}</dd>
                </div>
                <div>
                  <dt className="text-[11px] uppercase tracking-wide text-muted">Model spread</dt>
                  <dd className="tnum font-medium">{lead.model_spread_f == null ? "—" : `${round1(lead.model_spread_f)}°`}</dd>
                </div>
                <div>
                  <dt className="text-[11px] uppercase tracking-wide text-muted">Members</dt>
                  <dd className="tnum font-medium">{lead.n_models ?? "—"}</dd>
                </div>
              </dl>
            </div>

            {display.baselineF != null && (
              <p className="max-w-prose text-xs leading-relaxed text-muted">
                Intraday-updated: the flagship market signal republishes this high after folding in
                the day's observed high so far, so it reads{" "}
                <span className="tnum font-medium text-foreground">{round1(display.highF)}°</span>{" "}
                rather than the{" "}
                <span className="tnum font-medium text-foreground">{round1(display.baselineF)}°</span>{" "}
                EMOS issue. The sigma, spread and member count above describe that EMOS issue.
              </p>
            )}

            {leads.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[22rem] text-sm">
                  <thead>
                    <tr className="border-b border-border/60 text-left font-mono text-[10px] uppercase tracking-wider text-muted">
                      <th className="py-2 pr-3 font-medium">Target</th>
                      <th className="py-2 pr-3 text-right font-medium">High</th>
                      <th className="py-2 pr-3 text-right font-medium">±σ</th>
                      <th className="py-2 text-right font-medium">Lead</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border/40">
                    {leads.map((f: CityForecast) => {
                      const active = f.target_date === lead.target_date;
                      const row = lockedHigh(city.slug, f, intradayLock);
                      return (
                        <tr key={`${f.target_date}-${f.lead_days ?? ""}`} className={active ? "bg-accent-soft/50" : ""}>
                          <td className="py-2 pr-3 text-foreground">
                            {shortDateUTC(f.target_date)}
                            {row.baselineF != null && (
                              <span className="ml-1.5 font-mono text-[9px] uppercase tracking-wide text-muted">
                                intraday
                              </span>
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
                              {round1(row.highF)}°
                            </span>
                          </td>
                          <td className="tnum py-2 pr-3 text-right text-muted">{f.sigma_f == null ? "—" : `${round1(f.sigma_f)}°`}</td>
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
      k: "Open exposure",
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
    <Card className="h-full min-w-0 rounded-2xl">
      <Card.Header className="flex flex-row items-start justify-between gap-3">
        <div>
          <Card.Title className="text-base">Settlement & paper book</Card.Title>
          <Card.Description className="text-sm text-muted">
            {city.settlement_source ?? "Official NWS climate report"}
          </Card.Description>
        </div>
        <span className={`flex items-center gap-1.5 text-xs ${tone.text}`}>
          <span className={`size-1.5 rounded-full ${tone.dot}`} aria-hidden="true" />
          <span className="text-muted">{fresh.label}</span>
        </span>
      </Card.Header>
      <Card.Content className="space-y-4 pt-0">
        <div className="grid grid-cols-2 gap-x-6 gap-y-4">
          <Stat
            label={`Last settlement · ${shortDateUTC(settled?.local_date)}`}
            value={settled ? `${round1(settled.high_f)}°` : "—"}
          />
          <Stat label="Eligible decisions · 24h" value={currentStateAvailable ? String(books.approved_24h ?? 0) : "Unavailable"} />
        </div>

        <div className="overflow-x-auto">
          <table className="w-full min-w-[20rem] text-sm">
            <thead>
              <tr className="border-b border-border/60 text-left font-mono text-[10px] uppercase tracking-wider text-muted">
                <th className="py-2 pr-3 font-medium">Book</th>
                <th className="py-2 pr-3 text-right font-medium">Live</th>
                <th className="py-2 text-right font-medium">Research</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40">
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
              bracket-side decision evaluations in the last 24h
            </>
          ) : (
            "Current decision activity is unavailable until publication recovers"
          )}{" "}
          · station <span className="font-mono text-foreground">{city.station_id ?? "—"}</span>
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
  const currentStateAvailable = operational.state === "fresh";
  // The bracket-level signal artifact is San-Francisco-only, so gate on the
  // flagship blend AND the SFO slug — never attach SF's book to another city.
  const isFlagship = !!city.has_full_blend && city.slug === "sfo" && !!flagshipTarget;
  const showBrackets = isFlagship && currentStateAvailable;
  const mc = flagshipTarget?.market_consensus;
  const ladder = ladderBalance(mc?.overround);
  const approvedCount = flagshipTarget?.decisions?.filter((decision) => decision.approved).length ?? 0;

  return (
    <div className="space-y-6">
      <Reveal>
        <div className="grid gap-6 lg:grid-cols-[1.02fr_0.98fr]">
          <ForecastPanel city={city} intradayLock={intradayLock} />
          <BookPanel city={city} currentStateAvailable={currentStateAvailable} />
        </div>
      </Reveal>

      {isFlagship && !currentStateAvailable ? (
        <Reveal>
          <div role="status" className="flex items-start gap-3 rounded-2xl bg-surface-secondary px-4 py-4">
            <Icon icon="solar:clock-circle-bold" className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
            <p className="text-sm leading-relaxed text-muted">
              Current bracket prices, gate decisions, and prediction-market microstructure are unavailable until publication recovers.
              The calibrated forecast and settled history remain visible above.
            </p>
          </div>
        </Reveal>
      ) : showBrackets && flagshipTarget ? (
        <Reveal>
          <DetailDisclosure
            id="flagship-market-detail"
            icon="solar:chart-square-bold"
            title="Flagship market detail"
            note="Runtime inputs, pricing pipeline, bracket edge chart, market comparison, and gate traces"
          >
            <div className="flex items-center gap-2 text-sm text-muted">
              <Icon icon="solar:star-bold" className="size-4 shrink-0 text-accent" aria-hidden="true" />
              <span>
                {city.name} is the flagship, so it publishes bracket-level prediction-market microstructure
                on top of the calibrated forecast every city carries.
              </span>
            </div>

            <PipelineStepper />

            <div className="grid gap-6 lg:grid-cols-[1.02fr_0.98fr]">
              <ForecastInputs target={flagshipTarget} />
              <DecisionCard target={flagshipTarget} approvedCount={approvedCount} />
            </div>

            <EdgeChart target={flagshipTarget} />
            {mc?.available && (
              <Finding>
                The model reads the {targetLabel(flagshipTarget.target_date).toLowerCase()} high at{" "}
                <strong>{f1(mc.model_high_f)}</strong> while the market implies{" "}
                <strong>{f1(mc.implied_high_f)}</strong> — a{" "}
                <strong>
                  {mc.model_minus_market_f > 0 ? "+" : ""}
                  {round1(mc.model_minus_market_f)}°F
                </strong>{" "}
                disagreement. The market's most-likely bracket is <strong>{mc.modal_bin_label}</strong> at{" "}
                {pct(mc.modal_probability, 0)}.
                {ladder && (
                  <>
                    {" "}The book's raw ladder prices {ladder.lead}
                    <strong>{ladder.parLabel}</strong> of par — {ladder.descriptor}, not a cost to
                    beat: the market probabilities shown here are already normalized by that total.
                  </>
                )}{" "}
                This report marks{" "}
                <strong>{approvedCount}</strong> bracket-side candidate{approvedCount === 1 ? "" : "s"} eligible for this target;
                the separate paper-account scan handles admission and placement.
              </Finding>
            )}

            <MarketBook target={flagshipTarget} />
          </DetailDisclosure>
        </Reveal>
      ) : (
        <Reveal>
          <p className="rounded-2xl bg-surface-secondary/70 px-4 py-4 text-sm leading-relaxed text-muted">
            {city.name} publishes its calibrated forecast, official settlement and paper-book activity
            here. Bracket-level market microstructure — the model-vs-market bin overlay and the full
            gate trace — is published for the San Francisco flagship.
          </p>
        </Reveal>
      )}
    </div>
  );
}
