import { Icon } from "@iconify/react";
import { selectCurrentTargets, type Target } from "../../lib/data";

export function TargetStatusWarning({ targets }: { targets: Target[] }) {
  const pastDue = targets.filter((target) => target.target_status === "past");
  if (!pastDue.length) return null;
  const currentCount = selectCurrentTargets(targets).length;

  return (
    <div role="alert" className="flex items-start gap-3 rounded-xl bg-danger-soft px-4 py-3 text-sm ring-1 ring-danger/30">
      <Icon icon="solar:danger-triangle-bold" className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden="true" />
      <p className="leading-relaxed text-muted">
        <span className="font-semibold text-foreground">
          {pastDue.length} past-due prediction-market target{pastDue.length === 1 ? "" : "s"} detected.
        </span>{" "}
        {currentCount > 0
          ? "Past rows are archived and excluded from current status."
          : "No settlement-day or upcoming target is published; current market state is unavailable until the pipeline advances."}
      </p>
    </div>
  );
}
