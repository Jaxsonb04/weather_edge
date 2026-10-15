# October 15 Mac control and paper-loss update

The owner asked for a simple switch for using the Mac unplugged. WeatherEdge
Control is installed as a native app with a Desktop shortcut. Both buttons were
operated through the native UI. Final state is OFF: persistent launchd disable,
unloaded schedule, durable private pause marker and idle shared worker lock.
The app adds no background monitor. On resumes the same two-hour schedule with
no immediate run. Existing AC, thermal, load, memory, half-core pacing, numeric
thread and provider limits remain unchanged. The pause marker cancels scheduled
child work through the existing process-group cleanup; the controller waits for
idle and reports an unfinished stop rather than claiming success. Reinstall
preserves the saved pause. An explicit manual maintenance/restore command remains
separate from scheduled work and requires its existing native resource admission.

Focused validation passed 147 tests and 74 subtests, bytecode compilation and
diff checks. Real owned-child fixtures verified cleanup after pause and after a
simulated mid-run loss of AC power. Native app clicks verified on/off, including
an unloaded-job bootout returning macOS code 3; only that operation accepts it,
and success still requires readback. No provider call or AWS mutation was used
to validate the switch. The older scheduled SIGSTOP permission incident remains
unresolved; this control does not claim to repair that failure.

A read-only AWS inspection at 14:15 PDT found the same clean September 4 runtime,
exec-v4, no V7 vintage/member tables, real execution off/dry-run on, no failed
units, no maintenance marker and 29 canonical units without drift. The paper DB
is 37.29 GiB with 13.53 GiB free. Bounded recovery PR #147 merged into source at
`4a089719c` after exact-candidate Python 3.12/3.13 and Web checks passed. There
has been no production source cutover, paid backup upload or history import.

A fresh same-transaction, query-only financial summary at 14:27 PDT read 3,557
orders and 8,591 ledger rows within explicit CPU, memory, row and wall ceilings.
Booking dates below use PDT. Accounts are economically separate; fee columns
are already included in net P&L.

| Paper account | October 12–14 net | Included fees | October 5–14 net | October 15 partial | Realized ledger equity |
| --- | ---: | ---: | ---: | ---: | ---: |
| Live Stability | −$3.64 | $1.10 | −$38.42 | +$2.19 | $1,033.4485 |
| Research ROI V6 | −$31.2722 | $3.21 | −$52.7794 | +$3.48 | $970.3159 |

Both equities independently match opening cash plus retained order realized P&L.
Open cost basis includes partial-expired filled exposure; outstanding reservations
are zero. Market marks remain unavailable. This bounded summary is not a fresh
full lifecycle reconciliation of every archived account; October 11 remains
that last complete reconciliation.

Indexed bounded traces of ten losing close lots, eight logical roots, confirm:

- Boston October 12: model confidence near 93% vetoed the ordinary stop at a
  $0.47 bid, followed by catastrophic closes at $0.34. Live lost $1.75 and V6
  $4.0767 on their separate positions. The model read was under a minute old;
  a recent read does not establish accurate underlying weather probabilities.
- Denver October 14: the model still assigned about 91% while a $0.39 bid
  triggered a normal-stop veto. V6 later closed two lots at $0.21 and $0.24,
  losing $11.55 in the same logical position. Both closes booked shortly after
  midnight PDT on October 14.
- Houston October 13: V6 paid $0.70 for 63.96 NO contracts, then closed 24 at
  $0.46 and 39.96 at $0.45 under normal stops, losing $16.87 after exit fees.
  This was a concentrated position loss, not two independent trades and not a
  model-veto example in the retained recent trace.

These observations establish recorded exit mechanisms and loss concentration.
They do not prove disabling the veto is profitable, quantify avoidable dollars
or establish what undeployed V7 would have earned. No risk thresholds or
promotion gates were relaxed. Real-money allocation remains zero.

The confirmed AWS limit remains $40 total per calendar month, with the approved
one-attempt $1 recovery allowance inside that total. After fresh owner sign-in,
October 15 billing shows $21.23 month-to-date. Its summary forecasts $49.84 and
Budgets forecasts $44.19; both exceed $40 before recovery. The existing
whole-account monthly COST budget was changed from $28 to $40 and independently
read back through the API and console. Notifications were preserved and there
are zero budget actions. This is an alert budget, not a hard invoice cap.

The existing server still bills $0.04/hour, gp3 $0.096/GB-month, with standard
CPU credits and no elastic IP allocation. Fresh existing S3 controls retain
35-day snapshots plus seven noncurrent days and 90-day journals plus thirty
noncurrent days; versioning is Enabled. Daily standard-storage metrics were
144.66, 144.73, 117.29 and 89.41 decimal GB on October 11–14. Existing lifecycle
expiration explains a declining footprint; this task did not delete retention.

A same-memory, same-disk Spot proposal uses an explicit $0.018/hour ceiling,
standard CPU credits, retained 8-GB temporary boot storage, existing IPv4/S3,
the approved $1 attempt and a reserve. Its conditional October estimate is:

| Proposed October component | USD |
| --- | ---: |
| Already billed | 21.23 |
| Unbilled usage and bounded transition allowance | 1.25 |
| Up to 400 remaining Spot hours | 7.20 |
| Remaining original 64-GB gp3 storage | 3.31 |
| Remaining IPv4 | 2.00 |
| Existing S3 storage and requests, remaining month | 2.85 |
| Retained temporary 8-GB boot volume | 0.42 |
| Previously approved single recovery attempt | 1.00 |
| Contingency | 0.50 |
| **Conditional total** | **39.76** |

The original on-demand server must stop no later than 18:00 PDT October 15 for
this estimate; recompute it if that deadline is missed. Existing S3's remaining
storage envelope assumes at most 200 decimal GB, separate from new backup
storage covered by the $1 attempt. This is a planning estimate, not an actual
spend policy or guarantee. A full later 31-day month under these assumptions
is about $31.03. The current-zone Spot quote was $0.0178/hour when read.

Owner acceptance of Spot interruption and missed paper/forecast cycles is
pending. The proposed old server would remain stopped if cutover failed, with
the original journal disk retained for rollback. Final recovery, cost, capacity,
source identity and runtime checks remain mandatory; no infrastructure change
or shutdown has been executed or scheduled. The Mac is currently on battery,
so its native admission blocks independent recovery until connected to AC.
The background switch remains OFF through any explicit manual maintenance.

Mac control PR #153 merged at `1b3c40b4a` and exact merged-main Python 3.12/3.13
and Web CI passed. No budget-fit policy is fabricated. V7 activation and
prospective bankroll qualification remain unfinished.
