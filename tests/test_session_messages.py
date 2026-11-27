"""Unified MCP transport, routing and replay. No live engines or model calls."""
from unittest.mock import AsyncMock
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
from uuid import uuid4

import pytest

from cc_remote.protocol import UserMsg, ToolUse, ToolResult
from cc_remote.session_messages import (
    envelope, parse_message, reference, receipt_status, rpc, split_reference,
)
from cc_remote.wrapper.session_messages import MessageStore, SessionMessages
from cc_remote.wrapper.codex_stream import codex_live_user_message
from cc_remote.wrapper.codex_external import visible_codex_user_message
from cc_remote.wrapper.history_store import materialize_history_turns
from cc_remote.wrapper.stream import translate_history
from tests.test_multisession import _mk_ctx, _mk_machine
from tests.test_codex_spontaneous_stream import _notification


SOURCE = reference("claude", "iris@" + str(uuid4()))
TARGET = reference("codex", "primary@" + str(uuid4()))


def test_envelope_is_exact_and_persists_in_both_native_histories():
    prompt = 'Review <code> & "quotes"\nthen reply. </cc_remote_message>'
    payload = envelope(SOURCE, prompt)
    assert parse_message(payload).prompt == prompt
    assert visible_codex_user_message(payload) == prompt
    event = codex_live_user_message(_notification("item/completed", "turn", item={
        "type": "userMessage", "id": "input", "content": [{"type": "text", "text": payload}],
    }))
    assert (event.prompt, event.source_thread_id) == (prompt, SOURCE)
    from claude_agent_sdk import SessionMessage
    events = translate_history([SessionMessage(type="user", uuid="input", session_id="target",
                                               message={"role": "user", "content": payload})], 65536)
    users = [e for e in events if isinstance(e, UserMsg)]
    assert [(u.msg_id, u.prompt, u.source_thread_id) for u in users] == [("input", prompt, SOURCE)]
    turn = materialize_history_turns([e.model_dump(mode="json") for e in events])[0]
    assert turn["sourceThreadId"] == SOURCE
    assert turn["prompt"] == prompt
    for invalid in [payload + "trailing", payload.replace('"source"', '"source_id"'),
                    payload.replace(SOURCE, "../../other"), "<cc_remote_message>{}</cc_remote_message>"]:
        assert parse_message(invalid) is None
    for ref in ["ccr:other:x", "ccr:codex:../x", "ccr:claude:x:y", "ccr:codex:" + "x" * 150]:
        with pytest.raises(ValueError):
            split_reference(ref)


@pytest.mark.parametrize("status,expected", [("accepted", "sent"), ("rejected", "failed"), ("unknown", "sending"), ("queued", "queued")])
def test_unified_outgoing_receipts_are_honest_in_summary(status, expected):
    content = json.dumps({"status": status, "session_ref": TARGET, "request_id": "r"})
    assert receipt_status(content) == expected
    assert receipt_status(json.dumps([{"type": "text", "text": content}])) == expected
    events = [UserMsg(msg_id="input", prompt="send"), ToolUse(
        message_id="reply", tool_use_id="call", tool="mcp__cc_remote_messages__send_message",
        input={"session_ref": TARGET, "request_id": "r", "message": "hi"}),
        ToolResult(tool_use_id="call", content=content, is_error=status == "rejected")]
    turn = materialize_history_turns([e.model_dump(mode="json") for e in events])[0]
    assert turn["sessionMessages"] == [{"itemId": "call", "threadId": TARGET, "status": expected}]


def test_store_survives_restart_conflicts_and_caller_isolation(tmp_path):
    store = MessageStore(tmp_path / "messages")
    original, fresh = store.claim(SOURCE, "r", TARGET, "hi")
    assert fresh and original["status"] == "unknown"
    recovered, fresh = MessageStore(store.root).claim(SOURCE, "r", TARGET, "hi")
    assert not fresh and recovered == original
    with pytest.raises(ValueError, match="different arguments"):
        store.claim(SOURCE, "r", TARGET, "different")
    with pytest.raises(ValueError, match="No receipt"):
        store.get(TARGET, "r")
    store.settle(original["message_id"], "accepted")
    store.settle(original["message_id"], "unknown")
    assert store.get(SOURCE, "r")["status"] == "accepted"
    assert os.stat(store.root).st_mode & 0o077 == 0


def backend(monkeypatch, target_engine="claude"):
    machine, transport = _mk_machine()
    bus = SessionMessages(machine)
    machine._session_messages = bus
    source = _mk_ctx("source", "source")
    source.engine = "codex" if target_engine == "claude" else "claude"
    target = _mk_ctx("target", "target")
    target.engine = target_engine
    machine.sessions = {source.key: source, target.key: target}
    machine.focused_sid = source.key
    source_ref, target_ref = reference(source.engine, source.key), reference(target.engine, target.key)
    rows = {source_ref: {"session_ref": source_ref, "title": "sender", "engine": source.engine},
            target_ref: {"session_ref": target_ref, "title": "receiver", "engine": target.engine, "cwd": "/tmp"}}

    async def catalog():
        return dict(rows)
    monkeypatch.setattr(bus, "catalog", catalog)
    return bus, machine, target, rows, source_ref, target_ref


