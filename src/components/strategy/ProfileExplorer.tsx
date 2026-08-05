import { useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { Button } from "@heroui/react/button";
import { Chip } from "@heroui/react/chip";
import { Icon } from "@iconify/react/offline";
import { activeProfiles, money, profileDisplayLabel, type ProfileEntry, type StrategyLab } from "../../lib/strategy";
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

function ProfileOption({ profile, index, active, onSelect }: {
  profile: ProfileEntry;
  index: number;
  active: boolean;
  onSelect: () => void;
}) {
  const pnl = profile.paper_trading?.summary?.realized_pnl ?? 0;
  const closed = profile.paper_trading?.summary?.closed_positions ?? 0;
  const pnlTone = pnl > 0 ? "text-success" : pnl < 0 ? "text-danger" : "text-foreground";

  return (
    <Button
      variant="ghost"
      onPress={onSelect}
      aria-pressed={active}
      className={`group h-auto min-h-40 w-full min-w-0 touch-manipulation justify-start rounded-2xl p-0 text-left focus-visible:ring-2 focus-visible:ring-[color:var(--focus)] ${
        active
          ? "border border-accent/45 bg-surface shadow-md"
          : "border border-border/55 bg-surface-secondary/70 hover:bg-surface-secondary"
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
          <span className="block min-h-10 text-balance font-display text-sm font-semibold leading-snug text-foreground">{profileDisplayLabel(profile)}</span>
          <span className="mt-1 block text-xs text-muted">{ROLES[profile.risk_profile] ?? "Research profile"}</span>
        </span>
        <span className="flex items-end justify-between gap-3">
          <span>
            <span className="block text-[10px] uppercase tracking-wide text-muted">Realized P&amp;L</span>
            <span className={`tnum mt-0.5 block font-display text-lg font-semibold ${pnlTone}`}>{money(pnl)}</span>
          </span>
          <Chip size="sm" variant="soft" color={active ? "accent" : "default"}>
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
    <div className="mx-auto w-full">
      <div className="grid min-w-0 grid-cols-1 gap-3 pt-2 sm:grid-cols-2" role="group" aria-label="Choose a strategy profile">
        {profiles.map((profile, index) => (
          <ProfileOption
            key={profile.risk_profile}
            profile={profile}
            index={index}
            active={profile.risk_profile === active.risk_profile}
            onSelect={() => setSelected(profile.risk_profile)}
          />
        ))}
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
