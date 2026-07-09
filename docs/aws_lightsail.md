# AWS Lightsail Notes

Example runtime paths:

```text
/opt/weatheredge/forecaster
/opt/weatheredge/trading
```

Sync into those paths through:

```bash
bash trading/deploy/aws/sync_to_lightsail.sh
```

The sync copies source and rebuild inputs, but excludes local runtime artifacts
such as `weather.db`, `google_weather_cache.json`, `trading_signal.json`,
`strategy_research.json`, `cities_data.json`, `publication_manifest.json`,
SQLite files, and `trading/data/`. After sync and refresh, those live artifacts
belong to AWS.

Required local env before syncing:

```bash
export LIGHTSAIL_IP="replace_with_static_ip"
export LIGHTSAIL_KEY="/path/to/private/deploy-key.pem"
chmod 600 "$LIGHTSAIL_KEY"
```

The systemd installer remains in:

```bash
/opt/weatheredge/trading/deploy/aws/install_systemd.sh
```

## Timers

- `sfo-forecaster-refresh.timer` (every 30 minutes; serves live EMOS forecasts
  for all fifteen cities — one batched Open-Meteo call per city — plus NWS
  observations with `--days 2 --cities all`; this service only refreshes
  forecast state and never builds or publishes site artifacts)
- `sfo-operational-publish.timer` (every five minutes; builds
  `trading_signal.json`, `cities_data.json`, and the checksum-bearing
  `publication_manifest.json`, validates the snapshot, then publishes it)
- `sfo-strategy-lab-refresh.timer` (every fifteen minutes; rebuilds only the
  heavier `strategy_research.json`, refreshes the manifest, validates the
  snapshot, and publishes without paid Google Weather refresh calls)
- `sfo-dataset-backfill.timer` (nightly at 02:25 Pacific; also runs the IEM CLI
  settlement-truth refresh, NWP `--daily --cities all`, the EMOS rolling-origin
  rebuild for leads 1 and 2, and `paper-prune` snapshot retention)
- `sfo-kalshi-paper-scan.timer` (every five minutes; live-fetches the current
  order books across all cities and places paper-trade entries on fresh market
  data)
- `sfo-kalshi-paper-monitor.timer` (every two minutes; live exit prices for open
  positions, and fills resting maker limits when the visible ask crosses)
- `sfo-kalshi-paper-settle.timer` (per-city; walks each city's own NWS CLI
  product, with archived CLI truth as fallback)

Strategy Lab research is published as plain public `strategy_research.json` by
design. It contains only paper-trading research data, with no secrets.

Both publication services hold
`SFO_ARTIFACT_GENERATION_LOCK=/opt/weatheredge/.locks/artifact-generation.lock`
across generation, manifest validation, and artifact copying. This lock is
separate from the publisher's Git lock, which only serializes updates to the
`gh-pages` branch.

The freshness watchdog verifies more than `weather.db`: the operational
`trading_signal.json` and `cities_data.json` generation times must be within
10 minutes, and `strategy_research.json` must be present and within 20 minutes.
Set
`SFO_PUBLICATION_MANIFEST_URL=https://jaxsonb04.github.io/weather_edge/publication_manifest.json`
to apply the same metadata checks to the snapshot visitors receive. Missing,
invalid, stale, or checksum-mismatched local artifacts make the watchdog exit
non-zero.

For an existing paper database, create the covering decision-report index once
with paper scan and monitor services paused. Normal service initialization
deliberately skips this potentially large build on an existing journal:

```bash
sudo systemctl stop sfo-kalshi-paper-scan.timer sfo-kalshi-paper-monitor.timer
sudo systemctl stop sfo-kalshi-paper-scan.service sfo-kalshi-paper-monitor.service
bash /opt/weatheredge/trading/deploy/aws/create_decision_snapshot_index.sh
sudo systemctl start sfo-kalshi-paper-scan.timer sfo-kalshi-paper-monitor.timer
```

## Safety

- Paper scan and monitor are paper-only.
- Do not add live order placement unless it is a new, explicitly approved
  module with tests, kill switches, and separate credentials.
- Do not harden SSH or change firewall/server settings from this project unless
  doing a dedicated server-hardening pass.

## Staged Host Hardening

Do these only after SSH is reachable and a recovery path is confirmed. Keep a
second SSH session or the Lightsail browser SSH console open before changing the
host firewall.

Local checks:

```bash
source .local/lightsail.env
nc -vz -w 5 "$LIGHTSAIL_IP" 22
ssh -i "$LIGHTSAIL_KEY" -o BatchMode=yes -o ConnectTimeout=20 ubuntu@"$LIGHTSAIL_IP" 'date -u && uptime'
```

Before changing the host:

- Confirm the Lightsail firewall allows only intended inbound ports.
- Create or verify a recent Lightsail snapshot.

Server checks:

```bash
sudo systemctl --failed
systemctl list-timers 'sfo-*' --all
free -h
df -h /
sudo ufw status verbose
ss -tulpn
```

Enable a modest swap file if memory headroom remains tight:

```bash
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
printf '/swapfile none swap sw 0 0\n' | sudo tee -a /etc/fstab
free -h
```

Enable UFW only after the second-session/recovery check:

```bash
sudo ufw allow OpenSSH
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw --force enable
sudo ufw status verbose
```

Then rebuild and publish each path once:

```bash
sudo systemctl start sfo-operational-publish.service
sudo systemctl start sfo-strategy-lab-refresh.service
curl -I https://jaxsonb04.github.io/weather_edge/publication_manifest.json
curl -I https://jaxsonb04.github.io/weather_edge/strategy_research.json
```
