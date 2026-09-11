"""Small, bounded terminal projection. No engine or filesystem access."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json

from cc_remote.tui import _safe_remote_text
from cc_remote.tui_questions import message_metadata, question_text
from cc_remote.tui_presentation import (
    SessionPresentation,
    TurnDisplay,
    bounded,
    describe,
    duration,
    timestamp,
)

MAX_BLOCK_CHARS = 64 * 1024
MAX_BLOCKS = 160
TRUNCATED = "\n[Terminal display limit reached; inspect turn details.]"
REPORT_TYPES = {
    "models",
    "engine_capabilities",
    "permission_profiles",
    "diff_report",
    "turn_file_changes",
    "turn_file_changes_page",
    "files_listed",
    "file_preview",
    "file_save_result",
    "preview_asset",
    "history_image",
    "preview_authorization_required",
    "preview_authorization_result",
    "dir_list",
    "agent_detail",
    "work_dashboard",
    "work_artifacts",
    "queued_query_updated",
    "rollback_result",
    "rate_limit_reset_result",
    "session_forked",
    "btw_opened",
    "btw_sync",
    "btw_closed",
    "takeover_state",
}


def clip(text: str) -> str:
    if len(text) <= MAX_BLOCK_CHARS:
        return text
    return text[: MAX_BLOCK_CHARS - len(TRUNCATED)] + TRUNCATED


def detail_sections(text, starts):
    """Keep typed section boundaries; never infer roles from model text."""
    return [dict(
        line=text.count("\n", 0, start), role=block.role,
        channel=block.channel, status=block.data.get("status"),
        is_error=block.data.get("is_error"),
    ) for start, block in starts if start < len(text)]


@dataclass
class Block:
    id: str
    role: str
    text: str = ""
    turn: str = ""
    channel: str = "unknown"
    seq: int = 0
    expanded: bool = False
    data: dict = field(default_factory=dict)


@dataclass
class SessionView:
    blocks: list[Block] = field(default_factory=list)
    # Local receipts are not native turns and must survive history rebuilds.
    pending_messages: dict[str, Block] = field(default_factory=dict)
    tab_terminal: tuple[str, str] | None = None
    tab_read_ids: list[str] = field(default_factory=list)
    draft: str = ""
    draft_cursor: tuple[int, int] = (0, 0)
    attachments: list[dict] = field(default_factory=list)
    pending_queued_text: dict[str, dict] = field(default_factory=dict)
    recovered_queued_text: list[str] = field(default_factory=list)
    pending_attachments: dict[str, tuple[str, list[dict]]] = field(
        default_factory=dict
    )
    artifact_epoch: int = 0
    # Stable block id + offset, not wrapped screen coordinates.
    anchor: tuple[str, int] = ("", 0)
    selection: tuple[str, int] = ("", 0)
    viewport: tuple[str, int] = ("", 0)
    follow: bool = True
    tail_hidden: bool = False
    state: str = "unknown"
    write_state: str = "unknown"
    control_generation: str | None = None
    control_revision: int = -1
    queue: list[dict] = field(default_factory=list)
    revision: str = ""
    continuity_revision: str | None = None
    pending_revision: str | None = None
    generation: str | None = None
    build_seq: int = -1
    oldest: str | None = None
    has_more: bool = False
    loading: bool = False
    active_turn: str = ""
    aliases: dict[str, str] = field(default_factory=dict)
    details: dict[str, str | None] = field(default_factory=dict)
    details_newer: dict[str, str | None] = field(default_factory=dict)
    collapsed_details: set[str] = field(default_factory=set)
    local_details: dict[str, Block] = field(default_factory=dict)
    tool_groups: dict[str, Block] = field(default_factory=dict)
    group_parents: dict[str, str] = field(default_factory=dict)
    detail_blocks: dict[str, list[Block]] = field(default_factory=dict)
    jump_back: list[tuple] = field(default_factory=list)
    jump_forward: list[tuple] = field(default_factory=list)
    version: int = 0
    presentation: SessionPresentation = field(
        default_factory=SessionPresentation
    )
    presentation_generation: str | None = None

    def tab_identity(self, identity: str) -> str:
        identity = self.aliases.get(identity, identity)
        if identity in self.presentation.turns:
            return identity
        for tid, turn in self.presentation.turns.items():
            if identity in {tid, turn.fork_id, turn.checkpoint_id}:
                return tid
        return identity

    def tab_badge(self) -> str | None:
        if self.state == "running":
            return "running"
        read = {self.tab_identity(identity) for identity in self.tab_read_ids}
        if self.tab_terminal:
            identity, status = self.tab_terminal
            if self.tab_identity(identity) not in read:
                return status
        receipt = self.presentation.completion
        identity = receipt.get("completion_id")
        if (receipt.get("unread") and identity
                and self.tab_identity(identity) not in read):
            return "completed"
        return None

    def mark_tab_read(self, *identities: str) -> None:
        for identity in identities:
            if identity:
                identity = self.tab_identity(identity)
                if identity not in self.tab_read_ids:
                    self.tab_read_ids.append(identity)
        del self.tab_read_ids[:-64]

    def read_tab(self) -> None:
        self.mark_tab_read(
            self.tab_terminal[0] if self.tab_terminal else "",
            self.presentation.completion.get("completion_id") or "",
        )

    def put(self, block: Block, *, append: bool = False) -> None:
        if block.role == "user":
            identity = block.id.removeprefix("user:")
            for msg_id in list(self.pending_messages):
                if self.aliases.get(msg_id, msg_id) == identity:
                    self.pending_messages.pop(msg_id)
        previous = next((b for b in self.blocks if b.id == block.id), None)
        if previous is not None:
            if block.role == "user" and "status" not in block.data:
                previous.data.pop("status", None)
            if block.role in {"tool", "process"}:
                # Native item IDs may first arrive as commentary scaffolding.
                # Once typed, the item must use tool folding, not prose layout.
                if previous.role not in {"tool", "process"}:
                    previous.expanded = False
                previous.role = block.role
                previous.channel = "unknown"
            previous.text = (previous.text if append else "") + block.text
            previous.text = clip(previous.text)
            previous.seq = max(previous.seq, block.seq)
            if block.channel != "unknown":
                previous.channel = block.channel
            previous.data.update(block.data)
            if previous.data.get("delivery") == "async":
                previous.channel = "commentary"
        else:
            block.text = clip(block.text)
            self.blocks.append(block)
        self.trim()
        self.version += 1

    def trim(self, *, older_page: bool = False) -> None:
        if len(self.blocks) <= MAX_BLOCKS:
            return
        protected = {self.anchor[0], self.selection[0], self.viewport[0]}
        dropped = self.blocks[:-MAX_BLOCKS]
        if older_page or (
            not self.follow and any(b.id in protected for b in dropped)
        ):
            # The server remains the canonical tail store. Keep the reading
            # window pinned; G fetches a fresh newest page before following.
            self.blocks = self.blocks[:MAX_BLOCKS]
            self.tail_hidden = True
        else:
            self.blocks = self.blocks[-MAX_BLOCKS:]
            if self.blocks[0].turn:
                self.oldest = self.blocks[0].turn
                self.has_more = True

    def control(self, value: dict) -> None:
        generation = value.get("generation")
        revision = value.get("revision", -1)
        if self.control_generation and not generation:
            return
        if (
            generation == self.control_generation
            and revision <= self.control_revision
        ):
            return
        self.control_generation = generation
        self.control_revision = revision
        self.write_state = value.get("write_state", "unknown")
        self.presentation.control = bounded(value)

    def block_header(self, block: Block, *, show_turn=False, now=None,
                     detail_key="Enter") -> str:
        title = {
            "user": "You",
            "assistant": "Assistant",
            "tool": "Tool",
            "process": "Activity",
            "detail": "Turn details",
            "tool_group": "工具活动",
        }.get(block.role, block.role)
        if block.role == "assistant":
            title = {
                "thinking": "Thinking",
                "commentary": "Progress",
                "final": "Assistant · final",
            }.get(block.channel, title)
            if (block.channel == "final"
                    and (turn := self.presentation.turns.get(block.turn))
                    and turn.status == "running"):
                title = "Assistant"
        if block.data.get("questions"):
            title = "Question · non-blocking"
        if block.data.get("nested") or block.role == "tool_group":
            title += " ▾" if block.expanded else " ▸"
        if block.data.get("status"):
            title += " · " + block.data["status"]
        if block.data.get("duration_ms") is not None:
            title += " · " + duration(block.data["duration_ms"])
        if block.data.get("exit_code") is not None:
            title += f" · exit {block.data['exit_code']}"
        suffix = ""
        turn = self.presentation.turns.get(block.turn)
        if show_turn and turn:
            suffix = f"  [{timestamp(turn.started)} · {turn.label(now)}]"
        if block.data.get("nested"):
            suffix += f"  [{detail_key}]"
        return f"── {title} ──{suffix}"

    def display_blocks(self) -> list[Block]:
        from cc_remote.tui_activity import project

        return project(self)

    def render(self, *, fold=True, detail_key="Enter", older_key="o", newer_key="O",
               close_key="Esc") -> tuple[str, list[tuple[int, Block]]]:
        parts: list[str] = []
        starts: list[tuple[int, Block]] = []
        offset = 0
        seen_turns: set[str] = set()
        for block in self.display_blocks() if fold else self.blocks:
            starts.append((offset, block))
            content = _safe_remote_text(block.text)
            page_hint = ""
            if block.data.get("questions"):
                content = question_text(block.data["questions"], block.text)
            if block.role in {"tool", "process"} and not block.expanded:
                content = content.split("\n", 1)[0][:160] + f"  [{detail_key}: details]"
            if (block.role == "detail" and not block.expanded
                    and not block.data.get("nested")):
                content = f"{detail_key}: show this turn's details"
            elif block.role == "detail" and block.expanded:
                paging = []
                if self.details.get(block.turn):
                    paging.append(f"{older_key}: load older detail page")
                if self.details_newer.get(block.turn):
                    paging.append(f"{newer_key}: load newer detail page")
                if paging:
                    page_hint = " · ".join(paging)
                    page_hint += f" · {detail_key}/{close_key}: collapse"
                    content += "\n" + page_hint
            if block.role == "assistant" and block.channel == "thinking":
                if not block.expanded:
                    content = (
                        content.split("\n", 1)[0][:120] + f"  [{detail_key}: expand]"
                    )
            header = self.block_header(
                block, show_turn=block.turn not in seen_turns,
                detail_key=detail_key,
            )
            seen_turns.add(block.turn)
            if block.role == "tool_group":
                arrow = "▾" if block.expanded else "▸"
                status = block.data.get("status") or ""
                text = (
                    f"  {arrow} {_safe_remote_text(block.text)}"
                    + (f" · {status}" if status else "")
                    + f"  [{detail_key}]\n\n"
                )
            elif block.data.get("nested"):
                text = header + ("\n" + page_hint if page_hint else "") + "\n\n"
            else:
                text = f"{header}\n{content}\n\n"
            parts.append(text)
            offset += len(text)
        return "".join(parts), starts

    @staticmethod
    def locate(offset: int, starts: list[tuple[int, Block]]) -> tuple[str, int]:
        for start, block in reversed(starts):
            if start <= offset:
                return block.id, offset - start
        return "", 0

    def resolve(
        self,
        anchor: tuple[str, int],
        starts: list[tuple[int, Block]],
        length: int,
        *,
        fallback: int = 0,
    ) -> int:
        for prefix in ("user:", "detail:", "tools:history:"):
            if anchor[0].startswith(prefix):
                identity = anchor[0][len(prefix):]
                anchor = (prefix + self.aliases.get(identity, identity),
                          anchor[1])
                break
        visible_ids = {block.id for _, block in starts}
        visited = set()
        while anchor[0] not in visible_ids and anchor[0] in self.group_parents:
            if anchor[0] in visited:
                break
            visited.add(anchor[0])
            anchor = (self.group_parents[anchor[0]], 0)
        if anchor[0] not in visible_ids:
            original = next((b for b in self.blocks if b.id == anchor[0]), None)
            if original and "detail:" + original.turn in visible_ids:
                anchor = ("detail:" + original.turn, 0)
        for index, (start, block) in enumerate(starts):
            if block.id == anchor[0]:
                end = (
                    starts[index + 1][0] if index + 1 < len(starts) else length
                )
                return min(start + anchor[1], max(start, end - 1))
        return min(length, max(0, fallback))

    def restore_attachments(self, msg_id):
        pending = self.pending_attachments.pop(msg_id, None)
        if pending:
            self.attachments.extend(pending[1])
            self.version += 1

    def history(self, event: dict) -> None:
        self.loading = False
        if isinstance(event.get("control"), dict):
            self.control(event["control"])
        if not event.get("authoritative", True) or event.get("error"):
            return
        if (
            self.pending_revision
            and event.get("revision") != self.pending_revision
        ):
            return
        generation = event.get("generation")
        build_seq = event.get("build_seq", 0)
        if generation == self.generation and build_seq < self.build_seq:
            return
        if event.get("before") and self.revision != event.get("revision"):
            return
        same_revision = self.revision == event.get(
            "revision"
        ) and not event.get("reset")
        continuous = (
            generation is not None and generation == self.generation
            and event.get("continuity_revision") is not None
            and event["continuity_revision"]
            == (self.continuity_revision or self.revision)
        )
        discontinuous = bool(self.revision) and not event.get("before") and (
            generation != self.generation or not (same_revision or continuous)
        )
        if not same_revision:
            self.details.clear()
            self.details_newer.clear()
            self.detail_blocks.clear()
        if event.get("reset") or discontinuous:
            live_seq = event.get("live_seq")
            fresh = [b for b in self.blocks if (
                not event.get("reset") and generation == self.generation
                and live_seq is not None and b.seq > live_seq
            )]
            fresh_turns = {
                b.turn: self.presentation.turns[b.turn]
                for b in fresh if b.turn in self.presentation.turns
            }
            fresh_active = (
                self.presentation.active
                if self.presentation.active in fresh_turns else ""
            )
            fresh_plan = self.presentation.plan
            if not (
                fresh_plan and fresh_plan.get("turn_id") in fresh_turns
                and live_seq is not None
                and (fresh_plan.get("seq") or 0) > live_seq
            ):
                fresh_plan = None
            fresh_aliases = {
                alias: tid for alias, tid in self.aliases.items()
                if tid in fresh_turns
            }
            self.blocks.clear()
            self.details.clear()
            self.details_newer.clear()
            self.collapsed_details.clear()
            self.local_details.clear()
            self.tool_groups.clear()
            self.group_parents.clear()
            self.aliases.clear()
            self.oldest = None
            self.active_turn = ""
            self.presentation.turns.clear()
            self.presentation.active = ""
            self.presentation.plan = None
            self.presentation.retired_plans.clear()
            self.blocks.extend(fresh)
            self.presentation.turns.update(fresh_turns)
            self.active_turn = self.presentation.active = fresh_active
            self.presentation.plan = fresh_plan
            self.aliases.update(fresh_aliases)
            self.artifact_epoch += 1
        if not event.get("before"):
            self.tail_hidden = False
        self.pending_revision = None
        self.generation = generation
        self.build_seq = build_seq
        self.revision = event.get("revision", "")
        if not event.get("before"):
            self.continuity_revision = (
                event.get("continuity_revision") or self.revision
            )
        if (event.get("before") or not self.oldest
                or event.get("reset") or discontinuous):
            self.oldest = event.get("oldest_id")
            self.has_more = event.get("has_more", False)
        incoming: list[Block] = []
        history_plans: list[dict] = []
        covered: set[str] = set()
        live_seq = event.get("live_seq")
        for turn in event.get("turns", []):
            tid = turn["id"]
            for msg_id in (tid, turn.get("clientMsgId")):
                self.pending_attachments.pop(msg_id, None)
            covered.add(tid)
            for alias in (turn.get("clientMsgId"), turn.get("forkPointId")):
                if alias:
                    self.aliases[alias] = tid
                    self.presentation.bind(alias, tid)
                    covered.add(alias)
                    for attr in ("anchor", "selection", "viewport"):
                        anchor = getattr(self, attr)
                        if anchor[0] == "user:" + alias:
                            setattr(self, attr, ("user:" + tid, anchor[1]))
            current = self.presentation.turns.get(tid)
            live_seq = event.get("live_seq")
            if current is None or (
                live_seq is not None and current.seq <= live_seq
            ):
                native = turn.get("forkPointId") or tid
                continuing = native in event.get(
                    "compaction_continuation_turn_ids", []
                )
                done = turn.get("done") and not continuing
                current = TurnDisplay(
                    fork_id=turn.get("forkPointId"),
                    checkpoint_id=turn.get("checkpointId"),
                    started=(turn.get("ts") or 0) / 1000 or None,
                    ended=((turn.get("doneTs") or 0) / 1000 or None)
                    if done
                    else None,
                    duration_ms=turn.get("durationMs") if done else None,
                    status=(
                        "interrupted"
                        if turn.get("interrupted")
                        else "failed"
                        if turn.get("error")
                        else "completed"
                    )
                    if done
                    else "running",
                )
                self.presentation.turns[tid] = current
            if (
                turn.get("prompt")
                or turn.get("images")
                or turn.get("imageRefs")
                or turn.get("files")
            ):
                prompt = self.with_attachments(turn.get("prompt", ""), turn)
                incoming.append(Block("user:" + tid, "user", prompt, tid))
                for msg_id in list(self.pending_messages):
                    if self.aliases.get(msg_id, msg_id) == tid:
                        self.pending_messages.pop(msg_id)
                completed_at = (self.presentation.goal or {}).get("updatedAt")
                if (
                    self.presentation.goal
                    and self.presentation.goal.get("status") == "complete"
                    and completed_at is not None
                    and turn.get("ts", 0) > completed_at * 1000
                ):
                    self.presentation.retired_goal = self.presentation.goal_id
            for index, block in enumerate(turn.get("blocks", [])):
                if block.get("kind") == "text":
                    text = block.get("text", "")
                    data = message_metadata(block)
                    incoming.append(
                        Block(
                            block.get("message_id") or f"{tid}:{index}",
                            "assistant",
                            text,
                            tid,
                            ("commentary" if data.get("delivery") == "async"
                             else block.get("channel", "unknown")),
                            data=data,
                        )
                    )
                elif block.get("kind") in {"process", "tool"}:
                    data = bounded(block)
                    data.update(data.get("result") or {})
                    identity = (
                        block.get("item_id")
                        or block.get("tool_use_id")
                        or f"{tid}:{index}"
                    )
                    content = str(
                        block.get("title")
                        or block.get("tool")
                        or block.get("processKind")
                    )
                    content += "\n" + describe(data)
                    incoming.append(
                        Block(identity, block["kind"], content, tid, data=data)
                    )
                    if (
                        block.get("plan")
                        and tid not in self.presentation.retired_plans
                    ):
                        history_plans.append(
                            {
                                **data,
                                "turn_id": tid,
                                "done": current.status != "running",
                                "status": current.status,
                            }
                        )
            if turn.get("detailEventCount") or turn.get("detailReasons"):
                incoming.append(
                    Block(
                        "detail:" + tid,
                        "detail",
                        "Load this turn's details",
                        tid,
                    )
                )
            if turn.get("error"):
                incoming.append(
                    Block("error:" + tid, "error", turn["error"], tid)
                )
        if not event.get("turns") and event.get("detail") == "full":
            # Old-compatible event pages remain useful to embedded relays.
            for item in event.get("events", []):
                self.event(item)
            return
        old = self.blocks
        if history_plans:
            current_plan = self.presentation.plan
            candidate = history_plans[-1]
            can_replace = not current_plan or (
                not event.get("before")
                and (
                    (
                        live_seq is not None
                        and (current_plan.get("seq") or 0) <= live_seq
                    )
                    or (
                        not current_plan.get("seq")
                        and current_plan.get("turn_id") in covered
                    )
                )
            )
            if can_replace:
                self.presentation.plan = candidate
        for block in old:
            if block.id.startswith("user:"):
                identity = block.id[5:]
                block.id = "user:" + self.aliases.get(identity, identity)
            block.turn = self.aliases.get(block.turn, block.turn)
        for alias, tid in self.aliases.items():
            if alias == tid:
                continue
            if alias in self.local_details:
                outer = self.local_details.pop(alias)
                outer.id, outer.turn = "detail:" + tid, tid
                self.local_details.setdefault(tid, outer)
            if alias in self.collapsed_details:
                self.collapsed_details.discard(alias)
                self.collapsed_details.add(tid)
            if alias in self.detail_blocks:
                self.detail_blocks.setdefault(
                    tid, [replace(b, turn=tid)
                          for b in self.detail_blocks.pop(alias)],
                )
            if alias in self.details:
                self.details.setdefault(tid, self.details.pop(alias))
            if alias in self.details_newer:
                self.details_newer.setdefault(tid, self.details_newer.pop(alias))
        # A history read begun before newer live deltas must not roll them back.
        fresh = {
            b.id: b
            for b in old
            if b.seq and (live_seq is None or b.seq > live_seq)
        }
        retained_details = {
            b.id: b
            for b in old
            if same_revision and b.role == "detail" and b.turn in self.details
        }
        incoming = [retained_details.get(b.id, b) for b in incoming]
        incoming = [fresh.get(b.id, b) for b in incoming]
        ids = {b.id for b in incoming}
        remaining = [
            b
            for b in old
            if b.id not in ids and (b.turn not in covered or b.id in fresh)
        ]
        self.blocks = (
            incoming + remaining
            if event.get("before")
            else [b for b in remaining if b.id not in fresh]
            + incoming
            + [b for b in remaining if b.id in fresh]
        )
        for fence in event.get("terminal_fences", []):
            tid = self.aliases.get(fence["turn_id"], fence["turn_id"])
            if fence["turn_id"] in event.get(
                "compaction_continuation_turn_ids", []
            ):
                continue
            turn = self.presentation.turns.get(tid)
            if turn:
                turn.status = fence["status"]
                turn.duration_ms = fence.get("duration_ms", turn.duration_ms)
                turn.ended = fence.get("completed_at", turn.ended)
                if (
                    self.presentation.plan
                    and self.presentation.plan.get("turn_id") == tid
                ):
                    self.presentation.plan.update(done=True, status=turn.status)
        plan = self.presentation.plan
        if event.get("turns") and not event.get("before"):
            active = self.presentation.turns.get(self.presentation.active)
            if active is None or (
                live_seq is not None and active.seq <= live_seq
            ):
                newest = event["turns"][-1]["id"]
                self.presentation.active = newest
                if not self.active_turn:
                    self.active_turn = newest
        order = [b.turn for b in self.blocks if b.role == "user"]
        if (
            plan
            and plan.get("turn_id") in order
            and (plan.get("done") or self.presentation.plan_terminal())
        ):
            if order.index(plan["turn_id"]) < len(order) - 1:
                self.presentation.retired_plans.add(plan["turn_id"])
                self.presentation.plan = None
        # Pagination retains the page just requested, not only the live tail.
        self.trim(older_page=bool(event.get("before")))
        for block in self.blocks:
            block.text = clip(block.text)
        self.version += 1

    @staticmethod
    def with_attachments(prompt: str, event: dict) -> str:
        attachments = [
            str(f.get("filename", "file")) for f in event.get("files") or []
        ]
        for index, _ in enumerate(
            event.get("images") or event.get("imageRefs") or []
        ):
            attachments.append(f"Image {index + 1} [open in Web]")
        return prompt + (
            "\nAttachments: " + ", ".join(attachments) if attachments else ""
        )

    def event(self, event: dict) -> None:
        kind = event.get("type")
        if kind in {"user_msg", "turn_steered"}:
            for key in ("msg_id", "client_msg_id"):
                self.pending_attachments.pop(event.get(key), None)
                self.pending_queued_text.pop(event.get(key), None)
        elif kind == "query_queue":
            for item in event.get("items", []):
                self.pending_attachments.pop(item.get("msg_id"), None)
                self.pending_queued_text.pop(item.get("msg_id"), None)
        elif kind == "error":
            for msg_id, receipt in list(self.pending_queued_text.items()):
                if (msg_id == event.get("msg_id")
                        or receipt["cmd_id"] == event.get("request_id")):
                    receipt["rejected"] = True
                    self.pending_queued_text.pop(msg_id, None)
                    self.recovered_queued_text.append(receipt["prompt"])
                    self.version += 1
            for msg_id, (cmd_id, _) in list(self.pending_attachments.items()):
                if (msg_id == event.get("msg_id")
                        or cmd_id == event.get("request_id")):
                    self.restore_attachments(msg_id)
        if kind in {"user_msg", "turn_steered"}:
            for key in ("msg_id", "client_msg_id"):
                self.pending_messages.pop(event.get(key), None)
        elif kind == "error" and event.get("msg_id") in self.pending_messages:
            pending = self.pending_messages.pop(event["msg_id"])
            pending.data["status"] = "failed"
            self.put(pending)
        if (
            kind == "replay_start"
            and (event.get("sid") or "").startswith("btw-")
            and event.get("rebuild")
        ):
            self.blocks.clear()
            self.details.clear()
            self.details_newer.clear()
            self.detail_blocks.clear()
            self.local_details.clear()
            self.tool_groups.clear()
            self.group_parents.clear()
            self.collapsed_details.clear()
            self.aliases.clear()
            self.active_turn = ""
            self.presentation.turns.clear()
            self.presentation.active = ""
            self.presentation.plan = None
            self.presentation.retired_plans.clear()
            self.version += 1
        if kind in {"snapshot", "replay_start"} and event.get("generation"):
            generation = event["generation"]
            if (
                self.presentation_generation
                and generation != self.presentation_generation
            ):
                self.presentation.completion.clear()
                self.presentation.questions.clear()
                self.presentation.reports.clear()
                self.presentation.rates.clear()
                self.presentation.live_rate_revision = 0
                self.write_state = "unknown"
            self.presentation_generation = generation
        seq = event.get("seq") or 0
        tid = event.get("turn_id") or self.active_turn
        tid = self.aliases.get(tid, tid)
        if kind == "turn_binding":
            old, new = event["msg_id"], event["turn_id"]
            self.aliases[old] = new
            self.presentation.bind(old, new)
            for block in self.blocks:
                if block.turn == old:
                    block.turn = new
            if self.active_turn == old:
                self.active_turn = new
        elif kind == "user_msg":
            tid = event.get("client_msg_id") or event.get("msg_id", "")
            tid = self.aliases.get(tid, tid)
        elif kind == "turn_end" and tid not in self.presentation.turns:
            checkpoint = event.get("checkpoint_id")
            if checkpoint:
                tid = self.aliases.get(checkpoint, checkpoint)
            elif (
                self.active_turn in self.presentation.turns
                and self.active_turn not in self.aliases.values()
            ):
                # Claude's terminal assistant UUID differs from its user UUID.
                # Never apply this fallback to an already-bound Codex owner.
                tid = self.active_turn
        self.presentation.event(event, tid)
        if kind == "turn_end" and tid and tid == self.active_turn:
            turn = self.presentation.turns.get(tid)
            subtype = (event.get("result") or {}).get("subtype")
            if turn and subtype not in {"steered", "compacted"}:
                self.tab_terminal = (tid, turn.status)
        elif kind == "completion_state" and not self.presentation.completion.get(
            "unread"
        ):
            self.mark_tab_read(
                self.presentation.completion.get("completion_id") or ""
            )
        if kind in {"user_msg", "turn_steered"}:
            identity = event.get("client_msg_id") or event.get("msg_id", "")
            identity = self.aliases.get(identity, identity)
            if kind == "user_msg":
                if identity != self.active_turn:
                    self.read_tab()
                    self.tab_terminal = None
                self.active_turn = identity
            self.put(
                Block(
                    "user:" + identity,
                    "user",
                    self.with_attachments(event.get("prompt", ""), event),
                    tid if kind == "turn_steered" else identity,
                    seq=seq,
                )
            )
        elif kind in {"assistant_msg_start", "delta", "assistant_msg_end"}:
            identity = event.get("message_id", "")
            data = message_metadata(event)
            self.put(
                Block(
                    identity,
                    "assistant",
                    event.get("text", ""),
                    tid,
                    ("commentary" if data.get("delivery") == "async"
                     else event.get("channel", "unknown")),
                    seq,
                    data=data,
                ),
                append=True,
            )
        elif kind == "tool_use":
            inputs = bounded(event.get("input") or {})
            if len(json.dumps(inputs, ensure_ascii=False)) > MAX_BLOCK_CHARS:
                inputs = {}
            content = (
                str(event.get("title") or event.get("tool", "tool"))
                + "\n"
                + json.dumps(
                    event.get("input", {}), ensure_ascii=False, indent=2
                )
            )
            self.put(
                Block(
                    event.get("tool_use_id", ""),
                    "tool",
                    content,
                    tid,
                    seq=seq,
                    data={
                        "status": "running",
                        "category": event.get("category"),
                        "tool": event.get("tool"),
                        "input": inputs,
                    },
                )
            )
        elif kind in {"tool_result", "tool_delta"}:
            content = event.get("content", event.get("delta", ""))
            self.put(
                Block(
                    event.get("tool_use_id", ""),
                    "tool",
                    "\n" + str(content),
                    tid,
                    seq=seq,
                    data=(
                        {
                            "status": event.get("status")
                            or (
                                "failed"
                                if event.get("is_error")
                                else "succeeded"
                            ),
                            "duration_ms": event.get("duration_ms"),
                            "exit_code": event.get("exit_code"),
                        }
                        if kind == "tool_result"
                        else {}
                    ),
                ),
                append=True,
            )
            if kind == "tool_result":
                self.put(
                    Block(
                        event.get("tool_use_id", ""),
                        "tool",
                        "\n"
                        + str(event.get("diff") or event.get("summary") or ""),
                        tid,
                        seq=seq,
                    ),
                    append=True,
                )
        elif kind in {"process", "turn_plan", "turn_diff"}:
            identity = event["item_id"]
            previous = next((b for b in self.blocks if b.id == identity), None)
            # Native serialized events contain nulls for absent fields. An end
            # event without output must not erase already-streamed output.
            updates = bounded({k: v for k, v in event.items() if v is not None})
            if previous and updates.get("status") == "unknown":
                updates.pop("status")
            data = {**(previous.data if previous else {}), **updates}
            if event.get("append_to") and event.get("delta"):
                target = event["append_to"]
                data[target] = clip(
                    str((previous.data if previous else {}).get(target) or "")
                    + event["delta"]
                )
            title = str(
                data.get("title")
                or ("Plan" if kind == "turn_plan" else "Changes")
            )
            content = (
                title
                + "\n"
                + describe(
                    {
                        k: v
                        for k, v in data.items()
                        if k
                        not in {"v", "seq", "sid", "type", "delta", "append_to"}
                    }
                )
            )
            self.put(
                Block(identity, "process", content, tid, seq=seq, data=data)
            )
        elif kind in {"state", "snapshot", "session_activity"}:
            self.state = event.get("state", self.state)
            if isinstance(event.get("control"), dict):
                self.control(event["control"])
        elif kind == "session_control":
            self.control(event)
        elif kind == "query_queue":
            self.queue = event.get("items", [])
        elif kind == "error":
            self.put(
                Block(
                    "error:" + str(seq),
                    "error",
                    event.get("message", "Unknown error"),
                    tid,
                    seq=seq,
                )
            )
        elif kind == "turn_end":
            self.version += 1


class WorkspaceState:
    def __init__(self) -> None:
        self.views: dict[str, SessionView] = {}
        self.catalog: dict[str, dict] = {}
        self.rekeys: dict[str, str] = {}
        self.reports: dict[str, dict] = {}
        self.connection = "connecting"
        self.btw_revision = -1
        self.btw_generation = None
        self.generation = None

    def view(self, sid: str) -> SessionView:
        sid = self.rekeys.get(sid, sid)
        if sid not in self.views:
            self.views[sid] = SessionView()
        return self.views[sid]

    def _reconcile_side_chats(self) -> None:
        # Hello sends BTW before parent catalogs. Refresh derived metadata
        # after either arrives, and after parent renames or cwd migrations.
        for sid, row in self.catalog.items():
            if not sid.startswith("btw-"):
                continue
            parent = self.catalog.get(row.get("parent_sid"))
            if parent is not None:
                row.update(
                    space=parent.get("space", "code"),
                    cwd=parent.get("cwd"),
                    summary="BTW · " + (
                        parent.get("summary") or row["parent_sid"]
                    ),
                )

    def event(self, event: dict) -> None:
        kind = event.get("type")
        generation = event.get("generation")
        if kind == "queued_query_detail":
            return  # Full queued prompts belong only to the one-shot editor.
        if generation and kind in {"snapshot", "replay_start", "btw_sync"}:
            if self.generation and generation != self.generation:
                # A wrapper epoch also resets cold sessions' receipt domains;
                # those sessions may receive only a catalog, never a Snapshot.
                for resident in self.views.values():
                    resident.presentation.completion.clear()
                    resident.presentation.questions.clear()
                    resident.presentation.rates.clear()
                    resident.presentation.live_rate_revision = 0
                    resident.presentation.control.clear()
                    resident.state = resident.write_state = "unknown"
            self.generation = generation
        if kind in {"btw_sync", "btw_opened", "btw_closed"}:
            old_sides = {sid for sid in self.catalog if sid.startswith("btw-")}
            generation = event.get("generation", self.btw_generation)
            if generation != self.btw_generation:
                self.btw_revision = -1
                self.btw_generation = generation
            if event.get("revision", 0) < self.btw_revision:
                return
            self.btw_revision = event.get("revision", 0)
            if kind == "btw_sync":
                self.catalog = {
                    k: v
                    for k, v in self.catalog.items()
                    if not k.startswith("btw-")
                }
                sessions = event.get("sessions", [])
            elif kind == "btw_opened":
                sessions = [event]
            else:
                self.catalog.pop(event["btw_sid"], None)
                sessions = []
            for row in sessions:
                parent = self.catalog.get(row["parent_sid"], {})
                self.catalog[row["btw_sid"]] = {
                    "session_id": row["btw_sid"],
                    "summary": "BTW · "
                    + (parent.get("summary") or row["parent_sid"]),
                    "parent_sid": row["parent_sid"],
                    "engine": row["engine"],
                    "cwd": parent.get("cwd"),
                    "state": row.get("state", "idle"),
                    "space": parent.get("space", "code"),
                }
            for sid in old_sides - self.catalog.keys():
                self.view(sid).write_state = "unavailable"
            self._reconcile_side_chats()
        if kind in {"wrapper_disconnected", "wrapper_reconnected"}:
            self.connection = (
                "wrapper offline"
                if kind == "wrapper_disconnected"
                else "connected"
            )
        if kind == "session_migrated":
            sid = event["session_id"]
            if sid in self.catalog:
                self.catalog[sid]["cwd"] = event["cwd"]
                self._reconcile_side_chats()
            self.view(sid).presentation.reports.clear()
        if kind in REPORT_TYPES:
            sid = event.get("sid") or event.get("session_id")
            reports = (
                self.view(sid).presentation.reports if sid else self.reports
            )
            reports[str(kind)] = bounded(event)
            return
        if kind == "session_list":
            engine = event.get("engine", "claude")
            previous = set(self.catalog)
            profile_key = engine + "_profile_id"
            profiles_key = engine + "_profiles"
            metadata = dict(self.reports.get(profiles_key, {}))
            for key in (profiles_key, "default_" + profile_key):
                if event.get(key):
                    metadata[key] = event[key]
            self.reports[profiles_key] = bounded(metadata)
            unavailable = {
                profile["id"] for profile in metadata.get(profiles_key, [])
                if profile.get("error")
            }
            self.catalog = {
                sid: row
                for sid, row in self.catalog.items()
                if sid.startswith("btw-")
                or (row.get("engine", "claude"), row.get("space", "code"))
                != (engine, event.get("space", "code"))
                or (not row.get("provisional_fork")
                    and row.get(profile_key) in unavailable)
            }
            for row in event.get("sessions", []):
                row = {
                    **row,
                    "engine": row.get("engine") or engine,
                    "space": row.get("space") or event.get("space", "code"),
                }
                self.catalog[row["session_id"]] = row
                view = self.view(row["session_id"])
                view.presentation.engine = row.get("engine", engine)
                if row.get("completion_revision") is not None:
                    view.presentation.event(
                        {
                            "type": "completion_state",
                            "completion_id": row.get("completion_id"),
                            "unread": row.get("completion_unread", False),
                            "revision": row["completion_revision"],
                        },
                        "",
                    )
                if view.state == "unknown" and row.get("state"):
                    view.state = row["state"]
            for sid in previous - self.catalog.keys():
                self.view(sid).write_state = "unavailable"
            self._reconcile_side_chats()
            return
        if kind == "session_rekey":
            old, new = event.get("old_key"), event.get("session_id")
            if old in self.views and new:
                self.views[new] = self.views.pop(old)
                self.rekeys[old] = new
            if old in self.catalog:
                self.catalog[new] = {**self.catalog.pop(old), "session_id": new}
            return
        sid = (
            event.get("session_id")
            if kind
            in {
                "history",
                "turn_detail",
                "session_activity",
                "history_invalidated",
            }
            else event.get("sid")
        )
        if not sid:
            return
        view = self.view(sid)
        if kind == "history_invalidated":
            # A rollback barrier must invalidate the visible projection before
            # a late pre-rollback page can arrive.
            view.blocks.clear()
            view.details.clear()
            view.details_newer.clear()
            view.detail_blocks.clear()
            view.local_details.clear()
            view.tool_groups.clear()
            view.group_parents.clear()
            view.collapsed_details.clear()
            view.aliases.clear()
            view.presentation.turns.clear()
            view.presentation.active = ""
            view.active_turn = ""
            view.presentation.plan = None
            view.presentation.retired_plans.clear()
            view.presentation.reports.clear()
            view.revision = event["revision"]
            view.pending_revision = event["revision"]
            view.version += 1
        elif kind == "artifact_invalidated":
            view.presentation.reports.clear()
            view.artifact_epoch += 1
            view.version += 1
        elif kind == "history":
            view.history(event)
        elif kind == "turn_detail":
            if (
                event.get("revision") != view.revision
                or not event.get("authoritative", True)
                or event.get("error")
            ):
                return
            if event.get("reset_required"):
                view.details.pop(event["turn_id"], None)
                view.details_newer.pop(event["turn_id"], None)
                view.detail_blocks.pop(event["turn_id"], None)
                view.version += 1
                return
            turn = event["turn_id"]
            detail_view = SessionView(active_turn=turn)
            for item in event.get("events", []):
                detail_view.event(item)
            for block in detail_view.blocks:
                block.expanded = True
            text, starts = detail_view.render(fold=False)
            cursor = (
                event.get("oldest_cursor") if event.get("has_more") else None
            )
            view.details[turn] = cursor
            view.details_newer[turn] = (
                event.get("newer_cursor") if event.get("has_newer") else None
            )
            # Bound typed pages as well as the legacy flattened representation.
            # Nested folds must not introduce an unbounded raw event cache.
            children, remaining = [], MAX_BLOCK_CHARS
            for block in detail_view.blocks:
                metadata = dict(block.data)
                cost = len(json.dumps(metadata, ensure_ascii=False))
                if cost > remaining // 2:
                    metadata = {}
                    cost = 2
                room = max(0, remaining - cost)
                if len(block.text) > room:
                    body = block.text[:max(0, room - len(TRUNCATED))]
                    children.append(replace(block, text=body + TRUNCATED,
                                            data=metadata))
                    break
                children.append(replace(block, data=metadata))
                remaining -= cost + len(block.text)
                if remaining < len(TRUNCATED):
                    break
            view.detail_blocks[turn] = children
            view.put(Block("detail:" + turn, "detail", text, turn,
                           data={"sections": detail_sections(clip(text), starts)}))
            for block in view.blocks:
                if block.id == "detail:" + turn:
                    block.expanded = turn not in view.collapsed_details
        else:
            view.event(event)
