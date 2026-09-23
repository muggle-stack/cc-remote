import assert from "node:assert/strict";
import { MODELS, matchModelId, modelsFor, effortsFor, defaultEffortFor } from "../src/data.ts";
import { compatibleNewChatEffort, newChatEfforts, reconcileNewChatSelection } from "../src/new-chat-selection.ts";

for (const id of ["claude-fable-5-1", "claude-mythos-5-1"]) {
  assert.equal(matchModelId(`${id}[1m]`, "claude"), id);
}
assert.deepEqual(MODELS.map((model) => model.id), ["opus[1m]", "sonnet", "haiku"],
  "offline choices must follow native aliases instead of pinning versions");
assert.equal(matchModelId("claude-mythos-5", "claude"),
  "claude-mythos-5",
  "historical sessions must not be relabelled as Mythos 5.1");

const catalog = { claude: [
  { id: "claude-opus-5-5", display_name: "Opus", description: "Opus 5.5", efforts: ["high", "max"] },
  { id: "provider-new-model", display_name: "Company", description: "Native model", efforts: [] },
] };
assert.deepEqual(modelsFor("claude", catalog).map((m) => m.id),
  ["claude-opus-5-5", "provider-new-model"], "native catalog replaces curated choices");
assert.equal(modelsFor("claude", catalog)[0].ds, "Opus 5.5");
assert.equal(modelsFor("cc", catalog)[1].name, "Company");
assert.deepEqual(effortsFor("claude", "claude-opus-5-5[1m]", catalog).map((e) => e.id), ["high", "max"]);
assert.deepEqual(effortsFor("claude", "provider-new-model", catalog), []);
assert.equal(defaultEffortFor("claude", "provider-new-model", catalog), "model-default");
assert.equal(compatibleNewChatEffort("claude", "provider-new-model", "max", catalog, null), null);
assert.deepEqual(reconcileNewChatSelection("claude", "claude-opus-5-5", "max", catalog, null),
  { model: "claude-opus-5-5", effort: "max" });
assert.deepEqual(reconcileNewChatSelection("claude", "claude-opus-5[1m]", "max", catalog, null),
  { model: null, effort: null }, "unavailable fallback selections must clear after native discovery");

const workCatalog = { claude: [
  { id: "default", display_name: "Default", description: "Native default",
    is_default: true, efforts: ["low", "high"] },
] };
assert.deepEqual(newChatEfforts("claude", null, workCatalog).map((e) => e.id), ["low", "high"]);
assert.equal(compatibleNewChatEffort("claude", null, "max", workCatalog, null), null);
assert.equal(compatibleNewChatEffort("claude", null, "high", workCatalog, null), "high");
