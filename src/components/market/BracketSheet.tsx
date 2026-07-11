import { Button } from "@heroui/react/button";
import { Chip } from "@heroui/react/chip";
import { Separator } from "@heroui/react/separator";
import { Sheet } from "@heroui-pro/react/sheet";
import { Icon } from "@iconify/react/offline";
import { pct, type Decision } from "../../lib/data";
import { Stat } from "../ui/Stat";

interface BracketSheetProps {
  decision: Decision | null;
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
}

/** Right-side detail drawer for one bracket — rendered into a portal by Sheet. */
export function BracketSheet({ decision, isOpen, onOpenChange }: BracketSheetProps) {
  const [copied, setCopied] = useState(false);
  return (
    <Sheet isOpen={isOpen} onOpenChange={onOpenChange} placement="right">
      <Sheet.Backdrop variant="blur">
        <Sheet.Content className="ml-auto h-full w-full max-w-[26rem]">
          <Sheet.Dialog>
            <Sheet.CloseTrigger />
            {decision && (
              <>
                <Sheet.Header>
                  <div className="flex items-center gap-2">
                    <Chip size="sm" variant="soft" color={decision.decision === "TRADE" ? "success" : "warning"}>
                      <Chip.Label>{decision.decision.replace(/_/g, " ")}</Chip.Label>
                    </Chip>
                    <Chip size="sm" variant="soft" className="font-mono">
                      <Chip.Label>{decision.side}</Chip.Label>
                    </Chip>
                  </div>
                  <Sheet.Heading className="mt-2 font-display text-xl">{decision.label}</Sheet.Heading>
                  <p className="font-mono text-xs text-muted">{decision.ticker}</p>
                </Sheet.Header>
                <Sheet.Body className="space-y-5">
                  <div className="grid grid-cols-3 gap-3">
                    <Stat label="Model" value={pct(decision.model_probability)} />
                    <Stat label="Market" value={pct(decision.market_probability)} />
                    <Stat
                      label="Edge"
                      value={pct(decision.edge, 1)}
                      tone={decision.edge > 0.001 ? "pos" : decision.edge < -0.001 ? "neg" : "default"}
                    />
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <Stat label="Edge LCB" value={pct(decision.edge_lcb, 1)} />
                    <Stat label="Quality" value={String(Math.round(decision.trade_quality_score))} />
                  </div>

                  <div>
                    <p className="mb-2 font-mono text-[11px] font-semibold uppercase tracking-[0.16em] text-muted">
                      Gate trace
                    </p>
                    <ul className="space-y-2">
                      {(decision.reasons ?? []).map((r, i) => (
                        <li key={i} className="flex gap-2 rounded-lg bg-surface-secondary p-2.5 text-xs text-muted ring-1 ring-border/40">
                          <Icon icon="solar:shield-warning-bold" className="mt-0.5 size-3.5 shrink-0 text-warning" />
                          <span>{r}</span>
                        </li>
                      ))}
                      {!decision.reasons?.length && (
                        <li className="flex gap-2 text-xs text-success">
                          <Icon icon="solar:shield-check-bold" className="size-4" /> All gates passed.
                        </li>
                      )}
                    </ul>
                  </div>
                </Sheet.Body>
                <Sheet.Footer>
                  <Separator className="mb-3" />
                  <Button
                    variant="outline"
                    fullWidth
                    onPress={() => {
                      navigator.clipboard?.writeText(decision.ticker);
                      setCopied(true);
                      window.setTimeout(() => setCopied(false), 1_500);
                    }}
                  >
                    <Icon icon="solar:copy-bold" className="size-4" />
                    <span aria-live="polite">{copied ? "Copied ticker" : "Copy ticker"}</span>
                  </Button>
                </Sheet.Footer>
              </>
            )}
          </Sheet.Dialog>
        </Sheet.Content>
      </Sheet.Backdrop>
    </Sheet>
  );
}
import { useState } from "react";
