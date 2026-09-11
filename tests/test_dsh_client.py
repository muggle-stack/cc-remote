"""DSH 0.1.5 wire contracts; no engine, credentials or model request involved."""
import asyncio
import base64
import hashlib
import json
import os
import time

import httpx
import pytest
from websockets.asyncio.server import serve

from cc_remote.wrapper.dsh_client import (
    DshClient, DshConnection, DshError, exchange_launch_url,
    local_origin, native_session_id, wire_session_id,
)
from cc_remote.wrapper.dsh_pair import save_connection


def connection(origin="http://127.0.0.1:3080", *, expired=False):
    authority = origin.removeprefix("http://")
    def b64(value):
        return base64.urlsafe_b64encode(value).decode().rstrip("=")
    name = "dsh-auth-" + b64(hashlib.sha256(authority.encode()).digest())
    body = b64(json.dumps({
        "version": 1, "authority": authority, "issuedAt": 1,
        "expiresAt": int(time.time() * 1000) + (-1 if expired else 60000),
    }).encode())
    return DshConnection(origin, f"{name}=v1.{body}.testsignature")


@pytest.mark.parametrize("value", [
    "http://localhost:3080", "http://example.com:3080", "http://192.168.1.1:3080",
    "https://127.0.0.1:3080", "http://127.0.0.1", "http://127.0.0.1:0",
    "http://user:secret@127.0.0.1:3080", "http://127.0.0.1:3080/api",
    "http://127.0.0.1:3080/?token=secret", "http://127.0.0.1:3080/#secret",
    "http://127.0.0.1:3080\n", "http://[::ffff:127.0.0.1]:3080",
])
def test_only_literal_loopback_origin(value):
    with pytest.raises(DshError):
        local_origin(value)


def test_ids_and_ipv6():
    assert local_origin("http://[::1]:3080/") == "http://[::1]:3080"
    assert native_session_id(wire_session_id("abc-123")) == "abc-123"
    for sid in ("abc", "dsh@../etc", "dsh@", "dsh@a/b", "dsh@" + "a" * 201):
        with pytest.raises(DshError):
            native_session_id(sid)


