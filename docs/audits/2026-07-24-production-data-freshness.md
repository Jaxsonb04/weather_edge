# Production Data Freshness Incident — 2026-07-24

## Executive conclusion

The production generators were healthy, but a long research job serialized
with the visitor-facing delivery path and prevented it from meeting its
five-minute contract.

At 2026-07-25 00:11 UTC, GitHub Pages history showed paired publications only
about every fifteen minutes. The repository contract is one operational
publication every five minutes. After SSH was restored, the AWS process tree
showed the direct cause: Strategy Lab routinely ran for about eleven minutes
while holding `artifact-generation.lock`; the operational publisher waited on
that same lock. The old deployed Strategy Lab path also published after the
research build, producing paired commits when the queued operational cycle
finally ran. This is both lock-priority inversion and deployed-runtime drift:
scheduled forecaster-only source syncs do not reinstall the trading scripts or
systemd units.

GitHub Pages also returned `Cache-Control: max-age=600` for
`publication_manifest.json`. The SPA used `cache: "no-store"`, which bypasses
the browser cache but not the shared CDN cache. A visitor could therefore wait
another ten minutes before learning the hash of a newly published artifact.

Two timestamp semantics made the incident more confusing:

- `market_data_at` used the prediction market's `updated_time`, which is the
  market listing/schema update time, not when WeatherEdge fetched the current
  quote-bearing response.
- City cards said `Refreshed 30m ago` using the forecast model's issue time.
  The thirty-minute daytime forecast cadence is intentional and separate from
  the five-minute publication cadence.

## Evidence

Investigation timestamp: 2026-07-25 00:07–00:17 UTC
(2026-07-24 17:07–17:17 America/Los_Angeles).

- Public operational generation was live:
  - `trading_signal.json`: 2026-07-25 00:06:29 UTC
  - `cities_data.json`: 2026-07-25 00:06:33 UTC
  - `strategy_research.json`: 2026-07-25 00:05:59 UTC
- The latest twenty Pages commits formed pairs roughly fifteen minutes apart.
  Forty-six commits existed in the sampled six-hour window, representing about
  twenty-three effective publication windows, or 3.8/hour instead of 12/hour.
- The paired 00:06 commits carried operational timestamps 00:06:01 and
  00:06:29, and the same Strategy Lab timestamp 00:05:59.
- The public manifest response declared `max-age=600`.
- The live dashboard rendered all fifteen markets and described city forecasts
  as `Refreshed 30m ago`.
- A direct prediction-market API request returned current bid/ask values that
  matched the published report, while every market's `updated_time` still
  matched its listing time (14:00 UTC). This disproved the initial hypothesis
  that the quotes themselves were ten hours old.
- The authoritative AWS host initially could not be inspected because its SSH
  security-group rule still allowed the operator's previous-house `/32`.
  CloudShell was used to authorize the current-house `/32`; SSH was verified
  before the obsolete rule was removed.
- At 00:46 UTC, the live Strategy Lab process had run for over six minutes and
  held `artifact-generation.lock`; both the operational publisher and its
  `flock -w 900` child were waiting. The prior Strategy Lab run took about
  eleven minutes, while the operational publication itself took about
  twenty-eight seconds.

## Permanent fix

1. `publication_manifest.json` polling now adds a unique `poll` query value, so
   each poll obtains the current edge-cache key.
2. Strategy Lab now performs its expensive computation in an isolated staging
   directory without the operational artifact lock. It takes the lock only for
   an atomic promotion and manifest rebuild, so research compute cannot delay
   the five-minute publisher.
3. The Strategy Lab cycle can no longer publish, even through a stale
   environment override. The operational five-minute cycle is the only Pages
   publisher.
4. The watchdog's local and public operational thresholds are both ten minutes.
   Its public request is cache-busted, so a stalled publisher cannot hide behind
   the Pages TTL. The installer migrates only the exact obsolete 15/20-minute
   defaults and preserves custom operator values.
5. The public daily report stamps the actual quote fetch time without changing
   the prediction market's raw source timestamp.
6. City freshness copy now says `Forecast issued`, distinguishing model issue
   time from publication time.

These changes do not alter live-order behavior.

## Deployment and verification

The full EC2 deployment must reinstall the trading scripts and systemd units;
the scheduled forecaster-only sync is intentionally insufficient. After the
full sync:

```bash
systemctl cat sfo-operational-publish.timer
systemctl cat sfo-strategy-lab-refresh.service
systemctl list-timers 'sfo-*' --all
sudo systemctl start sfo-operational-publish.service
sudo systemctl start sfo-forecast-freshness.service
```

Success criteria:

- one Pages commit approximately every five minutes;
- no paired Strategy Lab publication;
- public operational artifact age remains under ten minutes;
- manifest polls return the newest snapshot after the next Pages deployment;
- `market_data_at` is close to report generation time;
- no new order submission or live-order attempt.

## Deployment status

SSH access was restored at 2026-07-25 00:45 UTC by replacing only the obsolete
operator `/32`; port 22 was never opened broadly. The code still requires the
full reviewed EC2 deployment so the corrected runner and systemd configuration
replace the drifted production copies.
