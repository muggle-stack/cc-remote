import { test, expect } from "@playwright/test";
import { PROTOCOL_VERSION } from "../src/protocol";

const ORIGIN = process.env.VIEWER_TEST_ORIGIN ?? "http://127.0.0.1:4174";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => { Object.defineProperty(window, "__fixtureNativeFetch", { value: window.fetch }); });
  // Test-only DNS mapping. Preserve real origins, browser cookie policy and
  // CSP; only the socket destination is redirected to the loopback fixtures.
  await page.route(/^http:\/\/(?:[a-z0-9-]+\.)*cc-remote\.localhost:\d+\//, async (route) => {
    const url = new URL(route.request().url());
    const backend = url.hostname !== "app.cc-remote.localhost" || url.pathname.startsWith("/api/");
    const headers = { ...await route.request().allHeaders(), host: url.host };
    const response = await route.fetch({
      url: `http://127.0.0.1:${backend ? 4178 : new URL(ORIGIN).port}${url.pathname}${url.search}`,
      headers, maxRedirects: 0,
    });
    await route.fulfill({ response });
  });
  await page.goto(`${ORIGIN}/tests/remote-viewer.html`);
  const status = await page.evaluate(async () => (await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: "local-viewer-fixture-password" }),
  })).status);
  expect(status).toBe(200);
});

test("HTML page zoom fits wide content and preserves controls and retained state", async ({ page }, info) => {
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^宽页面缩放测试/ }).click();
  const frame = page.frameLocator(".viewer-stage iframe");
  const zoom = page.getByRole("button", { name: "页面缩放", exact: true });
  await expect(zoom).toBeEnabled();
  await expect(zoom).toHaveText("100%");
  const source = await page.locator(".viewer-stage iframe").getAttribute("src");
  const viewportScale = await page.evaluate(() => visualViewport?.scale);
  await zoom.click();
  await page.getByRole("button", { name: "适应宽度", exact: true }).click();
  await expect.poll(() => frame.locator("html").evaluate(el => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(1);
  await frame.getByRole("button", { name: "计数 0", exact: true }).click();
  await expect(frame.locator("#counter")).toHaveText("计数 1");
  const range = frame.locator("#range");
  const box = await range.boundingBox();
  await range.click({ position: { x: box!.width * 0.75, y: box!.height / 2 } });
  await expect.poll(() => frame.locator("#value").textContent().then(Number)).toBeGreaterThan(65);
  await page.screenshot({ path: info.outputPath("page-fit-width.png") });
  if (info.project.name === "chromium") {
    await page.getByRole("button", { name: "居中展开远程预览" }).click();
    await expect.poll(() => page.locator(".viewer-stage iframe").evaluate(el => el.getBoundingClientRect().width)).toBeGreaterThan(800);
    await expect.poll(() => frame.locator("html").evaluate(el =>
      Math.abs(parseFloat(getComputedStyle(el).zoom) - Math.min(1, el.clientWidth / 1400))))
      .toBeLessThan(0.005);
    await expect.poll(() => frame.locator("html").evaluate(el => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(1);
  }
  const percentage = await zoom.textContent();
  // Switching to another engine hides, but must not reload, this frame.
  await page.getByRole("button", { name: "切换引擎" }).evaluate((el: HTMLButtonElement) => el.click());
  await page.getByRole("button", { name: "切换引擎" }).evaluate((el: HTMLButtonElement) => el.click());
  await expect(zoom).toHaveText(percentage!);
  await expect(frame.locator("#counter")).toHaveText("计数 1");
  await expect(page.locator(".viewer-stage iframe")).toHaveAttribute("src", source!);
  await zoom.click();
  await page.getByRole("button", { name: "恢复 100%" }).click();
  await expect(zoom).toHaveText("100%");
  expect(await page.evaluate(() => visualViewport?.scale)).toBe(viewportScale);
  await expect(page.locator(".viewer-error-banner")).toHaveCount(0);
});

test("HTML pinch zoom stays inside the frame and respects page-owned gestures", async ({ page }) => {
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^宽页面缩放测试/ }).click();
  const frame = page.frameLocator(".viewer-stage iframe");
  const zoom = page.getByRole("button", { name: "页面缩放", exact: true });
  await expect(zoom).toBeEnabled();
  const viewportScale = await page.evaluate(() => visualViewport?.scale);
  await frame.locator("body").evaluate(el => {
    el.dispatchEvent(new WheelEvent("wheel", { ctrlKey: true, deltaY: 30, clientX: 150, clientY: 150, bubbles: true, cancelable: true }));
  });
  await expect(zoom).toHaveText("74%");
  await frame.locator("body").evaluate(el => {
    // Ordinary scrolling must not change page scale.
    el.dispatchEvent(new WheelEvent("wheel", { deltaY: 60, bubbles: true, cancelable: true }));
  });
  await expect(zoom).toHaveText("74%");
  await frame.locator("#owned").evaluate(el => {
    el.dispatchEvent(new WheelEvent("wheel", { ctrlKey: true, deltaY: 50, bubbles: true, cancelable: true }));
  });
  await expect(zoom).toHaveText("74%");
  await frame.locator("body").evaluate(el => {
    const send = (type: string, scale: number) => {
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.assign(event, { scale, clientX: 150, clientY: 150 }); el.dispatchEvent(event);
    };
    send("gesturestart", 1); send("gesturechange", 1.25); send("gestureend", 1.25);
  });
  await expect(zoom).toHaveText("116%");
  await frame.locator("body").evaluate(el => {
    const send = (type: string, width: number) => {
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperty(event, "touches", { value: type === "touchend" ? [] : [
        { clientX: 100, clientY: 150 }, { clientX: 100 + width, clientY: 150 },
      ] }); el.dispatchEvent(event);
    };
    send("touchstart", 100); send("touchmove", 150); send("touchend", 150);
  });
  await expect(zoom).toHaveText("174%");
  // Sibling/main window messages cannot forge the current frame's zoom state.
  await page.evaluate(() => window.postMessage({ type: "cc-viewer-zoom-state", scale: 4, fit: false }, "*"));
  await expect(zoom).toHaveText("174%");
  expect(await page.evaluate(() => visualViewport?.scale)).toBe(viewportScale);
});

test("HTML browser touch input zooms locally while ordinary wheel scrolling remains native", async ({ page }, info) => {
  test.skip(info.project.name !== "chromium", "CDP touch injection is Chromium-only.");
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^宽页面缩放测试/ }).click();
  await expect(page.getByRole("button", { name: "页面缩放", exact: true })).toBeEnabled();
  const frame = page.frameLocator(".viewer-stage iframe");
  const viewportScale = await page.evaluate(() => visualViewport?.scale);
  const box = await page.locator(".viewer-stage iframe").boundingBox();
  const x = box!.x + 180, y = box!.y + 380;
  const client = await page.context().newCDPSession(page);
  const points = (gap: number) => [{ x: x - gap, y, id: 1 }, { x: x + gap, y, id: 2 }];
  await client.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: points(40) });
  for (const gap of [45, 55, 65, 75, 80]) {
    await client.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: points(gap) });
  }
  await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await expect.poll(() => frame.locator("html").evaluate(el => parseFloat(getComputedStyle(el).zoom))).toBeGreaterThan(1.5);
  expect(await page.evaluate(() => visualViewport?.scale)).toBe(viewportScale);
  const before = await frame.locator("body").evaluate(() => scrollY);
  await page.mouse.move(x, y);
  await page.mouse.wheel(0, 200);
  await expect.poll(() => frame.locator("body").evaluate(() => scrollY)).toBeGreaterThan(before);
  await client.detach();
});

