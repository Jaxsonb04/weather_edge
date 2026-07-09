#!/usr/bin/env bash
# Build the HeroUI React dashboard and deploy it to the Lightsail box's prebuilt
# web dir (/opt/weatheredge/webdist). The box's 5-minute operational publish
# (publish_forecaster_pages.sh) then serves that app shell with the freshly
# generated forecast/trading JSONs overlaid on top.
#
# Run from the repo root on a machine with the Pro-authenticated toolchain
# (i.e. the Mac, which has @heroui-pro/react installed). The box itself cannot
# build the app (HeroUI Pro CI auth is unavailable there).
#
# Usage: trading/deploy/aws/deploy_web_app.sh [path/to/lightsail.env]
set -euo pipefail

ENV_FILE="${1:-.local/lightsail.env}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing $ENV_FILE (needs LIGHTSAIL_IP and LIGHTSAIL_KEY)" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${LIGHTSAIL_IP:?set LIGHTSAIL_IP}"
: "${LIGHTSAIL_KEY:?set LIGHTSAIL_KEY}"
USER="${REMOTE_USER:-ubuntu}"
SSH="ssh -i $LIGHTSAIL_KEY -o StrictHostKeyChecking=accept-new"

echo "==> building dist"
bun run build

echo "==> syncing dist -> $USER@$LIGHTSAIL_IP:/opt/weatheredge/webdist"
$SSH "$USER@$LIGHTSAIL_IP" 'mkdir -p /opt/weatheredge/webdist'
rsync -az --delete -e "$SSH" dist/ "$USER@$LIGHTSAIL_IP:/opt/weatheredge/webdist/"

echo "==> publishing to GitHub Pages"
$SSH "$USER@$LIGHTSAIL_IP" 'sudo systemctl start sfo-operational-publish.service'

echo "Done — live at https://jaxsonb04.github.io/weather_edge/"
