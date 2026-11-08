"""Explicit offline acceptance for management, child history and native PTY."""
import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4

from cc_remote.protocol import CloseBtw, GetAgentDetail, OpenBtw, Query, ReadDsh, SetDshControl, SyncBtw


async def exercise(machine, runtime, sid, child):
    browser = "native-surfaces"
    async def read(kind, **fields):
        result = await machine._handle(ReadDsh(sid=sid, kind=kind, client_id=browser, cmd_id=uuid4().hex, **fields))
        assert not result.error, result
        return result
    async def control(body):
        result = await machine._handle(SetDshControl(sid=sid, kind="terminal", value=json.dumps(body), client_id=browser, cmd_id=uuid4().hex))
        assert result.status == "success", result
        return result
    for kind in ("settings", "presets", "bundles", "schedules", "workflows", "trajectory", "terminals"):
        result = await read(kind)
        print("PASS native surface", kind, len(result.items), flush=True)
    settings = await read("settings")
    row = next(item for item in settings.items if item.id == "subagent")
    result = await machine._handle(SetDshControl(sid=sid, kind="manage", client_id=browser, cmd_id=uuid4().hex,
        value=json.dumps({"action": "settings", "ns": "subagent", "revision": row.data["revision"], "patch": {"maxDepth": 2}})))
    assert result.status == "success", result
    actual = next(item for item in (await read("settings")).items if item.id == "subagent")
    assert actual.data["values"]["maxDepth"] == 2 and actual.data["applies"] == "live"
    print("PASS native settings effective-value receipt and readback", flush=True)
    detail = await machine._handle(GetAgentDetail(session_id=sid, run_id="dsh@" + child["id"],
        request_id=uuid4().hex, client_id=browser))
    assert detail.authoritative and not detail.error and detail.events, detail
    print("PASS native completed child process history", flush=True)
    async def manage(body, *, success=True):
        result = await machine._handle(SetDshControl(sid=sid, kind="manage", value=json.dumps(body),
            client_id=browser, cmd_id=uuid4().hex))
        assert (result.status == "success") == success, result
        return result
    timing = {"kind": "daily", "daily": {"time": "09:00:00", "time_zone": "Asia/Taipei"}}
    for destination in ({"kind": "session", "sid": sid}, {"kind": "new_once"}, {"kind": "new"}):
        title = "Offline " + destination["kind"]
        await manage({"action": "schedule_create", "request": {
            "title": title, "prompt": "hi", "timing": timing, "target": destination}})
        task = next(row for row in (await read("schedules")).items if row.title == title)
        assert task.data["timeZone"] == "Asia/Taipei" and task.data["target"]["kind"] == destination["kind"]
        if destination["kind"] != "session":
            assert not task.sid  # reserved new_once identity is not a real Session
        stale = task.data["updatedAt"]
        await manage({"action": "schedule_pause", "id": task.id, "expectedUpdatedAt": stale})
        task = next(row for row in (await read("schedules")).items if row.id == task.id)
        assert task.state == "paused"
        await manage({"action": "schedule_resume", "id": task.id, "expectedUpdatedAt": stale}, success=False)
        await manage({"action": "schedule_resume", "id": task.id, "expectedUpdatedAt": task.data["updatedAt"]})
        await manage({"action": "schedule_run", "id": task.id})
        history = await read("schedule_history", query=task.id)
        assert len(history.items) == 1 and history.items[0].sid
        first_target = history.items[0].sid
        # Admission is a delivery receipt, not a model completion receipt.
        async with asyncio.timeout(90):
            while any(row.get("executionActive") or (row.get("deliveryFloorEndsAt") and datetime.fromisoformat(row["deliveryFloorEndsAt"].replace("Z", "+00:00")) > datetime.now(timezone.utc)) for row in await runtime.client.rpc("schedule/catalog")):
                await asyncio.sleep(.05)
            while any(row.get("running") for row in await runtime.client.list_sessions()):
                await asyncio.sleep(.05)
        await manage({"action": "schedule_run", "id": task.id})
        history = await read("schedule_history", query=task.id)
        assert len(history.items) == 2
        targets = {row.sid for row in history.items}
        assert (len(targets) == 2) == (destination["kind"] == "new"), targets
        if destination["kind"] != "new":
            assert targets == {first_target}
        task = next(row for row in (await read("schedules")).items if row.id == task.id)
        await manage({"action": "schedule_update", "id": task.id, "expectedUpdatedAt": task.data["updatedAt"],
            "patch": {"timing": {"kind": "weekly", "weekly": {
                "time": "10:30:00", "time_zone": "Asia/Taipei", "weekdays": [1, 5]}}}})
        task = next(row for row in (await read("schedules")).items if row.id == task.id)
        assert task.data["kind"] == "weekly" and task.data["weekdays"] == [1, 5]
        await manage({"action": "schedule_delete", "id": task.id, "expectedUpdatedAt": task.data["updatedAt"]})
        assert not any(row.id == task.id for row in (await read("schedules")).items)
    print("PASS native schedules: three destinations, run/reuse, pause/resume, CAS, timing and deletion", flush=True)
    refused = await machine._handle(OpenBtw(sid=sid, client_id=browser, owner_id=browser, request_id=uuid4().hex))
    assert refused.type == "error" and refused.code == "dsh_sidechat_unavailable"
    # The optional isolated probe still covers the dormant implementation;
    # browser entry points remain disabled pending native deletion support.
    opened = await runtime.sidechats.open(OpenBtw(sid=sid, client_id=browser, owner_id=browser, request_id=uuid4().hex))
    assert isinstance(opened, tuple), opened
    fork_sid = opened[0].btw_sid
    branch = machine.sessions[fork_sid]
    parent = machine.sessions[sid]
    assert branch.sdk.model == parent.sdk.model and branch.sdk.effort == parent.sdk.effort
    assert branch.sdk.permission_mode == parent.sdk.permission_mode, (branch.sdk.permission_mode, parent.sdk.permission_mode)
    await machine._handle(SyncBtw(sid=fork_sid, client_id=browser, owner_id=browser))
    await machine._handle(Query(sid=fork_sid, prompt="hi", msg_id=uuid4().hex, client_id=browser, owner_id=browser))
    async with asyncio.timeout(20):
        while branch.state != "idle":
            await asyncio.sleep(.05)
    assert branch.state == "idle"
    # Restore the association and native history without activating an Agent.
    await branch.sdk.disconnect()
    machine.sessions.pop(fork_sid)
    await machine._handle(SyncBtw(sid=fork_sid, client_id=browser, owner_id=browser))
    branch = machine.sessions[fork_sid]
    assert branch.btw and branch.sdk.model == parent.sdk.model
    assert machine._btw_history(branch).turns
    await machine._handle(CloseBtw(sid=fork_sid, client_id=browser, owner_id=browser))
    assert not branch.btw and any(row["sessionId"] == fork_sid[4:] for row in await runtime.client.list_sessions())
    print("PASS native side-chat fork, inherited settings, isolated conversation and retained release", flush=True)
    terminal = uuid4().hex
    await control({"action": "create", "id": terminal})
    try:
        assert any(item.id == terminal for item in (await read("terminals")).items)
        lease = uuid4().hex
        await control({"action": "attach", "id": terminal, "lease": lease, "stream": True})
        await control({"action": "input", "lease": lease, "value": {"type": "resize", "cols": 82, "rows": 25}})
        # A shell-only harmless output marker; no model/network access.
        await control({"action": "input", "lease": lease, "value": {"type": "write", "data": "printf 'cc_remote_pty_ok\\n'\r"}})
        output, cursor = "", 0
        async with asyncio.timeout(10):
            while "cc_remote_pty_ok" not in output:
                pages = [m for m in machine.transport.sent if m.type == "dsh_read_result" and m.request_id == lease and (m.next_seq or 0) > cursor]
                for page in pages:
                    assert page.to == browser and not page.error
                    cursor = page.next_seq
                    output += "".join(str(item.data.get("screen", item.data.get("data", ""))) for item in page.items)
                    await control({"action": "ack", "lease": lease, "cursor": cursor})
                await asyncio.sleep(0.05)
        await control({"action": "detach", "lease": lease})
        assert any(item.id == terminal for item in (await read("terminals")).items)
        print("PASS native PTY create, resize, input, output and detach retention", flush=True)
    finally:
        attachment = await control({"action": "attach", "id": terminal})
        await control({"action": "close", "id": terminal})
        page = await read("terminal_output", query=attachment.request_id)
        assert any(item.data.get("info", {}).get("state") == "exited" for item in page.items), page
        print("PASS native terminal final state survives explicit close", flush=True)
    assert not any(item.id == terminal for item in (await read("terminals")).items)
    print("PASS explicit native PTY process close", flush=True)
