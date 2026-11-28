"""Gemini through the Wrapper command, replay and deferred-delivery boundaries."""
from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from cc_remote import protocol
from cc_remote.protocol import (
    FILE_PREVIEW_MAX_BYTES, AcknowledgeCompletion, AnswerQuestion, AntigravityAuth, ArchiveSession,
    GetAntigravityQuota, GetContext, GetEngineCapabilities, GetFilePreview,
    GetHistory, GetModels, GetTurnDetail, Hello, Interrupt, ListSessions, NewSession, Query, RenameSession,
    SetAntigravityOption, Steer, SwitchSession,
    deserialize, serialize,
)
from cc_remote.antigravity_service import (
    GEMINI_SERVICE_VERSION, GeminiController, GeminiService, ServiceTransport, local_message, service_control, service_message,
)
from cc_remote.claude_service.wire import read_frame, write_frame
from cc_remote.session_messages import parse_message, reference
from cc_remote.wrapper import antigravity_quota, antigravity_runtime
from cc_remote.wrapper import session_presentation
from cc_remote.wrapper.antigravity_client import AcpClient, AcpError, AntigravityLaunch
from cc_remote.wrapper.antigravity_hooks import AntigravityHooks
from cc_remote.wrapper.antigravity_runtime import AntigravityRuntime
from cc_remote.wrapper.session_messages import SessionMessages
from cc_remote.wrapper.session_pins import SessionPinStore
from cc_remote.wrapper.session_presentation import SessionPresentationStore
from tests.test_multisession import _mk_machine
from tests import test_viewer
from tests.test_video_preview import owner_for, video_file

publication = test_viewer.publication
isolated_login_limiter = test_viewer.isolated_login_limiter


def test_local_launch_is_opt_in_and_keeps_gateway_credentials_local(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    for variable in (
        "CC_REMOTE_ANTIGRAVITY", "CC_REMOTE_ANTIGRAVITY_BIN", "GEMINI_API_KEY",
        "AGY_LLM_GATEWAY_URL", "AGY_LLM_GATEWAY_API_KEY", "AGY_LLM_GATEWAY_WIRE_PROTOCOL",
    ):
        monkeypatch.delenv(variable, raising=False)
    binary = tmp_path / ".local" / "bin" / "agy"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    assert AntigravityLaunch.local() is None

    settings = tmp_path / ".antigravity" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({
        "gateway": {"enabled": True, "url": "https://gateway.test", "apiKey": "test-key"},
        "models": {"defaultModelId": "neon"},
    }))
    monkeypatch.setenv("WRAPPER_TOKEN", "private-relay-token")
    launch = AntigravityLaunch.local()
    assert launch is not None
    assert launch.default_model == "neon"
    assert launch.environment["AGY_LLM_GATEWAY_API_KEY"] == "test-key"
    assert "WRAPPER_TOKEN" not in launch.environment
    assert "test-key" not in repr(launch)
    monkeypatch.setenv("AGY_LLM_GATEWAY_URL", "https://other.test")
    assert AntigravityLaunch.local() is None
    monkeypatch.setenv("CC_REMOTE_ANTIGRAVITY", "0")
    assert AntigravityLaunch.local() is None


