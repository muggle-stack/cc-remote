"""Official DSH API envelopes, identity lifetimes and cold-read boundaries."""
import asyncio
import base64
import hashlib
import json
import os

import httpx
import pytest

from cc_remote.wrapper.dsh_client import (
    DshClient, DshConnection, DshError, local_origin, native_session_id,
)

ORIGIN = "http://127.0.0.1:3080"
TOKEN = "a" * 43


class Socket:
    def __init__(self):
        self.frames = asyncio.Queue()
        self.sent = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.frames.get()
        if value is None:
            raise StopAsyncIteration
        return value

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def close(self):
        if not self.closed:
            self.closed = True
            self.frames.put_nowait(None)


class Host:
    def __init__(self):
        self.calls = []
        self.sockets = []
        self.cookies = []
        self.exchange_count = 0
        self.answers = {}

    def request(self, request):
        if request.url.path == "/connect":
            assert request.url.params["token"] == TOKEN
            assert request.url.params["interactions"] == "answer"
            self.exchange_count += 1
            name = "dsh-auth-" + base64.urlsafe_b64encode(hashlib.sha256(b"127.0.0.1:3080").digest()).decode().rstrip("=")
            return httpx.Response(302, headers={"location": "./api/remote.mux",
                "set-cookie": f"{name}=v2.peer{self.exchange_count}.signature; Path=/; HttpOnly; SameSite=Strict"})
        data = json.loads(request.content)
        assert data["type"] == "client-request"
        self.calls.append((data["method"], data["payload"]["args"]))
        answer = self.answers.get(data["method"], {"items": []})
        if callable(answer):
            answer = answer(request, data)
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json={"type": "server-response", "rpcId": data["rpcId"],
                                        "result": {"ok": True, "value": answer}})

    async def connect(self, url, **kwargs):
        assert url == "ws://127.0.0.1:3080/api/remote.mux"
        assert kwargs["proxy"] is None
        assert kwargs["origin"] == ORIGIN
        self.cookies.append(kwargs["additional_headers"]["Cookie"])
        socket = Socket()
        self.sockets.append(socket)
        return socket

    def client(self):
        return DshClient(DshConnection(ORIGIN, TOKEN), http_transport=httpx.MockTransport(self.request),
                         socket_connect=self.connect)


@pytest.mark.parametrize("origin", [
    "https://127.0.0.1:3080", "http://localhost:3080", "http://10.0.0.1:3080",
    "http://127.0.0.1", "http://127.0.0.1:3080/api", "http://user@127.0.0.1:3080",
    "http://[::ffff:127.0.0.1]:3080", "http://127.0.0.1:3080?token=secret",
])
def test_only_literal_loopback_authorities(origin):
    with pytest.raises(DshError):
        local_origin(origin)


def test_connection_repr_and_file_do_not_expose_control_token(tmp_path):
    connection = DshConnection.from_launch_url(ORIGIN + "/?token=" + TOKEN)
    assert TOKEN not in repr(connection)
    path = tmp_path / "connection.json"
    path.write_text(json.dumps({"schema": 1, "origin": ORIGIN, "token": TOKEN}))
    path.chmod(0o600)
    assert DshConnection.load(path) == connection
    path.chmod(0o644)
    with pytest.raises(DshError):
        DshConnection.load(path)
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(DshError):
        DshConnection.load(link)
    hardlink = tmp_path / "hardlink"
    os.link(path, hardlink)
    with pytest.raises(DshError):
        DshConnection.load(path)


@pytest.mark.parametrize("sid", ["abc", "codex@abc", "dsh@", "dsh@../foo", "dsh@a/b"])
def test_native_identity_cannot_cross_engines(sid):
    with pytest.raises(DshError):
        native_session_id(sid)


