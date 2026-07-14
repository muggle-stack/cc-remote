import { strict as assert } from "node:assert";
import { accountStatsNote } from "../src/status-capabilities.js";

assert.equal(accountStatsNote({
  auth_type: "apiKey", requires_openai_auth: true,
}), "当前 API Key 模式不提供 ChatGPT 订阅限额和账户用量。");

assert.equal(accountStatsNote({
  auth_type: "amazonBedrock", requires_openai_auth: false,
}), "当前 Amazon Bedrock 模式不提供 ChatGPT 订阅限额和账户用量。");

assert.equal(accountStatsNote({
  auth_type: "chatgpt", plan_type: "pro", requires_openai_auth: false,
}), null);
assert.equal(accountStatsNote(null), null);

console.log("status capability tests passed");