def test_managed_agy_uses_the_wrappers_environment_and_clears_worker_values(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("CC_REMOTE_ANTIGRAVITY", "1")
    monkeypatch.setenv("CC_REMOTE_ANTIGRAVITY_BIN", "/usr/bin/true")
    monkeypatch.setenv("PATH", "/worker/bin")
    monkeypatch.setenv("HTTPS_PROXY", "http://worker.test")
    monkeypatch.setenv("WRAPPER_TOKEN", "control-plane-only")

    launch = AntigravityLaunch.local(wrapper_environment={
        "PATH": "/wrapper/bin:/usr/bin",
        "http_proxy": "http://wrapper.test",
        "NO_PROXY": "localhost",
        "GEMINI_API_KEY": "fixture-provider-key",
    })
    assert launch.environment["PATH"] == "/wrapper/bin:/usr/bin"
    assert launch.environment["http_proxy"] == "http://wrapper.test"
    assert launch.environment["NO_PROXY"] == "localhost"
    assert launch.environment["GEMINI_API_KEY"] == "fixture-provider-key"
    assert launch.environment["CC_REMOTE_ANTIGRAVITY"] == "1"
    assert "HTTPS_PROXY" not in launch.environment and "WRAPPER_TOKEN" not in launch.environment
    assert "fixture-provider-key" not in repr(launch)
    assert AntigravityLaunch.local(wrapper_environment={"CC_REMOTE_ANTIGRAVITY": "0"}) is None


class ScriptedAcp:
    auth_methods = ["oauth-personal", "oauth-business"]
    capabilities = {
        "loadSession": True, "sessionCapabilities": {"list": {}, "close": {}},
        "promptCapabilities": {"embeddedContext": True, "image": True},
    }
    launch = SimpleNamespace(default_model="neon")

    def __init__(self, remote, cwd):
        self.remote = remote
        self.cwd = str(cwd)
        self.running = True
        self.calls = []
        self.prompts = []
        self.rows = {}
        self.directories = {}
        self.write_lock = asyncio.Lock()
        self.controls = {"neon": {"model": "neon", "mode": "default", "effort": "low"}}

    @staticmethod
    def picker(id_, category, current, choices):
        return {"id": id_, "category": category, "type": "select", "currentValue": current,
                "options": [{"value": value, "name": label} for value, label in choices]}

    def options(self, sid):
        current = self.controls[sid]
        opts = [
            self.picker("model", "model", current["model"], [("flash", "Flash"), ("neon", "Gemini 4 Neon (High)")]),
            self.picker("mode", "mode", current["mode"], [("default", "Default"), ("plan", "Plan")]),
        ]
        if current["model"] == "flash":
            opts.append(self.picker("effort", "thought_level", current["effort"], [("low", "Low"), ("extra", "Extra")]))
        return {"sessionId": sid, "configOptions": opts}

    async def update(self, sid, kind_, **fields):
        await self.remote.on_update({"sessionId": sid, "update": {"sessionUpdate": kind_, **fields}})

    async def call(self, method, args, *, timeout=30):
        self.calls.append((method, args))
        if method == "authenticate":
            assert timeout == 300
            return {}
        sid = args.get("sessionId")
        if method == "session/new":
            sid = f"native-{len(self.controls)}"
            self.controls[sid] = {"model": "flash", "mode": "default", "effort": "low"}
            self.directories[sid] = args["cwd"]
            # Delayed messages for an earlier closed probe are interleaved
            # with the next session/new response.
            await self.update("retired-catalog", "usage_update", used=12, size=64)
            await self.update(sid, "available_commands_update",
                              availableCommands=[{"name": "compact", "description": "Native compact"}])
            await self.update(sid, "usage_update", used=0, size=1048576)
            return self.options(sid)
        if method == "session/set_config_option":
            self.controls[sid][args["configId"]] = args["value"]
            return self.options(sid)
        if method == "session/resume":
            await self.update(sid, "available_commands_update",
                              availableCommands=[{"name": "compact", "description": "Native compact"}])
            return self.options(sid)
        if method == "session/load":
            if sid not in self.controls:
                raise AcpError("session_missing", "Missing native session")
            await self.update(sid, "available_commands_update",
                              availableCommands=[{"name": "compact", "description": "Native compact"}])
            if sid in self.rows:
                await self.update(sid, "user_message_chunk", content={"type": "text", "text": "saved question"})
                await self.update(sid, "agent_message_chunk", content={"type": "text", "text": "saved answer"})
            return self.options(sid)
        if method == "session/list":
            return {"sessions": list(self.rows.values())}
        assert method == "session/close"
        return {}

    async def submit(self, method, args, *, guard=None):
        assert method == "session/prompt"
        if guard:
            guard()
        sid = args["sessionId"]
        self.rows[sid] = {"sessionId": sid, "title": "Gemini regression", "cwd": self.directories[sid]}
        outcome = asyncio.get_running_loop().create_future()
        self.prompts.append((args, outcome))
        return outcome

    async def notify(self, method, args):
        self.calls.append((method, args))
        assert method == "session/cancel"
        for request, response in self.prompts:
            if request["sessionId"] == args["sessionId"] and not response.done():
                response.set_result({"stopReason": "cancelled"})

    async def close(self):
        self.running = False
        for _, response in self.prompts:
            if not response.done():
                response.set_exception(AcpError("offline", "Disconnected native process", unknown=True))


@pytest_asyncio.fixture
async def wire(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_REMOTE_ANTIGRAVITY", "1")
    machine, transport = _mk_machine()
    machine.cfg.cc_cwd = str(tmp_path)
    trust_workspaces(machine, tmp_path)
    runtime = machine._antigravity = AntigravityRuntime(machine, launch_factory=lambda: True)
    client = ScriptedAcp(runtime, tmp_path)
    runtime.client = client
    yield machine, transport, runtime, client
    tasks = [task for ctx in machine.sessions.values()
             for task in (ctx.turn_task, ctx.queued_query_drain_task) if task and not task.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await runtime.close()


def trust_workspaces(machine, *paths):
    machine.cfg.antigravity_trusted_workspaces = json.dumps([str(path.resolve()) for path in paths])


@pytest_asyncio.fixture
async def managed_gemini(wire):
    worker, _, _, native = wire
    wrapper, transport = _mk_machine()
    worker.cfg.antigravity_owner_state_dir = wrapper.cfg.state_dir
    worker.transport = ServiceTransport()
    service = GeminiService(worker, worker.transport)
    with TemporaryDirectory(prefix="ccr-managed-gemini-", dir="/tmp") as directory:
        path = Path(directory) / "service.sock"
        listener = await asyncio.start_unix_server(service.connection, str(path))
        path.chmod(0o600)
        wrapper._gemini_controller = GeminiController(path, wrapper)
        try:
            yield wrapper, transport, worker, service, native
        finally:
            await wrapper._gemini_controller.close()
            await wrapper._command_scheduler.close()
            listener.close()
            await listener.wait_closed()
            await service.close()


async def new_session(machine, **fields):
    focus, _ = await machine._handle(NewSession(engine="antigravity", **fields))
    return machine.sessions[focus.session_id]


async def prompted(client, count):
    async with asyncio.timeout(1):
        while len(client.prompts) < count:
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_managed_gemini_media_is_registered_and_streamed_by_wrapper(tmp_path, publication, managed_gemini):
    wrapper, transport, worker, _, _ = managed_gemini
    cwd = tmp_path / "gemini-workspace"
    cwd.mkdir()
    trust_workspaces(wrapper, cwd)
    focus = await wrapper._handle(NewSession(engine="antigravity", cwd=str(cwd)))
    clip = video_file(tmp_path, size=4096)

    async with test_viewer.live_viewer(tmp_path, publication, mode="bridge", media_previews=wrapper.media_previews) as (
            client, _app, config, _site):
        owner = owner_for(client, config)
        preview = await wrapper._handle(GetFilePreview(sid=focus.session_id, path=str(clip),
                                                        request_id="gemini-video", client_id="browser", owner_id=owner))
        assert preview.format == "video" and preview.media_id and not preview.error
        assert any(frame == preview for frame in transport.sent)
        assert wrapper.media_previews.get(preview.media_id, owner).sid == focus.session_id
        url = f"/api/media/device/{preview.media_id}"
        async with httpx.AsyncClient(base_url=config.public_origin, cookies=client.cookies) as http:
            response = await http.get(url, headers={"Range": "bytes=0-15"})
            assert response.status_code == 206
            assert response.content == clip.read_bytes()[:16]
            worker._preview_capability_store.revoke("antigravity", "code", focus.session_id, str(clip.resolve()))
            assert (await http.get(url, headers={"Range": "bytes=0-15"})).status_code == 404

            source = cwd / "original.txt"
            source.write_bytes(b"x" * (FILE_PREVIEW_MAX_BYTES + 16))
            original = await wrapper._handle(GetFilePreview(sid=focus.session_id, path=source.name,
                                                              request_id="gemini-original", client_id="browser", owner_id=owner))
            assert original.truncated and original.media_id and not original.error
            raw = await http.get(f"/api/media/device/{original.media_id}", headers={"Range": "bytes=-16"})
            assert raw.status_code == 206 and raw.content == b"x" * 16
            assert raw.headers["content-type"] == "application/octet-stream"


@pytest.mark.asyncio
async def test_managed_gemini_imports_cold_receipts_and_keeps_newer_service_completions(managed_gemini, monkeypatch):
    wrapper, _, worker, service, native = managed_gemini
    old, newer = "agy@old", "agy@newer"
    for sid in (old, newer):
        native.rows[sid[4:]] = {"sessionId": sid[4:], "cwd": native.cwd, "title": sid}
    now = [100.0]
    monkeypatch.setattr(session_presentation, "time", SimpleNamespace(time=lambda: now[0]))
    previous = wrapper._session_presentation
    for sid in (old, newer):
        previous.mark_completion("antigravity", sid, "wrapper-first")
        previous.acknowledge_completion("antigravity", sid, "wrapper-first")
        previous.mark_completion("antigravity", sid, "wrapper-unread")
    previous.mark_completion("codex", "codex-receipt", "unrelated")
    now[0] = 200.0
    active = worker._session_presentation
    active.mark_completion("antigravity", newer, "service-receipt")

    listing = await wrapper._handle(ListSessions(engine="antigravity"))
    receipts = {row.session_id: (row.completion_id, row.completion_unread, row.completion_revision)
                for row in listing.sessions}
    assert receipts == {old: ("wrapper-unread", True, 3), newer: ("service-receipt", True, 4)}
    assert not active.entries_for_engine("codex")
    await wrapper._handle(AcknowledgeCompletion(sid=old, completion_id="wrapper-unread"))
    assert not active.get("antigravity", old).completion_unread
    assert active.get("antigravity", old).completion_revision == 4

    await wrapper._gemini_controller.close()
    async with asyncio.timeout(2):
        while service.transport.writer is not None:
            await asyncio.sleep(.001)
    now[0] = 300.0
    previous.mark_completion("antigravity", old, "stale-owner")
    await wrapper._gemini_controller.list_sessions()
    restored = SessionPresentationStore(worker.cfg.state_dir).get("antigravity", old)
    assert (restored.completion_id, restored.completion_unread, restored.completion_revision) == ("wrapper-unread", False, 4)


@pytest.mark.asyncio
async def test_acp_launch_uses_a_private_control_directory_and_managed_hooks(monkeypatch, tmp_path):
    captured = {}

    async def launch(*args, cwd, env, **_streams):
        captured["cwd"] = Path(cwd)
        captured["hooks"] = Path(args[args.index("--add-dir") + 1])
        assert env["PWD"] == cwd and not captured["cwd"].samefile(tmp_path)
        assert captured["cwd"].stat().st_mode & 0o777 == 0o700
        assert not list(captured["cwd"].iterdir())
        assert (captured["hooks"] / ".agents" / "hooks.json").is_file()
        raise OSError("fixture launch failure")

    async def noop(*_args):
        pass

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    acp = AcpClient(AntigravityLaunch("/fake/agy", {"PWD": str(tmp_path)}), noop, noop, on_lifecycle=noop)
    with pytest.raises(AcpError, match="无法启动"):
        await acp.start()
    assert all(not path.exists() for path in captured.values())


@pytest.mark.asyncio
async def test_workspace_must_be_trusted_before_agy_can_open_or_restore_it(wire, tmp_path):
    machine, _, _, client = wire
    project = tmp_path / "project"
    project.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)
    workspace = str(project.resolve())
    command = NewSession(engine="antigravity", cwd=str(alias), client_id="owner", cmd_id="trust-create")
    client.rows["previous"] = {"sessionId": "previous", "cwd": workspace}
    client.capabilities = {**client.capabilities, "sessionCapabilities": {"list": {}, "resume": {}}}
    assert (await machine._handle(command)).code == "gemini_workspace_untrusted"
    assert (await machine._handle(SwitchSession(engine="antigravity", session_id="agy@previous"))).code == "gemini_workspace_untrusted"
    assert "信任" in (await machine._handle(GetHistory(session_id="agy@previous"))).error
    assert all(method == "session/list" for method, _ in client.calls)

    machine.cfg.antigravity_trusted_workspaces = json.dumps([str(alias)])
    assert (await machine._handle(command)).code == "gemini_workspace_untrusted"
    machine.cfg.antigravity_trusted_workspaces = '{"invalid":true}'
    assert (await machine._handle(command)).code == "gemini_workspace_config"
    machine.cfg.antigravity_trusted_workspaces = json.dumps(["*", workspace])
    assert (await machine._handle(command)).code == "gemini_workspace_config"
    trust_workspaces(machine, project)
    focus, _ = await machine._handle(command)
    assert focus.cwd == workspace
    assert [params["cwd"] for method, params in client.calls if method == "session/new"] == [workspace]


@pytest.mark.asyncio
async def test_all_workspace_trust_can_open_reopen_and_continue_an_unlisted_project(wire, tmp_path):
    machine, _, runtime, client = wire
    project = tmp_path / "project"
    project.mkdir()
    machine.cfg.antigravity_trusted_workspaces = ' [ "*" ] '
    client.capabilities = {**client.capabilities, "sessionCapabilities": {"list": {}, "resume": {}}}
    opened = await machine._handle(NewSession(engine="antigravity", cwd=str(project)))
    assert isinstance(opened, tuple), opened
    ctx = machine.sessions[opened[0].session_id]
    await machine._handle(Query(sid=ctx.key, msg_id="first", prompt="Start"))
    client.prompts[-1][1].set_result({"stopReason": "end_turn"})
    await ctx.turn_task
    await machine._detach_idle_session(ctx.key)

    history = await machine._handle(GetHistory(session_id=ctx.key, detail="summary"))
    assert history.error is None and history.turns[0].prompt == "saved question"
    focus = await machine._handle(SwitchSession(engine="antigravity", session_id=ctx.key))
    assert focus.session_id == ctx.key
    assert any(method == "session/resume" and params["cwd"] == str(project.resolve())
               for method, params in client.calls)
    ctx = machine.sessions[ctx.key]
    await machine._handle(Query(sid=ctx.key, msg_id="second", prompt="Continue"))
    assert [params["prompt"][0]["text"] for params, _ in client.prompts] == ["Start", "Continue"]
    client.prompts[-1][1].set_result({"stopReason": "end_turn"})
    await ctx.turn_task

    trust_workspaces(machine)
    deferred = Query(sid=ctx.key, prompt="Queued before revocation", msg_id="revoked")
    assert (await runtime.query(ctx, deferred)).code == "gemini_workspace_untrusted"
    assert len(client.prompts) == 2


@pytest.mark.asyncio
async def test_resident_gemini_queries_cannot_change_or_skip_workspace_trust(wire):
    machine, _, runtime, client = wire
    ctx = await new_session(machine)
    result = await machine._handle(Query(sid=ctx.key, prompt="/add-dir", msg_id="change-workspace"))
    assert result.code == "gemini_workspace_command"
    trust_workspaces(machine)
    result = await machine._handle(Query(sid=ctx.key, prompt="Continue", msg_id="untrusted-workspace"))
    assert result.code == "gemini_workspace_untrusted"
    deferred = Query(sid=ctx.key, prompt="From the queue", msg_id="untrusted-deferred")
    assert (await runtime.query(ctx, deferred)).code == "gemini_workspace_untrusted"
    assert not client.prompts


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["/plan", "/plan Review this project"])
async def test_plan_slash_preserves_native_command_and_permission_mode(wire, prompt):
    machine, _, _, client = wire
    ctx = await new_session(machine)
    client.calls.clear()
    await machine._handle(Query(sid=ctx.key, msg_id="plan", prompt=prompt))
    request, outcome = client.prompts[0]
    assert request["prompt"] == [{"type": "text", "text": prompt}]
    assert not any(method in {"session/set_config_option", "session/set_mode"} for method, _ in client.calls)
    assert ctx.sdk.permission_mode == "default"
    outcome.set_result({"stopReason": "end_turn"})
    await ctx.turn_task


@pytest.mark.asyncio
async def test_goal_requires_an_objective_before_starting_a_prompt(wire):
    machine, _, _, client = wire
    failed = await machine._handle(NewSession(engine="antigravity", cwd=client.cwd, prompt="/goal", msg_id="empty-goal"))
    assert failed.code == "bad_prompt" and not client.calls
    ctx = await new_session(machine)
    failed = await machine._handle(Query(sid=ctx.key, msg_id="empty-goal", prompt="/goal"))
    assert failed.code == "bad_prompt" and not client.prompts
    await machine._handle(Query(sid=ctx.key, msg_id="goal", prompt="/goal Check the fixture"))
    request, outcome = client.prompts[0]
    assert request["prompt"] == [{"type": "text", "text": "/goal Check the fixture"}]
    outcome.set_result({"stopReason": "end_turn"})
    await ctx.turn_task


@pytest.mark.asyncio
async def test_catalog_controls_cwd_context_and_native_history(wire):
    machine, transport, runtime, client = wire
    catalog = await machine._handle(GetModels(engine="antigravity", cwd=client.cwd))
    assert catalog.default_model == "neon"
    assert not any(option.category == "thought_level" for option in catalog.antigravity_options)
    assert [command.name for command in catalog.antigravity_commands] == ["compact"]
    alternate = await machine._handle(GetModels(engine="antigravity", cwd=client.cwd, antigravity_model="flash"))
    assert alternate.default_model == "neon"
    assert next(model for model in alternate.models if model["id"] == "flash")["efforts"] == ["low", "extra"]
    assert (await machine._handle(ListSessions(engine="antigravity"))).sessions == []
    assert not machine.sessions and not client.prompts

    ctx = await new_session(machine, model="flash", antigravity_effort="extra", antigravity_mode="plan")
    assert (ctx.sdk.model, ctx.sdk.effort, ctx.sdk.permission_mode) == ("flash", "extra", "plan")
    assert (await machine._handle(GetContext(sid=ctx.key))).max_tokens == 1048576
    await machine._handle(SetAntigravityOption(sid=ctx.key, category="model", value="neon"))
    assert ctx.sdk.model == "neon" and ctx.sdk.effort is None
    assert not (await machine._handle(GetContext(sid=ctx.key))).available
    refused = await machine._handle(SetAntigravityOption(sid=ctx.key, category="thought_level", value="extra"))
    assert refused.code == "gemini_option"

    await machine._handle(Query(sid=ctx.key, msg_id="compact", prompt="/compact now",
                                files=[{"filename": "notes.txt", "data": "bm90ZXM="}]))
    blocks = client.prompts[0][0]["prompt"]
    assert blocks[0] == {"type": "text", "text": "/compact now"}
    assert blocks[1]["resource"]["text"] == "notes"
    await client.update(ctx.sdk.native_id, "agent_message_chunk", content={"type": "text", "text": "OK"})
    client.prompts[0][1].set_result({"stopReason": "end_turn", "usage": {
        "inputTokens": 12, "outputTokens": 9, "cachedReadTokens": 5,
        "cachedWriteTokens": 0, "thoughtTokens": 3,
    }})
    await ctx.turn_task
    history = await machine._handle(GetHistory(session_id=ctx.key, detail="summary"))
    assert len(history.turns) == 1 and history.turns[0].done and history.turns[0].id == "compact"
    usage, = [frame for frame in transport.sent if frame.type == "turn_usage"]
    assert usage.turn_id == "compact"
    assert history.turns[0].tokenUsage == usage.usage
    assert (usage.usage.input_tokens, usage.usage.output_tokens, usage.usage.thought_tokens) == (17, 9, 3)
    detail = await machine._handle(GetTurnDetail(session_id=ctx.key, turn_id="compact", revision=history.revision))
    assert any(row["type"] == "delta" and row["text"] == "OK" for row in detail.events)

    await machine._detach_idle_session(ctx.key)
    before = len(client.prompts)
    loaded = await machine._handle(GetHistory(session_id=ctx.key, detail="summary"))
    assert loaded.turns[0].prompt == "saved question" and not machine.sessions
    replay = await machine._handle(GetHistory(session_id=ctx.key))
    assert all("ts" not in event for event in replay.events)
    await machine._handle(SwitchSession(session_id=ctx.key, engine="antigravity"))
    assert machine.focused_sid == ctx.key and len(client.prompts) == before
    assert deserialize(serialize(catalog)).antigravity_commands[0].name == "compact"


@pytest.mark.asyncio
async def test_gemini_history_replay_keeps_original_message_times(wire, tmp_path):
    machine, _, _, client = wire
    ctx = await new_session(machine)
    native_id = ctx.sdk.native_id
    client.rows[native_id] = {"sessionId": native_id, "cwd": client.cwd}
    agent_home = tmp_path / "agy-home"
    client.launch = SimpleNamespace(default_model="neon", environment={"HOME": str(agent_home)})
    log = agent_home / ".gemini/antigravity-cli/brain" / native_id / ".system_generated/logs/transcript.jsonl"
    log.parent.mkdir(parents=True)
    records = [
        {"type": "USER_INPUT", "created_at": "2026-01-01T00:00:00Z",
         "content": "<USER_REQUEST>\nsaved question\n</USER_REQUEST>\n<ADDITIONAL_METADATA>fixture</ADDITIONAL_METADATA>"},
        {"type": "PLANNER_RESPONSE", "created_at": "2026-01-01T00:00:04Z", "content": "saved answer"},
    ]
    log.write_text("".join(json.dumps(row) + "\n" for row in records))
    await machine._detach_idle_session(ctx.key)

    history = await machine._handle(GetHistory(session_id=ctx.key, detail="summary"))
    turn, = history.turns
    assert (turn.ts, turn.doneTs) == (1767225600000, 1767225604000)
    detail = await machine._handle(GetHistory(session_id=ctx.key))
    assert next(row["ts"] for row in detail.events if row["type"] == "turn_end") == 1767225604

    session_caps = {**client.capabilities["sessionCapabilities"], "resume": {}}
    client.capabilities = {**client.capabilities, "sessionCapabilities": session_caps}
    await machine._handle(SwitchSession(session_id=ctx.key, engine="antigravity"))
    resumed = await machine._handle(GetHistory(session_id=ctx.key, detail="summary"))
    assert [(item.ts, item.doneTs) for item in resumed.turns] == [(turn.ts, turn.doneTs)]


@pytest.mark.asyncio
async def test_gemini_presentation_and_read_only_extensions(wire, monkeypatch, tmp_path):
    machine, _, _, client = wire
    ctx = await new_session(machine)
    native = ctx.sdk.native_id
    client.rows[native] = {"sessionId": native, "cwd": client.cwd, "title": "Native title"}
    await machine._handle(RenameSession(engine="antigravity", session_id=ctx.key, title=" Gemini plan "))
    machine._session_presentation = SessionPresentationStore(Path(machine.cfg.state_dir))
    listing = await machine._handle(ListSessions(engine="antigravity"))
    assert listing.sessions[0].summary == "Gemini plan"
    assert client.rows[native]["title"] == "Native title"

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    project = tmp_path / ".agents"
    skill = project / "skills" / "review"
    plugin = tmp_path / ".gemini" / "config" / "plugins" / "fixture"
    skill.mkdir(parents=True)
    plugin.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: review\ndescription: Review changes\n---\n")
    (plugin / "plugin.json").write_text(json.dumps({"name": "fixture", "displayName": "Fixture plugin"}))
    (project / "mcp_config.json").write_text(json.dumps({
        "mcpServers": {"read-only-name": {"headers": {"Authorization": "secret-only-in-config"}}},
    }))
    (project / "hooks.json").write_text(json.dumps({
        "check": {"enabled": False, "PreToolUse": [{"hooks": [{"command": "secret-only-in-config"}]}]},
    }))
    extensions = await machine._handle(GetEngineCapabilities(engine="antigravity", cwd=client.cwd))
    assert {item.kind: item.name for item in extensions.items} == {
        "skill": "review", "mcp": "read-only-name", "hook": "check", "plugin": "Fixture plugin",
    }
    assert not any(item.actions for item in extensions.items)
    assert "secret-only-in-config" not in extensions.model_dump_json()
    skills = await machine._handle(GetEngineCapabilities(engine="antigravity", cwd=client.cwd, skills_only=True))
    assert [item.name for item in skills.items] == ["review"]


@pytest.mark.asyncio
async def test_gemini_sign_in_is_scoped_deduplicated_and_blocks_active_turns(wire):
    machine, _, _, client = wire
    info = await machine._handle(AntigravityAuth(cmd_id="discover", client_id="browser"))
    assert info.to == "browser" and info.request_id == "discover"
    assert info.methods == ["oauth-personal", "oauth-business"] and not info.signed_in
    ctx = await new_session(machine)
    await machine._handle(Query(sid=ctx.key, msg_id="in-progress", prompt="Test"))
    cmd = AntigravityAuth(method="oauth-personal", cmd_id="login", client_id="browser")
    busy = await machine._handle(cmd)
    assert busy.error and not busy.signed_in
    client.prompts[-1][1].set_result({"stopReason": "end_turn"})
    await ctx.turn_task
    signin = await machine._handle(cmd)
    assert signin.signed_in and signin.to == "browser"
    assert (await machine._handle(cmd)).signed_in
    assert [args for method, args in client.calls if method == "authenticate"] == [{"methodId": "oauth-personal"}]
    assert deserialize(serialize(signin)) == signin


@pytest.mark.asyncio
@pytest.mark.parametrize("reliable", [False, True], ids=["dispatch", "reliable"])
async def test_managed_gemini_sign_in_refreshes_quota_before_the_worker_reply(managed_gemini, monkeypatch, reliable):
    wrapper, transport, _, service, native = managed_gemini
    login_started, finish_login, auth_result_sent, finish_reply = (asyncio.Event() for _ in range(4))
    current_plan = "Starter"
    plans_read = []
    login = AntigravityAuth(method="oauth-personal", client_id="browser", cmd_id="login")
    quota = GetAntigravityQuota(client_id="browser", cmd_id="quota")

    def read_quota(_launch):
        plans_read.append(current_plan)
        return protocol.AntigravityQuota(plan=current_plan)

    native_call = native.call

    async def sign_in(method, args, *, timeout=30):
        nonlocal current_plan
        if method == "authenticate":
            login_started.set()
            await finish_login.wait()
            current_plan = "Pro"
        return await native_call(method, args, timeout=timeout)

    send = transport.send

    async def observe_result(event):
        await send(event)
        if event.type == "antigravity_auth_result" and event.request_id == login.cmd_id and event.signed_in:
            auth_result_sent.set()

    write = service.transport.write

    async def hold_reply(writer, frame):
        if "reply" in frame:
            await finish_reply.wait()
        await write(writer, frame)

    monkeypatch.setattr(wrapper._antigravity, "launch_factory", lambda: AntigravityLaunch("/local/agy", {}))
    monkeypatch.setattr(antigravity_quota, "read_native_quota", read_quota)
    monkeypatch.setattr(native, "call", sign_in)
    monkeypatch.setattr(transport, "send", observe_result)
    monkeypatch.setattr(service.transport, "write", hold_reply)

    assert (await wrapper._handle(quota)).plan == "Starter"
    handle_login = wrapper._process_command if reliable else wrapper._handle
    task = asyncio.create_task(handle_login(login))
    try:
        async with asyncio.timeout(3):
            await login_started.wait()
            during_login = await wrapper._handle(quota)
            assert during_login.error == "unavailable" and during_login.plan is None
            finish_login.set()
            await auth_result_sent.wait()
            assert not task.done()
            assert (await wrapper._handle(quota)).plan == "Pro"
    finally:
        finish_login.set()
        finish_reply.set()
        await asyncio.wait_for(task, timeout=3)

    assert (await wrapper._handle(quota)).plan == "Pro"
    assert plans_read == ["Starter", "Pro"]
    assert any(event.type == "command_ack" and event.cmd_id == login.cmd_id and event.to == login.client_id
               for event in transport.sent) == reliable


@pytest.mark.asyncio
@pytest.mark.parametrize("trust_all", [False, True], ids=["explicit-paths", "all-workspaces"])
async def test_independent_gemini_service_keeps_running_question_across_wrapper_replacement(wire, monkeypatch, trust_all):
    class NewerHello(Hello):
        optional_display_hint: bool | None = None

    class NewerAvailability(protocol.AntigravityAvailability):
        optional_worker_label: str = "Gemini"

    worker, _, runtime, client = wire
    transport = worker.transport = ServiceTransport()
    service = GeminiService(worker, transport)
    with TemporaryDirectory(prefix="ccr-gemini-", dir="/tmp") as directory:
        path = Path(directory) / "service.sock"
        listener = await asyncio.start_unix_server(service.connection, str(path))
        path.chmod(0o600)
        first, _ = _mk_machine()
        first.cfg.antigravity_trusted_workspaces = '["*"]' if trust_all else worker.cfg.antigravity_trusted_workspaces
        trust_workspaces(worker)
        first._gemini_controller = GeminiController(path, first)
        second, second_transport = _mk_machine()
        second.cfg.antigravity_trusted_workspaces = first.cfg.antigravity_trusted_workspaces
        second._gemini_controller = GeminiController(path, second)
        try:
            for version, workspaces in ((2, "[]"), (GEMINI_SERVICE_VERSION, '["*"]')):
                reader, writer = await asyncio.open_unix_connection(str(path))
                try:
                    await write_frame(writer, {
                        "op": "attach", "protocol": version, "machine": worker.cfg.machine_id,
                        "runtime": {"session_messages": False, "environment": {}, "trusted_workspaces": workspaces},
                    })
                    assert (await read_frame(reader))["error"] == "scope"
                finally:
                    writer.close()
                    await writer.wait_closed()
            assert worker.cfg.antigravity_trusted_workspaces == "[]"
            focus = await first._handle(NewSession(engine="antigravity", cwd=client.cwd))
            assert worker.cfg.antigravity_trusted_workspaces == first.cfg.antigravity_trusted_workspaces
            sid = focus.session_id
            query = Query(sid=sid, msg_id="service-query", prompt="Keep one running turn",
                          cmd_id="service-command", client_id="browser-client")
            await first._process_command(query)
            ctx = worker.sessions[sid]
            queued = Query(sid=sid, msg_id="accepted-queue", prompt="Finish accepted queue", delivery="queue",
                           cmd_id="service-queue", client_id="browser-client")
            await first._process_command(queued)
            assert [item.msg_id for item in ctx.queued_queries] == [queued.msg_id]
            async with service_control(path, worker.cfg.machine_id, "drain") as reply:
                assert reply["error"] == "busy"
            await first._gemini_controller.close()

            async with asyncio.timeout(2):
                while transport.writer is not None:
                    await asyncio.sleep(.001)
            assert not first.sessions and ctx.sdk.turn is not None
            response = asyncio.create_task(runtime.on_request("session/request_permission", 15, {
                "sessionId": ctx.sdk.native_id, "toolCall": {"title": "fixture command"},
                "options": [{"optionId": "allow", "kind": "allow_once", "name": "Allow"}],
            }))
            async with asyncio.timeout(2):
                while not ctx.pending_asks:
                    await asyncio.sleep(.001)
            monkeypatch.setattr(protocol, "PROTOCOL_VERSION", protocol.PROTOCOL_VERSION + 1)
            monkeypatch.setenv("CC_REMOTE_ANTIGRAVITY", "0")
            hello = NewerHello(role="client", client_id="replacement", discover_engines=True)
            assert service_message(hello)["optional_display_hint"] is None
            await second._handle(hello)
            assert service.environment["CC_REMOTE_ANTIGRAVITY"] == "0"
            assert any(frame.type == "antigravity_availability" and not frame.enabled and frame.to == "replacement"
                       for frame in second_transport.sent)
            create = await second._handle(NewSession(engine="antigravity", cwd=client.cwd))
            assert create.code == "gemini_unavailable"
            extra = await second._handle(Query(sid=sid, msg_id="new-after-opt-out", prompt="New work"))
            assert extra.code == "gemini_unavailable"
            await transport.send(NewerAvailability(enabled=True, to="extended-event"))
            await second._process_command(query)
            assert len(client.prompts) == 1
            assert any(frame.type == "command_ack" and frame.cmd_id == query.cmd_id
                       for frame in second_transport.sent)
            assert any(frame.type == "antigravity_availability" and frame.enabled and frame.to == "extended-event"
                       and type(frame) is protocol.AntigravityAvailability for frame in second_transport.sent)
            ask, = [frame for frame in second_transport.sent if frame.type == "ask_user"]
            assert ask.to == "replacement" and ask.sid == sid
            assert any(frame.type == "snapshot" and frame.sid == sid and frame.state == "running"
                       for frame in second_transport.sent)
            assert not any(frame.type == "btw_sync" for frame in second_transport.sent[1:])
            await second._handle(AnswerQuestion(sid=sid, ask_id=ask.ask_id, answers=["Allow"]))
            assert (await asyncio.wait_for(response, 2))["outcome"]["optionId"] == "allow"
            await client.update(ctx.sdk.native_id, "agent_message_chunk",
                                content={"type": "text", "text": "Original work finished"})
            client.prompts[0][1].set_result({"stopReason": "end_turn"})
            await ctx.turn_task
            await prompted(client, 2)
            request, outcome = client.prompts[1]
            assert request["prompt"] == [{"type": "text", "text": queued.prompt}]
            outcome.set_result({"stopReason": "end_turn"})
            await ctx.turn_task
            snapshot = await second._handle(GetHistory(session_id=sid, detail="summary"))
            assert [turn.prompt for turn in snapshot.turns] == [query.prompt, queued.prompt]
            assert snapshot.turns[0].blocks[-1]["text"] == "Original work finished"
            async with service_control(path, worker.cfg.machine_id, "drain") as reply:
                assert reply["draining"] and runtime.admission_paused()
                with pytest.raises(AcpError, match="正在切换"):
                    await second._gemini_controller.list_sessions()
            async with asyncio.timeout(1):
                while runtime.admission_paused():
                    await asyncio.sleep(.001)
            assert (await second._gemini_controller.list_sessions())[0].session_id == sid
        finally:
            await first._gemini_controller.close()
            await second._gemini_controller.close()
            await first._command_scheduler.close()
            await second._command_scheduler.close()
            listener.close()
            await listener.wait_closed()
            await service.close()


def test_service_messages_do_not_carry_browser_protocol_version(monkeypatch):
    transfer = service_message(Query(sid="agy@fixture", msg_id="one", prompt="Keep the same native input"))
    assert "v" not in transfer
    newer = protocol.PROTOCOL_VERSION + 1
    monkeypatch.setattr(protocol, "PROTOCOL_VERSION", newer)
    decoded = local_message(transfer)
    assert (decoded.v, decoded.sid, decoded.msg_id) == (newer, "agy@fixture", "one")
    extended = {**transfer, "future_optional_field": None}
    assert local_message(extended) == decoded
    with pytest.raises(protocol.ProtocolError, match="extra_forbidden"):
        deserialize(json.dumps({**extended, "v": newer}))
    with pytest.raises(AcpError, match="格式不兼容"):
        local_message({**extended, "prompt": {"invalid": "type"}})


def test_service_reuses_wrapper_gemini_state_and_imports_sidebars():
    wrapper, _ = _mk_machine()
    worker, _ = _mk_machine()
    owner = wrapper.cfg.state_dir
    sid = "agy@fixture"
    try:
        SessionPinStore(owner).set_pinned("antigravity", sid, True)
        legacy = SessionPresentationStore(owner)
        legacy.set_title("antigravity", sid, "Saved name")
        legacy.set_hidden("antigravity", sid, True)
        legacy.set_title("claude", "private", "Other harness")
        wrapper._antigravity.control_store.save(sid, {"model": "neon", "mode": "plan"})

        worker.cfg.antigravity_owner_state_dir = owner
        service_runtime = AntigravityRuntime(worker, launch_factory=lambda: None)
        metadata = service_runtime.metadata.get(sid)
        assert (metadata.pinned, metadata.hidden, metadata.title) == (True, True, "Saved name")
        assert set(service_runtime.metadata.all()) == {sid}
        assert service_runtime.control_store.get(sid) == {"model": "neon", "mode": "plan"}
        service_runtime.metadata.set_hidden(sid, False)
        assert not wrapper._antigravity.metadata.get(sid).hidden
        assert SessionPresentationStore(owner).get("claude", "private").custom_title == "Other harness"
    finally:
        worker._test_state_tmp.cleanup()
        wrapper._test_state_tmp.cleanup()


@pytest.mark.asyncio
async def test_installed_service_waits_for_original_wrapper_message_receiver(wire, monkeypatch):
    worker, _, runtime, _ = wire
    wrapper, _ = _mk_machine()
    wrapper.cfg.session_messages_enabled = True
    # The installed worker environment starts without the feature setting.
    assert not worker.cfg.session_messages_enabled
    owner = SessionMessages(wrapper)
    await owner.start()
    transport = worker.transport = ServiceTransport()
    service = GeminiService(worker, transport, message_state=wrapper.cfg.state_dir)
    with TemporaryDirectory(prefix="ccr-gemini-", dir="/tmp") as directory:
        path = Path(directory) / "service.sock"
        listener = await asyncio.start_unix_server(service.connection, str(path))
        path.chmod(0o600)
        controller = GeminiController(path, wrapper)
        try:
            async with service_control(path, worker.cfg.machine_id, "status") as status:
                assert status["ready"]
            assert worker._session_messages is None
            reader, writer = await asyncio.open_unix_connection(str(path))
            try:
                await write_frame(writer, {"op": "attach", "protocol": GEMINI_SERVICE_VERSION,
                                          "machine": worker.cfg.machine_id,
                                          "runtime": {"session_messages": True, "environment": {}, "trusted_workspaces": "[]"}})
                assert (await read_frame(reader))["error"] == "occupied"
            finally:
                writer.close()
                await writer.wait_closed()
            assert worker._session_messages is None
            await owner.close()
            monkeypatch.setenv("PATH", "/wrapper/bin:/usr/bin")
            monkeypatch.setenv("HTTP_PROXY", "http://fixture.invalid")
            monkeypatch.setenv("no_proxy", "localhost")
            monkeypatch.setenv("WRAPPER_TOKEN", "fixture-relay-secret")
            monkeypatch.setenv("CC_REMOTE_SESSION_MESSAGES", "0")
            assert await controller.list_sessions() == []
            bus = worker._session_messages
            assert bus.enabled and bus.gemini_worker and not runtime.admission_paused()
            assert service.environment["PATH"] == "/wrapper/bin:/usr/bin"
            assert service.environment["HTTP_PROXY"] == "http://fixture.invalid"
            assert service.environment["no_proxy"] == "localhost"
            assert "WRAPPER_TOKEN" not in service.environment
            # A later Wrapper can turn the feature off while the worker owns
            # an in-flight receipt. Let it settle without admitting new work.
            source = reference("codex", "some-source")
            target = reference("antigravity", "agy@fixture")
            receipt, _ = bus.store.claim(source, "in-flight", target, "Already submitted", queue=True)
            assert bus.store.begin_delivery(receipt["message_id"])
            bus.active.add(receipt["message_id"])
            await controller.close()
            wrapper.cfg.session_messages_enabled = False
            monkeypatch.delenv("HTTP_PROXY")
            assert await controller.list_sessions() == []
            assert worker._session_messages is bus and not bus.enabled
            assert "HTTP_PROXY" not in service.environment
            with pytest.raises(ValueError, match="disabled"):
                await bus.call("ccr:antigravity:agy@fixture", "list_sessions", {})
            bus.accepted(receipt["message_id"])
            assert bus.store.get(source, "in-flight")["status"] == "accepted"
            await controller.close()
            wrapper.cfg.session_messages_enabled = True
            assert await controller.list_sessions() == []
            assert bus.enabled
        finally:
            await controller.close()
            if not owner.closing:
                await owner.close()
            listener.close()
            await listener.wait_closed()
            await service.close()


@pytest.mark.asyncio
async def test_gemini_controller_waits_for_previous_wrapper_to_release_service(wire):
    machine, _, _, _ = wire
    attaches = []
    commands = []

    async def accept(reader, writer):
        try:
            attach = await read_frame(reader)
            attaches.append(attach)
            if attach["protocol"] != 3:
                await write_frame(writer, {"error": "scope"})
                assert await reader.read() == b""
                return
            if len(attaches) == 1:
                await write_frame(writer, {"error": "occupied"})
                assert await reader.read() == b""
                return
            await write_frame(writer, {"attached": True})
            request = await read_frame(reader)
            commands.append(request["op"])
            await write_frame(writer, {"id": request["id"], "reply": []})
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    with TemporaryDirectory(prefix="ccr-gemini-", dir="/tmp") as directory:
        path = Path(directory) / "service.sock"
        listener = await asyncio.start_unix_server(accept, str(path))
        path.chmod(0o600)
        controller = GeminiController(path, machine)
        try:
            assert await controller.list_sessions() == []
            assert len(attaches) == 2 and all(frame["op"] == "attach" for frame in attaches)
            await controller.close()
            machine.cfg.antigravity_trusted_workspaces = '["*"]'
            with pytest.raises(AcpError, match="--upgrade") as exc:
                await controller.list_sessions()
            assert exc.value.code == "service_workspace_trust"
            assert attaches[-1]["protocol"] == 4
            assert commands == ["list_sessions"]
        finally:
            await controller.close()
            listener.close()
            await listener.wait_closed()


@pytest.mark.asyncio
async def test_catalog_reopens_one_blank_probe_across_scopes_and_wrapper_restart(wire, tmp_path):
    machine, _, runtime, client = wire
    control = tmp_path / "private-control"
    control.mkdir()
    client.cwd = str(control.resolve())
    other = tmp_path / "other"
    other.mkdir()
    trust_workspaces(machine)
    # The old probe may have been opened in a project. Never reload its ID.
    (runtime.catalog_path.parent / "antigravity-catalog.json").write_text('{"session_id":"old-project-probe"}')
    await machine._handle(GetModels(engine="antigravity", cwd=client.cwd))
    await machine._handle(GetModels(engine="antigravity", cwd=client.cwd, antigravity_model="flash"))
    await machine._handle(GetModels(engine="antigravity", cwd=str(other)))
    runtime = machine._antigravity = AntigravityRuntime(machine)
    runtime.client = client
    client.remote = runtime
    catalog = await machine._handle(GetModels(engine="antigravity", cwd=str(other), antigravity_model="flash"))
    assert catalog.default_model == "neon"
    assert sum(method == "session/new" for method, _ in client.calls) == 1
    loaded = [args for method, args in client.calls if method == "session/load"]
    assert len(loaded) == 2 and len({args["sessionId"] for args in loaded}) == 1
    assert loaded[-1]["sessionId"] != "old-project-probe"
    assert {args["cwd"] for method, args in client.calls if method in {"session/new", "session/load"}} == {client.cwd}
    assert all(not args["mcpServers"] for method, args in client.calls
               if method in {"session/new", "session/load"})
    assert not machine.sessions and not client.prompts and not client.rows


@pytest.mark.asyncio
async def test_cold_load_restores_confirmed_controls_before_publishing(wire, tmp_path):
    machine, transport, runtime, client = wire
    client.capabilities = {
        **client.capabilities, "_meta": {"antigravity": {"systemPrompt": {"append": True}}},
        "sessionCapabilities": {**client.capabilities["sessionCapabilities"], "resume": {}},
    }
    ctx = await new_session(machine, model="flash", antigravity_mode="plan")
    native = ctx.sdk.native_id
    client.controls[native]["effort"] = "extra"
    await client.update(native, "config_option_update", configOptions=client.options(native)["configOptions"])
    await machine._handle(Query(sid=ctx.key, msg_id="seed", prompt="Keep this session"))
    client.prompts[-1][1].set_result({"stopReason": "end_turn"})
    await ctx.turn_task
    launch, = [params for method, params in client.calls if method == "session/new"]
    instructions = launch["_meta"]["antigravity"]["systemPrompt"]["append"]
    assert "ask_user" in instructions and "clickable Markdown links" in instructions
    await runtime.close()
    machine.sessions.clear()

    restarted = machine._antigravity = AntigravityRuntime(machine, launch_factory=lambda: True)
    cold = ScriptedAcp(restarted, tmp_path)
    cold.capabilities = client.capabilities
    cold.controls[native] = {"model": "neon", "mode": "default", "effort": "low"}
    cold.rows, cold.directories = client.rows, client.directories
    restarted.client = cold
    try:
        history = await machine._handle(GetHistory(session_id=ctx.key, detail="summary"))
        assert history.turns[0].prompt == "saved question"
        history_load, = [params for method, params in cold.calls if method == "session/load"]
        assert history_load["mcpServers"] == [] and "_meta" not in history_load
        transport.sent.clear()
        await machine._handle(SwitchSession(engine="antigravity", session_id=ctx.key))
        loaded = machine.sessions[ctx.key].sdk
        assert (loaded.model, loaded.effort, loaded.permission_mode) == ("flash", "extra", "plan")
        assert {frame.model for frame in transport.sent if frame.type == "model"} == {"flash"}
        assert {frame.mode for frame in transport.sent if frame.type == "perm"} == {"plan"}
        assert [(params["configId"], params["value"]) for method, params in cold.calls
                if method == "session/set_config_option"] == [
            ("model", "flash"), ("effort", "extra"), ("mode", "plan"),
        ]
        reopened, = [params for method, params in cold.calls if method == "session/resume"]
        assert reopened["_meta"] == launch["_meta"]
        assert len([1 for method, _ in cold.calls if method == "session/load"]) == 1
        await machine._handle(Query(sid=ctx.key, msg_id="after-resume", prompt="Continue"))
        cold.prompts[-1][1].set_result({"stopReason": "end_turn"})
        await machine.sessions[ctx.key].turn_task
        replay = await machine._handle(GetHistory(session_id=ctx.key, detail="summary"))
        assert [turn.prompt for turn in replay.turns] == ["saved question", "Continue"]
        assert len([1 for method, _ in cold.calls if method == "session/load"]) == 2
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_missing_saved_permission_requires_explicit_reselection(wire, monkeypatch):
    machine, transport, runtime, client = wire
    ctx = await new_session(machine, model="flash", antigravity_mode="plan")
    native = ctx.sdk.native_id
    await ctx.sdk.disconnect()
    client.controls[native]["mode"] = "default"
    options = client.options

    def only_default(sid):
        result = options(sid)
        mode = next(row for row in result["configOptions"] if row["category"] == "mode")
        mode["options"] = [choice for choice in mode["options"] if choice["value"] == "default"]
        return result

    monkeypatch.setattr(client, "options", only_default)
    await machine._handle(SwitchSession(engine="antigravity", session_id=ctx.key))
    assert any(frame.type == "notice" and frame.notice_id == "gemini-controls-restore"
               for frame in transport.sent)
    assert runtime.control_store.get(ctx.key)["mode"] == "plan"
    blocked = await machine._handle(Query(sid=ctx.key, msg_id="blocked", prompt="Run something"))
    assert blocked.code == "gemini_controls_restore" and not client.prompts
    await machine._handle(SetAntigravityOption(sid=ctx.key, category="mode", value="default"))
    assert runtime.control_store.get(ctx.key)["mode"] == "default"
    await machine._handle(Query(sid=ctx.key, msg_id="confirmed", prompt="Continue with Default"))
    client.prompts[-1][1].set_result({"stopReason": "end_turn"})
    await ctx.turn_task


@pytest.mark.asyncio
async def test_missing_worktree_history_remains_readable_and_hidden_row_is_reversible(wire, tmp_path):
    machine, _, _, client = wire
    worktree = tmp_path / "removed"
    worktree.mkdir()
    trust_workspaces(machine, worktree)
    ctx = await new_session(machine, cwd=str(worktree))
    await machine._handle(Query(sid=ctx.key, msg_id="old", prompt="old"))
    client.prompts[0][1].set_result({"stopReason": "end_turn"})
    await ctx.turn_task
    await machine._detach_idle_session(ctx.key)
    worktree.rmdir()

    history = await machine._handle(GetHistory(session_id=ctx.key, detail="summary"))
    assert history.error is None and history.turns[0].prompt == "saved question"
    focus = await machine._handle(SwitchSession(engine="antigravity", session_id=ctx.key))
    assert focus.session_id == ctx.key
    assert machine.sessions[ctx.key].write_state == "read_only"
    rejected = await machine._handle(Query(sid=ctx.key, msg_id="while-removed", prompt="continue"))
    assert rejected.code == "gemini_directory" and len(client.prompts) == 1

    await machine._handle(ArchiveSession(session_id=ctx.key, archived=True, engine="antigravity"))
    assert machine._antigravity.metadata.get(ctx.key).hidden
    assert (await machine._handle(ListSessions(engine="antigravity"))).sessions[0].tag == "archived"
    assert client.rows[ctx.sdk.native_id]["cwd"] == str(worktree.resolve())
    await machine._handle(ArchiveSession(session_id=ctx.key, archived=False, engine="antigravity"))
    assert (await machine._handle(ListSessions(engine="antigravity"))).sessions[0].tag is None
    worktree.mkdir()
    await machine._handle(SwitchSession(engine="antigravity", session_id=ctx.key))
    assert machine.sessions[ctx.key].write_state == "writable"


@pytest.mark.asyncio
async def test_native_file_diffs_accumulate_by_real_path_and_omit_empty_output(wire):
    machine, transport, _, client = wire
    ctx = await new_session(machine)
    await machine._handle(Query(sid=ctx.key, msg_id="edit", prompt="edit files"))
    paths = [f"{ctx.cwd}/a.py", f"{ctx.cwd}/b file.py"]
    for index, path in enumerate(paths):
        native = f"edit-{index}"
        await client.update(ctx.sdk.native_id, "tool_call", toolCallId=native, kind="edit",
                            title=Path(path).name, rawInput={"Path": path}, status="in_progress")
        await client.update(ctx.sdk.native_id, "tool_call_update", toolCallId=native, status="completed",
                            rawOutput={}, content=[{"type": "diff", "path": path,
                                                   "oldText": "before\n", "newText": "after\n"}])
    client.prompts[0][1].set_result({"stopReason": "end_turn"})
    await ctx.turn_task
    summary = [frame for frame in transport.sent if frame.type == "turn_file_changes"][-1].changes
    assert [(row.path, row.additions, row.deletions) for row in summary.files] == [(path, 1, 1) for path in paths]
    results = [frame for frame in transport.sent if frame.type == "tool_result"]
    assert len(results) == 2 and all(result.content == "" for result in results)


@pytest.mark.asyncio
async def test_steer_retains_tool_owner_in_live_events_and_history(wire):
    machine, transport, _, client = wire
    ctx = await new_session(machine)
    await machine._handle(Query(sid=ctx.key, msg_id="original", prompt="edit fixture"))
    await client.update(ctx.sdk.native_id, "tool_call", kind="edit", toolCallId="native-edit", status="in_progress")
    await machine._handle(Steer(sid=ctx.key, msg_id="supplement", prompt="explain",
                                cmd_id="steer", client_id="web"))
    pending = await machine._handle(GetHistory(session_id=ctx.key, detail="summary"))
    tool = next(block for block in pending.turns[0].blocks if block["kind"] == "tool")
    assert pending.turns[0].done and not tool["done"]
    await client.update(ctx.sdk.native_id, "tool_call_update", toolCallId="native-edit",
                        status="completed", rawOutput={"message": "saved"})
    for _, receipt in client.prompts:
        receipt.set_result({"stopReason": "end_turn"})
    await ctx.turn_task
    use, = [frame for frame in transport.sent if frame.type == "tool_use"]
    result, = [frame for frame in transport.sent if frame.type == "tool_result"]
    assert result.tool_use_id == use.tool_use_id and result.turn_id == "original"
    history = await machine._handle(GetHistory(session_id=ctx.key, detail="summary"))
    assert [row.id for row in history.turns] == ["original", "supplement"]
    tool = next(block for block in history.turns[0].blocks if block["kind"] == "tool")
    assert tool["done"] and tool["result"]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_native_background_completion_keeps_exact_tool_owner_after_later_prompt(wire):
    machine, transport, _, client = wire
    ctx = await new_session(machine)
    native = ctx.sdk.native_id
    await machine._handle(Query(sid=ctx.key, msg_id="launch", prompt="Start local fixture"))
    await client.update(native, "tool_call", toolCallId="native-command", name="run_command",
                        title="Native fixture", kind="execute", status="in_progress",
                        rawInput={"CommandLine": "/bin/sleep 1", "toolSummary": "Fixture wait", "IsDaemon": True})
    client.prompts[0][1].set_result({"stopReason": "end_turn"})
    await ctx.turn_task
    assert ctx.state == "idle" and not machine._session_idle(ctx.key, ctx)
    sync = [frame for frame in transport.sent if frame.type == "background_process_sync"][-1]
    assert len(sync.items) == 1
    assert (sync.items[0].turn_id, sync.items[0].command) == ("launch", "/bin/sleep 1")
    running_history = await machine._handle(GetHistory(session_id=ctx.key, detail="summary"))
    assert not next(block for block in running_history.turns[0].blocks if block["kind"] == "tool")["done"]
    await machine._handle(Hello(role="client", client_id="fresh-client"))
    assert any(frame.type == "background_process_sync" and frame.to == "fresh-client"
               and frame.items == sync.items for frame in transport.sent)

    await machine._handle(Query(sid=ctx.key, msg_id="next", prompt="Next question"))
    await client.update(native, "tool_call_update", toolCallId="native-command", status="completed",
                        rawOutput={"output": "Command exited with code 23.", "exit_code": 23})
    result, = [frame for frame in transport.sent if frame.type == "tool_result"]
    assert result.turn_id == "launch" and result.exit_code == 23 and result.is_error
    assert [frame for frame in transport.sent if frame.type == "background_process_sync"][-1].items == []
    assert ctx.state == "running" and ctx.active_msg_id == "next"
    client.prompts[-1][1].set_result({"stopReason": "end_turn"})
    await ctx.turn_task
    history = await machine._handle(GetHistory(session_id=ctx.key, detail="summary"))
    assert [row.id for row in history.turns] == ["launch", "next"]
    command, = [block for block in history.turns[0].blocks if block["kind"] == "tool"]
    assert command["result"]["exit_code"] == 23


@pytest.mark.asyncio
async def test_background_wakeup_preserves_reply_and_approvals_without_another_native_prompt(wire, monkeypatch):
    machine, transport, runtime, client = wire
    monkeypatch.setattr(antigravity_runtime, "_FOLLOWUP_NOTICE_AFTER", .03)
    ctx = await new_session(machine)
    native = ctx.sdk.native_id
    await machine._handle(Query(sid=ctx.key, msg_id="launch", prompt="Start the fixture"))
    await client.update(native, "tool_call", toolCallId="native-command", name="run_command",
                        title="Fixture command", kind="execute", status="in_progress")
    client.prompts[-1][1].set_result({"stopReason": "end_turn"})
    await ctx.turn_task
    assert ctx.state == "idle"

    transport.sent.clear()
    await client.update(native, "tool_call_update", toolCallId="native-command", status="completed",
                        rawOutput={"exit_code": 23})
    result, = [frame for frame in transport.sent if frame.type == "tool_result"]
    assert result.turn_id == "launch" and result.exit_code == 23
    assert ctx.state == "running" and not ctx.sdk.transcript.background
    assert machine._ui_updates.busy_count() == 1

    interaction = asyncio.create_task(runtime.on_request("session/request_permission", 20, {
        "sessionId": native, "toolCall": {"title": "Confirm fixture edit"},
        "options": [{"optionId": "native-allow", "kind": "allow_once", "name": "Allow once"}],
    }))
    async with asyncio.timeout(1):
        while not ctx.pending_asks:
            await asyncio.sleep(.001)
    binding, = [frame for frame in transport.sent if frame.type == "turn_binding"]
    assert binding.autonomous and binding.turn_id != "launch"
    assert ctx.state == "running" and machine._ui_updates.busy_count() == 1
    task = ctx.turn_task
    await asyncio.sleep(.06)
    assert not task.done()
    ask_id, = ctx.pending_asks
    await machine._handle(AnswerQuestion(sid=ctx.key, ask_id=ask_id, answers=["Allow once"]))
    assert (await interaction)["outcome"]["optionId"] == "native-allow"

    await client.update(native, "tool_call", toolCallId="native-edit", name="write_to_file",
                        title="Fixture edit", kind="edit", status="in_progress")
    await asyncio.sleep(.06)
    assert not task.done()
    await client.update(native, "tool_call_update", toolCallId="native-edit", status="completed")
    await client.update(native, "agent_message_chunk", content={"type": "text", "text": "Fixture finished."})
    await asyncio.sleep(.06)
    assert not task.done() and ctx.state == "running" and machine._ui_updates.busy_count() == 1
    assert not any(frame.type == "turn_end" for frame in transport.sent)
    assert len(client.prompts) == 1 and not any(frame.type == "user_msg" for frame in transport.sent)
    page = await machine._handle(GetHistory(session_id=ctx.key, detail="summary"))
    assert [row.id for row in page.turns] == ["launch", binding.turn_id]
    assert page.turns[-1].prompt == ""
    assert any(block.get("text") == "Fixture finished." for block in page.turns[-1].blocks)
    # A missing working directory can make a live session read-only before
    # the engine reports its terminal. Its old turn still has to settle.
    ctx.write_state = "read_only"
    await runtime.on_native_lifecycle(client, native, "stop", "NO_TOOL_CALL")
    await asyncio.wait_for(task, 1)
    end, = [frame for frame in transport.sent if frame.type == "turn_end"]
    assert (end.turn_id, end.result.subtype) == (binding.turn_id, "background_followup")
    assert end.notification_context.engine == "antigravity"
    assert ctx.state == "idle" and machine._ui_updates.busy_count() == 0


@pytest.mark.asyncio
async def test_native_stop_releases_queue_without_closing_other_daemons(wire, monkeypatch):
    machine, transport, runtime, client = wire
    monkeypatch.setattr(antigravity_runtime, "_FOLLOWUP_STOP_SETTLE", .03)
    ctx = await new_session(machine)
    original_handle = ctx.sdk
    native = ctx.sdk.native_id
    await machine._handle(Query(sid=ctx.key, msg_id="launch", prompt="Start two daemons"))
    for tool in ("quick", "slow"):
        await client.update(native, "tool_call", toolCallId=tool, name="run_command",
                            kind="execute", status="in_progress", rawInput={"IsDaemon": True})
    client.prompts[0][1].set_result({"stopReason": "end_turn"})
    await ctx.turn_task
    transport.sent.clear()
    await client.update(native, "tool_call_update", toolCallId="quick", status="completed",
                        rawOutput={"exit_code": 23})
    followup = ctx.active_msg_id
    await machine._handle(Query(sid=ctx.key, msg_id="queued", prompt="Next question",
                                delivery="queue", cmd_id="queue", client_id="browser"))
    await client.update(native, "agent_message_chunk", content={"type": "text", "text": "Early reply"})
    await runtime.on_native_lifecycle(object(), native, "stop", "NO_TOOL_CALL")
    assert ctx.sdk.turn.native_stop is None
    await runtime.on_native_lifecycle(client, native, "stop", "NO_TOOL_CALL")
    await runtime.on_native_lifecycle(client, native, "invoke", "")
    await asyncio.sleep(.06)
    assert len(client.prompts) == 1 and ctx.queued_queries

    await client.update(native, "agent_message_chunk", content={"type": "text", "text": "Final continuation"})
    await runtime.on_native_lifecycle(client, native, "stop", "NO_TOOL_CALL")
    await prompted(client, 2)
    end, = [frame for frame in transport.sent if frame.type == "turn_end"]
    assert end.turn_id == followup and end.result.subtype == "background_followup"
    assert ctx.sdk is original_handle and ctx.active_msg_id == "queued"
    assert list(ctx.sdk.transcript.background) == ["slow"]
    assert not any(method == "session/close" for method, _ in client.calls)
    client.prompts[1][1].set_result({"stopReason": "end_turn"})
    await ctx.turn_task


@pytest.mark.asyncio
async def test_managed_hook_projects_only_lifecycle_fields():
    events = []

    async def receive(*event):
        events.append(event)

    hooks = AntigravityHooks(receive)
    try:
        _, directory = await hooks.start()
        config = json.loads((Path(directory) / ".agents" / "hooks.json").read_text())
        group, = config.values()
        for kind, extras in (("PreInvocation", {}),
                             ("Stop", {"fullyIdle": False, "terminationReason": "NO_TOOL_CALL"}),
                             ("Stop", {"fullyIdle": True, "terminationReason": "NO_TOOL_CALL"})):
            args = shlex.split(group[kind][0]["command"])
            process = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(json.dumps({
                "hookEventName": kind, "conversationId": "fixture",
                "lastUserInput": "private fixture text", **extras,
            }).encode()), 3)
            assert process.returncode == 0 and json.loads(stdout) == {}
        assert events == [("fixture", "invoke", ""), ("fixture", "stop", "NO_TOOL_CALL")]
    finally:
        await hooks.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_kind", ["agent_thought_chunk", "agent_message_chunk"])
