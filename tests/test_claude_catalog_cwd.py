"""Large leading image rows must not make valid native sessions look orphaned."""

import asyncio
import base64
import json

import pytest
from claude_agent_sdk import get_session_info
from claude_agent_sdk._internal.sessions import _sanitize_path
from claude_agent_sdk.types import SDKSessionInfo

from cc_remote.attachments import (
    MAX_ATTACHMENT_COUNT,
    MAX_SINGLE_ATTACHMENT_BYTES,
    MAX_TOTAL_ATTACHMENT_BYTES,
    validate_attachments,
)
from cc_remote.protocol import SwitchSession
from cc_remote.wrapper import claude_catalog
from tests.test_attachments import _complete_png
from tests.test_multisession import _mk_machine


SID = "11111111-1111-4111-8111-111111111111"


def write_image_transcript(root, cwd="/original-project", *, image_sizes=(100_000,)):
    path = root / "projects" / _sanitize_path(cwd) / f"{SID}.jsonl"
    path.parent.mkdir(parents=True)
    metadata = b"Comment\x00"
    overhead = len(base64.b64decode(_complete_png((b"tEXt", metadata))))
    images = [{
        "media_type": "image/png",
        "data": _complete_png((b"tEXt", metadata + b"A" * (size - overhead))),
    } for size in image_sizes]
    assert [len(base64.b64decode(image["data"])) for image in images] == list(image_sizes)
    assert validate_attachments(images, None) is None
    content = [{"type": "image", "source": {"type": "base64", **image}} for image in images]
    records = [
        {"type": "queue-operation", "operation": "enqueue", "content": content},
        {"type": "user", "uuid": "human", "parentUuid": None, "sessionId": SID,
         "cwd": cwd, "message": {"role": "user", "content": [*content, {"type": "text", "text": "inspect image"}]}},
        {"type": "assistant", "uuid": "answer", "parentUuid": "human", "sessionId": SID,
         "cwd": cwd + "/later-shell-directory", "message": {"role": "assistant", "content": "done"}},
        {"type": "custom-title", "sessionId": SID, "customTitle": "Image review"},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    return path


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("image_sizes", [
    (100_000,),
    (3 * 1024 * 1024,),
    (MAX_SINGLE_ATTACHMENT_BYTES,),
    (MAX_TOTAL_ATTACHMENT_BYTES // 2,) * 2,
    (MAX_TOTAL_ATTACHMENT_BYTES // MAX_ATTACHMENT_COUNT,) * MAX_ATTACHMENT_COUNT,
], ids=["sdk-window", "base64-record-boundary", "single-limit", "total-limit", "count-and-total-limit"])
def test_large_image_session_recovers_original_cwd_and_can_be_selected(tmp_path, monkeypatch, explicit, image_sizes):
    root = tmp_path / "selected-profile"
    path = write_image_transcript(root, image_sizes=image_sizes)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    raw = get_session_info(SID)
    assert raw is not None and raw.cwd is None  # The pinned SDK reproducer.
    original = path.read_bytes()

    async def run():
        machine, transport = _mk_machine()
        profile = machine._claude_profiles.default
        if explicit:
            from cc_remote.claude_profiles import ClaudeProfileRegistry

            machine._claude_profiles = ClaudeProfileRegistry.from_json(json.dumps({
                "selected": {"label": "Selected", "config_dir": str(root), "default": True},
                "other": {"label": "Other", "config_dir": str(tmp_path / "other-profile")},
            }))
            profile = machine._claude_profiles.default
        info = machine._claude_catalog_session_info(profile, SID)
        assert info is not None and info.cwd == "/original-project"
        listed = machine._claude_catalog_list_sessions(profile, limit=10)
        assert [item.cwd for item in listed if item.session_id == SID] == ["/original-project"]

        class SpawnReached(Exception):
            pass

        async def spawn(**kwargs):
            assert kwargs["resume_id"] == SID
            raise SpawnReached  # Stop before any native client is created.

        machine._spawn = spawn
        wire_id = machine._claude_profiles.wire_session_id(profile.id, SID)
        with pytest.raises(SpawnReached):
            await machine._handle_switch_session(SwitchSession(session_id=wire_id, engine="claude", space="code"))
        assert not transport.sent

    asyncio.run(run())
    assert path.read_bytes() == original


def test_cwd_recovery_skips_oversized_rows_without_unbounded_allocation(tmp_path):
    root = tmp_path / "profile"
    path = write_image_transcript(root)
    original = path.read_bytes()
    path.write_bytes((json.dumps({"type": "queue-operation", "content": "A" * (claude_catalog._CWD_RECORD_BYTES + 10)}) + "\n").encode() + original)
    info = claude_catalog.get_session_info(root, SID)
    assert info is not None and info.cwd == "/original-project"


@pytest.mark.parametrize("invalid", [
    {"type": "queue-operation"},
    {"isSidechain": True},
    {"sessionId": "22222222-2222-4222-8222-222222222222"},
    {"cwd": "/a-different-project"},
    {"cwd": "relative"},
    {"cwd": None, "message": {"role": "user", "content": '{"cwd":"/original-project"}'}},
])
def test_cwd_recovery_does_not_infer_from_prompt_foreign_session_or_later_directory(tmp_path, invalid):
    path = tmp_path / _sanitize_path("/original-project") / f"{SID}.jsonl"
    path.parent.mkdir()
    record = {"type": "user", "uuid": "human", "sessionId": SID, "cwd": "/original-project",
              "message": {"role": "user", "content": "fixture"}, **invalid}
    path.write_text(json.dumps(record) + "\n")
    info = SDKSessionInfo(session_id=SID, summary="fixture", last_modified=0)
    assert claude_catalog.recover_session_cwd(info, path).cwd is None


def test_cwd_recovery_stops_at_scan_budget_and_keeps_existing_native_metadata(tmp_path, monkeypatch):
    path = write_image_transcript(tmp_path / "profile")
    info = SDKSessionInfo(session_id=SID, summary="fixture", last_modified=0)
    monkeypatch.setattr(claude_catalog, "_CWD_SCAN_BYTES", 64 * 1024)
    assert claude_catalog.recover_session_cwd(info, path) is info
    info.cwd = "/already-known"
    assert claude_catalog.recover_session_cwd(info, path) is info
