# WeatherEdge — Second Audit, Integrity Recovery, and Growth Plan

**Date:** 2026-07-13 (Pacific)  
**Audit HEAD:** `2a1a771a749c78ef03175b79b3f75a1f5b60239b` (`main` = `origin/main` at inspection)  
**Scope:** local repository, GitHub repository and Actions, production AWS runtime, public artifacts, and live production SQLite evidence  
**Mode:** plan only. This audit changed no trading code, production data, AWS configuration, GitHub settings, or deployed service.  
**Execution target:** Claude Fable 5, using the handoff prompt in §12.

This is the follow-up to `docs/AUDIT-PLAN.md` (2026-07-10). The first audit was substantially implemented: 69 commits after its audit HEAD changed 312 files (+30,990/−15,922), the largest modules were split, finality and archive protections were added, and the test surface expanded. This plan does not repeat fixed work. It concentrates on the execution-accounting defects and model-validation gaps that are now visible because the system is running continuously across fifteen city markets.

---

## 1. Outcome, constraints, and completion bar

### 1.1 Outcome

The owner’s economic target is a **solid 5% gain from the original $1,000 paper equity**, meaning at least **$1,050 realized equity after fees**. At the audit snapshot:

- realized equity was **$912.70**;
- marked equity was **$914.57–$914.92**, depending on the minute of the live mark;
- the remaining gap was **$137.30 realized** or about **$135.43 marked**;
- all-time shared realized P&L was **−$87.30**.

This target is aspirational, not a guaranteed result and not permission to raise risk until the paper execution model is credible. The immediate objective is therefore:

> Make the measured P&L trustworthy, preserve the live behaviors that appear promising, isolate research losses from the production-intent score, and promote tuning only through chronological, after-fee, out-of-sample evidence.

### 1.2 Non-negotiable constraints

1. Keep all execution paper-only. Do not enable live orders or real-money credentials.
2. Do not tune thresholds, Kelly fraction, position caps, or frequency from the July 13 result.
3. Fix execution and accounting integrity before recomputing performance or changing the model.
4. Preserve immutable historical records. Corrections create versioned/restated views; they do not rewrite the original journal silently.
5. Use AWS runtime state as production truth. Ignored local runtime artifacts are not evidence.
6. Preserve the current final-only settlement gate, maker-first intent, favorite band, positive lower-bound edge gate, shared exposure limits, and city/region diversification unless a controlled replay disproves one.
7. Any GitHub setting change, deploy-key rotation, AWS mutation, systemd change, S3 creation, production sync, or timer restart requires an explicit operator checkpoint.
8. The public site remains a React + HeroUI Pro operational meteorological instrument. Public copy says “prediction market,” not a venue name.

### 1.3 Completion bar for this plan

The implementation is complete only when all of the following are true:

- maker fills conserve aggressor direction and public trade volume in the monitor and replay;
- exits cannot book more quantity at a quote than the recorded liquidity supports;
- catastrophic stop discipline cannot be overridden by the research basket veto;
- nonfinal station observations cannot create false exact settlement certainty at integer-report boundaries;
- research experiments cannot reduce the production-intent live equity or trigger its risk pauses;
- the corrected chronological replay runs from an explicit version boundary and is reconciled to an immutable event ledger;
- the readiness artifact is regenerated from corrected execution semantics;
- at least 30 independent weather days are available before any promotion decision;
- the day-clustered 95% after-fee ROI lower bound is positive, after-fee log growth is positive, calibration gap is below 0.10, and max drawdown stays within the predeclared 10% operating limit;
- the target is credited only when **realized live-profile equity is at least $1,050**, marked equity is also at least $1,050, and no single trade or city-day supplies more than 25% of the gain from the corrected baseline;
- Python 3.12/3.13 and the complete SPA gate pass in CI;
- AWS, GitHub, and the public manifest can identify the exact deployed source revisions.

If the corrected history loses its apparent edge, the correct result is to keep the system paper-only and improve the forecast/model through the challenger process in §7—not to loosen risk controls to force the equity target.

---

## 2. Audit method and evidence boundaries

Four independent passes were used:

1. **Local code and test audit:** execution, ledger, model, forecaster, deployment, SPA, and the changes since the first audit.
2. **GitHub audit:** branches, PRs, Actions, Pages, repository settings, deploy keys, security features, and deployment metadata.
3. **AWS/runtime audit:** read-only SSH, `systemctl`/`journalctl`, current production DB queries, deployed file hashes, public artifacts, and direct P&L reconstruction.
4. **Fresh adversarial verification:** a separate pass was instructed to disprove the highest-impact local findings. It confirmed maker semantics/volume reuse, exit-depth optimism, and the initialization race; it downgraded two model concerns to validation work.

### Evidence boundary

- AWS inspection occurred before the July 13 Pacific trading day was fully final. Preliminary daily reports existed, four positions remained open, and the three live winners were `PAPER_CLOSED`, not settled.
- Today’s direction is encouraging, but execution credibility is not established because full exits did not record depth and some maker fills reuse public trade IDs across orders.
- A direct cross-database check found no settlement corruption: all 37 settled orders had final daily climate rows, with zero missing or mismatched booked highs.
- Production services were healthy at the end of inspection. A recovered freshness alert and missing off-host archive are operational findings, not evidence that an outage caused the P&L.

### Prompt-design basis

