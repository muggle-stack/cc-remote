"""Record installer recovery sets and retire only proven superseded generations.

Called by the *new bundle's* installer, including upgrades started by old CLIs.
Deletion belongs to cleanup.py; unknown legacy trees are never inferred by age.
"""
from __future__ import annotations

import argparse
from collections import deque
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy import cleanup as retirement
from deploy.install_lock import LOCK_FD_ENV, acquire_install_lock, verify_install_lock
from deploy.release_manifest import load_manifest

LEDGER = ".release-generations.json"
BACKUPS = "rollback-config"
INVENTORY = ".release-cleanup.json"
STATUS = ".retention-status.json"
DEFAULT_TZDIR = Path('/usr/share/zoneinfo')


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def read_ledger(root: Path) -> dict:
    if not (root / LEDGER).exists():
        return {"schema": 1, "phase": "committed", "generations": []}
    data, _ = retirement.read_json(root / LEDGER, owner_uid=root.stat().st_uid)
    require(data.get("schema") == 1 and isinstance(data.get("generations"), list),
            "invalid release generation ledger")
    require(len(data["generations"]) <= 1024, "generation ledger exceeds limit; inspect retained files")
    ids = set()
    for row in data["generations"]:
        require(isinstance(row, dict) and re.fullmatch(r"[a-f0-9]{32}", row.get("id", ""))
                and row["id"] not in ids, "invalid or duplicate generation identity")
        ids.add(row["id"])
        release = retirement.absolute_path(row["release"])
        require(release.parent == root / "releases", "generation escapes releases")
        require(row["backup"] == str(root / BACKUPS / row["id"]), "invalid recovery directory")
        retirement.absolute_path(row["backup"])
        if row.get("snapshot"):
            require(retirement.absolute_path(row["snapshot"]).parent == root / "rollback-data",
                    "snapshot escapes rollback data")
        require(row.get("phase") in {"prepared", "committed", "aborted"},
                "unknown generation outcome; inspect before updating")
    return data


def compact_generations(data: dict) -> None:
    """Retain surviving payload provenance without duplicating rollback claims."""
    kept, retired = [], []
    for row in data['generations']:
        alive = row['phase'] == 'prepared' or any(
            p and Path(p).exists() for p in (row['release'], row['backup'], row.get('snapshot')))
        (kept if alive else retired).append(row)
    known = {path for row in kept for path in row.get('artifacts', {})}
    for row in retired:
        previous = row['previous']
        if previous and Path(previous).exists() and previous not in known:
            kept.append(row)
            known.update(row.get('artifacts', {}))
    identities = {row['id'] for row in kept}
    data['generations'] = [row for row in data['generations'] if row['id'] in identities]


def begin(root: Path, release: Path, previous: str, *, role: str, home: Path,
          service: str, service_file: Path, configs: list[Path], linux_service: dict | None = None) -> str:
    root = retirement.absolute_path(str(root))
    release = retirement.absolute_path(str(release))
    require(release.parent == root / "releases" and release.is_dir(), "invalid new release")
    manifest = load_manifest(release / "release-manifest.json")
    require(manifest["role"] == role, "release role mismatch")
    data = read_ledger(root)
    require(all(r["phase"] != "prepared" for r in data["generations"]),
            "unfinished installation record; inspect the original activation before retrying")
    compact_generations(data)
    require(len(data["generations"]) < 100, "too many retained generations; inspect retention")
    if previous:
        old = retirement.absolute_path(previous)
        require(old.is_dir() and old != release, "invalid previous release")
        require(((root / "current").is_symlink() and (root / "current").resolve(strict=True) == old)
                or (not (root / "current").exists() and old.parent != root / "releases"),
                "previous release is not the active installation")
    identity = uuid.uuid4().hex
    backup = root / BACKUPS / identity
    row = dict(id=identity, release=str(release), previous=previous, backup=str(backup),
               snapshot=None, role=role, home=str(home), service=service,
               service_file=str(service_file), phase="prepared", started_at=time.time(), artifacts={})
    if linux_service is not None:
        from deploy.linux_service import validate
        row['linux_service'] = validate(linux_service)
    data["generations"].append(row)
    data["phase"] = "prepared"
    retirement.write_journal(root / LEDGER, data)
    backup.mkdir(parents=True, mode=0o700)
    files = []
    # Include absence, mode and destination: rollback needs the matching service
    # definition/configuration as well as code and the private-state snapshot.
    for i, path in enumerate(dict.fromkeys([service_file, *configs])):
        entry = {"destination": str(path), "file": str(i), "present": path.exists()}
        if path.is_symlink():
            raise ValueError("configuration backup must not follow a symlink")
        if path.exists():
            info = path.stat()
            require(stat.S_ISREG(info.st_mode) and info.st_size <= 4 * 1024 * 1024,
                    "unsafe or oversized configuration backup")
            entry.update(mode=stat.S_IMODE(info.st_mode), uid=info.st_uid, gid=info.st_gid)
            with path.open("rb") as source, (backup / str(i)).open("xb") as target:
                os.fchmod(target.fileno(), 0o600)
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
        files.append(entry)
    retirement.write_journal(backup / "files.json", {"schema": 1, "files": files})
    for path in [release, backup, *([Path(previous)] if previous else [])]:
        row["artifacts"][str(path)] = list(retirement.identity(path))
    retirement.write_journal(root / LEDGER, data)
    return identity


def generation(root: Path, identity: str) -> tuple[dict, dict]:
    data = read_ledger(root)
    rows = [r for r in data["generations"] if r["id"] == identity]
    require(len(rows) == 1, "unknown activation record")
    return data, rows[0]


