"""Submit one Relay update with a durable, locked server-side launch receipt.

The client sends this stdlib-only program to the verified Relay's Python with
``-I -c``. It must also work when that Relay predates this helper. Receipt files
are outside releases and are deliberately retained across upgrades/reboots.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


RECOVERY_PROGRAM = '''"""OS-owned recovery of exactly one Relay activation."""
import json, os, stat, sys, time
from pathlib import Path
root = Path('/opt/cc-remote')
identity = sys.argv[1]
path = root / 'activation-jobs' / (identity + '.json')
if not path.exists():
    raise SystemExit(0)
info = path.lstat()
if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
    raise ValueError('unsafe activation receipt')
value = json.loads(path.read_text())
release = Path(value['release'])
if value.get('root') != str(root) or value.get('id') != identity or release.parent != root / 'releases' or release.is_symlink():
    raise ValueError('invalid recovery identity')
for item in (release, release / 'deploy', release / 'deploy/activation.py'):
    info = item.lstat()
    if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
        raise ValueError('unsafe recovery program')
sys.path.insert(0, str(release))
from cc_remote import activation
from deploy.install_lock import acquire_install_lock, LOCK_FD_ENV, InstallLockError
from deploy.activation import recover
deadline = time.monotonic() + 1700
while activation.install_locked(root):
    if time.monotonic() >= deadline:
        raise TimeoutError('original installer is still running')
    time.sleep(2)
try:
    descriptor = acquire_install_lock(root)
except InstallLockError:
    raise SystemExit(0)  # Another installer acquired the shared recovery gate.
os.environ[LOCK_FD_ENV] = str(descriptor)
try:
    if activation.receipt(root, identity)['phase'] not in {'unchanged', 'rolled_back'}:
        recover(root, identity)
finally:
    os.close(descriptor)
'''


def _activation_record(root: Path, identity: str) -> dict | None:
    try:
        fd = os.open(root / "activation-jobs" / f"{identity}.json", os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError("unsafe activation receipt")
        value = json.loads(stream.read(65537))
    if value.get("schema") != 1 or value.get("id") != identity or value.get("root") != str(root):
        raise ValueError("invalid activation receipt")
    return value


def _server_health(release: Path, origin: str) -> bool:
    """Stdlib bootstrap also verifies Relays predating activation receipts."""
    try:
        expected = json.loads((release / "release-manifest.json").read_text())
        expected_web = json.loads((release / "web/dist/cc-remote-build.json").read_text())
        observed = []
        for path in ("/healthz", "/cc-remote-build.json"):
            with urlopen(Request(origin + path, headers={"Cache-Control": "no-cache"}), timeout=5) as response:
                observed.append(json.loads(response.read(65537)))
        result = subprocess.run(["systemctl", "show", "cc-remote-relay", "--property=MainPID", "--value"],
                                capture_output=True, text=True, timeout=10, check=False)
        pid = result.stdout.strip()
        return (observed[0].get("ok") is True and observed[0].get("version") == expected["product_version"]
                and observed[0].get("protocol") == expected["protocol_version"] and observed[1] == expected_web
                and result.returncode == 0 and pid.isdecimal() and int(pid) > 0
                and Path(f"/proc/{pid}/cwd").resolve(strict=True) == release)
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
        return False


def inspect_original(root: Path, request: dict, prior: dict | None, state: dict, receipt: Path) -> dict:
    """Called under the original launch lock; it never submits another update."""
    if prior is None:
        return {"status": "missing" if state.get("LoadState") == "not-found" else "unavailable"}
    if prior.get("outcome") in {"succeeded", "failed_safe"}:
        return {"status": prior["outcome"]}
    if state.get("ActiveState") in {"activating", "deactivating"} or state.get("SubState") == "running":
        return {"status": "running"}
    # The helper is sent to old installations too. Import recovery code only
    # from a managed release, and only once the original OS job has stopped.
    try:
        descriptor = os.open(root / ".update.lock", os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        descriptor = None
    if descriptor is not None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "running"}
        finally:
            os.close(descriptor)
    identity = request["unit"].removeprefix("cc-remote-update-")
    record = _activation_record(root, identity)
    current = (root / "current").resolve(strict=True)
    if _server_health(current, record.get("origin") if record else prior.get("origin")):
        manifest = json.loads((current / "release-manifest.json").read_text())
        ledger_file = root / ".release-generations.json"
        pending = any(row["phase"] == "prepared" for row in json.loads(ledger_file.read_text())["generations"]) if ledger_file.exists() else False
        if ((record and record["phase"] == "succeeded" and str(current) == record["release"])
                or not record and manifest["product_version"] == request["version"]):
            outcome = "succeeded"
        elif ((record and record["phase"] in {"unchanged", "rolled_back"} and str(current) == record["previous"])
              or not record and str(current) == prior.get("previous") and not pending):
            outcome = "failed_safe"
        else:
            outcome = None
        if outcome:
            _write(receipt, {**prior, "outcome": outcome})
            return {"status": outcome}
    if not record:
        return {"status": "unavailable"}
    target = Path(record["release"])
    if target.parent != root / "releases" or target.is_symlink():
        raise ValueError("invalid original recovery release")
    sys.path.insert(0, str(target))
    from deploy.linux_service import trusted
    trusted(target)
    trusted((target / ".venv/bin/python").resolve(strict=True))
    attempts = prior.get("recovery_attempts", 0)
    if time.time() - prior.get("recovery_started_at", 0) < 120:
        return {"status": "recovering"}
    if attempts >= 3:
        return {"status": "unavailable"}
    recovery_unit = request["unit"] + f"-recovery-{attempts + 1}"
    _write(receipt, {**prior, "recovery_attempts": attempts + 1, "recovery_started_at": time.time()})
    result = subprocess.run([
        "systemd-run", "--unit", recovery_unit, "--no-block", "--collect", "--property=Type=oneshot",
        "--property=KillMode=process", "--", str(target / ".venv/bin/python"), "-I", "-B",
        str(target / "deploy/activation.py"), "--root", str(root), "recover", "--id", identity,
    ], capture_output=True, text=True, timeout=20, check=False)
    return {"status": "recovering" if result.returncode == 0 else "unavailable"}


def _private_file(path: Path, flags: int) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError("unsafe Relay update receipt permissions")
    return fd


def _write(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _receipt_lock(folder: Path, unit: str):
    folder.mkdir(mode=0o700, exist_ok=True)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("unsafe Relay update receipt directory")
    fd = _private_file(folder / f"{unit}.lock", os.O_CREAT | os.O_RDWR)
    try:
        # A delayed first SSH invocation and a retry must never both submit.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def submit(root: Path, request: dict) -> dict:
    if not isinstance(request, dict):
        raise ValueError("invalid Relay update request")
    unit = request.get("unit")
    version = request.get("version")
    if (not isinstance(unit, str) or not re.fullmatch(r"cc-remote-update-[a-f0-9]{32}", unit)
            or not isinstance(version, str) or len(version) > 64
            or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version)
            or type(request.get("protocol")) is not int or request["protocol"] < 1
            or type(request.get("allow_protocol_change")) is not bool
            or not isinstance(request.get("domain"), str)):
        raise ValueError("invalid Relay update request")
    identity = {key: request[key] for key in (
        "unit", "version", "protocol", "allow_protocol_change", "domain",
    )}
    operation = request.get("operation", "submit")
    if operation not in {"submit", "inspect"}:
        raise ValueError("invalid Relay operation")
    origin = request.get("origin", "https://" + identity["domain"])
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != identity["domain"] or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("invalid Relay origin")
    metadata = json.loads((root / "installation.json").read_text())
    if metadata.get("role") != "relay" or metadata.get("domain") != identity["domain"]:
        raise ValueError("SSH host does not manage this Relay domain")
    folder = root / "update-jobs"
    with _receipt_lock(folder, unit):
        receipt = folder / f"{unit}.json"
        prior = None
        try:
            fd = _private_file(receipt, os.O_RDONLY)
        except FileNotFoundError:
            pass
        else:
            with os.fdopen(fd) as stream:
                prior = json.loads(stream.read(16385))
            if not isinstance(prior, dict) or prior.get("request") != identity:
                raise ValueError("Relay update receipt does not match the request")
        result = subprocess.run(
            ["systemctl", "show", f"{unit}.service",
             "--property=LoadState,ActiveState,SubState"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode:
            return {"status": "unavailable"}
        state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if operation == "inspect":
            return inspect_original(root, identity, prior, state, receipt)
        if prior and prior.get("outcome") in {"succeeded", "failed_safe"}:
            return {"status": prior["outcome"]}
        if state.get("LoadState") == "loaded":
            if prior is None:
                _write(receipt, {"schema": 1, "request": identity, "phase": "observed"})
            return {"status": "existing"}
        if state != {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead"}:
            return {"status": "unavailable"}
        if prior is not None:
            # A receipt without a unit can follow a reboot, unit removal, or a
            # lost systemd acknowledgement. Absence is not proof of nonexecution.
            return {"status": "uncertain"}
        # Persist intent before talking to systemd, while holding the same lock
        # used by every retry. Never delete this receipt to make a retry succeed.
        record = {"schema": 1, "request": identity, "phase": "submitting",
                  "previous": str((root / "current").resolve()),
                  "origin": origin}
        _write(receipt, record)
        recovery = folder / f"{unit}-recover.py"
        fd = _private_file(recovery, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as stream:
            stream.write(RECOVERY_PROGRAM)
            stream.flush()
            os.fsync(stream.fileno())
        command = ["systemd-run", "--unit", unit, "--no-block",
                   "--property=Type=oneshot", "--property=RemainAfterExit=yes", "--property=KillMode=process",
                   "--property=TimeoutStopSec=30min",
                   f"--property=ExecStopPost={root}/current/.venv/bin/python -I -B {recovery} {unit.removeprefix('cc-remote-update-')}",
                   f"--setenv=CC_REMOTE_ACTIVATION_ID={unit.removeprefix('cc-remote-update-')}", "--",
                   "/usr/local/bin/cc-remote", "update", "--role", "relay", "--version", version]
        if identity["allow_protocol_change"]:
            command.append("--allow-protocol-change")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return {"status": "uncertain"}
        if result.returncode:
            _write(receipt, {**record, "phase": "launch-error", "exit_code": result.returncode})
            return {"status": "launch-error", "exit_code": result.returncode}
        _write(receipt, {**record, "phase": "submitted"})
        return {"status": "submitted"}


def main() -> int:
    try:
        if os.geteuid() != 0:
            raise ValueError("Relay update launch requires root")
        request = json.loads(sys.argv[1])
        result = submit(Path("/opt/cc-remote"), request)
    except BlockingIOError:
        result = {"status": "busy"}
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.TimeoutExpired):
        # Never expose remote config values, subprocess output or credentials.
        result = {"status": "unavailable"}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
