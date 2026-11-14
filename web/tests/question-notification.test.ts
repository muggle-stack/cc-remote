import assert from "node:assert/strict";
import { QuestionNotificationGate, questionNotificationPresentation } from "../src/question-notification.ts";
import { PROTOCOL_VERSION, type AskUser, type AssistantMsgEnd } from "../src/protocol.ts";

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

const asyncQuestion: AssistantMsgEnd = {
  v: PROTOCOL_VERSION, type: "assistant_msg_end", sid: "async-session", ts: 2,
  message_id: "question-item", delivery: "async", questions: [{ title: "private question" }],
  notification_context: { engine: "codex", space: "code", display_name: "Code session" },
};
assert.equal(gate.take("machine", asyncQuestion, view), true);
assert.equal(gate.take("machine", asyncQuestion, view), false, "repeated native items notify once");
assert.equal(gate.take("other-machine", asyncQuestion, view), true);
assert.equal(gate.take("machine", { ...asyncQuestion, message_id: "replay", to: "client" }, view), false);
assert.equal(gate.take("machine", { ...asyncQuestion, message_id: "history", notification_context: null }, view), false);
assert.equal(gate.take("machine", { ...asyncQuestion, message_id: "plain", questions: null }, view), false);
assert.equal(gate.take("machine", { ...asyncQuestion, message_id: "owner", owner_id: "owner" }, view), true,
  "owner-private side chats can use this authenticated page's local notification");
assert.equal(gate.take("machine", { ...asyncQuestion, message_id: "focused" }, {
  ...view, engine: "codex", sid: "async-session",
}), false);
const asyncGeneric = questionNotificationPresentation(asyncQuestion, "generic");
assert.equal(asyncGeneric.title, "Codex 想确认一下");
assert.doesNotMatch(JSON.stringify(asyncGeneric), /private|Code session|async-session/);
const asyncNamed = questionNotificationPresentation(asyncQuestion, "session");
assert.equal(asyncNamed.body, "Codex 想确认一下");
assert.equal(asyncNamed.sessionId, "async-session");
