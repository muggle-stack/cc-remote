from types import SimpleNamespace
import asyncio
import json
import pytest

from claude_agent_sdk.types import SystemMessage

from cc_remote.wrapper.claude_model_fallback import model_fallback_event
from cc_remote.wrapper.history_store import materialize_history_turns
from cc_remote.wrapper.stream import (
    StreamTranslator, extract_model, recover_claude_native_metadata,
    translate_history,
)


def fallback(**changes):
    return dict({
        "type": "system", "subtype": "model_refusal_fallback",
        "uuid": "00000000-0000-4000-8000-000000000002",
        "originalModel": "claude-fable-5-1[1m]",
        "fallbackModel": "claude-opus-4-8[1m]",
        "scope": "session", "trigger": "refusal",
        "timestamp": "2026-09-08T01:00:01Z",
        "content": "untrusted provider content SECRET",
    }, **changes)


def test_official_fallback_is_bounded_stable_and_changes_only_session_model():
    data = fallback()
    msg = SystemMessage(subtype=data["subtype"], data=data)
    event, = StreamTranslator(1000, turn_id="turn").feed(msg)
    assert event.tool == "model_refusal_fallback"
    assert "SECRET" not in event.model_dump_json()
    assert extract_model(msg) == data["fallbackModel"]
    assert model_fallback_event(fallback(timestamp=None)).item_id == event.item_id
    transient = fallback(scope="turn")
    assert extract_model(SystemMessage(subtype=transient["subtype"], data=transient)) is None
    assert model_fallback_event(fallback(fallbackModel="https://provider/SECRET")) is None
    assert model_fallback_event(fallback(subtype="init")) is None
    assert model_fallback_event(fallback(parent_tool_use_id="subagent")) is None
    assert model_fallback_event(fallback(parentToolUseID="subagent")) is None
    assert model_fallback_event(fallback(isSidechain=True)) is None


def test_live_fallback_updates_effective_model_persists_and_notifies_once():
    from cc_remote.wrapper.sdk import SdkHandle
    from tests.test_multisession import _mk_ctx, _mk_machine

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("fallback-session", "fallback-session")
        ctx.active_msg_id = "turn"
        sdk = ctx.sdk = SdkHandle(machine.cfg)
        sdk.model = "claude-fable-5-1[1m]"
        persisted = []
        async def persist(target):
            persisted.append(target.sdk.model)
        machine._persist_claude_session_controls = persist
        message = SystemMessage(subtype="model_refusal_fallback", data=fallback())
        sdk._observe_model_fallback(message)
        assert sdk.model == "claude-opus-4-8[1m]"
        await machine._observe_claude_model_fallback(ctx, message)
        await machine._observe_claude_model_fallback(ctx, message)
        assert len([event for event in transport.sent if event.type == "notice"]) == 1
        assert len([event for event in transport.sent if event.type == "model"]) == 1
        assert ctx.claude_model_fallback_notice.detail == "切换时间 2026-09-08T01:00:01+00:00"
        assert persisted[-1] == sdk.model
        sdk.model = "claude-fable-5-1[1m]"  # a newer explicit selection won
        await machine._observe_claude_model_fallback(ctx, message)
        assert persisted[-1] == "claude-opus-4-8[1m]"  # not overwritten by the late callback
        transient = SystemMessage(subtype="model_refusal_fallback", data=fallback(scope="turn"))
        sdk._observe_model_fallback(transient)
        assert sdk.model == "claude-fable-5-1[1m]"
    asyncio.run(run())


