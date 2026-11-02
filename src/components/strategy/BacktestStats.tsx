import { Chip } from "@heroui/react/chip";
import { Widget } from "@heroui-pro/react/widget";
import { Icon } from "@iconify/react/offline";
import { pct } from "../../lib/data";
import { decisionCountProvenance, deferralReason, type StrategyLab } from "../../lib/strategy";
import { InfoTip } from "../ui/InfoTip";
import { Stat } from "../ui/Stat";

const fmt = (n: number | undefined) => (n == null ? "—" : n.toLocaleString());

/** From every scheduled scan down to the scored slice, plus how the approved
    entries scored. Counts come from the deploy-time analysis. */
export function BacktestStats({ s }: { s: StrategyLab }) {
  const backtest = s.backtest_summary;
  if (!backtest) return null;
  const provenance = decisionCountProvenance(s);
  // The bounded public refresh can publish this section as an all-zero stub;
  // its published reason takes the place of counts that were never computed.
  if (backtest.available === false) {
    return (
      <Widget className="min-w-0">
        <Widget.Header>
          <div className="flex items-center gap-2">
            <Widget.Title>Backtest coverage</Widget.Title>
            <Chip size="sm" variant="soft" color="warning">
              <Chip.Label>Deferred</Chip.Label>
            </Chip>
          </div>
        </Widget.Header>
        <Widget.Content>
          <p role="status" className="flex gap-2 text-sm leading-relaxed text-muted">
            <Icon icon="solar:hourglass-line-linear" className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
            <span>{deferralReason(backtest)}</span>
          </p>
        </Widget.Content>
      </Widget>
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
    <Widget className="min-w-0">
      <Widget.Header>
        <div className="min-w-0">
          <div className="flex items-center gap-1">
            <Widget.Title>Backtest coverage</Widget.Title>
            <InfoTip label="how scans are deduplicated" title="Deduplication">
              {dedupeExplanation} A dollar total over approved snapshots is not shown: it counts repeated scans of the
              same market and is not money any account earned.
            </InfoTip>
          </div>
          <Widget.Description>
            {provenance.cached ? `Scan funnel as of ${provenance.asOf}` : "Scan funnel for this window"}
          </Widget.Description>
        </div>
      </Widget.Header>
      <Widget.Content className="flex flex-col gap-5">
        <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3 lg:grid-cols-6">
          {tiles.map((t) => (
            <Stat key={t.label} label={t.label} value={t.value} />
          ))}
        </div>
        {backtest.metrics_available && m && (
          <div className="border-t border-separator pt-4">
            <p className="mb-3 text-xs text-muted">How approved pre-resolution entries scored</p>
            <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3">
              <Stat label="Approval rate" value={pct(m.approval_rate, 1)} />
              <Stat label="Approved hit rate" value={pct(m.approved_hit_rate, 1)} />
              <Stat label="Brier (scored markets)" value={m.brier_score == null ? "—" : m.brier_score.toFixed(3)} />
            </div>
          </div>
        )}
      </Widget.Content>
    </Widget>
  );
}
