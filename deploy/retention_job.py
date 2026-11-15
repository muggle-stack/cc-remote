"""OS-managed retries of accepted installations' deferred release cleanup."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import plistlib
import pwd
import shlex
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy import cleanup, release_retention as retention
from deploy.install_cli import _atomic_file
from deploy.install_lock import InstallLockError, acquire_install_lock
from deploy.macos_service_scan import service_scan

MARKER = '# cc-remote release retention maintenance\n'
SYSTEM_UNITS = Path('/etc/systemd/system')


def label(root: Path) -> str:
    return 'cc-remote-retention-' + hashlib.sha256(str(root).encode()).hexdigest()[:16]


def service_files(root: Path, home: Path, system: str) -> dict[Path, bytes]:
    name = label(root)
    runner = ['/bin/sh', str(root / '.retention-runner.sh')]
    if system == 'darwin':
        return {home / 'Library/LaunchAgents' / (name + '.plist'): plistlib.dumps({
            'Label': name, 'ProgramArguments': runner, 'StartInterval': 300,
            'WorkingDirectory': str(home), 'AbandonProcessGroup': True,
            'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null',
        })}
    from deploy.ui_update_broker import unit_argument
    return {
        SYSTEM_UNITS / (name + '.service'): (MARKER + '[Unit]\nDescription=cc-remote release cleanup\n'
            '[Service]\nType=oneshot\nUMask=0077\nNoNewPrivileges=yes\nTimeoutStartSec=infinity\n'
            'WorkingDirectory=' + unit_argument(str(home), expand_environment=False) + '\n'
            'ExecStart=' + ' '.join(unit_argument(p) for p in runner) + '\n').encode(),
        SYSTEM_UNITS / (name + '.timer'): (MARKER + '[Unit]\nDescription=cc-remote deferred cleanup\n'
            '[Timer]\nOnBootSec=2min\nOnUnitInactiveSec=5min\n'
            '[Install]\nWantedBy=timers.target\n').encode(),
    }


def install(root: Path) -> None:
    from cc_remote.update import host_platform, read_installation
    system, architecture = host_platform()
    current = read_installation(root, system, architecture)
    if current.metadata.get('linux_service'):
        # Supervisor installations have no systemd manager to own a timer.
        # Their explicit maintenance command still uses the same retry engine.
        return
    if system == 'linux':
        from deploy.linux_service import trusted
        trusted(root)
        trusted(SYSTEM_UNITS)
    account = pwd.getpwnam(current.metadata['user']) if current.role == 'wrapper' else pwd.getpwnam('root')
    home = Path(account.pw_dir)
    files = service_files(root, home, system)
    runner = root / '.retention-runner.sh'
    command = [str(root / 'current/.venv/bin/python'), '-I', '-B',
               str(root / 'current/deploy/retention_job.py'), 'run', '--root', str(root)]
    script = '#!/bin/sh\n' + MARKER + 'set -eu\n' + (
        '[ -f ' + shlex.quote(str(root / 'current/deploy/retention_job.py')) + ' ] || exit 0\n'
        'exec ' + shlex.join(command) + '\n')
    if runner.exists() or runner.is_symlink():
        info = runner.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != root.stat().st_uid
                or info.st_mode & 0o022 or not runner.read_text().startswith('#!/bin/sh\n' + MARKER)):
            raise ValueError('unsafe or unrelated retention runner already exists')
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != root.stat().st_uid or info.st_mode & 0o022:
                raise ValueError('unsafe existing retention service')
            previous = path.read_bytes()
            related = (plistlib.loads(previous).get('Label') == label(root) if system == 'darwin'
                       else previous.startswith(MARKER.encode()))
            if not related:
                raise ValueError('unrelated retention service already exists')
    for path, content in files.items():
        _atomic_file(path, content, 0o600 if system == 'darwin' else 0o644)
    _atomic_file(runner, script.encode(), 0o700)
    if system == 'darwin':
        name = f'gui/{account.pw_uid}/{label(root)}'
        observed = subprocess.run(['/bin/launchctl', 'print', name], capture_output=True, timeout=10)
        if observed.returncode == 0:
            return  # Its stable runner resolves the active release on each tick.
        commands = [['/bin/launchctl', 'bootstrap', f'gui/{account.pw_uid}', str(next(iter(files)))]]
    else:
        commands = [['/usr/bin/systemctl', 'daemon-reload'],
                    ['/usr/bin/systemctl', 'enable', '--now', label(root) + '.timer']]
    for command in commands:
        subprocess.run(command, capture_output=True, timeout=30, check=True)


def run(root: Path, *, authorize: bool = False) -> None:
    root = cleanup.absolute_path(str(root))
    try:
        descriptor = acquire_install_lock(root)
    except InstallLockError as error:
        if str(error) != 'another install or update is already running; inspect it before retrying':
            raise
        return  # The next OS tick follows the original installer/cleanup.
    try:
        journal = root / cleanup.JOURNAL
        if journal.exists():
            record, _ = cleanup.read_json(journal, owner_uid=root.stat().st_uid)
            if record.get('status') not in {'complete', 'deferred', 'aborted'}:
                retention.record_retention(root, retention.read_ledger(root), status='deferred',
                                           reason='cleanup transaction requires inspection')
                return  # Partial quarantine requires recovery of that journal.
        from deploy.retention_staging import records
        pending_stages = any(Path(row['path']).exists() for row in records(root)['stages'])
        try:
            status, _ = cleanup.read_json(root / retention.STATUS, owner_uid=root.stat().st_uid)
        except FileNotFoundError:
            status = {}
        if (not pending_stages and status.get('current_release') == str((root / 'current').resolve())
                and status.get('status') == 'complete'):
            publish_status(root, status)
            return
        with service_scan(root, authorize=authorize):
            retention.prune(root, descriptor, wait=30)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        retention.record_retention(root, retention.read_ledger(root), status='deferred', reason=str(error))
    finally:
        os.close(descriptor)


def publish_status(root: Path, status: dict) -> None:
    """Update only this release's completed, public UI receipt under its lock."""
    from cc_remote.update_jobs import UpdateJobs
    from deploy.release_manifest import load_manifest
    if status.get('current_release') != str((root / 'current').resolve()):
        return
    store = UpdateJobs(root)
    try:
        with store.lock(blocking=False):
            job = store.active()
            manifest = load_manifest(root / 'current/release-manifest.json')
            if not job or job['phase'] != 'succeeded' or job['version'] != manifest['product_version']:
                return
            fields = {'cleanup_pending': status.get('status') != 'complete',
                      'cleanup_reason': retention.retention_reason(status)}
            if any(job.get(key) != value for key, value in fields.items()):
                store.save({**job, **fields})
    except (OSError, ValueError):
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'run'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--authorize-system-scan', action='store_true')
    args = parser.parse_args()
    if args.action == 'install':
        install(cleanup.absolute_path(str(args.root)))
    else:
        run(args.root, authorize=args.authorize_system_scan)


if __name__ == '__main__':
    main()
