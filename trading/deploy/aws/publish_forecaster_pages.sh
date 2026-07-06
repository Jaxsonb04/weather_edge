#!/usr/bin/env bash
set -euo pipefail

# Publishes the WeatherEdge dashboard to GitHub Pages.
#
# The site is the prebuilt HeroUI React single-page app (SFO_WEBDIST_DIR), with
# the freshly generated data JSONs overlaid on top each refresh so the SPA always
# loads live forecast/trading data. The React app shell changes rarely (rebuild +
# redeploy SFO_WEBDIST_DIR per README "Public Website"); the JSONs
# refresh every cycle.

if [[ "${SFO_PUBLISH_PAGES:-0}" != "1" ]]; then
  echo "GitHub Pages publishing disabled; set SFO_PUBLISH_PAGES=1 to enable"
  exit 0
fi

FORECASTER_DIR="${SFO_FORECASTER_ROOT:-/opt/weatheredge/forecaster}"
WEBDIST_DIR="${SFO_WEBDIST_DIR:-/opt/weatheredge/webdist}"
REMOTE_URL="${SFO_FORECASTER_GIT_REMOTE:-git@github.com:Jaxsonb04/weather_edge.git}"
PAGES_BRANCH="${SFO_PAGES_BRANCH:-gh-pages}"
DEPLOY_KEY="${SFO_PAGES_DEPLOY_KEY:-$HOME/.ssh/sfo_weather_pages_deploy}"

# Fresh data artifacts (regenerated each refresh by the forecaster pipeline).
# These are exactly the JSONs the SPA fetches; everything here is public
# paper-trading research by design.
JSON_ARTIFACTS=(
  trading_signal.json
  forecast_data.json
  weather_story_data.json
  strategy_research.json
)

if [[ ! -d "$FORECASTER_DIR" ]]; then
  echo "missing forecaster directory: $FORECASTER_DIR" >&2
  exit 1
fi
if [[ ! -d "$WEBDIST_DIR" || ! -f "$WEBDIST_DIR/index.html" ]]; then
  echo "missing prebuilt web app at $WEBDIST_DIR (expected index.html)" >&2
  exit 1
fi
if [[ ! -f "$DEPLOY_KEY" ]]; then
  echo "missing GitHub Pages deploy key: $DEPLOY_KEY" >&2
  exit 1
fi

export GIT_SSH_COMMAND="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

# Serialize publishers on the box (hourly forecaster-refresh + 5-min strategy-lab
# refresh) so they cannot race the same gh-pages ref. flock is the primary
# serializer; the fetch+retry loop is the portable backstop.
PAGES_LOCK="${SFO_PAGES_LOCK:-${TMPDIR:-/tmp}/sfo-weather-pages.lock}"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$PAGES_LOCK"
  flock 9
fi

publish_dir="$(mktemp -d "${TMPDIR:-/tmp}/sfo-weather-pages.XXXXXX")"
trap 'rm -rf "$publish_dir"' EXIT

git init -b "$PAGES_BRANCH" "$publish_dir" >/dev/null
cd "$publish_dir"
git remote add origin "$REMOTE_URL"
git config user.name "${SFO_PAGES_GIT_AUTHOR_NAME:-JaxsonB04}"
git config user.email "${SFO_PAGES_GIT_AUTHOR_EMAIL:-JaxsonB04@users.noreply.github.com}"

attempts="${SFO_PAGES_PUSH_ATTEMPTS:-4}"
attempt=1
while true; do
  if git fetch origin "$PAGES_BRANCH" >/dev/null 2>&1; then
    git checkout -B "$PAGES_BRANCH" "origin/$PAGES_BRANCH" >/dev/null
  else
    git checkout --orphan "$PAGES_BRANCH" >/dev/null 2>&1 \
      || git checkout -B "$PAGES_BRANCH" >/dev/null
  fi

  find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +

  # 1) the prebuilt React SPA (index.html, assets/, icons, diagnostics.json, …)
  cp -R "$WEBDIST_DIR"/. ./
  # 2) overlay the freshly generated data JSONs so the SPA loads live data
  for artifact in "${JSON_ARTIFACTS[@]}"; do
    if [[ -e "$FORECASTER_DIR/$artifact" ]]; then
      cp "$FORECASTER_DIR/$artifact" "./$artifact"
    fi
  done
  touch .nojekyll

  git add -A

  if git diff --cached --quiet; then
    echo "GitHub Pages artifacts unchanged"
    exit 0
  fi

  git commit -m "Update SFO weather dashboard" >/dev/null

  if git push origin "HEAD:$PAGES_BRANCH"; then
    echo "Published SFO weather dashboard to $PAGES_BRANCH"
    exit 0
  fi

  if (( attempt >= attempts )); then
    echo "gh-pages push failed after $attempt attempts" >&2
    exit 1
  fi
  echo "gh-pages push rejected (attempt $attempt/$attempts); re-fetching fresh tip and retrying" >&2
  attempt=$((attempt + 1))
  sleep "$attempt"
done
