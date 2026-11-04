import assert from "node:assert/strict";
import { test } from "node:test";
import { Resources, VIRTUAL_ORIGIN } from "../src/viewer-runner/resources.ts";

const MiB = 1024 * 1024;
const url = `${VIRTUAL_ORIGIN}/review/assembly.html`;

function resources(response: () => Response) {
  const port = { postMessage() {}, close() {} } as unknown as MessagePort;
  const value = new Resources(port, "fixture", url);
  value.fetch = async () => response();
  return value;
}

function textResponse(text: string) {
  return new Response(text, { headers: { "content-length": String(Buffer.byteLength(text)), etag: '"v1"' } });
}

function unreadResponse(size: number, status = 200) {
  let cancelled = false;
  const response = new Response(new ReadableStream({ cancel() { cancelled = true; } }), {
    status, headers: { "content-length": String(size), etag: '"v1"' },
  });
  return { response, cancelled: () => cancelled };
}

test("HTML with inert embedded model data can exceed the code-file limit", async () => {
  const html = `<script type="application/octet-stream">${"A".repeat(3 * MiB)}</script>`;
  const value = resources(() => textResponse(html));
  assert.equal(await value.text(url, "document"), html);
  value.checkCode("document.getElementById('mesh-data').textContent", url);
});

test("HTML accepts the exact 16 MiB boundary", async () => {
  const value = resources(() => textResponse("x".repeat(16 * MiB)));
  assert.equal((await value.text(url, "document")).length, 16 * MiB);
});

test("oversized HTML is cancelled before decoding with its path, size and limit", async () => {
  const pending = unreadResponse(16 * MiB + 1);
  const value = resources(() => pending.response);
  await assert.rejects(value.text(url, "document"), (error: Error) => {
    assert.match(error.message, /预览 HTML过大/);
    assert.match(error.message, /\/review\/assembly\.html/);
    assert.match(error.message, /16777217 字节/);
    assert.match(error.message, /上限 16 MiB/);
    return true;
  });
  assert.equal(pending.cancelled(), true);
});

test("external JS and CSS retain the 2 MiB boundary", async () => {
  assert.equal((await resources(() => textResponse("x".repeat(2 * MiB))).text(url)).length, 2 * MiB);
  const pending = unreadResponse(2 * MiB + 1);
  await assert.rejects(resources(() => pending.response).text(url), /预览代码资源过大.*上限 2 MiB/);
  assert.equal(pending.cancelled(), true);
});

test("file-size limits count UTF-8 bytes, not character count", async () => {
  const pending = unreadResponse(Buffer.byteLength("图".repeat(MiB)));
  await assert.rejects(resources(() => pending.response).text(url), /3\.00 MiB.*上限 2 MiB/);
  assert.equal(pending.cancelled(), true);
});

test("HTTP failures remain distinct from oversized resources", async () => {
  for (const status of [403, 404, 500]) {
    const pending = unreadResponse(0, status);
    await assert.rejects(resources(() => pending.response).text(url, "document"),
      new RegExp(`读取失败（HTTP ${status}）：/review/assembly\\.html`));
    assert.equal(pending.cancelled(), true);
  }
});

test("invalid size metadata is rejected without consuming its body", async () => {
  for (const size of [null, "-1", "NaN", "1.5"]) {
    const pending = unreadResponse(0);
    if (size === null) pending.response.headers.delete("content-length");
    else pending.response.headers.set("content-length", size);
    await assert.rejects(resources(() => pending.response).text(url, "document"), /资源大小无效/);
    assert.equal(pending.cancelled(), true);
  }
});

test("concurrent text decodes reserve the shared budget and release it on failure", async () => {
  const first = textResponse("");
  let fail!: (error: Error) => void;
  first.headers.set("content-length", String(16 * MiB));
  first.text = () => new Promise<string>((_, reject) => { fail = reject; });
  const second = textResponse("");
  let finish!: (text: string) => void;
  second.headers.set("content-length", String(8 * MiB));
  second.text = () => new Promise<string>((resolve) => { finish = resolve; });
  const blocked = unreadResponse(1);
  const queue = [first, second, blocked.response, textResponse("small")];
  const value = resources(() => queue.shift()!);
  const readingFirst = value.text(url, "document");
  const firstFailed = assert.rejects(readingFirst, /fixture disconnect/);
  const readingSecond = value.text(url, "document");
  await assert.rejects(value.text(url), /48 MiB 总预算/);
  assert.equal(blocked.cancelled(), true);
  fail(new Error("fixture disconnect"));
  finish("");
  await Promise.all([firstFailed, readingSecond]);
  assert.equal(await value.text(url), "small");
});

test("completed text loads still count towards the shared budget", async () => {
  const value = resources(() => textResponse("x".repeat(8 * MiB)));
  for (let i = 0; i < 3; i++) await value.text(url, "document");
  await assert.rejects(value.text(url, "document"), /48 MiB 总预算/);
});

test("larger HTML does not bypass inline script or style limits", () => {
  const value = resources(() => textResponse(""));
  value.checkCode("x".repeat(2 * MiB), url);
  assert.throws(() => value.checkCode("x".repeat(2 * MiB + 1), url), /脚本\/样式过大.*上限 2 MiB/);
  assert.throws(() => value.checkCode("图".repeat(MiB), url), /3\.00 MiB.*上限 2 MiB/);
});

test("parsed code has its own aggregate budget excluding inert model data", () => {
  const value = resources(() => textResponse(""));
  for (let i = 0; i < 4; i++) value.checkCode("x".repeat(2 * MiB), url);
  assert.throws(() => value.checkCode("x", url), /脚本\/样式超过 16 MiB 文本预算/);
});


test("retired media releases the shared blob budget without revoking page assets", async () => {
  const value = resources(() => new Response(new Uint8Array(10 * MiB), {
    headers: { "content-length": String(10 * MiB), etag: '"v1"' },
  }));
  const image = value.blob("page image", "text/plain");
  for (let i = 0; i < 9; i++) {
    const media = await value.mediaAsset(`${VIRTUAL_ORIGIN}/sample.wav`, new AbortController().signal);
    assert.equal((await fetch(media.url)).status, 200);
    media.release(); media.release();
    await assert.rejects(fetch(media.url));
  }
  assert.equal(await (await fetch(image)).text(), "page image");
  value.dispose();
  await assert.rejects(fetch(image));
});

test("pending media reserves shared blob memory and cancellation releases the reservation", async () => {
  const response = textResponse("");
  response.headers.set("content-length", String(60 * MiB));
  let complete!: (blob: Blob) => void;
  response.blob = () => new Promise(resolve => { complete = resolve; });
  const value = resources(() => response);
  const controller = new AbortController();
  const pending = value.mediaAsset(url, controller.signal);
  const rejected = assert.rejects(pending, { name: "AbortError" });
  await Promise.resolve();
  assert.throws(() => value.blob(new Uint8Array(5 * MiB), "audio/wav"), /缓存预算/);
  controller.abort(); complete(new Blob([]));
  await rejected;
  const media = value.blob(new Uint8Array(5 * MiB), "audio/wav");
  assert.ok(media.startsWith("blob:"));
  value.dispose();
});

test("a completed read cannot create a media URL after the frame closes", async () => {
  const response = textResponse("");
  let complete!: (blob: Blob) => void;
  response.blob = () => new Promise(resolve => { complete = resolve; });
  const value = resources(() => response);
  const pending = value.mediaAsset(url, new AbortController().signal);
  const rejected = assert.rejects(pending, /连接已断开/);
  await Promise.resolve();
  value.dispose(); complete(new Blob([]));
  await rejected;
});
