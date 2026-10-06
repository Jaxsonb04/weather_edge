# Website outage recovery — October 2, 2026

The public SPA shell was available, but its operational data had not published
since September 26 at 19:52 UTC. Browser checks showed the publication-behind
banner on Overview and Strategy Lab and correctly hidden current market data.
Recovery restored fresh public data and automatic operation without deploying
source or changing trading, provider, access, retention or billing policies.

## Cause

The AWS runtime root filesystem was at 100% with no available space. Forecast,
paper, Strategy and publication services failed; retained logs explicitly showed
`No space left on device` and subsequent database I/O errors.

A September deployment left a 23,543,779,328-byte local rollback snapshot
alongside a roughly 29 GiB live paper journal. Local backup cleanup runs during
deployment rather than continuously, while the archive-only retention policy
deliberately allows the live journal to grow. The retained copy consumed the
headroom needed by ongoing operation. The snapshot's presence does not prove
that the corresponding source release completed.

## Recovery

1. Captured all fourteen enabled timers, created maintenance state, and quiesced
   every canonical producer before working on storage.
2. Confirmed that the encrypted off-host backup existed with the same size and
   stored checksum. Recomputed the entire local file's SHA-256 and independently
   streamed the entire off-host object through SHA-256. Both reads matched the
   expected checksum before deleting only the redundant local snapshot and its
   sidecar. The encrypted off-host copy remains recoverable.
3. Cleared disposable package cache and an abandoned generated Pages checkout.
   Disk usage fell to 64%, with approximately 23 GiB available. No live journal,
   ledger, order, forecast history or archive rows were deleted.
4. Checked the core financial tables and both independent paper ledgers before
   resuming activity. Manually refreshed all-city forecasts, monitored existing
   paper positions, rebuilt Strategy and published the coherent snapshot.
5. Recovered only missing completed target days referenced by open paper orders.
   Existing IEM/NWS station identity and finality rules remained binding. Twelve
   final station-days were recovered and twelve paper orders settled; the
   canonical verifier reported twelve checks and zero mismatches.
6. Restored the exact fourteen captured timers and cleared maintenance after
   publication verification. Scheduler and forecast/publication health passed.
   Disk-related OS maintenance services also completed successfully on rerun.

## Verification at 08:13 PDT

- Twenty-nine canonical systemd units passed integrity with no runtime drift.
- Fourteen timers were restored enabled and active. Maintenance was absent.
- The scheduler verified fresh forecast state, local/public artifact structure,
  source provenance and disk usage below its unchanged 85% ceiling.
- All fifteen cities had fresh eight-member EMOS forecasts for today and both
  future lead days: 45 of 45 targets.
- Targeted SQLite checks passed for paper accounts, account ledger, paper orders,
  EMOS forecasts, NWP model forecasts and station CLI settlements. These checks
  are not a claim of a new full-file integrity scan of the 29 GiB journal.
- Both economically separate fixed-capital paper ledgers reconciled before and
  after settlement recovery. Each account began with $1,000; their performance
  must not be combined into one bankroll.
- Public publication at `2026-10-02T15:12:04+00:00` and Strategy generation at
  `2026-10-02T15:10:52+00:00` were fresh. All five JSON hashes matched the public
  manifest; the Pages shell and artifacts returned HTTP 200.
- Desktop and mobile browser checks showed no publication-behind banner, no page
  errors and no horizontal overflow. Screenshots were inspected. Mobile day
  selection and menu navigation into Strategy Lab worked and returned the
  expected DOM state.
- Automatic forecast, paper scan, monitor, Strategy and operational publication
  cycles completed successfully after timer restoration. The natural forecast
  finished at 15:12:12 UTC and the scheduler passed again at 15:13:27 UTC.
- Installed backend provenance and sampled source files match
  `2a6432e3bdb29fa1798a4b07e5f5396685b5245b`. The existing SPA was retained.
  Real-money execution remains disabled; dry-run remains enabled.

## Remaining work and deliberate deferment

Three Phoenix positions for September 27 remain pending final settlement
evidence. The retrieved older official report is preliminary, and the archive
does not provide an admissible final high. The existing gate correctly refuses
to synthesize settlement from observations or a prediction-market quote.

Dataset backfill, archive maintenance and non-SFO Google refresh still carry
failed markers from the full-disk period. Their daily timers are enabled. Heavy
catch-up was deliberately left to the next scheduled window; this incident
recovery does not claim those future runs have passed. Core automatic website,
scan, monitor and publication operation was verified separately.

Archive-only retention remains intentional. The live journal still grows and
needs a planned capacity or safely backed-up compaction operation before the
disk ceiling is approached again. This recovery removed a redundant deployment
artifact; it did not authorize live-row deletion or purchase more storage.

Historical analysis remains stale and readiness continues to fail closed.
The September proposed backend release was not completed in this recovery.
Access rules and the existing unconfigured alert endpoint were not changed.

Private logs, backup proof, fresh artifact copies and screenshots remain in the
ignored `.local/outage-2026-10-02/` directory. Sensitive infrastructure values
and operator connection details are excluded from this report.
