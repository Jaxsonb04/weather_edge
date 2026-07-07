# AWS Lightsail Deployment

This deploys the project as an always-on paper-trading research box.

Deployment flow:

- Source code is updated locally and published through Git.
- The configured Git branch provides forecaster source for scheduled refreshes.
- Lightsail pulls the `forecaster/` subdirectory from `main` before each
  scheduled forecast refresh.
- Lightsail publishes the prebuilt SPA and fresh data JSONs to `gh-pages`.
- GitHub Pages should serve the `gh-pages` branch.

It runs:

- Forecaster refresh: every 30 minutes from 05:10 through 18:40 Pacific time,
  then hourly overnight at minute 40. Each refresh serves live EMOS forecasts
  for all fifteen registered cities (one batched Open-Meteo call per city),
  refreshes NWS observations (`--days 2 --cities all`), and publishes
  `cities_data.json` alongside the other data JSONs.
- Compact dataset backfill: nightly at 02:25 Pacific time. The nightly unit
  also runs the IEM CLI settlement-truth refresh, the NWP archive update
  (`--daily --cities all`), the EMOS rolling-origin rebuild (leads 1 and 2),
  and `paper-prune` snapshot retention (7 days of full decision snapshots,
  last-per-market-side-day rows to 45 days, approved rows forever — fifteen
  cities would otherwise write ~60k rejection snapshots/~0.5 GB per day).
- Kalshi paper scan: every 5 minutes, around the clock, looping all cities.
  Each run live-fetches the current Kalshi order books and makes paper-trade
  entries on fresh market data (it is not a dashboard rebuild), so a
  newly-listed bracket is acted on within ~5 minutes.
- Paper exit monitor: every 2 minutes around the clock. It also fills resting
  maker limit entries when the visible ask crosses (a proxy fill model, no
  queue position).
- Paper settle: per-city; auto-settle walks each city's own NWS CLI product,
  with archived CLI truth as fallback.

The services are paper-only. They do not contain a live-order path.

## Recommended AWS Instance

Use Amazon Lightsail, not full EC2, for this stage.

- Platform: Linux/Unix
- Blueprint: OS Only, Ubuntu 24.04 LTS or latest Ubuntu LTS
- Region: choose the nearest appropriate AWS region
- Size: start with the small 1 GB Linux bundle
- Name: choose a project-specific instance name

Attach a static IP after creation so the SSH address does not change after
restarts.

## Copy Local Code To The Server

Set these locally after the Lightsail instance exists:

```bash
export LIGHTSAIL_IP="replace_with_static_ip"
export LIGHTSAIL_KEY="/path/to/private/deploy-key.pem"
chmod 600 "$LIGHTSAIL_KEY"
```

Copy WeatherEdge into the server runtime layout:

```bash
bash trading/deploy/aws/sync_to_lightsail.sh
```

This sync intentionally excludes local runtime artifacts such as
`weather.db`, `google_weather_cache.json`, `trading_signal.json`,
`strategy_research.json`, SQLite files, and `trading/data/`. Those files are
generated and refreshed on AWS so stale local MacBook state does not overwrite
server state.

## Install Services

SSH into the server and run the installer:

```bash
ssh -i "$LIGHTSAIL_KEY" ubuntu@$LIGHTSAIL_IP
cd /opt/weatheredge/trading
bash deploy/aws/install_systemd.sh
```

Edit the environment file:

```bash
sudo nano /etc/weatheredge.env
```

Set at least:

