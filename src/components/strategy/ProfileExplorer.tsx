import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { Button } from "@heroui/react/button";
import { Chip } from "@heroui/react/chip";
import { Icon } from "@iconify/react/offline";
import { activeProfiles, money, type ProfileEntry, type StrategyLab } from "../../lib/strategy";
import { ProfileDashboard } from "./ProfileDashboard";

const ICONS: Record<string, string> = {
  live: "solar:shield-check-bold",
  research: "solar:test-tube-bold",
  "research-target": "solar:target-bold",
  "research-motion": "solar:chart-2-bold",
};

const ROLES: Record<string, string> = {
  live: "Readiness candidate",
  research: "Legacy experiment",
  "research-target": "Fixed daily objective",
  "research-motion": "Execution learning",
};

function ProfileOption({ profile, index, active, reduceMotion, onSelect }: {
  profile: ProfileEntry;
  index: number;
  active: boolean;
  reduceMotion: boolean | null;
  onSelect: () => void;
}) {
  const buttonRef = useRef<HTMLButtonElement>(null);
  const wasActive = useRef(active);
  const pnl = profile.paper_trading?.summary?.realized_pnl ?? 0;
  const closed = profile.paper_trading?.summary?.closed_positions ?? 0;
  const pnlTone = pnl > 0 ? "text-success" : pnl < 0 ? "text-danger" : "text-foreground";

  useEffect(() => {
    if (active && !wasActive.current) {
      buttonRef.current?.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "nearest", inline: "center" });
    }
    wasActive.current = active;
  }, [active, reduceMotion]);

  return (
    <Button
      ref={buttonRef}
      variant="ghost"
      onPress={onSelect}
      aria-pressed={active}
      className={`group h-auto min-h-44 min-w-[16rem] flex-1 touch-manipulation justify-start rounded-2xl p-0 text-left focus-visible:ring-2 focus-visible:ring-[color:var(--focus)] sm:min-w-0 ${
        active
          ? "bg-surface shadow-md ring-1 ring-accent/45"
          : "bg-surface-secondary/70 ring-1 ring-border/55 hover:bg-surface-secondary"
      }`}
    >
      <span className="flex w-full flex-col gap-4 p-4">
        <span className="flex items-start justify-between gap-3">
          <span className={`grid size-10 place-items-center rounded-xl ${active ? "bg-accent-soft text-accent" : "bg-background text-muted"}`}>
            <Icon icon={ICONS[profile.risk_profile] ?? "solar:notebook-bold"} className="size-5" aria-hidden="true" />
          </span>
          <span className="font-mono text-[10px] tracking-[0.16em] text-muted">0{index + 1}</span>
        </span>
        <span className="min-w-0">
          <span className="block min-h-10 text-balance font-display text-sm font-semibold leading-snug text-foreground">{profile.label}</span>
          <span className="mt-1 block text-xs text-muted">{ROLES[profile.risk_profile] ?? "Research profile"}</span>
        </span>
        <span className="flex items-end justify-between gap-3">
          <span>
            <span className="block text-[10px] uppercase tracking-wide text-muted">Realized P&amp;L</span>
            <span className={`tnum mt-0.5 block font-display text-lg font-semibold ${pnlTone}`}>{money(pnl)}</span>
          </span>
          <Chip size="sm" variant="soft" color={active ? "warning" : "default"}>
            <Chip.Label>{closed} resolved</Chip.Label>
          </Chip>
        </span>
      </span>
    </Button>
  );
}

/** A profile rail behaves like a small stack of instrument presets: it exposes
    each book's identity and headline result before opening one complete book at
    a time. This replaces the cramped segmented toggle and side-by-side cards. */
export function ProfileExplorer({ s }: { s: StrategyLab }) {
  const profiles = activeProfiles(s);
  const reduce = useReducedMotion();
  const defaultProfile = profiles.some((profile) => profile.risk_profile === s.default_profile)
    ? s.default_profile
    : profiles[0]?.risk_profile;
  const [selected, setSelected] = useState<string>(defaultProfile ?? "live");
  if (!profiles.length) return null;
  const active = profiles.find((profile) => profile.risk_profile === selected) ?? profiles[0];

  return (
    <div>
      <div className="profile-rail -mx-1 overflow-x-auto px-1 pb-3" aria-label="Strategy profile selector">
        <div className="flex min-w-max gap-3 sm:min-w-0" role="group" aria-label="Choose a strategy profile">
          {profiles.map((profile, index) => (
            <ProfileOption
              key={profile.risk_profile}
              profile={profile}
              index={index}
              active={profile.risk_profile === active.risk_profile}
              reduceMotion={reduce}
              onSelect={() => setSelected(profile.risk_profile)}
            />
          ))}
        </div>
      </div>

      <div className="mt-5 flex items-center gap-3 rounded-xl bg-surface-secondary/60 px-4 py-3 ring-1 ring-border/50">
        <span className="relative flex size-2" aria-hidden="true">
          <span className="absolute inline-flex size-full animate-ping rounded-full bg-accent opacity-50 motion-reduce:animate-none" />
          <span className="relative inline-flex size-2 rounded-full bg-accent" />
        </span>
        <p className="text-xs text-muted">
          Inspecting <strong className="text-foreground">{active.label}</strong> · equity, filtering, exposure, trades, and learnings stay scoped to this profile.
        </p>
      </div>

      <AnimatePresence mode="wait" initial={false}>
        <motion.div
          key={active.risk_profile}
          initial={reduce ? false : { opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          exit={reduce ? undefined : { opacity: 0, y: -8 }}
          transition={{ duration: 0.24, ease: [0.16, 1, 0.3, 1] }}
          className="mt-6"
        >
          <ProfileDashboard s={s} p={active} />
        </motion.div>
      </AnimatePresence>
    </div>
  );
}
