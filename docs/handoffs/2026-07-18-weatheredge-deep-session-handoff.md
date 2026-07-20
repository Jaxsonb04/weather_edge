# WeatherEdge Deep-Session Handoff

Last updated: 2026-07-18 17:46 PDT

Status: implementation in progress; not deployed

Implementation branch: `codex/weatheredge-deep-session-impl`

Last implementation commit: `db29b1fc13eaf1915f384f181f1bc0cb6aa50b3b`

Production/source baseline: `origin/main` at `803bfad5f77b3a703d31a52471b5f1b4389dafeb`

This document is the restart point for another engineer or coding agent. It
captures the user goal, the production-safety rules, the forensic findings,
the code already implemented, the verification evidence, the interrupted
reviews, and the exact work still required before deployment.

## 1. Executive status

WeatherEdge has received a large reliability and research-account overhaul in
an isolated worktree. The branch is clean and contains 71 commits over
`origin/main`, touching 80 files with roughly 31,351 additions and 1,100
deletions. None of this session's code has been deployed.

The following foundations are implemented:

- The four visually duplicated Phoenix rows are now represented as one logical
  trade with four execution lots.
- Paper accounting, settlement, replay, maker-queue simulation, and readiness
  evidence fail closed when identity or money evidence is malformed.
- The live paper candidate remains unchanged and fingerprint-pinned.
- Research is split into isolated target and motion accounts.
- The target account has a fixed research objective of 5% of the original
  $1,000 reference equity, or $50 per Pacific calendar day. It is an evidence
  KPI, not a promised or expected return.
- The motion account remains frequent and experimental so it can generate
  evidence without contaminating the target KPI or live readiness.
- The Strategy Lab frontend renders live, target, and motion separately.
- Google Weather request accounting and a TTL-bound runtime SQLite store are
  implemented with event budgets, atomic generations, purge-resistant
  watermarks, schema migration, and secret/raw-data boundaries.
- The production nightly dataset backfill now closes SQLite handles and has
  bounded, code-aware, globally budgeted lock retries.

The following work is still unfinished:

- Three independent reviews were interrupted when this handoff was requested:
  final Google runtime-store review, final backfill review, and frontend Task 8
  review.
- Google city-aware fetching, challenger generation, orchestration,
  systemd/runtime operations, and final verification are not implemented yet.
- Chronological leakage-resistant research tuning and promotion tasks are not
  implemented yet.
- Browser verification, full repository verification, landing on clean
  `main`, AWS deployment, public deployment, and post-deploy canaries remain.

Do not call this production-ready until every item in Section 10 is complete.

## 2. User goal and non-negotiable constraints

The user asked for one uninterrupted, subagent-driven session that:

1. Finds and fixes the four duplicate trades.
2. Reviews this week's live and research trades, identifies what worked and
   failed, and tunes the system using that evidence.
3. Keeps the existing live-money candidate unchanged and bug-free while
   accelerating paper evidence toward real-money readiness.
4. Gives the research target account a hard $50/day KPI based on the original
   $1,000 equity, while allowing a separate high-motion research account to
   trade frequently.
5. Adds Google Weather data for all 15 cities under the free-event budget,
   stores it in a database, and uses it only as runtime forecast evidence.
6. Audits the broader codebase for performance-affecting defects.
7. Finishes with a complete AWS and public-site deployment.

The following constraints are mandatory:

- Real-money orders must remain disabled. Keep
  `SFO_LIVE_TRADING_ENABLED=0` and dry-run enabled.
- The existing live strategy must not be retuned from this short research
  window. Its pinned fingerprints are:
  - limit entry: `a965c8280aca2b3621f0c312`
  - market entry: `73b10240c1c00a8937b5314f`
- The public site must say "prediction market", never the exchange name.
- The $50/day target must never be presented as guaranteed, expected, or a
  readiness shortcut.
- Research evidence must never contribute to live P&L, live readiness, or
  live calibration.
- Raw Google content is TTL-bound under `/run/weatheredge`; permanent storage
  may contain request accounting and derived/versioned challenger evidence,
  not raw responses, URLs, API keys, conditions, or raw Google gaps.
