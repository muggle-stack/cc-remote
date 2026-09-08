import assert from "node:assert/strict";
import { TurnFilePageRequests } from "../src/turn-file-pages.ts";
import { PROTOCOL_VERSION, type ServerEvent, type TurnFileChangesPage } from "../src/protocol.ts";

const scope = { sid: "profile@session", engine: "codex" as const, turnId: "turn", revision: "version", offset: 64 };
const page: TurnFileChangesPage = {
  v: PROTOCOL_VERSION, ts: 1, type: "turn_file_changes_page", sid: scope.sid,
  engine: "codex", turn_id: scope.turnId, revision: scope.revision,
  request_id: "request", offset: 64, total_files: 65, next_offset: null,
  files: [{ path: "src/last.py", state: "available", additions: 1, deletions: 1 }],
};
const requests = new TurnFilePageRequests();
const first = requests.request(scope, () => "request", new AbortController().signal);
let settled = false;
void first.then(() => { settled = true; });
for (const changes of [{ sid: "other@session" }, { engine: "claude" }, { revision: "next-version" },
  { turn_id: "other-turn" }, { offset: 0 }, { request_id: "old-request" }]) {
  requests.accept({ ...page, ...changes } as ServerEvent);
}
await Promise.resolve();
assert.equal(settled, false, "responses cannot use current focus or another version as ownership");
assert.equal(requests.accept(page), true);
assert.deepEqual(await first, page);
assert.equal(requests.accept(page), true, "duplicate one-shot results are swallowed, not reducer events");

const invalid = requests.request(scope, () => "invalid", new AbortController().signal);
requests.accept({ ...page, request_id: "invalid", next_offset: 64 });
await assert.rejects(invalid, /不完整/);
const failed = requests.request(scope, () => "failed", new AbortController().signal);
requests.accept({ v: PROTOCOL_VERSION, ts: 1, type: "error", code: "internal", sid: scope.sid,
  request_id: "failed", message: "该版本暂不可用" });
await assert.rejects(failed, /该版本暂不可用/);
const retry = requests.request(scope, () => "retry", new AbortController().signal);
requests.accept({ ...page, request_id: "retry" });
assert.deepEqual((await retry).files, page.files);

const abort = new AbortController();
const cancelled = requests.request(scope, () => "cancelled", abort.signal);
abort.abort();
await assert.rejects(cancelled, /取消/);
assert.equal(requests.accept({ ...page, request_id: "cancelled" }), true);
const disconnected = requests.request(scope, () => "disconnected", new AbortController().signal);
requests.clear();
await assert.rejects(disconnected, /连接已更新/);
const timed = new TurnFilePageRequests(5);
await assert.rejects(timed.request(scope, () => "timeout", new AbortController().signal), /超时/);
await assert.rejects(requests.request(scope, () => null, new AbortController().signal), /连接暂不可用/);
await assert.rejects(requests.request(scope, () => { assert.fail("aborted reads cannot send"); }, abort.signal));
