"""Regression tests for the forwarded-header trust boundary.

The relay sits behind a same-host reverse proxy, so it accepts
``X-Forwarded-Proto`` / ``X-Forwarded-For`` — but only from peers on an
allowlist. Uvicorn enforces that in ``ProxyHeadersMiddleware``, which it wraps
around the app at startup (``uvicorn/config.py``). These tests drive that same
middleware instead of asserting on the allowlist string, because the string
being right is not what makes the feature work.

The failure this guards is silent: when a proxy connects from an address that
is not trusted, uvicorn ignores the forwarded scheme, the relay computes an
``http`` request target while the browser's Origin says ``https``, and every
WebSocket upgrade is rejected with 403 while page loads and ``/api/*`` keep
working. See the PR description for the original report.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from cc_remote.config import (
    LOOPBACK_PROXY_IPS,
    RelayConfig,
    _forwarded_allow_ips,
    validate_relay_config,
)
from cc_remote.protocol import Hello, serialize
from cc_remote.relay import server
from cc_remote.relay.auth import SESSION_COOKIE_NAME
from cc_remote.relay.server import create_app

ORIGIN = "https://remote.example"
# A non-loopback address standing in for a Tailscale/LAN/Docker-bridge proxy.
PROXY_IP = "100.64.0.9"
FOREIGN_IP = "203.0.113.77"
PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _clear_login_rate_limit():
    server._login_limiter.reset()
    yield
    server._login_limiter.reset()


def _cfg(**overrides) -> RelayConfig:
    values = {
        "login_password": PASSWORD,
        "session_secret": "s" * 48,
        "wrapper_token": "w" * 48,
        "public_origin": ORIGIN,
        "session_ttl_seconds": 3600,
        "device_db_path": str(
            Path(tempfile.mkdtemp(prefix="cc-remote-fwd-test-")) / "devices.sqlite3"
        ),
    }
    values.update(overrides)
    return RelayConfig(**values)


def _client(cfg: RelayConfig, peer: str) -> TestClient:
    """A client whose TCP peer is ``peer``, behind uvicorn's proxy middleware.

    ``TestClient(client=...)`` sets the ASGI ``client`` entry, which is exactly
    the peer address the middleware inspects, so this reproduces the production
    trust decision without needing a real reverse proxy. The transport is plain
    ``http`` because that is what the browser-to-proxy leg looks like on the
    wire: TLS terminates at Caddy/nginx, and the relay learns the real scheme
    only from ``X-Forwarded-Proto``.
    """
    app = create_app(cfg)
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=cfg.forwarded_allow_ips)
    return TestClient(wrapped, base_url="http://remote.example", client=(peer, 12345))


def _login(client: TestClient, password: str = PASSWORD, **headers):
    return client.post(
        "/api/login",
        json={"password": password},
        headers={"Origin": ORIGIN, "X-Forwarded-Proto": "https", **headers},
    )


def _ws_url(path: str = "/ws") -> str:
    """WebSocket URL for ``TestClient``.

    ``websocket_connect`` ignores the client's ``base_url`` and defaults to
    ``testserver``, which would not match ``PUBLIC_ORIGIN`` in the origin gate.
    """
    return f"ws://{ORIGIN.split('://', 1)[1]}{path}"


def _ws_cookie(client: TestClient) -> str:
    response = _login(client)
    assert response.status_code == 200
    token = response.cookies.get(SESSION_COOKIE_NAME)
    assert token
    return f"{SESSION_COOKIE_NAME}={token}"


# --------------------------------------------------------------------------
# The env-var contract: defaults, composition, validation
# --------------------------------------------------------------------------


def test_unset_or_blank_keeps_loopback_only(monkeypatch):
    for value in (None, "", "   ", ",", " , ,"):
        if value is None:
            monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
        else:
            monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
        assert _forwarded_allow_ips() == "127.0.0.1,::1"
        assert _cfg().forwarded_allow_ips == "127.0.0.1,::1"


def test_extra_proxies_are_appended_after_the_loopback_defaults(monkeypatch):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", f"{PROXY_IP} , fd7a:115c:a1e0::/48")
    assert _forwarded_allow_ips() == f"127.0.0.1,::1,{PROXY_IP},fd7a:115c:a1e0::/48"
    # The defaults survive configuration of extras -- an operator adding a
    # proxy must not have to restate them.
    for loopback in LOOPBACK_PROXY_IPS:
        assert loopback in _forwarded_allow_ips().split(",")


def test_duplicates_and_loopback_restatements_are_dropped(monkeypatch):
    monkeypatch.setenv(
        "FORWARDED_ALLOW_IPS", f"127.0.0.1,{PROXY_IP}, ::1 ,{PROXY_IP},127.0.0.1"
    )
    assert _forwarded_allow_ips() == f"127.0.0.1,::1,{PROXY_IP}"


@pytest.mark.parametrize("value", ["*", "0.0.0.0/0", "::/0"])
def test_wildcard_trust_is_rejected(value):
    with pytest.raises(ValueError, match="FORWARDED_ALLOW_IPS"):
        validate_relay_config(_cfg(forwarded_allow_ips=value))


@pytest.mark.parametrize(
    "value",
    ["not-an-ip", "proxy.internal", "100.64.0.9/24", "100.64.0.9/", "1.2.3.4:5"],
)
def test_malformed_entries_are_rejected_instead_of_silently_unmatched(value):
    # uvicorn stores these as string literals that are compared against a peer
    # address, so they start cleanly and then never match. Reject at startup.
    with pytest.raises(ValueError, match="not a valid IP address or CIDR"):
        validate_relay_config(_cfg(forwarded_allow_ips=f"127.0.0.1,::1,{value}"))


def test_valid_extra_entries_pass_validation():
    validate_relay_config(
        _cfg(forwarded_allow_ips=f"127.0.0.1,::1,{PROXY_IP},fd7a:115c:a1e0::/48")
    )


def test_main_passes_the_configured_allowlist_to_uvicorn(monkeypatch):
    import cc_remote.relay.__main__ as relay_main

    cfg = _cfg(forwarded_allow_ips=f"127.0.0.1,::1,{PROXY_IP}")
    called: dict = {}
    monkeypatch.setattr(relay_main, "relay_config", lambda: cfg)
    monkeypatch.setattr(relay_main, "create_app", lambda actual: object())
    monkeypatch.setattr(
        relay_main.uvicorn, "run",
        lambda *args, **kwargs: called.update(kwargs),
    )

    relay_main.main()

    assert called["proxy_headers"] is True
    assert called["forwarded_allow_ips"] == f"127.0.0.1,::1,{PROXY_IP}"


# --------------------------------------------------------------------------
# Middleware behavior: scheme
# --------------------------------------------------------------------------


def _cfg_via_env(monkeypatch, extras: str = "") -> RelayConfig:
    """Build the config the way a deployment does: through the environment.

    ``RelayConfig.forwarded_allow_ips`` defaults to ``_forwarded_allow_ips()``,
    which reads ``FORWARDED_ALLOW_IPS`` at construction time. Going through the
    env var keeps these tests on the real production path instead of injecting
    an allowlist that no deployment could produce.
    """
    if extras:
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", extras)
    else:
        monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    return _cfg()


def test_configured_proxy_scheme_is_trusted_and_matches_the_browser_origin(monkeypatch):
    cfg = _cfg_via_env(monkeypatch, PROXY_IP)
    assert PROXY_IP in cfg.forwarded_allow_ips.split(",")
    with _client(cfg, PROXY_IP) as client:
        response = _login(client)
    assert response.status_code == 200


def test_untrusted_peer_cannot_assert_a_scheme_it_does_not_terminate(monkeypatch):
    """The exact reported failure: the origin check compares the forwarded
    scheme against the browser Origin and rejects every WebSocket."""
    cfg = _cfg_via_env(monkeypatch, PROXY_IP)
    with _client(cfg, FOREIGN_IP) as client:
        response = _login(client)
    assert response.status_code == 403
    assert response.json() == {"error": "origin_rejected"}


def test_loopback_only_is_the_default_when_the_env_var_is_unset(monkeypatch):
    """Unset must reproduce the original behavior byte for byte: a proxy on a
    non-loopback address is not trusted, and loopback still is."""
    cfg = _cfg_via_env(monkeypatch)
    assert cfg.forwarded_allow_ips == "127.0.0.1,::1"
    with _client(cfg, PROXY_IP) as client:
        assert _login(client).status_code == 403
    with _client(cfg, "127.0.0.1") as client:
        assert _login(client).status_code == 200


def test_blank_env_var_is_treated_as_unset(monkeypatch):
    cfg = _cfg_via_env(monkeypatch, "   ")
    assert cfg.forwarded_allow_ips == "127.0.0.1,::1"
    with _client(cfg, PROXY_IP) as client:
        assert _login(client).status_code == 403


def test_the_scheme_mismatch_disappears_once_the_proxy_address_is_trusted(monkeypatch):
    """Same peer, same headers -- only the allowlist changes."""
    untrusted = _cfg_via_env(monkeypatch)
    with _client(untrusted, PROXY_IP) as client:
        assert _login(client).status_code == 403

    trusted = _cfg_via_env(monkeypatch, PROXY_IP)
    with _client(trusted, PROXY_IP) as client:
        assert _login(client).status_code == 200


def test_loopback_proxy_stays_trusted_when_extras_are_configured(monkeypatch):
    cfg = _cfg_via_env(monkeypatch, PROXY_IP)
    for loopback in LOOPBACK_PROXY_IPS:
        with _client(cfg, loopback) as client:
            assert _login(client).status_code == 200


def test_proxy_without_forwarded_proto_still_uses_the_real_scheme(monkeypatch):
    """A trusted proxy that forwards nothing must not break the plain path."""
    cfg = _cfg_via_env(monkeypatch, PROXY_IP)
    with _client(cfg, PROXY_IP) as client:
        response = client.post(
            "/api/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN},
        )
    # No X-Forwarded-Proto, so the request target is http and the https Origin
    # is correctly rejected rather than silently accepted.
    assert response.status_code == 403


# --------------------------------------------------------------------------
# Middleware behavior: client address (rate-limit bucketing)
# --------------------------------------------------------------------------


def test_trusted_proxy_forwards_the_client_address_for_rate_limiting(monkeypatch):
    cfg = _cfg_via_env(monkeypatch, PROXY_IP)
    with _client(cfg, PROXY_IP) as client:
        for _ in range(server._LOGIN_MAX):
            response = _login(client, password="wrong-password-here",
                              **{"X-Forwarded-For": "203.0.113.1"})
            assert response.status_code == 401
        # The bucket is exhausted for that forwarded client...
        exhausted = _login(client, password="wrong-password-here",
                           **{"X-Forwarded-For": "203.0.113.1"})
        assert exhausted.status_code == 429
        # ...but not for a different one behind the same proxy address. Without
        # a trusted X-Forwarded-For every user would share one bucket and five
        # bad attempts would lock the relay out for everyone.
        other = _login(client, password="wrong-password-here",
                       **{"X-Forwarded-For": "203.0.113.2"})
        assert other.status_code == 401


def test_untrusted_peer_cannot_choose_its_own_rate_limit_bucket(monkeypatch):
    """A forged X-Forwarded-For must not let a caller escape the peer bucket."""
    cfg = _cfg_via_env(monkeypatch, PROXY_IP)
    with _client(cfg, FOREIGN_IP) as client:
        for _ in range(server._LOGIN_MAX):
            response = _login(client, password="wrong-password-here",
                              **{"X-Forwarded-For": "203.0.113.1"})
            # Rejected at the origin gate before the limiter is consulted.
            assert response.status_code == 403
        still_rejected = _login(client, password="wrong-password-here",
                                **{"X-Forwarded-For": "203.0.113.2"})
        assert still_rejected.status_code == 403


# --------------------------------------------------------------------------
# The same gate on the WebSocket route
# --------------------------------------------------------------------------


def test_trusted_proxy_websocket_reaches_the_handshake(monkeypatch):
    cfg = _cfg_via_env(monkeypatch, PROXY_IP)
    with _client(cfg, PROXY_IP) as client:
        cookie = _ws_cookie(client)
        with client.websocket_connect(
            _ws_url(), headers={"cookie": cookie, "origin": ORIGIN,
                                "X-Forwarded-Proto": "https"},
        ) as websocket:
            websocket.send_text(
                serialize(Hello(role="client", client_id="forwarded-test"))
            )
            # Reaching the application layer proves the origin gate passed;
            # no wrapper is connected in this test.
            assert json.loads(websocket.receive_text())["code"] == "wrapper_offline"
            # Close explicitly: letting the context manager tear the portal
            # down first races the relay's own teardown and intermittently
            # surfaces as a CancelledError instead of a clean exit.
            websocket.close()


def test_untrusted_peer_websocket_is_closed_by_the_origin_gate(monkeypatch):
    cfg = _cfg_via_env(monkeypatch, PROXY_IP)
    with _client(cfg, FOREIGN_IP) as client:
        # Login itself is rejected, so build the cookie from a trusted peer and
        # replay it from the untrusted one -- the WS gate must stand alone.
        assert client.post(
            "/api/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN},
        ).status_code == 403

    with _client(cfg, PROXY_IP) as trusted:
        cookie = _ws_cookie(trusted)

    with _client(cfg, FOREIGN_IP) as untrusted:
        # The gate rejects before accepting, so entering the context raises.
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with untrusted.websocket_connect(
                _ws_url(), headers={"cookie": cookie, "origin": ORIGIN,
                                    "X-Forwarded-Proto": "https"},
            ):
                pass
        assert excinfo.value.code == 1008
