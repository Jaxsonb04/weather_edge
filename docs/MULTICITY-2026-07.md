# Multi-city redesign — 2026-07-06 record

One session took WeatherEdge from a single-market system (SFO daily high) to a
fifteen-city, Maker-side, favorite-band paper-trading research system. This
document records what was built, the evidence it rests on, and — with equal
weight — what is *not* yet proven.

## Why

The binding constraint was bet count, not forecasting. One city yields one or
two independent bets per day; with per-trade return sd ~33% against a mean
~2.6% (Burgi, Deng & Whelan, SSRN 5502658, 313,972 contracts), distinguishing
skill from luck needs `n ≈ (1.96·σ/μ)² ≈ 619` settled independent trades. At
two bets a day that is a year; across fifteen cities it is weeks-to-months.
The same study locates the durable edge for a small liquidity-providing
operator: contracts above ~70c earn small positive post-fee returns
(longshots lose heavily), and Makers materially outperform Takers. Both
caveats were treated as first-class: the +2.6% figure predates the current
fee schedule, and the bias is cleanest in small data-driven markets — hence
weather-only scope and a re-measurement obligation rather than trust.

## What runs now

- **City registry** (`cities.py`, duplicated forecaster/trading with a parity
  test): fifteen cities chosen by 24h volume, each with its Kalshi series,
  NWS settlement station, CLI product, and fixed standard-time offset. The
  settlement traps were verified against live CLI product headers: Houston
  settles on Hobby (KHOU) not Intercontinental, Dallas on KDFW not Love
  Field, Chicago on Midway, New York on Central Park.
- **Forecasting, two tiers.** Non-SFO cities run the station-agnostic
  NWP→EMOS→CLI path: nine-model Open-Meteo previous-runs archive (leakage
  free), per-city rolling-origin EMOS, settlement truth from each city's own
  CLI (live scans + IEM archive). Each city was backfilled ~400 days of NWP
  and ~2.5 years of CLI truth: ~340 scored out-of-sample EMOS days per city
  per lead — the calibration record the trading engine consumes. SFO keeps
  its full blend stack (Google budget, LSTM, marine-layer features).
- **Trading.** Both profiles scan all fifteen series each tick; separation is
  by gates, not city lists. Exposure caps and settlement are series-scoped
  (one city's high can never resolve another's bins on the same date).
  Per-city settlement clocks respect each station's standard-time climate
  day. The SFO warm/hot cohort block stays SFO-scoped — the evidence behind
  it is SFO's.
- **Execution reorientation.** Production entry is maker-first resting
  limits; resting quotes pay the maker fee (25% of the 0.07 quadratic taker
  rate — verified against the live series API: `fee_type=quadratic`,
  `fee_multiplier=1`). The live profile trades only the favorite band
  [0.70, 0.97]; the research profile keeps the whole price curve so the band
  remains measured, not assumed. The 2-minute monitor fills resting limits
  when the visible ask crosses the limit price.

## Evidence from this session (point-in-time, honest)

- First live 15-city sweep (`edge_scan`, 2026-07-06 evening): 51
  favorite-band maker posts across 16 distinct city-days (the old system
  offered 2-3 city-days), median model edge after maker fees +0.8c, mean
  maker-vs-taker cost saving 1.45c/contract. The mean model edge printed
  negative (−3.3c) because the left tail is dominated by nearly-settled
  same-day markets where a lead-1 Gaussian is the wrong instrument — a
  measurement artifact worth fixing (filter same-day evenings), not hiding.
- First production cycles: refresh serves live EMOS for all fifteen cities
  (30/45 rolling targets; the 15 misses are same-day lead-0 by design);
  the scan evaluated every city and placed its first multi-city trade
  (Houston NO at 0.898 — in-band, maker-limit, live profile); monitor,
  settle, and strategy-lab units green; the box survived a reboot with all
  seven timers re-enabled.

## What is NOT yet proven (do not overclaim)

- **No validated post-fee maker edge.** A snapshot cannot measure realized
  returns, maker fill rates, or adverse selection. The displayed-ask fill
  proxy has no queue position; measured "fill rates" are themselves model
  outputs. The definitive measurement is the settled journal, and at the
  literature's variance it needs ~619 settled independent trades before the
  mean is statistically distinguishable from zero.
- **New-city forecast skill is backtest-grade, not live-grade.** The ~340
  scored days per city are rolling-origin out-of-sample, but live serving
  (current-run inputs) has run for one evening. Watch the per-city EMOS
  calibration as live days settle.
- **The favorite band [0.70, 0.97] is a literature prior**, not a fitted
  parameter. The research profile's full-curve record exists precisely so the
  band can be re-fitted (or refuted) on our own settled data.

## Operational notes

- Fifteen cities write ~60k rejection snapshots (~0.5 GB) per day. Production
  retention runs only through `sfo-kalshi-paper-prune.service` and
  `run_archive_then_prune.sh`: the ordering archives and verifies complete UTC
  days before pruning. `SFO_PRUNE_FULL_DAYS=1` keeps one day at full fidelity,
  the last snapshot per market-side-day to 45 days, and approved rows forever.
  Do not schedule bare `paper-prune`; it is low-level/manual recovery tooling.
  The first prune reduced 161,528 → 42,102 rows.
- API budgets that shaped the design: Google Weather stays SFO-only (260
  events/day cap); live EMOS uses one batched Open-Meteo call per city per
  tick; the NWP archive fetch moved to nightly; GFS-ensemble sharpening
  stays SFO-only.
- `cities_data.json` is the public multi-city artifact (per-city forecasts,
  latest settlement, book activity), published every refresh; the site's
  Coverage grid renders it.
