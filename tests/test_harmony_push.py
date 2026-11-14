"""Harmony Push isolation, credential signing and privacy regression tests."""
from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import time
from dataclasses import replace

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from cc_remote.config import RelayConfig
from cc_remote.relay import server
from cc_remote.relay.harmony_push import (
    AUDIENCE, HarmonyPushDispatcher, HarmonyPushStore, HarmonySubscription, HuaweiPushSender,
)


@pytest.fixture
def credentials(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "service-account.json"
    path.write_text(json.dumps({
        "project_id": "project-123", "key_id": "key-123", "sub_account": "service@example",
        "private_key": key.private_bytes(serialization.Encoding.PEM,
                                         serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()).decode(),
    }))
    return path, key


@pytest.fixture(autouse=True)
def reset_login():
    server._login_limiter.reset()
    yield
    server._login_limiter.reset()


def cfg(tmp_path, credentials=None, **values):
    return RelayConfig(login_password="correct horse battery staple",
                       session_secret="s" * 48, wrapper_token="w" * 48,
                       public_origin="https://remote.example",
                       push_db_path=str(tmp_path / "push.sqlite3"),
                       device_db_path=str(tmp_path / "devices.sqlite3"),
                       harmony_push_service_account=str(credentials[0]) if credentials else "",
                       **values)


def payload(machine="device-a", **values):
    return {"machine_id": machine, "token": "private-token" * 10, **values}


def login(client, config):
    assert client.post("/api/login", json={"password": config.login_password}).status_code == 200


def test_disabled_channel_does_not_change_web_config(tmp_path):
    config = cfg(tmp_path)
    app = server.create_app(config)
    with TestClient(app, base_url=config.public_origin) as client:
        assert client.get("/api/push/harmony/config").status_code == 401
        login(client, config)
        assert client.get("/api/push-config").json() == {"enabled": False, "public_key": ""}
        assert client.get("/api/push/harmony/config").json() == {"enabled": False}
        assert client.post("/api/push/harmony/subscribe", json=payload()).status_code == 503


def test_native_binding_auth_origin_scope_and_logout(tmp_path, credentials):
    config = cfg(tmp_path, credentials, login_users_json=json.dumps({
        "alice": {"password": "alice correct horse battery staple", "machines": ["device-a"]},
    }))
    app = server.create_app(config)
    with TestClient(app, base_url=config.public_origin) as client:
        assert client.post("/api/push/harmony/subscribe", json=payload()).status_code == 401
        assert client.post("/api/login", json={"username": "alice",
                           "password": "alice correct horse battery staple"}).status_code == 200
        assert client.get("/api/push/harmony/config").json() == {"enabled": True}
        assert client.post("/api/push/harmony/subscribe", json=payload(),
                           headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post("/api/push/harmony/subscribe", json=payload("device-b")).status_code == 400
        for invalid in ({"token": "short"}, {"token": "x" * 4097},
                        {"notification_mode": {"session": True}}, {"notification_mode": "all"}):
            assert client.post("/api/push/harmony/subscribe", json=payload(**invalid)).status_code == 400
        assert client.post("/api/push/harmony/subscribe", content="x" * 20000).status_code == 413
        assert client.post("/api/push/harmony/subscribe", json=payload()).status_code == 200
        rows = asyncio.run(app.state.harmony_push_store.for_machine("device-a"))
        assert len(rows) == 1 and rows[0].subject == "alice"
        assert rows[0].notification_mode == "generic"
        assert "private-token" not in repr(rows[0])
        assert client.post("/api/logout").status_code == 200
        assert asyncio.run(app.state.harmony_push_store.for_machine("device-a")) == []


def test_bad_credentials_disable_native_only(tmp_path):
    config = cfg(tmp_path)
    config.harmony_push_service_account = str(tmp_path / "missing.json")
    config.push_vapid_public_key = "B" + "a" * 86
    config.push_vapid_private_key = "not-read-until-send"
    config.push_vapid_subject = "mailto:admin@example.com"
    app = server.create_app(config)
    assert app.state.push_dispatcher is not None
    assert app.state.harmony_push_dispatcher is None


def test_native_store_failure_preserves_web_push(tmp_path, credentials, monkeypatch):
    config = cfg(tmp_path, credentials)
    config.push_vapid_public_key = "B" + "a" * 86
    config.push_vapid_private_key = "not-read-until-send"
    config.push_vapid_subject = "mailto:admin@example.com"

    def unavailable(_):
        raise sqlite3.OperationalError("native table unavailable")

    monkeypatch.setattr(server, "HarmonyPushStore", unavailable)
    app = server.create_app(config)
    assert app.state.push_dispatcher is not None
    assert app.state.harmony_push_dispatcher is None
    assert app.state.harmony_push_store is None


def test_ps256_claims_and_signature(credentials):
    sender = HuaweiPushSender(str(credentials[0]))
    token = sender.authorization()
    assert sender.authorization() == token
    header, payload_part, signature = token.split(".")
    def decode(value):
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    assert json.loads(decode(header)) == {"kid": "key-123", "typ": "JWT", "alg": "PS256"}
    claims = json.loads(decode(payload_part))
    assert claims["iss"] == "service@example" and claims["aud"] == AUDIENCE
    assert claims["exp"] - claims["iat"] == 3600
    credentials[1].public_key().verify(decode(signature), f"{header}.{payload_part}".encode(),
                                     padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                                     hashes.SHA256())


@pytest.mark.asyncio
async def test_http_200_does_not_mask_provider_rejection(credentials, monkeypatch):
    sender = HuaweiPushSender(str(credentials[0]))
    original = httpx.AsyncClient
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"code": "80300007", "msg": "private provider data"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(respond), **kwargs))
    assert await sender({"target": {"token": ["test"]}}) == "80300007"
    assert str(seen[0].url) == "https://push-api.cloud.huawei.com/v3/project-123/messages:send"
    assert seen[0].headers["push-type"] == "0"
    assert seen[0].headers["authorization"].startswith("Bearer ")


