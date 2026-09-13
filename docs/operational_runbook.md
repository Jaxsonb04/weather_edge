# Operational Runbook

## Local Verification Gate

Run this before GitHub sync, AWS sync, dashboard publishing, or any larger code
change:

```bash
cd /path/to/WeatherEdge
bash scripts/verify_project.sh
```

This checks required WeatherEdge paths, local secret files, high-confidence
token patterns, trading tests, and Python syntax. Warnings about Git or optional
quality tools are useful setup reminders; failures should be fixed before sync
or deploy work continues.

## GitHub Hygiene Check

Run this before merging audit or deployment work:

```bash
cd /path/to/WeatherEdge
python3 scripts/github_hygiene_check.py
```

The checker is read-only. It verifies public branch-protection status for
`main` and `gh-pages`, reports stale or stacked open PRs, and lists stale remote
branches from local `origin/*` refs. Use a `GITHUB_TOKEN` only if public API rate
limits or private settings require authenticated reads.

## Local Forecast Refresh Without Google API

```bash
cd /path/to/WeatherEdge/forecaster
python nws_ground_truth.py --days 14
python google_weather_cache.py
```

This reuses cached Google Weather and refreshes public/free context.

## Local Forecast Refresh With Google API

```bash
cd /path/to/WeatherEdge/forecaster
export GOOGLE_WEATHER_API_KEY="..."
python google_weather_cache.py --refresh
python google_weather_cache.py
```

Check the budget fields in `google_weather_cache.json` after refresh.

## Paper Analyze

```bash
cd /path/to/WeatherEdge
python -m sfo_kalshi_quant.cli --no-color analyze --target-date both
```

`analyze` loops all fifteen registered cities by default (env `PAPER_CITIES`,
default `all`). Pass `--cities` with `all` or a comma list of slugs to
override:

```bash
python -m sfo_kalshi_quant.cli --no-color analyze --target-date both --cities sfo,lax
```

Production entry mode is reservation-first (`PAPER_ENTRY_MODE=limit`): it
normally rests limits, while configured paper profiles may take a bounded
whole-contract slice of displayed depth only when their exact after-fee point
and lower-bound edge floors still pass. The 2-minute monitor applies the
tape-based proxy fill model to orders that remain resting:

```bash
PAPER_ENTRY_MODE=limit python -m sfo_kalshi_quant.cli --no-color analyze --target-date rolling --side both --place-paper
```

## Portfolio Paper Scan

Scheduled AWS paper placement uses the shared allocator and loops all cities
(`PAPER_CITIES=all` in production; `--cities` narrows it for diagnostics):

```bash
cd /path/to/WeatherEdge/trading
python -m sfo_kalshi_quant.cli --no-color portfolio-scan --target-date rolling --side both
```

To record approved paper portfolio orders:

```bash
python -m sfo_kalshi_quant.cli --no-color portfolio-scan --target-date rolling --side both --place-paper
```

The allocator funds guaranteed arbitrage first, then high-confidence NO core,
capped YES convex exposure, and research-only exploration when the profile
allows it. The commands below are diagnostics and should not replace the
scheduled portfolio path.

## Paper Arbitrage Diagnostic

```bash
cd /path/to/WeatherEdge
python -m sfo_kalshi_quant.cli --no-color arbitrage --target-date rolling --max-arb-spend 12
```

To record approved paper arbitrage portfolios:

```bash
python -m sfo_kalshi_quant.cli --no-color arbitrage --target-date rolling --max-arb-spend 12 --place-paper
```

This scans all active temperature bins for the target day. Same-bin YES+NO boxes
and full-ladder YES/NO sets are paper-placed only when the guaranteed payout is
above all-in cost after rounded fees.

## Public Paper Research Artifact

```bash
cd /path/to/WeatherEdge
python -m sfo_kalshi_quant.cli --no-color daily-report --target-date both --side both --format json --no-live-market --output forecaster/trading_signal.json
python -m sfo_kalshi_quant.cli --no-color strategy-research --output forecaster/strategy_research.json
```

