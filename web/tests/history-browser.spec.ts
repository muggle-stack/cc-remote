import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";

const productionCsp = (() => {
  const template = readFileSync(
    new URL("../../deploy/Caddyfile", import.meta.url),
    "utf8",
  );
  const match = template.match(/Content-Security-Policy "([^"]+)"/);
  if (!match) throw new Error("production Content-Security-Policy is missing");
  return match[1].replace(
    "wss://cc-remote.example.com",
    "ws://127.0.0.1:4174",
  );
})();

async function applyProductionCsp(
  page: import("@playwright/test").Page,
): Promise<void> {
  await page.evaluate((policy) => {
    const meta = document.createElement("meta");
    meta.httpEquiv = "Content-Security-Policy";
    meta.content = policy;
    document.head.append(meta);
  }, productionCsp);
}

async function pinchThenPanPreview(
  page: import("@playwright/test").Page,
): Promise<{
  afterPinch: { scale: number; x: number; y: number };
  afterPan: { scale: number; x: number; y: number };
}> {
  return page.locator(".image-lightbox").evaluate(async (node) => {
    const stage = node as HTMLElement;
    const visual = stage.querySelector<HTMLElement>(".image-lightbox-visual");
    if (!visual) throw new Error("lightbox visual is missing");
    Object.defineProperties(stage, {
      setPointerCapture: { configurable: true, value: () => {} },
      releasePointerCapture: { configurable: true, value: () => {} },
      hasPointerCapture: { configurable: true, value: () => false },
    });
    const emit = (
      type: "pointerdown" | "pointermove" | "pointerup"
        | "lostpointercapture",
      pointerId: number,
      x: number,
      y: number,
    ) => stage.dispatchEvent(new PointerEvent(type, {
      bubbles: true,
      cancelable: true,
      pointerId,
      pointerType: "touch",
      clientX: x,
      clientY: y,
      buttons: type === "pointerup" || type === "lostpointercapture" ? 0 : 1,
    }));
    const nextPaint = () => new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    });
    const transform = () => {
      const matrix = new DOMMatrix(visual.style.transform);
      return {
        x: matrix.e,
        y: matrix.f,
        scale: Math.hypot(matrix.a, matrix.b),
      };
    };

    emit("pointerdown", 1, 150, 260);
    emit("pointerdown", 2, 250, 260);
    emit("pointermove", 1, 50, 260);
    emit("pointermove", 2, 350, 260);
    await nextPaint();
    const afterPinch = transform();

    emit("lostpointercapture", 2, 350, 260);
    emit("pointermove", 1, 0, 300);
    await nextPaint();
    const afterPan = transform();
    emit("pointerup", 1, 50, 300);
    stage.dispatchEvent(new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      detail: 1,
    }));
    return { afterPinch, afterPan };
  });
}

async function wheelZoomThenPanPreview(
  page: import("@playwright/test").Page,
): Promise<{
  zoomPrevented: boolean;
  panPrevented: boolean;
  afterZoom: { scale: number; x: number; y: number };
  afterPan: { scale: number; x: number; y: number };
}> {
  return page.locator(".image-lightbox").evaluate(async (node) => {
    const stage = node as HTMLElement;
    const visual = stage.querySelector<HTMLElement>(".image-lightbox-visual");
    if (!visual) throw new Error("lightbox visual is missing");
    Object.defineProperties(stage, {
      clientWidth: { configurable: true, value: 900 },
      clientHeight: { configurable: true, value: 720 },
    });
    Object.defineProperties(visual, {
      clientWidth: { configurable: true, value: 600 },
      clientHeight: { configurable: true, value: 400 },
    });
    const nextPaint = () => new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    });
    const transform = () => {
      const matrix = new DOMMatrix(visual.style.transform);
      return {
        x: matrix.e,
        y: matrix.f,
        scale: Math.hypot(matrix.a, matrix.b),
      };
    };
    const zoomPrevented = !stage.dispatchEvent(new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      ctrlKey: true,
      clientX: 450,
      clientY: 360,
      deltaY: -600,
      deltaMode: WheelEvent.DOM_DELTA_PIXEL,
    }));
    await nextPaint();
    const afterZoom = transform();
    const panPrevented = !stage.dispatchEvent(new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      clientX: 450,
      clientY: 360,
      deltaX: 45,
      deltaY: 30,
      deltaMode: WheelEvent.DOM_DELTA_PIXEL,
    }));
    await nextPaint();
    return {
      zoomPrevented,
      panPrevented,
      afterZoom,
      afterPan: transform(),
    };
  });
}

async function readingAnchor(page: import("@playwright/test").Page): Promise<{
  id: string;
  offset: number;
}> {
  return page.evaluate(() => {
    const viewport = document.querySelector<HTMLElement>(".thread");
    if (!viewport) throw new Error("thread viewport is missing");
    const viewportRect = viewport.getBoundingClientRect();
    const rows = [...document.querySelectorAll<HTMLElement>("[data-turn-id]")]
      .map((row) => ({ row, rect: row.getBoundingClientRect() }))
      .filter(({ rect }) =>
        rect.bottom > viewportRect.top && rect.top < viewportRect.bottom);
    const selected = rows.sort((left, right) =>
      Math.abs(left.rect.top - viewportRect.top)
      - Math.abs(right.rect.top - viewportRect.top))[0];
    const id = selected?.row.dataset.turnId;
    if (!selected || !id) throw new Error("no visible reading anchor");
    return { id, offset: selected.rect.top - viewportRect.top };
  });
}

async function processDetailEdge(
  page: import("@playwright/test").Page,
  edge: "start" | "end",
): Promise<number> {
  return page.locator('[data-turn-id="detail-page"]').evaluate(
    (turn, selectedEdge) => {
      const viewport = document.querySelector<HTMLElement>(".thread");
      const process = turn.querySelector<HTMLElement>(
        "[data-process-detail-root]",
      );
      if (!viewport || !process) throw new Error("detail process is missing");
      const viewportRect = viewport.getBoundingClientRect();
      const processRect = process.getBoundingClientRect();
      return (selectedEdge === "end" ? processRect.bottom : processRect.top)
        - viewportRect.top;
    },
    edge,
  );
}

async function turnIntersectsViewport(
  page: import("@playwright/test").Page,
  turnId: string,
): Promise<boolean> {
  return page.evaluate((id) => {
    const viewport = document.querySelector<HTMLElement>(".thread");
    const row = document.querySelector<HTMLElement>(
      `[data-turn-id="${CSS.escape(id)}"]`,
    );
    if (!viewport || !row) return false;
    const viewportRect = viewport.getBoundingClientRect();
    const rowRect = row.getBoundingClientRect();
    return rowRect.bottom > viewportRect.top && rowRect.top < viewportRect.bottom;
  }, turnId);
}

async function waitForScrollIdle(
  page: import("@playwright/test").Page,
): Promise<void> {
  let previous: number | null = null;
  let stableSamples = 0;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const current = await page.locator(".thread").evaluate(
      (node) => node.scrollTop,
    );
    stableSamples = previous != null && Math.abs(current - previous) < 0.5
      ? stableSamples + 1 : 0;
    if (stableSamples >= 4) return;
    previous = current;
    await page.waitForTimeout(50);
  }
  throw new Error("thread scroll position did not settle");
}

async function nativeSelectionSnapshot(
  page: import("@playwright/test").Page,
): Promise<{
  anchorTurnId: string | null;
  focusTurnId: string | null;
  anchorConnected: boolean;
  text: string;
}> {
  return page.evaluate(() => {
    const selection = window.getSelection();
    const turnId = (node: Node | null): string | null => {
      const element = node instanceof Element ? node : node?.parentElement;
      return element?.closest<HTMLElement>("[data-turn-id]")
        ?.dataset.turnId ?? null;
    };
    return {
      anchorTurnId: turnId(selection?.anchorNode ?? null),
      focusTurnId: turnId(selection?.focusNode ?? null),
      anchorConnected: selection?.anchorNode?.isConnected ?? false,
      text: selection?.toString() ?? "",
    };
  });
}

async function textSelectionPoint(
  locator: import("@playwright/test").Locator,
): Promise<{ x: number; y: number }> {
  const point = await locator.evaluate((node) => {
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    const text = walker.nextNode();
    if (!text || !text.textContent?.length) return null;
    const range = document.createRange();
    range.setStart(text, 0);
    range.setEnd(text, Math.min(2, text.textContent.length));
    const rect = range.getBoundingClientRect();
    return { x: rect.left + 1, y: rect.top + rect.height / 2 };
  });
  if (!point) throw new Error("selection text has no geometry");
  return point;
}

async function wheelUntilTurn(
  page: import("@playwright/test").Page,
  turnId: string,
  deltaY: number,
  projectName: string,
): Promise<void> {
  const viewport = page.locator(".thread");
  if (projectName === "webkit") {
    for (let attempt = 0; attempt < 40; attempt += 1) {
      if (await turnIntersectsViewport(page, turnId)) {
        if (deltaY < 0) {
          await expect(page.locator(".scroll-bottom-btn")).toBeVisible();
        }
        return;
      }
      await dispatchTouchGesture(page, deltaY < 0 ? 60 : -60);
      await viewport.evaluate((node, delta) => {
        const step = Math.max(
          160,
          Math.min(Math.abs(delta), node.clientHeight * 5),
        );
        node.scrollBy({
          top: Math.sign(delta) * step,
          behavior: "auto",
        });
      }, deltaY);
      await waitForScrollIdle(page);
    }
    expect(await turnIntersectsViewport(page, turnId)).toBe(true);
    return;
  }
  const box = await viewport.boundingBox();
  if (!box) throw new Error("thread viewport has no bounds");
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  for (let attempt = 0; attempt < 12; attempt += 1) {
    if (await turnIntersectsViewport(page, turnId)) return;
    await page.mouse.wheel(0, deltaY);
    await page.waitForTimeout(40);
  }
  expect(await turnIntersectsViewport(page, turnId)).toBe(true);
}

