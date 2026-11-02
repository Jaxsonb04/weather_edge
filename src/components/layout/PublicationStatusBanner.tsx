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

/** One slim line under the header when the publication pipeline is behind or
    its status cannot be read. Silent while the feed is current. */
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
      <div role="alert" className="fade-in border-b border-separator bg-warning-soft">
        <div className="mx-auto flex w-full max-w-6xl items-start gap-2.5 px-4 py-2.5 text-sm sm:px-6 lg:px-8">
          <Icon icon="solar:clock-circle-linear" className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
          <p className="leading-relaxed text-foreground">
            <span className="font-semibold">Data delayed.</span>{" "}
            <span className="text-muted">
              Live market and position figures are hidden until the next publication.
              {generated && (
                <>
                  {" "}Last published <time dateTime={operational.generatedAt ?? undefined}>{generated}</time>
                  {age ? ` (${age})` : ""}.
                </>
              )}
            </span>
          </p>
        </div>
      </div>
    );
  }

  return (
    <div role="status" aria-live="polite" className="fade-in border-b border-separator bg-surface-secondary">
      <div className="mx-auto flex w-full max-w-6xl items-start gap-2.5 px-4 py-2.5 text-sm sm:px-6 lg:px-8">
        <Icon icon="solar:info-circle-linear" className="mt-0.5 size-4 shrink-0 text-muted" aria-hidden="true" />
        <p className="leading-relaxed text-muted">
          <span className="font-semibold text-foreground">Status unavailable.</span>{" "}
          Live market and position figures aren't shown for this deployment.
        </p>
      </div>
    </div>
  );
}
