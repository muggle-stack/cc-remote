import { expect, test, type Page } from "@playwright/test";

const url = "/tests/theme-preview.html?engine=codex";
const picker = (page: Page) => page.getByRole("dialog", { name: "外观", exact: true });
async function openPicker(page: Page) {
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  await page.getByRole("button", { name: /^外观/ }).click();
  await expect(picker(page)).toBeVisible();
}
const color = (page: Page, name: string) => picker(page).getByRole("textbox", { name: `${name} HEX` });
async function choosePalette(page: Page, name: string) {
  await picker(page).getByRole("combobox", { name: "配色方案", exact: true }).click();
  await picker(page).getByRole("option", { name, exact: true }).click();
}
const background = (page: Page) => page.locator(".pane").evaluate(node => getComputedStyle(node).backgroundColor);

test("session layout compact hides repeated paths in every engine including single-account DSH", async ({ page }) => {
  for (const engine of ["codex", "claude", "dsh"]) {
    await page.goto(`/tests/theme-preview.html?engine=${engine}`);
    // Each navigation retains this engine's own appearance settings.
    await expect(page.locator(".sessions")).toHaveAttribute("data-layout", "compact");
    await expect(page.locator(".scard-location, .scard-prev")).toHaveCount(0);
    if (engine === "dsh") await expect(page.locator(".scard-profile-dot")).toHaveCount(0);
    await openPicker(page);
    await picker(page).getByRole("button", { name: "舒展", exact: true }).click();
    await expect(page.locator(".scard-location")).toHaveCount(engine === "dsh" ? 5 : 3);
    await picker(page).getByRole("button", { name: "完成", exact: true }).click();
  }
});

test("session layout changes live and persists per engine across themes, reloads and tabs", async ({ page }) => {
  await page.goto(url);
  const sidebar = page.locator(".sessions");
  const card = sidebar.locator(".scard").first();
  await expect(sidebar).toHaveAttribute("data-layout", "compact");
  await expect(sidebar.locator(".scard-profile-dot")).toHaveCount(5);
  const compactHeight = await card.evaluate(node => node.getBoundingClientRect().height);
  await page.locator(".inrow textarea").fill("保留排布切换前的草稿");
  await openPicker(page);
  await expect(picker(page).getByRole("button", { name: "紧凑", exact: true })).toHaveAttribute("aria-pressed", "true");
  await picker(page).getByRole("button", { name: "舒展", exact: true }).click();
  await expect(sidebar).toHaveAttribute("data-layout", "comfortable");
  await expect(sidebar.locator(".scard-profile-ribbon")).toHaveCount(5);
  await expect(sidebar.locator(".scard-profile-dot")).toHaveCount(0);
  await expect(sidebar.locator(".scard-location")).toHaveCount(3);
  expect(await card.evaluate(node => node.getBoundingClientRect().height)).toBeGreaterThan(compactHeight);
  await expect(sidebar.locator('[aria-current="true"] .scard-title')).toHaveText("cc-remote");
  await expect(sidebar.locator(".pill.idle")).toHaveCount(0);
  await expect(page.locator(".inrow textarea")).toHaveValue("保留排布切换前的草稿");
  await picker(page).getByRole("button", { name: "深色", exact: true }).click();
  await picker(page).getByRole("button", { name: "恢复默认", exact: true }).click();
  await expect(sidebar).toHaveAttribute("data-layout", "comfortable");
  await page.reload();
  await expect(sidebar).toHaveAttribute("data-layout", "comfortable");
  await page.getByRole("button", { name: "切换新会话引擎" }).click();
  await page.getByRole("menuitemradio", { name: "Claude" }).click();
  await expect(sidebar).toHaveAttribute("data-layout", "compact");
  await page.getByRole("button", { name: "切换新会话引擎" }).click();
  await page.getByRole("menuitemradio", { name: "Codex" }).click();
  await expect(sidebar).toHaveAttribute("data-layout", "comfortable");
  const other = await page.context().newPage();
  await other.goto(url);
  await openPicker(other);
  await other.getByRole("button", { name: "紧凑", exact: true }).click();
  await expect(sidebar).toHaveAttribute("data-layout", "compact");
  await expect(sidebar.locator(".scard-location")).toHaveCount(0);
  await other.close();
});

