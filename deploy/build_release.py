#!/usr/bin/env python3
"""Build deterministic, role-scoped cc-remote release archives."""
from __future__ import annotations

import argparse
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

from deploy.release_manifest import validate_release_directory
from deploy.validate_protocol_bundle import (
    ProtocolBundleError,
    backend_product_version,
    backend_protocol,
    validate_protocol_bundle,
)


class BuildError(ValueError):
    pass


_ROLES = {"relay", "wrapper"}
_SYSTEMS = {"linux", "darwin"}
_ARCHES = {"x86_64", "arm64"}
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_RELAY_DEPLOY = (
    "Caddyfile",
    "Caddyfile.insecure",
    "Caddyfile.viewer.example",
    "caddy_managed_block.py",
    "cc-remote-relay.service",
    "env.relay.example",
    "install-relay.sh",
    "python-version.txt",
    "release_manifest.py",
    "setup-vps.sh",
    "setup_transaction.sh",
    "validate_protocol_bundle.py",
)
_WRAPPER_DEPLOY = (
    "atomic_symlink.py",
    "cc-remote-wrapper.service",
    "com.muggle.cc-remote.wrapper.plist.in",
    "env.wrapper.example",
    "install-wrapper.sh",
    "prepare_wrapper_stage.py",
    "python-version.txt",
    "release_manifest.py",
    "validate_protocol_bundle.py",
    "work_registry_snapshot.py",
)
_WRAPPER_SCRIPTS = (
    "codex-auth-daemon-restart",
)


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise BuildError(f"required directory is missing: {source}")
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if (
            "__pycache__" in relative.parts
            or path.name.endswith((".pyc", ".pyo"))
            or path.name == ".DS_Store"
        ):
            continue
        if path.is_symlink():
            raise BuildError(f"release source contains a symbolic link: {path}")
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        else:
            raise BuildError(f"release source contains an unsupported entry: {path}")


