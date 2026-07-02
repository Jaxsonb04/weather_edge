import { Card, Chip } from "@heroui/react";
import { Icon } from "@iconify/react";
import type { ReadinessCheck, StrategyLab } from "../../lib/strategy";
import { Stat } from "../ui/Stat";

function CheckRow({ c }: { c: ReadinessCheck }) {
  const progress = Math.max(0, Math.min(1, c.progress ?? 0));
  return (
    <li className="flex items-start gap-3 py-2.5">
      <Icon
        icon={c.passed ? "solar:check-circle-bold" : "solar:close-circle-bold"}
        className={`mt-0.5 size-4.5 shrink-0 ${c.passed ? "text-success" : "text-danger/70"}`}
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-3">
          <p className="text-sm font-medium text-foreground">{c.label}</p>
          <p className="shrink-0 font-mono text-[11px] text-muted">{c.passed ? "PASS" : "FAIL"}</p>
        </div>
        <p className="mt-0.5 text-xs text-muted">{c.detail}</p>
        <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-foreground/10" aria-hidden="true">
          <div
            className={`h-full rounded-full ${c.passed ? "bg-success" : "bg-danger/60"}`}
            style={{ width: `${Math.max(progress * 100, c.passed ? 100 : 2)}%` }}
          />
        </div>
      </div>
    </li>
  );
}

/** The six go-live checks: the engine's own answer to "would you trade real
    money with this?" — enforced in code, published unedited. */
export function ReadinessPanel({ s }: { s: StrategyLab }) {
  const r = s.real_money_readiness;
  if (!r?.available) return null;
  const checks = r.checks ?? [];
  const passed = r.checks_passed ?? checks.filter((c) => c.passed).length;
  const total = r.checks_total ?? checks.length;
  const ready = r.ready === true;
  const policy = r.live_policy;

  return (
    <div className="grid gap-5 lg:grid-cols-[0.9fr_1.1fr]">
      <Card className="h-full rounded-2xl ring-1 ring-border/70">
        <Card.Header>
          <Card.Title className="text-base">Verdict</Card.Title>
          <Card.Description className="text-sm text-muted">Recomputed on every AWS refresh</Card.Description>
        </Card.Header>
        <Card.Content className="space-y-4 pt-0">
          <div className="flex items-center gap-3">
            <span className={`font-display text-3xl font-bold tracking-tight ${ready ? "text-success" : "text-danger"}`}>
              {r.verdict ?? (ready ? "READY" : "NOT READY")}
            </span>
            <Chip size="sm" variant="soft" color={ready ? "success" : "danger"}>
              <Chip.Label>
                {passed}/{total} checks
              </Chip.Label>
            </Chip>
          </div>
          {r.summary && <p className="text-sm leading-relaxed text-muted">{r.summary}</p>}

          {policy && (
            <div>
              <p className="mb-2 text-[11px] uppercase tracking-wide text-muted">Standing pilot policy (if it ever goes live)</p>
              <div className="grid grid-cols-2 gap-3">
                <Stat label="Live orders" value={policy.enabled ? "Enabled" : "Disabled"} tone={policy.enabled ? "pos" : "default"} />
                <Stat label="Dry run" value={policy.dry_run ? "On" : "Off"} />
                <Stat label="Per-trade risk" value={policy.per_trade_risk != null ? `$${policy.per_trade_risk}` : "—"} />
                <Stat label="Daily loss cap" value={policy.daily_loss != null ? `$${policy.daily_loss}` : "—"} />
              </div>
              {r.pilot_loss_remaining != null && policy.pilot_max_loss != null && (
                <p className="mt-2 text-xs text-muted">
                  Pilot kill-switch: hard stop after ${policy.pilot_max_loss} of losses (${r.pilot_loss_remaining} remaining).
                </p>
              )}
            </div>
          )}
        </Card.Content>
      </Card>

      <Card className="h-full rounded-2xl ring-1 ring-border/70">
        <Card.Header>
          <Card.Title className="text-base">Go-live checklist</Card.Title>
          <Card.Description className="text-sm text-muted">
            All {total} must pass before a single real-money order is possible
          </Card.Description>
        </Card.Header>
        <Card.Content className="pt-0">
          <ul className="divide-y divide-border/50">
            {checks.map((c) => (
              <CheckRow key={c.name} c={c} />
            ))}
          </ul>
        </Card.Content>
      </Card>
    </div>
  );
}
