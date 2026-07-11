import { Skeleton } from "@heroui/react/skeleton";
import { Icon } from "@iconify/react/offline";

export function LoadingState() {
  return (
    <div className="min-h-screen bg-background">
      <div className="mx-auto w-full max-w-6xl px-5 pt-24 sm:px-8">
        <div className="flex items-center gap-2 text-muted" role="status" aria-live="polite">
          <Icon icon="solar:refresh-bold" className="size-4 animate-spin" aria-hidden="true" />
          <span className="text-sm">Loading live forecast…</span>
        </div>
        <div className="mt-8 grid gap-10 lg:grid-cols-[1.08fr_0.92fr]">
          <div className="space-y-4">
            <Skeleton className="h-7 w-40 rounded-full" />
            <Skeleton className="h-16 w-full rounded-2xl" />
            <Skeleton className="h-16 w-4/5 rounded-2xl" />
            <Skeleton className="h-10 w-64 rounded-xl" />
          </div>
          <Skeleton className="h-80 w-full rounded-3xl" />
        </div>
        <div className="mt-12 grid grid-cols-2 gap-3 sm:grid-cols-6">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-24 rounded-2xl" />
          ))}
        </div>
      </div>
    </div>
  );
}

export function ErrorState({ message }: { message: string }) {
  return (
    <div className="grid min-h-screen place-items-center bg-background px-6 text-center" role="alert">
      <div className="flex max-w-md flex-col items-center gap-3 text-muted">
        <span className="grid size-12 place-items-center rounded-2xl bg-danger-soft text-danger">
          <Icon icon="solar:cloud-cross-bold" className="size-6" aria-hidden="true" />
        </span>
        <p className="font-display text-lg font-semibold text-foreground">Couldn't load the forecast</p>
        <p className="text-sm">{message}</p>
      </div>
    </div>
  );
}