This is read-only. It does not record snapshots, place paper orders, or expose
private DB state. In production, `sfo-operational-publish.timer` runs every five
minutes: `build_public_trading_signal.sh` generates
`forecaster/trading_signal.json`, `forecaster/cities_data.json`, and
`forecaster/publication_manifest.json`, then the publisher validates and ships
that snapshot alongside the SPA. The research-only
`sfo-strategy-lab-refresh.timer` runs on a fixed wall-clock five-minute cadence
to rebuild
`forecaster/strategy_research.json` separately without calling the paid Google
Weather refresh command or rescanning the full decision journal. Historical
backtest/rescore sections come from `strategy_analysis_cache.json` and expose a
separate `analysis_generated_at` timestamp. After production is restored and its
first publication validates, the deploy path refreshes the cache from the
verified immutable database snapshot. The cache is rejected when either the
deployed source SHA or the effective strategy/policy fingerprint changes; until
a matching refresh succeeds, the historical panels say that analysis is
deferred instead of presenting stale metrics as current. Current readiness is
still derived on every public refresh.
Historical replay is never executed on the recurring path: it is served from
the source/config-matched cache or shown as deferred. Current candidates are
supplemented from a hard-bounded, indexed scan-context tail so research profile
tabs stay current without a journal-wide query. The recurring path never
rebuilds a missing dataset-research artifact. A successful full analysis also
promotes the complete private replay evidence before the bounded public
artifact is republished.
No published artifact contains private DB state; the trading artifacts contain
only paper-trading research. `cities_data.json`
supplies per-city forecasts, latest settlement, and book activity for the
fifteen-city Coverage grid.

## Paper Place

```bash
python -m sfo_kalshi_quant.cli --no-color analyze --target-date today --paper-stake 10 --place-paper
```

Only rows that pass all risk gates are recorded.

## Paper Monitor

```bash
python -m sfo_kalshi_quant.cli --no-color paper-monitor \
  --yes-take-profit-pct 50 --yes-stop-loss-pct 25 \
  --no-take-profit-pct 35 --no-stop-loss-pct 35 \
  --model-veto-max-loss-pct 60 --model-veto-buffer 0.08
```

## Paper Settle

```bash
python -m sfo_kalshi_quant.cli --no-color paper-settle --target-date YYYY-MM-DD --settlement-high 67
```

Replace `67` with the official resolved high for that city and date.
Settlement is series-scoped: one city's high can never settle another city's
bins.

AWS can also settle automatically. Auto-settle reads only durable rows from
`weather.db` whose `cli_settlements.is_final=1`; it never books a raw live CLI
response. A target becomes eligible at 06:00 on the next fixed-standard
settlement day, and remains open if confirmed-final truth has not arrived:

```bash
python -m sfo_kalshi_quant.cli --no-color paper-auto-settle
```

Audit recently booked settlements without rewriting their P&L or outcome:

```bash
python -m sfo_kalshi_quant.cli --no-color paper-resettle --verify --days 14
```

The sweep records `MATCH`, `MISMATCH`, and `MISSING_FINAL` results in
`paper_settlement_verifications` using each city's fixed-standard date window.

## Edge Scan Diagnostic

`sfo_kalshi_quant/edge_scan.py` measures the favorite-band maker opportunity
on live order books. Its first run posted 51 quotes across 16 city-days with a
median model edge of +0.8c and a maker-vs-taker saving of 1.45c per contract;
roughly 619 settled trades are needed to confirm a 2.6% mean edge at 95%
confidence.

## Scheduled Multi-City Refresh And Nightly Maintenance

On AWS, the 30-minute forecaster refresh serves live EMOS forecasts for all
fifteen cities (one batched Open-Meteo call per city) plus NWS observations
(`--days 2 --cities all`).

The nightly dataset unit (10:01 UTC, or 03:01 PDT / 02:01 PST) starts after
the archive/prune unit's worst-case deadline and additionally runs:

- IEM CLI settlement-truth refresh
- NWP archive update (`--daily --cities all`, scheduled leads 1 and 2 only)
- EMOS rolling-origin rebuild (leads 1 and 2)

Dataset backfill and retention prune deliberately use `Persistent=false`.
If a deploy or reboot misses either heavy-maintenance window, systemd waits for
the next nightly run instead of replaying both jobs beside persistent forecast
or paper-runtime timers.

Lead 3 is research/on-demand only. Preserve it in explicit historical
`nwp_archive.py --backfill --start ... --end ...` runs, but do not add it back
to the nightly `--daily` job.

## Google Weather Client-Error Breaker

Every Google Weather request is a billable event whether or not it succeeds, and
the event budget alone cannot tell a working key from a dead one. In September
2026 the box billed roughly 124 events a day for five days while **every** request
returned 4xx (last success 2026-09-02T02:41Z, first failure 03:41Z the same
morning, then 100% failures across all three endpoints and all fifteen cities).