async function scrollThreadToEdge(
  page: import("@playwright/test").Page,
  edge: "start" | "end",
  projectName: string,
): Promise<void> {
  const viewport = page.locator(".thread");
  for (let attempt = 0; attempt < 6; attempt += 1) {
    if (projectName === "webkit") {
      await dispatchTouchGesture(page, edge === "start" ? 60 : -60);
    } else {
      await viewport.dispatchEvent("wheel", {
        deltaY: edge === "start" ? -80 : 80,
      });
    }
    await viewport.evaluate((node, selectedEdge) => {
      node.scrollTo({
        top: selectedEdge === "start" ? 0 : node.scrollHeight,
        behavior: "auto",
      });
      // Synthetic touch events mark genuine reader intent, but Playwright
      // WebKit does not perform the browser's native pan for them. Deliver the
      // matching scroll notification after the fixture's explicit scrollTop
      // write so React and the virtualizer observe the same offset before the
      // next dynamic row measurement.
      node.dispatchEvent(new Event("scroll"));
    }, edge);
    await waitForScrollIdle(page);
    const reached = await viewport.evaluate((node, selectedEdge) => {
      if (selectedEdge === "start") return node.scrollTop <= 1;
      return node.scrollHeight - node.scrollTop - node.clientHeight <= 1;
    }, edge);
    if (reached) {
      if (edge === "start") {
        await expect(page.locator(".scroll-bottom-btn")).toBeVisible();
      }
      return;
    }
  }
  expect(await viewport.evaluate((node, selectedEdge) => {
    if (selectedEdge === "start") return node.scrollTop <= 1;
    return node.scrollHeight - node.scrollTop - node.clientHeight <= 1;
  }, edge)).toBe(true);
}

async function dispatchTouchGesture(
  page: import("@playwright/test").Page,
  fingerDeltaY: number,
  moves = 1,
): Promise<void> {
  await page.locator(".thread").evaluate((node, input) => {
    const target = node as HTMLElement;
    const dispatchTouch = (
      type: "touchstart" | "touchmove" | "touchend",
      clientY: number,
    ) => {
      // WebKit's Touch constructor is intentionally not public. React only
      // needs the TouchEvent list shape, so define it on a real bubbling Event.
      const touch = { identifier: 1, target, clientX: 120, clientY };
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperties(event, {
        touches: { value: type === "touchend" ? [] : [touch] },
        targetTouches: { value: type === "touchend" ? [] : [touch] },
        changedTouches: { value: [touch] },
      });
      target.dispatchEvent(event);
    };
    const startY = 160;
    dispatchTouch("touchstart", startY);
    for (let index = 0; index < input.moves; index += 1) {
      dispatchTouch("touchmove", startY + input.fingerDeltaY * (index + 1));
    }
    dispatchTouch("touchend", startY + input.fingerDeltaY * input.moves);
  }, { fingerDeltaY, moves });
}

async function dispatchTouchPhase(
  page: import("@playwright/test").Page,
  type: "touchstart" | "touchmove" | "touchend",
  clientY: number,
): Promise<void> {
  await page.locator(".thread").evaluate((node, input) => {
    const target = node as HTMLElement;
    const touch = {
      identifier: 1,
      target,
      clientX: 120,
      clientY: input.clientY,
    };
    const event = new Event(input.type, { bubbles: true, cancelable: true });
    Object.defineProperties(event, {
      touches: { value: input.type === "touchend" ? [] : [touch] },
      targetTouches: { value: input.type === "touchend" ? [] : [touch] },
      changedTouches: { value: [touch] },
    });
    target.dispatchEvent(event);
  }, { type, clientY });
}

async function stageDelayedTouchMove(
  page: import("@playwright/test").Page,
  startY: number,
  clientY: number,
): Promise<void> {
  await page.locator(".thread").evaluate((node, input) => {
    const target = node as HTMLElement & { __delayedTouchMove?: Event };
    const startTouch = {
      identifier: 1,
      target,
      clientX: 120,
      clientY: input.startY,
    };
    const start = new Event("touchstart", { bubbles: true, cancelable: true });
    Object.defineProperties(start, {
      touches: { value: [startTouch] },
      targetTouches: { value: [startTouch] },
      changedTouches: { value: [startTouch] },
    });
    target.dispatchEvent(start);
    const touch = { ...startTouch, clientY: input.clientY };
    const event = new Event("touchmove", { bubbles: true, cancelable: true });
    Object.defineProperties(event, {
      touches: { value: [touch] },
      targetTouches: { value: [touch] },
      changedTouches: { value: [touch] },
    });
    target.__delayedTouchMove = event;
  }, { startY, clientY });
}

async function dispatchDelayedTouchMove(
  page: import("@playwright/test").Page,
): Promise<void> {
  await page.locator(".thread").evaluate((node) => {
    const target = node as HTMLElement & { __delayedTouchMove?: Event };
    const event = target.__delayedTouchMove;
    delete target.__delayedTouchMove;
    if (!event) throw new Error("delayed touchmove was not staged");
    target.dispatchEvent(event);
  });
}

async function requestOlderHistory(
  page: import("@playwright/test").Page,
  projectName: string,
  repeat = 1,
): Promise<void> {
  const viewport = page.locator(".thread");
  if (projectName !== "webkit") {
    for (let index = 0; index < repeat; index += 1) {
      await viewport.dispatchEvent("wheel", { deltaY: -80 });
    }
    return;
  }
  await dispatchTouchGesture(page, 60, repeat);
}

async function requestNewerHistory(
  page: import("@playwright/test").Page,
  projectName: string,
  repeat = 1,
): Promise<void> {
  const viewport = page.locator(".thread");
  if (projectName !== "webkit") {
    for (let index = 0; index < repeat; index += 1) {
      await viewport.dispatchEvent("wheel", { deltaY: 80 });
    }
    return;
  }
  await dispatchTouchGesture(page, -60, repeat);
}

test("prepend preserves the exact reading row through delayed row growth", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html");
  const viewport = page.locator(".thread");
  await expect(page.locator('[data-turn-id="o1"]')).toBeVisible();
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const before = await readingAnchor(page);
  await requestOlderHistory(page, testInfo.project.name);
  await expect(page.locator('[data-turn-id="n8"]')).toBeVisible();

  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);

  await page.waitForTimeout(800);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(before.id);
  expect(Math.abs(settled.offset - before.offset)).toBeLessThan(2);
});

test("one click loads every turn-detail page without collapsing or jumping", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&delay=80&growth-delay=180",
  );
  const header = page.locator(".turn-process-head");
  const initialStart = await processDetailEdge(page, "start");
  await header.click();
  await expect(page.locator(".thread"))
    .toHaveAttribute("data-detail-anchor-active", "true");
  await expect(page.getByText("较新命令 1")).toBeVisible();
  await expect(page.getByText("较早命令 1")).toBeVisible();
  await expect(page.getByRole("button", { name: "加载更早过程" }))
    .toHaveCount(0);
  await expect(page.getByRole("button", { name: "返回较新过程" }))
    .toHaveCount(0);
  await page.waitForTimeout(500);
  expect(Math.abs(await processDetailEdge(page, "start") - initialStart))
    .toBeLessThan(2);
  await expect(page.locator(".thread"))
    .toHaveAttribute("data-detail-anchor-active", "false");
});

test("retained truncated process still fetches its authoritative first detail page", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&detail-retained-preview=1"
      + "&delay=500&growth-delay=180",
  );
  const header = page.locator(".turn-process-head");
  await header.click();
  await expect(page.getByText("已缓存的较新命令")).toBeVisible();
  await expect(page.getByText("较早过程已省略")).toBeVisible();
  await expect(header).toHaveAttribute("aria-busy", "true");
  await expect(page.getByText("较早过程已省略")).toHaveCount(0);
  await expect(page.getByText("较新命令 1")).toBeVisible();
  await expect(page.getByText("较早命令 1")).toBeVisible();
});

test("user scrolling cancels a pending turn-detail anchor", async ({ page }) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&detail-scroll-cancel=1"
      + "&delay=250&growth-delay=180",
  );
  const header = page.locator(".turn-process-head");
  await header.scrollIntoViewIfNeeded();
  await header.click();
  await page.waitForTimeout(40);
  const viewport = page.locator(".thread");
  await viewport.dispatchEvent("wheel", { deltaY: 90 });
  await viewport.evaluate((node) => { node.scrollTop += 90; });
  await page.waitForTimeout(40);
  const userOffset = await processDetailEdge(page, "start");

  await expect(page.getByText("较早命令 1")).toBeVisible();
  await page.waitForTimeout(450);
  expect(Math.abs(await processDetailEdge(page, "start") - userOffset))
    .toBeLessThan(2);
});

