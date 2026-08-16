# WeatherEdge Codebase Review Dialogue

Started: 2026-08-11

This file preserves the owner's questions, objections, alternatives, and the
assistant's answers during the codebase walkthrough. It is the discussion and
proposal record; [`CODEBASE-WALKTHROUGH.md`](CODEBASE-WALKTHROUGH.md) remains
the architecture map and checklist.

## Review and implementation protocol

1. Append every owner question, answer, and suggestion to this file.
2. During the walkthrough, analyze and classify proposals but do not implement
   them immediately.
3. Keep disputed gears open. Agreement on a direction is not yet an
   implementation check mark.
4. After all gears have been reviewed, assemble the accepted proposals into one
   dependency-ordered implementation plan.
5. Implement that plan as a batch with explicit tests, migration/rollback
   boundaries, and production validation appropriate to each change.
6. Update the walkthrough checkboxes and decision log only from the verified
   implementation results and the owner's final acceptance.

Proposal status labels:

- **OPEN** — under discussion or awaiting an owner decision
- **ACCEPTED FOR BATCH** — direction accepted; implementation intentionally waits
- **REJECTED** — owner chose not to pursue it
- **NEEDS EVIDENCE** — cannot choose safely until a defined measurement exists
- **IMPLEMENTED** — code/config/docs changed and validation recorded

## D001 — Apple influence and why SFO evidence is separate

Date: 2026-08-11  
Gear: G00, with effects on G03-G06 and G11  
Status: **ACCEPTED FOR BATCH at the policy level / NEEDS EVIDENCE for weighting**

### Owner question and suggestion

> why shouldnt apple data have zero influcen, it should have the most
> influences becuase the data came from various reputable vluable weather
> source. why we have to treat sfo evdiecne separately.
>
> from now all of my question answer and suggestions is stored in a md file and
> later on we implmenet them all at once

Normalized proposal:

- Apple WeatherKit should be eligible for the highest influence because Apple
  aggregates weather information from multiple reputable sources.
- Reconsider the need to present SFO evidence separately.
- Preserve all future review dialogue in Markdown and postpone implementation
  until the full review is complete.

### Answer

Apple should not be permanently fixed at zero influence. Zero is defensible as
the **starting weight for a newly connected, unvalidated predictor**, not as a
judgment that Apple Weather is poor.

Apple's official data-source page supports the owner's premise: WeatherKit's
weather models draw on organizations including NOAA/NWS, Environment and
Climate Change Canada, DWD, the Met Office/ECMWF, the Japan Meteorological
Agency, and Météo-France. That makes Apple a serious candidate source.

However, source reputation and source count do not determine the correct model
weight by themselves:

1. **Much of the information may overlap with WeatherEdge's existing inputs.**
   WeatherEdge already consumes several numerical weather models through
   Open-Meteo. If Apple combines some of the same underlying agencies/models,
   treating Apple as independent evidence can count the same weather signal
   twice.
2. **A polished aggregate can still have city-, lead-, season-, and
   settlement-window-specific bias.** The target here is not general weather
   quality. It is the official integer daily high for one station's exact
   fixed-standard climate day.
3. **Weight should follow incremental out-of-sample skill.** Apple deserves the
   largest weight where paired, forecast-time testing shows better CRPS/Brier,
   calibration, MAE, and tail behavior than the existing served distribution.
   It should receive less weight where it duplicates or worsens that forecast.
4. **Current WeatherEdge has no compliant long-horizon Apple evidence set.**
   Apple values are temporary and provider-expiring, and the tracked production
   call graph does not score them against final CLI truth.
5. **Changing a trading forecast before validation would change paper
   probabilities, gates, and sizes all at once.** Shadow-first operation keeps
   the incumbent forecast measurable while the candidate earns influence.

The license boundary is separate from forecast quality. Apple's current
Developer Program terms permit internal use and value-added products, but also
prohibit using Apple Weather Data as a secondary/derived database and limit
caching/storage to a temporary basis unless documentation expressly permits
otherwise. WeatherEdge currently takes a conservative interpretation: it does
not retain Apple forecasts or Apple-specific residual histories. This is not
legal advice, and written Apple clarification or qualified counsel should define
what transformed, aggregate evaluation evidence can be stored before building
the promotion study.

### Why SFO evidence is currently presented separately

