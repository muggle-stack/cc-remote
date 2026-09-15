import { expect, test, type Page } from "@playwright/test";

const url = "/tests/history-browser.html?background-tasks=1";
const trigger = (page: Page) => page.getByRole("button", { name: /后台任务，\d+ 项进行中/ });
const dialog = (page: Page) => page.getByRole("dialog", { name: "后台任务", exact: true });

test("background tasks keep the composer compact and open readable details", async ({ page }, testInfo) => {
  await page.goto(url);
  const chip = trigger(page);
  await expect(chip).toHaveText("后台任务 · 2");
  await expect(dialog(page)).toHaveCount(0);
  const before = await page.locator(".thread-shell").boundingBox();
  const chipBox = (await chip.boundingBox())!;
  const modeBox = (await page.locator(".runbar .seg").boundingBox())!;
  expect(Math.abs(chipBox.y + chipBox.height / 2 - modeBox.y - modeBox.height / 2)).toBeLessThan(2);
  expect(chipBox.x + chipBox.width).toBeLessThan(modeBox.x);
  expect(chipBox.height).toBeLessThanOrEqual(40);
  await chip.click();
  const panel = dialog(page);
  await expect(panel).toBeVisible();
  await expect(panel.getByText("检查构建进度和板载温度", { exact: true })).toBeVisible();
  await expect(panel.getByText(/运行中 · 2分/)).toBeVisible();
  if ((page.viewportSize()?.width ?? 0) <= 600) {
    await page.keyboard.press("Tab");
    await expect(panel.getByRole("button", { name: "关闭后台任务" })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(panel.locator("summary")).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(panel.getByRole("button", { name: /核对构建产物/ })).toBeFocused();
  }
  await panel.locator("summary").click();
  await expect(panel.getByText("$ make verify", { exact: true })).toBeVisible();
  const after = await page.locator(".thread-shell").boundingBox();
  expect(after).toEqual(before);
  const panelBox = (await panel.boundingBox())!;
  const mobile = (page.viewportSize()?.width ?? 0) <= 600;
  if (mobile) {
    await expect(panel).toHaveAttribute("aria-modal", "true");
    expect(Math.abs(panelBox.x + panelBox.width / 2 - page.viewportSize()!.width / 2)).toBeLessThan(2);
    const viewport = await page.locator(".thread-shell").boundingBox();
    const visualCenter = await page.evaluate(() => (window.visualViewport?.offsetTop ?? 0)
      + (window.visualViewport?.height ?? window.innerHeight) / 2);
    const center = panelBox.y + panelBox.height / 2;
    // Short chat areas may use the full visible viewport to keep details usable.
    expect(Math.min(Math.abs(center - viewport!.y - viewport!.height / 2),
      Math.abs(center - visualCenter))).toBeLessThan(2);
  } else {
    await expect(panel).toHaveAttribute("data-placement", "above");
    expect(panelBox.y + panelBox.height).toBeLessThan(chipBox.y);
    expect(Math.abs(panelBox.x - chipBox.x)).toBeLessThan(2);
  }
  await page.screenshot({ path: testInfo.outputPath("background-tasks-open.png"), animations: "disabled" });
  await page.keyboard.press("Escape");
  await expect(panel).toHaveCount(0);
  await expect(chip).toBeFocused();
});

test("background tasks follow native terminal events and empty snapshots", async ({ page }) => {
  await page.goto(url);
  await page.getByRole("button", { name: "回复结束", exact: true }).click();
  await expect(page.locator(".runbar .seg")).toHaveCount(0);
  await expect(trigger(page)).toHaveText("后台任务 · 2");
  await trigger(page).click();
  // Native updates can arrive while a modal is open, without a user click.
  await page.getByRole("button", { name: "构建完成", exact: true }).evaluate(button => (button as HTMLButtonElement).click());
  await expect(trigger(page)).toHaveText("后台任务 · 1");
  await expect(dialog(page)).toBeVisible();
  await expect(dialog(page).getByText("检查构建进度和板载温度", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "代理完成", exact: true }).evaluate(button => (button as HTMLButtonElement).click());
  await expect(trigger(page)).toHaveCount(0);
  await expect(dialog(page)).toHaveCount(0);
  await expect(page.locator(".runbar")).toHaveCount(0);
  await expect(page.locator(".thread")).toContainText("构建还在后台运行");

  await page.getByRole("button", { name: "多个任务", exact: true }).click();
  await expect(trigger(page)).toHaveText("后台任务 · 12");
  await expect(dialog(page)).toHaveCount(0);
  await trigger(page).click();
  const list = dialog(page).locator(".background-task-list");
  expect(await list.evaluate(node => node.scrollHeight > node.clientHeight)).toBe(true);
  await page.getByRole("button", { name: "空快照", exact: true }).evaluate(button => (button as HTMLButtonElement).click());
  await expect(trigger(page)).toHaveCount(0);
  await expect(dialog(page)).toHaveCount(0);
});

test("background tasks preserve draft input and reset open state on session changes", async ({ page }) => {
  await page.goto(url);
  const input = page.locator(".inrow textarea");
  await input.fill("继续检查这次构建");
  await trigger(page).click();
  await dialog(page).getByRole("button", { name: "关闭后台任务", exact: true }).click();
  await expect(input).toHaveValue("继续检查这次构建");
  await trigger(page).click();
  await page.getByRole("button", { name: "切换会话", exact: true }).evaluate(button => (button as HTMLButtonElement).click());
  await expect(dialog(page)).toHaveCount(0);
  await expect(trigger(page)).toHaveText("后台任务 · 2");
  await trigger(page).click();
  await dialog(page).getByRole("button", { name: /核对构建产物/ }).click();
  await expect(page.getByTestId("background-opened")).toHaveText("agent");
  await expect(dialog(page)).toHaveCount(0);
});

test("background tasks stay within the mobile viewport after keyboard resize", async ({ page }) => {
  test.skip((page.viewportSize()?.width ?? 0) > 600, "Mobile keyboard layout");
  await page.goto(url);
  await page.getByRole("button", { name: "多个任务", exact: true }).click();
  await trigger(page).click();
  const originalHeight = (await dialog(page).boundingBox())!.height;
  const width = page.viewportSize()!.width;
  const height = page.viewportSize()!.height;
  await page.setViewportSize({ width, height: 380 });
  await expect.poll(async () => {
    const box = (await dialog(page).boundingBox())!;
    return box.y >= 0 && box.y + box.height <= 380;
  }).toBe(true);
  await page.setViewportSize({ width, height });
  await expect.poll(async () => (await dialog(page).boundingBox())!.height).toBeCloseTo(originalHeight, 0);
  await expect(dialog(page).locator(".background-task-list")).toBeVisible();
});

test("background tasks show a small desktop hover preview and honor reduced motion", async ({ page }) => {
  test.skip((page.viewportSize()?.width ?? 0) <= 600, "Desktop hover");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(url + "&theme=light");
  await trigger(page).hover();
  await expect(page.getByRole("tooltip")).toContainText("检查构建进度和板载温度");
  await expect(dialog(page)).toHaveCount(0);
  expect(await page.locator(".background-task-indicator").evaluate(node => getComputedStyle(node).animationName)).toBe("none");
  await trigger(page).click();
  await page.locator(".inrow textarea").click();
  await expect(dialog(page)).toHaveCount(0);
  await expect(page.locator(".inrow textarea")).toBeFocused();
});
