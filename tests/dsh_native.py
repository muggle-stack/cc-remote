"""Explicit offline integration probe against pinned, isolated native DSH."""

from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import textwrap
from cc_remote.wrapper.dsh_client import DshClient, DshConnection, TESTED_DSH_VERSION

REPO = Path(__file__).resolve().parents[1]


async def exercise(installation, root, *, product_only=False):
    assert (
        json.loads(
            (installation / "node_modules/@deepseek-ai/dsh/package.json").read_text()
        )["version"]
        == TESTED_DSH_VERSION
    )
    (root / "node_modules").symlink_to(
        installation / "node_modules", target_is_directory=True
    )
    shutil.copyfile(
        REPO / "tests/fixtures/dsh/scripted-adapter.mjs", root / "scripted-adapter.mjs"
    )
    (root / "toggle-fixture.mjs").write_text("export function apply() {}\n")
    # Explicitly opt the isolated preset into the upstream optional tool. The
    # installation and operator presets stay untouched.
    standard = (installation / "node_modules/@deepseek-ai/dsh-web-app/presets/standard.patch.yml").read_text()
    patch_preset = textwrap.dedent(standard.split("- insert:\n", 1)[1]).replace("applyPatch: false", "applyPatch: true")
    (root / "patch.yml").write_text(
        "- id: tool-fs\n  config:\n    applyPatch: true\n- id: session-query-sqlite\n  config:\n    path: ':memory:'\n    openAt: first-search\n- id: llm-deepseek\n  disabled: true\n- id: llm-pi-ai\n  disabled: true\n- insert:\n"
        + f"    - id: cc-remote-metadata\n      name: {json.dumps(str(REPO / 'integrations/dsh/cc-remote.mjs'))}\n    - id: offline-model\n      name: {json.dumps(str(root / 'scripted-adapter.mjs'))}\n    - id: cc-remote-toggle-fixture\n      name: {json.dumps(str(root / 'toggle-fixture.mjs'))}\n"
        + patch_preset
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log_path = root / "server.log"
    node = shutil.which("node")
    assert subprocess.check_output([node, "--version"], text=True).startswith("v24.")
    with log_path.open("wb") as log:
        os.chmod(log_path, 0o600)
        process = subprocess.Popen(
            [
                node,
                str(installation / "node_modules/.bin/dsh"),
                "--profile",
                "web",
                "--patch",
                str(root / "patch.yml"),
                "--port",
                str(port),
                "--no-open",
            ],
            cwd=root,
            env={
                "PATH": os.environ["PATH"],
                "HOME": str(root / "user"),
                "DSH_HOME": str(root / "home"),
                "LANG": "en_US.UTF-8",
            },
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    client = None
    try:
        async with asyncio.timeout(45):
            while True:
                assert process.poll() is None, (
                    "Isolated DSH startup failed; inspect private probe log."
                )
                match = re.search(
                    r"http://[^\s]+\?token=[A-Za-z0-9_-]+",
                    log_path.read_text(errors="replace"),
                )
                if match:
                    break
                await asyncio.sleep(0.1)
        client = DshClient(DshConnection.from_launch_url(match.group()))
        catalog = await client.rpc("session/modelCatalog")
        print(
            "PASS native API Peer, model catalog",
            [(g["id"], len(g["models"])) for g in catalog["groups"]],
            flush=True,
        )
        print(
            "DEFAULT",
            catalog["default"],
            "ROUTABLE",
            catalog["routableProviders"],
            flush=True,
        )
        before = await (await client._connection()).http.get(
            "/api/cc-remote.test-state"
        )
        draft = await client.draft_catalog(str(root), "standard")
        after = await (await client._connection()).http.get("/api/cc-remote.test-state")
        assert (
            "commands" in draft and "skills" in draft and before.json() == after.json()
        )
        print("PASS draft catalogs without an Agent or model turn", flush=True)
        presets = await client.rpc("agentPresets/list")
        print("PRESETS", [p["id"] for p in presets["presets"]], flush=True)
        created = await client.rpc(
            "session/create", {"request": {"cwd": str(root), "agentPreset": "standard"}}
        )
        sid = "dsh@" + created["sessionId"]
        await client.rpc(
            "session/selectModel",
            {
                "request": {
                    "sessionId": created["sessionId"],
                    "provider": "deepseek-official",
                    "model": "deepseek-flash",
                }
            },
        )
        page = await client.history_snapshot(sid)
        print("PASS official projections/page", page["cursor"], flush=True)
        meta = await client.metadata(sid)
        print(
            "PASS metadata",
            meta["version"],
            [c["name"] for c in meta["commands"]],
            flush=True,
        )
        frames = []
        ready = asyncio.Event()
        done = asyncio.Event()

        async def watch():
            async for frame in client.stream(
                "session/follow",
                {
                    "request": {
                        "address": {
                            "kind": "session",
                            "sessionId": created["sessionId"],
                        },
                        "assistantStream": True,
                    }
                },
            ):
                frames.append(frame)
                if frame["type"] == "snapshot":
                    ready.set()
                if frame.get("event", {}).get("type") == "turn/end":
                    done.set()

        passive_before = (await (await client._connection()).http.get("/api/cc-remote.test-state")).json()
        task = asyncio.create_task(watch())
        await asyncio.wait_for(ready.wait(), 10)
        passive_after = (await (await client._connection()).http.get("/api/cc-remote.test-state")).json()
        assert passive_before == passive_after
        print("PASS passive follow does not activate an Agent or model turn", flush=True)
        await client.rpc(
            "session/prompt",
            {
                "request": {
                    "sessionId": created["sessionId"],
                    "requestId": "fixture-first",
                    "mode": "queue",
                    "content": [{"type": "text", "text": "compatibility:simple"}],
                }
            },
        )
        await asyncio.wait_for(done.wait(), 20)
        (root / "flow.json").write_text(
            json.dumps(frames, ensure_ascii=False, indent=2)
        )
        print("PASS native prompt+follow", len(frames), "frames", flush=True)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        from tests.test_multisession import _mk_machine
        from cc_remote.protocol import (
            NewSession,
            Query,
            AnswerQuestion,
            GetHistory,
            Interrupt,
            GetEngineCapabilities,
        )

        machine, transport = _mk_machine()
        runtime = machine._dsh
        runtime.client = client

        async def wait(predicate, timeout=20):
            async with asyncio.timeout(timeout):
                while not predicate():
                    await asyncio.sleep(0.02)

        try:
            catalog = await machine._handle(GetEngineCapabilities(
                engine="dsh", cwd=str(root), skills_only=True, dsh_agent_preset="standard",
                cmd_id="draft-catalog", client_id="native-test"))
            assert catalog.type == "engine_capabilities" and catalog.sid is None, catalog
            assert {"goal", "plan"} <= {command.name for command in catalog.dsh_commands}
            assert not machine.sessions
            print("PASS current new-chat native command catalog without session creation", flush=True)
            result = await machine._handle(
                NewSession(
                    engine="dsh",
                    cwd=str(root),
                    dsh_agent_preset="standard",
                    prompt="compatibility:simple",
                    msg_id="runtime-1",
                    request_id="runtime-create",
                    client_id="native-test",
                )
            )
            assert len(result) == 1 and result[0].type == "session_focus", result
            assert result[0].request_id == "runtime-create"
            ctx = machine._focused_ctx()
            assert ctx and ctx.engine == "dsh"

            def ends():
                return [
                    m
                    for m in transport.sent
                    if m.type == "turn_end"
                    and m.sid == ctx.key
                    and m.result.subtype != "steered"
                ]

            await wait(lambda: ends() and ctx.state == "idle")
            assert ends()[-1].result.subtype == "success", ends()[-1]
            print(
                "PASS Wrapper create, model selection, streaming, native terminal",
                flush=True,
            )
            if product_only:
                from tests.dsh_native_plugins import exercise as exercise_plugins
                from tests.dsh_native_product import exercise as exercise_product

                await exercise_plugins(machine, runtime, ctx.key)
                await exercise_product(machine, runtime, ctx.key)
                return
            for operation in ["ask", "approval", "multi"]:
                count = len(ends())
                await machine._handle(
                    Query(
                        sid=ctx.key,
                        prompt="compatibility:" + operation,
                        msg_id="runtime-" + operation,
                    )
                )
                await wait(lambda: ctx.pending_asks)
                ask = next(iter(ctx.pending_asks.values())).event
                answer = "允许一次" if operation == "approval" else "继续"
                if operation == "multi":
                    assert len(ask.questions) == 3
                    # A disconnect preserves the same UI question; only the
                    # official replay may reauthorize its answer delivery.
                    previous_client = runtime.event_client
                    await client._peer.socket.close()
                    await wait(lambda: runtime.event_client and runtime.event_client != previous_client and runtime.questions)
                    assert ask.ask_id in ctx.pending_asks
                await machine._handle(
                    AnswerQuestion(
                        sid=ctx.key,
                        ask_id=ask.ask_id,
                        answer=answer if not ask.questions else None,
                        answers=[answer] * len(ask.questions)
                        if ask.questions
                        else None,
                        client_id="native-test",
                    )
                )
                await wait(lambda: len(ends()) > count and ctx.state == "idle")
                assert ends()[-1].result.subtype == "success", ends()[-1]
                print("PASS Wrapper native", operation, flush=True)
            history = await machine._handle(
                GetHistory(
                    session_id=ctx.key, detail="summary", client_id="native-test"
                )
            )
            assert history.type == "history" and history.turns, history
            print("PASS Wrapper history", len(history.turns), flush=True)

            from cc_remote.protocol import (
                Steer,
                ReadDsh,
                SetDshControl,
                ArchiveSession,
                GetSessionArtifacts,
            )

            count = len(ends())
            await machine._handle(
                Query(sid=ctx.key, prompt="compatibility:slow", msg_id="slow-steer")
            )
            await wait(lambda: ctx.active_turn_binding is not None)
            await machine._handle(
                Steer(sid=ctx.key, prompt="compatibility:simple", msg_id="native-steer", cmd_id="steer-probe", client_id="native-test")
            )
            await wait(lambda: len(ends()) > count and ctx.state == "idle")
            assert any(
                m.type == "user_msg" and m.msg_id == "native-steer"
                for m in transport.sent
            )
            print("PASS native steer admission identity", flush=True)
            count = len(ends())
            await machine._handle(
                Query(sid=ctx.key, prompt="compatibility:slow", msg_id="slow-queue")
            )
            await machine._handle(
                Query(
                    sid=ctx.key,
                    prompt="compatibility:simple",
                    msg_id="queued",
                    delivery="queue", cmd_id="queue-probe", client_id="native-test",
                )
            )
            await wait(lambda: len(ends()) >= count + 2 and ctx.state == "idle")
            assert not ctx.queued_queries
            print("PASS wrapper-owned queue drains without a browser", flush=True)
            count = len(ends())
            await machine._handle(
                Query(sid=ctx.key, prompt="compatibility:slow", msg_id="slow-interrupt")
            )
            await wait(lambda: ctx.active_turn_binding is not None)
            await machine._handle(Interrupt(sid=ctx.key))
            await wait(lambda: len(ends()) > count and ctx.state == "idle")
            assert ends()[-1].result.subtype == "interrupted"
            print("PASS explicit interrupt drains native terminal", flush=True)
            await runtime.client.rpc(
                "commands/execute",
                {
                    "agentId": ctx.sdk.native_id,
                    "line": "/permission danger-full-access",
                    "submittedAttachments": [],
                },
            )
            (root / "diff-fixture.txt").write_text("original native file\n")
            count = len(ends())
            await machine._handle(
                Query(
                    sid=ctx.key, prompt="compatibility:diff-first", msg_id="native-edit"
                )
            )
            await wait(lambda: len(ends()) > count and ctx.state == "idle")
            assert (root / "diff-fixture.txt").read_text() == "first native edit\n"
            artifacts = await machine._handle(
                GetSessionArtifacts(
                    sid=ctx.key,
                    engine="dsh",
                    request_id="artifacts-native",
                    client_id="native-test",
                )
            )
            assert not artifacts.error and any(
                e.name == "diff-fixture.txt" for e in artifacts.entries
            ), artifacts
            print("PASS native file mutations enter current Artifacts", flush=True)
            count = len(ends())
            await machine._handle(Query(sid=ctx.key, prompt="compatibility:patch", msg_id="native-patch"))
            await wait(lambda: len(ends()) > count and ctx.state == "idle")
            assert (root / "patch-fixture.txt").read_text() == "native patch output\n"
            artifacts = await machine._handle(GetSessionArtifacts(sid=ctx.key, engine="dsh",
                request_id="patch-artifacts", client_id="native-test"))
            assert any(e.name == "patch-fixture.txt" for e in artifacts.entries), artifacts
            print("PASS native apply_patch acknowledged outputs enter Artifacts", flush=True)
            receipt = await (await client._connection()).http.post(
                "/api/cc-remote.test-job", json={"sessionId": ctx.sdk.native_id}
            )
            assert receipt.status_code == 200, receipt.status_code
            job = receipt.json()["id"]
            await wait(lambda: any(j.id == job for j in ctx.sdk.state.jobs))
            output = await machine._handle(
                ReadDsh(
                    sid=ctx.key,
                    kind="job_output",
                    job_id=job,
                    cmd_id="read-job",
                    client_id="native-test",
                )
            )
            assert not output.error and "native output" in output.items[0].detail, (
                output
            )
            killed = await machine._handle(
                SetDshControl(
                    sid=ctx.key,
                    kind="job_stop",
                    value=job,
                    cmd_id="kill-job",
                    client_id="native-test",
                )
            )
            assert killed.status == "success", killed
            await wait(
                lambda: any(
                    j.id == job and j.status == "killed" for j in ctx.sdk.state.jobs
                )
            )
            print("PASS native job list, output and kill", flush=True)
            await wait(lambda: ctx.state == "idle")
            count = len(ends())
            await machine._handle(
                Query(
                    sid=ctx.key, prompt="compatibility:subagent", msg_id="native-child"
                )
            )
            await wait(lambda: ctx.sdk.children)
            child = ctx.sdk.children[0]
            tree = await machine._handle(
                ReadDsh(
                    sid=ctx.key,
                    kind="subagents",
                    cmd_id="tree",
                    client_id="native-test",
                )
            )
            assert tree.items and tree.items[0].mode, tree
            await wait(
                lambda: (
                    ctx.state == "idle"
                    and not any(
                        c.get("activity") == "running" for c in ctx.sdk.children
                    )
                    and any(m.type == "turn_binding" and m.sid == ctx.key
                            and m.continuation == "subagent"
                            and any(end.turn_id == m.turn_id for end in ends())
                            for m in transport.sent)
                )
            )
            child_page = await client.history_snapshot("dsh@" + child["id"])
            assert child_page["records"]
            print("PASS native child catalog and parent-addressed history", flush=True)
            # File/image upload uses native admission and native attachment reads.
            import base64
            import io
            from PIL import Image
            image = io.BytesIO()
            Image.new("RGB", (2, 2), (30, 90, 180)).save(image, format="PNG")
            count = len(ends())
            admitted = await machine._handle(Query(sid=ctx.key, prompt="compatibility:attachments", msg_id="attachment-probe",
                files=[{"filename":"fixture.txt", "data":base64.b64encode(b"offline fixture").decode()}],
                images=[{"media_type":"image/png", "data":base64.b64encode(image.getvalue()).decode()}]))
            assert not admitted, admitted
            await wait(lambda: ctx.state == "idle" and any(
                m.type == "turn_binding" and m.sid == ctx.key and m.msg_id == "attachment-probe"
                and any(end.turn_id == m.turn_id for end in ends()) for m in transport.sent))
            history = await machine._handle(GetHistory(session_id=ctx.key, detail="summary", client_id="native-test"))
            assert any(t.imageRefs for t in history.turns), history
            from cc_remote.protocol import GetHistoryImage
            image_turn = next(t for t in history.turns if t.imageRefs)
            image_reply = await machine._handle(GetHistoryImage(session_id=ctx.key, turn_id=image_turn.id,
                image_id=image_turn.imageRefs[0]["image_id"], revision=history.revision,
                variant="full", request_id="native-image", client_id="native-test"))
            assert image_reply.data and not image_reply.error, image_reply
            print("PASS native file/image admission and deferred history", flush=True)
            plugins = await machine._handle(ReadDsh(sid=ctx.key, kind="plugins", cmd_id="plugins", client_id="native-test"))
            toggle = next(item for item in plugins.items if "toggle-fixture" in item.title)
            for action in ["plugin_disable", "plugin_enable"]:
                result = await machine._handle(SetDshControl(sid=ctx.key, kind=action, value=toggle.id,
                    cmd_id=action, client_id="native-test"))
                assert result.status == "success", result
            print("PASS native plugin inventory and enable/disable receipts", flush=True)
            from cc_remote.protocol import ActDshGoal
            goal = await machine._handle(ActDshGoal(sid=ctx.key, action="create", objective="Offline one-round probe",
                max_rounds=1, cmd_id="create-goal", client_id="native-test"))
            assert goal.status == "success", goal
            await wait(lambda: ctx.sdk.state.goal)
            ref = ctx.sdk.state.goal
            paused = await machine._handle(ActDshGoal(sid=ctx.key, action="pause", goal_id=ref.id, revision=ref.revision,
                cmd_id="pause-goal", client_id="native-test"))
            assert paused.status == "success", paused
            ref = ctx.sdk.state.goal
            cleared = await machine._handle(ActDshGoal(sid=ctx.key, action="clear", goal_id=ref.id, revision=ref.revision,
                cmd_id="clear-goal", client_id="native-test"))
            assert cleared.status == "success", cleared
            await wait(lambda: ctx.state == "idle")
            print("PASS exact-revision native Goal create, pause and clear", flush=True)
            for line in ["/plan", "/plan off"]:
                command = await machine._handle(Query(sid=ctx.key, prompt=line, msg_id="command-"+str(len(line)),
                    cmd_id="native-command-"+str(len(line)), client_id="native-test"))
                assert command.type == "dsh_command_result" and command.status == "success", command
                await wait(lambda: ctx.sdk.state.plan_active == (line == "/plan"))
            print("PASS native slash routing and Plan state", flush=True)
            for archived in [True, False]:
                result = await machine._handle(
                    ArchiveSession(session_id=ctx.key, engine="dsh", archived=archived)
                )
                assert result is None, result
                assert (ctx.sdk.native_id in runtime.archived) == archived
            print("PASS archive and unarchive", flush=True)
            first_result = await machine._handle(NewSession(
                engine="dsh", cwd=str(root), dsh_agent_preset="standard", prompt="/plan",
                msg_id="first-command", request_id="create-command", client_id="native-test"))
            assert len(first_result) == 2 and first_result[0].type == "session_focus", first_result
            assert first_result[0].request_id == "create-command"
            first_command = first_result[1]
            assert first_command.type == "dsh_command_result" and first_command.status == "success", first_command
            assert first_command.request_id == "create-command"
            command_ctx = machine._focused_ctx()
            await wait(lambda: command_ctx.sdk.state.plan_active)
            assert command_ctx.state == "idle"
            print("PASS native command as the first new-session input", flush=True)
            from tests.dsh_native_plugins import exercise as exercise_plugins
            await exercise_plugins(machine, runtime, ctx.key)
            from tests.dsh_native_surfaces import exercise as exercise_surfaces
            await exercise_surfaces(machine, runtime, ctx.key, child)
            from tests.dsh_native_product import exercise as exercise_product
            await exercise_product(machine, runtime, ctx.key)
        finally:
            await runtime.close()

    finally:
        if client:
            await client.close()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        await asyncio.to_thread(process.wait, timeout=10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--installation", type=Path, required=True)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--product-only", action="store_true")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="cc-remote-dsh-v4-"))
    print("Offline probe directory:", root, flush=True)
    try:
        asyncio.run(exercise(args.installation.resolve(), root, product_only=args.product_only))
    finally:
        if not args.keep:
            shutil.rmtree(root)


if __name__ == "__main__":
    main()
