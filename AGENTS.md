# WeatherEdge Agent Instructions

## Attribution Hygiene

Do not add AI-assistant signatures, generated-by notices, model/vendor
attribution, co-author trailers, or assistant identities to committed files,
contributors, release notes, PR text, generated artifacts, or metadata unless
the user explicitly asks for that attribution.

Keep authorship and contributor metadata human-owned or project-bot-owned. Treat
local assistant state directories and agent lockfiles as disposable workspace
state, not project source.

## Design And Redesign Memory

The public site is the React + HeroUI Pro SPA at the repo root (`src/`,
built with bun + Vite, served from `/opt/weatheredge/webdist` in production).

Required design workflow:

- Use the local skills `frontend-design`, `ui-ux-pro-max`,
  `web-design-guidelines`, and `agent-browser` for substantial UI work.
- The SPA fetches its data JSONs (`trading_signal.json`, `forecast_data.json`,
  `weather_story_data.json`, `strategy_research.json`) at runtime; keep
  `src/lib/data.ts` / `src/lib/strategy.ts` parsing tolerant of missing fields.
- Keep the WeatherEdge visual direction: an operational meteorological
  instrument for a student quant weather project, not a marketing landing page.
- Verify desktop and real mobile layouts with browser screenshots, then verify
  behavior by driving the page and reading DOM state back.

Do not finish a frontend change with only static inspection. Run `bun run
build`, serve `dist/`, and use browser automation when the page is meant to be
viewed or interacted with.

## Runtime Data Authority

The local MacBook may contain stale ignored runtime artifacts. Treat these as
disposable unless you just regenerated them in the current task:

- `forecaster/weather.db`
- `forecaster/google_weather_cache.json`
- `forecaster/trading_signal.json`
- `forecaster/strategy_research.json`
- `trading/data/`

After sync and refresh, live API/cache/dashboard state is AWS-side, under the
Lightsail runtime paths documented in `docs/aws_lightsail.md`, and the public
dashboard is published from AWS-generated artifacts. Do not diagnose production
data problems from stale local ignored files.

Before local dashboard design verification, clear stale runtime state from the
repository root:

```bash
python3 scripts/clear_local_runtime_state.py --confirm
```

The cleanup writes explicit local placeholder JSON for the Google cache, trading
signal, and Strategy Lab research artifact saying the live data belongs on AWS
after sync. For frontend checks, build and serve the SPA:

```bash
bun run build   # or `bun run dev` for a live dev server
```

## Conversation Queue

When the user prefixes a message with `Queue:`, `Parking lot:`, or `Later:`,
treat it as saved context only. Acknowledge it briefly, then continue the
active thread without steering toward the queued item.

Only switch to queued text when the user explicitly says `Switch to queue`,
`Use the queued item`, or similar.
