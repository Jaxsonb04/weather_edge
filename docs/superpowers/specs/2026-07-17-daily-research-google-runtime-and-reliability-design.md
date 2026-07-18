# Daily Research Objective, Google Runtime Weather, and Reliability Design

**Date:** 2026-07-17  
**Status:** Approved operating direction; implementation specification  
**Scope:** paper execution/accounting, research strategy isolation, multi-city
Google Weather runtime data, model evaluation, reporting, and performance-impacting
reliability defects

## 1. Decision summary

WeatherEdge will keep one unchanged, conservative live-candidate book and split
paper research into two new, financially isolated books:

1. `research-target` pursues a hard daily realized-P&L objective of $50, equal
   to 5% of a fixed $1,000 reference account.
2. `research-motion` executes every realistic, eligible exploration opportunity
   at the smallest tradeable size so the system obtains fill, exit, and outcome
   evidence without contaminating the target book.

The $50 figure is a hard research KPI, not a guarantee and not a reason to
manufacture fills, admit negative expected value into the target book, or alter
the live candidate. A missed day is recorded as a miss. The engine must report
when the available opportunity set cannot plausibly reach the objective.

Google Weather will become a city-aware, database-backed runtime source across
the 15 existing cities. Google data will not train LSTM or EMOS, will not receive
learned weights, and will not replace station-aligned NWP/EMOS baselines. Google
content is stored only for its documented lifetime and used as a current
corroboration or fixed runtime blend input. Permanent forecast training rows
remain Google-free.

The previously implemented logical-position layer remains the accounting truth.
Every remaining dashboard, calibration, readiness, and summary consumer must be
migrated from execution-lot counts to logical-position counts while money stays
lot-exact.

No real-money order placement is enabled by this work. Live trading remains
disabled until the existing chronological readiness checks all pass and the
operator separately authorizes real orders.

## 2. Evidence behind the design

The authoritative EC2 database was copied with SQLite's online backup API at
2026-07-17 23:51:52 UTC and passed `quick_check` and foreign-key validation.
The journal contained 512 non-rejected execution rows, 506 canonical logical
positions, 205 terminal decisions, and no invalid logical groups. Raw and
logical accounting reconciled exactly at -$99.9540 realized P&L and $1,234.8547
resolved capital.

The apparent duplicate rows are child exit fills, not independent decisions.
The core logical-position correction is implemented, but older Strategy Lab
and CLI summary paths still consume execution lots directly and can therefore
render partial exits as duplicate trades.

For the current Pacific week through the snapshot:

| Book/cohort | Decisions | W-L | P&L | Resolved-capital ROI |
|---|---:|---:|---:|---:|
| Live, all | 11 | 9-2 | +$16.5750 | +7.02% |
| Research, all | 19 | 9-10 | -$25.8856 | -17.19% |
| Research, day-ahead | 5 | 4-1 | +$8.0129 | +20.08% |
| Research, same-day | 14 | 5-9 | -$33.8985 | -30.64% |

All-history research shows the same direction: day-ahead lost $4.3524 on 56
decisions (-2.10%), while same-day lost $109.6935 on 92 decisions (-20.63%).
The relative day-ahead-minus-same-day daily-P&L advantage has a positive
day-clustered bootstrap interval, but day-ahead's absolute mean interval still
crosses zero. Therefore same-day is demonstrably worse, while day-ahead is not
yet proven profitable.

No completed live or research calendar day has earned $50. The best live day
earned $7.9454, and completed-history live mean daily P&L was $0.4748 with a 95%
day-bootstrap interval of -$0.6499 to +$1.5050. The target is deliberately
ambitious and will be measured honestly.

Current research controls are internally inconsistent. The portfolio allocator
permits up to approximately $250 directional risk on $1,000, while downstream
account policy clips research near $10 per position and $40 aggregate, and the
research circuit breaker pauses near a $5 daily loss. Research motion is also
throttled by a three-entry lifetime cap, 25% deterministic sampling of marginal
trades, and a sampled exposure budget. These conflicts must be removed rather
than papered over with looser edge thresholds.

## 3. Alternatives considered