def test_history_restores_fallback_without_sidechain_or_prompt_injection(tmp_path):
    human = {
        "type": "user", "uuid": "00000000-0000-4000-8000-000000000001",
        "parentUuid": None, "timestamp": "2026-09-08T01:00:00Z",
        "message": {"role": "user", "content": "hello"},
    }
    notice = fallback(parentUuid=human["uuid"])
    answer = {
        "type": "assistant", "uuid": "00000000-0000-4000-8000-000000000003",
        "parentUuid": notice["uuid"], "timestamp": "2026-09-08T01:00:02Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn"},
    }
    side = fallback(uuid="side", parentUuid=human["uuid"], isSidechain=True)
    later_human = dict(human, uuid="later-user", parentUuid=answer["uuid"])
    later_notice = fallback(uuid="later-notice", parentUuid=later_human["uuid"])
    later_answer = dict(answer, uuid="later-answer", parentUuid=later_notice["uuid"])
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in (
        human, notice, answer, side, later_human, later_notice, later_answer)))
    messages = [SimpleNamespace(type=row["type"], uuid=row["uuid"], message=row["message"])
                for row in (human, answer)]
    timestamps = {}
    internal = {}
    restored = recover_claude_native_metadata(
        "session", messages, path=str(path), timestamps=timestamps, internal_events=internal)
    assert [row.uuid for row in restored] == [human["uuid"], notice["uuid"], answer["uuid"]]
    assert list(internal) == [notice["uuid"]]
    events = translate_history(restored, 1000, timestamps=timestamps, internal_user_events=internal)
    assert len([event for event in events if event.type == "user_msg"]) == 1
    summary, = materialize_history_turns([event.model_dump() for event in events])
    notices = [block for block in summary["blocks"] if block.get("tool") == "model_refusal_fallback"]
    assert len(notices) == 1
    assert summary["processDetailState"] == "none"
    assert summary["detailEventCount"] == 0
    assert "SECRET" not in json.dumps(summary)
    # A page starting after the switch must not inject an earlier notice.
    late = recover_claude_native_metadata(
        "session", messages[1:], path=str(path), timestamps={}, internal_events={})
    assert [row.uuid for row in late] == [answer["uuid"]]


@pytest.mark.parametrize("scope", ["session", None])
def test_real_sdk_live_fields_update_model_and_context_without_reconnect(scope):
    from claude_agent_sdk._internal.message_parser import parse_message
    from cc_remote.config import WrapperConfig
    from cc_remote.wrapper.sdk import SdkHandle

    data = {"type": "system", "subtype": "model_refusal_fallback",
            "original_model": "claude-opus-5-5", "fallback_model": "claude-opus-5",
            "request_id": "native-fallback", "trigger": "refusal"}
    if scope is not None:
        data["scope"] = scope
    sdk = SdkHandle(WrapperConfig())
    sdk.model = data["original_model"]
    message = parse_message(data)
    sdk._observe_model_fallback(message)
    assert sdk.model == data["fallback_model"]
    sdk._record_context_usage({"model": "claude-opus-5", "totalTokens": 200,
                               "maxTokens": 1_000_000}, update_model=True)
    assert sdk.model == data["fallback_model"]
    assert extract_model(message) == data["fallback_model"]
    assert model_fallback_event({**data, "originalModel": "claude-other"}) is None


def test_reattached_service_snapshot_wins_over_older_fallback_journal_rows():
    from cc_remote.config import WrapperConfig
    from cc_remote.wrapper.sdk import SdkHandle

    sdk = SdkHandle(WrapperConfig())
    sdk.model = "claude-opus-5-5"
    sdk._model_service_seq = 12
    message = SystemMessage(subtype="model_refusal_fallback", data=fallback())
    message._cc_service_seq = 9
    sdk._observe_model_fallback(message)
    assert sdk.model == "claude-opus-5-5"
    message._cc_service_seq = 13
    sdk._observe_model_fallback(message)
    assert sdk.model == message.data["fallbackModel"]


def test_fallback_notice_recovers_only_the_current_main_model(tmp_path):
    from cc_remote.wrapper.claude_controls import last_model_fallback

    path = tmp_path / "session.jsonl"
    rows = [fallback(), {"type": "assistant", "isSidechain": False,
                         "message": {"model": "claude-opus-4-8"}}]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    event, timestamp = last_model_fallback(str(path), 100000)
    assert event.input["fallback_model"] == "claude-opus-4-8[1m]"
    assert timestamp == "2026-09-08T01:00:01+00:00"
    with path.open("a") as stream:
        stream.write(json.dumps({"type": "assistant", "message": {"model": "claude-fable-5-1"}}) + "\n")
    assert last_model_fallback(str(path), 100000) is None


