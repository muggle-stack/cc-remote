"""Zero-model tests for persistent multi-device enrollment."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from argparse import Namespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from cc_remote.config import RelayConfig, WrapperConfig
from cc_remote import device as device_cli
from cc_remote.protocol import Hello, deserialize, serialize
from cc_remote.relay.auth import SESSION_COOKIE_NAME, session_token_claims
from cc_remote.relay import server
from cc_remote.relay.devices import DeviceStore
from cc_remote.relay.server import create_app


def _cfg(tmp_path, **overrides) -> RelayConfig:
    values = {
        "login_password": "correct horse battery staple",
        "session_secret": "s" * 48,
        "wrapper_token": "w" * 48,
        "public_origin": "https://remote.example",
        "session_ttl_seconds": 3600,
        "device_db_path": str(tmp_path / "devices.sqlite3"),
    }
    values.update(overrides)
    return RelayConfig(**values)


def _wait_for_device_online(
    client: TestClient,
    machine_id: str,
    *,
    timeout: float = 1.0,
) -> None:
    """Wait for the server task to consume the wrapper hello.

    ``send_text`` only hands the frame to TestClient's in-memory transport; it
    does not acknowledge that ``serve_wrapper`` has registered the route yet.
    Keep the assertion on the public device-list contract, but make that
    asynchronous boundary explicit and bounded.
    """
    deadline = time.monotonic() + timeout
    while True:
        devices = client.get("/api/devices").json()["devices"]
        device = next(
            item for item in devices if item["machine_id"] == machine_id
        )
        if device["online"]:
            return
        if time.monotonic() >= deadline:
            pytest.fail(
                f"device {machine_id!r} did not become online within {timeout}s"
            )
        time.sleep(0.01)


def test_pairing_code_is_single_use_and_plaintext_credential_is_not_stored(tmp_path):
    path = tmp_path / "devices.sqlite3"
    store = DeviceStore(str(path))

    async def scenario():
        grant = await store.create_pairing("legacy", ttl=600, now=100)
        enrolled = await store.redeem(
            grant.code,
            label="MacBook",
            platform="darwin",
            hostname="macbook.local",
            now=101,
        )
        assert enrolled is not None
        assert await store.redeem(
            grant.code,
            label="replay",
            platform="linux",
            hostname="replay",
            now=102,
        ) is None
        assert await store.machine_for_token(enrolled.token) == enrolled.machine_id
        assert await store.owned_by(enrolled.machine_id, "legacy") is True
        assert await store.revoke(enrolled.machine_id, "legacy") is True
        assert await store.machine_for_token(enrolled.token) is None
        return enrolled.token

    token = asyncio.run(scenario())
    if sys.platform != "win32":
        assert os.stat(path).st_mode & 0o777 == 0o600
    assert token.encode() not in path.read_bytes()


def test_browser_pairs_lists_renames_and_revokes_dynamic_wrapper(tmp_path):
    server._pair_limiter.reset()
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    with TestClient(app, base_url=cfg.public_origin) as client:
        assert client.post(
            "/api/login", json={"password": cfg.login_password}
        ).status_code == 200
        pairing = client.post("/api/devices/pairing")
        assert pairing.status_code == 200
        code = pairing.json()["code"]

        paired = client.post("/api/devices/pair", json={
            "code": code,
            "label": "Linux 工作站",
            "platform": "linux",
            "hostname": "nono",
        })
        assert paired.status_code == 200
        credential = paired.json()
        machine_id = credential["machine_id"]
        assert credential["relay_url"] == "wss://remote.example/ws"

        listing = client.get("/api/devices").json()
        assert listing["devices"] == [{
            "machine_id": machine_id,
            "label": "Linux 工作站",
            "platform": "linux",
            "hostname": "nono",
            "created_at": listing["devices"][0]["created_at"],
            "last_seen": None,
            "online": False,
            "managed": True,
        }]
        assert client.get("/api/machines").json()["machines"] == [machine_id]

        headers = {"authorization": f"Bearer {credential['token']}"}
        with client.websocket_connect("/ws", headers=headers) as wrapper:
            wrapper.send_text(serialize(Hello(
                role="wrapper", machine_id=machine_id,
                wrapper_generation="test-generation", state="idle",
            )))
            _wait_for_device_online(client, machine_id)
            renamed = client.patch(
                f"/api/devices/{machine_id}", json={"label": "nono"})
            assert renamed.status_code == 200
            assert client.get("/api/devices").json()["devices"][0]["label"] == "nono"
            revoked = client.delete(f"/api/devices/{machine_id}")
            assert revoked.status_code == 200

        assert client.get("/api/devices").json()["devices"] == []
        assert client.get("/api/machines").json()["machines"] == []


def test_revoke_during_wrapper_hello_prevents_late_registration(tmp_path):
    server._pair_limiter.reset()
    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg), base_url=cfg.public_origin) as client:
        assert client.post(
            "/api/login", json={"password": cfg.login_password}
        ).status_code == 200
        code = client.post("/api/devices/pairing").json()["code"]
        credential = client.post("/api/devices/pair", json={
            "code": code,
            "label": "Late wrapper",
            "platform": "linux",
            "hostname": "late",
        }).json()
        machine_id = credential["machine_id"]
        headers = {"authorization": f"Bearer {credential['token']}"}
        with client.websocket_connect("/ws", headers=headers) as wrapper:
            # Upgrade has authenticated the token, but the wrapper route does
            # not exist until its hello. Revoking in this gap must still win.
            assert client.delete(f"/api/devices/{machine_id}").status_code == 200
            wrapper.send_text(serialize(Hello(
                role="wrapper", machine_id=machine_id,
                wrapper_generation="late-generation", state="idle",
            )))
            error = deserialize(wrapper.receive_text())
            assert error.type == "error"
            assert error.message == "wrapper credential has been revoked"
            with pytest.raises(WebSocketDisconnect):
                wrapper.receive_text()
        assert client.app.state.hub.machine_ids == []


def test_paired_device_expands_multi_user_machine_authority(tmp_path):
    server._pair_limiter.reset()
    users = {
        "alice": {
            "password": "alice correct horse battery staple",
            "machines": ["office"],
        },
    }
    cfg = _cfg(tmp_path, login_users_json=json.dumps(users))
    with TestClient(create_app(cfg), base_url=cfg.public_origin) as client:
        login = client.post("/api/login", json={
            "username": "alice", "password": users["alice"]["password"],
        })
        assert login.status_code == 200
        code = client.post("/api/devices/pairing").json()["code"]
        paired = client.post("/api/devices/pair", json={
            "code": code,
            "label": "Alice Mac",
            "platform": "darwin",
            "hostname": "alice-mac",
        }).json()
        assert client.get("/api/machines").json()["machines"] == [
            paired["machine_id"], "office",
        ]
        # The signed session still contains the original static policy, but an
        # owner may immediately route to the device they just enrolled.
        claims = session_token_claims(
            client.cookies[SESSION_COOKIE_NAME].strip('"'), cfg.session_secret)
        assert claims is not None
        assert asyncio.run(server._claims_allow_machine(
            claims, paired["machine_id"], client.app.state.device_store)) is True


def test_wrapper_config_reads_private_paired_device_file(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip(
            "Windows os.chmod cannot clear group/other bits, so "
            "_load_device_config's 0o077 check always rejects the file")
    path = tmp_path / "device.json"
    path.write_text(json.dumps({
        "relay_url": "wss://remote.example/ws",
        "wrapper_token": "t" * 64,
        "machine_id": "device-1234",
        "label": "Mac",
    }), encoding="utf-8")
    os.chmod(path, 0o600)
    monkeypatch.setenv("CC_REMOTE_DEVICE_CONFIG", str(path))
    monkeypatch.delenv("RELAY_URL", raising=False)
    monkeypatch.delenv("WRAPPER_TOKEN", raising=False)
    monkeypatch.delenv("CC_REMOTE_MACHINE_ID", raising=False)
    cfg = WrapperConfig()
    assert cfg.relay_url == "wss://remote.example/ws"
    assert cfg.wrapper_token == "t" * 64
    assert cfg.machine_id == "device-1234"


def test_wrapper_config_rejects_group_readable_device_credentials(
    tmp_path, monkeypatch,
):
    path = tmp_path / "device.json"
    path.write_text(json.dumps({
        "relay_url": "wss://remote.example/ws",
        "wrapper_token": "t" * 64,
        "machine_id": "device-1234",
    }), encoding="utf-8")
    os.chmod(path, 0o640)
    monkeypatch.setenv("CC_REMOTE_DEVICE_CONFIG", str(path))
    monkeypatch.delenv("RELAY_URL", raising=False)
    with pytest.raises(ValueError, match="must not be accessible"):
        WrapperConfig()


def test_pairing_cli_writes_private_config_without_printing_token(
    tmp_path, monkeypatch, capsys,
):
    token = "secret-device-token-" + "x" * 48

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "ok": True,
                "relay_url": "wss://remote.example/ws",
                "token": token,
                "machine_id": "device-abcd1234",
                "label": "Test Mac",
            }

    monkeypatch.setattr(device_cli.httpx, "post", lambda *args, **kwargs: Response())
    target = tmp_path / "device.json"
    args = Namespace(
        relay="https://remote.example",
        code="AAAAA-BBBBB-CCCCC-DDDDD",
        name="Test Mac",
        env_file=None,
        config=str(target),
        replace=False,
    )
    assert device_cli.pair(args) == 0
    if sys.platform != "win32":
        assert os.stat(target).st_mode & 0o777 == 0o600
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["wrapper_token"] == token
    assert token not in capsys.readouterr().out
