"""Server maintenance stays independent of selected devices and native engines."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
import os
from pathlib import Path
import pwd
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

from cc_remote import installation_updates, update as updater
from cc_remote.relay import server, server_updates
from cc_remote.update_jobs import JOBS, POLICY, UpdateJobs, write_json
from deploy import ui_update_job as launcher, ui_update_broker as broker
from tests.test_devices import _cfg
from tests.test_update import _manifest

ID = "a" * 32
native_installation = launcher.installation


@pytest.fixture
def managed_server(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "relay"
    release = root / "releases/original"
    release.mkdir(parents=True)
    (root / "current").symlink_to(release)
    (release / "release-manifest.json").write_text(json.dumps(_manifest("relay", "linux")))
    (root / "installation.json").write_text(json.dumps({"schema": 1, "role": "relay", "domain": "remote.example.test"}))
    account = pwd.getpwuid(os.getuid())
    current = updater.read_installation(root, "linux", "arm64")
    monkeypatch.setattr(launcher, "installation", lambda _: (current, account))
    monkeypatch.setattr(launcher, "require_job_manager", lambda _: None)
    monkeypatch.setattr(broker, "install", lambda *_: None)
    for module in (launcher, installation_updates, server_updates):
        monkeypatch.setattr(module, "host_platform", lambda: ("linux", "arm64"))
    # run() normally lives in an isolated OS worker. Restore its environment
    # when exercising it in-process, so later Wrapper tests keep native policy.
    for name in ("CC_REMOTE_UI_UPDATE_ID", "CC_REMOTE_UI_ALLOW_RELAY_UPDATE",
                 "CC_REMOTE_GITHUB_REPOSITORY", "CC_REMOTE_RELEASE_BASE_URL"):
        # Record even an originally absent key before run() writes it directly.
        monkeypatch.setenv(name, os.environ.get(name, ""))
        monkeypatch.delenv(name)
    launcher.configure(root, enabled=True)
    # status only checks reachability; invocation is replaced with the real
    # broker dispatch below, with OS submission stubbed at its final boundary.
    (root / "ui-update.sock").touch()
    return root, release, account


def test_server_policy_uses_its_own_uid_and_no_wrapper_or_ssh_permission(managed_server):
    root, _, account = managed_server
    policy = UpdateJobs(root).policy()
    assert policy["role"] == "relay" and policy["uid"] == account.pw_uid
    assert policy["control_socket"] is None and not policy["allow_relay_update"]
    for args in ({"allow_relay": True}, {"control_socket": Path("/tmp/wrapper.sock")}):
        with pytest.raises(ValueError, match="do not use"):
            launcher.configure(root, enabled=True, **args)
    launcher.configure(root, enabled=False)
    assert not UpdateJobs(root).policy()["enabled"]


@pytest.mark.asyncio
async def test_server_request_survives_reconnect_and_observers_share_receipt(managed_server, monkeypatch):
    root, _, account = managed_server
    launches = []
    monkeypatch.setattr(launcher, "spawn_worker", lambda *args: launches.append(args))
    first = server_updates.ServerUpdates(root)
    second = server_updates.ServerUpdates(root)
    for instance in (first, second):
        monkeypatch.setattr(instance, "invoke", lambda op, identity, version=None:
                            broker.dispatch(root, {"op": op, "id": identity, **({"version": version} if version else {})}, account.pw_uid))
    request = {"op": "start", "job_id": ID, "version": "4.0.1"}
    a, b = await asyncio.gather(first.handle(request), second.handle(request))
    assert a["job"]["id"] == b["job"]["id"] == ID and len(launches) == 1
    # A fresh client, including after the Relay process restarts, reads the
    # original disk receipt without any selected/online Wrapper.
    status = await second.status()
    assert status["enabled"] and status["job"]["id"] == ID
    assert status["maintenance"] is False  # No local conversation admission barrier.
    assert (await second.handle({"op": "lookup", "job_id": ID}))["receipt"] == "found"
    conflict = await second.handle({**request, "job_id": "b" * 32})
    assert conflict["ok"] is False and len(launches) == 1


def test_server_worker_uses_relay_installer_and_original_os_receipt(managed_server, monkeypatch):
    root, release, account = managed_server
    calls = []
    monkeypatch.setattr(launcher.subprocess, "run", lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0))
    launcher.launch(root, ID, "4.0.1")
    assert calls[0][0] == "/usr/bin/systemd-run" and str(release / "deploy/ui_update_job.py") in calls[0]
    assert calls[0][-2:] == ["run", ID]
    installed = []
    monkeypatch.setattr(launcher, "update", lambda **kw: installed.append(kw))
    launcher.run(root, ID)
    assert installed[0]["role"] == "relay" and installed[0]["target_version"] == "4.0.1"
    assert not installed[0]["allow_relay_update"]
    assert UpdateJobs(root).state(ID)["phase"] == "succeeded"
    launcher.launch(root, ID, "4.0.1")
    assert len(calls) == len(installed) == 1
    with pytest.raises(ValueError, match="permission"):
        broker.dispatch(root, {"op": "start", "id": ID, "version": "4.0.1"}, account.pw_uid + 1)


def test_server_configure_checks_root_without_requiring_service_account_sudo(managed_server, monkeypatch):
    root, _, account = managed_server
    from deploy import linux_service
    monkeypatch.setattr(linux_service, "trusted", lambda _: None)
    monkeypatch.setattr(launcher.pwd, "getpwnam", lambda name: account if name == "ccremote" else pytest.fail(name))
    monkeypatch.setenv("SUDO_UID", str(account.pw_uid + 100))
    monkeypatch.setattr(launcher.os, "geteuid", lambda: 0)
    value, observed = native_installation(root)
    assert value.role == "relay" and observed == account
    monkeypatch.setattr(launcher.os, "geteuid", lambda: 501)
    with pytest.raises(ValueError, match="authorization"):
        native_installation(root)


def test_server_lost_os_launch_ack_is_never_resubmitted(managed_server, monkeypatch):
    root, _, _ = managed_server
    calls = []

    def timeout(argv, **_kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, 20)

    monkeypatch.setattr(launcher.subprocess, "run", timeout)
    assert launcher.launch(root, ID, "4.0.1")["phase"] == "unknown"
    assert launcher.launch(root, ID, "4.0.1")["phase"] == "unknown"
    assert len(calls) == 1


def test_server_failure_before_activation_uses_original_health_origin(managed_server, monkeypatch):
    from cc_remote import activation
    from deploy import activation as installer
    root, release, _ = managed_server
    store = UpdateJobs(root)
    state, _ = store.create(ID, "4.0.1", "4.0.0")
    store.save({**state, "phase": "preparing", "attempt_started": True, "original_release": str(release)})
    monkeypatch.setattr(activation, "receipt", lambda _root, identity=None:
                        {"role": "relay", "origin": "http://remote.example.test"} if identity is None else None)
    monkeypatch.setattr(activation, "install_locked", lambda _: False)

    def healthy(probe, current):
        assert probe == {"role": "relay", "origin": "http://remote.example.test"}
        assert current == str(release)
        return True

    monkeypatch.setattr(installer, "health", healthy)
    result = launcher.reconcile(root, ID)
    assert result["phase"] == "failed" and result["reason"] == "worker_exited"


@pytest.mark.asyncio
async def test_server_status_without_authorization_does_not_enable_it(managed_server, monkeypatch):
    root, _, _ = managed_server
    (root / POLICY).unlink()
    checks = []
    monkeypatch.setattr(server_updates, "release_catalog", lambda repo: checks.append(repo) or {"version": "4.0.1", "notes": "fix"})
    updates = server_updates.ServerUpdates(root)
    status = await updates.status(check=True)
    assert status["reason"] == "server_setup_required" and not status["enabled"]
    assert status["latest"]["version"] == "4.0.1" and len(checks) == 1
    assert not (root / POLICY).exists()
    assert (await updates.handle({"op": "start", "job_id": ID, "version": "4.0.1"}))["ok"] is False
    assert not (root / JOBS / f"{ID}.json").exists()


@pytest.mark.asyncio
async def test_server_refuses_wrapper_policy_binding(managed_server):
    root, _, _ = managed_server
    policy = UpdateJobs(root).policy()
    write_json(root / POLICY, {**policy, "role": "wrapper", "control_socket": "/tmp/wrapper.sock"})
    assert not (await server_updates.ServerUpdates(root).status())["enabled"]
    with pytest.raises(ValueError, match="permission"):
        broker.dispatch(root, {"op": "start", "id": ID, "version": "4.0.1"}, policy["uid"])


@pytest.mark.asyncio
async def test_server_maintenance_requests_are_bounded(managed_server, monkeypatch):
    updates = server_updates.ServerUpdates(managed_server[0])
    wait = asyncio.Event()

    async def slow_status(**_kwargs):
        await wait.wait()
        return {"enabled": True}

    monkeypatch.setattr(updates, "status", slow_status)
    pending = [asyncio.create_task(updates.handle({"op": "status"})) for _ in range(8)]
    await asyncio.sleep(0)
    assert await updates.handle({"op": "status"}) == {"ok": False, "reason": "busy"}
    wait.set()
    await asyncio.gather(*pending)
    assert updates.inflight == 0


def test_server_endpoint_requires_global_login_and_exact_origin(tmp_path):
    server._login_limiter.reset()
    cfg = _cfg(tmp_path, login_users_json=json.dumps({
        "owner": {"password": "global-password", "machines": ["*"]},
        "device": {"password": "device-password", "machines": ["device-a"]},
    }))
    app = server.create_app(cfg)
    handle = app.state.server_updates.handle = AsyncMock(return_value={"schema": 1, "enabled": True})
    headers = {"Origin": cfg.public_origin}
    with TestClient(app, base_url=cfg.public_origin) as client:
        assert client.get("/api/server-updates").status_code == 401
        assert client.post("/api/login", json={"username": "device", "password": "device-password"}).status_code == 200
        assert client.get("/api/server-updates?machine_id=device-a").status_code == 403
        assert client.post("/api/server-updates", headers=headers, json={"op": "check"}).status_code == 403
        handle.assert_not_awaited()
        assert client.post("/api/login", json={"username": "owner", "password": "global-password"}).status_code == 200
        assert client.post("/api/server-updates", json={"op": "check"}).status_code == 403
        for command in ({"op": "start", "job_id": ID, "version": "4.0.1", "machine_id": "device-a"},
                        {"op": "start", "job_id": ID, "version": "$(id)"}, {"op": "shell"}):
            assert client.post("/api/server-updates", headers=headers, json=command).status_code == 400
        result = client.get("/api/server-updates")
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        assert result.json()["relay_version"]
        handle.assert_awaited_once_with({"op": "status"})
        # No maintenance Wrapper channel exists; selecting another machine
        # cannot affect the global update request.
        assert not app.state.updates.peers
        assert client.post("/api/server-updates", headers=headers, json={"op": "lookup", "job_id": ID}).status_code == 200


def test_server_update_rechecks_revoked_login_after_pending_operation(tmp_path):
    server._login_limiter.reset()
    cfg = _cfg(tmp_path)
    app = server.create_app(cfg)
    from threading import Event
    entered, finish = Event(), Event()

    async def pending(_command):
        entered.set()
        await asyncio.to_thread(finish.wait, 3)
        return {"schema": 1}

    app.state.server_updates.handle = pending
    with TestClient(app, base_url=cfg.public_origin) as client, ThreadPoolExecutor(1) as pool:
        client.post("/api/login", json={"password": cfg.login_password})
        result = pool.submit(client.get, "/api/server-updates")
        assert entered.wait(3)
        client.post("/api/logout", headers={"Origin": cfg.public_origin})
        finish.set()
        assert result.result(timeout=3).status_code == 401
