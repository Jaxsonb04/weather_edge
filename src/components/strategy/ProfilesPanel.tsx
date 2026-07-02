import { Card, Chip } from "@heroui/react";
import { Icon } from "@iconify/react";
import { pct } from "../../lib/data";
import { money, profileGate, type ProfileEntry, type StrategyLab } from "../../lib/strategy";
import { Stat } from "../ui/Stat";

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

function ProfileCard({ s, p }: { s: StrategyLab; p: ProfileEntry }) {
  const sum = p.paper_trading?.summary;
  const resolved = s.paper_trading?.diagnostics?.by_profile?.[p.risk_profile];
  const gate = profileGate(s, p.risk_profile);
  const copy = PROFILE_COPY[p.risk_profile];
  const primary = p.profile_type === "primary";
  const pnl = sum?.realized_pnl ?? 0;
  const alertOk = (p.status?.alert_level ?? "ok") === "ok";

  return (
    <Card className="h-full rounded-2xl ring-1 ring-border/70">
      <Card.Header className="flex flex-row items-start justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <span className={`grid size-8 place-items-center rounded-lg ring-1 ${primary ? "bg-accent-soft text-accent ring-accent/25" : "bg-surface-secondary text-muted ring-border/60"}`}>
            <Icon icon={copy?.icon ?? "solar:notebook-bold"} className="size-4" aria-hidden="true" />
          </span>
          <div>
            <Card.Title className="text-base">{p.label}</Card.Title>
            <p className="font-mono text-[10px] uppercase tracking-[0.15em] text-muted">{p.risk_profile} profile</p>
          </div>
        </div>
        <Chip size="sm" variant="soft" color={primary ? "warning" : "default"}>
          <Chip.Label>{primary ? "Primary" : "Experimental"}</Chip.Label>
        </Chip>
      </Card.Header>
      <Card.Content className="space-y-4 pt-0">
        {copy && <p className="text-sm leading-relaxed text-muted">{copy.blurb}</p>}

        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
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
          <Stat label="Capital resolved" value={resolved ? `$${resolved.capital_resolved.toFixed(2)}` : "—"} />
          <Stat label="Candidates now" value={`${p.status?.latest_signal_count ?? 0}`} />
        </div>

        {gate && gate.signals > 0 && (
          <div className="rounded-xl bg-surface-secondary p-3 ring-1 ring-border/50">
            <div className="flex items-baseline justify-between gap-2">
              <p className="text-[11px] uppercase tracking-wide text-muted">Gate approvals · window</p>
              <p className="tnum text-sm font-semibold">
                {gate.approved.toLocaleString()} <span className="font-normal text-muted">of {gate.signals.toLocaleString()} scans</span>
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
        )}

        {!!p.learnings?.length && (
          <ul className="space-y-2">
            {p.learnings.map((l) => (
              <li key={l} className="flex gap-2.5 text-sm text-muted">
                <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-accent" />
                <span>{l}</span>
              </li>
            ))}
          </ul>
        )}
      </Card.Content>
      <Card.Footer className="flex items-center gap-2 border-t border-border/50 pt-3 text-xs text-muted">
        <span className={`size-1.5 rounded-full ${alertOk ? "bg-success" : "bg-warning"}`} aria-hidden="true" />
        <span>{p.status?.paper_trading_status ?? "status unavailable"}</span>
      </Card.Footer>
    </Card>
  );
}

/** The two isolated paper books, side by side — strict real-money candidate vs
    loose experimental data collector. */
export function ProfilesPanel({ s }: { s: StrategyLab }) {
  const profiles = s.profiles ?? [];
  if (!profiles.length) return null;
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      {profiles.map((p) => (
        <ProfileCard key={p.risk_profile} s={s} p={p} />
      ))}
    </div>
  );
}
