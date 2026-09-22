#!/usr/bin/env bash
# First-install/update entrypoint for a role-scoped wrapper release bundle.
set -euo pipefail
installer_args=("$@")

die() {
  echo "ERROR: $*" >&2
  exit 1
}

usage() {
  cat >&2 <<'EOF'
Usage:
  install-wrapper.sh BUNDLE --relay https://remote.example.com --pair PAIR-CODE [--name LABEL]
  install-wrapper.sh BUNDLE [--user USER]

The relay and pair arguments are required for the first install. They may be
omitted on upgrades when a device credential already exists. Linux installs
need --user when the invoking account cannot be inferred from sudo.
EOF
  exit 2
}

bundle="${1:-}"
[ -n "$bundle" ] || usage
shift

relay=""
pair_code=""
device_name=""
target_user="${CC_REMOTE_INSTALL_USER:-}"
replace_pair=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --relay)
      [ "$#" -ge 2 ] || usage
      relay="$2"
      shift 2
      ;;
    --pair)
      [ "$#" -ge 2 ] || usage
      pair_code="$2"
      shift 2
      ;;
    --name)
      [ "$#" -ge 2 ] || usage
      device_name="$2"
      shift 2
      ;;
    --user)
      [ "$#" -ge 2 ] || usage
      target_user="$2"
      shift 2
      ;;
    --replace-pair)
      replace_pair=1
      shift
      ;;
    *) die "unknown wrapper installer argument: $1" ;;
  esac
done

if [ -n "$relay" ] || [ -n "$pair_code" ]; then
  if [ -z "$relay" ] || [ -z "$pair_code" ]; then
    die "--relay and --pair must be provided together"
  fi
elif [ -n "$device_name" ] || [ "$replace_pair" -eq 1 ]; then
  die "--name and --replace-pair require --relay and --pair"
fi

raw_system="$(uname -s)"
case "$raw_system" in
  Darwin) system=darwin ;;
  Linux)
    system=linux
    command -v getconf >/dev/null 2>&1 || \
      die "published Linux wrapper bundles require glibc"
    getconf GNU_LIBC_VERSION >/dev/null 2>&1 || \
      die "published Linux wrapper bundles require glibc"
    ;;
  *) die "wrapper installation does not support $raw_system" ;;
esac
case "$(uname -m)" in
  x86_64|amd64) machine=x86_64 ;;
  arm64|aarch64) machine=arm64 ;;
  *) die "unsupported architecture: $(uname -m)" ;;
esac

bundle="$(cd "$bundle" && pwd -P)"
[ -x "$bundle/bin/uv" ] || die "bundled uv executable is missing"
[ -f "$bundle/requirements-wrapper.lock" ] || \
  die "requirements-wrapper.lock is missing"
manifest="$bundle/release-manifest.json"
[ -f "$manifest" ] || die "release-manifest.json is missing"
python_runtime="$(
  sed -n 's/.*"python":"\([0-9.]*\)".*/\1/p' "$manifest"
)"
[[ "$python_runtime" =~ ^3\.13\.[0-9]+$ ]] || \
  die "release manifest has an invalid Python runtime"

# The bundled uv supplies the only bootstrap runtime we need. This validation
# imports no cc-remote code and does not contact a model.
(
  cd "$bundle"
  "$bundle/bin/uv" run --no-project --no-env-file --managed-python \
    --python "$python_runtime" \
    python -m deploy.release_manifest \
    "$bundle" --role wrapper --os "$system" --arch "$machine"
)

version="$(sed -n 's/.*"product_version":"\([^"]*\)".*/\1/p' "$manifest")"
git_sha="$(sed -n 's/.*"git_sha":"\([0-9a-f]*\)".*/\1/p' "$manifest")"
case "$version" in
  [0-9]*.[0-9]*.[0-9]*) ;;
  *) die "release manifest has an invalid product version" ;;
esac
[ "${#git_sha}" -eq 40 ] || die "release manifest has an invalid git SHA"
release_name="release-v${version}-${git_sha:0:12}"

