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

---

## 3. Exchange settlement reconciliation

This is a measurement change like the two above: no `StrategyConfig` or
`ResearchSleevePolicy` field and no version constant moved.

### What was missing

`verify_paper_settlements` compares the high a lot was booked against with the
final NWS CLI maximum in `weather.db`. On the settle timer that is the number
the lot was just settled from, so the check can only notice a CLI value that
changed *after* settlement. Nothing compared the journal with what the exchange
itself settled the market on (audit D.8 asked for exactly that).

The gap has a measured failure shape. Kalshi moved daily-high settlement to The
Weather Company on 2026-08-14/15. Since then its `expiration_value` has equalled
the NWS CLI integer on 434 of 435 station-days, and all 53 auditable settled
lots in this book match the exchange, so the scorer is not wrong. The exception
was **Miami 2026-08-29**: the NWS issued two conflicting CLI versions (maximum
90 at 04:24 EDT, then 85 at 05:10 EDT) and the exchange settled on 90.
`forecaster/clisfo.py` keeps the newest final version it sees, so a lot settled
from the archive would have been booked on 85: YES on "87 or below", NO on
"90-91", and the booked-vs-CLI verification would have called both `MATCH`.

### What changed

`paper-auto-settle` and `paper-resettle --verify` now also read each settled
lot's market from the exchange's public API and record the comparison in
`paper_settlement_exchange_checks`, one row per settled lot:

| Column | Meaning |
| --- | --- |
| `booked_high_f`, `booked_winner` | the integer high the journal settled on, and the market side (`YES`/`NO`) it paid out |
| `kalshi_status`, `kalshi_result`, `kalshi_expiration_value` | the exchange's `status`, `result`, and `expiration_value` |
| `kalshi_source_endpoint` | `markets` or `historical/markets` |
| `verification_status` | `MATCH`, `MISMATCH`, `KALSHI_PENDING`, or `UNCHECKED` |
| `mismatch_reason` | for `MISMATCH`: `result`, `expiration_value`, and/or `result_not_yes_no` |
| `check_error` | for `UNCHECKED`: why the market could not be read, or why the lot could not be classified |
| `exchange_attempted_at` | when a run last tried to read the lot's market; lots are re-checked least-recently-attempted first |

- **`MATCH`**: finalized, and both the result and the settlement value agree.
- **`MISMATCH`**: finalized, and either one disagrees. The value is compared on
  its own because a lot on a bin both numbers fall on the same side of (Miami
  "94-95") paid out correctly while its siblings did not. Reporting only payout
  disagreements would hide the signal that the day's truth source diverged.
- **`KALSHI_PENDING`**: the exchange has not finalized the market.
- **`UNCHECKED`**: the market could not be fetched or read. It is never read as
  agreement and is retried on a later run.

A `MISMATCH` prints an `EXCHANGE SETTLEMENT MISMATCH` line on stderr and is
counted in the `exchange settlement check:` summary each command prints, next
to the existing `settlement verification:` line; `standing_mismatches` counts
every `MISMATCH` on record. The timer never re-selects a decided lot, so that
per-lot line reaches stderr once; every later run with a mismatch on record
also prints `STANDING EXCHANGE SETTLEMENT MISMATCHES: N` on stderr. It is an
incident signal: open a restatement, do not edit the journal. Both lines are
log-only and never alert, whether or not `SFO_FRESHNESS_ALERT_URL` is set: the
settle unit's `OnFailure=` fires only on a non-zero exit, which the check never
causes. A mismatch is visible only in the settle unit's journal and in the
table. The per-lot line appears once and the standing line on every later run,
so a daily check of this window cannot miss a mismatch on record:

```bash
sudo journalctl -u sfo-kalshi-paper-settle.service --since yesterday --no-pager \
  | grep -E 'EXCHANGE SETTLEMENT (MISMATCH|CHECK FAILED)|STANDING EXCHANGE SETTLEMENT MISMATCHES'
```

On the timer, lots settled five to seven days ago that still hold no
`MATCH`/`MISMATCH` are counted as `aging_undecided` and named on stderr. They
are about to leave the timer's seven-day re-check window, usually because the
exchange has been unreachable, and should be backfilled (below).

### Never blocks settlement