def test_replayed_fallback_uses_the_native_switch_time(tmp_path):
    from cc_remote.protocol import Notice
    from cc_remote.wrapper.sdk import SdkHandle
    from tests.test_multisession import _mk_ctx, _mk_machine

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("native-session", "native-session")
        machine.sessions[ctx.key] = ctx
        ctx.sdk = SdkHandle(machine.cfg)
        path = tmp_path / "native.jsonl"
        path.write_text(json.dumps(fallback()) + "\n")
        machine._claude_transcript_path = lambda *args: str(path)

        async def persist(_ctx):
            pass

        machine._persist_claude_session_controls = persist
        message = SystemMessage(subtype="model_refusal_fallback", data=fallback(timestamp=None))
        ctx.sdk._observe_model_fallback(message)
        await machine._observe_claude_model_fallback(ctx, message)
        notice = next(event for event in transport.sent if isinstance(event, Notice))
        assert notice.detail == "切换时间 2026-09-08T01:00:01+00:00"

    asyncio.run(run())


@pytest.mark.parametrize("choice, result", [
    ("同意回退", "retry_fallback"), ("拒绝回退", "cancelled"), ("编辑提问", "edit_prompt"),
])
def test_native_fallback_question_uses_reconnectable_question_lifecycle(choice, result):
    from cc_remote.protocol import AskUser, AskUserClosed, AnswerQuestion, Hello
    from tests.test_multisession import _mk_ctx, _mk_machine

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("native-session", "native-session")
        machine.sessions[ctx.key] = ctx
        ctx.active_msg_id = "human-turn"
        ctx.sdk = SimpleNamespace(service_turn_metadata={"prompt": "原始提问，等待编辑"})
        task = asyncio.create_task(machine._on_claude_model_fallback_dialog(ctx, "native-dialog", {
            "originalModel": "claude-opus-5-6", "fallbackModel": "claude-opus-5-5",
        }))
        async with asyncio.timeout(2):
            while not ctx.pending_asks:
                await asyncio.sleep(0)
        event = next(event for event in transport.sent if isinstance(event, AskUser))
        assert event.model_fallback.original_model == "claude-opus-5-6"
        assert event.model_fallback.original_prompt == "原始提问，等待编辑"
        assert "原始提问" not in repr(ctx.buffer._buf)
        await machine._handle_client_hello(Hello(role="client", client_id="reconnected", last_seq=0, sid=ctx.key))
        restored = [value for value in transport.sent if isinstance(value, AskUser)][-1]
        assert restored.ask_id == event.ask_id
        assert restored.model_fallback == event.model_fallback
        await machine._handle_answer_question(AnswerQuestion(sid=ctx.key, ask_id=event.ask_id, answer=choice))
        assert await asyncio.wait_for(task, 2) == {"behavior": "completed", "result": result}
        assert not ctx.pending_asks
        assert isinstance(transport.sent[-1], AskUserClosed)

    asyncio.run(run())


def test_native_fallback_cancellation_closes_question_without_answering():
    from cc_remote.protocol import AskUserClosed
    from tests.test_multisession import _mk_ctx, _mk_machine

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("native-session", "native-session")
        task = asyncio.create_task(machine._on_claude_model_fallback_dialog(ctx, "native-dialog", {
            "originalModel": "claude-opus-5-6", "fallbackModel": "claude-opus-5-5",
        }))
        async with asyncio.timeout(2):
            while not ctx.pending_asks:
                await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not ctx.pending_asks
        assert isinstance(transport.sent[-1], AskUserClosed)
        assert transport.sent[-1].reason == "cancelled"

    asyncio.run(run())


