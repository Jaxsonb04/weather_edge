# Logical Paper Positions and Research Lead-Time Design

Date: 2026-07-17
Status: Approved for implementation

## Summary

WeatherEdge records depth-limited partial exits as immutable child rows in
`paper_orders`. That execution journal is correct, but several reporting and
risk-control paths treat each child exit lot as an independent trade. The
result is a duplicated-looking closed-position ledger, inflated resolved-trade
and loss counts, a depressed hit rate, misleading best/worst-trade rankings,
and an early circuit-breaker sample threshold.

This design preserves the immutable execution journal and adds one shared
logical-position projection for all decision-level reporting. It also fixes a
live weekly-goal account leak, corrects partial-exit per-contract diagnostics,
and moves same-day research signals to shadow evidence rather than actual paper
positions. Live gates and day-ahead research gates remain unchanged.

## Evidence

### Duplicate-looking Phoenix trade

The four published Phoenix rows are one logical position:

- Root paper order: `456`
- Market: Phoenix 97 degrees or above, NO
- Filled position: 8 contracts at 93 cents
- Exits: four depth-verified fills of 2 contracts each at 86 cents
- Child exit lots: `458`, `459`, and `460`; the final 2 contracts resolved on
  the root row
- Aggregate realized result: approximately `-$0.63`

The child rows correctly preserve the liquidity evidence and accounting for
each fill. The defect is exclusively in consumers that discard
`parent_order_id` when counting or rendering trades.

The same issue splits a Philadelphia 35-contract position into three rows and
hides the logical position's aggregate loss of approximately `-$16.88`.

### Correct logical live record

At the investigation snapshot, the public page reported 62 live resolved rows
and a 40-22 record. Grouping partial exit lots into their originating decisions
produces:

- 57 logical resolved positions
- 40 wins and 17 losses
- 70.2% decision-level hit rate rather than 64.5%
- The same realized P&L and resolved capital, because lot-level money sums were
  already arithmetically correct

### Weekly profile performance

For the current Pacific-time calendar week:

| Profile / lead mode | Logical trades | W-L | Realized P&L | ROI |
| --- | ---: | ---: | ---: | ---: |
| Live, day-ahead | 11 | 9-2 | +$16.58 | +7.0% |
| Research, day-ahead | 6 | 4-2 | +$2.57 | +5.3% |
| Research, same-day | 11 | 4-7 | -$26.37 | -31.3% |

The complete authoritative AWS paper history confirms that the same-day result
is not merely a one-week fluctuation:

| Research lead mode | Logical trades | W-L | Realized P&L | ROI |
| --- | ---: | ---: | ---: | ---: |
| Day-ahead | 51 | 28-23 | +$6.49 | +2.4% |
| Same-day | 31 | 12-19 | -$76.60 | -33.7% |

The corrected execution-v3 cohort is consistent with this split: research
day-ahead was positive while same-day remained materially negative.

### Additional confirmed defects

1. The live weekly-goal query filters only on the shared account. Legacy
   research rows that still belong to that account therefore contribute to the
   displayed live result. At the investigation snapshot, approximately `$0.85`
   of research P&L increased the displayed live weekly result from the true
   `+$16.58` to `+$17.42`.
2. Partial-close outcome diagnostics divide an executed slice's P&L by the
   root order's pre-close contract count. The stored `pnl_per_contract` is
   therefore understated for child exit lots.
3. The paper-entry circuit breaker uses raw resolved row count. Multiple exit
   lots can satisfy its minimum sample size before the required number of
   independent decisions exists.

## Goals

1. Preserve every immutable entry and exit lot and its accounting evidence.
2. Make every trade count, win/loss count, hit rate, ranking, and closed-position
   listing decision-level rather than lot-level.
3. Continue summing each execution lot exactly once for realized P&L, capital,
   fees, cash timing, and account reconciliation.
4. Keep incomplete partially exited positions open and out of resolved-decision
   counts until the root position becomes terminal.
5. Isolate the live weekly objective from all research-profile results.
6. Correct partial-exit per-contract outcome diagnostics.
7. Stop actual same-day research paper entries while preserving their signals
   as decision and shadow evidence.
8. Preserve tolerant public JSON parsing and compatibility with legacy rows
   that have no `parent_order_id`.

## Non-goals

- Rewriting or compacting the execution journal
- Deleting historical child rows
- Altering the account ledger or restating realized P&L
- Relaxing or tightening live entry gates
- Changing day-ahead research thresholds or sizing
- Promoting the paper strategy to real money
- Treating a research shadow selection as a paper execution

## Domain model

### Execution lot

An execution lot is one physical row in `paper_orders`. It remains the source
of truth for:

- Executed quantity
- Entry cost and exit proceeds
- Fees
- Realized P&L
- Liquidity and tape evidence
- Ledger reconciliation
- Exact realized-cash timestamp

