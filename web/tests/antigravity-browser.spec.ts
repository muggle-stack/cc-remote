import { expect, test, type Locator, type Page } from "@playwright/test";
import { PROTOCOL_VERSION, type AntigravityOption, type NativeCommandInfo } from "../src/protocol";

const sid = "agy@native-session";
const messageReceiver = "codex-receiver";
const commands: NativeCommandInfo[] = [
  { name: "compact", description: "Compress native context", attachments: false },
  { name: "plan", description: "Enter native planning", attachments: false },
  { name: "goal", description: "Run until the goal is complete", attachments: false },
];
const options = (model: string, mode: string): AntigravityOption[] => [
  { category: "model", current: model, choices: [
    { value: "neon", name: "Gemini 4 Neon (High)", description: "" },
    { value: "pro", name: "Gemini Pro", description: "" },
  ] },
  { category: "mode", current: mode, choices: [
    { value: "default", name: "Default", description: "" },
    { value: "plan", name: "Plan", description: "" },
  ] },
  ...(model === "pro" ? [{ category: "thought_level", current: "low", choices: [
    { value: "low", name: "Low", description: "" },
    { value: "extra", name: "Extra", description: "" },
  ] } satisfies AntigravityOption] : []),
];

async function mockRelay(page: Page, enabled: boolean, withSessionMessage = false, emptyGeminiHistory = false) {
  const seen: Record<string, unknown>[] = [];
  let emit = (_event: Record<string, unknown>) => {};
  let created = withSessionMessage || emptyGeminiHistory;
  let workspaceTrusted = true;
  let hidden = false;
  let title = "Gemini session";
  let state = "idle";
  let finishFollowup: (() => void) | undefined;
  let model = "neon";
  let mode = "default";
  let seq = 0;
  let quotaPlan: "Starter" | "Pro" = "Starter";
  let quotaHeld = false;
  let pendingQuota: Record<string, unknown> | undefined;
  await page.addInitScript(() => {
    localStorage.setItem("cc_remote_engine", "codex");
    localStorage.setItem("cc_remote_machine", "gemini-machine");
    localStorage.setItem("cc_remote_theme", "dark");
  });
  await page.route("**/api/**", route => route.fulfill({ status: 404 }));
  await page.route("**/api/session", route => route.fulfill({ json: {} }));
  await page.route("**/api/viewers/pages", route => route.fulfill({ json: { pages: [] } }));
  await page.route("**/api/devices", route => route.fulfill({ json: {
    devices: [{ machine_id: "gemini-machine", label: "Development", online: true }],
  } }));
  await page.routeWebSocket(/\/ws(?:\?|$)/, socket => {
    let connected = false;
    emit = event => socket.send(JSON.stringify({ v: PROTOCOL_VERSION, ts: Date.now() / 1000, ...event }));
    const controls = () => {
      emit({ type: "antigravity_state", sid, connected: true, options: options(model, mode), commands, seq: ++seq });
      emit({ type: "model", sid, model, seq: ++seq });
      emit({ type: "perm", sid, mode, seq: ++seq });
      emit({ type: "effort", sid, effort: model === "pro" ? "extra" : "", seq: ++seq });
      emit({ type: "context_report", sid, available: true, source: "native_estimate",
        total_tokens: 1024, max_tokens: 1048576, percentage: .1 });
    };
    const snapshot = () => emit({ type: "snapshot", sid, state, cc_session_id: sid,
      tail_text: "", cwd: "/tmp", generation: "current-wrapper", seq });
    socket.onMessage(raw => {
      const cmd = JSON.parse(String(raw)) as Record<string, unknown>;
      seen.push(cmd);
      if (cmd.type === "hello") {
        if (!connected) {
          connected = true;
          emit({ type: "wrapper_reconnected", generation: "current-wrapper" });
        }
        if (cmd.discover_engines) emit({ type: "antigravity_availability", enabled });
        if (created) { snapshot(); controls(); }
      }
      if (cmd.type === "list_sessions") {
        emit({ type: "session_list", engine: cmd.engine ?? "claude", space: cmd.space ?? "code",
          request_id: cmd.cmd_id, sessions: cmd.engine === "antigravity" && created
            ? [{ session_id: sid, engine: "antigravity", space: "code", state: "idle", cwd: "/tmp",
              summary: title, tag: hidden ? "archived" : null }]
            : withSessionMessage && cmd.engine === "codex"
              ? [{ session_id: messageReceiver, engine: "codex", space: "code", state: "idle", cwd: "/tmp",
                summary: "Codex receiver" }] : [] });
      }
      if (cmd.type === "get_models" && cmd.engine === "antigravity") {
        const selected = String(cmd.antigravity_model ?? (cmd.sid ? model : "neon"));
        emit({ type: "models", engine: cmd.engine, sid: cmd.sid, cwd: cmd.cwd, antigravity_model: cmd.antigravity_model,
          default_model: "neon", antigravity_options: options(selected, mode), antigravity_commands: commands,
          models: [{ id: "neon", display_name: "Gemini 4 Neon (High)", efforts: [] },
            { id: "pro", display_name: "Gemini Pro", efforts: selected === "pro" ? ["low", "extra"] : [],
              effort_names: { low: "Low", extra: "Extra" } }] });
      }
      if (cmd.type === "get_antigravity_quota") {
        const weekly = { id: "gemini-weekly", window: "weekly", name: "Weekly Limit Remaining",
          remaining_fraction: .9419, resets_at: Math.floor(Date.now() / 1000) + 3600 };
        const fiveHour = { id: "gemini-5h", window: "5h", name: "Five Hour Limit Remaining",
          remaining_fraction: .8 };
        const result = { type: "antigravity_quota", request_id: cmd.cmd_id,
          plan: quotaPlan === "Starter" ? "Antigravity Starter Quota" : "Pro",
          observed_at: Math.floor(Date.now() / 1000), groups: [
            { name: "Gemini Models", models: ["Gemini 4 Neon"], buckets: quotaPlan === "Starter"
              ? [weekly] : [weekly, fiveHour] },
            { name: "Claude and GPT models", models: ["Claude Sonnet 4.6"], buckets: [
              { ...weekly, id: "3p-weekly", remaining_fraction: 1, resets_at: null },
              { ...fiveHour, id: "3p-5h", remaining_fraction: 1 },
            ] },
          ] };
        if (quotaHeld) pendingQuota = result;
        else emit(result);
      }
      if (cmd.type === "new_session" && cmd.engine === "antigravity") {
        if (!workspaceTrusted) {
          emit({ type: "error", code: "gemini_workspace_untrusted", message: "Gemini workspace needs trust",
            request_id: cmd.request_id, msg_id: cmd.msg_id });
          return;
        }
        created = true;
        model = String(cmd.model ?? "neon");
        mode = String(cmd.antigravity_mode ?? "default");
        emit({ type: "session_focus", session_id: sid, cwd: "/tmp", request_id: cmd.request_id });
        snapshot();
        controls();
        emit({ type: "session_list_invalidated", engine: "antigravity", space: "code" });
      }
      if (cmd.type === "switch_session") {
        emit({ type: "session_focus", session_id: cmd.session_id, cwd: "/tmp" });
        if (cmd.session_id === sid) controls();
      }
      if (cmd.type === "get_history" && cmd.session_id === messageReceiver) {
        emit({ type: "history", sid: messageReceiver, session_id: messageReceiver, detail: "summary",
          generation: "current-wrapper", revision: "receiver-history", build_seq: 1, events: [], turns: [
            { id: "forwarded-message", sourceThreadId: "ccr:antigravity:native-session",
              prompt: "Gemini 发送的消息", done: true, blocks: [] },
          ] });
      }
      if (cmd.type === "get_history" && cmd.session_id === sid) {
        emit({ type: "history", sid, session_id: sid, detail: "summary", generation: "current-wrapper",
          revision: "native-history", build_seq: 1, live_seq: seq, in_progress: state !== "idle", events: [], turns: emptyGeminiHistory ? [] : [{ id: "test-turn", prompt: "验证 Gemini 接入",
            done: true, doneTs: Date.now(),
            tokenUsage: { input_tokens: 17, output_tokens: 9, cache_read_tokens: 5, thought_tokens: 3 },
            blocks: [{ kind: "text", channel: "final", text: "Gemini 已就绪。", message_id: "test-response", done: true }] }] });
      }
      if (cmd.type === "antigravity_auth") {
        emit({ type: "antigravity_auth_result", request_id: cmd.cmd_id, methods: ["oauth-personal", "oauth-business"], signed_in: !!cmd.method });
      }
      if (cmd.type === "get_engine_capabilities" && cmd.engine === "antigravity") {
        emit({ type: "engine_capabilities", engine: "antigravity", space: "code", cwd: cmd.cwd,
          request_id: cmd.cmd_id, skills_only: cmd.skills_only, items: [
            { kind: "skill", id: "review", name: "Fixture Review" },
          ] });
      }
      if (cmd.type === "rename_session" && cmd.engine === "antigravity") {
        title = String(cmd.title);
        emit({ type: "session_list_invalidated", engine: "antigravity", space: "code" });
      }
      if (cmd.type === "set_antigravity_option" && cmd.category === "mode") {
        mode = String(cmd.value);
        controls();
      }
      if (cmd.type === "answer_question") {
        emit({ type: "ask_user_closed", sid, ask_id: cmd.ask_id, reason: "answered", seq: ++seq });
      }
      if (cmd.type === "archive_session") {
        hidden = cmd.archived === true;
        emit({ type: "session_list_invalidated", engine: "antigravity", space: "code" });
      }
      if (cmd.type === "interrupt") {
        if (finishFollowup) {
          finishFollowup();
          finishFollowup = undefined;
        } else {
          state = "idle";
          emit({ type: "state", state, sid, seq: ++seq });
        }
      }
      if (cmd.cmd_id) emit({ type: "command_ack", cmd_id: cmd.cmd_id, client_id: cmd.client_id });
    });
  });
  return {
    seen,
    followup: (awaitingConfirmation = true) => {
      const turn = "agy-automatic-followup";
      const message = "agy-automatic-result";
      emit({ type: "turn_binding", sid, seq: ++seq, autonomous: true, msg_id: turn, turn_id: turn });
      state = "running";
      emit({ type: "state", sid, seq: ++seq, state, msg_id: turn });
      emit({ type: "assistant_msg_start", sid, seq: ++seq, message_id: message, turn_id: turn, channel: "unknown" });
      emit({ type: "delta", sid, seq: ++seq, message_id: message, turn_id: turn, text: "后台检查完成，结果已保存。" });
      const finish = (subtype: "interrupted" | "background_followup") => {
        emit({ type: "assistant_msg_end", sid, seq: ++seq, message_id: message, turn_id: turn });
        emit({ type: "turn_end", sid, seq: ++seq, turn_id: turn,
          result: { subtype, duration_ms: 40, is_error: false } });
        state = "idle";
        emit({ type: "state", sid, seq: ++seq, state });
      };
      if (awaitingConfirmation) {
        emit({ type: "state", sid, seq: ++seq, state, msg_id: turn,
          detail: "Gemini 尚未确认续写结束，可点停止继续处理排队消息" });
        finishFollowup = () => finish("interrupted");
      }
      return () => finish("background_followup");
    },
    requireWorkspaceTrust: () => { workspaceTrusted = false; },
    setQuotaPlan: (value: "Starter" | "Pro") => { quotaPlan = value; },
    holdQuota: () => { quotaHeld = true; },
    releaseQuota: () => {
      quotaHeld = false;
      if (pendingQuota) emit(pendingQuota);
      pendingQuota = undefined;
    },
    draining: () => {
      state = "draining";
      emit({ type: "state", state, sid, seq: ++seq });
    },
    ask: () => {
      const question = { header: "Gemini 工具权限", question: "File note.txt\n\n/tmp/fixture/note.txt",
        options: [{ label: "Allow once" }, { label: "Deny" }], allow_text: false };
      emit({ type: "ask_user", sid, seq: ++seq, ask_id: "agy-approval",
        ...question, questions: [question], dismiss_actions: ["cancel"],
        approval_diff: "--- a//tmp/fixture/note.txt\n+++ b//tmp/fixture/note.txt\n@@ -1 +1 @@\n-before\n+after\n" });
    },
  };
}

