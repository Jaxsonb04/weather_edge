import { useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { activeProfiles, type StrategyLab } from "../../lib/strategy";
import { AccountCards } from "./AccountCards";
import { ProfileDashboard } from "./ProfileDashboard";

/** Accounts tab: the two paper accounts side by side, then one account's full
    detail. Switching accounts cross-fades the detail (motion/react, only on a
    user's selection; the first render is static so a background tab never
    waits on a frame to show content). */
export function ProfileExplorer({ s }: { s: StrategyLab }) {
  const profiles = activeProfiles(s);
  const reduce = useReducedMotion();
  const defaultProfile = profiles.some((profile) => profile.risk_profile === s.default_profile)
    ? s.default_profile
    : profiles[0]?.risk_profile;
  const [selected, setSelected] = useState<string>(defaultProfile ?? "live");
  const active = profiles.find((profile) => profile.risk_profile === selected) ?? profiles[0];

  return (
    <div className="flex min-w-0 flex-col gap-8">
      <AccountCards s={s} selected={active?.risk_profile ?? selected} onSelect={setSelected} />
      {active && s.available && s.accounting?.available !== false && (
        <AnimatePresence mode="wait" initial={false}>
          <motion.div
            key={active.risk_profile}
            initial={reduce ? { opacity: 0 } : { opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={reduce ? { opacity: 0 } : { opacity: 0, y: -6 }}
            transition={{ duration: reduce ? 0.12 : 0.22, ease: [0.165, 0.84, 0.44, 1] }}
            className="min-w-0"
          >
            <ProfileDashboard s={s} p={active} />
          </motion.div>
        </AnimatePresence>
      )}
    </div>
  );
}