@pytest.mark.asyncio
@pytest.mark.parametrize("target_engine", ["claude", "codex"])
async def test_dispatch_uses_existing_lane_and_exact_target_once(monkeypatch, target_engine):
    bus, machine, target, rows, source, recipient = backend(monkeypatch, target_engine)
    calls = []

    async def query(ctx, cmd, **_):
        calls.append(cmd)
        assert cmd.sid == target.key
        assert parse_message(cmd.prompt).source_thread_id == source
        assert parse_message(cmd.prompt).prompt == "hi"
        bus.accepted(cmd.msg_id)
    monkeypatch.setattr(machine, "_handle_immediate_query", query)
    args = {"session_ref": recipient, "message": "hi", "request_id": "r"}
    result, retry = await asyncio.gather(bus.call(source, "send_message", args), bus.call(source, "send_message", args))
    assert result["message_id"] == retry["message_id"]
    assert result["status"] == "accepted"
    assert len(calls) == 1
    assert machine.focused_sid == "source"
    rows.clear()  # A receipt is still recoverable after a target was deleted.
    assert (await bus.call(source, "send_message", args))["status"] == "accepted"
    assert (await bus.call(source, "message_status", {"request_id": "r"}))["status"] == "accepted"
    await machine._command_scheduler.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["deleted", "wrong_engine", "btw", "work", "external"])
async def test_no_send_for_ineligible_target(monkeypatch, condition):
    bus, machine, target, rows, source, recipient = backend(monkeypatch)
    if condition == "external":
        target.write_state = "read_only"
    if condition == "wrong_engine":
        target.engine = "codex"
    if condition == "btw":
        target.btw = True
    if condition == "work":
        target.space = "work"
    if condition == "deleted":
        reads = 0
        async def catalog():
            nonlocal reads
            reads += 1
            return rows if reads == 1 else {}
        monkeypatch.setattr(bus, "catalog", catalog)
    async def query(ctx, _cmd, **_):
        pytest.fail("An ineligible target must not receive a query")
    monkeypatch.setattr(machine, "_handle_immediate_query", query)
    result = await bus.call(source, "send_message", {"session_ref": recipient, "message": "hi", "request_id": "r"})
    assert result["status"] == "rejected"
    assert machine.focused_sid == "source"
    await machine._command_scheduler.close()


@pytest.mark.asyncio
async def test_lost_native_ack_stays_unknown_and_never_repeats(monkeypatch):
    bus, machine, target, rows, source, recipient = backend(monkeypatch)
    calls = []
    async def query(ctx, cmd, **_):
        calls.append(cmd)
        raise ConnectionError("native acknowledgment lost")
    monkeypatch.setattr(machine, "_handle_immediate_query", query)
    args = {"session_ref": recipient, "message": "hi", "request_id": "r"}
    assert (await bus.call(source, "send_message", args))["status"] == "unknown"
    assert (await bus.call(source, "send_message", args))["status"] == "unknown"
    assert len(calls) == 1
    await machine._command_scheduler.close()


@pytest.mark.asyncio
async def test_cli_ownership_adopted_during_preflight_rejects_without_sending(monkeypatch):
    bus, machine, target, rows, source, recipient = backend(monkeypatch)
    async def adopt(ctx):
        ctx.sdk = SimpleNamespace(is_claude_broker=True)
    monkeypatch.setattr(machine, "_adopt_claude_broker_handle", adopt)
    result = await bus.call(source, "send_message", {
        "session_ref": recipient, "message": "hi", "request_id": "ownership-race"})
    assert result["status"] == "rejected"
    assert "本机终端" in result["reason"]
    assert target.turn_task is None and target.state == "idle"
    await machine._command_scheduler.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude", "codex"])
async def test_cold_target_resumes_exact_account_without_focus_or_bootstrap(monkeypatch, engine):
    bus, machine, target, rows, source, recipient = backend(monkeypatch, engine)
    del machine.sessions[target.key]
    profile = SimpleNamespace(id="account")
    monkeypatch.setattr(machine, f"_{engine}_target", lambda sid: (profile, "target"))
    monkeypatch.setattr(machine, f"_{engine}_spawn_profile_kwargs", lambda p: {f"{engine}_profile_id": p.id})
    resumed = []
    async def spawn(**kwargs):
        resumed.append(kwargs)
        machine.sessions[target.key] = target
        return target
    async def query(ctx, cmd, **_):
        bus.accepted(cmd.msg_id)
    monkeypatch.setattr(machine, "_spawn", spawn)
    monkeypatch.setattr(machine, "_handle_immediate_query", query)
    result = await bus.call(source, "send_message", {"session_ref": recipient, "message": "hi", "request_id": "cold"})
    assert result["status"] == "accepted"
    assert resumed == [{"resume_id": "target", "cwd": "/tmp", "engine": engine,
                        "raise_on_failure": True, f"{engine}_profile_id": "account"}]
    assert machine.focused_sid == "source"
    await machine._command_scheduler.close()


