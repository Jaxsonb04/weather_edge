#!/usr/bin/env bash
set -euo pipefail

# Full operator-driven deploy: the sole production source-change path copies
# both source trees without deleting unrelated remote files. The manual
# recovery sync shares the same runtime-state exclusions.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEATHEREDGE_ROOT="${WEATHEREDGE_ROOT:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
ENV_FILE="${WEATHEREDGE_ENV_FILE:-$WEATHEREDGE_ROOT/.local/ec2.env}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

HOST_IP="${EC2_IP:-${LIGHTSAIL_IP:-}}"
HOST_KEY="${EC2_KEY:-${LIGHTSAIL_KEY:-}}"

if [[ -z "$HOST_IP" ]]; then
  echo "Set EC2_IP (or the legacy LIGHTSAIL_IP fallback) in $ENV_FILE or the environment." >&2
  exit 1
fi
if [[ -z "$HOST_KEY" ]]; then
  echo "Set EC2_KEY (or the legacy LIGHTSAIL_KEY fallback) in $ENV_FILE or the environment." >&2
  exit 1
fi
if [[ ! -f "$HOST_KEY" ]]; then
  echo "SSH key not found: $HOST_KEY" >&2
  exit 1
fi

REMOTE_USER="${REMOTE_USER:-ubuntu}"
REMOTE_BASE="${REMOTE_BASE:-/opt/weatheredge}"
LOCAL_TRADING_DIR="${LOCAL_TRADING_DIR:-$WEATHEREDGE_ROOT/trading}"
LOCAL_FORECASTER_DIR="${LOCAL_FORECASTER_DIR:-$WEATHEREDGE_ROOT/forecaster}"
FORECASTER_EXCLUDES="$SCRIPT_DIR/forecaster-runtime.rsync-filter"
QUIESCE_HELPER="$SCRIPT_DIR/disable_systemd_timers.sh"
BACKUP_HELPER="$SCRIPT_DIR/backup_paper_db.sh"
SYSTEMD_VERIFY_HELPER="$SCRIPT_DIR/verify_systemd_unit_integrity.sh"
INSTALL_SYSTEMD_HELPER="$SCRIPT_DIR/install_systemd.sh"
DEADMAN_HELPER="$SCRIPT_DIR/deploy_deadman.sh"

# Box-side deploy dead-man. Recovery used to live only in this script's bash
# traps, so a host that could no longer reach the box -- a laptop that slept on
# battery mid-backup, a network that handed it an address the security group
# blocks -- left production quiesced and dark: 46.8 h from 2026-09-13T01:22Z and
# 22.3 h from 2026-09-15T02:32Z. deploy_deadman.sh is armed ON THE BOX before
# anything is quiesced, so it survives this host vanishing and restores the
# runtime itself once this host stops proving it is alive.
DEADMAN_BIN="/usr/local/libexec/weatheredge/deploy_deadman.sh"
DEADMAN_STATE_DIR="/var/lib/weatheredge/deploy-deadman"
DEADMAN_ENABLED=1
if [[ "${SFO_DEPLOY_DEADMAN_DISABLE:-0}" == "1" ]]; then
  DEADMAN_ENABLED=0
fi
DEADMAN_LEASE_SECONDS="${SFO_DEPLOY_DEADMAN_LEASE_SECONDS:-600}"
DEADMAN_BEAT_SECONDS="${SFO_DEPLOY_DEADMAN_BEAT_SECONDS:-30}"
DEADMAN_TICK_WAIT_SECONDS="${SFO_DEPLOY_DEADMAN_TICK_WAIT_SECONDS:-180}"
DEADMAN_CLEAR_INCIDENT_FLAG=""
if [[ "${SFO_DEPLOY_DEADMAN_CLEAR_INCIDENT:-0}" == "1" ]]; then
  DEADMAN_CLEAR_INCIDENT_FLAG="--clear-incident"
fi
DEADMAN_BEAT_PID=""
DEADMAN_FENCE=""
DEADMAN_PRE_TRANSFER_RESTORE=0
# How many beats in a row may fail before this host declares itself unwatched.
# The lease tolerates 20 consecutive misses, so three is far inside it, and a
# single blip -- a tick holding the box-side lock, a momentary sudo hiccup --
# must never take a healthy deploy down.
DEADMAN_BEAT_FAILURE_LIMIT="${SFO_DEPLOY_DEADMAN_BEAT_FAILURE_LIMIT:-3}"
MAIN_PID=$$
if [[ ! "$DEADMAN_LEASE_SECONDS" =~ ^[0-9]+$ ]] || (( DEADMAN_LEASE_SECONDS < 60 )); then
  echo "SFO_DEPLOY_DEADMAN_LEASE_SECONDS must be an integer of at least 60" >&2
  exit 1
