#!/usr/bin/env bash
# Box-side deploy dead-man restore.
#
# sync_to_box.sh arms this on the box before it quiesces anything, and disarms
# it when the deploy finishes. Because it lives on the box it survives the
# deploy host vanishing -- a sleeping laptop, a changed IP, a dead SSH session --
# which is how production sat dark for 46.8 h from 2026-09-13T01:22Z and 22.3 h
# from 2026-09-15T02:32Z. The host's own bash traps cannot reach a box they can
# no longer connect to; this can.
#
# It is streamed onto a box that may have nothing of this release installed, so
# it is self-contained: no sourcing, no interpreter, nothing from /opt. The only
# other file it needs is the deploy's own disable_systemd_timers.sh, pinned
# beside its state at arm time, which stays the sole allowlist of unit names.
#
# Verbs: arm | beat | tick | status | clear | disarm | hold
# Exit:  0 ok | 2 usage | 11 another deploy or an unacknowledged incident owns
#        the box | 12 environment unusable / no trigger ticked / a state write
#        failed | 13 armed but no trigger has ticked recently | 14 revoked
#        (state gone, or it fired)
#
# `set -f` is deliberate. TIMERS is split on whitespace in several places and
# bash would also glob those words against the trigger's working directory
# (root's home for cron, / for the oneshot). Nothing here wants pathname
# expansion, so it is off everywhere and a damaged state file can never turn
# into an accidentally matched unit name.
set -euo pipefail
set -f
# A missing `logger` would otherwise reap the right-hand side of the cron
# pipeline and kill the payload with SIGPIPE part-way through a teardown.
trap '' PIPE

DEADMAN_VERSION="1"

DEADMAN_SELF_PATH="${BASH_SOURCE[0]}"
DEADMAN_STATE_DIR="${DEADMAN_STATE_DIR:-/var/lib/weatheredge/deploy-deadman}"
DEADMAN_SELF="${DEADMAN_SELF:-/usr/local/libexec/weatheredge/deploy_deadman.sh}"
DEADMAN_CRON_FILE="${DEADMAN_CRON_FILE:-/etc/cron.d/weatheredge-deploy-deadman}"
DEADMAN_UNIT_DIR="${DEADMAN_UNIT_DIR:-/etc/systemd/system}"
DEADMAN_LOCK_FILE="${DEADMAN_LOCK_FILE:-/run/weatheredge-deploy-deadman.lock}"
DEADMAN_MARKER="${DEADMAN_MARKER:-/run/weatheredge-deploy-maintenance}"
DEADMAN_BOOT_ID_FILE="${DEADMAN_BOOT_ID_FILE:-/proc/sys/kernel/random/boot_id}"
DEADMAN_REMOTE_BASE="${DEADMAN_REMOTE_BASE:-/opt/weatheredge}"
DEADMAN_TICK_STALE_SECONDS="${DEADMAN_TICK_STALE_SECONDS:-300}"
DEADMAN_LOGGER="${DEADMAN_LOGGER:-/usr/bin/logger}"
FLOCK_BIN="${FLOCK_BIN:-/usr/bin/flock}"
# The payload always runs as root (cron's root entry, a root oneshot unit, or
# `sudo deploy_deadman.sh` from the deploy host), so it needs no sudo of its own.
SYSTEMCTL="${SYSTEMCTL_BIN:-/bin/systemctl}"

DEADMAN_UNIT_NAME="weatheredge-deploy-deadman"
DEADMAN_ALERT_UNIT="sfo-alert@weatheredge-deploy-deadman.service"
DEADMAN_MAX_RESTORE_ATTEMPTS=5
DEADMAN_MAX_TRIGGER_REMOVALS=5
DEADMAN_MAX_TIMERS=32
DEADMAN_REVOKED_NOTE="this deploy is revoked; before re-deploying run trading/deploy/aws/README.md 'Release deploy and rollback' phase 0."

STATE_FILE="$DEADMAN_STATE_DIR/state"
HEARTBEAT_FILE="$DEADMAN_STATE_DIR/heartbeat"
LAST_TICK_FILE="$DEADMAN_STATE_DIR/last-tick"
MARKER_SEEN_FILE="$DEADMAN_STATE_DIR/marker-seen"
RESTORE_ATTEMPTS_FILE="$DEADMAN_STATE_DIR/restore-attempts"
# rsync -a preserves the source mtime, so "is any .py newer than build_info.json"
# cannot see a partial transfer. A file rsync writes does get a fresh ctime, and
# this stamp is the reference the pre-transfer probe compares that ctime against.
ARM_STAMP_FILE="$DEADMAN_STATE_DIR/arm-stamp"
TRIGGER_FAILURES_FILE="$DEADMAN_STATE_DIR/trigger-removal-failures"
LAST_ACTION_FILE="$DEADMAN_STATE_DIR/last-action"
PINNED_HELPER="$DEADMAN_STATE_DIR/disable_systemd_timers.sh"
TIMER_UNIT="$DEADMAN_UNIT_DIR/$DEADMAN_UNIT_NAME.timer"
SERVICE_UNIT="$DEADMAN_UNIT_DIR/$DEADMAN_UNIT_NAME.service"

STATE_VERSION=""
STATE_DEPLOY_ID=""
STATE_PHASE=""
STATE_PRE_TRANSFER_RESTORE=""
STATE_SOURCE_SHA=""
STATE_DEPLOY_HOST=""
STATE_ARMED_AT_EPOCH=""
STATE_ARMED_AT_UTC=""
STATE_ARMED_BOOT_ID=""
STATE_PHASE_AT_EPOCH=""
STATE_LEASE_SECONDS=""
STATE_TIMERS=""

usage_error() {
  echo "$1" >&2
  echo "usage: $0 [arm ...|beat <deploy-id>|tick <cron|systemd>|status|clear [--force]|disarm <deploy-id>|hold <deploy-id>]" >&2
  exit 2
}

now_epoch() { date +%s; }
now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Same-directory temp plus rename: a reader sees the old content or the new one,
# never a torn one, and never a half-written unit name.
write_atomic() {
  local target="$1"
  cat > "$target.new"
  chmod 0644 "$target.new"
  mv -f "$target.new" "$target"
}

