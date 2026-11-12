"""Public room data and native-session configuration for collaboration chats."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CollaborationMemberSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine: Literal["claude", "codex", "dsh"]
    name: str = Field(default="", max_length=80)
    profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    session_id: str | None = Field(default=None, min_length=1, max_length=256)
    cwd: str | None = Field(default=None, min_length=1, max_length=4096)
    model: str | None = Field(default=None, min_length=1, max_length=256)
    effort: str | None = Field(default=None, min_length=1, max_length=64)
    permission_mode: str | None = Field(default=None, min_length=1, max_length=64)
    permission_profile: str | None = Field(default=None, min_length=1, max_length=256)
    service_tier: str | None = Field(default=None, min_length=1, max_length=64)
    dsh_permission: str | None = Field(default=None, min_length=1, max_length=256)
    dsh_agent_preset: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def engine_controls(self):
        if self.engine != "codex" and (self.permission_profile or self.service_tier):
            raise ValueError("Codex controls require a Codex participant")
        if self.engine != "dsh" and (self.dsh_permission or self.dsh_agent_preset):
            raise ValueError("DSH controls require a DSH participant")
        if self.engine == "dsh" and self.profile_id not in {None, "default"}:
            raise ValueError("Select the configured DSH connection")
        if self.session_id and any(
            getattr(self, field)
            for field in (
                "model", "effort", "permission_mode", "permission_profile",
                "service_tier", "dsh_permission", "dsh_agent_preset",
            )
        ):
            raise ValueError("已有会话沿用原有运行配置。")
        return self

    def session_options(self) -> dict:
        values = self.model_dump(
            exclude={"name", "profile_id", "session_id"}, exclude_none=True
        )
        if self.profile_id and self.engine in {"claude", "codex"}:
            values[f"{self.engine}_profile_id"] = self.profile_id
        if self.engine == "dsh" and "effort" in values:
            values["dsh_effort"] = values.pop("effort")
        return values


ACTIVE_DELIVERIES = frozenset(
    {"queued", "preparing", "submitting", "accepted", "running", "stopping"}
)
MAX_ROOM_CONTEXT = 1024 * 1024
MAX_ROOM_MESSAGE = 64 * 1024


def shared_prompt(room: dict, member: dict, messages: list[dict], current: dict) -> str:
    """Build one attributed input, preserving the current instruction verbatim."""
    roster = "、".join(
        f"{item['name']}（{item['spec']['engine']} · {item['spec']['profile_id'] or 'default'}）"
        for item in room["members"]
        if not item["removed"]
    )
    parts = [
        f"你正在参加 cc-remote 协作聊天室「{room['name']}」。",
        f"房间成员包括 {roster}。本次输入发给 {member['name']}。",
        "以下公开讨论供你理解协作上下文，当前用户指令位于最后。",
        "完成后向聊天室给出结论与必要说明，详细工作过程保留在当前原生会话。",
        "\n新增公开讨论",
    ]
    names = {item["id"]: item["name"] for item in room["members"]}
    for message in messages:
        if message["id"] == current["id"]:
            continue
        author = (
            "用户"
            if message["role"] == "user"
            else names.get(message["member_id"], "参与者")
        )
        attachment_names = "、".join(
            item["name"] for item in message.get("attachments", [])
        )
        body = message["text"] + (
            f"\n附件 {attachment_names}" if attachment_names else ""
        )
        parts.append(
            f'\n<room-message seq="{message["seq"]}">\n{author}\n{body}\n</room-message>'
        )
    parts.extend(["\n当前用户给你的输入", current["text"]])
    prompt = "\n".join(parts)
    if len(prompt.encode("utf-8")) > MAX_ROOM_CONTEXT:
        raise ValueError(
            "讨论内容超过本次输入预算，请先让参与者整理房间总结，或开启新聊天。"
        )
    return prompt
