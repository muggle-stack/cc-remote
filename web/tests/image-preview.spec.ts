import { expect, test, type Locator, type Page } from "@playwright/test";

async function open(page: Page, height = 900, centered = false) {
  await page.addInitScript(height => {
    (window as unknown as { svgFixture: string }).svgFixture = btoa(
      `<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="${height}"><rect width="1200" height="${height}" fill="blue"/></svg>`);
  }, height);
  await page.goto(`/tests/history-browser.html?artifact-svg=1&image-gestures=1${centered ? "&image-centered=1" : ""}`);
  const stage = page.locator(".artifact-image-stage");
  await expect(stage.locator("img")).toBeVisible();
  await expect.poll(() => stage.locator("img").evaluate(node => (node as HTMLImageElement).naturalWidth)).toBe(1200);
  // The lazy download control changes header geometry when it mounts.
  await expect(page.getByRole("link", { name: "下载原文件" })).toBeVisible();
  return stage;
}
const matrix = (stage: Locator) => stage.locator("img").evaluate(node => {
  const m = new DOMMatrix(getComputedStyle(node).transform);
  return { scale: m.a, x: m.e, y: m.f };
});

for (const centered of [false, true]) {
  test(`artifact-svg first visible frame already has readable width in ${centered ? "centered" : "docked"} view`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.addInitScript(() => {
      const frames: { height: number; widthError: number; topError: number }[] = [];
      (window as unknown as { imageFrames: typeof frames }).imageFrames = frames;
      const sample = () => {
        const image = document.querySelector<HTMLImageElement>(".artifact-image-stage img");
        if (image?.complete && image.naturalWidth && getComputedStyle(image).visibility === "visible") {
          const rect = image.getBoundingClientRect(), viewport = image.parentElement!.getBoundingClientRect();
          frames.push({ height: image.naturalHeight,
            widthError: Math.abs(rect.width - Math.min(image.naturalWidth, viewport.width)),
            topError: Math.abs(rect.top - viewport.top) });
        }
        requestAnimationFrame(sample);
      };
      requestAnimationFrame(sample);
    });
    const stage = await open(page, 6000, centered);
    const frames = () => page.evaluate(() => (window as unknown as {
      imageFrames: { height: number; widthError: number; topError: number }[];
    }).imageFrames);
    await expect.poll(async () => (await frames()).filter(frame => frame.height === 6000).length).toBeGreaterThan(2);
    if (centered) {
      // The fixture's change-file button is behind the modal; activate only the
      // fixture control without changing the preview's presentation.
      await page.getByRole("button", { name: "换一张图片" }).evaluate(node => (node as HTMLButtonElement).click());
    } else {
      await page.getByRole("button", { name: "换一张图片" }).click();
    }
    await expect.poll(async () => (await frames()).filter(frame => frame.height === 1200).length).toBeGreaterThan(2);
    const initialFrames = (await frames()).filter(frame => frame.height === 6000).length;
    await page.getByRole("button", { name: "换一张图片" }).evaluate(node => (node as HTMLButtonElement).click());
    await expect.poll(() => stage.locator("img").evaluate(node => (node as HTMLImageElement).naturalHeight)).toBe(6000);
    await expect.poll(async () => (await frames()).filter(frame => frame.height === 6000).length).toBeGreaterThan(initialFrames + 2);
    expect((await frames()).filter(frame => frame.widthError > 1)).toEqual([]);
    // The long image must open at the top, including when returning to it.
    expect((await frames()).filter(frame => frame.height === 6000 && frame.topError > 1)).toEqual([]);
  });
}

async function wheel(stage: Locator, deltaY: number, ctrlKey = true, deltaX = 0) {
  const rect = await stage.boundingBox();
  await stage.dispatchEvent("wheel", { cancelable: true, ctrlKey, deltaY, deltaX,
    clientX: rect!.x + rect!.width / 2, clientY: rect!.y + rect!.height / 2 });
}