valid_timer() { [[ "$1" =~ ^[A-Za-z0-9@._-]+\.timer$ ]]; }
valid_deploy_id() { [[ "$1" =~ ^[A-Za-z0-9._-]{8,64}$ ]]; }
valid_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }
valid_token() { [[ "$1" =~ ^[A-Za-z0-9._-]{1,128}$ ]]; }

read_boot_id() {
  if [[ -r "$DEADMAN_BOOT_ID_FILE" ]]; then
    head -n 1 "$DEADMAN_BOOT_ID_FILE" 2>/dev/null || true
  fi
}

# check_scheduler_health.sh makes the same accommodation: no flock, no locking.
# Every verb is idempotent, so the lock is a politeness, not a safety property.
DEADMAN_LOCK_HELD=0
acquire_lock() {
  local mode="$1"
  command -v "$FLOCK_BIN" >/dev/null 2>&1 || return 0
  ( : >> "$DEADMAN_LOCK_FILE" ) 2>/dev/null || return 0
  exec 9>>"$DEADMAN_LOCK_FILE"
  DEADMAN_LOCK_HELD=1
  if [[ "$mode" == "nonblock" ]]; then
    "$FLOCK_BIN" -n 9 || return 1
  else
    "$FLOCK_BIN" -w 120 9 || return 1
  fi
  return 0
}

# Closing a descriptor that was never opened is a redirection error, which is
# fatal for `exec`; only close what acquire_lock actually opened.
release_lock() {
  if (( DEADMAN_LOCK_HELD == 1 )); then
    exec 9>&-
    DEADMAN_LOCK_HELD=0
  fi
}

log_line() { printf '%s\n' "$*" 2>/dev/null || true; }

parse_state() {
  local line=""
  local key=""
  local value=""
  local timer=""
  local count=0
  [[ -f "$STATE_FILE" ]] || return 1
  STATE_VERSION=""
  STATE_DEPLOY_ID=""
  STATE_PHASE=""
  STATE_PRE_TRANSFER_RESTORE=""
  STATE_SOURCE_SHA=""
  STATE_DEPLOY_HOST=""
  STATE_ARMED_AT_EPOCH=""
  STATE_ARMED_AT_UTC=""
  STATE_ARMED_BOOT_ID=""
  STATE_PHASE_AT_EPOCH=""
  STATE_LEASE_SECONDS=""
  STATE_TIMERS=""
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -n "$line" ]] || continue
    [[ "$line" == *=* ]] || return 1
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      DEADMAN_VERSION) STATE_VERSION="$value" ;;
      DEPLOY_ID) STATE_DEPLOY_ID="$value" ;;
      PHASE) STATE_PHASE="$value" ;;
      PRE_TRANSFER_RESTORE) STATE_PRE_TRANSFER_RESTORE="$value" ;;
      SOURCE_SHA) STATE_SOURCE_SHA="$value" ;;
      DEPLOY_HOST) STATE_DEPLOY_HOST="$value" ;;
      ARMED_AT_EPOCH) STATE_ARMED_AT_EPOCH="$value" ;;
      ARMED_AT_UTC) STATE_ARMED_AT_UTC="$value" ;;
      ARMED_BOOT_ID) STATE_ARMED_BOOT_ID="$value" ;;
      PHASE_AT_EPOCH) STATE_PHASE_AT_EPOCH="$value" ;;
      LEASE_SECONDS) STATE_LEASE_SECONDS="$value" ;;
      TIMERS) STATE_TIMERS="$value" ;;
      *) return 1 ;;
    esac
  done < "$STATE_FILE"
  [[ "$STATE_VERSION" == "$DEADMAN_VERSION" ]] || return 1
  valid_deploy_id "$STATE_DEPLOY_ID" || return 1
  case "$STATE_PHASE" in
    pre-transfer|mixed|post-install|attention) ;;
    *) return 1 ;;
  esac
  case "$STATE_PRE_TRANSFER_RESTORE" in 0|1) ;; *) return 1 ;; esac
  valid_token "$STATE_SOURCE_SHA" || return 1
  valid_token "$STATE_DEPLOY_HOST" || return 1
  valid_uint "$STATE_ARMED_AT_EPOCH" || return 1
  valid_uint "$STATE_PHASE_AT_EPOCH" || return 1
  valid_uint "$STATE_LEASE_SECONDS" || return 1
  [[ -n "$STATE_ARMED_AT_UTC" ]] || return 1
  for timer in $STATE_TIMERS; do
    valid_timer "$timer" || return 1
    count=$((count + 1))
  done
  (( count <= DEADMAN_MAX_TIMERS )) || return 1
  return 0
}

heartbeat_epoch() {
  local value=""
  if [[ -f "$HEARTBEAT_FILE" ]]; then
    value="$(head -n 1 "$HEARTBEAT_FILE" 2>/dev/null || true)"
    value="${value%% *}"
  fi
  if valid_uint "${value:-}"; then
    printf '%s' "$value"
  else
    printf '0'
  fi
}

last_tick_epoch() {
  local value=""
  if [[ -f "$LAST_TICK_FILE" ]]; then
    value="$(head -n 1 "$LAST_TICK_FILE" 2>/dev/null || true)"
    value="${value%% *}"
  fi
  if valid_uint "${value:-}"; then
    printf '%s' "$value"
  else
    printf '0'
  fi
}

last_tick_source() {
  local value=""
  if [[ -f "$LAST_TICK_FILE" ]]; then
    value="$(head -n 1 "$LAST_TICK_FILE" 2>/dev/null || true)"
    value="${value#* }"
  fi
  printf '%s' "${value:-none}"
}

restore_attempts() {
  local value=""
  if [[ -f "$RESTORE_ATTEMPTS_FILE" ]]; then
    value="$(head -n 1 "$RESTORE_ATTEMPTS_FILE" 2>/dev/null || true)"
  fi
  if valid_uint "${value:-}"; then
    printf '%s' "$value"
  else
    printf '0'
  fi
}

trigger_removal_failures() {
  local value=""
  if [[ -f "$TRIGGER_FAILURES_FILE" ]]; then
    value="$(head -n 1 "$TRIGGER_FAILURES_FILE" 2>/dev/null || true)"
  fi
  if valid_uint "${value:-}"; then
    printf '%s' "$value"
  else
    printf '0'
  fi
}

