import { Card } from "@heroui/react/card";
import { Chip } from "@heroui/react/chip";
import { Icon } from "@iconify/react/offline";
import {
  archivedProfiles,
  money,
  type AccountSnapshot,
  type StrategyLab,
} from "../../lib/strategy";
import { Stat } from "../ui/Stat";

type ArchivedAccount = AccountSnapshot & {
  key?: string;
  label?: string;
  profile_key?: string;
  best_day_pnl?: number | null;
  days_at_or_above_8?: number;
  wins?: number;
  losses?: number;
};

function lifecycleText(open: number, pending: number) {
  return [
    open > 0
      ? `${open} open position${open === 1 ? "" : "s"}`
      : null,
    pending > 0
      ? `${pending} pending limit order${pending === 1 ? "" : "s"}`
      : null,
  ]
    .filter(Boolean)
    .join(" and ");
}

export function ArchivedPerformance({ s }: { s: StrategyLab }) {
  const profiles = archivedProfiles(s);
  const accounts = (s.accounting?.archived_accounts ?? []) as ArchivedAccount[];
  if (!profiles.length && !accounts.length) return null;

  return (
    <div className="space-y-6">
      <div className="flex items-start gap-2 rounded-xl border border-border/60 bg-surface-secondary/60 px-4 py-3 text-xs leading-relaxed text-muted">
        <Icon
          icon="solar:archive-check-bold"
          className="mt-0.5 size-4 shrink-0 text-accent"
          aria-hidden="true"
        />
        <p>
          Read-only achieved performance. Strategy attribution and economic
          account balances are shown separately, so an old shared balance is
          never presented as one profile&apos;s money. Retained paper positions
          continue through normal monitoring and settlement.
        </p>
      </div>

      {profiles.length > 0 && (
        <section aria-labelledby="archived-strategy-attribution">
          <div className="mb-3">
            <h3
              id="archived-strategy-attribution"
              className="text-sm font-semibold text-foreground"
            >
              Strategy attribution
            </h3>
            <p className="mt-1 text-xs leading-relaxed text-muted">
              Results are grouped by the strategy that made each decision; no
              account balance is inferred from these rows.
            </p>
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            {profiles.map((profile) => {
              const summary = profile.paper_trading?.summary;
              const pnl = summary?.realized_pnl ?? 0;
              const open = summary?.open_positions ?? 0;
              const pending = summary?.pending_limit_orders ?? 0;
              const settling = open > 0 || pending > 0;
              return (
                <Card
                  key={profile.risk_profile}
                  className="rounded-2xl border border-border/60 bg-surface/80"
                >
                  <Card.Header className="flex flex-row items-start justify-between gap-3">
                    <div className="flex min-w-0 items-start gap-2.5">
                      <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-surface-secondary text-accent ring-1 ring-border/60">
                        <Icon
                          icon={settling ? "solar:clock-circle-bold" : "solar:cup-star-bold"}
                          className="size-4.5"
                          aria-hidden="true"
                        />
                      </span>
                      <div className="min-w-0">
                        <Card.Title className="truncate text-sm">
                          {profile.label}
                        </Card.Title>
                        <p className="mt-0.5 text-xs text-muted">
                          New entries disabled · attribution only
                        </p>
                      </div>
                    </div>
                    <Chip
                      size="sm"
                      variant="soft"
                      color={settling ? "warning" : "default"}
                    >
                      <Chip.Label>{settling ? "Settling" : "Archived"}</Chip.Label>
                    </Chip>
                  </Card.Header>
                  <Card.Content className="space-y-4 pt-0">
                    <div className="grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3">
                      <Stat
                        label="Attributed P&L"
                        value={money(pnl)}
                        tone={pnl > 0 ? "pos" : pnl < 0 ? "neg" : "default"}
                      />
                      <Stat
                        label="Resolved · W–L"
                        value={`${summary?.closed_positions ?? 0} · ${summary?.win_count ?? 0}–${summary?.loss_count ?? 0}`}
                      />
                      <Stat
                        label="Open · resting"
                        value={`${open} · ${pending}`}
                      />
                    </div>
                    {settling && (
                      <p className="flex items-start gap-1.5 text-xs leading-relaxed text-warning">
                        <Icon
                          icon="solar:refresh-circle-bold"
                          className="mt-0.5 size-3.5 shrink-0"
                          aria-hidden="true"
                        />
                        {lifecycleText(open, pending)} still settling in this
                        strategy&apos;s preserved history.
                      </p>
                    )}
                  </Card.Content>
                </Card>
              );
            })}
          </div>
        </section>
      )}

      {accounts.length > 0 && (
        <section aria-labelledby="archived-account-balances">
          <div className="mb-3">
            <h3
              id="archived-account-balances"
              className="text-sm font-semibold text-foreground"
            >
              Historical account balances
            </h3>
            <p className="mt-1 text-xs leading-relaxed text-muted">
              Early live and research orders used one economic paper account,
              so that shared balance cannot be split honestly by strategy.
              Later isolated ledgers remain separate below.
            </p>
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            {accounts.map((account) => {
              const open = account.open_positions ?? 0;
              const pending = account.pending_limit_orders ?? 0;
              const settling = open > 0 || pending > 0;
              const pnl = account.realized_pnl ?? 0;
              return (
                <Card
                  key={account.key ?? account.account_id}
                  className="rounded-2xl border border-border/60 bg-surface/80"
                >
                  <Card.Header className="flex flex-row items-start justify-between gap-3">
                    <div className="flex min-w-0 items-start gap-2.5">
                      <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-surface-secondary text-accent ring-1 ring-border/60">
                        <Icon
                          icon={settling ? "solar:clock-circle-bold" : "solar:wallet-money-bold"}
                          className="size-4.5"
                          aria-hidden="true"
                        />
                      </span>
                      <div className="min-w-0">
                        <Card.Title className="truncate text-sm">
                          {account.label ?? "Archived paper account"}
                        </Card.Title>
                        <p className="mt-0.5 text-xs text-muted">
                          Economic ledger · new entries disabled
                        </p>
                      </div>
                    </div>
                    <Chip
                      size="sm"
                      variant="soft"
                      color={settling ? "warning" : "default"}
                    >
                      <Chip.Label>{settling ? "Settling" : "Archived"}</Chip.Label>
                    </Chip>
                  </Card.Header>
                  <Card.Content className="space-y-4 pt-0">
                    <div className="grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3">
                      <Stat
                        label="Total realized balance"
                        value={money(account.realized_equity, { sign: "negative-only" })}
                      />
                      <Stat
                        label="Account P&L"
                        value={money(pnl)}
                        tone={pnl > 0 ? "pos" : pnl < 0 ? "neg" : "default"}
                      />
                      <Stat
                        label="Open · resting"
                        value={`${open} · ${pending}`}
                      />
                    </div>
                    {settling && (
                      <p className="flex items-start gap-1.5 text-xs leading-relaxed text-warning">
                        <Icon
                          icon="solar:refresh-circle-bold"
                          className="mt-0.5 size-3.5 shrink-0"
                          aria-hidden="true"
                        />
                        {lifecycleText(open, pending)} still settling; this
                        account balance remains provisional.
                      </p>
                    )}
                    {account.reconciliation_status && (
                      <p className="font-mono text-[10px] uppercase tracking-[0.12em] text-muted">
                        Ledger {account.reconciliation_status}
                      </p>
                    )}
                  </Card.Content>
                </Card>
              );
            })}
          </div>
        </section>
      )}
    </div>
  );
}
