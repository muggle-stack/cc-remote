#!/usr/bin/env bash
# vps setup (Ubuntu/Debian). Run as root/sudo:
#   sudo bash deploy/setup-vps.sh your-domain.com
#
# Assumes /opt/cc-remote already contains: the code, web/dist (from
# `npm --prefix web run build`), requirements.lock, and a filled-in .env (from
# deploy/env.relay.example — PUBLIC_ORIGIN, LOGIN_PASSWORD, SESSION_SECRET,
# WRAPPER_TOKEN).
set -euo pipefail

DOMAIN_INPUT="${1:?usage: sudo bash setup-vps.sh your-domain.com}"
# Browser Origin serialization lower-cases DNS hostnames.  Use that canonical
# spelling in Caddy and require the same spelling in PUBLIC_ORIGIN.
DOMAIN="${DOMAIN_INPUT,,}"
APPDIR=/opt/cc-remote
ENV_FILE="$APPDIR/.env"
VENV_STAGE=""
VENV_BACKUP="$APPDIR/.venv.previous"
VENV_SWAPPED=0
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
UNIT_CHANGED=0
UNIT_HAD_FILE=0
RELAY_SERVICE_TOUCHED=0
ROLLBACK_DONE=0

[ -r "$APPDIR/deploy/setup_transaction.sh" ] || {
  echo "ERROR: $APPDIR/deploy/setup_transaction.sh is missing" >&2
  exit 1
}
# shellcheck source=deploy/setup_transaction.sh
source "$APPDIR/deploy/setup_transaction.sh"
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
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || \
  die "Python 3.10 or newer is required (use Ubuntu 22.04+ or Debian 12+)"
