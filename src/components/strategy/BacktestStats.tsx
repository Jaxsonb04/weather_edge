import { Card } from "@heroui/react";
import { pct } from "../../lib/data";
import { money, type StrategyLab } from "../../lib/strategy";
import { Stat } from "../ui/Stat";

const fmt = (n: number | undefined) => (n == null ? "—" : n.toLocaleString());

/** The dedup funnel: hundreds of thousands of 15-min scans → unique → approved,
    plus how the approved slice actually scored. */
export function BacktestStats({ s }: { s: StrategyLab }) {
  const c = s.backtest_summary?.counts ?? {};
  const m = s.backtest_summary?.metrics;
  const tiles = [
    { label: "Raw scans", value: fmt(c.raw_signals) },
    { label: "Pre-resolution", value: fmt(c.pre_resolution_signals) },
    { label: "Deduped", value: fmt(c.deduped_signals) },
    { label: "Approved", value: fmt(c.approved_signals) },
    { label: "Settled", value: fmt(c.settled_signals) },
  ];
  return (
    <Card className="rounded-2xl">
      <Card.Header>
        <Card.Title className="text-base">Backtest coverage</Card.Title>
        <Card.Description className="text-sm text-muted">
          {s.backtest_summary?.dedupe_explanation ??
            "Every 15-minute scan is counted once per target/market/side, using the entry snapshot."}
        </Card.Description>
      </Card.Header>
      <Card.Content className="space-y-4">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          {tiles.map((t) => (
            <Stat key={t.label} label={t.label} value={t.value} />
          ))}
        </div>
        {s.backtest_summary?.metrics_available && m && (
          <div>
            <p className="mb-2 text-[11px] uppercase tracking-wide text-muted">How the approved slice scored (pre-resolution entries)</p>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
              <Stat label="Approval rate" value={pct(m.approval_rate, 1)} />
              <Stat
                label="Approved hit rate"
                value={pct(m.approved_hit_rate, 1)}
                tone={(m.approved_hit_rate ?? 0) >= 0.5 ? "pos" : "default"}
              />
              <Stat
                label="Approved P&L"
                value={money(m.approved_paper_pnl)}
                tone={(m.approved_paper_pnl ?? 0) > 0 ? "pos" : (m.approved_paper_pnl ?? 0) < 0 ? "neg" : "default"}
              />
              <Stat
                label="Approved ROI"
                value={m.approved_roi == null ? "—" : pct(m.approved_roi, 1)}
                tone={(m.approved_roi ?? 0) > 0 ? "pos" : (m.approved_roi ?? 0) < 0 ? "neg" : "default"}
              />
              <Stat label="Brier (all deduped)" value={m.brier_score == null ? "—" : m.brier_score.toFixed(3)} />
            </div>
          </div>
        )}
      </Card.Content>
    </Card>
  );
}
