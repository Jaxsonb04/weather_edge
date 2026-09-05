# Model efficiency and publication recovery — September 4, 2026

This audit follows the September remediation. Production observations are dated
snapshots; local reproductions do not establish historical dollar impact.
Live Stability and Research ROI remain economically separate paper accounts.

## Operational finding

At 03:33 UTC on September 5 the public manifest was roughly 58 minutes old,
and its Strategy artifact was almost three hours old. The previous deployment
task had been interrupted during a second backup-gated attempt, leaving its
server process running and every timer disabled under deployment maintenance.
The stale-publication warning was therefore correct. Twenty-nine installed
systemd definitions matched the canonical source; the revision stamp was
`dd159f814654d1d5de9a29a2ae9cef627cd2174a`.

The coordinator checked its Git revision before backup verification but would
later rsync a shared checkout without checking it again. Concurrent edits could
therefore be transferred with the old clean revision stamp. The coordinator was
stopped before transfer. New guards reject changes to HEAD, branch, tracked,
staged or untracked files after backup and after source transfer, before stamping
provenance or restoring services. Ten behavior tests exercise both boundaries.
These guards detect persistent changes; they do not replace immutable staging
or prove that a transient edit reverted during transfer was never copied.

Another recovery defect read the watchdog-enabled variable before initialization
when analysis was interrupted. It now derives that flag from the captured timer
policy before traps are installed. Four behavior tests cover SIGTERM and ordinary
failure with the watchdog enabled and deliberately absent, checking exact timer
restoration, maintenance release, and original exit status.

The existing backup must finish its complete downloaded integrity and foreign-key
checks. Recovery retains the exact captured timer policy and reuses the verified
immutable snapshot while writers remain quiesced, then runs the canonical source,
installation, unit, index, account, analysis and publication gates. Repeating an
identical expensive backup during continuous quiescence adds downtime without
new recovery evidence. Final outcome belongs in `docs/SESSION_MEMORY.md`.

The backup gate also performed two full database scans. It now retains the
stronger single proof: snapshot, hash, encrypted upload, download, checksum,
full restored SQLite integrity, and restored foreign-key checks before promotion.
Removing the duplicate pre-upload scan reduces future maintenance work; no new
production timing is claimed. An actual error-propagation defect could accept a
foreign-key command failure with empty stdout; that failure now rejects the
backup. Six new full-flow tests cover success, checksum/corruption failures,
foreign-key violations and command failure, with unchanged live database bytes.
The focused deployment/backup suite passed 175 tests.

## Corrected local behavior

- NWP collection combines lead-1 and lead-2 variables in one model request.
  The normal fifteen-city/eight-model collection requires 120 rather than 240
  HTTP requests. Combined and separate live API responses matched in a bounded
  GFS/SFO comparison; variable-level billing or byte savings are not established.
  Missing lead data never falls back to another lead or the newest analysis.
- Exit reports require an explicit terminal exit cause. Unknown causes retain
  their counts and P&L; partially filled expirations remain open. The UI labels
  unknown causes as "Reason unavailable". No economic or order policy changes.
- Strategy freshness uses its own generation time. Republishing operational
  artifacts cannot revive an old preserved Strategy snapshot. Operational
  publication freshness continues to use the coherent publication timestamp.
  Both stale preservation and independent expiration deadlines are tested.

## Further forecast defects to isolate before model promotion

1. **Decision-time lineage:** Previous Runs aligns each valid hour to a fixed
   lead, so one daily maximum may combine several issuances. It is useful for
   fixed-lead weather analysis but does not prove an input existed at one earlier
   trading decision. Use captured live inputs or a separate Single Runs archive
   with initialization and publication availability. The code's former blanket
   leakage-free claim is corrected; no live future-input use was established.
2. **Source selection:** `truth_store.load_nwp_model_forecasts` ignores `source`
   despite source being part of the stored primary key. A synthetic canonical
   70°F row and research 99°F row resolved differently by insertion order.
   Isolate source authority before introducing new providers into this table.
