import assert from "node:assert/strict";
import { displayCommand, readableToolInput, readableToolOutput } from "../src/tool-details.ts";

assert.equal(displayCommand("/bin/zsh -lc 'rg -n needle src'"), "rg -n needle src");
assert.equal(displayCommand(["/bin/bash", "-lc", "printf '%s' hello"]), "printf '%s' hello");
assert.equal(displayCommand("zsh -lc 'echo' 'extra argument'"), "zsh -lc 'echo' 'extra argument'");
assert.equal(displayCommand("echo $(secret)"), "echo $(secret)");
assert.deepEqual(readableToolInput({ pattern: "needle", path: "src", glob: "*.ts" }), [
  { label: "文件", text: "src" }, { label: "搜索内容", text: "needle" },
  { label: "搜索范围", text: "*.ts" },
]);
const wrapped = JSON.stringify([{ i: 0, status: "fulfilled", value: {
  chunk_id: "chunk", output: "src/main.ts:12: matched", exit_code: 0,
} }, { i: 1, status: "fulfilled", value: {
  output: "test failed", exit_code: 1,
} }]);
assert.deepEqual(readableToolOutput(wrapped), {
  text: "src/main.ts:12: matched\n\ntest failed\n退出码：1", unwrapped: true,
});
assert.equal(readableToolOutput(JSON.stringify({ content: [
  { type: "text", text: JSON.stringify({ output: "2 passed", wall_time_seconds: 0.3 }) },
] })).text, "2 passed");
const business = { output: "business field", customer: "keep me" };
assert.deepEqual(JSON.parse(readableToolOutput(JSON.stringify(business)).text), business);
assert.equal(readableToolOutput(JSON.stringify(business)).unwrapped, false);
assert.deepEqual(readableToolOutput("plain\n<script>not HTML</script>"), {
  text: "plain\n<script>not HTML</script>", unwrapped: false,
});
const large = "x".repeat(600_000);
assert.equal(readableToolOutput(large).text, large);
console.log("tool detail presentation passed");
