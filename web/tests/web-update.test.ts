import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { applyWebUpdate, availableWebBuild, parseWebBuild, type WebBuild } from "../src/web-build.ts";
import { ComposerDraftStore } from "../src/composer-drafts.ts";

const loaded: WebBuild = { build_id: "a".repeat(64), version: "5.3.7", protocol: 85 };
const next = { ...loaded, build_id: "b".repeat(64) };
const requests: Array<{ url: string; options?: RequestInit }> = [];
function fetcher(web: unknown, relay: unknown, status = 200): typeof fetch {
  return (async (url, options) => {
    requests.push({ url: String(url), options });
    return new Response(JSON.stringify(String(url).startsWith("/healthz") ? relay : web), { status });
  }) as typeof fetch;
}
assert.deepEqual(await availableWebBuild(fetcher(next, loaded), loaded), next,
  "same-version, same-protocol UI changes are still offered");
assert.equal(await availableWebBuild(fetcher(loaded, loaded), loaded), null);
assert.equal(await availableWebBuild(fetcher(next, { ...loaded, protocol: 84 }), loaded), null,
  "do not reload into a partially activated protocol bundle");
assert.equal(await availableWebBuild(fetcher(next, { ...loaded, version: "5.3.6" }), loaded), null);
const rollback = { ...next, version: "5.3.6", protocol: 84 };
assert.deepEqual(await availableWebBuild(fetcher(rollback, rollback), loaded), rollback,
  "rollback is an update to the loaded page too");
assert.equal(await availableWebBuild(fetcher(next, loaded, 503), loaded), null);
assert.equal(await availableWebBuild(fetcher(next, null), loaded), null);
assert.equal(await availableWebBuild((async () => { throw new Error("offline"); }) as typeof fetch, loaded), null);
assert.equal(parseWebBuild({ ...next, build_id: "" }), null);
assert.equal(parseWebBuild({ version: "5.3.7", protocol: 85 }), null);
assert.equal(parseWebBuild({ ...next, protocol: "85" }), null);
for (const request of requests) {
  assert.match(request.url, /\?build_probe=\d+$/);
  assert.equal(request.options?.cache, "no-store");
  assert.equal(request.options?.credentials, "same-origin");
}

const drafts = new ComposerDraftStore();
assert.equal(drafts.hasDrafts, false);
drafts.set("another-session", { input: "unsent", images: [], files: [], pastes: [] });
assert.equal(drafts.hasDrafts, true, "offscreen drafts also prevent update data loss");
drafts.delete("another-session");
const finish = drafts.beginImport();
assert.equal(drafts.hasDrafts, true, "an import must be protected before it produces an attachment");
finish();
finish();
assert.equal(drafts.hasDrafts, false);
drafts.set("image-only", { input: "", files: [], pastes: [],
  images: [{ media_type: "image/png", data: "aW1hZ2U=" }] });
assert.equal(drafts.hasDrafts, true);
let reloads = 0;
let probes = 0;
const blocked = () => drafts.hasDrafts ? "keep draft" : null;
const reload = () => { reloads += 1; };
assert.equal(await applyWebUpdate(blocked, reload, async () => { probes += 1; return next; }), "keep draft");
assert.equal(probes, 0);
assert.equal(reloads, 0);
drafts.clear();
assert.equal(await applyWebUpdate(blocked, reload, async () => {
  drafts.set("typed-during-probe", { input: "new text", images: [], files: [], pastes: [] });
  return next;
}), "keep draft");
assert.equal(reloads, 0, "input arriving during the version check prevents navigation");
drafts.clear();
assert.equal(await applyWebUpdate(blocked, reload, async () => null), "新版暂时不可用，请稍后重试。");
assert.equal(reloads, 0);
assert.equal(await applyWebUpdate(blocked, reload, async () => next), null);
assert.equal(reloads, 1, "reload occurs only after an explicit request with no protected input");

// Exercise the shipped worker: a cached shell must not hide new online assets,
// while the same shell still opens offline. Build/API probes never enter it.
const handlers = new Map<string, (event: unknown) => void>();
let online = true;
let downloads = 0;
runInNewContext(readFileSync("public/sw.js", "utf8"), {
  URL, Response,
  self: {
    location: { origin: "https://remote.test" },
    addEventListener: (name: string, handler: (event: unknown) => void) => handlers.set(name, handler),
  },
  fetch: async () => {
    downloads += 1;
    if (!online) throw new Error("offline");
    return new Response("new content");
  },
  caches: {
    match: async () => new Response("old cached content"),
    open: async () => ({ put: async () => {} }),
  },
});
async function workerFetch(path: string, mode = "cors") {
  let response: Promise<Response> | undefined;
  handlers.get("fetch")!({
    request: { method: "GET", url: `https://remote.test${path}`, mode },
    respondWith: (value: Promise<Response>) => { response = value; },
  });
  return response ? (await response).text() : null;
}
assert.equal(await workerFetch("/", "navigate"), "new content");
assert.equal(await workerFetch("/icon-192.png"), "new content");
assert.equal(await workerFetch("/manifest.webmanifest"), "new content");
const beforeCachedAsset = downloads;
assert.equal(await workerFetch("/assets/app-contenthash.js"), "old cached content");
assert.equal(downloads, beforeCachedAsset, "immutable bundles reuse their exact cached bytes");
assert.equal(await workerFetch("/cc-remote-build.json?build_probe=1"), null);
assert.equal(await workerFetch("/api/session"), null);
online = false;
assert.equal(await workerFetch("/", "navigate"), "old cached content");
assert.equal(await workerFetch("/manifest.webmanifest"), "old cached content");
console.log("web update checks passed");
