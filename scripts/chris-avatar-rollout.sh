#!/usr/bin/env bash

set -euo pipefail
umask 077

readonly PROFILE_NAME="${HERMES_PROFILE:-chris-avatar}"
readonly HERMES_ROOT="${HERMES_HOME:-${HOME}/.hermes}"
readonly PROFILE_DIR="${HERMES_PROFILE_DIR:-${HERMES_ROOT}/profiles/${PROFILE_NAME}}"
readonly GATEWAY_SERVICE="${HERMES_GATEWAY_SERVICE:-hermes-gateway-${PROFILE_NAME}.service}"
readonly HERMES_COMMAND="${HERMES_BIN:-hermes}"
readonly SQLITE_COMMAND="${SQLITE3_BIN:-sqlite3}"
readonly SYSTEMCTL_COMMAND="${SYSTEMCTL_BIN:-systemctl}"

usage() {
  printf '%s\n' \
    "Usage:" \
    "  $0 backup <new-backup-directory>" \
    "  $0 rollback <backup-directory>" \
    "" \
    "Defaults target the chris-avatar named profile. Override only with:" \
    "  HERMES_HOME, HERMES_PROFILE, HERMES_PROFILE_DIR, HERMES_GATEWAY_SERVICE"
}

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

validate_profile() {
  case "$PROFILE_NAME" in
    "" | *[!a-zA-Z0-9_-]*) fail "unsafe profile name: $PROFILE_NAME" ;;
  esac
  [[ -d "$PROFILE_DIR" ]] || fail "profile directory not found: $PROFILE_DIR"
  [[ -f "$PROFILE_DIR/config.yaml" ]] || fail "missing profile config.yaml"
  [[ -f "$PROFILE_DIR/SOUL.md" ]] || fail "missing profile SOUL.md"
  [[ -f "$PROFILE_DIR/state.db" ]] || fail "missing profile state.db"
}

validate_sqlite_dot_path() {
  case "$1" in
    *'"'* | *$'\n'* | *$'\r'*)
      fail "SQLite backup path contains an unsupported character"
      ;;
  esac
}

write_session_index() {
  local destination="$1"
  if [[ -d "$PROFILE_DIR/sessions" ]]; then
    find "$PROFILE_DIR/sessions" -mindepth 1 -maxdepth 1 -printf '%f\n' \
      | LC_ALL=C sort >"$destination"
  else
    : >"$destination"
  fi
}

backup_profile() {
  local destination="$1"
  validate_sqlite_dot_path "$destination"
  [[ ! -e "$destination" ]] || fail "backup target already exists: $destination"
  validate_profile
  require_command "$SQLITE_COMMAND"
  require_command sha256sum

  mkdir -m 0700 -- "$destination"
  cp -p -- "$PROFILE_DIR/config.yaml" "$destination/config.yaml"
  cp -p -- "$PROFILE_DIR/SOUL.md" "$destination/SOUL.md"
  "$SQLITE_COMMAND" "$PROFILE_DIR/state.db" ".backup \"$destination/state.db\""
  write_session_index "$destination/session-index.txt"

  {
    printf 'profile=%s\n' "$PROFILE_NAME"
    printf 'profile_dir=%s\n' "$PROFILE_DIR"
    printf 'gateway_service=%s\n' "$GATEWAY_SERVICE"
    printf 'created_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if [[ -d "$HERMES_ROOT/hermes-agent/.git" ]]; then
      printf 'hermes_commit=%s\n' "$(git -C "$HERMES_ROOT/hermes-agent" rev-parse HEAD)"
    fi
    if command -v "$SYSTEMCTL_COMMAND" >/dev/null 2>&1; then
      local gateway_active
      gateway_active="$(
        "$SYSTEMCTL_COMMAND" --user is-active "$GATEWAY_SERVICE" 2>/dev/null \
          || true
      )"
      printf 'gateway_active=%s\n' "${gateway_active:-unknown}"
      "$SYSTEMCTL_COMMAND" --user show "$GATEWAY_SERVICE" \
        --property=MainPID --property=ActiveState 2>/dev/null || true
    fi
  } >"$destination/metadata.txt"
  printf 'chris-hermes-agent-p7-backup\nprofile=%s\n' "$PROFILE_NAME" \
    >"$destination/.p7-rollout-backup"
  (
    cd "$destination"
    sha256sum config.yaml SOUL.md state.db session-index.txt metadata.txt \
      >SHA256SUMS
  )
  chmod 0600 "$destination"/* "$destination/.p7-rollout-backup"
  printf 'Backup complete: %s\n' "$destination"
}

validate_backup() {
  local source="$1"
  validate_sqlite_dot_path "$source"
  [[ -d "$source" ]] || fail "not a complete chris-avatar backup: $source"
  for required in \
    .p7-rollout-backup config.yaml SOUL.md state.db session-index.txt \
    metadata.txt SHA256SUMS; do
    [[ -f "$source/$required" ]] \
      || fail "not a complete chris-avatar backup: missing $required"
  done
  grep -Fxq 'chris-hermes-agent-p7-backup' "$source/.p7-rollout-backup" \
    || fail "not a complete chris-avatar backup: invalid marker"
  grep -Fxq "profile=$PROFILE_NAME" "$source/.p7-rollout-backup" \
    || fail "backup belongs to a different profile"
  (
    cd "$source"
    sha256sum --check --quiet SHA256SUMS
  ) || fail "backup checksum validation failed"
  [[ "$("$SQLITE_COMMAND" "$source/state.db" 'PRAGMA integrity_check;')" == "ok" ]] \
    || fail "backup state.db failed integrity check"
}

rollback_profile() {
  local source="$1"
  require_command "$SQLITE_COMMAND"
  require_command sha256sum
  validate_backup "$source"
  validate_profile
  require_command "$HERMES_COMMAND"
  require_command "$SYSTEMCTL_COMMAND"

  local gateway_stopped=false
  restart_on_failure() {
    if [[ "$gateway_stopped" == true ]]; then
      "$SYSTEMCTL_COMMAND" --user start "$GATEWAY_SERVICE" || true
    fi
  }
  trap restart_on_failure EXIT

  "$SYSTEMCTL_COMMAND" --user stop "$GATEWAY_SERVICE"
  gateway_stopped=true
  if ! "$HERMES_COMMAND" -p "$PROFILE_NAME" plugins disable chris-hermes-agent; then
    printf '%s\n' \
      'Warning: plugin disable failed; continuing with the known-good config restore.' >&2
  fi
  cp -p -- "$source/config.yaml" "$PROFILE_DIR/config.yaml"
  cp -p -- "$source/SOUL.md" "$PROFILE_DIR/SOUL.md"
  "$SQLITE_COMMAND" "$PROFILE_DIR/state.db" ".restore \"$source/state.db\""
  [[ "$("$SQLITE_COMMAND" "$PROFILE_DIR/state.db" 'PRAGMA integrity_check;')" == "ok" ]] \
    || fail "restored state.db failed integrity check"

  "$SYSTEMCTL_COMMAND" --user start "$GATEWAY_SERVICE"
  gateway_stopped=false
  trap - EXIT
  printf '%s\n' \
    "Rollback complete for $PROFILE_NAME." \
    "Plugin SQLite and Emergency archives were preserved for diagnosis."
}

[[ $# -eq 2 ]] || {
  usage >&2
  exit 2
}

case "$1" in
  backup) backup_profile "$2" ;;
  rollback) rollback_profile "$2" ;;
  *)
    usage >&2
    exit 2
    ;;
esac