@pytest.mark.asyncio
async def test_claude_bound_mcp_and_live_echo(monkeypatch):
    bus, machine, target, rows, source, recipient = backend(monkeypatch)
    from mcp.server.lowlevel.server import RequestContext, request_ctx
    from mcp.types import CallToolRequest, CallToolRequestParams
    ctx = machine.sessions["source"]
    ctx.engine = "claude"
    bound_source = reference("claude", ctx.key)
    rows[bound_source] = rows.pop(source)
    server = bus.claude_server(ctx)
    token = request_ctx.set(RequestContext(request_id=1, meta=None, session=None, lifespan_context=None))
    try:
        request = CallToolRequest(params=CallToolRequestParams(name="list_sessions", arguments={}))
        result = (await server.request_handlers[CallToolRequest](request)).root
        assert not result.isError
        assert len(json.loads(result.content[0].text)["sessions"]) == 1
        spoof = CallToolRequest(params=CallToolRequestParams(name="list_sessions", arguments={"source": recipient}))
        assert (await server.request_handlers[CallToolRequest](spoof)).root.isError
    finally:
        request_ctx.reset(token)
    msg = UserMsg(msg_id="message", prompt=envelope(bound_source, "hello"))
    await machine._emit(target, msg)
    assert (msg.prompt, msg.source_thread_id, msg.sid) == ("hello", bound_source, target.key)


@pytest.mark.asyncio
async def test_real_stdio_mcp_binds_codex_metadata_and_private_socket(monkeypatch):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    with tempfile.TemporaryDirectory(prefix="crm-", dir="/tmp") as folder:
        bus, machine, target, rows, source, recipient = backend(monkeypatch)
        bus.store = MessageStore(Path(folder) / "messages")
        bus.socket = bus.store.root / "control.sock"
        home = Path(folder) / "account"
        home.mkdir()
        machine._codex_profiles = [SimpleNamespace(id="test", home=home)]
        sid = str(uuid4())
        native_ref = reference("codex", "test@" + sid)
        rows[native_ref] = rows.pop(source)
        monkeypatch.setattr(machine, "_codex_wire_sid", lambda p, sid: "test@" + sid)
        sent = []
        async def query(ctx, cmd, **_):
            sent.append(parse_message(cmd.prompt))
            bus.accepted(cmd.msg_id)
        monkeypatch.setattr(machine, "_handle_immediate_query", query)
        await bus.start()
        try:
            parameters = StdioServerParameters(command=sys.executable,
                args=["-m", "cc_remote.session_messages", "mcp", "--socket", str(bus.socket), "--codex-home", str(home)],
                cwd=str(Path(__file__).resolve().parents[1]))
            async with asyncio.timeout(10), stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    assert {tool.name for tool in (await session.list_tools()).tools} == {
                        "list_sessions", "send_message", "message_status"}
                    missing = await session.call_tool("list_sessions", {})
                    assert missing.isError
                    result = await session.call_tool("list_sessions", {}, meta={
                        "x-codex-turn-metadata": {"thread_id": sid}})
                    assert not result.isError
                    assert json.loads(result.content[0].text)["sessions"][0]["session_ref"] == recipient
                    # The advertised character bound must also fit the private
                    # byte-framed transport for non-ASCII input, including emoji.
                    args = {"session_ref": recipient, "message": "🙂" * 32000, "request_id": "unicode"}
                    receipt = await session.call_tool("send_message", args, meta={"threadId": sid})
                    retry = await session.call_tool("send_message", args, meta={"threadId": sid})
                    assert not receipt.isError
                    assert json.loads(receipt.content[0].text)["status"] == "accepted"
                    assert retry.content == receipt.content
                    assert len(sent) == 1 and sent[0].source_thread_id == native_ref
                    assert sent[0].prompt == args["message"]
            with pytest.raises(ValueError):
                await rpc(bus.socket, {"codex_home": "/wrong", "thread_id": sid,
                                       "tool": "list_sessions", "arguments": {}})
            os.chmod(bus.socket, 0o666)
            with pytest.raises(ValueError, match="private"):
                await rpc(bus.socket, {})
        finally:
            await bus.close()
            await machine._command_scheduler.close()


