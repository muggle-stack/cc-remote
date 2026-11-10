"""Actionable updater diagnostics without echoing URLs, credentials or stderr."""
from __future__ import annotations

import errno
import socket
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit


def read_with_retry(open_request, request, *, timeout: int, limit: int) -> bytes:
    """Bounded retries for read-only metadata; never retry authentication errors."""
    for attempt in range(3):
        try:
            with open_request(request, timeout=timeout) as response:
                return response.read(limit + 1)
        except OSError as exc:
            cause = exc.reason if isinstance(exc, URLError) else exc
            transient = (
                isinstance(exc, HTTPError) and exc.code in {408, 429, 500, 502, 503, 504}
                or isinstance(cause, (TimeoutError, ConnectionError, ssl.SSLEOFError))
                or isinstance(cause, socket.gaierror) and cause.errno == socket.EAI_AGAIN
                or isinstance(cause, OSError) and cause.errno in {errno.EHOSTUNREACH, errno.ENETUNREACH}
            ) and not isinstance(cause, ssl.SSLCertVerificationError)
            if attempt == 2 or not transient:
                raise
            time.sleep(0.5 * (attempt + 1))
    raise AssertionError("unreachable metadata retry")



def endpoint(url: str) -> str:
    """Only display the origin: redirected URLs can contain signed credentials."""
    parsed = urlsplit(url)
    host = parsed.hostname or "local release files"
    return host.encode("unicode_escape").decode("ascii")


def network_reason(error: BaseException) -> str:
    if isinstance(error, HTTPError):
        if error.code == 407:
            return "HTTP 407: proxy authentication required; check your proxy configuration"
        if 300 <= error.code < 400:
            return f"HTTP {error.code}: unexpected redirect; check the configured Relay URL and reverse proxy"
        return f"HTTP {error.code}; check the endpoint, server availability and access permissions"
    if isinstance(error, URLError) and isinstance(error.reason, BaseException):
        return network_reason(error.reason)
    if isinstance(error, ssl.SSLCertVerificationError):
        return "TLS certificate verification failed; check the certificate, system clock and trusted CA configuration"
    if isinstance(error, ssl.SSLError):
        return "TLS handshake/connection failed; check your network, proxy and the server's HTTPS configuration"
    if isinstance(error, socket.gaierror):
        return "DNS lookup failed; check the hostname and your DNS/network configuration"
    if isinstance(error, TimeoutError):
        return "connection timed out; check your network, proxy and server availability"
    if isinstance(error, ConnectionRefusedError):
        return "connection refused; check the server address, listening port and firewall"
    if isinstance(error, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return "connection was interrupted/reset; check your network, proxy and server availability"
    return "network request failed; check your network, proxy and server availability"


def download_reason(code: int, http_status: str) -> str:
    reasons = {
        5: "proxy DNS lookup failed; check your proxy hostname and DNS",
        6: "DNS lookup failed; check your DNS/network configuration",
        7: "connection failed; check your network, proxy, firewall and server address",
        23: "cannot write the downloaded file; check disk space and directory permissions",
        28: "connection timed out; check your network, proxy and server availability",
        35: "TLS handshake failed; check your network, proxy and the server's HTTPS configuration",
        51: "TLS certificate verification failed; check the certificate and trusted CA configuration",
        60: "TLS certificate verification failed; check the certificate, system clock and trusted CA configuration",
        77: "cannot read the TLS CA certificate file; check your trusted CA configuration",
        52: "server closed the connection without a response; check your network, proxy and server",
        55: "connection failed while sending; check your network and proxy",
        56: "connection failed while receiving; check your network, proxy and server",
        63: "download exceeds the permitted size; check the release source",
    }
    if code == 22 and len(http_status) == 3 and http_status.isdigit():
        return f"HTTP {http_status}; check release availability and access permissions"
    return reasons.get(code, f"curl exited with code {code}; check the release source and network configuration")


def ssh_reason(code: int, stderr: str) -> str:
    # Only recognize categories. Never echo helper output, which may contain secrets.
    message = stderr.lower()
    if "sudo:" in message and any(text in message for text in (
        "password", "terminal is required", "not allowed", "not in the sudoers",
    )):
        return "sudo access denied or a password/terminal is required; check noninteractive sudo permissions on the SSH host"
    if code == 255:
        if "could not resolve hostname" in message:
            return "SSH hostname lookup failed; check the SSH target, config and DNS"
        if "host key verification failed" in message or "remote host identification has changed" in message:
            return "SSH host key verification failed; verify the server identity and your known_hosts entry"
        if "permission denied" in message:
            return "SSH authentication failed; check the SSH account, key and agent"
        if "connection refused" in message:
            return "SSH connection refused; check the SSH port and server availability"
        if "timed out" in message:
            return "SSH connection timed out; check your network, SSH routing and firewall"
        if "no route to host" in message or "network is unreachable" in message:
            return "SSH host is unreachable; check your network and routing"
        return "SSH connection/handshake failed or was interrupted; check your network and SSH configuration"
    return f"remote command failed (exit {code}); check sudo permissions and the remote service/job logs"
