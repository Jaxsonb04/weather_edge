# AWS Lightsail Deployment

This deploys the project as an always-on paper-trading research box.

Deployment flow:

- Source code is updated locally and published through Git.
- The configured Git branch provides forecaster source for scheduled refreshes.
- Lightsail pulls the `forecaster/` subdirectory from `main` before each
  scheduled forecast refresh.
- Lightsail publishes generated dashboard artifacts to `gh-pages`.
- GitHub Pages should serve the `gh-pages` branch.

It runs:

- Forecaster refresh: every 30 minutes from 05:10 through 18:40 Pacific time,
  then hourly overnight at minute 40.
- Compact dataset backfill: nightly at 02:25 Pacific time.
- Kalshi paper scan: every 5 minutes, around the clock. Each run live-fetches
  the current Kalshi order books and makes paper-trade entries on fresh market
  data (it is not a dashboard rebuild), so a newly-listed bracket is acted on
  within ~5 minutes.
- Paper exit monitor: every 5 minutes from 07:00 through 17:55 Pacific time.

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
`strategy_research.json`, generated dashboard HTML, SQLite files, and
`trading/data/`. Those files are generated and refreshed on AWS so stale local
MacBook state does not overwrite server state.

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
SFO_DATASET_SOURCES=iem-asos,open-meteo-previous-runs,open-meteo-historical-forecast,kalshi-history
SFO_DATASET_KALSHI_LOOKBACK_DAYS=90
SFO_DATASET_KALSHI_CANDLES=0
SFO_DATASET_KALSHI_TRADES=0
SFO_TRADING_SIGNAL_TARGET_DATE=both
SFO_TRADING_SIGNAL_SIDE=both
SFO_TRADING_SIGNAL_CALIBRATION_SOURCE=lstm
SFO_STRATEGY_RESEARCH_CALIBRATION_MIN_TRAIN=180
# Temporary public Strategy Lab mode. Set to 0 and set the password below to
# restore the protected browser-unlock flow.
SFO_STRATEGY_LAB_PUBLIC_MODE=1
# SFO_STRATEGY_LAB_PASSWORD=replace_with_private_strategy_lab_password
SFO_STRATEGY_LAB_PBKDF2_ITERATIONS=210000
# No PAPER_DAILY_BUDGET: paper exposure is risk-gated, not budget-capped.
PAPER_BANKROLL=1000
PAPER_RISK_PROFILE=live
PAPER_RISK_PROFILES=live,research
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

Each forecast refresh builds the public `trading_signal.json` and Strategy Lab
research data before dashboard HTML, so GitHub Pages is published from the same
AWS-side forecast DB, paper DB, and paper-research signal. The
`sfo-strategy-lab-refresh.timer` also runs every five minutes to rebuild
`trading_signal.json`, `strategy_research.json`, `strategy-lab.html`, and the
Pages branch without calling `google_weather_cache.py --refresh`.

Strategy Lab is temporarily published as plaintext
`strategy_research.json` while `SFO_STRATEGY_LAB_PUBLIC_MODE=1`. To restore the
password gate, set `SFO_STRATEGY_LAB_PUBLIC_MODE=0` and set
`SFO_STRATEGY_LAB_PASSWORD`; the publisher then ships
`strategy_research.protected.json` and omits plaintext Strategy Lab research
data.

AWS paper scanning is pinned to LSTM calibration during this deployment stage.
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
archives, and Kalshi market history. Historical Kalshi candles and trades are
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
sudo systemctl enable --now sfo-forecaster-refresh.timer sfo-strategy-lab-refresh.timer sfo-dataset-backfill.timer sfo-kalshi-paper-scan.timer sfo-kalshi-paper-monitor.timer sfo-kalshi-paper-settle.timer
```

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
