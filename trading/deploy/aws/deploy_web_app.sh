#!/usr/bin/env bash
# Build the HeroUI React dashboard and deploy it to the production box's prebuilt
# web dir ($REMOTE_BASE/webdist). The box's operational publish cycle
# (publish_forecaster_pages.sh) then serves that app shell with the freshly
# generated forecast/trading JSONs overlaid on top.
#
# Run from the repo root on a machine with the Pro-authenticated toolchain
# (i.e. the Mac, which has @heroui-pro/react installed). The box itself cannot
# build the app (HeroUI Pro CI auth is unavailable there).
#
# Target is EC2 by default (migrated off Lightsail 2026-07-10). Pass a different
# env file to override.
#   Usage: trading/deploy/aws/deploy_web_app.sh [path/to/target.env]
set -euo pipefail

ENV_FILE="${1:-.local/ec2.env}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing $ENV_FILE (needs the target host IP + SSH key)" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# Accept either the EC2_* names (current) or the legacy LIGHTSAIL_* names so an
# old env file still works during a migration window.
HOST_IP="${EC2_IP:-${LIGHTSAIL_IP:-}}"
HOST_KEY="${EC2_KEY:-${LIGHTSAIL_KEY:-}}"
: "${HOST_IP:?set EC2_IP (or LIGHTSAIL_IP)}"
: "${HOST_KEY:?set EC2_KEY (or LIGHTSAIL_KEY)}"
USER="${REMOTE_USER:-ubuntu}"
BASE="${REMOTE_BASE:-/opt/weatheredge}"
SSH="ssh -i $HOST_KEY -o StrictHostKeyChecking=accept-new"

echo "==> building dist"
bun run build

echo "==> syncing dist -> $USER@$HOST_IP:$BASE/webdist"
$SSH "$USER@$HOST_IP" "mkdir -p $BASE/webdist"
rsync -az --delete -e "$SSH" dist/ "$USER@$HOST_IP:$BASE/webdist/"

echo "==> publishing to GitHub Pages"
$SSH "$USER@$HOST_IP" 'sudo systemctl start sfo-operational-publish.service'

echo "Done — live at https://jaxsonb04.github.io/weather_edge/"