def _copy_file(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise BuildError(f"required file is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _tar_info(path: Path, arcname: str, epoch: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(arcname)
    stat_result = path.stat()
    info.mtime = epoch
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    if path.is_dir():
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        info.size = 0
    else:
        info.type = tarfile.REGTYPE
        info.mode = 0o755 if stat_result.st_mode & 0o111 else 0o644
        info.size = stat_result.st_size
    return info


def _write_archive(staging: Path, destination: Path, prefix: str, epoch: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw,
                compresslevel=9,
                mtime=epoch,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as archive:
                    root_info = _tar_info(staging, prefix, epoch)
                    archive.addfile(root_info)
                    for path in sorted(staging.rglob("*")):
                        relative = path.relative_to(staging).as_posix()
                        info = _tar_info(path, f"{prefix}/{relative}", epoch)
                        if path.is_file():
                            with path.open("rb") as handle:
                                archive.addfile(info, handle)
                        else:
                            archive.addfile(info)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def build_bundle(
    *,
    root: Path,
    output_dir: Path,
    role: str,
    system: str,
    machine: str,
    uv_bin: Path,
    git_sha: str,
    source_date_epoch: int,
) -> Path:
    root = root.resolve()
    role = role.lower()
    system = system.lower()
    machine = machine.lower()
    if role not in _ROLES:
        raise BuildError(f"unsupported role: {role}")
    if system not in _SYSTEMS:
        raise BuildError(f"unsupported operating system: {system}")
    if machine not in _ARCHES:
        raise BuildError(f"unsupported architecture: {machine}")
    if role == "relay" and system != "linux":
        raise BuildError("relay bundles only support linux")
    if not _SHA_RE.fullmatch(git_sha):
        raise BuildError("git_sha must be a lowercase 40-character SHA")
    if source_date_epoch < 0:
        raise BuildError("source_date_epoch must be non-negative")
    uv_bin = uv_bin.resolve()
    if not uv_bin.is_file() or not uv_bin.stat().st_mode & 0o111:
        raise BuildError("uv executable is missing or not executable")
    uv_version_path = root / "deploy" / "uv-version.txt"
    if not uv_version_path.is_file():
        raise BuildError("deploy/uv-version.txt is missing")
    uv_version = uv_version_path.read_text(encoding="utf-8").strip()
    python_version_path = root / "deploy" / "python-version.txt"
    if not python_version_path.is_file():
        raise BuildError("deploy/python-version.txt is missing")
    python_version = python_version_path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"3\.13\.[0-9]+", python_version):
        raise BuildError("deploy/python-version.txt must pin a Python 3.13 patch")
    try:
        uv_output = subprocess.run(
            [uv_bin, "--version"],
            check=True,
            text=True,
            capture_output=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise BuildError("uv executable could not report its version") from exc
    uv_match = re.fullmatch(
        r"uv ([0-9]+\.[0-9]+\.[0-9]+)(?: \([^\r\n]+\))?",
        uv_output,
    )
    actual_uv_version = uv_match.group(1) if uv_match else ""
    if actual_uv_version != uv_version:
        raise BuildError(
            f"uv version mismatch: expected {uv_version}, got {uv_output or 'empty output'}"
        )

    init_path = root / "cc_remote" / "__init__.py"
    protocol_path = root / "cc_remote" / "protocol.py"
    try:
        product_version = backend_product_version(init_path)
        protocol_version = backend_protocol(protocol_path)
    except ProtocolBundleError as exc:
        raise BuildError(str(exc)) from exc
    if role == "relay":
        try:
            validate_protocol_bundle(
                protocol_path,
                root / "web" / "dist" / "cc-remote-build.json",
                init_path,
            )
        except ProtocolBundleError as exc:
            raise BuildError(str(exc)) from exc
        if not (root / "web" / "dist" / "index.html").is_file():
            raise BuildError("web/dist is missing; build the web client first")
        if not (root / "web" / "dist" / "cc-remote-viewer-runner.js").is_file():
            raise BuildError("Viewer runner is missing; run the complete web build first")

    prefix = f"cc-remote-{role}-v{product_version}"
    name = f"{prefix}-{system}-{machine}.tar.gz"
    output = output_dir.resolve() / name
    with tempfile.TemporaryDirectory(prefix="cc-remote-release-") as temporary:
        staging = Path(temporary) / prefix
        staging.mkdir()
        _copy_tree(root / "cc_remote", staging / "cc_remote")
        _copy_file(root / "LICENSE", staging / "LICENSE")
        lock_name = f"requirements-{role}.lock"
        _copy_file(root / lock_name, staging / lock_name)
        _copy_file(uv_bin, staging / "bin" / "uv")
        (staging / "bin" / "uv").chmod(0o755)
        _copy_file(
            root / "deploy" / "uv-LICENSE-MIT",
            staging / "licenses" / "uv-LICENSE-MIT",
        )

        deploy_files = _RELAY_DEPLOY if role == "relay" else _WRAPPER_DEPLOY
        for filename in deploy_files:
            _copy_file(root / "deploy" / filename, staging / "deploy" / filename)
            if filename.endswith(".sh"):
                (staging / "deploy" / filename).chmod(0o755)
        if role == "relay":
            _copy_tree(root / "web" / "dist", staging / "web" / "dist")
        else:
            for filename in ("cc-remote.mjs", "README.md"):
                _copy_file(root / "integrations" / "dsh" / filename, staging / "integrations" / "dsh" / filename)
            for filename in _WRAPPER_SCRIPTS:
                target = staging / "scripts" / filename
                _copy_file(root / "scripts" / filename, target)
                target.chmod(0o755)

        manifest = {
            "schema": 1,
            "product_version": product_version,
            "protocol_version": protocol_version,
            "git_sha": git_sha,
            "role": role,
            "os": system,
            "arch": machine,
            "python": python_version,
            "uv": uv_version,
        }
        (staging / "release-manifest.json").write_text(
            json.dumps(manifest, sort_keys=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        validate_release_directory(
            staging,
            expected_role=role,
            expected_system=system,
            expected_arch=machine,
        )
        _write_archive(staging, output, prefix, source_date_epoch)
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--os", dest="system", required=True)
    parser.add_argument("--arch", dest="machine", required=True)
    parser.add_argument("--uv-bin", type=Path, required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument(
        "--source-date-epoch",
        type=int,
        default=int(os.environ.get("SOURCE_DATE_EPOCH", "0")),
    )
    args = parser.parse_args()
    try:
        output = build_bundle(
            root=args.root,
            output_dir=args.output_dir,
            role=args.role,
            system=args.system,
            machine=args.machine,
            uv_bin=args.uv_bin,
            git_sha=args.git_sha,
            source_date_epoch=args.source_date_epoch,
        )
    except BuildError as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
