import { Card } from "@heroui/react/card";
import { HoverCard } from "@heroui-pro/react/hover-card";
import { Icon } from "@iconify/react/offline";
import { f1, num, predictedHigh, targetLabel, type Target } from "../../lib/data";

export function ForecastInputs({ target }: { target: Target }) {
  const e = target.ensemble ?? {};
  const f = target.forecast ?? {};
  const rows: { k: string; v: string; hint?: string }[] = [
    { k: "Published high", v: f1(predictedHigh(target)) },
    { k: "Current method", v: String(f.method ?? "—").replace(/_/g, " ").replace(/\s+/g, " ").trim() || "—" },
    { k: "NWP members", v: `${f.source_count ?? "—"}` },
  ];
  if (e.member_count != null) {
    rows.push({
      k: "GFS ensemble",
      v: `${e.member_count} members`,
      hint: `μ ${f1(num(e, "station_mean_high_f"))} · σ ${f1(num(e, "station_std_high_f"))} · bias ${f1(num(e, "station_bias_f"))}`,
    });
  }
  if (typeof f.calls_used_today === "number" && typeof f.max_calls_per_day === "number") {
    rows.push({ k: "Google budget", v: `${f.calls_used_today} / ${f.max_calls_per_day} calls` });
  }

  return (
    <Card className="h-full rounded-2xl">
      <Card.Header>
        <Card.Title className="text-base">Forecast inputs</Card.Title>
        <Card.Description className="text-sm text-muted">
          Runtime inputs published for the {targetLabel(target.target_date).toLowerCase()} high
        </Card.Description>
      </Card.Header>
      <Card.Content className="pt-0">
        <dl className="divide-y divide-border/60">
          {rows.map((r) => (
            <div key={r.k} className="flex items-center justify-between gap-4 py-2.5">
              <dt className="text-sm text-muted">{r.k}</dt>
              <dd className="flex items-center gap-1.5 text-right">
                <span className="tnum text-sm font-medium">{r.v}</span>
                {r.hint && (
                  <HoverCard openDelay={120}>
                    <HoverCard.Trigger>
                      <button
                        type="button"
                        aria-label={`${r.k} detail: ${r.hint}`}
                        className="grid place-items-center rounded text-muted transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-[color:var(--focus)]"
                      >
                        <Icon icon="solar:info-circle-bold" className="size-3.5" />
                      </button>
                    </HoverCard.Trigger>
                    <HoverCard.Content className="max-w-[16rem]">
                      <HoverCard.Arrow />
                      <p className="tnum text-xs text-muted">{r.hint}</p>
                    </HoverCard.Content>
                  </HoverCard>
                )}
              </dd>
            </div>
          ))}
        </dl>
      </Card.Content>
    </Card>
  );
}
