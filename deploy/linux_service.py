"""Linux Wrapper service bindings shared by install, update and retention.

Supervisor uses its local Unix XML-RPC endpoint. Only the bound program is
started/stopped/reloaded; the daemon and other programs are never restarted.
"""
from __future__ import annotations

import argparse
import configparser
import glob
import http.client
import json
from pathlib import Path
import pwd
import re
import shlex
import socket
import stat
import subprocess
import sys
import xmlrpc.client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
ENV_KEYS = frozenset({"HOME", "CLAUDE_BIN", "CLAUDE_WORK_ROOT", "CODEX_WORK_ROOT",
                      "CC_REMOTE_STATE_DIR", "RELAY_URL", "WRAPPER_TOKEN", "CC_REMOTE_MACHINE_ID",
                      "CC_REMOTE_DEVICE_CONFIG", "CC_REMOTE_CLAUDE_SERVICE_SOCKET", "CC_REMOTE_SUPERVISOR_UNSET_ENV"})
UNSET_KEYS = frozenset({"CLAUDE_CONFIG_DIR", "CODEX_HOME", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV",
                        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                        "http_proxy", "https_proxy", "all_proxy", "no_proxy"})
FIELDS = {"manager", "name", "user", "home", "service_file", "env_file", "device_file", "supervisor_config"}


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise ValueError(reason)


def absolute(value: str) -> Path:
    require(isinstance(value, str) and value.startswith('/') and not re.search(r'[\s%\"\'\\\x00-\x1f]', value),
            'Linux service paths must be absolute, without whitespace or expansion characters')
    path = Path(value)
    require(path.resolve() == path, 'Linux service paths must be canonical (no symlink ancestors)')
    return path


def trusted(path: Path) -> None:
    """No elevated launcher may execute a tree replaceable by its service user."""
    for item in [path, *path.parents]:
        if not item.exists():
            continue
        info = item.lstat()
        require(info.st_uid == 0 and not stat.S_ISLNK(info.st_mode)
                and (not info.st_mode & 0o022 or (item != path and item.is_dir() and info.st_mode & stat.S_ISVTX)),
                'Linux managed paths must be root-owned and protected from replacement')


def validate(profile: dict) -> dict:
    require(isinstance(profile, dict) and set(profile) == FIELDS, 'invalid Linux service binding')
    require(all(isinstance(value, str) for value in profile.values()), 'invalid Linux service field')
    require(profile['manager'] == 'supervisor', 'unsupported Linux service binding')
    require(bool(NAME.fullmatch(profile['name'])) and bool(NAME.fullmatch(profile['user'])), 'invalid service/user name')
    for key in ('home', 'service_file', 'env_file', 'device_file'):
        absolute(profile[key])
    absolute(profile['supervisor_config'])
    return profile


def read_config(path: Path) -> configparser.ConfigParser:
    trusted(path)
    require(path.is_file() and path.stat().st_size <= 1024 * 1024, 'missing/oversized service configuration')
    config = configparser.ConfigParser(interpolation=None, strict=True)
    config.read_string(path.read_text())
    return config


def config_files(main: Path) -> list[Path]:
    config = read_config(main)
    result = [main]
    patterns = config.get('include', 'files', fallback='').replace('%(here)s', str(main.parent))
    require('%' not in patterns, 'dynamic Supervisor includes require explicit reconciliation')
    for pattern in shlex.split(patterns):
        result.extend(Path(p).resolve() for p in sorted(glob.glob(str(main.parent / pattern))))
    require(len(result) <= 256, 'too many Supervisor include files')
    return list(dict.fromkeys(result))


