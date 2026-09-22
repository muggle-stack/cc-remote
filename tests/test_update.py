"""Offline management CLI checks: real downloads from fixtures, no live services."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from cc_remote import update as updater
from cc_remote.__main__ import main
from deploy.install_cli import check_destination, install_cli
from deploy import install_cli as cli_installer
from deploy.install_lock import acquire_install_lock


ROOT = Path(__file__).resolve().parents[1]


def _manifest(role="wrapper", system="darwin", version="4.0.0"):
    return {
        "schema": 1, "product_version": version, "protocol_version": 72,
        "git_sha": "a" * 40, "role": role, "os": system, "arch": "arm64",
        "python": "3.13.9", "uv": "0.11.16",
    }


def _installation(tmp_path, monkeypatch, *, role="wrapper", system="darwin"):
    root = tmp_path / f"managed {role}"
    release = root / "releases/old"
    release.mkdir(parents=True)
    (root / "current").symlink_to(release)
    (release / "release-manifest.json").write_text(json.dumps(_manifest(role, system)))
    (release / "requirements-wrapper.lock").write_text("claude-agent-sdk==0.2.151\n")
    wire = release / "cc_remote/claude_service/wire.py"
    wire.parent.mkdir(parents=True)
    wire.write_text("VERSION = 1\n")
    metadata = {"schema": 1, "role": role}
    metadata.update({"user": "service-user"} if role == "wrapper" else {"domain": "remote.example.test"})
    (root / "installation.json").write_text(json.dumps(metadata))
    (root / "operator-config").write_text("preserve operator settings\n")
    (root / "native-service-alive").write_text("still running\n")
    monkeypatch.setattr(updater, "installation_roots", lambda _: {role: root})
    monkeypatch.setattr(updater, "host_platform", lambda: (system, "arm64"))
    monkeypatch.setattr(updater.os, "geteuid", lambda: 0 if system == "linux" else 501)
    # The fixture models activation privileges; its real files still belong to
    # this test process. Exercise native locking with that actual OS identity.
    def native_lock(path):
        with monkeypatch.context() as native_identity:
            native_identity.setattr(updater.os, "geteuid", os.getuid)
            return acquire_install_lock(path)
    monkeypatch.setattr(updater, "acquire_install_lock", native_lock)
    monkeypatch.setattr(updater, "require_independent_terminal", lambda: None)
    return updater.read_installation(root, system, "arm64")


def _bundle(tmp_path, installation, *, changes=None, unsafe=None, fail=False):
    mirror = tmp_path / "mirror"
    mirror.mkdir(exist_ok=True)
    target = {**installation.manifest, "product_version": "4.0.1", "git_sha": "b" * 40, **(changes or {})}
    prefix = f"cc-remote-{installation.role}-v4.0.1"
    filename = f"{prefix}-{installation.manifest['os']}-arm64.tar.gz"
    marker = tmp_path / "installer-calls.json"
    body = f"""
