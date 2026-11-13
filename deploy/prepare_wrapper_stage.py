#!/usr/bin/env python3
"""Prepare and validate a release-local Wrapper Python environment.

This command runs as the eventual Wrapper service user, never as root.  An
identical dependency/runtime contract may reuse the active environment.  Any
contract change is materialized with the repository-pinned uv and Python
versions before a privileged activator takes ownership of the release.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import NoReturn


MANIFEST_NAME = "wrapper-stage-manifest.json"
MANIFEST_SCHEMA = 1
_PYTHON_PIN_RE = re.compile(r"3\.13\.[0-9]+")
_RELEASE_RE = re.compile(
    r"release-[0-9]{8}T[0-9]{6}Z-v(?P<protocol>[0-9]{1,4})-"
    r"[a-z0-9][a-z0-9-]{0,63}"
)
_UV_VERSION_RE = re.compile(
    r"uv (?P<version>[0-9]+\.[0-9]+\.[0-9]+)(?: \([^\r\n]+\))?"
)


class StagePreparationError(ValueError):
    """The release stage cannot be prepared safely."""


def _fail(message: str) -> NoReturn:
    raise StagePreparationError(message)


def _directory(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        _fail(f"{label} must be an absolute path")
    if path.is_symlink() or not path.is_dir():
        _fail(f"{label} must be a real directory")
    resolved = path.resolve(strict=True)
    if resolved != path:
        _fail(f"{label} must not contain symbolic path components")
    return resolved


def _regular_file(path: Path, *, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        _fail(f"{label} must be a regular file")
    return path


def _read_pin(release: Path) -> str:
    path = _regular_file(
        release / "deploy" / "python-version.txt",
        label="deploy/python-version.txt",
    )
    value = path.read_text(encoding="utf-8").strip()
    if _PYTHON_PIN_RE.fullmatch(value) is None:
        _fail("deploy/python-version.txt must pin a Python 3.13 patch")
    return value


def _read_uv_pin(stage: Path) -> str:
    path = _regular_file(
        stage / "deploy" / "uv-version.txt",
        label="deploy/uv-version.txt",
    )
    value = path.read_text(encoding="utf-8").strip()
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value) is None:
        _fail("deploy/uv-version.txt must pin an exact uv version")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "PYTHONHOME",
        "PYTHONPATH",
        "UV_PROJECT_ENVIRONMENT",
        "UV_PYTHON",
        "VIRTUAL_ENV",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "UV_NO_CONFIG": "1",
        }
    )
    return environment


def _uv_version(command: list[str], environment: dict[str, str]) -> str | None:
    try:
        result = subprocess.run(
            [*command, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = _UV_VERSION_RE.fullmatch(result.stdout.strip())
    return match.group("version") if match is not None else None


def _candidate_uv_paths(explicit: Path | None, old: Path) -> list[Path]:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit.expanduser())
    discovered = shutil.which("uv")
    if discovered:
        candidates.append(Path(discovered))
    candidates.append(old / "bin" / "uv")
    for release in sorted(old.parent.glob("release-*/bin/uv"), reverse=True):
        candidates.append(release)

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved in seen or not resolved.is_file() or not os.access(resolved, os.X_OK):
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def select_uv_command(
    *,
    explicit: Path | None,
    old: Path,
    expected_version: str,
    environment: dict[str, str],
) -> list[str]:
    """Return a uv command prefix that resolves to the pinned version."""

    candidates = _candidate_uv_paths(explicit, old)
    for candidate in candidates:
        direct = [str(candidate), "--no-config"]
        if _uv_version(direct, environment) == expected_version:
            return direct

    # A different local uv may bootstrap the exact pinned uv into the user's
    # cache.  It still runs without privileges and the resolved version is
    # checked before it is allowed to prepare a release.
    for candidate in candidates:
        nested = [
            str(candidate),
            "tool",
            "run",
            "--from",
            f"uv=={expected_version}",
            "uv",
            "--no-config",
        ]
        if _uv_version(nested, environment) == expected_version:
            return nested

    _fail(
        f"no usable uv can resolve repository pin {expected_version}; "
        "pass --uv with a trusted executable"
    )


def _copy_existing_venv(old: Path, destination: Path) -> None:
    source = old / ".venv"
    if source.is_symlink() or not (source / "bin" / "python").exists():
        _fail("active release Python environment is incomplete")
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def _run_checked(command: list[str], *, environment: dict[str, str]) -> None:
    try:
        subprocess.run(command, check=True, env=environment)
    except (OSError, subprocess.CalledProcessError) as exc:
        rendered = " ".join(command)
        _fail(f"command failed while preparing Wrapper environment: {rendered}: {exc}")


def _build_venv(
    *,
    stage: Path,
    old: Path,
    runtimes: Path,
    explicit_uv: Path | None,
    python_pin: str,
    environment: dict[str, str],
) -> str:
    uv_pin = _read_uv_pin(stage)
    uv = select_uv_command(
        explicit=explicit_uv,
        old=old,
        expected_version=uv_pin,
        environment=environment,
    )
    environment["UV_PYTHON_INSTALL_DIR"] = str(runtimes)
    venv = stage / ".venv"
    _run_checked(
        [
            *uv,
            "venv",
            "--no-project",
            "--managed-python",
            "--relocatable",
            "--python",
            python_pin,
            str(venv),
        ],
        environment=environment,
    )
    _run_checked(
        [
            *uv,
            "pip",
            "sync",
            "--python",
            str(venv / "bin" / "python"),
            "--require-hashes",
            "--only-binary=:all:",
            "--link-mode",
            "copy",
            str(stage / "requirements-wrapper.lock"),
        ],
        environment=environment,
    )
    return uv_pin


_VALIDATE_CODE = r"""
import importlib
import json
from pathlib import Path
import sys