if [ "$system" = darwin ]; then
  [ "$(id -u)" -ne 0 ] || \
    die "macOS wrapper installation must run as the logged-in user, not root"
  target_user="$(id -un)"
  target_home="${HOME:-}"
  if [ -z "$target_home" ] || [ ! -d "$target_home" ]; then
    die "HOME is invalid"
  fi
  appdir="$target_home/Library/Application Support/cc-remote"
  config_dir="$target_home/.cc-remote"
  device_file="$config_dir/device.json"
  service_file="$target_home/Library/LaunchAgents/com.muggle.cc-remote.wrapper.plist"
  service_label="com.muggle.cc-remote.wrapper"
  log_dir="$target_home/Library/Logs/cc-remote"
  cli_path="$target_home/.local/bin/cc-remote"
else
  [ "$(id -u)" -eq 0 ] || die "Linux wrapper installation must run as root"
  command -v systemctl >/dev/null 2>&1 || die "systemd is required"
  command -v getent >/dev/null 2>&1 || die "getent is required"
  if [ -z "$target_user" ] || [ "$target_user" = root ]; then
    target_user="${SUDO_USER:-}"
  fi
  if [ -z "$target_user" ] || [ "$target_user" = root ]; then
    die "Linux wrapper installation needs --user USER"
  fi
  case "$target_user" in
    *[!A-Za-z0-9_.-]*|"") die "Linux service user has an invalid name" ;;
  esac
  id "$target_user" >/dev/null 2>&1 || die "Linux service user does not exist"
  target_home="$(getent passwd "$target_user" | awk -F: '{print $6}')"
  if [ -z "$target_home" ] || [ ! -d "$target_home" ]; then
    die "Linux service user has no usable home directory"
  fi
  case "$target_home" in
    *["	 \"'\\"]*) die "Linux service user home contains unsupported characters" ;;
  esac
  appdir=/opt/cc-remote-wrapper
  config_dir=/etc/cc-remote
  device_file="$config_dir/device.env"
  service_file=/etc/systemd/system/cc-remote-wrapper.service
  service_label=cc-remote-wrapper
  log_dir=""
  cli_path=/usr/local/bin/cc-remote
fi

# Lock before reading rollback state, staging releases or changing credentials.
# Managed updates pass their open lock; direct installs acquire it and re-enter.
if [ -z "${CC_REMOTE_INSTALL_LOCK_FD:-}" ]; then
  exec "$bundle/bin/uv" run --no-project --no-env-file --managed-python \
    --python "$python_runtime" python "$bundle/deploy/install_lock.py" \
    "$appdir" bash "$bundle/deploy/install-wrapper.sh" "${installer_args[@]}"
fi
"$bundle/bin/uv" run --no-project --no-env-file --managed-python \
  --python "$python_runtime" python "$bundle/deploy/install_lock.py" \
  --verify-fd "$CC_REMOTE_INSTALL_LOCK_FD" "$appdir"

"$bundle/bin/uv" run --no-project --no-env-file --managed-python \
  --python "$python_runtime" python "$bundle/deploy/install_cli.py" \
  --destination "$cli_path" --check

claude_bin="$target_home/.local/bin/claude"
[ -x "$claude_bin" ] || \
  die "daily Claude Code executable is missing: $claude_bin"

releases="$appdir/releases"
current="$appdir/current"
runtimes="$appdir/runtimes"
target="$releases/$release_name"
rollback_root="$appdir/rollback-data"
stage=""
previous=""
switched=0
service_had_file=0
service_backup=""
service_changed=0
device_had_file=0
device_backup=""
device_changed=0
unit_verify_dir=""
rollback_snapshot=""
snapshot_created=0
service_stopped=0
service_was_running=0
activation_committed=0

mkdir -p "$releases" "$runtimes" "$rollback_root"
if [ "$system" = darwin ]; then
  mkdir -p "$config_dir" "$(dirname "$service_file")" "$log_dir"
  chmod 0700 "$config_dir" "$rollback_root"
else
  install -d -o root -g root -m 0755 "$appdir" "$releases" "$runtimes"
  install -d -o root -g root -m 0700 "$config_dir"
  install -d -o root -g root -m 0700 "$rollback_root"
fi

