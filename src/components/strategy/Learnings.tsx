import { Card } from "@heroui/react";
import { Icon } from "@iconify/react";
import type { StrategyLab } from "../../lib/strategy";

export function Learnings({ s }: { s: StrategyLab }) {
  const learnings = s.daily_summary?.learnings ?? [];
  const recommended_changes = s.daily_summary?.recommended_changes ?? [];
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Card className="h-full rounded-2xl">
        <Card.Header className="flex flex-row items-center gap-2">
          <Icon icon="solar:lightbulb-bolt-bold" className="size-4 text-accent" />
          <Card.Title className="text-base">What the window taught us</Card.Title>
        </Card.Header>
        <Card.Content className="pt-0">
          <ul className="space-y-2.5">
            {learnings.map((l) => (
              <li key={l} className="flex gap-2.5 text-sm text-muted">
                <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-accent" />
                <span>{l}</span>
              </li>
            ))}
          </ul>
        </Card.Content>
      </Card>

      <Card className="h-full rounded-2xl">
        <Card.Header className="flex flex-row items-center gap-2">
          <Icon icon="solar:tuning-square-2-bold" className="size-4 text-warning" />
          <Card.Title className="text-base">Recommended changes</Card.Title>
        </Card.Header>
        <Card.Content className="pt-0">
          <ul className="space-y-2.5">
            {recommended_changes.map((r) => (
              <li key={r} className="flex gap-2.5 rounded-lg bg-surface-secondary p-3 text-sm text-muted ring-1 ring-border/40">
                <Icon icon="solar:arrow-right-up-linear" className="mt-0.5 size-4 shrink-0 text-warning" />
                <span>{r}</span>
              </li>
            ))}
          </ul>
        </Card.Content>
      </Card>
    </div>
  );
}
