import { Card } from "@heroui/react/card";
import { Icon } from "@iconify/react/offline";
import { LinkButton } from "../ui/LinkButton";

interface Pillar {
  icon: string;
  title: string;
  points: string[];
}

interface SystemHighlightsProps {
  /** `strategy_research.real_money_readiness.checks.length`, when a run publishes
      that block. The readiness gate is built dynamically per traded cohort and
      side, so its size is not a constant — and the recurring public artifact
      defers the block entirely (`available: false`, no `checks` array). Absent a
      published count, the copy stays count-free rather than inventing one. */
  readinessCheckCount?: number | null;
}

const readinessPoint = (checkCount: number | null) =>
  `${checkCount != null && checkCount > 0 ? `A ${checkCount}-check` : "A multi-check"} readiness gate` +
  " keeps live orders disabled; the UI renders whatever verdict the published artifact carries, never an override";

const buildPillars = (checkCount: number | null): Pillar[] => [
  {
    icon: "solar:cpu-bolt-bold",
    title: "Forecasting stack",
    points: [
      "An 8-member NWP ensemble with per-station EMOS point forecasts across all 15 cities",
      "San Francisco adds LSTM residual-calibration evidence, marine-layer features, and optional external inputs when fresh",
      "Held-out SFO diagnostics compare the LSTM with XGBoost and persistence; they are research evidence, not runtime health",
    ],
  },
  {
    icon: "solar:graph-new-up-bold",
    title: "Market engine",
    points: [
      "Bin-level probability engine with boundary-aware intraday math and an observed-high lock",
      "Fee- and liquidity-aware edge, gated on the lower confidence bound — not the point estimate",
      "Two economically separate paper cohorts: Live Stability for readiness and Research ROI for bounded policy experiments",
    ],
  },
  {
    icon: "solar:server-square-cloud-bold",
    title: "Production discipline",
    points: [
      "Unattended AWS timers scan every city's markets every 5 minutes and publish the runtime artifacts",
      "SQLite paper journal with rule-based monitor exits (take-profit, stop-loss, model veto)",
      readinessPoint(checkCount),
    ],
  },
];

/** Systems summary: what is engineered here, one level below the charts. */
export function SystemHighlights({ readinessCheckCount = null }: SystemHighlightsProps) {
  const pillars = buildPillars(readinessCheckCount);
  return (
    <>
      <div className="grid gap-5 lg:grid-cols-3">
        {pillars.map((p) => (
          <Card key={p.title} className="h-full rounded-2xl">
            <Card.Header className="flex flex-row items-center gap-2.5">
              <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-accent-soft text-accent ring-1 ring-accent/25">
                <Icon icon={p.icon} className="size-4" aria-hidden="true" />
              </span>
              <Card.Title className="text-base">{p.title}</Card.Title>
            </Card.Header>
            <Card.Content className="pt-0">
              <ul className="space-y-3">
                {p.points.map((pt) => (
                  <li key={pt} className="flex gap-2.5 text-sm leading-relaxed text-muted">
                    <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-accent" />
                    <span>{pt}</span>
                  </li>
                ))}
              </ul>
            </Card.Content>
          </Card>
        ))}
      </div>
      <div className="mt-6 flex flex-wrap gap-3">
        <LinkButton href="#/methodology" variant="primary" external={false}>
          <Icon icon="solar:graph-up-bold" className="size-4" aria-hidden="true" />
          See the model proof
        </LinkButton>
        <LinkButton href="#/lab" variant="outline" external={false}>
          <Icon icon="solar:test-tube-bold" className="size-4" aria-hidden="true" />
          Open the Strategy Lab
        </LinkButton>
      </div>
    </>
  );
}
