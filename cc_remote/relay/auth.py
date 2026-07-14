"""Authentication for wrapper Bearer credentials and browser sessions."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from cc_remote.config import RelayConfig

SESSION_COOKIE_NAME = "cc_remote_session"


@dataclass(frozen=True)
class SessionClaims:
    expires_at: int
    jti: str


def authenticate_token(token: str, cfg: RelayConfig) -> Optional[str]:
    """Validate a bare wrapper token. Browser sessions are cookie-only."""
    if not token:
        return None
    if hmac.compare_digest(token.encode("utf-8"), cfg.wrapper_token.encode("utf-8")):
        return "wrapper"
    return None


def authenticate(auth_header: str, cfg: RelayConfig) -> Optional[str]:
    """Validate a Bearer Authorization header for the wrapper only."""
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None
    return authenticate_token(auth_header[7:].strip(), cfg)


def verify_password(password: str, expected: str) -> bool:
    """Constant-time password check. Fails closed if no password is configured."""
    if not expected or not password:
        return False
    return hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8"))


def make_session_token(
    secret: str,
    ttl: int,
    *,
    now: float | None = None,
    jti: str | None = None,
) -> tuple[str, int]:
    """Sign a session token: base64(payload).base64(HMAC(payload)). Returns
    (token, exp_epoch)."""
    exp = int(time.time() if now is None else now) + ttl
    token_id = jti or secrets.token_urlsafe(24)
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp, "jti": token_id}, separators=(",", ":")).encode()
    ).decode()
    sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    ).decode()
    return f"{payload}.{sig}", exp


def session_token_claims(token: str, secret: str) -> Optional[SessionClaims]:
    """Return signed claims, or ``None`` for a malformed/forged token.

    Expired but otherwise authentic tokens still return claims. Callers decide
    whether to reject them against their own clock and server-side registry.
    """
    if not token or not secret:
        return None
    try:
        payload, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    ).decode()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
        expiry = data.get("exp") if isinstance(data, dict) else None
        jti = data.get("jti") if isinstance(data, dict) else None
        if isinstance(expiry, bool) or not isinstance(expiry, int):
            return None
        if not isinstance(jti, str) or not (16 <= len(jti) <= 128):
            return None
        return SessionClaims(expires_at=expiry, jti=jti)
    except Exception:
        return None


def session_token_expiry(token: str, secret: str) -> Optional[int]:
    """Return a signed token's expiry, or ``None`` when claims are invalid."""
    claims = session_token_claims(token, secret)
    return claims.expires_at if claims is not None else None


def verify_session_token(token: str, secret: str, *, now: float | None = None) -> bool:
    """Verify the HMAC signature and require the expiry to be in the future."""
    expiry = session_token_expiry(token, secret)
    current = time.time() if now is None else now
    return expiry is not None and expiry > current
