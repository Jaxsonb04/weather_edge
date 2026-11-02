import { Card } from "@heroui/react/card";
import { Chip } from "@heroui/react/chip";
import { Icon } from "@iconify/react/offline";
import { Stat } from "../ui/Stat";
import type { Diagnostics } from "../../lib/diagnostics";

const f2 = (n: number) => (Math.round(n * 100) / 100).toFixed(2);

/** The held-out A/B verdict: is the production LSTM's edge over XGBoost real? */
export function ABSignificance({ diag }: { diag: Diagnostics }) {
  const ab = diag.ab;
  return (
    <Card className="rounded-3xl">
      <Card.Header className="flex items-start justify-between gap-3">
        <div>
          <Card.Title className="text-base">Is the edge real?</Card.Title>
          <Card.Description className="text-sm text-muted">
            {ab.n_days.toLocaleString()} held-out days · LSTM vs XGBoost
          </Card.Description>
        </div>
        <Chip variant="soft" color={ab.significant ? "success" : "warning"}>
          <Chip.Label>
            <span className="flex items-center gap-1">
              <Icon icon={ab.significant ? "solar:check-circle-linear" : "solar:minus-circle-linear"} className="size-3.5" aria-hidden="true" />
              {ab.significant ? "Significant" : "Inconclusive"}
            </span>
          </Chip.Label>
        </Chip>
      </Card.Header>
      <Card.Content className="grid grid-cols-3 content-start gap-3">
        <Stat label="Error lift" value={`${ab.lift_pct > 0 ? "+" : ""}${f2(ab.lift_pct)}%`} tone={ab.lift_pct > 0 ? "pos" : "neg"} />
        <Stat label="Cohen's d" value={f2(ab.cohens_d)} />
        <Stat label="95% CI, MAE gain" value={`${f2(ab.ci_low)}°–${f2(ab.ci_high)}°`} />
      </Card.Content>
      <Card.Footer>
        <p className="text-xs text-muted">
          {ab.ci_low > 0
            ? "The interval stays above zero across resamples, so the gain holds."
            : "The interval reaches zero, so the gain is not yet established."}
        </p>
      </Card.Footer>
    </Card>
  );
}
