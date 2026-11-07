import { KPI } from "@heroui-pro/react/kpi";
import { KPIGroup } from "@heroui-pro/react/kpi-group";
import { AnimatedNumber } from "../ui/AnimatedNumber";
import { InfoTip } from "../ui/InfoTip";
import { SectionHeading } from "../ui/SectionHeading";
import { fixtureCoverage, skillStatus, type ForecastData, type TradingSignal } from "../../lib/data";
import { useTemperature } from "../../lib/units";

interface Metric {
  title: string;
  value: number | null;
  kind: "pct" | "count" | "temp";
  unit?: string;
  /** Visible caption: only the static-fixture figures carry one, so their
      provenance is never one tap away from a live-looking number. */
  hint?: string;
}

export function SkillStrip({ forecast, signal }: { forecast: ForecastData; signal: TradingSignal }) {
  const temperature = useTemperature();
  const c = signal.calibration;
  // History and the LSTM sigma come from the committed `forecast_data.json`
  // study fixture, not the running pipeline; the sigma is the held-out LSTM
  // residual, wider than the operational EMOS sigma the forecast card shows.
  const coverage = fixtureCoverage(forecast);
  const fixtureHint = coverage ? `static fixture · ${coverage}` : "static fixture";
  const metrics: Metric[] = [
    { title: "Brier skill", value: c?.brier_skill ?? null, kind: "pct" },
    { title: "Rank-prob. skill", value: c?.ranked_probability_skill ?? null, kind: "pct" },
    { title: "Top-bin accuracy", value: c?.top_bin_accuracy ?? null, kind: "pct" },
    // `calibration.n` counts settlement DAYS; each scores the whole ladder.
    { title: "Settled days", value: c?.n ?? null, kind: "count" },
    {
      title: "History",
      value: forecast.n_years ?? null,
      kind: "count",
      unit: "yrs",
      hint: `${forecast.n_days_observed?.toLocaleString() ?? "—"} KSFO days · ${fixtureHint}`,
    },
    {
      title: "LSTM study σ",
      value: forecast.lstm_sigma == null ? null : temperature.delta(forecast.lstm_sigma),
      kind: "temp",
      unit: temperature.symbol,
      hint: forecast.lstm_sigma_days
        ? `SFO held-out residual · ${forecast.lstm_sigma_days} days · ${fixtureHint}`
        : `SFO held-out residual · ${fixtureHint}`,
    },
  ];

  return (
    <section aria-labelledby="skill-title" className="min-w-0">
      <SectionHeading
        id="skill-title"
        title="Forecast skill"
        sub="San Francisco, scored on held-out settlement days"
        info={
          <InfoTip label="forecast skill" title="Reading these scores">
            Brier and ranked-probability skill compare the probability ladder with climatology; higher is better.
            Top-bin accuracy is how often the most likely bracket settled. History and the LSTM σ come from a
            committed study fixture, not the live pipeline.
          </InfoTip>
        }
      />
      <KPIGroup className="grid grid-cols-2 gap-px bg-separator sm:grid-cols-3 lg:grid-cols-6">
        {metrics.map((m) => (
          <MetricTile key={m.title} m={m} />
        ))}
      </KPIGroup>
    </section>
  );
}

function MetricTile({ m }: { m: Metric }) {
  const isPct = m.kind === "pct";
  return (
    <KPI className="min-w-0 bg-surface">
      <KPI.Header>
        <KPI.Title className="text-xs">{m.title}</KPI.Title>
      </KPI.Header>
      <KPI.Content className="content-start items-start">
        <div className="flex items-baseline gap-1">
          {m.value == null ? (
            <span className="text-2xl font-semibold tracking-tight">—</span>
          ) : (
            <AnimatedNumber
              className="text-2xl font-semibold tracking-tight"
              value={m.value}
              format={
                isPct
                  ? { style: "percent", maximumFractionDigits: 1 }
                  : { maximumFractionDigits: m.value < 10 ? 2 : 0 }
              }
            />
          )}
          {m.unit && <span className="text-sm text-muted">{m.unit}</span>}
        </div>
        {isPct && m.value != null && (
          // Decorative magnitude cue only; the number above states the value.
          // KPI.Progress names its inner bar "Progress", so the subtree is
          // hidden rather than announcing several identically named bars.
          <KPI.Progress
            aria-hidden="true"
            className="mt-2"
            value={Math.max(0, Math.min(100, m.value * 100))}
            status={skillStatus(m.value * 100)}
          />
        )}
        {m.hint && <p className="mt-1.5 text-xs text-muted [grid-column:1/-1]">{m.hint}</p>}
      </KPI.Content>
    </KPI>
  );
}
