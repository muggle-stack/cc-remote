"""Classification helpers for errors returned by Claude-compatible gateways."""

from __future__ import annotations

from typing import Literal


ProviderRequestTooLargeKind = Literal["context", "request"]

_CONTEXT_TOO_LARGE_MARKERS = (
    "输入tokens数量",
    "input token count exceed",
    "input tokens exceed",
    "too many input tokens",
    "maximum context length",
    "context length exceeded",
    "context window exceeded",
    "exceeds the maximum allowed tokens",
    "prompt is too long",
)
_REQUEST_TOO_LARGE_MARKERS = (
    "status_code=413",
    "status code: 413",
    "status code 413",
    "request entity too large",
    "payload too large",
)


def is_empty_system_content_error(message: str, status_code: int | None = None) -> bool:
    text = message.casefold()
    return (status_code == 400 or "400" in text) and (
        "system content must contain at least one block" in text)


EMPTY_SYSTEM_CONTENT_MESSAGE = (
    "Claude 的请求被上游以 400 拒绝：系统消息内容为空。"
    "这不是上下文达到上限的证明；会话历史已保留，未自动重试或切换模型。"
    "需要核对 Claude 原生请求与网关兼容性。"
)


def classify_provider_request_too_large(
    error: BaseException | str,
    *,
    status_code: int | None = None,
) -> ProviderRequestTooLargeKind | None:
    """Distinguish a proven context overflow from an otherwise generic 413.

    A generic HTTP 413 can be caused by an oversized attachment or another
    gateway body limit.  It is still terminal for the submitted prompt, but it
    must not be presented as proof that Claude's native autocompaction failed.
    The whole exception chain is scanned before deciding so a token-specific
    nested cause takes precedence over a generic outer 413.
    """
    context_too_large = False
    request_too_large = status_code == 413
    current: BaseException | str | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "status_code", None) == 413:
            request_too_large = True
        text = str(current).casefold()
        if any(marker in text for marker in _CONTEXT_TOO_LARGE_MARKERS):
            context_too_large = True
        if any(marker in text for marker in _REQUEST_TOO_LARGE_MARKERS):
            request_too_large = True
        if isinstance(current, BaseException):
            current = current.__cause__ or current.__context__
        else:
            current = None
    if context_too_large:
        return "context"
    if request_too_large:
        return "request"
    return None


def is_provider_request_too_large(
    error: BaseException | str,
    *,
    status_code: int | None = None,
) -> bool:
    """Recognize native and gateway request-size failures without retrying."""
    return classify_provider_request_too_large(
        error, status_code=status_code,
    ) is not None


def provider_request_too_large_message(
    kind: ProviderRequestTooLargeKind,
) -> str:
    """Return an actionable message without over-claiming the 413 cause."""
    if kind == "context":
        return (
            "上游按其 token 口径拒绝了上下文请求；这不代表 Claude Code 显示的"
            "原生上下文已经达到上限，网关计量或媒体请求体可能与其不一致。"
            "请运行 /compact，或 Fork/新建会话后继续。"
        )
    return (
        "上游拒绝了过大的请求（413）。若本条消息包含较大附件，请缩小或拆分后重试；"
        "若会话上下文过大，请运行 /compact，或 Fork/新建会话后继续。"
    )
