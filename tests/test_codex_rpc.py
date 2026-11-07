"""Zero-token tests for Codex control-plane RPCs and thread metadata."""
from __future__ import annotations

import asyncio
import json

import pytest

from cc_remote import __version__
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


class _BlockingFakeStdout(_FakeStdout):
    async def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _BackpressuredFakeStdin(_FakeStdin):
    async def drain(self) -> None:
        if len(self.writes) >= 3:
            await asyncio.Event().wait()


class _OversizedResponseStdout(_FakeStdout):
    async def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        raise ValueError("Separator is found, but chunk is longer than limit")


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
                "params": {
                    "clientInfo": {"name": "cc-remote", "version": __version__},
                    "capabilities": {"experimentalApi": True},
                },
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


def test_ephemeral_codex_thread_is_not_a_resumable_catalog_row():
    assert codex_sessions_module.codex_thread_catalog_row({
        "id": "private-btw",
        "ephemeral": True,
        "cwd": "/repo",
        "updatedAt": 20,
        "status": {"type": "idle"},
    }) is None


def test_ephemeral_codex_pages_do_not_consume_the_public_catalog_limit(
    monkeypatch,
):
    async def run():
        calls = []
        pages = {
            (False, None): {
                "data": [
                    {
                        "id": "private-btw-1", "ephemeral": True,
                        "updatedAt": 40,
                    },
                    {
                        "id": "private-btw-2", "ephemeral": True,
                        "updatedAt": 30,
                    },
                ],
                "nextCursor": "public-page",
            },
            (False, "public-page"): {
                "data": [
                    {
                        "id": "ordinary-1", "ephemeral": False,
                        "updatedAt": 20,
                    },
                    {
                        "id": "ordinary-2", "ephemeral": False,
                        "updatedAt": 10,
                    },
                ],
                "nextCursor": None,
            },
            (True, None): {"data": [], "nextCursor": None},
        }

        async def rpc(method, params, **_kwargs):
            calls.append((method, params.copy()))
            return pages[(params["archived"], params.get("cursor"))]

        monkeypatch.setattr(codex_sessions_module, "codex_rpc", rpc)
        monkeypatch.setattr(
            codex_sessions_module, "codex_current_provider", lambda: "")

        rows = await codex_sessions_module.list_codex_sessions(limit=2)

        assert [row["session_id"] for row in rows] == [
            "ordinary-1", "ordinary-2",
        ]
        assert [
            (params["archived"], params.get("cursor"), params["limit"])
            for _method, params in calls
        ] == [
            (False, None, 2),
            (False, "public-page", 2),
            (True, None, 2),
        ]

    asyncio.run(run())


def test_codex_rpc_can_raise_only_the_isolated_child_nofile_limit(
    monkeypatch,
    tmp_path,
):
    async def run():
        process = _FakeProcess([
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "id": 2, "result": {}},
        ])
        spawned = {}

        async def create_subprocess_exec(*args, **kwargs):
            spawned["args"] = args
            spawned["kwargs"] = kwargs
            return process

        monkeypatch.setattr(
            codex_rpc_module,
            "_resolve_codex_bin",
            lambda: "/bin/codex",
        )
        monkeypatch.setattr(
            codex_rpc_module,
            "_codex_env",
            lambda _path: {},
        )
        monkeypatch.setattr(
            codex_rpc_module.asyncio,
            "create_subprocess_exec",
            create_subprocess_exec,
        )

        await codex_rpc_module.codex_rpc(
            "thread/archive",
            {"threadId": "root"},
            cwd=str(tmp_path),
            nofile_soft_limit=4096,
        )

        assert spawned["args"] == (
            codex_rpc_module.sys.executable,
            codex_rpc_module._RLIMIT_EXEC,
            "4096",
            "/bin/codex",
            "app-server",
            "--stdio",
        )
        assert spawned["kwargs"]["cwd"] == str(tmp_path.resolve())

    asyncio.run(run())