import json, pathlib, shutil, sys
root = pathlib.Path({str(installation.root)!r})
marker = pathlib.Path({str(marker)!r})
calls = json.loads(marker.read_text()) if marker.exists() else []
calls.append(sys.argv[1:])
marker.write_text(json.dumps(calls))
if {fail!r}: raise SystemExit(7)
bundle = pathlib.Path(sys.argv[1])
target = root / 'releases' / 'new'
shutil.copytree(bundle, target)
link = root / 'next'
link.symlink_to(target)
link.replace(root / 'current')
"""
    installer = f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(body)} \"$@\"\n"
    files = {
        "release-manifest.json": json.dumps(target).encode(),
        "requirements-wrapper.lock": b"claude-agent-sdk==0.2.151\n",
        "cc_remote/claude_service/wire.py": b"VERSION = 1\n",
        f"deploy/install-{installation.role}.sh": installer.encode(),
    }
    with tarfile.open(mirror / filename, "w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size = len(data)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            archive.addfile(info, io.BytesIO(data))
        if unsafe:
            info = tarfile.TarInfo(unsafe if unsafe.startswith("/") else f"{prefix}/{unsafe}")
            if unsafe == "link":
                info.type = tarfile.SYMTYPE
                info.linkname = "/tmp"
            archive.addfile(info)
    digest = hashlib.sha256((mirror / filename).read_bytes()).hexdigest()
    (mirror / "SHA256SUMS").write_text(f"{digest}  {filename}\n")
    return mirror, marker, filename


@pytest.mark.parametrize("role,system", [("wrapper", "darwin"), ("wrapper", "linux"), ("relay", "linux")])
def test_update_downloads_verified_bundle_and_preserves_install_identity(tmp_path, monkeypatch, role, system):
    installation = _installation(tmp_path, monkeypatch, role=role, system=system)
    mirror, marker, _ = _bundle(tmp_path, installation)
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", mirror.as_uri())
    old_metadata = (installation.root / "installation.json").read_bytes()
    assert main(["update", "--version", "4.0.1"]) == 0
    active = updater.read_installation(installation.root, system, "arm64")
    assert active.manifest["product_version"] == "4.0.1"
    assert installation.release.exists()
    assert (installation.root / "installation.json").read_bytes() == old_metadata
    assert (installation.root / "operator-config").read_text() == "preserve operator settings\n"
    assert (installation.root / "native-service-alive").read_text() == "still running\n"
    calls = json.loads(marker.read_text())
    assert len(calls) == 1
    expected = ["--domain", "remote.example.test"] if role == "relay" else (
        ["--user", "service-user"] if system == "linux" else []
    )
    assert calls[0][1:] == expected


def test_check_never_downloads_locks_or_activates(tmp_path, monkeypatch, capsys):
    installation = _installation(tmp_path, monkeypatch)
    before = sorted(installation.root.iterdir())
    monkeypatch.setattr(updater, "latest_version", lambda _: "4.0.1")
    monkeypatch.setattr(updater, "download_bundle", lambda *a: pytest.fail("unexpected download"))
    assert main(["update", "--check"]) == 0
    assert "Update available" in capsys.readouterr().out
    assert main(["update", "--check", "--version", "4.0.0"]) == 0
    assert sorted(installation.root.iterdir()) == before
    assert (installation.root / "current").resolve() == installation.release


@pytest.mark.parametrize("pinned", [False, True])
def test_current_version_is_not_complete_while_activation_is_locked(tmp_path, monkeypatch, capsys, pinned):
    installation = _installation(tmp_path, monkeypatch)
    pending = installation.root / "releases/pending"
    shutil.copytree(installation.release, pending)
    (pending / "release-manifest.json").write_text(json.dumps(_manifest(version="4.0.1")))
    current = installation.root / "current"
    current.unlink()
    current.symlink_to(pending)
    monkeypatch.setattr(updater, "latest_version", lambda _: "4.0.1")
    monkeypatch.setattr(updater, "download_bundle", lambda *a: pytest.fail("unexpected download"))
    arguments = ["update", "--version", "4.0.1"] if pinned else ["update"]
    with updater.update_lock(installation.root):
        assert main(arguments) == 1
        output = capsys.readouterr()
        assert "already running" in output.err
        assert "Already up to date" not in output.out
    assert main(arguments) == 0
    assert "Already up to date" in capsys.readouterr().out


@pytest.mark.parametrize("rolled_back", [False, True])
def test_version_is_refreshed_after_acquiring_the_activation_lock(tmp_path, monkeypatch, capsys, rolled_back):
    installation = _installation(tmp_path, monkeypatch)
    mirror, marker, _ = _bundle(tmp_path, installation)
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", mirror.as_uri())
    concurrent = installation.root / "releases/concurrent"
    shutil.copytree(installation.release, concurrent)
    (concurrent / "release-manifest.json").write_text(json.dumps(_manifest(version="4.0.1")))
    current = installation.root / "current"
    if rolled_back:
        current.unlink()
        current.symlink_to(concurrent)
    acquire = updater.acquire_install_lock
    def prior_activation_finishes(root):
        replacement = root / "next"
        replacement.symlink_to(installation.release if rolled_back else concurrent)
        replacement.replace(current)
        return acquire(root)
    monkeypatch.setattr(updater, "acquire_install_lock", prior_activation_finishes)
    assert main(["update", "--version", "4.0.1"]) == 0
    assert updater.read_installation(installation.root, "darwin", "arm64").manifest["product_version"] == "4.0.1"
    if rolled_back:
        assert len(json.loads(marker.read_text())) == 1
        assert "Already up to date" not in capsys.readouterr().out
    else:
        assert not marker.exists()
        assert "Already up to date" in capsys.readouterr().out


@pytest.mark.parametrize("version", ["../4.0.1", "4.0.1;id", "v4.0.1", "4.0.1-beta", "04.0.1", "3.0.0"])
def test_invalid_or_older_version_does_not_download(tmp_path, monkeypatch, version):
    _installation(tmp_path, monkeypatch)
    monkeypatch.setattr(updater, "download_bundle", lambda *a: pytest.fail("unexpected download"))
    assert main(["update", "--version", version]) == 1


@pytest.mark.parametrize("changes", [{"os": "linux"}, {"arch": "x86_64"}, {"role": "relay"}, {"product_version": "4.0.2"}, {"protocol_version": True}])
def test_mismatched_bundle_never_reaches_installer(tmp_path, monkeypatch, changes):
    installation = _installation(tmp_path, monkeypatch)
    mirror, marker, _ = _bundle(tmp_path, installation, changes=changes)
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", mirror.as_uri())
    assert main(["update", "--version", "4.0.1"]) == 1
    assert not marker.exists()
    assert (installation.root / "current").resolve() == installation.release


@pytest.mark.parametrize("problem", ["checksum", "duplicate-checksum", "checksum-encoding", "link", "../outside", "/absolute", "release-manifest.json"])
def test_tampered_or_unsafe_archive_is_rejected(tmp_path, monkeypatch, problem):
    installation = _installation(tmp_path, monkeypatch)
    unsafe = None if "checksum" in problem else problem
    mirror, marker, filename = _bundle(tmp_path, installation, unsafe=unsafe)
    checksum = mirror / "SHA256SUMS"
    if problem == "checksum":
        checksum.write_text(f"{'0' * 64}  {filename}\n")
    elif problem == "duplicate-checksum":
        checksum.write_text(checksum.read_text() * 2)
    elif problem == "checksum-encoding":
        checksum.write_bytes(b"\xff\xfe")
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", mirror.as_uri())
    assert main(["update", "--version", "4.0.1"]) == 1
    assert not marker.exists()


def test_missing_download_never_reaches_activation(tmp_path, monkeypatch):
    installation = _installation(tmp_path, monkeypatch)
    mirror, marker, filename = _bundle(tmp_path, installation)
    (mirror / filename).unlink()
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", mirror.as_uri())
    assert main(["update", "--version", "4.0.1"]) == 1
    assert not marker.exists()
    assert (installation.root / "current").resolve() == installation.release


def test_changed_installation_is_not_activated_after_download(tmp_path, monkeypatch):
    installation = _installation(tmp_path, monkeypatch)
    mirror, marker, _ = _bundle(tmp_path, installation)
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", mirror.as_uri())
    download = updater.download_bundle
    def changed(*args):
        bundle = download(*args)
        metadata = {**installation.metadata, "user": "another-user"}
        (installation.root / "installation.json").write_text(json.dumps(metadata))
        return bundle
    monkeypatch.setattr(updater, "download_bundle", changed)
    assert main(["update", "--version", "4.0.1"]) == 1
    assert not marker.exists()
    assert (installation.root / "current").resolve() == installation.release


def test_protocol_change_requires_explicit_coordinated_upgrade(tmp_path, monkeypatch):
    installation = _installation(tmp_path, monkeypatch)
    mirror, marker, _ = _bundle(tmp_path, installation, changes={"protocol_version": 73})
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", mirror.as_uri())
    assert main(["update", "--version", "4.0.1"]) == 1
    assert not marker.exists()
    assert main(["update", "--version", "4.0.1", "--allow-protocol-change"]) == 0
    assert len(json.loads(marker.read_text())) == 1


def test_sdk_change_does_not_restart_an_independent_service(tmp_path, monkeypatch):
    installation = _installation(tmp_path, monkeypatch)
    (installation.release / "requirements-wrapper.lock").write_text("claude-agent-sdk==0.2.150\n")
    mirror, marker, _ = _bundle(tmp_path, installation)
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", mirror.as_uri())
    assert main(["update", "--version", "4.0.1", "--allow-protocol-change"]) == 1
    assert not marker.exists()


def test_installer_failure_is_not_retried_or_reported_as_success(tmp_path, monkeypatch):
    installation = _installation(tmp_path, monkeypatch)
    mirror, marker, _ = _bundle(tmp_path, installation, fail=True)
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", mirror.as_uri())
    assert main(["update", "--version", "4.0.1"]) == 1
    assert len(json.loads(marker.read_text())) == 1
    assert (installation.root / "current").resolve() == installation.release


def test_interrupt_waits_for_installer_rollback_instead_of_killing_it(monkeypatch):
    calls = []
    class Installer:
        def wait(self):
            calls.append("wait")
            if len(calls) == 1:
                raise KeyboardInterrupt
            return 130
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *args, **kwargs: Installer())
    assert updater.run_installer(["bash", "installer.sh"], 123) == 130
    assert calls == ["wait", "wait"]


def test_update_lock_is_exclusive_and_survives_holder_failure(tmp_path):
    with updater.update_lock(tmp_path):
        with pytest.raises(updater.UpdateError, match="already running"):
            with updater.update_lock(tmp_path):
                pytest.fail("second updater entered")
    with updater.update_lock(tmp_path):
        pass


def test_installer_keeps_lock_if_the_controller_disconnects(tmp_path):
    with updater.update_lock(tmp_path) as descriptor:
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read(1)"],
            stdin=subprocess.PIPE, pass_fds=(descriptor,),
        )
    try:
        with pytest.raises(updater.UpdateError, match="already running"):
            with updater.update_lock(tmp_path):
                pytest.fail("controller exit released the active installer lock")
    finally:
        child.communicate(b"x", timeout=5)
    with updater.update_lock(tmp_path):
        pass


def test_multiple_roles_need_an_explicit_selection(tmp_path, monkeypatch):
    relay = _installation(tmp_path, monkeypatch, role="relay", system="linux")
    wrapper = _installation(tmp_path, monkeypatch, system="linux")
    monkeypatch.setattr(updater, "installation_roots", lambda _: {"relay": relay.root, "wrapper": wrapper.root})
    with pytest.raises(updater.UpdateError, match="both roles"):
        updater.select_installation(None, "linux", "arm64")
    assert updater.select_installation("relay", "linux", "arm64") == relay
    assert updater.select_installation("wrapper", "linux", "arm64") == wrapper


@pytest.mark.parametrize("selected_role", ["relay", "wrapper"])
@pytest.mark.parametrize("problem", ["metadata", "current", "manifest"])
def test_explicit_role_ignores_an_unrelated_broken_installation(tmp_path, monkeypatch, selected_role, problem):
    installations = {
        role: _installation(tmp_path, monkeypatch, role=role, system="linux")
        for role in ("relay", "wrapper")
    }
    monkeypatch.setattr(updater, "installation_roots", lambda _: {
        role: installation.root for role, installation in installations.items()
    })
    other_role = "wrapper" if selected_role == "relay" else "relay"
    broken = installations[other_role]
    if problem == "metadata":
        (broken.root / "installation.json").write_text("{invalid metadata")
    elif problem == "current":
        (broken.root / "current").unlink()
        (broken.root / "current").symlink_to(broken.root / "releases/missing")
    else:
        (broken.release / "release-manifest.json").unlink()

    assert updater.select_installation(selected_role, "linux", "arm64") == installations[selected_role]
    assert main(["update", "--check", "--role", selected_role, "--version", "4.0.1"]) == 0
    for role in (other_role, None):
        with pytest.raises(updater.UpdateError, match="cannot read managed installation"):
            updater.select_installation(role, "linux", "arm64")


def test_selected_managed_root_cannot_impersonate_another_role(tmp_path, monkeypatch):
    wrapper = _installation(tmp_path, monkeypatch, system="linux")
    monkeypatch.setattr(updater, "installation_roots", lambda _: {"relay": wrapper.root})
    with pytest.raises(updater.UpdateError, match="role does not match"):
        updater.select_installation("relay", "linux", "arm64")


@pytest.fixture
def linux_launcher(tmp_path):
    managed = tmp_path / "managed roots"
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "uname").write_text("#!/bin/sh\necho Linux\n")
    (stubs / "id").write_text("#!/bin/sh\necho 0\n")
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    releases = {}
    for role, directory in (("relay", "cc-remote"), ("wrapper", "cc-remote-wrapper")):
        release = managed / directory / "release"
        (release / ".venv/bin").mkdir(parents=True)
        runtime = release / ".venv/bin/python"
        runtime.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n")
        runtime.chmod(0o755)
        package = release / "cc_remote"
        package.mkdir()
        (package / "__init__.py").touch()
        (package / "__main__.py").write_text(
            f"import json, sys\nprint(json.dumps({{'role': {role!r}, 'argv': sys.argv[1:]}}))\n")
        (release.parent / "current").symlink_to(release)
        releases[role] = release
    script = tmp_path / "cc-remote"
    script.write_text((ROOT / "scripts/cc-remote").read_text().replace(
        "/opt/cc-remote", str(managed / "cc-remote")))
    env = {**os.environ, "PATH": f'{stubs}:{os.environ["PATH"]}'}
    return ["bash", str(script)], env, releases


@pytest.mark.parametrize("role", ["relay", "wrapper"])
@pytest.mark.parametrize("option", ["--role", "--role=", "--ro", "--ro="])
def test_launcher_selects_requested_runtime_before_loading_python(linux_launcher, role, option):
    command, env, releases = linux_launcher
    other = "wrapper" if role == "relay" else "relay"
    (releases[other] / "cc_remote/__main__.py").write_text("raise ImportError('broken unrelated runtime')\n")
    selection = [f"{option}{role}"] if option.endswith("=") else [option, role]
    arguments = ["update", "--check", *selection]
    result = subprocess.run(command + arguments, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"role": role, "argv": arguments}


@pytest.mark.parametrize("role", ["relay", "wrapper"])
def test_launcher_does_not_fall_back_when_requested_runtime_is_missing(linux_launcher, role):
    command, env, releases = linux_launcher
    (releases[role] / ".venv/bin/python").unlink()
    result = subprocess.run(command + ["update", "--role", role], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "No managed cc-remote release found" in result.stderr


def test_launcher_uses_last_explicit_role_without_consuming_arguments(linux_launcher):
    command, env, _releases = linux_launcher
    arguments = ["update", "--role", "wrapper", "--role=relay", "--check"]
    result = subprocess.run(command + arguments, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"role": "relay", "argv": arguments}


@pytest.mark.parametrize("selection", [["--role"], ["--role="], ["--role", "invalid"]])
def test_launcher_rejects_missing_or_invalid_role(linux_launcher, selection):
    command, env, _releases = linux_launcher
    result = subprocess.run(command + ["update", *selection], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "--role requires relay or wrapper" in result.stderr


def test_custom_installation_is_not_adopted(tmp_path, monkeypatch):
    installation = _installation(tmp_path, monkeypatch)
    (installation.root / "installation.json").unlink()
    with pytest.raises(updater.UpdateError, match="no managed"):
        updater.select_installation(None, "darwin", "arm64")
    assert (installation.root / "current").resolve() == installation.release


@pytest.mark.parametrize("payload", [
    {"tag_name": "v4.0.2", "draft": False, "prerelease": False},
    {"tag_name": "v4.0.2-beta", "draft": False, "prerelease": True},
    {"tag_name": "v4.0.2", "draft": True, "prerelease": False},
    {"tag_name": "v../bad", "draft": False, "prerelease": False},
    [],
])
def test_latest_version_only_accepts_a_stable_release(monkeypatch, payload):
    monkeypatch.setattr(updater, "urlopen", lambda *a, **k: io.BytesIO(json.dumps(payload).encode()))
    if isinstance(payload, dict) and payload.get("tag_name") == "v4.0.2" and not payload.get("draft"):
        assert updater.latest_version("example/repository") == "4.0.2"
    else:
        with pytest.raises(updater.UpdateError):
            updater.latest_version("example/repository")


def test_rate_limited_release_lookup_has_an_actionable_error(monkeypatch):
    def limited(*args, **kwargs):
        raise HTTPError("https://api.github.com", 403, "rate limited", {}, None)
    monkeypatch.setattr(updater, "urlopen", limited)
    with pytest.raises(updater.UpdateError, match="HTTP 403.*--version"):
        updater.latest_version("example/repository")


@pytest.mark.parametrize("value", ["http://example.test/releases", "https://user:secret@example.test", "https://example.test?token=x", "file://other-host/tmp"])
def test_untrusted_release_url_is_rejected(monkeypatch, value):
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", value)
    with pytest.raises(updater.UpdateError):
        updater.release_base("example/repository", "4.0.1")


def test_wrapper_child_cannot_be_its_own_updater(monkeypatch):
    monkeypatch.setattr(updater.os, "getppid", lambda: 456)
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="1 /release/.venv/bin/python -m cc_remote.wrapper\n"))
    with pytest.raises(updater.UpdateError, match="independent terminal"):
        updater.require_independent_terminal()


def test_independent_terminal_is_accepted(monkeypatch):
    monkeypatch.setattr(updater.os, "getppid", lambda: 456)
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="1 /bin/zsh\n"))
    updater.require_independent_terminal()


def test_cli_registration_is_atomic_and_preserves_unrelated_commands(tmp_path, monkeypatch):
    installation = _installation(tmp_path, monkeypatch)
    source = installation.release / "bin/cc-remote"
    source.parent.mkdir()
    source.write_bytes((ROOT / "scripts/cc-remote").read_bytes())
    destination = tmp_path / "local/bin/cc-remote"
    install_cli(installation.root, destination, role="wrapper", user="service-user")
    assert destination.read_bytes() == source.read_bytes()
    assert os.access(destination, os.X_OK)
    metadata = json.loads((installation.root / "installation.json").read_text())
    assert metadata == {"schema": 1, "role": "wrapper", "user": "service-user"}
    install_cli(installation.root, destination, role="wrapper", user="service-user")
    destination.write_text("#!/bin/sh\necho user-command\n")
    with pytest.raises(ValueError, match="unrelated"):
        install_cli(installation.root, destination, role="wrapper", user="service-user")
    assert destination.read_text() == "#!/bin/sh\necho user-command\n"
    destination.unlink()
    destination.symlink_to(source)
    with pytest.raises(ValueError, match="symlink"):
        check_destination(destination)


def test_failed_registration_restores_the_previous_command(tmp_path, monkeypatch):
    installation = _installation(tmp_path, monkeypatch)
    source = installation.release / "bin/cc-remote"
    source.parent.mkdir()
    source.write_bytes((ROOT / "scripts/cc-remote").read_bytes())
    destination = tmp_path / "bin/cc-remote"
    install_cli(installation.root, destination, role="wrapper", user="service-user")
    previous = destination.read_bytes()
    source.write_bytes(previous + b"\n# new release\n")
    atomic = cli_installer._atomic_file
    def fail_metadata(target, content, mode):
        if target.name == "installation.json":
            raise OSError("injected disk failure")
        atomic(target, content, mode)
    monkeypatch.setattr(cli_installer, "_atomic_file", fail_metadata)
    with pytest.raises(OSError, match="disk failure"):
        install_cli(installation.root, destination, role="wrapper", user="service-user")
    assert destination.read_bytes() == previous


def test_macos_upgrade_preserves_operator_environment_and_service_socket(tmp_path):
    # Execute the installer's actual plist-rendering program against private fixtures.
    script = (ROOT / "deploy/install-wrapper.sh").read_text()
    marker = '"$service_backup" <<\'PY\'\n'
    program = script.split(marker, 1)[1].split("\nPY\n", 1)[0]
    prior = tmp_path / "previous.plist"
    environment = {
        "CC_REMOTE_CLAUDE_SERVICE_SOCKET": "/private/example/service.sock",
        "CC_REMOTE_CLAUDE_PROFILES_FILE": "/private/example/accounts.json",
        "CLAUDE_WORK_ROOT": "/private/example/work",
        "LOG_LEVEL": "DEBUG",
    }
    prior.write_bytes(plistlib.dumps({"EnvironmentVariables": environment}))
    destination = tmp_path / "new.plist"
    subprocess.run([
        sys.executable, "-", str(ROOT / "deploy/com.muggle.cc-remote.wrapper.plist.in"),
        str(destination), str(tmp_path / "current"), str(tmp_path / "home"),
        str(tmp_path / "logs"), str(prior),
    ], input=program, text=True, check=True)
    result = plistlib.loads(destination.read_bytes())
    assert all(result["EnvironmentVariables"][key] == value for key, value in environment.items())
    assert result["ProgramArguments"][0] == str(tmp_path / "current/.venv/bin/python")
