import { expect, test, type Page } from "@playwright/test";
import { PROTOCOL_VERSION, type DshState } from "../src/protocol";
import { staticPng } from "./fixtures/png";

const sid = "dsh@native-session";
const nativeState: Omit<DshState, "v" | "ts"> = { type: "dsh_state", sid, connected: true,
  agent_preset: "standard", permission: "read-only",
  permissions: [{ value: "read-only", name: "只读", description: "仅查看文件" },
    { value: "workspace-write", name: "工作区写入", description: "修改当前工作目录" }],
  commands: [{ name: "goal", description: "设置并管理目标", attachments: true },
    { name: "permission", description: "权限预设", attachments: false },
    { name: "compact", description: "压缩上下文", attachments: false }],
  goal: { id: "native-goal", revision: 2, objective: "完成 DSH 适配并验证手机界面", phase: "active",
    rounds: 3, max_rounds: 32, activation: "disarmed" },
};

async function mockDshRelay(page: Page, { running = false, commandSuccess = false } = {}) {
  const commands: Record<string, unknown>[] = [];
  const context = { type: "context_report", sid, total_tokens: 24000,
    max_tokens: 64000, percentage: 37.5, available: true,
    source: "recent_turn", categories: [] };
  let emit: (event: Record<string, unknown>) => void = () => {};
  await page.addInitScript(() => {
    localStorage.setItem("cc_remote_engine", "dsh");
    localStorage.setItem("cc_remote_machine", "dsh-machine");
  });
  await page.route("**/api/**", route => route.fulfill({ status: 404 }));
  await page.route("**/api/session", route => route.fulfill({ json: {} }));
  await page.route("**/api/viewers/pages", route => route.fulfill({ json: { pages: [] } }));
  await page.route("**/api/devices", route => route.fulfill({ json: {
    devices: [{ machine_id: "dsh-machine", label: "Development", online: true }],
  } }));
  await page.routeWebSocket(/\/ws(?:\?|$)/, socket => {
    emit = event => socket.send(JSON.stringify({ v: PROTOCOL_VERSION, ts: Date.now() / 1000, ...event }));
    const snapshot = () => {
      emit({ type: "snapshot", sid, cc_session_id: sid, state: running ? "running" : "idle",
        tail_text: "", cwd: "/tmp/dsh-test", generation: "dsh-generation" });
      emit(nativeState);
      emit({ type: "model", sid, model: "dsh:deepseek-official:deepseek-flash" });
      emit({ type: "effort", sid, effort: "off" });
      emit({ type: "perm", sid, mode: "read-only" });
      emit(context);
      emit({ type: "replay_end", sid, to_seq: 0, truncated: false });
    };
    socket.onMessage(raw => {
      const cmd = JSON.parse(String(raw)) as Record<string, unknown>;
      commands.push(cmd);
      if (cmd.type === "hello") snapshot();
      if (cmd.type === "get_models") emit({ type: "models", engine: cmd.engine, models: cmd.engine === "dsh" ? [
        { id: "dsh:deepseek-official:deepseek-flash", display_name: "DeepSeek-V41-Flash", efforts: ["off", "low"], default_effort: "low" },
      ] : [], default_model: "dsh:deepseek-official:deepseek-flash", default_effort: "low", dsh_presets: [
        { id: "standard", name: "标准", description: "文件、命令与交互工具", available: true, is_default: true },
        { id: "ptc", name: "代码编排", description: "通过代码组织工具调用", available: true, is_default: false },
      ] });
      if (cmd.type === "list_sessions") {
        // Match the real runtime: DSH has no Work catalog. A permissive mock
        // would hide unsupported background reads during a harness switch.
        if (cmd.engine === "dsh" && cmd.space === "work") emit({ type: "error",
          code: "dsh_unsupported", request_id: cmd.cmd_id, message: "DSH 目前支持 Code。" });
        else emit({ type: "session_list", engine: cmd.engine ?? "claude", space: cmd.space ?? "code", request_id: cmd.cmd_id,
          sessions: cmd.engine === "dsh" ? [{ session_id: sid, engine: "dsh", space: "code", cwd: "/tmp/dsh-test",
            summary: "DSH 原生会话", state: running ? "running" : "idle", last_modified: "100" }] : [] });
      }
      if (cmd.type === "switch_session") { emit({ type: "session_focus", session_id: cmd.session_id }); snapshot(); }
      if (cmd.type === "get_history") emit({ type: "history", sid, session_id: sid, revision: "dsh-history",
        generation: "dsh-generation", detail: "summary", events: [], turns: [{ id: "native-prompt", clientMsgId: "native-prompt",
          prompt: "验证图片、引导与原生控件", done: !running, forkPointId: "dsh-seq-5", blocks: [
            { kind: "text", message_id: "answer", text: "DSH 已准备好。", channel: "final", done: true },
          ] }], has_more: false });
      if (cmd.type === "set_dsh_control") emit({ type: "dsh_command_result", sid, request_id: cmd.cmd_id,
        status: commandSuccess ? "success" : "error", text: commandSuccess ? "命令已执行" : "当前目标不能替换，请使用 /goal edit" });
      if (cmd.type === "get_context") emit({ ...context, request_id: cmd.cmd_id });
      if (cmd.type === "get_engine_capabilities") {
        // Another browser may still own the wrapper's Codex focus. Only an
        // explicit DSH session target can safely read this native catalog.
        if (cmd.sid !== sid) emit({ type: "error", code: "dsh_invalid_session",
          request_id: cmd.cmd_id, message: "请选择 DSH 会话。" });
        else emit({ type: "engine_capabilities", sid, engine: "dsh", space: "code", cwd: "/tmp/dsh-test",
          request_id: cmd.cmd_id, skills_only: cmd.skills_only, items: [{ kind: "skill", id: "review", name: "review", description: "审查变更", enabled: true, actions: [] }] });
      }
      if (cmd.type === "ping") emit({ type: "pong", n: cmd.n });
      if (cmd.cmd_id) emit({ type: "command_ack", client_id: cmd.client_id, cmd_id: cmd.cmd_id });
    });
  });
  return { commands, emit: (event: Record<string, unknown>) => emit(event) };
}

