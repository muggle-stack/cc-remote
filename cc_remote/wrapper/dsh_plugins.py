"""Version selection and updates through the native plugin manager."""
import json
import hashlib
from urllib.parse import urlsplit

from cc_remote.protocol import DshItem
from cc_remote.wrapper.dsh_client import DshError


def source_id(row):
    return hashlib.sha256(row["url"].encode()).hexdigest()[:32]


async def sources(client):
    rows = await client.rpc("pluginMarket/listIndexes")
    revision = sources_revision(rows)
    return [DshItem(id=source_id(row), title=(label(row.get("title")) or f"插件源 {index + 1} · {urlsplit(row['url']).hostname or '本机'}")[:2048],
                   data={"url": public_url(row["url"]), "revision": revision})
            for index, row in enumerate(rows[:128])] or [DshItem(id="sources-empty", state="empty", data={"revision": revision})]


def label(value):
    if isinstance(value, str):
        return value[:4096]
    if isinstance(value, dict):
        return next((value[key][:4096] for key in ("zh-CN", "zh", "en", "en-US") if isinstance(value.get(key), str)), "")
    return ""


def public_url(value):
    try:
        parts = urlsplit(value)
        return value if parts.scheme in {"http", "https"} and parts.hostname and not (parts.username or parts.password or parts.query or parts.fragment) else None
    except ValueError:
        return None


def sources_revision(rows):
    return hashlib.sha256(json.dumps([row["url"] for row in rows]).encode()).hexdigest()


async def change_source(client, body):
    rows = await client.rpc("pluginMarket/listIndexes")
    if body.get("revision") != sources_revision(rows):
        raise DshError("source_conflict", "插件源清单已变化，请刷新后重新修改。")
    urls = [row["url"] for row in rows]
    if body["action"] == "market_source_add":
        url = body.get("url")
        if not isinstance(url, str) or len(url) > 2048 or not public_url(url) or any(char.isspace() for char in url):
            raise DshError("invalid_source", "请输入不含账号、密钥、查询参数或片段的 HTTP(S) 插件索引地址。")
        if url in urls:
            raise DshError("source_duplicate", "这个插件源已添加。")
        if len(urls) >= 128:
            raise DshError("source_limit", "插件源数量已达上限。")
        urls.append(url)
    else:
        selected = next((row for row in rows if source_id(row) == body.get("source")), None)
        if not selected:
            raise DshError("source_missing", "这个插件源已被移除，请刷新。")
        urls.remove(selected["url"])
    await client.rpc("pluginMarket/setIndexes", {"urls": urls})
    actual = await client.rpc("pluginMarket/listIndexes")
    if [row["url"] for row in actual] != urls:
        raise DshError("source_unknown", "插件源保存结果未确认，请刷新核实。", outcome_unknown=True)
    return "插件源已保存。"


async def market_entry(client, query):
    name = query.get("name")
    if not isinstance(name, str) or not name or len(name) > 256:
        raise DshError("invalid_control", "插件名称无效。")
    rows = await client.rpc("pluginMarket/listIndexes")
    source = next((row for row in rows if source_id(row) == query.get("source")), None)
    if not source:
        raise DshError("source_missing", "插件源已变化，请重新搜索。")
    page = await client.rpc("pluginMarket/search", {"url": source["url"], "query": {
        "text": name, "sort": {"kind": "relevance"}, "offset": 0, "limit": 100}})
    entry = next((row for row in page.get("entries", []) if row.get("name") == name), None)
    if not entry:
        raise DshError("plugin_missing", "插件已不在当前来源中，请重新搜索。")
    version = next((row for row in entry.get("versions", []) if row.get("version") == query.get("version")), None)
    if not version:
        raise DshError("plugin_version", "这个版本已不可用，请重新搜索。")
    return source, entry, version


async def install_target(client, body):
    _, entry, version = await market_entry(client, body)
    if entry.get("installation", {}).get("status") != "not-installed":
        raise DshError("plugin_installed", "插件已安装，请刷新后在详情中管理。")
    if version.get("compatibility", {}).get("compatible") is not True:
        raise DshError("plugin_version", "这个插件版本与当前 DSH 不兼容。")
    return version["install"]


