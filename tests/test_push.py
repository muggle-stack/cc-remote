"""Zero-model tests for durable, privacy-bounded Web Push delivery."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from cc_remote.config import RelayConfig
from cc_remote.protocol import TurnResult
from cc_remote.relay import server
from cc_remote.relay.push import (
    PushDispatcher, PushSubscription, PushSubscriptionStore,
)
from cc_remote.relay.server import _turn_push_outcome, create_app


@pytest.fixture(autouse=True)
def _reset_login_limiter():
    server._login_limiter.reset()
    yield
    server._login_limiter.reset()


def _cfg(tmp_path, **overrides) -> RelayConfig:
    values = {
        "login_password": "correct horse battery staple",
        "session_secret": "s" * 48,
        "wrapper_token": "w" * 48,
        "public_origin": "https://remote.example",
        "push_vapid_public_key": "B" + "a" * 86,
        "push_vapid_private_key": str(tmp_path / "vapid-private.pem"),
        "push_vapid_subject": "mailto:admin@example.com",
        "push_db_path": str(tmp_path / "push.sqlite3"),
        "device_db_path": str(tmp_path / "devices.sqlite3"),
    }
    values.update(overrides)
    return RelayConfig(**values)


def _subscription(
    machine_id: str = "nono",
    notification_mode: str | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "machine_id": machine_id,
        "endpoint": "https://push.example/send/token",
        "keys": {"p256dh": "p" * 87, "auth": "a" * 22},
    }
    if notification_mode is not None:
        value["notification_mode"] = notification_mode
    return value


@pytest.mark.parametrize("engine", ["claude", "codex", "dsh"])
def test_multi_device_push_keeps_background_completion_and_question_routes(tmp_path, engine):
    config = _cfg(tmp_path, login_users_json=json.dumps({
        "alice": {"password": "alice correct horse battery staple", "machines": ["device-a", "device-b"]},
    }))
    app = create_app(config)
    sent = []

    async def send(subscription, payload):
        sent.append((subscription.machine_id, json.loads(payload)))
        return 201

    app.state.push_dispatcher._sender = send
    with TestClient(app, base_url=config.public_origin) as client:
        assert client.post("/api/login", json={
            "username": "alice", "password": "alice correct horse battery staple",
        }).status_code == 200
        body = {**_subscription("device-a", "session"), "machine_ids": ["device-a", "device-b"]}
        assert client.post("/api/push/subscribe", json=body).status_code == 200
        assert client.post("/api/push/subscribe", json={
            **body, "machine_ids": ["device-a", "unauthorized"],
        }).status_code == 400
        for machine in body["machine_ids"]:
            context = {"engine": engine, "space": "code", "sid": "native-session", "display_name": "Task"}
            asyncio.run(app.state.push_dispatcher.notify_turn_end(machine, outcome="success", context=context))
            asyncio.run(app.state.push_dispatcher.notify_question(machine, context=context))
        assert [machine for machine, _ in sent] == ["device-a", "device-a", "device-b", "device-b"]
        assert all(payload["route"]["machine_id"] == machine for machine, payload in sent)
        assert all(payload["route"]["engine"] == engine for _, payload in sent)
        assert client.post("/api/push/unsubscribe", json={"endpoint": body["endpoint"]}).status_code == 200
        assert all(not asyncio.run(app.state.push_store.for_machine(machine)) for machine in body["machine_ids"])
        assert client.post("/api/push/subscribe", json=body).status_code == 200
        assert client.post("/api/logout").status_code == 200
        assert all(not asyncio.run(app.state.push_store.for_machine(machine)) for machine in body["machine_ids"])


@pytest.mark.asyncio
async def test_group_subscription_replacement_is_atomic_and_fences_old_deliveries(tmp_path):
    store = PushSubscriptionStore(str(tmp_path / "push.db"))
    original = PushSubscription("alice", "device-a", "https://push.example/test", "p", "a", "old-login", time.time() + 60)
    await store.upsert(original, ["device-a", "device-b"])
    with sqlite3.connect(store.path) as db:
        db.execute("""CREATE TRIGGER reject_blocked BEFORE INSERT ON push_subscriptions
                      WHEN NEW.machine_id='blocked' BEGIN SELECT RAISE(ABORT, 'injected'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        await store.upsert(replace(original, session_jti="new-login"), ["device-a", "blocked"])
    assert await store.current(original)
    assert len(await store.for_machine("device-b")) == 1
    replacement = replace(original, session_jti="new-login", notification_mode="session")
    await store.upsert(replacement, ["device-a", "device-b"])
    await store.remove_subscription(original)
    assert await store.current(replacement)
    sent = []

    async def send(*args):
        sent.append(args)
        return 201

    dispatcher = PushDispatcher(store, vapid_private_key="unused", vapid_subject="unused", sender=send)
    await dispatcher._deliver(original, "stale payload")
    assert sent == []
    await store.remove_machine("alice", "device-a")
    assert not await store.for_machine("device-a")
    assert len(await store.for_machine("device-b")) == 1


def test_push_subscription_requires_auth_and_machine_scope(tmp_path):
    users = json.dumps({
        "alice": {
            "password": "alice correct horse battery staple",
            "machines": ["nono"],
        },
    })
    cfg = _cfg(tmp_path, login_users_json=users)
    app = create_app(cfg)
    with TestClient(app, base_url=cfg.public_origin) as client:
        assert client.get("/api/push-config").status_code == 401
        assert client.post("/api/login", json={
            "username": "alice",
            "password": "alice correct horse battery staple",
        }).status_code == 200
        config = client.get("/api/push-config")
        assert config.json() == {
            "enabled": True, "public_key": cfg.push_vapid_public_key,
        }
        assert client.post(
            "/api/push/subscribe", json=_subscription("mac"),
        ).status_code == 400
        assert client.post(
            "/api/push/subscribe", json=_subscription(),
        ).json() == {"ok": True}

        stored = asyncio.run(app.state.push_store.for_machine("nono"))
        assert len(stored) == 1
        assert stored[0].subject == "alice"
        assert stored[0].notification_mode == "generic"
        assert stored[0].session_jti
        assert stored[0].expires_at > 0
        assert client.post("/api/push/unsubscribe", json={
            "endpoint": "https://push.example/send/token",
        }).json() == {"ok": True}
        assert asyncio.run(app.state.push_store.for_machine("nono")) == []


def test_push_subscription_modes_are_validated(tmp_path):
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    with TestClient(app, base_url=cfg.public_origin) as client:
        assert client.post("/api/login", json={
            "password": cfg.login_password,
        }).status_code == 200
        for mode in ("generic", "session"):
            payload = _subscription(notification_mode=mode)
            payload["endpoint"] = f"https://push.example/send/{mode}"
            assert client.post(
                "/api/push/subscribe", json=payload,
            ).status_code == 200
        for index, invalid_mode in enumerate(("everything", {"session": True})):
            invalid = _subscription()
            invalid["notification_mode"] = invalid_mode
            invalid["endpoint"] = f"https://push.example/send/invalid-{index}"
            assert client.post(
                "/api/push/subscribe", json=invalid,
            ).status_code == 400


def test_legacy_push_database_migrates_to_generic_mode(tmp_path):
    path = tmp_path / "push.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE push_subscriptions (
                subject TEXT NOT NULL,
                machine_id TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                session_jti TEXT NOT NULL,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (subject, machine_id, endpoint)
            )
            """
        )
        db.execute(
            "INSERT INTO push_subscriptions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "alice", "nono", "https://push.example/send/legacy",
                "p" * 87, "a" * 22, "j" * 24, 4_102_444_800, 1, 1,
            ),
        )

    store = PushSubscriptionStore(str(path))
    subscriptions = asyncio.run(store.for_machine("nono"))
    assert len(subscriptions) == 1
    assert subscriptions[0].notification_mode == "generic"


def test_dispatcher_payload_is_generic_and_prunes_expired_endpoint(tmp_path):
    async def run():
        store = PushSubscriptionStore(str(tmp_path / "push.sqlite3"))
        subscription = PushSubscription(
            subject="alice",
            machine_id="nono",
            endpoint="https://push.example/send/token",
            p256dh="p" * 87,
            auth="a" * 22,
            session_jti="j" * 24,
            expires_at=4_102_444_800,
        )
        await store.upsert(subscription)
        payloads: list[str] = []

        async def sender(_subscription, payload):
            payloads.append(payload)
            return 410

        dispatcher = PushDispatcher(
            store,
            vapid_private_key="unused",
            vapid_subject="mailto:admin@example.com",
            sender=sender,
        )
        await dispatcher.notify_turn_end("nono", outcome="success")
        assert len(payloads) == 1
        payload = json.loads(payloads[0])
        assert payload["body"] == "远程会话已经完成"
        assert "session" not in payloads[0].lower()
        assert "nono" not in payloads[0]
        assert await store.for_machine("nono") == []

    asyncio.run(run())


def test_dispatcher_distinguishes_terminal_outcomes_without_tag_collisions(tmp_path):
    async def run():
        store = PushSubscriptionStore(str(tmp_path / "push.sqlite3"))
        await store.upsert(PushSubscription(
            subject="alice",
            machine_id="nono",
            endpoint="https://push.example/send/token",
            p256dh="p" * 87,
            auth="a" * 22,
            session_jti="j" * 24,
            expires_at=4_102_444_800,
        ))
        payloads: list[dict[str, object]] = []

        async def sender(_subscription, payload):
            payloads.append(json.loads(payload))
            return 201

        dispatcher = PushDispatcher(
            store,
            vapid_private_key="unused",
            vapid_subject="mailto:admin@example.com",
            sender=sender,
        )
        for outcome in ("success", "interrupted", "failed"):
            await dispatcher.notify_turn_end("nono", outcome=outcome)

        assert [payload["body"] for payload in payloads] == [
            "远程会话已经完成",
            "远程会话已中断",
            "远程会话执行失败",
        ]
        tags = [str(payload["tag"]) for payload in payloads]
        assert all(tag.startswith("cc-remote-turn-") for tag in tags)
        assert len(set(tags)) == len(tags)
        serialized = json.dumps(payloads, ensure_ascii=False).lower()
        assert "session" not in serialized
        assert "nono" not in serialized

    asyncio.run(run())


def test_dispatcher_separates_generic_and_session_payloads(tmp_path):
    async def run():
        store = PushSubscriptionStore(str(tmp_path / "push.sqlite3"))
        for mode in ("generic", "session"):
            await store.upsert(PushSubscription(
                subject="alice",
                machine_id="nono",
                endpoint=f"https://push.example/send/{mode}",
                p256dh="p" * 87,
                auth="a" * 22,
                session_jti=f"{mode}-" + "j" * 24,
                expires_at=4_102_444_800,
                notification_mode=mode,
            ))
        payloads: dict[str, dict[str, object]] = {}

        async def sender(subscription, payload):
            payloads[subscription.notification_mode] = json.loads(payload)
            return 201

        dispatcher = PushDispatcher(
            store,
            vapid_private_key="unused",
            vapid_subject="mailto:admin@example.com",
            sender=sender,
        )
        await dispatcher.notify_turn_end(
            "nono",
            outcome="success",
            context={
                "sid": "session-1",
                "engine": "codex",
                "space": "work",
                "display_name": "  Release\ncheck\u0000  ",
            },
        )

        generic = payloads["generic"]
        serialized_generic = json.dumps(generic, ensure_ascii=False)
        assert generic["body"] == "远程会话已经完成"
        assert "route" not in generic
        assert "session-1" not in serialized_generic
        assert "nono" not in serialized_generic
        assert "Release" not in serialized_generic

        session = payloads["session"]
        assert session["title"] == "Release check"
        assert session["route"] == {
            "machine_id": "nono",
            "session_id": "session-1",
            "engine": "codex",
            "space": "work",
        }
        assert session["url"].startswith("/#notification=")

    asyncio.run(run())


def test_session_mode_without_context_falls_back_to_generic_payload(tmp_path):
    async def run():
        store = PushSubscriptionStore(str(tmp_path / "push.sqlite3"))
        await store.upsert(PushSubscription(
            subject="alice",
            machine_id="nono",
            endpoint="https://push.example/send/session",
            p256dh="p" * 87,
            auth="a" * 22,
            session_jti="j" * 24,
            expires_at=4_102_444_800,
            notification_mode="session",
        ))
        payloads: list[dict[str, object]] = []

        async def sender(_subscription, payload):
            payloads.append(json.loads(payload))
            return 201

        dispatcher = PushDispatcher(
            store,
            vapid_private_key="unused",
            vapid_subject="mailto:admin@example.com",
            sender=sender,
        )
        await dispatcher.notify_turn_end(
            "nono", outcome="failed", context=None)
        assert payloads == [{
            "title": "cc-remote",
            "body": "远程会话执行失败",
            "tag": payloads[0]["tag"],
            "url": "/",
        }]

    asyncio.run(run())


def test_turn_push_outcome_preserves_interruptions():
    assert _turn_push_outcome(TurnResult(
        subtype="success", duration_ms=1, is_error=False)) == "success"
    assert _turn_push_outcome(TurnResult(
        subtype="error", duration_ms=1, is_error=True)) == "failed"
    assert _turn_push_outcome(TurnResult(
        subtype="error_during_execution", duration_ms=1,
        is_error=True)) == "interrupted"


def test_logout_removes_only_the_current_session_push_subscription(tmp_path):
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    with TestClient(app, base_url=cfg.public_origin) as client:
        assert client.post("/api/login", json={
            "password": cfg.login_password,
        }).status_code == 200
        assert client.post(
            "/api/push/subscribe", json=_subscription(),
        ).status_code == 200
        assert len(asyncio.run(
            app.state.push_store.for_machine("nono"))) == 1

        assert client.post("/api/logout").status_code == 200
        assert asyncio.run(
            app.state.push_store.for_machine("nono")) == []


def test_expired_push_subscription_is_not_delivered(tmp_path):
    async def run():
        store = PushSubscriptionStore(str(tmp_path / "push.sqlite3"))
        await store.upsert(PushSubscription(
            subject="alice",
            machine_id="nono",
            endpoint="https://push.example/send/expired",
            p256dh="p" * 87,
            auth="a" * 22,
            session_jti="j" * 24,
            expires_at=1,
        ))
        assert await store.for_machine("nono") == []

    asyncio.run(run())


def test_inactive_login_subscription_is_pruned_before_delivery(tmp_path):
    async def run():
        store = PushSubscriptionStore(str(tmp_path / "push.sqlite3"))
        subscription = PushSubscription(
            subject="alice",
            machine_id="nono",
            endpoint="https://push.example/send/inactive",
            p256dh="p" * 87,
            auth="a" * 22,
            session_jti="j" * 24,
            expires_at=4_102_444_800,
        )
        await store.upsert(subscription)
        deliveries = 0

        async def sender(_subscription, _payload):
            nonlocal deliveries
            deliveries += 1
            return 201

        dispatcher = PushDispatcher(
            store,
            vapid_private_key="unused",
            vapid_subject="mailto:admin@example.com",
            sender=sender,
            session_active=lambda _subscription: asyncio.sleep(
                0, result=False),
        )
        await dispatcher.notify_turn_end("nono", outcome="success")
        assert deliveries == 0
        assert await store.for_machine("nono") == []

    asyncio.run(run())


def test_question_push_privacy_and_machine_scope(tmp_path):
    async def run():
        store = PushSubscriptionStore(str(tmp_path / "push.sqlite3"))
        for device, mode in (("device", "generic"), ("device", "session"), ("other", "session")):
            await store.upsert(PushSubscription(
                subject="alice", machine_id=device,
                endpoint=f"https://push.example/{device}/{mode}", p256dh="p" * 87, auth="a" * 22,
                session_jti=f"{device}-{mode}-" + "j" * 24, expires_at=4_102_444_800,
                notification_mode=mode))
        sent = {}

        async def sender(subscription, payload):
            assert subscription.machine_id == "device"
            sent[subscription.notification_mode] = json.loads(payload)
            return 201

        dispatcher = PushDispatcher(store, vapid_private_key="unused",
                                    vapid_subject="mailto:admin@example.com", sender=sender)
        await dispatcher.notify_question("device", context={
            "sid": "session-id", "engine": "claude", "space": "code",
            "display_name": "Session name", "question": "do not expose"})
        assert sent["generic"]["body"] == "远程会话正在等待你的回答"
        assert "route" not in sent["generic"]
        assert "session-id" not in json.dumps(sent["generic"])
        assert sent["session"]["body"] == "Claude 正在等待你的回答"
        assert sent["session"]["route"]["session_id"] == "session-id"
        assert "do not expose" not in json.dumps(sent)

    asyncio.run(run())


@pytest.mark.parametrize("notification_mode", ["generic", "session"])
def test_live_async_question_reaches_push_with_private_content_omitted(tmp_path, notification_mode):
    from cc_remote.protocol import AssistantMsgEnd, TurnNotificationContext, serialize

    config = _cfg(tmp_path)
    app = create_app(config)
    sent = []

    async def send(subscription, payload):
        sent.append(json.loads(payload))
        return 201

    app.state.push_dispatcher._sender = send
    with TestClient(app, base_url=config.public_origin) as client:
        assert client.post("/api/login", json={"password": config.login_password}).status_code == 200
        assert client.post("/api/push/subscribe", json=_subscription(
            "default", notification_mode)).status_code == 200
        with client.websocket_connect("/ws", headers={
            "Authorization": f"Bearer {config.wrapper_token}",
        }) as wrapper:
            from cc_remote.protocol import Hello
            wrapper.send_text(serialize(Hello(role="wrapper", machine_id="default")))
            wrapper.send_text(serialize(AssistantMsgEnd(
                sid="async-session", message_id="question-item", delivery="async",
                questions=[{"title": "private question", "options": ["private option"]}],
                notification_context=TurnNotificationContext(
                    engine="codex", space="code", display_name="Code session"),
            )))
            for _ in range(100):
                if sent:
                    break
                time.sleep(0.01)
    assert len(sent) == 1
    payload = sent[0]
    assert "private" not in json.dumps(payload)
    if notification_mode == "generic":
        assert payload["title"] == "Codex 想确认一下"
        assert payload["body"] == "远程会话有一个问题待回答"
        assert "route" not in payload
        assert "async-session" not in json.dumps(payload)
    else:
        assert payload["title"] == "Code session"
        assert payload["body"] == "Codex 想确认一下"
        assert payload["route"] == {
            "machine_id": "default", "session_id": "async-session", "engine": "codex", "space": "code"}