cron_armable() {
  local dir=""
  dir="$(dirname "$DEADMAN_CRON_FILE")"
  [[ -d "$dir" ]]
}

systemd_armable() { command -v "$SYSTEMCTL" >/dev/null 2>&1; }

write_cron_trigger() {
  local sink="2>&1 | $DEADMAN_LOGGER -t weatheredge-deploy-deadman"
  if [[ -z "$DEADMAN_LOGGER" ]]; then
    sink=">/dev/null 2>&1"
  fi
  write_atomic "$DEADMAN_CRON_FILE" <<CRON
# WeatherEdge deploy dead-man restore. Armed by trading/deploy/aws/sync_to_box.sh
# at quiesce time and removed when the deploy finishes or when it fires.
# Incident inspection:
#   sudo $DEADMAN_SELF status
#   cat $LAST_ACTION_FILE
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
* * * * * root $DEADMAN_SELF tick cron $sink
CRON
  chown root:root "$DEADMAN_CRON_FILE" 2>/dev/null || true
  chmod 0644 "$DEADMAN_CRON_FILE"
}

write_systemd_trigger() {
  write_atomic "$SERVICE_UNIT" <<UNIT
[Unit]
Description=WeatherEdge deploy dead-man restore poll
ConditionPathExists=$STATE_FILE

[Service]
Type=oneshot
TimeoutStartSec=180
ExecStart=$DEADMAN_SELF tick systemd
UNIT
  write_atomic "$TIMER_UNIT" <<UNIT
[Unit]
Description=WeatherEdge deploy dead-man restore poll timer

[Timer]
OnBootSec=1min
OnActiveSec=1min
OnUnitActiveSec=1min
AccuracySec=5s
Unit=${DEADMAN_UNIT_NAME}.service

[Install]
WantedBy=timers.target
UNIT
  "$SYSTEMCTL" daemon-reload >/dev/null 2>&1 || return 1
  "$SYSTEMCTL" enable --now "$DEADMAN_UNIT_NAME.timer" >/dev/null 2>&1 || return 1
  return 0
}

remove_triggers() {
  local remaining=0
  local wants="$DEADMAN_UNIT_DIR/timers.target.wants/$DEADMAN_UNIT_NAME.timer"
  rm -f "$DEADMAN_CRON_FILE" 2>/dev/null || true
  if [[ -e "$TIMER_UNIT" || -e "$SERVICE_UNIT" || -e "$wants" || -L "$wants" ]]; then
    "$SYSTEMCTL" disable --now "$DEADMAN_UNIT_NAME.timer" >/dev/null 2>&1 || true
    # `disable` is best effort, so remove the enablement symlink by hand too:
    # a dangling timers.target.wants link survives reboots and makes every
    # later daemon-reload on production log a unit that no longer exists.
    rm -f "$wants" 2>/dev/null || true
    rm -f "$TIMER_UNIT" "$SERVICE_UNIT" 2>/dev/null || true
    "$SYSTEMCTL" daemon-reload >/dev/null 2>&1 || true
  fi
  if [[ -e "$DEADMAN_CRON_FILE" || -e "$TIMER_UNIT" || -e "$SERVICE_UNIT" \
    || -e "$wants" || -L "$wants" ]]; then
    remaining=1
  fi
  return "$remaining"
}

remove_runtime_state() {
  rm -f \
    "$STATE_FILE" \
    "$HEARTBEAT_FILE" \
    "$LAST_TICK_FILE" \
    "$MARKER_SEEN_FILE" \
    "$RESTORE_ATTEMPTS_FILE" \
    "$TRIGGER_FAILURES_FILE" \
    "$ARM_STAMP_FILE" \
    2>/dev/null || true
}

# The incident record. It outlives the triggers, the state and the payload,
# because it is the only durable explanation an operator gets for a box that
# either came back on its own or deliberately stayed dark.
write_last_action() {
  local outcome="$1"
  local marker_disposition="$2"
  local probe="$3"
  local probe_detail="$4"
  local trigger="$5"
  local detail="$6"
  local heartbeat_age="-1"
  local heartbeat=""
  heartbeat="$(heartbeat_epoch)"
  if [[ "$heartbeat" != "0" ]]; then
    heartbeat_age=$(( $(now_epoch) - heartbeat ))
  fi
  # Never fatal. If /var cannot be written the restore has already happened by
  # the time this runs, and losing the record must not lose the restore.
  write_atomic "$LAST_ACTION_FILE" <<RECORD || log_line "warning: could not write $LAST_ACTION_FILE"
outcome=$outcome
phase=$STATE_PHASE
deploy_id=$STATE_DEPLOY_ID
deploy_host=$STATE_DEPLOY_HOST
source_sha=$STATE_SOURCE_SHA
armed_at_utc=$STATE_ARMED_AT_UTC
acted_at_utc=$(now_utc)
acted_at_epoch=$(now_epoch)
heartbeat_age_seconds=$heartbeat_age
lease_seconds=$STATE_LEASE_SECONDS
trigger=$trigger
marker=$marker_disposition
probe=$probe
probe_detail=$probe_detail
failed_restore_attempts=$(restore_attempts)
timers=$STATE_TIMERS
detail=$detail
note=$DEADMAN_REVOKED_NOTE
RECORD
}

mark_state_attention() {
  write_atomic "$STATE_FILE" <<STATE || log_line "warning: could not record PHASE=attention in $STATE_FILE"
DEADMAN_VERSION=$STATE_VERSION
DEPLOY_ID=$STATE_DEPLOY_ID
PHASE=attention
PRE_TRANSFER_RESTORE=$STATE_PRE_TRANSFER_RESTORE
SOURCE_SHA=$STATE_SOURCE_SHA
DEPLOY_HOST=$STATE_DEPLOY_HOST
ARMED_AT_EPOCH=$STATE_ARMED_AT_EPOCH
ARMED_AT_UTC=$STATE_ARMED_AT_UTC
ARMED_BOOT_ID=$STATE_ARMED_BOOT_ID
PHASE_AT_EPOCH=$STATE_PHASE_AT_EPOCH
LEASE_SECONDS=$STATE_LEASE_SECONDS
TIMERS=$STATE_TIMERS
STATE
}

