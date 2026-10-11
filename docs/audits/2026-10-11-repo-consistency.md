# Repository and operational consistency audit — October 11, 2026

The owner authorized repository and PR cleanup before the next research stage.
This audit separates merged source, installed backend, published artifacts and
private Mac evidence. Real-money allocation remains $0. Additional AWS charges,
hardware/power changes and sustained intensive Mac computation remain forbidden.

## Pull request decisions

| PR | Decision and reason |
|---|---|
| #116 | Close as superseded/deferred. The historical ML report is retained with its dated attribution; V7 has guarded chronological evaluation. Archive the unique WeatherKit probes rather than merge an older unrestricted experiment. |
| #52 | Close after selectively porting its useful per-profile monitor-health correction. Main already contains the evolved EMOS, point-in-time approval, truth and settlement safety fixes. Do not replace those with the old draft. |
| #106 | Integrate the pinned setup-chrome 2.2.0 commit in the consolidated cleanup release; close the original as superseded. Preserve Dependabot authorship and sign-off. |
| #60 | Integrate marked 18.0.6 in the consolidated cleanup release; resolve stale-base conflicts while retaining MapLibre 6.4.1 and all other current pins. Preserve Dependabot authorship and sign-off; close the original as superseded. |
| #104 | Discard this incomplete setuptools major update: it changes the build and input manifests to 84 while leaving the hashed production lock at 83. Keep the currently coherent 83 toolchain. |
| #63 | Discard the isolated HeroUI styles update: React 3.2.1 still requires styles 3.2.1, so it would introduce a second styles version. Review coupled HeroUI updates together. |
| #62 | Discard the isolated underline update: its 3.28 core requirement conflicts with the current 3.27 core. |
| #61 | Discard the isolated React editor update: it requires newer core/pm, with additional optional-menu peer mismatches. |
| #59 | Discard the isolated pm update: the existing core/starter family retains its older exact pm contract. |

Future version updates group `@tiptap/*` and `@heroui/*` by family. Existing
security-alert/update settings are unchanged; no broad ignore rule or
security-alert suppression is introduced. Grouping is a review aid,
not proof that a generated update satisfies every peer contract.

