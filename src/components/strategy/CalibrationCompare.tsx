import { Chip } from "@heroui/react/chip";
import { Widget } from "@heroui-pro/react/widget";
import { Icon } from "@iconify/react/offline";
import { pct } from "../../lib/data";
import type { CalibrationSide, StrategyLab } from "../../lib/strategy";
import { InfoTip } from "../ui/InfoTip";
import { Stat } from "../ui/Stat";

function SidePanel({ side, pinned }: { side: CalibrationSide; pinned: boolean }) {
  return (
    <div className="flex min-w-0 flex-col gap-4 rounded-2xl bg-surface-secondary p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-foreground">{pinned ? "Active" : "Challenger"}</p>
          <p className="truncate text-xs text-muted">{side.source ?? "Source unpublished"}</p>
        </div>
        <Chip size="sm" variant="soft" color={pinned ? "success" : "default"}>
          <Chip.Label>{pinned ? "Trading" : "Shadow only"}</Chip.Label>
        </Chip>
      </div>
      {side.available ? (
        <div className="grid grid-cols-2 gap-x-6 gap-y-4">
          <Stat label="RPS skill" value={pct(side.ranked_probability_skill, 1)} />
          <Stat label="Brier skill" value={pct(side.brier_skill, 1)} />
          <Stat label="Top-bin accuracy" value={pct(side.top_bin_accuracy, 1)} />
          <Stat label="Scored outcomes" value={`${(side.sample_size ?? 0).toLocaleString()} of ${(side.outcome_count ?? 0).toLocaleString()}`} />
        </div>
      ) : (
        <p className="flex gap-2 text-sm text-muted">
          <Icon icon="solar:hourglass-line-linear" className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
          <span>{side.reason ?? "Not enough clean data yet."}</span>
        </p>
      )}
    </div>
  );
}

/** Champion and challenger: trading stays on the active calibration until the
    challenger earns a clean, sufficient sample. */
export function CalibrationCompare({ s }: { s: StrategyLab }) {
  const c = s.calibration_comparison;
  if (!c?.active) return null;
  return (
    <Widget className="min-w-0">
      <Widget.Header>
        <div className="min-w-0">
          <div className="flex items-center gap-1">
            <Widget.Title>San Francisco calibration</Widget.Title>
            <InfoTip label="calibration scores">
              RPS and Brier skill compare the probability ladder with climatology (higher is better). Top-bin accuracy
              is how often the most likely bracket settled.
            </InfoTip>
          </div>
          <Widget.Description>Active versus challenger probability calibration</Widget.Description>
        </div>
      </Widget.Header>
      <Widget.Content className="flex flex-col gap-4">
        <div className="grid min-w-0 gap-3 md:grid-cols-2">
          <SidePanel side={c.active} pinned />
          {c.challenger && <SidePanel side={c.challenger} pinned={false} />}
        </div>
        {c.comparison?.recommendation && (
          <p className="text-sm text-muted">
            <span className="font-medium text-foreground">{c.comparison.label ?? "Verdict"}:</span>{" "}
            {c.comparison.recommendation.replace(/live model/gi, "active SFO calibration")}
          </p>
        )}
      </Widget.Content>
    </Widget>
  );
}