test("history page cache upgrades v1 and preserves pages in real IndexedDB", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html");
  const result = await page.evaluate(async () => {
    const modulePath = "/src/history-page-cache.ts";
    const cacheModule = await import(modulePath);
    const scope = {
      machineId: "browser-machine",
      engine: "codex",
      space: "code",
      sid: "browser-cache-session",
      revision: "browser-cache-revision",
    };
    const pageKey = "v1-page";
    const key = cacheModule.historyPageCachePageKey(scope, pageKey);
    const scopeKey = cacheModule.historyPageCacheScopeKey(scope);
    const sessionKey = cacheModule.historyPageCacheSessionKey(scope);
    const legacyDb = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open(cacheModule.HISTORY_PAGE_CACHE_DB_NAME, 1);
      request.onupgradeneeded = () => {
        const store = request.result.createObjectStore("pages", {
          keyPath: "key",
        });
        store.createIndex("scope", "scopeKey", { unique: false });
        store.createIndex("session", "sessionKey", { unique: false });
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    await new Promise<void>((resolve, reject) => {
      const transaction = legacyDb.transaction("pages", "readwrite");
      transaction.objectStore("pages").put({
        version: 1,
        key,
        scopeKey,
        sessionKey,
        ...scope,
        pageKey,
        page: {
          pageKey,
          turns: [{
            id: "legacy-turn",
            prompt: "legacy",
            blocks: [],
            done: true,
          }],
          hasOlder: false,
          olderCursor: "legacy-turn",
          hasNewer: false,
          newerPageKey: null,
          isLatest: false,
        },
        savedAt: 1,
        byteSize: 256,
      });
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error);
    });
    legacyDb.close();

    const cache = new cacheModule.HistoryPageCache();
    const upgraded = await cache.getPage(scope, pageKey);
    const stored = await cache.putPage(scope, {
      pageKey,
      turns: [{
        id: "new-turn",
        prompt: "new",
        blocks: [],
        done: true,
      }],
      hasOlder: false,
      olderCursor: "legacy-turn",
    });
    const merged = await cache.getPage(scope, pageKey);
    const invalidated = await cache.invalidateScope(scope);
    const afterInvalidation = await cache.getPage(scope, pageKey);
    return {
      upgradedIds: upgraded?.turns.map((turn: { id: string }) => turn.id),
      stored,
      mergedIds: merged?.turns.map((turn: { id: string }) => turn.id),
      invalidated,
      afterInvalidation,
    };
  });
  expect(result.upgradedIds).toEqual(["legacy-turn"]);
  expect(result.stored.ok).toBe(true);
  expect(result.mergedIds).toEqual(["legacy-turn", "new-turn"]);
  expect(result.invalidated.ok).toBe(true);
  expect(result.afterInvalidation).toBeNull();
});

test("instant session cache preserves a heavy turn's complete process skeleton", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html");
  const result = await page.evaluate(async () => {
    const cache = await import("/src/cache.ts");
    await cache.clearCache();
    cache.saveSession("heavy-refresh-session", [{
      id: "heavy-refresh-turn",
      prompt: "1",
      done: true,
      images: [{
        media_type: "image/png",
        data: "i".repeat(2 * 1024 * 1024 + 1),
      }],
      blocks: [
        {
          kind: "text", message_id: "heavy-commentary", text: "2",
          done: true, channel: "commentary",
        },
        {
          kind: "tool", message_id: "heavy-tool-message-3",
          tool_use_id: "heavy-tool-3", tool: "Read",
          input: { file_path: "/tmp/3" },
          result: {
            content: "x".repeat(2 * 1024 * 1024 + 1),
            is_error: false,
          },
          done: true,
        },
        {
          kind: "tool", message_id: "heavy-tool-message-4",
          tool_use_id: "heavy-tool-4", tool: "Bash",
          input: { command: "echo 4" }, done: true,
        },
        {
          kind: "process", item_id: "heavy-process-5",
          processKind: "command", phase: "snapshot", status: "succeeded",
          title: "5", done: true,
        },
        {
          kind: "text", message_id: "heavy-final", text: "6",
          done: true, channel: "final",
        },
      ],
      detailEventCount: 4,
    }], 42, "heavy-refresh-r1", "heavy-refresh-g1");
    await new Promise((resolve) => window.setTimeout(resolve, 700));
    const loaded = await cache.loadSession("heavy-refresh-session");
    const turn = loaded?.turns[0] as {
      images?: unknown[];
      blocks?: Array<Record<string, unknown>>;
      detailProjection?: unknown;
    } | undefined;
    return {
      size: new TextEncoder().encode(JSON.stringify(turn)).byteLength,
      hasImages: Array.isArray(turn?.images),
      hasDetailProjection: turn?.detailProjection != null,
      process: turn?.blocks?.map((block) => (
        block.kind === "text" ? block.text
          : block.kind === "tool" ? block.tool_use_id
            : block.title
      )),
    };
  });
  expect(result.size).toBeLessThan(2 * 1024 * 1024);
  expect(result.hasImages).toBe(false);
  expect(result.hasDetailProjection).toBe(false);
  expect(result.process).toEqual([
    "2", "heavy-tool-3", "heavy-tool-4", "5", "6",
  ]);
});

test("a canonical image reference does not reserve a second hidden image row", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?dual-image=1");
  const assertCanonicalImageLayout = async () => {
    const turn = page.locator('[data-turn-id="dual-image"]');
    await expect(turn).toBeVisible();
    await expect(turn.locator(".ubub-image-trigger")).toHaveCount(1);
    const gap = await turn.evaluate((node) => {
      const image = node.querySelector<HTMLElement>(".ubub-image-trigger");
      const meta = node.querySelector<HTMLElement>(".ubub-meta");
      if (!image || !meta) throw new Error("image layout is incomplete");
      return meta.getBoundingClientRect().top - image.getBoundingClientRect().bottom;
    });
    expect(gap).toBeLessThan(20);
  };

  await assertCanonicalImageLayout();
  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();
  await page.getByTestId("switch-session").click();
  await assertCanonicalImageLayout();
});

test("streaming rerenders cannot cancel an image preview close", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?dual-image=1");
  await page.locator(".ubub-image-trigger").click();
  await expect(page.locator(".image-lightbox")).toBeVisible();

  await page.evaluate(() => {
    document.querySelector<HTMLButtonElement>(".image-lightbox-close")?.click();
    document.querySelector<HTMLButtonElement>('[data-testid="append-turn"]')?.click();
  });

  await expect(page.locator(".image-lightbox")).toHaveCount(0);
});

test("expanded tool batches use dense rows instead of individual cards", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?compact-tools=1");
  await page.locator(".turn-process-head").click();
  await page.locator(".tool-group-h").click();

  const rows = page.locator(".tool-group-b .tool");
  await expect(rows).toHaveCount(3);
  const styles = await rows.evaluateAll((nodes) => nodes.map((node) => {
    const style = getComputedStyle(node);
    return {
      height: node.getBoundingClientRect().height,
      border: style.borderTopWidth,
      radius: style.borderTopLeftRadius,
      shadow: style.boxShadow,
    };
  }));
  for (const style of styles) {
    expect(style.height).toBeLessThan(40);
    expect(style.border).toBe("0px");
    expect(style.radius).toBe("0px");
    expect(style.shadow).toBe("none");
  }
});

test("completed Mermaid fences render isolated sanitized SVGs", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?mermaid=1");
  const diagrams = page.locator(".mermaid-block");
  await expect(diagrams).toHaveCount(2);
  await expect(page.locator(".mermaid-svg")).toHaveCount(2);
  await expect(diagrams.nth(0)).toHaveAttribute("data-mermaid-state", "ready");
  await expect(diagrams.nth(1)).toHaveAttribute("data-mermaid-state", "ready");

  const rootIds = await page.locator(".mermaid-svg > svg").evaluateAll(
    (nodes) => nodes.map((node) => node.id),
  );
  expect(rootIds.every(Boolean)).toBe(true);
  expect(new Set(rootIds).size).toBe(rootIds.length);
  await expect(page.locator(
    ".mermaid-svg script, .mermaid-svg foreignObject, .mermaid-svg image, "
      + ".mermaid-svg a",
  )).toHaveCount(0);
  const unsafeReferences = await page.locator(".mermaid-svg svg").evaluateAll(
    (nodes) => nodes.flatMap((node) =>
      [...node.querySelectorAll("*")].flatMap((element) =>
        [...element.attributes]
          .filter((attribute) => ["href", "xlink:href", "src"].includes(
            attribute.name.toLowerCase(),
          ))
          .map((attribute) => attribute.value)
          .filter((value) => !value.startsWith("#")))),
  );
  expect(unsafeReferences).toEqual([]);
  const clippedNodes = await diagrams.nth(0).evaluate((node) => {
    const svg = node.querySelector("svg");
    if (!svg) throw new Error("flowchart SVG is missing");
    const bounds = svg.getBoundingClientRect();
    return [...svg.querySelectorAll<SVGGraphicsElement>(".node")].filter((item) => {
      const rect = item.getBoundingClientRect();
      return rect.left < bounds.left - 1 || rect.right > bounds.right + 1
        || rect.top < bounds.top - 1 || rect.bottom > bounds.bottom + 1;
    }).length;
  });
  expect(clippedNodes).toBe(0);

  for (const diagram of await diagrams.all()) {
    const sizes = await diagram.evaluate((node) => {
      const svg = node.querySelector("svg");
      if (!svg) throw new Error("rendered Mermaid is missing its SVG");
      return {
        container: node.getBoundingClientRect().width,
        svg: svg.getBoundingClientRect().width,
      };
    });
    expect(sizes.svg).toBeLessThanOrEqual(sizes.container + 1);
  }

  const lightId = await page.locator(".mermaid-svg > svg").first().getAttribute("id");
  await page.evaluate(() => {
    document.documentElement.dataset.theme = "dark";
  });
  await expect.poll(async () =>
    page.locator(".mermaid-svg > svg").first().getAttribute("id"),
  ).not.toBe(lightId);
  await expect(diagrams.nth(0)).toHaveAttribute("data-mermaid-state", "ready");
});

test("completed chat formulas lazy-load accessible KaTeX markup", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?math=1");
  await expect(page.locator(".katex-display")).toHaveCount(1);
  await expect(page.locator(".katex")).toHaveCount(2);
  await expect(page.locator(".katex-mathml math")).toHaveCount(2);
  await expect(page.locator('[data-turn-id="math"]')).toContainText(
    "Inline:",
  );
  await expect(page.locator('[data-turn-id="math"] .message-code-copy'))
    .toHaveCount(0);
});

