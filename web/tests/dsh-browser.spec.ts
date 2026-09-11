import { expect, test, type Page } from "@playwright/test";
import { PROTOCOL_VERSION, type DshState } from "../src/protocol";
import { staticPng } from "./fixtures/png";

const sid = "dsh@native-session";
const nativeState: Omit<DshState, "v" | "ts"> = { type: "dsh_state", sid, connected: true,
  agent_preset: "standard", permission: "read-only",
  permissions: [{ value: "read-only", name: "只读", description: "仅查看文件" },
    { value: "workspace-write", name: "工作区写入", description: "修改当前工作目录" }],
  commands: [{ name: "goal", description: "设置并管理目标", attachments: true },
    { name: "plan", description: "进入计划模式；/plan off 退出", attachments: true },
    { name: "permission", description: "权限预设", attachments: false },
    { name: "compact", description: "压缩上下文", attachments: false }],
  goal: { id: "native-goal", revision: 2, objective: "完成 DSH 适配并验证手机界面", phase: "active",
    rounds: 3, max_rounds: 32, activation: "disarmed" },
};

async function mockDshRelay(page: Page, { running = false, commandSuccess = false, features = false } = {}) {
  const commands: Record<string, unknown>[] = [];
  let archived = false;
  let currentGoal = nativeState.goal;
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
    emit = event => {
      if (event.type === "dsh_state" && "goal" in event) currentGoal = event.goal as DshState["goal"];
      socket.send(JSON.stringify({ v: PROTOCOL_VERSION, ts: Date.now() / 1000, ...event }));
    };
    const snapshot = () => {
      emit({ type: "snapshot", sid, cc_session_id: sid, state: running ? "running" : "idle",
        tail_text: "", cwd: "/tmp/dsh-test", generation: "dsh-generation" });
      emit({ ...nativeState, ...(features ? { plan_active: false, plan_pending: false, jobs: [{ id: "job-1", label: "构建任务", status: "running", detail: "正在编译" }] } : {}) });
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
            summary: "DSH 原生会话", state: running ? "running" : "idle", last_modified: "100", tag: archived ? "archived" : null }] : [] });
      }
      if (features && cmd.type === "archive_session") {
        archived = true;
        emit({ type: "session_list_invalidated", engine: "dsh" });
      }
      if (cmd.type === "switch_session") { emit({ type: "session_focus", session_id: cmd.session_id }); snapshot(); }
      if (cmd.type === "get_history") emit({ type: "history", sid, session_id: sid, revision: "dsh-history",
        generation: "dsh-generation", detail: "summary", events: [], turns: [{ id: "native-prompt", clientMsgId: "native-prompt",
          prompt: "验证图片、引导与原生控件", done: !running, forkPointId: "dsh-seq-5", blocks: [
            ...(features ? [{ kind: "tool", message_id: "present", tool_use_id: "present-file", tool: "present",
              input: { files: [{ path: "/tmp/dsh-test/report.xlsx", description: "路线图" }] },
              result: { content: "Presented", is_error: false }, done: true }] : []),
            { kind: "text", message_id: "answer", text: features ? "[项目目录](/tmp/dsh-test/dir) 和 [文件行号](/tmp/dsh-test/README.md:7)" : "DSH 已准备好。", channel: "final", done: true },
          ] }], has_more: false });
      if (cmd.type === "set_dsh_control") emit({ type: "dsh_command_result", sid, request_id: cmd.cmd_id,
        status: commandSuccess ? "success" : "error", text: commandSuccess ? "命令已执行" : "当前目标不能替换，请使用 /goal edit" });
      if (cmd.type === "get_context") emit({ ...context, request_id: cmd.cmd_id });
      if (cmd.type === "act_dsh_goal") {
        if (commandSuccess) {
          const next = cmd.action === "clear" ? null : cmd.action === "create" ? {
            id: "created-goal", revision: 1, objective: String(cmd.objective), phase: "active" as const,
            rounds: 0, max_rounds: Number(cmd.max_rounds), activation: "armed" as const,
          } : { ...currentGoal!, revision: currentGoal!.revision + 1,
            ...(cmd.action === "edit" ? { objective: String(cmd.objective ?? currentGoal!.objective), max_rounds: Number(cmd.max_rounds ?? currentGoal!.max_rounds) }
              : { phase: cmd.action === "resume" ? "active" : cmd.action === "pause" ? "paused" : "complete",
                activation: cmd.action === "resume" ? "armed" : "disarmed" }),
          };
          emit({ ...nativeState, goal: next });
        }
        emit({ type: "dsh_command_result", sid: cmd.sid, request_id: cmd.cmd_id,
          status: commandSuccess ? "success" : "error", text: commandSuccess ? "" : "目标已更新，请重新打开编辑后提交。" });
      }
      if (cmd.type === "get_engine_capabilities") {
        // Another browser may still own the wrapper's Codex focus. Only an
        // explicit DSH session target can safely read this native catalog.
        if (cmd.sid !== sid) emit({ type: "error", code: "dsh_invalid_session",
          request_id: cmd.cmd_id, message: "请选择 DSH 会话。" });
        else emit({ type: "engine_capabilities", sid, engine: "dsh", space: "code", cwd: "/tmp/dsh-test",
          request_id: cmd.cmd_id, skills_only: cmd.skills_only, items: [{ kind: "skill", id: "review", name: "review", description: "审查变更", enabled: true, actions: [] }] });
      }
      if (features && cmd.type === "read_dsh") {
        const items = cmd.kind === "search" ? [{ id: "hit", sid, title: "DSH 原生会话", detail: `匹配 ${cmd.query}` }]
          : cmd.kind === "references" ? [
            { id: "file", title: "report notes.md", path: "report notes.md", state: "file", mention: '@"report notes.md"' },
            { id: "session", sid, title: "相关会话", state: "session", mention: "@[相关会话](dsh-session:native-session)" }]
          : cmd.kind === "subagents" ? [{ id: "child", sid: "dsh@child", title: "检查构建", detail: "", mode: "continuable", state: "running", has_children: true, controllable: true }]
          : cmd.kind === "conversation" ? [{ id: "message", title: "DSH", detail: `已经检查构建 ${cmd.query ?? ""}`, state: "message" }]
          : cmd.kind === "deliverables" ? [{ id: "file", title: "report.xlsx", path: "/tmp/dsh-test/report.xlsx", detail: "交付文件" }]
          : [{ id: "version", title: "DSH 版本", detail: "0.1.5-rc.2", state: "active" }];
        emit({ type: "dsh_read_result", sid: cmd.sid, request_id: cmd.cmd_id, kind: cmd.kind, items, available: true, has_more: false });
      }
      if (features && cmd.type === "act_dsh_subagent") emit({ type: "dsh_command_result", sid: cmd.sid,
        request_id: cmd.cmd_id, status: "success", text: cmd.action === "stop" ? "已请求停止子代理" : "已加入子代理队列" });
      if (features && cmd.type === "download_dsh") emit({ type: "dsh_download_chunk", sid: cmd.sid, request_id: cmd.cmd_id,
        export_id: cmd.export_id ?? cmd.cmd_id, offset: 0, total: cmd.cancel ? 0 : 4, data: cmd.cancel ? "" : "UEsDBA==", done: true });
      if (features && cmd.type === "get_file_preview") {
        if (String(cmd.path).endsWith("/dir")) emit({ type: "file_preview", sid: cmd.sid, request_id: cmd.request_id, path: cmd.path, directory: true, format: "text", content: "", size: 0 });
        else if (String(cmd.path).endsWith(".xlsx")) emit({ type: "file_preview", sid: cmd.sid, request_id: cmd.request_id, path: cmd.path,
          format: "spreadsheet", content: JSON.stringify({ sheets: [
            { name: "路线图", cells: [{ r: 1, c: 1, v: "本周进展" }, { r: 1, c: 2, v: "42", f: "SUM(B2:B3)" }], truncated: false },
            { name: "进展", cells: [{ r: 1, c: 1, v: "<script>bad()</script>" }], truncated: false }], truncated: false }),
          data: "UEsDBA==", size: 4, mtime_ns: "1", truncated: false });
        else emit({ type: "file_preview", sid: cmd.sid, request_id: cmd.request_id, path: cmd.path,
          format: "text", content: "first\nsecond\nthird\nfourth\nfifth\nsixth\nseventh", size: 42, mtime_ns: "1", truncated: false });
      }
      if (features && cmd.type === "browse_files") emit({ type: "files_listed", sid: cmd.sid, request_id: cmd.request_id,
        root: "/tmp/dsh-test", path: cmd.path, parent: "/tmp/dsh-test", kind: "directory", revision: "dir-1", next_offset: null,
        entries: [{ name: "README.md", path: "/tmp/dsh-test/dir/README.md", kind: "file" }] });
      if (cmd.type === "ping") emit({ type: "pong", n: cmd.n });
      if (cmd.cmd_id) emit({ type: "command_ack", client_id: cmd.client_id, cmd_id: cmd.cmd_id });
    });
  });
  return { commands, emit: (event: Record<string, unknown>) => emit(event) };
}