fi
if [[ ! "$DEADMAN_BEAT_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "SFO_DEPLOY_DEADMAN_BEAT_SECONDS must be a non-negative integer" >&2
  exit 1
fi
if [[ ! "$DEADMAN_TICK_WAIT_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "SFO_DEPLOY_DEADMAN_TICK_WAIT_SECONDS must be a non-negative integer" >&2
  exit 1
fi
if [[ ! "$DEADMAN_BEAT_FAILURE_LIMIT" =~ ^[1-9][0-9]*$ ]]; then
  echo "SFO_DEPLOY_DEADMAN_BEAT_FAILURE_LIMIT must be a positive integer" >&2
  exit 1
fi
if (( DEADMAN_ENABLED == 0 )); then
  echo "warning: SFO_DEPLOY_DEADMAN_DISABLE=1; no box-side dead-man will be armed." >&2
  echo "warning: deploying without it is how production sat dark for 46.8 h from 2026-09-13T01:22Z and 22.3 h from 2026-09-15T02:32Z." >&2
elif (( DEADMAN_BEAT_SECONDS == 0 )); then
  echo "warning: SFO_DEPLOY_DEADMAN_BEAT_SECONDS=0; the box-side dead-man is armed but this host will not refresh its lease between phases, so it can fire mid-deploy." >&2
fi
if (( DEADMAN_ENABLED == 1 && DEADMAN_TICK_WAIT_SECONDS == 0 )); then
  echo "warning: SFO_DEPLOY_DEADMAN_TICK_WAIT_SECONDS=0; arming will not wait for a box-side trigger to actually run the payload, so 'cron and systemd work on that box' goes back to being an assumption." >&2
fi

# Audit F-07: this script deliberately needs NO local interpreter. It used to
# stamp build provenance by importing two package constants, and discovering
# Xcode's Python 3.9 at that point -- roughly 90 lines after production timers
# were already quiesced -- stranded the box mid-deploy with every writer
# stopped. Those constants are now read literally from source further down, so
# there is nothing left to resolve and nothing left to fail on.
DEPLOY_MAINTENANCE_MARKER="/run/weatheredge-deploy-maintenance"
SSH_OPTS=(
  -i "$HOST_KEY"
  -o StrictHostKeyChecking=accept-new
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=6
)

if [[ ! "$REMOTE_BASE" =~ ^/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$ ]]; then
  echo "REMOTE_BASE must be a canonical conservative absolute path: $REMOTE_BASE" >&2
  exit 1
fi
IFS='/' read -r -a REMOTE_BASE_COMPONENTS <<<"${REMOTE_BASE#/}"
for component in "${REMOTE_BASE_COMPONENTS[@]}"; do
  if [[ "$component" == "." || "$component" == ".." ]]; then
    echo "REMOTE_BASE must not contain '.' or '..' path components: $REMOTE_BASE" >&2
    exit 1
  fi
done

if [[ ! -f "$LOCAL_FORECASTER_DIR/google_weather_cache.py" ]]; then
  echo "Forecaster source not found: $LOCAL_FORECASTER_DIR" >&2
  exit 1
fi
if [[ ! -d "$LOCAL_TRADING_DIR/sfo_kalshi_quant" ]]; then
  echo "Trading source not found: $LOCAL_TRADING_DIR" >&2
  exit 1
fi
if [[ ! -f "$WEATHEREDGE_ROOT/pyproject.toml" || ! -f "$WEATHEREDGE_ROOT/README.md" ]]; then
  echo "Root Python project not found: $WEATHEREDGE_ROOT" >&2
  exit 1
fi
if [[ ! -f "$WEATHEREDGE_ROOT/requirements/production.lock" ]]; then
  echo "Hashed production dependency lock is missing." >&2
  exit 1
fi
if [[ ! -f "$FORECASTER_EXCLUDES" ]]; then
  echo "Rsync exclude manifest not found: $FORECASTER_EXCLUDES" >&2
  exit 1
fi
if [[ ! -f "$QUIESCE_HELPER" ]]; then
  echo "Systemd quiescence helper not found: $QUIESCE_HELPER" >&2
  exit 1
fi
if [[ ! -f "$BACKUP_HELPER" ]]; then
  echo "Database backup helper not found: $BACKUP_HELPER" >&2
  exit 1
fi
if [[ ! -f "$SYSTEMD_VERIFY_HELPER" ]]; then
  echo "Systemd unit verification helper not found: $SYSTEMD_VERIFY_HELPER" >&2
  exit 1
fi
if [[ ! -f "$INSTALL_SYSTEMD_HELPER" ]]; then
  echo "Systemd installer not found: $INSTALL_SYSTEMD_HELPER" >&2
  exit 1
fi
if [[ ! -f "$DEADMAN_HELPER" ]]; then
  echo "Deploy dead-man helper not found: $DEADMAN_HELPER" >&2
  exit 1
fi

chmod 600 "$HOST_KEY"

SOURCE_SHA="$(git -C "$WEATHEREDGE_ROOT" rev-parse HEAD)"
SOURCE_BRANCH="$(git -C "$WEATHEREDGE_ROOT" branch --show-current)"
if [[ "$SOURCE_BRANCH" != "main" ]]; then
  echo "Deploy requires clean main; current branch is $SOURCE_BRANCH." >&2
  exit 1
fi
if ! git -C "$WEATHEREDGE_ROOT" diff --quiet \
  || ! git -C "$WEATHEREDGE_ROOT" diff --cached --quiet \
  || [[ -n "$(git -C "$WEATHEREDGE_ROOT" ls-files --others --exclude-standard)" ]]; then
  echo "Deploy requires an exact clean commit; source_dirty would be true." >&2
  exit 1
fi

# Deploy identity for the box-side dead-man. Bash builtins plus `date`, so the
# audit F-07 rule that this script needs no local interpreter still holds.
DEPLOY_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$-$RANDOM"
DEADMAN_DEPLOY_HOST="$(hostname -s 2>/dev/null || echo unknown)"
if [[ ! "$DEADMAN_DEPLOY_HOST" =~ ^[A-Za-z0-9._-]{1,64}$ ]]; then
  DEADMAN_DEPLOY_HOST="unknown"
fi
# Read literally, the same no-interpreter idiom as read_source_version_constant.
DEADMAN_LOCAL_VERSION="$(
  sed -n 's/^DEADMAN_VERSION="\([^"]*\)"$/\1/p' "$DEADMAN_HELPER" | head -n 1
)"
if [[ -z "$DEADMAN_LOCAL_VERSION" ]]; then
  echo "could not read DEADMAN_VERSION from $DEADMAN_HELPER" >&2
  exit 1
fi

# --- box-side deploy dead-man, host side -------------------------------------
deadman_fence_reason() {
  if [[ -n "${DEADMAN_FENCE:-}" && -f "$DEADMAN_FENCE" ]]; then
    head -n 1 "$DEADMAN_FENCE" 2>/dev/null || true
  fi
}

deadman_set_fence() {
  if [[ -n "${DEADMAN_FENCE:-}" ]]; then
    printf '%s\n' "$1" > "$DEADMAN_FENCE" 2>/dev/null || true
  fi
}

# "unwatched" is a recoverable observation, so a later healthy beat retracts it.
# "revoked" is a fact about the box and is never retracted.
deadman_clear_soft_fence() {
  if [[ -n "${DEADMAN_FENCE:-}" && -f "$DEADMAN_FENCE" ]]; then
    if [[ "$(deadman_fence_reason)" == "unwatched" ]]; then
      rm -f "$DEADMAN_FENCE" 2>/dev/null || true
    fi
  fi
}

# The heartbeat loop is a background child: it cannot see the main shell's
# variables, so the main shell records here whether a recovery trap is armed.
# A SIGTERM into the untrapped transfer/install window would kill bash outright
# and leave a half-synced box dark -- worse than the deploy it interrupted.
deadman_traps_armed() {
  if [[ -n "${DEADMAN_FENCE:-}" ]]; then
    : > "$DEADMAN_FENCE.traps" 2>/dev/null || true
  fi
}

deadman_traps_cleared() {
  if [[ -n "${DEADMAN_FENCE:-}" ]]; then
    rm -f "$DEADMAN_FENCE.traps" 2>/dev/null || true
  fi
}

deadman_cleanup_fence() {
  if [[ -n "${DEADMAN_FENCE:-}" ]]; then
    rm -f "$DEADMAN_FENCE" "$DEADMAN_FENCE.traps" 2>/dev/null || true
  fi
}

# Only signal the main shell where a handler exists to receive it. Everywhere
# else the fence is picked up by the next deadman_abort_if_revoked checkpoint,
# which is entered before every remote mutation.
deadman_signal_main() {
  if [[ -n "${DEADMAN_FENCE:-}" && -f "$DEADMAN_FENCE.traps" ]]; then
    kill -TERM "$MAIN_PID" 2>/dev/null || true
  fi
}

# One fresh short-lived SSH connection per beat -- never a remote loop. A remote
# `while :; do touch f; sleep 30; done` keeps the lease fresh for hours after the
# laptop vanishes, because Ubuntu's sshd ships ClientAliveInterval 0 and a
# sleeping laptop never sends a FIN. Each beat is an independent proof that this
# host reached the box within the last SFO_DEPLOY_DEADMAN_BEAT_SECONDS.
deadman_beat_ssh() {
  ssh "${SSH_OPTS[@]}" -o BatchMode=yes -o ConnectTimeout=15 \
    "$REMOTE_USER@$HOST_IP" "sudo -n '$DEADMAN_BIN' beat '$DEPLOY_ID'" \
    < /dev/null > /dev/null 2>&1
}

# Only two statuses PROVE the box-side dead-man acted: 14 (it fired, or this
# deploy no longer owns the state) and 127 (a RESTORED teardown removed the
# payload, so ssh cannot find it). Everything else -- 1 from a payload that
# could not write a full /var, 2, 11, 12, 126 -- is a failure of the messenger,
# not evidence about production. Reading those as "already restored" is how a
# recoverable abort would turn into a quiesced, dark box with the host-side
# recovery deliberately switched off.
deadman_classify_beat() {
  case "$1" in
    0|255) printf 'alive' ;;
    14|127) printf 'revoked' ;;
    13) printf 'unwatched' ;;
    *) printf 'unknown' ;;
  esac
}

deadman_beat_once() {
  local status=0
  (( DEADMAN_ENABLED == 1 )) || return 0
  deadman_beat_ssh || status=$?
  case "$(deadman_classify_beat "$status")" in
    alive) ;;
    revoked) deadman_set_fence revoked ;;
    unwatched) deadman_set_fence unwatched ;;
    *)
      echo "warning: unexpected box-side dead-man beat status=$status; this host stays in control and will recover normally." >&2
      ;;
  esac
  return 0
}

deadman_heartbeat_loop() {
  local status=0
  local consecutive=0
  local verdict=""
  while :; do
    sleep "$DEADMAN_BEAT_SECONDS"
    kill -0 "$MAIN_PID" 2>/dev/null || return 0
    status=0
    deadman_beat_ssh || status=$?
    verdict="$(deadman_classify_beat "$status")"
    if [[ "$verdict" == "revoked" ]]; then
      deadman_set_fence revoked
      deadman_signal_main
      return 0
    fi
    if [[ "$verdict" == "alive" ]]; then
      consecutive=0
      deadman_clear_soft_fence
      continue
    fi
    consecutive=$((consecutive + 1))
    if (( consecutive == 1 )); then
      echo "warning: a box-side dead-man beat failed (status=$status); retrying." >&2
    fi
    if (( consecutive >= DEADMAN_BEAT_FAILURE_LIMIT )); then
      # Armed but nothing is polling it, or unreachable for long enough to
      # matter. Record it and keep beating: the box may come back, and killing
      # a healthy deploy over a lost trigger is strictly worse than today.
      deadman_set_fence unwatched
      deadman_signal_main
      consecutive=0
    fi
  done
}

deadman_stop_heartbeat() {
  if [[ -n "${DEADMAN_BEAT_PID:-}" ]]; then
    kill "$DEADMAN_BEAT_PID" 2>/dev/null || true
    wait "$DEADMAN_BEAT_PID" 2>/dev/null || true
    DEADMAN_BEAT_PID=""
  fi
}

deadman_revoked_notice() {
  echo "ATTENTION: the box-side deploy dead-man fired and already acted on this host." >&2
  echo "This deploy is revoked. Nothing further will be changed remotely." >&2
  echo "Read: sudo $DEADMAN_BIN status; cat $DEADMAN_STATE_DIR/last-action" >&2
  echo "Then trading/deploy/aws/README.md, 'Release deploy and rollback', phase 0." >&2
}

