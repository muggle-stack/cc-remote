"""Project DSH V3 records and assistant attempts into cc-remote's timeline.

Only durable human messages create prompt rows. Stream attempts belong to the
step that opened them, including when a steer has since opened another row.
Settlement replaces provisional text by identity; replay never concatenates it.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

from cc_remote.protocol import (
    AssistantMsgEnd, AssistantMsgStart, ContextReport, Delta, Effort, Model,
    ProcessEvent, ToolResult, ToolUse, TurnBinding, TurnEnd, TurnPlan, TurnResult,
    UserMsg,
)
from cc_remote.wrapper.dsh_client import DshError


def model_id(provider: str, model: str) -> str:
    return f"dsh:{quote(provider, safe='')}:{quote(model, safe='')}"


def model_parts(value: str) -> tuple[str, str]:
    parts = value.split(":")
    if len(parts) != 3 or parts[0] != "dsh" or not all(parts[1:]):
        raise DshError("invalid_model", "请选择 DSH 模型目录中的模型。")
    return unquote(parts[1]), unquote(parts[2])


def identity(value: object, prefix: str = "dsh") -> str:
    # Native IDs can include characters/lengths outside the public WireId.
    return prefix + "-" + hashlib.sha256(str(value).encode()).hexdigest()[:32]


def text_content(blocks: object, kind: str = "text") -> str:
    if not isinstance(blocks, list):
        return ""
    return "\n".join(str(b.get("text", "")) for b in blocks
                     if isinstance(b, dict) and b.get("type") == kind)


def input_tokens(usage: dict) -> int:
    return sum(max(0, v) for key in (
        "inputTokens", "cacheReadTokens", "cacheWriteTokens",
    ) if type(v := usage.get(key)) is int)


def history_events(frames: list) -> list[dict]:
    """Group a cold page by native owner, not by late delivery order.

    A pre-steer assistant item can settle after the next human admission.
    The shared materializer expects contiguous visible turns, so gather each
    proven owner first and place its terminal after all its own items.
    """
    events = [frame.model_dump(exclude_none=True) for frame in frames]
    bindings = {e["msg_id"]: e["turn_id"] for e in events if e["type"] == "turn_binding"}
    groups: dict[str, list] = {}
    current = None
    for event in events:
        kind = event["type"]
        if kind == "user_msg":
            current = bindings.get(event["msg_id"])
        elif kind == "turn_binding":
            current = event["turn_id"]
        owner = event.get("turn_id") or current
        if owner is None or kind in {"model", "effort", "perm", "context_report"}:
            continue
        groups.setdefault(owner, []).append(event)
    return [event for group in groups.values()
            for event in [*[e for e in group if e["type"] != "turn_end"],
                          *[e for e in group if e["type"] == "turn_end"]]]


@dataclass
class Attempt:
    id: str
    turn: int
    step: int
    owner: str
    revision: int
    next_index: int = 0
    started_after: int = -1
    settled: bool = False
    text: dict[int, tuple[str, str]] = field(default_factory=dict)


class DshProjection:
    def __init__(self, *, cursor: int = -1):
        self.cursor = cursor
        self.turn = 0
        self.step = 0
        self.owner: str | None = None
        self.client_id: str | None = None
        self.open = False
        self.running = False
        self.started = 0
        self.step_owners: dict[tuple[int, int], str] = {}
        self.superseded: dict[str, None] = {}
        self.attempt: Attempt | None = None
        self.model: str | None = None
        self.effort: str | None = None
        self.capacity: int | None = None
        self.tokens: int | None = None
        self.context_estimated = False
        self.context_projection_seq = -1
        self.image_refs: dict[str, list[dict]] = {}
        self.fork_seqs: dict[str, int] = {}

    def owner_for(self, data: dict) -> str:
        key = (data.get("turn", self.turn), data.get("step", 0))
        return self.step_owners.setdefault(key, self.owner or f"dsh-turn-{key[0]}")

    def autonomous(self, turn: int) -> list:
        if self.owner is not None:
            return []
        self.owner = f"dsh-turn-{turn}"
        self.open = True
        return [TurnBinding(msg_id=self.owner, turn_id=self.owner, autonomous=True)]

    @staticmethod
    def message_id(turn: int, step: int, channel: str) -> str:
        return f"dsh-step-{turn}-{step}-{channel}"

    def context(self) -> ContextReport:
        available = self.tokens is not None and self.capacity is not None and self.capacity > 0
        return ContextReport(
            total_tokens=self.tokens or 0, max_tokens=self.capacity or 0,
            percentage=round(100 * self.tokens / self.capacity, 2) if available else 0,
            available=available, model=self.model,
            source="native_estimate" if self.context_estimated else "recent_turn",
        )

    def context_pressure(self, pressure: dict | None, *, seq: int | None = None) -> ContextReport:
        """Use DSH's provider-anchored projection, including compaction deltas.

        The native projection is a full value, not a patch. Missing capacity
        or usage must clear the old reading instead of reusing another route.
        Control projections and usage records travel on independent streams;
        their source sequence prevents a delayed record from undoing a refresh.
        """
        if type(seq) is int:
            if seq < self.context_projection_seq:
                return self.context()
            self.context_projection_seq = seq
        pressure = pressure if isinstance(pressure, dict) else {}
        capacity = pressure.get("contextWindow")
        projected = pressure.get("projectedTokens")
        self.context_estimated = type(projected) is int and projected >= 0
        tokens = projected if self.context_estimated else pressure.get("pressureTokens")
        self.capacity = capacity if type(capacity) is int and capacity > 0 else None
        self.tokens = tokens if type(tokens) is int and tokens >= 0 else None
        return self.context()

    def record(self, event: dict) -> list:
        seq = event.get("seq")
        if type(seq) is not int or seq < 0:
            raise DshError("invalid_stream", "DSH 记录序号无效，请重新连接。")
        if seq <= self.cursor:
            return []
        if seq != self.cursor + 1:
            raise DshError("stream_gap", "DSH 消息流存在缺口，正在重新同步。")
        self.cursor = seq
        kind, data = event.get("type"), event.get("data")
        if not isinstance(data, dict):
            raise DshError("invalid_stream", "DSH 记录格式无效。")
        stamp = event.get("time", 0)
        stamp = stamp / 1000 if isinstance(stamp, (float, int)) else 0
        out = self._record(kind, data, seq, stamp, event)
        for frame in out:
            frame.ts = stamp
        return out

    def _record(self, kind, data, seq, stamp, event):
        if kind == "turn/start":
            self.turn = data["turn"]
            self.started = stamp
            # Goal continuation can start a physical turn without a new human
            # prompt. Never attach its output to a completed previous turn.
            self.owner = None
            self.client_id = None
            self.open = False
            self.running = True
            self.step_owners.clear()
            self.superseded.clear()
            self.attempt = None
            return []
        if kind == "step/start":
            # The loop records step/start before admitting its human messages.
            # Bind ownership when the attempt/tool actually starts.
            self.step = data["step"]
            return []
        if kind == "user/message":
            # Compaction and plugins may append/replace user-role model context.
            # They are not another human prompt, even when sourced from one.
            if data.get("source", {}).get("kind") != "user" or isinstance(event.get("surfaceOp"), dict):
                return []
            out = []
            if self.open:
                # Steer admission is not the old step's terminal. Its active
                # model attempt can still stream and commit after this record.
                self.superseded[self.owner] = None
            self.owner = f"dsh-seq-{seq}"
            native_id = data.get("id", seq)
            self.client_id = data.get("source", {}).get("rpcId") or identity(native_id, "dsh-user")
            self.open = True
            blocks = data.get("content", [])
            refs = []
            files = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                attachment = block.get("attachment") or {}
                if block.get("type") == "image" and attachment.get("attachmentId"):
                    refs.append({"image_id": identity(attachment["attachmentId"], "dsh-image"),
                                 "media_type": attachment.get("mediaType", "image/png"),
                                 "width": attachment.get("width", 0), "height": attachment.get("height", 0),
                                 "byte_size": attachment.get("bytes", 0),
                                 "attachment_id": attachment["attachmentId"]})
                if block.get("type") == "file" and attachment.get("name"):
                    files.append({"filename": attachment["name"]})
            if refs:
                self.image_refs[self.client_id] = refs
            out.extend([UserMsg(msg_id=self.client_id, client_msg_id=self.client_id,
                                prompt=text_content(blocks), files=files or None),
                        TurnBinding(msg_id=self.client_id, turn_id=self.owner)])
            return out
        if kind == "step/end":
            owner = self.step_owners.get((data["turn"], data["step"]))
            if owner in self.superseded:
                self.superseded.pop(owner)
                return [TurnEnd(turn_id=owner, result=TurnResult(
                    subtype="steered", duration_ms=0, is_error=False))]
            return []
        if kind in {"request/header", "model/selection"}:
            if kind == "request/header":
                self.owner_for({"turn": self.turn, "step": self.step})
            config = data.get("header", {}).get("config", {}) if kind == "request/header" else data
            if config.get("provider") and config.get("model"):
                self.model = model_id(config["provider"], config["model"])
                self.effort = config.get("reasoningEffort")
                result = [Model(model=self.model)]
                if self.effort:
                    result.append(Effort(effort=self.effort))
                return result
            return []
        if kind == "request/context":
            self.owner_for({"turn": self.turn, "step": self.step})
            if seq <= self.context_projection_seq:
                return []
            capacity = data.get("contextWindow")
            self.capacity = capacity if type(capacity) is int and capacity > 0 else None
            return [self.context()]
        if kind in {"assistant/message", "assistant/attempt"}:
            opening = self.autonomous(data["turn"])
            owner = self.owner_for(data)
            if kind == "assistant/attempt":
                result = self._replace_text(data, owner, [], settled=False)
                result.append(ProcessEvent(
                    item_id=f"dsh-attempt-{seq}", kind="model", phase="end",
                    status="interrupted", turn_id=owner, title="模型尝试未提交",
                    detail="DSH 未将本次尝试作为正式回复；后续状态以会话事件为准。"))
            else:
                content = data.get("message", {}).get("content", [])
                result = self._replace_text(data, owner, content)
                if isinstance(data.get("usage"), dict) and seq > self.context_projection_seq:
                    self.tokens = input_tokens(data["usage"])
                    self.context_estimated = False
                    result.append(self.context())
            if self.attempt and (self.attempt.turn, self.attempt.step) == (data["turn"], data["step"]):
                self.attempt.settled = True
            return opening + result
        if kind == "tool/call":
            raw = data.get("arguments", "{}")
            try:
                args = json.loads(raw)
            except (ValueError, TypeError):
                args = {"arguments": str(raw)}
            if not isinstance(args, dict):
                args = {"arguments": args}
            name = data.get("name", "tool")
            category = ("command" if name in {"shell", "bash", "exec"} else
                        "file" if name in {"read", "write", "edit", "apply_patch"} else
                        "agent" if "agent" in name else "mcp" if name.startswith("mcp") else "tool")
            return self.autonomous(data["turn"]) + [ToolUse(message_id=f"dsh-step-{data['turn']}-{data['step']}-tools",
                            tool_use_id=identity(data["callId"], "dsh-tool"),
                            turn_id=self.owner_for(data), tool=name, input=args, category=category)]
        if kind == "tool/result":
            result = []
            for block in data.get("message", {}).get("content", []):
                if block.get("type") != "tool-result":
                    continue
                failed = bool(block.get("isError") or data.get("error"))
                result.append(ToolResult(
                    tool_use_id=identity(block["toolCallId"], "dsh-tool"), turn_id=self.owner_for(data),
                    content=text_content(block.get("content", [])), is_error=failed,
                    status="failed" if failed else "succeeded"))
            return result
        if kind == "turn/end":
            self.running = False
            opening = self.autonomous(data["turn"])
            if not self.open:
                return []
            self.open = False
            reason = data.get("reason", {})
            status = reason.get("kind", "interrupted")
            subtype = {"completed": "success", "aborted": "interrupted", "blocked": "error_blocked",
                       "error": "error_during_execution", "max-tokens": "error_max_tokens"}.get(status, status)
            error = status in {"error", "max-tokens", "blocked"}
            details = reason.get("error", {})
            out = opening + [TurnEnd(turn_id=owner, result=TurnResult(
                subtype="steered", duration_ms=0, is_error=False)) for owner in self.superseded]
            self.superseded.clear()
            if error:
                out.append(ProcessEvent(
                    item_id=f"dsh-error-{seq}", kind="model", phase="end", status="failed",
                    turn_id=self.owner, title={"blocked": "DSH 等待处理", "max-tokens": "达到输出长度限制"}.get(status, "DSH 模型请求失败"),
                    summary=str(details.get("message") or "请检查 DSH 状态后继续。")[:4096]))
            self.fork_seqs[self.owner] = seq
            while len(self.fork_seqs) > 256:
                del self.fork_seqs[next(iter(self.fork_seqs))]
            out.append(TurnEnd(turn_id=self.owner, result=TurnResult(
                subtype=subtype, duration_ms=max(0, int((stamp-self.started)*1000)),
                is_error=error, **({"error": str(details.get("message", ""))[:4096]} if error else {}))))
            return out
        if kind in {"compaction/start", "compaction/end"}:
            ended = kind.endswith("end")
            return [ProcessEvent(item_id=identity(data.get("compactionId", seq), "dsh-compact"),
                                 turn_id=self.owner, kind="compaction", phase="end" if ended else "start",
                                 status=("failed" if data.get("error") else "succeeded") if ended else "running",
                                 title="压缩上下文", summary=str(data.get("error", ""))[:4096] or None)]
        if kind == "todo/write":
            todos = data.get("todos", [])
            return [TurnPlan(item_id="dsh-todo", turn_id=self.owner, plan=[{
                "step": str(t.get("content", t.get("text", ""))),
                "status": {"pending": "pending", "in_progress": "in_progress", "completed": "completed"}.get(t.get("status"), "pending"),
            } for t in todos[:64]])]
        if kind in {"command/run", "command/done"}:
            ended = kind.endswith("done")
            return [ProcessEvent(
                item_id=identity(data["commandId"], "dsh-command"), turn_id=self.owner,
                kind="command", phase="end" if ended else "start",
                status=("failed" if data.get("kind") == "error" else "succeeded") if ended else "running",
                title="/" + data.get("name", "命令"), summary=str(data.get("text", ""))[:8192] or None)]
        # Domain projections (goals, presets, titles, permissions), constructor
        # seeds and model-context replacements do not create conversation rows.
        return []

    def _replace_text(self, data, owner, blocks, *, settled=True):
        out = []
        for channel, kind in (("thinking", "reasoning"), ("unknown", "text")):
            text = text_content(blocks, kind)
            message_id = self.message_id(data["turn"], data["step"], channel)
            # An empty replacement only retires a provisional block already seen.
            if not text and not (self.attempt and self.attempt.text):
                continue
            out.extend([AssistantMsgStart(message_id=message_id, turn_id=owner, channel=channel),
                        Delta(message_id=message_id, turn_id=owner, channel=channel, text=text, replace=True)])
            if settled:
                out.append(AssistantMsgEnd(message_id=message_id, turn_id=owner, channel=channel))
        return out

    def baseline(self, baseline: dict) -> list:
        self.attempt = None
        active = baseline.get("activeAttempt")
        if not active:
            return []
        out = self.frame({"type": "start", "revision": baseline["revision"], **active})
        for packed in active.get("stream", []):
            if packed.get("type") in {"text-chunks", "reasoning-chunks"}:
                kind = "text-delta" if packed["type"] == "text-chunks" else "reasoning-delta"
                out += self._chunk({"type": kind, "index": packed["index"], "text": "".join(packed["texts"])})
            elif packed.get("type") == "chunk":
                out += self._chunk(packed["chunk"])
        self.attempt.next_index = active.get("nextIndex", 0)
        return out

    def frame(self, frame: dict) -> list:
        kind = frame.get("type")
        if kind == "start":
            opening = self.autonomous(frame["turn"])
            self.attempt = Attempt(frame["attemptId"], frame["turn"], frame["step"],
                                   self.owner_for(frame), frame["revision"],
                                   started_after=frame.get("startedAfterSeq", -1))
            return opening
        attempt = self.attempt
        if attempt is None or frame.get("attemptId") != attempt.id:
            raise DshError("stream_gap", "DSH 输出尝试发生变化，正在重新同步。")
        if frame.get("revision") != attempt.revision + 1 or frame.get("index") != attempt.next_index:
            raise DshError("stream_gap", "DSH 输出片段不连续，正在重新同步。")
        attempt.next_index += 1
        attempt.revision = frame["revision"]
        if kind == "chunk":
            return [] if attempt.settled else self._chunk(frame["chunk"])
        if kind == "end":
            outcome = frame.get("outcome", {})
            if outcome.get("kind") == "committed" and outcome.get("seq", self.cursor+1) > self.cursor:
                raise DshError("stream_gap", "DSH 输出缺少正式记录，正在重新同步。")
            out = [] if attempt.settled else self._replace_text(
                {"turn": attempt.turn, "step": attempt.step}, attempt.owner, [])
            self.attempt = None
            return out
        raise DshError("invalid_stream", "DSH 输出片段格式无效。")

    def _chunk(self, chunk):
        attempt = self.attempt
        if attempt is None or chunk.get("type") not in {"text-delta", "reasoning-delta"}:
            return []
        channel = "thinking" if chunk["type"] == "reasoning-delta" else "unknown"
        index = chunk.get("index", 0)
        text = str(chunk.get("text", ""))
        _, old = attempt.text.get(index, (channel, ""))
        attempt.text[index] = (channel, old + text)
        message_id = self.message_id(attempt.turn, attempt.step, channel)
        return [AssistantMsgStart(message_id=message_id, turn_id=attempt.owner, channel=channel),
                Delta(message_id=message_id, turn_id=attempt.owner, channel=channel, text=text)]
