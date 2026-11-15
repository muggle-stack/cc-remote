import { expect, test, type Page } from "@playwright/test";
const url = "/tests/app-updates.html";
const base = { managed: true, enabled: true, allow_relay_update: true, current: "5.3.14", relay_version: "5.3.14",
  busy_count: 2, latest: { version: "5.3.15", notes: "## 中文\n\n- 支持应用内更新。\n- 修复 Claude 历史详情。\n- 优化旧版本清理。\n\n## English\n\n- In-app updates." }, job: null };
async function open(page: Page) {
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  await page.getByRole("button", { name: /^检查更新/ }).click();
  await expect(page.getByRole("dialog", { name: "检查更新", exact: true })).toBeVisible();
}

for (const phase of ["waiting_network", "waiting_device"] as const) {
  test(`application update ${phase} preserves its task and supports cancellation`, async ({ page }) => {
    let job: Record<string, unknown> | null = null;
    const starts: string[] = [];
    await page.route("**/api/updates/*", async route => {
      const body = route.request().postDataJSON();
      if (body?.op === "start") {
        starts.push(body.job_id);
        job = { id: body.job_id, version: body.version, previous: base.current, phase };
        await route.fulfill({ json: { ok: true, job } });
      } else if (body?.op === "cancel") {
        job = { ...job, phase: "cancelled" };
        await route.fulfill({ json: { ok: true, job } });
      } else await route.fulfill({ json: { ...base, online: phase !== "waiting_device", maintenance: false, job } });
    });
    await page.goto(url); await open(page);
    await page.getByRole("button", { name: "就绪后更新", exact: true }).click();
    const heading = phase === "waiting_network" ? "等待网络恢复" : "等待设备上线";
    await expect(page.getByRole("heading", { name: heading })).toBeVisible();
    await page.reload(); await open(page);
    await expect(page.getByRole("heading", { name: heading })).toBeVisible();
    await expect(page.getByRole("textbox", { name: "输入消息" })).toBeEnabled();
    expect(starts).toHaveLength(1);
    await page.getByRole("button", { name: "取消更新", exact: true }).click();
    await expect(page.getByRole("button", { name: "就绪后更新", exact: true })).toBeEnabled();
  });
}

test("device upgrade is available while an older server has separate update authorization", async ({ page }) => {
  await page.route("**/api/updates/*", route => route.fulfill({ json: {
    ...base, allow_relay_update: false, relay_version: "5.3.13", online: false,
  } }));
  await page.goto(url); await open(page);
  await expect(page.getByRole("button", { name: "就绪后更新", exact: true })).toBeEnabled();
  await expect(page.getByText("设备当前离线，确认后会保存更新请求，等待设备上线执行。", { exact: true })).toBeVisible();
});

test("independent device progress does not claim to update its server", async ({ page }) => {
  await page.route("**/api/updates/*", route => route.fulfill({ json: {
    ...base, allow_relay_update: false, relay_version: "5.3.13",
    job: { id: 'a'.repeat(32), previous: base.current, version: base.latest.version, phase: 'installing' },
  } }));
  await page.goto(url); await open(page);
  const steps = page.locator('.app-update-steps');
  await expect(steps.locator('li')).toHaveCount(2);
  await expect(steps.locator('li').first()).toContainText('安装中');
  await expect(steps).not.toContainText('服务器');
});

test("adopting the new updater clears an older protocol-blocked status", async ({ page }) => {
  await page.route("**/api/updates/*", route => route.fulfill({ json: {
    ...base, current: base.latest.version, busy_count: 0,
    job: { id: 'a'.repeat(32), previous: base.current, version: base.latest.version,
      phase: 'blocked', reason: 'protocol_change' },
  } }));
  await page.goto(url); await open(page);
  await expect(page.getByRole('heading', { name: '已是最新版本', exact: true })).toBeVisible();
  await expect(page.locator('.app-update-warning')).toHaveCount(0);
});

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
  const install = sheet.getByRole("button", { name: "就绪后更新", exact: true });
  expect(await install.evaluate(node => parseFloat(getComputedStyle(node).borderRadius))).toBeGreaterThan(20);
  await page.screenshot({ path: info.outputPath("update-sheet.png"), animations: "disabled" });
  await install.click();
  await expect(sheet.getByText("等待安全更新", { exact: true }).first()).toBeVisible();
  await sheet.getByRole("button", { name: "收起，后台继续", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "输入消息" })).toHaveValue("这段草稿必须保留");
  await open(page);
  await expect(sheet.getByText("等待安全更新", { exact: true }).first()).toBeVisible();
  expect(starts).toHaveLength(1);
  await sheet.getByRole("button", { name: "取消更新", exact: true }).click();
  await expect(sheet.getByRole("button", { name: "就绪后更新", exact: true })).toBeEnabled();
});

