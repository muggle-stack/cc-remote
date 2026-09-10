import { expect, test } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { PROTOCOL_VERSION, type ServerEvent } from "../src/protocol";
import {
  GITHUB_README_ATTACHMENT_URL, GITHUB_README_IMAGE_URL,
  MARKDOWN_HTML_HEADER_SVG, MARKDOWN_HTML_LOCAL_README,
} from "./fixtures/markdown-html";

type PanelRelayEvent<T = ServerEvent> = T extends ServerEvent
  ? Omit<T, "v" | "ts"> : never;

// Exercise the real App shell, not a copied layout fixture. All control/model
// traffic terminates in this in-memory relay; no real session is touched.
async function mockRightPanelRelay(
  page: import("@playwright/test").Page,
  { visible = false, retained = true, engine = "codex", seedTurns = [],
    secondParent = false, btwReadOnly = false, imageAssets = false, imageData, externalPreview }: {
    visible?: boolean;
    retained?: boolean;
    engine?: "codex" | "claude";
    seedTurns?: NonNullable<Extract<ServerEvent, { type: "history" }>["turns"]>;
    secondParent?: boolean;
    btwReadOnly?: boolean;
    imageAssets?: boolean;
    imageData?: { data: string; width: number; height: number };
    externalPreview?: "allow" | "replace";
  } = {},
) {
  const parentSid = "layout-parent";
  const btwSid = "btw-layout-child";
  const commands: Record<string, unknown>[] = [];
  const allowedReads = new Set<string>();
  let emit: (message: PanelRelayEvent) => void = () => {
    throw new Error("layout relay has not connected");
  };
  await page.addInitScript(({ visible, engine }) => {
    localStorage.setItem("cc_remote_engine", engine);
    localStorage.setItem("cc_remote_machine", "layout-machine");
    // Preserve a user's resize preference while opening/closing the slot.
    localStorage.setItem("cc_remote_artifact_panel_width", "520");
    if (sessionStorage.getItem("cc-remote:btw-panel-scopes-v1") === null) {
      sessionStorage.setItem("cc-remote:btw-panel-scopes-v1", JSON.stringify(visible
        ? [JSON.stringify(["layout-machine", "code", engine, "layout-parent"])] : []));
    }
  }, { visible, engine });
  await page.route("**/api/**", (route) => route.fulfill({ status: 404 }));
  await page.route("**/api/session", (route) => route.fulfill({ json: {} }));
  await page.route("**/api/viewers/pages", (route) => route.fulfill({ json: { pages: [] } }));
  await page.route("**/api/devices", (route) => route.fulfill({ json: {
    devices: [{ machine_id: "layout-machine", label: "Layout", online: true }],
  } }));
  await page.routeWebSocket(/\/ws(?:\?|$)/, (socket) => {
    emit = (message) => socket.send(JSON.stringify({
      v: PROTOCOL_VERSION, ts: Date.now() / 1000, ...message,
    }));
    const snapshot = (sid: string) => {
      emit({ type: "snapshot", sid, cc_session_id: sid,
        state: sid === btwSid && !btwReadOnly ? "running" : "idle", tail_text: "",
        cwd: "/tmp/layout", generation: "layout-generation",
        ...(sid === btwSid && btwReadOnly ? { control: {
          v: PROTOCOL_VERSION, ts: 1, type: "session_control" as const,
          control_mode: "codex_shared" as const, write_state: "read_only" as const,
          terminal_attached: false, can_takeover: false, revision: 2,
          generation: "layout-generation",
          reason: "当前临时对话已经销毁，请创建新的临时对话",
        } } : {}),
      });
      emit({ type: "replay_end", sid, to_seq: 0, truncated: false });
    };
    socket.onMessage((raw) => {
      const command = JSON.parse(String(raw)) as Record<string, unknown>;
      commands.push(command);
      if (command.type === "hello") {
        emit({ type: "btw_sync", generation: "layout-generation", revision: 1,
          sessions: retained ? [{ btw_sid: btwSid, parent_sid: parentSid,
            engine, created_at: 1, state: "running" }] : [] });
        snapshot(parentSid);
      } else if (command.type === "list_sessions") {
        emit({ type: "session_list",
          engine: command.engine === "codex" ? "codex" : "claude",
          space: command.space === "work" ? "work" : "code",
          request_id: String(command.cmd_id),
          sessions: command.space === "work" ? [] : [{ session_id: parentSid,
            engine, space: "code", summary: "Layout parent",
            cwd: "/tmp/layout", state: "idle", last_modified: "100" },
          ...(secondParent ? [{ session_id: "layout-other", engine,
            space: "code" as const, summary: "Second parent", cwd: "/tmp/other",
            state: "idle" as const, last_modified: "50" }] : [])] });
      } else if (command.type === "switch_session") {
        emit({ type: "session_focus", session_id: String(command.session_id) });
        snapshot(String(command.session_id));
      } else if (command.type === "get_history") {
        emit({ type: "history", session_id: String(command.session_id),
          sid: String(command.session_id),
          revision: "layout-history", generation: "layout-generation",
          detail: "summary", events: [], turns: seedTurns, has_more: false });
      } else if (externalPreview && (command.type === "get_file_preview" || command.type === "get_preview_asset")) {
        const requestId = String(command.request_id);
        const path = String(command.path);
        const isFile = command.type === "get_file_preview";
        if (!allowedReads.has(requestId) || externalPreview === "replace") {
          emit({ type: "preview_authorization_required", sid: String(command.sid),
            authorization_id: `${allowedReads.has(requestId) ? "replaced" : "original"}-${requestId}`,
            request_id: requestId, operation: isFile ? "file_preview" : "preview_asset",
            path, resolved_path: path, format: isFile ? "markdown" : "image",
            preview_id: isFile ? null : String(command.preview_id) });
        } else if (isFile) {
          emit({ type: "file_preview", sid: String(command.sid), request_id: requestId,
            path, format: "markdown", content: "# 外部预览已经打开", size: 30,
            mtime_ns: "1", revision: "a".repeat(64), writable: false });
        } else {
          emit({ type: "preview_asset", sid: String(command.sid), request_id: requestId,
            path, preview_id: String(command.preview_id), media_type: "image/png", data: TEST_GENERATED_PNG });
        }
      } else if (externalPreview && command.type === "authorize_preview") {
        const requestId = String(command.request_id);
        allowedReads.add(requestId);
        emit({ type: "preview_authorization_result", sid: String(command.sid),
          authorization_id: String(command.authorization_id), request_id: requestId,
          status: "granted" });
      } else if (imageAssets && command.type === "get_history_image") {
        emit({ type: "history_image", sid: String(command.session_id),
          session_id: String(command.session_id), turn_id: String(command.turn_id),
          image_id: String(command.image_id), variant: command.variant as "thumbnail" | "full",
          request_id: String(command.request_id), revision: "layout-history",
          media_type: "image/png", width: 1, height: 1,
          data: TEST_GENERATED_PNG,
          ...(command.variant === "full" ? imageData : {}) });
      } else if (imageAssets && command.type === "get_preview_asset") {
        emit({ type: "preview_asset", sid: String(command.sid),
          path: String(command.path), preview_id: String(command.preview_id),
          request_id: String(command.request_id), media_type: "image/png",
          data: imageData?.data ?? TEST_GENERATED_PNG });
      } else if (command.type === "sync_btw") {
        if (btwReadOnly) {
          emit({ type: "user_msg", sid: String(command.sid),
            msg_id: "retained-user", prompt: "以前的侧边提问" });
          emit({ type: "delta", sid: String(command.sid),
            message_id: "retained-answer", text: "仍可查看的侧边回答" });
        }
        snapshot(String(command.sid));
      } else if (command.type === "get_diff") {
        emit({ type: "diff_report", sid: String(command.sid),
          request_id: String(command.cmd_id), file: "", diff: "" });
      } else if (command.type === "ping") {
        emit({ type: "pong", n: Number(command.n) });
      }
      if (command.cmd_id) emit({ type: "command_ack",
        client_id: String(command.client_id), cmd_id: String(command.cmd_id) });
    });
  });
  return { commands, emit: (message: PanelRelayEvent) => emit(message) };
}

test("policy refusal remains specific through live delivery and history reload", async ({ page }, testInfo) => {
  const message = "上游模型因安全策略拒绝了本次请求（cyber_policy）。"
    + "这不是本地权限或网络错误；请核实并说明任务背景与授权范围，"
    + "若属误判请向服务提供方反馈。";
  const turns: NonNullable<Extract<ServerEvent, { type: "history" }>["turns"]> = [];
  const relay = await mockRightPanelRelay(page, { retained: false, seedTurns: turns });
  await page.goto("/");
  await expect(page.locator(".composer textarea")).toBeEnabled();
  relay.emit({ type: "user_msg", sid: "layout-parent", msg_id: "policy-user", prompt: "检查这段代码" });
  relay.emit({ type: "state", sid: "layout-parent", state: "running", msg_id: "policy-user" });
  relay.emit({ type: "error", sid: "layout-parent", code: "cc_crash", message, msg_id: "policy-user" });
  relay.emit({ type: "turn_end", sid: "layout-parent", turn_id: "policy-turn",
    result: { subtype: "error", duration_ms: 20, is_error: true } });
  relay.emit({ type: "state", sid: "layout-parent", state: "idle" });
  const problem = page.locator(".turn .note.interrupted");
  await expect(problem).toHaveText(message);
  await expect(page.locator(".composer textarea")).toBeEnabled();
  expect(relay.commands.filter(c => c.type === "query" || c.type === "steer")).toHaveLength(0);

  turns.push({ id: "policy-user", forkPointId: "policy-turn", prompt: "检查这段代码",
    blocks: [], done: true, error: message, detailEventCount: 0, detailLoaded: true },
  { id: "followup-user", forkPointId: "followup-turn", prompt: "解释现有日志",
    blocks: [{ kind: "text", message_id: "followup-answer", text: "日志说明已完成。",
      channel: "final", done: true }], done: true, detailEventCount: 0, detailLoaded: true });
  await page.reload();
  await expect(problem).toHaveCount(1);
  await expect(problem).toHaveText(message);
  await expect(page.locator('[data-turn-id="followup-user"]')).toContainText("日志说明已完成。");
  await expect(page.locator('[data-turn-id="followup-user"] .note.interrupted')).toHaveCount(0);
  for (const theme of ["light", "dark"]) {
    await page.evaluate(value => document.documentElement.setAttribute("data-theme", value), theme);
    await expect(problem).toBeVisible();
    expect(await problem.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
    expect(await problem.evaluate(node => getComputedStyle(node, "::before").display)).toBe("none");
    expect(await problem.evaluate(node => getComputedStyle(node, "::after").display)).toBe("none");
    await page.screenshot({ path: testInfo.outputPath(`policy-refusal-${theme}.png`) });
  }
});

test("destroyed BTW stays readable after refresh with disabled input and a working new-chat button", async ({ page }, testInfo) => {
  const relay = await mockRightPanelRelay(page, { visible: true, btwReadOnly: true });
  await page.goto("/");
  const panel = page.locator(".btw-panel");
  const reason = "当前临时对话已经销毁，请创建新的临时对话";
  for (let i = 0; i < 2; i++) {
    if (i) await page.reload();
    await expect(panel).toContainText("以前的侧边提问");
    await expect(panel).toContainText("仍可查看的侧边回答");
    await expect(panel.getByRole("status")).toHaveText(reason);
    await expect(panel.getByRole("textbox")).toBeDisabled();
    await expect(panel.getByRole("textbox")).toHaveAttribute("placeholder", reason);
    await expect(panel.locator(".btw-send")).toBeDisabled();
    await expect(panel.locator(".btw-controls button").first()).toBeDisabled();
    await expect(panel.locator(".btw-controls button").last()).toBeDisabled();
    await expect(panel.getByRole("button", { name: "新建侧边对话" })).toBeEnabled();
    await expect(panel.locator(".btw-chat-close")).toBeEnabled();
  }
  expect(relay.commands.filter((c) => ["query", "steer", "thread/fork"].includes(String(c.type))))
    .toHaveLength(0);
  await panel.screenshot({ path: testInfo.outputPath("btw-destroyed.png") });
  await panel.getByRole("button", { name: "新建侧边对话" }).click();
  await expect.poll(() => relay.commands.filter((c) => c.type === "open_btw").length).toBe(1);
  expect(relay.commands.find((c) => c.type === "open_btw")?.sid).toBe("layout-parent");
});

test("destroyed BTW live control preserves drafts and rejects stale writable snapshots", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { visible: true });
  await page.goto("/");
  const panel = page.locator(".btw-panel");
  const input = panel.getByRole("textbox");
  await expect(input).toBeEnabled();
  await input.fill("失效时不要清除我的草稿");
  const control = {
    type: "session_control" as const, sid: "btw-layout-child",
    generation: "layout-generation", revision: 2,
    control_mode: "codex_shared" as const, write_state: "read_only" as const,
    terminal_attached: false, can_takeover: false,
    reason: "当前临时对话已经销毁，请创建新的临时对话",
  };
  relay.emit(control);
  await expect(input).toBeDisabled();
  await expect(input).toHaveValue("失效时不要清除我的草稿");
  relay.emit({ ...control, revision: 1, write_state: "writable", reason: null });
  await expect(input).toBeDisabled();
  await expect(panel.locator(".btw-runbar")).toHaveCount(0);
  await expect(panel.locator(".btw-send")).toBeDisabled();
  // Selection/copy and manual close remain available; no automatic fork or send.
  expect(relay.commands.filter((c) => ["query", "steer", "open_btw"].includes(String(c.type))))
    .toHaveLength(0);
  page.on("dialog", (dialog) => dialog.accept());
  await panel.locator(".btw-chat-close").click();
  await expect.poll(() => relay.commands.filter((c) => c.type === "close_btw").length).toBe(1);
  expect(relay.commands.find((c) => c.type === "close_btw")?.sid).toBe("btw-layout-child");
});

test("transient BTW reconnect notice does not permanently disable the composer", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { visible: true });
  await page.goto("/");
  const panel = page.locator(".btw-panel");
  await expect(panel.getByRole("textbox")).toBeEnabled();
  relay.emit({ type: "session_control", sid: "btw-layout-child", generation: "layout-generation",
    revision: 1, control_mode: "codex_shared", write_state: "writable",
    terminal_attached: false, can_takeover: false, reason: "共享通道正在重新连接" });
  await expect(panel.getByRole("textbox")).toBeEnabled();
  await expect(panel.locator(".btw-readonly-notice")).toHaveCount(0);
});

async function openAsyncQuestion(page: import("@playwright/test").Page, index = 0) {
  // The scroll-to-bottom affordance can cover the middle of the last row in a
  // narrow BTW pane. Use the card's leading icon hit area, just like a user tap.
  await page.locator(".async-question-card").nth(index).click({ position: { x: 24, y: 24 } });
  const dialog = page.getByRole("dialog", { name: "助手询问", exact: true });
  await expect(dialog).toBeVisible();
  return dialog;
}

const ASYNC_QUESTIONS = [
  { title: "在哪个设备发生？", options: ["Mac", "手机"] },
  { title: "用的是什么手势？", options: null },
];
const ASYNC_HISTORY_TURN = {
  id: "async-user", prompt: "继续修复", done: true, detailLoaded: true,
  forkPointId: "async-task", blocks: [{ kind: "text", message_id: "async-item",
    text: "在哪个设备发生？用的是什么手势？", channel: "final", done: true,
    delivery: "async", questions: ASYNC_QUESTIONS }],
};

const TEST_GENERATED_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j6RkAAAAASUVORK5CYII=";

test("async question compact styling shows each question once on desktop and mobile", async ({ page }, testInfo) => {
  const title = "这几次恢复，是你在板子失联后手动重启或断电的吗？失联前是否也有主动重启或改 IP？这能帮助区分程序触发的故障与外部操作。";
  await mockRightPanelRelay(page, { seedTurns: [{
    ...ASYNC_HISTORY_TURN, blocks: [{ ...ASYNC_HISTORY_TURN.blocks[0],
      text: title, questions: [{ title, options: null }] }],
  }] });
  await page.goto("/");
  const entry = page.locator(".async-question-card");
  await expect(entry).toBeVisible();
  expect((await entry.boundingBox())!.height).toBeLessThan(110);
  await expect(page.getByRole("dialog", { name: "助手询问" })).toHaveCount(0);
  const card = await openAsyncQuestion(page);
  await expect(card).toBeVisible();
  const metrics = await card.evaluate((node) => ({
    width: node.getBoundingClientRect().width,
    height: node.getBoundingClientRect().height,
    overflow: node.scrollWidth - node.clientWidth,
    weight: Number(getComputedStyle(node.querySelector("legend")!).fontWeight),
  }));
  expect(metrics.width).toBeLessThanOrEqual(560);
  expect(metrics.height).toBeLessThan(page.viewportSize()!.height);
  expect(metrics.overflow).toBeLessThanOrEqual(1);
  expect(metrics.weight).toBeLessThan(600);
  expect(await card.evaluate(node => parseFloat(getComputedStyle(node).borderRadius))).toBeGreaterThanOrEqual(28);
  await expect(card).toHaveCSS("border-top-width", "1px");
  await card.getByLabel("你的回答", { exact: true }).fill("三指拖拽");
  await page.screenshot({ path: testInfo.outputPath("async-question-compact.png") });
  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await page.screenshot({ path: testInfo.outputPath("async-question-compact-dark.png") });
  await expect(card.getByText(title, { exact: true })).toHaveCount(1);
  await expect(card.locator("details, .async-question-source, .async-question-original")).toHaveCount(0);
  await expect(card).not.toContainText("原始提问");
  await expect(card).not.toContainText("发送后将开始新一轮对话");
  await expect(card.locator(".async-question-footer .async-question-hint")).toHaveCount(0);
  expect(await card.getAttribute("aria-describedby")).toBeNull();
  await expect(card.getByLabel("你的回答", { exact: true })).toHaveValue("三指拖拽");
});

test("generated image live snapshot renders outside collapsed process and duplicate events stay idempotent", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { imageAssets: true });
  await page.goto("/");
  await expect.poll(() => relay.commands.some((c) => c.type === "get_history")).toBe(true);
  const sid = "layout-parent";
  relay.emit({ type: "user_msg", sid, msg_id: "image-user", prompt: "画一张图" });
  relay.emit({ type: "process", sid, item_id: "generated-1", kind: "server_tool",
    phase: "start", status: "running", title: "生成图片", tool: "image_generation" });
  await expect(page.locator(".generated-image-gallery")).toHaveCount(0);
  for (let i = 0; i < 2; i++) relay.emit({ type: "process", sid,
    item_id: "generated-1", kind: "server_tool", phase: "end", status: "succeeded",
    title: "生成图片", tool: "image_generation",
    input: { file_path: "/generated/image.png", preview_id: "generated-1" } });
  const image = page.getByRole("button", { name: "预览生成的图片" });
  await expect(image).toHaveCount(1);
  await expect(image).toBeVisible();
  expect(relay.commands.filter((c) => c.type === "get_preview_asset")).toHaveLength(1);
  await image.click();
  await expect(page.locator(".image-lightbox-image")).toBeVisible();
});

test("async question first layout keeps optional notes visible with vertical choices and a separate send button", async ({ page }, testInfo) => {
  const title = "按这版目标开始可行性设计？先确认目标，再逐项验证哪些能做到。";
  const relay = await mockRightPanelRelay(page, { seedTurns: [{
    ...ASYNC_HISTORY_TURN, blocks: [{ ...ASYNC_HISTORY_TURN.blocks[0], text: title,
      questions: [{ title, options: ["确认，按这版做可行性设计", "需要调整，我补充说明"] }] }],
  }] });
  await page.goto("/");
  const card = await openAsyncQuestion(page);
  await expect(card).toBeVisible();
  await expect(card.getByRole("textbox")).toBeVisible();
  await expect(card.getByRole("textbox")).toHaveValue("");
  await expect(card.getByRole("textbox")).not.toBeFocused();
  await expect(card.locator(".async-question-notes")).toHaveCount(0);
  const send = card.getByRole("button", { name: "发送回答", exact: true });
  await expect(send).toBeEnabled();
  await expect(send).toHaveCSS("border-top-left-radius", "999px");
  await expect(card.locator(".async-question-option").first()).toHaveCSS("border-top-left-radius", "999px");
  await expect(card.locator(".async-question-composer .async-question-send")).toHaveCount(0);
  const options = card.locator(".async-question-option");
  const first = await options.nth(0).boundingBox();
  const second = await options.nth(1).boundingBox();
  expect(second!.y).toBeGreaterThanOrEqual(first!.y + first!.height);
  await page.screenshot({ path: testInfo.outputPath("async-question-first-layout.png") });
  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await page.screenshot({ path: testInfo.outputPath("async-question-first-layout-dark.png") });
  expect(relay.commands.filter(c => ["query", "steer"].includes(String(c.type)))).toHaveLength(0);
  await card.getByRole("radio", { name: "需要调整，我补充说明" }).check();
  await expect(card.getByRole("textbox")).toBeVisible();
  // The editor is always visible. Changing a choice neither submits nor steals
  // focus, and does not discard text that will override the selected option.
  await expect(card.getByRole("textbox")).not.toBeFocused();
  expect(relay.commands.filter(c => ["query", "steer"].includes(String(c.type)))).toHaveLength(0);
  await card.getByRole("textbox").fill("需要先验证续航。\n不要直接加工。");
  await card.getByRole("radio", { name: "确认，按这版做可行性设计" }).check();
  await expect(card.getByRole("textbox")).toBeVisible();
  await card.getByRole("button", { name: "稍后回答", exact: true }).click();
  await openAsyncQuestion(page);
  await expect(card.getByRole("radio", { name: "确认，按这版做可行性设计" })).toBeChecked();
  await expect(card.getByRole("textbox")).toHaveValue("需要先验证续航。\n不要直接加工。");
  await card.getByRole("button", { name: "发送回答", exact: true }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "query").length).toBe(1);
  const query = relay.commands.find(c => c.type === "query")!;
  expect(query.prompt).toContain("回答：需要先验证续航。\n不要直接加工。");
  expect(query.prompt).not.toContain("回答：确认");
});

test("async question option presses avoid native tap flashes and retain rounded keyboard focus", async ({ page, isMobile }, testInfo) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [{
    ...ASYNC_HISTORY_TURN, blocks: [{ ...ASYNC_HISTORY_TURN.blocks[0], questions: [ASYNC_QUESTIONS[0]] }],
  }] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  const option = dialog.locator(".async-question-option").nth(1);
  const radio = option.getByRole("radio");
  // Both the label and the transparent, full-size native input participate in
  // touch hit testing. Neither may paint the browser's rectangular tap overlay.
  for (const target of [option, radio]) {
    await expect(target).toHaveCSS("-webkit-tap-highlight-color", "rgba(0, 0, 0, 0)");
  }
  await expect(option).toHaveCSS("border-top-left-radius", "999px");
  const box = (await radio.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  try {
    await expect(option).toHaveCSS("outline-style", "none");
    await page.screenshot({ path: testInfo.outputPath("async-question-option-pressed.png") });
  } finally {
    await page.mouse.up();
  }
  await expect(radio).toBeChecked();
  if (isMobile) {
    await dialog.getByRole("radio", { name: "Mac", exact: true }).tap();
    await radio.tap();
    await expect(radio).toBeChecked();
  }
  await expect(dialog.getByRole("textbox")).toBeVisible();
  // No blanket outline removal: tabbing back to the selected radio still
  // exposes a visible focus ring following the option's rounded shape.
  // Mobile Safari need not focus a radio when tapped. Start the keyboard path
  // from a known control and tab to the selected option on both platforms.
  await dialog.getByRole("button", { name: "关闭助手询问" }).focus();
  await page.keyboard.press("Tab");
  await expect(radio).toBeFocused();
  await expect(option).toHaveCSS("outline-style", "solid");
  await expect(option).toHaveCSS("outline-width", "2px");
  await expect(option).toHaveCSS("border-top-left-radius", "999px");
  expect(relay.commands.filter(c => ["query", "steer", "answer_question", "interrupt"].includes(String(c.type))))
    .toHaveLength(0);
  // The visible textarea remains optional; an option-only answer still sends.
  await dialog.getByRole("button", { name: "发送回答" }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "query").length).toBe(1);
  expect(relay.commands.find(c => c.type === "query")?.prompt).toBe("补充回答：\n\n问题：在哪个设备发生？\n回答：手机");
});

test("generated image canonical summary survives refresh and loads original without detail", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { imageAssets: true, seedTurns: [{
    id: "image-user", prompt: "画一张图", done: true, detailLoaded: false,
    processDetailState: "present", detailReasons: ["process"],
    processStartedTs: 1788621175000, processDoneTs: 1788621261221,
    blocks: [{ kind: "process", item_id: "generated-1", processKind: "server_tool",
      phase: "end", status: "succeeded", done: true, title: "生成图片",
      tool: "image_generation", input: { history_image: {
        image_id: "img-native", media_type: "image/png", width: 1, height: 1, byte_size: 68,
      } } }],
  }] });
  await page.goto("/");
  const image = page.getByRole("button", { name: "预览生成的图片" });
  await expect(image).toBeEnabled();
  const process = page.getByRole("button", { name: /已处理 1m 26s/ });
  await expect(process).toBeVisible();
  await expect(process).toHaveAttribute("aria-expanded", "false");
  await page.reload();
  await expect(image).toBeEnabled();
  await expect(process).toBeVisible();
  await expect(process).toHaveAttribute("aria-expanded", "false");
  expect(relay.commands.filter((c) => c.type === "get_turn_detail")).toHaveLength(0);
  const reads = relay.commands.filter((c) => c.type === "get_history_image");
  expect(reads.length).toBeGreaterThanOrEqual(2);
  expect(reads.every((c) => c.turn_id === "image-user" && c.variant === "full")).toBe(true);
  await image.click();
  await expect(page.locator(".image-lightbox-image")).toBeVisible();
  expect(relay.commands.filter(c => c.type === "get_history_image")).toHaveLength(reads.length);
});

test("generated image stays loaded when a new message renames the historical turn", async ({ page }) => {
  const turns: NonNullable<Extract<ServerEvent, { type: "history" }>["turns"]> = [{
    id: "msg-old", forkPointId: "native-task", prompt: "画图", done: true, detailLoaded: true,
    blocks: [{ kind: "process", item_id: "image-old", processKind: "server_tool",
      phase: "end", status: "succeeded", done: true, title: "生成图片", tool: "image_generation",
      input: { history_image: { image_id: "img-stable", media_type: "image/png", width: 1, height: 1, byte_size: 68 } } }],
  }];
  const relay = await mockRightPanelRelay(page, { imageAssets: true, seedTurns: turns });
  await page.goto("/");
  const image = page.locator(".generated-output img");
  await expect(image).toHaveCount(1);
  const src = await image.getAttribute("src");
  const reads = relay.commands.filter(c => c.type === "get_history_image").length;
  expect(reads).toBe(1);
  await page.locator(".composer textarea").fill("继续");
  await page.locator(".composer").getByRole("button", { name: "发送", exact: true }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "query").length).toBe(1);
  const query = relay.commands.find(c => c.type === "query")!;
  await expect(image).toHaveAttribute("src", src!);
  turns[0] = { ...turns[0], id: "item-123" };
  turns.push({ id: "next-message", clientMsgId: String(query.msg_id), prompt: "继续", done: false, blocks: [] });
  relay.emit({ type: "user_msg", sid: "layout-parent", msg_id: "next-message",
    client_msg_id: String(query.msg_id), prompt: "继续" });
  relay.emit({ type: "state", sid: "layout-parent", state: "running", msg_id: "next-message" });
  relay.emit({ type: "history", sid: "layout-parent", session_id: "layout-parent",
    revision: "layout-history", generation: "layout-generation",
    detail: "summary", events: [], turns, has_more: false });
  await expect(page.getByText("继续", { exact: true })).toBeVisible();
  await expect(image).toHaveCount(1);
  await expect(image).toHaveAttribute("src", src!);
  await expect(page.locator(".generated-output .history-image-placeholder")).toHaveCount(0);
  await page.getByRole("button", { name: "查看大图" }).click();
  await expect(page.locator(".image-lightbox-image")).toHaveAttribute("src", src!);
  expect(relay.commands.filter(c => c.type === "get_history_image")).toHaveLength(reads);
  await page.keyboard.press("Escape");
  // A hard reload still uses the canonical id and full original, not a 360px thumbnail.
  await page.reload();
  await expect(image).toHaveAttribute("src", src!);
  expect(relay.commands.filter(c => c.type === "get_history_image").at(-1))
    .toMatchObject({ turn_id: "item-123", variant: "full" });
});

test("generated image live snapshot survives canonical item identity handoff", async ({ page }) => {
  const turns: NonNullable<Extract<ServerEvent, { type: "history" }>["turns"]> = [];
  const relay = await mockRightPanelRelay(page, { imageAssets: true, seedTurns: turns });
  await page.goto("/");
  await expect.poll(() => relay.commands.some(c => c.type === "get_history")).toBe(true);
  const sid = "layout-parent";
  const ref = { image_id: "img-live-stable", media_type: "image/png", width: 1, height: 1, byte_size: 68 };
  relay.emit({ type: "user_msg", sid, msg_id: "draw-user", prompt: "画图" });
  relay.emit({ type: "turn_binding", sid, msg_id: "draw-user", turn_id: "draw-task" });
  relay.emit({ type: "process", sid, item_id: "live-image", kind: "server_tool",
    phase: "end", status: "succeeded", tool: "image_generation", title: "生成图片",
    input: { history_image: ref, file_path: "/generated/live.png", preview_id: "live-image" } });
  const image = page.locator(".generated-output img");
  await expect(image).toHaveCount(1);
  const src = await image.getAttribute("src");
  // Completion can trigger an automatic history read. The mock's canonical
  // store must advance with the emitted page, not later return an empty store
  // and erase the completed turn depending on request timing.
  turns.push({
    id: "draw-user", forkPointId: "draw-task", prompt: "画图", done: true, detailLoaded: false,
    blocks: [{ kind: "process", item_id: "item-image-123", processKind: "server_tool",
      phase: "end", status: "succeeded", done: true, tool: "image_generation", title: "生成图片",
      input: { history_image: ref } }],
  });
  relay.emit({ type: "turn_end", sid, turn_id: "draw-task", result: { subtype: "success", is_error: false } });
  relay.emit({ type: "history", sid, session_id: sid,
    revision: "layout-history", generation: "layout-generation",
    detail: "summary", events: [], has_more: false, turns });
  await expect(image).toHaveCount(1);
  await expect(image).toHaveAttribute("src", src!);
  await expect(page.locator(".generated-output .history-image-placeholder")).toHaveCount(0);
  expect(relay.commands.filter(c => c.type === "get_preview_asset")).toHaveLength(1);
  expect(relay.commands.filter(c => c.type === "get_history_image")).toHaveLength(0);
});

test("generated image uses full reply width and remains visible through process collapse", async ({ page }, testInfo) => {
  const desktop = testInfo.project.name === "chromium";
  if (desktop) await page.setViewportSize({ width: 1360, height: 900 });
  const imageData = await page.evaluate(() => {
    const canvas = document.createElement("canvas");
    canvas.width = 1693;
    canvas.height = 929;
    const ctx = canvas.getContext("2d")!;
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#26334d";
    ctx.font = "24px sans-serif";
    for (let y = 50; y < 900; y += 40) ctx.fillText("Generated image — original resolution / 原图清晰度", 30, y);
    return { data: canvas.toDataURL("image/png").split(",")[1], width: canvas.width, height: canvas.height };
  });
  const relay = await mockRightPanelRelay(page, { imageAssets: true, imageData, seedTurns: [{
    id: "large-image", prompt: "画一张图", done: true, detailLoaded: true,
    blocks: [{ kind: "process", item_id: "generated-large", processKind: "server_tool",
      phase: "end", status: "succeeded", done: true, title: "生成图片", tool: "image_generation",
      input: { history_image: { image_id: "img-large", media_type: "image/png", width: 1693, height: 929, byte_size: 68 } } }],
  }] });
  await page.goto("/");
  const figure = page.locator(".generated-output");
  const image = figure.getByRole("button", { name: "预览生成的图片" });
  await expect(image).toBeEnabled();
  await expect.poll(() => image.locator("img").evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(1693);
  const bounds = await image.boundingBox();
  expect(bounds!.width).toBeGreaterThan(desktop ? 600 : 300);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(page.viewportSize()!.width);
  expect(Math.abs(bounds!.width / bounds!.height - 1693 / 929)).toBeLessThan(.02);
  const process = page.locator('.turn[data-turn-id="large-image"]').getByRole("button", { name: /已处理/ }).first();
  for (let i = 0; i < 2; i++) {
    await process.click();
    await expect(figure).toBeVisible();
  }
  await figure.getByRole("button", { name: "查看大图" }).click();
  await expect(page.locator(".image-lightbox-image")).toBeVisible();
  expect(relay.commands.filter(c => c.type === "get_turn_detail")).toHaveLength(0);
  await page.keyboard.press("Escape");
  await page.screenshot({ path: testInfo.outputPath("generated-image-wide.png") });
});

test("async question answered presentation survives refresh without rewriting or hiding the user reply", async ({ page }, testInfo) => {
  const prompt = "补充回答：\n\n问题：在哪个设备发生？\n回答：Mac 浏览器\n\n问题：用的是什么手势？\n回答：三指拖拽\n拖到页面下方";
  const relay = await mockRightPanelRelay(page, { seedTurns: [ASYNC_HISTORY_TURN, {
    id: "supplemental-reply", prompt, done: false, blocks: [],
  }] });
  await page.goto("/");
  for (let i = 0; i < 2; i++) {
    if (i) await page.reload();
    const entry = page.locator(".async-question-card");
    const card = page.getByRole("dialog", { name: "助手询问", exact: true });
    await expect(entry).toContainText("已回答");
    await expect(card.getByRole("textbox")).toHaveCount(0);
    const answer = page.locator(".supplemental-answer");
    await expect(answer).toContainText("三指拖拽\n拖到页面下方");
    await expect(answer.locator("details")).not.toHaveAttribute("open");
    await answer.locator("summary").click();
    await expect(answer.getByText("在哪个设备发生？", { exact: true })).toBeVisible();
    await answer.locator("summary").click();
    if (i === 1) await page.screenshot({ path: testInfo.outputPath("async-question-answered.png") });
    await openAsyncQuestion(page);
    await expect(card.getByRole("textbox")).toHaveCount(2);
    await card.getByLabel("你的回答", { exact: true }).fill("还没有发送的补充草稿");
    await card.getByRole("button", { name: "稍后回答", exact: true }).click();
    const reopen = entry;
    await expect(card).toHaveCount(0);
    await expect(card.getByRole("textbox")).toHaveCount(0);
    await reopen.click();
    await expect(card.getByLabel("你的回答", { exact: true })).toHaveValue("还没有发送的补充草稿");
    await card.getByRole("button", { name: "关闭助手询问" }).click();
  }
  expect(relay.commands.filter(c => ["query", "steer", "interrupt", "get_turn_detail"].includes(String(c.type)))).toHaveLength(0);
});

test("async question unanswered draft and choices survive repeated collapse without sending", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [ASYNC_HISTORY_TURN] });
  await page.goto("/");
  const card = await openAsyncQuestion(page);
  await expect(card).not.toContainText("发送后将开始新一轮对话");
  await card.getByLabel("手机", { exact: true }).check();
  await card.getByLabel("你的回答", { exact: true }).fill("选区手柄\n这是保留的草稿");
  for (let i = 0; i < 2; i++) {
    await card.getByRole("button", { name: "稍后回答", exact: true }).click();
    await expect(card.getByRole("textbox")).toHaveCount(0);
    await expect(page.locator(".async-question-card")).toContainText("待回答");
    await openAsyncQuestion(page);
    await expect(card.getByLabel("手机", { exact: true })).toBeChecked();
    await expect(card.getByLabel("你的回答", { exact: true })).toHaveValue("选区手柄\n这是保留的草稿");
  }
  expect(relay.commands.filter(c => ["query", "steer", "answer_question", "interrupt"].includes(String(c.type))))
    .toHaveLength(0);
});

test("async question dialog keeps independent drafts and restores keyboard focus", async ({ page }, testInfo) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [{ ...ASYNC_HISTORY_TURN,
    blocks: [ASYNC_HISTORY_TURN.blocks[0], { ...ASYNC_HISTORY_TURN.blocks[0], message_id: "other-question" }],
  }] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  await dialog.getByLabel("你的回答", { exact: true }).fill("第一个问题的草稿");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  if (testInfo.project.name === "chromium") await expect(page.locator(".async-question-card").first()).toBeFocused();
  await openAsyncQuestion(page, 1);
  await expect(dialog.getByLabel("你的回答", { exact: true })).toHaveValue("");
  await dialog.getByLabel("你的回答", { exact: true }).fill("第二个问题的草稿");
  await dialog.getByRole("button", { name: "关闭助手询问" }).click();
  await openAsyncQuestion(page);
  await expect(dialog.getByLabel("你的回答", { exact: true })).toHaveValue("第一个问题的草稿");
  // The native modal owns focus; tabbing cannot activate the main composer.
  for (let i = 0; i < 9; i++) {
    await page.keyboard.press("Tab");
    expect(await dialog.evaluate(node => node.contains(document.activeElement))).toBe(true);
  }
  await page.mouse.click(4, 4);
  await expect(dialog).toHaveCount(0);
  expect(relay.commands.filter(c => ["query", "steer", "interrupt"].includes(String(c.type)))).toHaveLength(0);
});

test("async question dialog fits a keyboard-sized viewport without moving into a side panel", async ({ page }, testInfo) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [ASYNC_HISTORY_TURN] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  await dialog.getByLabel("你的回答", { exact: true }).fill("保留输入中的草稿");
  await page.evaluate(() => {
    // Drive the real viewport synchronizer; writing its CSS output directly
    // races the delayed focus/keyboard settling reads in the App shell.
    Object.defineProperties(window.visualViewport!, {
      height: { configurable: true, value: 360 },
      offsetTop: { configurable: true, value: 40 },
    });
    window.visualViewport!.dispatchEvent(new Event("resize"));
  });
  await expect.poll(async () => {
    const rect = await dialog.boundingBox();
    return !!rect && rect.y >= 40 && rect.y + rect.height <= 400;
  }).toBe(true);
  expect(await dialog.evaluate(node => node.parentElement === document.body)).toBe(true);
  const box = (await dialog.boundingBox())!;
  expect(Math.abs(box.x + box.width / 2 - page.viewportSize()!.width / 2)).toBeLessThan(2);
  await expect(dialog.getByRole("button", { name: "发送回答" })).toBeInViewport();
  await expect(dialog.getByRole("button", { name: "关闭助手询问" })).toBeInViewport();
  const body = dialog.locator(".async-question-body");
  expect(await body.evaluate(node => node.scrollHeight > node.clientHeight)).toBe(true);
  await expect(dialog.getByLabel("你的回答", { exact: true })).toBeInViewport({ ratio: 1 });
  await expect.poll(() => dialog.getByLabel("你的回答", { exact: true }).evaluate(input => {
    const viewport = input.closest(".async-question-body")!.getBoundingClientRect();
    const editor = input.getBoundingClientRect();
    return editor.top >= viewport.top - 1 && editor.bottom <= viewport.bottom + 1;
  })).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("async-question-keyboard.png") });
  expect(relay.commands.filter(c => ["query", "steer"].includes(String(c.type)))).toHaveLength(0);
});