# Checked before every remote mutation. A SIGTERM from the heartbeat loop cannot
# interrupt a foreground ssh or rsync, so the abort latency is one deploy step --
# but no step that changes the box is entered without this check.
deadman_abort_if_revoked() {
  (( DEADMAN_ENABLED == 1 )) || return 0
  if [[ "$(deadman_fence_reason)" != "revoked" ]]; then
    return 0
  fi
  deadman_stop_heartbeat
  PRE_TRANSFER_RECOVERY_ARMED=0
  RUNTIME_RECOVERY_REQUIRED=0
  trap - EXIT HUP INT TERM
  deadman_traps_cleared
  deadman_revoked_notice
  deadman_cleanup_fence
  exit 70
}

# Run first in both recovery traps, after they disarm themselves.
deadman_recovery_gate() {
  local reason=""
  (( DEADMAN_ENABLED == 1 )) || return 0
  deadman_traps_cleared
  deadman_stop_heartbeat
  reason="$(deadman_fence_reason)"
  if [[ "$reason" != "revoked" && "$reason" != "unwatched" ]]; then
    # Close the window between beats: without this, a trap firing shortly after
    # connectivity returned could re-quiesce a box the dead-man had just
    # restored, or restore timers it had deliberately left alone.
    deadman_beat_once
    reason="$(deadman_fence_reason)"
  fi
  case "$reason" in
    revoked)
      PRE_TRANSFER_RECOVERY_ARMED=0
      RUNTIME_RECOVERY_REQUIRED=0
      deadman_revoked_notice
      deadman_cleanup_fence
      exit 70
      ;;
    unwatched)
      echo "warning: the box-side dead-man stopped ticking; recovering from the host and aborting." >&2
      ;;
  esac
  return 0
}

deadman_remote_arm_command() {
  local phase="$1"
  local tick_wait="$2"
  local extra="$3"
  local timer_args="$4"
  local remote_command=""
  remote_command="sudo '$DEADMAN_BIN' arm"
  remote_command="$remote_command --deploy-id '$DEPLOY_ID' --phase '$phase'"
  remote_command="$remote_command --source-sha '$SOURCE_SHA'"
  remote_command="$remote_command --deploy-host '$DEADMAN_DEPLOY_HOST'"
  remote_command="$remote_command --lease-seconds '$DEADMAN_LEASE_SECONDS'"
  remote_command="$remote_command --tick-wait-seconds '$tick_wait'"
  remote_command="$remote_command --pre-transfer-restore '$DEADMAN_PRE_TRANSFER_RESTORE'"
  remote_command="$remote_command --expect-version '$DEADMAN_LOCAL_VERSION'"
  if [[ -n "$extra" ]]; then
    remote_command="$remote_command $extra"
  fi
  remote_command="$remote_command --"
  if [[ -n "$timer_args" ]]; then
    remote_command="$remote_command $timer_args"
  fi
  printf '%s' "$remote_command"
}

deadman_timer_arguments() {
  local timer=""
  local timer_args=""
  for timer in "$@"; do
    if [[ ! "$timer" =~ ^[A-Za-z0-9@._-]+\.timer$ ]]; then
      echo "refusing to hand the box-side dead-man an unexpected timer name: $timer" >&2
      return 1
    fi
    if [[ -n "$timer_args" ]]; then
      timer_args="$timer_args $timer"
    else
      timer_args="$timer"
    fi
  done
  printf '%s' "$timer_args"
}

deadman_arm_initial() {
  local status=0
  local timer_args=""
  local extra=""
  local output=""
  local reported_version=""
  (( DEADMAN_ENABLED == 1 )) || return 0
  timer_args="$(
    deadman_timer_arguments ${CAPTURED_TIMERS[@]+"${CAPTURED_TIMERS[@]}"}
  )" || return 1
  extra="--install-helper --await-tick"
  if [[ -n "$DEADMAN_CLEAR_INCIDENT_FLAG" ]]; then
    extra="$extra $DEADMAN_CLEAR_INCIDENT_FLAG"
  fi
  # tee+mv, never a direct write: a trigger must never execute a half-copied
  # script, and /usr/local/libexec is deliberately outside the rsync target.
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    "sudo install -d -m 755 /usr/local/libexec/weatheredge && sudo tee '$DEADMAN_BIN.new' >/dev/null && sudo chown root:root '$DEADMAN_BIN.new' && sudo chmod 0755 '$DEADMAN_BIN.new' && sudo mv -f '$DEADMAN_BIN.new' '$DEADMAN_BIN'" \
    < "$DEADMAN_HELPER" || status=$?
  if (( status != 0 )); then
    return "$status"
  fi
  # The quiesce helper rides in on stdin and is pinned beside the payload, so
  # every box-side restore goes through this deploy's audited unit allowlist.
  output="$(
    ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
      "$(deadman_remote_arm_command pre-transfer "$DEADMAN_TICK_WAIT_SECONDS" "$extra" "$timer_args")" \
      < "$QUIESCE_HELPER"
  )" || status=$?
  if [[ -n "$output" ]]; then
    printf '%s\n' "$output"
  fi
  if (( status != 0 )); then
    return "$status"
  fi
  reported_version="$(sed -n 's/^DEADMAN_VERSION=//p' <<<"$output" | head -n 1)"
  if [[ -n "$reported_version" && "$reported_version" != "$DEADMAN_LOCAL_VERSION" ]]; then
    echo "box-side dead-man reports version $reported_version; this deploy carries $DEADMAN_LOCAL_VERSION" >&2
    return 1
  fi
  return 0
}

# Phase is data, never a re-arm by stop-and-recreate: there is no unprotected
# window and no create race. The same call is the compare-and-swap fence, so an
# 11 or 14 means this deploy has been revoked.
deadman_phase() {
  local phase="$1"
  shift
  local status=0
  local timer_args=""
  local attempt=1
  local attempts_max="${WEATHEREDGE_RECOVERY_SSH_ATTEMPTS:-4}"
  local retry_seconds="${WEATHEREDGE_RECOVERY_SSH_RETRY_SECONDS:-30}"
  (( DEADMAN_ENABLED == 1 )) || return 0
  timer_args="$(deadman_timer_arguments "$@")" || return 1
  [[ "$attempts_max" =~ ^[1-9][0-9]*$ ]] || attempts_max=4
  [[ "$retry_seconds" =~ ^[0-9]+$ ]] || retry_seconds=30
  # ssh exits 255 for its own connection failures and with the remote command's
  # status otherwise. A phase advance is pure bookkeeping, so a lost connection
  # is retried exactly like the recovery path rather than failing a deploy whose
  # install has already passed every gate.
  while :; do
    status=0
    ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
      "$(deadman_remote_arm_command "$phase" 0 "" "$timer_args")" < /dev/null \
      || status=$?
    if (( status != 255 || attempt >= attempts_max )); then
      break
    fi
    echo "warning: SSH connection lost while advancing the box-side dead-man to phase $phase (attempt $attempt of $attempts_max); retrying in ${retry_seconds}s" >&2
    sleep "$retry_seconds"
    attempt=$((attempt + 1))
  done
  if (( status == 0 )); then
    return 0
  fi
  case "$status" in
    11|14|127)
      deadman_set_fence revoked
      echo "the box-side deploy dead-man no longer recognises this deploy (status=$status)." >&2
      ;;
  esac
  echo "failed to advance the box-side deploy dead-man to phase $phase (status=$status)" >&2
  return "$status"
}

deadman_disarm() {
  (( DEADMAN_ENABLED == 1 )) || return 0
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    "sudo '$DEADMAN_BIN' disarm '$DEPLOY_ID'" < /dev/null
}

# Move the box-side dead-man to ATTENTION. Used only by a recovery that has
# decided to leave this box quiesced on purpose.
deadman_hold() {
  (( DEADMAN_ENABLED == 1 )) || return 0
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    "sudo '$DEADMAN_BIN' hold '$DEPLOY_ID'" < /dev/null
}

# Called at the END of a recovery, once its outcome is known. A recovery that
# put the runtime back owns the box and takes the dead-man down with it; a
# recovery that deliberately left the box quiesced must stop the dead-man from
# re-enabling those same timers ten minutes later. A recovery that FAILED to
# reach the box leaves the dead-man armed on purpose -- that is the case it
# exists for, and the failing ssh is itself the evidence that it is needed.
deadman_finalize_recovery() {
  local action="$1"
  local status=0
  (( DEADMAN_ENABLED == 1 )) || return 0
  case "$action" in
    disarm)
      deadman_disarm > /dev/null 2>&1 || status=$?
      if (( status != 0 && status != 14 && status != 127 )); then
        echo "warning: could not disarm the box-side dead-man after recovery (status=$status); run 'sudo $DEADMAN_BIN clear --force' on the box" >&2
      fi
      ;;
    hold)
      deadman_hold > /dev/null 2>&1 || status=$?
      if (( status == 0 || status == 14 || status == 127 )); then
        echo "The box-side dead-man was moved to ATTENTION so it cannot undo this deliberate quiesce." >&2
      else
        echo "warning: could not hold the box-side dead-man after a failed recovery (status=$status)." >&2
        echo "warning: it may re-enable the release timer set and release maintenance on its own; run 'sudo $DEADMAN_BIN clear --force' on the box if that is not what you want." >&2
      fi
      ;;
  esac
  return 0
}
# --- end box-side deploy dead-man, host side ---------------------------------