test("Bridge reports a stale runner immediately and reconnects with the current script", async ({ page }) => {
  test.skip(process.env.VIEWER_TEST_MODE === "isolated", "Bridge runner version handshake.");
  const runner = "**/cc-remote-viewer-runner.js";
  await page.route(runner, route => route.fulfill({
    contentType: "text/javascript",
    body: `parent.postMessage({type:"cc-viewer-bridge-ready",v:${PROTOCOL_VERSION - 1},
      id:location.pathname.split("/").pop()},"*");`,
  }));
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  await expect(page.locator(".viewer-error-banner")).toContainText(
    "预览组件版本不一致，请刷新 cc-remote 页面后重试。", { timeout: 5000 });
  await expect(page.locator(".viewer-stage iframe")).toHaveAttribute("sandbox", "allow-scripts");
  await page.unroute(runner);
  await page.getByRole("button", { name: "重新连接", exact: true }).click();
  await expect(page.frameLocator(".viewer-stage iframe").locator("#status"))
    .toContainText("模型已加载", { timeout: 30000 });
  await expect(page.locator(".viewer-error-banner")).toHaveCount(0);
});

test("remote Viewer loads modules and binary models without exposing main cookies", async ({ page }) => {
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  const frame = page.frameLocator(".viewer-stage iframe");
  await expect(frame.locator("#status")).toContainText("模型已加载", { timeout: 30000 });
  await expect(frame.locator("#badge")).toHaveJSProperty("naturalWidth", await page.evaluate(() => devicePixelRatio > 1 ? 8 : 16));
  expect(await frame.locator("body").evaluate((node) => getComputedStyle(node).getPropertyValue("--fixture-imported").trim())).toBe("yes");
  if (!ORIGIN.includes("localhost") && !ORIGIN.includes("127.0.0.1")) expect(await page.evaluate(() => isSecureContext)).toBe(false);
  await frame.getByRole("button", { name: "旋转模型" }).click();
  await expect(frame.locator("#status")).toHaveText("已旋转 1 次");
  const state = await frame.locator("body").evaluate(() => (window as unknown as {
    viewerState: { bytes: number; parentBlocked: boolean; rotations: number; webgl: boolean };
  }).viewerState);
  expect(state.bytes).toBe(18 * 8192);
  expect(state.parentBlocked).toBe(true);
  expect(state.rotations).toBe(1);
  expect(state.webgl).toBe(true);
  const source = await page.locator("iframe").getAttribute("src");
  await page.locator("#append-message").evaluate((button: HTMLButtonElement) => button.click());
  await expect(page.locator("iframe")).toHaveAttribute("src", source!);
  await expect(frame.locator("#status")).toHaveText("已旋转 1 次");
  const canFetchMain = await frame.locator("body").evaluate(async (_, origin) => {
    try { await fetch(`${origin}/api/session`, { credentials: "include" }); return true; } catch { return false; }
  }, ORIGIN);
  expect(canFetchMain).toBe(false);
  await page.screenshot({ path: test.info().outputPath("viewer.png"), fullPage: true });
});

test("remote Viewer audio and video play and seek inside the publication", async ({ page }) => {
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^音视频页面测试/ }).click();
  const frame = page.frameLocator(".viewer-stage iframe");
  for (const id of ["sound", "movie", "source-movie"]) {
    const media = frame.locator(`#${id}`);
    await media.scrollIntoViewIfNeeded();
    await expect.poll(() => media.evaluate((el: HTMLMediaElement) => el.readyState), { timeout: 15000, message: `${id} loads` })
      .toBeGreaterThanOrEqual(2);
    await media.evaluate(async (el: HTMLMediaElement) => { el.muted = true; await el.play(); });
    await expect.poll(() => media.evaluate((el: HTMLMediaElement) => el.currentTime)).toBeGreaterThan(0.1);
    await media.evaluate((el: HTMLMediaElement) => { el.pause(); el.currentTime = el.duration / 2; });
    expect(await media.evaluate((el: HTMLMediaElement) => el.currentTime / el.duration)).toBeCloseTo(0.5, 1);
    await media.evaluate((el: HTMLMediaElement) => el.play());
    await expect.poll(() => media.evaluate((el: HTMLMediaElement) => el.seeking), { message: `${id} seeks` }).toBe(false);
    await expect.poll(() => media.evaluate((el: HTMLMediaElement) => el.currentTime - el.duration / 2)).toBeGreaterThan(0.1);
    await media.evaluate((el: HTMLMediaElement) => el.pause());
    expect(await media.evaluate((el: HTMLMediaElement) => el.error)).toBeNull();
  }
  if (process.env.VIEWER_TEST_MODE !== "isolated") {
    expect(await frame.locator("#movie").evaluate((el: HTMLMediaElement) => !!el.srcObject || el.src.startsWith("blob:"))).toBe(true);
    await expect(frame.locator("#movie")).toHaveAttribute("poster", /^blob:/);
    await expect(frame.locator("#sound")).toHaveAttribute("src", /^blob:/);
    await expect(page.locator(".viewer-stage iframe")).toHaveAttribute("sandbox", "allow-scripts");
  }
  await expect(page.locator(".viewer-error-banner")).toHaveCount(0);
});