test("session layout picker fits narrow screens and keeps both choices reachable", async ({ page }, info) => {
  await page.goto(url);
  for (const width of [320, 390, 1440]) {
    await page.setViewportSize({ width, height: width < 980 ? 740 : 1080 });
    await openPicker(page);
    for (const label of ["舒展", "紧凑"]) {
      const option = picker(page).getByRole("button", { name: label, exact: true });
      await option.click();
      await expect(option).toHaveAttribute("aria-pressed", "true");
      const geometry = await picker(page).evaluate(node => ({
        fits: node.getBoundingClientRect().left >= 0 && node.getBoundingClientRect().right <= innerWidth,
        overflow: node.scrollWidth > node.clientWidth,
        clippedOptions: [...node.querySelectorAll(".appearance-layout-option")].some(option => option.scrollWidth > option.clientWidth),
      }));
      expect(geometry).toEqual({ fits: true, overflow: false, clippedOptions: false });
    }
    await picker(page).screenshot({ path: info.outputPath(`session-layout-${width}.png`), animations: "disabled" });
    await picker(page).getByRole("button", { name: "完成", exact: true }).click();
  }
});

test("preset content keeps emphasis, highlighted code and links distinct in both engines", async ({ page }, info) => {
  test.setTimeout(120_000);
  await page.route("**/api/link-preview**", route => route.fulfill({ status: 404, body: "{}" }));
  if (info.project.name === "chromium") await page.setViewportSize({ width: 1440, height: 1000 });
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  for (const engine of ["claude", "codex"]) {
    for (const palette of ["amber", "moss", "rose", "lagoon", "bordeaux", "heritage"]) {
      await page.goto(`/tests/theme-preview.html?engine=${engine}&palette=${palette}`);
      await expect(page.locator("html")).toHaveAttribute("data-palette", palette);
      await expect(page.getByText("关键结论更醒目", { exact: true })).toBeVisible();
      const sample = () => page.locator(".prose").first().evaluate(prose => {
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = 1;
        const ctx = canvas.getContext("2d")!;
        const rgb = (value: string) => {
          ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = value; ctx.fillRect(0, 0, 1, 1);
          return Array.from(ctx.getImageData(0, 0, 1, 1).data).slice(0, 3);
        };
        const luminance = (value: string) => rgb(value).reduce((sum, v, i) => {
          const c = v / 255;
          return sum + (c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4) * [.2126, .7152, .0722][i];
        }, 0);
        const contrast = (a: string, b: string) => {
          const x = luminance(a), y = luminance(b);
          return (Math.max(x, y) + .05) / (Math.min(x, y) + .05);
        };
        const body = getComputedStyle(prose), bg = getComputedStyle(document.querySelector(".pane")!).backgroundColor;
        const strong = getComputedStyle(prose.querySelector("strong")!);
        const link = getComputedStyle(prose.querySelector("a")!);
        const code = getComputedStyle([...prose.querySelectorAll("code")].find(n => n.textContent === "std::max_element")!);
        const linkedCode = getComputedStyle(prose.querySelector("a code")!);
        const block = getComputedStyle(prose.querySelector("pre")!);
        const blockCode = getComputedStyle(prose.querySelector("pre code")!);
        const selection = getComputedStyle(prose.querySelector("strong")!, "::selection");
        return { contrast: [contrast(body.color, bg), contrast(strong.color, bg), contrast(link.color, bg),
          contrast(code.color, code.backgroundColor), contrast(blockCode.color, block.backgroundColor),
          contrast(selection.color, selection.backgroundColor)],
          weightGap: Number(strong.fontWeight) - Number(body.fontWeight),
          emphasized: strong.color !== body.color, coloredLink: link.color !== body.color,
          codePadding: parseFloat(code.paddingLeft), codeBackground: code.backgroundColor,
          linkedCodePadding: linkedCode.paddingLeft, linkedCodeColor: linkedCode.color,
          linkColor: link.color, underline: parseFloat(link.borderBottomWidth) };
      });
      const normal = await sample();
      expect(Math.min(...normal.contrast), `${engine}/${palette}`).toBeGreaterThanOrEqual(4.5);
      expect(normal.weightGap).toBeGreaterThanOrEqual(200);
      expect(normal.emphasized && normal.coloredLink).toBe(true);
      expect(normal.codePadding).toBeGreaterThan(0);
      expect(normal.codeBackground).not.toBe("rgba(0, 0, 0, 0)");
      expect(normal.linkedCodePadding).toBe("0px");
      expect(normal.linkedCodeColor).toBe(normal.linkColor);
      expect(normal.underline).toBeGreaterThan(0);
      if (engine === "codex" && ["lagoon", "bordeaux", "heritage"].includes(palette)) {
        await page.screenshot({ path: info.outputPath(`readability-${palette}.png`), animations: "disabled" });
      }
      await openPicker(page);
      const toggle = page.getByRole("switch", { name: "加粗字体", exact: true });
      await toggle.click();
      const bold = await sample();
      expect(bold.weightGap, `${palette}: bold mode must not flatten emphasis`).toBeGreaterThanOrEqual(200);
      expect(bold.contrast).toEqual(normal.contrast);
      if (engine === "codex" && ["lagoon", "bordeaux", "heritage"].includes(palette)) {
        await page.keyboard.press("Escape");
        await page.screenshot({ path: info.outputPath(`readability-${palette}-bold.png`), animations: "disabled" });
        await openPicker(page);
      }
      await toggle.click();
      await page.keyboard.press("Escape");
    }
  }
  expect(errors).toEqual([]);
});

