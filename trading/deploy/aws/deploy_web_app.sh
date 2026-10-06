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
if [[ "$BASE" != /* || "$BASE" == "/" || "$BASE" == */ || "$BASE" == *//* ]]; then
  echo "REMOTE_BASE must be a non-root canonical absolute path: $BASE" >&2
  exit 1
fi
IFS='/' read -r -a BASE_COMPONENTS <<<"${BASE#/}"
for component in "${BASE_COMPONENTS[@]}"; do
  if [[ "$component" == "." || "$component" == ".." ]]; then
    echo "REMOTE_BASE must not contain '.' or '..' path components: $BASE" >&2
    exit 1
  fi
done
SSH_OPTS=(-i "$HOST_KEY" -o StrictHostKeyChecking=accept-new)
RSYNC_BIN="${RSYNC_BIN:-$(command -v rsync || true)}"
if [[ -z "$RSYNC_BIN" ]]; then
  echo "rsync is required" >&2
  exit 1
fi
RSYNC_HAS_PROTECT_ARGS=0
if "$RSYNC_BIN" --protect-args --version >/dev/null 2>&1; then
  RSYNC_HAS_PROTECT_ARGS=1
else
  if [[ ! "$BASE" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    echo "rsync at $RSYNC_BIN does not support --protect-args; REMOTE_BASE must match ^/[A-Za-z0-9._/-]+$" >&2
    exit 1
  fi
fi

# Rsync tokenizes -e itself, so a quoted key path in an -e string is unsafe on
# macOS openrsync. Use a no-space executable path and pass the key via the
# environment; the wrapper contains no endpoint or key value.
SSH_WRAPPER_DIR="$(mktemp -d /tmp/weatheredge-rsync-ssh.XXXXXX)"
SSH_WRAPPER="$SSH_WRAPPER_DIR/ssh"
cleanup_ssh_wrapper() {
  rm -rf "$SSH_WRAPPER_DIR"
}
trap cleanup_ssh_wrapper EXIT HUP INT TERM
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  ': "${WEATHEREDGE_RSYNC_SSH_KEY:?missing rsync SSH key}"' \
  'exec ssh -i "$WEATHEREDGE_RSYNC_SSH_KEY" -o StrictHostKeyChecking=accept-new "$@"' \
  >"$SSH_WRAPPER"
chmod 700 "$SSH_WRAPPER"
export WEATHEREDGE_RSYNC_SSH_KEY="$HOST_KEY"

echo "==> building dist"
bun run build

LOCAL_PYTHON="${WEB_DEPLOY_LOCAL_PYTHON:-python3}"
"$LOCAL_PYTHON" "$SCRIPT_DIR/web_app_release.py" --manifest dist --strip-runtime-json \
  >"$SSH_WRAPPER_DIR/candidate_hashes.json"
REMOTE_STAGE="$BASE/.web-deploy/${SSH_WRAPPER_DIR##*/}"
printf -v REMOTE_MKDIR 'mkdir -p %q && mkdir -m 700 %q && mkdir %q' \
  "$BASE/.web-deploy" "$REMOTE_STAGE" "$REMOTE_STAGE/candidate"
echo "==> transferring complete app to a separate release stage"
ssh "${SSH_OPTS[@]}" "$REMOTE_USER_NAME@$HOST_IP" "$REMOTE_MKDIR"
if (( RSYNC_HAS_PROTECT_ARGS )); then
  "$RSYNC_BIN" -az --delete --protect-args -e "$SSH_WRAPPER" \
    dist/ "$REMOTE_USER_NAME@$HOST_IP:$REMOTE_STAGE/candidate/"
  "$RSYNC_BIN" -az --protect-args -e "$SSH_WRAPPER" \
    "$SCRIPT_DIR/web_app_release.py" "$SSH_WRAPPER_DIR/candidate_hashes.json" \
    "$REMOTE_USER_NAME@$HOST_IP:$REMOTE_STAGE/"
else
  "$RSYNC_BIN" -az --delete -e "$SSH_WRAPPER" \
    dist/ "$REMOTE_USER_NAME@$HOST_IP:$REMOTE_STAGE/candidate/"
  "$RSYNC_BIN" -az -e "$SSH_WRAPPER" \
    "$SCRIPT_DIR/web_app_release.py" "$SSH_WRAPPER_DIR/candidate_hashes.json" \
    "$REMOTE_USER_NAME@$HOST_IP:$REMOTE_STAGE/"
fi

printf -v REMOTE_RELEASE 'sudo -n /bin/bash -s -- %q %q %q' \
  "$BASE" "$REMOTE_STAGE" "${REMOTE_ENV_FILE:-/etc/weatheredge.env}"
echo "==> installing atomically and verifying public publication"
ssh "${SSH_OPTS[@]}" "$REMOTE_USER_NAME@$HOST_IP" "$REMOTE_RELEASE" <<'REMOTE'
set -euo pipefail
release_base="$1"
release_stage="$2"
release_env="$3"
if [[ -e /run/weatheredge-deploy-maintenance || -e /var/lib/weatheredge/deploy-deadman/state ]]; then
  echo "backend deployment or unresolved recovery state is active; web app unchanged" >&2
  exit 1
fi
if [[ ! -f "$release_env" ]]; then
  echo "installed publisher EnvironmentFile is unavailable; web app unchanged" >&2
  exit 1
fi
release_env_files="$(systemctl show -p EnvironmentFiles --value sfo-operational-publish.service)"
if [[ "$release_env_files" != "$release_env (ignore_errors=no)" ]]; then
  echo "installed publisher must use exactly the selected required EnvironmentFile" >&2
  exit 1
fi
for property in Environment PassEnvironment UnsetEnvironment; do
  if [[ -n "$(systemctl show -p "$property" --value sfo-operational-publish.service)" ]]; then
    echo "publisher has unsupported environment overrides; web app unchanged" >&2
    exit 1
  fi
done
release_user="$(systemctl show -p User --value sfo-operational-publish.service)"
if [[ -z "$release_user" || "$release_user" == root ]]; then
  echo "installed publisher must run as an explicit application user" >&2
  exit 1
fi
IFS=: read -r _ _ _ _ _ release_user_home _ < <(getent passwd "$release_user")
[[ -n "$release_user_home" ]]
release_working_dir="$(systemctl show -p WorkingDirectory --value sfo-operational-publish.service)"
[[ -n "$release_working_dir" && -d "$release_working_dir" ]]
chown -R "$release_user:" -- "$release_stage"
exec systemd-run --wait --pipe --collect \
  --unit="weatheredge-web-release-${release_stage##*/}" \
  --uid="$release_user" --working-directory="$release_working_dir" \
  --property="EnvironmentFile=$release_env" --setenv="HOME=$release_user_home" \
  /usr/bin/python3 \
  "$release_stage/web_app_release.py" --base "$release_base" --stage "$release_stage"
REMOTE

echo "Done — installed app, Pages deployment, and public file hashes verified."