@pytest.mark.asyncio
async def test_api_peer_is_owned_by_one_socket_and_exchanged_again_after_disconnect(caplog):
    host = Host()
    client = host.client()
    await client.list_sessions()
    await client.list_sessions()
    assert host.exchange_count == 1
    assert host.calls == [("session/list", {"_request": {}})] * 2
    peer = client._peer
    await host.sockets[0].close()
    await peer.reader
    await client.list_sessions()
    assert host.exchange_count == 2
    assert host.cookies[0] != host.cookies[1]
    assert TOKEN not in caplog.text
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint,args,write", [
    ("pluginMarket/readEntryDetail", {"url": "https://index.test/plugins.json", "name": "example", "version": "1"}, False),
    ("pluginMarket/setIndexes", {"urls": []}, True),
    ("schedule/deleteInactive", {}, True),
    ("actionCards/act", {"agentId": "root", "callId": "proposal", "verb": "dismiss"}, True),
])
async def test_product_endpoints_classify_disconnects_and_never_retry(endpoint, args, write):
    host = Host()
    client = host.client()
    host.answers[endpoint] = {"ok": "fixture"}
    assert await client.rpc(endpoint, args) == {"ok": "fixture"}
    host.answers[endpoint] = httpx.Response(502)
    with pytest.raises(DshError) as failure:
        await client.rpc(endpoint, args)
    assert failure.value.outcome_unknown is write
    assert host.calls == [(endpoint, args)] * 2
    await client.close()


@pytest.mark.asyncio
async def test_follow_burst_during_snapshot_reads_does_not_disconnect_or_drop_frames():
    host = Host()
    client = host.client()
    stream = client.stream("session/follow", {"request": {"address": {"kind": "session", "sessionId": "one"}}})
    first = asyncio.create_task(anext(stream))
    while not host.sockets or not host.sockets[0].sent:
        await asyncio.sleep(0)
    socket = host.sockets[0]
    stream_id = socket.sent[0]["streamId"]
    for index in range(513):
        socket.frames.put_nowait(json.dumps({"type": "item", "streamId": stream_id, "value": {"index": index}}))
    assert await first == {"index": 0}
    # Simulate the snapshot consumer awaiting history/control reads while
    # native tokens and tools continue. The former 128-item queue disconnected.
    async with asyncio.timeout(2):
        while not socket.frames.empty():
            await asyncio.sleep(0)
    assert not socket.closed
    assert [await anext(stream) for _ in range(512)] == [{"index": i} for i in range(1, 513)]
    assert client._peer.queued_bytes == 0 and host.exchange_count == 1
    assert host.calls == []  # no prompt or mutation replay
    await stream.aclose()
    await client.close()


@pytest.mark.parametrize("cause", ["items", "bytes", "invalid"])
@pytest.mark.asyncio
async def test_stream_limits_remain_bounded_and_errors_distinguish_overflow(monkeypatch, cause):
    from cc_remote.wrapper import dsh_client
    host = Host()
    client = host.client()
    peer = await client._connection()
    queue = peer.streams["one"] = asyncio.Queue(1)
    if cause == "bytes":
        monkeypatch.setattr(dsh_client, "MAX_RESPONSE_BYTES", 32)
    if cause == "invalid":
        peer.socket.frames.put_nowait("invalid json")
    else:
        raw = json.dumps({"type": "item", "streamId": "one", "value": "text"})
        for _ in range(2 if cause == "items" else 1):
            peer.socket.frames.put_nowait(raw)
    await asyncio.wait_for(peer.reader, 2)
    error, size = queue.get_nowait()
    assert error.code == ("invalid_stream" if cause == "invalid" else "stream_overflow")
    assert peer.queued_bytes == size == 0 and peer.socket.closed
    assert host.calls == []
    await client.close()


@pytest.mark.asyncio
async def test_uncertain_mutation_is_never_retried_or_marked_rejected():
    host = Host()
    host.answers["session/prompt"] = httpx.Response(502)
    client = host.client()
    with pytest.raises(DshError) as error:
        await client.rpc("session/prompt", {"request": {"sessionId": "one"}})
    assert error.value.outcome_unknown
    assert len(host.calls) == 1
    await client.close()


@pytest.mark.asyncio
async def test_response_must_match_request_identity():
    host = Host()
    host.answers["session/prompt"] = httpx.Response(200, json={
        "type": "server-response", "rpcId": "other", "result": {"ok": True, "value": {"accepted": True}},
    })
    client = host.client()
    with pytest.raises(DshError) as error:
        await client.rpc("session/prompt")
    assert error.value.outcome_unknown
    await client.close()