test("a completed Mermaid diagram opens the shared pinch-zoom preview", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/tests/history-browser.html?mermaid=1");
  const diagram = page.locator(".mermaid-block").first();
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  await applyProductionCsp(page);

  await diagram.locator(".mermaid-zoom").click();
  const preview = page.getByRole("dialog", { name: "Mermaid 图表预览" });
  await expect(preview).toBeVisible();
  const vector = preview.locator(".image-lightbox-vector > svg");
  await expect(vector).toBeVisible();
  await expect(preview.locator("img")).toHaveCount(0);
  const gesture = await pinchThenPanPreview(page);
  expect(gesture.afterPinch.scale).toBeGreaterThan(1);
  expect(gesture.afterPan.scale).toBe(gesture.afterPinch.scale);
  expect(gesture.afterPan.x).toBeLessThan(gesture.afterPinch.x);
  await expect(preview).toBeVisible();
  await expect(preview).not.toHaveClass(/interacting/);
  await expect.poll(() => preview.locator(".image-lightbox-visual")
    .evaluate((node) => getComputedStyle(node).willChange)).toBe("auto");

  await page.getByRole("button", { name: "关闭 Mermaid 图表预览" }).click();
  await expect(preview).toHaveCount(0);

  await diagram.locator(".mermaid-svg").click();
  await expect(page.getByRole("dialog", { name: "Mermaid 图表预览" }))
    .toBeVisible();
  await page.evaluate(() => {
    document.querySelector<HTMLButtonElement>('[data-testid="switch-session"]')
      ?.click();
  });
  await expect(page.locator(".image-lightbox")).toHaveCount(0);
});

test("the real wide Robot Core diagram opens once and fits the viewport", async ({
  page,
}) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.setViewportSize({ width: 1568, height: 870 });
  await page.goto("/tests/history-browser.html?actual-mermaid=1");
  const diagram = page.locator(".mermaid-block");
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  const trigger = diagram.locator(".mermaid-svg");
  await expect(trigger).toHaveJSProperty("tagName", "BUTTON");
  const inlineBounds = await trigger.evaluate((node) => {
    const svg = node.querySelector("svg");
    if (!svg) throw new Error("inline Mermaid SVG is missing");
    return {
      containerWidth: node.getBoundingClientRect().width,
      svgWidth: svg.getBoundingClientRect().width,
    };
  });
  expect(inlineBounds.svgWidth).toBeLessThanOrEqual(
    inlineBounds.containerWidth + 1,
  );

  await trigger.click();
  const preview = page.getByRole("dialog", { name: "Mermaid 图表预览" });
  await expect.poll(async () => {
    if (pageErrors.length > 0) return `pageerror: ${pageErrors.join(" | ")}`;
    return await preview.isVisible() ? "visible" : "missing";
  }).toBe("visible");
  const bounds = await preview.evaluate((node) => {
    const visual = node.querySelector<HTMLElement>(".image-lightbox-visual");
    if (!visual) throw new Error("lightbox visual is missing");
    const stage = node.getBoundingClientRect();
    const image = visual.getBoundingClientRect();
    return {
      stage: {
        left: stage.left,
        top: stage.top,
        right: stage.right,
        bottom: stage.bottom,
      },
      image: {
        left: image.left,
        top: image.top,
        right: image.right,
        bottom: image.bottom,
      },
    };
  });
  expect(bounds.image.left).toBeGreaterThanOrEqual(bounds.stage.left + 17);
  expect(bounds.image.right).toBeLessThanOrEqual(bounds.stage.right - 17);
  expect(bounds.image.top).toBeGreaterThanOrEqual(bounds.stage.top + 17);
  expect(bounds.image.bottom).toBeLessThanOrEqual(bounds.stage.bottom - 17);

  await preview.evaluate((node) => {
    const visual = node.querySelector<HTMLElement>(".image-lightbox-visual");
    if (!visual) throw new Error("lightbox visual is missing");
    const bounds = visual.getBoundingClientRect();
    node.dispatchEvent(new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      clientX: (bounds.left + bounds.right) / 2,
      clientY: (bounds.top + bounds.bottom) / 2,
      detail: 1,
    }));
  });
  await expect(preview).toBeVisible();
  expect(pageErrors).toEqual([]);
});

test("desktop trackpad wheel zooms around the pointer and pans the preview", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "desktop trackpad behavior");
  await page.goto("/tests/history-browser.html?mermaid=1");
  const diagram = page.locator(".mermaid-block").first();
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  await diagram.locator(".mermaid-zoom").click();

  const gesture = await wheelZoomThenPanPreview(page);
  expect(gesture.zoomPrevented).toBe(true);
  expect(gesture.panPrevented).toBe(true);
  expect(gesture.afterZoom.scale).toBeGreaterThan(1);
  expect(gesture.afterPan.scale).toBe(gesture.afterZoom.scale);
  expect(gesture.afterPan.x).toBeLessThan(gesture.afterZoom.x);
  expect(gesture.afterPan.y).toBeLessThan(gesture.afterZoom.y);
  await expect(page.getByRole("dialog", { name: "Mermaid 图表预览" }))
    .toBeVisible();
});

test("a wide zoomed Mermaid can pan to both horizontal edges", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "desktop trackpad behavior");
  await page.setViewportSize({ width: 900, height: 720 });
  await page.goto("/tests/history-browser.html?mermaid=1");
  const diagram = page.locator(".mermaid-block").nth(1);
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  await diagram.locator(".mermaid-zoom").click();
  const preview = page.getByRole("dialog", { name: "Mermaid 图表预览" });
  await page.waitForTimeout(200);
  await preview.dispatchEvent("wheel", {
    ctrlKey: true,
    deltaY: -1_000,
    clientX: 450,
    clientY: 360,
  });
  await page.waitForTimeout(120);

  const horizontalEdges = async () => preview.evaluate((node) => {
    const visual = node.querySelector<HTMLElement>(".image-lightbox-visual");
    if (!visual) throw new Error("lightbox visual is missing");
    const stage = node.getBoundingClientRect();
    const image = visual.getBoundingClientRect();
    return {
      stageLeft: stage.left,
      stageRight: stage.right,
      imageLeft: image.left,
      imageRight: image.right,
    };
  });
  await preview.dispatchEvent("wheel", {
    deltaX: 5_000,
    clientX: 450,
    clientY: 360,
  });
  await page.waitForTimeout(120);
  const rightEdge = await horizontalEdges();
  expect(Math.abs(rightEdge.imageRight - rightEdge.stageRight)).toBeLessThan(1);

  await preview.dispatchEvent("wheel", {
    deltaX: -10_000,
    clientX: 450,
    clientY: 360,
  });
  await page.waitForTimeout(120);
  const leftEdge = await horizontalEdges();
  expect(Math.abs(leftEdge.imageLeft - leftEdge.stageLeft)).toBeLessThan(1);
});

test("desktop Mermaid content clicks are inert and the backdrop closes", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "desktop mouse behavior");
  await page.goto("/tests/history-browser.html?mermaid=1");
  const diagram = page.locator(".mermaid-block").first();
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  await diagram.locator(".mermaid-zoom").click();
  const preview = page.getByRole("dialog", { name: "Mermaid 图表预览" });
  const visual = preview.locator(".image-lightbox-visual");
  const scale = () => visual.evaluate((node) => {
    const matrix = new DOMMatrix(node.style.transform);
    return Math.hypot(matrix.a, matrix.b);
  });

  await preview.click({ position: { x: 450, y: 360 } });
  await expect(preview).toBeVisible();
  await expect.poll(scale).toBeCloseTo(1, 5);

  await preview.click({ position: { x: 5, y: 5 } });
  await expect(preview).toHaveCount(0);
});

test("invalid Mermaid falls back to copyable source", async ({ page }) => {
  await page.goto("/tests/history-browser.html?invalid-mermaid=1");
  const diagram = page.locator(".mermaid-block");
  await expect(diagram).toHaveAttribute("data-mermaid-state", "error");
  await expect(diagram.locator(".mermaid-source")).toContainText(
    "this is not a supported diagram",
  );
  await expect(diagram.getByRole("button", {
    name: "复制 Mermaid 源码",
  })).toBeVisible();
  await expect(diagram.locator(".mermaid-svg")).toHaveCount(0);
});

test("offscreen historical Mermaid does not load until its row is mounted", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?mermaid-history=1");
  await expect(page.locator('[data-turn-id="after-mermaid-40"]')).toBeVisible();
  await expect(page.locator(".mermaid-block")).toHaveCount(0);
  const before = await page.evaluate(() => performance.getEntriesByType("resource")
    .map((entry) => entry.name));
  expect(before.some((url) =>
    /\/node_modules\/\.vite\/deps\/mermaid(?:\.js|-)/i.test(url),
  )).toBe(false);

  await scrollThreadToEdge(page, "start", testInfo.project.name);
  const diagramTurn = page.locator('[data-turn-id="mermaid"]');
  await expect(diagramTurn).toBeVisible();
  await expect(diagramTurn.locator(".mermaid-block")).toHaveCount(2);
  await expect(diagramTurn.locator(".mermaid-svg")).toHaveCount(2);
});

test("switching sessions discards an in-flight Mermaid render", async ({
  page,
}) => {
  await page.route(/\/node_modules\/\.vite\/deps\/mermaid(?:\.js|-)/i, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 250));
    await route.continue();
  });
  await page.goto("/tests/history-browser.html?mermaid=1");
  await expect(page.locator(".mermaid-block")).toHaveCount(2);
  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();
  await page.waitForTimeout(400);
  await expect(page.locator(".mermaid-block")).toHaveCount(0);
  await expect(page.locator(
    "body > svg, body > div[id*='cc-remote-mermaid']",
  )).toHaveCount(0);

  await page.getByTestId("switch-session").click();
  await expect(page.locator(
    '[data-turn-id="mermaid"] .mermaid-svg',
  )).toHaveCount(2);
});