test("Gemini is hidden on a device without Antigravity enabled", async ({ page }) => {
  const relay = await mockRelay(page, false);
  await page.goto("/");
  await expect.poll(() => relay.seen.some(cmd => cmd.type === "hello")).toBe(true);
  await page.getByRole("button", { name: "切换新会话引擎" }).click();
  await expect(page.getByRole("menuitemradio", { name: "Codex" })).toBeVisible();
  await expect(page.getByRole("menuitemradio", { name: "Gemini" })).toHaveCount(0);
});

test("Gemini explains device-side workspace trust on new session denial", async ({ page, hasTouch }) => {
  const relay = await mockRelay(page, true);
  relay.requireWorkspaceTrust();
  await page.goto("/");
  await page.getByRole("button", { name: "切换新会话引擎" }).click();
  await page.getByRole("menuitemradio", { name: "Gemini" }).click();
  await page.locator(".newchat-input").fill("Check this project");
  const send = page.getByRole("button", { name: "开始", exact: true });
  if (hasTouch) await send.tap();
  else await send.click();
  const error = page.getByText(/CC_REMOTE_ANTIGRAVITY_TRUSTED_WORKSPACES/);
  await expect(error).toBeVisible();
  await expect(error).toContainText('["*"]');
});

test("a session message opens its original Gemini Code session", async ({ page }) => {
  const relay = await mockRelay(page, true, true);
  await page.goto("/");
  const source = page.getByRole("button", { name: /来自会话.*Gemini session/ });
  await expect(source).toBeEnabled();
  await source.click();
  await expect(page.locator("html")).toHaveAttribute("data-engine", "antigravity");
  await expect.poll(() => relay.seen.some(cmd => cmd.type === "switch_session" && cmd.session_id === sid)).toBe(true);
  await expect(page.getByText("Gemini 已就绪。")).toBeVisible();
  const replyFooter = page.locator(".ai-meta").last();
  await expect(replyFooter.locator(".ubub-time")).toHaveCount(1);
  await expect(replyFooter.getByRole("button", { name: "复制" })).toBeVisible();
  await expect(replyFooter.getByText(/输入|输出/)).toHaveCount(0);
});

