#!/usr/bin/env bash
# First-install/update entrypoint for a role-scoped relay release bundle.
set -euo pipefail
installer_args=("$@")

die() {
  echo "ERROR: $*" >&2
  exit 1
}

usage() {
  echo "usage: install-relay.sh BUNDLE --domain remote.example.com [--allow-private-origins]" >&2
  exit 2
}

bundle="${1:-}"
[ -n "$bundle" ] || usage
shift
domain=""
allow_private_origins=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --domain)
      [ "$#" -ge 2 ] || usage
      domain="$2"
      shift 2
      ;;
    --allow-private-origins)
      allow_private_origins=1
      shift
      ;;
    *) die "unknown relay installer argument: $1" ;;
  esac
done
[ -n "$domain" ] || usage
domain="$(printf '%s' "$domain" | tr '[:upper:]' '[:lower:]')"
case "$domain" in
  *[!a-z0-9.-]*|*..*|.*|*.) die "domain must be a DNS hostname without a scheme or path" ;;
esac
case "$domain" in
  *.*) ;;
  *) die "domain must be a DNS hostname such as remote.example.com" ;;
esac

[ "$(id -u)" -eq 0 ] || die "relay installation must run as root"
[ "$(uname -s)" = Linux ] || die "relay installation requires Linux"
[ -r /etc/os-release ] || die "/etc/os-release is required"
# shellcheck disable=SC1091
source /etc/os-release
case "${ID:-}" in
  ubuntu) minimum_major=22 ;;
  debian) minimum_major=12 ;;
  *) die "relay supports Ubuntu 22.04+ and Debian 12+" ;;
esac
os_major="${VERSION_ID%%.*}"
case "$os_major" in
  ""|*[!0-9]*) die "/etc/os-release has an invalid VERSION_ID" ;;
esac
os_major=$((10#$os_major))
[ "$os_major" -ge "$minimum_major" ] || \
  die "relay supports Ubuntu 22.04+ and Debian 12+"
command -v systemctl >/dev/null 2>&1 || die "systemd is required"
command -v openssl >/dev/null 2>&1 || die "openssl is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"

case "$(uname -m)" in
  x86_64|amd64) machine=x86_64 ;;
  arm64|aarch64) machine=arm64 ;;
  *) die "unsupported architecture: $(uname -m)" ;;
esac
bundle="$(cd "$bundle" && pwd -P)"
(
  cd "$bundle"
  python3 -m deploy.release_manifest \
    "$bundle" --role relay --os linux --arch "$machine"
)

appdir=/opt/cc-remote
if [ -z "${CC_REMOTE_INSTALL_LOCK_FD:-}" ]; then
  exec python3 "$bundle/deploy/install_lock.py" \
    "$appdir" bash "$bundle/deploy/install-relay.sh" "${installer_args[@]}"
