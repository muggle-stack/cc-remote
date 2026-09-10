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
      emit({ type: "model", sid, model: "dsh:local:deepseek-flash" });
      emit({ type: "effort", sid, effort: "off" });
      emit({ type: "perm", sid, mode: "read-only" });
      emit({ type: "context_report", sid, total_tokens: 24000, max_tokens: 64000, percentage: 37.5, available: true, source: "recent_turn" });
      emit({ type: "replay_end", sid, to_seq: 0, truncated: false });
    };
    socket.onMessage(raw => {
      const cmd = JSON.parse(String(raw)) as Record<string, unknown>;
      commands.push(cmd);
      if (cmd.type === "hello") snapshot();
      if (cmd.type === "get_models") emit({ type: "models", engine: cmd.engine, models: cmd.engine === "dsh" ? [
        { id: "dsh:local:deepseek-flash", display_name: "DeepSeek Flash", efforts: ["off", "low"], default_effort: "low" },
      ] : [], default_model: "dsh:local:deepseek-flash", default_effort: "low", dsh_presets: [
        { id: "standard", name: "标准", description: "文件、命令与交互工具", available: true, is_default: true },
        { id: "ptc", name: "代码编排", description: "通过代码组织工具调用", available: true, is_default: false },
      ] });
      if (cmd.type === "list_sessions") emit({ type: "session_list", engine: cmd.engine, space: "code", request_id: cmd.cmd_id,
        sessions: cmd.engine === "dsh" ? [{ session_id: sid, engine: "dsh", space: "code", cwd: "/tmp/dsh-test",
          summary: "DSH 原生会话", state: running ? "running" : "idle", last_modified: "100" }] : [] });
      if (cmd.type === "switch_session") { emit({ type: "session_focus", session_id: cmd.session_id }); snapshot(); }
      if (cmd.type === "get_history") emit({ type: "history", sid, session_id: sid, revision: "dsh-history",
        generation: "dsh-generation", detail: "summary", events: [], turns: [{ id: "native-prompt", clientMsgId: "native-prompt",
          prompt: "验证图片、引导与原生控件", done: !running, forkPointId: "dsh-seq-5", blocks: [
            { kind: "text", message_id: "answer", text: "DSH 已准备好。", channel: "final", done: true },
          ] }], has_more: false });
      if (cmd.type === "set_dsh_control") emit({ type: "dsh_command_result", sid, request_id: cmd.cmd_id,
        status: commandSuccess ? "success" : "error", text: commandSuccess ? "命令已执行" : "当前目标不能替换，请使用 /goal edit" });
      if (cmd.type === "get_engine_capabilities") emit({ type: "engine_capabilities", sid, engine: "dsh", space: "code", cwd: "/tmp/dsh-test",
        request_id: cmd.cmd_id, skills_only: cmd.skills_only, items: [{ kind: "skill", id: "review", name: "review", description: "审查变更", enabled: true, actions: [] }] });
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
  return relay;
}

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
