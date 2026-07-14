"""Translate Codex app-server notifications into the remote rich-event model.

Only app-server fields that are explicitly part of its public client protocol are
forwarded.  In particular, reasoning *summary* is visible, while raw/encrypted
reasoning and terminal stdin are deliberately hidden.  A terminal-interaction
marker is still forwarded so the remote timeline does not silently omit the step.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from datetime import datetime
from itertools import islice

from cc_remote.protocol import (
    AssistantMsgStart, Delta, ToolUse, ToolDelta, ToolResult, AssistantMsgEnd,
    ProcessEvent, TurnPlan, TurnDiff, TurnEnd, TurnResult, UserMsg, Error,
    StateEvent, ERR_CC_CRASH,
)
from cc_remote.wrapper.sanitize import bounded_text, bounded_tool_input

_TOOL_TYPES = {
    "commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall",
    "webSearch",
}
_PROCESS_ITEM_TYPES = {
    "plan", "reasoning", "collabAgentToolCall", "subAgentActivity",
    "contextCompaction",
}
_MAX_HISTORY_RECORD_CHARS = 16 * 1024 * 1024
_SAFE_WIRE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_CREDENTIAL_EXACT_KEYS = frozenset({"env", "environment"})
_CREDENTIAL_KEY_FRAGMENTS = (
    "secret",
    "password",
    "passwd",
    "token",
    "authorization",
    "credential",
    "cookie",
    "accesskey",
    "privatekey",
    "apikey",
)
_REDACTED = "[REDACTED]"
_REDACTION_BUDGET_EXCEEDED = "<redaction budget exceeded>"
_REDACTION_REMAINDER_KEY = "<remaining omitted>"
_MAX_REDACTION_DEPTH = 6
_MAX_REDACTION_NODES = 2048
_MAX_REDACTION_DICT_ITEMS = 64
_MAX_REDACTION_SEQUENCE_ITEMS = 32
_EMPTY_COMPLETED_MESSAGE = (
    "Codex 回合已结束，但没有返回任何内容；上游服务可能暂时不可用，请重试。"
)
_MAX_DELTA_STREAMS = 2048
_MAX_DELTA_EVENTS_PER_STREAM = 1024
_MAX_FINISHED_DELTA_ITEMS = 4096
_MAX_LIVE_ITEMS = 4096
_LIVE_ITEMS_OMITTED_ID = "cc-remote-live-items-omitted"
_DELTA_TRUNCATION_NOTICE = "\n…（后续输出已截断）"
_MODEL_NAME_MAX_CHARS = 256
_MODEL_ENUM_MAX_CHARS = 256
_MODEL_LIST_MAX_ITEMS = 32
_MODEL_DETAIL_MAX_CHARS = 16 * 1024


def _bounded_jsonl_records(file):
    """Yield bounded complete records; skip one pathological oversized line."""
    line_no = 0
    while True:
        line = file.readline(_MAX_HISTORY_RECORD_CHARS + 1)
        if not line:
            return
        line_no += 1
        complete = line.endswith("\n") or len(line) < _MAX_HISTORY_RECORD_CHARS + 1
        if complete:
            yield line_no, line
            continue
        while line and not line.endswith("\n"):
            line = file.readline(_MAX_HISTORY_RECORD_CHARS + 1)


class CodexStreamTranslator:
    def __init__(self, tool_result_max: int):
        self.tool_result_max = tool_result_max
        self._started: set[str] = set()
        self._text_seen: set[str] = set()
        self._message_channels: dict[str, str] = {}
        self._tools_started: set[str] = set()
        self._reasoning_started: set[str] = set()
        self._file_diffs: dict[str, str] = {}
        self._open_msg: str | None = None
        self._open_channel = "unknown"
        self._visible_output = False
        self._terminal_error = False
        self._delta_chars: dict[tuple[str, str], int] = {}
        self._delta_events: dict[tuple[str, str], int] = {}
        self._truncated_delta_streams: set[tuple[str, str]] = set()
        self._finished_delta_items: set[str] = set()
        # One translator owns one turn. Keep a fixed admission set instead of
        # allowing every distinct provider id to grow several parallel maps.
        # Rejected ids are not tombstoned individually: once full, *all* new ids
        # stay rejected, so a later completed event cannot resurrect them.
        self._live_items: set[str] = set()
        self._live_items_truncated = False
        self._turn_closed = False

    def feed(self, msg: dict) -> list:
        method = msg.get("method")
        p = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        out: list = []

        if method == "item/agentMessage/delta":
            iid = _live_id(p.get("itemId"), "agent-message")
            if not self._admit_live_item(iid, out):
                return out
            channel = self._message_channels.get(iid, "unknown")
            if iid not in self._started:
                if self._open_msg is not None and self._open_msg != iid:
                    self._close_open(out)
                self._started.add(iid)
                self._open_msg = iid
                self._open_channel = channel
                out.append(AssistantMsgStart(message_id=iid, channel=channel))
            delta = p.get("delta")
            if isinstance(delta, str) and delta:
                self._text_seen.add(iid)
                self._visible_output = True
                out.append(Delta(message_id=iid, text=delta, channel=channel))

        elif method == "item/started":
            item = p.get("item") if isinstance(p.get("item"), dict) else {}
            item_type = item.get("type")
            if item_type == "agentMessage":
                iid = _live_id(item.get("id"), "agent-message")
                if not self._admit_live_item(iid, out):
                    return out
                channel = _assistant_channel(item.get("phase"))
                self._message_channels[iid] = channel
                if iid not in self._started:
                    if self._open_msg is not None and self._open_msg != iid:
                        self._close_open(out)
                    self._started.add(iid)
                    self._open_msg = iid
                    self._open_channel = channel
                    out.append(AssistantMsgStart(
                        message_id=iid, channel=channel))
            elif item_type in _TOOL_TYPES:
                self._visible_output = True
                out.extend(self._tool_use(item))
            elif item_type in _PROCESS_ITEM_TYPES:
                iid = _live_id(item.get("id"), str(item_type or "process"))
                if not self._admit_live_item(iid, out):
                    return out
                event = self._process_item(item, p, completed=False)
                if event is not None:
                    self._visible_output = True
                    out.append(event)

        elif method == "item/completed":
            item = p.get("item") if isinstance(p.get("item"), dict) else {}
            t = item.get("type")
            if t == "agentMessage":
                text = item.get("text") if isinstance(item.get("text"), str) else ""
                iid = _live_id(item.get("id") or text, "agent-message")
                if not self._admit_live_item(iid, out):
                    return out
                channel = _assistant_channel(item.get("phase"))
                if channel == "unknown":
                    channel = self._message_channels.get(iid, "unknown")
                else:
                    self._message_channels[iid] = channel
                # Some providers send only item/completed with the final text and
                # no delta notification. Preserve that answer instead of turning
                # it into a false empty-completed error.
                if text and iid not in self._text_seen:
                    if iid not in self._started:
                        if self._open_msg is not None and self._open_msg != iid:
                            self._close_open(out)
                        self._started.add(iid)
                        self._open_msg = iid
                        self._open_channel = channel
                        out.append(AssistantMsgStart(
                            message_id=iid, channel=channel))
                    self._text_seen.add(iid)
                    self._visible_output = True
                    out.append(Delta(
                        message_id=iid, text=text, channel=channel))
                if iid in self._started:
                    out.append(AssistantMsgEnd(
                        message_id=iid, channel=channel))
                    if self._open_msg == iid:
                        self._open_msg = None
                        self._open_channel = "unknown"
            elif t in _TOOL_TYPES:
                self._visible_output = True
                iid = _live_id(item.get("id"), f"{t}-tool")
                if not self._admit_live_item(iid, out):
                    return out
                if iid not in self._tools_started:
                    out.extend(self._tool_use(item))
                out.append(self._tool_result(item))
            elif t in _PROCESS_ITEM_TYPES:
                iid = _live_id(item.get("id"), str(t or "process"))
                if not self._admit_live_item(iid, out):
                    return out
                event = self._process_item(item, p, completed=True)
                if event is not None:
                    self._visible_output = True
                    out.append(event)
            if t in _TOOL_TYPES | _PROCESS_ITEM_TYPES:
                fallback = {
                    "commandExecution": "command-tool",
                    "fileChange": "fileChange-tool",
                    "mcpToolCall": "mcpToolCall-tool",
                    "dynamicToolCall": "dynamicToolCall-tool",
                    "webSearch": "webSearch-tool",
                }.get(t, str(t or "process"))
                finished_id = _live_id(item.get("id"), fallback)
                self._finish_delta_item(finished_id)
                self._file_diffs.pop(finished_id, None)

        elif method == "item/reasoning/summaryPartAdded":
            iid = _live_id(p.get("itemId"), "reasoning")
            if not self._admit_live_item(iid, out):
                return out
            event = self._ensure_reasoning(iid, p)
            if event is not None:
                self._visible_output = True
                out.append(event)

        elif method == "item/reasoning/summaryTextDelta":
            iid = _live_id(p.get("itemId"), "reasoning")
            if not self._admit_live_item(iid, out):
                return out
            event = self._ensure_reasoning(iid, p)
            if event is not None:
                out.append(event)
            delta = self._bounded_live_delta(
                iid, "reasoning-summary", p.get("delta"), 512 * 1024)
            if delta:
                self._visible_output = True
                out.append(ProcessEvent(
                    item_id=iid,
                    kind="reasoning",
                    phase="update",
                    status="running",
                    turn_id=_optional_wire_id(p.get("turnId"), "turn"),
                    title="思考",
                    append_to="summary",
                    delta=delta,
                ))

        elif method == "item/plan/delta":
            iid = _live_id(p.get("itemId"), "plan")
            if not self._admit_live_item(iid, out):
                return out
            delta = self._bounded_live_delta(
                iid, "plan-detail", p.get("delta"), 512 * 1024)
            if delta:
                self._visible_output = True
                out.append(ProcessEvent(
                    item_id=iid,
                    kind="plan",
                    phase="update",
                    status="running",
                    turn_id=_optional_wire_id(p.get("turnId"), "turn"),
                    title="计划",
                    append_to="detail",
                    delta=delta,
                ))

        elif method == "turn/plan/updated":
            turn_id = _optional_wire_id(p.get("turnId"), "turn")
            iid = _live_id(
                f"plan:{turn_id or p.get('turnId') or 'current'}", "plan")
            if not self._admit_live_item(iid, out):
                return out
            plan = []
            for entry in (p.get("plan") or [])[:128]:
                if not isinstance(entry, dict):
                    continue
                step, _ = bounded_text(entry.get("step"), 16 * 1024)
                if not step:
                    continue
                plan.append({
                    "step": step,
                    "status": _plan_status(entry.get("status")),
                })
            explanation, _ = bounded_text(p.get("explanation"), 64 * 1024)
            self._visible_output = True
            out.append(TurnPlan(
                item_id=iid,
                turn_id=turn_id,
                explanation=explanation or None,
                plan=plan,
            ))

        elif method == "item/commandExecution/outputDelta":
            iid = _live_id(p.get("itemId"), "command-tool")
            if not self._admit_live_item(iid, out):
                return out
            delta = self._bounded_live_delta(
                iid, "output", p.get("delta"),
                min(self.tool_result_max, 512 * 1024))
            if delta:
                out.append(ToolDelta(
                    tool_use_id=iid,
                    stream="output",
                    delta=delta,
                ))

        elif method == "item/fileChange/outputDelta":
            # Kept for old app-server builds; 0.144.1 marks it deprecated.
            iid = _live_id(p.get("itemId"), "fileChange-tool")
            if not self._admit_live_item(iid, out):
                return out
            delta = self._bounded_live_delta(
                iid, "output", p.get("delta"),
                min(self.tool_result_max, 512 * 1024))
            if delta:
                out.append(ToolDelta(
                    tool_use_id=iid,
                    stream="output",
                    delta=delta,
                ))

        elif method == "item/fileChange/patchUpdated":
            iid = _live_id(p.get("itemId"), "fileChange-tool")
            if not self._admit_live_item(iid, out):
                return out
            latest, _ = bounded_text(_changes_diff(p.get("changes")), 2 * 1024 * 1024)
            previous = self._file_diffs.get(iid, "")
            self._file_diffs[iid] = latest
            # patchUpdated is a snapshot. ToolDelta is append-only, so forward
            # only the genuinely-new suffix; non-monotonic rewrites are still
            # delivered authoritatively by item/completed.diff.
            if latest and latest.startswith(previous):
                delta = self._bounded_live_delta(
                    iid, "diff", latest[len(previous):], 512 * 1024)
                if delta:
                    out.append(ToolDelta(
                        tool_use_id=iid, stream="diff", delta=delta))

        elif method == "turn/diff/updated":
            turn_id = _optional_wire_id(p.get("turnId"), "turn")
            iid = _live_id(
                f"diff:{turn_id or p.get('turnId') or 'current'}", "diff")
            if not self._admit_live_item(iid, out):
                return out
            diff, truncated = bounded_text(p.get("diff"), 2 * 1024 * 1024)
            self._visible_output = True
            out.append(TurnDiff(
                item_id=iid,
                turn_id=turn_id,
                diff=diff,
                truncated=True if truncated else None,
            ))

        elif method == "item/mcpToolCall/progress":
            iid = _live_id(p.get("itemId"), "mcpToolCall-tool")
            if not self._admit_live_item(iid, out):
                return out
            progress = self._bounded_live_delta(
                iid, "progress", p.get("message"), 64 * 1024)
            if progress:
                out.append(ToolDelta(
                    tool_use_id=iid,
                    stream="progress",
                    delta=progress,
                ))

        elif method == "item/commandExecution/terminalInteraction":
            # The official payload's only interaction body is `stdin`.  It may
            # contain a password, token, or an answer to a secret prompt, so never
            # copy it to the wire.  Preserve a visible, sanitized timeline marker
            # instead of making the interaction look like a stalled command.
            command_id = _live_id(p.get("itemId"), "command-tool")
            iid = _live_id(f"{command_id}:terminal", "terminal")
            if not self._admit_live_item(iid, out):
                return out
            self._visible_output = True
            out.append(ProcessEvent(
                item_id=iid,
                kind="terminal",
                phase="snapshot",
                status="succeeded",
                turn_id=_optional_wire_id(p.get("turnId"), "turn"),
                parent_id=command_id,
                title="终端交互",
                summary="已向运行中的终端进程写入输入（内容已隐藏）",
            ))

        elif method == "model/rerouted":
            turn_id = _optional_wire_id(p.get("turnId"), "turn")
            from_model = _bounded_model_field(
                p.get("fromModel"), _MODEL_NAME_MAX_CHARS)
            to_model = _bounded_model_field(
                p.get("toModel"), _MODEL_NAME_MAX_CHARS)
            reason = _bounded_model_field(
                p.get("reason"), _MODEL_ENUM_MAX_CHARS)
            if turn_id and from_model and to_model and reason:
                iid = _live_id(
                    f"reroute:{turn_id}:{from_model}:{to_model}:{reason}",
                    "model-reroute",
                )
                if not self._admit_live_item(iid, out):
                    return out
                summary, _ = bounded_text(
                    f"{from_model} → {to_model}", 1024)
                detail, _ = bounded_text(
                    f"原因：{reason}", _MODEL_DETAIL_MAX_CHARS)
                self._visible_output = True
                out.append(ProcessEvent(
                    item_id=iid,
                    kind="model",
                    phase="snapshot",
                    status="succeeded",
                    turn_id=turn_id,
                    title="模型已重路由",
                    summary=summary,
                    detail=detail,
                ))

        elif method == "model/safetyBuffering/updated":
            turn_id = _optional_wire_id(p.get("turnId"), "turn")
            model = _bounded_model_field(
                p.get("model"), _MODEL_NAME_MAX_CHARS)
            showing = p.get("showBufferingUi")
            if turn_id and model and isinstance(showing, bool):
                # One card follows the lifecycle of one turn/model pair. Repeated
                # updates therefore merge instead of filling the timeline.
                iid = _live_id(
                    f"safety-buffering:{turn_id}:{model}",
                    "model-safety-buffering",
                )
                if not self._admit_live_item(iid, out):
                    return out
                reasons = _bounded_model_list(p.get("reasons"))
                use_cases = _bounded_model_list(p.get("useCases"))
                faster_model = _bounded_model_field(
                    p.get("fasterModel"), _MODEL_NAME_MAX_CHARS)
                detail_parts = []
                if reasons:
                    detail_parts.append("原因：" + "、".join(reasons))
                if use_cases:
                    detail_parts.append("使用场景：" + "、".join(use_cases))
                if faster_model:
                    detail_parts.append(f"可用的更快模型：{faster_model}")
                detail, _ = bounded_text(
                    "\n".join(detail_parts), _MODEL_DETAIL_MAX_CHARS)
                self._visible_output = True
                out.append(ProcessEvent(
                    item_id=iid,
                    kind="safety",
                    phase="start" if showing else "end",
                    status="running" if showing else "succeeded",
                    turn_id=turn_id,
                    title="模型安全缓冲",
                    summary=f"模型：{model}",
                    detail=detail or None,
                ))

        elif method == "model/verification":
            turn_id = _optional_wire_id(p.get("turnId"), "turn")
            verifications = _bounded_model_list(p.get("verifications"))
            if turn_id and verifications:
                iid = _live_id(
                    f"model-verification:{turn_id}", "model-verification")
                if not self._admit_live_item(iid, out):
                    return out
                summary, _ = bounded_text(
                    "、".join(verifications), _MODEL_DETAIL_MAX_CHARS)
                self._visible_output = True
                out.append(ProcessEvent(
                    item_id=iid,
                    kind="safety",
                    phase="snapshot",
                    status="succeeded",
                    turn_id=turn_id,
                    title="模型验证",
                    summary=summary,
                ))

        elif method in {"hook/started", "hook/completed"}:
            event = _hook_event(p, completed=(method == "hook/completed"))
            if event is not None and self._admit_live_item(event.item_id, out):
                self._visible_output = True
                out.append(event)

        elif method == "thread/compacted":
            turn_id = _optional_wire_id(p.get("turnId"), "turn")
            iid = _live_id(
                f"compaction:{turn_id or p.get('turnId') or 'current'}",
                "compaction")
            if not self._admit_live_item(iid, out):
                return out
            self._visible_output = True
            out.append(ProcessEvent(
                item_id=iid,
                kind="compaction",
                phase="end",
                status="succeeded",
                turn_id=turn_id,
                title="压缩上下文",
            ))

        # Raw reasoning text is intentionally ignored. Only the public summary
        # notifications above and the summary array on a completed item cross the
        # remote boundary.

        elif method == "error":
            # Retrying provider failures are progress, not terminal errors. Emit a
            # running StateEvent so old clients remain compatible while new clients
            # can replace the generic spinner with a useful status.
            err = p.get("error") if isinstance(p.get("error"), dict) else {}
            if p.get("willRetry"):
                out.append(StateEvent(
                    state="running",
                    phase="retrying",
                    detail=_retry_detail(err),
                ))
            else:
                msg = err.get("message") or "codex 出错"
                det = err.get("additionalDetails")
                message_text, _ = bounded_text(msg, 24 * 1024)
                details_text, _ = bounded_text(det, 8 * 1024)
                detail = "codex: " + message_text
                if details_text:
                    detail += " — " + details_text
                self._terminal_error = True
                out.append(Error(code=ERR_CC_CRASH, message=detail))

        elif method == "turn/completed":
            self._close_open(out)
            turn = p.get("turn") or {}
            st = turn.get("status") or "completed"
            # a failed turn carries its reason in turn.error — surface it (the
            # error notifications above may not have fired for every failure mode).
            if st == "failed":
                te = turn.get("error")
                emsg = te.get("message") if isinstance(te, dict) else (te if isinstance(te, str) else None)
                if emsg and not self._terminal_error:
                    message_text, _ = bounded_text(emsg, 32 * 1024)
                    out.append(Error(
                        code=ERR_CC_CRASH,
                        message="codex 回合失败: " + message_text,
                    ))
                    self._terminal_error = True
                elif not self._terminal_error:
                    out.append(Error(
                        code=ERR_CC_CRASH,
                        message="Codex 回合失败，但没有返回错误详情。",
                    ))
                    self._terminal_error = True
            # Codex 0.144.1 can record an upstream 503 as completed/error=null with
            # only the userMessage item. Treat that impossible "empty success" as
            # a terminal failure, while allowing tool-only turns as visible output.
            if st == "completed" and not self._visible_output:
                if not self._terminal_error:
                    out.append(Error(
                        code=ERR_CC_CRASH,
                        message=_EMPTY_COMPLETED_MESSAGE,
                    ))
                    self._terminal_error = True
                st = "failed"
            elif st == "completed" and self._terminal_error:
                st = "failed"
            # Map codex TurnStatus (completed|interrupted|failed) onto cc's wire
            # subtype vocabulary so the engine-agnostic reducer treats them right:
            # "interrupted" -> "error_during_execution" is the token the client keys
            # on to render the "— 已打断 —" note (verified: turn/interrupt yields
            # turn/completed{status:"interrupted"}).
            subtype = ("success" if st == "completed"
                       else "error_during_execution" if st == "interrupted"
                       else "error")
            completed_turn_id = turn.get("id")
            out.append(TurnEnd(result=TurnResult(
                subtype=subtype,
                duration_ms=int(turn.get("durationMs") or 0),
                is_error=(st != "completed"),
            ), turn_id=(completed_turn_id
                        if isinstance(completed_turn_id, str) else None)))
            self._clear_all_delta_budgets()
            self._turn_closed = True

        # everything else (raw reasoning, userMessage, mcpServer/startupStatus,
        # thread/status, account/rateLimits, tokenUsage, remoteControl…) -> skip.
        return out

    # ---- helpers ----
    def _admit_live_item(self, item_id: str, out: list) -> bool:
        if self._turn_closed:
            return False
        if item_id in self._live_items:
            return True
        if len(self._live_items) < _MAX_LIVE_ITEMS:
            self._live_items.add(item_id)
            return True
        if not self._live_items_truncated:
            self._live_items_truncated = True
            self._visible_output = True
            out.append(ProcessEvent(
                item_id=_LIVE_ITEMS_OMITTED_ID,
                kind="compaction",
                phase="snapshot",
                status="succeeded",
                title="较早过程已省略",
                summary="此回合的处理项目过多，后续新增项目未实时展示。",
            ))
        return False

    def _bounded_live_delta(
        self, item_id: str, stream: str, value, single_event_cap: int,
    ) -> str:
        """Bound cumulative append-only payload and append count per UI field."""
        key = (item_id, stream)
        if (item_id in self._finished_delta_items
                or key in self._truncated_delta_streams):
            return ""
        if key not in self._delta_chars and len(self._delta_chars) >= _MAX_DELTA_STREAMS:
            return ""
        budget = max(1, self.tool_result_max)
        used = self._delta_chars.get(key, 0)
        count = self._delta_events.get(key, 0)
        remaining = budget - used
        # Reserve the final allowed append for an explicit truncation marker.
        if remaining <= 0 or count >= _MAX_DELTA_EVENTS_PER_STREAM - 1:
            self._truncated_delta_streams.add(key)
            if remaining <= 0:
                return ""
            notice = _DELTA_TRUNCATION_NOTICE[-remaining:]
            self._delta_chars[key] = used + len(notice)
            self._delta_events[key] = count + 1
            return notice

        text, truncated = bounded_text(
            value, min(max(1, single_event_cap), remaining))
        if not text and not truncated:
            return ""
        if truncated:
            self._truncated_delta_streams.add(key)
            notice = _DELTA_TRUNCATION_NOTICE
            if len(notice) >= remaining:
                text = notice[-remaining:]
            else:
                text = text[:remaining - len(notice)] + notice
        self._delta_chars[key] = used + len(text)
        self._delta_events[key] = count + 1
        return text

    def _finish_delta_item(self, item_id: str) -> None:
        if len(self._finished_delta_items) < _MAX_FINISHED_DELTA_ITEMS:
            self._finished_delta_items.add(item_id)
        for key in [key for key in self._delta_chars if key[0] == item_id]:
            self._delta_chars.pop(key, None)
            self._delta_events.pop(key, None)
            self._truncated_delta_streams.discard(key)

    def _clear_all_delta_budgets(self) -> None:
        self._delta_chars.clear()
        self._delta_events.clear()
        self._truncated_delta_streams.clear()
        self._finished_delta_items.clear()

    def _close_open(self, out: list) -> None:
        if self._open_msg is None:
            return
        out.append(AssistantMsgEnd(
            message_id=self._open_msg,
            channel=self._open_channel,
        ))
        self._open_msg = None
        self._open_channel = "unknown"

    def _ensure_block(self, mid: str, out: list) -> None:
        """A tool card needs an assistant message block to hang under (the reducer
        keys tool cards by message_id); open one lazily if none is active."""
        if self._open_msg is None:
            self._open_msg = mid
            self._open_channel = "commentary"
            self._started.add(mid)
            out.append(AssistantMsgStart(
                message_id=mid, channel="commentary"))

    def _tool_use(self, item: dict) -> list:
        out: list = []
        item_type = str(item.get("type") or "tool")
        iid = _live_id(item.get("id"), f"{item_type}-tool")
        if not self._admit_live_item(iid, out):
            return out
        if iid in self._tools_started:
            return out
        self._tools_started.add(iid)
        mid = self._open_msg or iid
        self._ensure_block(mid, out)
        inp = _tool_input(item)
        tool, category, title, server = _tool_presentation(item)
        out.append(ToolUse(
            message_id=self._open_msg or "",
            tool_use_id=iid,
            tool=tool,
            input=bounded_tool_input(inp, self.tool_result_max),
            category=category,
            title=title,
            server=server,
        ))
        return out

    def _tool_result(self, item: dict) -> ToolResult:
        item_type = item.get("type")
        status = _process_status(item.get("status"))
        code = _nonnegative_or_signed_int(item.get("exitCode"))
        diff = None
        summary = None
        raw_content = item.get("aggregatedOutput") or item.get("output") or ""
        if item_type == "fileChange":
            diff, diff_truncated = bounded_text(
                _changes_diff(item.get("changes")), 2 * 1024 * 1024)
            paths = _change_paths(item.get("changes"))
            summary = _file_summary(paths, status)
            raw_content = summary
        elif item_type == "mcpToolCall":
            raw_content = _mcp_result_content(item)
            error = item.get("error") if isinstance(item.get("error"), dict) else {}
            summary, _ = bounded_text(error.get("message"), 64 * 1024)
            if summary:
                status = "failed"
        elif item_type == "dynamicToolCall":
            raw_content = _redact_credentials(item.get("contentItems") or "")
            success = item.get("success")
            if success is False:
                status = "failed"
            elif success is True:
                status = "succeeded"
        elif item_type == "webSearch":
            raw_content = {
                "query": item.get("query"),
                "action": item.get("action"),
            }
            status = "succeeded"
        text, was_truncated = bounded_text(raw_content, self.tool_result_max)
        truncated = True if was_truncated else None
        if item_type == "fileChange" and diff_truncated:
            truncated = True
        is_error = (
            status in {"failed", "declined", "cancelled", "interrupted"}
            or (code is not None and code != 0)
        )
        return ToolResult(
            tool_use_id=_live_id(item.get("id"), f"{item_type}-tool"),
            content=text,
            is_error=is_error,
            truncated=truncated,
            status=status,
            summary=summary or None,
            diff=diff or None,
            exit_code=code,
            duration_ms=_duration_ms(item.get("durationMs")),
        )

    def _ensure_reasoning(self, iid: str, params: dict):
        if iid in self._reasoning_started:
            return None
        self._reasoning_started.add(iid)
        return ProcessEvent(
            item_id=iid,
            kind="reasoning",
            phase="start",
            status="running",
            turn_id=_optional_wire_id(params.get("turnId"), "turn"),
            title="思考",
        )

    def _process_item(self, item: dict, params: dict, *, completed: bool):
        item_type = item.get("type")
        iid = _live_id(item.get("id"), str(item_type or "process"))
        turn_id = _optional_wire_id(params.get("turnId"), "turn")
        phase = "end" if completed else "start"
        status = "succeeded" if completed else "running"
        if item_type == "reasoning":
            summary = _reasoning_summary(item)
            if not summary:
                # Never substitute content/encryptedContent for a missing public
                # summary.
                return None
            self._reasoning_started.add(iid)
            return ProcessEvent(
                item_id=iid,
                kind="reasoning",
                phase=phase,
                status=status,
                turn_id=turn_id,
                title="思考",
                summary=summary,
            )
        if item_type == "plan":
            detail, _ = bounded_text(item.get("text"), 256 * 1024)
            return ProcessEvent(
                item_id=iid, kind="plan", phase=phase, status=status,
                turn_id=turn_id, title="计划", detail=detail or None)
        if item_type == "collabAgentToolCall":
            return _collab_event(item, turn_id, completed)
        if item_type == "subAgentActivity":
            return _subagent_event(item, turn_id, completed)
        if item_type == "contextCompaction":
            return ProcessEvent(
                item_id=iid, kind="compaction", phase=phase, status=status,
                turn_id=turn_id, title="压缩上下文")
        return None


def _retry_detail(error: dict) -> str:
    """Return a bounded, credential-free retry status for the client."""
    message = error.get("message") if isinstance(error.get("message"), str) else ""
    details = (error.get("additionalDetails")
               if isinstance(error.get("additionalDetails"), str) else "")
    combined = message + " " + details
    status_match = re.search(r"\b([45]\d\d)\b", combined)
    status = status_match.group(1) if status_match else _structured_http_status(error)
    attempt = re.search(r"\b(\d+\s*/\s*\d+)\b", combined)
    if status:
        text = f"上游服务返回 HTTP {status}，Codex 正在重试"
    else:
        text = "Codex 上游请求暂时失败，正在重试"
    if attempt:
        text += f"（{attempt.group(1).replace(' ', '')}）"
    return text + "…"


def _bounded_model_field(value, max_chars: int) -> str:
    """Copy one declared model-notification string, never arbitrary payloads."""
    if not isinstance(value, str) or not value:
        return ""
    return bounded_text(value, max_chars)[0]


def _bounded_model_list(value) -> list[str]:
    """Bound declared string arrays by item count and per-item length."""
    if not isinstance(value, list):
        return []
    out = []
    for item in islice(value, _MODEL_LIST_MAX_ITEMS):
        text = _bounded_model_field(item, _MODEL_ENUM_MAX_CHARS)
        if text:
            out.append(text)
    return out


def _structured_http_status(error: dict) -> str | None:
    """Find a bounded codexErrorInfo.httpStatusCode without exposing details."""
    stack = [error.get("codexErrorInfo")]
    seen = 0
    while stack and seen < 32:
        value = stack.pop()
        seen += 1
        if not isinstance(value, dict):
            continue
        status = value.get("httpStatusCode")
        if isinstance(status, int) and 400 <= status <= 599:
            return str(status)
        stack.extend(list(value.values())[:16])
    return None


def _live_id(value, kind: str) -> str:
    """Return a protocol-safe, stable identity without trusting provider text."""
    if isinstance(value, str) and _SAFE_WIRE_ID.fullmatch(value):
        return value
    if isinstance(value, str):
        identity = value[:4096]
    elif value is None:
        identity = "missing"
    else:
        identity = type(value).__name__
    return hashlib.sha256(
        f"codex\0{kind}\0{identity}".encode("utf-8", "surrogatepass")
    ).hexdigest()[:32]


def _optional_wire_id(value, kind: str) -> str | None:
    return None if value is None else _live_id(value, kind)


def _assistant_channel(value) -> str:
    if value in {"final", "final_answer"}:
        return "final"
    if value == "commentary":
        return "commentary"
    if value == "thinking":
        return "thinking"
    return "unknown"


def _process_status(value) -> str:
    key = str(value or "").replace("_", "").replace("-", "").lower()
    if key in {"pending"}:
        return "pending"
    if key in {"inprogress", "running", "started"}:
        return "running"
    if key in {"completed", "complete", "succeeded", "success"}:
        return "succeeded"
    if key in {"failed", "failure", "error"}:
        return "failed"
    if key in {"declined", "denied", "blocked"}:
        return "declined"
    if key in {"cancelled", "canceled", "stopped"}:
        return "cancelled"
    if key in {"interrupted", "aborted"}:
        return "interrupted"
    return "unknown"


def _plan_status(value) -> str:
    status = _process_status(value)
    if status == "running":
        return "inProgress"
    if status == "succeeded":
        return "completed"
    return "pending"


def _duration_ms(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _nonnegative_or_signed_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _is_credential_key(key_text: str) -> bool:
    """Match common credential keys across snake, kebab and camel case."""
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key_text)
    tokens = tuple(filter(None, re.split(r"[^a-z0-9]+", separated.lower())))
    compact = "".join(tokens)
    return (
        compact in _CREDENTIAL_EXACT_KEYS
        or any(fragment in compact for fragment in _CREDENTIAL_KEY_FRAGMENTS)
    )


def _redact_credentials(
    value,
    depth: int = 0,
    ancestors=None,
    node_budget=None,
):
    """Copy bounded JSON-like tool data and replace credential-bearing values."""
    if node_budget is None:
        node_budget = [_MAX_REDACTION_NODES]
    if node_budget[0] <= 0:
        return _REDACTION_BUDGET_EXCEEDED
    node_budget[0] -= 1
    if depth >= _MAX_REDACTION_DEPTH:
        return f"<{type(value).__name__} omitted>"
    if isinstance(value, dict):
        ancestors = ancestors if ancestors is not None else set()
        identity = id(value)
        if identity in ancestors:
            return "<cycle omitted>"
        ancestors.add(identity)
        try:
            out = {}
            for key, item in islice(
                value.items(), _MAX_REDACTION_DICT_ITEMS
            ):
                if node_budget[0] <= 0:
                    out[_REDACTION_REMAINDER_KEY] = (
                        _REDACTION_BUDGET_EXCEEDED
                    )
                    break
                key_text = key if isinstance(key, str) else f"<{type(key).__name__}>"
                if _is_credential_key(key_text):
                    node_budget[0] -= 1
                    safe_item = _REDACTED
                else:
                    safe_item = _redact_credentials(
                        item, depth + 1, ancestors, node_budget
                    )
                out[key_text[:128]] = safe_item
            return out
        finally:
            ancestors.discard(identity)
    if isinstance(value, (list, tuple)):
        ancestors = ancestors if ancestors is not None else set()
        identity = id(value)
        if identity in ancestors:
            return "<cycle omitted>"
        ancestors.add(identity)
        try:
            out = []
            for item in islice(value, _MAX_REDACTION_SEQUENCE_ITEMS):
                if node_budget[0] <= 0:
                    out.append(_REDACTION_BUDGET_EXCEEDED)
                    break
                out.append(_redact_credentials(
                    item, depth + 1, ancestors, node_budget
                ))
            return out
        finally:
            ancestors.discard(identity)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return f"<{type(value).__name__}>"


def _tool_input(item: dict) -> dict:
    item_type = item.get("type")
    if item_type == "commandExecution":
        out = {
            "command": item.get("command"),
            "cwd": item.get("cwd"),
            "actions": item.get("commandActions"),
            "source": item.get("source"),
        }
        if item.get("processId") is not None:
            out["process_id"] = item.get("processId")
        return {key: value for key, value in out.items() if value is not None}
    if item_type == "fileChange":
        return {"changes": _change_descriptors(item.get("changes"))}
    if item_type == "mcpToolCall":
        arguments = item.get("arguments")
        if isinstance(arguments, dict):
            return _redact_credentials(arguments)
        return {"arguments": _redact_credentials(arguments)}
    if item_type == "dynamicToolCall":
        arguments = item.get("arguments")
        sanitized = _redact_credentials(arguments)
        out = sanitized if isinstance(sanitized, dict) else {"arguments": sanitized}
        if item.get("namespace") is not None:
            out = dict(out)
            out["namespace"] = item.get("namespace")
        return out
    if item_type == "webSearch":
        return {
            key: item.get(key) for key in ("query", "action")
            if item.get(key) is not None
        }
    return {}


def _tool_presentation(item: dict) -> tuple[str, str, str | None, str | None]:
    item_type = item.get("type")
    if item_type == "commandExecution":
        actions = item.get("commandActions") or []
        first = actions[0] if actions and isinstance(actions[0], dict) else {}
        action_type = first.get("type")
        if action_type == "read":
            path = first.get("path") or first.get("name")
            title = f"读取 {path}" if path else "读取文件"
            return "readFile", "command", title, None
        if action_type == "listFiles":
            path = first.get("path")
            title = f"列出 {path}" if path else "列出文件"
            return "listFiles", "command", title, None
        if action_type == "search":
            query = first.get("query")
            title = f"搜索 {query}" if query else "搜索内容"
            return "search", "command", title, None
        return "shell", "command", "运行命令", None
    if item_type == "fileChange":
        paths = _change_paths(item.get("changes"))
        return "apply_patch", "file", _file_summary(paths, "running"), None
    if item_type == "mcpToolCall":
        server = str(item.get("server") or "MCP")[:1024]
        tool = str(item.get("tool") or "mcp")[:1024]
        return tool, "mcp", f"{server} · {tool}"[:1024], server
    if item_type == "dynamicToolCall":
        tool = str(item.get("tool") or "dynamicTool")[:1024]
        namespace = item.get("namespace")
        title = f"{namespace} · {tool}" if namespace else tool
        return tool, "server_tool", title[:1024], None
    if item_type == "webSearch":
        query, _ = bounded_text(item.get("query"), 900)
        return "webSearch", "web_search", (
            f"搜索 {query}" if query else "搜索网页"), None
    return str(item_type or "tool")[:1024], "tool", None, None


def _change_descriptors(changes) -> list[dict]:
    descriptors: list[dict] = []
    if isinstance(changes, list):
        iterable = changes[:64]
        for entry in iterable:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            if isinstance(kind, dict):
                kind = kind.get("type")
            descriptors.append({
                "path": str(entry.get("path") or "")[:16 * 1024],
                "kind": str(kind or "update")[:128],
            })
    elif isinstance(changes, dict):
        for path, change in list(changes.items())[:64]:
            kind = change.get("type") if isinstance(change, dict) else "update"
            descriptors.append({
                "path": str(path)[:16 * 1024],
                "kind": str(kind or "update")[:128],
            })
    return descriptors


def _change_paths(changes) -> list[str]:
    return [entry["path"] for entry in _change_descriptors(changes)
            if entry.get("path")]


def _changes_diff(changes) -> str:
    """Normalize v2 FileUpdateChange arrays and legacy path->change maps."""
    parts: list[str] = []
    if isinstance(changes, list):
        for entry in changes[:64]:
            if not isinstance(entry, dict):
                continue
            diff = entry.get("diff")
            if isinstance(diff, str) and diff:
                parts.append(diff)
    elif isinstance(changes, dict):
        for _path, entry in list(changes.items())[:64]:
            if not isinstance(entry, dict):
                continue
            diff = entry.get("unified_diff") or entry.get("diff")
            if isinstance(diff, str) and diff:
                parts.append(diff)
    return "\n".join(parts)


def _file_summary(paths: list[str], status: str) -> str:
    prefix = "修改了" if status == "succeeded" else "修改"
    if not paths:
        return f"{prefix}文件"
    if len(paths) == 1:
        return f"{prefix} {paths[0]}"[:64 * 1024]
    return f"{prefix} {len(paths)} 个文件"


def _reasoning_summary(item: dict) -> str:
    values = item.get("summary")
    parts: list[str] = []
    if isinstance(values, list):
        for value in values[:128]:
            if isinstance(value, str):
                text = value
            elif isinstance(value, dict) and value.get("type") == "summary_text":
                text = value.get("text")
            else:
                continue
            if isinstance(text, str) and text:
                parts.append(text)
    text, _ = bounded_text("\n\n".join(parts), 64 * 1024)
    return text


def _mcp_result_content(item: dict):
    error = item.get("error") if isinstance(item.get("error"), dict) else None
    if error is not None:
        return error.get("message") or "MCP tool call failed"
    result = item.get("result")
    if not isinstance(result, dict):
        return result or ""
    # `_meta` is server-private and can contain connector/session data. Never put
    # it in a replayable client ring.
    return _redact_credentials({
        key: result.get(key) for key in ("content", "structuredContent")
        if result.get(key) is not None
    })


def _collab_event(item: dict, turn_id: str | None, completed: bool):
    tool = str(item.get("tool") or "agent")[:1024]
    status = _process_status(item.get("status"))
    if completed and status in {"unknown", "running", "pending"}:
        status = "succeeded"
    labels = {
        "spawnAgent": "启动协作代理",
        "sendInput": "向协作代理发送消息",
        "resumeAgent": "恢复协作代理",
        "wait": "等待协作代理",
        "closeAgent": "关闭协作代理",
    }
    states = item.get("agentsStates")
    safe_states = {}
    if isinstance(states, dict):
        for agent_id, state in list(states.items())[:32]:
            if not isinstance(state, dict):
                continue
            safe_states[str(agent_id)[:128]] = {
                "status": _process_status(state.get("status")),
            }
    input_value = bounded_tool_input({
        "prompt": item.get("prompt"),
        "model": item.get("model"),
        "reasoning_effort": item.get("reasoningEffort"),
        "receivers": item.get("receiverThreadIds"),
        "agents": safe_states,
    }, 64 * 1024)
    return ProcessEvent(
        item_id=_live_id(item.get("id"), "collab-agent"),
        kind="agent",
        phase="end" if completed else "start",
        status=status,
        turn_id=turn_id,
        parent_id=_optional_wire_id(item.get("senderThreadId"), "thread"),
        title=labels.get(tool, "协作代理"),
        input=input_value,
        tool=tool,
    )


def _subagent_event(item: dict, turn_id: str | None, completed: bool):
    kind = str(item.get("kind") or "started")
    status = (
        "interrupted" if kind == "interrupted"
        else "succeeded" if completed
        else "running"
    )
    path, _ = bounded_text(item.get("agentPath"), 16 * 1024)
    return ProcessEvent(
        item_id=_live_id(item.get("id"), "sub-agent"),
        kind="agent",
        phase="end" if completed else "start",
        status=status,
        turn_id=turn_id,
        parent_id=_optional_wire_id(item.get("agentThreadId"), "thread"),
        title={
            "started": "协作代理已启动",
            "interacted": "协作代理有新进展",
            "interrupted": "协作代理已中断",
        }.get(kind, "协作代理"),
        summary=path or None,
    )


def _hook_event(params: dict, *, completed: bool):
    run = params.get("run") if isinstance(params.get("run"), dict) else None
    if run is None:
        return None
    status = _process_status(run.get("status"))
    if completed and status in {"unknown", "running", "pending"}:
        status = "succeeded"
    event_name = str(run.get("eventName") or "hook")[:256]
    handler_type = str(run.get("handlerType") or "")[:128]
    # Hook output/statusMessage can include command output, environment data, or
    # credentials. Only lifecycle metadata crosses the remote boundary.
    return ProcessEvent(
        item_id=_live_id(run.get("id"), "hook"),
        kind="hook",
        phase="end" if completed else "start",
        status=status,
        turn_id=_optional_wire_id(params.get("turnId"), "turn"),
        title=(f"Hook · {event_name}" + (
            f" · {handler_type}" if handler_type else ""))[:1024],
        duration_ms=_duration_ms(run.get("durationMs")),
    )


# ---- helpers the machine loop needs (codex analogs of stream.extract_*) ----

def codex_session_id(msg: dict) -> str | None:
    """Thread id from any notification that carries the thread object."""
    p = msg.get("params") or {}
    th = p.get("thread")
    if isinstance(th, dict):
        return th.get("id") or th.get("sessionId")
    return None


def is_turn_terminal(msg: dict) -> bool:
    """Codex's turn/completed plays the role of Claude's ResultMessage."""
    return msg.get("method") == "turn/completed"


# ---- on-disk Codex rollout -> wire events (session history) ----

def codex_translate_history(path: str, tool_result_max: int) -> tuple[list, str | None]:
    """Translate a Codex rollout .jsonl into wire events (same vocabulary as the
    live stream) + the model used. Codex analog of stream.translate_history.

    A turn = event_msg/user_message -> (function_call/reasoning...) -> agent_message.
    Skips the <environment_context>/<permissions> developer/user envelope messages;
    uses the clean event_msg user_message / agent_message text. Returns
    (events, model)."""
    events: list = []
    model: str | None = None
    session_id: str | None = None
    turn_open = False
    active_turn_id: str | None = None
    active_msg_id: str | None = None
    pending_turn_id: str | None = None
    turn_visible = False
    turn_text_visible = False
    turn_final_visible = False
    assistant_open = False
    cur_mid: str | None = None
    cur_channel = "unknown"
    last_ts = None
    pending_images: list = []   # input_image blocks seen before the next user_message
    seen_tool_uses: set[str] = set()
    seen_tool_results: set[str] = set()
    seen_authoritative_results: set[str] = set()
    plan_tool_ids: set[str] = set()
    seen_process_items: set[str] = set()
    history_tools: dict[str, tuple[str, str, str | None, str | None, dict]] = {}
    seen_agent_messages: set[tuple[str, str, str]] = set()
    seen_reasoning: set[tuple[str, str]] = set()

    def _ts(iso: str):
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    def _stable_id(kind: str, line_no: int, raw_ts: str = "", identity=None) -> str:
        """Deterministic fallback for rollout records that carry no item id."""
        seed = "\0".join((
            session_id or path,
            kind,
            str(identity or active_turn_id or ""),
            str(line_no),
            raw_ts,
        ))
        return hashlib.sha256(seed.encode("utf-8", "surrogatepass")).hexdigest()[:32]

    def _history_id(value, kind: str, line_no: int, raw_ts: str = "") -> str:
        if isinstance(value, str) and _SAFE_WIRE_ID.fullmatch(value):
            return value
        identity = value[:1024] if isinstance(value, str) else type(value).__name__
        return _stable_id(kind, line_no, raw_ts, identity)

    def _duration(payload: dict) -> int:
        try:
            return int(payload.get("duration_ms") or payload.get("durationMs") or 0)
        except (TypeError, ValueError):
            return 0

    def _completed_ts(payload: dict, fallback):
        value = payload.get("completed_at") or payload.get("completedAt")
        if isinstance(value, (int, float)):
            value = float(value)
            return value / 1000 if value > 100_000_000_000 else value
        if isinstance(value, str):
            return _ts(value) or fallback
        return fallback

    def ensure_assistant(
        line_no: int,
        raw_ts: str = "",
        item_id=None,
        channel: str = "commentary",
        *,
        force_new: bool = False,
    ):
        nonlocal assistant_open, cur_mid, cur_channel
        if force_new and assistant_open:
            close_assistant()
        if assistant_open and cur_channel != channel:
            close_assistant()
        if not assistant_open:
            cur_mid = _history_id(item_id, "assistant", line_no, raw_ts)
            assistant_open = True
            cur_channel = channel
            events.append(AssistantMsgStart(
                message_id=cur_mid, channel=channel))

    def close_assistant():
        nonlocal assistant_open, cur_mid, cur_channel
        if assistant_open and cur_mid:
            events.append(AssistantMsgEnd(
                message_id=cur_mid, channel=cur_channel))
        assistant_open = False
        cur_mid = None
        cur_channel = "unknown"

    def upsert_tool_use(
        tool_id: str,
        tool: str,
        category: str,
        title: str | None,
        server: str | None,
        tool_input: dict,
        line_no: int,
        raw_ts: str,
    ) -> None:
        nonlocal turn_visible
        history_tools[tool_id] = (
            tool, category, title, server, tool_input)
        for event in reversed(events):
            if isinstance(event, ToolUse) and event.tool_use_id == tool_id:
                event.tool = tool
                event.category = category
                event.title = title
                event.server = server
                event.input = tool_input
                seen_tool_uses.add(tool_id)
                turn_visible = True
                return
        ensure_assistant(line_no, raw_ts)
        events.append(ToolUse(
            message_id=cur_mid or "",
            tool_use_id=tool_id,
            tool=tool,
            input=tool_input,
            category=category,
            title=title,
            server=server,
        ))
        seen_tool_uses.add(tool_id)
        turn_visible = True

    def upsert_tool_result(result: ToolResult) -> None:
        nonlocal turn_visible
        for index in range(len(events) - 1, -1, -1):
            event = events[index]
            if (isinstance(event, ToolResult)
                    and event.tool_use_id == result.tool_use_id):
                events[index] = result
                seen_tool_results.add(result.tool_use_id)
                turn_visible = True
                return
        events.append(result)
        seen_tool_results.add(result.tool_use_id)
        turn_visible = True

    def open_assistant_only_turn():
        """Start a visible continuation that has no user_message record.

        Goal/background continuations can begin with task_started after the
        previous user turn is already complete. The first visible assistant or
        tool item proves this is a separate assistant-only turn.
        """
        nonlocal turn_open, active_turn_id, active_msg_id
        nonlocal turn_visible, turn_text_visible, turn_final_visible
        if turn_open:
            return
        turn_open = True
        active_turn_id = pending_turn_id
        active_msg_id = None
        turn_visible = False
        turn_text_visible = False
        turn_final_visible = False

    def close_turn(
        subtype: str,
        duration_ms: int,
        is_error: bool,
        completed_ts=None,
        completed_turn_id=None,
        authoritative_boundary: bool = True,
    ):
        nonlocal turn_open, active_turn_id, active_msg_id, pending_turn_id
        nonlocal assistant_open, cur_mid, turn_visible, turn_text_visible
        nonlocal turn_final_visible
        if not turn_open:
            return
        close_assistant()
        # Automatic continuations may replace the initially-visible turn id.
        # The message action must fork after the last internal turn that actually
        # completed this visible reply, so prefer the terminal record, then the
        # latest task_started/turn_context id, and only then the first user turn.
        terminal_turn_id = (
            completed_turn_id or pending_turn_id
            if authoritative_boundary else None
        )
        if (not isinstance(terminal_turn_id, str)
                or not _SAFE_WIRE_ID.fullmatch(terminal_turn_id)):
            terminal_turn_id = None
        te = TurnEnd(result=TurnResult(
            subtype=subtype, duration_ms=duration_ms, is_error=is_error),
            turn_id=terminal_turn_id)
        terminal_ts = completed_ts if completed_ts is not None else last_ts
        if terminal_ts is not None:
            te.ts = terminal_ts
        events.append(te)
        turn_open = False
        pending_turn_id = None
        active_turn_id = None
        active_msg_id = None
        turn_visible = False
        turn_text_visible = False
        turn_final_visible = False

    try:
        f = open(path)
    except Exception:
        return [], None
    with f:
        for line_no, line in _bounded_jsonl_records(f):
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")
            p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
            raw_ts = d.get("timestamp", "")
            ts = _ts(raw_ts)
            payload_type = p.get("type")

            if t == "session_meta" and p.get("id"):
                session_id = str(p["id"])
            elif t == "turn_context":
                if p.get("model"):
                    model = p["model"]
                context_turn_id = p.get("turn_id")
                if context_turn_id:
                    context_turn_id = str(context_turn_id)
                    # Codex can start an automatic continuation with a new turn_id
                    # but no new user_message. It is still the same visible chat
                    # turn, so only a real user_message creates a boundary.
                    pending_turn_id = context_turn_id
            elif t == "response_item" and p.get("type") == "message" and p.get("role") == "user":
                # the raw user turn carries any uploaded images (input_image, a
                # data: URI). It precedes the clean event_msg/user_message; buffer
                # them and attach to that UserMsg so images replay on reload.
                for it in (p.get("content") or []):
                    if isinstance(it, dict) and it.get("type") == "input_image":
                        img = _data_uri_to_img(it.get("image_url"))
                        if img:
                            pending_images.append(img)
            elif t == "event_msg" and payload_type == "task_started":
                next_turn_id = p.get("turn_id")
                if next_turn_id:
                    next_turn_id = str(next_turn_id)
                    pending_turn_id = next_turn_id
            elif t == "event_msg" and payload_type == "user_message":
                msg = p.get("message") or ""
                if msg and not msg.lstrip().startswith("<"):
                    next_turn_id = p.get("turn_id") or pending_turn_id
                    if turn_open:
                        # No terminal record proved where the previous visible
                        # reply ended. In particular, pending_turn_id now often
                        # belongs to this NEW user turn; never attach it to the
                        # synthetic error boundary.
                        close_turn(
                            "error", 0, True,
                            authoritative_boundary=False)
                    active_turn_id = str(next_turn_id) if next_turn_id else None
                    pending_turn_id = active_turn_id
                    uid = _history_id(active_turn_id, "user", line_no, raw_ts)
                    active_msg_id = uid
                    um = UserMsg(msg_id=uid, prompt=msg)
                    if pending_images:
                        um.images = pending_images
                    if ts is not None:
                        um.ts = ts
                    events.append(um)
                    turn_open = True
                pending_images = []   # consume (per user turn)
            elif (t == "response_item"
                  and payload_type in {"function_call", "custom_tool_call"}):
                open_assistant_only_turn()
                tool_id = _history_id(
                    p.get("call_id") or p.get("id"),
                    "tool", line_no, raw_ts)
                arguments = (p.get("arguments") if payload_type == "function_call"
                             else p.get("input"))
                hist_input = _hist_tool_input(arguments, p.get("name"))
                plan_event = _history_plan_event(
                    p.get("name"), hist_input,
                    _history_optional_turn_id(
                        active_turn_id or pending_turn_id),
                    _history_id, tool_id, line_no, raw_ts,
                )
                if plan_event is not None:
                    plan_tool_ids.add(tool_id)
                    seen_tool_uses.add(tool_id)
                    turn_visible = True
                    events.append(plan_event)
                else:
                    ensure_assistant(line_no, raw_ts)
                    tool, category, title, server = _hist_tool_presentation(
                        p.get("name"), hist_input)
                    history_tools[tool_id] = (
                        tool, category, title, server, hist_input)
                    if tool_id not in seen_tool_uses:
                        seen_tool_uses.add(tool_id)
                        turn_visible = True
                        events.append(ToolUse(
                            message_id=cur_mid or "",
                            tool_use_id=tool_id,
                            tool=tool,
                            input=hist_input,
                            category=category,
                            title=title,
                            server=server,
                        ))
            elif (t == "response_item"
                  and payload_type in {
                      "function_call_output", "custom_tool_call_output"}):
                open_assistant_only_turn()
                tool_id = _history_id(
                    p.get("call_id"), "tool", line_no, raw_ts)
                tool_meta = history_tools.get(
                    tool_id, ("tool", "tool", None, None, {}))
                if tool_id in plan_tool_ids:
                    seen_tool_results.add(tool_id)
                else:
                    if tool_id not in seen_tool_uses:
                        ensure_assistant(line_no, raw_ts)
                        tool, category, title, server, hist_input = tool_meta
                        seen_tool_uses.add(tool_id)
                        events.append(ToolUse(
                            message_id=cur_mid or "", tool_use_id=tool_id,
                            tool=tool, input=hist_input, category=category,
                            title=title, server=server))
                    if tool_id not in seen_tool_results:
                        seen_tool_results.add(tool_id)
                        turn_visible = True
                        category = tool_meta[1]
                        raw_output = p.get("output")
                        structured_error = False
                        if category in {"mcp", "server_tool"}:
                            raw_output, structured_error = (
                                _history_structured_tool_output(raw_output))
                        output, was_truncated = bounded_text(
                            raw_output, tool_result_max)
                        exit_code = _history_exit_code(output)
                        is_error = structured_error or _exit_is_error(output)
                        events.append(ToolResult(
                            tool_use_id=tool_id,
                            content=output,
                            is_error=is_error,
                            truncated=True if was_truncated else None,
                            status="failed" if is_error else "succeeded",
                            exit_code=exit_code,
                        ))
            elif t == "event_msg" and payload_type == "exec_command_end":
                open_assistant_only_turn()
                tool_id = _history_id(
                    p.get("call_id"), "tool", line_no, raw_ts)
                if tool_id not in seen_authoritative_results:
                    seen_authoritative_results.add(tool_id)
                    command = _legacy_command_text(p.get("command"))
                    command_input = bounded_tool_input({
                        "command": command,
                        "cwd": p.get("cwd"),
                        "actions": p.get("parsed_cmd"),
                        "source": p.get("source"),
                        "process_id": p.get("process_id"),
                    }, 64 * 1024)
                    title = _legacy_command_title(p.get("parsed_cmd"))
                    upsert_tool_use(
                        tool_id, "shell", "command", title, None,
                        command_input, line_no, raw_ts)
                    output, truncated = bounded_text(
                        p.get("aggregated_output")
                        or p.get("formatted_output")
                        or p.get("stdout")
                        or p.get("stderr")
                        or "",
                        tool_result_max,
                    )
                    exit_code = _nonnegative_or_signed_int(p.get("exit_code"))
                    status = _process_status(p.get("status"))
                    if exit_code is not None and exit_code != 0:
                        status = "failed"
                    elif status in {"unknown", "running", "pending"}:
                        status = "succeeded"
                    upsert_tool_result(ToolResult(
                        tool_use_id=tool_id,
                        content=output,
                        is_error=(status in {
                            "failed", "declined", "cancelled", "interrupted"
                        }),
                        truncated=True if truncated else None,
                        status=status,
                        exit_code=exit_code,
                        duration_ms=_legacy_duration_ms(p.get("duration")),
                    ))
            elif t == "event_msg" and payload_type == "mcp_tool_call_end":
                open_assistant_only_turn()
                tool_id = _history_id(
                    p.get("call_id"), "tool", line_no, raw_ts)
                if tool_id not in seen_authoritative_results:
                    seen_authoritative_results.add(tool_id)
                    invocation = (p.get("invocation")
                                  if isinstance(p.get("invocation"), dict)
                                  else {})
                    server = str(invocation.get("server") or "MCP")[:1024]
                    tool = str(invocation.get("tool") or "mcp")[:1024]
                    arguments = invocation.get("arguments")
                    tool_input = bounded_tool_input(
                        _redact_credentials(
                            arguments if isinstance(arguments, dict)
                            else {"arguments": arguments}),
                        64 * 1024,
                    )
                    upsert_tool_use(
                        tool_id, tool, "mcp", f"{server} · {tool}"[:1024],
                        server, tool_input, line_no, raw_ts)
                    content, is_error = _legacy_mcp_result(p.get("result"))
                    output, truncated = bounded_text(content, tool_result_max)
                    upsert_tool_result(ToolResult(
                        tool_use_id=tool_id,
                        content=output,
                        is_error=is_error,
                        truncated=True if truncated else None,
                        status="failed" if is_error else "succeeded",
                        duration_ms=_legacy_duration_ms(p.get("duration")),
                    ))
            elif t == "event_msg" and payload_type == "item_completed":
                item = p.get("item") if isinstance(p.get("item"), dict) else {}
                if str(item.get("type") or "").lower() == "plan":
                    open_assistant_only_turn()
                    item_id = _history_id(
                        item.get("id"), "plan-detail", line_no, raw_ts)
                    if item_id not in seen_process_items:
                        seen_process_items.add(item_id)
                        detail, truncated = bounded_text(
                            item.get("text"), 256 * 1024)
                        events.append(ProcessEvent(
                            item_id=item_id,
                            kind="plan",
                            phase="end",
                            status="succeeded",
                            turn_id=_history_optional_turn_id(
                                p.get("turn_id") or active_turn_id
                                or pending_turn_id),
                            title="计划",
                            detail=detail or None,
                            truncated=True if truncated else None,
                        ))
                        turn_visible = True
            elif t == "response_item" and payload_type == "reasoning":
                summary = _reasoning_summary(p)
                key = (str(active_turn_id or pending_turn_id or ""), summary)
                if summary and key not in seen_reasoning:
                    seen_reasoning.add(key)
                    open_assistant_only_turn()
                    events.append(ProcessEvent(
                        item_id=_history_id(
                            p.get("id"), "reasoning", line_no, raw_ts),
                        kind="reasoning",
                        phase="end",
                        status="succeeded",
                        turn_id=_history_optional_turn_id(
                            active_turn_id or pending_turn_id),
                        title="思考",
                        summary=summary,
                    ))
            elif t == "event_msg" and payload_type == "agent_reasoning":
                summary, _ = bounded_text(p.get("text"), 64 * 1024)
                key = (str(active_turn_id or pending_turn_id or ""), summary)
                if summary and key not in seen_reasoning:
                    seen_reasoning.add(key)
                    open_assistant_only_turn()
                    events.append(ProcessEvent(
                        item_id=_history_id(
                            p.get("id") or p.get("event_id"),
                            "reasoning", line_no, raw_ts),
                        kind="reasoning",
                        phase="end",
                        status="succeeded",
                        turn_id=_history_optional_turn_id(
                            active_turn_id or pending_turn_id),
                        title="思考",
                        summary=summary,
                    ))
            elif t == "event_msg" and payload_type == "agent_message":
                open_assistant_only_turn()
                txt = p.get("message") or ""
                channel = _assistant_channel(p.get("phase"))
                key = (str(active_turn_id or pending_turn_id or ""), channel, txt)
                if txt and key not in seen_agent_messages:
                    seen_agent_messages.add(key)
                    close_assistant()
                    ensure_assistant(
                        line_no, raw_ts,
                        p.get("id") or p.get("message_id"),
                        channel=channel,
                    )
                    turn_visible = True
                    turn_text_visible = True
                    if channel == "final":
                        turn_final_visible = True
                    events.append(Delta(
                        message_id=cur_mid, text=txt, channel=channel))
                    close_assistant()
            elif t == "event_msg" and payload_type == "patch_apply_end":
                open_assistant_only_turn()
                ensure_assistant(line_no, raw_ts)
                tool_id = _history_id(
                    p.get("call_id"), "tool", line_no, raw_ts)
                paths = _change_paths(p.get("changes"))
                if tool_id not in seen_tool_uses:
                    seen_tool_uses.add(tool_id)
                    turn_visible = True
                    events.append(ToolUse(
                        message_id=cur_mid or "",
                        tool_use_id=tool_id,
                        tool="apply_patch",
                        input=bounded_tool_input({
                            "changes": _change_descriptors(p.get("changes")),
                        }, 64 * 1024),
                        category="file",
                        title=_file_summary(paths, "running"),
                    ))
                if tool_id not in seen_tool_results:
                    seen_tool_results.add(tool_id)
                    turn_visible = True
                    success = p.get("success") is not False
                    diff, diff_truncated = bounded_text(
                        _changes_diff(p.get("changes")), 2 * 1024 * 1024)
                    output, output_truncated = bounded_text(
                        p.get("stdout") or p.get("stderr") or "",
                        tool_result_max)
                    events.append(ToolResult(
                        tool_use_id=tool_id,
                        content=output,
                        is_error=not success,
                        truncated=(True if diff_truncated or output_truncated
                                   else None),
                        status="succeeded" if success else "failed",
                        summary=_file_summary(
                            paths, "succeeded" if success else "failed"),
                        diff=diff or None,
                    ))
            elif t == "event_msg" and payload_type == "web_search_end":
                open_assistant_only_turn()
                ensure_assistant(line_no, raw_ts)
                tool_id = _history_id(
                    p.get("call_id"), "tool", line_no, raw_ts)
                query, _ = bounded_text(p.get("query"), 16 * 1024)
                if tool_id not in seen_tool_uses:
                    seen_tool_uses.add(tool_id)
                    turn_visible = True
                    events.append(ToolUse(
                        message_id=cur_mid or "",
                        tool_use_id=tool_id,
                        tool="webSearch",
                        input=bounded_tool_input({
                            "query": query, "action": p.get("action"),
                        }, 64 * 1024),
                        category="web_search",
                        title=(f"搜索 {query}" if query else "搜索网页")[:1024],
                    ))
                if tool_id not in seen_tool_results:
                    seen_tool_results.add(tool_id)
                    events.append(ToolResult(
                        tool_use_id=tool_id,
                        content="",
                        is_error=False,
                        status="succeeded",
                    ))
            elif t == "event_msg" and payload_type == "sub_agent_activity":
                open_assistant_only_turn()
                item = {
                    "id": p.get("event_id"),
                    "kind": p.get("kind"),
                    "agentThreadId": p.get("agent_thread_id"),
                    "agentPath": p.get("agent_path"),
                }
                events.append(_subagent_event(
                    item,
                    _history_optional_turn_id(
                        active_turn_id or pending_turn_id),
                    completed=True,
                ))
                turn_visible = True
            elif t == "event_msg" and payload_type == "context_compacted":
                open_assistant_only_turn()
                events.append(ProcessEvent(
                    item_id=_history_id(
                        p.get("id"), "compaction", line_no, raw_ts),
                    kind="compaction",
                    phase="end",
                    status="succeeded",
                    turn_id=_history_optional_turn_id(
                        active_turn_id or pending_turn_id),
                    title="压缩上下文",
                ))
            elif t == "event_msg" and payload_type == "task_complete":
                last = p.get("last_agent_message")
                if (not turn_open and isinstance(last, str) and last):
                    open_assistant_only_turn()
                if turn_open:
                    turn_key = str(active_turn_id or pending_turn_id or "")
                    last_already_visible = any(
                        key[0] == turn_key and key[2] == last
                        for key in seen_agent_messages)
                    if (not turn_final_visible and isinstance(last, str) and last
                            and not last_already_visible):
                        close_assistant()
                        ensure_assistant(
                            line_no, raw_ts, channel="final", force_new=True)
                        events.append(Delta(
                            message_id=cur_mid, text=last, channel="final"))
                        close_assistant()
                        seen_agent_messages.add((turn_key, "final", last))
                        turn_visible = True
                        turn_text_visible = True
                        turn_final_visible = True
                    if turn_visible:
                        close_turn("success", _duration(p), False,
                                   _completed_ts(p, ts), p.get("turn_id"))
                    else:
                        events.append(Error(
                            code=ERR_CC_CRASH,
                            message=_EMPTY_COMPLETED_MESSAGE,
                            msg_id=active_msg_id,
                        ))
                        close_turn("error", _duration(p), True,
                                   _completed_ts(p, ts), p.get("turn_id"))
            elif t == "event_msg" and payload_type == "turn_aborted":
                if turn_open:
                    interrupted = p.get("reason") == "interrupted"
                    close_turn(
                        "error_during_execution" if interrupted else "error",
                        _duration(p), True, _completed_ts(p, ts),
                        p.get("turn_id"))
            elif t == "event_msg" and payload_type in {
                    "task_failed", "turn_failed", "task_error"}:
                if turn_open:
                    close_turn("error", _duration(p), True,
                               _completed_ts(p, ts), p.get("turn_id"))
            # session_meta / world_state / token_count / private reasoning : skipped
            if ts is not None:
                last_ts = ts
    # A file can be read while Codex is still appending the current turn. Close
    # only its current text block; deliberately omit TurnEnd so the reducer keeps
    # the turn not-done instead of fabricating a completed status.
    close_assistant()
    return events, model


def _hist_tool_name(name) -> str:
    if name in ("exec", "exec_command", "shell", "local_shell"):
        return "shell"
    if name in ("apply_patch",):
        return "apply_patch"
    return name or "tool"


def _history_plan_event(
    name,
    tool_input: dict,
    turn_id: str | None,
    id_builder,
    tool_id: str,
    line_no: int,
    raw_ts: str,
):
    normalized = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    if not normalized.endswith("updateplan"):
        return None
    raw_plan = tool_input.get("plan")
    if not isinstance(raw_plan, list):
        return None
    plan = []
    for entry in raw_plan[:128]:
        if not isinstance(entry, dict):
            continue
        step, _ = bounded_text(entry.get("step"), 16 * 1024)
        if not step:
            continue
        plan.append({
            "step": step,
            "status": _plan_status(entry.get("status")),
        })
    explanation, _ = bounded_text(tool_input.get("explanation"), 64 * 1024)
    identity = f"plan:{turn_id or tool_id}"
    return TurnPlan(
        item_id=id_builder(identity, "plan", line_no, raw_ts),
        turn_id=turn_id,
        explanation=explanation or None,
        plan=plan,
    )


def _hist_tool_presentation(
    name, tool_input: dict,
) -> tuple[str, str, str | None, str | None]:
    raw_name = str(name or "tool")
    tool = _hist_tool_name(raw_name)
    if tool == "shell":
        return tool, "command", "运行命令", None
    if tool == "apply_patch":
        return tool, "file", "修改文件", None
    if raw_name in {"web_search", "webSearch", "search_web"}:
        query = tool_input.get("query")
        title = f"搜索 {query}" if query else "搜索网页"
        return "webSearch", "web_search", title[:1024], None
    if raw_name in {
        "spawn_agent", "spawnAgent", "send_input", "sendInput",
        "resume_agent", "resumeAgent", "wait_agent", "wait",
        "close_agent", "closeAgent",
    }:
        return raw_name[:1024], "agent", "协作代理", None
    if raw_name.startswith("mcp__"):
        parts = raw_name.split("__", 2)
        server = parts[1] if len(parts) > 1 and parts[1] else "MCP"
        mcp_tool = parts[2] if len(parts) > 2 and parts[2] else raw_name
        return mcp_tool[:1024], "mcp", f"{server} · {mcp_tool}"[:1024], server[:1024]
    return tool[:1024], "tool", raw_name[:1024], None


def _hist_tool_input(arguments, name=None) -> dict:
    try:
        a = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
    except Exception:
        if isinstance(arguments, str):
            mapped = _hist_tool_name(name)
            key = "command" if mapped == "shell" else (
                "patch" if mapped == "apply_patch" else "input")
            return bounded_tool_input({key: arguments}, 64 * 1024)
        a = {}
    if not isinstance(a, dict):
        return bounded_tool_input(
            {"args": _redact_credentials(a)}, 64 * 1024)
    out: dict = {}
    if a.get("cmd") is not None:
        out["command"] = a["cmd"]
    if a.get("workdir") is not None:
        out["cwd"] = a["workdir"]
    for k, v in a.items():
        if k not in ("cmd", "workdir", "yield_time_ms"):
            out[k] = v
    return bounded_tool_input(_redact_credentials(out), 64 * 1024)


def _history_structured_tool_output(output) -> tuple[object, bool]:
    """Allow-list replayable MCP/dynamic result fields from rollout output."""
    parsed = output
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
        except (TypeError, ValueError):
            # Opaque strings can embed serialized `_meta` or credentials without
            # field boundaries, so never replay them as trusted MCP history.
            return "MCP 工具调用已完成（历史结果格式不可解析）", False
    if isinstance(parsed, list):
        return {"content": _redact_credentials(parsed)}, False
    if not isinstance(parsed, dict):
        return "MCP 工具调用已完成", False

    candidate = parsed.get("result")
    if not isinstance(candidate, dict):
        candidate = parsed
    error = parsed.get("error")
    if error is None and candidate is not parsed:
        error = candidate.get("error")
    if error:
        if isinstance(error, dict):
            message = error.get("message")
        else:
            message = str(error)
        safe_error, _ = bounded_text(message or "MCP tool call failed", 64 * 1024)
        return safe_error, True

    safe = {}
    aliases = (
        ("content", "content"),
        ("structuredContent", "structuredContent"),
        ("structured_content", "structuredContent"),
        ("contentItems", "content"),
    )
    for source, target in aliases:
        if source in candidate and target not in safe:
            safe[target] = _redact_credentials(candidate[source])
    failed = parsed.get("success") is False or str(
        parsed.get("status") or "").lower() in {"failed", "error"}
    return (safe or "MCP 工具调用已完成"), failed


def _legacy_command_text(command) -> str:
    """Normalize persisted ``exec_command_end.command`` into display text."""
    if isinstance(command, str):
        text, _ = bounded_text(command, 256 * 1024)
        return text
    if isinstance(command, (list, tuple)):
        argv = [str(part) for part in list(command)[:256]]
        try:
            text = shlex.join(argv)
        except (TypeError, ValueError):
            text = " ".join(argv)
        text, _ = bounded_text(text, 256 * 1024)
        return text
    text, _ = bounded_text(command, 256 * 1024)
    return text


def _legacy_command_title(parsed_command) -> str:
    """Give old rollout command records the same semantic title as live items."""
    actions = parsed_command if isinstance(parsed_command, list) else []
    first = actions[0] if actions and isinstance(actions[0], dict) else {}
    action_type = re.sub(
        r"[^a-z0-9]", "", str(first.get("type") or "").lower())
    if action_type == "read":
        path = first.get("path") or first.get("name")
        return (f"读取 {path}" if path else "读取文件")[:1024]
    if action_type in {"list", "listfiles"}:
        path = first.get("path")
        return (f"列出 {path}" if path else "列出文件")[:1024]
    if action_type in {"search", "grep"}:
        query = first.get("query") or first.get("pattern")
        return (f"搜索 {query}" if query else "搜索内容")[:1024]
    return "运行命令"


def _legacy_duration_ms(duration) -> int | None:
    """Convert persisted protobuf-style ``{secs, nanos}`` durations."""
    if not isinstance(duration, dict):
        return _duration_ms(duration)
    secs = duration.get("secs")
    nanos = duration.get("nanos")
    if isinstance(secs, bool) or isinstance(nanos, bool):
        return None
    try:
        milliseconds = int(secs or 0) * 1000 + int(nanos or 0) // 1_000_000
    except (TypeError, ValueError, OverflowError):
        return None
    return milliseconds if milliseconds >= 0 else None


def _legacy_mcp_result(result) -> tuple[object, bool]:
    """Decode persisted Rust ``Result`` while excluding server-private metadata."""
    if not isinstance(result, dict):
        return "MCP 工具调用已完成", False
    if "Err" in result:
        # Err is an opaque provider string and may itself contain connector
        # credentials. Preserve failure semantics without replaying it verbatim.
        return "MCP 工具调用失败", True
    value = result.get("Ok")
    if not isinstance(value, dict):
        return "MCP 工具调用已完成", False
    safe = _redact_credentials({
        key: value.get(key) for key in ("content", "structuredContent")
        if value.get(key) is not None
    })
    return safe or "MCP 工具调用已完成", bool(value.get("isError"))


def _exit_is_error(output: str) -> bool:
    code = _history_exit_code(output)
    return code is not None and code != 0


def _history_exit_code(output: str) -> int | None:
    match = re.search(
        r"\b(?:process\s+)?(?:exited|exit)\s+(?:with\s+)?code\s*[:=]?\s*(-?\d+)",
        output or "",
        re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


def _history_optional_turn_id(value) -> str | None:
    return _optional_wire_id(value, "turn")


def _data_uri_to_img(url) -> dict | None:
    """`data:image/png;base64,XXXX` -> {media_type, data} (the web's QueryImg shape)."""
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    try:
        head, data = url.split(",", 1)
        mt = head[5:].split(";")[0] or "image/png"
        return {"media_type": mt, "data": data}
    except Exception:
        return None
