import { expect, test, type Locator } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { pngChunk, staticPng } from "./fixtures/png";
import { PROTOCOL_VERSION, type ServerEvent } from "../src/protocol";

test("Claude rewind keeps long prompts bounded and requires explicit capsule confirmation", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 375, height: 740 });
  await page.goto("/tests/history-browser.html?rewind=1");
  await page.getByRole("button", { name: "打开回退" }).click();
  const dialog = page.getByRole("dialog", { name: "回退", exact: true });
  await expect(dialog.getByRole("button", { name: "确认回退", exact: true })).toBeEnabled();
  await expect(dialog.locator('[role="radio"]')).toHaveCount(3);
  await expect(dialog.locator('[role="radio"] svg, [role="radio"] input')).toHaveCount(0);
  await expect(dialog.getByRole("radio", { name: /^恢复对话和文件/ })).toHaveAttribute("aria-checked", "true");
  const collapsed = await dialog.locator(".rewind-original").boundingBox();
  await dialog.getByRole("button", { name: "展开原文" }).click();
  expect((await dialog.locator(".rewind-original").boundingBox())!.height).toBeLessThanOrEqual(180);
  expect(collapsed!.height).toBeLessThan(100);
  await dialog.screenshot({ path: testInfo.outputPath("claude-rewind-mobile.png") });
  for (const name of ["取消", "确认回退"]) {
    const button = dialog.getByRole("button", { name, exact: true });
    expect(await button.evaluate(node => getComputedStyle(node).borderRadius)).toBe("999px");
    const box = (await button.boundingBox())!;
    expect(box.y + box.height).toBeLessThan(740);
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(375);
  }
  await dialog.getByRole("radio", { name: /^仅恢复对话/ }).click();
  await expect(page.getByTestId("sent")).toHaveText("0");
  await dialog.getByRole("button", { name: "确认回退", exact: true }).click();
  await expect(page.getByTestId("sent")).toHaveText("1");
  await expect(dialog.getByRole("button", { name: "正在回退…" })).toBeDisabled();
  // The mocked native receipt is delivered while the modal owns the screen.
  await page.getByRole("button", { name: "返回回退结果" }).evaluate(node => (node as HTMLButtonElement).click());
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "草稿" })).toHaveValue("原消息");
  await page.getByRole("button", { name: "写入新草稿" }).evaluate(node => (node as HTMLButtonElement).click());
  await page.getByRole("button", { name: "返回回退结果" }).evaluate(node => (node as HTMLButtonElement).click());
  await expect(page.getByRole("textbox", { name: "草稿" })).toHaveValue("保留我的新草稿");
});

test("Claude rewind disables missing file checkpoints and keeps a newly typed draft", async ({ page }) => {
  await page.goto("/tests/history-browser.html?rewind=1&no-files=1");
  await page.getByRole("button", { name: "打开回退" }).click();
  const dialog = page.getByRole("dialog", { name: "回退", exact: true });
  await expect(dialog.getByRole("radio", { name: /^仅恢复对话/ })).toBeEnabled();
  await expect(dialog.getByRole("radio", { name: /^仅恢复文件/ })).toBeDisabled();
  await expect(dialog.getByRole("radio", { name: /^恢复对话和文件/ })).toBeDisabled();
  await dialog.getByRole("button", { name: "确认回退", exact: true }).click();
  await page.getByRole("button", { name: "写入新草稿" }).evaluate(node => (node as HTMLButtonElement).click());
  await page.getByRole("button", { name: "返回回退结果" }).evaluate(node => (node as HTMLButtonElement).click());
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "草稿" })).toHaveValue("保留我的新草稿");
});

test("Claude rewind receipt stays with its original session after switching away", async ({ page }) => {
  await page.goto("/tests/history-browser.html?rewind=1");
  await page.getByRole("button", { name: "打开回退" }).click();
  const dialog = page.getByRole("dialog", { name: "回退", exact: true });
  await expect(dialog.getByRole("button", { name: "确认回退", exact: true })).toBeEnabled();
  await dialog.getByRole("button", { name: "确认回退", exact: true }).click();
  await page.getByRole("button", { name: "切换会话" }).evaluate(node => (node as HTMLButtonElement).click());
  await expect(dialog).toHaveCount(0);
  await page.getByRole("button", { name: "写入新草稿" }).click();
  await page.getByRole("button", { name: "返回回退结果" }).click();
  await expect(page.getByRole("textbox", { name: "草稿" })).toHaveValue("保留我的新草稿");
  await page.getByRole("button", { name: "切换会话" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "草稿" })).toHaveValue("原消息");
});
test("Claude rewind keeps partial failures visible and closes successful files-only restore", async ({ page }) => {
  for (const partial of [true, false]) {
    await page.goto(`/tests/history-browser.html?rewind=1${partial ? "&partial=1" : ""}`);
    await page.getByRole("button", { name: "打开回退" }).click();
    const dialog = page.getByRole("dialog", { name: "回退", exact: true });
    await expect(dialog.getByRole("button", { name: "确认回退", exact: true })).toBeEnabled();
    if (!partial) await dialog.getByRole("radio", { name: /^仅恢复文件/ }).click();
    await dialog.getByRole("button", { name: "确认回退", exact: true }).click();
    await page.getByRole("button", { name: "返回回退结果" }).evaluate(node => (node as HTMLButtonElement).click());
    if (partial) {
      await expect(dialog).toContainText("文件：未恢复");
      await expect(dialog.getByRole("button", { name: "关闭", exact: true })).toBeEnabled();
    } else {
      await expect(dialog).toHaveCount(0);
      await expect(page.getByRole("textbox", { name: "草稿" })).toHaveValue("");
    }
  }
});
import {
  GITHUB_README_ATTACHMENT_URL, GITHUB_README_IMAGE_URL,
  MARKDOWN_HTML_HEADER_SVG, MARKDOWN_HTML_LOCAL_README,
} from "./fixtures/markdown-html";

async function navigationReadingAnchor(page: import("@playwright/test").Page) {
  return page.locator(".thread").evaluate(node => {
    const top = node.getBoundingClientRect().top;
    const paragraph = [...node.querySelectorAll<HTMLElement>(".prose p")]
      .find(p => p.getBoundingClientRect().top >= top
        && p.getBoundingClientRect().top < top + node.clientHeight);
    if (!paragraph) throw new Error("No visible reading paragraph");
    return { text: paragraph.textContent!, offset: paragraph.getBoundingClientRect().top - top };
  });
}

async function navigateReadingToMiddle(page: import("@playwright/test").Page) {
  await page.locator(".thread").evaluate(node => {
    node.dispatchEvent(new WheelEvent("wheel", { bubbles: true, deltaY: -1000 }));
    node.scrollTop = 1100;
    node.dispatchEvent(new Event("scroll"));
  });
  await waitForScrollIdle(page);
  return navigationReadingAnchor(page);
}

for (const older of [false, true]) {
  test(`navigation reading restores ${older ? "paginated history" : "latest window"} after engine shortcuts and background output`, async ({ page }) => {
    await page.goto("/tests/history-browser.html?reading-navigation=1");
    await expect(page.getByTestId("surface")).toHaveText("device-a:code:codex");
    if (older) {
      await page.getByRole("button", { name: "Browse", exact: true }).click();
      await page.getByRole("button", { name: "Older page", exact: true }).click();
    }
    const before = await navigateReadingToMiddle(page);
    if (older) expect(before.text).toContain("older-");
    for (let round = 0; round < 2; round++) {
      await page.keyboard.press("Meta+1");
      await expect(page.getByTestId("surface")).toHaveText("device-a:code:claude");
      if (round === 0) await page.getByRole("button", { name: "Background output" }).click();
      await page.keyboard.press("Meta+2");
      await expect(page.getByTestId("surface")).toHaveText("device-a:code:codex");
      await expect.poll(() => navigationReadingAnchor(page)).toEqual(before);
      if (older) await expect(page.getByTestId("browse-id")).not.toHaveText(/^(latest|old-browse)$/);
      await page.waitForTimeout(300);
      expect(await navigationReadingAnchor(page)).toEqual(before);
    }
  });
}

test("navigation reading keeps bottom-following sessions at the new tail", async ({ page }) => {
  await page.goto("/tests/history-browser.html?reading-navigation=1");
  await expect.poll(() => page.locator(".thread").evaluate(node =>
    node.scrollHeight - node.clientHeight - node.scrollTop)).toBeLessThan(3);
  await page.keyboard.press("Meta+1");
  await page.getByRole("button", { name: "Background output" }).click();
  await page.keyboard.press("Meta+2");
  await expect.poll(() => page.locator(".thread").evaluate(node =>
    node.scrollHeight - node.clientHeight - node.scrollTop)).toBeLessThan(3);
  await expect(page.locator('.turn[data-turn-id="background-output"]')).toBeVisible();
});

test("navigation reading isolates identical session ids on different devices", async ({ page }) => {
  await page.goto("/tests/history-browser.html?reading-navigation=1");
  const before = await navigateReadingToMiddle(page);
  await page.getByRole("button", { name: "Switch device" }).click();
  await expect.poll(() => page.locator(".thread").evaluate(node =>
    node.scrollHeight - node.clientHeight - node.scrollTop)).toBeLessThan(3);
  await page.getByRole("button", { name: "Switch device" }).click();
  await expect.poll(() => navigationReadingAnchor(page)).toEqual(before);
});

test("turn regressions keyboard scrolling leaves the live tail and resumes at bottom", async ({ page, isMobile }) => {
  test.skip(isMobile, "Physical desktop keyboard scrolling");
  await page.goto("/tests/history-browser.html?turn-regressions=1");
  const viewport = page.getByRole("region", { name: "对话记录", exact: true });
  await expect(page.locator(".turn-working")).toBeVisible();
  await page.getByTestId("regression-live-toggle").click();
  await expect(viewport).toContainText("Live answer line 1");
  // Clicking ordinary prose must establish keyboard ownership without a
  // synthetic focus() or scrollTop write hiding the original regression.
  await page.getByText("continue work", { exact: true }).click();
  await expect(viewport).toBeFocused();
  // A non-text input-source key can switch the browser to keyboard focus
  // styling after a prose click. The reading surface must not gain a full
  // accent frame (only its bottom edge is visible in the app layout).
  await page.keyboard.press("CapsLock");
  await expect.poll(() => viewport.evaluate(node => node.matches(":focus-visible"))).toBe(true);
  await expect(viewport).toHaveCSS("outline-style", "none");
  for (let repeat = 0; repeat < 16; repeat++) {
    await page.keyboard.down("ArrowUp");
    await page.waitForTimeout(40);
  }
  await page.keyboard.up("ArrowUp");
  await expect(page.locator(".scroll-bottom-btn")).toBeVisible();
  await waitForScrollIdle(page);
  expect(await viewport.evaluate(node => node.scrollHeight - node.clientHeight - node.scrollTop)).toBeGreaterThan(200);
  const readingTop = await viewport.evaluate(node => node.scrollTop);
  await page.waitForTimeout(350); // new deltas must not pull the reader back
  expect(Math.abs(await viewport.evaluate(node => node.scrollTop) - readingTop)).toBeLessThan(3);
  await page.keyboard.press("End");
  await expect(page.locator(".scroll-bottom-btn")).toHaveCount(0);
  await expect.poll(() => viewport.evaluate(node => node.scrollHeight - node.clientHeight - node.scrollTop)).toBeLessThan(3);
  await page.waitForTimeout(350);
  expect(await viewport.evaluate(node => node.scrollHeight - node.clientHeight - node.scrollTop)).toBeLessThan(3);
  await page.keyboard.press("Shift+Space");
  await expect(page.locator(".scroll-bottom-btn")).toBeVisible();
  await expect.poll(() => viewport.evaluate(node => node.scrollHeight - node.clientHeight - node.scrollTop)).toBeGreaterThan(200);
  // Suppressing the reading-surface frame must leave real controls visible
  // to keyboard users.
  const control = page.getByTestId("regression-live-toggle");
  await control.focus();
  await expect(control).toHaveCSS("outline-style", "solid");
  await expect(control).toHaveCSS("outline-width", "2px");
});

for (const visible of ["thinking and answer", "only answer", "process header and answer", "long answer", "background answer", "touch-release answer"]) {
  test(`turn regressions completion preserves the reading paragraph with ${visible} visible`, async ({ page, isMobile }, testInfo) => {
    const options = visible === "process header and answer" ? "&short-process=1"
      : visible === "background answer" || visible === "touch-release answer" ? "&background=1&long-answer=1"
      : visible === "long answer" ? "&long-answer=1" : "";
    await page.goto(`/tests/history-browser.html?completion-reading=1${options}`);
    const viewport = page.locator(".thread");
    const paragraph = page.getByText("Answer paragraph 0. This is the explanation the user is reading.", { exact: true });
    await expect(page.locator(".process-reasoning-body")).toBeVisible();
    await waitForScrollIdle(page);
    const offset = () => paragraph.evaluate(node => node.getBoundingClientRect().top
      - node.closest(".thread")!.getBoundingClientRect().top);
    const targetOffset = visible === "only answer" ? 0 : 180;
    if (isMobile) {
      await dispatchTouchPhase(page, "touchstart", 160);
      await dispatchTouchPhase(page, "touchmove", 220);
      await viewport.evaluate((node, delta) => { node.scrollTop += delta; }, await offset() - targetOffset);
      await dispatchTouchPhase(page, "touchend", 220);
    } else {
      await viewport.hover();
      await page.mouse.wheel(0, await offset() - targetOffset);
    }
    await waitForScrollIdle(page);
    await expect(page.locator(".scroll-bottom-btn")).toBeVisible();
    const before = await offset();
    expect(Math.abs(before - targetOffset)).toBeLessThan(3);
    // Incoming events must not transfer focus or manufacture a user gesture.
    await page.getByTestId("append-answer").evaluate(node => (node as HTMLButtonElement).click());
    await expect.poll(offset).toBeCloseTo(before, 0);
    const readingNode = await paragraph.elementHandle();
    const frames = await paragraph.evaluate((node, duringTouchRelease) => new Promise<number[]>((resolve) => {
      const viewport = node.closest(".thread")!;
      const samples: number[] = [];
      const sample = () => {
        samples.push(node.getBoundingClientRect().top - viewport.getBoundingClientRect().top);
        if (samples.length === 45) resolve(samples);
        // Measure after each paint opportunity, including the collapse frame,
        // so a jump to bottom followed by a correction cannot pass this test.
        else requestAnimationFrame(() => setTimeout(sample, 0));
      };
      if (duringTouchRelease) {
        // Complete in the same task as touchend, before the idle lease can
        // expire. A slow runner must not accidentally omit this race.
        const touch = { identifier: 1, clientX: 160, clientY: 220, target: viewport };
        for (const type of ["touchstart", "touchend"]) {
          const event = new Event(type, { bubbles: true, cancelable: true });
          Object.defineProperties(event, {
            touches: { value: type === "touchend" ? [] : [touch] },
            changedTouches: { value: [touch] },
          });
          viewport.dispatchEvent(event);
        }
      }
      document.querySelector<HTMLButtonElement>('[data-testid="complete-answer"]')!.click();
      requestAnimationFrame(() => setTimeout(sample, 0));
    }), visible === "touch-release answer");
    await expect(page.locator(".process-reasoning-body")).toHaveCount(0);
    await waitForScrollIdle(page);
    const after = await offset();
    const distance = await viewport.evaluate(node => node.scrollHeight - node.clientHeight - node.scrollTop);
    await testInfo.attach("reading-position", { body: JSON.stringify({ before, after, distance, frames }), contentType: "application/json" });
    expect(Math.max(...frames.map(value => Math.abs(value - before)))).toBeLessThan(3);
    expect(Math.abs(after - before)).toBeLessThan(3);
    expect(await readingNode!.evaluate(node => node.isConnected)).toBe(true);
    await expect(page.locator('.assistant-answer-segment[data-message-id="native-answer"]')).toHaveCount(1);
    // Late summary metadata can rename a continuation's native process owner.
    // It must not remount the reply, reset its disclosure, or resume following.
    await page.getByTestId("refresh-answer").evaluate(node => (node as HTMLButtonElement).click());
    await waitForScrollIdle(page);
    expect(await readingNode!.evaluate(node => node.isConnected)).toBe(true);
    expect(Math.abs(await offset() - before)).toBeLessThan(3);
    await expect(page.locator(".process-reasoning-body")).toHaveCount(0);
    await expect(page.locator(".scroll-bottom-btn")).toBeVisible();
    // The retained paragraph is not a permanent scroll lock.
    if (!isMobile) {
      await viewport.hover();
      await page.mouse.wheel(0, 100);
      await waitForScrollIdle(page);
      expect(await offset()).toBeLessThan(after - 80);
    }
    await page.locator(".scroll-bottom-btn").click();
    await expect(page.locator(".scroll-bottom-btn")).toHaveCount(0);
    await expect.poll(() => viewport.evaluate(node => node.scrollHeight - node.clientHeight - node.scrollTop)).toBeLessThan(3);
  });
}

test("turn regressions completion keeps a followed answer at the bottom", async ({ page }) => {
  await page.goto("/tests/history-browser.html?completion-reading=1");
  const viewport = page.locator(".thread");
  const distance = () => viewport.evaluate(node => node.scrollHeight - node.clientHeight - node.scrollTop);
  await expect.poll(distance).toBeLessThan(3);
  await page.getByTestId("append-answer").evaluate(node => (node as HTMLButtonElement).click());
  await expect.poll(distance).toBeLessThan(3);
  await page.getByTestId("complete-answer").evaluate(node => (node as HTMLButtonElement).click());
  await expect(page.locator(".process-reasoning-body")).toHaveCount(0);
  await waitForScrollIdle(page);
  expect(await distance()).toBeLessThan(3);
  await expect(page.locator(".scroll-bottom-btn")).toHaveCount(0);
});

test("turn regressions reading anchor survives message DOM replacement without confusing repeated prose", async ({ page }) => {
  await page.goto("/tests/history-browser.html?completion-reading=1");
  const results = await page.evaluate(async () => {
    const modulePath = "/src/reading-position.ts";
    const { captureReadingPosition, readingPositionDelta } = await import(modulePath);
    return ["same-message", "other-message", "other-turn", "duplicate-paragraph"].map(mode => {
      const viewport = document.createElement("div");
      viewport.style.cssText = "position:fixed;inset:0 auto auto 0;width:300px;height:160px;overflow:auto;overflow-anchor:none";
      viewport.innerHTML = '<div data-turn-id="turn"><div style="height:300px"></div>'
        + '<div class="assistant-answer-segment" data-message-id="stream"><div class="prose">'
        + '<p style="height:400px;margin:0">The paragraph being read.</p></div></div>'
        + '<div style="height:1000px"></div></div>';
      document.body.append(viewport);
      viewport.scrollTop = 350;
      const original = viewport.querySelector<HTMLElement>("p")!;
      const before = original.getBoundingClientRect().top;
      const position = captureReadingPosition(viewport, "same-view");
      const answer = viewport.querySelector<HTMLElement>(".assistant-answer-segment")!;
      const replacement = answer.cloneNode(true) as HTMLElement;
      replacement.dataset.messageId = "native";
      if (mode !== "other-message") replacement.dataset.streamMessageId = "stream";
      if (mode === "duplicate-paragraph") replacement.querySelector(".prose")!.append(original.cloneNode(true));
      answer.replaceWith(replacement);
      const turn = viewport.firstElementChild as HTMLElement;
      if (mode === "other-turn") turn.dataset.turnId = "another-turn";
      (turn.firstElementChild as HTMLElement).style.height = "240px";
      // Layout correction must preserve native movement since capture.
      viewport.scrollTop -= 25;
      const delta = readingPositionDelta(viewport, position);
      if (delta != null) viewport.scrollTop += delta;
      const motion = replacement.querySelector("p")!.getBoundingClientRect().top - before;
      viewport.remove();
      return { mode, delta, motion };
    });
  });
  expect(results[0]).toEqual({ mode: "same-message", delta: -60, motion: 25 });
  for (const result of results.slice(1)) expect(result.delta).toBeNull();
});

test("turn regressions manual keyboard collapse retains the process header", async ({ page, isMobile }) => {
  test.skip(isMobile, "Physical desktop keyboard activation");
  await page.goto("/tests/history-browser.html?completion-reading=1&short-process=1");
  const viewport = page.locator(".thread");
  const header = page.locator(".turn-process-head");
  await waitForScrollIdle(page);
  const offset = () => header.evaluate(node => node.getBoundingClientRect().top
    - node.closest(".thread")!.getBoundingClientRect().top);
  await viewport.hover();
  await page.mouse.wheel(0, await offset());
  await waitForScrollIdle(page);
  const before = await offset();
  expect(Math.abs(before)).toBeLessThan(3);
  await header.press("Enter");
  await expect(header).toHaveAttribute("aria-expanded", "false");
  await waitForScrollIdle(page);
  expect(Math.abs(await offset() - before)).toBeLessThan(3);
  await expect(page.locator("[data-process-auto-collapsed]")).toHaveCount(0);
});

test("turn regressions ArrowDown reaches bottom after a small wheel scroll", async ({ page, isMobile }) => {
  test.skip(isMobile, "Physical desktop keyboard scrolling");
  await page.goto("/tests/history-browser.html?turn-regressions=1");
  const viewport = page.locator(".thread");
  await expect(page.locator(".turn-working")).toBeVisible();
  await viewport.hover();
  await page.mouse.wheel(0, -40);
  await expect(page.locator(".scroll-bottom-btn")).toBeVisible();
  await waitForScrollIdle(page);
  // Deliver new output without moving keyboard focus to the fixture button.
  await page.getByTestId("regression-live-toggle").evaluate(node => (node as HTMLButtonElement).click());
  await expect(viewport).toContainText("Live answer line 1");
  for (let repeat = 0; repeat < 12; repeat++) {
    await page.keyboard.down("ArrowDown");
    await page.waitForTimeout(40);
  }
  await page.keyboard.up("ArrowDown");
  await expect.poll(() => viewport.evaluate(node => node.scrollHeight - node.clientHeight - node.scrollTop)).toBeLessThan(3);
  await expect(page.locator(".scroll-bottom-btn")).toHaveCount(0);
  // Wheel focus repair is limited to an unfocused document, never an active control.
  const control = page.getByTestId("regression-live-toggle");
  await control.focus();
  await viewport.hover();
  await page.mouse.wheel(0, -40);
  await expect(page.locator(".scroll-bottom-btn")).toBeVisible();
  await expect(control).toBeFocused();
});

test("turn regressions a resized viewport at bottom hides the return button without resuming output", async ({ page }) => {
  await page.goto("/tests/history-browser.html?turn-regressions=1");
  const viewport = page.locator(".thread");
  await expect(page.locator(".turn-working")).toBeVisible();
  await viewport.evaluate(node => {
    node.dispatchEvent(new WheelEvent("wheel", { bubbles: true, deltaY: -40 }));
    node.scrollTop -= 40;
    node.dispatchEvent(new Event("scroll"));
  });
  await expect(page.locator(".scroll-bottom-btn")).toBeVisible();
  await waitForScrollIdle(page);
  const size = page.viewportSize()!;
  await page.setViewportSize({ ...size, height: size.height + 180 });
  await expect.poll(() => viewport.evaluate(node =>
    node.scrollHeight - node.clientHeight - node.scrollTop)).toBeLessThan(3);
  await expect(page.locator(".scroll-bottom-btn")).toHaveCount(0);

  // Geometry alone must not turn following back on. New output should expose
  // the button again, without pulling the reader away from this position.
  await page.getByTestId("regression-live-toggle").evaluate(node => (node as HTMLButtonElement).click());
  await expect.poll(() => viewport.evaluate(node =>
    node.scrollHeight - node.clientHeight - node.scrollTop)).toBeGreaterThan(80);
  await expect(page.locator(".scroll-bottom-btn")).toBeVisible();
  await page.locator(".scroll-bottom-btn").click();
  await expect(page.locator(".scroll-bottom-btn")).toHaveCount(0);
  await page.waitForTimeout(350);
  expect(await viewport.evaluate(node =>
    node.scrollHeight - node.clientHeight - node.scrollTop)).toBeLessThan(3);
});

test("turn regressions manual small scroll back to bottom hides the return button", async ({ page }) => {
  await page.goto("/tests/history-browser.html?turn-regressions=1");
  const viewport = page.locator(".thread");
  await expect(page.locator(".turn-working")).toBeVisible();
  await expect(page.locator(".scroll-bottom-btn")).toHaveCount(0);
  for (const distance of [8, 60]) {
    await viewport.evaluate((node, delta) => {
      node.dispatchEvent(new WheelEvent("wheel", { bubbles: true, deltaY: -delta }));
      node.scrollTop -= delta;
      node.dispatchEvent(new Event("scroll"));
    }, distance);
    await expect(page.locator(".scroll-bottom-btn")).toBeVisible();
    await viewport.evaluate(node => {
      node.dispatchEvent(new WheelEvent("wheel", { bubbles: true, deltaY: 100 }));
      node.scrollTop = node.scrollHeight;
      node.dispatchEvent(new Event("scroll"));
    });
    await expect(page.locator(".scroll-bottom-btn")).toHaveCount(0);
  }
  // A coalesced/clamped scroll may reach the edge before the final downward
  // gesture. At that point the browser has no further scroll event to emit.
  for (const gesture of ["wheel", "touch"]) {
    await viewport.evaluate(node => {
      node.dispatchEvent(new WheelEvent("wheel", { bubbles: true, deltaY: -8 }));
      node.scrollTop -= 8;
      node.dispatchEvent(new Event("scroll"));
      node.scrollTop = node.scrollHeight;
      node.dispatchEvent(new Event("scroll"));
    });
    await expect(page.locator(".scroll-bottom-btn")).toHaveCount(0);
    if (gesture === "wheel") await viewport.dispatchEvent("wheel", { deltaY: 30 });
    else await dispatchTouchGesture(page, -30);
    await expect(page.locator(".scroll-bottom-btn")).toHaveCount(0);
  }
});

async function scrollRegressionToMiddle(page: import("@playwright/test").Page, projectName: string) {
  await scrollThreadToEdge(page, "start", projectName);
  if (isMobileWebKitProject(projectName)) await dispatchTouchGesture(page, -60);
  else await page.locator(".thread").dispatchEvent("wheel", { deltaY: 500 });
  await page.locator(".thread").evaluate((node) => {
    node.scrollTop = 500;
    node.dispatchEvent(new Event("scroll"));
  });
  await waitForScrollIdle(page);
}

test("turn regressions preserve history on send and stream, but follow at bottom", async ({ page }, testInfo) => {
  await page.goto("/tests/history-browser.html?turn-regressions=1");
  const scroll = page.locator(".thread");
  await expect(page.getByTestId("regression-send")).toBeVisible();
  await scrollRegressionToMiddle(page, testInfo.project.name);
  await page.waitForTimeout(150);
  const before = await scroll.evaluate((node) => node.scrollTop);
  await page.getByTestId("regression-send").click();
  await page.getByTestId("regression-stream").click();
  await page.waitForTimeout(200);
  expect(Math.abs(await scroll.evaluate((node) => node.scrollTop) - before)).toBeLessThan(3);
  await page.getByRole("button", { name: "滚动到底部", exact: true }).click();
  await expect.poll(() => scroll.evaluate((node) => node.scrollHeight - node.clientHeight - node.scrollTop)).toBeLessThan(3);
  await page.getByTestId("regression-stream").click();
  await expect.poll(() => scroll.evaluate((node) => node.scrollHeight - node.clientHeight - node.scrollTop)).toBeLessThan(3);
});

test("turn regressions copy action follows visible block and copies its entire content", async ({ page }, testInfo) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
      writeText: (value: string) => { (window as unknown as { copiedText: string }).copiedText = value; return Promise.resolve(); },
    } });
  });
  await page.goto("/tests/history-browser.html?turn-regressions=1");
  const scroll = page.locator(".thread");
  const first = page.locator(".message-code-block").first();
  await expect(first).toBeAttached();
  await scrollRegressionToMiddle(page, testInfo.project.name);
  await page.waitForTimeout(150);
  const blockRect = await first.boundingBox();
  const button = first.getByRole("button", { name: "复制代码" });
  const buttonRect = await button.boundingBox();
  const scrollRect = await scroll.boundingBox();
  expect(blockRect!.y).toBeLessThan(scrollRect!.y);
  expect(blockRect!.y + blockRect!.height).toBeGreaterThan(scrollRect!.y + 100);
  expect(buttonRect!.y).toBeGreaterThanOrEqual(scrollRect!.y);
  expect(buttonRect!.y + buttonRect!.height).toBeLessThan(blockRect!.y + blockRect!.height);
  await button.click();
  const copied = await page.evaluate(() => (window as unknown as { copiedText: string }).copiedText);
  expect(copied).toContain("Copy entire block line 0");
  expect(copied).toContain("Copy entire block line 89");
  expect(copied).not.toContain("Second block");
});

test("turn regressions keep changes collapsed per turn and remove the empty spark gap", async ({ page }) => {
  await page.goto("/tests/history-browser.html?turn-regressions=1");
  await expect(page.locator(".turn-changes-toggle")).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator(".turn-changes-files")).toHaveCount(0);
  await expect(page.locator(".page-meta")).toHaveCount(0);
  const gap = await page.locator(".turn-working").last().evaluate((node) =>
    node.getBoundingClientRect().top - node.previousElementSibling!.getBoundingClientRect().bottom);
  expect(gap).toBeLessThan(24);
  await page.locator(".turn-changes-toggle").click();
  await page.getByRole("button", { name: "src/code.py", exact: true }).click();
  await expect(page.getByTestId("regression-opened")).toHaveText("history-one:immutable-one:src/code.py");
  await page.getByTestId("regression-send").click();
  await expect(page.locator(".turn-changes-toggle")).toHaveAttribute("aria-expanded", "true");
});

test("turn regressions show typed historical files after refresh and session switch", async ({ page }, testInfo) => {
  await page.goto("/tests/history-browser.html?historical-files=1");
  const toggles = page.locator(".turn-changes-toggle");
  await expect(toggles).toHaveCount(2);
  await expect(toggles.first()).toHaveAttribute("aria-expanded", "false");
  await toggles.first().click();
  const list = page.locator(".turn-changes-files").first();
  await expect(list.locator(".turn-change-file")).toHaveCount(7);
  await expect(list.getByRole("img", { name: "Python", exact: true })).toHaveCount(2);
  await expect(list.getByRole("img", { name: "JSON", exact: true })).toBeVisible();
  await expect(list.getByRole("img", { name: "Docker", exact: true })).toBeVisible();
  await expect(list.getByRole("button", { name: "预览当前文件" })).toBeEnabled();
  const oldPath = "/workspace/robot-viewer/src/perception/camera.py";
  await list.getByRole("button", { name: oldPath, exact: true }).click();
  await expect(page.getByTestId("historical-file-opened")).toHaveText(`history-1:revision-1:${oldPath}`);
  await page.getByRole("button", { name: "刷新历史", exact: true }).click();
  await expect(toggles).toHaveCount(2);
  await page.getByRole("button", { name: "切换会话", exact: true }).click();
  await expect(toggles).toHaveCount(0);
  await page.getByRole("button", { name: "切换会话", exact: true }).click();
  await expect(toggles).toHaveCount(2);
  if (await toggles.first().getAttribute("aria-expanded") === "false") await toggles.first().click();
  await list.getByRole("button", { name: oldPath, exact: true }).click();
  await expect(page.getByTestId("historical-file-opened")).toHaveText(`history-1:revision-1:${oldPath}`);
  expect(await list.evaluate((node) => node.scrollWidth - node.clientWidth)).toBeLessThan(2);
  await page.screenshot({ path: testInfo.outputPath("historical-files-light.png") });
  await page.getByRole("button", { name: "切换主题", exact: true }).click();
  await page.screenshot({ path: testInfo.outputPath("historical-files-dark.png") });
});

test("turn regressions page all files and retry without losing the loaded list", async ({ page }) => {
  let calls = 0;
  await page.route("**/fixture-turn-files?**", async (route) => {
    calls += 1;
    if (calls === 1) { await route.fulfill({ status: 503, body: "unavailable" }); return; }
    const params = new URL(route.request().url()).searchParams;
    const offset = Number(params.get("offset"));
    const files = Array.from({ length: Math.min(64, 130 - offset) }, (_, index) => ({
      path: `/workspace/robot-viewer/src/file-${offset + index}.py`, state: "available", additions: 1, deletions: 1,
    }));
    await route.fulfill({ json: { type: "turn_file_changes_page", engine: "codex",
      turn_id: params.get("turn"), revision: params.get("revision"), offset, files,
      total_files: 130, next_offset: offset + files.length < 130 ? offset + files.length : null } });
  });
  await page.goto("/tests/history-browser.html?historical-files=1&many-files=1");
  const toggle = page.locator(".turn-changes-toggle").first();
  await expect(toggle).toContainText("130 个文件");
  await expect(toggle).not.toContainText("部分记录");
  await toggle.click();
  const list = page.locator(".turn-changes-files").first();
  await expect(list.locator(".turn-change-file")).toHaveCount(64);
  await list.getByRole("button", { name: "加载更多", exact: true }).click();
  await expect(list.getByRole("alert")).toBeVisible();
  await expect(list.locator(".turn-change-file")).toHaveCount(64);
  await list.getByRole("button", { name: "重试加载", exact: true }).click();
  await expect(list.locator(".turn-change-file")).toHaveCount(128);
  await list.getByRole("button", { name: "加载更多", exact: true }).click();
  await expect(list.locator(".turn-change-file")).toHaveCount(130);
  await expect(list.locator(".turn-changes-more")).toHaveCount(0);
  const path = "/workspace/robot-viewer/src/file-129.py";
  await list.getByRole("button", { name: path, exact: true }).click();
  await expect(page.getByTestId("historical-file-opened")).toHaveText(`history-1:revision-1-0:${path}`);
  await toggle.click();
  await toggle.click();
  await expect(list.locator(".turn-change-file")).toHaveCount(130);
  expect(calls).toBe(3);
});

test("turn regressions discard pending file pages after revision and session changes", async ({ page }) => {
  const pending: Array<() => Promise<void>> = [];
  await page.route("**/fixture-turn-files?**", (route) => {
    const params = new URL(route.request().url()).searchParams;
    pending.push(() => route.fulfill({ json: {
      type: "turn_file_changes_page", engine: "codex", turn_id: params.get("turn"),
      revision: params.get("revision"), offset: 64, total_files: 130, next_offset: 128,
      files: Array.from({ length: 64 }, (_, i) => ({
        path: `/workspace/robot-viewer/src/file-${64 + i}.py`, state: "available", additions: 1, deletions: 1,
      })),
    } }).catch(() => {}));
  });
  await page.goto("/tests/history-browser.html?historical-files=1&many-files=1");
  const toggle = page.locator(".turn-changes-toggle").first();
  await toggle.click();
  await page.getByRole("button", { name: "加载更多", exact: true }).click();
  await expect.poll(() => pending.length).toBe(1);
  await page.getByRole("button", { name: "刷新历史", exact: true }).click();
  await pending.shift()!();
  await expect(page.locator(".turn-change-file")).toHaveCount(64);
  await page.getByRole("button", { name: "加载更多", exact: true }).click();
  await expect.poll(() => pending.length).toBe(1);
  await page.getByRole("button", { name: "切换会话", exact: true }).click();
  await pending.shift()!();
  await expect(page.locator(".turn-change-file")).toHaveCount(0);
  await page.getByRole("button", { name: "切换会话", exact: true }).click();
  if (await toggle.getAttribute("aria-expanded") === "false") await toggle.click();
  await expect(page.locator(".turn-change-file")).toHaveCount(64);
});

type PanelRelayEvent<T = ServerEvent> = T extends ServerEvent
  ? Omit<T, "v" | "ts"> : never;

// Exercise the real App shell, not a copied layout fixture. All control/model
// traffic terminates in this in-memory relay; no real session is touched.
async function mockRightPanelRelay(
  page: import("@playwright/test").Page,
  { visible = false, retained = true, engine = "codex", seedTurns = [], parentSid = "layout-parent",
    secondParent = false, parentState = "idle", btwReadOnly = false, imageAssets = false, imageData, externalPreview, historyReply, listReply, capabilityReply,
    devices = [{ machine_id: "layout-machine", label: "Layout", online: true }], sessionIdForMachine }: {
    visible?: boolean;
    retained?: boolean;
    engine?: "codex" | "claude" | "dsh";
    devices?: { machine_id: string; label: string; online: boolean }[];
    parentSid?: string;
    sessionIdForMachine?: (machineId: string) => string;
    seedTurns?: NonNullable<Extract<ServerEvent, { type: "history" }>["turns"]>;
    secondParent?: boolean;
    parentState?: "idle" | "running";
    btwReadOnly?: boolean;
    imageAssets?: boolean;
    imageData?: { data: string; width: number; height: number };
    externalPreview?: "allow" | "replace";
    historyReply?: (command: Record<string, unknown>, machineId: string) => PanelRelayEvent<Extract<ServerEvent, { type: "history" }>> | null;
    listReply?: (command: Record<string, unknown>, machineId: string) => PanelRelayEvent | null | undefined;
    capabilityReply?: (command: Record<string, unknown>) => PanelRelayEvent | undefined;
  } = {},
) {
  const btwSid = "btw-layout-child";
  const commands: Record<string, unknown>[] = [];
  const allowedReads = new Set<string>();
  let emit: (message: PanelRelayEvent) => void = () => {
    throw new Error("layout relay has not connected");
  };
  await page.addInitScript(({ visible, engine, parentSid }) => {
    localStorage.setItem("cc_remote_engine", engine);
    localStorage.setItem("cc_remote_machine", "layout-machine");
    // Preserve a user's resize preference while opening/closing the slot.
    if (localStorage.getItem("cc_remote_artifact_panel_width") === null) {
      localStorage.setItem("cc_remote_artifact_panel_width", "520");
    }
    if (sessionStorage.getItem("cc-remote:btw-panel-scopes-v1") === null) {
      sessionStorage.setItem("cc-remote:btw-panel-scopes-v1", JSON.stringify(visible
        ? [JSON.stringify(["layout-machine", "code", engine, parentSid])] : []));
    }
  }, { visible, engine, parentSid });
  await page.route("**/api/**", (route) => route.fulfill({ status: 404 }));
  await page.route("**/api/session", (route) => route.fulfill({ json: {} }));
  await page.route("**/api/viewers/pages", (route) => route.fulfill({ json: { pages: [] } }));
  await page.route("**/api/devices", (route) => route.fulfill({ json: {
    devices,
  } }));
  await page.routeWebSocket(/\/ws(?:\?|$)/, (socket) => {
    const machineId = new URL(socket.url()).searchParams.get("machine") ?? "default";
    const machineParentSid = sessionIdForMachine?.(machineId) ?? parentSid;
    emit = (message) => socket.send(JSON.stringify({
      v: PROTOCOL_VERSION, ts: Date.now() / 1000, ...message,
    }));
    const snapshot = (sid: string) => {
      emit({ type: "snapshot", sid, cc_session_id: sid,
        state: sid === btwSid ? (btwReadOnly ? "idle" : "running") : parentState, tail_text: "",
        cwd: "/tmp/layout", generation: "layout-generation",
        ...(sid === btwSid && btwReadOnly ? { control: {
          v: PROTOCOL_VERSION, ts: 1, type: "session_control" as const,
          control_mode: "codex_shared" as const, write_state: "read_only" as const,
          terminal_attached: false, can_takeover: false, revision: 2,
          generation: "layout-generation",
          reason: "当前临时对话已经销毁，请创建新的临时对话",
        } } : {}),
      });
      emit({ type: "replay_end", sid, to_seq: 0, truncated: false });
    };
    socket.onMessage((raw) => {
      const command = JSON.parse(String(raw)) as Record<string, unknown>;
      commands.push(command);
      if (command.type === "hello") {
        emit({ type: "btw_sync", generation: "layout-generation", revision: 1,
          sessions: retained ? [{ btw_sid: btwSid, parent_sid: parentSid,
            engine, created_at: 1, state: "running" }] : [] });
        snapshot(machineParentSid);
      } else if (command.type === "list_sessions") {
        const response = listReply?.(command, machineId);
        if (response === null) return; // Allow a test to release restoration explicitly.
        if (response) { emit(response); return; }
        emit({ type: "session_list",
          engine: command.engine === "codex" ? "codex" : command.engine === "dsh" ? "dsh" : "claude",
          space: command.space === "work" ? "work" : "code",
          request_id: String(command.cmd_id),
          sessions: command.space === "work" ? [] : [{ session_id: machineParentSid,
            engine, space: "code", summary: "Layout parent",
            cwd: "/tmp/layout", state: parentState, last_modified: "100" },
          ...(secondParent ? [{ session_id: "layout-other", engine,
            space: "code" as const, summary: "Second parent", cwd: "/tmp/other",
            state: "idle" as const, last_modified: "50" }] : [])] });
      } else if (command.type === "switch_session") {
        emit({ type: "session_focus", session_id: String(command.session_id) });
        snapshot(String(command.session_id));
      } else if (command.type === "get_history") {
        const response = historyReply ? historyReply(command, machineId) : { type: "history" as const, session_id: String(command.session_id),
          sid: String(command.session_id),
          revision: "layout-history", generation: "layout-generation",
          detail: "summary" as const, events: [], turns: seedTurns, has_more: false };
        if (response) emit(response);
      } else if (externalPreview && (command.type === "get_file_preview" || command.type === "get_preview_asset")) {
        const requestId = String(command.request_id);
        const path = String(command.path);
        const isFile = command.type === "get_file_preview";
        if (!allowedReads.has(requestId) || externalPreview === "replace") {
          emit({ type: "preview_authorization_required", sid: String(command.sid),
            authorization_id: `${allowedReads.has(requestId) ? "replaced" : "original"}-${requestId}`,
            request_id: requestId, operation: isFile ? "file_preview" : "preview_asset",
            path, resolved_path: path, format: isFile ? "markdown" : "image",
            preview_id: isFile ? null : String(command.preview_id) });
        } else if (isFile) {
          emit({ type: "file_preview", sid: String(command.sid), request_id: requestId,
            path, format: "markdown", content: "# 外部预览已经打开", size: 30,
            mtime_ns: "1", revision: "a".repeat(64), writable: false });
        } else {
          emit({ type: "preview_asset", sid: String(command.sid), request_id: requestId,
            path, preview_id: String(command.preview_id), media_type: "image/png", data: TEST_GENERATED_PNG });
        }
      } else if (externalPreview && command.type === "authorize_preview") {
        const requestId = String(command.request_id);
        allowedReads.add(requestId);
        emit({ type: "preview_authorization_result", sid: String(command.sid),
          authorization_id: String(command.authorization_id), request_id: requestId,
          status: "granted" });
      } else if (imageAssets && command.type === "get_history_image") {
        emit({ type: "history_image", sid: String(command.session_id),
          session_id: String(command.session_id), turn_id: String(command.turn_id),
          image_id: String(command.image_id), variant: command.variant as "thumbnail" | "full",
          request_id: String(command.request_id), revision: "layout-history",
          media_type: "image/png", width: 1, height: 1,
          data: TEST_GENERATED_PNG,
          ...(command.variant === "full" ? imageData : {}) });
      } else if (imageAssets && command.type === "get_preview_asset") {
        emit({ type: "preview_asset", sid: String(command.sid),
          path: String(command.path), preview_id: String(command.preview_id),
          request_id: String(command.request_id), media_type: "image/png",
          data: imageData?.data ?? TEST_GENERATED_PNG });
      } else if (command.type === "sync_btw") {
        if (btwReadOnly) {
          emit({ type: "user_msg", sid: String(command.sid),
            msg_id: "retained-user", prompt: "以前的侧边提问" });
          emit({ type: "delta", sid: String(command.sid),
            message_id: "retained-answer", text: "仍可查看的侧边回答" });
        }
        snapshot(String(command.sid));
      } else if (command.type === "get_engine_capabilities") {
        const response = capabilityReply?.(command);
        if (response) emit(response);
      } else if (command.type === "get_diff") {
        emit({ type: "diff_report", sid: String(command.sid),
          request_id: String(command.cmd_id), file: "", diff: "" });
      } else if (command.type === "get_goal") {
        emit({ type: "goal_state", sid: String(command.sid),
          request_id: String(command.cmd_id), goal: null });
      } else if (command.type === "ping") {
        emit({ type: "pong", n: Number(command.n) });
      }
      if (command.cmd_id) emit({ type: "command_ack",
        client_id: String(command.client_id), cmd_id: String(command.cmd_id) });
    });
  });
  return { commands, emit: (message: PanelRelayEvent) => emit(message) };
}

for (const engine of ["claude", "codex", "dsh"] as const) {
  test(`device switching retains isolated instant session cache for ${engine}`, async ({ page }) => {
    let holdFirstDevice = false;
    const sid = "shared-native-session";
    await mockRightPanelRelay(page, {
      engine, retained: false, parentSid: sid,
      devices: [
        { machine_id: "layout-machine", label: "Device A", online: true },
        { machine_id: "second-machine", label: "Device B", online: true },
      ],
      historyReply: (_command, machine) => holdFirstDevice && machine === "layout-machine" ? null : {
        type: "history", session_id: sid, sid, revision: "device-history",
        generation: "layout-generation", detail: "summary", events: [], has_more: false,
        turns: [{ id: "native-turn", prompt: "Check device", done: true, ts: 1000,
          blocks: [{ kind: "text", message_id: "native-answer", channel: "final",
            text: `Cached answer from ${machine}`, done: true }] }],
      },
    });
    await page.goto("/");
    const firstAnswer = page.getByText("Cached answer from layout-machine", { exact: true });
    await expect(firstAnswer).toBeVisible();
    await expect.poll(() => page.evaluate(async sid => {
      const cache = await import("/src/cache.ts");
      return (await cache.loadSession(sid, "layout-machine"))?.turns.length;
    }, sid)).toBe(1);
    await page.evaluate(async ({ sid, engine }) => {
      const cache = await import("/src/cache.ts");
      const { HistoryPageCache } = await import("/src/history-page-cache.ts");
      const saved = await cache.loadSession(sid, "layout-machine");
      const pages = new HistoryPageCache();
      await pages.putPage({ machineId: "layout-machine", sid, engine, space: "code", revision: "device-history" }, {
        pageKey: "retained-page", turns: saved!.turns, hasOlder: false, isLatest: true,
      });
    }, { sid, engine });
    const selectDevice = async (label: string) => {
      await page.getByRole("button", { name: "设备中心", exact: true }).click();
      await page.getByRole("dialog", { name: "设备中心" }).getByRole("button", { name: new RegExp(label) }).click();
    };
    await selectDevice("Device B");
    await expect(page.getByText("Cached answer from second-machine", { exact: true })).toBeVisible();
    await expect(firstAnswer).toHaveCount(0);
    await expect.poll(() => page.evaluate(async sid => {
      const cache = await import("/src/cache.ts");
      return (await cache.loadSession(sid, "second-machine"))?.turns.length;
    }, sid)).toBe(1);
    holdFirstDevice = true;
    await selectDevice("Device A");
    await expect(firstAnswer).toBeVisible();
    await expect(page.getByText("Cached answer from second-machine", { exact: true })).toHaveCount(0);
    await page.evaluate(async sid => {
      const cache = await import("/src/cache.ts");
      for (const [machine, sequence] of [["layout-machine", 111], ["second-machine", 222]] as const) {
        const saved = await cache.loadSession(sid, machine);
        cache.saveSession(sid, saved!.turns, sequence, saved!.revision,
          saved!.generation, saved!.control, saved!.historyAtStart, machine);
      }
    }, sid);
    await expect.poll(() => page.evaluate(async sid => {
      const cache = await import("/src/cache.ts");
      return [(await cache.loadAllReplayState("layout-machine")).cursors[sid],
        (await cache.loadAllReplayState("second-machine")).cursors[sid]];
    }, sid)).toEqual([111, 222]);
    const retained = await page.evaluate(async ({ sid, engine }) => {
      const cache = await import("/src/cache.ts");
      const { HistoryPageCache } = await import("/src/history-page-cache.ts");
      const page = await new HistoryPageCache().getPage({
        machineId: "layout-machine", sid, engine, space: "code", revision: "device-history",
      }, "retained-page");
      const first = await cache.loadSession(sid, "layout-machine");
      const second = await cache.loadSession(sid, "second-machine");
      await cache.invalidateSessionCache(sid, "layout-machine");
      const invalidatedFirst = await cache.loadSession(sid, "layout-machine");
      const keptSecond = await cache.loadSession(sid, "second-machine");
      cache.saveSession(sid, keptSecond!.turns, 223, keptSecond!.revision,
        keptSecond!.generation, keptSecond!.control, keptSecond!.historyAtStart, "second-machine");
      await cache.clearCache();
      // The coalesced writer must also stay empty beyond its 400ms deadline.
      await new Promise(resolve => setTimeout(resolve, 450));
      return {
        pageRetained: !!page,
        first: first?.turns[0].blocks[0].text,
        second: second?.turns[0].blocks[0].text,
        invalidatedFirst,
        secondSurvivesInvalidation: !!keptSecond,
        clearedSecond: await cache.loadSession(sid, "second-machine"),
      };
    }, { sid, engine });
    expect(retained).toEqual({
      first: "Cached answer from layout-machine", second: "Cached answer from second-machine",
      pageRetained: true, invalidatedFirst: null, secondSurvivesInvalidation: true, clearedSecond: null,
    });
  });
}

for (const engine of ["claude", "codex", "dsh"] as const) {
  test(`device return paints cached ${engine} before the catalog or history replies`, async ({ page }) => {
    let holdReturningDevice = false;
    const relay = await mockRightPanelRelay(page, {
      engine, retained: false,
      sessionIdForMachine: machine => `${machine}-native-session`,
      devices: [
        { machine_id: "layout-machine", label: "Mac", online: true },
        { machine_id: "second-machine", label: "VPS", online: true },
      ],
      listReply: (_command, machine) => holdReturningDevice && machine === "layout-machine" ? null : undefined,
      historyReply: (command, machine) => holdReturningDevice && machine === "layout-machine" ? null : {
        type: "history", session_id: String(command.session_id), sid: String(command.session_id),
        revision: "instant-device-history", generation: "layout-generation",
        detail: "summary", events: [], has_more: false,
        turns: [{ id: "native-turn", prompt: "Check device", done: true, ts: 1000,
          blocks: [{ kind: "text", message_id: "native-answer", channel: "final",
            text: `Remembered answer from ${machine}`, done: true }] }],
      },
    });
    const selectDevice = async (label: string) => {
      await page.getByRole("button", { name: "设备中心", exact: true }).click();
      await page.getByRole("dialog", { name: "设备中心" }).getByRole("button", { name: new RegExp(label) }).click();
    };
    await page.goto("/");
    const macAnswer = page.getByText("Remembered answer from layout-machine", { exact: true });
    await expect(macAnswer).toBeVisible();
    await expect.poll(() => page.evaluate(async () => {
      const cache = await import("/src/cache.ts");
      return (await cache.loadSession("layout-machine-native-session", "layout-machine"))?.turns.length;
    })).toBe(1);
    await selectDevice("VPS");
    await expect(page.getByText("Remembered answer from second-machine", { exact: true })).toBeVisible();
    await page.evaluate(() => {
      document.documentElement.dataset.deviceReturnSpinner = "0";
      document.documentElement.dataset.deviceReturnWrongMachine = "0";
      const transaction = IDBDatabase.prototype.transaction;
      IDBDatabase.prototype.transaction = function (storeNames, mode, options) {
        if (this.name === "cc_remote_cache" && mode === "readonly") {
          throw new DOMException("IndexedDB paused for the return-view regression", "InvalidStateError");
        }
        return transaction.call(this, storeNames, mode, options);
      };
      new MutationObserver(() => {
        if (localStorage.getItem("cc_remote_machine") === "layout-machine") {
          if (document.querySelector(".empty .spinner")) {
            document.documentElement.dataset.deviceReturnSpinner = "1";
          }
          if (document.body.textContent?.includes("Remembered answer from second-machine")) {
            document.documentElement.dataset.deviceReturnWrongMachine = "1";
          }
        }
      }).observe(document.body, { childList: true, subtree: true });
    });
    holdReturningDevice = true;
    const returningCommandsStart = relay.commands.length;
    await selectDevice("Mac");
    await expect(macAnswer).toBeVisible({ timeout: 250 });
    await expect(page.getByText("Remembered answer from second-machine", { exact: true })).toHaveCount(0);
    await expect(page.locator(".empty .spinner")).toHaveCount(0);
    expect(await page.locator("html").getAttribute("data-device-return-spinner")).toBe("0");
    expect(await page.locator("html").getAttribute("data-device-return-wrong-machine")).toBe("0");
    expect(relay.commands.some(command => command.type === "query" || command.type === "new_session")).toBe(false);
    await expect.poll(() => relay.commands.slice(returningCommandsStart).some(command =>
      command.type === "list_sessions" && command.engine === engine && command.space === "code")).toBe(true);
    const listRequest = relay.commands.slice(returningCommandsStart).findLast(command =>
      command.type === "list_sessions" && command.engine === engine && command.space === "code")!;
    relay.emit({ type: "session_list", engine, space: "code", request_id: String(listRequest.cmd_id),
      sessions: [{ session_id: "layout-machine-native-session", engine, space: "code",
        summary: "Current Mac session", cwd: "/tmp/layout", state: "idle" }] });
    await expect.poll(() => relay.commands.slice(returningCommandsStart).some(command =>
      command.type === "get_history" && command.session_id === "layout-machine-native-session")).toBe(true);
    relay.emit({ type: "history", session_id: "layout-machine-native-session", sid: "layout-machine-native-session",
      revision: "instant-device-history", generation: "layout-generation",
      detail: "summary", events: [], has_more: false,
      turns: [{ id: "native-turn", prompt: "Check device", done: true, ts: 1000,
        blocks: [{ kind: "text", message_id: "native-answer", channel: "final",
          text: "Updated Mac answer after revalidation", done: true }] }] });
    await expect(page.getByText("Updated Mac answer after revalidation", { exact: true })).toBeVisible();
    await expect(macAnswer).toHaveCount(0);
    const previousLists = relay.commands.filter(command => command.type === "list_sessions").length;
    relay.emit({ type: "session_list_invalidated", engine, space: "code" });
    await expect.poll(() => relay.commands.filter(command => command.type === "list_sessions").length).toBeGreaterThan(previousLists);
    const replacementList = relay.commands.findLast(command =>
      command.type === "list_sessions" && command.engine === engine && command.space === "code")!;
    relay.emit({ type: "session_list", engine, space: "code", request_id: String(replacementList.cmd_id),
      sessions: [{ session_id: "replacement-native-session", engine, space: "code",
        summary: "Replacement session", cwd: "/tmp/layout", state: "idle" }] });
    await expect.poll(() => relay.commands.some(command =>
      command.type === "get_history" && command.session_id === "replacement-native-session")).toBe(true);
    relay.emit({ type: "history", session_id: "replacement-native-session", sid: "replacement-native-session",
      revision: "replacement-history", generation: "layout-generation", detail: "summary", events: [], has_more: false,
      turns: [{ id: "replacement-turn", prompt: "Replacement question", done: true, ts: 1000,
        blocks: [{ kind: "text", message_id: "replacement-answer", channel: "final",
          text: "Replacement session from the live catalog", done: true }] }] });
    await expect(page.getByText("Replacement session from the live catalog", { exact: true })).toBeVisible();
    await expect(page.getByText("Updated Mac answer after revalidation", { exact: true })).toHaveCount(0);
  });
}

test("device switching preserves background push bindings for every authorized device", async ({ page, browserName }) => {
  test.skip(browserName !== "chromium", "Web Push APIs are exercised in Chromium");
  const devices = [
    { machine_id: "layout-machine", label: "Device A", online: true },
    { machine_id: "second-machine", label: "Device B", online: true },
  ];
  await mockRightPanelRelay(page, {
    retained: false, devices,
  });
  const bindings: { machine_ids: string[]; notification_mode: string }[] = [];
  await page.addInitScript(() => {
    localStorage.setItem("cc_remote_notification_mode", "session");
    Object.defineProperty(Notification, "permission", { get: () => "granted" });
    Object.defineProperty(PushManager.prototype, "getSubscription", { value: async () => ({
      toJSON: () => ({ endpoint: "https://push.example/device-group",
        keys: { p256dh: "p".repeat(87), auth: "a".repeat(22) } }),
    }) });
  });
  await page.route("**/api/push-config", route => route.fulfill({
    json: { enabled: true, public_key: "unused-with-existing-subscription" },
  }));
  await page.route("**/api/push/subscribe", route => {
    bindings.push(route.request().postDataJSON());
    return route.fulfill({ json: { ok: true } });
  });
  await page.goto("/");
  expect(await page.evaluate(() => ({
    permission: Notification.permission,
    mode: localStorage.getItem("cc_remote_notification_mode"),
    native: !!window.ccRemoteHarmonyPush,
  }))).toEqual({ permission: "granted", mode: "session", native: false });
  await page.evaluate(async () => {
    await navigator.serviceWorker.register("/sw.js");
    await navigator.serviceWorker.ready;
  });
  await expect.poll(() => bindings.length).toBe(1);
  expect(bindings[0]).toMatchObject({
    machine_ids: ["layout-machine", "second-machine"], notification_mode: "session",
  });
  await page.getByRole("button", { name: "设备中心", exact: true }).click();
  await page.getByRole("dialog", { name: "设备中心" }).getByRole("button", { name: /Device B/ }).click();
  await expect(page.getByRole("button", { name: "设备中心", exact: true })).toContainText("Device B");
  await expect(page.locator(".c-head")).not.toContainText("设备离线");
  expect(bindings).toHaveLength(1);
  devices.push({ machine_id: "third-machine", label: "Device C", online: false });
  await page.getByRole("button", { name: "设备中心", exact: true }).click();
  await expect.poll(() => bindings.length).toBe(2);
  expect(bindings[1].machine_ids).toEqual(["layout-machine", "second-machine", "third-machine"]);
  await page.getByRole("button", { name: "关闭设备中心", exact: true }).click();
  devices.splice(0, 1);
  await page.getByRole("button", { name: "设备中心", exact: true }).click();
  await expect.poll(() => bindings.length).toBe(3);
  expect(bindings[2].machine_ids).toEqual(["second-machine", "third-machine"]);
});

for (const engine of ["claude", "codex"] as const) {
  for (const [width, height] of [[320, 844], [390, 844], [640, 320], [1280, 844]]) {
    test(`terminal status reserves its header position during ${engine} restoration at ${width}px`, async ({ page }, info) => {
      await page.setViewportSize({ width, height });
      const relay = await mockRightPanelRelay(page, { engine, retained: false, listReply: () => null });
      for (const reload of [false, true]) {
        relay.commands.length = 0;
        if (reload) await page.reload();
        else await page.goto("/");
        await expect.poll(() => relay.commands.some(command => command.type === "list_sessions")).toBe(true);
        const terminal = page.locator(".terminal-control-trigger");
        await expect(terminal).toBeVisible();
        if (!reload) await expect(terminal).toBeDisabled();
        await page.evaluate(() => document.fonts.ready);
        const before = await page.locator(".hstat").boundingBox();
        const iconBefore = await terminal.boundingBox();
        for (const command of relay.commands.filter(command => command.type === "list_sessions")) {
          relay.emit({ type: "session_list", engine, space: "code", request_id: String(command.cmd_id),
            sessions: [{ session_id: "layout-parent", engine, space: "code", summary: "Layout parent",
              cwd: "/tmp/layout", state: "idle", last_modified: "100" }] });
        }
        await expect(terminal).toBeEnabled();
        relay.emit({ type: "session_control", sid: "layout-parent", revision: 1,
          generation: "layout-generation", control_mode: engine === "codex" ? "codex_shared" : "claude_broker",
          write_state: "writable", terminal_attached: true, can_takeover: false });
        await expect(terminal).toHaveAccessibleName("终端状态：终端双向连接");
        expect(await page.locator(".hstat").boundingBox()).toEqual(before);
        expect(await terminal.boundingBox()).toEqual(iconBefore);
        await terminal.click();
        const dialog = page.getByRole("dialog", { name: "终端连接状态" });
        await expect(dialog).toBeVisible();
        await dialog.evaluate(async element => {
          await Promise.all(element.getAnimations().map(animation => animation.finished));
        });
        const bounds = (await dialog.boundingBox())!;
        expect(bounds.y).toBeCloseTo(iconBefore!.y + iconBefore!.height + 8, 0);
        expect(bounds.x).toBeGreaterThanOrEqual(10);
        expect(bounds.x + bounds.width).toBeLessThanOrEqual(width - 10);
        expect(bounds.y + bounds.height).toBeLessThanOrEqual(height - 12);
        if (height === 320) expect(await dialog.evaluate(el => el.scrollHeight > el.clientHeight)).toBe(true);
        if (!reload) await page.screenshot({ path: info.outputPath("terminal-status-popover.png") });
        await page.keyboard.press("Escape");
        await expect(terminal).toBeFocused();
      }
      expect(relay.commands.some(command => ["query", "takeover"].includes(String(command.type)))).toBe(false);
    });
  }
}

for (const [engine, space] of [["claude", "code"], ["codex", "code"], ["claude", "work"]] as const) {
  test(`session delete confirmation uses app styling and exact ${engine} ${space} target`, async ({ page }, info) => {
    const relay = await mockRightPanelRelay(page, { engine, retained: false,
      listReply: command => ({ type: "session_list", request_id: String(command.cmd_id), engine,
        space: command.space === "work" ? "work" : "code", sessions: [{
          session_id: "delete-target", engine, space, summary: "待删除的测试会话",
          cwd: "/tmp/delete-test", state: "idle", last_modified: "100",
        }] }),
    });
    if (space === "work") await page.addInitScript(() => localStorage.setItem("cc_remote_space", "work"));
    const nativeDialogs: string[] = [];
    page.on("dialog", async dialog => { nativeDialogs.push(dialog.type()); await dialog.dismiss(); });
    await page.goto("/");
    await page.getByLabel("显示会话侧栏", { exact: true }).click();
    if (space === "work") await page.getByRole("tab", { name: "Work", exact: true }).click();
    const card = page.locator(".scard").filter({ hasText: "待删除的测试会话" }).first();
    const noun = space === "work" ? "工作" : "会话";
    const open = async () => {
      await card.getByRole("button", { name: "更多操作" }).click();
      await page.locator(".card-menu").getByRole("button", { name: `删除${noun}`, exact: true }).click();
    };
    await open();
    const dialog = page.getByRole("dialog", { name: space === "work" ? "删除这项工作？" : "删除这条会话？" });
    await expect(dialog).toContainText("待删除的测试会话");
    await expect(dialog).toContainText(space === "work" ? "私有文件将被永久删除" : "项目目录和代码文件会保留");
    await expect(page.locator(".card-menu")).toHaveCount(0);
    await page.screenshot({ path: info.outputPath(`delete-${engine}-${space}.png`) });
    await dialog.getByRole("button", { name: "取消", exact: true }).click();
    await expect(dialog).toHaveCount(0);
    expect(relay.commands.filter(c => c.type === "delete_session")).toEqual([]);
    await open();
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    expect(relay.commands.filter(c => c.type === "delete_session")).toEqual([]);
    await open();
    await dialog.getByRole("button", { name: `删除${noun}`, exact: true }).click();
    await expect(dialog).toHaveCount(0);
    await expect.poll(() => relay.commands.filter(c => c.type === "delete_session").length).toBe(1);
    expect(relay.commands.find(c => c.type === "delete_session")).toMatchObject({
      session_id: "delete-target", engine, space,
    });
    expect(nativeDialogs).toEqual([]);
  });
}

for (const engine of ["claude", "codex"] as const) {
  test(`side chat scope shows ${engine} background tasks independently of the parent and siblings`, async ({ page, isMobile }, testInfo) => {
    await page.setViewportSize(isMobile ? { width: 390, height: 844 } : { width: 1568, height: 900 });
    const relay = await mockRightPanelRelay(page, { visible: true, engine });
    const sid = "btw-layout-child";
    const sibling = "btw-second-child";
    const panel = page.locator(".btw-panel");
    const trigger = panel.getByRole("button", { name: /^后台任务，/ });
    const dialog = page.getByRole("dialog", { name: "后台任务", exact: true });
    const expectPopoverInsideBtw = async () => {
      if (isMobile) return;
      await expect.poll(async () => {
        const bounds = (await panel.boundingBox())!;
        const popup = (await dialog.boundingBox())!;
        const button = (await trigger.boundingBox())!;
        return popup.x >= bounds.x && popup.x + popup.width <= bounds.x + bounds.width
          && Math.abs(popup.x - button.x) < 2 && popup.y + popup.height < button.y;
      }).toBe(true);
    };
    const tasks = (target: string, names: string[]) => relay.emit({
      type: "background_process_sync", sid: target, generation: "layout-generation",
      items: names.map((title, i) => ({ item_id: `${target}-${i}`, kind: i === 1 ? "agent" : "task",
        status: "running", title, command: i === 0 ? "make side-check" : undefined,
        progress: i === 1 ? "Reading side files" : undefined, started_at: Date.now() / 1000 - 120 })),
    });
    await page.goto("/");
    await expect.poll(() => relay.commands.some(c => c.type === "sync_btw")).toBe(true);
    relay.emit({ type: "btw_sync", generation: "layout-generation", revision: 2,
      sessions: [sid, sibling].map(btw_sid => ({ btw_sid, parent_sid: "layout-parent",
        engine, created_at: 1, state: "running" })) });
    tasks("layout-parent", ["Parent task"]);
    tasks(sid, ["Side build", "Side agent"]);
    tasks(sibling, ["Sibling task"]);
    await expect(trigger).toHaveText("后台任务 · 2");
    await expect(page.locator(".pane .background-task-trigger")).toHaveText("后台任务 · 1");
    await panel.locator("textarea").fill("Preserve side draft");
    await trigger.click();
    await expect(dialog).toContainText("Side build");
    await expect(dialog).toContainText("Side agent");
    await expectPopoverInsideBtw();
    await expect(dialog).not.toContainText("Parent task");
    await expect(dialog).not.toContainText("Sibling task");
    await dialog.getByText("Side build", { exact: true }).click();
    await expect(dialog).toContainText("$ make side-check");
    await dialog.getByText("Side agent", { exact: true }).click();
    await expect(dialog).toContainText("Reading side files");
    await page.screenshot({ path: testInfo.outputPath(`${engine}-btw-background-tasks.png`) });
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(panel.locator("textarea")).toHaveValue("Preserve side draft");
    await panel.getByRole("button", { name: "居中展开侧边对话", exact: true }).click();
    await expect(panel).toHaveClass(/btw-expanded/);
    await page.screenshot({ path: testInfo.outputPath(`${engine}-btw-centered.png`), animations: "disabled" });
    await trigger.click();
    await expect(dialog).toBeVisible();
    await expectPopoverInsideBtw();
    await page.screenshot({ path: testInfo.outputPath(`${engine}-btw-centered-tasks.png`), animations: "disabled" });
    // The nested list owns dismissal even when BTW itself is a modal.
    const outside = (await panel.locator(".btw-title").boundingBox())!;
    await page.mouse.click(outside.x + outside.width / 2, outside.y + outside.height / 2);
    await expect(dialog).toHaveCount(0);
    await expect(panel).toHaveClass(/btw-expanded/);
    await page.keyboard.press("Escape");
    await expect(panel).not.toHaveClass(/btw-expanded/);
    await panel.getByRole("tab", { name: "侧聊 2", exact: true }).click();
    await expect(trigger).toHaveText("后台任务 · 1");
    await expect(dialog).toHaveCount(0);
    await trigger.click();
    await expect(dialog).toContainText("Sibling task");
    await expect(dialog).not.toContainText("Side build");
    await page.keyboard.press("Escape");
    await panel.getByRole("tab", { name: "侧聊 1", exact: true }).click();
    await expect(trigger).toHaveText("后台任务 · 2");
    // Finishing a reply does not finish its detached work.
    relay.emit({ type: "turn_end", sid, turn_id: "side-answer",
      result: { subtype: "success", duration_ms: 1000, is_error: false } });
    relay.emit({ type: "state", sid, state: "idle" });
    await expect(panel.locator(".btw-chat-tab-wrap.active .btw-chat-state")).toHaveClass(/idle/);
    await expect(trigger).toHaveText("后台任务 · 2");
    await trigger.click();
    relay.emit({ type: "process", sid, item_id: `${sid}-0`, kind: "task",
      phase: "end", status: "succeeded", title: "Side build", background: true });
    await expect(trigger).toHaveText("后台任务 · 1");
    await expect(dialog).not.toContainText("Side build");
    tasks(sid, []);
    await expect(trigger).toHaveCount(0);
    await expect(dialog).toHaveCount(0);
    await expect(page.locator(".pane .background-task-trigger")).toHaveText("后台任务 · 1");
    expect(relay.commands.some(c => ["query", "steer", "open_btw", "close_btw"].includes(String(c.type)))).toBe(false);
  });

  test(`side chat scope keeps ${engine} BTW completion read when old history arrives after switching sessions`, async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    const relay = await mockRightPanelRelay(page, { visible: true, engine, secondParent: true });
    const sid = "btw-layout-child";
    const panel = page.locator(".btw-panel");
    const parent = page.locator(".scard").filter({ hasText: "Layout parent" });
    const badge = parent.getByText("BTW 完成", { exact: true });
    const selectSession = async (name: string) => {
      const showSidebar = page.getByRole("button", { name: "显示会话侧栏", exact: true });
      if (await showSidebar.getAttribute("aria-expanded") === "false") await showSidebar.click();
      await page.getByText(name, { exact: true }).click();
    };
    await page.goto("/");
    await expect.poll(() => relay.commands.some(c => c.type === "sync_btw")).toBe(true);
    const terminal = (id: string, seq?: number) => relay.emit({
      type: "turn_end", sid, turn_id: `answer-${id}`, checkpoint_id: `question-${id}`, seq,
      result: { subtype: "success", duration_ms: 1000, is_error: false },
    });
    const waitForDelivery = async (id: string) => {
      // A visible event on the same socket confirms all preceding hidden BTW
      // events were processed before checking that their badge stayed absent.
      relay.emit({ type: "delta", sid: "layout-other", message_id: `barrier-${id}`,
        channel: "final", text: `Events delivered ${id}` });
      await expect(page.getByText(`Events delivered ${id}`, { exact: true })).toBeVisible();
    };
    const replay = () => {
      relay.emit({ type: "replay_start", sid, generation: "layout-generation",
        from_seq: 0, to_seq: 100, rebuild: true, truncated: false });
      for (const id of ["old", "latest"]) {
        relay.emit({ type: "user_msg", sid, msg_id: `question-${id}`, prompt: `BTW question ${id}` });
        relay.emit({ type: "delta", sid, message_id: `answer-${id}`, channel: "final", text: `BTW answer ${id}` });
        terminal(id);
      }
      relay.emit({ type: "replay_end", sid, to_seq: 100, truncated: false });
      relay.emit({ type: "state", sid, state: "idle" });
    };
    // Read the completed conversation, then reproduce a delayed SyncBtw reply
    // arriving only after its parent is no longer the visible session.
    replay();
    await expect(panel.getByText("BTW answer latest", { exact: true })).toBeVisible();
    await expect(badge).toHaveCount(0);
    await selectSession("Second parent");
    await expect(panel).toHaveCount(0);
    replay();
    await waitForDelivery("replayed");
    await expect(badge).toHaveCount(0);
    // Suppression must be exact: a new completion, including one first learned
    // from reconnect history, still needs to notify its hidden parent.
    terminal("missed-while-disconnected");
    await expect(badge).toBeVisible();
    await selectSession("Layout parent");
    await expect(panel).toBeVisible();
    await expect(badge).toHaveCount(0);
    await selectSession("Second parent");
    await expect(panel).toHaveCount(0);
    terminal("missed-while-disconnected");
    await waitForDelivery("repeated");
    await expect(badge).toHaveCount(0);
    terminal("new-live", 101);
    await expect(badge).toBeVisible();
    expect(relay.commands.some(c => ["query", "steer", "open_btw", "close_btw"].includes(String(c.type)))).toBe(false);
  });
}

for (const engine of ["claude", "codex"] as const) {
  for (const pending of [false, true]) {
    test(`history prefetch ${engine} keeps the reading window stable and shares ${pending ? "pending" : "ready"} pages`, async ({ page, isMobile }) => {
      const turns = (start: number, count: number) => Array.from({ length: count }, (_, i) => ({
        id: `prefetch-${start + i}`, prompt: `Question ${start + i}`, done: true,
        blocks: [{ kind: "text" as const, message_id: `answer-${start + i}`,
          channel: "final" as const, done: true,
          text: Array.from({ length: 12 }, (_, p) => `Paragraph ${p}: ${"Reading history should stay in place. ".repeat(5)}`).join("\n\n") }],
      }));
      const base = { type: "history" as const, session_id: "layout-parent", sid: "layout-parent",
        revision: "layout-history", generation: "layout-generation",
        detail: "summary" as const, events: [], has_more: true };
      const relay = await mockRightPanelRelay(page, { engine, retained: false,
        historyReply: command => command.before ? null : {
          ...base, turns: turns(21, 4), oldest_id: "prefetch-21",
        },
      });
      const olderRequests = () => relay.commands.filter(c => c.type === "get_history" && c.before);
      await page.goto("/");
      const thread = page.locator(".pane .thread");
      const scrollUp = async (ratio: number) => {
        if (isMobile) {
          await dispatchTouchPhase(page, "touchstart", 160);
          await dispatchTouchPhase(page, "touchmove", 220);
        } else {
          await thread.dispatchEvent("wheel", { deltaY: -100 });
        }
        await thread.evaluate((el, ratio) => {
          el.scrollTop = (el.scrollHeight - el.clientHeight) * ratio;
          el.dispatchEvent(new Event("scroll"));
        }, ratio);
      };
      await expect(thread.locator('[data-turn-id="prefetch-24"]')).toBeAttached();
      await page.evaluate(() => document.fonts.ready);
      await waitForScrollIdle(page);
      expect(olderRequests()).toHaveLength(0);
      // Layout-driven movement alone must not fetch anything.
      await thread.evaluate(el => {
        el.scrollTop = (el.scrollHeight - el.clientHeight) * 0.65;
        el.dispatchEvent(new Event("scroll"));
      });
      await waitForScrollIdle(page);
      expect(olderRequests()).toHaveLength(0);
      await scrollUp(0.45);
      if (isMobile) await dispatchTouchPhase(page, "touchend", 220);
      await expect.poll(() => olderRequests().length).toBe(1);
      expect(olderRequests()[0]).toMatchObject({ before: "prefetch-21", limit: 12 });
      await waitForScrollIdle(page);
      const before = await thread.evaluate(el => ({ top: el.scrollTop, height: el.scrollHeight }));
      const respond = () => relay.emit({ ...base, before: "prefetch-21",
        turns: turns(9, 12), oldest_id: "prefetch-9" });
      if (!pending) respond();
      await page.waitForTimeout(200);
      const after = await thread.evaluate(el => ({ top: el.scrollTop, height: el.scrollHeight }));
      expect(Math.abs(after.top - before.top)).toBeLessThan(2);
      expect(after.height).toBe(before.height);
      await expect(thread.locator('[data-turn-id="prefetch-20"]')).toHaveCount(0);
      await scrollUp(0);
      const anchorTop = await thread.locator('[data-turn-id="prefetch-21"]').evaluate(el => el.getBoundingClientRect().top);
      if (pending) { await page.waitForTimeout(150); respond(); }
      if (isMobile) await dispatchTouchPhase(page, "touchend", 220);
      await expect(thread.locator('[data-turn-id="prefetch-20"]')).toBeAttached();
      await waitForScrollIdle(page);
      expect(olderRequests()).toHaveLength(1);
      const retainedTop = await thread.locator('[data-turn-id="prefetch-21"]').evaluate(el => el.getBoundingClientRect().top);
      expect(Math.abs(retainedTop - anchorTop)).toBeLessThan(3);
      await page.waitForTimeout(400);
      expect(olderRequests(), "an arriving page must not recursively prefetch history").toHaveLength(1);
      if (!pending) {
        await scrollUp(0.45);
        if (isMobile) await dispatchTouchPhase(page, "touchend", 220);
        await expect.poll(() => olderRequests().length).toBe(2);
        expect(olderRequests()[1]).toMatchObject({ before: "prefetch-9", limit: 12 });
        relay.emit({ ...base, before: "prefetch-9", turns: turns(1, 8),
          oldest_id: "prefetch-1", has_more: false });
        await waitForScrollIdle(page);
        await expect(thread.locator('[data-turn-id="prefetch-8"]')).toHaveCount(0);
        await scrollUp(0);
        const secondAnchor = await thread.locator('[data-turn-id="prefetch-9"]')
          .evaluate(el => el.getBoundingClientRect().top);
        const beforeGeometry = await thread.evaluate(el => ({
          top: el.getBoundingClientRect().top, scroll: el.scrollTop,
          height: el.scrollHeight,
          row: el.querySelector('[data-turn-id="prefetch-9"]')?.getBoundingClientRect().toJSON(),
        }));
        if (isMobile) await dispatchTouchPhase(page, "touchend", 220);
        await expect(thread.locator('[data-turn-id="prefetch-8"]')).toBeAttached();
        await waitForScrollIdle(page);
        const secondTop = await thread.locator('[data-turn-id="prefetch-9"]')
          .evaluate(el => el.getBoundingClientRect().top);
        const afterGeometry = await thread.evaluate(el => ({
          top: el.getBoundingClientRect().top, scroll: el.scrollTop,
          height: el.scrollHeight,
          row: el.querySelector('[data-turn-id="prefetch-9"]')?.getBoundingClientRect().toJSON(),
        }));
        expect(Math.abs(secondTop - secondAnchor), JSON.stringify({ beforeGeometry, afterGeometry, secondAnchor, secondTop })).toBeLessThan(3);
        expect(olderRequests()).toHaveLength(2);
        await expect(thread.getByRole("button", { name: "加载更早的历史", exact: true })).toHaveCount(0);
      }
    });
  }
}

test("side chat scope restores long Claude history atomically at the bottom with a completion spark", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { visible: true, engine: "claude" });
  const sid = "btw-layout-child";
  const panel = page.locator(".btw-panel");
  for (let attempt = 0; attempt < 2; attempt++) {
    const syncs = relay.commands.filter(c => c.type === "sync_btw").length;
    if (attempt) await page.reload();
    else await page.goto("/");
    await expect.poll(() => relay.commands.filter(c => c.type === "sync_btw").length).toBeGreaterThan(syncs);
    await expect(panel.getByRole("textbox")).toBeEnabled();
    relay.emit({ type: "replay_start", sid, generation: "layout-generation",
      from_seq: 0, to_seq: 100, rebuild: true, truncated: false });
    for (let i = 0; i < 20; i++) {
      relay.emit({ type: "user_msg", sid, msg_id: `human-${i}`, prompt: `Question ${i}` });
      relay.emit({ type: "delta", sid, message_id: `answer-${i}`, channel: "final",
        text: `Answer ${i}\n\n` + "Long side conversation paragraph.\n\n".repeat(15) });
      relay.emit({ type: "assistant_msg_end", sid, message_id: `answer-${i}`, channel: "final" });
      relay.emit({ type: "turn_end", sid, turn_id: `answer-${i}`, checkpoint_id: `human-${i}`,
        result: { subtype: "success", duration_ms: 1000, is_error: false } });
      if (i % 5 === 0) {
        await expect(panel.getByText("正在恢复侧边对话…")).toBeVisible();
        await expect(panel.locator(".thread")).toHaveCount(0);
      }
    }
    relay.emit({ type: "replay_end", sid, to_seq: 100, truncated: false });
    relay.emit({ type: "state", sid, state: "idle" });
    await expect(panel.getByText("正在恢复侧边对话…")).toHaveCount(0);
    const thread = panel.locator(".thread");
    await expect(thread).toBeVisible();
    await expect.poll(() => thread.evaluate(el =>
      el.scrollHeight - el.clientHeight - el.scrollTop)).toBeLessThan(3);
    await expect(panel.locator(".turn-done-mark")).toBeVisible();
    await expect(panel.locator(".turn-done-mark .engine-brand-icon.claude use")).toHaveAttribute("href", /#claude$/);
    await expect(panel.getByRole("button", { name: "Claude 已完成", exact: true })).toBeVisible();
  }
  expect(relay.commands.some(c => ["query", "steer", "open_btw"].includes(String(c.type)))).toBe(false);
});

function newChatSkillResponse(command: Record<string, unknown>, name: string, errors: string[] = []): PanelRelayEvent {
  return { type: "engine_capabilities", request_id: String(command.cmd_id),
    engine: "codex", space: "code", cwd: String(command.cwd ?? "/tmp/layout"),
    codex_profile_id: command.codex_profile_id as string | undefined,
    skills_only: true, errors,
    items: [{ id: name, name, kind: "skill", enabled: true, description: "Project skill" }] };
}

for (const engine of ["claude", "codex"] as const) {
  test(`new-chat controls open ${engine} extension catalogs before creating a session`, async ({ page }) => {
    const relay = await mockRightPanelRelay(page, { engine, retained: false,
      capabilityReply: command => ({ type: "engine_capabilities",
        request_id: String(command.cmd_id), engine, space: "code",
        cwd: String(command.cwd), skills_only: !!command.skills_only,
        items: [{ id: "available", name: "Available skill", kind: "skill" }],
      }),
    });
    await page.goto("/");
    await page.getByLabel("显示会话侧栏", { exact: true }).click();
    await page.getByRole("button", { name: "新会话", exact: true }).click();
    const input = page.locator(".newchat-input");
    const sheet = page.getByRole("dialog", { name: "Extensions", exact: true });
    for (const [slash, label] of [
      ["extensions", "全部"], ["skills", "Skills"], ["plugins", "Plugins"],
      ["apps", "Apps"], ["mcp", "MCP"], ["hooks", "Hooks"],
    ]) {
      // Cover both palette selection and submitting a complete command.
      await input.fill(`/${slash}`);
      if (slash === "skills" || slash === "mcp") {
        await page.getByRole("listbox", { name: "命令" }).getByRole("button").first().click();
      } else await page.getByRole("button", { name: "开始", exact: true }).click();
      await expect(sheet).toBeVisible();
      await expect(sheet.locator(".capabilities-tabs .active")).toHaveText(label);
      await expect(sheet).not.toContainText("正在读取扩展目录");
      if (slash === "skills" || slash === "extensions") await expect(sheet).toContainText("Available skill");
      await expect(input).toHaveValue("");
      await sheet.getByRole("button", { name: "关闭", exact: true }).click();
    }
    expect(relay.commands.filter(c => c.type === "get_engine_capabilities" && !c.skills_only)).toHaveLength(6);
    expect(relay.commands.some(c => ["new_session", "query", "steer"].includes(String(c.type)))).toBe(false);
  });

  test(`new-chat controls keep ${engine} extension reads and edits in the selected account and directory`, async ({ page }) => {
    const profileField = engine === "claude" ? "claude_profile_id" : "codex_profile_id";
    const response = (command: Record<string, unknown>, name: string): PanelRelayEvent => ({
      type: "engine_capabilities", request_id: String(command.cmd_id), engine, space: "code",
      cwd: String(command.cwd), skills_only: !!command.skills_only,
      [profileField]: command[profileField],
      items: [{ id: name, name, kind: "skill" }],
    });
    let hold = false;
    const relay = await mockRightPanelRelay(page, { engine, retained: false,
      listReply: command => ({ type: "session_list", engine, space: "code",
        request_id: String(command.cmd_id),
        [`${engine}_profiles`]: [{ id: "default", label: "Primary" }, { id: "secondary", label: "Secondary" }],
        [`default_${engine}_profile_id`]: "default",
        sessions: [{ session_id: "layout-parent", engine, space: "code", cwd: "/tmp/layout",
          summary: "Layout parent", state: "idle", last_modified: "100", [profileField]: "default" }],
      }),
      capabilityReply: command => hold ? undefined : response(command, "Original skill"),
    });
    await page.goto("/");
    await page.getByLabel("显示会话侧栏", { exact: true }).click();
    await page.getByRole("button", { name: "新会话", exact: true }).click();
    const input = page.locator(".newchat-input");
    const sheet = page.getByRole("dialog", { name: "Extensions", exact: true });
    const reads = () => relay.commands.filter(c => c.type === "get_engine_capabilities" && !c.skills_only);
    hold = true;
    await input.fill("/skills");
    await input.press("Enter");
    await expect.poll(() => reads().length).toBe(1);
    const oldRead = reads()[0];
    await expect(sheet).toContainText("正在读取扩展目录");
    await sheet.getByRole("button", { name: "关闭", exact: true }).click();
    await page.locator(".newchat-profile").click();
    await page.getByRole("dialog", { name: `选择 ${engine === "claude" ? "Claude" : "Codex"} 账号` })
      .getByRole("button", { name: /Secondary/ }).click();
    await page.getByTitle("更改工作目录", { exact: true }).click();
    const directory = page.getByRole("dialog", { name: "选择本地工作区" });
    await directory.getByPlaceholder("或粘贴绝对路径…").fill("/tmp/selected-project");
    await directory.getByRole("button", { name: "在此创建" }).click();
    await expect(directory).not.toBeVisible();
    await expect(page.getByTitle("更改工作目录", { exact: true })).toContainText("/tmp/selected-project");
    await input.fill("/skills");
    await input.press("Enter");
    // A delayed response from the old account must not populate the new sheet.
    relay.emit(response(oldRead, "Stale skill"));
    await expect.poll(() => reads().length).toBe(2);
    const selectedRead = reads()[1];
    expect(selectedRead).toMatchObject({ engine, space: "code", cwd: "/tmp/selected-project", [profileField]: "secondary" });
    await expect(sheet).not.toContainText("Stale skill");
    relay.emit(response(selectedRead, "Selected skill"));
    await expect(sheet).toContainText("Selected skill");
    await expect(sheet).not.toContainText("正在读取扩展目录");
    relay.emit({ type: "wrapper_reconnected", state: "idle", generation: "extension-generation" });
    await expect.poll(() => reads().length).toBe(3);
    expect(reads()[2]).toMatchObject({ cwd: "/tmp/selected-project", [profileField]: "secondary", skills_only: false });
    relay.emit(response(reads()[2], "Refreshed skill"));
    await expect(sheet).toContainText("Refreshed skill");
    await sheet.getByRole("button", { name: "+ 新建 Skill", exact: true }).click();
    await sheet.getByPlaceholder("Skill 名称").fill("new-skill");
    await sheet.getByPlaceholder("何时使用，以及应该如何执行…").fill("Review files when requested.");
    await sheet.getByRole("button", { name: "创建", exact: true }).click();
    await expect.poll(() => relay.commands.filter(c => c.type === "manage_engine_skill")).toEqual([
      expect.objectContaining({ engine, space: "code", cwd: "/tmp/selected-project",
        [profileField]: "secondary", action: "create", name: "new-skill" }),
    ]);
    expect(relay.commands.some(c => ["new_session", "query", "steer"].includes(String(c.type)))).toBe(false);
  });
}

for (const failure of ["response", "error"] as const) {
  test(`new-chat controls retry skill discovery after a ${failure} failure without editing the draft`, async ({ page }) => {
    let hold = false;
    const relay = await mockRightPanelRelay(page, { retained: false,
      capabilityReply: command => hold ? undefined : newChatSkillResponse(command, "warm"),
    });
    await page.goto("/");
    await page.getByLabel("显示会话侧栏", { exact: true }).click();
    await page.getByRole("button", { name: "新会话", exact: true }).click();
    const reads = () => relay.commands.filter(command => command.type === "get_engine_capabilities");
    hold = true;
    const before = reads().length;
    const input = page.locator(".newchat-input");
    await input.fill("$rev");
    await expect.poll(() => reads().length).toBeGreaterThan(before);
    const first = reads().at(-1)!;
    if (failure === "response") relay.emit(newChatSkillResponse(first, "", ["skills: unavailable"]));
    else relay.emit({ type: "error", request_id: String(first.cmd_id), code: "discovery_failed", message: "unavailable" });
    const retry = page.getByRole("button", { name: "重试读取 Skills", exact: true });
    await expect(retry).toBeVisible();
    const failedCount = reads().length;
    await input.fill("$revi");
    await expect(retry).toBeVisible();
    expect(reads().length).toBe(failedCount);
    await retry.click();
    await expect.poll(() => reads().length).toBe(failedCount + 1);
    relay.emit(newChatSkillResponse(reads().at(-1)!, "review"));
    await expect(page.getByRole("listbox", { name: "Skills" })).toContainText("$review");
    await expect(input).toHaveValue("$revi");
    await expect(retry).toHaveCount(0);
    expect(relay.commands.some(command => command.type === "new_session")).toBe(false);
  });
}

for (const loaded of [false, true]) {
  test(`new-chat controls recover ${loaded ? "loaded" : "pending"} skills after wrapper reconnect`, async ({ page }) => {
    let hold = false;
    const relay = await mockRightPanelRelay(page, { retained: false,
      capabilityReply: command => hold ? undefined : newChatSkillResponse(command, "warm"),
    });
    await page.goto("/");
    await page.getByLabel("显示会话侧栏", { exact: true }).click();
    await page.getByRole("button", { name: "新会话", exact: true }).click();
    hold = true;
    const reads = () => relay.commands.filter(command => command.type === "get_engine_capabilities");
    const before = reads().length;
    const input = page.locator(".newchat-input");
    await input.fill("$rev");
    await expect.poll(() => reads().length).toBeGreaterThan(before);
    const first = reads().at(-1)!;
    if (loaded) {
      relay.emit(newChatSkillResponse(first, "review-old"));
      await expect(page.getByRole("listbox", { name: "Skills" })).toContainText("$review-old");
    }
    const pendingCount = reads().length;
    relay.emit({ type: "wrapper_reconnected", state: "idle", generation: "replacement-generation" });
    await expect.poll(() => reads().length).toBeGreaterThan(pendingCount);
    // A delayed pre-reconnect result must not satisfy the replacement read.
    relay.emit(newChatSkillResponse(first, "review-stale"));
    for (let attempt = 0; attempt < 2; attempt++) {
      const next = reads().at(-1)!;
      relay.emit(newChatSkillResponse(next, "review-restored"));
      // The coordinator may first drain a different focused-directory read.
      await expect.poll(async () => (await page.getByRole("listbox", { name: "Skills" }).innerText()).includes("$review-restored")
        || reads().at(-1) !== next).toBe(true);
    }
    await expect(page.getByRole("listbox", { name: "Skills" })).toContainText("$review-restored");
    await expect(page.getByRole("listbox", { name: "Skills" })).not.toContainText("review-stale");
    await expect(input).toHaveValue("$rev");
    expect(relay.commands.some(command => command.type === "new_session")).toBe(false);
  });
}

for (const engine of ["claude", "codex"] as const) {
  test(`side chat scope reopens ${engine} history without a blank frame during sync`, async ({ page }) => {
    const relay = await mockRightPanelRelay(page, { visible: true, engine });
    const sid = "btw-layout-child";
    const panel = page.locator(".btw-panel");
    await page.goto("/");
    await expect.poll(() => relay.commands.some(c => c.type === "sync_btw")).toBe(true);
    const replayStart = () => relay.emit({ type: "replay_start", sid,
      generation: "layout-generation", from_seq: 0, to_seq: 100, rebuild: true, truncated: false });
    const history = (text: string) => {
      relay.emit({ type: "user_msg", sid, msg_id: "retained-question", prompt: "保留的侧聊问题" });
      relay.emit({ type: "delta", sid, message_id: "retained-answer", channel: "final", text });
      relay.emit({ type: "assistant_msg_end", sid, message_id: "retained-answer", channel: "final" });
      relay.emit({ type: "turn_end", sid, turn_id: "retained-answer", checkpoint_id: "retained-question",
        result: { subtype: "success", duration_ms: 1000, is_error: false } });
    };
    replayStart();
    history("完整的旧回答");
    relay.emit({ type: "replay_end", sid, to_seq: 100, truncated: false });
    relay.emit({ type: "state", sid, state: "idle" });
    await expect(panel.getByText("完整的旧回答", { exact: true })).toBeVisible();
    for (const centered of [false, true]) {
      await panel.getByRole("button", { name: "收起侧边对话", exact: true }).click();
      await expect(panel).toHaveCount(0);
      const syncs = relay.commands.filter(c => c.type === "sync_btw").length;
      await page.keyboard.press(centered ? "Control+Shift+e" : "Control+Shift+k");
      await expect.poll(() => relay.commands.filter(c => c.type === "sync_btw").length).toBeGreaterThan(syncs);
      await expect(panel.getByText("完整的旧回答", { exact: true })).toBeVisible();
      const thread = await panel.locator(".thread").elementHandle();
      replayStart();
      await expect(panel.getByText("完整的旧回答", { exact: true })).toBeVisible();
      await expect(panel.locator(".btw-chat-label")).toHaveText("保留的侧聊问题");
      await expect(panel.getByText("正在恢复侧边对话…")).toHaveCount(0);
      history("正在同步的新回答");
      // A slow replay must retain the complete old projection, not its prefix.
      await expect(panel.getByText("完整的旧回答", { exact: true })).toBeVisible();
      await expect(panel.getByText("正在同步的新回答", { exact: true })).toHaveCount(0);
      expect(await thread!.evaluate(node => node.isConnected)).toBe(true);
      relay.emit({ type: "replay_end", sid, to_seq: 100, truncated: false });
      await expect(panel.getByText("正在同步的新回答", { exact: true })).toBeVisible();
      expect(await thread!.evaluate(node => node.isConnected)).toBe(true);
      await expect(panel.locator(".turn-done-mark")).toHaveCount(1);
      replayStart();
      history("完整的旧回答");
      relay.emit({ type: "replay_end", sid, to_seq: 100, truncated: false });
      await expect(panel.getByText("完整的旧回答", { exact: true })).toBeVisible();
    }
    expect(relay.commands.some(c => ["query", "steer", "open_btw", "close_btw"].includes(String(c.type)))).toBe(false);
  });
}

test("side chat scope keeps Claude's running spark on a successor after replay", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { visible: true, engine: "claude" });
  const sid = "btw-layout-child";
  const panel = page.locator(".btw-panel");
  await page.goto("/");
  await expect.poll(() => relay.commands.some(c => c.type === "sync_btw")).toBe(true);
  const replay = (second = false) => {
    relay.emit({ type: "replay_start", sid, generation: "layout-generation",
      from_seq: 0, to_seq: second ? 200 : 100, rebuild: true, truncated: false });
    relay.emit({ type: "user_msg", sid, msg_id: "first", prompt: "first question" });
    relay.emit({ type: "delta", sid, message_id: "first-answer", turn_id: "first",
      channel: "final", text: "first answer" });
    if (second) {
      relay.emit({ type: "turn_end", sid, turn_id: "first-answer", checkpoint_id: "native-first",
        result: { subtype: "success", duration_ms: 1000, is_error: false } });
      relay.emit({ type: "user_msg", sid, msg_id: "second", prompt: "second question" });
      relay.emit({ type: "delta", sid, message_id: "second-thinking", turn_id: "second",
        channel: "thinking", text: "second thinking" });
    }
    relay.emit({ type: "state", sid, state: "running", msg_id: second ? "second" : "first" });
    relay.emit({ type: "replay_end", sid, to_seq: second ? 200 : 100, truncated: false });
  };
  replay();
  await expect(panel.locator(".turn-working")).toHaveCount(1);
  await panel.getByRole("button", { name: "收起侧边对话", exact: true }).click();
  await page.keyboard.press("Control+Shift+e");
  await expect(panel).toBeVisible();
  replay();
  await expect(panel.locator(".turn-working")).toHaveCount(1);
  relay.emit({ type: "turn_end", sid, seq: 101, turn_id: "first-answer", checkpoint_id: "native-first",
    result: { subtype: "success", duration_ms: 1000, is_error: false } });
  relay.emit({ type: "state", sid, seq: 102, state: "idle" });
  await expect(panel.locator(".turn-working")).toHaveCount(0);
  await panel.locator("textarea").fill("second question");
  await panel.locator("textarea").press("Enter");
  await expect.poll(() => relay.commands.some(c => c.type === "query")).toBe(true);
  const sent = relay.commands.findLast(c => c.type === "query")!;
  relay.emit({ type: "state", sid, seq: 103, state: "running" });
  relay.emit({ type: "user_msg", sid, seq: 104, msg_id: String(sent.msg_id), prompt: "second question" });
  relay.emit({ type: "delta", sid, seq: 105, message_id: "second-thinking", turn_id: String(sent.msg_id),
    channel: "thinking", text: "second thinking" });
  await expect(panel.locator(".turn").filter({ hasText: "second question" }).locator(".turn-working")).toHaveCount(1);
  await expect(panel.locator(".turn").filter({ hasText: "first question" }).locator(".turn-working")).toHaveCount(0);
  replay(true);
  await expect(panel.locator(".turn").filter({ hasText: "second question" }).locator(".turn-working")).toHaveCount(1);
});

test("side chat scope keeps Claude's running spark after hide, reopen and cold replay", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { visible: true, engine: "claude" });
  const sid = "btw-layout-child";
  const panel = page.locator(".btw-panel");
  await page.goto("/");
  await expect.poll(() => relay.commands.some(c => c.type === "sync_btw")).toBe(true);
  relay.emit({ type: "user_msg", sid, msg_id: "active-question", prompt: "正在执行的侧聊", seq: 1 });
  relay.emit({ type: "state", sid, state: "running", msg_id: "active-question", seq: 2 });
  relay.emit({ type: "delta", sid, message_id: "active-answer", channel: "commentary",
    text: "正在检查文件", seq: 3 });
  await expect(panel.locator(".turn-working")).toHaveCount(1);
  const replay = async (finished = false) => {
    relay.emit({ type: "replay_start", sid, generation: "layout-generation",
      from_seq: 0, to_seq: 10, rebuild: true, truncated: false });
    relay.emit({ type: "user_msg", sid, msg_id: "active-question", prompt: "正在执行的侧聊" });
    relay.emit({ type: "delta", sid, message_id: "active-answer", channel: "commentary",
      text: "正在检查文件" });
    if (finished) {
      relay.emit({ type: "turn_end", sid, turn_id: "active-answer", checkpoint_id: "active-question",
        result: { subtype: "success", duration_ms: 2000, is_error: false } });
    }
    relay.emit({ type: "state", sid, state: finished ? "idle" : "running",
      msg_id: finished ? null : "active-question" });
    relay.emit({ type: "replay_end", sid, to_seq: 10, truncated: false });
    if (!finished) await expect(panel.getByText("正在检查文件", { exact: true })).toBeVisible();
    await expect(panel.locator(".turn-working")).toHaveCount(finished ? 0 : 1);
    if (finished) await expect(panel.locator(".turn-done-mark")).toHaveCount(1);
  };
  for (const centered of [false, true]) {
    await panel.getByRole("button", { name: "收起侧边对话", exact: true }).click();
    await expect(panel).toHaveCount(0);
    const syncs = relay.commands.filter(c => c.type === "sync_btw").length;
    await page.keyboard.press(centered ? "Control+Shift+e" : "Control+Shift+k");
    await expect.poll(() => relay.commands.filter(c => c.type === "sync_btw").length).toBeGreaterThan(syncs);
    await replay();
    // No new model token is needed to bring back the working indicator.
    await expect(panel.locator(".turn-working")).toBeVisible();
  }
  const syncs = relay.commands.filter(c => c.type === "sync_btw").length;
  await page.reload();
  await expect.poll(() => relay.commands.filter(c => c.type === "sync_btw").length).toBeGreaterThan(syncs);
  await replay();
  await panel.getByRole("button", { name: "收起侧边对话", exact: true }).click();
  await expect(panel).toHaveCount(0);
  const finalSyncs = relay.commands.filter(c => c.type === "sync_btw").length;
  await page.keyboard.press("Control+Shift+k");
  await expect.poll(() => relay.commands.filter(c => c.type === "sync_btw").length).toBeGreaterThan(finalSyncs);
  await replay(true);
  expect(relay.commands.some(c => ["query", "steer", "open_btw", "close_btw"].includes(String(c.type)))).toBe(false);
});

for (const running of [false, true]) {
  test(`path-prefixed prompts send verbatim as ${running ? "steering" : "a new message"}`, async ({ page }) => {
    const relay = await mockRightPanelRelay(page, { retained: false });
    await page.goto("/");
    const input = page.locator(".composer textarea");
    await expect(input).toBeVisible();
    await expect.poll(() => relay.commands.some((command) =>
      command.type === "get_history" && command.session_id === "layout-parent",
    )).toBe(true);
    if (running) {
      relay.emit({ type: "state", sid: "layout-parent", state: "running" });
      await expect(input).toHaveAttribute("placeholder", /补充当前任务/);
    }
    const prompt = "/Users/Tester/workspace/unitree-go2这里有双目的，看看我们的大脑是否能接入？";
    await input.fill(prompt);
    if (running) await input.press("Enter");
    else await page.locator(".composer .sendbtn").click();
    await expect.poll(() => relay.commands.filter((command) =>
      command.type === (running ? "steer" : "query") && command.prompt === prompt,
    ).length).toBe(1);
    await expect(input).toHaveValue("");
    await expect(page.locator(".composer-notice")).toHaveCount(0);
    expect(relay.commands.some((command) => command.type === "browse_files")).toBe(false);
  });
}

for (const side of [false, true]) {
  test(`side chat scope Claude native steering keeps output and queue separate (${side ? "BTW" : "main"})`, async ({ page }) => {
    const relay = await mockRightPanelRelay(page, {
      engine: "claude", retained: side, visible: side, parentState: "running",
    });
    await page.goto("/");
    const sid = side ? "btw-layout-child" : "layout-parent";
    const surface = page.locator(side ? ".btw-panel" : ".composer");
    const input = surface.locator("textarea");
    await expect(input).toBeVisible();
    await expect(surface.getByRole("button", { name: "引导", exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "打断并发送", exact: true })).toHaveCount(0);
    relay.emit({ type: "user_msg", sid, msg_id: "claude-root", prompt: "检查输入" });
    relay.emit({ type: "state", sid, state: "running", msg_id: "claude-root" });
    relay.emit({ type: "assistant_msg_start", sid, message_id: "claude-old-output",
      turn_id: "claude-root", channel: "commentary" });
    relay.emit({ type: "delta", sid, message_id: "claude-old-output",
      turn_id: "claude-root", channel: "commentary", text: "原任务输出" });
    await input.fill("补充检查网络");
    await input.press("Enter");
    await expect.poll(() => relay.commands.filter(c => c.type === "steer").length).toBe(1);
    const command = relay.commands.find(c => c.type === "steer")!;
    expect(command.sid).toBe(sid);
    await expect(input).toHaveValue("");
    // Native next waits for its input boundary; old deltas still belong to the
    // old row while command acceptance and user echo are separate events.
    relay.emit({ type: "delta", sid, message_id: "claude-old-output",
      turn_id: "claude-root", channel: "commentary", text: "继续完整显示。" });
    relay.emit({ type: "assistant_msg_end", sid, message_id: "claude-old-output",
      turn_id: "claude-root", channel: "commentary" });
    relay.emit({ type: "turn_steered", sid, msg_id: String(command.msg_id),
      turn_id: "claude-native-guide", prompt: String(command.prompt) });
    relay.emit({ type: "assistant_msg_start", sid, message_id: "claude-guided-output",
      turn_id: String(command.msg_id), channel: "final" });
    relay.emit({ type: "delta", sid, message_id: "claude-guided-output",
      turn_id: String(command.msg_id), channel: "final", text: "已经收到网络检查引导。" });
    await expect(page.getByText("已经收到网络检查引导。", { exact: true })).toBeVisible();
    if (!side) {
      const original = page.locator('.turn[data-turn-id="claude-root"]');
      await original.locator(".turn-process-head").click();
      await expect(original)
        .toContainText("原任务输出继续完整显示。");
      await expect(page.locator(`.turn[data-turn-id="${String(command.msg_id)}"]`))
        .not.toContainText("原任务输出");
    }
    await input.fill("完成后再检查磁盘");
    await input.press("Tab");
    await expect.poll(() => relay.commands.filter(c => c.type === "query"
      && c.delivery === "queue" && c.sid === sid).length).toBe(1);
    expect(relay.commands.filter(c => c.type === "interrupt")).toHaveLength(0);
    expect(relay.commands.filter(c => c.type === "steer")).toHaveLength(1);
  });
}

for (const browsing of [false, true]) {
  test(`turn regressions App keeps painted history on post-send alias revision (${browsing ? "browsing" : "live"})`, async ({ page }, testInfo) => {
    const sid = "layout-parent";
    const oldTurns: NonNullable<Extract<ServerEvent, { type: "history" }>["turns"]> = Array.from({ length: 4 }, (_, i) => ({
      id: `old-${i}`, prompt: `old question ${i}`, done: true, durationMs: 71_000,
      ts: 1000 + i * 2000, doneTs: 2000 + i * 2000,
      blocks: [{ kind: "text", message_id: `answer-${i}`, done: true, channel: "final",
        text: `Retained answer ${i}\n\n${"Content already shown must not disappear. ".repeat(18)}` }],
    }));
    let head: PanelRelayEvent<Extract<ServerEvent, { type: "history" }>> = {
      type: "history", sid, session_id: sid, revision: "layout-history",
      continuity_revision: "layout-history", generation: "layout-generation",
      build_seq: 1, live_seq: 0, detail: "summary", events: [], turns: oldTurns,
      has_more: true, oldest_id: "old-0", newest_id: "old-3", in_progress: false,
    };
    let olderReads = 0;
    const relay = await mockRightPanelRelay(page, { retained: false, historyReply: (command) => {
      if (!command.before) return { ...head, request_id: String(command.cmd_id) };
      olderReads++;
      if (olderReads === 2) return null; // hold a stale in-flight pagination request
      const turnId = olderReads === 1 ? "older-page" : "earlier-page";
      return { ...head, before: String(command.before), request_id: String(command.cmd_id),
        turns: [{ ...oldTurns[0], id: turnId, prompt: `${turnId} question` }],
        oldest_id: turnId, newest_id: turnId, has_more: olderReads === 1 };
    } });
    await page.goto("/");
    const thread = page.locator(".thread");
    await expect(page.locator('[data-turn-id="old-3"]')).toBeAttached();
    if (browsing) {
      await scrollThreadToEdge(page, "start", testInfo.project.name);
      await expect(page.locator('[data-turn-id="older-page"]')).toBeAttached();
      // The prepend keeps the original row anchored, so the header can be
      // above the viewport again. Start a fresh scroll gesture for page two.
      if (olderReads < 2) await scrollThreadToEdge(page, "start", testInfo.project.name);
      await expect.poll(() => olderReads).toBe(2);
    }
    // Read mid-history, safely away from the top-trigger and the live edge.
    if (isMobileWebKitProject(testInfo.project.name)) await dispatchTouchGesture(page, browsing ? -60 : 60);
    else await thread.dispatchEvent("wheel", { deltaY: browsing ? 100 : -100 });
    await thread.evaluate((node) => {
      node.scrollTop = 500;
      node.dispatchEvent(new Event("scroll"));
    });
    await waitForScrollIdle(page);
    const anchorId = browsing ? "older-page" : "old-0";
    const anchor = page.locator(`[data-turn-id="${anchorId}"]`);
    await expect(anchor).toBeAttached();
    await page.locator(".composer textarea").fill("new question after history");
    const beforeSend = await thread.evaluate((node) => node.scrollTop);
    await page.locator(".composer").getByRole("button", { name: "发送", exact: true }).click();
    await expect.poll(() => relay.commands.some((c) => c.type === "query")).toBe(true);
    const query = relay.commands.find((c) => c.type === "query")!;
    relay.emit({ type: "user_msg", sid, seq: 1, msg_id: "native-new",
      client_msg_id: String(query.msg_id), prompt: "new question after history" });
    relay.emit({ type: "state", sid, seq: 2, state: "running", msg_id: "native-new" });
    head = { ...head, revision: "layout-alias-1", build_seq: 2, live_seq: 2, in_progress: true,
      turns: [{ id: "native-new", clientMsgId: String(query.msg_id), prompt: "new question after history",
        blocks: [], done: false, ts: Date.now() }], has_more: true, oldest_id: "native-new", newest_id: "native-new" };
    relay.emit(head);
    await expect(anchor).toBeAttached();
    await page.waitForTimeout(250);
    expect(Math.abs(await thread.evaluate((node) => node.scrollTop) - beforeSend)).toBeLessThan(4);
    await expect(thread).toContainText("Retained answer");

    const pending = relay.commands.filter((command) => command.type === "get_history" && command.before).at(-1);
    if (browsing) {
      expect(pending).toBeDefined();
      // A late old-version response must not exit the preserved browse window.
      relay.emit({ ...head, revision: "layout-history", request_id: String(pending!.cmd_id),
        before: String(pending!.before), turns: [], authoritative: false, error: "stale read" });
      await page.waitForTimeout(100);
      await expect(anchor).toBeAttached();
      expect(Math.abs(await thread.evaluate((node) => node.scrollTop) - beforeSend)).toBeLessThan(4);
      await scrollThreadToEdge(page, "start", testInfo.project.name);
      await expect(page.locator('[data-turn-id="earlier-page"]')).toBeAttached();
      const latestRead = relay.commands.filter((command) => command.type === "get_history" && command.before).at(-1)!;
      expect(latestRead.before).toBe("older-page");
    }
    if (!browsing) {
      await page.getByRole("button", { name: "滚动到底部", exact: true }).click();
      await expect(page.getByText("new question after history", { exact: true })).toBeVisible();
      await expect.poll(() => thread.evaluate((node) => node.scrollHeight - node.clientHeight - node.scrollTop)).toBeLessThan(4);
    }
  });
}

test("session workspace harness menu clears pointer focus and preserves keyboard navigation", async ({ page }) => {
  await mockRightPanelRelay(page, { retained: false });
  await page.goto("/");
  const select = page.locator(".engine-toggle");
  await select.click();
  await page.getByRole("menuitemradio", { name: "Claude", exact: true }).click();
  await expect(select).not.toBeFocused();
  await expect(select).toHaveCSS("outline-style", "none");
  await expect(page.locator(".engine-label")).toHaveText("Claude");
  await select.click();
  await expect(page.getByRole("menu", { name: "会话引擎" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu", { name: "会话引擎" })).toHaveCount(0);
  await expect(select).toBeFocused();
  await select.focus();
  await select.press("ArrowDown");
  const selected = page.getByRole("menuitemradio", { name: "Claude", exact: true });
  await expect(selected).toBeFocused();
  await selected.dispatchEvent("keydown", { key: "Escape", isComposing: true });
  await expect(selected).toBeFocused();
  await selected.dispatchEvent("keydown", { key: "Escape", keyCode: 229 });
  await expect(selected).toBeFocused();
  await selected.press("ArrowDown");
  await expect(page.getByRole("menuitemradio", { name: "Codex", exact: true })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(select).toBeFocused();
  await expect(select).toHaveCSS("outline-style", "solid");
  await expect(page.locator(".engine-label")).toHaveText("Codex");
  await select.press("ArrowUp");
  await page.keyboard.press("Home");
  await expect(page.getByRole("menuitemradio", { name: "Claude", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(select).toBeFocused();
  await expect(page.getByRole("menu", { name: "会话引擎" })).toHaveCount(0);
});

test("session workspace directory links open the browser and keep original downloads", async ({ page }) => {
  const path = "/tmp/layout/dir";
  const relay = await mockRightPanelRelay(page, { retained: false, seedTurns: [{
    id: "directory-link", prompt: "查看目录", done: true,
    blocks: [{ kind: "text", message_id: "directory-answer", channel: "final", done: true,
      text: `[目录](${path})` }],
  }] });
  await page.goto("/");
  await page.getByRole("button", { name: `在 Remote 中打开 ${path}`, exact: true }).click();
  await expect.poll(() => relay.commands.some(c => c.type === "get_file_preview")).toBe(true);
  const request = relay.commands.find(c => c.type === "get_file_preview")!;
  relay.emit({ type: "file_preview", sid: "layout-parent", request_id: String(request.request_id),
    path, directory: true, format: "text", content: "", size: 0 });
  await expect.poll(() => relay.commands.some(c => c.type === "browse_files")).toBe(true);
  const browse = relay.commands.find(c => c.type === "browse_files")!;
  expect(browse.path).toBe(path);
  expect(browse.sid).toBe("layout-parent");
  relay.emit({ type: "files_listed", sid: "layout-parent", request_id: String(browse.request_id),
    root: "/tmp/layout", path, parent: "/tmp/layout", kind: "directory", revision: "dir-1",
    next_offset: null, entries: [{ name: "README.md", path: `${path}/README.md`, kind: "file" }] });
  await expect(page.locator(".file-browser-shell")).toBeVisible();
  await page.getByRole("button", { name: "README.md", exact: true }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "get_file_preview").length).toBe(2);
  const preview = relay.commands.filter(c => c.type === "get_file_preview").at(-1)!;
  relay.emit({ type: "file_preview", sid: "layout-parent", request_id: String(preview.request_id),
    path: `${path}/README.md`, format: "text", content: "Directory preview", size: 17, writable: false });
  await expect(page.locator(".artifact-panel")).toContainText("Directory preview");
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("link", { name: "下载原文件", exact: true }).click();
  expect((await downloadEvent).suggestedFilename()).toBe("README.md");
  await page.getByRole("button", { name: "返回目录", exact: true }).click();
  await expect(page.getByRole("button", { name: "README.md", exact: true })).toBeVisible();
  expect(relay.commands.some(c => ["query", "steer", "new_session"].includes(String(c.type)))).toBe(false);
});

test("session workspace XLSX previews sheets and downloads the original on mobile", async ({ page }, info) => {
  const path = "/tmp/layout/report.xlsx";
  const relay = await mockRightPanelRelay(page, { retained: false, seedTurns: [{
    id: "spreadsheet-link", prompt: "查看表格", done: true,
    blocks: [{ kind: "text", message_id: "spreadsheet-answer", channel: "final", done: true,
      text: `[表格](${path})` }],
  }] });
  await page.goto("/");
  await page.getByRole("button", { name: `在 Remote 中打开 ${path}`, exact: true }).click();
  await expect.poll(() => relay.commands.some(c => c.type === "get_file_preview")).toBe(true);
  const request = relay.commands.find(c => c.type === "get_file_preview")!;
  relay.emit({ type: "file_preview", sid: "layout-parent", request_id: String(request.request_id),
    path, format: "spreadsheet", content: JSON.stringify({ sheets: [
      { name: "路线图", cells: [{ r: 1, c: 1, v: "本周进展" }, { r: 1, c: 2, v: "42", f: "SUM(B2:B3)" }], truncated: false },
      { name: "进展", cells: [{ r: 1, c: 1, v: "<script>bad()</script>" }], truncated: false }], truncated: false }),
    data: "UEsDBA==", size: 4, mtime_ns: "1", truncated: false });
  const sheet = page.getByRole("region", { name: "表格预览" });
  await expect(sheet).toContainText("本周进展");
  await expect(sheet.getByRole("cell", { name: "42", exact: true })).toHaveAttribute("title", "=SUM(B2:B3)");
  await sheet.getByRole("tab", { name: "进展", exact: true }).click();
  await expect(sheet).toContainText("<script>bad()</script>");
  await expect(sheet.locator("script")).toHaveCount(0);
  const downloadEvent = page.waitForEvent("download");
  await sheet.getByRole("link", { name: "下载原文件" }).click();
  expect((await downloadEvent).suggestedFilename()).toBe("report.xlsx");
  await page.screenshot({ path: info.outputPath("spreadsheet-preview.png") });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
});

test("session workspace opens files in the sidebar and returns to its directory", async ({ page }, testInfo) => {
  if (testInfo.project.name === "chromium") await page.setViewportSize({ width: 1440, height: 950 });
  const relay = await mockRightPanelRelay(page, { retained: false });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "更多设置", exact: true })).toBeVisible();
  await expect(page.locator(".c-head").getByRole("button", { name: "打开会话文件", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  await page.getByRole("button", { name: "打开会话文件", exact: true }).click();
  await expect.poll(() => relay.commands.filter((c) => c.type === "browse_files").length).toBe(1);
  const request = relay.commands.find((c) => c.type === "browse_files")!;
  expect(request.sid).toBe("layout-parent");
  const result: PanelRelayEvent<Extract<ServerEvent, { type: "files_listed" }>> = {
    type: "files_listed", sid: "layout-parent", request_id: String(request.request_id),
    root: "/tmp/layout", path: "/tmp/layout", kind: "directory", parent: null,
    revision: "one", next_offset: null, entries: [{ name: "README.md", path: "/tmp/layout/README.md", kind: "file" }],
  };
  relay.emit({ ...result, request_id: "stale-request" });
  await expect(page.getByRole("button", { name: "README.md", exact: true })).toHaveCount(0);
  relay.emit(result);
  const bounds = await page.locator(".file-browser-shell").boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(page.viewportSize()!.height);
  await page.getByRole("button", { name: "README.md", exact: true }).click();
  await expect.poll(() => relay.commands.some((c) => c.type === "get_file_preview")).toBe(true);
  const preview = relay.commands.find((c) => c.type === "get_file_preview")!;
  relay.emit({ type: "file_preview", sid: "layout-parent", request_id: String(preview.request_id),
    path: String(preview.path), format: "markdown", content: "# Workspace preview", size: 20, writable: false });
  await expect(page.getByRole("heading", { name: "Workspace preview" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("workspace-preview.png"), animations: "disabled" });
  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await page.screenshot({ path: testInfo.outputPath("workspace-preview-dark.png"), animations: "disabled" });
  await page.evaluate(() => { document.documentElement.dataset.theme = "light"; });
  await page.getByRole("button", { name: "返回目录", exact: true }).click();
  await expect(page.getByRole("button", { name: "README.md", exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("workspace-directory.png"), animations: "disabled" });
  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await page.screenshot({ path: testInfo.outputPath("workspace-directory-dark.png"), animations: "disabled" });
  await page.evaluate(() => { document.documentElement.dataset.theme = "light"; });
  expect(relay.commands.filter((c) => c.type === "browse_files")).toHaveLength(1);
  await page.getByRole("button", { name: "关闭文件浏览", exact: true }).click();
  const input = page.locator("textarea").first();
  await input.fill("/open README.md");
  await input.press("Enter");
  await expect.poll(() => relay.commands.filter((c) => c.type === "browse_files").length).toBe(2);
  const direct = relay.commands.filter((c) => c.type === "browse_files").at(-1)!;
  expect(direct.path).toBe("README.md");
  relay.emit({ ...result, request_id: String(direct.request_id), kind: "file", path: "/tmp/layout/README.md" });
  await expect.poll(() => relay.commands.filter((c) => c.type === "get_file_preview").length).toBe(2);
  expect(relay.commands.some((c) => ["query", "steer", "new_session"].includes(String(c.type)))).toBe(false);
});

test("session workspace browses outside cwd and never shows an old directory after navigation fails", async ({ page }, testInfo) => {
  const relay = await mockRightPanelRelay(page, { retained: false });
  await page.goto("/");
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  await page.getByRole("button", { name: "打开会话文件", exact: true }).click();
  const requests = () => relay.commands.filter((c) => c.type === "browse_files");
  await expect.poll(() => requests().length).toBe(1);
  const listing: PanelRelayEvent<Extract<ServerEvent, { type: "files_listed" }>> = {
    type: "files_listed", sid: "layout-parent", request_id: String(requests()[0].request_id),
    root: "/", path: "/tmp/layout", kind: "directory", parent: "/tmp",
    revision: "one", next_offset: null,
    entries: [{ name: "README.md", path: "/tmp/layout/README.md", kind: "file" }],
  };
  relay.emit(listing);
  const panel = page.locator(".workspace-files");
  const path = panel.getByRole("textbox", { name: "文件或目录路径", exact: true });
  await expect(panel.locator(".artifact-path")).toHaveText("/tmp/layout");
  await expect(panel.getByRole("button", { name: "上级目录", exact: true })).toBeEnabled();
  await panel.getByRole("button", { name: "上级目录", exact: true }).click();
  await expect.poll(() => requests().length).toBe(2);
  expect(requests()[1].path).toBe("/tmp");
  await expect(panel.getByRole("button", { name: "README.md", exact: true })).toHaveCount(0);
  relay.emit({ ...listing, request_id: String(requests()[1].request_id),
    path: "/tmp", parent: "/", entries: [{ name: "layout", path: "/tmp/layout", kind: "directory" }] });
  await expect(path).toHaveValue("/tmp");
  await expect(panel.locator(".artifact-path")).toHaveText("/tmp");

  await path.fill("/home/example");
  await panel.getByRole("button", { name: "打开", exact: true }).click();
  await expect.poll(() => requests().length).toBe(3);
  expect(requests()[2].path).toBe("/home/example");
  relay.emit({ ...listing, request_id: String(requests()[2].request_id),
    path: "", parent: null, entries: [], error: "没有权限读取该目录" });
  await expect(panel.getByRole("alert")).toHaveText("没有权限读取该目录");
  await expect(path).toHaveValue("/home/example");
  await expect(panel.locator(".workspace-entry")).toHaveCount(0);
  await expect(panel.locator(".artifact-path")).toHaveText("");
  await panel.getByRole("button", { name: "刷新目录", exact: true }).click();
  await expect.poll(() => requests().length).toBe(4);
  expect(requests()[3].path).toBe("/home/example");
  relay.emit(listing); // A late old response must not restore the project list.
  await expect(panel.locator(".workspace-entry")).toHaveCount(0);
  relay.emit({ ...listing, request_id: String(requests()[3].request_id),
    path: "/home/example", parent: "/home",
    entries: [{ name: "notes.md", path: "/home/example/notes.md", kind: "file" }] });
  await expect(panel.getByRole("alert")).toHaveCount(0);
  await expect(panel.locator(".artifact-path")).toHaveText("/home/example");
  await expect(panel.getByRole("button", { name: "notes.md", exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("workspace-external-directory.png") });
  expect(relay.commands.some((c) => ["query", "steer", "new_session"].includes(String(c.type)))).toBe(false);
});

test("session workspace Codex capacity rejects oversize and sends a session preference", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { retained: false });
  await page.goto("/");
  await expect(page.locator(".scard.active .scard-title")).toHaveText("Layout parent");
  const input = page.locator("textarea").first();
  await input.fill("/autocompact ");
  await input.press("Enter");
  relay.emit({ type: "codex_context", sid: "layout-parent", model: "fixture",
    max_context_tokens: null, applied_max_context_tokens: null, applied_threshold_tokens: null, model_max_tokens: 20000,
    context_window_tokens: null, limit_tokens: 19000, pending: false, mutable: true, error: null });
  const setting = page.getByRole("dialog", { name: "Codex 上下文上限", exact: true });
  await expect(setting).toBeVisible();
  await setting.getByLabel("上下文上限（tokens）").fill("20k");
  await setting.getByRole("button", { name: "应用", exact: true }).click();
  await expect(setting.getByRole("alert")).toContainText("19,000");
  expect(relay.commands.some((c) => c.type === "set_codex_context")).toBe(false);
  await setting.getByLabel("上下文上限（tokens）").fill("12k");
  await setting.getByRole("button", { name: "应用", exact: true }).click();
  await expect.poll(() => relay.commands.some((c) => c.type === "set_codex_context")).toBe(true);
  const request = relay.commands.find((c) => c.type === "set_codex_context")!;
  expect([request.sid, request.max_context_tokens]).toEqual(["layout-parent", 12000]);
  relay.emit({ type: "codex_context", sid: "layout-parent", model: "fixture",
    max_context_tokens: 12000, applied_max_context_tokens: null, applied_threshold_tokens: null, model_max_tokens: 20000,
    context_window_tokens: 12632, limit_tokens: 19000, pending: true, mutable: true, error: null });
  await expect(setting.getByRole("status")).toContainText("已保存上限 12,000");
  await setting.getByRole("button", { name: "恢复 Codex 默认值" }).click();
  await expect.poll(() => relay.commands.filter((c) => c.type === "set_codex_context").length).toBe(2);
  expect(relay.commands.filter((c) => c.type === "set_codex_context").at(-1)?.max_context_tokens).toBeNull();
  expect(relay.commands.some((c) => c.type === "query")).toBe(false);
});

test("session workspace context ring refreshes applied capacity and distinguishes pending capacities", async ({ page }, testInfo) => {
  const relay = await mockRightPanelRelay(page, { retained: false });
  await page.goto("/");
  const ring = page.getByRole("button", { name: "上下文占用", exact: true });
  await expect(ring).toBeVisible();
  await expect.poll(() => relay.commands.some((c) => c.type === "get_history")).toBe(true);
  const setting: PanelRelayEvent<Extract<ServerEvent, { type: "codex_context" }>> = {
    type: "codex_context", sid: "layout-parent", model: "fixture",
    max_context_tokens: 400000, applied_max_context_tokens: null, applied_threshold_tokens: null, model_max_tokens: 872000,
    context_window_tokens: 421053, limit_tokens: 828400, pending: true, mutable: true, error: null,
  };
  const report: PanelRelayEvent<Extract<ServerEvent, { type: "context_report" }>> = {
    type: "context_report", sid: "layout-parent", total_tokens: 142045,
    max_tokens: 258400, percentage: 55, model: "fixture", categories: [], source: "native_estimate",
  };
  relay.emit(setting);
  relay.emit(report);
  await ring.click();
  const popover = page.getByRole("dialog", { name: "上下文占用", exact: true });
  await expect(popover).toContainText("142,045 / 258,400 (55%)");
  await expect(popover).toContainText("已保存上限 400,000，等待生效");
  relay.emit({ ...setting, applied_max_context_tokens: 400000, applied_threshold_tokens: 378947, pending: false });
  await expect(popover).not.toContainText("258,400");
  const request = relay.commands.filter((c) => c.type === "get_context").at(-1)!;
  relay.emit({ ...report, request_id: String(request.cmd_id), max_tokens: 400000, percentage: 35.51125 });
  await expect(popover).toContainText("142,045 / 400,000 (36%)");
  await expect(popover.locator(".ctx-pop-row").filter({ hasText: "生效压缩阈值" })).toContainText("378,947");
  await expect(popover).not.toContainText("等待生效");
  await page.screenshot({ path: testInfo.outputPath("applied-context-capacity.png") });
  expect(relay.commands.some((c) => ["query", "steer", "new_session"].includes(String(c.type)))).toBe(false);
});

test("session workspace reconciles pending 400k with native runtime in both context surfaces", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { retained: false });
  await page.goto("/");
  await expect(page.locator(".scard.active .scard-title")).toHaveText("Layout parent");
  const state: PanelRelayEvent<Extract<ServerEvent, { type: "codex_context" }>> = {
    type: "codex_context", sid: "layout-parent", model: "gpt-6-astra", max_context_tokens: 400000,
    applied_max_context_tokens: null, applied_threshold_tokens: null, model_max_tokens: 872000,
    context_window_tokens: 421053, limit_tokens: 828400, pending: true, mutable: true, error: null,
  };
  const report: PanelRelayEvent<Extract<ServerEvent, { type: "context_report" }>> = {
    type: "context_report", sid: "layout-parent", total_tokens: 371048, max_tokens: 400000,
    percentage: 92.762, model: state.model, categories: [], auto_compact_threshold_tokens: 378947,
    source: "recent_turn",
  };
  relay.emit(state);
  relay.emit(report);
  const ring = page.getByRole("button", { name: "上下文占用", exact: true });
  await ring.click();
  const popover = page.getByRole("dialog", { name: "上下文占用", exact: true });
  await expect(popover).toContainText("待原生运行数据确认");
  relay.emit({ ...report, source: "native_estimate" });
  await expect(popover).toContainText("371,048 / 400,000 (93%)");
  await expect(popover).toContainText("原生上限 400,000，与已保存设置一致");
  await expect(popover).not.toContainText("等待生效");
  await popover.getByRole("button", { name: "设置上下文上限" }).click();
  const editor = page.getByRole("dialog", { name: "Codex 上下文上限", exact: true });
  await expect(editor.getByRole("status")).toContainText("与已保存设置一致");
  await expect(editor).toContainText("当前生效上限：400,000");
  await expect(editor).toContainText("当前生效阈值：378,947");
  // Periodic readings must not overwrite an unsent draft or reload the thread.
  await editor.getByLabel("上下文上限（tokens）").fill("350k");
  relay.emit({ ...report, source: "native_estimate", total_tokens: 372000, percentage: 93 });
  await expect(editor.getByLabel("上下文上限（tokens）")).toHaveValue("350k");
  relay.emit({ ...state, max_context_tokens: 300000, context_window_tokens: 315790 });
  await expect(editor.getByRole("status")).toContainText("已保存上限 300,000，等待生效");
  await expect(editor.getByRole("status")).toContainText("当前生效上限：400,000");
  // A real application failure must remain visible even if the latest runtime
  // happens to match a saved preference. Lack of a receipt is not that error.
  relay.emit({ ...state, error: "connection lost" });
  await expect(editor.getByRole("alert")).toContainText("connection lost");
  await expect(editor.getByRole("status")).toContainText("与已保存设置一致");
  relay.emit({ ...state, max_context_tokens: null, context_window_tokens: null });
  await expect(editor.getByRole("status")).toContainText("默认值，待原生运行数据确认");
  expect(relay.commands.some(c => ["query", "steer", "new_session", "set_codex_context", "reconnect"].includes(String(c.type)))).toBe(false);
});

test("session workspace distinguishes 300k capacity from native usage and compaction", async ({ page }, testInfo) => {
  const relay = await mockRightPanelRelay(page, { retained: false });
  await page.goto("/");
  // Initial focus changes the draft scope. Type only after this session is
  // selected, otherwise WebKit can fill the pre-focus draft and then lose it.
  await expect(page.locator(".scard.active .scard-title")).toHaveText("Layout parent");
  const input = page.locator("textarea").first();
  await input.fill("/autocompact ");
  await input.press("Enter");
  const state: PanelRelayEvent<Extract<ServerEvent, { type: "codex_context" }>> = {
    type: "codex_context", sid: "layout-parent", model: "gpt-6-astra",
    max_context_tokens: 300000, applied_max_context_tokens: 400000, applied_threshold_tokens: 378947, model_max_tokens: 872000,
    context_window_tokens: 315790, limit_tokens: 828400, pending: true, mutable: true, error: null,
  };
  relay.emit(state);
  const setting = page.getByRole("dialog", { name: "Codex 上下文上限", exact: true });
  await expect(setting).toContainText("可设置上限 828,400");
  await expect(setting.getByRole("status")).toContainText("已保存上限 300,000");
  await expect(setting.getByRole("status")).toContainText("当前生效上限：400,000");
  await expect(setting).toContainText("进度按 Codex 原生上下文估算显示");

  relay.emit({ ...state, applied_max_context_tokens: 300000,
    applied_threshold_tokens: 284211, pending: false });
  await expect(setting.getByRole("status")).toContainText("设定上限：300,000");
  await expect(setting.getByRole("status")).toContainText("当前生效上限：300,000");
  await expect(setting.getByRole("status")).toContainText("当前生效阈值：284,211");
  await expect(setting).not.toContainText("400,000");
  expect(await setting.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("context-300k-settings.png"), animations: "disabled" });

  const report: PanelRelayEvent<Extract<ServerEvent, { type: "context_report" }>> = {
    type: "context_report", sid: "layout-parent", total_tokens: 245325,
    max_tokens: 300000, percentage: 81.775, model: "gpt-6-astra", categories: [], source: "recent_turn",
  };
  relay.emit(report);
  await page.getByRole("button", { name: "上下文占用", exact: true }).click();
  await expect(setting).not.toBeVisible();
  const popover = page.getByRole("dialog", { name: "上下文占用", exact: true });
  const request = relay.commands.filter((c) => c.type === "get_context").at(-1)!;
  relay.emit({ ...report, request_id: String(request.cmd_id) });
  await expect(popover).toContainText("最近请求用量");
  await expect(popover).toContainText("245,325 tokens");
  await expect(popover).not.toContainText("82%");
  await expect(popover.locator(".ctx-pop-bar")).toHaveCount(0);
  const ring = page.getByRole("button", { name: "上下文占用", exact: true });
  await expect(ring.locator(".hr-fill")).toHaveCount(0);
  // Same incident: billing usage was 245325, but Codex compacted at 284793.
  const nativeReport = { ...report, source: "native_estimate" as const,
    total_tokens: 284793, percentage: 94.931, auto_compact_threshold_tokens: 284211 };
  relay.emit(nativeReport);
  await expect(popover).toContainText("284,793 / 300,000 (95%)");
  await expect(popover.locator(".ctx-pop-bar i")).toHaveCSS("width", /.+/);
  expect(await popover.locator(".ctx-pop-bar i").evaluate(node => (node as HTMLElement).style.width)).toBe("94.931%");
  expect(Number(await ring.locator(".hr-fill").getAttribute("stroke-dashoffset"))).toBeCloseTo(4.7775325);
  await expect(popover.locator(".ctx-pop-row").filter({ hasText: "生效压缩阈值" })).toContainText("284,211");
  await expect(popover).not.toContainText("正在读取真实上下文");
  for (const theme of ["light", "dark"]) {
    await page.evaluate(value => document.documentElement.setAttribute("data-theme", value), theme);
    expect(await popover.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`context-300k-usage-${theme}.png`), animations: "disabled" });
  }
  // Background read failures retain the last native estimate without UI noise.
  relay.emit(report);
  await expect(popover).toContainText("284,793 / 300,000 (95%)");
  const oldHeight = (await popover.boundingBox())!.height;
  const oldOffset = await ring.locator(".hr-fill").getAttribute("stroke-dashoffset");
  relay.emit({ ...report, available: false, total_tokens: 0, percentage: 0 });
  await expect(popover).toContainText("284,793 / 300,000 (95%)");
  const requestCount = relay.commands.filter(c => c.type === "get_context").length;
  await ring.click();
  await ring.click();
  await expect.poll(() => relay.commands.filter(c => c.type === "get_context").length).toBeGreaterThan(requestCount);
  const refresh = relay.commands.filter(c => c.type === "get_context").at(-1)!;
  relay.emit({ type: "error", sid: "layout-parent", request_id: String(refresh.cmd_id),
    code: "invalid_command", message: "无法读取上下文，请稍后重试" });
  await expect(popover).not.toContainText("正在读取");
  await expect(page.getByText("无法读取上下文，请稍后重试", { exact: true })).toHaveCount(0);
  await expect(popover.locator(".ctx-pop-status")).toHaveCount(0);
  await expect(ring.locator("text")).toHaveCount(0);
  await expect(ring.locator(".hr-fill")).toHaveAttribute("stroke-dashoffset", oldOffset!);
  expect((await popover.boundingBox())!.height).toBe(oldHeight);
  relay.emit({ ...nativeReport, total_tokens: 33824, percentage: 11.27467 });
  await expect(popover).toContainText("33,824 / 300,000 (11%)");
  expect(relay.commands.some((c) => ["query", "steer", "new_session"].includes(String(c.type)))).toBe(false);
});

test("session workspace refreshes native context during a running turn", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { retained: false });
  await page.goto("/");
  await expect.poll(() => relay.commands.some(c => c.type === "get_context")).toBe(true);
  const report: PanelRelayEvent<Extract<ServerEvent, { type: "context_report" }>> = {
    type: "context_report", sid: "layout-parent", total_tokens: 281362,
    max_tokens: 300000, percentage: 93.78733, model: "fixture", categories: [], source: "native_estimate",
  };
  const first = relay.commands.filter(c => c.type === "get_context").at(-1)!;
  relay.emit({ ...report, request_id: String(first.cmd_id) });
  relay.emit({ type: "state", sid: "layout-parent", state: "running" });
  const count = relay.commands.filter(c => c.type === "get_context").length;
  await expect.poll(() => relay.commands.filter(c => c.type === "get_context").length,
    { timeout: 8000 }).toBeGreaterThan(count);
  const next = relay.commands.filter(c => c.type === "get_context").at(-1)!;
  relay.emit({ ...report, request_id: String(next.cmd_id), total_tokens: 284793, percentage: 94.931 });
  const ring = page.getByRole("button", { name: "上下文占用", exact: true });
  await expect.poll(async () => Number(await ring.locator(".hr-fill").getAttribute("stroke-dashoffset")))
    .toBeCloseTo(4.7775325);
  expect(relay.commands.some(c => ["query", "steer", "new_session"].includes(String(c.type)))).toBe(false);
});

test("session workspace Claude harness switch keeps history during a deferred private-fork catalog read", async ({ page }) => {
  let deferClaude = false;
  let busyResponses = 0;
  const relay = await mockRightPanelRelay(page, {
    retained: false, engine: "claude",
    seedTurns: [{ id: "kept-turn", prompt: "保留原会话内容", done: true, blocks: [] }],
    listReply: command => {
      if (command.engine === "codex") return {
        type: "session_list", engine: "codex", space: "code",
        request_id: String(command.cmd_id), sessions: [{
          session_id: "codex-parent", engine: "codex", space: "code",
          summary: "Codex parent", cwd: "/tmp/codex", state: "idle",
        }],
      };
      if (deferClaude && busyResponses++ === 0) return {
        type: "error", code: "busy", request_id: String(command.cmd_id),
        message: "临时 btw 会话正在初始化，请稍后刷新会话列表",
      };
    },
  });
  await page.goto("/");
  await expect(page.getByText("保留原会话内容", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "切换新会话引擎" }).click();
  await page.getByRole("menuitemradio", { name: "Codex", exact: true }).click();
  await expect.poll(() => relay.commands.some(c => c.type === "switch_session"
    && c.session_id === "codex-parent")).toBe(true);
  deferClaude = true;
  await page.getByRole("button", { name: "切换新会话引擎" }).click();
  await page.getByRole("menuitemradio", { name: "Claude", exact: true }).click();
  await expect.poll(() => busyResponses).toBeGreaterThanOrEqual(2);
  await expect(page.getByText("保留原会话内容", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "切换新会话引擎" })).toContainText("Claude");
  await expect(page.getByText("当前操作暂时无法执行，请稍后重试。", { exact: true })).toHaveCount(0);
  expect(relay.commands.some(c => ["query", "steer", "interrupt", "new_session"].includes(String(c.type)))).toBe(false);
});

function otherHarnessList(command: Record<string, unknown>): PanelRelayEvent | undefined {
  if (command.engine !== "codex") return;
  return { type: "session_list", engine: "codex", space: "code",
    request_id: String(command.cmd_id), sessions: [{
      session_id: "codex-parent", engine: "codex", space: "code",
      summary: "Codex parent", cwd: "/tmp/codex", state: "idle",
    }] };
}

for (const older of [false, true]) {
  test(`session workspace navigation reading preserves ${older ? "older pages" : "current page"} across real harness shortcuts`, async ({ page }) => {
    const turns = (start: number, count: number) => Array.from({ length: count }, (_, i) => ({
      id: `reading-${start + i}`, prompt: `Question ${start + i}`, done: true,
      blocks: [{ kind: "text" as const, message_id: `answer-${start + i}`, done: true,
        text: Array.from({ length: 18 }, (_, p) => `Answer ${start + i}, paragraph ${p}: ${"Keep the original reading position. ".repeat(4)}`).join("\n\n") }],
    }));
    let advanced = false;
    const reply = (sid: string, before?: string) => ({
      type: "history" as const, session_id: sid, sid, before,
      revision: "layout-history", generation: "layout-generation", build_seq: advanced ? 2 : 1,
      detail: "summary" as const, events: [], authoritative: true,
      turns: before ? turns(9, 12) : turns(advanced && sid === "codex-parent" ? 22 : 21, 4),
      has_more: !before, oldest_id: before ? "reading-9" : advanced && sid === "codex-parent" ? "reading-22" : "reading-21",
    });
    const relay = await mockRightPanelRelay(page, { engine: "claude", retained: false,
      listReply: otherHarnessList,
      historyReply: command => reply(String(command.session_id), command.before as string | undefined),
    });
    await page.goto("/");
    await expect(page.locator('.thread [data-turn-id="reading-24"]')).toBeAttached();
    await page.keyboard.press("Meta+2");
    await expect(page.getByText("session codex-pa", { exact: true })).toBeAttached();
    const thread = page.locator(".pane .thread");
    if (older) {
      await thread.evaluate(node => {
        node.dispatchEvent(new WheelEvent("wheel", { bubbles: true, deltaY: -200 }));
        node.scrollTop = 0; node.dispatchEvent(new Event("scroll"));
      });
      await waitForScrollIdle(page);
      await expect.poll(() => relay.commands.some(c => c.type === "get_history" && !!c.before)).toBe(true);
      await expect(thread.locator('[data-turn-id="reading-20"]')).toBeAttached();
    }
    const before = await navigateReadingToMiddle(page);
    await page.keyboard.press("Meta+1");
    await expect(page.getByRole("button", { name: "切换新会话引擎" })).toContainText("Claude");
    advanced = true;
    relay.emit(reply("codex-parent"));
    await page.keyboard.press("Meta+2");
    await expect(page.getByText("session codex-pa", { exact: true })).toBeAttached();
    await expect.poll(() => navigationReadingAnchor(page)).toEqual(before);
    await page.waitForTimeout(300);
    expect(await navigationReadingAnchor(page)).toEqual(before);
    expect(relay.commands.some(c => ["query", "steer", "interrupt", "new_session"].includes(String(c.type)))).toBe(false);
  });
}

test("session workspace harness shortcuts preserve centered side chat and composer drafts", async ({ page }) => {
  await page.setViewportSize({ width: 1568, height: 900 });
  const relay = await mockRightPanelRelay(page, { engine: "claude", listReply: otherHarnessList,
    seedTurns: [{ id: "done", prompt: "已完成的任务", done: true,
      blocks: [{ kind: "text", message_id: "final", text: "已完成", done: true }] }],
  });
  await page.goto("/");
  const selector = page.getByRole("button", { name: "切换新会话引擎" });
  await expect(page.locator(".turn-done-mark .engine-brand-icon.claude")).toBeVisible();
  const completedMark = page.locator(".turn-done-mark .engine-done-mark");
  await completedMark.click();
  await expect(completedMark.locator(".claude-working-mark")).toBeVisible();
  await expect(completedMark.locator(".engine-brand-icon.claude")).toBeVisible();
  await completedMark.focus();
  await page.keyboard.press("Enter");
  await expect(completedMark.locator(".claude-working-mark")).toBeVisible();
  await expect(completedMark.locator(".engine-brand-icon.claude")).toBeVisible();
  await page.emulateMedia({ reducedMotion: "reduce" });
  await completedMark.click();
  await expect(completedMark.locator(".engine-brand-icon.claude")).toBeVisible();
  await expect(completedMark.locator(".claude-working-mark")).toHaveCount(0);
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.keyboard.press("Meta+Shift+e");
  const panel = page.locator(".btw-panel");
  await expect(panel).toHaveAttribute("aria-modal", "true");
  await panel.locator("textarea").fill("Claude 侧聊草稿");
  await page.keyboard.press("Meta+2");
  await expect(selector).toContainText("Codex");
  await expect(panel).toHaveCount(0);
  await expect(page.locator(".turn-done-mark .spark-btn")).toBeVisible();
  await page.locator("textarea[data-chat-composer]").fill("Codex 主聊草稿");
  await page.keyboard.press("Control+1");
  await expect(selector).toContainText("Claude");
  await expect(panel).toHaveAttribute("aria-modal", "true");
  await expect(panel.locator("textarea")).toHaveValue("Claude 侧聊草稿");
  await expect(panel.locator("textarea")).not.toBeFocused();
  // Shortcut navigation also owns a pointer-opened harness menu.
  await page.keyboard.press("Meta+2");
  await expect(page.locator("textarea[data-chat-composer]")).toHaveValue("Codex 主聊草稿");
  await selector.click();
  await page.keyboard.press("Meta+1");
  await expect(page.getByRole("menu", { name: "会话引擎" })).toHaveCount(0);
  await expect(panel).toHaveAttribute("aria-modal", "true");
  await page.keyboard.press("Meta+Shift+e");
  await expect(panel).toHaveCount(0);
  await page.keyboard.press("Meta+2");
  await expect(selector).toContainText("Codex");
  await page.keyboard.press("Meta+1");
  await expect(selector).toContainText("Claude");
  await expect(panel).toHaveCount(0);
  relay.emit({ type: "btw_sync", generation: "layout-generation", revision: 2, sessions: [
    { btw_sid: "btw-layout-child", parent_sid: "layout-parent", engine: "claude", created_at: 1, state: "running" },
    { btw_sid: "codex-child", parent_sid: "codex-parent", engine: "codex", created_at: 2, state: "idle" },
  ] });
  await page.keyboard.press("Meta+Shift+e");
  await expect(panel.locator("textarea")).toHaveValue("Claude 侧聊草稿");
  await page.keyboard.press("Meta+2");
  await expect(selector).toContainText("Codex");
  await page.keyboard.press("Meta+Shift+e");
  await expect(panel).toHaveAttribute("aria-modal", "true");
  await panel.locator("textarea").fill("Codex 独立侧聊草稿");
  await page.keyboard.press("Meta+1");
  await expect(panel).toHaveAttribute("aria-modal", "true");
  await expect(panel.locator("textarea")).toHaveValue("Claude 侧聊草稿");
  await page.keyboard.press("Meta+2");
  await expect(panel).toHaveAttribute("aria-modal", "true");
  await expect(panel.locator("textarea")).toHaveValue("Codex 独立侧聊草稿");
  expect(relay.commands.some(c => ["query", "steer", "interrupt", "open_btw", "close_btw", "answer_question"].includes(String(c.type)))).toBe(false);
});

for (const sideChat of [false, true]) {
  test(`session workspace harness shortcuts preserve ${sideChat ? "side" : "main"} question drafts and routing`, async ({ page }) => {
    await page.setViewportSize({ width: 1568, height: 900 });
    const relay = await mockRightPanelRelay(page, { engine: "claude", parentState: "running", listReply: otherHarnessList });
    await page.goto("/");
    await expect(page.locator("textarea[data-chat-composer]")).toBeEnabled();
    if (sideChat) {
      await page.keyboard.press("Meta+Shift+e");
      await expect(page.locator(".btw-panel")).toHaveAttribute("aria-modal", "true");
    }
    const sid = sideChat ? "btw-layout-child" : "layout-parent";
    relay.emit({ type: "ask_user", sid, seq: 1, ask_id: "shared-ask-id",
      question: "Claude 的待确认问题\n\n".repeat(60),
      options: [{ label: "保留选择" }, { label: "第二选项" }], allow_text: true, multi_select: true });
    const dialog = page.getByRole("dialog", { name: "操作确认" });
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "保留选择", exact: true }).click();
    const answer = dialog.getByPlaceholder("输入回答");
    await answer.fill("Claude 原有草稿");
    await answer.dispatchEvent("compositionstart");
    await answer.evaluate(input => { (input as HTMLInputElement).value = "Claude 未提交的回答"; });
    const scroll = await dialog.locator(".sheet-scroll").evaluate(node => node.scrollTop);
    await page.keyboard.press("Meta+2");
    const selector = page.getByRole("button", { name: "切换新会话引擎" });
    await expect(selector).toContainText("Codex");
    await expect(dialog).toHaveCount(0);
    await expect(page.getByText("session codex-pa", { exact: true })).toBeVisible();
    // The same provider-local ask ID must not transfer answers across sessions.
    relay.emit({ type: "ask_user", sid: "codex-parent", seq: 1, ask_id: "shared-ask-id",
      question: "Codex 的问题", options: [], allow_text: true });
    await expect(dialog.getByPlaceholder("输入回答")).toHaveValue("");
    await dialog.getByPlaceholder("输入回答").fill("Codex 未提交的回答");
    await page.keyboard.press("Control+1");
    await expect(selector).toContainText("Claude");
    await expect(dialog.getByPlaceholder("输入回答")).toHaveValue("Claude 未提交的回答");
    await expect(dialog.getByRole("button", { name: "保留选择", exact: true })).toHaveAttribute("aria-pressed", "true");
    await expect.poll(() => dialog.locator(".sheet-scroll").evaluate(node => node.scrollTop)).toBeCloseTo(scroll, 0);
    if (sideChat) await expect(page.locator(".btw-panel")).toHaveAttribute("aria-modal", "true");
    expect(relay.commands.some(c => ["query", "steer", "interrupt", "open_btw", "close_btw", "answer_question"].includes(String(c.type)))).toBe(false);
    await dialog.locator(".qa-text-answer").getByRole("button", { name: "确定", exact: true }).click();
    await expect(dialog).toHaveCount(0);
    await expect.poll(() => relay.commands.filter(c => c.type === "answer_question")
      .map(({ sid, ask_id, answer }) => ({ sid, ask_id, answer }))).toEqual([
      { sid, ask_id: "shared-ask-id", answer: ["保留选择", "Claude 未提交的回答"] },
    ]);
    await page.keyboard.press("Meta+2");
    await expect(dialog.getByPlaceholder("输入回答")).toHaveValue("Codex 未提交的回答");
    await page.keyboard.press("Meta+1");
    await expect(selector).toContainText("Claude");
    await expect(dialog).toHaveCount(0);
    // Closing a question in the background must not resurrect its saved draft.
    relay.emit({ type: "ask_user_closed", sid: "codex-parent", seq: 2, ask_id: "shared-ask-id" });
    await page.keyboard.press("Meta+2");
    await expect(selector).toContainText("Codex");
    await expect(dialog).toHaveCount(0);
  });
  test(`session workspace native question batch retains composing ${sideChat ? "side" : "main"} drafts across harness switches`, async ({ page }) => {
    const relay = await mockRightPanelRelay(page, {
      engine: "claude", parentState: "running", visible: sideChat, listReply: otherHarnessList,
    });
    await page.goto("/");
    await expect(page.locator("textarea[data-chat-composer]")).toBeEnabled();
    const sid = sideChat ? "btw-layout-child" : "layout-parent";
    const questions = [
      { question: "确认第一项", options: [{ label: "保留第一项" }], allow_text: true, multi_select: true },
      { question: "确认第二项\n".repeat(60), options: [{ label: "保留第二项" }], allow_text: true, multi_select: true },
    ];
    relay.emit({ type: "ask_user", sid, seq: 1, ask_id: "composing-batch", ...questions[0], questions });
    const dialog = page.getByRole("dialog", { name: "操作确认" });
    await dialog.getByRole("button", { name: "保留第一项", exact: true }).click();
    await dialog.getByRole("button", { name: "下一题", exact: true }).click();
    await expect(dialog.locator(".qa-progress")).toHaveText("2 / 2");
    await dialog.getByRole("button", { name: "保留第二项", exact: true }).click();
    const answer = dialog.getByRole("textbox", { name: "输入回答" });
    await answer.fill("第二题原有草稿");
    const scroll = await dialog.locator(".sheet-scroll").evaluate(node => node.scrollTop);
    await answer.dispatchEvent("compositionstart");
    await answer.evaluate(input => { (input as HTMLInputElement).value = "中文输入中的回答"; });
    await page.keyboard.press("Meta+2");
    const selector = page.getByRole("button", { name: "切换新会话引擎" });
    await expect(selector).toContainText("Codex");
    await expect(dialog).toHaveCount(0);
    await page.keyboard.press("Meta+1");
    await expect(dialog.locator(".qa-progress")).toHaveText("2 / 2");
    await expect(answer).toHaveValue("中文输入中的回答");
    await expect(dialog.getByRole("button", { name: "保留第二项", exact: true }))
      .toHaveAttribute("aria-pressed", "true");
    await expect.poll(() => dialog.locator(".sheet-scroll").evaluate(node => node.scrollTop))
      .toBeCloseTo(scroll, 0);
    expect(relay.commands.some(command => command.type === "answer_question")).toBe(false);
    await dialog.getByRole("button", { name: "提交回答", exact: true }).click();
    await expect.poll(() => relay.commands.filter(command => command.type === "answer_question")
      .map(({ sid, ask_id, answers }) => ({ sid, ask_id, answers }))).toEqual([
      { sid, ask_id: "composing-batch", answers: [["保留第一项"], ["保留第二项", "中文输入中的回答"]] },
    ]);
    relay.emit({ type: "ask_user_closed", sid, seq: 2, ask_id: "composing-batch", reason: "answered" });
    await expect(dialog).toHaveCount(0);
  });
}

for (const batch of [false, true]) {
  test(`session workspace DSH ${batch ? "batch" : "single"} questions identify their native harness`, async ({ page }) => {
    const sid = "dsh@question-label";
    const relay = await mockRightPanelRelay(page, { retained: false, listReply: command => {
      if (command.engine !== "dsh") return;
      return { type: "session_list", engine: "dsh", space: "code", request_id: String(command.cmd_id),
        sessions: [{ session_id: sid, engine: "dsh", space: "code", summary: "DSH question", cwd: "/tmp/dsh", state: "idle" }] };
    } });
    await page.goto("/");
    await expect(page.locator(".engine-name")).toHaveText("Codex");
    await page.keyboard.press("Control+3");
    await expect.poll(() => relay.commands.some(command =>
      command.type === "get_history" && command.session_id === sid)).toBe(true);
    const question = { question: "是否继续？", options: [{ label: "继续" }], allow_text: false };
    relay.emit({ type: "ask_user", sid, seq: 1, ask_id: "dsh-question", ...question,
      questions: batch ? [question, question] : undefined });
    const dialog = page.getByRole("dialog", { name: "操作确认" });
    await expect(dialog.locator(".question-dialog-title")).toContainText("DSH 想确认一下");
    await dialog.getByRole("button", { name: "收起问题", exact: true }).click();
    await expect(page.locator(".question-resume")).toBeVisible();
    expect(relay.commands.some(command => command.type === "answer_question")).toBe(false);
    relay.emit({ type: "ask_user_closed", sid, seq: 2, ask_id: "dsh-question", reason: "cancelled" });
    await expect(page.locator(".question-resume")).toHaveCount(0);
  });
}

test("session workspace Claude context refreshes while running and keeps its last reading silently", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, {
    retained: false, engine: "claude", parentState: "running",
  });
  await page.goto("/");
  const reads = () => relay.commands.filter(c => c.type === "get_context");
  await expect.poll(() => reads().length).toBeGreaterThan(0);
  // Cold restored workers need an actual summary to recover their capacity.
  expect(reads().at(-1)?.refresh).toBe(true);
  const report: PanelRelayEvent<Extract<ServerEvent, { type: "context_report" }>> = {
    type: "context_report", sid: "layout-parent", model: "claude-fable-5-1",
    total_tokens: 242701, max_tokens: 0, percentage: 0,
    categories: [], source: "recent_turn",
  };
  relay.emit({ ...report, request_id: String(reads().at(-1)!.cmd_id) });
  const ring = page.getByRole("button", { name: "上下文占用", exact: true });
  const before = reads().length;
  await ring.click();
  await expect.poll(() => reads().length).toBeGreaterThan(before);
  const opened = reads().at(-1)!;
  expect(opened.refresh).toBe(true);
  const fullReport = { ...report, max_tokens: 500000,
    raw_max_tokens: 1000000, percentage: 48.5402, source: "control" as const };
  relay.emit({ ...fullReport, request_id: String(opened.cmd_id) });
  const popover = page.getByRole("dialog", { name: "上下文占用", exact: true });
  await expect(popover).toContainText("242,701 / 500,000 (49%)");
  const count = reads().length;
  await expect.poll(() => reads().length, { timeout: 8000 }).toBeGreaterThan(count);
  const poll = reads().at(-1)!;
  expect(poll.refresh).toBe(false);
  relay.emit({ ...fullReport, request_id: String(poll.cmd_id),
    source: "recent_turn", total_tokens: 339685, percentage: 67.937 });
  await expect(popover).toContainText("339,685 / 500,000 (68%)");
  const offset = await ring.locator(".hr-fill").getAttribute("stroke-dashoffset");
  await ring.click();
  const closedCount = reads().length;
  await ring.click();
  await expect.poll(() => reads().length).toBeGreaterThan(closedCount);
  relay.emit({ ...report, request_id: String(reads().at(-1)!.cmd_id),
    total_tokens: 0, available: false });
  await expect(popover).toContainText("339,685 / 500,000 (68%)");
  relay.emit({ ...report, total_tokens: 350000 });
  await expect(popover).toContainText("339,685 / 500,000 (68%)");
  await expect(ring.locator(".hr-fill")).toHaveAttribute("stroke-dashoffset", offset!);
  await expect(popover.locator(".ctx-pop-status")).toHaveCount(0);
  await expect(popover).not.toContainText("正在读取");
  await expect(ring.locator("text")).toHaveCount(0);
  expect(relay.commands.some(c => ["query", "steer", "interrupt", "new_session"].includes(String(c.type)))).toBe(false);
});

test("turn regressions App routes file pages by exact request and opens the archived diff", async ({ page }) => {
  const files = Array.from({ length: 65 }, (_, i) => ({
    path: `/tmp/layout/file-${i}.py`, state: "available" as const, additions: 1, deletions: 1,
  }));
  const relay = await mockRightPanelRelay(page, { retained: false, seedTurns: [{
    id: "archived-turn", prompt: "many files", done: true, blocks: [],
    fileChanges: { revision: "immutable", files: files.slice(0, 64), total_files: 65,
      total_additions: 65, total_deletions: 65, next_offset: 64 },
  }] });
  await page.goto("/");
  await page.locator(".turn-changes-toggle").click();
  await page.getByRole("button", { name: "加载更多", exact: true }).click();
  await expect.poll(() => relay.commands.some((command) => command.type === "get_turn_file_changes")).toBe(true);
  const read = relay.commands.find((command) => command.type === "get_turn_file_changes")!;
  expect([read.sid, read.engine, read.turn_id, read.revision, read.offset, read.limit]).toEqual([
    "layout-parent", "codex", "archived-turn", "immutable", 64, 64,
  ]);
  const response: PanelRelayEvent<Extract<ServerEvent, { type: "turn_file_changes_page" }>> = {
    type: "turn_file_changes_page", sid: String(read.sid), engine: "codex", turn_id: "archived-turn",
    revision: "immutable", request_id: String(read.cmd_id), offset: 64, files: files.slice(64),
    total_files: 65, next_offset: null,
  };
  relay.emit({ ...response, revision: "other-version" });
  await expect(page.locator(".turn-change-file")).toHaveCount(64);
  relay.emit(response);
  await expect(page.locator(".turn-change-file")).toHaveCount(65);
  await page.getByRole("button", { name: files[64].path, exact: true }).click();
  await expect.poll(() => relay.commands.some((command) => command.type === "get_diff")).toBe(true);
  const diff = relay.commands.find((command) => command.type === "get_diff")!;
  expect([diff.sid, diff.engine, diff.turn_id, diff.revision, diff.file]).toEqual([
    "layout-parent", "codex", "archived-turn", "immutable", files[64].path,
  ]);
  expect(relay.commands.some((command) => ["query", "steer", "new_session"].includes(String(command.type)))).toBe(false);
});

test("policy refusal remains specific through live delivery and history reload", async ({ page }, testInfo) => {
  const message = "上游模型因安全策略拒绝了本次请求（cyber_policy）。"
    + "这不是本地权限或网络错误；请核实并说明任务背景与授权范围，"
    + "若属误判请向服务提供方反馈。";
  const turns: NonNullable<Extract<ServerEvent, { type: "history" }>["turns"]> = [];
  // Keep live delivery isolated from a fabricated empty canonical page. Once
  // the source has materialized, reload must use that exact canonical history.
  const relay = await mockRightPanelRelay(page, { retained: false,
    historyReply: (command) => turns.length === 0 ? null : {
      type: "history", sid: String(command.session_id), session_id: String(command.session_id),
      request_id: String(command.cmd_id), revision: "policy-history", generation: "layout-generation",
      detail: "summary", events: [], turns, has_more: false,
    },
  });
  await page.goto("/");
  await expect.poll(() => relay.commands.some(c => c.type === "get_history")).toBe(true);
  await expect(page.locator(".composer textarea")).toBeEnabled();
  relay.emit({ type: "user_msg", sid: "layout-parent", msg_id: "policy-user", prompt: "检查这段代码" });
  relay.emit({ type: "turn_binding", sid: "layout-parent", msg_id: "policy-user", turn_id: "policy-turn" });
  relay.emit({ type: "state", sid: "layout-parent", state: "running", msg_id: "policy-user" });
  relay.emit({ type: "error", sid: "layout-parent", code: "cc_crash", message, msg_id: "policy-user" });
  relay.emit({ type: "turn_end", sid: "layout-parent", turn_id: "policy-turn",
    result: { subtype: "error", duration_ms: 20, is_error: true } });
  relay.emit({ type: "state", sid: "layout-parent", state: "idle" });
  const problem = page.locator(".turn .turn-problem");
  await expect(problem).toHaveText(message);
  await expect(page.locator(".composer textarea")).toBeEnabled();
  expect(relay.commands.filter(c => c.type === "query" || c.type === "steer")).toHaveLength(0);

  turns.push({ id: "policy-user", forkPointId: "policy-turn", prompt: "检查这段代码",
    blocks: [], done: true, error: message, detailEventCount: 0, detailLoaded: true },
  { id: "followup-user", forkPointId: "followup-turn", prompt: "解释现有日志",
    blocks: [{ kind: "text", message_id: "followup-answer", text: "日志说明已完成。",
      channel: "final", done: true }], done: true, detailEventCount: 0, detailLoaded: true });
  await page.reload();
  await expect(problem).toHaveCount(1);
  await expect(problem).toHaveText(message);
  await expect(page.locator('[data-turn-id="followup-user"]')).toContainText("日志说明已完成。");
  await expect(page.locator('[data-turn-id="followup-user"] .turn-problem')).toHaveCount(0);
  for (const theme of ["light", "dark"]) {
    await page.evaluate(value => document.documentElement.setAttribute("data-theme", value), theme);
    await expect(problem).toBeVisible();
    expect(await problem.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
    expect(["none", "normal"]).toContain(await problem.evaluate(node => getComputedStyle(node, "::before").content));
    expect(["none", "normal"]).toContain(await problem.evaluate(node => getComputedStyle(node, "::after").content));
    await page.screenshot({ path: testInfo.outputPath(`policy-refusal-${theme}.png`) });
  }
});

for (const [engine, delivery] of [
  ["codex", "query"], ["codex", "steer"], ["codex", "queue"], ["claude", "query"],
] as const) {
  test(`side chat scope attachments send complete payloads through ${engine} ${delivery}`, async ({ page }) => {
    const relay = await mockRightPanelRelay(page, { visible: true, engine });
    await page.goto("/");
    const panel = page.locator(".btw-panel");
    await expect(panel.getByRole("textbox")).toBeEnabled();
    await expect(panel.locator(".btw-runbar")).toHaveCount(0);
    if (delivery === "query") {
      relay.emit({ type: "state", sid: "btw-layout-child", state: "idle" });
      await expect(panel.locator(".btw-runbar")).toHaveCount(0);
    }
    await panel.getByRole("button", { name: "添加附件", exact: true }).click();
    const chooser = page.getByRole("dialog", { name: "添加附件", exact: true });
    await expect(chooser.getByRole("button", { name: /从相册选择图片/ })).toBeVisible();
    await expect(chooser.getByRole("button", { name: /使用相机拍摄照片/ })).toBeVisible();
    const picker = page.waitForEvent("filechooser");
    await chooser.getByRole("button", { name: /添加文档、表格或其他文件/ }).click();
    await (await picker).setFiles([
      { name: "side.png", mimeType: "image/png", buffer: staticPng() },
      { name: "side.txt", mimeType: "text/plain", buffer: Buffer.from("side evidence") },
    ]);
    await expect(panel.locator(".attach-image-preview")).toHaveCount(1);
    await expect(panel.locator(".attach-file")).toHaveText("side.txt");
    // Pending attachments stay above the input border, scoped to this chat
    // for normal, queued and steered submission alike.
    await expect(panel.locator(".btw-composer>.btw-pastes .attach-image-preview")).toHaveCount(1);
    await expect(panel.locator(".btw-composer>.btw-pastes .attach-file")).toHaveText("side.txt");
    const card = await panel.locator(".btw-input-card").boundingBox();
    const attachment = await panel.locator(".attach-file").boundingBox();
    expect(attachment!.y + attachment!.height).toBeLessThan(card!.y);
    await expect(panel.locator(".btw-input-card .attach")).toHaveCount(0);
    await expect(page.locator(".composer .attach-file, .composer .attach-image-preview")).toHaveCount(0);
    // An attachment-only click/Tab sends the payload, never an interrupt.
    if (delivery === "queue") await panel.getByRole("textbox").press("Tab");
    else await panel.getByRole("button", { name: "发送", exact: true }).click();
    await expect.poll(() => relay.commands.filter(c => c.type === "query" || c.type === "steer").length).toBe(1);
    const sent = relay.commands.find(c => c.type === "query" || c.type === "steer")!;
    expect(sent.sid).toBe("btw-layout-child");
    expect(sent.type).toBe(delivery === "steer" ? "steer" : "query");
    expect(sent.delivery).toBe(delivery === "queue" ? "queue" : undefined);
    expect(sent.images).toEqual([{ media_type: "image/png", data: staticPng().toString("base64") }]);
    expect(sent.files).toEqual([{ filename: "side.txt", data: Buffer.from("side evidence").toString("base64") }]);
    expect(relay.commands.some(c => c.type === "interrupt")).toBe(false);
    await expect(panel.locator(".attach-image-preview, .attach-file")).toHaveCount(0);
  });
}

test("side chat scope attachments follow the drop location and accept clipboard images", async ({ page }) => {
  await page.setViewportSize({ width: 1400, height: 850 });
  await mockRightPanelRelay(page, { visible: true });
  await page.goto("/");
  const panel = page.locator(".btw-panel");
  await expect(panel.getByRole("textbox")).toBeEnabled();
  const main = page.locator(".composer");
  await main.getByRole("textbox").fill("main draft");
  const transfer = await page.evaluateHandle(encoded => {
    const data = new DataTransfer();
    data.items.add(new File(["drop evidence"], "drop.txt", { type: "text/plain" }));
    data.items.add(new File([Uint8Array.from(atob(encoded), c => c.charCodeAt(0))], "drop.png", { type: "image/png" }));
    return data;
  }, staticPng().toString("base64"));
  await panel.dispatchEvent("dragover", { dataTransfer: transfer });
  await expect(panel.locator(".drop-overlay")).toContainText("添加到侧边对话");
  await expect(main.locator(".drop-overlay")).toHaveCount(0);
  await panel.dispatchEvent("drop", { dataTransfer: transfer });
  await expect(panel.locator(".attach-file")).toHaveText("drop.txt");
  await expect(panel.locator(".attach-image-preview")).toHaveCount(1);
  await expect(main.locator(".attach-file, .attach-image-preview")).toHaveCount(0);
  await panel.getByRole("textbox").fill("side draft");
  await main.dispatchEvent("dragover", { dataTransfer: transfer });
  await expect(main.locator(".drop-overlay")).toBeVisible();
  await expect(panel.locator(".drop-overlay")).toHaveCount(0);
  await main.dispatchEvent("drop", { dataTransfer: transfer });
  await expect(main.locator(".attach-file")).toHaveText("drop.txt");
  await expect(page.locator(".drop-overlay")).toHaveCount(0);
  await transfer.dispose();
  await panel.getByRole("textbox").evaluate((textarea, encoded) => {
    const data = new DataTransfer();
    data.items.add(new File([Uint8Array.from(atob(encoded), c => c.charCodeAt(0))], "paste.png", { type: "image/png" }));
    textarea.dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: data }));
    data.items.clear();
  }, staticPng().toString("base64"));
  await expect(panel.locator(".attach-image-preview")).toHaveCount(2);
  await expect(main.locator(".attach-image-preview")).toHaveCount(1);
  await expect(main.getByRole("textbox")).toHaveValue("main draft");
  await expect(panel.getByRole("textbox")).toHaveValue("side draft");
});

test("side chat scope attachments stay with the original tab while importing", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { visible: true });
  await page.addInitScript(() => {
    const original = FileReader.prototype.readAsDataURL;
    FileReader.prototype.readAsDataURL = function(blob) {
      Object.assign(window, { releaseSideImport: () => original.call(this, blob) });
    };
  });
  await page.goto("/");
  const panel = page.locator(".btw-panel");
  await expect(panel.getByRole("textbox")).toBeEnabled();
  await panel.getByLabel("添加文件", { exact: true }).setInputFiles({
    name: "slow.txt", mimeType: "text/plain", buffer: Buffer.from("original tab"),
  });
  await expect.poll(() => page.evaluate(() => typeof Reflect.get(window, "releaseSideImport"))).toBe("function");
  await panel.getByRole("textbox").fill("wait for import");
  await panel.getByRole("textbox").press("Enter");
  expect(relay.commands.some(c => c.type === "query" || c.type === "steer")).toBe(false);
  relay.emit({ type: "btw_sync", generation: "layout-generation", revision: 2, sessions: [
    { btw_sid: "btw-layout-child", parent_sid: "layout-parent", engine: "codex", created_at: 1, state: "running" },
    { btw_sid: "btw-next-child", parent_sid: "layout-parent", engine: "codex", created_at: 2, state: "idle" },
  ] });
  await expect(panel.getByRole("tab")).toHaveCount(2);
  await panel.getByRole("tab").nth(1).click();
  await page.evaluate(() => Reflect.get(window, "releaseSideImport")());
  // Completion of the import must write the old draft, not the selected tab.
  await expect(panel.getByRole("button", { name: "添加附件", exact: true })).toBeEnabled();
  await expect(panel.locator(".attach-file")).toHaveCount(0);
  await panel.getByRole("tab").first().click();
  await expect(panel.locator(".attach-file")).toHaveText("slow.txt");
  await expect(panel.getByRole("textbox")).toHaveValue("wait for import");
});

test("side chat scope service tier toggles only its fork and waits for the reported value", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { visible: true });
  await page.goto("/");
  const panel = page.locator(".btw-panel");
  const control = panel.getByRole("button", { name: /^速度：/ });
  await expect(control).toBeEnabled();
  relay.emit({ type: "fast", sid: "layout-parent", on: false });
  relay.emit({ type: "fast", sid: "btw-layout-child", on: false });
  await expect(control).toHaveText("标准");
  await control.click();
  await page.getByRole("menuitemradio", { name: /标准/ }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "set_service_tier").length).toBe(1);
  expect(relay.commands.find(c => c.type === "set_service_tier")).toMatchObject({ sid: "btw-layout-child", service_tier: "default" });
  await expect(control).toHaveText("标准");
  relay.emit({ type: "fast", sid: "btw-layout-child", on: true });
  await expect(control).toHaveText("快速");
  await expect(page.locator(".composer .fast-chip")).toHaveText("标准");
  await panel.getByRole("textbox").fill("/fast");
  await panel.getByRole("textbox").press("Enter");
  await expect.poll(() => relay.commands.filter(c => c.type === "set_service_tier").length).toBe(2);
  expect(relay.commands.filter(c => c.type === "set_service_tier").every(c => c.sid === "btw-layout-child")).toBe(true);
  expect(relay.commands.some(c => c.type === "query" || c.type === "steer")).toBe(false);
});

test("destroyed BTW stays readable after refresh with disabled input and a working new-chat button", async ({ page }, testInfo) => {
  const relay = await mockRightPanelRelay(page, { visible: true, btwReadOnly: true });
  await page.goto("/");
  const panel = page.locator(".btw-panel");
  const reason = "当前临时对话已经销毁，请创建新的临时对话";
  for (let i = 0; i < 2; i++) {
    if (i) await page.reload();
    await expect(panel).toContainText("以前的侧边提问");
    await expect(panel).toContainText("仍可查看的侧边回答");
    await expect(panel.getByRole("status")).toHaveText(reason);
    await expect(panel.getByRole("textbox")).toBeDisabled();
    await expect(panel.getByRole("textbox")).toHaveAttribute("placeholder", reason);
    await expect(panel.locator(".btw-send")).toBeDisabled();
    await expect(panel.locator(".btw-controls button").first()).toBeDisabled();
    await expect(panel.locator(".btw-controls button").last()).toBeDisabled();
    await expect(panel.getByRole("button", { name: "添加附件", exact: true })).toBeDisabled();
    await panel.evaluate(node => {
      const data = new DataTransfer();
      data.items.add(new File(["locked"], "locked.txt", { type: "text/plain" }));
      node.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: data }));
    });
    await expect(page.locator(".attach-file")).toHaveCount(0);
    await expect(panel.getByRole("button", { name: "新建侧边对话" })).toBeEnabled();
    await expect(panel.locator(".btw-chat-close")).toBeEnabled();
  }
  expect(relay.commands.filter((c) => ["query", "steer", "thread/fork"].includes(String(c.type))))
    .toHaveLength(0);
  await panel.screenshot({ path: testInfo.outputPath("btw-destroyed.png") });
  await panel.getByRole("button", { name: "新建侧边对话" }).click();
  await expect.poll(() => relay.commands.filter((c) => c.type === "open_btw").length).toBe(1);
  expect(relay.commands.find((c) => c.type === "open_btw")?.sid).toBe("layout-parent");
});

test("destroyed BTW live control preserves drafts and rejects stale writable snapshots", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { visible: true });
  await page.goto("/");
  const panel = page.locator(".btw-panel");
  const input = panel.getByRole("textbox");
  await expect(input).toBeEnabled();
  await input.fill("失效时不要清除我的草稿");
  const control = {
    type: "session_control" as const, sid: "btw-layout-child",
    generation: "layout-generation", revision: 2,
    control_mode: "codex_shared" as const, write_state: "read_only" as const,
    terminal_attached: false, can_takeover: false,
    reason: "当前临时对话已经销毁，请创建新的临时对话",
  };
  relay.emit(control);
  await expect(input).toBeDisabled();
  await expect(input).toHaveValue("失效时不要清除我的草稿");
  relay.emit({ ...control, revision: 1, write_state: "writable", reason: null });
  await expect(input).toBeDisabled();
  await expect(panel.locator(".btw-runbar")).toHaveCount(0);
  await expect(panel.locator(".btw-send")).toBeDisabled();
  // Selection/copy and manual close remain available; no automatic fork or send.
  expect(relay.commands.filter((c) => ["query", "steer", "open_btw"].includes(String(c.type))))
    .toHaveLength(0);
  page.on("dialog", (dialog) => dialog.accept());
  await panel.locator(".btw-chat-close").click();
  await expect.poll(() => relay.commands.filter((c) => c.type === "close_btw").length).toBe(1);
  expect(relay.commands.find((c) => c.type === "close_btw")?.sid).toBe("btw-layout-child");
});

test("transient BTW reconnect notice does not permanently disable the composer", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { visible: true });
  await page.goto("/");
  const panel = page.locator(".btw-panel");
  await expect(panel.getByRole("textbox")).toBeEnabled();
  relay.emit({ type: "session_control", sid: "btw-layout-child", generation: "layout-generation",
    revision: 1, control_mode: "codex_shared", write_state: "writable",
    terminal_attached: false, can_takeover: false, reason: "共享通道正在重新连接" });
  await expect(panel.getByRole("textbox")).toBeEnabled();
  await expect(panel.locator(".btw-readonly-notice")).toHaveCount(0);
});

async function openAsyncQuestion(page: import("@playwright/test").Page, index?: number) {
  const dialog = page.getByRole("dialog", { name: "Codex 想确认一下", exact: true });
  const resume = page.locator(".question-resume").filter({ hasText: /等待你回答|回答提交中/ });
  await expect.poll(async () => await dialog.isVisible() || await resume.isVisible()).toBe(true);
  if (!await dialog.isVisible()) await resume.click();
  await expect(dialog).toBeVisible();
  if (index !== undefined) {
    const progress = dialog.locator(".qa-progress");
    let current = Number((await progress.textContent())?.split("/")[0].trim() ?? "1") - 1;
    while (current !== index) {
      await dialog.getByRole("button", { name: current < index ? "下一题" : "上一题" }).click();
      current += current < index ? 1 : -1;
    }
  }
  return dialog;
}

const ASYNC_QUESTIONS = [
  { title: "在哪个设备发生？", options: ["Mac", "手机"] },
  { title: "用的是什么手势？", options: null },
];
const ASYNC_HISTORY_TURN = {
  id: "async-user", prompt: "继续修复", done: true, detailLoaded: true,
  forkPointId: "async-task", blocks: [{ kind: "text", message_id: "async-item",
    text: "在哪个设备发生？用的是什么手势？", channel: "final", done: true,
    delivery: "async", questions: [ASYNC_QUESTIONS[1]] }],
};

const TEST_GENERATED_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j6RkAAAAASUVORK5CYII=";

test("async question compact styling matches the shared dialog on desktop and mobile", async ({ page }, testInfo) => {
  const title = "这几次恢复，是你在板子失联后手动重启或断电的吗？失联前是否主动重启或改 IP？";
  await mockRightPanelRelay(page, { seedTurns: [{
    ...ASYNC_HISTORY_TURN, blocks: [{ ...ASYNC_HISTORY_TURN.blocks[0],
      text: title, questions: [{ title, options: null }] }],
  }] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  await expect(page.locator(".async-question-card")).toHaveCount(0);
  await expect(dialog.locator(".engine-brand-icon.codex")).toHaveCount(1);
  await expect(dialog.getByText(title, { exact: true })).toHaveCount(1);
  const bounds = (await dialog.boundingBox())!;
  expect(bounds.width).toBeLessThanOrEqual(560);
  expect(bounds.y).toBeGreaterThanOrEqual(0);
  expect(bounds.y + bounds.height).toBeLessThanOrEqual(page.viewportSize()!.height);
  expect(await dialog.evaluate(node => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
  await expect(dialog).toHaveCSS("border-radius", "24px");
  await expect(dialog.getByRole("textbox")).not.toBeFocused();
  await dialog.getByRole("textbox").fill("三指拖拽");
  await page.screenshot({ path: testInfo.outputPath("async-question-compact.png") });
  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await page.screenshot({ path: testInfo.outputPath("async-question-compact-dark.png") });
  await expect(dialog.locator("details, .async-question-source, .async-question-original")).toHaveCount(0);
  await expect(dialog.locator(".async-question-hint")).toHaveCount(0);
  await expect(dialog.getByRole("textbox")).toHaveValue("三指拖拽");
});

test("active process text shimmers and readable tool output settles without replaying animation", async ({ page }, info) => {
  const relay = await mockRightPanelRelay(page, { engine: "claude", historyReply: () => null });
  await page.goto("/");
  await expect.poll(() => relay.commands.some((c) => c.type === "get_history")).toBe(true);
  const sid = "layout-parent";
  relay.emit({ type: "user_msg", sid, msg_id: "shimmer-user", prompt: "检查输出显示" });
  relay.emit({ type: "turn_binding", sid, msg_id: "shimmer-user", turn_id: "shimmer-user" });
  relay.emit({ type: "state", sid, state: "running" });
  relay.emit({ type: "tool_use", sid, message_id: "command-message", tool_use_id: "readable-command",
    turn_id: "shimmer-user", tool: "Bash", category: "command", title: "运行命令",
    input: { command: "/bin/zsh -lc 'rg -n needle src'", cwd: "/repo" } });
  const turn = page.locator('.turn[data-turn-id="shimmer-user"]');
  const label = turn.locator(".turn-process-label");
  const tools = turn.locator(".tool-group-label");
  await expect(label).toHaveClass(/is-active/);
  await expect(tools).toHaveClass(/is-active/);
  await expect(label).toHaveCSS("animation-name", "status-text-sweep");
  await expect(turn.locator(".tool-group-ic, .turn-process-state")).toHaveCount(0);
  const before = await label.evaluate((node) => getComputedStyle(node).backgroundPositionX);
  await expect.poll(() => label.evaluate((node) => getComputedStyle(node).backgroundPositionX))
    .not.toBe(before);
  await turn.locator(".tool-group-h").click();
  const card = turn.locator(".tool");
  await expect(card.locator(".tool-arg")).toHaveText("rg -n needle src");
  await card.locator(".tool-h").click();
  await expect(card.locator(".tool-pre").first()).toHaveText("rg -n needle src");
  const raw = JSON.stringify({ chunk_id: "transport-chunk", output: "src/main.ts:12: needle", exit_code: 0 });
  relay.emit({ type: "tool_result", sid, tool_use_id: "readable-command", content: raw,
    turn_id: "shimmer-user", is_error: false });
  await expect(card.locator(".tool-pre").last()).toHaveText("src/main.ts:12: needle");
  await expect(card).not.toContainText("transport-chunk");
  await card.getByText("原始结果", { exact: true }).click();
  await expect(card).toContainText("transport-chunk");
  await card.getByText("原始结果", { exact: true }).click();
  await expect(tools).not.toHaveClass(/is-active/);
  await expect(label).toHaveClass(/is-active/);
  await page.screenshot({ path: info.outputPath("text-shimmer-light.png") });
  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await page.screenshot({ path: info.outputPath("text-shimmer-dark.png") });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(label).toHaveCSS("animation-name", "none");
  await expect(label).not.toHaveCSS("-webkit-text-fill-color", "rgba(0, 0, 0, 0)");
  await page.emulateMedia({ reducedMotion: "no-preference" });
  relay.emit({ type: "turn_end", sid, turn_id: "shimmer-user",
    result: { subtype: "success", duration_ms: 5000, is_error: false } });
  relay.emit({ type: "state", sid, state: "idle" });
  await expect(label).toContainText("已处理");
  await expect(turn.locator(".status-shimmer.is-active")).toHaveCount(0);
});

test("Claude resumes a separate live process after an idle background task", async ({ page }, info) => {
  const relay = await mockRightPanelRelay(page, { engine: "claude", historyReply: () => null });
  let seq = 0;
  const emit = (message: PanelRelayEvent) => relay.emit({ ...message, seq: ++seq });
  await page.goto("/");
  await expect.poll(() => relay.commands.some((c) => c.type === "get_history")).toBe(true);
  const sid = "layout-parent";
  const turnId = "background-parent";
  emit({ type: "user_msg", sid, msg_id: turnId, prompt: "检查两个任务" });
  emit({ type: "turn_binding", sid, msg_id: turnId, turn_id: turnId });
  emit({ type: "state", sid, state: "running" });
  emit({ type: "process", sid, turn_id: turnId, item_id: "agent-a", kind: "agent",
    phase: "start", status: "running", title: "检查结构", background: true });
  emit({ type: "assistant_msg_start", sid, turn_id: turnId, message_id: "interim", channel: "final" });
  emit({ type: "delta", sid, turn_id: turnId, message_id: "interim", channel: "final", text: "中间结果先给你，两路探索还在跑。" });
  emit({ type: "assistant_msg_end", sid, turn_id: turnId, message_id: "interim", channel: "final" });
  emit({ type: "turn_end", sid, turn_id: turnId, result: { subtype: "success", duration_ms: 3000, is_error: false } });
  emit({ type: "state", sid, state: "idle" });
  const turn = page.locator(`.turn[data-turn-id="${turnId}"]`);
  await expect(turn.locator(".turn-working")).toHaveCount(0);
  await expect(turn.locator(".turn-process-head").first()).toHaveAttribute("aria-expanded", "false");
  emit({ type: "process", sid, turn_id: turnId, item_id: "agent-a", kind: "agent",
    phase: "end", status: "succeeded", title: "检查结构", summary: "Raw agent report must stay in details", background: true });
  await expect(turn.locator(".turn-working")).toHaveCount(0);
  await expect(turn.locator(".background-followup")).toHaveCount(0);

  emit({ type: "state", sid, state: "running", msg_id: turnId, continuation: true });
  await expect(turn.locator(".turn-working")).toBeVisible();
  emit({ type: "assistant_msg_start", sid, turn_id: turnId, message_id: "follow-thinking", channel: "thinking", background: true });
  emit({ type: "delta", sid, turn_id: turnId, message_id: "follow-thinking", channel: "thinking", text: "Fixture continuation reasoning", background: true });
  const continuation = turn.locator(".background-followup").first();
  await expect(turn.locator(".turn-working")).toBeVisible();
  await expect(continuation.locator(".turn-process-head")).toHaveAttribute("aria-expanded", "true");
  await expect(continuation.locator(".turn-process-label")).toHaveClass(/is-active/);
  await expect(continuation.locator(".turn-process-label")).toContainText("正在处理");
  const workingAnimation = turn.locator(".turn-working .claude-working-mark img");
  const frame = () => workingAnimation.evaluate(node => getComputedStyle(node).transform);
  const initialFrame = await frame();
  await expect.poll(frame).not.toBe(initialFrame);
  await expect(continuation.locator(".process-reasoning")).toBeVisible();
  await expect(turn.locator(".turn-process-head").first()).toHaveAttribute("aria-expanded", "false");
  await expect(continuation).not.toContainText("Raw agent report");
  emit({ type: "assistant_msg_end", sid, turn_id: turnId, message_id: "follow-thinking", channel: "thinking", background: true });
  emit({ type: "tool_use", sid, turn_id: turnId, message_id: "follow-tool", tool_use_id: "verify", tool: "Bash", category: "command", input: { command: "verify camera" }, background: true });
  await expect(continuation.locator(".tool-group-label")).toContainText("正在调用 1 个工具");
  await page.screenshot({ path: info.outputPath("claude-live-continuation.png") });
  emit({ type: "tool_result", sid, turn_id: turnId, tool_use_id: "verify", content: "Verified", is_error: false, background: true });
  await expect(continuation.locator(".turn-process-label")).toContainText("正在处理");
  emit({ type: "assistant_msg_start", sid, turn_id: turnId, message_id: "follow-answer", channel: "final", background: true });
  emit({ type: "delta", sid, turn_id: turnId, message_id: "follow-answer", channel: "final", text: "第一路检查完成。", background: true });
  emit({ type: "assistant_msg_end", sid, turn_id: turnId, message_id: "follow-answer", channel: "final", background: true });
  emit({ type: "state", sid, state: "idle" });
  await expect(turn.locator(".turn-working")).toHaveCount(0);
  await expect(continuation.locator(".turn-process-head")).toHaveAttribute("aria-expanded", "false");
  await expect(continuation).toContainText("第一路检查完成。");

  emit({ type: "state", sid, state: "running", msg_id: turnId, continuation: true });
  emit({ type: "assistant_msg_start", sid, turn_id: turnId, message_id: "second-thinking", channel: "thinking", background: true });
  emit({ type: "delta", sid, turn_id: turnId, message_id: "second-thinking", channel: "thinking", text: "Fixture second continuation", background: true });
  await expect(turn.locator(".background-followup")).toHaveCount(2);
  await expect(turn.locator(".background-followup").last().locator(".turn-process-head")).toHaveAttribute("aria-expanded", "true");
  await expect(continuation.locator(".turn-process-head")).toHaveAttribute("aria-expanded", "false");
  await expect(turn.locator(".turn-working")).toHaveCount(1);
});

test("Claude continuation survives device round trips without another model token", async ({ page }) => {
  const sid = "shared-session", turnId = "native-parent";
  const commands: { machine: string; type: string; [key: string]: unknown }[] = [];
  const emitters = new Map<string, (event: Record<string, unknown>) => void>();
  let running = true;
  let build = 0;
  await page.addInitScript(() => {
    if (!localStorage.getItem("cc_remote_machine")) {
      localStorage.setItem("cc_remote_machine", "desktop");
    }
    localStorage.setItem("cc_remote_engine", "claude");
  });
  await page.route("**/api/**", route => route.fulfill({ status: 404 }));
  await page.route("**/api/session", route => route.fulfill({ json: {} }));
  await page.route("**/api/devices", route => route.fulfill({ json: {
    devices: ["desktop", "us-vps"].map(machine_id => ({
      machine_id, label: machine_id, online: true,
    })),
  } }));
  await page.routeWebSocket(/\/ws(?:\?|$)/, socket => {
    const machine = new URL(socket.url()).searchParams.get("machine")!;
    const local = machine === "desktop";
    const generation = `generation-${machine}`;
    const emit = (event: Record<string, unknown>) => socket.send(JSON.stringify({
      v: PROTOCOL_VERSION, ts: 10, sid, ...event,
    }));
    emitters.set(machine, emit);
    const seed = () => {
      emit({ type: "snapshot", cc_session_id: sid,
        state: local && running ? "running" : "idle", generation, tail_text: "" });
      emit({ type: "replay_start", generation, from_seq: 101, to_seq: 100, truncated: true });
      emit({ type: "replay_end", to_seq: 100, truncated: true });
      if (local && running) emit({ type: "state", state: "running",
        msg_id: "browser-parent", continuation: true });
    };
    socket.onMessage(raw => {
      const command = JSON.parse(String(raw));
      commands.push({ ...command, machine });
      if (command.type === "hello") seed();
      else if (command.type === "list_sessions") emit({ type: "session_list",
        engine: command.engine, space: command.space, request_id: command.cmd_id,
        sessions: command.space === "code" && command.engine === "claude" ? [{
          session_id: sid, summary: `${machine} session`, engine: "claude",
          space: "code", cwd: "/tmp/restore", state: local && running ? "running" : "idle",
        }] : [],
      });
      else if (command.type === "switch_session") {
        emit({ type: "session_focus", session_id: sid });
      } else if (command.type === "get_history") emit({ type: "history",
        session_id: sid, revision: `revision-${machine}`, generation,
        build_seq: ++build, live_seq: 100, authoritative: true,
        in_progress: local && running, newest_id: turnId, has_more: false,
        detail: "summary", events: [], turns: [{ id: turnId,
          clientMsgId: "browser-parent", prompt: `${machine} request`, done: true,
          detailLoaded: false, processDetailState: "present", detailEventCount: 165,
          blocks: [{ kind: "text", message_id: "initial-answer", channel: "final",
            text: local ? "原回复已经结束，等待子代理。" : "VPS 的历史回复", done: true }],
        }],
      });
      else if (command.type === "get_turn_detail") emit({ type: "turn_detail",
        session_id: sid, turn_id: turnId, revision: `revision-${machine}`,
        authoritative: true, has_more: true, oldest_cursor: "older-detail",
        has_newer: false, events: [
          { v: PROTOCOL_VERSION, ts: 1, type: "user_msg", msg_id: turnId,
            prompt: `${machine} request` },
          { v: PROTOCOL_VERSION, ts: 8, type: "assistant_msg_start",
            message_id: "continuation-thinking", turn_id: turnId,
            channel: "thinking", background: true },
          { v: PROTOCOL_VERSION, ts: 8, type: "delta",
            message_id: "continuation-thinking", turn_id: turnId,
            channel: "thinking", background: true, text: "正在续办，检查剩余文件。" },
        ],
      });
      else if (command.type === "get_goal") emit({ type: "goal_state",
        request_id: command.cmd_id, goal: null });
      else if (command.type === "ping") emit({ type: "pong", n: command.n });
    });
  });
  const checkContinuation = async () => {
    await expect(page.getByText("正在续办，检查剩余文件。", { exact: true })).toBeVisible();
    await expect(page.locator(".turn-working")).toHaveCount(1);
    await expect(page.getByText("VPS 的历史回复", { exact: true })).toHaveCount(0);
  };
  const switchDevice = async (name: string) => {
    await page.getByRole("button", { name: "设备中心", exact: true }).click();
    await page.locator(".device-main").filter({ hasText: name }).click();
  };
  await page.goto("/");
  await checkContinuation();
  await page.reload();
  await checkContinuation();
  for (let round = 0; round < 2; round++) {
    await switchDevice("us-vps");
    await expect(page.getByText("VPS 的历史回复", { exact: true })).toBeVisible();
    await expect(page.getByText("正在续办，检查剩余文件。", { exact: true })).toHaveCount(0);
    await expect(page.locator(".turn-working")).toHaveCount(0);
    await switchDevice("desktop");
    await checkContinuation();
  }
  running = false;
  emitters.get("desktop")!({ type: "state", state: "idle", seq: 101 });
  await expect(page.locator(".turn-working")).toHaveCount(0);
  expect(commands.filter(c => c.type === "get_turn_detail").every(c =>
    c.machine === "desktop" && !c.before)).toBe(true);
  expect(commands.some(c => ["query", "steer", "interrupt"].includes(c.type))).toBe(false);
});

test("Claude overlapping return links recover from legacy live order across refresh and session switches", async ({ page }, info) => {
  const sid = "layout-parent", turnId = "overlapping-parent";
  let sourceReady = false;
  const returned = (id: string) => ({ kind: "process" as const, item_id: `return-${id}`,
    processKind: "agent" as const, phase: "end" as const, status: "succeeded" as const,
    done: true, title: `检查 ${id}`, server: "cc_remote", tool: "agent_return",
    input: { agent_run_id: `child-${id}` }, startedTs: id === "A" ? 20_000 : 40_000 });
  const answer = (id: string) => ({ kind: "text" as const, message_id: `answer-${id}`,
    channel: "final" as const, text: `检查 ${id} 的回复。`, done: true, background: true,
    startedTs: id === "A" ? 30_000 : 50_000 });
  const source = () => ({ type: "history" as const, sid, session_id: sid,
    revision: "overlapping-history", generation: "layout-generation", authoritative: true,
    detail: "summary" as const, newest_id: turnId, events: [], has_more: false, turns: [{
      id: turnId, prompt: "并行检查", done: true, detailLoaded: false,
      processDetailState: "none" as const, detailEventCount: 0,
      blocks: [returned("A"), answer("A"), returned("B"), answer("B")],
    }] });
  const relay = await mockRightPanelRelay(page, { engine: "claude", secondParent: true,
    historyReply: command => command.session_id === sid
      ? sourceReady ? source() : null
      : { type: "history", session_id: String(command.session_id),
        revision: "other-history", events: [], turns: [], has_more: false } });
  await page.goto("/");
  await expect.poll(() => relay.commands.some(command => command.type === "get_history")).toBe(true);
  relay.emit({ type: "user_msg", sid, msg_id: turnId, prompt: "并行检查" });
  relay.emit({ type: "turn_binding", sid, msg_id: turnId, turn_id: turnId });
  // Replay the old client's arrival-ordered metadata, before either reply.
  for (const id of ["A", "B"]) relay.emit({ type: "process", sid, turn_id: turnId,
    item_id: `return-${id}`, kind: "agent", phase: "end", status: "succeeded",
    title: `Running ${id}`, server: "cc_remote", tool: "agent_return",
    input: { agent_run_id: `child-${id}` }, background: true });
  for (const id of ["A", "B"]) {
    relay.emit({ type: "assistant_msg_start", sid, turn_id: turnId,
      message_id: `answer-${id}`, channel: "final", background: true });
    relay.emit({ type: "delta", sid, turn_id: turnId, message_id: `answer-${id}`,
      channel: "final", text: `检查 ${id} 的回复。`, background: true });
    relay.emit({ type: "assistant_msg_end", sid, turn_id: turnId,
      message_id: `answer-${id}`, channel: "final", background: true });
  }
  relay.emit({ type: "turn_end", sid, turn_id: "answer-B", checkpoint_id: turnId,
    result: { subtype: "success", duration_ms: 1000, is_error: false } });
  const followups = page.locator(`.turn[data-turn-id="${turnId}"] .background-followup`);
  await expect(followups.first().locator(".agent-return-link")).toHaveCount(2);
  sourceReady = true;
  relay.emit(source());
  const check = async () => {
    await expect(followups).toHaveCount(2);
    for (const [index, id] of ["A", "B"].entries()) {
      await expect(followups.nth(index).locator(".agent-return-link")).toHaveText(`代理返回 · 检查 ${id}`);
      await expect(followups.nth(index)).toContainText(`检查 ${id} 的回复。`);
    }
  };
  await check();
  await page.reload();
  await check();
  if (!(await page.locator(".shell").evaluate(el => el.classList.contains("sidebar-open")))) {
    await page.getByLabel("显示会话侧栏", { exact: true }).click();
  }
  await page.getByText("Second parent", { exact: true }).click();
  if (!(await page.locator(".shell").evaluate(el => el.classList.contains("sidebar-open")))) {
    await page.getByLabel("显示会话侧栏", { exact: true }).click();
  }
  await page.getByText("Layout parent", { exact: true }).click();
  if (await page.locator(".shell").evaluate(el => el.classList.contains("sidebar-open"))) {
    await page.getByLabel("显示会话侧栏", { exact: true }).click();
  }
  await check();
  await page.locator(".code-pane").evaluate(el => Promise.all(
    el.getAnimations().map(animation => animation.finished)));
  await page.screenshot({ path: info.outputPath("corrected-claude-return-links.png") });
  await followups.last().locator(".agent-return-link").click();
  await expect.poll(() => relay.commands.some(command => command.type === "get_agent_detail"
    && command.run_id === "child-B")).toBe(true);
  expect(relay.commands.some(command => ["query", "steer", "interrupt"].includes(String(command.type)))).toBe(false);
});

test("Claude return link survives refresh and a later detail window", async ({ page }) => {
  const sid = "layout-parent", turnId = "return-parent";
  const seedTurns = [{ id: turnId, prompt: "检查三个子代理", done: true,
    processDetailState: "present" as const, detailEventCount: 10, blocks: [
      { kind: "text" as const, message_id: "return-before", channel: "final" as const,
        text: "三个检查已启动。", done: true },
      { kind: "process" as const, item_id: "return-child", processKind: "agent" as const,
        phase: "end" as const, status: "succeeded" as const, done: true,
        title: "接口检查", server: "cc_remote", tool: "agent_return",
        input: { agent_run_id: "child-review" } },
      { kind: "text" as const, message_id: "return-after", channel: "final" as const,
        text: "接口检查完成，继续处理。", done: true, background: true },
    ] }];
  const relay = await mockRightPanelRelay(page, { engine: "claude", seedTurns });
  for (let round = 0; round < 2; round++) {
    const start = relay.commands.length;
    if (round) await page.reload(); else await page.goto("/");
    await expect.poll(() => relay.commands.slice(start).some(c => c.type === "get_history")).toBe(true);
    const link = page.locator(".background-followup .agent-return-link");
    await expect(link).toHaveText("代理返回 · 接口检查");
    relay.emit({ type: "turn_detail", session_id: sid, turn_id: turnId,
      revision: "layout-history", authoritative: true, has_more: true,
      oldest_cursor: "earlier-detail", has_newer: false, events: [
        { v: PROTOCOL_VERSION, ts: 1, type: "user_msg", msg_id: turnId, prompt: "检查三个子代理" },
        { v: PROTOCOL_VERSION, ts: 3, type: "assistant_msg_start", message_id: "return-after", channel: "final", background: true },
        { v: PROTOCOL_VERSION, ts: 3, type: "delta", message_id: "return-after", text: "接口检查完成，继续处理。", channel: "final", background: true },
        { v: PROTOCOL_VERSION, ts: 3, type: "assistant_msg_end", message_id: "return-after", channel: "final", background: true },
      ] });
    await expect(link).toHaveText("代理返回 · 接口检查");
    await expect(page.locator(".background-followup")).toHaveCount(1);
    const clicked = relay.commands.length;
    await link.click();
    await expect.poll(() => relay.commands.slice(clicked).some(c => c.type === "get_agent_detail"
      && c.run_id === "child-review")).toBe(true);
  }
});

test("Codex agent detail expands parent reports and still opens nested children", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { engine: "codex" });
  await page.goto("/");
  await expect.poll(() => relay.commands.some((c) => c.type === "get_history")).toBe(true);
  const sid = "layout-parent";
  relay.emit({ type: "user_msg", sid, msg_id: "parent-turn", prompt: "检查周报" });
  relay.emit({ type: "process", sid, turn_id: "parent-turn", item_id: "native-activity", kind: "agent",
    phase: "end", status: "succeeded", title: "协作代理有新进展", summary: "/root/weekly_solutions",
    input: { agent_run_id: "codex-agent:child-thread" } });
  const card = page.locator(".process-agent-card");
  await expect(card).toBeVisible();
  await card.click();
  const requests = () => relay.commands.filter((c) => c.type === "get_agent_detail");
  await expect.poll(() => requests().length).toBe(1);
  expect(requests()[0].run_id).toBe("codex-agent:child-thread");
  relay.emit({ type: "agent_detail", sid, session_id: sid, run_id: "codex-agent:child-thread",
    request_id: String(requests()[0].request_id), revision: "layout-history", detail_revision: "source-child",
    authoritative: true, title: "/root/weekly_solutions", status: "succeeded", events: [
      { v: PROTOCOL_VERSION, ts: 1, type: "tool_use", tool_use_id: "child-tool", message_id: "child-work",
        tool: "shell", category: "command", input: { command: "pwd" }, title: "核对工作目录" },
      { v: PROTOCOL_VERSION, ts: 2, type: "tool_result", tool_use_id: "child-tool", content: "/workspace", is_error: false },
      { v: PROTOCOL_VERSION, ts: 2, type: "process", item_id: "parent-report", kind: "agent",
        phase: "end", status: "succeeded", title: "向主代理汇报", summary: "/root",
        detail: "已核对周报来源，以下是汇报正文。", input: { agent_run_id: null } },
      { v: PROTOCOL_VERSION, ts: 2, type: "process", item_id: "grandchild-activity", kind: "agent",
        phase: "end", status: "succeeded", title: "检查周报来源", summary: "/root/weekly_solutions/sources",
        input: { agent_run_id: "codex-agent:grandchild-thread" } },
      { v: PROTOCOL_VERSION, ts: 3, type: "assistant_msg_start", message_id: "child-answer", channel: "final" },
      { v: PROTOCOL_VERSION, ts: 3, type: "delta", message_id: "child-answer", channel: "final", text: "周报方案核对完成" },
      { v: PROTOCOL_VERSION, ts: 3, type: "assistant_msg_end", message_id: "child-answer", channel: "final" },
    ], through_seq: 0, has_more: false });
  const panel = page.getByRole("complementary", { name: "协作代理详情" });
  await expect(panel).toContainText("周报方案核对完成");
  await expect(panel.locator(".agent-detail-status")).toHaveText("已完成");
  await panel.locator(".tool-group-h").click();
  await expect(panel).toContainText("pwd");
  const report = panel.locator(".process-activity").filter({ hasText: "向主代理汇报" });
  await expect(report.getByText("已核对周报来源，以下是汇报正文。", { exact: true })).not.toBeVisible();
  await report.locator("summary").click();
  await expect(report.getByText("已核对周报来源，以下是汇报正文。", { exact: true })).toBeVisible();
  await expect(report).not.toContainText("agent_run_id");
  await expect(report).not.toContainText("原始参数");
  expect(requests()).toHaveLength(1);
  await expect(panel.getByRole("alert")).toHaveCount(0);
  await panel.getByRole("button", { name: /检查周报来源/ }).click();
  await expect.poll(() => requests().length).toBe(2);
  expect(requests()[1].run_id).toBe("codex-agent:grandchild-thread");
  expect(requests()[1].session_id).toBe(sid);
  relay.emit({ type: "agent_detail", sid, session_id: sid, run_id: "codex-agent:grandchild-thread",
    request_id: String(requests()[1].request_id), revision: "layout-history", detail_revision: "source-grandchild",
    authoritative: true, title: "/root/weekly_solutions/sources", status: "succeeded", events: [
      { v: PROTOCOL_VERSION, ts: 4, type: "assistant_msg_start", message_id: "grandchild-answer", channel: "final" },
      { v: PROTOCOL_VERSION, ts: 4, type: "delta", message_id: "grandchild-answer", channel: "final", text: "下级代理核对完成" },
      { v: PROTOCOL_VERSION, ts: 4, type: "assistant_msg_end", message_id: "grandchild-answer", channel: "final" },
    ], through_seq: 0, has_more: false });
  await expect(panel).toContainText("下级代理核对完成");
  await panel.getByRole("button", { name: "返回上一级协作代理" }).click();
  await expect(panel).toContainText("周报方案核对完成");
  await expect(panel.getByRole("alert")).toHaveCount(0);
  expect(requests()).toHaveLength(2);
  expect(relay.commands.filter((c) => ["query", "session_resume", "steer"].includes(String(c.type)))).toEqual([]);
});

test("Claude agent detail recovers stale revisions and follows its own live status", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { engine: "claude" });
  await page.goto("/");
  await expect.poll(() => relay.commands.some((c) => c.type === "get_history")).toBe(true);
  const sid = "layout-parent";
  relay.emit({ type: "user_msg", sid, msg_id: "agent-parent", prompt: "运行子代理" });
  relay.emit({ type: "process", sid, turn_id: "agent-parent", item_id: "agent-detail-run", kind: "agent",
    phase: "start", status: "running", title: "检查相机链路", background: true });
  await page.locator(".process-agent-card").click();
  const panel = page.getByRole("complementary", { name: "协作代理详情" });
  await expect(panel.locator(".agent-detail-working")).toBeVisible();
  const requests = () => relay.commands.filter((c) => c.type === "get_agent_detail");
  await expect.poll(() => requests().length).toBe(1);
  const reply = (extra: Record<string, unknown> = {}) => relay.emit({
    type: "agent_detail", sid, session_id: sid, run_id: "agent-detail-run",
    request_id: String(requests().at(-1)!.request_id), revision: "fresh-history",
    detail_revision: "live-agent-1", authoritative: true, title: "检查相机链路",
    status: "running", events: [], through_seq: 0, ...extra,
  } as PanelRelayEvent<Extract<ServerEvent, { type: "agent_detail" }>>);
  reply({ authoritative: false, error: "会话历史已更新，请重新打开协作代理" });
  await expect(panel.getByRole("alert")).toContainText("会话记录已更新");
  await expect(panel).not.toContainText("正在读取协作代理过程");
  await panel.getByRole("button", { name: "重试", exact: true }).click();
  await expect.poll(() => requests().length).toBe(2);
  expect(requests().at(-1)!.revision).toBeUndefined();
  expect(requests().at(-1)!.detail_revision).toBeUndefined();
  reply();
  await expect(panel.locator(".agent-detail-status")).toHaveText("运行中");
  await expect(panel.locator(".agent-detail-working")).toBeVisible();
  reply({ live: true, request_id: null, through_seq: 1, events: [
    { v: PROTOCOL_VERSION, ts: 10, type: "assistant_msg_start", message_id: "child-note", channel: "commentary" },
    { v: PROTOCOL_VERSION, ts: 10, type: "delta", message_id: "child-note", channel: "commentary", text: "子代理正在核对驱动接口" },
  ] });
  await expect(panel).toContainText("子代理正在核对驱动接口");
  reply({ live: true, request_id: null, through_seq: 1, status: "succeeded", events: [] });
  await expect(panel.locator(".agent-detail-status")).toHaveText("已完成");
  await expect(panel.locator(".agent-detail-working")).toHaveCount(0);
});

test("Claude agent detail reads a fresh revision and refreshes active native history", async ({ page }) => {
  await page.clock.install();
  const relay = await mockRightPanelRelay(page, { engine: "claude" });
  await page.goto("/");
  await expect.poll(() => relay.commands.some((c) => c.type === "get_history")).toBe(true);
  const sid = "layout-parent";
  relay.emit({ type: "user_msg", sid, msg_id: "native-agent-parent", prompt: "查看原生子代理" });
  relay.emit({ type: "process", sid, turn_id: "native-agent-parent", item_id: "native-agent", kind: "agent",
    phase: "start", status: "running", title: "原生子代理", background: true });
  await page.locator(".process-agent-card").click();
  const panel = page.getByRole("complementary", { name: "协作代理详情" });
  const requests = () => relay.commands.filter((c) => c.type === "get_agent_detail");
  await expect.poll(() => requests().length).toBe(1);
  expect(requests()[0].revision).toBeUndefined();
  const reply = (status: "running" | "succeeded", detailRevision: string) => relay.emit({
    type: "agent_detail", sid, session_id: sid, run_id: "native-agent",
    request_id: String(requests().at(-1)!.request_id), revision: "new-native-history",
    detail_revision: detailRevision, authoritative: true, title: "原生子代理",
    status, events: [], through_seq: 0,
  });
  reply("running", "source-version-1");
  await expect(panel.locator(".agent-detail-status")).toHaveText("运行中");
  await expect(panel.getByRole("alert")).toHaveCount(0);
  await page.clock.fastForward(3500);
  await expect.poll(() => requests().length).toBe(2);
  expect(requests().at(-1)!.detail_revision).toBeUndefined();
  reply("succeeded", "source-version-2");
  await expect(panel.locator(".agent-detail-status")).toHaveText("已完成");
  await expect(panel.locator(".agent-detail-working")).toHaveCount(0);
  await page.clock.fastForward(7000);
  expect(requests()).toHaveLength(2);
});

for (const extension of ["mmd", "MERMAID"]) {
  test(`Work Mermaid artifact opens a diagram and its source (${extension})`, async ({ page }) => {
    await page.goto(`/tests/history-browser.html?artifact-mermaid=1&ext=${extension}`);
    const sheet = page.getByRole("dialog", { name: "Artifacts", exact: true });
    const file = sheet.getByRole("button", { name: new RegExp(`camera_navigation_pipeline\\.${extension}`) });
    await expect(file).toBeEnabled();
    await expect(file).toContainText("Mermaid 图表");
    await file.click();
    const panel = page.locator(".artifact-panel");
    await expect(panel.locator(".mermaid-svg svg")).toBeVisible();
    await expect(panel.locator(".mermaid-svg")).toContainText("双目相机");
    await expect(panel.locator(".mermaid-svg")).toContainText("路径规划");
    await panel.getByRole("button", { name: "源码", exact: true }).click();
    await expect(panel.locator(".mermaid-svg")).toHaveCount(0);
    await expect(panel.locator(".artifact-body")).toContainText("flowchart TD");
    await panel.getByRole("button", { name: "预览", exact: true }).click();
    await expect(panel.locator(".mermaid-svg svg")).toBeVisible();
  });
}

for (const engine of ["claude", "codex"]) {
  test(`${engine} agent detail accepts an immediate correlated response`, async ({ page }) => {
    await page.clock.install();
    await page.goto(`/tests/history-browser.html?agent-detail-timing=1&engine=${engine}`);
    const panel = page.getByRole("complementary", { name: "协作代理详情" });
    await expect(panel).toContainText("即时详情已返回");
    await expect(panel.locator(".agent-detail-status")).toHaveText("已完成");
    await expect(panel).not.toContainText("正在读取协作代理过程");
    await page.clock.fastForward(31_000);
    await expect(panel.getByRole("alert")).toHaveCount(0);
    await expect(page.getByTestId("agent-detail-requests")).toHaveText("1");
  });
}

test("Claude agent detail stops waiting on a lost response and retries", async ({ page }) => {
  await page.clock.install();
  const relay = await mockRightPanelRelay(page, { engine: "claude" });
  await page.goto("/");
  await expect.poll(() => relay.commands.some((c) => c.type === "get_history")).toBe(true);
  relay.emit({ type: "user_msg", sid: "layout-parent", msg_id: "agent-timeout-parent", prompt: "检查" });
  relay.emit({ type: "process", sid: "layout-parent", turn_id: "agent-timeout-parent", item_id: "timeout-agent", kind: "agent",
    phase: "start", status: "running", title: "检查慢请求", background: true });
  await page.locator(".process-agent-card").click();
  const panel = page.getByRole("complementary", { name: "协作代理详情" });
  await expect(panel).toContainText("正在读取协作代理过程");
  await page.clock.fastForward(31_000);
  await expect(panel.getByRole("alert")).toContainText("读取协作代理超时");
  await expect(panel).not.toContainText("正在读取协作代理过程");
  await panel.getByRole("button", { name: "重试", exact: true }).click();
  await expect.poll(() => relay.commands.filter((c) => c.type === "get_agent_detail").length).toBe(2);
});

test("generated image live snapshot renders outside collapsed process and duplicate events stay idempotent", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { imageAssets: true });
  await page.goto("/");
  await expect.poll(() => relay.commands.some((c) => c.type === "get_history")).toBe(true);
  const sid = "layout-parent";
  relay.emit({ type: "user_msg", sid, msg_id: "image-user", prompt: "画一张图" });
  relay.emit({ type: "process", sid, item_id: "generated-1", kind: "server_tool",
    phase: "start", status: "running", title: "生成图片", tool: "image_generation" });
  await expect(page.locator(".generated-image-gallery")).toHaveCount(0);
  for (let i = 0; i < 2; i++) relay.emit({ type: "process", sid,
    item_id: "generated-1", kind: "server_tool", phase: "end", status: "succeeded",
    title: "生成图片", tool: "image_generation",
    input: { file_path: "/generated/image.png", preview_id: "generated-1" } });
  const image = page.getByRole("button", { name: "预览生成的图片" });
  await expect(image).toHaveCount(1);
  await expect(image).toBeVisible();
  expect(relay.commands.filter((c) => c.type === "get_preview_asset")).toHaveLength(1);
  await image.click();
  await expect(page.locator(".image-lightbox-image")).toBeVisible();
});

test("provider capacity failure stays local to its turn while the next reply continues", async ({ page }, testInfo) => {
  // This case drives live packets; canonical history is covered below. Do not
  // let the generic empty-history fixture erase the completed live failure.
  const relay = await mockRightPanelRelay(page, { historyReply: () => null });
  await page.goto("/");
  await expect.poll(() => relay.commands.some((c) => c.type === "get_history")).toBe(true);
  const sid = "layout-parent";
  relay.emit({ type: "user_msg", sid, msg_id: "failed-request", prompt: "部署一下。" });
  relay.emit({ type: "turn_binding", sid, msg_id: "failed-request", turn_id: "failed-native" });
  relay.emit({ type: "process", sid, item_id: "build", kind: "command", phase: "end",
    status: "succeeded", turn_id: "failed-native", title: "检查构建", duration_ms: 2000 });
  relay.emit({ type: "error", sid, msg_id: "failed-request", code: "cc_crash",
    message: "当前模型繁忙，请稍后重试或切换模型。" });
  relay.emit({ type: "turn_end", sid, turn_id: "failed-native",
    result: { subtype: "error", duration_ms: 337204, is_error: true } });
  relay.emit({ type: "state", sid, state: "idle" });
  const failed = page.locator('.turn[data-turn-id="failed-request"]');
  await expect(failed.locator(".turn-process-head")).toContainText("回复未完成");
  await expect(failed.locator(".turn-problem")).toContainText("当前模型繁忙，请稍后重试或切换模型。");
  await expect(failed.locator(".turn-process-state.done, .note.interrupted")).toHaveCount(0);
  await expect(page.locator(".turn-working")).toHaveCount(0);

  relay.emit({ type: "user_msg", sid, msg_id: "followup-native", prompt: "" });
  relay.emit({ type: "turn_binding", sid, msg_id: "followup-native", turn_id: "followup-native" });
  relay.emit({ type: "state", sid, state: "running" });
  relay.emit({ type: "process", sid, item_id: "verify", kind: "command", phase: "start",
    status: "running", turn_id: "followup-native", title: "继续验证发布" });
  const followup = page.locator('.turn[data-turn-id="followup-native"]');
  await expect(followup.locator(".turn-process-head")).toContainText("正在处理");
  await expect(failed.locator(".turn-problem-continuation")).toContainText("后续回复正在处理中");
  await expect(failed.locator(".turn-problem")).not.toContainText("请稍后重试");
  await expect(page.locator(".turn-working")).toHaveCount(1);
  await expect(failed.locator(".turn-working")).toHaveCount(0);
  await failed.locator(".turn-process-head").click();
  await expect(failed).toContainText("检查构建");
  await page.screenshot({ path: testInfo.outputPath("capacity-followup-light.png") });
  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await page.screenshot({ path: testInfo.outputPath("capacity-followup-dark.png") });
  expect(await failed.locator(".turn-problem").evaluate(node => node.scrollWidth - node.clientWidth)).toBeLessThan(2);

  relay.emit({ type: "turn_end", sid, turn_id: "followup-native",
    result: { subtype: "success", duration_ms: 5000, is_error: false } });
  relay.emit({ type: "state", sid, state: "idle" });
  await expect(page.locator(".turn-working, .turn-problem-continuation")).toHaveCount(0);
  await expect(failed.locator(".turn-process-head")).toContainText("回复未完成");
  await expect(followup.locator(".turn-process-head")).toContainText("已处理");
  expect(relay.commands.filter(c => ["query", "steer", "interrupt"].includes(String(c.type)))).toHaveLength(0);
});

test("provider capacity history keeps its cause after reload without a stuck progress indicator", async ({ page }) => {
  await mockRightPanelRelay(page, { seedTurns: [{
    id: "failed-history", prompt: "部署一下。", done: true, forkPointId: "failed-native",
    error: "当前模型繁忙，请稍后重试或切换模型。", processDetailState: "present",
    blocks: [{ kind: "process", item_id: "build", processKind: "command", phase: "end",
      status: "succeeded", title: "检查构建", done: true, turn_id: "failed-native" }],
  }, { id: "later-history", prompt: "", done: true, forkPointId: "followup-native",
    blocks: [{ kind: "text", message_id: "finished", channel: "final", text: "已完成部署。", done: true }] }] });
  await page.goto("/");
  for (let iteration = 0; iteration < 2; iteration++) {
    if (iteration) await page.reload();
    await expect(page.locator(".turn-problem")).toHaveCount(1);
    await expect(page.locator(".turn-problem")).toContainText("当前模型繁忙");
    await expect(page.locator(".turn-process-head")).toContainText("回复未完成");
    await expect(page.getByText("已完成部署。", { exact: true })).toBeVisible();
    await expect(page.locator(".turn-working, .turn-problem-continuation, .note.interrupted")).toHaveCount(0);
  }
});

test("turn regressions usage limit shows the native retry date without blocking the next turn", async ({ page }, testInfo) => {
  const failure = "本轮使用的 Codex 账号额度已用完。可切换账号、补充额度，或等待恢复后重试。"
    + "官方提示可于 2026-09-15 09:24（设备当地时间）重试。";
  const relay = await mockRightPanelRelay(page, { historyReply: () => null });
  await page.goto("/");
  await expect.poll(() => relay.commands.some(c => c.type === "get_history")).toBe(true);
  const sid = "layout-parent";
  relay.emit({ type: "user_msg", sid, msg_id: "limited-request", prompt: "继续检查" });
  relay.emit({ type: "turn_binding", sid, msg_id: "limited-request", turn_id: "limited-native" });
  relay.emit({ type: "process", sid, item_id: "check", kind: "command", phase: "end",
    status: "succeeded", turn_id: "limited-native", title: "检查已有结果", duration_ms: 1000 });
  relay.emit({ type: "error", sid, msg_id: "limited-request", code: "cc_crash", message: failure });
  relay.emit({ type: "turn_end", sid, turn_id: "limited-native",
    result: { subtype: "error", duration_ms: 1000, is_error: true } });
  relay.emit({ type: "state", sid, state: "idle" });
  const failed = page.locator('.turn[data-turn-id="limited-request"]');
  await expect(failed.locator(".turn-process-head")).toContainText("账号额度已用完");
  await expect(failed.locator(".turn-problem")).toHaveText(failure);
  await expect(page.locator(".turn-working, .note.interrupted")).toHaveCount(0);
  expect(await failed.locator(".turn-problem").evaluate(node => node.scrollWidth - node.clientWidth)).toBeLessThan(2);
  await page.screenshot({ path: testInfo.outputPath("quota-failure-light.png") });
  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await page.screenshot({ path: testInfo.outputPath("quota-failure-dark.png") });

  relay.emit({ type: "user_msg", sid, msg_id: "quota-followup", prompt: "换个账号继续" });
  relay.emit({ type: "turn_binding", sid, msg_id: "quota-followup", turn_id: "followup-native" });
  relay.emit({ type: "state", sid, state: "running" });
  relay.emit({ type: "assistant_msg_start", sid, message_id: "resumed-answer", channel: "final" });
  relay.emit({ type: "delta", sid, message_id: "resumed-answer", text: "检查完成。", channel: "final" });
  relay.emit({ type: "turn_end", sid, turn_id: "followup-native",
    result: { subtype: "success", duration_ms: 1000, is_error: false } });
  relay.emit({ type: "state", sid, state: "idle" });
  await expect(page.locator('.turn[data-turn-id="quota-followup"] .turn-problem')).toHaveCount(0);
  await expect(page.getByText("检查完成。", { exact: true })).toBeVisible();
  await expect(page.locator(".turn-problem")).toHaveCount(1);
  await expect(page.locator(".turn-working")).toHaveCount(0);
  expect(relay.commands.filter(c => ["query", "steer", "interrupt"].includes(String(c.type)))).toHaveLength(0);
});

test("turn regressions usage limit history replaces an old generic cache and survives reload", async ({ page }) => {
  const failure = "本轮使用的 Codex 账号额度已用完。可切换账号、补充额度，或等待恢复后重试。"
    + "官方提示可于 2026-09-15 09:24（设备当地时间）重试。";
  // Match the real zero-output failed turn: no tool timeline, only its error.
  const seedTurns: NonNullable<Extract<ServerEvent, { type: "history" }>["turns"]> = [{
    id: "limited-history", prompt: "继续检查", done: true, forkPointId: "limited-native",
    error: "该轮未正常结束", blocks: [],
  }, { id: "quota-followup-history", prompt: "换个账号继续", done: true, forkPointId: "followup-native",
    blocks: [{ kind: "text", message_id: "finished", channel: "final", text: "检查完成。", done: true }] }];
  await mockRightPanelRelay(page, { seedTurns });
  await page.goto("/");
  await expect(page.locator(".turn-problem")).toHaveCount(0);
  await expect(page.locator(".turn-failure-status")).toHaveText("回复未完成，请重试。");
  await expect.poll(() => page.evaluate(async () => {
    const cache = await import("/src/cache.ts");
    return (await cache.loadSession("layout-parent"))?.turns.find(turn => turn.id === "limited-history")?.error;
  })).toBe("该轮未正常结束");
  seedTurns[0] = { ...seedTurns[0], error: failure };
  for (let iteration = 0; iteration < 2; iteration++) {
    await page.reload();
    await expect(page.locator(".turn-problem")).toHaveText(failure);
    await expect(page.locator(".turn-failure-status")).toHaveCount(0);
    await expect(page.getByText("检查完成。", { exact: true })).toBeVisible();
    await expect(page.locator(".turn-working, .note.interrupted")).toHaveCount(0);
  }
});

test("turn regressions daemon update keeps its cause across native history refresh and reload", async ({ page }, testInfo) => {
  const raw = "Codex 已自动更新，当前回合在更新时中断；为避免重复执行工具，"
    + "本次任务未自动重试。请确认已有结果后重新发送。";
  const expected = "Codex 自动更新时连接中断，本轮未确认完成。请检查已有结果后继续。";
  const seedTurns: NonNullable<Extract<ServerEvent, { type: "history" }>["turns"]> = [{
    id: "interrupted-update", prompt: "修复问题", done: true,
    forkPointId: "updated-native",
    error: raw, processDetailState: "present", blocks: [],
  }, { id: "continued-update", prompt: "继续", done: true,
    blocks: [{ kind: "text", message_id: "continued-answer", channel: "final",
      text: "继续核对已有结果。", done: true }] }];
  const relay = await mockRightPanelRelay(page, { seedTurns });
  await page.goto("/");
  for (let iteration = 0; iteration < 2; iteration++) {
    if (iteration) await page.reload();
    await expect(page.locator(".turn-problem")).toHaveText(expected);
    await expect(page.locator(".turn-process-head")).toContainText("Codex 自动升级，本轮中断");
    await expect(page.getByText("继续核对已有结果。", { exact: true })).toBeVisible();
    await expect(page.locator(".turn-working")).toHaveCount(0);
    if (!iteration) {
      await expect.poll(() => page.evaluate(async () => {
        const cache = await import("/src/cache.ts");
        const saved = await cache.loadSession("layout-parent");
        return saved?.turns.find(turn => turn.id === "interrupted-update")?.error;
      })).toBe(raw);
      // app-server history carries the interruption, but not the updater's
      // control-link explanation. The local observed cause must survive it.
      seedTurns[0] = { ...seedTurns[0], error: undefined, interrupted: true };
    }
  }
  await page.screenshot({ path: testInfo.outputPath("codex-update-interruption.png") });
  expect(relay.commands.filter(c => ["query", "steer", "interrupt"].includes(String(c.type)))).toHaveLength(0);
});

test("async question first layout uses rounded cards and preserves a postponed answer", async ({ page }, testInfo) => {
  const title = "按这版目标开始可行性设计？";
  const relay = await mockRightPanelRelay(page, { seedTurns: [{
    ...ASYNC_HISTORY_TURN, blocks: [{ ...ASYNC_HISTORY_TURN.blocks[0], text: title,
      questions: [{ title, options: ["确认，按这版做可行性设计", "需要调整，我补充说明"] }] }],
  }] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  await expect(dialog.getByRole("textbox")).not.toBeFocused();
  await expect(dialog.getByRole("radio").first()).toBeChecked();
  await expect(dialog.locator(".async-question-option").first()).toHaveCSS("border-radius", "12px");
  await expect(dialog.locator(".async-question-option-check")).toHaveCount(0);
  await dialog.getByRole("radio").nth(1).check();
  await dialog.getByRole("textbox").fill("需要先验证续航。\n不要直接加工。");
  await expect(dialog.getByRole("radio").nth(1)).not.toBeChecked();
  await dialog.getByRole("button", { name: "稍后回答" }).click();
  await expect(page.locator(".question-resume")).toContainText("等待你回答");
  await openAsyncQuestion(page);
  await expect(dialog.getByRole("textbox")).toHaveValue("需要先验证续航。\n不要直接加工。");
  await page.screenshot({ path: testInfo.outputPath("async-question-first-layout.png") });
  await dialog.getByRole("button", { name: "提交回答" }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "query").length).toBe(1);
  expect(relay.commands.find(c => c.type === "query")?.prompt).toContain("回答：需要先验证续航。\n不要直接加工。");
});

test("async question option presses avoid native tap flashes and retain rounded keyboard focus", async ({ page, isMobile }, testInfo) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [{
    ...ASYNC_HISTORY_TURN, blocks: [{ ...ASYNC_HISTORY_TURN.blocks[0], questions: [ASYNC_QUESTIONS[0]] }],
  }] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  const option = dialog.locator(".async-question-option").nth(1);
  const radio = option.getByRole("radio");
  // Both the label and the transparent, full-size native input participate in
  // touch hit testing. Neither may paint the browser's rectangular tap overlay.
  for (const target of [option, radio]) {
    await expect(target).toHaveCSS("-webkit-tap-highlight-color", "rgba(0, 0, 0, 0)");
  }
  await expect(option).toHaveCSS("border-top-left-radius", "12px");
  const box = (await radio.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  try {
    await expect(option).toHaveCSS("outline-style", "none");
    await page.screenshot({ path: testInfo.outputPath("async-question-option-pressed.png") });
  } finally {
    await page.mouse.up();
  }
  await expect(radio).toBeChecked();
  if (isMobile) {
    await dialog.getByRole("radio", { name: "Mac", exact: true }).tap();
    await radio.tap();
    await expect(radio).toBeChecked();
  }
  await expect(dialog.getByRole("textbox")).toBeVisible();
  // No blanket outline removal: tabbing back to the selected radio still
  // exposes a visible focus ring following the option's rounded shape.
  // Mobile Safari need not focus a radio when tapped. Start the keyboard path
  // from a known control and tab to the selected option on both platforms.
  await dialog.getByRole("button", { name: "忽略当前问题" }).focus();
  await page.keyboard.press("Tab");
  await expect(radio).toBeFocused();
  await expect(option).toHaveCSS("outline-style", "solid");
  await expect(option).toHaveCSS("outline-width", "2px");
  await expect(option).toHaveCSS("border-top-left-radius", "12px");
  expect(relay.commands.filter(c => ["query", "steer", "answer_question", "interrupt"].includes(String(c.type))))
    .toHaveLength(0);
  // The visible textarea remains optional; an option-only answer still sends.
  await dialog.getByRole("button", { name: "提交回答" }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "query").length).toBe(1);
  expect(relay.commands.find(c => c.type === "query")?.prompt).toBe("补充回答：\n\n问题：在哪个设备发生？\n回答：手机");
});

test("generated image canonical summary survives refresh and loads original without detail", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { imageAssets: true, seedTurns: [{
    id: "image-user", prompt: "画一张图", done: true, detailLoaded: false,
    processDetailState: "present", detailReasons: ["process"],
    processStartedTs: 1788621175000, processDoneTs: 1788621261221,
    blocks: [{ kind: "process", item_id: "generated-1", processKind: "server_tool",
      phase: "end", status: "succeeded", done: true, title: "生成图片",
      tool: "image_generation", input: { history_image: {
        image_id: "img-native", media_type: "image/png", width: 1, height: 1, byte_size: 68,
      } } }],
  }] });
  await page.goto("/");
  const image = page.getByRole("button", { name: "预览生成的图片" });
  await expect(image).toBeEnabled();
  const process = page.getByRole("button", { name: /已处理 1m 26s/ });
  await expect(process).toBeVisible();
  await expect(process).toHaveAttribute("aria-expanded", "false");
  await page.reload();
  await expect(image).toBeEnabled();
  await expect(process).toBeVisible();
  await expect(process).toHaveAttribute("aria-expanded", "false");
  expect(relay.commands.filter((c) => c.type === "get_turn_detail")).toHaveLength(0);
  const reads = relay.commands.filter((c) => c.type === "get_history_image");
  expect(reads.length).toBeGreaterThanOrEqual(2);
  expect(reads.every((c) => c.turn_id === "image-user" && c.variant === "full")).toBe(true);
  await image.click();
  await expect(page.locator(".image-lightbox-image")).toBeVisible();
  expect(relay.commands.filter(c => c.type === "get_history_image")).toHaveLength(reads.length);
});

test("generated image stays loaded when a new message renames the historical turn and alias revision", async ({ page }) => {
  const turns: NonNullable<Extract<ServerEvent, { type: "history" }>["turns"]> = [{
    id: "msg-old", forkPointId: "native-task", prompt: "画图", done: true, detailLoaded: true,
    blocks: [{ kind: "process", item_id: "image-old", processKind: "server_tool",
      phase: "end", status: "succeeded", done: true, title: "生成图片", tool: "image_generation",
      input: { history_image: { image_id: "img-stable", media_type: "image/png", width: 1, height: 1, byte_size: 68 } } }],
  }];
  const relay = await mockRightPanelRelay(page, { imageAssets: true, seedTurns: turns });
  await page.goto("/");
  const image = page.locator(".generated-output img");
  await expect(image).toHaveCount(1);
  const src = await image.getAttribute("src");
  const reads = relay.commands.filter(c => c.type === "get_history_image").length;
  expect(reads).toBe(1);
  await page.locator(".composer textarea").fill("继续");
  await page.locator(".composer").getByRole("button", { name: "发送", exact: true }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "query").length).toBe(1);
  const query = relay.commands.find(c => c.type === "query")!;
  await expect(image).toHaveAttribute("src", src!);
  turns[0] = { ...turns[0], id: "item-123" };
  turns.push({ id: "next-message", clientMsgId: String(query.msg_id), prompt: "继续", done: false, blocks: [] });
  relay.emit({ type: "user_msg", sid: "layout-parent", msg_id: "next-message",
    client_msg_id: String(query.msg_id), prompt: "继续" });
  relay.emit({ type: "state", sid: "layout-parent", state: "running", msg_id: "next-message" });
  relay.emit({ type: "history", sid: "layout-parent", session_id: "layout-parent",
    revision: "layout-alias-1", continuity_revision: "layout-history", generation: "layout-generation",
    detail: "summary", events: [], turns, has_more: false });
  await expect(page.getByText("继续", { exact: true })).toBeVisible();
  await expect(image).toHaveCount(1);
  await expect(image).toHaveAttribute("src", src!);
  await expect(page.locator(".generated-output .history-image-placeholder")).toHaveCount(0);
  await page.getByRole("button", { name: "查看大图" }).click();
  await expect(page.locator(".image-lightbox-image")).toHaveAttribute("src", src!);
  expect(relay.commands.filter(c => c.type === "get_history_image")).toHaveLength(reads);
  await page.keyboard.press("Escape");
  // A hard reload still uses the canonical id and full original, not a 360px thumbnail.
  await page.reload();
  await expect(image).toHaveAttribute("src", src!);
  expect(relay.commands.filter(c => c.type === "get_history_image").at(-1))
    .toMatchObject({ turn_id: "item-123", variant: "full" });
});

test("generated image live snapshot survives canonical item identity handoff", async ({ page }) => {
  const turns: NonNullable<Extract<ServerEvent, { type: "history" }>["turns"]> = [];
  const relay = await mockRightPanelRelay(page, { imageAssets: true, seedTurns: turns });
  await page.goto("/");
  await expect.poll(() => relay.commands.some(c => c.type === "get_history")).toBe(true);
  const sid = "layout-parent";
  const ref = { image_id: "img-live-stable", media_type: "image/png", width: 1, height: 1, byte_size: 68 };
  relay.emit({ type: "user_msg", sid, msg_id: "draw-user", prompt: "画图" });
  relay.emit({ type: "turn_binding", sid, msg_id: "draw-user", turn_id: "draw-task" });
  relay.emit({ type: "process", sid, item_id: "live-image", kind: "server_tool",
    phase: "end", status: "succeeded", tool: "image_generation", title: "生成图片",
    input: { history_image: ref, file_path: "/generated/live.png", preview_id: "live-image" } });
  const image = page.locator(".generated-output img");
  await expect(image).toHaveCount(1);
  const src = await image.getAttribute("src");
  // Completion can trigger an automatic history read. The mock's canonical
  // store must advance with the emitted page, not later return an empty store
  // and erase the completed turn depending on request timing.
  turns.push({
    id: "draw-user", forkPointId: "draw-task", prompt: "画图", done: true, detailLoaded: false,
    blocks: [{ kind: "process", item_id: "item-image-123", processKind: "server_tool",
      phase: "end", status: "succeeded", done: true, tool: "image_generation", title: "生成图片",
      input: { history_image: ref } }],
  });
  relay.emit({ type: "turn_end", sid, turn_id: "draw-task", result: { subtype: "success", is_error: false } });
  relay.emit({ type: "history", sid, session_id: sid,
    revision: "layout-history", generation: "layout-generation",
    detail: "summary", events: [], has_more: false, turns });
  await expect(image).toHaveCount(1);
  await expect(image).toHaveAttribute("src", src!);
  await expect(page.locator(".generated-output .history-image-placeholder")).toHaveCount(0);
  expect(relay.commands.filter(c => c.type === "get_preview_asset")).toHaveLength(1);
  expect(relay.commands.filter(c => c.type === "get_history_image")).toHaveLength(0);
  await page.getByRole("button", { name: "查看大图" }).click();
  await expect(page.locator(".image-lightbox-image")).toHaveAttribute("src", src!);
  expect(relay.commands.filter(c => c.type === "get_history_image")).toHaveLength(0);
});

test("generated image uses full reply width and remains visible through process collapse", async ({ page }, testInfo) => {
  const desktop = testInfo.project.name === "chromium";
  if (desktop) await page.setViewportSize({ width: 1360, height: 900 });
  const imageData = await page.evaluate(() => {
    const canvas = document.createElement("canvas");
    canvas.width = 1693;
    canvas.height = 929;
    const ctx = canvas.getContext("2d")!;
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#26334d";
    ctx.font = "24px sans-serif";
    for (let y = 50; y < 900; y += 40) ctx.fillText("Generated image — original resolution / 原图清晰度", 30, y);
    return { data: canvas.toDataURL("image/png").split(",")[1], width: canvas.width, height: canvas.height };
  });
  const relay = await mockRightPanelRelay(page, { imageAssets: true, imageData, seedTurns: [{
    id: "large-image", prompt: "画一张图", done: true, detailLoaded: true,
    blocks: [{ kind: "process", item_id: "generated-large", processKind: "server_tool",
      phase: "end", status: "succeeded", done: true, title: "生成图片", tool: "image_generation",
      input: { history_image: { image_id: "img-large", media_type: "image/png", width: 1693, height: 929, byte_size: 68 } } }],
  }] });
  await page.goto("/");
  const figure = page.locator(".generated-output");
  const image = figure.getByRole("button", { name: "预览生成的图片" });
  await expect(image).toBeEnabled();
  await expect.poll(() => image.locator("img").evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(1693);
  const bounds = await image.boundingBox();
  expect(bounds!.width).toBeGreaterThan(desktop ? 600 : 300);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(page.viewportSize()!.width);
  expect(Math.abs(bounds!.width / bounds!.height - 1693 / 929)).toBeLessThan(.02);
  const process = page.locator('.turn[data-turn-id="large-image"]').getByRole("button", { name: /已处理/ }).first();
  for (let i = 0; i < 2; i++) {
    await process.click();
    await expect(figure).toBeVisible();
  }
  await figure.getByRole("button", { name: "查看大图" }).click();
  await expect(page.locator(".image-lightbox-image")).toBeVisible();
  expect(relay.commands.filter(c => c.type === "get_turn_detail")).toHaveLength(0);
  await page.keyboard.press("Escape");
  await page.screenshot({ path: testInfo.outputPath("generated-image-wide.png") });
});

test("async question answered presentation preserves the prompt disclosure through refresh", async ({ page }, testInfo) => {
  const prompt = "补充回答：\n\n问题：用的是什么手势？\n回答：三指拖拽\n拖到页面下方";
  const relay = await mockRightPanelRelay(page, { seedTurns: [ASYNC_HISTORY_TURN, {
    id: "supplemental-reply", prompt, done: false, blocks: [],
  }] });
  await page.goto("/");
  for (let i = 0; i < 2; i++) {
    if (i) await page.reload();
    const answer = page.locator(".supplemental-answer");
    await expect(answer).toContainText("三指拖拽\n拖到页面下方");
    await expect(page.locator(".async-question-card, .question-resume, .async-question-dialog")).toHaveCount(0);
    await expect(answer.locator("details")).not.toHaveAttribute("open");
    await answer.getByText("查看问题", { exact: true }).click();
    await expect(answer.getByText("用的是什么手势？", { exact: true })).toBeVisible();
    await answer.locator("summary").click();
    if (i) await page.screenshot({ path: testInfo.outputPath("async-question-answered.png") });
  }
  expect(relay.commands.filter(c => ["query", "steer", "interrupt", "get_turn_detail"].includes(String(c.type)))).toHaveLength(0);
});

test("async question replay after compaction keeps one prompt disclosure and no duplicate pending question", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [ASYNC_HISTORY_TURN, {
    id: "answered-steer", prompt: "补充回答：\n\n问题：用的是什么手势？\n回答：已经退出", done: false, blocks: [],
  }, {
    ...ASYNC_HISTORY_TURN, id: "compacted-continuation", prompt: "", done: false,
    blocks: [...ASYNC_HISTORY_TURN.blocks, {
      kind: "text", message_id: "after-compaction-answer", channel: "final", text: "收到，继续执行。", done: true,
    }],
  }] });
  await page.goto("/");
  for (let i = 0; i < 2; i++) {
    if (i) await page.reload();
    await expect(page.locator(".supplemental-answer")).toContainText("已经退出");
    await expect(page.getByText("收到，继续执行。", { exact: true })).toBeVisible();
    await expect(page.locator(".async-question-card, .question-resume, .async-question-dialog")).toHaveCount(0);
  }
  expect(relay.commands.filter(c => ["query", "steer", "interrupt"].includes(String(c.type)))).toHaveLength(0);
});

test("async question unanswered drafts navigate freely and survive repeated postponement", async ({ page }, info) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [{ ...ASYNC_HISTORY_TURN,
    blocks: [{ ...ASYNC_HISTORY_TURN.blocks[0], questions: ASYNC_QUESTIONS }],
  }] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  await expect(dialog.locator(".qa-progress")).toHaveText("1 / 2");
  await dialog.getByRole("radio", { name: "手机", exact: true }).check();
  await dialog.getByRole("button", { name: "下一题" }).click();
  await expect(dialog.getByRole("button", { name: "提交回答" })).toBeDisabled();
  await dialog.getByRole("button", { name: "上一题" }).click();
  await expect(dialog.getByRole("radio", { name: "手机", exact: true })).toBeChecked();
  await dialog.getByRole("button", { name: "下一题" }).click();
  await dialog.getByRole("textbox").fill("选区手柄\n这是保留的草稿");
  const navigation = (await dialog.locator(".async-question-navigation").boundingBox())!;
  const actions = (await dialog.locator(".async-question-footer-actions").boundingBox())!;
  if (info.project.name.includes("webkit")) expect(actions.y).toBeGreaterThanOrEqual(navigation.y + navigation.height);
  for (let i = 0; i < 2; i++) {
    await dialog.getByRole("button", { name: "稍后回答" }).click();
    await expect(page.locator(".question-resume")).toContainText("2 / 2");
    await openAsyncQuestion(page);
    await expect(dialog.getByRole("textbox")).toHaveValue("选区手柄\n这是保留的草稿");
  }
  await page.screenshot({ path: info.outputPath("async-question-navigation.png") });
  expect(relay.commands.filter(c => ["query", "steer", "answer_question", "interrupt"].includes(String(c.type)))).toHaveLength(0);
});

test("async question dismiss removes its waiting entry across refresh without answering", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [ASYNC_HISTORY_TURN] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  await dialog.getByRole("textbox").fill("这一题不回答");
  await dialog.getByRole("button", { name: "忽略当前问题" }).click();
  await expect(page.locator(".async-question-dialog, .question-resume, .async-question-card")).toHaveCount(0);
  await page.reload();
  await expect(page.getByText("session layout-p", { exact: true })).toBeAttached();
  await expect.poll(() => relay.commands.filter(c => c.type === "get_history").length).toBeGreaterThanOrEqual(2);
  await expect(page.locator(".async-question-dialog, .question-resume, .async-question-card")).toHaveCount(0);
  expect(relay.commands.filter(c => ["query", "steer", "answer_question", "interrupt"].includes(String(c.type)))).toHaveLength(0);
});

test("async question can answer the next page first and retire only that accepted question", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [{ ...ASYNC_HISTORY_TURN,
    blocks: [{ ...ASYNC_HISTORY_TURN.blocks[0], questions: ASYNC_QUESTIONS }],
  }] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  await dialog.getByRole("button", { name: "下一题" }).click();
  await dialog.getByRole("textbox").fill("三指拖拽");
  await dialog.getByRole("button", { name: "提交回答" }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "query").length).toBe(1);
  const query = relay.commands.find(c => c.type === "query")!;
  expect(query.prompt).toBe("补充回答：\n\n问题：用的是什么手势？\n回答：三指拖拽");
  relay.emit({ type: "user_msg", sid: String(query.sid), msg_id: String(query.msg_id), prompt: String(query.prompt) });
  await expect(dialog.locator("legend")).toHaveText("在哪个设备发生？");
  await expect(dialog.locator(".qa-progress")).toHaveCount(0);
  await expect(page.locator(".supplemental-answer")).toContainText("三指拖拽");
  await expect(page.locator(".async-question-card")).toHaveCount(0);
  await dialog.getByRole("button", { name: "忽略当前问题" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator(".question-resume")).toHaveCount(0);
  await page.locator(".supplemental-answer summary").click();
  await expect(page.locator(".supplemental-answer-context")).toContainText("用的是什么手势？");
});

test("async question dialog keeps independent drafts across native messages and traps keyboard focus", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [{ ...ASYNC_HISTORY_TURN,
    blocks: [ASYNC_HISTORY_TURN.blocks[0], { ...ASYNC_HISTORY_TURN.blocks[0], message_id: "other-question" }],
  }] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  await dialog.getByRole("textbox").fill("第一个问题的草稿");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(page.locator(".question-resume")).toBeFocused();
  await openAsyncQuestion(page, 1);
  await expect(dialog.getByRole("textbox")).toHaveValue("");
  await dialog.getByRole("textbox").fill("第二个问题的草稿");
  await dialog.getByRole("button", { name: "上一题" }).click();
  await expect(dialog.getByRole("textbox")).toHaveValue("第一个问题的草稿");
  for (let i = 0; i < 9; i++) {
    await page.keyboard.press("Tab");
    expect(await dialog.evaluate(node => node.contains(document.activeElement))).toBe(true);
  }
  await page.mouse.click(4, 4);
  await expect(dialog).toHaveCount(0);
  expect(relay.commands.filter(c => ["query", "steer", "interrupt"].includes(String(c.type)))).toHaveLength(0);
});

test("async question dialog fits a keyboard-sized viewport without moving into a side panel", async ({ page }, testInfo) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [{ ...ASYNC_HISTORY_TURN, blocks: [{ ...ASYNC_HISTORY_TURN.blocks[0],
    questions: [{ title: "请说明发生问题时的具体操作、网络和设备情况。".repeat(24), options: null }],
  }] }] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  await dialog.getByLabel("你的回答", { exact: true }).fill("保留输入中的草稿");
  await page.evaluate(() => {
    // Drive the real viewport synchronizer; writing its CSS output directly
    // races the delayed focus/keyboard settling reads in the App shell.
    Object.defineProperties(window.visualViewport!, {
      height: { configurable: true, value: 360 },
      offsetTop: { configurable: true, value: 40 },
    });
    window.visualViewport!.dispatchEvent(new Event("resize"));
  });
  await expect.poll(async () => {
    const rect = await dialog.boundingBox();
    return !!rect && rect.y >= 40 && rect.y + rect.height <= 400;
  }).toBe(true);
  expect(await dialog.evaluate(node => node.parentElement === document.body)).toBe(true);
  const box = (await dialog.boundingBox())!;
  expect(Math.abs(box.x + box.width / 2 - page.viewportSize()!.width / 2)).toBeLessThan(2);
  await expect(dialog.getByRole("button", { name: "提交回答" })).toBeInViewport();
  await expect(dialog.getByRole("button", { name: "稍后回答" })).toBeInViewport();
  const body = dialog.locator(".async-question-body");
  expect(await body.evaluate(node => node.scrollHeight > node.clientHeight)).toBe(true);
  await expect(dialog.getByLabel("你的回答", { exact: true })).toBeInViewport({ ratio: 1 });
  await expect.poll(() => dialog.getByLabel("你的回答", { exact: true }).evaluate(input => {
    const viewport = input.closest(".async-question-body")!.getBoundingClientRect();
    const editor = input.getBoundingClientRect();
    return editor.top >= viewport.top - 1 && editor.bottom <= viewport.bottom + 1;
  })).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("async-question-keyboard.png") });
  expect(relay.commands.filter(c => ["query", "steer"].includes(String(c.type)))).toHaveLength(0);
});

test("async question dialog omits raw source previews and preserves its draft on reopen", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { externalPreview: "allow", seedTurns: [{
    ...ASYNC_HISTORY_TURN, blocks: [{ ...ASYNC_HISTORY_TURN.blocks[0],
      text: "请看[设计说明](/tmp/proposal.md)，用的是什么手势？" }],
  }] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  await expect(dialog.getByText("用的是什么手势？", { exact: true })).toHaveCount(1);
  await expect(dialog.locator("details, .async-question-source, .async-question-original")).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: "在 Remote 中打开 /tmp/proposal.md" })).toHaveCount(0);
  await dialog.getByLabel("你的回答", { exact: true }).fill("保留的回答草稿");
  await dialog.getByRole("button", { name: "稍后回答" }).click();
  await expect(dialog).toHaveCount(0);
  await openAsyncQuestion(page);
  await expect(dialog.getByLabel("你的回答", { exact: true })).toHaveValue("保留的回答草稿");
  expect(relay.commands.filter(c => ["query", "steer", "get_file_preview", "authorize_preview"].includes(String(c.type)))).toHaveLength(0);
});

test("async question dialog survives source row virtualization without losing its draft", async ({ page }) => {
  const older = Array.from({ length: 40 }, (_, i) => ({ id: `old-${i}`, prompt: `以前的问题 ${i}`,
    done: true, detailLoaded: true, blocks: [{ kind: "text" as const, message_id: `old-answer-${i}`,
      channel: "final" as const, text: "这是一条以前的回复。".repeat(20), done: true }] }));
  const relay = await mockRightPanelRelay(page, { seedTurns: [...older, ASYNC_HISTORY_TURN] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  await dialog.getByLabel("你的回答", { exact: true }).fill("虚拟列表外也要保留");
  await waitForScrollIdle(page);
  // Simulate a history viewport transition while the modal stays mounted. Use
  // the normal scroll ownership path so the old reader anchor can be released.
  await page.locator(".thread").dispatchEvent("wheel", { deltaY: -1000 });
  await pauseOutputAndScrollToHistoryStart(page);
  await expect(page.locator(".question-resume")).toHaveCount(0);
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel("你的回答", { exact: true })).toHaveValue("虚拟列表外也要保留");
  await dialog.getByRole("button", { name: "提交回答" }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "query").length).toBe(1);
  expect(relay.commands.find(c => c.type === "query")?.prompt).toContain("虚拟列表外也要保留");
  expect(relay.commands.find(c => c.type === "query")?.sid).toBe("layout-parent");
});

test("async question dialog switches sessions with independently retained drafts", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [ASYNC_HISTORY_TURN], secondParent: true });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  await dialog.getByRole("textbox").fill("只属于当前会话的草稿");
  await dialog.getByRole("button", { name: "稍后回答" }).click();
  await page.getByText("Second parent", { exact: true }).evaluate(node => (node as HTMLElement).click());
  await expect(page.getByText("session layout-o", { exact: true })).toBeAttached();
  await openAsyncQuestion(page);
  await expect(dialog.getByRole("textbox")).toHaveValue("");
  await dialog.getByRole("textbox").fill("第二个会话的草稿");
  await dialog.getByRole("button", { name: "稍后回答" }).click();
  await page.getByText("Layout parent", { exact: true }).evaluate(node => (node as HTMLElement).click());
  await openAsyncQuestion(page);
  await expect(dialog.getByRole("textbox")).toHaveValue("只属于当前会话的草稿");
  expect(relay.commands.filter(c => ["query", "steer", "interrupt"].includes(String(c.type)))).toHaveLength(0);
});
test("async question dialog retains an IME draft when control becomes read-only", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [ASYNC_HISTORY_TURN] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  const input = dialog.getByLabel("你的回答", { exact: true });
  await input.fill("三指拖拽的补充");
  await input.dispatchEvent("compositionstart");
  await input.dispatchEvent("keydown", { key: "Enter", code: "Enter", keyCode: 229, isComposing: true });
  await input.dispatchEvent("compositionend", { data: "补充" });
  expect(relay.commands.filter(c => ["query", "steer"].includes(String(c.type)))).toHaveLength(0);
  relay.emit({ type: "session_control", sid: "layout-parent", generation: "layout-generation", revision: 1,
    control_mode: "codex_shared", write_state: "read_only", terminal_attached: false,
    can_takeover: false, reason: "当前会话只读" });
  await expect(input).toBeDisabled();
  await expect(input).toHaveValue("三指拖拽的补充");
  await expect(dialog).toContainText("当前会话暂不可写，回答草稿会保留");
  expect(await dialog.getAttribute("aria-describedby")).toBeTruthy();
  await expect(dialog.getByRole("button", { name: "提交回答" })).toBeDisabled();
  await dialog.getByRole("button", { name: "稍后回答" }).click();
  await openAsyncQuestion(page);
  await expect(input).toHaveValue("三指拖拽的补充");
  expect(relay.commands.filter(c => ["query", "steer"].includes(String(c.type)))).toHaveLength(0);
});

for (const managed of [false, true]) {
test(`Claude native compaction animates once and settles at the persisted boundary (${managed ? "maintenance" : "turn"})`, async ({ page }, testInfo) => {
  const relay = await mockRightPanelRelay(page, {
    engine: "claude", retained: false, historyReply: () => null,
  });
  await page.goto("/");
  await expect.poll(() => relay.commands.some(c => c.type === "get_history")).toBe(true);
  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  const sid = "layout-parent";
  if (!managed) {
    relay.emit({ type: "user_msg", sid, msg_id: "compact-request", prompt: "/compact" });
    relay.emit({ type: "turn_binding", sid, msg_id: "compact-request", turn_id: "compact-turn" });
    relay.emit({ type: "state", sid, state: "running" });
  }
  const start: PanelRelayEvent<Extract<ServerEvent, { type: "process" }>> = {
    type: "process", sid, turn_id: managed ? undefined : "compact-turn", item_id: "compact-status",
    kind: "compaction", phase: "start", status: "running", title: "压缩上下文",
  };
  relay.emit(start);
  relay.emit(start);
  const turn = page.locator(`[data-turn-id="${managed ? "compact-status" : "compact-request"}"]`);
  const running = turn.locator(".process-compaction-running");
  await expect(running).toHaveCount(1);
  await expect(running).toBeVisible();
  await expect(running).toHaveText("正在压缩上下文");
  const bar = running.locator(".compact-motion i").first();
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await expect(bar).toHaveCSS("animation-name", "compact-fold");
  const firstTransform = await bar.evaluate(node => getComputedStyle(node).transform);
  await expect.poll(() => bar.evaluate(node => getComputedStyle(node).transform)).not.toBe(firstTransform);
  await expect(running).toHaveCSS("border-radius", "16px");
  await page.screenshot({ path: testInfo.outputPath("claude-compacting-dark.png") });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(bar).toHaveCSS("animation-name", "none");
  const end: typeof start = { ...start, item_id: "native-boundary", phase: "end",
    status: "succeeded", summary: "手动压缩 · 600,000 → 8,000 tokens", duration_ms: 20_000,
    input: { compaction_started_id: "compact-status" } };
  relay.emit(end);
  relay.emit(end);
  if (!managed) {
    relay.emit({ type: "turn_end", sid, turn_id: "compact-turn", result: { subtype: "success", is_error: false } });
    relay.emit({ type: "state", sid, state: "idle" });
  }
  await expect(running).toHaveCount(0);
  const head = turn.locator(".turn-process-head");
  await expect(head).toContainText("已处理");
  if (await head.getAttribute("aria-expanded") === "false") await head.click();
  const completed = turn.locator(".process-activity");
  await expect(completed).toHaveCount(1);
  await completed.locator("summary").click();
  await expect(completed).toContainText("手动压缩 · 600,000 → 8,000 tokens");
  await expect(completed).not.toContainText("compaction_started_id");
  await expect(turn.locator(".turn-working")).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("claude-compacted-dark.png") });
  expect(relay.commands.filter(c => ["query", "steer", "interrupt"].includes(String(c.type)))).toHaveLength(0);
});
}

for (const engine of ["claude", "codex"] as const) {
test(`manual compact stays inline, keeps the existing spark and accepts the next message (${engine})`, async ({ page }, testInfo) => {
  const relay = await mockRightPanelRelay(page, {
    engine, retained: false, historyReply: () => null,
  });
  await page.goto("/");
  await expect.poll(() => relay.commands.some(c => c.type === "get_history")).toBe(true);
  const input = page.locator(".composer textarea");
  await input.fill("/compact");
  await page.locator(".composer").getByRole("button", { name: "发送", exact: true }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "compact_session").length).toBe(1);
  expect(relay.commands.some(c => c.type === "query")).toBe(false);
  await expect(page.getByText("正在启动原生上下文压缩…", { exact: true })).toHaveCount(0);
  const sid = "layout-parent";
  const turnId = "manual-compact";
  let seq = 0;
  const emit = (event: PanelRelayEvent) => relay.emit({ ...event, seq: ++seq });
  emit({ type: "notice", sid, notice_id: "compact-started", severity: "info",
    category: "runtime", title: "上下文压缩已启动", message: `${engine} 正在压缩当前会话的上下文。` });
  emit({ type: "state", sid, state: "running", phase: "waiting", detail: "压缩中", msg_id: turnId });
  emit({ type: "user_msg", sid, msg_id: turnId, prompt: "/compact" });
  emit({ type: "process", sid, turn_id: turnId, item_id: "compact-start",
    kind: "compaction", phase: "start", status: "running", title: "压缩上下文" });
  const turn = page.locator(`[data-turn-id="${turnId}"]`);
  await expect(turn).toContainText("/compact");
  await expect(turn.locator(".turn-working")).toBeVisible();
  await expect(turn.locator(".turn-working svg")).toHaveAttribute("width", "24");
  await expect(turn.locator(".process-compaction-running")).toBeVisible();
  await expect(page.locator(".notice-stack")).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath(`${engine}-compact-inline-running.png`) });
  emit({ type: "process", sid, turn_id: turnId, item_id: "compact-boundary",
    kind: "compaction", phase: "end", status: "succeeded", title: "压缩上下文",
    input: { compaction_started_id: "compact-start" } });
  // A completed process item is not the native turn's terminal boundary.
  await expect(turn.locator(".turn-working")).toBeVisible();
  emit({ type: "notice", sid, notice_id: "compact-completed", severity: "info",
    category: "runtime", title: "上下文压缩完成", message: "可以继续使用当前会话。" });
  emit({ type: "assistant_msg_start", sid, turn_id: turnId, message_id: "compact-result", channel: "final" });
  emit({ type: "delta", sid, turn_id: turnId, message_id: "compact-result", channel: "final",
    text: "上下文已压缩，可以继续当前会话。" });
  emit({ type: "assistant_msg_end", sid, turn_id: turnId, message_id: "compact-result", channel: "final" });
  emit({ type: "turn_end", sid, turn_id: turnId, checkpoint_id: turnId,
    result: { subtype: "success", is_error: false, duration_ms: 20_000 } });
  emit({ type: "state", sid, state: "idle" });
  await expect(turn).toContainText("上下文已压缩，可以继续当前会话。");
  await expect(turn).not.toContainText("该轮未正常结束");
  await expect(turn.locator(".turn-working")).toHaveCount(0);
  await expect(turn.locator(".turn-done-mark .spark-btn")).toBeVisible();
  await expect(page.locator(".notice-stack")).toHaveCount(0);
  await expect(input).toBeEnabled();
  await page.screenshot({ path: testInfo.outputPath(`${engine}-compact-inline-completed.png`) });
  await input.fill("继续刚才的问题");
  await page.locator(".composer").getByRole("button", { name: "发送", exact: true }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "query").length).toBe(1);
  expect(relay.commands.find(c => c.type === "query")?.prompt).toBe("继续刚才的问题");
});
}

for (const staleProcess of [false, true]) {
test(`turn regressions compaction steer clears phantom detail failure across history and reload (${staleProcess ? "foreign process" : "clock only"})`, async ({ page }, testInfo) => {
  const seedTurns: NonNullable<Extract<ServerEvent, { type: "history" }>["turns"]> = [{
    id: "compact-original", prompt: "部署一下", done: true,
    processDetailState: "present", detailReasons: ["process"], detailEventCount: 1,
    blocks: [{ kind: "process", processKind: "compaction", item_id: "original-compact",
      phase: "end", status: "succeeded", title: "上下文已压缩", done: true }],
  }, {
    id: "native-latest", clientMsgId: "latest", prompt: "最新的", done: true,
    forkPointId: "compact-native-task",
    blocks: [], processDetailState: "present", detailReasons: ["process"],
    detailEventCount: 6, detailLoaded: true,
    processStartedTs: 20_000, processDoneTs: 127_000,
  }, {
    id: "native-flash", clientMsgId: "flash", forkPointId: "compact-native-task",
    prompt: "ds v4.1 flash适配了吗？", done: false,
    blocks: [{ kind: "text", channel: "commentary", message_id: "flash-commentary",
      text: "正在核对模型目录", done: false }],
  }];
  if (staleProcess) seedTurns[1].blocks = [...seedTurns[0].blocks];
  let buildSeq = 1;
  const historyReply = (): PanelRelayEvent<Extract<ServerEvent, { type: "history" }>> => ({
    type: "history", session_id: "layout-parent", sid: "layout-parent",
    revision: "layout-history", generation: "layout-generation", detail: "summary",
    events: [], turns: seedTurns, has_more: false, in_progress: true, live_seq: 0,
    newest_id: "native-flash", build_seq: buildSeq,
  });
  const relay = await mockRightPanelRelay(page, { historyReply });
  await page.goto("/");
  const latest = page.locator('[data-turn-id="native-latest"]');
  await expect(latest.locator(".turn-process-head")).toBeVisible();
  await latest.locator(".turn-process-head").click();
  await expect(latest.getByText(staleProcess
    ? "上下文已压缩" : "详细过程未完整返回，请重试")).toBeVisible();
  seedTurns[1] = { ...seedTurns[1], processDetailState: "none", detailReasons: [],
    detailEventCount: 0, detailLoaded: false, processStartedTs: undefined,
    processDoneTs: undefined, forkPointId: undefined, blocks: [] };
  buildSeq += 1;
  relay.emit(historyReply());
  for (let pass = 0; pass < 2; pass++) {
    if (pass) await page.reload();
    await expect(latest.getByText("最新的", { exact: true })).toBeVisible();
    await expect(latest.locator(".turn-process-head")).toHaveCount(0);
    await expect(page.getByText("详细过程未完整返回，请重试")).toHaveCount(0);
    await expect(page.getByText("正在核对模型目录", { exact: true })).toBeVisible();
    await expect(page.locator('[data-turn-id="native-flash"] .turn-process-head'))
      .toContainText("正在处理");
    await expect(page.locator('[data-turn-id="compact-original"] .turn-process-head')).toHaveCount(1);
  }
  await page.screenshot({ path: testInfo.outputPath("compaction-steer-repaired.png") });
  expect(relay.commands.filter(command => ["query", "steer", "interrupt"].includes(String(command.type))))
    .toHaveLength(0);
});
}

for (const processDetailState of ["none", "unknown"] as const) {
  test(`direct reply summary has no empty detail entry across refresh (${processDetailState})`, async ({ page }) => {
    const relay = await mockRightPanelRelay(page, { seedTurns: [{
      id: "direct-reply", prompt: "在吗？", done: true,
      processDetailState, detailReasons: [], detailLoaded: false, detailEventCount: 0,
      blocks: [{ kind: "text", message_id: "direct-answer", channel: "final", done: true, text: "在，请说。" }],
    }] });
    await page.goto("/");
    for (let i = 0; i < 2; i++) {
      if (i) await page.reload();
      await expect(page.getByText("在，请说。", { exact: true })).toBeVisible();
      await expect(page.locator(".turn-detail-entry")).toHaveCount(0);
      await expect(page.getByText(/已处理|查看本轮详情|查看完整内容/)).toHaveCount(0);
    }
    expect(relay.commands.filter(c => c.type === "get_turn_detail")).toHaveLength(0);
  });
}

for (const type of ["file", "image"] as const) {
  test(`external preview ${type} opens without a second permission prompt`, async ({ page }) => {
    const relay = await mockRightPanelRelay(page, { externalPreview: "allow", seedTurns: [{
      id: "external-output", prompt: "查看结果", done: true, detailLoaded: true,
      blocks: [{ kind: "text", message_id: "external-link", done: true, channel: "final",
        text: type === "file" ? "[打开预览](/tmp/external.md)" : "![外部生成图](/tmp/external.png)" }],
    }] });
    await page.goto("/");
    if (type === "file") {
      await page.getByRole("button", { name: "在 Remote 中打开 /tmp/external.md" }).click();
      await expect(page.locator(".artifact-panel")).toContainText("外部预览已经打开");
      await expect(page.getByRole("button", { name: "保存", exact: true })).toBeDisabled();
    } else {
      await expect(page.getByRole("img", { name: "外部生成图", exact: true })).toBeVisible();
    }
    await expect(page.getByRole("button", { name: "允许查看" })).toHaveCount(0);
    const grants = relay.commands.filter(c => c.type === "authorize_preview");
    expect(grants).toHaveLength(1);
    expect(grants[0].decision).toBe("allow");
    expect(grants[0].sid).toBe("layout-parent");
    expect(relay.commands.some(c => c.type === "save_markdown")).toBe(false);
  });
}

test("external preview replacement stops automatic grant retries and allows a fresh read", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { externalPreview: "replace", seedTurns: [{
    id: "external-output", prompt: "查看结果", done: true, blocks: [{ kind: "text", message_id: "external-link", done: true, channel: "final", text: "[打开预览](/tmp/external.md)" }],
  }] });
  await page.goto("/");
  await page.getByRole("button", { name: "在 Remote 中打开 /tmp/external.md" }).click();
  await expect(page.locator(".artifact-panel")).toContainText("读取未完成");
  expect(relay.commands.filter(c => c.type === "authorize_preview")).toHaveLength(1);
  expect(relay.commands.filter(c => c.type === "get_file_preview")).toHaveLength(2);
  await page.locator(".artifact-file-trigger").click();
  await page.getByRole("button", { name: "重新读取", exact: true }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "authorize_preview").length).toBe(2);
  await expect(page.locator(".artifact-panel")).toContainText("读取未完成");
  expect(relay.commands.filter(c => c.type === "get_file_preview")).toHaveLength(4);
});

test("async question history restores the nonblocking dialog and confirms only its scoped answer", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [ASYNC_HISTORY_TURN] });
  await page.goto("/");
  await openAsyncQuestion(page);
  await page.reload();
  const dialog = await openAsyncQuestion(page);
  await expect(page.getByRole("dialog", { name: "操作确认" })).toHaveCount(0);
  await dialog.getByRole("textbox").fill("三指拖拽");
  await dialog.getByRole("textbox").press("Shift+Enter");
  expect(relay.commands.filter(c => c.type === "query")).toHaveLength(0);
  await dialog.getByRole("button", { name: "提交回答" }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "query").length).toBe(1);
  const query = relay.commands.find(c => c.type === "query")!;
  expect(query.sid).toBe("layout-parent");
  expect(query.prompt).toContain("回答：三指拖拽");
  await expect(dialog.getByRole("button", { name: "等待确认" })).toBeDisabled();
  relay.emit({ type: "user_msg", sid: String(query.sid), msg_id: String(query.msg_id), prompt: String(query.prompt) });
  await expect(dialog).toHaveCount(0);
  await expect(page.locator(".async-question-card, .question-resume")).toHaveCount(0);
  await expect(page.locator(".supplemental-answer")).toContainText("查看问题");
  expect(relay.commands.filter(c => ["interrupt", "answer_question", "steer"].includes(String(c.type)))).toHaveLength(0);
});

test("async question summary keeps its ordinary answer visible after refresh without detail loading", async ({ page }) => {
  const answer = "普通回复仍然完整显示，不需要展开过程。";
  const relay = await mockRightPanelRelay(page, { seedTurns: [{
    ...ASYNC_HISTORY_TURN, detailLoaded: false,
    processDetailState: "none", detailReasons: [],
    blocks: [...ASYNC_HISTORY_TURN.blocks, {
      kind: "text", message_id: "unphased-answer", text: answer,
      channel: "final", done: true,
    }],
  }] });
  await page.goto("/");
  await openAsyncQuestion(page);
  await page.getByRole("button", { name: "稍后回答", exact: true }).click();
  await expect(page.getByText(answer, { exact: true })).toBeVisible();
  await page.reload();
  await openAsyncQuestion(page);
  await page.getByRole("button", { name: "稍后回答", exact: true }).click();
  await expect(page.getByText(answer, { exact: true })).toHaveCount(1);
  await expect(page.getByText(answer, { exact: true })).toBeVisible();
  expect(relay.commands.filter((c) => ["query", "steer", "get_turn_detail"].includes(String(c.type))))
    .toHaveLength(0);
});

test("async question delivery without questions stays in the running Codex process through refresh and completion", async ({ page }, info) => {
  const sid = "layout-parent";
  const notice = "查到了，原生 ID 已确认，正在继续回归。";
  const finalAnswer = "目录、显示标识与回归检查均已完成。";
  const malformedFrames: string[] = [];
  page.on("console", message => {
    if (message.text().includes("dropping malformed frame")) malformedFrames.push(message.text());
  });
  const turns: NonNullable<Extract<ServerEvent, { type: "history" }>["turns"]> = [];
  const relay = await mockRightPanelRelay(page, {
    retained: false, parentState: "running", historyReply: command => ({
      type: "history", sid: String(command.session_id), session_id: String(command.session_id),
      revision: turns.at(-1)?.done ? "notice-completed" : "notice-running",
      generation: "layout-generation", detail: "summary", events: [], turns, has_more: false,
      in_progress: turns.at(-1)?.done !== true, newest_id: turns.at(-1)?.id,
    }),
  });
  await page.goto("/");
  await expect.poll(() => relay.commands.some(c => c.type === "get_history")).toBe(true);
  relay.emit({ type: "user_msg", sid, msg_id: "notice-user", prompt: "核对这个问题并修复" });
  relay.emit({ type: "turn_binding", sid, msg_id: "notice-user", turn_id: "notice-task" });
  relay.emit({ type: "state", sid, state: "running", msg_id: "notice-user" });
  relay.emit({ type: "tool_use", sid, message_id: "notice-tool-parent", turn_id: "notice-task",
    tool_use_id: "notice-read", tool: "shell", category: "command", input: { command: "read session" } });
  relay.emit({ type: "tool_result", sid, turn_id: "notice-task", tool_use_id: "notice-read",
    content: "native ID confirmed", is_error: false });
  relay.emit({ type: "assistant_msg_start", sid, message_id: "notice-item", turn_id: "notice-task", channel: "final" });
  relay.emit({ type: "delta", sid, message_id: "notice-item", turn_id: "notice-task", channel: "final", text: notice });
  relay.emit({ type: "assistant_msg_end", sid, message_id: "notice-item", turn_id: "notice-task",
    channel: "final", delivery: "async" });
  const row = page.locator('.turn[data-turn-id="notice-user"]');
  await expect(row.locator(".process-commentary")).toContainText(notice);
  await expect(row.locator(".assistant-answer-segment")).toHaveCount(0);
  await expect(row.locator(".turn-process-label")).toContainText("正在处理");
  await expect(row.locator(".turn-working")).toHaveCount(1);
  await expect(page.getByRole("button", { name: "停止", exact: true })).toBeVisible();
  await row.screenshot({ path: info.outputPath("codex-async-notice-running.png") });

  turns.push({ id: "notice-user", clientMsgId: "notice-user", forkPointId: "notice-task",
    prompt: "核对这个问题并修复", done: false, ts: Date.now(), detailEventCount: 0, detailLoaded: true,
    blocks: [{ kind: "text", message_id: "notice-item", channel: "final",
      text: notice, delivery: "async", done: true }],
  });
  await page.reload();
  await expect(row.locator(".process-commentary")).toContainText(notice);
  await expect(row.locator(".assistant-answer-segment")).toHaveCount(0);
  // The Wrapper replays the exact native/browser binding after reconnect.
  // A running snapshot alone deliberately cannot animate an arbitrary row.
  relay.emit({ type: "turn_binding", sid, msg_id: "notice-user", turn_id: "notice-task" });
  relay.emit({ type: "state", sid, state: "running", msg_id: "notice-user" });
  await expect(row.locator(".turn-process-label")).toContainText("正在处理");
  relay.emit({ type: "tool_use", sid, message_id: "notice-late-parent", turn_id: "notice-task",
    tool_use_id: "notice-tests", tool: "shell", category: "command", input: { command: "run tests" } });
  relay.emit({ type: "tool_result", sid, turn_id: "notice-task", tool_use_id: "notice-tests",
    content: "passed", is_error: false });
  await expect(row.locator(".turn-process-label")).toContainText("正在处理");
  relay.emit({ type: "assistant_msg_start", sid, message_id: "notice-final", turn_id: "notice-task", channel: "final" });
  relay.emit({ type: "delta", sid, message_id: "notice-final", turn_id: "notice-task", channel: "final", text: finalAnswer });
  relay.emit({ type: "assistant_msg_end", sid, message_id: "notice-final", turn_id: "notice-task", channel: "final" });
  turns[0] = { ...turns[0], done: true, doneTs: Date.now(), blocks: [...turns[0].blocks,
    { kind: "text", message_id: "notice-final", channel: "final", text: finalAnswer, done: true }] };
  relay.emit({ type: "turn_end", sid, turn_id: "notice-task",
    result: { subtype: "success", duration_ms: 1000, is_error: false } });
  relay.emit({ type: "state", sid, state: "idle" });
  await expect(row.locator(".turn-working")).toHaveCount(0);
  await expect(row.locator(".turn-process-label")).toContainText("已处理");
  await expect(row.locator(".assistant-answer-segment")).toHaveCount(1);
  await expect(row.locator(".assistant-answer-segment")).toHaveText(finalAnswer);
  expect(malformedFrames).toEqual([]);
  expect(relay.commands.filter(c => ["query", "steer", "interrupt"].includes(String(c.type)))).toEqual([]);
});

test("async question live delivery preserves running state and uses steer instead of approval", async ({ page }) => {
  const relay = await mockRightPanelRelay(page);
  await page.goto("/");
  await expect(page.getByText("session layout-p", { exact: true })).toHaveCount(1);
  await expect.poll(() => relay.commands.some((c) => c.type === "get_history")).toBe(true);
  const sid = "layout-parent";
  relay.emit({ type: "user_msg", sid, msg_id: "async-user", prompt: "继续修复" });
  relay.emit({ type: "turn_binding", sid, msg_id: "async-user", turn_id: "async-task" });
  relay.emit({ type: "state", sid, state: "running", msg_id: "async-user" });
  relay.emit({ type: "assistant_msg_start", sid, message_id: "async-item", turn_id: "async-task", channel: "final" });
  relay.emit({ type: "delta", sid, message_id: "async-item", turn_id: "async-task", channel: "final", text: "在哪个设备发生？" });
  for (let i = 0; i < 2; i++) relay.emit({ type: "assistant_msg_end", sid,
    message_id: "async-item", turn_id: "async-task", channel: "final",
    delivery: "async", questions: [ASYNC_QUESTIONS[1]] });
  const card = await openAsyncQuestion(page);
  await expect(card).toHaveCount(1);
  await expect(card.locator(".async-question-hint")).toHaveCount(0);
  await expect(card).not.toContainText("发送后将开始新一轮对话");
  await card.getByLabel("你的回答", { exact: true }).fill("三指拖拽");
  await card.getByLabel("你的回答", { exact: true }).press("Enter");
  await expect.poll(() => relay.commands.filter((c) => c.type === "steer").length).toBe(1);
  expect(relay.commands.find((c) => c.type === "steer")?.sid).toBe(sid);
  expect(relay.commands.filter((c) => ["interrupt", "answer_question", "query"].includes(String(c.type))))
    .toHaveLength(0);
});

test("async question reply keeps an in-flight answer streaming across the steer fence", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { historyReply: () => null });
  await page.goto("/");
  await expect.poll(() => relay.commands.some(c => c.type === "get_history")).toBe(true);
  const sid = "layout-parent";
  let seq = 0;
  const emit = (event: PanelRelayEvent) => relay.emit({ ...event, seq: ++seq });
  emit({ type: "user_msg", sid, msg_id: "stream-user", prompt: "检查当前结果" });
  emit({ type: "turn_binding", sid, msg_id: "stream-user", turn_id: "stream-task" });
  emit({ type: "state", sid, state: "running", msg_id: "stream-user" });
  emit({ type: "assistant_msg_start", sid, message_id: "stream-question", channel: "final" });
  emit({ type: "delta", sid, message_id: "stream-question", channel: "final", text: "要检查哪一项？" });
  emit({ type: "assistant_msg_end", sid, message_id: "stream-question", channel: "final",
    delivery: "async", questions: [{ title: "要检查哪一项？", options: null }] });
  emit({ type: "assistant_msg_start", sid, message_id: "stream-answer", channel: "final" });
  emit({ type: "delta", sid, message_id: "stream-answer", channel: "final", text: "检查结果：周期约 **1" });
  const oldRow = page.locator('.turn[data-turn-id="stream-user"]');
  await expect(oldRow).toContainText("检查结果：周期约");
  const dialog = await openAsyncQuestion(page);
  await dialog.getByLabel("你的回答", { exact: true }).fill("再检查输入");
  await dialog.getByLabel("你的回答", { exact: true }).press("Enter");
  await expect.poll(() => relay.commands.filter(c => c.type === "steer").length).toBe(1);
  const steer = relay.commands.find(c => c.type === "steer")!;
  emit({ type: "turn_steered", sid, msg_id: String(steer.msg_id),
    turn_id: "stream-task", prompt: String(steer.prompt) });
  emit({ type: "delta", sid, message_id: "stream-answer", channel: "final", text: " ms**，" });
  await expect(oldRow.locator("strong")).toHaveText("1 ms");
  emit({ type: "delta", sid, message_id: "stream-answer", channel: "final", text: "输出仍会完整显示。" });
  emit({ type: "assistant_msg_end", sid, message_id: "stream-answer", channel: "final" });
  await expect(oldRow).toContainText("检查结果：周期约 1 ms，输出仍会完整显示。");
  emit({ type: "assistant_msg_start", sid, message_id: "next-answer", turn_id: "stream-task", channel: "final" });
  emit({ type: "delta", sid, message_id: "next-answer", turn_id: "stream-task", channel: "final", text: "继续检查输入。" });
  const newRow = page.locator(`.turn[data-turn-id="${String(steer.msg_id)}"]`);
  await expect(newRow).toContainText("继续检查输入。");
  await expect(newRow).not.toContainText("输出仍会完整显示。");
  expect(relay.commands.filter(c => ["query", "interrupt"].includes(String(c.type)))).toHaveLength(0);
});

test("async question process disclosure stays settled through late updates after a steer", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { historyReply: () => null });
  await page.goto("/");
  await expect.poll(() => relay.commands.some(c => c.type === "get_history")).toBe(true);
  const sid = "layout-parent";
  let seq = 0;
  const emit = (event: PanelRelayEvent) => relay.emit({ ...event, seq: ++seq });
  emit({ type: "user_msg", sid, msg_id: "process-user", prompt: "检查处理过程" });
  emit({ type: "turn_binding", sid, msg_id: "process-user", turn_id: "process-task" });
  emit({ type: "state", sid, state: "running", msg_id: "process-user" });
  emit({ type: "process", sid, item_id: "check-process", turn_id: "process-task",
    kind: "command", phase: "start", status: "running", title: "检查日志" });
  const head = page.locator('.turn[data-turn-id="process-user"] .turn-process-head');
  await expect(head).toHaveAttribute("aria-expanded", "true");
  emit({ type: "turn_steered", sid, msg_id: "next-process-user",
    turn_id: "process-task", prompt: "只检查输入" });
  await expect(head).toHaveAttribute("aria-expanded", "false");
  // Native item activity belongs to its old row even after the visible input
  // fence. Content can keep changing, but must not repeatedly reopen the UI.
  for (let i = 0; i < 2; i++) {
    emit({ type: "process", sid, item_id: "check-process", turn_id: "process-task",
      kind: "command", phase: "update", status: "running", title: "检查日志", output: `后续输出 ${i}` });
    await expect(head).toContainText("正在处理");
    await expect(head).toHaveAttribute("aria-expanded", "false");
    emit({ type: "process", sid, item_id: "check-process", turn_id: "process-task",
      kind: "command", phase: "end", status: "succeeded", title: "检查日志" });
    await expect(head).toContainText("已处理");
    await expect(head).toHaveAttribute("aria-expanded", "false");
  }
  await head.click();
  await expect(head).toHaveAttribute("aria-expanded", "true");
  emit({ type: "process", sid, item_id: "check-process", turn_id: "process-task",
    kind: "command", phase: "update", status: "running", title: "检查日志", output: "最后的输出" });
  emit({ type: "process", sid, item_id: "check-process", turn_id: "process-task",
    kind: "command", phase: "end", status: "succeeded", title: "检查日志" });
  await expect(head).toContainText("已处理");
  await expect(head).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator('.turn[data-turn-id="process-user"] .process-timeline')).toContainText("检查日志");
});

test("async question keyboard sends with Enter, keeps Shift+Enter newlines and dismisses unanswered drafts with Escape", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [ASYNC_HISTORY_TURN] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  const input = dialog.getByLabel("你的回答", { exact: true });
  await input.fill("第一行");
  await input.press("Shift+Enter");
  await page.keyboard.insertText("第二行");
  await expect(input).toHaveValue("第一行\n第二行");
  expect(relay.commands.filter(c => ["query", "steer"].includes(String(c.type)))).toHaveLength(0);
  await input.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(page.locator(".question-resume")).toBeVisible();
  await openAsyncQuestion(page);
  await expect(input).toHaveValue("第一行\n第二行");
  await input.press("Enter");
  await expect.poll(() => relay.commands.filter(c => c.type === "query").length).toBe(1);
  const query = relay.commands.find(c => c.type === "query")!;
  await expect(dialog.getByRole("button", { name: "等待确认" })).toBeDisabled();
  relay.emit({ type: "user_msg", sid: String(query.sid), msg_id: String(query.msg_id), prompt: String(query.prompt) });
  await expect(dialog).toHaveCount(0);
  expect(query.prompt).toContain("回答：第一行\n第二行");
  expect(query.sid).toBe("layout-parent");
  expect(relay.commands.filter(c => ["interrupt", "answer_question", "steer"].includes(String(c.type)))).toHaveLength(0);
});

test("async question keyboard leaves IME confirmation and repeated Enter out of the send path", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [ASYNC_HISTORY_TURN] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  const input = dialog.getByLabel("你的回答", { exact: true });
  await input.fill("中文输入");
  await input.dispatchEvent("compositionstart", { data: "输入" });
  // Some WebKit IME keys omit isComposing; the composition lifecycle still owns them.
  await input.press("Enter");
  await input.press("Escape");
  await expect(dialog).toBeVisible();
  expect(relay.commands.filter(c => ["query", "steer"].includes(String(c.type)))).toHaveLength(0);
  await input.dispatchEvent("compositionend", { data: "输入" });
  await input.dispatchEvent("keydown", { key: "Enter", code: "Enter", isComposing: true });
  await input.dispatchEvent("keydown", { key: "Enter", code: "Enter", keyCode: 229 });
  await input.dispatchEvent("keydown", { key: "Enter", code: "Enter", repeat: true });
  await expect(dialog).toBeVisible();
  expect(relay.commands.filter(c => ["query", "steer"].includes(String(c.type)))).toHaveLength(0);
  await input.press("Enter");
  await expect.poll(() => relay.commands.filter(c => c.type === "query").length).toBe(1);
  const query = relay.commands.find(c => c.type === "query")!;
  expect(query.prompt).toContain("回答：中文输入");
  relay.emit({ type: "user_msg", sid: String(query.sid), msg_id: String(query.msg_id), prompt: String(query.prompt) });
  await expect(dialog).toHaveCount(0);
});

test("async question completion updates reply mode while preserving a collapsed draft", async ({ page }) => {
  // This case is entirely live; an empty canonical history response after the
  // terminal would incorrectly claim that the question never existed.
  const relay = await mockRightPanelRelay(page, { historyReply: () => null });
  await page.goto("/");
  await expect.poll(() => relay.commands.some(c => c.type === "get_history")).toBe(true);
  const sid = "layout-parent";
  relay.emit({ type: "user_msg", sid, msg_id: "async-user", prompt: "继续修复" });
  relay.emit({ type: "turn_binding", sid, msg_id: "async-user", turn_id: "async-task" });
  relay.emit({ type: "state", sid, state: "running", msg_id: "async-user" });
  relay.emit({ type: "assistant_msg_start", sid, message_id: "async-item", turn_id: "async-task", channel: "final" });
  relay.emit({ type: "delta", sid, message_id: "async-item", turn_id: "async-task", channel: "final", text: "用的是什么手势？" });
  relay.emit({ type: "assistant_msg_end", sid, message_id: "async-item", turn_id: "async-task", channel: "final",
    delivery: "async", questions: [{ title: "用的是什么手势？", options: null }] });
  const card = await openAsyncQuestion(page);
  await expect(card.locator(".async-question-hint")).toHaveCount(0);
  await card.getByLabel("你的回答", { exact: true }).fill("三指拖拽");
  await card.getByRole("button", { name: "稍后回答", exact: true }).click();
  relay.emit({ type: "turn_end", sid, turn_id: "async-task", result: { subtype: "success", is_error: false } });
  relay.emit({ type: "state", sid, state: "idle" });
  await expect(page.locator(".question-resume")).toBeVisible();
  await expect(card.getByRole("textbox")).toHaveCount(0);
  await openAsyncQuestion(page);
  await expect(card.getByLabel("你的回答", { exact: true })).toHaveValue("三指拖拽");
  await expect(card).not.toContainText("发送后将开始新一轮对话");
  await expect(card.locator(".async-question-footer .async-question-hint")).toHaveCount(0);
  expect(await card.getAttribute("aria-describedby")).toBeNull();
  await expect(card).not.toContainText("不会中断执行");
  await card.getByRole("button", { name: "提交回答" }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "query").length).toBe(1);
  expect(relay.commands.filter(c => ["steer", "interrupt", "answer_question"].includes(String(c.type)))).toHaveLength(0);
  await expect(card.getByRole("button", { name: "等待确认" })).toBeDisabled();
  await card.getByRole("button", { name: "稍后回答", exact: true }).click();
  await expect(card.getByRole("textbox")).toHaveCount(0);
  // Submission locks input until native acceptance, not the view-only toggle.
  await openAsyncQuestion(page);
  await expect(card.getByRole("textbox")).toBeVisible();
  await expect(card.getByRole("textbox")).toBeDisabled();
  const query = relay.commands.find(c => c.type === "query")!;
  relay.emit({ type: "user_msg", sid, msg_id: String(query.msg_id), prompt: String(query.prompt) });
  await expect(card).toHaveCount(0);
  await expect(page.locator(".question-resume")).toHaveCount(0);
  expect(relay.commands.filter(c => c.type === "query")).toHaveLength(1);
});

test("async question rejected at turn completion keeps the answer and retries through a new query", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [ASYNC_HISTORY_TURN] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  const sid = "layout-parent";
  relay.emit({ type: "state", sid, state: "running" });
  await expect(dialog.locator(".async-question-hint")).toHaveCount(0);
  const input = dialog.getByLabel("你的回答", { exact: true });
  await input.fill("项目在这台设备的工作目录里");
  await input.press("Enter");
  await expect.poll(() => relay.commands.filter(c => c.type === "steer").length).toBe(1);
  const steer = relay.commands.find(c => c.type === "steer")!;
  // A transport ACK and another message's rejection are not this answer's result.
  relay.emit({ type: "command_ack", client_id: String(steer.client_id), cmd_id: String(steer.cmd_id) });
  relay.emit({ type: "error", sid, msg_id: "different-answer", code: "not_steerable", message: "unrelated" });
  await expect(dialog.getByRole("button", { name: "等待确认" })).toBeDisabled();
  await expect(input).toHaveValue("项目在这台设备的工作目录里");
  await expect(dialog).toBeVisible();
  relay.emit({ type: "state", sid, state: "idle" });
  relay.emit({ type: "error", sid, msg_id: String(steer.msg_id), code: "not_steerable",
    message: "Codex 任务已结束，本次引导未发送。" });
  await expect(dialog.getByRole("status")).toHaveText("当前任务已结束，本次引导未发送。请重试以开始新一轮对话。");
  await expect(input).toBeEnabled();
  await expect(input).toHaveValue("项目在这台设备的工作目录里");
  await expect(dialog).toBeVisible();
  await input.press("Enter");
  await expect.poll(() => relay.commands.filter(c => c.type === "query").length).toBe(1);
  const query = relay.commands.find(c => c.type === "query")!;
  expect(query.prompt).toBe(steer.prompt);
  expect(query.msg_id).not.toBe(steer.msg_id);
  relay.emit({ type: "error", sid, msg_id: String(steer.msg_id), code: "not_steerable", message: "late old rejection" });
  await expect(dialog.getByRole("button", { name: "等待确认" })).toBeDisabled();
  relay.emit({ type: "user_msg", sid, msg_id: String(query.msg_id), prompt: String(query.prompt) });
  await expect(dialog).toHaveCount(0);
  await expect(page.locator(".question-resume")).toHaveCount(0);
  expect(relay.commands.filter(c => c.type === "steer")).toHaveLength(1);
  expect(relay.commands.filter(c => c.type === "interrupt")).toHaveLength(0);
});

test("async question acceptance recovered from history closes the reopened pending editor", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [ASYNC_HISTORY_TURN] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  await dialog.getByLabel("你的回答", { exact: true }).fill("保留到确认收到");
  await dialog.getByRole("button", { name: "提交回答" }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "query").length).toBe(1);
  const query = relay.commands.find(c => c.type === "query")!;
  await dialog.getByRole("button", { name: "稍后回答", exact: true }).click();
  await openAsyncQuestion(page);
  await expect(dialog.getByRole("button", { name: "等待确认" })).toBeDisabled();
  await expect(dialog.getByLabel("你的回答", { exact: true })).toHaveValue("保留到确认收到");
  relay.emit({ type: "history", sid: "layout-parent", session_id: "layout-parent",
    revision: "layout-history", generation: "layout-generation", detail: "summary",
    events: [], has_more: false, authoritative: true, in_progress: true,
    turns: [ASYNC_HISTORY_TURN, { id: "native-recovered-reply", clientMsgId: String(query.msg_id),
      prompt: String(query.prompt), done: false, blocks: [] }] });
  await expect(dialog).toHaveCount(0);
  await expect(page.locator(".question-resume")).toHaveCount(0);
  expect(relay.commands.filter(c => c.type === "query")).toHaveLength(1);
});

test("async question unknown steer outcome keeps the draft and prevents duplicate sends", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [ASYNC_HISTORY_TURN] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  relay.emit({ type: "state", sid: "layout-parent", state: "running" });
  await expect(dialog.locator(".async-question-hint")).toHaveCount(0);
  await dialog.getByLabel("你的回答", { exact: true }).fill("只发一次");
  await dialog.getByRole("button", { name: "提交回答" }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "steer").length).toBe(1);
  const steer = relay.commands.find(c => c.type === "steer")!;
  relay.emit({ type: "error", sid: "layout-parent", msg_id: String(steer.msg_id),
    code: "steer_outcome_unknown", message: "private transport detail" });
  await expect(dialog.getByRole("status")).toHaveText("引导已发出，尚未确认是否生效。请先查看后续结果。");
  await expect(dialog.getByLabel("你的回答", { exact: true })).toHaveValue("只发一次");
  await expect(dialog.getByRole("button", { name: "提交回答" })).toBeDisabled();
  await dialog.getByRole("form").evaluate((form: HTMLFormElement) => form.requestSubmit());
  await dialog.getByRole("button", { name: "稍后回答", exact: true }).click();
  await openAsyncQuestion(page);
  await expect(dialog.getByRole("button", { name: "提交回答" })).toBeDisabled();
  expect(relay.commands.filter(c => ["query", "steer"].includes(String(c.type)))).toHaveLength(1);
});

test("async question does not mistake a queued submission for a confirmed answer", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { seedTurns: [ASYNC_HISTORY_TURN] });
  await page.goto("/");
  const dialog = await openAsyncQuestion(page);
  relay.emit({ type: "query_queue", sid: "layout-parent", total_count: 1, total_bytes: 64,
    items: [{ msg_id: "queued-work", kind: "queue", prompt_preview: "原有排队任务",
      image_count: 0, file_count: 0, retained_bytes: 64 }] });
  const input = dialog.getByLabel("你的回答", { exact: true });
  await input.fill("这条回答不能被替换或重复排队");
  await input.press("Enter");
  await expect(dialog.getByRole("status")).toHaveText("当前会话暂不可写，回答已保留，请稍后重试。");
  await expect(input).toHaveValue("这条回答不能被替换或重复排队");
  await expect(dialog).toBeVisible();
  expect(relay.commands.filter(c => ["query", "steer"].includes(String(c.type)))).toHaveLength(0);
});

test("async question from a hidden side chat never steals the main view", async ({ page }) => {
  const relay = await mockRightPanelRelay(page);
  await page.goto("/");
  await expect(page.getByText("session layout-p", { exact: true })).toHaveCount(1);
  const sid = "btw-layout-child";
  relay.emit({ type: "user_msg", sid, msg_id: "async-user", prompt: "side question" });
  relay.emit({ type: "assistant_msg_start", sid, message_id: "async-item", channel: "final" });
  relay.emit({ type: "delta", sid, message_id: "async-item", channel: "final", text: "在哪个设备发生？" });
  relay.emit({ type: "assistant_msg_end", sid, message_id: "async-item", channel: "final",
    delivery: "async", questions: [ASYNC_QUESTIONS[1]] });
  await expect(page.locator(".question-resume")).toHaveCount(0);
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await page.keyboard.press("Control+Shift+k");
  await expect(page.getByRole("dialog", { name: "Codex 想确认一下", exact: true })).toBeVisible();
  const card = await openAsyncQuestion(page);
  await expect(card).toBeVisible();
  await card.getByRole("textbox").fill("手机");
  await card.getByRole("button", { name: "提交回答" }).click();
  await expect.poll(() => relay.commands.filter((c) => c.type === "steer").length).toBe(1);
  expect(relay.commands.find((c) => c.type === "steer")?.sid).toBe(sid);
});

for (const sideChat of [false, true]) {
  test(`async question double submissions preserve the first answer in ${sideChat ? "side chat" : "main chat"}`, async ({ page }) => {
    const twoCards = {
      ...ASYNC_HISTORY_TURN,
      blocks: [ASYNC_HISTORY_TURN.blocks[0],
        { ...ASYNC_HISTORY_TURN.blocks[0], message_id: "async-item-two" }],
    };
    const relay = await mockRightPanelRelay(page, {
      visible: sideChat, seedTurns: sideChat ? [] : [twoCards],
    });
    await page.goto("/");
    if (sideChat) {
      await expect(page.locator(".btw-panel")).toBeVisible();
      await expect.poll(() => relay.commands.some((c) => c.type === "sync_btw")).toBe(true);
      relay.emit({ type: "user_msg", sid: "btw-layout-child", msg_id: "btw-ask",
        prompt: "two questions", seq: 1 });
      let seq = 2;
      for (const message_id of ["async-item", "async-item-two"]) {
        relay.emit({ type: "assistant_msg_start", sid: "btw-layout-child",
          message_id, channel: "final", seq: seq++ });
        relay.emit({ type: "delta", sid: "btw-layout-child", message_id,
          text: "需要补充信息", channel: "final", seq: seq++ });
        relay.emit({ type: "assistant_msg_end", sid: "btw-layout-child", message_id,
          channel: "final", delivery: "async", questions: [ASYNC_QUESTIONS[1]], seq: seq++ });
      }
      relay.emit({ type: "state", sid: "btw-layout-child", state: "idle", seq: seq++ });
    }
    await expect(page.locator(".async-question-card")).toHaveCount(0);
    const dialog = await openAsyncQuestion(page);
    await dialog.getByRole("textbox").fill("只发第一条回答");
    await dialog.getByRole("form").evaluate((form: HTMLFormElement) => {
      form.requestSubmit();
      form.requestSubmit();
    });
    await expect.poll(() => relay.commands.filter(c => c.type === "query").length).toBe(1);
    await expect(dialog.getByRole("button", { name: "等待确认" })).toBeDisabled();
    await dialog.getByRole("button", { name: "稍后回答", exact: true }).click();
    await expect(dialog).toHaveCount(0);
    await openAsyncQuestion(page, 1);
    await expect(dialog.getByRole("button", { name: "提交回答" })).toBeDisabled();
    await dialog.getByRole("form").evaluate((form: HTMLFormElement) => form.requestSubmit());
    expect(relay.commands.filter(c => c.type === "query")).toHaveLength(1);
    const query = relay.commands.find((c) => c.type === "query")!;
    expect(query.sid).toBe(sideChat ? "btw-layout-child" : "layout-parent");
    expect(query.delivery).not.toBe("replace");
    expect(relay.commands.some((c) => c.type === "interrupt")).toBe(false);
    relay.emit({ type: "user_msg", sid: String(query.sid), msg_id: String(query.msg_id), prompt: String(query.prompt) });
    await expect(dialog.getByRole("textbox")).toBeEnabled();
    await dialog.getByRole("textbox").fill("第二条回答的草稿");
    await expect(dialog.getByRole("button", { name: "提交回答" })).toBeEnabled();
    await expect(dialog).toBeVisible(); // The first question's late receipt cannot close the second.
  });
}

async function expectRightPanelSpace(
  page: import("@playwright/test").Page, open: boolean, desktop = true, panelWidth = 520,
) {
  const shell = page.locator(".shell");
  if (open) await expect(shell).toHaveClass(/\bpanel-open\b/);
  else await expect(shell).not.toHaveClass(/\bpanel-open\b/);
  // CSS transitions and persisted resize width must settle to actual geometry.
  await expect(page.locator(".pane")).toHaveCSS(
    "padding-right", open && desktop ? `${panelWidth + 28}px` : "0px");
}

const VIEWER_LINK_SITE = { id: "robot", label: "机器人结构", machine_id: "layout-machine",
  revision: "a".repeat(32), entry: "/viewer/index.html", urls: ["http://localhost:9000/viewer/index.html"] };

test("remote Viewer menu and registered links use a session-scoped panel without engine queries", async ({ page }) => {
  await page.setViewportSize({ width: 1568, height: 881 });
  const relay = await mockRightPanelRelay(page, { secondParent: true, seedTurns: [{
    id: "viewer-link", prompt: "查看结构", done: true, detailLoaded: true,
    blocks: [{ kind: "text", message_id: "viewer-message", done: true, channel: "final",
      text: "[结构预览](http://localhost:9000/viewer/index.html)" }],
  }] });
  let registered = false;
  await page.route("**/api/viewers", (route) => route.fulfill({ json: { enabled: true,
    sites: registered ? [VIEWER_LINK_SITE, { ...VIEWER_LINK_SITE, machine_id: "another-device" }] : [] } }));
  await page.goto("/");
  await expect(page.getByText("session layout-p", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  await page.getByRole("button", { name: /远程预览.*交互页面/ }).click();
  await expect(page.locator(".remote-viewer-panel")).toContainText("本会话还没有页面");
  await expectRightPanelSpace(page, true);
  await page.keyboard.press("Control+b");
  await page.getByText("Second parent", { exact: true }).click();
  await expect(page.locator(".remote-viewer-panel")).toHaveCount(0);
  await expectRightPanelSpace(page, false);
  await page.getByText("Layout parent", { exact: true }).click();
  await expect(page.locator(".remote-viewer-panel")).toBeVisible();
  await page.locator(".remote-viewer-panel .viewer-desktop-close").click();
  await expectRightPanelSpace(page, false);
  registered = true;
  const refreshed = page.waitForResponse("**/api/viewers");
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await (await refreshed).finished();
  await page.getByRole("link", { name: "结构预览", exact: true }).click();
  await expect(page.locator(".remote-viewer-panel")).toContainText("选择生成该页面的设备与预览");
  await expect(page.locator(".viewer-site")).toHaveCount(2);
  const previousPanel = await page.locator(".remote-viewer-panel").elementHandle();
  await page.getByRole("link", { name: "结构预览", exact: true }).click();
  await expect.poll(() => previousPanel!.evaluate((element) => element.isConnected)).toBe(false);
  await expect(page.locator(".remote-viewer-panel")).toContainText("选择生成该页面的设备与预览");
  expect(relay.commands.some((command) => ["query", "fork_btw", "get_file_preview"].includes(String(command.type))))
    .toBe(false);
});

for (const mode of ["disabled", "unregistered", "unrelated", "unavailable", "malformed"] as const) {
  test(`remote Viewer preserves native link navigation when the catalog is ${mode}`, async ({ page, context }) => {
    const href = "http://192.168.56.1/admin";
    const relay = await mockRightPanelRelay(page, { seedTurns: [{
      id: "ordinary-link", prompt: "查看页面", done: true, detailLoaded: true,
      blocks: [{ kind: "text", message_id: "ordinary-link-message", done: true, channel: "final",
        text: `[管理页面](${href})` }],
    }] });
    await context.route(href, (route) => route.fulfill({ contentType: "text/html", body: "native-link-target" }));
    await page.route("**/api/viewers", (route) => route.fulfill(mode === "unavailable"
      ? { status: 503, json: { error: "device_offline" } }
      : { json: mode === "malformed" ? null : { enabled: mode !== "disabled",
        sites: mode === "unregistered" ? [] : [{ ...VIEWER_LINK_SITE,
          urls: mode === "unrelated" ? VIEWER_LINK_SITE.urls : [href] }] } }));
    const catalog = page.waitForResponse("**/api/viewers");
    await page.goto("/");
    await (await catalog).finished();
    const popup = page.waitForEvent("popup");
    await page.getByRole("link", { name: "管理页面", exact: true }).click();
    const original = await popup;
    await expect(original).toHaveURL(href);
    await expect(page.locator(".remote-viewer-panel")).toHaveCount(0);
    expect(relay.commands.some((command) => ["query", "fork_btw", "get_file_preview"].includes(String(command.type))))
      .toBe(false);
    await original.close();
  });
}

test("remote Viewer preserves a first click while its catalog is still loading", async ({ page, context }) => {
  const href = VIEWER_LINK_SITE.urls[0];
  await mockRightPanelRelay(page, { seedTurns: [{
    id: "pending-link", prompt: "查看结构", done: true, detailLoaded: true,
    blocks: [{ kind: "text", message_id: "pending-link-message", done: true, channel: "final",
      text: `[结构预览](${href})` }],
  }] });
  await context.route(href, (route) => route.fulfill({ contentType: "text/html", body: "native-link-target" }));
  let pending: import("@playwright/test").Route | undefined;
  await page.route("**/api/viewers", (route) => { pending = route; });
  await page.goto("/");
  await expect.poll(() => !!pending).toBe(true);
  const popup = page.waitForEvent("popup");
  await page.getByRole("link", { name: "结构预览", exact: true }).click();
  const original = await popup;
  await expect(original).toHaveURL(href);
  await expect(page.locator(".remote-viewer-panel")).toHaveCount(0);
  await pending!.fulfill({ json: { enabled: true, sites: [VIEWER_LINK_SITE] } });
  await original.close();
});

test("remote Viewer registered link waits for its deferred session controller", async ({ page }) => {
  const href = VIEWER_LINK_SITE.urls[0];
  const relay = await mockRightPanelRelay(page, { seedTurns: [{
    id: "deferred-viewer-link", prompt: "查看结构", done: true, detailLoaded: true,
    blocks: [{ kind: "text", message_id: "deferred-viewer-message", done: true, channel: "final",
      text: `[结构预览](${href})` }],
  }] });
  let controller: import("@playwright/test").Route | undefined;
  await page.route("**/src/viewer-pages-controller.tsx*", (route) => { controller = route; });
  await page.route("**/api/viewers", (route) => route.fulfill({ json: { enabled: true, sites: [VIEWER_LINK_SITE] } }));
  let associations = 0;
  await page.route("**/api/viewers/pages", (route) => {
    const body = route.request().postDataJSON();
    if (body.action === "associate") associations++;
    return route.fulfill({ json: { pages: body.action === "associate" ? [{
      id: "deferred-page", ...body.page, label: VIEWER_LINK_SITE.label,
      references: [], turn_ids: [], available: true,
    }] : [] } });
  });
  await page.route("**/api/viewers/open", (route) => route.fulfill({ status: 503, json: { error: "device_offline" } }));
  const catalog = page.waitForResponse("**/api/viewers");
  await page.goto("/");
  await (await catalog).finished();
  await expect.poll(() => !!controller).toBe(true);
  await page.getByRole("link", { name: "结构预览", exact: true }).click();
  await expect(page.locator(".remote-viewer-panel")).toBeVisible();
  await expect(page.locator(".remote-viewer-panel [role=alert]")).toHaveCount(0);
  expect(associations).toBe(0);
  await controller!.continue();
  await expect(page.locator(".remote-viewer-panel [role=alert]")).toContainText("设备已离线");
  expect(associations).toBe(1);
  expect(relay.commands.some((command) => command.type === "query")).toBe(false);
});

test("remote Viewer registered link keeps an original-link fallback after a failed preview", async ({ page, context }) => {
  const href = VIEWER_LINK_SITE.urls[0] + "?camera=front#part";
  await mockRightPanelRelay(page, { seedTurns: [{
    id: "registered-link", prompt: "查看结构", done: true, detailLoaded: true,
    blocks: [{ kind: "text", message_id: "registered-link-message", done: true, channel: "final",
      text: `[结构预览](${href})` }],
  }] });
  await context.route("http://localhost:9000/**", (route) => route.fulfill({ contentType: "text/html", body: "native-link-target" }));
  let catalogReads = 0;
  await page.route("**/api/viewers", (route) => {
    catalogReads++;
    return route.fulfill({ json: { enabled: true, sites: [VIEWER_LINK_SITE] } });
  });
  await page.route("**/api/viewers/open", (route) => route.fulfill({ status: 503, json: { error: "device_offline" } }));
  await page.route("**/api/viewers/pages", (route) => {
    const body = route.request().postDataJSON();
    return route.fulfill({ json: { pages: body.action === "associate" ? [{
      id: "registered-page", ...body.page, label: VIEWER_LINK_SITE.label,
      references: [], turn_ids: [], available: true,
    }] : [] } });
  });
  const catalog = page.waitForResponse("**/api/viewers");
  await page.goto("/");
  await (await catalog).finished();
  const link = page.getByRole("link", { name: "结构预览", exact: true });
  await link.waitFor();
  // Refocusing the parent from an iframe must not drop a freshly resolved link.
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  // Modifier and non-primary clicks must never call the Viewer handler.
  const modifiers = await link.evaluate((node) => {
    return [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }]
      .map((options) => {
        const click = new MouseEvent("click", { bubbles: true, cancelable: true, ...options });
        // Block native test navigation only after React has had a chance to
        // consume it, and record whether the application did so.
        let allowed = false;
        const capture = (event: Event) => { allowed = !event.defaultPrevented; event.preventDefault(); };
        document.addEventListener("click", capture, { once: true });
        node.dispatchEvent(click);
        return allowed;
      });
  });
  expect(modifiers).toEqual([true, true, true, true, true]);
  await expect(page.locator(".remote-viewer-panel")).toHaveCount(0);
  expect(catalogReads).toBe(1);
  await link.click();
  await expect(page.locator(".remote-viewer-panel [role=alert]")).toContainText("设备已离线");
  await page.getByRole("button", { name: "切换预览", exact: true }).click();
  const fallback = page.getByRole("link", { name: "打开原链接", exact: true });
  await expect(fallback).toHaveAttribute("href", href);
  await expect(fallback).toHaveAttribute("rel", "noopener noreferrer");
  const popup = page.waitForEvent("popup");
  await fallback.click();
  const original = await popup;
  await expect(original).toHaveURL(href);
  await original.close();
});

test("remote Viewer drops a cached match as soon as catalog revalidation begins", async ({ page, context }) => {
  const href = VIEWER_LINK_SITE.urls[0];
  await mockRightPanelRelay(page, { seedTurns: [{
    id: "refresh-link", prompt: "查看结构", done: true, detailLoaded: true,
    blocks: [{ kind: "text", message_id: "refresh-link-message", done: true, channel: "final",
      text: `[结构预览](${href})` }],
  }] });
  await context.route(href, (route) => route.fulfill({ contentType: "text/html", body: "native-link-target" }));
  let hold = false;
  let pending: import("@playwright/test").Route | undefined;
  await page.route("**/api/viewers", (route) => {
    if (hold) { pending = route; return; }
    return route.fulfill({ json: { enabled: true, sites: [VIEWER_LINK_SITE] } });
  });
  const catalog = page.waitForResponse("**/api/viewers");
  await page.goto("/");
  await (await catalog).finished();
  hold = true;
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect.poll(() => !!pending).toBe(true);
  const popup = page.waitForEvent("popup");
  await page.getByRole("link", { name: "结构预览", exact: true }).click();
  const original = await popup;
  await expect(original).toHaveURL(href);
  await expect(page.locator(".remote-viewer-panel")).toHaveCount(0);
  await pending!.fulfill({ status: 503, json: { error: "device_offline" } });
  await original.close();
});

test("remote Viewer ignores a late catalog from the previous session", async ({ page, context }) => {
  const href = VIEWER_LINK_SITE.urls[0];
  await page.setViewportSize({ width: 1568, height: 881 });
  await mockRightPanelRelay(page, { secondParent: true, seedTurns: [{
    id: "scoped-link", prompt: "查看结构", done: true, detailLoaded: true,
    blocks: [{ kind: "text", message_id: "scoped-link-message", done: true, channel: "final",
      text: `[结构预览](${href})` }],
  }] });
  await context.route(href, (route) => route.fulfill({ contentType: "text/html", body: "native-link-target" }));
  let first: import("@playwright/test").Route | undefined;
  await page.route("**/api/viewers", (route) => {
    if (!first) { first = route; return; }
    return route.fulfill({ json: { enabled: false, sites: [] } });
  });
  await page.goto("/");
  await expect.poll(() => !!first).toBe(true);
  await page.keyboard.press("Control+b");
  const catalog = page.waitForResponse("**/api/viewers");
  await page.getByText("Second parent", { exact: true }).click();
  await (await catalog).finished();
  await first!.fulfill({ json: { enabled: true, sites: [VIEWER_LINK_SITE] } });
  await expect(page.getByText("session layout-o", { exact: true })).toBeVisible();
  const popup = page.waitForEvent("popup");
  await page.getByRole("link", { name: "结构预览", exact: true }).click();
  const original = await popup;
  await expect(original).toHaveURL(href);
  await expect(page.locator(".remote-viewer-panel")).toHaveCount(0);
  await original.close();
});

for (const engine of ["claude", "codex"] as const) {
  test(`side chat scope center expansion preserves the mounted ${engine} chat and draft`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1568, height: 1000 });
    const relay = await mockRightPanelRelay(page, { visible: true, engine,
      seedTurns: Array.from({ length: 16 }, (_, i) => ({
        id: `center-turn-${i}`, prompt: `侧聊问题 ${i + 1}`, done: true,
        blocks: [{ kind: "text" as const, message_id: `center-answer-${i}`,
          channel: "final" as const, done: true,
          text: `第 ${i + 1} 段。${"切换布局时保留当前阅读位置和正在处理的任务。".repeat(10)}` }],
      })),
    });
    await page.goto("/");
    const panel = page.locator(".btw-panel");
    const input = panel.getByRole("textbox");
    await expect(input).toBeEnabled();
    // BTW restores through live-tail replay, independently of main history.
    const sid = "btw-layout-child";
    relay.emit({ type: "replay_start", sid, generation: "layout-generation",
      from_seq: 0, to_seq: 100, rebuild: true, truncated: false });
    for (let i = 0; i < 16; i++) {
      relay.emit({ type: "user_msg", sid, msg_id: `center-user-${i}`, prompt: `侧聊问题 ${i + 1}` });
      relay.emit({ type: "delta", sid, message_id: `center-answer-${i}`, channel: "final",
        text: `第 ${i + 1} 段。${"切换布局时保留当前阅读位置和正在处理的任务。".repeat(10)}` });
      relay.emit({ type: "assistant_msg_end", sid, message_id: `center-answer-${i}`, channel: "final" });
      relay.emit({ type: "turn_end", sid, turn_id: `center-answer-${i}`, checkpoint_id: `center-user-${i}`,
        result: { subtype: "success", duration_ms: 1000, is_error: false } });
    }
    relay.emit({ type: "replay_end", sid, to_seq: 100, truncated: false });
    await expect(panel.locator(".thread")).toBeVisible();
    await input.fill("尚未发送的侧聊草稿");
    await panel.getByLabel("添加文件", { exact: true }).setInputFiles({
      name: "side-notes.txt", mimeType: "text/plain", buffer: Buffer.from("only this side chat"),
    });
    await expect(panel.locator(".btw-composer>.btw-pastes .attach-file")).toHaveText("side-notes.txt");
    const originalPanel = await panel.elementHandle();
    const originalThread = await panel.locator(".thread").elementHandle();
    const originalInput = await input.elementHandle();
    const docked = (await panel.boundingBox())!;
    await panel.getByRole("button", { name: "居中展开侧边对话", exact: true }).click();
    await expect(panel).toHaveAttribute("aria-modal", "true");
    await expect(panel.getByRole("button", { name: "调整 BTW 面板宽度" })).toHaveCount(0);
    const expanded = (await panel.boundingBox())!;
    expect(expanded.width).toBeGreaterThan(docked.width + 300);
    expect(Math.abs(expanded.x + expanded.width / 2 - 784)).toBeLessThan(2);
    expect(Math.abs(expanded.y + expanded.height / 2 - 500)).toBeLessThan(2);
    await expectRightPanelSpace(page, false);
    expect(await originalPanel!.evaluate(node => node === document.querySelector(".btw-panel"))).toBe(true);
    expect(await originalThread!.evaluate(node => node === document.querySelector(".btw-panel .thread"))).toBe(true);
    expect(await originalInput!.evaluate(node => node === document.querySelector(".btw-panel textarea"))).toBe(true);
    await expect(input).toHaveValue("尚未发送的侧聊草稿");
    await expect(panel.locator(".btw-composer>.btw-pastes .attach-file")).toHaveText("side-notes.txt");
    await expect.poll(() => panel.locator(".thread").evaluate(node =>
      node.scrollHeight - node.clientHeight - node.scrollTop)).toBeLessThan(3);

    // Nested attachment dialogs own Escape; closing them must not collapse BTW.
    await panel.getByRole("button", { name: "添加附件", exact: true }).click();
    const attachmentDialog = page.getByRole("dialog", { name: "添加附件", exact: true });
    await expect(attachmentDialog).toBeVisible();
    for (const key of ["Tab", "Shift+Tab"]) {
      await page.keyboard.press(key);
      expect(await attachmentDialog.evaluate(node => node.contains(document.activeElement))).toBe(true);
    }
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog", { name: "添加附件", exact: true })).toHaveCount(0);
    await expect(panel).toHaveClass(/btw-expanded/);
    await input.focus();
    await page.keyboard.press("Shift+Tab");
    expect(await panel.evaluate(node => node.contains(document.activeElement))).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`btw-centered-${engine}.png`) });
    await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
    await page.screenshot({ path: testInfo.outputPath(`btw-centered-${engine}-dark.png`) });

    await page.keyboard.press("Escape");
    await expect(panel).not.toHaveClass(/btw-expanded/);
    await expectRightPanelSpace(page, true);
    await expect(panel.getByRole("button", { name: "调整 BTW 面板宽度" })).toBeVisible();
    expect((await panel.boundingBox())!.width).toBe(docked.width);
    const thread = panel.locator(".thread");
    await thread.dispatchEvent("wheel", { deltaY: -500 });
    await thread.evaluate(node => { node.scrollTop -= 500; node.dispatchEvent(new Event("scroll")); });
    // Switch before the wheel idle timer fires: layout must retain this row.
    const readingTurn = await thread.evaluate(node => {
      const bounds = node.getBoundingClientRect();
      // The history virtualizer retains the visible row nearest the top edge.
      return [...node.querySelectorAll<HTMLElement>("[data-turn-id]")]
        .filter(row => row.getBoundingClientRect().bottom > bounds.top
          && row.getBoundingClientRect().top < bounds.bottom)
        .sort((a, b) => Math.abs(a.getBoundingClientRect().top - bounds.top)
          - Math.abs(b.getBoundingClientRect().top - bounds.top))[0]?.dataset.turnId;
    });
    expect(readingTurn).toBeTruthy();
    await page.keyboard.press("Control+Shift+e");
    await expect(panel).toHaveClass(/btw-expanded/);
    await expect.poll(() => thread.evaluate((node, id) => {
      const bounds = node.getBoundingClientRect();
      const row = node.querySelector(`[data-turn-id="${id}"]`)?.getBoundingClientRect();
      return !!row && row.bottom > bounds.top && row.top < bounds.bottom;
    }, readingTurn)).toBe(true);
    await panel.getByRole("button", { name: "收回右侧", exact: true }).click();
    await expect(panel).not.toHaveClass(/btw-expanded/);
    await page.keyboard.press("Control+Shift+k");
    await expect(panel).toHaveCount(0);
    // The new shortcut opens a retained chat directly in the centered view.
    await page.keyboard.press("Control+Shift+e");
    await expect(panel).toHaveClass(/btw-expanded/);
    await expect(input).toHaveValue("尚未发送的侧聊草稿");
    await expect(panel.locator(".btw-composer>.btw-pastes .attach-file")).toHaveText("side-notes.txt");
    await page.keyboard.press("Control+Shift+k");
    await page.keyboard.press("Control+Shift+k");
    await expect(panel).not.toHaveClass(/btw-expanded/);
    expect(relay.commands.filter(command =>
      ["open_btw", "close_btw", "query", "steer", "interrupt", "set_perm", "set_collaboration_mode"]
        .includes(String(command.type)))).toEqual([]);
  });
}

for (const engine of ["claude", "codex"] as const) {
  test(`side chat scope centered dismissal restores the previous ${engine} layout`, async ({ page }) => {
    await page.setViewportSize({ width: 1568, height: 1000 });
    const relay = await mockRightPanelRelay(page, { visible: false, engine });
    await page.goto("/");
    await expect(page.getByText("session layout-p", { exact: true })).toBeVisible();
    const panel = page.locator(".btw-panel");
    await expect(panel).toHaveCount(0);

    for (const origin of ["hidden", "docked"] as const) {
      for (const dismiss of ["shortcut", "outside", "escape"] as const) {
        await page.keyboard.press("Meta+Shift+e");
        await expect(panel).toHaveClass(/btw-expanded/);
        await expectRightPanelSpace(page, false);
        if (origin === "hidden" && dismiss === "shortcut") {
          await panel.getByRole("textbox").fill("关闭大窗也要保留的草稿");
          await panel.getByLabel("添加文件", { exact: true }).setInputFiles({
            name: "retained.txt", mimeType: "text/plain", buffer: Buffer.from("retained attachment"),
          });
        }
        await expect(panel.getByRole("textbox")).toHaveValue("关闭大窗也要保留的草稿");
        await expect(panel.locator(".attach-file")).toHaveText("retained.txt");
        // Clicking inside never dismisses the overlay.
        await panel.getByRole("textbox").click();
        await expect(panel).toHaveClass(/btw-expanded/);
        if (dismiss === "shortcut") await page.keyboard.press("Meta+Shift+e");
        else if (dismiss === "escape") await page.keyboard.press("Escape");
        else await page.locator(".panel-expanded-backdrop").click({ position: { x: 4, y: 4 } });
        if (origin === "hidden") await expect(panel).toHaveCount(0);
        else {
          await expect(panel).toBeVisible();
          await expect(panel).not.toHaveClass(/btw-expanded/);
        }
        await expect(page.locator(".panel-expanded-backdrop")).toHaveCount(0);
        await expectRightPanelSpace(page, origin === "docked");
      }
      if (origin === "hidden") {
        // The explicit header action still docks a directly opened overlay.
        await page.keyboard.press("Meta+Shift+e");
        await panel.getByRole("button", { name: "收回右侧", exact: true }).click();
        await expect(panel).not.toHaveClass(/btw-expanded/);
        await expectRightPanelSpace(page, true);
      }
    }
    // A later direct open must not inherit the previous docked return target.
    await page.keyboard.press("Control+Shift+k");
    await expect(panel).toHaveCount(0);
    await page.keyboard.press("Meta+Shift+e");
    await expect(panel).toHaveClass(/btw-expanded/);
    await page.keyboard.press("Escape");
    await expect(panel).toHaveCount(0);
    expect(relay.commands.filter(command =>
      ["open_btw", "close_btw", "query", "steer", "interrupt"].includes(String(command.type)))).toEqual([]);
  });
}

for (const engine of ["claude", "codex"] as const) {
  test(`side chat scope returning to ${engine} restores the panel without taking focus`, async ({ page }) => {
    await page.setViewportSize({ width: 1568, height: 1000 });
    const relay = await mockRightPanelRelay(page, { engine, secondParent: true });
    await page.goto("/");
    const panel = page.locator(".btw-panel");
    const sideInput = panel.getByRole("textbox");
    const mainInput = page.locator("textarea[data-chat-composer]").first();
    await expect(mainInput).toBeEnabled();
    await mainInput.fill("原会话主聊天草稿");
    await page.keyboard.press("Control+Shift+k");
    await expect(sideInput).toBeFocused();
    await page.keyboard.type("retained side draft");

    await page.keyboard.press("Control+b");
    await page.getByText("Second parent", { exact: true }).click();
    await expect(page.getByText("session layout-o", { exact: true })).toBeVisible();
    await expect(panel).toHaveCount(0);
    await mainInput.fill("在另一个会话处理工作");
    await mainInput.evaluate(node => node.blur());
    await page.getByText("Layout parent", { exact: true }).click();
    await expect(page.getByText("session layout-p", { exact: true })).toBeVisible();
    await expect(sideInput).toHaveValue("retained side draft");
    await expect(sideInput).not.toBeFocused();
    await expect(mainInput).toHaveValue("原会话主聊天草稿");
    await page.keyboard.press("/");
    await expect(mainInput).toBeFocused();
    await expect(mainInput).toHaveValue("原会话主聊天草稿");
    await expect(sideInput).toHaveValue("retained side draft");

    await page.reload();
    await expect(sideInput).toBeEnabled();
    await expect(sideInput).not.toBeFocused();
    await page.keyboard.press("/");
    await expect(mainInput).toBeFocused();
    expect(relay.commands.some(c => ["query", "steer", "open_btw", "close_btw"]
      .includes(String(c.type)))).toBe(false);
  });

  test(`side chat scope explicitly opening ${engine} focuses its composer once`, async ({ page }) => {
    await page.setViewportSize({ width: 1568, height: 1000 });
    const relay = await mockRightPanelRelay(page, { engine });
    await page.goto("/");
    const panel = page.locator(".btw-panel");
    const sideInput = panel.getByRole("textbox");
    const mainInput = page.locator("textarea[data-chat-composer]").first();
    await expect(mainInput).toBeEnabled();
    await mainInput.fill("主会话草稿");

    await page.keyboard.press("Control+Shift+k");
    await expect(sideInput).toBeFocused();
    await page.keyboard.type("side draft");
    await expect(sideInput).toHaveValue("side draft");
    await mainInput.focus();
    await panel.getByRole("button", { name: "居中展开侧边对话", exact: true }).click();
    await expect(sideInput).toBeFocused();
    await panel.getByRole("button", { name: "收回右侧", exact: true }).click();
    await expect(sideInput).toBeFocused();

    // Replays and new output must not pull focus out of the main draft.
    await mainInput.focus();
    relay.emit({ type: "user_msg", sid: "btw-layout-child", msg_id: "focus-user", prompt: "side work" });
    relay.emit({ type: "delta", sid: "btw-layout-child", message_id: "focus-answer",
      channel: "final", text: "background output" });
    await expect(panel.getByText("background output", { exact: true })).toBeVisible();
    await expect(mainInput).toBeFocused();

    await page.keyboard.press("Control+Shift+k");
    await expect(panel).toHaveCount(0);
    await page.keyboard.press("Meta+Shift+e");
    await expect(sideInput).toBeFocused();
    await expect(sideInput).toHaveValue("side draft");
    await page.keyboard.press("Meta+Shift+e");
    await expect(panel).toHaveCount(0);

    // The command palette used to schedule a second focus back to main.
    await mainInput.fill("/btw");
    await page.getByRole("button", { name: /\/btw.*侧边对话/ }).click();
    await expect(sideInput).toBeFocused();
    await page.keyboard.type(" continues");
    await expect(sideInput).toHaveValue("side draft continues");
    await expect(mainInput).toHaveValue("");
    expect(relay.commands.some(c => ["query", "steer", "open_btw", "close_btw"]
      .includes(String(c.type)))).toBe(false);
  });

  test(`side chat scope slash focuses the centered ${engine} composer and otherwise the main composer`, async ({ page }) => {
    await page.setViewportSize({ width: 1568, height: 1000 });
    const relay = await mockRightPanelRelay(page, { visible: true, engine });
    await page.goto("/");
    const panel = page.locator(".btw-panel");
    const sideInput = panel.getByRole("textbox");
    const mainInput = page.locator("textarea[data-chat-composer]").first();
    const close = panel.getByRole("button", { name: "收起侧边对话", exact: true });
    await expect(sideInput).toBeEnabled();
    await expect(mainInput).toBeEnabled();
    await mainInput.fill("主会话草稿");

    // A docked side chat never takes the main composer's slash shortcut.
    await close.focus();
    await page.keyboard.press("/");
    await expect(mainInput).toBeFocused();
    await expect(mainInput).toHaveValue("主会话草稿");
    await page.keyboard.press("/");
    await expect(mainInput).toHaveValue("主会话草稿/");

    await page.keyboard.press("Meta+Shift+e");
    await expect(panel).toHaveClass(/btw-expanded/);
    await close.focus();
    await page.keyboard.press("/");
    await expect(sideInput).toBeFocused();
    await expect(sideInput).toHaveValue("");
    await sideInput.fill("侧聊草稿");
    await page.keyboard.press("/");
    await expect(sideInput).toHaveValue("侧聊草稿/");

    // A nested modal still owns focus, even when its owner is centered BTW.
    await panel.getByRole("button", { name: "添加附件", exact: true }).click();
    const attachments = page.getByRole("dialog", { name: "添加附件", exact: true });
    await expect(attachments).toBeVisible();
    await attachments.getByRole("button").first().focus();
    await page.keyboard.press("/");
    expect(await attachments.evaluate(node => node.contains(document.activeElement))).toBe(true);
    await page.keyboard.press("Escape");
    await expect(attachments).toHaveCount(0);
    await close.focus();
    await page.keyboard.press("/");
    await expect(sideInput).toBeFocused();

    await page.keyboard.press("Meta+Shift+e");
    await expect(panel).not.toHaveClass(/btw-expanded/);
    await close.focus();
    await page.keyboard.press("/");
    await expect(mainInput).toBeFocused();
    await page.keyboard.press("Control+Shift+k");
    await expect(panel).toHaveCount(0);
    await mainInput.evaluate(node => node.blur());
    await page.keyboard.press("/");
    await expect(mainInput).toBeFocused();

    // Opening directly from hidden also routes slash to the retained side draft.
    await page.keyboard.press("Meta+Shift+e");
    await expect(panel).toHaveClass(/btw-expanded/);
    await expect(sideInput).toBeEnabled();
    await close.focus();
    await page.keyboard.press("/");
    await expect(sideInput).toBeFocused();
    await expect(sideInput).toHaveValue("侧聊草稿/");
    await expect(mainInput).toHaveValue("主会话草稿/");
    expect(relay.commands.some(c => ["query", "steer", "interrupt", "open_btw", "close_btw"]
      .includes(String(c.type)))).toBe(false);
  });
}

for (const afterOpen of ["wait", "type-main", "close"] as const) {
  test(`side chat scope delayed creation respects focus intent: ${afterOpen}`, async ({ page }) => {
    await page.setViewportSize({ width: 1568, height: 1000 });
    const relay = await mockRightPanelRelay(page, { retained: false });
    await page.goto("/");
    const mainInput = page.locator("textarea[data-chat-composer]").first();
    await expect(mainInput).toBeEnabled();
    await page.keyboard.press("Control+Shift+k");
    const panel = page.locator(".btw-panel");
    await expect(panel.getByRole("textbox")).toBeDisabled();
    await expect.poll(() => relay.commands.filter(c => c.type === "open_btw").length).toBe(1);
    const open = relay.commands.find(c => c.type === "open_btw")!;
    if (afterOpen === "type-main") {
      await mainInput.click();
      await page.keyboard.type("keep writing here");
    } else if (afterOpen === "close") {
      await panel.getByRole("button", { name: "收起侧边对话", exact: true }).click();
      await expect(panel).toHaveCount(0);
      await mainInput.focus();
    }
    relay.emit({ type: "btw_opened", sid: "btw-layout-child",
      btw_sid: "btw-layout-child", parent_sid: "layout-parent", engine: "codex",
      created_at: 1, revision: 2, request_id: String(open.request_id) });
    if (afterOpen === "close") {
      // Wait for a subsequent frame so the assertion observes the late open.
      relay.emit({ type: "delta", sid: "layout-parent", message_id: "focus-ack",
        channel: "final", text: "late open processed" });
      await expect(page.getByText("late open processed", { exact: true })).toBeVisible();
      await expect(panel).toHaveCount(0);
      await expect(mainInput).toBeFocused();
      await page.keyboard.press("Control+Shift+k");
      await expect(panel.getByRole("textbox")).toBeFocused();
    } else {
      await expect(panel.getByRole("textbox")).toBeEnabled();
      if (afterOpen === "wait") await expect(panel.getByRole("textbox")).toBeFocused();
      else await expect(mainInput).toBeFocused();
    }
  });
}

test("side chat scope slash never falls through a read-only centered chat", async ({ page }) => {
  await page.setViewportSize({ width: 1568, height: 1000 });
  await mockRightPanelRelay(page, { visible: true, btwReadOnly: true });
  await page.goto("/");
  const panel = page.locator(".btw-panel");
  await expect(panel.getByRole("textbox")).toBeDisabled();
  await page.keyboard.press("Meta+Shift+e");
  await expect(panel).toHaveClass(/btw-expanded/);
  const close = panel.getByRole("button", { name: "收起侧边对话", exact: true });
  await close.focus();
  await page.keyboard.press("/");
  await expect(close).toBeFocused();
  await expect(page.locator("textarea[data-chat-composer]").first()).not.toBeFocused();
});

for (const engine of ["claude", "codex"] as const) {
  test(`side chat scope centered Shift+Tab navigates without changing ${engine} mode`, async ({ page, browserName }) => {
    await page.setViewportSize({ width: 1568, height: 1000 });
    const relay = await mockRightPanelRelay(page, { visible: true, engine });
    await page.goto("/");
    const panel = page.locator(".btw-panel");
    const input = panel.getByRole("textbox");
    await expect(input).toBeEnabled();
    await panel.getByRole("button", { name: "居中展开侧边对话", exact: true }).click();
    const dock = panel.getByRole("button", { name: "收回右侧", exact: true });
    const close = panel.getByRole("button", { name: "收起侧边对话", exact: true });
    await close.focus();
    await expect(close).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(close).not.toBeFocused();
    // Mobile WebKit's native Tab order skips buttons, unlike desktop Chromium.
    if (browserName === "chromium") await expect(dock).toBeFocused();
    expect(await panel.evaluate(node => node.contains(document.activeElement))).toBe(true);
    const changes = () => relay.commands.filter(c =>
      ["set_perm", "set_collaboration_mode"].includes(String(c.type)));
    expect(changes()).toEqual([]);
    await page.keyboard.press("Escape");
    await page.locator("textarea[data-chat-composer]").first().focus();
    await page.keyboard.press("Shift+Tab");
    await expect.poll(() => changes().length).toBe(1);
  });
}

for (const engine of ["claude", "codex"] as const) {
  for (const popover of ["Token 用量", "计划进度"]) {
    test(`side chat scope centered ${engine} ${popover} keeps backward navigation and Escape scoped`, async ({ page }) => {
      await page.setViewportSize({ width: 1568, height: 1000 });
      const relay = await mockRightPanelRelay(page, { visible: true, engine });
      await page.goto("/");
      const panel = page.locator(".btw-panel");
      await expect(panel.getByRole("textbox")).toBeEnabled();
      const sid = "btw-layout-child";
      relay.emit({ type: "user_msg", sid, msg_id: "popover-user", prompt: "检查浮层键盘操作" });
      relay.emit({ type: "turn_binding", sid, msg_id: "popover-user", turn_id: "popover-turn" });
      relay.emit({ type: "delta", sid, message_id: "popover-answer", text: "正在检查" });
      relay.emit({ type: "turn_usage", sid, turn_id: "popover-turn",
        usage: { input_tokens: 32000, output_tokens: 1200 } });
      relay.emit({ type: "turn_plan", sid, turn_id: "popover-turn", item_id: "popover-plan",
        plan: [{ step: "验证浮层", status: "inProgress" }] });
      await panel.getByRole("button", { name: "居中展开侧边对话", exact: true }).click();
      const trigger = panel.getByRole("button", { name: popover === "Token 用量"
        ? "查看 token 用量" : /查看计划进度/ });
      const card = page.getByRole("dialog", { name: popover, exact: true });
      await trigger.click();
      await expect(card).toBeVisible();
      // The informative portal remains open, but does not own keyboard focus.
      const close = panel.getByRole("button", { name: "收起侧边对话", exact: true });
      await close.focus();
      await page.keyboard.press("Shift+Tab");
      await expect(close).not.toBeFocused();
      expect(await panel.evaluate(node => node.contains(document.activeElement))).toBe(true);
      await expect(card).toBeVisible();
      // Both boundary directions stay within the expanded panel as well.
      for (const backward of [false, true]) {
        await panel.evaluate((node, reverse) => {
          const buttons = [...node.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')]
            .filter(button => button.getClientRects().length);
          (reverse ? buttons[0] : buttons.at(-1))!.focus();
        }, backward);
        await page.keyboard.press(backward ? "Shift+Tab" : "Tab");
        expect(await panel.evaluate(node => node.contains(document.activeElement))).toBe(true);
      }
      await page.keyboard.press("Escape");
      await expect(card).toHaveCount(0);
      await expect(panel).toHaveClass(/btw-expanded/);
      await page.keyboard.press("Escape");
      await expect(panel).not.toHaveClass(/btw-expanded/);
      expect(relay.commands.filter(c =>
        ["set_perm", "set_collaboration_mode"].includes(String(c.type)))).toEqual([]);
    });
  }
}

for (const expanded of [false, true]) {
  test(`side chat scope speed menu blocks layout shortcuts when expanded=${expanded}`, async ({ page }) => {
    await page.setViewportSize({ width: 1568, height: 1000 });
    const relay = await mockRightPanelRelay(page, { visible: true, engine: "codex" });
    await page.goto("/");
    const panel = page.locator(".btw-panel");
    await expect(panel.getByRole("textbox")).toBeEnabled();
    if (expanded) await page.keyboard.press("Control+Shift+e");
    await panel.getByRole("button", { name: /^速度：/ }).click();
    const menu = page.getByRole("menu", { name: /的速度$/ });
    await expect(menu).toBeVisible();
    for (const key of ["/", "Control+Shift+e", "Meta+Shift+e", "Control+Shift+k", "Meta+Shift+k"]) {
      await page.keyboard.press(key);
      await expect(menu).toBeVisible();
      await expect(menu.getByRole("menuitemradio").first()).toBeFocused();
      await expect(panel).toBeVisible();
      if (expanded) await expect(panel).toHaveClass(/btw-expanded/);
      else await expect(panel).not.toHaveClass(/btw-expanded/);
    }
    await page.keyboard.press("Escape");
    await expect(menu).toHaveCount(0);
    await page.keyboard.press("Control+Shift+e");
    if (expanded) await expect(panel).not.toHaveClass(/btw-expanded/);
    else await expect(panel).toHaveClass(/btw-expanded/);
    expect(relay.commands.some(c => ["query", "steer", "interrupt", "set_service_tier"]
      .includes(String(c.type)))).toBe(false);
  });
}

test("side chat scope centered speed menu keeps keyboard focus inside its owner", async ({ page }) => {
  await page.setViewportSize({ width: 1568, height: 1000 });
  const relay = await mockRightPanelRelay(page, { visible: true, engine: "codex" });
  await page.goto("/");
  const panel = page.locator(".btw-panel");
  await expect(panel.getByRole("textbox")).toBeEnabled();
  await panel.getByRole("button", { name: "居中展开侧边对话", exact: true }).click();
  const trigger = panel.getByRole("button", { name: /^速度：/ });
  const menu = page.getByRole("menu", { name: /的速度$/ });
  for (const key of ["Tab", "Shift+Tab", "Escape"]) {
    await trigger.click();
    await expect(menu).toBeVisible();
    await expect(menu.getByRole("menuitemradio").first()).toBeFocused();
    await page.keyboard.press(key);
    await expect(menu).toHaveCount(0);
    await expect(panel).toHaveClass(/btw-expanded/);
    expect(await panel.evaluate(node => node.contains(document.activeElement))).toBe(true);
    if (key === "Escape") await expect(trigger).toBeFocused();
  }
  await page.keyboard.press("Escape");
  await expect(panel).not.toHaveClass(/btw-expanded/);
  expect(relay.commands.some(c => ["query", "steer", "interrupt", "set_service_tier",
    "set_perm", "set_collaboration_mode"].includes(String(c.type)))).toBe(false);
});

test("side chat scope centered attachments fit a narrow viewport", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockRightPanelRelay(page, { visible: true });
  await page.goto("/");
  const panel = page.locator(".btw-panel");
  await panel.getByRole("button", { name: "居中展开侧边对话", exact: true }).click();
  await panel.getByLabel("添加文件", { exact: true }).setInputFiles([
    { name: "一份名称很长的侧聊附件-验证窄屏输入区不溢出.txt", mimeType: "text/plain", buffer: Buffer.from("notes") },
    { name: "side.png", mimeType: "image/png", buffer: staticPng() },
  ]);
  await expect(panel.locator(".btw-composer>.btw-pastes .attach-image-preview")).toHaveCount(1);
  expect(await panel.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
  const card = (await panel.locator(".btw-input-card").boundingBox())!;
  expect(card.x).toBeGreaterThan(0);
  expect(card.x + card.width).toBeLessThan(390);
  expect(card.y + card.height).toBeLessThan(844);
  await panel.getByRole("button", { name: "预览待发送图片 1" }).click();
  await expect(page.getByRole("dialog", { name: "图片预览", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "图片预览", exact: true })).toHaveCount(0);
  await expect(panel).toHaveClass(/btw-expanded/);
  await panel.getByRole("button", { name: "移除待发送图片 1" }).click();
  await expect(panel.locator(".attach-image-preview")).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("btw-centered-mobile.png") });
});

test("session message tool keeps its receipt in chronology while later work continues", async ({ page }, info) => {
  const message = "请整理当前任务的目标、已完成的改动、验证结果与待办事项，回复一份完整交接说明。";
  const relay = await mockRightPanelRelay(page, { engine: "claude", retained: false,
    secondParent: true, historyReply: () => null });
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", { configurable: true,
      value: { writeText: async (text: string) => { document.documentElement.dataset.copiedMessage = text; } } });
  });
  await page.goto("/");
  await expect.poll(() => relay.commands.some(c => c.type === "get_history")).toBe(true);
  const sid = "layout-parent", turnId = "message-turn";
  relay.emit({ type: "user_msg", sid, msg_id: turnId, prompt: "确认交接信息后继续检查" });
  relay.emit({ type: "turn_binding", sid, msg_id: turnId, turn_id: turnId });
  relay.emit({ type: "state", sid, state: "running" });
  relay.emit({ type: "tool_use", sid, turn_id: turnId, message_id: "assistant-send",
    tool_use_id: "send-first", tool: "mcp__cc_remote_messages__send_message", category: "mcp",
    input: { session_ref: "ccr:claude:layout-other", message, request_id: "first" } });
  const turn = page.locator(`[data-turn-id="${turnId}"]`);
  await turn.locator(".tool-group-h").click();
  const first = turn.locator(".session-message-tool").first();
  await expect(first.getByRole("status")).toHaveText("发送中");
  await first.locator(".tool-h").click();
  const node = await first.elementHandle();
  await expect(first.getByRole("button", { name: "进入会话 Second parent" })).toBeEnabled();
  await first.getByRole("button", { name: "复制发送内容", exact: true }).click();
  await expect(page.locator("html")).toHaveAttribute("data-copied-message", message);
  relay.emit({ type: "tool_result", sid, turn_id: turnId, tool_use_id: "send-first",
    content: '[{"type":"text","text":"{\\"status\\":\\"accepted\\"}"}]', is_error: false });
  await expect(first.getByRole("status")).toHaveText("已发送");
  relay.emit({ type: "assistant_msg_start", sid, turn_id: turnId, message_id: "later-thinking", channel: "thinking" });
  relay.emit({ type: "delta", sid, turn_id: turnId, message_id: "later-thinking", channel: "thinking",
    text: "继续检查本地改动。" });
  relay.emit({ type: "assistant_msg_end", sid, turn_id: turnId, message_id: "later-thinking", channel: "thinking" });
  relay.emit({ type: "tool_use", sid, turn_id: turnId, message_id: "later-assistant",
    tool_use_id: "send-second", tool: "mcp__cc_remote_messages__send_message", category: "mcp",
    input: { session_ref: "ccr:claude:missing", message: "另一条独立消息", request_id: "second" } });
  await turn.locator(".tool-group-h").last().click();
  const second = turn.locator(".session-message-tool").last();
  await expect(second.getByRole("status")).toHaveText("发送中");
  relay.emit({ type: "tool_result", sid, turn_id: turnId, tool_use_id: "send-second",
    content: '{"status":"rejected","reason":"target_unavailable"}', is_error: true });
  await expect(second.getByRole("status")).toHaveText("发送失败");
  await second.locator(".tool-h").click();
  await expect(second.locator(".session-message-tool-target button")).toBeDisabled();
  await expect(first.getByRole("status")).toHaveText("已发送");
  expect(await node!.evaluate(el => el.isConnected && el.hasAttribute("open"))).toBe(true);
  expect(await first.evaluate(el => !!(el.compareDocumentPosition(
    [...el.closest(".turn")!.querySelectorAll(".prose")].find(p => p.textContent?.includes("继续检查本地改动。"))!,
  ) & Node.DOCUMENT_POSITION_FOLLOWING))).toBe(true);
  await expect(turn.locator(".session-message-sent")).toHaveCount(0);
  await expect(turn.locator(".turn-working")).not.toContainText("已发送");
  await expect(first.locator(".tool-raw[open]")).toHaveCount(0);
  expect(await first.evaluate(el => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(1);
  await first.scrollIntoViewIfNeeded();
  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await page.screenshot({ path: info.outputPath("inline-send-tool.png") });
});

for (const space of ["code", "work"] as const) {
  test(`web update remains usable after an incompatible ${space} handshake leaves catalog reads unacknowledged`, async ({ page }) => {
    await mockRightPanelRelay(page, { retained: false });
    await page.addInitScript(space => localStorage.setItem("cc_remote_space", space), space);
    const commands: Record<string, unknown>[] = [];
    await page.routeWebSocket(/\/ws(?:\?|$)/, socket => {
      socket.onMessage(raw => {
        const command = JSON.parse(String(raw));
        commands.push(command);
        // App's connected callback sends these immediately after hello, before
        // the browser can receive a protocol rejection. Never acknowledge them.
        if (command.type === "get_models") {
          socket.send(JSON.stringify({ v: PROTOCOL_VERSION + 1, ts: 1, type: "error",
            code: "protocol", message: "first frame must be a client hello" }));
          socket.close({ code: 4406, reason: "protocol version mismatch" });
        }
      });
    });
    const next = { build_id: "e".repeat(64), version: "5.3.8", protocol: PROTOCOL_VERSION + 1 };
    await page.route("**/cc-remote-build.json?*", route => route.fulfill({ json: next }));
    await page.route("**/healthz?*", route => route.fulfill({ json: next }));
    await page.goto("/");
    const status = page.getByRole("button", { name: "状态详情：有更新", exact: true });
    await expect(status).toBeVisible();
    await expect.poll(() => commands.some(c => c.type === "get_models")).toBe(true);
    expect(commands.some(c => c.type === "list_sessions" && c.space === space)).toBe(true);
    if (space === "work") expect(commands.some(c => c.type === "get_work_dashboard")).toBe(true);
    await status.click();
    const navigation = page.waitForEvent("framenavigated", frame => frame === page.mainFrame());
    await page.getByRole("button", { name: "更新页面", exact: true }).click();
    await navigation;
    expect(commands.some(c => ["query", "new_session", "steer", "save_markdown"].includes(String(c.type)))).toBe(false);
  });
}

for (const duringProbe of [false, true]) {
  test(`web update protects Markdown edited ${duringProbe ? "during" : "before"} its version check and retries after saving`, async ({ page }) => {
    await page.setViewportSize({ width: 1568, height: 1000 });
    const path = "/tmp/layout/overview.md";
    const relay = await mockRightPanelRelay(page, { retained: false, seedTurns: [{
      id: "document", prompt: "查看文档", done: true,
      blocks: [{ kind: "text", message_id: "answer", text: `[技术方案](${path})`, done: true }],
    }] });
    const next = { build_id: "f".repeat(64), version: "5.3.7", protocol: PROTOCOL_VERSION };
    let holdProbe = false;
    let releaseProbe: (() => void) | undefined;
    await page.route("**/cc-remote-build.json?*", async route => {
      if (holdProbe) {
        holdProbe = false;
        await new Promise<void>(resolve => { releaseProbe = resolve; });
      }
      await route.fulfill({ json: next });
    });
    await page.route("**/healthz?*", route => route.fulfill({ json: next }));
    let unloadDialogs = 0;
    page.on("dialog", async dialog => { unloadDialogs++; await dialog.dismiss(); });
    await page.goto("/");
    await page.getByRole("button", { name: `在 Remote 中打开 ${path}`, exact: true }).click();
    await expect.poll(() => relay.commands.some(c => c.type === "get_file_preview")).toBe(true);
    const request = relay.commands.find(c => c.type === "get_file_preview")!;
    relay.emit({ type: "file_preview", sid: "layout-parent", path, request_id: String(request.request_id),
      format: "markdown", content: "# 技术方案", size: 16, mtime_ns: "1", revision: "a".repeat(64), writable: true });
    const file = page.locator(".artifact-panel");
    await file.getByRole("button", { name: "源码", exact: true }).click();
    const editor = file.getByRole("textbox", { name: "Markdown 源码编辑器" });
    const draft = "# 未保存的草稿\n\n更新时必须保留";
    const status = page.getByRole("button", { name: "状态详情：有更新", exact: true });
    const update = page.getByRole("button", { name: "更新页面", exact: true });
    if (!duringProbe) await editor.fill(draft);
    await status.click();
    holdProbe = duringProbe;
    await update.click();
    if (duringProbe) {
      await expect.poll(() => !!releaseProbe).toBe(true);
      await page.keyboard.press("Escape");
      await editor.fill(draft);
      releaseProbe!();
      await status.click();
    }
    await expect(page.getByRole("dialog", { name: "状态详情", exact: true }))
      .toContainText("Markdown 有未保存的修改，请先保存或放弃修改，再更新页面。");
    await expect(update).toBeEnabled();
    await expect(editor).toHaveValue(draft);
    expect(unloadDialogs, "protect edits before entering the browser's cancelable unload dialog").toBe(0);
    await page.keyboard.press("Escape");
    await file.getByRole("button", { name: "保存", exact: true }).click();
    await expect.poll(() => relay.commands.some(c => c.type === "save_markdown")).toBe(true);
    const save = relay.commands.find(c => c.type === "save_markdown")!;
    expect(save.content).toBe(draft);
    relay.emit({ type: "file_save_result", sid: "layout-parent", path, request_id: String(save.request_id),
      status: "saved", size: 64, mtime_ns: "2", revision: "b".repeat(64) });
    await expect(file.locator(".markdown-save-state")).toHaveText("已保存");
    await status.click();
    // The earlier refusal must not strand the hook's updating/pending state.
    const navigation = page.waitForEvent("framenavigated", frame => frame === page.mainFrame());
    await update.click();
    await navigation;
    expect(unloadDialogs).toBe(0);
  });
}

for (const centered of [false, true]) {
  test(`right panel layout restores ${centered ? "centered" : "docked"} file preview across engines`, async ({ page }) => {
    await page.setViewportSize({ width: 1568, height: 1000 });
    const path = "/tmp/layout/README.md";
    const relay = await mockRightPanelRelay(page, { engine: "claude", listReply: otherHarnessList,
      seedTurns: [{ id: "preview-turn", prompt: "查看文件", done: true,
        blocks: [{ kind: "text", message_id: "preview-answer", channel: "final", done: true,
          text: `[文件](${path})` }] }] });
    await page.goto("/");
    await page.getByRole("button", { name: `在 Remote 中打开 ${path}`, exact: true }).click();
    await expect.poll(() => relay.commands.some(c => c.type === "get_file_preview")).toBe(true);
    const request = relay.commands.find(c => c.type === "get_file_preview")!;
    // A response arriving while the owner is hidden must be retained, not lost
    // or displayed over the other engine's conversation.
    await page.keyboard.press("Meta+2");
    const panel = page.locator(".artifact-panel");
    await expect(panel).toHaveCount(0);
    const content = "# Preview\n\n" + Array.from({ length: 100 }, (_, n) => `Paragraph ${n}\n\n`).join("");
    relay.emit({ type: "file_preview", sid: "layout-parent", request_id: String(request.request_id),
      path, format: "markdown", content, size: content.length,
      mtime_ns: "1", revision: "a".repeat(64), writable: true });
    await page.keyboard.press("Meta+1");
    await expect(panel.getByRole("heading", { name: "Preview", exact: true })).toBeVisible();
    if (centered) await panel.getByRole("button", { name: "居中展开文件预览", exact: true }).click();
    const body = panel.locator(".artifact-body");
    await body.evaluate(node => { node.scrollTop = 700; });
    await expect.poll(() => body.evaluate(node => node.scrollTop)).toBe(700);
    await page.keyboard.press("Meta+2");
    await expect(panel).toHaveCount(0);
    await page.keyboard.press("Meta+1");
    await expect(panel).toBeVisible();
    if (centered) await expect(panel).toHaveClass(/panel-expanded/);
    else await expect(panel).not.toHaveClass(/panel-expanded/);
    await expect.poll(() => body.evaluate(node => node.scrollTop)).toBe(700);
    await panel.getByRole("button", { name: "源码", exact: true }).click();
    const editor = panel.getByRole("textbox", { name: "Markdown 源码编辑器" });
    await expect(editor).toHaveValue(content);
    await expect(editor).toBeFocused();
    await editor.evaluate(node => { node.scrollTop = 500; });
    await expect.poll(() => editor.evaluate(node => node.scrollTop)).toBe(500);
    await page.keyboard.press("Meta+2");
    await expect(panel).toHaveCount(0);
    await page.keyboard.press("Meta+1");
    await expect(editor).toHaveValue(content);
    await expect.poll(() => editor.evaluate(node => node.scrollTop)).toBe(500);
    await panel.getByRole("button", { name: "收起", exact: true }).click();
    await page.keyboard.press("Meta+2");
    await page.keyboard.press("Meta+1");
    await expect(panel).toHaveCount(0);
    expect(relay.commands.filter(c => c.type === "get_file_preview")).toHaveLength(1);
    expect(relay.commands.some(c => ["query", "steer", "open_btw", "interrupt"].includes(String(c.type)))).toBe(false);
  });
}

for (const fromBrowser of [false, true]) {
  test(`right panel layout centered file edits survive ${fromBrowser ? "directory navigation" : "standalone preview"}`, async ({ page }, info) => {
    await page.setViewportSize({ width: 1568, height: 1000 });
    const path = "/tmp/layout/README.md";
    const relay = await mockRightPanelRelay(page, { seedTurns: [{
      id: "preview-turn", prompt: "查看文件", done: true,
      blocks: [{ kind: "text", message_id: "preview-answer", channel: "final", done: true,
        text: `[文件](${path})` }],
    }] });
    await page.goto("/");
    if (fromBrowser) {
      await page.getByRole("button", { name: "更多设置", exact: true }).click();
      await page.getByRole("button", { name: "打开会话文件", exact: true }).click();
      await expect.poll(() => relay.commands.some(c => c.type === "browse_files")).toBe(true);
      const request = relay.commands.find(c => c.type === "browse_files")!;
      relay.emit({ type: "files_listed", sid: "layout-parent", request_id: String(request.request_id),
        root: "/tmp/layout", path: "/tmp/layout", parent: null, kind: "directory", revision: "dir",
        next_offset: null, entries: [{ name: "README.md", path, kind: "file" }] });
      await page.getByRole("button", { name: "居中展开会话文件", exact: true }).click();
      await page.getByRole("button", { name: "README.md", exact: true }).click();
    } else {
      await page.getByRole("button", { name: `在 Remote 中打开 ${path}`, exact: true }).click();
    }
    await expect.poll(() => relay.commands.some(c => c.type === "get_file_preview")).toBe(true);
    const request = relay.commands.find(c => c.type === "get_file_preview")!;
    relay.emit({ type: "file_preview", sid: "layout-parent", request_id: String(request.request_id),
      path, format: "markdown", content: "# Preview\n\nOriginal", size: 19,
      mtime_ns: "1", revision: "a".repeat(64), writable: true });
    const panel = page.locator(fromBrowser ? ".file-browser-shell" : ".artifact-panel");
    await panel.getByRole("button", { name: "源码", exact: true }).click();
    const editor = panel.getByRole("textbox", { name: "Markdown 源码编辑器" });
    await editor.fill("# Unsaved draft\n\nKeep this text");
    const original = await editor.elementHandle();
    if (!fromBrowser) await panel.getByRole("button", { name: "居中展开文件预览", exact: true }).click();
    await expect(page.getByRole("dialog")).toHaveCount(1);
    await expect(panel).toHaveAttribute("aria-modal", "true");
    await expectRightPanelSpace(page, false);
    expect((await panel.boundingBox())!.width).toBe(1080);
    // Layout changes must not ask to discard or recreate the dirty editor.
    let confirmations = 0;
    page.on("dialog", async dialog => { confirmations++; await dialog.dismiss(); });
    await page.keyboard.press("Escape");
    await expect(panel).not.toHaveClass(/panel-expanded/);
    await expectRightPanelSpace(page, true);
    await panel.getByRole("button", { name: "居中展开文件预览", exact: true }).click();
    await expect(panel).toHaveClass(/panel-expanded/);
    await expect(editor).toHaveValue("# Unsaved draft\n\nKeep this text");
    expect(await original!.evaluate(node => node.isConnected)).toBe(true);
    await page.locator(".panel-expanded-backdrop").click({ position: { x: 4, y: 4 } });
    await expect(panel).not.toHaveClass(/panel-expanded/);
    expect(confirmations).toBe(0);
    await panel.getByRole("button", { name: "居中展开文件预览", exact: true }).click();
    await page.screenshot({ path: info.outputPath(`centered-file-desktop-${fromBrowser}.png`), animations: "disabled" });
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(panel).toHaveClass(/panel-expanded/);
    // The visualViewport controller applies its new metrics on the next frame.
    await expect.poll(async () => {
      const bounds = (await panel.boundingBox())!;
      return bounds.y + bounds.height;
    }).toBeLessThanOrEqual(844);
    const bounds = (await panel.boundingBox())!;
    expect(bounds.width).toBe(370);
    expect(bounds.y + bounds.height).toBeLessThanOrEqual(844);
    expect(await panel.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
    const heading = panel.locator(".artifact-file-trigger .artifact-title");
    await expect.poll(async () => {
      const name = (await heading.boundingBox())!;
      const action = (await panel.getByRole("button", { name: "收起", exact: true }).boundingBox())!;
      return Math.abs(name.y + name.height / 2 - action.y - action.height / 2);
    }).toBeLessThan(4);
    await page.screenshot({ path: info.outputPath(`centered-file-${fromBrowser}.png`), animations: "disabled" });
    // Actual close/navigation still protects the dirty file.
    await panel.getByRole("button", { name: fromBrowser ? "返回目录" : "收起", exact: true }).click();
    expect(confirmations).toBe(1);
    await expect(editor).toHaveValue("# Unsaved draft\n\nKeep this text");
    if (fromBrowser) {
      await editor.fill("# Preview\n\nOriginal");
      await panel.getByRole("button", { name: "返回目录", exact: true }).click();
      await expect(panel).toHaveClass(/panel-expanded/);
      await expect(panel.getByRole("button", { name: "README.md", exact: true })).toBeVisible();
      expect(relay.commands.filter(c => c.type === "browse_files")).toHaveLength(1);
    }
    expect(relay.commands.filter(c => ["get_file_preview", "save_markdown"].includes(String(c.type)))).toHaveLength(1);
    expect(relay.commands.some(c => ["query", "steer", "open_btw", "interrupt"].includes(String(c.type)))).toBe(false);
  });
}

test("right panel layout centered diff and agent details keep their own content", async ({ page }) => {
  await page.setViewportSize({ width: 1568, height: 1000 });
  const relay = await mockRightPanelRelay(page, { visible: true, engine: "claude" });
  await page.goto("/");
  await expect(page.locator(".btw-panel")).toBeVisible();
  await page.keyboard.press("Control+Shift+b");
  const diff = page.locator(".artifact-panel");
  await expect(diff).toBeVisible();
  await diff.getByRole("button", { name: "居中展开改动", exact: true }).click();
  await expect(diff).toHaveClass(/panel-expanded/);
  await expectRightPanelSpace(page, false);
  // A non-chat modal must not redirect slash to the main composer behind it.
  const dockDiff = diff.getByRole("button", { name: "收回右侧", exact: true });
  await dockDiff.focus();
  await page.keyboard.press("/");
  await expect(dockDiff).toBeFocused();
  const original = await diff.elementHandle();
  await page.keyboard.press("Control+Shift+e");
  await expect(diff).toHaveClass(/panel-expanded/);
  await expect(page.locator(".btw-panel")).toHaveClass(/panel-expanded/);
  expect(await original!.evaluate(node => node.isConnected)).toBe(true);
  await page.keyboard.press("Control+Shift+e");
  await diff.getByRole("button", { name: "收起", exact: true }).click();
  await expect(page.locator(".btw-panel")).toBeVisible();
  await expect(page.locator(".btw-panel")).not.toHaveClass(/panel-expanded/);
  relay.emit({ type: "background_process_sync", sid: "layout-parent",
    items: [{ item_id: "center-agent", kind: "agent", status: "succeeded", title: "Centered child" }] });
  await page.getByRole("button", { name: /后台任务/ }).click();
  await page.getByRole("button", { name: "Centered child" }).click();
  const agent = page.locator(".agent-detail-panel");
  await expect.poll(() => relay.commands.some(c => c.type === "get_agent_detail")).toBe(true);
  const request = relay.commands.find(c => c.type === "get_agent_detail")!;
  relay.emit({ type: "agent_detail", sid: "layout-parent", session_id: "layout-parent",
    run_id: String(request.run_id), request_id: String(request.request_id), revision: "layout-history",
    detail_revision: "agent-history", authoritative: true, title: "Centered child", status: "succeeded",
    events: [], through_seq: 0, has_more: false });
  await agent.getByRole("button", { name: "居中展开协作代理详情", exact: true }).click();
  await expect(agent).toHaveAttribute("role", "dialog");
  await expect(agent).toContainText("已完成");
  await expectRightPanelSpace(page, false);
  await agent.getByRole("button", { name: "收回右侧", exact: true }).click();
  await expect(agent).toHaveAttribute("role", "complementary");
  await expectRightPanelSpace(page, true);
  expect(relay.commands.filter(c => c.type === "get_agent_detail")).toHaveLength(1);
  expect(relay.commands.filter(c => c.type === "get_diff")).toHaveLength(1);
  expect(relay.commands.some(c => ["query", "open_btw", "interrupt"].includes(String(c.type)))).toBe(false);
});

for (const key of ["Tab", "Escape"]) {
  test(`right panel layout HTML iframe ${key} stays in the expanded boundary`, async ({ page }) => {
    await page.setViewportSize({ width: 1568, height: 1000 });
    const path = "/tmp/layout/index.html";
    const relay = await mockRightPanelRelay(page, { seedTurns: [{
      id: "html-turn", prompt: "查看文件", done: true,
      blocks: [{ kind: "text", message_id: "html-answer", channel: "final", done: true,
        text: `[文件](${path})` }],
    }] });
    await page.goto("/");
    await page.getByRole("button", { name: `在 Remote 中打开 ${path}`, exact: true }).click();
    await expect.poll(() => relay.commands.some(c => c.type === "get_file_preview")).toBe(true);
    const request = relay.commands.find(c => c.type === "get_file_preview")!;
    relay.emit({ type: "file_preview", sid: "layout-parent", request_id: String(request.request_id),
      path, format: "html", content: '<input aria-label="Draft"><button>Last control</button>', size: 62 });
    const panel = page.locator(".artifact-panel");
    const frame = panel.frameLocator("iframe");
    await frame.getByRole("textbox").fill("keep this");
    const original = await panel.locator("iframe").elementHandle();
    await panel.getByRole("button", { name: "居中展开文件预览", exact: true }).click();
    await frame.getByRole("button", { name: "Last control" }).click();
    if (key === "Escape") {
      await frame.locator("body").evaluate(() => {
        window.addEventListener("keydown", event => event.preventDefault(), { once: true });
      });
      await page.keyboard.press("Escape");
      await expect(panel).toHaveClass(/panel-expanded/);
      await frame.locator("body").evaluate(() => window.dispatchEvent(new KeyboardEvent("keydown", {
        key: "Escape", isComposing: true, bubbles: true, cancelable: true,
      })));
      await expect(panel).toHaveClass(/panel-expanded/);
    }
    await page.keyboard.press(key);
    if (key === "Escape") await expect(panel).not.toHaveClass(/panel-expanded/);
    else {
      await expect.poll(() => panel.evaluate(node => node.contains(document.activeElement))).toBe(true);
      await expect(panel).toHaveClass(/panel-expanded/);
      await expect(panel.locator("button:visible").first()).toBeFocused();
    }
    await expect(frame.getByRole("textbox")).toHaveValue("keep this");
    expect(await original!.evaluate(node => node.isConnected)).toBe(true);
    await expect(panel.locator("iframe")).toHaveAttribute("sandbox", "allow-scripts");
  });
}

test("remote Viewer centered expansion keeps the iframe and grant alive", async ({ page, context }) => {
  await page.setViewportSize({ width: 1568, height: 1000 });
  await mockRightPanelRelay(page, { seedTurns: [{
    id: "viewer-center", prompt: "预览", done: true,
    blocks: [{ kind: "text", message_id: "viewer-center-answer", channel: "final", done: true,
      text: `[结构预览](${VIEWER_LINK_SITE.urls[0]})` }],
  }] });
  let opens = 0;
  let closes = 0;
  const id = "a".repeat(32);
  let associated = false;
  await page.route("**/api/viewers", route => route.fulfill({ json: { enabled: true, sites: [VIEWER_LINK_SITE] } }));
  await page.route("**/api/viewers/pages", route => {
    const body = route.request().postDataJSON();
    if (body.action === "associate") associated = true;
    return route.fulfill({ json: { pages: associated ? [{
      id: "center-page", machine_id: VIEWER_LINK_SITE.machine_id,
      site_id: VIEWER_LINK_SITE.id, entry: VIEWER_LINK_SITE.entry, label: VIEWER_LINK_SITE.label,
      references: [], turn_ids: [], available: true,
    }] : [] } });
  });
  await page.route("**/api/viewers/open", route => {
    opens++;
    return route.fulfill({ json: { id, mode: "isolated", origin: "https://center-preview.example",
      expires_at: Date.now() / 1000 + 3600 } });
  });
  await page.route(`**/api/viewers/${id}`, route => {
    if (route.request().method() === "DELETE") closes++;
    return route.fulfill({ json: { expires_at: Date.now() / 1000 + 3600 } });
  });
  await context.route("https://center-preview.example/**", route => route.fulfill({
    contentType: "text/html", body: '<input aria-label="Preview state" value="original">',
  }));
  await page.goto("/");
  await page.getByRole("link", { name: "结构预览", exact: true }).click();
  const panel = page.locator(".remote-viewer-panel");
  await expect(panel.locator("iframe")).toBeVisible();
  const original = await panel.locator("iframe").elementHandle();
  const input = panel.frameLocator("iframe").getByRole("textbox", { name: "Preview state" });
  await input.fill("keep iframe state");
  await panel.getByRole("button", { name: "居中展开远程预览", exact: true }).click();
  await expect(panel).toHaveClass(/panel-expanded/);
  await expectRightPanelSpace(page, false);
  await page.keyboard.press("Escape");
  await expect(panel).not.toHaveClass(/panel-expanded/);
  await expect(panel).toBeVisible(); // Escape must dock, not run the Viewer's close handler too.
  await panel.getByRole("button", { name: "居中展开远程预览", exact: true }).click();
  await expect(panel).toHaveClass(/panel-expanded/);
  await expect(input).toHaveValue("keep iframe state");
  expect(await original!.evaluate(node => node.isConnected)).toBe(true);
  expect(opens).toBe(1);
  expect(closes).toBe(0);
});

test("side chat scope follows its parent through navigation and refresh without creating forks", async ({ page }) => {
  await page.setViewportSize({ width: 1568, height: 881 });
  const relay = await mockRightPanelRelay(page, { visible: true, secondParent: true });
  await page.goto("/");
  await expect(page.locator(".btw-panel")).toBeVisible();
  await page.locator(".btw-panel textarea").fill("只属于原会话的草稿");
  await page.keyboard.press("Control+b");
  await page.getByText("Second parent", { exact: true }).click();
  await expect(page.getByText("session layout-o", { exact: true })).toBeVisible();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await expectRightPanelSpace(page, false);
  relay.emit({ type: "user_msg", sid: "btw-layout-child", msg_id: "bg-scoped",
    prompt: "原会话的侧聊仍在运行", seq: 1 });
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await page.getByText("Layout parent", { exact: true }).click();
  await expect(page.locator(".btw-panel")).toContainText("原会话的侧聊仍在运行");
  await expect(page.locator(".btw-panel textarea")).toHaveValue("只属于原会话的草稿");
  await page.getByText("Second parent", { exact: true }).click();
  await page.reload();
  // The cold catalog chooses its newest parent, not the last browser focus.
  // Restored visibility must still belong only to the explicitly opened one.
  await expect(page.getByText("session layout-p", { exact: true })).toBeVisible();
  await expect(page.locator(".btw-panel")).toBeVisible();
  await page.keyboard.press("Control+b");
  await page.getByText("Second parent", { exact: true }).click();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await page.getByText("Layout parent", { exact: true }).click();
  await expect(page.locator(".btw-panel")).toBeVisible();
  expect(relay.commands.filter((command) =>
    ["open_btw", "close_btw", "query", "interrupt"].includes(String(command.type))))
    .toEqual([]);
});

test("side chat scope keeps late fork creation and sibling visibility with the originating parent", async ({ page }) => {
  await page.setViewportSize({ width: 1568, height: 881 });
  const relay = await mockRightPanelRelay(page, {
    visible: true, retained: false, secondParent: true,
  });
  await page.goto("/");
  await page.getByRole("button", { name: "新建侧边对话", exact: true }).click();
  await expect.poll(() => relay.commands.filter((c) => c.type === "open_btw").length).toBe(1);
  const open = relay.commands.find((c) => c.type === "open_btw")!;
  await page.keyboard.press("Control+b");
  await page.getByText("Second parent", { exact: true }).click();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  relay.emit({ type: "btw_opened", sid: "btw-layout-child",
    btw_sid: "btw-layout-child", parent_sid: "layout-parent", engine: "codex",
    created_at: 1, revision: 2, request_id: String(open.request_id) });
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await page.keyboard.press("Control+Shift+k");
  await expect.poll(() => relay.commands.filter((c) => c.type === "open_btw").length).toBe(2);
  const secondOpen = relay.commands.filter((c) => c.type === "open_btw")[1];
  expect(secondOpen.sid).toBe("layout-other");
  await page.getByRole("button", { name: "收起侧边对话", exact: true }).click();
  await page.getByText("Layout parent", { exact: true }).click();
  await expect(page.locator(".btw-chat-tab")).toHaveCount(1);
  relay.emit({ type: "btw_opened", sid: "btw-other-child",
    btw_sid: "btw-other-child", parent_sid: "layout-other", engine: "codex",
    created_at: 2, revision: 3, request_id: String(secondOpen.request_id) });
  await expect(page.locator(".btw-chat-tab")).toHaveCount(1);
  await page.getByText("Second parent", { exact: true }).click();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await page.keyboard.press("Control+Shift+k");
  await expect(page.locator(".btw-chat-tab")).toHaveCount(1);
  expect(relay.commands.filter((c) => c.type === "open_btw")).toHaveLength(2);
  expect(relay.commands.filter((c) => c.type === "close_btw")).toHaveLength(0);
});

test("right panel layout avoids repeated paragraph reflow while side chat opens and closes", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1160, height: 881 });
  await mockRightPanelRelay(page, { seedTurns: [{
    id: "layout-reflow", prompt: "查看长段落", done: true,
    blocks: [{ kind: "text", message_id: "layout-reflow-answer", channel: "final", done: true,
      text: Array.from({ length: 14 }, (_, i) => `${i + 1}. ${"打开侧边聊天时，正文应稳定地显示在分配好的区域里，保留当前阅读位置。".repeat(5)}`).join("\n\n") }],
  }] });
  await page.goto("/");
  const thread = page.locator(".pane .thread");
  await expect(thread.locator(".prose")).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(500);
  for (const open of [true, false, true]) {
    const frames = await page.evaluate(async () => {
      const thread = document.querySelector<HTMLElement>(".pane .thread")!;
      const prose = thread.querySelector<HTMLElement>(".prose")!;
      const frames: { width: number; height: number; top: number; scroll: number }[] = [];
      const record = () => {
        const rect = prose.getBoundingClientRect();
        frames.push({ width: Math.round(rect.width), height: Math.round(rect.height),
          top: Math.round(rect.top), scroll: thread.scrollTop });
      };
      record();
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "K", ctrlKey: true, shiftKey: true, bubbles: true }));
      const start = performance.now();
      await new Promise<void>(resolve => {
        const frame = () => {
          record();
          if (performance.now() - start >= 650) resolve();
          else requestAnimationFrame(frame);
        };
        requestAnimationFrame(frame);
      });
      return frames;
    });
    await testInfo.attach(`side-chat-${open ? "open" : "close"}-${Date.now()}.json`, {
      body: JSON.stringify(frames), contentType: "application/json",
    });
    await expectRightPanelSpace(page, open);
    if (open) {
      expect(await page.locator(".btw-panel").evaluate(node => node.getBoundingClientRect().width)).toBe(520);
    }
    // Text gets one final layout; only the floating panel animates into place.
    expect(new Set(frames.map(frame => frame.width)).size).toBeLessThanOrEqual(2);
    expect(new Set(frames.map(frame => frame.height)).size).toBeLessThanOrEqual(2);
  }
  // A deliberate resize remains responsive and survives hide/show and reload.
  const handle = page.getByRole("button", { name: "调整 BTW 面板宽度", exact: true });
  await handle.focus();
  await handle.press("ArrowLeft");
  await expectRightPanelSpace(page, true, true, 544);
  expect(await page.locator(".btw-panel").evaluate(node => node.getBoundingClientRect().width)).toBe(544);
  await page.keyboard.press("Control+Shift+k");
  await expectRightPanelSpace(page, false);
  await page.keyboard.press("Control+Shift+k");
  expect(await page.locator(".btw-panel").evaluate(node => node.getBoundingClientRect().width)).toBe(544);
  await page.reload();
  await expectRightPanelSpace(page, true, true, 544);
  expect(await page.locator(".btw-panel").evaluate(node => node.getBoundingClientRect().width)).toBe(544);
});

test("right panel layout releases collapsed and reload-restored side chat space", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1568, height: 881 });
  const relay = await mockRightPanelRelay(page);
  await page.goto("/");
  await expect(page.getByText("session layout-p", { exact: true })).toBeVisible();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await expectRightPanelSpace(page, false);

  await page.keyboard.press("Control+Shift+k");
  await expect(page.locator(".btw-panel")).toBeVisible();
  await expectRightPanelSpace(page, true);
  await expect(page.locator(".btw-chat-state.running")).toHaveCount(1);
  await page.getByRole("button", { name: "收起侧边对话", exact: true }).click();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await expectRightPanelSpace(page, false);

  // Background frames remain routed while hidden; expanding is not a new fork.
  relay.emit({ type: "user_msg", sid: "btw-layout-child",
    msg_id: "background-message", prompt: "侧聊仍在后台运行", seq: 1 });
  await page.keyboard.press("Control+Shift+k");
  await expect(page.locator(".btw-panel")).toContainText("侧聊仍在后台运行");
  await expectRightPanelSpace(page, true);
  await page.keyboard.press("Control+Shift+k");
  await expectRightPanelSpace(page, false);

  await page.reload();
  await expect(page.getByText("session layout-p", { exact: true })).toBeVisible();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await expectRightPanelSpace(page, false);
  await page.keyboard.press("Control+Shift+k");
  await expect(page.locator(".btw-chat-tab")).toHaveCount(1);
  await expectRightPanelSpace(page, true);
  await page.reload();
  await expect(page.locator(".btw-panel")).toBeVisible();
  await expectRightPanelSpace(page, true);
  await page.keyboard.press("Control+b");
  await page.getByRole("button", { name: "新会话", exact: true }).click();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await expectRightPanelSpace(page, false);
  await page.getByText("Layout parent", { exact: true }).click();
  await expect(page.locator(".btw-chat-tab")).toHaveCount(1);
  await expectRightPanelSpace(page, true);
  expect(relay.commands.filter((command) =>
    ["open_btw", "close_btw", "query", "interrupt"].includes(String(command.type))))
    .toEqual([]);
});

test("right panel layout allocates empty and opening side chats only while visible", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1568, height: 881 });
  const relay = await mockRightPanelRelay(page, { visible: true, retained: false });
  await page.goto("/");
  await expect(page.locator(".btw-panel")).toContainText("暂无侧边对话");
  await expectRightPanelSpace(page, true);
  await page.getByRole("button", { name: "新建侧边对话", exact: true }).click();
  await expect(page.locator(".btw-panel")).toContainText("正在打开侧边对话");
  await expectRightPanelSpace(page, true);
  await page.getByRole("button", { name: "收起侧边对话", exact: true }).click();
  await expectRightPanelSpace(page, false);
  const open = relay.commands.find((command) => command.type === "open_btw")!;
  // A late fork response must retain the chat without reopening the slot.
  relay.emit({ type: "btw_opened", sid: "btw-layout-child",
    btw_sid: "btw-layout-child", parent_sid: "layout-parent", engine: "codex",
    created_at: 1, revision: 2, request_id: String(open.request_id) });
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await expectRightPanelSpace(page, false);
  await page.keyboard.press("Control+Shift+k");
  await expect(page.locator(".btw-chat-tab")).toHaveCount(1);
  await expectRightPanelSpace(page, true);
});

test("right panel layout shares the diff slot and keeps mobile as an overlay", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1568, height: 881 });
  await mockRightPanelRelay(page, { visible: true });
  await page.goto("/");
  await expect(page.locator(".btw-panel")).toBeVisible();
  await expectRightPanelSpace(page, true);
  await page.keyboard.press("Control+Shift+b");
  await expect(page.locator(".artifact-panel")).toBeVisible();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await expectRightPanelSpace(page, true);
  await page.locator(".artifact-panel").getByRole("tab", { name: "btw" }).click();
  await expect(page.locator(".btw-panel")).toBeVisible();
  await expectRightPanelSpace(page, true);
  // Collapse BTW with an artifact still available: the diff becomes visible.
  await page.getByRole("button", { name: "收起侧边对话", exact: true }).click();
  await expect(page.locator(".artifact-panel")).toBeVisible();
  await expectRightPanelSpace(page, true);
  await page.locator(".artifact-panel").getByRole("button", { name: "收起", exact: true }).click();
  await expectRightPanelSpace(page, false);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.keyboard.press("Control+Shift+k");
  await expect(page.locator(".btw-panel")).toBeVisible();
  await expectRightPanelSpace(page, true, false);
  await page.getByRole("button", { name: "收起侧边对话", exact: true }).click();
  await expectRightPanelSpace(page, false, false);
  await page.setViewportSize({ width: 1568, height: 881 });
  await expectRightPanelSpace(page, false);
});

test("right panel layout preserves Claude agent detail priority over side chats", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1568, height: 881 });
  const relay = await mockRightPanelRelay(page, { visible: true, engine: "claude" });
  await page.goto("/");
  await expect(page.locator(".btw-panel")).toBeVisible();
  relay.emit({ type: "background_process_sync", sid: "layout-parent",
    items: [{ item_id: "layout-agent", kind: "agent", status: "running",
      title: "Layout child agent" }] });
  const agentCard = page.getByRole("button", { name: "Layout child agent" });
  await page.getByRole("button", { name: /后台任务，\d+ 项进行中/ }).click();
  await agentCard.click();
  await expect(page.locator(".agent-detail-panel")).toBeVisible();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await expectRightPanelSpace(page, true);
  await page.getByRole("button", { name: "关闭协作代理详情", exact: true }).click();
  await expect(page.locator(".btw-panel")).toBeVisible();
  await expectRightPanelSpace(page, true);
  await page.getByRole("button", { name: "收起侧边对话", exact: true }).click();
  await expectRightPanelSpace(page, false);
  await page.getByRole("button", { name: /后台任务，\d+ 项进行中/ }).click();
  await agentCard.click();
  await expect(page.locator(".agent-detail-panel")).toBeVisible();
  await expectRightPanelSpace(page, true);
  await page.getByRole("button", { name: "关闭协作代理详情", exact: true }).click();
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await expectRightPanelSpace(page, false);
  expect(relay.commands.some((command) => command.type === "close_btw"))
    .toBe(false);
});

async function coverBtwPanel(
  page: import("@playwright/test").Page,
  relay: Awaited<ReturnType<typeof mockRightPanelRelay>>,
  cover: "agent" | "diff",
) {
  if (cover === "agent") {
    relay.emit({ type: "background_process_sync", sid: "layout-parent",
      items: [{ item_id: "layout-agent", kind: "agent", status: "running",
        title: "Layout child agent" }] });
    await page.getByRole("button", { name: /后台任务，\d+ 项进行中/ }).click();
    await page.getByRole("button", { name: "Layout child agent" }).click();
    await expect(page.locator(".agent-detail-panel")).toBeVisible();
  } else {
    await page.keyboard.press("Control+Shift+b");
    await expect(page.locator(".artifact-panel")).toBeVisible();
  }
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  return cover === "agent"
    ? page.getByRole("button", { name: "关闭协作代理详情", exact: true })
    : page.locator(".artifact-panel").getByRole("button", { name: "收起", exact: true });
}

test("native question centers long Claude prompts and retains answers above the keyboard", async ({ page }, testInfo) => {
  const relay = await mockRightPanelRelay(page, { engine: "claude", parentState: "running", seedTurns: [{
    id: "read-before-answer", prompt: "先解释方案，再让我确认。", done: true,
    blocks: [{ kind: "text", message_id: "proposal", channel: "final", done: true,
      text: "请先核对原来的指标定义，再决定是否采用以下方案。\n\n".repeat(50) }],
  }] });
  await page.goto("/");
  await expect(page.locator(".composer textarea")).toBeEnabled();
  const paragraph = "写飞书前对照现有文档，发现 v0.8 端侧决策的指标和文档里已有的定义冲突。\n\n"
    + "文档「指标说明」里的定义是：误动作率 = 不应执行却执行，或方向、动作执行错误的比例。按这个定义，端侧接手后做错的指令也算误动作。\n\n";
  relay.emit({ type: "ask_user", sid: "layout-parent", seq: 1, ask_id: "long-ask",
    question: paragraph.repeat(7), options: [{ label: "修正指标" }, { label: "补充说明" }],
    allow_text: true, multi_select: true });
  const dialog = page.getByRole("dialog", { name: "操作确认" });
  await expect(dialog).toBeVisible();
  await expect(dialog.locator(".question-dialog-title")).toHaveText("Claude 想确认一下");
  await expect(dialog.locator(".engine-brand-icon.claude use")).toHaveAttribute("href", /#claude$/);
  expect(await dialog.evaluate(node => node.parentElement === document.body)).toBe(true);
  const box = (await dialog.boundingBox())!;
  const viewport = page.viewportSize()!;
  expect(Math.abs(box.x + box.width / 2 - viewport.width / 2)).toBeLessThan(2);
  expect(Math.abs(box.y + box.height / 2 - viewport.height / 2)).toBeLessThan(2);
  expect(box.y).toBeGreaterThan(viewport.height * .1);
  expect(viewport.height - box.y - box.height).toBeGreaterThan(viewport.height * .1);
  const body = dialog.locator(".sheet-scroll");
  expect(await body.evaluate(node => node.scrollHeight > node.clientHeight)).toBe(true);
  await expect(dialog.getByPlaceholder("输入回答")).not.toBeFocused();
  await page.screenshot({ path: testInfo.outputPath("claude-question-centered-light.png") });
  await page.evaluate(() => document.documentElement.dataset.theme = "dark");
  await page.screenshot({ path: testInfo.outputPath("claude-question-centered-dark.png") });
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "修正指标", exact: true }).click();
  await expect(dialog.getByRole("button", { name: "修正指标", exact: true })).toHaveAttribute("aria-pressed", "true");
  const input = dialog.getByPlaceholder("输入回答");
  await input.fill("按原定义修正并补充说明");
  const scrollBeforeCollapse = await body.evaluate(node => node.scrollTop);
  const beforeCollapse = relay.commands.length;
  await dialog.getByRole("button", { name: "收起问题", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  const resume = page.getByRole("button", { name: "等待你回答 继续回答" });
  await expect(resume).toBeVisible();
  await expect(resume).toBeFocused();
  const resumeBox = (await resume.boundingBox())!;
  const composerBox = (await page.locator(".composer").boundingBox())!;
  expect(resumeBox.y + resumeBox.height).toBeLessThan(composerBox.y);
  expect(resumeBox.height).toBeLessThanOrEqual(48);
  await page.locator(".composer textarea").click(); // The document is no longer inert.
  const thread = page.locator(".thread");
  const threadScroll = await thread.evaluate(node => node.scrollTop);
  const threadBox = (await thread.boundingBox())!;
  await page.mouse.move(threadBox.x + threadBox.width / 2, threadBox.y + threadBox.height / 2);
  if (testInfo.project.use.isMobile) {
    // Playwright cannot synthesize a wheel in mobile WebKit. The real pointer
    // click above verifies inert is released; verify the reading viewport too.
    await thread.evaluate(node => node.scrollBy({ top: -400 }));
  } else {
    await page.mouse.wheel(0, -400);
  }
  await expect.poll(() => thread.evaluate(node => node.scrollTop)).toBeLessThan(threadScroll - 50);
  await page.screenshot({ path: testInfo.outputPath("claude-question-collapsed-dark.png") });
  // Collapsing, reading and reopening are entirely local; no answer/cancel/turn command.
  expect(relay.commands.slice(beforeCollapse).filter(command => command.type !== "ping")).toEqual([]);
  await resume.click();
  await expect(dialog).toBeVisible();
  await expect(input).toHaveValue("按原定义修正并补充说明");
  await expect(dialog.getByRole("button", { name: "修正指标", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect.poll(() => body.evaluate(node => node.scrollTop)).toBe(scrollBeforeCollapse);
  await input.click();
  await page.evaluate(() => {
    Object.defineProperties(window.visualViewport!, {
      height: { configurable: true, value: 340 },
      offsetTop: { configurable: true, value: 40 },
    });
    window.visualViewport!.dispatchEvent(new Event("resize"));
  });
  await expect.poll(async () => {
    const rect = await dialog.boundingBox();
    return !!rect && rect.y >= 40 && rect.y + rect.height <= 380
      && Math.abs(rect.y + rect.height / 2 - 210) < 2;
  }).toBe(true);
  await expect(input).toBeFocused();
  await expect(input).toHaveValue("按原定义修正并补充说明");
  await expect(input).toBeInViewport({ ratio: 1 });
  await page.screenshot({ path: testInfo.outputPath("claude-question-keyboard.png") });
  await dialog.locator(".qa-text-answer").getByRole("button", { name: "确定", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => relay.commands.filter(command => command.type === "answer_question")
    .map(({ sid, ask_id, answer }) => ({ sid, ask_id, answer }))).toEqual([
      { sid: "layout-parent", ask_id: "long-ask", answer: ["修正指标", "按原定义修正并补充说明"] },
    ]);
});

test("native question batch keeps choices across pages and reconnects until atomic submission", async ({ page }, testInfo) => {
  const relay = await mockRightPanelRelay(page, { engine: "claude", listReply: otherHarnessList });
  await page.goto("/");
  await expect(page.locator(".composer textarea")).toBeEnabled();
  const questions = [
    { question: "文件列表按什么顺序展示？", header: "展示顺序", allow_text: true,
      options: [{ label: "最近更新优先", ds: "新上传或新生成的文件排在前面" }, { label: "按文件类型分组" }] },
    { question: "哪些文件放进 Artifacts？", header: "收录范围", multi_select: true, allow_text: true,
      options: [{ label: "上传的附件" }, { label: "AI 生成的文档" }, { label: "AI 生成的图片" }] },
    { question: "补充要求？", header: "其他", allow_text: true,
      options: [{ label: "保持现状" }, { label: "调整显示" }] },
  ];
  const event = { type: "ask_user", sid: "layout-parent", ask_id: "batch-ask", ...questions[0], questions };
  relay.emit({ ...event, seq: 1 });
  const dialog = page.getByRole("dialog", { name: "操作确认" });
  await expect(dialog.locator(".qa-progress")).toHaveText("1 / 3");
  await expect(dialog.getByRole("button", { name: "上一题" })).toBeDisabled();
  await expect(dialog.getByRole("button", { name: "下一题" })).toBeDisabled();
  const choice = dialog.getByRole("button", { name: "最近更新优先" });
  await choice.click();
  await expect(choice).toHaveAttribute("aria-pressed", "true");
  const customAnswer = dialog.getByRole("textbox", { name: "输入回答" });
  // Traversing the form is not a decision to replace a valid answer.
  await dialog.getByRole("button", { name: "按文件类型分组", exact: true }).focus();
  await page.keyboard.press("Tab");
  await expect(customAnswer).toBeFocused();
  await expect(choice).toHaveAttribute("aria-pressed", "true");
  await expect(dialog.getByRole("button", { name: "下一题" })).toBeEnabled();
  await dialog.getByRole("button", { name: "下一题" }).focus();
  await page.keyboard.press("Shift+Tab");
  await expect(customAnswer).toBeFocused();
  await expect(choice).toHaveAttribute("aria-pressed", "true");
  await expect(choice).toHaveCSS("background-color", "rgb(242, 231, 223)");
  expect(await choice.locator("svg, input, .checkbox, .radio").count()).toBe(0);
  const radius = await dialog.locator(".qa-next").evaluate(node => parseFloat(getComputedStyle(node).borderRadius));
  expect(radius).toBeGreaterThanOrEqual(18);
  const box = (await dialog.boundingBox())!;
  expect(Math.abs(box.y + box.height / 2 - page.viewportSize()!.height / 2)).toBeLessThan(2);
  await page.screenshot({ path: testInfo.outputPath("question-batch-single.png") });
  await dialog.getByRole("button", { name: "下一题" }).click();
  await expect(dialog.locator(".qa-progress")).toHaveText("2 / 3");
  await dialog.getByRole("button", { name: "上传的附件", exact: true }).click();
  await dialog.getByRole("button", { name: "AI 生成的文档", exact: true }).click();
  await dialog.getByRole("textbox", { name: "输入回答" }).fill("包含音频");
  await dialog.getByRole("button", { name: "上一题" }).click();
  await expect(choice).toHaveAttribute("aria-pressed", "true");
  await customAnswer.focus();
  await expect(choice).toHaveAttribute("aria-pressed", "true");
  // Keyboard edits still select the custom-answer mode without a pointer click.
  await customAnswer.fill("键盘输入的答案");
  await expect(choice).toHaveAttribute("aria-pressed", "false");
  await expect(dialog.getByRole("button", { name: "下一题" })).toBeEnabled();
  await customAnswer.fill("");
  await expect(dialog.getByRole("button", { name: "下一题" })).toBeDisabled();
  await choice.click();
  await customAnswer.click();
  await expect(choice).toHaveAttribute("aria-pressed", "false");
  await expect(dialog.getByRole("button", { name: "下一题" })).toBeDisabled();
  await customAnswer.fill("临时自填答案");
  await choice.click();
  await expect(customAnswer).toHaveValue("");
  await expect(choice).toHaveAttribute("aria-pressed", "true");
  await customAnswer.click();
  await expect(choice).toHaveAttribute("aria-pressed", "false");
  await expect(dialog.getByRole("button", { name: "下一题" })).toBeDisabled();
  await customAnswer.fill("按名称排序");
  await expect(choice).toHaveAttribute("aria-pressed", "false");
  await dialog.getByRole("button", { name: "下一题" }).click();
  await expect(dialog.getByRole("button", { name: "上传的附件", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(dialog.getByRole("textbox", { name: "输入回答" })).toHaveValue("包含音频");
  await page.evaluate(() => document.documentElement.dataset.theme = "dark");
  await expect(dialog.getByRole("button", { name: "上传的附件", exact: true }))
    .toHaveCSS("background-color", "rgb(53, 37, 32)");
  await page.screenshot({ path: testInfo.outputPath("question-batch-multi-dark.png") });
  expect(relay.commands.filter(command => command.type === "answer_question")).toHaveLength(0);
  await dialog.getByRole("button", { name: "收起问题", exact: true }).click();
  const resume = page.getByRole("button", { name: "等待你回答 2 / 3 继续回答" });
  await expect(resume).toBeVisible();
  await expect(dialog).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("question-batch-collapsed-dark.png") });
  await page.keyboard.press("Meta+2");
  await expect(page.getByRole("button", { name: "切换新会话引擎" })).toContainText("Codex");
  await expect(dialog).toHaveCount(0);
  await expect(resume).toHaveCount(0);
  await page.keyboard.press("Meta+1");
  await expect(resume).toBeVisible();
  await expect(dialog).toHaveCount(0);
  await resume.click();
  await expect(dialog.locator(".qa-progress")).toHaveText("2 / 3");
  await expect(dialog.getByRole("button", { name: "上传的附件", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(dialog.getByRole("textbox", { name: "输入回答" })).toHaveValue("包含音频");
  await dialog.getByRole("button", { name: "收起问题", exact: true }).click();
  relay.emit({ type: "ask_user_sync", sid: "layout-parent" });
  await expect(dialog).toHaveCount(0);
  await expect(resume).toHaveCount(0);
  relay.emit({ ...event, seq: 2 });
  await expect(resume).toBeVisible();
  await expect(dialog).toHaveCount(0);
  await resume.click();
  await expect(dialog.locator(".qa-progress")).toHaveText("2 / 3");
  await expect(dialog.getByRole("textbox", { name: "输入回答" })).toHaveValue("包含音频");
  await dialog.getByRole("button", { name: "下一题" }).click();
  await dialog.getByRole("button", { name: "保持现状", exact: true }).click();
  await page.screenshot({ path: testInfo.outputPath("question-batch-final.png") });
  await dialog.getByRole("button", { name: "提交回答", exact: true }).click();
  await expect.poll(() => relay.commands.filter(command => command.type === "answer_question")
    .map(({ sid, ask_id, answer, answers }) => ({ sid, ask_id, answer, answers }))).toEqual([
      { sid: "layout-parent", ask_id: "batch-ask", answer: undefined,
        answers: ["按名称排序", ["上传的附件", "AI 生成的文档", "包含音频"], "保持现状"] },
    ]);
  await expect(dialog).toBeVisible(); // Sending is not acknowledgement.
  await expect(dialog.getByRole("button", { name: "提交中…", exact: true })).toBeDisabled();
  await dialog.getByRole("button", { name: "收起问题", exact: true }).click();
  const pendingResume = page.getByRole("button", { name: "回答提交中… 3 / 3 查看回答" });
  await expect(pendingResume).toBeVisible();
  await pendingResume.click();
  await expect(dialog.getByRole("button", { name: "提交中…", exact: true })).toBeDisabled();
  expect(relay.commands.filter(command => command.type === "answer_question")).toHaveLength(1);
  await dialog.getByRole("button", { name: "收起问题", exact: true }).click();
  relay.emit({ type: "ask_user_closed", sid: "layout-parent", seq: 3, ask_id: "batch-ask", reason: "answered" });
  await expect(dialog).toHaveCount(0);
  await expect(pendingResume).toHaveCount(0);
});

test("async question tool approval survives idle focus after each page refresh", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { engine: "claude", retained: false });
  await page.goto("/");
  const dialog = page.getByRole("dialog", { name: "操作确认" });
  let commandCursor = 0;
  for (let reload = 0; reload < 2; reload++) {
    const seq = reload * 3 + 1;
    if (reload) await page.reload();
    await expect.poll(() => relay.commands.slice(commandCursor).some(command =>
      command.type === "get_history" && command.session_id === "layout-parent")).toBe(true);
    // Hello restores a still-pending background tool while its parent is idle.
    relay.emit({ type: "ask_user_sync", sid: "layout-parent" });
    relay.emit({ type: "ask_user", sid: "layout-parent", ask_id: "pending-tool",
      question: "Claude 请求使用工具：Bash", options: [{ label: "允许一次" }, { label: "拒绝" }] });
    await expect(dialog).toBeVisible();
    // Selecting that resident session publishes its current idle state again.
    relay.emit({ type: "session_focus", sid: "layout-parent", session_id: "layout-parent" });
    relay.emit({ type: "state", sid: "layout-parent", state: "idle", seq });
    await expect(dialog.getByRole("button", { name: "允许一次", exact: true })).toBeVisible();
    relay.emit({ type: "turn_end", sid: "layout-parent", turn_id: "finished-parent", seq: seq + 1,
      result: { subtype: "success", duration_ms: 1000, is_error: false } });
    await expect(dialog).toBeVisible();
    expect(relay.commands.filter(command => command.type === "answer_question")).toHaveLength(0);
    relay.emit({ type: "ask_user_closed", sid: "layout-parent", ask_id: "pending-tool",
      reason: "cancelled", seq: seq + 2 });
    await expect(dialog).toHaveCount(0);
    commandCursor = relay.commands.length;
  }
});

test("native question batch scrolls long text with fixed navigation and routes side chat answers", async ({ page }, testInfo) => {
  const relay = await mockRightPanelRelay(page, { engine: "claude", visible: true });
  await page.goto("/");
  await expect(page.locator(".btw-panel")).toBeVisible();
  const questions = [{ question: "请确认这组要求。\n".repeat(60), header: "需求", allow_text: true,
    options: [{ label: "同意" }, { label: "需要调整" }] }];
  relay.emit({ type: "ask_user", sid: "btw-layout-child", seq: 1, ask_id: "batch-child", ...questions[0], questions });
  const dialog = page.getByRole("dialog", { name: "操作确认" });
  await expect(dialog).toBeVisible();
  await expect(dialog.locator(".qa-progress")).toHaveCount(0);
  expect(await dialog.locator(".sheet-scroll").evaluate(node => node.scrollHeight > node.clientHeight)).toBe(true);
  const footer = (await dialog.locator(".qa-footer").boundingBox())!;
  const input = dialog.getByRole("textbox", { name: "输入回答" });
  await input.fill("补充验收标准");
  expect((await dialog.locator(".qa-footer").boundingBox())!.y).toBe(footer.y);
  const body = dialog.locator(".sheet-scroll");
  const scrollBeforeCollapse = await body.evaluate(node => node.scrollTop);
  const beforeCollapse = relay.commands.length;
  await dialog.getByRole("button", { name: "收起问题", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  const resume = page.locator(".btw-panel").getByRole("button", { name: "等待你回答 继续回答" });
  await expect(resume).toBeVisible();
  const barBox = (await resume.boundingBox())!;
  const composerBox = (await page.locator(".btw-composer").boundingBox())!;
  expect(barBox.y + barBox.height).toBeLessThan(composerBox.y);
  await page.locator(".btw-composer textarea").click();
  await page.screenshot({ path: testInfo.outputPath("question-batch-side-collapsed.png") });
  expect(relay.commands.slice(beforeCollapse).filter(command => command.type !== "ping")).toEqual([]);
  await resume.click();
  await expect(input).toHaveValue("补充验收标准");
  await expect.poll(() => body.evaluate(node => node.scrollTop)).toBe(scrollBeforeCollapse);
  await input.click();
  await page.evaluate(() => {
    Object.defineProperties(window.visualViewport!, {
      height: { configurable: true, value: 340 }, offsetTop: { configurable: true, value: 40 },
    });
    window.visualViewport!.dispatchEvent(new Event("resize"));
  });
  await expect.poll(async () => {
    const rect = await dialog.boundingBox();
    return !!rect && rect.y >= 40 && rect.y + rect.height <= 380;
  }).toBe(true);
  await expect(input).toBeInViewport({ ratio: 1 });
  await page.screenshot({ path: testInfo.outputPath("question-batch-keyboard.png") });
  await input.dispatchEvent("compositionstart");
  await input.press("Enter");
  expect(relay.commands.filter(command => command.type === "answer_question")).toHaveLength(0);
  await input.dispatchEvent("compositionend");
  await dialog.getByRole("button", { name: "提交回答", exact: true }).click();
  await expect.poll(() => relay.commands.filter(command => command.type === "answer_question")
    .map(({ sid, answers }) => ({ sid, answers }))).toEqual([
      { sid: "btw-layout-child", answers: ["补充验收标准"] },
    ]);
  relay.emit({ type: "ask_user_closed", sid: "btw-layout-child", seq: 2, ask_id: "batch-child", reason: "answered" });
  await expect(dialog).toHaveCount(0);
});

test("native question collapse retires only the exact server question and new asks still open", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { engine: "claude", parentState: "running" });
  await page.goto("/");
  await expect(page.locator(".composer textarea")).toBeEnabled();
  const ask = { type: "ask_user" as const, sid: "layout-parent", question: "是否继续？",
    options: [{ label: "继续" }, { label: "调整方案" }] };
  relay.emit({ ...ask, seq: 1, ask_id: "old-ask" });
  const dialog = page.getByRole("dialog", { name: "操作确认" });
  await dialog.getByRole("button", { name: "收起问题", exact: true }).click();
  const resume = page.getByRole("button", { name: "等待你回答 继续回答" });
  await expect(resume).toBeVisible();
  relay.emit({ ...ask, seq: 2, ask_id: "new-ask" });
  await expect(dialog).toBeVisible();
  await expect(resume).toHaveCount(0);
  relay.emit({ type: "ask_user_closed", sid: "layout-parent", seq: 3,
    ask_id: "old-ask", reason: "superseded" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "收起问题", exact: true }).click();
  await expect(resume).toBeVisible();
  relay.emit({ type: "ask_user_closed", sid: "layout-parent", seq: 4,
    ask_id: "new-ask", reason: "timeout" });
  await expect(resume).toHaveCount(0);
  await expect(dialog).toHaveCount(0);
  expect(relay.commands.filter(command => ["answer_question", "interrupt", "query"].includes(String(command.type)))).toEqual([]);
});

test("native question keeps custom headings and text-only Codex answers", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { engine: "codex" });
  await page.goto("/");
  await expect(page.locator(".composer textarea")).toBeEnabled();
  relay.emit({ type: "ask_user", sid: "layout-parent", seq: 1, ask_id: "text-ask",
    header: "部署目标", question: "请选择目标环境。", options: [], allow_text: true, secret: true });
  const dialog = page.getByRole("dialog", { name: "操作确认" });
  await expect(dialog).toBeVisible();
  await expect(dialog.locator(".question-dialog-title")).toHaveText("Codex 想确认一下");
  await expect(dialog.locator(".engine-brand-icon.codex use")).toHaveAttribute("href", /#codex$/);
  await expect(dialog.locator(".qa-header")).toHaveText("部署目标");
  const input = dialog.getByPlaceholder("输入敏感内容");
  await expect(input).toHaveAttribute("type", "password");
  await expect(input).toBeFocused();
  await input.fill("staging");
  await input.press("Enter");
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => relay.commands.filter(command => command.type === "answer_question")
    .map(({ sid, ask_id, answer }) => ({ sid, ask_id, answer }))).toEqual([
      { sid: "layout-parent", ask_id: "text-ask", answer: "staging" },
    ]);
});

for (const cover of ["agent", "diff"] as const) {
  test(`right panel visibility keeps parent questions answerable behind ${cover}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1568, height: 881 });
    const relay = await mockRightPanelRelay(page, {
      visible: true, engine: cover === "agent" ? "claude" : "codex",
    });
    await page.goto("/");
    await expect(page.locator(".btw-panel")).toBeVisible();
    const closeCover = await coverBtwPanel(page, relay, cover);

    // Emit the parent's question last: seeing it proves the hidden side-chat
    // question has arrived, rather than passing before its frame is processed.
    relay.emit({ type: "ask_user", sid: "btw-layout-child", seq: 1,
      ask_id: "side-ask", question: "侧聊等待确认",
      options: [{ label: "确认侧聊" }] });
    relay.emit({ type: "ask_user", sid: "layout-parent", seq: 1,
      ask_id: "parent-ask", question: "主会话等待确认",
      options: [{ label: "确认主会话" }] });
    await expect(page.getByText("主会话等待确认", { exact: true })).toBeVisible();
    await expect(page.getByRole("dialog", { name: "操作确认" })).toHaveCount(1);
    await expect(page.getByText("侧聊等待确认", { exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "确认主会话", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "操作确认" })).toHaveCount(0);

    // Revealing the side chat must preserve its pending ask and its routing.
    await closeCover.click();
    await expect(page.locator(".btw-panel")).toBeVisible();
    await expect(page.getByText("侧聊等待确认", { exact: true })).toBeVisible();
    await expect(page.getByRole("dialog", { name: "操作确认" })).toHaveCount(1);
    await page.getByRole("button", { name: "确认侧聊", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "操作确认" })).toHaveCount(0);
    await expect.poll(() => relay.commands
      .filter((command) => command.type === "answer_question")
      .map(({ sid, ask_id, answer }) => ({ sid, ask_id, answer })))
      .toEqual([
        { sid: "layout-parent", ask_id: "parent-ask", answer: "确认主会话" },
        { sid: "btw-layout-child", ask_id: "side-ask", answer: "确认侧聊" },
      ]);
  });

  test(`right panel visibility retains unseen side-chat completion behind ${cover}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1568, height: 881 });
    const relay = await mockRightPanelRelay(page, {
      visible: true, engine: cover === "agent" ? "claude" : "codex",
    });
    await page.goto("/");
    await expect(page.locator(".btw-panel")).toBeVisible();
    const closeCover = await coverBtwPanel(page, relay, cover);
    relay.emit({ type: "user_msg", sid: "btw-layout-child", seq: 1,
      msg_id: "hidden-work", prompt: "后台侧聊任务" });
    relay.emit({ type: "turn_end", sid: "btw-layout-child", seq: 2,
      result: { subtype: "success", duration_ms: 1000, is_error: false } });
    relay.emit({ type: "ask_user", sid: "layout-parent", seq: 1,
      ask_id: "completion-marker", question: "完成事件已经送达",
      options: [{ label: "继续查看" }] });
    await expect(page.getByText("完成事件已经送达", { exact: true })).toBeVisible();
    const badge = page.getByText("BTW 完成", { exact: true });
    await expect(badge).toHaveCount(1);

    // Returning to this browser tab must not acknowledge a covered side chat.
    await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
    await expect(badge).toHaveCount(1);
    await page.getByRole("button", { name: "继续查看", exact: true }).click();
    await closeCover.click();
    await expect(page.locator(".btw-panel")).toBeVisible();
    await expect(badge).toHaveCount(0);

    // Conversely, finishing in the actually visible side chat is already seen.
    relay.emit({ type: "user_msg", sid: "btw-layout-child", seq: 3,
      msg_id: "visible-work", prompt: "前台侧聊任务" });
    relay.emit({ type: "turn_end", sid: "btw-layout-child", seq: 4,
      result: { subtype: "success", duration_ms: 1000, is_error: false } });
    relay.emit({ type: "ask_user", sid: "layout-parent", seq: 2,
      ask_id: "visible-completion-marker", question: "前台完成事件已经送达",
      options: [{ label: "确认完成" }] });
    await expect(page.getByText("前台完成事件已经送达", { exact: true })).toBeVisible();
    await expect(badge).toHaveCount(0);
    expect(relay.commands.some((command) => command.type === "close_btw"))
      .toBe(false);
  });
}

for (const parentState of ["running", "interrupting"] as const) {
  test(`right panel layout retains completed side chat badge while parent is ${parentState}`, async ({
    page,
  }, testInfo) => {
    const mobile = isMobileWebKitProject(testInfo.project.name);
    if (!mobile) await page.setViewportSize({ width: 1568, height: 881 });
    const relay = await mockRightPanelRelay(page, { visible: true });
    await page.goto("/");
    await expect(page.locator(".btw-panel")).toBeVisible();
    relay.emit({ type: "state", sid: "layout-parent", state: parentState });
    await page.getByRole("button", { name: "收起侧边对话", exact: true }).click();
    await expect(page.locator(".btw-panel")).toHaveCount(0);
    const parent = page.locator(".scard").filter({ hasText: "Layout parent" });
    await expect(parent).toBeAttached();
    if (!await page.locator(".sessions.show").count()) await page.keyboard.press("Control+b");
    await expect(page.locator(".sessions")).toBeInViewport({ ratio: 1 });
    await expect(parent).toBeInViewport();
    await expect(parent.getByText(parentState === "running" ? "运行" : "中断", {
      exact: true,
    })).toBeVisible();

    relay.emit({ type: "user_msg", sid: "btw-layout-child", seq: 1,
      msg_id: "hidden-work", prompt: "后台侧聊任务" });
    relay.emit({ type: "turn_end", sid: "btw-layout-child", seq: 2,
      result: { subtype: "success", duration_ms: 1000, is_error: false } });
    relay.emit({ type: "state", sid: "btw-layout-child", seq: 3, state: "idle" });
    const badge = parent.getByText("BTW 完成", { exact: true });
    await expect(badge).toBeVisible();
    await expect(badge).toBeInViewport({ ratio: 1 });
    const geometry = await parent.evaluate((card) => {
      const badge = card.querySelector(".pill.completed")!.getBoundingClientRect();
      const menu = card.querySelector(".scard-actions")!.getBoundingClientRect();
      const title = card.querySelector(".scard-title")!.getBoundingClientRect();
      return {
        titleWidth: title.width,
        badgeUnderMenu: badge.left < menu.right && badge.right > menu.left
          && badge.top < menu.bottom && badge.bottom > menu.top,
      };
    });
    expect(geometry.titleWidth).toBeGreaterThan(0);
    expect(geometry.badgeUnderMenu).toBe(false);
    await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
    await expect(badge).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("btw-completed-parent-busy.png") });

    if (mobile) await page.keyboard.press("Control+b");
    await page.keyboard.press("Control+Shift+k");
    await expect(page.locator(".btw-panel")).toBeVisible();
    await expect(badge).toHaveCount(0);
    await expect(parent.locator(`.pill.${parentState}`)).toHaveCount(1);
    expect(relay.commands.some((command) =>
      ["open_btw", "close_btw", "query", "steer", "interrupt"].includes(String(command.type))))
      .toBe(false);
  });
}

test("right panel visibility acknowledges only the selected side chat", async ({ page }) => {
  await page.setViewportSize({ width: 1568, height: 881 });
  const relay = await mockRightPanelRelay(page, { visible: true });
  await page.goto("/");
  await expect(page.locator(".btw-panel")).toBeVisible();
  relay.emit({ type: "btw_sync", generation: "layout-generation", revision: 2,
    sessions: [
      { btw_sid: "btw-layout-child", parent_sid: "layout-parent",
        engine: "codex", created_at: 1, state: "running" },
      { btw_sid: "btw-other-child", parent_sid: "layout-parent",
        engine: "codex", created_at: 2, state: "running" },
    ] });
  await expect(page.locator(".btw-chat-tab")).toHaveCount(2);
  relay.emit({ type: "user_msg", sid: "btw-other-child", seq: 1,
    msg_id: "other-work", prompt: "另一个侧聊任务" });
  relay.emit({ type: "turn_end", sid: "btw-other-child", seq: 2,
    result: { subtype: "success", duration_ms: 1000, is_error: false } });
  relay.emit({ type: "ask_user", sid: "layout-parent", seq: 1,
    ask_id: "other-completion-marker", question: "另一个侧聊已经完成",
    options: [{ label: "继续查看" }] });
  await expect(page.getByText("另一个侧聊已经完成", { exact: true })).toBeVisible();
  const badge = page.getByText("BTW 完成", { exact: true });
  await expect(badge).toHaveCount(1);
  await expect(page.getByRole("tab", { name: "侧聊 1", exact: true }))
    .toHaveAttribute("aria-selected", "true");
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await page.getByRole("button", { name: "继续查看", exact: true }).click();
  await expect(badge).toHaveCount(1);
  await page.getByRole("tab", { name: "另一个侧聊任务", exact: true }).click();
  await expect(badge).toHaveCount(0);
  await expect(page.getByText("session layout-p", { exact: true })).toBeVisible();
  expect(relay.commands.some((command) =>
    ["open_btw", "close_btw", "query", "interrupt"].includes(String(command.type))))
    .toBe(false);
});

test("mounted message image retries once when cache capacity is released", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?inline-image-capacity=1");

  await expect(page.getByTestId("inline-load-attempts")).toHaveText("1");
  await expect(page.getByTestId("inline-network-loads")).toHaveText("0");
  await expect(page.locator(".message-image-error"))
    .toContainText("图片暂时无法加载");

  await page.getByTestId("release-inline-capacity").click();

  await expect(page.getByTestId("inline-load-attempts")).toHaveText("2");
  await expect(page.getByTestId("inline-network-loads")).toHaveText("1");
  await expect(page.locator(".message-image-loading")).toBeVisible();
  await page.waitForTimeout(150);
  await expect(page.getByTestId("inline-load-attempts")).toHaveText("2");
  await expect(page.getByTestId("inline-network-loads")).toHaveText("1");
});

test("two visible images do not reclaim one cache slot forever", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?inline-image-eviction=1");

  await expect(page.getByTestId("visible-inline-attempts")).toHaveText("3");
  await expect(page.getByTestId("visible-inline-network-loads")).toHaveText("2");
  const visibleImages = page.getByTestId("two-visible-inline-images");
  await expect(visibleImages.getByRole("button", {
    name: "图片暂时无法加载，点击重试",
  })).toBeVisible();
  await expect(visibleImages.getByRole("img", { name: "B" })).toBeAttached();

  await page.waitForTimeout(250);
  await expect(page.getByTestId("visible-inline-attempts")).toHaveText("3");
  await expect(page.getByTestId("visible-inline-network-loads")).toHaveText("2");

  await visibleImages.getByRole("button", {
    name: "图片暂时无法加载，点击重试",
  }).click();
  await expect(page.getByTestId("visible-inline-attempts")).toHaveText("4");
  await expect(page.getByTestId("visible-inline-network-loads")).toHaveText("3");
  await expect(visibleImages.getByRole("img", { name: "A" })).toBeAttached();
  await page.waitForTimeout(250);
  await expect(page.getByTestId("visible-inline-attempts")).toHaveText("4");
  await expect(page.getByTestId("visible-inline-network-loads")).toHaveText("3");

  const noLoader = page.getByTestId("inline-no-loader");
  await expect(noLoader.getByText("图片加载失败", { exact: true })).toBeVisible();
  await expect(noLoader.getByText("图片加载超时", { exact: true })).toBeVisible();
  await expect(noLoader.locator("button.message-image-error")).toHaveCount(0);
});

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

async function gotoWithProductionCsp(
  page: import("@playwright/test").Page,
  path: string,
): Promise<void> {
  await page.route("**/tests/history-browser.html*", async (route) => {
    const response = await route.fetch();
    const body = await response.text();
    const inlineModule = body.match(
      /<script type="module">([\s\S]*?)<\/script>/,
    )?.[1];
    const policy = inlineModule
      ? productionCsp.replace(
        "script-src 'self'",
        `script-src 'self' 'sha256-${createHash("sha256")
          .update(inlineModule)
          .digest("base64")}'`,
      )
      : productionCsp;
    await route.fulfill({
      response,
      body,
      headers: {
        ...response.headers(),
        "content-security-policy": policy,
      },
    });
  });
  await page.goto(path);
}

test("HTML preview filename menu preserves the frame and closes before the expanded panel", async ({ page }, info) => {
  await page.setViewportSize({ width: 320, height: 740 });
  await gotoWithProductionCsp(page, "/tests/history-browser.html?artifact-html=1");
  const panel = page.locator(".artifact-panel");
  const header = panel.locator(".artifact-preview-head");
  const trigger = header.getByRole("button", { name: "文件信息：preview.html", exact: true });
  const menu = panel.getByRole("dialog", { name: "文件信息", exact: true });
  const frame = page.frameLocator('iframe[title="HTML 交互预览"]');
  await expect(frame.getByRole("button", { name: "计数 0", exact: true })).toBeVisible();
  const original = (await panel.locator("iframe").elementHandle())!;
  await header.getByRole("button", { name: "居中展开文件预览", exact: true }).click();
  await expect(panel).toHaveClass(/panel-expanded/);
  const nav = (await header.locator(".artifact-preview-navigation").boundingBox())!;
  const toolbar = (await header.locator(".artifact-preview-toolbar").boundingBox())!;
  expect(toolbar.y).toBeGreaterThanOrEqual(nav.y + nav.height);
  expect(await header.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
  await expect(header.getByRole("button", { name: "刷新文件" })).toHaveCount(0);
  await expect(header.locator(".artifact-path")).toHaveCount(0);
  await trigger.click();
  await expect(menu).toContainText("preview.html");
  await expect(menu).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await expect(panel).toHaveClass(/panel-expanded/);
  await trigger.press("Enter");
  await expect(menu).toBeVisible();
  await header.getByRole("button", { name: "预览", exact: true }).click();
  await expect(menu).toHaveCount(0);
  await trigger.click();
  const frameBounds = (await panel.locator("iframe").boundingBox())!;
  // Click an uncovered part of the sandboxed frame, below the open popover.
  await page.mouse.click(frameBounds.x + frameBounds.width / 2, frameBounds.y + frameBounds.height - 20);
  await expect(menu).toHaveCount(0);
  await frame.getByRole("button", { name: "计数 0", exact: true }).click();
  await expect(frame.getByRole("button", { name: "计数 1", exact: true })).toBeVisible();
  expect(await original.evaluate(node => node.isConnected)).toBe(true);
  await header.screenshot({ path: info.outputPath("html-preview-header.png") });
});

test("HTML preview opens interactive by default and stays isolated under production CSP", async ({
  page,
}) => {
  const outbound: string[] = [];
  await page.route("https://cc-remote-preview-test.invalid/**", (route) => {
    outbound.push(route.request().url());
    return route.fulfill({ body: "unexpected network request" });
  });
  await gotoWithProductionCsp(page, "/tests/history-browser.html?artifact-html=1");

  const previewGeometry = async () => page.locator(
    ".artifact-html-stage",
  ).evaluate((stage) => {
    const body = stage.parentElement;
    const frame = stage.querySelector("iframe");
    return {
      bodyWidth: body?.getBoundingClientRect().width ?? 0,
      stageWidth: stage.getBoundingClientRect().width,
      frameWidth: frame?.getBoundingClientRect().width ?? 0,
    };
  });
  const expectPreviewFillsBody = async () => {
    await expect.poll(async () => {
      const geometry = await previewGeometry();
      return geometry.bodyWidth > 0
        && Math.abs(geometry.stageWidth - geometry.bodyWidth) <= 1
        && Math.abs(geometry.frameWidth - geometry.bodyWidth) <= 1;
    }).toBe(true);
  };

  const interactiveFrame = page.frameLocator('iframe[title="HTML 交互预览"]');
  await expectPreviewFillsBody();
  await expect(interactiveFrame.locator("#head-style")).toHaveCSS(
    "color",
    "rgb(12, 34, 56)",
  );
  await expect(interactiveFrame.locator("#visualization-theme")).toHaveCSS(
    "color",
    "rgb(236, 237, 243)",
  );
  await expect(interactiveFrame.locator("#visualization-theme")).toHaveCSS(
    "background-color",
    "rgb(13, 14, 21)",
  );
  await expect(interactiveFrame.locator("#visualization-theme")).toHaveCSS(
    "border-top-color",
    "rgb(49, 50, 63)",
  );
  await expect(page.getByRole("button", { name: /运行交互预览|停止交互预览/ })).toHaveCount(0);
  await expectPreviewFillsBody();
  await expect(interactiveFrame.locator("body")).toHaveAttribute(
    "data-script-ran",
    "yes",
  );
  await expect(interactiveFrame.locator("body")).toHaveAttribute(
    "data-parent-blocked",
    "yes",
  );
  await expect(page.locator("body")).not.toHaveAttribute(
    "data-preview-escaped",
    "yes",
  );
  await expect(interactiveFrame.locator("body")).toHaveAttribute("data-network-blocked", "yes");
  await expect(interactiveFrame.locator("body")).toHaveAttribute("data-storage-blocked", "yes");
  await interactiveFrame.getByRole("button", { name: "计数 0", exact: true }).click();
  await expect(interactiveFrame.getByRole("button", { name: "计数 1", exact: true })).toBeVisible();
  expect(outbound).toHaveLength(0);

  await page.getByRole("button", { name: "源码", exact: true }).click();
  await page.getByRole("button", { name: "预览", exact: true }).click();
  const warmInteractiveFrame = page.frameLocator(
    'iframe[title="HTML 交互预览"]',
  );
  await expect(warmInteractiveFrame.locator("body")).toHaveAttribute(
    "data-script-ran",
    "yes",
  );
});

test("Codex visualize output opens its local HTML through the preview callback", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?codex-visualization=1");
  const card = page.getByRole("button", { name: /结构原理草图/ });
  await expect(card).toBeVisible();
  await expect(card).toContainText("HTML 可视化");
  await expect(page.locator("main")).not.toContainText("/tmp/private");
  await card.click();
  await expect(page.getByTestId("visualization-opened-path")).toHaveText(
    "/tmp/private/concept.html",
  );
});

test("Codex file citations stay inline and open PDF or GIF artifacts", async ({
  page,
}) => {
  const disclosureLoads: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/src/markdown-extras.ts")) disclosureLoads.push(request.url());
  });
  await gotoWithProductionCsp(
    page,
    "/tests/history-browser.html?codex-file-citation=1",
  );
  const pdf = page.getByRole("button", { name: /final report\.pdf/ });
  const gif = page.getByRole("button", { name: /demo} final\.gif/ });
  await expect(pdf).toBeVisible();
  await expect(gif).toBeVisible();
  await expect(page.getByTestId("valid-citations"))
    .not.toContainText("codex-file-citation");
  await expect(page.getByTestId("invalid-citation"))
    .toContainText("codex-file-citation");
  await expect(page.getByTestId("invalid-citation")).toContainText("invalid tail");
  await pdf.click();
  await expect(page.getByTestId("citation-opened-path")).toHaveText(
    "/tmp/reports/final report.pdf",
  );
  await gif.click();
  await expect(page.getByTestId("citation-opened-path")).toHaveText(
    "/tmp/demo} final.gif",
  );
  expect(disclosureLoads).toEqual([]);
});

for (const fixture of ["artifact-svg", "artifact-markdown-svg"] as const) {
  test(`${fixture} sanitizes SVG before creating a blob URL`, async ({
    page,
  }) => {
    await page.goto(`/tests/history-browser.html?${fixture}=1`);
    const image = page.getByRole("img", { name: fixture === "artifact-svg"
      ? "diagram.svg" : "diagram" });
    await expect(image).toBeVisible();
    const sanitized = await image.evaluate(async (node) => {
      const src = (node as HTMLImageElement).src;
      return {
        src,
        text: await (await fetch(src)).text(),
      };
    });
    expect(sanitized.src).toMatch(/^blob:/);
    expect(sanitized.text).toContain("safe-svg-rect");
    expect(sanitized.text).not.toMatch(
      /<script|foreignObject|example\.com|<image/i,
    );
    const parsed = await page.evaluate((svg) => {
      const document = new DOMParser().parseFromString(svg, "image/svg+xml");
      return {
        error: !!document.querySelector("parsererror"),
        text: document.querySelector("text")?.textContent,
      };
    }, sanitized.text);
    expect(parsed).toEqual({ error: false, text: "A\u00a0B & <C>" });
    await applyProductionCsp(page);
    await expect(image).toBeVisible();
    await expect.poll(() => image.evaluate((node) =>
      (node as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
  });

  test(`${fixture} renders an optional operator SVG without external requests`, async ({
    page,
  }, testInfo) => {
    const path = process.env.CC_REMOTE_SVG_TEST_FILE;
    test.skip(!path, "No operator SVG sample selected");
    const bytes = readFileSync(path!);
    const externalRequests: string[] = [];
    await page.route(/^https?:\/\/(?!127\.0\.0\.1(?::|\/))/, (route) => {
      externalRequests.push(route.request().url());
      return route.abort();
    });
    await page.addInitScript((data) => {
      (window as unknown as { svgFixture: string }).svgFixture = data;
    }, bytes.toString("base64"));
    await gotoWithProductionCsp(page, `/tests/history-browser.html?${fixture}=1`);
    const image = page.getByRole("img", { name: fixture === "artifact-svg"
      ? "diagram.svg" : "diagram" });
    await expect(image).toBeVisible();
    await expect.poll(() => image.evaluate((node) => {
      const img = node as HTMLImageElement;
      return img.complete && img.naturalWidth > 0 && img.naturalHeight > 0;
    })).toBe(true);
    await expect(page.locator(".preview-error")).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath("operator-svg-preview.png") });
    expect(externalRequests).toEqual([]);
  });
}

test("artifact-audio plays and seeks WAV under production CSP on desktop and mobile", async ({ page }, testInfo) => {
  await gotoWithProductionCsp(page, "/tests/history-browser.html?artifact-audio=1");
  const audio = page.getByLabel("reference.wav 播放器");
  await expect(audio).toBeVisible();
  await expect(audio).toHaveAttribute("controls", "");
  await expect(audio).not.toHaveAttribute("autoplay");
  await expect.poll(() => audio.evaluate((node) => (node as HTMLAudioElement).duration)).toBe(3);
  expect(await audio.evaluate((node) => (node as HTMLAudioElement).paused)).toBe(true);
  const box = await audio.boundingBox();
  await audio.click({ position: { x: 24, y: box!.height / 2 } });
  await expect.poll(() => audio.evaluate((node) => (node as HTMLAudioElement).currentTime)).toBeGreaterThan(0.1);
  await audio.evaluate((node) => { const media = node as HTMLAudioElement; media.pause(); media.currentTime = 1.5; });
  await expect.poll(() => audio.evaluate((node) => (node as HTMLAudioElement).currentTime)).toBe(1.5);
  await page.getByLabel("播放速度").selectOption("1.5");
  expect(await audio.evaluate((node) => (node as HTMLAudioElement).playbackRate)).toBe(1.5);
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("link", { name: "下载音频" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("reference.wav");
  const bytes = readFileSync((await download.path())!);
  expect(bytes.length).toBe(48044);
  expect(bytes.subarray(8, 12).toString()).toBe("WAVE");
  expect(await page.locator(".artifact-audio-card").evaluate((node) => node.scrollWidth - node.clientWidth)).toBeLessThan(2);
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("audio-preview.png") });
});

test("artifact-audio stops and releases its source when refreshed or closed", async ({ page }) => {
  await page.addInitScript(() => {
    const revoke = URL.revokeObjectURL.bind(URL);
    (window as unknown as { revokedAudioUrls: string[] }).revokedAudioUrls = [];
    URL.revokeObjectURL = (url) => {
      (window as unknown as { revokedAudioUrls: string[] }).revokedAudioUrls.push(url);
      revoke(url);
    };
  });
  await gotoWithProductionCsp(page, "/tests/history-browser.html?artifact-audio=1");
  const audio = page.locator("audio");
  await expect.poll(() => audio.evaluate((node) => (node as HTMLAudioElement).duration)).toBe(3);
  const old = (await audio.elementHandle())!;
  const oldUrl = await old.getAttribute("src");
  const box = await audio.boundingBox();
  await audio.click({ position: { x: 24, y: box!.height / 2 } });
  await expect.poll(() => old.evaluate((node) => (node as HTMLAudioElement).paused)).toBe(false);
  await page.locator(".artifact-file-trigger").click();
  await page.getByRole("button", { name: "重新读取", exact: true }).click();
  await expect(page.getByLabel("second.wav 播放器")).toBeVisible();
  expect(await old.evaluate((node) => (node as HTMLAudioElement).paused)).toBe(true);
  expect(await old.getAttribute("src")).toBeNull();
  await expect(audio).toHaveAttribute("src", /^blob:/);
  const next = (await audio.elementHandle())!;
  const nextUrl = await next.getAttribute("src");
  expect(nextUrl).not.toBe(oldUrl);
  await page.getByRole("button", { name: "收起", exact: true }).click();
  await expect(page.getByTestId("audio-preview-closed")).toBeVisible();
  expect(await next.evaluate((node) => (node as HTMLAudioElement).paused)).toBe(true);
  expect(await next.getAttribute("src")).toBeNull();
  const revoked = await page.evaluate(() => (window as unknown as { revokedAudioUrls: string[] }).revokedAudioUrls);
  expect(revoked).toContain(oldUrl);
  expect(revoked).toContain(nextUrl);
});

test("artifact-audio explains decoder errors and recovers on the next file", async ({ page }) => {
  await gotoWithProductionCsp(page, "/tests/history-browser.html?artifact-audio=invalid");
  await expect(page.getByRole("alert")).toContainText("无法播放该音频编码");
  await expect(page.getByRole("link", { name: "下载音频" })).toBeVisible();
  await page.locator(".artifact-file-trigger").click();
  await page.getByRole("button", { name: "重新读取", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect.poll(() => page.locator("audio").evaluate((node) => (node as HTMLAudioElement).duration)).toBe(3);
});

test("artifact-audio decodes an optional operator WAV sample without conversion", async ({ page }, testInfo) => {
  const path = process.env.CC_REMOTE_AUDIO_TEST_FILE;
  test.skip(!path, "No operator audio sample selected");
  const bytes = readFileSync(path!);
  await page.addInitScript((data) => {
    (window as unknown as { audioFixture: { data: string; name: string } }).audioFixture = {
      data, name: "operator-sample.wav",
    };
  }, bytes.toString("base64"));
  await gotoWithProductionCsp(page, "/tests/history-browser.html?artifact-audio=1");
  const audio = page.locator("audio");
  await expect.poll(() => audio.evaluate((node) => (node as HTMLAudioElement).duration)).toBeGreaterThan(0);
  await expect(page.getByRole("alert")).toHaveCount(0);
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("link", { name: "下载音频" }).click();
  const downloaded = await downloadPromise;
  expect(readFileSync((await downloaded.path())!)).toEqual(bytes);
  await page.screenshot({ path: testInfo.outputPath("operator-audio-preview.png") });
});

test("artifact-pdf renders paged PDF content under the production CSP", async ({
  page,
}) => {
  await gotoWithProductionCsp(page, "/tests/history-browser.html?artifact-pdf=1");

  const canvas = page.locator(".artifact-pdf-page canvas");
  await expect(page.locator(".artifact-pdf-controls")).toContainText("1 / 2 页");
  await expect(canvas).toBeVisible();
  await expect.poll(() => canvas.evaluate((node) => {
    const target = node as HTMLCanvasElement;
    const context = target.getContext("2d");
    if (!context || target.width === 0 || target.height === 0) return false;
    const pixel = context.getImageData(
      Math.floor(target.width / 2),
      Math.floor(target.height / 2),
      1,
      1,
    ).data;
    return pixel[2] > pixel[0] && pixel[3] === 255;
  })).toBe(true);
  await expect(page.getByRole("button", { name: "下一页" })).toBeEnabled();

  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.locator(".artifact-pdf-controls")).toContainText("2 / 2 页");
  await expect.poll(() => canvas.evaluate((node) => {
    const target = node as HTMLCanvasElement;
    const context = target.getContext("2d");
    if (!context || target.width === 0 || target.height === 0) return false;
    const pixel = context.getImageData(
      Math.floor(target.width / 2),
      Math.floor(target.height / 2),
      1,
      1,
    ).data;
    return pixel[0] > pixel[2] && pixel[3] === 255;
  })).toBe(true);
  await expect(page.locator(".artifact-pdf-stage iframe")).toHaveCount(0);
  await expect(page.locator(".artifact-pdf-stage .preview-error")).toHaveCount(0);
});

test("artifact-gif preserves native animation under the production CSP", async ({
  page,
}) => {
  await gotoWithProductionCsp(page, "/tests/history-browser.html?artifact-gif=1");
  const image = page.getByRole("img", { name: "animation.gif" });
  await expect(image).toBeVisible();
  await expect.poll(() => image.evaluate((node) => ({
    width: (node as HTMLImageElement).naturalWidth,
    height: (node as HTMLImageElement).naturalHeight,
  }))).toEqual({ width: 16, height: 16 });

  const frames = new Set<string>();
  for (let index = 0; index < 8; index += 1) {
    const screenshot = await image.screenshot({ animations: "allow" });
    frames.add(createHash("sha256").update(screenshot).digest("hex"));
    await page.waitForTimeout(70);
  }
  expect(frames.size).toBeGreaterThan(1);
});

test("artifact-invalid-gif reports a decode error instead of a broken image", async ({
  page,
}) => {
  await gotoWithProductionCsp(
    page,
    "/tests/history-browser.html?artifact-invalid-gif=1",
  );
  await expect(page.locator(".preview-error")).toContainText(
    "图片无法解码或格式不受支持",
  );
  await expect(page.getByRole("img", { name: "animation.gif" })).toHaveCount(0);
});

test("Markdown HTML preview renders README layout without executing active HTML", async ({
  page,
}, testInfo) => {
  const requested: string[] = [];
  page.on("request", (request) => {
    if (/preview\.example|header\.svg|local-logo\.png|\/private\/local\.png/.test(request.url())) {
      requested.push(request.url());
    }
  });
  await page.route("https://preview.example/**", (route) => route.abort());
  await gotoWithProductionCsp(page, "/tests/history-browser.html?artifact-markdown-html=1");
  const preview = page.locator(".markdown-preview");
  const heading = preview.getByRole("heading", { name: "Microduck", exact: true });
  await expect(heading).toBeVisible();
  await expect(heading).toHaveCSS("text-align", "center");
  await expect(preview).not.toContainText('<p align="center">');
  const hero = preview.getByRole("img", { name: "Robot overview" });
  await expect(hero).toBeVisible();
  await expect(hero).toHaveAttribute("width", "820");
  await expect(hero).toHaveAttribute("height", "320");
  await expect.poll(() => hero.evaluate((node) =>
    (node as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
  const geometry = await hero.evaluate((node) => {
    const image = node.getBoundingClientRect();
    const container = node.closest(".markdown-preview")!.getBoundingClientRect();
    return { image: image.width, container: container.width };
  });
  expect(geometry.image).toBeLessThanOrEqual(geometry.container + 1);
  await preview.screenshot({ path: testInfo.outputPath("markdown-html-preview.png") });

  const local = preview.getByRole("img", { name: "Local logo" });
  await expect(local).toHaveAttribute("src", /^data:image\/png;base64,/);
  await expect(local).toHaveAttribute("width", "64");
  await expect.poll(() => local.evaluate((node) =>
    (node as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
  await preview.getByRole("button", { name: "在 Remote 中打开 README.md", exact: true }).click();
  await expect(page.getByTestId("artifact-opened-file")).toHaveText("README.md:");
  await preview.getByRole("button", { name: "在 Remote 中打开 docs/install.md:12", exact: true }).click();
  await expect(page.getByTestId("artifact-opened-file")).toHaveText("docs/install.md:12");
  await expect(preview.getByRole("link", { name: "官方项目" })).toHaveAttribute("target", "_blank");
  await preview.locator("summary").click();
  await expect(preview.locator("details")).toHaveAttribute("open", "");
  await expect(preview.locator("details strong")).toHaveText("Markdown");
  await preview.getByRole("link", { name: "跳转到安装" }).click();
  await expect(page).toHaveURL(/#cc-preview-installation$/);
  await expect(preview.locator("#cc-preview-installation")).toBeVisible();

  await expect(preview.locator("script,style,iframe,object,embed,form,source,math,svg:not(.message-link-icon svg)")).toHaveCount(0);
  // Screenshot capture can restore an empty style attribute on checkboxes.
  await expect(preview.locator('[style]:not([style=""]),[onclick],[onerror],[srcset]')).toHaveCount(0);
  await expect(page.locator("body")).not.toHaveAttribute("data-md-unsafe", /.+/);
  await expect(preview.getByRole("checkbox")).toBeChecked();
  await expect(preview.getByRole("checkbox")).toBeDisabled();
  expect(requested).toEqual([]);
  await page.getByRole("button", { name: "源码", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Markdown 源码编辑器" }))
    .toHaveValue(MARKDOWN_HTML_LOCAL_README);
  await page.getByRole("button", { name: "预览", exact: true }).click();
  await expect(heading).toBeVisible();
  await expect(page.locator("body")).not.toHaveAttribute("data-md-unsafe", /.+/);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => hero.evaluate((node) => {
    const bounds = node.getBoundingClientRect();
    return bounds.width > 0 && bounds.right <= window.innerWidth + 1;
  })).toBe(true);
});

test("Markdown HTML preview allows GitHub images without opening other network capabilities", async ({
  page,
}) => {
  const requested: { url: string; referer?: string }[] = [];
  // Fulfill every external request locally. The CSP, not a mock abort, must
  // prevent unlisted images, remote scripts, and fetches from reaching here.
  await page.route(/^https?:\/\/(?!127\.0\.0\.1(?::|\/))/, (route) => {
    const request = route.request();
    const url = request.url();
    requested.push({ url, referer: request.headers().referer });
    if (request.resourceType() === "script") {
      return route.fulfill({ contentType: "application/javascript",
        body: 'document.body.dataset.unexpectedGithubScript = "yes";' });
    }
    return route.fulfill({ contentType: "image/svg+xml", body: MARKDOWN_HTML_HEADER_SVG });
  });
  await gotoWithProductionCsp(page, "/tests/history-browser.html?artifact-markdown-github-html=1");
  const hero = page.getByRole("img", { name: "Robot overview" });
  await expect(hero).toHaveAttribute("src", GITHUB_README_ATTACHMENT_URL);
  await expect(hero).toHaveAttribute("referrerpolicy", "no-referrer");
  await expect.poll(() => hero.evaluate((node) =>
    (node as HTMLImageElement).naturalWidth)).toBe(820);
  expect(requested).toEqual([
    { url: GITHUB_README_ATTACHMENT_URL, referer: undefined },
  ]);

  // Exercise the attachment destination separately: Playwright's WebKit
  // route.fulfill cannot synthesize 3xx responses. Both real redirect origins
  // must be admitted by the production policy, not only github.com.
  const allowed = [
    GITHUB_README_IMAGE_URL,
    "https://raw.githubusercontent.com/test/readme.svg",
    "https://user-images.githubusercontent.com/test/readme.svg",
    "https://private-user-images.githubusercontent.com/test/readme.svg",
    "https://camo.githubusercontent.com/test/readme.svg",
    "https://avatars.githubusercontent.com/test/readme.svg",
  ];
  const blocked = [
    "https://preview.example/image.svg",
    "https://raw.githubusercontent.com.evil.example/image.svg",
    "https://github.com/not-an-attachment/image.svg",
    "https://other-bucket.s3.amazonaws.com/image.svg",
    "http://raw.githubusercontent.com/test/insecure.svg",
  ];
  const imageResults = await page.evaluate(async ({ allowed, blocked }) => {
    return Promise.all([...allowed, ...blocked].map((src) => new Promise<boolean>((resolve) => {
      const image = document.createElement("img");
      image.referrerPolicy = "no-referrer";
      image.onload = () => { image.remove(); resolve(true); };
      image.onerror = () => { image.remove(); resolve(false); };
      image.src = src;
      document.body.append(image);
    })));
  }, { allowed, blocked });
  expect(imageResults).toEqual([...allowed.map(() => true), ...blocked.map(() => false)]);
  const remoteScript = "https://raw.githubusercontent.com/test/blocked.js";
  const remoteFetch = "https://raw.githubusercontent.com/test/blocked.json";
  const otherCapabilities = await page.evaluate(async ({ remoteScript, remoteFetch }) => {
    const scriptLoaded = new Promise<boolean>((resolve) => {
      const script = document.createElement("script");
      script.onload = () => { script.remove(); resolve(true); };
      script.onerror = () => { script.remove(); resolve(false); };
      script.src = remoteScript;
      document.head.append(script);
    });
    const fetched = fetch(remoteFetch).then(() => true, () => false);
    return Promise.all([scriptLoaded, fetched]);
  }, { remoteScript, remoteFetch });
  expect(otherCapabilities).toEqual([false, false]);
  await expect(page.locator("body")).not.toHaveAttribute("data-unexpected-github-script", /.+/);
  expect(requested.map(({ url }) => url).sort()).toEqual([
    GITHUB_README_ATTACHMENT_URL, ...allowed,
  ].sort());
  expect(requested.every(({ referer }) => referer === undefined)).toBe(true);
});

test("mobile Markdown source editor fills the available artifact body", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/tests/history-browser.html?artifact-markdown-source=1");
  await page.getByRole("button", { name: "源码" }).click();
  const editor = page.getByRole("textbox", { name: "Markdown 源码编辑器" });
  await expect(editor).toBeVisible();

  const measure = () => page.locator(".source-artifact-body").evaluate((body) => {
    const textarea = body.querySelector<HTMLTextAreaElement>(".markdown-editor");
    if (!textarea) throw new Error("Markdown editor is missing");
    const bodyRect = body.getBoundingClientRect();
    const editorRect = textarea.getBoundingClientRect();
    return {
      bodyHeight: bodyRect.height,
      editorHeight: editorRect.height,
      bottomGap: bodyRect.bottom - editorRect.bottom,
      scrollHeight: textarea.scrollHeight,
      clientHeight: textarea.clientHeight,
    };
  });

  let geometry = await measure();
  expect(geometry.editorHeight).toBeGreaterThan(geometry.bodyHeight * 0.8);
  expect(geometry.bottomGap).toBeLessThanOrEqual(16);
  expect(geometry.scrollHeight).toBeGreaterThan(geometry.clientHeight);

  await page.setViewportSize({ width: 390, height: 480 });
  geometry = await measure();
  expect(geometry.editorHeight).toBeGreaterThan(geometry.bodyHeight * 0.75);
  expect(geometry.bottomGap).toBeLessThanOrEqual(16);
});

test("dark desktop code block and copy action stay visually distinct", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/tests/history-browser.html?code-copy-theme=1&theme=dark");
  await page.waitForFunction(() =>
    document.documentElement.dataset.theme === "dark"
  );
  await page.waitForTimeout(200);
  const copy = page.getByRole("button", { name: "复制代码" });
  await expect(copy).toBeVisible();

  const appearance = await copy.evaluate((button) => {
    const sample = (color: string) => {
      const canvas = document.createElement("canvas");
      canvas.width = 1;
      canvas.height = 1;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("canvas context unavailable");
      context.clearRect(0, 0, 1, 1);
      context.fillStyle = color;
      context.fillRect(0, 0, 1, 1);
      return Array.from(context.getImageData(0, 0, 1, 1).data);
    };
    const composite = (front: number[], back: number[]) => {
      const alpha = front[3] / 255;
      return front.slice(0, 3).map((value, index) =>
        value * alpha + back[index] * (1 - alpha)
      );
    };
    const luminance = (color: number[]) => {
      const channels = color.slice(0, 3).map((value) => {
        const normalized = value / 255;
        return normalized <= 0.04045
          ? normalized / 12.92
          : ((normalized + 0.055) / 1.055) ** 2.4;
      });
      return channels[0] * 0.2126 + channels[1] * 0.7152
        + channels[2] * 0.0722;
    };
    const block = button.closest(".message-code-block");
    const code = block?.querySelector("pre");
    const pageSurface = button.closest("main");
    if (!code || !pageSurface) throw new Error("code block is missing");
    const foreground = sample(getComputedStyle(button).color);
    const expectedForeground = sample(
      getComputedStyle(document.documentElement).getPropertyValue("--text"),
    );
    const buttonBackground = sample(getComputedStyle(button).backgroundColor);
    const codeBackground = sample(getComputedStyle(code).backgroundColor);
    const pageBackground = sample(getComputedStyle(pageSurface).backgroundColor);
    const effectiveCodeBackground = composite(codeBackground, pageBackground);
    const effectiveButtonBackground = composite(
      buttonBackground,
      effectiveCodeBackground,
    );
    const lighter = Math.max(
      luminance(foreground),
      luminance(effectiveButtonBackground),
    );
    const darker = Math.min(
      luminance(foreground),
      luminance(effectiveButtonBackground),
    );
    const codeLighter = Math.max(
      luminance(effectiveCodeBackground),
      luminance(pageBackground),
    );
    const codeDarker = Math.min(
      luminance(effectiveCodeBackground),
      luminance(pageBackground),
    );
    const codeBackgroundDelta = effectiveCodeBackground.reduce(
      (total, value, index) => total + Math.abs(value - pageBackground[index]),
      0,
    ) / 3;
    return {
      contrast: (lighter + 0.05) / (darker + 0.05),
      codeBackgroundContrast: (codeLighter + 0.05) / (codeDarker + 0.05),
      codeBackgroundDelta,
      foreground,
      expectedForeground,
    };
  });

  expect(appearance.contrast).toBeGreaterThanOrEqual(4.5);
  expect(appearance.codeBackgroundContrast).toBeGreaterThanOrEqual(1.28);
  expect(appearance.codeBackgroundDelta).toBeGreaterThanOrEqual(24);
  expect(appearance.foreground).toEqual(appearance.expectedForeground);
});

test("local Markdown file link reveals its complete path without native title", async ({
  page,
}) => {
  await page.setViewportSize({ width: 720, height: 480 });
  await page.goto("/tests/history-browser.html?local-file-link=1");
  const link = page.getByRole("button", {
    name: "在 Remote 中打开 /tmp/qwen3-tts-v017-release-test:42",
  });
  await expect(link).not.toHaveAttribute("title");

  await link.hover();
  const tooltip = page.getByRole("tooltip");
  await expect(tooltip).toContainText("/tmp/qwen3-tts-v017-release-test:42");
  await expect(tooltip).toBeInViewport();

  await tooltip.hover();
  await page.waitForTimeout(260);
  await expect(tooltip).toBeVisible();
  const path = tooltip.locator(".message-file-tooltip-path");
  await path.dblclick();
  await expect.poll(() => page.evaluate(() => getSelection()?.toString()))
    .toBe("/tmp/qwen3-tts-v017-release-test:42");
  await expect(tooltip.getByRole("button")).toHaveCount(0);

  await page.mouse.move(700, 460);
  await expect(tooltip).toHaveCount(0, { timeout: 1000 });
  await link.focus();
  await expect(page.getByRole("tooltip")).toBeVisible();
});

test("local Markdown file links retire the previous tooltip before showing the next path", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 720, height: 480 });
  await page.route("**/api/link-preview", route => route.fulfill({ json: {} }));
  await page.goto("/tests/history-browser.html?local-file-link=1");
  const links = page.locator(".message-file-link");
  const fullPath = "/workspace/project/packages/frontend/src/components/conversation/MessageBlock.tsx:455";
  for (const index of [0, 1, 0, 1]) {
    await links.nth(index).hover();
    expect(await page.getByRole("tooltip").count()).toBe(0);
    await expect(page.getByRole("tooltip")).toContainText(index ? fullPath : "/tmp/qwen3-tts-v017-release-test:42");
  }
  const tip = page.getByRole("tooltip");
  const box = await tip.boundingBox();
  expect(box!.width).toBeLessThanOrEqual(360);
  expect(box!.height).toBeGreaterThan(50);
  await page.screenshot({ path: testInfo.outputPath("compact-file-path.png") });
  await tip.locator(".message-file-tooltip-path").dblclick();
  await expect.poll(() => page.evaluate(() => getSelection()?.toString())).toBe(fullPath);
  await page.keyboard.press("Escape");
  await expect(tip).toHaveCount(0);
});

test("local Markdown file link and webpage previews share one overlay without shifting the conversation", async ({ page }, testInfo) => {
  const requests: string[] = [];
  await page.route("**/api/link-preview", async route => {
    const body = route.request().postDataJSON();
    requests.push(body.detail);
    await route.fulfill({ json: body.detail === "icon" ? {} : {
      title: "Agent loop overview", description: "Messages, tools, and conversation history.",
    } });
  });
  await page.goto("/tests/history-browser.html?local-file-link=1");
  const text = page.getByText("正文保持原位，预览只悬浮显示。");
  const before = await text.boundingBox();
  const file = page.locator(".message-file-link").first();
  await file.hover();
  await expect(page.getByRole("tooltip")).toHaveCount(1);
  const link = page.getByRole("link", { name: "official-docs" });
  await link.hover();
  expect(await page.getByRole("tooltip").count()).toBe(0);
  const preview = page.getByRole("dialog", { name: "网页预览 preview.example" });
  await expect(preview).toContainText("Agent loop overview");
  await expect(preview).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath("web-link-preview.png") });
  expect(await text.boundingBox()).toEqual(before);
  await expect(link).toHaveAttribute("href", "https://preview.example/docs");
  await preview.hover();
  await page.waitForTimeout(240);
  await expect(preview).toBeVisible();
  await file.hover();
  await expect(preview).toHaveCount(0);
  await expect(page.getByRole("tooltip")).toHaveCount(1);
  await page.getByRole("button", { name: "预览 preview.example", exact: true }).click();
  await expect(preview).toBeVisible();
  await expect(page.getByRole("tooltip")).toHaveCount(0);
  await preview.getByRole("button", { name: "关闭网页预览" }).click();
  await expect(preview).toHaveCount(0);
  expect(requests.filter(detail => detail === "page")).toHaveLength(1);
  const icon = page.getByRole("button", { name: "预览 preview.example", exact: true });
  await icon.focus();
  await page.keyboard.press("ArrowDown");
  await expect(preview.getByRole("link", { name: "打开 ↗" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(preview).toHaveCount(0);
  await expect(icon).toBeFocused();
});

test("local Markdown file link preview retries missing images when reopened after cache expiry", async ({ page }) => {
  let attempts = 0;
  await page.route("**/api/link-preview", async (route) => {
    if (route.request().postDataJSON().detail === "icon") return route.fulfill({ json: {} });
    attempts++;
    return route.fulfill({ json: { title: "Recoverable preview",
      ...(attempts > 1 ? { icon: `data:image/png;base64,${TEST_GENERATED_PNG}`,
        image: `data:image/png;base64,${TEST_GENERATED_PNG}` } : {}) } });
  });
  await page.goto("/tests/history-browser.html?local-file-link=1");
  const trigger = page.getByRole("button", { name: "预览 preview.example", exact: true });
  const preview = page.getByRole("dialog", { name: "网页预览 preview.example" });
  await trigger.click();
  await expect(preview).toContainText("Recoverable preview");
  await expect(preview.locator(".message-web-preview-cover")).toHaveCount(0);
  await preview.getByRole("button", { name: "关闭网页预览" }).click();
  await expect(preview).toHaveCount(0);
  await page.evaluate(() => {
    const now = Date.now;
    Date.now = () => now() + 61_000;
  });
  await trigger.click();
  await expect(preview.locator(".message-web-preview-cover")).toBeVisible();
  await expect(preview.locator(".message-site-icon")).toBeVisible();
  expect(attempts).toBe(2);
});

for (const top of [24, 200, 400]) {
  test(`local Markdown file link preview stays open after loading under a stationary pointer at ${top}px`, async ({ page }) => {
    await page.setViewportSize({ width: 720, height: 540 });
    let finishPage!: () => void;
    const pageGate = new Promise<void>(resolve => { finishPage = resolve; });
    await page.route("**/api/link-preview", async route => {
      const { detail } = route.request().postDataJSON();
      if (detail === "page") await pageGate;
      await route.fulfill({ json: detail === "icon" ? {} : {
        title: "Agent loop overview", description: "Messages, tools, and conversation history.",
        icon: `data:image/png;base64,${TEST_GENERATED_PNG}`,
        image: `data:image/png;base64,${TEST_GENERATED_PNG}`,
      } });
    });
    await page.goto("/tests/history-browser.html?local-file-link=1");
    await page.locator("main").evaluate((node, padding) => { node.style.paddingTop = `${padding}px`; }, top);
    const icon = page.getByRole("button", { name: "预览 preview.example", exact: true });
    await icon.hover();
    const preview = page.getByRole("dialog", { name: "网页预览 preview.example" });
    await expect(preview).toContainText("正在加载预览");
    finishPage();
    await expect(preview).toContainText("Agent loop overview");
    await expect(icon.locator("img")).toBeVisible();
    await page.waitForTimeout(400);
    await expect(preview).toBeVisible();
    await page.mouse.move(700, 520);
    await expect(preview).toHaveCount(0);
  });
}

test("local Markdown file link icons stay inside the inline button in document previews", async ({ page }) => {
  const icon = await page.evaluate(() => {
    const canvas = document.createElement("canvas");
    canvas.width = 64; canvas.height = 16;
    const ctx = canvas.getContext("2d")!;
    ctx.fillStyle = "#4285f4"; ctx.fillRect(0, 0, 64, 16);
    return canvas.toDataURL("image/png");
  });
  await page.route("**/api/link-preview", route => route.fulfill({ json: { icon } }));
  await page.goto("/tests/history-browser.html?local-file-link=1");
  const button = page.getByRole("button", { name: "预览 preview.example", exact: true });
  const image = button.locator("img");
  await expect(image).toBeVisible();
  for (const documentPreview of [false, true]) {
    await page.locator(".prose").evaluate((node, enabled) => {
      node.classList.toggle("markdown-preview", enabled);
    }, documentPreview);
    await expect(image).toHaveCSS("height", "16px");
    await expect(image).toHaveCSS("margin-top", "0px");
    const bounds = (await button.boundingBox())!;
    const pixels = (await image.boundingBox())!;
    expect(pixels.y).toBeGreaterThanOrEqual(bounds.y);
    expect(pixels.y + pixels.height).toBeLessThanOrEqual(bounds.y + bounds.height);
  }
});

for (const artwork of ["dark", "white", "colored", "opaque", "details"] as const) {
  test(`local Markdown file link icons adapt ${artwork} artwork without losing brand colors`, async ({ page }) => {
    const icon = await page.evaluate(kind => {
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = 32;
      const ctx = canvas.getContext("2d")!;
      if (kind === "opaque") { ctx.fillStyle = "white"; ctx.fillRect(0, 0, 32, 32); }
      ctx.fillStyle = kind === "white" ? "white" : kind === "colored" ? "#ffd21e" : "#24292f";
      ctx.beginPath(); ctx.arc(16, 16, 12, 0, Math.PI * 2); ctx.fill();
      if (kind === "details") { ctx.fillStyle = "white"; ctx.fillRect(12, 12, 8, 8); }
      return canvas.toDataURL("image/png");
    }, artwork);
    await page.route("**/api/link-preview", route => route.fulfill({ json: { icon, title: "Documentation" } }));
    await page.goto("/tests/history-browser.html?local-file-link=1");
    const button = page.getByRole("button", { name: "预览 preview.example", exact: true });
    const monochrome = artwork === "dark" || artwork === "white";
    await expect(button.locator(monochrome ? ".is-monochrome" : "img")).toBeVisible();
    await button.click();
    const header = page.getByRole("dialog", { name: "网页预览 preview.example" }).locator(".message-web-preview-header");
    await expect(header.locator(monochrome ? ".is-monochrome" : "img")).toBeVisible();
    for (const [engine, theme, color] of [
      ["codex", "dark", "rgb(236, 237, 243)"], ["codex", "light", "rgb(37, 35, 52)"],
      ["claude", "dark", "rgb(236, 233, 223)"], ["claude", "light", "rgb(32, 30, 24)"],
    ]) {
      await page.evaluate(({ engine, theme }) => {
        document.documentElement.dataset.engine = engine;
        document.documentElement.dataset.theme = theme;
      }, { engine, theme });
      for (const parent of [button, header]) {
        const image = parent.locator(".message-site-icon");
        await expect(image).toHaveCSS("width", "16px");
        await expect(image).toHaveCSS("height", "16px");
        if (monochrome) {
          await expect(image).toHaveCSS("background-color", color);
          expect(await image.evaluate(node => getComputedStyle(node).maskImage)).toContain(icon);
        } else {
          await expect(image).toHaveAttribute("src", icon);
          await expect(image).toHaveCSS("filter", "none");
          await expect(parent.locator(".is-monochrome")).toHaveCount(0);
        }
      }
    }
    if (monochrome) {
      await page.evaluate(() => document.documentElement.style.setProperty("--text", "#c2dfd3"));
      await expect(button.locator(".message-site-icon")).toHaveCSS("background-color", "rgb(194, 223, 211)");
      await expect(header.locator(".message-site-icon")).toHaveCSS("background-color", "rgb(194, 223, 211)");
    }
  });
}

for (const previewFirst of [false, true]) {
  test(`local Markdown file links share website icons ${previewFirst ? "after preview despite a late automatic miss" : "automatically without opening a preview"}`, async ({ page }) => {
    const icon = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6SAAAAABJRU5ErkJggg==";
    const requests: string[] = [];
    let releaseIcon!: () => void;
    const iconGate = new Promise<void>((resolve) => { releaseIcon = resolve; });
    await page.route("**/api/link-preview", async route => {
      const body = route.request().postDataJSON();
      requests.push(body.detail);
      if (body.detail === "icon") {
        expect(body.url).toBe("https://preview.example");
        if (previewFirst) await iconGate;
      }
      await route.fulfill({ json: previewFirst && body.detail === "icon" ? {} : { icon } });
    });
    await page.goto("/tests/history-browser.html?local-file-link=1&same-origin-links=1");
    const buttons = page.locator(".message-web-link .message-link-icon");
    await expect(buttons).toHaveCount(2);
    await expect.poll(() => requests.filter(detail => detail === "icon").length).toBe(1);
    if (previewFirst) await buttons.first().click();
    else await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(buttons.locator("img")).toHaveCount(2);
    await expect(buttons.last().locator("img")).toHaveAttribute("src", icon);
    if (previewFirst) {
      const response = page.waitForResponse(response => response.url().endsWith("/api/link-preview")
        && response.request().postDataJSON().detail === "icon");
      releaseIcon();
      await response;
      await expect(buttons.locator("img")).toHaveCount(2);
    }
    expect(requests.filter(detail => detail === "page")).toHaveLength(previewFirst ? 1 : 0);
  });
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

test("Codex settings opens the responsive daily usage activity view", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?header-menu=1&engine=codex");
  await page.getByRole("button", { name: "更多设置" }).click();
  const activityEntry = page.getByRole("button", { name: /使用活动/ });
  await expect(activityEntry).toBeVisible();
  await activityEntry.click();

  const dialog = page.getByRole("dialog", { name: "Codex 使用活动" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("累计 Token");
  await expect(dialog).toContainText("9.88亿");
  await expect(dialog).toContainText("单日峰值");
  await expect(dialog).toContainText("最长任务");
  await expect(dialog).toContainText("当前连续");
  await expect(dialog).toContainText("最长连续");
  await expect(dialog.locator(".usage-activity-tile")).toHaveCount(371);
  const activityBox = (await dialog.boundingBox())!;
  expect(activityBox.y).toBeGreaterThan(12);
  expect(activityBox.y + activityBox.height).toBeLessThan(page.viewportSize()!.height - 12);
  expect(await dialog.evaluate(node => parseFloat(getComputedStyle(node).borderBottomLeftRadius))).toBeGreaterThan(16);

  const geometry = await page.locator(".usage-activity-viewport")
    .evaluate((viewport) => ({
      viewportWidth: window.innerWidth,
      pageScrollWidth: document.documentElement.scrollWidth,
      clientWidth: viewport.clientWidth,
      scrollWidth: viewport.scrollWidth,
      scrollLeft: viewport.scrollLeft,
    }));
  expect(geometry.pageScrollWidth).toBeLessThanOrEqual(geometry.viewportWidth);
  if (geometry.scrollWidth > geometry.clientWidth) {
    expect(geometry.scrollLeft).toBeGreaterThan(0);
  }

  const busiest = dialog.locator('.usage-activity-tile[data-level="4"]')
    .first();
  await expect(busiest).toHaveAttribute(
    "title", /使用了 \d+(?:\.\d+)?万 个 Token/,
  );
  await busiest.click();
  await expect(dialog.locator(".usage-activity-caption"))
    .toContainText("Token");
});


test("Claude settings does not expose Codex usage activity", async ({ page }) => {
  await page.goto("/tests/history-browser.html?header-menu=1&engine=claude");
  await page.getByRole("button", { name: "更多设置" }).click();
  await expect(page.getByRole("button", { name: /使用活动/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /通知/ })).toBeVisible();
});

async function maxPaintedTurnOffsetShiftThroughAction(
  page: import("@playwright/test").Page,
  turnId: string,
  actionTestId: string,
  frameCount = 36,
): Promise<{ maxShift: number; missing: boolean }> {
  return page.evaluate(async ({ id, testId, frames }) => {
    const viewport = document.querySelector<HTMLElement>(".thread");
    const row = document.querySelector<HTMLElement>(
      `[data-turn-id="${CSS.escape(id)}"]`,
    );
    const action = document.querySelector<HTMLElement>(
      `[data-testid="${CSS.escape(testId)}"]`,
    );
    if (!viewport || !row || !action) {
      throw new Error("frame sampling target is missing");
    }
    const viewportTop = viewport.getBoundingClientRect().top;
    const initial = row.getBoundingClientRect().top - viewportTop;
    let maxShift = 0;
    let missing = false;
    action.click();
    await new Promise<void>((resolve) => {
      let remaining = frames;
      const sample = () => {
        const current = document.querySelector<HTMLElement>(
          `[data-turn-id="${CSS.escape(id)}"]`,
        );
        if (!current) {
          missing = true;
        } else {
          const offset = current.getBoundingClientRect().top
            - viewport.getBoundingClientRect().top;
          maxShift = Math.max(maxShift, Math.abs(offset - initial));
        }
        remaining -= 1;
        if (remaining <= 0) resolve();
        else requestAnimationFrame(() => window.setTimeout(sample, 0));
      };
      // rAF runs before ResizeObserver delivery. Sample after the paint
      // opportunity so the test observes user-visible frames, not the
      // browser's internal pre-observer layout checkpoint.
      requestAnimationFrame(() => window.setTimeout(sample, 0));
    });
    return { maxShift, missing };
  }, { id: turnId, testId: actionTestId, frames: frameCount });
}

async function maxTurnOffsetShiftThroughTouchHistoryLoad(
  page: import("@playwright/test").Page,
  turnId: string,
  frameCount = 60,
): Promise<{ maxShift: number; missing: boolean }> {
  return page.evaluate(async ({ id, frames }) => {
    const viewport = document.querySelector<HTMLElement>(".thread");
    const row = document.querySelector<HTMLElement>(
      `[data-turn-id="${CSS.escape(id)}"]`,
    );
    if (!viewport || !row) throw new Error("touch frame target is missing");
    const viewportTop = viewport.getBoundingClientRect().top;
    const initial = row.getBoundingClientRect().top - viewportTop;
    let maxShift = 0;
    let missing = false;
    const dispatch = (type: "touchstart" | "touchmove" | "touchend",
      clientY: number) => {
      const touch = {
        identifier: 1,
        target: viewport,
        clientX: 120,
        clientY,
      };
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperties(event, {
        touches: { value: type === "touchend" ? [] : [touch] },
        targetTouches: { value: type === "touchend" ? [] : [touch] },
        changedTouches: { value: [touch] },
      });
      viewport.dispatchEvent(event);
    };
    dispatch("touchstart", 160);
    dispatch("touchmove", 220);
    dispatch("touchend", 220);
    await new Promise<void>((resolve) => {
      let remaining = frames;
      const sample = () => {
        const current = document.querySelector<HTMLElement>(
          `[data-turn-id="${CSS.escape(id)}"]`,
        );
        if (!current) {
          missing = true;
        } else {
          const offset = current.getBoundingClientRect().top
            - viewport.getBoundingClientRect().top;
          maxShift = Math.max(maxShift, Math.abs(offset - initial));
        }
        remaining -= 1;
        if (remaining <= 0) resolve();
        else requestAnimationFrame(sample);
      };
      requestAnimationFrame(sample);
    });
    return { maxShift, missing };
  }, { id: turnId, frames: frameCount });
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

async function pauseOutputAndScrollToHistoryStart(
  page: import("@playwright/test").Page,
): Promise<void> {
  const viewport = page.locator(".thread");
  // Initial virtual measurements can finish one frame after the first write
  // and reassert the mounted tail. Retry the neutral test setup until the
  // physical viewport itself is stable at the history edge; no user-intent
  // event is emitted here, so the page request still belongs to the action
  // under test.
  for (let attempt = 0; attempt < 6; attempt += 1) {
    await viewport.evaluate((node) => { node.scrollTop = 0; });
    await waitForScrollIdle(page);
    if (await viewport.evaluate((node) => node.scrollTop <= 1)) return;
  }
  expect(await viewport.evaluate((node) => node.scrollTop)).toBeLessThanOrEqual(1);
}

async function waitForReadingPositionIdle(
  page: import("@playwright/test").Page,
): Promise<void> {
  let previous: { id: string; offset: number } | null = null;
  let stableSamples = 0;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const current = await readingAnchor(page).catch(() => null);
    if (!current) {
      previous = null;
      stableSamples = 0;
      await page.waitForTimeout(50);
      continue;
    }
    stableSamples = previous != null
      && current.id === previous.id
      && Math.abs(current.offset - previous.offset) < 0.5
      ? stableSamples + 1 : 0;
    if (stableSamples >= 4) return;
    previous = current;
    await page.waitForTimeout(50);
  }
  throw new Error("thread visual reading position did not settle");
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

function isMobileWebKitProject(projectName: string): boolean {
  return projectName.startsWith("webkit") && projectName !== "webkit-desktop-selection";
}

async function dispatchCancelledTouchTap(
  locator: import("@playwright/test").Locator,
  pointerId: number,
): Promise<void> {
  await locator.evaluate((node, id) => {
    const target = node as HTMLElement;
    let capturedPointer: number | null = null;
    let releases = 0;
    Object.defineProperties(target, {
      setPointerCapture: {
        configurable: true,
        value: (candidate: number) => { capturedPointer = candidate; },
      },
      hasPointerCapture: {
        configurable: true,
        value: (candidate: number) => capturedPointer === candidate,
      },
      releasePointerCapture: {
        configurable: true,
        value: (candidate: number) => {
          if (capturedPointer === candidate) capturedPointer = null;
          releases += 1;
        },
      },
    });
    target.focus();
    const bounds = target.getBoundingClientRect();
    const clientX = bounds.left + bounds.width / 2;
    const clientY = bounds.top + bounds.height / 2;
    target.dispatchEvent(new PointerEvent("pointerdown", {
      bubbles: true,
      cancelable: true,
      pointerId: id,
      pointerType: "touch",
      clientX,
      clientY,
      buttons: 1,
    }));
    target.dispatchEvent(new PointerEvent("pointercancel", {
      bubbles: true,
      cancelable: true,
      pointerId: id,
      pointerType: "touch",
      clientX,
      clientY,
    }));
    target.dispatchEvent(new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      clientX,
      clientY,
      detail: 1,
    }));
    target.dataset.cancelReleaseCount = String(releases);
    target.dataset.cancelCaptureActive = String(capturedPointer === id);
    target.dataset.cancelStillFocused = String(document.activeElement === target);
  }, pointerId);
  await expect(locator).toHaveAttribute("data-cancel-release-count", "1");
  await expect(locator).toHaveAttribute("data-cancel-capture-active", "false");
  await expect(locator).toHaveAttribute("data-cancel-still-focused", "false");
}

async function wheelUntilTurn(
  page: import("@playwright/test").Page,
  turnId: string,
  deltaY: number,
  projectName: string,
): Promise<void> {
  const viewport = page.locator(".thread");
  if (isMobileWebKitProject(projectName)) {
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
  for (let attempt = 0; attempt < 40; attempt += 1) {
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
    if (isMobileWebKitProject(projectName)) {
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
  if (!isMobileWebKitProject(projectName)) {
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
  if (!isMobileWebKitProject(projectName)) {
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

test("reducer history paging and live refresh keep one stable projection", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?reducer-pipeline=1");
  await expect(page.locator('[data-turn-id="reducer-m40"]')).toBeVisible();
  await expect(page.getByTestId("reducer-turn-count")).toHaveText("20");
  await expect(page.getByTestId("reducer-unique-turn-count")).toHaveText("20");

  await pauseOutputAndScrollToHistoryStart(page);
  const before = await readingAnchor(page);
  await page.getByRole("button", { name: "加载更早的历史" }).click();
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.locator('[data-turn-id="reducer-m20"]')).toBeAttached();
  await expect(page.getByTestId("reducer-turn-count")).toHaveText("40");
  await expect(page.getByTestId("reducer-unique-turn-count")).toHaveText("40");
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);

  // A same-revision newest-page refresh is delivered to the authoritative
  // runtime while the reducer-owned browse projection remains visible.
  await page.getByTestId("reducer-live-refresh").click();
  await expect(page.getByTestId("reducer-refresh-count")).toHaveText("1");
  await expect(page.getByTestId("reducer-turn-count")).toHaveText("40");
  await expect(page.getByTestId("reducer-unique-turn-count")).toHaveText("40");
  const afterRefresh = await readingAnchor(page);
  expect(afterRefresh.id).toBe(before.id);
  expect(Math.abs(afterRefresh.offset - before.offset)).toBeLessThan(2);

  // A -> B -> A discards only the display browse window. The accepted runtime
  // must still contain one canonical copy of every newest turn.
  await page.getByTestId("switch-session").click();
  await expect(page.getByTestId("reducer-focused-sid"))
    .toHaveText("reducer-history-session-b");
  await expect(page.locator('[data-turn-id="reducer-b8"]')).toBeVisible();
  await page.getByTestId("switch-session").click();
  await expect(page.getByTestId("reducer-focused-sid"))
    .toHaveText("reducer-history-session-a");
  await expect(page.locator('[data-turn-id="reducer-m40"]')).toBeVisible();
  await expect(page.getByTestId("reducer-turn-count")).toHaveText("20");
  await expect(page.getByTestId("reducer-unique-turn-count")).toHaveText("20");
});

test("Claude history aliases keep the greeting first without duplicated conversations", async ({ page }) => {
  await page.goto("/tests/history-browser.html?reducer-pipeline=1&claude-aliases=1");
  const expected = ["hi claude", ...Array.from({ length: 9 }, (_, index) =>
    `Claude question ${index + 2}`)];
  await expect(page.getByTestId("reducer-turn-count")).toHaveText("10");
  await pauseOutputAndScrollToHistoryStart(page);
  await page.getByRole("button", { name: "加载更早的历史" }).click();
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.getByTestId("reducer-turn-count")).toHaveText("10");
  await expect(page.getByTestId("reducer-unique-turn-count")).toHaveText("10");
  await expect(page.locator(".ubub")).toHaveText(expected);
  await page.getByTestId("reducer-live-refresh").click();
  await expect(page.locator(".ubub")).toHaveText(expected);
  await page.getByTestId("switch-session").click();
  await page.getByTestId("switch-session").click();
  await pauseOutputAndScrollToHistoryStart(page);
  await expect(page.locator(".ubub")).toHaveText(expected);
});

test("authoritative paging returns after an IndexedDB first paint", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?reducer-pipeline=1&cached-paging=1",
  );
  await expect(page.locator('[data-turn-id="reducer-cached-current"]'))
    .toBeVisible();
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const loader = page.getByTestId("load-older-history");
  await expect(loader).toBeVisible();
  const [loaderBox, viewportBox] = await Promise.all([
    loader.boundingBox(),
    viewport.boundingBox(),
  ]);
  expect(loaderBox).not.toBeNull();
  expect(viewportBox).not.toBeNull();
  expect(loaderBox!.y).toBeGreaterThanOrEqual(viewportBox!.y);
  expect(loaderBox!.y + loaderBox!.height)
    .toBeLessThanOrEqual(viewportBox!.y + viewportBox!.height);
});

test("reducer prepend never paints an intermediate reading-row jump", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?reducer-pipeline=1");
  await expect(page.locator('[data-turn-id="reducer-m40"]')).toBeVisible();
  await pauseOutputAndScrollToHistoryStart(page);
  const before = await readingAnchor(page);
  const sampled = await maxPaintedTurnOffsetShiftThroughAction(
    page,
    before.id,
    "load-older-history",
    60,
  );
  await expect(page.locator('[data-turn-id="reducer-m20"]')).toBeAttached();
  const after = await readingAnchor(page);
  expect(sampled.missing).toBe(false);
  expect(sampled.maxShift).toBeLessThan(2);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("touching the history edge never paints an intermediate reading-row jump", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?reducer-pipeline=1");
  await expect(page.locator('[data-turn-id="reducer-m40"]')).toBeVisible();
  await pauseOutputAndScrollToHistoryStart(page);
  const before = await readingAnchor(page);
  const sampled = await maxTurnOffsetShiftThroughTouchHistoryLoad(
    page,
    before.id,
  );
  await expect(page.locator('[data-turn-id="reducer-m20"]')).toBeAttached();
  const after = await readingAnchor(page);
  expect(sampled.missing).toBe(false);
  expect(sampled.maxShift).toBeLessThan(2);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("a history page waits for the active touch to release before mounting", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?delay=20&manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await dispatchTouchPhase(page, "touchstart", 160);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  const pageActivity = page.getByTestId("history-page-activity");
  await expect(pageActivity).toContainText("正在加载更早历史");
  await expect(pageActivity).toHaveCSS("pointer-events", "none");
  await page.waitForTimeout(100);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
  await expect(pageActivity).toBeVisible();
  const held = await readingAnchor(page);
  expect(held.id).toBe(before.id);
  expect(Math.abs(held.offset - before.offset)).toBeLessThan(2);

  await dispatchTouchPhase(page, "touchend", 220);
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await expect(pageActivity).toHaveCount(0);
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
  await page.waitForTimeout(350);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(before.id);
  expect(Math.abs(settled.offset - before.offset)).toBeLessThan(2);
});

test("the first runtime browse page stays staged under an active touch", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "mobile WebKit touch path");
  await page.goto(
    "/tests/history-browser.html?runtime-browse=1&delay=0&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await dispatchTouchPhase(page, "touchstart", 160);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await page.waitForTimeout(100);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
  await expect(page.getByTestId("history-page-activity")).toBeVisible();
  const held = await readingAnchor(page);
  expect(held.id).toBe(before.id);
  expect(Math.abs(held.offset - before.offset)).toBeLessThan(2);

  await dispatchTouchPhase(page, "touchend", 220);
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
});

test("the first runtime browse page stays staged until wheel idle", async ({
  page,
}, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name),
    "desktop wheel path");
  await page.goto(
    "/tests/history-browser.html?runtime-browse=1&delay=20&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await viewport.evaluate((node) => {
    const target = node as HTMLElement & { __wheelLease?: number };
    const signalWheel = () => target.dispatchEvent(new WheelEvent("wheel", {
      bubbles: true,
      deltaY: -80,
    }));
    signalWheel();
    target.__wheelLease = window.setInterval(signalWheel, 50);
  });
  try {
    await expect(page.getByTestId("load-count")).toHaveText("1");
    await page.waitForTimeout(100);
    await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
    await expect(page.getByTestId("history-page-activity")).toBeVisible();
  } finally {
    await viewport.evaluate((node) => {
      const target = node as HTMLElement & { __wheelLease?: number };
      if (target.__wheelLease != null) {
        window.clearInterval(target.__wheelLease);
        delete target.__wheelLease;
      }
    });
  }

  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
});

test("older history becoming available during a wheel gesture is restored once", async ({
  page,
}, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name),
    "desktop wheel path");
  await page.goto(
    "/tests/history-browser.html?delayed-history-availability=1&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const before = await readingAnchor(page);
  await viewport.dispatchEvent("wheel", { deltaY: -80 });

  const pending = await readingAnchor(page);
  expect(pending.id).toBe(before.id);
  expect(Math.abs(pending.offset - before.offset)).toBeLessThan(2);
  await expect(page.getByTestId("load-count")).toHaveText("0");
  await viewport.evaluate((node) => {
    node.dispatchEvent(new WheelEvent("wheel", {
      bubbles: true,
      deltaY: -80,
    }));
    const reveal = document.querySelector<HTMLButtonElement>(
      '[data-testid="reveal-older-history"]',
    );
    if (!reveal) throw new Error("history reveal control is missing");
    reveal.click();
  });
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await page.waitForTimeout(250);
  await expect(page.getByTestId("load-count")).toHaveText("1");
});

test("older history becoming available under touch waits for release", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "mobile WebKit touch path");
  await page.goto(
    "/tests/history-browser.html?delayed-history-availability=1&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await dispatchTouchPhase(page, "touchstart", 160);
  await dispatchTouchPhase(page, "touchmove", 220);
  await page.getByTestId("reveal-older-history").click();

  await expect(page.getByText("正在恢复历史…")).toBeVisible();
  await expect(page.getByTestId("load-count")).toHaveText("0");
  await dispatchTouchPhase(page, "touchend", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await page.waitForTimeout(250);
  await expect(page.getByTestId("load-count")).toHaveText("1");
});

test("restored turn detail uses one process disclosure and preserves its duration", async ({ page }) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&detail-restored-page=process",
  );
  const header = page.locator(".turn-process-head");
  await expect(header).toContainText("已处理 2h 9m");
  await expect(header).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator(".turn-detail-entry")).toHaveCount(0);
  await header.click();
  await expect(page.getByRole("button", { name: "加载更早过程" })).toBeVisible();
  await page.getByRole("button", { name: "加载更早过程" }).click();
  await expect(page.getByText("较早命令 1")).toBeVisible();
  await expect(page.locator(".turn-detail-entry")).toHaveCount(0);
});

for (const direction of ["older", "newer"]) {
  for (const failOnce of [false, true]) {
    test(`standalone turn detail follows its ${direction} cursor${failOnce ? " after retry" : ""}`, async ({ page }) => {
      await page.goto(
        "/tests/history-browser.html?detail-paging=1&delay=100"
          + `&detail-restored-page=${direction}`
          + (failOnce ? "&detail-error-once=1" : ""),
      );
      await expect(page.locator(".turn-process-head")).toHaveCount(0);
      const more = page.getByRole("button", { name: "查看更多内容" });
      await more.click();
      await expect(page.locator("html"))
        .toHaveAttribute("data-detail-last-before", `detail-${direction}`);
      if (failOnce) {
        await expect(page.locator(".turn-detail-entry-error")).toContainText("详细过程暂时不可用");
        await more.click();
      }
      await expect(page.locator(".turn-detail-entry")).toHaveCount(0);
      await expect(page.locator("html"))
        .toHaveAttribute("data-detail-requests", failOnce ? "2" : "1");
      await expect(page.locator("html"))
        .toHaveAttribute("data-detail-last-before", `detail-${direction}`);
      const header = page.locator(".turn-process-head");
      if (await header.getAttribute("aria-expanded") === "false") await header.click();
      await expect(page.getByText("较早命令 1")).toBeVisible();
      await expect(page.getByText("较新命令 1")).toBeVisible();
    });
  }
}

test("turn detail stays bounded and older pages load explicitly without jumping", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&delay=1000&growth-delay=180",
  );
  const header = page.locator(".turn-process-head");
  await expect(header).toHaveAttribute("aria-expanded", "false");
  const initialStart = await processDetailEdge(page, "start");
  await header.click();
  await expect(header).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText("正在加载过程…")).toBeVisible();
  await expect(page.locator(".thread"))
    .toHaveAttribute("data-detail-anchor-active", "true");
  await expect(page.getByText("较新命令 1")).toBeVisible();
  await expect(page.getByText("较早命令 1")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "加载更早过程" }))
    .toBeVisible();
  await expect(page.getByRole("button", { name: "返回较新过程" }))
    .toHaveCount(0);
  await page.waitForTimeout(500);
  expect(Math.abs(await processDetailEdge(page, "start") - initialStart))
    .toBeLessThan(2);
  await expect(page.locator(".thread"))
    .toHaveAttribute("data-detail-anchor-active", "false");
  expect(await page.evaluate(
    () => document.documentElement.dataset.detailRequests,
  )).toBe("1");

  const beforeOlderPage = await processDetailEdge(page, "start");
  await page.getByRole("button", { name: "加载更早过程" }).click();
  await expect(page.getByText("较早命令 1")).toBeVisible();
  await expect.poll(() => page.evaluate(
    () => document.documentElement.dataset.detailRequests,
  )).toBe("2");
  await expect(page.getByRole("button", { name: "加载更早过程" }))
    .toHaveCount(0);
  await page.waitForTimeout(500);
  expect(Math.abs(
    await processDetailEdge(page, "start") - beforeOlderPage,
  )).toBeLessThan(2);
});

for (const opening of ["default", "restored"]) {
  test(`initially ${opening} open process detail requests once and retries only explicitly`, async ({ page }) => {
    await page.goto("/tests/history-browser.html?detail-paging=1&delay=600&detail-error-once=1"
      + `&detail-initial-open=${opening}`);
    const header = page.locator(".turn-process-head");
    await expect(header).toHaveAttribute("aria-expanded", "true");
    await expect(page.locator("html")).toHaveAttribute("data-detail-requests", "1");
    // Automatic restoration must not start an explicit-click scroll transaction.
    await expect(page.locator(".thread")).toHaveAttribute("data-detail-anchor-active", "false");
    await expect(page.getByRole("alert")).toContainText("详细过程暂时不可用");
    await page.waitForTimeout(750);
    await expect(page.locator("html")).toHaveAttribute("data-detail-requests", "1");
    await page.getByRole("button", {name: "重试", exact: true}).click();
    await expect(page.locator("html")).toHaveAttribute("data-detail-requests", "2");
    await expect(header).toHaveAttribute("aria-busy", "false");
    await expect(page.getByRole("alert")).toHaveCount(0);
  });
}

test("a loading process can collapse and reopen without issuing a duplicate read", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&delay=10000&growth-delay=180",
  );
  const header = page.locator(".turn-process-head");
  await header.click();
  await expect(header).toHaveAttribute("aria-expanded", "true");
  await expect(header).toHaveAttribute("aria-busy", "true");
  await header.click();
  await expect(header).toHaveAttribute("aria-expanded", "false");
  await header.click();
  await expect(header).toHaveAttribute("aria-expanded", "true");
  expect(await page.evaluate(
    () => document.documentElement.dataset.detailRequests,
  )).toBe("1");
});

test("a failed process detail stays open and retries in place", async ({ page }) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&detail-error-once=1"
      + "&delay=500&growth-delay=30",
  );
  const header = page.locator(".turn-process-head");
  await header.click();
  await expect(header).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("alert")).toContainText(
    "详细过程暂时不可用，请稍后重试",
  );
  await page.getByRole("button", { name: "重试" }).click();
  await expect(header).toHaveAttribute("aria-expanded", "true");
  await expect(header).toHaveAttribute("aria-busy", "true");
  await expect(page.getByText("较新命令 1")).toBeVisible();
  await page.getByRole("button", { name: "加载更早过程" }).click();
  await expect(page.getByText("较早命令 1")).toBeVisible();
});

test("a failed older process page retries the exact cursor in place", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&detail-older-error-once=1"
      + "&delay=30&growth-delay=10000",
  );
  const header = page.locator(".turn-process-head");
  await header.click();
  await expect(page.getByText("较新命令 1")).toBeVisible();
  await page.getByRole("button", { name: "加载更早过程" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "详细过程暂时不可用，请稍后重试",
  );
  await expect.poll(() => page.evaluate(
    () => document.documentElement.dataset.detailLastBefore,
  )).toBe("detail-older");
  await page.getByRole("button", { name: "重试" }).click();
  await expect(page.getByText("较早命令 1")).toBeVisible();
  await expect.poll(() => page.evaluate(
    () => document.documentElement.dataset.detailRequests,
  )).toBe("3");
  await expect.poll(() => page.evaluate(
    () => document.documentElement.dataset.detailLastBefore,
  )).toBe("detail-older");
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
  await expect(page.getByText("较早过程已省略")).toHaveCount(0);
  await expect(header).toHaveAttribute("aria-busy", "true");
  await expect(page.getByText("较早过程已省略")).toHaveCount(0);
  await expect(page.getByText("较新命令 1")).toBeVisible();
  await expect(page.getByText("较早命令 1")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "加载更早过程" }))
    .toBeVisible();
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

  await expect(page.getByText("较新命令 1")).toBeVisible();
  await page.waitForTimeout(450);
  expect(Math.abs(await processDetailEdge(page, "start") - userOffset))
    .toBeLessThan(2);
});

test("instant session cache Claude deployment replay settles before and after refresh", async ({ page }) => {
  const sid = "layout-parent";
  let liveSeq = 0;
  const source = (): PanelRelayEvent<Extract<ServerEvent, { type: "history" }>> => ({
    type: "history", sid, session_id: sid, revision: "layout-history",
    generation: "layout-generation", live_seq: liveSeq, authoritative: true,
    detail: "summary", in_progress: false, has_more: true,
    newest_id: "native-human", events: [], turns: [{
      id: "native-human", prompt: "run task", done: true,
      ts: 1_000, doneTs: 9_000, durationMs: 8_000, detailEventCount: 1,
      detailLoaded: false, processDetailState: "present", blocks: [{
        kind: "text", message_id: "native-answer", channel: "final",
        text: "Finished task.", done: true,
      }],
    }],
  });
  const relay = await mockRightPanelRelay(page, {
    engine: "claude", retained: false,
    historyReply: command => ({ ...source(), request_id: String(command.cmd_id) }),
  });
  await page.goto("/");
  await expect(page.getByText("Finished task.", { exact: true })).toBeVisible();
  relay.emit({ type: "replay_start", sid, from_seq: 1, to_seq: 5,
    truncated: false, rebuild: false, generation: "layout-generation" });
  relay.emit({ type: "assistant_msg_start", sid, seq: 1,
    message_id: "replayed-envelope", channel: "commentary" });
  relay.emit({ type: "delta", sid, seq: 2, message_id: "replayed-envelope",
    text: "Checking files", channel: "commentary" });
  relay.emit({ type: "tool_use", sid, seq: 3, message_id: "replayed-envelope",
    tool_use_id: "replayed-tool", tool: "Bash", input: { command: "true" } });
  relay.emit({ type: "tool_result", sid, seq: 4,
    tool_use_id: "replayed-tool", content: "finished", is_error: false });
  relay.emit({ type: "assistant_msg_end", sid, seq: 5,
    message_id: "replayed-envelope", channel: "commentary" });
  relay.emit({ type: "replay_end", sid, to_seq: 5, truncated: false });
  liveSeq = 5;
  relay.emit(source());
  await expect(page.locator('[data-turn-id="replayed-envelope"]')).toHaveCount(0);
  await expect(page.locator('[data-turn-id="native-human"]')).toHaveCount(1);
  await expect(page.locator(".turn-problem, .turn-failure-status")).toHaveCount(0);
  for (let iteration = 0; iteration < 2; iteration++) {
    await page.reload();
    await expect(page.getByText("Finished task.", { exact: true })).toBeVisible();
    await expect(page.locator('[data-turn-id="replayed-envelope"]')).toHaveCount(0);
    await expect(page.locator(".turn-problem, .turn-failure-status")).toHaveCount(0);
  }
  expect(relay.commands.some(command => ["query", "steer", "new_session"]
    .includes(String(command.type)))).toBe(false);
});

test("instant session cache rebuilds Claude deployment residue in real IndexedDB", async ({ page }) => {
  await page.goto("/tests/history-browser.html");
  const ignored = await page.evaluate(async () => {
    const cache = await import("/src/cache.ts");
    const { WEB_BUILD } = await import("/src/web-build.ts");
    await cache.clearCache();
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open("cc_remote_cache", 1);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction("sessions", "readwrite");
      transaction.objectStore("sessions").put({
        sid: "claude-deployment-residue", v: 29, buildId: WEB_BUILD.build_id,
        revision: "source-r1", generation: "old-wrapper", lastSeq: 3905,
        savedAt: Date.now(), turns: [{
          id: "replayed-envelope", prompt: "", done: true, interrupted: true,
          error: "会话已结束，但未收到完整的终止状态。",
          terminalSource: "idle_history_recovery", blocks: [],
        }],
      }, "claude-deployment-residue");
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error);
    });
    database.close();
    const session = await cache.loadSession("claude-deployment-residue");
    const replay = await cache.loadAllReplayState();
    cache.saveSession("claude-deployment-residue", [{
      id: "native-human", prompt: "run task", done: true,
      ts: 1_000, doneTs: 9_000, blocks: [{
        kind: "text", message_id: "native-answer", text: "Finished.",
        channel: "final", done: true,
      }],
    }], 42, "source-r1", "new-wrapper");
    return { session, cursor: replay.cursors["claude-deployment-residue"] };
  });
  expect(ignored.session).toBeNull();
  expect(ignored.cursor).toBeUndefined();
  await expect.poll(() => page.evaluate(async () => {
    const cache = await import("/src/cache.ts");
    return (await cache.loadSession("claude-deployment-residue"))?.turns[0].id;
  })).toBe("native-human");
  await page.reload();
  const restored = await page.evaluate(async () => {
    const cache = await import("/src/cache.ts");
    return cache.loadSession("claude-deployment-residue");
  });
  expect(restored?.generation).toBe("new-wrapper");
  expect(restored?.turns).toHaveLength(1);
  expect(restored?.turns[0].id).toBe("native-human");
  expect(restored?.turns[0].error).toBeUndefined();
  expect(restored?.turns[0].doneTs).toBe(9_000);
});

test("history page cache rebuilds a v1 record in real IndexedDB", async ({
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
      legacyMiss: upgraded === null,
      stored,
      mergedIds: merged?.turns.map((turn: { id: string }) => turn.id),
      invalidated,
      afterInvalidation,
    };
  });
  expect(result.legacyMiss).toBe(true);
  expect(result.stored.ok).toBe(true);
  expect(result.mergedIds).toEqual(["new-turn"]);
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

test("instant session cache persists a bounded history-start proof only", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html");
  const result = await page.evaluate(async () => {
    const cache = await import("/src/cache.ts");
    await cache.clearCache();
    const turn = (id: string) => ({
      id, prompt: id, blocks: [], done: true,
    });
    cache.saveSession(
      "complete-history-head",
      [turn("complete-turn")],
      0,
      "complete-history-r1",
      "complete-history-g1",
      null,
      true,
    );
    cache.saveSession(
      "trimmed-history-head",
      Array.from({ length: 101 }, (_, index) => turn(`trimmed-${index}`)),
      0,
      "trimmed-history-r1",
      "trimmed-history-g1",
      null,
      true,
    );
    await new Promise((resolve) => window.setTimeout(resolve, 700));
    const complete = await cache.loadSession("complete-history-head");
    const trimmed = await cache.loadSession("trimmed-history-head");
    return {
      completeAtStart: complete?.historyAtStart,
      trimmedAtStart: trimmed?.historyAtStart,
      trimmedTurns: trimmed?.turns.length,
      pagingKeys: complete == null
        ? []
        : ["hasMore", "oldestId", "cursor"].filter((key) => (
            Object.prototype.hasOwnProperty.call(complete, key)
          )),
    };
  });
  expect(result.completeAtStart).toBe(true);
  expect(result.trimmedAtStart).toBe(false);
  expect(result.trimmedTurns).toBe(100);
  expect(result.pagingKeys).toEqual([]);
});

test("session cache rejects stale Claude and replay-orphan rows", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html");
  const result = await page.evaluate(async () => {
    const cache = await import("/src/cache.ts");
    const { WEB_BUILD } = await import("/src/web-build.ts");
    await cache.clearCache();
    const legacySid = "legacy-claude-prompt-alias";
    const replayOrphanSid = "completed-replay-orphan";
    const activeCompactionOrphanSid = "active-compaction-replay-orphan";
    const recoveredOwnerV16Sid = "completed-recovery-owner-v16";
    const lateSeedV17Sid = "active-late-binding-seed-v17";
    const pollutedAliasV20Sid = "claude-interrupt-alias-v20";
    const missingAnswerV25Sid = "async-question-missing-answer-v25";
    const optimisticSteerSid = "healthy-optimistic-steer";
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open("cc_remote_cache", 1);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    await new Promise<void>((resolve, reject) => {
      const tx = database.transaction("sessions", "readwrite");
      tx.objectStore("sessions").put({
        v: 10,
        turns: [{
          id: "claude-transcript-uuid",
          prompt: "legacy prompt",
          blocks: [],
          done: false,
        }],
        lastSeq: 41,
        revision: "legacy-r1",
        generation: "legacy-g1",
        savedAt: Date.now(),
      }, legacySid);
      tx.objectStore("sessions").put({
        v: 13,
        turns: [{
          id: "native-history-turn",
          prompt: "deploy",
          blocks: [],
          done: true,
        }, {
          id: "replayed-assistant-message",
          prompt: "",
          blocks: [{
            kind: "tool",
            message_id: "replayed-assistant-message",
            tool_use_id: "replayed-tool-a",
            tool: "Command",
            input: {},
            done: true,
          }, {
            kind: "tool",
            message_id: "replayed-assistant-message",
            tool_use_id: "replayed-tool-b",
            tool: "Command",
            input: {},
            done: true,
          }],
          done: true,
        }],
        lastSeq: 43,
        revision: "replay-orphan-r1",
        generation: "replay-orphan-g1",
        savedAt: Date.now(),
      }, replayOrphanSid);
      tx.objectStore("sessions").put({
        v: 16,
        turns: [{
          id: "browser-owner",
          clientMsgId: "browser-owner",
          forkPointId: "native-owner",
          prompt: "deploy",
          blocks: [],
          done: true,
        }, {
          id: "recovered-tail",
          prompt: "",
          blocks: [{
            kind: "text",
            message_id: "recovered-answer",
            text: "done",
            channel: "final",
            done: true,
          }],
          done: true,
        }],
        lastSeq: 365,
        revision: "recovered-owner-r1",
        generation: "recovered-owner-g1",
        savedAt: Date.now(),
      }, recoveredOwnerV16Sid);
      tx.objectStore("sessions").put({
        v: 15,
        turns: [{
          id: "item-51",
          prompt: "continue the task",
          forkPointId: "native-turn",
          blocks: [{
            kind: "process",
            item_id: "item-54",
            processKind: "compaction",
            phase: "snapshot",
            status: "succeeded",
            turn_id: "native-turn",
            title: "压缩上下文",
            done: true,
          }],
          done: false,
        }, {
          id: "msg-after-compact",
          prompt: "",
          blocks: [{
            kind: "text",
            message_id: "msg-after-compact",
            text: "continued output",
            channel: "commentary",
            done: true,
          }, {
            kind: "process",
            item_id: "replayed-compaction",
            processKind: "compaction",
            phase: "snapshot",
            status: "succeeded",
            turn_id: "native-turn",
            title: "压缩上下文",
            done: true,
          }],
          done: false,
        }],
        lastSeq: 364,
        revision: "active-compaction-r1",
        generation: "active-compaction-g1",
        savedAt: Date.now(),
      }, activeCompactionOrphanSid);
      tx.objectStore("sessions").put({
        v: 17,
        turns: [{
          id: "canonical-current-owner",
          clientMsgId: "canonical-current-owner",
          forkPointId: "shared-current-native-turn",
          prompt: "current prompt",
          blocks: [],
          done: false,
        }, {
          id: "late-seeded-live-tail",
          prompt: "",
          blocks: [{
            kind: "text",
            message_id: "late-seeded-live-tail",
            text: "duplicated current suffix",
            channel: "commentary",
            done: false,
          }],
          done: false,
        }],
        lastSeq: 46,
        revision: "late-seed-r1",
        generation: "late-seed-g1",
        savedAt: Date.now(),
      }, lateSeedV17Sid);
      tx.objectStore("sessions").put({
        v: 20,
        turns: [{
          id: "interrupt-marker-uuid",
          clientMsgId: "interrupt-marker-uuid",
          historyTurnId: "native-user-uuid",
          prompt: "continue after interrupt",
          blocks: [],
          done: false,
        }],
        lastSeq: 46,
        revision: "interrupt-alias-r1",
        generation: "interrupt-alias-g1",
        savedAt: Date.now(),
      }, pollutedAliasV20Sid);
      tx.objectStore("sessions").put({
        v: 25,
        turns: [{
          id: "async-user", prompt: "test", done: true,
          blocks: [{ kind: "text", message_id: "question", text: "Question?",
            channel: "final", done: true, delivery: "async",
            questions: [{ title: "Question?" }] }],
        }],
        lastSeq: 48,
        revision: "unchanged-source",
        generation: "old-generation",
        savedAt: Date.now(),
      }, missingAnswerV25Sid);
      tx.objectStore("sessions").put({
        v: 31, buildId: WEB_BUILD.build_id, machineId: "default", sid: optimisticSteerSid,
        turns: [{
          id: "active-before-steer",
          prompt: "first prompt",
          forkPointId: "shared-native-turn",
          blocks: [],
          done: false,
        }, {
          id: "optimistic-steer",
          clientMsgId: "optimistic-steer",
          prompt: "second prompt",
          blocks: [],
          done: false,
        }],
        lastSeq: 47,
        revision: "optimistic-steer-r1",
        generation: "optimistic-steer-g1",
        savedAt: Date.now(),
      }, cache.sessionCacheKey("default", optimisticSteerSid));
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
    const legacy = await cache.loadSession(legacySid);
    const replayOrphan = await cache.loadSession(replayOrphanSid);
    const recoveredOwnerV16 = await cache.loadSession(recoveredOwnerV16Sid);
    const activeCompactionOrphan = await cache.loadSession(
      activeCompactionOrphanSid);
    const lateSeedV17 = await cache.loadSession(lateSeedV17Sid);
    const pollutedAliasV20 = await cache.loadSession(pollutedAliasV20Sid);
    const missingAnswerV25 = await cache.loadSession(missingAnswerV25Sid);
    const optimisticSteer = await cache.loadSession(optimisticSteerSid);
    const replay = await cache.loadAllReplayState();
    await new Promise((resolve) => window.setTimeout(resolve, 100));
    const prunedCompactionOrphan = await new Promise((resolve, reject) => {
      const tx = database.transaction("sessions", "readonly");
      const request = tx.objectStore("sessions").get(activeCompactionOrphanSid);
      request.onsuccess = () => resolve(request.result ?? null);
      request.onerror = () => reject(request.error);
    });
    cache.saveSession("current-claude-prompt-alias", [{
      id: "browser-prompt-id",
      clientMsgId: "browser-prompt-id",
      historyTurnId: "claude-transcript-uuid",
      prompt: "current prompt",
      blocks: [],
      done: false,
    }], 42, "current-r1", "current-g1");
    await new Promise((resolve) => window.setTimeout(resolve, 700));
    const current = await cache.loadSession("current-claude-prompt-alias");
    database.close();
    return {
      legacy,
      legacyCursor: replay.cursors[legacySid],
      replayOrphan,
      replayOrphanCursor: replay.cursors[replayOrphanSid],
      recoveredOwnerV16,
      recoveredOwnerV16Cursor: replay.cursors[recoveredOwnerV16Sid],
      activeCompactionOrphan,
      activeCompactionCursor: replay.cursors[activeCompactionOrphanSid],
      prunedCompactionOrphan,
      lateSeedV17,
      lateSeedV17Cursor: replay.cursors[lateSeedV17Sid],
      pollutedAliasV20,
      pollutedAliasV20Cursor: replay.cursors[pollutedAliasV20Sid],
      missingAnswerV25,
      missingAnswerV25Cursor: replay.cursors[missingAnswerV25Sid],
      optimisticSteerCount: optimisticSteer?.turns.length,
      optimisticSteerCursor: replay.cursors[optimisticSteerSid],
      currentIds: current?.turns.map((turn: {
        id: string; clientMsgId?: string; historyTurnId?: string;
      }) => [turn.id, turn.clientMsgId, turn.historyTurnId]),
    };
  });
  expect(result.legacy).toBeNull();
  expect(result.legacyCursor).toBeUndefined();
  expect(result.replayOrphan).toBeNull();
  expect(result.replayOrphanCursor).toBeUndefined();
  expect(result.recoveredOwnerV16).toBeNull();
  expect(result.recoveredOwnerV16Cursor).toBeUndefined();
  expect(result.activeCompactionOrphan).toBeNull();
  expect(result.activeCompactionCursor).toBeUndefined();
  expect(result.prunedCompactionOrphan).toBeNull();
  expect(result.lateSeedV17).toBeNull();
  expect(result.lateSeedV17Cursor).toBeUndefined();
  expect(result.pollutedAliasV20).toBeNull();
  expect(result.pollutedAliasV20Cursor).toBeUndefined();
  expect(result.missingAnswerV25).toBeNull();
  expect(result.missingAnswerV25Cursor).toBeUndefined();
  expect(result.optimisticSteerCount).toBe(2);
  expect(result.optimisticSteerCursor).toBe(47);
  expect(result.currentIds).toEqual([[
    "browser-prompt-id", "browser-prompt-id", "claude-transcript-uuid",
  ]]);
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

test("fallback image preview and canonical retry remain independent", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?history-image-fallback-error=1");
  const turn = page.locator('[data-turn-id="history-fallback-error"]');
  const imageRow = turn.locator(".ubub-imgs");
  const preview = turn.getByRole("button", {
    name: "预览用户发送的图片",
  });
  const retry = turn.getByRole("button", { name: "点击重试" });

  await expect(turn).toBeVisible();
  await expect(imageRow).toHaveCount(1);
  await expect(imageRow.locator(":scope > .history-image-control"))
    .toHaveCount(1);
  await expect(turn.locator(".ubub-image-trigger")).toHaveCount(1);
  await expect(preview).toBeVisible();
  await expect(retry).toBeVisible();
  const gap = await turn.evaluate((node) => {
    const row = node.querySelector<HTMLElement>(".ubub-imgs");
    const meta = node.querySelector<HTMLElement>(".ubub-meta");
    if (!row || !meta) throw new Error("canonical image layout is incomplete");
    return meta.getBoundingClientRect().top - row.getBoundingClientRect().bottom;
  });
  expect(gap).toBeLessThan(20);
  await expect(page.getByTestId("history-fallback-loads")).toHaveText("0");

  await preview.click();
  await expect(page.locator(".image-lightbox")).toBeVisible();
  await expect(page.getByTestId("history-fallback-loads")).toHaveText("0");
  await page.locator(".image-lightbox-close").click();
  await expect(page.locator(".image-lightbox")).toHaveCount(0);

  await retry.click();
  await expect(page.getByTestId("history-fallback-loads")).toHaveText("1");
  await expect(page.getByTestId("history-fallback-last-load"))
    .toHaveText("history-fallback-error|history-fallback-image|thumbnail");
  await expect(page.locator(".image-lightbox")).toHaveCount(0);
  await expect(imageRow).toHaveCount(1);
  await expect(turn.locator(".ubub-image-trigger")).toHaveCount(1);
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

test("tool disclosures keep keyboard activation and ignore a scroll drag", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?compact-tools=1");
  await page.locator(".turn-process-head").click();
  const group = page.locator("details.tool-group");
  const groupSummary = group.locator(":scope > summary");

  await groupSummary.focus();
  await page.keyboard.press("Enter");
  await expect(group).toHaveAttribute("open", "");
  const firstTool = group.locator("details.tool").first();
  const firstToolSummary = firstTool.locator(":scope > summary");
  await firstToolSummary.focus();
  await page.keyboard.press("Enter");
  await expect(firstTool).toHaveAttribute("open", "");
  await page.keyboard.press("Enter");
  await expect(firstTool).not.toHaveAttribute("open", "");

  await groupSummary.focus();
  await page.keyboard.press("Enter");
  await expect(group).not.toHaveAttribute("open", "");
  const box = await groupSummary.boundingBox();
  if (!box) throw new Error("tool group summary has no geometry");
  await page.mouse.move(box.x + 20, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + 20, box.y + box.height / 2 + 24, {
    steps: 4,
  });
  await page.mouse.up();
  await expect(group).not.toHaveAttribute("open", "");
});

test("iOS pointercancel releases tool disclosures without toggling", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit pointer cancellation");
  await page.goto("/tests/history-browser.html?compact-tools=1");
  await page.locator(".turn-process-head").click();
  const group = page.locator("details.tool-group");
  const groupSummary = group.locator(":scope > summary");

  await dispatchCancelledTouchTap(groupSummary, 181);
  await expect(group).not.toHaveAttribute("open", "");

  await groupSummary.click();
  await expect(group).toHaveAttribute("open", "");
  const firstTool = group.locator("details.tool").first();
  await dispatchCancelledTouchTap(
    firstTool.locator(":scope > summary"), 182,
  );
  await expect(firstTool).not.toHaveAttribute("open", "");
});

test("Mermaid and history images prepare outside a nested scrollport before becoming visible", async ({ page }) => {
  await page.goto("/tests/history-browser.html?media-layout=1");
  const diagram = page.locator(".mermaid-block");
  const scroller = page.getByTestId("media-scrollport");
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  await expect(page.getByTestId("loaded-media")).toHaveText("near");
  expect(await scroller.evaluate((node) => node.scrollTop)).toBe(0);
  const bounds = await scroller.boundingBox();
  const media = await diagram.boundingBox();
  expect(media!.y).toBeGreaterThan(bounds!.y + bounds!.height);
});

test("Mermaid cached remount paints the final size and isolates duplicate SVG references", async ({ page }) => {
  await page.goto("/tests/history-browser.html?media-layout=1");
  const diagram = page.locator(".mermaid-block");
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  const before = await diagram.boundingBox();
  await page.getByText("Toggle diagram", { exact: true }).click();
  await expect(diagram).toHaveCount(0);
  await page.evaluate(() => {
    (window as unknown as { insertedStates: string[] }).insertedStates = [];
    const observer = new MutationObserver(() => {
      const state = document.querySelector(".mermaid-block")?.getAttribute("data-mermaid-state");
      if (state) (window as unknown as { insertedStates: string[] }).insertedStates.push(state);
    });
    observer.observe(document.querySelector('[data-testid="near-diagram"]')!, { subtree: true, childList: true, attributes: true });
  });
  await page.getByText("Toggle diagram", { exact: true }).click();
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  const states = await page.evaluate(() => (window as unknown as { insertedStates: string[] }).insertedStates);
  expect(states.length).toBeGreaterThan(0);
  expect(states.every((state) => state === "ready")).toBe(true);
  const after = await diagram.boundingBox();
  expect(Math.abs(after!.height - before!.height)).toBeLessThan(1);
  await page.getByText("Duplicate diagram", { exact: true }).click();
  await expect(page.locator('.mermaid-block[data-mermaid-state="ready"]')).toHaveCount(2);
  const references = await page.locator(".mermaid-svg svg").evaluateAll((svgs) => {
    const allIds = svgs.flatMap((svg) => [svg.id, ...Array.from(svg.querySelectorAll("[id]"), (node) => node.id)]);
    const missing = svgs.flatMap((svg) => {
      const ids = new Set([svg.id, ...Array.from(svg.querySelectorAll("[id]"), (node) => node.id)]);
      return [svg, ...svg.querySelectorAll("*")].flatMap((node) => [...node.attributes].flatMap((attribute) =>
        Array.from(attribute.value.matchAll(/url\(#([^)]*)\)/g), (match) => match[1]).filter((id) => !ids.has(id))));
    });
    return { count: allIds.length, unique: new Set(allIds).size, missing };
  });
  expect(references.unique).toBe(references.count);
  expect(references.missing).toEqual([]);
});

test("generated image and Markdown image reserve known geometry throughout decoding", async ({ page }) => {
  await page.goto("/tests/history-browser.html?media-layout=1&geometry=1");
  const placeholder = page.locator(".generated-image-frame .process-image-placeholder");
  await expect(placeholder).toHaveText("正在加载图片…");
  await expect(placeholder.locator("svg")).toHaveCount(0);
  const loadingTextPosition = await placeholder.evaluate(node => {
    const frame = node.getBoundingClientRect();
    const text = node.querySelector("span")!.getBoundingClientRect();
    return {
      horizontal: Math.abs((text.left + text.right - frame.left - frame.right) / 2),
      vertical: Math.abs((text.top + text.bottom - frame.top - frame.bottom) / 2),
    };
  });
  expect(loadingTextPosition.horizontal).toBeLessThan(1);
  expect(loadingTextPosition.vertical).toBeLessThan(1);
  const measure = () => page.evaluate(() => ({
    frames: [...document.querySelectorAll(".message-image-frame,.generated-image-frame")].map((node) => node.getBoundingClientRect().height),
    text: document.querySelector('[data-testid="below-images"]')!.getBoundingClientRect().top,
  }));
  const before = await measure();
  expect(before.frames).toHaveLength(2);
  await page.getByText("Resolve images", { exact: true }).click();
  await expect(page.locator(".message-inline-image,.generated-image-frame img")).toHaveCount(2);
  await expect.poll(() => page.locator(".message-inline-image").evaluate((node: HTMLImageElement) => node.complete)).toBe(true);
  const after = await measure();
  expect(Math.abs(after.text - before.text)).toBeLessThan(1);
  expect(after.frames).toEqual(before.frames);
});

test("Mermaid reading anchor survives source replacement without cancelling native scroll movement", async ({ page }) => {
  await page.goto("/tests/history-browser.html?media-layout=1&geometry=1");
  const result = await page.evaluate(async () => {
    const modulePath = "/src/reading-position.ts";
    const { captureReadingPosition, readingPositionDelta } = await import(modulePath);
    const viewport = document.createElement("div");
    viewport.style.cssText = "position:fixed;inset:0 auto auto 0;width:300px;height:160px;overflow:auto;overflow-anchor:none";
    viewport.innerHTML = '<div data-turn-id="media"><div style="height:300px"></div>'
      + '<div class="prose"><div class="mermaid-block" style="margin:0;border:0">'
      + '<pre style="height:400px;margin:0">diagram source</pre></div></div>'
      + '<div style="height:1000px"></div></div>';
    document.body.append(viewport);
    viewport.scrollTop = 350;
    const frame = viewport.querySelector(".mermaid-block")!;
    const before = frame.getBoundingClientRect().top;
    const position = captureReadingPosition(viewport, "media-test");
    // One layout mutation above the media and replacement of its source node
    // arrive between native wheel motion and the corresponding scroll event.
    const growth = document.createElement("div");
    growth.style.height = "60px";
    frame.before(growth);
    frame.innerHTML = '<button style="display:block;height:300px">rendered diagram</button>';
    viewport.scrollTop -= 25;
    const delta = readingPositionDelta(viewport, position);
    if (delta != null) viewport.scrollTop += delta;
    const after = frame.getBoundingClientRect().top;
    viewport.remove();
    return { delta, motion: after - before };
  });
  expect(result.delta).toBe(60);
  expect(result.motion).toBe(25);
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

test("a completed streaming formula renders while following the live tail", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?streaming-math=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await waitForScrollIdle(page);
  await expect(page.locator('[data-turn-id="streaming-math"] .katex'))
    .toHaveCount(0);

  await page.getByTestId("close-streaming-formula").evaluate(
    (button: HTMLButtonElement) => button.click(),
  );
  await expect(page.locator('[data-turn-id="streaming-math"] .katex'))
    .toHaveCount(1);
  await expect.poll(async () => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);
});

test("streaming formula closure preserves a non-bottom reading anchor", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?streaming-math=1");
  await wheelUntilTurn(
    page, "math-before-4", -1_200, testInfo.project.name,
  );
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await page.getByTestId("close-streaming-formula").evaluate(
    (button: HTMLButtonElement) => button.click(),
  );
  await page.waitForTimeout(150);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("a completed Mermaid diagram opens the shared pinch-zoom preview", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/tests/history-browser.html?mermaid=1");
  const diagram = page.locator(".mermaid-block").first();
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  await applyProductionCsp(page);
  const inlineSurface = await diagram.locator(".mermaid-svg").evaluate(
    (node) => getComputedStyle(node).backgroundColor,
  );

  await diagram.locator(".mermaid-zoom").click();
  const preview = page.getByRole("dialog", { name: "Mermaid 图表预览" });
  await expect(preview).toBeVisible();
  const vector = preview.locator(".image-lightbox-vector > svg");
  await expect(vector).toBeVisible();
  await expect(preview.locator("img")).toHaveCount(0);
  const vectorSurface = preview.locator(".image-lightbox-vector");
  await expect.poll(() => vectorSurface.evaluate(
    (node) => getComputedStyle(node).backgroundColor,
  )).toBe(inlineSurface);
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

test("a dark Mermaid preview keeps the render theme surface", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?mermaid=1");
  await page.evaluate(() => {
    document.documentElement.dataset.theme = "dark";
  });
  const diagram = page.locator(".mermaid-block").first();
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  const inlineSurface = await diagram.locator(".mermaid-svg").evaluate(
    (node) => getComputedStyle(node).backgroundColor,
  );
  await diagram.locator(".mermaid-zoom").click();

  const preview = page.getByRole("dialog", { name: "Mermaid 图表预览" });
  const vectorSurface = preview.locator(".image-lightbox-vector");
  await expect.poll(() => vectorSurface.evaluate(
    (node) => getComputedStyle(node).backgroundColor,
  )).toBe(inlineSurface);
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
  const rasterSurface = page.locator(
    ".image-lightbox img.image-lightbox-image",
  );
  await expect(rasterSurface).toBeVisible();
  const rasterBackground = await rasterSurface.evaluate((node) => {
    const style = getComputedStyle(node);
    return {
      color: style.backgroundColor,
      image: style.backgroundImage,
    };
  });
  expect(rasterBackground.color).toBe("rgb(248, 249, 251)");
  expect(rasterBackground.image).not.toBe("none");
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

test("a page waits through post-touch momentum and restores its final boundary", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit touch settlement");
  await page.goto("/tests/history-browser.html?delay=5&manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });

  await dispatchTouchPhase(page, "touchstart", 160);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await page.waitForTimeout(50);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
  // The network page may remain ready for multiple frames, but it cannot
  // rebase the still-active gesture onto a not-yet-mounted older row.
  await page.waitForTimeout(100);
  await dispatchTouchPhase(page, "touchend", 220);
  // touchend does not end iOS scroll ownership: native momentum continues to
  // emit scroll events without a finger. Keep the page staged, allow those
  // movements and unrelated row growth, then commit at the final idle point.
  await page.waitForTimeout(60);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
  let momentumScrollTop = 0;
  for (const top of [240, 420, 540]) {
    momentumScrollTop = await viewport.evaluate((node, scrollTop) => {
      node.scrollTop = scrollTop;
      node.dispatchEvent(new Event("scroll"));
      return node.scrollTop;
    }, top);
    await page.waitForTimeout(50);
  }
  // Capture the final momentum boundary before any slow-runner command can
  // legitimately cross the 260 ms idle lease and mount the staged page.
  const momentumEnd = await readingAnchor(page);
  expect(momentumScrollTop).toBeGreaterThan(200);

  await page.getByTestId("grow-row").click();
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await expect.poll(async () => (await readingAnchor(page)).id)
    .toBe(momentumEnd.id);
  await page.waitForTimeout(350);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(momentumEnd.id);
  expect(Math.abs(settled.offset - momentumEnd.offset)).toBeLessThan(2);
});

test("a delayed pre-commit touchmove never rebases a retained page", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit touch settlement");
  await page.goto("/tests/history-browser.html?delay=5&manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const before = await readingAnchor(page);

  // Create the reverse move before the page request. WebKit may queue that
  // native event and deliver it only after React commits the prepend.
  await stageDelayedTouchMove(page, 160, 80);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await page.waitForTimeout(50);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
  await dispatchDelayedTouchMove(page);
  await viewport.evaluate((node) => {
    node.scrollTop = 400;
    node.dispatchEvent(new Event("scroll"));
    node.scrollTop = 0;
    node.dispatchEvent(new Event("scroll"));
  });
  await dispatchTouchPhase(page, "touchend", 80);
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();

  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
});

test("a cached-newer page that finishes under touch keeps its retained row", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit touch settlement");
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
  await page.waitForTimeout(350);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(before.id);
  expect(Math.abs(settled.offset - before.offset)).toBeLessThan(2);
});

test("movement while a page is staged becomes the release boundary", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit touch settlement");
  await page.goto("/tests/history-browser.html?delay=5&manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const original = await readingAnchor(page);

  await dispatchTouchPhase(page, "touchstart", 160);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await page.waitForTimeout(50);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);

  // The response is staged, while the same finger deliberately reverses
  // toward newer content before it is lifted. That real position becomes the
  // keyed anchor used when the page finally mounts.
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
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();

  await expect.poll(async () => (await readingAnchor(page)).id).toBe(moved.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - moved.offset),
  ).toBeLessThan(2);
  await page.waitForTimeout(300);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(moved.id);
  expect(Math.abs(settled.offset - moved.offset)).toBeLessThan(2);
});

test("continuing to pull at the top keeps the staged page invisible", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit touch settlement");
  await page.goto("/tests/history-browser.html?delay=5&manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const original = await readingAnchor(page);

  await dispatchTouchPhase(page, "touchstart", 160);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await page.waitForTimeout(50);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);

  // The same finger keeps pulling into the top edge. The old page cannot move
  // the DOM out from under that gesture; it mounts once, after release.
  await dispatchTouchPhase(page, "touchmove", 280);
  await viewport.evaluate((node) => { node.scrollBy({ top: -720 }); });
  await waitForScrollIdle(page);
  const moved = await readingAnchor(page);
  expect(moved.id).toBe(original.id);
  await dispatchTouchPhase(page, "touchend", 280);
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();

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
    "/tests/history-browser.html?runtime-browse=1&delay=350&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await page.locator(".load-more-btn").dispatchEvent("click");
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.getByTestId("history-page-activity")).toBeVisible();
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
});

test("a script-only bottom write cannot leave history browse", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?runtime-browse=1&delay=5&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await page.locator(".load-more-btn").dispatchEvent("click");
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await expect(page.getByRole("button", { name: "回到最新" })).toBeVisible();
  await waitForScrollIdle(page);

  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await page.waitForTimeout(100);

  await expect(page.getByRole("button", { name: "回到最新" })).toBeVisible();
});

test("dragging the deep-history scrollbar to the bottom returns to the live tail", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?deep-browse=1&delay=5");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await waitForScrollIdle(page);
  await expect(page.getByRole("button", { name: "回到最新" })).toBeVisible();
  await expect(page.getByTestId("newest-turn-id")).toHaveText("m20");

  const gutterPoint = await viewport.evaluate((node) => {
    const rect = node.getBoundingClientRect();
    return { x: rect.right - 2, y: rect.top + 24 };
  });

  await viewport.dispatchEvent("pointerdown", {
    pointerType: "mouse", button: 0, buttons: 1, isPrimary: true,
    pointerId: 71, clientX: gutterPoint.x, clientY: gutterPoint.y,
  });
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });

  await expect(page.getByRole("button", { name: "回到最新" })).toHaveCount(0);
  await expect(page.getByTestId("newer-load-count")).toHaveText("0");
  await expect(page.getByTestId("newest-turn-id")).toHaveText("m40");
  const completedTail = page.locator('[data-turn-id="m40"]');
  await expect(completedTail.locator(".turn-done-mark")).toBeVisible();
  await expect(completedTail.locator(".ai-meta .ubub-time")).toBeVisible();
  await expect(completedTail.locator(
    '.ai-meta .ubub-act[aria-label="复制"]',
  )).toBeVisible();
});

test("dark chat threads expose a contrasting native scrollbar", async ({ page }) => {
  await page.goto("/tests/history-browser.html?large=40");
  await page.evaluate(() => {
    document.documentElement.dataset.theme = "dark";
  });
  const style = await page.locator(".thread").evaluate((node) => {
    const computed = getComputedStyle(node);
    return {
      colorScheme: computed.colorScheme,
      // WebKit applies scrollbar-color/color-scheme but does not expose the
      // non-standard scrollbarColor DOM property through getComputedStyle.
      scrollbarColor: computed.getPropertyValue("scrollbar-color").trim(),
    };
  });
  expect(style.colorScheme).toBe("dark");
  if (style.scrollbarColor) {
    expect(style.scrollbarColor).not.toBe("auto");
    expect(style.scrollbarColor)
      .not.toMatch(/^transparent(?:\s+transparent)?$/);
  }
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

  const returnButton = page.getByRole("button", { name: "回到最新" });
  await expect(returnButton).toHaveText("");
  const buttonBox = await returnButton.boundingBox();
  expect(buttonBox).not.toBeNull();
  expect(Math.abs((buttonBox?.width ?? 0) - (buttonBox?.height ?? 0)))
    .toBeLessThan(1);
  await returnButton.click();
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
  await expect(page.getByTestId("history-page-activity")).toBeVisible();
  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
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
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
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
  const before = await readingAnchor(page);
  const sampled = await maxPaintedTurnOffsetShiftThroughAction(
    page,
    before.id,
    "grow-row",
  );
  await expect(page.locator('[data-turn-id="n8"] p')).toHaveCount(28);
  await waitForScrollIdle(page);
  const after = await readingAnchor(page);
  expect(sampled.missing).toBe(false);
  expect(sampled.maxShift).toBeLessThan(2);
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

test("a generation change cancels the old page anchor and permits a new request", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?generation-shift=1&delay=1000&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await page.locator(".load-more-btn").dispatchEvent("click");
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.getByTestId("history-page-activity")).toBeVisible();

  await page.getByTestId("shift-generation").click();
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
  await page.locator(".load-more-btn").dispatchEvent("click");
  await expect(page.getByTestId("load-count")).toHaveText("2");
});

test("a generation change clears the automatic keyboard paging boundary", async ({
  page,
}, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name),
    "desktop keyboard path");
  await page.goto(
    "/tests/history-browser.html?generation-shift=1&delay=10000&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.focus();
  await viewport.press("Home");
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.getByTestId("history-page-activity")).toBeVisible();

  await page.getByTestId("shift-generation").click();
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
  const keyboardBaseline = await viewport.evaluate((node) => {
    // Chromium may expose the End position before delivering its scroll event,
    // then coalesce that event with the following Home movement. Reproduce the
    // ordering deterministically: keydown sees the physical bottom position,
    // while the only delivered scroll event observes the final top position.
    node.scrollTop = node.scrollHeight;
    const bottom = node.scrollTop;
    node.dispatchEvent(new KeyboardEvent("keydown", {
      bubbles: true,
      key: "Home",
    }));
    node.scrollTop = 0;
    node.dispatchEvent(new Event("scroll"));
    return bottom;
  });
  expect(keyboardBaseline).toBeGreaterThan(0);
  await expect(page.getByTestId("load-count")).toHaveText("2");
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

test("pending history handoffs retain rows only within one authority", async ({
  page,
}, testInfo) => {
  await page.goto(
    "/tests/history-browser.html?large=40&pending-revision-replace=1",
  );
  await expect(page.locator('[data-turn-id="m40"]')).toBeVisible();
  await wheelUntilTurn(page, "m10", -400, testInfo.project.name);
  await waitForReadingPositionIdle(page);
  const before = await readingAnchor(page);

  await page.getByTestId("replace-revision").click();
  await expect(page.getByTestId("history-transition-state"))
    .toHaveText("pending");
  const samples = await page.evaluate(async () => {
    const readings: Array<{
      count: number;
      id: string | null;
      offset: number | null;
      scrollTop: number;
    }> = [];
    const deadline = performance.now() + 100;
    while (performance.now() < deadline) {
      await new Promise<void>((resolveFrame) =>
        requestAnimationFrame(() => resolveFrame()));
      const viewport = document.querySelector<HTMLElement>(".thread");
      if (!viewport) throw new Error("thread viewport is missing");
      const viewportRect = viewport.getBoundingClientRect();
      const rows = [...document.querySelectorAll<HTMLElement>("[data-turn-id]")]
        .map((row) => ({ row, rect: row.getBoundingClientRect() }))
        .filter(({ rect }) =>
          rect.bottom > viewportRect.top && rect.top < viewportRect.bottom)
        .sort((left, right) =>
          Math.abs(left.rect.top - viewportRect.top)
          - Math.abs(right.rect.top - viewportRect.top));
      readings.push({
        count: document.querySelectorAll("[data-turn-id]").length,
        id: rows[0]?.row.dataset.turnId ?? null,
        offset: rows[0] ? rows[0].rect.top - viewportRect.top : null,
        scrollTop: viewport.scrollTop,
      });
    }
    return readings;
  });
  expect(samples.length).toBeGreaterThan(2);
  expect(samples.every((sample) => sample.count > 0)).toBe(true);
  expect(samples.every((sample) => sample.id === before.id)).toBe(true);
  expect(samples.every((sample) => sample.offset != null
    && Math.abs(sample.offset - before.offset) < 2)).toBe(true);
  expect(samples.every((sample) => sample.scrollTop > 1)).toBe(true);

  await expect(page.getByTestId("history-transition-state"))
    .toHaveText("ready");
  await expect(page.locator('[data-turn-id="m10"]')).toHaveCount(0);
  await expect(page.locator('[data-turn-id="r24"]')).toBeVisible();

  await page.getByTestId("replace-authority").click();
  await expect(page.getByTestId("session-authority-scope"))
    .toHaveText("fixture-authority-b");
  expect(await page.locator('[data-turn-id="r24"]').count()).toBe(0);

  await expect(page.getByTestId("history-transition-state"))
    .toHaveText("ready");
  await expect(page.locator('[data-turn-id="r24"]')).toBeVisible();
});

test("replay recovery replacement preserves the current reading row", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?large=40&recovery-replace=1");
  await expect(page.locator('[data-turn-id="m40"]')).toBeVisible();
  await wheelUntilTurn(page, "m10", -400, testInfo.project.name);
  await waitForReadingPositionIdle(page);
  const before = await readingAnchor(page);

  await page.getByTestId("replace-revision").click();
  await expect(page.locator('[data-turn-id="m10"] p')).toHaveCount(4);
  await waitForReadingPositionIdle(page);
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

test("plan progress uses a compact popover that closes outside", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?timeline=1");
  await scrollThreadToEdge(page, "start", testInfo.project.name);
  const timeline = page.locator('[data-turn-id="timeline"]');
  const trigger = timeline.getByRole("button", { name: /查看计划进度/ });
  await expect(trigger).toBeVisible();
  await trigger.click();

  const popover = page.getByRole("dialog", { name: "计划进度" });
  await expect(popover).toBeVisible();
  await expect(popover).toContainText("1 / 3");
  await expect(popover).toContainText("验证计划弹层");

  const box = await popover.boundingBox();
  const viewport = page.viewportSize();
  const anchorBox = await trigger.boundingBox();
  if (!box || !viewport || !anchorBox) {
    throw new Error("plan popover has no geometry");
  }
  expect(box.x).toBeGreaterThanOrEqual(8);
  expect(box.x + box.width).toBeLessThanOrEqual(viewport.width - 8);
  expect(box.y).toBeGreaterThanOrEqual(8);
  expect(box.y + box.height).toBeLessThanOrEqual(viewport.height - 8);
  const expectedCenter = Math.min(
    Math.max(anchorBox.x + anchorBox.width / 2, 16 + box.width / 2),
    viewport.width - 16 - box.width / 2,
  );
  expect(Math.abs(box.x + box.width / 2 - expectedCenter)).toBeLessThan(2);
  expect(Math.abs(box.y + box.height - (anchorBox.y - 8)))
    .toBeLessThan(2);

  await trigger.click();
  await expect(popover).toHaveCount(0);
  await trigger.click();
  await expect(popover).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(popover).toHaveCount(0);
  await trigger.click();
  await expect(popover).toBeVisible();

  await page.getByTestId("load-count").click();
  await expect(popover).toHaveCount(0);
});

test("historical Plan flips below a trigger near the top edge", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?timeline=1");
  await scrollThreadToEdge(page, "start", testInfo.project.name);
  const trigger = page.locator('[data-turn-id="timeline"]')
    .getByRole("button", { name: /查看计划进度/ });
  await expect(trigger).toBeVisible();
  if (isMobileWebKitProject(testInfo.project.name)) {
    await dispatchTouchGesture(page, -60);
  } else {
    await page.locator(".thread").dispatchEvent("wheel", { deltaY: 80 });
  }
  await trigger.evaluate((node) => {
    const viewport = node.closest<HTMLElement>(".thread");
    if (!viewport) throw new Error("historical Plan has no thread viewport");
    const viewportBox = viewport.getBoundingClientRect();
    const triggerBox = node.getBoundingClientRect();
    viewport.scrollTop += triggerBox.y - (viewportBox.y + 20);
  });
  await expect.poll(() => trigger.evaluate((node) => {
    const viewport = node.closest<HTMLElement>(".thread");
    if (!viewport) return false;
    const viewportBox = viewport.getBoundingClientRect();
    const triggerBox = node.getBoundingClientRect();
    const relativeTop = triggerBox.y - viewportBox.y;
    const settled = relativeTop >= 16 && relativeTop < 24;
    if (!settled) {
      viewport.scrollTop += relativeTop - 20;
    }
    return settled && triggerBox.bottom <= viewportBox.bottom;
  })).toBe(true);

  await trigger.click();
  const popover = page.getByRole("dialog", { name: "计划进度" });
  await expect(popover).toBeVisible();
  await expect(popover).toHaveAttribute("data-placement", "below");
  const { threadBox, anchorBox, popoverBox } = await trigger.evaluate((node) => {
    const viewport = node.closest<HTMLElement>(".thread");
    const dialog = document.querySelector<HTMLElement>(
      '[role="dialog"][aria-label="计划进度"]',
    );
    if (!viewport || !dialog) throw new Error("near-top Plan has no geometry");
    const bounds = (element: Element) => {
      const box = element.getBoundingClientRect();
      return {
        x: box.x,
        y: box.y,
        width: box.width,
        height: box.height,
      };
    };
    return {
      threadBox: bounds(viewport),
      anchorBox: bounds(node),
      popoverBox: bounds(dialog),
    };
  });
  expect(Math.abs(popoverBox.y - (anchorBox.y + anchorBox.height + 8)))
    .toBeLessThan(2);
  expect(popoverBox.height).toBeGreaterThan(64);
  expect(popoverBox.y + popoverBox.height)
    .toBeLessThanOrEqual(threadBox.y + threadBox.height - 15);
});

test("a long active turn keeps its plan beside the composer", async ({ page }) => {
  await page.goto("/tests/history-browser.html?persistent-plan=1");
  const thread = page.locator(".thread");
  await thread.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await expect.poll(() => thread.evaluate((node) => node.scrollTop))
    .toBeGreaterThan(200);
  await expect(page.locator('[data-turn-id="persistent-plan"]')
    .getByRole("button", { name: /查看计划进度/ })).toHaveCount(0);

  const chip = page.getByRole("button", { name: /查看计划进度/ });
  await expect(chip).toBeVisible();
  await chip.click();
  await expect(page.getByRole("dialog", { name: "计划进度" }))
    .toContainText("验证计划弹层");
});

test("an old terminal plan stays with its historical turn", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?historical-plan=1");

  await scrollThreadToEdge(page, "start", testInfo.project.name);
  const planTurn = page.locator('[data-turn-id="historical-plan"]');
  await expect(planTurn).toBeVisible();
  const trigger = planTurn.getByRole("button", { name: /查看计划进度/ });
  await expect(trigger).toBeVisible();
  await expect(page.getByRole("button", { name: /查看计划进度/ }))
    .toHaveCount(1);

  await trigger.click();
  await expect(page.getByRole("dialog", { name: "计划进度" }))
    .toContainText("验证计划弹层");
});

test("iOS pointercancel releases the plan trigger without opening it", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit pointer cancellation");
  await page.goto("/tests/history-browser.html?interactive-timeline=1");
  const trigger = page.getByRole("button", { name: /查看计划进度/ });
  await expect(trigger).toBeVisible();

  await dispatchCancelledTouchTap(trigger, 183);

  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("dialog", { name: "计划进度" }))
    .toHaveCount(0);
});

test("terminal turn does not mark unfinished structured plan complete", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?plan-ui=terminal");
  await page.getByRole("button", { name: /查看计划进度/ }).click();
  const popover = page.getByRole("dialog", { name: "计划进度" });
  await expect(popover).toContainText("1 / 3");
  await expect(popover).toContainText("本轮已结束，计划未更新");
  await expect(popover).not.toContainText("全部完成");
});

test("unstructured plan detail remains visible in the compact popover", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?plan-ui=unstructured");
  await page.getByRole("button", { name: /查看计划进度/ }).click();
  const popover = page.getByRole("dialog", { name: "计划进度" });
  await expect(popover).toContainText("已记录");
  await expect(popover).not.toContainText("全部完成");
  await expect(popover).toContainText("先检查协议，再验证移动端，最后发布。");
});

test("opening a cached plan refreshes authoritative detail", async ({ page }) => {
  await page.goto("/tests/history-browser.html?plan-ui=refresh");
  await page.getByRole("button", { name: /查看计划进度/ }).click();
  await expect(page.getByTestId("plan-detail-requests")).toHaveText("1");
  const popover = page.getByRole("dialog", { name: "计划进度" });
  await expect(page.getByTestId("plan-refresh-state")).toHaveText("loading");
  await expect(popover).toBeVisible();
  await expect(popover).toContainText("缓存步骤二");
  await expect(page.getByTestId("plan-refresh-state")).toHaveText("ready");
  await expect(popover).toContainText("权威步骤二");
  await expect(popover).not.toContainText("缓存步骤二");
});

test("only the selected plan block moves into the compact popover", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?plan-ui=mixed");
  await page.locator(".turn-process-head").click();
  await expect(page.getByText("旧版计划", { exact: true })).toBeVisible();
});

test("a turn plan without a Goal stays in a compact session-level strip", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=none&plan=1");
  const chip = page.getByRole("button", { name: /查看计划进度/ });
  await expect(chip).toBeVisible();
  await expect(chip).toContainText("实现固定入口");
  const box = await chip.boundingBox();
  if (!box) throw new Error("plan strip has no geometry");
  expect(box.height).toBeLessThanOrEqual(42);

  await chip.click();
  await expect(page.getByTestId("plan-detail-requests")).toHaveText("1");
  const dialog = page.getByRole("dialog", { name: "计划进度" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("1 / 3");
  await expect(dialog).toContainText("完成浏览器回归");
  await expect(page.locator(".scrim.show")).toHaveCount(0);
  await expect(page.locator(".plan-sheet")).toHaveCount(0);
  const dialogBox = await dialog.boundingBox();
  if (!dialogBox) throw new Error("plan popover has no geometry");
  expect(dialogBox.width).toBeLessThan(361);
  const anchorBox = await chip.boundingBox();
  if (!anchorBox) throw new Error("plan popover has no anchor");
  expect(Math.abs(
    dialogBox.x + dialogBox.width / 2
      - (anchorBox.x + anchorBox.width / 2),
  )).toBeLessThan(2);
  expect(Math.abs(
    dialogBox.y + dialogBox.height - (anchorBox.y - 8),
  )).toBeLessThan(2);
  const openChipBox = await chip.boundingBox();
  if (!openChipBox) throw new Error("open plan strip has no geometry");
  expect(Math.abs(openChipBox.x - box.x)).toBeLessThan(1);
  expect(Math.abs(openChipBox.y - box.y)).toBeLessThan(1);

  await page.locator("[data-testid=goal-fixture-content]").click({
    position: { x: 5, y: 5 },
  });
  await expect(dialog).toHaveCount(0);
});

test("standalone Plan closes from outside and fits the mobile viewport", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 720 });
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=none&plan=1");
  await page.getByRole("button", { name: /查看计划进度/ }).click();
  const dialog = page.getByRole("dialog", { name: "计划进度" });
  await expect(dialog).toBeVisible();
  const box = await dialog.boundingBox();
  if (!box) throw new Error("mobile plan has no geometry");
  const anchor = page.getByRole("button", { name: /查看计划进度/ });
  const anchorBox = await anchor.boundingBox();
  if (!anchorBox) throw new Error("mobile plan has no anchor");
  expect(box.x).toBeGreaterThanOrEqual(11);
  expect(box.x + box.width).toBeLessThanOrEqual(379);
  const visualBottom = await page.evaluate(() => (
    (window.visualViewport?.offsetTop ?? 0)
      + (window.visualViewport?.height ?? window.innerHeight)
  ));
  expect(box.y + box.height).toBeLessThanOrEqual(visualBottom - 11);
  expect(Math.abs(box.y + box.height - (anchorBox.y - 8)))
    .toBeLessThan(2);
  await page.locator("[data-testid=goal-fixture-content]").click({
    position: { x: 5, y: 5 },
  });
  await expect(dialog).toHaveCount(0);
});

test("Plan follows chat and composer geometry changes", async ({ page }) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=none&plan=1");
  await page.getByRole("button", { name: /查看计划进度/ }).click();
  const dialog = page.getByRole("dialog", { name: "计划进度" });
  const chip = page.getByRole("button", { name: /查看计划进度/ });
  const initialBox = await dialog.boundingBox();
  const initialAnchorBox = await chip.boundingBox();
  if (!initialBox || !initialAnchorBox) {
    throw new Error("Plan has no initial geometry");
  }
  await page.getByTestId("goal-fixture-composer").evaluate((node) => {
    node.style.height = "176px";
  });
  await expect.poll(async () => {
    const anchorBox = await chip.boundingBox();
    return anchorBox?.y ?? null;
  }).toBeLessThan(initialAnchorBox.y - 40);
  await expect.poll(async () => {
    const [currentDialog, currentAnchor] = await Promise.all([
      dialog.boundingBox(),
      chip.boundingBox(),
    ]);
    if (!currentDialog || !currentAnchor) return null;
    return Math.abs(
      currentDialog.y + currentDialog.height - (currentAnchor.y - 8),
    );
  }).toBeLessThan(2);
  const [resizedDialogBox, resizedAnchorBox, composerBox] = await Promise.all([
    dialog.boundingBox(),
    chip.boundingBox(),
    page.getByTestId("goal-fixture-composer").boundingBox(),
  ]);
  if (!resizedDialogBox || !resizedAnchorBox || !composerBox) {
    throw new Error("tall-composer Plan fixture has no geometry");
  }
  expect(Math.abs(
    resizedDialogBox.y + resizedDialogBox.height - (resizedAnchorBox.y - 8),
  )).toBeLessThan(2);
  expect(resizedDialogBox.y + resizedDialogBox.height).toBeLessThanOrEqual(
    composerBox.y - 15,
  );

  await page.getByTestId("goal-fixture-spacer").evaluate((node) => {
    node.style.width = "180px";
  });
  await expect.poll(async () => {
    const [currentDialog, currentAnchor] = await Promise.all([
      dialog.boundingBox(),
      chip.boundingBox(),
    ]);
    if (!currentDialog || !currentAnchor) return null;
    return Math.abs(
      currentDialog.x + currentDialog.width / 2
        - (currentAnchor.x + currentAnchor.width / 2),
    );
  }).toBeLessThan(2);
});

test("a long Plan scrolls within the space above its anchor", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 520 });
  await page.goto(
    "/tests/history-browser.html?goal-ui=1&goal-status=none&plan=1&plan-long=1",
  );
  const chip = page.getByRole("button", { name: /查看计划进度/ });
  await chip.click();
  const dialog = page.getByRole("dialog", { name: "计划进度" });
  await expect(dialog).toBeVisible();
  const [box, anchorBox] = await Promise.all([
    dialog.boundingBox(),
    chip.boundingBox(),
  ]);
  if (!box || !anchorBox) throw new Error("long Plan has no geometry");
  expect(Math.abs(box.y + box.height - (anchorBox.y - 8)))
    .toBeLessThan(2);
  expect(box.y).toBeGreaterThanOrEqual(15);
  expect(await dialog.evaluate((node) => ({
    clientHeight: node.clientHeight,
    scrollHeight: node.scrollHeight,
    overflowY: getComputedStyle(node).overflowY,
  }))).toMatchObject({ overflowY: "auto" });
  expect(await dialog.evaluate((node) => node.scrollHeight))
    .toBeGreaterThan(await dialog.evaluate((node) => node.clientHeight));
});

test("a completed session Plan disappears when the next message begins", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?plan-lifecycle=1");
  const chip = page.getByRole("button", { name: /查看计划进度/ });
  await expect(chip).toBeVisible();
  await expect(chip.locator(".plan-chip-ring.complete")).toHaveCount(1);
  await expect(chip).toContainText("2 / 2");

  await page.getByTestId("send-next-plan-message").click();
  await expect(chip).toHaveCount(0);
});

test("an interrupted session Plan disappears when the next message begins", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?plan-lifecycle=interrupted");
  const chip = page.getByRole("button", { name: /查看计划进度/ });
  await expect(chip).toBeVisible();
  await expect(chip.locator(".plan-chip-ring.failed")).toHaveCount(1);
  await expect(chip).toContainText("1 / 2");

  await page.getByTestId("send-next-plan-message").click();
  await expect(chip).toHaveCount(0);
});

test("an existing Goal owns the turn plan in its detail sheet", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&plan=1");
  await expect(page.getByRole("button", { name: /查看计划进度/ }))
    .toHaveCount(0);
  await page.getByRole("button", { name: /查看 Goal/ }).click();
  const dialog = page.getByRole("dialog", { name: "Codex Goal" });
  const planEntry = dialog.getByRole("button", { name: /查看计划进度/ });
  await expect(planEntry).toBeVisible();
  await expect(planEntry).toContainText("实现固定入口");
  await expect(page.getByTestId("plan-detail-requests")).toHaveText("0");
  await planEntry.click();
  await expect(page.getByTestId("plan-detail-requests")).toHaveText("1");
  await expect(dialog.getByLabel("计划执行状态")).toContainText("1 / 3");
  await expect(dialog.getByLabel("计划执行状态"))
    .toContainText("实现固定入口");
});

test("a completed Goal keeps the Plan from its own final turn", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=complete&plan=1");
  await expect(page.getByRole("button", { name: /查看 Goal/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /查看计划进度/ }))
    .toHaveCount(0);

  await page.getByRole("button", { name: /查看 Goal/ }).click();
  await expect(page.getByRole("dialog", { name: "Codex Goal" })
    .getByRole("button", { name: /查看计划进度/ })).toBeVisible();
});

test("a new turn retires its completed Goal and owns a standalone Plan", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?goal-ui=1&goal-status=complete&plan=1&goal-next-turn=1",
  );
  await expect(page.getByRole("button", { name: /查看 Goal/ })).toHaveCount(0);

  const plan = page.getByRole("button", { name: /查看计划进度/ });
  await expect(plan).toBeVisible();
  await expect(plan).toContainText("实现固定入口");
  await plan.click();
  await expect(page.getByRole("dialog", { name: "计划进度" }))
    .toContainText("完成浏览器回归");
  await expect(page.getByRole("dialog", { name: "Codex Goal" })).toHaveCount(0);
});

test("a long Goal keeps its merged plan in the detail sheet first viewport", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&plan=1&goal-long=1");
  await page.getByRole("button", { name: /查看 Goal/ }).click();

  const dialog = page.getByRole("dialog", { name: "Codex Goal" });
  const planEntry = dialog.getByRole("button", { name: /查看计划进度/ });
  await expect(planEntry).toContainText("实现固定入口");
  await expect(planEntry).toBeInViewport();
  await expect(dialog.locator(".goal-sheet-scroll"))
    .toHaveJSProperty("scrollTop", 0);
  await planEntry.click();
  await expect(dialog.getByLabel("计划执行状态"))
    .toContainText("实现固定入口");
});

test("a hidden Goal cannot make the current plan inaccessible", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-hidden=1&plan=1");
  await expect(page.getByRole("button", { name: /查看 Goal/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /查看计划进度/ }))
    .toBeVisible();
});

test("goal entry stays compact and opens its editor", async ({ page }) => {
  await page.goto("/tests/history-browser.html?goal-ui=1");
  const chip = page.getByRole("button", { name: "查看 Goal" });
  await expect(chip).toBeVisible();
  const box = await chip.boundingBox();
  const viewport = page.viewportSize();
  if (!box || !viewport) throw new Error("goal chip has no geometry");
  expect(box.height).toBeLessThanOrEqual(42);
  expect(box.width).toBeLessThan(viewport.width - 20);

  await chip.click();
  const dialog = page.getByRole("dialog", { name: "Codex Goal" });
  await expect(dialog).toBeVisible();
  const dialogBox = await dialog.boundingBox();
  if (!dialogBox || !viewport) throw new Error("goal dialog has no geometry");
  expect(dialogBox.width).toBeLessThanOrEqual(Math.min(580, viewport.width));
  const chatBox = await page.locator(".thread-shell").boundingBox();
  if (!chatBox) throw new Error("goal dialog has no chat viewport");
  expect(Math.abs(
    dialogBox.x + dialogBox.width / 2 - (chatBox.x + chatBox.width / 2),
  )).toBeLessThan(2);
  expect(Math.abs(
    dialogBox.y + dialogBox.height / 2 - (chatBox.y + chatBox.height / 2),
  )).toBeLessThan(2);
  const statCards = dialog.locator(".goal-stats > div");
  await expect(statCards).toHaveCount(3);
  expect(await statCards.first().evaluate((node) =>
    getComputedStyle(node).borderTopWidth)).toBe("0px");
  const icon = dialog.locator(".goal-sheet-icon");
  expect(await icon.evaluate((node) => {
    const style = getComputedStyle(node);
    return style.backgroundColor !== style.color;
  })).toBe(true);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Codex Goal" }))
    .toHaveCount(0);
  await chip.click();
  await expect(page.getByRole("dialog", { name: "Codex Goal" }))
    .toBeVisible();
  await page.locator(".scrim.show").click({ position: { x: 5, y: 5 } });
  await expect(page.getByRole("dialog", { name: "Codex Goal" })).toHaveCount(0);
});

test("remembered goal shows a compact recovery state without opening its editor", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=loading");
  const recovery = page.getByRole("status", { name: "正在恢复 Goal" });
  await expect(recovery).toBeVisible();
  await expect(recovery).toContainText("正在恢复…");
  await expect(page.getByRole("dialog", { name: "Codex Goal" })).toHaveCount(0);
});

test("budgeted goal keeps its blocked status visible on mobile", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=blocked");
  const chip = page.getByRole("button", { name: /查看 Goal/ });
  await expect(chip).toHaveAttribute("aria-label", /受阻/);
  await expect(chip.locator(".goal-chip-ring"))
    .toHaveClass(/goal-chip-ring-blocked/);
});

test("goal editor stays inside the tablet visual viewport above the keyboard", async ({
  page,
}) => {
  await page.setViewportSize({ width: 768, height: 1024 });
  await page.goto("/tests/history-browser.html?goal-ui=1");
  await page.getByRole("button", { name: "查看 Goal" }).click();
  const dialog = page.getByRole("dialog", { name: "Codex Goal" });
  await expect(dialog).toBeVisible();

  const visualTop = 20;
  const visualHeight = 560;
  await page.evaluate(({ top, height }) => {
    const root = document.documentElement;
    root.style.setProperty("--app-offset-top", `${top}px`);
    root.style.setProperty("--app-height", `${height}px`);
    root.style.setProperty(
      "--keyboard-inset", `${window.innerHeight - top - height}px`,
    );
    window.dispatchEvent(new Event("resize"));
  }, { top: visualTop, height: visualHeight });
  await dialog.getByRole("button", { name: "修改目标", exact: true }).click();
  await dialog.locator("textarea").focus();

  await expect.poll(async () => dialog.boundingBox()).not.toBeNull();
  const box = await dialog.boundingBox();
  if (!box) throw new Error("goal dialog has no tablet geometry");
  expect(box.y).toBeGreaterThanOrEqual(visualTop - 1);
  expect(box.y + box.height).toBeLessThanOrEqual(
    visualTop + visualHeight + 1,
  );
  expect(Math.abs(
    box.y + box.height / 2 - (visualTop + visualHeight / 2),
  )).toBeLessThan(2);
});

test("the compact Goal monitor yields while the mobile keyboard is open", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/tests/history-browser.html?goal-ui=1");
  const chip = page.getByRole("button", { name: "查看 Goal" });
  await expect(chip).toBeVisible();

  await page.evaluate(() => {
    document.documentElement.setAttribute("data-short-viewport", "ime");
  });
  await expect(chip).toBeHidden();

  await page.evaluate(() => {
    document.documentElement.setAttribute("data-short-viewport", "false");
  });
  await expect(chip).toBeVisible();
});

test("desktop text selection keeps its original virtual turn while edge-dragging", async ({
  page,
}, testInfo) => {
  test.setTimeout(60_000);
  test.skip(isMobileWebKitProject(testInfo.project.name),
    "the configured WebKit project is a touch phone; this is a desktop mouse path");
  await page.goto("/tests/history-browser.html?large=120");
  const viewport = page.locator(".thread");
  await wheelUntilTurn(page, "m42", -600, testInfo.project.name);
  await waitForScrollIdle(page);
  const startTurnId = (await readingAnchor(page)).id;
  const startText = page.locator(
    `[data-turn-id="${startTurnId}"] p`,
  ).first();
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
  await expect(viewport).toHaveAttribute("data-text-selection-dragging", "true");
  // Native edge auto-scroll has platform-dependent acceleration. In Linux
  // WebKit the same 12 wheel/move pairs only advance ~150px, while macOS moves
  // much farther. Drive a real held-pointer gesture until it crosses the same
  // virtual-row distance, not for a fixed number of ~45ms frames. Never assign
  // scrollTop or replace the native selection to make this regression pass.
  let step = 0;
  await expect.poll(async () => {
    await page.mouse.wheel(0, 220);
    await page.mouse.move(
      viewportBox.x + viewportBox.width - 48 + (step++ % 2),
      viewportBox.y + viewportBox.height - 2,
    );
    return await viewport.evaluate((node) => node.scrollTop) - startScrollTop;
  }, {
    timeout: 12_000,
    intervals: [75],
    message: "native edge-drag must scroll across virtual turns while held",
  }).toBeGreaterThan(800);

  const draggedScrollTop = await viewport.evaluate((node) => node.scrollTop);
  const draggingSelection = await nativeSelectionSnapshot(page);
  expect(draggedScrollTop - startScrollTop).toBeGreaterThan(800);
  expect(draggingSelection.anchorTurnId).toBe(startTurnId);
  expect(draggingSelection.focusTurnId).not.toBe(startTurnId);
  expect(draggingSelection.anchorConnected).toBe(true);
  expect(draggingSelection.text).toContain(startTurnId);
  await expect(page.locator(
    `[data-turn-id="${startTurnId}"]`,
  )).toBeAttached();
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
  const nativeReleaseAdvance = immediateReleasedScrollTop - draggedScrollTop;
  expect(nativeReleaseAdvance).toBeGreaterThanOrEqual(-2);
  expect(nativeReleaseAdvance).toBeLessThan(viewportBox.height / 2);
  const immediateReleasedAnchor = await readingAnchor(page);
  // Chromium may finish one native selection auto-scroll step on mouseup and
  // cross a virtual-row boundary. The post-release position is authoritative;
  // the app must not replay an older scroll command after control returns.
  await page.waitForTimeout(120);
  const releasedAnchor = await readingAnchor(page);
  expect(releasedAnchor.id).toBe(immediateReleasedAnchor.id);
  expect(Math.abs(
    releasedAnchor.offset - immediateReleasedAnchor.offset,
  )).toBeLessThan(2);
  await page.locator(`[data-turn-id="${startTurnId}"]`).evaluate((node) => {
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

for (const engine of ["codex", "claude"] as const) {
  for (const theme of ["light", "dark"] as const) {
    test(`Markdown disclosures stay compact and keep their header anchored (${engine}, ${theme})`, async ({ page }) => {
      await page.goto("/tests/history-browser.html?markdown-disclosure=1");
      await page.evaluate(({ engine, theme }) => {
        document.documentElement.dataset.engine = engine;
        document.documentElement.dataset.theme = theme;
      }, { engine, theme });
      const disclosure = page.getByTestId("markdown-disclosure").locator(".prose > details");
      const summary = disclosure.locator(":scope > summary");
      await expect(summary).toHaveText("文件清单：21 个源文件，无删除");
      await expect(summary).toHaveCSS("list-style-type", "none");
      await expect(summary).toHaveCSS("font-weight", "500");
      await expect(summary).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
      await expect(summary).toHaveCSS("border-top-width", "0px");
      await expect(disclosure).toHaveCSS("border-top-width", "0px");
      await expect(disclosure).toHaveCSS("border-left-width", "0px");
      const closed = await summary.boundingBox();
      const container = await disclosure.boundingBox();
      if (!closed || !container) throw new Error("disclosure geometry unavailable");
      const coarse = await page.evaluate(() => matchMedia("(pointer: coarse)").matches);
      expect(closed.height).toBeGreaterThanOrEqual(coarse ? 44 : 38);
      expect(closed.x).toBeGreaterThanOrEqual(container.x - 1);
      expect(closed.x + closed.width).toBeLessThanOrEqual(container.x + container.width + 1);
      if ((page.viewportSize()?.width ?? 0) >= 600) {
        expect(closed.width).toBeLessThan(container.width - 80);
      }
      const closedChevron = await summary.evaluate((node) => getComputedStyle(node, "::before").transform);
      await summary.click();
      await expect(disclosure).toHaveAttribute("open", "");
      await expect(summary).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
      await expect(summary).toHaveCSS("border-top-width", "0px");
      await expect.poll(() => summary.evaluate((node) => getComputedStyle(node, "::before").transform))
        .not.toBe(closedChevron);
      const expanded = await summary.boundingBox();
      if (!expanded) throw new Error("expanded header geometry unavailable");
      expect(expanded.x).toBeCloseTo(closed.x, 1);
      expect(expanded.y).toBeCloseTo(closed.y, 1);
      expect(expanded.width).toBeCloseTo(closed.width, 1);
      await expect(disclosure.getByText("README.md", { exact: true })).toBeVisible();
    });
  }
}

test("Markdown disclosures align a long file list with the quiet text header", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/tests/history-browser.html?markdown-disclosure=1&file-list=1");
  const disclosure = page.getByTestId("markdown-disclosure").locator(".prose > details");
  const summary = disclosure.locator(":scope > summary");
  await summary.click();
  await expect(disclosure.locator(":scope > ul > li")).toHaveCount(21);
  const geometry = await disclosure.evaluate((node) => {
    const header = node.querySelector("summary")!;
    const body = node.querySelector(":scope > p")!;
    const list = node.querySelector(":scope > ul")!;
    const headerBox = header.getBoundingClientRect();
    const bodyBox = body.getBoundingClientRect();
    return {
      titleLeft: headerBox.left + parseFloat(getComputedStyle(header).paddingLeft),
      bodyLeft: bodyBox.left,
      gap: bodyBox.top - headerBox.bottom,
      listIndent: parseFloat(getComputedStyle(list).paddingInlineStart),
      contentRight: Math.max(...Array.from(list.querySelectorAll("code")).flatMap(
        (code) => Array.from(code.getClientRects(), (rect) => rect.right),
      )),
      containerRight: node.getBoundingClientRect().right,
      scroll: document.documentElement.scrollWidth,
      viewport: document.documentElement.clientWidth,
    };
  });
  expect(geometry.bodyLeft).toBeCloseTo(geometry.titleLeft, 1);
  expect(geometry.gap).toBeGreaterThanOrEqual(0);
  expect(geometry.gap).toBeLessThanOrEqual(8);
  expect(geometry.listIndent).toBeLessThanOrEqual(20);
  expect(geometry.contentRight).toBeLessThanOrEqual(geometry.containerRight + 1);
  expect(geometry.scroll).toBeLessThanOrEqual(geometry.viewport);
  await summary.click();
  await expect(disclosure.locator(":scope > ul")).toBeHidden();
});

test("Markdown disclosures wrap long titles on a narrow screen without clipping", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto("/tests/history-browser.html?markdown-disclosure=1&long-title=1");
  const disclosure = page.getByTestId("markdown-disclosure").locator(".prose > details");
  const summary = disclosure.locator(":scope > summary");
  await expect(summary).toContainText("long_source_filename_");
  const geometry = await summary.evaluate((node) => {
    const box = node.getBoundingClientRect();
    return { left: box.left, right: box.right, height: box.height,
      scroll: node.scrollWidth, client: node.clientWidth, viewport: document.documentElement.clientWidth };
  });
  expect(geometry.height).toBeGreaterThan(60);
  expect(geometry.scroll).toBeLessThanOrEqual(geometry.client + 1);
  expect(geometry.left).toBeGreaterThanOrEqual(0);
  expect(geometry.right).toBeLessThanOrEqual(geometry.viewport);
  await summary.click();
  await expect(disclosure.getByText("README.md", { exact: true })).toBeVisible();
});

test("Markdown disclosures retain native keyboard controls and reduced-motion support", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/tests/history-browser.html?markdown-disclosure=1");
  const disclosure = page.getByTestId("markdown-disclosure").locator(".prose > details");
  const summary = disclosure.locator(":scope > summary");
  await summary.press("Enter");
  await expect(disclosure).toHaveAttribute("open", "");
  await expect(summary).toBeFocused();
  await expect(summary).toHaveCSS("outline-style", "solid");
  await expect(summary).toHaveCSS("transition-duration", "0s");
  expect(await summary.evaluate((node) => getComputedStyle(node, "::before").transitionDuration)).toBe("0s");
  await summary.press("Space");
  await expect(disclosure).not.toHaveAttribute("open", "");
});

test("Markdown breaks render table rows without adding a leading answer gap", async ({ page }, info) => {
  await page.goto("/tests/history-browser.html?markdown-breaks=1");
  const answer = page.getByTestId("break-answer");
  await expect(answer.locator("br")).toHaveCount(0);
  await expect(answer.locator("p")).toHaveText("正文第一行");
  const plainBox = await page.getByTestId("plain-answer").boundingBox();
  const breakBox = await answer.boundingBox();
  expect(breakBox!.height).toBeCloseTo(plainBox!.height, 0);
  const table = page.getByTestId("table-breaks");
  await expect(table.locator("td br")).toHaveCount(2);
  const rows = await table.locator("td").last().evaluate(cell => {
    const texts = Array.from(cell.childNodes).filter(node => node.nodeType === Node.TEXT_NODE);
    return texts.map(node => { const range = document.createRange(); range.selectNodeContents(node); return range.getBoundingClientRect().top; });
  });
  expect(rows).toHaveLength(3);
  expect(rows[1]).toBeGreaterThan(rows[0]);
  expect(rows[2]).toBeGreaterThan(rows[1]);
  await expect(table.locator("code").first()).toHaveText("<br>");
  await expect(table.locator("pre code")).toHaveText("<br>\n");
  await page.getByRole("button", { name: "Finish break stream", exact: true }).click();
  await expect(table.locator("p").last()).toHaveText("流式内容完成");
  await expect(table.locator("p").last().locator("br")).toHaveCount(1);
  await expect(table.locator("td br")).toHaveCount(2);
  await page.screenshot({ path: info.outputPath("markdown-breaks.png"), animations: "disabled" });
});

test("Markdown disclosures render safely and stay open across streaming updates", async ({ page }) => {
  const unexpectedRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("example.com")) unexpectedRequests.push(request.url());
  });
  await page.goto("/tests/history-browser.html?markdown-disclosure=1");
  const section = page.getByTestId("markdown-disclosure");
  const disclosure = section.locator(".prose > details");
  await expect(disclosure).toBeVisible();
  await expect(section.getByText("README.md", { exact: true })).toBeHidden();
  await disclosure.locator(":scope > summary").click();
  await expect(section.getByText("README.md", { exact: true })).toBeVisible();
  await expect(section.getByText("Inner body", { exact: true })).toBeVisible();
  await section.getByRole("button", { name: "在 Remote 中打开 /tmp/source.py" }).click();
  await expect(page.getByTestId("disclosure-opened-path")).toHaveText("/tmp/source.py");
  await page.getByRole("button", { name: "Finish stream", exact: true }).click();
  await expect(section.getByText("Last streamed item", { exact: true })).toBeVisible();
  await expect(section.getByText("After", { exact: true })).toBeVisible();
  await disclosure.locator(":scope > summary").click();
  await expect(section.getByText("Last streamed item", { exact: true })).toBeHidden();
  await expect(section.getByText("After", { exact: true })).toBeVisible();
  const unsafe = page.getByTestId("inert-disclosure-html");
  await unsafe.locator("summary").click();
  await expect(unsafe.locator("img, script")).toHaveCount(0);
  await expect(unsafe).toContainText('<script>alert(2)</script>');
  expect(unexpectedRequests).toEqual([]);
});

test("desktop native selection auto-scroll does not snap back at the lower edge", async ({
  page,
}, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name), "desktop native selection");
  await page.goto("/tests/history-browser.html?large=120");
  const viewport = page.locator(".thread");
  await wheelUntilTurn(page, "m42", -600, testInfo.project.name);
  await waitForScrollIdle(page);
  const startTurnId = (await readingAnchor(page)).id;
  const text = page.locator(`[data-turn-id="${startTurnId}"] p`).first();
  const point = await textSelectionPoint(text);
  const box = await viewport.boundingBox();
  if (!box) throw new Error("selection fixture has no geometry");
  const before = await viewport.evaluate((node) => node.scrollTop);
  await page.mouse.move(point.x, point.y);
  await page.mouse.down();
  await page.mouse.move(point.x + 100, point.y, { steps: 8 });
  await expect(viewport).toHaveAttribute("data-text-selection-dragging", "true");
  const samples: number[] = [];
  for (let i = 0; i < 40; i += 1) {
    await page.mouse.move(box.x + box.width / 2 + (i % 2), box.y + box.height + 12);
    await page.waitForTimeout(50);
    samples.push(await viewport.evaluate((node) => node.scrollTop));
  }
  const dragged = await viewport.evaluate((node) => node.scrollTop);
  const selected = await nativeSelectionSnapshot(page);
  await page.mouse.up();
  await waitForScrollIdle(page);
  const released = await viewport.evaluate((node) => node.scrollTop);
  expect(dragged - before).toBeGreaterThan(300);
  expect(Math.max(...samples) - dragged, JSON.stringify({ before, samples, released }))
    .toBeLessThan(3);
  expect(released).toBeGreaterThanOrEqual(dragged - 2);
  expect(selected.anchorTurnId).toBe(startTurnId);
  expect(selected.anchorConnected).toBe(true);
});

for (const sameTurn of [false, true]) {
test(`extending a released native selection yields the retained viewport anchor (${sameTurn ? "same turn" : "across turns"})`, async ({ page }) => {
  await page.goto(sameTurn
    ? "/tests/history-browser.html?large=6&paragraphs=60"
    : "/tests/history-browser.html?large=120");
  const viewport = page.locator(".thread");
  // Selection handles do not produce a new mouse pointerdown on the thread.
  // Exercise the browser's Range/selectionchange path directly, not a wheel.
  await waitForScrollIdle(page);
  // Initial virtual measurements can reassert the mounted tail after goto.
  // Establish room for the native scroll before testing selection retention;
  // a clamped write at the bottom cannot exercise the behavior under test.
  for (let attempt = 0; attempt < 6; attempt += 1) {
    await viewport.evaluate((node) => {
      node.scrollTop = Math.max(0, node.scrollHeight - node.clientHeight - 1800);
    });
    await waitForScrollIdle(page);
    if (await viewport.evaluate((node) => node.scrollHeight - node.clientHeight - node.scrollTop >= 1200)) break;
  }
  expect(await viewport.evaluate((node) => node.scrollHeight - node.clientHeight - node.scrollTop))
    .toBeGreaterThanOrEqual(1200);
  const start = (await readingAnchor(page)).id;
  const text = page.locator(`[data-turn-id="${start}"] p`).first();
  await text.evaluate((node) => {
    const range = document.createRange();
    range.setStart(node.firstChild!, 0);
    range.setEnd(node.firstChild!, 6);
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
  });
  await expect(viewport).toHaveAttribute("data-text-selection-retained", "true");
  await waitForScrollIdle(page);
  const before = await viewport.evaluate((node) => node.scrollTop);
  await page.evaluate(({ id, sameTurn }) => {
    const thread = document.querySelector<HTMLElement>(".thread")!;
    const turn = document.querySelector(`[data-turn-id="${id}"]`)!;
    const next = sameTurn ? turn.querySelectorAll("p")[25]
      : turn.nextElementSibling!.nextElementSibling!.querySelector("p")!;
    window.getSelection()!.extend(next.firstChild!, 12);
    // Mobile handles scroll natively after selectionchange, without touchmove
    // reaching React. Keep the test independent of OS handle automation.
    document.dispatchEvent(new Event("selectionchange"));
    thread.scrollTop += 500;
  }, { id: start, sameTurn });
  await waitForScrollIdle(page);
  const after = await viewport.evaluate((node) => node.scrollTop);
  expect(after - before).toBeGreaterThan(400);
  expect((await nativeSelectionSnapshot(page)).anchorTurnId).toBe(start);
  if (sameTurn) expect((await nativeSelectionSnapshot(page)).focusTurnId).toBe(start);
  await page.waitForTimeout(400);
  expect(Math.abs(await viewport.evaluate((node) => node.scrollTop) - after)).toBeLessThan(2);
});
}

test("desktop native selection retains ownership through a transient empty edge range", async ({ page }, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name), "desktop native selection");
  await page.goto("/tests/history-browser.html?large=120");
  const viewport = page.locator(".thread");
  await wheelUntilTurn(page, "m42", -600, testInfo.project.name);
  await waitForScrollIdle(page);
  const turn = (await readingAnchor(page)).id;
  const point = await textSelectionPoint(page.locator(`[data-turn-id="${turn}"] p`).first());
  await page.mouse.move(point.x, point.y);
  await page.mouse.down();
  await page.mouse.move(point.x + 100, point.y, { steps: 8 });
  await expect(viewport).toHaveAttribute("data-text-selection-dragging", "true");
  const range = await page.evaluateHandle(() => window.getSelection()!.getRangeAt(0).cloneRange());
  await page.evaluate(() => {
    window.getSelection()!.removeAllRanges();
    document.dispatchEvent(new Event("selectionchange"));
  });
  await expect(viewport).toHaveAttribute("data-text-selection-dragging", "true");
  await expect(viewport).toHaveAttribute("data-text-selection-retained", "true");
  const before = await viewport.evaluate((node) => node.scrollTop);
  await viewport.evaluate((node) => { node.scrollTop += 420; });
  await waitForScrollIdle(page);
  await expect(viewport).toHaveAttribute("data-text-selection-dragging", "true");
  await expect(viewport).toHaveAttribute("data-text-selection-retained", "true");
  await page.evaluate((saved) => {
    // WebKit may create a collapsed caret during the scroll above. addRange
    // does not replace an existing range in a single-range browser, so restore
    // this synthetic transient selection explicitly (also on a plain page).
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(saved);
    document.dispatchEvent(new Event("selectionchange"));
  }, range);
  expect((await nativeSelectionSnapshot(page)).anchorTurnId).toBe(turn);
  expect((await nativeSelectionSnapshot(page)).text).not.toBe("");
  await page.mouse.up();
  await waitForScrollIdle(page);
  expect(await viewport.evaluate((node) => node.scrollTop)).toBeGreaterThan(before + 400);
  expect((await nativeSelectionSnapshot(page)).anchorTurnId).toBe(turn);
  expect((await nativeSelectionSnapshot(page)).text).not.toBe("");
  await expect(viewport).toHaveAttribute("data-text-selection-dragging", "false");
  await range.dispose();
});

test("desktop wheel scrolling remains available after text selection", async ({
  page,
}, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name),
    "the configured WebKit project is a touch phone; this is a desktop mouse path");
  await page.goto("/tests/history-browser.html?large=120");
  const viewport = page.locator(".thread");
  await wheelUntilTurn(page, "m42", -600, testInfo.project.name);
  await waitForScrollIdle(page);
  const startTurnId = (await readingAnchor(page)).id;
  const text = page.locator(`[data-turn-id="${startTurnId}"] p`).first();
  // A reading anchor may be clipped above the thread. In the CI trace the
  // drag hit the fixture toolbar instead of text, so no selection existed.
  await text.scrollIntoViewIfNeeded();
  await waitForScrollIdle(page);
  const point = await textSelectionPoint(text);
  expect(await text.evaluate((node, at) =>
    node.contains(document.elementFromPoint(at.x, at.y)), point)).toBe(true);

  await page.mouse.move(point.x, point.y);
  await page.mouse.down();
  await page.mouse.move(
    point.x + 100, point.y,
    { steps: 12 },
  );
  await expect(viewport).toHaveAttribute("data-text-selection-dragging", "true");
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
  test.skip(isMobileWebKitProject(testInfo.project.name),
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
  test.skip(isMobileWebKitProject(testInfo.project.name),
    "the configured WebKit project is a touch phone; this is a desktop mouse path");
  await page.goto("/tests/history-browser.html?large=80");
  const viewport = page.locator(".thread");
  await wheelUntilTurn(page, "m42", -600, testInfo.project.name);
  await waitForScrollIdle(page);
  const startText = page.locator('[data-turn-id="m42"] p').first();
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
  await scrollThreadToEdge(page, "start", testInfo.project.name);
  const timeline = page.locator('[data-turn-id="timeline"]');
  await expect(timeline).toBeVisible();
  await timeline.locator(".turn-process-head").click();
  const activity = timeline.locator("details.process-activity");
  const reasoning = timeline.locator("details.process-reasoning");
  await activity.locator(":scope > summary").click();
  // Claude thinking starts open; remember an explicit collapse across unmount.
  await expect(reasoning).toHaveAttribute("open", "");
  await reasoning.locator(":scope > summary").click();
  await expect(activity).toHaveAttribute("open", "");
  await expect(reasoning).not.toHaveAttribute("open", "");

  await scrollThreadToEdge(page, "end", testInfo.project.name);
  await expect(timeline).toHaveCount(0);
  await scrollThreadToEdge(page, "start", testInfo.project.name);
  await expect(timeline).toBeVisible();
  await expect(timeline.locator("details.process-activity"))
    .toHaveAttribute("open", "");
  await expect(timeline.locator("details.process-reasoning"))
    .not.toHaveAttribute("open", "");
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
  await summary.click();
  await expect(timeline.locator("details.process-reasoning")).not.toHaveAttribute("open", "");
  await waitForScrollIdle(page);
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

test("dragging a process header outside cannot leave output following locked", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?interactive-timeline=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  const header = page.locator(
    '[data-turn-id="timeline"] .turn-process-head',
  );
  const box = await header.boundingBox();
  if (!box) throw new Error("process header has no bounds");

  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width + 80, box.y + box.height + 80);
  await page.mouse.up();
  await expect(header).toHaveAttribute("aria-expanded", "false");

  await page.getByTestId("grow-stream").click();
  await expect.poll(() => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);
});

for (const kind of ["thinking", "activity"] as const) {
test(`dragging nested process ${kind} outside cannot leave output following locked`, async ({
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
  const disclosure = timeline.locator(kind === "thinking"
    ? "details.process-reasoning" : "details.process-activity");
  const summary = disclosure.locator(":scope > summary");
  // Start both drags with the preceding reasoning closed, so the activity
  // label is inside the mobile viewport rather than below its bottom edge.
  await timeline.locator("details.process-reasoning > summary").click();
  await waitForScrollIdle(page);
  await expect(disclosure).not.toHaveAttribute("open", "");
  const box = await summary.boundingBox();
  const labelBox = await summary.locator(kind === "thinking"
    ? ":scope > span" : ".process-item-title").boundingBox();
  if (!box || !labelBox) throw new Error("nested process summary has no bounds");

  // Start on the label, not empty summary padding: WebKit can otherwise
  // alternate between a control drag and an accidental native text selection.
  await page.mouse.move(
    labelBox.x + labelBox.width / 2, labelBox.y + labelBox.height / 2,
  );
  await page.mouse.down();
  await page.mouse.move(box.x + box.width + 80, box.y + box.height + 80);
  await page.mouse.up();
  await expect(disclosure).not.toHaveAttribute("open", "");
  await expect(viewport).toHaveAttribute("data-text-selection-retained", "false");

  await page.getByTestId("grow-stream").click();
  await expect.poll(() => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);
});
}

test("nested process disclosures preserve keyboard activation and body selection", async ({ page }) => {
  await page.goto("/tests/history-browser.html?interactive-timeline=1&engine=claude");
  const timeline = page.locator('[data-turn-id="timeline"]');
  await timeline.locator(".turn-process-head").click();
  const disclosure = timeline.locator("details.process-reasoning");
  const summary = disclosure.locator(":scope > summary");
  await expect(disclosure).toHaveAttribute("open", "");
  await summary.click();
  await expect(disclosure).not.toHaveAttribute("open", "");
  await summary.press("Enter");
  await expect(disclosure).toHaveAttribute("open", "");
  await expect(summary).toBeFocused();
  await summary.press("Space");
  await expect(disclosure).not.toHaveAttribute("open", "");
  await summary.click();
  await expect(disclosure).toHaveAttribute("open", "");

  const body = disclosure.locator(".process-reasoning-body .prose p").first();
  await body.scrollIntoViewIfNeeded();
  await waitForScrollIdle(page);
  const start = await textSelectionPoint(body);
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await page.mouse.move(start.x + 60, start.y, { steps: 8 });
  await page.mouse.up();
  await expect(page.locator(".thread"))
    .toHaveAttribute("data-text-selection-retained", "true");
  expect((await nativeSelectionSnapshot(page)).text).not.toBe("");
});

test("iOS pointercancel releases process interactions and output following", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit pointer cancellation");
  await page.goto(
    "/tests/history-browser.html?interactive-timeline=1&engine=claude",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  const timeline = page.locator('[data-turn-id="timeline"]');
  const header = timeline.locator(".turn-process-head");

  await dispatchCancelledTouchTap(header, 184);
  await expect(header).toHaveAttribute("aria-expanded", "false");
  await page.getByTestId("grow-stream").click();
  await expect.poll(() => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);

  await header.evaluate((node) => {
    const target = node as HTMLElement;
    target.dispatchEvent(new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      detail: 1,
    }));
  });
  await expect(header).toHaveAttribute("aria-expanded", "true");
  const reasoning = timeline.locator("details.process-reasoning");
  await dispatchCancelledTouchTap(
    reasoning.locator(":scope > summary"), 185,
  );
  await expect(reasoning).toHaveAttribute("open", "");
  await page.getByTestId("grow-stream").click();
  await expect.poll(() => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);
});

// WebKit desktop cannot summon the iOS keyboard or drive native momentum.
// Exercise the same viewport events and touch ownership, and record every DOM
// scroll write: a later anchor repair can hide the fault from endpoint checks.
for (const [keyboardOpen, pullAtBottom] of [[false, false], [true, false], [true, true]]) {
  test(`live append follows yield to a Claude touch drag${keyboardOpen ? " with the keyboard open" : ""}${pullAtBottom ? " at the bottom edge" : ""}`, async ({ page }) => {
    await page.goto("/tests/history-browser.html?interactive-timeline=1&engine=claude"
      + (keyboardOpen ? "&mobile-scroll=1&quota-composer=1" : ""));
    await expect(page.locator('[data-turn-id="streaming"] .turn-working')).toBeVisible();
    for (let index = 0; index < 8; index += 1) {
      await page.getByTestId("grow-stream").evaluate((node) => node.click());
      await page.waitForTimeout(30);
    }
    await waitForScrollIdle(page);
    if (keyboardOpen) {
      await page.evaluate(() => {
        document.querySelector<HTMLTextAreaElement>("textarea")!.focus();
        Object.defineProperties(window.visualViewport!, {
          height: { configurable: true, value: 420 },
          offsetTop: { configurable: true, value: 0 },
        });
        window.visualViewport!.dispatchEvent(new Event("resize"));
      });
      await expect(page.locator("html")).toHaveAttribute("data-short-viewport", "ime");
      await waitForScrollIdle(page);
    }
    const result = await page.evaluate(async ({ keyboardOpen, pullAtBottom }) => {
      const thread = document.querySelector<HTMLElement>(".thread")!;
      const grow = document.querySelector<HTMLButtonElement>('[data-testid="grow-stream"]')!;
      const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
      const touch = (type: string, clientY: number) => {
        const point = { identifier: 1, target: thread, clientX: 120, clientY };
        const event = new Event(type, { bubbles: true, cancelable: true });
        Object.defineProperties(event, {
          touches: { value: type === "touchend" ? [] : [point] },
          targetTouches: { value: type === "touchend" ? [] : [point] },
          changedTouches: { value: [point] },
        });
        thread.dispatchEvent(event);
      };
      const writes: { from: number; to: number; kind: string }[] = [];
      let readerOwnsScroll = false;
      let simulateDrag = false;
      let intended = 0;
      let interrupt: (() => void) | null = null;
      const nativeScroll = thread.scrollTo;
      const scrollTop = Object.getOwnPropertyDescriptor(Element.prototype, "scrollTop")!;
      Object.defineProperty(thread, "scrollTop", {
        configurable: true,
        get() { return scrollTop.get!.call(this); },
        set(value: number) {
          const from = scrollTop.get!.call(this) as number;
          scrollTop.set!.call(this, value);
          if (readerOwnsScroll && !simulateDrag && Math.abs(value - from) > 1) {
            writes.push({ from, to: value, kind: "scrollTop" });
          }
        },
      });
      thread.scrollTo = function (...args: Parameters<HTMLElement["scrollTo"]>) {
        const from = thread.scrollTop;
        nativeScroll.apply(this, args);
        if (readerOwnsScroll && Math.abs(thread.scrollTop - from) > 1) {
          writes.push({ from, to: thread.scrollTop, kind: "scrollTo" });
        }
        if (interrupt) {
          const callback = interrupt;
          interrupt = null;
          // Interrupt an actual pending virtualizer bottom operation before
          // its next reconciliation frame, as a finger can do on iOS.
          queueMicrotask(callback);
        }
      };
      try {
        await new Promise<void>((resolve, reject) => {
          const timeout = setTimeout(() => reject(new Error("no followed-tail write")), 2_000);
          interrupt = () => {
            readerOwnsScroll = true;
            touch("touchstart", 120);
            touch("touchmove", pullAtBottom ? 60 : 300);
            if (!pullAtBottom) {
              simulateDrag = true;
              thread.scrollTop -= 180;
              simulateDrag = false;
            }
            intended = thread.scrollTop;
            clearTimeout(timeout);
            resolve();
          };
          if (keyboardOpen) {
            Object.defineProperty(window.visualViewport!, "height", {
              configurable: true, value: 400,
            });
            window.visualViewport!.dispatchEvent(new Event("resize"));
          } else grow.click();
        });
        await sleep(60);
        if (!pullAtBottom) touch("touchend", 300);
        for (let index = 0; index < 8; index += 1) {
          await sleep(80);
          grow.click();
          if (keyboardOpen) {
            Object.defineProperty(window.visualViewport!, "height", {
              configurable: true, value: index % 2 ? 400 : 410,
            });
            window.visualViewport!.dispatchEvent(new Event("resize"));
          }
        }
        await sleep(350);
        if (pullAtBottom) {
          readerOwnsScroll = false;
          touch("touchend", 60);
        }
        return {
          writes, intended, finalTop: thread.scrollTop,
          editorFocused: document.activeElement?.tagName === "TEXTAREA",
          paused: !!document.querySelector(".scroll-bottom-btn"),
        };
      } finally {
        thread.scrollTo = nativeScroll;
        Reflect.deleteProperty(thread, "scrollTop");
      }
    }, { keyboardOpen, pullAtBottom });
    expect(result.writes, JSON.stringify(result.writes)).toEqual([]);
    if (!pullAtBottom) {
      expect(result.paused, JSON.stringify(result)).toBe(true);
      expect(Math.abs(result.finalTop - result.intended)).toBeLessThanOrEqual(1);
      await page.locator(".scroll-bottom-btn").click();
    }
    if (keyboardOpen) expect(result.editorFocused).toBe(true);
    await expect.poll(() => page.locator(".thread").evaluate((node) =>
      Math.abs(node.scrollHeight - node.scrollTop - node.clientHeight),
    )).toBeLessThanOrEqual(2);
  });
}

test("live append follows yield to a held desktop scrollbar", async ({ page, isMobile }) => {
  test.skip(isMobile, "desktop scrollbar ownership");
  await page.goto("/tests/history-browser.html?interactive-timeline=1&engine=claude");
  for (let index = 0; index < 8; index += 1) {
    await page.getByTestId("grow-stream").evaluate((node) => node.click());
    await page.waitForTimeout(30);
  }
  await waitForScrollIdle(page);
  const result = await page.locator(".thread").evaluate(async (node) => {
    const rect = node.getBoundingClientRect();
    node.dispatchEvent(new PointerEvent("pointerdown", {
      bubbles: true, pointerId: 41, pointerType: "mouse", isPrimary: true,
      button: 0, clientX: rect.right - 2, clientY: rect.bottom - 30,
    }));
    await new Promise((resolve) => setTimeout(resolve, 350));
    node.scrollTop -= 180;
    const intended = node.scrollTop;
    await new Promise((resolve) => setTimeout(resolve, 50));
    document.querySelector<HTMLButtonElement>('[data-testid="grow-stream"]')!.click();
    await new Promise((resolve) => setTimeout(resolve, 400));
    document.dispatchEvent(new PointerEvent("pointerup", {
      bubbles: true, pointerId: 41, pointerType: "mouse", isPrimary: true,
    }));
    return { intended, actual: node.scrollTop };
  });
  expect(Math.abs(result.actual - result.intended)).toBeLessThanOrEqual(1);
  await expect(page.locator(".scroll-bottom-btn")).toBeVisible();
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
  await assertCodexBurstNeverPaintsAboveTail(page, testInfo.project.name);
});

for (const input of ["wheel", "touch", "keyboard"]) {
  test(`downward input at an already clamped history edge restores the active tail (${input})`, async ({ page }, testInfo) => {
    await page.goto("/tests/history-browser.html?deep-browse=1&dirty-live-browse=1&exhausted-browse=1");
    await page.getByTestId("append-turn").click();
    const thread = page.locator(".thread");
    // A resize or a settled page can leave the native viewport already at the
    // edge, without a user-driven scroll event. Merely being here must retain
    // the historical page until the reader explicitly continues downward.
    await thread.evaluate(el => { el.scrollTop = el.scrollHeight; });
    await waitForScrollIdle(page);
    await expect(page.getByRole("button", { name: "回到最新", exact: true })).toBeVisible();
    await expect(page.locator('[data-turn-id="live-streaming"]')).toHaveCount(0);
    if (input === "touch") await dispatchTouchGesture(page, -60);
    else if (input === "keyboard") {
      await thread.focus();
      await page.keyboard.press("ArrowDown");
    } else if (isMobileWebKitProject(testInfo.project.name)) {
      // Playwright cannot send native mouse-wheel input to iOS WebKit. Exercise
      // the clamped wheel event itself; native desktop wheel is covered below.
      await thread.dispatchEvent("wheel", { deltaY: 200 });
    } else {
      await thread.hover();
      await page.mouse.wheel(0, 200);
    }
    await expect(page.locator('[data-turn-id="live-streaming"] .turn-working')).toBeVisible();
    await expect(page.locator(".scroll-bottom-btn")).toHaveCount(0);
  });
}

test("scrolling a live-dirty history window to its latest edge restores the active tail", async ({
  page,
}, testInfo) => {
  await page.goto(
    "/tests/history-browser.html?deep-browse=1&dirty-live-browse=1&engine=claude",
  );
  await expect(page.locator('[data-turn-id="m20"]')).toBeVisible();
  await page.getByTestId("append-turn").click();
  await expect(page.locator('[data-turn-id="live-streaming"]')).toHaveCount(0);

  for (let pageIndex = 0; pageIndex < 5; pageIndex += 1) {
    await scrollThreadToEdge(page, "end", testInfo.project.name);
    if (await page.locator('[data-turn-id="live-streaming"] .turn-working')
      .count()) break;
    await page.waitForTimeout(80);
  }

  await expect(page.locator('[data-turn-id="live-streaming"] .turn-working'))
    .toBeVisible();
  await expect(page.getByTestId("newest-turn-id")).toHaveText("live-streaming");
  await expect(page.locator(".scroll-bottom-btn")).toHaveCount(0);
});

test("returning to a background-grown live turn settles at its current tail", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?interactive-timeline=1&engine=claude",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await expect(page.locator('[data-turn-id="streaming"] .turn-working'))
    .toBeVisible();

  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();
  // Visibility can precede ChatView's next-frame session-entry tail settle on
  // a loaded WebKit worker. Measure the background update only after that
  // intentional scope transition has finished.
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);
  await page.getByTestId("grow-background-stream").click();
  await page.waitForTimeout(200);
  const unchanged = await readingAnchor(page);
  expect(unchanged.id).toBe(before.id);
  expect(Math.abs(unchanged.offset - before.offset)).toBeLessThan(2);

  await page.getByTestId("switch-session").click();
  const working = page.locator(
    '[data-turn-id="streaming"] .turn-working',
  );
  await expect(working).toBeVisible();
  await expect.poll(async () => working.evaluate((node) => {
    const thread = document.querySelector<HTMLElement>(".thread");
    if (!thread) return false;
    return node.getBoundingClientRect().bottom
      <= thread.getBoundingClientRect().bottom + 1;
  })).toBe(true);
  for (let index = 0; index < 4; index += 1) {
    await page.getByTestId("grow-stream").click();
    await expect.poll(async () => working.evaluate((node) => {
      const thread = document.querySelector<HTMLElement>(".thread");
      if (!thread) return false;
      return node.getBoundingClientRect().bottom
        <= thread.getBoundingClientRect().bottom + 1;
    })).toBe(true);
  }
});

async function assertCodexBurstNeverPaintsAboveTail(
  page: import("@playwright/test").Page,
  projectName: string,
  query = "",
): Promise<void> {
  await page.goto(`/tests/history-browser.html?codex-live-burst=1${query}`);
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await waitForScrollIdle(page);

  const resultPromise = page.evaluate(async () => {
    const thread = document.querySelector<HTMLElement>(".thread");
    const start = document.querySelector<HTMLButtonElement>(
      '[data-testid="start-codex-burst"]',
    );
    if (!thread || !start) throw new Error("Codex burst fixture is incomplete");
    const distances: number[] = [];
    const violations: Array<{
      frame: number;
      distance: number;
      scrollTop: number;
      scrollHeight: number;
      burst: string | undefined;
    }> = [];
    const reverseJumps: Array<{
      frame: number;
      previousScrollTop: number;
      scrollTop: number;
      previousScrollHeight: number;
      scrollHeight: number;
    }> = [];
    let frames = 0;
    let doneFrames = 0;
    let previousScrollTop = thread.scrollTop;
    let previousScrollHeight = thread.scrollHeight;
    let previousBurst = document.documentElement.dataset.codexBurst;
    start.click();
    await new Promise<void>((resolve) => {
      const sample = () => {
        frames += 1;
        const distance = Math.max(
          0,
          thread.scrollHeight - thread.scrollTop - thread.clientHeight,
        );
        distances.push(distance);
        if (distance > 2) {
          violations.push({
            frame: frames,
            distance,
            scrollTop: thread.scrollTop,
            scrollHeight: thread.scrollHeight,
            burst: document.documentElement.dataset.codexBurst,
          });
        }
        const burst = document.documentElement.dataset.codexBurst;
        if (burst === "running" && previousBurst === "running"
            && thread.scrollTop < previousScrollTop - 2) {
          reverseJumps.push({
            frame: frames,
            previousScrollTop,
            scrollTop: thread.scrollTop,
            previousScrollHeight,
            scrollHeight: thread.scrollHeight,
          });
        }
        previousScrollTop = thread.scrollTop;
        previousScrollHeight = thread.scrollHeight;
        previousBurst = burst;
        if (burst === "done") {
          doneFrames += 1;
          if (doneFrames >= 6) {
            resolve();
            return;
          }
        }
        requestAnimationFrame(() => window.setTimeout(sample, 0));
      };
      requestAnimationFrame(() => window.setTimeout(sample, 0));
    });
    return {
      frames,
      worst: Math.max(0, ...distances),
      reverseJumps,
      violations,
    };
  });

  const result = await resultPromise;
  expect(result.frames).toBeGreaterThan(10);
  expect(result.violations, JSON.stringify(result.violations)).toHaveLength(0);
  expect(result.reverseJumps, JSON.stringify(result.reverseJumps))
    .toHaveLength(0);
  expect(result.worst).toBeLessThanOrEqual(2);

  // The pre-paint observer is active only while the newest turn is open. A
  // reader who has deliberately left the tail must retain the exact row while
  // the same long Codex tool burst continues in the background.
  await page.goto(`/tests/history-browser.html?codex-live-burst=1${query}`);
  await wheelUntilTurn(page, "burst-history-1", -2_000, projectName);
  const before = await readingAnchor(page);
  await page.getByTestId("start-codex-burst").click();
  await expect.poll(() => page.evaluate(
    () => document.documentElement.dataset.codexBurst,
  ), { timeout: 10_000 }).toBe("done");
  await page.waitForTimeout(100);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
}

test("live append follows separate commentary and tool messages without bobbing", async ({ page }, testInfo) => {
  await assertCodexBurstNeverPaintsAboveTail(page, testInfo.project.name, "&staged-live=1");
});

test("live append follows mixed tool statuses without bobbing", async ({ page }, testInfo) => {
  await assertCodexBurstNeverPaintsAboveTail(page, testInfo.project.name, "&mixed-tools=1");
});

test("multi-line IME growth stays pinned during a Codex tool burst", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?codex-live-burst=1&composer-live=1",
  );
  const result = await page.evaluate(async () => {
    const thread = document.querySelector<HTMLElement>(".thread");
    const input = document.querySelector<HTMLTextAreaElement>(
      '[data-testid="live-composer-shell"] textarea',
    );
    const start = document.querySelector<HTMLButtonElement>(
      '[data-testid="start-codex-burst"]',
    );
    if (!thread || !input || !start) {
      throw new Error("live composer fixture is incomplete");
    }
    const frame = () => new Promise<void>((resolve) => {
      requestAnimationFrame(() => resolve());
    });
    thread.scrollTop = thread.scrollHeight;
    await frame();
    await frame();

    const distances: number[] = [];
    let typed = false;
    let terminalFrames = 0;
    const monitor = new Promise<void>((resolve) => {
      const sample = () => {
        distances.push(Math.max(
          0,
          thread.scrollHeight - thread.scrollTop - thread.clientHeight,
        ));
        if (typed && document.documentElement.dataset.codexBurst === "done") {
          terminalFrames += 1;
          if (terminalFrames >= 6) {
            resolve();
            return;
          }
        }
        requestAnimationFrame(sample);
      };
      requestAnimationFrame(sample);
    });

    const setValue = Object.getOwnPropertyDescriptor(
      HTMLTextAreaElement.prototype, "value",
    )?.set;
    if (!setValue) throw new Error("textarea value setter is unavailable");
    const values = Array.from(
      { length: 5 },
      (_, index) => Array.from(
        { length: index + 1 },
        (__, line) => `第 ${line + 1} 行拼音输入内容用于验证输入框稳定`,
      ).join("\n"),
    );
    const heights: number[] = [input.getBoundingClientRect().height];
    input.focus();
    input.dispatchEvent(new CompositionEvent("compositionstart", {
      bubbles: true,
      data: "",
    }));
    start.click();
    for (const value of values) {
      setValue.call(input, value);
      input.dispatchEvent(new CompositionEvent("compositionupdate", {
        bubbles: true,
        data: value,
      }));
      input.dispatchEvent(new InputEvent("input", {
        bubbles: true,
        data: value,
        inputType: "insertCompositionText",
      }));
      await frame();
      await frame();
      heights.push(input.getBoundingClientRect().height);
    }
    input.dispatchEvent(new CompositionEvent("compositionend", {
      bubbles: true,
      data: values.at(-1),
    }));
    typed = true;
    await monitor;
    return {
      heights,
      worstDistance: Math.max(0, ...distances),
    };
  });

  expect(result.heights.at(-1)).toBeGreaterThan(result.heights[0]);
  expect(result.heights.at(-1)).toBeLessThanOrEqual(133);
  expect(result.worstDistance).toBeLessThanOrEqual(2);
});

for (const [target, source] of [
  ["composer", "picker"],
  ["composer", "drop"],
  ["composer", "paste"],
  ["new-chat controls", "picker"],
  ["new-chat controls", "paste"],
] as const) {
  test(`${target} bounded attachment selection from ${source} survives event cleanup`, async ({ page }) => {
    const alerts: string[] = [];
    page.on("dialog", async (dialog) => {
      alerts.push(dialog.message());
      await dialog.accept();
    });
    await page.goto(target === "composer"
      ? "/tests/history-browser.html?codex-live-burst=1&composer-live=1&composer-paste=1"
      : "/tests/history-browser.html?newchat-controls=1");
    const input = page.locator("textarea").first();
    await input.fill("keep this draft");
    const picker = page.getByLabel("添加文件", { exact: true });
    await picker.setInputFiles([
      { name: "existing.png", mimeType: "image/png", buffer: staticPng() },
      ...Array.from({ length: 5 }, (_, index) => ({
        name: `existing-${index}.txt`, mimeType: "text/plain", buffer: Buffer.from("existing"),
      })),
    ]);
    await expect(page.locator(".attach-image-preview")).toHaveCount(1);
    await expect(page.locator(".attach-file")).toHaveCount(5);
    expect(await picker.inputValue()).toBe("");

    // A huge lazy FileList exposes unbounded copying without allocating a huge
    // fixture. Invalidate it when the event returns, as native drop stores do.
    for (const remaining of [2, 0]) {
      const reads = await picker.evaluate((node, { source, remaining }) => {
        const element = node as HTMLInputElement;
        const reads: number[] = [];
        let expired = false;
        const files = new Proxy({ length: 100_000 }, {
          get(target, key) {
            if (expired) throw new Error("selection accessed after event cleanup");
            if (key === "length") return target.length;
            if (key === Symbol.iterator) throw new Error("unbounded FileList iteration");
            const index = Number(key);
            if (!Number.isInteger(index) || index < 0 || index >= remaining) {
              throw new Error("selection read beyond the remaining attachment allowance");
            }
            reads.push(index);
            return new File([`added ${index}`], `added-${index}.txt`, { type: "text/plain" });
          },
        });
        try {
          if (source === "picker") {
            Object.defineProperty(element, "files", { configurable: true, value: files });
            element.dispatchEvent(new Event("change", { bubbles: true }));
          } else {
            const data = new DataTransfer();
            Object.defineProperty(data, "files", { value: files });
            if (source === "paste") {
              data.setData("text/plain", ` pasted-${remaining}`);
              Object.defineProperty(data, "items", { get() {
                throw new Error("unbounded clipboard item access");
              } });
              const textarea = document.querySelector("textarea")!;
              textarea.focus();
              textarea.setSelectionRange(textarea.value.length, textarea.value.length);
              textarea.dispatchEvent(new ClipboardEvent("paste", {
                bubbles: true, cancelable: true, clipboardData: data,
              }));
            } else {
              Object.defineProperty(data, "types", { value: ["Files"] });
              window.dispatchEvent(new DragEvent("drop", {
                bubbles: true, cancelable: true, dataTransfer: data,
              }));
            }
          }
        } finally {
          expired = true;
          if (source === "picker") Reflect.deleteProperty(element, "files");
        }
        return reads;
      }, { source, remaining });
      expect(reads).toEqual(remaining ? [0, 1] : []);
      await expect(page.locator(".attach-file")).toHaveCount(7);
      await expect(page.locator(".attach-image-preview")).toHaveCount(1);
      await expect(input).toHaveValue(source === "paste"
        ? `keep this draft pasted-2${remaining ? "" : " pasted-0"}` : "keep this draft");
      const overflow = "一次消息最多 8 个附件，其余文件未导入";
      if (target === "composer") {
        await expect(page.getByText(overflow, { exact: true })).toBeVisible();
      } else {
        await expect.poll(() => alerts.length).toBe(remaining ? 1 : 2);
        expect(alerts.at(-1)).toBe(overflow);
      }
    }
    await expect(page.locator(".attach-file")).toContainText([
      "existing-0.txt", "existing-1.txt", "existing-2.txt", "existing-3.txt",
      "existing-4.txt", "added-0.txt", "added-1.txt",
    ]);
  });
}

for (const target of ["long paste", "new-chat controls", "side chat scope"]) {
  for (const replay of ["paste", "insertFromPaste", "insertText"]) {
    test(`${target} iOS keyboard paste ignores an immediate ${replay} replay`, async ({ page }) => {
      await page.addInitScript(() => Object.defineProperty(navigator, "userAgent", {
        configurable: true, value: "Mozilla/5.0 (iPhone; CPU iPhone OS 27_0 like Mac OS X)",
      }));
      if (target === "side chat scope") {
        await mockRightPanelRelay(page, { visible: true });
        await page.goto("/");
      } else await page.goto(target === "new-chat controls"
        ? "/tests/history-browser.html?newchat-controls=1"
        : "/tests/history-browser.html?codex-live-burst=1&composer-live=1&composer-paste=1");
      const input = target === "side chat scope"
        ? page.locator(".btw-panel textarea") : page.locator("textarea").first();
      await input.fill("before REPLACE after");
      const result = await input.evaluate((node, replay) => {
        const ta = node as HTMLTextAreaElement;
        ta.focus(); ta.setSelectionRange(7, 14);
        const pasted = "一份测试文字";
        const deliver = () => {
          const data = new DataTransfer();
          data.setData("text/plain", pasted);
          const accepted = ta.dispatchEvent(new ClipboardEvent("paste", {
            bubbles: true, cancelable: true, clipboardData: data,
          }));
          // Synthetic paste has no default action. Use the real editor to
          // simulate that default, including its input event and undo entry.
          if (accepted) document.execCommand("insertText", false, pasted);
          return accepted;
        };
        const first = deliver();
        const second = replay === "paste" ? deliver() : ta.dispatchEvent(new InputEvent("beforeinput", {
          bubbles: true, cancelable: true, inputType: replay, data: pasted,
        }));
        if (replay !== "paste" && second) document.execCommand("insertText", false, pasted);
        const once = ta.value;
        // A deliberate second action must work even before the window expires.
        ta.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "v", metaKey: true }));
        const third = deliver();
        return { first, second, third, once, twice: ta.value };
      }, replay);
      expect(result).toEqual({ first: true, second: false, third: true,
        once: "before 一份测试文字 after", twice: "before 一份测试文字一份测试文字 after" });
      await expect(input).toHaveValue(result.twice);
    });
  }

  test(`${target} iOS keyboard paste imports duplicate image deliveries once`, async ({ page }) => {
    await page.addInitScript(() => Object.defineProperty(navigator, "userAgent", {
      configurable: true, value: "Mozilla/5.0 (iPhone; CPU iPhone OS 27_0 like Mac OS X)",
    }));
    if (target === "side chat scope") {
      await mockRightPanelRelay(page, { visible: true });
      await page.goto("/");
    } else await page.goto(target === "new-chat controls"
      ? "/tests/history-browser.html?newchat-controls=1"
      : "/tests/history-browser.html?codex-live-burst=1&composer-live=1&composer-paste=1");
    const input = target === "side chat scope"
      ? page.locator(".btw-panel textarea") : page.locator("textarea").first();
    await input.focus();
    const result = await input.evaluate(async (node, encoded) => {
      // Warm only the importer: a native second delivery follows a completed
      // import, rather than being rejected by the ordinary import-in-flight lock.
      const importer = "/src/attachment-import.ts";
      const resolver = "/src/clipboard-files.ts";
      await Promise.all([import(/* @vite-ignore */ importer), import(/* @vite-ignore */ resolver)]);
      const ta = node as HTMLTextAreaElement;
      const root = ta.closest(".btw-panel") ?? document;
      const frames = () => new Promise<void>(resolve => requestAnimationFrame(() =>
        requestAnimationFrame(() => resolve())));
      const deliver = () => {
        const data = new DataTransfer();
        data.setData("text/plain", "配图文字");
        data.items.add(new File([Uint8Array.from(atob(encoded), c => c.charCodeAt(0))],
          "image.png", { type: "image/png" }));
        ta.dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: data }));
        data.items.clear();
      };
      const start = performance.now();
      const appeared = new Promise<void>(resolve => {
        const observer = new MutationObserver(() => {
          if (root.querySelectorAll(".attach-image-preview").length !== 1) return;
          observer.disconnect(); resolve();
        });
        observer.observe(root, { childList: true, subtree: true });
      });
      deliver();
      await appeared;
      const repeatAt = performance.now() - start;
      deliver();
      await frames();
      const once = { text: ta.value, images: root.querySelectorAll(".attach-image-preview").length };
      ta.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, pointerType: "touch" }));
      deliver();
      await frames();
      return { repeatAt, once, twice: { text: ta.value,
        images: root.querySelectorAll(".attach-image-preview").length } };
    }, staticPng().toString("base64"));
    expect(result.repeatAt, "probe must replay inside the native duplicate window").toBeLessThan(120);
    expect(result.once).toEqual({ text: "配图文字", images: 1 });
    expect(result.twice).toEqual({ text: "配图文字配图文字", images: 2 });
  });
}

for (const target of ["composer", "new-chat controls"]) {
for (const misreportedInsert of [false, true]) {
  test(`${target === "composer" ? "long paste" : target} mixed clipboard keeps text and deduplicates supplied images${misreportedInsert ? " after a misreported native insert" : ""}`, async ({ page }) => {
    const alerts: string[] = [];
    page.on("dialog", async (dialog) => {
      alerts.push(dialog.message());
      await dialog.accept();
    });
    await page.goto(target === "composer"
      ? "/tests/history-browser.html?codex-live-burst=1&composer-live=1&composer-paste=1"
      : "/tests/history-browser.html?newchat-controls=1");
    const input = page.locator("textarea").first();
    await input.fill("before REPLACE after");
    // One free slot must accept a clipboard File plus the same HTML image
    // without treating those two representations as two new attachments.
    await page.getByLabel("添加文件", { exact: true }).setInputFiles(
      Array.from({ length: 7 }, (_, index) => ({
        name: `existing-${index}.txt`, mimeType: "text/plain", buffer: Buffer.from("existing"),
      })),
    );
    await expect(page.locator(".attach-file")).toHaveCount(7);
    await input.evaluate((node, misreportedInsert) => {
      const ta = node as HTMLTextAreaElement;
      ta.focus(); ta.setSelectionRange(7, 14);
      const canvas = document.createElement("canvas");
      canvas.width = 40; canvas.height = 60;
      canvas.getContext("2d")!.fillRect(0, 0, 40, 60);
      const source = canvas.toDataURL();
      const bytes = Uint8Array.from(atob(source.split(",")[1]), (c) => c.charCodeAt(0));
      const data = new DataTransfer();
      data.setData("text/plain", "pasted text");
      data.setData("text/html", `<div>pasted text<img src="${source}"></div>`);
      data.items.add(new File([bytes], "clipboard.png", { type: "image/png" }));
      const nativeInsert = document.execCommand;
      if (misreportedInsert) {
        document.execCommand = function (command, ui, value) {
          const result = nativeInsert.call(this, command, ui, value);
          if (command === "insertText") {
            if (ta.value !== "before pasted text after") {
              throw new Error("the compatibility probe must actually insert text");
            }
            return false;
          }
          return result;
        };
      }
      try {
        ta.dispatchEvent(new ClipboardEvent("paste", {
          bubbles: true, cancelable: true, clipboardData: data,
        }));
      } finally {
        document.execCommand = nativeInsert;
      }
    }, misreportedInsert);
    await expect(input).toHaveValue("before pasted text after");
    await expect(page.locator(".attach-image-preview")).toHaveCount(1);
    await expect(page.locator(".attach-file")).toHaveCount(7);
    expect(alerts).toEqual([]);
    await expect(page.getByText("一次消息最多 8 个附件，其余文件未导入", { exact: true }))
      .toHaveCount(0);
    if (target === "composer") {
      await input.press("Enter");
      await expect(page.getByTestId("composer-paste-output"))
        .toHaveText("before pasted text after");
      await expect(page.getByTestId("composer-image-count")).toHaveText("1");
    }
  });
}
}

test("long paste static PNG pixels and metadata survive preview and send", async ({ page }) => {
  await page.goto("/tests/history-browser.html?codex-live-burst=1&composer-live=1&composer-paste=1");
  const input = page.locator("textarea").first();
  const image = staticPng(400, 1600, [pngChunk("tEXt", Buffer.from("Comment\0static acTL"))]);
  await input.evaluate((node, base64) => {
    const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
    const data = new DataTransfer();
    data.setData("text/plain", "截图测试文字");
    data.items.add(new File([bytes], "image.png", { type: "image/png" }));
    node.dispatchEvent(new ClipboardEvent("paste", {
      bubbles: true, cancelable: true, clipboardData: data,
    }));
  }, image.toString("base64"));
  await expect(input).toHaveValue("截图测试文字");
  await expect(page.locator(".attach-image-preview")).toHaveCount(1);
  const preview = page.locator(".attach-image-preview img");
  await expect.poll(() => preview.evaluate((node) =>
    [(node as HTMLImageElement).naturalWidth, (node as HTMLImageElement).naturalHeight],
  )).toEqual([392, 1568]);
  await input.press("Enter");
  await expect(page.getByTestId("composer-paste-output")).toHaveText("截图测试文字");
  await expect(page.getByTestId("composer-image-count")).toHaveText("1");
});

test("long paste rejected image headers explain the reason and retain text", async ({ page }) => {
  await page.goto("/tests/history-browser.html?codex-live-burst=1&composer-live=1&composer-paste=1");
  const input = page.locator("textarea").first();
  const warnings: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "warning") warnings.push(message.text());
  });
  const animation = Buffer.alloc(8);
  animation.writeUInt32BE(1, 0);
  for (const [image, expected, reason] of [
    [staticPng(2, 1, [pngChunk("acTL", animation)]), "是动画图片，暂不支持", "animated"],
    [Buffer.from("invalid header"), "图片格式无法识别", "invalid_header"],
  ] as const) {
    await input.fill("");
    await input.evaluate((node, base64) => {
      const data = new DataTransfer();
      const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
      data.setData("text/plain", "保留这段文字");
      data.items.add(new File([bytes], "private-name.png", { type: "image/png" }));
      node.dispatchEvent(new ClipboardEvent("paste", {
        bubbles: true, cancelable: true, clipboardData: data,
      }));
    }, image.toString("base64"));
    await expect(page.getByText(expected, { exact: false })).toBeVisible();
    await expect(input).toHaveValue("保留这段文字");
    await expect(page.locator(".attach-image-preview")).toHaveCount(0);
    expect(warnings.at(-1)).toContain(reason);
    expect(warnings.at(-1)).not.toContain("private-name");
    expect(warnings.at(-1)).not.toContain("保留这段文字");
  }
});

test("long paste Feishu private image references report missing images without losing text", async ({ page }) => {
  await page.goto("/tests/history-browser.html?codex-live-burst=1&composer-live=1&composer-paste=1");
  const input = page.locator("textarea").first();
  await input.focus();
  await input.evaluate((node) => {
    const data = new DataTransfer();
    data.setData("text/plain", "飞书测试文字");
    data.setData("text/html", '<div data-line="true">飞书测试文字</div>'
      + Array.from({ length: 3 }, (_, i) => `<div data-line="true"><img data-lark-image-uri="imkey://fixture-${i}" src="native-resource://sdk/image?resource_type=image&key=fixture-${i}"></div>`).join(""));
    node.dispatchEvent(new ClipboardEvent("paste", {
      bubbles: true, cancelable: true, clipboardData: data,
    }));
  });
  await expect(input).toHaveValue("飞书测试文字");
  await expect(page.getByText(/飞书的 3 张图片仅提供应用内引用/)).toBeVisible();
  await expect(page.locator(".attach-image-preview")).toHaveCount(0);
});

test("long paste rich images remain in their original draft during asynchronous import", async ({ page }) => {
  await page.goto("/tests/history-browser.html?codex-live-burst=1&composer-live=1&composer-paste=1");
  const input = page.locator("textarea").first();
  await input.evaluate((node) => {
    const original = File.prototype.arrayBuffer;
    let release!: () => void;
    const ready = new Promise<void>((resolve) => { release = resolve; });
    (window as unknown as { finishClipboard: () => void }).finishClipboard = release;
    File.prototype.arrayBuffer = async function () {
      await ready; return original.call(this);
    };
    const canvas = document.createElement("canvas");
    canvas.width = 50; canvas.height = 50;
    const data = new DataTransfer();
    data.setData("text/plain", "original draft text");
    data.setData("text/html", `<img src="${canvas.toDataURL()}">`);
    node.dispatchEvent(new ClipboardEvent("paste", {
      bubbles: true, cancelable: true, clipboardData: data,
    }));
  });
  await page.getByTestId("switch-composer-draft").click();
  await expect(input).toHaveValue("");
  await page.evaluate(() => (window as unknown as { finishClipboard: () => void }).finishClipboard());
  await expect(input).not.toHaveAttribute("placeholder", /正在安全导入/);
  await expect(page.locator(".attach-image-preview")).toHaveCount(0);
  await page.getByTestId("switch-composer-draft").click();
  await expect(input).toHaveValue("original draft text");
  await expect(page.locator(".attach-image-preview")).toHaveCount(1);
});

test("canonical image reference keeps portrait and mixed rows aligned through send and reload", async ({ page }, testInfo) => {
  await page.goto("/tests/history-browser.html?user-image-layout=1");
  const turn = page.locator('[data-turn-id="user-image-layout"]');
  const geometry = () => turn.evaluate((node) => {
    const prompt = node.querySelector(".ubub")!.getBoundingClientRect();
    const buttons = [...node.querySelectorAll(".ubub-image-trigger")].map((b) => {
      const rect = b.getBoundingClientRect();
      const img = b.querySelector("img")?.getBoundingClientRect();
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height,
        right: rect.right, imageRight: img?.right ?? rect.right };
    });
    return { right: prompt.right, buttons };
  });
  const initial = await geometry();
  expect(initial.buttons).toHaveLength(3);
  for (const button of initial.buttons) {
    expect(Math.abs(button.height - initial.buttons[0].height)).toBeLessThan(2);
  }
  expect(initial.buttons[0].height / initial.buttons[0].width).toBeCloseTo(3, 1);
  for (const stage of ["loading", "ready", "error"]) {
    await page.getByRole("button", { name: stage, exact: true }).click();
    await expect.poll(async () => (await geometry()).buttons.length).toBe(3);
    const current = await geometry();
    current.buttons.forEach((button, i) => {
      expect(Math.abs(button.width - initial.buttons[i].width)).toBeLessThan(2);
      expect(Math.abs(button.height - initial.buttons[i].height)).toBeLessThan(2);
      expect(Math.abs(button.right - initial.buttons[i].right)).toBeLessThan(2);
      expect(Math.abs(button.imageRight - button.right)).toBeLessThan(2);
    });
    expect(Math.abs(current.buttons.at(-1)!.right - current.right)).toBeLessThan(2);
  }
  await page.getByRole("button", { name: "prompt length" }).click();
  expect(Math.abs((await geometry()).buttons.at(-1)!.right - (await geometry()).right))
    .toBeLessThan(2);
  await page.reload();
  await expect(turn.locator(".ubub-image-trigger")).toHaveCount(3);
  expect((await geometry()).buttons.map((button) => button.width))
    .toEqual(initial.buttons.map((button) => button.width));
  await page.screenshot({ path: testInfo.outputPath("user-images.png"), fullPage: true });
});

test("long paste stays out of the textarea and remains editable before send", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?codex-live-burst=1&composer-live=1&composer-paste=1",
  );
  const input = page.locator(
    '[data-testid="live-composer-shell"] .composer textarea',
  );
  const pasted = `Editable paste opening ${"content ".repeat(170)}`;
  await input.evaluate((node, text) => {
    const data = new DataTransfer();
    data.setData("text/plain", text);
    node.dispatchEvent(new ClipboardEvent("paste", {
      bubbles: true,
      cancelable: true,
      clipboardData: data,
    }));
  }, pasted);
  await expect(input).toHaveValue("");
  const card = page.locator(
    '[data-testid="live-composer-shell"] .paste-card',
  );
  await expect(card).toContainText("Editable paste opening");
  await expect(card).toContainText(`${pasted.length} 字符`);
  const box = await card.boundingBox();
  expect(box?.width ?? 999).toBeLessThanOrEqual(302);

  await card.locator(".paste-open").click();
  const editor = page.getByRole("dialog", { name: "编辑粘贴内容" })
    .getByRole("textbox");
  await editor.fill("edited paste body");
  await page.getByRole("button", { name: "保存" }).click();
  await expect(card).toContainText("edited paste body");
  await expect(input).toHaveValue("");

  await input.fill("visible follow-up");
  await page.locator(
    '[data-testid="live-composer-shell"] .sendbtn',
  ).click();
  await expect(page.getByTestId("composer-paste-output"))
    .toHaveText("edited paste body\n\nvisible follow-up");
  await expect(card).toHaveCount(0);
  await expect(input).toHaveValue("");
});

test("oversized edited paste stays in the draft instead of being cleared", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?codex-live-burst=1&composer-live=1&composer-paste=1",
  );
  const input = page.locator(
    '[data-testid="live-composer-shell"] .composer textarea',
  );
  const seed = `oversized ${"seed ".repeat(205)}`;
  await input.evaluate((node, text) => {
    const data = new DataTransfer();
    data.setData("text/plain", text);
    node.dispatchEvent(new ClipboardEvent("paste", {
      bubbles: true,
      cancelable: true,
      clipboardData: data,
    }));
  }, seed);
  const card = page.locator(
    '[data-testid="live-composer-shell"] .paste-card',
  );
  await card.locator(".paste-open").click();
  const pasteEditor = page.getByRole("dialog", { name: "编辑粘贴内容" });
  await pasteEditor.getByRole("textbox")
    .fill("x".repeat(2 * 1024 * 1024));
  await expect(pasteEditor.locator("small"))
    .toContainText("2097152 字符 · 1 行");
  await page.getByRole("button", { name: "保存" }).click();
  await expect(card.locator(".paste-card-preview")).toHaveText(/^x{180}$/);
  await input.fill("tail");
  await page.locator(
    '[data-testid="live-composer-shell"] .sendbtn',
  ).click();
  await expect(page.locator(
    '[data-testid="live-composer-shell"] .composer-notice',
  )).toContainText("消息内容超过上限");
  await expect(card).toHaveCount(1);
  await expect(input).toHaveValue("tail");
  await expect(page.getByTestId("composer-paste-output")).toHaveText("");
});

test("multi-line composer growth does not move a history reader", async ({
  page,
}, testInfo) => {
  await page.goto(
    "/tests/history-browser.html?codex-live-burst=1&composer-live=1",
  );
  await wheelUntilTurn(
    page, "burst-history-1", -2_000, testInfo.project.name,
  );
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);
  await page.locator(
    '[data-testid="live-composer-shell"] textarea',
  ).fill([
    "第一行输入",
    "第二行输入",
    "第三行输入",
    "第四行输入",
    "第五行输入",
  ].join("\n"));
  await page.waitForTimeout(120);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
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
  test(`Claude quota periods and mobile text stay visible at ${width} px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 800 });
    await page.goto("/tests/history-browser.html?quota-layout=1");
    const meter = page.locator(".usage-meter");
    await expect(meter).toBeVisible();
    const initial = await page.locator(".composer").boundingBox();
    for (const [periods, labels] of [
      ["5h,week", ["5h", "周"]], ["5h,month", ["5h", "月"]],
      ["5h,week,month", ["5h", "周", "月"]], ["month", ["月"]],
    ] as const) {
      await page.getByRole("button", { name: periods, exact: true }).click();
      await expect(meter.locator("b")).toHaveText([...labels]);
      const current = await page.locator(".composer").boundingBox();
      expect(Math.abs(current!.y - initial!.y)).toBeLessThan(1);
      expect(Math.abs(current!.height - initial!.height)).toBeLessThan(1);
    }
    await page.getByRole("button", { name: "5h,month", exact: true }).click();
    await meter.click();
    const popover = page.getByRole("region", { name: "Claude 账户额度" });
    await expect(popover).toContainText("5 小时额度");
    await expect(popover).toContainText("每月额度");
    await expect(popover).not.toContainText("每周额度");
    for (const selector of [".composer", ".hint-mode-label", ".usage-pop", ".prose"]) {
      const geometry = await page.locator(selector).first().evaluate(node => {
        const bounds = node.getBoundingClientRect();
        return { scroll: node.scrollWidth, client: node.clientWidth,
          left: bounds.left, right: bounds.right, viewport: innerWidth };
      });
      expect(geometry.scroll).toBeLessThanOrEqual(geometry.client + 1);
      expect(geometry.left).toBeGreaterThanOrEqual(0);
      expect(geometry.right).toBeLessThanOrEqual(geometry.viewport);
    }
    const inlineCode = page.getByTestId("quota-prose").locator("code");
    expect(await inlineCode.evaluate(node => node.getClientRects().length)).toBeGreaterThan(1);
    await expect(page.locator(".hint-mode-label")).toHaveText("Bypass Permissions");
    expect(await page.locator(".hint-mode-label").evaluate(node =>
      node.clientHeight / Number.parseFloat(getComputedStyle(node).lineHeight))).toBeLessThanOrEqual(2.1);
    await expect(page.getByRole("button", { name: "Opus", exact: true })).toBeVisible();
    await page.screenshot({ path: info.outputPath(`monthly-quota-${width}.png`), animations: "disabled" });
  });
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
  await expect(page.getByTestId("migration-picker-request"))
    .toHaveText("/repo/current");
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
  await expect(dialog).not.toBeVisible();
  await expect(page.locator(".newchat-access")).toContainText("Full Access");
}

let newChatSubmissionSequence = 0;

test("new-chat controls route slash commands without creating a model turn", async ({ page }) => {
  await page.goto("/tests/history-browser.html?newchat-controls=1&selection-controls=1");
  const input = page.locator(".newchat-input");
  const submission = page.getByTestId("newchat-submission");
  await input.fill("/");
  await expect(page.getByRole("listbox", { name: "命令" })).toBeVisible();
  await input.press("Enter");
  await expect(submission).toHaveText("null");
  await input.fill("/mo");
  await input.press("Enter");
  await expect(page.getByRole("dialog", { name: "选择模型", exact: true })).toBeVisible();
  await expect(submission).toHaveText("null");
  await page.keyboard.press("Escape");
  await input.fill("/compact");
  await page.getByRole("button", { name: "开始", exact: true }).click();
  await expect(page.locator(".newchat-hint")).toContainText("需要已有会话");
  await expect(input).toHaveValue("/compact");
  await expect(submission).toHaveText("null");
  await input.fill("/unknown-command");
  await input.press("Enter");
  await expect(page.locator(".newchat-hint")).toContainText("Codex 无此指令");
  await expect(submission).toHaveText("null");
  await input.fill("/permissions");
  await input.press("Enter");
  await expect(page.getByRole("dialog", { name: "权限与执行环境" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByTestId("switch-newchat-space").click();
  await input.fill("/plan change files");
  await input.press("Enter");
  await expect(page.locator(".newchat-hint")).toContainText("Code 指令");
  await expect(submission).toHaveText("null");
});

test("new-chat controls pass native prompts and preserve command arguments", async ({ page }) => {
  await page.goto("/tests/history-browser.html?newchat-controls=1");
  const input = page.locator(".newchat-input");
  const submission = page.getByTestId("newchat-submission");
  await input.fill("/plan inspect this repository");
  await input.press("Enter");
  await expect.poll(async () => JSON.parse(await submission.innerText())).toMatchObject({
    prompt: "inspect this repository", collaborationMode: "plan",
  });
  await page.getByTestId("switch-newchat-device").click();
  await input.fill("/Users/Tester/My Project 请检查此目录");
  await input.press("Enter");
  await expect.poll(async () => JSON.parse(await submission.innerText())).toMatchObject({
    prompt: "/Users/Tester/My Project 请检查此目录", collaborationMode: "default",
  });
  await input.fill("/init follow project conventions");
  await input.press("Enter");
  await expect.poll(async () => JSON.parse(await submission.innerText()).prompt)
    .toMatch(/^Create or update AGENTS\.md[\s\S]*\n\nfollow project conventions$/);
  await page.getByTestId("switch-newchat-engine").click();
  await input.fill("/plugin:review 检查当前改动");
  await input.press("Enter");
  await expect.poll(async () => JSON.parse(await submission.innerText()).prompt)
    .toBe("/plugin:review 检查当前改动");
  await input.fill("/plan");
  await page.getByRole("button", { name: "开始", exact: true }).click();
  await expect(page.locator(".newchat-access")).toHaveText("Plan");
  await expect.poll(async () => JSON.parse(await submission.innerText()).prompt)
    .toBe("/plugin:review 检查当前改动");
  await input.fill("/normal inspect files");
  await input.press("Enter");
  await expect.poll(async () => JSON.parse(await submission.innerText())).toMatchObject({
    prompt: "inspect files", permissionMode: "bypassPermissions",
  });
});

test("new-chat controls keep attachments through local commands and IME confirmation", async ({ page }) => {
  await page.goto("/tests/history-browser.html?newchat-controls=1");
  const input = page.locator(".newchat-input");
  await page.getByLabel("添加文件", { exact: true }).setInputFiles({
    name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("inspect these notes"),
  });
  await expect(page.locator(".attach-file")).toContainText("notes.txt");
  await input.fill("/model");
  await page.getByRole("button", { name: "开始", exact: true }).click();
  const models = page.getByRole("dialog", { name: "选择模型", exact: true });
  await models.locator(".cmd").nth(1).click();
  await expect(page.locator(".attach-file")).toContainText("notes.txt");
  await expect(page.getByTestId("newchat-submission")).toHaveText("null");
  await input.fill("检查附件");
  await input.dispatchEvent("compositionstart");
  await input.press("Enter");
  await expect(page.getByTestId("newchat-submission")).toHaveText("null");
  await input.dispatchEvent("compositionend");
  // A pointer submit deliberately commits the IME before the deferred send.
  await page.getByRole("button", { name: "开始", exact: true }).click();
  await expect.poll(async () => JSON.parse(await page.getByTestId("newchat-submission").innerText()))
    .toMatchObject({ prompt: "检查附件", files: ["notes.txt"] });
});

test("new-chat controls offer enabled Codex skills before the first message", async ({ page }) => {
  await page.goto("/tests/history-browser.html?newchat-controls=1&newchat-commands=1");
  const input = page.locator(".newchat-input");
  await input.fill("$");
  const skills = page.getByRole("listbox", { name: "Skills" });
  await expect(skills).toContainText("$review");
  await expect(skills).not.toContainText("$retired");
  await input.fill("$rev");
  await input.press("Enter");
  await expect(input).toHaveValue("$review ");
  await expect(page.getByTestId("newchat-submission")).toHaveText("null");
  await input.fill("$review inspect the changes");
  await input.press("Enter");
  await expect.poll(async () => JSON.parse(await page.getByTestId("newchat-submission").innerText()).prompt)
    .toBe("$review inspect the changes");
});

test("new-chat controls omit Code-directory skills in a new Work session", async ({ page }) => {
  await page.goto("/tests/history-browser.html?newchat-controls=1&newchat-commands=1");
  const input = page.locator(".newchat-input");
  const skills = page.getByRole("listbox", { name: "Skills" });
  const requests = page.getByTestId("newchat-skill-requests");
  await page.getByTestId("switch-newchat-space").click();
  await input.fill("$");
  await expect(page.locator(".work-newchat")).toBeVisible();
  await expect(skills).toHaveCount(0);
  await expect(requests).toHaveText("0");
  await page.getByTestId("switch-newchat-space").click();
  await expect(skills).toContainText("$review");
  await expect(requests).toHaveText("1");
  await page.getByTestId("switch-newchat-space").click();
  await expect(page.locator(".work-newchat")).toBeVisible();
  await expect(skills).toHaveCount(0);
  await input.fill("$rev");
  await expect(requests).toHaveText("1");
  await input.fill("/extensions");
  await input.press("Enter");
  await expect(page.locator(".newchat-hint")).toContainText("Work 会在创建后分配独立目录");
  await expect(input).toHaveValue("/extensions");
  await expect(page.getByTestId("newchat-submission")).toHaveText("null");
  await input.fill("$rev");
  await page.getByTestId("switch-newchat-space").click();
  await expect(skills).toContainText("$review");
  await expect(requests).toHaveText("1");
});

for (const engine of ["claude", "codex"] as const) {
  test(`new-chat controls preserve raw ${engine} model IDs through catalog refresh and creation`, async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    const relay = await mockRightPanelRelay(page, { engine, visible: false, retained: false });
    await page.goto("/");
    await page.getByRole("button", { name: "显示会话侧栏", exact: true }).click();
    await page.getByRole("button", { name: "新会话", exact: true }).click();
    const input = page.locator(".newchat-input");
    await input.fill("/model provider/hidden-model[1m]");
    await input.press("Enter");
    await expect(input).toHaveValue("");
    await expect(page.getByTitle("选择模型", { exact: true })).toContainText("provider/hidden-model[1m]");
    await page.getByTitle("选择思考强度", { exact: true }).click();
    const efforts = page.getByRole("dialog", { name: "选择思考强度", exact: true });
    await expect(efforts.locator(".sheet-scroll").getByRole("button")).toHaveCount(1);
    await expect(efforts.locator(".sheet-scroll").getByRole("button")).toContainText("默认");
    await page.keyboard.press("Escape");
    expect(relay.commands.filter(command => command.type === "new_session")).toEqual([]);
    const request = relay.commands.filter(command => command.type === "get_models").at(-1)!;
    relay.emit({ type: "models", engine, cwd: request.cwd as string | undefined,
      models: [{ id: "catalog-model", display_name: "Listed model", description: "", efforts: ["high"] }] });
    // Observe catalog application before checking that reconciliation kept the raw ID.
    await page.getByTitle("选择模型", { exact: true }).click();
    await expect(page.getByRole("dialog", { name: "选择模型" })).toContainText("Listed model");
    await page.keyboard.press("Escape");
    await expect(page.getByTitle("选择模型", { exact: true })).toContainText("provider/hidden-model[1m]");
    await page.getByTitle("选择思考强度", { exact: true }).click();
    await expect(efforts.locator(".sheet-scroll").getByRole("button")).toHaveCount(1);
    await page.keyboard.press("Escape");
    await input.fill("hello");
    await input.press("Enter");
    await expect.poll(() => relay.commands.filter(command => command.type === "new_session").map(command => ({
      engine: command.engine ?? "claude", model: command.model, effort: command.effort ?? null,
    })))
      .toMatchObject([{ engine, model: "provider/hidden-model[1m]", effort: null }]);
  });

  test(`new-chat controls keep ${engine} effort choices consistent through model discovery`, async ({ page }) => {
    const relay = await mockRightPanelRelay(page, { engine, retained: false });
    await page.goto("/");
    await page.getByLabel("显示会话侧栏", { exact: true }).click();
    await page.getByRole("button", { name: "新会话", exact: true }).click();
    const input = page.locator(".newchat-input");
    const model = "provider/discovered-model";
    await input.fill(`/model ${model}`);
    await input.press("Enter");
    await expect(input).toHaveValue("");
    const request = relay.commands.filter(command => command.type === "get_models").at(-1)!;
    const effortButton = page.getByTitle("选择思考强度", { exact: true });
    const efforts = page.getByRole("dialog", { name: "选择思考强度", exact: true });
    await effortButton.click();
    await expect(efforts.locator(".sheet-scroll .cmd")).toHaveCount(1);
    relay.emit({ type: "models", engine, cwd: request.cwd as string | undefined,
      models: [{ id: model, display_name: "Discovered model", description: "", efforts: ["low", "high"] }] });
    await expect(efforts.locator(".sheet-scroll .cmd-nm")).toHaveText(["默认", "low", "high"]);
    await efforts.getByRole("button", { name: /^high / }).click();
    await expect(efforts).not.toBeVisible();
    await expect(effortButton).toHaveText("high");
    // A catalog refresh must not silently clear a supported explicit choice.
    relay.emit({ type: "models", engine, cwd: request.cwd as string | undefined,
      models: [{ id: model, display_name: "Refreshed model", description: "", efforts: ["high"] }] });
    await expect(page.getByTitle("选择模型", { exact: true })).toHaveText("Refreshed model");
    await expect(effortButton).toHaveText("high");
    expect(relay.commands.some(command => command.type === "new_session")).toBe(false);
    await input.fill("hello");
    await input.press("Enter");
    await expect.poll(() => relay.commands.filter(command => command.type === "new_session")).toEqual([
      expect.objectContaining({ model, effort: "high", prompt: "hello" }),
    ]);
  });
}

for (const [mode, label] of [
  ["default", "Default"], ["acceptEdits", "Accept Edits"], ["plan", "Plan"],
  ["auto", "Auto"], ["bypassPermissions", "Bypass Permissions"], [null, "默认权限"],
] as const) {
  test(`new-chat controls Claude permissions apply ${mode ?? "inherited"} to the first message`, async ({ page }) => {
    const relay = await mockRightPanelRelay(page, { engine: "claude", visible: false, retained: false });
    await page.goto("/");
    await page.getByLabel("显示会话侧栏", { exact: true }).click();
    await page.getByRole("button", { name: "新会话", exact: true }).click();
    const input = page.locator(".newchat-input");
    await input.fill("inspect the project");
    await page.getByTitle("选择权限模式", { exact: true }).click();
    const sheet = page.getByRole("dialog", { name: "选择权限模式" });
    await sheet.getByRole("button", { name: new RegExp(`^${label} `) }).click();
    await expect(sheet).not.toBeVisible();
    await expect(input).toHaveValue("inspect the project");
    await expect(page.locator(".newchat-access")).toHaveText(label);
    expect(relay.commands.some(command => ["new_session", "set_perm"].includes(String(command.type)))).toBe(false);
    await input.press("Enter");
    await expect.poll(() => relay.commands.filter(command => command.type === "new_session").map(command => ({
      prompt: command.prompt, permission: command.permission_mode ?? null,
      engine: command.engine ?? "claude",
    }))).toEqual([{ prompt: "inspect the project", permission: mode, engine: "claude" }]);
  });
}

test("new-chat controls Claude permissions use local commands and reset on account and surface changes", async ({ page }) => {
  await page.goto("/tests/history-browser.html?newchat-controls=1&selection-controls=1");
  await page.getByTestId("switch-newchat-engine").click();
  const input = page.locator(".newchat-input");
  const access = page.locator(".newchat-access");
  const sheet = page.getByRole("dialog", { name: "选择权限模式" });
  await input.fill("/permissions");
  await input.press("Enter");
  await expect(sheet).toBeVisible();
  await sheet.getByRole("button", { name: /^Accept Edits / }).click();
  await expect(access).toHaveText("Accept Edits");
  await input.press("Shift+Tab");
  await expect(access).toHaveText("Plan");
  await input.fill("keep this draft");
  await page.locator(".newchat-profile").click();
  await page.getByRole("dialog", { name: "选择 Claude 账号" }).getByRole("button", { name: /Secondary/ }).click();
  await expect(access).toHaveText("默认权限");
  await expect(input).toHaveValue("keep this draft");
  await access.click();
  await sheet.getByRole("button", { name: /^Bypass Permissions / }).click();
  await page.getByTestId("switch-newchat-space").click();
  await expect(access).toHaveCount(0);
  const work = await submitNewChatFixture(page);
  expect(work).not.toHaveProperty("permissionMode");
  await page.getByTestId("switch-newchat-space").click();
  await expect(access).toHaveText("默认权限");
  await access.click();
  await sheet.getByRole("button", { name: /^Plan / }).click();
  await access.click();
  await sheet.getByRole("button", { name: /^默认权限 / }).click();
  expect(await submitNewChatFixture(page)).not.toHaveProperty("permissionMode");
  await access.click();
  await sheet.getByRole("button", { name: /^Plan / }).click();
  await page.getByTestId("switch-newchat-device").click();
  await expect(access).toHaveText("默认权限");
});

test("new-chat controls Claude permissions keyboard changes never target the old session", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { engine: "claude", visible: false, retained: false });
  await page.goto("/");
  await page.getByLabel("显示会话侧栏", { exact: true }).click();
  await page.getByRole("button", { name: "新会话", exact: true }).click();
  const input = page.locator(".newchat-input");
  await input.press("Shift+Tab");
  await expect(page.locator(".newchat-access")).toHaveText("Default");
  expect(relay.commands.some(command => command.type === "set_perm")).toBe(false);
  await input.fill("/plan inspect files");
  await input.press("Enter");
  await expect.poll(() => relay.commands.filter(command => command.type === "new_session").map(command => ({
    prompt: command.prompt, permission: command.permission_mode,
  }))).toEqual([{ prompt: "inspect files", permission: "plan" }]);
});

test("new-chat controls apply fast and model commands to the first native query", async ({ page }) => {
  await page.goto("/tests/history-browser.html?newchat-controls=1&newchat-commands=1");
  const input = page.locator(".newchat-input");
  const submission = page.getByTestId("newchat-submission");
  await input.fill("/model fixture-codex");
  await input.press("Enter");
  await expect(input).toHaveValue("");
  await expect(submission).toHaveText("null");
  await input.fill("/fast");
  await input.press("Enter");
  await expect(page.getByRole("button", { name: "速度：快速", exact: true })).toBeVisible();
  await expect(submission).toHaveText("null");
  await input.fill("inspect the project");
  await input.press("Enter");
  await expect.poll(async () => JSON.parse(await submission.innerText())).toMatchObject({
    prompt: "inspect the project", model: "fixture-codex", serviceTier: "priority",
  });
  await input.fill("/fast");
  await input.press("Enter");
  await expect(page.getByRole("button", { name: "速度：标准", exact: true })).toBeVisible();
});

for (const width of [320, 390, 1280]) {
  test(`new-chat controls compact layout grows with text at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 844 });
    await page.goto("/tests/history-browser.html?newchat-controls=1&selection-controls=1");
    for (const engine of ["codex", "claude"]) {
      const input = page.locator(".newchat-input");
      await input.fill("");
      const bar = page.locator(".newchat-inputbar");
      const compactHeight = (await bar.boundingBox())!.height;
      expect(compactHeight).toBeLessThanOrEqual(66);
      await expect(page.locator(`.newchat-greet use[href$="#${engine}"]`)).toHaveCount(1);
      expect(await page.locator(".newchat-profile svg").count()).toBe(1);
      const layout = await page.locator(".newchat-card").evaluate(card => ({
        width: card.clientWidth, scroll: card.scrollWidth,
        page: document.documentElement.scrollWidth, viewport: window.innerWidth,
      }));
      expect(layout.scroll).toBeLessThanOrEqual(layout.width);
      expect(layout.page).toBeLessThanOrEqual(layout.viewport);
      await input.fill(Array.from({ length: 12 }, (_, i) => `第 ${i + 1} 行：检查输入区域`).join("\n"));
      expect((await bar.boundingBox())!.height).toBeGreaterThan(compactHeight);
      expect((await bar.boundingBox())!.height).toBeLessThanOrEqual(152);
      await input.fill("/mo");
      const popup = await page.getByRole("listbox", { name: "命令" }).boundingBox();
      expect(popup!.y).toBeGreaterThanOrEqual(0);
      await page.screenshot({ path: info.outputPath(`newchat-${engine}-${width}.png`) });
      await page.getByTestId("switch-newchat-engine").click();
    }
  });
}

test("new-chat controls center selections and recover after the phone keyboard closes", async ({ page }, info) => {
  await page.setViewportSize({ width: 393, height: 852 });
  await page.goto("/tests/history-browser.html?newchat-controls=1&selection-controls=1");
  for (const label of ["选择模型", "选择思考强度", "选择 Codex 账号"]) {
    const trigger = label.includes("账号")
      ? page.getByRole("button", { name: label, exact: true }) : page.getByTitle(label, { exact: true });
    await trigger.focus();
    await trigger.press("Enter");
    const dialog = page.getByRole("dialog", { name: label, exact: true });
    await expect(dialog).toBeVisible();
    const box = (await dialog.boundingBox())!;
    expect(Math.abs(box.x + box.width / 2 - 393 / 2)).toBeLessThan(2);
    expect(Math.abs(box.y + box.height / 2 - 852 / 2)).toBeLessThan(2);
    expect(await dialog.evaluate(node => parseFloat(getComputedStyle(node).borderBottomLeftRadius))).toBeGreaterThan(16);
    await page.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible();
    await expect(trigger).toBeFocused();
  }
  await page.locator(".newchat-access").click();
  const dialog = page.getByRole("dialog", { name: "权限与执行环境" });
  const original = (await dialog.boundingBox())!;
  await page.evaluate(() => {
    Object.defineProperties(window.visualViewport!, {
      height: { configurable: true, value: 380 },
      offsetTop: { configurable: true, value: 20 },
    });
    window.visualViewport!.dispatchEvent(new Event("resize"));
  });
  await expect.poll(() => dialog.evaluate(node => {
    const box = node.getBoundingClientRect();
    return box.top >= 20 && box.bottom <= 400;
  })).toBe(true);
  await page.evaluate(() => {
    Object.defineProperties(window.visualViewport!, {
      height: { configurable: true, value: 852 },
      offsetTop: { configurable: true, value: 0 },
    });
    window.visualViewport!.dispatchEvent(new Event("resize"));
  });
  await expect.poll(async () => Math.abs((await dialog.boundingBox())!.height - original.height)).toBeLessThan(2);
  await page.screenshot({ path: info.outputPath("mobile-permissions-centered.png") });
});

test("new-chat controls use custom account choices without losing the draft", async ({ page }) => {
  await page.goto("/tests/history-browser.html?newchat-controls=1&selection-controls=1");
  for (const engine of ["Codex", "Claude"]) {
    if (engine === "Claude") await page.getByTestId("switch-newchat-engine").click();
    await page.locator(".newchat-input").fill("保留这个新会话的内容");
    await page.getByRole("button", { name: `选择 ${engine} 账号` }).click();
    const dialog = page.getByRole("dialog", { name: `选择 ${engine} 账号` });
    const account = dialog.getByRole("button", { name: /Secondary/ });
    await account.focus();
    await page.keyboard.press("Enter");
    await expect(dialog).not.toBeVisible();
    await expect(page.locator(".newchat-input")).toHaveValue("保留这个新会话的内容");
    const submitted = await submitNewChatFixture(page);
    expect(submitted.account).toBe("secondary");
    await page.getByRole("button", { name: `选择 ${engine} 账号` }).click();
    await expect(account).toHaveAttribute("aria-pressed", "true");
    await page.keyboard.press("Escape");
  }
});

test("new-chat controls show attachment choices before opening the device picker", async ({ page }, info) => {
  await page.goto("/tests/history-browser.html?newchat-controls=1&selection-controls=1");
  await page.locator(".newchat-input").fill("带上这些附件");
  for (const [choice, inputLabel, accept, capture] of [
    ["照片", "添加照片", "image/*", null],
    ["文件", "添加文件", null, null],
    ["拍照", "拍照", "image/*", "environment"],
  ]) {
    let chooserCount = 0;
    const countChooser = () => { chooserCount += 1; };
    page.on("filechooser", countChooser);
    await page.getByRole("button", { name: "添加附件", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "添加附件", exact: true });
    await expect(dialog).toBeVisible();
    expect(chooserCount).toBe(0);
    if (choice === "照片") await page.screenshot({ path: info.outputPath("attachment-choices.png") });
    const picked = page.waitForEvent("filechooser");
    await dialog.getByRole("button", { name: new RegExp(`^${choice}`) }).click();
    const chooser = await picked;
    expect(await chooser.element().getAttribute("aria-label")).toBe(inputLabel);
    expect(await chooser.element().getAttribute("accept")).toBe(accept);
    expect(await chooser.element().getAttribute("capture")).toBe(capture);
    await chooser.setFiles(choice === "文件"
      ? { name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("附件内容") }
      : { name: `${choice}.png`, mimeType: "image/png", buffer: staticPng(8, 8) });
    await expect(dialog).not.toBeVisible();
    await expect(page.getByRole("button", { name: "添加附件", exact: true })).toBeEnabled();
    expect(chooserCount).toBe(1);
    page.off("filechooser", countChooser);
  }
  await expect(page.locator(".newchat-input")).toHaveValue("带上这些附件");
  await expect(page.locator(".newchat-attach .attach-file")).toContainText("notes.txt");
  const submitted = await submitNewChatFixture(page);
  expect(submitted.files).toEqual(["notes.txt"]);
  expect(submitted.images).toHaveLength(2);
});

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
  await expect(page.locator(".newchat-access")).toHaveText("默认权限");
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
  await page.goto("/tests/history-browser.html?newchat-controls=1&newchat-commands=1");
  await chooseDangerousNewChatControls(page);

  await page.getByTestId("switch-newchat-space").click();
  await expect(page.getByTestId("newchat-scope"))
    .toHaveText("machine-a:work:codex");
  await expect(page.locator(".newchat-access")).toHaveCount(0);
  const fast = page.getByRole("button", { name: /^速度：/ });
  await expect(fast).toHaveText("标准");
  await fast.click();
  await page.getByRole("menuitemradio", { name: /快速/ }).click();
  await expect(fast).toHaveText("快速");
  const workSubmission = await submitNewChatFixture(page);
  expect(workSubmission.permissionMode).toBe("never");
  expect(workSubmission).not.toHaveProperty("permissionProfile");
  expect(workSubmission).not.toHaveProperty("webSearch");
  expect(workSubmission.serviceTier).toBe("priority");

  await page.getByTestId("switch-newchat-engine").click();
  await expect(page.getByTestId("newchat-scope"))
    .toHaveText("machine-a:work:claude");
  await expect(page.getByRole("button", {
    name: /^速度：/,
  })).toHaveCount(0);
  await page.getByTestId("switch-newchat-engine").click();
  await expect(page.getByRole("button", {
    name: /^速度：/,
  })).toHaveText("标准");

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

test("new-chat controls fit the default permission picker on a short phone", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 568 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/tests/history-browser.html?newchat-controls=1");
  await page.locator(".newchat-access").click();
  const dialog = page.getByRole("dialog", {
    name: "权限与执行环境",
  });
  await expect(dialog).toBeVisible();

  const layout = await dialog.evaluate((sheet) => {
    const scroll = sheet.querySelector<HTMLElement>(".sheet-scroll");
    const optionButtons = Array.from(
      sheet.querySelectorAll<HTMLElement>(".permission-options .cmd"),
    );
    const searchButtons = Array.from(
      sheet.querySelectorAll<HTMLElement>(".cmd-search button"),
    );
    const descriptions = Array.from(
      sheet.querySelectorAll<HTMLElement>(".permission-options .cmd-ds"),
    );
    const live = searchButtons.find((button) => button.textContent === "Live");
    if (
      !scroll || optionButtons.length !== 6 || descriptions.length !== 6 || !live
    ) {
      throw new Error("compact permission controls are incomplete");
    }
    const sheetRect = sheet.getBoundingClientRect();
    return {
      viewportHeight: window.innerHeight,
      viewportWidth: window.innerWidth,
      pageScrollWidth: document.documentElement.scrollWidth,
      sheetTop: sheetRect.top,
      sheetBottom: sheetRect.bottom,
      scrollTop: scroll.scrollTop,
      scrollHeight: scroll.scrollHeight,
      scrollClientHeight: scroll.clientHeight,
      liveBottom: live.getBoundingClientRect().bottom,
      minOptionHeight: Math.min(...optionButtons.map(
        (button) => button.getBoundingClientRect().height,
      )),
      minSearchHeight: Math.min(...searchButtons.map(
        (button) => button.getBoundingClientRect().height,
      )),
      minDescriptionWidth: Math.min(...descriptions.map(
        (description) => description.getBoundingClientRect().width,
      )),
      minDescriptionHeight: Math.min(...descriptions.map(
        (description) => description.getBoundingClientRect().height,
      )),
    };
  });

  expect(layout.scrollTop).toBe(0);
  expect(layout.scrollHeight).toBeLessThanOrEqual(
    layout.scrollClientHeight + 1,
  );
  expect(layout.sheetTop).toBeGreaterThanOrEqual(0);
  expect(layout.sheetBottom).toBeLessThanOrEqual(layout.viewportHeight + 1);
  expect(layout.liveBottom).toBeLessThanOrEqual(layout.viewportHeight + 1);
  expect(layout.minOptionHeight).toBeGreaterThanOrEqual(44);
  expect(layout.minSearchHeight).toBeGreaterThanOrEqual(44);
  expect(layout.minDescriptionWidth).toBeGreaterThan(1);
  expect(layout.minDescriptionHeight).toBeGreaterThan(1);
  expect(layout.pageScrollWidth).toBeLessThanOrEqual(layout.viewportWidth);
});

test("default permission picker stays compact on a tall phone", async ({
  page,
}) => {
  await page.setViewportSize({ width: 430, height: 852 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/tests/history-browser.html?newchat-controls=1");
  await page.locator(".newchat-access").click();
  const dialog = page.getByRole("dialog", {
    name: "权限与执行环境",
  });
  await expect(dialog).toBeVisible();

  const layout = await dialog.evaluate((sheet) => {
    const scroll = sheet.querySelector<HTMLElement>(".sheet-scroll");
    const options = Array.from(
      sheet.querySelectorAll<HTMLElement>(".permission-options .cmd"),
    );
    const live = Array.from(
      sheet.querySelectorAll<HTMLElement>(".cmd-search button"),
    ).find((button) => button.textContent === "Live");
    if (!scroll || options.length !== 6 || !live) {
      throw new Error("compact permission controls are incomplete");
    }
    return {
      scrollHeight: scroll.scrollHeight,
      clientHeight: scroll.clientHeight,
      liveBottom: live.getBoundingClientRect().bottom,
      sheetBottom: sheet.getBoundingClientRect().bottom,
      minOptionHeight: Math.min(...options.map(
        (button) => button.getBoundingClientRect().height,
      )),
    };
  });

  expect(layout.scrollHeight).toBeLessThanOrEqual(layout.clientHeight + 1);
  expect(layout.liveBottom).toBeLessThanOrEqual(layout.sheetBottom + 1);
  expect(layout.minOptionHeight).toBeGreaterThanOrEqual(44);

  await page.setViewportSize({ width: 1200, height: 800 });
  const desktopDescriptions = await dialog.locator(".permission-options .cmd-ds")
    .evaluateAll((nodes) => nodes.map((node) => {
      const rect = node.getBoundingClientRect();
      return { width: rect.width, height: rect.height };
    }));
  expect(desktopDescriptions).toHaveLength(6);
  expect(desktopDescriptions.every(
    ({ width, height }) => width > 1 && height > 1,
  )).toBe(true);
});

test("new-chat controls keep scrolling for many custom permission profiles", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 400 });
  await page.goto(
    "/tests/history-browser.html?newchat-controls=1&many-profiles=1",
  );
  await page.locator(".newchat-access").click();
  const dialog = page.getByRole("dialog", {
    name: "权限与执行环境",
  });
  await expect(dialog).toBeVisible();

  const scrollState = await dialog.locator(".sheet-scroll").evaluate((scroll) => ({
    clientHeight: scroll.clientHeight,
    scrollHeight: scroll.scrollHeight,
    overflowY: getComputedStyle(scroll).overflowY,
  }));
  expect(scrollState.scrollHeight).toBeGreaterThan(scrollState.clientHeight);
  expect(scrollState.overflowY).toBe("auto");

  const live = dialog.getByRole("button", { name: "Live", exact: true });
  await live.scrollIntoViewIfNeeded();
  await expect(live).toBeInViewport();
});

test("new-chat controls fit when the visual app height is keyboard-sized", async ({
  page,
}) => {
  await page.setViewportSize({ width: 393, height: 852 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/tests/history-browser.html?newchat-controls=1");
  await page.evaluate(() => {
    // Opening the dialog focuses it and re-reads the native viewport. Keep
    // that reading consistent with the mirrored keyboard-sized shell.
    Object.defineProperties(window.visualViewport!, {
      height: { configurable: true, value: 400 },
      offsetTop: { configurable: true, value: 0 },
    });
    window.visualViewport!.dispatchEvent(new Event("resize"));
    document.documentElement.style.setProperty("--app-height", "400px");
    document.documentElement.style.setProperty("--keyboard-inset", "452px");
    document.documentElement.setAttribute("data-short-viewport", "ime");
  });
  await page.locator(".newchat-access").click();
  const dialog = page.getByRole("dialog", {
    name: "权限与执行环境",
  });
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveCSS("max-height", "368px");

  const layout = await dialog.evaluate((sheet) => {
    const scroll = sheet.querySelector<HTMLElement>(".sheet-scroll");
    const live = Array.from(
      sheet.querySelectorAll<HTMLElement>(".cmd-search button"),
    ).find((button) => button.textContent === "Live");
    if (!scroll || !live) throw new Error("permission search controls missing");
    return {
      scrollHeight: scroll.scrollHeight,
      clientHeight: scroll.clientHeight,
      liveBottom: live.getBoundingClientRect().bottom,
      sheetBottom: sheet.getBoundingClientRect().bottom,
    };
  });
  expect(layout.scrollHeight).toBeLessThanOrEqual(layout.clientHeight + 1);
  expect(layout.liveBottom).toBeLessThanOrEqual(layout.sheetBottom + 1);
});

test("Work multi-account controls filter labels and seed a new immutable owner", async ({
  page,
}) => {
  await page.setViewportSize({ width: 720, height: 900 });
  await page.goto("/tests/history-browser.html?profile-sidebar=work");

  await expect(page.getByRole("group", {
    name: "筛选 Codex 账号",
  })).toBeVisible();
  await expect(page.locator(".scard-profile-dot")).toHaveCount(2);

  await page.getByRole("button", { name: "nyx · Stack" }).click();
  await expect(page.locator(".scard")).toHaveCount(1);
  await expect(page.locator(".scard-profile-dot")).toHaveAttribute("aria-label", "Codex 账号：nyx · Stack");
  await page.getByRole("button", { name: "新工作" }).click();
  await expect(page.getByTestId("new-work-profile")).toHaveText("stack");

  await page.getByRole("tab", { name: "Code" }).click();
  await expect(page.getByRole("button", { name: "全部" })).toHaveClass(/active/);
  await expect(page.locator(".scard")).toHaveCount(2);

  await page.getByRole("tab", { name: "Work" }).click();
  await expect(page.getByRole("button", { name: "nyx · Stack" })).toHaveClass(/active/);
  await expect(page.locator(".scard")).toHaveCount(1);

  await page.getByRole("button", { name: "全部" }).click();
  await expect(page.locator(".scard")).toHaveCount(2);
});

test("profile dots stay outside session highlights without shifting titles", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/tests/history-browser.html?profile-sidebar=1&theme=dark");
  const active = page.locator(".scard.active");
  await expect(active).toBeVisible();
  const geometry = await active.evaluate(card => {
    const dot = card.querySelector<HTMLElement>(".scard-profile-dot")!;
    const title = card.querySelector<HTMLElement>(".scard-title")!;
    const sidebar = card.closest(".sessions")!;
    const row = card.getBoundingClientRect();
    const dotRect = dot.getBoundingClientRect();
    const label = title.getBoundingClientRect();
    return { dotRight: dotRect.right, dotLeft: dotRect.left, dotWidth: dotRect.width,
      rowLeft: row.left, sidebarLeft: sidebar.getBoundingClientRect().left,
      titleInset: label.left - row.left, dotCenter: (dotRect.top + dotRect.bottom) / 2,
      titleCenter: (label.top + label.bottom) / 2 };
  });
  expect(geometry.dotRight).toBeLessThan(geometry.rowLeft);
  expect(geometry.dotLeft).toBeGreaterThan(geometry.sidebarLeft);
  expect(geometry.dotWidth).toBeLessThanOrEqual(7);
  expect(geometry.titleInset).toBeLessThanOrEqual(12);
  expect(Math.abs(geometry.dotCenter - geometry.titleCenter)).toBeLessThanOrEqual(1);
  await expect(active.locator(".scard-prev")).toHaveCount(0);
  await active.getByRole("button", { name: "更多操作" }).click();
  await expect(page.getByRole("group", { name: "会话操作" })).toContainText("Codex 账号：nyx · Stack");
});

test("directory action menu dismisses outside and switches cleanly to session actions", async ({ page, isMobile }) => {
  await page.goto("/tests/history-browser.html?profile-sidebar=code&theme=dark");
  const activate = (locator: Locator, position?: { x: number; y: number }) =>
    isMobile ? locator.tap({ position }) : locator.click({ position });
  const trigger = page.getByRole("button", { name: "cc-remote 目录操作", exact: true });
  const menu = page.locator(".card-menu");
  const work = page.getByRole("tab", { name: "Work", exact: true });
  await activate(trigger);
  await expect(menu).toHaveCount(1);
  await activate(menu.locator(".card-menu-account"));
  await expect(menu).toBeVisible();
  await activate(page.locator(".s-head"), { x: 4, y: 4 });
  await expect(menu).toHaveCount(0);
  await activate(trigger);
  // Dismissal consumes the click instead of activating the surface underneath.
  await activate(work);
  await expect(menu).toHaveCount(0);
  await expect(work).toHaveAttribute("aria-selected", "false");
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  await activate(trigger);
  await activate(trigger);
  await expect(menu).toHaveCount(0);
  const card = page.locator(".scard.active");
  await activate(trigger);
  await activate(card.getByRole("button", { name: "更多操作" }));
  await expect(menu).toHaveCount(1);
  await expect(menu.getByRole("button", { name: "重命名", exact: true })).toBeVisible();
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  // The session popup covers this directory trigger; use keyboard activation.
  await trigger.focus();
  await page.keyboard.press("Enter");
  await expect(menu).toHaveCount(1);
  await expect(card.getByRole("button", { name: "更多操作" })).toHaveAttribute("aria-expanded", "false");
  await expect(menu.getByRole("button", { name: "新建会话", exact: true })).toBeVisible();
  const other = page.locator(".scard").filter({ hasText: "cc-remote 派生" });
  // Tap the exposed left edge; the menu can cover the title's center on phones.
  await activate(other.locator(".scard-title"), { x: 4, y: 4 });
  await expect(menu).toHaveCount(0);
  await expect(other).not.toHaveClass(/active/);
  await activate(other.locator(".scard-title"));
  await expect(other).toHaveClass(/active/);
});

test("directory actions keep new-session account scope and confirm archived plus active deletion", async ({ page }, info) => {
  await page.goto("/tests/history-browser.html?profile-sidebar=code&theme=dark");
  const trigger = page.getByRole("button", { name: "cc-remote 目录操作", exact: true });
  await trigger.focus();
  await page.keyboard.press("Enter");
  const menu = page.locator(".card-menu");
  await expect(menu.getByRole("button", { name: "新建会话", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
  await page.getByRole("button", { name: /default.*Main/ }).click();
  await trigger.click();
  await menu.getByRole("button", { name: "新建会话", exact: true }).click();
  await expect(page.getByTestId("new-work-profile")).toHaveText("primary");
  expect(await page.evaluate(() => document.body.dataset.newDirectory)).toBe("/repo/cc-remote");
  await page.getByRole("button", { name: "全部", exact: true }).click();
  await trigger.click();
  await menu.getByRole("button", { name: "删除目录会话…", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "永久删除此目录的会话？" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "删除会话", exact: true })).toBeDisabled();
  expect(await page.evaluate(() => JSON.parse(document.body.dataset.directoryPrepare!))).toEqual({
    cwd: "/repo/cc-remote", engine: "codex", action: "delete",
  });
  const response = async (value: object) => page.evaluate(value => window.dispatchEvent(new CustomEvent("directory-response", { detail: value })), value);
  const plan = { type: "directory_action_plan", request_id: "prepare-1", plan_id: "plan-1", engine: "codex",
    cwd: "/repo/cc-remote", action: "delete", profiles: ["Main", "Stack"], total: 12, eligible: 10,
    archived: 3, skipped: 2, expires_at: Date.now() / 1000 + 120 };
  await response({ ...plan, cwd: "/other" });
  await expect(dialog).not.toContainText("10 个");
  await response(plan);
  await expect(dialog).toContainText("未归档 7 个 · 已归档 3 个");
  await expect(dialog).toContainText("Test Mac · Main、Stack");
  await expect(dialog).toContainText("项目目录和文件保留");
  await page.screenshot({ path: info.outputPath("directory-delete-confirm.png") });
  await dialog.getByRole("button", { name: "删除 10 个会话", exact: true }).click();
  expect(await page.evaluate(() => document.body.dataset.directoryRun)).toBe("plan-1");
  expect(await page.evaluate(() => document.body.dataset.directoryReads)).toBe("1");
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  await response({ type: "directory_action_progress", request_id: "run-1", plan_id: "plan-1", engine: "codex",
    total: 10, succeeded: 8, skipped: 1, failed: 1, done: true, errors: ["一个会话未完成，请核查。"] });
  const result = page.getByRole("dialog", { name: "删除结果" });
  await expect(result).toContainText("8 个成功 · 3 个保留 · 1 个未完成");
  await result.getByRole("button", { name: "完成", exact: true }).click();
  await expect(result).not.toBeVisible();
});

test("directory actions expire without sending a mutation and allow a fresh confirmation", async ({ page }) => {
  await page.goto("/tests/history-browser.html?profile-sidebar=code");
  await page.getByRole("button", { name: "cc-remote 目录操作", exact: true }).click();
  await page.locator(".card-menu").getByRole("button", { name: "归档目录会话…", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "归档此目录的会话？" });
  await page.evaluate(() => window.dispatchEvent(new CustomEvent("directory-response", { detail: {
    type: "directory_action_plan", request_id: "prepare-1", plan_id: "expired", engine: "codex", cwd: "/repo/cc-remote",
    action: "archive", profiles: ["Main"], total: 1, eligible: 1, archived: 0, skipped: 0, expires_at: 1,
  } })));
  await dialog.getByRole("button", { name: "归档 1 个会话", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("清单已过期");
  expect(await page.evaluate(() => document.body.dataset.directoryRun)).toBeUndefined();
  await dialog.getByRole("button", { name: "重新读取" }).click();
  expect(await page.evaluate(() => document.body.dataset.directoryReads)).toBe("2");
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await expect(dialog).not.toBeVisible();
});

test("profile session card manual unread survives refresh until explicit opening", async ({ page }) => {
  await page.goto("/tests/history-browser.html?profile-sidebar=code");
  const active = page.locator(".scard").filter({ hasText: "看看当前仓库" });
  await active.getByRole("button", { name: "更多操作" }).click();
  await page.locator(".card-menu").getByRole("button", { name: "标记为未读" }).click();
  await expect(active).toHaveClass(/active/);
  await expect(active.locator(".pill.completed")).toHaveText("未读");
  await page.reload();
  await expect(active.locator(".pill.completed")).toHaveText("未读");
  await page.goto("/tests/history-browser.html?profile-sidebar=code&machine=another-device");
  await expect(active.locator(".pill.completed")).toHaveCount(0);
  await page.goto("/tests/history-browser.html?profile-sidebar=code");
  await expect(active.locator(".pill.completed")).toHaveText("未读");
  await page.locator(".scard").filter({ hasText: "cc-remote 派生" }).click();
  await active.click();
  await expect(active.locator(".pill.completed")).toHaveCount(0);
  await page.reload();
  await expect(active.locator(".pill.completed")).toHaveCount(0);
});

test("profile session card manual unread stays usable when storage is unavailable", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(() => {
    const mode = new URLSearchParams(location.search).get("unread-storage");
    if (mode === "missing" || mode === "access-denied") {
      Object.defineProperty(window, "localStorage", {
        configurable: true,
        get() {
          if (mode === "access-denied") throw new DOMException("blocked", "SecurityError");
          return undefined;
        },
      });
    } else {
      const getItem = Storage.prototype.getItem;
      const setItem = Storage.prototype.setItem;
      Storage.prototype.getItem = function (key) {
        if (mode === "read-denied" && key === "cc-remote:manual-unread-v1")
          throw new DOMException("blocked", "SecurityError");
        return getItem.call(this, key);
      };
      Storage.prototype.setItem = function (key, value) {
        if (mode === "write-denied" && key === "cc-remote:manual-unread-v1")
          throw new DOMException("full", "QuotaExceededError");
        return setItem.call(this, key, value);
      };
    }
  });
  for (const mode of ["missing", "access-denied", "read-denied", "write-denied"]) {
    await page.goto(`/tests/history-browser.html?profile-sidebar=code&unread-storage=${mode}&machine=storage-${mode}`);
    const active = page.locator(".scard").filter({ hasText: "看看当前仓库" });
    await active.getByRole("button", { name: "更多操作" }).click();
    await page.locator(".card-menu").getByRole("button", { name: "标记为未读" }).click();
    await expect(active.locator(".pill.completed")).toHaveText("未读");
    await page.evaluate(() => {
      // A storage event must not crash or erase local state if access is denied;
      // unrelated/sessionStorage events must never replace these local marks.
      window.dispatchEvent(new StorageEvent("storage", { key: "cc-remote:manual-unread-v1" }));
      window.dispatchEvent(new StorageEvent("storage", { storageArea: sessionStorage }));
    });
    await expect(active.locator(".pill.completed")).toHaveText("未读");
    await active.click();
    await expect(active.locator(".pill.completed")).toHaveCount(0);
    await active.getByRole("button", { name: "更多操作" }).click();
    await page.locator(".card-menu").getByRole("button", { name: "标记为未读" }).click();
    await expect(active.locator(".pill.completed")).toHaveText("未读");
    await page.reload();
    await expect(active.locator(".pill.completed")).toHaveCount(0);
  }
  expect(errors).toEqual([]);
});

test("profile session card manual unread still synchronizes across tabs", async ({ page, context }) => {
  await page.goto("/tests/history-browser.html?profile-sidebar=code");
  const other = await context.newPage();
  try {
    await other.goto("/tests/history-browser.html?profile-sidebar=code");
    const active = page.locator(".scard").filter({ hasText: "看看当前仓库" });
    const otherActive = other.locator(".scard").filter({ hasText: "看看当前仓库" });
    await active.getByRole("button", { name: "更多操作" }).click();
    await page.locator(".card-menu").getByRole("button", { name: "标记为未读" }).click();
    await expect(otherActive.locator(".pill.completed")).toHaveText("未读");
    await otherActive.click();
    await expect(active.locator(".pill.completed")).toHaveCount(0);
    await active.getByRole("button", { name: "更多操作" }).click();
    await page.locator(".card-menu").getByRole("button", { name: "标记为未读" }).click();
    await expect(otherActive.locator(".pill.completed")).toHaveText("未读");
    await other.evaluate(() => localStorage.clear());
    await expect(active.locator(".pill.completed")).toHaveCount(0);
  } finally {
    await other.close();
  }
});

test("profile session card only highlights the selected row in dark theme", async ({ page }) => {
  await page.goto("/tests/history-browser.html?profile-sidebar=1&theme=dark");
  await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");
  const inactive = page.locator(".scard:not(.active)");
  await expect(inactive).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  await expect(inactive).toHaveCSS("border-top-color", "rgba(0, 0, 0, 0)");
  await expect(page.locator(".scard.active")).not.toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
});

for (const engine of ["codex", "claude"] as const) {
  for (const theme of ["light", "dark"] as const) {
    test(`Goal rounded ${engine} card supports native fields and preserves edits (${theme})`, async ({ page }, info) => {
      await page.goto(`/tests/history-browser.html?goal-ui=1&engine=${engine}&theme=${theme}`);
      await page.getByRole("button", { name: /查看 Goal/ }).click();
      const dialog = page.getByRole("dialog", { name: `${engine === "codex" ? "Codex" : "Claude"} Goal` });
      await expect(dialog).toBeVisible();
      await expect(dialog.getByRole("textbox")).toHaveCount(0);
      expect(await dialog.evaluate(node => parseFloat(getComputedStyle(node).borderRadius))).toBeGreaterThanOrEqual(20);
      await expect(dialog.getByText("完成 protocol v30 发布并验证所有终端同步升级", { exact: true })).toHaveCount(1);
      if (engine === "claude") {
        await expect(dialog).toContainText("检查轮次");
        await expect(dialog).toContainText("3 次");
        await expect(dialog).toContainText("最近一次检查");
        await expect(dialog.getByText("Token 预算", { exact: true })).toHaveCount(0);
        await expect(dialog.getByRole("button", { name: "暂停续行" })).toHaveCount(0);
      } else {
        await expect(dialog.getByRole("progressbar", { name: "Token 预算用量" })).toHaveAttribute("value", "37000");
      }
      await page.screenshot({ path: info.outputPath(`${engine}-${theme}-active.png`) });
      await dialog.getByRole("button", { name: "修改目标", exact: true }).click();
      await dialog.getByLabel("目标内容").fill("修复引导后的输出中断，\n并通过回归测试。");
      await page.evaluate(() => window.dispatchEvent(new Event("goal-fixture-refresh")));
      await expect(dialog.getByLabel("目标内容")).toHaveValue("修复引导后的输出中断，\n并通过回归测试。");
      await expect(dialog.getByText("完成 protocol v30 发布并验证所有终端同步升级", { exact: true })).toHaveCount(0);
      if (engine === "codex") {
        await dialog.getByRole("button", { name: /预算 ·/ }).click();
        await dialog.getByLabel("Token 预算", { exact: true }).fill("0");
        await expect(dialog.getByRole("button", { name: "保存修改", exact: true })).toBeDisabled();
        await dialog.getByLabel("Token 预算", { exact: true }).fill("250000");
        await dialog.getByRole("button", { name: "确定", exact: true }).click();
      }
      await page.screenshot({ path: info.outputPath(`${engine}-${theme}-editing.png`) });
      await dialog.getByRole("button", { name: "保存修改", exact: true }).click();
      await expect(dialog.getByRole("textbox")).toHaveCount(0);
      await expect(dialog).toContainText("修复引导后的输出中断");
      if (engine === "codex") await expect(dialog.getByRole("progressbar")).toHaveAttribute("max", "250000");
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    });
  }
}

test("Goal creation uses optional Codex budget and shows native errors without discarding drafts", async ({ page }, info) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=none&goal-open=1&theme=dark");
  const dialog = page.getByRole("dialog", { name: "Codex Goal" });
  await expect(dialog.getByRole("button", { name: "开始目标", exact: true })).toBeDisabled();
  await dialog.getByLabel("目标内容").fill("完善 Goal 界面并通过桌面、手机回归。");
  await expect(dialog.getByRole("button", { name: /预算 · 不限/ })).toBeVisible();
  await page.screenshot({ path: info.outputPath("codex-create.png") });
  await dialog.getByRole("button", { name: "开始目标", exact: true }).click();
  await expect(dialog).toContainText("当前目标");
  await expect(dialog.getByRole("progressbar")).toHaveCount(0);
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-fail=1");
  await page.getByRole("button", { name: /查看 Goal/ }).click();
  await dialog.getByRole("button", { name: "修改目标", exact: true }).click();
  await dialog.getByLabel("目标内容").fill("失败后保留我的目标");
  await dialog.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("输入已保留");
  await expect(dialog.getByLabel("目标内容")).toHaveValue("失败后保留我的目标");
});

test("Goal completed state preserves actual budget usage and nested Escape only closes the budget picker", async ({ page }) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=complete");
  const chip = page.getByRole("button", { name: /查看 Goal/ });
  expect(await chip.locator(".goal-chip-ring").evaluate(node =>
    parseFloat((node as HTMLElement).style.getPropertyValue("--goal-progress")))).toBeCloseTo(133.2);
  await chip.click();
  const dialog = page.getByRole("dialog", { name: "Codex Goal" });
  await expect(dialog.getByRole("progressbar")).toHaveAttribute("value", "37000");
  await dialog.getByRole("button", { name: "修改目标", exact: true }).click();
  await dialog.getByRole("button", { name: /预算 ·/ }).click();
  await dialog.getByLabel("Token 预算", { exact: true }).focus();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel("Token 预算", { exact: true })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});

for (const engine of ["codex", "claude"] as const) {
  test(`Goal ${engine} editor stays usable through mobile keyboard open and close`, async ({ page }, info) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const relay = await mockRightPanelRelay(page, { retained: false, engine });
    await page.goto("/");
    // Initial focus restores the session's composer scope. Wait for that
    // boundary before entering the command whose keyboard layout we test.
    await expect(page.locator(".scard.active")).toContainText("Layout parent");
    const composer = page.locator(".composer textarea");
    await composer.fill("/goal");
    await composer.press("Enter");
    const dialog = page.getByRole("dialog", { name: `${engine === "codex" ? "Codex" : "Claude"} Goal` });
    const editor = dialog.getByLabel("目标内容");
    await expect(editor).toBeVisible();
    await editor.fill("键盘收起后保留目标，恢复完整编辑区。");
    const originalHeight = (await dialog.boundingBox())!.height;
    for (const { height, top } of [{ height: 350, top: 30 }, { height: 280, top: 12 }]) {
      await editor.focus();
      await page.evaluate(({ height, top }) => {
        Object.defineProperties(window.visualViewport!, {
          height: { configurable: true, value: height },
          offsetTop: { configurable: true, value: top },
        });
        window.visualViewport!.dispatchEvent(new Event("resize"));
      }, { height, top });
      await expect.poll(async () => {
        const visible = await dialog.locator(".goal-sheet-scroll").boundingBox();
        return visible?.height ?? 0;
      }).toBeGreaterThan(80);
      await expect.poll(() => dialog.evaluate(node => {
        const box = node.getBoundingClientRect();
        const viewport = window.visualViewport!;
        return box.top >= viewport.offsetTop - 1
          && box.bottom <= viewport.offsetTop + viewport.height + 1;
      })).toBe(true);
      await expect(dialog.getByRole("button", { name: "开始目标", exact: true })).toBeInViewport();
      await expect(editor).toBeFocused();
      await expect(editor).toHaveValue("键盘收起后保留目标，恢复完整编辑区。");
      await page.screenshot({ path: info.outputPath(`goal-${engine}-keyboard-${height}.png`) });
      await editor.blur();
      await page.evaluate(() => {
        Object.defineProperties(window.visualViewport!, {
          height: { configurable: true, value: window.innerHeight },
          offsetTop: { configurable: true, value: 0 },
        });
        window.visualViewport!.dispatchEvent(new Event("resize"));
      });
      await expect.poll(async () => Math.abs((await dialog.boundingBox())!.height - originalHeight)).toBeLessThan(2);
      await expect.poll(() => editor.evaluate(input => {
        const editorBox = input.getBoundingClientRect();
        const bodyBox = input.closest(".goal-sheet-scroll")!.getBoundingClientRect();
        return editorBox.top >= bodyBox.top && editorBox.bottom <= bodyBox.bottom;
      })).toBe(true);
      await expect(editor).toHaveValue("键盘收起后保留目标，恢复完整编辑区。");
    }
    await page.screenshot({ path: info.outputPath(`goal-${engine}-restored.png`) });
    expect(relay.commands.some(command => ["query", "set_goal", "interrupt"].includes(String(command.type)))).toBe(false);
  });
}

test("Goal editor restores after a late keyboard viewport correction without another resize", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=none&goal-open=1");
  const dialog = page.getByRole("dialog", { name: "Codex Goal" });
  const editor = dialog.getByLabel("目标内容");
  await editor.fill("晚到的键盘恢复也不能隐藏这段草稿。");
  const fullHeight = (await dialog.boundingBox())!.height;
  await page.evaluate(() => {
    document.documentElement.style.setProperty("--app-height", "220px");
    document.documentElement.style.setProperty("--app-offset-top", "20px");
    window.dispatchEvent(new Event("resize"));
  });
  await expect.poll(async () => (await dialog.boundingBox())!.height).toBeLessThan(fullHeight - 80);
  // A delayed viewport synchronization is allowed to arrive after the final
  // native resize event. The fixture's chat box itself remains 100dvh tall.
  await page.evaluate(() => {
    document.documentElement.style.setProperty("--app-height", `${window.innerHeight}px`);
    document.documentElement.style.setProperty("--app-offset-top", "0px");
  });
  await expect.poll(async () => Math.abs((await dialog.boundingBox())!.height - fullHeight)).toBeLessThan(2);
  await expect(editor).toBeFocused();
  await expect(editor).toHaveValue("晚到的键盘恢复也不能隐藏这段草稿。");
  await expect.poll(() => editor.evaluate(node => {
    const editorBox = node.getBoundingClientRect();
    const bodyBox = node.closest(".goal-sheet-scroll")!.getBoundingClientRect();
    return editorBox.top >= bodyBox.top && editorBox.bottom <= bodyBox.bottom;
  })).toBe(true);
});

async function openSessionActions(card: Locator) {
  const trigger = card.getByRole("button", { name: "更多操作" });
  if (await card.evaluate(node => matchMedia("(hover: none)").matches && !node.classList.contains("active"))) {
    await card.evaluate(node => {
      const box = node.getBoundingClientRect();
      const event = new Event("touchstart", { bubbles: true });
      Object.defineProperty(event, "touches", { value: [{ clientX: box.x + 10, clientY: box.y + 10 }] });
      node.dispatchEvent(event);
    });
    await expect(card).toHaveClass(/lifting/);
    await card.dispatchEvent("touchend");
  } else {
    await card.hover();
    await trigger.click();
  }
}

for (const viewport of [{ width: 390, height: 560 }, { width: 1280, height: 500 }]) {
  test(`timed task popover stays above the footer at ${viewport.width}px without making the session busy`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await page.goto("/tests/history-browser.html?profile-sidebar=code&timed-tasks");
    const card = page.locator(".scard").last();
    const trigger = card.getByRole("button", { name: "查看定时任务" });
    await trigger.click();
    const panel = page.getByRole("tooltip");
    await expect(panel).toContainText("定时任务仍在处理");
    await expect(panel).toContainText("已发送 2/3 次");
    await expect(card.locator(".pill.running")).toHaveCount(0);
    await expect(card.locator(".timed-task-orbit")).toHaveCount(1);
    const geometry = await panel.evaluate(element => {
      const box = element.getBoundingClientRect();
      return { top: box.top, bottom: box.bottom, left: box.left, right: box.right,
        footer: document.querySelector(".s-foot")!.getBoundingClientRect().top };
    });
    expect(geometry.top).toBeGreaterThanOrEqual(8);
    expect(geometry.left).toBeGreaterThanOrEqual(8);
    expect(geometry.right).toBeLessThanOrEqual(viewport.width - 8);
    expect(geometry.bottom).toBeLessThan(geometry.footer);
    await page.screenshot({ path: testInfo.outputPath("timed-task-popover.png") });
    await page.keyboard.press("Escape");
    await expect(panel).toHaveCount(0);
    await openSessionActions(card);
    await expect(panel).toHaveCount(0);
    await expect(page.locator(".card-menu")).toHaveAttribute("data-placement", "above");
  });
}

test("timed task completion and stale helper stop the orbit while message tags remain", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.clock.install();
  await page.goto("/tests/history-browser.html?profile-sidebar=code&timed-tasks");
  await expect(page.locator(".timed-task-orbit")).toHaveCount(1);
  await expect(page.locator(".timed-message-tag")).toHaveCount(1);
  await expect(page.locator(".ubub").last().locator(".timed-message-tag")).toHaveCount(0);
  await page.locator(".scard").last().hover();
  await expect(page.getByRole("tooltip")).toBeVisible();
  await page.clock.fastForward(91_000);
  await expect(page.locator(".timed-task-orbit")).toHaveCount(0);
  await expect(page.getByRole("tooltip")).toHaveCount(0);
  await expect(page.locator(".timed-message-tag")).toHaveCount(1);
  await page.reload();
  await expect(page.locator(".timed-task-orbit")).toHaveCount(1);
  await page.getByTestId("finish-timed-task").click();
  await expect(page.locator(".timed-task-orbit")).toHaveCount(0);
  await expect(page.locator(".timed-message-tag")).toHaveCount(1);
});

test("timed task reduced motion keeps a stationary outline", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/tests/history-browser.html?profile-sidebar=code&timed-tasks&theme=dark");
  const orbit = page.locator(".timed-task-orbit");
  await expect(orbit).toHaveCount(1);
  expect(await orbit.evaluate(element => getComputedStyle(element).animationName)).toBe("none");
  await page.getByRole("button", { name: "查看定时任务" }).focus();
  await expect(page.getByRole("tooltip")).toBeVisible();
});

for (const viewport of [{ width: 390, height: 560 }, { width: 1280, height: 500 }]) {
  test(`session action menu opens upward above the footer at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await page.goto("/tests/history-browser.html?profile-sidebar=code&theme=dark");
    await openSessionActions(page.locator(".scard").last());
    const menu = page.locator(".card-menu");
    await expect(menu).toHaveAttribute("data-placement", "above");
    const geometry = await menu.evaluate(element => {
      const box = element.getBoundingClientRect();
      const trigger = document.querySelector('.scard-act[aria-expanded="true"]')!.getBoundingClientRect();
      const footer = document.querySelector(".s-foot")!.getBoundingClientRect();
      return {
        top: box.top, bottom: box.bottom, triggerTop: trigger.top, footerTop: footer.top,
        actionsVisible: [...element.querySelectorAll("button")].every(button => {
          const rect = button.getBoundingClientRect();
          return button.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
        }),
      };
    });
    expect(geometry.top).toBeGreaterThanOrEqual(8);
    expect(geometry.bottom).toBeLessThan(geometry.triggerTop);
    expect(geometry.bottom).toBeLessThan(geometry.footerTop);
    expect(geometry.actionsVisible).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("session-menu.png") });
    await menu.getByRole("button", { name: "归档", exact: true }).click();
    await expect(menu).toHaveCount(0);
    await expect(page.locator(".scard.active")).toContainText("看看当前仓库");
  });
}

test("session action menu follows viewport changes and scrolls in a short viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await page.goto("/tests/history-browser.html?profile-sidebar=code");
  const trigger = page.locator(".scard").first().getByRole("button", { name: "更多操作" });
  await trigger.click();
  const menu = page.locator(".card-menu");
  await expect(menu).toHaveAttribute("data-placement", "below");
  // iOS can change its visible region without resizing the layout viewport.
  await page.evaluate(() => {
    Object.defineProperties(window.visualViewport!, {
      height: { configurable: true, value: 330 },
      offsetTop: { configurable: true, value: 80 },
    });
    window.visualViewport!.dispatchEvent(new Event("resize"));
  });
  await expect(menu).toHaveAttribute("data-placement", "above");
  const short = await menu.evaluate(element => ({
    top: element.getBoundingClientRect().top,
    bottom: element.getBoundingClientRect().bottom,
    height: element.clientHeight, content: element.scrollHeight,
  }));
  expect(short.top).toBeGreaterThanOrEqual(88);
  expect(short.bottom).toBeLessThanOrEqual(402);
  expect(short.content).toBeGreaterThan(short.height);
  // Reaching the last action must scroll the menu, not the page underneath it.
  const listScroll = await page.locator(".s-scroll").evaluate(element => element.scrollTop);
  await menu.getByRole("button", { name: "归档", exact: true }).scrollIntoViewIfNeeded();
  expect(await menu.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
  expect(await page.locator(".s-scroll").evaluate(element => element.scrollTop)).toBe(listScroll);
  await page.evaluate(() => {
    Object.defineProperties(window.visualViewport!, {
      height: { configurable: true, value: 900 },
      offsetTop: { configurable: true, value: 0 },
    });
    window.visualViewport!.dispatchEvent(new Event("resize"));
  });
  await expect(menu).toHaveAttribute("data-placement", "below");
  const restored = await menu.evaluate(element => ({ height: element.clientHeight, content: element.scrollHeight }));
  expect(restored.height).toBe(restored.content);
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(trigger).toBeFocused();
});

test("session action menu long press stays above the footer and dismisses outside", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 560 });
  await page.goto("/tests/history-browser.html?profile-sidebar=code");
  const card = page.locator(".scard").last();
  await card.evaluate(element => {
    const box = element.getBoundingClientRect();
    const event = new Event("touchstart", { bubbles: true });
    Object.defineProperty(event, "touches", { value: [{ clientX: box.x + 10, clientY: box.y + 10 }] });
    element.dispatchEvent(event);
  });
  await expect(card).toHaveClass(/lifting/);
  await card.dispatchEvent("touchend");
  const menu = page.locator(".card-menu");
  await expect(menu).toHaveAttribute("data-placement", "above");
  await menu.getByRole("button", { name: "标记为未读" }).click();
  await expect(menu).toHaveCount(0);
  await expect(card.locator(".pill.completed")).toHaveText("未读");
  await openSessionActions(card);
  await page.getByRole("button", { name: "新会话", exact: true }).click();
  await expect(menu).toHaveCount(0);
});

test("session action menu keeps keyboard navigation and closes when its card scrolls away", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 350 });
  await page.goto("/tests/history-browser.html?profile-sidebar=code");
  const trigger = page.locator(".scard").first().getByRole("button", { name: "更多操作" });
  await trigger.focus();
  await page.keyboard.press("Enter");
  const menu = page.locator(".card-menu");
  await expect(menu.getByRole("button", { name: "重命名" })).toBeFocused();
  await page.keyboard.press("End");
  await expect(menu.getByRole("button", { name: "删除会话", exact: true })).toBeFocused();
  await page.keyboard.press("Home");
  await expect(menu.getByRole("button", { name: "重命名" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await trigger.click();
  await page.locator(".s-scroll").evaluate(element => { element.scrollTop = element.scrollHeight; });
  expect(await trigger.evaluate(element => element.getBoundingClientRect().bottom
    <= element.closest(".s-scroll")!.getBoundingClientRect().top)).toBe(true);
  await expect(menu).toHaveCount(0);
});

for (const [engine, color] of [["codex", "#8590FF"], ["claude", "#DB7C5E"], ["dsh", "#94BDFF"]]) {
  test(`working words ${engine} replace token counters without changing native states`, async ({ page }) => {
    await page.clock.install({ time: new Date("2026-11-09T00:00:00Z") });
    await page.goto(`/tests/history-browser.html?turn-usage&engine=${engine}&theme=dark`);
    const working = page.locator(".turn-working");
    const word = working.locator(".working-word-layer.is-current");
    await expect(word).toHaveText(/\S+\.\.\.$/);
    const initial = await word.textContent();
    await expect(page.getByRole("button", { name: "查看 token 用量" })).toHaveCount(0);
    await expect(working).not.toContainText("tokens");
    await expect(word).toHaveCSS("animation-duration", "3s, 0.25s");
    await expect(word).toHaveCSS("--working-word-base", color);
    if (engine === "claude") {
      await expect(working.locator(".is-alternating > img")).toHaveCSS(
        "animation-name", "claude-working-entry, claude-working-alternating");
    } else {
      await expect(working.locator(".claude-working-mark")).toHaveCount(0);
      await expect(working.locator("svg")).toBeVisible();
    }
    await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000));
    await page.getByRole("button", { name: "更新用量" }).evaluate(el => (el as HTMLButtonElement).click());
    await expect(word).toHaveText(initial!);
    await page.clock.runFor(8100);
    await expect(word).not.toHaveText(initial!);
    const rotated = await word.textContent();
    await page.getByRole("button", { name: "切换会话显示" }).click();
    await expect(working).toHaveCount(0);
    await page.getByRole("button", { name: "切换会话显示" }).click();
    await expect(word).toHaveText(rotated!);
    await page.getByRole("button", { name: "压缩提示" }).click();
    await expect(working).toContainText("正在压缩上下文");
    await expect(word).toHaveCount(0);
    await page.getByRole("button", { name: "结束任务" }).click();
    await expect(working).toHaveCount(0);
    await expect(page.locator(".turn-done-mark")).toBeVisible();
  });
}

test("working words Claude sprite stays centered across cycles at fractional zoom", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/tests/history-browser.html?turn-usage&engine=claude&theme=dark");
  const mark = page.locator(".turn-working .claude-working-mark");
  await mark.locator("img").evaluate(async (node: HTMLImageElement) => {
    await node.decode();
    node.getAnimations().forEach(animation => animation.pause());
  });
  for (const zoom of [0.8, 0.9, 1, 1.1, 1.25]) {
    await mark.evaluate((node, zoom) => {
      (node.closest(".turn-working") as HTMLElement).style.zoom = String(zoom);
    }, zoom);
    const positions: number[] = [];
    // These times all show the same small dot, at the beginning/end of loops.
    // Inspect painted pixels: a fixed DOM box cannot detect drift inside an SVG.
    for (const time of [451, 2441, 3261, 5251, 6071]) {
      await mark.locator("img").evaluate((node, time) => {
        node.getAnimations().forEach(animation => { animation.currentTime = time; });
      }, time);
      const pixels = (await mark.screenshot()).toString("base64");
      positions.push(await page.evaluate(async pixels => {
        const image = new Image();
        image.src = `data:image/png;base64,${pixels}`;
        await image.decode();
        const canvas = document.createElement("canvas");
        canvas.width = image.width; canvas.height = image.height;
        const context = canvas.getContext("2d")!;
        context.drawImage(image, 0, 0);
        const rgba = context.getImageData(0, 0, canvas.width, canvas.height).data;
        const background = rgba[0] - rgba[2];
        let weight = 0, center = 0;
        for (let y = 0; y < canvas.height; y++) for (let x = 0; x < canvas.width; x++) {
          const offset = (y * canvas.width + x) * 4;
          const orange = Math.max(0, rgba[offset] - rgba[offset + 2] - background);
          if (orange <= 10) continue;
          weight += orange; center += (x + 0.5) * orange;
        }
        if (!weight) throw new Error("The Claude mark disappeared between frames");
        return center / weight * 24 / canvas.width;
      }, pixels));
    }
    // Allow subpixel rasterization differences, but not the old multi-pixel drift.
    expect(Math.max(...positions) - Math.min(...positions), `painted drift at zoom ${zoom}`).toBeLessThan(0.5);
  }
});

test("working words preserve the original Claude completed click replay", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.clock.install({ time: new Date("2026-11-09T00:00:00Z") });
  await page.goto("/tests/history-browser.html?turn-usage&engine=claude");
  await page.getByRole("button", { name: "结束任务" }).click();
  const mark = page.locator(".engine-done-mark");
  await expect(mark.locator(".engine-brand-icon.claude")).toBeVisible();
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000));
  await mark.click();
  const sprite = mark.locator(".claude-working-mark > img");
  await expect(sprite).toHaveCSS("animation-name", "claude-working-frames");
  await expect(sprite).toHaveCSS("animation-duration", "0.81s");
  await expect(sprite).toHaveCSS("animation-iteration-count", "1");
  await expect(mark.locator(".is-alternating")).toHaveCount(0);
  await page.clock.runFor(810);
  await expect(mark.locator(".engine-brand-icon.claude")).toBeVisible();
  await mark.press("Enter");
  await expect(sprite).toBeVisible();
  await page.clock.runFor(810);
  await expect(mark.locator(".engine-brand-icon.claude")).toBeVisible();
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mark.click();
  await expect(sprite).toHaveCount(0);
});


for (const scenario of [
  { name: "native", engine: "codex", targetEngine: "codex", account: "iris", native: true },
  { name: "same-account", engine: "claude", targetEngine: "claude", account: "iris" },
  { name: "cross-account", engine: "claude", targetEngine: "claude", account: "stack" },
  { name: "claude-to-codex", engine: "claude", targetEngine: "codex", account: "stack" },
  { name: "codex-to-claude", engine: "codex", targetEngine: "claude", account: "stack" },
] as const) {
  test(`session message ${scenario.name} links navigate the real conversation`, async ({ page }, testInfo) => {
    const originSid = "iris@receiver";
    const targetSid = `${scenario.account}@source`;
    const native = "native" in scenario;
    const sourceRef = native ? "source" : `ccr:${scenario.targetEngine}:${targetSid}`;
    const returnRef = native ? "receiver" : `ccr:${scenario.engine}:${originSid}`;
    const origin = { session_id: originSid, engine: scenario.engine, space: "code" as const,
      summary: "代码审查", cwd: "/tmp/layout", state: "idle" as const, last_modified: "100" };
    const target = { session_id: targetSid, engine: scenario.targetEngine, space: "code" as const,
      summary: "消息来源会话", cwd: "/tmp/source", state: "idle" as const, last_modified: "50" };
    const relay = await mockRightPanelRelay(page, {
      engine: scenario.engine, parentSid: originSid, retained: false,
      listReply: command => ({ type: "session_list", engine: command.engine as "claude" | "codex",
        space: command.space as "code" | "work", request_id: String(command.cmd_id),
        sessions: command.space === "code" ? [origin, target].filter(s => s.engine === command.engine) : [] }),
      historyReply: command => {
        const sid = String(command.session_id);
        return { type: "history", session_id: sid, sid, revision: `history-${sid}`,
          generation: "layout-generation", detail: "summary", events: [], has_more: false,
          turns: [{ id: `turn-${sid}`, prompt: sid === originSid ? "请审查消息来源" : "目标会话的完整历史",
            sourceThreadId: sid === originSid ? sourceRef : undefined,
            sessionMessages: sid === targetSid ? [{ itemId: "sent", threadId: returnRef, status: "sent" }] : [],
            done: true, blocks: [...(sid === targetSid ? [{ kind: "tool" as const,
              message_id: "send-assistant", tool_use_id: "sent",
              tool: native ? "mcp__codex_app__send_message_to_thread" : "mcp__cc_remote_messages__send_message",
              category: "mcp" as const, done: true,
              input: native ? { threadId: returnRef, input: "交接说明\n已完成验证，请继续。" }
                : { session_ref: returnRef, message: "交接说明\n已完成验证，请继续。", request_id: "handoff-test" },
              result: { content: '{"status":"accepted"}', is_error: false },
            }] : []), { kind: "text", message_id: `answer-${sid}`, done: true,
              text: sid === originSid ? "原会话回答" : "这里是可继续交流的目标会话" }] }] };
      },
    });
    await page.goto("/");
    const composer = page.locator(".composer textarea");
    const source = page.getByRole("button", { name: /来自会话 · 消息来源会话/ });
    await expect(source).toBeEnabled();
    await composer.fill("原会话未发送的草稿");
    const before = relay.commands.length;
    await source.click();
    await expect.poll(() => relay.commands.slice(before).filter(c => c.type === "switch_session")
      .map(c => [c.session_id, c.engine ?? "claude", c.space ?? "code"]))
      .toEqual([[targetSid, scenario.targetEngine, "code"]]);
    await expect(page.getByText("目标会话的完整历史", { exact: true })).toBeVisible();
    await expect(page.getByRole("dialog", { name: "关联会话历史" })).toHaveCount(0);
    await expect(page.getByText("只读查看", { exact: true })).toHaveCount(0);
    await expect(page.locator("html")).toHaveAttribute("data-engine", scenario.targetEngine);
    await expect(composer).toBeEditable();
    await expect(composer).toHaveValue("");
    await composer.fill("目标会话自己的草稿");
    await page.screenshot({ path: testInfo.outputPath("session-direct-navigation.png") });
    await expect(page.locator(".session-message-sent")).toHaveCount(0);
    await page.locator(".turn-process-head").click();
    await page.locator(".tool-group-h").click();
    const tool = page.locator(".session-message-tool");
    await expect(tool.locator(".session-message-tool-status")).toHaveText("已发送");
    await tool.locator(".tool-h").click();
    await expect(tool.locator(".session-message-tool-content")).toContainText("交接说明\n已完成验证，请继续。");
    await expect(tool.locator(".session-message-tool-account")).toHaveText("iris");
    await expect(tool.getByText('{"status":"accepted"}', { exact: true })).toHaveCount(0);
    await tool.locator("summary", { hasText: "原始输出" }).click();
    await expect(tool.getByText('{"status":"accepted"}', { exact: true })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("session-message-tool.png") });
    await tool.getByRole("button", { name: "进入会话 代码审查", exact: true }).click();
    await expect(page.getByText("原会话回答", { exact: true })).toBeVisible();
    await expect(composer).toHaveValue("原会话未发送的草稿");
    await expect(page.locator("html")).toHaveAttribute("data-engine", scenario.engine);
    expect(relay.commands.slice(before).filter(c => c.type === "switch_session")
      .map(c => [c.session_id, c.engine ?? "claude", c.space ?? "code"]))
      .toEqual([[targetSid, scenario.targetEngine, "code"], [originSid, scenario.engine, "code"]]);
    expect(relay.commands.slice(before).filter(c => ["query", "create_session", "interrupt", "takeover_session"]
      .includes(String(c.type)))).toEqual([]);
    // A removed target stays labelled, but cannot navigate using a stale cache.
    const listRequest = relay.commands.findLast(c => c.type === "list_sessions"
      && c.engine === scenario.targetEngine && c.space === "code")!;
    relay.emit({ type: "session_list", engine: scenario.targetEngine, space: "code",
      request_id: String(listRequest.cmd_id),
      sessions: scenario.targetEngine === scenario.engine ? [origin] : [] });
    await expect(page.getByRole("button", { name: /来自会话/ })).toBeDisabled();
  });
}

for (const centered of [false, true]) {
  test(`right panel layout centered side chat overlays ${centered ? "centered" : "docked"} editor without replacing it`, async ({ page }, info) => {
    await page.setViewportSize({ width: 1568, height: 1000 });
    const path = "/tmp/layout/overview.md";
    const relay = await mockRightPanelRelay(page, { seedTurns: [{ id: "source", prompt: "查看文档", done: true,
      blocks: [{ kind: "text", message_id: "answer", text: `[技术方案](${path})`, done: true }] }] });
    await page.goto("/");
    await page.getByRole("button", { name: `在 Remote 中打开 ${path}`, exact: true }).click();
    await expect.poll(() => relay.commands.some(c => c.type === "get_file_preview")).toBe(true);
    const request = relay.commands.find(c => c.type === "get_file_preview")!;
    relay.emit({ type: "file_preview", sid: "layout-parent", path, request_id: String(request.request_id),
      format: "markdown", content: "# 技术方案\n\n保留当前内容", size: 32, mtime_ns: "1", revision: "a".repeat(64), writable: true });
    const file = page.locator(".artifact-panel");
    await file.getByRole("button", { name: "源码", exact: true }).click();
    const editor = file.getByRole("textbox", { name: "Markdown 源码编辑器" });
    await editor.fill("# 未保存的草稿\n\n侧聊不应丢弃它");
    const node = await editor.elementHandle();
    if (centered) await file.getByRole("button", { name: "居中展开文件预览" }).click();
    let confirmations = 0;
    page.on("dialog", async dialog => { confirmations++; await dialog.dismiss(); });
    for (const close of ["shortcut", "escape", "backdrop"]) {
      await page.keyboard.press("Control+Shift+e");
      const btw = page.locator(".btw-panel");
      await expect(btw).toHaveAttribute("aria-modal", "true");
      await expect(btw.locator("textarea")).toBeFocused();
      await expect(file).toHaveAttribute("inert", "");
      expect(await node!.evaluate(el => el.isConnected)).toBe(true);
      expect(await editor.inputValue()).toContain("未保存的草稿");
      await btw.getByRole("button", { name: "收回右侧", exact: true }).focus();
      await page.keyboard.press("/");
      await expect(btw.locator("textarea")).toBeFocused();
      await page.keyboard.press("Shift+Tab");
      expect(await btw.evaluate(el => el.contains(document.activeElement))).toBe(true);
      if (close === "shortcut") await page.keyboard.press("Control+Shift+e");
      else if (close === "escape") await page.keyboard.press("Escape");
      else await page.locator(".panel-expanded-backdrop.panel-chat-layer").click({ position: { x: 4, y: 4 } });
      await expect(btw).toHaveCount(0);
      await expect(file).not.toHaveAttribute("inert");
      if (centered) await expect(file).toHaveClass(/panel-expanded/);
      else await expect(file).not.toHaveClass(/panel-expanded/);
    }
    expect(confirmations).toBe(0);
    expect(await node!.evaluate(el => el.isConnected)).toBe(true);
    expect(relay.commands.filter(c => ["query", "steer", "get_file_preview", "save_markdown"].includes(String(c.type)))).toHaveLength(1);
    if (centered) {
      await page.keyboard.press("Control+Shift+e");
      await page.screenshot({ animations: "disabled", path: info.outputPath("file-under-centered-chat.png") });
    }
  });
}

const ARTIFACT_THUMBNAIL = "UklGRtwCAABXRUJQVlA4INACAABwFgCdASqgAGQAPmkskUWkIqGYrQ2AQAaEoA0yImv9/wfKu8jGc1fj1NZwv9jP2Z7D285+g70wWRO+LY6qs6n0dvyfQA/XRQB23osL5Oml9nMS4gNItz/BqkDYvgLeLSEm08g24to6NGBdwgvlda3JG+dJOhgXbkzpQgaXwvKFiucB0JgYGCCrW2xsMKYgri2Bh3wIA3LPS1vqF4VCEH5XMfezkei12+V6oH2DOqg/4rIIaAyQMsP3Su+LwAD+/L0kmiyLtH7qy5e7S7xMQ0RpoCM4bi2O9b1Hx1RUnmE+vjjVs4XHpkwxpI/pFBxd/MXRYmC1H1g1yF9yf5CD+8/A1yi0UxY0dZaaXjkNbh93x1lXGdhNwgT7ondUHCAZekBFY3IYNVfU6dMwzF4/CH7CPGiL0Teb1Dusbmu9G5dhjPkoyXCsJDeNwZ/rQ5QoRAEFaui98RXfOcyaHd6m7cEkY+yb4LukxgkFIlbgg2xc+wpZPnVjVPTOApxiG1MuiCwjW4B6PP89Yjmp9/DBbGAwutiYT+ZQrfyxzuxpfqjJ+X6QQYxqSHRJsz8ELfwjNNdtZinkYO0mP/GJzD0gijO7d74/4Mj1zKh+hWzf8VyXcCXoX141t07IRnXWHujnMfNNup2EEJVcSqC3/BGkMVAVxA+aIGPe7k3rH23ah/K4nG/Z/gtGdHuKP4dMKVMOC6+baEyer1F9psT4Zm6vo45c1o9Ac4HtZinkYO0li0Z2/nWWQ6l8f8EvIz1+mWZTwAYP/f6qSmVV876qwMQY3eCpgTJd17CWpzW05cAhbyJZaX/INkj1DPQy2hYKkNhhL1Qh567JeVGDBkbZWnAl+DFAwEO6Nm5Kd13BxrU74C3n3537tm2uoqs4xbNC9rEGo1qNpj+vM+lddxhgEdkQnTFjCo2dPrAWTZ80TDkSdWH5uqHDRaVoz/ZG6AYAAAAAAAA=";

for (const format of ["image", "html", "markdown"] as const) {
  test(`right panel layout ${format} file controls stay stable while loading and reloading`, async ({ page }, info) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.setViewportSize({ width: 320, height: 740 });
    const extension = { image: "webp", html: "html", markdown: "md" }[format];
    const path = `/tmp/layout/exec-bcecc55b-6c70-4858-8033-38343e2ed9f2.${extension}`;
    const relay = await mockRightPanelRelay(page, { seedTurns: [{ id: "file", prompt: "查看文件", done: true,
      blocks: [{ kind: "text", message_id: "answer", text: `[交付文件](${path})`, done: true }] }] });
    await page.goto("/");
    await page.getByRole("button", { name: `在 Remote 中打开 ${path}`, exact: true }).click();
    const requests = () => relay.commands.filter(c => c.type === "get_file_preview");
    await expect.poll(() => requests().length).toBe(1);
    const header = page.locator(".artifact-preview-head");
    const disabledDownload = header.getByRole("button", { name: "下载原文件", exact: true });
    await expect(disabledDownload).toBeVisible();
    await expect(disabledDownload).toBeDisabled();
    const bounds = (await disabledDownload.boundingBox())!;
    const headerBounds = (await header.boundingBox())!;
    const closeBounds = (await header.getByRole("button", { name: "收起", exact: true }).boundingBox())!;
    await expect(header.getByRole("link", { name: "下载原文件", exact: true })).toHaveCount(0);
    if (format !== "image") {
      await expect(header.getByRole("button", { name: "预览", exact: true })).toBeDisabled();
      await expect(header.getByRole("button", { name: "源码", exact: true })).toBeDisabled();
    }
    const content = format === "html" ? "<!doctype html><p>测试文件</p>" : "# 测试文件";
    const respond = (index: number, error?: string) => relay.emit({ type: "file_preview", sid: "layout-parent",
      request_id: String(requests()[index].request_id), path, format,
      ...(error ? { error } : format === "image"
        ? { data: ARTIFACT_THUMBNAIL, media_type: "image/webp" } : { content }), size: 740, writable: false });
    respond(0);
    const download = header.getByRole("link", { name: "下载原文件", exact: true });
    await expect(download).toBeVisible();
    expect(await download.boundingBox()).toEqual(bounds);
    expect(await header.boundingBox()).toEqual(headerBounds);
    expect(await header.getByRole("button", { name: "收起", exact: true }).boundingBox()).toEqual(closeBounds);
    expect(await header.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
    const previousUrl = await download.getAttribute("href");
    const event = page.waitForEvent("download");
    await download.click();
    const file = await event;
    expect(file.suggestedFilename()).toBe(path.split("/").at(-1));
    expect(readFileSync((await file.path())!)).toEqual(
      format === "image" ? Buffer.from(ARTIFACT_THUMBNAIL, "base64") : Buffer.from(content));

    const openInfo = () => header.getByRole("button", { name: /^文件信息：/ }).click();
    await openInfo();
    const menu = page.getByRole("dialog", { name: "文件信息", exact: true });
    const reload = menu.getByRole("button", { name: "重新读取", exact: true });
    expect((await reload.boundingBox())!.width).toBeLessThan((await menu.boundingBox())!.width * .6);
    await page.locator(".artifact-panel").screenshot({ path: info.outputPath(`${format}-compact-controls.png`) });
    await reload.click();
    await expect.poll(() => requests().length).toBe(2);
    await expect(disabledDownload).toBeDisabled();
    expect(await disabledDownload.boundingBox()).toEqual(bounds);
    await expect(download).toHaveCount(0);
    await expect.poll(() => page.evaluate(async url => {
      try { await fetch(url!); return false; } catch { return true; }
    }, previousUrl)).toBe(true);
    respond(1, "文件读取失败");
    await expect(page.locator(".preview-error")).toContainText("文件读取失败");
    await expect(disabledDownload).toBeDisabled();
    expect(await header.boundingBox()).toEqual(headerBounds);
    await openInfo();
    await reload.click();
    await expect.poll(() => requests().length).toBe(3);
    respond(2);
    await expect(download).toBeVisible();
    expect(await download.boundingBox()).toEqual(bounds);
    expect(await download.getAttribute("href")).not.toBe(previousUrl);
  });
}

test("right panel layout session Artifacts list and preview stay separate and reject stale responses", async ({ page }, info) => {
  await page.setViewportSize({ width: 1568, height: 1000 });
  const relay = await mockRightPanelRelay(page, { secondParent: true, seedTurns: [{
    id: "artifact-turn", prompt: "帮我整理项目的技术架构和路线图", done: true,
    blocks: [{ kind: "text", message_id: "artifact-answer", done: true, text: "已整理好技术方案和路线图，可以在 Artifacts 中查看。" }],
  }] });
  await page.goto("/");
  const open = async () => {
    await page.getByRole("button", { name: "更多设置", exact: true }).click();
    await page.getByRole("button", { name: "打开 Artifacts", exact: true }).click();
  };
  await open();
  await expect.poll(() => relay.commands.filter(c => c.type === "get_session_artifacts").length).toBe(1);
  const first = relay.commands.find(c => c.type === "get_session_artifacts")!;
  const entries = ["技术架构.md", "路线图与部署流程（最终交付版本）.png", "会议记录.txt"].map((name, i) => ({
    id: `artifact-${i}`, name, path: `/tmp/layout/${name}`, source: i === 2 ? "upload" as const : "delivered" as const,
    kind: i === 1 ? "image" as const : "document" as const, created_at: (Date.now() / 1000) - (i === 2 ? 86400 : i), available: true, size: 1024,
  }));
  relay.emit({ type: "session_artifacts", sid: "layout-parent", request_id: String(first.request_id),
    entries, next_before: 1, has_more: false, history_more: true });
  await expect.poll(() => relay.commands.filter(c => c.type === "get_session_artifacts").length).toBe(2);
  const scan = relay.commands.filter(c => c.type === "get_session_artifacts")[1];
  expect(scan.scan_history).toBe(true);
  relay.emit({ type: "session_artifacts", sid: "layout-parent", request_id: String(scan.request_id),
    entries, next_before: 1, has_more: false, history_more: true });
  const panel = page.locator(".session-artifacts-panel");
  await expect(panel.locator(".session-artifact-row")).toHaveCount(3);
  await expect(panel.locator(".artifact-panel")).toHaveCount(0);
  await expect(panel.locator(".session-artifacts-date")).toHaveText(["今天", "昨天"]);
  await expect.poll(() => relay.commands.some(c => c.type === "get_session_artifact_preview" && c.variant === "thumbnail")).toBe(true);
  const thumbnail = relay.commands.find(c => c.type === "get_session_artifact_preview" && c.variant === "thumbnail")!;
  relay.emit({ type: "file_preview", sid: "layout-parent", request_id: String(thumbnail.request_id),
    path: entries[1].path, format: "image", media_type: "image/webp", data: ARTIFACT_THUMBNAIL, size: 740, writable: false });
  await expect(panel.locator(".session-artifact-icon img")).toHaveCount(1);
  await page.screenshot({ animations: "disabled", path: info.outputPath("session-artifacts-list.png") });
  await panel.getByRole("button", { name: /技术架构.md/ }).click();
  await expect.poll(() => relay.commands.some(c => c.type === "get_session_artifact_preview" && c.variant !== "thumbnail")).toBe(true);
  const preview = relay.commands.find(c => c.type === "get_session_artifact_preview" && c.variant !== "thumbnail")!;
  relay.emit({ type: "file_preview", sid: "layout-parent", request_id: String(preview.request_id), path: entries[0].path,
    format: "markdown", content: "# 技术架构\n\n## 连接方式\n\n客户端通过 Relay 连接设备上的 Wrapper。\n\n## 会话资料\n\n每个会话分别保存自己的附件和交付文件。", size: 200, mtime_ns: "1", writable: false });
  await expect(panel.locator(".session-artifacts-list")).toBeHidden();
  await expect(panel.locator(".artifact-panel")).toBeVisible();
  await expect(panel.getByText("每个会话分别保存自己的附件和交付文件。", { exact: true })).toBeVisible();
  await page.screenshot({ animations: "disabled", path: info.outputPath("session-artifacts-preview.png") });
  await page.keyboard.press("Control+Shift+e");
  await expect(page.locator(".btw-panel")).toHaveClass(/panel-expanded/);
  await page.keyboard.press("Control+Shift+e");
  await expect(panel.locator(".artifact-panel")).toBeVisible();
  await panel.getByRole("button", { name: "返回 Artifacts" }).click();
  await expect(panel.locator(".session-artifact-row")).toHaveCount(3);
  await expect(panel.locator(".artifact-panel")).toHaveCount(0);
  await page.setViewportSize({ width: 320, height: 740 });
  await panel.getByRole("button", { name: /路线图与部署流程/ }).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "get_session_artifact_preview" && c.variant !== "thumbnail").length).toBe(2);
  const imageRequest = relay.commands.filter(c => c.type === "get_session_artifact_preview" && c.variant !== "thumbnail")[1];
  relay.emit({ type: "file_preview", sid: "layout-parent", request_id: String(imageRequest.request_id),
    path: entries[1].path, format: "image", media_type: "image/webp", data: ARTIFACT_THUMBNAIL, size: 740, writable: false });
  const imageHeader = panel.locator(".artifact-preview-head");
  await expect(imageHeader.getByRole("link", { name: "下载原文件" })).toBeVisible();
  await expect(imageHeader.locator(".artifact-preview-toolbar")).toHaveCount(0);
  expect((await imageHeader.boundingBox())!.height).toBeLessThan(80);
  expect(await imageHeader.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
  await imageHeader.getByRole("button", { name: `文件信息：${entries[1].name}`, exact: true }).click();
  await expect(panel.getByRole("dialog", { name: "文件信息", exact: true })).toContainText(entries[1].path);
  await panel.locator(".artifact-image-stage img").click();
  await expect(panel.getByRole("dialog", { name: "文件信息", exact: true })).toHaveCount(0);
  await imageHeader.screenshot({ animations: "disabled", path: info.outputPath("image-preview-header.png") });
  await panel.getByRole("button", { name: "返回 Artifacts", exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  const button = panel.getByRole("button", { name: "查看更早的文件", exact: true });
  await expect(button).toBeVisible();
  const bounds = await button.boundingBox();
  const container = await panel.boundingBox();
  expect(bounds!.width).toBeLessThan(220);
  expect(Math.abs(bounds!.x + bounds!.width / 2 - container!.x - container!.width / 2)).toBeLessThan(2);
  expect(await button.evaluate(el => Number.parseFloat(getComputedStyle(el).borderRadius))).toBeGreaterThan(20);
  await page.screenshot({ animations: "disabled", path: info.outputPath("session-artifacts-mobile.png") });
  await page.getByText("Second parent", { exact: true }).evaluate(el => (el as HTMLElement).click());
  await expect(panel).toHaveCount(0);
  await open();
  await expect.poll(() => relay.commands.some(c => c.type === "get_session_artifacts" && c.sid === "layout-other")).toBe(true);
  relay.emit({ type: "session_artifacts", sid: "layout-parent", request_id: String(scan.request_id),
    entries, next_before: 1, has_more: false, history_more: true });
  await expect(panel.locator(".session-artifact-row")).toHaveCount(0);
  expect(relay.commands.some(c => ["query", "steer", "interrupt"].includes(String(c.type)))).toBe(false);
});

test("right panel layout Artifacts thumbnails load visible rows without replacing previews or crossing sessions", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const relay = await mockRightPanelRelay(page, { secondParent: true });
  await page.goto("/");
  const open = async () => {
    await page.getByRole("button", { name: "更多设置", exact: true }).click();
    await expect(page.locator(".header-menu-title")).toHaveCount(0);
    await page.getByRole("button", { name: "打开 Artifacts", exact: true }).click();
  };
  const lists = () => relay.commands.filter(c => c.type === "get_session_artifacts");
  const thumbs = () => relay.commands.filter(c => c.type === "get_session_artifact_preview" && c.variant === "thumbnail");
  await open();
  await expect.poll(() => lists().length).toBe(1);
  relay.emit({ type: "session_artifacts", sid: "layout-parent", request_id: String(lists()[0].request_id),
    entries: [], has_more: false, history_more: false });
  await expect.poll(() => lists().length).toBe(2);
  const entries = Array.from({ length: 40 }, (_, i) => ({
    id: `image-${i}`, name: `图-${i}.png`, path: `/tmp/layout/${i}.png`,
    kind: "image" as const, source: "upload" as const, created_at: Date.now() / 1000 - i, available: true, size: 1024,
  }));
  relay.emit({ type: "session_artifacts", sid: "layout-parent", request_id: String(lists()[1].request_id),
    entries, has_more: false, history_more: false });
  await expect.poll(() => thumbs().length).toBe(2);
  const respond = (request: Record<string, unknown>, error = false, sid = "layout-parent") => {
    relay.emit({ type: "file_preview", sid, request_id: String(request.request_id),
      path: entries.find(e => e.id === request.artifact_id)!.path, format: "image", media_type: "image/webp",
      ...(error ? { error: "unavailable" } : { data: ARTIFACT_THUMBNAIL }), size: 740, writable: false });
  };
  const panel = page.locator(".session-artifacts-panel");
  // Even an otherwise matching request ID cannot accept a foreign session's bytes.
  respond(thumbs()[0], false, "layout-other");
  await expect(panel.locator("img")).toHaveCount(0);
  respond(thumbs()[0]);
  await expect(panel.locator(".session-artifact-icon img")).toHaveCount(1);
  await expect.poll(() => thumbs().length).toBe(3);
  respond(thumbs()[1], true);
  await expect.poll(() => thumbs().length).toBe(4);
  await expect(panel.locator(".artifact-panel")).toHaveCount(0);
  expect(thumbs().some(c => c.artifact_id === "image-39")).toBe(false);
  await panel.getByRole("button", { name: /图-0.png/ }).click();
  const previewRequests = () => relay.commands.filter(c => c.type === "get_session_artifact_preview" && c.variant !== "thumbnail");
  await expect.poll(() => previewRequests().length).toBe(1);
  const full = previewRequests()[0];
  respond(thumbs()[2]); // Late thumbnail must not satisfy the full-image request.
  await expect(panel.locator(".artifact-panel")).toBeVisible();
  relay.emit({ type: "file_preview", sid: "layout-parent", request_id: String(full.request_id), path: entries[0].path,
    format: "image", media_type: "image/webp", data: ARTIFACT_THUMBNAIL, size: 740, writable: false });
  await expect(panel.locator(".artifact-panel img")).toBeVisible();
  await page.getByText("Second parent", { exact: true }).evaluate(el => (el as HTMLElement).click());
  await expect(panel).toHaveCount(0);
  await open();
  respond(thumbs()[3]);
  await expect(panel.locator("img")).toHaveCount(0);
  await expect(panel.locator(".artifact-panel")).toHaveCount(0);
  expect(relay.commands.some(c => ["query", "steer", "interrupt"].includes(String(c.type)))).toBe(false);
});

test("right panel layout iframe forwards centered-chat shortcut only from the focused preview", async ({ page }) => {
  const path = "/tmp/layout/page.html";
  const relay = await mockRightPanelRelay(page, { seedTurns: [{ id: "html", prompt: "预览", done: true,
    blocks: [{ kind: "text", message_id: "html-answer", text: `[页面](${path})`, done: true }] }] });
  await page.goto("/");
  await page.getByRole("button", { name: `在 Remote 中打开 ${path}`, exact: true }).click();
  await expect.poll(() => relay.commands.some(c => c.type === "get_file_preview")).toBe(true);
  const request = relay.commands.find(c => c.type === "get_file_preview")!;
  relay.emit({ type: "file_preview", sid: "layout-parent", path, request_id: String(request.request_id),
    format: "html", content: '<input aria-label="Preview input" value="keep this">', size: 60, mtime_ns: "1", writable: false });
  const panel = page.locator(".artifact-panel");
  await panel.getByRole("button", { name: "居中展开文件预览" }).click();
  await page.evaluate(() => window.postMessage({ type: "cc-remote-preview-chat" }, "*"));
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  const input = panel.frameLocator("iframe").getByRole("textbox");
  await input.fill("retained frame state");
  await input.press("Control+Shift+e");
  await expect(page.locator(".btw-panel")).toHaveClass(/panel-expanded/);
  await page.keyboard.press("Control+Shift+e");
  await expect(page.locator(".btw-panel")).toHaveCount(0);
  await expect(panel).toHaveClass(/panel-expanded/);
  await expect(input).toHaveValue("retained frame state");
});


test("right panel layout Artifacts retries the failed history page and retains the file list position", async ({ page }) => {
  await page.setViewportSize({ width: 1568, height: 1000 });
  const relay = await mockRightPanelRelay(page);
  await page.goto("/");
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  await page.getByRole("button", { name: "打开 Artifacts", exact: true }).click();
  const requests = () => relay.commands.filter(c => c.type === "get_session_artifacts");
  await expect.poll(() => requests().length).toBe(1);
  relay.emit({ type: "session_artifacts", sid: "layout-parent", request_id: String(requests()[0].request_id),
    entries: [], has_more: false, history_more: false });
  await expect.poll(() => requests().length).toBe(2);
  relay.emit({ type: "session_artifacts", sid: "layout-parent", request_id: String(requests()[1].request_id),
    entries: [], has_more: false, history_more: false, error: "历史暂时不可用" });
  const panel = page.locator(".session-artifacts-panel");
  await panel.getByRole("button", { name: "重试", exact: true }).click();
  await expect.poll(() => requests().length).toBe(3);
  expect(requests()[2].scan_history).toBe(true);
  const entries = Array.from({ length: 40 }, (_, i) => ({
    id: `entry-${i}`, name: `报告-${i}.md`, path: `/tmp/layout/report-${i}.md`,
    source: "delivered" as const, kind: "document" as const, created_at: 2000 - i, available: true, size: 1,
  }));
  relay.emit({ type: "session_artifacts", sid: "layout-parent", request_id: String(requests()[2].request_id),
    entries, next_before: 40, has_more: true, history_before: "older-turn", history_more: true });
  await panel.getByRole("button", { name: "查看更早的文件", exact: true }).click();
  await expect.poll(() => requests().length).toBe(4);
  expect(requests()[3]).toMatchObject({ before: 40, scan_history: false });
  relay.emit({ type: "session_artifacts", sid: "layout-parent", request_id: String(requests()[3].request_id),
    entries: [{ ...entries[0], id: "last", name: "末尾报告.md", path: "/tmp/layout/last.md", created_at: 1000 }],
    next_before: 41, has_more: false, history_more: false });
  await expect(panel.locator(".session-artifact-row")).toHaveCount(41);
  const last = panel.getByRole("button", { name: /末尾报告.md/ });
  await last.scrollIntoViewIfNeeded();
  const scrollTop = await panel.locator(".session-artifacts-scroll").evaluate(el => el.scrollTop);
  expect(scrollTop).toBeGreaterThan(0);
  await last.click();
  await panel.getByRole("button", { name: "返回 Artifacts", exact: true }).click();
  await expect.poll(() => panel.locator(".session-artifacts-scroll").evaluate(el => el.scrollTop)).toBe(scrollTop);
  await panel.getByRole("button", { name: "查看更早的文件", exact: true }).click();
  await expect.poll(() => requests().length).toBe(5);
  expect(requests()[4]).toMatchObject({ before: 41, scan_history: true, history_before: "older-turn" });
});

for (const settlement of ["success", "error", "timeout"] as const) {
  test(`right panel layout Artifacts queues a head refresh behind an older page (${settlement})`, async ({ page }) => {
    await page.setViewportSize({ width: 1568, height: 1000 });
    await page.clock.install();
    const relay = await mockRightPanelRelay(page);
    await page.goto("/");
    await page.getByRole("button", { name: "更多设置", exact: true }).click();
    await page.getByRole("button", { name: "打开 Artifacts", exact: true }).click();
    const requests = () => relay.commands.filter(c => c.type === "get_session_artifacts");
    const entry = (id: string, created_at: number) => ({ id, created_at, name: `${id}.md`,
      path: `/tmp/layout/${id}.md`, source: "delivered" as const, kind: "document" as const,
      available: true, size: 1 });
    const respond = (index: number, entries: ReturnType<typeof entry>[], before: number, error?: string) => {
      relay.emit({ type: "session_artifacts", sid: "layout-parent", request_id: String(requests()[index].request_id),
        entries, next_before: before, has_more: true, history_before: "older-turn", history_more: true, error });
    };
    await expect.poll(() => requests().length).toBe(1);
    respond(0, [entry("head", 100)], 40);
    await expect.poll(() => requests().length).toBe(2);
    respond(1, [], 40);
    const panel = page.locator(".session-artifacts-panel");
    await panel.getByRole("button", { name: "查看更早的文件", exact: true }).click();
    await expect.poll(() => requests().length).toBe(3);
    respond(2, [entry("older", 50)], 20);
    await panel.getByRole("button", { name: "查看更早的文件", exact: true }).click();
    await expect.poll(() => requests().length).toBe(4);
    expect(requests()[3]).toMatchObject({ before: 20, scan_history: false });
    // Multiple live revisions must coalesce, leaving the page request intact.
    for (const state of ["running", "idle"] as const) {
      relay.emit({ type: "state", sid: "layout-parent", state });
      await expect(page.getByText(state, { exact: true }).first()).toBeVisible();
    }
    expect(requests()).toHaveLength(4);
    if (settlement === "timeout") await page.clock.fastForward(30_001);
    else respond(3, settlement === "success" ? [entry("oldest", 25)] : [], 10,
      settlement === "error" ? "暂时不可用" : undefined);
    await expect.poll(() => requests().length).toBe(5);
    expect(requests()[4]).toMatchObject({ before: null, scan_history: false });
    respond(4, [entry("new-delivery", 200)], 40);
    await expect(panel.getByRole("button", { name: /new-delivery.md/ })).toBeVisible();
    await expect(panel.getByRole("button", { name: /older.md/ })).toBeVisible();
    if (settlement === "timeout") {
      respond(3, [entry("expired-response", 300)], 1);
      await expect(panel.getByRole("button", { name: /expired-response.md/ })).toHaveCount(0);
    }
    await panel.getByRole("button", { name: "查看更早的文件", exact: true }).click();
    await expect.poll(() => requests().length).toBe(6);
    expect(requests()[5]).toMatchObject({ before: settlement === "success" ? 10 : 20, scan_history: false });
    expect(relay.commands.some(c => ["query", "steer", "interrupt"].includes(String(c.type)))).toBe(false);
  });
}

test("right panel layout Artifacts retries expired thumbnails and ignores their late responses", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.clock.install();
  const relay = await mockRightPanelRelay(page, { secondParent: true });
  await page.goto("/");
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  await page.getByRole("button", { name: "打开 Artifacts", exact: true }).click();
  const lists = () => relay.commands.filter(c => c.type === "get_session_artifacts");
  const thumbs = () => relay.commands.filter(c => c.type === "get_session_artifact_preview" && c.variant === "thumbnail");
  await expect.poll(() => lists().length).toBe(1);
  relay.emit({ type: "session_artifacts", sid: "layout-parent", request_id: String(lists()[0].request_id),
    entries: [], has_more: false, history_more: false });
  await expect.poll(() => lists().length).toBe(2);
  const entries = [0, 1].map(i => ({ id: `retry-${i}`, name: `${i}.png`, path: `/tmp/layout/${i}.png`,
    source: "upload" as const, kind: "image" as const, created_at: 100 - i, available: true, size: 1 }));
  relay.emit({ type: "session_artifacts", sid: "layout-parent", request_id: String(lists()[1].request_id),
    entries, has_more: false, history_more: false });
  await expect.poll(() => thumbs().length).toBe(2);
  await page.clock.fastForward(30_001);
  await expect.poll(() => thumbs().length).toBe(4);
  expect(new Set(thumbs().map(c => c.request_id)).size).toBe(4);
  expect(thumbs().slice(2).map(c => c.artifact_id).sort()).toEqual(["retry-0", "retry-1"]);
  const respond = (index: number) => {
    const request = thumbs()[index];
    relay.emit({ type: "file_preview", sid: "layout-parent", request_id: String(request.request_id),
      path: entries.find(e => e.id === request.artifact_id)!.path,
      format: "image", media_type: "image/webp", data: ARTIFACT_THUMBNAIL, size: 740, writable: false });
  };
  const panel = page.locator(".session-artifacts-panel");
  respond(0);
  respond(1);
  await expect(panel.locator(".session-artifact-icon img")).toHaveCount(0);
  respond(2);
  await expect(panel.locator(".session-artifact-icon img")).toHaveCount(1);
  await page.clock.fastForward(30_001);
  await expect.poll(() => thumbs().length).toBe(5);
  respond(4);
  await expect(panel.locator(".session-artifact-icon img")).toHaveCount(2);
  await page.clock.fastForward(30_001);
  expect(thumbs()).toHaveLength(5);
  // A new pending thumbnail must be cancelled when its owner panel unmounts.
  await panel.getByRole("button", { name: "刷新 Artifacts", exact: true }).click();
  await expect.poll(() => thumbs().length).toBe(7);
  await page.getByText("Second parent", { exact: true }).evaluate(el => (el as HTMLElement).click());
  await expect(panel).toHaveCount(0);
  await page.clock.fastForward(30_001);
  expect(thumbs()).toHaveLength(7);
  expect(relay.commands.some(c => ["query", "steer", "interrupt"].includes(String(c.type)))).toBe(false);
});

for (const action of ["sidebar", "panel"]) {
  test(`right panel layout preserves the reading paragraph during ${action} changes`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1000, height: 881 });
    await mockRightPanelRelay(page, {
      seedTurns: [{
        id: "reading-long", prompt: "长段落", done: true,
        blocks: [{
          kind: "text", message_id: "reading-answer", channel: "final", done: true,
          text: Array.from({ length: 24 }, (_, i) => `段落${i}：${
            "这是一段用于检查阅读位置的长段落。收起和展开侧栏时，应保留用户正在阅读的内容。".repeat(6)
          }`).join("\n\n"),
        }],
      }],
    });
    await page.goto("/");
    const thread = page.locator(".pane .thread");
    await expect(thread.locator(".prose p").nth(12)).toBeAttached();
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(450);
    await thread.evaluate(el => {
      const paragraph = el.querySelectorAll(".prose p")[12];
      el.dispatchEvent(new WheelEvent("wheel", { bubbles: true, deltaY: -100 }));
      el.scrollTop += paragraph.getBoundingClientRect().top - el.getBoundingClientRect().top;
      el.dispatchEvent(new Event("scroll"));
    });
    await waitForScrollIdle(page);
    const frames = await page.evaluate(async action => {
      const el = document.querySelector<HTMLElement>(".pane .thread")!;
      const paragraph = el.querySelectorAll(".prose p")[12];
      const prose = el.querySelector(".prose")!;
      const frames: { width: number; y: number; scroll: number }[] = [];
      const record = () => frames.push({
        width: prose.getBoundingClientRect().width,
        y: paragraph.getBoundingClientRect().top - el.getBoundingClientRect().top,
        scroll: el.scrollTop,
      });
      record();
      window.dispatchEvent(new KeyboardEvent("keydown", {
        key: action === "sidebar" ? "b" : "K",
        ctrlKey: true, shiftKey: action === "panel", bubbles: true,
      }));
      const start = performance.now();
      await new Promise<void>(resolve => {
        const step = () => {
          record();
          if (performance.now() - start > 750) resolve();
          else requestAnimationFrame(step);
        };
        requestAnimationFrame(step);
      });
      return frames;
    }, action);
    await testInfo.attach(`reading-${action}.json`, {
      body: JSON.stringify(frames), contentType: "application/json",
    });
    expect(new Set(frames.map(frame => Math.round(frame.width))).size).toBeLessThanOrEqual(2);
    expect(Math.max(...frames.map(frame => Math.abs(frame.y - frames[0].y)))).toBeLessThan(2);
  });
}

for (const engine of ["claude", "codex", "dsh"]) {
  for (const working of [false, true]) {
    test(`desktop wheel scrolling preserves cold measured rows after switching to ${engine} ${working ? "working" : "complete"}`, async ({ page, isMobile }, testInfo) => {
      test.skip(isMobile, "Mobile WebKit has no native mouse wheel; touch cases run separately.");
      await page.goto(`/tests/history-browser.html?large=40&paragraphs=12&engine=${engine}&remount-session=1${working ? "&interactive-timeline=1" : ""}`);
      await expect(page.locator(".thread")).toBeVisible();
      await page.getByTestId("switch-session").click();
      await expect(page.locator('[data-turn-id="b4"]')).toBeAttached();
      await page.getByTestId("switch-session").click();
      await expect(page.locator(`[data-turn-id="${working ? "streaming" : "m40"}"]`)).toBeAttached();
      await page.waitForTimeout(400);
      await page.locator(".thread").evaluate(el => {
        const probe = {
          active: true, writes: 0, writtenDistance: 0,
          frames: [] as { t: number; movement: number; scroll: number; written: number }[],
        };
        Object.assign(window, { coldWheelProbe: probe });
        const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, "scrollTop")!;
        Object.defineProperty(el, "scrollTop", {
          configurable: true,
          get: () => descriptor.get!.call(el),
          set: (value: number) => {
            const before = descriptor.get!.call(el) as number;
            descriptor.set!.call(el, value);
            probe.writes++;
            probe.writtenDistance += el.scrollTop - before;
          },
        });
        let previous = new Map<string, { y: number; height: number }>();
        const frame = () => {
          const top = el.getBoundingClientRect().top;
          const next = new Map<string, { y: number; height: number }>();
          const movements: number[] = [];
          for (const row of el.querySelectorAll<HTMLElement>("[data-turn-id]")) {
            const rect = row.getBoundingClientRect();
            const y = rect.top - top;
            next.set(row.dataset.turnId!, { y, height: rect.height });
            const old = previous.get(row.dataset.turnId!);
            if (old && old.y < el.clientHeight && old.y + old.height > 0) {
              movements.push(y - old.y);
            }
          }
          if (movements.length) probe.frames.push({
            t: performance.now(),
            movement: movements.sort((a, b) => a - b)[Math.floor(movements.length / 2)],
            scroll: el.scrollTop, written: probe.writtenDistance,
          });
          previous = next;
          if (probe.active) requestAnimationFrame(frame);
        };
        requestAnimationFrame(frame);
      });
      const box = await page.locator(".thread").boundingBox();
      await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
      for (let i = 0; i < 80; i++) {
        await page.mouse.wheel(0, -60);
        await page.waitForTimeout(35);
      }
      await page.waitForTimeout(350);
      const probe = await page.evaluate(() => {
        const probe = (window as unknown as { coldWheelProbe: {
          active: boolean; writes: number;
          frames: { t: number; movement: number; scroll: number; written: number }[];
        } }).coldWheelProbe;
        probe.active = false;
        return probe;
      });
      await testInfo.attach("cold-wheel-frames.json", {
        body: JSON.stringify(probe), contentType: "application/json",
      });
      const movements = probe.frames.map(frame => frame.movement);
      // Permit two native ticks in one painted frame, but never an extra
      // estimate-to-measurement jump (the regression displaced 277px / tick).
      expect(Math.max(...movements)).toBeLessThanOrEqual(122);
      expect(Math.min(...movements)).toBeGreaterThanOrEqual(-2);
      // A visually stable viewport must not hide a measurement/scroll loop.
      expect(probe.writes).toBeLessThan(80);
    });
  }
}

async function swipeImageGallery(page: import("@playwright/test").Page,
  dx: number, dy = 0, cancel = false) {
  await page.locator(".image-lightbox").evaluate(async (node, input) => {
    const stage = node as HTMLElement;
    Object.defineProperties(stage, {
      setPointerCapture: { configurable: true, value: () => {} },
      releasePointerCapture: { configurable: true, value: () => {} },
      hasPointerCapture: { configurable: true, value: () => false },
    });
    const x = stage.clientWidth / 2;
    const y = stage.clientHeight / 2;
    const emit = (type: string, x: number, y: number) => stage.dispatchEvent(new PointerEvent(type, {
      bubbles: true, cancelable: true, pointerId: 91, pointerType: "touch", clientX: x, clientY: y,
      buttons: type === "pointerdown" || type === "pointermove" ? 1 : 0,
    }));
    emit("pointerdown", x, y);
    emit("pointermove", x + input.dx, y + input.dy);
    await new Promise(requestAnimationFrame);
    emit(input.cancel ? "pointercancel" : "pointerup", x + input.dx, y + input.dy);
    stage.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true,
      clientX: x + input.dx, clientY: y + input.dy }));
  }, { dx, dy, cancel });
}

test("image gallery browses uploads and generated images with lazy originals and retry", async ({ page }) => {
  await page.goto("/tests/history-browser.html?image-gallery=1");
  const first = page.locator('[data-turn-id="uploads"] .ubub-imgs button').first();
  await first.click();
  const counter = page.locator(".image-lightbox-navigation [role=status]");
  const image = page.locator(".image-lightbox-image");
  await expect(counter).toHaveText("1 / 5");
  const firstSource = await image.getAttribute("src");
  await expect(page.getByRole("button", { name: "上一张图片" })).toBeDisabled();
  await expect(page.getByTestId("image-requests")).toHaveText("");
  await swipeImageGallery(page, -120);
  await expect(counter).toHaveText("2 / 5");
  await expect(image).not.toHaveAttribute("src", firstSource!);
  await expect(page.getByTestId("image-requests")).toHaveText("");
  await swipeImageGallery(page, -120);
  await expect(counter).toHaveText("3 / 5");
  await expect(page.getByTestId("image-requests")).toHaveText("history-native/upload/full");
  // Keep the cached thumbnail visible until its original arrives.
  await expect(image).toHaveAttribute("src", firstSource!);
  await expect(image).not.toHaveAttribute("src", firstSource!);
  await expect(counter).toHaveText("3 / 5");
  await swipeImageGallery(page, -120);
  await expect(counter).toHaveText("4 / 5");
  await expect(image).toBeVisible();
  await swipeImageGallery(page, -120);
  await expect(counter).toHaveText("5 / 5");
  await expect(page.getByRole("button", { name: "下一张图片" })).toBeDisabled();
  await expect(page.getByText("图片加载失败", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await expect(image).toBeVisible();
  await swipeImageGallery(page, -120);
  await expect(counter).toHaveText("5 / 5");
  await swipeImageGallery(page, 120);
  await expect(counter).toHaveText("4 / 5");
  // Session focus must never leave another session's gallery on screen.
  await page.getByRole("button", { name: "Switch session" }).evaluate((button: HTMLButtonElement) => button.click());
  await expect(page.locator(".image-lightbox")).toHaveCount(0);
});

for (const failure of ["error", "timeout"]) {
  test(`image gallery retries an original after ${failure} while retaining its thumbnail`, async ({ page }) => {
    await page.clock.install();
    await page.goto(`/tests/history-browser.html?image-gallery=1&original-failure=${failure}`);
    await page.locator('[data-turn-id="history"] .ubub-imgs button').click();
    const preview = page.getByRole("dialog", { name: "图片预览" });
    const image = preview.locator(".image-lightbox-image");
    const requests = page.getByTestId("image-requests");
    await expect(image).toBeVisible();
    const thumbnail = await image.getAttribute("src");
    await expect(requests).toHaveText("history-native/upload/full");
    if (failure === "timeout") await page.clock.fastForward(15_001);
    await expect(preview.getByText("图片加载失败", { exact: true })).toBeVisible();
    await expect(image).toHaveAttribute("src", thumbnail!);
    const retry = preview.getByRole("button", { name: "重试", exact: true });
    await retry.click();
    // Retrying must neither dismiss the gallery nor change its selected image.
    await expect(preview).toBeVisible();
    await expect(preview.locator(".image-lightbox-navigation [role=status]")).toHaveText("3 / 5");
    await expect(requests).toHaveText("history-native/upload/full,history-native/upload/full");
    await expect(image).not.toHaveAttribute("src", thumbnail!);
    await expect(retry).toHaveCount(0);
    await expect(preview.getByText(/图片加载失败|正在加载图片/)).toHaveCount(0);
    await page.keyboard.press("ArrowLeft");
    await expect(preview.locator(".image-lightbox-navigation [role=status]")).toHaveText("2 / 5");
    await expect(preview.getByText(/图片加载失败|正在加载图片/)).toHaveCount(0);
  });
}

test("pending composer image gallery separates swiping from zoom, vertical drag and cancellation", async ({ page }) => {
  await page.goto("/tests/history-browser.html?image-gallery=1");
  await page.getByRole("button", { name: "预览待发送图片 1", exact: true }).click();
  const counter = page.locator(".image-lightbox-navigation [role=status]");
  await expect(counter).toHaveText("1 / 3");
  for (const [dx, dy, cancel] of [[-20, 0, false], [-100, 150, false], [-120, 0, true]] as const) {
    await swipeImageGallery(page, dx, dy, cancel);
    await expect(counter).toHaveText("1 / 3");
  }
  await swipeImageGallery(page, -120);
  await expect(counter).toHaveText("2 / 3");
  const gesture = await pinchThenPanPreview(page);
  expect(gesture.afterPinch.scale).toBeGreaterThan(1);
  await swipeImageGallery(page, -120);
  await expect(counter).toHaveText("2 / 3");
  await page.getByRole("button", { name: "下一张图片" }).click();
  await expect(counter).toHaveText("3 / 3");
  await expect.poll(() => page.locator(".image-lightbox-image").evaluate((node: HTMLElement) => {
    const matrix = new DOMMatrix(node.style.transform);
    return [matrix.a, matrix.e, matrix.f];
  })).toEqual([1, 0, 0]);
  await page.keyboard.press("ArrowLeft");
  await expect(counter).toHaveText("2 / 3");
  await page.getByRole("button", { name: "关闭图片预览" }).click();
  await expect(page.locator(".attach-image-preview")).toHaveCount(3);
});

test("Claude plan completion action fits beside the header on narrow phones", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 700 });
  await page.goto("/tests/history-browser.html?image-gallery=1&plan-header=1");
  const button = page.getByRole("button", { name: "确认已完成" });
  await expect(button).toBeVisible();
  const layout = await page.locator(".plan-progress-content > header").evaluate((header) => {
    const button = header.querySelector("button")!;
    const title = header.querySelector("b")!;
    const b = button.getBoundingClientRect(), h = header.getBoundingClientRect(), t = title.getBoundingClientRect();
    return { rightOfTitle: b.left > t.right, fits: b.right <= h.right + 1 && b.bottom <= h.bottom + 1,
      rounded: getComputedStyle(button).borderRadius === "999px", scrollTop: header.parentElement!.parentElement!.scrollTop };
  });
  expect(layout).toEqual({ rightOfTitle: true, fits: true, rounded: true, scrollTop: 0 });
  await button.click();
  await expect(page.locator(".plan-progress-popover")).toHaveCount(0);
});

test("Claude fresh query waits for live output without flashing a history detail retry", async ({ page }) => {
  const relay = await mockRightPanelRelay(page, { engine: "claude", retained: false,
    seedTurns: [{ id: "prior", prompt: "previous", done: true,
      detailLoaded: true, detailEventCount: 0, blocks: [{
        kind: "text", message_id: "prior-answer", channel: "final",
        text: "Previous answer", done: true,
      }],
    }],
  });
  await page.goto("/");
  await expect(page.getByText("Previous answer", { exact: true })).toBeVisible();
  await page.locator("textarea").fill("hi claude");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect.poll(() => relay.commands.some(c => c.type === "query")).toBe(true);
  const query = relay.commands.find(c => c.type === "query")!;
  const sid = "layout-parent", turnId = String(query.msg_id);
  const turn = page.locator(`.turn[data-turn-id="${turnId}"]`);
  relay.emit({ type: "state", sid, state: "running", seq: 209 });
  relay.emit({ type: "user_msg", sid, msg_id: turnId, prompt: "hi claude", seq: 210 });
  relay.emit({ type: "turn_binding", sid, msg_id: turnId, turn_id: "native-user", seq: 211 });
  await expect(turn.locator(".turn-working")).toContainText("思考中");
  // A new native transcript may not yet be readable. Previously this recovery
  // request produced a transient “重试加载详情” before the first thinking delta.
  await page.waitForTimeout(250);
  expect(relay.commands.filter(c => c.type === "get_turn_detail")).toEqual([]);
  await expect(turn.locator(".turn-detail-entry")).toHaveCount(0);
  await expect(turn.locator(".turn-process-head")).toHaveCount(0);

  relay.emit({ type: "assistant_msg_start", sid, turn_id: turnId,
    message_id: "thinking", channel: "thinking", seq: 213 });
  relay.emit({ type: "delta", sid, turn_id: turnId, message_id: "thinking",
    channel: "thinking", text: "Preparing a short reply.", seq: 214 });
  await expect(turn.locator(".turn-process-head")).toContainText("正在处理");
  await expect(turn.locator(".process-reasoning")).toContainText("Preparing a short reply.");
  await expect(turn.locator(".turn-working")).toContainText("处理中");
  await expect(turn.locator(".turn-detail-entry")).toHaveCount(0);
  expect(relay.commands.filter(c => c.type === "get_turn_detail")).toEqual([]);

  relay.emit({ type: "assistant_msg_end", sid, turn_id: turnId,
    message_id: "thinking", channel: "thinking", seq: 215 });
  relay.emit({ type: "delta", sid, turn_id: turnId, message_id: "answer",
    channel: "final", text: "Hello!", seq: 216 });
  relay.emit({ type: "turn_end", sid, turn_id: "native-user", seq: 217,
    result: { subtype: "success", duration_ms: 1000, is_error: false } });
  await page.waitForTimeout(250);
  expect(relay.commands.filter(c => c.type === "get_turn_detail")).toEqual([]);
  relay.emit({ type: "state", sid, state: "idle", seq: 218 });
  await expect(turn.getByText("Hello!", { exact: true })).toBeVisible();
  await expect(turn.locator(".turn-working")).toHaveCount(0);
  await expect(turn.locator(".turn-detail-entry")).toHaveCount(0);
  expect(relay.commands.filter(c => c.type === "get_turn_detail")).toEqual([]);
});

test("turn regressions Claude active detail stays segmented after AskUser and a task return", async ({ page }) => {
  const fixture = JSON.parse(readFileSync("tests/fixtures/claude-responses.json", "utf8"));
  const sid = "active-detail", root = fixture.summary.id;
  const relay = await mockRightPanelRelay(page, { engine: "claude", parentSid: sid,
    retained: false, seedTurns: [] });
  await page.goto("/");
  await expect.poll(() => relay.commands.some(c => c.type === "get_history")).toBe(true);
  let seq = 100;
  const emit = (body: Record<string, unknown>) => relay.emit({ sid, seq: seq++, ...body } as PanelRelayEvent);
  const source = fixture.events.slice(0, -1).map((body: Record<string, unknown>) =>
    ({ v: PROTOCOL_VERSION, ...body, sid, turn_id: root }));
  for (const [index, body] of source.entries()) {
    if (index === 9) {
      emit({ type: "turn_end", turn_id: root,
        result: { subtype: "success", duration_ms: 8000, is_error: false } });
      emit({ type: "state", state: "running", msg_id: root, continuation: true });
      emit({ type: "process", turn_id: root, item_id: "live-hook",
        kind: "hook", phase: "end", status: "succeeded", title: "Hook" });
    }
    emit(body);
  }
  const ask = { v: PROTOCOL_VERSION, type: "tool_use", sid, turn_id: root, ts: 2411,
    message_id: "ask-message", tool_use_id: "ask-tool",
    tool: "mcp__cc-remote-ask__ask_user", input: {}, background: true };
  source.push(ask);
  emit(ask);
  emit({ type: "ask_user", ask_id: "pending-question", question: "是否继续检查？",
    options: [{ label: "继续" }], allow_text: false });
  const dialog = page.getByRole("dialog", { name: "操作确认" });
  await expect(dialog).toBeVisible();
  emit({ type: "process", turn_id: root, item_id: "late-task", kind: "task",
    phase: "end", status: "succeeded", title: "Task finished", background: true });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "继续", exact: true }).click();
  await expect.poll(() => relay.commands.some(c => c.type === "answer_question"
    && c.ask_id === "pending-question")).toBe(true);
  await expect(dialog).toHaveCount(0);
  emit({ type: "ask_user_closed", ask_id: "pending-question", reason: "answered" });
  const answer = { v: PROTOCOL_VERSION, type: "tool_result", sid, turn_id: root, ts: 2500,
    tool_use_id: "ask-tool", content: "Continue", is_error: false, background: true };
  source.push(answer);
  emit(answer);
  emit({ type: "tool_use", turn_id: root, ts: 2501, message_id: "current-tool",
    tool_use_id: "current-tool", tool: "Bash", input: { command: "echo checking" }, background: true });
  const refresh = () => emit({ type: "history", session_id: sid,
    revision: "layout-history", generation: "layout-generation", detail: "summary",
    in_progress: true, has_more: false, events: [], turns: [fixture.summary] });
  const detail = () => emit({ type: "turn_detail", session_id: sid, turn_id: root,
    revision: "layout-history", authoritative: true, has_more: false, has_newer: false, events: source });
  refresh();
  detail();
  const turn = page.locator(`[data-turn-id="${root}"]`);
  const heads = turn.locator(".turn-process-head");
  await expect(heads).toHaveCount(6);
  // A reader scrolls away from the live tail before opening an older response.
  // Programmatic scrollIntoView alone does not release follow mode on iOS.
  await page.locator(".thread").dispatchEvent("wheel", { deltaY: -600 });
  for (let index = 1; index <= 4; index++) {
    const head = heads.nth(index);
    await expect(head).toContainText("2 项");
    if (await head.getAttribute("aria-expanded") !== "true") await head.click();
    await expect(turn.getByText(`Inspect result ${index - 1}`, { exact: true })).toBeVisible();
    refresh();
    detail();
    await expect(head).toHaveAttribute("aria-expanded", "true");
    await expect(turn.getByText(`Inspect result ${index - 1}`, { exact: true })).toBeVisible();
    await expect(turn.getByText("详细过程未完整返回，请重试", { exact: true })).toHaveCount(0);
    await head.click();
    await expect(head).toHaveAttribute("aria-expanded", "false");
  }
  await expect(turn.locator(".assistant-answer-segment")).toHaveCount(5);
  expect(relay.commands.some(c => ["query", "steer", "interrupt"].includes(String(c.type)))).toBe(false);
});

for (const firstHeader of [0, 1, 2, 3, 4]) {
test(`turn regressions Claude cold history keeps response headers through expand collapse and reload (${firstHeader})`, async ({ page }) => {
  const fixture = JSON.parse(readFileSync("tests/fixtures/claude-responses.json", "utf8"));
  const sid = "response-history";
  const relay = await mockRightPanelRelay(page, { engine: "claude", parentSid: sid,
    retained: false, seedTurns: [fixture.summary] });
  await page.setViewportSize({ width: 1200, height: 1000 });
  await page.goto("/");
  const turn = page.locator('[data-turn-id="human-response"]');
  const heads = turn.locator(".turn-process-head");
  const headerText = async () => (await heads.allTextContents()).map(text => text.replace(/\s/g, ""));
  await expect(heads).toHaveCount(5);
  const cold = ["已处理8s2项", ...Array(4).fill("已处理10s2项")];
  await expect.poll(headerText).toEqual(cold);
  const answerNodes = await turn.locator(".assistant-answer-segment").elementHandles();
  expect(answerNodes).toHaveLength(5);
  expect(relay.commands.filter(c => c.type === "get_turn_detail")).toHaveLength(0);
  // This is a fresh browser context: no persisted detail can hide the cold
  // summary problem. Expanding the original header requests the parent's detail.
  await heads.nth(firstHeader).click();
  await expect.poll(() => relay.commands.filter(c => c.type === "get_turn_detail").length).toBe(1);
  relay.emit({ type: "turn_detail", sid, session_id: sid, turn_id: fixture.summary.id,
    revision: "layout-history", authoritative: true, has_more: false, has_newer: false,
    events: fixture.events.map((event: Record<string, unknown>) => ({ v: PROTOCOL_VERSION, ...event })) });
  await expect(turn.getByText(firstHeader === 0 ? "Inspect the original job" : `Inspect result ${firstHeader - 1}`, { exact: true })).toBeVisible();
  for (let index = 0; index < 5; index += 1) {
    await expect(heads.nth(index)).toHaveAttribute("aria-expanded", String(index === firstHeader));
    expect(await answerNodes[index].evaluate(node => node.isConnected)).toBe(true);
  }
  await expect.poll(headerText).toEqual(cold);
  await heads.nth(firstHeader).click();
  await expect(heads.nth(firstHeader)).toHaveAttribute("aria-expanded", "false");
  await expect.poll(headerText).toEqual(cold);
  await heads.nth(1).click();
  await expect(turn.getByText("Inspect result 0", { exact: true })).toBeVisible();
  await expect(heads.nth(1)).toHaveAttribute("aria-expanded", "true");
  relay.emit({ type: "history", sid, session_id: sid,
    revision: "layout-history", generation: "layout-generation", detail: "summary",
    in_progress: false, has_more: false, events: [],
    turns: [{ ...fixture.summary, prompt: "Check the background results (refreshed)" }] });
  await expect(turn.getByText("Check the background results (refreshed)", { exact: true })).toBeVisible();
  await expect(heads.nth(1)).toHaveAttribute("aria-expanded", "true");
  await expect(turn.getByText("Inspect result 0", { exact: true })).toBeVisible();
  for (const answer of answerNodes) expect(await answer.evaluate(node => node.isConnected)).toBe(true);
  await heads.nth(1).click();
  await expect.poll(headerText).toEqual(cold);
  expect(relay.commands.filter(c => c.type === "get_turn_detail")).toHaveLength(1);
  await page.reload();
  await expect(heads).toHaveCount(5);
  await expect.poll(headerText).toEqual(cold);
  expect(relay.commands.some(c => ["query", "steer", "interrupt"].includes(String(c.type)))).toBe(false);
});
}

for (const choice of ["同意回退", "拒绝回退", "编辑提问"] as const) {
test(`Claude model fallback ${choice} uses the native pending dialog on desktop and mobile`, async ({ page }, testInfo) => {
  const relay = await mockRightPanelRelay(page, { engine: "claude", parentState: "running" });
  await page.goto("/");
  await expect.poll(() => relay.commands.some(command => command.type === "switch_session")).toBe(true);
  await expect(page.locator(".composer textarea")).toBeVisible();
  const ask = {
    type: "ask_user" as const, sid: "layout-parent", ask_id: "native-fallback", seq: 1,
    question: "是否允许模型回退？", options: ["同意回退", "拒绝回退", "编辑提问", "同意回退并记住"].map(label => ({ label })),
    model_fallback: { original_model: "claude-opus-5-6", fallback_model: "claude-opus-5-5",
      turn_id: "original-turn", original_prompt: "分析这个仓库，并保留原模型重试" },
  };
  relay.emit(ask);
  const dialog = page.getByRole("dialog", { name: "是否允许模型回退？" });
  await expect(dialog).toBeVisible();
  await expect(page.locator(".hstat")).toHaveText("待确认");
  await expect(dialog.getByText("Opus 5.6", { exact: true })).toBeVisible();
  await expect(dialog.getByText("Opus 5.5", { exact: true })).toBeVisible();
  const automatic = dialog.getByRole("checkbox", { name: "以后自动切换" });
  await expect(automatic).not.toBeChecked();
  const box = (await dialog.boundingBox())!;
  const viewport = page.viewportSize()!;
  expect(Math.abs(box.x + box.width / 2 - viewport.width / 2)).toBeLessThan(2);
  expect(Math.abs(box.y + box.height / 2 - viewport.height / 2)).toBeLessThan(2);
  await page.screenshot({ path: testInfo.outputPath("model-fallback-open.png") });
  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await page.screenshot({ path: testInfo.outputPath("model-fallback-dark.png") });
  await automatic.check();
  await dialog.getByRole("button", { name: "收起回退确认", exact: true }).click();
  await expect(dialog).toBeHidden();
  const resume = page.getByRole("button", { name: /等待确认模型切换.*处理/ });
  await expect(resume).toBeVisible();
  await page.setViewportSize({ width: 320, height: 740 });
  expect((await resume.boundingBox())!.height).toBeLessThanOrEqual(48);
  expect(await resume.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
  await page.setViewportSize(viewport);
  expect(relay.commands.filter(command => command.type === "answer_question")).toHaveLength(0);
  await page.screenshot({ path: testInfo.outputPath("model-fallback-collapsed.png") });
  await resume.click();
  await expect(automatic).toBeChecked();
  if (choice !== "同意回退") await automatic.uncheck();
  await dialog.getByRole("button", { name: choice, exact: true }).click();
  await expect.poll(() => relay.commands.filter(command => command.type === "answer_question").length).toBe(1);
  const answer = relay.commands.find(command => command.type === "answer_question")!;
  expect(answer.sid).toBe("layout-parent");
  expect(answer.answer).toBe(choice === "同意回退" ? "同意回退并记住" : choice);
  await expect(dialog).toBeVisible(); // Only the native close retires the pending request.
  relay.emit({ type: "ask_user_closed", sid: "layout-parent", seq: 2,
    ask_id: "native-fallback", reason: "answered" });
  await expect(dialog).toBeHidden();
  if (choice === "编辑提问") {
    await expect(page.locator(".composer textarea")).toHaveValue(ask.model_fallback.original_prompt);
  }
  if (choice === "同意回退") {
    relay.emit({ type: "model", sid: "layout-parent", seq: 3, model: "claude-opus-5-5" });
    relay.emit({ type: "notice", sid: "layout-parent", seq: 4, notice_id: "model-fallback:native-event",
      severity: "warning", category: "runtime", title: "模型已回退",
      message: "claude-opus-5-6 → claude-opus-5-5。原模型拒绝响应，已切换备用模型。",
      detail: "切换时间 2026-11-14T01:00:00Z" });
    await expect(page.getByTitle("选择模型", { exact: true })).toContainText("claude-opus-5-5");
    await expect(page.locator(".header-feedback")).toHaveText("模型已回退");
    await page.locator(".header-feedback").click();
    const details = page.getByRole("dialog", { name: "状态详情" });
    await expect(details).toContainText("claude-opus-5-6 → claude-opus-5-5");
    await expect(details).toContainText("切换时间 2026-11-14T01:00:00Z");
    await page.keyboard.press("Escape");
  }
  expect(relay.commands.some(command => ["query", "steer", "interrupt", "set_model"].includes(String(command.type)))).toBe(false);
});
}

test("notification click refreshes stale device validation and keeps the current session intact", async ({ page }) => {
  const relay = await mockRightPanelRelay(page);
  let unavailable = true;
  let reads = 0;
  await page.route("**/api/devices", async route => {
    reads += 1;
    if (unavailable) return route.fulfill({ status: 503, body: "unavailable" });
    return route.fulfill({ json: { devices: [{ machine_id: "layout-machine", label: "Layout", online: true }] } });
  });
  await page.goto("/");
  await expect.poll(() => relay.commands.some(command => command.type === "switch_session")).toBe(true);
  await expect(page.locator(".composer textarea")).toBeVisible();
  await expect.poll(() => reads).toBeGreaterThan(0);
  const focusBefore = relay.commands.filter(command => command.type === "switch_session").length;
  const readsBefore = reads;
  unavailable = false;
  await page.evaluate(() => navigator.serviceWorker.dispatchEvent(new MessageEvent("message", { data: {
    type: "cc-remote-notification", notification_id: "completed-turn",
    route: { machine_id: "layout-machine", session_id: "layout-parent", engine: "codex", space: "code" },
  } })));
  await expect.poll(() => reads).toBeGreaterThan(readsBefore);
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("cc_remote_notification_target"))).toBeNull();
  expect(relay.commands.filter(command => command.type === "switch_session")).toHaveLength(focusBefore);
  await expect(page.locator(".header-feedback").filter({ hasText: "操作未完成" })).toHaveCount(0);
  const afterFirst = reads;
  await page.evaluate(() => navigator.serviceWorker.dispatchEvent(new MessageEvent("message", { data: {
    type: "cc-remote-notification", notification_id: "completed-turn",
    route: { machine_id: "layout-machine", session_id: "layout-parent", engine: "codex", space: "code" },
  } })));
  await page.waitForTimeout(100);
  expect(reads).toBe(afterFirst);
  expect(relay.commands.some(command => ["query", "steer", "interrupt"].includes(String(command.type)))).toBe(false);
});

test("notification click retries a transient device outage and consumes the retained cold route", async ({ page }) => {
  await mockRightPanelRelay(page);
  let reads = 0;
  await page.route("**/api/devices", async route => {
    reads += 1;
    if (reads <= 2) return route.fulfill({ status: 503, body: "unavailable" });
    return route.fulfill({ json: { devices: [{ machine_id: "layout-machine", label: "Layout", online: true }] } });
  });
  await page.addInitScript(() => {
    if (!sessionStorage.getItem("notification-fixture-captured")) {
      sessionStorage.setItem("notification-fixture-captured", "1");
      sessionStorage.setItem("cc_remote_notification_target", JSON.stringify({
        machine_id: "layout-machine", session_id: "layout-parent", engine: "codex", space: "code",
      }));
    }
  });
  await page.goto("/");
  await expect.poll(() => reads).toBeGreaterThan(2);
  await expect(page.locator(".composer textarea")).toBeVisible();
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("cc_remote_notification_target"))).toBeNull();
  await expect(page.locator(".header-feedback").filter({ hasText: "操作未完成" })).toHaveCount(0);
  await page.reload();
  await expect(page.locator(".composer textarea")).toBeVisible();
  await expect(page.locator(".header-feedback").filter({ hasText: "操作未完成" })).toHaveCount(0);
});

test("notification click can retry the same notification after a failed validation", async ({ page }) => {
  const relay = await mockRightPanelRelay(page);
  let unavailable = true;
  await page.route("**/api/devices", async route => {
    if (unavailable) return route.fulfill({ status: 503, body: "unavailable" });
    return route.fulfill({ json: { devices: [{ machine_id: "layout-machine", label: "Layout", online: true }] } });
  });
  await page.goto("/");
  await expect.poll(() => relay.commands.some(command => command.type === "switch_session")).toBe(true);
  const clickNotification = () => page.evaluate(() => navigator.serviceWorker.dispatchEvent(new MessageEvent("message", {
    data: { type: "cc-remote-notification", notification_id: "retryable-turn",
      route: { machine_id: "layout-machine", session_id: "layout-parent", engine: "codex", space: "code" } },
  })));
  await clickNotification();
  await expect(page.locator(".header-feedback")).toHaveText("操作未完成");
  await page.locator(".header-feedback").click();
  await page.getByRole("button", { name: "知道了", exact: true }).click();
  unavailable = false;
  await clickNotification();
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("cc_remote_notification_target"))).toBeNull();
  await expect(page.locator(".header-feedback").filter({ hasText: "操作未完成" })).toHaveCount(0);
});

test("notification click resumes a cached session when returning from another device", async ({ page }) => {
  let holdReturningDevice = false;
  const relay = await mockRightPanelRelay(page, {
    retained: false,
    devices: [
      { machine_id: "layout-machine", label: "Mac", online: true },
      { machine_id: "second-machine", label: "VPS", online: true },
    ],
    sessionIdForMachine: machine => `${machine}-session`,
    listReply: (_command, machine) => holdReturningDevice && machine === "layout-machine" ? null : undefined,
    historyReply: (command, machine) => ({
      type: "history", session_id: String(command.session_id), sid: String(command.session_id),
      revision: "notification-cache", generation: "layout-generation", detail: "summary", has_more: false,
      events: [], turns: [{ id: "remembered-turn", prompt: "Check device", done: true,
        blocks: [{ kind: "text", message_id: "remembered-answer", channel: "final",
          text: `Cached answer from ${machine}`, done: true }] }],
    }),
  });
  await page.goto("/");
  await expect(page.getByText("Cached answer from layout-machine", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "设备中心", exact: true }).click();
  await page.getByRole("dialog", { name: "设备中心" }).getByRole("button", { name: /VPS/ }).click();
  await expect(page.getByText("Cached answer from second-machine", { exact: true })).toBeVisible();
  holdReturningDevice = true;
  const start = relay.commands.length;
  await page.evaluate(() => navigator.serviceWorker.dispatchEvent(new MessageEvent("message", { data: {
    type: "cc-remote-notification", notification_id: "mac-cached-turn",
    route: { machine_id: "layout-machine", session_id: "layout-machine-session", engine: "codex", space: "code" },
  } })));
  await expect(page.getByText("Cached answer from layout-machine", { exact: true })).toBeVisible();
  await expect.poll(() => relay.commands.slice(start).some(command =>
    command.type === "list_sessions" && command.engine === "codex" && command.space === "code")).toBe(true);
  const request = relay.commands.slice(start).findLast(command =>
    command.type === "list_sessions" && command.engine === "codex" && command.space === "code")!;
  relay.emit({ type: "session_list", engine: "codex", space: "code", request_id: String(request.cmd_id),
    sessions: [{ session_id: "layout-machine-session", engine: "codex", space: "code",
      summary: "Restored Mac session", cwd: "/tmp/layout", state: "idle" }] });
  await expect.poll(() => relay.commands.slice(start).filter(command => command.type === "switch_session"
    && command.session_id === "layout-machine-session").length).toBe(1);
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("cc_remote_notification_target"))).toBeNull();
  expect(relay.commands.some(command => ["query", "steer", "interrupt"].includes(String(command.type)))).toBe(false);
});
