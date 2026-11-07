import type { ReactNode } from "react";
import { Chip } from "@heroui/react/chip";
import { EmptyState } from "@heroui-pro/react/empty-state";
import { Widget } from "@heroui-pro/react/widget";
import { Icon } from "@iconify/react/offline";
import { m1Book, money, type M1Hypothesis, type M1StopRule, type StrategyLab } from "../../lib/strategy";
import { percent } from "../../lib/lab-format";
import { usePublication } from "../../lib/publication";
import { AnimatedNumber } from "../ui/AnimatedNumber";
import { DetailDisclosure } from "../ui/DetailDisclosure";
import { InfoTip } from "../ui/InfoTip";
import { LinkButton } from "../ui/LinkButton";
import { SectionHeading } from "../ui/SectionHeading";
import { Stat } from "../ui/Stat";
import { EquityCurve } from "./EquityCurve";

const PREREGISTRATION = "https://github.com/Jaxsonb04/weather_edge/blob/main/docs/research/m1_preregistration.md";
/** The pre-registered capital, for the empty state before the book exists. */
const REGISTERED_CAPITAL = 2_000;

type Tone = "success" | "warning" | "danger" | "default";
const STATUS: Record<string, { label: string; color: Tone }> = {
  ok: { label: "Clear", color: "success" },
  running: { label: "Running", color: "success" },
  not_yet_eligible: { label: "Not yet eligible", color: "default" },
  not_yet_checked: { label: "Not yet checked", color: "default" },
  insufficient_data: { label: "Too little data", color: "default" },
  fit_failed: { label: "Fit failed", color: "warning" },
  triggered: { label: "Triggered", color: "warning" },
  paused: { label: "Paused", color: "warning" },
  stopped: { label: "Stopped", color: "warning" },
  halted: { label: "Halted", color: "danger" },
};

const CAPS: Array<[string, string]> = [
  ["stake_dollars", "Stake per entry"],
  ["position_cap_dollars", "Per position"],
  ["event_cap_dollars", "Per event"],
  ["target_date_cap_dollars", "Per target date"],
  ["daily_loss_pause_dollars", "Daily loss pause"],
  ["cumulative_loss_stop_dollars", "Cumulative loss stop"],
];

const count = (n?: number | null) => (n == null ? "—" : n.toLocaleString());
const flat = (n?: number | null) => money(n, { sign: "negative-only" });

function when(value?: string | null) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

function stopDetail(rule: M1StopRule): string | null {
  switch (rule.rule) {
    case "fidelity":
      return rule.threshold ?? null;
    case "loss_stop":
      return `Stops at ${money(rule.threshold_dollars)}; realized ${money(rule.realized_pnl)}`;
    case "futility":
      return `After ${count(rule.min_settled_entries)} settled entries at ROI ${percent(rule.threshold_roi)} or worse; ${count(rule.settled_entries)} settled`;
    case "premise":
      return `Recalibration slope below ${rule.threshold_slope ?? "—"} over ${count(rule.window_target_dates)} target dates`;
    case "final_read":
      return `Ends at ${count(rule.entries_limit)} entries or ${count(rule.trading_days_limit)} trading days`;
    default:
      return null;
  }
}

function Meter({ label, value, limit }: { label: string; value?: number; limit?: number }) {
  if (!limit) return null;
  const done = Math.min(value ?? 0, limit);
  return (
    <div>
      <p className="flex items-baseline justify-between gap-3 text-xs text-muted">
        {label}
        <span className="tnum text-sm font-semibold text-foreground">
          {count(value ?? 0)} <span className="text-xs font-normal text-muted">of {count(limit)}</span>
        </span>
      </p>
      <div
        className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-default"
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={limit}
        aria-valuenow={done}
      >
        <div className="bar-grow h-full rounded-full bg-accent" style={{ width: `${(done / limit) * 100}%` }} />
      </div>
    </div>
  );
}

function StopRuleRow({ rule, currentStateAvailable }: { rule: M1StopRule; currentStateAvailable: boolean }) {
  const status = currentStateAvailable
    ? STATUS[rule.status ?? ""] ?? { label: rule.status?.replaceAll("_", " ") ?? "Unknown", color: "default" as Tone }
    : { label: "Unavailable", color: "default" as Tone };
  const detail = stopDetail(rule);
  return (
    <li className="flex items-start justify-between gap-3 py-3 first:pt-0 last:pb-0">
      <div className="min-w-0">
        <p className="text-sm font-medium text-foreground">{rule.label ?? rule.rule}</p>
        {detail && <p className="mt-0.5 text-xs leading-relaxed text-muted">{detail}</p>}
      </div>
      <Chip size="sm" variant="soft" color={status.color} className="shrink-0">
        <Chip.Label>{status.label}</Chip.Label>
      </Chip>
    </li>
  );
}

