"""Private management projections over pinned official DSH Remote APIs."""
import asyncio
import json

from cc_remote.protocol import DshItem
from cc_remote.wrapper import dsh_plugins, dsh_schedules, dsh_stats
from cc_remote.wrapper.dsh_client import DshError, wire_session_id

READS = {"workflows", "trajectory", "schedules", "schedule_history", "settings", "presets", "bundles"}
# Model/provider/credential namespaces never enter this adapter. Read only
# simple execution preferences from native, already-redacted form values.
SETTINGS = {"subagent", "agent-loop", "loop", "tool-bash", "shell", "web-search", "agent-preset-registry"}


def localized(value):
    if isinstance(value, str):
        return value[:4096]
    if isinstance(value, dict):
        return next((value[k][:4096] for k in ("zh-CN", "zh", "en", "en-US") if isinstance(value.get(k), str)), "")
    return ""


def bundle_item(row):
    meta = row.get("meta") or {}
    data = {k: row[k] for k in ("name", "version", "installed", "enabled", "removable", "updatable", "optional", "availability", "readOnlyReason", "installTarget") if k in row}
    available = row.get("availability") in {"installation", "profile"}
    return DshItem(id=row["name"], title=(localized(meta.get("title")) or row["name"])[:2048],
        detail=localized(meta.get("description")) or localized(row.get("description")),
        state="enabled" if row.get("enabled") else "disabled" if available else "not-installed",
        controllable=available and not row.get("readOnlyReason"), data=data)


async def market_read(client, cmd, response):
    try:
        query = json.loads(cmd.query)
    except (ValueError, TypeError):
        raise DshError("invalid_control", "插件搜索请求无效。") from None
    if not isinstance(query, dict) or query.get("market") is not True:
        raise DshError("invalid_control", "插件搜索请求无效。")
    text = query.get("text", "")
    if not isinstance(text, str) or len(text) > 200:
        raise DshError("invalid_control", "搜索词过长。")
    indexes = await client.rpc("pluginMarket/listIndexes")
    if not indexes:
        return []
    selected = next((row for row in indexes if dsh_plugins.source_id(row) == query.get("source")), None) if query.get("source") else indexes[0]
    if not selected:
        raise DshError("market_unavailable", "这个插件源已被移除，请重新打开插件面板。")
    # Only the runtime's configured source, never an arbitrary browser URL.
    page = await client.rpc("pluginMarket/search", {"url": selected["url"], "query": {
        "text": text, "sort": {"kind": "relevance"}, "offset": cmd.before_seq or 0, "limit": 30,
        **({"fetchedAt": query["fetchedAt"]} if type(query.get("fetchedAt")) in (int, float) else {})}})
    if response is not None:
        response.next_seq = page.get("nextOffset")
        response.has_more = response.next_seq is not None
    bundles = {row["name"]: row for row in await client.rpc("pluginManager/listBundles")}
    items = []
    for row in page["entries"][:30]:
        version = next((v for v in row["versions"] if v["version"] == row.get("defaultVersion")), None)
        state = row["installation"]["status"]
        items.append(DshItem(id=row["name"], title=(localized(row.get("title")) or row["name"])[:2048],
            detail=localized(row.get("description")), state=state,
            data={"name": row["name"], "version": version["version"] if version else None,
                  "installedVersion": row["installation"].get("installedVersion"),
                  "updatable": bundles.get(row["name"], {}).get("updatable", False),
                  "source": dsh_plugins.source_id(selected), "fetchedAt": page.get("fetchedAt"),
                  "installTarget": {"source": dsh_plugins.source_id(selected), "name": row["name"], "version": version["version"]}
                    if version and state == "not-installed" and version.get("compatibility", {}).get("compatible") is True else None}))
    return items


def visible_settings(value):
    fields = []
    for row in value.get("namespaces", []):
        if row.get("ns") not in SETTINGS:
            continue
        hidden = {s["path"][0] for s in row.get("secrets", []) if s.get("path")}
        values = row.get("value", {})
        if not isinstance(values, dict):
            continue
        simple = {k: v for k, v in values.items() if k not in hidden and
                  (v is None or isinstance(v, (str, int, float, bool))) and
                  not any(word in k.lower() for word in ("secret", "token", "password", "credential", "api_key", "apikey"))}
        schema = row.get("schema") or {}
        refs = schema.get("refs", {})
        properties = refs.get(str(schema.get("uid")), {}).get("dict", {})
        if row["ns"] == "agent-preset-registry" and "selectedDefault" in properties:
            simple.setdefault("selectedDefault", None)
        constraints = {}
        for key in simple:
            field = refs.get(str(properties.get(key)), {})
            meta = field.get("meta", {})
            constraints[key] = {k: meta[k] for k in ("min", "max", "step") if type(meta.get(k)) in (int, float)}
        data = {"revision": row["revision"], "values": simple}
        if row.get("applies") == "live":
            data.update(applies="live", constraints=constraints)
        fields.append(DshItem(id=row["ns"], title=row["ns"], state="settings",
            controllable=bool(value.get("writable")), data=data))
    return fields