### A. One aggressive research account

Increase risk limits and remove trade-count gates in the current account. This
is the smallest code change, but exploration losses, pauses, cash, P&L, and
calibration would contaminate the $50 evaluation. Rejected.

### B. One account with tagged sleeves

Tag target and motion orders but share cash and circuit breakers. This improves
reporting but still allows one experiment to pause or bankrupt the other and
does not produce an independently auditable target book. Rejected.

### C. Separate target and motion accounts

Give each sleeve its own $1,000 virtual account, policy, cash ledger, exposure,
pause state, and reporting. Preserve the legacy research account as historical
evidence. This costs more schema and reporting work but produces clean causal
evidence and lets motion increase without corrupting target performance.
Selected.

For Google, three options were considered: permanent adaptive Google learning,
a fixed runtime blend, and corroboration-only. Permanent adaptive learning is
out of scope because the user specified that Google is not a training input.
The selected rollout keeps the existing SFO runtime behavior compatible and
starts the other cities at corroboration-only. A fixed, versioned runtime
adjustment may be tested in research after the ingestion path is reliable, but
it cannot silently change live forecasts.

## 4. Non-negotiable invariants

1. `risk_profile=live`, `paper-shared`, `LIVE_PROFILE_OVERRIDES`, the live
   strategy fingerprint, live placement flags, and live readiness thresholds do
   not change.
2. Research accounts, rows, outcomes, Google experiments, and motion activity
   are excluded from every live P&L, pause, equity, readiness, and promotion
   calculation.
3. Trade counts are canonical logical roots. Realized P&L, fees, cash, capital,
   partial-fill timing, and partial-close timing remain immutable lot-level
   accounting.
4. A child lot that crosses account, sleeve, policy, strategy fingerprint, or
   execution-model identity invalidates the entire logical group.
5. Paper fills must be possible at the contemporaneous quote or verified public
   tape. Quote attempts and expired limits are not trades.
6. A daily target shortfall never loosens edge, data-integrity, market-status,
   liquidity, fee, or fill-validity gates.
7. Google cannot override final CLI truth, NWS observed-high locks, or the
   station/fixed-standard settlement calendar.
8. Expired or incomplete Google data is unavailable at the query boundary.
9. The Google event ledger is transactional and never exceeds the 260/day,
   8,000/month internal limits.
10. No change reaches the live candidate automatically from research evidence.

## 5. Account and research-sleeve architecture

### 5.1 Accounts

The following account identities are additive:

| Account | Purpose | Opening capital | Live readiness |
|---|---|---:|---|
| `paper-shared` | unchanged live candidate and legacy rows | existing | Live groups only |
| `paper-research-shadow` | immutable legacy research history | existing | Excluded |
| `paper-research-target-v1` | target-seeking research | $1,000 | Excluded |
| `paper-research-motion-v1` | high-motion execution research | $1,000 | Excluded |

New research orders must carry explicit `research_sleeve`, policy version,
policy fingerprint, objective day, lead bucket, and scan-run identity. Account
routing must be explicit; it must not be inferred from free-form reason strings.
Legacy nullable research rows remain legacy and never count toward the new
target's attainment history.

`paper-shared` is never included wholesale in readiness. Readiness includes
only logical groups whose root and every lot validate as live under the existing
explicit legacy-NULL policy. Research, unknown, or mixed-identity groups are
excluded, and mixed groups fail closed.

Research admission is atomic. One `BEGIN IMMEDIATE` transaction recomputes
account cash, the sleeve's civil-day pause state, active scenario exposure, and
duplicate state; inserts the order and its reservation or fill ledger entry;
then commits. The active-order backstop is a partial unique index on
`(account_id, target_date, market_ticker, UPPER(side))`. Failure to build the
index makes new research entry fail closed.

### 5.2 Target sleeve

The target sleeve has these paper-only policies:

- target/reference timezone: `America/Los_Angeles`;
- reference equity: fixed $1,000;
- daily target return: 5%;
- daily target realized P&L: $50;
- only realized target-account P&L counts;
- every calendar day after activation counts, including zero-activity days;
- `lead_days >= 1`;
- point edge and after-fee lower-bound edge must be non-negative;
- rank by conservative expected profit per worst-case dollar and expected log
  growth;
