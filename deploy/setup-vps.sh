#!/usr/bin/env bash
# vps setup (Ubuntu/Debian). Run as root/sudo:
#   sudo bash /path/to/upload/deploy/setup-vps.sh \
#     domain-or-public-ip [/path/to/upload]
#
# Copies one validated upload into an immutable /opt/cc-remote/releases entry,
# builds its venv in place, and atomically switches /opt/cc-remote/current only
# after staging succeeds. /opt/cc-remote/.env remains shared across releases.
set -euo pipefail

TARGET_INPUT="${1:?usage: sudo bash setup-vps.sh domain-or-public-ip}"
# Browser Origin serialization lower-cases DNS hostnames. Use that canonical
# spelling in Caddy and require the same spelling in PUBLIC_ORIGIN.
TARGET="${TARGET_INPUT,,}"
SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SOURCE_DIR="$(cd "${2:-$SCRIPT_ROOT}" && pwd -P)"
APPDIR=/opt/cc-remote
ENV_FILE="$APPDIR/.env"
RELEASES_DIR="$APPDIR/releases"
STATE_DIR="$APPDIR/state"
RUNTIMES_DIR="$APPDIR/runtimes"
CURRENT_LINK="$APPDIR/current"
NEW_RELEASE_DIR=""
PREVIOUS_RELEASE=""
RELEASE_SWITCHED=0
DEPLOY_READY=0
CADDYFILE=/etc/caddy/Caddyfile
CADDY_BACKUP=""
CADDY_SITE=""
CADDY_CANDIDATE=""
CADDY_CHANGED=0
CADDY_HAD_CONFIG=0
CADDY_SERVICE_TOUCHED=0
RELAY_UNIT_FILE=/etc/systemd/system/cc-remote-relay.service
UNIT_BACKUP=""
UNIT_VERIFY_DIR=""
UNIT_CHANGED=0
UNIT_HAD_FILE=0
RELAY_SERVICE_TOUCHED=0
ROLLBACK_DONE=0
INSECURE_HTTP=0
PRIVATE_DIRECT=0
PUBLIC_SCHEME=https
CADDY_TEMPLATE=""
MANAGED_RELEASE=0
CLI_PATH=/usr/local/bin/cc-remote

[ -r "$SOURCE_DIR/deploy/setup_transaction.sh" ] || {
  echo "ERROR: $SOURCE_DIR/deploy/setup_transaction.sh is missing" >&2
  exit 1
}
# shellcheck source=deploy/setup_transaction.sh
source "$SOURCE_DIR/deploy/setup_transaction.sh"
trap cleanup EXIT

die() {
  echo "ERROR: $*" >&2
  exit 1
}

