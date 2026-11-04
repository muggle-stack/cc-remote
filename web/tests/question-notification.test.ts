import assert from "node:assert/strict";
import { QuestionNotificationGate, questionNotificationPresentation } from "../src/question-notification.ts";
import { PROTOCOL_VERSION, type AskUser } from "../src/protocol.ts";

const ask: AskUser = {
  v: PROTOCOL_VERSION, type: "ask_user", sid: "profile@session", ask_id: "ask-1", ts: 1,
  question: "private question", options: [{ label: "private option" }], allow_text: true,
  notification_context: { engine: "claude", space: "code", display_name: " private\nsession\u0000 " },
};
const gate = new QuestionNotificationGate();
const view = { sid: "other", engine: "claude", space: "code", hidden: false };
assert.equal(gate.take("machine", ask, view), true, "visible tab on another session receives an alert");
assert.equal(gate.take("machine", ask, view), false, "duplicate live delivery stays silent");
assert.equal(gate.take("machine", { ...ask, ask_id: "replay", notification_context: null }, view), false);
assert.equal(gate.take("machine", { ...ask, ask_id: "focused" }, { ...view, sid: ask.sid! }), false);
assert.equal(gate.take("machine", { ...ask, ask_id: "hidden" }, { ...view, sid: ask.sid!, hidden: true }), true);
assert.equal(gate.take("machine", { ...ask, ask_id: "engine" }, { ...view, sid: ask.sid!, engine: "codex" }), true);
assert.equal(gate.take("other-machine", ask, view), true, "identical ids on other machines stay independent");
const generic = questionNotificationPresentation(ask, "generic");
assert.deepEqual(generic, { title: "cc-remote", body: "远程会话正在等待你的回答",
  sessionId: null, engine: null, space: null });
assert.doesNotMatch(JSON.stringify(generic), /private|profile@session/);
const named = questionNotificationPresentation(ask, "session");
assert.equal(named.title, "private session");
assert.equal(named.body, "Claude 正在等待你的回答");
assert.equal(named.sessionId, ask.sid);
assert.doesNotMatch(JSON.stringify(named), /private question|private option/);
assert.equal(questionNotificationPresentation({ ...ask, notification_context: {
  ...ask.notification_context!, parent_session_id: "parent",
} }, "session").sessionId, "parent");
console.log("question notification checks passed");
