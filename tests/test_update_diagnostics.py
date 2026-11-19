"""User-facing failure stages and sanitized network diagnostics."""
import io
import socket
import ssl
import subprocess
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from cc_remote import update, update_download, update_errors, update_relay
from tests.test_update import _installation


@pytest.fixture
def retry_delays(monkeypatch):
    delays = []
    # Network failures are simulated here, so record backoff without waiting.
    monkeypatch.setattr(update_errors, "time", SimpleNamespace(sleep=delays.append))
    monkeypatch.setattr(update_download, "time", SimpleNamespace(sleep=delays.append))
    return delays


@pytest.mark.parametrize("error,expected", [
    (URLError(ssl.SSLEOFError("secret proxy token")), "TLS handshake"),
    (URLError(ssl.SSLCertVerificationError("secret certificate detail")), "TLS certificate verification"),
    (URLError(socket.gaierror(-2, "secret hostname")), "DNS lookup"),
    (URLError(TimeoutError("secret URL")), "timed out"),
    (URLError(ConnectionRefusedError("secret address")), "connection refused"),
    (URLError(ConnectionResetError("secret upstream")), "connection was interrupted/reset"),
    (HTTPError("https://secret/", 502, "secret body", {}, None), "HTTP 502"),
    (HTTPError("https://secret/", 407, "secret body", {}, None), "proxy authentication"),
    (HTTPError("https://secret/", 302, "secret Location", {}, None), "unexpected redirect"),
])
@pytest.mark.parametrize("path,stage", [("healthz", "Relay health check"), ("cc-remote-build.json", "Relay Web version check")])
def test_relay_network_failure_names_the_stage_and_cause_without_leaking_secrets(monkeypatch, error, expected, path, stage, retry_delays):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(update_relay, "build_opener", lambda *args: SimpleNamespace(open=fail))
    with pytest.raises(update.UpdateError) as caught:
        update_relay._get_json(f"https://user:secret@relay.example.test/{path}?token=secret")
    message = str(caught.value)
    assert stage in message and expected in message and "relay.example.test" in message
    assert "check" in message and "no local service was changed" in message
    assert "secret" not in message
    transient = expected in {
        "TLS handshake", "timed out", "connection refused",
        "connection was interrupted/reset", "HTTP 502",
    }
    assert retry_delays == ([0.5, 1.0] if transient else [])


def test_bad_server_response_is_not_misreported_as_a_network_problem(monkeypatch):
    monkeypatch.setattr(update_relay, "build_opener", lambda *args: SimpleNamespace(open=lambda *a, **k: io.BytesIO(b"<html>login</html>")))
    with pytest.raises(update.UpdateError, match="invalid Relay JSON response"):
        update_relay._get_json("https://relay.example.test/healthz")


@pytest.mark.parametrize("code,expected", [(5, "proxy DNS"), (6, "DNS lookup"), (7, "connection failed"),
                                         (22, "HTTP 403"), (23, "disk space"), (28, "timed out"),
                                         (35, "TLS handshake"), (60, "TLS certificate verification"),
                                         (56, "receiving"), (63, "permitted size")])
def test_download_failure_preserves_curl_reason_but_not_signed_redirects(tmp_path, monkeypatch, code, expected, capsys, retry_delays):
    def run(command, **kwargs):
        assert kwargs["capture_output"] is True
        assert "--write-out" in command
        return SimpleNamespace(returncode=code, stdout="403", stderr="https://private.example/?secret=credential")
    monkeypatch.setattr(update.subprocess, "run", run)
    with pytest.raises(update.UpdateError) as caught:
        update._download("https://releases.example.test/SHA256SUMS", tmp_path / "SHA256SUMS", 1024)
    message = str(caught.value)
    assert "SHA256SUMS" in message and "releases.example.test" in message and expected in message
    assert "no service was changed" in message
    assert "secret" not in message and "private.example" not in message
    assert not capsys.readouterr().err
    assert retry_delays == ([0.5, 1.0] if code in {5, 6, 7, 28, 35, 56} else [])


@pytest.mark.parametrize("code,stderr,expected", [
    (255, "ssh: Could not resolve hostname x: secret", "hostname lookup failed"),
    (255, "ssh: connect to host x port 22: Connection refused; secret", "SSH connection refused"),
    (255, "ssh: connect to host x port 22: Connection timed out; secret", "SSH connection timed out"),
    (255, "user@host: Permission denied (publickey). secret", "SSH authentication failed"),
    (255, "Host key verification failed. secret", "host key verification failed"),
    (255, "Connection reset by peer; secret", "SSH connection/handshake failed"),
    (1, "sudo: a password is required; secret", "sudo access denied"),
    (1, "secret remote command output", "remote command failed (exit 1)"),
])
def test_ssh_failure_explains_access_or_transport_problem(tmp_path, monkeypatch, code, stderr, expected):
    installation = _installation(tmp_path, monkeypatch)
    monkeypatch.setattr(update_relay.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=code, stderr=stderr, stdout="secret"))
    with pytest.raises(update.UpdateError) as caught:
        update_relay.RelayUpdate(installation, "operator@relay")._ssh(["true"], operation="Relay update submission")
    message = str(caught.value)
    assert expected in message and "Relay update submission" in message
    assert "secret" not in message and "retry update" in message


def test_ssh_operation_timeout_keeps_remote_outcome_unknown(tmp_path, monkeypatch):
    installation = _installation(tmp_path, monkeypatch)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(["ssh", "secret"], 30)
    monkeypatch.setattr(update_relay.subprocess, "run", timeout)
    with pytest.raises(update.UpdateError, match="submitted job may still be running") as caught:
        update_relay.RelayUpdate(installation, "relay")._ssh(["true"])
    assert "secret" not in str(caught.value)