- Local ignored runtime files are not production authority. AWS runtime data
  under `/opt/weatheredge` is authoritative after deployment and refresh.
- Preserve human/project-bot authorship. Do not add assistant attribution or
  co-author trailers.

## 3. Workspace and branch state

Start a shell in the implementation worktree, then derive portable paths:

```bash
weatheredge_worktree="$(git rev-parse --show-toplevel)"
weatheredge_repo="$(cd "$weatheredge_worktree/../.." && pwd)"
weatheredge_venv="$weatheredge_repo/.venv-dev"
```

Primary repository:

```text
$weatheredge_repo
```

Implementation worktree:

```text
$weatheredge_worktree
```

Implementation branch state before adding this handoff:

```text
codex/weatheredge-deep-session-impl
last code commit db29b1fc13eaf1915f384f181f1bc0cb6aa50b3b
working tree clean before this handoff document was added
no upstream branch pushed
```

Local Python test environment:

```text
$weatheredge_venv
```

The ignored EC2 operator configuration exists only in the primary repository:

```text
$weatheredge_repo/.local/ec2.env
```

Never copy its values into logs, commits, or handoff text.

The implementation branch descends from local `main` planning commits and can
eventually fast-forward `main`, but deployment scripts reject every branch
except a clean `main`.

## 4. Plans and design documents

Read these before changing architecture:

- [Daily research, Google runtime, and reliability design](../superpowers/specs/2026-07-17-daily-research-google-runtime-and-reliability-design.md)
- [Accounting, calibration, and execution reliability plan](../superpowers/plans/2026-07-17-accounting-calibration-execution-reliability.md)
- [Research target and motion accounts plan](../superpowers/plans/2026-07-17-research-target-and-motion-accounts.md)
- [Multi-city Google runtime weather plan](../superpowers/plans/2026-07-17-multicity-google-runtime-weather.md)
- [Chronological research tuning and promotion plan](../superpowers/plans/2026-07-17-chronological-research-tuning-and-promotion.md)
- [AWS deployment runbook](../aws_deployment.md)
- [Operational runbook](../operational_runbook.md)

Plan checkboxes were not kept synchronized during subagent execution. Treat the
status in this handoff as authoritative, then update the plans if desired.

## 5. What the trade forensics found

### 5.1 The four duplicate Phoenix trades

The visible rows were not four independent strategy decisions. They were one
logical root order for eight contracts, closed as four two-contract lots. The
old Strategy Lab ledger rendered each execution lot as a full trade.

The audit reconciled:

- 512 raw non-rejected paper rows
- 506 logical positions
- 205 terminal decisions
- exact P&L agreement after grouping

The correction keeps lot-level accounting for auditability while reporting
decision-level counts and one logical trade to users.

### 5.2 This week's live candidate

The forensic week slice showed:

- 11 logical decisions
- 9 wins and 2 losses
- +$16.575 realized P&L
- +7.02% resolved-capital ROI
- all qualifying decisions were day-ahead

The all-history live attribution was approximately +$14.09 at the audit point.

What went well:

- Day-ahead opportunities were profitable in the reviewed slice.
- Strict lower-bound edge, source agreement, and liquidity filters remained
  selective.
- The live candidate produced positive realized evidence without same-day
  exposure.

What not to infer:

- Eleven decisions are not enough to retune or promote the live strategy.
- The 5% daily research KPI is not a live-return expectation.
- The code still requires chronological replay, breadth, forecast skill, and
  calibration gates before real-money readiness.

### 5.3 This week's research profile

The forensic week slice showed:

- 19 logical decisions
- 9 wins and 10 losses
- -$25.8856 realized P&L
- day-ahead subset: +$8.0129
- same-day subset: -$33.8985

The all-history research attribution was approximately -$114.0459 at the audit
point.

What went well:

- Day-ahead research evidence was positive in this small slice.
- Frequent scanning produced useful failure evidence and exposed execution and
  accounting defects quickly.

What went wrong:

- Same-day entries dominated the research losses.
- One loose research profile mixed KPI-seeking behavior with exploratory
  motion, making performance and readiness interpretation misleading.