def test_private_cookie_roundtrip_and_no_repr_leak(tmp_path):
    conn = connection()
    assert conn.cookie not in repr(conn)
    path = tmp_path / "pair.json"
    save_connection(conn, path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert DshConnection.load(path) == conn
    path.chmod(0o644)
    with pytest.raises(DshError, match="仅当前用户"):
        DshConnection.load(path)
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(DshError):
        DshConnection.load(link)


def test_cookie_scope_and_expiry():
    conn = connection()
    with pytest.raises(DshError):
        DshConnection("http://127.0.0.1:3081", conn.cookie)
    with pytest.raises(DshError, match="过期"):
        connection(expired=True)


@pytest.mark.asyncio
async def test_exchange_uses_official_redirect_without_following():
    conn = connection()
    seen = []
    async def handle(request):
        seen.append(request)
        assert request.url.params["token"] == "A" * 43
        return httpx.Response(303, headers={
            "Location": "/", "Set-Cookie": conn.cookie + "; Path=/; HttpOnly; SameSite=Strict",
        })
    paired = await exchange_launch_url(
        conn.origin + "/?token=" + "A" * 43,
        http_transport=httpx.MockTransport(handle),
    )
    assert paired == conn
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_exchange_rejects_foreign_redirect_and_hides_secret():
    secret = "A" * 43
    async def handle(_request):
        return httpx.Response(303, headers={"Location": "https://evil.invalid/" + secret})
    with pytest.raises(DshError) as error:
        await exchange_launch_url(
            "http://127.0.0.1:3080/?token=" + secret,
            http_transport=httpx.MockTransport(handle),
        )
    assert secret not in str(error.value)


@pytest.mark.asyncio
async def test_exchange_does_not_log_login_token(caplog):
    caplog.set_level("DEBUG")
    conn = connection()
    async def handle(_request):
        return httpx.Response(303, headers={"Location": "/", "Set-Cookie": conn.cookie})
    await exchange_launch_url(
        conn.origin + "/?token=" + "A" * 43, http_transport=httpx.MockTransport(handle),
    )
    assert "A" * 43 not in caplog.text
    assert conn.cookie not in caplog.text


@pytest.mark.asyncio
async def test_rpc_named_arguments_and_exact_identity():
    conn = connection()
    async def handle(request):
        assert request.url.path == "/api/session/page"
        assert request.headers["cookie"] == conn.cookie
        assert request.headers["origin"] == conn.origin
        body = json.loads(request.content)
        assert body["method"] == "session/page"
        assert body["payload"] == {"args": {"request": {"throughSeq": 3}}}
        return httpx.Response(200, json={
            "type": "server-response", "rpcId": body["rpcId"],
            "result": {"ok": True, "value": {"records": [], "hasMore": False}},
        })
    client = DshClient(conn, http_transport=httpx.MockTransport(handle))
    try:
        assert await client.rpc("session/page", {"request": {"throughSeq": 3}}) == {
            "records": [], "hasMore": False,
        }
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_session_list_preserves_upstream_parameter_name():
    async def handle(request):
        body = json.loads(request.content)
        assert body["method"] == "session/list"
        assert body["payload"] == {"args": {"_request": {}}}
        return httpx.Response(200, json={
            "type": "server-response", "rpcId": body["rpcId"],
            "result": {"ok": True, "value": {
                "items": [{"sessionId": "s-1", "running": False, "blank": False, "updatedAt": 1}],
            }},
        })
    client = DshClient(connection(), http_transport=httpx.MockTransport(handle))
    try:
        assert (await client.list_sessions())[0]["sessionId"] == "s-1"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ambiguous_mutation_is_not_retried():
    calls = 0
    async def handle(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("private request state", request=request)
    client = DshClient(connection(), http_transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(DshError) as error:
            await client.rpc("session/prompt", {"request": {"requestId": "p-1"}})
        assert error.value.outcome_unknown
        assert "private" not in str(error.value)
        assert calls == 1
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "code"), [
    (301, "unavailable"), (302, "unavailable"),
    (401, "auth_required"), (403, "auth_required"),
    (404, "gateway/not-found"), (500, "unavailable"),
])
async def test_rpc_rejection_does_not_follow_or_retry(status, code):
    count = 0
    async def handle(_request):
        nonlocal count
        count += 1
        return httpx.Response(status, headers={"Location": "http://evil.invalid/"})
    client = DshClient(connection(), http_transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(DshError) as error:
            await client.rpc("session/prompt", {"request": {"content": "private"}})
        assert error.value.code == code
        assert count == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wrong_rpc_id_and_remote_error_do_not_leak_payload():
    async def wrong_id(_request):
        return httpx.Response(200, json={
            "type": "server-response", "rpcId": "someone-else",
            "result": {"ok": True, "value": "secret"},
        })
    client = DshClient(connection(), http_transport=httpx.MockTransport(wrong_id))
    with pytest.raises(DshError) as error:
        await client.rpc("session/list", {"request": {}})
    assert error.value.outcome_unknown
    await client.close()
    async def failure(request):
        return httpx.Response(200, json={
            "type": "server-response", "rpcId": json.loads(request.content)["rpcId"],
            "result": {"ok": False, "error": {
                "code": "session/not-found", "message": "secret-token", "details": {"key": "secret"},
            }},
        })
    client = DshClient(connection(), http_transport=httpx.MockTransport(failure))
    with pytest.raises(DshError, match="会话不存在") as error:
        await client.rpc("session/list", {"request": {}})
    assert "secret" not in str(error.value)
    await client.close()


@pytest.mark.asyncio
async def test_mux_routes_independent_streams_and_cancel_is_only_subscription():
    seen = []
    cancelled = asyncio.Event()
    async def handler(socket):
        assert socket.request.path == "/api/remote.mux"
        async for text in socket:
            frame = json.loads(text)
            seen.append(frame)
            if frame["type"] == "cancel":
                cancelled.set()
                continue
            assert frame["payload"] == {"args": {}}
            await socket.send(json.dumps({
                "type": "item", "streamId": frame["streamId"],
                "value": {"endpoint": frame["endpoint"]},
            }))
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = DshClient(connection(f"http://127.0.0.1:{port}"))
        events = client.stream("$events")
        control = client.stream("session/control")
        try:
            assert await anext(events) == {"endpoint": "$events"}
            assert await anext(control) == {"endpoint": "session/control"}
            await events.aclose()
            await asyncio.wait_for(cancelled.wait(), 1)
            assert len([row for row in seen if row["type"] == "open"]) == 2
            assert not any(row.get("endpoint") == "session/cancel" for row in seen)
        finally:
            await control.aclose()
            await client.close()


@pytest.mark.asyncio
async def test_disconnect_fails_pending_streams_promptly():
    async def handler(socket):
        await socket.recv()
        await socket.close()
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = DshClient(connection(f"http://127.0.0.1:{port}"))
        stream = client.stream("$events")
        try:
            with pytest.raises(DshError, match="中断"):
                await asyncio.wait_for(anext(stream), 1)
        finally:
            await stream.aclose()
            await client.close()


@pytest.mark.asyncio
async def test_history_uses_only_read_only_route_and_preserves_cut():
    calls = []
    async def handle(request):
        calls.append(request.url.path)
        assert request.method == "GET"
        assert dict(request.url.params) == {
            "sessionId": "s-1", "maxMessages": "2", "beforeSeq": "5", "throughSeq": "9",
        }
        return httpx.Response(200, json={
            "contract": 1, "header": {"id": "s-1", "version": 3}, "cursor": 9,
            "records": [{"type": "event", "event": {"seq": seq}} for seq in (2, 3, 4)],
            "hasMore": True,
        })
    client = DshClient(connection(), http_transport=httpx.MockTransport(handle))
    try:
        page = await client.history_snapshot("dsh@s-1", before_seq=5, through_seq=9, max_messages=2)
        assert page["cursor"] == 9
        assert calls == ["/api/cc-remote.snapshot"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_missing_history_bridge_never_falls_back_to_follow():
    calls = []
    async def handle(request):
        calls.append(request.url.path)
        return httpx.Response(404)
    client = DshClient(connection(), http_transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(DshError) as error:
            await client.history_snapshot("dsh@s-1")
        assert error.value.code == "history_bridge_required"
        assert calls == ["/api/cc-remote.snapshot"]
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("update", [
    {"header": {"id": "other", "version": 3}}, {"header": {"id": "s-1", "version": 2}},
    {"cursor": True}, {"contract": 0}, {"records": [
        {"type": "event", "event": {"seq": 1}}, {"type": "event", "event": {"seq": 3}},
    ]},
])
async def test_history_rejects_mixed_session_format_or_event_gaps(update):
    async def handle(_request):
        return httpx.Response(200, json={
            "contract": 1, "header": {"id": "s-1", "version": 3}, "cursor": 4,
            "records": [], "hasMore": False, **update,
        })
    client = DshClient(connection(), http_transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(DshError) as error:
            await client.history_snapshot("dsh@s-1")
        assert error.value.code == "invalid_history"
    finally:
        await client.close()


def test_connection_file_fifo_is_rejected_without_blocking(tmp_path):
    path = tmp_path / "fifo"
    os.mkfifo(path, 0o600)
    with pytest.raises(DshError):
        DshConnection.load(path)