stage = Path(sys.argv[1]).resolve(strict=True)
venv = Path(sys.argv[2]).resolve(strict=True)
runtimes = Path(sys.argv[3]).resolve(strict=True)
python_pin = sys.argv[4]
expected_protocol = int(sys.argv[5])

if Path(sys.prefix).resolve(strict=True) != venv:
    raise SystemExit("validation did not run in the staged environment")
actual_version = ".".join(str(value) for value in sys.version_info[:3])
if actual_version != python_pin:
    raise SystemExit(
        f"staged Python is {actual_version}, expected {python_pin}"
    )
base_prefix = Path(sys.base_prefix).resolve(strict=True)
if base_prefix != runtimes and runtimes not in base_prefix.parents:
    raise SystemExit("staged Python runtime is outside the declared runtime root")

lock = (stage / "requirements-wrapper.lock").read_text(encoding="utf-8")
modules = ["claude_agent_sdk", "httpx", "pydantic", "websockets"]
if "\npillow==" in "\n" + lock.lower():
    modules.append("PIL")
for module in modules:
    importlib.import_module(module)

sys.path.insert(0, str(stage))
from cc_remote import __version__
from cc_remote.protocol import PROTOCOL_VERSION
from cc_remote.wrapper.machine import WrapperMachine

if PROTOCOL_VERSION != expected_protocol:
    raise SystemExit("Python protocol does not match the release name")
build = json.loads(
    (stage / "web" / "dist" / "cc-remote-build.json").read_text(
        encoding="utf-8"
    )
)
if build.get("protocol") != expected_protocol:
    raise SystemExit("Web protocol does not match the release name")
if build.get("version") != __version__:
    raise SystemExit("Web product version does not match Python")
