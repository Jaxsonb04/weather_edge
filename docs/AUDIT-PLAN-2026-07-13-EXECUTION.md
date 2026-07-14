# Second-Audit Execution Report — 2026-07-13

**Plan:** `docs/AUDIT-PLAN-2026-07-13.md`
**Baseline (frozen before any change):** audit HEAD `2a1a771a749c78ef03175b79b3f75a1f5b60239b`; Python suite 959 passed / 1 failed (the TEST-01 layout test only); user-owned `CLAUDE.md` working-tree edit and both untracked audit plans preserved untouched.
**Final state:** Python suite **985 passed / 0 failed** (Python 3.13, `ulimit -n 8192`); SPA gate lint/test/build green, landing bundle unchanged and under budget.
**Semantics versions introduced:** `exec-v2-2026-07-13` (execution) and `acct-v3-research-shadow-2026-07-13` (accounting).
**Mode:** local repository changes and read-only production inspection only. Nothing was committed, pushed, deployed, or mutated on GitHub/AWS/production.

---

## 1. What changed, by finding

### EX-01 — maker fills (fixed)
- New `trading/sfo_kalshi_quant/maker_fills.py`: one shared normalizer/allocator. Every public trade is normalized to exactly one maker side per the official order-direction semantics (`taker_book_side "bid"` fills resting NO bids, `"ask"` fills resting YES bids — the old code demanded `"bid"` for both sides). Volume is allocated once, in price-time priority, queue-ahead before quantity, with Decimal conservation math.
- `monitor.py` fill pass and `replay.py` both consume the allocator/normalizer; the replay no longer fans one trade into both YES and NO events.
- Cross-pass conservation (found by the fresh adversarial verifier): every capital fill persists per-trade `maker_volume_claims`; later passes subtract claims before allocating, so a restart can never re-credit consumed volume.
- Fill evidence now carries `allocations` (per-trade quantities), `queue_consumed`, and `execution_model_version` — not just a repeated trade-id list.

### EX-02 — depth-aware exits (fixed)
- `db.close_paper_order(order_id, price, max_quantity=, liquidity_evidence=)`: an exit can never book more quantity than the displayed top-bid size the monitor recorded. Partial closes materialize the executed slice as an immutable child `PAPER_CLOSED` row (`parent_order_id`), the remainder stays open; fees are computed on executed quantity; ledger and equity reconcile (verified empirically).
- The monitor records `HOLD_NO_DISPLAYED_DEPTH` when displayed size is zero and persists `exit_execution` evidence (requested/executed quantity, displayed size, snapshot time) on every close.
- Replay treats child rows as targeted quantity exits of the parent (parent replays at original size); `entries_for_market_side` ignores lot rows.

### RK-01 — catastrophic stop priority (fixed)
- `ExitSignal` gained `catastrophic: bool`; both stop branches in `decide_exit` set it. The research NO-basket rule moved into the decision engine (`exits.research_no_basket_hold_reason`) strictly below catastrophic priority; the monitor wrapper re-checks. A consequence made explicit by tests: with catastrophic priority enforced, the basket veto's only previously reachable effect *was* overriding catastrophic stops — non-catastrophic model-supported stops were already held by `HOLD_MODEL_VETO`.
- Every monitor decision now persists the model snapshot timestamp and age (`model_read` in snapshot diagnostics).
- Production regression test reproduces order 311 (Dallas 89–90 NO, held by the basket veto to −96.1%) and proves it now closes at the catastrophic floor.

### MD-01 — nonfinal observation certainty (fixed)
- A raw nonfinal station maximum is no longer treated as exact settlement truth. `_condition_on_observed_high` gained `is_final`: final truth keeps exact conditioning and the point mass; nonfinal observations damp bins by a raw-to-official feasibility (`sigma 0.6°F`, near-zero cutoff 1e-3) instead of hard-zeroing. The intraday truncation floor is relaxed by two observation sigmas on the nonfinal path. Applied uniformly to the residual, ensemble, and market-prior conditioning.
- `BucketProbability.observed_high_is_final` records the provenance; a new evaluator safety gate (`nonfinal_certainty_gate_enabled`, default on) blocks entries whose side probability ≥ 0.99 rests on a nonfinal raw observation, until a calibrated raw-to-official mapping passes walk-forward verification (MD-02 track).
- Production regression test encodes order 188 (PHIL raw 87.8°F, official 87°F, 86–87 bin): the bin keeps positive probability.

### AC-01 — research shadow accounting (fixed)
- New research orders book against a separate virtual account `paper-research-shadow` (own ledger, cash, drawdown, daily-loss pause). Research losses can no longer reduce live available cash, deepen live drawdown, or trigger live pauses — and the live book cannot pause research. Percentage caps still reference live equity, so sizing semantics are unchanged (no tuning change).
- Research resting orders are allocated **counterfactually** in the fill pass (full public tape, `research_shadow`/`counterfactual` evidence marks, no volume claims), so shadows never consume live maker volume.
- Historical rows are untouched: legacy research orders keep `paper-shared` and the −$87.30 shared history remains exactly as booked. The policy transition is an immutable `ACCOUNTING_POLICY_TRANSITION` ledger event. An explicit env flag (`PAPER_RESEARCH_SHARED_CAPITAL_ENABLED=1`) restores co-mingled behavior for deliberate shared-capital experiments.