read_env_value() {
  local key="$1" value
  value="$(awk -v wanted="$key" '
    /^[[:space:]]*#/ { next }
    {
      line = $0
      sub(/^[[:space:]]*/, "", line)
      pos = index(line, "=")
      if (!pos) next
      name = substr(line, 1, pos - 1)
      gsub(/[[:space:]]/, "", name)
      if (name == wanted) {
        value = substr(line, pos + 1)
        found = 1
      }
    }
    END {
      if (!found) exit 1
      printf "%s", value
    }
  ' "$ENV_FILE")" || return 1

  # Match python-dotenv's common KEY=value / KEY="value" forms without
  # sourcing a root-owned file as shell code.
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  if [[ ${#value} -ge 2 ]]; then
    if [[ ${value:0:1} == '"' && ${value: -1} == '"' ]] ||
       [[ ${value:0:1} == "'" && ${value: -1} == "'" ]]; then
      value="${value:1:${#value}-2}"
    fi
  fi
  printf '%s' "$value"
}

require_secret() {
  local key="$1" min_length="$2" value normalized
  value="$(read_env_value "$key")" || die "$key is missing from $ENV_FILE"
  normalized="${value,,}"
  case "$normalized" in
    ""|replace_with*|change-me*|changeme*|your_*|your-*)
      die "$key still contains a placeholder"
      ;;
  esac
  if (( ${#value} < min_length )); then
    die "$key must be at least $min_length characters"
  fi
}

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "run this script as root (sudo)"
command -v python3 >/dev/null 2>&1 || die "python3 is required (3.10 or newer)"
command -v flock >/dev/null 2>&1 || die "flock is required (install util-linux)"
command -v tar >/dev/null 2>&1 || die "tar is required"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || \
  die "Python 3.10 or newer is required (use Ubuntu 22.04+ or Debian 12+)"

# Backups and current are process-wide resources. Serializing installers keeps
# one failed deployment from rolling back or deleting another one's release.
exec 9>/run/lock/cc-remote-deploy.lock
flock -n 9 || die "another cc-remote deployment is already running"

[ -f "$ENV_FILE" ] || die "$ENV_FILE missing (copy deploy/env.relay.example, fill tokens)"
[ ! -L "$ENV_FILE" ] || die "$ENV_FILE must be a regular file, not a symlink"
ALLOW_INSECURE_VALUE="$(read_env_value ALLOW_INSECURE_HTTP 2>/dev/null || printf '0')"
case "${ALLOW_INSECURE_VALUE,,}" in
  1|true|yes|on)
    INSECURE_HTTP=1
    PUBLIC_SCHEME=http
    CADDY_TEMPLATE="Caddyfile.insecure"
    python3 - "$TARGET" <<'PY' || \
      die "ALLOW_INSECURE_HTTP=1 requires a public IPv4 address as the setup target"
import ipaddress
import sys

address = ipaddress.ip_address(sys.argv[1])
raise SystemExit(not (
    address.version == 4 and address.is_global and not address.is_multicast
))
PY
    ;;
  0|false|no|off|"")
    [[ "$TARGET" == *.* && "$TARGET" != *..* &&
       "$TARGET" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])$ &&
       ! "$TARGET" =~ ^[0-9.]+$ ]] ||
      die "TLS mode requires a hostname such as cc.example.com (without a scheme or path)"
    CADDY_TEMPLATE="Caddyfile"
    ;;
  *)
    die "ALLOW_INSECURE_HTTP must be one of 1/true/yes/on or 0/false/no/off"
    ;;
esac
ALLOW_PRIVATE_VALUE="$(
  read_env_value ALLOW_PRIVATE_ORIGINS 2>/dev/null || printf '0'
)"
case "${ALLOW_PRIVATE_VALUE,,}" in
  1|true|yes|on) PRIVATE_DIRECT=1 ;;
  0|false|no|off|"") ;;
  *)
    die "ALLOW_PRIVATE_ORIGINS must be one of 1/true/yes/on or 0/false/no/off"
    ;;
esac
[ -d "$SOURCE_DIR/web/dist" ] || die "$SOURCE_DIR/web/dist missing (run 'npm --prefix web run build' before uploading)"
[ -s "$SOURCE_DIR/web/dist/index.html" ] || die "$SOURCE_DIR/web/dist/index.html missing or empty"
[ -s "$SOURCE_DIR/web/dist/cc-remote-build.json" ] || die "$SOURCE_DIR/web/dist/cc-remote-build.json missing"
[ -s "$SOURCE_DIR/cc_remote/__init__.py" ] || die "$SOURCE_DIR/cc_remote/__init__.py missing"
[ -s "$SOURCE_DIR/cc_remote/protocol.py" ] || die "$SOURCE_DIR/cc_remote/protocol.py missing"
[ -s "$SOURCE_DIR/deploy/validate_protocol_bundle.py" ] || \
  die "$SOURCE_DIR/deploy/validate_protocol_bundle.py missing"
[ -s "$SOURCE_DIR/deploy/python-version.txt" ] || \
  die "$SOURCE_DIR/deploy/python-version.txt missing"
PYTHON_RUNTIME="$(tr -d '[:space:]' < "$SOURCE_DIR/deploy/python-version.txt")"
[[ "$PYTHON_RUNTIME" =~ ^3\.13\.[0-9]+$ ]] || \
  die "deploy/python-version.txt must pin a Python 3.13 patch"
