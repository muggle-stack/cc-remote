#!/usr/bin/env bash
# Download, verify, and run a role-specific cc-remote release installer.
set -euo pipefail

VERSION="${CC_REMOTE_VERSION:-4.0.2}"
REPOSITORY="${CC_REMOTE_GITHUB_REPOSITORY:-muggle-stack/cc-remote}"
BASE_URL="${CC_REMOTE_RELEASE_BASE_URL:-https://github.com/$REPOSITORY/releases/download/v$VERSION}"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || \
  die "CC_REMOTE_VERSION must be an exact semantic version such as 4.0.0"

usage() {
  cat >&2 <<'EOF'
Usage:
  install.sh relay --domain remote.example.com [--allow-private-origins]
  install.sh wrapper --relay https://remote.example.com --pair PAIR-CODE

The role is explicit. The script detects only the operating system and CPU.
EOF
  exit 2
}

role="${1:-}"
case "$role" in
  relay|wrapper) shift ;;
  *) usage ;;
esac

raw_os="${CC_REMOTE_TEST_OS:-$(uname -s)}"
case "$(printf '%s' "$raw_os" | tr '[:upper:]' '[:lower:]')" in
  linux) system=linux ;;
  darwin) system=darwin ;;
  *) die "unsupported operating system: $raw_os" ;;
esac

raw_arch="${CC_REMOTE_TEST_ARCH:-$(uname -m)}"
case "$(printf '%s' "$raw_arch" | tr '[:upper:]' '[:lower:]')" in
  x86_64|amd64) machine=x86_64 ;;
  arm64|aarch64) machine=arm64 ;;
  *) die "unsupported architecture: $raw_arch" ;;
esac

if [ "$role" = relay ] && [ "$system" != linux ]; then
  die "relay requires Linux (Ubuntu 22.04+ or Debian 12+)"
fi
command -v curl >/dev/null 2>&1 || die "curl is required"
command -v tar >/dev/null 2>&1 || die "tar is required"

asset="cc-remote-$role-v$VERSION-$system-$machine.tar.gz"
prefix="cc-remote-$role-v$VERSION"
temporary="$(mktemp -d "${TMPDIR:-/tmp}/cc-remote-install.XXXXXX")"
cleanup() {
  rm -rf -- "$temporary"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

curl --fail --location --proto '=https,file' --tlsv1.2 \
  --output "$temporary/$asset" "$BASE_URL/$asset"
curl --fail --location --proto '=https,file' --tlsv1.2 \
  --output "$temporary/SHA256SUMS" "$BASE_URL/SHA256SUMS"

verify_checksum() {
  expected="$(awk -v wanted="$asset" '
    $2 == wanted || $2 == ("*" wanted) ||
    $2 == ("./" wanted) || $2 == ("*./" wanted) {
      print $1
      found=1
    }
    END { if (!found) exit 1 }
  ' "$temporary/SHA256SUMS")" || die "release checksum is missing for $asset"
  case "$expected" in
    ""|*[!0-9a-fA-F]*) die "release checksum has an invalid format" ;;
  esac
  [ "${#expected}" -eq 64 ] || die "release checksum has an invalid length"
  expected="$(printf '%s' "$expected" | tr '[:upper:]' '[:lower:]')"
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$temporary/$asset" | awk '{print $1}')"
  elif command -v shasum >/dev/null 2>&1; then
    actual="$(shasum -a 256 "$temporary/$asset" | awk '{print $1}')"
  elif command -v openssl >/dev/null 2>&1; then
    actual="$(openssl dgst -sha256 "$temporary/$asset" | awk '{print $NF}')"
  else
    die "sha256sum, shasum, or openssl is required"
  fi
  [ "$actual" = "$expected" ] || die "SHA256 verification failed for $asset"
}

archive_is_safe() {
  while IFS= read -r entry; do
    case "$entry" in
      "$prefix"|"$prefix"/*) ;;
      *) return 1 ;;
    esac
    case "/$entry/" in
      */../*|*/./*) return 1 ;;
    esac
  done < <(tar -tzf "$temporary/$asset")
  while IFS= read -r entry; do
    case "$entry" in
      -*|d*) ;;
      *) return 1 ;;
    esac
  done < <(tar -tvzf "$temporary/$asset")
}

verify_checksum
archive_is_safe || die "release archive contains an unsafe path"
tar -xzf "$temporary/$asset" -C "$temporary"

bundle="$temporary/$prefix"
installer="$bundle/deploy/install-$role.sh"
[ -x "$installer" ] || die "release installer is missing: install-$role.sh"

if [ "$system" = linux ] && [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || die "sudo is required for Linux installation"
  install_user="$(id -un)"
  sudo_env=(CC_REMOTE_INSTALL_USER="$install_user")
  if [ -n "${CC_REMOTE_LOGIN_PASSWORD_FILE:-}" ]; then
    sudo_env+=(CC_REMOTE_LOGIN_PASSWORD_FILE="$CC_REMOTE_LOGIN_PASSWORD_FILE")
  fi
  # Preserve mirror-acceleration variables for the bundled uv across sudo.
  # Without this, `export UV_DEFAULT_INDEX=... ./install.sh wrapper ...` on
  # Linux silently loses the mirror because sudo filters the environment.
  for uv_var in UV_DEFAULT_INDEX UV_PYTHON_INSTALL_MIRROR; do
    if [ -n "${!uv_var:-}" ]; then
      sudo_env+=("$uv_var=${!uv_var}")
    fi
  done
  sudo env "${sudo_env[@]}" \
    "$installer" "$bundle" "$@"
else
  "$installer" "$bundle" "$@"
fi