async function openDsh(page: Page, options = {}) {
  const relay = await mockDshRelay(page, options);
  await page.goto("/");
  await expect(page.locator(".engine-toggle")).toHaveValue("dsh");
  await expect(page.locator(".composer textarea")).toBeVisible();
  await expect(page.getByText("验证图片、引导与原生控件", { exact: true })).toBeVisible();
  return relay;
}

for (const running of [false, true]) {
  test(`DSH path-prefixed prompts send verbatim as ${running ? "steering" : "a new message"}`, async ({ page }) => {
    const relay = await openDsh(page, { running });
    const input = page.locator(".composer textarea");
    const prompt = "/Users/Tester/workspace/unitree-go2这里有双目的，看看我们的大脑是否能接入？";
    await input.fill(prompt);
    if (running) await input.press("Enter");
    else await page.locator(".composer .sendbtn").click();
    await expect.poll(() => relay.commands.filter(command =>
      command.type === (running ? "steer" : "query") && command.prompt === prompt,
    ).length).toBe(1);
    await expect(input).toHaveValue("");
    await expect(page.locator(".composer-notice")).toHaveCount(0);
    expect(relay.commands.some(command => command.type === "set_dsh_control")).toBe(false);
  });
}

test("DSH context reports remain visible without a Codex estimate and clear unavailable readings", async ({ page }, info) => {
  const relay = await openDsh(page, { running: true });
  const ring = page.getByRole("button", { name: "上下文占用", exact: true });
  await ring.click();
  const popover = page.getByRole("dialog", { name: "上下文占用", exact: true });
  await expect(popover).toContainText("24,000 / 64,000 (38%)");
  await expect(popover.locator(".ctx-pop-bar i")).toHaveAttribute("style", "width: 37.5%;");
  await expect(popover.getByText("设置上下文上限", { exact: true })).toHaveCount(0);
  await expect(popover).not.toContainText("Codex");
  relay.emit({ type: "context_report", sid, source: "recent_turn", available: true,
    total_tokens: 6400, max_tokens: 64000, percentage: 10, categories: [] });
  await expect(popover).toContainText("6,400 / 64,000 (10%)");
  await expect(popover.locator(".ctx-pop-bar i")).toHaveAttribute("style", "width: 10%;");
  await page.screenshot({ path: info.outputPath("dsh-context.png"), animations: "disabled" });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  relay.emit({ type: "context_report", sid, source: "recent_turn", available: false,
    total_tokens: 0, max_tokens: 0, percentage: 0, categories: [] });
  await expect(popover.locator(".ctx-pop-nums")).toHaveCount(0);
  await expect(popover).toContainText("DSH 暂未返回上下文用量。");
  await expect(ring.locator(".hr-fill")).toHaveCount(0);
  expect(relay.commands.some(c => c.type === "query" || c.type === "steer")).toBe(false);
});

