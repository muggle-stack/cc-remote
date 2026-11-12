import assert from "node:assert/strict";
import {
  editRoomMentions,
  mergeRoomMessages,
  readRoomDraft,
  roomCatalogKey,
  roomMemberIdentity,
  roomMemberDescription,
  roomMentionMembers,
  shortRoomSessionId,
  resolveRoomFile,
  refreshRoomMentionNames,
} from "../src/collaboration.ts";

const roomMembers: import("../src/protocol").CollaborationMember[] = [
  { id: "implementation", room_id: "room", name: "代码实现", spec: { engine: "codex", profile_id: "primary" },
    sid: "session-one", native_sid: "01a0d871-one", cwd: "/repo/main", removed: false },
  { id: "review", room_id: "room", name: "交叉审查", spec: { engine: "codex", profile_id: "primary" },
    sid: "session-two", native_sid: "01a0d15f-two", cwd: "/repo/review", removed: false },
  { id: "removed", room_id: "room", name: "已移出", spec: { engine: "codex", profile_id: "primary" },
    sid: "session-three", native_sid: "three", cwd: "/repo", removed: true },
];
assert.equal(roomMemberIdentity(roomMembers[0]), "Codex · primary");
assert.equal(roomMemberDescription(roomMembers[1]), "Codex · primary · repo/review · 01a0d15f");
assert.deepEqual(roomMentionMembers(roomMembers, "primary").map(member => member.id), ["implementation", "review"]);
assert.deepEqual(roomMentionMembers(roomMembers, "01a0d15f").map(member => member.id), ["review"]);
assert.deepEqual(roomMentionMembers(roomMembers, "/repo/main").map(member => member.id), ["implementation"]);
assert.deepEqual(roomMentionMembers(roomMembers, "交叉").map(member => member.id), ["review"]);
const unbound = { ...roomMembers[0], cwd: null, native_sid: null, sid: null,
  spec: { ...roomMembers[0].spec, cwd: "/repo/not-started" } };
assert.equal(roomMemberDescription(unbound), "Codex · primary · repo/not-started");
assert.deepEqual(roomMentionMembers([unbound], "not-started").map(member => member.id), ["implementation"]);
const dshMember = { ...roomMembers[0], name: "资料核对", spec: { engine: "dsh" as const },
  sid: "dsh@session-711d960d-3a17-491f-8b66-32b3a6e0a946",
  native_sid: "session-711d960d-3a17-491f-8b66-32b3a6e0a946" };
assert.equal(shortRoomSessionId(dshMember.native_sid), "711d960d");
assert.equal(shortRoomSessionId(dshMember.sid), "711d960d");
assert.equal(shortRoomSessionId("dsh@session-68a304ab-22b0-40d9-9edb-b553c3a97b77"), "68a304ab");
assert.equal(shortRoomSessionId("codex@iris@01a0d871-one"), "01a0d871");
assert.equal(shortRoomSessionId("58c51dea-1111-4111-8111-111111111111"), "58c51dea");
assert.equal(shortRoomSessionId(null), undefined);
assert.equal(shortRoomSessionId(undefined), undefined);
assert.equal(roomMemberDescription(dshMember), "DSH · default · repo/main · 711d960d");
assert.equal(resolveRoomFile(roomMembers[0], "design/note.md"), "/repo/main/design/note.md");
assert.equal(resolveRoomFile(roomMembers[1], "../audio.wav"), "/repo/audio.wav");
assert.equal(resolveRoomFile(unbound, "note.md"), "/repo/not-started/note.md");
assert.equal(resolveRoomFile(roomMembers[0], "/tmp/full.mp4"), "/tmp/full.mp4");
assert.equal(resolveRoomFile(roomMembers[0], "~/note.md"), "~/note.md");
const renamedDraft = refreshRoomMentionNames("@代码实现 请核对 @交叉审查", [
  { start: 10, end: 15, memberIds: ["review"] },
  { start: 0, end: 5, memberIds: ["implementation"] },
], [{ ...roomMembers[0], name: "新的实现会话" }, { ...roomMembers[1], name: "审查" }]);
assert.equal(renamedDraft.text, "@新的实现会话 请核对 @审查");
assert.deepEqual(renamedDraft.mentions.map(mention => renamedDraft.text.slice(mention.start, mention.end)), ["@新的实现会话", "@审查"]);
assert.equal(refreshRoomMentionNames("@所有参与者", [{ start: 0, end: 6, memberIds: ["implementation"] }], roomMembers).text, "@所有参与者");
assert.deepEqual(roomMentionMembers([dshMember], "711d960d").map(member => member.id), ["implementation"]);

const text = "@Claude · default 看看方案";
const mentions = [{ start: 0, end: 17, memberIds: ["claude-default"] }];
assert.deepEqual(editRoomMentions(text, text + "。", mentions), mentions);
assert.deepEqual(editRoomMentions(text, "请 " + text, mentions), [
  { start: 2, end: 19, memberIds: ["claude-default"] },
]);
assert.deepEqual(
  editRoomMentions(text, text.replace("Claude", "Codex"), mentions),
  [],
);
assert.deepEqual(editRoomMentions(text, "看看方案", mentions), []);
assert.deepEqual(readRoomDraft(JSON.stringify({ text, mentions })), {
  text,
  mentions,
});
assert.deepEqual(readRoomDraft("invalid"), { text: "", mentions: [] });
assert.deepEqual(
  readRoomDraft(
    JSON.stringify({
      text,
      mentions,
      messageId: "pending-1",
      attachmentCount: 2,
    }),
  ),
  { text, mentions, messageId: "pending-1", attachmentCount: 2 },
);
assert.deepEqual(
  readRoomDraft(
    JSON.stringify({
      text,
      mentions,
      messageId: "invalid/id",
      attachmentCount: -1,
    }),
  ),
  { text, mentions },
);
assert.deepEqual(
  readRoomDraft(
    JSON.stringify({ text, mentions: [{ start: -1, end: 1, memberIds: [] }] }),
  ),
  { text, mentions: [] },
);
assert.notEqual(
  roomCatalogKey({ engine: "codex", profile_id: "iris", cwd: "/repo/a" }),
  roomCatalogKey({ engine: "codex", profile_id: "iris", cwd: "/repo/b" }),
);
const first = {
  id: "first",
  seq: 1,
} as import("../src/protocol").CollaborationMessage;
const second = {
  id: "second",
  seq: 2,
  text: "old",
} as import("../src/protocol").CollaborationMessage;
assert.deepEqual(
  mergeRoomMessages([second], [first, { ...second, text: "updated" }]),
  [first, { ...second, text: "updated" }],
);
assert.notEqual(
  roomCatalogKey({ engine: "codex", profile_id: "iris" }),
  roomCatalogKey({ engine: "codex", profile_id: "default" }),
);
console.log(
  "Collaboration mentions, draft restoration and native catalog scopes passed",
);

const { allowedDuringUpdate } = await import("../src/update-maintenance.ts");
for (const operation of [
  "read",
  "list",
  "catalog",
  "sessions",
  "reconcile",
  "stop",
  "answer",
]) {
  assert.equal(allowedDuringUpdate("collaboration", operation), true);
}
for (const operation of ["send", "create", "add_member", "update", "dismiss", "restore_member", "rename_member"]) {
  assert.equal(allowedDuringUpdate("collaboration", operation), false);
}