# ATTENTION: act once, stay quiesced, keep everything an operator needs.
# The alert is best effort -- sfo-alert@.service points into the tree that may
# be mid-rsync, and SFO_FRESHNESS_ALERT_URL is very likely unset on this box.
# The durable signals are last-action, the journal tag, and the retained
# maintenance marker, which makes the next deploy refuse on its own.
raise_attention() {
  local outcome="$1"
  local marker_disposition="$2"
  local probe="$3"
  local probe_detail="$4"
  local trigger="$5"
  local detail="$6"
  write_last_action "$outcome" "$marker_disposition" "$probe" "$probe_detail" "$trigger" "$detail"
  mark_state_attention
  remove_triggers || log_line "warning: could not remove every dead-man trigger"
  "$SYSTEMCTL" start "$DEADMAN_ALERT_UNIT" >/dev/null 2>&1 || true
  log_line "ATTENTION $outcome: $detail"
  log_line "read: sudo $DEADMAN_SELF status; cat $LAST_ACTION_FILE"
}

# RESTORED / stood down: leave nothing behind but the record.
teardown_and_exit() {
  local outcome="$1"
  local marker_disposition="$2"
  local probe="$3"
  local probe_detail="$4"
  local trigger="$5"
  local detail="$6"
  write_last_action "$outcome" "$marker_disposition" "$probe" "$probe_detail" "$trigger" "$detail"
  if ! remove_triggers; then
    # Never leave a payload that can fire again with no trigger file to
    # explain it: downgrade to an incident instead.
    raise_attention "attention-trigger-removal-failed" "$marker_disposition" \
      "$probe" "$probe_detail" "$trigger" \
      "could not remove the dead-man cron entry or systemd unit pair"
    exit 0
  fi
  remove_runtime_state
  log_line "$outcome: $detail"
  exec /bin/rm -f -- "$PINNED_HELPER" "$DEADMAN_SELF"
}

timers_already_good() {
  local timer=""
  for timer in $STATE_TIMERS; do
    "$SYSTEMCTL" is-enabled --quiet "$timer" || return 1
    "$SYSTEMCTL" is-active --quiet "$timer" || return 1
  done
  return 0
}

state_has_timer() {
  local wanted="$1"
  local timer=""
  for timer in $STATE_TIMERS; do
    [[ "$timer" != "$wanted" ]] || return 0
  done
  return 1
}

# Does the tree look half-synced? Two independent questions, because neither
# alone is enough:
#
#   1. Has any source file been WRITTEN since this deploy armed? rsync -a
#      preserves the source mtime, so a transferred file can easily look older
#      than the release stamp -- but rsync writes a temp file and renames it, so
#      its ctime is the transfer time. Comparing ctime against the arm stamp is
#      therefore a positive, mtime-proof detector of this deploy's own partial
#      transfer, which is the case this probe exists for.
#   2. The runbook's own phase-0.2 question: does any source file post-date the
#      release stamp? That can still catch a tree left mixed by an EARLIER
#      deploy, which question 1 cannot see.
#
# Either answer being yes means mixed.
probe_pre_transfer_tree() {
  local build_info="$DEADMAN_REMOTE_BASE/forecaster/build_info.json"
  local newer=""
  if [[ -f "$ARM_STAMP_FILE" ]]; then
    newer="$(
      find \
        "$DEADMAN_REMOTE_BASE/trading/sfo_kalshi_quant" \
        "$DEADMAN_REMOTE_BASE/forecaster" \
        -name '*.py' -newercm "$ARM_STAMP_FILE" -print 2>/dev/null \
        | head -n 5 || true
    )"
    if [[ -n "$newer" ]]; then
      printf 'written-since-arm:%s' "$(head -n 1 <<<"$newer")"
      return 0
    fi
  fi
  if [[ ! -f "$build_info" ]]; then
    printf 'inconclusive'
    return 0
  fi
  newer="$(
    find \
      "$DEADMAN_REMOTE_BASE/trading/sfo_kalshi_quant" \
      "$DEADMAN_REMOTE_BASE/forecaster" \
      -name '*.py' -newer "$build_info" -print 2>/dev/null \
      | head -n 5 || true
  )"
  if [[ -n "$newer" ]]; then
    printf 'mixed:%s' "$(head -n 1 <<<"$newer")"
  else
    printf 'clean'
  fi
}

perform_restore() {
  local trigger="$1"
  local probe="$2"
  local probe_detail="$3"
  local outcome="$4"
  local attempts=0
  local restore_status=0
  local -a timers=()
  local timer=""
  for timer in $STATE_TIMERS; do
    timers+=("$timer")
  done

  attempts="$(restore_attempts)"
  if (( attempts >= DEADMAN_MAX_RESTORE_ATTEMPTS )); then
    raise_attention "attention-restore-exhausted" "kept" "$probe" "$probe_detail" \
      "$trigger" "restore failed $attempts time(s); the box stays quiesced"
    return 0
  fi

  if (( ${#timers[@]} == 0 )); then
    :
  elif timers_already_good; then
    probe_detail="$probe_detail nothing-to-restore"
  else
    SYSTEMCTL_BIN="$SYSTEMCTL" bash "$PINNED_HELPER" restore "${timers[@]}" \
      || restore_status=$?
  fi

  if (( restore_status != 0 )); then
    attempts=$((attempts + 1))
    printf '%s\n' "$attempts" | write_atomic "$RESTORE_ATTEMPTS_FILE"
    log_line "warning: dead-man restore failed (status=$restore_status, attempt $attempts of $DEADMAN_MAX_RESTORE_ATTEMPTS); keeping the box quiesced and retrying on the next tick"
    return 0
  fi

  rm -f "$DEADMAN_MARKER" 2>/dev/null || true
  if state_has_timer "sfo-scheduler-health.timer"; then
    "$SYSTEMCTL" start sfo-scheduler-health.service >/dev/null 2>&1 \
      || log_line "warning: scheduler health run after the dead-man restore failed; its timer is active"
  fi
  teardown_and_exit "$outcome" "removed" "$probe" "$probe_detail" "$trigger" \
    "restored ${#timers[@]} timer(s) and released deployment maintenance"
}

fire_pre_transfer() {
  local trigger="$1"
  local probe=""
  local detail=""
  if [[ "$STATE_PRE_TRANSFER_RESTORE" != "1" ]]; then
    # Exactly the host-side rule: a host that was already stranded, or a
    # genuinely new one, is never auto-restored.
    raise_attention "attention-not-restorable" "kept" "not-probed" "" "$trigger" \
      "the deploy recorded PRE_TRANSFER_RESTORE=0, so this host is never auto-restored"
    return 0
  fi
  probe="$(probe_pre_transfer_tree)"
  case "$probe" in
    written-since-arm:*)
      detail="${probe#written-since-arm:}"
      raise_attention "attention-mixed-tree" "kept" "written-since-arm" "$detail" "$trigger" \
        "a source file was written after this deploy armed, so a transfer landed and the tree may be half-synced: $detail"
      return 0
      ;;
    mixed:*)
      detail="${probe#mixed:}"
      raise_attention "attention-mixed-tree" "kept" "mixed" "$detail" "$trigger" \
        "a source file post-dates build_info.json, so the tree may be half-synced: $detail"
      return 0
      ;;
    inconclusive)
      # build_info.json is missing, so no rsync of this deploy landed either.
      # The failure being eliminated is *dark*, so only a positive mixed-tree
      # signal may block a restore.
      perform_restore "$trigger" "inconclusive" "build_info.json absent" "restored-pre-transfer"
      ;;
    *)
      perform_restore "$trigger" "clean" "" "restored-pre-transfer"
      ;;
  esac
}

