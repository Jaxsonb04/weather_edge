import { Icon } from "@iconify/react/offline";
import { usePublication, usePublicationClock } from "../../lib/publication";

function publicationTime(iso: string | null): string | null {
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

function publicationAge(iso: string | null, now: number): string | null {
  if (!iso) return null;
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return null;
  const minutes = Math.max(0, Math.floor((now - parsed) / 60_000));
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return `${hours}h${remainder ? ` ${remainder}m` : ""} ago`;
}

export function PublicationStatusBanner() {
  // Use the manifest-driven pipeline freshness (not the load-gated `operational`,
  // which stays "unknown" on routes that never fetch cities_data.json — e.g. the
  // Strategy Lab — and would fire this banner even though the feed is current).
  const { operationalPipeline: operational, manifest, error } = usePublication();
  const now = usePublicationClock();

  if (operational.state === "fresh") return null;
  if (operational.state === "unknown" && !manifest && !error) return null;

  if (operational.state === "stale") {
    const generated = publicationTime(operational.generatedAt);
    const age = publicationAge(operational.generatedAt, now);
    return (
      <div role="alert" className="border-b border-danger/35 bg-danger-soft text-foreground">
        <div className="mx-auto flex w-full max-w-6xl items-start gap-3 px-5 py-3 sm:px-8">
          <Icon icon="solar:danger-triangle-bold" className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden="true" />
          <div className="text-sm leading-relaxed">
            <p className="font-semibold">Live data is behind</p>
            <p className="text-muted">
              Real-time prediction-market and open-position data is paused until the feed catches up.
              {generated && (
                <>
                  {" "}Last operational publication: <time dateTime={operational.generatedAt ?? undefined}>{generated}</time>
                  {age ? ` (${age})` : ""}.
                </>
              )}
            </p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div role="status" aria-live="polite" className="border-b border-border/70 bg-surface-secondary text-foreground">
      <div className="mx-auto flex w-full max-w-6xl items-start gap-3 px-5 py-2.5 sm:px-8">
        <Icon icon="solar:info-circle-bold" className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
        <p className="text-sm leading-relaxed text-muted">
          <span className="font-medium text-foreground">Live status unavailable.</span>{" "}
          Real-time prediction-market and position data isn't being shown for this deployment.
        </p>
      </div>
    </div>
  );
}