- allocate every eligible opportunity while realistic liquidity, account cash,
  and scenario-risk constraints permit; there is no trade-count cap;
- one concurrent logical position per market, side, and target sleeve;
- unlimited re-entry is allowed after the preceding logical position is terminal
  and a new decision snapshot demonstrates a changed executable opportunity;
- take immediately only when after-taker-fee lower-bound edge remains
  non-negative at visible ask depth; otherwise use an executable maker limit;
- after the target reaches $50, lock new target risk for that objective day so
  realized attainment cannot be gambled away; the motion account continues.

Paper-only target risk envelope:

- 3% maximum risk per logical position;
- 6% maximum city-target scenario loss;
- 12% maximum region-day scenario loss;
- 25% maximum aggregate open scenario loss;
- 10% realized daily-loss pause;
- cash and solvency remain hard constraints.

The $50 gap changes reporting and opportunity ranking, not the probability or
edge calculation. The portfolio report must expose when the currently available
opportunity set cannot reach $50 even if all expected profits realize.

### 5.3 Motion sleeve

The motion sleeve maximizes verified execution evidence rather than headline
P&L:

- same-day candidates are allowed until the existing station-local cutoff and
  never after the official high is complete;
- evaluate every point-positive research candidate and persist its disposition;
- remove the 25% sampler and lifetime entry-count cap;
- retain the current -0.07 lower-bound-edge floor and every data, source,
  market-status, price, and liquidity validity check;
- exactly one contract per entry;
- use immediate taker execution only at the contemporaneous visible ask, capped
  by displayed depth and charged full fees;
- one concurrent logical position per market, side, and motion sleeve;
- place eligible candidates in deterministic priority order until cash or
  scenario limits bind;
- unlimited terminal re-entry requires a different `scan_run_id` and at least
  one versioned change: executable price by at least 1 cent, side probability by
  at least 2 percentage points, or a new observed-high/completeness state;
- persist a re-entry fingerprint and reject the same fingerprint idempotently;
- quote attempts, rejected candidates, and unfilled orders remain evidence but
  never enter equity or resolved-trade counts.

Paper-only motion risk envelope:

- one contract per position;
- 2% maximum city-target scenario loss;
- 4% maximum region-day scenario loss;
- 10% maximum aggregate open scenario loss;
- 5% realized daily-loss pause;
- no arbitrary trade-count or minimum-$5-notional throttle.

Motion P&L is always displayed separately and is excluded from the target KPI,
live readiness, and target-policy promotion.

### 5.4 Correlated exposure

Research exposure is computed by settlement scenarios, not by summing historical
spend. Group open roots and pending limits by account, sleeve, city series, and
target date. Evaluate the maximum loss for each integer settlement bin. Pending
maker orders reserve full possible loss. Region exposure is the conservative
sum of city-target maxima. Closing a position releases its active exposure;
cumulative lifetime spend cannot be a turnover throttle.

## 6. Daily objective and reporting

Add a versioned `research_daily_goals` record for every target-account Pacific
day. The record freezes the policy, reference equity, target return, and target
P&L so later configuration changes cannot rewrite history.

Research target and motion KPI/loss windows use `America/Los_Angeles` civil
dates. Live retains its existing breaker and weekly clocks unchanged.
Settlement and lead bucketing retain each station's fixed-standard clock. These
three clocks use separate named helpers and explicit DST-boundary tests; a
shared convenience helper must not silently shift live behavior.

The Strategy Lab research headline refers only to `research-target`. It shows:

- today's realized P&L, $50 target, remaining amount, and hit/miss state;
- observed days, hit count, attainment rate, and zero-activity days;
- mean and median daily realized P&L, p25/p75, standard deviation, and
  day-clustered confidence interval;
- maximum drawdown and log growth;
- independent city-target days and resolution days;
- same-day versus day-ahead results;
- maker/taker, fee, slippage, fill, partial-fill, expiry, and exit breakdowns;
- target feasibility from the current opportunity set;
- an explicit statement that a hard research objective is not a guaranteed
  return.