fire_mixed() {
  local trigger="$1"
  # Never enables a timer. The tree may be half-synced and enabling producers
  # over it risks the production database, which is permanently worse than an
  # alerted outage.
  raise_attention "attention-mixed-tree" "kept" "not-probed" "" "$trigger" \
    "the deploy died between its first rsync and a verified install; the tree may be mixed"
}

fire_post_install() {
  local trigger="$1"
  local build_info="$DEADMAN_REMOTE_BASE/forecaster/build_info.json"
  if [[ ! -f "$build_info" ]]; then
    raise_attention "attention-missing-build-info" "kept" "missing" "$build_info" \
      "$trigger" "the release stamp is gone although the install had already passed its gates"
    return 0
  fi
  if ! grep -Fq "\"source_sha\": \"$STATE_SOURCE_SHA\"" "$build_info"; then
    raise_attention "attention-source-sha-mismatch" "kept" "mismatch" "$build_info" \
      "$trigger" "build_info.json no longer names $STATE_SOURCE_SHA"
    return 0
  fi
  perform_restore "$trigger" "clean" "source_sha matched" "restored-post-install"
}

tick_main() {
  local trigger="${1:-}"
  local trigger_failures=0
  local marker_present=0
  local boot_now=""
  local rebooted=0
  local heartbeat=0
  local now=0
  case "$trigger" in
    cron|systemd) ;;
    *) usage_error "tick requires a trigger source: cron or systemd" ;;
  esac
  acquire_lock nonblock || exit 0

  if [[ ! -f "$STATE_FILE" ]]; then
    # Orphan self-cleanup: a trigger with nothing to watch removes itself.
    remove_triggers || true
    exit 0
  fi
  if ! parse_state; then
    # No unit name is ever derived from damaged input, and nothing is started.
    STATE_PHASE="unparseable"
    write_last_action "attention-unparseable-state" "kept" "not-probed" "" \
      "$trigger" "the dead-man state file is damaged; nothing was touched"
    remove_triggers || true
    log_line "ATTENTION attention-unparseable-state: the dead-man state file is damaged"
    exit 0
  fi
  if [[ "$STATE_PHASE" == "attention" ]]; then
    # An incident acts exactly once. Normally the triggers are already gone and
    # this is a no-op; when removal keeps failing (a read-only /etc, immutable
    # unit files) stop retrying rather than issuing a daemon-reload every minute
    # on a box an operator has been told to leave alone.
    trigger_failures="$(trigger_removal_failures)"
    if (( trigger_failures < DEADMAN_MAX_TRIGGER_REMOVALS )); then
      if ! remove_triggers; then
        trigger_failures=$((trigger_failures + 1))
        printf '%s\n' "$trigger_failures" \
          | write_atomic "$TRIGGER_FAILURES_FILE" 2>/dev/null || true
        if (( trigger_failures >= DEADMAN_MAX_TRIGGER_REMOVALS )); then
          log_line "warning: could not remove the dead-man triggers after $trigger_failures attempts; giving up so this incident stops touching systemd"
        fi
      fi
    fi
    exit 0
  fi

  # Never fatal: a box that cannot write /var must still be able to fire. This
  # write is only evidence for `beat` that a trigger is alive.
  printf '%s %s\n' "$(now_epoch)" "$trigger" \
    | write_atomic "$LAST_TICK_FILE" \
    || log_line "warning: could not record the dead-man tick in $LAST_TICK_FILE"

  if [[ -e "$DEADMAN_MARKER" ]]; then
    marker_present=1
    [[ -f "$MARKER_SEEN_FILE" ]] || : > "$MARKER_SEEN_FILE"
  fi

  boot_now="$(read_boot_id)"
  if [[ -n "$boot_now" && -n "$STATE_ARMED_BOOT_ID" && "$boot_now" != "$STATE_ARMED_BOOT_ID" ]]; then
    rebooted=1
  fi

  if (( rebooted == 0 )) && [[ -f "$MARKER_SEEN_FILE" ]] && (( marker_present == 0 )); then
    # The marker was observed and is now gone on the same boot: the box is not
    # being held any more, so the dead-man has no business touching a timer.
    # This is also what stops a stale dead-man from re-enabling a timer an
    # operator deliberately paused.
    teardown_and_exit "stood-down" "absent" "not-probed" "" "$trigger" \
      "the maintenance marker was released, so the deploy finished or was recovered"
  fi

  if (( rebooted == 0 )); then
    heartbeat="$(heartbeat_epoch)"
    now="$(now_epoch)"
    if (( now - heartbeat <= STATE_LEASE_SECONDS )); then
      exit 0
    fi
  fi

  case "$STATE_PHASE" in
    pre-transfer) fire_pre_transfer "$trigger" ;;
    mixed) fire_mixed "$trigger" ;;
    post-install) fire_post_install "$trigger" ;;
  esac
  exit 0
}