test("sample galleries load lazily, recycle paused players and retain native controls", async ({ page }) => {
  test.skip(process.env.VIEWER_TEST_MODE === "isolated", "Bridge media scheduling.");
  test.setTimeout(60000);
  const reads: string[] = [];
  await page.routeWebSocket(/\/ws\/viewer-client(?:\?|$)/, client => {
    const server = client.connectToServer();
    client.onMessage(message => {
      const value = typeof message === "string" ? JSON.parse(message) : null;
      if (value?.type === "read" && value.method === "GET" && value.path.endsWith(".wav")) reads.push(value.path);
      server.send(message);
    });
  });
  await page.reload();
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^多音频试听测试/ }).click();
  const frame = page.frameLocator("iframe");
  await expect(frame.getByRole("heading", { name: "52 audio samples" })).toBeVisible();
  await expect(frame.locator("audio")).toHaveCount(52);
  const first = frame.locator("#sample-0");
  await expect.poll(() => first.evaluate((el: HTMLMediaElement) => el.readyState)).toBeGreaterThanOrEqual(2);
  expect(reads.length).toBeLessThanOrEqual(8);
  expect(reads).not.toContain("/viewer/samples/51.wav");
  // A real click on the native play control, not a synthetic play event.
  await first.click({ position: { x: 20, y: 20 } });
  await expect.poll(() => first.evaluate((el: HTMLMediaElement) => el.currentTime)).toBeGreaterThan(0.1);
  await first.evaluate((el: HTMLMediaElement) => { el.pause(); el.currentTime = 1.5; });
  for (let index = 8; index < 18; index++) {
    const sample = frame.locator(`#sample-${index}`);
    await sample.evaluate(async (el: HTMLMediaElement) => { el.muted = true; await el.play(); el.pause(); });
    expect(await frame.locator("audio[src]").count()).toBeLessThanOrEqual(8);
  }
  await expect(first).not.toHaveAttribute("src");
  await first.evaluate(async (el: HTMLMediaElement) => { el.muted = true; await el.play(); el.pause(); });
  expect(await first.evaluate((el: HTMLMediaElement) => el.currentTime)).toBeCloseTo(1.5, 1);
  const last = frame.locator("#sample-51");
  await last.scrollIntoViewIfNeeded();
  await expect.poll(() => last.evaluate((el: HTMLMediaElement) => el.readyState)).toBeGreaterThanOrEqual(2);
  await last.click({ position: { x: 20, y: 20 } });
  await expect.poll(() => last.evaluate((el: HTMLMediaElement) => el.currentTime)).toBeGreaterThan(0.1);
  await expect(page.locator(".viewer-error-banner")).toHaveCount(0);
});

test("lazy media honours a first click while loading and a pause before loading finishes", async ({ page }) => {
  test.skip(process.env.VIEWER_TEST_MODE === "isolated", "Bridge media scheduling.");
  const held = new Map<string, () => void>();
  let waiting = true;
  await page.routeWebSocket(/\/ws\/viewer-client(?:\?|$)/, client => {
    const server = client.connectToServer();
    client.onMessage(message => {
      const value = typeof message === "string" ? JSON.parse(message) : null;
      if (waiting && value?.type === "read" && value.path.endsWith(".wav")) {
        held.set(value.path, () => server.send(message)); return;
      }
      server.send(message);
    });
  });
  await page.reload();
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^多音频试听测试/ }).click();
  const frame = page.frameLocator("iframe");
  const first = frame.locator("#sample-0");
  await expect.poll(() => held.has("/viewer/samples/0.wav")).toBe(true);
  await first.click({ position: { x: 20, y: 20 } });
  waiting = false;
  held.forEach(release => release()); held.clear();
  await expect.poll(() => first.evaluate((el: HTMLMediaElement) => el.currentTime)).toBeGreaterThan(0.1);
  await first.evaluate((el: HTMLMediaElement) => el.pause());

  waiting = true;
  const last = frame.locator("#sample-50");
  await last.evaluate((el: HTMLMediaElement) => {
    el.muted = true;
    void el.play().then(() => el.dataset.result = "played", error => el.dataset.result = error.name);
    el.pause();
  });
  await expect.poll(() => held.has("/viewer/samples/50.wav")).toBe(true);
  waiting = false;
  held.forEach(release => release());
  await expect(last).toHaveAttribute("data-result", "AbortError");
  expect(await last.evaluate((el: HTMLMediaElement) => el.paused)).toBe(true);
  await expect(page.locator(".viewer-error-banner")).toHaveCount(0);
});

test("the media residency limit never interrupts playing samples", async ({ page }) => {
  test.skip(process.env.VIEWER_TEST_MODE === "isolated", "Bridge media scheduling.");
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^多音频试听测试/ }).click();
  const frame = page.frameLocator("iframe");
  for (let index = 12; index < 20; index++) {
    await frame.locator(`#sample-${index}`).evaluate(async (el: HTMLMediaElement) => {
      el.muted = true; el.loop = true; await el.play();
    });
  }
  const result = await frame.locator("#sample-40").evaluate(async (el: HTMLMediaElement) => {
    try { await el.play(); return "unexpected playback"; } catch (error) { return String(error); }
  });
  expect(result).toContain("请先暂停一个播放器");
  expect(await frame.locator("audio").evaluateAll(elements => elements.filter(el => !(el as HTMLMediaElement).paused).length)).toBe(8);
  await expect(frame.getByRole("heading", { name: "52 audio samples" })).toBeVisible();
  await expect(page.locator(".viewer-error-banner")).toHaveCount(0);
});

test("page scripts render before media arrives, then media uses authorized byte ranges", async ({ page }) => {
  test.skip(process.env.VIEWER_TEST_MODE === "isolated", "Bridge resource transport.");
  const held: (() => void)[] = [];
  const reads: { path: string; method: string; headers: Record<string, string> }[] = [];
  let waiting = true;
  await page.routeWebSocket(/\/ws\/viewer-client(?:\?|$)/, client => {
    const server = client.connectToServer();
    client.onMessage(message => {
      const value = typeof message === "string" ? JSON.parse(message) : null;
      if (value?.type === "read" && /\.(mp4|wav)$/.test(value.path)) {
        reads.push(value);
        if (waiting) { held.push(() => server.send(message)); return; }
      }
      server.send(message);
    });
  });
  await page.reload(); // Install the socket interceptor before the app realm.
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^音视频页面测试/ }).click();
  const frame = page.frameLocator("iframe");
  await expect(frame.getByRole("heading", { name: "Media page ready" })).toBeVisible();
  await expect.poll(() => frame.locator("body").evaluate(() => (window as unknown as { mediaPageReady: boolean }).mediaPageReady)).toBe(true);
  await expect(page.locator(".viewer-loading")).toHaveCount(0);
  await expect.poll(() => held.length).toBeGreaterThan(0);
  waiting = false;
  held.forEach(release => release());
  await expect.poll(() => frame.locator("#movie").evaluate((el: HTMLMediaElement) => el.readyState)).toBeGreaterThanOrEqual(2);
  const movieReads = reads.filter(read => read.path.endsWith(".mp4") && read.method === "GET");
  expect(movieReads.length).toBeGreaterThan(0);
  expect(movieReads.every(read => /^bytes=\d+-\d+$/.test(read.headers.range))).toBe(true);
  await expect(page.locator(".viewer-error-banner")).toHaveCount(0);
});