test("artifact-svg file preview zooms and pans in docked and centered views", async ({ page }) => {
  const stage = await open(page);
  for (const centered of [false, true]) {
    if (centered) {
      await page.getByRole("button", { name: "居中展开文件预览", exact: true }).click();
      await expect(page.getByRole("dialog", { name: "文件预览", exact: true })).toBeVisible();
    }
    await wheel(stage, -350);
    await expect.poll(async () => (await matrix(stage)).scale).toBeGreaterThan(1.9);
    await wheel(stage, 70, false, 60);
    await expect.poll(async () => (await matrix(stage)).x).toBeLessThan(-20);
    await wheel(stage, 10_000);
    await expect.poll(() => matrix(stage)).toEqual({ scale: 1, x: 0, y: 0 });
    expect(await page.evaluate(() => window.scrollX)).toBe(0);
  }
});

test("artifact-svg Safari gestures zoom once and reset for a different file", async ({ page }) => {
  const stage = await open(page);
  const initial = await matrix(stage);
  const gesture = async (type: string, scale: number) => stage.evaluate((node, { type, scale }) => {
    const event = new Event(type, { bubbles: true, cancelable: true });
    const r = node.getBoundingClientRect();
    Object.assign(event, { scale, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2 });
    return !node.dispatchEvent(event);
  }, { type, scale });
  expect(await gesture("gesturestart", 1)).toBe(true);
  expect(await gesture("gesturechange", 1.5)).toBe(true);
  await expect.poll(async () => (await matrix(stage)).scale).toBeCloseTo(initial.scale * 2.25, 4);
  await wheel(stage, -350);
  await expect.poll(async () => (await matrix(stage)).scale).toBeCloseTo(initial.scale * 2.25, 4);
  await gesture("gestureend", 1.5);
  await page.getByRole("button", { name: "居中展开文件预览", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "文件预览", exact: true })).toBeVisible();
  await expect.poll(async () => (await matrix(stage)).scale).toBeCloseTo(initial.scale * 2.25, 4);
  await page.getByRole("button", { name: "收回右侧", exact: true }).click();
  // The fixture control is behind the full-screen preview on mobile.
  await page.getByRole("button", { name: "换一张图片" }).evaluate((button: HTMLButtonElement) => button.click());
  await expect.poll(() => stage.locator("img").evaluate(node => (node as HTMLImageElement).naturalHeight)).toBe(1200);
  await expect.poll(() => stage.evaluate(node => {
    const image = node.querySelector("img")!.getBoundingClientRect(), viewport = node.getBoundingClientRect();
    return Math.abs(image.width - viewport.width) < 1 && image.top >= viewport.top - 1;
  })).toBe(true);
});

test("artifact-svg long files open at readable width from the top and scroll to the end", async ({ page }) => {
  // A very tall image needs more than 4x its contained size to fill the panel.
  await page.setViewportSize({ width: 1440, height: 900 });
  const stage = await open(page, 6000);
  const geometry = () => stage.evaluate(node => {
    const viewport = node.getBoundingClientRect();
    const image = node.querySelector("img")!.getBoundingClientRect();
    return { widthError: Math.abs(image.width - viewport.width),
      topError: Math.abs(image.top - viewport.top),
      bottomError: Math.abs(image.bottom - viewport.bottom) };
  });
  await expect.poll(async () => (await geometry()).widthError).toBeLessThan(1);
  await expect.poll(async () => (await geometry()).topError).toBeLessThan(1);
  await page.getByRole("button", { name: "居中展开文件预览", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "文件预览", exact: true })).toBeVisible();
  await expect.poll(async () => (await geometry()).widthError).toBeLessThan(1);
  await expect.poll(async () => (await geometry()).topError).toBeLessThan(1);
  const initial = await matrix(stage);
  expect(initial.scale).toBeGreaterThan(4);
  await wheel(stage, 120, false);
  await expect.poll(async () => (await matrix(stage)).y).toBeCloseTo(initial.y - 120, 1);
  await wheel(stage, 100_000, false);
  await expect.poll(async () => (await geometry()).bottomError).toBeLessThan(1);
  // A modest trackpad pinch now changes size noticeably, in both directions.
  await wheel(stage, -40);
  await expect.poll(async () => (await matrix(stage)).scale).toBeGreaterThan(initial.scale * 1.4);
  await wheel(stage, 40);
  await expect.poll(async () => (await matrix(stage)).scale).toBeCloseTo(initial.scale, 3);
  await wheel(stage, 10_000);
  await expect.poll(() => matrix(stage)).toEqual({ scale: 1, x: 0, y: 0 });
  const fits = await stage.evaluate(node => {
    const viewport = node.getBoundingClientRect(), image = node.querySelector("img")!.getBoundingClientRect();
    return image.top >= viewport.top - 1 && image.bottom <= viewport.bottom + 1
      && image.left >= viewport.left - 1 && image.right <= viewport.right + 1;
  });
  expect(fits).toBe(true);
  expect(await page.evaluate(() => ({ x: scrollX, y: scrollY, zoom: visualViewport?.scale })))
    .toEqual({ x: 0, y: 0, zoom: 1 });
});

