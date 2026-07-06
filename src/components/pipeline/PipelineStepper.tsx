import { Card } from "@heroui/react";
import { Stepper } from "@heroui-pro/react";
import { Icon } from "@iconify/react";
import { Reveal } from "../ui/Reveal";

const STEPS = [
  { title: "Blend forecast", desc: "Google · NWS · Open-Meteo · 10-yr history → station-aligned high", icon: "solar:cloud-bold" },
  { title: "Post-process", desc: "NWP/EMOS calibration → predictive distribution (μ, σ)", icon: "solar:graph-up-bold" },
  { title: "Price the bins", desc: "Distribution → market bracket probabilities → fee-aware edge", icon: "solar:tag-price-bold" },
  { title: "Gate & size", desc: "Edge LCB · liquidity · spread · freshness — before any paper order", icon: "solar:shield-check-bold" },
];

export function PipelineStepper() {
  return (
    <Reveal>
      <Card variant="secondary" className="overflow-x-auto rounded-2xl">
        <Card.Content className="p-5 sm:p-6">
          <Stepper defaultStep={STEPS.length} orientation="horizontal" className="min-w-[640px]">
            {STEPS.map((s, i) => (
              <Stepper.Step key={i}>
                <Stepper.Indicator>
                  <Icon icon={s.icon} className="size-4" />
                </Stepper.Indicator>
                <Stepper.Content>
                  <Stepper.Title className="text-sm font-semibold">{s.title}</Stepper.Title>
                  <Stepper.Description className="text-xs">{s.desc}</Stepper.Description>
                </Stepper.Content>
                <Stepper.Separator />
              </Stepper.Step>
            ))}
          </Stepper>
        </Card.Content>
      </Card>
    </Reveal>
  );
}