beat_main() {
  local deploy_id="${1:-}"
  local tick=0
  local now=0
  [[ -n "$deploy_id" ]] || usage_error "beat requires the deploy id"
  # A lock we cannot take means a tick is deciding right now: never report the
  # deploy as still watched while that is true.
  acquire_lock wait || exit 13
  [[ -f "$STATE_FILE" ]] || exit 14
  parse_state || exit 14
  [[ "$STATE_DEPLOY_ID" == "$deploy_id" ]] || exit 14
  [[ "$STATE_PHASE" != "attention" ]] || exit 14
  # A failed state write is an ENVIRONMENT failure (a full or read-only /var),
  # not a revocation. It must be distinguishable, because the deploy host reads
  # anything it cannot interpret as "the dead-man already acted" only for the
  # two statuses that actually prove that.
  if ! printf '%s %s\n' "$(now_epoch)" "$(now_utc)" | write_atomic "$HEARTBEAT_FILE"; then
    echo "could not refresh the dead-man heartbeat: $HEARTBEAT_FILE" >&2
    exit 12
  fi
  tick="$(last_tick_epoch)"
  now="$(now_epoch)"
  if (( tick == 0 || now - tick > DEADMAN_TICK_STALE_SECONDS )); then
    echo "no dead-man trigger has ticked in the last ${DEADMAN_TICK_STALE_SECONDS}s" >&2
    exit 13
  fi
  exit 0
}

status_main() {
  local heartbeat=0
  local tick=0
  local now=0
  now="$(now_epoch)"
  if [[ ! -f "$STATE_FILE" ]]; then
    echo "deadman=not-armed"
    if [[ -f "$LAST_ACTION_FILE" ]]; then
      echo "last_action_file=$LAST_ACTION_FILE"
    fi
    exit 0
  fi
  if ! parse_state; then
    echo "deadman=unparseable-state"
    echo "state_file=$STATE_FILE"
    exit 0
  fi
  heartbeat="$(heartbeat_epoch)"
  tick="$(last_tick_epoch)"
  echo "deadman=armed"
  echo "phase=$STATE_PHASE"
  echo "deploy_id=$STATE_DEPLOY_ID"
  echo "source_sha=$STATE_SOURCE_SHA"
  echo "deploy_host=$STATE_DEPLOY_HOST"
  echo "armed_at_utc=$STATE_ARMED_AT_UTC"
  echo "pre_transfer_restore=$STATE_PRE_TRANSFER_RESTORE"
  echo "lease_seconds=$STATE_LEASE_SECONDS"
  echo "heartbeat_age_seconds=$(( now - heartbeat ))"
  echo "fires_in_seconds=$(( heartbeat + STATE_LEASE_SECONDS - now ))"
  echo "cron_trigger_present=$([[ -f "$DEADMAN_CRON_FILE" ]] && echo 1 || echo 0)"
  echo "systemd_trigger_present=$([[ -f "$TIMER_UNIT" ]] && echo 1 || echo 0)"
  echo "systemd_trigger_active=$("$SYSTEMCTL" is-active --quiet "$DEADMAN_UNIT_NAME.timer" >/dev/null 2>&1 && echo 1 || echo 0)"
  echo "last_tick_age_seconds=$(( now - tick ))"
  echo "last_tick_source=$(last_tick_source)"
  echo "marker_present=$([[ -e "$DEADMAN_MARKER" ]] && echo 1 || echo 0)"
  echo "restore_attempts=$(restore_attempts)"
  echo "timers=$STATE_TIMERS"
  exit 0
}

clear_main() {
  local force=0
  local heartbeat=0
  local now=0
  if [[ "${1:-}" == "--force" ]]; then
    force=1
  elif [[ -n "${1:-}" ]]; then
    usage_error "clear accepts only --force"
  fi
  acquire_lock wait || exit 12
  if [[ -f "$STATE_FILE" ]] && parse_state && (( force == 0 )); then
    heartbeat="$(heartbeat_epoch)"
    now="$(now_epoch)"
    if [[ "$STATE_PHASE" != "attention" ]] && (( now - heartbeat <= STATE_LEASE_SECONDS )); then
      echo "refusing to clear: deploy $STATE_DEPLOY_ID from $STATE_DEPLOY_HOST still holds a live lease (heartbeat $(( now - heartbeat ))s ago). Use --force to override." >&2
      exit 11
    fi
  fi
  remove_triggers || echo "warning: could not remove every dead-man trigger" >&2
  remove_runtime_state
  rm -f "$PINNED_HELPER" 2>/dev/null || true
  echo "dead-man cleared; $LAST_ACTION_FILE is kept"
  exit 0
}

# The deploy host's recovery traps use this when they deliberately leave the box
# quiesced: a post-install dead-man would otherwise re-enable every timer about
# ten minutes later and undo a stay-dark decision the host made on purpose.
hold_main() {
  local deploy_id="${1:-}"
  [[ -n "$deploy_id" ]] || usage_error "hold requires the deploy id"
  acquire_lock wait || exit 12
  [[ -f "$STATE_FILE" ]] || exit 14
  parse_state || exit 14
  if [[ "$STATE_PHASE" == "attention" ]]; then
    echo "the dead-man is already holding this box" >&2
    exit 0
  fi
  [[ "$STATE_DEPLOY_ID" == "$deploy_id" ]] || exit 11
  raise_attention "attention-host-recovery-held" "kept" "not-probed" "" "host" \
    "the deploy host could not restore this box and quiesced it deliberately; the dead-man must not undo that"
  exit 0
}

disarm_main() {
  local deploy_id="${1:-}"
  [[ -n "$deploy_id" ]] || usage_error "disarm requires the deploy id"
  acquire_lock wait || exit 12
  [[ -f "$STATE_FILE" ]] || exit 14
  parse_state || exit 14
  [[ "$STATE_PHASE" != "attention" ]] || exit 14
  [[ "$STATE_DEPLOY_ID" == "$deploy_id" ]] || exit 11
  if ! remove_triggers; then
    echo "failed to remove every dead-man trigger" >&2
    exit 12
  fi
  remove_runtime_state
  echo "dead-man disarmed for $deploy_id"
  exec /bin/rm -f -- "$PINNED_HELPER" "$DEADMAN_SELF"
}