Motion receives a separate card and ledger labeled “excluded from target and
live readiness.” Live retains its existing card, policy, and readiness verdict.

Every summary and Strategy Lab path uses the logical projection for counts and
rows. Partial close lots contribute money on their actual Pacific realization
date but do not create extra decisions, wins, losses, or resolved positions.

## 7. Google Weather runtime architecture

### 7.1 City and settlement alignment

The repository contains 15 cities total: SFO plus 14 others. All Google requests
use the settlement-station coordinates in `CityConfig`. Hourly timestamps are
bucketed into each station's fixed-standard climate day, never a civil-DST day
or a Pacific default. A complete forecast daily high requires all 24 expected
fixed-standard hours. Partial same-day coverage is “remaining heat,” not a
complete daily high.

Google daily forecasts remain secondary context because their 7am-to-7am civil
interval does not match the prediction market's midnight-to-midnight
fixed-standard settlement day.

### 7.2 Storage boundaries

Use two SQLite stores:

1. Permanent `weather.db` stores request/reservation accounting and non-Google
   forecast baselines, but no raw Google values.
2. `/run/weatheredge/google_runtime.db` stores minimally parsed Google runtime
   content on tmpfs. It is never backed up, synced, committed, or published.

The permanent usage ledger records reservation, billing month/day, city,
station, endpoint, page, status, billable-event count, and sanitized error kind.
Admission uses `BEGIN IMMEDIATE`, counting consumed and outstanding reserved
events so concurrent processes cannot overspend. A reservation may be released
only before HTTP dispatch. At dispatch it becomes consumed; timeout, transport
ambiguity, HTTP error, or parse failure remains counted as one event. An unknown
billing outcome remains consumed unless it is reconciled against authoritative
provider usage.

The runtime store has city-aware hourly, daily, current-condition, and derived
daily-high tables. It stores only fields needed by the forecast path. It never
stores response JSON, page tokens, full request URLs, API keys, or unused
descriptions.

Maximum lifetimes are enforced per row:

- current conditions: one hour;
- hourly forecasts: one hour;
- today's daily forecast: 30 days;
- future daily forecasts: 24 hours;
- hourly-derived or composite output: the earliest expiry of its constituents.

All reads filter `expires_at > now`. A purge service deletes at the next expiry,
and every producer/consumer performs a startup purge. Configuration may shorten
but never extend these limits.

Permanent prediction-training tables, LSTM/EMOS inputs, adaptive source weights,
source MOS, and residual de-bias exclude Google fields. Versioned derived
decision evidence may outlive the raw runtime TTL so the fixed research
challenger can be scored after settlement. It contains only baseline and
Google-conditioned challenger mean, sigma, bracket probabilities, action,
policy fingerprint, issued timestamp, and target date. It never contains raw
Google fields, responses, exact Google high, or exact disagreement gap.

### 7.3 Runtime forecast behavior

SFO keeps priority and the currently served live forecast path byte-compatible
during the compatibility migration. The new non-Google baseline and fixed
Google runtime adapter dual-run as research shadow output first. Google weight
learning is not expanded to other cities. Removing legacy Google-dependent
learning from the served SFO path requires paired shadow evidence and the normal
live-promotion gate; this project does not silently change the live forecast to
satisfy an architectural cleanup. The new path learns only from a separately
archived non-Google baseline.

For the other 14 cities, every eligible ingestion produces two parallel research
outputs: the unchanged served EMOS baseline and a versioned, fixed
Google-conditioned challenger. Live consumes only the baseline. Research may
consume the challenger under its explicit policy:

- compute the Google hourly maximum over the exact settlement day;
- compare it with the permanent EMOS mean in memory;
- let `gap = google_hourly_high - baseline_mu`;
- when `abs(gap) < 7F`, set
  `challenger_mu = baseline_mu + clamp(0.15 * gap, -1.5F, +1.5F)` and retain
  `challenger_sigma = baseline_sigma`;
- when `abs(gap) >= 7F`, emit the challenger action
  `external_runtime_corroboration_block` rather than a tradeable probability;