def test_fallback_preference_defaults_to_asking_and_saves_only_explicit_account_opt_in(tmp_path):
    from cc_remote.config import WrapperConfig
    from cc_remote.wrapper.claude_settings import automatic_model_fallback
    from cc_remote.wrapper.sdk import SdkHandle

    async def run():
        primary = tmp_path / "primary"
        primary.mkdir()
        path = primary / "settings.json"
        path.write_text(json.dumps({"model": "claude-opus-5-6", "env": {"PRIVATE": "preserve"}}))
        other = tmp_path / "other"
        other.mkdir()
        other_path = other / "settings.json"
        other_path.write_text('{"switchModelsOnFlag": false}')
        sdk = SdkHandle(WrapperConfig(), claude_config_dir=str(primary))
        requests = []

        async def control(request):
            requests.append(request)
            return {}

        sdk.client = SimpleNamespace(
            description={"supported_dialog_kinds": ["refusal_fallback_prompt"]},
            _query=SimpleNamespace(_send_control_request=control))
        sdk.user_dialog_callback = object()
        await sdk._configure_model_fallback_policy()
        assert requests[-1]["settings"] == {"switchModelsOnFlag": False}
        assert "switchModelsOnFlag" not in json.loads(path.read_text())
        await sdk.allow_automatic_model_fallback()
        assert automatic_model_fallback(primary)
        assert requests[-1]["settings"] == {"switchModelsOnFlag": True}
        assert json.loads(path.read_text())["env"] == {"PRIVATE": "preserve"}
        assert not automatic_model_fallback(other)
        assert other_path.read_text() == '{"switchModelsOnFlag": false}'
        await sdk._configure_model_fallback_policy()
        assert requests[-1]["settings"] == {"switchModelsOnFlag": True}

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_native_dialog_transport_advertises_capability_and_keeps_stdout_reading(monkeypatch, cancel):
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    from cc_remote.wrapper.claude_transport import user_dialog_transport

    async def run():
        queue = asyncio.Queue()
        writes = []
        received = []
        entered = asyncio.Event()
        release = asyncio.Event()

        async def write(self, data):
            writes.append(json.loads(data))

        async def read(self):
            while True:
                value = await queue.get()
                if value is None:
                    return
                yield value

        async def answer(request_id, payload):
            assert request_id == "native-dialog"
            assert payload["fallbackModel"] == "claude-opus-5-5"
            entered.set()
            await release.wait()
            return {"behavior": "completed", "result": "retry_fallback"}

        monkeypatch.setattr(SubprocessCLITransport, "write", write)
        monkeypatch.setattr(SubprocessCLITransport, "_read_messages_impl", read)
        transport = user_dialog_transport(ClaudeAgentOptions(), answer)
        await transport.write(json.dumps({"type": "control_request", "request_id": "init",
                                          "request": {"subtype": "initialize"}}))
        assert writes[0]["request"]["supportedDialogKinds"] == ["refusal_fallback_prompt"]

        async def consume():
            async for value in transport.read_messages():
                received.append(value)

        reader = asyncio.create_task(consume())
        request = {"type": "control_request", "request_id": "native-dialog", "request": {
            "subtype": "request_user_dialog", "dialog_kind": "refusal_fallback_prompt",
            "payload": {"originalModel": "claude-opus-5-6", "fallbackModel": "claude-opus-5-5"},
        }}
        await queue.put(request)
        await asyncio.wait_for(entered.wait(), 2)
        await queue.put(request)  # Native redelivery retains the existing waiter.
        await queue.put({"type": "system", "subtype": "status", "status": "working"})
        async with asyncio.timeout(2):
            while not received:
                await asyncio.sleep(0)
        assert len(transport.dialog_tasks) == 1
        if cancel:
            await queue.put({"type": "control_cancel_request", "request_id": "native-dialog"})
        else:
            release.set()
        async with asyncio.timeout(2):
            while transport.dialog_tasks:
                await asyncio.sleep(0)
        responses = [message for message in writes if message["type"] == "control_response"]
        assert len(responses) == (0 if cancel else 1)
        if responses:
            assert responses[0]["response"]["response"] == {
                "behavior": "completed", "result": "retry_fallback"}
        await queue.put(None)
        await asyncio.wait_for(reader, 2)
        assert len(received) == 1

    asyncio.run(run())