“Separate” means **separate claims and evidence**, not a separate product or a
ban on using Apple in other cities.

SFO currently has evidence and components that the other cities do not:

- roughly ten years of KSFO station history;
- 442 held-out days in the published LSTM comparison;
- 262 scored probability outcomes in the published summary;
- SFO-only LSTM residual and marine-layer research;
- a legacy SFO blend/Google path; and
- a longer operational/research history.

The other 14 cities primarily share the newer station-agnostic NWP-to-EMOS
pipeline and have a shorter operational record. Combining those claims would
make SFO's sample size and specialist models appear to validate cities where
they were never tested. That would be a measurement error.

The better future structure is not “SFO forever separate.” It is:

- one common 15-city platform and settlement contract;
- per-city and per-lead scorecards;
- a pooled multi-city summary only when it reports heterogeneity and does not
  let SFO dominate the sample; and
- explicit labels for SFO-only features until those features are independently
  trained and validated elsewhere.

### Recommended batch direction

Replace the permanent-sounding zero-weight rule with this policy:

> Apple begins as a shadow-only candidate with zero decision weight. Apple is
> eligible to become the primary or highest-weight source for each city and
> forecast lead when a legally compliant, preregistered, forward evaluation
> demonstrates incremental out-of-sample skill and calibration over the exact
> served baseline. Weight is evidence-based, per city/lead, capped during
> rollout, and reversible if live scorecards degrade.

Proposed later implementation sequence:

1. Obtain written guidance on what Apple-derived aggregate scores, comparison
   outcomes, and model parameters may be retained.
2. Define the exact target: final station-specific NWS CLI integer high over the
   fixed-standard climate window.
3. Pre-register the comparison metrics, minimum sample, cities, leads, seasons,
   and promotion threshold before examining results.
4. Evaluate Apple, current EMOS, the final served distribution, and candidate
   blends from the same forecast vintages. Correct for overlapping errors and
   report confidence intervals, not only mean error.
5. Choose weights from held-out incremental performance. Allow Apple to become
   highest-weight where it wins; do not force one global weight across all 15
   cities.
6. Roll out to paper decisions behind a reversible cap, publish required Apple
   attribution for any Apple-derived public output, and monitor served
   calibration continuously.

### Sources checked for this answer