test("a pending composer image previews without triggering removal", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?composer-attachment=1");
  const preview = page.getByRole("button", { name: "预览待发送图片 1" });
  await expect(preview).toBeVisible();

  await preview.click();
  await expect(page.locator(".image-lightbox")).toBeVisible();
  await expect(page.locator(".image-lightbox img.image-lightbox-image"))
    .toBeVisible();
  await expect(page.locator(".image-lightbox-vector")).toHaveCount(0);
  await page.getByRole("button", { name: "关闭图片预览" }).click();
  await expect(page.locator(".image-lightbox")).toHaveCount(0);
  await expect(preview).toBeVisible();

  await preview.click();
  await expect(page.locator(".image-lightbox")).toBeVisible();
  await page.locator(".image-lightbox").evaluate((node) => {
    const stage = node as HTMLElement;
    const visual = stage.querySelector<HTMLElement>(".image-lightbox-visual");
    if (!visual) throw new Error("lightbox visual is missing");
    Object.defineProperties(stage, {
      setPointerCapture: { configurable: true, value: () => {} },
      releasePointerCapture: { configurable: true, value: () => {} },
      hasPointerCapture: { configurable: true, value: () => false },
    });
    const bounds = visual.getBoundingClientRect();
    const x = (bounds.left + bounds.right) / 2;
    const y = (bounds.top + bounds.bottom) / 2;
    stage.dispatchEvent(new PointerEvent("pointerdown", {
      bubbles: true,
      cancelable: true,
      pointerId: 73,
      pointerType: "touch",
      clientX: x,
      clientY: y,
      buttons: 1,
    }));
    stage.dispatchEvent(new PointerEvent("pointerup", {
      bubbles: true,
      cancelable: true,
      pointerId: 73,
      pointerType: "touch",
      clientX: x,
      clientY: y,
    }));
    stage.dispatchEvent(new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      clientX: x,
      clientY: y,
      detail: 1,
    }));
  });
  await expect(page.locator(".image-lightbox")).toHaveCount(0);
  await expect(preview).toBeVisible();

  await page.getByRole("button", { name: "移除待发送图片 1" }).click();
  await expect(preview).toHaveCount(0);
  await expect(page.locator(".image-lightbox")).toHaveCount(0);
});

test("a page that finishes under an active touch restores its retained boundary", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "webkit", "iOS WebKit touch settlement");
  await page.goto("/tests/history-browser.html?delay=5&manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const before = await readingAnchor(page);

  await dispatchTouchPhase(page, "touchstart", 160);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  // WebKit may deliver the scroll that reached the paging edge only after the
  // prepended rows have committed. Reproduce that stale metrics sequence
  // deterministically: without a post-commit touchmove it must not replace o1.
  await viewport.evaluate((node) => {
    node.scrollTop = 400;
    node.dispatchEvent(new Event("scroll"));
    node.scrollTop = 0;
    node.dispatchEvent(new Event("scroll"));
  });
  await dispatchTouchPhase(page, "touchend", 220);

  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
  await page.waitForTimeout(300);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(before.id);
  expect(Math.abs(settled.offset - before.offset)).toBeLessThan(2);
});

test("a delayed pre-commit touchmove never rebases a retained page", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "webkit", "iOS WebKit touch settlement");
  await page.goto("/tests/history-browser.html?delay=5&manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const before = await readingAnchor(page);

  // Create the reverse move before the page request. WebKit may queue that
  // native event and deliver it only after React commits the prepend.
  await stageDelayedTouchMove(page, 160, 80);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await dispatchDelayedTouchMove(page);
  await viewport.evaluate((node) => {
    node.scrollTop = 400;
    node.dispatchEvent(new Event("scroll"));
    node.scrollTop = 0;
    node.dispatchEvent(new Event("scroll"));
  });
  await dispatchTouchPhase(page, "touchend", 80);

  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
});

test("a cached-newer page that finishes under touch keeps its retained row", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "webkit", "iOS WebKit touch settlement");
  await page.goto("/tests/history-browser.html?deep-browse=1&delay=5");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await dispatchTouchPhase(page, "touchstart", 220);
  await dispatchTouchPhase(page, "touchmove", 160);
  await expect(page.getByTestId("newer-load-count")).toHaveText("1");
  await expect(page.getByTestId("newest-turn-id")).toHaveText("m28");
  await dispatchTouchPhase(page, "touchend", 160);

  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
});

test("movement after an attached page rebases the held touch boundary", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "webkit", "iOS WebKit touch settlement");
  await page.goto("/tests/history-browser.html?delay=5&manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const original = await readingAnchor(page);

  await dispatchTouchPhase(page, "touchstart", 160);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();

  // The response is already installed, but the same finger deliberately
  // reverses toward newer content before it is lifted.
  await dispatchTouchPhase(page, "touchmove", 80);
  await viewport.evaluate((node) => { node.scrollBy({ top: 720 }); });
  await expect.poll(async () => (await readingAnchor(page)).id)
    .not.toBe(original.id);
  // WebKit dispatches scroll before the virtualizer has necessarily committed
  // the newly visible row measurements. Freeze the user's actual settled
  // reading position, not that intermediate layout frame.
  await waitForScrollIdle(page);
  const moved = await readingAnchor(page);
  await dispatchTouchPhase(page, "touchend", 80);

  await expect.poll(async () => (await readingAnchor(page)).id).toBe(moved.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - moved.offset),
  ).toBeLessThan(2);
  await page.waitForTimeout(300);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(moved.id);
  expect(Math.abs(settled.offset - moved.offset)).toBeLessThan(2);
});

test("repeated prepends preserve each page boundary instead of jumping to the inserted page", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?pages=4&delay=5&manual-growth=1");
  const viewport = page.locator(".thread");

  for (let pageNumber = 1; pageNumber <= 4; pageNumber += 1) {
    if (pageNumber === 1) {
      await viewport.evaluate((node) => { node.scrollTop = 0; });
    }
    await waitForScrollIdle(page);
    const before = await readingAnchor(page);
    const beforeScrollHeight = await viewport.evaluate((node) => node.scrollHeight);
    if (pageNumber === 1) {
      await requestOlderHistory(page, testInfo.project.name);
    } else {
      await page.locator(".load-more-btn").dispatchEvent("click");
    }
    await expect(page.getByTestId("load-count")).toHaveText(String(pageNumber));
    const insertedOldestId = pageNumber === 1 ? "n1" : `p${pageNumber}-1`;
    await expect.poll(async () =>
      viewport.evaluate((node) => node.scrollHeight),
    ).toBeGreaterThan(beforeScrollHeight);

    await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
    await expect.poll(async () =>
      Math.abs((await readingAnchor(page)).offset - before.offset),
    ).toBeLessThan(2);
    expect((await readingAnchor(page)).id).not.toBe(insertedOldestId);
    // End the wheel/touch gesture before pulling the next page. The product
    // intentionally allows only one request per physical gesture.
    await page.waitForTimeout(250);
  }
});

test("the first runtime-to-browse page preserves its captured reading row", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?runtime-browse=1&delay=5&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await page.locator(".load-more-btn").dispatchEvent("click");
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
});

test("one upward gesture starts at most one older-page request", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await requestOlderHistory(page, testInfo.project.name, 2);
  await expect(page.getByTestId("load-count")).toHaveText("1");
});

test("cached-newer append with head eviction preserves the reading row", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?deep-browse=1&delay=5&manual-growth=1");
  const viewport = page.locator(".thread");
  await expect(page.locator('[data-turn-id="m20"]')).toBeVisible();
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await requestNewerHistory(page, testInfo.project.name);
  await expect(page.getByTestId("newer-load-count")).toHaveText("1");
  await expect(page.getByTestId("newest-turn-id")).toHaveText("m28");
  await expect(page.locator('[data-turn-id="m1"]')).toHaveCount(0);
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);

  // A late image/Markdown measurement before the retained row must reuse the
  // same transaction instead of introducing a second scroll writer.
  await page.getByTestId("grow-row").click();
  await expect(page.locator('[data-turn-id="m15"] p')).toHaveCount(28);
  await waitForScrollIdle(page);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(before.id);
  expect(Math.abs(settled.offset - before.offset)).toBeLessThan(2);
});

test("one downward gesture starts at most one cached-newer page", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?deep-browse=1&delay=80");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await requestNewerHistory(page, testInfo.project.name, 2);
  await expect(page.getByTestId("newer-load-count")).toHaveText("1");
});

test("repeated cached-newer pages keep the protected row through the final page", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?deep-browse=1&delay=5");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await waitForScrollIdle(page);
  const expectedNewest = ["m28", "m36", "m40"];

  for (let index = 0; index < expectedNewest.length; index += 1) {
    const before = await readingAnchor(page);
    await page.getByRole("button", { name: "加载更新的历史" })
      .dispatchEvent("click");
    await expect(page.getByTestId("newer-load-count"))
      .toHaveText(String(index + 1));
    await expect(page.getByTestId("newest-turn-id"))
      .toHaveText(expectedNewest[index]);
    await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
    await expect.poll(async () =>
      Math.abs((await readingAnchor(page)).offset - before.offset),
    ).toBeLessThan(2);
    await page.waitForTimeout(80);
  }
  await expect(page.getByRole("button", {
    name: "加载更新的历史",
  })).toHaveCount(0);
});

test("browse live updates stay passive until the user returns to latest", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?deep-browse=1");
  await expect(page.locator('[data-turn-id="m20"]')).toBeVisible();
  await wheelUntilTurn(page, "m12", -1_200, testInfo.project.name);
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await page.getByTestId("append-turn").click();
  await page.waitForTimeout(100);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
  await expect(page.locator('[data-turn-id="live-41"]')).toHaveCount(0);

  await page.getByRole("button", { name: "回到最新" }).click();
  await expect(page.locator('[data-turn-id="live-41"]')).toBeVisible();
  await expect(page.getByRole("button", { name: "回到最新" })).toHaveCount(0);
});

test("a delayed cached-newer page cannot move another session", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?deep-browse=1&delay=350");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await requestNewerHistory(page, testInfo.project.name);
  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);
  await page.waitForTimeout(500);
  await waitForScrollIdle(page);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
  await expect(page.locator('[data-turn-id="m28"]')).toHaveCount(0);
});

