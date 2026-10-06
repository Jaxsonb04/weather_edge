# V7 and hybrid-compute verification

Audit observed October 5, 2026, 23:02–23:05 PDT. These are dated snapshots,
not assertions that production will remain in this state. The checks used
bounded public downloads and read-only SSH inspections. No AWS API billing,
S3 upload/download, service start, timer change, policy change or deployment ran
as part of this audit. Private access details and full capture receipts remain
in ignored operator state.

## Result

V7 is present in the local merged source but **has not reached the installed
backend**. The public dashboard artifacts still represent the earlier backend
and Research ROI V6. New append-only served-forecast evidence is not being
collected by that deployed source. The static V7 frontend must continue to
distinguish its dated source audit from the active runtime generation.

Real-money execution remains disabled, and the public readiness result is
`ANALYSIS_STALE`. An October 31 engineering-review target cannot establish a
profitable algorithm or override missing evidence. V7 changes both the live
strategy fingerprint and execution semantics; the unchanged readiness gate
requires at least thirty independent post-boundary weather days. A verified
cutover after this October 5 snapshot cannot accumulate thirty such days by
October 31. Earlier V6 results must not satisfy the new generation's clock.

## Fresh public artifact evidence

At 23:02:53 PDT, the manifest bytes were identical before and after downloading
all five JSON artifacts, and every artifact hash matched the manifest. The
publication itself was timestamped 23:02:26 PDT. Snapshot identifier:
`ff2368dce6b44a0d407592dc`. Manifest SHA-256:
`1e6112631c7fa157c8bc41b331fd89c146cd9014a983112b0802beb66a4f705a`.

| Public artifact | SHA-256 |
| --- | --- |
| `trading_signal.json` | `66165b3a336a8f10f5c215b263550fa1e1f8aa8b99639f809a917859926f23ca` |
| `forecast_data.json` | `b6ee97371a24bf467ad3543a710b588868a65cba6cc80878e7781d31e2ff060d` |
| `weather_story_data.json` | `f5587b28e6b6c6b63ff69eaf91afcb063d91dab5eac2001db363715f63af9048` |
| `strategy_research.json` | `08bb6a85a07d5283837c3394d04f32b3539cc4fc97f55dfafffab3ad97c2de25` |
| `cities_data.json` | `0a2811223bd80a1ef1b70c25d13abc11f3632fac38946dca68667ca88f75c78d` |

The manifest identifies clean backend source
`2a6432e3bdb29fa1798a4b07e5f5396685b5245b`, synchronized September 4 at
21:27:51 PDT, and execution generation `exec-v4-2026-07-17`. Strategy Lab was
generated at 23:00:52 PDT, but its historical analysis still dates September 4
at 21:36:08 PDT: 745.41 hours old against a 36-hour maximum. Artifact delivery
freshness does not make the historical analysis fresh.

The public Research ROI target policy remains V6. The Strategy artifact has no
V7 `release` payload. The coverage artifact contains fifteen cities; local
source configures twenty. Both Strategy and signal declare paper-only mode and
`live_orders_enabled: false`.

## Installed source and evidence capture

The read-only host check at 23:04:17–23:04:18 PDT independently confirmed the
same backend source marker. Installed `account.py`, `maker_fills.py`,
`research_policy.py` and `emos_forecast.py` differ from their local V7 versions.
The installed tree has neither `forecaster/live_forecast_evidence.py` nor
`trading/sfo_kalshi_quant/strategy_lab/release.py`. Neither
`forecast_emos_live_vintages` nor `nwp_live_forecast_members` exists in the
authoritative weather database. This is direct evidence that the V7
prospective member/vintage integration has not run.

The paper database contains ten economically separate ledgers. The active
ones are Live Stability and Research ROI V6, each with $1,000 initial virtual
capital. V7 has no initialized ledger in this snapshot. Public account
reconciliation reports no mismatches. Live Stability realized equity is
$1,053.67, with two open positions; Research ROI V6 realized equity is
$1,017.93, with five open positions. These are two separate experiments;
their capital or P&L must not be added into a single investor account. Their
marked equity is unavailable in this capture. Older zero and negative
research eras remain published separately.

The unit-integrity command returned zero, no systemd unit was failed, and no
deployment-maintenance marker existed. All fourteen inherited timers were
enabled and active, including the Apple refresh timer that current source
documents as retired. This discrepancy needs explicit correction at guarded
cutover; it is not evidence that the retired provider has predictive value.
Selected installed safety settings confirmed live trading disabled and live
dry-run enabled. The installed prune wrapper defaults to `archive-only`, and
the environment does not override that setting. A V7 installer must preserve
this deliberate safety state rather than inherit the newer source default.

## Capacity and recoverability