- [Apple WeatherKit data sources](https://developer.apple.com/weatherkit/data-source-attribution/)
- [Apple WeatherKit overview and attribution requirements](https://developer.apple.com/weatherkit/)
- [Apple Developer Program License Agreement, Attachment 8](https://developer.apple.com/support/terms/apple-developer-program-license-agreement/)

### Owner direction recorded after this answer

The owner rejected permanent shadow-only/zero influence. Apple must be treated
as an input intended to improve the overall forecast. The exact initial weight,
blend method, evidence gate, and rollout cap remain design questions for the
later implementation batch.

## D002 — Apple must contribute to the overall forecast

Date: 2026-08-11  
Gear: G00, with implementation in G03-G06 and evaluation in G11  
Status: **ACCEPTED FOR BATCH / IMPLEMENTATION DESIGN OPEN**

### Owner direction

> i feel now you should still allow appple to influence, matter of fact it
> should be treated as. a srouce to improve overall forecast accuracy

Normalized direction:

- Apple WeatherKit must not remain permanently at zero influence.
- Treat Apple as a forecast source whose purpose is to improve the combined
  forecast's accuracy.
- Defer the code/configuration change until the complete review batch.

### Answer

Accepted as a product/modeling direction. The implementation should give Apple
a real, nonzero path into the served forecast while preserving a measurable
baseline and preventing one provider failure from breaking the forecast.

“Allow Apple to influence” and “assume Apple improves accuracy” are different
claims. The batch should implement the former and continuously test the latter.
If measured accuracy degrades, the system should be able to reduce or reverse
the weight rather than preserving a harmful blend because the provider is
reputable.

### Proposed technical shape for the later batch

Do not insert Apple directly into historical EMOS training as though a durable
Apple archive already existed. Instead, add an explicit runtime candidate-blend
layer after the existing EMOS/SFO baseline:

1. Derive Apple's high from its 24 hourly values over the exact station and
   fixed-standard settlement window, as the current temporary source already
   does.
2. When Apple is complete and unexpired, move the baseline mean toward Apple's
   estimate through an explicit, versioned Apple weight.
3. Treat large Apple-versus-baseline disagreement as additional uncertainty;
   do not shift the mean aggressively while leaving sigma unchanged.
4. When Apple is unavailable, incomplete, expired, or rejected, reproduce the
   original baseline exactly rather than failing the forecast.
5. Record the blend policy/version and privacy-safe decision metadata so a
   served forecast can be reconstructed without publishing or improperly
   retaining raw Apple data.
6. Evaluate final served predictions against final CLI truth per city, lead,
   season, and hours-to-settlement. Use those results to increase, decrease, or
   specialize Apple's weight.
7. Apply the Apple attribution requirements to any public Apple-derived or
   value-added output.

The exact initial nonzero weight is deliberately not chosen in this dialogue.
It should be selected in the batch design together with the legal evidence
boundary, uncertainty response, rollback threshold, and forward-test plan.

### Acceptance criteria for the later implementation

- Apple has a genuine nonzero influence when a complete current value exists.
- Baseline behavior is identical when Apple is unavailable.
- A versioned decision record identifies whether and how Apple influenced each
  served forecast.
- Disagreement affects uncertainty as well as the point estimate.
- Per-city/lead scorecards compare the Apple-influenced forecast with the
  unchanged baseline using paired final CLI outcomes.
- The rollout has a reversible weight/cap and a documented degradation rule.
- Storage and public presentation comply with the final approved Apple legal
  and attribution design.

### Remaining questions for the implementation plan

- What legally permissible evidence can be retained to tune Apple weights?
- Should the first nonzero weight be one conservative global value or different
  values by city/lead?
- What initial cap and automatic/manual rollback threshold should apply?
- Should Apple influence only the mean initially, or should the first release
  also model its effect on predictive variance from disagreement?

## D003 — Move to G01: city registry and settlement clock

Date: 2026-08-11  
Gear: G01  
Status: **ACCEPTED FOR BATCH**

### Owner direction

> now with the next thing in check list.

### Checklist transition

G00 discussion is marked **REVIEWED / ACCEPTED FOR BATCH**, not complete. Its
checkbox remains open until the later Apple-influence and related batch changes
are implemented and verified. G01 is now the active discussion.

### What G01 controls

The city registry is the identity spine of WeatherEdge. Each city record binds
together facts that must refer to the same market outcome:

- internal slug and display name;
- prediction-market series ticker;
- NWS settlement station—the exact thermometer that resolves the outcome;
- NWS CLI product site and issuer—the official daily climate report;
- latitude/longitude used to fetch weather-model grid data;
- civil timezone used for local operations/display;
- fixed standard-time UTC offset used for the official climate day; and
- flags for SFO-only blend/cohort behavior.

The settlement calendar then maps timestamps to a midnight-to-midnight climate
day in local **standard** time year-round and rounds a finite official high with
`floor(value + 0.5)`.

The distinction between civil time and fixed standard time is important during
daylight saving time. For example, SFO's settlement climate window remains
08:00 UTC to 08:00 UTC. Moving it to civil midnight during summer would shift
the modeled/observed day by one hour and could put a temperature into the wrong
market date.

### Current 15-city identity table

The following is the repository's current mapping. Its external station/rule
verification is dated 2026-07-06 and must be rechecked before an identity
change or production claim.

| City | Series | Settlement station | CLI site/issuer | Standard offset |
|---|---|---|---|---:|
| Miami | `KXHIGHMIA` | `KMIA` | `MFL/MIA` | UTC-5 |
| Los Angeles | `KXHIGHLAX` | `KLAX` | `LOX/LAX` | UTC-8 |
| Chicago | `KXHIGHCHI` | `KMDW` | `LOT/MDW` | UTC-6 |
| Atlanta | `KXHIGHTATL` | `KATL` | `FFC/ATL` | UTC-5 |
| New York | `KXHIGHNY` | `KNYC` | `OKX/NYC` | UTC-5 |
| Dallas | `KXHIGHTDAL` | `KDFW` | `FWD/DFW` | UTC-6 |
| Seattle | `KXHIGHTSEA` | `KSEA` | `SEW/SEA` | UTC-8 |
| Philadelphia | `KXHIGHPHIL` | `KPHL` | `PHI/PHL` | UTC-5 |
| Phoenix | `KXHIGHTPHX` | `KPHX` | `PSR/PHX` | UTC-7 |
| Austin | `KXHIGHAUS` | `KAUS` | `EWX/AUS` | UTC-6 |
| San Francisco | `KXHIGHTSFO` | `KSFO` | `MTR/SFO` | UTC-8 |
| Houston | `KXHIGHTHOU` | `KHOU` | `HGX/HOU` | UTC-6 |
| Oklahoma City | `KXHIGHTOKC` | `KOKC` | `OUN/OKC` | UTC-6 |
| Boston | `KXHIGHTBOS` | `KBOS` | `BOX/BOS` | UTC-5 |
| Denver | `KXHIGHDEN` | `KDEN` | `BOU/DEN` | UTC-7 |

Known traps already encoded in the registry include Houston Hobby rather than
Bush Intercontinental, Dallas/Fort Worth rather than Love Field, Chicago Midway
rather than O'Hare, New York Central Park rather than an airport, and Phoenix's
no-DST civil clock.

### What is good about the current design

- The registry makes settlement identity explicit instead of inferring it from
  names.
- Python records are immutable dataclasses.
- Lookups exist by slug, station, series, and longest market-ticker prefix.
- Forecaster and trading copies are byte-identical and guarded by a parity
  test.
- Station-specific fixed-standard time is distinct from civil time.
- SFO coordinates are deliberately frozen because changing the Open-Meteo grid
  point would invalidate learned model biases.

### Current weakness

City identity is copied across several places:

1. `forecaster/cities.py`;
2. `trading/sfo_kalshi_quant/cities.py`;
3. the frontend `CITY_TICKERS` array in `src/lib/data.ts`; and
4. the exposure-region map in `trading/sfo_kalshi_quant/account.py`.

The two Python files have a strong parity test, but the TypeScript and region
maps are maintained separately. Adding, renaming, or correcting a city can
therefore leave the browser or account exposure grouping behind even while the
Python parity test passes.

Directly importing one Python package from the other is not an ideal solution:
the forecaster is script-oriented, the trading package is installed separately,
and production uses split deployment roots.

### Recommended batch direction

Create one tracked, declarative city schema as the human-edited source of truth,
then generate deployment-local representations from it:

- generated `forecaster/cities.py`;
- generated `trading/sfo_kalshi_quant/cities.py`;
- generated TypeScript city identity data; and
- generated/default region membership, or an explicit reviewed region field in
  the canonical schema.

The canonical record should include the current identity fields plus:

- exposure region;
- verification source/date for the market rule and CLI product;
- whether a coordinate change requires archive/model recalibration; and
- flags that identify SFO-only evidence without hard-coding SFO behavior in
  unrelated modules.

The generator should be deterministic and CI should fail if generated files are
stale. Runtime packages can remain independent because each deploy receives its
generated local copy; only human editing becomes centralized.

### Acceptance criteria for the later batch

- One human-edited schema owns every city identity and region field.
- Generation is deterministic and checked by CI.
- Both Python runtime locations and the frontend remain independently usable
  after split deployment/build.
- Uniqueness is validated for slug, series, station, and CLI identity.
- Coordinates, timezone names, and standard offsets are validated.
- Existing ticker-prefix behavior and all 15 current mappings remain unchanged.
- A station, CLI identity, coordinate, or settlement-clock change requires an
  explicit migration/recalibration warning rather than looking like harmless
  copy editing.
- External market-rule verification is dated and easy to renew.

### Decision requested

Should G01 adopt the recommended **canonical schema plus generated local
copies** direction for the later batch?

- **Yes** — mark the direction **ACCEPTED FOR BATCH** and move to G02.
- **No** — preserve manual duplication and its current parity tests.
- **Could we do this instead?** — record the alternative without implementing
  it yet.

### Owner decision

> lets do that. the goal here is to based on the settlement with what kalshi
> trading platform is using

The owner accepted the canonical-schema/generated-copy direction with an
important governing rule: WeatherEdge identity and settlement configuration
must follow the prediction market's actual settlement contract.

### Authority order accepted for the batch

The canonical city schema is a reviewed local representation of an external
contract, not an independent source of settlement truth. The later batch should
apply this precedence:

1. the exact market/event contract and terms for the traded event;
2. event-level settlement metadata, if it is more specific;
3. the series-level `settlement_sources` contract;
4. the referenced NWS CLI product and its location header; and
5. the generated WeatherEdge schema copies.

If upstream contract metadata changes, WeatherEdge must not silently rewrite
the station or continue using a stale mapping. It should fail closed, record the
drift, and require review because changing a station, CLI product, coordinate,
or climate clock can invalidate learned biases, historical comparability, and
open paper positions.

### Read-only external verification performed

On 2026-08-11, the public production `GET /series/{series_ticker}` endpoint was
queried without authentication for all 15 configured series. Every returned
`settlement_sources` URL matched the repository's configured NWS CLI
`site/product/issuedby` URL.

The referenced live NWS CLI products were also read. Their current headers
identified the expected locations, including:

- Chicago Midway;
- Central Park, New York;
- Dallas/Fort Worth;
- Houston Hobby;
- Los Angeles International;
- Seattle-Tacoma Airport;
- Austin Bergstrom;
- San Francisco Airport; and
- the corresponding configured locations for the other seven cities.

This is current read-only verification of source identity, not a guarantee that
the external contracts cannot change later.

Primary documentation: the prediction-market API documents that a series
defines the settlement sources applied to its recurring events:
[Get Series](https://docs.kalshi.com/api-reference/market/get-series).

### Additional acceptance criteria

- Store a verification timestamp and upstream series update timestamp with
  each canonical identity.
- Add a read-only audit command that compares all configured series and current
  event metadata with the canonical schema.
- CI can verify deterministic generated files; a scheduled/runtime audit should
  detect external contract drift separately.
- Contract drift raises a visible, nonzero/fail-closed result and never
  automatically migrates station identity.
- Open orders and historical evidence retain the exact settlement-contract
  version/identity under which they were created.

## D004 — Move to G02: settlement truth versus observations

Date: 2026-08-11  
Gear: G02  
Status: **OPEN**

### Checklist transition

G01 is **REVIEWED / ACCEPTED FOR BATCH** and remains unchecked until generated
schema work and contract-drift validation are implemented. G02 is now active.

### What G02 controls

WeatherEdge observes two temperature concepts that must never be confused:

- **running observed high:** the maximum seen so far in NWS station
  observations; useful for updating a forecast, but subject to missing reports,
  sensor/reporting differences, rounding, and later official publication; and
- **final settlement high:** the integer maximum in the final NWS
  Climatological Report referenced by the market contract.

Live CLI products and historical IEM CLI rows populate the station-keyed
`cli_settlements` table. Preliminary products containing `AS OF` remain
nonfinal, and a nonfinal row cannot overwrite a final row. That is the correct
truth architecture.

### Confirmed current defect

The separate observation-derived daily-high path sets `is_complete=1` whenever
the observation's local date is earlier than today. The trading adapter accepts
that flag as final. Probability conditioning may then hard-zero impossible bins
or create a point mass exactly as it would for final CLI truth.

That is not aligned with the owner's G01 rule. A finished calendar day does not
turn station observations into the market's official settlement source. Only a
final CLI value should create exact certainty or settle an order.

### Recommended batch direction

Enforce provenance-based finality:

1. Observation-derived daily highs are always labeled provisional, even after
   the date passes.
2. Only a confirmed `cli_settlements.is_final=1` row may set
   `observed_high_is_final=true`, create exact 0/1 probabilities, or settle a
   paper order.
3. If final CLI truth is unavailable, hold settlement and keep probability
   conditioning soft; never promote a running observation merely because time
   passed.
4. Rename ambiguous fields/tables or expose an explicit provenance enum so
   future code cannot confuse “day complete” with “settlement final.”
5. Add tests covering missing observations, preliminary `AS OF` CLI products,
   late/final CLI arrival, a one-degree observation/CLI difference, and final
   rows resisting preliminary overwrite.
6. Later, in G10, add read-only reconciliation against the platform-published
   market result as an independent audit. The market contract's NWS CLI remains
   the primary numeric truth source for model scoring.

### Decision requested

Should G02 adopt strict provenance-based finality—only a final contract-linked
NWS CLI report can create exact certainty or settlement—for the later batch?

- **Yes** — mark the direction **ACCEPTED FOR BATCH** and move to G03.
- **No** — preserve the current rule that past observation days can become
  complete/final.
- **Could we do this instead?** — record a different finality hierarchy.