test("an empty final page removes the loader without moving the reading row", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?empty-final=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const before = await readingAnchor(page);
  await requestOlderHistory(page, testInfo.project.name);
  await expect(page.getByRole("button", {
    name: "加载更早的历史",
  })).toHaveCount(0);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("user movement after prepend stays stable through delayed growth", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const initial = await readingAnchor(page);
  await requestOlderHistory(page, testInfo.project.name);
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(initial.id);
  await wheelUntilTurn(page, "o2", 300, testInfo.project.name);
  await waitForScrollIdle(page);
  await page.waitForTimeout(300);
  const before = await readingAnchor(page);
  await page.getByTestId("grow-row").click();
  await expect(page.locator('[data-turn-id="n8"] p')).toHaveCount(28);
  await waitForScrollIdle(page);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("a delayed page from the previous session cannot move the new session", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?delay=350");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await requestOlderHistory(page, testInfo.project.name);
  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();
  await page.waitForTimeout(500);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();

  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="o4"]')).toBeVisible();
});

test("same-session revision replacement resets to the latest row", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?large=40");
  await expect(page.locator('[data-turn-id="m40"]')).toBeVisible();
  await wheelUntilTurn(page, "m1", -2_000, testInfo.project.name);
  await expect(page.locator('[data-turn-id="m1"]')).toBeVisible();

  await page.getByTestId("replace-revision").click();
  await expect(page.locator('[data-turn-id="m1"]')).toHaveCount(0);
  await expect(page.locator('[data-turn-id="r24"]')).toBeVisible();
  await expect(page.locator('[data-turn-id="r1"]')).toHaveCount(0);
});

test("replay recovery replacement preserves the current reading row", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?large=40&recovery-replace=1");
  await expect(page.locator('[data-turn-id="m40"]')).toBeVisible();
  await wheelUntilTurn(page, "m10", -400, testInfo.project.name);
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await page.getByTestId("replace-revision").click();
  await expect(page.locator('[data-turn-id="m10"] p')).toHaveCount(4);
  await waitForScrollIdle(page);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("reversing direction while a page is pending preserves the reading row", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?delay=700");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await requestOlderHistory(page, testInfo.project.name);
  await wheelUntilTurn(page, "o4", 2_000, testInfo.project.name);
  const before = await readingAnchor(page);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(1);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("virtualization bounds mounted rows and preserves an expanded timeline", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?timeline=1");
  await scrollThreadToEdge(page, "start", testInfo.project.name);
  const timeline = page.locator('[data-turn-id="timeline"]');
  await expect(timeline).toBeVisible();
  await timeline.locator(".turn-process-head").click();
  await expect(timeline.locator(".turn-process-head")).toHaveAttribute("aria-expanded", "true");

  await scrollThreadToEdge(page, "end", testInfo.project.name);
  await expect(timeline).toHaveCount(0);
  expect(await page.locator(".turn").count()).toBeLessThan(40);

  await scrollThreadToEdge(page, "start", testInfo.project.name);
  await expect(timeline).toBeVisible();
  await expect(timeline.locator(".turn-process-head")).toHaveAttribute("aria-expanded", "true");
});

test("desktop text selection keeps its original virtual turn while edge-dragging", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "webkit",
    "the configured WebKit project is a touch phone; this is a desktop mouse path");
  await page.goto("/tests/history-browser.html?large=120");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => {
    node.scrollTop = node.scrollHeight * (41 / 120);
  });
  const startText = page.locator('[data-turn-id="m42"] p').first();
  await startText.scrollIntoViewIfNeeded();
  await expect(startText).toBeInViewport();
  const viewportBox = await viewport.boundingBox();
  const startPoint = await textSelectionPoint(startText);
  if (!viewportBox) {
    throw new Error("selection fixture has no geometry");
  }
  const startScrollTop = await viewport.evaluate((node) => node.scrollTop);

  await page.mouse.move(startPoint.x, startPoint.y);
  await page.mouse.down();
  await page.mouse.move(
    viewportBox.x + viewportBox.width - 48,
    viewportBox.y + viewportBox.height - 2,
    { steps: 20 },
  );
  for (let step = 0; step < 24; step += 1) {
    await page.mouse.wheel(0, 220);
    await page.mouse.move(
      viewportBox.x + viewportBox.width - 48 + (step % 2),
      viewportBox.y + viewportBox.height - 2,
    );
    await page.waitForTimeout(45);
  }

  const draggedScrollTop = await viewport.evaluate((node) => node.scrollTop);
  const draggingSelection = await nativeSelectionSnapshot(page);
  expect(draggedScrollTop - startScrollTop).toBeGreaterThan(800);
  expect(draggingSelection.anchorTurnId).toBe("m42");
  expect(draggingSelection.anchorConnected).toBe(true);
  expect(draggingSelection.text).toContain("m42");
  await expect(page.locator('[data-turn-id="m42"]')).toBeAttached();
  const draggedAnchor = await readingAnchor(page);
  await page.mouse.up();
  await expect(viewport).toHaveAttribute(
    "data-text-selection-dragging", "false",
  );
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "true",
  );
  await page.evaluate(() => new Promise<void>((resolve) => {
    window.requestAnimationFrame(() => resolve());
  }));
  const immediateReleasedScrollTop =
    await viewport.evaluate((node) => node.scrollTop);
  expect(immediateReleasedScrollTop - startScrollTop).toBeGreaterThan(800);
  const immediateReleasedAnchor = await readingAnchor(page);
  // Chromium may finish one native selection auto-scroll step on mouseup.
  // Retain that released visual position; the app must not replay an older
  // scroll command after the browser has handed control back.
  expect(immediateReleasedAnchor.id).toBe(draggedAnchor.id);
  await page.waitForTimeout(120);
  const releasedAnchor = await readingAnchor(page);
  expect(releasedAnchor.id).toBe(immediateReleasedAnchor.id);
  expect(Math.abs(
    releasedAnchor.offset - immediateReleasedAnchor.offset,
  )).toBeLessThan(2);
  await page.locator('[data-turn-id="m42"]').evaluate((node) => {
    (node as HTMLElement).style.paddingBottom = "320px";
  });
  await page.waitForTimeout(120);
  const measuredAnchor = await readingAnchor(page);
  expect(measuredAnchor.id).toBe(releasedAnchor.id);
  expect(Math.abs(measuredAnchor.offset - releasedAnchor.offset)).toBeLessThan(2);
  const measuredScrollTop = await viewport.evaluate((node) => node.scrollTop);
  await page.getByTestId("append-turn").evaluate(
    (button: HTMLButtonElement) => button.click(),
  );
  await page.waitForTimeout(100);
  expect(Math.abs(
    await viewport.evaluate((node) => node.scrollTop) - measuredScrollTop,
  )).toBeLessThan(2);
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "true",
  );

  await page.evaluate(() => {
    document.dispatchEvent(new ClipboardEvent("copy", {
      bubbles: true,
      cancelable: true,
    }));
  });
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "false",
  );
  expect(await page.locator(".turn").count()).toBeLessThan(40);
});

test("desktop wheel scrolling remains available after text selection", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "webkit",
    "the configured WebKit project is a touch phone; this is a desktop mouse path");
  await page.goto("/tests/history-browser.html?large=120");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => {
    node.scrollTop = node.scrollHeight * (41 / 120);
  });
  const text = page.locator('[data-turn-id="m42"] p').first();
  await text.scrollIntoViewIfNeeded();
  const box = await text.boundingBox();
  if (!box) throw new Error("selection fixture has no geometry");

  await page.mouse.move(box.x + 4, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(
    Math.min(box.x + box.width - 4, box.x + 180),
    box.y + box.height / 2,
    { steps: 12 },
  );
  await page.mouse.up();
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "true",
  );
  expect((await nativeSelectionSnapshot(page)).text.length).toBeGreaterThan(0);
  const beforeScrollTop = await viewport.evaluate((node) => node.scrollTop);

  await page.mouse.wheel(0, 640);
  await expect.poll(
    () => viewport.evaluate((node) => node.scrollTop),
  ).toBeGreaterThan(beforeScrollTop + 200);
  const afterScroll = await nativeSelectionSnapshot(page);
  expect(afterScroll.anchorConnected).toBe(true);
  expect(afterScroll.text.length).toBeGreaterThan(0);
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "true",
  );
});

test("a late cached-newer page cannot evict an active text selection", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "webkit",
    "the configured WebKit project is a touch phone; this is a desktop mouse path");
  await page.goto(
    "/tests/history-browser.html?deep-browse=1&delay=3000",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await page.getByRole("button", { name: "加载更新的历史" })
    .dispatchEvent("click");
  await expect(page.getByTestId("newer-load-count")).toHaveText("1");

  await wheelUntilTurn(page, "m5", -400, testInfo.project.name);
  const startText = page.locator('[data-turn-id="m5"] p').first();
  await expect(startText).toBeInViewport();
  const start = await textSelectionPoint(startText);
  const textBox = await startText.boundingBox();
  if (!textBox) throw new Error("selection page fixture has no geometry");
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await page.mouse.move(
    Math.min(textBox.x + textBox.width - 2, start.x + 140),
    start.y,
    { steps: 8 },
  );
  await expect(viewport).toHaveAttribute(
    "data-text-selection-dragging", "true",
  );

  await expect(page.getByTestId("newest-turn-id")).toHaveText("m28");
  await expect(page.locator('[data-turn-id="m5"]')).toBeAttached();
  expect((await nativeSelectionSnapshot(page)).anchorTurnId).toBe("m5");
  await page.mouse.up();
  await page.evaluate(() => {
    document.dispatchEvent(new ClipboardEvent("copy", { bubbles: true }));
  });
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "false",
  );
});

