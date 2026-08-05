# Research ROI v6 case study — 2026-08-05

**Status:** audited intraday paper-trading snapshot, published at 2026-08-05
15:08 PDT. This is one economically isolated Research ROI ledger. It is not the
Live Stability ledger, not a combined Strategy Lab total, and not evidence of a
repeatable 5% daily return.

## Result

Research ROI v6 realized **+$44.5025** on 2026-08-05 Pacific time. That is
**4.45025% of the fixed original $1,000 reference equity** and **$5.4975 short**
of the $50 paper-research objective. The public artifact therefore correctly
reports the objective as a miss, even though this was the strongest v6 day so
far.

The day's resolved book was seven wins and no losses: six official settlements
and one profitable pre-settlement close. It resolved $181.59 of entry capital,
for a separate **24.5069% return on resolved capital**. That denominator must
not be confused with the fixed-$1,000 daily KPI.

At the snapshot, the ledger had:

- $1,038.2389 of realized equity, or +$38.2389 lifetime v6 P&L;
- four open positions with $10.49 of cost basis and about +$0.99 unrealized P&L;
- two pending maker requests reserving $178.46; and
- a reconciled account ledger with zero reconciliation difference.

## Where the $44.5025 came from

| City | Target | Position | Exit path | Realized P&L |
|---|---:|---|---|---:|
| HOU | Aug 5 | 94–95 °F — NO | take-profit close | +$35.6927 |
| SEA | Aug 4 | 90–91 °F — NO | settlement | +$0.6500 |
| SEA | Aug 4 | 88–89 °F — NO | settlement | +$2.8800 |
| PHX | Aug 4 | 113–114 °F — NO | settlement | +$0.2617 |
| CHI | Aug 4 | 80 °F or below — NO | settlement | +$3.7200 |
| ATL | Aug 4 | 90–91 °F — NO | settlement | +$0.4581 |
| ATL | Aug 4 | 83 °F or below — NO | settlement | +$0.8400 |
| **Total** |  |  |  | **+$44.5025** |

The Houston position contributed **80.20%** of the day. The other six positions
contributed $8.8098 together.

This was a **resolution-date result**, not an entry-day cohort. The six Aug 4
targets were admitted on the Aug 3 objective day and officially settled on Aug
5. The Houston Aug 5 target was admitted on Aug 4 and closed on Aug 5. One new
position opened on Aug 5, but it did not create the realized gain above. Any
future comparison must keep admission day, target day, and resolution day
separate.

## The Houston trade

The large winner was a day-ahead NO position on Houston finishing in the
94–95 °F bracket.

| Event | Evidence |
|---|---|
| Maker request | 147 contracts at $0.61, placed Aug 4 at 16:31 PDT |
| Fill | 94.1 contracts, allocated from tape between roughly 16:35 and 16:45 PDT |
| Capture | 64.01% of the request; a partial fill, not a full request |
| Forecast context | 97.04 °F forecast high, 2.2 °F source spread, 24-hour lead |
| Probability context | 0.8575 model probability versus 0.6311 market probability |
| Conservative gate | 0.6607 lower-bound probability and +0.0507 lower-bound edge |
| Entry market | $0.60 bid / $0.64 ask; the system rested at $0.61 |
| Exit | $0.99 bid at Aug 5 12:55 PDT; $0.989306 net after exit fee |
| Realized P&L | 94.1 × ($0.989306 − $0.61) = **+$35.6927** |

The path was not smooth. During 548 HOLD observations, the displayed bid fell
as low as $0.27. The marked net exit was about $0.2562, implying roughly
**-$33.29 unrealized P&L and a -58% drawdown** on this position. The normal NO
stop threshold had fired, but the existing fresh-model veto retained the trade
because updated model support remained materially above entry. The loss stayed
just inside the unconditional **-60% catastrophic floor**; crossing that floor
would have forced the close regardless of the model. The position later closed
when the net bid reached the updated model-fair-value take-profit level.

That risk path is part of the result. Replicating only the final profit while
ignoring the drawdown would be dishonest and unsafe.

## What v6 changed—and what it did not

For this one fixed placement, replaying the same public tape with the historical
position budgets produces this request/capture curve:

| Policy-sized budget | Requested at $0.61 | Tape-capped fill | Same-trade P&L |
|---|---:|---:|---:|
| v4 — $30 | 49 | 49.0 | +$18.5860 |
| v5 — $45 | 73 | 73.0 | +$27.6893 |
| v6 — $90 | 147 | 94.1 | +$35.6927 |
| Above v6 — $105 to $180 | 172 to 295 | 94.1 | +$35.6927 |

This is a **fixed-placement maker request curve**, not a full counterfactual
policy backtest. It says v6 captured $8.0034 more than v5 on this opportunity,
but size above v6 would not have captured another contract from the observed
tape. Larger requests would only have reserved more shared capacity.

Across the audited v6 trade-through fills, only **2 of 18 positive-fill maker
requests reached their full requested size (11.1%)**. Houston was not one of
them. That is interesting capacity evidence, but 18 fills and one concentrated
winner are not enough to activate a v7 policy.

## The replication protocol

The safest way to try to reproduce this mechanism is to leave v6 running
unchanged and measure it prospectively:

1. **Keep the v6 geometry frozen:** $90 position, $180 city-target, $360
   region-day, $750 aggregate, and $150 daily-loss pause on the isolated $1,000
   research ledger.
2. **Keep the opportunity gates frozen:** day-ahead targets only; non-negative
   after-fee point and lower-bound edge; no same-day relaxation to manufacture
   volume.
3. **Keep maker breadth:** scan every five minutes, improve the bid by one cent,
   and allow each individual quote a 15-minute lifetime. A filled position may
   remain open much longer; the quote itself does not rest for 6–12 hours.
4. **Keep execution honest:** cross only when the natural price or displayed ask
   can absorb the full intended size while both edge gates remain valid. Do not
   infer extra fills from displayed depth or assume a large request fills.
5. **Keep the existing two-minute exit monitor:** model-fair-value take-profit,
   fresh-model NO stop veto, and the unconditional -60% catastrophic floor.
6. **Measure every request, including zero fills:** preserve point-in-time tape,
   queue, policy fingerprint, execution version, monitor thresholds, entry
   decision, and exit/settlement evidence. Report both positive-fill and
   all-request denominators.
7. **Predeclare any scale experiment:** use the same prospective window and all
   eligible v6 requests, then report leave-Houston-out and leave-one-day-out
   results. Never choose a multiplier after seeing which one won.

The operating system is already using this recipe. There is no justified
runtime change to make from this day alone. The unrepeatable inputs—the market's
mispricing, the public tape available at the maker price, and the weather
outcome—cannot be forced. What can be replicated is the disciplined placement,
partial-fill capture, breadth-preserving caps, and exit discipline.

## Evidence limits and promotion rule

The complete v6 daily realized series through this snapshot is:

`-$0.0086, -$16.4757, +$6.4512, +$1.2050, +$2.5645, +$44.5025`

Its mean is +$6.3732/day, its median is +$1.8848/day, and its deterministic
day-cluster bootstrap 95% interval is **-$6.7367 to +$23.5259/day**. The interval
crosses zero, and v6 has hit the $50 objective on **0 of 6** observed days.

Therefore:

- preserve v6 and collect more prospective evidence;
- do not describe this as a repeatable 5% daily return;
- do not raise size, shared caps, the daily-loss pause, or the catastrophic
  floor from this case study; and
- do not promote a candidate policy unless it survives concentration checks,
  complete point-in-time replay, after-fee accounting, and a predeclared
  out-of-sample window.

## Sources and audit boundary

Evidence pin: runtime source revision
`2c7a4b25948a6bccd38d506ea27db27f0bbcf2d9`; public snapshot
`e0da5a73fd6e6c1c508c1795`; Strategy artifact SHA-256
`f52b5604f37f72aadd351eab91f6f909b8e46bdbb9219df22f37938cf6491d59`;
active policy `research-target-roi-v6`; allocator `policy-sized-v3`; verified fill
scope `exec-v4-2026-07-17`.

- Public Strategy Lab artifact:
  <https://jaxsonb04.github.io/weather_edge/strategy_research.json>
- Publication manifest:
  <https://jaxsonb04.github.io/weather_edge/publication_manifest.json>
- Public Strategy Lab:
  <https://jaxsonb04.github.io/weather_edge/#/lab>

The dollar attribution and Houston path were also checked against the canonical
production paper ledger and monitor history using read-only queries. No strategy
policy, production service, timer, order, ledger row, or live-trading setting was
changed during this audit. Live orders remained disabled and the system remained
paper-only.
