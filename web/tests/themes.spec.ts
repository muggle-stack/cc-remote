import { expect, test, type Page } from "@playwright/test";

const url = "/tests/theme-preview.html?engine=codex";
const picker = (page: Page) => page.getByRole("dialog", { name: "主题", exact: true });
async function openPicker(page: Page) {
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  await page.getByRole("button", { name: /^主题/ }).click();
  await expect(picker(page)).toBeVisible();
}

test("theme picker applies all six palettes without losing the draft and restores classic colors", async ({ page }, testInfo) => {
  await page.goto(url);
  const root = page.locator("html");
  const input = page.locator(".inrow textarea");
  await input.fill("这条消息先保留");
  await openPicker(page);
  await expect(picker(page).locator(".theme-card")).toHaveCount(6);
  for (const [name, id, mode] of [
    ["奶油琥珀", "amber", "light"], ["森林苔绿", "moss", "light"], ["玫瑰雾", "rose", "light"],
    ["深海青", "lagoon", "dark"], ["酒红夜色", "bordeaux", "dark"], ["复古终端绿", "heritage", "dark"],
  ]) {
    await picker(page).getByRole("button", { name, exact: true }).click();
    await expect(root).toHaveAttribute("data-palette", id);
    await expect(root).toHaveAttribute("data-theme", mode);
    await expect(input).toHaveValue("这条消息先保留");
    await expect(picker(page).getByRole("status")).toHaveText(`已选：${name}`);
    expect(await root.evaluate(node => getComputedStyle(node).colorScheme)).toBe(mode);
    const miniature = picker(page).getByRole("button", { name, exact: true }).locator(".theme-miniature");
    expect(await page.locator(".pane").evaluate(node => getComputedStyle(node).backgroundColor))
      .toBe(await miniature.evaluate(node => getComputedStyle(node).backgroundColor));
  }
  await page.screenshot({ path: testInfo.outputPath("theme-picker.png"), animations: "disabled" });
  await picker(page).getByRole("button", { name: "经典浅色", exact: true }).click();
  await expect(root).toHaveAttribute("data-palette", "classic");
  expect(await root.evaluate(node => (node as HTMLElement).style.getPropertyValue("--bg"))).toBe("");
  await page.keyboard.press("Escape");
  await expect(picker(page)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "更多设置", exact: true })).toBeFocused();
  await expect(input).toHaveValue("这条消息先保留");
});

test("theme picker remembers each engine and survives a reload", async ({ page }) => {
  await page.goto(url);
  await openPicker(page);
  await picker(page).getByRole("button", { name: "森林苔绿", exact: true }).click();
  await picker(page).getByRole("button", { name: "完成", exact: true }).click();
  await page.getByRole("button", { name: "切换新会话引擎" }).click();
  await page.getByRole("menuitemradio", { name: "Claude" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-palette", "classic");
  await openPicker(page);
  await picker(page).getByRole("button", { name: "酒红夜色", exact: true }).click();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "切换新会话引擎" }).click();
  await page.getByRole("menuitemradio", { name: "Codex" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-palette", "moss");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-palette", "moss");
  const preferences = await page.evaluate(() => JSON.parse(localStorage.getItem("cc_remote_themes_v1")!));
  expect(preferences).toMatchObject({ codex: "moss", claude: "bordeaux" });
});

test("theme picker follows the system only when selected and preserves old preferences", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.addInitScript(() => localStorage.setItem("cc_remote_theme", "dark"));
  await page.goto(url);
  const root = page.locator("html");
  await expect(root).toHaveAttribute("data-theme", "dark");
  await openPicker(page);
  await expect(picker(page).getByRole("button", { name: "经典深色", exact: true })).toHaveAttribute("aria-pressed", "true");
  await picker(page).getByRole("button", { name: "跟随系统", exact: true }).click();
  await expect(root).toHaveAttribute("data-theme", "light");
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(root).toHaveAttribute("data-theme", "dark");
  await picker(page).getByRole("button", { name: "奶油琥珀", exact: true }).click();
  await page.emulateMedia({ colorScheme: "light" });
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(root).toHaveAttribute("data-theme", "light");
  expect(await page.evaluate(() => localStorage.getItem("cc_remote_theme"))).toBe("dark");
});