test("engine switches retain separate frames, playback and scroll; close and logout release leases", async ({ page }) => {
  // Fixture controls stay outside the full-screen mobile panel, like an app's
  // global navigation shortcut. Invoke those controls without closing it.
  const control = (name: string) => page.getByRole("button", { name, exact: true })
    .evaluate((button: HTMLButtonElement) => button.click());
  await control("打开远程预览");
  await page.getByRole("button", { name: /^音视频页面测试/ }).click();
  const firstFrame = await page.locator("iframe").elementHandle();
  const movie = page.frameLocator("iframe").locator("#movie");
  await expect.poll(() => movie.evaluate((el: HTMLMediaElement) => el.readyState)).toBeGreaterThanOrEqual(2);
  await movie.evaluate(async (el: HTMLMediaElement) => { el.muted = true; await el.play(); el.pause(); el.currentTime = 4; });
  await expect.poll(() => movie.evaluate((el: HTMLMediaElement) => el.seeking)).toBe(false);
  await page.frameLocator("iframe").locator("body").evaluate(() => {
    document.body.style.height = "3000px"; window.scrollTo(0, 190);
  });
  const initialScroll = await page.frameLocator("iframe").locator("body").evaluate(() => scrollY);
  const opened: string[] = [], revoked: string[] = [];
  page.on("request", request => {
    if (request.url().endsWith("/api/viewers/open")) opened.push(request.url());
    if (request.method() === "DELETE" && request.url().includes("/api/viewers/")) revoked.push(request.url());
  });
  await control("切换引擎");
  await expect(page.locator("iframe:visible")).toHaveCount(0);
  await page.keyboard.press("Escape"); // Hidden panel must not own shortcuts.
  expect(await firstFrame!.evaluate(node => node.isConnected)).toBe(true);
  await control("打开远程预览");
  await page.getByRole("button", { name: "+ 关联已有预览", exact: true }).click();
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  const robot = page.frameLocator("iframe:visible");
  await expect(robot.locator("#status")).toContainText("模型已加载");
  await robot.getByRole("button", { name: "旋转模型" }).click();
  const secondFrame = await page.locator("iframe:visible").elementHandle();
  await control("切换引擎");
  expect(await page.locator("iframe:visible").evaluate((node, old) => node === old, firstFrame)).toBe(true);
  expect(await page.frameLocator("iframe:visible").locator("#movie").evaluate((el: HTMLMediaElement) => el.currentTime)).toBeCloseTo(4, 1);
  expect(await page.frameLocator("iframe:visible").locator("body").evaluate(() => scrollY)).toBe(initialScroll);
  await control("切换引擎");
  expect(await page.locator("iframe:visible").evaluate((node, old) => node === old, secondFrame)).toBe(true);
  await expect(page.frameLocator("iframe:visible").locator("#status")).toHaveText("已旋转 1 次");
  expect(opened).toHaveLength(1);
  expect(revoked).toHaveLength(0);
  await page.locator(".remote-viewer-panel:visible").getByRole("button", { name: /^(关闭预览|返回对话)$/ }).filter({ visible: true }).click();
  await expect.poll(() => revoked.length).toBe(1);
  expect(await firstFrame!.evaluate(node => node.isConnected)).toBe(true);
  await control("退出测试");
  await expect(page.locator("iframe")).toHaveCount(0);
  await expect.poll(() => revoked.length).toBe(2);
});

test("retained previews evict the oldest frame after three scopes", async ({ page }) => {
  const control = (name: string) => page.getByRole("button", { name, exact: true })
    .evaluate((button: HTMLButtonElement) => button.click());
  const frames = [];
  const revoked: string[] = [];
  page.on("request", request => { if (request.method() === "DELETE") revoked.push(request.url()); });
  for (let i = 0; i < 4; i++) {
    if (i) await control("下个会话");
    await control("打开远程预览");
    if (i) await page.getByRole("button", { name: "+ 关联已有预览", exact: true }).click();
    await page.getByRole("button", { name: /^机器人结构/ }).click();
    await expect(page.frameLocator("iframe:visible").locator("#status")).toContainText("模型已加载");
    frames.push(await page.locator("iframe:visible").elementHandle());
  }
  await expect(page.locator("iframe")).toHaveCount(3);
  expect(await frames[0]!.evaluate(node => node.isConnected)).toBe(false);
  for (const frame of frames.slice(1)) expect(await frame!.evaluate(node => node.isConnected)).toBe(true);
  await expect.poll(() => revoked.length).toBe(1);
});

test("preview title opens a compact picker without restarting the running page", async ({ page }) => {
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^音视频页面测试/ }).click();
  const panel = page.locator(".remote-viewer-panel");
  const frame = panel.frameLocator("iframe");
  const movie = frame.locator("#movie");
  await expect.poll(() => movie.evaluate((el: HTMLVideoElement) => el.readyState), { timeout: 15000 })
    .toBeGreaterThanOrEqual(2);
  await movie.evaluate(async (el: HTMLVideoElement) => { el.muted = true; el.loop = true; await el.play(); });
  await expect.poll(() => movie.evaluate((el: HTMLVideoElement) => el.currentTime)).toBeGreaterThan(0.1);
  const originalFrame = await panel.locator("iframe").elementHandle();
  const grants: string[] = [];
  page.on("request", request => { if (request.url().endsWith("/api/viewers/open")) grants.push(request.url()); });

  const trigger = panel.getByRole("button", { name: "切换预览", exact: true });
  const picker = panel.getByRole("dialog", { name: "页面详情与切换" });
  await trigger.click();
  await expect(picker).toBeVisible();
  await expect(picker.locator('[aria-current="page"]')).toContainText("音视频页面测试");
  await expect(picker.locator('[aria-current="page"]')).toContainText("开发设备 · /viewer/media.html");
  await page.keyboard.press("Tab");
  await expect(picker.getByRole("button").first()).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(picker).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await expect(panel).toBeVisible();

  await trigger.click();
  await page.locator(".viewer-picker-backdrop").click({ position: { x: 20, y: (await panel.boundingBox())!.height - 20 } });
  await expect(picker).toHaveCount(0);
  if (page.viewportSize()!.width > 980) {
    await trigger.click();
    await page.locator(".pane p").first().click();
    await expect(picker).toHaveCount(0);
  }
  // Selecting the current page only dismisses the picker; media keeps playing.
  await trigger.click();
  await picker.getByRole("button", { name: /^音视频页面测试/ }).click();
  await expect(picker).toHaveCount(0);
  expect(await originalFrame!.evaluate(node => node.isConnected)).toBe(true);
  expect(await movie.evaluate((el: HTMLVideoElement) => el.paused)).toBe(false);
  expect(grants).toEqual([]);

  await trigger.click();
  await picker.getByRole("button", { name: /^机器人结构/ }).click();
  await expect(picker).toHaveCount(0);
  await expect(frame.locator("#status")).toContainText("模型已加载", { timeout: 30000 });
  await expect(trigger).toContainText("机器人结构");
  expect(grants).toHaveLength(1);
});