- derive bracket probabilities from the fixed challenger mean and unchanged
  sigma in memory, then persist only the derived evidence defined above;
- stale, unavailable, incomplete, or budget-blocked Google data fails neutral
  to the current EMOS path;
- Google never changes EMOS parameters, training rows, or final truth.

The 15%/1.5F adjustment is predeclared rather than fitted from archived Google
values. It is a research challenger, not a served default, and is compared with
the simultaneous non-Google baseline before any live proposal.

### 7.4 Event budget and schedule

Hourly pagination has a strict maximum of three 24-hour pages. Underfilled
coverage is marked incomplete instead of chasing extra pages.

Base maximum for a 31-day month:

- SFO: 38 refreshes/day x 5 events = 190/day, 5,890/month;
- non-SFO: 14 once-daily batches x 4 events = 56/day, 1,736/month;
- total: 246/day, 7,626/month.

The hard internal caps remain 260/day and 8,000/month. A 7,800/month scheduling
ceiling reserves 200 events for retries and manual operations. SFO capacity is
reserved first. Extra non-SFO refreshes, when affordable, are prioritized by
active exposure, soonest close, oldest corroboration, and market volume, never
by post-hoc Google accuracy.

### 7.5 Publication and attribution

TTL-bound Google content cannot enter Git-published JSON, Git history, deployment
archives, or backups. If live Google detail is displayed, it comes from a
TTL-capped runtime endpoint and is visibly separated and attributed. Direct
values use “Source: Includes weather data from Google”; transformed content uses
“Includes data from Google Maps.” Static methodology attribution does not
replace adjacent attribution for displayed live content.

Legacy Google-bearing rows and artifacts are not deleted automatically. New code
must stop relying on expired legacy values and produce an explicit remediation
inventory. Any material historical deletion remains a separate approved action.

## 8. Forecast tuning and promotion

This week's trades define hypotheses, not fitted parameters. Add a research-only
chronological runner with these rules:

1. Split by complete city-target settlement days; all correlated brackets for
   the same city and target stay in one fold.
2. A training label must have settled before the test decision timestamp.
3. Apply an embargo at fold boundaries.
4. Fit station/lead-horizon calibration only from preceding folds, with
   shrinkage or pooled fallback when a city lacks evidence.
5. Replay point-in-time quotes and public trade tape through the exact exec-v3
   fill, fee, partial-fill, and exit engine.
6. Pair active and challenger results on the same city-target days.
7. Evaluate after-fee P&L/ROI, log growth, mean/median daily P&L, $50 attainment,
   Brier/CRPS/calibration, drawdown, fill ratio, and executable capacity.
8. Correct for multiple candidate comparisons and report practical effect sizes
   with confidence intervals.

Promotion into `research-target` requires at least 30 independent complete days,
enough filled logical positions, positive day-clustered after-fee lower
confidence bound, positive log growth, and no material calibration or drawdown
regression. Motion data may propose a challenger but motion P&L is never target
promotion evidence.

Promotion from research-target to live is a separate process. It requires the
existing chronological live readiness checklist, a new independent cohort, and
explicit operator authorization. Nothing in this design bypasses that gate.

## 9. Reliability defects included in scope

The deep audit already identified these P1 defects:

1. Strategy Lab paper cards and the CLI summary still count partial-exit lots as
   decisions. A partial child can appear as a win before the root is terminal.
2. Calibration snapshot deduplication omits `risk_profile`; live and research
   snapshots for the same market and side can overwrite one another according to
   insertion order.
3. A new inside-spread maker order inherits queue-ahead depth from the old lower
   bid. Depth at a worse price is behind the new order, so this understates fills
   and biases execution evaluation.

The implementation must add focused red tests for each reproduction before the
fix. The remaining audit may add only evidence-backed performance, settlement,
forecast, execution, risk, timer, or publication defects. Unrelated refactors
are out of scope.

## 10. Failure handling

- Invalid logical groups fail closed for counts, readiness, and promotion while
  preserving raw-money reconciliation and emitting an integrity finding.
