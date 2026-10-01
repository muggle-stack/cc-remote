import assert from "node:assert/strict";
import { newChatSpeed, speedsFor, speedLabel } from "../src/codex-speed.ts";
import { createServer } from "vite";
import { PROTOCOL_VERSION, type ServerEvent } from "../src/protocol.ts";
import type { Catalog } from "../src/data.ts";

const catalog: Catalog = { codex: [
  { id: "astra", display_name: "Astra", description: "", efforts: [], service_tiers: [
    { id: "priority", name: "Fast", description: "2x speed" },
    { id: "ultrafast", name: "Ultrafast", description: "Higher usage" },
  ] },
  { id: "sol", display_name: "Sol", description: "", efforts: [], service_tiers: [
    { id: "priority", name: "Fast", description: "1.5x speed" },
  ] },
  { id: "standard-only", display_name: "Standard", description: "", efforts: [], service_tiers: [] },
] };
assert.deepEqual(speedsFor("astra", catalog).options.map((t) => t.id), ["default", "priority", "ultrafast"]);
assert.deepEqual(speedsFor("sol", catalog).options.map((t) => t.id), ["default", "priority"]);
assert.equal(speedsFor("sol", catalog).options[1].description, "1.5x speed");
assert.equal(speedsFor("standard-only", catalog).known, true);
assert.equal(speedsFor("missing", catalog).known, false);
assert.deepEqual(speedsFor("astra", {}).options.map((t) => t.id), ["default"],
  "an account without a catalog must not borrow paid tiers from another account");
assert.equal(newChatSpeed("ultrafast", "sol", catalog), "default");
assert.equal(newChatSpeed("ultrafast", "astra", catalog), "ultrafast");
assert.equal(newChatSpeed("fast", "sol", catalog), "priority");
assert.equal(speedLabel("ultrafast"), "Ultrafast");
assert.equal(speedLabel("priority"), "快速");
assert.equal(speedLabel(null), "速度读取中");

const harness = await createServer({
  root: process.cwd(), appType: "custom", logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});
try {
  const { createRuntime, initialState, reduce } = await harness.ssrLoadModule("/src/reducer.ts");
  let state = { ...initialState, focusedSid: "a",
    runtimes: { a: createRuntime(), b: createRuntime() } };
  for (const tier of ["ultrafast", "default", "ultrafast"]) {
    state = reduce(state, { type: "event", event: {
      type: "fast", v: PROTOCOL_VERSION, ts: 1, sid: "a", on: tier !== "default", tier,
    } as ServerEvent });
    assert.equal(state.runtimes.a.serviceTier, tier,
      "native updates and replay retain the exact tier, including return to an earlier tier");
    assert.equal(state.runtimes.b.serviceTier, null, "another session stays uninitialized");
  }
  state = reduce(state, { type: "event", event: {
    type: "fast", v: PROTOCOL_VERSION, ts: 1, sid: "b", on: true,
  } as ServerEvent });
  assert.equal(state.runtimes.b.serviceTier, "priority", "legacy fast frames retain compatibility");
  assert.equal(state.runtimes.a.serviceTier, "ultrafast");
} finally { await harness.close(); }
console.log("Codex speed catalog and per-session native state passed");
