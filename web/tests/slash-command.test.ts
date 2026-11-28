import assert from "node:assert/strict";
import { composePastePrompt } from "../src/composer-pastes.ts";
import { CODEX_PROMPTS, isGeminiGoalCommand, matchCommands, parseSlash, routeComposerInput, slashToken } from "../src/data.ts";

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
  for (const engine of ["claude", "codex"]) {
    assert.deepEqual(routeComposerInput(prompt, engine), { kind: "prompt", prompt });
  }
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

for (const engine of ["claude", "codex"]) {
  assert.deepEqual(routeComposerInput("/", engine), { kind: "empty" });
  assert.deepEqual(routeComposerInput(" /model opus ", engine),
    { kind: "client", slash: "model", args: "opus" });
  assert.deepEqual(routeComposerInput("/compact", engine),
    { kind: "client", slash: "compact", args: "" });
  assert.equal(routeComposerInput("/plan check code", engine, "work").kind, "blocked");
  assert.equal(routeComposerInput("/model", engine, "work").kind, "client");
  assert.deepEqual(routeComposerInput("$my-skill check code", engine),
    { kind: "prompt", prompt: "$my-skill check code" });
}
for (const command of ["/unknown-command", "/constructor", "/__proto__", "/toString"]) {
  assert.equal(routeComposerInput(command, "codex").kind, "blocked");
  assert.deepEqual(routeComposerInput(command, "claude"),
    { kind: "prompt", prompt: command }, "Claude native skill names must pass through");
}
assert.deepEqual(routeComposerInput("/init follow project conventions", "codex"),
  { kind: "prompt", prompt: `${CODEX_PROMPTS.init}\n\nfollow project conventions` });

for (const slash of ["model", "permissions", "context", "extensions"]) {
  assert.deepEqual(routeComposerInput(`/${slash} `, "antigravity"), { kind: "client", slash, args: "" });
  const commands = matchCommands(slash, "antigravity", "code",
    [{ name: slash, description: "Native command with the same name", attachments: false }]);
  assert.equal(commands.length, 1);
  assert.notEqual(commands[0].ic, "term");
}
assert.deepEqual(routeComposerInput("/plan check code", "antigravity"),
  { kind: "prompt", prompt: "/plan check code" });
assert.deepEqual(routeComposerInput(" /goal  ", "antigravity"), { kind: "client", slash: "goal", args: "" });
assert.deepEqual(routeComposerInput("/goal 修复预览\n桌面和手机端都通过", "antigravity"),
  { kind: "prompt", prompt: "/goal 修复预览\n桌面和手机端都通过" });
assert.deepEqual(matchCommands("go", "antigravity"), []);
assert.deepEqual(matchCommands("go", "antigravity", "code",
  [{ name: "goal", description: "Native goal", attachments: false }]).map(({ slash }) => slash), ["goal"]);
assert.equal(isGeminiGoalCommand("antigravity", "/goal/notes"), false);
assert.deepEqual(composePastePrompt([{ text: "完成条件" }], "/goal 修复预览", true),
  { ok: true, prompt: "/goal 修复预览\n\n完成条件" });

console.log("slash command path regressions passed");
