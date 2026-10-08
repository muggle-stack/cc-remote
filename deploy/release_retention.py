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


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def read_ledger(root: Path) -> dict:
    if not (root / LEDGER).exists():
        return {"schema": 1, "phase": "committed", "generations": []}
    data, _ = retirement.read_json(root / LEDGER)
    require(data.get("schema") == 1 and isinstance(data.get("generations"), list),
            "invalid release generation ledger")
    require(len(data["generations"]) <= 100, "generation ledger exceeds limit; inspect retained files")
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
        raw = command(["launchctl", "print", f"gui/{os.getuid()}/{row['service']}"])
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
    metadata, _ = retirement.read_json(root / "installation.json")
    python = str(release / ".venv/bin/python")
    if row["role"] == "relay":
        origin = dotenv_values(root / ".env").get("PUBLIC_ORIGIN")
        require(isinstance(origin, str) and bool(origin), "Relay public origin missing")
        expected_version = manifest["product_version"]
    else:
        installation = Installation(root, release, manifest, metadata)
        origin = relay_origin(installation)
        expected_version = None  # A newer compatible Relay is allowed.
        home = Path(row["home"])
        if sys.platform == "darwin":
            environment = plistlib.loads(Path(row["service_file"]).read_bytes()).get("EnvironmentVariables", {})
            device = Path(environment.get("CC_REMOTE_DEVICE_CONFIG", str(home / ".cc-remote/device.json")))
            config = json.loads(device.read_text()) if device.exists() else {}
            machine = environment.get("CC_REMOTE_MACHINE_ID") or config.get("machine_id") or "default"
            selector = ["--plist", row["service_file"]]
            prefix = []
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


def abort(root: Path, identity: str) -> None:
    data, row = generation(root, identity)
    require(row["phase"] == "prepared", "cannot abort a committed generation")
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


def service_roots(row: dict) -> list[Path]:
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
            roots.extend(unit_paths([
                'runuser', '-u', account.pw_name, '--', *environment,
                f'DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus',
                'systemctl', '--user', 'show', '-p', 'UnitPath', '--value',
            ]))
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
    while pending:
        part = pending.popleft()
        if part == '..':
            current = current.parent
            continue
        current /= part
        found.update(str(candidate) for candidate in candidates if current.is_relative_to(candidate))
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


def service_dependencies(row: dict, candidates: list[Path]) -> list[str]:
    """Read dormant service definitions and their literal environment files.

    Do not source shell or run arbitrary services. Incomplete enumeration,
    unreadable files or unresolved environment paths defer cleanup.
    """
    found = set()
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
    total = 0
    paths_seen = set()

    def protect(path: Path) -> None:
        if path not in paths_seen:
            paths_seen.add(path)
            require(len(paths_seen) <= 8192, 'service path discovery exceeds bound')
            found.update(service_path_dependencies(path, candidates))

    def inspect_value(value: str, *, atomic: bool = False) -> None:
        for path in service_literal_paths(value, atomic=atomic):
            protect(path)

    def inspect_plist(value, key: str = '') -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                inspect_plist(child, child_key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                inspect_plist(child, 'Program' if key == 'ProgramArguments' and index == 0 else '')
        elif isinstance(value, str):
            inspect_value(value, atomic=key in {
                'Program', 'WorkingDirectory', 'RootDirectory', 'StandardOutPath', 'StandardErrorPath',
            })

    def inspect(path: Path) -> bytes:
        nonlocal total
        protect(path)
        target = path.resolve(strict=True)
        if str(target) == "/dev/null":  # masked systemd unit
            return b""
        info = path.stat()
        total += info.st_size
        require(stat.S_ISREG(info.st_mode) and info.st_size <= 4 * 1024 * 1024
                and total <= 32 * 1024 * 1024, "service dependency scan incomplete or oversized")
        raw = path.read_bytes()
        if path.suffix == ".plist":
            value = plistlib.loads(raw)
            inspect_plist(value)
            raw = json.dumps(value, default=str).encode()
        else:
            for line in service_lines(raw):
                key, separator, value = line.partition(b'=')
                if key.strip() == b'EnvironmentFile':
                    continue  # exact single-filename handling below
                if (key.strip().startswith(b'Exec') or key.strip() == b'command') and value.strip():
                    words = shlex.split(value.decode('utf-8'))
                    require(bool(words) and words[0].lstrip('-@:+!').startswith('/'),
                            'nonliteral service executable requires explicit retention inventory')
                inspect_value((value if separator else line).decode('utf-8'))
        for candidate in candidates:
            if str(candidate).encode() in raw or target.is_relative_to(candidate):
                found.add(str(candidate))
        return raw

    for path in files:
        raw = inspect(path)
        for env, optional in service_environment_files(raw):
            if optional and not env.exists() and not env.is_symlink():
                continue
            inspect(env)
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
        if record["phase"] == "aborted":
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
    protected.update(service_dependencies(row, [Path(p) for p in candidates]))
    # Independent services may intentionally keep their own current pointer,
    # even while stopped. Preserve those targets and their runtime closure.
    for path in root.iterdir():
        if path.is_symlink():
            protected.add(str(path.resolve(strict=True)))
    python = str(Path(row["release"]) / ".venv/bin/python")
    check = [python, "-B", str(Path(row["release"]) / "deploy/release_retention.py"),
             "verify", "--root", str(root), "--generation", row["id"]]
    service_context = {key: row[key] for key in ("home", "linux_service") if key in row}
    return {"schema": 1, "installation_root": str(root), "current_release": row["release"],
            "rollback_paths": rollback, "protected_paths": sorted(protected),
            "service_context": service_context,
            "cleanup_roots": [str(root / name) for name in ("releases", BACKUPS, "rollback-data")
                              if (root / name).is_dir()], "candidates": sorted(candidates),
            "transactions": [{"path": str(root / LEDGER), "field": "phase", "equals": "committed"}],
            "checks": [{"name": "live release, service, public health and native config",
                        "argv": check, "cwd": row["home"]}]}


def prune(root: Path, descriptor: int) -> dict | None:
    data = read_ledger(root)
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
        print("Retention: current release and previous rollback retained; no proven older artifacts to remove.")
        return None
    path = root / INVENTORY
    retirement.write_journal(path, plan)
    preview = retirement.cleanup(path, lock_descriptor=descriptor)
    print(f"Retention: checked {len(preview['candidates'])} recorded artifacts; live dependencies stay protected.")
    result = retirement.cleanup(path, apply=True, lock_descriptor=descriptor)
    # Keep provenance for every surviving artifact. Completed records whose
    # payloads have all gone no longer need to accumulate indefinitely.
    data["generations"] = [r for r in data["generations"] if r["id"] == row["id"] or any(
        p and Path(p).exists() for p in (r["release"], r["previous"], r["backup"], r.get("snapshot")))]
    retirement.write_journal(root / LEDGER, data)
    print(f"Retention: removed {result['removed_allocated_bytes']} allocated bytes; "
          f"{sum(r['state'] == 'deferred' for r in result['candidates'])} artifacts retained for dependencies.")
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
            abort(root, args.generation)
        else:
            prune(root, descriptor)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Retention deferred: {exc}", file=sys.stderr)
        return 1
    finally:
        if descriptor is not None:
            os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
