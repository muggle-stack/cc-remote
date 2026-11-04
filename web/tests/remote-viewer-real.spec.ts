import { test, expect } from "@playwright/test";

// Opt-in, read-only acceptance against an operator-supplied source snapshot.
// No private paths, original source, credentials or generated models in Git.
test("real Three.js STL Viewer loads and retains interaction over Bridge", async ({ page }, info) => {
  test.skip(!process.env.VIEWER_TEST_SOURCE || ["spine", "embedded", "media"].includes(process.env.VIEWER_TEST_KIND ?? ""), "Supply a matching read-only source snapshot for acceptance.");
  test.setTimeout(180000);
  const origin = process.env.VIEWER_TEST_ORIGIN ?? "http://127.0.0.1:4174";
  const failures: string[] = [];
  const models = new Set<string>();
  let binaryBytes = 0;
  page.on("websocket", (socket) => {
    if (!socket.url().endsWith("/ws/viewer-client")) return;
    socket.on("framesent", ({ payload }) => {
      if (typeof payload !== "string") return;
      const data = JSON.parse(payload);
      if (data.type === "read" && data.method === "GET" && data.path.endsWith(".stl")) models.add(data.path);
    });
    socket.on("framereceived", ({ payload }) => { if (typeof payload !== "string") binaryBytes += payload.byteLength - 16; });
  });
  page.on("pageerror", (error) => failures.push(error.message));
  const start = Date.now();
  await page.goto(`${origin}/tests/remote-viewer.html`);
  expect(await page.evaluate(async () => (await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: "local-viewer-fixture-password" }),
  })).status)).toBe(200);
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  const frame = page.frameLocator(".viewer-stage iframe");
  await expect(frame.locator("#load-state")).toHaveAttribute("data-ready", "true", { timeout: 120000 });
  const elapsedMs = Date.now() - start;
  await expect(page.locator(".viewer-error-banner")).toHaveCount(0);
  expect(failures).toEqual([]);
  const secure = await page.evaluate(() => isSecureContext);
  if (origin.startsWith("http:") && !origin.includes("127.0.0.1") && !origin.includes("localhost")) expect(secure).toBe(false);
  await frame.locator('[data-pose="crouch"]').click();
  await expect(frame.locator('[data-pose="crouch"]')).toHaveAttribute("aria-pressed", "true");
  expect(await frame.locator("#knee-angle").inputValue()).not.toBe("0");
  await frame.locator('button[data-explode-mode="full"]').click();
  await expect(frame.locator('button[data-explode-mode="full"]')).toHaveAttribute("aria-pressed", "true");
  const source = await page.locator("iframe").getAttribute("src");
  await page.locator("#append-message").evaluate((button: HTMLButtonElement) => button.click());
  await expect(page.locator("iframe")).toHaveAttribute("src", source!);
  await expect(frame.locator('[data-pose="crouch"]')).toHaveAttribute("aria-pressed", "true");
  expect(models.size).toBe(43);
  const metrics = { elapsedMs, secure, models: models.size, binaryBytes, errors: failures };
  console.info(`Viewer acceptance (${info.project.name}): ${JSON.stringify(metrics)}`);
  await info.attach("acceptance.json", { body: JSON.stringify(metrics), contentType: "application/json" });
  await frame.locator('button[data-explode-mode="assembled"]').click();
  await frame.locator('[data-pose="stand"]').click();
  const canvas = frame.locator("#robot-canvas");
  await canvas.scrollIntoViewIfNeeded();
  const original = await canvas.screenshot();
  await frame.locator('button[data-view="side"]').click();
  await canvas.scrollIntoViewIfNeeded();
  await expect.poll(async () => original.equals(await canvas.screenshot())).toBe(false);
  await frame.locator('button[data-view="iso"]').click();
  // Allow the source page's 360 ms camera animation to settle before raycasting.
  await page.waitForTimeout(500);
  await canvas.scrollIntoViewIfNeeded();
  const size = await canvas.boundingBox();
  await canvas.click({ position: { x: size!.width * 0.5, y: size!.height * 0.45 } });
  await expect(frame.locator("#selected-part")).not.toHaveText("整机");
  if (!info.project.name.includes("mobile")) {
    const beforeZoom = await canvas.screenshot();
    await canvas.hover();
    await page.mouse.wheel(0, -160);
    await expect.poll(async () => beforeZoom.equals(await canvas.screenshot())).toBe(false);
  }
  await page.screenshot({ path: info.outputPath("real-viewer.png"), fullPage: true });
});