python3 "$SOURCE_DIR/deploy/validate_protocol_bundle.py" \
  "$SOURCE_DIR/cc_remote/protocol.py" \
  "$SOURCE_DIR/web/dist/cc-remote-build.json" >/dev/null || \
  die "web build metadata does not match backend"
[ -f "$SOURCE_DIR/requirements-relay.lock" ] || \
  die "$SOURCE_DIR/requirements-relay.lock missing"
[ -f "$SOURCE_DIR/deploy/$CADDY_TEMPLATE" ] || \
  die "$SOURCE_DIR/deploy/$CADDY_TEMPLATE missing"
[ -f "$SOURCE_DIR/deploy/caddy_managed_block.py" ] || die "$SOURCE_DIR/deploy/caddy_managed_block.py missing"
[ -f "$SOURCE_DIR/deploy/cc-remote-relay.service" ] || die "$SOURCE_DIR/deploy/cc-remote-relay.service missing"

if [ -f "$SOURCE_DIR/release-manifest.json" ]; then
  (
    cd "$SOURCE_DIR"
    python3 -m deploy.release_manifest "$SOURCE_DIR" --role relay --os linux
  )
  python3 "$SOURCE_DIR/deploy/install_cli.py" --destination "$CLI_PATH" --check
  MANAGED_RELEASE=1
fi

require_secret SESSION_SECRET 32
if LOGIN_USERS_POLICY="$(read_env_value LOGIN_USERS_JSON 2>/dev/null)" && \
   [[ -n "$LOGIN_USERS_POLICY" ]]; then
  : # Parsed and strength-checked by validate_relay_config below.
else
  require_secret LOGIN_PASSWORD 16
fi
if WRAPPER_TOKENS_POLICY="$(read_env_value WRAPPER_TOKENS_JSON 2>/dev/null)" && \
   [[ -n "$WRAPPER_TOKENS_POLICY" ]]; then
  : # Parsed and strength-checked by validate_relay_config below.
else
  require_secret WRAPPER_TOKEN 32
fi
CONFIGURED_ORIGIN="$(read_env_value PUBLIC_ORIGIN)" || \
  die "PUBLIC_ORIGIN is missing from $ENV_FILE"
[[ "$CONFIGURED_ORIGIN" == "$PUBLIC_SCHEME://$TARGET" ]] || \
  die "PUBLIC_ORIGIN must be exactly $PUBLIC_SCHEME://$TARGET"
CONFIGURED_RELAY_HOST="$(read_env_value RELAY_HOST)" || \
  die "RELAY_HOST is missing from $ENV_FILE"
CONFIGURED_RELAY_PORT="$(read_env_value RELAY_PORT)" || \
  die "RELAY_PORT is missing from $ENV_FILE"
CONFIGURED_STATIC_DIR="$(read_env_value WEB_STATIC_DIR)" || \
  die "WEB_STATIC_DIR is missing from $ENV_FILE"
if [[ "$PRIVATE_DIRECT" -eq 1 ]]; then
  [[ "$CONFIGURED_RELAY_HOST" == "0.0.0.0" ]] || \
    die "RELAY_HOST must be 0.0.0.0 when ALLOW_PRIVATE_ORIGINS is enabled"
  echo "WARNING: relay port 8765 is exposed on every IPv4 interface."
  echo "Restrict direct access to trusted LAN/Tailscale peers with a firewall."
else
  [[ "$CONFIGURED_RELAY_HOST" == "127.0.0.1" ]] || \
    die "RELAY_HOST must be 127.0.0.1 unless ALLOW_PRIVATE_ORIGINS is enabled"
fi
[[ "$CONFIGURED_RELAY_PORT" == "8765" ]] || \
  die "RELAY_PORT must be 8765 for the bundled Caddy/readiness setup"
[[ "$CONFIGURED_STATIC_DIR" == "$CURRENT_LINK/web/dist" ]] || \
  die "WEB_STATIC_DIR must be $CURRENT_LINK/web/dist"
chmod 0600 "$ENV_FILE"

