"""Explicit, zero-provider acceptance against an installed, pinned native DSH.

Run with Python's dev environment and Node 24 on PATH:
  python -m tests.dsh_native --installation /private/isolated/npm-install

The installation must contain @deepseek-ai/dsh@0.1.5-rc.2. Every run gets a
fresh DSH_HOME, patch, working directory, port, and process. Production model
adapters are disabled; the only adapter is the offline scripted fixture.
No operator session is loaded and no live Wrapper/relay is started.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tempfile
from types import SimpleNamespace

from cc_remote.protocol import (
    ActDshGoal, ArchiveSession, ReadDsh, DownloadDsh, AnswerQuestion, BrowseFiles, ForkSession, GetContext, GetFilePreview, GetHistory,
    GetHistoryImage, Interrupt, NewSession, Query, SetDshControl, SetModel, Steer, SwitchSession,
)
from cc_remote.wrapper.dsh_client import DshClient, TESTED_DSH_VERSION, exchange_launch_url, native_session_id
from tests.test_attachments import _complete_png
from tests.test_multisession import _mk_machine

REPO = Path(__file__).resolve().parents[1]


async def eventually(predicate, label, timeout=15):
    async with asyncio.timeout(timeout):
        while not (result := predicate()):
            await asyncio.sleep(.02)
        return result


async def exercise(installation: Path, root: Path):
    package = installation / "node_modules/@deepseek-ai/dsh/package.json"
    assert json.loads(package.read_text())["version"] == TESTED_DSH_VERSION
    shutil.copyfile(REPO / "tests/fixtures/dsh/scripted-adapter.mjs", root / "scripted-adapter.mjs")
    patch = root / "patch.yml"
    patch.write_text("- id: session-query-sqlite\n  config:\n    path: ':memory:'\n    openAt: first-search\n- id: llm-deepseek\n  disabled: true\n- id: llm-pi-ai\n  disabled: true\n- insert:\n"
        f"    - id: cc-remote-history\n      name: {json.dumps(str(REPO / 'integrations/dsh/cc-remote.mjs'))}\n"
        f"    - id: offline-model\n      name: {json.dumps(str(root / 'scripted-adapter.mjs'))}\n")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    log_path = root / "server.log"
    node = shutil.which("node")
    assert node and subprocess.check_output([node, "--version"], text=True).startswith("v24.")
    shutil.copyfile(REPO / "tests/fixtures/dsh/migration.mjs", root / "migration.mjs")
    await asyncio.to_thread(subprocess.run, [node, str(root / "migration.mjs")],
                            cwd=root, check=True, timeout=30)
    def start_server():
        with log_path.open("wb") as log:
            os.chmod(log_path, 0o600)
            return subprocess.Popen([node, str(installation / "node_modules/.bin/dsh"),
                "--profile", "web", "--patch", str(patch), "--port", str(port), "--no-open"],
                cwd=root, env={"PATH": os.environ["PATH"], "HOME": str(Path.home()),
                    "DSH_HOME": str(root / "home"), "LANG": "en_US.UTF-8"},
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    process = start_server()
    machine, transport = _mk_machine()
    runtime = machine._dsh
    try:
        def launch_url():
            assert process.poll() is None, "isolated DSH exited during startup"
            return re.search(r"http://[^\s]+", log_path.read_text(errors="replace"))
        match = await eventually(launch_url, "native startup")
        runtime.client = DshClient(await exchange_launch_url(match.group()))
        async def native_state():
            response = await runtime.client._http.get('/api/cc-remote.test-state')
            response.raise_for_status()
            return response.json()
        # Native create also creates a resident Agent. Restart this isolated
        # server before testing cold discovery, so no resident scope can mask it.
        seeded = {preset: await runtime.client.rpc("session/create", {"request": {"cwd": str(root), "agentPreset": preset}})
                  for preset in ("standard", "ptc", "minimal")}
        await runtime.client.close()
        os.killpg(process.pid, signal.SIGTERM)
        await asyncio.to_thread(process.wait, timeout=5)
        process = start_server()
        match = await eventually(launch_url, "cold native startup")
        runtime.client = DshClient(await exchange_launch_url(match.group()))
        created = seeded["standard"]
        cold = "dsh@" + created["sessionId"]
        await machine._handle(GetHistory(session_id=cold, detail="summary", client_id="native-test"))
        assert cold not in machine.sessions
        assert not next(item for item in await runtime.client.list_sessions() if item["sessionId"] == created["sessionId"])["running"]
        print("PASS native cold history", flush=True)

        before_discovery = await native_state()
        for preset in ("standard", "ptc", "minimal"):
            item = seeded[preset]
            target = "dsh@" + item["sessionId"]
            result = await machine._handle(SwitchSession(session_id=target, engine="dsh", client_id="native-test"))
            assert result is None, result
            handle = machine.sessions[target].sdk
            names = {c.name for c in handle.state.commands}
            assert "permission" in names, (preset, names)
            assert ({"goal", "plan"} <= names) == (preset != "minimal"), (preset, names)
            assert handle.watch is None
        after_discovery = await native_state()
        assert after_discovery == before_discovery, ('command discovery activated an Agent or model', before_discovery, after_discovery)
        print("PASS native cold command discovery: standard/PTC/minimal; no Agent or model activation", flush=True)

        await machine._handle(NewSession(engine="dsh", cwd=str(root), dsh_agent_preset="standard",
            prompt="compatibility:simple", msg_id="native-1", client_id="native-test", request_id="create-1"))
        ctx = machine._focused_ctx()
        assert ctx and ctx.engine == "dsh"
        def ends():
            return [m for m in transport.sent if m.type == "turn_end" and m.sid == ctx.key and m.result.subtype != "steered"]
        await eventually(lambda: ends(), "native response")
        assert ends()[-1].result.subtype == "success"
        assert any(m.type == "delta" and m.replace and m.text == "DSH compatibility response." for m in transport.sent)
        await eventually(lambda: ctx.state == "idle", "native idle")
        await runtime.read_catalog()
        await machine._handle(SetModel(sid=ctx.key, model=runtime.default_model))
        await machine._handle(SetDshControl(sid=ctx.key, kind="effort", value="off"))
        await eventually(lambda: ctx.sdk.effort == "off", "model effort")
        await machine._handle(SetDshControl(sid=ctx.key, kind="permission", value="read-only"))
        await eventually(lambda: ctx.sdk.permission_mode == "read-only", "permission")
        print("PASS native prompt, model, effort and permission", flush=True)

        count = len(ends())
        image = {"media_type": "image/png", "data": _complete_png()}
        files = [{"filename": "reference.txt", "data": base64.b64encode(b"offline reference").decode()}]
        await machine._handle(Query(sid=ctx.key, msg_id="native-2", prompt="compatibility:attachments", images=[image], files=files))
        await eventually(lambda: len(ends()) > count, "image/file prompt")
        assert ends()[-1].result.subtype == "success"
        history = await machine._handle(GetHistory(session_id=ctx.key, detail="summary", client_id="native-test"))
        row = next(row for row in history.turns if row.clientMsgId == "native-2")
        assert row.imageRefs and row.files
        result = await machine._handle(GetHistoryImage(session_id=ctx.key, turn_id=row.id,
            image_id=row.imageRefs[0]["image_id"], variant="full", request_id="image-1", client_id="native-test"))
        assert result.type == "history_image" and result.data and not result.error
        child = await machine._handle(ForkSession(session_id=ctx.key, last_turn_id=row.forkPointId,
            request_id="fork-1", client_id="native-test"))
        assert child.type == "session_forked" and child.parent_session_id == ctx.key
        (root / "reference.md").write_text("# Offline preview\n")
        listing = await machine._handle(BrowseFiles(sid=ctx.key, path=str(root), request_id="files-1", client_id="native-test"))
        assert listing.type == "files_listed" and not listing.error
        preview = await machine._handle(GetFilePreview(sid=ctx.key, path=str(root / "reference.md"), request_id="file-1", client_id="native-test"))
        assert preview.type == "file_preview" and not preview.error
        report = await machine._handle(GetContext(sid=ctx.key))
        assert report.available and report.max_tokens == 64000
        print("PASS native images, uploads, historical image, fork, file browser and context", flush=True)

        count = len(ends())
        await machine._handle(Query(sid=ctx.key, msg_id="native-slow", prompt="compatibility:slow"))
        await eventually(lambda: any(m.type == "user_msg" and m.msg_id == "native-slow" for m in transport.sent), "slow admission")
        await machine._handle(Steer(sid=ctx.key, msg_id="native-steer", prompt="compatibility:steer", cmd_id="steer-1", client_id="native-test"))
        await eventually(lambda: any(m.type == "user_msg" and m.msg_id == "native-steer" for m in transport.sent), "steer admission")
        await eventually(lambda: len(ends()) > count and ctx.state == "idle", "steer completion")
        count = len(ends())
        await machine._handle(Query(sid=ctx.key, msg_id="native-cancel", prompt="compatibility:slow"))
        await eventually(lambda: any(m.type == "user_msg" and m.msg_id == "native-cancel" for m in transport.sent), "cancel admission")
        await machine._handle(Interrupt(sid=ctx.key))
        await eventually(lambda: len(ends()) > count and ctx.state == "idle", "native cancel")
        assert ends()[-1].result.subtype == "interrupted"
        print("PASS native steer and cancellation", flush=True)

        for operation, answer in [("ask", "继续"), ("approval", "允许一次")]:
            count = len(ends())
            await machine._handle(Query(sid=ctx.key, msg_id="native-" + operation, prompt="compatibility:" + operation))
            await eventually(lambda: ctx.pending_asks, "native " + operation)
            ask_id = next(iter(ctx.pending_asks))
            await machine._handle(AnswerQuestion(sid=ctx.key, ask_id=ask_id, answer=answer, client_id="native-test"))
            await eventually(lambda: len(ends()) > count and ctx.state == "idle", "answered " + operation)
            assert ends()[-1].result.subtype == "success"
            assert not ctx.pending_asks
        print("PASS native questions and one-shot approvals", flush=True)

        before_plan = await native_state()
        for line, active in [("/plan", True), ("/plan off", False), ("/goal", False)]:
            result = await machine._handle(SetDshControl(sid=ctx.key, kind="command", value=line,
                cmd_id="native-control-" + line.replace("/", "").replace(" ", "-"), client_id="native-test"))
            assert result.type == "dsh_command_result" and result.status == "success", result
            await eventually(lambda: ctx.sdk.state.plan_active == active and not ctx.sdk.state.plan_pending, line)
        assert await native_state() == before_plan, 'bare plan/goal controls submitted model input'
        for answer, stays_active in [("Keep planning", True), ("Approve", False)]:
            count = len(ends())
            result = await machine._handle(SetDshControl(sid=ctx.key, kind="command", value="/plan compatibility:plan",
                cmd_id="native-review-" + str(stays_active), client_id="native-test"))
            assert result.status == "success", result
            await eventually(lambda: ctx.pending_asks, 'native plan review')
            ask_id = next(iter(ctx.pending_asks))
            assert "# Offline plan" in ctx.pending_asks[ask_id].event.question
            await machine._handle(AnswerQuestion(sid=ctx.key, ask_id=ask_id, answer=answer, client_id="native-test"))
            await eventually(lambda: len(ends()) > count and ctx.state == "idle", 'plan review completion')
            assert ctx.sdk.state.plan_active == stays_active
            assert not ctx.pending_asks
        print("PASS native plan entry/exit without model input; plan review, keep-planning and approval", flush=True)

        command = await machine._handle(SetDshControl(sid=ctx.key, kind="command", value="/goal compatibility goal",
            images=[image], files=files, cmd_id="goal-create", client_id="native-test"))
        assert command.type == "dsh_command_result" and command.status == "success", str(command)
        await eventually(lambda: ctx.sdk.state.goal, "goal projection")
        await machine._handle(SetDshControl(sid=ctx.key, kind="command", value="/goal pause", cmd_id="goal-pause", client_id="native-test"))
        await eventually(lambda: ctx.sdk.state.goal.phase == "paused", "pause goal")
        await eventually(lambda: ctx.state == "idle", "paused goal turn completes")
        paused_rounds = ctx.sdk.state.goal.rounds
        before_resume = await native_state()
        resumed = await machine._handle(SetDshControl(sid=ctx.key, kind="command", value="/goal resume", cmd_id="goal-resume", client_id="native-test"))
        assert resumed.status == "success", resumed
        await eventually(lambda: ctx.sdk.state.goal.phase == "active", "resume goal")
        await eventually(lambda: ctx.sdk.state.goal.rounds >= paused_rounds + 2, "autonomous goal rounds")
        assert (await native_state())["modelCalls"] > before_resume["modelCalls"]
        edited = await machine._handle(SetDshControl(sid=ctx.key, kind="command", value="/goal edit revised compatibility goal", cmd_id="goal-edit", client_id="native-test"))
        assert edited.status == "success", edited
        await eventually(lambda: ctx.sdk.state.goal.objective == "revised compatibility goal", "edit goal")
        await machine._handle(SetDshControl(sid=ctx.key, kind="command", value="/goal pause", cmd_id="goal-pause-final", client_id="native-test"))
        await machine._handle(Interrupt(sid=ctx.key))
        await machine._handle(SetDshControl(sid=ctx.key, kind="command", value="/goal clear", cmd_id="goal-clear", client_id="native-test"))
        await eventually(lambda: ctx.sdk.state.goal is None, "clear goal")
        print("PASS native command attachments, goal lifecycle and autonomous continuation", flush=True)

        await eventually(lambda: ctx.state == "idle", "goal test idle")
        created = await machine._handle(ActDshGoal(sid=ctx.key, action="create", objective="round cap acceptance",
            max_rounds=2, cmd_id="structured-goal", client_id="native-test"))
        assert created.type == "dsh_command_result" and created.status == "success", created
        await eventually(lambda: ctx.sdk.state.goal and ctx.sdk.state.goal.rounds == 2
            and ctx.sdk.state.goal.phase == "blocked" and ctx.state == "idle", "native two-round cap")
        current = ctx.sdk.state.goal
        ref = {"goal_id": current.id, "revision": current.revision}
        assert current.max_rounds == 2
        revised = await machine._handle(ActDshGoal(sid=ctx.key, action="edit", **ref, max_rounds=4,
            cmd_id="raise-round-cap", client_id="native-test"))
        assert revised.status == "success", revised
        await eventually(lambda: ctx.sdk.state.goal.max_rounds == 4, "native updated cap")
        stale = await machine._handle(ActDshGoal(sid=ctx.key, action="edit", **ref, max_rounds=8,
            cmd_id="stale-goal-ref", client_id="native-test"))
        assert stale.status == "error", stale
        assert ctx.sdk.state.goal.max_rounds == 4
        current = ctx.sdk.state.goal
        resumed = await machine._handle(ActDshGoal(sid=ctx.key, action="resume", goal_id=current.id,
            revision=current.revision, cmd_id="resume-raised-cap", client_id="native-test"))
        assert resumed.status == "success", resumed
        await eventually(lambda: ctx.sdk.state.goal.rounds == 4 and ctx.sdk.state.goal.phase == "blocked"
            and ctx.state == "idle", "native raised cap exhaustion")
        current = ctx.sdk.state.goal
        cleared = await machine._handle(ActDshGoal(sid=ctx.key, action="clear", goal_id=current.id,
            revision=current.revision, cmd_id="clear-structured-goal", client_id="native-test"))
        assert cleared.status == "success", cleared
        await eventually(lambda: ctx.sdk.state.goal is None, "structured goal cleared")
        print("PASS native Goal form: exact round caps, stale edit rejection, resume and cap exhaustion", flush=True)

        for kind, query in [("search", "compatibility"), ("references", "reference"),
                            ("subagents", ""), ("diagnostics", ""), ("conversation", "compatibility"), ("deliverables", "")]:
            result = await machine._handle(ReadDsh(sid=ctx.key, kind=kind, query=query,
                cmd_id="native-read-" + kind, client_id="native-test"))
            assert result.type == "dsh_read_result" and not result.error, (kind, result)
            if kind in {"search", "references", "conversation"}:
                assert result.items, (kind, result)
            if kind == "diagnostics":
                assert next(i.detail for i in result.items if i.id == "version") == TESTED_DSH_VERSION
        preview = await machine._handle(GetFilePreview(sid=ctx.key, path=str(root), request_id="directory-link", client_id="native-test"))
        assert preview.directory and not preview.error
        export = await machine._handle(DownloadDsh(sid=ctx.key, cmd_id="native-export", client_id="native-test"))
        assert export.type == "dsh_download_chunk" and not export.error and base64.b64decode(export.data).startswith(b"PK")
        await machine._handle(DownloadDsh(sid=ctx.key, cmd_id="native-export-cancel", client_id="native-test", export_id=export.export_id, cancel=True))
        await machine._handle(ArchiveSession(session_id=cold, engine="dsh", archived=True, cmd_id="native-archive", client_id="native-test"))
        assert native_session_id(cold) in runtime.archived
        archived = await runtime.handle_list_sessions(SimpleNamespace(cmd_id="native-list", client_id="native-test"))
        assert next(item for item in archived.sessions if item.session_id == cold).tag == "archived"
        assert (await machine._handle(GetHistory(session_id=cold, detail="summary", client_id="native-test"))).authoritative
        print("PASS native search, references, subagents, export, archive, diagnostics and directory links", flush=True)

    except BaseException:
        shutil.copyfile(log_path, installation / "native-test-last-error.log")
        os.chmod(installation / "native-test-last-error.log", 0o600)
        raise
    finally:
        for ctx in machine.sessions.values():
            if ctx.engine == "dsh":
                await ctx.sdk.disconnect()
            if ctx.queued_query_drain_task:
                ctx.queued_query_drain_task.cancel()
        await runtime.close()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.to_thread(process.wait, timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            await asyncio.to_thread(process.wait)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", type=Path, required=True)
    args = parser.parse_args()
    installation = args.installation.resolve()
    # Place the fixture below this installation so native ESM dependency lookup
    # resolves the exact same pinned DSH packages as the launcher.
    with tempfile.TemporaryDirectory(prefix="cc-remote-native-", dir=installation) as folder:
        asyncio.run(exercise(installation, Path(folder)))


if __name__ == "__main__":
    main()