verify_deploy_source_unchanged() {
  local phase="$1"
  # Backup verification can run for hours while another task edits this shared
  # checkout. Never install those edits with the revision captured before it.
  if [[ "$(git -C "$WEATHEREDGE_ROOT" rev-parse HEAD)" != "$SOURCE_SHA" \
     || "$(git -C "$WEATHEREDGE_ROOT" branch --show-current)" != "main" ]] \
    || ! git -C "$WEATHEREDGE_ROOT" diff --quiet \
    || ! git -C "$WEATHEREDGE_ROOT" diff --cached --quiet \
    || [[ -n "$(git -C "$WEATHEREDGE_ROOT" ls-files --others --exclude-standard)" ]]; then
    if (( ${PRE_TRANSFER_RECOVERY_ARMED:-0} == 1 )); then
      echo "Deploy source changed during $phase; refusing to stamp or resume this deployment. No source was transferred, so the captured runtime is restored." >&2
    else
      echo "Deploy source changed during $phase; refusing to stamp or resume this deployment. Runtime remains quiesced." >&2
    fi
    exit 1
  fi
}

# Prove the database, AWS identity, and encrypted/versioned backup target are
# usable before stopping a single service. The same audited local helper is
# streamed for preflight and backup so an old remote source tree cannot weaken
# the deployment gate.
REMOTE_DB="$REMOTE_BASE/trading/data/paper_trading.db"
preflight_output="$(
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    bash -s preflight "$REMOTE_DB" < "$BACKUP_HELPER"
)"
if [[ -n "$preflight_output" ]]; then
  printf '%s\n' "$preflight_output"
fi
# The preflight names an existing authoritative database with a dedicated line;
# an explicitly authorized empty host never prints it. Only an established host
# can be stranded by an earlier deploy, so the guard below keys on this.
HOST_DATABASE_PRESENT=0
if grep -qx 'WEATHEREDGE_DATABASE_PRESENT=1' <<<"$preflight_output"; then
  HOST_DATABASE_PRESENT=1
fi

# Preserve an intentional pause if the independent scheduler watchdog already
# exists, but enable it on the first deploy that introduces the unit.
SCHEDULER_WATCHDOG_WAS_ABSENT=0
scheduler_probe_status=0
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  bash -s probe sfo-scheduler-health.timer < "$QUIESCE_HELPER" \
  || scheduler_probe_status=$?
case "$scheduler_probe_status" in
  0) ;;
  10) SCHEDULER_WATCHDOG_WAS_ABSENT=1 ;;
  *)
    echo "failed to inspect scheduler watchdog before quiescence (status=$scheduler_probe_status)" >&2
    exit "$scheduler_probe_status"
    ;;
esac

# FC-4 (2026-09-03 audit, re-verified 2026-09-06): the Apple WeatherKit refresh
# is retired. Its 4x/day paid fetch fills `AppleRuntimeCache.active_highs`,
# which has no caller anywhere outside apple_weatherkit.py; the service log says
# so itself on every run ("live trading weight remains 0"); and the 10-minute
# purge deletes the cache about an hour into each 6-hour cycle, so the data does
# not exist most of the time. It cannot become a scored EMOS member either,
# because Apple's terms do not permit retaining the archive that scoring needs.
# The unit files stay installed so re-enabling is one systemctl command, but the
# installed release must never restore this timer -- including on a host that
# has it enabled right now, whose captured policy would otherwise put it
# straight back. The one exception is a deploy that fails before its first
# rsync: that host still runs the old release, whose check_scheduler_health.sh
# requires every timer it had enabled, so the pre-transfer recovery below puts
# retired timers back too.
RETIRED_TIMERS=(
  "weatheredge-apple-refresh.timer"
)

# Preserve an intentional pause once the Apple purge unit exists, but enable it
# on the first deploy that introduces it. Otherwise the timerless install would
# create a disabled unit that the canonical scheduler check immediately reports
# as missing from the established host's captured policy. The purge survives the
# refresh's retirement deliberately: it makes no API call and it guarantees any
# residual Apple runtime content still expires.
APPLE_PURGE_WAS_ABSENT=0
apple_purge_probe_status=0
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  bash -s probe weatheredge-apple-purge.timer < "$QUIESCE_HELPER" \
  || apple_purge_probe_status=$?
case "$apple_purge_probe_status" in
  0) ;;
  10) APPLE_PURGE_WAS_ABSENT=1 ;;
  *)
    echo "failed to inspect Apple purge timer before quiescence (status=$apple_purge_probe_status)" >&2
    exit "$apple_purge_probe_status"
    ;;
esac

# Capture the established host's timer policy before quiescing it. Stream the
# current helper because the remote source tree may be older than this deploy.
# The capture yields two sets:
#   CAPTURED_TIMERS  every timer enabled at capture, retired ones included. The
#                    pre-transfer recovery restores exactly these: before the
#                    first rsync the host still runs the old release, whose
#                    scheduler watchdog can require a timer this release retires.
#   ENABLED_TIMERS   the policy restored once the new release is installed: the
#                    capture minus retired timers, plus the first-deploy
#                    additions below.
# A failed transfer or install deliberately leaves the box quiesced.
enabled_timer_output="$(
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" bash -s capture < "$QUIESCE_HELPER"
)"
ENABLED_TIMERS=()
CAPTURED_TIMERS=()
CAPTURED_MAINTENANCE_MARKER=0
while IFS= read -r timer; do
  [[ -n "$timer" ]] || continue
  if [[ "$timer" == "@deploy-maintenance-marker-present" ]]; then
    CAPTURED_MAINTENANCE_MARKER=1
    continue
  fi
  CAPTURED_TIMERS+=("$timer")
  retired=0
  for retired_timer in ${RETIRED_TIMERS[@]+"${RETIRED_TIMERS[@]}"}; do
    if [[ "$timer" == "$retired_timer" ]]; then
      retired=1
    fi
  done
  if (( retired == 1 )); then
    echo "retired WeatherEdge timer captured; the installed release will not restore it, only a pre-transfer recovery would: $timer" >&2
    continue
  fi
  ENABLED_TIMERS+=("$timer")
done <<<"$enabled_timer_output"
# Retired timers do not count: a host whose only enabled timer is retired runs
# no production timer, so the stranded-host guard below treats it as empty.
CAPTURED_TIMER_COUNT=${#ENABLED_TIMERS[@]}
if (( SCHEDULER_WATCHDOG_WAS_ABSENT == 1 )); then
  ENABLED_TIMERS+=("sfo-scheduler-health.timer")
fi
if (( APPLE_PURGE_WAS_ABSENT == 1 )); then
  ENABLED_TIMERS+=("weatheredge-apple-purge.timer")
fi

# The release canonical timer set: exactly what install_systemd.sh enables,
# minus retired timers, plus the scheduler watchdog. Read literally from the
# installer so this deploy needs no interpreter and cannot drift from it.
canonical_release_timers() {
  local enable_line=""
  local line_count=""
  local timer=""
  local retired_timer=""
  local retired=0
  local has_watchdog=0
  local -a words=()
  enable_line="$(sed -n 's/^sudo systemctl enable --now //p' "$INSTALL_SYSTEMD_HELPER")"
  line_count="$(grep -c . <<<"$enable_line" || true)"
  if [[ "$line_count" != "1" ]]; then
    echo "expected exactly one 'sudo systemctl enable --now' line in $INSTALL_SYSTEMD_HELPER (found $line_count)" >&2
    return 1
  fi
  read -r -a words <<<"$enable_line"
  for timer in ${words[@]+"${words[@]}"}; do
    if [[ ! "$timer" =~ ^[A-Za-z0-9@._-]+\.timer$ ]]; then
      echo "unexpected token in the installer's enabled timer list: $timer" >&2
      return 1
    fi
    retired=0
    for retired_timer in ${RETIRED_TIMERS[@]+"${RETIRED_TIMERS[@]}"}; do
      if [[ "$timer" == "$retired_timer" ]]; then
        retired=1
      fi
    done
    if (( retired == 1 )); then
      continue
    fi
    case "$timer" in
      sfo-scheduler-health.timer) has_watchdog=1 ;;
    esac
    printf '%s\n' "$timer"
  done
  if (( has_watchdog == 0 )); then
    printf '%s\n' "sfo-scheduler-health.timer"
  fi
}

