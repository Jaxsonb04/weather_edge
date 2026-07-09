import { useMemo, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { Card, Chip, Separator } from "@heroui/react";
import { Segment, TrendChip } from "@heroui-pro/react";
import { Icon } from "@iconify/react";
import { f1, predictedHigh, selectCurrentTargets, targetLabel, type Target } from "../../lib/data";
import { usePublication } from "../../lib/publication";
import { SourceBlend } from "./SourceBlend";

export function ForecastDial({ targets }: { targets: Target[] }) {
  const reduce = useReducedMotion();
  const { operational } = usePublication();
  const currentStateAvailable = operational.state === "fresh";
  const [idx, setIdx] = useState(0);
  const displayTargets = useMemo(() => selectCurrentTargets(targets), [targets]);
  const target = displayTargets[idx] ?? displayTargets.at(0);

  if (!target) {
    return (
      <Card className="rounded-3xl ring-1 ring-danger/30">
        <Card.Content className="flex items-start gap-3 p-6" role="alert">
          <Icon icon="solar:danger-triangle-bold" className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden="true" />
          <p className="text-sm text-muted">No settlement-day or upcoming prediction-market target is published.</p>
        </Card.Content>
      </Card>
    );
  }
  const high = predictedHigh(target);
  const mc = target.market_consensus;
  const intraday = target.intraday;
  const delta = mc?.model_minus_market_f ?? null;

  return (
    <Card className="overflow-hidden rounded-3xl ring-1 ring-border/70">
      <Card.Content className="p-6">
        <div className="mb-4 flex items-center justify-between gap-3">
          <span className="font-mono text-[11px] font-medium uppercase tracking-[0.16em] text-muted">
            SFO daily high · forecast
          </span>
          <Chip size="sm" variant="soft" color={currentStateAvailable && target.market_available ? "success" : "default"}>
            <Chip.Label>
              {currentStateAvailable
                ? target.market_available
                  ? "Market live"
                  : "No market"
                : "Current status unavailable"}
            </Chip.Label>
          </Chip>
        </div>

        {displayTargets.length > 1 && (
          <Segment
            aria-label="Forecast day"
            size="sm"
            selectedKey={String(idx)}
            onSelectionChange={(k) => setIdx(Number(k))}
            className="mb-5"
          >
            {displayTargets.map((t, i) => (
              <Segment.Item key={i} id={String(i)}>
                {targetLabel(t.target_date)}
              </Segment.Item>
            ))}
          </Segment>
        )}

        <div className="flex items-end justify-between gap-4">
          <AnimatePresence mode="popLayout" initial={false}>
            <motion.div
              key={idx}
              initial={reduce ? false : { opacity: 0, y: 12, filter: "blur(4px)" }}
              animate={{ opacity: 1, y: 0, filter: "blur(0px)" }}
              exit={reduce ? undefined : { opacity: 0, y: -12, filter: "blur(4px)" }}
              transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
              className="leading-none"
            >
              <p className="temp-text font-display text-[5.5rem] font-bold leading-[0.85] tnum">
                {high == null ? "—" : Math.round(high)}
                <span className="align-top font-sans text-2xl font-semibold text-muted">°F</span>
              </p>
            </motion.div>
          </AnimatePresence>

          {delta != null && mc && (
            <div className="mb-1 text-right">
              <TrendChip trend={delta > 0.2 ? "up" : delta < -0.2 ? "down" : "neutral"} size="sm">
                {delta > 0 ? "+" : ""}{f1(delta)}
                <TrendChip.Suffix>vs market</TrendChip.Suffix>
              </TrendChip>
              <p className="mt-1.5 text-xs text-muted">
                market implies <span className="tnum font-medium text-foreground">{f1(mc.implied_high_f)}</span>
              </p>
            </div>
          )}
        </div>

        {currentStateAvailable && intraday && !intraday.is_complete && (
          <div className="mt-5 rounded-2xl bg-surface-secondary px-4 py-3 ring-1 ring-border/50">
            <div className="flex items-center justify-between text-xs">
              <span className="flex items-center gap-2 text-muted">
                <span className="relative inline-flex size-2 text-success">
                  <span className="pulse-dot absolute inset-0 rounded-full" />
                  <span className="relative size-2 rounded-full bg-success" />
                </span>
                Live · {intraday.observation_count} obs
              </span>
              <span className="tnum text-muted">now {f1(intraday.latest_temp_f)}</span>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="text-xs text-muted">Observed high so far</span>
              <span className="tnum font-display text-xl font-semibold">{f1(intraday.observed_high_f)}</span>
              <Icon icon="solar:arrow-right-up-linear" className="size-3.5 text-success" />
            </div>
          </div>
        )}

        <Separator className="my-5" />
        <p className="mb-3 font-mono text-[11px] font-medium uppercase tracking-[0.16em] text-muted">
          Source blend · {targetLabel(target.target_date).toLowerCase()}
        </p>
        <SourceBlend target={target} />
      </Card.Content>
    </Card>
  );
}
