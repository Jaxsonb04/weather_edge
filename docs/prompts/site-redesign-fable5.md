# WeatherEdge site redesign — multi-city reframe + Strategy Lab overhaul

> Safety revision (2026-07-11): this reusable prompt intentionally differs from
> its original archived copy so delivery cannot bypass branch review or operator
> control.

You are redesigning the public website of **WeatherEdge**, a solo quant's paper-trading
research project. This is a portfolio piece: recruiters and technical peers look at it to
judge whether the person can build a real forecasting-and-trading system and present it
honestly. The site is live at **https://jaxsonb04.github.io/weather_edge/** and is a
React 19 + Vite + Tailwind v4 + **HeroUI Pro** single-page app.

## Why this matters (read before scoping)

The backend was just rebuilt from a single market (San Francisco daily-high temperature)
into **fifteen US city daily-high markets**, and the execution strategy was re-oriented to
**Maker-side resting limit orders concentrated on the high-probability "favorite" band**.
The site has not kept up. It still reads as a one-city project with a fifteen-city grid
bolted on, and its most important surface — the Strategy Lab, where the actual
paper-trading evidence lives — is an unnavigable wall. The system is now genuinely more
impressive than the site makes it look. Your job is to close that gap: make the site
present the fifteen-city, two-profile, Maker-first reality clearly, honestly, and with
design quality that reads as intentional rather than templated.

Read `CLAUDE.md` for repository context, and read the WeatherEdge memory
files (especially `weatheredge-multicity-2026-07-06`, `weatheredge-ui-heroui-pro`, and
`weatheredge-recovery-2026-07-06`) for accumulated context and hard-won gotchas before you
touch anything. Then verify the current state with your own eyes — do not take the flaw
list below on faith; the site may have shifted, and fresh observation is part of the job.

## The outcome you own

When you are done, all of the following are true and you have verified each one in a real
browser at desktop and mobile sizes (not just a successful build):

- The whole site is framed around fifteen cities, not San Francisco with a grid attached.
  A first-time visitor understands within the first screen that this forecasts and trades
  fifteen markets, and can drill into any one of them.
- There are exactly **three** primary navigation tabs: **Overview**, **Methodology**,
  **Strategy Lab**. No more, no fewer. (They already exist as hash routes in
  `src/lib/useHashRoute.ts` — keep exactly these three.)
- The **Strategy Lab** is navigable and gives a full, honest analysis of what **both**
  paper-trading profiles (`live` and `research`) are doing: one clear overview of the
  entire book, then complete per-profile diagnostics and statistics for each profile,
  reachable without an 8,000-pixel scroll or a buried toggle.
- **Methodology** explains the actual production forecasting pipeline that runs for all
  fifteen cities, not only San Francisco's flagship extras.
- The site is fresh, coherent, responsive (no horizontal overflow at any breakpoint), and
  truthful about data status. It never uses the word "Kalshi" — always "prediction
  market(s)". It is built with HeroUI Pro components used correctly (props verified via the
  MCP, not guessed) and reflects the HeroUI design-taste principles.
- It is deployment-ready and verified locally; production rollout remains an
  operator-controlled follow-up.

## Safe delivery workflow

- Start from an up-to-date `codex/` feature branch or isolated worktree. Never
  work directly on `main`.
- Run the full test suite, production build, and real browser verification at
  the desktop/mobile widths below, preserving screenshots and DOM evidence.
- Request an independent review of the final diff, address blocking feedback,
  and open a pull request. Do not merge or push directly to `main`.
- Merging, pushing `main`, and any production deploy require explicit operator approval.

## Current state — verify this yourself, then improve it

These are the flaws as last observed (2026-07-07). Re-check them; treat them as leads.

**Overview (`#/overview`) still reads single-city.** The hero headline already says
"fifteen cities," but every operational surface beneath it is San-Francisco-only: the
"today's call" forecast pipeline, the "where the engine sees a mispricing" model-vs-market
chart, and the "every active bracket · Today" market book all show SFO and only SFO. The
fifteen-city coverage grid appears *below* these as a mid-page section — a bolt-on, not the
frame. The result: the page promises fifteen cities and then shows one.

