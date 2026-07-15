"""Zero-model regression tests for relay authentication and secret handling."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from cc_remote.config import RelayConfig, validate_relay_config
from cc_remote.log import JsonFormatter
from cc_remote.protocol import (
    PROTOCOL_VERSION, Hello, ProtocolError, deserialize, serialize,
)
from cc_remote.relay import server
from cc_remote.relay.auth import (
    SESSION_COOKIE_NAME,
    authenticate,
    make_session_token,
    session_token_claims,
    session_token_expiry,
    verify_session_token,
)
from cc_remote.relay.log_safety import (
    SensitiveLogFilter, redact_log_text, uvicorn_log_config,
)
from cc_remote.relay.server import create_app


def _cfg(**overrides) -> RelayConfig:
    values = {
        "login_password": "correct horse battery staple",
        "session_secret": "s" * 48,
        "wrapper_token": "w" * 48,
        "public_origin": "https://remote.example",
        "session_ttl_seconds": 3600,
    }
    values.update(overrides)
    return RelayConfig(**values)


@pytest.fixture(autouse=True)
def _clear_login_rate_limit():
    server._login_limiter.reset()
    yield
    server._login_limiter.reset()


def _login(client: TestClient, cfg: RelayConfig):
    response = client.post("/api/login", json={"password": cfg.login_password})
    assert response.status_code == 200
    return response


def _cookie_header(response) -> str:
    token = response.cookies.get(SESSION_COOKIE_NAME)
    assert token
    return f"{SESSION_COOKIE_NAME}={token}"


async def _asgi_login(app, body: bytes = b'{}') -> tuple[int, bytes, list[tuple[bytes, bytes]]]:
    """Issue one login request without a socket (supports concurrency tests)."""
    sent = []
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/login",
            "raw_path": b"/api/login",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("198.51.100.10", 12345),
            "server": ("remote.example", 443),
        },
        receive,
        send,
    )
    start = next(message for message in sent if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return start["status"], response_body, start["headers"]


def test_legacy_client_bearer_is_rejected_but_wrapper_bearer_still_works():
    cfg = _cfg()
    assert authenticate("Bearer change-me-client", cfg) is None
    assert authenticate(f"Bearer {cfg.wrapper_token}", cfg) == "wrapper"


def test_login_ip_trusts_forwarding_header_only_from_loopback_proxy():
    proxied = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"x-forwarded-for": "203.0.113.7, 127.0.0.1"},
    )
    direct = SimpleNamespace(
        client=SimpleNamespace(host="198.51.100.9"),
        headers={"x-forwarded-for": "203.0.113.99"},
    )
    malformed = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"x-forwarded-for": "not-an-ip"},
    )

    assert server._request_ip(proxied) == "203.0.113.7"
    assert server._request_ip(direct) == "198.51.100.9"
    assert server._request_ip(malformed) == "127.0.0.1"


def test_login_sets_httponly_secure_strict_cookie_without_returning_token():
    cfg = _cfg()
    with TestClient(create_app(cfg), base_url=cfg.public_origin) as client:
        response = _login(client, cfg)

    body = response.json()
    assert body["ok"] is True
    assert "token" not in body
    cookie = response.headers["set-cookie"].lower()
    assert f"{SESSION_COOKIE_NAME}=" in cookie
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=strict" in cookie
    cookie_value = response.cookies[SESSION_COOKIE_NAME].strip('"')
    assert verify_session_token(cookie_value, cfg.session_secret)


def test_login_and_logout_reject_cross_origin_browser_posts():
    cfg = _cfg()
    with TestClient(create_app(cfg), base_url=cfg.public_origin) as client:
        rejected_login = client.post(
            "/api/login",
            json={"password": cfg.login_password},
            headers={"Origin": "https://evil.example"},
        )
        assert rejected_login.status_code == 403

        login = _login(client, cfg)
        assert login.status_code == 200
        rejected_logout = client.post(
            "/api/logout", headers={"Origin": "https://evil.example"})
        assert rejected_logout.status_code == 403
        assert client.get("/api/session").status_code == 200


@pytest.mark.parametrize("message", [
    "WebSocket /ws?token=unique-secret-marker accepted",
    "wss://remote.example/ws?password=unique-secret-marker",
    'payload={"password":"unique-secret-marker"}',
    "Authorization: Bearer unique-secret-marker",
    "Cookie: cc_remote_session=unique-secret-marker",
])
def test_relay_log_redaction_removes_secret_markers(message):
    assert "unique-secret-marker" not in redact_log_text(message)


def test_relay_log_filter_redacts_format_arguments_and_scope():
    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, __file__, 1,
        "WebSocket %s", ("/ws?token=unique-secret-marker",), None,
    )
    record.scope = {
        "headers": {"authorization": "Bearer unique-secret-marker"},
        "query_string": b"token=unique-secret-marker",
    }

    assert SensitiveLogFilter().filter(record) is True
    assert "unique-secret-marker" not in record.getMessage()
    assert "unique-secret-marker" not in repr(record.scope)


def test_login_read_has_total_timeout_and_releases_capacity(monkeypatch):
    cfg = _cfg(login_read_timeout=1, login_inflight_cap=1)
    app = create_app(cfg)
    # Keep startup validation realistic while making the regression fast.
    cfg.login_read_timeout = 0.01

    async def never_finishes(_req, _max_bytes):
        await asyncio.Event().wait()

    monkeypatch.setattr(server, "_read_json_limited", never_finishes)
    status, body, _headers = asyncio.run(_asgi_login(app))

    assert status == 408
    assert json.loads(body) == {"error": "request_timeout"}
    assert app.state.login_slots._value == 1


def test_login_rejects_excess_inflight_bodies_without_waiting(monkeypatch):
    cfg = _cfg(login_inflight_cap=1)
    app = create_app(cfg)

    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_read(_req, _max_bytes):
            entered.set()
            await release.wait()
            return {"password": cfg.login_password}

        monkeypatch.setattr(server, "_read_json_limited", blocked_read)
        first = asyncio.create_task(_asgi_login(app))
        await entered.wait()
        second_status, second_body, second_headers = await _asgi_login(app)
        release.set()
        first_status, _first_body, _first_headers = await first
        return first_status, second_status, second_body, second_headers

    first_status, second_status, second_body, second_headers = asyncio.run(exercise())
    assert first_status == 200
    assert second_status == 503
    assert json.loads(second_body) == {"error": "login_capacity"}
    assert (b"retry-after", b"1") in second_headers


def test_session_token_expiry_parsing_and_boundary_are_deterministic():
    cfg = _cfg()
    token, expiry = make_session_token(cfg.session_secret, 30, now=1_000.75)
    assert expiry == 1_030
    assert session_token_expiry(token, cfg.session_secret) == expiry
    assert verify_session_token(token, cfg.session_secret, now=expiry - 0.001)
    assert not verify_session_token(token, cfg.session_secret, now=expiry)
    assert not verify_session_token(token, cfg.session_secret, now=expiry + 1)
    assert session_token_expiry(token + "tampered", cfg.session_secret) is None
    assert session_token_expiry(token, "wrong-secret") is None


def test_session_tokens_have_unique_signed_jti_even_in_the_same_second():
    cfg = _cfg()
    first, first_expiry = make_session_token(cfg.session_secret, 30, now=1_000)
    second, second_expiry = make_session_token(cfg.session_secret, 30, now=1_000)
    first_claims = session_token_claims(first, cfg.session_secret)
    second_claims = session_token_claims(second, cfg.session_secret)

    assert first_expiry == second_expiry
    assert first != second
    assert first_claims is not None and second_claims is not None
    assert first_claims.jti != second_claims.jti


def test_unicode_login_password_is_compared_without_type_error():
    cfg = _cfg(login_password="正确马电池订书钉正确马电池")
    with TestClient(create_app(cfg), base_url=cfg.public_origin) as client:
        assert client.post(
            "/api/login", json={"password": cfg.login_password}).status_code == 200
        assert client.post(
            "/api/login", json={"password": "不正确"}).status_code == 401


def test_protocol_validation_error_never_embeds_untrusted_input():
    sentinel = "UNLABELED_SECRET_SHOULD_NOT_REACH_LOGS"
    raw = json.dumps({
        "v": PROTOCOL_VERSION,
        "type": "query",
        "prompt": [sentinel],
        "msg_id": "m1",
        "images": [{"media_type": "image/png", "data": [sentinel]}],
    })
    with pytest.raises(ProtocolError) as error:
        deserialize(raw)
    assert sentinel not in str(error.value)
    assert "prompt" in str(error.value)

    with pytest.raises(ProtocolError):
        deserialize(json.dumps({
            "v": PROTOCOL_VERSION, "type": "hello", "role": "client",
            "client_id": "../../unsafe*glob",
        }))


def test_protocol_normalizes_pathological_json_numbers():
    raw = '{"v":' + ("9" * 5000) + ',"type":"hello","role":"client"}'
    with pytest.raises(ProtocolError, match="invalid JSON payload"):
        deserialize(raw)


def test_login_body_is_streamed_and_rejected_above_hard_limit():
    cfg = _cfg(login_body_max_bytes=256)

    def chunks():
        yield b'{"password":"'
        yield b"x" * 512
        yield b'"}'

    with TestClient(create_app(cfg), base_url=cfg.public_origin) as client:
        response = client.post(
            "/api/login",
            content=chunks(),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json() == {"error": "too_large"}


def test_login_rejects_non_string_password_without_server_error():
    cfg = _cfg()
    with TestClient(create_app(cfg), base_url=cfg.public_origin) as client:
        response = client.post("/api/login", json={"password": {"nested": True}})
    assert response.status_code == 401


def test_login_rate_limiter_globally_prunes_and_never_exceeds_caps():
    limiter = server.LoginRateLimiter(
        window=60,
        max_per_ip=2,
        max_ips=2,
        max_total_attempts=3,
        cleanup_interval=5,
    )
    assert not limiter.limited("ip-1", now=0)
    assert not limiter.limited("ip-1", now=1)
    assert limiter.limited("ip-1", now=2)
    assert not limiter.limited("ip-2", now=2)
    assert limiter.limited("ip-3", now=3)
    assert limiter.key_count == 2
    assert limiter.total_attempts == 3

    # The periodic global sweep removes stale keys, not just the current IP.
    assert not limiter.limited("ip-3", now=62)
    assert limiter.key_count == 1
    assert limiter.total_attempts == 1


def test_loopback_http_cookie_is_not_secure_but_remains_httponly_strict():
    cfg = _cfg(public_origin="http://127.0.0.1:8765")
    with TestClient(create_app(cfg), base_url=cfg.public_origin) as client:
        response = _login(client, cfg)

    cookie = response.headers["set-cookie"].lower()
    assert "secure" not in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie


def test_logout_expires_session_cookie_with_matching_security_attributes():
    cfg = _cfg()
    with TestClient(create_app(cfg), base_url=cfg.public_origin) as client:
        _login(client, cfg)
        assert client.get("/api/session").status_code == 200
        response = client.post("/api/logout")
        assert client.get("/api/session").status_code == 401

    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert f"{SESSION_COOKIE_NAME}=" in cookie
    assert "max-age=0" in cookie
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=strict" in cookie


def test_logout_revokes_session_and_closes_all_of_its_websockets():
    cfg = _cfg()
    with TestClient(create_app(cfg), base_url=cfg.public_origin) as client:
        login = _login(client, cfg)
        headers = {"cookie": _cookie_header(login), "origin": cfg.public_origin}
        with client.websocket_connect("/ws", headers=headers) as first:
            with client.websocket_connect("/ws", headers=headers) as second:
                response = client.post("/api/logout", headers={"cookie": _cookie_header(login)})
                first_close = first.receive()
                second_close = second.receive()

    assert response.status_code == 200
    expected = {
        "type": "websocket.close",
        "code": server.SESSION_REVOKED_CLOSE_CODE,
        "reason": server.SESSION_REVOKED_CLOSE_REASON,
    }
    assert first_close == expected
    assert second_close == expected


def test_relay_restart_invalidates_old_registry_session():
    cfg = _cfg()
    with TestClient(create_app(cfg), base_url=cfg.public_origin) as first_relay:
        login = _login(first_relay, cfg)
    headers = {"cookie": _cookie_header(login), "origin": cfg.public_origin}

    # A fresh app has the same HMAC secret but a new empty registry.
    with TestClient(create_app(cfg), base_url=cfg.public_origin) as restarted:
        assert restarted.get("/api/session", headers=headers).status_code == 401
        with pytest.raises(WebSocketDisconnect) as error:
            with restarted.websocket_connect("/ws", headers=headers):
                pass
    assert error.value.code == 1008


def test_session_registry_capacity_fails_closed():
    cfg = _cfg(session_registry_cap=1)
    with TestClient(create_app(cfg), base_url=cfg.public_origin) as client:
        assert _login(client, cfg).status_code == 200
        second = client.post("/api/login", json={"password": cfg.login_password})
    assert second.status_code == 503
    assert second.json() == {"error": "session_capacity"}


def test_cookie_and_exact_origin_authenticate_browser_websocket():
    cfg = _cfg()
    with TestClient(create_app(cfg), base_url=cfg.public_origin) as client:
        login = _login(client, cfg)
        headers = {"cookie": _cookie_header(login), "origin": cfg.public_origin}
        with client.websocket_connect("/ws", headers=headers) as websocket:
            websocket.send_text(serialize(Hello(role="client", client_id="auth-test")))
            assert json.loads(websocket.receive_text())["code"] == "wrapper_offline"
            websocket.close()


def test_cookie_websocket_is_bound_to_signed_expiry(monkeypatch):
    cfg = _cfg()
    guarded = {}

    async def expire_immediately(websocket, hub, expires_at, revoked):
        guarded["expires_at"] = expires_at
        await websocket.close(
            code=server.SESSION_EXPIRED_CLOSE_CODE,
            reason=server.SESSION_EXPIRED_CLOSE_REASON,
        )

    monkeypatch.setattr(server, "_serve_client_until_expiry", expire_immediately)
    with TestClient(create_app(cfg), base_url=cfg.public_origin) as client:
        login = _login(client, cfg)
        headers = {"cookie": _cookie_header(login), "origin": cfg.public_origin}
        with client.websocket_connect("/ws", headers=headers) as websocket:
            closed = websocket.receive()

    assert guarded["expires_at"] == login.json()["exp"]
    assert closed == {
        "type": "websocket.close",
        "code": server.SESSION_EXPIRED_CLOSE_CODE,
        "reason": server.SESSION_EXPIRED_CLOSE_REASON,
    }


def test_expiry_guard_cancels_client_handler_and_closes_socket():
    class StubHub:
        cancelled = False

        async def serve_client(self, websocket):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True

    class StubWebSocket:
        closed = None

        async def close(self, *, code, reason):
            self.closed = (code, reason)

    async def run():
        hub = StubHub()
        websocket = StubWebSocket()
        await server._serve_client_until_expiry(
            websocket, hub, time.time() + 0.01, asyncio.Event()
        )
        return hub, websocket

    hub, websocket = asyncio.run(run())
    assert hub.cancelled is True
    assert websocket.closed == (
        server.SESSION_EXPIRED_CLOSE_CODE,
        server.SESSION_EXPIRED_CLOSE_REASON,
    )


@pytest.mark.parametrize("origin", ["", "https://evil.example", "https://remote.example/"])
def test_cookie_websocket_rejects_missing_or_mismatched_origin(origin: str):
    cfg = _cfg()
    with TestClient(create_app(cfg), base_url=cfg.public_origin) as client:
        login = _login(client, cfg)
        headers = {"cookie": _cookie_header(login)}
        if origin:
            headers["origin"] = origin
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("/ws", headers=headers):
                pass
    assert error.value.code == 1008


def test_query_session_token_is_rejected_even_when_signature_is_valid():
    cfg = _cfg()
    token, _ = make_session_token(cfg.session_secret, cfg.session_ttl_seconds)
    with TestClient(create_app(cfg), base_url=cfg.public_origin) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(
                f"/ws?token={quote(token, safe='')}",
                headers={"origin": cfg.public_origin},
            ):
                pass
    assert error.value.code == 1008


def test_wrapper_bearer_does_not_require_browser_origin_or_expiry(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr(
        server,
        "session_token_claims",
        lambda *args: pytest.fail("wrapper auth must not parse a browser session"),
    )
    with TestClient(create_app(cfg), base_url=cfg.public_origin) as client:
        with client.websocket_connect(
            "/ws", headers={"authorization": f"Bearer {cfg.wrapper_token}"}
        ):
            pass


def test_scalar_and_nested_secret_log_fields_are_redacted():
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        'Authorization: Bearer top-secret password="hunter two" token=url-secret',
        (),
        None,
    )
    record.extra_data = {
        "token": "top-level-token",
        "password": "top-level-password",
        "prompt": "user pasted an unlabeled secret",
        "line": "raw child stderr secret",
        "error": "request failed: api_key=error-secret",
        "nested": {"secret": "nested-secret", "safe": "visible"},
        "safe": "visible",
    }
    payload = json.loads(JsonFormatter().format(record))
    assert "top-secret" not in payload["msg"]
    assert "hunter two" not in payload["msg"]
    assert "url-secret" not in payload["msg"]
    assert payload["token"] == "***"
    assert payload["password"] == "***"
    assert payload["prompt"] == "***"
    assert payload["line"] == "***"
    assert "error-secret" not in payload["error"]
    assert payload["nested"] == {"secret": "***", "safe": "visible"}
    assert payload["safe"] == "visible"


def test_child_stderr_message_is_never_logged_verbatim():
    record = logging.LogRecord(
        "test", logging.WARNING, __file__, 1,
        "cc stderr: unlabeled-SENTINEL-secret", (), None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["msg"] == "cc stderr: ***"


def test_structured_logging_is_bounded_and_never_stringifies_objects():
    class Explosive:
        def __str__(self):
            raise AssertionError("logger must not invoke arbitrary __str__")

    record = logging.LogRecord("test", logging.INFO, __file__, 1, "ok", (), None)
    record.extra_data = {
        "huge": "x" * 100_000,
        "many": list(range(1000)),
        "object": Explosive(),
    }
    payload = json.loads(JsonFormatter().format(record))

    assert len(payload["huge"]) < 9000
    assert len(payload["many"]) == 65
    assert payload["object"] == "<Explosive>"


def test_relay_config_construction_is_test_friendly_but_startup_validation_fails_closed():
    RelayConfig()  # constructing a local/unit-test config must not raise
    invalid = RelayConfig(
        login_password="",
        session_secret="",
        wrapper_token="change-me-wrapper",
        public_origin="",
    )
    with pytest.raises(ValueError) as error:
        validate_relay_config(invalid)
    message = str(error.value)
    assert "LOGIN_PASSWORD" in message
    assert "SESSION_SECRET" in message
    assert "WRAPPER_TOKEN" in message
    assert "PUBLIC_ORIGIN" in message
    validate_relay_config(_cfg())
    validate_relay_config(_cfg(public_origin="http://localhost:8765"))


def test_relay_config_rejects_non_tls_public_origin():
    with pytest.raises(ValueError, match="PUBLIC_ORIGIN must use https"):
        validate_relay_config(_cfg(public_origin="http://remote.example"))


def test_allow_insecure_http_permits_a_plain_http_public_ip_origin():
    # Explicit opt-in escape hatch: a bare public IP without a TLS terminator.
    cfg = _cfg(public_origin="http://198.51.100.10:8765", allow_insecure_http=True)
    validate_relay_config(cfg)  # must not raise
    assert cfg.public_origin == "http://198.51.100.10:8765"


def test_allow_insecure_http_off_still_rejects_non_tls_public_origin():
    with pytest.raises(ValueError, match="PUBLIC_ORIGIN must use https"):
        validate_relay_config(
            _cfg(public_origin="http://remote.example", allow_insecure_http=False)
        )


def test_allow_insecure_http_cookie_is_not_secure_for_plain_http_public_origin():
    cfg = _cfg(
        public_origin="http://198.51.100.10:8765", allow_insecure_http=True
    )
    with TestClient(create_app(cfg), base_url=cfg.public_origin) as client:
        response = _login(client, cfg)
    cookie = response.headers["set-cookie"].lower()
    assert "secure" not in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie


@pytest.mark.parametrize(
    ("configured", "canonical"),
    [
        ("https://REMOTE.Example", "https://remote.example"),
        ("https://remote.example:443", "https://remote.example"),
        ("https://REMOTE.Example:8443/", "https://remote.example:8443"),
        ("http://LOCALHOST:80/", "http://localhost"),
    ],
)
def test_public_origin_is_normalized_to_browser_serialization(configured, canonical):
    cfg = _cfg(public_origin=configured)
    validate_relay_config(cfg)
    assert cfg.public_origin == canonical


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("login_body_max_bytes", 128, "LOGIN_BODY_MAX_BYTES"),
        ("login_read_timeout", float("nan"), "LOGIN_READ_TIMEOUT"),
        ("login_read_timeout", 0, "LOGIN_READ_TIMEOUT"),
        ("login_inflight_cap", 0, "LOGIN_INFLIGHT_CAP"),
        ("session_registry_cap", 0, "SESSION_REGISTRY_CAP"),
        ("max_clients", 0, "MAX_CLIENTS"),
        ("client_hello_timeout", 0, "CLIENT_HELLO_TIMEOUT"),
        ("ws_max_size_bytes", 128, "WS_MAX_SIZE_BYTES"),
    ],
)
def test_relay_config_rejects_invalid_resource_limits(field, value, message):
    with pytest.raises(ValueError, match=message):
        validate_relay_config(_cfg(**{field: value}))


def test_uvicorn_access_log_is_disabled(monkeypatch):
    import cc_remote.relay.__main__ as relay_main

    cfg = _cfg()
    app = object()
    called = {}
    monkeypatch.setattr(relay_main, "relay_config", lambda: cfg)
    monkeypatch.setattr(relay_main, "create_app", lambda actual: app)
    monkeypatch.setattr(relay_main.uvicorn, "run", lambda *args, **kwargs: called.update({"args": args, "kwargs": kwargs}))

    relay_main.main()

    assert called["args"] == (app,)
    assert called["kwargs"]["access_log"] is False
    configured = called["kwargs"]["log_config"]
    expected = uvicorn_log_config()
    assert configured.keys() == expected.keys()
    for handler in configured["handlers"].values():
        assert "cc_remote_sensitive_log_redaction" in handler["filters"]
    assert called["kwargs"]["ws_max_size"] == cfg.ws_max_size_bytes
    assert called["kwargs"]["ws_max_queue"] == 2
