import assert from "node:assert/strict";
import { parseSlash, slashToken } from "../src/data.ts";

for (const prompt of [
  "/Users/tester/workspace/unitree-go2这里有双目的，看看我们的大脑是否能接入？",
  "/Users/Tester/My Project 请检查这个目录",
  "/home/tester/project\n请检查这个目录",
  "/model/weights 检查模型文件",
  "/open/project",
  "//server/share/image.png",
  "/folder\\file.txt",
]) {
  assert.equal(parseSlash(prompt), null, "path-prefixed prompts must remain ordinary text");
  assert.equal(slashToken(prompt), null, "paths must not open the command palette");
}
assert.equal(slashToken("/"), "");
assert.equal(slashToken("/op"), "op");
assert.equal(slashToken("/open /Users/Tester/My Project"), null);
assert.equal(parseSlash("/"), null);
assert.deepEqual(parseSlash("/OPEN /Users/Tester/My Project"),
  { slash: "open", args: "/Users/Tester/My Project" });
assert.deepEqual(parseSlash("/plugin:review 检查修改\n保留接口"),
  { slash: "plugin:review", args: "检查修改\n保留接口" });
assert.deepEqual(parseSlash("/unknown-command"), { slash: "unknown-command", args: "" },
  "unknown command names must retain their existing engine-specific handling");

console.log("slash command path regressions passed");
