import { Card } from "@heroui/react";
import { Icon } from "@iconify/react";
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
      "LSTM in production, with an XGBoost challenger A/B-tested out-of-sample (Diebold–Mariano, bootstrap CIs)",
      "EMOS-weighted 8-member NWP ensemble in all fifteen cities, plus Google, NWS, and Open-Meteo blend inputs on the SFO flagship",
      "A decade of station-aligned KSFO history behind climatology, bias correction, and bin calibration",
    ],
  },
  {
    icon: "solar:graph-new-up-bold",
    title: "Market engine",
    points: [
      "Bin-level probability engine with boundary-aware intraday math and an observed-high lock",
      "Fee- and liquidity-aware edge, gated on the lower confidence bound — not the point estimate",
      "Two isolated risk profiles: a strict real-money candidate and a loose research collector",
    ],
  },
  {
    icon: "solar:server-square-cloud-bold",
    title: "Production discipline",
    points: [
      "Unattended AWS timers scan every city's markets on a 15-minute cadence and publish these JSON artifacts",
      "SQLite paper journal with rule-based monitor exits (take-profit, stop-loss, model veto)",
      "A six-check go-live readiness gate keeps real money disabled in code until it's earned",
    ],
  },
];

/** Recruiter-facing systems summary: what is actually engineered here, one
    level below the charts. */
export function SystemHighlights() {
  return (
    <>
      <div className="grid gap-5 lg:grid-cols-3">
        {PILLARS.map((p) => (
          <Card key={p.title} className="h-full rounded-2xl ring-1 ring-border/70">
            <Card.Header className="flex flex-row items-center gap-2.5">
              <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-accent-soft text-accent ring-1 ring-accent/25">
                <Icon icon={p.icon} className="size-4" aria-hidden="true" />
              </span>
              <Card.Title className="text-base">{p.title}</Card.Title>
            </Card.Header>
            <Card.Content className="pt-0">
              <ul className="space-y-2.5">
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