test("theme picker stays centered and scrollable when the mobile viewport shrinks", async ({ page }, testInfo) => {
  test.skip((page.viewportSize()?.width ?? 0) > 600, "Mobile viewport");
  await page.goto(url);
  await openPicker(page);
  const panel = picker(page);
  const size = page.viewportSize()!;
  const initial = (await panel.boundingBox())!;
  expect(Math.abs(initial.x + initial.width / 2 - size.width / 2)).toBeLessThan(2);
  await expect(panel.locator("select")).toHaveCount(0);
  await page.setViewportSize({ width: size.width, height: 380 });
  await expect.poll(async () => {
    const box = (await panel.boundingBox())!;
    return box.y >= 0 && box.y + box.height <= 380;
  }).toBe(true);
  await panel.getByRole("button", { name: "复古终端绿", exact: true }).click();
  await expect(panel.getByRole("button", { name: "完成", exact: true })).toBeVisible();
  await page.setViewportSize(size);
  await expect.poll(async () => (await panel.boundingBox())!.height).toBeCloseTo(initial.height, 0);
  await page.screenshot({ path: testInfo.outputPath("mobile-theme-picker.png"), animations: "disabled" });
});

test("theme picker receives another tab's selection without changing other engines", async ({ page }) => {
  await page.goto(url);
  await openPicker(page);
  await picker(page).getByRole("button", { name: "森林苔绿", exact: true }).click();
  const other = await page.context().newPage();
  await other.goto("/tests/theme-preview.html?engine=claude");
  await openPicker(other);
  await picker(other).getByRole("button", { name: "玫瑰雾", exact: true }).click();
  await expect(page.locator("html")).toHaveAttribute("data-palette", "moss");
  await other.goto(url);
  await expect(other.locator("html")).toHaveAttribute("data-palette", "moss");
  await openPicker(other);
  await picker(other).getByRole("button", { name: "深海青", exact: true }).click();
  await expect(page.locator("html")).toHaveAttribute("data-palette", "lagoon");
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem("cc_remote_themes_v1")!)))
    .toMatchObject({ claude: "rose", codex: "lagoon" });
  await other.close();
});