test("application update retry after lost response reuses durable identity across reload", async ({ page }) => {
  const starts: string[] = [];
  let job: Record<string, unknown> | null = { id: "b".repeat(32), version: "5.3.15", previous: "5.3.14", phase: "cancelled" };
  await page.route("**/api/updates/*", async route => {
    const body = route.request().postDataJSON();
    if (body?.op === "start") {
      starts.push(body.job_id);
      if (starts.length === 1) await route.abort("failed");
      else {
        job = { id: body.job_id, version: body.version, previous: "5.3.14", phase: "waiting" };
        await route.fulfill({ json: { ok: true, job } });
      }
    } else if (body?.op === "lookup") await route.fulfill({ json: { ok: true, job_id: body.job_id, receipt: "missing", job: null } });
    else await route.fulfill({ json: { ...base, job } });
  });
  await page.goto(url); await open(page);
  await page.getByRole("button", { name: "就绪后更新", exact: true }).click();
  await expect(page.getByRole("heading", { name: "更新尚未启动", exact: true })).toBeVisible();
  expect(starts).toHaveLength(1);
  await page.reload(); await open(page);
  await page.getByRole("button", { name: "重试更新", exact: true }).click();
  expect(starts).toHaveLength(2);
  expect(starts[1]).toBe(starts[0]);
  await expect(page.getByRole("heading", { name: "等待安全更新", exact: true })).toBeVisible();
});

test("update submission error survives successful polling and reload without automatic installation", async ({ page }) => {
  await page.clock.install();
  let starts = 0, lookups = 0;
  await page.route("**/api/updates/*", async route => {
    const body = route.request().postDataJSON();
    if (body?.op === "start") {
      starts++;
      await route.fulfill({ json: { ok: false, reason: "permission_required" } });
    } else if (body?.op === "lookup") {
      lookups++;
      await route.fulfill({ json: { ok: true, job_id: body.job_id, receipt: "missing", job: null } });
    } else await route.fulfill({ json: base });
  });
  await page.goto(url); await open(page);
  await page.getByRole("button", { name: "就绪后更新", exact: true }).click();
  await expect(page.getByRole("heading", { name: "更新尚未启动" })).toBeVisible();
  await expect(page.locator(".app-update-warning")).toContainText("升级权限");
  const before = lookups;
  await page.clock.fastForward(5000);
  await expect.poll(() => lookups).toBeGreaterThan(before);
  await expect(page.locator(".app-update-warning")).toContainText("升级权限");
  await page.reload(); await open(page);
  await expect(page.getByRole("button", { name: "重试更新", exact: true })).toBeEnabled();
  await expect(page.locator(".app-update-warning")).toContainText("升级权限");
  expect(starts).toBe(1);
});

