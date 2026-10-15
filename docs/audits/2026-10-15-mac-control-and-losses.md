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

Focused validation passed 146 tests and 74 subtests, bytecode compilation and
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
one-attempt $1 recovery allowance inside that total. The last verified billing
snapshot, October 12, was $17.55 month-to-date and a $43.37 full-month forecast,
above the cap before recovery. The October 15 console requires sign-in again;
the owner has been asked to refresh it. No budget-fit estimate or cost policy
is fabricated. V7 activation and prospective bankroll qualification remain
unfinished. The owner's Mac OFF preference must survive subsequent maintenance.