test("remote Viewer expanded iframe keeps Tab and Escape within its panel", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  const panel = page.locator(".remote-viewer-panel");
  const frame = panel.frameLocator("iframe");
  await expect(frame.locator("#status")).toContainText("模型已加载", { timeout: 30000 });
  const original = await panel.locator("iframe").elementHandle();
  await panel.getByRole("button", { name: "居中展开远程预览" }).click();
  const controls = frame.locator('button, input, a[href], [tabindex="0"]');
  await controls.last().focus();
  await page.keyboard.press("Tab");
  await expect.poll(() => panel.evaluate(node => node.contains(document.activeElement))).toBe(true);
  await expect(panel.locator("button:visible").first()).toBeFocused();
  await frame.getByRole("button", { name: "旋转模型" }).click();
  await page.keyboard.press("Shift+Tab");
  await expect.poll(() => panel.evaluate(node => node.contains(document.activeElement))).toBe(true);
  await frame.getByRole("button", { name: "旋转模型" }).click();
  // A different window cannot forge a docking request for the focused iframe.
  await page.evaluate(() => window.postMessage({ type: "cc-remote-preview-escape" }, "*"));
  await expect(panel).toHaveClass(/panel-expanded/);
  await page.keyboard.press("Escape");
  await expect(panel).not.toHaveClass(/panel-expanded/);
  await expect(panel).toBeVisible();
  await expect(frame.locator("#status")).toHaveText("已旋转 2 次");
  expect(await original!.evaluate(node => node.isConnected)).toBe(true);
  expect(await frame.locator("body").evaluate(() => {
    try { void parent.document; return false; } catch { return true; }
  })).toBe(true);
});

for (const policy of ["self", "none"]) {
  test(`isolated expanded iframe Escape survives page CSP ${policy}`, async ({ page }) => {
    test.skip(process.env.VIEWER_TEST_MODE !== "isolated", "Isolated HTML navigation coverage.");
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.getByRole("button", { name: "打开远程预览" }).click();
    await page.getByRole("button", { name: new RegExp(`^CSP ${policy}`) }).click();
    const panel = page.locator(".remote-viewer-panel");
    const frame = panel.frameLocator("iframe");
    const zoom = panel.getByRole("button", { name: "页面缩放", exact: true });
    await expect(zoom).toBeEnabled();
    await zoom.click();
    await page.getByRole("button", { name: "缩小页面", exact: true }).click();
    await expect(zoom).toHaveText("83%");
    await page.keyboard.press("Escape");
    const input = frame.getByRole("textbox", { name: "保留草稿" });
    await input.fill("中文草稿");
    await panel.getByRole("button", { name: "居中展开远程预览" }).click();
    await input.focus();
    await page.keyboard.press("Escape");
    await expect(panel).not.toHaveClass(/panel-expanded/);
    await expect(input).toHaveValue("中文草稿");
    expect(await frame.locator("body").evaluate(() => ({
      mode: document.compatMode, language: document.documentElement.lang,
      charset: document.characterSet,
      ran: Object.hasOwn(window, "inlineScriptRan"),
      policy: document.querySelector('meta[http-equiv="Content-Security-Policy"]')?.getAttribute("content"),
    }))).toEqual({ mode: "CSS1Compat", language: "zh", charset: "UTF-8",
      ran: false, policy: `script-src '${policy}'` });
  });
}

test("Bridge blocks storage and native network access, and logout clears the frame", async ({ page }) => {
  test.skip(process.env.VIEWER_TEST_MODE === "isolated", "Opaque-frame behavior is specific to Bridge.");
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  const frame = page.frameLocator(".viewer-stage iframe");
  await expect(frame.locator("#status")).toContainText("模型已加载", { timeout: 20000 });
  const protection = await frame.locator("body").evaluate(async (_, origin) => {
    let cookieBlocked = false, storageBlocked = false, networkBlocked = false;
    try { void document.cookie; } catch { cookieBlocked = true; }
    try { void localStorage.length; } catch { storageBlocked = true; }
    try {
      const original = (window as unknown as { __fixtureNativeFetch: typeof fetch }).__fixtureNativeFetch;
      await original(`${origin}/api/session`, { credentials: "include" });
    } catch { networkBlocked = true; }
    return { cookieBlocked, storageBlocked, networkBlocked };
  }, ORIGIN);
  expect(protection).toEqual({ cookieBlocked: true, storageBlocked: true, networkBlocked: true });
  // The unsupported native request produces a useful warning, not a network
  // permission prompt. Explicitly reconnect to exercise live logout revocation.
  await expect(page.locator(".viewer-error-banner")).toBeVisible();
  await page.getByRole("button", { name: "重新连接", exact: true }).click();
  await expect(frame.locator("#status")).toContainText("模型已加载", { timeout: 20000 });
  await page.evaluate(() => fetch("/api/logout", { method: "POST" }));
  await expect(page.locator(".viewer-stage iframe")).toHaveCount(0, { timeout: 5000 });
});

test("Bridge loads multi-MiB embedded data and rejects oversized HTML with a precise error", async ({ page }) => {
  test.skip(process.env.VIEWER_TEST_MODE === "isolated", "Bridge text budgets.");
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^内嵌模型测试/ }).click();
  const frame = page.frameLocator(".viewer-stage iframe");
  await expect(frame.locator("#embedded-status")).toHaveText("内嵌模型已加载 · 3145728", { timeout: 30000 });
  await expect(page.locator(".viewer-error-banner")).toHaveCount(0);
  await expect(page.locator(".viewer-stage iframe")).toHaveAttribute("sandbox", "allow-scripts");
  await frame.getByRole("button", { name: "旋转", exact: true }).click();
  const src = await page.locator("iframe").getAttribute("src");
  await page.locator("#append-message").evaluate((button: HTMLButtonElement) => button.click());
  await expect(page.locator("iframe")).toHaveAttribute("src", src!);
  await expect(frame.locator("#embedded-status")).toHaveText("已旋转");
  await page.getByRole("button", { name: "切换预览", exact: true }).click();
  await page.getByRole("button", { name: /^HTML 超限测试/ }).click();
  const error = page.locator(".viewer-error-banner");
  await expect(error).toContainText("预览 HTML过大", { timeout: 20000 });
  await expect(error).toContainText("/viewer/too-large.html");
  await expect(error).toContainText("16777217 字节");
  await expect(error).toContainText("上限 16 MiB");
  await expect(page.locator(".viewer-stage iframe")).toHaveAttribute("sandbox", "allow-scripts");
});

test("Bridge provides Range, HEAD, cancellation and bounded parallel fetches", async ({ page }) => {
  test.skip(process.env.VIEWER_TEST_MODE === "isolated", "Bridge adapter coverage.");
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  const frame = page.frameLocator(".viewer-stage iframe");
  await expect(frame.locator("#status")).toContainText("模型已加载", { timeout: 20000 });
  const result = await frame.locator("body").evaluate(async () => {
    const path = "../meshes/model.stl";
    const head = await fetch(path, { method: "HEAD" });
    const range = await fetch(path, { headers: { Range: "bytes=2-7" } });
    const cached = await fetch(path, { headers: { "If-None-Match": head.headers.get("etag")! } });
    const cancelled = await fetch(path);
    await cancelled.body!.cancel();
    const aborter = new AbortController(); aborter.abort();
    let aborted = false;
    try { await fetch(path, { signal: aborter.signal }); } catch { aborted = true; }
    const burst = await Promise.all(Array.from({ length: 43 }, async (_, index) => {
      const response = await fetch(`${path}?v=${index}`);
      return (await response.arrayBuffer()).byteLength;
    }));
    return { head: head.status, headBytes: (await head.arrayBuffer()).byteLength,
      range: range.status, rangeBytes: (await range.arrayBuffer()).byteLength,
      cached: cached.status, aborted, burst };
  });
  expect(result).toEqual({ head: 200, headBytes: 0, range: 206, rangeBytes: 6,
    cached: 304, aborted: true, burst: Array(43).fill(18 * 8192) });
  await expect(page.locator(".viewer-error-banner")).toHaveCount(0);
});