assert WrapperMachine
print("CC_REMOTE_STAGE_RESULT=" + json.dumps({
    "python_version": actual_version,
    "runtime": str(base_prefix),
    "product_version": __version__,
}, sort_keys=True))
"""


def _validate_environment(
    *,
    python: Path,
    stage: Path,
    venv: Path,
    runtimes: Path,
    python_pin: str,
    protocol: int,
    environment: dict[str, str],
) -> dict[str, str]:
    if python.is_symlink():
        try:
            python.resolve(strict=True)
        except OSError as exc:
            _fail(f"staged Python link is broken: {exc}")
    if not python.exists() or not os.access(python, os.X_OK):
        _fail("staged Python is not executable")
    result = subprocess.run(
        [
            str(python),
            "-B",
            "-I",
            "-c",
            _VALIDATE_CODE,
            str(stage),
            str(venv),
            str(runtimes),
            python_pin,
            str(protocol),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        _fail(f"staged Wrapper import validation failed: {detail}")
    marker = "CC_REMOTE_STAGE_RESULT="
    payload = next(
        (line[len(marker):] for line in result.stdout.splitlines() if line.startswith(marker)),
        None,
    )
    if payload is None:
        _fail("staged Wrapper validation did not return metadata")
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        _fail(f"staged Wrapper validation returned invalid metadata: {exc}")
    if not isinstance(parsed, dict) or not all(
        isinstance(parsed.get(key), str)
        for key in ("python_version", "runtime", "product_version")
    ):
        _fail("staged Wrapper validation metadata has the wrong shape")
    return parsed


def _normalize_venv_permissions(venv: Path) -> None:
    for root, directories, files in os.walk(venv, followlinks=False):
        root_path = Path(root)
        os.chmod(root_path, 0o750)
        for name in directories:
            path = root_path / name
            if not path.is_symlink():
                os.chmod(path, 0o750)
        for name in files:
            path = root_path / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                continue
            mode = 0o750 if metadata.st_mode & 0o111 else 0o640
            os.chmod(path, mode)


def _write_manifest(stage: Path, payload: dict[str, object]) -> Path:
    destination = stage / "deploy" / MANIFEST_NAME
    if destination.exists() or destination.is_symlink():
        _fail(f"stage already contains deploy/{MANIFEST_NAME}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{MANIFEST_NAME}.",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def prepare_stage(
    *,
    old: Path,
    stage: Path,
    runtimes: Path,
    release: str,
    explicit_uv: Path | None,
    defer_identical: bool,
) -> dict[str, object]:
    match = _RELEASE_RE.fullmatch(release)
    if match is None:
        _fail("release name is invalid")
    protocol = int(match.group("protocol"))
    old = _directory(old, label="active release")
    stage = _directory(stage, label="release stage")
    if not runtimes.is_absolute():
        _fail("runtime root must be an absolute path")
    if runtimes.is_symlink():
        _fail("runtime root must not be a symbolic link")
    runtimes.mkdir(parents=True, exist_ok=True)
    runtimes = _directory(runtimes, label="runtime root")
    if old == stage or old in stage.parents or stage in old.parents:
        _fail("active release and stage must be separate trees")

    old_lock = _regular_file(
        old / "requirements-wrapper.lock",
        label="active requirements-wrapper.lock",
    )
    stage_lock = _regular_file(
        stage / "requirements-wrapper.lock",
        label="staged requirements-wrapper.lock",
    )
    _regular_file(
        stage / "cc_remote" / "protocol.py",
        label="cc_remote/protocol.py",
    )
    _regular_file(
        stage / "web" / "dist" / "cc-remote-build.json",
        label="web/dist/cc-remote-build.json",
    )
    venv = stage / ".venv"
    manifest = stage / "deploy" / MANIFEST_NAME
    if venv.exists() or venv.is_symlink():
        _fail("release stage already contains .venv")
    if manifest.exists() or manifest.is_symlink():
        _fail(f"release stage already contains deploy/{MANIFEST_NAME}")

    old_pin = _read_pin(old)
    python_pin = _read_pin(stage)
    identical = old_pin == python_pin and old_lock.read_bytes() == stage_lock.read_bytes()
    strategy = "reuse" if identical and defer_identical else "copy"
    uv_version: str | None = None
    created_venv = False
    environment = _clean_environment()
    try:
        if strategy != "reuse":
            venv.mkdir(mode=0o700)
            created_venv = True
        if strategy == "reuse":
            validation_python = old / ".venv" / "bin" / "python"
            validation_venv = old / ".venv"
        elif identical:
            _copy_existing_venv(old, venv)
            validation_python = venv / "bin" / "python"
            validation_venv = venv
        else:
            strategy = "rebuild"
            uv_version = _build_venv(
                stage=stage,
                old=old,
                runtimes=runtimes,
                explicit_uv=explicit_uv,
                python_pin=python_pin,
                environment=environment,
            )
            validation_python = venv / "bin" / "python"
            validation_venv = venv

        # The validator expects its venv argument to equal sys.prefix.  A
        # deferred reuse validates the active venv while importing staged code.
        validation_stage_venv = stage / ".venv"
        if strategy == "reuse":
            validation_stage_venv = validation_venv
        metadata = _validate_environment(
            python=validation_python,
            stage=stage,
            runtimes=runtimes,
            python_pin=python_pin,
            protocol=protocol,
            environment=environment,
            venv=validation_stage_venv,
        )
        if created_venv:
            _normalize_venv_permissions(venv)
        payload: dict[str, object] = {
            "schema": MANIFEST_SCHEMA,
            "release": release,
            "protocol": protocol,
            "strategy": strategy,
            "requirements_lock_sha256": _sha256(stage_lock),
            "python_pin": python_pin,
            "python_version": metadata["python_version"],
            "runtime": metadata["runtime"],
            "product_version": metadata["product_version"],
            "uv": uv_version,
        }
        _write_manifest(stage, payload)
        return payload
    except BaseException:
        if created_venv and venv.is_dir() and not venv.is_symlink():
            shutil.rmtree(venv)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", type=Path, required=True)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--runtimes", type=Path, required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--uv", type=Path)
    parser.add_argument(
        "--defer-identical",
        action="store_true",
        help="let the privileged nono activator copy an identical active venv",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        payload = prepare_stage(
            old=args.old,
            stage=args.stage,
            runtimes=args.runtimes,
            release=args.release,
            explicit_uv=args.uv,
            defer_identical=args.defer_identical,
        )
    except (OSError, StagePreparationError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