echo "==> installing system deps (python3-venv) + Caddy (official repo)"
apt-get update -y
apt-get install -y python3-venv debian-keyring debian-archive-keyring apt-transport-https curl gnupg
command -v gpg >/dev/null 2>&1 || die "gnupg/gpg is required to install the Caddy repository key"
curl -1sLf "https://dl.cloudsmith.io/public/caddy/stable/gpg.key" | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf "https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt" > /etc/apt/sources.list.d/caddy-stable.list
apt-get update -y
apt-get install -y caddy

echo "==> creating service user"
getent group ccremote >/dev/null 2>&1 || groupadd --system ccremote
id -u ccremote >/dev/null 2>&1 || \
  useradd --system --gid ccremote --no-create-home --home-dir /nonexistent \
    --shell /usr/sbin/nologin ccremote

mkdir -p "$RELEASES_DIR" "$STATE_DIR" "$RUNTIMES_DIR"
chown root:ccremote "$APPDIR" "$RELEASES_DIR"
chown ccremote:ccremote "$STATE_DIR"
chown root:ccremote "$RUNTIMES_DIR"
chmod 0750 "$APPDIR" "$RELEASES_DIR" "$STATE_DIR" "$RUNTIMES_DIR"
chown root:ccremote "$ENV_FILE"
chmod 0640 "$ENV_FILE"