def command(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=45, check=False)
    require(result.returncode == 0, f"read-only acceptance failed: {Path(argv[0]).name}")
    return result.stdout


def service_identity(row: dict) -> dict:
    if row.get('linux_service'):
        from deploy.linux_service import validate, state
        live = state(validate(row['linux_service']))
        require(live['running'] and live['pid'] > 0, 'Supervisor Wrapper is not running')
        return live
    if sys.platform == "darwin":
        uid = Path(row['home']).stat().st_uid
        raw = command(["launchctl", "print", f"gui/{uid}/{row['service']}"])
        fields = dict(re.findall(r"^\s*(state|pid|runs) = ([^\n]+)$", raw, re.M))
        require(fields.get("state") in {"running", "active"} and int(fields.get("pid", 0)) > 0,
                "Wrapper is not running")
        return {k: fields[k] for k in ("pid", "runs")}
    raw = command(["systemctl", "show", row["service"], "-p", "ActiveState", "-p", "MainPID",
                   "-p", "NRestarts", "-p", "ExecMainStartTimestampMonotonic"])
    fields = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    require(fields.get("ActiveState") == "active" and int(fields.get("MainPID", 0)) > 0,
            "service is not running")
    return fields


def acceptance(root: Path, row: dict) -> None:
    from dotenv import dotenv_values
    from cc_remote.update import Installation
    from cc_remote.update_relay import _get_json, relay_origin, relay_release

    release = Path(row["release"])
    require((root / "current").resolve(strict=True) == release, "active release changed")
    manifest = load_manifest(release / "release-manifest.json")
    live = service_identity(row)
    if row.get("service_identity"):
        require(live == row["service_identity"], "service restarted; retain recovery files")
    metadata, _ = retirement.read_json(root / "installation.json", owner_uid=root.stat().st_uid)
    python = str(release / ".venv/bin/python")
    if row["role"] == "relay":
        origin = dotenv_values(root / ".env").get("PUBLIC_ORIGIN")
        require(isinstance(origin, str) and bool(origin), "Relay public origin missing")
        expected_version = manifest["product_version"]
    else:
        installation = Installation(root, release, manifest, metadata)
        origin = (relay_origin(installation, home=Path(row['home']))
                  if sys.platform == 'darwin' and os.geteuid() == 0 else relay_origin(installation))
        expected_version = None  # A newer compatible Relay is allowed.
        home = Path(row["home"])
        if sys.platform == "darwin":
            environment = plistlib.loads(Path(row["service_file"]).read_bytes()).get("EnvironmentVariables", {})
            device = Path(environment.get("CC_REMOTE_DEVICE_CONFIG", str(home / ".cc-remote/device.json")))
            config = json.loads(device.read_text()) if device.exists() else {}
            machine = environment.get("CC_REMOTE_MACHINE_ID") or config.get("machine_id") or "default"
            selector = ["--plist", row["service_file"]]
            owner = pwd.getpwuid(home.stat().st_uid)
            prefix = (["/usr/bin/sudo", "-H", "-u", owner.pw_name, "--"]
                      if os.geteuid() == 0 and owner.pw_uid != 0 else [])
        else:
            from deploy.work_registry_snapshot import resolve_wrapper_state_dir
            if row.get('linux_service'):
                from deploy.linux_service import environment as service_environment
                environment = service_environment(row['linux_service'])
                state = resolve_wrapper_state_dir(home, environment=environment)
            else:
                environment = {**dotenv_values("/etc/cc-remote/wrapper.env"),
                               **dotenv_values("/etc/cc-remote/device.env")}
                state = resolve_wrapper_state_dir(home, env_file=Path("/etc/cc-remote/wrapper.env"))
            machine = environment.get("CC_REMOTE_MACHINE_ID") or "default"
            selector = ["--state-dir", str(state)]
            owner = pwd.getpwnam(metadata["user"])
            prefix = (["runuser", "-u", owner.pw_name, "--"] if os.geteuid() == 0 and owner.pw_uid != 0 else [])
        command([*prefix, python, "-I", "-B", str(release / "deploy/check_codex_readiness.py"),
                 "--home", str(home), "--release", str(release), "--after", str(row["started_at"]),
                 "--wait", "0", "--live-config", *selector])
        health = _get_json(origin + "/healthz")
        require(machine in health.get("machines", []), "updated Wrapper has not reconnected")
        require(bool(row.get("snapshot")), "matching private-state snapshot missing")
        command([python, "-B", str(release / "deploy/work_registry_snapshot.py"), "verify",
                 "--snapshot", row["snapshot"]])
    public = relay_release(origin)
    require(public["protocol"] == manifest["protocol_version"]
            and (expected_version is None or public["version"] == expected_version),
            "public Relay/Web build does not match")
    time.sleep(1)
    require(service_identity(row) == live, "service is unstable; retain recovery files")


def commit(root: Path, identity: str, snapshot: str | None = None) -> None:
    data, row = generation(root, identity)
    require(row["phase"] == "prepared", "activation already finalized; inspect instead of repeating")
    require((root / "current").resolve(strict=True) == Path(row["release"]), "activation not current")
    if snapshot:
        path = retirement.absolute_path(snapshot)
        require(path.parent == root / "rollback-data" and path.is_dir(), "invalid state snapshot")
        row["snapshot"] = str(path)
        row["artifacts"][str(path)] = list(retirement.identity(path))
    row.update(phase="committed", committed_at=time.time(), service_identity=service_identity(row))
    data["phase"] = "committed"
    retirement.write_journal(root / LEDGER, data)
    # A failed check leaves the committed installation and all recovery files
    # intact. It is not a reason to roll back a healthy, already registered app.
    acceptance(root, row)