test("Gemini keeps a background follow-up active until Stop is acknowledged", async ({ page, hasTouch }) => {
  const relay = await mockRelay(page, true, true);
  await page.goto("/");
  await page.getByRole("button", { name: /来自会话.*Gemini session/ }).click();
  await expect(page.getByText("Gemini 已就绪。")).toBeVisible();
  relay.followup();
  await expect(page.getByText("后台检查完成，结果已保存。")).toBeVisible();
  await expect(page.locator(".composer textarea")).toHaveAttribute("placeholder", /补充/);
  await expect(page.getByText(/Gemini 尚未确认续写结束/)).toBeVisible();
  const stop = page.getByRole("button", { name: "停止", exact: true });
  await (hasTouch ? stop.tap() : stop.click());
  await expect.poll(() => relay.seen.some(cmd => cmd.type === "interrupt" && cmd.sid === sid)).toBe(true);
  await expect(page.locator(".composer textarea")).toHaveAttribute("placeholder", /输入/);
  await expect(page.getByText(/Gemini 尚未确认续写结束/)).toHaveCount(0);
  await expect(page.getByText("后台检查完成，结果已保存。")).toHaveCount(1);
  expect(relay.seen.some(cmd => cmd.type === "query")).toBe(false);
});

test("Gemini returns to idle after a native background completion", async ({ page }) => {
  const relay = await mockRelay(page, true, true);
  await page.goto("/");
  await page.getByRole("button", { name: /来自会话.*Gemini session/ }).click();
  await expect(page.getByText("Gemini 已就绪。")).toBeVisible();
  const finish = relay.followup(false);
  await expect(page.locator(".composer textarea")).toHaveAttribute("placeholder", /补充/);
  finish();
  await expect(page.locator(".composer textarea")).toHaveAttribute("placeholder", /输入/);
  await expect(page.getByText("后台检查完成，结果已保存。")).toHaveCount(1);
  expect(relay.seen.some(cmd => cmd.type === "interrupt")).toBe(false);
});