async def test_background_silence_keeps_queued_input_out_of_the_old_native_stream(wire, monkeypatch, chunk_kind):
    machine, transport, _, client = wire
    monkeypatch.setattr(antigravity_runtime, "_FOLLOWUP_NOTICE_AFTER", .03)
    ctx, other = await new_session(machine), await new_session(machine)
    native = ctx.sdk.native_id
    await machine._handle(Query(sid=ctx.key, msg_id="launch", prompt="Start a daemon fixture"))
    await client.update(native, "tool_call", toolCallId="detached", name="run_command", kind="execute",
                        status="in_progress", rawInput={"IsDaemon": True})
    client.prompts[0][1].set_result({"stopReason": "end_turn"})
    await ctx.turn_task
    transport.sent.clear()
    await client.update(native, "tool_call_update", toolCallId="detached", status="completed",
                        rawOutput={"exit_code": 0})
    followup_id = ctx.active_msg_id
    await machine._handle(Query(sid=ctx.key, msg_id="queue", prompt="Independent question",
                                delivery="queue", cmd_id="queue", client_id="browser"))
    # Even before the first native output the queue cannot take the stream.
    await asyncio.sleep(.06)
    assert len(client.prompts) == 1 and ctx.queued_queries[0].msg_id == "queue"
    await client.update(native, chunk_kind, content={"type": "text", "text": "Original continuation"})
    await asyncio.sleep(.06)
    await client.update(native, "agent_message_chunk", content={"type": "text", "text": "Late output"})
    late, = [frame for frame in transport.sent if frame.type == "delta" and frame.text == "Late output"]
    assert late.turn_id == followup_id and not any(frame.type == "turn_end" for frame in transport.sent)
    await machine._handle(Query(sid=other.key, msg_id="other", prompt="Run another session"))

    old = ctx.sdk
    assert await machine._handle(Interrupt(sid=ctx.key, cmd_id="stop-followup", client_id="browser")) is None
    await prompted(client, 3)
    assert ctx.sdk is not old and ctx.active_msg_id == "queue" and other.active_msg_id == "other"
    assert [args["sessionId"] for method, args in client.calls if method == "session/close"] == [native]
    done, = [frame for frame in transport.sent if frame.type == "turn_end"]
    assert (done.turn_id, done.result.subtype) == (followup_id, "interrupted")
    for index, target in ((1, other), (2, ctx)):
        client.prompts[index][1].set_result({"stopReason": "end_turn"})
        await target.turn_task


