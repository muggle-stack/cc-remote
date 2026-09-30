"""Strict protocol bounds for client-controlled scalars and attachments."""
from __future__ import annotations

import base64
import json
import struct

import pytest
from pydantic import ValidationError

from cc_remote.attachments import MAX_ATTACHMENT_COUNT, validate_attachments
from cc_remote.protocol import (
    ASK_ANSWER_MAX_CHARS,
    MAX_AUTO_COMPACT_TOKENS,
    MIN_AUTO_COMPACT_TOKENS,
    AnswerQuestion,
    AutoCompact,
    AuthorizePreview,
    CollaborationMode,
    ForkSessionWorktree,
    GetDiff,
    GetFilePreview,
    GetEngineCapabilities,
    GetModels,
    GetPreviewAsset,
    ManageEngineHook,
    ManageEngineSkill,
    FILE_PREVIEW_MAX_BYTES,
    NewSession,
    PROTOCOL_VERSION,
    PreviewAuthorizationRequired,
    PreviewAuthorizationResult,
    ProtocolError,
    Query,
    SaveMarkdown,
    SetEffort,
    SetAutoCompact,
    SetCollaborationMode,
    SetModel,
    SetPerm,
    SetServiceTier,
    Steer,
    SwitchSession,
    deserialize,
    is_downstream,
    serialize,
)


def _png() -> str:
    header = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", 1, 1)
    )
    return base64.b64encode(header).decode()


@pytest.mark.parametrize(
    "attachment_field,attachment",
    [
        ("images", {"media_type": "image/png", "data": _png(), "padding": "x"}),
        ("files", {"filename": "note.txt", "data": "eA==", "padding": "x"}),
    ],
)
def test_nested_attachment_unknown_fields_are_rejected_without_reflection(
    attachment_field, attachment,
):
    sentinel = "UNTRUSTED_ATTACHMENT_PADDING"
    attachment["padding"] = sentinel
    raw = json.dumps({
        "v": PROTOCOL_VERSION,
        "type": "query",
        "prompt": "inspect",
        "msg_id": "msg-1",
        attachment_field: [attachment],
    })

    with pytest.raises(ProtocolError) as error:
        deserialize(raw)

    assert "extra_forbidden" in str(error.value)
    assert sentinel not in str(error.value)


def test_attachment_count_is_bounded_across_images_and_files():
    images = [{"media_type": "image/png", "data": _png()}]
    files = [
        {"filename": f"{index}.txt", "data": "eA=="}
        for index in range(MAX_ATTACHMENT_COUNT)
    ]
    with pytest.raises(ValidationError, match="attachments exceed"):
        Query(prompt="inspect", msg_id="msg-1", images=images, files=files)
    with pytest.raises(ValidationError, match="attachments exceed"):
        NewSession(
            prompt="inspect", msg_id="msg-1", images=images, files=files)
    with pytest.raises(ValidationError, match="attachments exceed"):
        Steer(
            sid="session-1",
            cmd_id="cmd-1",
            client_id="client-1",
            prompt="inspect",
            msg_id="msg-1",
            images=images,
            files=files,
        )


def test_steer_requires_an_explicit_bounded_target_and_prompt():
    with pytest.raises(ValidationError, match="sid"):
        Steer(
            cmd_id="cmd-1", client_id="client-1",
            prompt="inspect", msg_id="msg-1")
    with pytest.raises(ValidationError, match="cmd_id"):
        Steer(
            sid="session-1", client_id="client-1",
            prompt="inspect", msg_id="msg-1")
    with pytest.raises(ValidationError, match="client_id"):
        Steer(
            sid="session-1", cmd_id="cmd-1",
            prompt="inspect", msg_id="msg-1")
    with pytest.raises(ValidationError):
        Steer(
            sid="bad target", cmd_id="cmd-1", client_id="client-1",
            prompt="inspect", msg_id="msg-1")
    with pytest.raises(ValidationError):
        Steer(
            sid="session-1",
            cmd_id="cmd-1",
            client_id="client-1",
            prompt="x" * (2 * 1024 * 1024 + 1),
            msg_id="msg-1",
        )

    raw = json.dumps({
        "v": PROTOCOL_VERSION,
        "type": "steer",
        "prompt": "inspect",
        "msg_id": "msg-1",
        "cmd_id": "cmd-1",
        "client_id": "client-1",
    })
    with pytest.raises(ProtocolError, match="sid:missing"):
        deserialize(raw)


