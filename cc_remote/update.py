"""Update managed Release installations through the existing role installers.

No model imports, credentials, remote shell access, or service restarts belong
here. Activation and rollback remain owned by deploy/install-{role}.sh.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from deploy.release_manifest import ReleaseManifestError, load_manifest
from deploy.install_lock import LOCK_FD_ENV, InstallLockError, acquire_install_lock


class UpdateError(ValueError):
    pass


_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
_MAX_ARCHIVE = 256 * 1024 * 1024
_MAX_EXPANDED = 1024 * 1024 * 1024


def version_tuple(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or len(value) > 64 or not _VERSION.fullmatch(value):
        raise UpdateError("version must be an exact stable version, such as 4.0.1")
    return tuple(map(int, value.split(".")))


def host_platform() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    machine = {"aarch64": "arm64", "amd64": "x86_64"}.get(machine, machine)
    if system not in {"linux", "darwin"} or machine not in {"x86_64", "arm64"}:
        raise UpdateError("published updates support macOS/Linux on x86_64 or arm64")
    return system, machine


@dataclass(frozen=True)
class Installation:
    root: Path
    release: Path
    manifest: dict
    metadata: dict

    @property
    def role(self) -> str:
        return self.manifest["role"]


def installation_roots(system: str) -> dict[str, Path]:
    if system == "darwin":
        return {"wrapper": Path.home() / "Library/Application Support/cc-remote"}
    return {"relay": Path("/opt/cc-remote"), "wrapper": Path("/opt/cc-remote-wrapper")}


def read_installation(root: Path, system: str, machine: str) -> Installation:
    metadata_path = root / "installation.json"
    current = root / "current"
    if metadata_path.is_symlink() or not current.is_symlink():
        raise UpdateError(f"not a managed Release installation: {root}")
    try:
        metadata = json.loads(metadata_path.read_text())
        release = current.resolve(strict=True)
        manifest = load_manifest(release / "release-manifest.json")
    except (OSError, ValueError, TypeError) as exc:
        raise UpdateError(f"cannot read managed installation at {root}") from exc
    if release.parent != (root / "releases").resolve():
        raise UpdateError(f"current points outside the managed releases directory: {root}")
    if (
        not isinstance(metadata, dict) or type(metadata.get("schema")) is not int
        or metadata["schema"] != 1
        or metadata.get("role") != manifest["role"]
        or (manifest["os"], manifest["arch"]) != (system, machine)
    ):
        raise UpdateError(f"installation metadata does not match this host: {root}")
    version_tuple(manifest["product_version"])
    if type(manifest["protocol_version"]) is not int or manifest["protocol_version"] < 1:
        raise UpdateError("installation has an invalid protocol version")
    if manifest["role"] == "wrapper":
        user = metadata.get("user")
        if not isinstance(user, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", user) or user == "root":
            raise UpdateError("wrapper installation has no valid original service user")
    elif not isinstance(metadata.get("domain"), str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9.-]*\.[a-z0-9.-]+", metadata["domain"]
    ):
        raise UpdateError("relay installation has no valid original domain")
    return Installation(root, release, manifest, metadata)


def select_installation(role: str | None, system: str, machine: str) -> Installation:
    found = []
    for expected_role, root in installation_roots(system).items():
        # Standard roots have fixed roles. Do not read unrelated installations
        # when an operator explicitly selects one role for an update.
        if role is not None and expected_role != role:
            continue
        if not (root / "installation.json").exists():
            continue
        installation = read_installation(root, system, machine)
        if installation.role != expected_role:
            raise UpdateError(f"installation role does not match its managed directory: {root}")
        found.append(installation)
    if not found:
        raise UpdateError(
            "no managed Release installation found; install a release containing this "
            "command first. Source, Docker and custom deployments keep their own upgrade procedure"
        )
    if len(found) != 1:
        raise UpdateError("both roles are installed; select --role relay or --role wrapper")
    return found[0]


def release_repository() -> str:
    value = os.environ.get("CC_REMOTE_GITHUB_REPOSITORY", "muggle-stack/cc-remote")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise UpdateError("CC_REMOTE_GITHUB_REPOSITORY must be owner/repository")
    return value


def latest_version(repository: str) -> str:
    request = Request(
        f"https://api.github.com/repos/{repository}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "cc-remote-updater"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = response.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise ValueError("oversized response")
        data = json.loads(payload)
        tag = data["tag_name"]
        if data.get("draft") is not False or data.get("prerelease") is not False or not tag.startswith("v"):
            raise ValueError("not a stable release")
        version = tag[1:]
        version_tuple(version)
        return version
    except HTTPError as exc:
        raise UpdateError(f"GitHub release lookup failed (HTTP {exc.code}); try later or use --version") from exc
    except (URLError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise UpdateError("cannot determine the latest stable release; try later or use --version") from exc


def release_base(repository: str, version: str) -> str:
    value = os.environ.get(
        "CC_REMOTE_RELEASE_BASE_URL",
        f"https://github.com/{repository}/releases/download/v{version}",
    ).rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"https", "file"} or parsed.username or parsed.password
        or parsed.query or parsed.fragment
        or (parsed.scheme == "https" and not parsed.hostname)
        or (parsed.scheme == "file" and (parsed.netloc or not parsed.path.startswith("/")))
    ):
        raise UpdateError("release base must be an HTTPS URL or an absolute local file:// directory")
    return value


def _download(url: str, destination: Path, limit: int) -> None:
    result = subprocess.run(
        ["curl", "--fail", "--location", "--proto", "=https,file", "--proto-redir", "=https",
         "--tlsv1.2", "--connect-timeout", "15", "--max-time", "300",
         "--retry", "2", "--retry-max-time", "60", "--max-filesize", str(limit),
         "--silent", "--show-error", "--output", str(destination), url],
        check=False,
    )
    if result.returncode or not destination.is_file() or destination.stat().st_size > limit:
        raise UpdateError(f"download failed for {destination.name}; no service was changed")


def download_bundle(installation: Installation, version: str, base: str, stage: Path) -> Path:
    manifest = installation.manifest
    name = f"cc-remote-{installation.role}-v{version}-{manifest['os']}-{manifest['arch']}.tar.gz"
    checksum = stage / "SHA256SUMS"
    archive_path = stage / name
    _download(f"{base}/SHA256SUMS", checksum, 1024 * 1024)
    matches = []
    try:
        lines = checksum.read_text().splitlines()
    except UnicodeError as exc:
        raise UpdateError("release checksum file is invalid") from exc
    for line in lines:
        fields = line.split()
        if len(fields) == 2 and fields[1].lstrip("*").removeprefix("./") == name:
            matches.append(fields[0])
    if len(matches) != 1 or not re.fullmatch(r"[0-9a-fA-F]{64}", matches[0]):
        raise UpdateError(f"missing or ambiguous SHA256SUMS entry for {name}")
    _download(f"{base}/{name}", archive_path, _MAX_ARCHIVE)
    with archive_path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != matches[0].lower():
        raise UpdateError("release SHA-256 verification failed; no service was changed")
    prefix = f"cc-remote-{installation.role}-v{version}"
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = []
            names = set()
            expanded = 0
            for member in archive:
                members.append(member)
                path = PurePosixPath(member.name)
                expanded += member.size
                if (
                    not path.parts or path.parts[0] != prefix or ".." in path.parts
                    or path.is_absolute() or not (member.isfile() or member.isdir())
                    or path.as_posix() in names or expanded > _MAX_EXPANDED
                    or len(members) > 65536
                ):
                    raise UpdateError("release archive contains unsafe entries")
                names.add(path.as_posix())
            archive.extractall(stage, members=members, filter="data")
    except (tarfile.TarError, EOFError) as exc:
        raise UpdateError("release archive is invalid") from exc
    bundle = stage / prefix
    try:
        target = load_manifest(bundle / "release-manifest.json")
    except (ReleaseManifestError, TypeError) as exc:
        raise UpdateError("downloaded release manifest is invalid") from exc
    for key in ("role", "os", "arch"):
        if target[key] != manifest[key]:
            raise UpdateError(f"downloaded release {key} does not match the installation")
    if target["product_version"] != version:
        raise UpdateError("downloaded release does not match the selected version")
    if type(target["protocol_version"]) is not int or target["protocol_version"] < 1:
        raise UpdateError("downloaded release has an invalid protocol version")
    return bundle


def _claude_contract(release: Path) -> tuple[str, int]:
    """Read constants without importing an SDK or executing the new release."""
    try:
        lock = (release / "requirements-wrapper.lock").read_text()
        sdk = re.search(r"^claude-agent-sdk==([0-9.]+)(?:\s|$)", lock, re.MULTILINE)
        tree = ast.parse((release / "cc_remote/claude_service/wire.py").read_text())
        for statement in tree.body:
            if isinstance(statement, ast.Assign) and any(
                isinstance(name, ast.Name) and name.id == "VERSION" for name in statement.targets
            ):
                wire = ast.literal_eval(statement.value)
                if sdk and type(wire) is int and wire > 0:
                    return sdk[1], wire
    except (OSError, ValueError, SyntaxError):
        pass
    raise UpdateError("cannot verify Claude service compatibility; use the documented service upgrade procedure")


@contextmanager
def update_lock(root: Path):
    try:
        descriptor = acquire_install_lock(root)
    except InstallLockError as exc:
        raise UpdateError(str(exc)) from exc
    try:
        # The installer inherits this descriptor so loss of its controller does
        # not let a second update enter an activation whose result is unknown.
        yield descriptor
    finally:
        os.close(descriptor)


def require_independent_terminal() -> None:
    """A restart must not kill its own updater halfway through activation."""
    pid = os.getppid()
    for _ in range(128):
        if pid <= 1:
            return
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "ppid=", "-o", "args="],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise UpdateError("cannot verify updater ancestry; run from an independent terminal") from exc
        fields = result.stdout.strip().split(maxsplit=1)
        if result.returncode or len(fields) != 2:
            raise UpdateError("cannot verify updater ancestry; run from an independent terminal")
        if re.search(r"(?:^|\s)-m\s+cc_remote\.(?:wrapper|relay)(?:\s|$)", fields[1]):
            raise UpdateError("run update from an independent terminal/SSH session, outside cc-remote")
        try:
            parent = int(fields[0])
        except ValueError as exc:
            raise UpdateError("cannot verify updater ancestry; run from an independent terminal") from exc
        if parent == pid:
            break
        pid = parent
    raise UpdateError("cannot verify updater ancestry")


def run_installer(command: list[str], lock_descriptor: int) -> int:
    # subprocess.run kills its child on KeyboardInterrupt. A role installer may
    # already be rolling back in its INT trap, so leave it alive until it exits.
    process = subprocess.Popen(
        command, pass_fds=(lock_descriptor,),
        env={**os.environ, LOCK_FD_ENV: str(lock_descriptor)},
    )
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            print("\nWaiting for installer shutdown/rollback; do not start a second update.", file=sys.stderr)


def update(*, role: str | None, target_version: str | None, check: bool,
           allow_protocol_change: bool = False) -> int:
    system, machine = host_platform()
    installation = select_installation(role, system, machine)
    repository = release_repository()
    version = target_version or latest_version(repository)
    selected = version_tuple(version)
    if not check and system == "linux" and os.geteuid() != 0:
        raise UpdateError("Linux activation needs root; run sudo cc-remote update")
    if not check and system == "darwin" and os.geteuid() == 0:
        raise UpdateError("run macOS updates as the desktop user, without sudo")
    with (nullcontext() if check else update_lock(installation.root)) as lock_descriptor:
        if not check:
            # A switched current link is provisional until its installer releases
            # the lock. Re-read it before even reporting a no-op as successful.
            fresh = read_installation(installation.root, system, machine)
            if fresh.role != installation.role:
                raise UpdateError("installation role changed; inspect it before retrying")
            installation = fresh
        current = version_tuple(installation.manifest["product_version"])
        print(f"{installation.role}: installed {installation.manifest['product_version']}; selected {version}", flush=True)
        if selected <= current:
            if target_version and selected < current:
                raise UpdateError("update does not downgrade private state; use the documented rollback procedure")
            print("Already up to date." if selected == current else "Installed version is newer than the latest release.")
            return 0
        if check:
            print(f"Update available: cc-remote update --role {installation.role} --version {version}")
            return 0
        require_independent_terminal()
        with tempfile.TemporaryDirectory(prefix="cc-remote-update-") as temporary:
            stage = Path(temporary)
            print(f"Downloading and verifying {installation.role} v{version}...", flush=True)
            bundle = download_bundle(installation, version, release_base(repository, version), stage)
            target = load_manifest(bundle / "release-manifest.json")
            if installation.role == "wrapper" and _claude_contract(installation.release) != _claude_contract(bundle):
                raise UpdateError(
                    "Claude SDK/service compatibility changes in this release; drain native work and "
                    "follow docs/claude-session-service.md before upgrading. No service was changed"
                )
            if target["protocol_version"] != installation.manifest["protocol_version"]:
                if not allow_protocol_change:
                    raise UpdateError(
                        "wire protocol changes in this release; coordinate Relay/Web and every Wrapper, "
                        "then repeat with --allow-protocol-change. No service was changed"
                    )
                print("Protocol upgrade: coordinate every machine and reload Web/PWA clients.", flush=True)
            fresh = read_installation(installation.root, system, machine)
            if fresh != installation:
                raise UpdateError("installation changed while downloading; inspect it before retrying")
            command = ["bash", str(bundle / "deploy" / f"install-{installation.role}.sh"), str(bundle)]
            if installation.role == "relay":
                command += ["--domain", installation.metadata["domain"]]
            elif system == "linux":
                command += ["--user", installation.metadata["user"]]
            print("Activating with the release installer; previous release retained for rollback.", flush=True)
            if run_installer(command, lock_descriptor):
                raise UpdateError("installer did not complete successfully; inspect its rollback report before retrying")
            active = read_installation(installation.root, system, machine)
            if active.manifest != target:
                raise UpdateError("installer returned without activating the selected release; inspect current")
    print(f"Updated {installation.role} to {version}.")
    return 0
