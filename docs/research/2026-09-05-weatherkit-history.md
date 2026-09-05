# WeatherKit historical access and forecasting research

Verified 2026-09-05. This memo separates documented capabilities, one actual
capability check, and unresolved retention permissions. No accuracy improvement
or trading benefit has been measured from Apple data.

## What Apple provides

| Product | Verified capability | Meaning for WeatherEdge |
| --- | --- | --- |
| Hourly/daily weather queries | Apple's native documentation supports historical dates from **2021-08-01**, at most **10 days per request**, with inclusive start and exclusive end. Daily inclusion follows local midnight. REST v1 exposes corresponding start/end and timezone parameters. | Historical conditions can be requested; they do not supply a documented original forecast-issuance selector. |
| Daily Summary | Apple's official REST example uses `/api/v2/summary/daily/{latitude}/{longitude}` with `dataSets`, `start`, and `end`. Summaries include past high/low temperature and precipitation/snow. The native interval is capped at one year. | Date-specific summaries are separate from climatological averages. They are not automatically official station settlement observations. |
| Historical statistics/comparisons | Hourly, daily, and monthly averages use records beginning in 1970; comparisons describe deviations from averages. | This is climate context, not a downloadable series of individual forecasts from 1970. |

Sources: [hourly query](https://developer.apple.com/documentation/weatherkit/weatherquery/hourly(startdate:enddate:)),
[daily query](https://developer.apple.com/documentation/weatherkit/weatherquery/daily(startdate:enddate:)),
[REST v1 parameters](https://developer.apple.com/documentation/weatherkitrestapi/get-api-v1-weather-_language_-_latitude_-_longitude_),
[Apple's REST examples and historical explanation](https://developer.apple.com/videos/play/wwdc2024/10067/),
[Daily Summary interval](https://developer.apple.com/documentation/weatherkit/weatherservice/dailysummary(for:fordaysin:including:)).

The summary's `highTemperature` is documented as an observed daily high.
Neither this definition nor a coordinate query establishes agreement with the
specific CLI station, instrument, or settlement-day convention. Historical
conditions returned today also do not establish what Apple's forecast said
before a past trading decision. Keep final station CLI reports as outcome truth.
[Observed high definition](https://developer.apple.com/documentation/weatherkit/daytemperaturesummary/hightemperature).

## Actual check and code review

At approximately **06:25 UTC on 2026-09-05**, one authenticated REST v1
`forecastHourly` request ran in AWS memory for SFO's **2025-07-19** fixed-standard
day: **2025-07-19 08:00Z inclusive to 2025-07-20 08:00Z exclusive**. The sanitized
report recorded **24 covered hours, zero outside-window rows, and complete
coverage**. No returned temperatures or response body were written to an
archive, log, or training dataset. The local operator report contains structural
counts and requested dates only.

A separate current-forecast compatibility run refreshed all **15/15 cities**
and obtained **30 complete future targets**. Twenty had current exact-key live
EMOS baselines. A read-only metadata check at **06:34:18 UTC** explained all ten
missing matches: Chicago, Dallas, Austin, Houston, and Oklahoma City had crossed
Central standard midnight at 06:00 UTC. Their next-day rows still had the prior
day's lead-two key, and the new lead-two date was absent. All twenty matching
baselines were about 0.88 hours old, below the configured six-hour limit. This
is a refresh-cadence gap, not evidence of Apple accuracy or a reason to relabel
an old forecast lead. The temporary Apple cache was already purged when a later
inspection tried to read it; no additional Apple fetch was needed to explain
the baseline gap. The next natural baseline refresh was scheduled for
**06:40:20 UTC**; its overnight host-local schedule is hourly at :40.

This verifies that endpoint, location, date, and window. It does not verify all
historical dates or all fifteen cities, data accuracy, or original forecast
vintages. Apple describes global coordinate coverage generally, while some
products have regional availability limits.
[WeatherKit overview](https://developer.apple.com/weatherkit/).

Independent review of `forecaster/apple_history_probe.py` found no blocking
source or security defect. All **7 probe tests passed**. The code checks the
completed-day boundary before authentication, requests once without retries,
uses the existing redirect-blocking transport and response-size limit, checks
metadata and finite temperatures, requires exact unique hourly coverage, and
returns no weather values. Its forecast-vintage boolean must be read as a limit
of this probe, not a demonstrated absence of every possible Apple capability.

## Terms: facts and remaining interpretation

Apple Developer Program License Agreement, **Attachment 8**:

- **1.2:** Allows internal uses and transformed value-added products whose
  original Apple data cannot be recovered by users or third parties.
- **1.5:** Prohibits bulk downloads/feeds or extraction, including use of Apple
  data as part of a secondary or derived database.
- **1.6:** Restricts caching, prefetching, and storage to a temporary, limited
  basis solely for API performance unless documentation expressly permits more.

There is **no explicit WeatherKit-specific ML-training ban in these clauses**.
The agreement's express EnergyKit AI-training restriction belongs to another
service. Its wording must not be imported into WeatherKit policy. Conversely,
internal-use and value-added permissions do not establish an exemption from
WeatherKit's storage/database restrictions. A retained Apple training archive
is therefore not an authorized conclusion of this review. The one-date,
in-memory integration check fits the documented internal-use purpose; that is
an interpretation of the combined terms, not written Apple approval.
[Current Apple agreement](https://developer.apple.com/support/terms/apple-developer-program-license-agreement/).

## Next useful work

Keep the history probe a bounded diagnostic. Existing NWP history and official
CLI outcomes can support immediate ML/calibration experiments without an Apple
archive. Apple history may establish API compatibility and historical context;
evaluating Apple's day-ahead contribution requires genuine forecasts captured
before decisions and paired later with station truth. Do not substitute
retrospective conditions for those predictors.

The repository's temporary-cache policy is an implementation choice for
respecting the retention limits, not evidence that Apple forecasts cannot be
used in any model. Resolve the precise retention design before building an
Apple-specific calibration history or permanently retaining derived scores.

**Draft inquiry to Apple Developer Support — not sent:**

> We operate a weather forecasting research service using WeatherKit at fifteen
> fixed US locations. Please clarify under Attachment 8 sections 1.2, 1.5, and
> 1.6 whether we may retain a forecast for up to 72 hours, solely to compare it
> with a subsequently published official station observation, then delete the
> Apple values. May we retain only aggregated error/calibration scores and
> fitted model parameters, without per-date Apple values or recoverable
> residuals, for internal evaluation and a transformed forecasting product?
> Please distinguish permitted aggregation/model updates from a prohibited
> secondary or derived database, and specify any retention, minimum aggregation,
> attribution, or additional-license requirements. If this design is not
> permitted under the standard agreement, is an appropriate permission or
> licensing arrangement available?

This draft is original proposed correspondence, not a quotation from Apple.