@pytest.mark.asyncio
async def test_corrupt_ack_is_unknown_not_a_rejection():
    with tempfile.TemporaryDirectory(prefix="crm-ack-", dir="/tmp") as folder:
        path = Path(folder) / "control.sock"
        received = []
        async def broken(reader, writer):
            received.append(await reader.readline())
            writer.write(b"not-json\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_unix_server(broken, str(path))
        os.chmod(path, 0o600)
        try:
            with pytest.raises(ConnectionError, match="acknowledgment"):
                await rpc(path, {"tool": "send_message"})
            assert len(received) == 1
        finally:
            server.close()
            await server.wait_closed()


def test_sdk_injects_message_server_only_into_code():
    from cc_remote.config import WrapperConfig
    from cc_remote.wrapper.sdk import SdkHandle
    from cc_remote.session_messages import make_server
    async def call(*_):
        return {}
    handle = SdkHandle(WrapperConfig())
    handle.message_server = make_server(call)
    assert handle._options(None, "/tmp").mcp_servers["cc_remote_messages"]["instance"] is handle.message_server
    handle.work_mode = True
    assert handle._options(None, "/tmp").mcp_servers == {}


async def wait_receipt(bus, source, key, status):
    async with asyncio.timeout(3):
        while bus.store.get(source, key)['status'] != status:
            await asyncio.sleep(0.01)
    return bus.store.get(source, key)


@pytest.mark.asyncio
@pytest.mark.parametrize('engine', ['claude', 'codex'])
@pytest.mark.parametrize('busy', ['turn', 'question', 'terminal_drain', 'human_queue', 'autonomous'])
async def test_busy_message_waits_for_real_boundary_without_interrupt(monkeypatch, engine, busy):
    if engine == 'codex' and busy == 'autonomous':
        return
    bus, machine, target, rows, source, recipient = backend(monkeypatch, engine)
    active = None
    if busy == 'turn':
        target.state = 'running'
    elif busy == 'question':
        target.state = 'waiting_user'
    elif busy == 'terminal_drain':
        active = asyncio.create_task(asyncio.Event().wait())
        target.turn_task = active
    elif busy == 'human_queue':
        target.queued_queries.append(object())
    else:
        target.claude_background_followups['native-task'] = 'pending'
    calls = []
    async def query(ctx, cmd, **_):
        calls.append(cmd)
        bus.accepted(cmd.msg_id)
    monkeypatch.setattr(machine, '_handle_immediate_query', query)
    args = {'session_ref': recipient, 'message': 'reply', 'request_id': 'busy-reply'}
    async with asyncio.timeout(2):
        receipt = await bus.call(source, 'send_message', args)
    assert receipt['status'] == 'queued'
    assert bus.store.pending()[0]['prompt'] == envelope(source, 'reply')
    assert await bus.call(source, 'send_message', args) == receipt
    bus.pump = asyncio.create_task(bus._drain())
    try:
        await asyncio.sleep(0.03)
        assert not calls
        if active:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        target.state = 'idle'
        target.queued_queries.clear()
        target.claude_background_followups.clear()
        bus.wakeup.set()
        final = await wait_receipt(bus, source, 'busy-reply', 'accepted')
        assert final['message_id'] == receipt['message_id']
        assert len(calls) == 1 and not bus.store.pending()
        assert machine.focused_sid == 'source'
    finally:
        await bus.close()
        await machine._command_scheduler.close()


@pytest.mark.asyncio
async def test_queued_inbox_recovers_after_wrapper_restart_in_fifo_order(monkeypatch):
    bus, machine, target, rows, source, recipient = backend(monkeypatch)
    target.state = 'running'
    for key in ['first', 'second']:
        assert (await bus.call(source, 'send_message', {
            'session_ref': recipient, 'message': key, 'request_id': key}))['status'] == 'queued'
    await bus.close()
    replacement = SessionMessages(machine)
    machine._session_messages = replacement
    async def catalog(): return dict(rows)
    monkeypatch.setattr(replacement, 'catalog', catalog)
    calls = []
    async def query(ctx, cmd, **_):
        calls.append(parse_message(cmd.prompt).prompt)
        target.state = 'running'
        replacement.accepted(cmd.msg_id)
    monkeypatch.setattr(machine, '_handle_immediate_query', query)
    replacement.pump = asyncio.create_task(replacement._drain())
    try:
        target.state = 'idle'
        replacement.wakeup.set()
        await wait_receipt(replacement, source, 'first', 'accepted')
        assert calls == ['first']
        assert replacement.store.get(source, 'second')['status'] == 'queued'
        target.state = 'idle'
        replacement.wakeup.set()
        await wait_receipt(replacement, source, 'second', 'accepted')
        assert calls == ['first', 'second']
    finally:
        await replacement.close()
        await machine._command_scheduler.close()


def test_inbox_claim_is_crash_safe_bounded_and_erases_delivered_payload(tmp_path):
    store = MessageStore(tmp_path / 'messages')
    for number in range(32):
        store.claim(SOURCE, str(number), TARGET, 'private body', queue=True)
    with pytest.raises(ValueError, match='inbox is full'):
        store.claim(SOURCE, 'overflow', TARGET, 'no', queue=True)
    first = store.get(SOURCE, '0')
    assert store.begin_delivery(first['message_id'])
    assert not store.begin_delivery(first['message_id'])
    recovered = MessageStore(store.root)
    assert len(recovered.pending()) == 31
    assert recovered.claim(SOURCE, '0', TARGET, 'private body', queue=True)[0]['status'] == 'unknown'
    with store.db() as db:
        assert db.execute('SELECT prompt FROM messages WHERE id=?', (first['message_id'],)).fetchone()[0] is None
    store.unsubmitted(first['message_id'], 'private body')
    assert len(store.pending()) == 32
    store.settle(first['message_id'], 'accepted')
    store.unsubmitted(first['message_id'], 'must not replay')
    assert store.get(SOURCE, '0')['status'] == 'accepted'
    with store.db() as db:
        assert db.execute('SELECT prompt FROM messages WHERE id=?', (first['message_id'],)).fetchone()[0] is None


@pytest.mark.asyncio
async def test_queued_target_is_revalidated_before_delivery(monkeypatch):
    bus, machine, target, rows, source, recipient = backend(monkeypatch)
    target.state = 'running'
    await bus.call(source, 'send_message', {'session_ref': recipient, 'message': 'hi', 'request_id': 'stale'})
    rows.pop(recipient)
    target.state = 'idle'
    async def query(*_, **__): pytest.fail('Deleted/archived target must not run')
    monkeypatch.setattr(machine, '_handle_immediate_query', query)
    bus.pump = asyncio.create_task(bus._drain())
    try:
        await wait_receipt(bus, source, 'stale', 'rejected')
    finally:
        await bus.close()
        await machine._command_scheduler.close()


@pytest.mark.asyncio
async def test_native_launch_fence_requeues_only_proven_unsent_input(monkeypatch):
    bus, machine, target, rows, source, recipient = backend(monkeypatch)
    attempts = []
    async def query(ctx, cmd, *, launch_receipt):
        attempts.append(cmd)
        async def native():
            if len(attempts) == 1:
                launch_receipt.set_result(False)
            else:
                bus.accepted(cmd.msg_id)
                launch_receipt.set_result(True)
        target.turn_task = asyncio.create_task(native())
    monkeypatch.setattr(machine, '_handle_immediate_query', query)
    result = await bus.call(source, 'send_message', {'session_ref': recipient, 'message': 'hi', 'request_id': 'race'})
    assert result['status'] == 'queued'
    bus.pump = asyncio.create_task(bus._drain())
    try:
        await wait_receipt(bus, source, 'race', 'accepted')
        assert len(attempts) == 2
        assert attempts[0].msg_id == attempts[1].msg_id
    finally:
        await bus.close()
        await machine._command_scheduler.close()


@pytest.mark.asyncio
async def test_missing_tools_retry_stays_queued_without_a_native_write(monkeypatch):
    from cc_remote.protocol import Error
    bus, machine, target, rows, source, recipient = backend(monkeypatch)
    async def query(*_, **__): return Error(code='message_tools_pending', message='pending')
    monkeypatch.setattr(machine, '_handle_immediate_query', query)
    receipt = await bus.call(source, 'send_message', {'session_ref': recipient, 'message': 'hi', 'request_id': 'tools'})
    assert receipt['status'] == 'queued'
    assert bus.store.pending()[0]['prompt'] == envelope(source, 'hi')
    await bus.close()
    await machine._command_scheduler.close()


def legacy_handle(*, dedicated=False):
    from cc_remote.config import WrapperConfig
    from cc_remote.wrapper.sdk import SdkHandle
    from cc_remote.wrapper.ask import make_ask_server
    from cc_remote.session_messages import make_server
    from mcp import types
    async def ask(*_): return 'answer'
    calls = []
    async def message(name, args, meta):
        calls.append((name, args))
        return {'sessions': []}
    handle = SdkHandle(WrapperConfig(), make_ask_server(ask, ask, ask_batch=ask))
    handle.message_server = make_server(message)
    handle.needs_message_tools_refresh = True

    class Control:
        name = 'cc_remote_messages' if dedicated else 'cc-remote-ask'
        server = handle.message_server if dedicated else handle.ask_server
        tools = []
        requests = []
        state = 'connected'
        ask_state = 'connected'
        ask_tools = []
        dropped_tools = set()
        fail_off = False
        async def _send_control_request(self, request, **_):
            self.requests.append(request)
            if request['subtype'] == 'mcp_status':
                rows = ([] if self.state == 'absent' else
                        [{'name': self.name, 'status': self.state, 'tools': self.tools}])
                if dedicated:
                    rows.append({'name': 'cc-remote-ask', 'status': self.ask_state, 'tools': self.ask_tools})
                return {'mcpServers': rows}
            name = request['serverName']
            assert name in ({'cc-remote-ask', self.name} if dedicated else {self.name})
            assert request == {'subtype': 'mcp_toggle', 'serverName': name, 'enabled': request['enabled']}
            separate_ask = dedicated and name == 'cc-remote-ask'
            if request['enabled']:
                server = handle.ask_server if separate_ask else self.server
                result = await server.request_handlers[types.ListToolsRequest](types.ListToolsRequest())
                tools = [{'name': tool.name} for tool in result.root.tools if tool.name not in self.dropped_tools]
                if separate_ask:
                    self.ask_tools, self.ask_state = tools, 'connected'
                else:
                    self.tools, self.state = tools, 'connected'
            else:
                if separate_ask:
                    self.ask_state = 'disabled'
                else:
                    self.state = 'disabled'
                if self.fail_off:
                    self.fail_off = False
                    raise TimeoutError('off acknowledgment lost')

    control = Control()
    handle.client = SimpleNamespace(_query=control)
    return handle, control, calls


@pytest.mark.asyncio
@pytest.mark.parametrize('dedicated', [False, True])
async def test_attached_claude_refreshes_actual_tool_list_and_preserves_ask(dedicated):
    from mcp.server.lowlevel.server import RequestContext, request_ctx
    from mcp import types
    handle, control, calls = legacy_handle(dedicated=dedicated)
    client = handle.client
    assert await handle.refresh_message_tools()
    assert handle.client is client and not handle.needs_message_tools_refresh
    assert {t['name'] for t in control.tools} == ({'list_sessions', 'send_message', 'message_status'}
        | (set() if dedicated else {'ask_user', 'set_mode'}))
    if dedicated:
        assert {t['name'] for t in control.ask_tools} == {'ask_user', 'set_mode'}
    assert [r['enabled'] for r in control.requests if r['subtype'] == 'mcp_toggle'] == (
        [False, True] * (2 if dedicated else 1))
    before = len(control.requests)
    assert await handle.refresh_message_tools()
    assert len(control.requests) == before
    token = request_ctx.set(RequestContext(request_id=1, meta=None, session=None, lifespan_context=None))
    try:
        req = types.CallToolRequest(params=types.CallToolRequestParams(name='list_sessions', arguments={}))
        result = (await control.server.request_handlers[types.CallToolRequest](req)).root
        assert not result.isError and calls == [('list_sessions', {})]
        if not dedicated:
            req = types.CallToolRequest(params=types.CallToolRequestParams(name='ask_user', arguments={
                'question': 'pick', 'options': [{'label': 'a'}, {'label': 'b'}]}))
            result = (await control.server.request_handlers[types.CallToolRequest](req)).root
            assert result.content[0].text == 'answer'
    finally:
        request_ctx.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize('dedicated', [False, True])
@pytest.mark.parametrize('missing', ['ask_user', 'set_mode'])
async def test_connected_message_tools_do_not_hide_a_missing_ask_tool(dedicated, missing):
    handle, control, _ = legacy_handle(dedicated=dedicated)
    control.dropped_tools = {missing}
    markers = []
    async def mark(name): markers.append(name)
    handle.client.mark_message_tools_refresh = mark
    assert not await handle.refresh_message_tools()
    assert handle.needs_message_tools_refresh
    assert handle._message_tools_toggle_started == 'cc-remote-ask'
    assert markers == ['cc-remote-ask']
    assert control.ask_state == 'connected' and control.state == 'connected'
    control.dropped_tools = set()
    assert await handle.refresh_message_tools()
    assert not handle.needs_message_tools_refresh
    assert handle._message_tools_toggle_started is None
    assert markers[-1] is None


@pytest.mark.asyncio
async def test_ask_schema_refresh_does_not_require_session_messaging():
    handle, control, _ = legacy_handle()
    handle.message_server = None
    assert await handle.refresh_message_tools()
    assert {t['name'] for t in control.tools} == {'ask_user', 'set_mode'}
    assert not handle.needs_message_tools_refresh


@pytest.mark.asyncio
@pytest.mark.parametrize('pending', [None, 'cc_remote_messages'])
async def test_disabled_ask_does_not_block_independent_message_refresh(pending):
    handle, control, _ = legacy_handle(dedicated=True)
    control.ask_state = 'disabled'
    handle._message_tools_toggle_started = pending
    control.state = 'disabled' if pending else 'connected'
    markers = []
    async def mark(name): markers.append(name)
    handle.client.mark_message_tools_refresh = mark
    client = handle.client
    assert await handle.refresh_message_tools()
    assert handle.client is client and not handle.needs_message_tools_refresh
    assert handle._message_tools_toggle_started is None
    assert control.ask_state == 'disabled' and control.state == 'connected'
    assert {tool['name'] for tool in control.tools} == {'list_sessions', 'send_message', 'message_status'}
    assert markers == ['cc_remote_messages', None]
    assert [request for request in control.requests if request['subtype'] == 'mcp_toggle'] == [
        {'subtype': 'mcp_toggle', 'serverName': 'cc_remote_messages', 'enabled': False},
        {'subtype': 'mcp_toggle', 'serverName': 'cc_remote_messages', 'enabled': True},
    ]


@pytest.mark.asyncio
async def test_disabled_ask_cannot_mask_missing_dedicated_message_tools():
    handle, control, _ = legacy_handle(dedicated=True)
    control.ask_state = 'disabled'
    control.dropped_tools = {'send_message'}
    assert not await handle.refresh_message_tools()
    assert handle.needs_message_tools_refresh
    assert handle._message_tools_toggle_started == 'cc_remote_messages'
    assert control.ask_state == 'disabled'
    control.dropped_tools.clear()
    assert await handle.refresh_message_tools()
    assert handle._message_tools_toggle_started is None
    assert control.ask_state == 'disabled'


@pytest.mark.asyncio
async def test_pending_ask_toggle_is_restored_even_with_independent_messaging():
    handle, control, _ = legacy_handle(dedicated=True)
    control.ask_state = 'disabled'
    handle._message_tools_toggle_started = 'cc-remote-ask'
    assert await handle.refresh_message_tools()
    assert control.ask_state == 'connected'
    assert {tool['name'] for tool in control.ask_tools} == {'ask_user', 'set_mode'}
    assert handle._message_tools_toggle_started is None


@pytest.mark.asyncio
async def test_restore_pending_message_toggle_before_refreshing_separate_ask():
    handle, control, _ = legacy_handle(dedicated=True)
    control.state = 'disabled'
    handle._message_tools_toggle_started = 'cc_remote_messages'
    markers = []
    async def mark(name): markers.append(name)
    handle.client.mark_message_tools_refresh = mark
    assert await handle.refresh_message_tools()
    assert markers == ['cc_remote_messages', None, 'cc-remote-ask', None]
    assert [r['serverName'] for r in control.requests if r['subtype'] == 'mcp_toggle'] == [
        'cc_remote_messages', 'cc_remote_messages', 'cc-remote-ask', 'cc-remote-ask']


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['disabled', 'connected', 'failed', 'pending', 'absent'])
async def test_pending_message_refresh_can_finish_after_messaging_is_disabled(state):
    handle, control, _ = legacy_handle(dedicated=True)
    client = handle.client
    handle.message_server = None
    handle._message_tools_toggle_started = 'cc_remote_messages'
    control.state = state
    markers = []
    async def mark(name):
        if name is None:
            assert control.state in {'disabled', 'absent'}
        markers.append(name)
    client.mark_message_tools_refresh = mark
    assert await handle.refresh_message_tools()
    assert handle.client is client and not handle.needs_message_tools_refresh
    assert handle._message_tools_toggle_started is None
    assert markers == [None, 'cc-remote-ask', None]
    assert {tool['name'] for tool in control.ask_tools} == {'ask_user', 'set_mode'}
    assert [request['enabled'] for request in control.requests
            if request.get('serverName') == 'cc_remote_messages'] == (
                [] if state in {'disabled', 'absent'} else [False])


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['off_ack', 'still_connected', 'missing_status', 'marker_ack'])
async def test_retiring_pending_message_route_retains_intent_until_confirmed(failure):
    handle, control, _ = legacy_handle(dedicated=True)
    handle.message_server = None
    handle._message_tools_toggle_started = 'cc_remote_messages'
    markers = []
    async def mark(name):
        if failure == 'marker_ack':
            raise ConnectionError('metadata acknowledgment lost')
        markers.append(name)
    handle.client.mark_message_tools_refresh = mark
    original_send = control._send_control_request
    async def send(request, **kwargs):
        if request['subtype'] == 'mcp_status' and failure == 'missing_status':
            return {}
        result = await original_send(request, **kwargs)
        if request.get('serverName') == 'cc_remote_messages' and failure == 'still_connected':
            control.state = 'connected'
        return result
    control._send_control_request = send
    control.fail_off = failure == 'off_ack'
    assert not await handle.refresh_message_tools()
    assert handle.needs_message_tools_refresh
    assert handle._message_tools_toggle_started == 'cc_remote_messages'
    assert not markers
    assert not any(request.get('serverName') == 'cc-remote-ask' for request in control.requests)
    failure = None
    assert await handle.refresh_message_tools()
    assert handle._message_tools_toggle_started is None
    assert control.state == 'disabled'
    assert not any(request.get('serverName') == 'cc_remote_messages' and request['enabled']
                   for request in control.requests)


