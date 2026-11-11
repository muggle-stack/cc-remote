import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/tests/header-feedback.html");
  await expect(page.locator(".hstat-label")).toHaveText("空闲");
});

test("header feedback includes all command warnings and preserves page refresh actions", async ({ page }) => {
  await page.clock.install();
  for (const [control, copy] of [["session failure", "当前操作不适用于这个会话。"],
    ["permission failure", "通知权限未开启，设置没有更改。"],
    ["page mismatch", "页面版本已更新，请刷新后重试。"]]) {
    await page.getByRole("button", { name: control, exact: true }).click();
    await expect(page.locator(".banner.show, .notice-stack")).toHaveCount(0);
    await page.clock.fastForward(60_000);
    await page.locator(".header-feedback").click();
    await expect(page.getByRole("dialog")).toContainText(copy);
    if (control === "page mismatch") {
      await page.getByRole("button", { name: "刷新页面", exact: true }).click();
      await expect(page.getByTestId("reload-count")).toHaveText("1");
    }
    await page.getByRole("button", { name: "知道了", exact: true }).click();
    await expect(page.locator(".header-feedback")).toHaveCount(0);
  }
  await page.getByRole("button", { name: "protocol mismatch", exact: true }).click();
  await expect(page.locator(".header-feedback")).toHaveText("版本不兼容");
  await page.locator(".header-feedback").click();
  await expect(page.getByRole("dialog")).toContainText("版本不兼容，请更新设备。");
  await page.getByRole("button", { name: "收起详情", exact: true }).click();
  await expect(page.locator(".header-feedback")).toHaveText("版本不兼容");
  await page.getByRole("button", { name: "protocol mismatch", exact: true }).click();
  await expect(page.locator(".header-feedback")).toHaveCount(0);
});

test("header feedback keeps native connection recovery compact and scoped", async ({ page }) => {
  await page.clock.install();
  await page.getByRole("button", { name: "engine connection", exact: true }).click();
  await page.clock.fastForward(7_900);
  await expect(page.locator(".header-feedback")).toHaveCount(0);
  await page.clock.fastForward(101);
  await expect(page.locator(".header-feedback")).toHaveText("连接未恢复");
  await page.locator(".header-feedback").click();
  await expect(page.getByRole("dialog")).toContainText("本机连接断开");
  await page.getByRole("button", { name: "收起详情", exact: true }).click();
  await expect(page.locator(".header-feedback")).toBeVisible();
  await page.getByRole("button", { name: "engine connection", exact: true }).click();
  await expect(page.locator(".header-feedback")).toHaveCount(0);
});

test("header feedback hides an outage through recovery and accepts a new outage", async ({ page }) => {
  const feedback = page.locator(".header-feedback");
  await page.getByRole("button", { name: "offline", exact: true }).click();
  await expect(feedback).toHaveText("设备离线");
  await expect(page.locator(".banner.show")).toHaveCount(0);
  await feedback.click();
  await page.getByRole("button", { name: "收起提示", exact: true }).click();
  await expect(feedback).toHaveCount(0);
  for (const name of ["offline", "reconnecting", "sync", "ready"]) {
    await page.getByRole("button", { name, exact: true }).click();
    await expect(feedback).toHaveCount(0);
  }
  await page.getByRole("button", { name: "offline", exact: true }).click();
  await expect(feedback).toHaveText("设备离线");
  await page.getByRole("button", { name: "sync", exact: true }).click();
  await expect(feedback).toHaveText("同步中");
  await page.getByRole("button", { name: "ready", exact: true }).click();
  await expect(feedback).toHaveText("✓已重连");
  await expect(page.locator(".hstat-label")).toHaveText("空闲");
});

test("header feedback success expires and a replay cannot resurrect an acknowledged receipt", async ({ page }) => {
  await page.clock.install();
  const feedback = page.locator(".header-feedback");
  await page.getByRole("button", { name: "undo", exact: true }).click();
  await expect(feedback).toHaveText("✓已撤销");
  await feedback.click();
  await expect(page.getByRole("dialog")).toContainText("对话：已恢复 · 代码：已恢复");
  await page.clock.fastForward(10_000);
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(feedback).toBeVisible();
  await page.clock.fastForward(6_001);
  await expect(feedback).toHaveCount(0);
  await page.getByRole("button", { name: "replay same undo", exact: true }).click();
  await expect(feedback).toHaveCount(0);
  await page.getByRole("button", { name: "session", exact: true }).click();
  await page.getByRole("button", { name: "session", exact: true }).click();
  await expect(feedback).toHaveCount(0);
  await page.getByRole("button", { name: "new undo", exact: true }).click();
  await expect(feedback).toHaveText("✓已撤销");
});

test("header feedback failure stays until acknowledged and identical later failures show again", async ({ page }) => {
  await page.clock.install();
  const feedback = page.locator(".header-feedback");
  await page.getByRole("button", { name: "fail", exact: true }).click();
  await page.clock.fastForward(60_000);
  await expect(feedback).toHaveText("操作未完成");
  await page.getByRole("button", { name: "session", exact: true }).click();
  await expect(feedback).toHaveCount(0);
  await page.getByRole("button", { name: "session", exact: true }).click();
  await expect(feedback).toHaveText("操作未完成");
  await feedback.click();
  await page.locator(".header-feedback-scrim").click({ position: { x: 4, y: 350 } });
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(feedback).toHaveText("操作未完成");
  await feedback.click();
  await page.getByRole("button", { name: "知道了", exact: true }).click();
  await expect(feedback).toHaveCount(0);
  await page.getByRole("button", { name: "fail", exact: true }).click();
  await expect(feedback).toHaveText("操作未完成");
  await page.getByRole("button", { name: "other exception", exact: true }).click();
  await expect(page.locator(".notice-stack, .banner.show")).toHaveCount(0);
  await feedback.click();
  await expect(page.getByRole("dialog")).toContainText("压缩未完成");
});