- The previous UI and summaries sometimes collapsed research identities,
  calibration, exits, and lots.

Implemented response:

- `research-target` is day-ahead-only and isolated around the $50 evidence KPI.
- `research-motion` remains frequent and may study same-day opportunities, but
  is excluded from the target KPI and readiness.
- No day in the reviewed data reached $50. The UI and reports say so honestly.
- Promotion requires chronological paired evidence, not this descriptive split.

## 6. Work completed

### 6.1 Reliability, accounting, and replay

Implemented and reviewed:

- Logical-position grouping across root orders and partial lots.
- Decision-level P&L/count reporting while retaining lot-level audit rows.
- Invalid-group quarantine so malformed positions cannot contaminate money,
  ROI, hit rate, or readiness.
- Profile-specific calibration sampling.
- Maker queue consumption at the posted price level.
- `exec-v4` evidence versioning and partial `exec-v3` cutover freeze.
- Immediate crossing-limit replay and exact quantity conservation.
- Taker/current-generation evidence verification.
- Fail-closed chronological restatement for malformed, missing, or ambiguous
  evidence.
- Settlement verification and logical decision gating.
- An adversarial exec-v4 fixture that temporarily removes and then restores the
  canonical account-scoped open-position guard when constructing impossible
  corrupt history.

Representative commits:

```text
08bc77a3 feat: project execution lots into logical positions
e91a49f8 fix: summarize logical paper positions
2ef34541 fix: fail closed on malformed paper exits
9d47fe0f fix: isolate profile calibration samples
84fabb13 fix: model maker queue at the posted price
a71f35f5 fix: version corrected maker semantics
00801578 fix: replay maker evidence during restatement
40194a4c Reconcile maker decisions to immutable evidence
c78d238f Verify all current execution evidence
7389d971 Conserve immediate execution quantities
435098a8 test: preserve guard in corrupt replay fixture
```

### 6.2 Research target and motion accounts

Implemented:

- Immutable research sleeve policies and fingerprints.
- Separate accounts for target and motion.
- Account-scoped open-position guard.
- Atomic order, evidence, ledger, and approval admission.
- Idempotent abandoned-admission recovery.
- Strict identity agreement across account, sleeve, policy version, and policy
  fingerprint.
- Target and motion opportunity allocation.
- Target lock after the fixed daily objective is attained; motion continues.
- Depth-aware downsizing and sleeve-specific gates.
- One scan context and immutable plan snapshot per scan, including empty scans.
- Separate default-off AWS paper-placement flags:
  - `PAPER_PLACE_LIVE`
  - `PAPER_PLACE_RESEARCH_TARGET`
  - `PAPER_PLACE_RESEARCH_MOTION`
- Whole-profile CSV validation before any scan dispatch.
- Research flags cannot enable real-money execution.

Representative commits:

```text
5e1afe81 feat: define isolated research sleeve policies
0c2409b6 feat: add research account identity schema
a47469c0 feat: admit research orders atomically
ad39073b fix: validate research lot identity
d6823643 feat: allocate target and motion research
56dc2b4b feat: run target and motion research books
64a0e021 fix: make research evidence admission atomic
9cc896ce feat: isolate research placement controls
87ce2ba8 fix: normalize paper scan profile aliases
a8397675 fix: prevalidate paper scan profiles
```

### 6.3 Daily target evidence and research reporting

Implemented and independently reviewed READY:

- Exact active-policy validation for $1,000 reference equity, 5% target return,
  and $50 target P&L.
- Pacific-calendar daily rows, zero-P&L days, bounded 30-day default window,
  and a maximum 365-day request window.
- Mean, median, distribution, drawdown, log-growth, breadth, lead, fee, fill,
  expiry, and feasibility evidence.
- Target-only new-risk lock.
- Canonical allocator feasibility persisted for empty and non-empty scans.
- Immutable update/delete triggers for plan snapshots.
- Exact take-profit, stop-loss, break-even, settlement, and expired-unfilled
  exit categories.
- Resolution-day breadth based on lots, including lots from one logical position
  resolving on different Pacific days.
