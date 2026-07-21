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
    <div className="grid gap-6 sm:grid-cols-2">
      <div>
        <h4 id="movers-best-trades" className="mb-2 font-display text-sm font-semibold text-foreground">
          Best trades
        </h4>
        <ul aria-labelledby="movers-best-trades" className="divide-y divide-border/50">
          {winners.map((m) => (
            <Row key={`${m.ticker}-${m.target_date}-${m.side}`} m={m} />
          ))}
        </ul>
      </div>
      <div>
        <h4 id="movers-worst-trades" className="mb-2 font-display text-sm font-semibold text-foreground">
          Worst trades
        </h4>
        <ul aria-labelledby="movers-worst-trades" className="divide-y divide-border/50">
          {losers.map((m) => (
            <Row key={`${m.ticker}-${m.target_date}-${m.side}`} m={m} />
          ))}
        </ul>
      </div>
    </div>
  );
}
