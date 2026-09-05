# Apple WeatherKit Research Source

## Current State

WeatherEdge's Apple WeatherKit REST source was deployed for all fifteen
settlement stations on August 10, 2026. The September 5, 06:20 UTC production
check confirmed that its refresh and independent expiry-purge timers are active.
The cache was absent between scheduled refreshes, consistent with its temporary
lifecycle. New installations remain disabled by default. Apple's current
decision weight is zero: fetching data alone does not change NWP→EMOS forecasts
or paper decisions. The owner's accepted direction is to make Apple eligible
for meaningful influence, with weight determined by evidence; see
[`CODEBASE-REVIEW-DIALOGUE.md`, D001–D002](CODEBASE-REVIEW-DIALOGUE.md).

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

The revised CLI also checks whether each complete lead-one/two Apple target has
a fresh, valid `source='live'` EMOS baseline for the exact station, target date,
and lead. It opens SQLite read-only and reports only pairing counts. Run
`apple_weatherkit.py --probe-only --cities all` to inspect the current cache
without an API request. Missing baselines do not fail a provider refresh; this
check establishes input compatibility, not accuracy or Apple influence.

The revised code was exercised in AWS memory on September 5: 15/15 cities
refreshed, 30 complete targets, 20 compatible baselines. The ten missing matches
were the five Central-time cities immediately after standard midnight, before
the next scheduled baseline update. Existing matches were fresh, and previous
lead-two rows were not silently reused as current lead-one forecasts. These
diagnostics are implemented and tested; the installed scheduled source remains
at the previously deployed revision until the next guarded backend deployment.

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

## Historical Access

Apple does provide historical hourly/daily weather and daily summaries; these
are different from historical averages and from forecasts issued before a past
trading decision. On September 5, 2026, one authenticated REST request returned
24/24 hourly records for SFO's July 19, 2025 fixed-standard settlement window,
with no out-of-window rows. No weather values were retained.

`apple_history_probe.py --city sfo --date 2025-07-19` performs this bounded
capability check in an environment with the existing WeatherKit credentials.
It makes one request, has no retry or date-range loop, and prints only structural
availability. It does not cache data, fit a model, score accuracy, or verify an
original forecast issuance. Apple's coordinate-based past weather must not
replace the official station CLI settlement truth. See the
[historical-access research](research/2026-09-05-weatherkit-history.md) for the
official API sources and remaining evidence boundary.

## License Boundary And Deferred Work

The implementation follows a conservative reading of the current
[Apple Developer Program License Agreement](https://developer.apple.com/support/terms/apple-developer-program-license-agreement/),
Attachment 8: Apple Weather Data must not become a secondary or derived
database, while caching is allowed only on a temporary and limited basis to
improve WeatherKit API performance. Consequently, WeatherEdge does not retain
Apple-only forecast vintages or residuals for historical scoring.

The reviewed WeatherKit terms do not explicitly prohibit all machine learning.
They do restrict the bulk download and lasting training database requested here.
The permitted retention of transformed aggregate scores and learned parameters
still needs clarification; deleting raw values alone does not establish that
every derived dataset is permitted. The existing evidence design therefore
cannot yet justify a learned Apple weight. WeatherKit authentication and endpoint behavior should continue to be
checked against Apple's official
[request-authentication](https://developer.apple.com/documentation/WeatherKitRESTAPI/request-authentication-for-weatherkit-rest-api)
and
[forecast endpoint](https://developer.apple.com/documentation/weatherkitrestapi/get-api-v1-weather-_language_-_latitude_-_longitude_)
documentation before production activation.