test("async question dialog omits raw source previews and preserves its draft on reopen", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { externalPreview: "allow", seedTurns: [{
    ...ASYNC_HISTORY_TURN, blocks: [{ ...ASYNC_HISTORY_TURN.blocks[0],
      text: "请看[设计说明](/tmp/proposal.md)，用的是什么手势？" }],
  }] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  await expect(dialog.getByText("用的是什么手势？", { exact: true })).toHaveCount(1);
  await expect(dialog.locator("details, .async-question-source, .async-question-original")).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: "在 Remote 中打开 /tmp/proposal.md" })).toHaveCount(0);
  await dialog.getByLabel("你的回答", { exact: true }).fill("保留的回答草稿");
  await dialog.getByRole("button", { name: "关闭助手询问" }).click();
  await expect(dialog).toHaveCount(0);
  await openAsyncQuestion(page);
  await expect(dialog.getByLabel("你的回答", { exact: true })).toHaveValue("保留的回答草稿");
  expect(relay.commands.filter(c => ["query", "steer", "get_file_preview", "authorize_preview"].includes(String(c.type)))).toHaveLength(0);
});

test("async question dialog survives source row virtualization without losing its draft", async ({ page }) => {
  const older = Array.from({ length: 40 }, (_, i) => ({ id: `old-${i}`, prompt: `以前的问题 ${i}`,
    done: true, detailLoaded: true, blocks: [{ kind: "text" as const, message_id: `old-answer-${i}`,
      channel: "final" as const, text: "这是一条以前的回复。".repeat(20), done: true }] }));
  const relay = await mockRightPanelRelay(page, { seedTurns: [...older, ASYNC_HISTORY_TURN] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  await dialog.getByLabel("你的回答", { exact: true }).fill("虚拟列表外也要保留");
  await waitForScrollIdle(page);
  // Simulate a history viewport transition while the modal stays mounted. Use
  // the normal scroll ownership path so the old reader anchor can be released.
  await page.locator(".thread").dispatchEvent("wheel", { deltaY: -1000 });
  await pauseOutputAndScrollToHistoryStart(page);
  await expect(page.locator(".async-question-card")).toHaveCount(0);
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel("你的回答", { exact: true })).toHaveValue("虚拟列表外也要保留");
  await dialog.getByRole("button", { name: "发送回答" }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "query").length).toBe(1);
  expect(relay.commands.find(c => c.type === "query")?.prompt).toContain("虚拟列表外也要保留");
  expect(relay.commands.find(c => c.type === "query")?.sid).toBe("layout-parent");
});

test("async question dialog closes on a session switch and never leaks drafts", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [ASYNC_HISTORY_TURN], secondParent: true });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  await dialog.getByLabel("你的回答", { exact: true }).fill("只属于当前会话的草稿");
  // Drive an explicit navigation intent through the real sidebar handler.
  // Unsolicited remote focus frames are correctly ignored by the WS client.
  await page.getByText("Second parent", { exact: true }).evaluate(node => (node as HTMLElement).click());
  await expect(page.getByText("session layout-o", { exact: true })).toBeAttached();
  await expect(dialog).toHaveCount(0);
  await openAsyncQuestion(page);
  await expect(dialog.getByLabel("你的回答", { exact: true })).toHaveValue("");
  expect(relay.commands.filter(c => ["query", "steer", "interrupt"].includes(String(c.type)))).toHaveLength(0);
});

test("async question dialog retains an IME draft when control becomes read-only", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [ASYNC_HISTORY_TURN] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  const input = dialog.getByLabel("你的回答", { exact: true });
  await input.fill("三指拖拽的补充");
  await input.dispatchEvent("compositionstart");
  await input.dispatchEvent("keydown", { key: "Enter", code: "Enter", keyCode: 229, isComposing: true });
  await input.dispatchEvent("compositionend", { data: "补充" });
  expect(relay.commands.filter(c => ["query", "steer"].includes(String(c.type)))).toHaveLength(0);
  relay.emit({ type: "session_control", sid: "layout-parent", generation: "layout-generation", revision: 1,
    control_mode: "codex_shared", write_state: "read_only", terminal_attached: false,
    can_takeover: false, reason: "当前会话只读" });
  await expect(input).toBeDisabled();
  await expect(input).toHaveValue("三指拖拽的补充");
  await expect(dialog).toContainText("当前会话暂不可写，回答草稿会保留");
  expect(await dialog.getAttribute("aria-describedby")).toBeTruthy();
  await expect(dialog.getByRole("button", { name: "发送回答" })).toBeDisabled();
  await dialog.getByRole("button", { name: "稍后回答" }).click();
  await openAsyncQuestion(page);
  await expect(input).toHaveValue("三指拖拽的补充");
  expect(relay.commands.filter(c => ["query", "steer"].includes(String(c.type)))).toHaveLength(0);
});

for (const processDetailState of ["none", "unknown"] as const) {
  test(`direct reply summary has no empty detail entry across refresh (${processDetailState})`, async ({ page }) => {
    const relay = await mockRightPanelRelay(page, { seedTurns: [{
      id: "direct-reply", prompt: "在吗？", done: true,
      processDetailState, detailReasons: [], detailLoaded: false, detailEventCount: 0,
      blocks: [{ kind: "text", message_id: "direct-answer", channel: "final", done: true, text: "在，请说。" }],
    }] });
    await page.goto("/");
    for (let i = 0; i < 2; i++) {
      if (i) await page.reload();
      await expect(page.getByText("在，请说。", { exact: true })).toBeVisible();
      await expect(page.locator(".turn-detail-entry")).toHaveCount(0);
      await expect(page.getByText(/已处理|查看本轮详情|查看完整内容/)).toHaveCount(0);
    }
    expect(relay.commands.filter(c => c.type === "get_turn_detail")).toHaveLength(0);
  });
}

for (const type of ["file", "image"] as const) {
  test(`external preview ${type} opens without a second permission prompt`, async ({ page }) => {
    const relay = await mockRightPanelRelay(page, { externalPreview: "allow", seedTurns: [{
      id: "external-output", prompt: "查看结果", done: true, detailLoaded: true,
      blocks: [{ kind: "text", message_id: "external-link", done: true, channel: "final",
        text: type === "file" ? "[打开预览](/tmp/external.md)" : "![外部生成图](/tmp/external.png)" }],
    }] });
    await page.goto("/");
    if (type === "file") {
      await page.getByRole("button", { name: "在 Remote 中打开 /tmp/external.md" }).click();
      await expect(page.locator(".artifact-panel")).toContainText("外部预览已经打开");
      await expect(page.getByRole("button", { name: "保存", exact: true })).toBeDisabled();
    } else {
      await expect(page.getByRole("img", { name: "外部生成图", exact: true })).toBeVisible();
    }
    await expect(page.getByRole("button", { name: "允许查看" })).toHaveCount(0);
    const grants = relay.commands.filter(c => c.type === "authorize_preview");
    expect(grants).toHaveLength(1);
    expect(grants[0].decision).toBe("allow");
    expect(grants[0].sid).toBe("layout-parent");
    expect(relay.commands.some(c => c.type === "save_markdown")).toBe(false);
  });
}

test("external preview replacement stops automatic grant retries and allows a fresh read", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { externalPreview: "replace", seedTurns: [{
    id: "external-output", prompt: "查看结果", done: true, blocks: [{ kind: "text", message_id: "external-link", done: true, channel: "final", text: "[打开预览](/tmp/external.md)" }],
  }] });
  await page.goto("/");
  await page.getByRole("button", { name: "在 Remote 中打开 /tmp/external.md" }).click();
  await expect(page.locator(".artifact-panel")).toContainText("读取未完成");
  expect(relay.commands.filter(c => c.type === "authorize_preview")).toHaveLength(1);
  expect(relay.commands.filter(c => c.type === "get_file_preview")).toHaveLength(2);
  await page.getByRole("button", { name: "刷新文件" }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "authorize_preview").length).toBe(2);
  await expect(page.locator(".artifact-panel")).toContainText("读取未完成");
  expect(relay.commands.filter(c => c.type === "get_file_preview")).toHaveLength(4);
});

test("async question history restores a nonblocking card and replies through the scoped query outbox", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [ASYNC_HISTORY_TURN] });
  await page.goto("/");
  const entry = page.locator(".async-question-card");
  const card = page.getByRole("dialog", { name: "助手询问", exact: true });
  await expect(entry).toBeVisible();
  await expect(page.getByRole("dialog", { name: "操作确认" })).toHaveCount(0);
  // This is server-backed History, not a local pendingQuestion or replay ring.
  await page.reload();
  await expect(entry).toBeVisible();
  await openAsyncQuestion(page);
  await expect(card).not.toContainText("发送后将开始新一轮对话");
  await expect(card.getByLabel("Mac", { exact: true })).toBeChecked();
  await expect(card.getByLabel("其他回答（填写后替代选项）")).toBeVisible();
  await card.getByLabel("其他回答（填写后替代选项）").fill("Mac 浏览器");
  await card.getByLabel("你的回答", { exact: true }).fill("三指拖拽");
  await card.getByLabel("你的回答", { exact: true }).press("Enter");
  expect(relay.commands.filter((c) => c.type === "query")).toHaveLength(0);
  await card.getByRole("button", { name: "发送回答" }).click();
  await expect.poll(() => relay.commands.filter((c) => c.type === "query").length).toBe(1);
  const query = relay.commands.find((c) => c.type === "query")!;
  expect(query.sid).toBe("layout-parent");
  expect(query.prompt).toContain("回答：Mac 浏览器");
  expect(query.prompt).toContain("回答：三指拖拽");
  expect(relay.commands.filter((c) => ["interrupt", "answer_question", "steer"].includes(String(c.type))))
    .toHaveLength(0);
  await expect(card).toHaveCount(0);
});

test("async question summary keeps its ordinary answer visible after refresh without detail loading", async ({ page }) => {
  const answer = "普通回复仍然完整显示，不需要展开过程。";
  const relay = await mockRightPanelRelay(page, { seedTurns: [{
    ...ASYNC_HISTORY_TURN, detailLoaded: false,
    processDetailState: "none", detailReasons: [],
    blocks: [...ASYNC_HISTORY_TURN.blocks, {
      kind: "text", message_id: "unphased-answer", text: answer,
      channel: "final", done: true,
    }],
  }] });
  await page.goto("/");
  await expect(page.locator(".async-question-card")).toHaveCount(1);
  await expect(page.getByText(answer, { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.locator(".async-question-card")).toHaveCount(1);
  await expect(page.getByText(answer, { exact: true })).toHaveCount(1);
  await expect(page.getByText(answer, { exact: true })).toBeVisible();
  expect(relay.commands.filter((c) => ["query", "steer", "get_turn_detail"].includes(String(c.type))))
    .toHaveLength(0);
});

test("async question live delivery preserves running state and uses steer instead of approval", async ({ page }) => {
  const relay = await mockRightPanelRelay(page);
  await page.goto("/");
  await expect(page.getByText("session layout-p", { exact: true })).toHaveCount(1);
  await expect.poll(() => relay.commands.some((c) => c.type === "get_history")).toBe(true);
  const sid = "layout-parent";
  relay.emit({ type: "user_msg", sid, msg_id: "async-user", prompt: "继续修复" });
  relay.emit({ type: "turn_binding", sid, msg_id: "async-user", turn_id: "async-task" });
  relay.emit({ type: "state", sid, state: "running", msg_id: "async-user" });
  relay.emit({ type: "assistant_msg_start", sid, message_id: "async-item", turn_id: "async-task", channel: "final" });
  relay.emit({ type: "delta", sid, message_id: "async-item", turn_id: "async-task", channel: "final", text: "在哪个设备发生？" });
  for (let i = 0; i < 2; i++) relay.emit({ type: "assistant_msg_end", sid,
    message_id: "async-item", turn_id: "async-task", channel: "final",
    delivery: "async", questions: ASYNC_QUESTIONS });
  const card = await openAsyncQuestion(page);
  await expect(card).toHaveCount(1);
  await expect(card).toContainText("补充会发送给当前任务，不会中断执行");
  await expect(card).not.toContainText("发送后将开始新一轮对话");
  await card.getByLabel("你的回答", { exact: true }).fill("三指拖拽");
  await card.getByRole("button", { name: "发送回答" }).click();
  await expect.poll(() => relay.commands.filter((c) => c.type === "steer").length).toBe(1);
  expect(relay.commands.find((c) => c.type === "steer")?.sid).toBe(sid);
  expect(relay.commands.filter((c) => ["interrupt", "answer_question", "query"].includes(String(c.type))))
    .toHaveLength(0);
});

test("async question completion updates reply mode while preserving a collapsed draft", async ({ page }) => {
  const relay = await mockRightPanelRelay(page);
  await page.goto("/");
  await expect.poll(() => relay.commands.some(c => c.type === "get_history")).toBe(true);
  const sid = "layout-parent";
  relay.emit({ type: "user_msg", sid, msg_id: "async-user", prompt: "继续修复" });
  relay.emit({ type: "turn_binding", sid, msg_id: "async-user", turn_id: "async-task" });
  relay.emit({ type: "state", sid, state: "running", msg_id: "async-user" });
  relay.emit({ type: "assistant_msg_start", sid, message_id: "async-item", turn_id: "async-task", channel: "final" });
  relay.emit({ type: "delta", sid, message_id: "async-item", turn_id: "async-task", channel: "final", text: "用的是什么手势？" });
  relay.emit({ type: "assistant_msg_end", sid, message_id: "async-item", turn_id: "async-task", channel: "final",
    delivery: "async", questions: [{ title: "用的是什么手势？", options: null }] });
  const card = await openAsyncQuestion(page);
  await expect(card).toContainText("补充会发送给当前任务，不会中断执行");
  await card.getByLabel("你的回答", { exact: true }).fill("三指拖拽");
  await card.getByRole("button", { name: "稍后回答", exact: true }).click();
  relay.emit({ type: "turn_end", sid, turn_id: "async-task", result: { subtype: "success", is_error: false } });
  relay.emit({ type: "state", sid, state: "idle" });
  await expect(page.locator(".async-question-card")).toBeVisible();
  await expect(card.getByRole("textbox")).toHaveCount(0);
  await openAsyncQuestion(page);
  await expect(card.getByLabel("你的回答", { exact: true })).toHaveValue("三指拖拽");
  await expect(card).not.toContainText("发送后将开始新一轮对话");
  await expect(card.locator(".async-question-footer .async-question-hint")).toHaveCount(0);
  expect(await card.getAttribute("aria-describedby")).toBeNull();
  await expect(card).not.toContainText("不会中断执行");
  await card.getByRole("button", { name: "发送回答" }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "query").length).toBe(1);
  expect(relay.commands.filter(c => ["steer", "interrupt", "answer_question"].includes(String(c.type)))).toHaveLength(0);
  await expect(card.getByRole("textbox")).toHaveCount(0);
  // Submission locks input until native acceptance, not the view-only toggle.
  await openAsyncQuestion(page);
  await expect(card.getByRole("textbox")).toBeVisible();
  await expect(card.getByRole("textbox")).toBeDisabled();
  await card.getByRole("button", { name: "稍后回答", exact: true }).click();
  expect(relay.commands.filter(c => c.type === "query")).toHaveLength(1);
});

test("async question from a hidden side chat never steals the main view", async ({ page }) => {
  const relay = await mockRightPanelRelay(page);
  await page.goto("/");
  await expect(page.getByText("session layout-p", { exact: true })).toHaveCount(1);
  const sid = "btw-layout-child";
  relay.emit({ type: "user_msg", sid, msg_id: "async-user", prompt: "side question" });
  relay.emit({ type: "assistant_msg_start", sid, message_id: "async-item", channel: "final" });
  relay.emit({ type: "delta", sid, message_id: "async-item", channel: "final", text: "在哪个设备发生？" });
  relay.emit({ type: "assistant_msg_end", sid, message_id: "async-item", channel: "final",
    delivery: "async", questions: ASYNC_QUESTIONS });
  await expect(page.locator(".async-question-card")).toHaveCount(0);
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await page.keyboard.press("Control+Shift+k");
  await expect(page.locator(".btw-panel .async-question-card")).toBeVisible();
  const card = await openAsyncQuestion(page);
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: "发送回答" }).click();
  await expect.poll(() => relay.commands.filter((c) => c.type === "steer").length).toBe(1);
  expect(relay.commands.find((c) => c.type === "steer")?.sid).toBe(sid);
});

for (const sideChat of [false, true]) {
  test(`async question double submissions preserve the first answer in ${sideChat ? "side chat" : "main chat"}`, async ({ page }) => {
    const twoCards = {
      ...ASYNC_HISTORY_TURN,
      blocks: [ASYNC_HISTORY_TURN.blocks[0],
        { ...ASYNC_HISTORY_TURN.blocks[0], message_id: "async-item-two" }],
    };
    const relay = await mockRightPanelRelay(page, {
      visible: sideChat, seedTurns: sideChat ? [] : [twoCards],
    });
    await page.goto("/");
    if (sideChat) {
      await expect(page.locator(".btw-panel")).toBeVisible();
      await expect.poll(() => relay.commands.some((c) => c.type === "sync_btw")).toBe(true);
      relay.emit({ type: "user_msg", sid: "btw-layout-child", msg_id: "btw-ask",
        prompt: "two questions", seq: 1 });
      let seq = 2;
      for (const message_id of ["async-item", "async-item-two"]) {
        relay.emit({ type: "assistant_msg_start", sid: "btw-layout-child",
          message_id, channel: "final", seq: seq++ });
        relay.emit({ type: "delta", sid: "btw-layout-child", message_id,
          text: "需要补充信息", channel: "final", seq: seq++ });
        relay.emit({ type: "assistant_msg_end", sid: "btw-layout-child", message_id,
          channel: "final", delivery: "async", questions: ASYNC_QUESTIONS, seq: seq++ });
      }
      relay.emit({ type: "state", sid: "btw-layout-child", state: "idle", seq: seq++ });
    }
    const entries = page.locator(".async-question-card");
    await expect(entries).toHaveCount(2);
    const dialog = await openAsyncQuestion(page);
    await dialog.getByRole("form").evaluate((form: HTMLFormElement) => {
      form.requestSubmit();
      form.requestSubmit();
    });
    await expect.poll(() => relay.commands.filter(c => c.type === "query").length).toBe(1);
    await expect(dialog).toHaveCount(0);
    await openAsyncQuestion(page, 1);
    await expect(dialog.getByRole("button", { name: "发送回答" })).toBeDisabled();
    await dialog.getByRole("form").evaluate((form: HTMLFormElement) => form.requestSubmit());
    expect(relay.commands.filter(c => c.type === "query")).toHaveLength(1);
    const query = relay.commands.find((c) => c.type === "query")!;
    expect(query.sid).toBe(sideChat ? "btw-layout-child" : "layout-parent");
    expect(query.delivery).not.toBe("replace");
    expect(relay.commands.some((c) => c.type === "interrupt")).toBe(false);
  });
}

async function expectRightPanelSpace(
  page: import("@playwright/test").Page, open: boolean, desktop = true,
) {
  const shell = page.locator(".shell");
  if (open) await expect(shell).toHaveClass(/\bpanel-open\b/);
  else await expect(shell).not.toHaveClass(/\bpanel-open\b/);
  // CSS transitions and persisted resize width must settle to actual geometry.
  await expect(page.locator(".pane")).toHaveCSS(
    "padding-right", open && desktop ? "548px" : "0px");
}

const VIEWER_LINK_SITE = { id: "robot", label: "机器人结构", machine_id: "layout-machine",
  revision: "a".repeat(32), entry: "/viewer/index.html", urls: ["http://localhost:9000/viewer/index.html"] };

test("remote Viewer menu and registered links use a session-scoped panel without engine queries", async ({ page }) => {
  await page.setViewportSize({ width: 1568, height: 881 });
  const relay = await mockRightPanelRelay(page, { secondParent: true, seedTurns: [{
    id: "viewer-link", prompt: "查看结构", done: true, detailLoaded: true,
    blocks: [{ kind: "text", message_id: "viewer-message", done: true, channel: "final",
      text: "[结构预览](http://localhost:9000/viewer/index.html)" }],
  }] });
  let registered = false;
  await page.route("**/api/viewers", (route) => route.fulfill({ json: { enabled: true,
    sites: registered ? [VIEWER_LINK_SITE, { ...VIEWER_LINK_SITE, machine_id: "another-device" }] : [] } }));
  await page.goto("/");
  await expect(page.getByText("session layout-p", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  await page.getByRole("button", { name: /远程预览.*交互页面/ }).click();
  await expect(page.locator(".remote-viewer-panel")).toContainText("本会话还没有页面");
  await expectRightPanelSpace(page, true);
  await page.keyboard.press("Control+b");
  await page.getByText("Second parent", { exact: true }).click();
  await expect(page.locator(".remote-viewer-panel")).toHaveCount(0);
  await expectRightPanelSpace(page, false);
  await page.getByText("Layout parent", { exact: true }).click();
  await expect(page.locator(".remote-viewer-panel")).toBeVisible();
  await page.locator(".remote-viewer-panel .viewer-desktop-close").click();
  await expectRightPanelSpace(page, false);
  registered = true;
  const refreshed = page.waitForResponse("**/api/viewers");
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await (await refreshed).finished();
  await page.getByRole("link", { name: "结构预览", exact: true }).click();
  await expect(page.locator(".remote-viewer-panel")).toContainText("选择生成该页面的设备与预览");
  await expect(page.locator(".viewer-site")).toHaveCount(2);
  const previousPanel = await page.locator(".remote-viewer-panel").elementHandle();
  await page.getByRole("link", { name: "结构预览", exact: true }).click();
  await expect.poll(() => previousPanel!.evaluate((element) => element.isConnected)).toBe(false);
  await expect(page.locator(".remote-viewer-panel")).toContainText("选择生成该页面的设备与预览");
  expect(relay.commands.some((command) => ["query", "fork_btw", "get_file_preview"].includes(String(command.type))))
    .toBe(false);
});

for (const mode of ["disabled", "unregistered", "unrelated", "unavailable", "malformed"] as const) {
  test(`remote Viewer preserves native link navigation when the catalog is ${mode}`, async ({ page, context }) => {
    const href = "http://192.168.56.1/admin";
    const relay = await mockRightPanelRelay(page, { seedTurns: [{
      id: "ordinary-link", prompt: "查看页面", done: true, detailLoaded: true,
      blocks: [{ kind: "text", message_id: "ordinary-link-message", done: true, channel: "final",
        text: `[管理页面](${href})` }],
    }] });
    await context.route(href, (route) => route.fulfill({ contentType: "text/html", body: "native-link-target" }));
    await page.route("**/api/viewers", (route) => route.fulfill(mode === "unavailable"
      ? { status: 503, json: { error: "device_offline" } }
      : { json: mode === "malformed" ? null : { enabled: mode !== "disabled",
        sites: mode === "unregistered" ? [] : [{ ...VIEWER_LINK_SITE,
          urls: mode === "unrelated" ? VIEWER_LINK_SITE.urls : [href] }] } }));
    const catalog = page.waitForResponse("**/api/viewers");
    await page.goto("/");
    await (await catalog).finished();
    const popup = page.waitForEvent("popup");
    await page.getByRole("link", { name: "管理页面", exact: true }).click();
    const original = await popup;
    await expect(original).toHaveURL(href);
    await expect(page.locator(".remote-viewer-panel")).toHaveCount(0);
    expect(relay.commands.some((command) => ["query", "fork_btw", "get_file_preview"].includes(String(command.type))))
      .toBe(false);
    await original.close();
  });
}

test("remote Viewer preserves a first click while its catalog is still loading", async ({ page, context }) => {
  const href = VIEWER_LINK_SITE.urls[0];
  await mockRightPanelRelay(page, { seedTurns: [{
    id: "pending-link", prompt: "查看结构", done: true, detailLoaded: true,
    blocks: [{ kind: "text", message_id: "pending-link-message", done: true, channel: "final",
      text: `[结构预览](${href})` }],
  }] });
  await context.route(href, (route) => route.fulfill({ contentType: "text/html", body: "native-link-target" }));
  let pending: import("@playwright/test").Route | undefined;
  await page.route("**/api/viewers", (route) => { pending = route; });
  await page.goto("/");
  await expect.poll(() => !!pending).toBe(true);
  const popup = page.waitForEvent("popup");
  await page.getByRole("link", { name: "结构预览", exact: true }).click();
  const original = await popup;
  await expect(original).toHaveURL(href);
  await expect(page.locator(".remote-viewer-panel")).toHaveCount(0);
  await pending!.fulfill({ json: { enabled: true, sites: [VIEWER_LINK_SITE] } });
  await original.close();
});

test("remote Viewer registered link waits for its deferred session controller", async ({ page }) => {
  const href = VIEWER_LINK_SITE.urls[0];
  const relay = await mockRightPanelRelay(page, { seedTurns: [{
    id: "deferred-viewer-link", prompt: "查看结构", done: true, detailLoaded: true,
    blocks: [{ kind: "text", message_id: "deferred-viewer-message", done: true, channel: "final",
      text: `[结构预览](${href})` }],
  }] });
  let controller: import("@playwright/test").Route | undefined;
  await page.route("**/src/viewer-pages-controller.tsx*", (route) => { controller = route; });
  await page.route("**/api/viewers", (route) => route.fulfill({ json: { enabled: true, sites: [VIEWER_LINK_SITE] } }));
  let associations = 0;
  await page.route("**/api/viewers/pages", (route) => {
    const body = route.request().postDataJSON();
    if (body.action === "associate") associations++;
    return route.fulfill({ json: { pages: body.action === "associate" ? [{
      id: "deferred-page", ...body.page, label: VIEWER_LINK_SITE.label,
      references: [], turn_ids: [], available: true,
    }] : [] } });
  });
  await page.route("**/api/viewers/open", (route) => route.fulfill({ status: 503, json: { error: "device_offline" } }));
  const catalog = page.waitForResponse("**/api/viewers");
  await page.goto("/");
  await (await catalog).finished();
  await expect.poll(() => !!controller).toBe(true);
  await page.getByRole("link", { name: "结构预览", exact: true }).click();
  await expect(page.locator(".remote-viewer-panel")).toBeVisible();
  await expect(page.locator(".remote-viewer-panel [role=alert]")).toHaveCount(0);
  expect(associations).toBe(0);
  await controller!.continue();
  await expect(page.locator(".remote-viewer-panel [role=alert]")).toContainText("设备已离线");
  expect(associations).toBe(1);
  expect(relay.commands.some((command) => command.type === "query")).toBe(false);
});

test("remote Viewer registered link keeps an original-link fallback after a failed preview", async ({ page, context }) => {
  const href = VIEWER_LINK_SITE.urls[0] + "?camera=front#part";
  await mockRightPanelRelay(page, { seedTurns: [{
    id: "registered-link", prompt: "查看结构", done: true, detailLoaded: true,
    blocks: [{ kind: "text", message_id: "registered-link-message", done: true, channel: "final",
      text: `[结构预览](${href})` }],
  }] });
  await context.route("http://localhost:9000/**", (route) => route.fulfill({ contentType: "text/html", body: "native-link-target" }));
  let catalogReads = 0;
  await page.route("**/api/viewers", (route) => {
    catalogReads++;
    return route.fulfill({ json: { enabled: true, sites: [VIEWER_LINK_SITE] } });
  });
  await page.route("**/api/viewers/open", (route) => route.fulfill({ status: 503, json: { error: "device_offline" } }));
  await page.route("**/api/viewers/pages", (route) => {
    const body = route.request().postDataJSON();
    return route.fulfill({ json: { pages: body.action === "associate" ? [{
      id: "registered-page", ...body.page, label: VIEWER_LINK_SITE.label,
      references: [], turn_ids: [], available: true,
    }] : [] } });
  });
  const catalog = page.waitForResponse("**/api/viewers");
  await page.goto("/");
  await (await catalog).finished();
  const link = page.getByRole("link", { name: "结构预览", exact: true });
  await link.waitFor();
  // Refocusing the parent from an iframe must not drop a freshly resolved link.
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  // Modifier and non-primary clicks must never call the Viewer handler.
  const modifiers = await link.evaluate((node) => {
    return [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }]
      .map((options) => {
        const click = new MouseEvent("click", { bubbles: true, cancelable: true, ...options });
        // Block native test navigation only after React has had a chance to
        // consume it, and record whether the application did so.
        let allowed = false;
        const capture = (event: Event) => { allowed = !event.defaultPrevented; event.preventDefault(); };
        document.addEventListener("click", capture, { once: true });
        node.dispatchEvent(click);
        return allowed;
      });
  });
  expect(modifiers).toEqual([true, true, true, true, true]);
  await expect(page.locator(".remote-viewer-panel")).toHaveCount(0);
  expect(catalogReads).toBe(1);
  await link.click();
  await expect(page.locator(".remote-viewer-panel [role=alert]")).toContainText("设备已离线");
  const fallback = page.getByRole("link", { name: "打开原链接", exact: true });
  await expect(fallback).toHaveAttribute("href", href);
  await expect(fallback).toHaveAttribute("rel", "noopener noreferrer");
  const popup = page.waitForEvent("popup");
  await fallback.click();
  const original = await popup;
  await expect(original).toHaveURL(href);
  await original.close();
});

test("remote Viewer drops a cached match as soon as catalog revalidation begins", async ({ page, context }) => {
  const href = VIEWER_LINK_SITE.urls[0];
  await mockRightPanelRelay(page, { seedTurns: [{
    id: "refresh-link", prompt: "查看结构", done: true, detailLoaded: true,
    blocks: [{ kind: "text", message_id: "refresh-link-message", done: true, channel: "final",
      text: `[结构预览](${href})` }],
  }] });
  await context.route(href, (route) => route.fulfill({ contentType: "text/html", body: "native-link-target" }));
  let hold = false;
  let pending: import("@playwright/test").Route | undefined;
  await page.route("**/api/viewers", (route) => {
    if (hold) { pending = route; return; }
    return route.fulfill({ json: { enabled: true, sites: [VIEWER_LINK_SITE] } });
  });
  const catalog = page.waitForResponse("**/api/viewers");
  await page.goto("/");
  await (await catalog).finished();
  hold = true;
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect.poll(() => !!pending).toBe(true);
  const popup = page.waitForEvent("popup");
  await page.getByRole("link", { name: "结构预览", exact: true }).click();
  const original = await popup;
  await expect(original).toHaveURL(href);
  await expect(page.locator(".remote-viewer-panel")).toHaveCount(0);
  await pending!.fulfill({ status: 503, json: { error: "device_offline" } });
  await original.close();
});

test("remote Viewer ignores a late catalog from the previous session", async ({ page, context }) => {
  const href = VIEWER_LINK_SITE.urls[0];
  await page.setViewportSize({ width: 1568, height: 881 });
  await mockRightPanelRelay(page, { secondParent: true, seedTurns: [{
    id: "scoped-link", prompt: "查看结构", done: true, detailLoaded: true,
    blocks: [{ kind: "text", message_id: "scoped-link-message", done: true, channel: "final",
      text: `[结构预览](${href})` }],
  }] });
  await context.route(href, (route) => route.fulfill({ contentType: "text/html", body: "native-link-target" }));
  let first: import("@playwright/test").Route | undefined;
  await page.route("**/api/viewers", (route) => {
    if (!first) { first = route; return; }
    return route.fulfill({ json: { enabled: false, sites: [] } });
  });
  await page.goto("/");
  await expect.poll(() => !!first).toBe(true);
  await page.keyboard.press("Control+b");
  const catalog = page.waitForResponse("**/api/viewers");
  await page.getByText("Second parent", { exact: true }).click();
  await (await catalog).finished();
  await first!.fulfill({ json: { enabled: true, sites: [VIEWER_LINK_SITE] } });
  await expect(page.getByText("session layout-o", { exact: true })).toBeVisible();
  const popup = page.waitForEvent("popup");
  await page.getByRole("link", { name: "结构预览", exact: true }).click();
  const original = await popup;
  await expect(original).toHaveURL(href);
  await expect(page.locator(".remote-viewer-panel")).toHaveCount(0);
  await original.close();
});

test("side chat scope follows its parent through navigation and refresh without creating forks", async ({ page }) => {
  await page.setViewportSize({ width: 1568, height: 881 });
  const relay = await mockRightPanelRelay(page, { visible: true, secondParent: true });
  await page.goto("/");
  await expect(page.locator(".btw-panel")).toBeVisible();
  await page.locator(".btw-panel textarea").fill("只属于原会话的草稿");
  await page.keyboard.press("Control+b");
  await page.getByText("Second parent", { exact: true }).click();
  await expect(page.getByText("session layout-o", { exact: true })).toBeVisible();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await expectRightPanelSpace(page, false);
  relay.emit({ type: "user_msg", sid: "btw-layout-child", msg_id: "bg-scoped",
    prompt: "原会话的侧聊仍在运行", seq: 1 });
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await page.getByText("Layout parent", { exact: true }).click();
  await expect(page.locator(".btw-panel")).toContainText("原会话的侧聊仍在运行");
  await expect(page.locator(".btw-panel textarea")).toHaveValue("只属于原会话的草稿");
  await page.getByText("Second parent", { exact: true }).click();
  await page.reload();
  // The cold catalog chooses its newest parent, not the last browser focus.
  // Restored visibility must still belong only to the explicitly opened one.
  await expect(page.getByText("session layout-p", { exact: true })).toBeVisible();
  await expect(page.locator(".btw-panel")).toBeVisible();
  await page.keyboard.press("Control+b");
  await page.getByText("Second parent", { exact: true }).click();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await page.getByText("Layout parent", { exact: true }).click();
  await expect(page.locator(".btw-panel")).toBeVisible();
  expect(relay.commands.filter((command) =>
    ["open_btw", "close_btw", "query", "interrupt"].includes(String(command.type))))
    .toEqual([]);
});

test("side chat scope keeps late fork creation and sibling visibility with the originating parent", async ({ page }) => {
  await page.setViewportSize({ width: 1568, height: 881 });
  const relay = await mockRightPanelRelay(page, {
    visible: true, retained: false, secondParent: true,
  });
  await page.goto("/");
  await page.getByRole("button", { name: "新建侧边对话", exact: true }).click();
  await expect.poll(() => relay.commands.filter((c) => c.type === "open_btw").length).toBe(1);
  const open = relay.commands.find((c) => c.type === "open_btw")!;
  await page.keyboard.press("Control+b");
  await page.getByText("Second parent", { exact: true }).click();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  relay.emit({ type: "btw_opened", sid: "btw-layout-child",
    btw_sid: "btw-layout-child", parent_sid: "layout-parent", engine: "codex",
    created_at: 1, revision: 2, request_id: String(open.request_id) });
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await page.keyboard.press("Control+Shift+k");
  await expect.poll(() => relay.commands.filter((c) => c.type === "open_btw").length).toBe(2);
  const secondOpen = relay.commands.filter((c) => c.type === "open_btw")[1];
  expect(secondOpen.sid).toBe("layout-other");
  await page.getByRole("button", { name: "收起侧边对话", exact: true }).click();
  await page.getByText("Layout parent", { exact: true }).click();
  await expect(page.locator(".btw-chat-tab")).toHaveCount(1);
  relay.emit({ type: "btw_opened", sid: "btw-other-child",
    btw_sid: "btw-other-child", parent_sid: "layout-other", engine: "codex",
    created_at: 2, revision: 3, request_id: String(secondOpen.request_id) });
  await expect(page.locator(".btw-chat-tab")).toHaveCount(1);
  await page.getByText("Second parent", { exact: true }).click();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await page.keyboard.press("Control+Shift+k");
  await expect(page.locator(".btw-chat-tab")).toHaveCount(1);
  expect(relay.commands.filter((c) => c.type === "open_btw")).toHaveLength(2);
  expect(relay.commands.filter((c) => c.type === "close_btw")).toHaveLength(0);
});

test("right panel layout releases collapsed and reload-restored side chat space", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1568, height: 881 });
  const relay = await mockRightPanelRelay(page);
  await page.goto("/");
  await expect(page.getByText("session layout-p", { exact: true })).toBeVisible();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await expectRightPanelSpace(page, false);

  await page.keyboard.press("Control+Shift+k");
  await expect(page.locator(".btw-panel")).toBeVisible();
  await expectRightPanelSpace(page, true);
  await expect(page.locator(".btw-chat-state.running")).toHaveCount(1);
  await page.getByRole("button", { name: "收起侧边对话", exact: true }).click();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await expectRightPanelSpace(page, false);

  // Background frames remain routed while hidden; expanding is not a new fork.
  relay.emit({ type: "user_msg", sid: "btw-layout-child",
    msg_id: "background-message", prompt: "侧聊仍在后台运行", seq: 1 });
  await page.keyboard.press("Control+Shift+k");
  await expect(page.locator(".btw-panel")).toContainText("侧聊仍在后台运行");
  await expectRightPanelSpace(page, true);
  await page.keyboard.press("Control+Shift+k");
  await expectRightPanelSpace(page, false);

  await page.reload();
  await expect(page.getByText("session layout-p", { exact: true })).toBeVisible();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await expectRightPanelSpace(page, false);
  await page.keyboard.press("Control+Shift+k");
  await expect(page.locator(".btw-chat-tab")).toHaveCount(1);
  await expectRightPanelSpace(page, true);
  await page.reload();
  await expect(page.locator(".btw-panel")).toBeVisible();
  await expectRightPanelSpace(page, true);
  await page.keyboard.press("Control+b");
  await page.getByRole("button", { name: "新会话", exact: true }).click();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await expectRightPanelSpace(page, false);
  await page.getByText("Layout parent", { exact: true }).click();
  await expect(page.locator(".btw-chat-tab")).toHaveCount(1);
  await expectRightPanelSpace(page, true);
  expect(relay.commands.filter((command) =>
    ["open_btw", "close_btw", "query", "interrupt"].includes(String(command.type))))
    .toEqual([]);
});

test("right panel layout allocates empty and opening side chats only while visible", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1568, height: 881 });
  const relay = await mockRightPanelRelay(page, { visible: true, retained: false });
  await page.goto("/");
  await expect(page.locator(".btw-panel")).toContainText("暂无侧边对话");
  await expectRightPanelSpace(page, true);
  await page.getByRole("button", { name: "新建侧边对话", exact: true }).click();
  await expect(page.locator(".btw-panel")).toContainText("正在打开侧边对话");
  await expectRightPanelSpace(page, true);
  await page.getByRole("button", { name: "收起侧边对话", exact: true }).click();
  await expectRightPanelSpace(page, false);
  const open = relay.commands.find((command) => command.type === "open_btw")!;
  // A late fork response must retain the chat without reopening the slot.
  relay.emit({ type: "btw_opened", sid: "btw-layout-child",
    btw_sid: "btw-layout-child", parent_sid: "layout-parent", engine: "codex",
    created_at: 1, revision: 2, request_id: String(open.request_id) });
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await expectRightPanelSpace(page, false);
  await page.keyboard.press("Control+Shift+k");
  await expect(page.locator(".btw-chat-tab")).toHaveCount(1);
  await expectRightPanelSpace(page, true);
});