[[ "$DOMAIN" == *.* && "$DOMAIN" != *..* &&
   "$DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])$ ]] ||
  die "domain must be a hostname such as cc.example.com (without a scheme or path)"

[ -f "$ENV_FILE" ] || die "$ENV_FILE missing (copy deploy/env.relay.example, fill tokens)"
[ ! -L "$ENV_FILE" ] || die "$ENV_FILE must be a regular file, not a symlink"
[ -d "$APPDIR/web/dist" ] || die "$APPDIR/web/dist missing (run 'npm --prefix web run build' on your dev machine, then rsync web/dist here)"
[ -s "$APPDIR/web/dist/index.html" ] || die "$APPDIR/web/dist/index.html missing or empty"
[ -s "$APPDIR/web/dist/cc-remote-build.json" ] || die "$APPDIR/web/dist/cc-remote-build.json missing"
grep -Eq '"protocol"[[:space:]]*:[[:space:]]*8' \
  "$APPDIR/web/dist/cc-remote-build.json" || die "web build protocol is not v8"
[ -f "$APPDIR/requirements.lock" ] || die "$APPDIR/requirements.lock missing"
[ -f "$APPDIR/deploy/Caddyfile" ] || die "$APPDIR/deploy/Caddyfile missing"
[ -f "$APPDIR/deploy/caddy_managed_block.py" ] || die "$APPDIR/deploy/caddy_managed_block.py missing"
[ -f "$APPDIR/deploy/setup_transaction.sh" ] || die "$APPDIR/deploy/setup_transaction.sh missing"
[ -f "$APPDIR/deploy/cc-remote-relay.service" ] || die "$APPDIR/deploy/cc-remote-relay.service missing"

require_secret LOGIN_PASSWORD 16
require_secret SESSION_SECRET 32
require_secret WRAPPER_TOKEN 32
CONFIGURED_ORIGIN="$(read_env_value PUBLIC_ORIGIN)" || \
  die "PUBLIC_ORIGIN is missing from $ENV_FILE"
[[ "$CONFIGURED_ORIGIN" == "https://$DOMAIN" ]] || \
  die "PUBLIC_ORIGIN must be exactly https://$DOMAIN"
CONFIGURED_RELAY_HOST="$(read_env_value RELAY_HOST)" || \
  die "RELAY_HOST is missing from $ENV_FILE"
CONFIGURED_RELAY_PORT="$(read_env_value RELAY_PORT)" || \
  die "RELAY_PORT is missing from $ENV_FILE"
CONFIGURED_STATIC_DIR="$(read_env_value WEB_STATIC_DIR)" || \
  die "WEB_STATIC_DIR is missing from $ENV_FILE"
[[ "$CONFIGURED_RELAY_HOST" == "127.0.0.1" ]] || \
  die "RELAY_HOST must be 127.0.0.1 for the bundled Caddy/systemd setup"
[[ "$CONFIGURED_RELAY_PORT" == "8765" ]] || \
  die "RELAY_PORT must be 8765 for the bundled Caddy/readiness setup"
[[ "$CONFIGURED_STATIC_DIR" == "$APPDIR/web/dist" ]] || \
  die "WEB_STATIC_DIR must be $APPDIR/web/dist"
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

# Never let the network-facing service account own code that this root installer
# executes on a later run. The service gets group read/execute only; ProtectSystem
# makes the same tree read-only again at runtime.
chown -R root:ccremote "$APPDIR"
chmod -R o-rwx "$APPDIR"
chmod -R g+rX "$APPDIR"
chown root:ccremote "$ENV_FILE"
chmod 0640 "$ENV_FILE"

echo "==> python venv + deps"
# Build and import-check a separate venv first. A network/package failure leaves
# the currently-running deployment untouched; only a complete stage is swapped.
VENV_STAGE="$(mktemp -d "$APPDIR/.venv.new.XXXXXX")"
python3 -m venv "$VENV_STAGE"
"$VENV_STAGE/bin/python" -m pip install --require-hashes --only-binary=:all: \
  -r "$APPDIR/requirements.lock"
PYTHONPATH="$APPDIR" "$VENV_STAGE/bin/python" -c \
  'import fastapi, pydantic, uvicorn, websockets, cc_remote.protocol'
chown -R root:ccremote "$VENV_STAGE"
chmod -R o-rwx "$VENV_STAGE"
chmod -R g+rX "$VENV_STAGE"
rm -rf "$VENV_BACKUP"
if [ -d "$APPDIR/.venv" ]; then
  mv "$APPDIR/.venv" "$VENV_BACKUP"
fi
mv "$VENV_STAGE" "$APPDIR/.venv"
VENV_STAGE=""
VENV_SWAPPED=1

echo "==> Caddy config (domain: $DOMAIN)"
CADDY_SITE="$(mktemp /etc/caddy/cc-remote-site.XXXXXX)"
CADDY_CANDIDATE="$(mktemp /etc/caddy/Caddyfile.cc-remote.XXXXXX)"
sed "s/cc-remote\.example\.com/$DOMAIN/g" "$APPDIR/deploy/Caddyfile" > "$CADDY_SITE"
python3 "$APPDIR/deploy/caddy_managed_block.py" \
  --current "$CADDYFILE" \
  --site "$CADDY_SITE" \
  --output "$CADDY_CANDIDATE" \
  --domain "$DOMAIN"
chmod 0644 "$CADDY_CANDIDATE"
caddy validate --config "$CADDY_CANDIDATE" --adapter caddyfile

if [ ! -f "$CADDYFILE" ] || ! cmp -s "$CADDY_CANDIDATE" "$CADDYFILE"; then
  if [ -f "$CADDYFILE" ]; then
    CADDY_BACKUP="$(mktemp /etc/caddy/Caddyfile.cc-remote.bak.XXXXXX)"
    cp -a "$CADDYFILE" "$CADDY_BACKUP"
    CADDY_HAD_CONFIG=1
  fi
  install -o root -g root -m 0644 "$CADDY_CANDIDATE" "$CADDYFILE"
  CADDY_CHANGED=1
fi

systemctl enable caddy
CADDY_SERVICE_TOUCHED=1
if ! systemctl restart caddy; then
  die "Caddy failed to restart with the staged config"
fi

echo "==> relay systemd service"
systemd-analyze verify "$APPDIR/deploy/cc-remote-relay.service"
if [ ! -f "$RELAY_UNIT_FILE" ] || \
   ! cmp -s "$APPDIR/deploy/cc-remote-relay.service" "$RELAY_UNIT_FILE"; then
  if [ -f "$RELAY_UNIT_FILE" ]; then
    UNIT_BACKUP="$(mktemp /etc/systemd/system/cc-remote-relay.service.bak.XXXXXX)"
    cp -a "$RELAY_UNIT_FILE" "$UNIT_BACKUP"
    UNIT_HAD_FILE=1
  fi
  install -o root -g root -m 0644 "$APPDIR/deploy/cc-remote-relay.service" \
    "$RELAY_UNIT_FILE"
  UNIT_CHANGED=1
fi
systemctl daemon-reload
systemctl enable cc-remote-relay
RELAY_SERVICE_TOUCHED=1
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

DEPLOY_READY=1
rm -rf "$VENV_BACKUP"

echo
echo "Done. Check:"
echo "  https://$DOMAIN/healthz   (should show {\"ok\":true,...})"
echo "  https://$DOMAIN/          (web client)"
echo "  journalctl -u cc-remote-relay -f"
echo "  journalctl -u caddy -f"