test("real local-addon Three.js Viewer loads with prefix import maps", async ({ page }, info) => {
  test.skip(!process.env.VIEWER_TEST_SOURCE || process.env.VIEWER_TEST_KIND !== "spine", "Supply a local-addon Viewer snapshot.");
  test.setTimeout(180000);
  const origin = process.env.VIEWER_TEST_ORIGIN ?? "http://127.0.0.1:4174";
  const failures: string[] = [];
  page.on("pageerror", (error) => failures.push(error.message));
  await page.goto(`${origin}/tests/remote-viewer.html`);
  expect(await page.evaluate(async () => (await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: "local-viewer-fixture-password" }),
  })).status)).toBe(200);
  const start = Date.now();
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  const frame = page.frameLocator(".viewer-stage iframe");
  const audit = () => frame.locator("body").evaluate(() => (window as unknown as {
    __spineAudit?: () => { loaded: number; total: number; contextLoaded: number; contextTotal: number;
      mode: string; q: number; rendererCalls: number; missing: string[] };
  }).__spineAudit?.());
  await expect.poll(async () => {
    const value = await audit();
    return !!value && value.total > 0 && value.loaded === value.total
      && value.contextLoaded === value.contextTotal && value.rendererCalls > 0;
  }, { timeout: 120000 }).toBe(true);
  const elapsedMs = Date.now() - start;
  await expect(frame.locator("#error")).toBeHidden();
  await expect(page.locator(".viewer-error-banner")).toHaveCount(0);
  expect(failures).toEqual([]);
  expect((await audit())!.missing).toEqual([]);
  await frame.locator("#full").click();
  await expect.poll(async () => (await audit())?.mode).toBe("full");
  await frame.locator("#q").fill("65");
  await expect.poll(async () => (await audit())?.q).toBe(65);
  const src = await page.locator("iframe").getAttribute("src");
  await page.locator("#append-message").evaluate((button: HTMLButtonElement) => button.click());
  await expect(page.locator("iframe")).toHaveAttribute("src", src!);
  await expect.poll(async () => (await audit())?.q).toBe(65);
  const metrics = { elapsedMs, ...await audit(), failures };
  console.info(`Local-addon Viewer (${info.project.name}): ${JSON.stringify(metrics)}`);
  await info.attach("local-addon-acceptance.json", { body: JSON.stringify(metrics), contentType: "application/json" });
  await frame.locator("#canvas").scrollIntoViewIfNeeded();
  await page.screenshot({ path: info.outputPath("local-addon-viewer.png"), fullPage: true });
});

test("real embedded Three.js Viewer loads model data and retains interaction", async ({ page }, info) => {
  test.skip(!process.env.VIEWER_TEST_SOURCE || process.env.VIEWER_TEST_KIND !== "embedded", "Supply an embedded assembly Viewer snapshot.");
  test.setTimeout(180000);
  const origin = process.env.VIEWER_TEST_ORIGIN ?? "http://127.0.0.1:4174";
  const failures: string[] = [];
  page.on("pageerror", (error) => failures.push(error.message));
  await page.goto(`${origin}/tests/remote-viewer.html`);
  expect(await page.evaluate(async () => (await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: "local-viewer-fixture-password" }),
  })).status)).toBe(200);
  const start = Date.now();
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  const frame = page.frameLocator(".viewer-stage iframe");
  const audit = () => frame.locator("body").evaluate(() => {
    const viewer = (window as unknown as { assemblyViewer?: {
      ready: boolean; stats: () => { parts: number; visible: number; definitions: number; triangles: number };
    } }).assemblyViewer;
    return viewer?.ready ? viewer.stats() : null;
  });
  await expect.poll(async () => (await audit())?.parts ?? 0, { timeout: 120000 }).toBeGreaterThan(0);
  const stats = (await audit())!;
  const elapsedMs = Date.now() - start;
  expect(stats.definitions).toBeGreaterThan(0);
  expect(stats.triangles).toBeGreaterThan(0);
  await expect(frame.locator("#loading")).toHaveCount(0);
  await expect(page.locator(".viewer-error-banner")).toHaveCount(0);
  await frame.locator("#parts .part-row").first().click();
  await frame.locator("#isolate").click();
  await expect.poll(async () => (await audit())?.visible).toBe(1);
  const source = await page.locator("iframe").getAttribute("src");
  await page.locator("#append-message").evaluate((button: HTMLButtonElement) => button.click());
  await expect(page.locator("iframe")).toHaveAttribute("src", source!);
  await expect.poll(async () => (await audit())?.visible).toBe(1);
  await frame.locator("#restore").click();
  await expect.poll(async () => (await audit())?.visible ?? 0).toBeGreaterThan(1);
  const canvas = frame.locator("#viewport canvas");
  await canvas.scrollIntoViewIfNeeded();
  const before = await canvas.screenshot();
  await frame.locator('[data-view="side"]').click();
  await canvas.scrollIntoViewIfNeeded();
  await expect.poll(async () => before.equals(await canvas.screenshot())).toBe(false);
  await expect(page.locator(".viewer-error-banner")).toHaveCount(0);
  expect(failures).toEqual([]);
  const metrics = { elapsedMs, ...stats, failures };
  console.info(`Embedded Viewer (${info.project.name}): ${JSON.stringify(metrics)}`);
  await info.attach("embedded-acceptance.json", { body: JSON.stringify(metrics), contentType: "application/json" });
  await page.screenshot({ path: info.outputPath("embedded-viewer.png"), fullPage: true });
});

test("real media gallery renders and plays the first and last sample", async ({ page }, info) => {
  test.skip(!process.env.VIEWER_TEST_SOURCE || process.env.VIEWER_TEST_KIND !== "media", "Supply a read-only media gallery.");
  test.setTimeout(90000);
  const origin = process.env.VIEWER_TEST_ORIGIN ?? "http://127.0.0.1:4174";
  await page.goto(`${origin}/tests/remote-viewer.html`);
  expect(await page.evaluate(async () => (await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: "local-viewer-fixture-password" }),
  })).status)).toBe(200);
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  const frame = page.frameLocator(".viewer-stage iframe");
  await expect(frame.locator("audio").first()).toBeVisible();
  const count = await frame.locator("audio").count();
  expect(count).toBeGreaterThan(8);
  for (const sample of [frame.locator("audio").first(), frame.locator("audio").last()]) {
    await sample.scrollIntoViewIfNeeded();
    await sample.evaluate(async (el: HTMLMediaElement) => { el.muted = true; await el.play(); });
    await expect.poll(() => sample.evaluate((el: HTMLMediaElement) => el.currentTime)).toBeGreaterThan(0.1);
    await sample.evaluate((el: HTMLMediaElement) => el.pause());
  }
  await expect(page.locator(".viewer-error-banner")).toHaveCount(0);
  await info.attach("media-acceptance.json", { body: JSON.stringify({ count }), contentType: "application/json" });
});
