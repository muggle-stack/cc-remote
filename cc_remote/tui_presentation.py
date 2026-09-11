"""Terminal presentation of the same public state consumed by Web.

No inferred completion, model RPC, or transcript replay side effects belong
here. Native identities/timestamps are used wherever the protocol has them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import math
import time

from cc_remote.tui import _safe_remote_text

LIMIT = 64 * 1024
TERMINAL = {"completed", "failed", "declined", "cancelled", "interrupted"}
SETTING_TYPES = {
    "model",
    "effort",
    "auto_compact",
    "codex_context",
    "fast",
    "collaboration_mode",
    "perm",
    "permission_profile",
    "web_search",
}


def bounded(value):
    """Never retain/render binary bodies or terminal escape sequences."""
    if isinstance(value, str):
        return _safe_remote_text(value[:LIMIT]) + (
            "\n[Display truncated; open Web for the full artifact.]"
            if len(value) > LIMIT
            else ""
        )
    if isinstance(value, dict):
        return {
            k: (
                "[binary attachment; open in Web]"
                if k in {"data", "data_url"}
                else bounded(v)
            )
            for k, v in list(value.items())[:256]
        }
    if isinstance(value, list):
        return [bounded(v) for v in value[:512]]
    return value


def describe(value) -> str:
    if not value:
        return "No data yet."
    return json.dumps(bounded(value), ensure_ascii=False, indent=2)


def duration(ms: float | None) -> str:
    if ms is None:
        return "duration unavailable"
    seconds = max(0, int(ms / 1000))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return (
        (f"{hours}h " if hours else "")
        + (f"{minutes}m " if minutes else "")
        + f"{seconds}s"
    )


def tokens(value: int | None) -> str:
    if value is None:
        return "—"
    for base, unit in ((10**12, "兆"), (10**8, "亿"), (10**4, "万")):
        if value >= base:
            return f"{value / base:.2f}".rstrip("0").rstrip(".") + unit
    return str(value)


def bar(percent: float | None, width: int = 10) -> str:
    if percent is None or not math.isfinite(percent):
        return "[" + "?" * width + "]"
    filled = round(max(0, min(100, percent)) * width / 100)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def quota_windows(
    rates: dict[str, dict], engine: str, now: float
) -> list[tuple[str, dict | None]]:
    """Mirror Web's account bucket selection; never mix model quotas into it."""
    limits = []
    for key, source in rates.items():
        windows = [source.get(name) for name in ("primary", "secondary")]
        windows = [
            w
            for w in windows
            if w and (w.get("resets_at") is None or w["resets_at"] > now)
        ]
        if windows:
            identity = source.get("limit_id") or (
                None if key == "default" else key
            )
            limits.append((identity, windows))
    account = next((windows for key, windows in limits if key == engine), None)
    if account is None:
        legacy = [
            windows
            for key, windows in limits
            if key is None
            and any(
                w.get("window_duration_mins") in {300, 10080} for w in windows
            )
        ]
        account = next(
            (
                windows
                for windows in legacy
                if {w.get("window_duration_mins") for w in windows}
                >= {300, 10080}
            ),
            next(iter(legacy), []),
        )
    five = next(
        (w for w in account if w.get("window_duration_mins") == 300), None
    )
    week = next(
        (w for w in account if w.get("window_duration_mins") == 10080), None
    )
    if five is None and week is None and account:
        result = [("Overall", account[0])]
    else:
        result = [("5h", five), ("Week", week)]
    if engine == "claude":
        for key, windows in limits:
            if key in {"claude-seven-day-opus", "claude-seven-day-sonnet"}:
                result.append((key.rsplit("-", 1)[1].title(), windows[0]))
    return result


