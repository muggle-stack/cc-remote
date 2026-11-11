#!/usr/bin/env python3
"""Peer-credential checked systemd socket broker for the fixed-root UI updater.

The Wrapper retains NoNewPrivileges. Only this short-lived, root-owned service
can submit the already-authorized update operations to the OS job manager.
"""
from __future__ import annotations

import argparse
import grp
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cc_remote.update_jobs import UpdateJobs, failure_reason, helper_path, job_id

UNIT_DIRECTORY = Path("/etc/systemd/system")


def unit_argument(value: str, *, expand_environment: bool = True) -> str:
    if any(ord(char) < 32 for char in value):
        raise ValueError("invalid update service argument")
    escaped = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    return '"' + (escaped.replace('$', '$$') if expand_environment else escaped) + '"'


def install(root: Path, account) -> None:
    from deploy.install_cli import _atomic_file
    from deploy.linux_service import trusted
    trusted(UNIT_DIRECTORY)
    name = helper_path(root).name
    group = grp.getgrgid(account.pw_gid).gr_name
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", group):
        raise ValueError("unsupported update socket group")
    unit_argument(str(root))  # Validate controls before using a scalar socket address.
    command = [str(root / 'current/.venv/bin/python'), '-I', '-B',
               str(root / 'current/deploy/ui_update_broker.py'), '--root', str(root)]
    marker = '# cc-remote restricted UI update broker\n'
    socket_unit = marker + f'''[Unit]
Description=cc-remote authorized update socket
[Socket]
ListenStream={str(root / 'ui-update.sock').replace('%', '%%')}
SocketUser=root
SocketGroup={group}
SocketMode=0660
RemoveOnStop=yes
Accept=yes
MaxConnections=16
[Install]
WantedBy=sockets.target
'''
    service_unit = marker + f'''[Unit]
Description=cc-remote authorized update request
[Service]
Type=exec
ExecStart={' '.join(unit_argument(arg) for arg in command)}
StandardInput=socket
StandardOutput=inherit
StandardError=journal
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths={unit_argument(str(root), expand_environment=False)}
PrivateTmp=yes
UMask=0077
RuntimeMaxSec=100
'''
    original = {}
    paths = {UNIT_DIRECTORY / (name + '.socket'): socket_unit,
             UNIT_DIRECTORY / (name + '@.service'): service_unit}
    for path in paths:
        if path.is_symlink() or path.exists() and not path.read_text().startswith(marker):
            raise ValueError("unrelated update broker unit already exists")
        original[path] = path.read_bytes() if path.exists() else None
    was_enabled = subprocess.run(['systemctl', 'is-enabled', '--quiet', name + '.socket'],
                                 capture_output=True, timeout=10).returncode == 0
    was_active = subprocess.run(['systemctl', 'is-active', '--quiet', name + '.socket'],
                                capture_output=True, timeout=10).returncode == 0
    try:
        for path, value in paths.items():
            _atomic_file(path, value.encode(), 0o644)
        subprocess.run(['systemctl', 'daemon-reload'], check=True, capture_output=True, timeout=30)
        subprocess.run(['systemctl', 'enable', '--now', name + '.socket'], check=True, capture_output=True, timeout=30)
        subprocess.run(['systemctl', 'is-active', '--quiet', name + '.socket'], check=True, capture_output=True, timeout=10)
    except Exception:
        if not was_enabled:
            subprocess.run(['systemctl', 'disable', name + '.socket'], check=False, capture_output=True, timeout=30)
        if not was_active:
            subprocess.run(['systemctl', 'stop', name + '.socket'], check=False, capture_output=True, timeout=30)
        for path, value in original.items():
            if value is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_file(path, value, 0o644)
        subprocess.run(['systemctl', 'daemon-reload'], check=False, capture_output=True, timeout=30)
        raise


def dispatch(root: Path, request: dict, uid: int) -> dict:
    from deploy.ui_update_job import cancel, installation, launch, reconcile
    installation_value, account = installation(root)
    policy = UpdateJobs(root).policy()
    if (uid not in {0, account.pw_uid} or policy['uid'] != account.pw_uid
            or policy['role'] != installation_value.role):
        raise ValueError('update broker permission denied')
    if not isinstance(request, dict):
        raise ValueError('invalid update request')
    operation = request.get('op')
    if operation not in {'start', 'cancel', 'reconcile', 'observe'}:
        raise ValueError('invalid update request')
    if set(request) != ({'op', 'id', 'version'} if operation == 'start' else {'op', 'id'}):
        raise ValueError('invalid update request')
    identity = job_id(request['id'])
    result = (launch(root, identity, request['version']) if operation == 'start' else
              cancel(root, identity) if operation == 'cancel' else
              reconcile(root, identity, explicit=operation == 'reconcile'))
    return {'ok': True, 'job': result}


def serve(root: Path, peer: socket.socket) -> None:
    peer.settimeout(90)
    try:
        if os.geteuid() != 0:
            raise ValueError('update broker requires root permission')
        _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        with peer.makefile('rb') as stream:
            raw = stream.readline(4097)
        if len(raw) > 4096 or not raw.endswith(b'\n'):
            raise ValueError('invalid update request')
        result = dispatch(root, json.loads(raw), uid)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        result = {'ok': False, 'reason': failure_reason(exc)}
    peer.sendall(json.dumps(result).encode() + b'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    with socket.socket(fileno=os.dup(0)) as connection:
        serve(parser.parse_args().root, connection)
