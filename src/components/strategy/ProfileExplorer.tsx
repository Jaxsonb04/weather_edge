import { useState } from "react";
import { Segment } from "@heroui-pro/react/segment";
import { Icon } from "@iconify/react/offline";
import { activeProfiles, type StrategyLab } from "../../lib/strategy";
import { ProfileDashboard } from "./ProfileDashboard";

const ICONS: Record<string, string> = {
  live: "solar:shield-check-bold",
  research: "solar:test-tube-bold",
  "research-target": "solar:target-bold",
  "research-motion": "solar:chart-2-bold",
};

/** Segmented profile selector → the full per-book dashboard. One book at a
    time, cleanly (the side-by-side comparison lives in the Book Overview
    above; this is where each book gets its complete diagnostics). */
export function ProfileExplorer({ s }: { s: StrategyLab }) {
  const profiles = activeProfiles(s);
  const [selected, setSelected] = useState<string>(s.default_profile ?? profiles[0]?.risk_profile ?? "live");
  if (!profiles.length) return null;
  const active = profiles.find((x) => x.risk_profile === selected) ?? profiles[0];

  return (
    <div>
      <div className="mb-5 max-w-full overflow-x-auto">
        <Segment aria-label="Select a book to inspect" selectedKey={active.risk_profile} onSelectionChange={(k) => setSelected(String(k))}>
          {profiles.map((x) => (
            <Segment.Item key={x.risk_profile} id={x.risk_profile}>
              <span className="flex items-center gap-1.5">
                <Icon icon={ICONS[x.risk_profile] ?? "solar:notebook-bold"} className="size-3.5" aria-hidden="true" />
                {x.label}
              </span>
            </Segment.Item>
          ))}
        </Segment>
      </div>
      <div key={active.risk_profile}>
        <ProfileDashboard s={s} p={active} />
      </div>
    </div>
  );
}
