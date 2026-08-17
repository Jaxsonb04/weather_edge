# Settlement observability and outcome-field semantics

Two defects in how the paper journal records outcomes, and what changed. Both
are measurement changes: **no `StrategyConfig` or `ResearchSleevePolicy` field
was added, renamed, or re-defaulted, and no admission, exit, veto, exposure,
loss-pause, calibration, or liquidity threshold moved.** The policy fingerprints
`7d14362fe8bd99af7ef4a06d` (limit) and `b6a41dbd68ee2fe51d3db026` (market) are
unchanged, so the real-money readiness clock is untouched.

---

## 1. The settlement blind spot

### What was wrong

`paper_orders.settlement_high_f` and `paper_orders.resolved_yes` are written by
exactly one code path: `PaperStore.settle_paper_orders`. That path only updates
rows that are **still open** when the day settles. Two consequences followed:

- A market-day whose every lot was exited early by the monitor kept its exit
  price and its realized P&L, and **no record whatsoever of what the market
  actually did**.
- Worse, `cmd_paper_auto_settle` enumerates `open_paper_target_dates()`, so a
  *target date* on which nothing survived to settlement never reached the
  settlement path at all.

The system therefore observed outcomes only on the days it happened to still be
holding. That is precisely the population an exit-rule change has to be judged
on, so **no exit rule could be proven better or worse than any other.**

Measured on the production journal (read-only, 2026-08-16): closed lots whose
outcome is obtainable realized **+$76.12**, while **250 lots across 145
market-days (-$178.66 realized) had no obtainable settlement outcome at all.**
The losses live where the visibility does not.

### What changed

A new table, `market_day_settlements`, records the final outcome of **every**
market-day the book traded, whether or not a position survived to settlement.

It is a **new** table on purpose. `paper_orders` feeds the policy fingerprint,
the replay machinery, and the restatement harness; adding or rewriting a column
there would put the readiness clock at risk. A new table changes no existing
row, gate, or policy, so recording here costs zero evidence.

Population has three paths:

| Path | Trigger | Covers |
| --- | --- | --- |
| `settle_paper_orders` | any settlement | every traded market-day on that `(series, target_date)`, including the fully-exited ones |
| `paper-auto-settle` record-only pass | a target date with traded market-days but nothing open | the residual the settle path structurally cannot reach |
| `paper-backfill-market-day-settlements` | operator, one-off | history |

### Truth sources

Only independently validated sources are accepted, in this order of authority:

1. **`settlement_path`** (rank 3) — the integer °F high handed to
   `settle_paper_orders` at the moment the day settled. The same number the
   ledger booked against.
2. **`settled_sibling`** (rank 2) — `settlement_high_f` persisted on a
   `PAPER_SETTLED` order for the same `(series_ticker, target_date)`. Verified:
   zero internal conflicts.
3. **`dataset_kalshi_markets`** (rank 1) — the exchange's own finalized
   `result` for the exact ticker. Verified: zero disagreements with the
   settled-sibling highs. Carries no temperature, so `settlement_high_f` stays
   NULL on rows sourced this way rather than inventing one.

Tested and **rejected** — do not reintroduce these:

| Rejected source | Why |
| --- | --- |
| `probability_snapshots.observed_high_f` | a running intraday max; exact on 1.5% of days |
| station METAR daily max | 27.4% exact and systematically 1 °F low |
| `market_snapshots.result` | only ever the string `active` |

### Idempotency and precedence

The primary key is `(market_ticker, target_date)`. Derived counters
(`traded_lots`, `settled_lots`, `closed_lots`, `realized_pnl`) refresh on every
write because they are a projection of `paper_orders`. The outcome fields only
move to an **equal-or-better** authority, so a late low-authority backfill can
never downgrade a recorded settlement. Re-running any path is a no-op.

### What remains unrecoverable

A traded market-day is unrecoverable when neither surviving source covers it:
no `PAPER_SETTLED` order exists for its `(series_ticker, target_date)` **and**
`dataset_kalshi_markets` holds no finalized `yes`/`no` result for its ticker.
Those days predate any durable capture of the outcome and cannot be
reconstructed from this database.

`paper-backfill-market-day-settlements` reports them explicitly rather than
guessing:

```
sfo-kalshi paper-backfill-market-day-settlements --dry-run
```

Every future day is covered, because the live settlement path now records the
whole traded market-day.

### Not archived, by design

`market_day_settlements` is deliberately absent from `archive.FULL_TABLES`. It
is fully reconstructible by the backfill from `paper_orders` and
`dataset_kalshi_markets`, both of which are already archived nightly. Adding it
would change the archive manifest shape that the retention gate verifies, for
no durability gain.

### Read-only by contract

