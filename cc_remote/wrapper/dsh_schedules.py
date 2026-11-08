"""Host-owned DSH schedules; management reads never activate a session."""
from __future__ import annotations

import os
import hashlib
import json

from cc_remote.protocol import DshItem
from cc_remote.wrapper.dsh_client import DshError, native_session_id, wire_session_id

FIELDS = ("id", "kind", "title", "prompt", "scheduledAt", "afterSeconds", "everySeconds",
          "time", "timeZone", "weekdays", "expression", "rrule", "updatedAt", "executionActive", "deliveryFloorEndsAt")


def visible(runtime, row):
    private = runtime.sidechats.check()
    target = row.get("target", {})
    ids = [*row.get("involvedSessionIds", []), target.get("sessionId")]
    return not any(sid and wire_session_id(sid) in private for sid in ids)


async def catalog(runtime):
    rows = await runtime.client.rpc("schedule/catalog")
    if not isinstance(rows, list):
        raise DshError("schedule_invalid", "DSH 任务列表格式不兼容。")
    return [row for row in rows if visible(runtime, row)]


def item(row):
    target = row["target"]
    # new_once reserves an id before any Session exists. It is not a clickable session yet.
    sid = wire_session_id(target["sessionId"]) if target["kind"] == "session" else None
    data = {key: row[key] for key in FIELDS if key in row}
    data["updatedAt"] = row.get("updatedAt")
    data["target"] = {"kind": target["kind"]}
    if sid:
        data["target"]["sid"] = sid
    if target.get("create"):
        data["target"]["create"] = {k: target["create"][k] for k in ("cwd", "agentPreset") if k in target["create"]}
    return DshItem(id=row["id"], sid=sid, title=row["title"], detail=row["prompt"][:65536],
                   state=row["status"], data=data)


async def read(runtime, cmd, response):
    rows = await catalog(runtime)
    if cmd.kind == "schedules" and cmd.query == "cleanup":
        ended = [row for row in rows if row.get("status") == "ended"]
        return [DshItem(id="cleanup", data={"count": len(ended), "revision": cleanup_revision(ended)})]
    if cmd.kind == "schedules":
        offset = cmd.before_seq or 0
        response.has_more = len(rows) > offset + 128
        response.next_seq = offset + 128 if response.has_more else None
        return [item(row) for row in rows[offset:offset + 128]]
    if not any(row["id"] == cmd.query for row in rows):
        raise DshError("not_found", "任务不存在或当前不可见，请刷新。")
    value = await runtime.client.rpc("schedule/history", {"request": {"id": cmd.query, "limit": 30}})
    if value.get("code"):
        raise DshError("schedule_history", "投递记录暂不可用，请刷新任务列表。")
    private = runtime.sidechats.check()
    result = []
    for index, record in enumerate(value.get("records", [])[:30]):
        sid = wire_session_id(record["targetSessionId"]) if record.get("targetSessionId") else None
        if sid in private:
            continue
        result.append(DshItem(id=str(index), sid=sid, title=str(record.get("deliveredAt") or record.get("scheduledAt") or "投递记录"),
                             state="delivered", detail=str(record.get("messageId") or "")))
    return result


async def target(runtime, value):
    if not isinstance(value, dict) or value.get("kind") not in {"session", "new_once", "new"}:
        raise DshError("invalid_control", "请选择任务执行位置。")
    kind = value["kind"]
    if kind == "session":
        sid = value.get("sid")
        native = native_session_id(sid)
        rows = await runtime.client.list_sessions()
        if (sid in runtime.sidechats.check() or not any(row["sessionId"] == native
                and row.get("origin") != "subagent" for row in rows)):
            raise DshError("private_session", "请选择当前设备上的普通会话。")
        return {"kind": kind, "sessionId": native}
    create = value.get("create", {})
    if not isinstance(create, dict) or set(create) - {"cwd", "agentPreset"}:
        raise DshError("invalid_control", "新会话配置无效。")
    if any(not isinstance(v, str) or not v or "\0" in v or len(v) > 4096 for v in create.values()):
        raise DshError("invalid_control", "新会话配置无效。")
    if create.get("cwd") and not os.path.isabs(create["cwd"]):
        raise DshError("invalid_control", "项目路径必须为设备上的绝对路径。")
    return {"kind": kind, "create": create}


def fields(value, *, creating=False):
    allowed = {"title", "prompt", "timing", "target"}
    if not isinstance(value, dict) or not value or set(value) - allowed:
        raise DshError("invalid_control", "任务配置无效。")
    for name, limit in (("title", 120), ("prompt", 24000)):
        if name in value or creating:
            text = value.get(name)
            if not isinstance(text, str) or not text.strip() or len(text) > limit:
                raise DshError("invalid_control", "请填写有效的任务名称和内容。")
    if "timing" in value or creating:
        timing = value.get("timing")
        if not isinstance(timing, dict) or timing.get("kind") not in {"after", "at", "every", "daily", "weekly", "cron", "rrule"}:
            raise DshError("invalid_control", "请选择有效的执行时间规则。")
    return dict(value)


