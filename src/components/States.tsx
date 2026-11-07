import { Skeleton } from "@heroui/react/skeleton";
import { Icon } from "@iconify/react/offline";

/** First-load placeholder in the shape of the Overview: hero text, forecast
    card, then the KPI row. One shimmer sweep across the whole layout. */
export function LoadingState() {
  return (
    <div className="min-h-screen bg-background">
      <div className="mx-auto w-full max-w-6xl px-4 pt-10 sm:px-6 sm:pt-14 lg:px-8">
        <p className="sr-only" role="status" aria-live="polite">Loading latest published forecast…</p>
        <div className="skeleton--shimmer relative grid gap-10 overflow-hidden lg:grid-cols-[1.05fr_0.95fr]">
          <div className="flex flex-col gap-4 pt-4">
            <Skeleton animationType="none" className="h-6 w-44 rounded-full" />
            <Skeleton animationType="none" className="h-12 w-full rounded-2xl" />
            <Skeleton animationType="none" className="h-12 w-3/4 rounded-2xl" />
            <Skeleton animationType="none" className="h-5 w-2/3 rounded-full" />
            <div className="mt-2 flex gap-3">
              <Skeleton animationType="none" className="h-10 w-36 rounded-full" />
              <Skeleton animationType="none" className="h-10 w-32 rounded-full" />
            </div>
          </div>
          <Skeleton animationType="none" className="h-80 w-full rounded-3xl" />
        </div>
        <div className="skeleton--shimmer relative mt-12 grid grid-cols-2 gap-3 overflow-hidden lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} animationType="none" className="h-24 rounded-2xl" />
          ))}
        </div>
      </div>
    </div>
  );
}

export function ErrorState({ message, title = "Couldn't load the forecast" }: { message: string; title?: string }) {
  return (
    <div className="grid min-h-[70vh] place-items-center bg-background px-6 text-center" role="alert">
      <div className="flex max-w-md flex-col items-center gap-3">
        <span className="grid size-12 place-items-center rounded-2xl bg-danger-soft text-danger">
          <Icon icon="solar:cloud-cross-linear" className="size-6" aria-hidden="true" />
        </span>
        <p className="text-lg font-semibold text-foreground">{title}</p>
        <p className="text-sm text-muted">{message}</p>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="mt-1 text-sm font-medium text-[color:var(--accent-text)] underline-offset-4 hover:underline focus-visible:rounded focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--focus)]"
        >
          Reload
        </button>
      </div>
    </div>
  );
}