test("DSH native context shows small readings and updates after compaction", async ({ page }) => {
  const relay = await openDsh(page);
  await page.getByRole("button", { name: "上下文占用", exact: true }).click();
  const popover = page.getByRole("dialog", { name: "上下文占用", exact: true });
  relay.emit({ type: "context_report", sid, source: "native_estimate", available: true,
    total_tokens: 593, max_tokens: 1000000, percentage: .06, categories: [] });
  await expect(popover).toContainText("593 / 1,000,000 (<1%)");
  await expect(popover).toContainText("按 DSH 原生估算显示");
  relay.emit({ type: "context_report", sid, source: "native_estimate", available: true,
    total_tokens: 100, max_tokens: 1000000, percentage: .01, categories: [] });
  await expect(popover).toContainText("100 / 1,000,000 (<1%)");
  await expect(popover.locator(".ctx-pop-bar i")).toHaveAttribute("style", "width: 0.01%;");
  expect(relay.commands.some(c => ["query", "steer", "compact_session"].includes(String(c.type)))).toBe(false);
});

test("DSH harness switch fits its label and Work is visibly locked", async ({ page }) => {
  await openDsh(page);
  const selector = page.locator(".engine-selector");
  expect((await selector.boundingBox())!.width).toBeLessThan(65);
  await page.locator(".surface-head-title").click();
  const work = page.getByRole("tab", { name: "Work", exact: true });
  await expect(work).toBeDisabled();
  await expect(work.locator(".space-lock")).toBeVisible();
  await expect(work).toHaveAttribute("title", "DSH 暂不支持 Work");
  await page.locator(".s-head").getByRole("button", { name: "收起", exact: true }).click();
  await page.locator(".engine-toggle").selectOption("claude");
  await expect(page.locator(".engine-label")).toHaveText("✳ Claude");
  expect((await selector.boundingBox())!.width).toBeLessThan(100);
  await page.locator(".engine-toggle").selectOption("dsh");
  await expect(page.locator(".engine-label")).toHaveText("DSH");
  await expect(page.getByRole("tab", { name: "Code", exact: true })).toHaveAttribute("aria-selected", "true");
});

test("DSH Skills reads carry the selected session and show completion without a focus error", async ({ page }) => {
  const relay = await openDsh(page);
  await expect.poll(() => relay.commands.filter(cmd => cmd.type === "get_engine_capabilities").length).toBeGreaterThan(0);
  expect(relay.commands.filter(cmd => cmd.type === "get_engine_capabilities").every(cmd => cmd.sid === sid)).toBe(true);
  const composer = page.locator(".composer textarea");
  await composer.fill("$rev");
  await expect(page.getByText("审查变更", { exact: true })).toBeVisible();
  await expect(page.getByText("DSH 会话尚未选定或已失效，请重新选择会话。", { exact: true })).toHaveCount(0);
});

test("DSH harness selection clears pointer focus but retains the keyboard focus indicator", async ({ page }) => {
  await openDsh(page);
  const select = page.locator(".engine-toggle");
  await select.focus();
  await select.dispatchEvent("pointerdown", { pointerType: "touch" });
  await select.selectOption("codex");
  await expect(select).not.toBeFocused();
  await expect(page.locator(".engine-label")).toHaveCSS("outline-style", "none");
  await select.focus();
  await select.press("ArrowDown");
  await select.selectOption("dsh");
  await expect(select).toBeFocused();
  await expect(page.locator(".engine-label")).toHaveCSS("outline-style", "solid");
});

