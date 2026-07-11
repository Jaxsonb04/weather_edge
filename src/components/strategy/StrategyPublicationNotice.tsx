import { Icon } from "@iconify/react/offline";
import { usePublication } from "../../lib/publication";

function formatTime(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return null;
  return new Date(parsed).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZone: "UTC",
    timeZoneName: "short",
  });
}

export function StrategyPublicationNotice({ generatedAt }: { generatedAt?: string }) {
  const { strategy, manifest, error } = usePublication();
  if (strategy.state === "fresh") return null;
  if (strategy.state === "unknown" && !manifest && !error) return null;

  const timestamp = strategy.generatedAt ?? generatedAt ?? null;
  const formatted = formatTime(timestamp);
  const stale = strategy.state === "stale";

  return (
    <div
      role={stale ? "alert" : "status"}
      aria-live={stale ? undefined : "polite"}
      className={`mb-6 flex items-start gap-3 rounded-xl px-4 py-3 ring-1 ${
        stale ? "bg-danger-soft ring-danger/30" : "bg-surface-secondary ring-border/70"
      }`}
    >
      <Icon
        icon={stale ? "solar:danger-triangle-bold" : "solar:info-circle-bold"}
        className={`mt-0.5 size-4 shrink-0 ${stale ? "text-danger" : "text-warning"}`}
        aria-hidden="true"
      />
      <div className="text-sm leading-relaxed">
        <p className="font-semibold text-foreground">
          {stale ? "Strategy Lab data is behind." : "Live Strategy Lab status unavailable."}
        </p>
        <p className="text-muted">
          Open positions, pending limits, and current candidate counts aren't shown until the feed catches up.
          {formatted && timestamp && (
            <>
              {" "}Artifact generated <time dateTime={timestamp}>{formatted}</time>.
            </>
          )}
        </p>
      </div>
    </div>
  );
}
