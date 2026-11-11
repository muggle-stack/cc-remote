"""Server-wide maintenance, authorized separately from device routing."""
from __future__ import annotations

import asyncio
import os
import time

from cc_remote.installation_updates import InstallationUpdates, release_catalog
from cc_remote.update import UpdateError, host_platform, read_installation, release_repository
from cc_remote.update_jobs import failure_reason


class ServerUpdates(InstallationUpdates):
    def __init__(self, root=None):
        super().__init__(root)
        self.inflight = 0

    async def handle(self, request: dict) -> dict:
        if self.inflight >= 8:
            return {"ok": False, "reason": "busy"}
        self.inflight += 1
        try:
            return await super().handle(request)
        finally:
            self.inflight -= 1

    async def status(self, *, check: bool = False) -> dict:
        result = {"schema": 1, "managed": False, "enabled": False, "reason": "unsupported",
                  "busy_count": 0, "latest": self.catalog, "job": None, "maintenance": False}
        if self.root is None:
            return result
        try:
            current = read_installation(self.root, *host_platform())
            if current.role != "relay":
                raise UpdateError("invalid server update installation")
            result.update(managed=True, current=current.manifest["product_version"], reason="server_setup_required")
            store = self.store()
            repository = release_repository()
            try:
                policy = store.policy()
                if policy["role"] != "relay" or policy["uid"] != os.getuid():
                    raise UpdateError("invalid server update policy")
                repository = policy["repository"]
                result.update(enabled=policy["enabled"], reason=None if policy["enabled"] else "server_setup_required")
                if policy["enabled"] and not (self.root / "ui-update.sock").exists():
                    result.update(enabled=False, reason="server_setup_required")
                result["job"] = await self._public_state(store.active())
            except FileNotFoundError:
                pass
            if check:
                async with self.check_lock:
                    if time.monotonic() - self.checked_at >= 60:
                        self.catalog = await asyncio.to_thread(release_catalog, repository)
                        self.checked_at = time.monotonic()
                result["latest"] = self.catalog
        except (OSError, ValueError) as exc:
            result.update(enabled=False, reason=failure_reason(exc))
        return result

    async def start(self):
        if self.root is not None:
            try:
                if read_installation(self.root, *host_platform()).role == "relay":
                    self.recovery_task = asyncio.create_task(self._recover_pending())
            except (OSError, ValueError):
                pass  # An unsupported installation never adopts another role's jobs.

    async def close(self):
        if self.recovery_task:
            self.recovery_task.cancel()
            await asyncio.gather(self.recovery_task, return_exceptions=True)
            self.recovery_task = None
