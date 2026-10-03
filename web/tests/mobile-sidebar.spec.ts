import { expect, test, type Page } from "@playwright/test";

async function touch(page: Page, type: string, x: number, y: number, time: number,
  selector = ".thread", count = 1) {
  return page.locator(selector).evaluate((target, args) => {
    const point = { identifier: 1, target, clientX: args.x, clientY: args.y };
    const touches = args.type === "touchend" || args.type === "touchcancel" ? []
      : args.count === 2 ? [point, { identifier: 2, target,
        clientX: args.x + 30, clientY: args.y + 30 }] : [point];
    // WebKit does not expose a constructible Touch; use the native event shape.
    const event = new Event(args.type, { bubbles: true, cancelable: true });
    Object.defineProperties(event, {
      touches: { value: touches }, targetTouches: { value: touches },
      changedTouches: { value: [point] }, timeStamp: { value: args.time },
    });
    target.dispatchEvent(event);
    return event.defaultPrevented;
  }, { type, x, y, time, count });
}

async function bounds(page: Page) {
  return page.evaluate(() => {
    const rect = (selector: string) => {
      const r = document.querySelector(selector)!.getBoundingClientRect();
      return { x: r.x, width: r.width };
    };
    return { pane: rect(".pane"), sidebar: rect(".sessions"),
      header: rect(".c-head"), composer: rect(".composer"),
      scroll: document.querySelector(".thread")!.scrollTop };
  });
}

async function settled(page: Page, open: boolean) {
  const shell = page.locator(".shell");
  if (open) await expect(shell).toHaveClass(/sidebar-open/);
  else await expect(shell).not.toHaveClass(/sidebar-open/);
  await expect(shell).not.toHaveAttribute("data-sidebar-motion");
  await expect.poll(async () => {
    const b = await bounds(page);
    return Math.abs(b.pane.x - (open ? b.sidebar.width : 0));
  }).toBeLessThan(1);
}

test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/tests/history-browser.html?mobile-sidebar=1");
  await expect(page.locator(".thread")).toBeVisible();
  // ChatView first restores its latest-page position after font/layout frames.
  await page.waitForTimeout(500);
  await page.locator(".thread").dispatchEvent("wheel", { deltaY: -240 });
  await page.locator(".thread").evaluate(node => { node.scrollTop = 240; });
  await expect.poll(() => page.locator(".thread").evaluate(node => node.scrollTop)).toBe(240);
});

test("mobile sidebar tracks the finger without reflow and survives streaming renders", async ({ page }) => {
  const before = await bounds(page);
  await touch(page, "touchstart", 60, 280, 1000);
  expect(await touch(page, "touchmove", 170, 283, 1200)).toBe(true);
  await expect.poll(async () => (await bounds(page)).pane.x).toBe(110);
  const during = await bounds(page);
  expect(during.pane.width).toBe(before.pane.width);
  expect(during.header.x - before.header.x).toBe(110);
  expect(during.composer.x - before.composer.x).toBe(110);
  expect(during.scroll).toBe(before.scroll);
  expect(during.sidebar.x).toBeLessThan(0);
  // The shell class changes just as App rerenders during streaming/panel changes.
  await page.evaluate(() => window.dispatchEvent(new Event("sidebar-fixture-stream")));
  await expect(page.locator(".shell")).toHaveAttribute("data-revision", "1");
  await expect(page.locator(".shell")).toHaveAttribute("data-sidebar-motion", "dragging");
  expect((await bounds(page)).pane.x).toBe(110);
  await touch(page, "touchmove", 280, 283, 1400);
  await touch(page, "touchend", 280, 283, 1600);
  await settled(page, true);
  expect((await bounds(page)).pane.width).toBeCloseTo(390, 2);
  expect(await page.locator(".pane").evaluate(node => (node as HTMLElement).inert)).toBe(true);
  await page.screenshot({ path: test.info().outputPath("sidebar-open.png") });
});

