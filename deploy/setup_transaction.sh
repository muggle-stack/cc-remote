#!/usr/bin/env bash
# Transaction helpers sourced by setup-vps.sh. The caller defines release,
# Caddy, unit, and service state before installing the EXIT trap.

atomic_install_file() {
  local source="$1" destination="$2" owner="$3" group="$4" mode="$5"
  local destination_dir destination_name staged

  [ -f "$source" ] || {
    echo "ERROR: install source is not a regular file: $source" >&2
    return 1
  }
  destination_dir="$(dirname "$destination")"
  destination_name="$(basename "$destination")"
  [ -d "$destination_dir" ] || {
    echo "ERROR: install destination directory is missing: $destination_dir" >&2
    return 1
  }

  staged="$(mktemp "$destination_dir/.${destination_name}.cc-remote.XXXXXX")" || \
    return 1
  if ! install -o "$owner" -g "$group" -m "$mode" "$source" "$staged"; then
    rm -f -- "$staged"
    return 1
  fi
  if ! python3 - "$staged" "$destination" "$destination_dir" <<'PY'
import os
import sys

source, destination, destination_dir = sys.argv[1:]
with open(source, "rb") as stream:
    os.fsync(stream.fileno())
os.replace(source, destination)
directory_fd = os.open(destination_dir, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
  then
    rm -f -- "$staged"
    return 1
  fi
}

harden_release_permissions() {
  local release="$1"
  [ -d "$release" ] || {
    echo "ERROR: release target is not a directory: $release" >&2
    return 1
  }
  # Archive/extract and legacy cp preserve source modes. Remove inherited group
  # write before granting the service account read/traverse access, and never
  # expose release contents to other users.
  chmod -R g-w,o-rwx "$release"
  chmod -R g+rX "$release"
}

atomic_release_link() {
  local target="$1" link="$2" next
  [ -d "$target" ] || {
    echo "ERROR: release target is not a directory: $target" >&2
    return 1
  }
  next="${link}.next.$$"
  rm -f "$next"
  ln -s "$target" "$next"
  # os.replace performs one same-directory rename and never follows an
  # existing current symlink. This is atomic on the target filesystem and also
  # avoids GNU/BSD mv differences around symlink-to-directory destinations.
  python3 - "$next" "$link" <<'PY'
import os
import sys

os.replace(sys.argv[1], sys.argv[2])
PY
}

rollback_release() {
  if (( ! RELEASE_SWITCHED )); then
    return 0
  fi
  if [ -n "$PREVIOUS_RELEASE" ] && [ -d "$PREVIOUS_RELEASE" ]; then
    if ! atomic_release_link "$PREVIOUS_RELEASE" "$CURRENT_LINK"; then
      echo "ERROR: could not restore previous release $PREVIOUS_RELEASE" >&2
      return 1
    fi
  elif ! rm -f "$CURRENT_LINK"; then
    echo "ERROR: could not remove failed first-release link" >&2
    return 1
  fi
  RELEASE_SWITCHED=0
}

rollback_deployment() {
  if (( ROLLBACK_DONE )); then
    return 0
  fi
  ROLLBACK_DONE=1

  local had_errexit=0
  local restore_relay=$((RELEASE_SWITCHED || UNIT_CHANGED || RELAY_SERVICE_TOUCHED))
  local restore_caddy=$((CADDY_CHANGED || CADDY_SERVICE_TOUCHED))
  local had_previous_release=0
  local had_previous_unit=0
  [ -n "$PREVIOUS_RELEASE" ] && [ -d "$PREVIOUS_RELEASE" ] && \
    had_previous_release=1
  if (( UNIT_CHANGED )); then
    (( UNIT_HAD_FILE )) && had_previous_unit=1
  elif [ -f "$RELAY_UNIT_FILE" ]; then
    had_previous_unit=1
  fi
  [[ $- == *e* ]] && had_errexit=1
  set +e

  echo "==> rollback: restoring the previous cc-remote release" >&2

  rollback_release

  if (( UNIT_CHANGED )); then
    if (( UNIT_HAD_FILE )); then
      if cp -a "$UNIT_BACKUP" "$RELAY_UNIT_FILE"; then
        UNIT_CHANGED=0
      else
        echo "ERROR: failed to restore relay unit from $UNIT_BACKUP" >&2
      fi
    elif rm -f "$RELAY_UNIT_FILE"; then
      UNIT_CHANGED=0
    else
      echo "ERROR: failed to remove newly-installed relay unit" >&2
    fi
    systemctl daemon-reload || \
      echo "ERROR: systemd daemon-reload failed during rollback" >&2
  fi

  if (( CADDY_CHANGED )); then
    if (( CADDY_HAD_CONFIG )); then
      if cp -a "$CADDY_BACKUP" "$CADDYFILE"; then
        CADDY_CHANGED=0
      else
        echo "ERROR: failed to restore Caddy config from $CADDY_BACKUP" >&2
      fi
    elif rm -f "$CADDYFILE"; then
      CADDY_CHANGED=0
    else
      echo "ERROR: failed to remove newly-installed Caddy config" >&2
    fi
  fi

  if (( restore_caddy )); then
    if [ -f "$CADDYFILE" ]; then
      systemctl restart caddy || \
        echo "ERROR: Caddy failed to restart after rollback" >&2
    else
      systemctl stop caddy || true
    fi
  fi
  if (( restore_relay )); then
    if (( had_previous_unit && had_previous_release \
          && ! UNIT_CHANGED && ! RELEASE_SWITCHED )); then
      if systemctl restart cc-remote-relay; then
        local rollback_ready=0
        local attempts=0
        while (( attempts < 10 )); do
          attempts=$((attempts + 1))
          if curl --fail --silent --show-error --max-time 2 \
              http://127.0.0.1:8765/healthz >/dev/null; then
            rollback_ready=1
            break
          fi
          sleep 1
        done
        if (( rollback_ready )); then
          echo "==> rollback: previous relay passed /healthz" >&2
        else
          echo "ERROR: previous relay failed /healthz after rollback" >&2
        fi
      else
        echo "ERROR: relay failed to restart after rollback" >&2
      fi
    else
      systemctl stop cc-remote-relay || true
    fi
  fi

  (( had_errexit )) && set -e
  return 0
}

cleanup() {
  local exit_status=$?
  if (( ! DEPLOY_READY )); then
    rollback_deployment
    # A restored current link alone does not prove Caddy, service and data
    # recovery. Leave a failed managed activation prepared for inspection.
    if [ -n "$NEW_RELEASE_DIR" ]; then
      if (( RELEASE_SWITCHED || ${RETENTION_STARTED:-0} )) || [ -n "${RETENTION_GENERATION:-}" ]; then
        # A failed rollback may have left current pointing at the new release.
        # Keep that tree intact rather than turning the active symlink into a
        # dangling link; the operator can repair current and remove it later.
        if (( RELEASE_SWITCHED )); then
          echo "ERROR: retaining staged release because release rollback failed" >&2
        else
          echo "ERROR: retaining staged release for transaction inspection" >&2
        fi
      else
        case "$NEW_RELEASE_DIR" in
          "$RELEASES_DIR"/release-*) rm -rf -- "$NEW_RELEASE_DIR" ;;
          *) echo "ERROR: refusing to remove unexpected release path" >&2 ;;
        esac
      fi
    fi
  fi
  [ -z "$CADDY_SITE" ] || rm -f "$CADDY_SITE"
  [ -z "$CADDY_CANDIDATE" ] || rm -f "$CADDY_CANDIDATE"
  if [ -n "${UNIT_VERIFY_DIR:-}" ]; then
    case "$UNIT_VERIFY_DIR" in
      /run/cc-remote-unit.*) rm -rf -- "$UNIT_VERIFY_DIR" ;;
      *) echo "ERROR: refusing to remove unexpected unit verify path" >&2 ;;
    esac
  fi
  rm -f "${CURRENT_LINK}.next.$$"
  if (( DEPLOY_READY || ! CADDY_CHANGED )); then
    [ -z "$CADDY_BACKUP" ] || rm -f "$CADDY_BACKUP"
  fi
  if (( DEPLOY_READY || ! UNIT_CHANGED )); then
    [ -z "$UNIT_BACKUP" ] || rm -f "$UNIT_BACKUP"
  fi
  return "$exit_status"
}