if [ -L "$CURRENT_LINK" ]; then
  PREVIOUS_RELEASE="$(readlink -f "$CURRENT_LINK")"
  case "$PREVIOUS_RELEASE" in
    "$RELEASES_DIR"/*) [ -d "$PREVIOUS_RELEASE" ] || \
      die "$CURRENT_LINK points to a missing release" ;;
    *) die "$CURRENT_LINK must point inside $RELEASES_DIR" ;;
  esac
elif [ -e "$CURRENT_LINK" ]; then
  die "$CURRENT_LINK must be a symlink, not a regular file or directory"
elif [ -d "$APPDIR/cc_remote" ] && [ -d "$APPDIR/.venv" ]; then
  # One-time migration from the legacy overlay layout. Copy, do not move: the
  # old service keeps reading the original tree until current switches.
  PREVIOUS_RELEASE="$(mktemp -d "$RELEASES_DIR/legacy-$(date -u +%Y%m%dT%H%M%SZ).XXXXXX")"
  while IFS= read -r -d '' item; do
    case "$(basename "$item")" in
      .env|current|releases|runtimes|state) continue ;;
    esac
    cp -a "$item" "$PREVIOUS_RELEASE/"
  done < <(find "$APPDIR" -mindepth 1 -maxdepth 1 -print0)
fi

if [ -n "$PREVIOUS_RELEASE" ]; then
  # Re-assert the invariant for both a freshly copied legacy baseline and a
  # current release produced by an older installer that preserved g+w bits.
  chown -R root:ccremote "$PREVIOUS_RELEASE"
  harden_release_permissions "$PREVIOUS_RELEASE"
fi

# During the one-time overlay migration, WEB_STATIC_DIR is changed to the
# stable current path before this installer runs. Establish a complete v15
# baseline link immediately so any pre-activation failure that restarts the old
# unit still has a valid same-version web tree. This baseline is not the new
# release switch and therefore must not set RELEASE_SWITCHED.
if [ ! -L "$CURRENT_LINK" ] && [ -n "$PREVIOUS_RELEASE" ]; then
  atomic_release_link "$PREVIOUS_RELEASE" "$CURRENT_LINK"
fi

echo "==> staging immutable release"
NEW_RELEASE_DIR="$(mktemp -d "$RELEASES_DIR/release-$(date -u +%Y%m%dT%H%M%SZ).XXXXXX")"
# The upload may itself be /opt/cc-remote during a one-time migration. Exclude
# shared/runtime paths so release creation never recursively copies releases or
# imports mutable secrets and old virtualenvs.
tar -C "$SOURCE_DIR" \
  --exclude='./.git' --exclude='./.env' --exclude='./.venv' \
  --exclude='./current' --exclude='./releases' \
  --exclude='./runtimes' --exclude='./state' \
  --exclude='./web/node_modules' -cf - . | tar -C "$NEW_RELEASE_DIR" -xf -

[ -s "$NEW_RELEASE_DIR/cc_remote/protocol.py" ] || die "staged backend missing"
[ -s "$NEW_RELEASE_DIR/web/dist/index.html" ] || die "staged web build missing"
python3 "$NEW_RELEASE_DIR/deploy/validate_protocol_bundle.py" \
  "$NEW_RELEASE_DIR/cc_remote/protocol.py" \
  "$NEW_RELEASE_DIR/web/dist/cc-remote-build.json" >/dev/null || \
  die "staged web build metadata does not match backend"

echo "==> release-local python venv + deps"
if [ -x "$NEW_RELEASE_DIR/bin/uv" ]; then
  UV_PYTHON_INSTALL_DIR="$RUNTIMES_DIR" \
    "$NEW_RELEASE_DIR/bin/uv" venv \
    --no-project --managed-python --python "$PYTHON_RUNTIME" \
    "$NEW_RELEASE_DIR/.venv"
  "$NEW_RELEASE_DIR/bin/uv" pip sync \
    --python "$NEW_RELEASE_DIR/.venv/bin/python" \
    --require-hashes --only-binary=:all: --no-binary=http-ece \
    "$NEW_RELEASE_DIR/requirements-relay.lock"
else
  # Manual source deployments retain the Python 3.10+ path documented before
  # v3 release bundles. Published assets always take the managed-Python branch.
  python3 -m venv "$NEW_RELEASE_DIR/.venv"
  "$NEW_RELEASE_DIR/.venv/bin/python" -m pip install \
    --require-hashes --only-binary=:all: --no-binary=http-ece \
    -r "$NEW_RELEASE_DIR/requirements-relay.lock"
fi
(
  cd "$NEW_RELEASE_DIR"
  CC_REMOTE_ENV_FILE="$ENV_FILE" \
  WEB_STATIC_DIR="$NEW_RELEASE_DIR/web/dist" \
  PYTHONPATH="$NEW_RELEASE_DIR" "$NEW_RELEASE_DIR/.venv/bin/python" -c \
    'import os; from dotenv import load_dotenv; load_dotenv(os.environ["CC_REMOTE_ENV_FILE"]); import fastapi, httpx, pydantic, uvicorn, websockets; from cc_remote.config import relay_config, validate_relay_config; validate_relay_config(relay_config())'
)
chown -R root:ccremote "$NEW_RELEASE_DIR"
harden_release_permissions "$NEW_RELEASE_DIR"
chown -R root:ccremote "$RUNTIMES_DIR"
harden_release_permissions "$RUNTIMES_DIR"

echo "==> Caddy config ($PUBLIC_SCHEME://$TARGET)"
CADDY_SITE="$(mktemp /etc/caddy/cc-remote-site.XXXXXX)"
CADDY_CANDIDATE="$(mktemp /etc/caddy/Caddyfile.cc-remote.XXXXXX)"
sed "s/cc-remote\.example\.com/$TARGET/g" \
  "$NEW_RELEASE_DIR/deploy/$CADDY_TEMPLATE" > "$CADDY_SITE"
python3 "$NEW_RELEASE_DIR/deploy/caddy_managed_block.py" \
  --current "$CADDYFILE" \
  --site "$CADDY_SITE" \
  --output "$CADDY_CANDIDATE" \
  --domain "$TARGET"
chmod 0644 "$CADDY_CANDIDATE"
caddy validate --config "$CADDY_CANDIDATE" --adapter caddyfile

if [ ! -f "$CADDYFILE" ] || ! cmp -s "$CADDY_CANDIDATE" "$CADDYFILE"; then
  if [ -f "$CADDYFILE" ]; then
    CADDY_BACKUP="$(mktemp /etc/caddy/Caddyfile.cc-remote.bak.XXXXXX)"
    cp -a "$CADDYFILE" "$CADDY_BACKUP"
    CADDY_HAD_CONFIG=1
  fi
  # Register the mutation before touching the destination. If staging or the
  # atomic replace fails, EXIT cleanup restores the backup (or removes a partial
  # first-install destination) instead of discarding the only recovery copy.
  CADDY_CHANGED=1
  atomic_install_file "$CADDY_CANDIDATE" "$CADDYFILE" root root 0644
fi

systemctl enable caddy
CADDY_SERVICE_TOUCHED=1
if ! systemctl restart caddy; then
  die "Caddy failed to restart with the staged config"
fi

echo "==> relay systemd service"
# Verify the exact unit structure against staged paths before changing current.
# This lets the old relay and old web stay paired until the final stop/switch.
UNIT_VERIFY_DIR="$(mktemp -d /run/cc-remote-unit.XXXXXX)"
sed "s#/opt/cc-remote/current#$NEW_RELEASE_DIR#g" \
  "$NEW_RELEASE_DIR/deploy/cc-remote-relay.service" \
  > "$UNIT_VERIFY_DIR/cc-remote-relay.service"
systemd-analyze verify "$UNIT_VERIFY_DIR/cc-remote-relay.service"
rm -rf -- "$UNIT_VERIFY_DIR"
UNIT_VERIFY_DIR=""
if [ ! -f "$RELAY_UNIT_FILE" ] || \
   ! cmp -s "$NEW_RELEASE_DIR/deploy/cc-remote-relay.service" "$RELAY_UNIT_FILE"; then
  if [ -f "$RELAY_UNIT_FILE" ]; then
    UNIT_BACKUP="$(mktemp /etc/systemd/system/cc-remote-relay.service.bak.XXXXXX)"
    cp -a "$RELAY_UNIT_FILE" "$UNIT_BACKUP"
    UNIT_HAD_FILE=1
  fi
  UNIT_CHANGED=1
  atomic_install_file \
    "$NEW_RELEASE_DIR/deploy/cc-remote-relay.service" \
    "$RELAY_UNIT_FILE" root root 0644
fi
systemctl daemon-reload
systemctl enable cc-remote-relay

RELAY_SERVICE_TOUCHED=1
# Do not let the old relay process run while current already serves the new web
# bundle. Protocol versions are strict, so stop first, switch once, then start.
if ! systemctl stop cc-remote-relay; then
  die "could not stop the previous relay for the atomic release switch"
fi
echo "==> atomically activating release"
RELEASE_SWITCHED=1
atomic_release_link "$NEW_RELEASE_DIR" "$CURRENT_LINK"
if ! systemctl restart cc-remote-relay; then
  die "relay failed to restart with the staged environment"
fi

echo "==> waiting for relay readiness"
READY=0
for _ in $(seq 1 20); do
  if curl --fail --silent --show-error --max-time 2 \
      http://127.0.0.1:8765/healthz >/dev/null; then
    READY=1
    break
  fi
  sleep 1
done
if (( ! READY )); then
  systemctl status --no-pager cc-remote-relay || true
  die "relay did not become ready on http://127.0.0.1:8765/healthz"
fi

# Registration is the final fallible activation step. Its own atomic writer
# restores the command on failure; our EXIT trap restores Relay/Caddy/current.
# Source deployments without a release manifest do not become managed installs.
if (( MANAGED_RELEASE )); then
  "$NEW_RELEASE_DIR/.venv/bin/python" "$NEW_RELEASE_DIR/deploy/install_cli.py" \
    --root "$APPDIR" --destination "$CLI_PATH" --role relay --domain "$TARGET"
fi
DEPLOY_READY=1

echo
echo "Active release: $NEW_RELEASE_DIR"
if [ -n "$PREVIOUS_RELEASE" ]; then
  echo "Previous release retained for rollback: $PREVIOUS_RELEASE"
fi
echo "Done. Check:"
echo "  $PUBLIC_SCHEME://$TARGET/healthz   (should show {\"ok\":true,...})"
echo "  $PUBLIC_SCHEME://$TARGET/          (web client)"
if (( INSECURE_HTTP )); then
  echo "  WARNING: login credentials, cookies, and session traffic are unencrypted"
fi
echo "  journalctl -u cc-remote-relay -f"
echo "  journalctl -u caddy -f"