**Methodology (`#/methodology`) is entirely about San Francisco.** It explains the SFO LSTM
model and "ten years of KSFO." It says nothing about the station-agnostic multi-model
NWP→EMOS pipeline that is the *actual* production forecaster for fourteen of the fifteen
cities. A reader would wrongly conclude the other cities have no real forecasting method.

**Strategy Lab (`#/lab`) is the worst offender.** It is an ~8,500px desktop / ~14,000px
mobile single scroll, split into two confusing sub-tabs ("Lab overview" / "Trading desk")
holding eleven numbered sections between them. The two-profile comparison — the single most
important thing in the Lab — is buried as one section with a segmented **toggle that shows
one profile at a time**, so you cannot see `live` and `research` together, and every deeper
diagnostic below it (the gate funnel, calibration, readiness, ops health, backtest) is shown
**combined across both profiles**, never broken out per profile. It is thorough but
unusable, and it does not actually deliver a per-profile analysis.

**The underlying data is fine; the presentation wastes it.** `strategy_research.json`
already carries a `profiles[]` array with per-profile daily summaries, signal-quality charts,
paper-trading summaries, learnings, recommended changes, and status, plus
`gate_behavior.by_profile`. The two profiles are real and active (`live` ≈ 11 closed
positions, `research` ≈ 75). The Lab just doesn't surface this per-profile structure. Note
also that some copy in that JSON still describes an SFO-only blend — the site should present
truthfully what the data says without amplifying stale single-city framing.

## The three workstreams

### 1. Strategy Lab overhaul — this is the priority

Replace the two-sub-tab, eleven-section scroll with a **profile-centric** structure. My
recommended shape (adapt if you find better, but keep the intent):

- **A "Book Overview" at the top** — the whole system at a glance, before any drill-down
  (general-to-specific hierarchy): combined headline KPIs, the equity curve, the go-live
  readiness verdict, a compact gate-funnel summary, and — critically — the **`live` vs
  `research` comparison shown side by side**, not as a toggle. A visitor should see both
  books' size, activity, hit rate, and P&L next to each other immediately.
- **A profile selector** (a segmented control: `Live` | `Research`) that drives a full
  per-profile dashboard: that profile's KPIs, its own gate funnel and top rejection reasons,
  its signal-quality charts, its calibration, its open positions / ledger / monitor log
  filtered to that profile, and its learnings and recommended changes. This is where "all
  the relevant diagnostics and statistics for both profiles" actually lives — each profile
  gets the full treatment, one at a time, cleanly.
