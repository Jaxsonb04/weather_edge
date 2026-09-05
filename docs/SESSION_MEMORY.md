# WeatherEdge Session Memory

Last updated: 2026-09-04 23:36 PDT

Last complete production verification: 2026-09-04 21:57 PDT

Last public artifact inspection: 2026-09-04 23:25 PDT (fresh)

This is the rolling cross-session handoff. Recheck AWS before making a current
operational claim; all production observations below are dated snapshots.

## Session Brief

- **Apple history and ML follow-up (September 4, 23:36 PDT snapshot):** Apple's
  historical REST capability is now actually verified for one SFO day: 24/24
  hours over the July 19, 2025 fixed-standard climate window. Returned weather
  stayed in AWS process memory. Current refresh also succeeded for 15/15 cities;
  a new counts-only compatibility probe matched 20/30 future targets. All ten
  missing baselines were Central-time lead keys after midnight and before the
  next normal refresh, not stale matched rows. Implemented history/compatibility
  diagnostics were exercised in memory; installed backend source was not
  replaced. Apple history is retrospective conditions, not proven original
  forecast vintages. Attachment 8 restricts bulk downloads and retained derived
  databases; it does not explicitly prohibit all WeatherKit machine learning.
  Apple aggregate-score/model-parameter retention still needs clarification;
  an unsent inquiry and sources are in `docs/research/2026-09-05-weatherkit-history.md`.
- **New offline ML result:** a fixed histogram-gradient residual model trained
  on 11,101 existing complete eight-member cases, using truth through June 3,
  2026. On the same 2,658 exploratory cases as the earlier pilot, MAE fell from
  1.642986 to 1.604579°F (2.34%), but its confidence interval includes no gain.
  CRPS slightly worsened to 1.184925 versus 1.181990, and nominal 80% intervals
  covered 88.90%. SFO point error worsened. No candidate was promoted and no
  forecast probabilities, risk gates, sizing or execution flags changed.
  Training ran locally in 3.21 seconds; repeated artifacts were identical.
  Independent arithmetic/temporal review passed. Final local verification:
  2,876 Python tests passed, eight skipped; changed files compiled and diff
  checks passed. Local Semgrep was unavailable; CI remains its required gate.
  See `docs/research/2026-09-05-ml-challenger.md`. The 23:25 PDT public check
  verified five JSON hashes, all 38 static files, fresh Strategy/operational
  publication and live execution disabled. Each paper account remains separate.

- **Publication recovered (September 4, 21:57 PDT snapshot):** backend source,
  public provenance and full Strategy analysis match clean merged PR #113,
  `2a6432e3bdb29fa1798a4b07e5f5396685b5245b`. The public manifest was published
  at 04:56:12 UTC, Strategy generated at 04:56:05 UTC, and full analysis at
  04:36:08 UTC. Five public JSON hashes and all 38 uploaded static app files
  matched. The live desktop/mobile pages showed no publication-behind alert.
  The frontend wording correction from PR #114 is also deployed, at
  `65fc162ac12964f2fe0053b7d1b55aae1beeba27`. Backend artifact provenance remains
  at PR #113 because its Python source and runtime settings did not change.
- **Runtime healthy:** all fourteen original timers are enabled and active,
  twenty-nine canonical units pass integrity, no unit is failed, maintenance is
  absent, and disk use is 42%. Automatic scan, monitor, settlement, forecast,
  Strategy and publication cycles completed successfully. The two fixed-capital
  paper ledgers reconcile. Live execution remains disabled and dry-run enabled.
- **Incident and recovery:** the earlier task left an ongoing backup verification
  and a paused host. The obsolete local coordinator was stopped before it could
  transfer concurrent edits. The inherited downloaded backup completed integrity
  and foreign-key checks; identity, checksum and explicit successful FK status
  were rechecked while writers remained continuously stopped. Recovery reused
  that protected snapshot and the original timer policy through the canonical
  install, unit, index, account, full-analysis and publication gates. The
  temporary snapshot was removed before producer restoration. An extra staging
  assertion initially rejected tracked fallback JSON; the disposable build was
  stripped of those six runtime paths and verified before publication resumed.
- **Deployed fixes:** NWP lead batching reduces the normal daily HTTP request
  count from 240 to 120 with unchanged returned forecast values; exit reporting
  requires evidenced causes and keeps partial expiry open; Strategy freshness
  uses its own generation clock. Deploy source guards reject persistent checkout
  changes after long backup/transfer work, and interruption recovery initializes
  its watchdog policy before use. Future backup runs retain full downloaded
  verification while avoiding the duplicate pre-upload scan; FK command errors
  now fail closed.
- **Validation:** merged CI passed 2,842 Python tests with nine skipped on both
  Python 3.12 and 3.13, plus 172 frontend tests. Earlier local Python verification
  passed 2,837 tests with eight skipped in UTC and Pacific before the final six
  backup tests; the final targeted backup/deploy run passed 175 tests. Build,
  lint (two existing Fast Refresh warnings), browser bundle budgets and desktop/
  390px mobile behavior checks passed.
- **Final frontend deployment:** the wording correction passed 172 frontend
  tests, build/lint, independent review and the CI matrices. It was uploaded and
  hash-verified in a separate static directory before a short publication pause.
  The two publication jobs and any in-flight scheduler repair were drained, the
  app directory was switched with rollback retained, and fresh Strategy/operational
  data plus every public static file were verified before timer restoration and
  maintenance release. Desktop/mobile DOM checks confirmed accurate methodology,
  no publication-behind alert and no horizontal overflow. No forecast, trading,
  provider, billing or account policy changed in this frontend deployment.
- **Measured research:** a fresh AWS export and independent review support a
  small exploratory CRPS improvement: 1.125% across 2,658 paired forecasts,
  with only 0.011°F MAE improvement and a Philadelphia lead-two regression.
  This reconstructed fixed-lead experiment is not proven deployed accuracy,
  execution replay or higher profitability. See `docs/research/2026-09-04-crps-pilot.md`.
- **Use existing resources first:** AIFS is already in the eight serving models.
  Google and WeatherKit caches do not enter EMOS; Google shadow helpers have no
  scheduled caller. Do not disable the whole main forecaster/Google unit: it also
  runs all-city EMOS. Existing decision/context/forecast archives retain served
  distributions for offline recalibration, but not named live NWP member highs.
  Add only missing per-vintage evidence, and run heavy comparisons locally.
- **Remaining evidence gaps:** isolate posterior sizing by account/policy era and
  dependent fills; establish actual exit-quote age; preserve hourly completeness
  and provider availability. Source mixing and unseen-member bugs are latent
  in the inspected production sample, not established causes of low trading
  frequency. Existing archive rollups lack full account/policy lineage and the
  nightly full-table archive omits two maker-execution tables used by replay;
  these must be included before claiming off-host replay equivalence.
- **Account boundary and safety:** Live Stability and Research ROI are separate
  paper accounts, each started with $1,000. Their 21:57 PDT published equity was
  $1,052.23 and $1,078.97 respectively; never combine these into one bankroll.
  Only Live Stability contributes to readiness. Retention stays archive-only;
  alert webhook is unconfigured; unlimited CPU credits remain an owner-only AWS
  decision. Keep edge, loss, exposure, calibration and liquidity gates binding.

## Historical incident briefs (dated snapshots)

The former long opening brief is preserved below for continuity. Later entries
and the current brief supersede older state claims; none is a live status check.

