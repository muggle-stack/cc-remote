"""Bounded privileged plist reads, using stub authorization and transport."""
from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy import macos_service_scan as scan


@pytest.fixture
def protected_plist(tmp_path, monkeypatch):
    path = Path('/Library/LaunchDaemons/com.example.helper.plist')
    payload = b'private system service configuration'
    original_read, original_stat = Path.read_bytes, Path.stat
    info = SimpleNamespace(st_dev=1, st_ino=2, st_size=len(payload), st_mtime_ns=3,
                           st_ctime_ns=4, st_uid=0, st_mode=0o100700)

    def read(current):
        if current == path:
            raise PermissionError('protected system plist')
        return original_read(current)

    monkeypatch.setattr(Path, 'read_bytes', read)
    monkeypatch.setattr(Path, 'stat', lambda p, *a, **kw: info if p == path else original_stat(p, *a, **kw))
    monkeypatch.setattr(scan.sys, 'platform', 'darwin')
    monkeypatch.setattr(scan, '_trusted_helper', lambda path: True)
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        return SimpleNamespace(returncode=0, stdout=base64.b64encode(payload))

    monkeypatch.setattr(scan.subprocess, 'run', run)
    return tmp_path.resolve(), path, payload, info, calls


def test_authorized_helper_reads_only_the_requested_plist(protected_plist):
    root, path, payload, _, calls = protected_plist
    with scan.service_scan(root):
        assert scan.read_service_bytes(path) == payload
    assert calls == [['/usr/bin/sudo', '-n', '--', str(scan.helper_path(root)), str(path)]]
    with pytest.raises(PermissionError):
        scan.read_service_bytes(path)


def test_missing_helper_requires_explicit_administrator_authorization(protected_plist, monkeypatch):
    root, path, payload, _, calls = protected_plist
    monkeypatch.setattr(scan, '_trusted_helper', lambda path: False)
    authorized = []
    monkeypatch.setattr(scan, 'authorize_helper', lambda root: authorized.append(root))
    with scan.service_scan(root):
        with pytest.raises(PermissionError, match='authorize'):
            scan.read_service_bytes(path)
    assert calls == [] and authorized == []
    with scan.service_scan(root, authorize=True), scan.service_scan(root):
        assert scan.read_service_bytes(path) == payload
    assert authorized == [root]


def test_configuration_change_during_admin_read_is_rejected(protected_plist, monkeypatch):
    root, path, _, info, _ = protected_plist

    def run(*args, **kwargs):
        info.st_mtime_ns += 1
        return SimpleNamespace(returncode=0, stdout=base64.b64encode(b'changed'))

    monkeypatch.setattr(scan.subprocess, 'run', run)
    with scan.service_scan(root), pytest.raises(PermissionError, match='changed'):
        scan.read_service_bytes(path)


def test_oversized_or_malformed_admin_response_is_rejected(protected_plist, monkeypatch):
    root, path, _, _, _ = protected_plist
    monkeypatch.setattr(scan.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=b'!invalid!'))
    with scan.service_scan(root), pytest.raises(PermissionError, match='invalid'):
        scan.read_service_bytes(path)


def test_system_scanner_installation_embeds_fixed_helper_and_validates_sudoers(tmp_path, monkeypatch):
    monkeypatch.setattr(scan.sys, 'platform', 'darwin')
    monkeypatch.setattr(scan, '_trusted_helper', lambda path: True)
    calls = []
    monkeypatch.setattr(scan.subprocess, 'run', lambda command, **kw: (
        calls.append(command) or SimpleNamespace(returncode=0)))
    scan.authorize_helper(tmp_path.resolve())
    assert calls[0][:2] == ['/usr/bin/osascript', '-e']
    command = calls[0][2]
    assert '/usr/sbin/visudo -cf' in command and 'with administrator privileges' in command
    assert '/Library/PrivilegedHelperTools' in command and '/private/etc/sudoers.d' in command
    assert 'current/.venv' not in command