3. **New-member mismatch:** historical postprocessing includes an unseen model
   member on introduction while live serving drops members absent from the fit.
   A synthetic example moved the historical mean from 69°F to 81.75°F while the
   serving rule retained 69°F. Align the two paths before interpreting a source
   expansion backtest as deployable performance.
4. **Incomplete days:** daily-high reconstruction accepts one finite hourly
   value with all afternoon observations missing. A midnight-only 50°F synthetic
   day was accepted as a daily maximum. Capture completeness and compare training
   coverage before changing this input policy or rebuilding historical results.

These are confirmed mechanisms, with unmeasured production prevalence and
performance impact. They were not used to justify looser risk gates or a new
forecast policy during operational recovery. Remaining sizing and quote-age
findings are detailed in `2026-09-04-trading-followup.md`.

## Recommended use of current resources

Use authoritative AWS exports for narrow offline research, run fitting and replay
on the development machine or CI, and export small versioned serving parameters.
Audit forecast vintages first, then compare CRPS-fitted EMOS, existing source
ablations and a pooled quantile forest on identical held-out dates. Keep newly
researched WeatherNext 3 and ensemble feeds in shadow until access, cost,
station-target alignment, publication delay and incremental skill are verified.
The companion research report contains direct papers, official documentation,
maintained repositories and acceptance criteria.

Higher win rate is not a substitute for positive after-fee edge. More volume must
come from eligible distinct opportunities and realistic fills, with independent
weather-day uncertainty and each account's unchanged risk limits.


## Bounded offline result

A fresh, narrow read-only AWS forecast export supported an independently reviewed
90-day comparison across 2,658 paired forecasts. Constrained CRPS fitting reduced
pooled bias-corrected CRPS by 1.125%, while MAE improved only 0.011°F. Philadelphia
lead two regressed 1.64%. The approximate reconstructed baseline, fixed-hour
feature vintages and absent hourly completeness/publication metadata make this
an exploratory weather-skill result, not evidence of better deployed forecasts
or trading returns. See `../research/2026-09-04-crps-pilot.md` for the full method,
paired block-bootstrap intervals and limitations. Production parameters remain
unchanged pending shadow evaluation.


## Recovery outcome and resource prevalence

At 04:39 UTC, backend source and public/full-analysis provenance matched clean
PR #113 (`2a6432e3bdb29fa1798a4b07e5f5396685b5245b`). Public generation at
04:37:54 UTC and Strategy analysis at 04:36:08 UTC were current. Five public
artifact hashes and 38 static app files matched the deployed build. All fourteen
timers were enabled/active, twenty-nine unit definitions matched, no unit was
failed, maintenance was absent and disk use returned to 42%. Both separate
paper ledgers reconciled. Automatic scan, monitor, settlement, forecast and
publication cycles succeeded; live execution stayed disabled and dry-run enabled.
The extra recovery staging check stopped on bundled fallback JSON before
restoration; stripping only those six disposable build copies resolved it,
with authoritative data supplied by the canonical AWS publisher.

A 0.44-second read-only production query covered 124,804 NWP rows since January
2024 across fifteen stations and leads one/two: every inspected row used the
canonical Previous Runs source. A fresh eighteen-month replay found zero unseen
members across 12,163 eligible targets. These are latent hardening defects within
that scope. Every current station/lead archive has all eight serving models;
complete hourly coverage still cannot be established from the stored schema.

Google and WeatherKit caches currently do not enter EMOS. Google paired-evidence
and shadow helpers have no scheduled callers; WeatherKit's active-high reader
has no consumer. The main Google orchestration also runs all-city EMOS, so
turning off that entire service would break an active forecast resource. See
the companion resource report for a bounded use of existing archives and feeds.

The public visual audit also found overly broad leakage-free claims in the
homepage and methodology. A separate frontend correction describes the actual
rolling-origin fit and explicitly distinguishes fixed-hour archives from
proven availability at a historical trading decision.