test("Gemini exposes its sign-in, read-only catalog and local clear command", async ({ page, hasTouch }) => {
  const relay = await mockRelay(page, true, false, true);
  const press = (control: Locator) => hasTouch ? control.tap() : control.click();
  await page.goto("/");
  await press(page.getByRole("button", { name: "切换新会话引擎" }));
  await press(page.getByRole("menuitemradio", { name: "Gemini" }));
  await expect(page.locator("html")).toHaveAttribute("data-engine", "antigravity");
  await press(page.getByRole("button", { name: "更多设置", exact: true }));
  await press(page.getByRole("button", { name: /^Gemini 登录/ }));
  const auth = page.getByRole("dialog", { name: "Gemini 登录" });
  await press(auth.getByRole("button", { name: "Google 个人账号" }));
  await expect(auth.getByText("Gemini 登录成功。")).toBeVisible();
  await expect.poll(() => relay.seen.find(cmd => cmd.type === "antigravity_auth" && cmd.method)).toMatchObject({
    engine: "antigravity", method: "oauth-personal",
  });
  await press(auth.getByRole("button", { name: "关闭Gemini 登录" }));
  const input = page.locator(".composer textarea");
  await input.fill("/extensions");
  await input.press("Enter");
  const extensions = page.getByRole("dialog", { name: "Extensions" });
  await expect(extensions.getByText("Fixture Review")).toBeVisible();
  await expect(extensions.getByRole("button", { name: "新建 Skill" })).toHaveCount(0);
  await press(extensions.getByRole("button", { name: "关闭" }));
  await input.fill("/clear");
  await input.press("Enter");
  await expect(page.locator(".newchat-input")).toBeVisible();
  expect(relay.seen.filter(cmd => cmd.type === "new_session" || cmd.type === "query")).toHaveLength(0);
});