- Keep the honest empty states (a book with no open positions is a real, informative state —
  say so, don't hide it).

Prefer surfacing per-profile data that already exists in `profiles[]` and
`gate_behavior.by_profile`. Where a diagnostic the user wants per-profile is currently only
computed combined (e.g. calibration or backtest rollups), you may extend the backend
generator — `trading/sfo_kalshi_quant/strategy_research.py` on the remote Mac — to emit it
per profile, then render it. That backend change is in scope and legitimate; do it only for
diagnostics that genuinely need per-profile fidelity, and keep the JSON parsing on the
client tolerant of the fields being absent (older published artifacts won't have them).

Add a lightweight **multi-city lens** where it's cheap: the ledger and open positions carry
market tickers, and `src/lib/data.ts` already exports `cityForTicker(ticker)`. Use it to add
a city dimension (a "by city" grouping or filter on the ledger, and a city column already
present — make it first-class). Deeper per-city gate/calibration statistics would require a
per-city backend rollup; treat that as optional and out of scope unless you judge it worth
doing.

### 2. Overview — make fifteen cities the frame

Reframe so the fifteen cities *are* the operational surface, not a section below it. The
strongest move is a **city selector** on the "today's call" / mispricing / market-book
surfaces so the engine's per-market work renders for **any** chosen city, defaulting to a
sensible flagship (San Francisco has the richest pipeline, so it's a reasonable default, but
the selector must make clear this is one of fifteen). The existing fifteen-city coverage grid
becomes the navigator into that per-city detail rather than a mid-page afterthought. The
data is in `cities_data.json` (per-city forecasts, latest settlement, book activity,
freshness) and the per-city forecast/market surfaces already exist for SFO — generalize them.
Keep San Francisco's flagship status visible (it alone has the full Google/LSTM/marine-layer
blend) without letting it dominate the framing.

### 3. Methodology — tell the two-tier forecasting story

Add the pipeline that actually runs everywhere: a nine-model NWP ensemble (Open-Meteo
previous-runs, leakage-free) post-processed per city with rolling-origin EMOS, settled
against each city's own NWS Climatological Report. Present San Francisco's LSTM, Google
Weather blend, and marine-layer features as **flagship-only extras layered on top**, not as
the whole method. Keep the existing SFO model-proof content; it's good — just stop letting it
stand in for the entire forecasting story. Be truthful about what is validated where (the
multi-city EMOS record is backtest-grade with limited live history; don't overclaim).

## Data contracts you'll use

- `strategy_research.json` — the Lab's data. Key: `profiles[]` (per entry: `risk_profile`,
  `profile_type`, `daily_summary`, `signal_quality.charts`, `paper_trading.summary`,
  `learnings`, `recommended_changes`, `status`), `daily_summary` (combined; has
  `gate_behavior.by_profile`, `exit_reasons_by_profile`, `biggest_winners/losers`,
  `model_vs_market`), `real_money_readiness`, `calibration_comparison`, `backtest_summary`.
  Types are in `src/lib/strategy.ts`.
- `cities_data.json` — per-city forecasts, latest settlement, book activity, freshness.
  Types + `useCitiesData()` + `cityForTicker()` are in `src/lib/data.ts`.
- `forecast_data.json`, `trading_signal.json`, `weather_story_data.json` — the SFO-flagship
  forecast/decision/market surfaces. Fetched relative to `BASE_URL` by hooks in `data.ts`.
- All JSON parsing must stay tolerant of missing/renamed fields (optional chaining,
  fallbacks) — the published artifact is regenerated by the runtime and schemas drift.

## Use HeroUI Pro correctly

- The `heroui-pro` MCP server covers both `@heroui/react` (OSS) and `@heroui-pro/react`
  (Pro). **Before using any component, look up its real API** with the MCP:
  `list_components`, then `get_component_docs` / `get_css` / `get_theme_variables` /
  `get_component_source_code`. Never guess component names, props, or compound-component
  structure.
- Invoke the `heroui-react-pro` and `heroui-pro-design-taste` skills and apply them. The
  design-taste profile is 78 principles; the ones that bite hardest here:
  - **Parallel views use segmented controls/tabs, not separator-divided sections or a
    single toggle that hides the other option.** (This is exactly the Strategy Lab fix.)
  - **Summary/overview content goes at the top; detail and drill-down below** — general to
    specific.
  - Semantic tokens over raw colors (`variant="primary"`, surface tokens for hierarchy);
    never nest a surface inside a surface; minimize borders to those that carry structural
    meaning.
  - `tabular-nums` on every numeric display (P&L, stats, temperatures, counts).
  - Generous, consistent whitespace on the 4/8px grid; tight vertical sizing (no section
    taller than its content); constrained page max-width.
  - Avoid duplicate representations of the same datum; one clear representation each.
  - Icon-only buttons get Tooltips; keep the operational-instrument aesthetic (this is a
    quant tool, not a marketing landing page).
- The existing app already uses Pro `Navbar, Command, Segment, KPI/KPIGroup, Widget,
  AreaChart/BarChart/LineChart, DataGrid, Sheet, TrendChip`, etc. Reuse these patterns;
  match the established dark "operational instrument" direction (warm-gold `--accent`, the
  de-rainbowed cool→hot thermal ramp via `tempColor()`/`.temp-text`, indigo model-vs-market
  series). Green/red is reserved for P&L by trading convention.

## Build and verify locally; prepare an operator handoff

- **The build runs on the remote Mac, never in GitHub Actions** (the HeroUI Pro CI token is
  rejected). `bun` is at `~/.bun/bin/bun`. Build with `bun run build`; the repo is at
  `~/develop/WeatherEdge`. Your local file tools see the laptop, not the Mac — edit through
  the remote as the memory describes.
- `trading/deploy/aws/deploy_web_app.sh` is the operator-owned production path.
  Do not run it from this task. Include the reviewed commit, test/browser
  evidence, and rollback notes in the handoff so an operator can approve or
  reject deployment separately.
- **Entrance animations are CSS-driven via `src/components/ui/Reveal.tsx`, NOT
  framer-motion** — framer pauses in backgrounded/headless tabs and leaves content stuck
  invisible. Keep that pattern.
- **Verify in a real browser, not the preview tab** (the preview tab is `document.hidden`
  and only paints the initial viewport). Use the Playwright-on-Mac recipe in the memory:
  `playwright-core` + the cached Chromium at `~/Library/Caches/ms-playwright/`, neutralize
  `.reveal,.pop { opacity:1; transform:none }` before screenshotting, and read DOM state
  back (KPI numbers are NumberFlow shadow-DOM — trust DOM probes over naive innerText).
  Check **320, 375, 768, 1024, 1440** widths and confirm no horizontal overflow at any of
  them; verify both the Strategy Lab profile selector and the Overview city selector
  actually switch content by driving the page and reading the result.
- If `bun install` reverts `@heroui-pro/react` to a 431-byte stub, restore
  `node_modules/@heroui-pro/react` from `/tmp/we_pro_backup` on the Mac.

## Boundaries

- Paper-trading research only. Never present or imply live real-money trading. Keep the
  existing "paper only / no live orders" disclaimer visible in the Lab.
- Do not weaken truthful data-status labeling to make the site look healthier. If a city's
  forecast is stale or a book is empty, say so.
- Never write "Kalshi" in user-visible copy — "prediction market(s)".
- Keep exactly three top-level tabs. Do not add a fourth view.
- Don't break the deploy pipeline, the single-writer gh-pages publish, or the runtime JSON
  contract other than the deliberate, tolerant per-profile additions described above.
- Don't over-build: no speculative abstractions, no framework for hypothetical future
  cities, no refactor of code the task doesn't touch. Keep components focused (aim under
  ~300 lines; hard cap 800). Do the simplest thing that reads as intentional.