test("Bridge explains unsupported modules and failed CDN dependencies without weakening isolation", async ({ page }) => {
  test.skip(process.env.VIEWER_TEST_MODE === "isolated", "Bridge adapter coverage.");
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^动态模块限制测试/ }).click();
  await expect(page.locator(".viewer-error-banner")).toContainText("运行时动态模块地址", { timeout: 20000 });
  await expect(page.locator(".viewer-stage iframe")).toHaveAttribute("sandbox", "allow-scripts");
  await page.getByRole("button", { name: "切换预览", exact: true }).click();
  await page.route("https://esm.sh/cc-remote-fixture-dependency", (route) => route.abort());
  await page.getByRole("button", { name: /^依赖断网测试/ }).click();
  await expect(page.locator(".viewer-error-banner")).toContainText(/脚本|模块|加载|网络/, { timeout: 20000 });
  await expect(page.locator(".viewer-stage iframe")).toHaveAttribute("sandbox", "allow-scripts");
});

for (const api of ["WebSocket", "XMLHttpRequest", "EventSource", "Worker", "SharedWorker"]) {
  test(`Bridge rejects ${api} without suggesting a mode switch will connect a backend`, async ({ page }) => {
    test.skip(process.env.VIEWER_TEST_MODE === "isolated", "Bridge compiler diagnostics.");
    await page.getByRole("button", { name: "打开远程预览" }).click();
    await page.getByRole("button", { name: new RegExp(`^${api} 限制测试`) }).click();
    const error = page.locator(".viewer-error-banner");
    await expect(error).toContainText(`此页面使用 ${api}，Bridge 暂不支持。`, { timeout: 20000 });
    if (["Worker", "SharedWorker"].includes(api)) {
      await expect(error).toContainText("请使用不依赖 Worker 的静态页面。");
      await expect(error).not.toContainText("后台连接");
    } else {
      await expect(error).toContainText("当前预览不代理后台连接，切换 Isolated 模式也无法接通后台服务。");
    }
    await expect(error).not.toContainText("请使用 Isolated 模式");
    await expect(page.locator(".viewer-stage iframe")).toHaveAttribute("sandbox", "allow-scripts");
  });
}

test("an unrelated opaque frame cannot steal the Bridge initialization port", async ({ page }) => {
  test.skip(process.env.VIEWER_TEST_MODE === "isolated", "Bridge handshake coverage.");
  let releaseRunner!: () => void;
  const delayed = new Promise<void>((resolve) => { releaseRunner = resolve; });
  await page.route("**/__cc_viewer/bridge/*", async (route) => { await delayed; await route.continue(); });
  await page.getByRole("button", { name: "打开远程预览" }).click();
  const opened = page.waitForResponse((response) => response.url().endsWith("/api/viewers/open"));
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  const grant = await (await opened).json();
  try {
    const stolen = await page.evaluate(async ({ id, version }: { id: string; version: number }) => {
      const attack = document.createElement("iframe");
      attack.sandbox.add("allow-scripts");
      attack.hidden = true;
      const result = new Promise<boolean>((resolve) => {
        let received = false;
        const listener = (event: MessageEvent) => {
          if (event.source === attack.contentWindow && event.data === "port-stolen") received = true;
          if (event.source === attack.contentWindow && event.data === "probe-done") {
            window.removeEventListener("message", listener); resolve(received);
          }
        };
        window.addEventListener("message", listener);
      });
      attack.srcdoc = `<script>
        addEventListener('message',e=>{if(e.ports.length)parent.postMessage('port-stolen','*')});
        parent.postMessage({type:'cc-viewer-bridge-ready',id:${JSON.stringify(id)},v:${version}},'*');
        setTimeout(()=>parent.postMessage('probe-done','*'),250);
        </script>`;
      document.body.append(attack);
      const value = await result;
      attack.remove();
      return value;
    }, { id: grant.id, version: PROTOCOL_VERSION });
    expect(stolen).toBe(false);
  } finally { releaseRunner(); }
  await expect(page.frameLocator(".viewer-stage iframe").locator("#status"))
    .toContainText("模型已加载", { timeout: 20000 });
});

test("remote Viewer survives refresh, keeps session scope and closes without deleting files", async ({ page, isMobile }) => {
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  await expect(page.frameLocator("iframe").locator("#status")).toContainText("模型已加载", { timeout: 30000 });
  const oldSource = await page.locator("iframe").getAttribute("src");
  await page.reload();
  await expect(page.frameLocator("iframe").locator("#status")).toContainText("模型已加载", { timeout: 30000 });
  expect(await page.locator("iframe").getAttribute("src")).not.toBe(oldSource);
  const stored = await page.evaluate(() => sessionStorage.getItem("cc-remote:viewer-panels-v1"));
  expect(stored).not.toContain("origin");
  expect(stored).not.toContain("cookie");
  if (!isMobile) {
    await page.frameLocator("iframe").getByRole("button", { name: "旋转模型" }).click();
    const frame = await page.locator("iframe").elementHandle();
    await page.getByRole("button", { name: "切换会话", exact: true }).click();
    await expect(page.getByRole("complementary", { name: "远程预览" })).toHaveCount(0);
    await page.getByRole("button", { name: "切换会话", exact: true }).click();
    await expect(page.frameLocator("iframe").locator("#status")).toHaveText("已旋转 1 次");
    expect(await page.locator("iframe").evaluate((node, old) => node === old, frame)).toBe(true);
  }
  await page.getByRole("button", { name: isMobile ? "返回对话" : "关闭预览", exact: true }).click();
  await expect(page.locator(".remote-viewer-panel")).toHaveCount(0);
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await expect(page.getByRole("button", { name: /^机器人结构/ })).toBeVisible();
});

