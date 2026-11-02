import { Widget } from "@heroui-pro/react/widget";
import { cityForTicker } from "../../lib/data";
import { shortDay } from "../../lib/incidents";
import { money, type StrategyLab, type WinnerLoser } from "../../lib/strategy";

const ACCOUNT: Record<string, string> = { live: "Live", "research-target": "Research" };

function Row({ m }: { m: WinnerLoser }) {
  const pos = m.realized_pnl >= 0;
  const city = cityForTicker(m.ticker ?? "");
  return (
    <li className="flex items-center justify-between gap-3 py-2.5 first:pt-0 last:pb-0">
      <div className="min-w-0">
        <p className="truncate text-sm font-medium text-foreground">
          {m.label} <span className="text-xs font-semibold text-muted">{m.side}</span>
        </p>
        <p className="tnum truncate text-xs text-muted">
          {city?.name ?? "Unknown city"} · {shortDay(m.target_date)}
          {m.risk_profile ? ` · ${ACCOUNT[m.risk_profile] ?? m.risk_profile}` : ""}
        </p>
      </div>
      <span className={`tnum shrink-0 text-sm font-semibold ${pos ? "text-success" : "text-danger"}`}>
        {money(m.realized_pnl)}
      </span>
    </li>
  );
}

/** The week's single best and worst trades, each labelled with its account.
    Individual trades only; nothing here is a sum across accounts. */
export function MoversCard({ s }: { s: StrategyLab }) {
  const winners = (s.daily_summary?.biggest_winners ?? []).slice(0, 4);
  const losers = (s.daily_summary?.biggest_losers ?? []).slice(0, 4);
  if (!winners.length && !losers.length) return null;
  return (
    <Widget className="min-w-0">
      <Widget.Header>
        <div className="flex min-w-0 flex-col gap-0.5">
          <Widget.Title>Biggest movers this week</Widget.Title>
          <Widget.Description>Single trades, labelled by account</Widget.Description>
        </div>
      </Widget.Header>
      <Widget.Content className="grid gap-6 sm:grid-cols-2">
        <div className="min-w-0">
          <h4 id="movers-best-trades" className="mb-2 text-xs font-medium text-muted">Best trades</h4>
          <ul aria-labelledby="movers-best-trades" className="flex flex-col divide-y divide-separator">
            {winners.map((m) => (
              <Row key={`${m.ticker}-${m.target_date}-${m.side}-${m.risk_profile ?? ""}`} m={m} />
            ))}
          </ul>
        </div>
        <div className="min-w-0">
          <h4 id="movers-worst-trades" className="mb-2 text-xs font-medium text-muted">Worst trades</h4>
          <ul aria-labelledby="movers-worst-trades" className="flex flex-col divide-y divide-separator">
            {losers.map((m) => (
              <Row key={`${m.ticker}-${m.target_date}-${m.side}-${m.risk_profile ?? ""}`} m={m} />
            ))}
          </ul>
        </div>
      </Widget.Content>
    </Widget>
  );
}
