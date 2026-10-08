"""Real temporary-tree retirement with stubbed OS/health observations; no model."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from deploy import cleanup, release_retention as retention
from deploy.install_lock import acquire_install_lock

SERVICE_DEPENDENCIES = retention.service_dependencies


@pytest.fixture
def systemd_paths(tmp_path, monkeypatch):
    from types import SimpleNamespace

    filesystem = tmp_path.resolve() / 'systemd'
    home = filesystem / 'home/operator'
    home.mkdir(parents=True)
    account = SimpleNamespace(pw_dir=str(home), pw_uid=1000, pw_name='operator')
    paths = {
        'system': [filesystem / p for p in (
            'run/systemd/system', 'run/systemd/generator.early',
            'run/systemd/generator', 'run/systemd/generator.late',
            'etc/systemd/system.control', 'run/systemd/transient')],
        'global': [filesystem / p for p in (
            'etc/systemd/user', 'usr/lib/systemd/user', 'usr/local/lib/systemd/user')],
        'user': [home / '.config/systemd/user', home / '.local/share/systemd/user',
                 filesystem / 'run/user/1000/systemd/user'],
        'live-system': [filesystem / 'custom-system-units'],
        'live-user': [filesystem / 'custom-user-units'],
    }
    monkeypatch.setattr(retention.sys, 'platform', 'linux')
    monkeypatch.setattr(retention, 'os', SimpleNamespace(**{**vars(os), 'geteuid': lambda: 0}))
    monkeypatch.setattr(retention.pwd, 'getpwall', lambda: [account])
    calls = []

    def command(argv):
        calls.append(argv)
        if 'unit-paths' in argv:
            scope = next(s for s in ('system', 'global', 'user') if '--' + s in argv)
            if scope == 'user':
                assert 'HOME=' + str(home) in argv
                assert 'XDG_RUNTIME_DIR=/run/user/1000' in argv
            return '\n'.join(map(str, paths[scope])) + '\n'
        if 'list-units' in argv:
            return 'user@1000.service loaded active running User Manager for UID 1000\n'
        if '--user' in argv:
            assert argv[:3] == ['runuser', '-u', 'operator']
            return ' '.join(map(str, paths['live-user'])) + '\n'
        return ' '.join(map(str, paths['live-system'])) + '\n'

    monkeypatch.setattr(retention, 'command', command)
    return home, paths, calls


@pytest.mark.parametrize('scope,index', [
    ('system', 0), ('system', 1), ('system', 2), ('system', 3),
    ('system', 4), ('system', 5), ('global', 0), ('global', 1),
    ('global', 2), ('user', 1), ('user', 2), ('live-system', 0), ('live-user', 0),
])
def test_dormant_units_in_all_load_paths_protect_old_releases(
    fleet, systemd_paths, monkeypatch, scope, index,
):
    root, _, activate = fleet
    home, paths, _ = systemd_paths
    old, _ = activate('first')
    activate('second')
    activate('third')
    units = paths[scope][index]
    units.mkdir(parents=True, exist_ok=True)
    (units / 'dormant.service').write_text(f'[Service]\nExecStart={old}/payload\n')
    monkeypatch.setattr(retention, 'service_dependencies', SERVICE_DEPENDENCIES)
    report = prune(root)
    assert report['status'] == 'deferred'
    assert old.is_dir()
    assert all(p in retention.service_roots({'home': str(home)}) for group in paths.values() for p in group)


@pytest.mark.parametrize('failure', ['unavailable', 'empty', 'relative', 'global-unavailable',
                                     'unknown-user', 'user-manager-unavailable'])
def test_incomplete_unit_path_discovery_never_deletes(fleet, systemd_paths, monkeypatch, failure):
    root, _, activate = fleet
    old, _ = activate('first')
    activate('second')
    activate('third')

    discover = retention.command

    def incomplete(argv):
        if failure == 'unavailable':
            raise OSError('unit path discovery unavailable')
        if failure == 'global-unavailable' and '--global' in argv:
            raise OSError('global unit path discovery unavailable')
        if failure == 'unknown-user' and 'list-units' in argv:
            return 'user@2000.service loaded active running User Manager for UID 2000\n'
        if failure == 'user-manager-unavailable' and '--user' in argv and 'show' in argv:
            raise ValueError('user manager not reachable')
        if failure in {'empty', 'relative'}:
            return '' if failure == 'empty' else 'relative/unit/path\n'
        return discover(argv)

    monkeypatch.setattr(retention, 'command', incomplete)
    monkeypatch.setattr(retention, 'service_dependencies', SERVICE_DEPENDENCIES)
    with pytest.raises((ValueError, OSError)):
        prune(root)
    assert old.is_dir()


@pytest.mark.parametrize('failure', ['permission', 'dangling-directory'])
def test_unreadable_unit_directory_never_deletes(fleet, systemd_paths, monkeypatch, failure):
    root, _, activate = fleet
    old, _ = activate('first')
    activate('second')
    activate('third')
    _, paths, _ = systemd_paths
    directory = paths['global'][0]
    directory.parent.mkdir(parents=True)
    if failure == 'permission':
        directory.mkdir()
        original = Path.stat

        def unreadable(path, *args, **kwargs):
            if path == directory:
                raise PermissionError('cannot inspect unit directory')
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, 'stat', unreadable)
    else:
        directory.symlink_to(directory.with_name('missing'))
    monkeypatch.setattr(retention, 'service_dependencies', SERVICE_DEPENDENCIES)
    with pytest.raises((ValueError, OSError)):
        prune(root)
    assert old.is_dir()


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "install"
    (root / "releases").mkdir(parents=True)
    service = tmp_path.resolve() / "wrapper.service"
    service.write_text("operator-owned service settings\n")
    monkeypatch.setattr(retention, "acceptance", lambda *a: None)
    monkeypatch.setattr(retention, "service_identity", lambda row: {"pid": "123", "runs": "1"})
    monkeypatch.setattr(retention, "service_dependencies", lambda *a: [])
    monkeypatch.setattr(cleanup, "process_references", lambda paths: {p: [] for p in paths})
    monkeypatch.setattr(cleanup, "run_checks", lambda plan: None)

    def activate(name):
        release = root / "releases" / name
        release.mkdir()
        (release / "payload").write_text("installed bytes")
        (release / "release-manifest.json").write_text(json.dumps({
            "schema": 1, "product_version": "4.0.9", "protocol_version": 74,
            "git_sha": "a" * 40, "role": "wrapper", "os": "linux", "arch": "x86_64",
            "python": "3.13.9", "uv": "0.11.16",
        }))
        link = root / "current"
        previous = str(link.resolve()) if link.exists() else ""
        identity = retention.begin(root, release, previous, role="wrapper", home=root,
                                   service="wrapper", service_file=service, configs=[])
        snapshot = root / "rollback-data" / identity
        snapshot.mkdir(parents=True)
        (snapshot / "private-state").write_text("recover the previous data")
        if link.is_symlink():
            link.unlink()
        link.symlink_to(release)
        retention.commit(root, identity, str(snapshot))
        return release, retention.generation(root, identity)[1]

    return root, service, activate


def prune(root):
    fd = acquire_install_lock(root)
    try:
        return retention.prune(root, fd)
    finally:
        os.close(fd)


def test_three_upgrades_keep_current_and_complete_previous_recovery(fleet):
    root, service, activate = fleet
    first, old = activate("first")
    second, middle = activate("second")
    third, latest = activate("third")
    unknown = root / "releases/unknown-legacy"
    unknown.mkdir()
    report = prune(root)
    assert report["status"] == "complete"
    assert third.is_dir() and second.is_dir() and not first.exists()
    assert unknown.is_dir()  # no filename/mtime guessing
    assert Path(latest["snapshot"]).is_dir() and Path(latest["backup"]).is_dir()
    assert (Path(latest["backup"]) / "0").read_bytes() == service.read_bytes()
    assert Path(latest["backup"]).stat().st_mode & 0o777 == 0o700
    for row in (old, middle):
        assert not Path(row["backup"]).exists() and not Path(row["snapshot"]).exists()


@pytest.mark.parametrize('when', ['inventory', 'preview', 'before-rename', 'quarantined', 'before-delete'])
@pytest.mark.parametrize('kind', ['systemd', 'launchd', 'supervisor', 'environment', 'optional-environment'])
def test_new_service_dependency_during_retirement_preserves_artifacts(fleet, tmp_path, monkeypatch, when, kind):
    import plistlib
    from deploy import linux_service

    root, _, activate = fleet
    old, _ = activate('first')
    previous, _ = activate('second')
    current, _ = activate('third')
    units = tmp_path / 'units'
    units.mkdir()
    environment = tmp_path / 'external.env'
    if kind in {'environment', 'optional-environment'}:
        (units / 'dormant.service').write_text(f'[Service]\nEnvironmentFile=-{environment}\n')
        if kind == 'environment':
            environment.write_text('WORKER=/bin/true\n')
    monkeypatch.setattr(retention, 'service_roots', lambda row: [units] if kind != 'supervisor' else [])
    monkeypatch.setattr(retention, 'service_dependencies', SERVICE_DEPENDENCIES)
    if kind == 'supervisor':
        # Exercise the saved binding and a newly added include, outside systemd
        # discovery, rather than pretending Supervisor is a systemd directory.
        main = tmp_path / 'supervisord.conf'
        main.write_text(f'[include]\nfiles={units}/late.conf\n')
        profile = dict(manager='supervisor', name='wrapper', user='root', home=str(root),
                       service_file=str(units / 'wrapper.conf'), env_file=str(environment),
                       device_file=str(tmp_path / 'device.json'), supervisor_config=str(main))
        data = retention.read_ledger(root)
        data['generations'][-1]['linux_service'] = profile
        cleanup.write_journal(root / retention.LEDGER, data)
        monkeypatch.setattr(linux_service, 'trusted', lambda path: None)
    injected = False

    def add_reference():
        nonlocal injected
        assert not injected
        injected = True
        if kind == 'systemd':
            (units / 'late.service').write_text(f'[Service]\nExecStart={old}/payload\n')
        elif kind == 'launchd':
            (units / 'late.plist').write_bytes(plistlib.dumps({'ProgramArguments': [str(old / 'payload')]}))
        elif kind == 'supervisor':
            (units / 'late.conf').write_text(f'[program:late]\ncommand={old}/payload\n')
        else:
            environment.write_text(f'WORKER={old}/payload\n')

    if when == 'inventory':
        original = retention.inventory

        def inventory(*args):
            result = original(*args)
            add_reference()
            return result

        monkeypatch.setattr(retention, 'inventory', inventory)
    elif when == 'preview':
        original = cleanup.cleanup

        def after_preview(*args, **kwargs):
            result = original(*args, **kwargs)
            if not kwargs.get('apply'):
                add_reference()
            return result

        monkeypatch.setattr(cleanup, 'cleanup', after_preview)
    elif when == 'before-delete':
        original = cleanup.artifact_entries

        def entries(path):
            yield from original(path)
            if path.name.startswith('.cc-remote-retired-') and not injected:
                add_reference()

        monkeypatch.setattr(cleanup, 'artifact_entries', entries)
    else:
        checks = 0

        def acceptance(plan):
            nonlocal checks
            checks += 1
            if checks == (1 if when == 'before-rename' else 2):
                add_reference()

        monkeypatch.setattr(cleanup, 'run_checks', acceptance)
    with pytest.raises(cleanup.CleanupError, match='service dependencies changed'):
        prune(root)
    assert injected and (old / 'payload').read_text() == 'installed bytes'
    assert previous.is_dir() and current.is_dir()
    assert not list(root.rglob('.cc-remote-retired-*'))
    plan = json.loads((root / retention.INVENTORY).read_text())
    assert plan['service_context']['home'] == str(root)
    if kind == 'supervisor':
        assert plan['service_context']['linux_service'] == profile
    # No artifact (including old rollback snapshots) may be lost in these races.
    assert all(Path(p).exists() for p in plan['candidates'])


@pytest.mark.parametrize('failure', ['unreadable', 'new-load-path', 'retargeted-alias'])
def test_service_rediscovery_after_quarantine_retains_runtime_closure(fleet, tmp_path, monkeypatch, failure):
    root, _, activate = fleet
    runtime, _ = activate('first')
    worker, _ = activate('second')
    (worker / 'runtime').symlink_to(runtime)
    activate('third')
    activate('fourth')
    units = tmp_path / 'units'
    units.mkdir()
    extra = tmp_path / 'extra-units'
    extra.mkdir()
    executable = tmp_path / 'external-worker'
    executable.write_text('external')
    alias = tmp_path / 'alias'
    alias.symlink_to(executable)
    if failure == 'retargeted-alias':
        (units / 'worker.service').write_text(f'[Service]\nExecStart={alias}\n')
    checks = 0

    def roots(row):
        if checks >= 2 and failure == 'unreadable':
            raise PermissionError('service discovery incomplete')
        return [units, extra] if checks >= 2 else [units]

    def acceptance(plan):
        nonlocal checks
        checks += 1
        if checks == 2:
            if failure == 'retargeted-alias':
                alias.unlink()
                alias.symlink_to(worker / 'payload')
            else:
                (extra / 'worker.service').write_text(f'[Service]\nExecStart={worker}/payload\n')

    monkeypatch.setattr(retention, 'service_roots', roots)
    monkeypatch.setattr(retention, 'service_dependencies', SERVICE_DEPENDENCIES)
    monkeypatch.setattr(cleanup, 'run_checks', acceptance)
    with pytest.raises((cleanup.CleanupError, OSError)):
        prune(root)
    assert runtime.is_dir() and worker.is_dir()
    assert (worker / 'runtime').resolve(strict=True) == runtime
    assert not list(root.rglob('.cc-remote-retired-*'))


def test_first_preledger_release_becomes_eligible_only_after_next_success(fleet):
    root, _, activate = fleet
    first = root / "releases/pre-ledger"
    first.mkdir()
    (root / "current").symlink_to(first)
    second, _ = activate("second")
    assert prune(root) is None
    assert first.is_dir()
    third, _ = activate("third")
    prune(root)
    assert not first.exists() and second.exists() and third.exists()


def test_live_old_release_survives_then_same_version_prune_retries(fleet, monkeypatch):
    root, _, activate = fleet
    first, _ = activate("first")
    activate("second")
    activate("third")
    monkeypatch.setattr(cleanup, "process_references", lambda paths: {
        p: ["pid 55: cwd"] if p == first else [] for p in paths})
    assert prune(root)["status"] == "deferred"
    assert first.exists()
    monkeypatch.setattr(cleanup, "process_references", lambda paths: {p: [] for p in paths})
    assert prune(root)["status"] == "complete"
    assert not first.exists()


def test_transitive_runtime_of_previous_release_is_not_removed(fleet):
    root, _, activate = fleet
    first, _ = activate("first")
    second, _ = activate("second")
    (second / "runtime").symlink_to(first, target_is_directory=True)
    activate("third")
    report = prune(root)
    assert first.exists()
    assert any("symlink dependency" in reason for row in report["candidates"]
               if row["path"] == str(first) for reason in row["reasons"])


@pytest.mark.parametrize("failure", ["health", "visibility", "pending", "replaced", "unknown_cleanup"])
def test_uncertain_acceptance_never_deletes(fleet, monkeypatch, failure):
    root, _, activate = fleet
    first, _ = activate("first")
    activate("second")
    _, row = activate("third")
    if failure == "health":
        def fail(*args):
            raise ValueError("health failed")
        monkeypatch.setattr(retention, "acceptance", fail)
    elif failure == "visibility":
        def invisible(*args):
            raise cleanup.CleanupError("process visibility incomplete")
        monkeypatch.setattr(cleanup, "process_references", invisible)
    elif failure == "pending":
        data = retention.read_ledger(root)
        data["generations"][0]["phase"] = "prepared"
        cleanup.write_journal(root / retention.LEDGER, data)
    elif failure == "replaced":
        first.rename(first.with_name("original"))
        first.mkdir()
    else:
        cleanup.write_journal(root / cleanup.JOURNAL, {"status": "requires_inspection"})
    with pytest.raises(ValueError):
        prune(root)
    assert first.exists() and Path(row["snapshot"]).exists()


def test_post_quarantine_failure_restores_original_paths(fleet, monkeypatch):
    root, _, activate = fleet
    first, _ = activate("first")
    activate("second")
    activate("third")
    calls = []

    def checks(plan):
        calls.append(first.exists())
        if len(calls) == 2:
            raise cleanup.CleanupError("fresh config read failed")

    monkeypatch.setattr(cleanup, "run_checks", checks)
    with pytest.raises(ValueError, match="fresh config"):
        prune(root)
    assert first.is_dir() and (first / "payload").read_text() == "installed bytes"
    assert json.loads((root / cleanup.JOURNAL).read_text())["status"] == "aborted"


def test_failed_activation_does_not_create_deletable_generation(fleet):
    root, _, activate = fleet
    first, _ = activate("first")
    second, row = activate("second")
    data = retention.read_ledger(root)
    data["generations"][-1]["phase"] = "prepared"
    cleanup.write_journal(root / retention.LEDGER, data)
    (root / "current").unlink()
    (root / "current").symlink_to(first)
    retention.abort(root, row["id"])
    third, _ = activate("third")
    prune(root)
    assert first.exists() and second.exists() and third.exists()
    assert Path(row["snapshot"]).exists()


def test_copy_failure_leaves_explicit_unfinished_record(fleet):
    root, service, activate = fleet
    activate("first")
    service.unlink()
    service.symlink_to(root / "releases/first/payload")
    with pytest.raises(ValueError, match="symlink"):
        activate("second")
    assert retention.read_ledger(root)["generations"][-1]["phase"] == "prepared"
    with pytest.raises(ValueError, match="unfinished"):
        activate("third")


def test_legacy_outside_root_is_kept_for_explicit_migration_acceptance(fleet, tmp_path):
    root, service, _ = fleet
    legacy = tmp_path.resolve() / "legacy/release"
    legacy.mkdir(parents=True)
    release = root / "releases/new"
    release.mkdir()
    # Reuse the normal fixture manifest without installing another generation.
    manifest = {"schema": 1, "product_version": "4.0.9", "protocol_version": 74,
                "git_sha": "a" * 40, "role": "wrapper", "os": "linux", "arch": "x86_64",
                "python": "3.13.9", "uv": "0.11.16"}
    (release / "release-manifest.json").write_text(json.dumps(manifest))
    identity = retention.begin(root, release, str(legacy), role="wrapper", home=root,
                               service="wrapper", service_file=service, configs=[])
    (root / "current").symlink_to(release)
    retention.commit(root, identity)
    with pytest.raises(ValueError, match="legacy rollback"):
        prune(root)
    assert legacy.is_dir()


def test_dormant_service_and_environment_dependencies(tmp_path, monkeypatch):
    services = tmp_path / 'units'
    services.mkdir()
    old = tmp_path / 'release-old'
    old.mkdir()
    env = tmp_path / 'daemon.env'
    env.write_text(f'CONFIG={old}/settings.json\n')
    (services / 'idle.service').write_text(f'[Service]\nEnvironmentFile={env}\n')
    (services / 'masked.service').symlink_to('/dev/null')
    monkeypatch.setattr(retention, 'service_roots', lambda row: [services])
    assert retention.service_dependencies({}, [old]) == [str(old)]
    (services / 'idle.service').write_text('[Service]\nEnvironmentFile=/private/%i.env\n')
    with pytest.raises(ValueError, match='dynamic'):
        retention.service_dependencies({}, [old])


@pytest.mark.parametrize('kind', [
    'executable', 'relative-chain', 'directory', 'argument', 'environment',
    'environment-file', 'intermediate-owner', 'plist', 'plist-spaces', 'supervisor',
])
def test_dormant_service_alias_keeps_release(fleet, tmp_path, monkeypatch, kind):
    import plistlib

    root, _, activate = fleet
    old, _ = activate('first')
    activate('second')
    activate('third')
    units = tmp_path / 'units'
    units.mkdir()
    alias = tmp_path / 'stable-worker'
    alias.symlink_to(old / 'payload')
    service = units / 'dormant.service'
    text = f'[Service]\nExecStart={alias}\n'
    if kind == 'relative-chain':
        next_alias = tmp_path / 'second-alias'
        next_alias.symlink_to(alias.name)
        text = f'[Service]\nExecStart=-@{next_alias} worker\n'
    elif kind == 'directory':
        alias.unlink()
        alias.symlink_to(old, target_is_directory=True)
        text = f'[Service]\nExecStart={alias}/payload\nWorkingDirectory={alias}\n'
    elif kind == 'argument':
        text = f'[Service]\nExecStart=/bin/true --config={alias}\n'
    elif kind == 'environment':
        env = tmp_path / 'external.env'
        env.write_text(f'WORKER="{alias}"\n')
        text = f'[Service]\nEnvironmentFile={env}\n'
    elif kind == 'environment-file':
        text = f'[Service]\nEnvironmentFile={alias}\n'
    elif kind == 'intermediate-owner':
        external = tmp_path / 'external-worker'
        external.write_text('worker')
        (old / 'payload').unlink()
        (old / 'payload').symlink_to(external)
    elif kind in {'plist', 'plist-spaces'}:
        if kind == 'plist-spaces':
            directory = tmp_path / 'Application Support'
            directory.mkdir()
            alias = directory / 'worker'
            alias.symlink_to(old / 'payload')
        service = units / 'dormant.plist'
        service.write_bytes(plistlib.dumps({'ProgramArguments': [str(alias), '--serve']}))
    elif kind == 'supervisor':
        service = units / 'worker.conf'
        text = f'[program:worker]\ncommand={alias} --serve\n'
    if kind not in {'plist', 'plist-spaces'}:
        service.write_text(text)
    monkeypatch.setattr(retention, 'service_roots', lambda row: [units])
    monkeypatch.setattr(retention, 'service_dependencies', SERVICE_DEPENDENCIES)
    assert prune(root)['status'] == 'deferred'
    assert old.is_dir()
    assert alias.resolve(strict=True).exists()


@pytest.mark.parametrize('failure', ['broken', 'cycle', 'permission', 'expansion', 'escaped', 'bare-command'])
def test_unresolved_service_alias_never_deletes(fleet, tmp_path, monkeypatch, failure):
    root, _, activate = fleet
    old, _ = activate('first')
    activate('second')
    activate('third')
    units = tmp_path / 'units'
    units.mkdir()
    alias = tmp_path / 'worker'
    alias.symlink_to(old / 'payload')
    command = str(alias)
    if failure == 'broken':
        alias.unlink()
        alias.symlink_to(tmp_path / 'missing')
    elif failure == 'cycle':
        alias.unlink()
        alias.symlink_to(alias.name)
    elif failure == 'permission':
        original = Path.lstat

        def unreadable(path, *args, **kwargs):
            if path == alias:
                raise PermissionError('cannot inspect service alias')
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, 'lstat', unreadable)
    elif failure == 'expansion':
        command = str(alias.parent / '${WORKER}')
    elif failure == 'escaped':
        command = str(alias).replace('worker', r'wor\x6ber')
    else:
        command = 'worker'
    (units / 'dormant.service').write_text(f'[Service]\nExecStart={command}\n')
    monkeypatch.setattr(retention, 'service_roots', lambda row: [units])
    monkeypatch.setattr(retention, 'service_dependencies', SERVICE_DEPENDENCIES)
    with pytest.raises((ValueError, OSError, RuntimeError)):
        prune(root)
    assert old.is_dir()


def test_unrelated_service_alias_allows_retirement(fleet, tmp_path, monkeypatch):
    root, _, activate = fleet
    old, _ = activate('first')
    activate('second')
    activate('third')
    units = tmp_path / 'units'
    units.mkdir()
    worker = tmp_path / 'external-worker'
    worker.write_text('worker')
    alias = tmp_path / 'stable-worker'
    alias.symlink_to(worker)
    (units / 'dormant.service').write_text(f'[Service]\nExecStart={alias}\n')
    monkeypatch.setattr(retention, 'service_roots', lambda row: [units])
    monkeypatch.setattr(retention, 'service_dependencies', SERVICE_DEPENDENCIES)
    assert prune(root)['status'] == 'complete'
    assert not old.exists() and alias.resolve(strict=True) == worker


@pytest.mark.parametrize('kind', ['systemd', 'supervisor', 'launchd', 'launchd-program'])
def test_indirect_service_command_never_deletes(fleet, tmp_path, monkeypatch, kind):
    import plistlib
    import subprocess

    root, _, activate = fleet
    old, _ = activate('first')
    activate('second')
    activate('third')
    # This directory exists only in the service manager's environment. The
    # updater cannot infer its PATH from its own environment or unit text.
    inherited_bin = tmp_path / 'manager-bin'
    inherited_bin.mkdir()
    (old / 'payload').write_text('#!/bin/sh\nexit 0\n')
    (old / 'payload').chmod(0o755)
    (inherited_bin / 'worker').symlink_to(old / 'payload')
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    manager_env = {**os.environ, 'PATH': f'{inherited_bin}:/usr/bin:/bin'}
    subprocess.run(['/bin/sh', '-c', 'worker'], env=manager_env, check=True)
    units = tmp_path / 'units'
    units.mkdir()
    if kind.startswith('launchd'):
        payload = {'ProgramArguments': ['/bin/sh', '-c', 'worker']}
        if kind == 'launchd-program':
            payload['Program'] = '/bin/sh'
            payload['ProgramArguments'][0] = 'custom-argv-zero'
        (units / 'worker.plist').write_bytes(plistlib.dumps(payload))
    elif kind == 'supervisor':
        (units / 'worker.conf').write_text('[program:worker]\ncommand=/bin/sh -c worker\n')
    else:
        (units / 'worker.service').write_text('[Service]\nExecStart=/bin/sh -c worker\n')
    monkeypatch.setattr(retention, 'service_roots', lambda row: [units])
    monkeypatch.setattr(retention, 'service_dependencies', SERVICE_DEPENDENCIES)
    with pytest.raises(ValueError, match='indirect service command'):
        prune(root)
    assert old.is_dir() and (inherited_bin / 'worker').resolve(strict=True).exists()
    assert not list(root.rglob('.cc-remote-retired-*'))
    subprocess.run(['/bin/sh', '-c', 'worker'], env=manager_env, check=True)


@pytest.fixture
def executable_units(tmp_path, monkeypatch):
    units = tmp_path / 'units'
    units.mkdir()
    paths = [tmp_path / 'local-bin', tmp_path / 'bin']
    for directory in paths:
        directory.mkdir()
    calls = []

    def discover(argv):
        calls.append(argv)
        assert argv == ['env', '-i', 'PATH=/usr/sbin:/usr/bin:/sbin:/bin',
                        'systemd-path', 'search-binaries-default']
        return ':'.join(map(str, paths)) + '\n'

    monkeypatch.setattr(retention, 'command', discover)
    monkeypatch.setattr(retention, 'service_roots', lambda row: [units])
    monkeypatch.setattr(retention, 'service_dependencies', SERVICE_DEPENDENCIES)
    return units, paths, calls


@pytest.mark.parametrize('directive,prefix', [
    ('ExecStart', ''), ('ExecStartPre', '-'), ('ExecStartPost', '+'),
    ('ExecCondition', ':'), ('ExecReload', '@'), ('ExecStop', '!'), ('ExecStopPost', '-@'),
])
def test_systemd_bare_executable_allows_retirement(
    fleet, executable_units, tmp_path, monkeypatch, directive, prefix,
):
    root, _, activate = fleet
    old, _ = activate('first')
    activate('second')
    activate('third')
    units, paths, calls = executable_units
    binary = paths[-1] / 'systemd-tmpfiles'
    binary.write_text('must never execute the discovered service')
    binary.chmod(0o755)
    ambient = tmp_path / 'updater-bin'
    ambient.mkdir()
    (ambient / binary.name).symlink_to(old / 'payload')
    monkeypatch.setenv('PATH', str(ambient))
    (units / 'tmpfiles.service').write_text(
        f'[Service]\n{directive} = {prefix}{binary.name} --create --remove --boot\n')
    assert prune(root)['status'] == 'complete'
    assert not old.exists() and binary.exists()
    assert len(calls) > 1, 'each destructive boundary rediscovers the native search path'


@pytest.mark.parametrize('lookup', ['default', 'drop-in', 'environment', 'environment-file'])
def test_systemd_bare_executable_alias_keeps_release(fleet, executable_units, tmp_path, lookup):
    root, _, activate = fleet
    old, _ = activate('first')
    activate('second')
    activate('third')
    (old / 'payload').chmod(0o755)
    units, paths, _ = executable_units
    custom = tmp_path / 'custom-bin'
    custom.mkdir()
    directory = paths[0] if lookup == 'default' else custom
    (directory / 'worker').symlink_to(old / 'payload')
    # A second match must not hide an alias used under a different service
    # user, search-path override, or effective drop-in precedence.
    (paths[-1] / 'worker').write_text('unrelated binary')
    (paths[-1] / 'worker').chmod(0o755)
    config = '[Service]\nExecStart=-@worker worker --serve\n'
    if lookup == 'drop-in':
        drop_in = units / 'worker.service.d'
        drop_in.mkdir()
        (drop_in / 'path.conf').write_text(f'[Service]\nExecSearchPath={custom}:{paths[-1]}\n')
    elif lookup == 'environment':
        config += f'Environment="PATH={custom}:{paths[-1]}"\n'
    elif lookup == 'environment-file':
        env = tmp_path / 'worker.env'
        env.write_text(f'PATH="{custom}:{paths[-1]}"\n')
        config += f'EnvironmentFile = {env}\n'
    (units / 'worker.service').write_text(config)
    assert prune(root)['status'] == 'deferred'
    assert old.is_dir() and (directory / 'worker').resolve(strict=True).exists()


@pytest.mark.parametrize('change', ['alias', 'search-path'])
def test_bare_executable_rediscovery_restores_quarantined_artifacts(
    fleet, executable_units, tmp_path, monkeypatch, change,
):
    root, _, activate = fleet
    old, _ = activate('first')
    activate('second')
    activate('third')
    units, paths, _ = executable_units
    worker = paths[-1] / 'worker'
    worker.write_text('unrelated binary')
    worker.chmod(0o755)
    (units / 'worker.service').write_text('[Service]\nExecStart=worker\n')
    checks = 0

    def acceptance(plan):
        nonlocal checks
        checks += 1
        if checks == 2:
            directory = paths[0]
            if change == 'search-path':
                directory = tmp_path / 'new-systemd-bin'
                directory.mkdir()
                paths.insert(0, directory)
            (directory / 'worker').symlink_to(old / 'payload')

    monkeypatch.setattr(cleanup, 'run_checks', acceptance)
    with pytest.raises((cleanup.CleanupError, OSError)):
        prune(root)
    plan = json.loads((root / retention.INVENTORY).read_text())
    assert checks == 2 and all(Path(p).exists() for p in plan['candidates'])
    assert not list(root.rglob('.cc-remote-retired-*'))


@pytest.mark.parametrize('failure', [
    'discovery', 'empty-path', 'relative-path', 'missing', 'non-executable', 'broken',
    'custom-root', 'inherited-path', 'relative-command', 'shell-prefix', 'supervisor',
])
def test_unresolved_bare_executable_never_deletes(fleet, executable_units, monkeypatch, failure):
    root, _, activate = fleet
    old, _ = activate('first')
    activate('second')
    activate('third')
    units, paths, _ = executable_units
    worker = paths[0] / 'worker'
    worker.write_text('binary')
    worker.chmod(0o755)
    config = '[Service]\nExecStart=worker\n'
    if failure == 'discovery':
        def unavailable(argv):
            raise OSError('systemd-path unavailable')
        monkeypatch.setattr(retention, 'command', unavailable)
    elif failure in {'empty-path', 'relative-path'}:
        monkeypatch.setattr(retention, 'command', lambda argv: '' if failure == 'empty-path' else 'relative/bin')
    elif failure in {'missing', 'broken'}:
        worker.unlink()
        if failure == 'broken':
            worker.symlink_to(old / 'missing')
    elif failure == 'non-executable':
        worker.chmod(0o644)
    elif failure == 'custom-root':
        config += f'RootDirectory={paths[0]}\n'
    elif failure == 'inherited-path':
        config += 'PassEnvironment=PATH\n'
    elif failure == 'relative-command':
        config = '[Service]\nExecStart=bin/worker\n'
    elif failure == 'shell-prefix':
        config = '[Service]\nExecStart=|worker\n'
    elif failure == 'supervisor':
        config = '[program:worker]\ncommand=worker\n'
    (units / 'worker.service').write_text(config)
    with pytest.raises((ValueError, OSError)):
        prune(root)
    assert old.is_dir()


@pytest.mark.parametrize('command', [
    '/bin/sh -c worker', '/bin/bash -lc worker', '-@/bin/sh custom-name -c worker',
    '/usr/bin/env worker', '/usr/bin/env -S "sh -c worker"',
    '/usr/bin/nohup worker', '/usr/bin/nice -n 5 worker', '/usr/bin/timeout 10 worker',
    '/usr/bin/busybox sh -c worker', '/usr/bin/python3.13 -c worker',
    '/usr/bin/python3 -I -m worker', '/usr/bin/node --eval worker',
    '/usr/bin/perl -e worker', '/usr/bin/ruby -e worker',
    '/bin/bash worker', '/bin/sh', '/usr/bin/python3 -',
    'shell-alias -c worker', '{alias} -c worker',
])
def test_interpreter_and_launcher_indirection_defers_retirement(
    fleet, executable_units, tmp_path, command,
):
    root, _, activate = fleet
    old, _ = activate('first')
    activate('second')
    activate('third')
    units, paths, _ = executable_units
    shell = tmp_path / 'sh'
    shell.write_text('must never execute the service command')
    shell.chmod(0o755)
    alias = paths[0] / 'shell-alias'
    alias.symlink_to(shell)
    (units / 'worker.service').write_text(
        '[Service]\nExecStart=' + command.format(alias=alias) + '\n')
    with pytest.raises(ValueError, match='indirect service command'):
        prune(root)
    assert old.is_dir()


@pytest.mark.parametrize('command', [
    '/bin/sh {script}', '/bin/bash -- {script}',
    '/usr/bin/python3 -I -B {script}', '/usr/bin/node {script}',
    '/bin/true -c worker',
])
def test_literal_service_payloads_still_allow_retirement(
    fleet, executable_units, tmp_path, command,
):
    root, _, activate = fleet
    old, _ = activate('first')
    activate('second')
    activate('third')
    units, _, _ = executable_units
    script = tmp_path / 'worker-script'
    script.write_text('must never execute this script')
    (units / 'worker.service').write_text(
        '[Service]\nExecStart=' + command.format(script=script) + '\n')
    assert prune(root)['status'] == 'complete'
    assert not old.exists() and script.exists()


def test_direct_interpreter_script_alias_keeps_release(fleet, executable_units, tmp_path):
    root, _, activate = fleet
    old, _ = activate('first')
    activate('second')
    activate('third')
    units, _, _ = executable_units
    script = tmp_path / 'worker-script'
    script.symlink_to(old / 'payload')
    (units / 'worker.service').write_text(f'[Service]\nExecStart=/usr/bin/python3 -I -B {script}\n')
    assert prune(root)['status'] == 'deferred'
    assert old.exists() and script.resolve(strict=True).exists()


def test_late_interpreter_indirection_restores_quarantined_artifacts(
    fleet, executable_units, monkeypatch,
):
    root, _, activate = fleet
    activate('first')
    activate('second')
    activate('third')
    units, _, _ = executable_units
    service = units / 'worker.service'
    service.write_text('[Service]\nExecStart=/bin/true\n')
    checks = 0

    def acceptance(plan):
        nonlocal checks
        checks += 1
        if checks == 2:
            service.write_text('[Service]\nExecStart=/bin/sh -c worker\n')

    monkeypatch.setattr(cleanup, 'run_checks', acceptance)
    with pytest.raises(ValueError, match='indirect service command'):
        prune(root)
    plan = json.loads((root / retention.INVENTORY).read_text())
    assert checks == 2 and all(Path(p).exists() for p in plan['candidates'])
    assert not list(root.rglob('.cc-remote-retired-*'))


def test_dormant_alias_preserves_transitive_release_dependencies(fleet, tmp_path, monkeypatch):
    root, _, activate = fleet
    runtime, _ = activate('first')
    worker, _ = activate('second')
    (worker / 'runtime').symlink_to(runtime, target_is_directory=True)
    activate('third')
    activate('fourth')
    units = tmp_path / 'units'
    units.mkdir()
    alias = tmp_path / 'worker'
    alias.symlink_to(worker / 'payload')
    (units / 'dormant.service').write_text(f'[Service]\nExecStart={alias}\n')
    monkeypatch.setattr(retention, 'service_roots', lambda row: [units])
    monkeypatch.setattr(retention, 'service_dependencies', SERVICE_DEPENDENCIES)
    assert prune(root)['status'] == 'deferred'
    assert worker.is_dir() and runtime.is_dir()


@pytest.mark.parametrize('assignment', [
    'EnvironmentFile={env}',
    'EnvironmentFile = {env}',
    '\tEnvironmentFile\t=\t{env}\t',
    'EnvironmentFile = -{env}',
    'EnvironmentFile = \\\n# ignored during continuation\n; also ignored\n  {env}',
    'EnvironmentFile \\\n = {env}',
    'EnvironmentFile = {env}\nEnvironmentFile =',
    'EnvironmentFile = {env}\r\n',
])
def test_environment_assignments_keep_dormant_release(fleet, tmp_path, monkeypatch, assignment):
    root, _, activate = fleet
    old, _ = activate('first')
    activate('second')
    activate('third')
    units = tmp_path / 'units'
    dropin = units / 'dormant.service.d'
    dropin.mkdir(parents=True)
    (units / 'dormant.service').write_text('[Service]\nExecStart=/bin/true\n')
    env = tmp_path / 'external.env'
    env.write_text(f'CONFIG={old}/payload\n')
    (dropin / 'override.conf').write_text('[Service]\n' + assignment.format(env=env) + '\n')
    monkeypatch.setattr(retention, 'service_roots', lambda row: [units])
    monkeypatch.setattr(retention, 'service_dependencies', SERVICE_DEPENDENCIES)
    assert prune(root)['status'] == 'deferred'
    assert old.is_dir() and (old / 'payload').read_text() == 'installed bytes'


@pytest.mark.parametrize('continued', [False, True])
def test_environment_path_with_spaces_is_one_filename(fleet, tmp_path, monkeypatch, continued):
    root, _, activate = fleet
    old, _ = activate('first')
    activate('second')
    activate('third')
    units = tmp_path / 'units'
    units.mkdir()
    env = tmp_path / 'external settings.env'
    env.write_text(f'CONFIG={old}/payload\n')
    name = str(env).replace('external settings', 'external\\\nsettings') if continued else str(env)
    (units / 'dormant.service').write_text(f'[Service]\nEnvironmentFile=-{name}\n')
    monkeypatch.setattr(retention, 'service_roots', lambda row: [units])
    monkeypatch.setattr(retention, 'service_dependencies', SERVICE_DEPENDENCIES)
    assert prune(root)['status'] == 'deferred'
    assert old.is_dir()


@pytest.mark.parametrize('value', [
    '-{env}\\x20suffix', '-"{env}"', "-'{env}'", '-{env}/%i', '-{env}/*',
    '-{env}/$NAME', '-relative.env', '--{env}',
])
def test_unresolved_environment_assignment_never_deletes(fleet, tmp_path, monkeypatch, value):
    root, _, activate = fleet
    old, _ = activate('first')
    activate('second')
    activate('third')
    units = tmp_path / 'units'
    units.mkdir()
    (units / 'dormant.service').write_text(
        '[Service]\nEnvironmentFile = ' + value.format(env=tmp_path / 'external.env') + '\n')
    monkeypatch.setattr(retention, 'service_roots', lambda row: [units])
    monkeypatch.setattr(retention, 'service_dependencies', SERVICE_DEPENDENCIES)
    with pytest.raises(ValueError, match='environment'):
        prune(root)
    assert old.is_dir()


def test_optional_missing_environment_and_empty_assignment_allow_retirement(fleet, tmp_path, monkeypatch):
    root, _, activate = fleet
    old, _ = activate('first')
    activate('second')
    activate('third')
    units = tmp_path / 'units'
    units.mkdir()
    (units / 'dormant.service').write_text(
        '[Service]\nEnvironmentFile =\n'
        f'EnvironmentFile = -{tmp_path}/missing.env\n'
        '# EnvironmentFile = /comment/missing.env\n'
        '; EnvironmentFile = /comment/missing.env\n')
    monkeypatch.setattr(retention, 'service_roots', lambda row: [units])
    monkeypatch.setattr(retention, 'service_dependencies', SERVICE_DEPENDENCIES)
    assert prune(root)['status'] == 'complete'
    assert not old.exists()


@pytest.mark.parametrize('failure', ['none', 'wrong-protocol', 'offline', 'restarted', 'native-config'])
def test_wrapper_acceptance_binds_service_owner_public_route_and_fresh_config(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace
    from cc_remote import update_relay
    from deploy import work_registry_snapshot
    import dotenv

    root = tmp_path.resolve()
    release = root / 'releases/current'
    release.mkdir(parents=True)
    (release / 'release-manifest.json').write_text(json.dumps({
        'schema': 1, 'product_version': '4.0.9', 'protocol_version': 74,
        'git_sha': 'a' * 40, 'role': 'wrapper', 'os': 'linux', 'arch': 'x86_64',
        'python': '3.13.9', 'uv': '0.11.16',
    }))
    (root / 'installation.json').write_text(json.dumps({'schema': 1, 'user': 'service-owner'}))
    (root / 'current').symlink_to(release)
    row = {'release': str(release), 'role': 'wrapper', 'home': str(root),
           'service_file': '/etc/wrapper.service', 'started_at': 123, 'snapshot': str(root / 'snapshot')}
    calls = []
    identities = iter([{'pid': 1}, {'pid': 2 if failure == 'restarted' else 1}])
    monkeypatch.setattr(retention.sys, 'platform', 'linux')
    monkeypatch.setattr(retention, 'os', SimpleNamespace(geteuid=lambda: 0))
    monkeypatch.setattr(retention.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_name='service-owner', pw_uid=1000))
    monkeypatch.setattr(retention, 'service_identity', lambda row: next(identities))
    monkeypatch.setattr(retention.time, 'sleep', lambda _: None)
    monkeypatch.setattr(dotenv, 'dotenv_values', lambda _: {'CC_REMOTE_MACHINE_ID': 'fixture-machine'})
    monkeypatch.setattr(work_registry_snapshot, 'resolve_wrapper_state_dir', lambda *a, **k: root / 'private')
    monkeypatch.setattr(update_relay, 'relay_origin', lambda _: 'https://relay.test')
    monkeypatch.setattr(update_relay, '_get_json', lambda _: {'machines': [] if failure == 'offline' else ['fixture-machine']})
    monkeypatch.setattr(update_relay, 'relay_release', lambda _: {'version': '4.0.9', 'protocol': 75 if failure == 'wrong-protocol' else 74})
    def command(argv):
        calls.append(argv)
        if failure == 'native-config':
            raise ValueError('native config failed')
        return ''
    monkeypatch.setattr(retention, 'command', command)
    if failure == 'none':
        retention.acceptance(root, row)
        assert any('verify' in call and str(root / 'snapshot') in call for call in calls)
    else:
        with pytest.raises(ValueError):
            retention.acceptance(root, row)
    assert calls[0][:4] == ['runuser', '-u', 'service-owner', '--']
    assert calls[0][-3:] == ['--live-config', '--state-dir', str(root / 'private')]


def test_stopped_independent_service_pointer_keeps_its_old_release(fleet):
    root, _, activate = fleet
    first, _ = activate('first')
    (root / 'service-current').symlink_to(first)
    activate('second')
    activate('third')
    assert prune(root)['status'] == 'deferred'
    assert first.is_dir()