- Deterministic migration of only exact legacy goal rows; ambiguous rows fail
  closed.
- Account/lifecycle indexes and Pacific-bounded queries instead of full-history
  scans inside write transactions.
- Contradictory, stale, crossed, or partial research identities are classified
  as unknown and excluded from every aggregate.
- Target and motion calibration/rescore buckets remain separate.

Key commits:

```text
aa42ad23 feat: report the daily research target
340f4c09 fix: harden research goal evidence
9c2c0902 fix: isolate research evidence reporting
```

Independent final evidence:

- 205 targeted Task 7 tests passed.
- Full trading suite: 1,535 passed.
- `compileall` and `git diff --check` passed.
- Adversarial migration rollback preserved schema, data, and immutable triggers.
- `EXPLAIN QUERY PLAN` used all intended account/lifecycle/parent indexes.

### 6.4 Separate-book frontend

Implemented in `db29b1fc`:

- Tolerant optional `ResearchDailyTarget` parsing.
- Canonical ordering: live, research-target, research-motion.
- Legacy research appears only when neither canonical sleeve exists.
- No synthetic or phantom research book.
- Live remains first and retains live weekly-goal language.
- Target shows fixed $50 progress, realized and remaining P&L, mean and median,
  observed/hit/independent days, feasibility true/false/unknown, and lock state.
- Target copy explicitly says the objective is not guaranteed.
- Motion is labeled experimental/high activity and excluded from the daily
  target and live readiness.
- Separate accounts and ledgers are visible.
- Long IDs wrap; progress uses ARIA; objective dates are localized.
- Public modified copy contains no forbidden exchange name.

Implementer verification:

- 34 focused frontend tests passed.
- Full frontend suite: 119 passed.
- `bun run lint` passed.
- `bun run build` passed.
- `git diff --check` passed.

The independent Task 8 code review was started but interrupted before a verdict.
Browser verification was intentionally deferred.

### 6.5 Google Weather budgets and permanent request accounting

Implemented:

- Canonical 15-city/station identity.
- Production runtime path: `/run/weatheredge/google_runtime.db`.
- TTLs:
  - hourly: 1 hour
  - current: 1 hour
  - current-day daily forecast: 30 days
  - future-day daily forecast: 24 hours
- Strict hourly page ceiling of 3; zero pages means no hourly calls.
- Event limits:
  - 260 per Pacific day
  - 8,000 per month
  - 7,800 soft monthly ceiling
- Planned steady-state load: 246 events/day, approximately 7,626 events in a
  31-day month.
- `BEGIN IMMEDIATE` reservation before dispatch.
- Pacific billing dates.
- Reservation, dispatch, completion, cancellation, and billable-error states.
- Exclusive dispatch ownership and UUIDv4 request IDs.
- Permanent ledger stores accounting metadata only and rejects secret/raw
  columns.
- Secrets and full URLs are redacted from errors.

Key commits:

```text
b368f22d feat: define Google runtime limits
46c208fc fix: enforce Google hourly page ceiling
5ed18af7 fix: honor disabled Google hourly pagination
eef85fd9 feat: account for Google events transactionally
d06b339a fix: harden Google event lifecycle
```

### 6.6 Google runtime content store

Implemented through `17b45447`:

- Raw Google content lives only in the runtime database under the protected
  `/run/weatheredge` tree.
- Production root and file must be application-owned, non-symlinked, and not
  group/world writable.
- Pre/post-open inode checks detect path substitution.
- Every SQLite handle closes deterministically, including setup failures.
- Finite positive timeout validation occurs before opening SQLite.
- Hourly, daily, and current refreshes replace a complete generation in one
  transaction.
- Readers see one coherent newest generation, never a partial refresh.
- Scope-keyed generation watermarks survive TTL purge, so delayed older
  responses cannot resurrect.
- Same-issued corrections remove absent rows.
- Concurrent generation replacements preserve newest-issue ordering.
- Runtime high is derived from validated hourly constituents.
- Completeness requires exactly 24 unique fixed-standard station-day hours.
- High temperature and expiry are derived from constituents; contradictory DB
  state is rejected.
