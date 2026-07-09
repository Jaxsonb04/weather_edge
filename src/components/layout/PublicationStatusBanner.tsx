import { Icon } from "@iconify/react";
import { usePublication } from "../../lib/publication";

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

export function PublicationStatusBanner() {
  const { operational, manifest, error } = usePublication();

  if (operational.state === "fresh") return null;
  if (operational.state === "unknown" && !manifest && !error) return null;

  if (operational.state === "stale") {
    const generated = publicationTime(operational.generatedAt);
    return (
      <div role="alert" className="border-b border-danger/35 bg-danger-soft text-foreground">
        <div className="mx-auto flex w-full max-w-6xl items-start gap-3 px-5 py-3 sm:px-8">
          <Icon icon="solar:danger-triangle-bold" className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden="true" />
          <div className="text-sm leading-relaxed">
            <p className="font-semibold">Publication stale</p>
            <p className="text-muted">
              Current prediction-market and open-position status is suppressed until publication recovers.
              {generated && (
                <>
                  {" "}Last operational publication: <time dateTime={operational.generatedAt ?? undefined}>{generated}</time>.
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
          <span className="font-medium text-foreground">Publication freshness is unavailable.</span>{" "}
          This may be a legacy deployment; current status remains withheld until a publication manifest is available.
        </p>
      </div>
    </div>
  );
}