The check runs after the journal is written and holds no database lock while it
talks to the network. Transport failures, HTTP errors, malformed payloads, and
even an exception inside the check itself are reported on stderr and leave the
exit status at 0; on the settle timer a non-zero exit would fire `OnFailure=` as
though settlement had failed. The first transport failure stops fetching for
the rest of that run, so an exchange outage costs one timeout rather than one
per lot. Finalized results are cached before any lot is classified, and each
lot is classified on its own: a lot that cannot be classified is `UNCHECKED`
with the reason in `check_error`, and a later failure in the run (a locked
database, say) cannot discard what was already fetched.

`--skip-exchange-check` runs either command offline. In production,
`SFO_EXCHANGE_SETTLEMENT_CHECK=off` in the settle unit's EnvironmentFile turns
the check off without editing a canonical unit, which the post-install
integrity gate rejects. An unrecognized value keeps the check on and says so on
stderr.

### Rate and caching

- Requests are spaced at least 0.4 s apart, under the public API's ~3 requests
  per second, with a 10 s timeout and 2 attempts per request.
- The settle timer fetches at most 10 markets per run and re-checks only lots
  settled in the last seven days that do not yet hold `MATCH`/`MISMATCH`. The
  timer fires on the same minutes as a trading scan (`:10`, `:40`) and shares
  the box's public-API allowance, so the budget stays small: ten spaced markets
  take about four seconds, and 48 runs a day still reach ~480 markets against
  at most ~90 (15 city events of 6 brackets) settling per day.
- Markets never attempted come first, newest settlement first, then the least
  recently attempted (`exchange_attempted_at`). A market that never becomes
  decidable -- missing from both endpoints, or stuck unfinalized -- is tried
  once per run at the back of the queue and cannot starve newly settled lots.
- Finalized results are cached per ticker in `kalshi_market_resolutions` and are
  never fetched again. Pending markets are not cached.
- A decided verdict is never downgraded by a later `KALSHI_PENDING` or
  `UNCHECKED` observation.
- Neither table is archived. Both are reconstructible by re-running
  `paper-resettle --verify --exchange-check-only` over the wanted window.

### Backfilling history

The timer looks back only seven days. Backfill older lots with:

```bash
python -m sfo_kalshi_quant.cli --no-color paper-resettle --verify --exchange-check-only --days N
```

It walks the whole window with a budget of 400 markets
(`--exchange-max-fetches`) and writes only the two exchange tables.

A trading scan fires every five minutes, so a backfill always overlaps one
and shares its public-API allowance. A market old enough to have left the
live endpoint costs two spaced requests, so the full budget is up to about
five minutes of fetching. Keep each burst short by running it in slices, for
example with `--exchange-max-fetches 100` (about 80 seconds). Finalized
results are cached and never-attempted lots go first, so each slice resumes
where the last one stopped; repeat until the `unchecked` count stops falling.
Markets missing from both endpoints stay `UNCHECKED` and are retried each run.

Do not use plain `paper-resettle --verify` for this. It first re-runs the
booked-vs-CLI sweep, which upserts `paper_settlement_verifications`, and
`restatement.py` classifies settled lots from that table. Over a wide window
that adds rows for lots that had none, which clears
`SETTLEMENT_VERIFICATION_REQUIRED`. It also flips `MATCH` to `MISMATCH`
wherever a CLI final changed after settlement. Widening that sweep is a
separate, owner-approved step that changes restatement findings; take a
restatement diff before and after it.

### Live and historical endpoints

`GET /markets/{ticker}` serves only recent markets; older markets move to
`GET /historical/markets/{ticker}`. Measured 2026-09-13,
`KXHIGHMIA-26AUG29-T88` is live-only (historical 404) and
`KXHIGHTSFO-26JUN12-T81` is historical-only (live 404). The book's settled
history starts 2026-06-10, so the historical endpoint is asked after a live 404;
a market missing from both is `UNCHECKED`.

### CLI issuance is not recorded

The check does not record which CLI issuance a booked high came from, because
no machine-readable issuance exists where settlement truth is loaded.
`cli_settlements` stores `station_id, local_date, max_temperature_f,
fetched_at, source, is_final`, and `forecaster/clisfo.CliReport` parses only a
report date, a maximum, and a preliminary flag. `fetched_at` is when this
project fetched the product, not when the NWS issued it, so it is not recorded
as an issuance. Recording the product's issuance time and version would need a
forecaster-side change first.

### Read-only by contract

The check writes only its two tables. It never writes `paper_orders`, the
ledger, or `paper_settlement_verifications`, and `restatement.py` does not read
it, so it moves no evidence classification, gate, fingerprint, or version.
`test_exchange_settlement_checks.py::test_nothing_in_the_trading_path_reads_the_exchange_check_tables`
enforces that nothing else in the package reads the two tables.
