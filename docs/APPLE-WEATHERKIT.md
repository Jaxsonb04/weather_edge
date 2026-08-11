# Apple WeatherKit Research Source

## Current State

WeatherEdge has a credential-ready Apple WeatherKit REST source for all fifteen
settlement stations. It is disabled by default, has **zero live trading
weight**, and has not been deployed to production as of 2026-08-10. Enabling
the source fetches data; it does not change the NWP→EMOS forecast, calibrated
market probabilities, risk gates, sizing, or paper decisions.

This separation is deliberate. A new provider is a hypothesis, not an edge.
The trading decision remains the calibrated WeatherEdge probability versus the
market price after fees, uncertainty, liquidity, and risk gates. Apple can only
be promoted after it demonstrates incremental out-of-sample value rather than
duplicating information already present in the NWP ensemble.

## Runtime Contract

`forecaster/apple_weatherkit.py`:

- requests `forecastHourly,forecastDaily` together at the exact coordinates in
  the shared city registry;
- generates Apple's required ES256 bearer token in memory from a private
  mode-0600 `.p8` key and refuses HTTP redirects;
- derives the market-day high from 24 unique hourly forecasts inside the exact
  fixed-standard settlement window;
- treats Apple's daily maximum only as a transient diagnostic;
- isolates a failed city from the other fourteen;
- writes only complete, normalized current highs to a private mode-0600 file
  under `/run/weatheredge`;
- makes each value unavailable at the earliest relevant
  `metadata.expireTime`, removes it immediately on the next read or refresh,
  and has an independent purge that bounds unattended physical removal to the
  next ten-minute cycle (plus up to 30 seconds of jitter); and
- emits counts and status only, never Apple temperatures, tokens, response
  bodies, credential identifiers, or key paths.

The source never writes Apple values to `weather.db`,
`nwp_model_forecasts`, forecast archives, paper-decision snapshots, model
training data, or public JSON. In particular, inserting Apple rows into
`nwp_model_forecasts` would silently alter EMOS fitting and is prohibited by
the release boundary.

Only complete future settlement days are cached. A refresh after local
fixed-standard midnight cannot reconstruct the already elapsed forecast hours,
so the current settlement day is intentionally omitted rather than mislabeled
as a complete Apple daily high. Any future same-day consumer must combine the
remaining Apple hours with WeatherEdge's authoritative NWS high-so-far in
memory; it must not backfill or archive missing Apple hours.

## Cadence And Cost Boundary

`weatheredge-apple-refresh.timer` schedules four fixed UTC vintages per day:
02:17, 08:17, 14:17, and 20:17 UTC, with up to 60 seconds of jitter. One bundled
request per city per vintage is 60 scheduled calls/day, or at most 1,860 calls
in a 31-day month. A dedicated ten-minute Apple purge is an independent,
alertable expiry safety net; it cannot be blocked by a Google purge failure.

The timer is part of the canonical unit inventory but the command exits safely
without a request while `ENABLE_APPLE_WEATHER=0`. If the flag is enabled with
incomplete credentials, the service fails visibly instead of reporting a false
success.

## Activation Inputs

The server environment needs these operator-supplied values:

- `ENABLE_APPLE_WEATHER`
- `APPLE_WEATHER_TEAM_ID`
- `APPLE_WEATHER_SERVICE_ID`
- `APPLE_WEATHER_KEY_ID`
- `APPLE_WEATHER_PRIVATE_KEY_PATH`
- `APPLE_WEATHER_TOKEN_TTL_SECONDS`
- `APPLE_WEATHER_HTTP_TIMEOUT_SECONDS`
- `APPLE_WEATHER_RUNTIME_CACHE_PATH`

Apple Developer Program membership alone is not the complete REST setup: the
account must also have a WeatherKit Service ID and WeatherKit private key. Keep
the `.p8` outside the source tree, owned by the service account, and mode 0600.
The repository ignores and deploy-excludes all `.p8` files.
The cache-path setting is not a general storage override: the CLI requires the
exact canonical Apple filename directly under the transient runtime root so a
misconfiguration can never purge another provider's store.

## License Boundary And Deferred Work

The implementation follows a conservative reading of the current
[Apple Developer Program License Agreement](https://developer.apple.com/support/terms/apple-developer-program-license-agreement/),
Attachment 8: Apple Weather Data must not become a secondary or derived
database, while caching is allowed only on a temporary and limited basis to
improve WeatherKit API performance. Consequently, WeatherEdge does not retain
Apple-only forecast vintages or residuals for historical scoring.

That prevents an honest long-horizon promotion study today. Durable Apple
forecast evidence, Apple-specific error histories, inferred residuals, and any
nonzero model/trading weight remain deliberately deferred until Apple gives
written clarification or qualified counsel approves a compliant evidence
design. WeatherKit authentication and endpoint behavior should continue to be
checked against Apple's official
[request-authentication](https://developer.apple.com/documentation/WeatherKitRESTAPI/request-authentication-for-weatherkit-rest-api)
and
[forecast endpoint](https://developer.apple.com/documentation/weatherkitrestapi/get-api-v1-weather-_language_-_latitude_-_longitude_)
documentation before production activation.
