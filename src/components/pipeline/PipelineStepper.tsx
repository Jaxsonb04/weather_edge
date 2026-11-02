import { Stepper } from "@heroui-pro/react/stepper";
import { Icon } from "@iconify/react/offline";

const STEPS = [
  { title: "Serve forecast", desc: "Method and member count from the runtime", icon: "solar:cloud-linear" },
  { title: "Calibrate", desc: "EMOS turns members into a distribution (μ, σ)", icon: "solar:graph-up-linear" },
  { title: "Price brackets", desc: "Bracket probabilities, then fee-aware edge", icon: "solar:tag-price-linear" },
  { title: "Gate and size", desc: "Edge bound, liquidity, spread, freshness", icon: "solar:shield-check-linear" },
];

export function PipelineStepper() {
  return (
    <div className="overflow-x-auto">
          <Stepper defaultStep={STEPS.length} orientation="horizontal" className="min-w-[600px]">
            {STEPS.map((s, i) => (
              <Stepper.Step key={i}>
                <Stepper.Indicator>
                  <Icon icon={s.icon} className="size-4" aria-hidden="true" />
                </Stepper.Indicator>
                <Stepper.Content>
                  <Stepper.Title className="text-sm font-semibold">{s.title}</Stepper.Title>
                  <Stepper.Description className="text-xs">{s.desc}</Stepper.Description>
                </Stepper.Content>
                <Stepper.Separator />
              </Stepper.Step>
            ))}
          </Stepper>
    </div>
  );
}
