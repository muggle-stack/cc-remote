"""Official, read-only Codex persisted-history projection.

The app-server owns Codex's native compaction and persisted turn model.  This
module reads that model without resuming a thread or creating a turn, converts
official ``ThreadItem`` values through the same translator used by the live
stream, and keeps opaque app-server cursors inside the wrapper process.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

from cc_remote.protocol import TurnEnd, TurnResult, UserMsg
from cc_remote.wrapper.codex_rpc import (
    CodexRpcRejected,
    CodexRpcResponseTooLarge,
    codex_rpc,
)
from cc_remote.wrapper.codex_stream import (
    CodexStreamTranslator,
    MIN_PROCESS_DURATION_MS,
)
from cc_remote.wrapper.history_store import materialize_history_turns


_Rpc = Callable[
    [str, dict[str, Any] | None, str | None],
    Awaitable[Any],
]
_RecoverUser = Callable[
    [str, str, str, int],
    Awaitable[UserMsg | None],
]
_RecoverUsers = Callable[
    [str, tuple[str, ...]],
    Awaitable[dict[str, UserMsg]],
]
_SAFE_WIRE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_TURN_STATUSES = frozenset({
    "completed", "interrupted", "failed", "inProgress",
})
_ITEM_VIEWS = frozenset({"notLoaded", "summary", "full"})
_IMAGE_MEDIA_TYPES = frozenset({
    "image/png", "image/jpeg", "image/jpg", "image/webp",
})
_DATA_IMAGE = re.compile(
    r"^data:(image/(?:png|jpeg|jpg|webp));base64,([A-Za-z0-9+/=]+)$",
)
_ITEM_PAGE_LIMIT = 1024
_MAX_ITEM_PAGES = 16
_MAX_DETAIL_ITEMS = 4096
_MAX_LOCATORS = 8192
_MAX_DETAIL_CACHE_ENTRIES = 64
_MAX_DETAIL_CACHE_BYTES = 64 * 1024 * 1024
_MAX_ACTIVE_TURN_CACHE_ENTRIES = 4
_MAX_TERMINAL_REFRESH_FALLBACKS = 64


class CodexHistoryError(RuntimeError):
    """Base class for official persisted-history failures."""


class CodexHistoryUnsupported(CodexHistoryError):
    """The installed app-server lacks the required official read API."""


class CodexHistoryInvalidResponse(CodexHistoryError):
    """The app-server returned malformed or internally inconsistent history."""


class CodexHistoryCursorError(CodexHistoryInvalidResponse):
    """A browser-safe cursor has no mapping in this wrapper generation."""


@dataclass(frozen=True)
class CodexHistoryPage:
    events: tuple[dict[str, Any], ...]
    turns: tuple[dict[str, Any], ...]
    has_more: bool
    oldest_id: str | None
    newest_id: str | None
    # Native ids are newest first, matching the official descending page. They
    # stay wrapper-private and let the caller validate projection completeness
    # without guessing from visible steer-segment ids.
    native_turn_ids: tuple[str, ...] = ()
    # Official user item ids and rollout user ids are intentionally unrelated.
    # Join positive rollout evidence through the native task plus chronological
    # steer index instead of guessing that either visible id is shared.
    native_segment_by_visible_id: dict[
        str, tuple[str, int]
    ] = field(default_factory=dict)
    file_change_offsets_by_visible_id: dict[str, tuple[int, ...]] = field(default_factory=dict)
    file_changes_truncated: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class CodexRolloutFallback:
    before: str | None
    limit: int
    native_turn_id: str
    segment_index: int
    segment_count: int


@dataclass(frozen=True)
class _TurnLocator:
    native_turn_id: str
    page_cursor: str | None
    native_index: int
    segment_index: int
    segment_count: int
    request_before: str | None
    request_limit: int


def _wire_id(value: Any, kind: str) -> str:
    if isinstance(value, str) and _SAFE_WIRE_ID.fullmatch(value):
        return value
    if not isinstance(value, str) or not value:
        raise CodexHistoryInvalidResponse(f"invalid Codex {kind} id")
    return hashlib.sha256(
        f"codex-history\0{kind}\0{value[:4096]}".encode(
            "utf-8", "surrogatepass")
    ).hexdigest()[:32]


def _optional_wire_id(value: Any, kind: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        return None
    return _wire_id(value, kind)


def _optional_nonnegative_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CodexHistoryInvalidResponse(
            f"invalid Codex turn {field}")
    return value


def _response_page(value: Any) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(value, dict):
        raise CodexHistoryInvalidResponse(
            "invalid Codex history response")
    data = value.get("data")
    next_cursor = value.get("nextCursor")
    if not isinstance(data, list):
        raise CodexHistoryInvalidResponse(
            "invalid Codex history data")
    if next_cursor is not None and not isinstance(next_cursor, str):
        raise CodexHistoryInvalidResponse(
            "invalid Codex history cursor")
    if isinstance(next_cursor, str) and not next_cursor:
        raise CodexHistoryInvalidResponse(
            "empty Codex history cursor")
    if not all(isinstance(row, dict) for row in data):
        raise CodexHistoryInvalidResponse(
            "invalid Codex history row")
    return data, next_cursor


def _validated_turn(
    value: dict[str, Any],
    *,
    expected_view: str,
) -> dict[str, Any]:
    native_id = value.get("id")
    status = value.get("status")
    items_view = value.get("itemsView", "full")
    items = value.get("items")
    if not isinstance(native_id, str) or not native_id:
        raise CodexHistoryInvalidResponse("Codex turn id is missing")
    if status not in _TURN_STATUSES:
        raise CodexHistoryInvalidResponse("invalid Codex turn status")
    if items_view not in _ITEM_VIEWS or items_view != expected_view:
        raise CodexHistoryInvalidResponse("unexpected Codex turn item view")
    if not isinstance(items, list) or not all(
            isinstance(item, dict) for item in items):
        raise CodexHistoryInvalidResponse("invalid Codex turn items")
    if expected_view == "notLoaded" and items:
        raise CodexHistoryInvalidResponse(
            "notLoaded Codex turn returned items")

    seen: set[str] = set()
    for item in items:
        item_id = item.get("id")
        item_type = item.get("type")
        if not isinstance(item_id, str) or not item_id:
            raise CodexHistoryInvalidResponse(
                "Codex item id is missing")
        if not isinstance(item_type, str) or not item_type:
            raise CodexHistoryInvalidResponse(
                "Codex item type is missing")
        safe_item_id = _wire_id(item_id, "item")
        if safe_item_id in seen:
            raise CodexHistoryInvalidResponse(
                "duplicate Codex item id")
        seen.add(safe_item_id)
        if item_type == "userMessage":
            content = item.get("content")
            if not isinstance(content, list) or not all(
                    isinstance(part, dict) for part in content):
                raise CodexHistoryInvalidResponse(
                    "invalid Codex user content")

    normalized = dict(value)
    normalized["startedAt"] = _optional_nonnegative_int(
        value.get("startedAt"), "startedAt")
    normalized["completedAt"] = _optional_nonnegative_int(
        value.get("completedAt"), "completedAt")
    normalized["durationMs"] = _optional_nonnegative_int(
        value.get("durationMs"), "durationMs")
    return normalized


def _user_message(item: dict[str, Any], *, ts: float | None) -> UserMsg:
    prompt_parts: list[str] = []
    recovered_images = item.get("_ccRemoteImages")
    images: list[dict[str, str]] = (
        [dict(image) for image in recovered_images]
        if isinstance(recovered_images, list)
        and all(isinstance(image, dict) for image in recovered_images)
        else []
    )
    for part in item.get("content", []):
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text")
            if not isinstance(text, str):
                raise CodexHistoryInvalidResponse(
                    "invalid Codex user text")
            prompt_parts.append(text)
        elif part_type == "image":
            url = part.get("url")
            match = _DATA_IMAGE.fullmatch(url) if isinstance(url, str) else None
            if match is None:
                continue
            media_type, data = match.groups()
            if media_type not in _IMAGE_MEDIA_TYPES:
                continue
            try:
                base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError):
                raise CodexHistoryInvalidResponse(
                    "invalid Codex image data")
            images.append({"media_type": media_type, "data": data})

    kwargs: dict[str, Any] = {
        "msg_id": _wire_id(item.get("id"), "user"),
        "client_msg_id": _optional_wire_id(
            item.get("clientId"), "client-message"),
        "prompt": "".join(prompt_parts),
        "images": images or None,
    }
    if ts is not None:
        kwargs["ts"] = ts
    try:
        return UserMsg(**kwargs)
    except (TypeError, ValueError) as exc:
        raise CodexHistoryInvalidResponse(
            "Codex user message exceeds remote limits") from exc


def _segments(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    ordered_items = items
    first_user_index = next((
        index
        for index, item in enumerate(items)
        if item.get("type") == "userMessage"
    ), None)
    if (
        first_user_index is not None
        and first_user_index > 0
        and all(
            item.get("type") == "contextCompaction"
            for item in items[:first_user_index]
        )
    ):
        # Codex can persist automatic compaction before the user item which
        # logically owns the new turn.  Leaving that prefix in place creates a
        # synthetic prompt-less continuation followed by the real question.
        # Normalize only an all-compaction leading prefix: assistant/tool
        # prefixes are genuine autonomous continuations and must stay separate.
        ordered_items = [
            items[first_user_index],
            *items[:first_user_index],
            *items[first_user_index + 1:],
        ]

    segments: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for item in ordered_items:
        if item.get("type") == "userMessage" and current:
            segments.append(current)
            current = []
        current.append(item)
    if current:
        segments.append(current)
    return segments or [[]]


def _translate_segment(
    thread_id: str,
    turn: dict[str, Any],
    segment: list[dict[str, Any]],
    *,
    segment_index: int,
    segment_count: int,
    tool_result_max: int,
) -> list[dict[str, Any]]:
    native_turn_id = _wire_id(turn["id"], "turn")
    started = turn.get("startedAt")
    completed = turn.get("completedAt")
    started_ts = float(started) if isinstance(started, int) else None
    completed_ts = float(completed) if isinstance(completed, int) else None
    translator = CodexStreamTranslator(tool_result_max)
    events: list[dict[str, Any]] = []

    def serialized(event, timestamp: float | None) -> dict[str, Any]:
        """Keep parser time out of persisted-history evidence.

        Every protocol model receives a wall-clock ``ts`` by default for live
        transport.  Official history can omit both turn timestamps, though;
        serializing that default would make the time at which the browser read
        history look like the time at which the model worked.  Only an
        app-server timestamp is authoritative here.
        """
        event.sid = thread_id
        if timestamp is not None:
            event.ts = timestamp
        payload = event.model_dump(mode="json")
        if timestamp is None:
            payload.pop("ts", None)
        return payload

    if not any(item.get("type") == "userMessage" for item in segment):
        # Automatic continuation turns have no user item. Anchor them to the
        # native turn id so an in-progress summary and its later completed
        # refresh replace the same UI turn instead of changing from the latest
        # assistant item id to the terminal turn id.
        anchor = UserMsg(
            msg_id=native_turn_id,
            prompt="",
            sid=thread_id,
        )
        events.append(serialized(anchor, started_ts))

    for item in segment:
        if item.get("type") == "userMessage":
            event = _user_message(item, ts=started_ts)
            events.append(serialized(event, started_ts))
            continue
        if (
            item.get("type") == "agentMessage"
            and not isinstance(item.get("text"), str)
        ) or (
            item.get("type") == "agentMessage"
            and not item.get("text")
        ):
            # Persisted full views may contain lifecycle-only message shells.
            # They are useful to the native client but become empty chat
            # bubbles after translation, so keep them out of the projection.
            continue
        translated = translator.feed({
            "method": "item/completed",
            "params": {
                "threadId": thread_id,
                "turnId": native_turn_id,
                "item": item,
            },
        })
        events.extend(serialized(event, started_ts) for event in translated)

    final_segment = segment_index == segment_count - 1
    status = turn["status"]
    if final_segment and status != "inProgress":
        translated = translator.feed({
            "method": "turn/completed",
            "params": {
                "threadId": thread_id,
                "turn": {
                    "id": native_turn_id,
                    "status": status,
                    "durationMs": turn.get("durationMs") or 0,
                    "error": (
                        turn.get("error")
                        if status == "failed"
                        else None
                    ),
                },
            },
        })
        events.extend(serialized(event, completed_ts) for event in translated)
    elif not final_segment:
        terminal = TurnEnd(
            result=TurnResult(
                subtype="steered",
                duration_ms=0,
                is_error=False,
            ),
            sid=thread_id,
        )
        events.append(serialized(terminal, started_ts))

    return events


def _translate_turn(
    thread_id: str,
    turn: dict[str, Any],
    *,
    tool_result_max: int,
) -> list[list[dict[str, Any]]]:
    item_segments = _segments(turn["items"])
    return [
        _translate_segment(
            thread_id,
            turn,
            segment,
            segment_index=index,
            segment_count=len(item_segments),
            tool_result_max=tool_result_max,
        )
        for index, segment in enumerate(item_segments)
    ]


def _unsupported(exc: CodexRpcRejected) -> bool:
    return exc.code == -32601


def _needs_terminal_full_refresh(
    summary_turn: dict[str, Any],
    cached_full: dict[str, Any] | None,
) -> bool:
    """Return whether an active full snapshot predates terminal content."""
    return bool(
        cached_full is not None
        and cached_full.get("status") == "inProgress"
        and summary_turn.get("status") != "inProgress"
    )


def _merge_terminal_summary_messages(
    cached_items: list[dict[str, Any]],
    summary_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep full shape while restoring terminal messages after read failure."""
    merged = list(cached_items)
    indices = {
        item.get("id"): index
        for index, item in enumerate(merged)
        if isinstance(item.get("id"), str)
    }
    for item in summary_items:
        item_type = item.get("type")
        if item_type not in {"userMessage", "agentMessage"}:
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str):
            continue
        index = indices.get(item_id)
        if index is None:
            indices[item_id] = len(merged)
            merged.append(item)
        elif item_type == "agentMessage":
            # A full user item may retain richer content than its summary
            # counterpart (notably attachment metadata). User content is
            # immutable once its stable id exists, while terminal agent shells
            # legitimately gain their final text and phase.
            merged[index] = item
    return merged