def test_codex_rpc_batch_uses_one_process_and_preserves_partial_results(
    monkeypatch, tmp_path,
):
    async def run():
        process = _FakeProcess([
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "method": "account/updated", "params": {}},
            {"jsonrpc": "2.0", "id": 4, "result": {"third": True}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "error": {"code": -32602, "message": "unsupported"},
            },
            {"jsonrpc": "2.0", "id": 3, "result": {"second": True}},
        ])
        spawn_count = 0

        async def create_subprocess_exec(*_args, **_kwargs):
            nonlocal spawn_count
            spawn_count += 1
            return process

        monkeypatch.setattr(
            codex_rpc_module, "_resolve_codex_bin", lambda: "/bin/codex"
        )
        monkeypatch.setattr(codex_rpc_module, "_codex_env", lambda _path: {})
        monkeypatch.setattr(
            codex_rpc_module.asyncio,
            "create_subprocess_exec",
            create_subprocess_exec,
        )

        results = await codex_rpc_module.codex_rpc_batch([
            ("skills/list", {"cwds": [str(tmp_path)]}),
            ("hooks/list", {"cwds": [str(tmp_path)]}),
            ("mcpServerStatus/list", None),
        ], cwd=str(tmp_path))

        assert spawn_count == 1
        assert isinstance(results[0], codex_rpc_module.CodexRpcRejected)
        assert results[1:] == [{"second": True}, {"third": True}]
        messages = [json.loads(line) for line in process.stdin.writes]
        assert [message.get("method") for message in messages] == [
            "initialize",
            "initialized",
            "skills/list",
            "hooks/list",
            "mcpServerStatus/list",
        ]
        assert [message.get("id") for message in messages[2:]] == [2, 3, 4]
        assert process.stdin.closed is True
        assert process.terminated is True and process.killed is False

    asyncio.run(run())


def test_codex_rpc_batch_timeout_preserves_completed_results(
    monkeypatch, tmp_path,
):
    async def run():
        process = _FakeProcess([])
        process.stdout = _BlockingFakeStdout([
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "id": 2, "result": {"skills": True}},
        ])

        async def create_subprocess_exec(*_args, **_kwargs):
            return process

        monkeypatch.setattr(
            codex_rpc_module, "_resolve_codex_bin", lambda: "/bin/codex"
        )
        monkeypatch.setattr(codex_rpc_module, "_codex_env", lambda _path: {})
        monkeypatch.setattr(
            codex_rpc_module.asyncio,
            "create_subprocess_exec",
            create_subprocess_exec,
        )

        results = await codex_rpc_module.codex_rpc_batch([
            ("skills/list", {"cwds": [str(tmp_path)]}),
            ("mcpServerStatus/list", None),
        ], cwd=str(tmp_path), timeout=0.1)

        assert results[0] == {"skills": True}
        assert isinstance(results[1], codex_rpc_module.CodexRpcOutcomeUnknown)
        assert process.stdin.closed is True
        assert process.terminated is True and process.killed is False

    asyncio.run(run())


def test_codex_rpc_batch_deadline_covers_request_backpressure(
    monkeypatch, tmp_path,
):
    async def run():
        process = _FakeProcess([
            {"jsonrpc": "2.0", "id": 1, "result": {}},
        ])
        process.stdin = _BackpressuredFakeStdin()

        async def create_subprocess_exec(*_args, **_kwargs):
            return process

        monkeypatch.setattr(
            codex_rpc_module, "_resolve_codex_bin", lambda: "/bin/codex"
        )
        monkeypatch.setattr(codex_rpc_module, "_codex_env", lambda _path: {})
        monkeypatch.setattr(
            codex_rpc_module.asyncio,
            "create_subprocess_exec",
            create_subprocess_exec,
        )

        results = await asyncio.wait_for(
            codex_rpc_module.codex_rpc_batch([
                ("skills/list", {"cwds": [str(tmp_path)]}),
            ], cwd=str(tmp_path), timeout=0.1),
            timeout=0.5,
        )

        assert isinstance(results[0], codex_rpc_module.CodexRpcOutcomeUnknown)
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
        with pytest.raises(codex_rpc_module.CodexRpcRejected) as rejected:
            await invoke([
                {"jsonrpc": "2.0", "id": 1, "result": {}},
                {"jsonrpc": "2.0", "id": 2,
                 "error": {"code": -32602, "message": "invalid params"}},
            ])
        assert rejected.value.code == -32602
        assert rejected.value.message == (
            "codex app-server error -32602: invalid params"
        )

        with pytest.raises(codex_rpc_module.CodexRpcOutcomeUnknown):
            await invoke([
                {"jsonrpc": "2.0", "id": 1, "result": {}},
                # EOF after request id=2 was written: commit status is unknown.
            ])

    asyncio.run(run())


def test_codex_rpc_preserves_oversized_response_boundary(monkeypatch, tmp_path):
    async def run():
        process = _FakeProcess([
            {"jsonrpc": "2.0", "id": 1, "result": {}},
        ])
        process.stdout = _OversizedResponseStdout([
            {"jsonrpc": "2.0", "id": 1, "result": {}},
        ])

        async def create_subprocess_exec(*_args, **_kwargs):
            return process

        monkeypatch.setattr(
            codex_rpc_module, "_resolve_codex_bin", lambda: "/bin/codex")
        monkeypatch.setattr(
            codex_rpc_module, "_codex_env", lambda _path: {})
        monkeypatch.setattr(
            codex_rpc_module.asyncio,
            "create_subprocess_exec",
            create_subprocess_exec,
        )

        with pytest.raises(
            codex_rpc_module.CodexRpcResponseTooLarge
        ) as oversized:
            await codex_rpc_module.codex_rpc(
                "thread/turns/list",
                {"threadId": "thread-1", "itemsView": "full"},
                cwd=str(tmp_path),
            )
        assert isinstance(
            oversized.value, codex_rpc_module.CodexRpcOutcomeUnknown)
        assert process.stdin.closed is True
        assert process.terminated is True

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