# Stranded-host guard (release decision 3). A deploy that dies after quiescing
# and before its recovery trap is armed deliberately leaves every timer disabled
# and the maintenance marker in place; production sat dark that way from
# 2026-09-13T01:22Z. Capturing that state as "the policy to restore" would make
# the next deploy restore nothing, remove the marker, never re-arm the scheduler
# watchdog, and still report success -- a dark, unwatched box.
#
# The guard applies to an ESTABLISHED host: one whose preflight found the
# authoritative database. A genuinely new host has neither timers nor a database
# and keeps the historical first-deploy behaviour. On an established host an
# empty capture, or any leftover maintenance marker, is never guessed at:
#   SFO_DEPLOY_RESTORE_CANONICAL_TIMERS=1  a successful deploy restores the
#                                          release canonical timer set, printed
#                                          below before anything is quiesced;
#   SFO_DEPLOY_KEEP_CAPTURED_TIMERS=1      deploy with exactly what was captured,
#                                          so an intentionally paused host stays
#                                          paused.
HOST_STRANDED=0
if (( CAPTURED_MAINTENANCE_MARKER == 1 )) \
  || (( HOST_DATABASE_PRESENT == 1 && CAPTURED_TIMER_COUNT == 0 )); then
  HOST_STRANDED=1
fi
if (( HOST_STRANDED == 1 )); then
  restore_canonical="${SFO_DEPLOY_RESTORE_CANONICAL_TIMERS:-0}"
  keep_captured="${SFO_DEPLOY_KEEP_CAPTURED_TIMERS:-0}"
  if [[ "$restore_canonical" == "1" && "$keep_captured" == "1" ]]; then
    echo "refusing to deploy: set only one of SFO_DEPLOY_RESTORE_CANONICAL_TIMERS=1 and SFO_DEPLOY_KEEP_CAPTURED_TIMERS=1" >&2
    exit 1
  elif [[ "$restore_canonical" == "1" ]]; then
    canonical_output="$(canonical_release_timers)"
    ENABLED_TIMERS=()
    while IFS= read -r timer; do
      if [[ -n "$timer" ]]; then
        ENABLED_TIMERS+=("$timer")
      fi
    done <<<"$canonical_output"
    echo "SFO_DEPLOY_RESTORE_CANONICAL_TIMERS=1: the host looks stranded (captured enabled timers=$CAPTURED_TIMER_COUNT, maintenance marker present=$CAPTURED_MAINTENANCE_MARKER). A successful deploy restores these ${#ENABLED_TIMERS[@]} release canonical timer(s) instead of the captured policy:" >&2
    printf '  %s\n' "${ENABLED_TIMERS[@]}" >&2
  elif [[ "$keep_captured" == "1" ]]; then
    echo "SFO_DEPLOY_KEEP_CAPTURED_TIMERS=1: deploying with the captured policy (captured enabled timers=$CAPTURED_TIMER_COUNT, maintenance marker present=$CAPTURED_MAINTENANCE_MARKER)" >&2
  else
    echo "refusing to deploy: the host looks stranded by an earlier deploy (captured enabled timers=$CAPTURED_TIMER_COUNT, maintenance marker present=$CAPTURED_MAINTENANCE_MARKER)." >&2
    echo "No timer has been quiesced, and no source, unit or maintenance marker has been changed. The backup preflight has already run its sweep, though: it deletes local database snapshot and checksum pairs older than SFO_DATABASE_BACKUP_KEEP_DAYS and, when no maintenance marker is present, interrupted-backup leftovers older than six hours." >&2
    echo "Inspect the host first (trading/deploy/aws/README.md, 'Release deploy and rollback', phase 0), then rerun with" >&2
    echo "SFO_DEPLOY_RESTORE_CANONICAL_TIMERS=1 to restore the release canonical timer set, or" >&2
    echo "SFO_DEPLOY_KEEP_CAPTURED_TIMERS=1 to keep exactly the captured policy." >&2
    exit 1
  fi
fi

# Arm the box-side dead-man before the pre-transfer traps, the maintenance
# marker and the quiesce. Production is fully live at this point, so a failure
# to arm costs nothing: nothing is quiesced and no marker exists.
#
# PRE_TRANSFER_RESTORE is the host's own rule, handed to the box verbatim, so
# the dead-man can never invent a policy this deploy would not apply: a host
# that was already stranded, or a genuinely new one, is never auto-restored.
if (( HOST_STRANDED == 0 && HOST_DATABASE_PRESENT == 1 )); then
  DEADMAN_PRE_TRANSFER_RESTORE=1
fi
if (( DEADMAN_ENABLED == 1 )); then
  DEADMAN_ARM_STATUS=0
  deadman_arm_initial || DEADMAN_ARM_STATUS=$?
  if (( DEADMAN_ARM_STATUS != 0 )); then
    echo "refusing to deploy: could not arm the box-side deploy dead-man (status=$DEADMAN_ARM_STATUS)." >&2
    echo "Nothing has been quiesced and no maintenance marker was installed; production is untouched." >&2
    if (( DEADMAN_ARM_STATUS == 11 )); then
      # The box is owned by an unacknowledged incident or by another deploy's
      # state. Turning the dead-man OFF is the worst possible response to that,
      # so name the right escape hatch and not the blanket one.
      echo "Status 11 means the box already holds dead-man state: an incident nobody has" >&2
      echo "acknowledged, or another deploy's lease. Inspect it before anything else:" >&2
      echo "  ssh <box> sudo $DEADMAN_BIN status" >&2
      echo "  ssh <box> cat $DEADMAN_STATE_DIR/last-action" >&2
      echo "Then follow trading/deploy/aws/README.md, 'Release deploy and rollback', phase 0." >&2
      echo "To take the box over once you have read the incident: SFO_DEPLOY_DEADMAN_CLEAR_INCIDENT=1" >&2
      echo "(equivalently, run 'sudo $DEADMAN_BIN clear --force' on the box)." >&2
    else
      echo "Deploying without it is how production sat dark for 46.8 h from 2026-09-13T01:22Z and" >&2
      echo "22.3 h from 2026-09-15T02:32Z. To proceed anyway: SFO_DEPLOY_DEADMAN_DISABLE=1" >&2
    fi
    exit 1
  fi
  DEADMAN_FENCE="$(mktemp)"
  rm -f "$DEADMAN_FENCE"
  if (( DEADMAN_BEAT_SECONDS > 0 )); then
    # Its own ssh output is already discarded inside deadman_beat_ssh, so the
    # only thing this can print is a warning the operator needs in the log.
    deadman_heartbeat_loop > /dev/null &
    DEADMAN_BEAT_PID=$!
  fi
fi