### Logical position

A logical position is one originating entry decision and all of its immutable
partial-close children.

The logical identifier is:

```text
parent_order_id when present, otherwise id
```

The root row is the row whose `id` equals the logical identifier and whose
`parent_order_id` is null.

### Terminal logical position

A logical position is resolved only when its root status is
`PAPER_CLOSED` or `PAPER_SETTLED` and the root has realized P&L. A child lot
does not resolve the logical position while the root remains open.

`PAPER_EXPIRED` remains a never-filled order and is not a resolved position.

## Required invariants

The shared projection must enforce or expose failures of these invariants:

1. Every child references an existing root.
2. A child cannot itself be another child's parent.
3. Root and children agree on market, target date, side, risk profile, account,
   entry price, and entry cost within the existing numeric tolerance.
4. Logical filled quantity equals root remaining/resolved quantity plus all
   child quantities.
5. Logical realized P&L and resolved capital equal the sum of resolved lots,
   with each lot included once.
6. A non-terminal root is never counted as a resolved decision even when it has
   realized child lots.
7. A terminal root produces exactly one resolved trade outcome.
8. Legacy rows without parent metadata behave as one-row logical positions.

Malformed relationships must not silently merge unrelated orders. The
projection should fail closed for the affected group, surface an integrity
diagnostic, and retain the underlying rows for audit.

## Shared logical-position projection

A dependency-light module in the trading package will provide the canonical
grouping and aggregation helpers. It will accept `sqlite3.Row` or mapping-like
rows so the same logic can serve database summaries, Strategy Lab publication,
and unit tests.

Each projected position will contain root entry attributes plus derived fields:

- `logical_order_id`
- `contracts`: total filled contracts across root and children
- `resolved_contracts`
- `open_contracts`
- `resolved_lot_count`
- `exit_fill_count`
- `child_order_ids`
- `realized_pnl`: sum across resolved lots
- `capital_resolved`: sum of `contracts * cost_per_contract` across resolved
  lots
- `exit_price`: contract-weighted average gross exit price when available
- `exit_fee_per_contract`: contract-weighted average exit fee
- `closed_at` / `settled_at`: terminal root timestamps, with the latest
  resolved-lot timestamp available separately for audit
- Aggregate win/loss/undecided outcome
- Integrity findings, if any

The projection will expose both group membership and the terminal logical
position view. Consumers that need exact cash timing can continue iterating the
execution lots while taking counts and outcomes from logical positions.

## Consumer changes

### Market paper summary

`market_backtest_summary` will:

- Count one terminal logical position as one order
- Count one aggregate decision outcome as one win or loss
- Calculate hit rate from logical outcomes
- Sum contracts, capital, and P&L from resolved lots exactly once
- Count partially exited roots as open until terminal
- Preserve target-date filters
- Use root entry edge for the decision-level average edge

### Strategy Lab paper card

The paper card will:

- Publish one closed-position row per terminal logical position
- Publish aggregate contracts, P&L, ROI, and weighted exit price
- Include `exit_fill_count` and `child_order_ids`
- Calculate profile summaries and diagnostics from logical positions
- Keep exact monitor actions and exit events as lot-level operational history
- Keep duplicate-open detection unchanged because it diagnoses concurrent open
  entries, not partial exits

### Daily and rolling summary

The daily summary will separate money timing from decision timing:

- Realized P&L continues to post on each lot's actual close or settlement day
- A trade opens once, on the root fill day
- A trade closes or settles once, when the root becomes terminal
- The win/loss outcome is the aggregate logical result
- Best/worst trades, side performance, profile totals, and recommendations use
  logical positions
- Capital and P&L totals remain the sum of execution lots

This avoids moving cash between days while eliminating duplicate trade counts.

### Circuit breaker

The minimum resolved-trade threshold will use distinct terminal logical
positions. ROI and daily loss remain lot-summed so the breaker retains correct
money sensitivity.

### Readiness replay

The readiness replay already groups partial-close lots by parent decision. Its
behavior is retained and regression-tested against the new shared semantics.
No promotion threshold changes are part of this work.

## Live weekly-goal isolation

The weekly-goal resolved-row query will require both:

```text
account_id = paper-shared
risk_profile normalized to live
```

Legacy missing profiles normalize to live for backward compatibility. Explicit
research aliases normalize to research and are excluded.

The starting-equity calculation continues to use the actual shared-account
realized equity. Only the attributed weekly P&L and objective return numerator
exclude research, matching the public claim that research never contributes to
the live objective.

The payload's exclusion metadata and disclaimer will explicitly state that all
research profiles are excluded.

## Partial-exit diagnostics

`_outcome_diagnostics_payload` will accept the executed quantity explicitly.
Settlement and full-close callers pass the row quantity; partial-close callers
pass the depth-limited executed quantity.