def _history_rpc_factory(monkeypatch, processes):
    spawned = []
    async def create(*args, **kwargs):
        process = processes[len(spawned)]
        spawned.append((process, kwargs))
        return process
    monkeypatch.setattr(codex_rpc_module, '_resolve_codex_bin', lambda: '/bin/codex')
    monkeypatch.setattr(codex_rpc_module, '_child_env', lambda _bin, home: {'PROFILE': home or ''})
    monkeypatch.setattr(codex_rpc_module.asyncio, 'create_subprocess_exec', create)
    return spawned


def _history_process(results):
    return _FakeProcess([{'id': 1, 'result': {}}] + [
        {'id': i + 2, 'result': result} for i, result in enumerate(results)])


def test_history_scope_reuses_reader_but_not_accounts_or_mutations(monkeypatch):
    async def run():
        processes = [_history_process([{'n': 1}, {'n': 2}, {'data': []}]),
                     _history_process([{'n': 3}]), _history_process([{'n': 4}])]
        spawned = _history_rpc_factory(monkeypatch, processes)
        async with codex_rpc_module.codex_history_rpc_scope():
            assert await codex_rpc_module.codex_rpc('thread/turns/list', {}, codex_home='/a') == {'n': 1}
            async with codex_rpc_module.codex_history_rpc_scope():
                assert await codex_rpc_module.codex_rpc('thread/items/list', {}, codex_home='/a') == {'n': 2}
                assert await codex_rpc_module.codex_rpc('thread/list', {}, codex_home='/a') == {'data': []}
            assert len(spawned) == 1 and not processes[0].terminated
            await codex_rpc_module.codex_rpc('thread/turns/list', {}, codex_home='/b')
            await codex_rpc_module.codex_rpc('thread/name/set', {}, codex_home='/a')
            assert processes[2].terminated and not processes[1].terminated
        assert all(p.terminated and p.stdin.closed for p in processes)
        assert [json.loads(row).get('id') for row in processes[0].stdin.writes] == [1, None, 2, 3, 4]
        assert [args['env']['PROFILE'] for _p, args in spawned] == ['/a', '/b', '/a']
    asyncio.run(run())


def test_history_scope_cannot_share_parent_reader_with_child_task(monkeypatch):
    async def run():
        processes = [_history_process([1, 3]), _history_process([2])]
        spawned = _history_rpc_factory(monkeypatch, processes)
        async with codex_rpc_module.codex_history_rpc_scope():
            assert await codex_rpc_module.codex_rpc('thread/turns/list', {}) == 1
            assert await asyncio.create_task(codex_rpc_module.codex_rpc('thread/items/list', {})) == 2
            assert processes[1].terminated
            assert await codex_rpc_module.codex_rpc('thread/turns/list', {}) == 3
        assert len(spawned) == 2 and all(p.terminated for p in processes)
    asyncio.run(run())


def test_history_scope_reaps_broken_pipe_before_explicit_fallback(monkeypatch):
    async def run():
        broken = _history_process([])
        broken.stdout = _OversizedResponseStdout([{'id': 1, 'result': {}}])
        fallback = _history_process([{'items': []}])
        _history_rpc_factory(monkeypatch, [broken, fallback])
        async with codex_rpc_module.codex_history_rpc_scope():
            with pytest.raises(codex_rpc_module.CodexRpcResponseTooLarge):
                await codex_rpc_module.codex_rpc('thread/turns/list', {})
            assert broken.terminated and broken.stdin.closed
            assert await codex_rpc_module.codex_rpc('thread/items/list', {}) == {'items': []}
        assert fallback.terminated
    asyncio.run(run())


def test_cancelled_history_page_reaps_its_reader(monkeypatch):
    async def run():
        process = _history_process([])
        process.stdout = _BlockingFakeStdout([{'id': 1, 'result': {}}])
        _history_rpc_factory(monkeypatch, [process])
        async def read():
            async with codex_rpc_module.codex_history_rpc_scope():
                await codex_rpc_module.codex_rpc('thread/turns/list', {})
        task = asyncio.create_task(read())
        while len(process.stdin.writes) < 3:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.terminated and process.stdin.closed
    asyncio.run(run())
