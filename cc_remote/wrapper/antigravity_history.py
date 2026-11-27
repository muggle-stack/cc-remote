"""Recover original message times missing from Antigravity ACP history replay."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import stat

from cc_remote.session_messages import parse_message
from cc_remote.wrapper.antigravity_stream import AntigravityTranscript, wire_session_id

_MAX_TRANSCRIPT_BYTES = 16 * 1024 * 1024


@dataclass
class NativeTurnTime:
    prompt: bytes
    sent_at: float | None
    answer: bytes | None = None
    answered_at: float | None = None


def _fingerprint(value: str) -> bytes:
    return sha256(value.strip().encode("utf-8", errors="replace")).digest()


def _prompt_text(value: str) -> str:
    prefix = "<USER_REQUEST>\n"
    if value.startswith(prefix):
        body, end, _ = value[len(prefix):].rpartition("\n</USER_REQUEST>")
        if end:
            value = body
    message = parse_message(value)
    return message.prompt if message else value


def _time(value) -> float | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        date = datetime.fromisoformat(value)
        if date.tzinfo is not None and (stamp := date.timestamp()) > 0:
            return stamp
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _read_transcript_tail(home: Path, native_id: str) -> bytes:
    if wire_session_id(native_id) is None:
        return b""
    try:
        root = (home / ".gemini/antigravity-cli/brain").resolve(strict=True)
        path = root / native_id / ".system_generated/logs/transcript.jsonl"
        if path.resolve(strict=True) != path:
            return b""
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return b""
            offset = max(0, info.st_size - _MAX_TRANSCRIPT_BYTES)
            stream.seek(offset)
            body = stream.read(_MAX_TRANSCRIPT_BYTES)
            return body.partition(b"\n")[2] if offset else body
    except (OSError, ValueError, RuntimeError):
        return b""


def read_native_turn_times(home: Path, native_id: str) -> list[NativeTurnTime]:
    turns: list[NativeTurnTime] = []
    for line in _read_transcript_tail(home, native_id).splitlines():
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(row, dict) or not isinstance(content := row.get("content"), str):
            continue
        if row.get("type") == "USER_INPUT":
            turns.append(NativeTurnTime(_fingerprint(_prompt_text(content)), _time(row.get("created_at"))))
        elif row.get("type") == "PLANNER_RESPONSE" and turns and content.strip():
            turns[-1].answer = _fingerprint(content)
            turns[-1].answered_at = _time(row.get("created_at"))
    return turns


def _replay_key(group: list[dict]) -> tuple[bytes, bytes | None]:
    prompt = _fingerprint(group[0]["prompt"])
    visible = [entry for entry in group if entry["type"] == "delta" and entry.get("channel") != "thinking"]
    if not visible:
        return prompt, None
    final_id = visible[-1]["message_id"]
    answer = "".join(entry["text"] for entry in visible if entry["message_id"] == final_id)
    return prompt, _fingerprint(answer)


def apply_native_turn_times(transcript: AntigravityTranscript, native: list[NativeTurnTime]) -> None:
    groups = [group for group in transcript.groups.values() if group and group[0]["type"] == "user_msg"]
    if not groups or not native:
        return
    keys = [_replay_key(group) for group in groups]
    tail = native[-len(groups):]
    if len(native) == transcript.replay_turns and [turn.prompt for turn in tail] == [key[0] for key in keys]:
        # Matching prompt order also resolves repeated identical turns.
        matches = tail
    else:
        # If the native log and replay have different boundaries, only match
        # a prompt/answer pair that occurs once in each source.
        by_key: dict[tuple[bytes, bytes | None], list[NativeTurnTime]] = defaultdict(list)
        for turn in native:
            by_key[turn.prompt, turn.answer].append(turn)
        counts = Counter(keys)
        matches = [by_key[key][0] if counts[key] == len(by_key[key]) == 1 else None for key in keys]
    for group, key, match in zip(groups, keys, matches, strict=True):
        if match is None:
            continue
        if match.sent_at is not None:
            group[0].setdefault("ts", match.sent_at)
        if (key[1] is not None and key[1] == match.answer and match.answered_at is not None
                and (match.sent_at is None or match.answered_at >= match.sent_at)):
            for entry in group:
                if entry["type"] == "turn_end":
                    entry.setdefault("ts", match.answered_at)