The outcome payload will record:

- `executed_quantity`
- `realized_pnl`
- `pnl_per_contract = realized_pnl / executed_quantity`

The existing `exit_execution` evidence remains authoritative and must agree
with the new outcome field.

## Research lead-time policy

Actual research paper positions will require at least a day-ahead target. A
same-day target will be entry-blocked with an explicit research-policy reason.

The production portfolio scan will continue to:

1. Evaluate the same-day market and model probabilities.
2. Record decision snapshots with `signal_approved` preserved.
3. Record eligible blocked research candidates in the shadow ledger.
4. Place no actual same-day research paper order.

Day-ahead research remains the wide data-collection profile. It keeps its
current loose price curve, model regimes, edge thresholds, sizing, and account
isolation. Live already requires day-ahead entry and is unchanged.

The public explanation will call these prediction-market research signals and
will not name a venue.

## Public JSON and UI

The JSON change is additive except for corrected counts and collapsed closed
rows:

- `closed_positions` contains logical positions instead of exit lots.
- Each logical row may add `logical_order_id`, `exit_fill_count`, and
  `child_order_ids`.
- Existing fields remain present and retain their types.
- Missing new fields continue to parse safely in the SPA.

The detailed ledger will render one row per logical position. When
`exit_fill_count > 1`, it will display a compact `N fills` annotation alongside
the entry-to-exit price. This explains the liquidity-limited execution without
reintroducing duplicate rows.

The quantity, P&L, ROI, city grouping, and profile totals will all use the
aggregate logical row.

## Test strategy

Implementation will be test-driven. New failing regressions will cover:

1. A position closed through three children plus a terminal root appears as one
   logical trade with correct quantity, P&L, capital, weighted exit, and fill
   count.
2. A root with closed children but remaining open quantity is not resolved.
3. Market summary counts one decision while preserving lot-summed money.
4. Strategy Lab publishes one closed ledger row and corrected profile record.
5. Daily summary counts one opening and one terminal outcome while retaining
   exact lot-day P&L.
6. Circuit-breaker minimum sample size cannot be reached by partial-exit lots.
7. Weekly live P&L excludes an explicit research row in the shared account and
   includes a legacy null-profile live row.
8. Partial-exit `pnl_per_contract` uses executed quantity.
9. Research same-day entry is blocked, its signal remains recorded, and the
   portfolio path writes shadow evidence.
10. Day-ahead research and live entry behavior remain unchanged.
11. Legacy single-row paper orders produce unchanged logical outputs.
12. Malformed parent relationships produce visible integrity findings and do
    not merge unrelated positions.

Existing readiness, restatement, account-ledger, fill, settlement, and browser
tests remain required.

## Verification and rollout

Before completion:

1. Run focused failing tests, implement the minimum behavior, and show the
   focused tests passing.
2. Run the complete Python suite.
3. Run SPA unit tests, lint, and production build.
4. Run the repository security/static-analysis checks.
5. Verify SQLite quick-check, foreign keys, parent integrity, quantity
   conservation, and account reconciliation on a copy or read-only connection
   to the authoritative AWS database.
6. Generate a Strategy Lab artifact from the authoritative runtime and compare
   raw lots to logical counts and money totals.
7. Confirm the corrected live weekly attribution.
8. Clear stale local runtime state before local dashboard verification.
9. Serve the production SPA build and use browser automation to verify desktop
   and real mobile layouts, including DOM-read assertions.
10. Confirm Phoenix renders once with four fills and that no public copy names a
    prediction-market venue.

Deployment remains paper-only. No historical execution rows are mutated. The
corrected report can be regenerated from the existing journal.

## Risks and mitigations

### Double-counting or dropping money

Mitigation: keep all monetary sums lot-based and assert equality between raw
lot totals and projected totals in tests and production diagnostics.

### Counting an incomplete partial exit as resolved

Mitigation: terminality comes only from the root status, never from child rows.

### Hiding corrupt parent relationships

Mitigation: validate group invariants, publish integrity findings, and refuse to
merge invalid groups silently.

### Overfitting the research tune

Mitigation: the same-day policy is supported by both the current week and all
available AWS history. It does not alter the positive day-ahead research cohort
or any live gate.

### Breaking older public artifacts

Mitigation: new JSON fields are optional and SPA parsing remains tolerant of
their absence.

## Completion criteria

The change is complete only when:

- The immutable execution journal remains unchanged.
- All decision-level consumers use the shared logical projection.
- Raw and projected P&L/capital reconcile exactly.
- Phoenix and Philadelphia render as one logical row each.
- Corrected live counts and weekly attribution match the authoritative data.
- Same-day research produces evidence but no paper position.
- Focused and full regression suites pass.
- Desktop and mobile browser verification passes.
- No unresolved integrity, static-analysis, or runtime-health defect remains in
  the requested scope.