test("DSH completion spark replays on click and settles at rest", async ({ page }) => {
  await openDsh(page);
  const spark = page.locator(".turn-done-mark").getByRole("button", { name: "DSH", exact: true });
  await expect(spark).toBeVisible();
  await expect(page.locator(".turn-working")).toHaveCount(0);
  const resting = await spark.locator("svg").innerHTML();
  await page.clock.install();
  await spark.click();
  await expect.poll(() => spark.locator("svg").innerHTML()).not.toBe(resting);
  await page.clock.runFor(850);
  await expect.poll(() => spark.locator("svg").innerHTML()).toBe(resting);
  await spark.click();
  await expect.poll(() => spark.locator("svg").innerHTML()).not.toBe(resting);
});

test("DSH working spark animates while a native turn runs", async ({ page }) => {
  await page.clock.install();
  const relay = await openDsh(page, { running: true });
  relay.emit({ type: "turn_binding", sid, msg_id: "native-prompt", turn_id: "dsh-seq-5" });
  const working = page.locator(".turn-working svg");
  await expect(working).toBeVisible();
  await expect(page.locator(".turn-done-mark")).toHaveCount(0);
  const initial = await working.innerHTML();
  await page.clock.runFor(180);
  await expect.poll(() => working.innerHTML()).not.toBe(initial);
});

test("DSH model picker offers Flash only and preserves native reasoning levels", async ({ page }) => {
  await openDsh(page);
  await page.getByTitle("选择模型", { exact: true }).click();
  const models = page.getByRole("dialog", { name: "选择模型", exact: true });
  await expect(models.locator(".cmd-nm")).toHaveText(["DeepSeek-V41-Flash"]);
  await models.locator(".cmd").click();
  await page.getByTitle("思考强度", { exact: true }).click();
  await expect(page.getByRole("dialog", { name: "选择思考强度", exact: true }).locator(".cmd")).toHaveCount(2);
});

test("DSH native controls remain usable while running and preserve command drafts", async ({ page }) => {
  const relay = await openDsh(page, { running: true });
  await page.locator(".hint-mode").click();
  await page.getByText("工作区写入", { exact: true }).click();
  await expect.poll(() => relay.commands.some(c => c.type === "set_dsh_control" && c.kind === "permission" && c.value === "workspace-write")).toBe(true);
  const input = page.locator(".composer textarea");
  await input.press("Shift+Tab");
  await expect.poll(() => relay.commands.filter(c => c.type === "set_dsh_control" && c.kind === "permission").length).toBe(2);
  expect(relay.commands.filter(c => c.type === "set_dsh_control" && c.kind === "permission")
    .every(c => c.value === "workspace-write")).toBe(true);
  await input.fill("/goal 完成修复");
  await input.press("Enter");
  await expect(page.getByText("当前目标不能替换，请使用 /goal edit", { exact: true })).toBeVisible();
  await expect(input).toHaveValue("/goal 完成修复");
  expect(relay.commands.some(c => c.type === "get_goal" || c.type === "set_perm")).toBe(false);
  await input.fill("继续检查布局");
  await input.press("Enter");
  await expect.poll(() => relay.commands.some(c => c.type === "steer" && c.prompt === "继续检查布局")).toBe(true);
});

for (const theme of ["light", "dark"] as const) {
test(`DSH native progress distinguishes inactive continuation and fits the viewport (${theme})`, async ({ page }, info) => {
  if (info.project.name === "chromium") await page.setViewportSize({ width: 1440, height: 900 });
  await page.addInitScript(theme => localStorage.setItem("cc_remote_theme", theme), theme);
  const relay = await openDsh(page);
  const goal = page.locator(".dsh-goal");
  await expect(goal).toContainText("等待继续");
  await goal.locator("summary").click();
  await expect(goal).toContainText("3 / 32");
  await goal.getByRole("button", { name: "继续目标", exact: true }).click();
  await expect.poll(() => relay.commands.some(c => c.type === "set_dsh_control" && c.value === "/goal resume")).toBe(true);
  await page.screenshot({ path: info.outputPath("dsh-controls.png") });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  relay.emit({ ...nativeState, connected: false, error: "DSH 连接中断，正在重新连接。" });
  await expect(page.getByText("DSH 连接中断，正在重新连接。", { exact: true })).toBeVisible();
  await expect(goal.getByRole("button", { name: "继续目标", exact: true })).toBeDisabled();
});
}