async function openDsh(page: Page, options = {}) {
  const relay = await mockDshRelay(page, options);
  await page.goto("/");
  await expect(page.locator(".engine-label")).toHaveText("DSH");
  await expect(page.locator(".composer textarea")).toBeVisible();
  await expect(page.getByText("验证图片、引导与原生控件", { exact: true })).toBeVisible();
  return relay;
}

async function chooseEngine(page: Page, engine: string) {
  await page.locator(".engine-toggle").click();
  await page.getByRole("menuitemradio", { name: engine === "dsh" ? "DSH" : engine === "codex" ? "Codex" : "Claude", exact: true }).click();
  await expect(page.getByRole("menu", { name: "会话引擎" })).toHaveCount(0);
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

test("DSH context reports retain their last reading during temporary failures", async ({ page }, info) => {
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
  await expect(popover).toContainText("6,400 / 64,000 (10%)");
  await expect(popover.locator(".ctx-pop-status")).toHaveCount(0);
  await expect(ring.locator(".hr-fill")).toHaveCount(1);
  await expect(ring.locator("text")).toHaveCount(0);
  relay.emit({ type: "model", sid, model: "different-model" });
  await expect(popover).not.toContainText("6,400");
  await expect(popover.locator(".ctx-pop-nums")).toHaveText("—");
  await expect(ring.locator(".hr-fill")).toHaveCount(0);
  await expect(ring.locator("text")).toHaveCount(0);
  expect(relay.commands.some(c => c.type === "query" || c.type === "steer")).toBe(false);
});

test("DSH native context shows small readings and updates after compaction", async ({ page }) => {
  const relay = await openDsh(page);
  await page.getByRole("button", { name: "上下文占用", exact: true }).click();
  const popover = page.getByRole("dialog", { name: "上下文占用", exact: true });
  relay.emit({ type: "context_report", sid, source: "native_estimate", available: true,
    total_tokens: 593, max_tokens: 1000000, percentage: .06, categories: [] });
  await expect(popover).toContainText("593 / 1,000,000 (<1%)");
  await expect(popover.locator(".ctx-pop-status")).toHaveCount(0);
  relay.emit({ type: "context_report", sid, source: "native_estimate", available: true,
    total_tokens: 100, max_tokens: 1000000, percentage: .01, categories: [] });
  await expect(popover).toContainText("100 / 1,000,000 (<1%)");
  await expect(popover.locator(".ctx-pop-bar i")).toHaveAttribute("style", "width: 0.01%;");
  expect(relay.commands.some(c => ["query", "steer", "compact_session"].includes(String(c.type)))).toBe(false);
});

test("DSH harness switch fits its label and Work is visibly locked", async ({ page }, info) => {
  await openDsh(page);
  const selector = page.locator(".engine-selector");
  expect((await selector.boundingBox())!.width).toBeLessThan(90);
  await page.locator(".engine-toggle").click();
  const menu = page.getByRole("menu", { name: "会话引擎" });
  await expect(menu).toBeVisible();
  await expect(menu.getByRole("menuitemradio")).toHaveCount(3);
  await expect(menu.getByRole("menuitemradio", { name: "DSH", exact: true }).locator("svg")).toHaveCount(2);
  await expect(menu.getByRole("menuitemradio", { name: "DSH", exact: true })).toHaveAttribute("aria-checked", "true");
  for (const theme of ["light", "dark"]) {
    await page.evaluate(value => document.documentElement.setAttribute("data-theme", value), theme);
    await page.screenshot({ path: info.outputPath(`harness-menu-${theme}.png`), animations: "disabled" });
    expect((await menu.boundingBox())!.x).toBeGreaterThanOrEqual(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  }
  await page.locator(".surface-head-title").click();
  const work = page.getByRole("tab", { name: "Work", exact: true });
  await expect(work).toBeDisabled();
  await expect(work.locator(".space-lock")).toBeVisible();
  await expect(work).toHaveAttribute("title", "DSH 暂不支持 Work");
  await page.locator(".s-head").getByRole("button", { name: "收起", exact: true }).click();
  await expect(menu).toHaveCount(0);
  await chooseEngine(page, "claude");
  await expect(page.locator(".engine-label")).toHaveText("Claude");
  expect((await selector.boundingBox())!.width).toBeLessThan(100);
  await chooseEngine(page, "dsh");
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
  await chooseEngine(page, "codex");
  await expect(select).not.toBeFocused();
  await expect(select).toHaveCSS("outline-style", "none");
  await select.focus();
  await select.press("ArrowDown");
  const selected = page.getByRole("menuitemradio", { name: "Codex", exact: true });
  await expect(selected).toBeFocused();
  await selected.press("ArrowDown");
  await expect(page.getByRole("menuitemradio", { name: "DSH", exact: true })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(select).toBeFocused();
  await expect(select).toHaveCSS("outline-style", "solid");
  await expect(page.locator(".engine-label")).toHaveText("DSH");
  await select.press("ArrowUp");
  await page.keyboard.press("Home");
  await expect(page.getByRole("menuitemradio", { name: "Claude", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(select).toBeFocused();
  await expect(page.getByRole("menu", { name: "会话引擎" })).toHaveCount(0);
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
  const relay = await openDsh(page, { commandSuccess: true });
  await page.getByRole("button", { name: /查看 DSH Goal/ }).click();
  const goal = page.getByRole("dialog", { name: "DSH Goal" });
  await expect(goal).toContainText("等待继续");
  await expect(goal).toContainText("3 / 32");
  await goal.getByRole("button", { name: "继续目标", exact: true }).click();
  await expect.poll(() => relay.commands.some(c => c.type === "act_dsh_goal" && c.action === "resume")).toBe(true);
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
  // Explicit command admission establishes native follow, which publishes its
  // actual command catalog before the existing goal's controls become usable.
  relay.emit({ ...nativeState });
  await page.getByRole("button", { name: /查看 DSH Goal/ }).click();
  const goal = page.getByRole("dialog", { name: "DSH Goal" });
  await goal.getByRole("button", { name: "编辑目标", exact: true }).click();
  await goal.getByLabel("目标内容").fill("保存失败也保留这段文字");
  await goal.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(goal.getByRole("alert")).toContainText("目标已更新");
  await expect(goal.getByLabel("目标内容")).toHaveValue("保存失败也保留这段文字");
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

  for (const engine of ["codex", "claude"]) {
    const switches = relay.commands.filter(cmd => cmd.type === "switch_session").length;
    await chooseEngine(page, engine);
    // Supported engines still warm Work; the DSH guard must not disable this.
    await expect.poll(() => relay.commands.some(cmd => cmd.type === "list_sessions"
      && (cmd.engine ?? "claude") === engine && cmd.space === "work")).toBe(true);
    await chooseEngine(page, "dsh");
    await expect.poll(() => relay.commands.filter(cmd => cmd.type === "switch_session").length).toBeGreaterThan(switches);
    await expect(page.getByText("验证图片、引导与原生控件", { exact: true })).toBeVisible();
    expect(dshLists().map(cmd => cmd.space ?? "code")).not.toContain("work");
    await expect(page.locator(".banner")).toHaveCount(0);
  }
});

async function openDshTools(page: Page) {
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  await page.getByRole("button", { name: "会话工具" }).click();
  await expect(page.getByRole("dialog", { name: "DSH 会话工具" })).toBeVisible();
}

test("DSH native directory links open the file browser and retain file previews", async ({ page }) => {
  const relay = await openDsh(page, { features: true });
  await page.getByRole("button", { name: "在 Remote 中打开 /tmp/dsh-test/dir", exact: true }).click();
  await expect(page.locator(".file-browser-shell")).toBeVisible();
  await expect(page.getByRole("button", { name: "README.md", exact: true })).toBeVisible();
  expect(relay.commands.find(c => c.type === "browse_files")?.path).toBe("/tmp/dsh-test/dir");
  await page.getByRole("button", { name: "README.md", exact: true }).click();
  await expect(page.locator(".artifact-panel")).toContainText("seventh");
  await expect(page.getByRole("button", { name: "返回目录", exact: true })).toBeVisible();
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("link", { name: "下载原文件", exact: true }).click();
  expect((await downloadEvent).suggestedFilename()).toBe("README.md");
});

test("DSH archived sessions remain readable without unarchive or delete controls", async ({ page }) => {
  const relay = await openDsh(page, { features: true });
  if (!await page.locator(".shell.sidebar-open").count()) await page.getByRole("button", { name: "Code", exact: true }).click();
  await page.getByRole("button", { name: "更多操作", exact: true }).click();
  await page.getByRole("button", { name: "归档", exact: true }).click();
  await expect.poll(() => relay.commands.some(c => c.type === "archive_session" && c.session_id === sid && c.engine === "dsh")).toBe(true);
  await page.getByText("已归档", { exact: true }).click();
  await page.getByRole("button", { name: "更多操作", exact: true }).click();
  await expect(page.getByRole("button", { name: "取消归档", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "删除会话", exact: true })).toHaveCount(0);
  await expect(page.locator(".composer textarea")).toBeDisabled();
});

test("DSH references insert the native quoted file and session syntax", async ({ page }) => {
  const relay = await openDsh(page, { features: true });
  const input = page.locator(".composer textarea");
  await input.fill("检查 @report");
  await expect(page.getByRole("option", { name: "文件 report notes.md" })).toBeVisible();
  await input.press("Enter");
  await expect(input).toHaveValue('检查 @"report notes.md" ');
  expect(relay.commands.some(c => c.type === "query")).toBe(false);
  await input.fill("关联 @session");
  await page.getByRole("option", { name: "会话 相关会话" }).click();
  await expect(input).toHaveValue("关联 @[相关会话](dsh-session:native-session) ");
  await input.press("Enter");
  await expect.poll(() => relay.commands.filter(c => c.type === "query" && c.prompt === "关联 @[相关会话](dsh-session:native-session)").length).toBe(1);
});

test("DSH subagent controls, jobs, diagnostics and complete export share a compact panel", async ({ page }, info) => {
  const relay = await openDsh(page, { features: true });
  await openDshTools(page);
  const panel = page.getByRole("dialog", { name: "DSH 会话工具" });
  await panel.getByRole("button", { name: "检查构建", exact: true }).click();
  await expect(panel).toContainText("已经检查构建");
  await panel.getByRole("textbox", { name: "子代理消息" }).fill("继续检查");
  await panel.getByRole("button", { name: "排队发送", exact: true }).click();
  await expect(panel).toContainText("已加入子代理队列");
  const act = relay.commands.find(c => c.type === "act_dsh_subagent");
  expect(act?.sid).toBe(sid); expect(act?.target_sid).toBe("dsh@child");
  await panel.getByRole("button", { name: "后台任务", exact: true }).click();
  await expect(panel).toContainText("构建任务");
  await expect(panel).toContainText("运行中");
  await panel.getByRole("button", { name: "连接诊断", exact: true }).click();
  await expect(panel).toContainText("0.1.5-rc.2");
  await panel.getByRole("button", { name: "导出完整会话", exact: true }).click();
  await expect(panel.getByRole("link", { name: "保存 ZIP" })).toBeVisible();
  const downloadEvent = page.waitForEvent("download");
  await panel.getByRole("link", { name: "保存 ZIP" }).click();
  expect((await downloadEvent).suggestedFilename()).toBe("dsh-session-native-session.zip");
  await page.screenshot({ path: info.outputPath("dsh-tools.png") });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
});

test("DSH produced XLSX files preview multiple sheets and preserve the original download", async ({ page }, info) => {
  await openDsh(page, { features: true });
  await openDshTools(page);
  const panel = page.getByRole("dialog", { name: "DSH 会话工具" });
  await panel.getByRole("button", { name: "产出文件", exact: true }).click();
  await panel.getByRole("button", { name: "report.xlsx", exact: true }).click();
  const sheet = page.getByRole("region", { name: "表格预览" });
  await expect(sheet).toContainText("本周进展");
  await expect(sheet.getByRole("cell", { name: "42", exact: true })).toHaveAttribute("title", "=SUM(B2:B3)");
  await sheet.getByRole("tab", { name: "进展", exact: true }).click();
  await expect(sheet).toContainText("<script>bad()</script>");
  expect(await sheet.locator("script").count()).toBe(0);
  const downloadEvent = page.waitForEvent("download");
  await sheet.getByRole("link", { name: "下载原文件" }).click();
  expect((await downloadEvent).suggestedFilename()).toBe("report.xlsx");
  await page.screenshot({ path: info.outputPath("dsh-spreadsheet.png") });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
});

test("DSH native present exposes generated files beneath the answer", async ({ page }) => {
  await openDsh(page, { features: true });
  await page.locator('.dsh-produced').getByRole("button", { name: "report.xlsx", exact: true }).click();
  await expect(page.getByRole("region", { name: "表格预览" })).toContainText("本周进展");
});

test("DSH full text search opens matching context without sending a prompt", async ({ page }) => {
  const relay = await openDsh(page, { features: true });
  if (!await page.locator(".shell.sidebar-open").count()) await page.getByRole("button", { name: "Code", exact: true }).click();
  await page.getByRole("textbox", { name: "搜索会话", exact: true }).fill("needle");
  const results = page.getByRole("region", { name: "全文搜索结果" });
  await expect(results).toContainText("匹配 needle");
  await results.getByRole("button", { name: /DSH 原生会话/ }).click();
  await expect(page.getByRole("dialog", { name: "DSH 会话工具" })).toContainText("已经检查构建 needle");
  expect(relay.commands.some(c => c.type === "query" || c.type === "steer")).toBe(false);
});

test("DSH plan command uses native syntax and shows pending and settled states without a toolbar button", async ({ page }) => {
  const relay = await openDsh(page, { features: true, commandSuccess: true });
  await expect(page.getByRole("button", { name: "计划", exact: true })).toHaveCount(0);
  const input = page.locator(".composer textarea");
  await input.fill("/pla");
  await page.getByRole("listbox", { name: "命令", exact: true }).getByRole("button", { name: /\/plan/ }).click();
  await input.press("Enter");
  await expect.poll(() => relay.commands.some(c => c.type === "set_dsh_control" && c.value === "/plan")).toBe(true);
  expect(relay.commands.some(c => c.type === "query" || c.type === "steer" || c.value === "/plan on")).toBe(false);
  await expect(page.getByRole("status", { name: "DSH 计划状态" })).toHaveCount(0);
  relay.emit({ ...nativeState, plan_active: false, plan_pending: true });
  await expect(page.getByRole("status", { name: "DSH 计划状态" })).toHaveText("进入计划待生效");
  relay.emit({ ...nativeState, plan_active: true, plan_pending: false });
  await expect(page.getByRole("status", { name: "DSH 计划状态" })).toHaveText("计划模式");
  await input.fill("/plan off");
  await input.press("Enter");
  await expect.poll(() => relay.commands.some(c => c.type === "set_dsh_control" && c.value === "/plan off")).toBe(true);
  relay.emit({ ...nativeState, plan_active: false, plan_pending: false });
  await expect(page.getByRole("status", { name: "DSH 计划状态" })).toHaveCount(0);
});

test("DSH cold goal and plan discovery refreshes the open menu and explains minimal capabilities", async ({ page }) => {
  const relay = await openDsh(page);
  const input = page.locator(".composer textarea");
  relay.emit({ ...nativeState, commands: [], goal: null });
  await input.fill("/goa");
  const menu = page.getByRole("listbox", { name: "命令", exact: true });
  await expect(menu.getByRole("button", { name: /\/goal/ })).toBeDisabled();
  relay.emit({ ...nativeState, goal: null });
  await expect(menu.getByRole("button", { name: /\/goal/ })).toBeEnabled();
  await menu.getByRole("button", { name: /\/goal/ }).click();
  const dialog = page.getByRole("dialog", { name: "DSH Goal" });
  await expect(dialog).toBeVisible();
  expect(relay.commands.some(c => c.type === "act_dsh_goal" || (c.type === "set_dsh_control" && c.value === "/goal"))).toBe(false);
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  relay.emit({ ...nativeState, agent_preset: "minimal", commands: nativeState.commands.filter(c => c.name === "permission"), goal: null });
  for (const prefix of ["/goa", "/pla"]) {
    await input.fill(prefix);
    await expect(menu.getByRole("button")).toBeDisabled();
    await expect(menu).toContainText("minimal 未启用");
  }
  expect(relay.commands.some(c => c.type === "query" || c.type === "steer")).toBe(false);
});

test("DSH command errors are dismissible above the input and preserve the draft", async ({ page }, info) => {
  const relay = await openDsh(page);
  const input = page.locator(".composer textarea");
  await input.fill("/goal 保留我的目标");
  await input.press("Enter");
  const notice = page.locator(".composer-in .dsh-command-result");
  await expect(notice).toContainText("当前目标不能替换");
  await expect(page.locator("details.dsh-command-result")).toHaveCount(0);
  await page.screenshot({ path: info.outputPath("dsh-command-error.png") });
  await notice.getByRole("button", { name: "关闭命令提示" }).click();
  await expect(notice).toHaveCount(0);
  await expect(input).toHaveValue("/goal 保留我的目标");
  relay.emit({ ...nativeState, plan_active: false });
  await expect(notice).toHaveCount(0);
});

test("DSH Goal form creates native round cap, pauses, edits and resumes", async ({ page }, info) => {
  const relay = await openDsh(page, { commandSuccess: true });
  relay.emit({ ...nativeState, goal: null });
  const input = page.locator(".composer textarea");
  await input.fill("/goal");
  await input.press("Enter");
  const dialog = page.getByRole("dialog", { name: "DSH Goal" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: /轮次上限 · 256/ })).toBeVisible();
  expect(relay.commands.some(c => c.type === "act_dsh_goal")).toBe(false);
  await dialog.getByLabel("目标内容").fill("完成 DSH 适配并验证手机界面");
  await dialog.getByRole("button", { name: /轮次上限/ }).click();
  await dialog.getByRole("button", { name: "64", exact: true }).click();
  await page.screenshot({ path: info.outputPath("dsh-create.png") });
  await dialog.getByRole("button", { name: "开始目标", exact: true }).click();
  await expect(dialog.getByRole("progressbar", { name: "轮次用量" })).toHaveAttribute("max", "64");
  const created = relay.commands.find(c => c.type === "act_dsh_goal")!;
  expect(created).toMatchObject({ sid, action: "create", max_rounds: 64, objective: "完成 DSH 适配并验证手机界面" });
  expect(created.goal_id).toBeUndefined();
  await dialog.getByRole("button", { name: "暂停续行", exact: true }).click();
  await expect(dialog).toContainText("已暂停");
  await dialog.getByRole("button", { name: "编辑目标", exact: true }).click();
  await dialog.getByLabel("目标内容").fill("保留目标，增加轮次");
  await dialog.getByRole("button", { name: /轮次上限/ }).click();
  await dialog.getByRole("button", { name: "128", exact: true }).click();
  await dialog.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(dialog.getByRole("progressbar")).toHaveAttribute("max", "128");
  expect(relay.commands.find(c => c.type === "act_dsh_goal" && c.action === "edit"))
    .toMatchObject({ goal_id: "created-goal", revision: 2, max_rounds: 128 });
  await expect(dialog).toContainText("已暂停");
  await dialog.getByRole("button", { name: "继续目标", exact: true }).click();
  await expect(dialog).toContainText("自动续行");
  await dialog.getByRole("button", { name: "更多目标操作", exact: true }).click();
  await dialog.getByRole("button", { name: "清除目标", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator(".dsh-goal")).toHaveCount(0);
});

test("DSH Goal live updates preserve edit and its original revision; exhausted goals require a higher cap", async ({ page }) => {
  const relay = await openDsh(page);
  await page.getByRole("button", { name: /查看 DSH Goal/ }).click();
  const dialog = page.getByRole("dialog", { name: "DSH Goal" });
  await dialog.getByRole("button", { name: "编辑目标", exact: true }).click();
  await dialog.getByLabel("目标内容").fill("不能被实时更新覆盖");
  relay.emit({ ...nativeState, goal: { ...nativeState.goal, revision: 3, rounds: 32, phase: "blocked", activation: "disarmed" } });
  await expect(dialog.getByLabel("目标内容")).toHaveValue("不能被实时更新覆盖");
  await dialog.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("目标已更新");
  expect(relay.commands.find(c => c.type === "act_dsh_goal")).toMatchObject({ goal_id: "native-goal", revision: 2 });
  await expect(dialog.getByLabel("目标内容")).toHaveValue("不能被实时更新覆盖");
  await dialog.getByRole("button", { name: "保留输入，使用最新版本", exact: true }).click();
  expect(relay.commands.filter(c => c.type === "act_dsh_goal")).toHaveLength(1);
  await expect(dialog.getByLabel("目标内容")).toHaveValue("不能被实时更新覆盖");
  await dialog.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("目标已更新");
  expect(relay.commands.filter(c => c.type === "act_dsh_goal").at(-1))
    .toMatchObject({ goal_id: "native-goal", revision: 3, objective: "不能被实时更新覆盖" });
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await expect(dialog.getByRole("button", { name: "继续目标", exact: true })).toBeDisabled();
  await expect(dialog).toContainText("可编辑上限后继续");
});
