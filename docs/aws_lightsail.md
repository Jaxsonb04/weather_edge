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
`strategy_research.json`, SQLite files, and `trading/data/`. After sync and
refresh, those live artifacts belong to AWS.

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

- `sfo-forecaster-refresh.timer`
- `sfo-strategy-lab-refresh.timer` (every five minutes; live-fetches Kalshi
  top-of-book via `daily-report`, rebuilds `trading_signal.json` and
  `strategy_research.json`, and republishes the SPA site to Pages without paid
  Google Weather refresh calls)
- `sfo-dataset-backfill.timer`
- `sfo-kalshi-paper-scan.timer` (every five minutes; live-fetches the current
  order books and places paper-trade entries on fresh market data)
- `sfo-kalshi-paper-monitor.timer` (every two minutes; live exit prices for open
  positions)
- `sfo-kalshi-paper-settle.timer`

Strategy Lab research is published as plain public `strategy_research.json` by
design. It contains only paper-trading research data, with no secrets.

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

Then rebuild and publish once to verify Strategy Lab output:

```bash
sudo systemctl start sfo-strategy-lab-refresh.service
curl -I https://jaxsonb04.github.io/weather_edge/strategy_research.json
```