- Disposable runtime schema v2 migrates old weak runtime tables while
  preserving the permanent usage ledger.
- Missing runtime indexes self-repair.
- Runtime tmpfs reset intentionally resets the watermark.

Key commits:

```text
c88e0478 feat: store expiring Google runtime weather
66c95b52 fix: keep Google runtime generations coherent
698fefbf fix: close Google weather sqlite handles
17b45447 fix: harden Google runtime generations
```

Implementer verification:

- Runtime-store suite: 76 passed.
- Full forecaster suite: 232 passed.
- `compileall` and `git diff --check` passed.

The final independent Google Task 3 review was started but interrupted before a
verdict. Resume that review before implementing Task 4.

### 6.7 Nightly dataset backfill lock repair

Production evidence:

- `sfo-dataset-backfill.service` started at 02:25:03 PDT.
- `open-meteo-previous-runs` opened the paper DB at 02:26:04.
- It failed with `database is locked` at 02:26:51.
- No WeatherEdge paper, forecaster, or Strategy Lab systemd service start was
  present in the 02:20-02:28 journal window.
- The original theory that a recurring paper timer owned the lock was rejected.
- A local reproduction proved `DatasetStore` leaked 41 open DB handles across
  20 start/finish cycles until garbage collection.

Implemented:

- Deterministic DatasetStore commit/rollback/close behavior.
- SQLite lock errors map to temporary-failure exit 75 using base
  `SQLITE_BUSY`/`SQLITE_LOCKED` codes, including extended codes.
- Tightly anchored legacy fallback accepts exact lock messages or a `: `
  suffix; deceptive text such as `no such column: locked` is non-retryable.
- Canonical retry attempts and finite delay validation.
- One monotonic lock-retry budget shared across every dataset source.
- Maximum retry budget: 600 seconds.
- The 1,800-second systemd timeout reserves 1,200 seconds for research and four
  post-processing steps.
- Retry attempts receive one absolute deadline and clamp SQLite busy timeout to
  remaining time.
- Budget exhaustion fails the source honestly, continues the batch, runs
  research once, and exits nonzero.
- The fix does not stop, disable, or alter timer schedules.

Key commits:

```text
ce5a2a15 fix: recover nightly backfill sqlite locks
2787fe04 fix: validate dataset lock retries
9852b4bd fix: bound dataset lock retries globally
```

Implementer verification:

- 210 focused backfill/deploy tests passed.
- `bash -n` and `git diff --check` passed.

The final independent review reached a green 111-test checkpoint with manual
rollback, error-code, legacy-message, and deadline-clamp probes, but was
interrupted before its formal verdict.

## 7. Review loops and defects caught during the session

The initial implementations were not accepted on implementer tests alone.
Independent reviewers found and drove fixes for:

- Fixed-$50 rows that accepted internally consistent but wrong $2,000/$100
  policy evidence.
- Target and motion evidence collapsed back into raw `research` calibration.
- Feasibility not persisted for empty scans.
- Generic monitor exits instead of audited exit reasons.
- Resolution breadth counted only the latest lot date.
- Unbounded daily report generation.
- Permissive account-or-sleeve identity routing.
- Mutable research plan snapshots.
- Legacy goal schema migration that could brick frozen rows.
- Unindexed full-history scans under `BEGIN IMMEDIATE`.
- Google delayed-old generation fallback.
- Purge resurrecting stale Google issues after deleting the newest watermark.
- Row-by-row Google writes exposing partial generations.
- Weak old runtime schema surviving `CREATE TABLE IF NOT EXISTS`.
- SQLite connection-context managers that committed but did not close.
- DatasetStore DB handles left open until garbage collection.
- Bash octal parsing of retry values such as `08`.
- Loose substring lock classification.
- Unbounded retry delay and accepted retry settings exceeding systemd timeout.

This review history matters. Continue using implementer, spec reviewer, and
quality reviewer loops for each remaining task.

## 8. Verification already completed

The strongest current evidence on committed HEAD is:

