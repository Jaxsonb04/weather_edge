# V7 priorities and recent paper losses

Audit started October 11, 2026, 13:03 PDT. All operational observations are
dated snapshots. Private exports, access details and full receipts remain in
ignored operator state. These are separate paper experiments, not real-money
returns or one combined bankroll.

## Fresh production evidence

The 13:03–13:05 PDT read-only inspection found clean backend `2a6432e3`,
execution `exec-v4-2026-07-17`, no V7 weather-vintage/member tables, fourteen
application timers, zero failed units, successful canonical-unit integrity and
no maintenance marker. Real-money execution is disabled and dry-run enabled.
The paper journal is 34.63 GiB with 16.62 GiB free; the canonical backup plus
1-GiB headroom gate is short approximately 19.01 GiB.

Public snapshot `25a091e1d77fad165df2e45f` passed all five runtime hashes against
an unchanged manifest. Fresh artifact generation does not make the September 4
historical analysis current. The installed research account is still V6. The
September 27 Phoenix final-truth backlog still contains three positions.

## Recent losses

The full read-only order export contains 3,472 order rows and ten economically
separate account histories. Logical-position grouping retains partial-close
lots, fees and unassigned older attribution. Calendar windows below use PDT
booking dates, include zero days, and exclude the current partial day.

| Separate active paper profile | October 5–10 net P&L | Positive lot P&L | Negative lot P&L | Included entry/exit fees | Realized ledger equity at audit |
| --- | ---: | ---: | ---: | ---: | ---: |
| Live Stability | −$38.21 | +$0.99 | −$39.20 | $2.67 | $1,033.29 |
| Research ROI V6 | −$19.6772 | +$9.31 | −$28.9872 | $2.72 | $1,001.16 |

Each ledger began with $1,000. The fee column is already included in net P&L;
it must not be subtracted twice. October 11 partial realized P&L is +$1.82 and
+$1.22 respectively. Indicative profile-summary unrealized marks are −$1.28
and −$0.49, with five and eleven open positions. Authoritative account accounting
reports partial mark coverage and leaves account unrealized P&L and marked
equity unavailable; those summary marks cannot establish total account equity.

Live Stability's recent city attribution is Dallas −$13.95, Atlanta −$12.54,
Austin −$8.02, New York −$4.69, Los Angeles +$0.41 and Oklahoma City +$0.58.
Thirteen of its sixteen resolved execution lots explicitly record stop-loss
exits; three record take-profit exits. Partial lots are not independent trades.
V6's recent Dallas attribution is −$19.44. Its full retained record has
$513.7007 positive logical-position P&L and −$512.5426 negative P&L, leaving
$1.1581 net. Its 197 wins out of 274 resolved logical positions (71.90%) are
only just above the 71.85% break-even hit rate implied by the observed average
$2.61 win and $6.66 loss. Requested size remains weak execution evidence:
only 4.66% of V6's cumulative requested contracts filled. These retained
account statistics span three strategy fingerprints and are descriptive,
not qualification evidence for one frozen policy.

Bounded indexed monitor queries for nine losing roots retained fresh model
reads and explicit actions. Austin's October 5 close lost $8.02: a preceding
normal stop was vetoed with model probability near 96%, before the NO bid fell
to $0.36 and a catastrophic stop fired. Dallas's October 7 V6 close lost
$12.68: the model still assigned about 80% while the bid fell to $0.37, vetoing
the normal stop, before it closed at $0.30. New York's October 10 closes show
the same model-veto/catastrophic-stop sequence. Model reads in these examples
were minutes old, not stale timestamps. These traces establish the recorded
exit mechanism; they do not establish that disabling the veto would improve
future returns or quantify avoidable loss. The sampled recent roots do not
establish a new partial-loss-memory incident.

The deployment lag is independently verified. The installed backend lacks the
reviewed source corrections for probability/distribution matching, incomplete
weather inputs, source mixing, conservative sizing, partial-loss memory and
finite execution depth. Their presence in current source is not evidence of
already improved production P&L. No thresholds, stops, exposure limits or
promotion gates are relaxed in this task.

## Completion boundaries

At the start of this audit, all three requested priorities remained incomplete.
The work below must distinguish code validation, actual data collection,
backend cutover and prospective profit evidence.

- Five-city reconstructed training must preserve provider attribution, raw
  response hashes and actual current retrieval clocks. Historical values cannot
  become original issued vintages or prospective trading outcomes.
- A budget-compatible backup must retain durable encrypted off-host recovery,
  independent checksum/integrity/foreign-key verification and cutover-time
  frozen database identity. Compression estimates alone do not prove a zero
  spending increase or satisfy the restore gate.
- V7 applies correctness fixes to both active profiles. Live Stability retains
  its economic account with a new evidence cohort; Research ROI starts a new V7
  account while V6 exposure settles. Archived profiles remain entry-frozen.