Nothing in the trading path may read this table. It exists to measure decisions
after the fact, never to make one.
`test_market_day_settlements.py::test_nothing_in_the_trading_path_reads_the_observability_table`
enforces that with an allowlist over the package source.

---

## 2. The `resolved_yes` semantics defect

### What was wrong

`db.py::close_paper_order` derived `resolved_yes` from the P&L sign:

```python
position_won = realized_pnl > 0.0
resolved_yes = 1 if (position_won if side == "YES" else not position_won) else 0
```

and propagated it to child lots through `_insert_partial_close_lot`. So on a
closed row, a column named after **the market's** outcome actually recorded
**the position's** P&L sign.

Verified on production (read-only, 2026-08-16):

- It is a perfect function of `sign(realized_pnl)` and side: **764/764 rows.**
- It disagrees with the true market outcome on **211 of 514 checkable rows —
  41.1%.** (The original investigation measured 224/553 = 40.5% using a wider
  truth join; both land in the same place.)
- **45 of 445 ticker-days carry both `0` and `1` across their own lots.**

The field also leaked into a **public artifact**: `strategy_lab/paper_card.py`
emitted raw `resolved_yes` into the Strategy Lab payload, and
`store/diagnostics.py` persisted it into `outcome_diagnostics_json`.

### What deliberately did **not** change

`_row_position_won` / `_paper_order_won` and their `_decided` variants inverted
the same encoding, so **win/loss and hit-rate were correct the whole time.
There was never any P&L corruption.** Those readers were repointed, not
"fixed", and the accounting they produce is unchanged by construction.
`posterior_kelly.py`, `restatement.py`, `research_shadow.py` and
`backtest_rescore.py` are guarded or compute from the settlement high; they keep
working untouched.

### What changed

- `close_paper_order` no longer writes `resolved_yes`; it leaves it NULL.
- A new `position_won INTEGER NULL` column carries the position fact. A close
  writes it from the realized P&L sign; settlement writes it from the resolved
  market side. A break-even close leaves it NULL, so it stays undecided and out
  of the hit-rate denominator exactly as before.
- `resolved_yes` now strictly means **"the market resolved YES"** and is written
  only when a settlement high is known.
- The four `_won`/`_decided` readers prefer `position_won`, falling back to the
  historical `resolved_yes` decode for rows written before the split.
- The public Strategy Lab payload emits `resolved_yes` only when a settlement
  high proves one, and publishes `position_won` alongside it.
- `outcome_diagnostics_json` no longer carries `resolved_yes` on close. Its
  `position_won` and `win_loss_reason` fields were already correct and are
  untouched.

### Migration

`schema.py::_migrate_closed_row_position_won`, keyed
`closed_row_position_won_v1` in `schema_migrations`, runs once:

1. For every `PAPER_CLOSED` row with a non-NULL `resolved_yes`: decode it with
   the exact rule every reader used, write that into `position_won`, set
   `resolved_yes` back to NULL, and drop the stale `resolved_yes` key from the
   persisted outcome block.
2. For every `PAPER_SETTLED` row: derive `position_won` from the real
   `resolved_yes`, so both columns mean one thing each.

**The backfill is lossless.** The stored value carried no information beyond the
P&L sign, and the P&L is already in `realized_pnl`. Decoding rather than
recomputing guarantees no row's win/loss can move even if a stored value were
inconsistent with its P&L — and on production none is (764/764).

### The restatement blind spot this closed

`restatement.py` reconciled `resolved_yes` against real market truth in exactly
one place, `_settled_accounting_findings`, which is reached only for
`PAPER_SETTLED` rows and then returns early unless `settled_at` parses. A closed
row has no `settled_at` by definition. **The project's strictest integrity
harness structurally could not see a closed-row outcome defect — which is how
this survived for months without a single finding.**

`_closed_accounting_findings` now also runs `_closed_outcome_semantics_findings`,
which raises:

| Finding | Meaning |
| --- | --- |
| `CLOSED_ROW_CLAIMS_MARKET_OUTCOME` | a closed row carries a non-NULL `resolved_yes` |
| `CLOSED_OUTCOME_CLAIMS_MARKET_OUTCOME` | its persisted outcome block still carries one |
| `CLOSED_POSITION_WON_MISMATCH` | `position_won` disagrees with the realized P&L sign |
| `CLOSED_OUTCOME_EVENT_MISMATCH` | the outcome block is not an exit |
| `CLOSED_OUTCOME_RESOLVED_AT_MISMATCH` | its `resolved_at` is not the row's `closed_at` |
| `CLOSED_OUTCOME_POSITION_WON_MISMATCH` | the outcome block's win/loss disagrees with the P&L |

These are generation-gated the same way the settled-row checks already are, and
after the migration a clean database raises none of them.