test("mobile sidebar settles short drags, directional flicks and interrupted gestures", async ({ page }) => {
  await touch(page, "touchstart", 50, 280, 1000);
  await touch(page, "touchmove", 140, 280, 1200);
  await touch(page, "touchend", 140, 280, 1400);
  await settled(page, false);
  // A short fast flick opens even before halfway.
  await touch(page, "touchstart", 50, 280, 2000);
  await touch(page, "touchmove", 80, 280, 2020);
  await touch(page, "touchend", 110, 280, 2040);
  await settled(page, true);
  // Reverse flick from the drawer closes, preserving the normal short-drag case.
  await touch(page, "touchstart", 250, 280, 3000, ".sessions");
  await touch(page, "touchmove", 220, 280, 3020, ".sessions");
  await touch(page, "touchend", 190, 280, 3040, ".sessions");
  await settled(page, false);
  for (const cancelType of ["touchcancel", "multitouch"]) {
    await touch(page, "touchstart", 50, 280, 4000);
    await touch(page, "touchmove", 250, 280, 4200);
    if (cancelType === "touchcancel") await touch(page, "touchcancel", 250, 280, 4250);
    else await touch(page, "touchstart", 250, 280, 4250, ".thread", 2);
    await settled(page, false);
  }
});

test("mobile sidebar preserves vertical scrolling, horizontal scrollers, editors and selection", async ({ page }) => {
  await touch(page, "touchstart", 50, 280, 1000);
  expect(await touch(page, "touchmove", 60, 310, 1020)).toBe(false);
  expect(await touch(page, "touchmove", 270, 315, 1200)).toBe(false);
  await touch(page, "touchend", 280, 315, 1400);
  await settled(page, false);
  for (const selector of ['textarea', '[data-testid="sidebar-locked"]', '[data-testid="sidebar-horizontal"]']) {
    await touch(page, "touchstart", 50, 280, 2000, selector);
    expect(await touch(page, "touchmove", 280, 280, 2200, selector)).toBe(false);
    await touch(page, "touchend", 280, 280, 2400, selector);
    await settled(page, false);
  }
  await page.locator(".thread").evaluate(node => {
    const range = document.createRange();
    range.selectNodeContents(node);
    window.getSelection()!.removeAllRanges(); window.getSelection()!.addRange(range);
  });
  await touch(page, "touchstart", 50, 280, 3000);
  expect(await touch(page, "touchmove", 280, 280, 3200)).toBe(false);
  await touch(page, "touchend", 280, 280, 3400);
  await settled(page, false);
  expect(await page.evaluate(() => window.getSelection()!.isCollapsed)).toBe(false);
});

test("mobile sidebar menu and exposed chat strip work, while drag does not select a session", async ({ page }) => {
  await page.getByTestId("sidebar-toggle").click();
  await settled(page, true);
  const card = page.locator(".scard").nth(1);
  await card.evaluate(node => { node.setAttribute("data-testid", "drag-card"); });
  await touch(page, "touchstart", 200, 320, 1000, '[data-testid="drag-card"]');
  await touch(page, "touchmove", 175, 320, 1200, '[data-testid="drag-card"]');
  await touch(page, "touchend", 175, 320, 1400, '[data-testid="drag-card"]');
  await card.dispatchEvent("click", { bubbles: true, cancelable: true, detail: 1 });
  await expect(page.locator(".shell")).toHaveAttribute("data-selected", "sidebar-0");
  await settled(page, true);
  // A new tap, unlike the preceding drag's synthetic click, closes immediately.
  await touch(page, "touchstart", 365, 320, 2000, ".scrim-side");
  await touch(page, "touchend", 365, 320, 2050, ".scrim-side");
  await page.locator(".scrim-side").click({ position: { x: 25, y: 320 } });
  await settled(page, false);
  await expect.poll(() => page.locator(".pane").evaluate(node => (node as HTMLElement).inert)).toBe(false);
  await page.getByTestId("sidebar-toggle").click();
  await settled(page, true);
  await card.click();
  await settled(page, false);
  await expect(page.locator(".shell")).toHaveAttribute("data-selected", "sidebar-1");
});

