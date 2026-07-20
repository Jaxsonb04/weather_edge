# AI-Assisted Development

WeatherEdge is built with heavy use of AI coding agents. That is a deliberate
choice, and this document explains how it works, what stops it from producing
plausible-looking garbage, and where it has failed.

The short version: **AI writes a lot of the code here; it does not decide what
is correct.** Correctness is decided by a verification harness that runs
independently of whatever produced the change, and by a review process designed
around the specific ways AI-generated code fails.

## Why write this down

An employer looking at this repository will notice AI involvement — the commit
history has co-author trailers, and the design docs are written to be read by
agents as well as people. Concealing that would be both dishonest and pointless.
The interesting question is not *whether* AI was used but whether the author can
tell when it is wrong. This document is the answer to that question.

## The verification harness

Every change, regardless of origin, has to clear the following before it can
reach `main`:

| Gate | What it enforces |
|---|---|
| **1,869 tests** across 122 files | Behavioral coverage; the Python test corpus (~56k lines) is roughly the same size as the source it covers (~65k lines) |
| **Two Python versions** in CI | Production runs 3.12, development runs 3.13; both must pass |
| **Semgrep** with project-specific rules | Blocks committed secrets, private local paths and identity metadata, and `shell=True`/`os.system` in committed Python |
| **Bytecode compile gate** | Catches syntax-level breakage in files no test imports |
| **SPA bundle budget** | Verified against a browser-observed resource list, not just the build manifest — stale chunk hashes are rejected |
| **Branch protection** | Required checks plus `enforce_admins`; no force-push, no branch deletion |
| **Dependency pinning** | Hash-pinned Python requirements; pinned GitHub Action SHAs |

None of these gates care who or what wrote the code. That is the entire point.

## Reviewing what the agents produce

Tests catch regressions in behavior that is already specified. They do not
catch a confidently-written function that solves the wrong problem, and that is
the characteristic AI failure mode. Two practices address it:

**Adversarial verification.** Findings are not trusted from the reviewer that
produced them. In the June 2026 whole-repository audit, every module was read
by a dedicated reviewer and every finding was then re-opened by a *separate*
verifier whose job was to refute it. That pass confirmed 106 findings and
**rejected 9 as false positives** — those 9 are the reason the process is worth
running. A review that never rejects anything is not reviewing.

**Status must cite current source.** When the audit was re-verified in July
2026, a finding could only be marked fixed on positive evidence — a guard that
now exists at a named `file:line`, or a named regression test. "Something nearby
changed" does not count. See
[the remediation ledger](codebase_audit_2026-06-15.md#remediation-status):
43 fixed, 17 partial, 25 still open, stated plainly rather than quietly closed.

## Where this has failed

Three failures worth naming, because they show what the harness does and does
not catch:

**Published numbers drifted from the artifacts that produced them.** The results
table in `forecaster/README.md` cited model MAEs that matched none of the three
JSON artifacts in the repo. Every test passed the entire time — no test asserted
that documentation agreed with data. Caught only by a human cross-check.

**A statistical bug survived a full audit pass.** The A/B harness scores the
`target_temp_next_24h` target per hourly row instead of collapsing to one
observation per calendar day, so its sample size counts autocorrelated readings
as independent and its p-value overstates significance. The daily-high path
does this correctly. The audit found it; it is still open, and the affected
number is now explicitly not quoted as a result.

**Infrastructure detail reached a public commit.** Production host identifiers
were committed to a tracked file and sat in public history for four days before
removal. The response was to add a semgrep rule that fails the build on private
local paths and identity metadata — the class of error, not the instance.

The pattern across all three: automated gates catch what they were told to
check, and the interesting failures are always outside that set. Expanding the
gate set after each miss is the actual work.

## What this is not

This is not an argument that AI review replaces human judgment. Every decision
in this project that mattered — refusing win-rate as a success metric, rejecting
a +8.49% retune as noise, declining to switch predictors because confidence
intervals overlapped, keeping the whole system paper-only until a readiness gate
passes — was a judgment call about what the evidence supports. Agents are
extremely good at generating candidate answers and extremely willing to defend
wrong ones. The harness exists so that willingness costs nothing.

## Related reading

- [Remediation ledger](codebase_audit_2026-06-15.md#remediation-status) — every audit finding with current status and evidence
- [Accuracy evaluation](accuracy_evaluation_2026-07-06.md) — including hypotheses *not* acted on for insufficient evidence
- [Retune validation](trading_retune_validation_2026-06-17.md) — a measured improvement rejected as noise
- [Trade engine overhaul plan](trade_engine_overhaul_plan_2026-06-17.md) — why win-rate was refused as a success metric