def abort(root: Path, identity: str, *, rollback_verified: bool = False) -> None:
    data, row = generation(root, identity)
    require(row["phase"] == "prepared", "cannot abort a committed generation")
    if rollback_verified:
        if row['previous']:
            require((root / 'current').resolve(strict=True) == Path(row['previous']),
                    'rollback has not restored the previous release')
            row['rollback_service_identity'] = service_identity(row)
        else:
            require(not (root / 'current').exists() and not (root / 'current').is_symlink()
                    and not Path(row['service_file']).exists(), 'first-install rollback is incomplete')
            row['rollback_service_identity'] = {'state': 'absent'}
        row['rollback_verified_at'] = time.time()
    row["phase"] = "aborted"
    data["phase"] = "committed"
    retirement.write_journal(root / LEDGER, data)


def unit_paths(argv: list[str], *, lines: bool = False) -> list[Path]:
    raw = command(argv)
    require(len(raw) <= 65536, "systemd unit path discovery exceeds bound")
    values = raw.splitlines() if lines else shlex.split(raw)
    require(0 < len(values) <= 256, "systemd unit path discovery is incomplete")
    require(all(value.startswith('/') and not re.search(r'[\\\x00-\x1f]', value) for value in values),
            "systemd unit paths must be literal absolute paths")
    return [Path(value) for value in values]


class _UserManagerDiscoveryError(ValueError):
    """A manager observed as active could not supply its complete UnitPath."""


def service_roots(row: dict) -> list[Path]:
    # A login manager can exit between list-units and its D-Bus query. Restart
    # the *whole* discovery, never omit that manager or reuse a partial result.
    for attempt in range(3):
        try:
            return _service_roots_once(row)
        except _UserManagerDiscoveryError:
            if attempt == 2:
                raise
            time.sleep(0.1)
    raise AssertionError('service discovery did not finish')


def _service_roots_once(row: dict) -> list[Path]:
    home = Path(row["home"])
    if sys.platform == "darwin":
        return [home / "Library/LaunchAgents", Path("/Library/LaunchAgents"), Path("/Library/LaunchDaemons")]
    require(os.geteuid() == 0, "Linux service discovery requires visibility of every service user")
    # systemd-analyze includes the distribution's complete default load paths,
    # including generated/transient/control units and global user directories.
    # It does not query managers: also include their actual UnitPath overrides.
    # See systemd-analyze(1), "unit-paths". Failure is not an empty inventory.
    clean_env = ['env', '-i', 'PATH=/usr/sbin:/usr/bin:/sbin:/bin']
    roots = []
    for scope in ('system', 'global'):
        roots.extend(unit_paths([*clean_env, 'systemd-analyze', '--' + scope, 'unit-paths'], lines=True))
    roots.extend(unit_paths([*clean_env, 'systemctl', '--system', 'show', '-p', 'UnitPath', '--value']))
    active = command([*clean_env, 'systemctl', '--system', 'list-units', '--all', '--plain', '--no-legend',
                      '--no-pager', '--state=active,activating,reloading,deactivating', 'user@*.service'])
    require(len(active) <= 65536, "user manager discovery exceeds bound")
    active_users = set()
    for line in active.splitlines():
        match = re.fullmatch(r'user@(\d+)\.service\s+.+', line.strip())
        require(match is not None, "user manager discovery is incomplete")
        active_users.add(int(match[1]))
    accounts = pwd.getpwall()
    require(len(accounts) <= 1024, "service user discovery exceeds bound")
    for account in accounts:
        require(account.pw_dir.startswith('/'), "service user has an unknown HOME")
        user_home = Path(account.pw_dir)
        try:
            is_directory = stat.S_ISDIR(user_home.stat().st_mode)
        except FileNotFoundError:
            is_directory = False
        require(is_directory or account.pw_uid not in active_users, "active user manager HOME is unavailable")
        if not is_directory:
            continue
        runtime = f'/run/user/{account.pw_uid}'
        environment = [*clean_env, f'HOME={user_home}', f'XDG_RUNTIME_DIR={runtime}']
        roots.extend(unit_paths([*environment, 'systemd-analyze', '--user', 'unit-paths'], lines=True))
        if account.pw_uid in active_users:
            try:
                roots.extend(unit_paths([
                    'runuser', '-u', account.pw_name, '--', *environment,
                    f'DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus',
                    'systemctl', '--user', 'show', '-p', 'UnitPath', '--value',
                ]))
            except ValueError as exc:
                raise _UserManagerDiscoveryError(str(exc)) from exc
            active_users.remove(account.pw_uid)
    require(not active_users, "an active user manager has no discoverable service account")
    # A bound service can deliberately use a different HOME from passwd.
    roots.extend([home / '.config/systemd/user', home / '.config/systemd/user.control',
                  home / '.local/share/systemd/user'])
    return list(dict.fromkeys(roots))


def service_lines(raw: bytes) -> list[bytes]:
    """Join systemd logical lines, preserving literal path whitespace."""
    lines = []
    parts = []
    # The sentinel also flushes a continuation at EOF, as systemd does.
    for physical in [*raw.splitlines(), b'']:
        physical = physical.removeprefix(b'\xef\xbb\xbf')
        if physical.lstrip().startswith((b'#', b';')):
            continue
        trailing_slashes = len(physical) - len(physical.rstrip(b'\\'))
        if trailing_slashes % 2:
            parts.append(physical[:-1] + b' ')
            continue
        parts.append(physical)
        lines.append(b''.join(parts))
        parts.clear()
    return lines


