import { Card, Chip } from "@heroui/react";
import { Icon } from "@iconify/react";
import { pct } from "../../lib/data";
import { money, profileGate, type ProfileEntry, type StrategyLab } from "../../lib/strategy";
import { usePublication } from "../../lib/publication";

const PROFILE_META: Record<string, { icon: string; blurb: string }> = {
  live: {
    icon: "solar:shield-check-bold",
    blurb: "Real-money candidate — trades only on a non-negative lower-bound edge, with forecast sources in agreement and reliable liquidity. Trades infrequently by design.",
  },
  research: {
    icon: "solar:test-tube-bold",
    blurb: "Experimental book — the loosest filters at the smallest stakes, so it records the full range of opportunities quickly.",
  },
};

interface MetricRow {
  label: string;
  render: (p: ProfileEntry, s: StrategyLab) => { text: string; tone?: "pos" | "neg" };
}

const ROWS: MetricRow[] = [
  {
    label: "Resolved trades",
    render: (p) => {
      const sum = p.paper_trading?.summary;
      const wl = sum ? ` · ${sum.win_count}–${sum.loss_count}` : "";
      return { text: `${sum?.closed_positions ?? 0}${wl}` };
    },
  },
  {
    label: "Hit rate",
    render: (p) => {
      const hr = p.paper_trading?.summary?.hit_rate;
      return { text: hr == null ? "—" : pct(hr, 1) };
    },
  },
  {
    label: "Realized P&L",
    render: (p) => {
      const v = p.paper_trading?.summary?.realized_pnl ?? 0;
      return { text: money(v), tone: v > 0 ? "pos" : v < 0 ? "neg" : undefined };
    },
  },
  {
    label: "ROI · resolved",
    render: (p) => {
      const roi = p.paper_trading?.summary?.roi;
      return { text: roi == null ? "—" : pct(roi, 1), tone: (roi ?? 0) > 0 ? "pos" : (roi ?? 0) < 0 ? "neg" : undefined };
    },
  },
  {
    label: "Open now",
    render: (p) => ({ text: `${p.paper_trading?.summary?.open_positions ?? 0}` }),
  },
  {
    label: "Candidates this scan",
    render: (p) => ({ text: `${p.status?.latest_signal_count ?? 0}` }),
  },
];

const CURRENT_ROWS = new Set(["Open now", "Candidates this scan"]);

function BookColumn({ s, p, currentStateAvailable }: { s: StrategyLab; p: ProfileEntry; currentStateAvailable: boolean }) {
  const meta = PROFILE_META[p.risk_profile];
  const primary = p.profile_type === "primary";
  const gate = profileGate(s, p.risk_profile);
  const approvalRate = gate && gate.signals > 0 ? gate.approved / gate.signals : null;
  const alertOk = currentStateAvailable && (p.status?.alert_level ?? "ok") === "ok";
  const barColor = primary ? "bg-accent" : "bg-[color:var(--series-market)]";

  return (
    <Card className="h-full rounded-2xl ring-1 ring-border/70">
      <Card.Header className="flex flex-row items-start justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <span
            className={`grid size-9 place-items-center rounded-lg ring-1 ${
              primary ? "bg-accent-soft text-accent ring-accent/25" : "bg-surface-secondary text-[color:var(--series-market)] ring-border/60"
            }`}
          >
            <Icon icon={meta?.icon ?? "solar:notebook-bold"} className="size-4.5" aria-hidden="true" />
          </span>
          <div>
            <Card.Title className="text-base">{p.label}</Card.Title>
            <p className="flex items-center gap-1.5 text-xs text-muted">
              <span className={`size-1.5 rounded-full ${alertOk ? "bg-success" : "bg-warning"}`} aria-hidden="true" />
              {currentStateAvailable
                ? p.status?.paper_trading_status ?? (primary ? "primary book" : "experimental book")
                : "Current profile status unavailable"}
            </p>
          </div>
        </div>
        <Chip size="sm" variant="soft" color={primary ? "warning" : "default"}>
          <Chip.Label>{primary ? "Primary" : "Experimental"}</Chip.Label>
        </Chip>
      </Card.Header>
      <Card.Content className="space-y-4 pt-0">
        {meta && <p className="text-xs leading-relaxed text-muted">{meta.blurb}</p>}

        <dl className="divide-y divide-border/50">
          {ROWS.map((row) => {
            const { text, tone } =
              !currentStateAvailable && CURRENT_ROWS.has(row.label)
                ? { text: "Unavailable" }
                : row.render(p, s);
            const toneClass = tone === "pos" ? "text-success" : tone === "neg" ? "text-danger" : "text-foreground";
            return (
              <div key={row.label} className="flex items-baseline justify-between gap-3 py-2">
                <dt className="text-xs text-muted">{row.label}</dt>
                <dd className={`tnum font-display text-sm font-semibold ${toneClass}`}>{text}</dd>
              </div>
            );
          })}
        </dl>

        {gate && gate.signals > 0 && (
          <div>
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-[11px] uppercase tracking-wide text-muted">Gate approvals · window</span>
              <span className="tnum text-xs font-medium text-foreground">
                {gate.approved.toLocaleString()}
                <span className="font-normal text-muted"> / {gate.signals.toLocaleString()}</span>
              </span>
            </div>
            <div
              className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-foreground/10"
              role="img"
              aria-label={`${p.label} approved ${gate.approved} of ${gate.signals} scans (${pct(approvalRate, 2)}).`}
            >
              <div className={`h-full rounded-full ${barColor}`} style={{ width: `${Math.max((approvalRate ?? 0) * 100, 0.6)}%` }} />
            </div>
            <p className="mt-1 text-[11px] text-muted">{pct(approvalRate, 2)} approval rate — selectivity is the strategy.</p>
          </div>
        )}
      </Card.Content>
    </Card>
  );
}

/** The two isolated books shown TOGETHER, side by side — same metric rows in
    the same order so size, activity, hit rate and P&L compare at a glance. No
    toggle: both books are always visible. */
export function ProfileComparison({ s }: { s: StrategyLab }) {
  const { strategy } = usePublication();
  const currentStateAvailable = strategy.state === "fresh";
  const rank = (p: ProfileEntry) => (p.profile_type === "primary" ? 0 : 1);
  const profiles = [...(s.profiles ?? [])].sort((a, b) => rank(a) - rank(b));
  if (profiles.length < 2) return null;
  return (
    <div className="grid gap-4 md:grid-cols-2">
      {profiles.map((p) => (
        <BookColumn key={p.risk_profile} s={s} p={p} currentStateAvailable={currentStateAvailable} />
      ))}
    </div>
  );
}
