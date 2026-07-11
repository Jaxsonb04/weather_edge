import { Card, Chip } from "@heroui/react";
import { Icon } from "@iconify/react";
import { pct } from "../../lib/data";
import { usePublication } from "../../lib/publication";
import {
  ledgerByCity,
  ledgerForProfile,
  money,
  monitorForProfile,
  profileGate,
  profileGateCounts,
  type ProfileEntry,
  type StrategyLab,
} from "../../lib/strategy";
import { Stat } from "../ui/Stat";
import { EquityCurve } from "./EquityCurve";
import { CandidateScatter, EdgeByPriceChart } from "./SignalCharts";
import { ExitReasonBars, SidePerformanceList } from "./ExitPolicyCard";
import { OpenBook } from "./OpenBook";
import { MonitorLog } from "./MonitorLog";
import { LedgerTable } from "./LedgerTable";

const PROFILE_COPY: Record<string, { icon: string; blurb: string }> = {
  live: {
    icon: "solar:shield-check-bold",
    blurb:
      "The real-money candidate. Trades only when the lower-bound edge is non-negative, forecast sources agree, and liquidity is structural — and stays paper-only until every go-live check passes.",
  },
  research: {
    icon: "solar:test-tube-bold",
    blurb:
      "The experimental book. Runs the loosest filters at the smallest stakes so it records the full range of opportunities quickly. Its P&L is kept separate from the live candidate's record.",
  },
};

/** Small labelled divider for sub-sections inside a single book's dashboard. */
function SubHead({ icon, title, note }: { icon: string; title: string; note?: string }) {
  return (
    <div className="mb-3 flex items-center gap-2">
      <Icon icon={icon} className="size-4 shrink-0 text-accent" aria-hidden="true" />
      <h4 className="font-display text-sm font-semibold text-foreground">{title}</h4>
      {note && <span className="ml-auto text-[11px] text-muted">{note}</span>}
    </div>
  );
}

/** Everything for ONE book: its KPIs, equity, gate, signal quality, exits,
    lessons, and its own positions/ledger/monitor filtered to this profile. */