test("original update receipt resolves a restored intent without replacing another active update", async ({ page }) => {
  const id = "a".repeat(32), other = "b".repeat(32);
  await page.addInitScript(({ id }) => localStorage.setItem("cc_remote_ui_update:device-a",
    JSON.stringify({ id, version: "5.3.15" })), { id });
  const commands: string[] = [];
  await page.route("**/api/updates/*", async route => {
    const body = route.request().postDataJSON(); commands.push(body?.op ?? "status");
    const job = { id: body?.op === "lookup" ? id : other, version: "5.3.15", previous: "5.3.14",
      phase: body?.op === "lookup" ? "cancelled" : "waiting" };
    await route.fulfill({ json: body?.op === "lookup"
      ? { ok: true, job_id: id, receipt: "found", job } : { ...base, job } });
  });
  await page.goto(url); await open(page);
  await expect(page.getByRole("heading", { name: "等待安全更新" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => localStorage.getItem("cc_remote_ui_update:device-a"))).toBeNull();
  expect(commands).toContain("lookup"); expect(commands).not.toContain("start");
  await expect(page.getByRole("button", { name: "取消更新", exact: true })).toBeEnabled();
});

for (const phase of ["waiting", "unknown"] as const) {
  test(`lost start acknowledgement restores exact ${phase} receipt without another start`, async ({ page }) => {
    let job: Record<string, unknown> | null = null, original: Record<string, unknown> | null = null;
    let starts = 0;
    await page.route("**/api/updates/*", async route => {
      const body = route.request().postDataJSON();
      if (body?.op === "start") {
        starts++;
        original = { id: body.job_id, version: body.version, previous: "5.3.14", phase };
        await route.abort("failed");
      } else if (body?.op === "lookup") {
        job = original;
        await route.fulfill({ json: { ok: true, job_id: body.job_id, receipt: "found", job } });
      } else await route.fulfill({ json: { ...base, job } });
    });
    await page.goto(url); await open(page);
    await page.getByRole("button", { name: "就绪后更新", exact: true }).click();
    await expect(page.getByRole("heading", { name: phase === "waiting" ? "等待安全更新" : "更新结果待确认" })).toBeVisible();
    expect(starts).toBe(1);
    expect(await page.evaluate(() => localStorage.getItem("cc_remote_ui_update:device-a"))).toBeNull();
    await expect(page.getByRole("button", { name: /重试更新|重新确认|更新到 v/ })).toHaveCount(0);
  });
}

for (const lookupFailure of ["offline", "wrong-receipt", "old-device"] as const) {
  test(`unconfirmed update handles ${lookupFailure} without clearing its identity or submitting automatically`, async ({ page }) => {
    const id = "a".repeat(32);
    await page.addInitScript(({ id }) => localStorage.setItem("cc_remote_ui_update:device-a",
      JSON.stringify({ id, version: "5.3.15" })), { id });
    const starts: string[] = [];
    await page.route("**/api/updates/*", async route => {
      const body = route.request().postDataJSON();
      if (body?.op === "lookup") {
        if (lookupFailure === "offline") await route.abort("failed");
        else if (lookupFailure === "old-device") await route.fulfill({ status: 400, json: { reason: "invalid_request" } });
        else await route.fulfill({ json: { ok: true, job_id: "b".repeat(32), receipt: "missing", job: null } });
      } else if (body?.op === "start") {
        starts.push(body.job_id);
        await route.fulfill({ json: { ok: true, job: { id, version: "5.3.15", previous: "5.3.14", phase: "waiting" } } });
      } else await route.fulfill({ json: base });
    });
    await page.goto(url); await open(page);
    await expect(page.getByRole("heading", { name: "更新请求待确认" })).toBeVisible();
    const action = page.getByRole("button", { name: lookupFailure === "old-device" ? "重试更新" : "重新确认", exact: true });
    await expect(action).toBeEnabled();
    expect(starts).toHaveLength(0);
    expect(JSON.parse((await page.evaluate(() => localStorage.getItem("cc_remote_ui_update:device-a")))!).id).toBe(id);
    await action.click();
    if (lookupFailure === "old-device") await expect.poll(() => starts).toEqual([id]);
    else { await expect(action).toBeEnabled(); expect(starts).toHaveLength(0); }
  });
}

test("late missing receipt cannot replace an acknowledged update after retry", async ({ page }) => {
  const id = "a".repeat(32);
  await page.addInitScript(({ id }) => localStorage.setItem("cc_remote_ui_update:device-a",
    JSON.stringify({ id, version: "5.3.15" })), { id });
  let release = () => {}, delay = false, waiting = false;
  const delayed = new Promise<void>(resolve => { release = resolve; });
  let job: Record<string, unknown> | null = null;
  const starts: string[] = [];
  await page.route("**/api/updates/*", async route => {
    const body = route.request().postDataJSON();
    if (body?.op === "lookup") {
      if (delay) { waiting = true; await delayed; }
      await route.fulfill({ json: { ok: true, job_id: id, receipt: "missing", job: null } });
    } else if (body?.op === "start") {
      starts.push(body.job_id);
      job = { id, version: "5.3.15", previous: "5.3.14", phase: "waiting" };
      await route.fulfill({ json: { ok: true, job } });
    } else await route.fulfill({ json: { ...base, job } });
  });
  await page.goto(url); await open(page);
  await expect(page.getByRole("button", { name: "重试更新", exact: true })).toBeEnabled();
  delay = true;
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect.poll(() => waiting).toBe(true);
  await page.getByRole("button", { name: "重试更新", exact: true }).click();
  await expect(page.getByRole("heading", { name: "等待安全更新" })).toBeVisible();
  release();
  await expect.poll(() => page.evaluate(() => localStorage.getItem("cc_remote_ui_update:device-a"))).toBeNull();
  await expect(page.getByRole("heading", { name: "等待安全更新" })).toBeVisible();
  expect(starts).toEqual([id]);
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
  const install = page.getByRole("button", { name: "就绪后更新", exact: true });
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
  await expect(sheet.getByRole("button", { name: /更新到|就绪后更新|刷新页面/ })).toHaveCount(0);
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