class CodexOfficialHistory:
    """Generation-local official history reader with opaque cursor isolation."""

    def __init__(
        self,
        tool_result_max: int,
        *,
        rpc: _Rpc = codex_rpc,
        recover_user: _RecoverUser | None = None,
        recover_users: _RecoverUsers | None = None,
    ) -> None:
        self.tool_result_max = tool_result_max
        self._rpc = rpc
        self._recover_user = recover_user
        self._recover_users = recover_users
        self._before_cursors: OrderedDict[
            tuple[str, str], str | None
        ] = OrderedDict()
        self._locators: OrderedDict[
            tuple[str, str], _TurnLocator
        ] = OrderedDict()
        self._summary_events: OrderedDict[
            tuple[str, str], tuple[dict[str, Any], ...]
        ] = OrderedDict()
        # Only authoritative prompts belong here. A recovery miss can be
        # temporary while Codex is still flushing the correlated Goal records;
        # caching None would permanently hide that prompt from later pages.
        self._automatic_users: OrderedDict[
            tuple[str, str], UserMsg
        ] = OrderedDict()
        # The official summary view collapses same-turn steer boundaries to the
        # first user and latest assistant item. Once an active turn has been
        # hydrated from the exact full view, retain that item shape across its
        # terminal summary refresh so completed history cannot erase the steer
        # prompts and work that the browser already saw.
        self._native_full_turns: OrderedDict[
            tuple[str, str], dict[str, Any]
        ] = OrderedDict()
        # A terminal summary is sufficient after the official full view proves
        # permanently unavailable (unsupported or over the bounded RPC size).
        # Keep that fact separate from the exact full cache: explicit detail
        # requests may still use thread/items/list, while passive refreshes must
        # not repeat the same oversized full-turn request forever.
        self._terminal_refresh_fallbacks: OrderedDict[
            tuple[str, str], None
        ] = OrderedDict()
        # Full/summary official items can expose the exact native user id and
        # Remote clientId even when a moving active head later forces rollout
        # fallback. Keep only these bounded ids so Machine can strengthen its
        # source-bound durable alias without retaining another history copy.
        self._client_message_identities: OrderedDict[
            tuple[str, str], tuple[str, str]
        ] = OrderedDict()
        self._detail_events: OrderedDict[
            tuple[str, str], tuple[dict[str, Any], ...]
        ] = OrderedDict()
        self._detail_sources: OrderedDict[
            tuple[str, str], str
        ] = OrderedDict()
        self._detail_event_bytes: dict[tuple[str, str], int] = {}
        self._detail_cache_bytes = 0

    def remember_automatic_user(
        self,
        thread_id: str,
        native_turn_id: str,
        user: UserMsg,
    ) -> None:
        """Remember the authoritative live Goal prompt for one native turn."""
        if not user.prompt:
            return
        self._remember(
            self._automatic_users,
            (thread_id, native_turn_id),
            user,
        )

    def has_automatic_user(
        self,
        thread_id: str,
        native_turn_id: str,
    ) -> bool:
        """Return whether a live or recovered Goal prompt is authoritative."""
        return (thread_id, native_turn_id) in self._automatic_users

    def _remember_client_message_identities(
        self,
        thread_id: str,
        native_turn_id: str,
        items: list[dict[str, Any]],
    ) -> None:
        for item in items:
            if item.get("type") != "userMessage":
                continue
            client_message_id = _optional_wire_id(
                item.get("clientId"), "client-message")
            if client_message_id is None:
                continue
            native_message_id = _wire_id(item.get("id"), "user")
            key = (thread_id, native_message_id)
            value = (native_turn_id, client_message_id)
            previous = self._client_message_identities.get(key)
            if previous is not None and previous != value:
                raise CodexHistoryInvalidResponse(
                    "Codex client-message identity changed")
            self._remember(self._client_message_identities, key, value)

    def take_client_message_identities(
        self,
        thread_id: str,
    ) -> tuple[tuple[str, str, str], ...]:
        """Drain exact official ids observed during the latest page reads."""
        identities: list[tuple[str, str, str]] = []
        for key in list(self._client_message_identities):
            if key[0] != thread_id:
                continue
            native_turn_id, client_message_id = (
                self._client_message_identities.pop(key))
            identities.append((native_turn_id, key[1], client_message_id))
        return tuple(identities)

    async def _call(self, method: str, params: dict[str, Any]) -> Any:
        return await self._rpc(method, params, None)

    @staticmethod
    def _remember(
        mapping: OrderedDict,
        key: Any,
        value: Any,
    ) -> None:
        mapping[key] = value
        mapping.move_to_end(key)
        while len(mapping) > _MAX_LOCATORS:
            mapping.popitem(last=False)

    def _remember_terminal_refresh_fallback(
        self,
        key: tuple[str, str],
    ) -> None:
        self._terminal_refresh_fallbacks[key] = None
        self._terminal_refresh_fallbacks.move_to_end(key)
        while (
            len(self._terminal_refresh_fallbacks)
            > _MAX_TERMINAL_REFRESH_FALLBACKS
        ):
            self._terminal_refresh_fallbacks.popitem(last=False)

    async def summary_page(
        self,
        thread_id: str,
        *,
        before: str | None,
        limit: int,
        include_live_detail: bool = False,
        active_turn_ids: set[str] | frozenset[str] = frozenset(),
        hydrate_recent: int = 0,
        client_message_ids: dict[str, str] | None = None,
        segment_client_message_ids: (
            dict[tuple[str, int], str] | None
        ) = None,
    ) -> CodexHistoryPage:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("Codex history limit must be between 1 and 200")
        if (
            not isinstance(hydrate_recent, int)
            or isinstance(hydrate_recent, bool)
            or hydrate_recent < 0
            or hydrate_recent > limit
        ):
            raise ValueError(
                "Codex recent history hydration must fit inside the page")
        page_cursor = None
        if before is not None:
            key = (thread_id, before)
            if key not in self._before_cursors:
                raise CodexHistoryCursorError(
                    "Codex history cursor belongs to another page or generation")
            page_cursor = self._before_cursors[key]
            if page_cursor is None:
                raise CodexHistoryCursorError(
                    "Codex history has no older page")

        try:
            response = await self._call("thread/turns/list", {
                "threadId": thread_id,
                "cursor": page_cursor,
                "limit": limit,
                "sortDirection": "desc",
                "itemsView": "summary",
            })
        except CodexRpcRejected as exc:
            if _unsupported(exc):
                raise CodexHistoryUnsupported(
                    "thread/turns/list summary is unsupported") from exc
            raise

        rows, next_cursor = _response_page(response)
        validated_rows = [
            _validated_turn(row, expected_view="summary")
            for row in rows
        ]
        prefetched_full: dict[int, dict[str, Any]] = {}
        terminal_refresh_indices: dict[int, tuple[str, str]] = {}
        for index, turn in enumerate(validated_rows):
            native_id = _wire_id(turn["id"], "turn")
            key = (thread_id, native_id)
            if (
                before is None
                and index < hydrate_recent
                and key not in self._terminal_refresh_fallbacks
                and _needs_terminal_full_refresh(
                    turn, self._native_full_turns.get(key))
            ):
                terminal_refresh_indices[index] = key
        hydration_indices = [
            index
            for index, turn in enumerate(validated_rows)
            if (
                _wire_id(turn["id"], "turn") in active_turn_ids
                or (
                    index < hydrate_recent
                    and (
                        (
                            thread_id,
                            _wire_id(turn["id"], "turn"),
                        ) not in self._native_full_turns
                        or index in terminal_refresh_indices
                    )
                )
            )
        ]
        failed_terminal_prefetch: set[tuple[str, str]] = set()
        # A single descending full page is cheaper than walking an opaque cursor
        # once per recent row. Include any preceding cached row only when needed
        # to reach a later uncached row.
        recent_count = (
            max(hydration_indices) + 1 if hydration_indices else 0
        )
        if recent_count:
            try:
                full_response = await self._call("thread/turns/list", {
                    "threadId": thread_id,
                    "cursor": page_cursor,
                    "limit": recent_count,
                    "sortDirection": "desc",
                    "itemsView": "full",
                })
                full_rows, _full_next_cursor = _response_page(full_response)
                if len(full_rows) != recent_count:
                    raise CodexHistoryInvalidResponse(
                        "Codex recent full page changed length")
                for index, full_row in enumerate(full_rows):
                    full_turn = _validated_turn(
                        full_row, expected_view="full")
                    if _wire_id(
                        full_turn["id"], "turn"
                    ) != _wire_id(validated_rows[index]["id"], "turn"):
                        raise CodexHistoryInvalidResponse(
                            "Codex recent full page changed identity")
                    prefetched_full[index] = full_turn
            except CodexRpcRejected as exc:
                if not _unsupported(exc):
                    raise
                failed_terminal_prefetch.update(
                    terminal_refresh_indices.values())
                for key in failed_terminal_prefetch:
                    self._remember_terminal_refresh_fallback(key)
            except CodexRpcResponseTooLarge:
                failed_terminal_prefetch.update(
                    terminal_refresh_indices.values())
                for key in failed_terminal_prefetch:
                    self._remember_terminal_refresh_fallback(key)
                prefetched_full.clear()
            except CodexHistoryInvalidResponse:
                # A combined recent page is an optimization. Exact active-turn
                # hydration below remains mandatory; completed rows can retain
                # their valid summary if the native page moved during the read.
                # Invalid/moving pages are retried on a later refresh, but never
                # issue the identical request twice in this one refresh.
                failed_terminal_prefetch.update(
                    terminal_refresh_indices.values())
                prefetched_full.clear()
        native_seen: set[str] = set()
        visible_seen: set[str] = set()
        projected_rows: list[dict[str, Any]] = []
        chronological_segments: list[
            tuple[list[dict[str, Any]], _TurnLocator, bool]
        ] = []
        if self._recover_users is not None:
            missing_automatic: list[str] = []
            for native_index, summary_turn in enumerate(validated_rows):
                turn = prefetched_full.get(native_index, summary_turn)
                if any(
                    item.get("type") == "userMessage"
                    for item in turn["items"]
                ):
                    continue
                native_id = _wire_id(turn["id"], "turn")
                if (thread_id, native_id) not in self._automatic_users:
                    missing_automatic.append(native_id)
            if missing_automatic:
                recovered_users = await self._recover_users(
                    thread_id, tuple(missing_automatic))
                if not isinstance(recovered_users, dict):
                    raise CodexHistoryInvalidResponse(
                        "invalid automatic Codex user recovery")
                for native_id in missing_automatic:
                    recovered = recovered_users.get(native_id)
                    if recovered is not None and not isinstance(
                        recovered, UserMsg
                    ):
                        raise CodexHistoryInvalidResponse(
                            "invalid automatic Codex user row")
                    if recovered is not None and recovered.prompt:
                        self._remember(
                            self._automatic_users,
                            (thread_id, native_id),
                            recovered,
                        )
        for native_index, summary_turn in enumerate(validated_rows):
            turn = prefetched_full.get(native_index, summary_turn)
            native_id = _wire_id(turn["id"], "turn")
            if native_id in native_seen:
                raise CodexHistoryInvalidResponse(
                    "duplicate Codex turn id")
            native_seen.add(native_id)
            locator = _TurnLocator(
                native_turn_id=native_id,
                page_cursor=page_cursor,
                native_index=native_index,
                segment_index=0,
                segment_count=1,
                request_before=before,
                request_limit=limit,
            )
            active_turn = native_id in active_turn_ids
            cached_full = self._native_full_turns.get(
                (thread_id, native_id))
            terminal_refresh = _needs_terminal_full_refresh(
                summary_turn, cached_full)
            terminal_fallback = (
                (thread_id, native_id)
                in self._terminal_refresh_fallbacks
            )
            hydrate_turn = active_turn or (
                hydrate_recent > native_index
                and before is None
                and (
                    cached_full is None
                    or (terminal_refresh and not terminal_fallback)
                )
            )
            if (
                hydrate_turn
                and turn.get("itemsView") != "full"
                and (
                    active_turn
                    or (thread_id, native_id)
                    not in failed_terminal_prefetch
                )
            ):
                try:
                    turn = await self._full_turn(thread_id, locator)
                except CodexRpcRejected as exc:
                    if not _unsupported(exc):
                        raise
                    if active_turn:
                        raise CodexHistoryUnsupported(
                            "active Codex full turn is unsupported") from exc
                except (
                    CodexHistoryInvalidResponse,
                    CodexRpcResponseTooLarge,
                ) as exc:
                    if active_turn:
                        raise CodexHistoryUnsupported(
                            "active Codex full turn is incompatible") from exc
            if turn.get("itemsView") == "full":
                self._terminal_refresh_fallbacks.pop(
                    (thread_id, native_id), None)
                if active_turn:
                    # App-server 0.147 can persist ``interrupted`` on a steered
                    # native turn while that exact turn continues producing
                    # items. The wrapper's ordered rollout lifecycle is
                    # authoritative evidence that it is active.
                    turn["status"] = "inProgress"
                    turn["completedAt"] = None
                    turn["durationMs"] = None
                self._remember(
                    self._native_full_turns,
                    (thread_id, native_id),
                    dict(turn),
                )
                while (
                    len(self._native_full_turns)
                    > _MAX_ACTIVE_TURN_CACHE_ENTRIES
                ):
                    self._native_full_turns.popitem(last=False)
            if turn.get("itemsView") != "full":
                if cached_full is not None:
                    # Keep the exact full item sequence but accept lifecycle and
                    # timing from the newest official summary response. If the
                    # terminal full read failed, at least merge its authoritative
                    # terminal user/agent messages so a full snapshot captured
                    # between compaction and the user-item flush cannot retain
                    # an empty prompt or hide the completed answer.
                    items = cached_full["items"]
                    if terminal_refresh:
                        items = _merge_terminal_summary_messages(
                            items, summary_turn["items"])
                    turn = {
                        **cached_full,
                        "items": items,
                        "status": turn["status"],
                        "startedAt": turn.get("startedAt"),
                        "completedAt": turn.get("completedAt"),
                        "durationMs": turn.get("durationMs"),
                        "error": turn.get("error"),
                    }
                    self._native_full_turns.move_to_end(
                        (thread_id, native_id))
            if self._recover_user is not None or self._recover_users is not None:
                copied_items = None
                user_index = 0
                for item_index, item in enumerate(turn["items"]):
                    if item.get("type") != "userMessage":
                        continue
                    if self._recover_user is not None and any(
                        part.get("type") == "localImage"
                        for part in item.get("content", [])
                    ):
                        visible_id = _wire_id(item.get("id"), "user")
                        recovered = await self._recover_user(
                            thread_id,
                            native_id,
                            visible_id,
                            user_index,
                        )
                        if recovered is not None and recovered.images:
                            if copied_items is None:
                                copied_items = [
                                    dict(row) for row in turn["items"]]
                            copied_items[item_index][
                                "_ccRemoteImages"
                            ] = [
                                dict(image) for image in recovered.images]
                    user_index += 1
                if copied_items is not None:
                    turn["items"] = copied_items
                if user_index == 0:
                    automatic_key = (thread_id, native_id)
                    if automatic_key in self._automatic_users:
                        recovered = self._automatic_users[automatic_key]
                        self._automatic_users.move_to_end(automatic_key)
                    elif self._recover_users is None:
                        recovered = await self._recover_user(
                            thread_id,
                            native_id,
                            native_id,
                            0,
                        )
                        if recovered is not None and recovered.prompt:
                            self._remember(
                                self._automatic_users,
                                automatic_key,
                                recovered,
                            )
                    else:
                        recovered = None
                    if recovered is not None and recovered.prompt:
                        turn["items"] = [{
                            "type": "userMessage",
                            "id": native_id,
                            "clientId": recovered.client_msg_id,
                            "content": [{
                                "type": "text",
                                "text": recovered.prompt,
                            }],
                            "_ccRemoteImages": [
                                dict(image) for image in recovered.images or []
                            ],
                        }, *turn["items"]]
            self._remember_client_message_identities(
                thread_id,
                native_id,
                turn["items"],
            )
            projected_rows.append(turn)
            segments = _translate_turn(
                thread_id, turn, tool_result_max=self.tool_result_max)
            for segment_index, segment_events in enumerate(segments):
                materialized = materialize_history_turns(
                    segment_events,
                    include_live_detail=include_live_detail,
                )
                if len(materialized) != 1:
                    raise CodexHistoryInvalidResponse(
                        "Codex turn did not produce one visible segment")
                visible_id = materialized[0].get("id")
                if not isinstance(visible_id, str) or visible_id in visible_seen:
                    raise CodexHistoryInvalidResponse(
                        "duplicate Codex visible turn id")
                visible_seen.add(visible_id)
                chronological_segments.append((
                    segment_events,
                    _TurnLocator(
                        native_turn_id=native_id,
                        page_cursor=page_cursor,
                        native_index=native_index,
                        segment_index=segment_index,
                        segment_count=len(segments),
                        request_before=before,
                        request_limit=limit,
                    ),
                    turn.get("itemsView") == "full",
                ))

        if active_turn_ids and before is None and not (
                native_seen.intersection(active_turn_ids)):
            # A moving native head that is absent from the official page must
            # use the bounded rollout compatibility source. Treating the older
            # summary row as terminal would replace the live UI with stale data.
            raise CodexHistoryUnsupported(
                "active Codex turn is absent from the official head")

        # The official page is descending by native turn. Preserve the order of
        # steer segments inside each native turn while reversing native turns.
        grouped: list[list[
            tuple[list[dict[str, Any]], _TurnLocator, bool]
        ]] = []
        offset = 0
        for projected_turn in projected_rows:
            count = len(_segments(projected_turn.get("items", [])))
            grouped.append(chronological_segments[offset:offset + count])
            offset += count
        ordered = [
            segment
            for native_group in reversed(grouped)
            for segment in native_group
        ]
        # Official pagination counts native turns, whereas the rollout
        # compatibility reader counts visible user segments. One native turn
        # may contain several steer segments, so replaying the native request
        # limit can trim the oldest visible row out of its own detail fallback.
        # Preserve the exact source page but widen its visible-row budget to the
        # number this official page actually materialized.
        rollout_fallback_limit = max(limit, len(ordered))
        if rollout_fallback_limit != limit:
            ordered = [
                (
                    segment_events,
                    replace(
                        locator,
                        request_limit=rollout_fallback_limit,
                    ),
                    process_detail_exact,
                )
                for segment_events, locator, process_detail_exact in ordered
            ]

        native_aliases = client_message_ids or {}
        segment_aliases = segment_client_message_ids or {}
        for segment_events, locator, _process_detail_exact in ordered:
            user = next((
                event for event in segment_events
                if event.get("type") == "user_msg"
            ), None)
            if user is None or user.get("client_msg_id") is not None:
                continue
            native_message_id = user.get("msg_id")
            alias = (
                native_aliases.get(native_message_id)
                if isinstance(native_message_id, str) else None
            )
            if alias is None:
                alias = segment_aliases.get((
                    locator.native_turn_id,
                    locator.segment_index,
                ))
            if alias is not None:
                user["client_msg_id"] = alias

        events = tuple(
            event
            for segment_events, _locator, _process_detail_exact in ordered
            for event in segment_events
        )
        turns = materialize_history_turns(
            events,
            include_live_detail=include_live_detail,
        )
        if len(turns) != len(ordered):
            raise CodexHistoryInvalidResponse(
                "Codex page segment count changed during projection")
        for turn, (
            segment_events,
            locator,
            process_detail_exact,
        ) in zip(turns, ordered):
            visible_id = turn["id"]
            if (
                locator.native_turn_id in active_turn_ids
                and locator.segment_index == locator.segment_count - 1
            ):
                turn["forkPointId"] = locator.native_turn_id
            # A native summary proves that more item data can be requested, but
            # it does not prove that the hidden payload contains a process the
            # UI is allowed to show. Keep that uncertainty distinct from both
            # exact direct answers and exact process-bearing full projections.
            if (
                not process_detail_exact
                and turn.get("processDetailState") == "none"
            ):
                turn["processDetailState"] = "unknown"
            process_started = turn.get("processStartedTs")
            process_done = turn.get("processDoneTs")
            if (
                isinstance(process_started, int)
                and isinstance(process_done, int)
                and process_done - process_started < MIN_PROCESS_DURATION_MS
            ):
                # Full native items inherit the turn-level second timestamp,
                # so parsing several real process items can otherwise invent a
                # misleading 0s interval. Presence remains exact; timing waits
                # for a source-backed witness with distinct event timestamps.
                turn.pop("processStartedTs", None)
                turn.pop("processDoneTs", None)
            self._remember(
                self._locators, (thread_id, visible_id), locator)
            previous_summary = self._summary_events.get(
                (thread_id, visible_id))
            if previous_summary != tuple(segment_events):
                self._drop_detail((thread_id, visible_id))
            self._remember(
                self._summary_events,
                (thread_id, visible_id),
                tuple(segment_events),
            )

        native_segment_by_visible_id = {
            turn["id"]: (
                locator.native_turn_id,
                locator.segment_index,
            )
            for turn, (_events, locator, _exact) in zip(turns, ordered)
        }

        oldest_id = turns[0]["id"] if turns else None
        newest_id = turns[-1]["id"] if turns else None
        if next_cursor is not None and oldest_id is None:
            raise CodexHistoryInvalidResponse(
                "Codex history page has a cursor but no visible turns")
        if oldest_id is not None:
            self._remember(
                self._before_cursors,
                (thread_id, oldest_id),
                next_cursor,
            )
        return CodexHistoryPage(
            events=events,
            turns=turns,
            has_more=next_cursor is not None,
            oldest_id=oldest_id,
            newest_id=newest_id,
            native_turn_ids=tuple(
                _wire_id(turn["id"], "turn")
                for turn in validated_rows
            ),
            native_segment_by_visible_id=native_segment_by_visible_id,
        )

    def _locator(self, thread_id: str, visible_turn_id: str) -> _TurnLocator:
        locator = self._locators.get((thread_id, visible_turn_id))
        if locator is None:
            raise CodexHistoryCursorError(
                "Codex turn detail belongs to another page or generation")
        return locator

    def _drop_detail(self, key: tuple[str, str]) -> None:
        self._detail_events.pop(key, None)
        self._detail_sources.pop(key, None)
        size = self._detail_event_bytes.pop(key, 0)
        self._detail_cache_bytes = max(
            0, self._detail_cache_bytes - size)

    def _remember_detail(
        self,
        key: tuple[str, str],
        events: tuple[dict[str, Any], ...],
        source: str,
    ) -> None:
        encoded_bytes = len(json.dumps(
            events,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"))
        self._drop_detail(key)
        if encoded_bytes > _MAX_DETAIL_CACHE_BYTES:
            return
        self._detail_events[key] = events
        self._detail_events.move_to_end(key)
        self._detail_sources[key] = source
        self._detail_sources.move_to_end(key)
        self._detail_event_bytes[key] = encoded_bytes
        self._detail_cache_bytes += encoded_bytes
        while (
            len(self._detail_events) > _MAX_DETAIL_CACHE_ENTRIES
            or self._detail_cache_bytes > _MAX_DETAIL_CACHE_BYTES
        ):
            oldest, _events = self._detail_events.popitem(last=False)
            self._detail_sources.pop(oldest, None)
            size = self._detail_event_bytes.pop(oldest, 0)
            self._detail_cache_bytes = max(
                0, self._detail_cache_bytes - size)

    async def _items_for_turn(
        self,
        thread_id: str,
        native_turn_id: str,
    ) -> list[dict[str, Any]]:
        cursor = None
        seen_cursors: set[str] = set()
        items: list[dict[str, Any]] = []
        seen_items: set[str] = set()
        page_count = 0
        while True:
            if page_count >= _MAX_ITEM_PAGES:
                raise CodexHistoryInvalidResponse(
                    "Codex item pagination exceeded its page limit")
            page_count += 1
            response = await self._call("thread/items/list", {
                "threadId": thread_id,
                "turnId": native_turn_id,
                "cursor": cursor,
                "limit": _ITEM_PAGE_LIMIT,
                "sortDirection": "asc",
            })
            rows, next_cursor = _response_page(response)
            if not rows and next_cursor is not None:
                raise CodexHistoryInvalidResponse(
                    "Codex item page is empty before its terminal cursor")
            for row in rows:
                if not isinstance(row, dict):
                    raise CodexHistoryInvalidResponse(
                        "invalid Codex item entry")
                if row.get("turnId") != native_turn_id:
                    raise CodexHistoryInvalidResponse(
                        "Codex item belongs to another turn")
                item = row.get("item")
                if not isinstance(item, dict):
                    raise CodexHistoryInvalidResponse(
                        "Codex item entry is missing its item")
                # Reuse the full-turn validator for item shape and duplicate
                # checks without duplicating the public ThreadItem boundary.
                checked = _validated_turn({
                    "id": native_turn_id,
                    "status": "completed",
                    "itemsView": "full",
                    "items": [item],
                }, expected_view="full")
                normalized_item = checked["items"][0]
                item_id = _wire_id(normalized_item["id"], "item")
                if item_id in seen_items:
                    raise CodexHistoryInvalidResponse(
                        "duplicate Codex item page entry")
                seen_items.add(item_id)
                items.append(normalized_item)
                if len(items) > _MAX_DETAIL_ITEMS:
                    raise CodexHistoryUnsupported(
                        "Codex turn exceeds the bounded official item projection")
            if next_cursor is None:
                return items
            if next_cursor in seen_cursors:
                raise CodexHistoryInvalidResponse(
                    "Codex item cursor repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    async def _full_turn(
        self,
        thread_id: str,
        locator: _TurnLocator,
    ) -> dict[str, Any]:
        cursor = locator.page_cursor
        for _index in range(locator.native_index):
            response = await self._call("thread/turns/list", {
                "threadId": thread_id,
                "cursor": cursor,
                "limit": 1,
                "sortDirection": "desc",
                "itemsView": "notLoaded",
            })
            rows, next_cursor = _response_page(response)
            if len(rows) != 1:
                raise CodexHistoryInvalidResponse(
                    "Codex detail locator no longer resolves")
            _validated_turn(rows[0], expected_view="notLoaded")
            if next_cursor is None:
                raise CodexHistoryInvalidResponse(
                    "Codex detail locator ended early")
            cursor = next_cursor

        response = await self._call("thread/turns/list", {
            "threadId": thread_id,
            "cursor": cursor,
            "limit": 1,
            "sortDirection": "desc",
            "itemsView": "full",
        })
        rows, _next_cursor = _response_page(response)
        if len(rows) != 1:
            raise CodexHistoryInvalidResponse(
                "Codex detail turn is missing")
        turn = _validated_turn(rows[0], expected_view="full")
        if _wire_id(turn["id"], "turn") != locator.native_turn_id:
            raise CodexHistoryInvalidResponse(
                "Codex detail cursor resolved another turn")
        return turn

    async def turn_events(
        self,
        thread_id: str,
        visible_turn_id: str,
    ) -> tuple[dict[str, Any], ...]:
        key = (thread_id, visible_turn_id)
        cached = self._detail_events.get(key)
        if cached is not None:
            self._detail_events.move_to_end(key)
            return cached
        locator = self._locator(thread_id, visible_turn_id)
        detail_source = "items"
        try:
            try:
                try:
                    items = await self._items_for_turn(
                        thread_id, locator.native_turn_id)
                except CodexRpcRejected as exc:
                    if not _unsupported(exc):
                        raise
                    items = None
                except (
                    CodexHistoryInvalidResponse,
                    CodexRpcResponseTooLarge,
                ):
                    items = None
                if items is None:
                    turn = await self._full_turn(thread_id, locator)
                    detail_source = "full"
                else:
                    # Item pages do not carry status/timing. Those values were
                    # already validated in summary and remain bound to the
                    # locator.
                    summary_events = self._summary_events.get(
                        (thread_id, visible_turn_id), ())
                    summary_turns = materialize_history_turns(summary_events)
                    summary = (
                        summary_turns[0] if len(summary_turns) == 1 else {})
                    status = (
                        "interrupted" if summary.get("interrupted")
                        else "failed" if summary.get("error")
                        else "completed" if summary.get("done")
                        else "inProgress"
                    )
                    turn = {
                        "id": locator.native_turn_id,
                        "status": status,
                        "itemsView": "full",
                        "items": items,
                        "startedAt": (
                            summary.get("ts", 0) // 1000
                            if isinstance(summary.get("ts"), int)
                            else None
                        ),
                        "completedAt": (
                            summary.get("doneTs", 0) // 1000
                            if isinstance(summary.get("doneTs"), int)
                            else None
                        ),
                        "durationMs": summary.get("durationMs"),
                    }
            except (
                CodexHistoryInvalidResponse,
                CodexRpcResponseTooLarge,
            ) as exc:
                raise CodexHistoryUnsupported(
                    "official Codex turn detail is incompatible") from exc
        except CodexRpcRejected as exc:
            if _unsupported(exc):
                raise CodexHistoryUnsupported(
                    "official Codex turn detail is unsupported") from exc
            raise

        segments = _translate_turn(
            thread_id, turn, tool_result_max=self.tool_result_max)
        selected = None
        for segment in segments:
            projected = materialize_history_turns(segment)
            if (
                len(projected) == 1
                and projected[0].get("id") == visible_turn_id
            ):
                selected = segment
                break
        if selected is None:
            raise CodexHistoryInvalidResponse(
                "Codex detail segment no longer exists")
        events = tuple(selected)
        turns = materialize_history_turns(events)
        if len(turns) != 1 or turns[0].get("id") != visible_turn_id:
            raise CodexHistoryInvalidResponse(
                "Codex detail resolved another visible turn")
        self._remember_detail(key, events, detail_source)
        return events

    def turn_detail_source(
        self,
        thread_id: str,
        visible_turn_id: str,
    ) -> str | None:
        """Return whether cached detail came from item pages or a full turn.

        ``thread/items/list`` is the only official API which exposes the full
        process sequence in current app-server builds.  ``itemsView=full`` can
        be structurally valid while omitting commands, so callers supplement
        that source from the bounded rollout projection before presenting it as
        complete history.
        """
        key = (thread_id, visible_turn_id)
        source = self._detail_sources.get(key)
        if source is not None:
            self._detail_sources.move_to_end(key)
        return source

    def invalidate_thread(self, thread_id: str) -> None:
        """Drop every generation-local cursor/cache entry for one thread."""
        for mapping in (
            self._before_cursors,
            self._locators,
            self._summary_events,
            self._automatic_users,
            self._native_full_turns,
            self._terminal_refresh_fallbacks,
        ):
            for key in tuple(mapping):
                if key[0] == thread_id:
                    mapping.pop(key, None)
        for key in tuple(self._detail_events):
            if key[0] == thread_id:
                self._drop_detail(key)

    def rollout_fallback(
        self,
        thread_id: str,
        visible_turn_id: str,
    ) -> CodexRolloutFallback:
        locator = self._locator(thread_id, visible_turn_id)
        return CodexRolloutFallback(
            before=locator.request_before,
            limit=locator.request_limit,
            native_turn_id=locator.native_turn_id,
            segment_index=locator.segment_index,
            segment_count=locator.segment_count,
        )

    def summary_events(
        self,
        thread_id: str,
        visible_turn_id: str,
    ) -> tuple[dict[str, Any], ...] | None:
        return self._summary_events.get((thread_id, visible_turn_id))