# Pre-transfer recovery (release review, deploy HIGH). From the maintenance
# marker until the first rsync the remote source tree is still the running
# revision, so a failure in that window -- a dropped SSH session during the
# multi-gigabyte backup round trip, a failed backup gate, an operator Ctrl-C --
# restores every timer enabled at capture (CAPTURED_TIMERS, retired ones
# included, because the old release's scheduler watchdog still requires them)
# and releases maintenance instead of leaving the host dark. Only an
# established, non-stranded host is recovered
# this way: a stranded host was already quiesced before this deploy began (its
# tree may hold a partial earlier transfer), and a new host has nothing to
# restore. From the first rsync on the tree may be mixed, so the historical rule
# applies again and a failure leaves the host quiesced with the marker.
RECOVERY_SSH_ATTEMPTS="${WEATHEREDGE_RECOVERY_SSH_ATTEMPTS:-4}"
RECOVERY_SSH_RETRY_SECONDS="${WEATHEREDGE_RECOVERY_SSH_RETRY_SECONDS:-30}"
if [[ ! "$RECOVERY_SSH_ATTEMPTS" =~ ^[1-9][0-9]*$ || ! "$RECOVERY_SSH_RETRY_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "WEATHEREDGE_RECOVERY_SSH_ATTEMPTS must be a positive integer and WEATHEREDGE_RECOVERY_SSH_RETRY_SECONDS a non-negative integer" >&2
  exit 1
fi
# ssh exits 255 for its own connection failures and with the remote command's
# status otherwise, so only a lost connection is retried; a remote command that
# ran and failed is reported, never repeated.
recovery_ssh() {
  local input="$1"
  shift
  local attempt=1
  local status=0
  while true; do
    status=0
    if [[ -n "$input" ]]; then
      ssh "${SSH_OPTS[@]}" -o ConnectTimeout=20 "$REMOTE_USER@$HOST_IP" "$@" < "$input" || status=$?
    else
      ssh "${SSH_OPTS[@]}" -o ConnectTimeout=20 "$REMOTE_USER@$HOST_IP" "$@" < /dev/null || status=$?
    fi
    if (( status != 255 || attempt >= RECOVERY_SSH_ATTEMPTS )); then
      return "$status"
    fi
    echo "warning: SSH connection lost during pre-transfer recovery (attempt $attempt of $RECOVERY_SSH_ATTEMPTS); retrying in ${RECOVERY_SSH_RETRY_SECONDS}s" >&2
    sleep "$RECOVERY_SSH_RETRY_SECONDS"
    attempt=$((attempt + 1))
  done
}
PRE_TRANSFER_RECOVERY_ARMED=0
recover_pre_transfer_runtime() {
  local interrupted_status="${1:-$?}"
  local restore_status=0
  local release_status=0
  local watchdog_captured=0
  local timer=""
  trap - EXIT HUP INT TERM
  deadman_recovery_gate
  if (( PRE_TRANSFER_RECOVERY_ARMED == 1 )); then
    PRE_TRANSFER_RECOVERY_ARMED=0
    echo "deploy stopped before any source was transferred (status=$interrupted_status); the remote tree is unchanged, so restoring the ${#CAPTURED_TIMERS[@]} captured timer(s) and releasing maintenance" >&2
    if (( ${#CAPTURED_TIMERS[@]} > 0 )); then
      recovery_ssh "$QUIESCE_HELPER" bash -s restore "${CAPTURED_TIMERS[@]}" || restore_status=$?
    fi
    if (( restore_status == 0 )); then
      recovery_ssh "" "sudo rm -f -- '$DEPLOY_MAINTENANCE_MARKER'" || release_status=$?
    fi
    if (( restore_status == 0 && release_status == 0 )); then
      for timer in ${CAPTURED_TIMERS[@]+"${CAPTURED_TIMERS[@]}"}; do
        case "$timer" in
          sfo-scheduler-health.timer) watchdog_captured=1 ;;
        esac
      done
      if (( watchdog_captured == 1 )); then
        recovery_ssh "" "sudo systemctl start sfo-scheduler-health.service" \
          || echo "warning: scheduler health run after pre-transfer recovery failed; its timer is active" >&2
      fi
      echo "Pre-transfer recovery restored ${#CAPTURED_TIMERS[@]} timer(s) and released deployment maintenance." >&2
      # The box is fully back, so leave nothing behind. A dead-man left armed
      # here would keep refusing the next deploy for the rest of its lease.
      deadman_finalize_recovery disarm
    else
      echo "warning: pre-transfer recovery failed (restore status=$restore_status, release status=$release_status). The host may remain quiesced with the maintenance marker: follow trading/deploy/aws/README.md, 'Release deploy and rollback', phase 0." >&2
      # Deliberately NOT disarmed: this is exactly the box-side dead-man's case,
      # and the tree is still the running revision, so its pre-transfer restore
      # is safe.
      echo "The box-side dead-man is still armed and will attempt this restore itself." >&2
    fi
  fi
  deadman_cleanup_fence
  exit "$interrupted_status"
}
if (( HOST_STRANDED == 0 && HOST_DATABASE_PRESENT == 1 )); then
  PRE_TRANSFER_RECOVERY_ARMED=1
  trap 'recover_pre_transfer_runtime 129' HUP
  trap 'recover_pre_transfer_runtime 130' INT
  trap 'recover_pre_transfer_runtime 143' TERM
  trap 'recover_pre_transfer_runtime $?' EXIT
  deadman_traps_armed
fi

ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "sudo install -o root -g root -m 600 /dev/null '$DEPLOY_MAINTENANCE_MARKER'"
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" bash -s quiesce < "$QUIESCE_HELPER"

backup_output="$(
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    bash -s backup "$REMOTE_DB" < "$BACKUP_HELPER"
)"
printf '%s\n' "$backup_output"
deadman_abort_if_revoked
ANALYSIS_DB_SNAPSHOT="$(
  sed -n 's/^WEATHEREDGE_BACKUP_SNAPSHOT=//p' <<<"$backup_output" | tail -n 1
)"
if [[ ! "$ANALYSIS_DB_SNAPSHOT" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
  echo "verified backup did not return a conservative absolute snapshot path" >&2
  exit 1
fi
IFS='/' read -r -a ANALYSIS_DB_COMPONENTS <<<"${ANALYSIS_DB_SNAPSHOT#/}"
for component in "${ANALYSIS_DB_COMPONENTS[@]}"; do
  if [[ "$component" == "." || "$component" == ".." ]]; then
    echo "verified backup snapshot must not contain '.' or '..' path components" >&2
    exit 1
  fi
done

verify_deploy_source_unchanged "backup verification"

ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "sudo mkdir -p '$REMOTE_BASE/requirements' && sudo chown '$REMOTE_USER:$REMOTE_USER' '$REMOTE_BASE' '$REMOTE_BASE/requirements'"

# Source transfer starts here. From now on the remote tree may be mixed, so a
# failure must leave the host quiesced (see the pre-transfer recovery note).
# The box-side dead-man is told the same thing, with no timers at all: from here
# until the install is verified it may never enable one, only record and alert.
deadman_abort_if_revoked
deadman_phase mixed
PRE_TRANSFER_RECOVERY_ARMED=0
trap - EXIT HUP INT TERM
deadman_traps_cleared

# The sole Python manifest lives at the repository root and reads README.md
# while discovering the package below trading/. Send those build inputs before
# either installer runs; the package source itself is synced in the next rsync.
rsync -av \
  -e "ssh -i '$HOST_KEY' -o StrictHostKeyChecking=accept-new" \
  -- \
  "$WEATHEREDGE_ROOT/pyproject.toml" \
  "$WEATHEREDGE_ROOT/README.md" \
  "$REMOTE_USER@$HOST_IP:$REMOTE_BASE/"

rsync -av \
  -e "ssh -i '$HOST_KEY' -o StrictHostKeyChecking=accept-new" \
  -- \
  "$WEATHEREDGE_ROOT/requirements/production.lock" \
  "$REMOTE_USER@$HOST_IP:$REMOTE_BASE/requirements/production.lock"

rsync -av \
  -e "ssh -i '$HOST_KEY' -o StrictHostKeyChecking=accept-new" \
  --exclude-from="$FORECASTER_EXCLUDES" \
  "$LOCAL_FORECASTER_DIR/" \
  "$REMOTE_USER@$HOST_IP:$REMOTE_BASE/forecaster/"

rsync -av \
  -e "ssh -i '$HOST_KEY' -o StrictHostKeyChecking=accept-new" \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.DS_Store' \
  --exclude '.env' \
  --exclude '*.p8' \
  --exclude '*.pem' \
  --exclude '*.key' \
  --exclude '.venv' \
  --exclude '.venv-dev' \
  --exclude 'venv' \
  --exclude '*.egg-info' \
  --exclude 'data' \
  --exclude 'tmp_*' \
  "$LOCAL_TRADING_DIR/" \
  "$REMOTE_USER@$HOST_IP:$REMOTE_BASE/trading/"

# Full sync intentionally avoids broad --delete semantics because production
# runtime state shares these trees. Remove only the audited source paths retired
# by TP-12/FC-7, and only after every transfer above has succeeded.
REMOTE_RETIRED_PATHS=(
  "$REMOTE_BASE/trading/pyproject.toml"
  "$REMOTE_BASE/trading/sfo_kalshi_quant/sfo-dataset-backfill.service.in"
  "$REMOTE_BASE/trading/sfo_kalshi_quant/sfo-forecaster-refresh.service.in"
  "$REMOTE_BASE/forecaster/forecast_tomorrow.py"
  "$REMOTE_BASE/forecaster/load_to_db.py"
  "$REMOTE_BASE/forecaster/combine_psv.py"
  "$REMOTE_BASE/forecaster/eda.py"
  "$REMOTE_BASE/forecaster/lstm_model.py"
  "$REMOTE_BASE/forecaster/xgboost_model.py"
  "$REMOTE_BASE/forecaster/ab_test.py"
  "$REMOTE_BASE/forecaster/compare_models.py"
  "$REMOTE_BASE/forecaster/features.py"
  "$REMOTE_BASE/forecaster/forecast_validation.py"
  "$REMOTE_BASE/forecaster/fetch_inland_history.py"
)
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" rm -f -- "${REMOTE_RETIRED_PATHS[@]}"

verify_deploy_source_unchanged "source transfer"

# Audit PR-01: immutable build provenance. The host tree is an rsync copy with
# no .git, so the deployed source revision must be stamped at sync time; the
# publication manifest and the Pages commit message carry it onward so the
# public site can identify the exact source that generated its artifacts.
BUILD_INFO_TMP="$(mktemp)"
SOURCE_DIRTY=false
# Audit F-07: read the version constants literally instead of importing them.
# Importing required a local interpreter, and `python3` resolves to the Xcode
# system build (3.9) on macOS, which fails on `from datetime import UTC` and
# aborted the deploy *after* the box had already been quiesced. Both constants
# are module-level string literals, so a literal read needs no interpreter at
# all and cannot be broken by an unrelated import error elsewhere in the
# package. `test_deploy_provenance_versions_match_imported_constants` asserts
# these stay identical to the imported values, so a format change is caught.
read_source_version_constant() {
  local source_file="$1"
  local constant_name="$2"
  local value
  value="$(
    sed -n "s/^${constant_name} = \"\([^\"]*\)\"\$/\1/p" "$source_file" \
      | head -n 1
  )"
  if [[ -z "$value" ]]; then
    echo "could not read $constant_name from $source_file" >&2
    exit 1
  fi
  printf '%s' "$value"
}

EXECUTION_MODEL_VERSION="$(
  read_source_version_constant \
    "$WEATHEREDGE_ROOT/trading/sfo_kalshi_quant/maker_fills.py" \
    EXECUTION_MODEL_VERSION
)"
ACCOUNTING_POLICY_VERSION="$(
  read_source_version_constant \
    "$WEATHEREDGE_ROOT/trading/sfo_kalshi_quant/account.py" \
    ACCOUNTING_POLICY_VERSION
)"
cat > "$BUILD_INFO_TMP" <<JSON
{
  "source_sha": "$SOURCE_SHA",
  "source_dirty": $SOURCE_DIRTY,
  "synced_at_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "execution_model_version": "$EXECUTION_MODEL_VERSION",
  "accounting_policy_version": "$ACCOUNTING_POLICY_VERSION"
}
JSON
rsync -av \
  -e "ssh -i '$HOST_KEY' -o StrictHostKeyChecking=accept-new" \
  -- \
  "$BUILD_INFO_TMP" \
  "$REMOTE_USER@$HOST_IP:$REMOTE_BASE/forecaster/build_info.json"
rm -f "$BUILD_INFO_TMP"

# Render the transferred units and refresh the editable Python installation
# while every timer remains stopped. The timer-less installer is the deployment
# gate: any dependency, package, or unit failure exits here and leaves the host
# safely quiesced instead of restarting a partial tree.
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "cd '$REMOTE_BASE/trading' && bash deploy/aws/install_systemd_notimers.sh"
# This read-only gate catches runtime drop-ins and other effective-unit drift
# after daemon-reload. Any mismatch exits while all producer timers are still
# quiesced; operators must remove the drift explicitly before retrying.
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "cd '$REMOTE_BASE/trading' && bash deploy/aws/verify_systemd_unit_integrity.sh"
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "cd '$REMOTE_BASE/trading' && bash deploy/aws/create_decision_snapshot_index.sh"
# Retention indexes share that quiesced window. They must exist before the prune
# timer is restored: without them the bounded-subquery prune degenerates to a
# correlated full scan per candidate row and is slower than what it replaced.
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "cd '$REMOTE_BASE/trading' && bash deploy/aws/create_retention_indexes.sh"
# Initialize the restart-era account schema while every producer is quiesced.
# The Strategy builder is intentionally read-only, so this gate must run before
# any timer restoration or seed publication.
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "cd '$REMOTE_BASE/trading' && .venv/bin/python deploy/aws/validate_account_cutover.py --db '$REMOTE_DB'"

# From this point onward the transferred source and account cutover have passed
# their gates and producer restoration may begin. Any later exit must either
# restore the exact captured timer policy and release maintenance, or quiesce
# the host again while retaining the marker. This prevents a split-brain state
# where timers run but the independent scheduler watchdog remains suppressed.
RUNTIME_RECOVERY_REQUIRED=1
# Recovery can run during historical analysis, before the normal timer split
# below. Resolve the watchdog now so set -u cannot abort that recovery, while
# preserving an operator's deliberately disabled watchdog.
SCHEDULER_WATCHDOG_ENABLED=0
for timer in ${ENABLED_TIMERS[@]+"${ENABLED_TIMERS[@]}"}; do
  if [[ "$timer" == "sfo-scheduler-health.timer" ]]; then
    SCHEDULER_WATCHDOG_ENABLED=1
  fi
done
recover_deploy_runtime() {
  local interrupted_status="${1:-$?}"
  local restore_status=0
  local release_status=0
  trap - EXIT HUP INT TERM
  deadman_recovery_gate

  if (( RUNTIME_RECOVERY_REQUIRED == 1 )); then
    if (( ${#ENABLED_TIMERS[@]} > 0 )); then
      if ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
        bash -s restore "${ENABLED_TIMERS[@]}" < "$QUIESCE_HELPER"; then
        :
      else
        restore_status=$?
      fi
    fi
    if (( restore_status == 0 )); then
      if ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
        "sudo rm -f -- '$DEPLOY_MAINTENANCE_MARKER'"; then
        RUNTIME_RECOVERY_REQUIRED=0
        if (( SCHEDULER_WATCHDOG_ENABLED == 1 )); then
          ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
            "sudo systemctl start sfo-scheduler-health.service" \
            || echo "warning: scheduler health recovery run failed; its timer remains active" >&2
        fi
      else
        release_status=$?
      fi
    fi
    if (( RUNTIME_RECOVERY_REQUIRED == 1 )); then
      ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
        bash -s quiesce < "$QUIESCE_HELPER" \
        || echo "warning: failed to re-quiesce after deployment recovery failure" >&2
      if (( restore_status != 0 )); then
        echo "warning: failed to restore captured timers during deployment recovery (status=$restore_status)" >&2
      fi
      if (( release_status != 0 )); then
        echo "warning: failed to release deployment maintenance during recovery (status=$release_status)" >&2
      fi
    fi
  fi
  if (( RUNTIME_RECOVERY_REQUIRED == 0 )); then
    deadman_finalize_recovery disarm
  else
    # This recovery deliberately left the box quiesced with the marker. The
    # box-side dead-man is still in post-install and would re-enable every one
    # of those timers about a lease later, defeating the split-brain protection
    # it sits next to -- so it is moved to ATTENTION instead.
    deadman_finalize_recovery hold
  fi
  deadman_cleanup_fence
  exit "$interrupted_status"
}
trap 'recover_deploy_runtime 129' HUP
trap 'recover_deploy_runtime 130' INT
trap 'recover_deploy_runtime 143' TERM
trap 'recover_deploy_runtime $?' EXIT
deadman_traps_armed
# --- end deployment runtime recovery -----------------------------------------

# Only now is the box-side dead-man told that the install is verified.
# ENABLED_TIMERS is final here, so handing it over makes its recovery and
# recover_deploy_runtime's the same policy. The advance is an ssh round trip
# like any other and therefore sits INSIDE the trap's cover: run before the
# traps, a transient failure killed the deploy with no handler at all and left
# a fully installed, fully gated box quiesced behind a `mixed` dead-man that by
# design never restores a timer.
deadman_abort_if_revoked
deadman_phase post-install ${ENABLED_TIMERS[@]+"${ENABLED_TIMERS[@]}"}

# Historical analysis is diagnostic and the frequent builder has an explicit
# deferred state. Run it while deployment maintenance still holds every
# producer: the verified snapshot may temporarily push the runtime volume above
# its normal disk-health threshold, so it must be consumed and removed before
# any producer or freshness check is restored. The helper reads the immutable,
# integrity-checked deploy snapshot instead of the live journal.
ANALYSIS_CACHE_REFRESHED=0
if ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "cd '$REMOTE_BASE/trading' && SFO_STRATEGY_ANALYSIS_DB_PATH='$ANALYSIS_DB_SNAPSHOT' bash deploy/aws/refresh_strategy_analysis_cache.sh"; then
  ANALYSIS_CACHE_REFRESHED=1
else
  echo "warning: historical Strategy Lab cache refresh failed; continuing with deferred analysis" >&2
  # The helper uses a stable transient-unit name and normally cleans it up via
  # its trap. This best-effort second guard covers an abruptly dropped SSH
  # transport before the remote shell can run that trap.
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    "sudo systemctl stop weatheredge-strategy-analysis-cache.service >/dev/null 2>&1 || true; sudo systemctl reset-failed weatheredge-strategy-analysis-cache.service >/dev/null 2>&1 || true" \
    || echo "warning: could not confirm Strategy Lab analysis unit cleanup" >&2
fi

# The analysis refresh was the last consumer of the verified snapshot. Drop it
# before restoring runtime health checks. It is redundant because this deploy
# already round-tripped it through S3 and re-verified the download.
if [[ -n "$ANALYSIS_DB_SNAPSHOT" ]]; then
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    "rm -f -- '$ANALYSIS_DB_SNAPSHOT'" \
    || echo "warning: could not remove verified local snapshot $ANALYSIS_DB_SNAPSHOT" >&2
fi

# Restore producers first, seed and validate one complete publication, then
# restore the persistent watchdog last so it cannot race the first fresh build.
PRODUCER_TIMERS=()
WATCHDOG_ENABLED=0
SCHEDULER_WATCHDOG_ENABLED=0
PUBLISH_TIMER_ENABLED=0
STRATEGY_TIMER_ENABLED=0
for timer in ${ENABLED_TIMERS[@]+"${ENABLED_TIMERS[@]}"}; do
  if [[ "$timer" == "sfo-scheduler-health.timer" ]]; then
    SCHEDULER_WATCHDOG_ENABLED=1
  elif [[ "$timer" == "sfo-forecast-freshness.timer" ]]; then
    WATCHDOG_ENABLED=1
  elif [[ "$timer" == "sfo-operational-publish.timer" ]]; then
    # Keep the recurring publisher stopped until the one deploy-seed snapshot
    # is visible publicly. Otherwise the five-minute timer changes the local
    # manifest while the propagation waiter is checking the prior snapshot.
    PUBLISH_TIMER_ENABLED=1
  elif [[ "$timer" == "sfo-strategy-lab-refresh.timer" ]]; then
    # Strategy cycles also rebuild the global manifest. Hold this timer with
    # the publisher until the explicit seed has propagated.
    STRATEGY_TIMER_ENABLED=1
  else
    PRODUCER_TIMERS+=("$timer")
  fi
done
if (( ${#PRODUCER_TIMERS[@]} > 0 )); then
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    bash -s restore "${PRODUCER_TIMERS[@]}" < "$QUIESCE_HELPER"
fi
INITIAL_HELD_TIMERS=()
if (( STRATEGY_TIMER_ENABLED == 1 )); then
  INITIAL_HELD_TIMERS+=("sfo-strategy-lab-refresh.timer")
fi
if (( PUBLISH_TIMER_ENABLED == 1 )); then
  INITIAL_HELD_TIMERS+=("sfo-operational-publish.timer")
fi
INITIAL_RESTORE_REQUIRED=1
restore_initial_timers() {
  local include_watchdog="${1:-0}"
  local restore_status=0
  local timers=()
  local timer=""
  for timer in ${INITIAL_HELD_TIMERS[@]+"${INITIAL_HELD_TIMERS[@]}"}; do
    timers+=("$timer")
  done
  if (( include_watchdog == 1 && WATCHDOG_ENABLED == 1 )); then
    timers+=("sfo-forecast-freshness.timer")
  fi
  if (( include_watchdog == 1 && SCHEDULER_WATCHDOG_ENABLED == 1 )); then
    timers+=("sfo-scheduler-health.timer")
  fi
  if (( INITIAL_RESTORE_REQUIRED == 1 )); then
    if (( ${#timers[@]} == 0 )); then
      INITIAL_RESTORE_REQUIRED=0
    elif ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
      bash -s restore "${timers[@]}" < "$QUIESCE_HELPER"; then
      INITIAL_RESTORE_REQUIRED=0
    else
      restore_status=$?
    fi
  fi
  return "$restore_status"
}
INITIAL_SEED_STATUS=0
if ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "sudo systemctl start sfo-strategy-lab-refresh.service && sudo systemctl start sfo-operational-publish.service"; then
  if ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    "cd '$REMOTE_BASE/trading' && bash deploy/aws/wait_for_publication_manifest.sh"; then
    :
  else
    INITIAL_SEED_STATUS=$?
  fi
else
  INITIAL_SEED_STATUS=$?
fi
INITIAL_RESTART_STATUS=0
if restore_initial_timers "$(( INITIAL_SEED_STATUS != 0 ))"; then
  :
else
  INITIAL_RESTART_STATUS=$?
fi
if (( INITIAL_SEED_STATUS != 0 )); then
  echo "initial Strategy Lab publication failed (status=$INITIAL_SEED_STATUS)" >&2
fi
if (( INITIAL_RESTART_STATUS != 0 )); then
  echo "failed to restore held deployment timers after the initial publication (status=$INITIAL_RESTART_STATUS)" >&2
fi
if (( INITIAL_SEED_STATUS != 0 )); then
  exit "$INITIAL_SEED_STATUS"
fi
if (( INITIAL_RESTART_STATUS != 0 )); then
  exit "$INITIAL_RESTART_STATUS"
fi
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "sudo systemctl start sfo-forecast-freshness.service"
if (( WATCHDOG_ENABLED == 1 )); then
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    bash -s restore sfo-forecast-freshness.timer < "$QUIESCE_HELPER"
fi

# A successful full analysis only updates the private cache. Rebuild the
# bounded public artifact from that cache, publish it, and wait for the exact
# immutable manifest captured by the waiter so the deploy does not finish with
# a fresh cache but a deferred public Strategy Lab.
if (( ANALYSIS_CACHE_REFRESHED == 1 )); then
  POST_ANALYSIS_TIMERS=()
  if (( STRATEGY_TIMER_ENABLED == 1 )); then
    POST_ANALYSIS_TIMERS+=("sfo-strategy-lab-refresh.timer")
  fi
  if (( PUBLISH_TIMER_ENABLED == 1 )); then
    POST_ANALYSIS_TIMERS+=("sfo-operational-publish.timer")
  fi
  POST_ANALYSIS_RESTORE_REQUIRED=0
  restore_post_analysis_timers() {
    local restore_status=0
    if (( POST_ANALYSIS_RESTORE_REQUIRED == 1 )) \
      && (( ${#POST_ANALYSIS_TIMERS[@]} > 0 )); then
      if ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
        bash -s restore "${POST_ANALYSIS_TIMERS[@]}" < "$QUIESCE_HELPER"; then
        POST_ANALYSIS_RESTORE_REQUIRED=0
      else
        restore_status=$?
      fi
    fi
    return "$restore_status"
  }
  POST_ANALYSIS_STATUS=0
  if (( ${#POST_ANALYSIS_TIMERS[@]} > 0 )); then
    # Prevent either recurring writer from racing cache promotion, the exact
    # Strategy rebuild, or its publication. Let already-running units finish
    # instead of terminating a Python write or git push midway through.
    POST_ANALYSIS_RESTORE_REQUIRED=1
    if ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
      "sudo systemctl stop sfo-strategy-lab-refresh.timer sfo-operational-publish.timer && timeout 910 bash -c 'while systemctl is-active --quiet sfo-strategy-lab-refresh.service || systemctl is-active --quiet sfo-operational-publish.service; do sleep 1; done'"; then
      :
    else
      POST_ANALYSIS_STATUS=$?
    fi
  fi
  if (( POST_ANALYSIS_STATUS == 0 )); then
    if ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
      "sudo systemctl start sfo-strategy-lab-refresh.service && sudo systemctl start sfo-operational-publish.service"; then
      if ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
        "cd '$REMOTE_BASE/trading' && bash deploy/aws/wait_for_publication_manifest.sh"; then
        :
      else
        POST_ANALYSIS_STATUS=$?
      fi
    else
      POST_ANALYSIS_STATUS=$?
    fi
  fi
  POST_ANALYSIS_RESTART_STATUS=0
  if restore_post_analysis_timers; then
    :
  else
    POST_ANALYSIS_RESTART_STATUS=$?
  fi
  if (( POST_ANALYSIS_STATUS != 0 )); then
    echo "post-analysis Strategy Lab publication failed (status=$POST_ANALYSIS_STATUS)" >&2
  fi
  if (( POST_ANALYSIS_RESTART_STATUS != 0 )); then
    echo "failed to restore recurring Strategy Lab/publication timers after post-analysis publication (status=$POST_ANALYSIS_RESTART_STATUS)" >&2
  fi
  if (( POST_ANALYSIS_STATUS != 0 )); then
    exit "$POST_ANALYSIS_STATUS"
  fi
  if (( POST_ANALYSIS_RESTART_STATUS != 0 )); then
    exit "$POST_ANALYSIS_RESTART_STATUS"
  fi
fi

if (( SCHEDULER_WATCHDOG_ENABLED == 1 )); then
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    bash -s restore sfo-scheduler-health.timer < "$QUIESCE_HELPER"
fi
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "sudo rm -f -- '$DEPLOY_MAINTENANCE_MARKER'"
RUNTIME_RECOVERY_REQUIRED=0
trap - EXIT HUP INT TERM
deadman_traps_cleared
if (( SCHEDULER_WATCHDOG_ENABLED == 1 )); then
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    "sudo systemctl start sfo-scheduler-health.service"
fi

# Heartbeat first, so a beat cannot race the disarm. A failed disarm is a
# warning, not a deploy failure: the deploy has already succeeded, and the worst
# case is a post-install dead-man that later idempotently re-enables timers that
# are already enabled and then removes itself.
deadman_stop_heartbeat
deadman_disarm \
  || echo "warning: dead-man disarm failed; run 'sudo $DEADMAN_BIN clear --force' on the box" >&2
deadman_cleanup_fence

echo "Synced root packaging inputs, forecaster, and trading source to $REMOTE_USER@$HOST_IP:$REMOTE_BASE"
echo "Local source: $WEATHEREDGE_ROOT"
echo "Restored ${#PRODUCER_TIMERS[@]} producer timer(s); watchdog restored last=$WATCHDOG_ENABLED."
echo "Scheduler watchdog restored after maintenance=$SCHEDULER_WATCHDOG_ENABLED."
echo "Historical Strategy Lab cache refreshed=$ANALYSIS_CACHE_REFRESHED."