@pytest.mark.asyncio
async def test_native_followup_wins_before_a_new_prompt_is_written(wire):
    machine, transport, _, client = wire
    ctx = await new_session(machine)
    submit = client.submit

    async def raced_submit(method, args, *, guard):
        await client.update(ctx.sdk.native_id, "agent_message_chunk",
                            content={"type": "text", "text": "Native follow-up"})
        return await submit(method, args, guard=guard)

    client.submit = raced_submit
    query = Query(sid=ctx.key, msg_id="overlap", prompt="New human question")
    rejection = await machine._handle(query)
    assert rejection.code == protocol.ERR_BUSY and not client.prompts
    assert any(frame.type == "delta" and frame.text == "Native follow-up" for frame in transport.sent)
    client.submit = submit
    await machine._handle(Interrupt(sid=ctx.key, cmd_id="stop-followup"))
    await machine._handle(query)
    assert len(client.prompts) == 1
    client.prompts[-1][1].set_result({"stopReason": "end_turn"})
    await ctx.turn_task


@pytest.mark.asyncio
async def test_native_schedule_visibility_is_independent_of_subagent_dispatch(wire):
    machine, transport, _, client = wire
    ctx = await new_session(machine)
    native = ctx.sdk.native_id
    await machine._handle(Query(sid=ctx.key, msg_id="parent", prompt="Run fixture tools"))
    child_args = {"Role": "Fixture reader", "Prompt": "Read notes", "TypeName": "fixture-reader"}
    await client.update(native, "tool_call", toolCallId="child", name="invoke_subagent", kind="other",
                        status="in_progress", rawInput=child_args)
    await client.update(native, "tool_call_update", toolCallId="child", status="completed")
    child, = [frame for frame in transport.sent if frame.type == "tool_use"]
    assert child.title == "派发子代理 · Fixture reader" and child.category == "agent"
    assert child.input == child_args and not ctx.sdk.transcript.background

    timer_args = {"DurationSeconds": 12, "Prompt": "Read the fixture again", "TimerCondition": "never"}
    await client.update(native, "tool_call", toolCallId="wake", name="schedule", kind="other",
                        status="in_progress", rawInput=timer_args)
    timer = [frame for frame in transport.sent if frame.type == "tool_use"][-1]
    sync, = [frame for frame in transport.sent if frame.type == "background_process_sync"]
    item, = sync.items
    assert timer.input == timer_args and timer.background
    assert (item.item_id, item.turn_id, item.title, item.summary) == (
        timer.tool_use_id, "parent", "定时任务", "Read the fixture again")
    assert ctx.state == "running"
    await client.update(native, "tool_call_update", toolCallId="wake", status="completed")
    done = [frame for frame in transport.sent if frame.type == "tool_result"][-1]
    assert done.tool_use_id == timer.tool_use_id and done.turn_id == "parent" and done.background
    assert [frame for frame in transport.sent if frame.type == "background_process_sync"][-1].items == []
    client.prompts[-1][1].set_result({"stopReason": "end_turn"})
    await ctx.turn_task


