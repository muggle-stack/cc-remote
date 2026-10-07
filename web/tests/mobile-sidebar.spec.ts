import { expect, test, type Page } from "@playwright/test";

async function touch(page: Page, type: string, x: number, y: number, time: number,
  selector = ".thread", count = 1, cancelable = true) {
  return page.locator(selector).evaluate((target, args) => {
    const point = { identifier: 1, target, clientX: args.x, clientY: args.y };
    const touches = args.type === "touchend" || args.type === "touchcancel" ? []
      : args.count === 2 ? [point, { identifier: 2, target,
        clientX: args.x + 30, clientY: args.y + 30 }] : [point];
    // WebKit does not expose a constructible Touch; use the native event shape.
    const event = new Event(args.type, { bubbles: true, cancelable: args.cancelable });
    Object.defineProperties(event, {
      touches: { value: touches }, targetTouches: { value: touches },
      changedTouches: { value: [point] }, timeStamp: { value: args.time },
    });
    target.dispatchEvent(event);
    return event.defaultPrevented;
  }, { type, x, y, time, count, cancelable });
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

async function blur(page: Page) {
  return page.locator(".pane").evaluate(node => {
    const filter = getComputedStyle(node).filter;
    return filter === "none" ? 0 : Number(filter.match(/blur\(([^p]+)px\)/)![1]);
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

test("mobile sidebar keeps the page containing block stable from rest through the first drag", async ({ page }) => {
  const probe = () => page.evaluate(() => {
    const pane = document.querySelector<HTMLElement>(".pane")!;
    const thread = document.querySelector<HTMLElement>(".thread")!;
    const header = document.querySelector<HTMLElement>(".c-head")!;
    const control = document.querySelector<HTMLElement>("#fixed-probe")!;
    const style = getComputedStyle(pane);
    return { hint: style.willChange, transform: style.transform, filter: style.filter,
      opacity: style.opacity, x: pane.getBoundingClientRect().x,
      y: header.getBoundingClientRect().y, scroll: thread.scrollTop,
      fixedY: control.getBoundingClientRect().y };
  });
  await page.evaluate(() => {
    // An offset makes a containing-block change observable: the probe must
    // never rebase when the first move introduces a nonzero transform/blur.
    // Real viewport menus/editors are tested separately through their portals.
    document.querySelector<HTMLElement>(".shell")!.style.top = "24px";
    const control = document.createElement("div");
    control.id = "fixed-probe";
    control.style.cssText = "position:fixed;top:7px;left:10px;width:20px;height:20px";
    document.querySelector(".pane")!.append(control);
  });
  const before = await probe();
  expect(before.hint).toBe("transform, filter");
  expect(before.fixedY).toBe(31);
  expect(before.transform).not.toBe("none");
  expect(before.filter).toBe("blur(0px)");
  for (let cycle = 0; cycle < 3; cycle++) {
    const time = 1000 + cycle * 1000;
    await touch(page, "touchstart", 60, 280, time);
    expect(await probe()).toEqual(before);
    // Include the first claimed frame, not just a fully-open screenshot.
    for (const delta of [12, 18, 26]) {
      await touch(page, "touchmove", 60 + delta, 280, time + delta * 2);
      await page.evaluate(() => new Promise(requestAnimationFrame));
      const during = await probe();
      expect(during.hint).toBe(before.hint);
      expect(during.opacity).toBe("1");
      expect(during.x).toBeCloseTo(delta, 2);
      expect(during.y).toBe(before.y);
      expect(during.fixedY).toBe(before.fixedY);
      expect(during.scroll).toBe(before.scroll);
    }
    await touch(page, "touchcancel", 86, 280, time + 200);
    await settled(page, false);
    expect(await probe()).toEqual(before);
  }
  await page.setViewportSize({ width: 1200, height: 800 });
  expect((await probe()).hint).toBe("auto");
});

test("mobile sidebar introduces its rounded edge gradually on the first drag frames", async ({ page }) => {
  const edges = () => page.evaluate(() => {
    const pane = getComputedStyle(document.querySelector(".pane")!);
    const veil = getComputedStyle(document.querySelector(".scrim-side")!);
    return { radius: parseFloat(pane.borderTopLeftRadius),
      veilRadius: parseFloat(veil.borderTopLeftRadius), shadow: pane.boxShadow, z: pane.zIndex };
  });
  for (const theme of ["light", "dark"]) {
    await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
    const before = await edges();
    expect(before.radius).toBe(0);
    await touch(page, "touchstart", 60, 280, 1000);
    expect(await edges()).toEqual(before);
    let previous = 0;
    for (const delta of [9, 10, 12, 18, 26]) {
      await touch(page, "touchmove", 60 + delta, 280, 1000 + delta * 2);
      await page.evaluate(() => new Promise(requestAnimationFrame));
      const edge = await edges();
      // Recognizing the swipe must not suddenly cut a full phone-size corner
      // out of the page or add a new shadow/stacking order on that same frame.
      expect(edge.radius).toBeGreaterThanOrEqual(previous);
      expect(edge.radius - previous).toBeLessThan(2);
      expect(edge.veilRadius).toBeCloseTo(edge.radius, 3);
      expect(edge.shadow).toBe(before.shadow);
      expect(edge.z).toBe(before.z);
      previous = edge.radius;
    }
    expect(previous).toBeGreaterThan(0);
    await touch(page, "touchmove", 72, 280, 1200);
    expect((await edges()).radius).toBeLessThan(previous);
    await touch(page, "touchcancel", 72, 280, 1400);
    await settled(page, false);
    expect(await edges()).toEqual(before);
    await page.getByTestId("sidebar-toggle").click();
    await settled(page, true);
    expect((await edges()).radius).toBeGreaterThanOrEqual(36);
    await page.getByRole("button", { name: "收起", exact: true }).click();
    await settled(page, false);
    expect(await edges()).toEqual(before);
  }
});

test("mobile sidebar keeps native list ancestors opaque across reveal and animation cleanup", async ({ page }) => {
  const read = () => page.locator(".s-scroll").evaluate(scroller => {
    const ancestors = [];
    for (let node: Element | null = scroller; node && !node.matches(".shell"); node = node.parentElement) {
      const style = getComputedStyle(node);
      ancestors.push({ opacity: style.opacity, filter: style.filter, visibility: style.visibility,
        transformed: style.transform !== "none", hint: style.willChange });
    }
    return { ancestors, top: scroller.scrollTop, height: scroller.clientHeight,
      contentHeight: scroller.scrollHeight };
  });
  await page.locator(".s-scroll").evaluate(node => { node.scrollTop = 150; });
  const before = await read();
  // Fade the sibling veil, not a UIScrollView ancestor. Visibility and layer
  // hints must not be introduced on reveal or removed at an opaque endpoint.
  for (const style of before.ancestors) {
    expect(style.opacity).toBe("1");
    expect(style.filter).toBe("none");
    expect(style.visibility).toBe("visible");
  }
  for (let cycle = 0; cycle < 2; cycle++) {
    await touch(page, "touchstart", 50, 280, 1000);
    await touch(page, "touchmove", 65, 280, 1050);
    expect(await read()).toEqual(before);
    await touch(page, "touchmove", 240, 280, 1200);
    expect(await read()).toEqual(before);
    await touch(page, "touchend", 240, 280, 1400);
    await touch(page, "touchstart", 160, 450, 1500, ".s-scroll");
    expect(await touch(page, "touchmove", 162, 390, 1520, ".s-scroll")).toBe(false);
    await touch(page, "touchend", 162, 370, 1540, ".s-scroll");
    expect(await read()).toEqual(before);
    await settled(page, true);
    expect(await read()).toEqual(before);
    await page.getByRole("button", { name: "收起", exact: true }).click();
    await settled(page, false);
    expect(await read()).toEqual(before);
  }
});

test("mobile sidebar stable page layers leave viewport menus and editors outside the pane", async ({ page }) => {
  await page.goto("/tests/history-browser.html?mobile-sidebar=1&sidebar-overlays=1");
  // Detect rebasing/clipping against a keyboard-sized, offset page rather than
  // an origin-aligned full-height fixture, where the regression is invisible.
  await page.locator(".shell").evaluate(node => {
    Object.assign((node as HTMLElement).style, { top: "24px", height: "580px" });
  });
  const trigger = page.getByRole("button", { name: "切换新会话引擎" });
  await trigger.click();
  const menu = page.getByRole("menu", { name: "会话引擎" });
  await expect(menu).toBeVisible();
  expect(await menu.evaluate(node => node.parentElement === document.body)).toBe(true);
  expect((await menu.boundingBox())!.y).toBeCloseTo(
    (await trigger.boundingBox())!.y + (await trigger.boundingBox())!.height + 8, 1);
  await page.getByRole("menuitemradio", { name: "Codex" }).click();
  await expect(menu).toHaveCount(0);
  for (const [open, selector, close] of [
    ["图片预览", ".image-lightbox", "关闭图片预览"],
    ["粘贴内容 ·", ".paste-preview-backdrop", "取消"],
  ]) {
    await page.getByRole("button", { name: new RegExp(open) }).click();
    const overlay = page.locator(selector);
    await expect(overlay).toBeVisible();
    expect(await overlay.evaluate(node => node.parentElement === document.body)).toBe(true);
    expect(await overlay.boundingBox()).toEqual({ x: 0, y: 0, width: 390, height: 844 });
    await page.getByRole("button", { name: close, exact: true }).click();
    await expect(overlay).toHaveCount(0);
  }
});

test("mobile sidebar crossfades content continuously when a drag reverses or cancels", async ({ page }) => {
  const opacity = (selector: string) => page.locator(selector).evaluate(node => {
    const value = Number(getComputedStyle(node).opacity);
    return node.classList.contains("s-fade") ? 1 - value : value;
  });
  const menu = ".sessions > .s-fade";
  for (const theme of ["light", "dark"]) {
    await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
    await expect.poll(() => opacity(menu)).toBe(0);
    expect(await blur(page)).toBe(0);
    await touch(page, "touchstart", 40, 280, 1000);
    await touch(page, "touchmove", 52, 280, 1100);
    await expect.poll(async () => (await bounds(page)).pane.x).toBe(12);
    const initialBlur = await blur(page);
    // Crossing the swipe threshold must not apply the fully-open blur.
    expect(initialBlur).toBeGreaterThan(0);
    expect(initialBlur).toBeLessThan(.1);
    await touch(page, "touchmove", 140, 280, 1200);
    await expect.poll(async () => (await bounds(page)).pane.x).toBe(100);
    const partial = await opacity(menu);
    expect(partial).toBeGreaterThan(0);
    expect(partial).toBeLessThan(1);
    const partialVeil = await opacity(".scrim-side");
    const partialBlur = await blur(page);
    expect(partialBlur).toBeGreaterThan(initialBlur);
    expect(partialBlur).toBeLessThan(.5);
    // Holding a finger still must not let a time-based effect run to its end.
    await page.waitForTimeout(350);
    expect(await blur(page)).toBeCloseTo(partialBlur, 3);
    // The foreground card itself must remain opaque, including its background,
    // so text fading cannot reveal the menu through the shifted conversation.
    expect(await opacity(".pane")).toBe(1);
    await touch(page, "touchmove", 240, 280, 1400);
    await expect.poll(() => opacity(menu)).toBeGreaterThan(partial);
    expect(await opacity(".scrim-side")).toBeGreaterThan(partialVeil);
    expect(await blur(page)).toBeGreaterThan(partialBlur);
    await page.screenshot({ path: test.info().outputPath(`sidebar-fade-partial-${theme}.png`) });
    await touch(page, "touchmove", 140, 280, 1600);
    await expect.poll(() => opacity(menu)).toBeCloseTo(partial, 2);
    expect(await opacity(".scrim-side")).toBeCloseTo(partialVeil, 2);
    expect(await blur(page)).toBeCloseTo(partialBlur, 3);
    await page.evaluate(() => window.dispatchEvent(new Event("sidebar-fixture-stream")));
    expect(await opacity(menu)).toBeCloseTo(partial, 2);
    expect(await blur(page)).toBeCloseTo(partialBlur, 3);
    await touch(page, "touchcancel", 140, 280, 1800);
    await settled(page, false);
    await expect.poll(() => opacity(menu)).toBe(0);
    await expect.poll(() => opacity(".scrim-side")).toBe(0);
    expect(await blur(page)).toBe(0);
    expect(await page.locator(".pane").evaluate(node => (node as HTMLElement).style.filter)).toBe("");
    // Button opening and selection/close use the same endpoints, with no
    // lingering inline opacity after the gesture's cleanup.
    await page.getByTestId("sidebar-toggle").click();
    await settled(page, true);
    expect(await opacity(menu)).toBe(1);
    expect(await blur(page)).toBeGreaterThan(partialBlur);
    await page.getByRole("button", { name: "收起", exact: true }).click();
    await settled(page, false);
    expect(await page.locator(menu).evaluate(node => (node as HTMLElement).style.opacity)).toBe("");
  }
});

test("horizontal finger noise does not run history layout or pause live output", async ({ page }) => {
  await page.goto("/tests/history-browser.html?mobile-sidebar=1&sidebar-load=1");
  await expect(page.locator(".thread")).toContainText("当前输出 0");
  await expect(page.locator(".scroll-bottom-btn")).not.toBeVisible();
  const reads = await page.locator(".thread").evaluate(async target => {
    // Count synchronous layout reads made INSIDE the move handler, not normal
    // ResizeObserver/virtualizer work between events. Real fingers drift on Y;
    // perfectly horizontal synthetic moves previously missed this regression.
    let measuring = false;
    let reads = 0;
    const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, "scrollHeight")!;
    Object.defineProperty(target, "scrollHeight", {
      configurable: true,
      get() { if (measuring) reads++; return descriptor.get!.call(this); },
    });
    const emit = (type: string, x: number, y: number) => {
      const point = { identifier: 1, target, clientX: x, clientY: y };
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperties(event, {
        touches: { value: type === "touchend" ? [] : [point] },
        changedTouches: { value: [point] },
      });
      target.dispatchEvent(event);
    };
    try {
      emit("touchstart", 30, 280);
      for (let index = 1; index <= 60; index++) {
        await new Promise<void>(resolve => requestAnimationFrame(() => {
          measuring = true;
          emit("touchmove", 30 + index * 4, 280 + index * .15);
          measuring = false;
          resolve();
        }));
      }
      emit("touchend", 270, 289);
      return reads;
    } finally {
      Reflect.deleteProperty(target, "scrollHeight");
    }
  });
  expect(reads).toBe(0);
  await settled(page, true);
  await page.getByRole("button", { name: "收起", exact: true }).click();
  await settled(page, false);
  await page.evaluate(() => window.dispatchEvent(new Event("sidebar-fixture-stream")));
  await expect(page.locator(".thread")).toContainText("当前输出 1");
  await expect(page.locator(".scroll-bottom-btn")).not.toBeVisible();
  // A vertical pan still transfers follow ownership to the history reader.
  await touch(page, "touchstart", 60, 280, 3000);
  await touch(page, "touchmove", 62, 320, 3100);
  await touch(page, "touchend", 62, 320, 3300);
  await expect(page.locator(".scroll-bottom-btn")).toBeVisible();
});

test("sidebar release updates its controls without rebuilding the conversation owner", async ({ page }) => {
  await page.goto("/tests/history-browser.html?mobile-sidebar=1&sidebar-load=1");
  await expect(page.locator(".thread")).toContainText("当前输出 0");
  const renderCount = () => page.evaluate(() => (window as unknown as {
    sidebarSceneRenders: { count: number };
  }).sidebarSceneRenders.count);
  const before = await renderCount();
  await page.getByTestId("sidebar-toggle").click();
  await settled(page, true);
  await page.getByRole("button", { name: "收起", exact: true }).click();
  await settled(page, false);
  // A short fast flick releases into the automatic animation. Its logical
  // state updates immediately, without waiting for an animation-end timer.
  await touch(page, "touchstart", 50, 280, 1000);
  await touch(page, "touchmove", 80, 281, 1030);
  await touch(page, "touchend", 80, 281, 1040);
  await expect(page.locator(".shell")).toHaveClass(/sidebar-open/);
  await expect(page.locator(".pane")).toHaveJSProperty("inert", true);
  await settled(page, true);
  expect(await renderCount()).toBe(before);

  // Closing while an opening animation is still active wins immediately;
  // there is no deferred callback that can reopen it after the user's close.
  await page.getByRole("button", { name: "收起", exact: true }).click();
  await settled(page, false);
  await touch(page, "touchstart", 50, 280, 2000);
  await touch(page, "touchmove", 80, 281, 2030);
  await touch(page, "touchend", 80, 281, 2040);
  await page.getByRole("button", { name: "收起", exact: true }).dispatchEvent("click");
  await settled(page, false);
  expect(await renderCount()).toBe(before);
  // Ordinary stream updates must still reach the conversation and sidebar.
  await page.evaluate(() => window.dispatchEvent(new Event("sidebar-fixture-stream")));
  await expect(page.locator(".thread")).toContainText("当前输出 1");
  expect(await renderCount()).toBeGreaterThan(before);
});

test("non-cancelable horizontal release does not rebuild history on its first animation frame", async ({ page }) => {
  await touch(page, "touchstart", 55, 300, 1000);
  await touch(page, "touchmove", 68, 312, 1020);
  await touch(page, "touchmove", 140, 312, 1060, ".thread", 1, false);
  await touch(page, "touchmove", 275, 315, 1100, ".thread", 1, false);
  const before = await page.evaluate(() => (window as unknown as {
    sidebarRenderDurations: number[];
  }).sidebarRenderDurations.length);
  await touch(page, "touchend", 275, 315, 1120, ".thread", 1, false);
  await page.evaluate(() => new Promise(requestAnimationFrame));
  const after = await page.evaluate(() => (window as unknown as {
    sidebarRenderDurations: number[];
  }).sidebarRenderDurations.length);
  expect(after).toBe(before);
  await settled(page, true);
});

test("mobile sidebar reuses unchanged Markdown during gestures and live output", async ({ page }) => {
  await page.goto("/tests/history-browser.html?mobile-sidebar=1&sidebar-load=1");
  await expect(page.locator(".thread")).toContainText("当前输出 0");
  await page.waitForTimeout(500);
  const parses = () => page.evaluate(() => (window as unknown as {
    sidebarMarkdownParses: { history: number; live: number };
  }).sidebarMarkdownParses);
  const before = await parses();
  expect(before.history).toBeGreaterThan(0);
  expect(before.live).toBeGreaterThan(0);

  // Include canceled drags, real reversals, and parent renders with NEW file
  // callbacks. Neither historical text nor the unchanged live buffer is parsed.
  await touch(page, "touchstart", 60, 280, 1000);
  await touch(page, "touchmove", 100, 280, 1200);
  await touch(page, "touchend", 100, 280, 1400);
  await settled(page, false);
  for (let index = 0; index < 4; index++) {
    const opening = index % 2 === 0;
    const selector = opening ? ".thread" : ".sessions";
    const start = opening ? 60 : 280;
    const end = opening ? 280 : 60;
    const time = 2000 + index * 1000;
    await touch(page, "touchstart", start, 280, time, selector);
    await touch(page, "touchmove", end, 280, time + 200, selector);
    await touch(page, "touchend", end, 280, time + 400, selector);
    await settled(page, opening);
  }
  expect(await parses()).toEqual(before);

  // Changed output still catches up, and context consumers in the cached tree
  // must use today's callback, not the closure from its initial parse.
  await page.evaluate(() => window.dispatchEvent(new Event("sidebar-fixture-stream")));
  await expect(page.locator(".thread")).toContainText("当前输出 1");
  const after = await parses();
  expect(after.history).toBe(before.history);
  expect(after.live).toBeGreaterThan(before.live);
  // Exercise the already-mounted overscan row without scrolling/virtualizing a
  // new tree, which would no longer test the callback inside the cached tree.
  await page.locator('[data-turn-id="history-11"] .message-file-link').last().dispatchEvent("click");
  await expect(page.locator("body")).toHaveAttribute("data-file-revision", "1");
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

test("mobile sidebar reopens from the middle of the chat after a reverse swipe", async ({ page }) => {
  await page.getByTestId("sidebar-toggle").click();
  await settled(page, true);
  await touch(page, "touchstart", 280, 280, 1000, ".sessions");
  await touch(page, "touchmove", 210, 280, 1020, ".sessions");
  await touch(page, "touchend", 180, 280, 1040, ".sessions");
  await settled(page, false);
  // The next opening swipe starts where the previous closing swipe finished.
  await touch(page, "touchstart", 180, 280, 1100);
  expect(await touch(page, "touchmove", 240, 280, 1120)).toBe(true);
  await touch(page, "touchend", 290, 280, 1140);
  await settled(page, true);
});

test("mobile sidebar keeps a new touch when the previous settle would finish", async ({ page }) => {
  await touch(page, "touchstart", 50, 280, 1000);
  await touch(page, "touchmove", 100, 280, 1200);
  await touch(page, "touchend", 100, 280, 1400);
  // Press during the closing animation; hold across the previous cleanup timer.
  await touch(page, "touchstart", 55, 280, 1500);
  await page.waitForTimeout(420);
  expect(await touch(page, "touchmove", 180, 280, 1520)).toBe(true);
  await touch(page, "touchend", 240, 280, 1540);
  await settled(page, true);
});

test("mobile sidebar never snaps back while the conversation delays its open-state commit", async ({ page }) => {
  await page.goto("/tests/history-browser.html?mobile-sidebar=1&sidebar-delayed-commit=1");
  await expect(page.locator(".thread")).toBeVisible();
  await touch(page, "touchstart", 50, 280, 1000);
  await touch(page, "touchmove", 270, 280, 1200);
  await touch(page, "touchend", 270, 280, 1400);
  const width = (await bounds(page)).sidebar.width;
  // Hold the parent commit beyond both transitionend and the fallback timer.
  // Neither may expose the old closed class while the user's open is pending.
  await page.waitForTimeout(450);
  expect((await bounds(page)).pane.x).toBeCloseTo(width, 1);
  // A second gesture starts from the visible pending-open state. Canceling it
  // must return there, not to the stale closed state still held by the parent.
  await touch(page, "touchstart", 280, 280, 1500, ".sessions");
  await touch(page, "touchmove", 200, 280, 1600, ".sessions");
  expect((await bounds(page)).pane.x).toBeCloseTo(width - 80, 1);
  await touch(page, "touchcancel", 200, 280, 1700, ".sessions");
  await page.waitForTimeout(450);
  expect((await bounds(page)).pane.x).toBeCloseTo(width, 1);
  await page.evaluate(() => window.dispatchEvent(new Event("sidebar-fixture-commit")));
  await settled(page, true);
  await touch(page, "touchstart", 280, 280, 2000, ".sessions");
  await touch(page, "touchmove", 60, 280, 2200, ".sessions");
  await touch(page, "touchend", 60, 280, 2400, ".sessions");
  await page.waitForTimeout(450);
  expect((await bounds(page)).pane.x).toBeCloseTo(0, 1);
  await page.evaluate(() => window.dispatchEvent(new Event("sidebar-fixture-commit")));
  await settled(page, false);
});

test("mobile sidebar cleanup waits for the actual transition instead of a fixed deadline", async ({ page }) => {
  await page.addStyleTag({ content: `
    .shell[data-sidebar-motion="settling"] > :is(.pane,.sessions,.scrim-side),
    .shell[data-sidebar-motion="settling"] > .sessions > .s-fade {
      transition-duration: 1s !important; transition-timing-function: linear !important;
    }
  ` });
  await touch(page, "touchstart", 50, 280, 1000);
  await touch(page, "touchmove", 250, 280, 1200);
  await touch(page, "touchend", 250, 280, 1400);
  await page.waitForTimeout(400);
  await expect(page.locator(".shell")).toHaveAttribute("data-sidebar-motion", "settling");
  const b = await bounds(page);
  expect(b.pane.x).toBeGreaterThan(200);
  expect(b.pane.x).toBeLessThan(b.sidebar.width - 10);
  await settled(page, true);
});

test("mobile sidebar release preserves a short flick's speed and keeps effects in step", async ({ page }) => {
  await touch(page, "touchstart", 50, 280, 1000);
  await touch(page, "touchmove", 75, 280, 1050);
  await touch(page, "touchmove", 100, 280, 1100);
  await touch(page, "touchend", 100, 280, 1100);
  const motion = await page.evaluate(() => {
    const pane = document.querySelector<HTMLElement>(".pane")!;
    const sidebar = document.querySelector<HTMLElement>(".sessions")!;
    const scrim = document.querySelector<HTMLElement>(".scrim-side")!;
    const menu = sidebar.querySelector<HTMLElement>(".s-fade")!;
    // Sample browser interpolation at exact times, independent of CI's frame
    // scheduling. A .5px/ms short flick used to jump ~46px in its first 16ms.
    const animations = [pane, sidebar, scrim, menu].flatMap(node => node.getAnimations());
    animations.forEach(animation => animation.pause());
    const frames = [0, 1, 16, 100].map(time => {
      animations.forEach(animation => { animation.currentTime = time; });
      return { time, x: pane.getBoundingClientRect().x, scrimX: scrim.getBoundingClientRect().x,
        width: sidebar.getBoundingClientRect().width, opacity: 1 - Number(getComputedStyle(menu).opacity),
        filter: getComputedStyle(pane).filter };
    });
    animations.forEach(animation => animation.play());
    return frames;
  });
  expect(motion[0].x).toBeCloseTo(50, 1);
  expect(motion[1].x - motion[0].x).toBeCloseTo(.5, 1);
  expect(motion[2].x - motion[0].x).toBeLessThan(16);
  expect(motion[3].x - motion[0].x).toBeLessThan(125);
  for (const frame of motion) {
    expect(frame.scrimX).toBeCloseTo(frame.x, 1);
    expect(frame.opacity).toBeCloseTo(frame.x / frame.width, 3);
    expect(Number(frame.filter.match(/blur\(([^p]+)px\)/)![1])).toBeCloseTo(.7 * frame.x / frame.width, 3);
  }
  await settled(page, true);
  for (const selector of [".pane", ".sessions", ".scrim-side", ".s-fade"]) {
    expect(await page.locator(selector).evaluate(node => (node as HTMLElement).style.transitionDuration)).toBe("");
  }
});

test("mobile sidebar release carries a fast closing flick without overshooting", async ({ page }) => {
  await page.getByTestId("sidebar-toggle").click();
  await settled(page, true);
  await touch(page, "touchstart", 250, 280, 1000, ".sessions");
  await touch(page, "touchmove", 200, 280, 1025, ".sessions");
  await touch(page, "touchend", 200, 280, 1025, ".sessions");
  const samples = await page.locator(".pane").evaluate(node => {
    const animation = node.getAnimations().find(value =>
      value instanceof CSSTransition && value.transitionProperty === "transform")!;
    animation.pause();
    const duration = Number(animation.effect!.getComputedTiming().duration);
    const positions = [0, 1, duration / 3, duration * 2 / 3, duration].map(time => {
      animation.currentTime = time;
      return node.getBoundingClientRect().x;
    });
    animation.play();
    return positions;
  });
  expect(samples[0] - samples[1]).toBeCloseTo(2, 1);
  expect(samples.at(-1)).toBeCloseTo(0, 2);
  for (let index = 1; index < samples.length; index++) {
    expect(samples[index]).toBeGreaterThanOrEqual(0);
    expect(samples[index]).toBeLessThan(samples[index - 1]);
  }
  await settled(page, false);
});

test("mobile sidebar tracks input arriving in the current frame without a queued older position", async ({ page }) => {
  await touch(page, "touchstart", 50, 280, 1000);
  const samples = await page.locator(".thread").evaluate(async target => {
    const pane = document.querySelector(".pane")!;
    const samples: { wanted: number; actual: number }[] = [];
    for (const offset of [20, 40, 60, 80, 100, 80, 60, 40, 20]) {
      await new Promise<void>(resolve => requestAnimationFrame(() => {
        const point = { identifier: 1, target, clientX: 50 + offset, clientY: 280 };
        const event = new Event("touchmove", { bubbles: true, cancelable: true });
        Object.defineProperties(event, { touches: { value: [point] }, changedTouches: { value: [point] } });
        target.dispatchEvent(event);
        samples.push({ wanted: offset, actual: pane.getBoundingClientRect().x });
        resolve();
      }));
    }
    return samples;
  });
  for (const sample of samples) expect(sample.actual).toBeCloseTo(sample.wanted, 1);
  await touch(page, "touchcancel", 70, 280, 2000);
  await settled(page, false);
});

test("mobile sidebar keeps recent flick velocity when touchend repeats the last coordinate", async ({ page }) => {
  await touch(page, "touchstart", 180, 280, 1000);
  await touch(page, "touchmove", 205, 280, 1040);
  await touch(page, "touchmove", 230, 280, 1080);
  await touch(page, "touchend", 230, 280, 1150);
  await settled(page, true);
  await touch(page, "touchstart", 280, 280, 2000, ".sessions");
  await touch(page, "touchmove", 255, 280, 2040, ".sessions");
  await touch(page, "touchmove", 230, 280, 2080, ".sessions");
  await touch(page, "touchend", 230, 280, 2150, ".sessions");
  await settled(page, false);
  // Holding after a short drag cancels its momentum; distance decides instead.
  await touch(page, "touchstart", 180, 280, 3000);
  await touch(page, "touchmove", 205, 280, 3040);
  await touch(page, "touchmove", 230, 280, 3080);
  await touch(page, "touchend", 230, 280, 3300);
  await settled(page, false);
});

test("mobile sidebar repeatedly reverses before settling and releases a vertical interruption", async ({ page }) => {
  for (let index = 0; index < 12; index++) {
    const opening = index % 2 === 0;
    const start = opening ? 180 : 280;
    const end = opening ? 280 : 180;
    const time = 1000 + index * 100;
    const selector = opening ? ".thread" : ".sessions";
    await touch(page, "touchstart", start, 280, time, selector);
    expect(await touch(page, "touchmove", (start + end) / 2, 280, time + 20, selector)).toBe(true);
    await touch(page, "touchend", end, 280, time + 40, selector);
    await expect(page.locator(".shell")).toHaveClass(opening ? /sidebar-open/ : /^shell(?: fixture-streaming)?$/);
    await page.waitForTimeout(20);
  }
  await settled(page, false);
  await touch(page, "touchstart", 180, 280, 3000);
  await touch(page, "touchmove", 250, 280, 3020);
  await touch(page, "touchend", 280, 280, 3040);
  await touch(page, "touchstart", 200, 280, 3100, ".sessions");
  expect(await touch(page, "touchmove", 200, 320, 3120, ".sessions")).toBe(false);
  await touch(page, "touchend", 200, 320, 3140, ".sessions");
  await settled(page, true);
});

test("mobile sidebar native touches reverse across the exposed closing surface", async ({ page, browserName }) => {
  test.skip(browserName !== "chromium", "Trusted swipe injection uses Chromium CDP; WebKit checks the same rapid state transitions separately.");
  const input = await page.context().newCDPSession(page);
  await input.send("Emulation.setTouchEmulationEnabled", { enabled: true });
  // Keep the source cadence explicit: waiting for each CDP acknowledgement can
  // otherwise turn a 16ms flick into a slow drag on a busy test runner.
  let timestamp = Date.now() / 1000;
  const send = (type: "touchStart" | "touchMove" | "touchEnd", x: number) =>
    input.send("Input.dispatchTouchEvent", { type, timestamp: timestamp += .016,
      touchPoints: type === "touchEnd" ? [] : [{ x, y: 300, id: 1 }] });
  for (let index = 0; index < 10; index++) {
    const opening = index % 2 === 0;
    const start = opening ? 180 : 280;
    const end = opening ? 280 : 180;
    await send("touchStart", start);
    for (let step = 1; step <= 4; step++) {
      await send("touchMove", start + (end - start) * step / 4);
      await page.waitForTimeout(16);
    }
    await send("touchEnd", end);
    if (opening) await expect(page.locator(".shell")).toHaveClass(/sidebar-open/);
    else await expect(page.locator(".shell")).not.toHaveClass(/sidebar-open/);
    await page.waitForTimeout(16);
  }
  // While closing, the exposed sidebar is inert. Hit testing reaches the shell
  // itself; a swipe from this surface must still be able to reverse the drawer.
  // Start from a known open position: velocity-based settles can finish the
  // preceding rapid cycle before the test runner delivers its next CDP event.
  await settled(page, false);
  await page.getByTestId("sidebar-toggle").click();
  await settled(page, true);
  await send("touchStart", 280);
  await send("touchMove", 230);
  await send("touchEnd", 230);
  await page.locator(".shell").evaluate(shell => {
    shell.addEventListener("touchstart", event => {
      shell.setAttribute("data-native-touch-target", event.target === shell ? "shell" : "child");
    }, { once: true });
  });
  await send("touchStart", 20);
  await expect(page.locator(".shell")).toHaveAttribute("data-native-touch-target", "shell");
  await send("touchMove", 70);
  await send("touchEnd", 120);
  await expect(page.locator(".shell")).toHaveClass(/sidebar-open/);
  await send("touchStart", 280);
  await send("touchMove", 230);
  await send("touchEnd", 180);
  await settled(page, false);
  await input.detach();
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

test("mobile sidebar canceled touches and native scrolling cannot leave a pending long press", async ({ page }) => {
  await page.getByTestId("sidebar-toggle").click();
  await settled(page, true);
  const card = ".scard.active";
  // Native scrolling can claim the touch before React sees a >10px move.
  // Also cover scroll takeover without a cancellation event being delivered.
  for (const takeover of ["touchcancel", "pointercancel", "scroll"]) {
    await touch(page, "touchstart", 160, 300, 1000, card);
    await touch(page, "touchmove", 162, 305, 1020, card);
    if (takeover === "touchcancel") await touch(page, takeover, 162, 305, 1040, card);
    else if (takeover === "pointercancel") {
      await page.locator(card).dispatchEvent("pointercancel", { pointerType: "touch", bubbles: true });
    } else {
      await page.locator(".s-scroll").evaluate(node => { node.scrollTop += 80; });
    }
    // Wait beyond the real long-press timeout before asserting: an immediate
    // check misses the menu appearing halfway through momentum scrolling.
    await page.waitForTimeout(650);
    await expect(page.locator(".scard.lifting")).toHaveCount(0);
    await expect(page.locator(".card-menu")).toHaveCount(0);
    await expect(page.locator(".s-lift-scrim")).not.toHaveClass(/show/);
    await touch(page, "touchend", 162, 305, 1700, card);
  }
  // A genuine stationary press must still work after a canceled gesture.
  await page.locator(".s-scroll").evaluate(node => { node.scrollTop = 0; });
  await page.waitForTimeout(100);
  await touch(page, "touchstart", 160, 300, 2000, card);
  await expect(page.locator(".scard.lifting")).toHaveCount(1);
  await expect(page.locator(".card-menu")).toBeVisible();
  await touch(page, "touchend", 160, 300, 2700, card);
});

test("mobile sidebar leaves its opening animation untouched by a native list pan", async ({ page }) => {
  // Keep the overlap deterministic instead of waiting for settled(), which
  // misses a list fling that begins as soon as the drawer becomes reachable.
  await page.addStyleTag({ content: ".shell.sidebar-open > :is(.pane,.sessions,.scrim-side), .shell.sidebar-open > .sessions > .s-fade { transition-duration: 1s; }" });
  await page.getByTestId("sidebar-toggle").click();
  await expect(page.locator(".shell")).toHaveAttribute("data-sidebar-motion", "settling");
  await page.locator(".pane").evaluate(node => Promise.all(node.getAnimations().map(animation => animation.ready)));
  const readMotion = () => page.evaluate(() => ({
    phase: document.querySelector<HTMLElement>(".shell")!.dataset.sidebarMotion,
    styles: [".pane", ".sessions", ".scrim-side", ".s-fade"].map(selector =>
      document.querySelector(selector)!.getAttribute("style")),
    animations: document.querySelector(".pane")!.getAnimations().map(animation => ({
      startTime: animation.startTime, playState: animation.playState,
    })),
  }));
  const before = await readMotion();
  expect(before.animations.length).toBeGreaterThan(0);
  await touch(page, "touchstart", 160, 450, 1000, ".s-scroll");
  expect(await readMotion()).toEqual(before);
  expect(await touch(page, "touchmove", 162, 390, 1020, ".s-scroll")).toBe(false);
  // This is the native scroll ownership sequence on iOS/Chrome. It must not
  // restart a horizontal transition as the list enters momentum scrolling.
  await page.locator(".s-scroll").dispatchEvent("pointercancel", { pointerType: "touch", bubbles: true });
  await touch(page, "touchend", 162, 370, 1040, ".s-scroll");
  expect(await readMotion()).toEqual(before);
  await settled(page, true);
});

test("mobile sidebar pending touches survive animation cleanup without freezing it", async ({ page }) => {
  await page.getByTestId("sidebar-toggle").click();
  await touch(page, "touchstart", 280, 400, 1000, ".s-scroll");
  // The drawer must finish normally while direction is still unknown, without
  // losing the candidate if this turns into a horizontal drag after a hold.
  await settled(page, true);
  expect(await touch(page, "touchmove", 210, 400, 1400, ".s-scroll")).toBe(true);
  await touch(page, "touchmove", 65, 400, 1500, ".s-scroll");
  await touch(page, "touchend", 65, 400, 1700, ".s-scroll");
  await settled(page, false);
});

test("mobile sidebar focus settling leaves an unpanned document alone", async ({ page }) => {
  await page.locator("textarea").focus();
  await page.evaluate(() => {
    const original = window.scrollTo.bind(window);
    Object.assign(window, { layoutScrollResets: 0 });
    window.scrollTo = ((...args: Parameters<typeof original>) => {
      (window as unknown as { layoutScrollResets: number }).layoutScrollResets++;
      original(...args);
    }) as typeof window.scrollTo;
  });
  await touch(page, "touchstart", 50, 280, 1000);
  await touch(page, "touchmove", 280, 280, 1200);
  await touch(page, "touchend", 280, 280, 1400);
  await settled(page, true);
  await page.waitForTimeout(350);
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
  expect(await page.evaluate(() => (window as unknown as {
    layoutScrollResets: number;
  }).layoutScrollResets)).toBe(0);

  // Keep the keyboard recovery: a genuinely displaced root must return to 0.
  await page.addStyleTag({ content: "html { overflow:auto!important; } body { min-height:1800px!important; }" });
  await page.evaluate(() => { window.scrollTo(0, 100); });
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(100);
  await page.evaluate(() => document.dispatchEvent(new FocusEvent("focusout", { bubbles: true })));
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
});

test("mobile sidebar list keeps native momentum after release in both directions", async ({ page, browserName }) => {
  test.skip(browserName !== "chromium", "Trusted fling injection needs CDP; WebKit covers cancellation and focus settling separately.");
  await page.getByTestId("sidebar-toggle").click();
  await settled(page, true);
  const input = await page.context().newCDPSession(page);
  await input.send("Emulation.setTouchEmulationEnabled", { enabled: true });
  const list = page.locator(".s-scroll");
  for (const direction of [-1, 1]) {
    await list.evaluate(node => { node.scrollTop = (node.scrollHeight - node.clientHeight) / 2; });
    await page.waitForTimeout(100);
    const initial = await list.evaluate(node => node.scrollTop);
    let timestamp = Date.now() / 1000;
    const send = (type: "touchStart" | "touchMove" | "touchEnd", y: number) =>
      input.send("Input.dispatchTouchEvent", { type, timestamp: timestamp += .032,
        touchPoints: type === "touchEnd" ? [] : [{ x: 160, y, id: 1 }] });
    await send("touchStart", 450);
    for (let step = 1; step <= 6; step++) {
      await send("touchMove", 450 + direction * step * 16);
      await page.waitForTimeout(32);
    }
    await send("touchEnd", 450 + direction * 96);
    const released = await list.evaluate(node => node.scrollTop);
    expect((released - initial) * -direction).toBeGreaterThan(30);
    // A normal React/session update must not remount or reset the list while
    // the native scroller keeps moving after the finger has left the screen.
    await page.evaluate(() => window.dispatchEvent(new Event("sidebar-fixture-stream")));
    await page.waitForTimeout(150);
    const coasting = await list.evaluate(node => node.scrollTop);
    expect((coasting - released) * -direction).toBeGreaterThan(10);
    await page.waitForTimeout(550);
    await expect(page.locator(".scard.lifting")).toHaveCount(0);
    await expect(page.locator(".card-menu")).toHaveCount(0);
    await settled(page, true);
    await expect(page.locator(".shell")).toHaveAttribute("data-selected", "sidebar-0");
  }
  await input.detach();
});

test("mobile sidebar immediate native list fling coasts across opening completion", async ({ page, browserName }) => {
  test.skip(browserName !== "chromium", "Trusted fling injection needs CDP; the separate WebKit test verifies untouched transition identity.");
  const input = await page.context().newCDPSession(page);
  await input.send("Emulation.setTouchEmulationEnabled", { enabled: true });
  const list = page.locator(".s-scroll");
  for (const direction of [-1, 1]) {
    await list.evaluate(node => { node.scrollTop = (node.scrollHeight - node.clientHeight) / 2; });
    // Release past halfway, with enough of the list already exposed to touch.
    // Start the native fling immediately: deliberately do NOT await settled().
    await touch(page, "touchstart", 50, 280, 1000);
    await touch(page, "touchmove", 240, 280, 1200);
    await touch(page, "touchend", 240, 280, 1400);
    await expect(page.locator(".shell")).toHaveAttribute("data-sidebar-motion", "settling");
    const initial = await list.evaluate(node => node.scrollTop);
    let timestamp = Date.now() / 1000;
    const send = (type: "touchStart" | "touchMove" | "touchEnd", y: number) =>
      input.send("Input.dispatchTouchEvent", { type, timestamp: timestamp += .016,
        touchPoints: type === "touchEnd" ? [] : [{ x: 100, y, id: 1 }] });
    await send("touchStart", 450);
    await expect(page.locator(".shell")).toHaveAttribute("data-sidebar-motion", "settling");
    for (let step = 1; step <= 6; step++) {
      await send("touchMove", 450 + direction * step * 12);
      await page.waitForTimeout(16);
    }
    await send("touchEnd", 450 + direction * 72);
    const released = await list.evaluate(node => node.scrollTop);
    expect((released - initial) * -direction).toBeGreaterThan(20);
    await settled(page, true);
    const afterAnimation = await list.evaluate(node => node.scrollTop);
    expect((afterAnimation - released) * -direction).toBeGreaterThan(5);
    await page.waitForTimeout(100);
    expect(((await list.evaluate(node => node.scrollTop)) - afterAnimation) * -direction).toBeGreaterThan(5);
    await expect(page.locator(".scard.lifting")).toHaveCount(0);
    await expect(page.locator(".shell")).toHaveAttribute("data-selected", "sidebar-0");
    await page.getByRole("button", { name: "收起", exact: true }).click();
    await settled(page, false);
  }
  await input.detach();
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
  expect(await page.locator(".pane").evaluate(node => getComputedStyle(node).transform)).not.toBe("none");
  // Mobile resting surfaces retain the same containing block; viewport UI
  // uses body portals instead of relying on filter:none at the endpoint.
  await expect.poll(() => page.locator(".pane").evaluate(node => getComputedStyle(node).filter)).toBe("blur(0px)");
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
  expect(await page.locator(".sessions > .s-inner").evaluate(node => getComputedStyle(node).opacity)).toBe("1");
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

test("mobile sidebar native diagonal starts keep working through repeated open and close gestures", async ({ page, browserName }) => {
  test.skip(browserName !== "chromium", "Browser gesture arbitration requires native Chromium touch input.");
  const input = await page.context().newCDPSession(page);
  await input.send("Emulation.setTouchEmulationEnabled", { enabled: true });
  const send = (type: "touchStart" | "touchMove" | "touchEnd", x: number, y: number) =>
    input.send("Input.dispatchTouchEvent", { type,
      touchPoints: type === "touchEnd" ? [] : [{ x, y, id: 1 }] });
  const startScroll = (await bounds(page)).scroll;
  for (let cycle = 0; cycle < 3; cycle++) {
    for (const opening of [true, false]) {
      const start = opening ? 55 : 280;
      const direction = opening ? 1 : -1;
      await send("touchStart", start, 300);
      // Native Chrome first delivers (13,12), where direction is not yet
      // clear. Subsequent moves may be non-cancelable despite NO native pan.
      for (const [dx, dy] of [[5, 5], [9, 9], [13, 12], [25, 12], [60, 12], [120, 14], [220, 15]]) {
        await send("touchMove", start + direction * dx, 300 + dy);
        await page.waitForTimeout(24);
      }
      await expect(page.locator(".shell")).toHaveAttribute("data-sidebar-motion", "dragging");
      await send("touchEnd", start + direction * 220, 315);
      await settled(page, opening);
      expect((await bounds(page)).scroll).toBe(startScroll);
      await expect(page.locator(".shell")).toHaveAttribute("data-selected", "sidebar-0");
    }
  }
  // A vertical-leading ambiguous start still belongs to native scrolling.
  // Once pointercancel transfers ownership, later horizontal movement cannot
  // steal this gesture back to navigation.
  await send("touchStart", 55, 500);
  for (const [dx, dy] of [[5, -5], [9, -9], [12, -13], [20, -80], [120, -85], [220, -90]]) {
    await send("touchMove", 55 + dx, 500 + dy);
    await page.waitForTimeout(24);
  }
  await send("touchEnd", 275, 410);
  await settled(page, false);
  await expect.poll(async () => (await bounds(page)).scroll).toBeGreaterThan(startScroll + 30);
  await input.detach();
});

test("mobile sidebar navigates from wrapped code and preserves native nested horizontal scrolling", async ({ page, browserName }) => {
  await page.goto("/tests/history-browser.html?mobile-sidebar=1&sidebar-code=1");
  const code = page.getByTestId("sidebar-code").locator("pre");
  await expect(code).toBeVisible();
  // Mobile code already wraps; its tag alone must not disable navigation.
  await touch(page, "touchstart", 55, 700, 1000, '[data-testid="sidebar-code"] pre');
  expect(await touch(page, "touchmove", 275, 702, 1200, '[data-testid="sidebar-code"] pre')).toBe(true);
  await touch(page, "touchend", 275, 702, 1400, '[data-testid="sidebar-code"] pre');
  await settled(page, true);
  await page.getByRole("button", { name: "收起", exact: true }).click();
  await settled(page, false);
  // Selection still wins over navigation, including inside a wrapped block.
  await code.evaluate(node => {
    const range = document.createRange(); range.selectNodeContents(node);
    window.getSelection()!.removeAllRanges(); window.getSelection()!.addRange(range);
  });
  await touch(page, "touchstart", 55, 700, 2000, '[data-testid="sidebar-code"] pre');
  expect(await touch(page, "touchmove", 275, 702, 2200, '[data-testid="sidebar-code"] pre')).toBe(false);
  await touch(page, "touchend", 275, 702, 2400, '[data-testid="sidebar-code"] pre');
  await settled(page, false);
  await page.evaluate(() => window.getSelection()!.removeAllRanges());
  if (browserName !== "chromium") return;
  const input = await page.context().newCDPSession(page);
  await input.send("Emulation.setTouchEmulationEnabled", { enabled: true });
  // A truly wide code block and a normal horizontal scroller still scroll
  // natively despite pan-y on the surrounding navigation/scroll surfaces.
  await code.evaluate(node => {
    node.style.whiteSpace = "pre"; node.style.overflowX = "auto";
    node.textContent = "wide code ".repeat(80);
  });
  for (const selector of ['[data-testid="sidebar-code"] pre', '[data-testid="sidebar-horizontal"]']) {
    const scroller = page.locator(selector);
    const box = (await scroller.boundingBox())!;
    const y = box.y + box.height / 2;
    await input.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: 280, y, id: 1 }] });
    for (const x of [260, 230, 190, 140, 80]) {
      await input.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x, y, id: 1 }] });
      await page.waitForTimeout(24);
    }
    await input.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    await expect.poll(() => scroller.evaluate(node => node.scrollLeft)).toBeGreaterThan(80);
    await settled(page, false);
  }
  await input.detach();
});

test("mobile sidebar reveals one lower surface behind both rounded page corners", async ({ page }) => {
  for (const theme of ["light", "dark"]) {
    await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
    await page.getByTestId("sidebar-toggle").click();
    await settled(page, true);
    const layers = await page.evaluate(() => {
      const shell = document.querySelector(".shell")!;
      const pane = document.querySelector(".pane")!;
      const sidebar = document.querySelector(".sessions")!;
      const scrim = document.querySelector(".scrim-side")!;
      const rect = pane.getBoundingClientRect();
      const cornerTarget = (y: number) => document.elementFromPoint(rect.left + 2, y) === shell;
      return {
        shellBg: getComputedStyle(shell).backgroundColor,
        sidebarBg: getComputedStyle(sidebar).backgroundColor,
        pageZ: Number(getComputedStyle(pane).zIndex), sidebarZ: Number(getComputedStyle(sidebar).zIndex),
        top: parseFloat(getComputedStyle(pane).borderTopLeftRadius),
        bottom: parseFloat(getComputedStyle(pane).borderBottomLeftRadius),
        scrimBottom: getComputedStyle(scrim).borderBottomLeftRadius,
        pageBottom: getComputedStyle(pane).borderBottomLeftRadius,
        topCutout: cornerTarget(rect.top + 2), bottomCutout: cornerTarget(rect.bottom - 2),
      };
    });
    expect(layers.shellBg).toBe(layers.sidebarBg);
    expect(layers.pageZ).toBeGreaterThan(layers.sidebarZ);
    expect(layers.top).toBeGreaterThanOrEqual(36);
    expect(layers.bottom).toBe(layers.top);
    expect(layers.scrimBottom).toBe(layers.pageBottom);
    expect(layers.topCutout).toBe(true);
    expect(layers.bottomCutout).toBe(true);
    await page.screenshot({ path: test.info().outputPath(`sidebar-layered-${theme}.png`) });
    await page.getByRole("button", { name: "收起", exact: true }).click();
    await settled(page, false);
    await expect.poll(() => page.locator(".pane").evaluate(node => getComputedStyle(node).borderRadius)).toBe("0px");
  }
});

test("mobile sidebar opening feedback fires once and ignores canceled drags and desktop", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "maxTouchPoints", { configurable: true, value: 1 });
    Object.defineProperty(navigator, "vibrate", { configurable: true, value: (duration: number) => {
      document.body.dataset.pulses = String(Number(document.body.dataset.pulses ?? 0) + 1);
      document.body.dataset.pulseDuration = String(duration);
      return true;
    } });
  });
  await page.reload();
  await expect(page.locator(".thread")).toBeVisible();
  const count = () => page.evaluate(() => Number(document.body.dataset.pulses ?? 0));
  expect(await count()).toBe(0);
  await touch(page, "touchstart", 50, 280, 1000);
  await touch(page, "touchmove", 140, 280, 1200);
  await touch(page, "touchend", 140, 280, 1400);
  await settled(page, false);
  expect(await count()).toBe(0);
  await touch(page, "touchstart", 50, 280, 2000);
  await touch(page, "touchmove", 280, 280, 2200);
  await touch(page, "touchcancel", 280, 280, 2400);
  await settled(page, false);
  expect(await count()).toBe(0);
  await page.getByTestId("sidebar-toggle").click();
  await settled(page, true);
  expect(await count()).toBe(1);
  await page.evaluate(() => window.dispatchEvent(new Event("sidebar-fixture-stream")));
  expect(await count()).toBe(1);
  await page.getByRole("button", { name: "收起", exact: true }).click();
  await settled(page, false);
  await touch(page, "touchstart", 50, 280, 3000);
  await touch(page, "touchmove", 280, 280, 3200);
  await touch(page, "touchend", 280, 280, 3400);
  await settled(page, true);
  expect(await count()).toBe(2);
  await page.getByRole("button", { name: "收起", exact: true }).click();
  await settled(page, false);
  await page.setViewportSize({ width: 1200, height: 800 });
  await page.getByTestId("sidebar-toggle").click();
  expect(await count()).toBe(2);
});