test("Gemini colors carry through empty sessions, appearance and question dialogs", async ({ page, hasTouch }) => {
  const relay = await mockRelay(page, true, false, true);
  const press = (control: Locator) => hasTouch ? control.tap() : control.click();
  await page.goto("/");
  await press(page.getByRole("button", { name: "切换新会话引擎" }));
  await press(page.getByRole("menuitemradio", { name: "Gemini" }));
  await expect(page.locator(".thread-shell .empty .glyph img.antigravity")).toBeVisible();
  await expect(page.locator(".thread-shell .empty")).toContainText("查看 Gemini 命令");

  await press(page.getByRole("button", { name: "更多设置", exact: true }));
  await press(page.getByRole("button", { name: /^外观/ }));
  const picker = page.getByRole("dialog", { name: "外观", exact: true });
  const done = picker.getByRole("button", { name: "完成", exact: true });
  await expect(done).toHaveCSS("background-color", "rgb(168, 199, 250)");
  await expect(picker.locator(".appearance-miniature.dark")).toHaveCSS("background-color", "rgb(17, 18, 22)");
  await press(picker.getByRole("button", { name: "浅色", exact: true }));
  await expect(done).toHaveCSS("background-color", "rgb(57, 95, 168)");
  await expect(page.locator(".centered-sheet-scrim")).toHaveCSS("background-color", "rgba(30, 43, 70, 0.34)");
  await press(done);

  relay.ask();
  const question = page.getByRole("dialog", { name: "操作确认" });
  await expect(question.locator(".question-dialog-title img.antigravity")).toBeVisible();
  await expect(question.getByText("Gemini 想确认一下")).toBeVisible();
  const submit = question.getByRole("button", { name: "提交回答" });
  const cancel = question.getByRole("button", { name: "取消请求" });
  await expect(submit).toHaveCSS("background-color", "rgb(57, 95, 168)");
  await expect(question.locator(".qa-footer")).toHaveCount(1);
  await expect(cancel).toBeVisible();
  expect((await cancel.boundingBox())?.y).toBeCloseTo((await submit.boundingBox())?.y ?? -1, 0);
  expect(await question.evaluate(node => getComputedStyle(node, "::backdrop").backgroundColor))
    .toBe("rgba(30, 43, 70, 0.34)");
  await press(cancel);
  await expect.poll(() => relay.seen.find(cmd => cmd.type === "answer_question")).toMatchObject({
    ask_id: "agy-approval", action: "cancel",
  });
});