function HypothesisRow({ hypothesis: h }: { hypothesis: M1Hypothesis }) {
  const interval = h.lower == null || h.upper == null ? null : `${percent(h.lower, 1, "always")} to ${percent(h.upper, 1, "always")}`;
  return (
    <li className="py-3 first:pt-0 last:pb-0">
      <p className="text-sm text-foreground">
        <span className="font-semibold">{h.id}</span> {h.statement}
      </p>
      <p className="tnum mt-0.5 text-xs text-muted">
        ROI {percent(h.roi, 1, "always")}
        {interval ? ` · 95% ${interval}` : ""}
        {h.settled_entries != null ? ` · n=${h.settled_entries.toLocaleString()}` : ""}
        {h.alpha != null ? ` · α ${h.alpha}` : ""}
      </p>
    </li>
  );
}

function Panel({ title, sub, children }: { title: string; sub?: ReactNode; children: ReactNode }) {
  return (
    <Widget className="w-full min-w-0">
      <Widget.Header>
        <div className="flex min-w-0 flex-col gap-0.5">
          <Widget.Title>{title}</Widget.Title>
          {sub && <Widget.Description>{sub}</Widget.Description>}
        </div>
      </Widget.Header>
      <Widget.Content>{children}</Widget.Content>
    </Widget>
  );
}

/** The M1 market-recalibration trial as its own book. Its figures are read from
    `accounts.research_m1` and shown alone: nothing here is added to, or compared
    in one number with, any other account. Every block tolerates a missing field,
    because the book is absent until the runtime first publishes it. */