def test_surrogate_filename_is_a_clean_validation_error():
    invalid = {"filename": "\ud800", "data": "eA=="}
    assert "filename" in validate_attachments([], [invalid])

    raw = json.dumps({
        "v": PROTOCOL_VERSION,
        "type": "query",
        "prompt": "inspect",
        "msg_id": "msg-1",
        "files": [invalid],
    })
    with pytest.raises(ProtocolError, match="files.0.filename"):
        deserialize(raw)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: SetModel(model="m" * 257),
        lambda: SetEffort(effort="bogus"),
        lambda: SetServiceTier(service_tier="x" * 65),
        lambda: SetServiceTier(service_tier="invalid tier"),
        lambda: SetCollaborationMode(mode="bogus"),
        lambda: SetPerm(mode="bogus"),
        lambda: SwitchSession(session_id="sid-1", engine="bogus"),
        lambda: GetModels(engine="bogus"),
        lambda: GetDiff(file="x", theme="bogus"),
        lambda: GetFilePreview(path="x" * 4097, request_id="preview-1"),
        lambda: GetPreviewAsset(
            path="image.png", preview_id="preview-1", request_id="x" * 129),
        lambda: AuthorizePreview(
            authorization_id="x" * 129,
            request_id="preview-1",
            decision="allow",
        ),
        lambda: AuthorizePreview(
            authorization_id="authorization-1",
            request_id="preview-1",
            decision="always",
        ),
        lambda: AnswerQuestion(
            ask_id="ask-1", answer="x" * (ASK_ANSWER_MAX_CHARS + 1)),
        lambda: ForkSessionWorktree(
            session_id="sid-1", request_id="request-1", name="x" * 81),
        lambda: ManageEngineSkill(
            engine="claude", action="create", name="x" * 129),
        lambda: ManageEngineHook(
            engine="claude", action="create", event="PreToolUse",
            command="x" * (16 * 1024 + 1)),
    ],
)
def test_client_command_scalars_are_bounded_or_enumerated(factory):
    with pytest.raises(ValidationError):
        factory()


def test_known_dynamic_control_values_remain_supported():
    assert SetEffort(effort="ultra").effort == "ultra"
    assert SetServiceTier(service_tier="toggle").service_tier == "toggle"
    assert SetCollaborationMode(mode="plan").mode == "plan"
    assert SetCollaborationMode(mode="default").mode == "default"
    assert SetPerm(mode="on-request").mode == "on-request"
    assert GetModels(engine="cc", cwd="/tmp/project").cwd == "/tmp/project"
    assert GetEngineCapabilities(
        engine="codex", skills_only=True
    ).skills_only is True
    assert ForkSessionWorktree(
        session_id="sid-1", request_id="request-1", name="feature",
    ).name == "feature"
    required = PreviewAuthorizationRequired(
        authorization_id="authorization-1",
        request_id="preview-1",
        operation="file_preview",
        path="/tmp/file.md",
        resolved_path="/private/tmp/file.md",
        format="markdown",
    )
    assert deserialize(serialize(required)) == required
    result = PreviewAuthorizationResult(
        authorization_id="authorization-1",
        request_id="preview-1",
        operation="file_preview",
        path="/tmp/file.md",
        status="granted",
    )
    assert deserialize(serialize(result)) == result
    assert is_downstream(required) is False
    assert is_downstream(result) is False


def test_claude_autocompact_modes_and_thresholds_are_strictly_bounded():
    assert SetAutoCompact(mode="inherit").threshold_tokens is None
    assert SetAutoCompact(mode="auto").threshold_tokens is None
    assert SetAutoCompact(
        mode="custom", threshold_tokens=MIN_AUTO_COMPACT_TOKENS,
    ).threshold_tokens == MIN_AUTO_COMPACT_TOKENS
    assert NewSession(
        engine="claude",
        auto_compact_mode="custom",
        auto_compact_threshold_tokens=MAX_AUTO_COMPACT_TOKENS,
    ).auto_compact_threshold_tokens == MAX_AUTO_COMPACT_TOKENS

    for factory in (
        lambda: SetAutoCompact(mode="custom"),
        lambda: SetAutoCompact(
            mode="custom", threshold_tokens=MIN_AUTO_COMPACT_TOKENS - 1),
        lambda: SetAutoCompact(
            mode="custom", threshold_tokens=MAX_AUTO_COMPACT_TOKENS + 1),
        lambda: SetAutoCompact(mode="auto", threshold_tokens=200_000),
        lambda: NewSession(
            engine="claude", auto_compact_mode="custom"),
        lambda: NewSession(
            engine="codex", auto_compact_mode="auto"),
    ):
        with pytest.raises(ValidationError):
            factory()

    state = AutoCompact(
        mode="custom",
        threshold_tokens=200_000,
        applied_mode="inherit",
        pending=True,
    )
    assert deserialize(serialize(state)) == state
    assert is_downstream(state) is True


def test_markdown_save_content_is_bounded_by_utf8_bytes():
    common = {
        "path": "README.md",
        "request_id": "save-1",
        "expected_size": 0,
        "expected_mtime_ns": "0",
        "expected_revision": "0" * 64,
    }
    assert SaveMarkdown(content="界" * (FILE_PREVIEW_MAX_BYTES // 3), **common)
    with pytest.raises(ValidationError, match="UTF-8 bytes"):
        SaveMarkdown(content="界" * (FILE_PREVIEW_MAX_BYTES // 3 + 1), **common)


def test_collaboration_mode_state_roundtrips_as_downstream():
    state = CollaborationMode(mode="plan")
    assert deserialize(serialize(state)) == state
    assert is_downstream(state) is True
