"""Offline release-bundle and one-command installer guardrails."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile

import pytest

from cc_remote import __version__
from cc_remote.protocol import PROTOCOL_VERSION
from deploy.build_release import BuildError, _copy_tree, build_bundle
from deploy.release_manifest import (
    ReleaseManifestError,
    validate_release_directory,
)


ROOT = Path(__file__).resolve().parents[1]
FULL_SHA = "0123456789abcdef0123456789abcdef01234567"


def test_release_workflow_materializes_web_before_python_bundle_tests():
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    python_job = workflow.split("\n  python:\n", 1)[1].split(
        "\n  web:\n", 1)[0]
    web_job = workflow.split("\n  web:\n", 1)[1].split(
        "\n  deploy:\n", 1)[0]

    assert "uses: ./.github/workflows/ci.yml" in release
    assert "needs: web" in python_job
    assert web_job.index("npm --prefix web ci") < web_job.index(
        "npm --prefix web run build")
    assert web_job.index("npm --prefix web run build") < web_job.index(
        "name: ci-web-dist")
    assert python_job.index("name: ci-web-dist") < python_job.index(
        ".venv/bin/python -m pytest")


def _fake_uv(tmp_path: Path) -> Path:
    uv = tmp_path / "uv"
    version = (ROOT / "deploy" / "uv-version.txt").read_text().strip()
    uv.write_text(
        f"#!/bin/sh\n[ \"${{1:-}}\" = --version ] && echo 'uv {version}'\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    return uv


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _members(path: Path) -> set[str]:
    with tarfile.open(path, "r:gz") as archive:
        return {member.name for member in archive.getmembers()}


def _manifest(path: Path) -> dict[str, object]:
    with tarfile.open(path, "r:gz") as archive:
        member = next(
            item for item in archive.getmembers()
            if item.name.endswith("/release-manifest.json")
        )
        extracted = archive.extractfile(member)
        assert extracted is not None
        return json.loads(extracted.read())


def _bootstrap_fixture(
    directory: Path,
    *,
    checksum: str | None = None,
    symlink: bool = False,
) -> tuple[Path, str]:
    asset = f"cc-remote-wrapper-v{__version__}-darwin-arm64.tar.gz"
    prefix = f"cc-remote-wrapper-v{__version__}"
    archive_path = directory / asset
    marker = directory / "installer-ran"
    with tarfile.open(archive_path, "w:gz") as archive:
        for name in (prefix, f"{prefix}/deploy"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            archive.addfile(info)
        if symlink:
            info = tarfile.TarInfo(f"{prefix}/deploy/unsafe")
            info.type = tarfile.SYMTYPE
            info.linkname = "/tmp"
            archive.addfile(info)
        installer = (
            "#!/bin/sh\n"
            "printf '%s\\n' \"$@\" > \"$CC_REMOTE_INSTALL_MARKER\"\n"
        ).encode()
        info = tarfile.TarInfo(f"{prefix}/deploy/install-wrapper.sh")
        info.mode = 0o755
        info.size = len(installer)
        archive.addfile(info, io.BytesIO(installer))
    digest = checksum or _digest(archive_path)
    (directory / "SHA256SUMS").write_text(f"{digest}  {asset}\n")
    return marker, asset


@pytest.mark.parametrize(
    ("role", "system", "machine"),
    [
        ("relay", "linux", "x86_64"),
        ("wrapper", "linux", "arm64"),
        ("wrapper", "darwin", "arm64"),
    ],
)
def test_release_bundles_are_deterministic_and_role_scoped(
    tmp_path: Path,
    role: str,
    system: str,
    machine: str,
):
    uv = _fake_uv(tmp_path)
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first = build_bundle(
        root=ROOT,
        output_dir=first_dir,
        role=role,
        system=system,
        machine=machine,
        uv_bin=uv,
        git_sha=FULL_SHA,
        source_date_epoch=1_700_000_000,
    )
    second = build_bundle(
        root=ROOT,
        output_dir=second_dir,
        role=role,
        system=system,
        machine=machine,
        uv_bin=uv,
        git_sha=FULL_SHA,
        source_date_epoch=1_700_000_000,
    )

    assert first.name == (
        f"cc-remote-{role}-v{__version__}-{system}-{machine}.tar.gz"
    )
    assert _digest(first) == _digest(second)

    prefix = f"cc-remote-{role}-v{__version__}"
    members = _members(first)
    assert f"{prefix}/release-manifest.json" in members
    assert f"{prefix}/bin/uv" in members
    assert f"{prefix}/licenses/uv-LICENSE-MIT" in members
    assert f"{prefix}/cc_remote/protocol.py" in members
    assert not any("/tests/" in name for name in members)
    assert not any(name.endswith("/.env") for name in members)
    assert not any("/node_modules/" in name for name in members)

    if role == "relay":
        assert not any("/integrations/dsh/" in name for name in members)
        assert f"{prefix}/web/dist/index.html" in members
        assert f"{prefix}/web/dist/cc-remote-viewer-runner.js" in members
        assert f"{prefix}/requirements-relay.lock" in members
        assert f"{prefix}/deploy/install-relay.sh" in members
        assert f"{prefix}/deploy/setup-vps.sh" in members
        assert f"{prefix}/deploy/Caddyfile.insecure" in members
        assert f"{prefix}/deploy/install-wrapper.sh" not in members
        assert f"{prefix}/requirements-wrapper.lock" not in members
    else:
        assert f"{prefix}/integrations/dsh/cc-remote.mjs" in members
        assert f"{prefix}/integrations/dsh/README.md" in members
        assert f"{prefix}/integrations/dsh/cc-remote.test.mjs" not in members
        assert not any("/web/" in name for name in members)
        assert f"{prefix}/requirements-wrapper.lock" in members
        assert f"{prefix}/deploy/install-wrapper.sh" in members
        assert f"{prefix}/deploy/work_registry_snapshot.py" in members
        assert f"{prefix}/scripts/codex-auth-daemon-restart" in members
        assert f"{prefix}/deploy/setup-vps.sh" not in members
        assert f"{prefix}/requirements-relay.lock" not in members

    manifest = _manifest(first)
    assert manifest == {
        "schema": 1,
        "product_version": __version__,
        "protocol_version": PROTOCOL_VERSION,
        "git_sha": FULL_SHA,
        "role": role,
        "os": system,
        "arch": machine,
        "python": (ROOT / "deploy" / "python-version.txt").read_text().strip(),
        "uv": (ROOT / "deploy" / "uv-version.txt").read_text().strip(),
    }


def test_release_builder_rejects_unsupported_role_platform_and_bad_uv(
    tmp_path: Path,
):
    uv = _fake_uv(tmp_path)
    common = {
        "root": ROOT,
        "output_dir": tmp_path / "out",
        "uv_bin": uv,
        "git_sha": FULL_SHA,
        "source_date_epoch": 1_700_000_000,
    }
    with pytest.raises(BuildError, match="unsupported role"):
        build_bundle(role="desktop", system="darwin", machine="arm64", **common)
    with pytest.raises(BuildError, match="relay bundles only support linux"):
        build_bundle(role="relay", system="darwin", machine="arm64", **common)
    with pytest.raises(BuildError, match="unsupported architecture"):
        build_bundle(role="wrapper", system="linux", machine="riscv64", **common)

    uv.chmod(0o644)
    with pytest.raises(BuildError, match="uv executable"):
        build_bundle(role="wrapper", system="darwin", machine="arm64", **common)

    uv.chmod(0o755)
    uv.write_text("#!/bin/sh\necho 'uv 0.0.0'\n", encoding="utf-8")
    with pytest.raises(BuildError, match="uv version mismatch"):
        build_bundle(role="wrapper", system="darwin", machine="arm64", **common)


def test_release_builder_rejects_symbolic_links(tmp_path: Path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    target = tmp_path / "outside"
    target.write_text("must not enter the release", encoding="utf-8")
    (source / "linked").symlink_to(target)

    with pytest.raises(BuildError, match="symbolic link"):
        _copy_tree(source, destination)


def test_release_bootstrap_fails_closed_before_download():
    script = ROOT / "deploy" / "install.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)

    unsupported = subprocess.run(
        [str(script), "wrapper"],
        env={
            "PATH": "/usr/bin:/bin",
            "CC_REMOTE_TEST_OS": "windows",
            "CC_REMOTE_TEST_ARCH": "x86_64",
        },
        text=True,
        capture_output=True,
    )
    assert unsupported.returncode != 0
    assert "unsupported operating system" in unsupported.stderr.lower()

    invalid_version = subprocess.run(
        [str(script), "wrapper"],
        env={
            "PATH": "/usr/bin:/bin",
            "CC_REMOTE_VERSION": "../3.0.0",
            "CC_REMOTE_TEST_OS": "darwin",
            "CC_REMOTE_TEST_ARCH": "arm64",
        },
        text=True,
        capture_output=True,
    )
    assert invalid_version.returncode != 0
    assert "exact semantic version" in invalid_version.stderr.lower()

    relay_on_mac = subprocess.run(
        [str(script), "relay"],
        env={
            "PATH": "/usr/bin:/bin",
            "CC_REMOTE_TEST_OS": "darwin",
            "CC_REMOTE_TEST_ARCH": "arm64",
        },
        text=True,
        capture_output=True,
    )
    assert relay_on_mac.returncode != 0
    assert "relay requires linux" in relay_on_mac.stderr.lower()

    source = script.read_text(encoding="utf-8")
    verify = source.index("verify_checksum")
    extract = source.index("tar -xzf")
    execute = source.index("install-$role.sh")
    assert verify < extract < execute
    assert "curl |" not in source
    assert "CC_REMOTE_RELEASE_BASE_URL" in source
    assert "WRAPPER_TOKEN" not in source
    assert "LOGIN_PASSWORD=" not in source


def test_release_bootstrap_verifies_before_invoking_role_installer(
    tmp_path: Path,
):
    marker, _ = _bootstrap_fixture(tmp_path)
    script = ROOT / "deploy" / "install.sh"
    result = subprocess.run(
        [str(script), "wrapper", "--relay", "https://example.test"],
        env={
            **os.environ,
            "CC_REMOTE_RELEASE_BASE_URL": tmp_path.as_uri(),
            "CC_REMOTE_TEST_OS": "darwin",
            "CC_REMOTE_TEST_ARCH": "arm64",
            "CC_REMOTE_INSTALL_MARKER": str(marker),
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert marker.is_file()
    assert "https://example.test" in marker.read_text()


@pytest.mark.parametrize(
    ("checksum", "symlink", "message"),
    [
        ("0" * 64, False, "sha256 verification failed"),
        (None, True, "unsafe path"),
    ],
)
def test_release_bootstrap_rejects_tampered_or_linked_archives(
    tmp_path: Path,
    checksum: str | None,
    symlink: bool,
    message: str,
):
    marker, _ = _bootstrap_fixture(
        tmp_path,
        checksum=checksum,
        symlink=symlink,
    )
    result = subprocess.run(
        [str(ROOT / "deploy" / "install.sh"), "wrapper"],
        env={
            **os.environ,
            "CC_REMOTE_RELEASE_BASE_URL": tmp_path.as_uri(),
            "CC_REMOTE_TEST_OS": "darwin",
            "CC_REMOTE_TEST_ARCH": "arm64",
            "CC_REMOTE_INSTALL_MARKER": str(marker),
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert message in result.stderr.lower()
    assert not marker.exists()


def test_release_manifest_and_atomic_switch_fail_closed(tmp_path: Path):
    bundle = build_bundle(
        root=ROOT,
        output_dir=tmp_path,
        role="wrapper",
        system="darwin",
        machine="arm64",
        uv_bin=_fake_uv(tmp_path),
        git_sha=FULL_SHA,
        source_date_epoch=1_700_000_000,
    )
    extracted = tmp_path / "extracted"
    with tarfile.open(bundle, "r:gz") as archive:
        archive.extractall(extracted, filter="data")
    root = extracted / f"cc-remote-wrapper-v{__version__}"
    manifest = json.loads((root / "release-manifest.json").read_text())
    manifest["protocol_version"] += 1
    (root / "release-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ReleaseManifestError, match="protocol version"):
        validate_release_directory(root)

    first = tmp_path / "release-one"
    second = tmp_path / "release-two"
    first.mkdir()
    second.mkdir()
    link = tmp_path / "current"
    helper = ROOT / "deploy" / "atomic_symlink.py"
    subprocess.run([str(helper), str(first), str(link)], check=True)
    assert link.resolve() == first.resolve()
    subprocess.run([str(helper), str(second), str(link)], check=True)
    assert link.resolve() == second.resolve()


def test_role_installers_keep_credentials_out_of_service_definitions():
    relay = ROOT / "deploy" / "install-relay.sh"
    wrapper = ROOT / "deploy" / "install-wrapper.sh"
    plist = ROOT / "deploy" / "com.muggle.cc-remote.wrapper.plist.in"
    for script in (relay, wrapper):
        subprocess.run(["bash", "-n", str(script)], check=True)

    relay_source = relay.read_text(encoding="utf-8")
    assert "read -r -s" in relay_source
    assert "openssl rand -hex 32" in relay_source
    assert 'ubuntu) minimum_major=22' in relay_source
    assert 'debian) minimum_major=12' in relay_source
    assert "setup-vps.sh" in relay_source
    assert "--allow-private-origins" in relay_source
    assert '"RELAY_HOST=${relay_host}"' in relay_source
    assert '"ALLOW_PRIVATE_ORIGINS=${allow_private_origins}"' in relay_source
    assert "relay_host=0.0.0.0" in relay_source
    assert "--login-password" not in relay_source
    assert "LOGIN_PASSWORD=" in relay_source

    wrapper_source = wrapper.read_text(encoding="utf-8")
    assert "releases" in wrapper_source
    assert "current" in wrapper_source
    assert "launchctl bootstrap" in wrapper_source
    assert "systemctl enable" in wrapper_source
    assert "cc_remote.device" in wrapper_source
    assert "pair_args=(pair" in wrapper_source
    assert "--env-file" in wrapper_source
    assert "requirements-wrapper.lock" in wrapper_source
    assert 'UV_PYTHON_INSTALL_DIR="$runtimes"' in wrapper_source
    assert "--relocatable" in wrapper_source
    assert '$python_runtime' in wrapper_source

    plist_source = plist.read_text(encoding="utf-8")
    assert "ProgramArguments" in plist_source
    assert "KeepAlive" in plist_source
    assert "WRAPPER_TOKEN" not in plist_source
    assert "PAIR" not in plist_source


@pytest.mark.parametrize("name", ["install-relay.sh", "install-wrapper.sh"])
def test_role_installers_avoid_ambiguous_and_or_guards(name: str):
    source = (ROOT / "deploy" / name).read_text(encoding="utf-8")
    ambiguous_guard = re.compile(
        r"\[[^\n]+\]\s*&&\s*\[[^\n]+\]\s*\|\|"
    )
    assert ambiguous_guard.search(source) is None


def test_role_locks_and_release_workflow_are_versioned_inputs():
    relay_lock = ROOT / "requirements-relay.lock"
    wrapper_lock = ROOT / "requirements-wrapper.lock"
    for lock in (relay_lock, wrapper_lock):
        source = lock.read_text(encoding="utf-8")
        assert "--hash=sha256:" in source

    assert "fastapi==" in relay_lock.read_text(encoding="utf-8")
    assert "claude-agent-sdk==" not in relay_lock.read_text(encoding="utf-8")
    assert "claude-agent-sdk==" in wrapper_lock.read_text(encoding="utf-8")
    assert "fastapi==" not in wrapper_lock.read_text(encoding="utf-8")

    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    source_workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "tags:" in workflow
    assert "v*.*.*" in workflow
    assert "npm --prefix web run build" in workflow
    assert "uses: ./.github/workflows/ci.yml" in workflow
    assert "pytest" in source_workflow
    assert "test:reliability" in source_workflow
    assert "test:viewer" in source_workflow
    assert "actions/attest" in workflow
    assert "gh release upload" in workflow
    uv_version = (ROOT / "deploy" / "uv-version.txt").read_text().strip()
    assert (
        workflow + source_workflow
    ).count(f'version: "{uv_version}"') == 3
    python_version = (
        ROOT / "deploy" / "python-version.txt"
    ).read_text().strip()
    assert re.fullmatch(r"3\.13\.\d+", python_version)
    assert "cat deploy/python-version.txt" in workflow


def test_release_locks_keep_intel_macos_cryptography_wheel():
    compatible_pin = "cryptography==48.0.0"
    assert compatible_pin in (ROOT / "requirements.txt").read_text()
    for name in (
        "requirements.lock",
        "requirements-relay.lock",
        "requirements-wrapper.lock",
    ):
        assert compatible_pin in (ROOT / name).read_text()