- Unknown research sleeve/account identity fails closed for new writes and is
  excluded from new target KPIs.
- A failure in one Google city or endpoint does not abort other cities.
- Google expiry, missing hours, budget denial, or parsing failure preserves the
  non-Google baseline and records a sanitized status.
- API errors never log keys or full URLs.
- SQLite writes are transactional; cache files and public artifacts remain
  atomic.
- An impossible $50 opportunity set reports `target_feasible=false`; it does not
  relax controls.
- Research failure cannot pause, reserve, debit, or mutate live.

## 11. Verification requirements

Implementation uses test-driven development and subagent specification/quality
reviews. Required verification includes:

### Accounting and accounts

- target/motion/live cash, P&L, reservations, exposure, and pause isolation;
- atomic admission under concurrent scans and account-scoped active-order
  uniqueness;
- fixed $50 Pacific-day objective and zero-activity days;
- motion excluded from target and live readiness;
- partial fills/closes reconcile lot money but count one logical decision;
- mixed account/sleeve/policy children invalidate a logical group;
- target and motion may intentionally hold the same market while duplicates
  within one sleeve are rejected.

### Research execution

- target blocks same-day and negative lower-bound edge;
- motion retains eligible same-day, removes sampling, and stays one contract;
- unlimited terminal re-entry with active duplicate prevention and scan
  idempotency;
- scenario-based city/region/aggregate risk;
- exact maker/taker fees, depth, tape conservation, queue priority, partials,
  expiry, and settlement;
- target locks after $50 while motion continues.

### Google

- all 15 city coordinates, station identities, and fixed-standard DST boundary
  cases;
- active exactly before expiry and unavailable exactly at expiry;
- physical purge and reboot-safe tmpfs behavior;
- today/future daily TTL distinction;
- three-page hourly ceiling and 24-hour completeness;
- 246/day and 7,626/31-day base-budget proofs;
- concurrent reservation safety and SFO priority;
- pre-dispatch cancellation versus post-dispatch timeout/ambiguity accounting;
- no raw Google content, URL, key, or page token in permanent stores or Git
  artifacts;
- durable derived challenger evidence contains only the approved versioned
  outputs and remains scoreable after raw-content expiry;
- changing Google runtime values cannot alter trained weights, EMOS/LSTM
  artifacts, or historical scorecards;
- publication rejection of TTL-bound fields and correct attribution.

### Tuning and system integrity

- no future labels or look-ahead joins;
- city-target grouped walk-forward folds and embargo;
- exact point-in-time execution replay;
- profile-aware calibration deduplication;
- inside-spread queue initialization;
- full Python and frontend suites;
- `bun run build`;
- local Strategy Lab artifact generation;
- browser verification of desktop and mobile dashboard state;
- AWS canary/runtime checks before any deployment is declared healthy.

## 12. Rollout sequence

1. Land and reverify logical accounting; repair remaining lot-count consumers.
2. Fix profile-aware calibration and inside-spread queue defects.
3. Add research account/sleeve schema and immutable audit identity.
4. Add target/motion allocation, risk, execution, and daily objective reporting.
5. Add the transactional Google event ledger and tmpfs runtime store.
6. Dual-run SFO baseline-plus-runtime shadow reads without changing its served
   live forecast or trading policy; propose removal of legacy Google-dependent
   learning only after paired promotion evidence.
7. Enable other cities at corroboration-only and verify budget/TTL behavior.
8. Add the chronological research tuner and evaluate challengers.
9. Complete full regression, reconciliation, static, build, browser, and runtime
   checks.
10. Deploy only after local evidence passes; keep all real-money order placement
    disabled pending the independent live readiness gate and operator action.

## 13. Explicit non-goals

- guaranteeing a 5% daily return;
- sizing backward from the $50 shortfall;
- Martingale or loss-chasing behavior;
- fake, unverified, or impossible paper fills;
- using motion P&L to claim target or live readiness;
- training LSTM/EMOS or learned source weights on Google content;
- changing live gates or enabling real orders;
- deleting legacy Google data without a separate destructive-action approval;
- unrelated architectural cleanup.
