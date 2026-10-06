# Architecture

WeatherEdge connects a weather runtime, an account-scoped paper engine and a
static evidence app through versioned forecast rows and atomic public artifacts.
The [repository map](REPOSITORY_MAP.md) owns file navigation and canonical
entrypoints; this document owns data-flow and correctness boundaries.

## Weather and settlement

The current source registry defines twenty station/market pairs. The deployed
registry is a release property, not inferred from a checkout: the October 5,
2026 audit found fifteen on an older backend. Registry copies in forecaster and
trading are parity-tested because their deployed Python import roots differ.

Each pair defines its market series, official NWS station/CLI product,
coordinates, civil timezone and fixed-standard climate-day window. NWP hourly
values are reduced only over a complete station-day window. Provider daily-max
responses do not expose constituent-hour completeness or initialization; the
new original-serving evidence leaves those fields unavailable. Final CLI truth is
stored by station/date in `cli_settlements`; preliminary reports and observed
high-so-far remain separate. Official truth independently scores forecasts and
resolves paper outcomes.

The shared serve uses eight NWP members and per-station EMOS post-processing.
Training uses earlier settled targets and a version-scoped rolling-origin
archive. Consumers prefer `rolling_origin_v2` within the relevant station/lead
scope, with legacy fallback only where v2 is absent. The live distribution's
mean and spread stay coupled when used to price brackets. Historical backfills,
reconstructed runs and original decision-time vintages have different evidence
strength and must stay labeled.

SFO retains optional legacy blend/residual adapters and offline ML research.
Their historical scores do not describe every serving city or today's method.
Google's orchestrator serves the all-city non-Google baseline first, then uses
budgeted private expiring provider storage. Apple remains a temporary shadow
source with no live weight. Provider-data retention and promotion boundaries
are documented in [data and artifacts](data_and_artifacts.md) and
[WeatherKit](APPLE-WEATHERKIT.md).

## Probability, execution and account identity

The trading adapter supplies station/date forecasts, freshness and same-day
observations. The probability engine conditions feasible settlement outcomes,
then compares weather probabilities with an orderbook-derived market prior.
Candidate sides face exact fee and conservative-edge checks, quote/liquidity
requirements, exposure/concentration limits, loss capacity and account-policy
admission.

Scheduled `portfolio-scan` allocates across cities. Reservation-price maker
limits and configured bounded taker crosses share the admission/risk boundary.
Requested, pending, partially filled and filled quantities are distinct states.
Maker tape evidence is a fill proxy without reconstructed real queue priority.
Exit lots consume shared displayed bid depth; quote time stays distinct from
execution time. Simulation evidence is not a guaranteed real fill.

The SQLite journal retains decision context, orders, lots, exits, settlements
and account identities. Account reconciliation checks the full lifecycle.
Strategy attribution is separate from economic account balances. Live Stability
alone contributes readiness evidence; Research ROI and archives remain
excluded. v7 uses a new paper research ledger while v6 and prior account/policy
histories remain unchanged and their existing positions continue settling.
Real-money order placement stays unimplemented and rejects non-dry orders.

## Publication and release provenance

AWS runtime state is authoritative. `strategy_research.py` is a stable facade
over the `strategy_lab/` builder. Frequent public builds refresh current account
state while historical analysis has its own generation time, source revision
and configuration fingerprint. A fresh public shell or recent publication clock
does not make cached analysis current.

The root React/HeroUI Pro SPA is built to `dist/` and installed separately in the
runtime web root. The publisher overlays five AWS-generated data JSONs and a
validated manifest on those prebuilt assets, then publishes GitHub Pages.
Desktop/mobile behavior, public hashes and freshness gates validate the actual
release. Runtime source, SPA source, analysis source and public artifacts may
have distinct revisions; report each independently.

Full source deployment uses a clean exact Git revision, verified off-host
SQLite backup, captured/quiesced timer policy, installed dependencies/units,
account cutover validation, seeded artifacts and timer/freshness recovery. The
deployment deadman handles abandoned sessions. Compatibility tombstones reject
partial-source sync. See [the deployment runbook](aws_deployment.md) for the
operational sequence; repository edits alone do not release changes.