test("right panel layout shares the diff slot and keeps mobile as an overlay", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1568, height: 881 });
  await mockRightPanelRelay(page, { visible: true });
  await page.goto("/");
  await expect(page.locator(".btw-panel")).toBeVisible();
  await expectRightPanelSpace(page, true);
  await page.keyboard.press("Control+Shift+b");
  await expect(page.locator(".artifact-panel")).toBeVisible();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await expectRightPanelSpace(page, true);
  await page.locator(".artifact-panel").getByRole("tab", { name: "btw" }).click();
  await expect(page.locator(".btw-panel")).toBeVisible();
  await expectRightPanelSpace(page, true);
  // Collapse BTW with an artifact still available: the diff becomes visible.
  await page.getByRole("button", { name: "收起侧边对话", exact: true }).click();
  await expect(page.locator(".artifact-panel")).toBeVisible();
  await expectRightPanelSpace(page, true);
  await page.locator(".artifact-panel").getByRole("button", { name: "收起", exact: true }).click();
  await expectRightPanelSpace(page, false);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.keyboard.press("Control+Shift+k");
  await expect(page.locator(".btw-panel")).toBeVisible();
  await expectRightPanelSpace(page, true, false);
  await page.getByRole("button", { name: "收起侧边对话", exact: true }).click();
  await expectRightPanelSpace(page, false, false);
  await page.setViewportSize({ width: 1568, height: 881 });
  await expectRightPanelSpace(page, false);
});

test("right panel layout preserves Claude agent detail priority over side chats", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1568, height: 881 });
  const relay = await mockRightPanelRelay(page, { visible: true, engine: "claude" });
  await page.goto("/");
  await expect(page.locator(".btw-panel")).toBeVisible();
  relay.emit({ type: "background_process_sync", sid: "layout-parent",
    items: [{ item_id: "layout-agent", kind: "agent", status: "running",
      title: "Layout child agent" }] });
  const agentCard = page.getByRole("button", { name: "Layout child agent" });
  await agentCard.click();
  await expect(page.locator(".agent-detail-panel")).toBeVisible();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await expectRightPanelSpace(page, true);
  await page.getByRole("button", { name: "关闭协作代理详情", exact: true }).click();
  await expect(page.locator(".btw-panel")).toBeVisible();
  await expectRightPanelSpace(page, true);
  await page.getByRole("button", { name: "收起侧边对话", exact: true }).click();
  await expectRightPanelSpace(page, false);
  await agentCard.click();
  await expect(page.locator(".agent-detail-panel")).toBeVisible();
  await expectRightPanelSpace(page, true);
  await page.getByRole("button", { name: "关闭协作代理详情", exact: true }).click();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await expectRightPanelSpace(page, false);
  expect(relay.commands.some((command) => command.type === "close_btw"))
    .toBe(false);
});

async function coverBtwPanel(
  page: import("@playwright/test").Page,
  relay: Awaited<ReturnType<typeof mockRightPanelRelay>>,
  cover: "agent" | "diff",
) {
  if (cover === "agent") {
    relay.emit({ type: "background_process_sync", sid: "layout-parent",
      items: [{ item_id: "layout-agent", kind: "agent", status: "running",
        title: "Layout child agent" }] });
    await page.getByRole("button", { name: "Layout child agent" }).click();
    await expect(page.locator(".agent-detail-panel")).toBeVisible();
  } else {
    await page.keyboard.press("Control+Shift+b");
    await expect(page.locator(".artifact-panel")).toBeVisible();
  }
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  return cover === "agent"
    ? page.getByRole("button", { name: "关闭协作代理详情", exact: true })
    : page.locator(".artifact-panel").getByRole("button", { name: "收起", exact: true });
}

for (const cover of ["agent", "diff"] as const) {
  test(`right panel visibility keeps parent questions answerable behind ${cover}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1568, height: 881 });
    const relay = await mockRightPanelRelay(page, {
      visible: true, engine: cover === "agent" ? "claude" : "codex",
    });
    await page.goto("/");
    await expect(page.locator(".btw-panel")).toBeVisible();
    const closeCover = await coverBtwPanel(page, relay, cover);

    // Emit the parent's question last: seeing it proves the hidden side-chat
    // question has arrived, rather than passing before its frame is processed.
    relay.emit({ type: "ask_user", sid: "btw-layout-child", seq: 1,
      ask_id: "side-ask", question: "侧聊等待确认",
      options: [{ label: "确认侧聊" }] });
    relay.emit({ type: "ask_user", sid: "layout-parent", seq: 1,
      ask_id: "parent-ask", question: "主会话等待确认",
      options: [{ label: "确认主会话" }] });
    await expect(page.getByText("主会话等待确认", { exact: true })).toBeVisible();
    await expect(page.getByRole("dialog", { name: "操作确认" })).toHaveCount(1);
    await expect(page.getByText("侧聊等待确认", { exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "确认主会话", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "操作确认" })).toHaveCount(0);

    // Revealing the side chat must preserve its pending ask and its routing.
    await closeCover.click();
    await expect(page.locator(".btw-panel")).toBeVisible();
    await expect(page.getByText("侧聊等待确认", { exact: true })).toBeVisible();
    await expect(page.getByRole("dialog", { name: "操作确认" })).toHaveCount(1);
    await page.getByRole("button", { name: "确认侧聊", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "操作确认" })).toHaveCount(0);
    await expect.poll(() => relay.commands
      .filter((command) => command.type === "answer_question")
      .map(({ sid, ask_id, answer }) => ({ sid, ask_id, answer })))
      .toEqual([
        { sid: "layout-parent", ask_id: "parent-ask", answer: "确认主会话" },
        { sid: "btw-layout-child", ask_id: "side-ask", answer: "确认侧聊" },
      ]);
  });

  test(`right panel visibility retains unseen side-chat completion behind ${cover}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1568, height: 881 });
    const relay = await mockRightPanelRelay(page, {
      visible: true, engine: cover === "agent" ? "claude" : "codex",
    });
    await page.goto("/");
    await expect(page.locator(".btw-panel")).toBeVisible();
    const closeCover = await coverBtwPanel(page, relay, cover);
    relay.emit({ type: "user_msg", sid: "btw-layout-child", seq: 1,
      msg_id: "hidden-work", prompt: "后台侧聊任务" });
    relay.emit({ type: "turn_end", sid: "btw-layout-child", seq: 2,
      result: { subtype: "success", duration_ms: 1000, is_error: false } });
    relay.emit({ type: "ask_user", sid: "layout-parent", seq: 1,
      ask_id: "completion-marker", question: "完成事件已经送达",
      options: [{ label: "继续查看" }] });
    await expect(page.getByText("完成事件已经送达", { exact: true })).toBeVisible();
    const badge = page.getByText("BTW 完成", { exact: true });
    await expect(badge).toHaveCount(1);

    // Returning to this browser tab must not acknowledge a covered side chat.
    await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
    await expect(badge).toHaveCount(1);
    await page.getByRole("button", { name: "继续查看", exact: true }).click();
    await closeCover.click();
    await expect(page.locator(".btw-panel")).toBeVisible();
    await expect(badge).toHaveCount(0);

    // Conversely, finishing in the actually visible side chat is already seen.
    relay.emit({ type: "user_msg", sid: "btw-layout-child", seq: 3,
      msg_id: "visible-work", prompt: "前台侧聊任务" });
    relay.emit({ type: "turn_end", sid: "btw-layout-child", seq: 4,
      result: { subtype: "success", duration_ms: 1000, is_error: false } });
    relay.emit({ type: "ask_user", sid: "layout-parent", seq: 2,
      ask_id: "visible-completion-marker", question: "前台完成事件已经送达",
      options: [{ label: "确认完成" }] });
    await expect(page.getByText("前台完成事件已经送达", { exact: true })).toBeVisible();
    await expect(badge).toHaveCount(0);
    expect(relay.commands.some((command) => command.type === "close_btw"))
      .toBe(false);
  });
}

test("right panel visibility acknowledges only the selected side chat", async ({ page }) => {
  await page.setViewportSize({ width: 1568, height: 881 });
  const relay = await mockRightPanelRelay(page, { visible: true });
  await page.goto("/");
  await expect(page.locator(".btw-panel")).toBeVisible();
  relay.emit({ type: "btw_sync", generation: "layout-generation", revision: 2,
    sessions: [
      { btw_sid: "btw-layout-child", parent_sid: "layout-parent",
        engine: "codex", created_at: 1, state: "running" },
      { btw_sid: "btw-other-child", parent_sid: "layout-parent",
        engine: "codex", created_at: 2, state: "running" },
    ] });
  await expect(page.locator(".btw-chat-tab")).toHaveCount(2);
  relay.emit({ type: "user_msg", sid: "btw-other-child", seq: 1,
    msg_id: "other-work", prompt: "另一个侧聊任务" });
  relay.emit({ type: "turn_end", sid: "btw-other-child", seq: 2,
    result: { subtype: "success", duration_ms: 1000, is_error: false } });
  relay.emit({ type: "ask_user", sid: "layout-parent", seq: 1,
    ask_id: "other-completion-marker", question: "另一个侧聊已经完成",
    options: [{ label: "继续查看" }] });
  await expect(page.getByText("另一个侧聊已经完成", { exact: true })).toBeVisible();
  const badge = page.getByText("BTW 完成", { exact: true });
  await expect(badge).toHaveCount(1);
  await expect(page.getByRole("tab", { name: "侧聊 1", exact: true }))
    .toHaveAttribute("aria-selected", "true");
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await page.getByRole("button", { name: "继续查看", exact: true }).click();
  await expect(badge).toHaveCount(1);
  await page.getByRole("tab", { name: "另一个侧聊任务", exact: true }).click();
  await expect(badge).toHaveCount(0);
  await expect(page.getByText("session layout-p", { exact: true })).toBeVisible();
  expect(relay.commands.some((command) =>
    ["open_btw", "close_btw", "query", "interrupt"].includes(String(command.type))))
    .toBe(false);
});

test("mounted message image retries once when cache capacity is released", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?inline-image-capacity=1");

  await expect(page.getByTestId("inline-load-attempts")).toHaveText("1");
  await expect(page.getByTestId("inline-network-loads")).toHaveText("0");
  await expect(page.locator(".message-image-error"))
    .toContainText("图片暂时无法加载");

  await page.getByTestId("release-inline-capacity").click();

  await expect(page.getByTestId("inline-load-attempts")).toHaveText("2");
  await expect(page.getByTestId("inline-network-loads")).toHaveText("1");
  await expect(page.locator(".message-image-loading")).toBeVisible();
  await page.waitForTimeout(150);
  await expect(page.getByTestId("inline-load-attempts")).toHaveText("2");
  await expect(page.getByTestId("inline-network-loads")).toHaveText("1");
});

test("two visible images do not reclaim one cache slot forever", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?inline-image-eviction=1");

  await expect(page.getByTestId("visible-inline-attempts")).toHaveText("3");
  await expect(page.getByTestId("visible-inline-network-loads")).toHaveText("2");
  const visibleImages = page.getByTestId("two-visible-inline-images");
  await expect(visibleImages.getByRole("button", {
    name: "图片暂时无法加载，点击重试",
  })).toBeVisible();
  await expect(visibleImages.getByRole("img", { name: "B" })).toBeAttached();

  await page.waitForTimeout(250);
  await expect(page.getByTestId("visible-inline-attempts")).toHaveText("3");
  await expect(page.getByTestId("visible-inline-network-loads")).toHaveText("2");

  await visibleImages.getByRole("button", {
    name: "图片暂时无法加载，点击重试",
  }).click();
  await expect(page.getByTestId("visible-inline-attempts")).toHaveText("4");
  await expect(page.getByTestId("visible-inline-network-loads")).toHaveText("3");
  await expect(visibleImages.getByRole("img", { name: "A" })).toBeAttached();
  await page.waitForTimeout(250);
  await expect(page.getByTestId("visible-inline-attempts")).toHaveText("4");
  await expect(page.getByTestId("visible-inline-network-loads")).toHaveText("3");

  const noLoader = page.getByTestId("inline-no-loader");
  await expect(noLoader.getByText("图片加载失败", { exact: true })).toBeVisible();
  await expect(noLoader.getByText("图片加载超时", { exact: true })).toBeVisible();
  await expect(noLoader.locator("button.message-image-error")).toHaveCount(0);
});

const productionCsp = (() => {
  const template = readFileSync(
    new URL("../../deploy/Caddyfile", import.meta.url),
    "utf8",
  );
  const match = template.match(/Content-Security-Policy "([^"]+)"/);
  if (!match) throw new Error("production Content-Security-Policy is missing");
  return match[1].replace(
    "wss://cc-remote.example.com",
    "ws://127.0.0.1:4174",
  );
})();

async function applyProductionCsp(
  page: import("@playwright/test").Page,
): Promise<void> {
  await page.evaluate((policy) => {
    const meta = document.createElement("meta");
    meta.httpEquiv = "Content-Security-Policy";
    meta.content = policy;
    document.head.append(meta);
  }, productionCsp);
}

async function gotoWithProductionCsp(
  page: import("@playwright/test").Page,
  path: string,
): Promise<void> {
  await page.route("**/tests/history-browser.html*", async (route) => {
    const response = await route.fetch();
    const body = await response.text();
    const inlineModule = body.match(
      /<script type="module">([\s\S]*?)<\/script>/,
    )?.[1];
    const policy = inlineModule
      ? productionCsp.replace(
        "script-src 'self'",
        `script-src 'self' 'sha256-${createHash("sha256")
          .update(inlineModule)
          .digest("base64")}'`,
      )
      : productionCsp;
    await route.fulfill({
      response,
      body,
      headers: {
        ...response.headers(),
        "content-security-policy": policy,
      },
    });
  });
  await page.goto(path);
}

test("HTML preview opens interactive by default and stays isolated under production CSP", async ({
  page,
}) => {
  const outbound: string[] = [];
  await page.route("https://cc-remote-preview-test.invalid/**", (route) => {
    outbound.push(route.request().url());
    return route.fulfill({ body: "unexpected network request" });
  });
  await gotoWithProductionCsp(page, "/tests/history-browser.html?artifact-html=1");

  const previewGeometry = async () => page.locator(
    ".artifact-html-stage",
  ).evaluate((stage) => {
    const body = stage.parentElement;
    const frame = stage.querySelector("iframe");
    return {
      bodyWidth: body?.getBoundingClientRect().width ?? 0,
      stageWidth: stage.getBoundingClientRect().width,
      frameWidth: frame?.getBoundingClientRect().width ?? 0,
    };
  });
  const expectPreviewFillsBody = async () => {
    await expect.poll(async () => {
      const geometry = await previewGeometry();
      return geometry.bodyWidth > 0
        && Math.abs(geometry.stageWidth - geometry.bodyWidth) <= 1
        && Math.abs(geometry.frameWidth - geometry.bodyWidth) <= 1;
    }).toBe(true);
  };

  const interactiveFrame = page.frameLocator('iframe[title="HTML 交互预览"]');
  await expectPreviewFillsBody();
  await expect(interactiveFrame.locator("#head-style")).toHaveCSS(
    "color",
    "rgb(12, 34, 56)",
  );
  await expect(interactiveFrame.locator("#visualization-theme")).toHaveCSS(
    "color",
    "rgb(236, 237, 243)",
  );
  await expect(interactiveFrame.locator("#visualization-theme")).toHaveCSS(
    "background-color",
    "rgb(13, 14, 21)",
  );
  await expect(interactiveFrame.locator("#visualization-theme")).toHaveCSS(
    "border-top-color",
    "rgb(49, 50, 63)",
  );
  await expect(page.getByRole("button", { name: /运行交互预览|停止交互预览/ })).toHaveCount(0);
  await expectPreviewFillsBody();
  await expect(interactiveFrame.locator("body")).toHaveAttribute(
    "data-script-ran",
    "yes",
  );
  await expect(interactiveFrame.locator("body")).toHaveAttribute(
    "data-parent-blocked",
    "yes",
  );
  await expect(page.locator("body")).not.toHaveAttribute(
    "data-preview-escaped",
    "yes",
  );
  await expect(interactiveFrame.locator("body")).toHaveAttribute("data-network-blocked", "yes");
  await expect(interactiveFrame.locator("body")).toHaveAttribute("data-storage-blocked", "yes");
  await interactiveFrame.getByRole("button", { name: "计数 0", exact: true }).click();
  await expect(interactiveFrame.getByRole("button", { name: "计数 1", exact: true })).toBeVisible();
  expect(outbound).toHaveLength(0);

  await page.getByRole("button", { name: "源码", exact: true }).click();
  await page.getByRole("button", { name: "预览", exact: true }).click();
  const warmInteractiveFrame = page.frameLocator(
    'iframe[title="HTML 交互预览"]',
  );
  await expect(warmInteractiveFrame.locator("body")).toHaveAttribute(
    "data-script-ran",
    "yes",
  );
});

test("Codex visualize output opens its local HTML through the preview callback", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?codex-visualization=1");
  const card = page.getByRole("button", { name: /结构原理草图/ });
  await expect(card).toBeVisible();
  await expect(card).toContainText("HTML 可视化");
  await expect(page.locator("main")).not.toContainText("/tmp/private");
  await card.click();
  await expect(page.getByTestId("visualization-opened-path")).toHaveText(
    "/tmp/private/concept.html",
  );
});

test("Codex file citations stay inline and open PDF or GIF artifacts", async ({
  page,
}) => {
  const disclosureLoads: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/src/markdown-extras.ts")) disclosureLoads.push(request.url());
  });
  await gotoWithProductionCsp(
    page,
    "/tests/history-browser.html?codex-file-citation=1",
  );
  const pdf = page.getByRole("button", { name: /final report\.pdf/ });
  const gif = page.getByRole("button", { name: /demo} final\.gif/ });
  await expect(pdf).toBeVisible();
  await expect(gif).toBeVisible();
  await expect(page.getByTestId("valid-citations"))
    .not.toContainText("codex-file-citation");
  await expect(page.getByTestId("invalid-citation"))
    .toContainText("codex-file-citation");
  await expect(page.getByTestId("invalid-citation")).toContainText("invalid tail");
  await pdf.click();
  await expect(page.getByTestId("citation-opened-path")).toHaveText(
    "/tmp/reports/final report.pdf",
  );
  await gif.click();
  await expect(page.getByTestId("citation-opened-path")).toHaveText(
    "/tmp/demo} final.gif",
  );
  expect(disclosureLoads).toEqual([]);
});

for (const fixture of ["artifact-svg", "artifact-markdown-svg"] as const) {
  test(`${fixture} sanitizes SVG before creating a blob URL`, async ({
    page,
  }) => {
    await page.goto(`/tests/history-browser.html?${fixture}=1`);
    const image = page.getByRole("img", { name: fixture === "artifact-svg"
      ? "diagram.svg" : "diagram" });
    await expect(image).toBeVisible();
    const sanitized = await image.evaluate(async (node) => {
      const src = (node as HTMLImageElement).src;
      return {
        src,
        text: await (await fetch(src)).text(),
      };
    });
    expect(sanitized.src).toMatch(/^blob:/);
    expect(sanitized.text).toContain("safe-svg-rect");
    expect(sanitized.text).not.toMatch(
      /<script|foreignObject|example\.com|<image/i,
    );
    await applyProductionCsp(page);
    await expect(image).toBeVisible();
  });
}

test("artifact-pdf renders paged PDF content under the production CSP", async ({
  page,
}) => {
  await gotoWithProductionCsp(page, "/tests/history-browser.html?artifact-pdf=1");

  const canvas = page.locator(".artifact-pdf-page canvas");
  await expect(page.locator(".artifact-pdf-controls")).toContainText("1 / 2 页");
  await expect(canvas).toBeVisible();
  await expect.poll(() => canvas.evaluate((node) => {
    const target = node as HTMLCanvasElement;
    const context = target.getContext("2d");
    if (!context || target.width === 0 || target.height === 0) return false;
    const pixel = context.getImageData(
      Math.floor(target.width / 2),
      Math.floor(target.height / 2),
      1,
      1,
    ).data;
    return pixel[2] > pixel[0] && pixel[3] === 255;
  })).toBe(true);
  await expect(page.getByRole("button", { name: "下一页" })).toBeEnabled();

  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.locator(".artifact-pdf-controls")).toContainText("2 / 2 页");
  await expect.poll(() => canvas.evaluate((node) => {
    const target = node as HTMLCanvasElement;
    const context = target.getContext("2d");
    if (!context || target.width === 0 || target.height === 0) return false;
    const pixel = context.getImageData(
      Math.floor(target.width / 2),
      Math.floor(target.height / 2),
      1,
      1,
    ).data;
    return pixel[0] > pixel[2] && pixel[3] === 255;
  })).toBe(true);
  await expect(page.locator(".artifact-pdf-stage iframe")).toHaveCount(0);
  await expect(page.locator(".artifact-pdf-stage .preview-error")).toHaveCount(0);
});

test("artifact-gif preserves native animation under the production CSP", async ({
  page,
}) => {
  await gotoWithProductionCsp(page, "/tests/history-browser.html?artifact-gif=1");
  const image = page.getByRole("img", { name: "animation.gif" });
  await expect(image).toBeVisible();
  await expect.poll(() => image.evaluate((node) => ({
    width: (node as HTMLImageElement).naturalWidth,
    height: (node as HTMLImageElement).naturalHeight,
  }))).toEqual({ width: 16, height: 16 });

  const frames = new Set<string>();
  for (let index = 0; index < 8; index += 1) {
    const screenshot = await image.screenshot({ animations: "allow" });
    frames.add(createHash("sha256").update(screenshot).digest("hex"));
    await page.waitForTimeout(70);
  }
  expect(frames.size).toBeGreaterThan(1);
});

test("artifact-invalid-gif reports a decode error instead of a broken image", async ({
  page,
}) => {
  await gotoWithProductionCsp(
    page,
    "/tests/history-browser.html?artifact-invalid-gif=1",
  );
  await expect(page.locator(".preview-error")).toContainText(
    "图片无法解码或格式不受支持",
  );
  await expect(page.getByRole("img", { name: "animation.gif" })).toHaveCount(0);
});

test("Markdown HTML preview renders README layout without executing active HTML", async ({
  page,
}, testInfo) => {
  const requested: string[] = [];
  page.on("request", (request) => {
    if (/preview\.example|header\.svg|local-logo\.png|\/private\/local\.png/.test(request.url())) {
      requested.push(request.url());
    }
  });
  await page.route("https://preview.example/**", (route) => route.abort());
  await gotoWithProductionCsp(page, "/tests/history-browser.html?artifact-markdown-html=1");
  const preview = page.locator(".markdown-preview");
  const heading = preview.getByRole("heading", { name: "Microduck", exact: true });
  await expect(heading).toBeVisible();
  await expect(heading).toHaveCSS("text-align", "center");
  await expect(preview).not.toContainText('<p align="center">');
  const hero = preview.getByRole("img", { name: "Robot overview" });
  await expect(hero).toBeVisible();
  await expect(hero).toHaveAttribute("width", "820");
  await expect(hero).toHaveAttribute("height", "320");
  await expect.poll(() => hero.evaluate((node) =>
    (node as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
  const geometry = await hero.evaluate((node) => {
    const image = node.getBoundingClientRect();
    const container = node.closest(".markdown-preview")!.getBoundingClientRect();
    return { image: image.width, container: container.width };
  });
  expect(geometry.image).toBeLessThanOrEqual(geometry.container + 1);
  await preview.screenshot({ path: testInfo.outputPath("markdown-html-preview.png") });

  const local = preview.getByRole("img", { name: "Local logo" });
  await expect(local).toHaveAttribute("src", /^data:image\/png;base64,/);
  await expect(local).toHaveAttribute("width", "64");
  await expect.poll(() => local.evaluate((node) =>
    (node as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
  await preview.getByRole("link", { name: "English", exact: true }).click();
  await expect(page.getByTestId("artifact-opened-file")).toHaveText("README.md:");
  await preview.getByRole("link", { name: "安装指南", exact: true }).click();
  await expect(page.getByTestId("artifact-opened-file")).toHaveText("docs/install.md:12");
  await expect(preview.getByRole("link", { name: "官方项目" })).toHaveAttribute("target", "_blank");
  await preview.locator("summary").click();
  await expect(preview.locator("details")).toHaveAttribute("open", "");
  await expect(preview.locator("details strong")).toHaveText("Markdown");
  await preview.getByRole("link", { name: "跳转到安装" }).click();
  await expect(page).toHaveURL(/#cc-preview-installation$/);
  await expect(preview.locator("#cc-preview-installation")).toBeVisible();

  await expect(preview.locator("script,style,iframe,object,embed,form,source,svg,math")).toHaveCount(0);
  // Screenshot capture can restore an empty style attribute on checkboxes.
  await expect(preview.locator('[style]:not([style=""]),[onclick],[onerror],[srcset]')).toHaveCount(0);
  await expect(page.locator("body")).not.toHaveAttribute("data-md-unsafe", /.+/);
  await expect(preview.getByRole("checkbox")).toBeChecked();
  await expect(preview.getByRole("checkbox")).toBeDisabled();
  expect(requested).toEqual([]);
  await page.getByRole("button", { name: "源码", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Markdown 源码编辑器" }))
    .toHaveValue(MARKDOWN_HTML_LOCAL_README);
  await page.getByRole("button", { name: "预览", exact: true }).click();
  await expect(heading).toBeVisible();
  await expect(page.locator("body")).not.toHaveAttribute("data-md-unsafe", /.+/);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => hero.evaluate((node) => {
    const bounds = node.getBoundingClientRect();
    return bounds.width > 0 && bounds.right <= window.innerWidth + 1;
  })).toBe(true);
});

test("Markdown HTML preview allows GitHub images without opening other network capabilities", async ({
  page,
}) => {
  const requested: { url: string; referer?: string }[] = [];
  // Fulfill every external request locally. The CSP, not a mock abort, must
  // prevent unlisted images, remote scripts, and fetches from reaching here.
  await page.route(/^https?:\/\/(?!127\.0\.0\.1(?::|\/))/, (route) => {
    const request = route.request();
    const url = request.url();
    requested.push({ url, referer: request.headers().referer });
    if (request.resourceType() === "script") {
      return route.fulfill({ contentType: "application/javascript",
        body: 'document.body.dataset.unexpectedGithubScript = "yes";' });
    }
    return route.fulfill({ contentType: "image/svg+xml", body: MARKDOWN_HTML_HEADER_SVG });
  });
  await gotoWithProductionCsp(page, "/tests/history-browser.html?artifact-markdown-github-html=1");
  const hero = page.getByRole("img", { name: "Robot overview" });
  await expect(hero).toHaveAttribute("src", GITHUB_README_ATTACHMENT_URL);
  await expect(hero).toHaveAttribute("referrerpolicy", "no-referrer");
  await expect.poll(() => hero.evaluate((node) =>
    (node as HTMLImageElement).naturalWidth)).toBe(820);
  expect(requested).toEqual([
    { url: GITHUB_README_ATTACHMENT_URL, referer: undefined },
  ]);

  // Exercise the attachment destination separately: Playwright's WebKit
  // route.fulfill cannot synthesize 3xx responses. Both real redirect origins
  // must be admitted by the production policy, not only github.com.
  const allowed = [
    GITHUB_README_IMAGE_URL,
    "https://raw.githubusercontent.com/test/readme.svg",
    "https://user-images.githubusercontent.com/test/readme.svg",
    "https://private-user-images.githubusercontent.com/test/readme.svg",
    "https://camo.githubusercontent.com/test/readme.svg",
    "https://avatars.githubusercontent.com/test/readme.svg",
  ];
  const blocked = [
    "https://preview.example/image.svg",
    "https://raw.githubusercontent.com.evil.example/image.svg",
    "https://github.com/not-an-attachment/image.svg",
    "https://other-bucket.s3.amazonaws.com/image.svg",
    "http://raw.githubusercontent.com/test/insecure.svg",
  ];
  const imageResults = await page.evaluate(async ({ allowed, blocked }) => {
    return Promise.all([...allowed, ...blocked].map((src) => new Promise<boolean>((resolve) => {
      const image = document.createElement("img");
      image.referrerPolicy = "no-referrer";
      image.onload = () => { image.remove(); resolve(true); };
      image.onerror = () => { image.remove(); resolve(false); };
      image.src = src;
      document.body.append(image);
    })));
  }, { allowed, blocked });
  expect(imageResults).toEqual([...allowed.map(() => true), ...blocked.map(() => false)]);
  const remoteScript = "https://raw.githubusercontent.com/test/blocked.js";
  const remoteFetch = "https://raw.githubusercontent.com/test/blocked.json";
  const otherCapabilities = await page.evaluate(async ({ remoteScript, remoteFetch }) => {
    const scriptLoaded = new Promise<boolean>((resolve) => {
      const script = document.createElement("script");
      script.onload = () => { script.remove(); resolve(true); };
      script.onerror = () => { script.remove(); resolve(false); };
      script.src = remoteScript;
      document.head.append(script);
    });
    const fetched = fetch(remoteFetch).then(() => true, () => false);
    return Promise.all([scriptLoaded, fetched]);
  }, { remoteScript, remoteFetch });
  expect(otherCapabilities).toEqual([false, false]);
  await expect(page.locator("body")).not.toHaveAttribute("data-unexpected-github-script", /.+/);
  expect(requested.map(({ url }) => url).sort()).toEqual([
    GITHUB_README_ATTACHMENT_URL, ...allowed,
  ].sort());
  expect(requested.every(({ referer }) => referer === undefined)).toBe(true);
});

test("mobile Markdown source editor fills the available artifact body", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/tests/history-browser.html?artifact-markdown-source=1");
  await page.getByRole("button", { name: "源码" }).click();
  const editor = page.getByRole("textbox", { name: "Markdown 源码编辑器" });
  await expect(editor).toBeVisible();

  const measure = () => page.locator(".source-artifact-body").evaluate((body) => {
    const textarea = body.querySelector<HTMLTextAreaElement>(".markdown-editor");
    if (!textarea) throw new Error("Markdown editor is missing");
    const bodyRect = body.getBoundingClientRect();
    const editorRect = textarea.getBoundingClientRect();
    return {
      bodyHeight: bodyRect.height,
      editorHeight: editorRect.height,
      bottomGap: bodyRect.bottom - editorRect.bottom,
      scrollHeight: textarea.scrollHeight,
      clientHeight: textarea.clientHeight,
    };
  });

  let geometry = await measure();
  expect(geometry.editorHeight).toBeGreaterThan(geometry.bodyHeight * 0.8);
  expect(geometry.bottomGap).toBeLessThanOrEqual(16);
  expect(geometry.scrollHeight).toBeGreaterThan(geometry.clientHeight);

  await page.setViewportSize({ width: 390, height: 480 });
  geometry = await measure();
  expect(geometry.editorHeight).toBeGreaterThan(geometry.bodyHeight * 0.75);
  expect(geometry.bottomGap).toBeLessThanOrEqual(16);
});

test("dark desktop code block and copy action stay visually distinct", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/tests/history-browser.html?code-copy-theme=1&theme=dark");
  await page.waitForFunction(() =>
    document.documentElement.dataset.theme === "dark"
  );
  await page.waitForTimeout(200);
  const copy = page.getByRole("button", { name: "复制代码" });
  await expect(copy).toBeVisible();

  const appearance = await copy.evaluate((button) => {
    const sample = (color: string) => {
      const canvas = document.createElement("canvas");
      canvas.width = 1;
      canvas.height = 1;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("canvas context unavailable");
      context.clearRect(0, 0, 1, 1);
      context.fillStyle = color;
      context.fillRect(0, 0, 1, 1);
      return Array.from(context.getImageData(0, 0, 1, 1).data);
    };
    const composite = (front: number[], back: number[]) => {
      const alpha = front[3] / 255;
      return front.slice(0, 3).map((value, index) =>
        value * alpha + back[index] * (1 - alpha)
      );
    };
    const luminance = (color: number[]) => {
      const channels = color.slice(0, 3).map((value) => {
        const normalized = value / 255;
        return normalized <= 0.04045
          ? normalized / 12.92
          : ((normalized + 0.055) / 1.055) ** 2.4;
      });
      return channels[0] * 0.2126 + channels[1] * 0.7152
        + channels[2] * 0.0722;
    };
    const block = button.closest(".message-code-block");
    const code = block?.querySelector("pre");
    const pageSurface = button.closest("main");
    if (!code || !pageSurface) throw new Error("code block is missing");
    const foreground = sample(getComputedStyle(button).color);
    const expectedForeground = sample(
      getComputedStyle(document.documentElement).getPropertyValue("--text"),
    );
    const buttonBackground = sample(getComputedStyle(button).backgroundColor);
    const codeBackground = sample(getComputedStyle(code).backgroundColor);
    const pageBackground = sample(getComputedStyle(pageSurface).backgroundColor);
    const effectiveCodeBackground = composite(codeBackground, pageBackground);
    const effectiveButtonBackground = composite(
      buttonBackground,
      effectiveCodeBackground,
    );
    const lighter = Math.max(
      luminance(foreground),
      luminance(effectiveButtonBackground),
    );
    const darker = Math.min(
      luminance(foreground),
      luminance(effectiveButtonBackground),
    );
    const codeLighter = Math.max(
      luminance(effectiveCodeBackground),
      luminance(pageBackground),
    );
    const codeDarker = Math.min(
      luminance(effectiveCodeBackground),
      luminance(pageBackground),
    );
    const codeBackgroundDelta = effectiveCodeBackground.reduce(
      (total, value, index) => total + Math.abs(value - pageBackground[index]),
      0,
    ) / 3;
    return {
      contrast: (lighter + 0.05) / (darker + 0.05),
      codeBackgroundContrast: (codeLighter + 0.05) / (codeDarker + 0.05),
      codeBackgroundDelta,
      foreground,
      expectedForeground,
    };
  });

  expect(appearance.contrast).toBeGreaterThanOrEqual(4.5);
  expect(appearance.codeBackgroundContrast).toBeGreaterThanOrEqual(1.28);
  expect(appearance.codeBackgroundDelta).toBeGreaterThanOrEqual(24);
  expect(appearance.foreground).toEqual(appearance.expectedForeground);
});

test("local Markdown file link reveals its complete path without native title", async ({
  page,
}) => {
  await page.setViewportSize({ width: 720, height: 480 });
  await page.goto("/tests/history-browser.html?local-file-link=1");
  const link = page.getByRole("button", {
    name: "在 Remote 中打开 /tmp/qwen3-tts-v017-release-test:42",
  });
  await expect(link).not.toHaveAttribute("title");

  await link.hover();
  const tooltip = page.getByRole("tooltip");
  await expect(tooltip).toContainText("/tmp/qwen3-tts-v017-release-test:42");
  await expect(tooltip).toBeInViewport();

  await tooltip.hover();
  await page.waitForTimeout(260);
  await expect(tooltip).toBeVisible();
  const path = tooltip.locator(".message-file-tooltip-path");
  await path.dblclick();
  await expect.poll(() => page.evaluate(() => getSelection()?.toString()))
    .toBe("/tmp/qwen3-tts-v017-release-test:42");
  await expect(tooltip.getByRole("button")).toHaveCount(0);

  await page.mouse.move(700, 460);
  await expect(tooltip).toHaveCount(0, { timeout: 1000 });
  await link.focus();
  await expect(page.getByRole("tooltip")).toBeVisible();
});

async function pinchThenPanPreview(
  page: import("@playwright/test").Page,
): Promise<{
  afterPinch: { scale: number; x: number; y: number };
  afterPan: { scale: number; x: number; y: number };
}> {
  return page.locator(".image-lightbox").evaluate(async (node) => {
    const stage = node as HTMLElement;
    const visual = stage.querySelector<HTMLElement>(".image-lightbox-visual");
    if (!visual) throw new Error("lightbox visual is missing");
    Object.defineProperties(stage, {
      setPointerCapture: { configurable: true, value: () => {} },
      releasePointerCapture: { configurable: true, value: () => {} },
      hasPointerCapture: { configurable: true, value: () => false },
    });
    const emit = (
      type: "pointerdown" | "pointermove" | "pointerup"
        | "lostpointercapture",
      pointerId: number,
      x: number,
      y: number,
    ) => stage.dispatchEvent(new PointerEvent(type, {
      bubbles: true,
      cancelable: true,
      pointerId,
      pointerType: "touch",
      clientX: x,
      clientY: y,
      buttons: type === "pointerup" || type === "lostpointercapture" ? 0 : 1,
    }));
    const nextPaint = () => new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    });
    const transform = () => {
      const matrix = new DOMMatrix(visual.style.transform);
      return {
        x: matrix.e,
        y: matrix.f,
        scale: Math.hypot(matrix.a, matrix.b),
      };
    };

    emit("pointerdown", 1, 150, 260);
    emit("pointerdown", 2, 250, 260);
    emit("pointermove", 1, 50, 260);
    emit("pointermove", 2, 350, 260);
    await nextPaint();
    const afterPinch = transform();

    emit("lostpointercapture", 2, 350, 260);
    emit("pointermove", 1, 0, 300);
    await nextPaint();
    const afterPan = transform();
    emit("pointerup", 1, 50, 300);
    stage.dispatchEvent(new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      detail: 1,
    }));
    return { afterPinch, afterPan };
  });
}

async function wheelZoomThenPanPreview(
  page: import("@playwright/test").Page,
): Promise<{
  zoomPrevented: boolean;
  panPrevented: boolean;
  afterZoom: { scale: number; x: number; y: number };
  afterPan: { scale: number; x: number; y: number };
}> {
  return page.locator(".image-lightbox").evaluate(async (node) => {
    const stage = node as HTMLElement;
    const visual = stage.querySelector<HTMLElement>(".image-lightbox-visual");
    if (!visual) throw new Error("lightbox visual is missing");
    Object.defineProperties(stage, {
      clientWidth: { configurable: true, value: 900 },
      clientHeight: { configurable: true, value: 720 },
    });
    Object.defineProperties(visual, {
      clientWidth: { configurable: true, value: 600 },
      clientHeight: { configurable: true, value: 400 },
    });
    const nextPaint = () => new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    });
    const transform = () => {
      const matrix = new DOMMatrix(visual.style.transform);
      return {
        x: matrix.e,
        y: matrix.f,
        scale: Math.hypot(matrix.a, matrix.b),
      };
    };
    const zoomPrevented = !stage.dispatchEvent(new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      ctrlKey: true,
      clientX: 450,
      clientY: 360,
      deltaY: -600,
      deltaMode: WheelEvent.DOM_DELTA_PIXEL,
    }));
    await nextPaint();
    const afterZoom = transform();
    const panPrevented = !stage.dispatchEvent(new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      clientX: 450,
      clientY: 360,
      deltaX: 45,
      deltaY: 30,
      deltaMode: WheelEvent.DOM_DELTA_PIXEL,
    }));
    await nextPaint();
    return {
      zoomPrevented,
      panPrevented,
      afterZoom,
      afterPan: transform(),
    };
  });
}