test("appearance keeps authored defaults, edits live and restores only the visible mode", async ({ page }, info) => {
  if (info.project.name === "chromium") await page.setViewportSize({ width: 1440, height: 1000 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto(url);
  expect(await background(page)).toBe("rgb(250, 250, 252)");
  expect(await page.evaluate(() => localStorage.getItem("cc_remote_appearance_v2"))).toBeNull();
  await page.locator(".inrow textarea").fill("保留这条草稿");
  await page.screenshot({ path: info.outputPath("codex-light-default.png"), animations: "disabled" });
  await openPicker(page);
  await expect(color(page, "强调色")).toHaveValue("#6F69D2");
  await page.screenshot({ path: info.outputPath("appearance-default.png"), animations: "disabled" });
  await color(page, "背景色").fill("#F5F2FA");
  await color(page, "强调色").fill("#346B5E");
  expect(await background(page)).toBe("rgb(245, 242, 250)");
  await picker(page).getByRole("combobox", { name: "字体", exact: true }).click();
  await picker(page).getByRole("option", { name: "衬线", exact: true }).click();
  await expect(picker(page).getByRole("combobox", { name: "配色方案" })).toHaveText("自定义");
  await expect(page.locator(".inrow textarea")).toHaveValue("保留这条草稿");
  const codeFont = await page.locator(".prose pre code").evaluate(node => getComputedStyle(node).fontFamily);
  expect(codeFont).toMatch(/mono|Menlo/i);
  await picker(page).getByRole("button", { name: "深色", exact: true }).click();
  await expect(color(page, "背景色")).toHaveValue("#0D0E15");
  await color(page, "背景色").fill("#181920");
  await picker(page).getByRole("button", { name: "浅色", exact: true }).click();
  await expect(color(page, "背景色")).toHaveValue("#F5F2FA");
  await expect(picker(page).getByRole("combobox", { name: "字体", exact: true })).toHaveText("衬线");
  await picker(page).getByRole("button", { name: "恢复默认", exact: true }).click();
  await expect(color(page, "背景色")).toHaveValue("#FAFAFC");
  await expect(color(page, "强调色")).toHaveValue("#6F69D2");
  await expect(picker(page).getByRole("combobox", { name: "字体", exact: true })).toHaveText("系统默认");
  await picker(page).getByRole("button", { name: "深色", exact: true }).click();
  await expect(color(page, "背景色")).toHaveValue("#181920");
  await page.keyboard.press("Escape");
  await expect(picker(page)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "更多设置", exact: true })).toBeFocused();
});