def sub(**values):
    return HarmonySubscription(**{
        "subject": "alice", "machine_id": "device-a", "token": "private-token" * 10,
        "session_jti": "login-a", "expires_at": time.time() + 3600, **values,
    })


@pytest.mark.asyncio
async def test_store_transfer_expiry_and_versioned_removal(tmp_path):
    store = HarmonyPushStore(str(tmp_path / "push.db"))
    first = sub()
    await store.upsert(first)
    second = replace(first, machine_id="device-b", subject="bob", binding_id="new")
    await store.upsert(second)
    assert not await store.current(first)
    await store.remove_binding(first)  # stale delivery cannot erase the replacement
    await store.remove_token("alice", first.token)  # another subject cannot unsubscribe it
    assert await store.current(second)
    assert not await store.for_machine("device-a")
    expired = sub(token="expired", expires_at=time.time() - 1)
    await store.upsert(expired)
    assert not await store.for_machine("device-a")
    await store.remove_session(second.session_jti)
    assert not await store.for_machine("device-b")


@pytest.mark.parametrize("engine", ["claude", "codex", "dsh"])
def test_native_group_binding_checks_each_device_and_clears_all_on_logout(tmp_path, credentials, engine):
    from urllib.parse import parse_qs, urlsplit

    config = cfg(tmp_path, credentials, login_users_json=json.dumps({
        "alice": {"password": "alice correct horse battery staple", "machines": ["device-a", "device-b"]},
    }))
    app = server.create_app(config)
    sent = []

    async def send(message):
        sent.append(message)
        return "80000000"

    app.state.harmony_push_dispatcher.sender = send
    with TestClient(app, base_url=config.public_origin) as client:
        assert client.post("/api/login", json={
            "username": "alice", "password": "alice correct horse battery staple",
        }).status_code == 200
        body = {**payload(notification_mode="session"), "machine_ids": ["device-a", "device-b"]}
        assert client.post("/api/push/harmony/subscribe", json=body).status_code == 200
        for invalid in [[], ["device-a", "denied"], ["device-b"], ["device-a", 7], ["device-a"] * 129]:
            assert client.post("/api/push/harmony/subscribe", json={**body, "machine_ids": invalid}).status_code == 400
        assert all(len(asyncio.run(app.state.harmony_push_store.for_machine(machine))) == 1
                   for machine in body["machine_ids"])
        for machine in body["machine_ids"]:
            context = {"engine": engine, "space": "code", "sid": "native-session", "display_name": "Task"}
            asyncio.run(app.state.harmony_push_dispatcher.notify_turn_end(machine, outcome="success", context=context))
            asyncio.run(app.state.harmony_push_dispatcher.notify_question(machine, context=context))
        routes = []
        for message in sent:
            uri = message["payload"]["notification"]["clickAction"]["uri"]
            routes.append(json.loads(parse_qs(urlsplit(uri).query)["route"][0]))
        assert [route["machine_id"] for route in routes] == ["device-a", "device-a", "device-b", "device-b"]
        assert all(route["engine"] == engine for route in routes)
        assert client.post("/api/logout").status_code == 200
        assert all(not asyncio.run(app.state.harmony_push_store.for_machine(machine)) for machine in body["machine_ids"])


