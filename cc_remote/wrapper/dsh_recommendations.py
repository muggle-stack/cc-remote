"""Native official-plugin action cards, with exact proposal and turn ownership."""
from cc_remote.protocol import DshItem
from cc_remote.wrapper.dsh_client import DshError, native_session_id
from cc_remote.wrapper.dsh_plugins import label
from cc_remote.wrapper.dsh_stream import DshProjection


async def view(client, sid):
    native = native_session_id(sid)
    baseline = await client.rpc("session/projections", {"request": {"sessionId": native}})
    if not isinstance(baseline, dict) or baseline.get("kind") != "sequenced":
        raise DshError("recommendations_unavailable", "推荐状态暂不可读取。")
    return baseline.get("values", {}).get("actionCards") or {"proposals": {}, "decisions": {}}


async def read(runtime, cmd):
    client = runtime.client
    cards = await view(client, cmd.sid)
    proposals = {key: row for key, row in cards.get("proposals", {}).items()
                 if row.get("action") == "enable_official_plugin"}
    if not proposals:
        return []
    bundles = {row["name"]: row for row in await client.rpc("pluginManager/listBundles")
               if row.get("official") and row.get("optional") and row.get("availability") != "missing"
               and not row.get("readOnlyReason") and not row.get("error")}
    snapshot = await client.history_snapshot(cmd.sid, max_messages=100)
    records = snapshot["records"]
    projection = DshProjection(cursor=records[0]["event"]["seq"] - 1 if records else -1)
    owners = {}
    for record in records:
        event = record["event"]
        projection.record(event)
        if event["type"] == "tool/call" and event["data"].get("callId") in proposals:
            call = event["data"]["callId"]
            binding = projection.tool_owners.get(call)
            if binding:
                owners[call] = binding[0]
    sessions = await client.list_sessions()
    live = any(row["sessionId"] == native_session_id(cmd.sid) and row.get("agentAvailable") for row in sessions)
    result = []
    for call, proposal in list(proposals.items())[-32:]:
        phase = cards.get("decisions", {}).get(call, {}).get("phase", "proposed")
        bundle = bundles.get(proposal.get("details", {}).get("name"))
        if phase in {"dismissed", "replaced"} or not bundle or call not in owners:
            continue
        result.append(DshItem(id=call, title=(label(bundle.get("meta", {}).get("title")) or bundle["name"])[:2048],
            detail=str(proposal.get("brief") or label(bundle.get("meta", {}).get("description")))[:4096],
            state=phase, controllable=live and phase in {"proposed", "failed", "unconfirmed"},
            data={"name": bundle["name"], "owner": owners[call], "enabled": bool(bundle.get("enabled")),
                  "application": cards.get("decisions", {}).get(call, {}).get("data", {}).get("application")}))
    return result


async def act(runtime, sid, body):
    cards = await view(runtime.client, sid)
    call, verb = body.get("call"), body.get("verb")
    proposal = cards.get("proposals", {}).get(call) if isinstance(call, str) else None
    if not proposal or proposal.get("action") != "enable_official_plugin" or verb not in {"enable", "dismiss"}:
        raise DshError("invalid_recommendation", "这个推荐操作不可用，请刷新。")
    result = await runtime.client.rpc("actionCards/act", {"agentId": native_session_id(sid), "callId": call, "verb": verb})
    if result.get("status") == "unconfirmed":
        raise DshError("recommendation_unknown", "操作结果未确认，请刷新核实；不会自动重试。", outcome_unknown=True)
    if result.get("status") != "settled":
        raise DshError("recommendation_refused", "原生 DSH 未接受这个操作，请刷新推荐状态。")
    decision = result.get("decision", {})
    if decision.get("callId") != call or decision.get("action") != "enable_official_plugin":
        raise DshError("recommendation_unknown", "推荐操作返回身份不一致，请刷新核实。", outcome_unknown=True)
    if decision.get("phase") == "dismissed":
        return "这条推荐已忽略，本会话不再提示。"
    if decision.get("phase") in {"enabled", "already-enabled"}:
        return "插件已保存，需重启 DSH 后生效。" if decision.get("data", {}).get("application") == "restart-required" else "插件已启用。"
    raise DshError("recommendation_failed", "插件尚未启用，请检查原生插件状态。")
