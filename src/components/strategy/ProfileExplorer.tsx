import { useState } from "react";
import { Card, Chip } from "@heroui/react";
import { Segment } from "@heroui-pro/react";
import { Icon } from "@iconify/react";
import { pct } from "../../lib/data";
import { money, profileGate, type ProfileEntry, type StrategyLab } from "../../lib/strategy";
import { Stat } from "../ui/Stat";
import { CandidateScatter, EdgeByPriceChart } from "./SignalCharts";
import { ExitReasonBars, SidePerformanceList } from "./ExitPolicyCard";

const PROFILE_COPY: Record<string, { icon: string; blurb: string }> = {
  live: {
    icon: "solar:shield-check-bold",
    blurb:
      "The real-money candidate. Trades only when the lower-bound edge is non-negative, forecast sources agree, and liquidity is structural — and stays paper-only until every go-live check passes.",
  },
  research: {
    icon: "solar:test-tube-bold",
    blurb:
      "The experimental book. Runs the loosest gates at the smallest stakes so the journal fills with the full opportunity set fast. Its P&L is deliberately isolated from the live candidate's record.",
  },
};

function ProfileDetail({ s, p }: { s: StrategyLab; p: ProfileEntry }) {
  const sum = p.paper_trading?.summary;
  const resolved = s.paper_trading?.diagnostics?.by_profile?.[p.risk_profile];
  const gate = profileGate(s, p.risk_profile);
  const copy = PROFILE_COPY[p.risk_profile];
  const primary = p.profile_type === "primary";
  const pnl = sum?.realized_pnl ?? 0;
  const alertOk = (p.status?.alert_level ?? "ok") === "ok";
  const charts = p.signal_quality?.charts;
  const rejections = (gate?.top_rejections_all?.length ? gate.top_rejections_all : gate?.top_rejections ?? []).slice(0, 5);
  const maxRejection = rejections[0]?.count ?? 1;

  return (
    <div className="space-y-5">
      <Card className="rounded-2xl ring-1 ring-border/70">
        <Card.Header className="flex flex-row items-start justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <span className={`grid size-8 place-items-center rounded-lg ring-1 ${primary ? "bg-accent-soft text-accent ring-accent/25" : "bg-surface-secondary text-muted ring-border/60"}`}>
              <Icon icon={copy?.icon ?? "solar:notebook-bold"} className="size-4" aria-hidden="true" />
            </span>
            <div>
              <Card.Title className="text-base">{p.label}</Card.Title>
              <p className="flex items-center gap-1.5 text-xs text-muted">
                <span className={`size-1.5 rounded-full ${alertOk ? "bg-success" : "bg-warning"}`} aria-hidden="true" />
                {p.status?.paper_trading_status ?? "status unavailable"}
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
            <Stat label="Resolved trades" value={`${sum?.closed_positions ?? resolved?.resolved ?? 0}`} />
            <Stat
              label="Hit rate"
              value={sum?.hit_rate == null ? "—" : `${pct(sum.hit_rate, 1)} · ${sum.win_count}–${sum.loss_count}`}
            />
            <Stat label="Realized P&L" value={money(pnl)} tone={pnl > 0 ? "pos" : pnl < 0 ? "neg" : "default"} />
            <Stat
              label="ROI (resolved)"
              value={sum?.roi == null ? "—" : pct(sum.roi, 1)}
              tone={(sum?.roi ?? 0) > 0 ? "pos" : (sum?.roi ?? 0) < 0 ? "neg" : "default"}
            />
            <Stat label="Capital resolved" value={resolved?.capital_resolved != null ? `$${resolved.capital_resolved.toFixed(2)}` : "—"} />
            <Stat label="Candidates now" value={`${p.status?.latest_signal_count ?? 0}`} />
          </div>

          {gate && gate.signals > 0 && (
            <div className="grid gap-4 md:grid-cols-2">
              <div className="rounded-xl bg-surface-secondary p-3 ring-1 ring-border/50">
                <div className="flex items-baseline justify-between gap-2">
                  <p className="text-[11px] uppercase tracking-wide text-muted">Gate approvals · window</p>
                  <p className="tnum text-sm font-semibold">
                    {gate.approved.toLocaleString()}{" "}
                    <span className="font-normal text-muted">of {gate.signals.toLocaleString()} scans</span>
                  </p>
                </div>
                <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-foreground/10">
                  <div
                    className={`h-full rounded-full ${primary ? "bg-accent" : "bg-[color:var(--series-market)]"}`}
                    style={{ width: `${Math.max(0.75, (gate.approved / gate.signals) * 100)}%` }}
                  />
                </div>
                <p className="mt-1.5 text-[11px] text-muted">
                  {pct(gate.approved / gate.signals, 2)} approval rate — the gates do the heavy lifting.
                </p>
              </div>
              <div className="rounded-xl bg-surface-secondary p-3 ring-1 ring-border/50">
                <p className="mb-2 text-[11px] uppercase tracking-wide text-muted">Why this book says no</p>
                <ul className="space-y-1.5">
                  {rejections.map((r) => (
                    <li key={r.reason} className="flex items-center gap-2.5">
                      <span className="w-32 shrink-0 truncate text-xs text-muted" title={r.reason}>{r.reason}</span>
                      <div className="h-1 flex-1 overflow-hidden rounded-full bg-foreground/8">
                        <div
                          className={`h-full rounded-full ${primary ? "bg-accent/70" : "bg-[color:var(--series-market)]/70"}`}
                          style={{ width: `${Math.max((r.count / maxRejection) * 100, 2)}%` }}
                        />
                      </div>
                      <span className="tnum shrink-0 font-mono text-[10px] text-muted">{r.count.toLocaleString()}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}
        </Card.Content>
      </Card>

      {charts?.probability_vs_market?.length ? (
        <div className="grid gap-5 lg:grid-cols-2">
          <CandidateScatter points={charts.probability_vs_market} targetDate={p.signal_quality?.latest_target_date} />
          {charts.edge_by_market_bucket?.length ? <EdgeByPriceChart buckets={charts.edge_by_market_bucket} /> : null}
        </div>
      ) : null}

      <div className="grid gap-5 lg:grid-cols-2">
        <Card className="h-full rounded-2xl">
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

        <Card className="h-full rounded-2xl">
          <Card.Header className="flex flex-row items-center gap-2">
            <Icon icon="solar:route-bold" className="size-4 text-accent" aria-hidden="true" />
            <Card.Title className="text-base">How this book's positions resolved</Card.Title>
          </Card.Header>
          <Card.Content className="space-y-5 pt-0">
            <div>
              <p className="mb-2 text-[11px] uppercase tracking-wide text-muted">Exit reasons</p>
              <ExitReasonBars
                reasons={p.daily_summary?.exit_reasons}
                emptyNote={`${p.risk_profile} recorded no monitored exits in this window — the book has been standing down.`}
              />
            </div>
            <div>
              <p className="mb-2 text-[11px] uppercase tracking-wide text-muted">Performance by side</p>
              <SidePerformanceList
                side={p.daily_summary?.side_performance}
                emptyNote={`No resolved ${p.risk_profile} trades to split by side in this window.`}
              />
            </div>
          </Card.Content>
        </Card>
      </div>
    </div>
  );
}

/** The two isolated books, one at a time: a Segment toggle switches the whole
    panel so each profile tells its own story with its own numbers. */
export function ProfileExplorer({ s }: { s: StrategyLab }) {
  const profiles = s.profiles ?? [];
  const [selected, setSelected] = useState<string>(s.default_profile ?? profiles[0]?.risk_profile ?? "live");
  if (!profiles.length) return null;
  const active = profiles.find((x) => x.risk_profile === selected) ?? profiles[0];

  return (
    <div>
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <div className="max-w-full overflow-x-auto">
        <Segment
          aria-label="Risk profile"
          selectedKey={active.risk_profile}
          onSelectionChange={(k) => setSelected(String(k))}
        >
          {profiles.map((x) => (
            <Segment.Item key={x.risk_profile} id={x.risk_profile}>
              <span className="flex items-center gap-1.5">
                <Icon icon={PROFILE_COPY[x.risk_profile]?.icon ?? "solar:notebook-bold"} className="size-3.5" aria-hidden="true" />
                {x.label}
              </span>
            </Segment.Item>
          ))}
        </Segment>
        </div>
        <p className="text-xs text-muted" aria-label="Both books at a glance">
          {profiles.map((x, i) => {
            const v = x.paper_trading?.summary?.realized_pnl ?? 0;
            return (
              <span key={x.risk_profile}>
                {i > 0 && <span className="mx-2 text-border">·</span>}
                <span className="font-mono uppercase">{x.risk_profile}</span>{" "}
                <span className={`tnum font-medium ${v > 0 ? "text-success" : v < 0 ? "text-danger" : "text-foreground"}`}>
                  {money(v)}
                </span>
              </span>
            );
          })}
        </p>
      </div>
      <div key={active.risk_profile}>
        <ProfileDetail s={s} p={active} />
      </div>
    </div>
  );
}
