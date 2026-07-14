import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import { classifyPreviewTarget, isMarkdownPath } from "../src/preview-path.ts";
import { parseLocalFileTarget } from "../src/file-link.ts";
import type { ServerEvent } from "../src/protocol.ts";

assert.deepEqual(classifyPreviewTarget("docs/README.md", "./img/a.png"), {
  kind: "local", value: "docs/img/a.png",
});
assert.deepEqual(classifyPreviewTarget("docs/README.md", "../root.png?raw=1"), {
  kind: "local", value: "root.png",
});
assert.equal(classifyPreviewTarget("README.md", "../secret.png").kind, "blocked");
assert.equal(classifyPreviewTarget("README.md", "/etc/passwd").kind, "blocked");
assert.equal(classifyPreviewTarget("README.md", "file:///etc/passwd").kind, "blocked");
assert.equal(classifyPreviewTarget("README.md", "//example.com/a.png").kind, "blocked");
assert.deepEqual(classifyPreviewTarget("README.md", "https://example.com/a.png"), {
  kind: "external", value: "https://example.com/a.png",
});
assert.deepEqual(classifyPreviewTarget("README.md", "#section"), {
  kind: "anchor", value: "#section",
});
assert.equal(isMarkdownPath("docs/guide.MD#intro"), true);
assert.equal(isMarkdownPath("docs/image.png"), false);
assert.deepEqual(parseLocalFileTarget(
  "/home/nancy/project/codex_stream.py:731"), {
  path: "/home/nancy/project/codex_stream.py", line: 731, column: undefined,
});
assert.deepEqual(parseLocalFileTarget("src/app.ts#L42C7"), {
  path: "src/app.ts", line: 42, column: 7,
});
assert.deepEqual(parseLocalFileTarget("file:///tmp/a%20b.py:9"), {
  path: "/tmp/a b.py", line: 9, column: undefined,
});
assert.equal(parseLocalFileTarget("https://example.com/a.py:9"), null);
assert.equal(parseLocalFileTarget("#L9"), null);

