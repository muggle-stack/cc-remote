"""Two-level activity projection; original narrative blocks remain intact."""

from collections import Counter
from dataclasses import replace


def activity(block):
    return not block.data.get("questions") and (
        block.role in {"tool", "process"}
        or block.channel in {"commentary", "thinking"}
    )


def tool_item(block):
    return block.role in {"tool", "process"} or block.channel == "thinking"


def summary(items):
    tools, other, files = Counter(), 0, set()
    running = failed = False
    for block in items:
        data = block.data
        kind = data.get("kind") or data.get("processKind")
        name = data.get("tool")
        if block.role == "tool" or name or kind in {"command", "file_change"}:
            tools[str(name or data.get("title") or kind or "tool")[:80]] += 1
        else:
            other += 1
        running |= data.get("status") in {"running", "pending", "in_progress"}
        failed |= bool(data.get("is_error")) or data.get("status") in {
            "failed",
            "error",
            "interrupted",
            "declined",
            "cancelled",
        }
        inputs = data.get("input") or {}
        if not isinstance(inputs, dict):
            continue
        if kind == "file_change" or str(name).lower() in {
            "edit",
            "write",
            "multiedit",
            "apply_patch",
            "notebookedit",
        }:
            paths = inputs.get("file_paths")
            for path in paths if isinstance(paths, list) else []:
                if isinstance(path, str):
                    files.add(path)
            changes = inputs.get("changes")
            for change in changes if isinstance(changes, list) else []:
                if isinstance(change, dict) and isinstance(
                    change.get("path"), str
                ):
                    files.add(change["path"])
            path = inputs.get("file_path") or inputs.get("path")
            if isinstance(path, str):
                files.add(path)
    parts = [f"{sum(tools.values())} 个工具调用"] if tools else []
    if files:
        parts.append(f"修改 {len(files)} 个文件")
    if other:
        parts.append(f"{other} 项活动")
    if tools:
        parts.extend(
            f"{name} ×{count}" for name, count in list(tools.items())[:8]
        )
        if len(tools) > 8:
            parts.append(f"另 {len(tools) - 8} 类")
    return " · ".join(parts), (
        "running" if running else "failed" if failed else "succeeded"
    )


def project(view):
    from cc_remote.tui_state import Block

    # Hydrated pages replace lightweight summaries, including truncated final
    # text. Never replace user messages; newer live rows win by sequence.
    source = list(view.blocks)
    retained = {b.turn for b in source}
    for tid, children in view.detail_blocks.items():
        if tid not in retained:
            continue
        anchor = next(
            (
                i
                for i, b in enumerate(source)
                if b.turn == tid
                and (b.channel == "final" or b.role == "detail")
            ),
            len(source),
        )
        # Merge against stable item IDs. A partial page containing just a tool
        # must not move that tool before its already-visible progress message.
        for child in reversed(children):
            if child.role in {"user", "detail"}:
                continue
            existing = next(
                (i for i, b in enumerate(source) if b.id == child.id), None
            )
            if existing is not None:
                if source[existing].seq <= child.seq:
                    source[existing] = child
                anchor = existing
            else:
                source.insert(anchor, child)

    # Summary pages place their lazy-detail marker after the answer. Present
    # that loader inside the activity section, before the corresponding final.
    markers = {b.turn: b for b in source if b.role == "detail"}
    moved = set()
    ordered = []
    for block in source:
        if block.channel == "final" and block.turn in markers:
            if block.turn not in moved:
                ordered.append(markers[block.turn])
                moved.add(block.turn)
        if block.role == "detail":
            if block.turn in moved:
                continue
            moved.add(block.turn)
        ordered.append(block)

    visible, parents = [], {}
    active_outer = None
    used_outer, used_tools = set(), set()
    run = []

    def outer_for(block):
        tid = block.turn
        outer = view.local_details.setdefault(
            tid,
            Block(
                "detail:" + tid,
                "detail",
                turn=tid,
                expanded=True,
                data={"local": True, "nested": True},
            ),
        )
        outer.data["nested"] = True
        if tid in view.collapsed_details:
            outer.expanded = False
        if tid not in used_outer:
            used_outer.add(tid)
            visible.append(outer)
        return outer

    def flush():
        if not run:
            return
        first = run[0]
        identity = "tools:" + first.id
        group = view.tool_groups.setdefault(
            identity,
            Block(identity, "tool_group", turn=first.turn),
        )
        group.turn = first.turn
        used_tools.add(identity)
        group.text, status = summary(run)
        turn = view.presentation.turns.get(first.turn)
        if status == "running" and turn and turn.status != "running":
            status = turn.status
        group.data.update(status=status, count=len(run))
        parents[identity] = active_outer.id if active_outer else ""
        for child in run:
            parents[child.id] = identity
        if not active_outer or active_outer.expanded:
            visible.append(group)
            if group.expanded:
                # Opening the second layer shows contents, not a third fold.
                visible.extend(replace(child, expanded=True) for child in run)
        run.clear()

    for block in ordered:
        if block.role == "detail":
            flush()
            active_outer = outer_for(block)
            identity = "tools:history:" + block.turn
            parents[identity] = active_outer.id
            if block.turn in view.detail_blocks:
                continue
            loader = view.tool_groups.setdefault(
                identity,
                Block(
                    identity,
                    "tool_group",
                    turn=block.turn,
                    text="载入历史进度和工具",
                    data={"request_detail": True},
                ),
            )
            used_tools.add(identity)
            if active_outer.expanded:
                visible.append(loader)
            continue
        if not activity(block):
            flush()
            active_outer = None
            visible.append(block)
            continue
        if active_outer is None or active_outer.turn != block.turn:
            flush()
            active_outer = outer_for(block)
        if tool_item(block):
            run.append(block)
        else:
            flush()
            parents[block.id] = active_outer.id
            if active_outer.expanded:
                visible.append(block)
    flush()
    view.group_parents = parents
    view.local_details = {
        k: v for k, v in view.local_details.items() if k in used_outer
    }
    view.tool_groups = {
        k: v for k, v in view.tool_groups.items() if k in used_tools
    }
    view.detail_blocks = {
        k: v for k, v in view.detail_blocks.items() if k in retained
    }
    return visible + list(view.pending_messages.values())