test("appearance validates color input and keeps its controls readable for low contrast choices", async ({ page }) => {
  await page.goto(url);
  await openPicker(page);
  await color(page, "背景色").fill("#12345");
  await expect(color(page, "背景色")).toHaveAttribute("aria-invalid", "true");
  expect(await background(page)).toBe("rgb(250, 250, 252)");
  await color(page, "背景色").press("Tab");
  await expect(color(page, "背景色")).toHaveValue("#FAFAFC");
  await color(page, "背景色").fill("#abc");
  await color(page, "背景色").press("Enter");
  await expect(color(page, "背景色")).toHaveValue("#AABBCC");
  await color(page, "文字色").fill("#AABBCC");
  await expect(picker(page).getByRole("status")).toContainText("文字与背景较接近");
  await expect(picker(page)).toHaveCSS("color", "rgb(37, 35, 52)");
  await picker(page).getByRole("combobox", { name: "配色方案" }).focus();
  await page.keyboard.press("Tab");
  await expect(picker(page).getByLabel("选择强调色", { exact: true })).toBeFocused();
  await picker(page).getByRole("button", { name: "恢复默认", exact: true }).click();
  await expect(picker(page).getByRole("status")).toHaveCount(0);
});

test("appearance preserves legacy palettes and follows the system without writing on media changes", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await page.addInitScript(() => localStorage.setItem("cc_remote_themes_v1", JSON.stringify({ codex: "moss", claude: "bordeaux" })));
  await page.goto(url);
  await expect(page.locator("html")).toHaveAttribute("data-palette", "moss");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await openPicker(page);
  await expect(picker(page).getByRole("combobox", { name: "配色方案" })).toHaveText("森林苔绿");
  for (const [mode, id, name] of [["浅色", "amber", "奶油琥珀"], ["浅色", "moss", "森林苔绿"], ["浅色", "rose", "玫瑰雾"],
    ["深色", "lagoon", "深海青"], ["深色", "bordeaux", "酒红夜色"], ["深色", "heritage", "复古终端绿"]]) {
    await picker(page).getByRole("button", { name: mode, exact: true }).click();
    await choosePalette(page, name);
    await expect(page.locator("html")).toHaveAttribute("data-palette", id);
  }
  await picker(page).getByRole("button", { name: "跟随系统", exact: true }).click();
  const saved = await page.evaluate(() => localStorage.getItem("cc_remote_appearance_v2"));
  await page.emulateMedia({ colorScheme: "light" });
  await expect(page.locator("html")).toHaveAttribute("data-palette", "rose");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(page.locator("html")).toHaveAttribute("data-palette", "heritage");
  expect(await page.evaluate(() => localStorage.getItem("cc_remote_appearance_v2"))).toBe(saved);
});

