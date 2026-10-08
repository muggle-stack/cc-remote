"""Supervisor bootstrap: load external service env, set HOME, then drop identity.

The Supervisor program starts this trusted shim as root, like systemd loading
EnvironmentFile before User=. Native CLIs are only executed by the final user.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import pwd
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from dotenv import dotenv_values
    parser = argparse.ArgumentParser()
    parser.add_argument('--user', required=True)
    parser.add_argument('--home', required=True)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--device-file', type=Path, required=True)
    args = parser.parse_args()
    from deploy.linux_service import trusted, absolute, unset_keys
    home = absolute(args.home)
    owner = pwd.getpwnam(args.user)
    env = dict(os.environ)
    env.update(CC_REMOTE_STATE_DIR=str(home / '.cc-remote'),
               CLAUDE_WORK_ROOT=str(home / '.claude/cc-remote/work'),
               CODEX_WORK_ROOT=str(home / '.codex/cc-remote/work'))
    for path in (args.env_file, args.device_file):
        if path == args.device_file and path.suffix == '.json':
            env['CC_REMOTE_DEVICE_CONFIG'] = str(path)
            continue
        if path.exists():
            trusted(path)
            env.update({k: v for k, v in dotenv_values(path, interpolate=False).items() if v is not None})
    for key in unset_keys(env.pop('CC_REMOTE_SUPERVISOR_UNSET_ENV', '')):
        env.pop(key, None)
    env.setdefault('PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')
    env['PATH'] = str(home / '.local/bin') + ':' + env['PATH']
    env.update(HOME=str(home), USER=owner.pw_name, LOGNAME=owner.pw_name, PYTHON_DOTENV_DISABLED='1')
    for key in ('PYTHONHOME', 'PYTHONPATH'):
        env.pop(key, None)
    if os.geteuid() == 0:
        os.initgroups(owner.pw_name, owner.pw_gid)
        os.setgid(owner.pw_gid)
        os.setuid(owner.pw_uid)
    elif os.geteuid() != owner.pw_uid:
        raise SystemExit('Wrapper bootstrap is running under the wrong user')
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    # Keep the release venv interpreter (sys.executable), not its resolved runtime.
    os.execve(sys.executable, [sys.executable, '-s', '-m', 'cc_remote.wrapper'], env)


if __name__ == '__main__':
    main()
