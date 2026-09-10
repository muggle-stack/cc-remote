"""Session-owned native compaction preferences and model catalog bounds."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path


@dataclass(frozen=True)
class ContextBounds:
    default_window: int
    max_window: int
    effective_percent: int

    @property
    def limit(self) -> int:
        return self.max_window * self.effective_percent // 100

    def window_for(self, threshold: int) -> int:
        if not 1 <= threshold <= self.limit:
            raise ValueError(f"压缩阈值必须在 1–{self.limit:,} tokens 之间")
        return max(self.default_window, math.ceil(threshold * 100 / self.effective_percent))


def model_context_bounds(model: str, codex_home: str | None) -> ContextBounds | None:
    # model/list currently omits context sizes. Its official per-account cache
    # contains them; never guess a window from a model name or another account.
    home = Path(codex_home or os.environ.get("CODEX_HOME") or "~/.codex").expanduser()
    try:
        with (home / "models_cache.json").open("rb") as stream:
            data = stream.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            return None
        catalog = json.loads(data)
        for entry in catalog.get("models", [])[:256]:
            if not isinstance(entry, dict) or entry.get("slug") != model:
                continue
            default = entry.get("context_window")
            maximum = entry.get("max_context_window", default)
            percent = entry.get("effective_context_window_percent")
            if (all(type(value) is int for value in (default, maximum, percent))
                    and 1 <= default <= maximum <= 100_000_000
                    and 1 <= percent <= 100):
                return ContextBounds(default, maximum, percent)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


class NativeContextSettings:
    def __init__(self):
        self.threshold: int | None = None
        self.window: int | None = None
        self.applied_threshold: int | None = None
        self.applied_window: int | None = None
        self.applied_effective_window: int | None = None
        self.applied_model: str | None = None
        self.pending = False
        self.error: str | None = None
        self.lock = asyncio.Lock()
        self.reloaded = False

    async def confirm_applied(self, handle) -> None:
        """Keep capacity from the accepted configuration separate from old usage.

        Resume can replay a token sample from before the reload. Its token count
        remains useful, but its modelContextWindow no longer describes this
        configuration. A subsequent native usage notification takes precedence.
        """
        self.applied_threshold, self.applied_window = self.threshold, self.window
        self.pending, self.error = False, None
        self.applied_model = handle.model
        self.applied_effective_window = None
        bounds = await asyncio.to_thread(
            model_context_bounds, handle.model, handle.codex_home)
        if bounds is None:
            return
        window = self.applied_window
        if window is None:
            try:
                result = await handle._request("config/read", {
                    "cwd": handle.cwd, "includeLayers": False,
                })
                configured = result.get("config", {}).get("model_context_window")
            except Exception:
                # The setting was accepted, but its inherited capacity is
                # unavailable. Never substitute a pre-reset rollout window.
                return
            window = configured if type(configured) is int and configured > 0 else bounds.default_window
        self.applied_effective_window = (
            min(window, bounds.max_window) * bounds.effective_percent // 100)

    def restore(self, threshold: int | None, window: int | None, selected: bool = False) -> None:
        if threshold is not None and (window is None or threshold > window):
            threshold = window = None
        self.threshold, self.window = threshold, window
        self.pending = selected or threshold is not None

    def config(self) -> dict:
        if self.threshold is None:
            return {}
        return {"model_auto_compact_token_limit": self.threshold,
                "model_context_window": self.window,
                "model_auto_compact_token_limit_scope": "total"}

    async def validated_config(self, handle) -> dict:
        if self.threshold is None:
            return {}
        bounds = await asyncio.to_thread(model_context_bounds, handle.model, handle.codex_home)
        if (bounds is None or self.window is None or self.window > bounds.max_window
                or self.threshold > self.window * bounds.effective_percent // 100):
            self.pending = True
            self.error = "模型上下文上限已变化，请重新设置压缩阈值"
            return {}
        return self.config()

    async def select(self, handle, threshold: int | None) -> None:
        bounds = await asyncio.to_thread(
            model_context_bounds, handle.model, handle.codex_home)
        if threshold is not None and bounds is None:
            raise ValueError("尚未读取到此模型的上下文上限，请刷新模型目录后重试")
        window = bounds.window_for(threshold) if threshold is not None else None
        if window is not None:
            # Lowering the trigger must not also shrink the available model
            # window around already-carried history.
            observed = getattr(handle, "context_window", 0)
            observed_raw = (math.ceil(observed * 100 / bounds.effective_percent)
                            if type(observed) is int and observed > 0 else 0)
            window = max(window, min(bounds.max_window, max(self.window or 0, observed_raw)))
        self.threshold, self.window = threshold, window
        self.pending = True
        self.error = None

    async def apply(self, handle) -> bool:
        """Resubscribe with config and let native guards reload an idle thread.

        A subscribed thread silently ignores resume config (verified on 0.153.4).
        Another client retaining this thread must leave the change pending.
        """
        async with self.lock:
            if not self.pending or handle.turn_active or handle.turn_start_pending:
                return False
            sid = handle.thread_id
            if not sid or handle.work_mode or handle._ephemeral_thread_id:
                self.error = "此会话暂不支持重新加载压缩配置"
                return False
            if handle._pending_server_request_ids:
                self.error = "等待当前交互完成后应用"
                return False
            if isinstance(handle.last_goal, dict) and handle.last_goal.get("status") == "active":
                self.error = "目标仍在自动运行，待目标暂停或结束后应用"
                return False
            if self.threshold is not None:
                bounds = await asyncio.to_thread(
                    model_context_bounds, handle.model, handle.codex_home)
                if bounds is None or self.window > bounds.max_window:
                    self.error = "模型上下文上限已变化，请重新设置压缩阈值"
                    return False
                bounds.window_for(self.threshold)
            # Check official activity immediately before detaching. Reading
            # state creates no model turn and never downloads history.
            result = await handle._request("thread/read", {"threadId": sid, "includeTurns": False})
            status = result.get("thread", {}).get("status", {})
            if status.get("type") != "idle":
                return False
            await handle._request("thread/unsubscribe", {"threadId": sid})
            # Native resume atomically replaces an idle cache entry only when
            # no subscribers remain. Its notLoaded -> idle notifications prove
            # replacement; loaded/list alone cannot distinguish that case.
            await handle.force_reconnect(sid, reason="session context settings")
            if not self.pending:
                self.error = None
                return True
            self.error = "其他客户端仍连接此会话；设置已保存，待会话可重新加载时应用"
            return False