fi
python3 "$bundle/deploy/install_lock.py" --verify-fd "$CC_REMOTE_INSTALL_LOCK_FD" "$appdir"
env_file="$appdir/.env"
cli_path=/usr/local/bin/cc-remote
python3 "$bundle/deploy/install_cli.py" --destination "$cli_path" --check
new_env=""
cleanup() {
  [ -z "$new_env" ] || rm -f -- "$new_env"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

if [ ! -e "$env_file" ]; then
  password=""
  if [ -n "${CC_REMOTE_LOGIN_PASSWORD_FILE:-}" ]; then
    password_file="$CC_REMOTE_LOGIN_PASSWORD_FILE"
    if [ ! -f "$password_file" ] || [ -L "$password_file" ]; then
      die "CC_REMOTE_LOGIN_PASSWORD_FILE must be a regular file"
    fi
    IFS= read -r password < "$password_file" || true
  else
    [ -t 0 ] || die \
      "interactive login password required (or set CC_REMOTE_LOGIN_PASSWORD_FILE)"
    printf 'Web login password (minimum 16 characters): ' >&2
    IFS= read -r -s password
    printf '\nRepeat web login password: ' >&2
    IFS= read -r -s password_repeat
    printf '\n' >&2
    [ "$password" = "$password_repeat" ] || die "login passwords do not match"
  fi
  [ "${#password}" -ge 16 ] || die "login password must be at least 16 characters"
  [ "${#password}" -le 1024 ] || die "login password must be at most 1024 characters"
  if printf '%s' "$password" | LC_ALL=C grep -q '[[:cntrl:]]'; then
    die "login password cannot contain control characters"
  fi
  case "$password" in
    *"'"*|*\\*) die "login password cannot contain a single quote or backslash" ;;
  esac

  session_secret="$(openssl rand -hex 32)"
  wrapper_token="$(openssl rand -hex 32)"
  [ "${#session_secret}" -eq 64 ] || die "could not generate SESSION_SECRET"
  [ "${#wrapper_token}" -eq 64 ] || die "could not generate WRAPPER_TOKEN"

  install -d -o root -g root -m 0755 "$appdir"
  umask 077
  new_env="$(mktemp "$appdir/.env.new.XXXXXX")"
  relay_host=127.0.0.1
  if [ "$allow_private_origins" -eq 1 ]; then
    relay_host=0.0.0.0
  fi
  {
    printf '%s\n' \
      "RELAY_HOST=${relay_host}" \
      'RELAY_PORT=8765' \
      "PUBLIC_ORIGIN=https://${domain}" \
      "ALLOW_PRIVATE_ORIGINS=${allow_private_origins}" \
      "LOGIN_PASSWORD='${password}'" \
      "SESSION_SECRET=${session_secret}" \
      "WRAPPER_TOKEN=${wrapper_token}" \
      'WEB_STATIC_DIR=/opt/cc-remote/current/web/dist' \
      'PUSH_DB_PATH=/opt/cc-remote/state/relay-push.sqlite3' \
      'DEVICE_DB_PATH=/opt/cc-remote/state/relay-devices.sqlite3' \
      'LOG_LEVEL=INFO'
  } > "$new_env"
  install -o root -g root -m 0600 "$new_env" "$env_file"
  rm -f -- "$new_env"
  new_env=""
  unset password password_repeat session_secret wrapper_token relay_host
  echo "==> created root-only relay configuration"
  if [ "$allow_private_origins" -eq 1 ]; then
    echo "WARNING: relay port 8765 will listen on every IPv4 interface."
    echo "Restrict direct access to trusted LAN/Tailscale peers with a firewall."
  fi
else
  if [ ! -f "$env_file" ] || [ -L "$env_file" ]; then
    die "$env_file must be a regular file"
  fi
  if [ "$allow_private_origins" -eq 1 ]; then
    private_setting="$(
      awk -F= '
        /^[[:space:]]*ALLOW_PRIVATE_ORIGINS[[:space:]]*=/ {
          value = $0
          sub(/^[^=]*=/, "", value)
          gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
          print tolower(value)
        }
      ' "$env_file" | tail -n 1
    )"
    relay_setting="$(
      awk -F= '
        /^[[:space:]]*RELAY_HOST[[:space:]]*=/ {
          value = $0
          sub(/^[^=]*=/, "", value)
          gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
          print value
        }
      ' "$env_file" | tail -n 1
    )"
    case "$private_setting" in
      1|true|yes|on) ;;
      *) die "--allow-private-origins requires ALLOW_PRIVATE_ORIGINS=1 in the existing $env_file" ;;
    esac
    [ "$relay_setting" = "0.0.0.0" ] || \
      die "--allow-private-origins requires RELAY_HOST=0.0.0.0 in the existing $env_file"
  fi
  echo "==> preserving existing relay configuration"
fi

bash "$bundle/deploy/setup-vps.sh" "$domain" "$bundle"
"$appdir/current/.venv/bin/python" "$appdir/current/deploy/install_cli.py" \
  --root "$appdir" --destination "$cli_path" --role relay --domain "$domain"

echo
echo "Relay installed. Open https://$domain/ and log in."
echo "Then open Devices, create a one-time pairing code, and run the"
echo "wrapper installer on the Mac or Linux machine that hosts Claude/Codex."
echo "Updates: cc-remote update (check only: cc-remote update --check)"