def test_device_revocation_removes_only_that_devices_push_bindings(tmp_path, credentials):
    config = cfg(tmp_path, credentials, login_users_json=json.dumps({
        "alice": {"password": "alice correct horse battery staple", "machines": ["fixed-device"]},
    }), push_vapid_public_key="B" + "a" * 86, push_vapid_private_key="unused", push_vapid_subject="mailto:test@example.com")
    app = server.create_app(config)
    grant = asyncio.run(app.state.device_store.create_pairing("alice"))
    enrolled = asyncio.run(app.state.device_store.redeem(grant.code, label="Temporary device", platform="test", hostname="test"))
    assert enrolled is not None
    machine_ids = ["fixed-device", enrolled.machine_id]
    with TestClient(app, base_url=config.public_origin) as client:
        assert client.post("/api/login", json={
            "username": "alice", "password": "alice correct horse battery staple",
        }).status_code == 200
        web = {"machine_id": "fixed-device", "machine_ids": machine_ids,
               "endpoint": "https://push.example/test", "keys": {"p256dh": "p" * 87, "auth": "a" * 22}}
        native = {**payload("fixed-device"), "machine_ids": machine_ids}
        assert client.post("/api/push/subscribe", json=web).status_code == 200
        assert client.post("/api/push/harmony/subscribe", json=native).status_code == 200
        assert client.delete(f"/api/devices/{enrolled.machine_id}").status_code == 200
        assert client.post("/api/push/subscribe", json=web).status_code == 400
        assert client.post("/api/push/harmony/subscribe", json=native).status_code == 400
        for store in [app.state.push_store, app.state.harmony_push_store]:
            assert not asyncio.run(store.for_machine(enrolled.machine_id))
            assert len(asyncio.run(store.for_machine("fixed-device"))) == 1