```bash
GOOGLE_WEATHER_API_KEY=replace_with_google_weather_key
SFO_PUBLISH_PAGES=1
SFO_FORECASTER_GIT_REMOTE=git@github.com:Jaxsonb04/weather_edge.git
SFO_FORECASTER_SOURCE_SUBDIR=forecaster
SFO_PAGES_DEPLOY_KEY=/home/ubuntu/.ssh/sfo_weather_pages_deploy
SFO_PAGES_BRANCH=gh-pages
SFO_PAGES_GIT_AUTHOR_NAME=JaxsonB04
SFO_PAGES_GIT_AUTHOR_EMAIL=JaxsonB04@users.noreply.github.com
SFO_DATASET_DB=/opt/weatheredge/trading/data/paper_trading.db
SFO_DATASET_SOURCES=iem-asos,open-meteo-previous-runs,open-meteo-historical-forecast,lamp,gfs-mos,nbm,hrrr,kalshi-history
SFO_DATASET_KALSHI_LOOKBACK_DAYS=90
SFO_DATASET_KALSHI_CANDLES=0
SFO_DATASET_KALSHI_TRADES=0
SFO_TRADING_SIGNAL_TARGET_DATE=both
SFO_TRADING_SIGNAL_SIDE=both
SFO_TRADING_SIGNAL_CALIBRATION_SOURCE=lstm
SFO_STRATEGY_RESEARCH_CALIBRATION_MIN_TRAIN=180
# No PAPER_DAILY_BUDGET: paper exposure is risk-gated, not budget-capped.
PAPER_BANKROLL=1000
PAPER_RISK_PROFILE=live
PAPER_RISK_PROFILES=live,research
# All fifteen registered city markets; a comma list of slugs also works.
PAPER_CITIES=all
# Maker-first: rest limit orders (maker fee is 25% of the quadratic taker
# rate); the monitor fills them when the visible ask crosses.
PAPER_ENTRY_MODE=limit
PAPER_TAKE_PROFIT_PCT=40
PAPER_STOP_LOSS_PCT=35
PAPER_YES_TAKE_PROFIT_PCT=50
PAPER_YES_STOP_LOSS_PCT=25
PAPER_NO_TAKE_PROFIT_PCT=35
PAPER_NO_STOP_LOSS_PCT=35
PAPER_MODEL_VETO_MAX_LOSS_PCT=60
PAPER_MODEL_VETO_BUFFER=0.08
SFO_PORTFOLIO_MAX_ARB_SPEND=12
SFO_PORTFOLIO_MIN_PROFIT=0.01
```

Each forecast refresh builds the public `trading_signal.json`, Strategy Lab
research data (`strategy_research.json`), and `cities_data.json` (per-city
forecasts, latest settlement, book activity) before publishing, so GitHub
Pages is published from the same AWS-side forecast DB, paper DB, and
paper-research signal. The publisher ships the prebuilt React SPA from
`/opt/weatheredge/webdist` plus the fresh data JSONs. The
`sfo-strategy-lab-refresh.timer` also runs every five minutes to rebuild
`trading_signal.json` and `strategy_research.json` and republish the Pages
branch without calling `google_weather_cache.py --refresh`. Strategy Lab
research is plain public JSON by design; it contains only paper-trading
research data.

AWS paper scanning is pinned to LSTM calibration for SFO during this
deployment stage; non-SFO cities calibrate from their scored out-of-sample
EMOS archive outcomes.
If `PAPER_RISK_PROFILES` is a comma-list, the scan service runs each profile
back to back in the same paper DB. Orders are tagged by `risk_profile`, so the
`live` paper book and `research` paper book do not block each other.
Scheduled paper placement runs through `portfolio-scan`: guaranteed arbitrage
is funded first, then high-confidence NO core, capped YES convex sleeve, and
research exploration when the profile allows it. The standalone `arbitrage`,
`tail-basket`, and `analyze` commands remain diagnostics, but the timer no
longer places from independent paths with separate budgets.
The public signal builder can be switched later with
`SFO_TRADING_SIGNAL_CALIBRATION_SOURCE=clean-blend` after the server has enough
clean next-day blend rows. Keeping this explicit avoids a silent source change
mid-run.

The compact dataset timer writes point-in-time feature tables into the paper DB
by default. It intentionally starts with Iowa Mesonet ASOS, Open-Meteo forecast
archives, NOAA LAMP/GFS MOS/NBM/HRRR guidance, and Kalshi market history.
Historical Kalshi candles and trades are
opt-in because a broad 90-day pull can hit API rate limits on the small
Lightsail box; run those locally or as a narrow one-off backfill when needed.
NOAA ISD stays out of the default scheduled source list until NCEI access is
stable from the Lightsail instance; add `noaa-isd` to `SFO_DATASET_SOURCES`
later after a manual backfill test succeeds.

To publish the refreshed dashboard to GitHub Pages, create a deploy key on the
server:

```bash
ssh-keygen -t ed25519 -N "" -C "weatheredge-pages" -f ~/.ssh/sfo_weather_pages_deploy
cat ~/.ssh/sfo_weather_pages_deploy.pub
```

Add the public key to the GitHub repository at:

```text
Settings -> Deploy keys -> Add deploy key
```

Enable **Allow write access**. The private key stays on the Lightsail instance.

In the GitHub repository Pages settings, set the source branch to:

```text
gh-pages / root
```

Then enable the timers:

```bash
sudo systemctl enable --now sfo-forecaster-refresh.timer sfo-strategy-lab-refresh.timer sfo-dataset-backfill.timer sfo-kalshi-paper-scan.timer sfo-kalshi-paper-monitor.timer sfo-kalshi-paper-settle.timer sfo-forecast-freshness.timer
```

## Self-Sufficient Refresh (Mac powered off)

The box can own the entire daily refresh, so the Mac heavy forecaster is no longer
required. Every refresh-path entrypoint (`nws_ground_truth`, `google_weather_cache`,
`nwp_archive`, `emos_forecast`) is pure stdlib + numpy/pandas and
peaks ~250 MB; only offline LSTM/XGBoost **training** is heavy, and that stays off the
box (run on a dev machine or in CI; the box serves the committed `models/` artifacts).

To run Mac-off:

1. **Keep the box on light deps only.** `install_systemd.sh` installs `certifi numpy
   pandas` into the forecaster venv. NEVER run `pip install .[forecaster]` is now safe
   (it is light), but `pip install .[train]` (torch/xgboost/scipy/sklearn) must NEVER run
   on the 1 GB box — it will OOM.
2. **Set the env** in `/etc/weatheredge.env`: a real `GOOGLE_WEATHER_API_KEY` (the box
   now drives the paid refresh; the 260/day + 8000/mo budget and overnight throttle still
   apply), and `SFO_ENABLE_LIGHTSAIL_FORECASTER_REFRESH=1`.
3. **Enable the refresh + watchdog timers** (above). `sfo-forecaster-refresh.timer`
   tolerates transient NWS/Google fetch failures (`-` prefix) so a hiccup can't freeze
   publishing.
4. **Freshness watchdog.** `sfo-forecast-freshness.timer` runs
   `check_forecast_db_freshness.sh` every 30 min; if `weather.db` is older than
   `SFO_FORECAST_MAX_AGE_HOURS` (default 6 h, ahead of the 12 h trade-halt) it logs,
   writes a `STALE_FORECAST` marker, exits non-zero (so `systemctl --failed` flags it),
   and POSTs to `SFO_FRESHNESS_ALERT_URL` if set (ntfy.sh / Slack / Discord webhook).
5. **Memory safety.** Every unit carries a `MemoryMax`, and a swapfile is recommended on
   the no-swap box, so a transient spike self-limits instead of the OOM-killer taking the
   trading loop. Tune the `MemoryMax` values after observing real peaks
   (`systemctl status <unit>`).

Retire the Mac only after one on-box refresh succeeds and the freshness watchdog reads OK.

## Check It

Run the services once:

```bash
sudo systemctl start sfo-forecaster-refresh.service
sudo systemctl start sfo-strategy-lab-refresh.service
sudo systemctl start sfo-dataset-backfill.service
sudo systemctl start sfo-kalshi-paper-scan.service
sudo systemctl start sfo-kalshi-paper-monitor.service
sudo systemctl start sfo-kalshi-paper-settle.service
```

See logs:

```bash
journalctl -u sfo-forecaster-refresh.service -n 80 --no-pager
journalctl -u sfo-strategy-lab-refresh.service -n 80 --no-pager
journalctl -u sfo-dataset-backfill.service -n 80 --no-pager
journalctl -u sfo-kalshi-paper-scan.service -n 80 --no-pager
journalctl -u sfo-kalshi-paper-monitor.service -n 80 --no-pager
journalctl -u sfo-kalshi-paper-settle.service -n 80 --no-pager
```

See upcoming runs:

```bash
systemctl list-timers 'sfo-*' --all
```

See paper orders:

```bash
cd /opt/weatheredge/trading
.venv/bin/python -m sfo_kalshi_quant.cli --no-color paper-report
.venv/bin/python -m sfo_kalshi_quant.cli --no-color --db-path data/paper_trading.db dataset-status
```

## Update Code Later

After syncing updated code, run on the server:

```bash
cd /opt/weatheredge/trading
bash deploy/aws/install_systemd.sh
sudo systemctl restart sfo-forecaster-refresh.service
sudo systemctl restart sfo-strategy-lab-refresh.service
```

## Stop Everything

```bash
sudo systemctl disable --now sfo-forecaster-refresh.timer sfo-strategy-lab-refresh.timer sfo-dataset-backfill.timer sfo-kalshi-paper-scan.timer sfo-kalshi-paper-monitor.timer sfo-kalshi-paper-settle.timer
```