@pytest.mark.asyncio
@pytest.mark.parametrize("next_prompt_active", [False, True])
async def test_acp_eof_retires_background_on_its_original_turn(wire, next_prompt_active):
    machine, transport, runtime, client = wire
    ctx = await new_session(machine)
    await machine._handle(Query(sid=ctx.key, msg_id="launch", prompt="Start fixture"))
    await client.update(ctx.sdk.native_id, "tool_call", toolCallId="native-command", name="run_command",
                        title="Fixture command", kind="execute", status="in_progress")
    client.prompts[-1][1].set_result({"stopReason": "end_turn"})
    await ctx.turn_task
    assert ctx.sdk.transcript.background

    stream = asyncio.StreamReader()
    acp = AcpClient(AntigravityLaunch("/fake/agy", {}), runtime.on_update,
                    runtime.on_request, on_disconnect=runtime.on_disconnect)
    acp.process = SimpleNamespace(stdout=stream, returncode=None)
    turn_task = None
    if next_prompt_active:
        await machine._handle(Query(sid=ctx.key, msg_id="next", prompt="Next question"))
        acp.pending[1] = client.prompts[-1][1]
        turn_task = ctx.turn_task
    ctx.sdk.client = acp
    transport.sent.clear()
    acp.reader = asyncio.create_task(acp._read())
    stream.feed_eof()
    await acp.reader
    if turn_task:
        await turn_task

    assert ctx.state == "idle" and ctx.write_state == "read_only"
    state, = [frame for frame in transport.sent if frame.type == "antigravity_state"]
    assert not state.connected
    result, = [frame for frame in transport.sent if frame.type == "tool_result"]
    assert result.turn_id == "launch" and result.status == "unknown" and result.background
    sync, = [frame for frame in transport.sent if frame.type == "background_process_sync"]
    assert sync.items == []
    page = await machine._handle(GetHistory(session_id=ctx.key, detail="summary"))
    command, = [block for block in page.turns[0].blocks if block["kind"] == "tool"]
    assert command["done"] and command["result"]["status"] == "unknown"


