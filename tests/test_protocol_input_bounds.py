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
    AnswerQuestion,
    CollaborationMode,
    ForkSessionWorktree,
    GetDiff,
    GetModels,
    NewSession,
    PROTOCOL_VERSION,
    ProtocolError,
    Query,
    SetEffort,
    SetCollaborationMode,
    SetModel,
    SetPerm,
    SetServiceTier,
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
        lambda: SetServiceTier(service_tier="bogus"),
        lambda: SetCollaborationMode(mode="bogus"),
        lambda: SetPerm(mode="bogus"),
        lambda: SwitchSession(session_id="sid-1", engine="bogus"),
        lambda: GetModels(engine="bogus"),
        lambda: GetDiff(file="x", theme="bogus"),
        lambda: AnswerQuestion(
            ask_id="ask-1", answer="x" * (ASK_ANSWER_MAX_CHARS + 1)),
        lambda: ForkSessionWorktree(
            session_id="sid-1", request_id="request-1", name="x" * 81),
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
    assert ForkSessionWorktree(
        session_id="sid-1", request_id="request-1", name="feature",
    ).name == "feature"


def test_collaboration_mode_state_roundtrips_as_downstream():
    state = CollaborationMode(mode="plan")
    assert deserialize(serialize(state)) == state
    assert is_downstream(state) is True