Primary references: [setup-chrome 2.2.0 release](https://github.com/browser-actions/setup-chrome/releases/tag/v2.2.0),
[marked 18.0.6 fixes](https://github.com/markedjs/marked/releases/tag/v18.0.6),
and [Dependabot grouping reference](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference#groups).
Marked's patch includes a backtracking fix; the action release adds Linux arm64
support. Actual retained changes still require the repository's required gates.

## Workspace preservation

All eight other worktrees had clean tracked state, no unique commits relative
to current main, and no active process working directory. Seven contained
ignored trading data. The 165 files (3,348,176 metadata-counted bytes) were moved
without rewriting them into owner-private recovery storage before the obsolete
worktrees were removed. Disposable caches and the old build output were removed
with their obsolete checkout. The current private 32-GiB research/replica state
was preserved.

Twenty-six obsolete local branches were removed. Five unique branch heads are
retained in a verified 217,347-byte incremental Git bundle; seven dependency PR
heads are retained in a second verified 11,277-byte bundle. Original ref names,
commit identities, PR metadata and worktree file metadata are saved privately.
Sixteen unique remote branch heads, including older squash-merged/closed work,
are also retained in a verified 170,028-byte incremental bundle before remote
branch cleanup. The twelve other obsolete remote heads are ancestors of main.
The bundles require the retained pre-cleanup main history, which remains in
main. This preserves recoverable source and attribution without leaving stale
branches or duplicate checkouts active. Private recovery locations and restore
instructions belong in operator state, not tracked access documentation.

## Narrow correctness and reliability work

The monitor-health regression was reproduced without production data: twelve
newer research-profile actions evict a healthy live-profile action from the
global public activity list. Deriving health from that list falsely reports
`monitor-not-recording`. The correction derives per-profile health from existing
current per-order inspection rows before display caps, while retaining the
twelve-action public limit and existing account isolation and valuation rules.
It changes neither order admission nor real-money behavior.

Two post-October-6 Mac failures occurred after output creation: October 9 at
13:20 PDT and October 10 at 09:36 PDT. Their pacing signal raised permission
denied; cleanup then failed fatally. The exact OS cause remains unproven.
Sixteen short native child fixtures took 0.696 seconds and reproduced eight
already-handled exited/reapable permission errors, with no survivors. This
does not reproduce the later alive-or-not-yet-reapable case. Diagnostics must
retain both primary and cleanup tracebacks and the exact attempted signal,
phase and reaping state. Live-child signaling errors remain fatal; diagnostics
must not be described as a proven reliability repair.

The idle local schedule was unloaded at 11:06:55 PDT for source maintenance.
Its plist and enabled preference were preserved. It was restored at
**11:18:38 PDT**, loaded and idle with no immediate run. Code was frozen at
reviewed `49606cd88`; only the README copy correction was dirty at restoration.
The installed scheduled checks-only path correctly deferred for competing
load, without changing run-folder count or last-success evidence. The original
two-hour interval, Background/nice/low-I/O settings, job-scoped caffeinate,
request ceilings and resource/admission limits are unchanged. No full
weather/ML batch bypassed native AC/load admission to manufacture success.

## Dated operational evidence

- **AWS, 11:01 PDT:** clean backend `2a6432e3`, exec-v4, V6 research, live off,
  dry-run on, fourteen active application timers, zero failed units,
  archive-only pruning. Paper database 34.55 GiB, free space 16.71 GiB,
  canonical backup short **18.84 GiB**. All non-paper allocation combined is
  smaller than the gap, with no spare disk. Routine cleanup cannot satisfy the
  gate while preserving research. No cloud mutation or paid billing query.
- **Public, 10:58 PDT:** stable snapshot `5a18dd005895a4dbc33b5b75`, all five
  runtime hashes match, three static assets match the reviewed local build;
  fifteen cities, V6, both live flags false, September 5 analysis explicitly
  stale. The hybrid summary remains the explicitly dated October 5, 23:50 PDT
  batch, not current private evidence. No backend release occurred.
- **Mac, latest complete analysis October 10, 15:41 PDT:** 489 original issued
  distributions, 3,912 members, fifteen stations; 151 scored identity records
  reduce to 139 unique station/lead/date outcomes over five dates (59 lead-zero,
  47 day-ahead, 33 lead-two). Single-model cohort: 137 records, 46 day-ahead.
  Missing initialization/hour/training-availability receipts still block
  promotion. Historical ML challengers remain negative and unpromoted.
- **Schedule:** 42 complete post-repair folders, two failures, one retained
  deferral; pre-admission deferrals are not fully journaled. Every complete
  folder has all five hashes. Latest success used 111.516 wall/53.84 observed
  CPU seconds and about 380 MiB. October 11, 09:42 PDT correctly deferred for
  AC power; fresh admission also rejected competing load. No full job forced.

## Remaining work before activation

1. Observe the improved signaling diagnostics during admitted scheduled work;
   the underlying later permission-error cause is still open.
2. Onboard the five cities with zero seeded NWP training rows using bounded,
   provenance-preserving collection and provider-equivalent quota accounting.
   Retained live raw responses alone do not warm the current training archive.
3. Recover four older final-truth gaps, preserving preliminary/final and
   revision provenance rather than inventing outcomes.
4. Design and validate independent durable recovery inside the existing cost
   limit, including transfer allowance, before backend V7 cutover or moving
   production analysis to the Mac. The old Mac paper replica is insufficient.
5. Gather prospective V7 net-fill evidence after verified cutover. October 31
   is an evidence review date; no optimal bankroll or profitability is proven.

## Final validation and disposition

Root's combined affected gate passed **224 tests and 74 subtests** in 5.61
seconds with one numerical thread and low process priority. Independent monitor
validation passed 90 Strategy tests and 108 related tests. Independent guard
validation passed 40 tests and 74 subtests, including fatal cleanup behavior;
additional small interruption fixtures retained cancellation semantics. The
project health gate and diff check passed; the health gate's local-runtime
warning is expected and is not production evidence.

Web validation passed **218 tests in 45 files** with a single worker, lint with
the same two existing Fast Refresh warnings, and the production build. The
frozen install changed only marked; all 546 installed licensed HeroUI artifact
hashes stayed unchanged. A final complete web rerun after the README correction
passed all 218 tests in 24.96 seconds. Served candidate checks at desktop
1440×900 and mobile 390×844 verified explicit AWS-authority placeholders,
withheld balances/profiles, and working mobile navigation. Separate ordinary
public-app checks at those viewports and 320 CSS-pixel width verified V6,
live $0, dated hybrid numbers and click/Space/Enter disclosure behavior with
correct `aria-expanded`. Both sets had zero page errors and no horizontal
overflow. These are actual browser viewport checks, not a physical-phone test;
the public observation concerns the existing deployment, not the newly built
dependency candidate. Screenshots, DOM results and logs remain private. The
browser session and preview server were stopped.

All nine original open PRs are closed with the dispositions above. Their useful
changes are retained in [the consolidated cleanup PR #133](https://github.com/Jaxsonb04/weather_edge/pull/133).
Twenty-one obsolete remote branches were removed with exact-head leases;
seven closed dependency branches were already absent. Only main, gh-pages and
the active cleanup branch remained before landing. All eight obsolete
worktrees and twenty-six obsolete local branches were removed with the
preservation described above.

The first CI candidate caught a README coverage-copy assertion after the
documentation changed during local web validation (217/218 passed). The README
was corrected to retain the expected `20 city markets,` wording without
weakening the test; its five focused copy tests then passed. Required gates
must pass on the corrected final head before merge; no administrative bypass
is permitted.

Both Python 3.12 and 3.13 gates on candidate `49606cd88` passed **3,750 tests,
twelve skips and 74 subtests**; required Semgrep reported **zero findings,
zero blocking**. The corrected final candidate must rerun all three required
gates. The final head, checks and merge disposition are retained in the linked
consolidated PR; the sole working checkout is to return to clean main only
after those checks pass. Source changes in this audit do not constitute an
AWS backend deployment or replace public runtime authority.