@pytest.mark.asyncio
async def test_forced_stop_closes_only_the_target_and_reopens_it(wire, monkeypatch):
    machine, _, _, client = wire
    monkeypatch.setattr(antigravity_runtime, "_CANCEL_TIMEOUT", 0.04)

    async def ignore_cancel(method, args):
        client.calls.append((method, args))

    client.notify = ignore_cancel
    ctx, other = await new_session(machine), await new_session(machine)
    await machine._handle(Query(sid=ctx.key, msg_id="first", prompt="first"))
    await machine._handle(Query(sid=other.key, msg_id="other", prompt="other"))
    signal = await machine._handle(Interrupt(sid=ctx.key, cmd_id="ordinary"))
    assert signal.code == "gemini_cancel_pending" and ctx.state == "draining"
    previous = ctx.sdk
    await machine._handle(Interrupt(sid=ctx.key, cmd_id="forced"))
    assert ctx.state == "idle" and ctx.sdk is not previous and ctx.sdk.connected
    assert other.state == "running" and not client.prompts[1][1].done()
    assert [args["sessionId"] for method, args in client.calls if method == "session/close"] == [previous.native_id]
    assert len(client.prompts) == 2
    await machine._handle(Query(sid=ctx.key, msg_id="next", prompt="next"))
    await machine._handle(Interrupt(sid=ctx.key, cmd_id="forced"))
    assert len(client.prompts) == 3 and ctx.state == "running"
    for target, index in ((ctx, 2), (other, 1)):
        client.prompts[index][1].set_result({"stopReason": "end_turn"})
        await target.turn_task