async function readingAnchor(page: import("@playwright/test").Page): Promise<{
  id: string;
  offset: number;
}> {
  return page.evaluate(() => {
    const viewport = document.querySelector<HTMLElement>(".thread");
    if (!viewport) throw new Error("thread viewport is missing");
    const viewportRect = viewport.getBoundingClientRect();
    const rows = [...document.querySelectorAll<HTMLElement>("[data-turn-id]")]
      .map((row) => ({ row, rect: row.getBoundingClientRect() }))
      .filter(({ rect }) =>
        rect.bottom > viewportRect.top && rect.top < viewportRect.bottom);
    const selected = rows.sort((left, right) =>
      Math.abs(left.rect.top - viewportRect.top)
      - Math.abs(right.rect.top - viewportRect.top))[0];
    const id = selected?.row.dataset.turnId;
    if (!selected || !id) throw new Error("no visible reading anchor");
    return { id, offset: selected.rect.top - viewportRect.top };
  });
}

test("Codex settings opens the responsive daily usage activity view", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?header-menu=1&engine=codex");
  await page.getByRole("button", { name: "更多设置" }).click();
  const activityEntry = page.getByRole("button", { name: /使用活动/ });
  await expect(activityEntry).toBeVisible();
  await activityEntry.click();

  const dialog = page.getByRole("dialog", { name: "Codex 使用活动" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("累计 Token");
  await expect(dialog).toContainText("9.88亿");
  await expect(dialog).toContainText("单日峰值");
  await expect(dialog).toContainText("最长任务");
  await expect(dialog).toContainText("当前连续");
  await expect(dialog).toContainText("最长连续");
  await expect(dialog.locator(".usage-activity-tile")).toHaveCount(371);

  const geometry = await page.locator(".usage-activity-viewport")
    .evaluate((viewport) => ({
      viewportWidth: window.innerWidth,
      pageScrollWidth: document.documentElement.scrollWidth,
      clientWidth: viewport.clientWidth,
      scrollWidth: viewport.scrollWidth,
      scrollLeft: viewport.scrollLeft,
    }));
  expect(geometry.pageScrollWidth).toBeLessThanOrEqual(geometry.viewportWidth);
  if (geometry.scrollWidth > geometry.clientWidth) {
    expect(geometry.scrollLeft).toBeGreaterThan(0);
  }

  const busiest = dialog.locator('.usage-activity-tile[data-level="4"]')
    .first();
  await expect(busiest).toHaveAttribute(
    "title", /使用了 \d+(?:\.\d+)?万 个 Token/,
  );
  await busiest.click();
  await expect(dialog.locator(".usage-activity-caption"))
    .toContainText("Token");
});


test("Claude settings does not expose Codex usage activity", async ({ page }) => {
  await page.goto("/tests/history-browser.html?header-menu=1&engine=claude");
  await page.getByRole("button", { name: "更多设置" }).click();
  await expect(page.getByRole("button", { name: /使用活动/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /通知/ })).toBeVisible();
});

async function maxPaintedTurnOffsetShiftThroughAction(
  page: import("@playwright/test").Page,
  turnId: string,
  actionTestId: string,
  frameCount = 36,
): Promise<{ maxShift: number; missing: boolean }> {
  return page.evaluate(async ({ id, testId, frames }) => {
    const viewport = document.querySelector<HTMLElement>(".thread");
    const row = document.querySelector<HTMLElement>(
      `[data-turn-id="${CSS.escape(id)}"]`,
    );
    const action = document.querySelector<HTMLElement>(
      `[data-testid="${CSS.escape(testId)}"]`,
    );
    if (!viewport || !row || !action) {
      throw new Error("frame sampling target is missing");
    }
    const viewportTop = viewport.getBoundingClientRect().top;
    const initial = row.getBoundingClientRect().top - viewportTop;
    let maxShift = 0;
    let missing = false;
    action.click();
    await new Promise<void>((resolve) => {
      let remaining = frames;
      const sample = () => {
        const current = document.querySelector<HTMLElement>(
          `[data-turn-id="${CSS.escape(id)}"]`,
        );
        if (!current) {
          missing = true;
        } else {
          const offset = current.getBoundingClientRect().top
            - viewport.getBoundingClientRect().top;
          maxShift = Math.max(maxShift, Math.abs(offset - initial));
        }
        remaining -= 1;
        if (remaining <= 0) resolve();
        else requestAnimationFrame(() => window.setTimeout(sample, 0));
      };
      // rAF runs before ResizeObserver delivery. Sample after the paint
      // opportunity so the test observes user-visible frames, not the
      // browser's internal pre-observer layout checkpoint.
      requestAnimationFrame(() => window.setTimeout(sample, 0));
    });
    return { maxShift, missing };
  }, { id: turnId, testId: actionTestId, frames: frameCount });
}

async function maxTurnOffsetShiftThroughTouchHistoryLoad(
  page: import("@playwright/test").Page,
  turnId: string,
  frameCount = 60,
): Promise<{ maxShift: number; missing: boolean }> {
  return page.evaluate(async ({ id, frames }) => {
    const viewport = document.querySelector<HTMLElement>(".thread");
    const row = document.querySelector<HTMLElement>(
      `[data-turn-id="${CSS.escape(id)}"]`,
    );
    if (!viewport || !row) throw new Error("touch frame target is missing");
    const viewportTop = viewport.getBoundingClientRect().top;
    const initial = row.getBoundingClientRect().top - viewportTop;
    let maxShift = 0;
    let missing = false;
    const dispatch = (type: "touchstart" | "touchmove" | "touchend",
      clientY: number) => {
      const touch = {
        identifier: 1,
        target: viewport,
        clientX: 120,
        clientY,
      };
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperties(event, {
        touches: { value: type === "touchend" ? [] : [touch] },
        targetTouches: { value: type === "touchend" ? [] : [touch] },
        changedTouches: { value: [touch] },
      });
      viewport.dispatchEvent(event);
    };
    dispatch("touchstart", 160);
    dispatch("touchmove", 220);
    dispatch("touchend", 220);
    await new Promise<void>((resolve) => {
      let remaining = frames;
      const sample = () => {
        const current = document.querySelector<HTMLElement>(
          `[data-turn-id="${CSS.escape(id)}"]`,
        );
        if (!current) {
          missing = true;
        } else {
          const offset = current.getBoundingClientRect().top
            - viewport.getBoundingClientRect().top;
          maxShift = Math.max(maxShift, Math.abs(offset - initial));
        }
        remaining -= 1;
        if (remaining <= 0) resolve();
        else requestAnimationFrame(sample);
      };
      requestAnimationFrame(sample);
    });
    return { maxShift, missing };
  }, { id: turnId, frames: frameCount });
}

async function processDetailEdge(
  page: import("@playwright/test").Page,
  edge: "start" | "end",
): Promise<number> {
  return page.locator('[data-turn-id="detail-page"]').evaluate(
    (turn, selectedEdge) => {
      const viewport = document.querySelector<HTMLElement>(".thread");
      const process = turn.querySelector<HTMLElement>(
        "[data-process-detail-root]",
      );
      if (!viewport || !process) throw new Error("detail process is missing");
      const viewportRect = viewport.getBoundingClientRect();
      const processRect = process.getBoundingClientRect();
      return (selectedEdge === "end" ? processRect.bottom : processRect.top)
        - viewportRect.top;
    },
    edge,
  );
}

async function turnIntersectsViewport(
  page: import("@playwright/test").Page,
  turnId: string,
): Promise<boolean> {
  return page.evaluate((id) => {
    const viewport = document.querySelector<HTMLElement>(".thread");
    const row = document.querySelector<HTMLElement>(
      `[data-turn-id="${CSS.escape(id)}"]`,
    );
    if (!viewport || !row) return false;
    const viewportRect = viewport.getBoundingClientRect();
    const rowRect = row.getBoundingClientRect();
    return rowRect.bottom > viewportRect.top && rowRect.top < viewportRect.bottom;
  }, turnId);
}

async function waitForScrollIdle(
  page: import("@playwright/test").Page,
): Promise<void> {
  let previous: number | null = null;
  let stableSamples = 0;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const current = await page.locator(".thread").evaluate(
      (node) => node.scrollTop,
    );
    stableSamples = previous != null && Math.abs(current - previous) < 0.5
      ? stableSamples + 1 : 0;
    if (stableSamples >= 4) return;
    previous = current;
    await page.waitForTimeout(50);
  }
  throw new Error("thread scroll position did not settle");
}

async function pauseOutputAndScrollToHistoryStart(
  page: import("@playwright/test").Page,
): Promise<void> {
  const viewport = page.locator(".thread");
  // Initial virtual measurements can finish one frame after the first write
  // and reassert the mounted tail. Retry the neutral test setup until the
  // physical viewport itself is stable at the history edge; no user-intent
  // event is emitted here, so the page request still belongs to the action
  // under test.
  for (let attempt = 0; attempt < 6; attempt += 1) {
    await viewport.evaluate((node) => { node.scrollTop = 0; });
    await waitForScrollIdle(page);
    if (await viewport.evaluate((node) => node.scrollTop <= 1)) return;
  }
  expect(await viewport.evaluate((node) => node.scrollTop)).toBeLessThanOrEqual(1);
}

async function waitForReadingPositionIdle(
  page: import("@playwright/test").Page,
): Promise<void> {
  let previous: { id: string; offset: number } | null = null;
  let stableSamples = 0;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const current = await readingAnchor(page).catch(() => null);
    if (!current) {
      previous = null;
      stableSamples = 0;
      await page.waitForTimeout(50);
      continue;
    }
    stableSamples = previous != null
      && current.id === previous.id
      && Math.abs(current.offset - previous.offset) < 0.5
      ? stableSamples + 1 : 0;
    if (stableSamples >= 4) return;
    previous = current;
    await page.waitForTimeout(50);
  }
  throw new Error("thread visual reading position did not settle");
}

async function nativeSelectionSnapshot(
  page: import("@playwright/test").Page,
): Promise<{
  anchorTurnId: string | null;
  focusTurnId: string | null;
  anchorConnected: boolean;
  text: string;
}> {
  return page.evaluate(() => {
    const selection = window.getSelection();
    const turnId = (node: Node | null): string | null => {
      const element = node instanceof Element ? node : node?.parentElement;
      return element?.closest<HTMLElement>("[data-turn-id]")
        ?.dataset.turnId ?? null;
    };
    return {
      anchorTurnId: turnId(selection?.anchorNode ?? null),
      focusTurnId: turnId(selection?.focusNode ?? null),
      anchorConnected: selection?.anchorNode?.isConnected ?? false,
      text: selection?.toString() ?? "",
    };
  });
}

async function textSelectionPoint(
  locator: import("@playwright/test").Locator,
): Promise<{ x: number; y: number }> {
  const point = await locator.evaluate((node) => {
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    const text = walker.nextNode();
    if (!text || !text.textContent?.length) return null;
    const range = document.createRange();
    range.setStart(text, 0);
    range.setEnd(text, Math.min(2, text.textContent.length));
    const rect = range.getBoundingClientRect();
    return { x: rect.left + 1, y: rect.top + rect.height / 2 };
  });
  if (!point) throw new Error("selection text has no geometry");
  return point;
}

function isMobileWebKitProject(projectName: string): boolean {
  return projectName.startsWith("webkit") && projectName !== "webkit-desktop-selection";
}

async function dispatchCancelledTouchTap(
  locator: import("@playwright/test").Locator,
  pointerId: number,
): Promise<void> {
  await locator.evaluate((node, id) => {
    const target = node as HTMLElement;
    let capturedPointer: number | null = null;
    let releases = 0;
    Object.defineProperties(target, {
      setPointerCapture: {
        configurable: true,
        value: (candidate: number) => { capturedPointer = candidate; },
      },
      hasPointerCapture: {
        configurable: true,
        value: (candidate: number) => capturedPointer === candidate,
      },
      releasePointerCapture: {
        configurable: true,
        value: (candidate: number) => {
          if (capturedPointer === candidate) capturedPointer = null;
          releases += 1;
        },
      },
    });
    target.focus();
    const bounds = target.getBoundingClientRect();
    const clientX = bounds.left + bounds.width / 2;
    const clientY = bounds.top + bounds.height / 2;
    target.dispatchEvent(new PointerEvent("pointerdown", {
      bubbles: true,
      cancelable: true,
      pointerId: id,
      pointerType: "touch",
      clientX,
      clientY,
      buttons: 1,
    }));
    target.dispatchEvent(new PointerEvent("pointercancel", {
      bubbles: true,
      cancelable: true,
      pointerId: id,
      pointerType: "touch",
      clientX,
      clientY,
    }));
    target.dispatchEvent(new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      clientX,
      clientY,
      detail: 1,
    }));
    target.dataset.cancelReleaseCount = String(releases);
    target.dataset.cancelCaptureActive = String(capturedPointer === id);
    target.dataset.cancelStillFocused = String(document.activeElement === target);
  }, pointerId);
  await expect(locator).toHaveAttribute("data-cancel-release-count", "1");
  await expect(locator).toHaveAttribute("data-cancel-capture-active", "false");
  await expect(locator).toHaveAttribute("data-cancel-still-focused", "false");
}

async function wheelUntilTurn(
  page: import("@playwright/test").Page,
  turnId: string,
  deltaY: number,
  projectName: string,
): Promise<void> {
  const viewport = page.locator(".thread");
  if (isMobileWebKitProject(projectName)) {
    for (let attempt = 0; attempt < 40; attempt += 1) {
      if (await turnIntersectsViewport(page, turnId)) {
        if (deltaY < 0) {
          await expect(page.locator(".scroll-bottom-btn")).toBeVisible();
        }
        return;
      }
      await dispatchTouchGesture(page, deltaY < 0 ? 60 : -60);
      await viewport.evaluate((node, delta) => {
        const step = Math.max(
          160,
          Math.min(Math.abs(delta), node.clientHeight * 5),
        );
        node.scrollBy({
          top: Math.sign(delta) * step,
          behavior: "auto",
        });
      }, deltaY);
      await waitForScrollIdle(page);
    }
    expect(await turnIntersectsViewport(page, turnId)).toBe(true);
    return;
  }
  const box = await viewport.boundingBox();
  if (!box) throw new Error("thread viewport has no bounds");
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (await turnIntersectsViewport(page, turnId)) return;
    await page.mouse.wheel(0, deltaY);
    await page.waitForTimeout(40);
  }
  expect(await turnIntersectsViewport(page, turnId)).toBe(true);
}

async function scrollThreadToEdge(
  page: import("@playwright/test").Page,
  edge: "start" | "end",
  projectName: string,
): Promise<void> {
  const viewport = page.locator(".thread");
  for (let attempt = 0; attempt < 6; attempt += 1) {
    if (isMobileWebKitProject(projectName)) {
      await dispatchTouchGesture(page, edge === "start" ? 60 : -60);
    } else {
      await viewport.dispatchEvent("wheel", {
        deltaY: edge === "start" ? -80 : 80,
      });
    }
    await viewport.evaluate((node, selectedEdge) => {
      node.scrollTo({
        top: selectedEdge === "start" ? 0 : node.scrollHeight,
        behavior: "auto",
      });
      // Synthetic touch events mark genuine reader intent, but Playwright
      // WebKit does not perform the browser's native pan for them. Deliver the
      // matching scroll notification after the fixture's explicit scrollTop
      // write so React and the virtualizer observe the same offset before the
      // next dynamic row measurement.
      node.dispatchEvent(new Event("scroll"));
    }, edge);
    await waitForScrollIdle(page);
    const reached = await viewport.evaluate((node, selectedEdge) => {
      if (selectedEdge === "start") return node.scrollTop <= 1;
      return node.scrollHeight - node.scrollTop - node.clientHeight <= 1;
    }, edge);
    if (reached) {
      if (edge === "start") {
        await expect(page.locator(".scroll-bottom-btn")).toBeVisible();
      }
      return;
    }
  }
  expect(await viewport.evaluate((node, selectedEdge) => {
    if (selectedEdge === "start") return node.scrollTop <= 1;
    return node.scrollHeight - node.scrollTop - node.clientHeight <= 1;
  }, edge)).toBe(true);
}

async function dispatchTouchGesture(
  page: import("@playwright/test").Page,
  fingerDeltaY: number,
  moves = 1,
): Promise<void> {
  await page.locator(".thread").evaluate((node, input) => {
    const target = node as HTMLElement;
    const dispatchTouch = (
      type: "touchstart" | "touchmove" | "touchend",
      clientY: number,
    ) => {
      // WebKit's Touch constructor is intentionally not public. React only
      // needs the TouchEvent list shape, so define it on a real bubbling Event.
      const touch = { identifier: 1, target, clientX: 120, clientY };
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperties(event, {
        touches: { value: type === "touchend" ? [] : [touch] },
        targetTouches: { value: type === "touchend" ? [] : [touch] },
        changedTouches: { value: [touch] },
      });
      target.dispatchEvent(event);
    };
    const startY = 160;
    dispatchTouch("touchstart", startY);
    for (let index = 0; index < input.moves; index += 1) {
      dispatchTouch("touchmove", startY + input.fingerDeltaY * (index + 1));
    }
    dispatchTouch("touchend", startY + input.fingerDeltaY * input.moves);
  }, { fingerDeltaY, moves });
}

async function dispatchTouchPhase(
  page: import("@playwright/test").Page,
  type: "touchstart" | "touchmove" | "touchend",
  clientY: number,
): Promise<void> {
  await page.locator(".thread").evaluate((node, input) => {
    const target = node as HTMLElement;
    const touch = {
      identifier: 1,
      target,
      clientX: 120,
      clientY: input.clientY,
    };
    const event = new Event(input.type, { bubbles: true, cancelable: true });
    Object.defineProperties(event, {
      touches: { value: input.type === "touchend" ? [] : [touch] },
      targetTouches: { value: input.type === "touchend" ? [] : [touch] },
      changedTouches: { value: [touch] },
    });
    target.dispatchEvent(event);
  }, { type, clientY });
}

async function stageDelayedTouchMove(
  page: import("@playwright/test").Page,
  startY: number,
  clientY: number,
): Promise<void> {
  await page.locator(".thread").evaluate((node, input) => {
    const target = node as HTMLElement & { __delayedTouchMove?: Event };
    const startTouch = {
      identifier: 1,
      target,
      clientX: 120,
      clientY: input.startY,
    };
    const start = new Event("touchstart", { bubbles: true, cancelable: true });
    Object.defineProperties(start, {
      touches: { value: [startTouch] },
      targetTouches: { value: [startTouch] },
      changedTouches: { value: [startTouch] },
    });
    target.dispatchEvent(start);
    const touch = { ...startTouch, clientY: input.clientY };
    const event = new Event("touchmove", { bubbles: true, cancelable: true });
    Object.defineProperties(event, {
      touches: { value: [touch] },
      targetTouches: { value: [touch] },
      changedTouches: { value: [touch] },
    });
    target.__delayedTouchMove = event;
  }, { startY, clientY });
}

async function dispatchDelayedTouchMove(
  page: import("@playwright/test").Page,
): Promise<void> {
  await page.locator(".thread").evaluate((node) => {
    const target = node as HTMLElement & { __delayedTouchMove?: Event };
    const event = target.__delayedTouchMove;
    delete target.__delayedTouchMove;
    if (!event) throw new Error("delayed touchmove was not staged");
    target.dispatchEvent(event);
  });
}

async function requestOlderHistory(
  page: import("@playwright/test").Page,
  projectName: string,
  repeat = 1,
): Promise<void> {
  const viewport = page.locator(".thread");
  if (!isMobileWebKitProject(projectName)) {
    for (let index = 0; index < repeat; index += 1) {
      await viewport.dispatchEvent("wheel", { deltaY: -80 });
    }
    return;
  }
  await dispatchTouchGesture(page, 60, repeat);
}

async function requestNewerHistory(
  page: import("@playwright/test").Page,
  projectName: string,
  repeat = 1,
): Promise<void> {
  const viewport = page.locator(".thread");
  if (!isMobileWebKitProject(projectName)) {
    for (let index = 0; index < repeat; index += 1) {
      await viewport.dispatchEvent("wheel", { deltaY: 80 });
    }
    return;
  }
  await dispatchTouchGesture(page, -60, repeat);
}

test("prepend preserves the exact reading row through delayed row growth", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html");
  const viewport = page.locator(".thread");
  await expect(page.locator('[data-turn-id="o1"]')).toBeVisible();
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const before = await readingAnchor(page);
  await requestOlderHistory(page, testInfo.project.name);
  await expect(page.locator('[data-turn-id="n8"]')).toBeVisible();

  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);

  await page.waitForTimeout(800);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(before.id);
  expect(Math.abs(settled.offset - before.offset)).toBeLessThan(2);
});

test("reducer history paging and live refresh keep one stable projection", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?reducer-pipeline=1");
  await expect(page.locator('[data-turn-id="reducer-m40"]')).toBeVisible();
  await expect(page.getByTestId("reducer-turn-count")).toHaveText("20");
  await expect(page.getByTestId("reducer-unique-turn-count")).toHaveText("20");

  await pauseOutputAndScrollToHistoryStart(page);
  const before = await readingAnchor(page);
  await page.getByRole("button", { name: "加载更早的历史" }).click();
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.locator('[data-turn-id="reducer-m20"]')).toBeAttached();
  await expect(page.getByTestId("reducer-turn-count")).toHaveText("40");
  await expect(page.getByTestId("reducer-unique-turn-count")).toHaveText("40");
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);

  // A same-revision newest-page refresh is delivered to the authoritative
  // runtime while the reducer-owned browse projection remains visible.
  await page.getByTestId("reducer-live-refresh").click();
  await expect(page.getByTestId("reducer-refresh-count")).toHaveText("1");
  await expect(page.getByTestId("reducer-turn-count")).toHaveText("40");
  await expect(page.getByTestId("reducer-unique-turn-count")).toHaveText("40");
  const afterRefresh = await readingAnchor(page);
  expect(afterRefresh.id).toBe(before.id);
  expect(Math.abs(afterRefresh.offset - before.offset)).toBeLessThan(2);

  // A -> B -> A discards only the display browse window. The accepted runtime
  // must still contain one canonical copy of every newest turn.
  await page.getByTestId("switch-session").click();
  await expect(page.getByTestId("reducer-focused-sid"))
    .toHaveText("reducer-history-session-b");
  await expect(page.locator('[data-turn-id="reducer-b8"]')).toBeVisible();
  await page.getByTestId("switch-session").click();
  await expect(page.getByTestId("reducer-focused-sid"))
    .toHaveText("reducer-history-session-a");
  await expect(page.locator('[data-turn-id="reducer-m40"]')).toBeVisible();
  await expect(page.getByTestId("reducer-turn-count")).toHaveText("20");
  await expect(page.getByTestId("reducer-unique-turn-count")).toHaveText("20");
});

test("authoritative paging returns after an IndexedDB first paint", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?reducer-pipeline=1&cached-paging=1",
  );
  await expect(page.locator('[data-turn-id="reducer-cached-current"]'))
    .toBeVisible();
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const loader = page.getByTestId("load-older-history");
  await expect(loader).toBeVisible();
  const [loaderBox, viewportBox] = await Promise.all([
    loader.boundingBox(),
    viewport.boundingBox(),
  ]);
  expect(loaderBox).not.toBeNull();
  expect(viewportBox).not.toBeNull();
  expect(loaderBox!.y).toBeGreaterThanOrEqual(viewportBox!.y);
  expect(loaderBox!.y + loaderBox!.height)
    .toBeLessThanOrEqual(viewportBox!.y + viewportBox!.height);
});

test("reducer prepend never paints an intermediate reading-row jump", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?reducer-pipeline=1");
  await expect(page.locator('[data-turn-id="reducer-m40"]')).toBeVisible();
  await pauseOutputAndScrollToHistoryStart(page);
  const before = await readingAnchor(page);
  const sampled = await maxPaintedTurnOffsetShiftThroughAction(
    page,
    before.id,
    "load-older-history",
    60,
  );
  await expect(page.locator('[data-turn-id="reducer-m20"]')).toBeAttached();
  const after = await readingAnchor(page);
  expect(sampled.missing).toBe(false);
  expect(sampled.maxShift).toBeLessThan(2);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("touching the history edge never paints an intermediate reading-row jump", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?reducer-pipeline=1");
  await expect(page.locator('[data-turn-id="reducer-m40"]')).toBeVisible();
  await pauseOutputAndScrollToHistoryStart(page);
  const before = await readingAnchor(page);
  const sampled = await maxTurnOffsetShiftThroughTouchHistoryLoad(
    page,
    before.id,
  );
  await expect(page.locator('[data-turn-id="reducer-m20"]')).toBeAttached();
  const after = await readingAnchor(page);
  expect(sampled.missing).toBe(false);
  expect(sampled.maxShift).toBeLessThan(2);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("a history page waits for the active touch to release before mounting", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?delay=20&manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await dispatchTouchPhase(page, "touchstart", 160);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  const pageActivity = page.getByTestId("history-page-activity");
  await expect(pageActivity).toContainText("正在加载更早历史");
  await expect(pageActivity).toHaveCSS("pointer-events", "none");
  await page.waitForTimeout(100);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
  await expect(pageActivity).toBeVisible();
  const held = await readingAnchor(page);
  expect(held.id).toBe(before.id);
  expect(Math.abs(held.offset - before.offset)).toBeLessThan(2);

  await dispatchTouchPhase(page, "touchend", 220);
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await expect(pageActivity).toHaveCount(0);
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
  await page.waitForTimeout(350);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(before.id);
  expect(Math.abs(settled.offset - before.offset)).toBeLessThan(2);
});

test("the first runtime browse page stays staged under an active touch", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "mobile WebKit touch path");
  await page.goto(
    "/tests/history-browser.html?runtime-browse=1&delay=0&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await dispatchTouchPhase(page, "touchstart", 160);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await page.waitForTimeout(100);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
  await expect(page.getByTestId("history-page-activity")).toBeVisible();
  const held = await readingAnchor(page);
  expect(held.id).toBe(before.id);
  expect(Math.abs(held.offset - before.offset)).toBeLessThan(2);

  await dispatchTouchPhase(page, "touchend", 220);
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
});

test("the first runtime browse page stays staged until wheel idle", async ({
  page,
}, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name),
    "desktop wheel path");
  await page.goto(
    "/tests/history-browser.html?runtime-browse=1&delay=20&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await viewport.evaluate((node) => {
    const target = node as HTMLElement & { __wheelLease?: number };
    const signalWheel = () => target.dispatchEvent(new WheelEvent("wheel", {
      bubbles: true,
      deltaY: -80,
    }));
    signalWheel();
    target.__wheelLease = window.setInterval(signalWheel, 50);
  });
  try {
    await expect(page.getByTestId("load-count")).toHaveText("1");
    await page.waitForTimeout(100);
    await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
    await expect(page.getByTestId("history-page-activity")).toBeVisible();
  } finally {
    await viewport.evaluate((node) => {
      const target = node as HTMLElement & { __wheelLease?: number };
      if (target.__wheelLease != null) {
        window.clearInterval(target.__wheelLease);
        delete target.__wheelLease;
      }
    });
  }

  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
});

test("older history becoming available during a wheel gesture is restored once", async ({
  page,
}, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name),
    "desktop wheel path");
  await page.goto(
    "/tests/history-browser.html?delayed-history-availability=1&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const before = await readingAnchor(page);
  await viewport.dispatchEvent("wheel", { deltaY: -80 });

  const pending = await readingAnchor(page);
  expect(pending.id).toBe(before.id);
  expect(Math.abs(pending.offset - before.offset)).toBeLessThan(2);
  await expect(page.getByTestId("load-count")).toHaveText("0");
  await viewport.evaluate((node) => {
    node.dispatchEvent(new WheelEvent("wheel", {
      bubbles: true,
      deltaY: -80,
    }));
    const reveal = document.querySelector<HTMLButtonElement>(
      '[data-testid="reveal-older-history"]',
    );
    if (!reveal) throw new Error("history reveal control is missing");
    reveal.click();
  });
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await page.waitForTimeout(250);
  await expect(page.getByTestId("load-count")).toHaveText("1");
});

test("older history becoming available under touch waits for release", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "mobile WebKit touch path");
  await page.goto(
    "/tests/history-browser.html?delayed-history-availability=1&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await dispatchTouchPhase(page, "touchstart", 160);
  await dispatchTouchPhase(page, "touchmove", 220);
  await page.getByTestId("reveal-older-history").click();

  await expect(page.getByText("正在恢复历史…")).toBeVisible();
  await expect(page.getByTestId("load-count")).toHaveText("0");
  await dispatchTouchPhase(page, "touchend", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await page.waitForTimeout(250);
  await expect(page.getByTestId("load-count")).toHaveText("1");
});

test("restored turn detail uses one process disclosure and preserves its duration", async ({ page }) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&detail-restored-page=process",
  );
  const header = page.locator(".turn-process-head");
  await expect(header).toContainText("已处理 2h 9m");
  await expect(header).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator(".turn-detail-entry")).toHaveCount(0);
  await header.click();
  await expect(page.getByRole("button", { name: "加载更早过程" })).toBeVisible();
  await page.getByRole("button", { name: "加载更早过程" }).click();
  await expect(page.getByText("较早命令 1")).toBeVisible();
  await expect(page.locator(".turn-detail-entry")).toHaveCount(0);
});

for (const direction of ["older", "newer"]) {
  for (const failOnce of [false, true]) {
    test(`standalone turn detail follows its ${direction} cursor${failOnce ? " after retry" : ""}`, async ({ page }) => {
      await page.goto(
        "/tests/history-browser.html?detail-paging=1&delay=100"
          + `&detail-restored-page=${direction}`
          + (failOnce ? "&detail-error-once=1" : ""),
      );
      await expect(page.locator(".turn-process-head")).toHaveCount(0);
      const more = page.getByRole("button", { name: "查看更多内容" });
      await more.click();
      await expect(page.locator("html"))
        .toHaveAttribute("data-detail-last-before", `detail-${direction}`);
      if (failOnce) {
        await expect(page.locator(".turn-detail-entry-error")).toContainText("详细过程暂时不可用");
        await more.click();
      }
      await expect(page.locator(".turn-detail-entry")).toHaveCount(0);
      await expect(page.locator("html"))
        .toHaveAttribute("data-detail-requests", failOnce ? "2" : "1");
      await expect(page.locator("html"))
        .toHaveAttribute("data-detail-last-before", `detail-${direction}`);
      const header = page.locator(".turn-process-head");
      if (await header.getAttribute("aria-expanded") === "false") await header.click();
      await expect(page.getByText("较早命令 1")).toBeVisible();
      await expect(page.getByText("较新命令 1")).toBeVisible();
    });
  }
}

test("turn detail stays bounded and older pages load explicitly without jumping", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&delay=1000&growth-delay=180",
  );
  const header = page.locator(".turn-process-head");
  await expect(header).toHaveAttribute("aria-expanded", "false");
  const initialStart = await processDetailEdge(page, "start");
  await header.click();
  await expect(header).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText("正在加载过程…")).toBeVisible();
  await expect(page.locator(".thread"))
    .toHaveAttribute("data-detail-anchor-active", "true");
  await expect(page.getByText("较新命令 1")).toBeVisible();
  await expect(page.getByText("较早命令 1")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "加载更早过程" }))
    .toBeVisible();
  await expect(page.getByRole("button", { name: "返回较新过程" }))
    .toHaveCount(0);
  await page.waitForTimeout(500);
  expect(Math.abs(await processDetailEdge(page, "start") - initialStart))
    .toBeLessThan(2);
  await expect(page.locator(".thread"))
    .toHaveAttribute("data-detail-anchor-active", "false");
  expect(await page.evaluate(
    () => document.documentElement.dataset.detailRequests,
  )).toBe("1");

  const beforeOlderPage = await processDetailEdge(page, "start");
  await page.getByRole("button", { name: "加载更早过程" }).click();
  await expect(page.getByText("较早命令 1")).toBeVisible();
  await expect.poll(() => page.evaluate(
    () => document.documentElement.dataset.detailRequests,
  )).toBe("2");
  await expect(page.getByRole("button", { name: "加载更早过程" }))
    .toHaveCount(0);
  await page.waitForTimeout(500);
  expect(Math.abs(
    await processDetailEdge(page, "start") - beforeOlderPage,
  )).toBeLessThan(2);
});

test("a loading process can collapse and reopen without issuing a duplicate read", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&delay=10000&growth-delay=180",
  );
  const header = page.locator(".turn-process-head");
  await header.click();
  await expect(header).toHaveAttribute("aria-expanded", "true");
  await expect(header).toHaveAttribute("aria-busy", "true");
  await header.click();
  await expect(header).toHaveAttribute("aria-expanded", "false");
  await header.click();
  await expect(header).toHaveAttribute("aria-expanded", "true");
  expect(await page.evaluate(
    () => document.documentElement.dataset.detailRequests,
  )).toBe("1");
});

test("a failed process detail stays open and retries in place", async ({ page }) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&detail-error-once=1"
      + "&delay=500&growth-delay=30",
  );
  const header = page.locator(".turn-process-head");
  await header.click();
  await expect(header).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("alert")).toContainText(
    "详细过程暂时不可用，请稍后重试",
  );
  await page.getByRole("button", { name: "重试" }).click();
  await expect(header).toHaveAttribute("aria-expanded", "true");
  await expect(header).toHaveAttribute("aria-busy", "true");
  await expect(page.getByText("较新命令 1")).toBeVisible();
  await page.getByRole("button", { name: "加载更早过程" }).click();
  await expect(page.getByText("较早命令 1")).toBeVisible();
});

test("a failed older process page retries the exact cursor in place", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&detail-older-error-once=1"
      + "&delay=30&growth-delay=10000",
  );
  const header = page.locator(".turn-process-head");
  await header.click();
  await expect(page.getByText("较新命令 1")).toBeVisible();
  await page.getByRole("button", { name: "加载更早过程" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "详细过程暂时不可用，请稍后重试",
  );
  await expect.poll(() => page.evaluate(
    () => document.documentElement.dataset.detailLastBefore,
  )).toBe("detail-older");
  await page.getByRole("button", { name: "重试" }).click();
  await expect(page.getByText("较早命令 1")).toBeVisible();
  await expect.poll(() => page.evaluate(
    () => document.documentElement.dataset.detailRequests,
  )).toBe("3");
  await expect.poll(() => page.evaluate(
    () => document.documentElement.dataset.detailLastBefore,
  )).toBe("detail-older");
});

test("retained truncated process still fetches its authoritative first detail page", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&detail-retained-preview=1"
      + "&delay=500&growth-delay=180",
  );
  const header = page.locator(".turn-process-head");
  await header.click();
  await expect(page.getByText("已缓存的较新命令")).toBeVisible();
  await expect(page.getByText("较早过程已省略")).toHaveCount(0);
  await expect(header).toHaveAttribute("aria-busy", "true");
  await expect(page.getByText("较早过程已省略")).toHaveCount(0);
  await expect(page.getByText("较新命令 1")).toBeVisible();
  await expect(page.getByText("较早命令 1")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "加载更早过程" }))
    .toBeVisible();
});

test("user scrolling cancels a pending turn-detail anchor", async ({ page }) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&detail-scroll-cancel=1"
      + "&delay=250&growth-delay=180",
  );
  const header = page.locator(".turn-process-head");
  await header.scrollIntoViewIfNeeded();
  await header.click();
  await page.waitForTimeout(40);
  const viewport = page.locator(".thread");
  await viewport.dispatchEvent("wheel", { deltaY: 90 });
  await viewport.evaluate((node) => { node.scrollTop += 90; });
  await page.waitForTimeout(40);
  const userOffset = await processDetailEdge(page, "start");

  await expect(page.getByText("较新命令 1")).toBeVisible();
  await page.waitForTimeout(450);
  expect(Math.abs(await processDetailEdge(page, "start") - userOffset))
    .toBeLessThan(2);
});

test("history page cache rebuilds a v1 record in real IndexedDB", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html");
  const result = await page.evaluate(async () => {
    const modulePath = "/src/history-page-cache.ts";
    const cacheModule = await import(modulePath);
    const scope = {
      machineId: "browser-machine",
      engine: "codex",
      space: "code",
      sid: "browser-cache-session",
      revision: "browser-cache-revision",
    };
    const pageKey = "v1-page";
    const key = cacheModule.historyPageCachePageKey(scope, pageKey);
    const scopeKey = cacheModule.historyPageCacheScopeKey(scope);
    const sessionKey = cacheModule.historyPageCacheSessionKey(scope);
    const legacyDb = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open(cacheModule.HISTORY_PAGE_CACHE_DB_NAME, 1);
      request.onupgradeneeded = () => {
        const store = request.result.createObjectStore("pages", {
          keyPath: "key",
        });
        store.createIndex("scope", "scopeKey", { unique: false });
        store.createIndex("session", "sessionKey", { unique: false });
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    await new Promise<void>((resolve, reject) => {
      const transaction = legacyDb.transaction("pages", "readwrite");
      transaction.objectStore("pages").put({
        version: 1,
        key,
        scopeKey,
        sessionKey,
        ...scope,
        pageKey,
        page: {
          pageKey,
          turns: [{
            id: "legacy-turn",
            prompt: "legacy",
            blocks: [],
            done: true,
          }],
          hasOlder: false,
          olderCursor: "legacy-turn",
          hasNewer: false,
          newerPageKey: null,
          isLatest: false,
        },
        savedAt: 1,
        byteSize: 256,
      });
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error);
    });
    legacyDb.close();

    const cache = new cacheModule.HistoryPageCache();
    const upgraded = await cache.getPage(scope, pageKey);
    const stored = await cache.putPage(scope, {
      pageKey,
      turns: [{
        id: "new-turn",
        prompt: "new",
        blocks: [],
        done: true,
      }],
      hasOlder: false,
      olderCursor: "legacy-turn",
    });
    const merged = await cache.getPage(scope, pageKey);
    const invalidated = await cache.invalidateScope(scope);
    const afterInvalidation = await cache.getPage(scope, pageKey);
    return {
      legacyMiss: upgraded === null,
      stored,
      mergedIds: merged?.turns.map((turn: { id: string }) => turn.id),
      invalidated,
      afterInvalidation,
    };
  });
  expect(result.legacyMiss).toBe(true);
  expect(result.stored.ok).toBe(true);
  expect(result.mergedIds).toEqual(["new-turn"]);
  expect(result.invalidated.ok).toBe(true);
  expect(result.afterInvalidation).toBeNull();
});

