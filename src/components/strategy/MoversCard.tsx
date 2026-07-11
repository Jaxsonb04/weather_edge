import { Card } from "@heroui/react/card";
import { Icon } from "@iconify/react/offline";
import { money, type StrategyLab, type WinnerLoser } from "../../lib/strategy";

function Row({ m }: { m: WinnerLoser }) {
  const pos = m.realized_pnl >= 0;
  return (
    <li className="flex items-center justify-between gap-3 py-2">
      <div className="min-w-0">
        <p className="truncate text-sm font-medium">{m.label}</p>
        <p className="font-mono text-[11px] text-muted">
          {m.side} · {m.target_date.slice(5)}
        </p>
      </div>
      <span className={`tnum shrink-0 text-sm font-semibold ${pos ? "text-success" : "text-danger"}`}>
        {money(m.realized_pnl)}
      </span>
    </li>
  );
}

export function MoversCard({ s }: { s: StrategyLab }) {
  const winners = (s.daily_summary.biggest_winners ?? []).slice(0, 4);
  const losers = (s.daily_summary.biggest_losers ?? []).slice(0, 4);
  return (
    <div className="grid gap-5 sm:grid-cols-2">
      <Card className="rounded-2xl">
        <Card.Header className="flex flex-row items-center gap-2">
          <Icon icon="solar:arrow-up-bold" className="size-4 text-success" aria-hidden="true" />
          <Card.Title className="text-base">Best trades</Card.Title>
        </Card.Header>
        <Card.Content className="pt-0">
          <ul className="divide-y divide-border/50">{winners.map((m) => <Row key={`${m.ticker}-${m.target_date}-${m.side}`} m={m} />)}</ul>
        </Card.Content>
      </Card>
      <Card className="rounded-2xl">
        <Card.Header className="flex flex-row items-center gap-2">
          <Icon icon="solar:arrow-down-bold" className="size-4 text-danger" aria-hidden="true" />
          <Card.Title className="text-base">Worst trades</Card.Title>
        </Card.Header>
        <Card.Content className="pt-0">
          <ul className="divide-y divide-border/50">{losers.map((m) => <Row key={`${m.ticker}-${m.target_date}-${m.side}`} m={m} />)}</ul>
        </Card.Content>
      </Card>
    </div>
  );
}
