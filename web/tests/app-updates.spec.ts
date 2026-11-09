import { expect, test, type Page } from "@playwright/test";
const url = "/tests/app-updates.html";
const base = { managed: true, enabled: true, allow_relay_update: true, current: "5.3.14", relay_version: "5.3.14",
  busy_count: 2, latest: { version: "5.3.15", notes: "## 中文\n\n- 支持应用内更新。\n- 修复 Claude 历史详情。\n- 优化旧版本清理。\n\n## English\n\n- In-app updates." }, job: null };
async function open(page: Page) {
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  await page.getByRole("button", { name: /^检查更新/ }).click();
  await expect(page.getByRole("dialog", { name: "检查更新", exact: true })).toBeVisible();
}

test("application updates preserve drafts, reattach to one job and fit mobile", async ({ page }, info) => {
  let job: Record<string, unknown> | null = null;
  const starts: string[] = [];
  await page.emulateMedia({ colorScheme: "dark" });
  await page.route("**/api/updates/*", async route => {
    const body = route.request().postDataJSON();
    if (body?.op === "start") {
      starts.push(body.job_id);
      job = { id: body.job_id, version: body.version, previous: "5.3.14", phase: "waiting" };
      await route.fulfill({ json: { ok: true, job } });
    } else if (body?.op === "cancel") {
      job = { ...job, phase: "cancelled" };
      await route.fulfill({ json: { ok: true, job } });
    } else await route.fulfill({ json: { ...base, job } });
  });
  await page.goto(url);
  await page.getByRole("textbox", { name: "输入消息" }).fill("这段草稿必须保留");
  await open(page);
  const sheet = page.getByRole("dialog", { name: "检查更新", exact: true });
  await expect(sheet.getByText("发现新版本", { exact: true })).toBeVisible();
  const bounds = await sheet.boundingBox();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(page.viewportSize()!.width);
  expect(await sheet.locator(".app-update-body").evaluate(node => parseFloat(getComputedStyle(node).paddingLeft))).toBeGreaterThanOrEqual(16);
  await expect(sheet.getByRole("button", { name: "收起更新窗口" })).toBeVisible();
  const install = sheet.getByRole("button", { name: "任务结束后更新", exact: true });
  expect(await install.evaluate(node => parseFloat(getComputedStyle(node).borderRadius))).toBeGreaterThan(20);
  await page.screenshot({ path: info.outputPath("update-sheet.png"), animations: "disabled" });
  await install.click();
  await expect(sheet.getByText("等待任务结束", { exact: true }).first()).toBeVisible();
  await sheet.getByRole("button", { name: "收起，后台继续", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "输入消息" })).toHaveValue("这段草稿必须保留");
  await open(page);
  await expect(sheet.getByText("等待任务结束", { exact: true }).first()).toBeVisible();
  expect(starts).toHaveLength(1);
  await sheet.getByRole("button", { name: "取消更新", exact: true }).click();
  await expect(sheet.getByRole("button", { name: "任务结束后更新", exact: true })).toBeEnabled();
});

test("application update retry after lost response reuses durable identity across reload", async ({ page }) => {
  const starts: string[] = [];
  await page.route("**/api/updates/*", async route => {
    const body = route.request().postDataJSON();
    if (body?.op === "start") {
      starts.push(body.job_id);
      if (starts.length === 1) await route.abort("failed");
      else await route.fulfill({ json: { ok: true, job: { id: body.job_id, version: body.version, previous: "5.3.14", phase: "waiting" } } });
    } else await route.fulfill({ json: base });
  });
  await page.goto(url); await open(page);
  await page.getByRole("button", { name: "任务结束后更新", exact: true }).click();
  await expect(page.getByRole("button", { name: "确认原更新", exact: true })).toBeEnabled();
  await page.reload(); await open(page);
  await page.getByRole("button", { name: "确认原更新", exact: true }).click();
  expect(starts).toHaveLength(2);
  expect(starts[1]).toBe(starts[0]);
});