test("instant session cache preserves a heavy turn's complete process skeleton", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html");
  const result = await page.evaluate(async () => {
    const cache = await import("/src/cache.ts");
    await cache.clearCache();
    cache.saveSession("heavy-refresh-session", [{
      id: "heavy-refresh-turn",
      prompt: "1",
      done: true,
      images: [{
        media_type: "image/png",
        data: "i".repeat(2 * 1024 * 1024 + 1),
      }],
      blocks: [
        {
          kind: "text", message_id: "heavy-commentary", text: "2",
          done: true, channel: "commentary",
        },
        {
          kind: "tool", message_id: "heavy-tool-message-3",
          tool_use_id: "heavy-tool-3", tool: "Read",
          input: { file_path: "/tmp/3" },
          result: {
            content: "x".repeat(2 * 1024 * 1024 + 1),
            is_error: false,
          },
          done: true,
        },
        {
          kind: "tool", message_id: "heavy-tool-message-4",
          tool_use_id: "heavy-tool-4", tool: "Bash",
          input: { command: "echo 4" }, done: true,
        },
        {
          kind: "process", item_id: "heavy-process-5",
          processKind: "command", phase: "snapshot", status: "succeeded",
          title: "5", done: true,
        },
        {
          kind: "text", message_id: "heavy-final", text: "6",
          done: true, channel: "final",
        },
      ],
      detailEventCount: 4,
    }], 42, "heavy-refresh-r1", "heavy-refresh-g1");
    await new Promise((resolve) => window.setTimeout(resolve, 700));
    const loaded = await cache.loadSession("heavy-refresh-session");
    const turn = loaded?.turns[0] as {
      images?: unknown[];
      blocks?: Array<Record<string, unknown>>;
      detailProjection?: unknown;
    } | undefined;
    return {
      size: new TextEncoder().encode(JSON.stringify(turn)).byteLength,
      hasImages: Array.isArray(turn?.images),
      hasDetailProjection: turn?.detailProjection != null,
      process: turn?.blocks?.map((block) => (
        block.kind === "text" ? block.text
          : block.kind === "tool" ? block.tool_use_id
            : block.title
      )),
    };
  });
  expect(result.size).toBeLessThan(2 * 1024 * 1024);
  expect(result.hasImages).toBe(false);
  expect(result.hasDetailProjection).toBe(false);
  expect(result.process).toEqual([
    "2", "heavy-tool-3", "heavy-tool-4", "5", "6",
  ]);
});

test("instant session cache persists a bounded history-start proof only", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html");
  const result = await page.evaluate(async () => {
    const cache = await import("/src/cache.ts");
    await cache.clearCache();
    const turn = (id: string) => ({
      id, prompt: id, blocks: [], done: true,
    });
    cache.saveSession(
      "complete-history-head",
      [turn("complete-turn")],
      0,
      "complete-history-r1",
      "complete-history-g1",
      null,
      true,
    );
    cache.saveSession(
      "trimmed-history-head",
      Array.from({ length: 101 }, (_, index) => turn(`trimmed-${index}`)),
      0,
      "trimmed-history-r1",
      "trimmed-history-g1",
      null,
      true,
    );
    await new Promise((resolve) => window.setTimeout(resolve, 700));
    const complete = await cache.loadSession("complete-history-head");
    const trimmed = await cache.loadSession("trimmed-history-head");
    return {
      completeAtStart: complete?.historyAtStart,
      trimmedAtStart: trimmed?.historyAtStart,
      trimmedTurns: trimmed?.turns.length,
      pagingKeys: complete == null
        ? []
        : ["hasMore", "oldestId", "cursor"].filter((key) => (
            Object.prototype.hasOwnProperty.call(complete, key)
          )),
    };
  });
  expect(result.completeAtStart).toBe(true);
  expect(result.trimmedAtStart).toBe(false);
  expect(result.trimmedTurns).toBe(100);
  expect(result.pagingKeys).toEqual([]);
});

test("session cache rejects stale Claude and replay-orphan rows", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html");
  const result = await page.evaluate(async () => {
    const cache = await import("/src/cache.ts");
    await cache.clearCache();
    const legacySid = "legacy-claude-prompt-alias";
    const replayOrphanSid = "completed-replay-orphan";
    const activeCompactionOrphanSid = "active-compaction-replay-orphan";
    const recoveredOwnerV16Sid = "completed-recovery-owner-v16";
    const lateSeedV17Sid = "active-late-binding-seed-v17";
    const pollutedAliasV20Sid = "claude-interrupt-alias-v20";
    const missingAnswerV25Sid = "async-question-missing-answer-v25";
    const optimisticSteerSid = "healthy-optimistic-steer";
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open("cc_remote_cache", 1);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    await new Promise<void>((resolve, reject) => {
      const tx = database.transaction("sessions", "readwrite");
      tx.objectStore("sessions").put({
        v: 10,
        turns: [{
          id: "claude-transcript-uuid",
          prompt: "legacy prompt",
          blocks: [],
          done: false,
        }],
        lastSeq: 41,
        revision: "legacy-r1",
        generation: "legacy-g1",
        savedAt: Date.now(),
      }, legacySid);
      tx.objectStore("sessions").put({
        v: 13,
        turns: [{
          id: "native-history-turn",
          prompt: "deploy",
          blocks: [],
          done: true,
        }, {
          id: "replayed-assistant-message",
          prompt: "",
          blocks: [{
            kind: "tool",
            message_id: "replayed-assistant-message",
            tool_use_id: "replayed-tool-a",
            tool: "Command",
            input: {},
            done: true,
          }, {
            kind: "tool",
            message_id: "replayed-assistant-message",
            tool_use_id: "replayed-tool-b",
            tool: "Command",
            input: {},
            done: true,
          }],
          done: true,
        }],
        lastSeq: 43,
        revision: "replay-orphan-r1",
        generation: "replay-orphan-g1",
        savedAt: Date.now(),
      }, replayOrphanSid);
      tx.objectStore("sessions").put({
        v: 16,
        turns: [{
          id: "browser-owner",
          clientMsgId: "browser-owner",
          forkPointId: "native-owner",
          prompt: "deploy",
          blocks: [],
          done: true,
        }, {
          id: "recovered-tail",
          prompt: "",
          blocks: [{
            kind: "text",
            message_id: "recovered-answer",
            text: "done",
            channel: "final",
            done: true,
          }],
          done: true,
        }],
        lastSeq: 365,
        revision: "recovered-owner-r1",
        generation: "recovered-owner-g1",
        savedAt: Date.now(),
      }, recoveredOwnerV16Sid);
      tx.objectStore("sessions").put({
        v: 15,
        turns: [{
          id: "item-51",
          prompt: "continue the task",
          forkPointId: "native-turn",
          blocks: [{
            kind: "process",
            item_id: "item-54",
            processKind: "compaction",
            phase: "snapshot",
            status: "succeeded",
            turn_id: "native-turn",
            title: "压缩上下文",
            done: true,
          }],
          done: false,
        }, {
          id: "msg-after-compact",
          prompt: "",
          blocks: [{
            kind: "text",
            message_id: "msg-after-compact",
            text: "continued output",
            channel: "commentary",
            done: true,
          }, {
            kind: "process",
            item_id: "replayed-compaction",
            processKind: "compaction",
            phase: "snapshot",
            status: "succeeded",
            turn_id: "native-turn",
            title: "压缩上下文",
            done: true,
          }],
          done: false,
        }],
        lastSeq: 364,
        revision: "active-compaction-r1",
        generation: "active-compaction-g1",
        savedAt: Date.now(),
      }, activeCompactionOrphanSid);
      tx.objectStore("sessions").put({
        v: 17,
        turns: [{
          id: "canonical-current-owner",
          clientMsgId: "canonical-current-owner",
          forkPointId: "shared-current-native-turn",
          prompt: "current prompt",
          blocks: [],
          done: false,
        }, {
          id: "late-seeded-live-tail",
          prompt: "",
          blocks: [{
            kind: "text",
            message_id: "late-seeded-live-tail",
            text: "duplicated current suffix",
            channel: "commentary",
            done: false,
          }],
          done: false,
        }],
        lastSeq: 46,
        revision: "late-seed-r1",
        generation: "late-seed-g1",
        savedAt: Date.now(),
      }, lateSeedV17Sid);
      tx.objectStore("sessions").put({
        v: 20,
        turns: [{
          id: "interrupt-marker-uuid",
          clientMsgId: "interrupt-marker-uuid",
          historyTurnId: "native-user-uuid",
          prompt: "continue after interrupt",
          blocks: [],
          done: false,
        }],
        lastSeq: 46,
        revision: "interrupt-alias-r1",
        generation: "interrupt-alias-g1",
        savedAt: Date.now(),
      }, pollutedAliasV20Sid);
      tx.objectStore("sessions").put({
        v: 25,
        turns: [{
          id: "async-user", prompt: "test", done: true,
          blocks: [{ kind: "text", message_id: "question", text: "Question?",
            channel: "final", done: true, delivery: "async",
            questions: [{ title: "Question?" }] }],
        }],
        lastSeq: 48,
        revision: "unchanged-source",
        generation: "old-generation",
        savedAt: Date.now(),
      }, missingAnswerV25Sid);
      tx.objectStore("sessions").put({
        v: 26,
        turns: [{
          id: "active-before-steer",
          prompt: "first prompt",
          forkPointId: "shared-native-turn",
          blocks: [],
          done: false,
        }, {
          id: "optimistic-steer",
          clientMsgId: "optimistic-steer",
          prompt: "second prompt",
          blocks: [],
          done: false,
        }],
        lastSeq: 47,
        revision: "optimistic-steer-r1",
        generation: "optimistic-steer-g1",
        savedAt: Date.now(),
      }, optimisticSteerSid);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
    const legacy = await cache.loadSession(legacySid);
    const replayOrphan = await cache.loadSession(replayOrphanSid);
    const recoveredOwnerV16 = await cache.loadSession(recoveredOwnerV16Sid);
    const activeCompactionOrphan = await cache.loadSession(
      activeCompactionOrphanSid);
    const lateSeedV17 = await cache.loadSession(lateSeedV17Sid);
    const pollutedAliasV20 = await cache.loadSession(pollutedAliasV20Sid);
    const missingAnswerV25 = await cache.loadSession(missingAnswerV25Sid);
    const optimisticSteer = await cache.loadSession(optimisticSteerSid);
    const replay = await cache.loadAllReplayState();
    await new Promise((resolve) => window.setTimeout(resolve, 100));
    const prunedCompactionOrphan = await new Promise((resolve, reject) => {
      const tx = database.transaction("sessions", "readonly");
      const request = tx.objectStore("sessions").get(activeCompactionOrphanSid);
      request.onsuccess = () => resolve(request.result ?? null);
      request.onerror = () => reject(request.error);
    });
    cache.saveSession("current-claude-prompt-alias", [{
      id: "browser-prompt-id",
      clientMsgId: "browser-prompt-id",
      historyTurnId: "claude-transcript-uuid",
      prompt: "current prompt",
      blocks: [],
      done: false,
    }], 42, "current-r1", "current-g1");
    await new Promise((resolve) => window.setTimeout(resolve, 700));
    const current = await cache.loadSession("current-claude-prompt-alias");
    database.close();
    return {
      legacy,
      legacyCursor: replay.cursors[legacySid],
      replayOrphan,
      replayOrphanCursor: replay.cursors[replayOrphanSid],
      recoveredOwnerV16,
      recoveredOwnerV16Cursor: replay.cursors[recoveredOwnerV16Sid],
      activeCompactionOrphan,
      activeCompactionCursor: replay.cursors[activeCompactionOrphanSid],
      prunedCompactionOrphan,
      lateSeedV17,
      lateSeedV17Cursor: replay.cursors[lateSeedV17Sid],
      pollutedAliasV20,
      pollutedAliasV20Cursor: replay.cursors[pollutedAliasV20Sid],
      missingAnswerV25,
      missingAnswerV25Cursor: replay.cursors[missingAnswerV25Sid],
      optimisticSteerCount: optimisticSteer?.turns.length,
      optimisticSteerCursor: replay.cursors[optimisticSteerSid],
      currentIds: current?.turns.map((turn: {
        id: string; clientMsgId?: string; historyTurnId?: string;
      }) => [turn.id, turn.clientMsgId, turn.historyTurnId]),
    };
  });
  expect(result.legacy).toBeNull();
  expect(result.legacyCursor).toBeUndefined();
  expect(result.replayOrphan).toBeNull();
  expect(result.replayOrphanCursor).toBeUndefined();
  expect(result.recoveredOwnerV16).toBeNull();
  expect(result.recoveredOwnerV16Cursor).toBeUndefined();
  expect(result.activeCompactionOrphan).toBeNull();
  expect(result.activeCompactionCursor).toBeUndefined();
  expect(result.prunedCompactionOrphan).toBeNull();
  expect(result.lateSeedV17).toBeNull();
  expect(result.lateSeedV17Cursor).toBeUndefined();
  expect(result.pollutedAliasV20).toBeNull();
  expect(result.pollutedAliasV20Cursor).toBeUndefined();
  expect(result.missingAnswerV25).toBeNull();
  expect(result.missingAnswerV25Cursor).toBeUndefined();
  expect(result.optimisticSteerCount).toBe(2);
  expect(result.optimisticSteerCursor).toBe(47);
  expect(result.currentIds).toEqual([[
    "browser-prompt-id", "browser-prompt-id", "claude-transcript-uuid",
  ]]);
});

test("a canonical image reference does not reserve a second hidden image row", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?dual-image=1");
  const assertCanonicalImageLayout = async () => {
    const turn = page.locator('[data-turn-id="dual-image"]');
    await expect(turn).toBeVisible();
    await expect(turn.locator(".ubub-image-trigger")).toHaveCount(1);
    const gap = await turn.evaluate((node) => {
      const image = node.querySelector<HTMLElement>(".ubub-image-trigger");
      const meta = node.querySelector<HTMLElement>(".ubub-meta");
      if (!image || !meta) throw new Error("image layout is incomplete");
      return meta.getBoundingClientRect().top - image.getBoundingClientRect().bottom;
    });
    expect(gap).toBeLessThan(20);
  };

  await assertCanonicalImageLayout();
  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();
  await page.getByTestId("switch-session").click();
  await assertCanonicalImageLayout();
});

test("fallback image preview and canonical retry remain independent", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?history-image-fallback-error=1");
  const turn = page.locator('[data-turn-id="history-fallback-error"]');
  const imageRow = turn.locator(".ubub-imgs");
  const preview = turn.getByRole("button", {
    name: "预览用户发送的图片",
  });
  const retry = turn.getByRole("button", { name: "点击重试" });

  await expect(turn).toBeVisible();
  await expect(imageRow).toHaveCount(1);
  await expect(imageRow.locator(":scope > .history-image-control"))
    .toHaveCount(1);
  await expect(turn.locator(".ubub-image-trigger")).toHaveCount(1);
  await expect(preview).toBeVisible();
  await expect(retry).toBeVisible();
  const gap = await turn.evaluate((node) => {
    const row = node.querySelector<HTMLElement>(".ubub-imgs");
    const meta = node.querySelector<HTMLElement>(".ubub-meta");
    if (!row || !meta) throw new Error("canonical image layout is incomplete");
    return meta.getBoundingClientRect().top - row.getBoundingClientRect().bottom;
  });
  expect(gap).toBeLessThan(20);
  await expect(page.getByTestId("history-fallback-loads")).toHaveText("0");

  await preview.click();
  await expect(page.locator(".image-lightbox")).toBeVisible();
  await expect(page.getByTestId("history-fallback-loads")).toHaveText("0");
  await page.locator(".image-lightbox-close").click();
  await expect(page.locator(".image-lightbox")).toHaveCount(0);

  await retry.click();
  await expect(page.getByTestId("history-fallback-loads")).toHaveText("1");
  await expect(page.getByTestId("history-fallback-last-load"))
    .toHaveText("history-fallback-error|history-fallback-image|thumbnail");
  await expect(page.locator(".image-lightbox")).toHaveCount(0);
  await expect(imageRow).toHaveCount(1);
  await expect(turn.locator(".ubub-image-trigger")).toHaveCount(1);
});

test("streaming rerenders cannot cancel an image preview close", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?dual-image=1");
  await page.locator(".ubub-image-trigger").click();
  await expect(page.locator(".image-lightbox")).toBeVisible();

  await page.evaluate(() => {
    document.querySelector<HTMLButtonElement>(".image-lightbox-close")?.click();
    document.querySelector<HTMLButtonElement>('[data-testid="append-turn"]')?.click();
  });

  await expect(page.locator(".image-lightbox")).toHaveCount(0);
});

test("expanded tool batches use dense rows instead of individual cards", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?compact-tools=1");
  await page.locator(".turn-process-head").click();
  await page.locator(".tool-group-h").click();

  const rows = page.locator(".tool-group-b .tool");
  await expect(rows).toHaveCount(3);
  const styles = await rows.evaluateAll((nodes) => nodes.map((node) => {
    const style = getComputedStyle(node);
    return {
      height: node.getBoundingClientRect().height,
      border: style.borderTopWidth,
      radius: style.borderTopLeftRadius,
      shadow: style.boxShadow,
    };
  }));
  for (const style of styles) {
    expect(style.height).toBeLessThan(40);
    expect(style.border).toBe("0px");
    expect(style.radius).toBe("0px");
    expect(style.shadow).toBe("none");
  }
});

test("tool disclosures keep keyboard activation and ignore a scroll drag", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?compact-tools=1");
  await page.locator(".turn-process-head").click();
  const group = page.locator("details.tool-group");
  const groupSummary = group.locator(":scope > summary");

  await groupSummary.focus();
  await page.keyboard.press("Enter");
  await expect(group).toHaveAttribute("open", "");
  const firstTool = group.locator("details.tool").first();
  const firstToolSummary = firstTool.locator(":scope > summary");
  await firstToolSummary.focus();
  await page.keyboard.press("Enter");
  await expect(firstTool).toHaveAttribute("open", "");
  await page.keyboard.press("Enter");
  await expect(firstTool).not.toHaveAttribute("open", "");

  await groupSummary.focus();
  await page.keyboard.press("Enter");
  await expect(group).not.toHaveAttribute("open", "");
  const box = await groupSummary.boundingBox();
  if (!box) throw new Error("tool group summary has no geometry");
  await page.mouse.move(box.x + 20, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + 20, box.y + box.height / 2 + 24, {
    steps: 4,
  });
  await page.mouse.up();
  await expect(group).not.toHaveAttribute("open", "");
});

test("iOS pointercancel releases tool disclosures without toggling", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit pointer cancellation");
  await page.goto("/tests/history-browser.html?compact-tools=1");
  await page.locator(".turn-process-head").click();
  const group = page.locator("details.tool-group");
  const groupSummary = group.locator(":scope > summary");

  await dispatchCancelledTouchTap(groupSummary, 181);
  await expect(group).not.toHaveAttribute("open", "");

  await groupSummary.click();
  await expect(group).toHaveAttribute("open", "");
  const firstTool = group.locator("details.tool").first();
  await dispatchCancelledTouchTap(
    firstTool.locator(":scope > summary"), 182,
  );
  await expect(firstTool).not.toHaveAttribute("open", "");
});

test("completed Mermaid fences render isolated sanitized SVGs", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?mermaid=1");
  const diagrams = page.locator(".mermaid-block");
  await expect(diagrams).toHaveCount(2);
  await expect(page.locator(".mermaid-svg")).toHaveCount(2);
  await expect(diagrams.nth(0)).toHaveAttribute("data-mermaid-state", "ready");
  await expect(diagrams.nth(1)).toHaveAttribute("data-mermaid-state", "ready");

  const rootIds = await page.locator(".mermaid-svg > svg").evaluateAll(
    (nodes) => nodes.map((node) => node.id),
  );
  expect(rootIds.every(Boolean)).toBe(true);
  expect(new Set(rootIds).size).toBe(rootIds.length);
  await expect(page.locator(
    ".mermaid-svg script, .mermaid-svg foreignObject, .mermaid-svg image, "
      + ".mermaid-svg a",
  )).toHaveCount(0);
  const unsafeReferences = await page.locator(".mermaid-svg svg").evaluateAll(
    (nodes) => nodes.flatMap((node) =>
      [...node.querySelectorAll("*")].flatMap((element) =>
        [...element.attributes]
          .filter((attribute) => ["href", "xlink:href", "src"].includes(
            attribute.name.toLowerCase(),
          ))
          .map((attribute) => attribute.value)
          .filter((value) => !value.startsWith("#")))),
  );
  expect(unsafeReferences).toEqual([]);
  const clippedNodes = await diagrams.nth(0).evaluate((node) => {
    const svg = node.querySelector("svg");
    if (!svg) throw new Error("flowchart SVG is missing");
    const bounds = svg.getBoundingClientRect();
    return [...svg.querySelectorAll<SVGGraphicsElement>(".node")].filter((item) => {
      const rect = item.getBoundingClientRect();
      return rect.left < bounds.left - 1 || rect.right > bounds.right + 1
        || rect.top < bounds.top - 1 || rect.bottom > bounds.bottom + 1;
    }).length;
  });
  expect(clippedNodes).toBe(0);

  for (const diagram of await diagrams.all()) {
    const sizes = await diagram.evaluate((node) => {
      const svg = node.querySelector("svg");
      if (!svg) throw new Error("rendered Mermaid is missing its SVG");
      return {
        container: node.getBoundingClientRect().width,
        svg: svg.getBoundingClientRect().width,
      };
    });
    expect(sizes.svg).toBeLessThanOrEqual(sizes.container + 1);
  }

  const lightId = await page.locator(".mermaid-svg > svg").first().getAttribute("id");
  await page.evaluate(() => {
    document.documentElement.dataset.theme = "dark";
  });
  await expect.poll(async () =>
    page.locator(".mermaid-svg > svg").first().getAttribute("id"),
  ).not.toBe(lightId);
  await expect(diagrams.nth(0)).toHaveAttribute("data-mermaid-state", "ready");
});

test("completed chat formulas lazy-load accessible KaTeX markup", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?math=1");
  await expect(page.locator(".katex-display")).toHaveCount(1);
  await expect(page.locator(".katex")).toHaveCount(2);
  await expect(page.locator(".katex-mathml math")).toHaveCount(2);
  await expect(page.locator('[data-turn-id="math"]')).toContainText(
    "Inline:",
  );
  await expect(page.locator('[data-turn-id="math"] .message-code-copy'))
    .toHaveCount(0);
});

test("a completed streaming formula renders while following the live tail", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?streaming-math=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await waitForScrollIdle(page);
  await expect(page.locator('[data-turn-id="streaming-math"] .katex'))
    .toHaveCount(0);

  await page.getByTestId("close-streaming-formula").evaluate(
    (button: HTMLButtonElement) => button.click(),
  );
  await expect(page.locator('[data-turn-id="streaming-math"] .katex'))
    .toHaveCount(1);
  await expect.poll(async () => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);
});

test("streaming formula closure preserves a non-bottom reading anchor", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?streaming-math=1");
  await wheelUntilTurn(
    page, "math-before-4", -1_200, testInfo.project.name,
  );
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await page.getByTestId("close-streaming-formula").evaluate(
    (button: HTMLButtonElement) => button.click(),
  );
  await page.waitForTimeout(150);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("a completed Mermaid diagram opens the shared pinch-zoom preview", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/tests/history-browser.html?mermaid=1");
  const diagram = page.locator(".mermaid-block").first();
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  await applyProductionCsp(page);
  const inlineSurface = await diagram.locator(".mermaid-svg").evaluate(
    (node) => getComputedStyle(node).backgroundColor,
  );

  await diagram.locator(".mermaid-zoom").click();
  const preview = page.getByRole("dialog", { name: "Mermaid 图表预览" });
  await expect(preview).toBeVisible();
  const vector = preview.locator(".image-lightbox-vector > svg");
  await expect(vector).toBeVisible();
  await expect(preview.locator("img")).toHaveCount(0);
  const vectorSurface = preview.locator(".image-lightbox-vector");
  await expect.poll(() => vectorSurface.evaluate(
    (node) => getComputedStyle(node).backgroundColor,
  )).toBe(inlineSurface);
  const gesture = await pinchThenPanPreview(page);
  expect(gesture.afterPinch.scale).toBeGreaterThan(1);
  expect(gesture.afterPan.scale).toBe(gesture.afterPinch.scale);
  expect(gesture.afterPan.x).toBeLessThan(gesture.afterPinch.x);
  await expect(preview).toBeVisible();
  await expect(preview).not.toHaveClass(/interacting/);
  await expect.poll(() => preview.locator(".image-lightbox-visual")
    .evaluate((node) => getComputedStyle(node).willChange)).toBe("auto");

  await page.getByRole("button", { name: "关闭 Mermaid 图表预览" }).click();
  await expect(preview).toHaveCount(0);

  await diagram.locator(".mermaid-svg").click();
  await expect(page.getByRole("dialog", { name: "Mermaid 图表预览" }))
    .toBeVisible();
  await page.evaluate(() => {
    document.querySelector<HTMLButtonElement>('[data-testid="switch-session"]')
      ?.click();
  });
  await expect(page.locator(".image-lightbox")).toHaveCount(0);
});

test("a dark Mermaid preview keeps the render theme surface", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?mermaid=1");
  await page.evaluate(() => {
    document.documentElement.dataset.theme = "dark";
  });
  const diagram = page.locator(".mermaid-block").first();
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  const inlineSurface = await diagram.locator(".mermaid-svg").evaluate(
    (node) => getComputedStyle(node).backgroundColor,
  );
  await diagram.locator(".mermaid-zoom").click();

  const preview = page.getByRole("dialog", { name: "Mermaid 图表预览" });
  const vectorSurface = preview.locator(".image-lightbox-vector");
  await expect.poll(() => vectorSurface.evaluate(
    (node) => getComputedStyle(node).backgroundColor,
  )).toBe(inlineSurface);
});

test("the real wide Robot Core diagram opens once and fits the viewport", async ({
  page,
}) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.setViewportSize({ width: 1568, height: 870 });
  await page.goto("/tests/history-browser.html?actual-mermaid=1");
  const diagram = page.locator(".mermaid-block");
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  const trigger = diagram.locator(".mermaid-svg");
  await expect(trigger).toHaveJSProperty("tagName", "BUTTON");
  const inlineBounds = await trigger.evaluate((node) => {
    const svg = node.querySelector("svg");
    if (!svg) throw new Error("inline Mermaid SVG is missing");
    return {
      containerWidth: node.getBoundingClientRect().width,
      svgWidth: svg.getBoundingClientRect().width,
    };
  });
  expect(inlineBounds.svgWidth).toBeLessThanOrEqual(
    inlineBounds.containerWidth + 1,
  );

  await trigger.click();
  const preview = page.getByRole("dialog", { name: "Mermaid 图表预览" });
  await expect.poll(async () => {
    if (pageErrors.length > 0) return `pageerror: ${pageErrors.join(" | ")}`;
    return await preview.isVisible() ? "visible" : "missing";
  }).toBe("visible");
  const bounds = await preview.evaluate((node) => {
    const visual = node.querySelector<HTMLElement>(".image-lightbox-visual");
    if (!visual) throw new Error("lightbox visual is missing");
    const stage = node.getBoundingClientRect();
    const image = visual.getBoundingClientRect();
    return {
      stage: {
        left: stage.left,
        top: stage.top,
        right: stage.right,
        bottom: stage.bottom,
      },
      image: {
        left: image.left,
        top: image.top,
        right: image.right,
        bottom: image.bottom,
      },
    };
  });
  expect(bounds.image.left).toBeGreaterThanOrEqual(bounds.stage.left + 17);
  expect(bounds.image.right).toBeLessThanOrEqual(bounds.stage.right - 17);
  expect(bounds.image.top).toBeGreaterThanOrEqual(bounds.stage.top + 17);
  expect(bounds.image.bottom).toBeLessThanOrEqual(bounds.stage.bottom - 17);

  await preview.evaluate((node) => {
    const visual = node.querySelector<HTMLElement>(".image-lightbox-visual");
    if (!visual) throw new Error("lightbox visual is missing");
    const bounds = visual.getBoundingClientRect();
    node.dispatchEvent(new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      clientX: (bounds.left + bounds.right) / 2,
      clientY: (bounds.top + bounds.bottom) / 2,
      detail: 1,
    }));
  });
  await expect(preview).toBeVisible();
  expect(pageErrors).toEqual([]);
});

test("desktop trackpad wheel zooms around the pointer and pans the preview", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "desktop trackpad behavior");
  await page.goto("/tests/history-browser.html?mermaid=1");
  const diagram = page.locator(".mermaid-block").first();
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  await diagram.locator(".mermaid-zoom").click();

  const gesture = await wheelZoomThenPanPreview(page);
  expect(gesture.zoomPrevented).toBe(true);
  expect(gesture.panPrevented).toBe(true);
  expect(gesture.afterZoom.scale).toBeGreaterThan(1);
  expect(gesture.afterPan.scale).toBe(gesture.afterZoom.scale);
  expect(gesture.afterPan.x).toBeLessThan(gesture.afterZoom.x);
  expect(gesture.afterPan.y).toBeLessThan(gesture.afterZoom.y);
  await expect(page.getByRole("dialog", { name: "Mermaid 图表预览" }))
    .toBeVisible();
});

test("a wide zoomed Mermaid can pan to both horizontal edges", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "desktop trackpad behavior");
  await page.setViewportSize({ width: 900, height: 720 });
  await page.goto("/tests/history-browser.html?mermaid=1");
  const diagram = page.locator(".mermaid-block").nth(1);
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  await diagram.locator(".mermaid-zoom").click();
  const preview = page.getByRole("dialog", { name: "Mermaid 图表预览" });
  await page.waitForTimeout(200);
  await preview.dispatchEvent("wheel", {
    ctrlKey: true,
    deltaY: -1_000,
    clientX: 450,
    clientY: 360,
  });
  await page.waitForTimeout(120);

  const horizontalEdges = async () => preview.evaluate((node) => {
    const visual = node.querySelector<HTMLElement>(".image-lightbox-visual");
    if (!visual) throw new Error("lightbox visual is missing");
    const stage = node.getBoundingClientRect();
    const image = visual.getBoundingClientRect();
    return {
      stageLeft: stage.left,
      stageRight: stage.right,
      imageLeft: image.left,
      imageRight: image.right,
    };
  });
  await preview.dispatchEvent("wheel", {
    deltaX: 5_000,
    clientX: 450,
    clientY: 360,
  });
  await page.waitForTimeout(120);
  const rightEdge = await horizontalEdges();
  expect(Math.abs(rightEdge.imageRight - rightEdge.stageRight)).toBeLessThan(1);

  await preview.dispatchEvent("wheel", {
    deltaX: -10_000,
    clientX: 450,
    clientY: 360,
  });
  await page.waitForTimeout(120);
  const leftEdge = await horizontalEdges();
  expect(Math.abs(leftEdge.imageLeft - leftEdge.stageLeft)).toBeLessThan(1);
});

test("desktop Mermaid content clicks are inert and the backdrop closes", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "desktop mouse behavior");
  await page.goto("/tests/history-browser.html?mermaid=1");
  const diagram = page.locator(".mermaid-block").first();
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  await diagram.locator(".mermaid-zoom").click();
  const preview = page.getByRole("dialog", { name: "Mermaid 图表预览" });
  const visual = preview.locator(".image-lightbox-visual");
  const scale = () => visual.evaluate((node) => {
    const matrix = new DOMMatrix(node.style.transform);
    return Math.hypot(matrix.a, matrix.b);
  });

  await preview.click({ position: { x: 450, y: 360 } });
  await expect(preview).toBeVisible();
  await expect.poll(scale).toBeCloseTo(1, 5);

  await preview.click({ position: { x: 5, y: 5 } });
  await expect(preview).toHaveCount(0);
});

test("invalid Mermaid falls back to copyable source", async ({ page }) => {
  await page.goto("/tests/history-browser.html?invalid-mermaid=1");
  const diagram = page.locator(".mermaid-block");
  await expect(diagram).toHaveAttribute("data-mermaid-state", "error");
  await expect(diagram.locator(".mermaid-source")).toContainText(
    "this is not a supported diagram",
  );
  await expect(diagram.getByRole("button", {
    name: "复制 Mermaid 源码",
  })).toBeVisible();
  await expect(diagram.locator(".mermaid-svg")).toHaveCount(0);
});

test("offscreen historical Mermaid does not load until its row is mounted", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?mermaid-history=1");
  await expect(page.locator('[data-turn-id="after-mermaid-40"]')).toBeVisible();
  await expect(page.locator(".mermaid-block")).toHaveCount(0);
  const before = await page.evaluate(() => performance.getEntriesByType("resource")
    .map((entry) => entry.name));
  expect(before.some((url) =>
    /\/node_modules\/\.vite\/deps\/mermaid(?:\.js|-)/i.test(url),
  )).toBe(false);

  await scrollThreadToEdge(page, "start", testInfo.project.name);
  const diagramTurn = page.locator('[data-turn-id="mermaid"]');
  await expect(diagramTurn).toBeVisible();
  await expect(diagramTurn.locator(".mermaid-block")).toHaveCount(2);
  await expect(diagramTurn.locator(".mermaid-svg")).toHaveCount(2);
});

test("switching sessions discards an in-flight Mermaid render", async ({
  page,
}) => {
  await page.route(/\/node_modules\/\.vite\/deps\/mermaid(?:\.js|-)/i, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 250));
    await route.continue();
  });
  await page.goto("/tests/history-browser.html?mermaid=1");
  await expect(page.locator(".mermaid-block")).toHaveCount(2);
  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();
  await page.waitForTimeout(400);
  await expect(page.locator(".mermaid-block")).toHaveCount(0);
  await expect(page.locator(
    "body > svg, body > div[id*='cc-remote-mermaid']",
  )).toHaveCount(0);

  await page.getByTestId("switch-session").click();
  await expect(page.locator(
    '[data-turn-id="mermaid"] .mermaid-svg',
  )).toHaveCount(2);
});

test("a pending composer image previews without triggering removal", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?composer-attachment=1");
  const preview = page.getByRole("button", { name: "预览待发送图片 1" });
  await expect(preview).toBeVisible();

  await preview.click();
  await expect(page.locator(".image-lightbox")).toBeVisible();
  const rasterSurface = page.locator(
    ".image-lightbox img.image-lightbox-image",
  );
  await expect(rasterSurface).toBeVisible();
  const rasterBackground = await rasterSurface.evaluate((node) => {
    const style = getComputedStyle(node);
    return {
      color: style.backgroundColor,
      image: style.backgroundImage,
    };
  });
  expect(rasterBackground.color).toBe("rgb(248, 249, 251)");
  expect(rasterBackground.image).not.toBe("none");
  await expect(page.locator(".image-lightbox-vector")).toHaveCount(0);
  await page.getByRole("button", { name: "关闭图片预览" }).click();
  await expect(page.locator(".image-lightbox")).toHaveCount(0);
  await expect(preview).toBeVisible();

  await preview.click();
  await expect(page.locator(".image-lightbox")).toBeVisible();
  await page.locator(".image-lightbox").evaluate((node) => {
    const stage = node as HTMLElement;
    const visual = stage.querySelector<HTMLElement>(".image-lightbox-visual");
    if (!visual) throw new Error("lightbox visual is missing");
    Object.defineProperties(stage, {
      setPointerCapture: { configurable: true, value: () => {} },
      releasePointerCapture: { configurable: true, value: () => {} },
      hasPointerCapture: { configurable: true, value: () => false },
    });
    const bounds = visual.getBoundingClientRect();
    const x = (bounds.left + bounds.right) / 2;
    const y = (bounds.top + bounds.bottom) / 2;
    stage.dispatchEvent(new PointerEvent("pointerdown", {
      bubbles: true,
      cancelable: true,
      pointerId: 73,
      pointerType: "touch",
      clientX: x,
      clientY: y,
      buttons: 1,
    }));
    stage.dispatchEvent(new PointerEvent("pointerup", {
      bubbles: true,
      cancelable: true,
      pointerId: 73,
      pointerType: "touch",
      clientX: x,
      clientY: y,
    }));
    stage.dispatchEvent(new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      clientX: x,
      clientY: y,
      detail: 1,
    }));
  });
  await expect(page.locator(".image-lightbox")).toHaveCount(0);
  await expect(preview).toBeVisible();

  await page.getByRole("button", { name: "移除待发送图片 1" }).click();
  await expect(preview).toHaveCount(0);
  await expect(page.locator(".image-lightbox")).toHaveCount(0);
});

test("a page waits through post-touch momentum and restores its final boundary", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit touch settlement");
  await page.goto("/tests/history-browser.html?delay=5&manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });

  await dispatchTouchPhase(page, "touchstart", 160);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await page.waitForTimeout(50);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
  // The network page may remain ready for multiple frames, but it cannot
  // rebase the still-active gesture onto a not-yet-mounted older row.
  await page.waitForTimeout(100);
  await dispatchTouchPhase(page, "touchend", 220);
  // touchend does not end iOS scroll ownership: native momentum continues to
  // emit scroll events without a finger. Keep the page staged, allow those
  // movements and unrelated row growth, then commit at the final idle point.
  await page.waitForTimeout(60);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
  let momentumScrollTop = 0;
  for (const top of [240, 420, 540]) {
    momentumScrollTop = await viewport.evaluate((node, scrollTop) => {
      node.scrollTop = scrollTop;
      node.dispatchEvent(new Event("scroll"));
      return node.scrollTop;
    }, top);
    await page.waitForTimeout(50);
  }
  // Capture the final momentum boundary before any slow-runner command can
  // legitimately cross the 260 ms idle lease and mount the staged page.
  const momentumEnd = await readingAnchor(page);
  expect(momentumScrollTop).toBeGreaterThan(200);

  await page.getByTestId("grow-row").click();
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await expect.poll(async () => (await readingAnchor(page)).id)
    .toBe(momentumEnd.id);
  await page.waitForTimeout(350);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(momentumEnd.id);
  expect(Math.abs(settled.offset - momentumEnd.offset)).toBeLessThan(2);
});

test("a delayed pre-commit touchmove never rebases a retained page", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit touch settlement");
  await page.goto("/tests/history-browser.html?delay=5&manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const before = await readingAnchor(page);

  // Create the reverse move before the page request. WebKit may queue that
  // native event and deliver it only after React commits the prepend.
  await stageDelayedTouchMove(page, 160, 80);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await page.waitForTimeout(50);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
  await dispatchDelayedTouchMove(page);
  await viewport.evaluate((node) => {
    node.scrollTop = 400;
    node.dispatchEvent(new Event("scroll"));
    node.scrollTop = 0;
    node.dispatchEvent(new Event("scroll"));
  });
  await dispatchTouchPhase(page, "touchend", 80);
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();

  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
});

