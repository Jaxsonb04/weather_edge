import { Chip } from "@heroui/react/chip";
import { Icon } from "@iconify/react/offline";
import { pct } from "../../lib/data";
import { deferralReason, money, type StrategyLab } from "../../lib/strategy";
import { Stat } from "../ui/Stat";

const fmt = (n: number | undefined) => (n == null ? "—" : n.toLocaleString());

/** The dedup funnel: repeated scheduled scans → unique → approved,
    plus how the approved slice actually scored. */
export function BacktestStats({ s }: { s: StrategyLab }) {
  const backtest = s.backtest_summary;
  if (!backtest) return null;
  // The bounded public refresh publishes this section as an all-zero stub, so
  // the funnel tiles and the authored dedupe prose would describe counts that
  // were never computed. The published reason takes their place.
  if (backtest.available === false) {
    return (
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <h4 className="font-display text-sm font-semibold text-foreground">Backtest coverage</h4>
          <Chip size="sm" variant="soft" color="warning">
            <Chip.Label>Deferred</Chip.Label>
          </Chip>
        </div>
        <p role="status" className="flex gap-2 text-sm leading-relaxed text-muted">
          <Icon icon="solar:hourglass-line-bold" className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
          <span>{deferralReason(backtest)}</span>
        </p>
      </div>
    );
  }
  const c = backtest.counts ?? {};
  const m = backtest.metrics;
  const tiles = [
    { label: "Raw scans", value: fmt(c.raw_signals) },
    { label: "Pre-resolution", value: fmt(c.pre_resolution_signals) },
    { label: "Deduped", value: fmt(c.deduped_signals) },
    { label: "Approved", value: fmt(c.approved_signals) },
    { label: "Settled", value: fmt(c.settled_signals) },
    { label: "Scored markets", value: fmt(c.scored_observations) },
  ];
  const dedupeExplanation = (
    backtest.dedupe_explanation ??
    "Repeated scheduled scans are counted once per target, market, and side using the entry snapshot."
  ).replace(/repeated 15[ -]minute AWS scans/gi, "Repeated scheduled AWS scans");
  return (
    <div className="space-y-4">
      <div>
        <h4 className="mb-2 font-display text-sm font-semibold text-foreground">Backtest coverage</h4>
        <p className="text-sm text-muted">
          {dedupeExplanation}
        </p>
      </div>
      <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3 lg:grid-cols-6">
        {tiles.map((t) => (
          <Stat key={t.label} label={t.label} value={t.value} />
        ))}
      </div>
      {backtest.metrics_available && m && (
        <div>
          <p className="mb-2 text-xs font-medium text-muted">How the approved slice scored (pre-resolution entries)</p>
          <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-5">
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
            <Stat label="Brier (scored markets)" value={m.brier_score == null ? "—" : m.brier_score.toFixed(3)} />
          </div>
        </div>
      )}
    </div>
  );
}