@pytest.mark.asyncio
async def test_force_stop_restarts_hung_transport_only_after_other_turn_settles(wire, monkeypatch):
    machine, _, runtime, client = wire
    monkeypatch.setattr(antigravity_runtime, "_CANCEL_TIMEOUT", 0.03)
    monkeypatch.setattr(antigravity_runtime, "_CLOSE_TIMEOUT", 0.02)
    original_call = client.call

    async def hang_close(method, args):
        if method == "session/close":
            await asyncio.Future()
        return await original_call(method, args)

    async def ignore_cancel(method, args):
        client.calls.append((method, args))

    client.call = hang_close
    client.notify = ignore_cancel
    ctx, other = await new_session(machine), await new_session(machine)
    await machine._handle(Query(sid=ctx.key, msg_id="stranded", prompt="stranded"))
    await machine._handle(Query(sid=other.key, msg_id="live", prompt="live"))
    replacement = ScriptedAcp(runtime, client.cwd)
    replacement.controls.update(client.controls)
    replacement.rows.update(client.rows)

    async def connect():
        if runtime.client is None:
            runtime.client = replacement
        return runtime.client

    runtime.connection = connect
    await machine._handle(Interrupt(sid=ctx.key, cmd_id="ordinary"))
    still_waiting = await machine._handle(Interrupt(sid=ctx.key, cmd_id="force-blocked"))
    assert still_waiting.code == "gemini_cancel_pending" and client.running
    assert ctx.state == "draining" and other.state == "running"
    client.prompts[1][1].set_result({"stopReason": "end_turn"})
    await other.turn_task
    await machine._handle(Interrupt(sid=ctx.key, cmd_id="force-restart"))
    assert not client.running and runtime.client is replacement
    assert ctx.state == "idle" and ctx.sdk.client is replacement and ctx.sdk.connected
    assert not replacement.prompts and len(client.prompts) == 2


@pytest.mark.asyncio
async def test_rpc_diagnostics_keep_error_code_without_native_secret():
    async def noop(*_args):
        return None

    client = AcpClient(AntigravityLaunch("/fake/agy", {}), noop, noop)
    receipt = asyncio.get_running_loop().create_future()
    client.pending[1] = receipt
    await client._receive({"id": 1, "error": {"code": -32602, "message": "private gateway key"}})
    with pytest.raises(AcpError, match=r"ACP -32602.*acp-\*\.log") as failure:
        await receipt
    assert "private gateway key" not in str(failure.value)


@pytest.mark.asyncio
async def test_steer_receipt_owns_queue_until_all_native_requests_finish(wire):
    machine, transport, _, client = wire
    ctx = await new_session(machine)
    await machine._handle(Query(sid=ctx.key, msg_id="first", prompt="first"))
    client.rows.clear()  # Native listing may lag the first prompt.
    rows = (await machine._handle(ListSessions(engine="antigravity"))).sessions
    assert [(row.session_id, row.state) for row in rows] == [(ctx.key, "running")]
    await machine._handle(Query(sid=ctx.key, msg_id="queue", prompt="queue", delivery="queue",
                                cmd_id="queue-cmd", client_id="browser"))
    await machine._handle(Steer(sid=ctx.key, msg_id="steer", prompt="steer",
                                cmd_id="steer-cmd", client_id="browser"))
    client.prompts[0][1].set_result({"stopReason": "end_turn"})
    await asyncio.sleep(0)
    assert ctx.state == "running" and len(client.prompts) == 2
    assert any(frame.type == "turn_steered" and frame.msg_id == "steer" for frame in transport.sent)

    client.prompts[1][1].set_result({"stopReason": "end_turn"})
    await prompted(client, 3)
    assert client.prompts[2][0]["prompt"][0]["text"] == "queue"
    await machine._handle(Interrupt(sid=ctx.key, cmd_id="stop", client_id="browser"))
    assert ctx.state == "idle" and any(
        frame.type == "turn_end" and frame.result.subtype == "interrupted" for frame in transport.sent)