test("switching sessions clears retained desktop text selection", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "webkit",
    "the configured WebKit project is a touch phone; this is a desktop mouse path");
  await page.goto("/tests/history-browser.html?large=80");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => {
    node.scrollTop = node.scrollHeight * 0.25;
  });
  const startText = page.locator('[data-turn-id="m20"] p').first();
  await startText.scrollIntoViewIfNeeded();
  const start = await textSelectionPoint(startText);
  const textBox = await startText.boundingBox();
  if (!textBox) throw new Error("selection switch fixture has no geometry");
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await page.mouse.move(
    Math.min(textBox.x + textBox.width - 2, start.x + 120),
    start.y,
    { steps: 8 },
  );
  await page.mouse.up();
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "true",
  );

  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "false",
  );
  expect((await nativeSelectionSnapshot(page)).text).toBe("");
  expect(await page.locator(".turn").count()).toBeLessThan(40);
});

test("nested process disclosures survive virtual row unmounts", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?timeline=1&engine=claude");
  await wheelUntilTurn(page, "timeline", -4_000, testInfo.project.name);
  await waitForScrollIdle(page);
  const timeline = page.locator('[data-turn-id="timeline"]');
  await expect(timeline).toBeVisible();
  await timeline.locator(".turn-process-head").click();
  const activity = timeline.locator("details.process-activity");
  const reasoning = timeline.locator("details.process-reasoning");
  await activity.locator(":scope > summary").click();
  await reasoning.locator(":scope > summary").click();
  await expect(activity).toHaveAttribute("open", "");
  await expect(reasoning).toHaveAttribute("open", "");

  await wheelUntilTurn(page, "f80", 4_000, testInfo.project.name);
  await waitForScrollIdle(page);
  await expect(timeline).toHaveCount(0);
  await wheelUntilTurn(page, "timeline", -4_000, testInfo.project.name);
  await waitForScrollIdle(page);
  await expect(timeline).toBeVisible();
  await expect(timeline.locator("details.process-activity"))
    .toHaveAttribute("open", "");
  await expect(timeline.locator("details.process-reasoning"))
    .toHaveAttribute("open", "");
});

test("one stationary press opens a process timeline while a newer turn grows", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?interactive-timeline=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });

  const header = page.locator(
    '[data-turn-id="timeline"] .turn-process-head',
  );
  await expect(header).toBeVisible();
  await expect(header).toHaveAttribute("aria-expanded", "false");
  const box = await header.boundingBox();
  if (!box) throw new Error("process header has no bounds");
  const point = {
    x: box.x + box.width / 2,
    y: box.y + box.height / 2,
  };

  await page.mouse.move(point.x, point.y);
  await page.mouse.down();
  for (let index = 0; index < 4; index += 1) {
    await page.getByTestId("grow-stream").evaluate(
      (button: HTMLButtonElement) => button.click(),
    );
    await page.waitForTimeout(35);
  }
  await page.mouse.up();

  await expect(header).toHaveAttribute("aria-expanded", "true");
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "false",
  );
});

test("one stationary press opens nested thinking while a newer turn grows", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?interactive-timeline=1&engine=claude",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  const timeline = page.locator('[data-turn-id="timeline"]');
  await timeline.locator(".turn-process-head").click();
  await waitForScrollIdle(page);
  const summary = timeline.locator(".process-reasoning > summary");
  const box = await summary.boundingBox();
  if (!box) throw new Error("nested reasoning summary has no bounds");

  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  for (let index = 0; index < 4; index += 1) {
    await page.getByTestId("grow-stream").evaluate(
      (button: HTMLButtonElement) => button.click(),
    );
    await page.waitForTimeout(35);
  }
  await page.mouse.up();

  await expect(timeline.locator("details.process-reasoning"))
    .toHaveAttribute("open", "");
});

test("live append follows at the bottom but not while reading history", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?large=40");
  await expect(page.locator('[data-turn-id="m40"]')).toBeVisible();
  await page.getByTestId("append-turn").click();
  await expect(page.locator('[data-turn-id="live-41"]')).toBeVisible();

  // Use the browser's native scroll pipeline here so the virtualizer and
  // React receive the same wheel/scroll ordering as a real user gesture.
  await wheelUntilTurn(page, "m1", -2_000, testInfo.project.name);
  await expect(page.locator(".scroll-bottom-btn")).toBeVisible();
  await page.waitForTimeout(250);
  await expect(page.locator('[data-turn-id="live-41"]')).toHaveCount(0);
  const before = await readingAnchor(page);
  await page.getByTestId("append-turn").click();
  await page.waitForTimeout(100);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
  await expect(page.locator('[data-turn-id="live-42"]')).toHaveCount(0);
  expect(await page.locator(".turn").count()).toBeLessThan(40);
});

test("composer action growth keeps the live tail visible without stealing history", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?large=40&composer-resize=1");
  const viewport = page.locator(".thread");
  await expect(page.locator('[data-turn-id="m40"]')).toBeVisible();
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await expect.poll(async () => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);

  await page.getByTestId("toggle-composer").click();
  await expect.poll(async () => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);
  const spark = page.locator('[data-turn-id="m40"] .turn-done-mark');
  await expect(spark).toBeVisible();
  expect(await spark.evaluate((node) => {
    const viewportNode = document.querySelector<HTMLElement>(".thread");
    if (!viewportNode) throw new Error("thread viewport is missing");
    return node.getBoundingClientRect().bottom
      <= viewportNode.getBoundingClientRect().bottom + 1;
  })).toBe(true);

  await wheelUntilTurn(page, "m1", -2_000, testInfo.project.name);
  const before = await readingAnchor(page);
  await page.getByTestId("toggle-composer").click();
  await page.waitForTimeout(200);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

for (const width of [320, 390]) {
  test(`Codex controls stay on one row in a ${width} px composer`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 720 });
    await page.goto("/tests/history-browser.html?quota-composer=1");

    const composer = page.getByTestId("quota-composer");
    const input = composer.getByRole("textbox", { name: "message" });
    await expect(input).toHaveAttribute(
      "placeholder", "输入 / 命令，$ Skill");
    await expect(composer.locator(".fast-chip")).toHaveText("快速");
    await expect(composer.locator(".fast-chip")).not.toContainText("⚡");
    await expect(composer.locator(".usage-meter")).toBeVisible();
    await expect(composer.locator(".hint-ring")).toBeVisible();
    const inputHeight = await input.evaluate((node) => ({
      client: node.clientHeight,
      scroll: node.scrollHeight,
    }));
    expect(inputHeight.scroll).toBeLessThanOrEqual(inputHeight.client + 1);

    const layout = await composer.evaluate((node) => {
      const footer = node.getBoundingClientRect();
      const controls = [
        node.querySelector<HTMLElement>(".hint-mode"),
        ...node.querySelectorAll<HTMLElement>(".hint-right > .hint-ctl"),
        node.querySelector<HTMLElement>(".usage-meter"),
        node.querySelector<HTMLElement>(".hint-ring"),
      ].filter((control): control is HTMLElement => control !== null)
        .map((control) => {
          const rect = control.getBoundingClientRect();
          return {
            left: rect.left,
            right: rect.right,
            width: rect.width,
            centerY: rect.top + rect.height / 2,
          };
        });
      return {
        clientWidth: node.clientWidth,
        scrollWidth: node.scrollWidth,
        footerLeft: footer.left,
        footerRight: footer.right,
        controls,
      };
    });
    expect(layout.controls).toHaveLength(6);
    expect(layout.scrollWidth).toBeLessThanOrEqual(layout.clientWidth);
    expect(Math.max(...layout.controls.map((control) => control.centerY))
      - Math.min(...layout.controls.map((control) => control.centerY)))
      .toBeLessThanOrEqual(2);
    const minimumWidths = [48, 48, 28, 20, 44, 28];
    for (const [index, control] of layout.controls.entries()) {
      expect(control.width).toBeGreaterThanOrEqual(minimumWidths[index]);
    }
    for (const [index, control] of layout.controls.entries()) {
      expect(control.left).toBeGreaterThanOrEqual(layout.footerLeft);
      expect(control.right).toBeLessThanOrEqual(layout.footerRight);
      if (index > 0) {
        expect(control.left - layout.controls[index - 1].right)
          .toBeGreaterThanOrEqual(10);
      }
    }
  });
}

test("queued messages expand to full editable prompts", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 720 });
  await page.goto("/tests/history-browser.html?queued-query-editor=1");

  const fixture = page.getByTestId("queued-query-fixture");
  const preview = fixture.locator(".qt");
  await expect(preview).toBeVisible();
  await expect(fixture).not.toContainText("QUEUED-INSTRUCTION-END");
  expect(await preview.evaluate((node) =>
    node.scrollWidth > node.clientWidth)).toBe(true);

  await fixture.getByRole("button", { name: "查看排队消息" }).click();
  const dialog = page.getByRole("dialog", { name: "排队消息详情" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByTestId("queued-full-prompt"))
    .toContainText("QUEUED-INSTRUCTION-END");
  await expect(dialog).toContainText("编辑文字不会移除附件");

  await dialog.getByRole("button", { name: "编辑", exact: true }).click();
  const editor = dialog.getByRole("textbox", { name: "编辑排队消息" });
  await editor.fill("Updated queued instruction\nwith the complete context.");
  await dialog.getByRole("button", { name: "保存修改" }).click();
  await expect(dialog.getByTestId("queued-full-prompt"))
    .toHaveText("Updated queued instruction\nwith the complete context.");

  await dialog.getByRole(
    "button", { name: "关闭排队消息详情" },
  ).click();
  await expect(dialog).not.toBeVisible();
  await expect(preview).toContainText("Updated queued instruction");
  await fixture.getByRole("button", { name: "查看排队消息" }).click();
  await expect(page.getByTestId("queued-full-prompt"))
    .toContainText("with the complete context.");
  expect(await page.evaluate(() => (
    document.documentElement.scrollWidth <= document.documentElement.clientWidth
  ))).toBe(true);
});

