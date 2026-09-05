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
BASE_DIR="${SFO_BASE_DIR:-${BASE_DIR:-$(dirname "$TRADING_DIR")}}"
PYTHON_BIN="${SFO_TRADING_PYTHON:-$TRADING_DIR/.venv/bin/python}"
WEBDIST_DIR="${SFO_WEBDIST_DIR:-/opt/weatheredge/webdist}"
REMOTE_URL="${SFO_FORECASTER_GIT_REMOTE:-git@github.com:Jaxsonb04/weather_edge.git}"
PAGES_BRANCH="${SFO_PAGES_BRANCH:-gh-pages}"
DEPLOY_KEY="${SFO_PAGES_DEPLOY_KEY:-$HOME/.ssh/sfo_weather_pages_deploy}"
MANIFEST_PATH="${SFO_PUBLICATION_MANIFEST_PATH:-$FORECASTER_DIR/publication_manifest.json}"
ARTIFACT_LOCK="${SFO_ARTIFACT_GENERATION_LOCK:-/opt/weatheredge/.locks/artifact-generation.lock}"
ARTIFACT_LOCK_WAIT_SECONDS="${SFO_OPERATIONAL_ARTIFACT_LOCK_WAIT_SECONDS:-60}"
# The Pages lock is now held across the delivery gate as well as the push, but
# the wait deliberately stays short. Holding it means another publisher is
# already delivering current data, so this cycle gains nothing by queueing
# behind it -- and a wait long enough to cover the gate would consume the whole
# TimeoutStartSec=900 service deadline before any work began. Short wait, then
# defer.
PAGES_LOCK_WAIT_SECONDS="${SFO_PAGES_LOCK_WAIT_SECONDS:-60}"
PROPAGATION_WAITER="${SFO_PAGES_PROPAGATION_WAITER:-$TRADING_DIR/deploy/aws/wait_for_publication_manifest.sh}"
# Workflow-based Pages deploys (pages-deploy-workflow.yml) complete in ~40-60s
# and cancel superseded runs, so the successor push IS the recovery mechanism:
# pushing cancels a hung or queued deploy and replaces it with fresher data.
# Deferring a cycle therefore makes things WORSE, not safer -- observed
# 2026-08-09 05:28Z, a deploy run hung for 13 minutes and the gate's deferral
# delayed the unsticking push, stretching public staleness to ~19 minutes.
# Wait briefly for the ordinary in-flight deploy to land (churn hygiene), then
# always publish. MAX_GATE_DEFERRALS=1 means "publish anyway on the first
# miss"; it is kept configurable only for emergencies.
PENDING_PROPAGATION_TIMEOUT_SECONDS="${SFO_PAGES_PENDING_PROPAGATION_TIMEOUT_SECONDS:-60}"
MAX_GATE_DEFERRALS="${SFO_PAGES_MAX_GATE_DEFERRALS:-1}"
PUBLISH_DEADLINE_SECONDS="${SFO_PAGES_PUBLISH_DEADLINE_SECONDS:-780}"
GATE_STATE_DIR="${SFO_PAGES_GATE_STATE_DIR:-$BASE_DIR/.locks}"
GATE_DEFERRAL_FILE="$GATE_STATE_DIR/pages-gate-deferrals"
PUBLIC_MANIFEST_URL="${SFO_PUBLICATION_MANIFEST_URL:-${SFO_PUBLIC_MANIFEST_URL:-}}"

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
if [[ ! -f "$PROPAGATION_WAITER" ]]; then
  echo "missing Pages propagation waiter: $PROPAGATION_WAITER" >&2
  exit 1
fi
# Anchored to this script's own directory (not $SFO_TRADING_ROOT) so sandboxed
# callers that stub the trading root still find the real template.
PAGES_WORKFLOW_TEMPLATE="${SFO_PAGES_WORKFLOW_TEMPLATE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pages-deploy-workflow.yml}"
if [[ ! -f "$PAGES_WORKFLOW_TEMPLATE" ]]; then
  echo "missing Pages deploy workflow template: $PAGES_WORKFLOW_TEMPLATE" >&2
  exit 1
