"""Real local sockets prove health of a pre-update_status Wrapper process."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from cc_remote import activation
from cc_remote.wrapper.local_control import LocalControl
from cc_remote.wrapper import process_scan
from deploy import activation as installer, release_retention
from deploy.install_lock import acquire_install_lock, LOCK_FD_ENV
from tests.test_activation_recovery import record, IDENTITY
from tests.test_ui_updates import managed as managed


# The previous LocalControl._client returns this exact rejection for the new
# read-only operation. Run it under the real managed Wrapper argv and cwd so
# health must identify the kernel socket peer, not trust a fabricated JSON PID.
LEGACY_SERVER = '''
import json, os, socket
from pathlib import Path
path = Path(os.environ["TEST_CONTROL"])
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(str(path)); os.chmod(path, 0o600); server.listen()
print("ready", flush=True)
while True:
    peer, _ = server.accept()
    with peer:
        with peer.makefile("rb") as stream:
            request = json.loads(stream.readline())
        with Path(os.environ["TEST_REQUESTS"]).open("a") as output:
            output.write(json.dumps(request) + "\\n")
        result = json.loads(os.environ.get("TEST_RESPONSE", '{"status":"rejected","error":"Unknown control request"}'))
        peer.sendall(json.dumps(result).encode() + b"\\n")
'''


@contextmanager
def legacy_wrapper(release, *, cwd=None, own_interpreter=True, module=True, response=None):
    folder = release / ".venv/bin"
    folder.mkdir(parents=True, exist_ok=True)
    executable = folder / "python"
    if not executable.exists():
        executable.symlink_to(sys.executable)
    cwd = cwd or release
    package = cwd / "cc_remote"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").touch()
    (package / "wrapper.py").write_text(LEGACY_SERVER)
    with tempfile.TemporaryDirectory(prefix="ccr-legacy-", dir="/tmp") as short:
        control = Path(short) / "control.sock"
        requests = Path(short) / "requests.jsonl"
        env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
        env.update(TEST_CONTROL=str(control), TEST_REQUESTS=str(requests))
        if response is not None:
            env["TEST_RESPONSE"] = json.dumps(response)
        args = [str(executable) if own_interpreter else sys.executable]
        args += ["-s", "-m", "cc_remote.wrapper"] if module else ["-c", LEGACY_SERVER]
        child = subprocess.Popen(args, cwd=cwd, env=env, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            assert child.stdout.readline().strip() == "ready"
            yield {"control_socket": str(control), "uid": os.getuid()}, child, requests
        finally:
            child.terminate()
            child.communicate(timeout=5)


def test_legacy_read_only_health_is_bound_to_the_real_release(tmp_path):
    previous = tmp_path / "previous release"
    previous.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    with legacy_wrapper(previous) as (value, child, requests):
        assert installer.health(value, str(previous))
        assert not installer.health(value, str(other))
        assert not installer.health({**value, "uid": os.getuid() + 1}, str(previous))
        assert child.poll() is None
        assert all(json.loads(line) == {"op": "update_status"}
                   for line in requests.read_text().splitlines())


@pytest.mark.parametrize("mismatch", ["cwd", "interpreter", "module", "response", "claimed-pid"])
def test_unknown_or_unrelated_socket_cannot_prove_a_legacy_rollback(tmp_path, mismatch):
    previous = tmp_path / "previous"
    previous.mkdir()
    kwargs = {}
    if mismatch == "cwd":
        kwargs["cwd"] = tmp_path / "other"
    if mismatch == "interpreter":
        kwargs["own_interpreter"] = False
    if mismatch == "module":
        kwargs["module"] = False
    if mismatch == "response":
        kwargs["response"] = {"status": "unknown"}
    if mismatch == "claimed-pid":
        kwargs["response"] = {"status": "ready", "release": str(previous),
                              "process": {"pid": 1, "start_ticks": 1}}
    with legacy_wrapper(previous, **kwargs) as (value, _, _):
        assert not installer.health(value, str(previous))


def test_process_replacement_during_legacy_health_is_rejected(tmp_path, monkeypatch):
    previous = tmp_path / "previous"
    previous.mkdir()
    with legacy_wrapper(previous) as (value, child, _):
        identity = process_scan.process_identity(child.pid)
        assert identity is not None
        reads = iter([identity, process_scan.ProcessIdentity(identity.pid, identity.start_ticks + 1)])
        monkeypatch.setattr(process_scan, "process_identity", lambda pid: next(reads))
        assert not installer.health(value, str(previous))


@pytest.mark.asyncio
async def test_current_wrapper_health_confirms_the_socket_peer():
    with tempfile.TemporaryDirectory(prefix="ccr-health-", dir="/tmp") as short:
        local = LocalControl(SimpleNamespace(cfg=SimpleNamespace(state_dir=Path(short))))
        await local.start()
        try:
            import asyncio
            value = {"control_socket": str(local.socket), "uid": os.getuid()}
            release = str(Path(__file__).resolve().parents[1])
            assert await asyncio.to_thread(installer.health, value, release)
            assert not await asyncio.to_thread(installer.health, value, short)
        finally:
            await local.close()


def test_restored_legacy_wrapper_finalizes_original_recovery(managed, monkeypatch):
    root, previous, store = managed
    config = root / "service.conf"
    config.write_text("previous service")
    with legacy_wrapper(previous) as (peer, _, requests):
        value = record(root, previous, phase="restored", ui_job=IDENTITY, **peer)
        generation = release_retention.begin(root, Path(value["release"]), str(previous),
            role="wrapper", home=root, service="fixture", service_file=config, configs=[])
        activation.save(root, {**value, "generation": generation})
        state, _ = store.create(IDENTITY, "4.0.1", "4.0.0")
        store.save({**state, "phase": "unknown"})
        monkeypatch.setattr(installer, "service", lambda *args: pytest.fail("already restored; do not restart"))
        fd = acquire_install_lock(root)
        monkeypatch.setenv(LOCK_FD_ENV, str(fd))
        try:
            assert installer.recover(root, IDENTITY) == "failed"
            assert activation.receipt(root)["phase"] == "rolled_back"
            assert release_retention.generation(root, generation)[1]["phase"] == "aborted"
            assert store.state(IDENTITY)["reason"] == "rolled_back"
            assert not activation.guarded(root)
            # A retry checks the same finished receipt without recreating work.
            assert installer.recover(root, IDENTITY) == "failed"
            assert all(json.loads(line) == {"op": "update_status"}
                       for line in requests.read_text().splitlines())
        finally:
            os.close(fd)
