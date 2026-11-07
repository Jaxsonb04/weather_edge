import { Chip } from "@heroui/react/chip";
import { Icon } from "@iconify/react/offline";
import { pct } from "../../lib/data";
import {
  activeProfiles,
  money,
  profileDisplayLabel,
  researchDailyTarget,
  type ProfileEntry,
  type StrategyLab,
} from "../../lib/strategy";
import { AnimatedNumber } from "../ui/AnimatedNumber";
import { percent, policyMarker } from "../../lib/lab-format";

const ROLE: Record<string, string> = {
  live: "Readiness book",
  "research-target": "Research book",
  research: "Legacy research book",
};

/** Closing balances across the published window, for the card sparkline. */
function balanceSeries(profile: ProfileEntry): number[] {
  return [...(profile.daily_summary?.days ?? [])]
    .filter((day) => day?.date && typeof day.closing_equity === "number" && Number.isFinite(day.closing_equity))
    .sort((a, b) => a.date.localeCompare(b.date))
    .map((day) => day.closing_equity as number);
}

function Sparkline({ values, tone }: { values: number[]; tone: "pos" | "neg" | "flat" }) {
  if (values.length < 2) return null;
  const width = 96;
  const height = 28;
  const low = Math.min(...values);
  const high = Math.max(...values);
  const span = Math.max(high - low, 0.01);
  const points = values
    .map((value, index) => {
      const x = (index / (values.length - 1)) * width;
      const y = height - 2 - ((value - low) / span) * (height - 4);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const color = tone === "pos" ? "text-success" : tone === "neg" ? "text-danger" : "text-muted";
  return (
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className={`bar-grow h-7 w-24 shrink-0 ${color}`} aria-hidden="true">
      <polyline
        points={points}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

function AccountCard({
  s,
  profile,
  selected,
  onSelect,
}: {
  s: StrategyLab;
  profile: ProfileEntry;
  selected: boolean;
  onSelect: () => void;
}) {
  const summary = profile.paper_trading?.summary;
  const balance = profile.daily_summary?.current_equity;
  const start = profile.daily_summary?.starting_bankroll ?? null;
  const pnl = summary?.realized_pnl ?? null;
  // Only a research book runs a versioned ROI policy. researchDailyTarget()
  // falls back to the top-level research target, so it must never be asked
  // on behalf of the readiness book.
  const policy = profile.risk_profile.startsWith("research")
    ? policyMarker(researchDailyTarget(s, profile)?.policy_version)
    : null;
  const reason = profile.status?.entry_scanner_reason ?? "";
  const paused = /pause/i.test(reason);
  const open = summary?.open_positions ?? 0;
  const pending = summary?.pending_limit_orders ?? 0;
  const tone = pnl == null || pnl === 0 ? "flat" : pnl > 0 ? "pos" : "neg";
  const returnOnStart = pnl != null && start ? pnl / start : null;
  const label = profileDisplayLabel(profile, researchDailyTarget(s, profile)?.policy_version);

  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onSelect}
      data-account={profile.risk_profile}
      className={`group flex w-full min-w-0 cursor-pointer flex-col gap-4 rounded-2xl p-4 text-left transition-[scale,background-color] duration-150 active:scale-[0.99] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--focus)] motion-reduce:transition-none motion-reduce:active:scale-100 sm:p-5 ${
        selected
          ? "bg-surface shadow-surface ring-2 ring-accent"
          : "bg-surface-secondary hover:bg-surface-tertiary"
      }`}
    >
      <span className="flex min-w-0 items-start justify-between gap-3">
        <span className="min-w-0">
          <span className="block truncate text-sm font-semibold text-foreground">{label}</span>
          <span className="mt-0.5 flex items-center gap-1.5 text-xs text-muted">
            <span
              className={`size-1.5 shrink-0 rounded-full ${paused ? "bg-warning" : "bg-success"}`}
              aria-hidden="true"
            />
            {paused ? "Entries paused" : "Scanning"} · {ROLE[profile.risk_profile] ?? "Paper book"}
          </span>
        </span>
        {policy && (
          <Chip size="sm" variant="soft" color={selected ? "accent" : "default"}>
            <Chip.Label>Policy {policy}</Chip.Label>
          </Chip>
        )}
      </span>

      <span className="flex min-w-0 items-end justify-between gap-3">
        <span className="min-w-0">
          <span className="block text-xs text-muted">Balance</span>
          <span className="block text-3xl font-semibold tracking-tight text-foreground">
            {balance == null ? (
              "—"
            ) : (
              <>
                {/* Screen readers get the exact figure once; the rolling digits are visual. */}
                <span className="sr-only">{money(balance, { sign: "negative-only" })}</span>
                <span aria-hidden="true">
                  <AnimatedNumber value={balance} format={{ style: "currency", currency: "USD" }} />
                </span>
              </>
            )}
          </span>
        </span>
        <Sparkline values={balanceSeries(profile)} tone={tone} />
      </span>

      <span className="grid grid-cols-2 gap-x-3 gap-y-3 border-t border-separator pt-3 text-xs min-[480px]:grid-cols-4">
        <span className="min-w-0">
          <span className="block text-muted">Since start</span>
          <span className={`tnum block text-sm font-semibold ${tone === "pos" ? "text-success" : tone === "neg" ? "text-danger" : "text-foreground"}`}>
            {money(pnl)}
          </span>
          {returnOnStart != null && <span className="tnum block text-muted">{percent(returnOnStart, 1, "always")}</span>}
        </span>
        <span className="min-w-0">
          <span className="block text-muted">Closed</span>
          <span className="tnum block text-sm font-semibold text-foreground">{(summary?.closed_positions ?? 0).toLocaleString()}</span>
        </span>
        <span className="min-w-0">
          <span className="block text-muted">Profitable exits</span>
          <span className="tnum block text-sm font-semibold text-foreground">
            {summary?.hit_rate == null ? "—" : pct(summary.hit_rate, 1)}
          </span>
          {summary?.win_count != null && summary?.loss_count != null && (
            <span className="tnum block text-muted">{summary.win_count}–{summary.loss_count}</span>
          )}
        </span>
        <span className="min-w-0">
          <span className="block text-muted">Open</span>
          <span className="tnum block text-sm font-semibold text-foreground">{open}</span>
          <span className="tnum block text-muted">{pending} resting</span>
        </span>
      </span>
    </button>
  );
}

/** The two active paper accounts, side by side and never summed. Each card is
    also the selector for the account detail below. Fails closed: when the
    publication or its accounting is unavailable, no balance is shown at all. */
export function AccountCards({
  s,
  selected,
  onSelect,
}: {
  s: StrategyLab;
  selected: string;
  onSelect: (riskProfile: string) => void;
}) {
  const unavailable = !s.available || s.accounting?.available === false;
  if (unavailable) {
    const reason = !s.available
      ? s.reason ?? "The Strategy publication did not report an available artifact."
      : s.accounting?.reason ?? "Fresh active paper ledgers did not pass accounting validation.";
    return (
      <div role="alert" aria-label="Paper account state unavailable" className="flex items-start gap-3 rounded-2xl bg-warning-soft px-4 py-4">
        <Icon icon="solar:shield-warning-linear" className="mt-0.5 size-5 shrink-0 text-warning" aria-hidden="true" />
        <div className="min-w-0 text-sm">
          <p className="font-semibold text-foreground">Paper account state unavailable</p>
          <p className="mt-1 text-muted">{reason} Balances stay hidden until a reconciled publication arrives.</p>
        </div>
      </div>
    );
  }

  const profiles = activeProfiles(s);
  if (!profiles.length) {
    return (
      <p role="status" className="rounded-2xl bg-surface-secondary px-4 py-6 text-center text-sm text-muted">
        No active paper account is published right now.
      </p>
    );
  }

  return (
    <div role="group" aria-label="Paper accounts" className="grid min-w-0 gap-3 sm:grid-cols-2">
      {profiles.map((profile) => (
        <AccountCard
          key={profile.risk_profile}
          s={s}
          profile={profile}
          selected={profile.risk_profile === selected}
          onSelect={() => onSelect(profile.risk_profile)}
        />
      ))}
    </div>
  );
}
