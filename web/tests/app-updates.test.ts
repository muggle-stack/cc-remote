import assert from "node:assert/strict";
import { AppUpdateClient, appUpdateFeedback, newerVersion, parseAppUpdate, parseUpdateJob,
  UpdateIntentStore, updateReason, releaseHighlights, parseUpdateReceipt } from "../src/app-updates.ts";

assert(newerVersion("5.10.0", "5.9.99"));
for (const target of ["5.3.14", "5.3.13", "05.3.15", "5.3.15-beta", "$(id)"]) {
  assert.equal(newerVersion(target, "5.3.14"), false);
}
const job = { id: "a".repeat(32), version: "5.3.15", previous: "5.3.14", phase: "waiting", secret: "redacted" };
assert.equal(parseUpdateJob({ ...job, phase: "invalid" }), null);
assert.equal(parseUpdateJob({ ...job, id: "../../a" }), null);
assert(!("secret" in parseUpdateJob(job)!));
const state = parseAppUpdate({ managed: true, enabled: true, current: "5.3.14", busy_count: 2,
  latest: { version: "5.3.15", notes: "更新内容" }, job });
assert.equal(appUpdateFeedback(state)?.label, "等待更新");
assert.equal(appUpdateFeedback({ ...state, job: null })?.label, "有更新");
assert.equal(appUpdateFeedback({ ...state, latest: null, job: { ...state.job!, phase: "succeeded", updated_at: 1 } }, 500_000), undefined);
assert.match(updateReason("unknown secret"), /更新没有完成/);

const storage = new Map<string, string>();
const adapter = { getItem: (k: string) => storage.get(k) ?? null,
  setItem: (k: string, v: string) => { storage.set(k, v); }, removeItem: (k: string) => { storage.delete(k); } };
const ledger = new UpdateIntentStore("one", adapter);
ledger.save({ id: job.id, version: job.version });
assert.deepEqual(new UpdateIntentStore("one", adapter).read(), { id: job.id, version: job.version });
assert.equal(new UpdateIntentStore("two", adapter).read(), null);
ledger.clear(); assert.equal(ledger.read(), null);
ledger.save({ id: job.id, version: job.version, reason: "permission_required" });
assert.equal(ledger.read()?.reason, "permission_required");
adapter.setItem("cc_remote_ui_update:one", JSON.stringify({ id: [job.id], version: job.version }));
assert.equal(ledger.read(), null);
assert.deepEqual(parseUpdateReceipt({ job_id: job.id, receipt: "missing", job: null }, job), { status: "missing" });
assert.equal(parseUpdateReceipt({ job_id: job.id, receipt: "found", job }, job).status, "found");
for (const raw of [
  { job_id: "b".repeat(32), receipt: "missing", job: null },
  { job_id: job.id, receipt: "missing" },
  { job_id: job.id, receipt: "found", job: { ...job, version: "5.3.16" } },
  { job_id: job.id, receipt: "found", job: { ...job, id: "b".repeat(32) } },
  { job_id: job.id, receipt: "found", job: null },
]) assert.throws(() => parseUpdateReceipt(raw, job), { message: "outcome_pending" });
const client = new AppUpdateClient("machine/one", (async (url, init) => {
  assert.equal(url, "/api/updates/machine%2Fone");
  assert.equal(init?.credentials, "same-origin");
  assert.equal(init?.cache, "no-store");
  assert.equal(init?.method, "POST");
  assert.deepEqual(JSON.parse(String(init?.body)), { op: "start", job_id: job.id, version: job.version });
  return new Response(JSON.stringify({ ok: true, job }));
}) as typeof fetch);
assert.equal((await client.call({ op: "start", job_id: job.id, version: job.version })).ok, true);
client.close();
for (const [status, reason] of [[401, "unauthorized"], [403, "forbidden"], [404, "unsupported"]] as const) {
  const denied = new AppUpdateClient("one", (async () => new Response("", { status })) as typeof fetch);
  await assert.rejects(denied.call(), { message: reason });
  denied.close();
}
const offline = new AppUpdateClient("one", (async () => { throw new TypeError("offline"); }) as typeof fetch);
await assert.rejects(offline.call({ op: "start" }), { message: "outcome_pending" });
await assert.rejects(offline.call(), { message: "network" });
offline.close();
console.log("app updates: scope, retry receipts, parsing and state feedback passed");

assert.deepEqual(releaseHighlights("## 中文\n- **修复历史**。详细信息\n- [更新入口](https://example.com)\n## English\n- English detail"), ["修复历史", "更新入口"]);
assert.equal(parseAppUpdate({ ...state, maintenance: false }).maintenance, false);
assert.equal(parseAppUpdate({ ...state, maintenance: true }).maintenance, true);