test("header feedback shows checkpoint limitations compactly and keeps acknowledged replays hidden", async ({ page }) => {
  await page.clock.install();
  const feedback = page.locator(".header-feedback");
  await page.getByRole("button", { name: "checkpoint", exact: true }).click();
  await page.clock.fastForward(60_000);
  await expect(feedback).toHaveText("回滚受限");
  await expect(page.locator(".notice-stack, .banner.show")).toHaveCount(0);
  await expect(page.locator(".hstat-label")).toHaveText("空闲");
  await feedback.click();
  const dialog = page.getByRole("dialog", { name: "状态详情" });
  await expect(dialog).toContainText("本轮修改了 Git 暂存区或包含当前无法安全记录的文件；对话仍可正常回滚。");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(feedback).toHaveText("回滚受限");
  await feedback.click();
  await page.getByRole("button", { name: "知道了", exact: true }).click();
  await expect(feedback).toHaveCount(0);
  for (const name of ["offline", "reconnecting", "sync", "replay same checkpoint", "ready"])
    await page.getByRole("button", { name, exact: true }).click();
  await page.clock.fastForward(6_001);
  await expect(feedback).toHaveCount(0);
  await page.getByRole("button", { name: "session", exact: true }).click();
  await page.getByRole("button", { name: "session", exact: true }).click();
  await expect(feedback).toHaveCount(0);
  await expect(page.locator(".notice-stack, .banner.show")).toHaveCount(0);
  await page.getByRole("button", { name: "new checkpoint", exact: true }).click();
  await expect(feedback).toHaveText("回滚受限");
});

test("header feedback remains scoped and exposes simultaneous feedback without losing it", async ({ page }) => {
  await page.getByRole("button", { name: "undo", exact: true }).click();
  await page.locator(".header-feedback").click();
  // A real focus change must close the popover even when triggered outside its scrim.
  await page.getByRole("button", { name: "session", exact: true }).evaluate((button: HTMLButtonElement) => button.click());
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.locator(".header-feedback")).toHaveCount(0);
  await page.getByRole("button", { name: "session", exact: true }).click();
  await page.getByRole("button", { name: "offline", exact: true }).click();
  await page.getByRole("button", { name: "fail", exact: true }).click();
  await page.locator(".header-feedback").click();
  await expect(page.getByRole("dialog")).toContainText("已撤销");
  await expect(page.getByRole("dialog")).toContainText("操作未完成");
  await expect(page.getByRole("dialog")).toContainText("设备离线");
  await expect(page.getByRole("button", { name: "收起提示" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "sync", exact: true }).click();
  await page.getByRole("button", { name: "ready", exact: true }).click();
  await expect(page.locator(".header-feedback")).toHaveText("操作未完成");
});

test("header feedback keeps idle fixed and all controls reachable on narrow screens", async ({ page }, testInfo) => {
  for (const width of [360, 390, 1280]) {
    await page.setViewportSize({ width, height: 844 });
    await page.locator(".c-head").evaluate(async el => {
      await document.fonts.ready;
      // Terminal/menu controls also animate width across the mobile breakpoint.
      // Measure notification movement only after those finite resize transitions.
      await Promise.all(el.getAnimations({ subtree: true })
        .filter(animation => Number.isFinite(animation.effect?.getComputedTiming().endTime))
        .map(animation => animation.finished));
    });
    const before = await page.locator(".hstat").boundingBox();
    await page.getByRole("button", { name: "fail", exact: true }).click();
    const after = await page.locator(".hstat").boundingBox();
    expect(after?.x).toBe(before?.x);
    for (const selector of [".header-feedback", ".hstat", ".terminal-control-trigger", ".device-trigger", ".engine-toggle", ".c-head .iconbtn"]) {
      const rect = await page.locator(selector).boundingBox();
      expect(rect!.x).toBeGreaterThanOrEqual(0);
      expect(rect!.x + rect!.width).toBeLessThanOrEqual(width);
    }
    const overflow = await page.locator(".c-head").evaluate(el => el.scrollWidth > el.clientWidth);
    expect(overflow).toBe(false);
    await expect(page.locator(".engine-name")).toBeVisible();
    await page.getByRole("button", { name: "running", exact: true }).click();
    expect(await page.locator(".c-head").evaluate(el => el.scrollWidth > el.clientWidth)).toBe(false);
    await page.getByRole("button", { name: "running", exact: true }).click();
    await page.locator(".header-feedback").click();
    await page.getByRole("button", { name: "知道了", exact: true }).click();
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "long failure", exact: true }).click();
  await page.locator(".header-feedback").click();
  const dialog = page.getByRole("dialog");
  const rect = await dialog.boundingBox();
  expect(rect!.x).toBeGreaterThanOrEqual(0);
  expect(rect!.x + rect!.width).toBeLessThanOrEqual(390);
  expect(rect!.y + rect!.height).toBeLessThanOrEqual(844);
  await page.getByRole("button", { name: "知道了", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await page.getByRole("button", { name: "offline", exact: true }).click();
  await page.locator(".header-feedback").click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("dialog").evaluate(async el => {
    await Promise.all(el.getAnimations().map(animation => animation.finished));
  });
  await page.screenshot({ path: testInfo.outputPath("header-feedback-mobile.png") });
});