| Area | Evidence |
| --- | --- |
| Trading/backend | 1,535 tests passed in the final Task 7 review |
| Task 7 focused | 205 passed; adversarial migration/index/identity probes passed |
| Forecaster | 232 passed after Google runtime hardening |
| Google runtime store | 76 passed |
| Frontend | 119 passed |
| Frontend focused | 34 passed |
| Backfill/deploy | 210 passed |
| Frontend lint | passed |
| Frontend production build | passed |
| Python compile checks | passed for reviewed scopes |
| Shell syntax | passed for changed deploy scripts |
| Git whitespace checks | passed for committed changes |

These were scope-specific runs at clean commit boundaries. A single complete
repository verification run at final HEAD has not been performed.

## 9. Production preflight observed earlier in the session

This was a read-only snapshot and must be repeated before deployment:

- EC2 SSH was reachable.
- Host: Ubuntu ARM64, Pacific timezone.
- Disk: about 56% used with about 17 GB free.
- Memory: about 3.3 GiB available; swap healthy.
- Nine WeatherEdge timers were enabled and active.
- Safety state was correct:
  - `SFO_LIVE_TRADING_ENABLED=0`
  - dry-run enabled
  - new paper-placement flags absent, therefore default-off
- Forecast DB `quick_check` and foreign-key checks passed.
- Runtime paths, locks, webdist, build, and manifests existed.
- Public/source manifest still pointed to old source
  `803bfad5f77b3a703d31a52471b5f1b4389dafeb` and snapshot
  `234e678fd0577d87aafbdb4d`.
- Forecast/public artifacts were minutes old at the observation point.
- Source cache on EC2 was clean.
- The nightly dataset backfill had failed on the SQLite lock described above.
- The 2.94 GB paper DB still required the deployment script's quiesced
  backup/restore integrity gate; a bounded live quick-check was not treated as
  enough evidence.

The backfill fix exists only locally. Production still has the old failure
behavior until deployment.

## 10. Work still required, in order

### 10.1 Close the three interrupted reviews

The user's handoff request interrupted these read-only subagent reviews:

1. Google runtime Task 3 final spec/quality review.
2. Frontend Task 8 final spec/quality review.
3. Dataset backfill final spec/quality review.

Resume or recreate them. Do not modify code during review. If any verdict is
`NOT READY`, use a new bounded TDD repair and cross-review it again.

Minimum review targets:

- Google: `c88e0478`, `66c95b52`, `698fefbf`, `17b45447`.
- Frontend: `db29b1fc` against Task 8 and the current backend artifact contract.
- Backfill: `ce5a2a15`, `2787fe04`, `9852b4bd`.

### 10.2 Complete Google Weather Tasks 4-9

Task 4, city-aware fetching:

- Implement explicit 15-city request APIs in `forecaster/google_api.py`.
- Reserve before each page, mark dispatched immediately before transport, then
  complete or classify failure.
- Use at most three hourly pages.
- Use each city's coordinates, station, civil timezone metadata, and
  fixed-standard timezone for settlement-day bucketing.
- Use the new atomic generation replacement APIs.
- Test all cities, pagination, underfilled pages, independent endpoint failures,
  and secret-safe exceptions.

Task 5, station-day highs and challengers:

- Derive a high only from a complete fixed-standard 24-hour station day.
- Implement the fixed research challenger:
  - 15% Google share
  - adjustment capped at +/-1.5 F
  - baseline sigma unchanged
- Missing/incomplete/external-corroboration-block data must fail closed.

Task 6, budget-safe orchestration:

- Build/archive the non-Google baseline first.
- Refresh all 15 cities within 260/day, 8,000/month, and 7,800 soft ceiling.
- Keep SFO compatibility output unchanged.
- Stop writing raw Google values to the old JSON cache.
- Purge expired runtime content.

Task 7, paired evidence:

- Persist paired baseline and derived Google challenger probabilities only.
- Never persist raw Google high, gap, body, URL, key, token, or conditions.
- Prove the served live SFO forecast and live strategy fingerprint are bitwise
  unchanged in shadow mode.

Task 8, runtime operations and attribution:

- Add `/run/weatheredge` creation/ownership and cleanup to systemd/tmpfiles.
- Add runtime endpoint attribution adjacent to Google-derived data.
- Bound response cache lifetime to remaining row TTL.
- Fail publication if Git-published artifacts contain raw Google fields.

Task 9, verification and staged enablement:

- Run Google, settlement, full Python, frontend, and build suites.
- Deploy with challenger consumption disabled.
- Verify SFO output equality and event-ledger counts.
- Enable non-SFO research challenger collection only after equality passes.
- Keep real-money execution disabled.

### 10.3 Complete chronological research tuning Tasks 1-7

No task in the chronological plan has been implemented yet.

Required sequence:

1. Persist source-neutral scan contexts and immutable experiment declarations.
2. Build leakage-resistant chronological folds by Pacific day/city-target.
3. Evaluate predeclared fixed Gaussian and Google-conditioned challengers.
4. Replay candidates through exact `exec-v3` mechanics and fees.
5. Compute paired daily and capacity evidence.
6. Gate target-paper promotion and repeated experiments.
7. Publish and operate the evidence loop.

Required metrics include paired baseline/challenger deltas for daily realized
P&L, mean, median, standard deviation, positive-day rate, $50 hit rate,
after-fee ROI, log growth/day, drawdown, turnover, fills, rejection reasons,
contracts, dollars at risk, and target/motion capacity.

Do not choose a challenger from the same days used to report its performance.
Do not promote a model from descriptive same-week results.

### 10.4 Browser-verify the frontend

The repository instructions require browser verification, not static review.

Before browser work:

```bash
cd "$weatheredge_worktree"
python3 scripts/clear_local_runtime_state.py --confirm
bun run build
```

Serve `dist/`, then use the `agent-browser` skill. Before browser commands run:

```bash
agent-browser skills get core --full
```

Verify:

- 1440px desktop and 390px mobile.
- Live, target, and motion selectors by keyboard and pointer.
- DOM state after every selection.
- No horizontal overflow.
- Long account/policy IDs wrap.
- Target progress, feasibility false, feasibility unknown, and lock state.
- Motion exclusion language.
- Legacy fixture fallback with no phantom profile.
- Screenshots for both sizes.
- Public copy contains no forbidden exchange name.

### 10.5 Run final local verification

From the implementation worktree:

```bash
"$weatheredge_venv/bin/pytest" -q
bun test
bun run lint
bun run build
python3 -m compileall -q forecaster trading/sfo_kalshi_quant
```

Also run focused shell syntax checks on every changed `.sh` file and
`git diff --check`.

Required invariant tests before landing:

```bash
"$weatheredge_venv/bin/pytest" -q \
  trading/tests/test_research_sleeves.py::test_live_account_and_fingerprint_are_unchanged
```

Add or locate tests proving real-money enabled remains false/dry-run true by
default and in the rendered AWS environment.

Run a final security/static review for:

- secrets and raw Google fields in tracked artifacts
- SQL migration and trigger safety
- shell quoting and environment validation
- path/symlink handling under `/run/weatheredge`
- money reconciliation and account isolation

### 10.6 Land on clean main

The deployment script refuses non-main branches and dirty trees.

Before landing:

- Finish all reviews and repairs.
- Commit this handoff document only if it should remain in history.
- Confirm the implementation worktree is clean.
- Confirm the primary repository has no user changes that would be overwritten.
- Fast-forward local `main` to `codex/weatheredge-deep-session-impl`.
- Run the final verification again from clean `main` if paths or generated files
  differ.

Do not use destructive reset or checkout commands. Preserve all user worktrees
and unrelated branches.

### 10.7 Deploy AWS source/runtime and web app

The source deploy must run from the primary repository on clean `main`, because
the ignored operator env lives there:

```bash
cd "$weatheredge_repo"
bash trading/deploy/aws/sync_to_box.sh
```

That command must pass its authoritative S3 upload/restore and quiesced SQLite
backup/restore gates. A failure can leave the host intentionally quiesced; read
the script output and restore only the captured timer set.

After source/runtime deployment and service verification, deploy the web app:

```bash
cd "$weatheredge_repo"
bash trading/deploy/aws/deploy_web_app.sh .local/ec2.env
```

Intended paper-placement flags after migration and schema verification:

```text
PAPER_PLACE_LIVE=1
PAPER_PLACE_RESEARCH_TARGET=1
PAPER_PLACE_RESEARCH_MOTION=1
```

These enable paper placement only. Preserve:

```text
SFO_LIVE_TRADING_ENABLED=0
dry-run enabled
```

Do not enable Google challenger consumption at the same time as first deploy.
First prove SFO live-output equality and budget accounting; then enable only the
approved non-SFO research collection.

Rerun the formerly failed dataset backfill after deployment and require success.

### 10.8 Run post-deploy canaries

Do not declare completion until all canaries pass:

- Source/build/public manifests match the landed commit.
- All intended timers are enabled and active; no extra timers were enabled.
- Paper DB backup/restore, `quick_check`, and foreign keys pass while quiesced.
- Dataset backfill succeeds without leaked handles or timeout kill.
- Google daily/monthly/soft budget counts reconcile exactly.
- Google runtime DB is under `/run/weatheredge`, correctly owned, protected,
  and purges expired content.
- No raw Google content appears in tracked/public JSON or Git.
- Live fingerprints equal the pinned hashes.
- Real-money execution is disabled and dry-run remains enabled.
- Live, target, and motion accounts, P&L, open risk, and ledgers reconcile.
- Research remains excluded from live readiness.
- Public Strategy Lab shows one logical Phoenix trade, not four duplicate
  decisions.
- Public desktop and mobile views render all three books and correct target
  language.
- Artifact freshness and pipeline health are within operational thresholds.

## 11. Suggested subagent restart sequence

The user explicitly requested subagent-driven development. Use at most three
worker agents alongside the root.

Wave 1:

- Agent A: final read-only Google Task 3 review.
- Agent B: final read-only frontend Task 8 review.
- Agent C: final read-only backfill review.

Wave 2 after READY verdicts:

- Agent A: Google Task 4 implementer.
- Agent B: chronological Task 1 implementer.
- Agent C: browser/frontend verification or Task 4 independent reviewer,
  depending on timing.

For every implementation task:

1. Write RED tests.
2. Implement the smallest complete fix.
3. Run focused and broad suites.
4. Commit only that task's files.
5. Use a different agent for spec review.
6. Use another different agent for quality review.
7. Repair and repeat until READY.

The interrupted agents and their last assignments were:

- `/root/backfill_lock_fix`: final Google Task 3 review.
- `/root/google_task3_impl`: final frontend Task 8 review.
- `/root/research_task7_impl`: final backfill review.

They were interrupted by the handoff request, not by a code failure.

## 12. Quick restart commands

```bash
cd "$weatheredge_worktree"
git status --short
git log --oneline -15
git diff --stat origin/main...HEAD
```

Expected before this document is committed:

```text
branch: codex/weatheredge-deep-session-impl
code HEAD: db29b1fc
only this handoff document should be untracked/modified
```

Inspect the entire session commit stack:

```bash
git log --oneline --reverse "$(git merge-base HEAD origin/main)..HEAD"
```

Re-run current milestone suites:

```bash
"$weatheredge_venv/bin/pytest" -q trading/tests
"$weatheredge_venv/bin/pytest" -q forecaster/tests
bun test
bun run lint
bun run build
```

## 13. Completion definition

This project is complete only when:

1. All interrupted and future task reviews are READY.
2. Google Tasks 4-9 are implemented and verified.
3. Chronological tuning Tasks 1-7 are implemented and verified.
4. Desktop/mobile browser verification passes.
5. Full local Python/frontend/build/security suites pass at final HEAD.
6. The branch is landed on clean `main` without losing user work.
7. AWS source/runtime deployment passes the quiesced backup/restore gate.
8. The public web app is deployed from the same commit.
9. The formerly failed dataset backfill succeeds in production.
10. Every post-deploy canary passes.
11. Real-money execution remains disabled and dry-run remains enabled.

Until then, the active goal is unfinished.
