"""Administrator-authorized reads of root-owned macOS service property lists.

The installed helper accepts one plain plist name in the two system service
directories. It can only return bounded file bytes, never execute a release or
accept a command. Cleanup itself continues as the installation's desktop user.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os
from pathlib import Path
import pwd
import re
import shlex
import stat
import subprocess
import sys

HELPERS = Path('/Library/PrivilegedHelperTools')
SUDOERS = Path('/private/etc/sudoers.d')
MARKER = '# cc-remote read-only system service scanner'
HELPER = '''#!/bin/sh
# cc-remote read-only system service scanner
set -eu
[ "$#" -eq 1 ] || exit 2
path=$1
case "$path" in
  /Library/LaunchAgents/*|/Library/LaunchDaemons/*) name=${path##*/} ;;
  *) exit 2 ;;
esac
case "$name" in
  ''|.*|*[!A-Za-z0-9._-]*) exit 2 ;;
  *.plist) ;;
  *) exit 2 ;;
esac
directory=${path%/*}
case "$directory" in /Library/LaunchAgents|/Library/LaunchDaemons) ;; *) exit 2 ;; esac
for item in /Library "$directory" "$path"; do
  [ ! -L "$item" ] || exit 2
  record=$(/usr/bin/stat -f '%u %Lp %z' "$item")
  set -- $record
  [ "$1" -eq 0 ] || exit 2
  case "$2" in ''|*[!0-7]*) exit 2 ;; esac
  [ "$((0$2 & 0022))" -eq 0 ] || exit 2
done
[ -f "$path" ] && [ "$3" -le 4194304 ] || exit 2
exec /usr/bin/base64 -i "$path"
'''
_access: ContextVar[tuple[Path, bool] | None] = ContextVar('service_scan_access', default=None)


@contextmanager
def service_scan(root: Path, *, authorize: bool = False):
    previous = _access.get()
    authorize = authorize or bool(previous and previous[0] == root and previous[1])
    token = _access.set((root, authorize))
    try:
        yield
    finally:
        _access.reset(token)


def helper_path(root: Path) -> Path:
    suffix = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    return HELPERS / ('cc-remote-service-scan-' + suffix)


def _trusted_helper(path: Path) -> bool:
    try:
        for parent in (HELPERS.parent, HELPERS):
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid or info.st_mode & 0o022:
                return False
        info = path.lstat()
        return (stat.S_ISREG(info.st_mode) and info.st_uid == 0
                and not info.st_mode & 0o022 and info.st_nlink == 1
                and path.read_bytes() == HELPER.encode())
    except OSError:
        return False


def authorize_helper(root: Path) -> None:
    if sys.platform != 'darwin' or root.stat().st_uid != os.getuid() or os.getuid() == 0:
        raise PermissionError('system service scan requires the installation owner')
    account = pwd.getpwuid(os.getuid()).pw_name
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*', account):
        raise PermissionError('unsupported system service scanner account')
    helper = helper_path(root)
    rule = SUDOERS / helper.name
    permission = f'{MARKER}\n{account} ALL=(root) NOPASSWD: {helper} *\n'
    # All privileged executable names and destinations are fixed. Payload bytes
    # are embedded in the authorized command, never read from a user-writable
    # staging script after approval.
    command = 'set -eu\numask 077\n'
    for directory in (HELPERS.parent, SUDOERS.parent, HELPERS, SUDOERS):
        name = shlex.quote(str(directory))
        command += (f'if [ ! -d {name} ]; then /usr/bin/install -d -o root -g wheel -m 0755 {name}; fi\n'
                    f'[ ! -L {name} ]\n'
                    f'[ "$(/usr/bin/stat -f %u {name})" -eq 0 ]\n'
                    f'[ "$((0$(/usr/bin/stat -f %Lp {name}) & 0022))" -eq 0 ]\n')
    for destination, content, mode in ((helper, HELPER, '0755'), (rule, permission, '0440')):
        name = shlex.quote(str(destination))
        payload = shlex.quote(base64.b64encode(content.encode()).decode())
        command += (f'[ ! -L {name} ]\n'
                    f'if [ -e {name} ]; then [ -f {name} ]; '
                    f'[ "$(/usr/bin/stat -f %u {name})" -eq 0 ]; '
                    f'/usr/bin/grep -Fxq {shlex.quote(MARKER)} {name}; fi\n'
                    f'pending=$(/usr/bin/mktemp {shlex.quote(str(destination) + ".XXXXXX")})\n'
                    'trap \'/bin/rm -f "$pending"\' EXIT\n'
                    f'/usr/bin/printf %s {payload} | /usr/bin/base64 -D > "$pending"\n'
                    f'/bin/chmod {mode} "$pending"\n'
                    '/usr/sbin/chown root:wheel "$pending"\n')
        if destination == rule:
            command += '/usr/sbin/visudo -cf "$pending" > /dev/null\n'
        command += f'/bin/mv -f "$pending" {name}\ntrap - EXIT\n'
    literal = '"' + command.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'
    try:
        result = subprocess.run(['/usr/bin/osascript', '-e',
                                 'do shell script ' + literal + ' with administrator privileges'],
                                capture_output=True, timeout=90, check=False)
    except (OSError, subprocess.SubprocessError):
        raise PermissionError('administrator authorization for system service scanning is required') from None
    if result.returncode or not _trusted_helper(helper):
        raise PermissionError('administrator authorization for system service scanning is required')


def read_service_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except PermissionError:
        context = _access.get()
        if sys.platform != 'darwin' or context is None:
            raise
    root, authorize = context
    if (path.parent not in (Path('/Library/LaunchAgents'), Path('/Library/LaunchDaemons'))
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*\.plist', path.name)):
        raise PermissionError('protected service input requires administrator inspection')
    helper = helper_path(root)
    if not _trusted_helper(helper):
        if not authorize:
            raise PermissionError('authorize the system service scanner with cc-remote cleanup --authorize-system-scan')
        authorize_helper(root)
    before = path.stat()
    result = subprocess.run(['/usr/bin/sudo', '-n', '--', str(helper), str(path)],
                            capture_output=True, timeout=15, check=False)
    if result.returncode and authorize:
        authorize_helper(root)
        result = subprocess.run(['/usr/bin/sudo', '-n', '--', str(helper), str(path)],
                                capture_output=True, timeout=15, check=False)
    if result.returncode or len(result.stdout) > 6 * 1024 * 1024:
        raise PermissionError('administrator system service scan is unavailable')
    try:
        raw = base64.b64decode(b''.join(result.stdout.split()), validate=True)
    except ValueError:
        raise PermissionError('invalid administrator system service scan response') from None
    after = path.stat()
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_uid', 'st_mode')
    if (any(getattr(before, key) != getattr(after, key) for key in fields)
            or len(raw) != before.st_size or len(raw) > 4 * 1024 * 1024):
        raise PermissionError('system service configuration changed during inspection')
    return raw