def service_environment_files(raw: bytes) -> list[tuple[Path, bool]]:
    """Find literal systemd EnvironmentFile paths without interpreting shell syntax.

    Keep a superset across sections, repeated assignments and resets: retaining
    an overridden dependency is safer than guessing the effective drop-in order.
    """
    paths = []
    for line in service_lines(raw):
        key, separator, value = line.partition(b'=')
        if not separator or key.strip() != b'EnvironmentFile':
            continue
        name = value.strip().decode('utf-8')
        if not name:  # reset: do not discard dependencies already discovered
            continue
        optional = name.startswith('-')
        name = name.removeprefix('-')
        # EnvironmentFile takes one filename, including literal internal spaces,
        # not a shell word list. Never erase escapes/quotes before validation or
        # an optional path could silently turn into a different, missing file.
        require(name.startswith('/') and not re.search(r'''[%$*?\[\\'"\x00-\x1f]''', name),
                'dynamic or ambiguous service environment requires explicit retention inventory')
        paths.append((Path(name), optional))
    return paths


def service_literal_paths(value: str, *, atomic: bool = False) -> set[Path]:
    """Extract only literal paths; ambiguous shell/expansion syntax defers cleanup.

    Also inspect the complete scalar for path-valued settings with unquoted
    spaces. Tokenizing it alone could mistake an existing prefix for the path.
    """
    if '/' not in value and not atomic:
        return set()
    require(not re.search(r'[\\%$`*?{}\x00-\x1f]', value),
            'nonliteral service paths require explicit retention inventory')
    if atomic:
        require(value.startswith('/'), 'relative service paths require explicit retention inventory')
        return {Path(value)}
    paths = set()
    scalar = value.strip().removeprefix('-')
    if scalar.startswith('/'):
        paths.add(Path(scalar))
    for word in shlex.split(value):
        # Assignments and command options can carry a path as their value.
        if '=' in word and not word.startswith('/'):
            word = word.partition('=')[2]
        if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*://', word):
            continue
        if '/' not in word:
            continue
        name = word.lstrip('-@:+!')  # systemd executable prefixes
        require(name.startswith('/') and not re.search(r'[;:,|<>\[\]]', name),
                'ambiguous service paths require explicit retention inventory')
        paths.add(Path(name))
    return paths


def executable_search_paths(value: str) -> list[Path]:
    """Validate a literal systemd search path without using the caller's PATH."""
    require(len(value) <= 65536, 'service executable search path exceeds bound')
    names = value.strip().split(':')
    require(0 < len(names) <= 256, 'service executable search path is incomplete')
    paths = []
    for name in names:
        paths.extend(service_literal_paths(name, atomic=True))
    return paths


def systemd_executable_paths() -> list[Path]:
    # systemd.service(5): bare Exec* filenames use the installed systemd's
    # compile-time search path, which is not necessarily the updater's PATH.
    return executable_search_paths(command([
        'env', '-i', 'PATH=/usr/sbin:/usr/bin:/sbin:/bin',
        'systemd-path', 'search-binaries-default',
    ]))


def literal_service_command(executable: Path, arguments: list[str]) -> None:
    """Do not mistake an interpreter/launcher for its indirectly selected payload.

    The service manager's inherited environment and interpreter/module lookup
    are not established by scanning literal paths. Never execute the command to
    discover them. Check both the configured name and the symlink target, since
    an alias must not bypass this guard.
    """
    names = {executable.name, executable.resolve().name}
    launchers = {
        'env', 'sudo', 'doas', 'su', 'runuser', 'chroot', 'chpst', 'setpriv',
        'setsid', 'nohup', 'timeout', 'nice', 'ionice', 'stdbuf', 'taskset',
        'flock', 'xargs', 'systemd-run', 'start-stop-daemon', 'daemon', 'exec',
        'busybox', 'toybox', 'java', 'dotnet', 'deno', 'pwsh', 'powershell',
        'awk', 'gawk', 'mawk', 'nawk', 'R', 'Rscript',
    }
    shells = {'sh', 'bash', 'dash', 'ash', 'ksh', 'ksh93', 'zsh', 'fish', 'csh', 'tcsh'}
    python = any(re.fullmatch(r'(?:python|pypy)(?:\d+(?:\.\d+)*)?t?', name) for name in names)
    interpreter = python or bool(names & shells) or any(
        re.fullmatch(r'(?:node|nodejs|bun|perl|ruby|php|lua|luajit|tclsh|wish)(?:\d+(?:\.\d+)*)?', name)
        for name in names
    )
    message = 'indirect service command requires explicit retention inventory'
    require(not names & launchers, message)
    if not interpreter:
        return
    # A direct absolute script is still discovered by the literal-path scan.
    # Only recognize a small set of argument-free Python flags, including the
    # isolated wrapper_exec.py invocation generated by our installer. Inline
    # code, modules, stdin, relative scripts and unknown options defer cleanup.
    remaining = list(arguments)
    if python:
        while remaining and remaining[0] in {'-I', '-B', '-E', '-s', '-S', '-u', '-O', '-OO'}:
            remaining.pop(0)
    if remaining and remaining[0] == '--':
        remaining.pop(0)
    require(bool(remaining) and remaining[0].startswith('/'), message)


