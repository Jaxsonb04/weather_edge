# Unit templates that are deliberately NOT installed

`deploy/aws/systemd/` is a closed set. Three repository invariants treat every
template directly under it as a canonical, installed unit:

* `test_aws_deploy.py::test_deploy_verifies_canonical_systemd_units_before_restoring_timers`
  requires every `*.service.in` and `*.timer` there to be listed in
  `verify_systemd_unit_integrity.sh`, and that gate fails a deploy unless the
  unit is actually installed on the box (`FragmentPath` must resolve under
  `/etc/systemd/system`).
* `test_aws_deploy.py::test_disable_systemd_timers_knows_about_every_installed_timer`
  requires every `*.timer` there to appear in `disable_systemd_timers.sh`.
* `test_audit_batch_g.py::test_all_and_only_operational_services_alert_without_recursion`
  requires every `sfo-*.service.in` there to be in the operational alert set.

So there is no such thing as an inert template in that directory: putting one
there is a deploy change. This subdirectory holds ready-to-install units that
the owner has not chosen to run yet. Nothing globs it and no installer renders
it.

## sfo-kalshi-ladder-outcomes

Nightly `paper-ladder-outcomes --nightly` (audit IMP-1): resolves every offered
ladder bin for the previous complete settlement day, plus
`--nightly-lookback-days` (default 3) before it, into `ladder_bin_outcomes`.
Read-only scoring -- it writes no order, ledger, policy, or trading decision --
but it does read `decision_snapshots` on a multi-gigabyte database, so it should
only be enabled once the box's CPU-credit and disk budgets (audit OPS-1/OPS-2)
are known to be healthy.

Two behaviours the operator should know before enabling it:

* **It re-resolves recent days rather than only D-1.** A station's final CLI for
  D-1 is issued 01:30-04:40 local, so a one-shot nightly leaves a permanent hole
  for any station whose CLI had not landed yet: the bins come back in
  `missing_truth`, are written nowhere, and the next night moves on to the next
  day. The lookback closes that on the following run, and the ledger's upsert
  refuses to downgrade a row it already has. The timer also fires at 13:20 UTC
  rather than 11:20, which is past the westernmost station's CLI window.
* **It exits non-zero when something is wrong,** because `OnFailure=` is the
  only production notification path this unit has. An integrity contradiction,
  a hole on a day older than the newest in range, or a ladder retention has
  already thinned all produce exit 1. `--allow-incomplete` waives the
  completeness alerts for a deliberate historical backfill; it never waives an
  integrity contradiction.

Per-run cost on production (measured read-only, 2026-09-05): two bounded
`decision_snapshots` passes per target date, 0.26 s for the pair, plus one
exchange-settlement scan and one traded-bins query. The write lock is taken per
target date for a ~180-row upsert and released immediately, so a run cannot
starve the 2-minute paper monitor.

To enable it, deliberately:

1. move both files up into `deploy/aws/systemd/`;
2. add `render_unit`/`install` lines for them in `install_systemd.sh` and
   `install_systemd_notimers.sh`;
3. add `sfo-kalshi-ladder-outcomes.service` and `.timer` to `MANAGED_UNITS` in
   `verify_systemd_unit_integrity.sh`;
4. add the pair to `UNIT_PAIRS` in `disable_systemd_timers.sh`;
5. add the service to `OPERATIONAL_SERVICES` in `test_audit_batch_g.py`;
6. only then `systemctl enable --now sfo-kalshi-ladder-outcomes.timer`, and add
   it to `check_scheduler_health.sh` if it should be watched.

Until step 6 the timer does nothing; the command is fully usable by hand.