test("theme picker restores DSH colors independently of Codex", async ({ page }) => {
  await page.goto("/tests/theme-preview.html?engine=dsh");
  await expect(page.locator("html")).toHaveAttribute("data-engine", /claude|dsh/);
  test.skip(await page.locator("html").getAttribute("data-engine") !== "dsh", "DSH branch only");
  await openPicker(page);
  await picker(page).getByRole("button", { name: "复古终端绿", exact: true }).click();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "切换新会话引擎" }).click();
  await page.getByRole("menuitemradio", { name: "Codex" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-palette", "classic");
  await page.getByRole("button", { name: "切换新会话引擎" }).click();
  await page.getByRole("menuitemradio", { name: "DSH" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-palette", "heritage");
});


const textSamples = ["body", ".prose p", ".prose h3", ".prose strong", ".prose pre code",
  ".scard-title", ".inrow textarea", ".c-head .ttl", ".header-menu-item small"];
async function sampleTypography(page: Page) {
  return Promise.all(textSamples.map(selector => page.locator(selector).first().evaluate(node => {
    const style = getComputedStyle(node);
    return { weight: Number(style.fontWeight), size: style.fontSize, family: style.fontFamily,
      stroke: parseFloat(style.webkitTextStrokeWidth) };
  })));
}

test("bold text preserves hierarchy and drafts across engines, themes and reloads", async ({ page }, info) => {
  await page.goto(url);
  const input = page.locator(".inrow textarea");
  await input.fill("保留草稿，调整阅读效果");
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  const toggle = page.getByRole("switch", { name: "加粗字体", exact: true });
  await expect(toggle).not.toBeChecked();
  const normal = await sampleTypography(page);
  await toggle.focus();
  await page.keyboard.press("Space");
  await expect(toggle).toBeChecked();
  await expect(page.locator("html")).toHaveAttribute("data-bold-text", "true");
  const bold = await sampleTypography(page);
  for (let index = 0; index < normal.length; index++) {
    const sample = bold[index];
    expect(sample.size, textSamples[index]).toBe(normal[index].size);
    expect(sample.family, textSamples[index]).toBe(normal[index].family);
    expect(sample.weight, textSamples[index]).toBeGreaterThan(normal[index].weight);
    expect(sample.weight, textSamples[index]).toBeGreaterThanOrEqual(700);
    expect(sample.stroke, textSamples[index]).toBeGreaterThan(0);
    expect(sample.stroke, textSamples[index]).toBeLessThanOrEqual(0.35);
    // Emphasis must remain distinguishable from ordinary body text.
    for (let other = 0; other < normal.length; other++) {
      if (normal[index].weight > normal[other].weight) {
        expect(sample.weight).toBeGreaterThan(bold[other].weight);
      }
    }
  }
  await expect(page.locator(".prose p").first()).toHaveCSS("font-weight", "700");
  await expect(page.locator(".c-head svg").first()).toHaveCSS("-webkit-text-stroke-width", "0px");
  await expect(input).toHaveValue("保留草稿，调整阅读效果");
  await page.screenshot({ path: info.outputPath("bold-text-menu.png"), animations: "disabled" });
  await page.keyboard.press("Escape");
  await openPicker(page);
  await picker(page).getByRole("button", { name: "森林苔绿", exact: true }).click();
  // Lazy sheets also inherit the same weight scale.
  await expect(picker(page).locator(".theme-card-name").first()).toHaveCSS("font-weight", "800");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "切换新会话引擎" }).click();
  await page.getByRole("menuitemradio", { name: "Claude" }).click();
  await expect(page.locator(".prose p").first()).toHaveCSS("font-weight", "700");
  await page.getByRole("button", { name: "切换新会话引擎" }).click();
  const dsh = page.getByRole("menuitemradio", { name: "DSH" });
  if (await dsh.count()) {
    await dsh.click();
    await expect(page.locator(".prose p").first()).toHaveCSS("font-weight", "700");
    await page.getByRole("button", { name: "切换新会话引擎" }).click();
  }
  await page.getByRole("menuitemradio", { name: "Codex" }).click();
  await expect(input).toHaveValue("保留草稿，调整阅读效果");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-bold-text", "true");
  await expect(page.locator("html")).toHaveAttribute("data-palette", "moss");
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  await expect(toggle).toBeChecked();
  await toggle.click();
  await expect(page.locator("html")).toHaveAttribute("data-bold-text", "false");
  expect(await sampleTypography(page)).toEqual(normal);
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-bold-text", "false");
});

test("bold text syncs between browser tabs and works without storage", async ({ page }) => {
  await page.goto(url);
  const other = await page.context().newPage();
  await other.goto("/tests/theme-preview.html?engine=claude");
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  await page.getByRole("switch", { name: "加粗字体", exact: true }).click();
  await expect(other.locator(".prose p").first()).toHaveCSS("font-weight", "700");
  await other.getByRole("button", { name: "更多设置", exact: true }).click();
  await expect(other.getByRole("switch", { name: "加粗字体", exact: true })).toBeChecked();
  await other.getByRole("switch", { name: "加粗字体", exact: true }).click();
  await expect(page.getByRole("switch", { name: "加粗字体", exact: true })).not.toBeChecked();
  await other.close();

  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript(() => Object.defineProperty(window, "localStorage", {
    get() { throw new DOMException("Storage unavailable", "SecurityError"); },
  }));
  await page.reload();
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  await page.getByRole("switch", { name: "加粗字体", exact: true }).click();
  await expect(page.locator(".prose p").first()).toHaveCSS("font-weight", "700");
  expect(errors).toEqual([]);
});
