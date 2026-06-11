#!/usr/bin/env bash
set -euo pipefail

if [[ "${SFO_PUBLISH_PAGES:-0}" != "1" ]]; then
  echo "GitHub Pages publishing disabled; set SFO_PUBLISH_PAGES=1 to enable"
  exit 0
fi

FORECASTER_DIR="${SFO_FORECASTER_ROOT:-/opt/weatheredge/forecaster}"
REMOTE_URL="${SFO_FORECASTER_GIT_REMOTE:-git@github.com:Jaxsonb04/weather-edge.git}"
PAGES_BRANCH="${SFO_PAGES_BRANCH:-gh-pages}"
DEPLOY_KEY="${SFO_PAGES_DEPLOY_KEY:-$HOME/.ssh/sfo_weather_pages_deploy}"
PUBLIC_MODE_RAW="${SFO_STRATEGY_LAB_PUBLIC_MODE:-1}"
PUBLIC_MODE="$(printf '%s' "$PUBLIC_MODE_RAW" | tr '[:upper:]' '[:lower:]')"

ARTIFACTS=(
  index.html
  details.html
  strategy-lab.html
  google_weather_cache.json
  trading_signal.json
  forecast_data.json
  weather_story_data.json
)

if [[ "$PUBLIC_MODE" != "0" && "$PUBLIC_MODE" != "false" && "$PUBLIC_MODE" != "no" && "$PUBLIC_MODE" != "off" ]]; then
  ARTIFACTS+=(strategy_research.json)
elif [[ -n "${SFO_STRATEGY_LAB_PASSWORD:-}" ]]; then
  ARTIFACTS+=(strategy_research.protected.json)
else
  ARTIFACTS+=(strategy_research.json)
fi

if [[ ! -d "$FORECASTER_DIR" ]]; then
  echo "missing forecaster directory: $FORECASTER_DIR" >&2
  exit 1
fi

if [[ ! -f "$DEPLOY_KEY" ]]; then
  echo "missing GitHub Pages deploy key: $DEPLOY_KEY" >&2
  exit 1
fi

export GIT_SSH_COMMAND="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

present_artifacts=()
for artifact in "${ARTIFACTS[@]}"; do
  if [[ -e "$FORECASTER_DIR/$artifact" ]]; then
    present_artifacts+=("$FORECASTER_DIR/$artifact")
  fi
done

if [[ "${#present_artifacts[@]}" -eq 0 ]]; then
  echo "no publish artifacts found"
  exit 0
fi

publish_dir="$(mktemp -d "${TMPDIR:-/tmp}/sfo-weather-pages.XXXXXX")"
trap 'rm -rf "$publish_dir"' EXIT

git init -b "$PAGES_BRANCH" "$publish_dir" >/dev/null
cd "$publish_dir"
git remote add origin "$REMOTE_URL"
git config user.name "${SFO_PAGES_GIT_AUTHOR_NAME:-JaxsonB04}"
git config user.email "${SFO_PAGES_GIT_AUTHOR_EMAIL:-JaxsonB04@users.noreply.github.com}"

if git fetch origin "$PAGES_BRANCH" >/dev/null 2>&1; then
  git checkout -B "$PAGES_BRANCH" "origin/$PAGES_BRANCH" >/dev/null
else
  git checkout --orphan "$PAGES_BRANCH" >/dev/null
fi

find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +

for artifact_path in "${present_artifacts[@]}"; do
  cp "$artifact_path" "./$(basename "$artifact_path")"
done
touch .nojekyll

git add -A

if git diff --cached --quiet; then
  echo "GitHub Pages artifacts unchanged"
  exit 0
fi

git commit -m "Update SFO weather dashboard"
git push origin "HEAD:$PAGES_BRANCH"