async def manage(runtime, body):
    action = body["action"]
    if action == "schedule_cleanup":
        rows = await runtime.client.rpc("schedule/catalog")
        ended = [row for row in rows if row.get("status") == "ended"]
        allowed = [row for row in ended if visible(runtime, row)]
        if body.get("revision") != cleanup_revision(allowed):
            raise DshError("schedule_conflict", "已结束任务清单已变化，请重新读取后确认。")
        if not allowed:
            return "没有需要清理的已结束任务。"
        if any(row.get("executionActive") for row in allowed):
            raise DshError("schedule_busy", "部分已结束任务仍在投递，请等待投递完成后重新读取清单。")
        # Freeze the confirmed identities/revisions before the first write.
        # deleteInactive re-enumerates the global ended set inside the Host and
        # could also remove a newly ended (including private) task. edit checks
        # each observed revision in the same native transaction as its deletion.
        confirmed = tuple((row["id"], row.get("updatedAt")) for row in allowed)
        deleted = 0
        for identity, revision in confirmed:
            try:
                value = await runtime.client.rpc("schedule/edit", {"request": {
                    "id": identity, "expectedUpdatedAt": revision, "action": "delete"}})
                if not isinstance(value, dict):
                    raise DshError("schedule_unknown", "本项清理结果未确认。", outcome_unknown=True)
                if value.get("code"):
                    raise DshError("schedule_conflict", "本项任务已变化或未能删除。")
                if value.get("id") != identity or value.get("deleted") is not True:
                    raise DshError("schedule_unknown", "本项清理结果未确认。", outcome_unknown=True)
            except DshError as exc:
                # Persist the partial/unknown result through the existing whole
                # command receipt. Never resume a partially applied batch on retry.
                raise DshError(exc.code, f"已确认清理 {deleted} 个任务；{exc}后续任务未执行，请重新读取核实；不会自动重试。",
                               outcome_unknown=exc.outcome_unknown) from None
            deleted += 1
        return f"已清理 {deleted} 个已结束任务；已有会话内容保留。"
    if action == "schedule_create":
        request = fields(body.get("request"), creating=True)
        request["target"] = await target(runtime, request.get("target"))
        value = await runtime.client.rpc("schedule/create", {"request": request})
        if not isinstance(value, dict) or not value.get("id"):
            raise DshError("schedule_unknown", "创建结果未确认，请刷新任务列表；不会自动重建。", outcome_unknown=True)
        return "定时任务已创建。"
    row = next((row for row in await catalog(runtime) if row["id"] == body.get("id")), None)
    if not row:
        raise DshError("not_found", "任务不存在或当前不可见，请刷新。")
    if action == "schedule_run":
        value = await runtime.client.rpc("schedule/run", {"request": {"id": row["id"]}})
        text = "执行指令已送达；完成情况请查看目标会话。"
    else:
        if "expectedUpdatedAt" not in body or body["expectedUpdatedAt"] != row.get("updatedAt"):
            raise DshError("schedule_conflict", "任务已被修改，请刷新后重试；编辑内容已保留。")
        request = {"id": row["id"], "expectedUpdatedAt": body["expectedUpdatedAt"], "action": "update"}
        if action == "schedule_delete":
            request["action"] = "delete"
            text = "任务及其投递记录已删除；会话内容仍保留。"
        elif action in {"schedule_pause", "schedule_resume"}:
            request["patch"] = {"active": action == "schedule_resume"}
            text = "任务已恢复。" if action == "schedule_resume" else "任务已暂停。"
        elif action == "schedule_update":
            request["patch"] = fields(body.get("patch"))
            if "target" in request["patch"]:
                request["patch"]["target"] = await target(runtime, request["patch"]["target"])
            text = "任务已更新。"
        else:
            raise DshError("unsupported", "任务操作尚未适配。")
        value = await runtime.client.rpc("schedule/edit", {"request": request})
    if not isinstance(value, dict):
        raise DshError("schedule_unknown", "任务操作结果未确认，请刷新核实。", outcome_unknown=True)
    if value.get("code"):
        messages = {"schedule_conflict": "任务已被修改，请刷新后重试；编辑内容已保留。",
                    "schedule_execution_pending": "任务仍在执行或尚未达到原生最短执行间隔，请刷新查看。"}
        raise DshError("schedule_refused", messages.get(value["code"], "任务操作未完成，请刷新任务列表。"))
    if value.get("id") != row["id"] or (action == "schedule_delete" and value.get("deleted") is not True):
        raise DshError("schedule_unknown", "任务操作结果未确认，请刷新核实。", outcome_unknown=True)
    return text


def cleanup_revision(rows):
    return hashlib.sha256(json.dumps(sorted(
        [(row["id"], row.get("updatedAt"), bool(row.get("executionActive"))) for row in rows],
        key=lambda row: row[0]), ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