test("a cached-newer page that finishes under touch keeps its retained row", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit touch settlement");
  await page.goto("/tests/history-browser.html?deep-browse=1&delay=5");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await dispatchTouchPhase(page, "touchstart", 220);
  await dispatchTouchPhase(page, "touchmove", 160);
  await expect(page.getByTestId("newer-load-count")).toHaveText("1");
  await expect(page.getByTestId("newest-turn-id")).toHaveText("m28");
  await dispatchTouchPhase(page, "touchend", 160);

  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
  await page.waitForTimeout(350);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(before.id);
  expect(Math.abs(settled.offset - before.offset)).toBeLessThan(2);
});

test("movement while a page is staged becomes the release boundary", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit touch settlement");
  await page.goto("/tests/history-browser.html?delay=5&manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const original = await readingAnchor(page);

  await dispatchTouchPhase(page, "touchstart", 160);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await page.waitForTimeout(50);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);

  // The response is staged, while the same finger deliberately reverses
  // toward newer content before it is lifted. That real position becomes the
  // keyed anchor used when the page finally mounts.
  await dispatchTouchPhase(page, "touchmove", 80);
  await viewport.evaluate((node) => { node.scrollBy({ top: 720 }); });
  await expect.poll(async () => (await readingAnchor(page)).id)
    .not.toBe(original.id);
  // WebKit dispatches scroll before the virtualizer has necessarily committed
  // the newly visible row measurements. Freeze the user's actual settled
  // reading position, not that intermediate layout frame.
  await waitForScrollIdle(page);
  const moved = await readingAnchor(page);
  await dispatchTouchPhase(page, "touchend", 80);
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();

  await expect.poll(async () => (await readingAnchor(page)).id).toBe(moved.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - moved.offset),
  ).toBeLessThan(2);
  await page.waitForTimeout(300);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(moved.id);
  expect(Math.abs(settled.offset - moved.offset)).toBeLessThan(2);
});

test("continuing to pull at the top keeps the staged page invisible", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit touch settlement");
  await page.goto("/tests/history-browser.html?delay=5&manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const original = await readingAnchor(page);

  await dispatchTouchPhase(page, "touchstart", 160);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await page.waitForTimeout(50);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);

  // The same finger keeps pulling into the top edge. The old page cannot move
  // the DOM out from under that gesture; it mounts once, after release.
  await dispatchTouchPhase(page, "touchmove", 280);
  await viewport.evaluate((node) => { node.scrollBy({ top: -720 }); });
  await waitForScrollIdle(page);
  const moved = await readingAnchor(page);
  expect(moved.id).toBe(original.id);
  await dispatchTouchPhase(page, "touchend", 280);
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();

  await expect.poll(async () => (await readingAnchor(page)).id).toBe(moved.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - moved.offset),
  ).toBeLessThan(2);
  await page.waitForTimeout(300);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(moved.id);
  expect(Math.abs(settled.offset - moved.offset)).toBeLessThan(2);
});

test("repeated prepends preserve each page boundary instead of jumping to the inserted page", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?pages=4&delay=5&manual-growth=1");
  const viewport = page.locator(".thread");

  for (let pageNumber = 1; pageNumber <= 4; pageNumber += 1) {
    if (pageNumber === 1) {
      await viewport.evaluate((node) => { node.scrollTop = 0; });
    }
    await waitForScrollIdle(page);
    const before = await readingAnchor(page);
    const beforeScrollHeight = await viewport.evaluate((node) => node.scrollHeight);
    if (pageNumber === 1) {
      await requestOlderHistory(page, testInfo.project.name);
    } else {
      await page.locator(".load-more-btn").dispatchEvent("click");
    }
    await expect(page.getByTestId("load-count")).toHaveText(String(pageNumber));
    const insertedOldestId = pageNumber === 1 ? "n1" : `p${pageNumber}-1`;
    await expect.poll(async () =>
      viewport.evaluate((node) => node.scrollHeight),
    ).toBeGreaterThan(beforeScrollHeight);

    await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
    await expect.poll(async () =>
      Math.abs((await readingAnchor(page)).offset - before.offset),
    ).toBeLessThan(2);
    expect((await readingAnchor(page)).id).not.toBe(insertedOldestId);
    // End the wheel/touch gesture before pulling the next page. The product
    // intentionally allows only one request per physical gesture.
    await page.waitForTimeout(250);
  }
});

test("the first runtime-to-browse page preserves its captured reading row", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?runtime-browse=1&delay=350&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await page.locator(".load-more-btn").dispatchEvent("click");
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.getByTestId("history-page-activity")).toBeVisible();
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
});

test("a script-only bottom write cannot leave history browse", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?runtime-browse=1&delay=5&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await page.locator(".load-more-btn").dispatchEvent("click");
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await expect(page.getByRole("button", { name: "回到最新" })).toBeVisible();
  await waitForScrollIdle(page);

  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await page.waitForTimeout(100);

  await expect(page.getByRole("button", { name: "回到最新" })).toBeVisible();
});

test("dragging the deep-history scrollbar to the bottom returns to the live tail", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?deep-browse=1&delay=5");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await waitForScrollIdle(page);
  await expect(page.getByRole("button", { name: "回到最新" })).toBeVisible();
  await expect(page.getByTestId("newest-turn-id")).toHaveText("m20");

  const gutterPoint = await viewport.evaluate((node) => {
    const rect = node.getBoundingClientRect();
    return { x: rect.right - 2, y: rect.top + 24 };
  });

  await viewport.dispatchEvent("pointerdown", {
    pointerType: "mouse", button: 0, buttons: 1, isPrimary: true,
    pointerId: 71, clientX: gutterPoint.x, clientY: gutterPoint.y,
  });
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });

  await expect(page.getByRole("button", { name: "回到最新" })).toHaveCount(0);
  await expect(page.getByTestId("newer-load-count")).toHaveText("0");
  await expect(page.getByTestId("newest-turn-id")).toHaveText("m40");
  const completedTail = page.locator('[data-turn-id="m40"]');
  await expect(completedTail.locator(".turn-done-mark")).toBeVisible();
  await expect(completedTail.locator(".ai-meta .ubub-time")).toBeVisible();
  await expect(completedTail.locator(
    '.ai-meta .ubub-act[aria-label="复制"]',
  )).toBeVisible();
});

test("dark chat threads expose a contrasting native scrollbar", async ({ page }) => {
  await page.goto("/tests/history-browser.html?large=40");
  await page.evaluate(() => {
    document.documentElement.dataset.theme = "dark";
  });
  const style = await page.locator(".thread").evaluate((node) => {
    const computed = getComputedStyle(node);
    return {
      colorScheme: computed.colorScheme,
      // WebKit applies scrollbar-color/color-scheme but does not expose the
      // non-standard scrollbarColor DOM property through getComputedStyle.
      scrollbarColor: computed.getPropertyValue("scrollbar-color").trim(),
    };
  });
  expect(style.colorScheme).toBe("dark");
  if (style.scrollbarColor) {
    expect(style.scrollbarColor).not.toBe("auto");
    expect(style.scrollbarColor)
      .not.toMatch(/^transparent(?:\s+transparent)?$/);
  }
});

test("one upward gesture starts at most one older-page request", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await requestOlderHistory(page, testInfo.project.name, 2);
  await expect(page.getByTestId("load-count")).toHaveText("1");
});

test("cached-newer append with head eviction preserves the reading row", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?deep-browse=1&delay=5&manual-growth=1");
  const viewport = page.locator(".thread");
  await expect(page.locator('[data-turn-id="m20"]')).toBeVisible();
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await requestNewerHistory(page, testInfo.project.name);
  await expect(page.getByTestId("newer-load-count")).toHaveText("1");
  await expect(page.getByTestId("newest-turn-id")).toHaveText("m28");
  await expect(page.locator('[data-turn-id="m1"]')).toHaveCount(0);
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);

  // A late image/Markdown measurement before the retained row must reuse the
  // same transaction instead of introducing a second scroll writer.
  await page.getByTestId("grow-row").click();
  await expect(page.locator('[data-turn-id="m15"] p')).toHaveCount(28);
  await waitForScrollIdle(page);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(before.id);
  expect(Math.abs(settled.offset - before.offset)).toBeLessThan(2);
});

test("one downward gesture starts at most one cached-newer page", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?deep-browse=1&delay=80");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await requestNewerHistory(page, testInfo.project.name, 2);
  await expect(page.getByTestId("newer-load-count")).toHaveText("1");
});

test("repeated cached-newer pages keep the protected row through the final page", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?deep-browse=1&delay=5");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await waitForScrollIdle(page);
  const expectedNewest = ["m28", "m36", "m40"];

  for (let index = 0; index < expectedNewest.length; index += 1) {
    const before = await readingAnchor(page);
    await page.getByRole("button", { name: "加载更新的历史" })
      .dispatchEvent("click");
    await expect(page.getByTestId("newer-load-count"))
      .toHaveText(String(index + 1));
    await expect(page.getByTestId("newest-turn-id"))
      .toHaveText(expectedNewest[index]);
    await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
    await expect.poll(async () =>
      Math.abs((await readingAnchor(page)).offset - before.offset),
    ).toBeLessThan(2);
    await page.waitForTimeout(80);
  }
  await expect(page.getByRole("button", {
    name: "加载更新的历史",
  })).toHaveCount(0);
});

test("browse live updates stay passive until the user returns to latest", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?deep-browse=1");
  await expect(page.locator('[data-turn-id="m20"]')).toBeVisible();
  await wheelUntilTurn(page, "m12", -1_200, testInfo.project.name);
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await page.getByTestId("append-turn").click();
  await page.waitForTimeout(100);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
  await expect(page.locator('[data-turn-id="live-41"]')).toHaveCount(0);

  const returnButton = page.getByRole("button", { name: "回到最新" });
  await expect(returnButton).toHaveText("");
  const buttonBox = await returnButton.boundingBox();
  expect(buttonBox).not.toBeNull();
  expect(Math.abs((buttonBox?.width ?? 0) - (buttonBox?.height ?? 0)))
    .toBeLessThan(1);
  await returnButton.click();
  await expect(page.locator('[data-turn-id="live-41"]')).toBeVisible();
  await expect(page.getByRole("button", { name: "回到最新" })).toHaveCount(0);
});

test("a delayed cached-newer page cannot move another session", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?deep-browse=1&delay=350");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await requestNewerHistory(page, testInfo.project.name);
  await expect(page.getByTestId("history-page-activity")).toBeVisible();
  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);
  await page.waitForTimeout(500);
  await waitForScrollIdle(page);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
  await expect(page.locator('[data-turn-id="m28"]')).toHaveCount(0);
});

test("an empty final page removes the loader without moving the reading row", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?empty-final=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const before = await readingAnchor(page);
  await requestOlderHistory(page, testInfo.project.name);
  await expect(page.getByRole("button", {
    name: "加载更早的历史",
  })).toHaveCount(0);
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("user movement after prepend stays stable through delayed growth", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const initial = await readingAnchor(page);
  await requestOlderHistory(page, testInfo.project.name);
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(initial.id);
  await wheelUntilTurn(page, "o2", 300, testInfo.project.name);
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);
  const sampled = await maxPaintedTurnOffsetShiftThroughAction(
    page,
    before.id,
    "grow-row",
  );
  await expect(page.locator('[data-turn-id="n8"] p')).toHaveCount(28);
  await waitForScrollIdle(page);
  const after = await readingAnchor(page);
  expect(sampled.missing).toBe(false);
  expect(sampled.maxShift).toBeLessThan(2);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("a delayed page from the previous session cannot move the new session", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?delay=350");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await requestOlderHistory(page, testInfo.project.name);
  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();
  await page.waitForTimeout(500);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();

  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="o4"]')).toBeVisible();
});

test("a generation change cancels the old page anchor and permits a new request", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?generation-shift=1&delay=1000&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await page.locator(".load-more-btn").dispatchEvent("click");
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.getByTestId("history-page-activity")).toBeVisible();

  await page.getByTestId("shift-generation").click();
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
  await page.locator(".load-more-btn").dispatchEvent("click");
  await expect(page.getByTestId("load-count")).toHaveText("2");
});

test("a generation change clears the automatic keyboard paging boundary", async ({
  page,
}, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name),
    "desktop keyboard path");
  await page.goto(
    "/tests/history-browser.html?generation-shift=1&delay=10000&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.focus();
  await viewport.press("Home");
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.getByTestId("history-page-activity")).toBeVisible();

  await page.getByTestId("shift-generation").click();
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
  const keyboardBaseline = await viewport.evaluate((node) => {
    // Chromium may expose the End position before delivering its scroll event,
    // then coalesce that event with the following Home movement. Reproduce the
    // ordering deterministically: keydown sees the physical bottom position,
    // while the only delivered scroll event observes the final top position.
    node.scrollTop = node.scrollHeight;
    const bottom = node.scrollTop;
    node.dispatchEvent(new KeyboardEvent("keydown", {
      bubbles: true,
      key: "Home",
    }));
    node.scrollTop = 0;
    node.dispatchEvent(new Event("scroll"));
    return bottom;
  });
  expect(keyboardBaseline).toBeGreaterThan(0);
  await expect(page.getByTestId("load-count")).toHaveText("2");
});

test("same-session revision replacement resets to the latest row", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?large=40");
  await expect(page.locator('[data-turn-id="m40"]')).toBeVisible();
  await wheelUntilTurn(page, "m1", -2_000, testInfo.project.name);
  await expect(page.locator('[data-turn-id="m1"]')).toBeVisible();

  await page.getByTestId("replace-revision").click();
  await expect(page.locator('[data-turn-id="m1"]')).toHaveCount(0);
  await expect(page.locator('[data-turn-id="r24"]')).toBeVisible();
  await expect(page.locator('[data-turn-id="r1"]')).toHaveCount(0);
});

test("pending history handoffs retain rows only within one authority", async ({
  page,
}, testInfo) => {
  await page.goto(
    "/tests/history-browser.html?large=40&pending-revision-replace=1",
  );
  await expect(page.locator('[data-turn-id="m40"]')).toBeVisible();
  await wheelUntilTurn(page, "m10", -400, testInfo.project.name);
  await waitForReadingPositionIdle(page);
  const before = await readingAnchor(page);

  await page.getByTestId("replace-revision").click();
  await expect(page.getByTestId("history-transition-state"))
    .toHaveText("pending");
  const samples = await page.evaluate(async () => {
    const readings: Array<{
      count: number;
      id: string | null;
      offset: number | null;
      scrollTop: number;
    }> = [];
    const deadline = performance.now() + 100;
    while (performance.now() < deadline) {
      await new Promise<void>((resolveFrame) =>
        requestAnimationFrame(() => resolveFrame()));
      const viewport = document.querySelector<HTMLElement>(".thread");
      if (!viewport) throw new Error("thread viewport is missing");
      const viewportRect = viewport.getBoundingClientRect();
      const rows = [...document.querySelectorAll<HTMLElement>("[data-turn-id]")]
        .map((row) => ({ row, rect: row.getBoundingClientRect() }))
        .filter(({ rect }) =>
          rect.bottom > viewportRect.top && rect.top < viewportRect.bottom)
        .sort((left, right) =>
          Math.abs(left.rect.top - viewportRect.top)
          - Math.abs(right.rect.top - viewportRect.top));
      readings.push({
        count: document.querySelectorAll("[data-turn-id]").length,
        id: rows[0]?.row.dataset.turnId ?? null,
        offset: rows[0] ? rows[0].rect.top - viewportRect.top : null,
        scrollTop: viewport.scrollTop,
      });
    }
    return readings;
  });
  expect(samples.length).toBeGreaterThan(2);
  expect(samples.every((sample) => sample.count > 0)).toBe(true);
  expect(samples.every((sample) => sample.id === before.id)).toBe(true);
  expect(samples.every((sample) => sample.offset != null
    && Math.abs(sample.offset - before.offset) < 2)).toBe(true);
  expect(samples.every((sample) => sample.scrollTop > 1)).toBe(true);

  await expect(page.getByTestId("history-transition-state"))
    .toHaveText("ready");
  await expect(page.locator('[data-turn-id="m10"]')).toHaveCount(0);
  await expect(page.locator('[data-turn-id="r24"]')).toBeVisible();

  await page.getByTestId("replace-authority").click();
  await expect(page.getByTestId("session-authority-scope"))
    .toHaveText("fixture-authority-b");
  expect(await page.locator('[data-turn-id="r24"]').count()).toBe(0);

  await expect(page.getByTestId("history-transition-state"))
    .toHaveText("ready");
  await expect(page.locator('[data-turn-id="r24"]')).toBeVisible();
});

test("replay recovery replacement preserves the current reading row", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?large=40&recovery-replace=1");
  await expect(page.locator('[data-turn-id="m40"]')).toBeVisible();
  await wheelUntilTurn(page, "m10", -400, testInfo.project.name);
  await waitForReadingPositionIdle(page);
  const before = await readingAnchor(page);

  await page.getByTestId("replace-revision").click();
  await expect(page.locator('[data-turn-id="m10"] p')).toHaveCount(4);
  await waitForReadingPositionIdle(page);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("reversing direction while a page is pending preserves the reading row", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?delay=700");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await requestOlderHistory(page, testInfo.project.name);
  await wheelUntilTurn(page, "o4", 2_000, testInfo.project.name);
  const before = await readingAnchor(page);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(1);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("virtualization bounds mounted rows and preserves an expanded timeline", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?timeline=1");
  await scrollThreadToEdge(page, "start", testInfo.project.name);
  const timeline = page.locator('[data-turn-id="timeline"]');
  await expect(timeline).toBeVisible();
  await timeline.locator(".turn-process-head").click();
  await expect(timeline.locator(".turn-process-head")).toHaveAttribute("aria-expanded", "true");

  await scrollThreadToEdge(page, "end", testInfo.project.name);
  await expect(timeline).toHaveCount(0);
  expect(await page.locator(".turn").count()).toBeLessThan(40);

  await scrollThreadToEdge(page, "start", testInfo.project.name);
  await expect(timeline).toBeVisible();
  await expect(timeline.locator(".turn-process-head")).toHaveAttribute("aria-expanded", "true");
});

test("plan progress uses a compact popover that closes outside", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?timeline=1");
  await scrollThreadToEdge(page, "start", testInfo.project.name);
  const timeline = page.locator('[data-turn-id="timeline"]');
  const trigger = timeline.getByRole("button", { name: /查看计划进度/ });
  await expect(trigger).toBeVisible();
  await trigger.click();

  const popover = page.getByRole("dialog", { name: "计划进度" });
  await expect(popover).toBeVisible();
  await expect(popover).toContainText("1 / 3");
  await expect(popover).toContainText("验证计划弹层");

  const box = await popover.boundingBox();
  const viewport = page.viewportSize();
  const anchorBox = await trigger.boundingBox();
  if (!box || !viewport || !anchorBox) {
    throw new Error("plan popover has no geometry");
  }
  expect(box.x).toBeGreaterThanOrEqual(8);
  expect(box.x + box.width).toBeLessThanOrEqual(viewport.width - 8);
  expect(box.y).toBeGreaterThanOrEqual(8);
  expect(box.y + box.height).toBeLessThanOrEqual(viewport.height - 8);
  const expectedCenter = Math.min(
    Math.max(anchorBox.x + anchorBox.width / 2, 16 + box.width / 2),
    viewport.width - 16 - box.width / 2,
  );
  expect(Math.abs(box.x + box.width / 2 - expectedCenter)).toBeLessThan(2);
  expect(Math.abs(box.y + box.height - (anchorBox.y - 8)))
    .toBeLessThan(2);

  await trigger.click();
  await expect(popover).toHaveCount(0);
  await trigger.click();
  await expect(popover).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(popover).toHaveCount(0);
  await trigger.click();
  await expect(popover).toBeVisible();

  await page.getByTestId("load-count").click();
  await expect(popover).toHaveCount(0);
});

test("historical Plan flips below a trigger near the top edge", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?timeline=1");
  await scrollThreadToEdge(page, "start", testInfo.project.name);
  const trigger = page.locator('[data-turn-id="timeline"]')
    .getByRole("button", { name: /查看计划进度/ });
  await expect(trigger).toBeVisible();
  if (isMobileWebKitProject(testInfo.project.name)) {
    await dispatchTouchGesture(page, -60);
  } else {
    await page.locator(".thread").dispatchEvent("wheel", { deltaY: 80 });
  }
  await trigger.evaluate((node) => {
    const viewport = node.closest<HTMLElement>(".thread");
    if (!viewport) throw new Error("historical Plan has no thread viewport");
    const viewportBox = viewport.getBoundingClientRect();
    const triggerBox = node.getBoundingClientRect();
    viewport.scrollTop += triggerBox.y - (viewportBox.y + 20);
  });
  await expect.poll(() => trigger.evaluate((node) => {
    const viewport = node.closest<HTMLElement>(".thread");
    if (!viewport) return false;
    const viewportBox = viewport.getBoundingClientRect();
    const triggerBox = node.getBoundingClientRect();
    const relativeTop = triggerBox.y - viewportBox.y;
    const settled = relativeTop >= 16 && relativeTop < 24;
    if (!settled) {
      viewport.scrollTop += relativeTop - 20;
    }
    return settled && triggerBox.bottom <= viewportBox.bottom;
  })).toBe(true);

  await trigger.click();
  const popover = page.getByRole("dialog", { name: "计划进度" });
  await expect(popover).toBeVisible();
  await expect(popover).toHaveAttribute("data-placement", "below");
  const { threadBox, anchorBox, popoverBox } = await trigger.evaluate((node) => {
    const viewport = node.closest<HTMLElement>(".thread");
    const dialog = document.querySelector<HTMLElement>(
      '[role="dialog"][aria-label="计划进度"]',
    );
    if (!viewport || !dialog) throw new Error("near-top Plan has no geometry");
    const bounds = (element: Element) => {
      const box = element.getBoundingClientRect();
      return {
        x: box.x,
        y: box.y,
        width: box.width,
        height: box.height,
      };
    };
    return {
      threadBox: bounds(viewport),
      anchorBox: bounds(node),
      popoverBox: bounds(dialog),
    };
  });
  expect(Math.abs(popoverBox.y - (anchorBox.y + anchorBox.height + 8)))
    .toBeLessThan(2);
  expect(popoverBox.height).toBeGreaterThan(64);
  expect(popoverBox.y + popoverBox.height)
    .toBeLessThanOrEqual(threadBox.y + threadBox.height - 15);
});

test("a long active turn keeps its plan beside the composer", async ({ page }) => {
  await page.goto("/tests/history-browser.html?persistent-plan=1");
  const thread = page.locator(".thread");
  await thread.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await expect.poll(() => thread.evaluate((node) => node.scrollTop))
    .toBeGreaterThan(200);
  await expect(page.locator('[data-turn-id="persistent-plan"]')
    .getByRole("button", { name: /查看计划进度/ })).toHaveCount(0);

  const chip = page.getByRole("button", { name: /查看计划进度/ });
  await expect(chip).toBeVisible();
  await chip.click();
  await expect(page.getByRole("dialog", { name: "计划进度" }))
    .toContainText("验证计划弹层");
});

test("an old terminal plan stays with its historical turn", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?historical-plan=1");

  await scrollThreadToEdge(page, "start", testInfo.project.name);
  const planTurn = page.locator('[data-turn-id="historical-plan"]');
  await expect(planTurn).toBeVisible();
  const trigger = planTurn.getByRole("button", { name: /查看计划进度/ });
  await expect(trigger).toBeVisible();
  await expect(page.getByRole("button", { name: /查看计划进度/ }))
    .toHaveCount(1);

  await trigger.click();
  await expect(page.getByRole("dialog", { name: "计划进度" }))
    .toContainText("验证计划弹层");
});

test("iOS pointercancel releases the plan trigger without opening it", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit pointer cancellation");
  await page.goto("/tests/history-browser.html?interactive-timeline=1");
  const trigger = page.getByRole("button", { name: /查看计划进度/ });
  await expect(trigger).toBeVisible();

  await dispatchCancelledTouchTap(trigger, 183);

  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("dialog", { name: "计划进度" }))
    .toHaveCount(0);
});

test("terminal turn does not mark unfinished structured plan complete", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?plan-ui=terminal");
  await page.getByRole("button", { name: /查看计划进度/ }).click();
  const popover = page.getByRole("dialog", { name: "计划进度" });
  await expect(popover).toContainText("1 / 3");
  await expect(popover).toContainText("本轮已结束，计划未更新");
  await expect(popover).not.toContainText("全部完成");
});

test("unstructured plan detail remains visible in the compact popover", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?plan-ui=unstructured");
  await page.getByRole("button", { name: /查看计划进度/ }).click();
  const popover = page.getByRole("dialog", { name: "计划进度" });
  await expect(popover).toContainText("已记录");
  await expect(popover).not.toContainText("全部完成");
  await expect(popover).toContainText("先检查协议，再验证移动端，最后发布。");
});

test("opening a cached plan refreshes authoritative detail", async ({ page }) => {
  await page.goto("/tests/history-browser.html?plan-ui=refresh");
  await page.getByRole("button", { name: /查看计划进度/ }).click();
  await expect(page.getByTestId("plan-detail-requests")).toHaveText("1");
  const popover = page.getByRole("dialog", { name: "计划进度" });
  await expect(page.getByTestId("plan-refresh-state")).toHaveText("loading");
  await expect(popover).toBeVisible();
  await expect(popover).toContainText("缓存步骤二");
  await expect(page.getByTestId("plan-refresh-state")).toHaveText("ready");
  await expect(popover).toContainText("权威步骤二");
  await expect(popover).not.toContainText("缓存步骤二");
});

test("only the selected plan block moves into the compact popover", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?plan-ui=mixed");
  await page.locator(".turn-process-head").click();
  await expect(page.getByText("旧版计划", { exact: true })).toBeVisible();
});

test("a turn plan without a Goal stays in a compact session-level strip", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=none&plan=1");
  const chip = page.getByRole("button", { name: /查看计划进度/ });
  await expect(chip).toBeVisible();
  await expect(chip).toContainText("实现固定入口");
  const box = await chip.boundingBox();
  if (!box) throw new Error("plan strip has no geometry");
  expect(box.height).toBeLessThanOrEqual(42);

  await chip.click();
  await expect(page.getByTestId("plan-detail-requests")).toHaveText("1");
  const dialog = page.getByRole("dialog", { name: "计划进度" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("1 / 3");
  await expect(dialog).toContainText("完成浏览器回归");
  await expect(page.locator(".scrim.show")).toHaveCount(0);
  await expect(page.locator(".plan-sheet")).toHaveCount(0);
  const dialogBox = await dialog.boundingBox();
  if (!dialogBox) throw new Error("plan popover has no geometry");
  expect(dialogBox.width).toBeLessThan(361);
  const anchorBox = await chip.boundingBox();
  if (!anchorBox) throw new Error("plan popover has no anchor");
  expect(Math.abs(
    dialogBox.x + dialogBox.width / 2
      - (anchorBox.x + anchorBox.width / 2),
  )).toBeLessThan(2);
  expect(Math.abs(
    dialogBox.y + dialogBox.height - (anchorBox.y - 8),
  )).toBeLessThan(2);
  const openChipBox = await chip.boundingBox();
  if (!openChipBox) throw new Error("open plan strip has no geometry");
  expect(Math.abs(openChipBox.x - box.x)).toBeLessThan(1);
  expect(Math.abs(openChipBox.y - box.y)).toBeLessThan(1);

  await page.locator("[data-testid=goal-fixture-content]").click({
    position: { x: 5, y: 5 },
  });
  await expect(dialog).toHaveCount(0);
});

test("standalone Plan closes from outside and fits the mobile viewport", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 720 });
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=none&plan=1");
  await page.getByRole("button", { name: /查看计划进度/ }).click();
  const dialog = page.getByRole("dialog", { name: "计划进度" });
  await expect(dialog).toBeVisible();
  const box = await dialog.boundingBox();
  if (!box) throw new Error("mobile plan has no geometry");
  const anchor = page.getByRole("button", { name: /查看计划进度/ });
  const anchorBox = await anchor.boundingBox();
  if (!anchorBox) throw new Error("mobile plan has no anchor");
  expect(box.x).toBeGreaterThanOrEqual(11);
  expect(box.x + box.width).toBeLessThanOrEqual(379);
  const visualBottom = await page.evaluate(() => (
    (window.visualViewport?.offsetTop ?? 0)
      + (window.visualViewport?.height ?? window.innerHeight)
  ));
  expect(box.y + box.height).toBeLessThanOrEqual(visualBottom - 11);
  expect(Math.abs(box.y + box.height - (anchorBox.y - 8)))
    .toBeLessThan(2);
  await page.locator("[data-testid=goal-fixture-content]").click({
    position: { x: 5, y: 5 },
  });
  await expect(dialog).toHaveCount(0);
});

test("Plan follows chat and composer geometry changes", async ({ page }) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=none&plan=1");
  await page.getByRole("button", { name: /查看计划进度/ }).click();
  const dialog = page.getByRole("dialog", { name: "计划进度" });
  const chip = page.getByRole("button", { name: /查看计划进度/ });
  const initialBox = await dialog.boundingBox();
  const initialAnchorBox = await chip.boundingBox();
  if (!initialBox || !initialAnchorBox) {
    throw new Error("Plan has no initial geometry");
  }
  await page.getByTestId("goal-fixture-composer").evaluate((node) => {
    node.style.height = "176px";
  });
  await expect.poll(async () => {
    const anchorBox = await chip.boundingBox();
    return anchorBox?.y ?? null;
  }).toBeLessThan(initialAnchorBox.y - 40);
  await expect.poll(async () => {
    const [currentDialog, currentAnchor] = await Promise.all([
      dialog.boundingBox(),
      chip.boundingBox(),
    ]);
    if (!currentDialog || !currentAnchor) return null;
    return Math.abs(
      currentDialog.y + currentDialog.height - (currentAnchor.y - 8),
    );
  }).toBeLessThan(2);
  const [resizedDialogBox, resizedAnchorBox, composerBox] = await Promise.all([
    dialog.boundingBox(),
    chip.boundingBox(),
    page.getByTestId("goal-fixture-composer").boundingBox(),
  ]);
  if (!resizedDialogBox || !resizedAnchorBox || !composerBox) {
    throw new Error("tall-composer Plan fixture has no geometry");
  }
  expect(Math.abs(
    resizedDialogBox.y + resizedDialogBox.height - (resizedAnchorBox.y - 8),
  )).toBeLessThan(2);
  expect(resizedDialogBox.y + resizedDialogBox.height).toBeLessThanOrEqual(
    composerBox.y - 15,
  );

  await page.getByTestId("goal-fixture-spacer").evaluate((node) => {
    node.style.width = "180px";
  });
  await expect.poll(async () => {
    const [currentDialog, currentAnchor] = await Promise.all([
      dialog.boundingBox(),
      chip.boundingBox(),
    ]);
    if (!currentDialog || !currentAnchor) return null;
    return Math.abs(
      currentDialog.x + currentDialog.width / 2
        - (currentAnchor.x + currentAnchor.width / 2),
    );
  }).toBeLessThan(2);
});

test("a long Plan scrolls within the space above its anchor", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 520 });
  await page.goto(
    "/tests/history-browser.html?goal-ui=1&goal-status=none&plan=1&plan-long=1",
  );
  const chip = page.getByRole("button", { name: /查看计划进度/ });
  await chip.click();
  const dialog = page.getByRole("dialog", { name: "计划进度" });
  await expect(dialog).toBeVisible();
  const [box, anchorBox] = await Promise.all([
    dialog.boundingBox(),
    chip.boundingBox(),
  ]);
  if (!box || !anchorBox) throw new Error("long Plan has no geometry");
  expect(Math.abs(box.y + box.height - (anchorBox.y - 8)))
    .toBeLessThan(2);
  expect(box.y).toBeGreaterThanOrEqual(15);
  expect(await dialog.evaluate((node) => ({
    clientHeight: node.clientHeight,
    scrollHeight: node.scrollHeight,
    overflowY: getComputedStyle(node).overflowY,
  }))).toMatchObject({ overflowY: "auto" });
  expect(await dialog.evaluate((node) => node.scrollHeight))
    .toBeGreaterThan(await dialog.evaluate((node) => node.clientHeight));
});

test("a completed session Plan disappears when the next message begins", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?plan-lifecycle=1");
  const chip = page.getByRole("button", { name: /查看计划进度/ });
  await expect(chip).toBeVisible();
  await expect(chip.locator(".plan-chip-ring.complete")).toHaveCount(1);
  await expect(chip).toContainText("2 / 2");

  await page.getByTestId("send-next-plan-message").click();
  await expect(chip).toHaveCount(0);
});

test("an interrupted session Plan disappears when the next message begins", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?plan-lifecycle=interrupted");
  const chip = page.getByRole("button", { name: /查看计划进度/ });
  await expect(chip).toBeVisible();
  await expect(chip.locator(".plan-chip-ring.failed")).toHaveCount(1);
  await expect(chip).toContainText("1 / 2");

  await page.getByTestId("send-next-plan-message").click();
  await expect(chip).toHaveCount(0);
});

test("an existing Goal owns the turn plan in its detail sheet", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&plan=1");
  await expect(page.getByRole("button", { name: /查看计划进度/ }))
    .toHaveCount(0);
  await page.getByRole("button", { name: /查看 Goal/ }).click();
  const dialog = page.getByRole("dialog", { name: "Codex Goal" });
  const planEntry = dialog.getByRole("button", { name: /查看计划进度/ });
  await expect(planEntry).toBeVisible();
  await expect(planEntry).toContainText("实现固定入口");
  await expect(page.getByTestId("plan-detail-requests")).toHaveText("0");
  await planEntry.click();
  await expect(page.getByTestId("plan-detail-requests")).toHaveText("1");
  await expect(dialog.getByLabel("计划执行状态")).toContainText("1 / 3");
  await expect(dialog.getByLabel("计划执行状态"))
    .toContainText("实现固定入口");
});

test("a completed Goal keeps the Plan from its own final turn", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=complete&plan=1");
  await expect(page.getByRole("button", { name: /查看 Goal/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /查看计划进度/ }))
    .toHaveCount(0);

  await page.getByRole("button", { name: /查看 Goal/ }).click();
  await expect(page.getByRole("dialog", { name: "Codex Goal" })
    .getByRole("button", { name: /查看计划进度/ })).toBeVisible();
});

test("a new turn retires its completed Goal and owns a standalone Plan", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?goal-ui=1&goal-status=complete&plan=1&goal-next-turn=1",
  );
  await expect(page.getByRole("button", { name: /查看 Goal/ })).toHaveCount(0);

  const plan = page.getByRole("button", { name: /查看计划进度/ });
  await expect(plan).toBeVisible();
  await expect(plan).toContainText("实现固定入口");
  await plan.click();
  await expect(page.getByRole("dialog", { name: "计划进度" }))
    .toContainText("完成浏览器回归");
  await expect(page.getByRole("dialog", { name: "Codex Goal" })).toHaveCount(0);
});

test("a long Goal keeps its merged plan in the detail sheet first viewport", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&plan=1&goal-long=1");
  await page.getByRole("button", { name: /查看 Goal/ }).click();

  const dialog = page.getByRole("dialog", { name: "Codex Goal" });
  const planEntry = dialog.getByRole("button", { name: /查看计划进度/ });
  await expect(planEntry).toContainText("实现固定入口");
  await expect(planEntry).toBeInViewport();
  await expect(dialog.locator(".goal-sheet-scroll"))
    .toHaveJSProperty("scrollTop", 0);
  await planEntry.click();
  await expect(dialog.getByLabel("计划执行状态"))
    .toContainText("实现固定入口");
});

test("a hidden Goal cannot make the current plan inaccessible", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-hidden=1&plan=1");
  await expect(page.getByRole("button", { name: /查看 Goal/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /查看计划进度/ }))
    .toBeVisible();
});

test("goal entry stays compact and opens its editor", async ({ page }) => {
  await page.goto("/tests/history-browser.html?goal-ui=1");
  const chip = page.getByRole("button", { name: "查看 Goal" });
  await expect(chip).toBeVisible();
  const box = await chip.boundingBox();
  const viewport = page.viewportSize();
  if (!box || !viewport) throw new Error("goal chip has no geometry");
  expect(box.height).toBeLessThanOrEqual(42);
  expect(box.width).toBeLessThan(viewport.width - 20);

  await chip.click();
  const dialog = page.getByRole("dialog", { name: "Codex Goal" });
  await expect(dialog).toBeVisible();
  const dialogBox = await dialog.boundingBox();
  if (!dialogBox || !viewport) throw new Error("goal dialog has no geometry");
  expect(dialogBox.width).toBeLessThanOrEqual(Math.min(580, viewport.width));
  const chatBox = await page.locator(".thread-shell").boundingBox();
  if (!chatBox) throw new Error("goal dialog has no chat viewport");
  expect(Math.abs(
    dialogBox.x + dialogBox.width / 2 - (chatBox.x + chatBox.width / 2),
  )).toBeLessThan(2);
  expect(Math.abs(
    dialogBox.y + dialogBox.height / 2 - (chatBox.y + chatBox.height / 2),
  )).toBeLessThan(2);
  const statCards = dialog.locator(".goal-stats > div");
  await expect(statCards).toHaveCount(3);
  expect(await statCards.first().evaluate((node) =>
    getComputedStyle(node).borderTopWidth)).toBe("0px");
  const icon = dialog.locator(".goal-sheet-icon");
  expect(await icon.evaluate((node) => {
    const style = getComputedStyle(node);
    return style.backgroundColor !== style.color;
  })).toBe(true);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Codex Goal" }))
    .toHaveCount(0);
  await chip.click();
  await expect(page.getByRole("dialog", { name: "Codex Goal" }))
    .toBeVisible();
  await page.locator(".scrim.show").click({ position: { x: 5, y: 5 } });
  await expect(page.getByRole("dialog", { name: "Codex Goal" })).toHaveCount(0);
});

test("remembered goal shows a compact recovery state without opening its editor", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=loading");
  const recovery = page.getByRole("status", { name: "正在恢复 Goal" });
  await expect(recovery).toBeVisible();
  await expect(recovery).toContainText("正在恢复…");
  await expect(page.getByRole("dialog", { name: "Codex Goal" })).toHaveCount(0);
});

test("budgeted goal keeps its blocked status visible on mobile", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=blocked");
  const chip = page.getByRole("button", { name: /查看 Goal/ });
  await expect(chip).toHaveAttribute("aria-label", /受阻/);
  await expect(chip.locator(".goal-chip-ring"))
    .toHaveClass(/goal-chip-ring-blocked/);
});

