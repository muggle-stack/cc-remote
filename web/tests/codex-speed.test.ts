import assert from "node:assert/strict";
import { newChatSpeed, speedsFor, speedLabel } from "../src/codex-speed.ts";
import { createServer } from "vite";
import { PROTOCOL_VERSION, type ServerEvent } from "../src/protocol.ts";
import { modelsFor, effortsFor, type Catalog } from "../src/data.ts";
import { compatibleNewChatEffort } from "../src/new-chat-selection.ts";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

assert.deepEqual(modelsFor("codex"), [],
  "first paint waits for native discovery without static model options");
assert.deepEqual(modelsFor("codex", { codex: [] }), [],
  "an empty native response cannot restore a hardcoded catalog");
assert.deepEqual(modelsFor("codex", { claude: [{
  id: "other-account-model", display_name: "Other", description: "", efforts: [],
}] }), [], "a missing Codex catalog cannot borrow another engine's models");
const nativeCatalog: Catalog = { codex: [
  { id: "gpt-next", display_name: "Native next model", description: "Latest native description", efforts: [] },
  { id: "gpt-5.6-terra", display_name: "Native Terra", description: "Updated native Terra description", efforts: ["high"] },
  { id: "gpt-5.6-luna", display_name: "Native Luna", description: "Updated native Luna description",
    efforts: ["adaptive", "low"], default_effort: "adaptive",
    effort_descriptions: { adaptive: "Native adaptive reasoning", low: "Official low description" } },
] };
const nativeModels = modelsFor("codex", nativeCatalog);
assert.deepEqual(nativeModels.map(({ id, name, ds }) => ({ id, name, ds })),
  nativeCatalog.codex.map(({ id, display_name, description }) => ({
    id, name: display_name, ds: description,
  })), "names, descriptions and model order come from the native catalog");
assert.ok(nativeModels.every(model => model.ic === "cpu"),
  "model ids must not assign ranking icons");
assert.deepEqual(effortsFor("codex", "gpt-5.6-terra", nativeCatalog).map(item => item.id), ["high"]);
assert.deepEqual(effortsFor("codex", "gpt-next", nativeCatalog), []);
assert.deepEqual(effortsFor("codex", "gpt-5.6-terra"), [],
  "missing native metadata cannot invent reasoning capabilities");
assert.deepEqual(effortsFor("codex", "unlisted-model", nativeCatalog), []);
assert.deepEqual(effortsFor("codex", "gpt-5.6-luna", nativeCatalog), [
  { id: "adaptive", name: "adaptive", ds: "Native adaptive reasoning", ic: "gauge3" },
  { id: "low", name: "low", ds: "Official low description", ic: "gauge3" },
], "effort ids, order and descriptions come from native metadata");
assert.equal(effortsFor("codex", "gpt-5.6-terra", nativeCatalog)[0].ds, "",
  "older catalogs without descriptions must not restore local descriptions");
assert.equal(compatibleNewChatEffort("codex", "gpt-5.6-luna", "adaptive", nativeCatalog, null), "adaptive");
assert.equal(compatibleNewChatEffort("codex", "gpt-5.6-luna", "ultra", nativeCatalog, null), null,
  "switching models with an incompatible effort returns to native defaults");

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
  const { createRuntime, initialState, reduce, modelCatalogScopeKey } = await harness.ssrLoadModule("/src/reducer.ts");
  const { CommandSheet } = await harness.ssrLoadModule("/src/components/CommandSheet.tsx");
  const emptyMarkup = renderToStaticMarkup(createElement(CommandSheet, {
    open: true, kind: "models", engine: "codex", onClose: () => undefined,
    onRequestModels: () => undefined,
  }));
  assert.match(emptyMarkup, /暂未读取到模型列表/);
  assert.match(emptyMarkup, /重新读取模型列表/);
  assert.doesNotMatch(emptyMarkup, /GPT-|gpt-/);
  const nativeMarkup = renderToStaticMarkup(createElement(CommandSheet, {
    open: true, kind: "models", engine: "codex", onClose: () => undefined,
    catalog: nativeCatalog,
  }));
  assert.match(nativeMarkup, /Native Terra/);
  assert.match(nativeMarkup, /Updated native Terra description/);
  assert.doesNotMatch(nativeMarkup, /均衡|日常编码|GPT-5\.6 Terra/,
    "known ids use the same native display metadata as newly discovered models");
  const effortMarkup = renderToStaticMarkup(createElement(CommandSheet, {
    open: true, kind: "efforts", engine: "codex", onClose: () => undefined,
    currentModel: "gpt-5.6-luna", currentEffort: "adaptive", catalog: nativeCatalog,
  }));
  assert.match(effortMarkup, /Native adaptive reasoning/);
  assert.match(effortMarkup, /Official low description/);
  assert.doesNotMatch(effortMarkup, /更快|最深|极限|high|ultra/);
  const emptyEffortMarkup = renderToStaticMarkup(createElement(CommandSheet, {
    open: true, kind: "efforts", engine: "codex", onClose: () => undefined,
    onRequestModels: () => undefined, currentModel: "unknown",
  }));
  assert.match(emptyEffortMarkup, /暂未读取到当前模型的思考强度/);
  assert.match(emptyEffortMarkup, /重新读取模型列表/);

  let profileState = initialState;
  for (const profile of ["primary", "stack"]) {
    profileState = reduce(profileState, { type: "event", event: {
      type: "models", v: PROTOCOL_VERSION, ts: 1, engine: "codex",
      codex_profile_id: profile, default_model: profile, default_effort: "adaptive", models: [{
        id: profile, display_name: profile, description: "", efforts: [],
      }],
    } });
  }
  const failedRefresh = reduce(profileState, { type: "event", event: {
    type: "models", v: PROTOCOL_VERSION, ts: 1, engine: "codex", codex_profile_id: "stack", models: [],
  } });
  assert.deepEqual(failedRefresh.catalog, profileState.catalog,
    "a failed model refresh keeps each account's last native catalog");
  assert.deepEqual(failedRefresh.catalogDefaultEffort, profileState.catalogDefaultEffort);
  const refreshedDefaults = reduce(failedRefresh, { type: "event", event: {
    type: "models", v: PROTOCOL_VERSION, ts: 1, engine: "codex", codex_profile_id: "stack",
    default_model: "stack", models: [{
      id: "stack", display_name: "Stack", description: "", efforts: [],
    }],
  } });
  assert.equal(refreshedDefaults.catalogDefaultEffort[modelCatalogScopeKey("codex", "stack")], undefined,
    "a successful native refresh clears a removed default effort");
  assert.equal(refreshedDefaults.catalogDefaultEffort[modelCatalogScopeKey("codex", "primary")], "adaptive",
    "refreshing one account does not change another account's default");
  const emptyAccount = reduce(failedRefresh, { type: "event", event: {
    type: "models", v: PROTOCOL_VERSION, ts: 1, engine: "codex", codex_profile_id: "new-account", models: [],
  } });
  assert.deepEqual(modelsFor("codex", {
    codex: emptyAccount.catalog[modelCatalogScopeKey("codex", "new-account")] ?? [],
  }), [], "an uninitialized account never borrows another account's model list");
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
console.log("Codex native model catalog, speed options and per-session state passed");