At 23:04 PDT, the live paper journal was 33,329,750,016 bytes (31.04 GiB), its
WAL was empty, and the filesystem had 21,610,119,168 bytes (20.13 GiB) free.
The existing canonical backup gate requires a same-volume consistent SQLite
snapshot plus 1 GiB headroom. Its shortfall was therefore 12,793,372,672 bytes
(11.91 GiB). That gate remains unsatisfied by available capacity.

The subsequent resource inspection found no full SQLite backup/checksum pair
in the local backup directory. That directory occupied approximately 36 KiB;
the disposable Pages clone occupied approximately 16 MiB and the deployed
app approximately 2.5 MiB. Even removing these disposable items would not close
the shortfall. The retained compressed journal archive occupied approximately
1.61 GiB and is research evidence, not a disposable cache. It also cannot
provide enough space. No existing same-volume recovery path was demonstrated.

A prior transaction-aware Mac replica has a completed receipt, verified
October 5 at 22:54:58 PDT. It records a 33,322,299,392-byte database,
`integrity_check: ok`, `foreign_key_check: ok`, and SHA-256
`09b2f5672b0af3927b2f0cc7bf6ad9bed99cb7868db5ad33bb7067e0cc23afee`.
The current local file size matches that receipt and its WAL is empty. This
audit did not repeat the expensive full-file hash/integrity checks. The
receipt explicitly does **not** satisfy the existing durable S3
upload/independent-restore gate. It must not be silently promoted as a backend
deployment backup.

A no-expansion design may prepare and validate snapshots on the Mac, but a
replacement cutover contract still needs tested origin transaction identity,
fresh quiesced lineage, independent recovery checks and durable encrypted
recovery. Full snapshot uploads, downloads and additional retention incur
possible AWS charges; none was authorized as a workaround under the user's
strict no-increase constraint. The present evidence supports retaining
archive-only production and deferring backend activation until a validated,
budget-compatible recovery path exists. Full history must be preserved.

The host is a `t4g.medium` with two CPUs and approximately 3.74 GiB RAM. A
quiet instantaneous snapshot showed about 3.28 GiB available and a one-minute
load of 0.63. Those readings do not establish the nightly peak or justify
downsizing. Local offload reduces the work assigned to AWS; it does not reduce
the fixed instance charge until a separately verified capacity change occurs.

## Source correctness and hybrid integration

Fresh focused validation ran on Python 3.13 with numerical-library threading
limited to one thread and low process priority: **107 tests passed in 3.34
seconds**. The selection covered named append-only forecast vintages,
rejection of replacement/update/delete, missing provider clocks remaining
unknown, source-separated NWP loads, archive input integrity, EMOS serving,
V7 ledger preservation, maker tape isolation, partial-loss memory and account
cutover. This is a focused correctness result, not a full-release gate or
proof of profitable execution.

The inspected V7 source freezes V6 numeric policy controls, starts a distinct
research identity, rotates execution to `exec-v5-2026-10-05`, and expires old
unfilled remainders while preserving already filled history. The forecast
writer preserves actual response retrieval clocks and deliberately leaves
provider initialization and constituent-hour completeness unknown. Its
historical reconstructions remain research diagnostics.

The pre-change Mac exporter only exports reconstructed NWP/CLI/EMOS tables;
it omits the new original served-vintage/member tables. A hybrid research
worker must add bounded, optional export of those tables after V7 activation
and distinguish an absent table from an empty prospective cohort. Historical
weather rows must never be relabeled as original vintages. A model evaluator
must use the append-only issued evidence, whole-date validation blocks and
account-scoped trading observations; all diagnostic outputs must remain
non-promoting until their own verified evidence contract passes.

The current static frontend resolves the active Research ROI version from the
artifact policy rather than hard-coding V7. Its fallback links to the dated
V7 audit while explicitly stating that the runtime version is reported above.
Source inspection establishes that intended distinction; the parent task
owns fresh desktop/mobile browser verification and any static deployment.

No additional critical defect in the inspected V7 correctness paths was
demonstrated by these focused tests. Unimplemented production cutover,
prospective data collection, full-journal analysis refresh, capacity recovery,
retired-provider scheduling, and paired execution validation remain concrete
gaps. They must be presented as remaining work, never as completed V7 profit
evidence.

## Follow-up: tested hybrid corrections and separate local collection

Independent review demonstrated and corrected three hybrid implementation
defects: the member exporter filtered a nonexistent member-table date column
instead of joining its parent vintage; nonfinite resource limits could disable
a guard; and moving-block intervals could silently exclude isolated adverse
dates. Regression fixtures now cover the actual production member schema,
finite numeric policies, and complete calendar-date support. Source-change
checks, snapshot/input hashes, shared locks and explicit nonpromotion were also
added to the offline workflow. These corrections do not activate the backend.

