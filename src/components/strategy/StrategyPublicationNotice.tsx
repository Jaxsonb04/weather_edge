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

/** Shown only when the Strategy artifact is behind or its freshness unknown:
    live book state is withheld, and the artifact's time is given. */
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
      className={`fade-in flex items-start gap-3 rounded-2xl px-4 py-3 ${stale ? "bg-warning-soft" : "bg-surface-secondary"}`}
    >
      <Icon
        icon={stale ? "solar:clock-circle-linear" : "solar:info-circle-linear"}
        className={`mt-0.5 size-4 shrink-0 ${stale ? "text-warning" : "text-muted"}`}
        aria-hidden="true"
      />
      <p className="text-sm leading-relaxed text-muted">
        <span className="font-semibold text-foreground">
          {stale ? "Strategy Lab data is behind." : "Current Strategy Lab status unavailable."}
        </span>{" "}
        Open positions, pending limits, and current candidate counts aren't shown until it catches up.
        {formatted && timestamp && (
          <>
            {" "}Generated <time dateTime={timestamp}>{formatted}</time>.
          </>
        )}
      </p>
    </div>
  );
}