export function M1Trial({ s }: { s: StrategyLab }) {
  const { strategy } = usePublication();
  const currentStateAvailable = strategy.state === "fresh";
  const book = m1Book(s);
  const trial = book?.trial;
  const capital = book?.initial_equity ?? trial?.initial_capital ?? REGISTERED_CAPITAL;
  const heading = (
    <SectionHeading
      as="h2"
      title="M1 paper trial"
      sub={`Its own ${flat(capital).replace(".00", "")} paper book, never added to any other account.`}
      info={
        <InfoTip label="the M1 trial" title="What M1 tests">
          M1 recalibrates the prediction market's own prices and buys where the recalibrated probability beats the
          full cost. {trial?.promotion_note ?? "Nothing is promoted to real money from this run alone."}
        </InfoTip>
      }
    />
  );

  // An unverified book shows no figures, only the publisher's reason.
  if (!book || book.available === false || (!trial && book.available !== true)) {
    return (
      <section className="flex min-w-0 flex-col" aria-label="M1 paper trial">
        {heading}
        <div className="rounded-2xl bg-surface-secondary px-4 py-8">
          <EmptyState>
            <EmptyState.Header>
              <EmptyState.Media variant="icon">
                <Icon icon="solar:test-tube-linear" className="size-5" aria-hidden="true" />
              </EmptyState.Media>
              <EmptyState.Title>No M1 data published yet</EmptyState.Title>
              <EmptyState.Description>
                {book?.reason ?? "The trial's paper book appears here after its first publication."}
              </EmptyState.Description>
            </EmptyState.Header>
            <EmptyState.Content>
              <LinkButton href={PREREGISTRATION} variant="tertiary">Read the pre-registration</LinkButton>
            </EmptyState.Content>
          </EmptyState>
        </div>
      </section>
    );
  }

  const balance = book.realized_equity ?? null;
  const pnl = book.realized_pnl ?? trial?.book?.realized_pnl ?? null;
  const primary = trial?.hypotheses?.find((h) => h.id === "H1");
  const entriesOpen = trial?.entries_enabled_by_stop_rules;
  const lastEvaluation = when(trial?.health?.last_evaluation_at);
  const days = book.days?.filter((day) => day?.date && typeof day.closing_equity === "number" && Number.isFinite(day.closing_equity)) ?? [];
  const finalRead = trial?.stop_rules?.find((rule) => rule.rule === "final_read");
  const arms = Object.values(trial?.shadow_arms ?? {}).filter((arm) => arm != null);
  const caps = CAPS.filter(([key]) => typeof trial?.caps?.[key] === "number");

  return (
    <section className="flex min-w-0 flex-col" aria-label="M1 paper trial">
      {heading}
      <div className="flex min-w-0 flex-col gap-4">
        <Widget className="w-full min-w-0">
          <Widget.Header className="flex-wrap gap-2">
            <Widget.Title>Paper book</Widget.Title>
            <div className="flex flex-wrap gap-1.5">
              <Chip size="sm" variant="soft" color="success"><Chip.Label>Paper only</Chip.Label></Chip>
              {currentStateAvailable && entriesOpen != null && (
                <Chip size="sm" variant="soft" color={entriesOpen ? "default" : "danger"}>
                  <Chip.Label>{entriesOpen ? "Stop rules clear" : "Entries halted"}</Chip.Label>
                </Chip>
              )}
            </div>
          </Widget.Header>
          <Widget.Content className="flex flex-col gap-4">
            <div>
              <p className="text-xs text-muted">Balance</p>
              <p className="text-3xl font-semibold tracking-tight text-foreground">
                {balance == null ? "—" : (
                  <>
                    <span className="sr-only">{flat(balance)}</span>
                    <span aria-hidden="true">
                      <AnimatedNumber value={balance} format={{ style: "currency", currency: "USD" }} />
                    </span>
                  </>
                )}
              </p>
              <p className="tnum text-xs text-muted">from {flat(capital)}</p>
            </div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-4 border-t border-separator pt-4 sm:grid-cols-4">
              <Stat label="Realized P&L" value={money(pnl)} tone={pnl == null || pnl === 0 ? "default" : pnl > 0 ? "pos" : "neg"} />
              <Stat
                label="ROI · settled"
                value={percent(primary?.roi, 1, "always")}
                tone={primary?.roi == null || primary.roi === 0 ? "default" : primary.roi > 0 ? "pos" : "neg"}
                note={trial?.book?.settled_stake == null ? undefined : `on ${flat(trial.book.settled_stake)}`}
              />
              <Stat label="Open" value={currentStateAvailable ? count(book.open_positions) : "Unavailable"} note={!currentStateAvailable || book.open_cost_basis == null ? undefined : `${flat(book.open_cost_basis)} at risk`} />
              <Stat label="Settled entries" value={count(trial?.book?.settled_entries)} note={lastEvaluation ? `checked ${lastEvaluation}` : undefined} />
            </div>
          </Widget.Content>
        </Widget>

        {days.length > 1 && (
          <EquityCurve
            s={s}
            days={days}
            startingBankroll={capital}
            windowDays={days.length}
            title="Balance"
            description="M1 book only, realized, by day"
          />
        )}

        <div className="grid min-w-0 gap-4 lg:grid-cols-2">
          {(trial?.stop_rules?.length ?? 0) > 0 && (
            <Panel title="Stop rules" sub="Fixed before the first entry">
              <ul className="divide-y divide-separator">
                {trial?.stop_rules?.map((rule, index) => <StopRuleRow key={rule.rule ?? index} rule={rule} currentStateAvailable={currentStateAvailable} />)}
              </ul>
            </Panel>
          )}
          <div className="flex min-w-0 flex-col gap-4">
            {finalRead && (
              <Panel title="Road to the final read">
                <div className="flex flex-col gap-3">
                  <Meter label="Entries" value={finalRead.entries} limit={finalRead.entries_limit} />
                  <Meter label="Trading days" value={finalRead.trading_days} limit={finalRead.trading_days_limit} />
                </div>
              </Panel>
            )}
            {(trial?.hypotheses?.length ?? 0) > 0 && (
              <Panel
                title="Hypotheses"
                sub={trial?.inference_note ? (
                  <span className="inline-flex items-center gap-1">
                    Read with care
                    <InfoTip label="how to read the hypotheses">{trial.inference_note}</InfoTip>
                  </span>
                ) : undefined}
              >
                <ul className="divide-y divide-separator">
                  {trial?.hypotheses?.map((h, index) => <HypothesisRow key={h.id ?? index} hypothesis={h} />)}
                </ul>
              </Panel>
            )}
          </div>
        </div>

        {(caps.length > 0 || trial?.rule) && (
          <DetailDisclosure id="m1-rule" icon="solar:shield-check-linear" title="Entry rule and caps" note={trial?.policy_version}>
            {trial?.rule && (
              <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
                {trial.rule.sampling && <div><dt className="text-xs text-muted">Sampling</dt><dd className="text-foreground">{trial.rule.sampling}</dd></div>}
                {trial.rule.exits && <div><dt className="text-xs text-muted">Exits</dt><dd className="text-foreground">{trial.rule.exits}</dd></div>}
                {trial.rule.edge_threshold != null && <div><dt className="text-xs text-muted">Minimum edge</dt><dd className="tnum text-foreground">{percent(trial.rule.edge_threshold, 0)}</dd></div>}
                {trial.rule.lead_days != null && <div><dt className="text-xs text-muted">Lead</dt><dd className="tnum text-foreground">{trial.rule.lead_days} day</dd></div>}
              </dl>
            )}
            {caps.length > 0 && (
              <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
                {caps.map(([key, label]) => <Stat key={key} label={label} value={flat(trial?.caps?.[key])} />)}
              </div>
            )}
          </DetailDisclosure>
        )}

        {arms.length > 0 && (
          <DetailDisclosure id="m1-shadow" icon="solar:eye-linear" title="Shadow arms" note="Recorded only, never filled">
            <div className="grid gap-4 sm:grid-cols-2">
              {arms.map((arm) => (
                <Stat
                  key={arm.arm}
                  label={arm.arm?.replaceAll("_", " ") ?? "Shadow arm"}
                  value={percent(arm.roi, 1, "always")}
                  note={`${count(arm.settled_entries)} settled of ${count(arm.entries)}`}
                />
              ))}
            </div>
          </DetailDisclosure>
        )}
      </div>
    </section>
  );
}