test("goal editor stays inside the tablet visual viewport above the keyboard", async ({
  page,
}) => {
  await page.setViewportSize({ width: 768, height: 1024 });
  await page.goto("/tests/history-browser.html?goal-ui=1");
  await page.getByRole("button", { name: "查看 Goal" }).click();
  const dialog = page.getByRole("dialog", { name: "Codex Goal" });
  await expect(dialog).toBeVisible();

  const visualTop = 20;
  const visualHeight = 560;
  await page.evaluate(({ top, height }) => {
    const root = document.documentElement;
    root.style.setProperty("--app-offset-top", `${top}px`);
    root.style.setProperty("--app-height", `${height}px`);
    root.style.setProperty(
      "--keyboard-inset", `${window.innerHeight - top - height}px`,
    );
    window.dispatchEvent(new Event("resize"));
  }, { top: visualTop, height: visualHeight });
  await dialog.locator("textarea").focus();

  await expect.poll(async () => dialog.boundingBox()).not.toBeNull();
  const box = await dialog.boundingBox();
  if (!box) throw new Error("goal dialog has no tablet geometry");
  expect(box.y).toBeGreaterThanOrEqual(visualTop - 1);
  expect(box.y + box.height).toBeLessThanOrEqual(
    visualTop + visualHeight + 1,
  );
  expect(Math.abs(
    box.y + box.height / 2 - (visualTop + visualHeight / 2),
  )).toBeLessThan(2);
});

test("the compact Goal monitor yields while the mobile keyboard is open", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/tests/history-browser.html?goal-ui=1");
  const chip = page.getByRole("button", { name: "查看 Goal" });
  await expect(chip).toBeVisible();

  await page.evaluate(() => {
    document.documentElement.setAttribute("data-short-viewport", "ime");
  });
  await expect(chip).toBeHidden();

  await page.evaluate(() => {
    document.documentElement.setAttribute("data-short-viewport", "false");
  });
  await expect(chip).toBeVisible();
});

test("desktop text selection keeps its original virtual turn while edge-dragging", async ({
  page,
}, testInfo) => {
  test.setTimeout(60_000);
  test.skip(isMobileWebKitProject(testInfo.project.name),
    "the configured WebKit project is a touch phone; this is a desktop mouse path");
  await page.goto("/tests/history-browser.html?large=120");
  const viewport = page.locator(".thread");
  await wheelUntilTurn(page, "m42", -600, testInfo.project.name);
  await waitForScrollIdle(page);
  const startTurnId = (await readingAnchor(page)).id;
  const startText = page.locator(
    `[data-turn-id="${startTurnId}"] p`,
  ).first();
  await expect(startText).toBeInViewport();
  const viewportBox = await viewport.boundingBox();
  const startPoint = await textSelectionPoint(startText);
  if (!viewportBox) {
    throw new Error("selection fixture has no geometry");
  }
  const startScrollTop = await viewport.evaluate((node) => node.scrollTop);

  await page.mouse.move(startPoint.x, startPoint.y);
  await page.mouse.down();
  await page.mouse.move(
    viewportBox.x + viewportBox.width - 48,
    viewportBox.y + viewportBox.height - 2,
    { steps: 20 },
  );
  await expect(viewport).toHaveAttribute("data-text-selection-dragging", "true");
  // Native edge auto-scroll has platform-dependent acceleration. In Linux
  // WebKit the same 12 wheel/move pairs only advance ~150px, while macOS moves
  // much farther. Drive a real held-pointer gesture until it crosses the same
  // virtual-row distance, not for a fixed number of ~45ms frames. Never assign
  // scrollTop or replace the native selection to make this regression pass.
  let step = 0;
  await expect.poll(async () => {
    await page.mouse.wheel(0, 220);
    await page.mouse.move(
      viewportBox.x + viewportBox.width - 48 + (step++ % 2),
      viewportBox.y + viewportBox.height - 2,
    );
    return await viewport.evaluate((node) => node.scrollTop) - startScrollTop;
  }, {
    timeout: 12_000,
    intervals: [75],
    message: "native edge-drag must scroll across virtual turns while held",
  }).toBeGreaterThan(800);

  const draggedScrollTop = await viewport.evaluate((node) => node.scrollTop);
  const draggingSelection = await nativeSelectionSnapshot(page);
  expect(draggedScrollTop - startScrollTop).toBeGreaterThan(800);
  expect(draggingSelection.anchorTurnId).toBe(startTurnId);
  expect(draggingSelection.focusTurnId).not.toBe(startTurnId);
  expect(draggingSelection.anchorConnected).toBe(true);
  expect(draggingSelection.text).toContain(startTurnId);
  await expect(page.locator(
    `[data-turn-id="${startTurnId}"]`,
  )).toBeAttached();
  await page.mouse.up();
  await expect(viewport).toHaveAttribute(
    "data-text-selection-dragging", "false",
  );
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "true",
  );
  await page.evaluate(() => new Promise<void>((resolve) => {
    window.requestAnimationFrame(() => resolve());
  }));
  const immediateReleasedScrollTop =
    await viewport.evaluate((node) => node.scrollTop);
  expect(immediateReleasedScrollTop - startScrollTop).toBeGreaterThan(800);
  const nativeReleaseAdvance = immediateReleasedScrollTop - draggedScrollTop;
  expect(nativeReleaseAdvance).toBeGreaterThanOrEqual(-2);
  expect(nativeReleaseAdvance).toBeLessThan(viewportBox.height / 2);
  const immediateReleasedAnchor = await readingAnchor(page);
  // Chromium may finish one native selection auto-scroll step on mouseup and
  // cross a virtual-row boundary. The post-release position is authoritative;
  // the app must not replay an older scroll command after control returns.
  await page.waitForTimeout(120);
  const releasedAnchor = await readingAnchor(page);
  expect(releasedAnchor.id).toBe(immediateReleasedAnchor.id);
  expect(Math.abs(
    releasedAnchor.offset - immediateReleasedAnchor.offset,
  )).toBeLessThan(2);
  await page.locator(`[data-turn-id="${startTurnId}"]`).evaluate((node) => {
    (node as HTMLElement).style.paddingBottom = "320px";
  });
  await page.waitForTimeout(120);
  const measuredAnchor = await readingAnchor(page);
  expect(measuredAnchor.id).toBe(releasedAnchor.id);
  expect(Math.abs(measuredAnchor.offset - releasedAnchor.offset)).toBeLessThan(2);
  const measuredScrollTop = await viewport.evaluate((node) => node.scrollTop);
  await page.getByTestId("append-turn").evaluate(
    (button: HTMLButtonElement) => button.click(),
  );
  await page.waitForTimeout(100);
  expect(Math.abs(
    await viewport.evaluate((node) => node.scrollTop) - measuredScrollTop,
  )).toBeLessThan(2);
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "true",
  );

  await page.evaluate(() => {
    document.dispatchEvent(new ClipboardEvent("copy", {
      bubbles: true,
      cancelable: true,
    }));
  });
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "false",
  );
  expect(await page.locator(".turn").count()).toBeLessThan(40);
});

for (const engine of ["codex", "claude"] as const) {
  for (const theme of ["light", "dark"] as const) {
    test(`Markdown disclosures stay compact and keep their header anchored (${engine}, ${theme})`, async ({ page }) => {
      await page.goto("/tests/history-browser.html?markdown-disclosure=1");
      await page.evaluate(({ engine, theme }) => {
        document.documentElement.dataset.engine = engine;
        document.documentElement.dataset.theme = theme;
      }, { engine, theme });
      const disclosure = page.getByTestId("markdown-disclosure").locator(".prose > details");
      const summary = disclosure.locator(":scope > summary");
      await expect(summary).toHaveText("文件清单：21 个源文件，无删除");
      await expect(summary).toHaveCSS("list-style-type", "none");
      await expect(summary).toHaveCSS("font-weight", "500");
      await expect(summary).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
      await expect(summary).toHaveCSS("border-top-width", "0px");
      await expect(disclosure).toHaveCSS("border-top-width", "0px");
      await expect(disclosure).toHaveCSS("border-left-width", "0px");
      const closed = await summary.boundingBox();
      const container = await disclosure.boundingBox();
      if (!closed || !container) throw new Error("disclosure geometry unavailable");
      const coarse = await page.evaluate(() => matchMedia("(pointer: coarse)").matches);
      expect(closed.height).toBeGreaterThanOrEqual(coarse ? 44 : 38);
      expect(closed.x).toBeGreaterThanOrEqual(container.x - 1);
      expect(closed.x + closed.width).toBeLessThanOrEqual(container.x + container.width + 1);
      if ((page.viewportSize()?.width ?? 0) >= 600) {
        expect(closed.width).toBeLessThan(container.width - 80);
      }
      const closedChevron = await summary.evaluate((node) => getComputedStyle(node, "::before").transform);
      await summary.click();
      await expect(disclosure).toHaveAttribute("open", "");
      await expect(summary).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
      await expect(summary).toHaveCSS("border-top-width", "0px");
      await expect.poll(() => summary.evaluate((node) => getComputedStyle(node, "::before").transform))
        .not.toBe(closedChevron);
      const expanded = await summary.boundingBox();
      if (!expanded) throw new Error("expanded header geometry unavailable");
      expect(expanded.x).toBeCloseTo(closed.x, 1);
      expect(expanded.y).toBeCloseTo(closed.y, 1);
      expect(expanded.width).toBeCloseTo(closed.width, 1);
      await expect(disclosure.getByText("README.md", { exact: true })).toBeVisible();
    });
  }
}

test("Markdown disclosures align a long file list with the quiet text header", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/tests/history-browser.html?markdown-disclosure=1&file-list=1");
  const disclosure = page.getByTestId("markdown-disclosure").locator(".prose > details");
  const summary = disclosure.locator(":scope > summary");
  await summary.click();
  await expect(disclosure.locator(":scope > ul > li")).toHaveCount(21);
  const geometry = await disclosure.evaluate((node) => {
    const header = node.querySelector("summary")!;
    const body = node.querySelector(":scope > p")!;
    const list = node.querySelector(":scope > ul")!;
    const headerBox = header.getBoundingClientRect();
    const bodyBox = body.getBoundingClientRect();
    return {
      titleLeft: headerBox.left + parseFloat(getComputedStyle(header).paddingLeft),
      bodyLeft: bodyBox.left,
      gap: bodyBox.top - headerBox.bottom,
      listIndent: parseFloat(getComputedStyle(list).paddingInlineStart),
      contentRight: Math.max(...Array.from(list.querySelectorAll("code")).flatMap(
        (code) => Array.from(code.getClientRects(), (rect) => rect.right),
      )),
      containerRight: node.getBoundingClientRect().right,
      scroll: document.documentElement.scrollWidth,
      viewport: document.documentElement.clientWidth,
    };
  });
  expect(geometry.bodyLeft).toBeCloseTo(geometry.titleLeft, 1);
  expect(geometry.gap).toBeGreaterThanOrEqual(0);
  expect(geometry.gap).toBeLessThanOrEqual(8);
  expect(geometry.listIndent).toBeLessThanOrEqual(20);
  expect(geometry.contentRight).toBeLessThanOrEqual(geometry.containerRight + 1);
  expect(geometry.scroll).toBeLessThanOrEqual(geometry.viewport);
  await summary.click();
  await expect(disclosure.locator(":scope > ul")).toBeHidden();
});

test("Markdown disclosures wrap long titles on a narrow screen without clipping", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto("/tests/history-browser.html?markdown-disclosure=1&long-title=1");
  const disclosure = page.getByTestId("markdown-disclosure").locator(".prose > details");
  const summary = disclosure.locator(":scope > summary");
  await expect(summary).toContainText("long_source_filename_");
  const geometry = await summary.evaluate((node) => {
    const box = node.getBoundingClientRect();
    return { left: box.left, right: box.right, height: box.height,
      scroll: node.scrollWidth, client: node.clientWidth, viewport: document.documentElement.clientWidth };
  });
  expect(geometry.height).toBeGreaterThan(60);
  expect(geometry.scroll).toBeLessThanOrEqual(geometry.client + 1);
  expect(geometry.left).toBeGreaterThanOrEqual(0);
  expect(geometry.right).toBeLessThanOrEqual(geometry.viewport);
  await summary.click();
  await expect(disclosure.getByText("README.md", { exact: true })).toBeVisible();
});

test("Markdown disclosures retain native keyboard controls and reduced-motion support", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/tests/history-browser.html?markdown-disclosure=1");
  const disclosure = page.getByTestId("markdown-disclosure").locator(".prose > details");
  const summary = disclosure.locator(":scope > summary");
  await summary.press("Enter");
  await expect(disclosure).toHaveAttribute("open", "");
  await expect(summary).toBeFocused();
  await expect(summary).toHaveCSS("outline-style", "solid");
  await expect(summary).toHaveCSS("transition-duration", "0s");
  expect(await summary.evaluate((node) => getComputedStyle(node, "::before").transitionDuration)).toBe("0s");
  await summary.press("Space");
  await expect(disclosure).not.toHaveAttribute("open", "");
});

test("Markdown disclosures render safely and stay open across streaming updates", async ({ page }) => {
  const unexpectedRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("example.com")) unexpectedRequests.push(request.url());
  });
  await page.goto("/tests/history-browser.html?markdown-disclosure=1");
  const section = page.getByTestId("markdown-disclosure");
  const disclosure = section.locator(".prose > details");
  await expect(disclosure).toBeVisible();
  await expect(section.getByText("README.md", { exact: true })).toBeHidden();
  await disclosure.locator(":scope > summary").click();
  await expect(section.getByText("README.md", { exact: true })).toBeVisible();
  await expect(section.getByText("Inner body", { exact: true })).toBeVisible();
  await section.getByRole("button", { name: "在 Remote 中打开 /tmp/source.py" }).click();
  await expect(page.getByTestId("disclosure-opened-path")).toHaveText("/tmp/source.py");
  await page.getByRole("button", { name: "Finish stream", exact: true }).click();
  await expect(section.getByText("Last streamed item", { exact: true })).toBeVisible();
  await expect(section.getByText("After", { exact: true })).toBeVisible();
  await disclosure.locator(":scope > summary").click();
  await expect(section.getByText("Last streamed item", { exact: true })).toBeHidden();
  await expect(section.getByText("After", { exact: true })).toBeVisible();
  const unsafe = page.getByTestId("inert-disclosure-html");
  await unsafe.locator("summary").click();
  await expect(unsafe.locator("img, script")).toHaveCount(0);
  await expect(unsafe).toContainText('<script>alert(2)</script>');
  expect(unexpectedRequests).toEqual([]);
});

test("desktop native selection auto-scroll does not snap back at the lower edge", async ({
  page,
}, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name), "desktop native selection");
  await page.goto("/tests/history-browser.html?large=120");
  const viewport = page.locator(".thread");
  await wheelUntilTurn(page, "m42", -600, testInfo.project.name);
  await waitForScrollIdle(page);
  const startTurnId = (await readingAnchor(page)).id;
  const text = page.locator(`[data-turn-id="${startTurnId}"] p`).first();
  const point = await textSelectionPoint(text);
  const box = await viewport.boundingBox();
  if (!box) throw new Error("selection fixture has no geometry");
  const before = await viewport.evaluate((node) => node.scrollTop);
  await page.mouse.move(point.x, point.y);
  await page.mouse.down();
  await page.mouse.move(point.x + 100, point.y, { steps: 8 });
  await expect(viewport).toHaveAttribute("data-text-selection-dragging", "true");
  const samples: number[] = [];
  for (let i = 0; i < 40; i += 1) {
    await page.mouse.move(box.x + box.width / 2 + (i % 2), box.y + box.height + 12);
    await page.waitForTimeout(50);
    samples.push(await viewport.evaluate((node) => node.scrollTop));
  }
  const dragged = await viewport.evaluate((node) => node.scrollTop);
  const selected = await nativeSelectionSnapshot(page);
  await page.mouse.up();
  await waitForScrollIdle(page);
  const released = await viewport.evaluate((node) => node.scrollTop);
  expect(dragged - before).toBeGreaterThan(300);
  expect(Math.max(...samples) - dragged, JSON.stringify({ before, samples, released }))
    .toBeLessThan(3);
  expect(released).toBeGreaterThanOrEqual(dragged - 2);
  expect(selected.anchorTurnId).toBe(startTurnId);
  expect(selected.anchorConnected).toBe(true);
});

for (const sameTurn of [false, true]) {
test(`extending a released native selection yields the retained viewport anchor (${sameTurn ? "same turn" : "across turns"})`, async ({ page }) => {
  await page.goto(sameTurn
    ? "/tests/history-browser.html?large=6&paragraphs=60"
    : "/tests/history-browser.html?large=120");
  const viewport = page.locator(".thread");
  // Selection handles do not produce a new mouse pointerdown on the thread.
  // Exercise the browser's Range/selectionchange path directly, not a wheel.
  await waitForScrollIdle(page);
  // Initial virtual measurements can reassert the mounted tail after goto.
  // Establish room for the native scroll before testing selection retention;
  // a clamped write at the bottom cannot exercise the behavior under test.
  for (let attempt = 0; attempt < 6; attempt += 1) {
    await viewport.evaluate((node) => {
      node.scrollTop = Math.max(0, node.scrollHeight - node.clientHeight - 1800);
    });
    await waitForScrollIdle(page);
    if (await viewport.evaluate((node) => node.scrollHeight - node.clientHeight - node.scrollTop >= 1200)) break;
  }
  expect(await viewport.evaluate((node) => node.scrollHeight - node.clientHeight - node.scrollTop))
    .toBeGreaterThanOrEqual(1200);
  const start = (await readingAnchor(page)).id;
  const text = page.locator(`[data-turn-id="${start}"] p`).first();
  await text.evaluate((node) => {
    const range = document.createRange();
    range.setStart(node.firstChild!, 0);
    range.setEnd(node.firstChild!, 6);
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
  });
  await expect(viewport).toHaveAttribute("data-text-selection-retained", "true");
  await waitForScrollIdle(page);
  const before = await viewport.evaluate((node) => node.scrollTop);
  await page.evaluate(({ id, sameTurn }) => {
    const thread = document.querySelector<HTMLElement>(".thread")!;
    const turn = document.querySelector(`[data-turn-id="${id}"]`)!;
    const next = sameTurn ? turn.querySelectorAll("p")[25]
      : turn.nextElementSibling!.nextElementSibling!.querySelector("p")!;
    window.getSelection()!.extend(next.firstChild!, 12);
    // Mobile handles scroll natively after selectionchange, without touchmove
    // reaching React. Keep the test independent of OS handle automation.
    document.dispatchEvent(new Event("selectionchange"));
    thread.scrollTop += 500;
  }, { id: start, sameTurn });
  await waitForScrollIdle(page);
  const after = await viewport.evaluate((node) => node.scrollTop);
  expect(after - before).toBeGreaterThan(400);
  expect((await nativeSelectionSnapshot(page)).anchorTurnId).toBe(start);
  if (sameTurn) expect((await nativeSelectionSnapshot(page)).focusTurnId).toBe(start);
  await page.waitForTimeout(400);
  expect(Math.abs(await viewport.evaluate((node) => node.scrollTop) - after)).toBeLessThan(2);
});
}

test("desktop native selection retains ownership through a transient empty edge range", async ({ page }, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name), "desktop native selection");
  await page.goto("/tests/history-browser.html?large=120");
  const viewport = page.locator(".thread");
  await wheelUntilTurn(page, "m42", -600, testInfo.project.name);
  await waitForScrollIdle(page);
  const turn = (await readingAnchor(page)).id;
  const point = await textSelectionPoint(page.locator(`[data-turn-id="${turn}"] p`).first());
  await page.mouse.move(point.x, point.y);
  await page.mouse.down();
  await page.mouse.move(point.x + 100, point.y, { steps: 8 });
  await expect(viewport).toHaveAttribute("data-text-selection-dragging", "true");
  const range = await page.evaluateHandle(() => window.getSelection()!.getRangeAt(0).cloneRange());
  await page.evaluate(() => {
    window.getSelection()!.removeAllRanges();
    document.dispatchEvent(new Event("selectionchange"));
  });
  await expect(viewport).toHaveAttribute("data-text-selection-dragging", "true");
  await expect(viewport).toHaveAttribute("data-text-selection-retained", "true");
  const before = await viewport.evaluate((node) => node.scrollTop);
  await viewport.evaluate((node) => { node.scrollTop += 420; });
  await waitForScrollIdle(page);
  await expect(viewport).toHaveAttribute("data-text-selection-dragging", "true");
  await expect(viewport).toHaveAttribute("data-text-selection-retained", "true");
  await page.evaluate((saved) => {
    // WebKit may create a collapsed caret during the scroll above. addRange
    // does not replace an existing range in a single-range browser, so restore
    // this synthetic transient selection explicitly (also on a plain page).
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(saved);
    document.dispatchEvent(new Event("selectionchange"));
  }, range);
  expect((await nativeSelectionSnapshot(page)).anchorTurnId).toBe(turn);
  expect((await nativeSelectionSnapshot(page)).text).not.toBe("");
  await page.mouse.up();
  await waitForScrollIdle(page);
  expect(await viewport.evaluate((node) => node.scrollTop)).toBeGreaterThan(before + 400);
  expect((await nativeSelectionSnapshot(page)).anchorTurnId).toBe(turn);
  expect((await nativeSelectionSnapshot(page)).text).not.toBe("");
  await expect(viewport).toHaveAttribute("data-text-selection-dragging", "false");
  await range.dispose();
});

test("desktop wheel scrolling remains available after text selection", async ({
  page,
}, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name),
    "the configured WebKit project is a touch phone; this is a desktop mouse path");
  await page.goto("/tests/history-browser.html?large=120");
  const viewport = page.locator(".thread");
  await wheelUntilTurn(page, "m42", -600, testInfo.project.name);
  await waitForScrollIdle(page);
  const startTurnId = (await readingAnchor(page)).id;
  const text = page.locator(`[data-turn-id="${startTurnId}"] p`).first();
  // A reading anchor may be clipped above the thread. In the CI trace the
  // drag hit the fixture toolbar instead of text, so no selection existed.
  await text.scrollIntoViewIfNeeded();
  await waitForScrollIdle(page);
  const point = await textSelectionPoint(text);
  expect(await text.evaluate((node, at) =>
    node.contains(document.elementFromPoint(at.x, at.y)), point)).toBe(true);

  await page.mouse.move(point.x, point.y);
  await page.mouse.down();
  await page.mouse.move(
    point.x + 100, point.y,
    { steps: 12 },
  );
  await expect(viewport).toHaveAttribute("data-text-selection-dragging", "true");
  await page.mouse.up();
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "true",
  );
  expect((await nativeSelectionSnapshot(page)).text.length).toBeGreaterThan(0);
  const beforeScrollTop = await viewport.evaluate((node) => node.scrollTop);

  await page.mouse.wheel(0, 640);
  await expect.poll(
    () => viewport.evaluate((node) => node.scrollTop),
  ).toBeGreaterThan(beforeScrollTop + 200);
  const afterScroll = await nativeSelectionSnapshot(page);
  expect(afterScroll.anchorConnected).toBe(true);
  expect(afterScroll.text.length).toBeGreaterThan(0);
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "true",
  );
});

test("a late cached-newer page cannot evict an active text selection", async ({
  page,
}, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name),
    "the configured WebKit project is a touch phone; this is a desktop mouse path");
  await page.goto(
    "/tests/history-browser.html?deep-browse=1&delay=3000",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await page.getByRole("button", { name: "加载更新的历史" })
    .dispatchEvent("click");
  await expect(page.getByTestId("newer-load-count")).toHaveText("1");

  await wheelUntilTurn(page, "m5", -400, testInfo.project.name);
  const startText = page.locator('[data-turn-id="m5"] p').first();
  await expect(startText).toBeInViewport();
  const start = await textSelectionPoint(startText);
  const textBox = await startText.boundingBox();
  if (!textBox) throw new Error("selection page fixture has no geometry");
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await page.mouse.move(
    Math.min(textBox.x + textBox.width - 2, start.x + 140),
    start.y,
    { steps: 8 },
  );
  await expect(viewport).toHaveAttribute(
    "data-text-selection-dragging", "true",
  );

  await expect(page.getByTestId("newest-turn-id")).toHaveText("m28");
  await expect(page.locator('[data-turn-id="m5"]')).toBeAttached();
  expect((await nativeSelectionSnapshot(page)).anchorTurnId).toBe("m5");
  await page.mouse.up();
  await page.evaluate(() => {
    document.dispatchEvent(new ClipboardEvent("copy", { bubbles: true }));
  });
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "false",
  );
});

test("switching sessions clears retained desktop text selection", async ({
  page,
}, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name),
    "the configured WebKit project is a touch phone; this is a desktop mouse path");
  await page.goto("/tests/history-browser.html?large=80");
  const viewport = page.locator(".thread");
  await wheelUntilTurn(page, "m42", -600, testInfo.project.name);
  await waitForScrollIdle(page);
  const startText = page.locator('[data-turn-id="m42"] p').first();
  const start = await textSelectionPoint(startText);
  const textBox = await startText.boundingBox();
  if (!textBox) throw new Error("selection switch fixture has no geometry");
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await page.mouse.move(
    Math.min(textBox.x + textBox.width - 2, start.x + 120),
    start.y,
    { steps: 8 },
  );
  await page.mouse.up();
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "true",
  );

  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "false",
  );
  expect((await nativeSelectionSnapshot(page)).text).toBe("");
  expect(await page.locator(".turn").count()).toBeLessThan(40);
});

test("nested process disclosures survive virtual row unmounts", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?timeline=1&engine=claude");
  await scrollThreadToEdge(page, "start", testInfo.project.name);
  const timeline = page.locator('[data-turn-id="timeline"]');
  await expect(timeline).toBeVisible();
  await timeline.locator(".turn-process-head").click();
  const activity = timeline.locator("details.process-activity");
  const reasoning = timeline.locator("details.process-reasoning");
  await activity.locator(":scope > summary").click();
  await reasoning.locator(":scope > summary").click();
  await expect(activity).toHaveAttribute("open", "");
  await expect(reasoning).toHaveAttribute("open", "");

  await scrollThreadToEdge(page, "end", testInfo.project.name);
  await expect(timeline).toHaveCount(0);
  await scrollThreadToEdge(page, "start", testInfo.project.name);
  await expect(timeline).toBeVisible();
  await expect(timeline.locator("details.process-activity"))
    .toHaveAttribute("open", "");
  await expect(timeline.locator("details.process-reasoning"))
    .toHaveAttribute("open", "");
});

test("one stationary press opens a process timeline while a newer turn grows", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?interactive-timeline=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });

  const header = page.locator(
    '[data-turn-id="timeline"] .turn-process-head',
  );
  await expect(header).toBeVisible();
  await expect(header).toHaveAttribute("aria-expanded", "false");
  const box = await header.boundingBox();
  if (!box) throw new Error("process header has no bounds");
  const point = {
    x: box.x + box.width / 2,
    y: box.y + box.height / 2,
  };

  await page.mouse.move(point.x, point.y);
  await page.mouse.down();
  for (let index = 0; index < 4; index += 1) {
    await page.getByTestId("grow-stream").evaluate(
      (button: HTMLButtonElement) => button.click(),
    );
    await page.waitForTimeout(35);
  }
  await page.mouse.up();

  await expect(header).toHaveAttribute("aria-expanded", "true");
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "false",
  );
});

test("one stationary press opens nested thinking while a newer turn grows", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?interactive-timeline=1&engine=claude",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  const timeline = page.locator('[data-turn-id="timeline"]');
  await timeline.locator(".turn-process-head").click();
  await waitForScrollIdle(page);
  const summary = timeline.locator(".process-reasoning > summary");
  const box = await summary.boundingBox();
  if (!box) throw new Error("nested reasoning summary has no bounds");

  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  for (let index = 0; index < 4; index += 1) {
    await page.getByTestId("grow-stream").evaluate(
      (button: HTMLButtonElement) => button.click(),
    );
    await page.waitForTimeout(35);
  }
  await page.mouse.up();

  await expect(timeline.locator("details.process-reasoning"))
    .toHaveAttribute("open", "");
});

test("dragging a process header outside cannot leave output following locked", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?interactive-timeline=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  const header = page.locator(
    '[data-turn-id="timeline"] .turn-process-head',
  );
  const box = await header.boundingBox();
  if (!box) throw new Error("process header has no bounds");

  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width + 80, box.y + box.height + 80);
  await page.mouse.up();
  await expect(header).toHaveAttribute("aria-expanded", "false");

  await page.getByTestId("grow-stream").click();
  await expect.poll(() => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);
});

for (const kind of ["thinking", "activity"] as const) {
test(`dragging nested process ${kind} outside cannot leave output following locked`, async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?interactive-timeline=1&engine=claude",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  const timeline = page.locator('[data-turn-id="timeline"]');
  await timeline.locator(".turn-process-head").click();
  await waitForScrollIdle(page);
  const disclosure = timeline.locator(kind === "thinking"
    ? "details.process-reasoning" : "details.process-activity");
  const summary = disclosure.locator(":scope > summary");
  const box = await summary.boundingBox();
  const labelBox = await summary.locator(kind === "thinking"
    ? ":scope > span" : ".process-item-title").boundingBox();
  if (!box || !labelBox) throw new Error("nested process summary has no bounds");

  // Start on the label, not empty summary padding: WebKit can otherwise
  // alternate between a control drag and an accidental native text selection.
  await page.mouse.move(
    labelBox.x + labelBox.width / 2, labelBox.y + labelBox.height / 2,
  );
  await page.mouse.down();
  await page.mouse.move(box.x + box.width + 80, box.y + box.height + 80);
  await page.mouse.up();
  await expect(disclosure).not.toHaveAttribute("open", "");
  await expect(viewport).toHaveAttribute("data-text-selection-retained", "false");

  await page.getByTestId("grow-stream").click();
  await expect.poll(() => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);
});
}

test("nested process disclosures preserve keyboard activation and body selection", async ({ page }) => {
  await page.goto("/tests/history-browser.html?interactive-timeline=1&engine=claude");
  const timeline = page.locator('[data-turn-id="timeline"]');
  await timeline.locator(".turn-process-head").click();
  const disclosure = timeline.locator("details.process-reasoning");
  const summary = disclosure.locator(":scope > summary");
  await summary.press("Enter");
  await expect(disclosure).toHaveAttribute("open", "");
  await expect(summary).toBeFocused();
  await summary.press("Space");
  await expect(disclosure).not.toHaveAttribute("open", "");
  await summary.click();
  await expect(disclosure).toHaveAttribute("open", "");

  const body = disclosure.locator(".process-reasoning-body .prose p").first();
  await body.scrollIntoViewIfNeeded();
  await waitForScrollIdle(page);
  const start = await textSelectionPoint(body);
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await page.mouse.move(start.x + 60, start.y, { steps: 8 });
  await page.mouse.up();
  await expect(page.locator(".thread"))
    .toHaveAttribute("data-text-selection-retained", "true");
  expect((await nativeSelectionSnapshot(page)).text).not.toBe("");
});

test("iOS pointercancel releases process interactions and output following", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit pointer cancellation");
  await page.goto(
    "/tests/history-browser.html?interactive-timeline=1&engine=claude",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  const timeline = page.locator('[data-turn-id="timeline"]');
  const header = timeline.locator(".turn-process-head");

  await dispatchCancelledTouchTap(header, 184);
  await expect(header).toHaveAttribute("aria-expanded", "false");
  await page.getByTestId("grow-stream").click();
  await expect.poll(() => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);

  await header.evaluate((node) => {
    const target = node as HTMLElement;
    target.dispatchEvent(new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      detail: 1,
    }));
  });
  await expect(header).toHaveAttribute("aria-expanded", "true");
  const reasoning = timeline.locator("details.process-reasoning");
  await dispatchCancelledTouchTap(
    reasoning.locator(":scope > summary"), 185,
  );
  await expect(reasoning).not.toHaveAttribute("open", "");
  await page.getByTestId("grow-stream").click();
  await expect.poll(() => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);
});

test("live append follows at the bottom but not while reading history", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?large=40");
  await expect(page.locator('[data-turn-id="m40"]')).toBeVisible();
  await page.getByTestId("append-turn").click();
  await expect(page.locator('[data-turn-id="live-41"]')).toBeVisible();

  // Use the browser's native scroll pipeline here so the virtualizer and
  // React receive the same wheel/scroll ordering as a real user gesture.
  await wheelUntilTurn(page, "m1", -2_000, testInfo.project.name);
  await expect(page.locator(".scroll-bottom-btn")).toBeVisible();
  await page.waitForTimeout(250);
  await expect(page.locator('[data-turn-id="live-41"]')).toHaveCount(0);
  const before = await readingAnchor(page);
  await page.getByTestId("append-turn").click();
  await page.waitForTimeout(100);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
  await expect(page.locator('[data-turn-id="live-42"]')).toHaveCount(0);
  expect(await page.locator(".turn").count()).toBeLessThan(40);
  await assertCodexBurstNeverPaintsAboveTail(page, testInfo.project.name);
});

test("scrolling a live-dirty history window to its latest edge restores the active tail", async ({
  page,
}, testInfo) => {
  await page.goto(
    "/tests/history-browser.html?deep-browse=1&dirty-live-browse=1&engine=claude",
  );
  await expect(page.locator('[data-turn-id="m20"]')).toBeVisible();
  await page.getByTestId("append-turn").click();
  await expect(page.locator('[data-turn-id="live-streaming"]')).toHaveCount(0);

  for (let pageIndex = 0; pageIndex < 5; pageIndex += 1) {
    await scrollThreadToEdge(page, "end", testInfo.project.name);
    if (await page.locator('[data-turn-id="live-streaming"] .turn-working')
      .count()) break;
    await page.waitForTimeout(80);
  }

  await expect(page.locator('[data-turn-id="live-streaming"] .turn-working'))
    .toBeVisible();
  await expect(page.getByTestId("newest-turn-id")).toHaveText("live-streaming");
  await expect(page.locator(".scroll-bottom-btn")).toHaveCount(0);
});

test("returning to a background-grown live turn settles at its current tail", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?interactive-timeline=1&engine=claude",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await expect(page.locator('[data-turn-id="streaming"] .turn-working'))
    .toBeVisible();

  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();
  // Visibility can precede ChatView's next-frame session-entry tail settle on
  // a loaded WebKit worker. Measure the background update only after that
  // intentional scope transition has finished.
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);
  await page.getByTestId("grow-background-stream").click();
  await page.waitForTimeout(200);
  const unchanged = await readingAnchor(page);
  expect(unchanged.id).toBe(before.id);
  expect(Math.abs(unchanged.offset - before.offset)).toBeLessThan(2);

  await page.getByTestId("switch-session").click();
  const working = page.locator(
    '[data-turn-id="streaming"] .turn-working',
  );
  await expect(working).toBeVisible();
  await expect.poll(async () => working.evaluate((node) => {
    const thread = document.querySelector<HTMLElement>(".thread");
    if (!thread) return false;
    return node.getBoundingClientRect().bottom
      <= thread.getBoundingClientRect().bottom + 1;
  })).toBe(true);
  for (let index = 0; index < 4; index += 1) {
    await page.getByTestId("grow-stream").click();
    await expect.poll(async () => working.evaluate((node) => {
      const thread = document.querySelector<HTMLElement>(".thread");
      if (!thread) return false;
      return node.getBoundingClientRect().bottom
        <= thread.getBoundingClientRect().bottom + 1;
    })).toBe(true);
  }
});

async function assertCodexBurstNeverPaintsAboveTail(
  page: import("@playwright/test").Page,
  projectName: string,
): Promise<void> {
  await page.goto("/tests/history-browser.html?codex-live-burst=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await waitForScrollIdle(page);

  const resultPromise = page.evaluate(async () => {
    const thread = document.querySelector<HTMLElement>(".thread");
    const start = document.querySelector<HTMLButtonElement>(
      '[data-testid="start-codex-burst"]',
    );
    if (!thread || !start) throw new Error("Codex burst fixture is incomplete");
    const distances: number[] = [];
    const violations: Array<{
      frame: number;
      distance: number;
      scrollTop: number;
      scrollHeight: number;
      burst: string | undefined;
    }> = [];
    const reverseJumps: Array<{
      frame: number;
      previousScrollTop: number;
      scrollTop: number;
      previousScrollHeight: number;
      scrollHeight: number;
    }> = [];
    let frames = 0;
    let doneFrames = 0;
    let previousScrollTop = thread.scrollTop;
    let previousScrollHeight = thread.scrollHeight;
    let previousBurst = document.documentElement.dataset.codexBurst;
    start.click();
    await new Promise<void>((resolve) => {
      const sample = () => {
        frames += 1;
        const distance = Math.max(
          0,
          thread.scrollHeight - thread.scrollTop - thread.clientHeight,
        );
        distances.push(distance);
        if (distance > 2) {
          violations.push({
            frame: frames,
            distance,
            scrollTop: thread.scrollTop,
            scrollHeight: thread.scrollHeight,
            burst: document.documentElement.dataset.codexBurst,
          });
        }
        const burst = document.documentElement.dataset.codexBurst;
        if (burst === "running" && previousBurst === "running"
            && thread.scrollTop < previousScrollTop - 2) {
          reverseJumps.push({
            frame: frames,
            previousScrollTop,
            scrollTop: thread.scrollTop,
            previousScrollHeight,
            scrollHeight: thread.scrollHeight,
          });
        }
        previousScrollTop = thread.scrollTop;
        previousScrollHeight = thread.scrollHeight;
        previousBurst = burst;
        if (burst === "done") {
          doneFrames += 1;
          if (doneFrames >= 6) {
            resolve();
            return;
          }
        }
        requestAnimationFrame(() => window.setTimeout(sample, 0));
      };
      requestAnimationFrame(() => window.setTimeout(sample, 0));
    });
    return {
      frames,
      worst: Math.max(0, ...distances),
      reverseJumps,
      violations,
    };
  });

  const result = await resultPromise;
  expect(result.frames).toBeGreaterThan(10);
  expect(result.violations, JSON.stringify(result.violations)).toHaveLength(0);
  expect(result.reverseJumps, JSON.stringify(result.reverseJumps))
    .toHaveLength(0);
  expect(result.worst).toBeLessThanOrEqual(2);

  // The pre-paint observer is active only while the newest turn is open. A
  // reader who has deliberately left the tail must retain the exact row while
  // the same long Codex tool burst continues in the background.
  await page.goto("/tests/history-browser.html?codex-live-burst=1");
  await wheelUntilTurn(page, "burst-history-1", -2_000, projectName);
  const before = await readingAnchor(page);
  await page.getByTestId("start-codex-burst").click();
  await expect.poll(() => page.evaluate(
    () => document.documentElement.dataset.codexBurst,
  )).toBe("done");
  await page.waitForTimeout(100);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
}

test("multi-line IME growth stays pinned during a Codex tool burst", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?codex-live-burst=1&composer-live=1",
  );
  const result = await page.evaluate(async () => {
    const thread = document.querySelector<HTMLElement>(".thread");
    const input = document.querySelector<HTMLTextAreaElement>(
      '[data-testid="live-composer-shell"] textarea',
    );
    const start = document.querySelector<HTMLButtonElement>(
      '[data-testid="start-codex-burst"]',
    );
    if (!thread || !input || !start) {
      throw new Error("live composer fixture is incomplete");
    }
    const frame = () => new Promise<void>((resolve) => {
      requestAnimationFrame(() => resolve());
    });
    thread.scrollTop = thread.scrollHeight;
    await frame();
    await frame();

    const distances: number[] = [];
    let typed = false;
    let terminalFrames = 0;
    const monitor = new Promise<void>((resolve) => {
      const sample = () => {
        distances.push(Math.max(
          0,
          thread.scrollHeight - thread.scrollTop - thread.clientHeight,
        ));
        if (typed && document.documentElement.dataset.codexBurst === "done") {
          terminalFrames += 1;
          if (terminalFrames >= 6) {
            resolve();
            return;
          }
        }
        requestAnimationFrame(sample);
      };
      requestAnimationFrame(sample);
    });

    const setValue = Object.getOwnPropertyDescriptor(
      HTMLTextAreaElement.prototype, "value",
    )?.set;
    if (!setValue) throw new Error("textarea value setter is unavailable");
    const values = Array.from(
      { length: 5 },
      (_, index) => Array.from(
        { length: index + 1 },
        (__, line) => `第 ${line + 1} 行拼音输入内容用于验证输入框稳定`,
      ).join("\n"),
    );
    const heights: number[] = [input.getBoundingClientRect().height];
    input.focus();
    input.dispatchEvent(new CompositionEvent("compositionstart", {
      bubbles: true,
      data: "",
    }));
    start.click();
    for (const value of values) {
      setValue.call(input, value);
      input.dispatchEvent(new CompositionEvent("compositionupdate", {
        bubbles: true,
        data: value,
      }));
      input.dispatchEvent(new InputEvent("input", {
        bubbles: true,
        data: value,
        inputType: "insertCompositionText",
      }));
      await frame();
      await frame();
      heights.push(input.getBoundingClientRect().height);
    }
    input.dispatchEvent(new CompositionEvent("compositionend", {
      bubbles: true,
      data: values.at(-1),
    }));
    typed = true;
    await monitor;
    return {
      heights,
      worstDistance: Math.max(0, ...distances),
    };
  });

  expect(result.heights.at(-1)).toBeGreaterThan(result.heights[0]);
  expect(result.heights.at(-1)).toBeLessThanOrEqual(133);
  expect(result.worstDistance).toBeLessThanOrEqual(2);
});