test("remote Viewer uses one desktop side panel and fills the mobile viewport", async ({ page, isMobile }) => {
  await page.getByRole("button", { name: "打开远程预览" }).click();
  const panel = page.locator(".remote-viewer-panel");
  await expect(panel).toBeVisible();
  const box = await panel.boundingBox();
  const viewport = page.viewportSize()!;
  expect(box).not.toBeNull();
  if (isMobile) {
    expect(box!.x).toBe(0);
    expect(box!.width).toBe(viewport.width);
    expect(box!.height).toBeCloseTo(viewport.height, 2);
    await expect(page.getByRole("button", { name: "返回对话", exact: true })).toBeVisible();
  } else {
    expect(box!.width).toBeLessThan(viewport.width * 0.6);
    await page.getByRole("button", { name: "居中展开远程预览" }).click();
    const expanded = (await panel.boundingBox())!;
    expect(expanded.width).toBeGreaterThan(box!.width);
    expect(expanded.x + expanded.width / 2).toBeCloseTo(viewport.width / 2, 1);
    await expect(panel).toHaveAttribute("aria-modal", "true");
    await page.getByRole("button", { name: "收回右侧" }).click();
    expect((await panel.boundingBox())!.width).toBeLessThan(viewport.width * 0.6);
  }
});

test("remote Viewer reacquires authorization after a cached page is restored", async ({ page }) => {
  const bindings: string[] = [];
  page.on("websocket", (socket) => {
    if (!socket.url().endsWith("/ws/viewer-client")) return;
    socket.on("framesent", ({ payload }) => {
      if (typeof payload !== "string") return;
      const message = JSON.parse(payload);
      if (message.type === "bind") bindings.push(message.id);
    });
  });
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  await expect(page.frameLocator("iframe").locator("#status")).toContainText("模型已加载", { timeout: 30000 });
  const source = await page.locator("iframe").getAttribute("src");
  const oldBindings = [...bindings];
  let release!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/viewers/open", async (route) => {
    await held;
    await route.fallback();
  });
  const opening = page.waitForRequest((request) => request.url().endsWith("/api/viewers/open"));
  const opened = page.waitForResponse((response) => response.url().endsWith("/api/viewers/open"));
  try {
    await page.evaluate(() => {
      window.dispatchEvent(new PageTransitionEvent("pagehide", { persisted: true }));
      window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true }));
    });
    await opening;
    // The catalog is back, but the replacement lease is not. Do not remount
    // the revoked frame in this gap or let its 403 retire the new lease.
    await expect(page.locator("iframe")).toHaveCount(0);
    expect(bindings).toEqual(oldBindings);
  } finally { release(); }
  const replacement = await (await opened).json();
  await expect(page.frameLocator("iframe").locator("#status")).toContainText("模型已加载", { timeout: 30000 });
  await expect(page.locator("iframe")).not.toHaveAttribute("src", source!);
  await expect(page.getByRole("alert")).toHaveCount(0);
  if (replacement.mode === "bridge") expect(bindings).toEqual([...oldBindings, replacement.id]);
  await page.frameLocator("iframe").getByRole("button", { name: "旋转模型" }).click();
  await expect(page.frameLocator("iframe").locator("#status")).toHaveText("已旋转 1 次");
});

test("session page discovery is explicit, durable and leaves cloud links alone", async ({ page, isMobile }) => {
  const sid = `artifact-${Date.now()}`;
  await page.goto(`${ORIGIN}/tests/remote-viewer.html?sid=${sid}`);
  await page.getByRole("button", { name: "显示页面产物" }).click();
  const preview = page.getByRole("button", { name: "查看页面", exact: true });
  await expect(preview).toBeVisible();
  await expect(preview).toHaveCount(1);
  await expect(page.getByRole("link", { name: "GitHub", exact: true }))
    .toHaveAttribute("href", "https://github.com/demo/index.html");
  await expect(page.locator(".remote-viewer-panel")).toHaveCount(0);
  await preview.click();
  const frame = page.frameLocator(".viewer-stage iframe");
  await expect(frame.locator("#status")).toContainText("模型已加载", { timeout: 20000 });
  await frame.getByRole("button", { name: "旋转模型" }).click();
  await page.locator("#append-message").evaluate((button: HTMLButtonElement) => button.click());
  await expect(frame.locator("#status")).toHaveText("已旋转 1 次");
  await expect(page.locator(".remote-viewer-panel")).toHaveCSS("opacity", "1");
  await page.screenshot({ path: test.info().outputPath("session-page-preview.png"), fullPage: true });
  await page.getByRole("button", { name: isMobile ? "返回对话" : "关闭预览", exact: true }).click();
  // Browser panel memory is not the source of session associations.
  await page.evaluate(() => sessionStorage.clear());
  await page.reload();
  await page.getByRole("button", { name: "打开远程预览", exact: true }).click();
  await expect(page.getByRole("button", { name: /^机器人结构/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /^动态模块限制测试/ })).toHaveCount(0);
  await page.getByRole("button", { name: "移除关联 机器人结构", exact: true }).click();
  await expect(page.getByText("本会话还没有页面", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "+ 关联已有预览", exact: true }).click();
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  await expect(frame.locator("#status")).toContainText("模型已加载", { timeout: 20000 });
});

test("opening the picker in another session does not copy the global catalog", async ({ page }) => {
  await page.goto(`${ORIGIN}/tests/remote-viewer.html?sid=empty-${Date.now()}`);
  await page.getByRole("button", { name: "打开远程预览", exact: true }).click();
  await expect(page.getByText("本会话还没有页面", { exact: true })).toBeVisible();
  await expect(page.locator(".viewer-site")).toHaveCount(0);
  await page.getByRole("button", { name: "+ 关联已有预览", exact: true }).click();
  await expect(page.getByRole("button", { name: /^机器人结构/ })).toBeVisible();
  await page.getByRole("button", { name: "返回本会话", exact: true }).click();
  await expect(page.locator(".viewer-site")).toHaveCount(0);
});

test("page entry retries while the answer is streaming without a session switch", async ({ page }) => {
  let attempts = 0;
  await page.route("**/api/viewers/pages", async (route) => {
    if (route.request().postDataJSON()?.action === "resolve" && ++attempts === 1) {
      await route.fulfill({ status: 503, json: { error: "device_offline" } });
    } else await route.continue();
  });
  await page.goto(`${ORIGIN}/tests/remote-viewer.html?sid=stream-${Date.now()}`);
  await page.getByRole("button", { name: "开始流式页面产物", exact: true }).click();
  await expect(page.locator("p").filter({ hasText: "页面已做好：本地页面" })).toBeVisible();
  const preview = page.getByRole("button", { name: "查看页面", exact: true });
  await expect(preview).toBeVisible({ timeout: 10000 });
  await expect(page.getByRole("button", { name: "完成回复", exact: true })).toBeVisible();
  expect(attempts).toBe(2);
  await expect(preview).toHaveCSS("border-top-width", "0px");
  await expect(preview).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  await expect(page.locator(".page-preview-link > span")).toHaveCount(0);
  await preview.click();
  await expect(page.frameLocator("iframe").locator("#status")).toContainText("模型已加载", { timeout: 20000 });
});