async def read(runtime, cmd, response=None):
    client = runtime.client
    if cmd.kind == "trajectory" and cmd.query == "stats":
        return await dsh_stats.read(client, cmd, response)
    if cmd.kind == "settings":
        return visible_settings(await client.rpc("settings/describe"))
    if cmd.kind == "presets":
        roster = await client.rpc("agentPresets/list")
        return [DshItem(id=p["id"], title=p.get("name") or p["id"],
            detail=p.get("description") or "", state="failed" if p.get("broken") else "default" if p.get("isDefault") else "available")
            for p in roster.get("presets", [])[:128]]
    if cmd.kind == "bundles":
        if cmd.query:
            try:
                query = json.loads(cmd.query)
            except (ValueError, TypeError):
                query = None
            if isinstance(query, dict) and query.get("versions") is True:
                return await dsh_plugins.read(client, cmd)
            if isinstance(query, dict) and query.get("sources") is True:
                return await dsh_plugins.sources(client)
            if isinstance(query, dict) and query.get("detail") is True:
                return await dsh_plugins.detail(client, query)
            return await market_read(client, cmd, response)
        rows = await client.rpc("pluginManager/listBundles")
        return [bundle_item(r) for r in rows[:128]]
    if cmd.kind == "schedules" and cmd.query == "sessions":
        rows = await client.list_sessions()
        return [DshItem(id=wire_session_id(r["sessionId"]), title=str(r.get("projections", {}).get("values", {}).get("title") or "未命名会话")[:2048],
                       detail=r.get("cwd", "")) for r in rows if r.get("origin") != "subagent"
                       and wire_session_id(r["sessionId"]) not in runtime.sidechats.check()][:256]
    if cmd.kind in {"schedules", "schedule_history"}:
        return await dsh_schedules.read(runtime, cmd, response)
    snapshot = await client.history_snapshot(cmd.sid, before_seq=cmd.before_seq, max_messages=100)
    if response is not None:
        response.has_more = bool(snapshot.get("hasMore") and snapshot["records"])
        response.next_seq = snapshot["records"][0]["event"]["seq"] if response.has_more else None
    events = [r["event"] for r in snapshot["records"]]
    if cmd.kind == "workflows":
        runs = {}
        for e in events:
            d, kind = e.get("data", {}), e["type"]
            if not kind.startswith("tool-workflow/") or not isinstance(d.get("runId"), str):
                continue
            run = runs.setdefault(d["runId"], {"name": "工作流", "name_known": False, "state": "unknown", "members": {}})
            if kind == "tool-workflow/run-start":
                run.update(name=d["name"], name_known=True, state="running")
            elif kind == "tool-workflow/run-end":
                run["state"] = d["stopReason"]
            elif kind == "tool-workflow/agent-start":
                run["members"][d["seq"]] = {"seq": d["seq"], "sid": wire_session_id(d["childId"]), "label": d["label"], "phase": d.get("phase"), "state": "running"}
            elif kind == "tool-workflow/agent-end":
                run["members"].setdefault(d["seq"], {"seq": d["seq"]})["state"] = d["outcome"]
        return [DshItem(id=k, title=v["name"], state=v["state"], data={"name_known": v["name_known"], "members": list(v["members"].values())[:128]}) for k, v in list(runs.items())[-64:]]
    turns = {}
    current = None
    for e in events:
        d, kind = e.get("data", {}), e["type"]
        if kind == "turn/start":
            current = str(d["turn"])
            turns[current] = {"started": e.get("time"), "state": "running", "events": [], "input": 0, "output": 0}
        elif type(d.get("turn")) is int:
            current = str(d["turn"])
            turns.setdefault(current, {"state": "unknown", "events": [], "input": 0, "output": 0})
        if current is None:
            continue
        row = turns[current]
        if kind == "turn/end":
            row["state"] = d.get("reason", {}).get("kind", "unknown")
        if kind in {"tool/call", "tool/ptc-dispatch-start", "assistant/message", "subagent/catalog", "compaction/start"}:
            row["events"].append({"seq": e["seq"], "type": kind, "time": e.get("time"), "label": str(d.get("name") or d.get("label") or {"assistant/message": "模型回复", "subagent/catalog": "子代理", "compaction/start": "压缩上下文"}.get(kind, kind))[:256]})
            row["events"] = row["events"][-256:]
        if kind == "assistant/message":
            u = d.get("usage") or {}
            row["input"] += sum(max(0, u[k]) for k in ("inputTokens", "cacheReadTokens", "cacheWriteTokens") if type(u.get(k)) is int)
            row["output"] += max(0, u.get("outputTokens", 0)) if type(u.get("outputTokens")) is int else 0
    return [DshItem(id=k, title=f"第 {k} 轮", state=v["state"], data=v) for k, v in list(turns.items())[-64:]]