test("mobile sidebar still opens when the browser rejects feedback", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "maxTouchPoints", { configurable: true, value: 1 });
    Object.defineProperty(navigator, "vibrate", { configurable: true, value: () => { throw new Error("Unavailable"); } });
  });
  await page.reload();
  await expect(page.locator(".thread")).toBeVisible();
  await page.getByTestId("sidebar-toggle").click();
  await settled(page, true);
});

test("mobile sidebar Safari toggle uses a genuine native switch and follows swipe state", async ({ page }) => {
  test.skip(!await page.evaluate(() => "switch" in document.createElement("input")),
    "Native switch feedback is a WebKit capability; physical vibration still needs an iPhone.");
  await page.evaluate(() => {
    Object.defineProperty(navigator, "maxTouchPoints", { configurable: true, value: 1 });
    Object.defineProperty(navigator, "vibrate", { configurable: true, value: undefined });
    document.addEventListener("click", event => {
      if (event.target instanceof HTMLInputElement && event.target.hasAttribute("switch")) {
        document.body.dataset.nativeFeedback = JSON.stringify({
          trusted: event.isTrusted, active: navigator.userActivation.isActive,
        });
      }
    }, true);
  });
  await page.getByTestId("sidebar-toggle").click();
  await settled(page, true);
  expect(await page.evaluate(() => JSON.parse(document.body.dataset.nativeFeedback ?? "null"))).toEqual({
    trusted: true, active: true,
  });
  const control = page.getByRole("switch", { name: "显示会话侧栏", includeHidden: true });
  expect(await control.isChecked()).toBe(true);
  await page.getByRole("button", { name: "收起", exact: true }).click();
  await settled(page, false);
  expect(await control.isChecked()).toBe(false);
  await touch(page, "touchstart", 50, 280, 1000);
  await touch(page, "touchmove", 280, 280, 1200);
  await touch(page, "touchend", 280, 280, 1400);
  await settled(page, true);
  expect(await control.isChecked()).toBe(true);
  await page.setViewportSize({ width: 1200, height: 800 });
  await expect(page.getByTestId("sidebar-toggle")).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator('input[switch]')).toHaveCount(0);
});
