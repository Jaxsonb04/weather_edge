# Apple WeatherKit Source

## Current State

Owner decision, 2026-11-09 (personal paper-trading project; the owner accepts
the license gray area described below): Apple WeatherKit nudges the live
forecast with a **fixed, capped weight**, and every nudge is recorded so it can
be scored. It is off until `ENABLE_APPLE_WEATHER=1` and the credentials below
are set on the server.

How it works (`forecaster/emos_forecast.py`):

- Each live EMOS serve fetches Apple in memory (`apple_weatherkit.live_highs`),
  one call per city, 5 s timeout, at most 60 s of Apple calls per run. A failed
  or slow city simply serves without Apple.
- Only next-day and later targets take Apple. Same-day targets never do: they
  belong to the intraday path, and Apple's current day is incomplete anyway.
- The rule (`apple_weatherkit.apple_nudge_f`): move the EMOS mean 50% toward
  Apple's station-day high, never more than 3.5°F, and ignore Apple when it
  disagrees by 7°F or more. Sigma is unchanged. 50% was the owner's choice; it
  makes Apple count as much as all the NWP models combined.
- The nudged mean is what trading and the public JSON see. The serve's
  `forecast_emos_live_vintages.serve_policy_json` records `apple_adjustment_f`
  and `apple_action`, so the baseline is always `predicted_high_f - apple_adjustment_f`.
- `python emos_forecast.py --apple-score` prints, per lead, settled MAE with
  and without Apple on the same vintages. Raw Apple highs are never stored.

The weight is a guess, not a fit. Read `--apple-score` after a few weeks of
settled days; if "with" is not clearly below "without", set
`ENABLE_APPLE_WEATHER=0`.

## Runtime Contract

`forecaster/apple_weatherkit.py`:

- requests `forecastHourly,forecastDaily` together at the exact coordinates in
  the shared city registry;
- generates Apple's required ES256 bearer token in memory from a private
  mode-0600 `.p8` key and refuses HTTP redirects;
- derives the market-day high from 24 unique hourly forecasts inside the exact
  fixed-standard settlement window;
- treats Apple's daily maximum only as a transient diagnostic;
- isolates a failed city from the other nineteen;
- writes only complete, normalized current highs to a private mode-0600 file
  under `/run/weatheredge`;
- makes each value unavailable at the earliest relevant
  `metadata.expireTime`, removes it immediately on the next read or refresh,
  and has an independent purge that bounds unattended physical removal to the
  next ten-minute cycle (plus up to 30 seconds of jitter); and
- emits counts and status only, never Apple temperatures, tokens, response
  bodies, credential identifiers, or key paths.

The source never writes raw Apple values to `weather.db`,
`nwp_model_forecasts`, forecast archives, paper-decision snapshots, model
training data, or public JSON. The only Apple-derived values stored are the
nudged live mean and its `apple_adjustment_f` (see Current State). Inserting Apple rows into
`nwp_model_forecasts` would silently alter EMOS fitting and is prohibited by
the release boundary.

Only complete future settlement days are cached. A refresh after local
fixed-standard midnight cannot reconstruct the already elapsed forecast hours,
so the current settlement day is intentionally omitted rather than mislabeled
as a complete Apple daily high. Any future same-day consumer must combine the
remaining Apple hours with WeatherEdge's authoritative NWS high-so-far in
memory; it must not backfill or archive missing Apple hours.

## Cadence And Cost Boundary

The retired `weatheredge-apple-refresh.timer` defines four fixed UTC vintages
per day: 02:17, 08:17, 14:17, and 20:17 UTC, with up to 60 seconds of jitter. One
bundled request per city per vintage for twenty cities would be 80 calls/day, or at most
2,480 calls in a 31-day month if re-enabled. A dedicated ten-minute Apple purge
is an independent, alertable expiry safety net; it cannot be blocked by a Google
purge failure.

The refresh timer is **no longer part of the canonical unit inventory**. It is **retired as of the 2026-09-03 audit (FC-4)**: its runtime cache had no reader anywhere outside `apple_weatherkit.py`, and Apple's terms do not permit retaining an archive, so it could not become a scored EMOS member either. The unit files are still installed and integrity-checked, so an operator can re-enable it with `sudo systemctl enable --now weatheredge-apple-refresh.timer`, but no deploy path turns it on.
`weatheredge-apple-purge.timer` stays canonical and enabled: it makes no request
and it is what guarantees any residual Apple runtime content still expires.

The command still exits safely without a request while `ENABLE_APPLE_WEATHER=0`,
and if the flag is enabled with incomplete credentials the service fails visibly
instead of reporting a false success.

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

The 2026-11-09 fixed weight stores the Apple-derived nudge per served forecast,
which this conservative reading treats as an inferred residual. The owner chose
to accept that for a personal, non-commercial paper project. Raw Apple highs and
the runtime cache rules above are unchanged. WeatherKit authentication and endpoint behavior should continue to be
checked against Apple's official
[request-authentication](https://developer.apple.com/documentation/WeatherKitRESTAPI/request-authentication-for-weatherkit-rest-api)
and
[forecast endpoint](https://developer.apple.com/documentation/weatherkitrestapi/get-api-v1-weather-_language_-_latitude_-_longitude_)
documentation before production activation.
