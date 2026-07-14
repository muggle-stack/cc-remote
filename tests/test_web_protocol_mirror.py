"""Mechanical guards for the hand-maintained browser protocol mirror."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import get_args

from cc_remote.protocol import (
    AssistantChannel,
    CodexThreadStatus,
    CollaborationModeName,
    EffortLevel,
    Engine,
    GoalStatus,
    NoticeCategory,
    NoticeSeverity,
    PermissionMode,
    ProcessAppendTarget,
    ProcessKind,
    ProcessPhase,
    ProcessStatus,
    SetServiceTier,
    State,
    ThreadGoal,
    ToolCategory,
    _Command,
    _TYPE_MAP,
    PROTOCOL_VERSION,
    SessionInfo,
)


ROOT = Path(__file__).resolve().parents[1]
TS_PROTOCOL = (ROOT / "web/src/protocol.ts").read_text()
TS_REDUCER = (ROOT / "web/src/reducer.ts").read_text()
TS_PROCESS_TIMELINE = (ROOT / "web/src/components/ProcessTimeline.tsx").read_text()


def _without_typescript_comments(source: str) -> str:
    return re.sub(
        r"/\*.*?\*/|//[^\n]*",
        lambda match: "".join(
            "\n" if char == "\n" else " " for char in match.group()
        ),
        source,
        flags=re.DOTALL,
    )


def _interface_blocks(source: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for match in re.finditer(r"(?:export\s+)?interface\s+(\w+)[^{]*\{", source):
        name = match.group(1)
        start = match.end() - 1
        depth = 0
        quote: str | None = None
        escaped = False
        for index in range(start, len(source)):
            char = source[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in {'"', "'", "`"}:
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    blocks[name] = source[start + 1:index]
                    break
    return blocks


INTERFACES = _interface_blocks(_without_typescript_comments(TS_PROTOCOL))


def _field_optional(interface: str, field: str) -> bool | None:
    body = "\n" + INTERFACES[interface]
    match = re.search(
        rf"(?:^|[;\n])\s*{re.escape(field)}\s*(\?)?\s*:", body,
        re.MULTILINE,
    )
    return None if match is None else bool(match.group(1))


def _wire_interfaces() -> dict[str, str]:
    result: dict[str, str] = {}
    for name, body in INTERFACES.items():
        match = re.search(r'\btype\s*:\s*"([a-z_]+)"', body)
        if match:
            result[match.group(1)] = name
    return result


def _alias_literals(name: str) -> set[str]:
    match = re.search(
        rf"export type\s+{re.escape(name)}\s*=\s*(.*?);",
        TS_PROTOCOL,
        re.DOTALL,
    )
    assert match, f"TypeScript protocol alias missing: {name}"
    return set(re.findall(r'"([^"]*)"', match.group(1)))


def test_every_python_wire_type_has_a_typescript_interface():
    mirrored = _wire_interfaces()
    assert set(_TYPE_MAP) - set(mirrored) == set()

    # Catch fields added to concrete wire classes even when the event name was
    # remembered. Inherited envelope fields are checked separately below.
    for wire_type, model in _TYPE_MAP.items():
        body_name = mirrored[wire_type]
        for field in model.__annotations__:
            if field == "type":
                continue
            assert _field_optional(body_name, field) is not None, (
                f"{body_name} is missing Python field {field!r}"
            )


def test_client_command_requiredness_matches_pydantic_acceptance():
    mirrored = _wire_interfaces()
    for wire_type, model in _TYPE_MAP.items():
        if not issubclass(model, _Command):
            continue
        body_name = mirrored[wire_type]
        for field in model.__annotations__:
            if field == "type":
                continue
            optional = _field_optional(body_name, field)
            assert optional is not None
            assert optional is (not model.model_fields[field].is_required()), (
                f"{body_name}.{field} requiredness drifted from Pydantic"
            )


def test_nested_session_and_goal_models_keep_fields_and_requiredness():
    for ts_name, model in (("SessionInfo", SessionInfo), ("ThreadGoal", ThreadGoal)):
        for field, definition in model.model_fields.items():
            optional = _field_optional(ts_name, field)
            assert optional is not None, f"{ts_name} is missing {field!r}"
            assert optional is (not definition.is_required()), (
                f"{ts_name}.{field} requiredness drifted from Pydantic"
            )


def test_literal_unions_match_python_protocol():
    aliases = {
        "State": State,
        "Engine": Engine,
        "AssistantChannel": AssistantChannel,
        "ToolCategory": ToolCategory,
        "ProcessKind": ProcessKind,
        "ProcessPhase": ProcessPhase,
        "ProcessStatus": ProcessStatus,
        "ProcessAppendTarget": ProcessAppendTarget,
        "EffortLevel": EffortLevel,
        "PermissionMode": PermissionMode,
        "CollaborationModeName": CollaborationModeName,
        "GoalStatus": GoalStatus,
        "NoticeSeverity": NoticeSeverity,
        "NoticeCategory": NoticeCategory,
        "CodexThreadStatus": CodexThreadStatus,
        "ServiceTier": SetServiceTier.model_fields["service_tier"].annotation,
    }
    for name, annotation in aliases.items():
        assert _alias_literals(name) == set(get_args(annotation)), name


def test_process_timeline_has_an_icon_for_every_process_kind():
    match = re.search(
        r"const\s+PROCESS_IC[^=]*=\s*\{(.*?)\};",
        _without_typescript_comments(TS_PROCESS_TIMELINE),
        re.DOTALL,
    )
    assert match
    icon_kinds = set(re.findall(
        r"^\s*([a-z_]+)\s*:", match.group(1), re.MULTILINE))
    assert icon_kinds == set(get_args(ProcessKind))


def test_server_event_union_and_reducer_cover_every_backend_event():
    mirrored = _wire_interfaces()
    match = re.search(
        r"export type\s+ServerEvent\s*=\s*(.*?);",
        TS_PROTOCOL,
        re.DOTALL,
    )
    assert match
    union_names = set(re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", match.group(1)))
    server_wire_types = {
        wire_type
        for wire_type, model in _TYPE_MAP.items()
        if not issubclass(model, _Command) and wire_type != "ping"
    }
    union_wire_types = {
        wire_type for wire_type, name in mirrored.items() if name in union_names
    }
    assert union_wire_types == server_wire_types

    reducer_cases = set(re.findall(r'case\s+"([a-z_]+)"\s*:', TS_REDUCER))
    assert server_wire_types - reducer_cases == set()


def test_shared_envelope_includes_relay_route_generation():
    assert _field_optional("Base", "route_id") is True


def test_protocol_version_and_web_build_manifest_match_backend():
    match = re.search(r"PROTOCOL_VERSION\s*=\s*(\d+)", TS_PROTOCOL)
    assert match, "TypeScript protocol version is missing"
    manifest = json.loads(
        (ROOT / "web/public/cc-remote-build.json").read_text()
    )
    assert int(match.group(1)) == PROTOCOL_VERSION
    assert manifest["protocol"] == PROTOCOL_VERSION
