import { expect, test } from "@playwright/test";

// Run against the exported bundle, through the real Relay/Wrapper resource
// channel and opaque frame. A Vite dev-page test cannot catch TSX/asset failures.
test("exported theme preview loads and switches palettes over Bridge", async ({ page }, info) => {
  test.skip(process.env.VIEWER_TEST_KIND !== "theme", "Supply a built theme preview.");
  test.setTimeout(120000);
  const failures: string[] = [];
  const reads: string[] = [];
  page.on("pageerror", error => failures.push(error.message));
  page.on("websocket", socket => {
    if (!socket.url().endsWith("/ws/viewer-client")) return;
    socket.on("framesent", ({ payload }) => {
      if (typeof payload !== "string") return;
      const message = JSON.parse(payload);
      if (message.type === "read") reads.push(message.path);
    });
  });
  await page.goto(`${process.env.VIEWER_TEST_ORIGIN ?? "http://127.0.0.1:4174"}/tests/remote-viewer.html`);
  expect(await page.evaluate(async () => (await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: "local-viewer-fixture-password" }),
  })).status)).toBe(200);
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  const frame = page.frameLocator(".viewer-stage iframe");
  await expect(frame.locator(".shell")).toBeVisible({ timeout: 30000 });
  await expect(frame.getByText("主题预览 · 示例会话", { exact: true })).toBeAttached();
  await expect(page.locator(".viewer-error-banner")).toHaveCount(0);
  await expect(page.locator(".viewer-stage iframe")).toHaveAttribute("sandbox", "allow-scripts");
  const draft = frame.locator(".inrow textarea");
  await draft.fill("手机预览保留草稿");
  await frame.getByRole("button", { name: "更多设置", exact: true }).click();
  await frame.getByRole("button", { name: /^主题/ }).click();
  const dialog = frame.getByRole("dialog", { name: "主题", exact: true });
  await expect(dialog.locator(".theme-card")).toHaveCount(6);
  for (const [name, id] of [["奶油琥珀", "amber"], ["森林苔绿", "moss"], ["玫瑰雾", "rose"],
    ["深海青", "lagoon"], ["酒红夜色", "bordeaux"], ["复古终端绿", "heritage"]]) {
    await dialog.getByRole("button", { name, exact: true }).click();
    await expect(frame.locator("html")).toHaveAttribute("data-palette", id);
    await expect(draft).toHaveValue("手机预览保留草稿");
  }
  await dialog.getByRole("button", { name: "森林苔绿", exact: true }).click();
  await page.screenshot({ path: info.outputPath("theme-preview-bridge.png"), fullPage: true });
  await dialog.getByRole("button", { name: "完成", exact: true }).click();
  await page.getByRole("button", { name: "刷新预览", exact: true }).click();
  await expect(frame.locator(".shell")).toBeVisible({ timeout: 30000 });
  await expect(page.locator(".viewer-error-banner")).toHaveCount(0);
  expect(reads.some(path => path.endsWith(".js"))).toBe(true);
  // A huge lazy graph passes on loopback but times out over a mobile relay.
  expect(new Set(reads).size).toBeLessThanOrEqual(6);
  expect(reads.filter(path => /\.[jt]sx?(?:$|\?)/.test(path) && !path.endsWith(".js"))).toEqual([]);
  expect(failures).toEqual([]);
});
