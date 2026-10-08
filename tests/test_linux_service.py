"""Supervisor identity, scoped configuration changes and updater bindings."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import xmlrpc.client

import pytest

from deploy import linux_service as service
from deploy import install_cli
from cc_remote import update, update_relay


@pytest.fixture
def binding(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    home = root / 'home'
    home.mkdir()
    profile = dict(manager='supervisor', name='wrapper', user='root', home=str(home),
                   service_file=str(root / 'supervisor.conf'), env_file=str(root / 'wrapper.env'),
                   device_file=str(home / 'device.json'), supervisor_config=str(root / 'supervisor.conf'))
    Path(profile['service_file']).write_text('''[supervisord]
user=root
[supervisorctl]
serverurl=unix:///run/fixture.sock
[program:claude-service]
command=/usr/bin/python3 /opt/native-service.py
[program:wrapper]
command=/usr/bin/python3 /opt/legacy-wrapper.py
autostart=true
stdout_logfile=/var/log/wrapper.log
stopasgroup=true
''')
    Path(profile['env_file']).write_text(f'HOME={home}\nCC_REMOTE_STATE_DIR={home}/private\n')
    Path(profile['device_file']).write_text(json.dumps({'relay_url': 'wss://relay.test/ws',
                                                     'wrapper_token': 'private-fixture-token', 'machine_id': 'device'}))
    monkeypatch.setattr(service, 'trusted', lambda _: None)
    return root, profile


def test_named_program_rewrite_preserves_other_sections_and_policy(binding):
    root, profile = binding
    path = Path(profile['service_file'])
    before = path.read_text()
    service.render(root, profile, root / 'unused')
    after = path.read_text()
    assert after.split('[program:wrapper]')[0] == before.split('[program:wrapper]')[0]
    assert 'stdout_logfile=/var/log/wrapper.log' in after
    assert 'stopasgroup=false' in after
    assert 'wrapper_exec.py' in after
    assert service.supervisor_program(profile)['directory'] == str(root / 'current')
    # A second normal upgrade is idempotent, not a growing duplicate section.
    service.render(root, profile, root / 'unused')
    assert path.read_text() == after
    backup = root / 'backup'
    backup.write_text(before)
    path.write_text(after.replace('/opt/native-service.py', '/opt/new-native-service.py'))
    service.restore_program(profile, backup)
    restored = path.read_text()
    assert restored.split('[program:wrapper]')[1] == before.split('[program:wrapper]')[1]
    assert '/opt/new-native-service.py' in restored


def test_selectors_pairing_and_snapshot_share_profile(binding):
    from deploy.work_registry_snapshot import resolve_work_roots, resolve_wrapper_state_dir
    root, profile = binding
    env = service.environment(profile)
    home = Path(profile['home'])
    assert env['RELAY_URL'] == 'wss://relay.test/ws'
    assert env['CC_REMOTE_DEVICE_CONFIG'] == profile['device_file']
    assert resolve_wrapper_state_dir(home, environment=env) == home / 'private'
    assert resolve_work_roots(home, environment=env)['claude'] == home / '.claude/cc-remote/work'
    installation = update.Installation(root, root, {'os': 'linux'}, {'linux_service': profile})
    assert update_relay.relay_origin(installation) == 'https://relay.test'
    Path(profile['env_file']).write_text('RELAY_URL=wss://override.test/ws\n')
    assert update_relay.relay_origin(installation) == 'https://override.test'


def test_start_reload_changes_only_the_selected_program(binding, monkeypatch):
    _, profile = binding
    calls = []
    server = SimpleNamespace(
        reloadConfig=lambda: [[[], ['wrapper', 'claude-service'], ['unrelated']]],
        getProcessInfo=lambda name: {'statename': 'STOPPED', 'pid': 0, 'start': 0},
        removeProcessGroup=lambda name: calls.append(('remove', name)),
        addProcessGroup=lambda name: calls.append(('add', name)),
        startProcess=lambda name, wait: calls.append(('start', name)),
    )
    monkeypatch.setattr(service, 'rpc', lambda _: server)
    service.start(profile)
    assert calls == [('remove', 'wrapper'), ('add', 'wrapper'), ('start', 'wrapper')]


def test_start_refuses_reconfiguring_a_live_process(binding, monkeypatch):
    _, profile = binding
    server = SimpleNamespace(reloadConfig=lambda: [[[], ['wrapper'], []]],
                             getProcessInfo=lambda _: {'statename': 'STARTING', 'pid': 123, 'start': 1})
    monkeypatch.setattr(service, 'rpc', lambda _: server)
    with pytest.raises(ValueError, match='stop the Wrapper'):
        service.start(profile)


@pytest.mark.parametrize('code,success', [(10, True), (70, True), (1, False)])
def test_stop_does_not_hide_arbitrary_supervisor_failures(binding, monkeypatch, code, success):
    _, profile = binding
    def stop(name, wait):
        assert name == 'wrapper' and wait
        raise xmlrpc.client.Fault(code, 'fixture')
    monkeypatch.setattr(service, 'rpc', lambda _: SimpleNamespace(stopProcess=stop))
    if success:
        service.stop(profile)
    else:
        with pytest.raises(ValueError):
            service.stop(profile)


@pytest.fixture
def fresh_binding(binding):
    root, profile = binding
    main = Path(profile['supervisor_config'])
    main.write_text(main.read_text().split('[program:wrapper]')[0]
                    + f'[include]\nfiles={root}/fresh.conf\n')
    return root, {**profile, 'service_file': str(root / 'fresh.conf')}


@pytest.mark.parametrize('loaded', [False, True])
def test_remove_fresh_group_is_scoped_and_idempotent(fresh_binding, monkeypatch, loaded):
    _, profile = fresh_binding
    groups = {'wrapper', 'claude-service'} if loaded else {'claude-service'}
    calls = []

    def info(name):
        if name not in groups:
            raise xmlrpc.client.Fault(10, 'BAD_NAME')
        return {'statename': 'STOPPED', 'pid': 0}

    def remove(name):
        calls.append(name)
        groups.remove(name)

    server = SimpleNamespace(
        reloadConfig=lambda: [[['unrelated-added'], ['claude-service'], list(groups)]],
        getProcessInfo=info, removeProcessGroup=remove,
    )
    monkeypatch.setattr(service, 'rpc', lambda _: server)
    service.remove(profile)
    service.remove(profile)
    assert groups == {'claude-service'}
    assert calls == (['wrapper'] if loaded else [])


@pytest.mark.parametrize('failure', ['configured', 'running', 'reappeared', 'unreported',
                                     'reload', 'inspect', 'remove', 'still-loaded'])
def test_remove_fresh_group_refuses_incomplete_rollback(fresh_binding, monkeypatch, failure):
    _, profile = fresh_binding
    if failure == 'configured':
        Path(profile['service_file']).write_text('[program:wrapper]\ncommand=/bin/true\n')
    calls = []

    def reload_config():
        if failure == 'reload':
            raise xmlrpc.client.Fault(92, 'CANT_REREAD')
        return [[[], ['wrapper'] if failure == 'reappeared' else [],
                 [] if failure == 'unreported' else ['wrapper']]]

    def info(name):
        assert name == 'wrapper'
        if failure == 'inspect':
            raise xmlrpc.client.Fault(1, 'UNKNOWN_METHOD')
        return {'statename': 'RUNNING' if failure == 'running' else 'STOPPED',
                'pid': 123 if failure == 'running' else 0}

    def remove(name):
        calls.append(name)
        if failure == 'remove':
            raise xmlrpc.client.Fault(91, 'STILL_RUNNING')

    monkeypatch.setattr(service, 'rpc', lambda _: SimpleNamespace(
        reloadConfig=reload_config, getProcessInfo=info, removeProcessGroup=remove))
    with pytest.raises((ValueError, xmlrpc.client.Fault)):
        service.remove(profile)
    assert calls == (['wrapper'] if failure in {'remove', 'still-loaded'} else [])


def test_unix_transport_preserves_authentication():
    transport = service.UnixTransport('/run/fixture.sock')
    connection = transport.make_connection('fixture:password@localhost')
    assert connection.path == '/run/fixture.sock'
    assert any(key == 'Authorization' and value.startswith('Basic ') for key, value in transport._extra_headers)


def test_root_custom_registration_round_trip_and_relay_launcher_preservation(binding, monkeypatch):
    root, profile = binding
    release = root / 'releases/v1'
    (release / 'bin').mkdir(parents=True)
    (root / 'current').symlink_to(release)
    repo = Path(__file__).resolve().parents[1]
    (release / 'bin/cc-remote').write_bytes((repo / 'scripts/cc-remote').read_bytes())
    (release / 'release-manifest.json').write_text(json.dumps(dict(
        schema=1, product_version='4.0.9', protocol_version=74, git_sha='a'*40,
        role='wrapper', os='linux', arch='arm64', python='3.13.9', uv='0.11.16')))
    cli = root / 'bin/cc-remote'
    install_cli.install_cli(root, cli, role='wrapper', user='root', linux_service=profile)
    assert 'managed_wrapper_root=' + str(root) in cli.read_text()
    assert update.read_installation(root, 'linux', 'arm64').metadata['linux_service'] == profile
    monkeypatch.setenv('CC_REMOTE_MANAGED_ROOT', str(root))
    assert update.installation_roots('linux')['wrapper'] == root
    # Root remains rejected without an explicit bound service profile.
    with pytest.raises(ValueError, match='original service user'):
        install_cli.install_cli(root, cli, role='wrapper', user='root')
    relay = root / 'relay'
    relay_release = relay / 'releases/v1'
    (relay_release / 'bin').mkdir(parents=True)
    (relay / 'current').symlink_to(relay_release)
    (relay_release / 'bin/cc-remote').write_bytes((repo / 'scripts/cc-remote').read_bytes())
    install_cli.install_cli(relay, cli, role='relay', domain='relay.test')
    assert 'managed_wrapper_root=' + str(root) in cli.read_text()


def test_root_upstream_ssh_does_not_require_sudo(binding, monkeypatch):
    root, profile = binding
    installation = update.Installation(root, root, {'os': 'linux'}, {'user': 'root', 'linux_service': profile})
    monkeypatch.setattr(update_relay.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(update_relay.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_name='root'))
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='ok')
    monkeypatch.setattr(update_relay.subprocess, 'run', run)
    assert update_relay.RelayUpdate(installation, 'relay-host')._ssh(['true']) == 'ok'
    assert calls[0][0] == '/usr/bin/ssh'


def test_trusted_rejects_user_writable_managed_root(tmp_path):
    # Root check is meaningful on both the normal developer account and root CI.
    path = tmp_path / 'untrusted'
    path.mkdir(mode=0o777)
    path.chmod(0o777)
    with pytest.raises(ValueError, match='root-owned'):
        service.trusted(path)


@pytest.mark.parametrize('value', ['HOME', 'CC_REMOTE_STATE_DIR', 'WRAPPER_TOKEN'])
def test_bootstrap_unset_cannot_change_snapshot_or_pairing_selectors(value):
    with pytest.raises(ValueError, match='only account'):
        service.unset_keys(value)


def test_supervisor_paths_and_dynamic_config_fail_closed(binding):
    _, profile = binding
    with pytest.raises(ValueError):
        service.validate({**profile, 'home': None})
    with pytest.raises(ValueError):
        service.validate({**profile, 'home': '/home/../root'})
    main = Path(profile['supervisor_config'])
    main.write_text('[include]\nfiles=%(ENV_CONFIG_DIR)s/*.conf\n')
    with pytest.raises(ValueError, match='dynamic Supervisor'):
        service.config_files(main)


def test_snapshot_uses_bound_private_state_directory(binding):
    root, profile = binding
    profile_file = root / 'binding.json'
    profile_file.write_text(json.dumps(profile))
    destination = root / 'snapshot'
    assert service.main(['snapshot', '--root', str(root), '--profile', str(profile_file),
                         '--destination', str(destination)]) == 0
    manifest = json.loads((destination / 'manifest.json').read_text())
    assert manifest['wrapper_state']['directory'] == str(Path(profile['home']) / 'private')