def merge_rate_window(
    current: dict, update: dict, *, replace: bool = False
) -> dict:
    """Match Web's sparse-safe quota periods and one-minute reset jitter."""
    if replace:
        return dict(update)
    old_duration, new_duration = (
        current.get("window_duration_mins"),
        update.get("window_duration_mins"),
    )
    if (
        old_duration is not None
        and new_duration is not None
        and old_duration != new_duration
    ):
        return dict(update)
    old_reset, new_reset = current.get("resets_at"), update.get("resets_at")
    if (
        old_reset is not None
        and new_reset is not None
        and new_reset < old_reset - 60
    ):
        return current
    new_period = (
        old_reset is not None
        and new_reset is not None
        and new_reset > old_reset + 60
    )
    result = dict(current)
    used = update.get("used_percent")
    if new_period and used is None:
        result.pop("used_percent", None)
    elif used is not None and (
        new_period
        or current.get("used_percent") is None
        or used >= current["used_percent"]
    ):
        result["used_percent"] = used
    if new_reset is not None:
        result["resets_at"] = (
            new_reset
            if old_reset is None or new_period
            else max(old_reset, new_reset)
        )
    if new_duration is not None:
        result["window_duration_mins"] = new_duration
    return result


@dataclass
class TurnDisplay:
    fork_id: str | None = None
    checkpoint_id: str | None = None
    started: float | None = None
    ended: float | None = None
    duration_ms: float | None = None
    status: str = "running"
    activity: str = "Processing"
    seq: int = 0

    def label(self, now: float | None = None) -> str:
        elapsed = self.duration_ms
        if elapsed is None and self.started is not None:
            end = self.ended if self.ended is not None else (now or time.time())
            if self.status == "running" or self.ended is not None:
                elapsed = (end - self.started) * 1000
        title = self.activity if self.status == "running" else self.status
        return f"{title} · {duration(elapsed)}"


