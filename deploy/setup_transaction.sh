#!/usr/bin/env bash
# Transaction helpers sourced by setup-vps.sh.  The caller defines the path,
# backup, and state variables below before installing the EXIT trap.

rollback_venv() {
  if (( ! VENV_SWAPPED )); then
    return 0
  fi
  if ! rm -rf "$APPDIR/.venv"; then
    echo "ERROR: could not remove the failed staged venv" >&2
    return 1
  fi
  if [ -d "$VENV_BACKUP" ] && ! mv "$VENV_BACKUP" "$APPDIR/.venv"; then
    echo "ERROR: could not restore the previous venv from $VENV_BACKUP" >&2
    return 1
  fi
  VENV_SWAPPED=0
}

rollback_deployment() {
  if (( ROLLBACK_DONE )); then
    return 0
  fi
  ROLLBACK_DONE=1

  local had_errexit=0
  local restore_relay=$((VENV_SWAPPED || UNIT_CHANGED || RELAY_SERVICE_TOUCHED))
  local restore_caddy=$((CADDY_CHANGED || CADDY_SERVICE_TOUCHED))
  local had_previous_venv=0
  local had_previous_unit=0
  if (( VENV_SWAPPED )); then
    [ -d "$VENV_BACKUP" ] && had_previous_venv=1
  elif [ -d "$APPDIR/.venv" ]; then
    had_previous_venv=1
  fi
  if (( UNIT_CHANGED )); then
    (( UNIT_HAD_FILE )) && had_previous_unit=1
  elif [ -f "$RELAY_UNIT_FILE" ]; then
    had_previous_unit=1
  fi
  [[ $- == *e* ]] && had_errexit=1
  set +e

  echo "==> rollback: restoring the previous cc-remote deployment" >&2

  rollback_venv

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
    if (( had_previous_unit && had_previous_venv \
          && ! UNIT_CHANGED && ! VENV_SWAPPED )); then
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
  local status=$?
  if (( ! DEPLOY_READY )); then
    rollback_deployment
  fi
  [ -z "$VENV_STAGE" ] || rm -rf "$VENV_STAGE"
  [ -z "$CADDY_SITE" ] || rm -f "$CADDY_SITE"
  [ -z "$CADDY_CANDIDATE" ] || rm -f "$CADDY_CANDIDATE"
  if (( DEPLOY_READY || ! CADDY_CHANGED )); then
    [ -z "$CADDY_BACKUP" ] || rm -f "$CADDY_BACKUP"
  fi
  if (( DEPLOY_READY || ! UNIT_CHANGED )); then
    [ -z "$UNIT_BACKUP" ] || rm -f "$UNIT_BACKUP"
  fi
  return "$status"
}
