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
`strategy_research.json`, `strategy_research.protected.json`, generated
dashboard HTML, SQLite files, and `trading/data/`. After sync and refresh,
those live artifacts belong to AWS.

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
- `sfo-strategy-lab-refresh.timer` (every five minutes; rebuilds
  `trading_signal.json`, `strategy_research.json`, dashboard HTML, and Pages
  without paid Google Weather refresh calls)
- `sfo-dataset-backfill.timer`
- `sfo-kalshi-paper-scan.timer`
- `sfo-kalshi-paper-monitor.timer`
- `sfo-kalshi-paper-settle.timer`

Strategy Lab is temporarily public when `SFO_STRATEGY_LAB_PUBLIC_MODE=1`, so
AWS publishes plaintext `strategy_research.json`. To restore the password gate,
set `SFO_STRATEGY_LAB_PUBLIC_MODE=0` and set `SFO_STRATEGY_LAB_PASSWORD`; the
publisher will ship `strategy_research.protected.json` instead.

## Safety

- Paper scan and monitor are paper-only.
- Do not add live order placement unless it is a new, explicitly approved
  module with tests, kill switches, and separate credentials.
- Do not harden SSH or change firewall/server settings from this project unless
  doing a dedicated server-hardening pass.