fi
if [[ ! "$PENDING_PROPAGATION_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "Pages pending propagation timeout must be a positive integer" >&2
  exit 1
fi
if [[ ! "$MAX_GATE_DEFERRALS" =~ ^[1-9][0-9]*$ ]]; then
  echo "Pages gate deferral limit must be a positive integer" >&2
  exit 1
fi
if [[ ! "$PUBLISH_DEADLINE_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "Pages publish deadline must be a positive integer" >&2
  exit 1
fi
if ! command -v flock >/dev/null 2>&1; then
  echo "flock is required for artifact publication" >&2
  exit 1
fi

# The operational runner hands us its generation lock after building. Release
# it before waiting for the previous Pages deployment, then reacquire it for a
# fresh, coherent snapshot. This keeps Strategy Lab promotion unblocked while
# GitHub exposes the already-pushed branch head.
if [[ "${SFO_ARTIFACT_LOCK_HELD:-0}" == "1" ]]; then
  case "${SFO_ARTIFACT_LOCK_FD:-}" in
    7)
      flock -u 7
      exec 7>&-
      ;;
    8)
      flock -u 8
      exec 8>&-
      ;;
    *)
      echo "artifact lock marker is missing a supported inherited descriptor" >&2
      exit 1
      ;;
  esac
  unset SFO_ARTIFACT_LOCK_HELD SFO_ARTIFACT_LOCK_FD
fi

snapshot_dir="$(mktemp -d "${TMPDIR:-/tmp}/sfo-weather-snapshot.XXXXXX")"
publish_dir="$(mktemp -d "${TMPDIR:-/tmp}/sfo-weather-pages.XXXXXX")"
trap 'rm -rf "$snapshot_dir" "$publish_dir"' EXIT

export GIT_SSH_COMMAND="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

# Keep the Pages Git lock across the delivery gate, snapshot, and push so no
# same-host publisher can overtake the branch head we just checked.
PAGES_LOCK="${SFO_PAGES_LOCK:-$BASE_DIR/.locks/pages-publish.lock}"
mkdir -p "$(dirname "$PAGES_LOCK")"
exec 9>"$PAGES_LOCK"
if ! flock -w "$PAGES_LOCK_WAIT_SECONDS" 9; then
  # A concurrent publisher holds the lock and is delivering a newer snapshot
  # than this one. Deferring is correct and is not a unit failure; the next
  # five-minute timer picks up whatever is current then.
  echo "another publisher holds $PAGES_LOCK; deferring this cycle" >&2
  exit 0
fi

git init -b "$PAGES_BRANCH" "$publish_dir" >/dev/null
cd "$publish_dir"
git remote add origin "$REMOTE_URL"
git config user.name "${SFO_PAGES_GIT_AUTHOR_NAME:-JaxsonB04}"
git config user.email "${SFO_PAGES_GIT_AUTHOR_EMAIL:-JaxsonB04@users.noreply.github.com}"

gate_deferrals() {
  local value=0
  if [[ -f "$GATE_DEFERRAL_FILE" ]]; then
    value="$(<"$GATE_DEFERRAL_FILE")"
    [[ "$value" =~ ^[0-9]+$ ]] || value=0
  fi
  printf '%s' "$value"
}

record_gate_deferrals() {
  local value="$1"
  mkdir -p "$GATE_STATE_DIR"
  local tmp="$GATE_DEFERRAL_FILE.$$"
  printf '%s\n' "$value" >"$tmp"
  mv -f "$tmp" "$GATE_DEFERRAL_FILE"
}

# Returns 0 to proceed with publication, 1 to defer this cycle without error.
# A gate that can only ever block would turn a genuinely failed or disabled
# Pages build into a permanent publication outage, because the commit that
# unsticks Pages is exactly the commit the gate refuses to push. After
# MAX_GATE_DEFERRALS consecutive misses we publish anyway: at that point the
# prior deployment is not "in flight", it is broken.
wait_for_remote_publication() {
  local remote_manifest="$snapshot_dir/remote-publication-manifest.json"
  local budget=$((PUBLISH_DEADLINE_SECONDS - SECONDS))
  local started deferrals

  if [[ -z "$PUBLIC_MANIFEST_URL" ]]; then
    echo "no public manifest URL configured; Pages delivery gate skipped" >&2
    return 0
  fi
  if ! git show "origin/$PAGES_BRANCH:publication_manifest.json" >"$remote_manifest" 2>/dev/null; then
    echo "remote Pages branch has no publication_manifest.json; delivery gate skipped" >&2
    return 0
  fi
  if (( budget > PENDING_PROPAGATION_TIMEOUT_SECONDS )); then
    budget="$PENDING_PROPAGATION_TIMEOUT_SECONDS"
  fi
  if (( budget < 1 )); then
    budget=1
  fi

  started="$SECONDS"
  if SFO_PUBLICATION_MANIFEST_PATH="$remote_manifest" \
    SFO_PUBLICATION_PROPAGATION_TIMEOUT_SECONDS="$budget" \
    /bin/bash "$PROPAGATION_WAITER"; then
    echo "prior Pages snapshot became public after $((SECONDS - started))s; publishing successor"
    record_gate_deferrals 0
    return 0
  fi

  deferrals=$(( $(gate_deferrals) + 1 ))
  if (( deferrals >= MAX_GATE_DEFERRALS )); then
    echo "prior Pages snapshot still not public after $deferrals consecutive deferrals; publishing anyway to unstick GitHub Pages" >&2
    record_gate_deferrals 0
    return 0
  fi
  record_gate_deferrals "$deferrals"
  echo "prior Pages snapshot is still deploying (deferral $deferrals/$MAX_GATE_DEFERRALS); skipping this publication cycle"
  return 1
}

prepare_pages_branch() {
  # The Pages branch is a generated snapshot. Fetch only its current head:
  # downloading the full, fast-growing publication history every cycle burned
  # most of the burstable instance's CPU and network budget.
  if git fetch --depth=1 origin "$PAGES_BRANCH" >/dev/null 2>&1; then
    if ! wait_for_remote_publication; then
      return 1
    fi
    git checkout -B "$PAGES_BRANCH" "origin/$PAGES_BRANCH" >/dev/null
  else
    git checkout --orphan "$PAGES_BRANCH" >/dev/null 2>&1 \
      || git checkout -B "$PAGES_BRANCH" >/dev/null
  fi
}

# Do not snapshot new data until the currently pushed branch head is public.
# This is the key backpressure rule: GitHub cannot cancel an in-flight Pages
# deployment because this publisher will not push its successor yet.
if ! prepare_pages_branch; then
  exit 0
fi

mkdir -p "$(dirname "$ARTIFACT_LOCK")"
exec 8>"$ARTIFACT_LOCK"
if ! flock -w "$ARTIFACT_LOCK_WAIT_SECONDS" 8; then
  echo "timed out waiting for artifact generation lock: $ARTIFACT_LOCK" >&2
  exit 1
fi
export SFO_ARTIFACT_LOCK_HELD=1
export SFO_ARTIFACT_LOCK_FD=8

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

flock -u 8
exec 8>&-
unset SFO_ARTIFACT_LOCK_HELD SFO_ARTIFACT_LOCK_FD

attempts="${SFO_PAGES_PUSH_ATTEMPTS:-4}"
attempt=1
while true; do
  if (( attempt > 1 )); then
    if ! prepare_pages_branch; then
      exit 0
    fi
  fi

  find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +

  # 1) the prebuilt React SPA (index.html, assets/, icons, diagnostics.json, …)
  cp -R "$WEBDIST_DIR"/. ./
  # 2) overlay the freshly generated data JSONs so the SPA loads live data
  for artifact in "${JSON_ARTIFACTS[@]}"; do
    cp "$snapshot_dir/$artifact" "./$artifact"
  done
  touch .nojekyll
  # 3) the Pages deploy workflow. The branch is wiped and regenerated on every
  # push, so the workflow must be re-emitted each cycle or GitHub loses it and
  # deploys stop entirely under build_type=workflow.
  mkdir -p .github/workflows
  cp "$PAGES_WORKFLOW_TEMPLATE" .github/workflows/pages-deploy.yml

  git add -A

  if git diff --cached --quiet; then
    echo "GitHub Pages artifacts unchanged"
    exit 0
  fi

  # Audit PR-01: the Pages commit names the deployed source revision.
  SOURCE_SHA="$(grep -o '"source_sha": *"[0-9a-f]\{7,40\}"' "$FORECASTER_DIR/build_info.json" 2>/dev/null | sed 's/.*"\([0-9a-f]*\)"/\1/' | head -1 || true)"
  git commit -m "Update SFO weather dashboard${SOURCE_SHA:+ (source $SOURCE_SHA)}" >/dev/null

  if git push origin "HEAD:$PAGES_BRANCH"; then
    echo "Published SFO weather dashboard to $PAGES_BRANCH"
    exit 0
  fi

  if (( SECONDS >= PUBLISH_DEADLINE_SECONDS )); then
    echo "gh-pages publication budget exhausted after ${SECONDS}s" >&2
    exit 1
  fi
  if (( attempt >= attempts )); then
    echo "gh-pages push failed after $attempt attempts" >&2
    exit 1
  fi
  echo "gh-pages push rejected (attempt $attempt/$attempts); re-fetching fresh tip and retrying" >&2
  attempt=$((attempt + 1))
  sleep "$attempt"
done
