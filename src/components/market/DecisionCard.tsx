import { Card } from "@heroui/react/card";
import { Chip } from "@heroui/react/chip";
import { Separator } from "@heroui/react/separator";
import { Icon } from "@iconify/react/offline";
import { f1, num, pct, qualityColor, signedPct, type Target } from "../../lib/data";
import { Stat } from "../ui/Stat";

export function DecisionCard({ target, approvedCount }: { target: Target; approvedCount: number }) {
  const d = target.best_decision;
  const noTrade = d.decision !== "TRADE";
  const spread = num(target.forecast, "source_spread_f");
  const topReason = d.reasons?.[0]?.replace(/\.$/, "");
  return (
    <Card className="h-full rounded-2xl">
      <Card.Header className="flex items-start justify-between gap-3">
        <div>
          <Card.Title className="text-base">Best decision</Card.Title>
          <Card.Description className="text-sm text-muted">{d.label} · {d.ticker}</Card.Description>
        </div>
        <Chip variant="soft" color={noTrade ? "warning" : "success"}>
          <Chip.Label>{d.decision.replace(/_/g, " ")}</Chip.Label>
        </Chip>
      </Card.Header>
      <Card.Content className="pt-0">
        <div className="grid grid-cols-3 gap-3">
          <Stat label="Model" value={pct(d.model_probability)} />
          <Stat label="Market" value={pct(d.market_probability)} />
          <Stat label="Edge" value={signedPct(d.edge, 1)} tone={d.edge > 0.001 ? "pos" : d.edge < -0.001 ? "neg" : "default"} />
        </div>

        <div className="mt-4 flex items-center gap-2 text-xs text-muted">
          <span className="shrink-0">Quality</span>
          <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-foreground/10">
            <div
              className="h-full rounded-full transition-[width] duration-700 ease-out motion-reduce:transition-none"
              style={{ width: `${Math.min(100, Math.max(0, d.trade_quality_score))}%`, background: qualityColor(d.trade_quality_score) }}
            />
          </div>
          <span className="tnum font-medium text-foreground">{Math.round(d.trade_quality_score)}</span>
        </div>

        {d.reasons?.length > 0 && (
          <ul className="mt-4 space-y-1.5">
            {d.reasons.slice(0, 3).map((r, i) => (
              <li key={i} className="flex gap-2 text-xs text-muted">
                <Icon icon="solar:shield-warning-bold" className="mt-0.5 size-3.5 shrink-0 text-warning" />
                <span>{r}</span>
              </li>
            ))}
          </ul>
        )}

        <Separator className="my-4" />
        <p className="text-xs text-muted">
          {approvedCount > 0 ? (
            <>
              <span className="font-medium text-success">{approvedCount}</span> signal{approvedCount === 1 ? "" : "s"} cleared every gate and would be placed.
            </>
          ) : (
            <>
              <span className="font-medium text-foreground">0</span> approved signals —{" "}
              {topReason ?? `today's sources disagree by ${f1(spread)}`}.
            </>
          )}
        </p>
      </Card.Content>
    </Card>
  );
}
