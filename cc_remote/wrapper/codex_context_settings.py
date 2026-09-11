"""Session-owned native compaction preferences and model catalog bounds."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path


# Codex 0.154.0 ModelInfo::auto_compact_token_limit caps the trigger at
# floor(raw context window * 9 / 10), independently of usable_context_window.
_NATIVE_COMPACT_PERCENT = 90
_COMPACT_TARGET_PERCENT = 95


@dataclass(frozen=True)
class ContextBounds:
    default_window: int
    max_window: int
    effective_percent: int

    def effective_window(self, window: int) -> int:
        return min(window, self.max_window) * self.effective_percent // 100

    def compact_limit(self, window: int) -> int:
        # Target 95% of usable capacity, subject to Codex's separate raw-window
        # ceiling. With a 95% usable window the native ceiling is ~94.7% of it.
        return max(1, min(
            self.effective_window(window) * _COMPACT_TARGET_PERCENT // 100,
            min(window, self.max_window) * _NATIVE_COMPACT_PERCENT // 100,
        ))

    @property
    def limit(self) -> int:
        return self.effective_window(self.max_window)

    def window_for(self, max_tokens: int) -> int:
        if not 1 <= max_tokens <= self.limit:
            raise ValueError(f"上下文上限必须在 1–{self.limit:,} tokens 之间")
        return (max_tokens * 100 + self.effective_percent - 1) // self.effective_percent


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
        self.max_tokens: int | None = None
        self.threshold: int | None = None
        self.window: int | None = None
        self.applied_threshold: int | None = None
        self.applied_window: int | None = None
        self.applied_effective_window: int | None = None
        self.applied_model: str | None = None
        self.pending = False
        self.apply_attempted = False
        self.error: str | None = None
        self.lock = asyncio.Lock()

    @property
    def needs_apply(self) -> bool:
        # Pending describes confirmation, not permission to detach again.
        return self.pending and not self.apply_attempted

    def mark_attempted(self) -> None:
        """Consume the automatic attempt without claiming native acceptance."""
        self.apply_attempted = True
        if self.pending:
            self.error = "设置已保存，但尚未确认原生会话已应用。可重新应用以重试。"

    async def confirm_applied(self, handle) -> None:
        """Keep capacity from the accepted configuration separate from old usage.

        Resume can replay a token sample from before the reload. Its token count
        remains useful, but its modelContextWindow no longer describes this
        configuration. A subsequent native usage notification takes precedence.
        """
        self.applied_window = self.window
        self.applied_threshold = (
            min(self.threshold, self.window * _NATIVE_COMPACT_PERCENT // 100)
            if self.threshold is not None and self.window is not None else None)
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
        if self.applied_threshold is not None:
            self.applied_threshold = min(self.applied_threshold, bounds.compact_limit(window))

    def restore(self, max_tokens: int | None, window: int | None, selected: bool = False) -> None:
        self.max_tokens, self.window = max_tokens, window
        # A persisted raw window may belong to the former threshold setting.
        # Recalculate against this account's model before submitting any config.
        self.threshold = None
        self.pending = selected or max_tokens is not None
        self.apply_attempted = False

    def config(self) -> dict:
        if self.max_tokens is None or self.threshold is None or self.window is None:
            return {}
        return {"model_auto_compact_token_limit": self.threshold,
                "model_context_window": self.window,
                "model_auto_compact_token_limit_scope": "total"}

    async def validated_config(self, handle) -> dict:
        if self.max_tokens is None:
            return {}
        bounds = await asyncio.to_thread(model_context_bounds, handle.model, handle.codex_home)
        if bounds is None:
            self.pending = True
            self.error = "尚未读取到此模型的上下文上限，请刷新模型目录后重试"
            return {}
        try:
            window = bounds.window_for(self.max_tokens)
        except ValueError as exc:
            self.pending, self.error = True, str(exc)
            return {}
        threshold = bounds.compact_limit(window)
        if (self.window, self.threshold) != (window, threshold):
            # Preserve the number the user entered, now explicitly a usable
            # capacity. Lowering it must also lower the window at safe reload.
            self.window, self.threshold = window, threshold
            self.pending = True
            self.apply_attempted = False
        return self.config()

    async def select(self, handle, max_tokens: int | None) -> None:
        bounds = await asyncio.to_thread(
            model_context_bounds, handle.model, handle.codex_home)
        if max_tokens is not None and bounds is None:
            raise ValueError("尚未读取到此模型的上下文上限，请刷新模型目录后重试")
        window = bounds.window_for(max_tokens) if max_tokens is not None else None
        self.max_tokens, self.window = max_tokens, window
        self.threshold = bounds.compact_limit(window) if window is not None else None
        self.pending = True
        self.apply_attempted = False
        self.error = None

    async def apply(self, handle) -> bool:
        """Resubscribe with config and let native guards reload an idle thread.

        A subscribed thread silently ignores resume config (verified on 0.153.4).
        Shared resume has no request-bound context configuration receipt, so
        even an apparent reload must leave the change pending. Only a connect
        path that can prove it accepted these settings may clear pending.
        Each selection gets one automatic attempt; an unconfirmable or failed
        attempt must not repeatedly detach at idle/query boundaries.
        """
        async with self.lock:
            if not self.needs_apply or handle.turn_active or handle.turn_start_pending:
                return False
            sid = handle.thread_id
            if not sid or handle.work_mode or handle._ephemeral_thread_id:
                self.error = "此会话暂不支持重新加载上下文配置"
                return False
            if handle._pending_server_request_ids:
                self.error = "等待当前交互完成后应用"
                return False
            if isinstance(handle.last_goal, dict) and handle.last_goal.get("status") == "active":
                self.error = "目标仍在自动运行，待目标暂停或结束后应用"
                return False
            if self.max_tokens is not None and not await self.validated_config(handle):
                return False
            # Check official activity immediately before detaching. Reading
            # state creates no model turn and never downloads history.
            result = await handle._request("thread/read", {"threadId": sid, "includeTurns": False})
            status = result.get("thread", {}).get("status", {})
            if status.get("type") != "idle":
                return False
            # Once detachment starts, even a lost response is an uncertain
            # result. Keep pending, but require an explicit save to retry.
            self.mark_attempted()
            try:
                await handle._request("thread/unsubscribe", {"threadId": sid})
            except Exception:
                # A timed-out unsubscribe may already have detached us while
                # leaving the transport open. Restore event delivery once;
                # subsequent turns must not use an unsubscribed connection.
                await handle.force_reconnect(sid, reason="restore context subscription")
                raise
            # Native may replace an idle cache entry when no subscribers remain,
            # but status notifications cannot distinguish our reload from a
            # competing client's. Reconnect must independently confirm config.
            await handle.force_reconnect(sid, reason="session context settings")
            if not self.pending:
                self.error = None
                return True
            return False
