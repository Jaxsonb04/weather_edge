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
TRADING_DIR="${SFO_TRADING_ROOT:-/opt/weatheredge/trading}"
PYTHON_BIN="${SFO_TRADING_PYTHON:-$TRADING_DIR/.venv/bin/python}"
WEBDIST_DIR="${SFO_WEBDIST_DIR:-/opt/weatheredge/webdist}"
REMOTE_URL="${SFO_FORECASTER_GIT_REMOTE:-git@github.com:Jaxsonb04/weather_edge.git}"
PAGES_BRANCH="${SFO_PAGES_BRANCH:-gh-pages}"
DEPLOY_KEY="${SFO_PAGES_DEPLOY_KEY:-$HOME/.ssh/sfo_weather_pages_deploy}"
MANIFEST_PATH="${SFO_PUBLICATION_MANIFEST_PATH:-$FORECASTER_DIR/publication_manifest.json}"
ARTIFACT_LOCK="${SFO_ARTIFACT_GENERATION_LOCK:-/opt/weatheredge/.locks/artifact-generation.lock}"
LOCK_WAIT_SECONDS="${SFO_ARTIFACT_LOCK_WAIT_SECONDS:-900}"

# The manifest validator always emits these required files. It emits the
# strategy_research.json artifact only when the manifest records a validated
# current or preserved copy, then emits publication_manifest.json itself.
REQUIRED_JSON_ARTIFACTS=(
  trading_signal.json
  forecast_data.json
  weather_story_data.json
  cities_data.json
  publication_manifest.json
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
if [[ "$PYTHON_BIN" != */* ]]; then
  if ! PYTHON_BIN="$(command -v "$PYTHON_BIN")"; then
    echo "missing trading Python runtime: ${SFO_TRADING_PYTHON:-$PYTHON_BIN}" >&2
    exit 1
  fi
elif [[ ! -x "$PYTHON_BIN" ]]; then
  echo "missing trading Python runtime: $PYTHON_BIN" >&2
  exit 1
fi

if [[ "${SFO_ARTIFACT_LOCK_HELD:-0}" != "1" ]]; then
  if ! command -v flock >/dev/null 2>&1; then
    echo "flock is required for artifact publication" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$ARTIFACT_LOCK")"
  exec 8>"$ARTIFACT_LOCK"
  if ! flock -w "$LOCK_WAIT_SECONDS" 8; then
    echo "timed out waiting for artifact generation lock: $ARTIFACT_LOCK" >&2
    exit 1
  fi
  export SFO_ARTIFACT_LOCK_HELD=1
  export SFO_ARTIFACT_LOCK_FD=8
fi

case "${SFO_ARTIFACT_LOCK_FD:-}" in
  7|8) ;;
  *)
    echo "artifact lock marker is missing a supported inherited descriptor" >&2
    exit 1
    ;;
esac

validate_args=(
  -m sfo_kalshi_quant.publication validate
  --artifact-root "$FORECASTER_DIR"
  --manifest "$MANIFEST_PATH"
  --print-artifacts
)
if [[ "${SFO_REQUIRE_STRATEGY_ARTIFACT:-0}" == "1" ]]; then
  validate_args+=(--require-strategy)
fi
if ! validated_artifacts="$(cd "$TRADING_DIR" && "$PYTHON_BIN" "${validate_args[@]}")"; then
  echo "publication manifest validation failed" >&2
  exit 1
fi
JSON_ARTIFACTS=()
while IFS= read -r artifact; do
  [[ -n "$artifact" ]] && JSON_ARTIFACTS+=("$artifact")
done <<<"$validated_artifacts"

for required in "${REQUIRED_JSON_ARTIFACTS[@]}"; do
  found=0
  for artifact in "${JSON_ARTIFACTS[@]}"; do
    [[ "$artifact" == "$required" ]] && found=1
  done
  if (( found == 0 )); then
    echo "validated publication set omitted required artifact: $required" >&2
    exit 1
  fi
done

snapshot_dir="$(mktemp -d "${TMPDIR:-/tmp}/sfo-weather-snapshot.XXXXXX")"
publish_dir="$(mktemp -d "${TMPDIR:-/tmp}/sfo-weather-pages.XXXXXX")"
trap 'rm -rf "$snapshot_dir" "$publish_dir"' EXIT

# Copy exactly the validator's list while the generation lock is held. There is
# no existence-based skip: a vanished or unreadable configured file fails here.
for artifact in "${JSON_ARTIFACTS[@]}"; do
  case "$artifact" in
    trading_signal.json|forecast_data.json|weather_story_data.json|cities_data.json|strategy_research.json|publication_manifest.json) ;;
    *)
      echo "validator returned unexpected artifact path: $artifact" >&2
      exit 1
      ;;
  esac
  source_path="$FORECASTER_DIR/$artifact"
  if [[ "$artifact" == "publication_manifest.json" ]]; then
    source_path="$MANIFEST_PATH"
  fi
  cp "$source_path" "$snapshot_dir/$artifact"
done

flock -u "$SFO_ARTIFACT_LOCK_FD"
case "$SFO_ARTIFACT_LOCK_FD" in
  7) exec 7>&- ;;
  8) exec 8>&- ;;
esac
unset SFO_ARTIFACT_LOCK_HELD SFO_ARTIFACT_LOCK_FD

export GIT_SSH_COMMAND="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

# Keep the pages Git lock distinct from the artifact-generation lock. The
# artifact snapshot above is already immutable, so slow fetch/push retries do
# not block the next generator.
PAGES_LOCK="${SFO_PAGES_LOCK:-${TMPDIR:-/tmp}/sfo-weather-pages.lock}"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$PAGES_LOCK"
  flock 9
fi

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
    cp "$snapshot_dir/$artifact" "./$artifact"
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