- **SEPTEMBER AUDIT INCIDENT STABILIZED; EXACT FULL DEPLOY PENDING
  (2026-09-04):** the 30.6 GB paper journal had outgrown the deploy backup gate,
  the five-minute Pages publisher was repeatedly fetching full history, and the
  host was CPU-credit throttled. Controlled emergency installation switched the
  publisher to shallow fetches, moved it to an offset ten-minute cadence, gave
  scan/monitor priority over publication/Strategy Lab, lengthened artifact-lock
  waits, and added bounded Pages retry with inner-error propagation. A failure
  alert URL is still not configured because no real webhook was available; do
  not invent one. Enabling unlimited EC2 CPU credits remains an owner-only AWS
  action.

  A supervised retention run stopped every captured writer, verified archive
  coverage and foreign keys, and kept S3-verified recovery artifacts before any
  deletion. The original implementation then exposed a production-scale defect:
  every 5,000-row dedup batch rescanned roughly the whole journal and hit its
  one-hour timeout after freeing only tens of megabytes. The replacement
  materializes candidate IDs once, then performs durable primary-key batches.
  It removed 2,421,829 redundant decision rows, 2,546 old rejected decisions,
  175,107 contexts, 415,458 probability rows, 116,152 monitor rows, and 66,413
  orphan rows from each forecast/market parent table; approved rows were never
  deleted. `VACUUM INTO` produced a 17,946,406,912-byte candidate, whose full
  integrity, foreign keys, and per-table row counts passed before atomic swap.
  WAL mode was restored, `ANALYZE` completed, targeted statistics integrity and
  foreign keys passed, and a full paper scan passed before the 29 GB rollback
  file was removed. The scan added normal new rows, leaving the journal at
  17,953,009,664 bytes and the root filesystem at 42% used. Scheduled live-DB
  deletion remains safe-off until writer quiescence can be automated; the
  one-time cleanup is recoverable from verified archives and backup storage.

  Local remediation fixes also correct multi-city CLI settlement truth in CLV,
  persisted directional budget accounting, arbitrage/account and joint-resize
  exposure enforcement, narrow research-entry rejection handling, fee rounding,
  per-profile exit model reads, target-day EMOS sigma, city-clock lead days,
  the wrong-sided edge-reversal exit, a research-only five-point take-profit
  margin, and the same-day model heartbeat. A behavior-version input now rotates
  strategy fingerprints explicitly rather than blending policy eras. Dashboard
  publication freshness uses `published_at`, deploy-time readiness is labeled as
  such, and cached analytics show their cutoff. No risk floor was loosened and
  Live Stability remains economically separate from Research ROI. Final local
  verification passed **2,806 Python tests with 8 skipped** in both UTC and
  America/Los_Angeles, **170 frontend tests**, lint (two pre-existing Fast
  Refresh warnings), and the production SPA build. Exact clean landing, full
  deployment, public verification, and the final provenance update remain.