test("DSH command attachments wait for acceptance before clearing", async ({ page }) => {
  const relay = await openDsh(page, { commandSuccess: true });
  const input = page.locator(".composer textarea");
  await page.locator('.composer input[type="file"][accept*="image"]').setInputFiles({
    name: "portrait.png", mimeType: "image/png", buffer: staticPng(90, 180),
  });
  await expect(page.locator(".composer .attach img")).toBeVisible();
  await input.fill("/goal 检查截图");
  await input.press("Enter");
  await expect.poll(() => relay.commands.some(c => c.type === "set_dsh_control" && c.kind === "command" && Array.isArray(c.images) && c.images.length === 1)).toBe(true);
  await expect(input).toHaveValue("");
  await expect(page.locator(".composer .attach")).toHaveCount(0);
});

test("DSH cold command admission and failed goal editing retain the user's text", async ({ page }) => {
  const relay = await openDsh(page);
  relay.emit({ ...nativeState, commands: [] });
  const input = page.locator(".composer textarea");
  await input.fill("/goal 冷会话目标");
  await input.press("Enter");
  await expect.poll(() => relay.commands.some(c => c.type === "set_dsh_control" && c.value === "/goal 冷会话目标")).toBe(true);
  await expect(input).toHaveValue("/goal 冷会话目标");
  const goal = page.locator(".dsh-goal");
  await goal.locator("summary").click();
  await goal.getByRole("button", { name: "编辑", exact: true }).click();
  await goal.getByLabel("编辑目标").fill("保存失败也保留这段文字");
  await goal.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("当前目标不能替换，请使用 /goal edit", { exact: true })).toBeVisible();
  await expect(goal.getByLabel("编辑目标")).toHaveValue("保存失败也保留这段文字");
});

test("DSH preset picker uses the native roster and carries the selected preset", async ({ page }) => {
  const relay = await openDsh(page);
  await page.locator(".surface-head-title").click();
  await page.getByRole("button", { name: "新会话", exact: true }).click();
  await page.getByLabel("DSH Agent Preset").selectOption("ptc");
  await expect(page.locator(".dsh-preset-row")).toContainText("通过代码组织工具调用");
  await page.locator(".newchat-input").fill("开始任务");
  await page.locator(".newchat-send").click();
  await expect.poll(() => relay.commands.some(c => c.type === "new_session" && c.engine === "dsh" && c.dsh_agent_preset === "ptc")).toBe(true);
  const command = relay.commands.find(c => c.type === "new_session")!;
  expect(command.claude_profile_id).toBeUndefined();
  expect(command.codex_profile_id).toBeUndefined();
});

test("DSH loading and harness switches only prefetch supported session surfaces", async ({ page }) => {
  const relay = await openDsh(page);
  await expect.poll(() => relay.commands.some(cmd => cmd.type === "get_context")).toBe(true);
  const dshLists = () => relay.commands.filter(cmd => cmd.type === "list_sessions" && cmd.engine === "dsh");
  expect(dshLists().length).toBeGreaterThan(0);
  expect(dshLists().map(cmd => cmd.space ?? "code")).not.toContain("work");
  await expect(page.locator(".banner")).toHaveCount(0);

  const selector = page.locator(".engine-toggle");
  for (const engine of ["codex", "claude"]) {
    const switches = relay.commands.filter(cmd => cmd.type === "switch_session").length;
    await selector.selectOption(engine);
    // Supported engines still warm Work; the DSH guard must not disable this.
    await expect.poll(() => relay.commands.some(cmd => cmd.type === "list_sessions"
      && (cmd.engine ?? "claude") === engine && cmd.space === "work")).toBe(true);
    await selector.selectOption("dsh");
    await expect.poll(() => relay.commands.filter(cmd => cmd.type === "switch_session").length).toBeGreaterThan(switches);
    await expect(page.getByText("验证图片、引导与原生控件", { exact: true })).toBeVisible();
    expect(dshLists().map(cmd => cmd.space ?? "code")).not.toContain("work");
    await expect(page.locator(".banner")).toHaveCount(0);
  }
});