@pytest.mark.asyncio
async def test_unknown_pending_route_is_not_cleared_or_toggled():
    handle, control, _ = legacy_handle(dedicated=True)
    handle.message_server = None
    handle._message_tools_toggle_started = 'unowned-route'
    assert not await handle.refresh_message_tools()
    assert handle._message_tools_toggle_started == 'unowned-route'
    assert control.requests == [{'subtype': 'mcp_status'}]


@pytest.mark.asyncio
async def test_lost_toggle_off_ack_always_restores_ask_and_remains_retryable():
    handle, control, _ = legacy_handle()
    control.fail_off = True
    assert not await handle.refresh_message_tools()
    assert control.state == 'connected'
    assert handle.needs_message_tools_refresh
    assert await handle.refresh_message_tools()


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['pending', 'disabled'])
async def test_refresh_does_not_enable_a_previously_disabled_or_unready_server(state):
    handle, control, _ = legacy_handle()
    control.state = state
    assert not await handle.refresh_message_tools()
    assert control.requests == [{'subtype': 'mcp_status'}]


@pytest.mark.asyncio
@pytest.mark.parametrize('busy', ['_turn_active', '_turn_consumer_active', '_background_callbacks_pending', '_autonomous_steer_root'])
async def test_tool_refresh_never_crosses_native_turn_or_pending_callback(busy):
    handle, control, _ = legacy_handle()
    setattr(handle, busy, True)
    assert not await handle.refresh_message_tools()
    assert not control.requests
    assert handle.needs_message_tools_refresh


