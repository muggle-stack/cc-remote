"""Optional HarmonyOS Push Kit channel; independent of Web Push/VAPID.

Service-account material stays on the Relay. Device tokens are private routing
credentials, never URL parameters or log fields. Only explicit session mode
sends a bounded title and navigation route to Huawei.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Mapping
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from cc_remote.log import logger
from cc_remote.relay.push import PushOutcome, _generic_payload, _session_payload

log = logger("cc_remote.relay.harmony_push")
TOKEN_RE = re.compile(r"[A-Za-z0-9_:+/=-]{32,4096}\Z")
AUDIENCE = "https://oauth-login.cloud.huawei.com/oauth2/v3/token"


@dataclass(frozen=True)
class HarmonySubscription:
    subject: str
    machine_id: str
    token: str = field(repr=False)
    session_jti: str = field(repr=False)
    expires_at: float
    notification_mode: str = "generic"
    binding_id: str = field(default_factory=lambda: secrets.token_hex(16))


class HarmonyPushStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        schema = """CREATE TABLE IF NOT EXISTS harmony_push_subscriptions (
            token TEXT NOT NULL, subject TEXT NOT NULL,
            machine_id TEXT NOT NULL, session_jti TEXT NOT NULL,
            expires_at REAL NOT NULL, notification_mode TEXT NOT NULL,
            binding_id TEXT NOT NULL, PRIMARY KEY (token, machine_id))"""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(schema)
            primary_key = [row[1] for row in db.execute(
                "PRAGMA table_info(harmony_push_subscriptions)") if row[5]]
            if primary_key == ["token"]:
                db.execute("ALTER TABLE harmony_push_subscriptions RENAME TO harmony_push_legacy")
                db.execute(schema)
                db.execute("INSERT INTO harmony_push_subscriptions SELECT * FROM harmony_push_legacy")
                db.execute("DROP TABLE harmony_push_legacy")
            db.execute("CREATE INDEX IF NOT EXISTS harmony_push_machine "
                       "ON harmony_push_subscriptions(machine_id)")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    async def upsert(self, sub: HarmonySubscription, machine_ids: list[str] | None = None) -> None:
        def write() -> None:
            with self._connect() as db:
                db.execute("DELETE FROM harmony_push_subscriptions WHERE expires_at<=?",
                           (time.time(),))
                # Bound tokens per login, independently of their device count.
                count = db.execute("SELECT count(DISTINCT token) FROM harmony_push_subscriptions "
                                   "WHERE session_jti=? AND token<>?",
                                   (sub.session_jti, sub.token)).fetchone()[0]
                if count >= 32:
                    raise ValueError("native push subscription limit")
                db.execute("DELETE FROM harmony_push_subscriptions WHERE token=?", (sub.token,))
                db.executemany("INSERT INTO harmony_push_subscriptions VALUES (?, ?, ?, ?, ?, ?, ?)",
                               [(sub.token, sub.subject, machine, sub.session_jti,
                                 sub.expires_at, sub.notification_mode, sub.binding_id)
                                for machine in (machine_ids or [sub.machine_id])])
        await asyncio.to_thread(write)

    async def remove_session(self, jti: str) -> None:
        await self._delete("session_jti=?", (jti,))

    async def remove_token(self, subject: str, token: str) -> None:
        await self._delete("subject=? AND token=?", (subject, token))

    async def remove_binding(self, sub: HarmonySubscription) -> None:
        await self._delete("token=? AND machine_id=? AND binding_id=?",
                           (sub.token, sub.machine_id, sub.binding_id))

    async def remove_machine(self, subject: str, machine_id: str) -> None:
        await self._delete("subject=? AND machine_id=?", (subject, machine_id))

    async def _delete(self, where: str, values: tuple) -> None:
        def write() -> None:
            with self._connect() as db:
                db.execute(f"DELETE FROM harmony_push_subscriptions WHERE {where}", values)
        await asyncio.to_thread(write)

    async def for_machine(self, machine_id: str) -> list[HarmonySubscription]:
        def read() -> list[HarmonySubscription]:
            with self._connect() as db:
                db.execute("DELETE FROM harmony_push_subscriptions WHERE expires_at<=?",
                           (time.time(),))
                return [HarmonySubscription(
                    token=r[0], subject=r[1], machine_id=r[2], session_jti=r[3],
                    expires_at=r[4], notification_mode=r[5], binding_id=r[6],
                ) for r in db.execute("SELECT * FROM harmony_push_subscriptions "
                                      "WHERE machine_id=?", (machine_id,)).fetchall()]
        return await asyncio.to_thread(read)

    async def current(self, sub: HarmonySubscription) -> bool:
        def read() -> bool:
            with self._connect() as db:
                return db.execute("SELECT 1 FROM harmony_push_subscriptions "
                                  "WHERE token=? AND machine_id=? AND binding_id=? AND expires_at>?",
                                  (sub.token, sub.machine_id, sub.binding_id, time.time())).fetchone() is not None
        return await asyncio.to_thread(read)


class HuaweiPushSender:
    """PS256 service-account JWT and fixed Huawei v3 endpoint, no redirects."""

    def __init__(self, credential_path: str) -> None:
        try:
            path = Path(credential_path).expanduser()
            if path.stat().st_size > 65536:
                raise ValueError("oversized credential")
            value = json.loads(path.read_text())
            self.project_id = value["project_id"]
            self.key_id = value["key_id"]
            self.issuer = value["sub_account"]
            if not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.@-]{1,256}", v)
                       for v in (self.project_id, self.key_id, self.issuer)):
                raise ValueError("invalid identity")
            key = serialization.load_pem_private_key(
                value["private_key"].encode(), password=None)
            if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
                raise ValueError("invalid key")
            self.key = key
        except Exception:
            # Do not propagate parser errors containing private JSON or paths.
            raise ValueError("Invalid Harmony Push service-account file") from None
        self._jwt = ""
        self._expires = 0.0

    def authorization(self) -> str:
        now = int(time.time())
        if now < self._expires - 60:
            return self._jwt

        def encode(value: object) -> bytes:
            return base64.urlsafe_b64encode(json.dumps(
                value, separators=(",", ":")).encode()).rstrip(b"=")

        header = encode({"kid": self.key_id, "typ": "JWT", "alg": "PS256"})
        payload = encode({"aud": AUDIENCE, "iss": self.issuer,
                          "iat": now, "exp": now + 3600})
        message = header + b"." + payload
        signature = self.key.sign(message, padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
        self._jwt = (message + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()
        self._expires = now + 3600
        return self._jwt

    async def __call__(self, payload: dict[str, object]) -> str:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(
                f"https://push-api.cloud.huawei.com/v3/{self.project_id}/messages:send",
                headers={"Authorization": f"Bearer {self.authorization()}", "push-type": "0"},
                json=payload,
            )
        # HTTP success alone is not delivery acceptance. Never log provider body.
        if len(response.content) > 65536:
            return "response_invalid"
        try:
            code = response.json().get("code")
        except (ValueError, AttributeError):
            return "response_invalid"
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{8}", code):
            return "response_invalid"
        if code == "80000000" and response.status_code != 200:
            return "response_invalid"
        return code


class HarmonyPushDispatcher:
    def __init__(self, store: HarmonyPushStore, *, origin: str,
                 sender: Callable[[dict[str, object]], Awaitable[str]],
                 session_active: Callable[[HarmonySubscription], Awaitable[bool]],
                 category: str = "MARKETING", test_message: bool = False) -> None:
        self.store = store
        self.origin = origin.rstrip("/")
        self.sender = sender
        self.session_active = session_active
        self.category = category
        self.test_message = test_message
        self._slots = asyncio.Semaphore(8)

    async def notify_turn_end(self, machine_id: str, *, outcome: PushOutcome,
                              context: Mapping[str, object] | None = None) -> None:
        await self._notify(machine_id, outcome=outcome, context=context)

    async def notify_question(self, machine_id: str, *,
                              context: Mapping[str, object] | None = None) -> None:
        await self._notify(machine_id, outcome="question", context=context)

    async def _notify(self, machine_id: str, *, outcome: PushOutcome,
                      context: Mapping[str, object] | None = None) -> None:
        subs = await self.store.for_machine(machine_id)
        generic = _generic_payload(outcome, "")
        session = _session_payload(machine_id, outcome, "", context)
        results = await asyncio.gather(*(
            self._deliver(sub, session if sub.notification_mode == "session"
                          and session else generic, foreground_show=outcome == "question")
            for sub in subs), return_exceptions=True)
        if any(isinstance(r, Exception) for r in results):
            log.warning("Harmony push transport failed", failures=sum(
                isinstance(r, Exception) for r in results))

    async def _deliver(self, sub: HarmonySubscription, content: dict[str, object],
                       *, foreground_show: bool = False) -> None:
        async with self._slots:
            if not await self.session_active(sub):
                await self.store.remove_binding(sub)
                return
            if not await self.store.current(sub):
                return
            action: dict[str, object] = {"actionType": 0}
            route = content.get("route")
            if route:
                action = {"actionType": 1, "uri": "cc-remote-app://notification?" + urlencode({
                    "origin": self.origin,
                    "route": json.dumps(route, separators=(",", ":")),
                })}
            payload: dict[str, object] = {
                "payload": {"notification": {
                    "category": self.category, "title": content["title"],
                    "body": content["body"], "clickAction": action,
                    "notifyId": secrets.randbelow(2**31 - 1) + 1,
                    # Questions use the system notification in both foreground
                    # and background; Web never duplicates it with a toast.
                    "foregroundShow": foreground_show,
                }},
                "target": {"token": [sub.token]},
                "pushOptions": {"testMessage": self.test_message, "ttl": 300},
            }
            code = await self.sender(payload)
            if code == "80300007":
                # Only this exact version; a stale response cannot erase a rebind.
                await self.store.remove_binding(sub)
            elif code != "80000000":
                log.warning("Harmony push rejected", provider_code=code)
