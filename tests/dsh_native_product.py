"""Explicit zero-provider-call acceptance of the native DSH product surfaces."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from cc_remote.protocol import Query, ReadDsh, SetDshControl


async def exercise(machine, runtime, sid):
    client = runtime.client
    ctx = machine.sessions[sid]
    async def read(kind, query):
        result = await machine._handle(ReadDsh(sid=sid, kind=kind, query=query, cmd_id=uuid4().hex, client_id="product-probe"))
        assert not result.error, result
        return result
    async def manage(body):
        result = await machine._handle(SetDshControl(sid=sid, kind="manage", value=json.dumps(body), cmd_id=uuid4().hex, client_id="product-probe"))
        assert result.status == "success", result
        return result
    peer = await client._connection()
    before = (await peer.http.get("/api/cc-remote.test-state")).json()
    stats = await read("trajectory", "stats")
    assert stats.items and any(row.data["input"] and row.data["model"] is not None for row in stats.items)
    assert (await peer.http.get("/api/cc-remote.test-state")).json() == before
    print("PASS native measured statistics without model or agent activation", flush=True)

    at = datetime.now(timezone.utc) + timedelta(seconds=2)
    await manage({"action": "schedule_create", "request": {"title": "Ended cleanup fixture", "prompt": "hi",
        "target": {"kind": "session", "sid": sid}, "timing": {"kind": "at", "at": {
            "date": at.strftime("%Y-%m-%d"), "time": at.strftime("%H:%M:%S"), "time_zone": "UTC"}}}})
    await manage({"action": "schedule_create", "request": {"title": "Active cleanup fixture", "prompt": "hi",
        "target": {"kind": "session", "sid": sid}, "timing": {"kind": "daily", "daily": {"time": "23:59:59", "time_zone": "UTC"}}}})
    async with asyncio.timeout(45):
        while True:
            rows = await client.rpc("schedule/catalog")
            ended = next(row for row in rows if row["title"] == "Ended cleanup fixture")
            if ended["status"] == "ended" and not ended.get("executionActive") and ctx.state == "idle":
                break
            await asyncio.sleep(.1)
    history_before = await client.history_snapshot(sid)
    plan = (await read("schedules", "cleanup")).items[0].data
    assert plan["count"] >= 1
    await manage({"action": "schedule_cleanup", "revision": plan["revision"]})
    rows = await client.rpc("schedule/catalog")
    assert not any(row["title"] == "Ended cleanup fixture" for row in rows)
    active = next(row for row in rows if row["title"] == "Active cleanup fixture")
    assert active["status"] == "active"
    assert (await client.history_snapshot(sid))["records"] == history_before["records"]
    await manage({"action": "schedule_delete", "id": active["id"], "expectedUpdatedAt": active.get("updatedAt")})
    print("PASS native bulk cleanup preserves active schedule and conversation history", flush=True)

    await manage({"action": "bundle_enable", "name": "@deepseek-ai/dsh-personalized-recommendations"})
    for verb in ("dismiss", "enable"):
        result = await machine._handle(Query(sid=sid, prompt="compatibility:recommend-" + verb, msg_id="native-recommend-" + verb))
        assert result is None or getattr(result, "type", "") != "error", result
        async with asyncio.timeout(40):
            while True:
                cards = (await read("plugins", "recommendations")).items
                card = next((row for row in cards if row.state == "proposed"), None)
                if card and ctx.state == "idle":
                    break
                await asyncio.sleep(.2)
        assert card.controllable and card.data["owner"].startswith("dsh-seq-")
        await manage({"action": "recommendation", "call": card.id, "verb": verb})
        actual = (await read("plugins", "recommendations")).items
        if verb == "dismiss":
            assert not any(row.id == card.id for row in actual)
        else:
            assert next(row for row in actual if row.id == card.id).state in {"enabled", "already-enabled"}
        async with asyncio.timeout(40):
            while ctx.state != "idle":
                await asyncio.sleep(.1)
    print("PASS native recommendation proposal, exact owner, persistent dismissal and enable receipt", flush=True)