- Do not rewrite public git history or force-push. Commit only to the feature
  branch/worktree. Submit a reviewed pull request; do not merge, push `main`, or
  run production deployment scripts without explicit operator approval.

## How to operate

You are working autonomously; the user is not watching in real time, so don't stop to ask
"want me to…?" for reversible work that follows from this brief — proceed, and pause only
for a genuinely irreversible action or a real scope change. When you have enough to act,
act; give a recommendation rather than surveying options you won't pursue.

Delegate independent work to subagents and keep working while they run — per-view redesign,
the browser verification pass, and a design-quality review are natural parallel tasks.
Prefer a **fresh-context verifier subagent** over self-review for checking the finished site
against this brief. Keep a short Markdown notes file for the run (one lesson per entry:
what worked, what failed, why) and read it back before major decisions.

Before reporting progress, audit every claim against an actual result — a build output, a
DOM probe, or a screenshot. If something is unverified, say so. Local build success is not
production proof; label production verification as pending unless an operator separately
approved and performed the rollout.

## Definition of done (verify each; this is the bar, not a suggestion)

1. Exactly three tabs (Overview, Methodology, Strategy Lab); no fourth view; nav works on
   desktop and mobile.
2. Overview frames fifteen cities and lets you drill into any one city's forecast + market
   surfaces (city selector or equivalent), verified by driving the selector in a real
   browser.
3. Methodology explains the multi-city NWP→EMOS pipeline as the production method, with SFO
   extras as flagship add-ons.
4. Strategy Lab: a top Book Overview with `live` vs `research` **side by side**, then a
   profile selector giving each profile its full per-profile diagnostics/statistics; the
   old 8,000px-single-scroll / buried-toggle structure is gone; verified by switching
   profiles in a real browser and confirming content changes.
5. No horizontal overflow at 320/375/768/1024/1440; no console errors on any view; the word
   "Kalshi" appears nowhere in the shipped bundle.
6. HeroUI Pro components used with MCP-verified APIs; design-taste principles visibly
   applied (side-by-side comparison, summary-first hierarchy, tabular numerals, surface
   hierarchy, restrained borders, operational-instrument aesthetic).
7. Built and fully tested on the Mac, committed to a feature branch, independently
   reviewed, and submitted as a pull request with desktop/mobile evidence. Report the
   candidate commit and explicitly state that merge and production deploy await operator
   approval.