test("artifact-svg native two-finger touch zooms without moving the page", async ({ browser, browserName }) => {
  test.skip(browserName !== "chromium", "CDP supplies real multi-touch input to Chromium");
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  try {
    const page = await context.newPage();
    const stage = await open(page);
    const cdp = await context.newCDPSession(page);
    for (const centered of [false, true]) {
      if (centered) {
        await page.getByRole("button", { name: "居中展开文件预览", exact: true }).click();
        await expect(page.getByRole("dialog", { name: "文件预览", exact: true })).toBeVisible();
      }
      const rect = (await stage.boundingBox())!;
      const x = rect.x + rect.width / 2, y = rect.y + Math.min(250, rect.height / 2);
      const fingers = (distance: number) => [{ x: x - distance, y, id: 1 }, { x: x + distance, y, id: 2 }];
      await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: fingers(40) });
      await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: fingers(100) });
      await expect.poll(async () => (await matrix(stage)).scale).toBeGreaterThan(2);
      await cdp.send("Input.dispatchTouchEvent", { type: "touchCancel", touchPoints: [] });
      const beforePan = await matrix(stage);
      await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y, id: 1 }] });
      await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: x - 50, y, id: 1 }] });
      await expect.poll(async () => (await matrix(stage)).x).toBeLessThan(beforePan.x - 20);
      await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
      // A cancelled touch releases ownership; a fresh pinch can shrink again.
      await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: fingers(100) });
      await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: fingers(30) });
      await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
      await expect.poll(() => matrix(stage)).toEqual({ scale: 1, x: 0, y: 0 });
    }
    expect(await page.evaluate(() => ({ x: window.scrollX, y: window.scrollY,
      zoom: window.visualViewport?.scale }))).toEqual({ x: 0, y: 0, zoom: 1 });
  } finally { await context.close(); }
});

test("artifact-svg touch pointers own an overlapping Safari pinch", async ({ page }) => {
  const stage = await open(page);
  const initial = await matrix(stage);
  const result = await stage.evaluate(async node => {
    const element = node as HTMLElement;
    // WebKit automation has no multi-touch injector; exercise its pointer and
    // gesture event streams together, including pointer-capture cancellation.
    Object.defineProperties(element, {
      setPointerCapture: { value: () => {} },
      hasPointerCapture: { value: () => false },
    });
    const rect = element.getBoundingClientRect();
    const point = (type: string, id: number, x: number) => element.dispatchEvent(
      new PointerEvent(type, { pointerType: "touch", pointerId: id, bubbles: true,
        clientX: rect.x + x, clientY: rect.y + 200, cancelable: true }));
    point("pointerdown", 1, 120); point("pointerdown", 2, 200);
    const gesture = (type: string, scale: number) => {
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.assign(event, { scale, clientX: rect.x + 160, clientY: rect.y + 200 });
      return !element.dispatchEvent(event);
    };
    const prevented = gesture("gesturestart", 1);
    point("pointermove", 1, 80); point("pointermove", 2, 240);
    gesture("gesturechange", 3);
    await new Promise(requestAnimationFrame);
    const scale = new DOMMatrix(element.querySelector("img")!.style.transform).a;
    point("pointercancel", 1, 80); point("pointercancel", 2, 240);
    gesture("gestureend", 3);
    return { scale, prevented };
  });
  expect(result.prevented).toBe(true);
  expect(result.scale).toBeCloseTo(initial.scale * 2, 4);
  await wheel(stage, 10_000);
  await expect.poll(() => matrix(stage)).toEqual({ scale: 1, x: 0, y: 0 });
});
