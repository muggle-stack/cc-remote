"""UI updates use isolated filesystem receipts and stub OS launchers, no models."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import plistlib
import pwd
import shlex
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from cc_remote import update as updater
from cc_remote import update_relay
from cc_remote.update_jobs import JOBS, POLICY, UpdateJobs, public_job
from cc_remote.wrapper.updates import UIUpdates
from cc_remote.wrapper import updates as device_updates
from cc_remote.relay import server
from cc_remote.relay.updates import UpdateRelay, Peer, UpdateChannelError
from deploy import ui_update_job as launcher
from tests.test_devices import _cfg
from tests.test_multisession import _mk_machine, _mk_ctx
from tests.test_update import _manifest, _installation, _bundle

IDENTITY = "a" * 32


@pytest.fixture(autouse=True)
def isolated_login_limiter():
    server._login_limiter.reset()
    yield
    server._login_limiter.reset()


@pytest.fixture
def managed(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "managed"
    release = root / "releases/current-build"
    release.mkdir(parents=True)
    (root / "current").symlink_to(release)
    (release / "release-manifest.json").write_text(json.dumps(_manifest(version="4.0.0")))
    user = pwd.getpwuid(os.geteuid())
    (root / "installation.json").write_text(json.dumps({"schema": 1, "role": "wrapper", "user": user.pw_name}))
    monkeypatch.setattr(launcher, "host_platform", lambda: ("darwin", "arm64"))
    monkeypatch.setattr(device_updates, "host_platform", lambda: ("darwin", "arm64"))
    monkeypatch.setattr(launcher, "latest_version", lambda repo: "4.0.1")
    monkeypatch.setenv("CC_REMOTE_GITHUB_REPOSITORY", "fixture/releases")
    launcher.configure(root, enabled=True, control_socket=Path("/tmp/fixture-ui-update.sock"))
    return root, release, UpdateJobs(root)


def test_os_job_launch_is_durable_and_replays_never_resubmit(managed, monkeypatch):
    root, release, store = managed
    calls = []
    def submit(argv, **kwargs):
        # Intent is on disk before launchctl receives the request.
        assert store.state(IDENTITY)["phase"] == "submitting"
        calls.append(argv)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(launcher.subprocess, "run", submit)
    first = launcher.launch(root, IDENTITY, "4.0.1")
    second = launcher.launch(root, IDENTITY, "4.0.1")
    assert first == second and len(calls) == 1
    plist = plistlib.loads((root / JOBS / f"{IDENTITY}.plist").read_bytes())
    assert plist["ProgramArguments"][0] == str(release / ".venv/bin/python")
    assert plist["ProgramArguments"][1:3] == ["-I", "-B"]
    assert plist["ProgramArguments"][-2:] == ["run", IDENTITY]
    assert "PYTHONPATH" not in plist["EnvironmentVariables"]
    assert calls[0][:2] == ["/bin/launchctl", "bootstrap"]
    with pytest.raises(ValueError, match="reused"):
        launcher.launch(root, IDENTITY, "4.0.2")
    with pytest.raises(ValueError, match="pending"):
        launcher.launch(root, "b" * 32, "4.0.1")


@pytest.mark.parametrize("failure", ["timeout", "nonzero"])
def test_unknown_os_acknowledgement_is_not_retried(managed, monkeypatch, failure):
    root, _, store = managed
    calls = []
    def submit(argv, **kwargs):
        calls.append(argv)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 20)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(launcher.subprocess, "run", submit)
    assert launcher.launch(root, IDENTITY, "4.0.1")["phase"] == "unknown"
    assert launcher.launch(root, IDENTITY, "4.0.1")["phase"] == "unknown"
    assert len(calls) == 1
    assert store.active()["id"] == IDENTITY


@pytest.mark.parametrize("identity,version", [
    ("../../escape", "4.0.1"), (IDENTITY, "4.0.1; touch /tmp/oops"),
    (IDENTITY, "4.0.1-beta"), (IDENTITY, "4.0.0"), (IDENTITY, "4.0.2"),
])
def test_launcher_rejects_unapproved_versions_and_paths(managed, monkeypatch, identity, version):
    root, _, store = managed
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(ValueError):
        launcher.launch(root, identity, version)
    assert store.active() is None


@pytest.mark.parametrize("fault", ["symlink", "writable", "disabled"])
def test_invalid_or_disabled_policy_never_launches(managed, monkeypatch, fault):
    root, _, _ = managed
    path = root / POLICY
    if fault == "symlink":
        path.rename(root / "other.json")
        path.symlink_to(root / "other.json")
    elif fault == "writable":
        path.chmod(0o666)
    else:
        launcher.configure(root, enabled=False)
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises((OSError, ValueError)):
        launcher.launch(root, IDENTITY, "4.0.1")


def prepare_worker(store, monkeypatch):
    monkeypatch.setenv("CC_REMOTE_UI_ALLOW_RELAY_UPDATE", "1")
    monkeypatch.setattr(launcher, "retire", lambda *args: None)
    with store.lock():
        store.create(IDENTITY, "4.0.1", "4.0.0")
    from cc_remote.wrapper import process_scan
    monkeypatch.setattr(process_scan, "process_identity", lambda pid: SimpleNamespace(start_ticks=123))


def test_worker_reserves_idle_boundary_and_keeps_terminal_receipt(managed, monkeypatch):
    root, _, store = managed
    prepare_worker(store, monkeypatch)
    order = []
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", "file:///untrusted")
    def reserve(_store, identity):
        assert identity == IDENTITY and store.state(identity)["phase"] == "waiting"
        order.append("reserved")
    def update(**kwargs):
        assert kwargs["allow_relay_update"] is False
        assert os.environ["CC_REMOTE_GITHUB_REPOSITORY"] == "fixture/releases"
        assert "CC_REMOTE_RELEASE_BASE_URL" not in os.environ
        for phase in ["downloading", "validating", "ready", "updating_relay", "installing", "verifying"]:
            kwargs["progress"](phase)
            order.append(phase)
        return 0
    monkeypatch.setattr(launcher, "reserve", reserve)
    monkeypatch.setattr(launcher, "update", update)
    launcher.run(root, IDENTITY)
    assert order.index("reserved") < order.index("installing")
    assert store.state(IDENTITY)["phase"] == "succeeded"
    assert store.state(IDENTITY)["cleanup_pending"] is True
    before = list(order)
    launcher.run(root, IDENTITY)
    assert order == before, "a completed worker invocation must not rerun the installer"
    assert "worker_pid" not in public_job(store.state(IDENTITY))


@pytest.mark.parametrize("message,phase,reason", [
    ("wire protocol changes", "blocked", "protocol_change"),
    ("Claude SDK/service compatibility changes", "blocked", "service_migration"),
    ("upstream upgrade is not authorized", "blocked", "relay_permission"),
    ("installer did not complete", "failed", "update_failed"),
])
def test_worker_failure_does_not_claim_success_or_expose_private_output(managed, monkeypatch, message, phase, reason):
    root, _, store = managed
    prepare_worker(store, monkeypatch)
    def fail(**kwargs):
        raise updater.UpdateError(message + " secret-value")
    monkeypatch.setattr(launcher, "update", fail)
    launcher.run(root, IDENTITY)
    state = store.state(IDENTITY)
    assert (state["phase"], state["reason"]) == (phase, reason)
    assert "secret-value" not in json.dumps(public_job(state))


def test_cancel_before_install_is_cooperative_and_never_kills_services(managed, monkeypatch):
    root, _, store = managed
    prepare_worker(store, monkeypatch)
    launcher.cancel(root, IDENTITY)
    def update(**kwargs):
        kwargs["progress"]("downloading")
        pytest.fail("a cancelled update must not reach activation")
    monkeypatch.setattr(launcher, "update", update)
    launcher.run(root, IDENTITY)
    assert store.state(IDENTITY)["phase"] == "cancelled"
    store.save({**store.state(IDENTITY), "phase": "installing", "cancel_requested": False})
    launcher.cancel(root, IDENTITY)
    assert store.state(IDENTITY)["cancel_requested"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["idle", "turn", "queue", "background", "query-lock", "scheduler", "work-scan", "dsh-job",
                                  "question", "queue-start", "queue-drain", "native-turn", "dsh-child", "dsh-question"])
async def test_reservation_checks_activity_before_blocking_new_work(managed, monkeypatch, busy):
    root, _, store = managed
    machine, transport = _mk_machine()
    updates = UIUpdates(machine, root)
    machine._ui_updates = updates
    ctx = _mk_ctx("session", "session")
    machine.sessions[ctx.key] = ctx
    machine.focused_sid = ctx.key
    with store.lock():
        state, _ = store.create(IDENTITY, "4.0.1", "4.0.0")
        store.save({**state, "phase": "waiting"})
    if busy == "turn":
        ctx.state = "running"
    if busy == "queue":
        ctx.queued_queries.append(object())
    if busy == "background":
        ctx.claude_active_tasks.add("agent")
    if busy == "query-lock":
        await ctx.query_lock.acquire()
    if busy == "scheduler":
        machine._command_scheduler._pending["fixture"] = SimpleNamespace(task=SimpleNamespace(done=lambda: False))
    if busy == "work-scan":
        machine._ui_schedule_scanning = True
    if busy == "dsh-job":
        from cc_remote.protocol import DshJob
        machine._dsh.jobs["session"] = [DshJob(id="job", label="Job", status="running")]
    if busy == "dsh-child":
        ctx.engine = "dsh"
        ctx.sdk = SimpleNamespace(children=[{"id": "child", "activity": "running"}])
    if busy == "dsh-question":
        machine._dsh.questions["ask"] = SimpleNamespace(done=lambda: False)
    if busy == "question":
        ctx.pending_asks["question"] = object()
    if busy == "queue-start":
        ctx.queued_query_starting_msg_id = "queued"
    if busy == "queue-drain":
        ctx.queued_query_drain_task = SimpleNamespace(done=lambda: False)
    if busy == "native-turn":
        ctx.sdk = SimpleNamespace(turn_active=True)
    try:
        assert updates.reserve(IDENTITY)["status"] == ("ready" if busy == "idle" else "busy")
        if busy == "idle":
            from cc_remote.protocol import Query, GetHistory
            assert (await updates.reject(Query(prompt="test", msg_id="q", sid=ctx.key))).code == "busy"
            assert await updates.reject(GetHistory(session_id=ctx.key)) is None
            store.save({**store.state(IDENTITY), "phase": "succeeded"})
            assert updates.guarded() is False
    finally:
        machine._command_scheduler._pending.clear()
        await machine._command_scheduler.close()


@pytest.mark.asyncio
async def test_source_checkout_does_not_discover_real_user_installation():
    machine, _ = _mk_machine()
    assert machine._ui_updates.root is None
    await machine._ui_updates.start()
    assert machine._ui_updates.task is None
    assert (await machine._ui_updates.status())["managed"] is False
    await machine._command_scheduler.close()


def test_existing_installer_progress_waits_before_mutation(tmp_path, monkeypatch):
    installation = _installation(tmp_path, monkeypatch)
    mirror, marker, _ = _bundle(tmp_path, installation)
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", mirror.as_uri())
    phases = []
    def progress(phase):
        phases.append(phase)
        if phase == "ready":
            assert not marker.exists()
    updater.update(role="wrapper", target_version="4.0.1", check=False, progress=progress)
    assert phases == ["downloading", "validating", "ready", "updating_relay", "installing", "verifying"]
    assert marker.exists()


def test_browser_update_api_enforces_auth_origin_scope_and_strict_commands(tmp_path):
    cfg = _cfg(tmp_path, login_users_json=json.dumps({"owner": {"password": "password-for-owner", "machines": ["device-a"]}}))
    app = server.create_app(cfg)
    app.state.updates.request = AsyncMock(return_value={"schema": 1, "enabled": False})
    with TestClient(app, base_url=cfg.public_origin) as client:
        assert client.get("/api/updates/device-a").status_code == 401
        assert client.post("/api/login", json={"username": "owner", "password": "password-for-owner"}).status_code == 200
        assert client.get("/api/updates/device-b").status_code == 403
        assert client.post("/api/updates/device-a", json={"op": "check"}).status_code == 403
        headers = {"Origin": cfg.public_origin}
        for request in [{"op": "shell", "argv": ["rm"]}, {"op": "start", "job_id": IDENTITY, "version": "4.0.1", "argv": []},
                        {"op": "start", "job_id": "../escape", "version": "4.0.1"}]:
            assert client.post("/api/updates/device-a", json=request, headers=headers).status_code == 400
        assert app.state.updates.request.await_count == 0
        response = client.post("/api/updates/device-a", json={"op": "check"}, headers=headers)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        app.state.updates.request.assert_awaited_once_with("device-a", {"op": "check"})


def test_maintenance_roundtrip_is_independent_of_chat_protocol(tmp_path):
    cfg = _cfg(tmp_path)
    app = server.create_app(cfg)
    with TestClient(app, base_url=cfg.public_origin) as client:
        assert client.post("/api/login", json={"password": cfg.login_password}).status_code == 200
        with client.websocket_connect("/ws/updates", headers={"Authorization": f"Bearer {cfg.wrapper_token}"}) as ws:
            ws.send_json({"schema": 1, "machine_id": "device-a"})
            assert ws.receive_json() == {"schema": 1, "ready": True}
            # Execute an HTTP call while the independent device channel answers.
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(client.get, "/api/updates/device-a")
                request = ws.receive_json()
                assert request["schema"] == 1 and "v" not in request
                ws.send_json({"schema": 1, "id": request["id"], "result": {"schema": 1, "managed": True}})
                assert future.result(timeout=3).json()["managed"] is True


@pytest.mark.parametrize("headers,hello", [
    ({}, {"schema": 1, "machine_id": "device-a"}),
    ({"Authorization": "Bearer " + "a" * 48, "Origin": "https://remote.example"}, {"schema": 1, "machine_id": "device-a"}),
    ({"Authorization": "Bearer " + "a" * 48}, {"schema": 1, "machine_id": "device-b"}),
])
def test_maintenance_socket_rejects_browser_or_wrong_device(tmp_path, headers, hello):
    cfg = _cfg(tmp_path, wrapper_tokens_json=json.dumps({"device-a": "a" * 48}))
    app = server.create_app(cfg)
    with TestClient(app, base_url=cfg.public_origin) as client, pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/updates", headers=headers) as ws:
            ws.send_json(hello)
            ws.receive_json()


@pytest.mark.asyncio
async def test_revoked_idle_maintenance_peer_cannot_receive_a_command():
    hub = UpdateRelay()
    ws = SimpleNamespace(send_text=AsyncMock())
    hub.peers["device"] = Peer(ws, AsyncMock(return_value=False))
    with pytest.raises(UpdateChannelError, match="unavailable"):
        await hub.request("device", {"op": "start"})
    ws.send_text.assert_not_awaited()


def test_linux_setup_writes_only_fixed_root_validated_helper(managed, monkeypatch, tmp_path):
    root, _, store = managed
    value, account = launcher.installation(root)
    value.manifest['os'] = 'linux'
    monkeypatch.setattr(launcher, 'installation', lambda _root: (value, account))
    monkeypatch.setattr(launcher, 'require_job_manager', lambda system: None)
    from deploy import linux_service
    monkeypatch.setattr(linux_service, 'trusted', lambda path: None)
    helper = tmp_path / 'libexec' / 'cc-remote-ui-update-fixture'
    monkeypatch.setattr(launcher, 'helper_path', lambda _root: helper)
    monkeypatch.setattr(launcher, 'SUDOERS_DIRECTORY', tmp_path / 'sudoers')
    calls = []
    monkeypatch.setattr(launcher.subprocess, 'run', lambda args, **kw: calls.append(args))
    launcher.configure(root, enabled=True, allow_relay=True)
    assert calls[0][:2] == ['/usr/sbin/visudo', '-cf']
    content = helper.read_text()
    assert 'start:3|cancel:2' in content
    assert str(root / 'current/.venv/bin/python') in content
    assert f'--root {root}' in content
    assert '--root "$' not in content
    assert (tmp_path / 'sudoers' / helper.name).stat().st_mode & 0o777 == 0o440
    assert store.policy()['allow_relay_update'] is True
    launcher.configure(root, enabled=False)
    assert store.policy()['enabled'] is False


def test_no_systemd_cannot_enable_or_launch_linux_job(monkeypatch):
    monkeypatch.setattr(launcher.os, 'access', lambda *args: False)
    with pytest.raises(ValueError, match='OS job manager'):
        launcher.require_job_manager('linux')


@pytest.mark.asyncio
async def test_guard_preserves_target_and_prevents_cold_resume(managed):
    from cc_remote.protocol import Query, SwitchSession
    root, _, store = managed
    machine, transport = _mk_machine()
    updates = UIUpdates(machine, root)
    with store.lock():
        state, _ = store.create(IDENTITY, '4.0.1', '4.0.0')
        store.save({**state, 'phase': 'installing'})
    updates.reserved = IDENTITY
    machine.focused_sid = 'foreground'
    error = await updates.reject(Query(prompt='new', msg_id='q', sid='background'))
    assert error.sid == 'background'
    assert await updates.reject(SwitchSession(session_id='cold-session')) is not None
    await machine._command_scheduler.close()


@pytest.mark.asyncio
async def test_worker_pid_reuse_is_unknown_and_never_unblocks_installation(managed, monkeypatch):
    root, _, store = managed
    machine, _ = _mk_machine()
    machine._local_control.socket = Path(store.policy()['control_socket'])
    machine._local_control.server = object()
    updates = UIUpdates(machine, root)
    prepare_worker(store, monkeypatch)
    store.save({**store.active(), 'phase': 'installing', 'worker_pid': 123, 'worker_start': 999})
    updates.reserved = IDENTITY
    state = await updates.status()
    assert state['job']['phase'] == 'unknown'
    assert state['job']['reason'] == 'job_unknown'
    assert updates.guarded()
    assert 'worker_pid' not in state['job']
    await machine._command_scheduler.close()


@pytest.mark.asyncio
async def test_missing_control_barrier_cannot_launch(managed, monkeypatch):
    root, _, _ = managed
    machine, _ = _mk_machine()
    updates = UIUpdates(machine, root)
    monkeypatch.setattr(updates, 'invoke', lambda *args: pytest.fail('must not launch'))
    result = await updates.handle({'op': 'start', 'job_id': IDENTITY, 'version': '4.0.1'})
    assert result == {'ok': False, 'reason': 'control_unavailable'}
    await machine._command_scheduler.close()


def test_installer_failure_requires_reconciliation_before_new_job(managed, monkeypatch):
    root, _, store = managed
    prepare_worker(store, monkeypatch)
    def fail(**kwargs):
        kwargs['progress']('installing')
        raise updater.UpdateError('installer did not complete successfully')
    monkeypatch.setattr(launcher, 'update', fail)
    launcher.run(root, IDENTITY)
    assert store.active()['phase'] == 'unknown'
    with pytest.raises(ValueError, match='pending'):
        store.create('b' * 32, '4.0.1', '4.0.0')


@pytest.mark.parametrize("failure", [
    "submit-timeout", "submit-disconnect", "invalid-ack", "status-timeout",
    "status-disconnect", "health-unavailable", "failed-unit", "preflight-denied",
])
def test_relay_uncertainty_keeps_receipt_and_barrier(managed, monkeypatch, failure):
    root, _, store = managed
    launcher.configure(root, enabled=True, allow_relay=True)
    prepare_worker(store, monkeypatch)
    value, _ = launcher.installation(root)
    monkeypatch.setattr(update_relay, "relay_origin", lambda _: "https://relay.example.test")
    relay = update_relay.RelayUpdate(value, "fixture-relay")
    updates = UIUpdates(SimpleNamespace(sessions={}), root)
    requests = []
    calls = []
    def inspect(_origin):
        if requests and failure == "health-unavailable":
            raise updater.UpdateError("Relay health check failed")
        return {"version": "4.0.1" if requests else "4.0.0", "protocol": 72}
    monkeypatch.setattr(update_relay, "relay_release", inspect)

    def ssh(argv, **kwargs):
        assert argv[0] == "/usr/bin/ssh", "only the SSH boundary is stubbed"
        command = shlex.split(argv[-1])
        calls.append(command)
        stage = "status" if "systemctl" in command else "submit" if len(command) == 7 else "preflight"
        if stage == "submit":
            requests.append(json.loads(command[-1]))
        if failure == f"{stage}-timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if failure == f"{stage}-disconnect" or failure == "preflight-denied" and stage == "preflight":
            return SimpleNamespace(returncode=255, stdout="", stderr="Permission denied" if stage == "preflight"
                                   else "Connection reset by peer")
        if stage == "preflight":
            output = '{"domain":"relay.example.test","version":"4.0.0"}'
        elif stage == "submit":
            output = "invalid acknowledgement" if failure == "invalid-ack" else '{"status":"submitted"}'
        else:
            output = ("LoadState=loaded\nActiveState=failed\n" if failure == "failed-unit" else
                      "LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0\n")
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(update_relay.subprocess, "run", ssh)
    def reserve(_store, identity):
        assert updates.reserve(identity) == {"status": "ready"}
    monkeypatch.setattr(launcher, "reserve", reserve)
    def update(**kwargs):
        kwargs["progress"]("ready")
        kwargs["progress"]("updating_relay")
        relay.ensure("4.0.1", 72, allow_protocol_change=False)
        pytest.fail("an uncertain Relay cannot activate the device")
    monkeypatch.setattr(launcher, "update", update)
    launcher.run(root, IDENTITY)
    if failure == "preflight-denied":
        assert store.active()["phase"] == "blocked"
        assert store.active()["reason"] == "relay_permission"
        assert not relay.journal.exists() and not requests
        assert not updates.guarded()
        return

    assert store.active()["phase"] == "unknown"
    assert store.active()["reason"] == "outcome_pending"
    assert updates.guarded()
    assert UIUpdates(SimpleNamespace(), root).guarded(), "Wrapper restart restores the reservation"
    transaction = json.loads(relay.journal.read_text())
    assert transaction["complete"] is False
    assert requests[0]["unit"] == transaction["unit"]
    before = list(calls)
    assert launcher.launch(root, IDENTITY, "4.0.1")["phase"] == "unknown"
    launcher.run(root, IDENTITY)
    assert calls == before, "neither a browser replay nor a duplicate worker may launch again"
    with pytest.raises(ValueError, match="pending"):
        launcher.launch(root, "b" * 32, "4.0.1")
    assert not launcher.cancel(root, IDENTITY).get("cancel_requested")
    assert updates.guarded()


def test_linux_os_job_has_system_path_and_collects_after_exit(managed, monkeypatch):
    root, _, store = managed
    value, account = launcher.installation(root)
    value.manifest['os'] = 'linux'
    monkeypatch.setattr(launcher, 'installation', lambda _root: (value, account))
    monkeypatch.setattr(launcher, 'require_job_manager', lambda system: None)
    calls = []
    def submit(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(launcher.subprocess, 'run', submit)
    launcher.launch(root, IDENTITY, '4.0.1')
    assert calls[0][0] == '/usr/bin/systemd-run'
    assert '--collect' in calls[0] and '--no-block' in calls[0]
    assert '--setenv=PATH=/usr/sbin:/usr/bin:/sbin:/bin' in calls[0]
    assert calls[0][-2:] == ['run', IDENTITY]
    assert store.active()['phase'] == 'submitting'


def test_mac_completed_os_job_is_retired_only_after_terminal_receipt(managed, monkeypatch):
    root, _, store = managed
    state, _ = store.create(IDENTITY, '4.0.1', '4.0.0')
    calls = []
    monkeypatch.setattr(launcher.subprocess, 'run', lambda args, **kw: calls.append(args))
    launcher.retire(root, IDENTITY)
    assert not calls
    store.save({**state, 'phase': 'succeeded'})
    launcher.retire(root, IDENTITY)
    assert calls == [['/bin/launchctl', 'bootout', f'gui/{os.geteuid()}/cc-remote-ui-update-{IDENTITY}']]


def test_restart_restores_only_an_activation_reservation(managed):
    root, _, store = managed
    state, _ = store.create(IDENTITY, '4.0.1', '4.0.0')
    machine = SimpleNamespace()
    store.save({**state, 'phase': 'unknown', 'reason': 'launch_unknown'})
    assert UIUpdates(machine, root).reserved is None
    store.save({**state, 'phase': 'installing', 'worker_pid': 123})
    assert UIUpdates(machine, root).reserved == IDENTITY
    store.save({**state, 'phase': 'unknown', 'worker_pid': 123})
    assert UIUpdates(machine, root).reserved == IDENTITY


def test_disabling_permission_during_download_stops_before_activation(managed, monkeypatch):
    root, _, store = managed
    prepare_worker(store, monkeypatch)
    def update(**kwargs):
        kwargs['progress']('downloading')
        launcher.configure(root, enabled=False)
        kwargs['progress']('validating')
        pytest.fail('disabled update must never activate')
    monkeypatch.setattr(launcher, 'update', update)
    launcher.run(root, IDENTITY)
    assert store.active()['phase'] == 'cancelled'