@pytest.mark.asyncio
async def test_legacy_tools_are_recognized_as_receipts_not_unattributed_tool_output():
    from cc_remote.session_messages import message_target
    assert message_target('mcp__cc-remote-ask__send_message', {'session_ref': TARGET}) == TARGET
    assert message_target('mcp__other__send_message', {'session_ref': TARGET}) is None


@pytest.mark.asyncio
async def test_failed_sdk_route_can_be_repaired_without_restarting_claude():
    handle, control, _ = legacy_handle()
    control.state = 'failed'
    assert await handle.refresh_message_tools()
    assert control.state == 'connected'


def test_restart_only_replays_queued_never_inflight_even_with_remaining_body(tmp_path):
    store = MessageStore(tmp_path / 'messages')
    queued, _ = store.claim(SOURCE, 'queued', TARGET, 'not sent', queue=True)
    inflight, _ = store.claim(SOURCE, 'started', TARGET, 'might have run', queue=True)
    assert store.begin_delivery(inflight['message_id'])
    store.recover()
    assert [r['id'] for r in store.pending()] == [queued['message_id']]
    assert store.get(SOURCE, 'started')['status'] == 'unknown'
    store.unsubmitted(inflight['message_id'], 'cannot revive an old claim')
    assert [r['id'] for r in store.pending()] == [queued['message_id']]