if [ -L "$current" ]; then
  previous="$(readlink "$current")"
  case "$previous" in
    "$releases"/*) [ -d "$previous" ] || die "current points to a missing release" ;;
    *) die "current must point inside $releases" ;;
  esac
elif [ -e "$current" ]; then
  die "current must be a symlink"
fi

restart_after_rollback() {
  if [ "$system" = darwin ]; then
    domain="gui/$(id -u)"
    launchctl bootout "$domain/$service_label" >/dev/null 2>&1 || true
    if [ "$service_was_running" -eq 1 ] && [ -n "$previous" ] && \
       [ -f "$service_file" ]; then
      launchctl bootstrap "$domain" "$service_file" >/dev/null 2>&1 &&
        launchctl kickstart -k "$domain/$service_label" >/dev/null 2>&1
    fi
  else
    systemctl daemon-reload >/dev/null 2>&1 || return 1
    if [ "$service_was_running" -eq 1 ] && [ -n "$previous" ] && \
       [ -f "$service_file" ]; then
      systemctl restart "$service_label" >/dev/null 2>&1
    else
      systemctl stop "$service_label" >/dev/null 2>&1 || true
    fi
  fi
}

stop_wrapper_service() {
  if [ "$system" = darwin ]; then
    domain="gui/$(id -u)"
    launchctl bootout "$domain/$service_label" >/dev/null 2>&1 || true
  else
    systemctl stop "$service_label" >/dev/null 2>&1 || true
  fi
  for _attempt in $(seq 1 10); do
    if ! wrapper_service_active; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wrapper_service_active() {
  if [ "$system" = darwin ]; then
    domain="gui/$(id -u)"
    launchctl print "$domain/$service_label" >/dev/null 2>&1
  else
    systemctl is-active --quiet "$service_label"
  fi
}

cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$status" -ne 0 ] && [ "$activation_committed" -eq 0 ]; then
    rollback_ready=1
    if [ "$snapshot_created" -eq 1 ]; then
      # Never start old code against data migrated by the failed new release.
      # Stop the new process first, then restore the SQLite images and every
      # participant in the matching private profile-migration transaction.
      if ! stop_wrapper_service; then
        rollback_ready=0
        echo "ERROR: new wrapper could not be stopped; data was not restored" >&2
      elif ! "$target/.venv/bin/python" \
          "$target/deploy/work_registry_snapshot.py" restore \
          --snapshot "$rollback_snapshot"; then
        rollback_ready=0
        echo "ERROR: wrapper data restore failed; wrapper remains stopped" >&2
      fi
    fi
    if [ "$switched" -eq 1 ]; then
      if [ -n "$previous" ]; then
        if ! "$target/.venv/bin/python" "$target/deploy/atomic_symlink.py" \
            "$previous" "$current" >/dev/null 2>&1; then
          rollback_ready=0
        fi
      elif [ -L "$current" ]; then
        rm -f -- "$current" || rollback_ready=0
      fi
    fi
    if [ "$service_changed" -eq 1 ]; then
      if [ "$service_had_file" -eq 1 ] && [ -n "$service_backup" ]; then
        cp -p "$service_backup" "$service_file" >/dev/null 2>&1 || \
          rollback_ready=0
      elif [ "$service_had_file" -eq 0 ] && [ -f "$service_file" ]; then
        rm -f -- "$service_file" || rollback_ready=0
      fi
    fi
    if [ "$device_changed" -eq 1 ] && [ "$device_had_file" -eq 1 ] &&
       [ -n "$device_backup" ]; then
      device_recovery="$device_file.failed-$release_name-$$"
      cp -p "$device_file" "$device_recovery" >/dev/null 2>&1 || \
        rollback_ready=0
      cp -p "$device_backup" "$device_file" >/dev/null 2>&1 || \
        rollback_ready=0
      echo "New device credential retained for recovery: $device_recovery" >&2
    elif [ "$device_changed" -eq 1 ]; then
      echo "New device credential retained for a retry: $device_file" >&2
    fi
    if [ "$rollback_ready" -eq 1 ] && { [ "$service_stopped" -eq 1 ] || \
       [ "$switched" -eq 1 ] || [ "$service_changed" -eq 1 ]; }; then
      if ! restart_after_rollback; then
        rollback_ready=0
      fi
    fi
    if [ "$rollback_ready" -eq 1 ]; then
      echo "ERROR: wrapper activation failed; code and wrapper data were restored" >&2
    else
      echo "ERROR: wrapper activation failed; manual data recovery is required" >&2
    fi
  elif [ "$status" -ne 0 ]; then
    echo "WARNING: Wrapper activation was committed; inspect it before retrying." >&2
  fi
  [ -z "$stage" ] || rm -rf -- "$stage"
  [ -z "$service_backup" ] || rm -f -- "$service_backup"
  [ -z "$device_backup" ] || rm -f -- "$device_backup"
  [ -z "$unit_verify_dir" ] || rm -rf -- "$unit_verify_dir"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

if [ -e "$service_file" ]; then
  if [ ! -f "$service_file" ] || [ -L "$service_file" ]; then
    die "$service_file must be a regular file"
  fi
  service_had_file=1
  service_backup="$(mktemp "${TMPDIR:-/tmp}/cc-remote-service.XXXXXX")"
  cp -p "$service_file" "$service_backup"
fi
if [ -e "$device_file" ]; then
  if [ ! -f "$device_file" ] || [ -L "$device_file" ]; then
    die "$device_file must be a regular file"
  fi
  device_had_file=1
  if [ -n "$pair_code" ] && [ "$replace_pair" -ne 1 ]; then
    die "$device_file already exists; pass --replace-pair to replace it"
  fi
  if [ -n "$pair_code" ]; then
    device_backup="$(mktemp "${TMPDIR:-/tmp}/cc-remote-device.XXXXXX")"
    cp -p "$device_file" "$device_backup"
  fi
fi

if [ -d "$target" ]; then
  [ -x "$target/.venv/bin/python" ] || \
    die "existing release is incomplete: $target"
  cmp -s "$manifest" "$target/release-manifest.json" || \
    die "existing release manifest differs: $target"
  echo "==> reusing installed wrapper release $release_name"
else
  stage="$(mktemp -d "$releases/.staging-$release_name.XXXXXX")"
  echo "==> staging wrapper release $release_name"
  tar -C "$bundle" -cf - . | tar -C "$stage" -xf -
  UV_PYTHON_INSTALL_DIR="$runtimes" \
    "$stage/bin/uv" venv --no-project --managed-python --relocatable \
    --python "$python_runtime" "$stage/.venv"
  "$stage/bin/uv" pip sync \
    --python "$stage/.venv/bin/python" \
    --require-hashes --only-binary=:all: \
    "$stage/requirements-wrapper.lock"
  (
    cd "$stage"
    PYTHONPATH="$stage" "$stage/.venv/bin/python" - <<'PY'
import claude_agent_sdk
import httpx
import PIL
import pydantic
import websockets
from cc_remote.wrapper.machine import WrapperMachine

assert WrapperMachine
PY
  )
  if [ "$system" = linux ]; then
    chown -R root:root "$runtimes"
    chmod -R a+rX,go-w "$runtimes"
    chown -R root:root "$stage"
    find "$stage" -type d -exec chmod go-w {} +
    find "$stage" -type f -exec chmod go-w {} +
  fi
  mv "$stage" "$target"
  stage=""
fi

if [ -n "$pair_code" ]; then
  pair_args=(pair "$relay" "$pair_code")
  if [ -n "$device_name" ]; then
    pair_args+=(--name "$device_name")
  fi
  if [ "$replace_pair" -eq 1 ]; then
    pair_args+=(--replace)
  fi
  if [ "$system" = linux ]; then
    pair_args+=(--env-file "$device_file")
  else
    pair_args+=(--config "$device_file")
  fi
  (
    cd "$target"
    PYTHONPATH="$target" "$target/.venv/bin/python" \
      -m cc_remote.device "${pair_args[@]}"
  )
  device_changed=1
fi

if [ ! -f "$device_file" ] || [ -L "$device_file" ]; then
  die "device credential is missing; provide --relay and --pair"
fi
chmod 0600 "$device_file"

# Mutable Work metadata and private wrapper control state live outside immutable
# release directories. Capture them before activation so a code rollback also
# restores the schemas understood by the previous wrapper.
if [ "$service_had_file" -eq 1 ]; then
  if [ "$system" = darwin ]; then
    domain="gui/$(id -u)"
    if launchctl print "$domain/$service_label" >/dev/null 2>&1; then
      service_was_running=1
    fi
  elif systemctl is-active --quiet "$service_label"; then
    service_was_running=1
  fi
fi
if [ "$service_was_running" -eq 1 ]; then
  stop_wrapper_service || die "existing wrapper could not be stopped safely"
  service_stopped=1
fi
rollback_snapshot="$(
  mktemp -d "$rollback_root/activation-$release_name.XXXXXX"
)"
snapshot_args=(
  snapshot
  --destination "$rollback_snapshot"
  --home "$target_home"
)
if [ "$system" = darwin ] && [ "$service_had_file" -eq 1 ]; then
  snapshot_args+=(--plist "$service_file")
elif [ "$system" = linux ] && [ -f "$config_dir/wrapper.env" ]; then
  snapshot_args+=(--env-file "$config_dir/wrapper.env")
fi
"$target/.venv/bin/python" "$target/deploy/work_registry_snapshot.py" \
  "${snapshot_args[@]}" >/dev/null
snapshot_created=1

if [ "$system" = darwin ]; then
  "$target/.venv/bin/python" - \
    "$target/deploy/com.muggle.cc-remote.wrapper.plist.in" \
    "$service_file" "$current" "$target_home" "$log_dir" \
    "$service_backup" <<'PY'
from pathlib import Path
import plistlib
import sys
from xml.sax.saxutils import escape

source, destination, current, home, log_dir = map(Path, sys.argv[1:6])
previous_plist = Path(sys.argv[6]) if sys.argv[6] else None
text = source.read_text(encoding="utf-8")
previous_environment = {}
work_roots = {
    "CLAUDE_WORK_ROOT": str(home / ".claude" / "cc-remote" / "work"),
    "CODEX_WORK_ROOT": str(home / ".codex" / "cc-remote" / "work"),
}
if previous_plist is not None:
    with previous_plist.open("rb") as stream:
        previous = plistlib.load(stream)
    environment = previous.get("EnvironmentVariables", {})
    if isinstance(environment, dict):
        previous_environment = environment
        for key in work_roots:
            value = environment.get(key)
            if isinstance(value, str) and value:
                work_roots[key] = value
values = {
    "__CURRENT__": escape(str(current)),
    "__HOME__": escape(str(home)),
    "__LOG_DIR__": escape(str(log_dir)),
    "__CLAUDE_WORK_ROOT__": escape(work_roots["CLAUDE_WORK_ROOT"]),
    "__CODEX_WORK_ROOT__": escape(work_roots["CODEX_WORK_ROOT"]),
}
for marker, value in values.items():
    text = text.replace(marker, value)
if any(marker in text for marker in values):
    raise SystemExit("unresolved LaunchAgent template marker")
staged = destination.with_name(f".{destination.name}.new")
payload = plistlib.loads(text.encode("utf-8"))
# Operator configuration (including the independent Claude service endpoint)
# survives a Wrapper upgrade. New installs still use the secret-free template.
payload["EnvironmentVariables"].update(previous_environment)
staged.write_bytes(plistlib.dumps(payload))
staged.replace(destination)
PY
  service_changed=1
  chmod 0644 "$service_file"
  plutil -lint "$service_file" >/dev/null
else
  if [ ! -e "$config_dir/wrapper.env" ]; then
    umask 077
    env_stage="$(mktemp "$config_dir/.wrapper.env.XXXXXX")"
    {
      printf 'CC_CWD=%s\n' "$target_home"
      printf 'CLAUDE_BIN=%s/.local/bin/claude\n' "$target_home"
      printf 'CLAUDE_WORK_ROOT=%s/.claude/cc-remote/work\n' "$target_home"
      printf 'CODEX_WORK_ROOT=%s/.codex/cc-remote/work\n' "$target_home"
      printf '%s\n' \
        'CC_REMOTE_CODEX_DAEMON=auto' \
        'WRAPPER_INBOX_CAP=1024' \
        'WRAPPER_SEND_QUEUE_CAP=8192' \
        'LOG_LEVEL=INFO'
    } > "$env_stage"
    install -o root -g root -m 0600 "$env_stage" "$config_dir/wrapper.env"
    rm -f -- "$env_stage"
  else
    if [ ! -f "$config_dir/wrapper.env" ] || \
        [ -L "$config_dir/wrapper.env" ]; then
      die "$config_dir/wrapper.env must be a regular file"
    fi
    chmod 0600 "$config_dir/wrapper.env"
  fi
  "$target/.venv/bin/python" - \
    "$target/deploy/cc-remote-wrapper.service" "$service_file" \
    "$target_user" "$current" "$target_home" <<'PY'
from pathlib import Path
import sys

source, destination = map(Path, sys.argv[1:3])
user, current, home = sys.argv[3:]
text = source.read_text(encoding="utf-8")
text = text.replace("youruser", user)
text = text.replace("/path/to/cc-remote", current)
text = text.replace(f"/home/{user}", home)
staged = destination.with_name(f".{destination.name}.new")
staged.write_text(text, encoding="utf-8")
staged.replace(destination)
PY
  service_changed=1
  chmod 0644 "$service_file"
  unit_verify_dir="$(mktemp -d /run/cc-remote-wrapper-unit.XXXXXX)"
  sed "s#$current#$target#g" "$service_file" \
    > "$unit_verify_dir/cc-remote-wrapper.service"
  systemd-analyze verify \
    "$unit_verify_dir/cc-remote-wrapper.service" >/dev/null
  rm -rf -- "$unit_verify_dir"
  unit_verify_dir=""
fi

"$target/.venv/bin/python" "$target/deploy/atomic_symlink.py" "$target" "$current"
switched=1
activation_started="$(date +%s)"

if [ "$system" = darwin ]; then
  domain="gui/$(id -u)"
  launchctl bootout "$domain/$service_label" >/dev/null 2>&1 || true
  launchctl bootstrap "$domain" "$service_file"
  launchctl kickstart -k "$domain/$service_label"
  launchctl print "$domain/$service_label" >/dev/null
else
  systemctl daemon-reload
  systemctl enable "$service_label"
  systemctl restart "$service_label"
  systemctl is-active --quiet "$service_label"
fi

migration_ready=0
for _attempt in $(seq 1 20); do
  if "$target/.venv/bin/python" \
      "$target/deploy/work_registry_snapshot.py" verify \
      --snapshot "$rollback_snapshot" >/dev/null 2>&1; then
    migration_ready=1
    break
  fi
  wrapper_service_active || break
  sleep 1
done
if [ "$migration_ready" -ne 1 ]; then
  "$target/.venv/bin/python" \
    "$target/deploy/work_registry_snapshot.py" verify \
    --snapshot "$rollback_snapshot" || true
  die "Claude/Codex Work profile migrations did not become ready"
fi

# The real Wrapper prepares and probes each account as the service user. Read
# its fresh result here; never run a user's Codex binary as the Linux installer.
codex_check_args=(--home "$target_home" --release "$target" --after "$activation_started")
if [ "$system" = darwin ]; then
  codex_check_args+=(--plist "$service_file")
else
  codex_check_args+=(--env-file "$config_dir/wrapper.env")
fi
echo "==> checking Codex shared connections (no model messages)"
if ! "$target/.venv/bin/python" "$target/deploy/check_codex_readiness.py" \
    "${codex_check_args[@]}"; then
  echo "WARNING: Wrapper is installed; Codex sharing still needs attention."
fi

# Keep registration after the interruptible readiness check. Once it succeeds,
# post-install output failures must not roll back a registered installation.
"$target/.venv/bin/python" "$target/deploy/install_cli.py" \
  --root "$appdir" --destination "$cli_path" --role wrapper --user "$target_user"
activation_committed=1

echo
echo "Wrapper v$version installed from $git_sha."
echo "Active release: $target"
if [ -n "$previous" ] && [ "$previous" != "$target" ]; then
  echo "Previous release retained for rollback: $previous"
  echo "Matching wrapper data snapshot: $rollback_snapshot"
fi
if [ "$system" = darwin ]; then
  echo "Logs: $log_dir"
else
  echo "Logs: journalctl -u $service_label -f"
fi
echo "Updates: $cli_path update (check only: $cli_path update --check)"
if [ "$system" = darwin ] && ! command -v cc-remote >/dev/null 2>&1; then
  echo 'Add ~/.local/bin to PATH to use the cc-remote command.'
fi