def supervisor_program(profile: dict, *, optional: bool = False) -> configparser.SectionProxy | None:
    main = Path(profile['supervisor_config'])
    expected = Path(profile['service_file'])
    files = config_files(main)
    section = 'program:' + profile['name']
    matches = [(path, read_config(path)) for path in files]
    matches = [(path, config) for path, config in matches if config.has_section(section)]
    if not matches and optional and not expected.exists():
        config = read_config(main)
        import fnmatch
        patterns = config.get('include', 'files', fallback='').replace('%(here)s', str(main.parent))
        require(any(fnmatch.fnmatch(str(expected), str(main.parent / p)) for p in shlex.split(patterns)),
                'new Supervisor program file is not covered by the existing include pattern')
        return None
    require(len(matches) == 1 and matches[0][0] == expected,
            'Supervisor program is missing, duplicated or belongs to another file')
    config = matches[0][1]
    program = config[section]
    require(program.get('numprocs', '1') == '1'
            and program.get('process_name', '%(program_name)s') in {'%(program_name)s', profile['name']},
            'managed Wrapper needs one named Supervisor process')
    return program


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__('localhost', timeout=45)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class UnixTransport(xmlrpc.client.Transport):
    def __init__(self, path: str):
        super().__init__()
        self.path = path

    def make_connection(self, host):
        _, self._extra_headers, _ = self.get_host_info(host)
        return UnixConnection(self.path)


