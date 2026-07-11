import { Card } from "@heroui/react/card";
import { Chip } from "@heroui/react/chip";
import { Icon } from "@iconify/react/offline";
import { Stat } from "../ui/Stat";
import type { Diagnostics } from "../../lib/diagnostics";

const f2 = (n: number) => (Math.round(n * 100) / 100).toFixed(2);
const pval = (p: number) => (p < 0.001 ? "< 0.001" : p.toFixed(3));

/** The held-out A/B verdict: is the production LSTM's edge over XGBoost real? */
export function ABSignificance({ diag }: { diag: Diagnostics }) {
  const ab = diag.ab;
  return (
    <Card className="rounded-2xl">
      <Card.Header className="flex items-start justify-between gap-3">
        <div>
          <Card.Title className="text-base">Is the LSTM edge real?</Card.Title>
          <Card.Description className="text-sm text-muted">
            Held-out A/B over {ab.n_days.toLocaleString()} days · LSTM vs XGBoost challenger
          </Card.Description>
        </div>
        <Chip variant="soft" color={ab.significant ? "success" : "warning"}>
          <Chip.Label>
            <span className="flex items-center gap-1">
              <Icon icon={ab.significant ? "solar:check-circle-bold" : "solar:minus-circle-bold"} className="size-3.5" />
              {ab.significant ? "Significant" : "Inconclusive"}
            </span>
          </Chip.Label>
        </Chip>
      </Card.Header>
      <Card.Content className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <Stat label="LSTM MAE" value={`${f2(ab.mae_lstm)}°`} tone="pos" />
        <Stat label="XGBoost MAE" value={`${f2(ab.mae_xgb)}°`} />
        <Stat label="Error lift" value={`${ab.lift_pct > 0 ? "+" : ""}${f2(ab.lift_pct)}%`} tone={ab.lift_pct > 0 ? "pos" : "neg"} />
        <Stat label="Win rate" value={`${Math.round(ab.win_rate * 100)}%`} />
        <Stat label="Diebold–Mariano p" value={pval(ab.p_diebold_mariano)} tone={ab.p_diebold_mariano < 0.05 ? "pos" : "default"} />
        <Stat label="Cohen's d" value={f2(ab.cohens_d)} />
      </Card.Content>
      <Card.Footer>
        <p className="text-xs text-muted">
          95% CI on the MAE gain: <span className="tnum font-medium text-foreground">{f2(ab.ci_low)}° – {f2(ab.ci_high)}°</span>
          {ab.ci_low > 0 && " — the interval stays above zero, so the improvement holds across resamples."}
        </p>
      </Card.Footer>
    </Card>
  );
}