def test_wrapper_recovery_preserves_inflight_receipt_owned_by_gemini_service(tmp_path):
    store = MessageStore(tmp_path / "messages")
    direct, _ = store.claim(SOURCE, "direct", TARGET, "direct sender", queue=True)
    gemini, _ = store.claim(SOURCE, "gemini", "ccr:antigravity:fixture", "persistent sender", queue=True)
    assert store.begin_delivery(direct["message_id"])
    assert store.begin_delivery(gemini["message_id"])
    store.recover(lambda target: not target.startswith("ccr:antigravity:"))
    store.settle(gemini["message_id"], "accepted")
    assert store.get(SOURCE, "gemini")["status"] == "accepted"
    assert store.get(SOURCE, "direct")["status"] == "unknown"


@pytest.mark.asyncio
async def test_refresh_intent_survives_controller_loss_between_off_and_on():
    handle, control, _ = legacy_handle()
    markers = []
    async def mark(name): markers.append(name)
    handle.client.mark_message_tools_refresh = mark
    # A replacement restores only a disable proved to be our unfinished toggle.
    control.state = 'disabled'
    handle._message_tools_toggle_started = control.name
    assert await handle.refresh_message_tools()
    assert markers == [control.name, None]
    assert control.state == 'connected'


@pytest.mark.asyncio
async def test_failed_refresh_marker_does_not_disable_anything():
    handle, control, _ = legacy_handle()
    async def mark(_): raise ConnectionError('lost metadata ack')
    handle.client.mark_message_tools_refresh = mark
    assert not await handle.refresh_message_tools()
    assert control.requests == [{'subtype': 'mcp_status'}]
    assert handle.needs_message_tools_refresh