async def manage(runtime, cmd):
    identity, fresh, receipt = await runtime.mutation_intent(cmd, "management", {"sid": cmd.sid, "value": cmd.value})
    if not fresh:
        if receipt["result"] is None:
            raise runtime.uncertain_mutation()
        saved = json.loads(receipt["result"])
        if "error" in saved:
            raise DshError(**saved["error"])
        return saved["text"]
    try:
        text = await apply(runtime, cmd)
    except DshError as exc:
        await asyncio.to_thread(runtime.mutations.finish, identity, {"error": {
            "code": exc.code, "message": str(exc), "outcome_unknown": exc.outcome_unknown}})
        raise
    await asyncio.to_thread(runtime.mutations.finish, identity, {"text": text})
    return text


async def apply(runtime, cmd):
    try:
        body = json.loads(cmd.value)
        action = body["action"]
        if not isinstance(body, dict) or not isinstance(action, str):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise DshError("invalid_control", "设置请求无效。") from None
    client = runtime.client
    if action == "recommendation":
        from cc_remote.wrapper.dsh_recommendations import act
        return await act(runtime, cmd.sid, body)
    if action in {"market_source_add", "market_source_remove"}:
        # Native setIndexes replaces the whole list without a revision field.
        # Serialize our own clients' observed-revision read/modify/write cycles.
        async with runtime.features.sources_lock:
            return await dsh_plugins.change_source(client, body)
    if action == "market_install":
        body = {"action": "bundle_install", **await dsh_plugins.install_target(client, body)}
        action = "bundle_install"
    if action in {"settings", "preset_default"}:
        ns = "agent-preset-registry" if action == "preset_default" else body.get("ns")
        rows = visible_settings(await client.rpc("settings/describe"))
        row = next((r for r in rows if r.id == ns and r.controllable), None)
        if not row or type(body.get("revision")) is not int or body["revision"] != row.data["revision"]:
            raise DshError("settings_conflict", "设置已变化，请刷新后重新修改。")
        patch = {"selectedDefault": body["id"]} if action == "preset_default" else body.get("patch")
        if not isinstance(patch, dict) or not patch or not set(patch) <= set(row.data["values"]):
            raise DshError("invalid_control", "此设置不支持通过当前页面修改。")
        if any(isinstance(v, (dict, list)) for v in patch.values()):
            raise DshError("invalid_control", "设置值格式无效。")
        saved = await client.rpc("settings/update", {"ns": ns, "patch": patch, "expectedRevision": body["revision"]})
        if not isinstance(saved, dict) or saved.get("ns") != ns or any(saved.get("value", {}).get(k) != v for k, v in patch.items()):
            raise DshError("settings_unconfirmed", "设置的实际值与提交值不同，请刷新核实；修改不会自动重发。", outcome_unknown=True)
        return "默认预设已保存，之后新建的会话使用此预设。" if action == "preset_default" else "设置已保存，DSH 已确认生效。"
    if action.startswith("schedule_"):
        return await dsh_schedules.manage(runtime, body)
    if action == "bundle_update":
        return await dsh_plugins.update(client, body, cmd.cmd_id)
    if action == "bundle_install":
        spec = body.get("spec")
        if not isinstance(spec, str) or not spec.strip() or len(spec) > 2048:
            raise DshError("invalid_control", "请输入插件包名或本机路径。")
        options = {"requestId": cmd.cmd_id}
        registry = body.get("registry")
        if registry is not None:
            # Preserve the native registry validation; never interpolate into a shell.
            if not isinstance(registry, str) or len(registry) > 2048:
                raise DshError("invalid_control", "插件源无效。")
            options["registry"] = registry
        inspected = await client.rpc("pluginManager/inspect", {"spec": spec,
            **({"options": {"registry": registry}} if registry else {})})
        if inspected.get("status") != "accepted":
            reasons = {"already-installed": "插件包已安装，请在插件详情中选择版本更新。",
                       "not-found": "未找到这个插件包，请检查包名或路径。", "not-a-bundle": "这个包不是 DSH 插件包。",
                       "network": "无法连接插件源，请稍后重试。"}
            raise DshError("plugin_refused", reasons.get(inspected.get("problem"), "DSH 不接受这个插件来源，请检查包名或路径。"))
        value = await client.rpc("pluginManager/installBundle", {"spec": spec, "options": options})
    elif action in {"bundle_enable", "bundle_disable", "bundle_remove"}:
        name = body.get("name")
        if not isinstance(name, str) or len(name) > 256:
            raise DshError("invalid_control", "插件包名无效。")
        value = await client.rpc("pluginManager/removeBundle" if action == "bundle_remove" else "pluginManager/setBundleEnabled",
            {"name": name, **({} if action == "bundle_remove" else {"enabled": action == "bundle_enable"})})
    else:
        raise DshError("unsupported", "当前 DSH 未提供此设置操作。")
    return dsh_plugins.result_text(value.get("application"))
