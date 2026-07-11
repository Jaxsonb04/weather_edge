#!/usr/bin/env bash
set -euo pipefail

FORECASTER_DIR="${SFO_FORECASTER_ROOT:-/opt/weatheredge/forecaster}"
REMOTE_URL="${SFO_FORECASTER_GIT_REMOTE:-git@github.com:Jaxsonb04/weather_edge.git}"
BRANCH="${SFO_FORECASTER_GIT_BRANCH:-main}"
DEPLOY_KEY="${SFO_PAGES_DEPLOY_KEY:-$HOME/.ssh/sfo_weather_pages_deploy}"
SOURCE_CACHE_DIR="${SFO_FORECASTER_SOURCE_CACHE:-/opt/weatheredge/.cache/main}"
SOURCE_LOCK="${SFO_FORECASTER_SOURCE_LOCK:-/opt/weatheredge/.locks/source-cache-main.lock}"
SOURCE_SUBDIR="${SFO_FORECASTER_SOURCE_SUBDIR:-forecaster}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FORECASTER_EXCLUDES="$SCRIPT_DIR/forecaster-runtime.rsync-filter"

if [[ "$REMOTE_URL" == git@* && ! -f "$DEPLOY_KEY" ]]; then
  echo "missing deploy key for private SSH remote: $DEPLOY_KEY" >&2
  exit 1
fi

if [[ "$REMOTE_URL" == git@* ]]; then
  export GIT_SSH_COMMAND="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
fi

mkdir -p "$(dirname "$SOURCE_CACHE_DIR")" "$FORECASTER_DIR"
mkdir -p "$(dirname "$SOURCE_LOCK")"

exec 9>"$SOURCE_LOCK"
flock 9

if [[ ! -d "$SOURCE_CACHE_DIR/.git" ]]; then
  rm -rf "$SOURCE_CACHE_DIR"
  git clone --depth 1 --branch "$BRANCH" "$REMOTE_URL" "$SOURCE_CACHE_DIR"
else
  cd "$SOURCE_CACHE_DIR"
  git remote set-url origin "$REMOTE_URL"
  git fetch --depth 1 origin "$BRANCH"
  git checkout -B "$BRANCH" "origin/$BRANCH"
fi

RSYNC_SOURCE="$SOURCE_CACHE_DIR/"
if [[ -n "$SOURCE_SUBDIR" ]]; then
  RSYNC_SOURCE="$SOURCE_CACHE_DIR/$SOURCE_SUBDIR/"
fi

if [[ ! -f "$RSYNC_SOURCE/google_weather_cache.py" ]]; then
  echo "missing forecaster source at $RSYNC_SOURCE" >&2
  exit 1
fi

if [[ ! -f "$FORECASTER_EXCLUDES" ]]; then
  echo "missing rsync exclude manifest: $FORECASTER_EXCLUDES" >&2
  exit 1
fi

# These two tracked files are public-site fixtures today. They stay remote-owned
# until the committed-input migration intentionally removes this pair.
rsync -a --delete \
  --exclude-from="$FORECASTER_EXCLUDES" \
  --exclude "forecast_data.json" \
  --exclude "weather_story_data.json" \
  "$RSYNC_SOURCE" "$FORECASTER_DIR"/

echo "synced $REMOTE_URL#$BRANCH:$SOURCE_SUBDIR into $FORECASTER_DIR"
