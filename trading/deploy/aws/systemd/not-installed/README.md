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
ladder bin for the previous complete settlement day into `ladder_bin_outcomes`.
Read-only scoring -- it writes no order, ledger, policy, or trading decision --
but it does read `decision_snapshots` on a multi-gigabyte database, so it should
only be enabled once the box's CPU-credit and disk budgets (audit OPS-1/OPS-2)
are known to be healthy.

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