def service_path_dependencies(path: Path, candidates: list[Path]) -> set[str]:
    """Follow literal path components without losing intermediate symlink owners.

    A link inside an old release is a dependency even if its ultimate target is
    outside the release. Missing ordinary paths may be future output files;
    broken links, cycles and inaccessible components are incomplete visibility.
    """
    require(path.is_absolute(), 'service dependency path must be absolute')
    pending = deque(path.parts[1:])
    current = Path('/')
    links = 0
    found = set()
    candidate_paths = set(candidates)
    while pending:
        part = pending.popleft()
        if part == '..':
            current = current.parent
            continue
        current /= part
        # Every ancestor is visited component by component. Matching the exact
        # component retains nested candidates and intermediate symlink owners
        # without comparing every service path against every candidate.
        if current in candidate_paths:
            found.add(str(current))
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            links += 1
            require(links <= 64, 'service dependency symlink chain is cyclic or exceeds limit')
            # Validate the link itself, not a possibly not-yet-created output
            # suffix below it. Keep walking components to retain every owner.
            current.resolve(strict=True)
            target = Path(os.readlink(current))
            current = Path('/') if target.is_absolute() else current.parent
            pending.extendleft(reversed(target.parts[1:] if target.is_absolute() else target.parts))
    return found


def service_files(row: dict) -> set[Path]:
    """Enumerate the complete native search paths, including dormant units."""
    files = set()

    def walk_error(error: OSError) -> None:
        raise error

    for directory in set(service_roots(row)):
        try:
            info = directory.stat()
        except FileNotFoundError:
            require(not directory.is_symlink(), "dangling service directory requires explicit inspection")
            continue
        require(stat.S_ISDIR(info.st_mode), "service search path is not a directory")
        for parent, directories, names in os.walk(directory, onerror=walk_error):
            require(not any((Path(parent) / name).is_symlink() for name in directories),
                    "symlinked service directory requires explicit retention inventory")
            files.update(Path(parent) / name for name in names)
            require(len(files) <= 8192, "service dependency scan exceeds bound")
    if profile := row.get('linux_service'):
        from deploy.linux_service import config_files, validate
        validate(profile)
        files.update(config_files(Path(profile['supervisor_config'])))
        files.update(Path(profile[key]) for key in ('env_file', 'device_file') if Path(profile[key]).exists())
    return files


def service_dependencies(row: dict, candidates: list[Path]) -> list[str]:
    """Discover dependencies afresh; an explicit review never relaxes by default."""
    try:
        native = native_configuration_dependencies(row, candidates) if row.get('native_configuration') else []
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise ValueError('native account configuration dependencies could not be inspected') from None
    if row.get('review'):
        from deploy.retention_services import reviewed_dependencies
        return sorted(set(native + reviewed_dependencies(row, candidates)))
    return sorted(set(native + inspect_service_files(service_files(row), candidates)))


def native_configuration_dependencies(row: dict, candidates: list[Path]) -> list[str]:
    """Retain dormant account MCP runtimes even after their process has exited."""
    import tomllib
    from deploy.work_registry_snapshot import resolve_wrapper_state_dir

    home = Path(row['home'])
    if row.get('linux_service'):
        from deploy.linux_service import environment
        state = resolve_wrapper_state_dir(home, environment=environment(row['linux_service']))
    elif Path(row['service_file']).suffix == '.plist':
        state = resolve_wrapper_state_dir(home, plist=Path(row['service_file']))
    else:
        state = resolve_wrapper_state_dir(home, env_file=Path('/etc/cc-remote/wrapper.env'))
    homes = {home / '.codex'}
    report = state / 'codex-readiness.json'
    if report.exists():
        value, _ = retirement.read_json(report, owner_uid=home.stat().st_uid)
        profiles = value.get('profiles')
        require(isinstance(profiles, list) and len(profiles) <= 32
                and all(isinstance(p, dict) for p in profiles), 'invalid native account inventory')
        for profile in profiles:
            if profile.get('home'):
                path = Path(profile['home'])
                require(path.is_absolute(), 'native account home must be absolute')
                homes.add(path)
    found = set()

    def inspect_values(value) -> None:
        if isinstance(value, dict):
            for child in value.values():
                inspect_values(child)
        elif isinstance(value, list):
            for child in value:
                inspect_values(child)
        elif isinstance(value, str) and value.startswith('/'):
            found.update(service_path_dependencies(Path(value), candidates))

    for account in homes:
        path = account / 'config.toml'
        found.update(service_path_dependencies(path, candidates))
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as stream:
                info = os.fstat(stream.fileno())
                require(stat.S_ISREG(info.st_mode), 'native configuration is not a regular file')
                raw = stream.read(4 * 1024 * 1024 + 1)
        except FileNotFoundError:
            continue
        require(len(raw) <= 4 * 1024 * 1024, 'native configuration exceeds scan bound')
        inspect_values(tomllib.loads(raw.decode('utf-8')))
        found.update(str(p) for p in candidates if os.fsencode(p) in raw)
    return sorted(found)


