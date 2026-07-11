# WeatherEdge — Full Codebase Audit (plan-only)

> Paste this as the opening message of a fresh Claude Fable 5 session, run at **effort: xhigh**.
> It is written for Fable 5 specifically (boundaries, evidence-grounding, subagent verification).

---

## Why you're doing this (the intent)

WeatherEdge is a production system that forecasts daily-high temperatures for 15 US cities (an NWP-ensemble → EMOS/MOS statistical post-processing → CLI-settlement-truth pipeline) and paper-trades the Kalshi temperature markets on those forecasts (selectivity gates → Kelly sizing → maker-first execution → exits). The forecaster is Python, the trading engine is Python (`trading/sfo_kalshi_quant`), the dashboard is a React/TypeScript SPA, and it runs on an AWS EC2 box driven by systemd timers.

The codebase has grown fast and organically. I want a rigorous, evidence-backed audit of the **entire** repository so I can pay down debt and make the overall system faster, cleaner, and more correct. **You are the analyst, not the implementer.** Your single deliverable is one implementation-ready remediation plan. A *different* LLM, with none of your context, will read your plan and execute the fixes — so the plan has to stand completely on its own.

## Your mandate — read this twice

**Produce a plan. Change nothing.** You may create/modify exactly two files: the deliverable `docs/AUDIT-PLAN.md` and a scratch notes file for yourself. You must **not** edit, move, or delete any other file; **not** run `git rm`, `git commit`, `git push`, `git checkout`, or any code formatter/linter that writes; and **not** "fix while you're in there." Every problem you find becomes an entry in the plan, never an edit. If you feel the pull to fix something, that pull is the signal to write a precise finding instead.

Everything else is read-only analysis: read files, grep, build import graphs, run **read-only** commands, run existing tests to observe behavior (do not modify them), profile, and search the web.

## The environment

The code does **not** live on this machine. Read the auto-loaded `CLAUDE.md` in the working directory for the exact access pattern (this is a thin client; the repo is on a remote build Mac reached over SSH, and there is a second `CLAUDE.md` inside the repo describing the production EC2 box). Orient yourself from those two files before anything else. Reach the code with the SSH pattern they document; your local file tools see this laptop, not the repo.

Before auditing, spend your first pass on **orientation so you don't re-flag recent or known work**:
- Read the last ~40 commits (`git log --oneline -40` and skim diffs of anything large). The system had heavy recent work (trading taken out of shadow mode, EMOS serve-time recalibration + a same-day lead-0 serve, an EC2 migration off Lightsail, a web rebrand). Do not report things that were just changed as if they were neglected.
- Read `docs/codebase_audit_2026-06-15.md` (a prior audit) and the other `docs/*.md` — but treat them as possibly stale themselves (their staleness is itself in scope).
- Build a map first: top-level layout, entry points, the CLI subcommand registry in `trading/sfo_kalshi_quant/cli.py`, what each systemd unit invokes (`trading/deploy/aws/`), the Python import graph, the SPA route/lazy-import graph, the dependency manifests (`pyproject.toml`, `package.json`), and where the test suites are. Ground the whole audit on this map.

## What to audit

Cover the whole repo. For each dimension, the output is findings-with-fixes, not fixes.

1. **Unused / stale / dead** — orphaned modules, unreferenced functions/classes, dead branches, stale docs, unused dependencies, dead config/env vars, duplicated files, leftover scratch/experiment code, commented-out blocks.
2. **Bugs & correctness** — logic errors, silent failures and swallowed exceptions, incorrect error handling, race conditions, resource leaks, off-by-one / boundary / timezone bugs (this system is full of local-standard-time settlement logic and UTC/local conversions — look hard there), float/precision issues in sizing and settlement, mismatches between what a comment claims and what the code does.
3. **Performance — of the overall model and the system** — anything degrading forecast accuracy (calibration leaks, data-staleness paths, ensemble-weighting flaws, train/serve skew, leakage), and anything degrading runtime (N+1 or unindexed SQLite queries, full-table scans on the growing snapshot tables, redundant recomputation, memory blowups, hot loops, needless API calls against metered budgets, inefficient pandas/numpy).
4. **Structure & quality** — files that are too large or doing too much (for reference, `strategy_research.py` is ~4,300 lines, `cli.py` ~4,000, `db.py` ~3,700), poor separation of concerns, god-objects, tangled dependencies, real (not speculative) missing abstractions, inconsistent patterns, weak module boundaries, test gaps on critical logic.