def test_service_keeps_actual_fallback_model_across_controller_replacement():
    from tests.test_claude_service import environment, released

    async def run():
        async with environment() as (service, attach):
            first = await attach()
            worker = service.sessions[first.id]
            worker.controls["model"] = "claude-opus-5-6"
            await worker.client.queue.put({
                "type": "system", "subtype": "model_refusal_fallback", "scope": "session",
                "original_model": "claude-opus-5-6", "fallback_model": "claude-opus-5-5",
            })
            async with asyncio.timeout(2):
                while worker.controls["model"] != "claude-opus-5-5":
                    await asyncio.sleep(0)
            await first.detach()
            await released(worker)
            second = await attach()
            assert second.id == first.id
            assert second.description["controls"]["model"] == "claude-opus-5-5"
            assert second.description["model_state_seq"] > 0

    asyncio.run(run())


def test_service_native_dialog_survives_wrapper_detachment():
    from tests.test_claude_service import environment, released

    async def run():
        async with environment() as (service, attach):
            first = await attach()
            entered = asyncio.Event()

            async def waiting(*args):
                entered.set()
                await asyncio.Future()

            first.user_dialog_callback = waiting
            worker = service.sessions[first.id]
            native = asyncio.create_task(worker.callback("user_dialog", {
                "request_id": "native-dialog", "payload": {
                    "originalModel": "claude-opus-5-6", "fallbackModel": "claude-opus-5-5"},
            }))
            await asyncio.wait_for(entered.wait(), 2)
            await first.detach()
            await released(worker)
            assert not native.done()
            second = await attach()

            async def answer(*args):
                return {"behavior": "completed", "result": "cancelled"}

            second.user_dialog_callback = answer
            assert await asyncio.wait_for(native, 2) == {"behavior": "completed", "result": "cancelled"}
            assert second.id == first.id
            assert not worker.callbacks

    asyncio.run(run())


def test_context_refresh_repairs_only_a_source_proven_fallback(tmp_path):
    from cc_remote.protocol import Model
    from cc_remote.wrapper.sdk import SdkHandle
    from tests.test_multisession import _mk_ctx, _mk_machine

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("native-session", "native-session")
        machine.sessions[ctx.key] = ctx
        sdk = ctx.sdk = SdkHandle(machine.cfg)
        sdk.model = "claude-opus-5-6"
        path = tmp_path / "native.jsonl"
        path.write_text(json.dumps(fallback(originalModel=sdk.model, fallbackModel="claude-opus-5-5")) + "\n")
        machine._claude_transcript_path = lambda *args: str(path)

        async def persist(_ctx):
            pass

        machine._persist_claude_session_controls = persist
        usage = {"model": "claude-opus-5-5", "totalTokens": 500, "maxTokens": 1_000_000}
        sdk._record_context_usage(usage, update_model=True)
        assert sdk.model == "claude-opus-5-6"  # A provider observation alone has no authority.
        await machine._reconcile_claude_context_fallback(ctx, usage)
        assert sdk.model == "claude-opus-5-5"
        assert any(isinstance(event, Model) and event.model == sdk.model for event in transport.sent)
        assert sdk._last_context_usage["totalTokens"] == 500
        assert ctx.claude_model_fallback_notice is not None

        sdk.model = "claude-opus-5-6"
        path.write_text(json.dumps({"type": "assistant", "message": {"model": "claude-opus-5-5"}}) + "\n")
        await machine._reconcile_claude_context_fallback(ctx, usage)
        assert sdk.model == "claude-opus-5-6"

    asyncio.run(run())