@pytest.mark.asyncio
async def test_cold_history_uses_exact_projections_cursor_never_follow_or_resume():
    host = Host()
    host.answers["session/projections"] = {"kind": "sequenced", "asOfSeq": 27, "values": {"title": "test"}}
    def page(request, data):
        before = data["payload"]["args"]["request"]["beforeSeq"]
        event = {"seq": 22, "type": "turn/start"} if before == 25 else {"seq": 21, "type": "turn/end"}
        return {"records": [{"type": "event", "event": event}], "hasMore": True}
    host.answers["session/page"] = page
    client = host.client()
    result = await client.history_snapshot("dsh@one", before_seq=25)
    assert result["cursor"] == 27
    assert host.calls == [
        ("session/projections", {"request": {"sessionId": "one"}}),
        ("session/page", {"request": {"address": {"kind": "session", "sessionId": "one"},
                                     "throughSeq": 27, "beforeSeq": 25, "maxMessages": 12}}),
        ("session/page", {"request": {"address": {"kind": "session", "sessionId": "one"},
                                     "throughSeq": 27, "beforeSeq": 22, "maxMessages": 1}}),
    ]
    assert host.sockets[0].sent == []
    await client.close()


@pytest.mark.asyncio
async def test_migration_hints_do_not_authorize_history_or_native_migration():
    host = Host()
    host.answers["session/projections"] = {"kind": "migration-required", "values": {"title": "legacy"}}
    client = host.client()
    with pytest.raises(DshError, match="格式升级"):
        await client.history_snapshot("dsh@legacy")
    assert len(host.calls) == 1
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("records", [
    [None], [{"type": "event", "event": {"seq": 28}}],
    [{"type": "event", "event": {"seq": 2}}, {"type": "event", "event": {"seq": 2}}],
    [{"type": "event", "event": {"seq": True}}],
])
async def test_history_rejects_malformed_or_unordered_events(records):
    host = Host()
    host.answers["session/projections"] = {"kind": "sequenced", "asOfSeq": 27, "values": {}}
    host.answers["session/page"] = {"records": records, "hasMore": False}
    client = host.client()
    with pytest.raises(DshError):
        await client.history_snapshot("dsh@one")
    await client.close()


@pytest.mark.asyncio
async def test_disposing_follow_only_cancels_subscription():
    host = Host()
    client = host.client()
    stream = client.stream("session/follow", {"request": {"address": {"kind": "session", "sessionId": "one"}}})
    item = asyncio.create_task(anext(stream))
    while not host.sockets or not host.sockets[0].sent:
        await asyncio.sleep(0)
    socket = host.sockets[0]
    opened = socket.sent[0]
    await socket.frames.put(json.dumps({"type": "item", "streamId": opened["streamId"], "value": {"type": "snapshot"}}))
    assert await item == {"type": "snapshot"}
    await stream.aclose()
    assert socket.sent[-1] == {"type": "cancel", "streamId": opened["streamId"]}
    assert host.calls == []  # no session/cancel and no prompt
    await client.close()


@pytest.mark.asyncio
async def test_truncated_message_page_recovers_only_its_native_turn_prefix():
    host = Host()
    host.answers['session/projections'] = {'kind': 'sequenced', 'asOfSeq': 30, 'values': {}}
    def row(seq, kind):
        return {'type': 'event', 'event': {'seq': seq, 'type': kind, 'data': {}}}
    def page(request, data):
        fields = data['payload']['args']['request']
        assert fields['throughSeq'] == 30
        if 'beforeSeq' not in fields:
            return {'hasMore': True, 'records': [row(25, 'assistant/message'), row(30, 'turn/end')]}
        if fields['beforeSeq'] == 5:
            assert fields['maxMessages'] == 1
            return {'hasMore': True, 'records': [row(4, 'turn/end')]}
        assert fields['beforeSeq'] == 25
        return {'hasMore': True, 'records': [row(4, 'turn/end'), row(5, 'turn/start'), row(6, 'user/message')]}
    host.answers['session/page'] = page
    client = host.client()
    try:
        value = await client.history_snapshot('dsh@native')
        assert [r['event']['seq'] for r in value['records']] == [5, 6, 25, 30]
        assert value['hasMore']
        assert all(method in {'session/projections', 'session/page'} for method, _ in host.calls)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("truncated", [False, True])
