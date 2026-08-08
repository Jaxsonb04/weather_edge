# PR #52 — EMOS point/distribution coupling: scoped landing plan

Status: **not landed.** Split out of the 2026-08-07 audit batch (PR #87)
because the guard as written can silence the live SFO book. This document is
the record F-04 asked for: what is genuinely broken, what to take, what to
change before taking it, and the pre-flight that decides whether it is safe.

## The hole is real on current main

`_latest_emos_snapshot` (`trading/sfo_kalshi_quant/forecast.py:453`) builds a
snapshot whose point **is** the EMOS mu and stashes the matching sigma in
`raw["emos"]`. But `build_scan_context` (`_cli/scan.py:370`) and
`build_target_report` (`report.py:152`) discard that and re-read the
distribution from a separate query, `adapter.load_emos_mu_sigma(lead_days=None)`,
gated on `config.emos_distribution_enabled`.

That flag defaults to `False` (`config.py:228`), is set `True` only in
`RESEARCH_PROFILE_OVERRIDES` (`config.py:620`), and `config_for_city` forces it
on only when `not city.has_full_blend` (`config.py:704`) — which **excludes
SFO**. So on the `live` profile at SFO, whenever `latest_live_forecast` takes
its documented "SFO operational fallback" branch (`forecast.py:150-179`, added
precisely because the legacy blend stopped refreshing), `emos_lookup` is `{}`,
`emos_active` is `False` (`probability.py:113`), and an EMOS point forecast is
priced with `cond.residuals` — the empirical residual law of a *different*
model. Point from one model, distribution from another.

Main has no equivalent guard on these two paths. It already implements the
exact pattern in `monitor.py:245-252`, and already carries the
`-(5.0/60.0)` future-timestamp guard at `monitor.py:239` — so the approach is
accepted in-repo, just never applied to scan and report.

## Why it is not merged yet

**The fail-closed branch fires for exactly one production configuration:**
live profile + SFO + EMOS operational fallback — which `forecast.py:122`'s own
docstring calls the post-migration steady state. If that fallback is active
today, landing the guard unchanged makes every SFO scan raise
`ForecastDataError`. The blast radius is bounded (`cmd_scan_all` catches it
per-target and yellow-skips at `scan.py:1671`, so the process survives and the
other 14 cities keep trading) but SFO produces **zero** signals.

**This is a merge blocker, not a follow-up.** Resolve it with the pre-flight
below before merging anything.

## Mergeability is much better than 14 days suggests

`git merge-tree` reports exactly 4 conflicted files — `bun.lock`,
`package.json`, `strategy_lab/paper_card.py`, `tests/test_strategy_research.py`
— and **none carry safety logic**. They are the Strategy Lab monitor-query perf
rework and a dependency bump main already superseded (main is on dompurify
3.4.12 / tar 7.5.21). Extracting the 19 safety-relevant files as a patch,
`git apply --check` reports it applies to current main with **zero conflicts**.

Take the safety subset; drop `paper_card.py`, `test_strategy_research.py`,
`bun.lock`, and `package.json` entirely.

## Pre-flight (do this first — it decides the config question)

Run read-only against a **copy** of the production DB, never prod itself:

1. Determine whether the SFO operational fallback is currently active — i.e.
   whether `latest_live_forecast` is taking the `forecast.py:150-179` branch for
   SFO on the live profile today. This is the single fact the merge decision
   turns on and it could not be established read-only during the audit.
2. If active: do **not** land the bare `if not enabled: raise`. Either enable
   `emos_distribution_enabled` for SFO on the live profile (so the guard finds a
   real distribution instead of raising), or scope the raise to profiles where a
   coupled distribution is genuinely reachable.
3. Replay the last 30 target dates on the DB copy and diff bucket probabilities
   before/after. The 14 non-SFO cities already run
   `emos_distribution_enabled=True` and currently resolve `(mu, sigma)` through
   `load_emos_mu_sigma`, whose rolling-origin/source filter
   (`forecast.py:810-825`) uses different rules than `_latest_emos_snapshot`'s
   `forecast_source_precedence`. Forcing same-row coupling changes which pair is
   used on any day those disagreed. That is the intended fix, but its magnitude
   is unmeasured.

## Required deviations from the PR as written

- **Do not inject `matching_emos` verbatim.** The PR injects the *pre*-intraday
  mu, so `probability.py:151`'s `bias = emos_mu - predicted_high_f` exactly
  cancels `apply_intraday_update`'s high-so-far adjustment on every EMOS-sourced
  target. Inject the post-intraday point with the EMOS sigma re-centered on it.
- **Fourth call site.** The PR misses `_tail_basket_one_target`
  (`scan.py` ~1205), the fourth `bucket_probabilities` call site. Apply the same
  re-centered injection there.
- **Three copies of the freshness guard.** The future-timestamp fix belongs in
  `scan.py:120`, `cli.py:553`, and `report.py:613` — three separate copies of
  the same function. `monitor.py:239` already has it.
- **Do not add `matching_emos_distribution` to `cli.py`'s `_sync_scan_bindings`
  list** (`cli.py:185-197`) — it must not be patchable out from under the scan
  engine.
- **Grep stored settlement labels first.** `settlement_truth.py`'s narrowed
  range regex requires an explicit `to`/`through`/dash separator. Any legacy
  label phrased differently ("between 72 and 73") that previously matched will
  now resolve NO, silently restating historical ledger P&L. Diff the affected
  set before merging.

## Worth taking alongside

- `settlement_truth.py` label-parser hardening, including the sign-aware
  `or below`/`or above` fixes. Real bug: the old `(\d+)` drops the minus sign on
  sub-zero strikes.
- The `truth_lag_days` plumbing in `forecaster/postproc_models.py` +
  `forecast_postproc_backtest.py` + their two test files + the
  `google_weather_cache.py` `_IMPLEMENTATION_ONLY_NAMES` entry. Closes a
  truth-availability leak.
- `docs/audits/2026-07-24-production-trading-incident-audit.md` — the durable
  record correcting the incident premise (the live paper profile *did* trade;
  real-money execution remained disabled as designed).

## Explicitly out of scope

`forecaster/blend_sources.py`'s `_blocked_live_promotion()` /
`_promoted_dataset_keys` pair is audit finding **H2**, not H1. Landing both
halves permanently changes promotion semantics. Landing the safety subset must
**not** be recorded as closing H2, which leaves research-only
`accuracy_candidate` sources able to take 12% live blend weight without an
explicit after-cost decision.

## Separately tracked hole

`monitor.py:245-252` extracts the same-row EMOS distribution correctly but does
**not** check `config.emos_distribution_enabled`, while `probability.py:113`
gates on that flag. On the live profile the monitor's careful coupling is
therefore discarded and exit decisions are priced with blend residuals against
an EMOS point. PR #52 does not touch this. If the config decision leaves the
flag `False`, this hole stays open and the exit path stays mis-priced.

## Test mechanics

Run with `ulimit -n 8192` — roughly 30 phantom failures otherwise, from fd
exhaustion at the default 256 limit. Re-verify `test_portfolio_cli`'s patch of
`sfo_kalshi_quant.cli._ensemble_for_target`: it is not in `_sync_scan_bindings`
and only works because `args.no_ensemble=True` short-circuits.