test("long paste stays out of the textarea and remains editable before send", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?codex-live-burst=1&composer-live=1&composer-paste=1",
  );
  const input = page.locator(
    '[data-testid="live-composer-shell"] .composer textarea',
  );
  const pasted = `Editable paste opening ${"content ".repeat(170)}`;
  await input.evaluate((node, text) => {
    const data = new DataTransfer();
    data.setData("text/plain", text);
    node.dispatchEvent(new ClipboardEvent("paste", {
      bubbles: true,
      cancelable: true,
      clipboardData: data,
    }));
  }, pasted);
  await expect(input).toHaveValue("");
  const card = page.locator(
    '[data-testid="live-composer-shell"] .paste-card',
  );
  await expect(card).toContainText("Editable paste opening");
  await expect(card).toContainText(`${pasted.length} 字符`);
  const box = await card.boundingBox();
  expect(box?.width ?? 999).toBeLessThanOrEqual(302);

  await card.locator(".paste-open").click();
  const editor = page.getByRole("dialog", { name: "编辑粘贴内容" })
    .getByRole("textbox");
  await editor.fill("edited paste body");
  await page.getByRole("button", { name: "保存" }).click();
  await expect(card).toContainText("edited paste body");
  await expect(input).toHaveValue("");

  await input.fill("visible follow-up");
  await page.locator(
    '[data-testid="live-composer-shell"] .sendbtn',
  ).click();
  await expect(page.getByTestId("composer-paste-output"))
    .toHaveText("edited paste body\n\nvisible follow-up");
  await expect(card).toHaveCount(0);
  await expect(input).toHaveValue("");
});

test("oversized edited paste stays in the draft instead of being cleared", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?codex-live-burst=1&composer-live=1&composer-paste=1",
  );
  const input = page.locator(
    '[data-testid="live-composer-shell"] .composer textarea',
  );
  const seed = `oversized ${"seed ".repeat(205)}`;
  await input.evaluate((node, text) => {
    const data = new DataTransfer();
    data.setData("text/plain", text);
    node.dispatchEvent(new ClipboardEvent("paste", {
      bubbles: true,
      cancelable: true,
      clipboardData: data,
    }));
  }, seed);
  const card = page.locator(
    '[data-testid="live-composer-shell"] .paste-card',
  );
  await card.locator(".paste-open").click();
  const pasteEditor = page.getByRole("dialog", { name: "编辑粘贴内容" });
  await pasteEditor.getByRole("textbox")
    .fill("x".repeat(2 * 1024 * 1024));
  await expect(pasteEditor.locator("small"))
    .toContainText("2097152 字符 · 1 行");
  await page.getByRole("button", { name: "保存" }).click();
  await expect(card.locator(".paste-card-preview")).toHaveText(/^x{180}$/);
  await input.fill("tail");
  await page.locator(
    '[data-testid="live-composer-shell"] .sendbtn',
  ).click();
  await expect(page.locator(
    '[data-testid="live-composer-shell"] .composer-notice',
  )).toContainText("消息内容超过上限");
  await expect(card).toHaveCount(1);
  await expect(input).toHaveValue("tail");
  await expect(page.getByTestId("composer-paste-output")).toHaveText("");
});

test("multi-line composer growth does not move a history reader", async ({
  page,
}, testInfo) => {
  await page.goto(
    "/tests/history-browser.html?codex-live-burst=1&composer-live=1",
  );
  await wheelUntilTurn(
    page, "burst-history-1", -2_000, testInfo.project.name,
  );
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);
  await page.locator(
    '[data-testid="live-composer-shell"] textarea',
  ).fill([
    "第一行输入",
    "第二行输入",
    "第三行输入",
    "第四行输入",
    "第五行输入",
  ].join("\n"));
  await page.waitForTimeout(120);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("composer action growth keeps the live tail visible without stealing history", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?large=40&composer-resize=1");
  const viewport = page.locator(".thread");
  await expect(page.locator('[data-turn-id="m40"]')).toBeVisible();
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await expect.poll(async () => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);

  await page.getByTestId("toggle-composer").click();
  await expect.poll(async () => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);
  const spark = page.locator('[data-turn-id="m40"] .turn-done-mark');
  await expect(spark).toBeVisible();
  expect(await spark.evaluate((node) => {
    const viewportNode = document.querySelector<HTMLElement>(".thread");
    if (!viewportNode) throw new Error("thread viewport is missing");
    return node.getBoundingClientRect().bottom
      <= viewportNode.getBoundingClientRect().bottom + 1;
  })).toBe(true);

  await wheelUntilTurn(page, "m1", -2_000, testInfo.project.name);
  const before = await readingAnchor(page);
  await page.getByTestId("toggle-composer").click();
  await page.waitForTimeout(200);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

for (const width of [320, 390]) {
  test(`Codex controls stay on one row in a ${width} px composer`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 720 });
    await page.goto("/tests/history-browser.html?quota-composer=1");

    const composer = page.getByTestId("quota-composer");
    const input = composer.getByRole("textbox", { name: "message" });
    await expect(input).toHaveAttribute(
      "placeholder", "输入 / 命令，$ Skill");
    await expect(composer.locator(".fast-chip")).toHaveText("快速");
    await expect(composer.locator(".fast-chip")).not.toContainText("⚡");
    await expect(composer.locator(".usage-meter")).toBeVisible();
    await expect(composer.locator(".hint-ring")).toBeVisible();
    const inputHeight = await input.evaluate((node) => ({
      client: node.clientHeight,
      scroll: node.scrollHeight,
    }));
    expect(inputHeight.scroll).toBeLessThanOrEqual(inputHeight.client + 1);

    const layout = await composer.evaluate((node) => {
      const footer = node.getBoundingClientRect();
      const controls = [
        node.querySelector<HTMLElement>(".hint-mode"),
        ...node.querySelectorAll<HTMLElement>(".hint-right > .hint-ctl"),
        node.querySelector<HTMLElement>(".usage-meter"),
        node.querySelector<HTMLElement>(".hint-ring"),
      ].filter((control): control is HTMLElement => control !== null)
        .map((control) => {
          const rect = control.getBoundingClientRect();
          return {
            left: rect.left,
            right: rect.right,
            width: rect.width,
            centerY: rect.top + rect.height / 2,
          };
        });
      return {
        clientWidth: node.clientWidth,
        scrollWidth: node.scrollWidth,
        footerLeft: footer.left,
        footerRight: footer.right,
        controls,
      };
    });
    expect(layout.controls).toHaveLength(6);
    expect(layout.scrollWidth).toBeLessThanOrEqual(layout.clientWidth);
    expect(Math.max(...layout.controls.map((control) => control.centerY))
      - Math.min(...layout.controls.map((control) => control.centerY)))
      .toBeLessThanOrEqual(2);
    const minimumWidths = [48, 48, 28, 20, 44, 28];
    for (const [index, control] of layout.controls.entries()) {
      expect(control.width).toBeGreaterThanOrEqual(minimumWidths[index]);
    }
    for (const [index, control] of layout.controls.entries()) {
      expect(control.left).toBeGreaterThanOrEqual(layout.footerLeft);
      expect(control.right).toBeLessThanOrEqual(layout.footerRight);
      if (index > 0) {
        expect(control.left - layout.controls[index - 1].right)
          .toBeGreaterThanOrEqual(10);
      }
    }
  });
}

test("queued messages expand to full editable prompts", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 720 });
  await page.goto("/tests/history-browser.html?queued-query-editor=1");

  const fixture = page.getByTestId("queued-query-fixture");
  const preview = fixture.locator(".qt");
  await expect(preview).toBeVisible();
  await expect(fixture).not.toContainText("QUEUED-INSTRUCTION-END");
  expect(await preview.evaluate((node) =>
    node.scrollWidth > node.clientWidth)).toBe(true);

  await fixture.getByRole("button", { name: "查看排队消息" }).click();
  const dialog = page.getByRole("dialog", { name: "排队消息详情" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByTestId("queued-full-prompt"))
    .toContainText("QUEUED-INSTRUCTION-END");
  await expect(dialog).toContainText("编辑文字不会移除附件");

  await dialog.getByRole("button", { name: "编辑", exact: true }).click();
  const editor = dialog.getByRole("textbox", { name: "编辑排队消息" });
  await editor.fill("Updated queued instruction\nwith the complete context.");
  await dialog.getByRole("button", { name: "保存修改" }).click();
  await expect(dialog.getByTestId("queued-full-prompt"))
    .toHaveText("Updated queued instruction\nwith the complete context.");

  await dialog.getByRole(
    "button", { name: "关闭排队消息详情" },
  ).click();
  await expect(dialog).not.toBeVisible();
  await expect(preview).toContainText("Updated queued instruction");
  await fixture.getByRole("button", { name: "查看排队消息" }).click();
  await expect(page.getByTestId("queued-full-prompt"))
    .toContainText("with the complete context.");
  expect(await page.evaluate(() => (
    document.documentElement.scrollWidth <= document.documentElement.clientWidth
  ))).toBe(true);
});

test("migration picker cannot confirm a stale directory", async ({ page }) => {
  await page.goto("/tests/history-browser.html?migration-picker=1");
  await page.getByTestId("open-migration-picker").click();

  const dialog = page.getByRole("dialog", { name: "迁移 Codex 会话" });
  const confirm = dialog.getByRole(
    "button", { name: "迁移到此目录" },
  );
  await expect(dialog.locator(".dp-crumbs")).toHaveText("/repo/current");
  await expect(dialog).toContainText("正在读取目录");
  await expect(dialog).not.toContainText("stale-child");
  await expect(confirm).toBeDisabled();
  await expect(page.getByTestId("migration-picker-request"))
    .toHaveText("/repo/current");
  await expect(page.getByTestId("migration-picker-confirmed")).toBeEmpty();

  await page.getByTestId("resolve-migration-picker")
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect(dialog).not.toContainText("正在读取目录");
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(page.getByTestId("migration-picker-confirmed"))
    .toHaveText("/repo/current");
});

test("migration picker waits for its null-path response", async ({ page }) => {
  await page.goto("/tests/history-browser.html?migration-picker-null=1");
  await page.getByTestId("open-migration-picker").click();

  const dialog = page.getByRole("dialog", { name: "迁移 Codex 会话" });
  const confirm = dialog.getByRole(
    "button", { name: "迁移到此目录" },
  );
  await expect(dialog.locator(".dp-crumbs")).toHaveText("…");
  await expect(dialog).toContainText("正在读取目录");
  await expect(dialog).not.toContainText("stale-child");
  await expect(confirm).toBeDisabled();
  await expect(page.getByTestId("migration-picker-request"))
    .toHaveText("<home>");

  await page.getByTestId("resolve-migration-picker")
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect(dialog.locator(".dp-crumbs")).toHaveText("/home/fixture");
  await expect(dialog).not.toContainText("正在读取目录");
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(page.getByTestId("migration-picker-confirmed"))
    .toHaveText("/home/fixture");
});

test("migration picker follows an external session move", async ({ page }) => {
  await page.goto("/tests/history-browser.html?migration-picker=1");
  await page.getByTestId("open-migration-picker").click();

  const dialog = page.getByRole("dialog", { name: "迁移 Codex 会话" });
  const confirm = dialog.getByRole(
    "button", { name: "迁移到此目录" },
  );
  await expect(page.getByTestId("migration-picker-request"))
    .toHaveText("/repo/current");
  await page.getByTestId("resolve-migration-picker")
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect(confirm).toBeEnabled();

  await page.getByTestId("externally-migrate-picker")
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect(dialog.locator(".dp-crumbs")).toHaveText("/repo/external");
  await expect(dialog).toContainText("正在读取目录");
  await expect(confirm).toBeDisabled();
  await expect(page.getByTestId("migration-picker-request"))
    .toHaveText("/repo/external");
  await expect(page.getByTestId("migration-picker-confirmed")).toBeEmpty();

  await page.getByTestId("resolve-migration-picker")
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect(dialog).not.toContainText("正在读取目录");
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(page.getByTestId("migration-picker-confirmed"))
    .toHaveText("/repo/external");
});

async function chooseDangerousNewChatControls(
  page: import("@playwright/test").Page,
): Promise<void> {
  await page.locator(".newchat-access").click();
  const dialog = page.getByRole("dialog", {
    name: "权限与执行环境",
  });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: /On Request/ }).click();
  await dialog.getByRole("button", { name: /Full Access/ }).click();
  await dialog.getByRole("button", { name: "Live", exact: true }).click();
  await page.locator(".scrim.show").click({ position: { x: 8, y: 8 } });
  await expect(dialog).not.toHaveClass(/(?:^|\s)show(?:\s|$)/);
  await expect(page.locator(".newchat-access")).toContainText("Full Access");
}

let newChatSubmissionSequence = 0;

async function submitNewChatFixture(
  page: import("@playwright/test").Page,
): Promise<Record<string, unknown>> {
  const prompt = `verify scoped controls ${++newChatSubmissionSequence}`;
  await page.locator(".newchat-input").fill(prompt);
  await page.getByRole("button", { name: "开始", exact: true }).click();
  await expect(page.getByTestId("newchat-submission")).toContainText(prompt);
  return JSON.parse(
    await page.getByTestId("newchat-submission").innerText(),
  ) as Record<string, unknown>;
}

test("new-chat controls reset across device authorization scopes", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?newchat-controls=1");
  await chooseDangerousNewChatControls(page);

  await page.getByTestId("switch-newchat-device").click();
  await expect(page.getByTestId("newchat-scope"))
    .toHaveText("machine-b:code:codex");
  await expect(page.locator(".newchat-access")).toContainText("默认环境");

  const submitted = await submitNewChatFixture(page);
  expect(submitted.permissionMode).toBe("never");
  expect(submitted).not.toHaveProperty("permissionProfile");
  expect(submitted).not.toHaveProperty("webSearch");
});

test("new-chat controls reset across engine authorization scopes", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?newchat-controls=1");
  await chooseDangerousNewChatControls(page);

  await page.getByTestId("switch-newchat-engine").click();
  await expect(page.getByTestId("newchat-scope"))
    .toHaveText("machine-a:code:claude");
  await expect(page.locator(".newchat-access")).toHaveCount(0);
  const claudeSubmission = await submitNewChatFixture(page);
  expect(claudeSubmission).not.toHaveProperty("permissionMode");

  await page.getByTestId("switch-newchat-engine").click();
  await expect(page.locator(".newchat-access")).toContainText("默认环境");
  const codexSubmission = await submitNewChatFixture(page);
  expect(codexSubmission.permissionMode).toBe("never");
  expect(codexSubmission).not.toHaveProperty("permissionProfile");
  expect(codexSubmission).not.toHaveProperty("webSearch");
});

test("new-chat controls reset and normalize across Code and Work", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?newchat-controls=1");
  await chooseDangerousNewChatControls(page);

  await page.getByTestId("switch-newchat-space").click();
  await expect(page.getByTestId("newchat-scope"))
    .toHaveText("machine-a:work:codex");
  await expect(page.locator(".newchat-access")).toHaveCount(0);
  const fast = page.getByRole("button", {
    name: "新工作 Fast 服务档位",
  });
  await expect(fast).toHaveText("标准");
  await fast.click();
  await expect(fast).toHaveText("快速");
  const workSubmission = await submitNewChatFixture(page);
  expect(workSubmission.permissionMode).toBe("never");
  expect(workSubmission).not.toHaveProperty("permissionProfile");
  expect(workSubmission).not.toHaveProperty("webSearch");
  expect(workSubmission.serviceTier).toBe("fast");

  await page.getByTestId("switch-newchat-engine").click();
  await expect(page.getByTestId("newchat-scope"))
    .toHaveText("machine-a:work:claude");
  await expect(page.getByRole("button", {
    name: "新工作 Fast 服务档位",
  })).toHaveCount(0);
  await page.getByTestId("switch-newchat-engine").click();
  await expect(page.getByRole("button", {
    name: "新工作 Fast 服务档位",
  })).toHaveText("标准");

  await page.getByTestId("switch-newchat-space").click();
  await expect(page.locator(".newchat-access")).toContainText("默认环境");
});

test("a 256-character profile id fits a 296 px new-chat row", async ({
  page,
}) => {
  await page.setViewportSize({ width: 296, height: 720 });
  await page.goto(
    "/tests/history-browser.html?newchat-controls=1&long-profile=1",
  );
  await page.locator(".newchat-access").click();
  const customProfile = page.getByRole("dialog", {
    name: "权限与执行环境",
  }).locator('button[title^="custom-profile-"]');
  const profileRow = await customProfile.evaluate((button) => {
    const sheet = button.closest<HTMLElement>(".sheet");
    const name = button.querySelector<HTMLElement>(".cmd-nm");
    if (!sheet || !name) {
      throw new Error("permission-profile sheet row is incomplete");
    }
    const rowRect = button.getBoundingClientRect();
    const sheetRect = sheet.getBoundingClientRect();
    const nameStyle = getComputedStyle(name);
    return {
      viewportWidth: window.innerWidth,
      pageScrollWidth: document.documentElement.scrollWidth,
      sheetClientWidth: sheet.clientWidth,
      sheetScrollWidth: sheet.scrollWidth,
      rowLeft: rowRect.left,
      rowRight: rowRect.right,
      sheetLeft: sheetRect.left,
      sheetRight: sheetRect.right,
      nameClientWidth: name.clientWidth,
      nameScrollWidth: name.scrollWidth,
      nameOverflow: nameStyle.overflow,
      nameTextOverflow: nameStyle.textOverflow,
      titleLength: Array.from(button.getAttribute("title") ?? "").length,
    };
  });
  expect(profileRow.pageScrollWidth).toBeLessThanOrEqual(
    profileRow.viewportWidth);
  expect(profileRow.sheetScrollWidth).toBeLessThanOrEqual(
    profileRow.sheetClientWidth);
  expect(profileRow.rowLeft).toBeGreaterThanOrEqual(profileRow.sheetLeft);
  expect(profileRow.rowRight).toBeLessThanOrEqual(profileRow.sheetRight);
  expect(profileRow.nameScrollWidth).toBeGreaterThan(
    profileRow.nameClientWidth);
  expect(profileRow.nameOverflow).toBe("hidden");
  expect(profileRow.nameTextOverflow).toBe("ellipsis");
  expect(profileRow.titleLength).toBe(256);
  await customProfile.click();
  await page.locator(".scrim.show").click({ position: { x: 8, y: 8 } });

  const layout = await page.getByTestId("newchat-controls-fixture")
    .evaluate((node) => {
      const card = node.querySelector<HTMLElement>(".newchat-card");
      const access = node.querySelector<HTMLElement>(".newchat-access");
      if (!card || !access) throw new Error("new-chat controls are missing");
      const cardRect = card.getBoundingClientRect();
      const accessRect = access.getBoundingClientRect();
      return {
        fixtureClientWidth: node.clientWidth,
        fixtureScrollWidth: node.scrollWidth,
        cardClientWidth: card.clientWidth,
        cardScrollWidth: card.scrollWidth,
        accessLeft: accessRect.left,
        accessRight: accessRect.right,
        cardLeft: cardRect.left,
        cardRight: cardRect.right,
        label: access.textContent ?? "",
      };
    });
  expect(layout.fixtureScrollWidth).toBeLessThanOrEqual(
    layout.fixtureClientWidth);
  expect(layout.cardScrollWidth).toBeLessThanOrEqual(layout.cardClientWidth);
  expect(layout.accessLeft).toBeGreaterThanOrEqual(layout.cardLeft);
  expect(layout.accessRight).toBeLessThanOrEqual(layout.cardRight);
  expect(layout.label).toContain("…");
  expect(Array.from(layout.label).length).toBeLessThan(32);
});

test("new-chat controls fit the default permission picker on a short phone", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 568 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/tests/history-browser.html?newchat-controls=1");
  await page.locator(".newchat-access").click();
  const dialog = page.getByRole("dialog", {
    name: "权限与执行环境",
  });
  await expect(dialog).toBeVisible();

  const layout = await dialog.evaluate((sheet) => {
    const scroll = sheet.querySelector<HTMLElement>(".sheet-scroll");
    const optionButtons = Array.from(
      sheet.querySelectorAll<HTMLElement>(".permission-options .cmd"),
    );
    const searchButtons = Array.from(
      sheet.querySelectorAll<HTMLElement>(".cmd-search button"),
    );
    const descriptions = Array.from(
      sheet.querySelectorAll<HTMLElement>(".permission-options .cmd-ds"),
    );
    const live = searchButtons.find((button) => button.textContent === "Live");
    if (
      !scroll || optionButtons.length !== 6 || descriptions.length !== 6 || !live
    ) {
      throw new Error("compact permission controls are incomplete");
    }
    const sheetRect = sheet.getBoundingClientRect();
    return {
      viewportHeight: window.innerHeight,
      viewportWidth: window.innerWidth,
      pageScrollWidth: document.documentElement.scrollWidth,
      sheetTop: sheetRect.top,
      sheetBottom: sheetRect.bottom,
      scrollTop: scroll.scrollTop,
      scrollHeight: scroll.scrollHeight,
      scrollClientHeight: scroll.clientHeight,
      liveBottom: live.getBoundingClientRect().bottom,
      minOptionHeight: Math.min(...optionButtons.map(
        (button) => button.getBoundingClientRect().height,
      )),
      minSearchHeight: Math.min(...searchButtons.map(
        (button) => button.getBoundingClientRect().height,
      )),
      minDescriptionWidth: Math.min(...descriptions.map(
        (description) => description.getBoundingClientRect().width,
      )),
      minDescriptionHeight: Math.min(...descriptions.map(
        (description) => description.getBoundingClientRect().height,
      )),
    };
  });

  expect(layout.scrollTop).toBe(0);
  expect(layout.scrollHeight).toBeLessThanOrEqual(
    layout.scrollClientHeight + 1,
  );
  expect(layout.sheetTop).toBeGreaterThanOrEqual(0);
  expect(layout.sheetBottom).toBeLessThanOrEqual(layout.viewportHeight + 1);
  expect(layout.liveBottom).toBeLessThanOrEqual(layout.viewportHeight + 1);
  expect(layout.minOptionHeight).toBeGreaterThanOrEqual(44);
  expect(layout.minSearchHeight).toBeGreaterThanOrEqual(44);
  expect(layout.minDescriptionWidth).toBeGreaterThan(1);
  expect(layout.minDescriptionHeight).toBeGreaterThan(1);
  expect(layout.pageScrollWidth).toBeLessThanOrEqual(layout.viewportWidth);
});

test("default permission picker stays compact on a tall phone", async ({
  page,
}) => {
  await page.setViewportSize({ width: 430, height: 852 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/tests/history-browser.html?newchat-controls=1");
  await page.locator(".newchat-access").click();
  const dialog = page.getByRole("dialog", {
    name: "权限与执行环境",
  });
  await expect(dialog).toBeVisible();

  const layout = await dialog.evaluate((sheet) => {
    const scroll = sheet.querySelector<HTMLElement>(".sheet-scroll");
    const options = Array.from(
      sheet.querySelectorAll<HTMLElement>(".permission-options .cmd"),
    );
    const live = Array.from(
      sheet.querySelectorAll<HTMLElement>(".cmd-search button"),
    ).find((button) => button.textContent === "Live");
    if (!scroll || options.length !== 6 || !live) {
      throw new Error("compact permission controls are incomplete");
    }
    return {
      scrollHeight: scroll.scrollHeight,
      clientHeight: scroll.clientHeight,
      liveBottom: live.getBoundingClientRect().bottom,
      sheetBottom: sheet.getBoundingClientRect().bottom,
      minOptionHeight: Math.min(...options.map(
        (button) => button.getBoundingClientRect().height,
      )),
    };
  });

  expect(layout.scrollHeight).toBeLessThanOrEqual(layout.clientHeight + 1);
  expect(layout.liveBottom).toBeLessThanOrEqual(layout.sheetBottom + 1);
  expect(layout.minOptionHeight).toBeGreaterThanOrEqual(44);

  await page.setViewportSize({ width: 1200, height: 800 });
  const desktopDescriptions = await dialog.locator(".permission-options .cmd-ds")
    .evaluateAll((nodes) => nodes.map((node) => {
      const rect = node.getBoundingClientRect();
      return { width: rect.width, height: rect.height };
    }));
  expect(desktopDescriptions).toHaveLength(6);
  expect(desktopDescriptions.every(
    ({ width, height }) => width > 1 && height > 1,
  )).toBe(true);
});

test("new-chat controls keep scrolling for many custom permission profiles", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 400 });
  await page.goto(
    "/tests/history-browser.html?newchat-controls=1&many-profiles=1",
  );
  await page.locator(".newchat-access").click();
  const dialog = page.getByRole("dialog", {
    name: "权限与执行环境",
  });
  await expect(dialog).toBeVisible();

  const scrollState = await dialog.locator(".sheet-scroll").evaluate((scroll) => ({
    clientHeight: scroll.clientHeight,
    scrollHeight: scroll.scrollHeight,
    overflowY: getComputedStyle(scroll).overflowY,
  }));
  expect(scrollState.scrollHeight).toBeGreaterThan(scrollState.clientHeight);
  expect(scrollState.overflowY).toBe("auto");

  const live = dialog.getByRole("button", { name: "Live", exact: true });
  await live.scrollIntoViewIfNeeded();
  await expect(live).toBeInViewport();
});

test("new-chat controls fit when the visual app height is keyboard-sized", async ({
  page,
}) => {
  await page.setViewportSize({ width: 393, height: 852 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/tests/history-browser.html?newchat-controls=1");
  await page.evaluate(() => {
    document.documentElement.style.setProperty("--app-height", "400px");
    document.documentElement.style.setProperty("--keyboard-inset", "452px");
    document.documentElement.setAttribute("data-short-viewport", "ime");
  });
  await page.locator(".newchat-access").click();
  const dialog = page.getByRole("dialog", {
    name: "权限与执行环境",
  });
  await expect(dialog).toBeVisible();

  const layout = await dialog.evaluate((sheet) => {
    const scroll = sheet.querySelector<HTMLElement>(".sheet-scroll");
    const live = Array.from(
      sheet.querySelectorAll<HTMLElement>(".cmd-search button"),
    ).find((button) => button.textContent === "Live");
    if (!scroll || !live) throw new Error("permission search controls missing");
    return {
      scrollHeight: scroll.scrollHeight,
      clientHeight: scroll.clientHeight,
      liveBottom: live.getBoundingClientRect().bottom,
      sheetBottom: sheet.getBoundingClientRect().bottom,
    };
  });
  expect(layout.scrollHeight).toBeLessThanOrEqual(layout.clientHeight + 1);
  expect(layout.liveBottom).toBeLessThanOrEqual(layout.sheetBottom + 1);
});

test("Work multi-account controls filter labels and seed a new immutable owner", async ({
  page,
}) => {
  await page.setViewportSize({ width: 720, height: 900 });
  await page.goto("/tests/history-browser.html?profile-sidebar=work");

  await expect(page.getByRole("group", {
    name: "筛选 Codex 账号",
  })).toBeVisible();
  await expect(page.locator(".scard-profile-ribbon")).toHaveCount(2);

  await page.getByRole("button", { name: "nyx · Stack" }).click();
  await expect(page.locator(".scard")).toHaveCount(1);
  await expect(page.locator(".scard-profile-ribbon")).toHaveText("nyx");
  await page.getByRole("button", { name: "新工作" }).click();
  await expect(page.getByTestId("new-work-profile")).toHaveText("stack");

  await page.getByRole("tab", { name: "Code" }).click();
  await expect(page.getByRole("button", { name: "全部" })).toHaveClass(/active/);
  await expect(page.locator(".scard")).toHaveCount(2);

  await page.getByRole("tab", { name: "Work" }).click();
  await expect(page.getByRole("button", { name: "nyx · Stack" })).toHaveClass(/active/);
  await expect(page.locator(".scard")).toHaveCount(1);

  await page.getByRole("button", { name: "全部" }).click();
  await expect(page.locator(".scard")).toHaveCount(2);
});

test("profile keycaps hang from session cards without shifting titles", async ({
  page,
}) => {
  await page.setViewportSize({ width: 720, height: 900 });
  await page.goto("/tests/history-browser.html?profile-sidebar=1");

  const activeCard = page.locator(".scard.active");
  await expect(activeCard).toBeVisible();
  const geometry = await activeCard.evaluate((card) => {
    const keycap = card.querySelector<HTMLElement>(".scard-profile-ribbon");
    const title = card.querySelector<HTMLElement>(".scard-title");
    const preview = card.querySelector<HTMLElement>(".scard-prev");
    if (!keycap || !title || !preview) {
      throw new Error("profile sidebar fixture is incomplete");
    }
    const cardRect = card.getBoundingClientRect();
    const keycapRect = keycap.getBoundingClientRect();
    const titleRect = title.getBoundingClientRect();
    const previewRect = preview.getBoundingClientRect();
    return {
      cardLeft: cardRect.left,
      cardTop: cardRect.top,
      keycapTop: keycapRect.top,
      keycapBottom: keycapRect.bottom,
      keycapWidth: keycapRect.width,
      titleLeft: titleRect.left,
      titleTop: titleRect.top,
      previewLeft: previewRect.left,
      position: getComputedStyle(keycap).position,
    };
  });

  expect(geometry.position).toBe("absolute");
  expect(geometry.keycapTop).toBeLessThan(geometry.cardTop);
  expect(geometry.keycapBottom).toBeGreaterThan(geometry.cardTop);
  expect(geometry.keycapBottom).toBeLessThanOrEqual(geometry.titleTop);
  expect(geometry.keycapWidth).toBeLessThanOrEqual(64);
  expect(geometry.titleLeft - geometry.cardLeft).toBeLessThanOrEqual(18);
  expect(Math.abs(geometry.titleLeft - geometry.previewLeft)).toBeLessThanOrEqual(1);

  const ordinaryCard = page.locator(".scard").filter({
    hasText: "cc-remote 派生",
  });
  const ordinaryGeometry = await ordinaryCard.evaluate((card) => {
    const title = card.querySelector<HTMLElement>(".scard-title");
    if (!title) throw new Error("ordinary profile title missing");
    const style = getComputedStyle(card);
    return {
      titleInset:
        title.getBoundingClientRect().left - card.getBoundingClientRect().left,
      borderColor: style.borderTopColor,
      backgroundColor: style.backgroundColor,
    };
  });
  expect(ordinaryGeometry.titleInset).toBeLessThanOrEqual(18);
  expect(ordinaryGeometry.borderColor).not.toBe("transparent");
  expect(ordinaryGeometry.borderColor).not.toBe("rgba(0, 0, 0, 0)");
  expect(ordinaryGeometry.backgroundColor).not.toBe("rgba(0, 0, 0, 0)");
});

test("profile session card manual unread survives refresh until explicit opening", async ({ page }) => {
  await page.goto("/tests/history-browser.html?profile-sidebar=code");
  const active = page.locator(".scard").filter({ hasText: "看看当前仓库" });
  await active.getByRole("button", { name: "更多操作" }).click();
  await active.getByRole("button", { name: "标记为未读" }).click();
  await expect(active).toHaveClass(/active/);
  await expect(active.locator(".pill.completed")).toHaveText("未读");
  await page.reload();
  await expect(active.locator(".pill.completed")).toHaveText("未读");
  await page.goto("/tests/history-browser.html?profile-sidebar=code&machine=another-device");
  await expect(active.locator(".pill.completed")).toHaveCount(0);
  await page.goto("/tests/history-browser.html?profile-sidebar=code");
  await expect(active.locator(".pill.completed")).toHaveText("未读");
  await page.locator(".scard").filter({ hasText: "cc-remote 派生" }).click();
  await active.click();
  await expect(active.locator(".pill.completed")).toHaveCount(0);
  await page.reload();
  await expect(active.locator(".pill.completed")).toHaveCount(0);
});

test("profile session card manual unread stays usable when storage is unavailable", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(() => {
    const mode = new URLSearchParams(location.search).get("unread-storage");
    if (mode === "missing" || mode === "access-denied") {
      Object.defineProperty(window, "localStorage", {
        configurable: true,
        get() {
          if (mode === "access-denied") throw new DOMException("blocked", "SecurityError");
          return undefined;
        },
      });
    } else {
      const getItem = Storage.prototype.getItem;
      const setItem = Storage.prototype.setItem;
      Storage.prototype.getItem = function (key) {
        if (mode === "read-denied" && key === "cc-remote:manual-unread-v1")
          throw new DOMException("blocked", "SecurityError");
        return getItem.call(this, key);
      };
      Storage.prototype.setItem = function (key, value) {
        if (mode === "write-denied" && key === "cc-remote:manual-unread-v1")
          throw new DOMException("full", "QuotaExceededError");
        return setItem.call(this, key, value);
      };
    }
  });
  for (const mode of ["missing", "access-denied", "read-denied", "write-denied"]) {
    await page.goto(`/tests/history-browser.html?profile-sidebar=code&unread-storage=${mode}&machine=storage-${mode}`);
    const active = page.locator(".scard").filter({ hasText: "看看当前仓库" });
    await active.getByRole("button", { name: "更多操作" }).click();
    await active.getByRole("button", { name: "标记为未读" }).click();
    await expect(active.locator(".pill.completed")).toHaveText("未读");
    await page.evaluate(() => {
      // A storage event must not crash or erase local state if access is denied;
      // unrelated/sessionStorage events must never replace these local marks.
      window.dispatchEvent(new StorageEvent("storage", { key: "cc-remote:manual-unread-v1" }));
      window.dispatchEvent(new StorageEvent("storage", { storageArea: sessionStorage }));
    });
    await expect(active.locator(".pill.completed")).toHaveText("未读");
    await active.click();
    await expect(active.locator(".pill.completed")).toHaveCount(0);
    await active.getByRole("button", { name: "更多操作" }).click();
    await active.getByRole("button", { name: "标记为未读" }).click();
    await expect(active.locator(".pill.completed")).toHaveText("未读");
    await page.reload();
    await expect(active.locator(".pill.completed")).toHaveCount(0);
  }
  expect(errors).toEqual([]);
});

test("profile session card manual unread still synchronizes across tabs", async ({ page, context }) => {
  await page.goto("/tests/history-browser.html?profile-sidebar=code");
  const other = await context.newPage();
  try {
    await other.goto("/tests/history-browser.html?profile-sidebar=code");
    const active = page.locator(".scard").filter({ hasText: "看看当前仓库" });
    const otherActive = other.locator(".scard").filter({ hasText: "看看当前仓库" });
    await active.getByRole("button", { name: "更多操作" }).click();
    await active.getByRole("button", { name: "标记为未读" }).click();
    await expect(otherActive.locator(".pill.completed")).toHaveText("未读");
    await otherActive.click();
    await expect(active.locator(".pill.completed")).toHaveCount(0);
    await active.getByRole("button", { name: "更多操作" }).click();
    await active.getByRole("button", { name: "标记为未读" }).click();
    await expect(otherActive.locator(".pill.completed")).toHaveText("未读");
    await other.evaluate(() => localStorage.clear());
    await expect(active.locator(".pill.completed")).toHaveCount(0);
  } finally {
    await other.close();
  }
});

test("profile session card edges remain visible in dark theme", async ({
  page,
}) => {
  await page.setViewportSize({ width: 720, height: 900 });
  await page.goto("/tests/history-browser.html?profile-sidebar=1&theme=dark");
  await page.waitForFunction(() =>
    document.documentElement.dataset.theme === "dark"
  );
  await page.waitForTimeout(200);

  const appearance = await page.locator(".scard").evaluateAll((cards) => {
    const sample = (color: string) => {
      const canvas = document.createElement("canvas");
      canvas.width = 1;
      canvas.height = 1;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("canvas context unavailable");
      context.clearRect(0, 0, 1, 1);
      context.fillStyle = color;
      context.fillRect(0, 0, 1, 1);
      return Array.from(context.getImageData(0, 0, 1, 1).data);
    };
    const channelDelta = (left: number[], right: number[]) =>
      Math.max(...left.slice(0, 3).map((value, index) =>
        Math.abs(value - right[index])
      ));
    const sidebar = sample(getComputedStyle(document.documentElement)
      .getPropertyValue("--sidebar"));
    return cards.map((card) => {
      const style = getComputedStyle(card);
      const border = sample(style.borderTopColor);
      const background = sample(style.backgroundColor);
      return {
        active: card.classList.contains("active"),
        borderAlpha: border[3],
        borderCardDelta: channelDelta(border, background),
        borderSidebarDelta: channelDelta(border, sidebar),
        background: style.backgroundColor,
        boxShadow: style.boxShadow,
      };
    });
  });

  expect(appearance).toHaveLength(2);
  for (const card of appearance) {
    expect(card.borderAlpha).toBe(255);
    expect(card.borderCardDelta).toBeGreaterThanOrEqual(20);
    expect(card.borderSidebarDelta).toBeGreaterThanOrEqual(20);
  }
  expect(appearance.some((card) => card.active)).toBe(true);
  expect(appearance.some((card) => !card.active)).toBe(true);
  const active = appearance.find((card) => card.active)!;
  const inactive = appearance.find((card) => !card.active)!;
  expect(active.background).not.toBe(inactive.background);
  expect(active.boxShadow).not.toBe("none");
});
