"""Zero-token tests for Codex control-plane RPCs and thread metadata."""
from __future__ import annotations

import asyncio
import json

import pytest

from cc_remote.wrapper import codex_rpc as codex_rpc_module
from cc_remote.wrapper import codex_sessions as codex_sessions_module


class _FakeStdin:
    def __init__(self):
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeStdout:
    def __init__(self, messages):
        self.lines = [(json.dumps(message) + "\n").encode() for message in messages]

    async def readline(self) -> bytes:
        return self.lines.pop(0) if self.lines else b""


class _FakeProcess:
    def __init__(self, messages):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(messages)
        self.returncode = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.returncode = 0
        return 0


def test_codex_rpc_initializes_sends_exact_shape_and_reaps(monkeypatch, tmp_path):
    async def run():
        process = _FakeProcess([
            {"jsonrpc": "2.0", "id": 1, "result": {"userAgent": "test"}},
            {"jsonrpc": "2.0", "method": "thread/name/updated", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}},
        ])
        spawned = {}

        async def create_subprocess_exec(*args, **kwargs):
            spawned["args"] = args
            spawned["kwargs"] = kwargs
            return process

        monkeypatch.setattr(codex_rpc_module, "_resolve_codex_bin", lambda: "/bin/codex")
        monkeypatch.setattr(codex_rpc_module, "_codex_env", lambda path: {"BIN": path})
        monkeypatch.setattr(
            codex_rpc_module.asyncio, "create_subprocess_exec", create_subprocess_exec)

        result = await codex_rpc_module.codex_rpc(
            "thread/name/set", {"threadId": "thread-1", "name": "renamed"},
            cwd=str(tmp_path),
        )

        assert result == {"ok": True}
        assert spawned["args"] == ("/bin/codex", "app-server", "--stdio")
        assert spawned["kwargs"]["cwd"] == str(tmp_path.resolve())
        assert spawned["kwargs"]["env"] == {"BIN": "/bin/codex"}
        assert spawned["kwargs"]["limit"] == codex_rpc_module._STREAM_LIMIT
        messages = [json.loads(line) for line in process.stdin.writes]
        assert messages == [
            {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"clientInfo": {"name": "cc-remote", "version": "0.1.0"}},
            },
            {"jsonrpc": "2.0", "method": "initialized"},
            {
                "jsonrpc": "2.0", "id": 2, "method": "thread/name/set",
                "params": {"threadId": "thread-1", "name": "renamed"},
            },
        ]
        assert process.stdin.closed is True
        assert process.terminated is True and process.killed is False

    asyncio.run(run())


def test_codex_rpc_distinguishes_rejection_from_unknown_post_submit_outcome(
    monkeypatch, tmp_path,
):
    async def invoke(messages):
        process = _FakeProcess(messages)

        async def create_subprocess_exec(*_args, **_kwargs):
            return process

        monkeypatch.setattr(codex_rpc_module, "_resolve_codex_bin", lambda: "/bin/codex")
        monkeypatch.setattr(codex_rpc_module, "_codex_env", lambda _path: {})
        monkeypatch.setattr(
            codex_rpc_module.asyncio, "create_subprocess_exec", create_subprocess_exec)
        return await codex_rpc_module.codex_rpc(
            "thread/fork", {"threadId": "parent"}, cwd=str(tmp_path))

    async def run():
        with pytest.raises(codex_rpc_module.CodexRpcRejected):
            await invoke([
                {"jsonrpc": "2.0", "id": 1, "result": {}},
                {"jsonrpc": "2.0", "id": 2,
                 "error": {"code": -32602, "message": "invalid params"}},
            ])

        with pytest.raises(codex_rpc_module.CodexRpcOutcomeUnknown):
            await invoke([
                {"jsonrpc": "2.0", "id": 1, "result": {}},
                # EOF after request id=2 was written: commit status is unknown.
            ])

    asyncio.run(run())


def test_codex_thread_list_paginates_both_archive_states_and_normalizes(monkeypatch):
    async def run():
        calls = []
        pages = {
            (False, None): {
                "data": [
                    {
                        "id": "thread-1", "name": "renamed", "preview": "first",
                        "cwd": "/repo", "updatedAt": 20,
                        "gitInfo": {"branch": "feature/one"},
                        "forkedFromId": "parent-1", "status": {"type": "active"},
                    },
                    {
                        "id": "thread-2", "name": None, "preview": "second",
                        "cwd": "/repo", "updatedAt": 10,
                        "gitInfo": None, "forkedFromId": None,
                        "status": {"type": "notLoaded"},
                    },
                ],
                "nextCursor": "active-next",
            },
            (False, "active-next"): {
                "data": [{
                    "id": "thread-3", "name": "third", "preview": "third prompt",
                    "cwd": "/repo-3", "updatedAt": 30,
                    "gitInfo": {"branch": None}, "forkedFromId": None,
                    "status": {"type": "idle"},
                }],
                "nextCursor": None,
            },
            (True, None): {
                "data": [{
                    "id": "thread-4", "name": "archived", "preview": "old",
                    "cwd": "/old", "updatedAt": 40,
                    "gitInfo": {"branch": "old"}, "forkedFromId": None,
                    "status": {"type": "notLoaded"},
                }],
                "nextCursor": "archive-next",
            },
            (True, "archive-next"): {
                "data": [{
                    "id": "thread-5", "name": "new archive", "preview": "new",
                    "cwd": "/new", "updatedAt": 50,
                    "gitInfo": {"branch": "new"}, "forkedFromId": "thread-1",
                    "status": {"type": "systemError"},
                }],
                "nextCursor": None,
            },
        }

        async def rpc(method, params, cwd=None):
            calls.append((method, params.copy(), cwd))
            return pages[(params["archived"], params.get("cursor"))]

        monkeypatch.setattr(codex_sessions_module, "codex_rpc", rpc)
        monkeypatch.setattr(
            codex_sessions_module, "codex_current_provider", lambda: "openai")

        rows = await codex_sessions_module.list_codex_sessions(limit=3)

        assert [row["session_id"] for row in rows] == [
            "thread-5", "thread-4", "thread-3", "thread-1", "thread-2",
        ]
        first = next(row for row in rows if row["session_id"] == "thread-1")
        assert first == {
            "session_id": "thread-1",
            "summary": "renamed",
            "first_prompt": "first",
            "cwd": "/repo",
            "last_modified": "20",
            "git_branch": "feature/one",
            "forked_from_id": "parent-1",
            "status": "active",
            "tag": None,
        }
        assert all(
            row["tag"] == "archived" for row in rows
            if row["session_id"] in {"thread-4", "thread-5"}
        )
        assert [(params["archived"], params.get("cursor"), params["limit"])
                for _, params, _ in calls] == [
            (False, None, 3),
            (False, "active-next", 1),
            (True, None, 3),
            (True, "archive-next", 2),
        ]
        assert all(method == "thread/list" and cwd is None
                   for method, _, cwd in calls)
        assert all(params["modelProviders"] == ["openai"]
                   for _, params, _ in calls)
        assert all(params["sortKey"] == "updated_at"
                   and params["sortDirection"] == "desc"
                   for _, params, _ in calls)

    asyncio.run(run())