test("appearance remembers each engine across switches and reloads, and syncs other tabs", async ({ page }) => {
  await page.goto(url);
  await openPicker(page);
  await color(page, "强调色").fill("#326B5E");
  await picker(page).getByRole("button", { name: "完成", exact: true }).click();
  await page.getByRole("button", { name: "切换新会话引擎" }).click();
  await page.getByRole("menuitemradio", { name: "Claude" }).click();
  await openPicker(page);
  await expect(color(page, "强调色")).toHaveValue("#C15F3C");
  await color(page, "背景色").fill("#EFE8DA");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "切换新会话引擎" }).click();
  await page.getByRole("menuitemradio", { name: "Codex" }).click();
  await page.reload();
  await openPicker(page);
  await expect(color(page, "强调色")).toHaveValue("#326B5E");
  const other = await page.context().newPage();
  await other.goto(url);
  await openPicker(other);
  await color(other, "背景色").fill("#F4F5FA");
  await expect(color(page, "背景色")).toHaveValue("#F4F5FA");
  await color(page, "文字色").fill("#292929");
  await expect(color(other, "文字色")).toHaveValue("#292929");
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem("cc_remote_appearance_v2")!)))
    .toMatchObject({ claude: { light: { background: "#EFE8DA" } },
      codex: { light: { accent: "#326B5E", background: "#F4F5FA", foreground: "#292929" } } });
  await other.close();
});

test("appearance remains centered and usable in a narrow or keyboard-height viewport", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(url);
  await openPicker(page);
  const panel = picker(page);
  const initial = (await panel.boundingBox())!;
  expect(Math.abs(initial.x + initial.width / 2 - 195)).toBeLessThan(2);
  expect(await panel.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath("appearance-mobile.png"), animations: "disabled" });
  await page.setViewportSize({ width: 320, height: 380 });
  await expect.poll(async () => {
    const box = (await panel.boundingBox())!;
    return box.y >= 0 && box.y + box.height <= 380 && box.x >= 0 && box.x + box.width <= 320;
  }).toBe(true);
  await color(page, "强调色").fill("#824C70");
  await picker(page).getByRole("combobox", { name: "字体", exact: true }).click();
  await picker(page).getByRole("option", { name: "衬线", exact: true }).click();
  await expect(panel.getByRole("button", { name: "完成", exact: true })).toBeVisible();
  expect(await panel.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
  await panel.getByRole("button", { name: "完成", exact: true }).click();
  await expect(panel).toHaveCount(0);
});

test("appearance still works with blocked storage", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript(() => Object.defineProperty(window, "localStorage", {
    get() { throw new DOMException("Storage unavailable", "SecurityError"); },
  }));
  await page.goto(url);
  await openPicker(page);
  await color(page, "背景色").fill("#F5F2FA");
  await color(page, "强调色").fill("#346B5E");
  await picker(page).getByRole("button", { name: "深色", exact: true }).click();
  await picker(page).getByRole("button", { name: "浅色", exact: true }).click();
  await expect(color(page, "背景色")).toHaveValue("#F5F2FA");
  await expect(color(page, "强调色")).toHaveValue("#346B5E");
  expect(errors).toEqual([]);
});