@pytest.mark.asyncio
async def test_permission_reply_is_bound_to_native_request_and_survives_hello(wire):
    machine, transport, runtime, client = wire
    ctx = await new_session(machine)
    await machine._handle(Query(sid=ctx.key, msg_id="permission", prompt="permission"))
    path = str(Path(ctx.cwd) / "note.txt")
    interaction = asyncio.create_task(runtime.on_request("session/request_permission", 19, {
        "sessionId": ctx.sdk.native_id,
        "toolCall": {"title": "File note.txt", "rawInput": {"Path": "note.txt"},
                     "content": [{"type": "diff", "path": path, "oldText": "before\n", "newText": "after\n"}]},
        "options": [{"optionId": "approve", "kind": "allow_once", "name": "Allow once"},
                    {"optionId": "deny", "kind": "reject_once", "name": "Deny"}],
    }))
    async with asyncio.timeout(1):
        while not ctx.pending_asks:
            await asyncio.sleep(0)
    ask_id, = ctx.pending_asks
    assert path in ctx.pending_asks[ask_id].event.question
    await machine._handle(Hello(role="client", client_id="fresh", discover_engines=True, cursors={ctx.key: ctx.seq},
                                generations={ctx.key: machine.instance_id}))
    replay = next(frame for frame in transport.sent if frame.type == "ask_user" and frame.to == "fresh")
    assert replay.ask_id == ask_id and "-before\n+after" in deserialize(serialize(replay)).approval_diff
    assert transport.sent[-1].type == "antigravity_availability"
    assert transport.sent[-1].enabled and transport.sent[-1].to == "fresh"
    await machine._handle(AnswerQuestion(sid=ctx.key, ask_id="unrelated", answers=["Allow once"], client_id="fresh"))
    assert not interaction.done()
    await machine._handle(AnswerQuestion(sid=ctx.key, ask_id=ask_id, answers=["Allow once"], client_id="fresh"))
    assert await interaction == {"outcome": {"outcome": "selected", "optionId": "approve"}}
    client.prompts[0][1].set_result({"stopReason": "end_turn"})
    await ctx.turn_task


@pytest.mark.asyncio
async def test_ambiguous_prompt_is_never_resubmitted_after_recovery(wire):
    machine, transport, runtime, client = wire
    ctx = await new_session(machine)
    query = Query(sid=ctx.key, msg_id="ambiguous", prompt="do it", owner_id="owner")
    await machine._handle(query)
    client.prompts[0][1].set_exception(AcpError("offline", "unknown", unknown=True))
    await ctx.turn_task
    # Replace the adapter to model a recovered Wrapper with only its durable
    # intent ledger remaining. The prior native prompt could have succeeded.
    recovered = machine._antigravity = AntigravityRuntime(machine, launch_factory=lambda: True)
    recovered.client = client
    duplicate = await machine._handle(query.model_copy(update={"client_id": "browser-b"}))
    assert duplicate.code == "gemini_outcome_unknown"
    assert len(client.prompts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["accept", "decline", "cancel"])
async def test_native_elicitation_uses_form_answers_and_explicit_actions(wire, action):
    machine, _, runtime, client = wire
    ctx = await new_session(machine)
    await machine._handle(Query(sid=ctx.key, msg_id="form", prompt="form"))
    interaction = asyncio.create_task(runtime.on_request("elicitation/create", "form-request", {
        "sessionId": ctx.sdk.native_id, "mode": "form", "message": "How should we proceed?",
        "requestedSchema": {"type": "object", "properties": {
            "scope": {"type": "string", "enum": ["small", "full"]},
            "notes": {"type": "string"},
        }},
    }))
    async with asyncio.timeout(1):
        while not ctx.pending_asks:
            await asyncio.sleep(0)
    ask_id, = ctx.pending_asks
    assert ctx.pending_asks[ask_id].event.dismiss_actions == ["decline", "cancel"]
    fields = {"answers": ["small", "Use the existing directory."]} if action == "accept" else {"action": action}
    await machine._handle(AnswerQuestion(sid=ctx.key, ask_id=ask_id, **fields))
    expected = {"action": action}
    if action == "accept":
        expected["content"] = {"scope": "small", "notes": "Use the existing directory."}
    assert await interaction == expected
    client.prompts[0][1].set_result({"stopReason": "end_turn"})
    await ctx.turn_task


@pytest.mark.asyncio
async def test_cc_remote_mcp_question_is_bound_to_its_session_and_closes_on_disconnect(wire):
    machine, transport, runtime, client = wire
    source = await new_session(machine)
    other = await new_session(machine)
    servers = next(params["mcpServers"] for method, params in client.calls if method == "session/new")
    owned = source.sdk.tools.ask_server_name
    assert {server["name"] for server in servers} == {owned, "cc_remote_artifact_preview"}
    assert owned != other.sdk.tools.ask_server_name
    endpoint = next(server["args"][-1] for server in servers if server["name"] == owned)
    reader, writer = await asyncio.open_unix_connection(endpoint)

    async def send(method, identity=None, **params):
        frame = {"jsonrpc": "2.0", "method": method, "params": params}
        if identity is not None:
            frame["id"] = identity
        writer.write(json.dumps(frame).encode() + b"\n")
        await writer.drain()

    async def pending_ask():
        async with asyncio.timeout(2):
            while not source.pending_asks:
                await asyncio.sleep(.001)
        return next(iter(source.pending_asks))

    arguments = {"question": "Which scope?", "options": [{"label": "Small"}, {"label": "Full"}]}
    try:
        await send("server/discover", 1)
        assert json.loads(await reader.readline())["error"]["code"] == -32601
        await send("initialize", 2, protocolVersion="2025-11-25", capabilities={},
                   clientInfo={"name": "agy-fixture", "version": "1"})
        assert json.loads(await reader.readline())["result"]["serverInfo"]["name"] == "cc-remote-ask"
        await send("notifications/initialized")
        await machine._handle(Query(sid=source.key, msg_id="ask", prompt="Ask with a card"))
        approval_options = [
            {"optionId": "always", "kind": "allow_always", "name": "Always"},
            {"optionId": "once", "kind": "allow_once", "name": "Allow"},
            {"optionId": "deny", "kind": "reject_once", "name": "Deny"},
        ]
        approval = {"sessionId": source.sdk.native_id, "options": approval_options}
        direct = await runtime.on_request("session/request_permission", 23, {
            **approval, "toolCall": {"name": f"mcp_{owned}_ask_user", "title": "Ask fixture"},
        })
        assert direct == {"outcome": {"outcome": "selected", "optionId": "once"}}
        assert not source.pending_asks
        for mode_tool in (f"mcp_{owned}_set_mode", f"mcp_{other.sdk.tools.ask_server_name}_ask_user",
                          "mcp_cc_remote_artifact_preview_inspect_file", "mcp_cc_remote_messages_send_message"):
            requiring_approval = asyncio.create_task(runtime.on_request("session/request_permission", mode_tool, {
                **approval, "toolCall": {"name": mode_tool, "title": f"mcp_{owned}_ask_user"},
            }))
            ask_id = await pending_ask()
            assert source.pending_asks[ask_id].event.header == "Gemini 工具权限"
            await machine._handle(AnswerQuestion(sid=source.key, ask_id=ask_id, answers=["Deny"]))
            assert await requiring_approval == {"outcome": {"outcome": "selected", "optionId": "deny"}}
        await send("tools/call", 3, name="ask_user", arguments=arguments)
        ask_id = await pending_ask()
        await machine._handle(AnswerQuestion(sid=other.key, ask_id=ask_id, answers=["Full"]))
        assert ask_id in source.pending_asks and not other.pending_asks
        await machine._handle(Hello(role="client", client_id="fresh", cursors={source.key: source.seq},
                                    generations={source.key: machine.instance_id}))
        assert any(frame.type == "ask_user" and frame.ask_id == ask_id and frame.to == "fresh"
                   for frame in transport.sent)
        await machine._handle(AnswerQuestion(sid=source.key, ask_id=ask_id, answers=["Small"]))
        result = json.loads(await reader.readline())["result"]
        assert result["content"][0]["text"] == "Small"

        await send("tools/call", 4, name="ask_user", arguments=arguments)
        ask_id = await pending_ask()
    finally:
        writer.close()
        await writer.wait_closed()
    async with asyncio.timeout(2):
        while ask_id in source.pending_asks:
            await asyncio.sleep(.001)
    assert any(frame.type == "ask_user_closed" and frame.ask_id == ask_id and frame.reason == "cancelled"
               for frame in transport.sent)
    client.prompts[0][1].set_result({"stopReason": "end_turn"})
    await source.turn_task


@pytest.mark.asyncio
@pytest.mark.parametrize("connected", [True, False])
async def test_messages_to_gemini_confirm_native_delivery_without_retrying_ambiguous_input(wire, monkeypatch, connected):
    machine, _, runtime, client = wire
    bus = machine._session_messages = SessionMessages(machine)
    monkeypatch.setattr(machine, "_claude_profiles", [])

    async def no_codex_sessions():
        return []

    monkeypatch.setattr(machine, "_read_all_codex_profile_sessions", no_codex_sessions)
    source = await new_session(machine)
    target = await new_session(machine)
    machine.focused_sid = source.key
    source_ref, target_ref = (reference("antigravity", ctx.key) for ctx in (source, target))
    args = dict(session_ref=target_ref, message="Check this result", request_id="exact-once")
    sending = asyncio.create_task(bus.call(source_ref, "send_message", args))
    try:
        await prompted(client, 1)
        request, response = client.prompts[0]
        message = parse_message(request["prompt"][0]["text"])
        assert request["sessionId"] == target.sdk.native_id
        assert (message.source_thread_id, message.prompt) == (source_ref, args["message"])
        assert bus.store.get(source_ref, args["request_id"])["status"] == "unknown"
        assert not sending.done()
        if connected:
            await client.update(target.sdk.native_id, "agent_message_chunk",
                                content={"type": "text", "text": "Received"})
            assert (await sending)["status"] == "accepted"
            response.set_result({"stopReason": "end_turn"})
        else:
            await client.close()
            assert (await sending)["status"] == "unknown"
        monkeypatch.setenv("CC_REMOTE_ANTIGRAVITY", "0")
        assert await bus.call(source_ref, "send_message", args) == await sending
        if connected:
            assert (await bus.call(source_ref, "list_sessions", {}))["sessions"] == []
            with pytest.raises(ValueError, match="Gemini is disabled"):
                await bus.call(source_ref, "send_message", {**args, "request_id": "new-after-opt-out"})
        assert len(client.prompts) == 1
        update = {"name": "mcp_cc_remote_messages_send_message", "status": "completed", "rawInput": args}
        assert json.loads(runtime.with_message_receipt(source.key, update)["rawOutput"]) == await sending
        conflicting = {**update, "rawInput": {**args, "message": "A different message"}}
        assert runtime.with_message_receipt(source.key, conflicting) is conflicting
        user = next(iter(target.sdk.transcript.groups.values()))[0]
        assert (user["source_thread_id"], user["prompt"]) == (source_ref, args["message"])
        assert machine.focused_sid == source.key
    finally:
        sending.cancel()
        await asyncio.gather(sending, return_exceptions=True)
        await bus.close()
        await machine._command_scheduler.close()