export function ProfileDashboard({ s, p }: { s: StrategyLab; p: ProfileEntry }) {
  const { strategy } = usePublication();
  const currentStateAvailable = strategy.state === "fresh";
  const rp = p.risk_profile;
  const sum = p.paper_trading?.summary;
  const resolved = s.paper_trading?.diagnostics?.by_profile?.[rp];
  const gate = profileGate(s, rp);
  const gateCount = profileGateCounts(gate);
  const copy = PROFILE_COPY[rp];
  const primary = p.profile_type === "primary";
  const pnl = sum?.realized_pnl ?? 0;
  const charts = p.signal_quality?.charts;
  const days = p.daily_summary?.days;
  const rejections = (gate?.top_rejections_all?.length ? gate.top_rejections_all : gate?.top_rejections ?? []).slice(0, 5);
  const maxRejection = rejections[0]?.count ?? 1;
  const barColor = primary ? "bg-accent" : "bg-[color:var(--series-market)]";

  const ledger = ledgerForProfile(s, rp);
  const byCity = ledgerByCity(ledger);
  const allTimeClosed = sum?.closed_positions ?? 0;
  const monitorRows = monitorForProfile(s, rp);

  return (
    <div className="space-y-5">
      {/* identity + KPIs */}
      <Card className="rounded-2xl ring-1 ring-border/70">
        <Card.Header className="flex flex-row items-start justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <span
              className={`grid size-9 place-items-center rounded-lg ring-1 ${
                primary ? "bg-accent-soft text-accent ring-accent/25" : "bg-surface-secondary text-[color:var(--series-market)] ring-border/60"
              }`}
            >
              <Icon icon={copy?.icon ?? "solar:notebook-bold"} className="size-4.5" aria-hidden="true" />
            </span>
            <div>
              <Card.Title className="text-base">{p.label}</Card.Title>
              <p className="text-xs text-muted">
                {currentStateAvailable ? p.status?.paper_trading_status ?? "status unavailable" : "Current profile status unavailable"}
              </p>
            </div>
          </div>
          <Chip size="sm" variant="soft" color={primary ? "warning" : "default"}>
            <Chip.Label>{primary ? "Primary" : "Experimental"}</Chip.Label>
          </Chip>
        </Card.Header>
        <Card.Content className="space-y-4 pt-0">
          {copy && <p className="max-w-3xl text-sm leading-relaxed text-muted">{copy.blurb}</p>}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <Stat label="Resolved trades" value={`${allTimeClosed}`} />
            <Stat label="Hit rate" value={sum?.hit_rate == null ? "—" : `${pct(sum.hit_rate, 1)} · ${sum.win_count}–${sum.loss_count}`} />
            <Stat label="Realized P&L" value={money(pnl)} tone={pnl > 0 ? "pos" : pnl < 0 ? "neg" : "default"} />
            <Stat label="ROI · resolved" value={sum?.roi == null ? "—" : pct(sum.roi, 1)} tone={(sum?.roi ?? 0) > 0 ? "pos" : (sum?.roi ?? 0) < 0 ? "neg" : "default"} />
            <Stat label="Capital resolved" value={resolved?.capital_resolved != null ? `$${resolved.capital_resolved.toFixed(2)}` : "—"} />
            <Stat label="Candidates now" value={currentStateAvailable ? `${p.status?.latest_signal_count ?? 0}` : "Unavailable"} />
          </div>
        </Card.Content>
      </Card>

      {/* per-book equity curve */}
      {!!days?.length && (
        <EquityCurve
          s={s}
          days={days}
          startingBankroll={0}
          windowDays={p.daily_summary?.window_days}
          title={`${p.label} — P&L contribution`}
          description={`Cumulative realized P&L attributed to this book within the shared account · ${p.daily_summary?.window_days ?? days.length}-day view`}
          contributionMode
        />
      )}

      {/* this book's gate */}
      {gate && gateCount.signals > 0 && (
        <Card className="rounded-2xl ring-1 ring-border/70">
          <Card.Content className="grid gap-4 p-4 md:grid-cols-2">
            <div className="rounded-xl bg-surface-secondary p-3 ring-1 ring-border/50">
              <div className="flex items-baseline justify-between gap-2">
                <p className="text-[11px] uppercase tracking-wide text-muted">Gate approvals · window</p>
                <p className="tnum text-sm font-semibold">
                  {gateCount.approved.toLocaleString()} <span className="font-normal text-muted">of {gateCount.signals.toLocaleString()} scans</span>
                </p>
              </div>
              <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-foreground/10">
                <div className={`h-full rounded-full ${barColor}`} style={{ width: `${Math.max((gateCount.approved / gateCount.signals) * 100, 0.75)}%` }} />
              </div>
              <p className="mt-1.5 text-[11px] text-muted">{pct(gateCount.approved / gateCount.signals, 2)} approval rate — the gates do the heavy lifting.</p>
            </div>
            <div className="rounded-xl bg-surface-secondary p-3 ring-1 ring-border/50">
              <p className="mb-2 text-[11px] uppercase tracking-wide text-muted">Why this book says no</p>
              <ul className="space-y-1.5">
                {rejections.map((r) => (
                  <li key={r.reason} className="flex items-center gap-2.5">
                    <span className="w-32 shrink-0 truncate text-xs text-muted" title={r.reason}>{r.reason}</span>
                    <div className="h-1 flex-1 overflow-hidden rounded-full bg-foreground/8">
                      <div className={`h-full rounded-full ${barColor} opacity-70`} style={{ width: `${Math.max((r.count / maxRejection) * 100, 2)}%` }} />
                    </div>
                    <span className="tnum shrink-0 font-mono text-[10px] text-muted">{r.count.toLocaleString()}</span>
                  </li>
                ))}
              </ul>
            </div>
          </Card.Content>
        </Card>
      )}

      {/* this book's signal quality */}
      {charts?.probability_vs_market?.length ? (
        <div className="grid gap-5 lg:grid-cols-2">
          <CandidateScatter points={charts.probability_vs_market} targetDate={p.signal_quality?.latest_target_date} />
          {charts.edge_by_market_bucket?.length ? <EdgeByPriceChart buckets={charts.edge_by_market_bucket} /> : null}
        </div>
      ) : null}

      {/* exits + side + lessons */}
      <div className="grid gap-5 lg:grid-cols-2">
        <Card className="h-full rounded-2xl ring-1 ring-border/70">
          <Card.Header className="flex flex-row items-center gap-2">
            <Icon icon="solar:route-bold" className="size-4 text-accent" aria-hidden="true" />
            <Card.Title className="text-base">How this book's positions resolved</Card.Title>
          </Card.Header>
          <Card.Content className="space-y-5 pt-0">
            <div>
              <p className="mb-2 text-[11px] uppercase tracking-wide text-muted">Exit reasons</p>
              <ExitReasonBars reasons={p.daily_summary?.exit_reasons} emptyNote={`${p.label} recorded no monitored exits this window — the book has not been trading.`} />
            </div>
            <div>
              <p className="mb-2 text-[11px] uppercase tracking-wide text-muted">Performance by side</p>
              <SidePerformanceList side={p.daily_summary?.side_performance} emptyNote={`No resolved ${rp} trades to split by side this window.`} />
            </div>
          </Card.Content>
        </Card>

        <Card className="h-full rounded-2xl ring-1 ring-border/70">
          <Card.Header className="flex flex-row items-center gap-2">
            <Icon icon="solar:lightbulb-bolt-bold" className="size-4 text-accent" aria-hidden="true" />
            <Card.Title className="text-base">What this book learned</Card.Title>
          </Card.Header>
          <Card.Content className="space-y-4 pt-0">
            <ul className="space-y-2.5">
              {(p.learnings ?? []).map((l) => (
                <li key={l} className="flex gap-2.5 text-sm text-muted">
                  <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-accent" />
                  <span>{l}</span>
                </li>
              ))}
            </ul>
            {!!p.recommended_changes?.length && (
              <ul className="space-y-2">
                {p.recommended_changes.map((r) => (
                  <li key={r} className="flex gap-2.5 rounded-lg bg-surface-secondary p-3 text-sm text-muted ring-1 ring-border/40">
                    <Icon icon="solar:tuning-square-2-bold" className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
                    <span>{r}</span>
                  </li>
                ))}
              </ul>
            )}
          </Card.Content>
        </Card>
      </div>

      {/* this book's current exposure */}
      <div>
        <SubHead
          icon="solar:folder-open-bold"
          title="Current book state"
          note={currentStateAvailable ? "open positions + pending limits, scoped to this profile" : "unavailable until publication recovers"}
        />
        <OpenBook s={s} profile={rp} />
      </div>

      {/* this book's ledger + city lens */}
      <div>
        <SubHead
          icon="solar:clipboard-list-bold"
          title="Recent closed positions"
          note={`showing ${Math.min(ledger.length, allTimeClosed || ledger.length)} of ${allTimeClosed} resolved all-time`}
        />
        {byCity.length > 0 && (
          <div className="mb-3">
            <p className="mb-2 flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-muted">
              <Icon icon="solar:map-point-bold" className="size-3.5 text-accent" aria-hidden="true" />
              By settlement city
            </p>
            <div className="flex flex-wrap gap-2" aria-label="Closed positions grouped by settlement city">
              {byCity.map((c) => (
                <span key={c.slug} className="flex items-center gap-1.5 rounded-full bg-surface-secondary px-2.5 py-1 text-xs ring-1 ring-border/50">
                  <span className="font-medium text-foreground">{c.name}</span>
                  <span className="tnum text-muted">
                    {c.trades} trade{c.trades === 1 ? "" : "s"} · {c.wins}W
                  </span>
                  <span className={`tnum font-medium ${c.pnl > 0 ? "text-success" : c.pnl < 0 ? "text-danger" : "text-muted"}`}>{money(c.pnl)}</span>
                </span>
              ))}
              {byCity.length === 1 && (
                <span className="self-center text-[11px] text-muted">— other cities populate here as their markets settle</span>
              )}
            </div>
          </div>
        )}
        <LedgerTable
          s={s}
          rows={ledger}
          detailed
          hideProfile
          emptyNote={`No closed positions published for the ${rp} book in the current slice — its ${allTimeClosed} resolved trades roll off as newer ones settle.`}
        />
      </div>

      {/* this book's monitor exits */}
      {monitorRows.length > 0 && (
        <div>
          <SubHead icon="solar:history-bold" title="Monitor exits" note="rule-based closes for this book" />
          <MonitorLog s={s} rows={monitorRows} hideProfile />
        </div>
      )}
    </div>
  );
}