`GoogleUsageLedger.reserve_event` now refuses to reserve once
`GOOGLE_WEATHER_CLIENT_ERROR_BREAKER` consecutive completed 4xx events land on
the same Pacific billing date (default 12, about two SFO bundles; 0 disables it).
One non-4xx outcome resets the run, and a new billing date starts clean. The
refresh cycle also checks the breaker once up front and skips every city with
`skipped_reason=client_error_breaker`, and `google_weather_cache.py` prints a
loud `ERROR: Google Weather client-error circuit breaker is OPEN` line. The unit
still exits on the EMOS baseline's result, not Google's: the served forecast does
not depend on Google, and failing the forecaster unit over a dead research
credential would be worse than the outage.

To diagnose an open breaker:

```bash
# Which endpoints, which days, and which HTTP class.
sqlite3 -header -column "file:/opt/weatheredge/forecaster/weather.db?mode=ro" \
  "SELECT billing_date_pacific, endpoint, status, response_status_class,
          error_kind, count(*)
     FROM google_weather_usage_events
    WHERE billing_date_pacific >= date('now','-7 day')
    GROUP BY 1,2,3,4,5 ORDER BY 1 DESC;"
```

The ledger deliberately records only the HTTP **class**, never the status code or
the response body: `_open_google_request` is the only place that ever holds the
key-bearing URL, and `_dispatch_google_request` re-raises a sanitized error so no
secret can escape into a log or a database. A 4xx on every endpoint of every city
is a credential, entitlement, or billing-account failure and has to be fixed in
the Google Cloud console; nothing in this repository can repair it. The breaker's
job is only to stop paying for it.

## Archive-Gated Paper Retention

Production retention belongs only to the dedicated
`sfo-kalshi-paper-prune.timer` / `sfo-kalshi-paper-prune.service`. The service
runs `trading/deploy/aws/run_archive_then_prune.sh`, which losslessly archives
and verifies every complete UTC day before its final prune step. If archival or
the explicit archive gate fails, pruning does not run.

Production sets `SFO_PRUNE_FULL_DAYS=1`; last-per-market-side-day rows remain for
45 days and approved rows remain indefinitely. Probability and paper-monitor
streams, plus unreferenced forecast/market parents, remain online for the same
45-day window; their older lossless copies stay in the archive. Fifteen cities
otherwise write roughly 60k rejection snapshots (~0.5 GB) per day. Do not
schedule or routinely run bare `paper-prune`: it is a low-level/manual command
for recovery work only, after an operator has independently completed and
verified the archive gate.

### Retention modes

`SFO_PRUNE_MODE` selects the delete step. Leaving the key unset selects the
default.

| Mode | What it does | When |
|---|---|---|
| `bounded-delete` | Nightly batched delete with the paper writers running. Each batch commits and releases the write lock; `SFO_PRUNE_MAX_BATCH_SECONDS` (2 s) is a shrink target measured after the batch, not a ceiling, so an early batch can overrun it against the writers' 30 s `busy_timeout` before the row limit halves. | Default. |
| `quiesced-delete` | The same delete plus an operator assertion that every paper-journal writer is stopped. | Supervised catch-up after a long archive-only stretch. |
| `archive-only` | Archive, upload, gate, FK audit; delete nothing. | Escape hatch only. |

To enable nightly deletion on a host that predates this default, do nothing
beyond deploying: the key is absent from `/etc/weatheredge.env` and the wrapper's
default now deletes. To turn it back off, add `SFO_PRUNE_MODE=archive-only` to
that file. Expect roughly an 88% collapse of `decision_snapshots` under the
configured 45-day dedup, taking journal growth from ~0.67 GB/day to ~0.15 GB/day.
Deletion frees SQLite pages but does not shrink the file; run the separately
quiesced `compact_paper_db.sh` once when the filesystem needs the space back.

The first delete after a long archive-only stretch is by far the largest. The
unit is fenced at `MemoryHigh=2600M` / `MemoryMax=3000M` and
`TimeoutStartSec=3600`; a supervised 2026-09-04 catch-up run of 2.1 M rows on a
30 GB journal peaked at 3.3 GB outside those limits. Prefer a one-off
`quiesced-delete` run for the first catch-up on such a host, then let the nightly
`bounded-delete` hold the steady state.