- Bankroll qualification requires one account/policy's realistic prospective
  net fills, fees, gross losses, exposure and completed independent weather
  dates. Earlier V6 results and separate Mac weather-only observations cannot
  satisfy that contract. Significant profit improvement cannot be verified on
  the day a previously undeployed generation begins collecting evidence.

Implementation, collection and deployment outcomes will be recorded here after
their respective verification gates.

## Verified training and report recovery

Bounded local collection retrieved Open-Meteo previous-runs forecasts and IEM
CLI archives with eighteen total free GETs, never exceeding eight per batch or
the shared 96-request UTC-day ceiling. Native AC, thermal, memory, load and
process-group budgets remained in force. The local schedule was idle and
temporarily unloaded before source maintenance, preserving its enabled
preference, plist, two-hour interval and existing limits.

Las Vegas, Minneapolis, San Antonio, New Orleans and Washington DC each now
have 120 matched final-truth dates at both leads 1 and 2, with eight named
models per date: 9,600 new source-attributed reconstructed rows. Independent
read-only joins verified all counts, no missing raw-response references and
zero foreign-key errors. Raw HTTP hashes, registered station/model scope,
24-hour completeness, provider attribution and actual retrieval clocks are
retained. These are historical training values first available locally today;
unknown original initialization/publication clocks remain unknown, and none
is relabeled as an originally issued forecast.

The four older Mac truth gaps now have original archived NWS CLI text and
hashes: Los Angeles October 5, 101°F; Austin October 6, 90°F; Denver October 6,
87°F; and Denver October 7, 81°F. Two valid record highs exposed a concrete
parser defect: the official observed tokens `101R` and `87R` were rejected by
both duplicated parsers. NWS's `R` record-set/tied flag is now accepted only on
a complete integer observed token; missing values, unknown suffixes and
fractions remain rejected. Retained raw reports were replayed under the native
budget with zero additional GETs. Washington DC's archive missing-value
sentinels are explicitly skipped and counted rather than converted to numeric
truth. Phoenix September 27 still has explicit `M` in its archive; no numeric
final value was recovered, and its three AWS positions must remain unresolved.

This training is verified in the separate Mac store. AWS onboarding is a
separate guarded import and is not complete merely because these local rows
exist. Likewise, repairing parser source does not install it on the backend.

## Implemented recovery and decision-evidence controls

The opt-in compressed backup leaves canonical same-volume behavior unchanged.
It requires current-month hashed no-increase-spend evidence before any remote
quiescence, a maximum 8-GiB compressed archive, encrypted/versioned durable S3,
an independently downloaded matching manifest and actual Mac raw restore with
checksum, full integrity and foreign-key verification. It serializes against
the installed local research worker, enforces native/FileVault/resource guards,
and proves unchanged checkpointed database identity immediately before the
first source transfer. Failed attempts retain conservative egress reservations.
No capacity increase, journal deletion, IAM change or retention relaxation is
part of the change. Historical Strategy Lab analysis remains explicitly deferred
when the verified raw recovery snapshot exists only offhost.

The optional deployment import independently replays the SHA-pinned bundle's
original raw responses before any remote action and again before applying it.
It imports only reconstructed weather training, final observations and their
append-only source receipt under the quiesced maintenance gate. Fourteen
pre-existing NWS observations were corroborated by exact retained IEM responses
with zero new requests; their original canonical source and clocks remain
unchanged. The transferable bundle contains all 9,600 forecast rows and 600
final observations, with 120 matched days for both leads at every added city.
Its content SHA256 is
`7e420f6fe09ec1e461c112ccb94b1659dc04bd6602e76ae67a532347f3b671f6`.
Issued forecast tables, Mac research identities and paper ledgers are excluded.

Every new trading decision can now retain a compact receipt linking its exact
served distribution to content-verified immutable member/fit/training evidence.
Missing provider initialization, original hourly completeness, truth-publication
clocks and serving-code provenance remain explicit: linkage is unqualified and
cannot approve promotion. No probability, stop, sizing or exposure threshold was
relaxed for this receipt.

The local full verification gate passed 3,847 tests, ten skips and 74 subtests,
plus Python compilation. Local Semgrep was unavailable; exact-candidate CI must
supply the required static-analysis gate. The final affected gate passed
195 tests, including all 137 deployment-shell tests and the nine later
optional-import cases, with shell syntax and diff checks clean. An independent
review also recomputed all recent-loss, fee and logical-position totals.

Actual AWS recovery and V7 cutover remain blocked on fresh billing/remaining
allowance evidence: the existing operator SSO session expired, the instance
role cannot read Free Tier usage, and the browser remains at sign-in. No
cost policy has been fabricated and no AWS mutation has occurred. Archive-only
pruning must be made explicit before cutover because the installed and current
source defaults differ. Both active paper profiles will use V7 correctness;
archived profiles remain entry-frozen and V6 filled exposure must settle.
