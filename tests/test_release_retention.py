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