The execution prompt in §12 follows the official guidance for [GPT-5.6 prompting](https://developers.openai.com/api/docs/guides/prompt-guidance-gpt-5p6) and [Claude Fable 5 prompting](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5): outcome-first instructions, explicit success and stop conditions, evidence-grounded progress, bounded autonomy, parallel independent reads, sparse updates, and fresh verification before completion.

---

## 3. Executive truth: what July 13 did and did not prove

### 3.1 P&L reconstruction

For the Pacific resolution day beginning 2026-07-13 00:00 PDT:

| Slice | Resolved | W–L | Realized P&L | Resolved cost | ROI on resolved cost | Return on original $1,000 |
|---|---:|---:|---:|---:|---:|---:|
| Live profile | 3 | 3–0 | **+$7.9454** | $52.2913 | **+15.19%** | +0.795% |
| Research profile | 3 | 2–1 | **−$4.6045** | $25.9400 | −17.75% | −0.460% |
| Shared account | 6 | 5–1 | **+$3.3409** | $78.2313 | +4.27% | **+0.334%** |

Therefore:

- “the live profile made about $7.95” is true;
- “the account made $7.95” is false;
- “the account made 5% today” is false;
- “the result proves the model edge” is false.

### 3.2 Why the live profile looked strong

The three live winners were diversified day-ahead NO favorite positions:

| Order | City / bin | Contracts | Entry | Net exit | P&L | Entry evidence | Exit rule |
|---|---|---:|---:|---:|---:|---|---|
| 270 | Dallas 91–92°F NO | 20 | $0.88 | $0.9467 | +$1.3335 | maker queue/trade-through | fair-value convergence |
| 271 | Chicago 89–90°F NO | 10 | $0.7631 all-in | $0.9361 | +$1.7292 | immediate visible quote | fair-value convergence |
| 261 | Seattle 82–83°F NO | 33 | $0.82 | $0.9680 | +$4.8827 | maker queue/trade-through | fair-value convergence |

Promising behavior to preserve:

- day-ahead entries, before final settlement truth;
- high-probability favorite-side concentration rather than cheap tails;
- positive lower-bound edge at approval;
- maker-first pricing with queue-ahead and later-trade evidence;
- fair-value exits that bank convergence rather than requiring settlement;
- three distinct cities instead of one concentrated event;
- account-level position, aggregate, sleeve, city/day, region/day, daily-loss, and drawdown caps.

The two maker entries saved about $3.13 versus the visible taker entry, roughly 39% of the live day’s reported P&L. That is economically meaningful, which is exactly why maker-fill correctness is the first implementation batch.

### 3.3 Why the result is not promotion evidence yet

- All +$7.95 was from `PAPER_CLOSED` exits. The monitor recorded price but not the top-bid size used to justify selling every contract.
- Maker fill evidence contains public trade IDs that were credited to more than one account order. Orders 226/227 and 261/262 share trade IDs across live and research orders on the same market.
- The current readiness artifact says `ready=false`, `REPLAY_REQUIRED`, with 5 of 17 checks passing.
- Only 12 of the required 30 independent weather days are present.
- Day-clustered 95% ROI interval: **−12.79% to +1.96%**.
- After-fee log growth: **−0.0166/day**.
- Recorded-config rescore: 50 trades, 42–8, but **−$181.09**, −4.42% ROI, ending equity $818.91, and 22.4% max drawdown.
- All-time live attribution is only +$5.46 on $312.50 resolved cost; research attribution is −$92.76 on $614.03.

The right lesson is “protect and test the live pattern,” not “increase size.”

---

## 4. Ranked findings

| Rank | ID | Impact | Confidence | Track | Finding |
|---:|---|---:|---|---|---|
| 1 | EX-01 | 10 | high, independently confirmed | execution | Maker fills use the wrong aggressor direction for YES and reuse public trade volume across orders; replay repeats the error |
| 2 | EX-02 | 10 | high, independently confirmed | execution | Full paper exits price every contract at the top bid without checking bid size or book depth |
| 3 | RK-01 | 10 | high, runtime incident + code/test | risk | The research same-day NO basket veto can override a catastrophic stop |
| 4 | MD-01 | 9 | high, runtime incident + cohort | model correctness | Raw nonfinal station maxima can create false p=0/1 certainty against integer daily-report settlement bins |
| 5 | AC-01 | 9 | high, DB/account reconstruction | accounting | Research experiments share the production-intent account and can erase live gains or pause live entries |
| 6 | DB-01 | 9 | high, CI + local stress reproduction | reliability | Concurrent store initialization races on schema migration and shared-account creation |
| 7 | GH-01 | 9 | high, GitHub API | change control | `main` is unprotected while a repository-wide write deploy key exists on production |
| 8 | CI-01 | 9 | high, workflow inspection | verification | GitHub CI omits the SPA and the production Python 3.12 runtime |
| 9 | MD-02 | 7 | high on mechanism, unproven magnitude | model research | One inherited intraday hour/sigma/blend curve is unvalidated across fifteen climates |
| 10 | MD-03 | 7 | high, explicitly accepted mismatch | model research | EMOS trains on previous-run lead cohorts but serves current runs; lead 0 reuses lead-1 coefficients |
| 11 | OP-01 | 7 | high, AWS inspection | recovery | Nightly archive is verified locally but has no configured off-host S3 copy |
| 12 | PR-01 | 6 | high, manifest/GitHub inspection | provenance | Public freshness cannot prove which source revisions generated the AWS artifacts |
| 13 | SEC-01 | 6 | high, GitHub API/workflow | supply chain | Dependency alerts are disabled and workflow dependencies are mutable/unpinned |
| 14 | OP-02 | 5 | high, GitHub history + timers | operations | Two publishers create about 15 Pages commits/hour and routine cancellations; freshness threshold has little jitter margin |
| 15 | DOC-01 | 5 | high | docs | Canonical deployment docs still identify us-east-1 while production is us-west-1 |
| 16 | ST-01 | 5 | high, direct DB check | settlement verification | Current settlements are correct, but the persisted verification table covers only a manual subset |
| 17 | UI-01 | 4 | high | reporting | Dashboard exit thresholds omit series-specific fee rounding used by the monitor |
| 18 | TEST-01 | 3 | high, reproduced | test harness | A tracked-source layout test scans ignored `.local` migration state and fails outside a clean clone |
| 19 | GH-02 | 4 | high facts, action partly conditional | repository hygiene | PR #29, diverged branches, and an unrelated Railway environment need owner reconciliation |

The existing `ready=false` promotion gate is a strength, not a finding. It must remain binding while EX-01 through AC-01 are corrected and the history is replayed.

---

## 5. Execution, risk, and accounting findings

### EX-01 — Maker fills violate side semantics and volume conservation

**Impact:** 10/10  
**Why first:** current paper positions and replay promotion evidence can include fills that could not all have occurred.

**Locations**

- `trading/sfo_kalshi_quant/monitor.py:611-695`
- `trading/sfo_kalshi_quant/replay.py:147` and `:293`
- `trading/tests/test_shared_account.py:248-305`
- `trading/tests/test_replay.py`

**Evidence**

- Monitor line 654 requires `taker_book_side == "bid"` for both resting YES and resting NO orders.
- Official [order-direction semantics](https://docs.kalshi.com/getting_started/order_direction) require the aggressor to be complementary: a resting YES bid is lifted by an ask/NO taker; a resting NO bid is lifted by a bid/YES taker. The public [trade schema](https://docs.kalshi.com/api-reference/market/get-trades) exposes the taker-side fields used to normalize that direction.
- Each resting order independently sums the entire matching trade history. No residual quantity is consumed.
- The test named `test_resting_orders_sharing_market_fetch_trades_once` intentionally expects one trade to fill both incompatible YES and NO orders.
- Replay converts a single public trade into both YES and NO maker events.
- Production evidence contains 16 trade IDs credited to more than one order. Orders 261/262 share the same market, timestamp window, and public trade IDs across live and research; orders 226/227 do the same.

**Required design**

Introduce one shared execution allocator used by the live paper monitor and chronological replay:

```python
@dataclass(frozen=True)
class PublicAggressorTrade:
    trade_id: str
    created_at: datetime
    maker_side: Literal["YES", "NO"]
    yes_price: Decimal
    quantity: Decimal

@dataclass(frozen=True)
class AllocatedFill:
    order_id: int
    trade_id: str
    quantity: Decimal
    price: Decimal
    queue_consumed: Decimal
```

1. Normalize each public trade once into exactly one maker side.
2. Sort trades chronologically and resting orders by price-time priority.
3. Consume queue-ahead before order quantity.
4. Allocate each trade’s residual volume once across compatible orders.
5. Persist per-order allocations, not only a repeated list of source trade IDs.
6. Make allocator output deterministic and idempotent across monitor restarts.
7. Replay must consume the same normalized events and allocator.
8. Decide explicitly whether research orders are counterfactual shadows or participants in the shared queue. AC-01 recommends shadows; if so, they observe the market without consuming live-profile volume.

**Tests**

- YES resting bid fills only from the complementary aggressor direction.
- NO resting bid fills only from the complementary aggressor direction.
- A single trade never produces both a YES and NO maker event.
- Quantity 10 cannot fill two 8-contract same-price orders; the second receives at most 2 after priority and queue.
- Earlier order wins equal-price priority.
- Re-running the same trade batch creates no duplicate fill.
- Monitor and replay allocate an identical fixture identically.
- Production regression fixtures for orders 226/227 and 261/262 expose the old double credit and pass under the declared research-shadow rule.

**Verification**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  trading/tests/test_shared_account.py trading/tests/test_replay.py -q
```

Then run a read-only restatement that reports changed order status/P&L without touching production.

**Rollback:** keep the old evidence immutable, version the allocator, and retain a read-only legacy report. Never silently reinterpret an order in place.

### EX-02 — Full exits assume unlimited top-bid liquidity

**Impact:** 10/10

**Locations**

- `trading/sfo_kalshi_quant/monitor.py:380-406` and `:515-529`
- `trading/sfo_kalshi_quant/models.py:197-207`
- `trading/sfo_kalshi_quant/db.py:1826-1907`

**Evidence**

- `MarketBin.side_bid_size()` already exists, but the monitor reads only `side_bid()`.
- P&L, fees, and close state use all `row["contracts"]` at that one price.
- `close_paper_order` has no executed quantity or depth evidence.
- Every dollar of the July 13 live gain was booked through this full-close path.

**Required design**

Implement quantity-aware lots and partial closes. The conservative intermediate version may refuse a close unless displayed top-bid size covers the full position, but the durable design is:

```python
@dataclass(frozen=True)
class ExitExecution:
    requested_quantity: Decimal
    executed_quantity: Decimal
    vwap: Decimal
    fee: Decimal
    book_snapshot_id: int
    levels: tuple[BookLevelFill, ...]
```

- Persist bid price, bid size, fetch time, and source snapshot on every close decision.
- Walk available depth if the API provides it; otherwise execute only the displayed top level and leave the remainder open.
- Add `paper_order_lots` or equivalent immutable position/close events. Derive order status: filled, partially closed, or fully closed.
- Calculate fees on executed quantity only.
- Risk and marked equity use remaining quantity after partial close.
- The dashboard exposes partial exits and recorded liquidity.

**Tests**

- 10 contracts with bid size 3 cannot become fully closed at the top price.
- Partial close realizes 3 contracts and leaves 7 open with reconciled cash/equity.
- A second close consumes only the remaining 7.
- Concurrent settle/close remains idempotent.
- July 13 orders replay under recorded/available depth; if depth cannot be reconstructed, label reported exit P&L “unverified,” not zero and not trusted.

**Verification**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  trading/tests/test_monitor_model_veto.py trading/tests/test_shared_account.py \
  trading/tests/test_strategy_research.py -q
```

### RK-01 — Basket veto can override the catastrophic stop

**Impact:** 10/10

**Locations**

- `trading/sfo_kalshi_quant/exits.py:209-245`
- `trading/sfo_kalshi_quant/monitor.py:130-159` and `:462-490`
- `trading/tests/test_monitor_model_veto.py:1049-1095`

**Evidence**

- `decide_exit` correctly disables the model veto beyond `model_veto_max_loss_roi`.
- The monitor then applies a second research basket veto with no catastrophic bypass.
- The current test explicitly expects a catastrophic stop to be held when a research basket model still supports the leg.
- Production order 311, Dallas 89–90°F NO, entered at $0.72. It was held around −67% and again near −95%; it closed only after the 90-minute model read became stale, realizing −$8.3045 at about −96.1%.

**Fix**

- Return a typed exit decision containing `catastrophic: bool` or a priority enum.
- Apply the basket rule inside the same decision engine, below the unconditional catastrophic priority.
- Remove the test that blesses catastrophic override; replace it with a test that proves the opposite.
- Persist model snapshot timestamp and age in every monitor decision.
- Separately evaluate the same-day model heartbeat. It may refresh probability journals for already-open positions, but it must be structurally unable to place orders.

**Verification**

- Replay order 311 and assert close at the first executable mark at/below the configured catastrophic loss.
- Assert the heartbeat spans the 14:00 cutoff, records zero orders, and stale reads fail safe.

### MD-01 — Raw station observations can create false exact settlement certainty

**Impact:** 9/10

**Locations**

- `trading/sfo_kalshi_quant/forecast.py:115-205`
- `trading/sfo_kalshi_quant/probability.py:309-334`
- `trading/sfo_kalshi_quant/models.py:223-241`

**Evidence**

- Before final daily truth, the adapter uses `MAX(temp_f)` from raw station observations.
- The probability conditioner hard-zeros any bin whose continuous upper interval is below that raw maximum and can create a point mass.
- Settlement uses an integer daily climate report. Philadelphia order 188 observed a raw maximum of 87.8°F, which made the 86–87°F bin look impossible, while the final official integer high was 87°F and the NO position lost $11.63.
- Resolved p≥0.99 cohorts are poor: live 0–2 (−$11.90) and research 3–6 (−$51.49).

**Fix**

- Represent observation measurement/rounding uncertainty explicitly. A nonfinal raw observation must not be treated as an exact settlement value.
- Use an observation likelihood over possible official integer highs, or conservatively widen the conditioning boundary by a calibrated station/report uncertainty.
- Forbid exact 0/1 posterior output from nonfinal raw observations.
- Add a temporary safety gate for p≥0.99 candidates driven by nonfinal raw observations until the calibrated mapping passes walk-forward verification.

**Verification**

- Boundary fixture: raw 87.8°F, final daily report 87°F, market bin 86–87°F.
- Replay every historical p≥0.99 decision and report calibration, P&L, and which source created the certainty.
- Exact point mass remains allowed only when `is_complete=true` final truth supports it.

### AC-01 — Research losses contaminate the production-intent goal

**Impact:** 9/10

**Locations**

- `trading/sfo_kalshi_quant/account.py:12-29` and `:49-117`
- `trading/sfo_kalshi_quant/db.py:234-319` and `:2075-2097`
- `trading/sfo_kalshi_quant/paper.py:308-335`
- research shadow/order schema and Strategy Lab accounting views

**Evidence**

- The account ledger, realized equity, daily P&L pause, drawdown, cash, and aggregate risk include both live and research orders.
- Profile-specific `paper_equity()` exists for Kelly sizing, but final account capacity is still applied from shared state.
- July 13 live +$7.95 became only +$3.34 shared after research −$4.60.
- All-time live attribution is +$5.46; research attribution is −$92.76.
- Research is documented as paper-only and never a real-money candidate, yet its exploratory losses can reduce deployable live equity and trigger shared pauses.

**Fix**

- Make research **shadow-only** by default: it records decisions, counterfactual fills, marks, outcomes, and a separate virtual bankroll, but does not reserve or mutate the production-intent live account.
- Keep an optional explicit “shared-capital experiment” mode for research questions that truly need queue/capital competition; it must be off by default and visually distinct.
- Define the $1,050 target against corrected live-profile realized equity from a versioned baseline.
- Continue displaying shared historical accounting, live attribution, and research attribution; do not erase the −$87.30 history.
- Add an immutable accounting-policy version and transition event.

**Tests**

- Research loss cannot change live available cash, live drawdown, live daily pause, or live position room.
- Research shadow fills do not consume live maker trade volume.
- Each ledger reconciles independently; the historical combined view still reconciles.
- Target progress uses live realized and marked equity, never resolved-cost ROI.

---

## 6. Reliability, CI, and change-control findings

### DB-01 — Store initialization is not process-safe

**Impact:** 9/10

**Locations**

- `trading/sfo_kalshi_quant/store/schema.py:481-515`
- `trading/sfo_kalshi_quant/db.py:171-210`
- `trading/tests/test_scan_context_normalization.py:316-336`

**Evidence**

- GitHub Actions run `29161518056` failed with `sqlite3.OperationalError: database schema has changed` at schema line 496.
- Later green runs did not include a code fix to this path.
- Fresh adversarial stress reproduced `UNIQUE constraint failed: paper_accounts.account_id` with 16 workers and, after 344 attempts, with the same four-worker shape as the test.
- The recovery branch catches one `ALTER` collision and immediately performs another racing `PRAGMA table_info`.
- Shared-account creation is SELECT-then-INSERT rather than atomically idempotent.

**Fix**

- Serialize the complete initialization/migration/account-bootstrap path with a database-level `BEGIN IMMEDIATE` transaction and bounded busy/schema retry around the transaction boundary.
- Make account creation and opening-ledger creation atomically idempotent.
- Do not patch only the failing PRAGMA; there are at least two independently reproduced races.

**Verification**

```bash
for i in $(seq 1 1000); do
  PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
    trading/tests/test_scan_context_normalization.py::test_init_concurrently_migrates_legacy_decision_schema \
    -q || exit 1
done
```

Also run fresh-DB and legacy-DB multi-process initializers and assert one account, one opening event, complete schema, and `PRAGMA integrity_check='ok'`.

### GH-01 — Protect `main` before rotating the write deploy key

**Impact:** 9/10

**Evidence**

- GitHub reports no branch protection and no rulesets.
- The 69-commit post-audit tranche landed directly on `main`.
- Deploy key `sfo-weather-pages-lightsail` is repository-wide write-enabled and named for a retired host.
- The active publisher is on us-west-1; the old east box is quiesced pending decommission.

**Plan**

1. After CI-01 lands, create a default-branch ruleset requiring the Python and web checks.
2. Require PRs, block force pushes/deletion, and include administrators. Zero approving reviews is acceptable for a single-owner repository; checks are not optional.
3. Confirm the deploy key exists only on the active west publisher.
4. Rotate/remove retired copies and rename through replacement.
5. Verify Pages publication still works while direct deploy-key writes to `main` are rejected.

**Operator checkpoint:** GitHub settings and key rotation are external mutations.

### CI-01 — CI omits the SPA and production Python

**Impact:** 9/10

**Location:** `.github/workflows/verify.yml:13-36`

**Fix**

- Split Python and web into independent jobs.
- Test Python 3.12 and 3.13; run Semgrep once.
- Add a Bun job:

```bash
bun install --frozen-lockfile
bun run lint
bun run test
bun run build
bun run bundle:check:observed
```

- Resolve private HeroUI Pro package authentication without exposing secrets. If CI cannot install it safely, stop and ask for the owner’s chosen secret/package policy.
- Require both jobs through GH-01.

### TEST-01 — Layout test is not hermetic

**Impact:** 3/10

**Location:** `forecaster/tests/test_research_layout.py:48-54`

**Evidence:** local audit result was 959 Python passes and one failure because ignored `.local/migration-stage/.../pyproject.toml` was discovered by `ROOT.rglob`. A clean GitHub clone passes.

**Fix:** inspect `git ls-files '**/pyproject.toml'` or explicitly limit the assertion to tracked project source. Do not delete `.local` state merely to satisfy a test.

### SEC-01 — Complete the repository security gate

**Impact:** 6/10

- Enable vulnerability alerts and Dependabot security updates.
- Add weekly Python and Bun/JavaScript dependency updates.
- Pin `actions/checkout`, `actions/setup-python`, Bun setup, and other actions to reviewed full SHAs.
- Pin Semgrep to a reviewed version; publish SARIF or add code scanning.
- Restrict allowed Actions to GitHub-owned plus an explicit allowlist.
- Preserve secret scanning and push protection, which are already enabled.

**Operator checkpoint:** repository settings are external mutations.

---

## 7. Model-validation track

These items must not displace execution integrity work. They are challenger experiments, not permission for direct production coefficient changes.

### MD-02 — Validate intraday curves by city, season, and local hour

**Impact:** 7/10 as model risk; not a proven deterministic bug.

**Locations**

- `trading/sfo_kalshi_quant/probability.py:363-424` and `:518-589`
- `trading/sfo_kalshi_quant/forecast.py:750-810`
- `trading/sfo_kalshi_quant/config.py:566+`

**Evidence and counterevidence**

- Remaining heat, intraday sigma, and blend-weight curves are global and originated in the SFO-only implementation; comments cite one SFO loss.
- Observations, forecast center, markets, EMOS distribution, and timezone are already city-specific. Therefore “the full SFO model is used everywhere” is false.
- The unproven question is whether the remaining global hour-to-sigma/weight assumptions calibrate across Miami, Phoenix, Seattle, Chicago, and the other cities.

**Research plan**

1. Build a leakage-safe table keyed by city, local date, observation timestamp/hour, observed-high-so-far, final official integer high, season, forecast center, and remaining rise.
2. Compare the shared curve against hierarchical partial pooling: global prior → climate/season group → city adjustment.
3. Walk forward by independent date. Report CRPS/Brier, bin calibration, p≥0.9 reliability, and after-fee trading impact.
4. Require enough support per cell; never create 15 unconstrained lookup tables from sparse data.
5. Until the challenger passes, cap non-SFO intraday blend weight or use the safer day-ahead distribution. This is a configuration proposal requiring replay evidence, not an immediate production change.

### MD-03 — Match EMOS training and serving horizons

**Impact:** 7/10 as accepted distribution shift.

**Locations:** `forecaster/emos_forecast.py:279-303` and `:425-465`

**Evidence:** training uses historical `previous_day1`/lead cohorts while serving current stitched runs; same-day lead 0 intentionally reuses lead-1 coefficients. The code and tests document this choice.

**Research plan**

- Archive the exact live model vector and issuance/initialization metadata used at stable decision times.
- Construct matched-horizon training cohorts from those immutable records, or reconstruct fixed initialization runs where defensible.
- Compare current and matched-horizon EMOS walk-forward by city/lead/season.
- Do not swap coefficients unless calibration and downstream after-fee replay improve without worsening independent-day drawdown.
- If lead-0 support remains thin, use explicit uncertainty inflation rather than pretending the lead-1 fit is identical.

### Readiness re-evaluation

After EX-01, EX-02, RK-01, MD-01, and AC-01:

1. Declare an `execution_model_version` and `accounting_policy_version`.
2. Run a dry, read-only historical restatement from raw immutable evidence.
3. Mark orders with insufficient evidence as `UNVERIFIABLE`, not filled by assumption.
4. Generate a chronological account replay that consumes cash, risk, queue volume, partial exits, fees, and finality in event-time order.
5. Compare legacy and corrected metrics side by side.
6. Reset the promotion clock at the first date with fully trustworthy execution evidence; do not mix incompatible pre-fix and post-fix observations in a headline readiness score.

---

## 8. Operations, provenance, and documentation

### OP-01 — Add off-host archive recovery

Nightly archive/prune succeeded and verified 130,824 decisions before pruning 124,627. The remaining gap is disaster recovery: the S3 bucket is unset, so the verified archive exists only on the EC2 disk.

**Plan**

- Provision a private encrypted versioned S3 bucket with least-privilege instance access.
- Upload only after local export/manifest verification.
- Verify remote object hash and manifest before allowing prune.
- Add lifecycle tiers appropriate to the dataset and a quarterly restore drill to a disposable path.
- Alert when remote verification is unavailable; define whether prune blocks or a bounded local grace applies.

**Operator checkpoint:** bucket/IAM creation and production environment changes.

### PR-01 — Publish exact build provenance

`publication_manifest.json` proves artifact freshness/hashes but lacks the main source SHA, deployed trading SHA, forecaster SHA, and SPA build SHA.

**Plan**

- Generate an immutable `build_info.json` during sync/deploy.
- Include source/deployed/web SHAs, deployment timestamp, execution-model version, accounting-policy version, and schema version in the public manifest.
- Put the source SHA in the `gh-pages` commit message.
- Optionally register deployments against a protected `aws-production` GitHub environment.
- Health checks compare manifest provenance with the deployed build-info file and expected main revision.

### OP-02 — Reduce publication churn without relaxing freshness

**Evidence:** about 397 `gh-pages` commits occurred from July 13 00:00Z through July 14 02:39Z, roughly 15/hour, with routine cancellations. Operational publication runs every five minutes; Strategy Lab runs every fifteen minutes; both publish.

**Fix**

- Let Strategy Lab build its artifact but leave publishing to the next operational cycle, adding at most five minutes.
- Keep one Git pusher and one publication lock.
- Preserve the five-minute operational SLA.
- Set watchdog grace from cadence + randomized delay + observed p95 build/publish duration, rather than a brittle flat 10-minute boundary. Alert only after consecutive failure or an evidence-based margin.
- Target about 12 deployments/hour and no routine cancellations.

### DOC-01 — Reconcile the production region

`CLAUDE.md` correctly points to us-west-1, but `docs/aws_deployment.md:3` and `trading/deploy/aws/README.md:5` still state us-east-1. Update canonical runbooks, host history, access file naming, key inventory, rollback/decommission state, and every operator command. Do not hardcode private secret values.

### ST-01 — Persist settlement verification continuously

The direct audit found 37/37 settled orders matched final truth, but `paper_settlement_verifications` contains only 18 rows from a manual July 11 check.

- Run a read-only verification automatically after settlement.
- Store one idempotent verification per settled order/finality revision.
- Alert on missing final truth, booked/official mismatch, or changed final product.
- Verification never edits P&L automatically; it opens an incident/restatement path.

### UI-01 — Use the same fee schedule in the dashboard and monitor

`exits.py:72-102` computes threshold bids without a series ticker, while the monitor supplies the ticker for series-specific rounding. Extend the helper signature with fee configuration and series ticker; pass it from `strategy_lab/paper_card.py:925-926`. Add a boundary test that the displayed exit bid produces the same net value used by the monitor.

### GH-02 — Owner decisions, after core work

- PR #29 is conflicting, two commits ahead and 85 behind. Compare its unique fast-publication behavior, preserve only measured value, then close as superseded or replace with a clean PR.
- Reconcile local worktrees before deleting any diverged `codex/*` branch; enable automatic branch deletion after merge afterward.
- Confirm whether GitHub environment `immigration_app / production` and its inactive Railway deployments are intentional. Remove access/environment only with owner approval and retained evidence.

---

## 9. What is already strong and must not regress

1. **First-audit implementation:** final truth gating, archive-gated prune, source precedence, defensive SPA handling, and major module splits are materially present.
2. **Current automated baseline:** SPA 224/224 tests, lint, icon checks, and build pass locally. Python had 959 passes and only TEST-01 failed locally; clean CI had 959 passes and one skip. Semgrep scanned 428 tracked files with zero findings.
3. **Healthy production:** all nine current services succeeded at final inspection; artifacts were fresh; the west host matched local/origin source hashes.
4. **Reconciled accounting:** shared account reconciliation difference is effectively zero.
5. **Settlement integrity:** all directly checked settled orders match final daily truth.
6. **Risk layering:** position, aggregate, live/research sleeve, city/day, region/day, daily loss, 10% half-size, and 15% pause controls exist.
7. **Signal discipline:** the live favorite band, after-fee lower-bound edge, uncertainty-aware Kelly sizing, and maker-first intent are plausible contributors to July 13 and should remain the control.
8. **Repository controls already present:** secret scanning, push protection, and read-only default workflow token permissions are enabled.
9. **SPA budget:** observed landing bundle was about 172 KiB gzip JS and 15 KiB gzip CSS, under current 300/40 KiB budgets.

---

## 10. Rejected claims and tempting wrong fixes

Do not reintroduce these as findings or implementation shortcuts without new evidence:

1. **“The account gained 5% on July 13.”** False; shared realized gain was about 0.334% of initial capital.
2. **“The shared day made $7.95.”** False; $7.95 is the live slice, before research −$4.60.
3. **“Three wins prove edge.”** False; readiness remains negative and the confidence interval crosses zero.
4. **“Settlement leakage caused the wins.”** Rejected; the winners entered day-ahead and exited before final truth, and settled rows matched final reports.
5. **“A production outage caused the behavior.”** Rejected; no current outage was present.
6. **“Maker execution has no queue evidence.”** False; queue-ahead and later public trades are recorded. The real defects are side semantics and volume allocation.
7. **“All fifteen cities use the entire SFO model.”** Too strong. The global intraday curve is unvalidated, but city observations, forecast centers, markets, EMOS distributions, and timezones are city-specific.
8. **“Lead-1 reuse is an accidental regression.”** False; it is documented and tested. It is still an accepted model-risk gap worth evaluating.
9. **“Raise Kelly/caps to reach $1,050 faster.”** Rejected until corrected replay passes. The current artifact itself labels frequency above target and recommends risk review.
10. **“Delete ignored local state so the test passes.”** Rejected; fix TEST-01’s tracked-source boundary.
11. **“Rewrite historical P&L after fixing execution.”** Rejected; preserve original events and publish versioned restatements.

---

## 11. Ordered implementation plan

Each batch ends with a fresh-context review. Do not begin model tuning while Batch A is incomplete.

### Batch 0 — Freeze the evidence and establish the red tests

**Files:** new/updated tests and version constants only; no production settings.

- [ ] Record audit HEAD, public artifact timestamp, DB schema version, and source hashes in test fixtures/implementation notes.
- [ ] Add failing tests for EX-01, EX-02, RK-01, MD-01, AC-01, and DB-01 before changing implementation.
- [ ] Add production-derived, de-identified fixtures for orders 261/262, 226/227, 311, and 188.
- [ ] Confirm current full suite baseline and save command/results in the execution report.
- [ ] Fresh verifier confirms every red test fails for the intended reason.

### Batch A — Repair execution and risk integrity

**Dependencies:** Batch 0.  
**Order:** EX-01 → EX-02 → RK-01.

- [ ] Extract the shared maker event normalizer/allocator; update monitor and replay.
- [ ] Add immutable fill allocations and idempotency keys.
- [ ] Add partial exit/lot accounting and recorded depth evidence.
- [ ] Move basket-veto logic under catastrophic-stop priority.
- [ ] Update dashboard/reporting schemas tolerantly for missing legacy fields.
- [ ] Run targeted tests, complete Python tests, and read-only historical restatement.
- [ ] Fresh verifier checks side direction, quantity conservation, cash reconciliation, concurrent close/settle, and that legacy evidence remains immutable.

**Stop condition:** if the prediction-market trade feed lacks enough fields to establish side/quantity deterministically, stop maker promotion and conservatively label affected orders unverified. Do not invent fills.

### Batch B — Repair observation certainty and account separation

**Dependencies:** Batch A.

- [ ] Implement MD-01 observation/report uncertainty with boundary tests.
- [ ] Convert research placement to shadow-only under a versioned accounting policy.
- [ ] Separate live and research cash, pauses, drawdown, and target reporting while preserving the combined historical view.
- [ ] Add explicit public labels for legacy, restated, live, and research metrics.
- [ ] Replay order 188/p≥0.99 cohort and prove research cannot alter live capacity.
- [ ] Fresh verifier audits settlement intervals, finality, and every ledger invariant.

### Batch C — Make initialization and CI deterministic

**Dependencies:** may run in parallel with Batch B if files do not overlap.

- [ ] Serialize store initialization and atomically bootstrap the account.
- [ ] Run 1,000 repeated thread tests plus a multi-process stress test.
- [ ] Fix the tracked-source layout test.
- [ ] Add Python 3.12/3.13 and complete Bun jobs.
- [ ] Pin workflow dependencies.
- [ ] Fresh verifier reproduces the old race against the parent commit and fails to reproduce it against the candidate.

### Batch D — Rebuild trustworthy performance evidence

**Dependencies:** Batches A–C.

- [ ] Version execution/accounting semantics.
- [ ] Run the immutable, read-only restatement and chronological replay.
- [ ] Regenerate readiness with incompatible legacy evidence separated.
- [ ] Publish changed-order counts, unverifiable counts, P&L delta, max drawdown, clustered interval, calibration, and per-city/per-side results.
- [ ] Reset the 30-independent-day clock at the earliest trustworthy version boundary.
- [ ] Do not alter live profile thresholds from the July 13 sample.

### Batch E — Run model challengers

**Dependencies:** Batch D baseline.

- [ ] Build MD-02 city/season/hour validation dataset and hierarchical challenger.
- [ ] Build MD-03 matched-horizon EMOS archive/challenger.
- [ ] Evaluate one change at a time against the unchanged live control.
- [ ] Require walk-forward, after-fee, independent-date results and multiple-comparison-aware reporting.
- [ ] Promote no parameter automatically. Produce a recommendation and evidence package for owner approval.

### Batch F — Harden GitHub and operations

**Dependencies:** CI-01 must be green before GH-01.

- [ ] With approval, add the branch ruleset and require checks.
- [ ] With approval, rotate/restrict the production deploy credential.
- [ ] Add provenance/build info.
- [ ] Consolidate publication pushing and derive freshness margins.
- [ ] With approval, configure and restore-test off-host archives.
- [ ] Reconcile docs, PR #29, branches, and the Railway environment.
- [ ] Fresh verifier checks AWS health, public artifact freshness/provenance, GitHub checks, and rollback instructions.

### Full verification gate

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  trading/tests forecaster/tests -q
bun install --frozen-lockfile
bun run lint
bun run test
bun run build
bun run bundle:check:observed
bash scripts/verify_project.sh
```

For any frontend changes, also clear disposable local runtime state as instructed by `AGENTS.md`, serve `dist/`, capture desktop and real-mobile screenshots, drive the page, and read DOM state back. Static inspection is not completion.

---

## 12. Copy/paste handoff prompt for Claude Fable 5

Use high effort. Use xhigh only for the maker allocator, partial-exit ledger, chronological replay, and accounting migration if the interface provides that option.

```text
You are implementing the WeatherEdge second-audit plan in:

/Users/jaxson/develop/WeatherEdge/docs/AUDIT-PLAN-2026-07-13.md

Outcome

Make WeatherEdge’s paper P&L execution-credible, preserve the promising live-profile behavior, isolate research experiments from production-intent equity, and rebuild the readiness evidence. The economic goal is at least $1,050 realized live-profile equity after fees, but this is an evidence-gated target—not a guarantee and not permission to increase risk.

Read first

1. Read the repository’s AGENTS.md instructions and follow them.
2. Read the complete dated audit plan above.
3. Read the prior docs/AUDIT-PLAN.md only for historical context; do not repeat already-fixed work.
4. Inspect current git status and preserve every user-owned change. In particular, do not overwrite the existing CLAUDE.md working-tree edit or the prior untracked audit plan.

Execution order

Execute the batches in §11. Begin with Batch 0 red tests. Do not tune strategy parameters or change risk limits before Batches A–D establish trustworthy execution and accounting. Use the dated plan as the source of truth.

Core invariants

- Paper-only: never enable live/real-money orders.
- A public trade has one aggressor direction and finite quantity. Allocate it once in price-time order.
- Never close more quantity at a quote than recorded liquidity supports.
- Catastrophic stop priority cannot be vetoed.
- Nonfinal raw observations cannot create false exact settlement certainty.
- Research shadows cannot consume live cash, live risk room, live maker volume, or trigger live pauses.
- Historical journal events are immutable. Restatements are versioned and side-by-side.
- Final settlement requires final official truth.
- Public parsing remains tolerant of missing legacy fields.
- Public copy says “prediction market,” never the venue name.
- Do not add assistant attribution, co-author trailers, generated-by text, or assistant metadata.

Autonomy boundaries

You may inspect and edit local repository files, add tests, run local commands, and create read-only reports. Do not commit, push, merge, close PRs, change GitHub settings, rotate keys, write to AWS, change systemd, sync production, create S3/IAM resources, restart services, or mutate the production database unless the user explicitly authorizes that exact external action. Read-only GitHub/AWS checks are allowed when needed and must be reported.

How to work

- Ground each implementation step in current code and tool results. Treat plan line numbers as hints if code moved.
- Use test-driven development for each correctness defect: prove the old behavior, make the smallest coherent fix, then refactor.
- Delegate bounded independent reads/tests to parallel subagents when useful. Do not give overlapping file ownership. Use long-lived subagents only for genuinely independent tracks.
- After each batch, use a fresh-context adversarial verifier whose job is to disprove correctness and find regressions.
- Keep progress updates short: completed outcome, evidence, next action, and any blocker.
- Do not perform unrelated cleanup. Preserve backward-compatible parsing and existing user changes.
- Prefer shared execution/accounting primitives over duplicate monitor/replay logic.

Validation

Run the targeted commands in each finding and the full gate in §11. For concurrency, run the full 1,000-iteration and multi-process stress tests. For historical performance, use a read-only copy/snapshot and produce legacy-versus-restated metrics. Mark insufficient execution evidence UNVERIFIABLE; never fill gaps with optimistic assumptions.

External checkpoints

Pause and ask before every external mutation listed in Batch F. State the exact action, expected effect, rollback, and evidence that the local candidate is ready. Do not bundle multiple permissions into one vague request.

Stop conditions

Stop and report a blocker if required market fields cannot prove maker side/quantity, a schema migration cannot be made rollback-safe, a private package prevents reproducible CI, production/local source provenance conflicts, or a requested external change lacks authorization. Do not guess around these conditions.

Completion report

When all locally authorized work is complete, report:

- files and behavior changed by finding ID;
- targeted and full test results with counts;
- concurrency stress results;
- legacy versus corrected P&L/readiness, including unverifiable orders;
- risks and rollback path;
- external actions still awaiting approval;
- whether the $1,050 promotion criteria are met. If not, say so plainly and identify the next evidence-gated step.

Do not claim completion while tests, replay, independent verification, or required authorized rollout checks remain unfinished.
```

---

## 13. Audit closeout snapshot

At audit close:

- local `main` and `origin/main` were `2a1a771a`;
- deployed west-host code hashes matched that revision;
- working tree already contained a user-owned `CLAUDE.md` modification and untracked `docs/AUDIT-PLAN.md`; both were preserved;
- this dated plan is the only intended new file from the second audit;
- no production, GitHub, AWS, or trading-state mutation was performed.

The first implementation priority is **EX-01**, then **EX-02**, **RK-01**, **MD-01**, **AC-01**, and **DB-01**. Only after corrected replay should WeatherEdge consider model or sizing changes aimed at the $1,050 target.