async def detail(client, query):
    if query.get("source"):
        source, _, _ = await market_entry(client, query)
        value = await client.rpc("pluginMarket/readEntryDetail", {"url": source["url"],
            "name": query["name"], "version": query["version"], "language": "zh-CN"})
    else:
        name = query.get("name")
        if not isinstance(name, str) or not name or len(name) > 256:
            raise DshError("invalid_control", "插件名称无效。")
        value = await client.rpc("pluginManager/readBundleDetail", {"name": name, "language": "zh-CN"})
    markdown = value.get("markdown", "") if value.get("status") == "found" else ""
    if not isinstance(markdown, str):
        raise DshError("plugin_detail", "插件详情格式不兼容。")
    return [DshItem(id=query["name"], detail=markdown[:65536], state="found" if markdown else "none",
                    data={"truncated": len(markdown) > 65536})]


async def versions(client, name):
    if not isinstance(name, str) or not name or len(name) > 256:
        raise DshError("invalid_control", "插件包名无效。")
    value = await client.rpc("pluginManager/listVersions", {"name": name})
    if value.get("status") != "listed":
        messages = {"not-updatable": "此插件随 DSH 更新，不支持单独更新。",
                    "not-found": "插件源没有这个包。", "network": "无法连接插件源，请稍后重试。"}
        raise DshError("plugin_versions", messages.get(value.get("problem"), "暂时无法读取插件版本。"))
    return value


async def read(client, cmd):
    try:
        query = json.loads(cmd.query)
        if not isinstance(query, dict) or query.get("versions") is not True:
            raise ValueError
    except (ValueError, TypeError):
        raise DshError("invalid_control", "插件版本请求无效。") from None
    value = await versions(client, query.get("name"))
    # Do not forward registry credentials or native diagnostic strings.
    return [DshItem(id=row["version"], title=row["version"],
        state="compatible" if row.get("bundle") and not row.get("incompatible") else "incompatible",
        data={key: row[key] for key in ("version", "tags", "prerelease", "publishedAt") if key in row} |
             {"deprecated": bool(row.get("deprecated"))}) for row in value.get("versions", [])[:128]]


async def update(client, body, request_id):
    name, version = body.get("name"), body.get("version")
    if not isinstance(version, str) or not version or len(version) > 128:
        raise DshError("invalid_control", "请选择插件版本。")
    # Revalidate against native metadata at click time; no arbitrary package spec
    # or registry may be smuggled in through the version selector.
    value = await versions(client, name)
    if not any(row.get("version") == version and row.get("bundle") and not row.get("incompatible")
               for row in value.get("versions", [])):
        raise DshError("plugin_version", "这个版本不可用或与当前 DSH 不兼容，请重新读取版本。")
    outcome = await client.rpc("pluginManager/updateBundle", {"name": name, "version": version,
        "options": {"requestId": request_id, "registry": value["registry"]}})
    result = outcome.get("result", {})
    application = result.get("application")
    if application in {"failed", "cancelled"}:
        restore = outcome.get("restore")
        if restore:
            restored = restore.get("application")
            suffix = {"applied": "原版本已恢复。", "restart-required": "原版本已恢复，需重启 DSH 后生效。",
                      "overridden": "原版本已恢复，但被其他配置覆盖。"}.get(restored, "原版本未能确认恢复，请检查原生插件状态。")
        else:
            suffix = "请刷新查看当前插件状态。"
        raise DshError("plugin_update", ("更新已取消；" if application == "cancelled" else "更新失败；") + suffix)
    if outcome.get("step") != "install":
        raise DshError("plugin_update_incomplete", "更新尚未完成。" + result_text(application))
    return result_text(application, update=True)


def result_text(application, *, update=False):
    messages = {"applied": "插件更新已生效。" if update else "插件设置已生效。",
                "restart-required": "已保存，需重启 DSH 后生效；当前会话继续运行。",
                "overridden": "已保存，但当前被其他配置覆盖。"}
    if application not in messages:
        raise DshError("plugin_result", "插件操作未确认成功，请刷新原生插件状态。",
                       outcome_unknown=application not in {"failed", "cancelled"})
    return messages[application]