@pytest.mark.asyncio
async def test_optional_tool_refresh_does_not_block_ordinary_chat_with_background_work(monkeypatch):
    from cc_remote.protocol import Error, Query
    bus, machine, target, *_ = backend(monkeypatch)
    handle, control, _ = legacy_handle()
    target.sdk = handle
    target.claude_active_tasks.add('background-agent')
    async def noop(*_, **__): pass
    async def no_owner(*_): return False
    monkeypatch.setattr(machine, '_adopt_claude_broker_handle', noop)
    monkeypatch.setattr(machine, '_prime_claude_ownership', no_owner)
    monkeypatch.setattr(machine, '_watch_session', AsyncMock(side_effect=lambda *_: None))
    monkeypatch.setattr(machine, '_prepare_claude_context_before_query', noop)
    launched = []
    async def run(*args, **_): launched.append(args[1])
    monkeypatch.setattr(machine, '_run_turn', run)
    async with target.query_lock:
        result = await machine._handle_immediate_query(target, Query(sid=target.key, prompt='ordinary chat', msg_id='human'))
    assert result is None
    await target.turn_task
    assert launched == ['ordinary chat'] and not control.requests
    target.state = 'idle'
    bus.active.add('message-test')
    async with target.query_lock:
        result = await machine._handle_immediate_query(target, Query(sid=target.key, prompt='reply', msg_id='message-test'))
    assert isinstance(result, Error) and result.code == 'message_tools_pending'
    assert launched == ['ordinary chat'] and not control.requests
    await machine._command_scheduler.close()


@pytest.mark.asyncio
async def test_message_delivery_refreshes_independent_route_with_ask_disabled(monkeypatch):
    from cc_remote.protocol import Query
    bus, machine, target, *_ = backend(monkeypatch)
    handle, control, _ = legacy_handle(dedicated=True)
    target.sdk = handle
    client = handle.client
    control.ask_state = 'disabled'
    bus.active.add('message-test')
    async def noop(*_, **__): pass
    async def no_owner(*_): return False
    monkeypatch.setattr(machine, '_adopt_claude_broker_handle', noop)
    monkeypatch.setattr(machine, '_prime_claude_ownership', no_owner)
    monkeypatch.setattr(machine, '_watch_session', AsyncMock(side_effect=lambda *_: None))
    monkeypatch.setattr(machine, '_prepare_claude_context_before_query', noop)
    launched = []
    async def run(*args, **_): launched.append(args[1])
    monkeypatch.setattr(machine, '_run_turn', run)
    try:
        async with target.query_lock:
            result = await machine._handle_immediate_query(target, Query(
                sid=target.key, prompt='reply to the other session', msg_id='message-test'))
        assert result is None
        await target.turn_task
        assert launched == ['reply to the other session']
        assert handle.client is client and not handle.needs_message_tools_refresh
        assert control.ask_state == 'disabled' and control.state == 'connected'
    finally:
        await machine._command_scheduler.close()


@pytest.mark.asyncio
async def test_ordinary_chat_recovers_pending_refresh_after_messaging_opt_out(monkeypatch):
    from cc_remote.protocol import Query
    _, machine, target, *_ = backend(monkeypatch)
    machine._session_messages = None
    handle, control, _ = legacy_handle(dedicated=True)
    target.sdk = handle
    client = handle.client
    handle.message_server = None
    handle._message_tools_toggle_started = 'cc_remote_messages'
    control.state = 'disabled'
    async def noop(*_, **__): pass
    async def no_owner(*_): return False
    monkeypatch.setattr(machine, '_adopt_claude_broker_handle', noop)
    monkeypatch.setattr(machine, '_prime_claude_ownership', no_owner)
    monkeypatch.setattr(machine, '_watch_session', AsyncMock(side_effect=lambda *_: None))
    monkeypatch.setattr(machine, '_prepare_claude_context_before_query', noop)
    launched = []
    async def run(*args, **_): launched.append(args[1])
    monkeypatch.setattr(machine, '_run_turn', run)
    try:
        async with target.query_lock:
            result = await machine._handle_immediate_query(target, Query(
                sid=target.key, prompt='ordinary chat', msg_id='human'))
        assert result is None
        await target.turn_task
        assert launched == ['ordinary chat']
        assert handle.client is client and handle._message_tools_toggle_started is None
        assert control.state == 'disabled'
    finally:
        await machine._command_scheduler.close()
