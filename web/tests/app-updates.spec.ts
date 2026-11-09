import { expect, test, type Page } from "@playwright/test";
const url = "/tests/app-updates.html";
const base = { managed: true, enabled: true, allow_relay_update: true, current: "5.3.14", relay_version: "5.3.14",
  busy_count: 2, latest: { version: "5.3.15", notes: "更新入口与进度面板\n保留任务和草稿\n统一外观中的阅读设置" }, job: null };
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
  expect(await sheet.evaluate(node => parseFloat(getComputedStyle(node).paddingLeft))).toBeGreaterThanOrEqual(16);
  await expect(sheet.locator(".app-update-symbol svg path")).toHaveCount(1);
  const install = sheet.getByRole("button", { name: "任务结束后更新", exact: true });
  expect(await install.evaluate(node => parseFloat(getComputedStyle(node).borderRadius))).toBeGreaterThan(20);
  await page.screenshot({ path: info.outputPath("update-sheet.png"), animations: "disabled" });
  await install.click();
  await expect(sheet.getByText("等待任务结束", { exact: true }).first()).toBeVisible();
  await sheet.getByRole("button", { name: "收起", exact: true }).click();
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