def rpc(profile: dict):
    main = Path(profile['supervisor_config'])
    config = read_config(main)
    url = config.get('supervisorctl', 'serverurl', fallback='').replace('%(here)s', str(main.parent))
    require(url.startswith('unix:///'), 'Supervisor management requires an existing local Unix socket')
    path = absolute(url[len('unix://'):])
    info = path.stat()
    require(stat.S_ISSOCK(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o002,
            'Supervisor control socket must be root-owned and not world-writable')
    # Credentials stay in the existing Supervisor config, never metadata/argv/logs.
    username = config.get('supervisorctl', 'username', fallback='')
    password = config.get('supervisorctl', 'password', fallback='')
    from urllib.parse import quote
    authority = f'{quote(username, safe="")}:{quote(password, safe="")}@' if username else ''
    return xmlrpc.client.ServerProxy(f'http://{authority}localhost/RPC2', transport=UnixTransport(str(path))).supervisor


def state(profile: dict) -> dict:
    try:
        info = rpc(profile).getProcessInfo(profile['name'])
    except xmlrpc.client.Fault as exc:
        if exc.faultCode == 10:  # BAD_NAME, e.g. before the first registration
            return {'running': False, 'pid': 0, 'start': 0}
        raise ValueError('Supervisor could not inspect the bound program') from None
    return {'running': info['statename'] == 'RUNNING', 'pid': info['pid'], 'start': info['start']}


def stop(profile: dict) -> None:
    server = rpc(profile)
    try:
        server.stopProcess(profile['name'], True)
    except xmlrpc.client.Fault as exc:
        require(exc.faultCode in {10, 70}, 'Supervisor could not stop the bound program')


def remove(profile: dict) -> None:
    """Forget only a stopped, no-longer-configured group after fresh rollback."""
    require(supervisor_program(profile, optional=True) is None,
            'refusing to remove a configured Supervisor program')
    server = rpc(profile)
    name = profile['name']
    added, changed, removed = server.reloadConfig()[0]
    require(name not in added and name not in changed,
            'Supervisor program reappeared during rollback')
    try:
        info = server.getProcessInfo(name)
    except xmlrpc.client.Fault as exc:
        require(exc.faultCode == 10, 'Supervisor could not inspect the rolled-back program')
        return  # already absent, including a failure before registration
    require(name in removed and not info['pid'] and info['statename'] in {'STOPPED', 'EXITED', 'FATAL'},
            'stop the removed Wrapper before forgetting its Supervisor group')
    server.removeProcessGroup(name)
    try:
        server.getProcessInfo(name)
    except xmlrpc.client.Fault as exc:
        require(exc.faultCode == 10, 'Supervisor could not verify rolled-back group removal')
        return
    raise ValueError('rolled-back Supervisor group is still loaded')


def start(profile: dict) -> None:
    server = rpc(profile)
    added, changed, removed = server.reloadConfig()[0]
    name = profile['name']
    if name in changed or name in removed:
        require(not state(profile)['pid'], 'stop the Wrapper before reloading its configuration')
        server.removeProcessGroup(name)
    if name in added or name in changed:
        server.addProcessGroup(name)
    if name not in removed:
        # addProcessGroup may autostart this program. Never touch others.
        info = server.getProcessInfo(name)
        if info['statename'] not in {'STARTING', 'RUNNING'}:
            server.startProcess(name, True)


def parse_environment(value: str) -> dict[str, str]:
    lexer = shlex.shlex(value, posix=True)
    lexer.whitespace = ','
    lexer.whitespace_split = True
    lexer.commenters = ''
    result = {}
    for item in lexer:
        key, sep, value = item.strip().partition('=')
        require(bool(sep), 'invalid Supervisor environment')
        if key in ENV_KEYS:
            require('%' not in value, 'dynamic deployment environment requires explicit reconciliation')
            result[key] = value
    return result


def unset_keys(value: str) -> set[str]:
    keys = {key.strip() for key in value.split(',') if key.strip()}
    require(keys <= UNSET_KEYS, 'only account, Python and proxy selectors may be unset')
    return keys


def environment(profile: dict) -> dict[str, str]:
    from dotenv import dotenv_values
    result = {}
    if profile['manager'] == 'supervisor':
        main = read_config(Path(profile['supervisor_config']))
        program = supervisor_program(profile, optional=True)
        result.update(parse_environment(main.get('supervisord', 'environment', fallback='')))
        if program is not None:
            result.update(parse_environment(program.get('environment', '')))
        home = Path(profile['home'])
        # The bootstrap defines these even if the daemon inherited other values.
        result.update(HOME=str(home), CC_REMOTE_STATE_DIR=str(home / '.cc-remote'),
                      CLAUDE_WORK_ROOT=str(home / '.claude/cc-remote/work'),
                      CODEX_WORK_ROOT=str(home / '.codex/cc-remote/work'))
    for key in ('env_file', 'device_file'):
        path = Path(profile[key])
        if path.exists() and not (key == 'device_file' and path.suffix == '.json'):
            trusted(path)
            result.update({k: v for k, v in dotenv_values(path, interpolate=False).items() if k in ENV_KEYS and v is not None})
    unset_keys(result.pop('CC_REMOTE_SUPERVISOR_UNSET_ENV', ''))
    device = Path(profile['device_file'])
    if device.suffix == '.json':
        result['CC_REMOTE_DEVICE_CONFIG'] = str(device)
        if device.exists():
            require(device.is_file() and not device.is_symlink() and device.stat().st_size <= 65536,
                    'invalid device credential file')
            config = json.loads(device.read_text())
            for env_key, file_key in [('RELAY_URL', 'relay_url'), ('WRAPPER_TOKEN', 'wrapper_token'),
                                      ('CC_REMOTE_MACHINE_ID', 'machine_id')]:
                if not result.get(env_key) and config.get(file_key):
                    result[env_key] = config[file_key]
    return result


def replace_program(original: str, name: str, replacement: str) -> str:
    """Change one section byte-for-byte; keep other programs and daemon policy."""
    sections = list(re.finditer(r'^\[([^]\n]+)\][^\n]*(?:\n|$)', original, re.M))
    matches = [(m.start(), sections[i+1].start() if i+1 < len(sections) else len(original))
               for i, m in enumerate(sections) if m.group(1) == 'program:' + name]
    require(len(matches) == 1, 'expected one Supervisor program section')
    start, end = matches[0]
    # Preserve operator logging/restart/environment settings. Only identity and
    # command are replaced; Wrapper shutdown must not kill native descendants.
    changed = {'command', 'directory', 'user', 'stopasgroup', 'killasgroup'}
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(replacement)
    values = config['program:' + name]
    old = original[start:end].splitlines(keepends=True)
    kept = [old[0]]
    dropping = False
    for line in old[1:]:
        match = re.match(r'^([A-Za-z_]+)\s*=', line)
        if match:
            dropping = match.group(1) in changed
        elif line and not line[0].isspace():
            dropping = False
        if not dropping:
            kept.append(line)
    body = ''.join(kept).rstrip('\n') + '\n'
    body += ''.join(f'{key}={values[key]}\n' for key in sorted(changed)) + '\n'
    return original[:start] + body + original[end:]


def expected_command(root: Path, profile: dict) -> list[str]:
    return [str(root / 'current/.venv/bin/python'), '-I', '-B', str(root / 'current/deploy/wrapper_exec.py'),
            '--user', profile['user'], '--home', profile['home'], '--env-file', profile['env_file'],
            '--device-file', profile['device_file']]


def preflight(root: Path, profile: dict, *, adopt: bool = False) -> None:
    """Check live ownership/configuration before stopping or registering anything."""
    validate(profile)
    trusted(root)
    for key in ('service_file', 'env_file', 'device_file'):
        if key != 'device_file' or Path(profile[key]).suffix != '.json':
            trusted(Path(profile[key]))
    account = pwd.getpwnam(profile['user'])
    require(Path(profile['home']).is_dir(), 'service HOME is missing')
    metadata = root / 'installation.json'
    if metadata.exists():
        saved = json.loads(metadata.read_text()).get('linux_service')
        require(saved == profile, 'registered service binding differs; do not retarget an installation')
    program = supervisor_program(profile, optional=True)
    server = rpc(profile)
    env = environment(profile)
    cli = Path(env.get('CLAUDE_BIN') or str(Path(profile['home']) / '.local/bin/claude'))
    require(cli.is_file() and cli.stat().st_mode & 0o111, 'daily Claude CLI is missing')
    require(env.get('HOME', account.pw_dir) == profile['home'], 'service HOME differs from the installation binding')

    if program is None:
        require(not adopt, 'adoption requires an existing Supervisor program')
        require(not state(profile)['pid'], 'Supervisor already owns an unbound Wrapper process')
        return
    command = shlex.split(program.get('command', ''))
    managed = command == expected_command(root, profile)
    require(managed or adopt, 'custom Supervisor command requires explicit --adopt-supervisor and reconciled environment')
    if adopt:
        require((root / 'current').is_symlink() and (root / 'current').resolve().parent == root / 'releases',
                'adoption requires the existing immutable root')
        require(Path(profile['env_file']).is_file(), 'adoption requires an explicit external Wrapper environment file')
    if managed:
        require(program.get('directory') == str(root / 'current'), 'Supervisor Wrapper directory mismatch')
    expected_user = program.get('user', read_config(Path(profile['supervisor_config'])).get('supervisord', 'user', fallback='root'))
    require(expected_user == ('root' if managed else profile['user']), 'Supervisor Wrapper user mismatch')
    configs = [x for x in server.getAllConfigInfo() if x['name'] == profile['name']]
    require(len(configs) == 1 and configs[0]['group'] == profile['name']
            and shlex.split(configs[0]['command']) == command,
            'loaded Supervisor config differs from disk; reconcile it before updating')
    expected = {'directory': program.get('directory', 'none'),
                'uid': pwd.getpwnam(expected_user).pw_uid if 'user' in program else 'none'}
    require(all(key not in configs[0] or configs[0][key] == value for key, value in expected.items()),
            'loaded Supervisor config differs from disk; reconcile it before updating')
    # Before 4.2.5 getAllConfigInfo omits directory/uid. The native comparison
    # below still checks both against the active process-group configuration
    # (even for stopped programs). It reads config without applying changes;
    # never skip it just because the fields above are absent or match.
    changes = server.reloadConfig()[0]
    require(not any(profile['name'] in group for group in changes),
            'Supervisor program has unapplied configuration changes; reconcile it before updating')


def render(root: Path, profile: dict, template: Path) -> None:
    service = Path(profile['service_file'])
    service.parent.mkdir(parents=True, exist_ok=True)
    text = f'''[program:{profile['name']}]
command={shlex.join(expected_command(root, profile))}
directory={root}/current
user=root
autostart=true
autorestart=unexpected
startsecs=1
stopsignal=TERM
stopwaitsecs=30
stopasgroup=false
killasgroup=false
umask=0077
stdout_logfile=AUTO
stderr_logfile=AUTO
'''
    if service.exists():
        text = replace_program(service.read_text(), profile['name'], text)
    from deploy.install_cli import _atomic_file
    _atomic_file(service, text.encode(), stat.S_IMODE(service.stat().st_mode) if service.exists() else 0o600)


def restore_program(profile: dict, backup: Path) -> None:
    """Rollback only the named program, retaining concurrent unrelated edits."""
    original = backup.read_text()
    section = re.search(r'^\[program:' + re.escape(profile['name']) + r'\][^\n]*\n.*?(?=^\[|\Z)',
                        original, re.M | re.S)
    require(section is not None, 'rollback program definition missing')
    path = Path(profile['service_file'])
    current = path.read_text()
    matches = list(re.finditer(r'^\[program:' + re.escape(profile['name']) + r'\][^\n]*\n.*?(?=^\[|\Z)',
                              current, re.M | re.S))
    require(len(matches) == 1, 'rollback program definition changed')
    match = matches[0]
    from deploy.install_cli import _atomic_file
    _atomic_file(path, (current[:match.start()] + section.group() + current[match.end():]).encode(),
                 stat.S_IMODE(path.stat().st_mode))


def verify_health(root: Path, profile: dict) -> None:
    import time
    from cc_remote.update import Installation
    from cc_remote.update_relay import relay_origin, relay_release, _get_json
    from deploy.release_manifest import load_manifest
    before = state(profile)
    require(before['running'] and before['pid'] > 0, 'Wrapper is not running')
    manifest = load_manifest(root / 'current/release-manifest.json')
    installation = Installation(root, (root / 'current').resolve(), manifest, {'linux_service': profile})
    origin = relay_origin(installation)
    public = relay_release(origin)
    require(public['protocol'] == manifest['protocol_version'], 'Relay protocol does not match')
    machine = environment(profile).get('CC_REMOTE_MACHINE_ID') or 'default'
    connected = False
    for _ in range(15):
        require(state(profile) == before, 'Wrapper restarted during acceptance')
        if machine in _get_json(origin + '/healthz').get('machines', []):
            connected = True
            break
        time.sleep(1)
    require(connected, 'Wrapper did not reconnect to Relay')
    time.sleep(2)
    require(state(profile) == before, 'Wrapper restarted during acceptance')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['preflight', 'state', 'stop', 'remove', 'start', 'render', 'snapshot', 'probe-state', 'health', 'restore'])
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--adopt', action='store_true')
    parser.add_argument('--template', type=Path)
    args = parser.parse_args(argv)
    try:
        profile = validate(json.loads(args.profile.read_text()))
        if args.action == 'preflight':
            preflight(args.root, profile, adopt=args.adopt)
        elif args.action == 'state':
            return 0 if state(profile)['running'] else 1
        elif args.action == 'health':
            verify_health(args.root, profile)
        elif args.action == 'stop':
            stop(profile)
        elif args.action == 'remove':
            remove(profile)
        elif args.action == 'start':
            start(profile)
        elif args.action == 'restore':
            restore_program(profile, args.template)
        elif args.action == 'render':
            render(args.root, profile, args.template)
        else:
            from deploy.work_registry_snapshot import create_snapshot, resolve_work_roots, resolve_wrapper_state_dir
            env = environment(profile)
            # Only three path selectors reach snapshot resolution, no credentials.
            home = Path(profile['home'])
            roots = resolve_work_roots(home, environment=env)
            state_dir = resolve_wrapper_state_dir(home, environment=env)
            if args.action == 'snapshot':
                create_snapshot(args.destination, roots, state_dir=state_dir)
            else:
                print(state_dir)
        return 0
    except (OSError, ValueError, KeyError, configparser.Error, xmlrpc.client.Error, subprocess.SubprocessError) as exc:
        reason = str(exc) if type(exc) is ValueError else type(exc).__name__
        print(f'Linux service operation failed: {reason}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