for (const plan of ["Starter", "Pro"] as const) {
  test(`Gemini quota displays ${plan} native windows with a subscription badge`, async ({ page, hasTouch }) => {
    const relay = await mockRelay(page, true);
    relay.setQuotaPlan(plan);
    relay.holdQuota();
    const press = (control: Locator) => hasTouch ? control.tap() : control.click();
    await page.goto("/");
    await press(page.getByRole("button", { name: "切换新会话引擎" }));
    await press(page.getByRole("menuitemradio", { name: "Gemini" }));
    const pending = page.getByRole("button", { name: /账户额度：.*尚未同步/ });
    await expect(pending.locator(".usage-meter-line b")).toHaveText(["5h", "周"]);
    await expect.poll(() => relay.seen.some(cmd => cmd.type === "get_antigravity_quota")).toBe(true);
    relay.releaseQuota();
    const meter = page.getByRole("button", { name: /账户额度：.*每周额度剩余94%/ });
    await expect(meter).toBeVisible();
    await expect(meter.locator(".usage-meter-line")).toHaveCount(plan === "Starter" ? 1 : 2);
    await press(meter);
    const panel = page.getByRole("region", { name: "Gemini 账户额度" });
    await expect(panel.getByText(plan, { exact: true })).toBeVisible();
    await expect(panel.getByRole("button", { name: "刷新", exact: true })).toHaveCount(0);
    const groups = panel.locator(".usage-pop-group");
    await expect(groups.first().getByText("剩余 94%")).toBeVisible();
    await expect(groups.first().getByText("5 小时额度", { exact: true })).toHaveCount(plan === "Starter" ? 0 : 1);
    await expect(groups.last().getByText("剩余 100%")).toHaveCount(2);
    await expect(groups.last().locator(".usage-pop-window small")).toHaveCount(0);
  });
}

