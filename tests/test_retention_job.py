"""Deferred maintenance uses stable OS jobs and never restarts native services."""
from __future__ import annotations

import os
import plistlib
from types import SimpleNamespace

import pytest

from cc_remote import update
from deploy import retention_job as worker


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path.resolve() / 'install'
    root.mkdir()
    home = tmp_path.resolve() / 'home'
    home.mkdir()
    units = tmp_path.resolve() / 'units'
    units.mkdir()
    current = SimpleNamespace(role='wrapper', metadata={'user': 'operator'})
    monkeypatch.setattr(update, 'host_platform', lambda: ('linux', 'x86_64'))
    monkeypatch.setattr(update, 'read_installation', lambda *a: current)
    monkeypatch.setattr(worker, 'SYSTEM_UNITS', units)
    monkeypatch.setattr(worker.pwd, 'getpwnam', lambda name: SimpleNamespace(pw_dir=str(home), pw_uid=os.getuid()))
    from deploy import linux_service
    monkeypatch.setattr(linux_service, 'trusted', lambda path: None)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1 if command[1] == 'print' else 0)

    monkeypatch.setattr(worker.subprocess, 'run', run)
    return root, home, units, current, calls


@pytest.mark.parametrize('system', ['linux', 'darwin'])
def test_install_is_idempotent_and_resolves_current_on_each_tick(installation, monkeypatch, system):
    root, home, units, _, calls = installation
    monkeypatch.setattr(update, 'host_platform', lambda: (system, 'arm64'))
    worker.install(root)
    worker.install(root)
    runner = (root / '.retention-runner.sh').read_text()
    assert 'current/deploy/retention_job.py' in runner and '[ -f ' in runner
    assert 'releases/' not in runner
    assert not any('restart' in command or 'kickstart' in command for command in calls)
    if system == 'linux':
        timer = units / (worker.label(root) + '.timer')
        assert 'OnUnitInactiveSec=5min' in timer.read_text()
        assert 'TimeoutStartSec=infinity' in timer.with_suffix('.service').read_text()
    else:
        path = home / 'Library/LaunchAgents' / (worker.label(root) + '.plist')
        value = plistlib.loads(path.read_bytes())
        assert value['StartInterval'] == 300
        assert value['WorkingDirectory'] == str(home)
        assert value['ProgramArguments'] == ['/bin/sh', str(root / '.retention-runner.sh')]


@pytest.mark.parametrize('kind', ['dangling-link', 'unrelated', 'writable'])
def test_install_preserves_unsafe_or_unrelated_runner(installation, kind):
    root, _, _, _, calls = installation
    path = root / '.retention-runner.sh'
    if kind == 'dangling-link':
        path.symlink_to(root / 'absent')
    else:
        path.write_text('unrelated file' if kind == 'unrelated' else '#!/bin/sh\n' + worker.MARKER)
        path.chmod(0o666 if kind == 'writable' else 0o600)
    with pytest.raises(ValueError, match='retention runner'):
        worker.install(root)
    assert not calls and (path.is_symlink() or path.exists())


def test_existing_launch_agent_reuses_its_stable_runner(installation, monkeypatch):
    root, _, _, _, calls = installation
    monkeypatch.setattr(update, 'host_platform', lambda: ('darwin', 'arm64'))
    monkeypatch.setattr(worker.subprocess, 'run', lambda command, **kw: (
        calls.append(command) or SimpleNamespace(returncode=0)))
    worker.install(root)
    assert len(calls) == 1 and calls[0][1] == 'print'


@pytest.mark.parametrize('suffix', ['.service', '.timer'])
def test_unrelated_os_unit_is_preserved(installation, suffix):
    root, _, units, _, calls = installation
    path = units / (worker.label(root) + suffix)
    path.write_text('[Service]\nExecStart=/bin/true\n')
    other = path.with_suffix('.timer' if suffix == '.service' else '.service')
    previous = worker.MARKER + 'previous managed definition\n'
    other.write_text(previous)
    with pytest.raises(ValueError, match='unrelated retention service'):
        worker.install(root)
    assert path.read_text() == '[Service]\nExecStart=/bin/true\n' and not calls
    assert other.read_text() == previous
