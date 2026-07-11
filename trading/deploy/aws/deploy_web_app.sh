#!/usr/bin/env bash
# Build the HeroUI React dashboard and deploy it to the production box's prebuilt
# web dir ($REMOTE_BASE/webdist). The box's operational publish cycle
# (publish_forecaster_pages.sh) then serves that app shell with the freshly
# generated forecast/trading JSONs overlaid on top.
#
# Run on a machine with the Pro-authenticated toolchain (i.e. the Mac, which has
# @heroui-pro/react installed). The box itself cannot
# build the app (HeroUI Pro CI auth is unavailable there).
#
# Target is EC2 by default (migrated off Lightsail 2026-07-10). Pass a different
# env file to override.
#   Usage: trading/deploy/aws/deploy_web_app.sh [path/to/target.env]
set -euo pipefail

CALLER_CWD="$PWD"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
if [[ -n "${1:-}" ]]; then
  if [[ "$1" == /* ]]; then
    ENV_FILE="$1"
  else
    ENV_FILE="$CALLER_CWD/$1"
  fi
else
  ENV_FILE="$REPO_ROOT/.local/ec2.env"
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing $ENV_FILE (needs the target host IP + SSH key)" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
cd "$REPO_ROOT"

# Accept either the EC2_* names (current) or the legacy LIGHTSAIL_* names so an
# old env file still works during a migration window.
HOST_IP="${EC2_IP:-${LIGHTSAIL_IP:-}}"
HOST_KEY="${EC2_KEY:-${LIGHTSAIL_KEY:-}}"
: "${HOST_IP:?set EC2_IP (or LIGHTSAIL_IP)}"
: "${HOST_KEY:?set EC2_KEY (or LIGHTSAIL_KEY)}"
if [[ ! -f "$HOST_KEY" ]]; then
  echo "SSH key not found: $HOST_KEY" >&2
  exit 1
fi
REMOTE_USER_NAME="${REMOTE_USER:-ubuntu}"
BASE="${REMOTE_BASE:-/opt/weatheredge}"
SSH_OPTS=(-i "$HOST_KEY" -o StrictHostKeyChecking=accept-new)
printf -v RSYNC_RSH 'ssh -i %q -o StrictHostKeyChecking=accept-new' "$HOST_KEY"
printf -v REMOTE_WEBDIST '%q' "$BASE/webdist/"
printf -v REMOTE_MKDIR 'mkdir -p %q' "$BASE/webdist"

echo "==> building dist"
bun run build

echo "==> syncing dist -> $REMOTE_USER_NAME@$HOST_IP:$BASE/webdist"
ssh "${SSH_OPTS[@]}" "$REMOTE_USER_NAME@$HOST_IP" "$REMOTE_MKDIR"
rsync -az --delete -e "$RSYNC_RSH" dist/ "$REMOTE_USER_NAME@$HOST_IP:$REMOTE_WEBDIST"

echo "==> publishing to GitHub Pages"
ssh "${SSH_OPTS[@]}" "$REMOTE_USER_NAME@$HOST_IP" \
  'sudo systemctl start sfo-operational-publish.service'

echo "Done — live at https://jaxsonb04.github.io/weather_edge/"