ARM_HAD_STATE=0
ARM_PREV_STATE=""
arm_rollback() {
  if (( ARM_HAD_STATE == 1 )); then
    printf '%s' "$ARM_PREV_STATE" > "$STATE_FILE.new" 2>/dev/null \
      && mv -f "$STATE_FILE.new" "$STATE_FILE" 2>/dev/null || true
    return 0
  fi
  remove_triggers || true
  remove_runtime_state
  rm -f "$PINNED_HELPER" 2>/dev/null || true
}

arm_main() {
  local deploy_id=""
  local phase=""
  local source_sha=""
  local deploy_host=""
  local lease_seconds=""
  local tick_wait_seconds="0"
  local pre_transfer_restore=""
  local expect_version=""
  local install_helper=0
  local await_tick=0
  local clear_incident=0
  local -a timers=()
  local timer=""
  local timers_line=""
  local triggers_line=""
  local armed_cron=0
  local armed_systemd=0
  local deadline=0
  local seen_cron=0
  local seen_systemd=0
  local tick_epoch=0
  local armed_epoch=0
  local armed_at_utc=""
  local phase_epoch=0
  local boot_id=""

  while (( $# > 0 )); do
    case "$1" in
      --deploy-id) deploy_id="${2:-}"; shift 2 ;;
      --phase) phase="${2:-}"; shift 2 ;;
      --source-sha) source_sha="${2:-}"; shift 2 ;;
      --deploy-host) deploy_host="${2:-}"; shift 2 ;;
      --lease-seconds) lease_seconds="${2:-}"; shift 2 ;;
      --tick-wait-seconds) tick_wait_seconds="${2:-}"; shift 2 ;;
      --pre-transfer-restore) pre_transfer_restore="${2:-}"; shift 2 ;;
      --expect-version) expect_version="${2:-}"; shift 2 ;;
      --install-helper) install_helper=1; shift ;;
      --await-tick) await_tick=1; shift ;;
      --clear-incident) clear_incident=1; shift ;;
      --) shift; break ;;
      *) usage_error "unknown arm option: $1" ;;
    esac
  done
  for timer in "$@"; do
    valid_timer "$timer" || usage_error "arm refuses an unexpected timer name: $timer"
    timers+=("$timer")
    if [[ -n "$timers_line" ]]; then
      timers_line="$timers_line $timer"
    else
      timers_line="$timer"
    fi
  done

  valid_deploy_id "$deploy_id" || usage_error "arm requires a valid --deploy-id"
  case "$phase" in
    pre-transfer|mixed|post-install) ;;
    *) usage_error "arm requires --phase pre-transfer|mixed|post-install" ;;
  esac
  valid_token "$source_sha" || usage_error "arm requires a valid --source-sha"
  valid_token "$deploy_host" || usage_error "arm requires a valid --deploy-host"
  valid_uint "$lease_seconds" || usage_error "arm requires numeric --lease-seconds"
  (( lease_seconds >= 60 )) || usage_error "arm requires --lease-seconds of at least 60"
  valid_uint "$tick_wait_seconds" || usage_error "arm requires numeric --tick-wait-seconds"
  case "$pre_transfer_restore" in 0|1) ;; *) usage_error "arm requires --pre-transfer-restore 0|1" ;; esac
  (( ${#timers[@]} <= DEADMAN_MAX_TIMERS )) || usage_error "arm refuses more than $DEADMAN_MAX_TIMERS timers"
  if [[ -n "$expect_version" && "$expect_version" != "$DEADMAN_VERSION" ]]; then
    usage_error "dead-man payload version mismatch: box has $DEADMAN_VERSION, host expected $expect_version"
  fi

  acquire_lock wait || exit 12

  # Compare and swap. A live incumbent, or an incident nobody has acknowledged,
  # owns this box until a human clears it.
  if [[ -f "$STATE_FILE" ]]; then
    ARM_HAD_STATE=1
    ARM_PREV_STATE="$(cat "$STATE_FILE")"
    if ! parse_state; then
      if (( clear_incident == 0 )); then
        echo "refusing to arm: the box holds an unreadable dead-man state file ($STATE_FILE)" >&2
        exit 11
      fi
      ARM_HAD_STATE=0
    elif [[ "$STATE_PHASE" == "attention" ]]; then
      if (( clear_incident == 0 )); then
        echo "refusing to arm: an unacknowledged dead-man incident owns this box" >&2
        echo "  deploy_id=$STATE_DEPLOY_ID phase=$STATE_PHASE deploy_host=$STATE_DEPLOY_HOST heartbeat_age=$(( $(now_epoch) - $(heartbeat_epoch) ))s" >&2
        echo "  read: cat $LAST_ACTION_FILE; then sudo $DEADMAN_SELF clear --force" >&2
        exit 11
      fi
      ARM_HAD_STATE=0
    elif [[ "$STATE_DEPLOY_ID" != "$deploy_id" ]]; then
      # A host whose heartbeat is already older than its own lease is provably
      # gone, so --clear-incident may take the box over from it. Anything newer
      # is a live deploy and is never stolen.
      if (( clear_incident == 1 )) \
        && (( $(now_epoch) - $(heartbeat_epoch) > STATE_LEASE_SECONDS )); then
        ARM_HAD_STATE=0
      else
        echo "refusing to arm: another deploy already owns this box" >&2
        echo "  deploy_id=$STATE_DEPLOY_ID phase=$STATE_PHASE deploy_host=$STATE_DEPLOY_HOST heartbeat_age=$(( $(now_epoch) - $(heartbeat_epoch) ))s" >&2
        echo "  if that deploy is gone: sudo $DEADMAN_SELF status; then sudo $DEADMAN_SELF clear --force" >&2
        echo "  or rerun the deploy with SFO_DEPLOY_DEADMAN_CLEAR_INCIDENT=1" >&2
        exit 11
      fi
    fi
  fi

  # Environment. Anything unusable here fails before the host quiesces a thing.
  if ! ( install -d -o root -g root -m 0755 "$DEADMAN_STATE_DIR" 2>/dev/null \
      || mkdir -p "$DEADMAN_STATE_DIR" 2>/dev/null ); then
    echo "dead-man state directory is not creatable: $DEADMAN_STATE_DIR" >&2
    exit 12
  fi
  if ! cron_armable && ! systemd_armable; then
    echo "neither cron nor systemd can run the dead-man on this box" >&2
    arm_rollback
    exit 12
  fi
  if cron_armable && ! command -v "$DEADMAN_LOGGER" >/dev/null 2>&1; then
    # The cron entry normally pipes the payload into logger. Without it the
    # pipeline's right-hand side is reaped immediately, so drop the pipe and
    # send the output nowhere instead of leaving the payload writing to a
    # closed descriptor.
    echo "warning: $DEADMAN_LOGGER is missing; the dead-man cron entry will discard its output instead of journalling it" >&2
    DEADMAN_LOGGER=""
  fi
  if (( ARM_HAD_STATE == 0 )); then
    # A new deploy inherits no history from an old one. A leftover marker-seen
    # would stand the dead-man down on its very first tick, because arm runs
    # before the maintenance marker is installed; a leftover restore-attempts
    # would exhaust the retry budget on the first failure.
    rm -f "$MARKER_SEEN_FILE" "$RESTORE_ATTEMPTS_FILE" "$LAST_TICK_FILE" \
      "$TRIGGER_FAILURES_FILE" \
      2>/dev/null || true
  fi

  if (( install_helper == 1 )); then
    if ! cat > "$PINNED_HELPER.new"; then
      echo "could not pin the quiesce helper beside the dead-man state" >&2
      arm_rollback
      exit 12
    fi
    chmod 0755 "$PINNED_HELPER.new"
    mv -f "$PINNED_HELPER.new" "$PINNED_HELPER"
  elif [[ ! -f "$PINNED_HELPER" ]]; then
    echo "the pinned quiesce helper is missing: $PINNED_HELPER" >&2
    arm_rollback
    exit 12
  fi

  phase_epoch="$(now_epoch)"
  armed_epoch="$phase_epoch"
  armed_at_utc="$(now_utc)"
  boot_id="$(read_boot_id)"
  if (( ARM_HAD_STATE == 1 )); then
    # Same deploy, later phase. Keep when this deploy actually started and which
    # boot it started on, so a reboot anywhere inside the deploy still fires and
    # the incident record still says how long the deploy had been running.
    armed_epoch="$STATE_ARMED_AT_EPOCH"
    armed_at_utc="$STATE_ARMED_AT_UTC"
    boot_id="$STATE_ARMED_BOOT_ID"
  fi
  # An arm is itself proof that the deploy host reached this box just now, so it
  # refreshes the lease exactly as a beat does. Without this a phase change
  # could inherit an almost-expired lease from a slow previous step.
  printf '%s %s\n' "$phase_epoch" "$(now_utc)" | write_atomic "$HEARTBEAT_FILE"
  # Written last of the timestamps, so nothing the deploy transfers afterwards
  # can be mistaken for a file that was already there.
  : > "$ARM_STAMP_FILE" 2>/dev/null || true
  write_atomic "$STATE_FILE" <<STATE
DEADMAN_VERSION=$DEADMAN_VERSION
DEPLOY_ID=$deploy_id
PHASE=$phase
PRE_TRANSFER_RESTORE=$pre_transfer_restore
SOURCE_SHA=$source_sha
DEPLOY_HOST=$deploy_host
ARMED_AT_EPOCH=$armed_epoch
ARMED_AT_UTC=$armed_at_utc
ARMED_BOOT_ID=$boot_id
PHASE_AT_EPOCH=$phase_epoch
LEASE_SECONDS=$lease_seconds
TIMERS=$timers_line
STATE

  if cron_armable; then
    if write_cron_trigger; then
      armed_cron=1
      triggers_line="cron"
    else
      echo "warning: could not write the dead-man cron trigger" >&2
    fi
  fi
  if systemd_armable; then
    if write_systemd_trigger; then
      armed_systemd=1
      if [[ -n "$triggers_line" ]]; then
        triggers_line="$triggers_line,systemd"
      else
        triggers_line="systemd"
      fi
    else
      echo "warning: could not install the dead-man systemd trigger" >&2
    fi
  fi
  if (( armed_cron == 0 && armed_systemd == 0 )); then
    echo "could not arm any dead-man trigger" >&2
    arm_rollback
    exit 12
  fi

  # Release the lock before waiting: the proof we want is a trigger actually
  # running the payload, and the payload takes the same lock.
  release_lock

  if (( await_tick == 1 && tick_wait_seconds > 0 )); then
    deadline=$(( phase_epoch + tick_wait_seconds ))
    while :; do
      tick_epoch="$(last_tick_epoch)"
      if (( tick_epoch >= phase_epoch )); then
        case "$(last_tick_source)" in
          cron) seen_cron=1 ;;
          systemd) seen_systemd=1 ;;
        esac
      fi
      if (( (armed_cron == 0 || seen_cron == 1) && (armed_systemd == 0 || seen_systemd == 1) )); then
        break
      fi
      if (( $(now_epoch) >= deadline )); then
        break
      fi
      sleep 2
    done
    if (( seen_cron == 0 && seen_systemd == 0 )); then
      echo "no dead-man trigger ran the payload within ${tick_wait_seconds}s" >&2
      arm_rollback
      exit 12
    fi
    if (( armed_cron == 1 && seen_cron == 0 )); then
      echo "warning: the dead-man cron trigger never ticked; only systemd is watching this deploy" >&2
    fi
    if (( armed_systemd == 1 && seen_systemd == 0 )); then
      echo "warning: the dead-man systemd trigger never ticked; only cron is watching this deploy" >&2
    fi
  fi

  echo "DEADMAN_VERSION=$DEADMAN_VERSION"
  echo "DEADMAN_DEPLOY_ID=$deploy_id"
  echo "DEADMAN_TRIGGERS=$triggers_line"
  exit 0
}

MODE="${1:-}"
if (( $# > 0 )); then
  shift
fi
case "$MODE" in
  arm) arm_main "$@" ;;
  beat) beat_main "$@" ;;
  tick) tick_main "$@" ;;
  status) status_main "$@" ;;
  clear) clear_main "$@" ;;
  disarm) disarm_main "$@" ;;
  hold) hold_main "$@" ;;
  *) usage_error "unknown mode: ${MODE:-<none>}" ;;
esac