- **THIN-LIQUIDITY PAPER RETUNE DEPLOYED; SAFETY GATES STILL DETERMINE
  FREQUENCY (2026-08-28, PRs #105 and #107):** clean runtime and public
  provenance now match `f617b200c06216322bacc75f1b7560a90794028d`. Production
  evidence for August 23--27 separated the two economically independent paper
  accounts: Live Stability had zero new roots because no signals passed its
  existing policy, while Research ROI submitted thirteen roots, only four
  received any fill, and 27.66 of 1,610 requested contracts filled. The
  deployed execution retune gives both paper profiles a `$1` executable-
  notional floor and lets the Research ROI target sleeve take a whole-contract
  partial slice of displayed ask depth only when its exact-fee non-negative
  point and LCB edge floors still pass. Signal, forecast-spread, favorite-band,
  liquidity, exposure, loss, drawdown, and entry-edge controls are unchanged.

  The first deployment attempt was deliberately interrupted before source
  installation when a verified database snapshot temporarily raised disk use
  to 95%, above the host's 85% runtime ceiling. Its incomplete local snapshot
  was removed, the exact fourteen captured timers were restored, maintenance
  was released, and scheduler health passed without changing the live database,
  ledgers, orders, execution flags, or public artifacts. Root cause was deploy
  ordering: the large snapshot remained present while producers and freshness
  health were restored even though historical Strategy analysis was its final
  consumer. PR #107 moved analysis and exact snapshot removal ahead of all
  producer restoration and freshness validation.

  The retry created an off-host backup and re-verified its checksum, full SQLite
  integrity, and foreign keys after download. Source and twenty-nine canonical
  units installed cleanly; both fixed-capital paper ledgers reconciled; full
  Strategy analysis completed from the immutable snapshot; the snapshot was
  removed before runtime restoration; disk returned to 56% with about 28 GiB
  available; maintenance is absent; all fourteen timers are enabled and active;
  no unit is failed; and explicit scheduler health passes. Public publication
  at 07:01 UTC and Strategy analysis at 06:55 UTC carry the exact clean deployed
  revision. Local validation passed with **2,796 tests, 8 skipped**; the PR and
  post-merge Python 3.12, Python 3.13, and web verification matrices passed.

  Natural post-deploy scan and monitor cycles both completed successfully. The
  scan wrote 336 Live Stability and 360 Research ROI decision rows. Live had
  zero signal approvals. Research had 194 signal approvals but zero final entry
  approvals because every one failed the unchanged non-negative point and
  after-fee LCB edge rule; no new order was manufactured to inflate frequency.
  Thus the execution bottleneck is fixed for the next qualifying thin-depth
  opportunity, but zero Live Stability trades can still be the correct safety
  outcome. Live execution remains disabled, dry-run remains enabled, and no
  authenticated order client exists.

- **THIN-LIQUIDITY TRADE-CAPTURE RETUNE VALIDATED LOCALLY; NOT DEPLOYED
  (2026-08-22):** a fresh read-only runtime check found the deployed checkout
  still clean on `c82a67e0fb0a138ce42b86f22fbff6282590718f`, with no failed
  units and paper-only artifacts generated around 05:05 UTC. This was not a
  complete production re-verification, so the production-verification timestamp
  above remains 2026-08-16. Live Stability held `$1,051.40` realized equity but
  opened no new positions from August 17 through August 22; its only August 16
  root closed `+$0.84`. Research ROI held `$1,084.16`, opened nine positions
  from August 16 through August 22, and had achieved its `$50/day` research KPI
  on only 1 of 23 observed days. These are economically separate paper accounts
  and the figures must never be combined into one bankroll.

  The root execution bottleneck was displayed liquidity and the maker fill
  path, not unused capital or a missing risk exception. The existing live
  frequency diagnostic was also semantically wrong: it called 407 rescored
  candidate approvals over 45 independent days "trades" even though actual
  recent live orders were zero. Local code now labels that diagnostic as
  candidate approvals, never executed trades. For the paper profiles, an
  evidence-selected `$1` executable-notional floor replaces the historical `$5`
  floor while the frozen/default configuration retains `$5`. The Research ROI
  target sleeve may take a whole-contract partial slice of the displayed ask
  when exact taker fees and its non-negative point/LCB edge floors still pass;
  insufficient or invalid depth keeps the prior maker path. Signal approval,
  favorite-band, source-spread, after-fee lower-bound edge, position, account,
  city, region, aggregate exposure, daily-loss, and drawdown controls are
  unchanged.

  Point-in-time settlement replay over the captured depth window supported the
  narrow change but did not prove profitability. The capped Live replay moved
  from 62 fills at the `$5` floor to 84 at `$1` (`+35%`), with modeled outcomes
  moving from 57-5 and `+$12.48` to 79-5 and `+$17.73`; its day-clustered 95%
  P&L interval still crossed zero (`-$2.54` to `+$3.35` per day). Research ROI's
  `$1` partial-cross hybrid modeled 85 fills versus 54 maker-only fills, with
  `+$250.53` versus `+$156.97` and a less-negative worst day (`-$44.93` versus
  `-$69.97`); its daily interval also crossed zero (`-$2.08` to `+$27.82`).
  Longer maker TTL, deeper-price crossing, and same-day entry were rejected by
  the evidence rather than used to manufacture volume. Validation passed with
  **2,795 tests passed, 8 skipped**, plus a clean diff check. No production
  source, service, timer, database, ledger, order, execution flag, or public
  artifact was changed. Deployment remains a separate audited operation.

- **PAPER-READINESS HARDENING DEPLOYED; LIVE TRADING REMAINS IMPOSSIBLE
  (2026-08-16, PR #96):** runtime and public SPA revision
  `c82a67e0fb0a138ce42b86f22fbff6282590718f` are deployed with clean source
  provenance. The deploy created an off-host encrypted database backup, then
  downloaded and re-verified its checksum, full SQLite integrity, and foreign
  keys before installing source. Both complete integrity scans took roughly an
  hour on this I/O-constrained host, and the immutable-snapshot Strategy Lab
  refresh added about eighteen minutes. This is slow but produced a tested
  rollback image; redesign deploy-time verification only with equivalent
  recovery proof. A first SPA attempt from a clean public dependency install
  failed locally before publication because the registry HeroUI Pro stub omits
  authenticated subpath modules. Rebuilding with the documented authenticated
  Mac toolchain passed and the current asset is public.

  Production now has fourteen active canonical timers, twenty-nine units with
  no drift or daemon-reload debt, zero failed units, no maintenance marker, and
  a successful scheduler watchdog. The full analysis cache and private evidence
  were regenerated from the verified immutable snapshot, then the public
  artifact was rebuilt and matched its exact manifest. During the retention
  canary, five monitor and two scan activations started and completed normally.
  Disk use was 45% with about 34 GiB available; the paper database was about
  18.4 GB.

  The scheduled retention service now defaults to archive-only and its canary
  succeeded: archive coverage and foreign keys passed, the wrapper emitted the
  expected `DEGRADED` no-delete diagnostic, and no live-database prune ran. It
  removed only old local archive partitions whose uploaded copies were verified;
  those partitions remain recoverable off-host. The canary used about 2m09s CPU,
  peaked at 2.5 GB memory with no swap, and cleared the five-day failed-unit
  marker. Live-journal rows were not deleted, so database growth remains an
  explicit operational risk. Keep disk monitoring active and use
  `quiesced-delete` only under supervised writer shutdown; never leave that mode
  enabled for the timer.

  Future-live limits now resolve from a configured risk bankroll: current
  defaults are $1,000 capital, 1% per order ($10), 2% daily loss ($20), and 5%
  total pilot loss ($50). The host migrated only the historical exact
  $50/$20/$10 defaults. Live remains `enabled=0` and `dry_run=1`, and the code
  still has no authenticated write client. Fresh strict readiness is
  `REPLAY_REQUIRED`, **4/12 checks, 43.6%, four complete post-boundary settlement
  days**. The older 8/12 and seventeen-day available-case result is superseded
  and must not be quoted as current evidence. The economically separate Live
  Stability and Research ROI paper ledgers reconciled during installation and
  must never be combined.

- **SCHEDULED LIVE-JOURNAL DELETION DEFERRED SAFE-OFF LOCALLY; NOT DEPLOYED
  (2026-08-15):** the retention wrapper now defaults to
  `SFO_PRUNE_MODE=archive-only`, including on established hosts whose preserved
  environment does not yet contain the key. Nightly archive export, feature
  rollup, optional upload, exact archive gate, foreign-key audit, and
  upload-backed archive cleanup still run, but the wrapper skips `paper-prune`,
  emits explicit `DEGRADED` diagnostics, and exits successfully so it does not
  take the long live-database write lock or repeatedly fail the unit. Unknown
  mode values fail closed to the same no-delete behavior. The only retained
  opt-in is the exact `quiesced-delete` mode for an operator-supervised run
  after all journal writers are stopped; restore archive-only before timers
  resume. This deliberately allows the live SQLite journal to grow. The disk
  watchdog remains the safety alarm (85% default ceiling) but does not delete
  data, and deletion alone makes pages reusable without shrinking the file;
  filesystem reclamation still requires the separately quiesced compaction
  workflow. Focused validation: **122 retention/deployment tests passed**,
  changed shell syntax passed, and the diff check passed. No production timer,
  service, database, ledger, order, or live-trading flag was changed.

- **REAL-MONEY READINESS AUDIT: NO-GO; LOCAL EVIDENCE HARDENING IS NOT DEPLOYED
  (2026-08-15):** WeatherEdge has more than two months of project/paper journal
  history, but the two current comparable paper ledgers have not each run for a
  month. Live Stability began July 26 (about 20 days at this snapshot) and
  Research ROI began August 1 (about 15 days). Live Stability held $1,047.81
  realized equity, four open positions, no pending requests, and $1,048.11
  marked equity. Research ROI held $1,002.78 realized equity, ten open
  positions, one $89.88 pending reservation, and $1,021.93 marked equity.
  These remain economically separate simulated ledgers and must never be added
  into one bankroll or described as customer funds.

  Production was clean and paper-only on `10b4844...`; all fourteen canonical
  timers were enabled and active, twenty-nine canonical units matched their
  templates, and the natural scheduler, forecast freshness, publication, and
  disk checks passed. The only failed unit was retention pruning. The August
  11--15 jobs each completed archive/upload/gate/foreign-key work and then hit
  the one-hour prune timeout. On the two directly inspected incidents this
  displaced 58 monitor/scan ticks in total. The database was about 17 GiB
  (whole volume about 18.25 GiB), disk use was 45%, and core financial-table
  quick checks plus the natural foreign-key audit passed. A whole-file forecast
  database quick check was stopped after 30 seconds, so full-file integrity is
  unproven, not known bad. The probable prune causes are a non-enforced
  per-batch deadline, a poor competing SQLite plan under stale statistics, and
  insufficient phase instrumentation; do not mask this with a larger service
  timeout.

  The fresh public wrapper still reported `REPLAY_REQUIRED` (8/12, 17/30 days,
  0.210 calibration gap versus the 0.100 ceiling), but its inner analysis was
  generated August 11 and was about five days old. The watchdog checked only
  the fresh wrapper timestamp, so those numbers are a stale snapshot rather
  than current readiness proof. The prior replay also mixed policy eras and
  calculated economics from partially observed target days: only six dates
  were wholly qualified while favorable P&L/CI metrics used seventeen. Local
  code now fails stale inner analysis closed, qualifies whole weather days,
  requires exact per-series policy fingerprints, rejects mixed eras, and
  explicitly blocks promotion because those fingerprints still omit immutable
  model/source/training/calibration lineage. A recurring full-analysis producer
  does not yet exist, so the new 36-hour freshness gate will eventually become
  `ANALYSIS_STALE` until that producer is added.

  Local, undeployed hardening also corrected observation-versus-final settlement
  authority, future-dated forecast acceptance, multi-city failure propagation,
  all-skip false success, NO-side bound math, live-ledger scoping, seven-day P&L
  window inconsistencies, private-key hygiene, invalid live intent/aggregate
  batch risk, zero-depth taker fills, and maker TTL/tape reconciliation. Maker
  expiry now reconciles before cancellation, retains a five-minute ingestion
  grace and per-ticker watermark, rejects malformed/wrong-ticker/out-of-window
  payloads, preserves coverage through settlement/partial close, and replays
  late pre-TTL tape. The fixed five-minute grace is provisional: production lag
  telemetry and periodic terminal-ticker backfill are still required before its
  zero-fill evidence can support real money. A maker root closed directly after
  a partial fill also has no durable post-close reconciliation queue today; the
  stricter restatement correctly excludes it instead of inventing completeness.

  Real-money execution remains structurally unavailable. There is no signed
  authenticated client, persisted client/exchange/fill ID state machine,
  timeout-after-accept recovery, restart reconciliation, private user stream,
  exchange-authoritative balance/position/settlement comparison, atomic
  pending-risk reservation, sticky kill/cancel-all path, credential-isolated
  executor, canary/deploy/incident runbook, or send-time quote/market/clock/
  tick/depth revalidation. The valid enabled + non-dry `PILOT_READY` test still
  stops at `authenticated live order client is not implemented`. Do not add an
  API key or enable production writes until the demo fault/chaos suite,
  reconciliation, kill drills, current-policy complete-day evidence, model
  lineage, calibration, and operational blockers all pass.

  The undeployed future-live policy no longer embeds one universal $50 ceiling.
  Its defaults now resolve from a deliberately isolated $1,000 risk bankroll:
  1% per order ($10), 2% daily loss ($20), and 5% total pilot loss ($50).
  Capital and percentages are operator-configurable, explicit dollar overrides
  remain backward compatible, contradictory/nonfinite/blank configuration
  fails closed, and the guarded host migration changes only the historical
  exact $50/$20/$10 defaults. These controls do not enable live execution.

  Fresh local verification after the audit: **2,721 Python tests passed, 8
  skipped**; **166 frontend tests passed across 38 files**; the production SPA
  build, Python compilation, changed-shell syntax, and diff checks passed.
  Frontend lint exited successfully with two pre-existing Fast Refresh warnings
  in `CityGrid.tsx`. No production service, timer, database, order, ledger,
  credential, execution flag, or policy was mutated, and these local changes
  were not deployed.

- **APPLE WEATHERKIT SOURCE DEPLOYED AND ACTIVE AT ZERO TRADING WEIGHT
  (2026-08-10, PR #93):** runtime revision
  `10b4844dd28e1008789dab5846b67e07bfeabc0c` now runs the WeatherKit REST
  source for all fifteen settlement stations at four fixed UTC vintages. One
  bundled hourly+daily call per station per vintage is 60 scheduled calls/day.
  The first manual production refresh and the next natural timer refresh each
  completed 15/15 cities with zero failures. The independent ten-minute purge
  also passed. Fourteen total canonical timers are enabled and active (thirteen
  application timers plus the scheduler watchdog), zero units are failed, and
  scheduler/publication health passed after activation. Live execution remains
  disabled and the two paper accounts remain economically separate.

  Authentication is ES256 in memory; requests refuse redirects; each city
  fails independently; and settlement highs require 24 unique hourly forecasts
  in the registry's exact fixed-standard climate window. Production held 30
  complete, unexpired station-day rows across all fifteen cities in a private
  mode-0600 tmpfs cache after activation. The source key is outside the source
  tree, and a production audit found no key file in deployed source and no
  credential identifiers, private-key location, tokens, or Apple temperatures
  in service journals. No Apple table exists in `weather.db`.

  Apple values do **not** enter `weather.db`, `nwp_model_forecasts`, EMOS
  fitting, training archives, paper-decision snapshots, public JSON, or logs.
  Live trading weight remains exactly zero, so activation cannot alter forecast
  probabilities, risk gates, size, or paper decisions. This is a live source,
  not evidence that Apple improves the model. Under the current Apple Developer
  Program License Agreement Attachment 8 storage restrictions, Apple-only
  forecast vintages/residuals are not durably archived; historical evaluation
  and any nonzero weight remain deferred until Apple provides written
  clarification or qualified counsel approves a compliant evidence design.

  Local evidence before merge: **2,623 tests passed, 8 skipped**; Python 3.12,
  Python 3.13, and Web CI passed; production lock resolution, shell syntax,
  compilation, and diff checks passed. Deployment used the full quiesced,
  encrypted off-host backup gate: upload, independent download, checksum,
  SQLite integrity, foreign keys, unit integrity, account cutover, exact source
  provenance, public-manifest parity, and the bounded full Strategy analysis
  refresh all passed before maintenance mode cleared.

- **PRE-DEPLOY NIGHTLY FAILURES CAPTURED; ROOT CAUSES REMAIN OPEN
  (2026-08-10):** fresh production preflight found two failed historical
  oneshots before WeatherKit deployment. The dataset job failed after IEM ASOS
  returned a second consecutive HTTP 503; its other configured sources
  completed. The retention job archived and uploaded its day, passed its archive
  gate and foreign-key audit, then exceeded its existing one-hour service
  deadline during pruning. Their failed-state markers were cleared only after
  preserving the evidence so the independent deployment health gate could run;
  neither job, timer, threshold, or retention policy was changed in this
  WeatherKit task. Both timers remain enabled. Revalidate their next natural
  runs and treat recurrence as a separate production incident; do not describe
  the underlying causes as fixed merely because final post-deploy health showed
  zero failed units.

- **TRANSIENT PUBLIC-MANIFEST HTTP 503 DURING FINAL AUDIT; RECOVERED WITHOUT
  CODE OR POLICY CHANGE (2026-08-10):** scheduler health passed immediately
  after deployment, then one later manual audit failed solely because the
  public Pages manifest returned HTTP 503. Unit/timer integrity, local
  artifacts, forecast freshness, disk, source provenance, Apple refresh, and
  paper-only safety were otherwise healthy. The same public manifest then
  returned HTTP 200 on three workstation and three AWS probes, and the
  unchanged scheduler service passed on rerun with zero failed units. This is
  evidence of transient upstream publication availability, not a WeatherKit or
  source-deploy defect. No retry threshold or watchdog behavior changed in this
  task; treat recurrence as a separate reliability issue rather than claiming
  it was permanently fixed.

- **RESEARCH ROI V6 NEAR-5% PAPER DAY AUDITED; POLICY HELD STEADY
  (2026-08-05 intraday snapshot):** the economically isolated Research ROI v6
  ledger realized **+$44.5025** on Aug 5 Pacific time: 4.45025% of the fixed
  original $1,000 reference and $5.4975 short of the $50 paper-research
  objective. The public objective correctly remained a miss. Seven logical
  decisions resolved 7-0 on $181.59 of entry capital; realized equity reached
  $1,038.2389, or +$38.2389 lifetime v6 P&L. At the 15:08 PDT publication
  snapshot there were four separately marked open positions ($10.49 cost basis,
  about +$0.99 unrealized) and two pending maker reservations. These figures
  belong only to Research ROI and must not be added to Live Stability as though
  they were one bankroll.

  Houston's Aug 5 94–95 °F NO position supplied +$35.6927, or 80.20% of the
  day. v6 requested 147 contracts at $0.61 and public tape partially filled
  94.1; the position closed at a $0.99 displayed bid ($0.989306 net) after first
  falling to roughly -$33.29 / -58% marked P&L. The normal NO stop fired, but
  the existing fresh-model veto retained it while model support remained above
  entry; the loss stayed just inside the unconditional -60% catastrophic floor.
  Six Aug 4 targets contributed the other +$8.8098 when official settlements
  booked on Aug 5. This is resolution-date P&L from earlier admissions, not a
  cohort opened on Aug 5.

  Fixed-placement replay shows why v6 helped without authorizing more size: the
  same Houston tape would have filled 49 v4 contracts, 73 v5 contracts, and 94.1
  of v6's 147 request; requests above v6 still cap at 94.1. Only 2 of 18 audited
  positive-fill v6 trade-through requests were full (11.1%). The six-day v6
  bootstrap interval remains -$6.7367 to +$23.5259/day and the $50 objective has
  been hit on 0/6 days. Action: preserve the exact v6 gates, cap geometry,
  five-minute scan, 15-minute maker-request lifetime, two-minute monitor,
  model-fair-value take-profit, fresh-model veto, and hard floor. Do not activate
  v7 or loosen any safety control from this concentrated observation. Full
  evidence and the prospective replication protocol are in
  [`docs/RESEARCH-ROI-V6-2026-08-05.md`](RESEARCH-ROI-V6-2026-08-05.md).

  Read-only production checks at 15:03 PDT found zero failed units, all 12
  canonical timers enabled and active, no runtime-unit drift, 58% disk use, and
  clean source provenance at the revision above. Live execution remained
  disabled and dry-run remained enabled. This audit made no policy, service,
  order, ledger, deployment, or other production mutation.

- **RECRUITER-SITE DESIGN DEPLOYED (2026-08-05, PR #84):** the React SPA from
  merge `1356a73beedef927d6227ab389acfad69ada66a2` was published with the
  web-only deploy path. The AWS Python/runtime source deliberately remains on
  `2c7a4b25948a6bccd38d506ea27db27f0bbcf2d9`; the user explicitly narrowed
  this release to the design after the full runtime backup proved slow. The
  runtime publisher changes in PR #84 therefore remain pending a later full,
  backup-gated runtime sync.

  A full sync was started, then canceled during its verified database-backup
  gate at the user's direction. The orphaned upload and its exact temporary
  local snapshot were removed; no source or schema transfer had begun. Recovery
  cleared the deployment-maintenance marker, restored all 12 previously enabled
  timers, restarted publication producers, and returned the host to zero failed
  units with no backup process remaining. Live execution stayed disabled and
  the runtime remained paper-only throughout.

  Live production canaries at 390x844 and 1440x1000 verified zero horizontal
  overflow, four featured archive tabs, exactly one selected evidence panel,
  preserved open and closed position sections, and omission of the zero-trade
  v2 archive. The Strategy Lab loaded without an error on the public URL.

  Strategy Lab keeps the publication-stamped dossier and economically separate
  Live Stability / Research ROI paper workbenches, including the open, pending,
  and closed positions the owner wanted to preserve. Research lineage is now a
  compact four-era experiment switchboard: four selector cards show the exact
  attributed P&L and resolved sample at a glance, while only the selected era
  opens into its dated graph, evidence window, W-L/ROI metrics, accounting
  boundary, and daily table. Zero-trade v2, execution-only motion, and generic
  non-comparable archives stay queryable in the public artifact but are omitted
  from the recruiter view; the two-trade v5 scale probe appears only in the
  compact policy-decision ribbon. Arrow/Home/End keyboard navigation and one
  labelled tabpanel were verified. At 390x844 the lineage fell from about 5,655
  px to 1,951 px (-65%) and the full page from 16,229 px to 12,453 px, with zero
  horizontal overflow. At 1440x1000 the lineage fell from about 3,051 px to
  1,178 px (-61%) and the full page from 9,365 px to 7,519 px.

  A fresh local/AWS/public copy audit also removed unsupported or stale claims
  across all routes: current coverage is 15 cities / 8 NWP members; the current
  scheduled paper scan is five-minute while historical dedupe copy is
  cadence-neutral; SFO is blend-capable but currently serves the EMOS
  operational fallback; LSTM diagnostics are SFO residual-calibration evidence,
  optional external inputs are conditional, and overview skill metrics are
  explicitly SFO-scoped. Read-only daily-report eligibility is no longer
  described as an order placement, five-minute publications are no longer
  called real-time, calibration warnings are surfaced, and the readiness panel
  states that authenticated real-money execution is not implemented. Frontend
  presentation normalizes older runtime labels such as `Research ROI · 5% daily
  KPI` without altering the raw audit artifact.

  The current AWS/public audit used Strategy schema v3 at
  `2026-08-05T21:55:44Z` and city coverage at `2026-08-05T21:57:37Z`. Live
  Stability reported `$1,032.50` realized paper balance, `+$32.50` all-time
  realized P&L, 53 resolved (47-6), and two open positions. Research ROI v6
  reported `$1,038.24`, `+$38.24`, 32 resolved (23-9), and four open positions.
  The active policy remained `research-target-roi-v6`; readiness remained 5/12
  with replay required; live orders were disabled. These are two economically
  separate paper accounts, not one bankroll or combined return.

  Verification: production build passed; 164/164 frontend tests and 2,554/2,554
  Python tests passed (8 environment-dependent skips); lint, icon determinism,
  diff checks, and browser-observed bundle budgets passed. Browser checks
  covered 390 px mobile and desktop Strategy Lab layouts with zero page
  overflow, four archive tabs, one selected evidence panel, and preserved open
  and closed position sections. The
  publisher now emits cadence-neutral dedupe copy, current profile labels,
  conditional SFO-method wording, and exact archived-account snapshots for
  v3/v4/v5; these publisher changes take effect only after a later deployment.
  The 2026-08-05 pre-deploy audit also verified zero failed units, all 12 timers
  enabled and active, paper-only mode, live execution disabled, dry-run enabled,
  and exact source/manifest provenance at the clean `main` revision above. No
  The publisher/backend portion described above was not deployed in this
  web-only release; only the reviewed SPA bundle was published.

- **Retention timeout and disk-preflight blocker RESOLVED (2026-07-27; current
  health reconfirmed 2026-08-05):** PR #73 replaced the unbounded retention
  query shape with indexed, bounded work, preserved the rejected-arm evidence,
  and added safe offline compaction without raising the 30-minute timeout.
  The first production repair deleted the queued stale population while leaving
  approved rows untouched and reduced the database from about 10.86 GB to 9.61
  GB. The current deployed revision contains that fix. On Aug 5 the prune unit
  was not failed, the canonical timer set was healthy, and disk use was 58%.
  Remaining maintenance debt: the archive coverage gaps and the day loop noted
  in the modeling audit should still be bounded before they become growth
  problems again; they are not current blockers.
- **Ladder-depth capture is present in the current runtime:** PR #71
  (`7de41a89`) is an ancestor of the deployed revision. It remains best-effort,
  observation-only evidence; never treat ladder depth as proof that a resting
  maker request filled. Public trade-through tape and the canonical allocation
  ledger remain the fill authority.
- **Execution-bar alignment (2026-07-27, PR #69, runtime revision
  `b5ae442b22d37ac6ad831db02e7c50a5309a47fc`):** the 07-26 capture release was
  a near-no-op in production. On 07-26/27 the live book recorded 23 approved
  candidates and placed ZERO orders: every one had a one-tick spread and an
  after-fee lower-bound edge of 0.002-0.007, while the execution layer still
  demanded the 0.02 MAKER reservation margin. Two fixes: a natural cross
  (bid+1 already at the ask) now routes through the taker path instead of
  being judged against a margin that exists to cover adverse selection on a
  RESTING quote, and the live crossing bar is now the approval gate's own
  floor (non-negative after-fee edge against the modelled lower bound).
  Measured on settled outcomes with the repo's canonical
  `settlement_truth` rule -- which reproduces the engine's realized P&L on
  283/283 settled orders with zero mismatches -- the change moves live from
  58 positions / 87.9% win / $1.33 per day (day-clustered 95% CI
  -$0.41..+$3.14) to 144 positions / 92.4% win / $3.22 per day (CI
  +$0.27..+$5.94). No decision or safety gate moved.

- **MEASURED CEILING -- read before promising a daily number.** A 36-agent
  adversarially-verified analysis over the full decision journal established
  that the binding constraint is DISPLAYED LIQUIDITY, not the model, the
  gates, sizing, or exits. Median `recommended_contracts` is 88 against a
  median displayed ask of 5, and **97.4% of live approved candidates are
  depth-bound**. At recommended Kelly size the approved population was worth
  ~$41/day; capped at the depth actually shown it is ~$5/day. Peak daily
  capital deployed was $76 of a $180 budget (42%) -- the book is
  liquidity-starved, not capital- or gate-starved. Best case with three
  entries per market/side and perfect capture is $9.29/day; a realistic
  post-change run rate is **$4-6/day, with $10 days on roughly the 40% of
  days when depth is generous**. **$10/day is not reachable as a sustained
  average on this liquidity, and Research ROI's $50/day is roughly 3x above
  its measured ceiling (~$18.60/day even granted live-like caps).** Closing
  the gap needs more markets or deeper books, not looser gates.

- **What was measured and REJECTED** (do not re-propose without new
  evidence): loosening any rejection bucket -- every one loses money on
  settled outcomes (sleeve edge/LCB -$0.028/-$0.032 per contract, source
  spread -$0.047 and monotonically worse with spread, the 1c/2c tail rule
  0/34 wins, model/market gap -$0.024); the live `edge_lcb >= 0` floor's
  marginal population is a null (n=28, -$0.0019/contract, t=-0.03); the
  same-day `min_lead_days=1` block (research same-day is ~6x worse than
  next-day, and all 7 signal-approved blocked live candidates had zero
  displayed depth, so $0.00 was forgone); per-city selection (permutation
  test p=0.538 live, p=0.958 research); narrowing or widening the 0.70-0.97
  favorite band; banking profits earlier (every variant loses at every
  level); raising position caps (buys ~$1/day while the worst position grows
  to 201% of the daily-loss breaker, and inflates the bucket with the LOWEST
  settled ROI); and a research policy v4 with live-like caps (order coverage
  of approved candidates is already 100%, so it cannot add a filled
  contract).

- **Position accumulation was measured, not assumed.** Allowing 2/3/5 lots
  per market-side lifts live to $3.55/$5.27/$6.67 per day but pushes the
  day-clustered CI lower bound to -$1.00/-$0.20/+$1.09 and the worst day
  from -$13.30 to -$20.09 (the 2% daily-loss breaker), while capital
  efficiency falls. It also requires relaxing the side-agnostic
  `has_active_paper_entry` guard, which additionally prevents holding YES and
  NO on the same market. Deferred as the highest-EV candidate for a future
  walk-forward, not shipped.

- **Highest-value next step is instrumentation, not tuning.** Only
  top-of-book depth is persisted, so whether walking one or two ticks deeper
  into the ladder would lift the ceiling is currently UNANSWERABLE. Record
  the top ~3 ladder levels per side plus a per-attempt fill record (quoted
  price, mode, filled/expired, depth visible at attempt), mindful that
  `decision_snapshots` is already the table under retention pressure. Then
  hold live behavior steady for a ~30-day window: the capture release has
  only one day of evidence (07-26 filled 3 of 7 placements, 43%, against a
  15.6% baseline of 26/167 over 07-18..07-25).

- **A methodology warning that invalidated an early pass.** `decision_snapshots`
  recording changed TWICE inside retention: research began recording the FULL
  ladder on 07-19 (~60k rows/day) and live on 07-24 (43 -> 64,872 rows). Any
  approval-RATE comparison across that boundary is a denominator artifact.
  Use `approved`/`signal_approved` counts and de-duplicate to DISTINCT
  (target_date, market_ticker, side). Live distinct approved opportunities
  per day: 20, 18, 15, 21, 8, 4, 8, 4, 8, 3 (07-18..07-27).

- **Execution-capture release (2026-07-26 evening):** PRs #66 and #67 are
  merged and deployed at runtime revision
  `5f5dc1e05e0a40524042710c9943f1290a02d2be`. July's tightening favorite
  books (displayed ask depth 21 -> 4-6 contracts, spreads 3.6c -> 1.5-1.9c)
  had starved maker-only entries: live maker quotes filled under 20% (46/49
  expired 07-18; 0/3 on 07-22) and live realized P&L fell from ~$10/day to
  ~$0 while approved candidates carried positive after-fee taker-cost edge.
  The release changes EXECUTION only: (1) live taker-cross when the
  after-fee LOWER-BOUND edge at the displayed ask clears the SAME 2% buffer
  the maker path enforces (whole contracts, depth-capped, >= $5 crossing
  notional); (2) live reservation-price resting fallback (rest at the
  highest tick preserving the buffer instead of dropping the candidate);
  (3) target-research crossing at its UNCHANGED zero after-fee point/LCB
  floor, only when displayed depth absorbs the entire intended size, so the
  v3 allocator's policy-sized resting path is preserved. No decision gate,
  sizing cap, account policy, loss pause, or readiness scope changed; the
  frozen `StrategyConfig()` defaults keep every new flag off. Live strategy
  fingerprints moved with the config (limit `b0075c015530e830c11c588b`,
  market `b0fece729659b86d2e1e35f1`); readiness treats any non-legacy
  fingerprint as valid, so the promotion clock is unaffected.
- **Post-deploy state (confirmed with LIVE production data, not just
  backtest):** cutover validation reported exactly two fixed-capital
  ledgers; build_info matched the new revision with a clean tree; 0 failed
  units; 12/12 timers enabled and active; scheduler health succeeded. Within
  hours of the liquid US afternoon window (14:00-17:00 UTC), the live book
  produced real taker-cross fills: order 1711 (KXHIGHTSEA, 8 contracts,
  instant fill at the ask), order 1714 (KXHIGHPHIL, 6 contracts, instant),
  order 1721 (KXHIGHTATL, 14 contracts, instant) -- confirming the natural-
  cross routing fix works against the real Kalshi order book, not only the
  settlement-replay backtest.
- **Production release:** PR #58 is merged. The EC2 runtime, generated
  artifacts, public manifest, and rebuilt React app shell were verified against
  runtime revision `71ac845422fc75cc35e24bb3b3a918dd44f917b3`. The app-shell
  checksum matched locally, on EC2, and on the public site.
- **Fresh account era:** exactly two `$1,000` paper ledgers are active. **Live
  Stability** prioritizes win rate, consistency, and controlled growth and is
  the only readiness profile. **Research ROI** accepts higher bounded paper risk
  against a fixed `$50/day` KPI, equal to 5% of original capital, and is
  excluded from live readiness.
- **Preserved history:** five prior accounts are archived and entry-frozen.
  Sixty-two legacy open paper positions remain settlement-active in the normal
  monitor/settlement lifecycle; no orders, fills, P&L, or resting history were
  deleted or reassigned to either fresh ledger.
- **Accounting safety:** both active ledgers were `$1,000`, `ACTIVE`, and
  reconciled with zero open or pending positions at verification. Invalid
  account identity or reconciliation now fails closed: active profile,
  accounting, and readiness displays disappear instead of inferring a balance.
- **Readiness:** Live Stability is the sole readiness profile, using valid live
  evidence across the legacy and fresh live identities. The deployed status
  was `REPLAY_REQUIRED` with 5 of 12 checks passed; every research account was
  explicitly excluded. This is not real-money ready.
- **Reliability:** Strategy Lab now uses a persistent fixed five-minute
  wall-clock timer. An offset scheduler watchdog verifies the 13 application
  timers, all canonical unit definitions, database/disk health, hashes, source
  provenance, and local/public freshness. It can repair only bounded
  publication staleness and never starts scan, monitor, settlement, or another
  trading action. A separate production canary verifies all 14 timers,
  including the watchdog.
- **Public experience:** the Strategy Lab now exposes both fresh profiles,
  achieved-performance history, daily and cumulative P&L, and true account
  balance. Rapid-hover QA left one tooltip, one active dot, and zero ghost
  cursors; the current-day tooltip showed `$1,000.00` account balance. Desktop
  and 390 px mobile layouts had no page-level horizontal overflow.
- **Execution layout:** Positions & execution log now has balanced gutters,
  more separation from dividers and P&L, and a compact pending-limit empty
  state. The public desktop and mobile layouts were inspected after deployment.
- **Operational health:** zero failed units; 25 canonical systemd units matched
  source; 12 of 12 timers were enabled and active; scheduler health succeeded;
  disk use was 68.4%, below the 85% guard; public and local manifests were fresh,
  source-matched, and snapshot-identical. Natural Strategy cycles generated at
  10:40 and 10:45 UTC after deployment; the latest was present in the public
  manifest published at 10:50 UTC.
- **Safety and access:** real-money execution remains disabled and dry-run
  remains enabled. Keep the narrowly scoped owner SSH rule for the next session;
  revoke it only when the owner says access is no longer needed. Its identifiers
  remain only in ignored operator state.

## What Went Wrong

### Strategy Lab appeared behind

There were two different causes across the recovery:

1. The earlier production Strategy tail query used parameterized SQLite limits.
   Production SQLite selected a full scan over a large decision journal, causing
   a service timeout and stale public Strategy data. PRs #55 and #56 restored a
   bounded indexed plan and separated overlapping maintenance windows.
2. During this restart, the new schema, profiles, and interface existed on the
   implementation branch before production had received both release halves.
   The public site therefore still showed the prior schema/account era until the
   runtime/data sync and separate prebuilt React app-shell sync both completed.
   This was deployment lag, not evidence that the paper engine had stopped.

The release was completed through both audited paths and then verified by exact
source, manifest, app-shell checksum, DOM, and screenshot evidence.

### Completion-relative scheduling could drift

The Strategy timer was completion-relative. A slow cycle moved the next cycle
later, and no independent scheduler knew whether publication was stale, a unit
had drifted, or trading was intentionally paused. The new calendar timer is tied
to wall-clock time. The new watchdog distinguishes safe age-only publication
repair from unsafe timer/unit state and refuses to auto-repair trading services.

### Legacy attribution was easy to mistake for account equity

Early live and research strategies shared economic paper accounts, while later
research policies used isolated accounts. A profile-attribution curve could
therefore be misread as that profile's bankroll, and cross-account totals could
be described as one balance. The new publication and UI separate:

- strategy-attributed P&L;
- true economic account balances;
- two fresh active ledgers; and
- entry-frozen archived histories with settlement state shown explicitly.

Missing historical account balance is now shown as unavailable rather than
synthesized from attributed P&L.

### Network location changed

The owner's public IP changed between houses, so the narrow SSH allowlist no
longer matched. This interrupted operator access but did not prove an
application failure. A narrow owner rule restored access and is intentionally
retained for the next session.

### Maker-only entries starved once the market tightened

After 07-22 the favorite books thinned (displayed ask 21 -> 4-6 contracts,
spreads 3.6c -> 1.5-1.9c) and live volume collapsed (45 placements/day ->
2-9) while win rate stayed ~100%: a fill-capture failure, not an edge
failure. Journal counterfactuals showed capture-eligible expected profit of
$0.8-2.2/day (live, at the unchanged 2% LCB bar with >= $5 depth) and
$8.6-44.5/day (research target, at its unchanged zero floor with full depth
coverage). The 07-26 evening release converts exactly that eligible set to
immediate fills and rests everything else as before. It cannot conjure edge
on days the bars fail; expect live recovery toward $10/day only when
capture-eligible candidates exist, and treat the research $50/day KPI as an
aspiration the market may simply not offer on quiet days.

### Deploy preflight ran out of disk twice, then needed a modern python3

Back-to-back deploys hit the backup preflight space check (snapshot +
restore copy + 1 GiB ~= 19.6 GB): KEEP_DAYS=1 never removes same-day
snapshots, so each earlier same-day deploy's verified 9 GB local rollback
snapshot had to be removed manually after confirming its off-host copy and
checksum existed. Separately, sync_to_box.sh shells `python3` for the
execution/accounting version stamps; the Mac's default python3 is Xcode
3.9 (no `datetime.UTC`) and one run aborted after quiesce+transfer,
leaving the box safely quiesced with the maintenance marker as designed.
The rerun used a PATH with a modern python3 first. Rerun hazard to know:
capture records the CURRENTLY-enabled timer policy, so a rerun from the
quiesced state would capture an empty policy and restore nothing;
neutralized by `systemctl enable` (without `--now`) of the 12-timer policy
before rerunning, so capture saw the true policy while nothing ran
unvalidated.

### Supply decline is a market condition, not a fixable data bug

The LAMP/GFS-MOS HTTP 403s look alarming but do NOT explain the decline: those
feeds supply station-guidance features, not the NWP ensemble that drives EMOS.
`nwp_model_forecasts` is stable at 8 models x 240 rows/day through 07-27
(only `gfs_graphcast025` stopped, on 2026-05-21, two months before the decay).
What actually happened is that the crowd priced closer to the model: mean edge
on APPROVED live rows fell 0.064 -> 0.033-0.056, spreads tightened 3.6c ->
1.5-1.9c, and displayed depth fell 21 -> 4-11 contracts. Distinct approved
opportunities fell from ~20/day to 3-8/day. Nothing in our control caused it
and no safe parameter change reverses it.

### Profit targets were being treated too literally

The July 20 live paper result demonstrated that a strict, high-confidence,
larger-size NO position could produce roughly `$10` in one day. It did not prove
that `$10`, `$16`, or 5% can be repeated daily. Counterfactual review did not
support blindly holding 98-cent exits to settlement merely to collect the last
two cents; Live Stability keeps profit-banking and safety gates. More aggressive
exit and sizing ideas belong in Research ROI, still bounded and paper-only.

## What We Accomplished

### Two-profile strategy restart

- Created one fresh Live Stability paper ledger with `$1,000`.
- Created one fresh Research ROI paper ledger with `$1,000`.
- Fixed the Research ROI daily KPI at `$50`, measured against original capital,
  rather than allowing the denominator or target to drift.
- Kept Live Stability conservative and readiness-bearing.
- Allowed Research ROI higher bounded paper risk while excluding it from
  readiness and live goals.
- Removed the legacy motion book from recurring entry scans. Its retained
  positions still monitor and settle normally.
- Archived every prior or unknown account identity and rejected new-entry
  admission to archived or ambiguous identities.

### Accounting, identity, and replay correctness

- Added canonical account/profile identity across admission, orders, replay,
  reports, and readiness.
- Added exact active-order/ledger lifecycle reconciliation covering fills,
  resting and partial orders, terminal rows, missing rows, and orphans.
- Made deployment cutover require exactly two active fixed-capital reconciled
  ledgers.
- Published Strategy schema v3 with exact active ledgers, archived accounts,
  pending risk/count, and backend-provided `closing_equity`.
- Restricted readiness to valid live evidence across legacy and fresh live
  identities. Research evidence cannot cross into the live cohort.
- Made backend and frontend independently fail closed if active accounting is
  unavailable or malformed.

### Strategy Lab and recruiter-facing design

- Added **Achieved performance & profiles**.
- Separated Strategy attribution from Historical account balances.
- Added true total balance to profile summaries.
- Added account balance as the third tooltip metric when the backend has
  economic balance for that day; older attribution-only days remain honest.
- Disabled tooltip animation and cursor rendering to remove rapid-hover
  remnants and clipped borders.
- Improved profile naming, readiness language, current-book spacing, and the
  pending-limit empty state with the existing HeroUI Pro design system.
- Verified the deployed page at desktop and real 390 px mobile viewports.

### Timers, deployment, and workload reduction

- Replaced Strategy's completion-relative timer with a persistent five-minute
  `OnCalendar` schedule.
- Added a five-minute offset scheduler-health timer.
- Added a root-owned deployment-maintenance marker and recovery trap that
  either restores the captured timer policy or safely re-quiesces.
- Added unit-integrity, database/disk, artifact checksum, provenance, and
  local/public freshness checks.
- Limited automatic repair to Strategy/publication age-only failures.
- Kept the app-shell and runtime publication writers quiesced during the final
  web sync, then restored both timers and reran scheduler health.
- Preserved one short-lived local rollback snapshot plus its independently
  downloaded, integrity-checked, encrypted off-host copy. Normal retention
  removes old local snapshots.

### Security and maintenance

- Restricted NWS-advertised URLs to HTTPS, the exact expected host, no
  credentials, port absent or 443, revalidated redirects, and a 4 MiB cap.
- Fixed the scheduler's root/application lock boundary so validation and lock
  acquisition run as the unprivileged app user; the symlink non-truncation
  regression passes.
- Added a seven-day Dependabot cooldown for routine churn; security updates
  continue to bypass cooldown.
- Cleared stale ignored local runtime databases/data through the canonical
  cleanup script. AWS-generated runtime state remains authoritative.
- Removed redundant recurring motion scans and retained only data/history still
  needed for settlement, research, rollback, or operation.

## Verification Evidence

- PR #58 merged at
  `71ac845422fc75cc35e24bb3b3a918dd44f917b3`.
- GitHub CI: Python 3.12, Python 3.13, and Web (Bun) all passed.
- Full backend/forecaster suite: 2,469 passed, 8 skipped.
- Frontend suite: 153 passed.
- Deployment/watchdog suite: 146 passed.
- Independent focused review: 186 backend/security/deploy checks and 51
  frontend checks passed with no release blocker.
- Production build, lint, icon integrity, Python compile, AWS shell syntax,
  YAML parsing, diff checks, and Bun dependency audit passed.
- The deployment backup was uploaded encrypted, downloaded independently,
  checksum-matched, and passed SQLite integrity and foreign-key checks before
  source transfer.
- Independent post-deploy canary: 0 failures.
- Public app-shell checksum matched local `dist/` and EC2 `webdist`.
- Rapid-hover stress: one visible tooltip, one active dot, zero tooltip cursors.
- Natural five-minute Strategy cycles generated successfully at 10:40 and
  10:45 UTC with schema v3, current accounting, and live-only readiness. The
  latest was promoted in the public manifest at 10:50 UTC.

## Paper Performance Snapshot

These are paper results and research evidence, not promised returns. The table
stops at the last completed published day, 2026-07-25.

| Date | Legacy live strategy attribution | Cross-account Strategy Lab total |
| --- | ---: | ---: |
| 2026-07-20 | +$9.9765 | +$17.4301 |
| 2026-07-21 | +$9.91 | +$18.89 |
| 2026-07-22 | +$7.57 | +$18.22 |
| 2026-07-23 | +$0.83 | +$11.58 |
| 2026-07-24 | +$0.20 | +$7.95 |
| 2026-07-25 | +$0.00 | +$4.38 |

The cross-account column combines strategy-attributed outcomes from
economically separate historical paper accounts and must never be described as
one bankroll's return.

At the 2026-07-26 verification:

- Fresh Live Stability: `$1,000.00` realized equity, `$0.00` realized P&L,
  zero open positions, zero pending limits.
- Fresh Research ROI: `$1,000.00` realized equity, `$0.00` realized P&L, zero
  open positions, zero pending limits.
- Archived accounts: five.
- Archived open positions: 62 total; 10 target-v1 and 52 motion positions were
  still settling.
- Legacy live strategy attribution: `+$45.70` across 80 resolved positions,
  62 wins and 18 losses.
- The legacy shared live account's true all-time realized P&L remained
  `-$41.62`; it is deliberately not presented as the legacy live strategy's
  attributed balance.
- Research ROI's `$50/day` goal was not achieved on activation day and was not
  feasible from the current scan's conservative expected-profit evidence.

## Safety And Interpretation Rules

- Never enable real-money trading as part of an audit, recovery, target chase,
  or UI change.
- Never promise `$10/day`, `$16/day`, 5% per day, or any return. Targets are
  paper research KPIs.
- Never weaken a `NO_TRADE`, after-fee edge, loss-pause, exposure,
  calibration, liquidity, or evidence gate merely to increase activity.
- Report each economic account separately from strategy attribution and
  cross-account research totals.
- Never synthesize account balance from attributed P&L when the backend did not
  publish a true balance.
- Keep AWS access identifiers, credentials, network addresses, and key paths
  out of this file and all tracked project artifacts.

## Known Nonblocking Concerns

- The watchdog deliberately refuses to auto-repair disabled or drifted trading
  timers. That requires operator investigation; only age-only Strategy and
  operational publication staleness is safely repairable.
- LAMP and GFS-MOS NOAA archive paths previously returned HTTP 403 during
  backfill. IEM, Open-Meteo previous runs, NBM, HRRR, truth, and EMOS remained
  available. Replace or remove those fetch paths if the 403 responses persist.
- The runtime revision above is the deployed code revision. A later
  documentation-only commit containing this memory and the reviewer prompt does
  not require another 9.5 GB database-backed runtime deployment because it
  changes no deployed package, unit, SPA asset, or artifact generator.

## Next Session Checklist

1. Print the `Session Brief` above before taking action.
2. Confirm the checkout is clean and compare local `HEAD` with `origin/main`.
3. When production state matters, freshly revalidate runtime source, failed
   units, all 14 timers, unit integrity, manifest parity/freshness, disk, active
   ledger reconciliation, readiness scope, and real-money safety flags.
4. Keep the narrow owner SSH rule until the owner says access is finished.
5. Continue observing later natural Strategy refreshes and archived-position
   settlements.
6. Treat ignored local runtime files as disposable; use AWS/public artifacts.
7. Update this file after every material incident, deployment, policy change,
   deliberate deferment, or production verification.

## Memory Update Contract

Keep this document compact enough to print its opening brief, but detailed
enough that another engineer can understand the last failure without chat
history. When updating:

1. Timestamp the last production verification.
2. Replace stale status claims rather than stacking contradictions.
3. Record root cause, not only symptoms.
4. Record exact merged/deployed revisions and objective verification.
5. Separate completed work, deliberate retention, and true remaining work.
6. Preserve safety and P&L interpretation rules.
7. Never add secrets, exact access identifiers, key paths, or sensitive
   operator commands.