**Research the norm.** Use web search/fetch to establish how systems like this are normally structured, then evaluate WeatherEdge against that and cite what you used. At minimum research: NWP-ensemble → statistical post-processing (EMOS/NGR/MOS) forecasting pipeline architecture; quantitative / prediction-market trading system layering (signal → risk/sizing → execution → monitoring/settlement); idiomatic large-Python-project structure; and modern React/TS dashboard structure. Recommend a concrete target structure where the current one diverges for real reasons, not fashion.

## How to work

- **Parallelize with subagents.** Fan the audit out across independent threads — by dimension (dead-code, bugs, performance, structure) and by subsystem (forecaster, trading engine, SPA, deploy/infra, docs). Keep working while they run; step in if one drifts or lacks context. Long-lived subagents that hold context across a subsystem are cheaper and better than one-shot ones.
- **Ground every finding in a real tool result.** No finding enters the plan on intuition. "Dead" means you showed zero references (grep across the whole repo *plus* the dynamic-dispatch surfaces below). A "bug" means a concrete input/state → wrong output/crash you can point at. A "perf" finding means a measurement, a query plan, a complexity argument, or a specific hot path — not a vibe. If you cannot get evidence, either get it or leave the finding out and note it as unproven.
- **Verify before you write it down.** Before a candidate finding goes in the plan, hand it to a *separate, fresh-context* subagent whose job is to disprove it. Keep only findings that survive. Dead-code and "bug" claims are where false positives cluster — kill them here. Keep a short list of candidates that were considered and rejected, and why, so the implementer doesn't rediscover them.
- **Guard the dead-code scan against dynamic use.** Before declaring anything unused, check every way this repo reaches code indirectly: the `cli.py` subcommand dispatch table, invocations inside the systemd `.service`/`.timer` units and the `deploy/aws/*.sh` scripts, `pyproject.toml`/`package.json` entry points and scripts, dynamic imports / `getattr` / registries / `__all__`, test-only usage, and SPA lazy routes / barrel re-exports. A module run only by a nightly timer or a CLI subcommand is **live**.
- **Check your own work at intervals.** As the plan grows, periodically re-verify a sample of already-written findings with a fresh subagent against the actual code, and reconcile the impact ranking. Fix the plan, not the code.

## Rank by impact

Score every finding by its own estimated impact (correctness risk, effect on forecast accuracy or trading PnL, runtime/cost, and maintainability), and order the whole plan by that score — there is no pre-set priority between subsystems. Make the score and its rationale explicit per finding so the implementer can triage.

## The deliverable: `docs/AUDIT-PLAN.md`

Write one self-contained document. Assume the implementer LLM has never seen the codebase and cannot ask you anything.

**Top of the document:**
- Executive summary: total findings, the ranked table (id · category · subsystem · impact · confidence · one-line), and the 3–6 headline themes.
- Architecture assessment: current structure vs. the researched norm (with citations), an oversized-file / module-boundary map, notable import-graph observations, and a recommended target structure for the parts that should move.
- Suggested execution order for the implementer: group findings into safe independent batches, mark dependencies/conflicts between them, and name the "start here" set (lowest-risk, highest-value first).
- A short "considered and rejected" list (false-positive candidates you disproved).

**Each finding, in a consistent schema:**
`id` · `title` · `category` · `subsystem` · `impact` (score + why) · `confidence` (and how verified) · `location` (file:line ranges) · `evidence` (the concrete proof) · `root_cause` · `why_it_matters` · `fix` (precise and implementation-ready: exact edits, or the transformation and target layout if it's a restructure) · `verification` (the exact commands/tests that confirm the fix) · `risk` (blast radius + rollback) · `depends_on` / `conflicts_with` (other ids) · `effort` (S/M/L).

The bar for `fix`: a competent implementer with no context can execute it and know when it's done. Vague advice ("consider refactoring this") is not a finding — either specify the refactor concretely or drop it.

## Operating instructions

- You are operating autonomously. I am not watching in real time and cannot answer mid-run, so don't ask "want me to…?" — the scope is set: audit everything, write the plan, change nothing. Proceed end to end. If you hit something genuinely blocking (e.g., you cannot reach the repo at all), say so plainly and stop; otherwise keep going. Before ending your turn, check your last paragraph — if it's a plan, a question, or a promise of work you haven't done, do that work now instead.
- Before reporting progress or a finding as confirmed, audit the claim against a tool result from this session. Report only what you can point to; if something is unproven, label it unproven. Don't fabricate coverage.
- Keep a memory/notes file (one lesson per line, why it mattered) so you don't relearn the codebase as you go; update it rather than duplicating.
- Don't expand scope into fixes, refactors, or "tidying." The plan is the product.
- Final message to me: lead with the outcome — how many findings, the top few by impact, and where the plan is. Write it for someone who didn't watch the run: complete sentences, no working shorthand, no invented labels, each file or identifier in its own plain clause. Clear beats short.
