import { test, expect } from "@playwright/test";

const ORIGIN = process.env.VIEWER_TEST_ORIGIN ?? "http://127.0.0.1:4174";

test("video artifact plays, seeks, expands and releases its source on close", async ({ page }, testInfo) => {
  await page.request.post(`${ORIGIN}/api/login`, { data: { password: "local-viewer-fixture-password" } });
  const ranges: number[] = [];
  page.on("response", response => {
    if (response.url().includes("/api/media/")) ranges.push(response.status());
  });
  await page.goto(`${ORIGIN}/tests/video-preview.html`);
  await page.getByRole("button", { name: "sample.mp4", exact: true }).click();
  const video = page.locator("video");
  await expect(video).toBeVisible();
  await expect.poll(() => video.evaluate((el: HTMLVideoElement) => el.readyState), { timeout: 20_000 }).toBeGreaterThanOrEqual(1);
  await expect.poll(() => video.evaluate((el: HTMLVideoElement) => el.duration)).toBeGreaterThan(10);
  await video.evaluate(async (el: HTMLVideoElement) => { el.muted = true; await el.play(); });
  await expect.poll(() => video.evaluate((el: HTMLVideoElement) => el.currentTime)).toBeGreaterThan(0.2);
  await video.evaluate((el: HTMLVideoElement) => { el.pause(); el.currentTime = el.duration * 0.8; });
  await expect.poll(() => video.evaluate((el: HTMLVideoElement) => ({
    seeking: el.seeking, ready: el.readyState, error: el.error?.message ?? null,
    buffered: Array.from({ length: el.buffered.length }, (_, i) => [el.buffered.start(i), el.buffered.end(i)]),
  })), { timeout: 15_000 }).toMatchObject({ seeking: false, error: null });
  await expect.poll(() => video.evaluate((el: HTMLVideoElement) => el.currentTime / el.duration)).toBeCloseTo(0.8, 1);
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(ranges).toContain(206);
  await page.getByRole("button", { name: "居中展开文件预览", exact: true }).click();
  await expect(video).toBeVisible();
  await expect(page.getByRole("dialog", { name: "文件预览", exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("video-preview.png") });
  await video.evaluate((el: HTMLVideoElement) => {
    (window as unknown as { closedVideo: HTMLVideoElement }).closedVideo = el;
  });
  await page.getByRole("button", { name: "收起", exact: true }).click();
  await expect(video).toHaveCount(0);
  expect(await page.evaluate(() => {
    const el = (window as unknown as { closedVideo: HTMLVideoElement }).closedVideo;
    return { paused: el.paused, src: el.getAttribute("src") };
  })).toEqual({ paused: true, src: null });
});
