"""Work-only context accounting; Code keeps the raw engine contract."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from cc_remote.protocol import ContextReport, GetContext, serialize
from cc_remote.workspaces import WorkRegistry
from cc_remote.wrapper import work_context as work_context_module
from cc_remote.wrapper.session_ctx import SessionContext
from cc_remote.wrapper.ringbuffer import RingBuffer
from cc_remote.wrapper.work_context import (
    claude_recent_context_usage,
    initial_work_context_baseline,
    recover_claude_context_usage,
    recover_codex_context_usage,
    recover_work_context_baseline,
    work_context_metrics,
)
from tests.test_multisession import _mk_machine


def _codex_usage() -> dict:
    return {
        "used_tokens": 11_194,
        "context_window": 353_400,
        "raw": {
            "last": {
                "inputTokens": 11_181,
                "outputTokens": 13,
                "totalTokens": 11_194,
            },
        },
    }


def test_work_context_metrics_keep_raw_overhead_out_of_session_gauge():
    assert initial_work_context_baseline("codex", _codex_usage()) == 11_181
    session, fixed, percentage, baseline = work_context_metrics(
        "codex", _codex_usage(), None)
    assert (session, fixed, baseline) == (13, 11_181, 11_181)
    assert percentage == 13 / 353_400 * 100

    session, fixed, percentage, baseline = work_context_metrics(
        "claude",
        {"totalTokens": 25_572, "maxTokens": 1_000_000},
        25_500,
    )
    assert (session, fixed, baseline) == (72, 25_500, 25_500)
    assert percentage == 72 / 1_000_000 * 100


def test_context_breakdown_is_emitted_only_when_work_has_a_baseline():
    code = ContextReport(
        total_tokens=100, max_tokens=1_000, percentage=10,
        model="test", categories=[],
    )
    assert not ({"session_tokens", "fixed_tokens", "session_percentage"}
                & json.loads(serialize(code)).keys())
    assert "available" not in json.loads(serialize(code))

    unavailable = code.model_copy(update={"available": False})
    assert json.loads(serialize(unavailable))["available"] is False

    work = code.model_copy(update={
        "session_tokens": 10,
        "fixed_tokens": 90,
        "session_percentage": 1.0,
    })
    payload = json.loads(serialize(work))
    assert payload["session_tokens"] == 10
    assert payload["fixed_tokens"] == 90

    recent = code.model_copy(update={"source": "recent_turn"})
    assert json.loads(serialize(recent))["source"] == "recent_turn"
    native = ContextReport(total_tokens=284793, max_tokens=300000, percentage=94.931,
                           source="native_estimate", auto_compact_threshold_tokens=284211)
    payload = json.loads(serialize(native))
    assert payload["source"] == "native_estimate"
    assert payload["total_tokens"] == 284793
    assert payload["auto_compact_threshold_tokens"] == 284211
    assert GetContext().refresh is False
    assert json.loads(serialize(GetContext(refresh=True)))["refresh"] is True


def test_claude_recent_context_usage_counts_cache_partitions_and_output():
    assert claude_recent_context_usage({
        "input_tokens": 24_676,
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 896,
        "output_tokens": 19,
    }) == {"totalTokens": 25_691}
    assert claude_recent_context_usage({
        "input_tokens": 10,
        "output_tokens": 2,
    }) == {"totalTokens": 12}

    for invalid in (
        None,
        [],
        {},
        {"input_tokens": 0, "output_tokens": 0},
        {"input_tokens": True},
        {"input_tokens": -1},
        {"input_tokens": 1.5},
        {"input_tokens": 9_007_199_254_740_992},
        {"input_tokens": 9_007_199_254_740_990, "output_tokens": 2},
    ):
        assert claude_recent_context_usage(invalid) is None


def test_claude_context_usage_recovers_newest_main_chain_assistant(
    tmp_path: Path, monkeypatch,
):
    transcript = tmp_path / "claude.jsonl"
    oversized = b"x" * (work_context_module._CONTEXT_RECORD_MAX_BYTES + 1)
    rows = [
        b'{broken json}',
        json.dumps({
            "type": "assistant",
            "message": {
                "model": "claude-main",
                "usage": {"input_tokens": 100, "output_tokens": 5},
            },
        }).encode(),
        json.dumps({
            "type": "assistant",
            "isSidechain": True,
            "message": {
                "model": "claude-sidechain",
                "usage": {"input_tokens": 900, "output_tokens": 9},
            },
        }).encode(),
        json.dumps({
            "type": "assistant",
            "parentToolUseID": "agent-tool",
            "message": {
                "model": "claude-child",
                "usage": {"input_tokens": 800, "output_tokens": 8},
            },
        }).encode(),
        oversized,
    ]
    transcript.write_bytes(b"\n".join(rows) + b"\n")
    monkeypatch.setattr(
        work_context_module, "transcript_path",
        lambda session_id: str(transcript) if session_id == "session" else None,
    )

    assert recover_claude_context_usage("session") == {"totalTokens": 105}


def test_claude_context_usage_rejects_concurrent_truncation(
    tmp_path: Path, monkeypatch,
):
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {
            "model": "claude-main",
            "usage": {"input_tokens": 100, "output_tokens": 5},
        },
    }) + "\n", encoding="utf-8")
    real_open = open

    class TruncatingReader:
        def __init__(self, stream):
            self._stream = stream
            self._truncated = False

        def __enter__(self):
            self._stream.__enter__()
            return self

        def __exit__(self, *args):
            return self._stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._stream, name)

        def read(self, size=-1):
            data = self._stream.read(size)
            if not self._truncated:
                self._truncated = True
                with real_open(transcript, "wb"):
                    pass
            return data

    def truncating_open(path, mode="r", *args, **kwargs):
        stream = real_open(path, mode, *args, **kwargs)
        return TruncatingReader(stream) if mode == "rb" else stream

    monkeypatch.setattr(
        work_context_module, "open", truncating_open, raising=False)
    monkeypatch.setattr(
        work_context_module, "transcript_path", lambda _sid: str(transcript))

    assert recover_claude_context_usage("session") is None


def test_migrated_work_baseline_recovers_from_native_histories(
    tmp_path: Path, monkeypatch,
):
    claude = tmp_path / "claude.jsonl"
    claude.write_text(
        '{"type":"user","message":{"role":"user"}}\n'
        '{"type":"assistant","message":{"usage":{'
        '"input_tokens":24676,"cache_creation_input_tokens":0,'
        '"cache_read_input_tokens":896,"output_tokens":19}}}\n',
        encoding="utf-8",
    )
    codex = tmp_path / "codex.jsonl"
    codex.write_text(
        '{"type":"event_msg","payload":{"type":"token_count",'
        '"info":{"last_token_usage":{"input_tokens":16774,'
        '"output_tokens":271}}}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        work_context_module, "transcript_path", lambda _sid: str(claude))
    monkeypatch.setattr(
        work_context_module, "codex_rollout_path", lambda _sid: str(codex))

    assert recover_work_context_baseline("claude", "claude-session") == 25_572
    assert recover_work_context_baseline("codex", "codex-session") == 16_774


def test_claude_context_and_work_baseline_accept_profile_explicit_paths(
    tmp_path: Path, monkeypatch,
):
    transcript = tmp_path / "company.jsonl"
    transcript.write_text(
        '{"type":"assistant","message":{"usage":{'
        '"input_tokens":300,"cache_creation_input_tokens":20,'
        '"cache_read_input_tokens":4,"output_tokens":5}}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        work_context_module,
        "transcript_path",
        lambda _sid: (_ for _ in ()).throw(
            AssertionError("ambient Claude catalog must not be read")),
    )

    assert recover_claude_context_usage(
        "same-native-id", path=str(transcript),
    ) == {"totalTokens": 329}
    assert recover_work_context_baseline(
        "claude", "same-native-id", claude_path=str(transcript),
    ) == 324


def test_codex_work_baseline_uses_the_default_profile_home(
    tmp_path: Path, monkeypatch,
):
    codex = tmp_path / "codex.jsonl"
    codex.write_text(
        '{"type":"event_msg","payload":{"type":"token_count",'
        '"info":{"last_token_usage":{"input_tokens":321,'
        '"output_tokens":12}}}}\n',
        encoding="utf-8",
    )
    homes = []

    def rollout(session_id, *, codex_home=None):
        assert session_id == "codex-session"
        homes.append(codex_home)
        return str(codex)

    monkeypatch.setattr(work_context_module, "codex_rollout_path", rollout)

    assert recover_work_context_baseline(
        "codex",
        "codex-session",
        codex_home=str(tmp_path / "profile-home"),
    ) == 321
    assert homes == [str(tmp_path / "profile-home")]


def test_codex_context_usage_recovers_newest_bounded_profile_tail(
    tmp_path: Path, monkeypatch,
):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(
        b"x" * (4 * 1024 * 1024) + b"\n"
        b'{"type":"event_msg","payload":{"type":"token_count","info":{'
        b'"last_token_usage":{"total_tokens":103658,"input_tokens":103000},'
        b'"model_context_window":258400}}}\n'
        b'{"type":"event_msg","payload":{"type":"token_count","info":{'
        b'"last_token_usage":{"total_tokens":104321,"input_tokens":103500},'
        b'"model_context_window":258400}}}\n'
    )
    calls = []

    def path(session_id, *, codex_home=None):
        calls.append((session_id, codex_home))
        return str(rollout)

    monkeypatch.setattr(work_context_module, "codex_rollout_path", path)
    usage = recover_codex_context_usage(
        "native-session", codex_home=str(tmp_path / "profile"))
    assert usage == {
        "last": {"totalTokens": 104321, "inputTokens": 103500},
        "modelContextWindow": 258400,
    }
    assert calls == [("native-session", str(tmp_path / "profile"))]


def test_codex_context_usage_keeps_complete_record_at_tail_boundary(
    tmp_path: Path, monkeypatch,
):
    rollout = tmp_path / "rollout.jsonl"
    record = (
        b'{"type":"event_msg","payload":{"type":"token_count","info":{'
        b'"last_token_usage":{"total_tokens":777},'
        b'"model_context_window":1000}}}\n'
    )
    rollout.write_bytes(
        b"x" * (work_context_module._CONTEXT_TAIL_SCAN_BYTES - len(record) - 1)
        + b"\n" + record
    )
    monkeypatch.setattr(
        work_context_module, "codex_rollout_path",
        lambda *_args, **_kwargs: str(rollout))

    assert recover_codex_context_usage("native-session") == {
        "last": {"totalTokens": 777},
        "modelContextWindow": 1000,
    }


def test_codex_context_usage_recovery_fails_closed(tmp_path: Path, monkeypatch):
    rollout = tmp_path / "rollout.jsonl"
    monkeypatch.setattr(
        work_context_module, "codex_rollout_path", lambda *_args, **_kwargs: str(rollout))

    for record in (
        b'["valid json, but not a rollout object"]\n',
        b'{"type":"event_msg","payload":{"type":"token_count","info":{'
        b'"last_token_usage":{"total_tokens":true},'
        b'"model_context_window":258400}}}\n',
        b'{"type":"event_msg","payload":{"type":"token_count","info":{'
        b'"last_token_usage":{"total_tokens":123},'
        b'"model_context_window":-1}}}\n',
        b'{"type":"event_msg","payload":{"type":"token_count","info":{'
        b'"last_token_usage":{"total_tokens":123},'
        b'"model_context_window":9007199254740992}}}\n',
        b'{broken json}\n',
    ):
        rollout.write_bytes(record)
        assert recover_codex_context_usage("native-session") is None


def test_codex_context_usage_ignores_concurrent_append(
    tmp_path: Path, monkeypatch,
):
    rollout = tmp_path / "rollout.jsonl"
    original = (
        b'{"type":"event_msg","payload":{"type":"token_count","info":{'
        b'"last_token_usage":{"total_tokens":321},'
        b'"model_context_window":1000}}}\n'
    )
    appended = (
        b'{"type":"event_msg","payload":{"type":"token_count","info":{'
        b'"last_token_usage":{"total_tokens":654},'
        b'"model_context_window":1000}}}\n'
    )
    rollout.write_bytes(original)
    real_open = open

    class AppendingReader:
        def __init__(self, stream):
            self._stream = stream
            self._appended = False

        def __enter__(self):
            self._stream.__enter__()
            return self

        def __exit__(self, *args):
            return self._stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._stream, name)

        def read(self, size=-1):
            data = self._stream.read(size)
            if not self._appended:
                self._appended = True
                with real_open(rollout, "ab") as writer:
                    writer.write(appended)
            return data

    def growing_open(path, mode="r", *args, **kwargs):
        stream = real_open(path, mode, *args, **kwargs)
        return AppendingReader(stream) if mode == "rb" else stream

    monkeypatch.setattr(work_context_module, "open", growing_open, raising=False)
    monkeypatch.setattr(
        work_context_module, "codex_rollout_path",
        lambda *_args, **_kwargs: str(rollout))

    assert recover_codex_context_usage("native-session") == {
        "last": {"totalTokens": 321},
        "modelContextWindow": 1000,
    }


def test_codex_context_usage_rejects_concurrent_path_replacement(
    tmp_path: Path, monkeypatch,
):
    rollout = tmp_path / "rollout.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    original = (
        b'{"type":"event_msg","payload":{"type":"token_count","info":{'
        b'"last_token_usage":{"total_tokens":321},'
        b'"model_context_window":1000}}}\n'
    )
    replacement.write_bytes(
        b'{"type":"event_msg","payload":{"type":"token_count","info":{'
        b'"last_token_usage":{"total_tokens":654},'
        b'"model_context_window":1000}}}\n'
    )
    rollout.write_bytes(original)
    real_open = open

    class ReplacingReader:
        def __init__(self, stream):
            self._stream = stream
            self._replaced = False

        def __enter__(self):
            self._stream.__enter__()
            return self

        def __exit__(self, *args):
            return self._stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._stream, name)

        def read(self, size=-1):
            data = self._stream.read(size)
            if not self._replaced:
                self._replaced = True
                replacement.replace(rollout)
            return data

    def replacing_open(path, mode="r", *args, **kwargs):
        stream = real_open(path, mode, *args, **kwargs)
        return ReplacingReader(stream) if mode == "rb" else stream

    monkeypatch.setattr(work_context_module, "open", replacing_open, raising=False)
    monkeypatch.setattr(
        work_context_module, "codex_rollout_path",
        lambda *_args, **_kwargs: str(rollout))

    assert recover_codex_context_usage("native-session") is None


def test_work_registry_persists_context_baseline_once(tmp_path: Path):
    store = WorkRegistry(tmp_path / "work", "codex")
    record = store.create_session()
    assert record.context_baseline_tokens is None
    assert store.set_context_baseline(record.work_id, 11_181) == 11_181
    assert store.set_context_baseline(record.work_id, 99_999) == 11_181
    restored = store.get_by_work_id(record.work_id)
    assert restored is not None
    assert restored.context_baseline_tokens == 11_181


class _ContextSdk:
    def __init__(self, usage: dict):
        self.usage = usage
        self.model = "test-model"

    async def get_context_usage(self) -> dict:
        return self.usage


def test_machine_splits_context_only_for_work(tmp_path: Path):
    async def run():
        machine, transport = _mk_machine()
        machine._work = machine._work.__class__(
            tmp_path / "claude-work", tmp_path / "codex-work")
        store = machine._work.for_engine("codex")
        record = store.create_session()
        store.bind_session(record.work_id, "work-session")
        work = SessionContext(
            session_id="work-session",
            sdk=_ContextSdk(_codex_usage()),
            buffer=RingBuffer(100, 100_000),
            cwd=record.cwd,
            key="work-session",
            engine="codex",
            space="work",
            work_id=record.work_id,
            work_context_baseline_pending=True,
        )
        migrated_record = store.create_session()
        store.bind_session(migrated_record.work_id, "migrated-work-session")
        migrated = SessionContext(
            session_id="migrated-work-session",
            sdk=_ContextSdk(_codex_usage()),
            buffer=RingBuffer(100, 100_000),
            cwd=migrated_record.cwd,
            key="migrated-work-session",
            engine="codex",
            space="work",
            work_id=migrated_record.work_id,
        )
        code = SessionContext(
            session_id="code-session",
            sdk=_ContextSdk(_codex_usage()),
            buffer=RingBuffer(100, 100_000),
            cwd="/tmp",
            key="code-session",
            engine="codex",
            space="code",
        )
        unknown = SessionContext(
            session_id="unknown-code-session",
            sdk=_ContextSdk({
                "used_tokens": None,
                "context_window": 256_000,
                "raw": {},
            }),
            buffer=RingBuffer(100, 100_000),
            cwd="/tmp",
            key="unknown-code-session",
            engine="codex",
            space="code",
        )
        machine.sessions = {
            "work-session": work,
            "migrated-work-session": migrated,
            "code-session": code,
            "unknown-code-session": unknown,
        }

        await machine._handle_get_context(SimpleNamespace(sid="work-session"))
        work_report = transport.sent[-1]
        assert work_report.total_tokens == 11_194
        assert work_report.percentage == 11_194 / 353_400 * 100
        assert work_report.session_tokens == 13
        assert work_report.fixed_tokens == 11_181
        assert work_report.session_percentage == 13 / 353_400 * 100
        assert work.work_context_baseline_pending is False
        assert store.get_by_work_id(
            record.work_id).context_baseline_tokens == 11_181

        # A pre-upgrade session with no durable baseline must keep the honest
        # raw reading; it may not relabel all existing history as fixed cost.
        await machine._handle_get_context(SimpleNamespace(
            sid="migrated-work-session"))
        migrated_report = transport.sent[-1]
        assert migrated_report.total_tokens == 11_194
        assert migrated_report.session_tokens is None
        assert migrated_report.fixed_tokens is None
        assert store.get_by_work_id(
            migrated_record.work_id).context_baseline_tokens is None

        await machine._handle_get_context(SimpleNamespace(sid="code-session"))
        code_report = transport.sent[-1]
        assert code_report.total_tokens == 11_194
        assert code_report.percentage == 11_194 / 353_400 * 100
        assert code_report.session_tokens is None
        assert code_report.fixed_tokens is None
        assert code_report.session_percentage is None

        # A lightweight Codex resume has no tokenUsage until a model turn emits
        # it.  The wrapper must preserve that unknown state rather than forge
        # the 0 / context-window reading that used to appear as 0%.
        await machine._handle_get_context(SimpleNamespace(
            sid="unknown-code-session"))
        unknown_report = transport.sent[-1]
        assert unknown_report.available is False
        assert unknown_report.total_tokens == 0
        assert unknown_report.max_tokens == 256_000
        assert unknown_report.percentage == 0

    asyncio.run(run())
