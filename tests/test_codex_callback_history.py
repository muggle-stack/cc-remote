"""Callback prefixes remain distinct from a human steer in that native turn."""
import json

import pytest

from cc_remote.protocol import TurnEnd, UserMsg
from cc_remote.wrapper.codex_client_messages import CodexClientMessageAliases
from cc_remote.wrapper.codex_stream import codex_translate_history
from cc_remote.wrapper.machine import WrapperMachine
from cc_remote.wrapper.history_store import materialize_history_turns


@pytest.mark.parametrize("completed", [False, True])
def test_first_human_steer_closes_callback_without_an_interruption(tmp_path, completed):
    path = tmp_path / "callback.jsonl"
    records = [
        {"type": "task_started", "turn_id": "callback"},
        {"type": "agent_message", "message": "Background progress"},
        {"type": "user_message", "message": "Where is my message?", "turn_id": "callback"},
        {"type": "agent_message", "message": "Human reply"},
    ]
    if completed:
        records.append({"type": "task_complete", "turn_id": "callback"})
    path.write_text("".join(json.dumps({"timestamp": f"2026-11-10T10:10:0{i}Z",
                                       "type": "event_msg", "payload": p}) + "\n"
                            for i, p in enumerate(records)))
    events, _ = codex_translate_history(str(path), 4096)
    assert [event.prompt for event in events if isinstance(event, UserMsg)] == ["Where is my message?"]
    turns = materialize_history_turns([event.model_dump(mode="json") for event in events])
    assert [turn["prompt"] for turn in turns] == ["", "Where is my message?"]
    ends = [event for event in events if isinstance(event, TurnEnd)]
    assert len(ends) == (2 if completed else 1)
    assert not any(event.result.is_error or event.result.subtype == "interrupted" for event in ends)


@pytest.mark.parametrize("turns", [False, True])
def test_old_callback_cache_drops_only_synthetic_user_alias(turns):
    identity, client = ("id", "clientMsgId") if turns else ("msg_id", "client_msg_id")
    rows = [{"type": "user_msg", identity: "callback", client: "browser", "prompt": ""},
            {"type": "user_msg", identity: "human", "prompt": "Where?"}]
    aliases = CodexClientMessageAliases(native_messages={"human": "browser"},
                                        segments={("callback", 0): "browser"})
    result = WrapperMachine._overlay_codex_cached_client_aliases(rows, aliases, turns=turns)
    assert not result[0].get(client)
    assert result[1][client] == "browser"
    assert rows[0][client] == "browser", "repair must not mutate cached input rows"