test("mobile sidebar handles keyboard height, rotation, reduced motion and desktop layout", async ({ page }) => {
  await page.locator("textarea").focus();
  await touch(page, "touchstart", 50, 280, 1000);
  await touch(page, "touchmove", 190, 280, 1200);
  await expect.poll(async () => (await bounds(page)).pane.x).toBe(140);
  await page.setViewportSize({ width: 390, height: 520 });
  expect((await bounds(page)).pane.x).toBe(140);
  await touch(page, "touchmove", 280, 280, 1400);
  await touch(page, "touchend", 280, 280, 1600);
  await settled(page, true);
  await expect(page.locator("textarea")).not.toBeFocused();
  await page.setViewportSize({ width: 844, height: 390 });
  await settled(page, true);
  await page.getByRole("button", { name: "收起", exact: true }).click();
  await settled(page, false);
  expect(await page.locator(".pane").evaluate(node => getComputedStyle(node).transform)).toBe("none");
  // A resting filter creates a containing block and breaks fixed popovers.
  await expect.poll(() => page.locator(".pane").evaluate(node => getComputedStyle(node).filter)).toBe("none");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await touch(page, "touchstart", 50, 280, 2000);
  await touch(page, "touchmove", 300, 280, 2200);
  await touch(page, "touchend", 300, 280, 2400);
  await settled(page, true);
  await page.setViewportSize({ width: 1200, height: 800 });
  await expect.poll(async () => (await bounds(page)).pane.x).toBe(352);
  expect((await bounds(page)).pane.width).toBe(848);
  expect(await page.locator(".pane").evaluate(node => getComputedStyle(node).filter)).toBe("none");
  await expect.poll(() => page.locator(".pane").evaluate(node => (node as HTMLElement).inert)).toBe(false);
  await touch(page, "touchstart", 500, 280, 3000);
  expect(await touch(page, "touchmove", 700, 280, 3200)).toBe(false);
  await touch(page, "touchend", 700, 280, 3400);
  await expect(page.locator(".shell")).not.toHaveAttribute("data-sidebar-motion");
});

test("mobile sidebar native touch input preserves browser scroll ownership", async ({ page, browserName }) => {
  test.skip(browserName !== "chromium", "Native touch injection uses Chromium's CDP; WebKit covers event/layout contracts above.");
  const input = await page.context().newCDPSession(page);
  await input.send("Emulation.setTouchEmulationEnabled", { enabled: true });
  const send = (type: "touchStart" | "touchMove" | "touchEnd", x: number, y: number) =>
    input.send("Input.dispatchTouchEvent", { type,
      touchPoints: type === "touchEnd" ? [] : [{ x, y, id: 1 }] });
  const before = await bounds(page);
  await send("touchStart", 55, 300);
  for (let x = 80; x <= 280; x += 25) {
    await send("touchMove", x, 300);
    await page.waitForTimeout(20);
  }
  await expect.poll(async () => (await bounds(page)).pane.x).toBeGreaterThan(200);
  expect((await bounds(page)).scroll).toBe(before.scroll);
  await send("touchEnd", 280, 300);
  await settled(page, true);
  await page.getByRole("button", { name: "收起", exact: true }).click();
  await settled(page, false);
  // A vertical drag begins in the same opening region but remains native scroll.
  await send("touchStart", 55, 500);
  for (let y = 480; y >= 320; y -= 20) {
    await send("touchMove", 55, y);
    await page.waitForTimeout(20);
  }
  await send("touchEnd", 55, 320);
  await expect.poll(async () => (await bounds(page)).scroll).toBeGreaterThan(before.scroll + 80);
  await settled(page, false);
  await input.detach();
});