`scripts/local_compute/shadow_collect.py` provides a separate Mac-only
educational weather experiment while guarded AWS cutover remains deferred.
It uses current named-model daily maxima and the existing V7 EMOS serving code
to append actual issued distributions and member evidence. The immutable
`local-shadow-v7-v1` lineage binds source/file hashes, historical seed hash,
model policy and original snapshot identity; provider initialization and hourly
coverage stay NULL. It never imports old issued-vintage tables from its seed,
and historical NWP/CLI/EMOS rows retain their reconstructed identity and source.
The initial seed is imported once. A later changed seed path does not replace
that imported history or its original receipt. An implementation or
training-policy change therefore remains a new model-policy cohort. Git commit
and dirty-state metadata stay in each immutable lineage row, full policy and
receipt, but are excluded from the behavior fingerprint: documentation-only
commits do not fragment otherwise identical forecast cohorts.

Only the documented unauthenticated HTTPS hosts for Open-Meteo and NWS are
allowed. Redirects, paid endpoints, credentials, retries, AWS writes and orders
are absent. The collector reserves failed requests in a durable daily counter,
allows at most eight requests per attempt and ninety-six per UTC day, limits
each response to 1 MiB, and has a five-minute internal runtime ceiling. Its CLI
requires the reviewed worker resource budget. State, raw report/response
evidence and receipts live in a separate ignored private directory; canonical
local weather and trading stores are not destinations.

Open-Meteo's free endpoint permits disclosed noncommercial educational research
under its stated rate limits. Commercial products or undisclosed commercial
research require a separate provider decision; the collector has no paid
fallback and is not approval for future real-money use. Required Open-Meteo
attribution is included in its policy and receipt. NWS documents public free
API access and requires an identifying User-Agent, which the collector sends.
[Open-Meteo terms](https://open-meteo.com/en/terms),
[Open-Meteo forecast documentation](https://open-meteo.com/en/docs),
[NWS public API documentation](https://www.weather.gov/documentation/services-web-api).

For each selected city the collector makes one forecast request and one latest
NWS station CLI request. A configured four-city batch may rotate over all
twenty **source** cities, preserving an explicit cursor. At six-hour scheduling
that revisits a city approximately every thirty hours. Latest-only CLI retrieval
can miss an intervening final report, so missing outcomes remain visible gaps.
The station identity must match, a preliminary report never becomes final,
and a final report's climate day must have ended. The collector does not claim
complete prospective outcome coverage or thirty independent completed days.

The first real SFO sample ran October 5, 23:24:52–23:24:54 PDT
(`2026-10-06T06:24:52.351017+00:00` through
`2026-10-06T06:24:54.929507+00:00`). Native admission confirmed AC power, normal
thermal state and eighty percent reported memory headroom. Exactly two public
requests produced three actual issued lead-0/1/2 distributions. The latest
October 5 CLI report was correctly retained as nonfinal. The budget observed
3.241 seconds elapsed, 1.15 CPU seconds, 262,995,968 bytes maximum sampled
aggregate RSS and one numerical thread. No AWS connection ran. Export SHA-256:
`0d2020a971d87d0a8f1e06edd4cd0710be4a08a259ca688ef45d4d83b20c4b88`.

That initial sample preceded final dependency-hash and seed-validation
hardening; its original receipt remains unchanged and is not relabeled as a
later-source run. Fresh focused collector validation subsequently passed
**26 tests in 0.54 seconds**, including real V7 fitting, production evaluator
hash compatibility, append-only protection, unknown-field preservation,
preliminary/wrong-station handling, malformed seed provenance, import mutation
rollback, four-city rotation, destination isolation and public request limits.
The parent task owns the final installed-worker observation and publication
verification. Local shadow weather scores, historical ML results and AWS paper
fills remain separate populations, with live-money allocation zero.

Final integration review found and corrected supervisor-termination cleanup:
SIGTERM and SIGHUP now enter group cleanup instead of leaving a separately
sessioned compute child unpaced. The actual signal regressions pass for both
termination forms. Final combined focused validation passed **101 tests and
31 subtests in 2.15 seconds**, covering the collector, evaluator, forecast
evidence audit and local-compute guards. The allowlisted public research summary
matches its report SHA-256, and that report binds the current evaluator hash.
The UI now explicitly says original AWS V7 vintage capture requires guarded
backend activation, alongside its separate local shadow description.

The final fingerprint correction excludes only `source_commit` and
`source_dirty` from its hashed policy; actual serving/dependency hashes, seed,
model selection, method and other behavior policy remain bound. Existing
collected records were not changed or relabeled. Regression checks show that a
docs-only commit preserves the cohort while a serving implementation, method,
model list or seed change rotates it, and that resulting immutable records
remain compatible with the production issued-vintage evaluator. After this
narrow correction, **103 tests and 31 subtests passed in 4.18 seconds**. No new
provider request or AWS action ran for this correction.
