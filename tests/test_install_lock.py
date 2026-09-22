"""Exercise the real Wrapper entrypoint with inert platform/bootstrap fixtures."""
from __future__ import annotations

import os
from pathlib import Path
import select
import shutil
import subprocess
import sys

import pytest

from cc_remote import update as updater
from deploy.install_lock import LOCK_FD_ENV, InstallLockError, acquire_install_lock, verify_install_lock

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def wrapper_install(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "bin").mkdir(parents=True)
    (bundle / "deploy").mkdir()
    (bundle / "requirements-wrapper.lock").touch()
    (bundle / "release-manifest.json").write_text(
        '{"python":"3.13.9","product_version":"4.0.1","git_sha":"' + "a" * 40 + '"}')
    for filename in ("install-wrapper.sh", "install_lock.py"):
        shutil.copyfile(ROOT / "deploy" / filename, bundle / "deploy" / filename)
    uv = bundle / "bin/uv"
    uv.write_text(f"""#!{sys.executable}
import os, pathlib, sys
args = sys.argv[sys.argv.index('python') + 1:]
if args[:2] == ['-m', 'deploy.release_manifest']:
    raise SystemExit(0)
if pathlib.Path(args[0]).name == 'install_lock.py':
    os.execv(sys.executable, [sys.executable, *args])
if pathlib.Path(args[0]).name == 'install_cli.py':
    pathlib.Path(os.environ['TEST_ENTERED']).write_text('entered')
    print('entered', flush=True)
    if os.environ.get('TEST_HOLD') == '1':
        sys.stdin.read(1)
    raise SystemExit(17)  # Stop before touching any actual service or account.
raise AssertionError(args)
""")
    uv.chmod(0o755)
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "uname").write_text('#!/bin/sh\ncase "$1" in -s) echo Darwin;; -m) echo arm64;; esac\n')
    (stubs / "id").write_text('#!/bin/sh\ncase "$1" in -u) echo 501;; -un) echo fixture-user;; esac\n')
    for path in stubs.iterdir():
        path.chmod(0o755)
    home = tmp_path / "user home"
    home.mkdir()
    install_root = home / "Library/Application Support/cc-remote"
    install_root.mkdir(parents=True)
    marker = tmp_path / "protected-phase"
    env = {**os.environ, "HOME": str(home), "PATH": f'{stubs}:{os.environ["PATH"]}',
           "TEST_ENTERED": str(marker)}
    env.pop(LOCK_FD_ENV, None)
    command = ["bash", str(bundle / "deploy/install-wrapper.sh"), str(bundle)]
    return install_root, marker, env, command


def test_direct_wrapper_install_cannot_enter_during_managed_update(wrapper_install):
    root, marker, env, command = wrapper_install
    with updater.update_lock(root):
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "already running" in result.stderr
    assert not marker.exists()


def test_managed_update_and_second_install_cannot_enter_direct_install(wrapper_install):
    root, marker, env, command = wrapper_install
    process = subprocess.Popen(command, env={**env, "TEST_HOLD": "1"}, text=True,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert select.select([process.stdout], [], [], 10)[0], "installer never reached protected phase"
        assert process.stdout.readline().strip() == "entered"
        assert marker.exists()
        with pytest.raises(updater.UpdateError, match="already running"):
            with updater.update_lock(root):
                pytest.fail("managed update entered a direct installation")
        second = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
        assert second.returncode != 0 and "already running" in second.stderr
        process.communicate("x", timeout=10)
        assert process.returncode == 17
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    with updater.update_lock(root):
        pass


def test_wrapper_accepts_updater_descriptor_without_releasing_parent_lock(wrapper_install, monkeypatch):
    root, marker, env, command = wrapper_install
    for key in ("HOME", "PATH", "TEST_ENTERED"):
        monkeypatch.setenv(key, env[key])
    with updater.update_lock(root) as descriptor:
        assert updater.run_installer(command, descriptor) == 17
        assert marker.exists()
        with pytest.raises(updater.UpdateError, match="already running"):
            with updater.update_lock(root):
                pytest.fail("the child released the parent's lock")


def test_inherited_marker_for_another_file_cannot_bypass_wrapper_lock(wrapper_install, tmp_path):
    _root, marker, env, command = wrapper_install
    other = os.open(tmp_path / "other", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        result = subprocess.run(command, env={**env, LOCK_FD_ENV: str(other)},
                                pass_fds=(other,), capture_output=True, text=True, timeout=10)
    finally:
        os.close(other)
    assert result.returncode != 0
    assert not marker.exists()


def test_lock_rejects_symlinks_and_wrong_inherited_file(tmp_path):
    path = tmp_path / ".update.lock"
    target = tmp_path / "outside"
    target.write_text("preserve")
    path.symlink_to(target)
    with pytest.raises(OSError):
        acquire_install_lock(tmp_path)
    assert target.read_text() == "preserve"
    path.unlink()
    descriptor = acquire_install_lock(tmp_path)
    try:
        with target.open("rb") as other:
            with pytest.raises(InstallLockError, match="does not match"):
                verify_install_lock(tmp_path, other.fileno())
        with pytest.raises(InstallLockError, match="invalid inherited"):
            verify_install_lock(tmp_path, 0)
    finally:
        os.close(descriptor)
