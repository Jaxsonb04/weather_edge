# WeatherEdge — production access

## Authoritative runtime

The trading and forecasting runtime is the AWS EC2 deployment documented in
docs/aws_deployment.md. Do not diagnose production data from ignored local
runtime files.

Connection details, private keys, instance identifiers, and operator-specific
commands belong in the gitignored .local directory. Load .local/ec2.env and use
the variables defined there; never copy their values into tracked files,
commits, issues, pull requests, or generated artifacts.

The runtime application roots are under /opt/weatheredge, environment settings
are in /etc/weatheredge.env with root-only permissions, and the sfo-prefixed
systemd services and timers drive the unattended pipeline. Production remains
paper-only unless the owner separately authorizes a real-money change.

## Deployment and publishing

Use the scripts under trading/deploy/aws and follow docs/aws_deployment.md.
Preserve the runtime timer state, take a verified database backup, deploy a
clean exact Git SHA, seed the strategy/publication jobs, and restore the
freshness watchdog last.

GitHub Pages authentication is supplied through the production environment.
Never commit the deploy key path or key material. On rebuild, verify publishing
with a harmless dry run and confirm the public manifest hash before declaring
the deployment healthy.

## Archive layer

Nightly retention is archive-gated: complete UTC days are exported and verified
before pruning. Off-host upload is controlled by SFO_ARCHIVE_S3_BUCKET. When it
is unset, the archive is only a local ring buffer and is not a disaster-recovery
backup.