const harness = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});
try {
  const { initialState, reduce } = await harness.ssrLoadModule("/src/reducer.ts");
  const { ArtifactPanel } = await harness.ssrLoadModule(
    "/src/components/ArtifactPanel.tsx");
  const { MessageBlock } = await harness.ssrLoadModule(
    "/src/components/MessageBlock.tsx");
  let state = reduce(initialState, {
    type: "open_file_loading",
    file: "README.md",
    sid: "session-1",
    requestId: "preview-new",
    kind: "md",
  });
  const loading = state;

  state = reduce(state, { type: "event", event: {
    v: 10,
    type: "file_preview",
    ts: 1,
    sid: "session-1",
    path: "README.md",
    request_id: "preview-old",
    format: "markdown",
    content: "stale",
    size: 5,
    truncated: false,
    mtime_ns: "1",
  } as ServerEvent });
  assert.equal(state, loading,
    "a stale preview response must not replace the open request");

  state = reduce(state, { type: "event", event: {
    v: 10,
    type: "file_preview",
    ts: 2,
    sid: "session-1",
    path: "docs/README.md",
    request_id: "preview-new",
    format: "markdown",
    content: "# current",
    size: 9,
    truncated: false,
    mtime_ns: "2",
    revision: "a".repeat(64),
  } as ServerEvent });
  assert.equal(state.artifact?.file, "docs/README.md");
  assert.equal(state.artifact?.content, "# current");
  assert.equal(state.artifact?.revision, "a".repeat(64));
  assert.equal(state.artifact?.loading, undefined);

  const rendered = state;
  state = reduce(state, { type: "event", event: {
    v: 10,
    type: "preview_asset",
    ts: 3,
    sid: "other-session",
    path: "docs/image.png",
    preview_id: "preview-new",
    request_id: "asset-wrong-session",
    media_type: "image/png",
    data: "cG5n",
  } as ServerEvent });
  assert.equal(state, rendered, "assets from another session must be ignored");

  state = reduce(state, { type: "event", event: {
    v: 10,
    type: "preview_asset",
    ts: 4,
    sid: "session-1",
    path: "docs/image.png",
    preview_id: "preview-new",
    request_id: "asset-1",
    media_type: "image/png",
    data: "cG5n",
  } as ServerEvent });
  assert.deepEqual(state.artifact?.assets?.["docs/image.png"], {
    mediaType: "image/png", data: "cG5n", error: undefined,
  });

  state = reduce(state, {
    type: "start_file_save",
    requestId: "save-1",
    content: "# edited",
  });
  const saving = state;
  state = reduce(state, { type: "event", event: {
    v: 10,
    type: "file_save_result",
    ts: 5,
    sid: "session-1",
    path: "docs/README.md",
    request_id: "stale-save",
    status: "saved",
    size: 8,
    mtime_ns: "3",
    revision: "b".repeat(64),
  } as ServerEvent });
  assert.equal(state, saving, "a stale save response must be ignored");

  state = reduce(state, { type: "event", event: {
    v: 10,
    type: "file_save_result",
    ts: 6,
    sid: "session-1",
    path: "docs/README.md",
    request_id: "save-1",
    status: "conflict",
    size: 12,
    mtime_ns: "4",
    revision: "c".repeat(64),
    error: "文件已修改",
  } as ServerEvent });
  assert.equal(state.artifact?.content, "# current");
  assert.equal(state.artifact?.saveStatus, "conflict");
  assert.equal(state.artifact?.saveError, "文件已修改");

  state = reduce(state, {
    type: "start_file_save",
    requestId: "save-2",
    content: "# edited",
  });
  state = reduce(state, { type: "event", event: {
    v: 10,
    type: "file_save_result",
    ts: 7,
    sid: "session-1",
    path: "docs/README.md",
    request_id: "save-2",
    status: "saved",
    size: 8,
    mtime_ns: "5",
    revision: "d".repeat(64),
  } as ServerEvent });
  assert.equal(state.artifact?.content, "# edited");
  assert.equal(state.artifact?.saveStatus, "saved");
  assert.equal(state.artifact?.revision, "d".repeat(64));

  state = reduce(state, {
    type: "open_file_loading",
    file: "/home/nancy/project/codex_stream.py",
    sid: "session-1",
    requestId: "source-1",
    kind: "file",
    line: 731,
  });
  state = reduce(state, { type: "event", event: {
    v: 10,
    type: "file_preview",
    ts: 5,
    sid: "session-1",
    path: "cc_remote/wrapper/codex_stream.py",
    request_id: "source-1",
    format: "text",
    content: "source",
    size: 6,
    truncated: false,
    mtime_ns: "3",
  } as ServerEvent });
  assert.equal(state.artifact?.kind, "file");
  assert.equal(state.artifact?.line, 731);
  assert.equal(state.artifact?.file, "cc_remote/wrapper/codex_stream.py");

  const markup = renderToStaticMarkup(createElement(ArtifactPanel, {
    artifact: {
      file: "docs/README.md",
      sid: "session-1",
      requestId: "preview-new",
      kind: "md",
      content: "# Preview\n\n<script>alert(1)</script>",
      size: 42,
      mtimeNs: "2",
      revision: "a".repeat(64),
      assets: {},
    },
    active: "diff",
    hasBtw: false,
    onTab: () => {},
    onClose: () => {},
  }));
  assert.match(markup, /markdown-preview/);
  assert.match(markup, /panel-resizer/);
  assert.match(markup, /data-lock-horizontal-swipe="true"/);
  assert.match(markup, />预览</);
  assert.match(markup, />源码</);
  assert.match(markup, />保存</);
  assert.match(markup, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(markup, /<script>/);

  const messageMarkup = renderToStaticMarkup(createElement(MessageBlock, {
    text: "[codex_stream.py](/home/nancy/project/codex_stream.py:731)",
    done: true,
    onOpenFile: () => {},
  }));
  assert.match(messageMarkup, /message-file-link/);
  assert.match(messageMarkup, /在 Remote 中打开/);
  assert.doesNotMatch(messageMarkup, /href="\/home\/nancy/);

  const source = Array.from({ length: 740 }, (_, index) => `line ${index + 1}`).join("\n");
  const sourceMarkup = renderToStaticMarkup(createElement(ArtifactPanel, {
    artifact: {
      file: "cc_remote/wrapper/codex_stream.py",
      sid: "session-1",
      requestId: "source-1",
      kind: "file",
      content: source,
      line: 731,
      assets: {},
    },
    active: "diff",
    hasBtw: false,
    onTab: () => {},
    onClose: () => {},
  }));
  assert.match(sourceMarkup, /source-line focused/);
  assert.match(sourceMarkup, />731<\/span><code>line 731<\/code>/);
  assert.match(sourceMarkup, /501–740 \/ 740 行/);
} finally {
  await harness.close();
}

console.log("markdown preview tests passed");