**Watch the first nightly run by hand.** `sfo-kalshi-paper-prune.service` carries
`OnFailure=sfo-alert@%n.service`, but that hook is a no-op while
`SFO_FRESHNESS_ALERT_URL` is empty in `/etc/weatheredge.env` (OPS-3, still open:
48 `alert was not sent` lines in the seven days to 2026-09-06). So an OOM against
`MemoryMax`, an exhausted `TimeoutStartSec`, or a `materialized retention
candidates changed during prune` abort notifies nobody, and retention silently
reverts to the situation this default exists to end. Read
`journalctl -u sfo-kalshi-paper-prune.service -b` after the first 08:20 UTC run.

A failed delete no longer cancels the ring-buffer cleanup. `paper-prune` fails on
ordinary lock contention (SQLITE_BUSY, CLI exit 75); step 7 of the wrapper — the
only thing bounding `data/archive`, ~33 MB/day of uploaded partitions — now runs
regardless, and the wrapper re-reports the delete's exit status afterwards so the
unit still fails.

**Run `ANALYZE` once after the first bounded delete.** `sqlite_stat1` on the box
claimed 914,768 `decision_snapshots` rows against an actual 4,102,302 on
2026-09-03, and an ~88% collapse of that table moves the estimate wrong in the
other direction. Plans still pick the right indexes, but the strategy-lab
`GROUP BY` cost estimates drift further off. `ANALYZE` takes the write lock for
its duration, so run it in the same quiesced window as `compact_paper_db.sh`:

```bash
# paper timers already stopped for the compaction
sqlite3 /opt/weatheredge/trading/data/paper_trading.db 'ANALYZE;'
```

## Operator-Only Box Cleanups

These need a shell on the production host, so no deploy and no agent performs
them. Each is dead weight verified present on 2026-09-07; none is referenced by
any running unit.

```bash
# Stale rsync-era source checkout, frozen at 5bc9113 (2026-07-25). The runtime
# tree is rsynced by sync_to_box.sh, so nothing reads this. ~17 MB.
sudo rm -rf /opt/weatheredge/.cache/main

# Orphaned SQLite sidecars from a backup snapshot that no longer exists. A -shm
# and a zero-byte -wal with no matching .sqlite3 file, plus a checksum sidecar
# whose snapshot the last deploy already removed.
cd /opt/weatheredge/trading/data/backups
ls -la                                   # confirm the .sqlite3 files are gone
sudo rm -f paper_trading-20260828T030029Z.sqlite3-shm \
           paper_trading-20260828T030029Z.sqlite3-wal
# Any *.sha256 with no matching *.sqlite3 is likewise orphaned.

# Frozen legacy Google usage ledger, last written 2026-07-19. The authoritative
# ledger is the google_weather_usage_events table in forecaster/weather.db.
sudo rm -f /opt/weatheredge/forecaster/.google_weather_usage.json

# Superseded hand-made journald drop-in (SystemMaxUse=500M, written 2026-07-11).
# Both installers now delete it and restart journald, so this should already be
# gone after the first deploy that carries OPS-7 -- verify rather than assume,
# because two WeatherEdge drop-ins with contradictory values would be decided
# only by filename order.
ls -la /etc/systemd/journald.conf.d/     # expect only zz-weatheredge.conf
sudo rm -f /etc/systemd/journald.conf.d/00-weatheredge.conf
```

The journald cap moves from 500M to 1500M, so the journal may grow by up to 1 GB
on a box whose deploy backup gate (`available >= db_bytes + 1 GiB`) is the OPS-2
deadline. `ForwardToSyslog=no` gives most of it back — `/var/log/syslog` and its
rotations held ~590 MB on 2026-09-03 and drain over one logrotate cycle — for a
net cost of roughly half a gigabyte. That is deliberate: at 500M the journal
covered 2.6-4.5 days and a four-day-old root cause was simply unavailable during
the audit. If the disk gets tight before `compact_paper_db.sh` runs, lower
`SystemMaxUse` in
`trading/deploy/aws/systemd/weatheredge-journald.conf` and redeploy;
`journalctl --vacuum-size=` reclaims the space immediately.

Delete only what the listing confirms. A `-wal`/`-shm` pair beside a database
file that still exists is live SQLite state, never garbage.

## Signal Backtest

```bash
python -m sfo_kalshi_quant.cli --no-color backtest-signals
python -m sfo_kalshi_quant.cli --no-color backtest-signals --min-quality 60
```

This scores recorded decision snapshots against official settled highs. Use
it to check rejected rows, approved rows, and quality buckets before trusting a
new gate profile.