test("finishing an answer while page discovery is in flight still brings up its entry", async ({ page }) => {
  let attempts = 0;
  let started!: () => void;
  const firstRequest = new Promise<void>((resolve) => { started = resolve; });
  let release!: () => void;
  const delayed = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/viewers/pages", async (route) => {
    if (route.request().postDataJSON()?.action === "resolve" && ++attempts === 1) {
      started();
      await delayed;
      await route.fulfill({ json: { pages: [] } });
    } else await route.continue();
  });
  await page.goto(`${ORIGIN}/tests/remote-viewer.html?sid=boundary-${Date.now()}`);
  try {
    await page.getByRole("button", { name: "开始流式页面产物", exact: true }).click();
    await firstRequest;
    await expect(page.getByRole("button", { name: "查看页面", exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "完成回复", exact: true }).click();
  } finally { release(); }
  await expect(page.getByRole("button", { name: "查看页面", exact: true })).toBeVisible({ timeout: 10000 });
  expect(attempts).toBe(2);
  await expect(page.getByRole("button", { name: "完成回复", exact: true })).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath("lightweight-page-entry.png"), fullPage: true });
});

test("a discovery superseded by a list refresh is retried instead of remembered as invisible", async ({ page }) => {
  let attempts = 0;
  let started!: () => void;
  const firstRequest = new Promise<void>((resolve) => { started = resolve; });
  let release!: () => void;
  const delayed = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/viewers/pages", async (route) => {
    const action = route.request().postDataJSON()?.action;
    if (action === "resolve" && ++attempts === 1) {
      const response = await route.fetch();
      started();
      await delayed;
      await route.fulfill({ response });
    } else if (action === "list" && attempts === 1) {
      await route.fulfill({ json: { pages: [] } });
    } else await route.continue();
  });
  await page.goto(`${ORIGIN}/tests/remote-viewer.html?sid=refresh-race-${Date.now()}`);
  try {
    await page.getByRole("button", { name: "显示页面产物", exact: true }).click();
    await firstRequest;
    const refreshed = page.waitForResponse((response) => response.url().endsWith("/api/viewers/pages")
      && response.request().postDataJSON()?.action === "list");
    await page.getByRole("button", { name: "刷新页面关联", exact: true }).click();
    await refreshed;
  } finally { release(); }
  await expect(page.getByRole("button", { name: "查看页面", exact: true })).toBeVisible({ timeout: 10000 });
  expect(attempts).toBe(2);
});

test("page metadata never shifts the completion footer on cold load or A-B-A focus", async ({ page }) => {
  let release!: () => void;
  let delayed = new Promise<void>((resolve) => { release = resolve; });
  let hold = true;
  const pages = [{ id: "layout-page", machine_id: "render-device", site_id: "robot", entry: "/viewer/index.html",
    label: "页面", references: ["viewer/index.html"], turn_ids: ["page-turn"], available: true }];
  await page.route("**/api/viewers/pages", async (route) => {
    if (hold) await delayed;
    const scope = route.request().postDataJSON().scope;
    await route.fulfill({ json: { pages: scope.sid === "session-a" ? pages : [] } });
  });
  await page.goto(`${ORIGIN}/tests/remote-viewer.html?sid=session-a&chat-layout=1`);
  const mark = page.locator(".turn-done-mark");
  const preview = page.getByRole("button", { name: "查看页面", exact: true });
  try {
    await page.getByRole("button", { name: "显示页面产物", exact: true }).click();
    await expect(mark).toBeVisible();
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    const initial = await mark.boundingBox();
    expect(initial).not.toBeNull();
    await expect(preview).toHaveCount(0);
    hold = false; release();
    await expect(preview).toBeVisible();
    expect(Math.abs((await mark.boundingBox())!.y - initial!.y)).toBeLessThan(1);
    expect(await page.locator(".ai-meta").evaluate((el) => el.getBoundingClientRect().height)).toBe(22);

    hold = true;
    delayed = new Promise<void>((resolve) => { release = resolve; });
    await page.getByRole("button", { name: "切换会话", exact: true }).click();
    await expect(page.getByTestId("current-sid")).toHaveText("session-b");
    await expect(preview).toHaveCount(0);
    // The chat/scroll root is retained; individual session rows may re-key.
    await page.locator(".thread-shell").evaluate((el) => el.setAttribute("data-retained", "yes"));
    await page.getByRole("button", { name: "切换会话", exact: true }).click();
    await expect(page.getByTestId("current-sid")).toHaveText("session-a");
    await expect(preview).toBeVisible(); // list/resolve are still deliberately blocked
    await expect(page.locator(".thread-shell")).toHaveAttribute("data-retained", "yes");
    const restored = (await mark.boundingBox())!.y;
    hold = false; release();
    await expect(preview).toBeVisible();
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    expect(Math.abs((await mark.boundingBox())!.y - restored)).toBeLessThan(1);
    await page.screenshot({ path: test.info().outputPath("stable-page-footer.png"), fullPage: true });
  } finally { hold = false; release(); }
});

test("multiple page entries use one footer slot and an out-of-flow menu", async ({ page }) => {
  await page.route("**/api/viewers/pages", async (route) => route.fulfill({ json: { pages: [1, 2, 3].map((id) => ({
    id: `layout-${id}`, machine_id: "render-device", site_id: "robot", entry: `/viewer/${id}.html`,
    label: `页面 ${id}`, references: [], turn_ids: ["page-turn"], available: true,
  })) } }));
  await page.goto(`${ORIGIN}/tests/remote-viewer.html?sid=session-a&chat-layout=1`);
  await page.getByRole("button", { name: "显示页面产物", exact: true }).click();
  const mark = page.locator(".turn-done-mark");
  const trigger = page.locator(".page-preview-menu > summary");
  await expect(trigger).toBeVisible();
  const before = (await mark.boundingBox())!.y;
  await trigger.click();
  await expect(page.locator(".page-preview-options button")).toHaveCount(3);
  expect(Math.abs((await mark.boundingBox())!.y - before)).toBeLessThan(1);
});

test("home static-server link becomes a session preview without registration or global clutter", async ({ page }) => {
  await page.goto(`${ORIGIN}/tests/remote-viewer.html?sid=home-${Date.now()}`);
  await page.getByRole("button", { name: "显示未登记页面", exact: true }).click();
  await expect(page.getByRole("button", { name: "查看页面", exact: true })).toBeVisible({ timeout: 20000 });
  // A confirmed local link itself is interactive too; cloud links stay native.
  await page.getByRole("link", { name: "本地页面", exact: true }).click();
  const frame = page.frameLocator(".viewer-stage iframe");
  await expect(frame.locator("#status")).toContainText("模型已加载", { timeout: 30000 });
  await frame.getByRole("button", { name: "旋转模型" }).click();
  await expect(frame.locator("#status")).toHaveText("已旋转 1 次");
  const catalog = await page.evaluate(async () => (await (await fetch("/api/viewers")).json()).sites);
  expect(catalog.some((site: { id: string }) => site.id.startsWith("auto-"))).toBe(false);
  await page.reload();
  await expect(frame.locator("#status")).toContainText("模型已加载", { timeout: 30000 });
  await expect(page.locator(".viewer-error-banner")).toHaveCount(0);
});
