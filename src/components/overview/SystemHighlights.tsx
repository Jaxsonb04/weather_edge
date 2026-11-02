import { Icon } from "@iconify/react/offline";
import { LinkButton } from "../ui/LinkButton";

interface Step {
  icon: string;
  title: string;
  text: string;
}

// Two different timers, two cadences: the paper scan runs every five minutes
// (*:00,05,10,...) and the operational publish every ten (*:02,12,22,...). One
// number for both is always wrong for one of them; a test pins each to its unit.
const STEPS: Step[] = [
  {
    icon: "solar:cloud-storm-linear",
    title: "Forecast",
    text: "A multi-model NWP ensemble, calibrated per station with EMOS and settled on each city's NWS report.",
  },
  {
    icon: "solar:tag-price-linear",
    title: "Price and gate",
    text: "Bracket probabilities become fee- and liquidity-aware edge, gated on its lower confidence bound.",
  },
  {
    icon: "solar:server-square-cloud-linear",
    title: "Run unattended",
    text: "Unattended AWS timers scan every city's markets every 5 minutes and publish the runtime artifacts every 10 minutes",
  },
];

/** The system in three steps, one sentence each, with the routes that go deeper. */
export function SystemHighlights() {
  return (
    <div className="flex flex-col gap-6">
      <ol className="grid gap-3 md:grid-cols-3">
        {STEPS.map((step, index) => (
          <li key={step.title} className="flex min-w-0 flex-col gap-3 rounded-3xl bg-surface p-5 shadow-surface">
            <div className="flex items-center justify-between">
              <span className="grid size-9 place-items-center rounded-full bg-accent-soft text-[color:var(--accent-text)]">
                <Icon icon={step.icon} className="size-5" aria-hidden="true" />
              </span>
              <span className="tnum text-xs text-muted">Step {index + 1}</span>
            </div>
            <p className="text-sm font-semibold text-foreground">{step.title}</p>
            <p className="text-sm leading-relaxed text-muted">{step.text}</p>
          </li>
        ))}
      </ol>
      <div className="flex flex-wrap gap-3">
        <LinkButton href="#/methodology" variant="secondary" external={false}>
          Read the methodology
        </LinkButton>
        <LinkButton href="#/lab" variant="ghost" external={false}>
          See the paper accounts <Icon icon="solar:arrow-right-linear" aria-hidden="true" />
        </LinkButton>
      </div>
    </div>
  );
}
