# WeatherEdge Agent Instructions

## Attribution

Keep AI co-author trailers and assistant attribution on commits that carry them.
This project documents its AI-assisted workflow openly rather than concealing it
— see `docs/ai-assisted-development.md`. Do not strip attribution from commit
metadata, contributor lists, PR text, release notes, or generated artifacts.

Treat local assistant state directories and agent lockfiles as disposable
workspace state, not project source.

## Design And Redesign Memory

The public site is the React + HeroUI Pro SPA at the repo root (`src/`,
built with bun + Vite, served from the deployment web root in production).

Required design workflow:

- Use the local skills `frontend-design`, `ui-ux-pro-max`,
  `web-design-guidelines`, and `agent-browser` for substantial UI work.
- The SPA fetches its data JSONs (`trading_signal.json`, `forecast_data.json`,
  `weather_story_data.json`, `strategy_research.json`, `cities_data.json`) at
  runtime; keep `src/lib/data.ts` / `src/lib/strategy.ts` parsing tolerant of
  missing fields.
- The site covers fifteen city markets (Coverage grid fed by
  `cities_data.json`), with SFO presented as the flagship. The site never says
  "Kalshi"; say "prediction market".
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
- `forecaster/cities_data.json`
- `trading/data/`

After sync and refresh, live API/cache/dashboard state is AWS-side, under the
EC2 runtime paths documented in `docs/aws_deployment.md`, and the public
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