### DB-01 — initialization races (fixed)
- The whole init/migration/account-bootstrap path is serialized by an exclusive advisory file lock (`<db>.init.lock`, flock; skipped for :memory:), and account bootstrap is additionally `INSERT OR IGNORE` + idempotent ledger keys.
- Verified: the pre-fix race reproduced deterministically-enough in the new stress test (UNIQUE constraint at attempt 9 and 22 of 40); post-fix, the multi-process stress (30 attempts × 8 spawn-processes × fresh+legacy DBs) and **1,000 consecutive iterations** of the thread-concurrency tests all pass with exactly one row per account, one opening event each, clean `integrity_check`.

### CI-01 / SEC-01 — verification and supply chain
- `.github/workflows/verify.yml` split into a Python matrix (3.12 = production runtime: tests + compileall; 3.13: full gate incl. Semgrep pinned to 1.169.0) and a Web job (bun 1.3.14: install --frozen-lockfile, lint, test, build, bundle budget). All actions pinned to full commit SHAs.
- `@heroui-pro/react@1.0.0-beta.6` resolves from the **public** npm registry with a lockfile integrity hash — no private-registry secret is needed for CI (the plan's stop-and-ask condition did not materialize).
- `.github/dependabot.yml` added (weekly pip/bun/github-actions updates). Enabling vulnerability alerts + Dependabot security updates, branch protection, and the Actions allowlist are repository *settings* — owner actions, listed in §4.

### TEST-01 — hermetic layout test (fixed)
- `test_root_manifest_is_the_only_install_manifest` now inspects `git ls-files` (tracked source), with a hidden-dir-excluded fallback outside a git checkout. `.local` migration staging no longer fails the suite; nothing in `.local` was deleted.

### UI-01 — dashboard/monitor fee parity (fixed)
- `exits.net_exit_per_contract` / `exit_bid_for_net` accept the fee schedule + series ticker; `strategy_lab/paper_card.py` passes the profile config and series so displayed exit bids net exactly what the monitor executes.

### ST-01 — continuous settlement verification (fixed)
- `paper-auto-settle` now immediately re-verifies every settlement read-only against the same final truth, persists one idempotent verification row per settled order, prints a summary, and alerts loudly (stderr) on MISMATCH/MISSING_FINAL. Verification never edits P&L.

### OP-02 — publication churn (fixed, deploy pending)
- The strategy cycle builds its artifact + manifest but defers publishing to the next operational cycle (≤5 min away): one Git pusher, ~12 deployments/hour, 5-minute operational SLA preserved. `SFO_STRATEGY_PUBLISH=1` restores legacy behavior explicitly.

### PR-01 — build provenance (fixed, deploy pending)
- `sync_to_box.sh` stamps `build_info.json` (source SHA, dirty flag, sync time, semantics versions) onto the host; the publication manifest embeds it under `provenance` (tolerant when absent); the gh-pages commit message names the source SHA.

### DOC-01 — region docs (fixed)
- `docs/aws_deployment.md` and `trading/deploy/aws/README.md` now state us-west-1 (migrated 2026-07-11) with the east decommission noted.

---

## 2. Batch D — restatement and corrected evidence (run against production snapshot)

A read-only snapshot (`sqlite3 .backup`) of the production DB was taken 2026-07-13 21:24 PT and analyzed on the dev Mac (production untouched).

New `trading/sfo_kalshi_quant/restatement.py` (read-only, URI mode=ro) classifies every order's entry/exit evidence under `exec-v2`. Result on the snapshot (`/tmp/restatement_20260713.json` on the Mac):

| View | Resolved orders | Realized P&L |
|---|---:|---:|
| Legacy (as booked, immutable) | 181 | **−$87.30** |
| Corrected: VERIFIED | 3 | −$4.71 |
| Corrected: UNVERIFIABLE | 178 | −$82.59 |

- Reconciles exactly with the audit: all-time live attribution +$5.46 (all UNVERIFIABLE), research −$92.76.
- Finding counts: 144 EXIT_DEPTH_UNVERIFIED (every legacy close, including all +$7.95 of July 13's live wins), 97 LEGACY_TAPE_UNREPLAYABLE maker fills, 79 TAKER_PRE_SIZING_FIX, 4 DOUBLE_CREDITED, 2 DIRECTION_INVALID (YES maker fills under the old wrong-direction filter). The 16 double-credited public trade ids match the audit list exactly.
- **`dataset_kalshi_trades` is empty in production** — there is no persisted public tape, so historical maker fills cannot be re-derived. Per the plan's stop condition they are labeled UNVERIFIABLE, not reconstructed.

Corrected chronological replay on the snapshot (with final CLI settlement truth from production, read-only): 329 placed, 88 filled (taker entries only — no tape for maker validation), ending realized equity **$940.99**, daily log growth **−0.0026**, promotion **blocked** with reasons including `only 0 independent trading days under exec-v2/acct-v3 (need 30); promotion clock restarted at the corrected-semantics boundary`.

The promotion clock now restarts mechanically: `replay_from_database` reads the `ACCOUNTING_POLICY_TRANSITION` ledger event as the semantics boundary and counts only days whose every resolved order postdates it. The production readiness artifact regenerates on the next strategy-lab cycle **after the owner deploys this code** — it will remain `ready=false`, which is correct.

### Verdict on the $1,050 target
Not met and not close: legacy realized equity is $912.70, and under corrected semantics only $−4.71 of P&L across 3 orders is execution-verified at all. The honest position is: deploy the corrected execution model, let the claims/allocator/depth-aware evidence accumulate, and re-evaluate after 30 independent post-boundary days. Do not raise risk to force the target.

---

## 3. Verification performed

- Batch 0 red tests: 13 tests failed for their intended reasons at baseline (each failure message inspected); all pass after implementation. Production-derived fixtures: orders 261/262, 226/227 (shared trade ids), 311 (basket-veto catastrophic hold), 188 (nonfinal boundary).
- Fresh adversarial verifier #1 (Batch A): confirmed direction/conservation/partial-close-accounting/catastrophic-priority/immutability held, and found two real defects — cross-pass volume re-credit and replay corruption by partial-close child rows — both fixed and regression-tested (`test_ex01_volume_is_not_recredited_across_monitor_passes`, `test_ex02_partial_close_replays_as_targeted_exit_of_parent`).
- Fresh adversarial verifier #2 (Batches B–D): see its report in the session log.
- DB-01: multiprocess stress (fresh + legacy schemas) 30/30 clean; 1,000/1,000 pytest iterations of the concurrency tests clean.
- Full gate: Python 985/985; `bun install/lint/test/build` green; static landing-bundle report under budget (no SPA source changed; the audit's observed 172 KiB measurement stands). `bash -n` clean on all modified shell scripts.

---

## 4. Owner actions required (external mutations — nothing executed)

1. **Deploy** this code to production (sync + installers) so exec-v2/acct-v3 semantics go live and the readiness artifact regenerates. Until then production keeps booking under legacy semantics.
2. **GH-01** (after CI is green on both jobs): default-branch ruleset — require the `Python 3.12`, `Python 3.13`, and `Web (bun)` checks, require PRs, block force pushes/deletion, include administrators.
3. **Deploy-key rotation**: confirm `sfo-weather-pages-lightsail` exists only on the active west publisher; rotate/rename through replacement; verify Pages still publishes while direct pushes to `main` are rejected.
4. **SEC-01 settings**: enable vulnerability alerts + Dependabot security updates; restrict allowed Actions to GitHub-owned + allowlist.
5. **OP-01**: provision the private encrypted versioned S3 bucket + least-privilege instance role for off-host archive; then wire upload-verify-before-prune and a quarterly restore drill.
6. **GH-02**: decide PR #29 (close as superseded or salvage its fast-publication behavior), reconcile/delete diverged `codex/*` branches, confirm or remove the `immigration_app / production` GitHub environment and its Railway deployments.
7. **Lightsail/east cleanup** (pre-existing): the quiesced us-east-1 instance and the abandoned 7/10 west box still await console deletion.

## 5. Model-validation track (Batch E — evidence-gated, not implemented as production changes)

- **MD-02**: build the leakage-safe city/season/local-hour table (city, date, observation time, high-so-far, final integer high, forecast center, remaining rise) and compare the shared intraday curve against hierarchical partial pooling, walk-forward by independent date, with after-fee impact. Until it passes: the nonfinal-certainty gate (shipped) already blocks the worst failure mode; capping non-SFO intraday blend weight remains a *proposal requiring replay evidence*.
- **MD-03**: archive exact live model vectors + issuance metadata at decision time, build matched-horizon EMOS training cohorts, and swap coefficients only if calibration and after-fee replay improve without worsening independent-day drawdown; use explicit uncertainty inflation for thin lead-0 support.
- Neither was tuned or promoted in this run (constraint: no model changes before corrected execution evidence exists).

## 6. Rollback

Every change is additive or behind explicit semantics: reverting the commit range restores legacy behavior; the new tables (`maker_volume_claims`), columns (`parent_order_id`), accounts (`paper-research-shadow`), and ledger events are ignored by the old code (additive, tolerant parsing). Historical journal rows were never modified; restated views are separate artifacts (`/tmp/restatement_20260713.json`, `/tmp/corrected_replay_20260713.json` on the dev Mac).