test("application update response from a previous device cannot replace current device", async ({ page }) => {
  let release = () => {};
  const wait = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/updates/*", async route => {
    if (route.request().url().endsWith("device-a")) {
      await wait;
      await route.fulfill({ json: base }).catch(() => {});
    } else await route.fulfill({ json: { ...base, current: "5.3.15" } });
  });
  await page.goto(url);
  await page.getByRole("button", { name: "切换测试设备" }).click();
  await open(page); release();
  await expect(page.getByText("已是最新版本", { exact: true })).toBeVisible();
  await expect(page.getByText("发现新版本", { exact: true })).toHaveCount(0);
});

test("application update unresolved Relay keeps installation disabled across reload", async ({ page }) => {
  const commands: string[] = [];
  await page.route("**/api/updates/*", async route => {
    const body = route.request().postDataJSON();
    if (body?.op) commands.push(body.op);
    await route.fulfill({ json: { ...base, busy_count: 0, job: {
      id: "a".repeat(32), version: "5.3.15", previous: "5.3.14",
      phase: "unknown", reason: "outcome_pending",
    } } });
  });
  await page.goto(url);
  for (let reload = 0; reload < 2; reload++) {
    if (reload) await page.reload();
    await open(page);
    const sheet = page.getByRole("dialog", { name: "检查更新", exact: true });
    await expect(sheet.getByRole("button", { name: /检查状态|重新检查更新/ })).toHaveCount(0);
    await expect(sheet).toContainText("不要重复创建更新");
    await expect(sheet.getByRole("button", { name: "取消更新", exact: true })).toHaveCount(0);
    await expect(sheet.getByRole("button", { name: "收起，后台继续", exact: true })).toBeEnabled();
  }
  expect(commands).not.toContain("start");
  expect(commands).not.toContain("cancel");
});

test("appearance contains global bold preference and updates menu dismisses outside", async ({ page }) => {
  await page.route("**/api/updates/*", route => route.fulfill({ json: base }));
  await page.goto(url);
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  await expect(page.getByRole("switch", { name: "加粗字体", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: /^外观/ }).click();
  const toggle = page.getByRole("switch", { name: "加粗字体", exact: true });
  await toggle.click();
  await expect(page.locator("html")).toHaveAttribute("data-bold-text", "true");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  await page.locator(".header-menu-scrim").click({ position: { x: 4, y: 4 } });
  await expect(page.getByRole("button", { name: /^检查更新/ })).toHaveCount(0);
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-bold-text", "true");
});

test("application update long notes fit a small viewport with footer visible", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 568 });
  await page.route("**/api/updates/*", route => route.fulfill({ json: {
    ...base, latest: { ...base.latest, notes: "一段完整的更新记录，包含功能和修复。\n".repeat(120) },
  } }));
  await page.goto(url); await open(page);
  const install = page.getByRole("button", { name: "任务结束后更新", exact: true });
  await expect(install).toBeInViewport();
  const box = await install.boundingBox();
  expect(box!.y + box!.height).toBeLessThan(568);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320);
});

test("login during installation defers recovery without an error banner and resumes current history", async ({ page }) => {
  let phase = "installing";
  let first = true;
  let releaseFirst = () => {};
  const firstRead = new Promise<void>(resolve => { releaseFirst = resolve; });
  const frames: Array<Record<string, unknown>> = [];
  let deliver: (data: string) => void = () => {};
  await page.routeWebSocket(/\/ws(?:\?|$)/, socket => {
    deliver = data => socket.send(data);
    socket.onMessage(raw => { frames.push(JSON.parse(String(raw))); });
  });
  await page.route("**/api/updates/*", async route => {
    if (first) {
      first = false; await firstRead;
      await route.fulfill({ json: { ...base, maintenance: false } });
    } else await route.fulfill({ json: { ...base, maintenance: phase !== "succeeded", job: {
      id: "c".repeat(32), version: "5.3.15", previous: "5.3.14", phase,
    } } });
  });
  await page.goto(url + "?maintenance=1");
  await expect.poll(() => frames.some(frame => frame.type === "switch_session")).toBeTruthy();
  const draft = page.getByRole("textbox", { name: "输入消息" });
  await draft.fill("更新期间保留这段草稿");
  deliver(JSON.stringify({ v: 90, ts: 1, type: "error", code: "update_in_progress", sid: "session-a",
    message: "设备正在安装更新，请在重新连接后继续。" }));
  await expect(draft).toBeDisabled();
  await open(page);
  const sheet = page.getByRole("dialog", { name: "检查更新", exact: true });
  await expect(sheet.getByText("正在确认更新进度", { exact: true })).toBeVisible();
  await expect(sheet.getByRole("button", { name: /更新到|任务结束后更新|刷新页面/ })).toHaveCount(0);
  await sheet.getByRole("button", { name: "收起更新窗口", exact: true }).click();
  releaseFirst();
  await expect(page.getByRole("status").first()).toHaveText("正在更新");
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.getByRole("button", { name: "查看另一条历史" }).click();
  await expect.poll(() => frames.some(frame => frame.type === "get_history" && frame.session_id === "session-b")).toBeTruthy();
  expect(frames.filter(frame => frame.type === "switch_session" && frame.session_id === "session-b")).toHaveLength(0);
  phase = "succeeded";
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(draft).toBeEnabled();
  await expect(draft).toHaveValue("更新期间保留这段草稿");
  await expect.poll(() => frames.filter(frame => frame.type === "switch_session" && frame.session_id === "session-b").length).toBe(1);
  deliver(JSON.stringify({ v: 90, ts: 2, type: "error", code: "busy", message: "ordinary busy" }));
  await expect(page.getByRole("alert")).toHaveText("当前操作暂时无法执行，请稍后重试。");
});

test("completed installation keeps page refresh separate and folds release details", async ({ page }, info) => {
  await page.route("**/api/updates/*", route => route.fulfill({ json: {
    ...base, current: "5.3.15", relay_version: "5.3.15", busy_count: 0, maintenance: false,
    latest: { version: "5.3.15", notes: "## 中文\n\n- 支持应用内更新。\n\n## English\n\n- Keep drafts intact." },
    job: { id: "d".repeat(32), version: "5.3.15", previous: "5.3.14", phase: "succeeded", cleanup_pending: true },
  } }));
  await page.goto(url + "?pageVersion=5.3.15"); await open(page);
  const sheet = page.getByRole("dialog", { name: "检查更新" });
  await expect(sheet.getByText("刷新页面即可使用新版", { exact: true })).toBeVisible();
  await expect(sheet.getByRole("button", { name: "刷新页面", exact: true })).toBeEnabled();
  await expect(sheet.getByText(/旧版本备份|旧备份/)).toHaveCount(0);
  await expect(sheet.getByText("Keep drafts intact.", { exact: true })).not.toBeVisible();
  await page.screenshot({ path: info.outputPath("update-completed.png"), animations: "disabled" });
  await sheet.getByText("版本与更新记录", { exact: true }).click();
  await expect(sheet.getByText("Keep drafts intact.", { exact: true })).toBeVisible();
  await expect(sheet.getByRole("button", { name: /检查状态|重新检查更新/ })).toHaveCount(0);
});

test("reopening updates checks the catalog even when a background status read is pending", async ({ page }) => {
  await page.clock.install();
  let checks = 0, statusPending = false;
  let releaseStatus = () => {};
  const statusWait = new Promise<void>(resolve => { releaseStatus = resolve; });
  await page.route("**/api/updates/*", async route => {
    const body = route.request().postDataJSON();
    if (body?.op === "check") {
      checks++;
      await route.fulfill({ json: { ...base, busy_count: 0,
        latest: { ...base.latest, version: checks >= 3 ? "5.3.16" : "5.3.15" } } });
    } else {
      statusPending = true; await statusWait;
      await route.fulfill({ json: base });
    }
  });
  await page.goto(url);
  await expect.poll(() => checks).toBe(1);
  await expect(page.getByRole("status").first()).toHaveText("有更新");
  await open(page);
  const sheet = page.getByRole("dialog", { name: "检查更新" });
  await expect.poll(() => checks).toBe(2);
  await expect(sheet.getByRole("button", { name: "更新到 v5.3.15" })).toBeEnabled();
  await page.clock.fastForward(5000);
  await expect.poll(() => statusPending).toBeTruthy();
  await sheet.getByRole("button", { name: "收起更新窗口" }).click();
  await open(page);
  await expect(sheet.getByRole("button", { name: "更新到 v5.3.15" })).toBeDisabled();
  releaseStatus();
  await expect.poll(() => checks).toBe(3);
  await expect(sheet.getByRole("button", { name: "更新到 v5.3.16" })).toBeEnabled();
});

test("installation has one collapse action and no repeat-install or cancellation controls", async ({ page }, info) => {
  await page.route("**/api/updates/*", route => route.fulfill({ json: {
    ...base, relay_version: "5.3.16", maintenance: true, job: {
      id: "e".repeat(32), version: "5.3.16", previous: "5.3.15", phase: "installing",
    },
  } }));
  await page.goto(url); await open(page);
  const sheet = page.getByRole("dialog", { name: "检查更新" });
  await expect(sheet.getByText("正在安装 device-a 更新", { exact: true })).toBeVisible();
  await expect(sheet.getByRole("button", { name: "取消更新", exact: true })).toHaveCount(0);
  await expect(sheet.getByRole("button", { name: /更新到 v/ })).toHaveCount(0);
  await expect(sheet.getByRole("button", { name: "收起，后台继续", exact: true })).toBeInViewport();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.screenshot({ path: info.outputPath("update-installing.png"), animations: "disabled" });
  await sheet.getByRole("button", { name: "收起，后台继续", exact: true }).click();
  await page.reload(); await open(page);
  await expect(sheet.getByText("正在安装 device-a 更新", { exact: true })).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
});