def inspect_service_files(files: set[Path], candidates: list[Path], *,
                          reviewed_inputs: dict[Path, bytes] | None = None) -> list[str]:
    """Read literal commands without sourcing shell or running a service.

    Reviewed bytes must come from the validated, complete service snapshot.
    They exempt opaque command selection, never literal path/symlink discovery.
    """
    found = set()
    total = 0
    paths_seen = set()
    executable_commands = set()
    search_paths = set()
    timezone_names = set()
    timezone_directories = {DEFAULT_TZDIR}
    custom_namespace = False
    candidate_names = {str(p) for p in candidates}
    candidate_bytes = [(str(p), os.fsencode(p)) for p in candidates]

    def search_path(value: str) -> None:
        if reviewed_inputs is None:
            search_paths.update(executable_search_paths(value))
        else:
            for part in value.strip().split(':'):
                search_paths.update(literal_paths(part, atomic=True))
        require(len(search_paths) <= 256, 'service executable search path exceeds bound')
        for directory in search_paths:
            protect(directory)

    def protect(path: Path) -> None:
        if path not in paths_seen:
            paths_seen.add(path)
            require(len(paths_seen) <= 8192, 'service path discovery exceeds bound')
            found.update(service_path_dependencies(path, candidates))

    def literal_paths(value: str, *, atomic: bool = False) -> set[Path]:
        try:
            return service_literal_paths(value, atomic=atomic)
        except ValueError:
            if reviewed_inputs is None:
                raise
        # An opaque argument must not hide a different, literal argument (e.g.
        # ExecStart=/srv/bin/worker --option=$VALUE). Dynamic paths themselves
        # remain covered by the operator's explicit protected inputs.
        paths = set()
        if not atomic:
            try:
                words = shlex.split(value)
            except ValueError:
                words = []
            for word in words:
                try:
                    paths.update(service_literal_paths(word))
                except ValueError:
                    continue
        return paths

    def inspect_value(value: str, *, atomic: bool = False) -> None:
        for path in literal_paths(value, atomic=atomic):
            protect(path)

    def inspect_timezone(key: str, value: str) -> None:
        # Resolve named zones and POSIX rules through zoneinfo, preserving
        # file dependencies and every literal TZDIR override across services.
        if key == 'TZ':
            name = value.removeprefix(':')
            if name:
                timezone_names.update((name, 'posixrules'))
        elif value:
            for directory in literal_paths(value, atomic=True):
                timezone_directories.add(directory)
                protect(directory)

    def inspect_plist(value, key: str = '') -> None:
        if isinstance(value, dict):
            if 'Program' in value or 'ProgramArguments' in value:
                arguments = value.get('ProgramArguments', [])
                require(isinstance(arguments, list) and all(isinstance(arg, str) for arg in arguments),
                        'invalid service program arguments')
                executable = value.get('Program') or (arguments[0] if arguments else '')
                require(isinstance(executable, str) and executable.startswith('/'),
                        'nonliteral service executable requires explicit retention inventory')
                if reviewed_inputs is None:
                    literal_service_command(Path(executable), arguments[1:])
            for child_key, child in value.items():
                if key == 'EnvironmentVariables' and child_key in {'TZ', 'TZDIR'}:
                    require(isinstance(child, str), 'invalid service timezone environment')
                    inspect_timezone(child_key, child)
                    continue
                inspect_plist(child, child_key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                inspect_plist(child, 'Program' if key == 'ProgramArguments' and index == 0 else '')
        elif isinstance(value, str):
            if key == 'PATH':
                search_path(value)
                return
            inspect_value(value, atomic=value.startswith('/') or key in {
                'Program', 'WorkingDirectory', 'RootDirectory', 'StandardOutPath', 'StandardErrorPath',
            })

    def inspect(path: Path) -> bytes:
        nonlocal total, custom_namespace
        if reviewed_inputs is None:
            protect(path)
            target = path.resolve(strict=True)
            if str(target) == "/dev/null":  # masked systemd unit
                return b""
            info = path.stat()
            total += info.st_size
            require(stat.S_ISREG(info.st_mode) and info.st_size <= 4 * 1024 * 1024
                    and total <= 32 * 1024 * 1024, "service dependency scan incomplete or oversized")
            raw = path.read_bytes()
        else:
            # The snapshot has already fingerprinted these bounded inputs and
            # retained all aliases, including reviewed missing/masked units.
            target = path.resolve()
            raw = reviewed_inputs[path]
            if not raw:
                return b""
        if path.suffix == ".plist":
            value = plistlib.loads(raw)
            inspect_plist(value)
            raw = json.dumps(value, default=str).encode()
        else:
            for line in service_lines(raw):
                key, separator, value = line.partition(b'=')
                key = key.strip()
                text = value.strip().decode('utf-8')
                if key.strip() == b'EnvironmentFile':
                    continue  # exact single-filename handling below
                if key in {b'StandardOutput', b'StandardError', b'StandardInput'}:
                    destination = text.partition(':')
                    if destination[0] in {'append', 'file', 'truncate'} and destination[1]:
                        inspect_value(destination[2], atomic=True)
                        continue
                if key in {b'RootDirectory', b'RootImage', b'BindPaths', b'BindReadOnlyPaths',
                           b'TemporaryFileSystem'} and text:
                    custom_namespace = True
                if key == b'PassEnvironment':
                    inherited = set(shlex.split(text))
                    if 'PATH' in inherited:
                        custom_namespace = True
                    require(reviewed_inputs is not None or inherited.isdisjoint({'TZ', 'TZDIR'}),
                            'inherited service timezone requires explicit retention inventory')
                if key == b'ExecSearchPath':
                    if text:  # keep a superset across resets and drop-ins
                        search_path(text)
                    continue
                if key == b'PATH':  # external EnvironmentFile
                    words = shlex.split(text)
                    require(reviewed_inputs is not None or len(words) == 1,
                            'ambiguous service PATH requires explicit retention inventory')
                    for word in words:
                        search_path(word)
                    continue
                if key in {b'TZ', b'TZDIR'}:
                    words = shlex.split(text)
                    require(reviewed_inputs is not None or len(words) <= 1,
                            'ambiguous service timezone requires explicit retention inventory')
                    for word in words:
                        inspect_timezone(key.decode(), word)
                    continue
                if key == b'Environment':
                    for assignment in shlex.split(text):
                        if assignment.startswith('PATH='):
                            search_path(assignment.removeprefix('PATH='))
                        elif assignment.startswith(('TZ=', 'TZDIR=')):
                            name, _, timezone = assignment.partition('=')
                            inspect_timezone(name, timezone)
                        else:
                            inspect_value(assignment)
                    continue
                if key in {b'ExecCondition', b'ExecStartPre', b'ExecStart', b'ExecStartPost',
                           b'ExecReload', b'ExecStop', b'ExecStopPost', b'command'} and text:
                    words = shlex.split(text)
                    require(bool(words), 'service executable is missing')
                    executable = words[0].lstrip('-@:+!')
                    prefixes = words[0][:len(words[0]) - len(executable)]
                    arguments = words[2:] if key != b'command' and '@' in prefixes else words[1:]
                    if not executable.startswith('/'):
                        literal = (key != b'command' and re.fullmatch(r'[A-Za-z0-9_+.-]+', executable)
                                   and executable not in {'.', '..'} and '\\' not in text)
                        require(reviewed_inputs is not None or literal,
                                'nonliteral service executable requires explicit retention inventory')
                        if literal:
                            executable_commands.add((executable, tuple(arguments)))
                    elif reviewed_inputs is None:
                        literal_service_command(Path(executable), arguments)
                inspect_value((value if separator else line).decode('utf-8'))
        found.update(name for name, marker in candidate_bytes if marker in raw)
        found.update(candidate_names.intersection(map(str, [target, *target.parents])))
        return raw

    for path in sorted(files):
        try:
            raw = inspect(path)
            for env, optional in service_environment_files(raw) if reviewed_inputs is None else []:
                if optional and not env.exists() and not env.is_symlink():
                    continue
                inspect(env)
        except OSError as exc:
            exc.add_note(f"While inspecting service definition {path}")
            raise
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from exc
    for name in timezone_names:
        for directory in timezone_directories:
            inspect_value(str(directory / name), atomic=True)
    if executable_commands:
        require(reviewed_inputs is not None or not custom_namespace,
                'custom service executable namespace requires explicit retention inventory')
        # Include all literal overrides from definitions, drop-ins and external
        # environment files. Keeping a superset avoids guessing unit precedence
        # or access permissions for dormant service users. Recomputed on every
        # cleanup revalidation, including aliases in an earlier search directory.
        search_paths.update(systemd_executable_paths())
        require(len(search_paths) <= 256, 'service executable search path exceeds bound')
        for name, arguments in executable_commands:
            resolved = False
            for directory in search_paths:
                executable = directory / name
                protect(executable)
                try:
                    info = executable.stat()
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(info.st_mode) and info.st_mode & 0o111:
                    if reviewed_inputs is None:
                        literal_service_command(executable, list(arguments))
                    resolved = True
            require(reviewed_inputs is not None or resolved,
                    'unresolved service executable requires explicit retention inventory')
    return sorted(found)


def inventory(root: Path, data: dict, row: dict) -> dict | None:
    require(all(r["phase"] != "prepared" for r in data["generations"]),
            "unfinished activation; cleanup deferred")
    if not row["previous"]:
        return None
    previous = retirement.absolute_path(row["previous"])
    require(previous.parent == root / "releases",
            "legacy rollback is outside the managed root; retain it for manual migration acceptance")
    rollback = [str(previous), row["backup"]]
    if row.get("snapshot"):
        rollback.append(row["snapshot"])
    candidates, protected = set(), set()
    for record in data["generations"]:
        paths = [record["release"], record["backup"]]
        if record["previous"] and Path(record["previous"]).parent == root / "releases":
            paths.append(record["previous"])
        if record.get("snapshot"):
            paths.append(record["snapshot"])
        if record["phase"] == "aborted" and not record.get('rollback_verified_at'):
            protected.update(p for p in paths if Path(p).exists())
        elif record["id"] != row["id"]:
            candidates.update(paths)
    candidates -= {row["release"], *rollback}
    candidates = {p for p in candidates if Path(p).exists()}
    for candidate in candidates:
        identities = [r["artifacts"][candidate] for r in data["generations"]
                      if candidate in r.get("artifacts", {})]
        require(bool(identities) and all(i == list(retirement.identity(Path(candidate))) for i in identities),
                "recorded artifact was replaced; cleanup requires inspection")
    if not candidates:
        return None
    service_context = {key: row[key] for key in ("home", "linux_service") if key in row}
    service_context.update(service_file=row['service_file'], native_configuration=row['role'] == 'wrapper')
    review = root / '.retention-services.json'
    if review.exists() or review.is_symlink():
        service_context['review'] = str(review)
    protected.update(service_dependencies(service_context, [Path(p) for p in candidates]))
    # Independent services may intentionally keep their own current pointer,
    # even while stopped. Preserve those targets and their runtime closure.
    protected.update(map(str, retirement.runtime_link_dependencies(root, [Path(p) for p in candidates])))
    python = str(Path(row["release"]) / ".venv/bin/python")
    check = [python, "-B", str(Path(row["release"]) / "deploy/release_retention.py"),
             "verify", "--root", str(root), "--generation", row["id"]]
    return {"schema": 1, "installation_root": str(root), "current_release": row["release"],
            "rollback_paths": rollback, "protected_paths": sorted(protected),
            "service_context": service_context,
            "cleanup_roots": [str(root / name) for name in ("releases", BACKUPS, "rollback-data")
                              if (root / name).is_dir()], "candidates": sorted(candidates),
            "transactions": [{"path": str(root / LEDGER), "field": "phase", "equals": "committed"}],
            "checks": [{"name": "live release, service, public health and native config",
                        "argv": check, "cwd": row["home"]}]}


def available_bytes(root: Path) -> int:
    filesystem = os.statvfs(root)
    return filesystem.f_bavail * filesystem.f_frsize


def record_retention(root: Path, data: dict, *, status: str, report: dict | None = None,
                     before_available: int | None = None, reason: str | None = None) -> dict:
    known = {p for row in data['generations'] for p in (row['release'], row['previous']) if p}
    untracked = sorted(str(p) for p in (root / 'releases').iterdir() if p.is_dir() and str(p) not in known)
    after = available_bytes(root)
    result = {'schema': 1, 'checked_at': time.time(), 'status': status,
              'current_release': str((root / 'current').resolve()),
              'removed_count': sum(r['state'] == 'removed' for r in (report or {}).get('candidates', [])),
              'removed_allocated_bytes': (report or {}).get('removed_allocated_bytes', 0),
              'available_bytes_after': after,
              'available_change_bytes': after - before_available if before_available is not None else None,
              'retained_dependencies': [r for r in (report or {}).get('candidates', []) if r['state'] == 'deferred'],
              'untracked_release_count': len(untracked), 'untracked_releases': untracked[:1024]}
    if reason:
        result['reason'] = reason
    retirement.write_journal(root / STATUS, result)
    return result


def prune(root: Path, descriptor: int) -> dict | None:
    data = read_ledger(root)
    before_available = available_bytes(root)
    current = str((root / "current").resolve(strict=True))
    rows = [r for r in data["generations"] if r["release"] == current and r["phase"] == "committed"]
    require(len(rows) == 1, "no unique committed generation; legacy artifacts remain untouched")
    row = rows[0]
    # Reboots and normal service restarts may legitimately replace the accepted
    # process. Rebind only after fresh native/config/public checks prove this
    # exact release again; the retirement checks below freeze that new identity.
    fresh_row = {k: v for k, v in row.items() if k != "service_identity"}
    before = service_identity(row)
    acceptance(root, fresh_row)
    require(service_identity(row) == before, "service changed during retention preflight")
    row["service_identity"] = before
    retirement.write_journal(root / LEDGER, data)
    plan = inventory(root, data, row)
    if plan is None:
        record_retention(root, data, status='complete', before_available=before_available)
        print("Retention: current release and previous rollback retained; no proven older artifacts to remove.")
        return None
    path = root / INVENTORY
    retirement.write_journal(path, plan)
    preview = retirement.cleanup(path, lock_descriptor=descriptor, owner_uid=root.stat().st_uid)
    print(f"Retention: checked {len(preview['candidates'])} recorded artifacts; live dependencies stay protected.")
    result = retirement.cleanup(path, apply=True, lock_descriptor=descriptor, owner_uid=root.stat().st_uid)
    # Keep provenance for every surviving artifact. Completed records whose
    # payloads have all gone no longer need to accumulate indefinitely.
    compact_generations(data)
    retirement.write_journal(root / LEDGER, data)
    summary = record_retention(root, data, status=result['status'], report=result, before_available=before_available)
    print(f"Retention: removed {result['removed_allocated_bytes']} allocated bytes; "
          f"{sum(r['state'] == 'deferred' for r in result['candidates'])} artifacts retained for dependencies.")
    if summary['untracked_release_count']:
        print(f"Retention: {summary['untracked_release_count']} untracked release directories still require inspection.")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("begin", "commit", "abort", "prune", "verify"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--generation")
    parser.add_argument("--release", type=Path)
    parser.add_argument("--previous", default="")
    parser.add_argument("--role", choices=("wrapper", "relay"))
    parser.add_argument("--home", type=Path)
    parser.add_argument("--service")
    parser.add_argument("--service-file", type=Path)
    parser.add_argument("--config", type=Path, action="append", default=[])
    parser.add_argument("--snapshot")
    parser.add_argument('--rollback-verified', action='store_true')
    parser.add_argument("--linux-service", type=Path)
    args = parser.parse_args(argv)
    descriptor = None
    try:
        root = retirement.absolute_path(str(args.root))
        if args.action == "verify":
            _, row = generation(root, args.generation)
            acceptance(root, row)
            return 0
        inherited = os.environ.get(LOCK_FD_ENV)
        if inherited:
            verify_install_lock(root, int(inherited))
            descriptor = os.dup(int(inherited))
        else:
            descriptor = acquire_install_lock(root)
        if args.action == "begin":
            require(all((args.release, args.role, args.home, args.service, args.service_file)),
                    "begin requires release, role, home and service binding")
            print(begin(root, args.release, args.previous, role=args.role, home=args.home,
                        service=args.service, service_file=args.service_file, configs=args.config,
                        linux_service=json.loads(args.linux_service.read_text()) if args.linux_service else None))
        elif args.action == "commit":
            commit(root, args.generation, args.snapshot)
        elif args.action == "abort":
            abort(root, args.generation, rollback_verified=args.rollback_verified)
        else:
            prune(root, descriptor)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if descriptor is not None and args.action == 'prune':
            try:
                record_retention(root, read_ledger(root), status='deferred',
                                 reason='; '.join([str(exc), *getattr(exc, '__notes__', [])]))
            except (OSError, ValueError):
                pass  # Preserve the original failure; no cleanup was authorized by this report.
        print(f"Retention deferred: {exc}", file=sys.stderr)
        return 1
    finally:
        if descriptor is not None:
            os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
