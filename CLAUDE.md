# WeatherEdge — deploy target and access

## Production runtime: AWS EC2 (us-west-1, California)

The trading + forecaster runtime lives on an EC2 instance, NOT on this Mac and
NOT on the laptop. Reach it via a **double hop**: laptop -> `ssh jaxson-build`
(this Mac) -> `ssh -i <key> ubuntu@<ip>` (the EC2 box). Credentials/IP live in
`.local/ec2.env` (gitignored via `.git/info/exclude` — never commit it):

    source .local/ec2.env
    ssh -i "$EC2_KEY" -o StrictHostKeyChecking=accept-new "$REMOTE_USER@$EC2_IP"

- Instance: `i-06b30e0c893b2597a` (`weatheredge-trading-west-1`), t4g.medium
  (2 vCPU, 4 GB RAM), region **us-west-1** (California).
- Public IP: `13.52.240.76` — DNS `ec2-13-52-240-76.us-west-1.compute.amazonaws.com`.
- SSH key: `/Users/jaxson/.ssh/weatheredge-trading-key.pem` (co-located copy at
  `.local/weatheredge-trading-key.pem`). User `ubuntu`, passwordless sudo.
- OS: Ubuntu 24.04 LTS, arm64 (aarch64). Python 3.12.3 (system default).
- App root: `/opt/weatheredge/{trading,forecaster}`; env `/etc/weatheredge.env`
  (root:600). Deploy scripts in `trading/deploy/aws/`; 9 `sfo-*` systemd
  timers/services drive the whole loop.
- Timezone is **America/Los_Angeles** — matches the same-day trading cutoff and
  the nightly ~02:2x-Pacific prune/backfill jobs. Keep it Pacific on any rebuild.
- Full login recipe + health checks: `.local/EC2_WEST_ACCESS.md`.

## GitHub Pages publish auth

The publish pipeline pushes to `git@github.com:Jaxsonb04/weather_edge.git` using
an **SSH deploy key** at `/home/ubuntu/.ssh/sfo_weather_pages_deploy` (referenced
by `SFO_PAGES_DEPLOY_KEY` in `/etc/weatheredge.env`). On any box rebuild that
keypair + `/etc/weatheredge.env` + the `sfo-*` unit files MUST be carried over or
publishing breaks silently while data looks fresh.

## Platform history

- Lightsail (1 GB, us-west-2) until 2026-07-10.
- EC2 t4g.medium **us-east-1** (`i-0539d834272e33991`, `75.101.203.114`) on
  2026-07-10 — launched in the wrong region.
- EC2 t4g.medium **us-west-1** (`i-06b30e0c893b2597a`, `13.52.240.76`) since
  2026-07-11 — the current and only production target. Faithful lift-and-shift
  from us-east-1 with byte-identical DBs verified at cutover. The us-east-1 box
  was quiesced (timers disabled, data intact) as a rollback and is being
  decommissioned.

## Archive layer (2026-07-10)

Nightly retention on the trading DB is archive-gated: every complete UTC day of
the five snapshot tables is losslessly exported and verified before
`paper-prune` deletes anything. See `trading/sfo_kalshi_quant/archive.py` and
`trading/deploy/aws/run_archive_then_prune.sh`. S3 upload is env-gated
(`SFO_ARCHIVE_S3_BUCKET`, unset — archive is a local ring buffer only until a
bucket is created).