test("appearance palettes derive custom status, preserve fonts and restore preset colors", async ({ page }) => {
  await page.goto(url);
  await openPicker(page);
  const scheme = picker(page).getByRole("combobox", { name: "配色方案", exact: true });
  const font = picker(page).getByRole("combobox", { name: "字体", exact: true });
  const menu = picker(page).getByRole("listbox", { name: "配色方案选项", exact: true });
  await expect(scheme).toHaveText("默认配色");
  await font.click();
  await picker(page).getByRole("option", { name: "衬线", exact: true }).click();
  await expect(scheme).toHaveText("默认配色");
  await scheme.focus();
  await scheme.press("ArrowDown");
  await expect(menu.getByRole("option")).toHaveText(["奶油琥珀", "森林苔绿", "玫瑰雾"]);
  await expect(menu.getByRole("option", { selected: true })).toHaveCount(0);
  await expect(menu.getByRole("option", { name: "奶油琥珀" })).toBeFocused();
  await expect(menu.locator("svg, input, optgroup")).toHaveCount(0);
  await expect(picker(page).locator("select")).toHaveCount(0);
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(scheme).toHaveText("森林苔绿");
  await expect(font).toHaveText("衬线");
  await expect(scheme).toBeFocused();
  await color(page, "强调色").fill("#405060");
  await expect(scheme).toHaveText("自定义");
  await page.reload();
  await openPicker(page);
  await expect(scheme).toHaveText("自定义");
  await expect(font).toHaveText("衬线");
  await scheme.click();
  await expect(menu.getByRole("option", { selected: true })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(picker(page)).toBeVisible();
  await expect(color(page, "强调色")).toHaveValue("#405060");
  await choosePalette(page, "森林苔绿");
  await expect(color(page, "强调色")).toHaveValue("#96A28B");
  await expect(font).toHaveText("衬线");
  await color(page, "强调色").fill("#405060");
  await color(page, "强调色").fill("#96A28B");
  await expect(scheme).toHaveText("森林苔绿");
  expect(await page.locator("html").evaluate(node => (node as HTMLElement).style.getPropertyValue("--accent"))).toBe("");
  await scheme.click();
  await expect(menu.getByRole("option", { name: "森林苔绿" })).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Tab");
  await expect(menu).toHaveCount(0);
  await expect(picker(page).getByRole("button", { name: "选择强调色", exact: true })).toBeFocused();
  await picker(page).getByRole("button", { name: "恢复默认", exact: true }).click();
  await expect(scheme).toHaveText("默认配色");
  await expect(font).toHaveText("系统默认");
  await expect(color(page, "强调色")).toHaveValue("#6F69D2");
});

test("appearance palette menu aligns samples and fits desktop and mobile dark mode", async ({ page }, info) => {
  const errors: string[] = [];
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  page.on("pageerror", error => errors.push(error.message));
  // The standalone Vite fixture has no Relay link-preview endpoint.
  await page.route("**/api/link-preview", route => route.fulfill({ json: {} }));
  if (info.project.name === "chromium") await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(url);
  await openPicker(page);
  await picker(page).getByRole("button", { name: "深色", exact: true }).click();
  await choosePalette(page, "深海青");
  const scheme = picker(page).getByRole("combobox", { name: "配色方案", exact: true });
  const menu = picker(page).getByRole("listbox", { name: "配色方案选项", exact: true });
  await scheme.click();
  await expect(menu.getByRole("option")).toHaveText(["深海青", "酒红夜色", "复古终端绿"]);
  await expect(menu.getByRole("option", { name: "深海青" })).toHaveAttribute("aria-selected", "true");
  await expect(menu).toHaveCSS("background-color", "rgb(32, 33, 40)");
  const samples = await menu.getByRole("option").evaluateAll(rows => rows.map(row => ({
    text: row.firstElementChild!.getBoundingClientRect().left,
    samples: row.lastElementChild!.getBoundingClientRect().right,
    colors: [...row.querySelectorAll("i")].map(node => getComputedStyle(node).backgroundColor),
  })));
  expect(new Set(samples.map(row => row.text)).size).toBe(1);
  expect(new Set(samples.map(row => row.samples)).size).toBe(1);
  expect(samples[0].colors).toEqual(["rgb(36, 43, 45)", "rgb(154, 177, 179)", "rgb(226, 230, 229)"]);
  await page.screenshot({ path: info.outputPath("palette-menu-dark.png"), animations: "disabled" });
  await page.keyboard.press("Escape");
  await color(page, "强调色").fill("#8590FF");
  await expect(scheme).toHaveText("自定义");
  await picker(page).locator(".theme-picker-footer > span").click();
  await page.screenshot({ path: info.outputPath("palette-custom-dark.png"), animations: "disabled" });
  await scheme.click();
  for (const viewport of [{ width: 320, height: 380 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    await expect.poll(async () => {
      const box = (await menu.boundingBox())!;
      return box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width && box.y + box.height <= viewport.height;
    }).toBe(true);
    expect(await menu.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
  }
  await page.keyboard.press("End");
  await page.keyboard.press("Enter");
  await expect(scheme).toHaveText("复古终端绿");
  // Restore desktop width before leaving the sheet: the fixture retains its open sidebar.
  if (info.project.name === "chromium") await page.setViewportSize({ width: 1440, height: 1000 });
  await picker(page).getByRole("button", { name: "完成", exact: true }).click();
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  await expect(page.getByRole("button", { name: /^外观/ })).toContainText("复古终端绿");
  expect(errors).toEqual([]);
});

test("appearance font menu supports keyboard selection, dismissal and themed samples", async ({ page }, info) => {
  await page.goto(url);
  await openPicker(page);
  const font = picker(page).getByRole("combobox", { name: "字体", exact: true });
  await font.focus();
  await page.keyboard.press("ArrowDown");
  const menu = picker(page).getByRole("listbox", { name: "字体选项" });
  await expect(menu.getByRole("option", { name: "系统默认" })).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(menu.getByRole("option", { name: "无衬线", exact: true })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(font).toHaveText("无衬线");
  await expect(font).toBeFocused();
  await font.click();
  await page.keyboard.press("End");
  await page.keyboard.press("Space");
  await expect(font).toHaveText("衬线");
  await font.click();
  await expect(menu.getByRole("option", { name: "衬线", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(menu.locator("svg, input")).toHaveCount(0);
  await page.screenshot({ path: info.outputPath("appearance-font-menu.png"), animations: "disabled" });
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(picker(page)).toBeVisible();
  await expect(font).toBeFocused();
  await font.click();
  await page.keyboard.press("Tab");
  await expect(menu).toHaveCount(0);
  await expect(picker(page).getByRole("button", { name: "舒展", exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(picker(page).getByRole("button", { name: "紧凑", exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(picker(page).getByRole("button", { name: "恢复默认", exact: true })).toBeFocused();
  await font.click();
  await picker(page).locator(".theme-picker-intro").click();
  await expect(menu).toHaveCount(0);
  await expect(picker(page)).toBeVisible();
});

test("appearance color popover keeps hue through black, edits HEX and resets only one color", async ({ page }, info) => {
  await page.goto(url);
  await openPicker(page);
  await color(page, "背景色").fill("#EDE9F5");
  const trigger = picker(page).getByRole("button", { name: "选择强调色", exact: true });
  await trigger.click();
  const palette = picker(page).getByRole("dialog", { name: "强调色取色器", exact: true });
  const hex = palette.getByRole("textbox", { name: "强调色取色器 HEX" });
  await expect(palette).toBeVisible();
  await expect(picker(page).locator('input[type="color"]')).toHaveCount(0);
  await expect(palette.getByRole("button", { name: "关闭强调色取色器" })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(palette.getByRole("button", { name: "恢复默认", exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(palette.getByRole("button", { name: "关闭强调色取色器" })).toBeFocused();
  await hex.fill("#0F0");
  await hex.press("Tab");
  await expect(hex).toHaveValue("#00FF00");
  await expect(color(page, "强调色")).toHaveValue("#00FF00");
  await hex.fill("#abc");
  await picker(page).locator(".theme-picker-footer > span").click();
  await expect(palette).toHaveCount(0);
  await expect(color(page, "强调色")).toHaveValue("#AABBCC");
  await trigger.click();
  await hex.fill("#000000");
  const hue = palette.getByRole("slider", { name: "色相", exact: true });
  await hue.focus();
  await hue.press("Home");
  await hue.press("ArrowRight");
  await expect(hue).toHaveValue("1");
  await expect(hex).toHaveValue("#000000");
  await palette.getByRole("slider", { name: "饱和度", exact: true }).press("End");
  await palette.getByRole("slider", { name: "亮度", exact: true }).press("End");
  await expect(hex).toHaveValue("#FF0400");
  if (info.project.name === "chromium") {
    const box = (await palette.getByRole("group", { name: "饱和度与亮度" }).boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width + 10, box.y + box.height + 10);
    await expect(hex).toHaveValue("#000000");
    await page.mouse.move(box.x - 10, box.y - 10);
    await page.mouse.up();
    await expect(hex).toHaveValue("#FFFFFF");
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await expect(hex).toHaveValue("#FFFFFF");
  }
  await palette.getByRole("button", { name: "使用颜色 #6F69D2", exact: true }).click();
  await expect(hex).toHaveValue("#6F69D2");
  await page.screenshot({ path: info.outputPath("appearance-color-picker.png"), animations: "disabled" });
  await palette.getByRole("button", { name: "使用颜色 #518C69", exact: true }).click();
  await palette.getByRole("button", { name: "恢复默认", exact: true }).click();
  await expect(hex).toHaveValue("#6F69D2");
  await expect(color(page, "背景色")).toHaveValue("#EDE9F5");
  await page.keyboard.press("Escape");
  await expect(palette).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await expect(picker(page)).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(picker(page)).toHaveCount(0);
  await page.reload();
  await openPicker(page);
  await expect(color(page, "背景色")).toHaveValue("#EDE9F5");
  await expect(color(page, "强调色")).toHaveValue("#6F69D2");
});

test("appearance popovers fit small viewports, support touch and retain dark-theme contrast", async ({ page }, info) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/tests/theme-preview.html?engine=claude");
  await openPicker(page);
  await picker(page).getByRole("button", { name: "深色", exact: true }).click();
  const trigger = picker(page).getByRole("button", { name: "选择强调色", exact: true });
  await trigger.click();
  const palette = picker(page).getByRole("dialog", { name: "强调色取色器", exact: true });
  await expect(palette).toHaveCSS("background-color", "rgb(32, 33, 40)");
  const area = palette.getByRole("group", { name: "饱和度与亮度" });
  if (info.project.name === "webkit") await area.tap({ position: { x: 50, y: 50 } });
  else await area.click({ position: { x: 50, y: 50 } });
  await expect(color(page, "强调色")).not.toHaveValue("#DB7C5E");
  await page.screenshot({ path: info.outputPath("appearance-color-dark-mobile.png"), animations: "disabled" });
  for (const viewport of [{ width: 320, height: 380 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    await expect.poll(async () => {
      const box = (await palette.boundingBox())!;
      return box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width && box.y + box.height <= viewport.height;
    }).toBe(true);
    expect(await palette.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
    await palette.getByRole("textbox").fill("#AA6688");
    await expect(color(page, "强调色")).toHaveValue("#AA6688");
  }
  await palette.getByRole("button", { name: "关闭强调色取色器" }).click();
  const font = picker(page).getByRole("combobox", { name: "字体", exact: true });
  await font.click();
  await expect(picker(page).getByRole("listbox")).toBeVisible();
  // The menu floats above the rows; dismiss it from the uncovered label first.
  await picker(page).locator(".appearance-label", { hasText: "字体" }).click();
  await expect(picker(page).getByRole("listbox")).toHaveCount(0);
  await trigger.click();
  await expect(picker(page).getByRole("listbox")).toHaveCount(0);
  await expect(palette).toBeVisible();
  await palette.getByRole("button", { name: "关闭强调色取色器" }).click();
  await picker(page).getByRole("button", { name: "浅色", exact: true }).click();
  await trigger.click();
  await expect(palette.getByRole("textbox")).toHaveValue("#C15F3C");
  await expect(palette).toHaveCSS("background-color", "rgb(255, 255, 255)");
  expect(errors).toEqual([]);
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
  await openPicker(page);
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
  await choosePalette(page, "森林苔绿");
  // Lazy sheets also inherit the same weight scale.
  await expect(picker(page).locator(".appearance-label").first()).toHaveCSS("font-weight", "750");
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
  await openPicker(page);
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
  await openPicker(page);
  await page.getByRole("switch", { name: "加粗字体", exact: true }).click();
  await expect(other.locator(".prose p").first()).toHaveCSS("font-weight", "700");
  await openPicker(other);
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
  await openPicker(page);
  await page.getByRole("switch", { name: "加粗字体", exact: true }).click();
  await expect(page.locator(".prose p").first()).toHaveCSS("font-weight", "700");
  expect(errors).toEqual([]);
});