test("migration picker cannot confirm a stale directory", async ({ page }) => {
  await page.goto("/tests/history-browser.html?migration-picker=1");
  await page.getByTestId("open-migration-picker").click();

  const dialog = page.getByRole("dialog", { name: "迁移 Codex 会话" });
  const confirm = dialog.getByRole(
    "button", { name: "迁移到此目录" },
  );
  await expect(dialog.locator(".dp-crumbs")).toHaveText("/repo/current");
  await expect(dialog).toContainText("正在读取目录");
  await expect(dialog).not.toContainText("stale-child");
  await expect(confirm).toBeDisabled();
  await expect(page.getByTestId("migration-picker-request"))
    .toHaveText("/repo/current");
  await expect(page.getByTestId("migration-picker-confirmed")).toBeEmpty();

  await page.getByTestId("resolve-migration-picker")
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect(dialog).not.toContainText("正在读取目录");
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(page.getByTestId("migration-picker-confirmed"))
    .toHaveText("/repo/current");
});

test("migration picker waits for its null-path response", async ({ page }) => {
  await page.goto("/tests/history-browser.html?migration-picker-null=1");
  await page.getByTestId("open-migration-picker").click();

  const dialog = page.getByRole("dialog", { name: "迁移 Codex 会话" });
  const confirm = dialog.getByRole(
    "button", { name: "迁移到此目录" },
  );
  await expect(dialog.locator(".dp-crumbs")).toHaveText("…");
  await expect(dialog).toContainText("正在读取目录");
  await expect(dialog).not.toContainText("stale-child");
  await expect(confirm).toBeDisabled();
  await expect(page.getByTestId("migration-picker-request"))
    .toHaveText("<home>");

  await page.getByTestId("resolve-migration-picker")
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect(dialog.locator(".dp-crumbs")).toHaveText("/home/fixture");
  await expect(dialog).not.toContainText("正在读取目录");
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(page.getByTestId("migration-picker-confirmed"))
    .toHaveText("/home/fixture");
});

test("migration picker follows an external session move", async ({ page }) => {
  await page.goto("/tests/history-browser.html?migration-picker=1");
  await page.getByTestId("open-migration-picker").click();

  const dialog = page.getByRole("dialog", { name: "迁移 Codex 会话" });
  const confirm = dialog.getByRole(
    "button", { name: "迁移到此目录" },
  );
  await page.getByTestId("resolve-migration-picker")
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect(confirm).toBeEnabled();

  await page.getByTestId("externally-migrate-picker")
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect(dialog.locator(".dp-crumbs")).toHaveText("/repo/external");
  await expect(dialog).toContainText("正在读取目录");
  await expect(confirm).toBeDisabled();
  await expect(page.getByTestId("migration-picker-request"))
    .toHaveText("/repo/external");
  await expect(page.getByTestId("migration-picker-confirmed")).toBeEmpty();

  await page.getByTestId("resolve-migration-picker")
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect(dialog).not.toContainText("正在读取目录");
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(page.getByTestId("migration-picker-confirmed"))
    .toHaveText("/repo/external");
});

async function chooseDangerousNewChatControls(
  page: import("@playwright/test").Page,
): Promise<void> {
  await page.locator(".newchat-access").click();
  const dialog = page.getByRole("dialog", {
    name: "权限与执行环境",
  });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: /On Request/ }).click();
  await dialog.getByRole("button", { name: /Full Access/ }).click();
  await dialog.getByRole("button", { name: "Live", exact: true }).click();
  await page.locator(".scrim.show").click({ position: { x: 8, y: 8 } });
  await expect(dialog).not.toHaveClass(/(?:^|\s)show(?:\s|$)/);
  await expect(page.locator(".newchat-access")).toContainText("Full Access");
}

let newChatSubmissionSequence = 0;

async function submitNewChatFixture(
  page: import("@playwright/test").Page,
): Promise<Record<string, unknown>> {
  const prompt = `verify scoped controls ${++newChatSubmissionSequence}`;
  await page.locator(".newchat-input").fill(prompt);
  await page.getByRole("button", { name: "开始", exact: true }).click();
  await expect(page.getByTestId("newchat-submission")).toContainText(prompt);
  return JSON.parse(
    await page.getByTestId("newchat-submission").innerText(),
  ) as Record<string, unknown>;
}

test("new-chat controls reset across device authorization scopes", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?newchat-controls=1");
  await chooseDangerousNewChatControls(page);

  await page.getByTestId("switch-newchat-device").click();
  await expect(page.getByTestId("newchat-scope"))
    .toHaveText("machine-b:code:codex");
  await expect(page.locator(".newchat-access")).toContainText("默认环境");

  const submitted = await submitNewChatFixture(page);
  expect(submitted.permissionMode).toBe("never");
  expect(submitted).not.toHaveProperty("permissionProfile");
  expect(submitted).not.toHaveProperty("webSearch");
});

test("new-chat controls reset across engine authorization scopes", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?newchat-controls=1");
  await chooseDangerousNewChatControls(page);

  await page.getByTestId("switch-newchat-engine").click();
  await expect(page.getByTestId("newchat-scope"))
    .toHaveText("machine-a:code:claude");
  await expect(page.locator(".newchat-access")).toHaveCount(0);
  const claudeSubmission = await submitNewChatFixture(page);
  expect(claudeSubmission).not.toHaveProperty("permissionMode");

  await page.getByTestId("switch-newchat-engine").click();
  await expect(page.locator(".newchat-access")).toContainText("默认环境");
  const codexSubmission = await submitNewChatFixture(page);
  expect(codexSubmission.permissionMode).toBe("never");
  expect(codexSubmission).not.toHaveProperty("permissionProfile");
  expect(codexSubmission).not.toHaveProperty("webSearch");
});

test("new-chat controls reset and normalize across Code and Work", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?newchat-controls=1");
  await chooseDangerousNewChatControls(page);

  await page.getByTestId("switch-newchat-space").click();
  await expect(page.getByTestId("newchat-scope"))
    .toHaveText("machine-a:work:codex");
  await expect(page.locator(".newchat-access")).toHaveCount(0);
  const workSubmission = await submitNewChatFixture(page);
  expect(workSubmission.permissionMode).toBe("never");
  expect(workSubmission).not.toHaveProperty("permissionProfile");
  expect(workSubmission).not.toHaveProperty("webSearch");

  await page.getByTestId("switch-newchat-space").click();
  await expect(page.locator(".newchat-access")).toContainText("默认环境");
});

test("a 256-character profile id fits a 296 px new-chat row", async ({
  page,
}) => {
  await page.setViewportSize({ width: 296, height: 720 });
  await page.goto(
    "/tests/history-browser.html?newchat-controls=1&long-profile=1",
  );
  await page.locator(".newchat-access").click();
  const customProfile = page.getByRole("dialog", {
    name: "权限与执行环境",
  }).locator('button[title^="custom-profile-"]');
  const profileRow = await customProfile.evaluate((button) => {
    const sheet = button.closest<HTMLElement>(".sheet");
    const name = button.querySelector<HTMLElement>(".cmd-nm");
    if (!sheet || !name) {
      throw new Error("permission-profile sheet row is incomplete");
    }
    const rowRect = button.getBoundingClientRect();
    const sheetRect = sheet.getBoundingClientRect();
    const nameStyle = getComputedStyle(name);
    return {
      viewportWidth: window.innerWidth,
      pageScrollWidth: document.documentElement.scrollWidth,
      sheetClientWidth: sheet.clientWidth,
      sheetScrollWidth: sheet.scrollWidth,
      rowLeft: rowRect.left,
      rowRight: rowRect.right,
      sheetLeft: sheetRect.left,
      sheetRight: sheetRect.right,
      nameClientWidth: name.clientWidth,
      nameScrollWidth: name.scrollWidth,
      nameOverflow: nameStyle.overflow,
      nameTextOverflow: nameStyle.textOverflow,
      titleLength: Array.from(button.getAttribute("title") ?? "").length,
    };
  });
  expect(profileRow.pageScrollWidth).toBeLessThanOrEqual(
    profileRow.viewportWidth);
  expect(profileRow.sheetScrollWidth).toBeLessThanOrEqual(
    profileRow.sheetClientWidth);
  expect(profileRow.rowLeft).toBeGreaterThanOrEqual(profileRow.sheetLeft);
  expect(profileRow.rowRight).toBeLessThanOrEqual(profileRow.sheetRight);
  expect(profileRow.nameScrollWidth).toBeGreaterThan(
    profileRow.nameClientWidth);
  expect(profileRow.nameOverflow).toBe("hidden");
  expect(profileRow.nameTextOverflow).toBe("ellipsis");
  expect(profileRow.titleLength).toBe(256);
  await customProfile.click();
  await page.locator(".scrim.show").click({ position: { x: 8, y: 8 } });

  const layout = await page.getByTestId("newchat-controls-fixture")
    .evaluate((node) => {
      const card = node.querySelector<HTMLElement>(".newchat-card");
      const access = node.querySelector<HTMLElement>(".newchat-access");
      if (!card || !access) throw new Error("new-chat controls are missing");
      const cardRect = card.getBoundingClientRect();
      const accessRect = access.getBoundingClientRect();
      return {
        fixtureClientWidth: node.clientWidth,
        fixtureScrollWidth: node.scrollWidth,
        cardClientWidth: card.clientWidth,
        cardScrollWidth: card.scrollWidth,
        accessLeft: accessRect.left,
        accessRight: accessRect.right,
        cardLeft: cardRect.left,
        cardRight: cardRect.right,
        label: access.textContent ?? "",
      };
    });
  expect(layout.fixtureScrollWidth).toBeLessThanOrEqual(
    layout.fixtureClientWidth);
  expect(layout.cardScrollWidth).toBeLessThanOrEqual(layout.cardClientWidth);
  expect(layout.accessLeft).toBeGreaterThanOrEqual(layout.cardLeft);
  expect(layout.accessRight).toBeLessThanOrEqual(layout.cardRight);
  expect(layout.label).toContain("…");
  expect(Array.from(layout.label).length).toBeLessThan(32);
});
