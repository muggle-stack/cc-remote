"""Project native approval choices without inventing broader grants.

Structured decisions stay on the device; the browser answers an ordinary
scoped question. Only an exact, understood choice offered by app-server can
be returned, including persistent command/network rules.
"""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

ApprovalDecision = str | dict[str, Any]
_BASIC = {
    "accept": ("允许一次", "仅批准这一次操作"),
    "acceptForSession": ("本会话允许", "本会话后续同类操作不再询问"),
    "decline": ("拒绝", "拒绝这次操作"),
    "cancel": ("取消", "取消当前操作或回合"),
}
_COMMAND_METHOD = "item/commandExecution/requestApproval"


def _text(value: Any, limit: int = 1200) -> str:
    value = str(value or "").strip()
    return value if len(value) <= limit else value[:limit] + "…"


def _describe(decision: Any, *, command: bool) -> tuple[str, str] | None:
    if isinstance(decision, str):
        return _BASIC.get(decision)
    if not command or not isinstance(decision, dict) or len(decision) != 1:
        return None
    amendment = decision.get("acceptWithExecpolicyAmendment")
    if isinstance(amendment, dict) and set(amendment) == {"execpolicy_amendment"}:
        prefix = amendment["execpolicy_amendment"]
        if (isinstance(prefix, list) and 0 < len(prefix) <= 128
                and all(isinstance(part, str) and 0 < len(part) <= 4096 for part in prefix)):
            rendered = json.dumps(prefix, ensure_ascii=False)
            # Never ask approval for a persistent rule whose scope is hidden.
            if len(rendered) <= 1200:
                return "允许并保存命令规则", f"持久允许此前缀匹配的命令：{rendered}"
    amendment = decision.get("applyNetworkPolicyAmendment")
    if isinstance(amendment, dict) and set(amendment) == {"network_policy_amendment"}:
        rule = amendment["network_policy_amendment"]
        if (isinstance(rule, dict) and set(rule) == {"host", "action"}
                and isinstance(rule["host"], str) and 0 < len(rule["host"]) <= 512
                and isinstance(rule.get("action"), str)
                and rule.get("action") in {"allow", "deny"}):
            action = "允许" if rule["action"] == "allow" else "拒绝"
            return f"保存网络{action}规则", f"持久{action}访问：{rule['host']}"
    return None


def approval_choices(method: str, params: dict) -> list[tuple[dict[str, str], ApprovalDecision]]:
    offered = params.get("availableDecisions") if method == _COMMAND_METHOD else None
    if offered is None:
        offered = list(_BASIC)
    if not isinstance(offered, list) or len(offered) > 32:
        return []
    result: list[tuple[dict[str, str], ApprovalDecision]] = []
    for decision in offered:
        if params.get("kind") == "humanCommand" and decision not in ("accept", "decline", "cancel"):
            continue
        description = _describe(decision, command=method == _COMMAND_METHOD)
        if description is None or any(value == decision for _, value in result):
            continue
        label, detail = description
        # Distinguish multiple rules without relying on a truncated host label.
        base = label
        ordinal = 2
        while any(option["label"] == label for option, _ in result):
            label = f"{base} {ordinal}"
            ordinal += 1
        result.append(({"label": label, "ds": detail}, deepcopy(decision)))
    return result


def valid_approval_decision(method: str, params: dict, decision: Any) -> bool:
    return any(decision == value for _, value in approval_choices(method, params))


def approval_prompt(method: str, params: dict) -> str:
    command = method in {_COMMAND_METHOD, "execCommandApproval"}
    network = params.get("networkApprovalContext")
    if command and isinstance(network, dict):
        lines = ["Codex 请求网络访问：", f"目标：{_text(network.get('host'), 512)}",
                 f"协议：{_text(network.get('protocol'), 64)}"]
    elif command:
        title = "Codex 请求向终端发送输入：" if params.get("kind") == "writeStdin" else "Codex 请求执行命令："
        value = params.get("command") or params.get("parsedCmd") or params.get("itemId")
        if isinstance(value, list):
            value = " ".join(str(part) for part in value)
        lines = [title, _text(value)]
    else:
        lines = ["Codex 请求修改文件："]
        changes = params.get("fileChanges")
        if isinstance(changes, dict) and changes:
            lines.extend(_text(path, 240) for path in list(changes)[:12])
            if len(changes) > 12:
                lines.append(f"另有 {len(changes) - 12} 个文件")
        elif params.get("itemId"):
            lines.append(f"变更项 {_text(params['itemId'], 240)}")
    for key, label in (("cwd", "目录"), ("grantRoot", "授权目录"), ("reason", "原因")):
        if params.get(key):
            lines.append(f"{label}：{_text(params[key], 500)}")
    permissions = params.get("additionalPermissions")
    if isinstance(permissions, dict) and permissions:
        lines.append("额外权限：\n" + json.dumps(permissions, ensure_ascii=False, indent=2))
    prompt = "\n".join(lines)
    if len(prompt) > 12000:
        raise ValueError("approval scope exceeds the question display limit")
    return prompt
