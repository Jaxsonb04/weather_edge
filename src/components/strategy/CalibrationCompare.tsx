import { Card, Chip } from "@heroui/react";
import { Icon } from "@iconify/react";
import { pct } from "../../lib/data";
import type { CalibrationSide, StrategyLab } from "../../lib/strategy";
import { Stat } from "../ui/Stat";

function SideCard({ side, pinned }: { side: CalibrationSide; pinned: boolean }) {
  return (
    <Card className="h-full rounded-2xl ring-1 ring-border/70">
      <Card.Header className="flex flex-row items-start justify-between gap-3">
        <div>
          <Card.Title className="text-base">{side.role ?? (pinned ? "Active calibration" : "Challenger")}</Card.Title>
          <p className="mt-0.5 font-mono text-[11px] text-muted">source: {side.source ?? "—"}</p>
        </div>
        <Chip size="sm" variant="soft" color={pinned ? "success" : "default"}>
          <Chip.Label>{pinned ? "Pinned to execution" : "Shadow only"}</Chip.Label>
        </Chip>
      </Card.Header>
      <Card.Content className="pt-0">
        {side.available ? (
          <div className="grid grid-cols-2 gap-3">
            <Stat label="RPS skill vs climatology" value={pct(side.ranked_probability_skill, 1)} tone="pos" />
            <Stat label="Brier skill" value={pct(side.brier_skill, 1)} />
            <Stat label="Top-bin accuracy" value={pct(side.top_bin_accuracy, 1)} />
            <Stat label="Scored outcomes" value={`${(side.sample_size ?? 0).toLocaleString()} of ${(side.outcome_count ?? 0).toLocaleString()}`} />
          </div>
        ) : (
          <div className="flex gap-2.5 rounded-xl bg-surface-secondary p-3 text-sm text-muted ring-1 ring-border/40">
            <Icon icon="solar:hourglass-line-bold" className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
            <span>{side.reason ?? "Not enough clean data yet."}</span>
          </div>
        )}
      </Card.Content>
    </Card>
  );
}

/** Champion/challenger governance: the execution calibration is locked to the
    champion until the challenger earns a clean, sufficient sample. */
export function CalibrationCompare({ s }: { s: StrategyLab }) {
  const c = s.calibration_comparison;
  if (!c?.active) return null;
  return (
    <div className="space-y-4">
      <div className="grid gap-5 lg:grid-cols-2">
        <SideCard side={c.active} pinned />
        {c.challenger && <SideCard side={c.challenger} pinned={false} />}
      </div>
      {c.comparison?.recommendation && (
        <div className="flex items-center gap-2.5 rounded-xl bg-surface-secondary/70 px-4 py-3 text-sm ring-1 ring-border/50">
          <Icon icon="solar:lock-keyhole-bold" className="size-4 shrink-0 text-accent" aria-hidden="true" />
          <span className="text-muted">
            <span className="font-medium text-foreground">{c.comparison.label ?? "Verdict"}:</span>{" "}
            {c.comparison.recommendation}
          </span>
        </div>
      )}
    </div>
  );
}
