#!/usr/bin/env bash
set -euo pipefail

if [[ "${SFO_PUBLISH_PAGES:-0}" != "1" ]]; then
  echo "GitHub Pages publishing disabled; set SFO_PUBLISH_PAGES=1 to enable"
  exit 0
fi

FORECASTER_DIR="${SFO_FORECASTER_ROOT:-/opt/weatheredge/forecaster}"
REMOTE_URL="${SFO_FORECASTER_GIT_REMOTE:-git@github.com:Jaxsonb04/weather_edge.git}"
PAGES_BRANCH="${SFO_PAGES_BRANCH:-gh-pages}"
DEPLOY_KEY="${SFO_PAGES_DEPLOY_KEY:-$HOME/.ssh/sfo_weather_pages_deploy}"
PUBLIC_MODE_RAW="${SFO_STRATEGY_LAB_PUBLIC_MODE:-0}"
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
  echo "Strategy Lab protected mode is enabled but SFO_STRATEGY_LAB_PASSWORD is empty" >&2
  exit 1
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

# Serialize publishers on the box so the hourly forecaster-refresh and the
# 5-minute strategy-lab-refresh cannot race the same gh-pages ref (the
# documented intermittent "publish failed" was a non-fast-forward push from two
# overlapping runs). flock is the primary on-box serializer; it is a no-op where
# unavailable (e.g. local macOS dev), and the fetch+retry loop below is the
# portable backstop that also survives any out-of-band pusher.
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

# Re-fetch the remote tip, re-apply artifacts onto it, commit and push -- and on
# a non-fast-forward rejection (another publisher pushed between our fetch and
# push) start over from the fresh tip. "Lost the race" is success, not failure:
# the other run published the same artifacts. Bounded so a genuinely broken
# remote still surfaces an error instead of looping forever.
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

  for artifact_path in "${present_artifacts[@]}"; do
    cp "$artifact_path" "./$(basename "$artifact_path")"
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
