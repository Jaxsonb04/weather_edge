# WeatherEdge — deploy target and access

## Production runtime: AWS EC2 (migrated from Lightsail 2026-07-10)

The trading + forecaster runtime lives on an EC2 instance, NOT on this Mac and
NOT on the laptop. Reach it via a **double hop**: laptop -> `ssh jaxson-build`
(this Mac) -> `ssh -i <key> ubuntu@<ip>` (the EC2 box). Credentials/IP live in
`.local/ec2.env` (gitignored via `.git/info/exclude` — never commit it):

```
source .local/ec2.env
ssh -i "$EC2_KEY" -o StrictHostKeyChecking=accept-new "$REMOTE_USER@$EC2_IP"
```

- Instance: `i-0539d834272e33991` (`weatheredge-trading`), t4g.medium (2 vCPU,
  4 GB RAM), region **us-east-1** (not us-west-2 — a one-off deviation from
  the rest of this AWS account, noted at migration time; not worth relaunching
  over).
- OS: Ubuntu 24.04.4 LTS, arm64 (aarch64). Python 3.12.3 (system default).
- App root: `/opt/weatheredge/{trading,forecaster}`, same layout as the old
  Lightsail box — deploy scripts in `trading/deploy/aws/` and systemd units
  (`sfo-*.service`/`.timer`) are unchanged and portable.
- Security group allows SSH (22) from one IP only — the operator's, not
  0.0.0.0/0. Credit specification is `standard` (not `unlimited`), so the
  box cannot incur burst-CPU overage charges.
- GitHub push (for the `gh-pages` publish pipeline) uses the same credential
  mechanism as before — check `publish_forecaster_pages.sh` if it needs
  rotating.

## Superseded: Lightsail

`.local/lightsail.env` still exists for reference/rollback during the
migration window but the Lightsail instance is being decommissioned. Do not
deploy new changes there once cutover is confirmed complete — check
`.local/ec2.env` first for the current target.

## Archive layer (2026-07-10)

Nightly retention on the trading DB is now archive-gated: every complete UTC
day of the five snapshot tables is losslessly exported and verified before
`paper-prune` is allowed to delete anything. See
`trading/sfo_kalshi_quant/archive.py` and
`trading/deploy/aws/run_archive_then_prune.sh`. S3 upload is env-gated
(`SFO_ARCHIVE_S3_BUCKET`, unset as of this migration — archive currently
lives in the local ring buffer only until that bucket is created).
