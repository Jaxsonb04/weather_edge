import { Card } from "@heroui/react/card";
import { Icon } from "@iconify/react/offline";
import { LinkButton } from "../ui/LinkButton";

interface Pillar {
  icon: string;
  title: string;
  points: string[];
}

const PILLARS: Pillar[] = [
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
      "A 12-check readiness gate keeps live orders disabled; the current runtime verdict is published without UI overrides",
    ],
  },
];

/** Systems summary: what is engineered here, one level below the charts. */
export function SystemHighlights() {
  return (
    <>
      <div className="grid gap-5 lg:grid-cols-3">
        {PILLARS.map((p) => (
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