@dataclass
class SessionPresentation:
    engine: str = "codex"
    selected_turn: str = ""
    turns: dict[str, TurnDisplay] = field(default_factory=dict)
    active: str = ""
    plan: dict | None = None
    retired_plans: set[str] = field(default_factory=set)
    goal: dict | None = None
    goal_id: str | None = None
    goal_dismissed: bool = False
    retired_goal: str | None = None
    completion: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    status: dict = field(default_factory=dict)
    rates: dict[str, dict] = field(default_factory=dict)
    live_rate_revision: int = 0
    reports: dict[str, dict] = field(default_factory=dict)
    background: list[dict] = field(default_factory=list)
    notices: dict[str, dict] = field(default_factory=dict)
    questions: dict[str, dict] = field(default_factory=dict)
    control: dict = field(default_factory=dict)

    def bind(self, old: str, new: str) -> None:
        if old == new:
            return
        if old in self.turns:
            original = self.turns.pop(old)
            self.turns.setdefault(new, original)
        if self.active == old:
            self.active = new
        if self.selected_turn == old:
            self.selected_turn = new
        if self.plan and self.plan.get("turn_id") == old:
            self.plan["turn_id"] = new

    def event(self, e: dict, tid: str) -> None:
        kind = e.get("type")
        seq = e.get("seq") or 0
        ts = e.get("ts")
        known_turn = tid in self.turns
        if tid and kind in {
            "user_msg",
            "assistant_msg_start",
            "delta",
            "tool_use",
            "tool_result",
            "process",
            "turn_plan",
        }:
            turn = self.turns.setdefault(tid, TurnDisplay())
            turn.seq = max(turn.seq, seq)
            if turn.started is None and isinstance(ts, (int, float)):
                turn.started = ts
        else:
            turn = self.turns.get(tid)
        if kind == "user_msg":
            if (
                self.plan
                and self.plan.get("turn_id") != tid
                and (self.plan_terminal() or self.plan.get("done"))
            ):
                self.retired_plans.add(self.plan.get("turn_id", ""))
                self.plan = None
            completed_at = (self.goal or {}).get("updatedAt")
            if (
                self.goal
                and self.goal.get("status") == "complete"
                and (
                    (
                        completed_at is not None
                        and ts is not None
                        and ts > completed_at
                    )
                    or (completed_at is None and not known_turn)
                )
            ):
                self.retired_goal = self.goal_id
            self.active = tid
        elif kind in {"assistant_msg_start", "delta", "tool_use", "process"}:
            if turn and turn.status == "running" and not e.get("background"):
                if not self.active:
                    self.active = tid
                title = {"thinking": "Thinking", "final": "Answering"}.get(
                    e.get("channel"), "Processing"
                )
                turn.activity = str(e.get("title") or e.get("tool") or title)
        elif kind == "state" and turn and turn.status == "running":
            turn.activity = str(
                e.get("detail") or e.get("phase") or "Processing"
            )
        elif kind == "turn_end" and turn:
            turn.fork_id = e.get("turn_id") or turn.fork_id
            turn.checkpoint_id = e.get("checkpoint_id") or turn.checkpoint_id
            result = e.get("result") or {}
            subtype = result.get("subtype", "")
            turn.status = (
                "interrupted"
                if "interrupt" in subtype or subtype == "error_during_execution"
                else "failed"
                if result.get("is_error")
                else "completed"
            )
            turn.duration_ms = result.get("duration_ms")
            turn.ended = ts
            if (
                self.plan
                and self.plan.get("turn_id") == tid
                and subtype != "steered"
            ):
                self.plan.update(done=True, status=turn.status)
        elif kind == "turn_plan":
            if tid not in self.retired_plans:
                self.plan = {
                    **bounded(e),
                    "turn_id": tid,
                    "done": bool(turn and turn.status != "running"),
                }
        elif kind == "goal_state":
            self.goal = bounded(e.get("goal"))
            self.goal_id = e.get("goal_id")
            self.goal_dismissed = e.get("dismissed", False)
            if not self.goal or self.goal.get("status") != "complete":
                self.retired_goal = None
        elif kind == "completion_state":
            if e.get("revision", 0) >= self.completion.get("revision", -1):
                self.completion = bounded(e)
        elif kind in SETTING_TYPES:
            self.settings[kind] = bounded(
                {
                    k: v
                    for k, v in e.items()
                    if k not in {"v", "type", "sid", "seq", "ts"}
                }
            )
        elif kind == "context_report":
            self.context = bounded(e)
        elif kind == "status_report":
            self.status = bounded(e)
            # A status read may have started before a newer rolling update.
            # Only bootstrap rates here; live sparse updates remain authoritative.
            if e.get("_rates_current", not self.live_rate_revision):
                self.rates = {
                    r.get("limit_id") or "default": bounded(r)
                    for r in e.get("rate_limits", [])
                }
        elif kind == "rate_limit_update":
            self.live_rate_revision += 1
            key = e.get("limit_id") or "default"
            if not e.get("limit_id") and len(self.rates) == 1:
                key = next(iter(self.rates))
            previous = self.rates.setdefault(key, {})
            replace = (
                bool(
                    previous.get("reached_type")
                    or previous.get("rate_limit_reached_type")
                )
                and e.get("reached_type") == ""
            )
            for name, value in e.items():
                if value is not None and name in {
                    "name",
                    "plan_type",
                    "reached_type",
                    "primary",
                    "secondary",
                }:
                    if isinstance(value, dict):
                        previous[name] = merge_rate_window(
                            previous.get(name) or {}, value, replace=replace
                        )
                    else:
                        previous[name] = value
            self.rates = dict(list(self.rates.items())[-16:])
        elif kind == "background_process_sync":
            self.background = bounded(e.get("items", []))
        elif kind == "notice":
            self.notices[e["notice_id"]] = bounded(e)
            self.notices = dict(list(self.notices.items())[-16:])
        elif kind == "ask_user_sync":
            self.questions.clear()
        elif kind == "ask_user":
            self.questions[e["ask_id"]] = bounded(e)
        elif kind == "ask_user_closed":
            self.questions.pop(e["ask_id"], None)
        self.turns = dict(list(self.turns.items())[-160:])
        if len(self.retired_plans) > 160:
            self.retired_plans.intersection_update(self.turns)

    def plan_terminal(self) -> bool:
        if not self.plan:
            return False
        steps = self.plan.get("plan", [])
        return (
            bool(steps)
            and all(s.get("status") == "completed" for s in steps)
            or self.plan.get("status") in TERMINAL
        )

    def visible_goal(self) -> dict | None:
        if self.goal_dismissed or (
            self.goal_id and self.goal_id == self.retired_goal
        ):
            return None
        return self.goal

    def progress_label(self, shortcut: str = "Space g") -> str:
        goal = self.visible_goal()
        if goal:
            return f"Goal: {goal.get('status')} · {goal.get('objective', '')[:90]}  [{shortcut}: details]"
        if self.plan:
            steps = self.plan.get("plan", [])
            completed = sum(s.get("status") == "completed" for s in steps)
            state = self.plan.get("status", "")
            if (
                self.plan.get("done")
                and completed < len(steps)
                and state not in {"failed", "interrupted"}
            ):
                state = "turn ended; steps not updated"
            return f"Plan {completed}/{len(steps)} · {state}  [{shortcut}: details]"
        return ""

    def usage_label(self) -> str:
        c = self.context or self.status.get("context", {})
        percentage = (
            None if c.get("available") is False else c.get("percentage")
        )
        parts = [
            f"Context {bar(percentage)} {percentage:.0f}% used"
            if isinstance(percentage, (int, float))
            and math.isfinite(percentage)
            else "Context unavailable"
        ]
        for label, window in quota_windows(
            self.rates, self.engine, time.time()
        ):
            used = (window or {}).get("used_percent")
            if not isinstance(used, (int, float)) or not math.isfinite(used):
                continue
            remaining = max(0, min(100, 100 - used))
            parts.append(f"{label} {bar(remaining)} {remaining:.0f}% remaining")
        return " · ".join(parts)

    def settings_label(self) -> str:
        parts = []
        for kind, key in (
            ("model", "model"),
            ("effort", "effort"),
            ("perm", "mode"),
            ("permission_profile", "profile"),
            ("web_search", "mode"),
            ("collaboration_mode", "mode"),
        ):
            value = self.settings.get(kind, {}).get(key)
            if value:
                parts.append(str(value))
        if "fast" in self.settings:
            parts.append(
                "Fast" if self.settings["fast"].get("on") else "Standard"
            )
        return " · ".join(parts)

    def panel(self, name: str) -> str:
        from cc_remote.tui_details import details

        if name == "Goal / Plan":
            g = self.goal
            parts = []
            if g:
                parts += [
                    f"Goal — {g.get('status')}",
                    g.get("objective", ""),
                    f"Tokens: {tokens(g.get('tokensUsed'))} / {tokens(g.get('tokenBudget'))}",
                    f"Elapsed: {duration(g.get('timeUsedSeconds', 0) * 1000)}",
                    "Hidden on other surfaces" if self.goal_dismissed else "",
                ]
                for key in ("iterations", "lastReason"):
                    if g.get(key) is not None:
                        parts.append(f"{key}: {g[key]}")
            if self.plan:
                parts += ["\nPlan", self.plan.get("explanation") or ""]
                for entry in self.plan.get("plan", []):
                    mark = {"completed": "✓", "inProgress": "→"}.get(
                        entry.get("status"), "○"
                    )
                    parts.append(f"{mark} {entry.get('step')}")
                if self.plan.get("done"):
                    parts.append(
                        "Turn ended; uncompleted steps are not assumed complete."
                    )
            return "\n".join(parts) or "No Goal or Plan."
        if name == "Usage / Context":
            parts = [
                self.usage_label(),
                "\nContext",
                details(self.context or self.status.get("context")),
                "\nCodex context settings",
                details(self.settings.get("codex_context")),
                "\nQuota (consumed, not remaining)",
                details(self.rates),
            ]
            usage = self.status.get("usage") or {}
            if usage:
                parts += [
                    f"\nLifetime: {tokens(usage.get('lifetime_tokens'))} Tokens"
                ]
                buckets = usage.get("daily_usage_buckets", [])
                peak = max((b.get("tokens", 0) for b in buckets), default=0)
                for b in buckets:
                    value = b.get("tokens", 0)
                    parts.append(
                        f"{b.get('start_date')} {bar(value / peak * 100 if peak else 0)} {tokens(value)} Tokens"
                    )
            return "\n".join(parts)
        if name == "Settings":
            return (
                details(self.settings)
                + "\n\nControl\n"
                + details(self.control)
                + "\n\nAvailable models\n"
                + details(self.reports.get("models"))
                + "\n\nPermission profiles\n"
                + details(self.reports.get("permission_profiles"))
            )
        if name == "Background":
            return details(self.background)
        if name == "Notices":
            return details(list(self.notices.values()))
        if name == "Questions":
            return details(list(self.questions.values()))
        return details(self.status if name == "Status" else self.reports)


def timestamp(value: float | None) -> str:
    if value is None:
        return ""
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return ""