test("Gemini uses native model, effort, permission and slash options on desktop and mobile", async ({ page, hasTouch }, info) => {
  const relay = await mockRelay(page, true);
  if (!hasTouch) await page.setViewportSize({ width: 1280, height: 800 });
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  await expect.poll(() => relay.seen.some(cmd => cmd.type === "hello")).toBe(true);
  await page.getByRole("button", { name: "切换新会话引擎" }).click();
  await page.getByRole("menuitemradio", { name: "Gemini" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-engine", "antigravity");
  await expect(page.locator(".engine-toggle")).toContainText("Gemini");
  await expect(page.locator(".engine-toggle img.antigravity")).toBeVisible();
  await expect(page.locator(".brand-mark img.antigravity")).toHaveCount(1);
  await expect(page.locator(".space-switch [role=tab]").first()).toBeDisabled();
  const newComposer = page.locator(".newchat-input");
  if (!(await newComposer.isVisible())) {
    const newButton = page.getByRole("button", { name: "新会话", exact: true });
    if (!(await newButton.isVisible())) await page.getByRole("button", { name: "打开会话列表" }).click();
    await newButton.click();
  }
  await expect(page.getByTitle("选择模型", { exact: true })).toContainText("Gemini 4 Neon (High)");
  await expect(page.getByTitle("选择思考强度", { exact: true })).toHaveCount(0);
  await page.getByTitle("选择模型", { exact: true }).click();
  await page.getByRole("button", { name: "Gemini Pro", exact: true }).click();
  await page.getByTitle("选择思考强度", { exact: true }).click();
  await page.getByRole("button", { name: "Extra", exact: true }).click();
  await page.getByTitle("选择权限模式", { exact: true }).click();
  await page.getByRole("button", { name: "Plan", exact: true }).click();
  await newComposer.fill("/comp");
  await expect(page.getByRole("listbox", { name: "命令" }).getByText("/compact", { exact: true })).toBeVisible();
  const send = page.getByRole("button", { name: "开始", exact: true });
  await newComposer.fill("/goal");
  if (hasTouch) await send.tap();
  else await send.click();
  await expect(page.getByText("请在 /goal 后填写目标。")).toBeVisible();
  await expect(newComposer).toHaveValue("/goal");
  expect(relay.seen.filter(cmd => cmd.type === "new_session")).toHaveLength(0);
  await newComposer.fill("/compact with summary");
  if (hasTouch) await send.tap();
  else await send.click();
  await expect.poll(() => relay.seen.find(cmd => cmd.type === "new_session")).toMatchObject({
    engine: "antigravity", model: "pro", antigravity_effort: "extra", antigravity_mode: "plan",
    prompt: "/compact with summary",
  });
  await expect(page.getByText("Gemini 已就绪。")).toBeVisible();
  await expect(page.locator(".composer .inrow")).toHaveCSS("background-image", /radial-gradient/);
  expect(relay.seen.some(cmd => cmd.engine === "antigravity" && cmd.space === "work")).toBe(false);

  relay.ask();
  await expect(page.locator(".qa-question")).toContainText("/tmp/fixture/note.txt");
  await expect(page.locator(".qa-approval-diff pre")).toBeVisible();
  await expect(page.locator(".qa-approval-diff pre")).toContainText("-before\n+after");
  await page.getByRole("button", { name: "Allow once" }).click();
  expect(relay.seen.some(cmd => cmd.type === "answer_question")).toBe(false);
  await page.getByRole("button", { name: "提交回答", exact: true }).click();
  await expect.poll(() => relay.seen.find(cmd => cmd.type === "answer_question")).toMatchObject({
    ask_id: "agy-approval", answers: ["Allow once"],
  });
  await expect(page.locator(".question-dialog")).toHaveCount(0);
  relay.draining();
  await page.getByRole("button", { name: "强制停止 Gemini" }).click();
  await expect.poll(() => relay.seen.find(cmd => cmd.type === "interrupt")).toMatchObject({ sid });
  expect(errors).toEqual([]);
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 800 });
    if (width === 1280) {
      await page.getByRole("button", { name: "显示会话侧栏" }).click();
      await expect(page.locator(".brand-mark img.antigravity")).toBeVisible();
      const card = page.locator(".scard").filter({ hasText: "Gemini session" });
      const menuButton = card.getByRole("button", { name: "更多操作" });
      if (hasTouch) await menuButton.tap();
      else await menuButton.click();
      await page.getByRole("button", { name: "隐藏会话", exact: true }).click();
      await expect(card).toHaveCount(0);
      await page.getByRole("button", { name: /已隐藏/ }).click();
      await expect(menuButton).toHaveAttribute("aria-expanded", "false");
      if (hasTouch) await menuButton.tap();
      else await menuButton.click();
      await page.getByRole("button", { name: "恢复显示", exact: true }).click();
      await expect(page.getByRole("button", { name: /已隐藏/ })).toHaveCount(0);
      if (hasTouch) await menuButton.tap();
      else await menuButton.click();
      await page.getByRole("button", { name: "重命名", exact: true }).click();
      const rename = page.locator(".scard input");
      await rename.fill("Gemini renamed");
      await rename.press("Enter");
      await expect(page.locator(".scard").filter({ hasText: "Gemini renamed" })).toBeVisible();
    } else {
      await page.getByRole("button", { name: "cc-remote，收起会话列表" }).click();
    }
    await expect(page.locator(".engine-toggle")).toBeVisible();
    expect(await page.locator(".composer").evaluate(node => node.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`gemini-${width}.png`), animations: "disabled" });
  }
});