async def test_first_turn_does_not_offer_setup_records_as_older_conversations(truncated):
    host = Host()
    host.answers["session/projections"] = {"kind": "sequenced", "asOfSeq": 8, "values": {}}
    def row(seq, kind):
        return {"type": "event", "event": {"seq": seq, "type": kind, "data": {}}}
    setup = [row(0, "permission/preset"), row(1, "model/selection"), row(2, "command/run")]
    turn = [row(3, "turn/start"), row(4, "user/message")]
    tail = [row(7, "assistant/message"), row(8, "turn/end")]
    def page(request, data):
        fields = data["payload"]["args"]["request"]
        assert fields["throughSeq"] == 8
        if "beforeSeq" not in fields:
            return {"records": tail if truncated else turn + tail, "hasMore": True}
        if fields["beforeSeq"] == 7:
            return {"records": setup + turn, "hasMore": False}
        assert fields["beforeSeq"] == 3
        return {"records": setup, "hasMore": False}
    host.answers["session/page"] = page
    client = host.client()
    try:
        result = await client.history_snapshot("dsh@one")
        assert not result["hasMore"]
        assert [r["event"]["seq"] for r in result["records"]] == [3, 4, 7, 8]
        assert all(method in {"session/projections", "session/page"} for method, _ in host.calls)
        assert not host.sockets[0].sent
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("earlier_turn", [False, True])
async def test_pagination_looks_past_non_conversation_records_without_hiding_real_history(earlier_turn):
    host = Host()
    host.answers["session/projections"] = {"kind": "sequenced", "asOfSeq": 20, "values": {}}
    def page(request, data):
        fields = data["payload"]["args"]["request"]
        before = fields.get("beforeSeq", 21)
        seq, kind = {21: (15, "turn/start"), 15: (5, "user/message"),
                     5: (0, "turn/end" if earlier_turn else "permission/preset")}[before]
        return {"records": [{"type": "event", "event": {"seq": seq, "type": kind, "data": {}}}],
                "hasMore": before != 5}
    host.answers["session/page"] = page
    client = host.client()
    try:
        result = await client.history_snapshot("dsh@one")
        assert result["hasMore"] is earlier_turn
        assert [call[1]["request"].get("beforeSeq") for call in host.calls[1:]] == [None, 15, 5]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cold_child_page_uses_verified_parent_address():
    host = Host()
    host.answers['session/projections'] = {'kind': 'sequenced', 'asOfSeq': 2, 'values': {'subagent': {'mode': 'continuable'}}}
    host.answers['session/list'] = {'items': [{'sessionId': 'child', 'origin': 'subagent', 'parentSessionId': 'parent'}]}
    host.answers['session/page'] = {'records': [], 'hasMore': False}
    client = host.client()
    try:
        await client.history_snapshot('dsh@child')
        request = host.calls[-1][1]['request']
        assert request['address'] == {'kind': 'subagent', 'parentSessionId': 'parent', 'childSessionId': 'child', 'mode': 'unknown'}
        assert not host.sockets[0].sent
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [200, 404, 422, 503, "timeout"])
async def test_direct_schedule_write_is_authenticated_and_never_retried(outcome):
    host, writes = Host(), []
    original = host.request
    def request(req):
        if req.url.path != "/api/schedule/create":
            return original(req)
        assert "dsh-auth-" in req.headers["cookie"]
        writes.append(json.loads(req.content))
        if outcome == "timeout":
            raise httpx.ReadTimeout("unconfirmed")
        return httpx.Response(outcome, json={"type": "server-response", "rpcId": writes[-1]["rpcId"],
            "result": {"ok": True, "value": {"id": "schedule"}}})
    host.request = request
    client = host.client()
    try:
        if outcome == 200:
            assert await client.create_schedule({"title": "Check"}) == {"id": "schedule"}
        else:
            with pytest.raises(DshError) as error:
                await client.create_schedule({"title": "Check"})
            assert error.value.outcome_unknown == (outcome in {422, 503, "timeout"})
        assert len(writes) == 1
        assert writes[0]["payload"] == {"args": {"request": {"title": "Check"}}}
    finally:
        await client.close()