@pytest.mark.asyncio
async def test_native_multi_device_migration_and_failed_replacement_preserve_bindings(tmp_path):
    path = tmp_path / "push.db"
    original = sub()
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE harmony_push_subscriptions (
            token TEXT PRIMARY KEY, subject TEXT NOT NULL, machine_id TEXT NOT NULL,
            session_jti TEXT NOT NULL, expires_at REAL NOT NULL, notification_mode TEXT NOT NULL,
            binding_id TEXT NOT NULL)""")
        db.execute("INSERT INTO harmony_push_subscriptions VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (original.token, original.subject, original.machine_id, original.session_jti,
                    original.expires_at, original.notification_mode, original.binding_id))
    store = HarmonyPushStore(str(path))
    assert await store.current(original)
    replacement = replace(original, binding_id="group")
    await store.upsert(replacement, ["device-a", "device-b"])
    store = HarmonyPushStore(str(path))
    assert await store.current(replacement)
    assert await store.current(replace(replacement, machine_id="device-b"))
    await store.remove_binding(original)
    assert await store.current(replacement)
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TRIGGER reject_blocked BEFORE INSERT ON harmony_push_subscriptions
                      WHEN NEW.machine_id='blocked' BEGIN SELECT RAISE(ABORT, 'injected'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        await store.upsert(replace(replacement, binding_id="failed"), ["device-a", "blocked"])
    assert await store.current(replacement)
    assert len(await store.for_machine("device-b")) == 1
    await store.remove_machine("alice", "device-a")
    assert not await store.for_machine("device-a")
    assert len(await store.for_machine("device-b")) == 1


@pytest.mark.asyncio
async def test_generic_privacy_session_routes_and_logout_fence(tmp_path):
    store = HarmonyPushStore(str(tmp_path / "push.db"))
    generic = sub()
    named = sub(token="session-token", notification_mode="session")
    await store.upsert(generic)
    await store.upsert(named)
    sent = []
    active = True

    async def send(message):
        sent.append(message)
        return "80000000"

    async def session_active(_):
        return active

    dispatcher = HarmonyPushDispatcher(store, origin="https://remote.example", sender=send,
                                       session_active=session_active)
    await dispatcher.notify_turn_end("device-a", outcome="success", context={
        "engine": "claude", "space": "code", "sid": "child", "parent_session_id": "parent",
        "display_name": "secret session", "prompt": "never send me",
    })
    assert len(sent) == 2
    first = next(p for p in sent if p["target"]["token"] == [generic.token])
    body = json.dumps(first)
    for secret in ("device-a", "child", "parent", "secret session", "never send me", "remote.example"):
        assert secret not in body
    assert first["payload"]["notification"]["clickAction"] == {"actionType": 0}
    second = next(p for p in sent if p["target"]["token"] == [named.token])
    notification = second["payload"]["notification"]
    assert notification["title"] == "secret session"
    assert "parent" in notification["clickAction"]["uri"]
    assert "child" not in notification["clickAction"]["uri"]
    assert second["pushOptions"] == {"testMessage": False, "ttl": 300}
    active = False
    await dispatcher.notify_turn_end("device-a", outcome="failed")
    assert len(sent) == 2 and not await store.for_machine("device-a")


@pytest.mark.asyncio
async def test_invalid_token_response_cannot_remove_rebound_mode(tmp_path):
    store = HarmonyPushStore(str(tmp_path / "push.db"))
    original = sub()
    await store.upsert(original)
    replacement = replace(original, notification_mode="session", binding_id="replacement")

    async def send(_):
        await store.upsert(replacement)
        return "80300007"

    async def active(_):
        return True

    dispatcher = HarmonyPushDispatcher(store, origin="https://remote.example", sender=send,
                                       session_active=active)
    await dispatcher.notify_turn_end("device-a", outcome="success")
    assert await store.current(replacement)


@pytest.mark.asyncio
async def test_question_push_uses_native_notification_route(tmp_path):
    store = HarmonyPushStore(str(tmp_path / "push.db"))
    await store.upsert(sub(notification_mode="session"))
    sent = []

    async def send(message):
        sent.append(message)
        return "80000000"

    async def active(_):
        return True

    dispatcher = HarmonyPushDispatcher(store, origin="https://remote.example", sender=send,
                                       session_active=active)
    await dispatcher.notify_question("device-a", context={
        "engine": "claude", "space": "code", "sid": "question-session",
        "display_name": "Question session", "question": "private contents"})
    assert len(sent) == 1
    notice = sent[0]["payload"]["notification"]
    assert notice["body"] == "Claude 正在等待你的回答"
    assert notice["foregroundShow"] is True
    assert "question-session" in notice["clickAction"]["uri"]
    assert "private contents" not in json.dumps(sent)


@pytest.mark.asyncio
async def test_async_question_push_preserves_harmony_route_and_codex_title(tmp_path):
    store = HarmonyPushStore(str(tmp_path / "push.db"))
    await store.upsert(sub(notification_mode="session"))
    sent = []

    async def send(message):
        sent.append(message)
        return "80000000"

    async def active(_):
        return True

    dispatcher = HarmonyPushDispatcher(store, origin="https://remote.example", sender=send,
                                       session_active=active)
    await dispatcher.notify_question("device-a", context={
        "engine": "codex", "space": "code", "sid": "async-session",
        "async_question": True, "question": "private contents"})
    notice = sent[0]["payload"]["notification"]
    assert notice["body"] == "Codex 想确认一下"
    assert notice["foregroundShow"] is True
    assert "async-session" in notice["clickAction"]["uri"]
    assert "private contents" not in json.dumps(sent)
