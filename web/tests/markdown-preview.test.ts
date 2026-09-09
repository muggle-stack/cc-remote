import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import { classifyPreviewTarget, isMarkdownPath } from "../src/preview-path.ts";
import { parseLocalFileTarget } from "../src/file-link.ts";
import {
  parseCodexFileCitationDirective,
  remarkCodexFileCitations,
} from "../src/codex-file-citation.ts";
import {
  InlineImageAssetCache,
  classifyMessageImageTarget,
} from "../src/inline-image-assets.ts";
import { imageDimensionsFromBase64, queryImageDimensions } from "../src/img.ts";
import { collectTurnFileChanges, filePathsFromInput, mutatedFilePaths } from "../src/file-changes.ts";
import {
  MAX_MERMAID_SOURCE_CHARS,
  MAX_MERMAID_SOURCE_LINES,
  isMermaidFenceClass,
  mermaidSourceProblem,
} from "../src/mermaid.ts";
import type { ServerEvent } from "../src/protocol.ts";
import type { Turn } from "../src/domain/conversation.ts";
import { presentAsyncQuestionReplies, supplementalAnswerPrompt } from "../src/async-question-presentation.ts";
import { MARKDOWN_HTML_README } from "./fixtures/markdown-html.ts";
import { previewImageDimension } from "../src/markdown-preview-html.ts";

for (const [input, expected] of [[820, 820], ["64", 64], ["100%", "100%"],
  ["40%", "40%"], [4096, 4096]] as const) {
  assert.equal(previewImageDimension(input), expected);
}
for (const input of [undefined, null, "", 0, -1, "-10px", "99px", "100.1%",
  "101%", "0%", 4097, "99999999", "calc(100vh)", "1;position:fixed"]) {
  assert.equal(previewImageDimension(input), undefined);
}

const nativeQuestion: Turn = { id: "question-turn", prompt: "检查日志", done: true,
  blocks: [{ kind: "text", message_id: "question-message", text: "native question", done: true,
    delivery: "async", questions: [{ title: "在哪个设备？" }, { title: "用的什么手势？" }] }] };
const nativeReply: Turn = { id: "reply-turn", done: false, blocks: [],
  prompt: supplementalAnswerPrompt([{ question: "用的什么手势？", answer: "三指拖拽\n第二行也要保留" }]) };
const presentation = presentAsyncQuestionReplies([nativeQuestion, nativeReply]);
assert.deepEqual(presentation.replies.get(nativeReply.id), [{ question: "用的什么手势？", answer: "三指拖拽\n第二行也要保留" }]);
assert.equal(presentation.answered.has("question-message"), true);
assert.equal(presentAsyncQuestionReplies([nativeReply]).replies.size, 0, "never guess from a prefix without the native question");
assert.equal(presentAsyncQuestionReplies([nativeReply, nativeQuestion]).replies.size, 0, "a future question cannot own this reply");
const repeatedQuestion = { ...nativeQuestion, id: "after-compaction" };
for (const turns of [
  [nativeQuestion, repeatedQuestion, nativeReply],
  [nativeQuestion, nativeReply, repeatedQuestion],
]) {
  const restored = presentAsyncQuestionReplies(turns);
  assert.equal(restored.replies.size, 1, "native question replay is not an ambiguous second question");
  assert.equal(restored.questionOwners.get("question-message"), nativeQuestion.id);
  assert.equal(restored.answered.has("question-message"), true,
    "all row aliases agree that the same native question was answered");
}
const independentQuestion: Turn = { ...nativeQuestion, id: "independent-question",
  blocks: nativeQuestion.blocks.map(block => ({ ...block, message_id: "other-question-message" })) };
assert.equal(presentAsyncQuestionReplies([nativeQuestion, independentQuestion, nativeReply]).replies.size, 0,
  "distinct native questions with equal titles remain ambiguous");
assert.equal(presentAsyncQuestionReplies([nativeQuestion, { ...nativeReply, error: "rejected" }]).answered.size, 0);
assert.equal(presentAsyncQuestionReplies([nativeQuestion, { ...nativeReply, prompt: "补充回答：\n\n问题：其他问题\n回答：保留原文" }]).replies.size, 0);
const multiReply = { ...nativeReply, prompt: supplementalAnswerPrompt([
  { question: "在哪个设备？", answer: "Mac" }, { question: "用的什么手势？", answer: "三指拖拽" },
]) };
assert.equal(presentAsyncQuestionReplies([nativeQuestion, multiReply]).replies.get(nativeReply.id)?.length, 2);
assert.equal(nativeReply.prompt, "补充回答：\n\n问题：用的什么手势？\n回答：三指拖拽\n第二行也要保留", "presentation never rewrites the wire payload");

assert.deepEqual(classifyPreviewTarget("docs/README.md", "./img/a.png"), {
  kind: "local", value: "docs/img/a.png",
});
assert.deepEqual(classifyPreviewTarget("docs/README.md", "../root.png?raw=1"), {
  kind: "local", value: "root.png",
});
assert.deepEqual(classifyPreviewTarget("README.md", "../secret.png"), {
  kind: "local", value: "../secret.png",
});
assert.deepEqual(classifyPreviewTarget("README.md", "/tmp/report.png"), {
  kind: "local", value: "/tmp/report.png",
});
assert.deepEqual(classifyPreviewTarget(
  "/tmp/report.md", "./images/chart.png"), {
  kind: "local", value: "/tmp/images/chart.png",
});
assert.equal(classifyPreviewTarget("README.md", "file:///etc/passwd").kind, "blocked");
assert.equal(classifyPreviewTarget(
  "README.md", `${"图".repeat(1400)}.png`).kind, "blocked");
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
const reorderedCitation = ':codex-file-citation{purpose="output" '
  + 'label="报告 } 终版" path="/tmp/demo} final.gif" '
  + 'artifact_kind="animation"}';
assert.deepEqual(parseCodexFileCitationDirective(reorderedCitation), {
  citation: {
    path: "/tmp/demo} final.gif",
    purpose: "output",
    artifactKind: "animation",
    label: "报告 } 终版",
  },
  end: reorderedCitation.length,
}, "citation attributes are order-independent and quoted braces stay in values");
for (const malformed of [
  ':codex-file-citation{path="/tmp/report.pdf" garbage that should remain}',
  ':codex-file-citation{path="/tmp/report.pdf" purpose="unsafe"}',
  ':codex-file-citation{path="/tmp/a.pdf" path="/tmp/b.pdf"}',
  ':codex-file-citation{path="relative.pdf" purpose="output"}',
  ':codex-file-citation{path="https://example.com/report.pdf"}',
  ':codex-file-citation{path="//server/share/report.pdf"}',
  String.raw`:codex-file-citation{path="/tmp/line\nfeed.pdf"}`,
  `:codex-file-citation{path=${JSON.stringify(`/tmp/${"界".repeat(1400)}.pdf`)}}`,
  `:codex-file-citation{path=${JSON.stringify(`/tmp/\uD800.pdf`)}}`,
]) {
  assert.equal(parseCodexFileCitationDirective(malformed), null,
    "malformed or unsafe citation syntax must remain literal");
}

interface CitationAstNode {
  type: string;
  value?: string;
  url?: string;
  title?: string | null;
  children?: CitationAstNode[];
}

const citationTree: CitationAstNode = {
  type: "root",
  children: [{
    type: "paragraph",
    children: [{
      type: "text",
      value: `前 ${reorderedCitation} 后 `
        + ':codex-file-citation{path="/tmp/report.pdf" invalid tail}',
    }, {
      type: "code",
      value: ':codex-file-citation{path="/tmp/example.pdf"}',
    }, {
      type: "link",
      children: [{
        type: "text",
        value: ':codex-file-citation{path="/tmp/already-linked.pdf"}',
      }],
    }],
  }],
};
remarkCodexFileCitations()(citationTree);
const citationChildren = citationTree.children![0].children!;
assert.deepEqual(citationChildren.slice(0, 2).map((node) => node.type), [
  "text", "link",
]);
assert.equal(citationChildren[1].url,
  encodeURIComponent("/tmp/demo} final.gif"));
assert.equal(citationChildren[1].title,
  "cc-remote-file-citation:output:animation");
assert.match(citationChildren[2].value ?? "", /invalid tail/,
  "an invalid directive and its surrounding prose must not be swallowed");
assert.match(citationChildren[3].value ?? "", /codex-file-citation/,
  "code nodes stay literal");
assert.match(citationChildren[4].children?.[0].value ?? "", /codex-file-citation/,
  "existing links are not rewritten");

const repeatedIncompleteCitation = (
  ':codex-file-citation{path="/tmp/incomplete.pdf" '
).repeat(20_000);
const adversarialCitationTree: CitationAstNode = {
  type: "root",
  children: [{ type: "text", value: repeatedIncompleteCitation }],
};
const citationScanStarted = performance.now();
remarkCodexFileCitations()(adversarialCitationTree);
const citationScanMs = performance.now() - citationScanStarted;
assert.ok(citationScanMs < 1_500,
  `incomplete citation scanning must stay linear (took ${citationScanMs.toFixed(1)}ms)`);
assert.equal(adversarialCitationTree.children![0].value,
  repeatedIncompleteCitation, "incomplete streaming syntax remains visible");
assert.equal(isMermaidFenceClass("language-mermaid"), true);
assert.equal(isMermaidFenceClass("foo language-mermaid bar"), true);
assert.equal(isMermaidFenceClass("language-mermaid-extra"), false);
assert.equal(isMermaidFenceClass("mermaid"), false);
assert.equal(isMermaidFenceClass(undefined), false);
assert.equal(mermaidSourceProblem("flowchart LR\nA --> B"), null);
assert.match(
  mermaidSourceProblem("x".repeat(MAX_MERMAID_SOURCE_CHARS + 1)) || "",
  /过长/,
);
assert.match(
  mermaidSourceProblem(
    Array.from({ length: MAX_MERMAID_SOURCE_LINES + 1 }, () => "A").join("\n"),
  ) || "",
  /行数/,
);
assert.deepEqual(classifyMessageImageTarget(
  "/Volumes/MuggleSSD/workspace/project/tmp-auth.png"), {
  kind: "local", value: "/Volumes/MuggleSSD/workspace/project/tmp-auth.png",
});
assert.deepEqual(classifyMessageImageTarget("screenshots/result.webp?raw=1"), {
  kind: "local", value: "screenshots/result.webp",
});
assert.deepEqual(classifyMessageImageTarget("https://example.com/result.png"), {
  kind: "external", value: "https://example.com/result.png",
});
assert.equal(classifyMessageImageTarget("data:image/png;base64,cG5n").kind, "blocked");
assert.equal(classifyMessageImageTarget("/etc/password.txt").kind, "blocked");

const inlineAssets = new InlineImageAssetCache(2);
assert.equal(inlineAssets.begin({
  sid: "session-1", path: "qr.png", previewId: "preview-1", requestId: "request-1",
}), true);
assert.equal(inlineAssets.begin({
  sid: "session-1", path: "qr.png", previewId: "preview-2", requestId: "request-2",
}), false, "one visible local image must have at most one in-flight request");
assert.equal(inlineAssets.accept({
  v: 20, type: "preview_asset", ts: 1, sid: "other-session",
  path: "qr.png", preview_id: "preview-1", request_id: "request-1",
  media_type: "image/png", data: "cG5n",
}), false, "a response from another session must not satisfy the request");
assert.equal(inlineAssets.accept({
  v: 20, type: "preview_asset", ts: 2, sid: "session-1",
  path: "qr.png", preview_id: "preview-1", request_id: "request-1",
  media_type: "image/png", data: "cG5n",
}), true);
assert.deepEqual(inlineAssets.forSession("session-1")["qr.png"], {
  status: "ready", mediaType: "image/png", data: "cG5n",
});
assert.equal(inlineAssets.forSession("other-session")["qr.png"], undefined,
  "a background response must never populate the focused session's asset view");
assert.equal(inlineAssets.dropSession("session-1"), true);
assert.equal(inlineAssets.forSession("session-1")["qr.png"], undefined,
  "a destructive history invalidation must evict the session's rendered assets");

const pngHeader = new Uint8Array(24);
pngHeader.set([0x89, ...new TextEncoder().encode("PNG\r\n\x1a\n")], 0);
pngHeader.set(new TextEncoder().encode("IHDR"), 12);
new DataView(pngHeader.buffer).setUint32(16, 640);
new DataView(pngHeader.buffer).setUint32(20, 480);
const pngHeaderBase64 = Buffer.from(pngHeader).toString("base64");
assert.deepEqual(imageDimensionsFromBase64(pngHeaderBase64, "image/png"), [640, 480],
  "base64 chat images expose dimensions without decoding a DOM image");
assert.deepEqual(queryImageDimensions({
  media_type: "image/png", data: pngHeaderBase64,
}), [640, 480], "wire-compatible QueryImg objects can provide local layout metadata");
new DataView(pngHeader.buffer).setUint32(16, 8192);
new DataView(pngHeader.buffer).setUint32(20, 8192);
assert.equal(imageDimensionsFromBase64(
  Buffer.from(pngHeader).toString("base64"), "image/png",
), null, "untrusted image headers cannot reserve an unbounded layout box");
new DataView(pngHeader.buffer).setUint32(16, 640);
new DataView(pngHeader.buffer).setUint32(20, 480);

const sizedInlineAssets = new InlineImageAssetCache(2);
assert.equal(sizedInlineAssets.begin({
  sid: "session-1", path: "sized-qr.png",
  previewId: "preview-sized", requestId: "request-sized",
}), true);
assert.equal(sizedInlineAssets.accept({
  v: 20, type: "preview_asset", ts: 3, sid: "session-1",
  path: "sized-qr.png", preview_id: "preview-sized",
  request_id: "request-sized", media_type: "image/png", data: pngHeaderBase64,
}), true);
assert.deepEqual(sizedInlineAssets.forSession("session-1")["sized-qr.png"], {
  status: "ready", mediaType: "image/png", data: pngHeaderBase64,
  width: 640, height: 480,
}, "local Markdown images keep an intrinsic first-frame aspect ratio");

const authorizedInlineAssets = new InlineImageAssetCache(3);
assert.equal(authorizedInlineAssets.begin({
  sid: "temp-session",
  path: "/tmp/outside.png",
  previewId: "preview-auth",
  requestId: "request-auth",
}), true);
assert.equal(authorizedInlineAssets.rekeySession(
  "temp-session", "real-session"), true);
assert.equal(authorizedInlineAssets.forSession(
  "temp-session")["/tmp/outside.png"], undefined);
assert.equal(authorizedInlineAssets.requireAuthorization({
  v: 28,
  type: "preview_authorization_required",
  ts: 4,
  sid: "wrong-session",
  authorization_id: "authorization-1",
  request_id: "request-auth",
  operation: "preview_asset",
  path: "/tmp/outside.png",
  resolved_path: "/tmp/outside.png",
  format: "image",
  preview_id: "preview-auth",
}), false, "a challenge from another session cannot capture the pending image");
assert.equal(authorizedInlineAssets.requireAuthorization({
  v: 28,
  type: "preview_authorization_required",
  ts: 5,
  sid: "real-session",
  authorization_id: "authorization-1",
  request_id: "request-auth",
  operation: "preview_asset",
  path: "/tmp/outside.png",
  resolved_path: "/tmp/outside.png",
  format: "image",
  preview_id: "preview-auth",
}), true);
const inlineAuthorization = authorizedInlineAssets.forSession(
  "real-session")["/tmp/outside.png"].authorization!;
assert.equal(inlineAuthorization.status, "required");
assert.equal(inlineAuthorization.sid, "real-session");
assert.equal(authorizedInlineAssets.markAuthorizationSubmitting(
  inlineAuthorization), true);
assert.equal(authorizedInlineAssets.forSession(
  "real-session")["/tmp/outside.png"].authorization?.status, "submitting");
assert.equal(authorizedInlineAssets.acceptAuthorizationResult({
  v: 28,
  type: "preview_authorization_result",
  ts: 6,
  sid: "wrong-session",
  authorization_id: "authorization-1",
  request_id: "request-auth",
  operation: "preview_asset",
  path: "/tmp/outside.png",
  preview_id: "preview-auth",
  status: "granted",
}), false);
assert.deepEqual(authorizedInlineAssets.acceptAuthorizationResult({
  v: 28,
  type: "preview_authorization_result",
  ts: 7,
  sid: "real-session",
  authorization_id: "authorization-1",
  request_id: "request-auth",
  operation: "preview_asset",
  path: "/tmp/outside.png",
  preview_id: "preview-auth",
  status: "granted",
}), {
  sid: "real-session",
  path: "/tmp/outside.png",
  assetKey: undefined,
  previewId: "preview-auth",
  requestId: "request-auth",
});
assert.equal(authorizedInlineAssets.acceptAuthorizationResult({
  v: 28,
  type: "preview_authorization_result",
  ts: 8,
  sid: "real-session",
  authorization_id: "authorization-1",
  request_id: "request-auth",
  operation: "preview_asset",
  path: "/tmp/outside.png",
  preview_id: "preview-auth",
  status: "granted",
}), false, "a duplicate grant result cannot start another retry");
assert.equal(authorizedInlineAssets.accept({
  v: 28,
  type: "preview_asset",
  ts: 9,
  sid: "real-session",
  path: "/tmp/outside.png",
  preview_id: "preview-auth",
  request_id: "request-auth",
  media_type: "image/png",
  data: pngHeaderBase64,
}), true);
assert.equal(authorizedInlineAssets.forSession(
  "real-session")["/tmp/outside.png"].status, "ready");

const reorderedAuthorizedImage = new InlineImageAssetCache(1);
assert.equal(reorderedAuthorizedImage.begin({
  sid: "btw-session",
  path: "/tmp/reordered.png",
  previewId: "reordered-preview",
  requestId: "reordered-request",
}), true);
assert.equal(reorderedAuthorizedImage.requireAuthorization({
  v: 28,
  type: "preview_authorization_required",
  ts: 9.1,
  sid: "btw-session",
  authorization_id: "reordered-authorization",
  request_id: "reordered-request",
  operation: "preview_asset",
  path: "/tmp/reordered.png",
  resolved_path: "/private/tmp/reordered.png",
  format: "image",
  preview_id: "reordered-preview",
}), true);
const reorderedAuthorization = reorderedAuthorizedImage.forSession(
  "btw-session")["/tmp/reordered.png"].authorization!;
const reorderedAsset = {
  v: 28,
  type: "preview_asset",
  ts: 9.2,
  sid: "btw-session",
  path: "/tmp/reordered.png",
  preview_id: "reordered-preview",
  request_id: "reordered-request",
  media_type: "image/png",
  data: pngHeaderBase64,
} as const;
assert.equal(reorderedAuthorizedImage.accept(reorderedAsset), false,
  "an external asset must not bypass the user's confirmation");
assert.equal(reorderedAuthorizedImage.forSession(
  "btw-session")["/tmp/reordered.png"].authorization?.status, "required");
assert.equal(reorderedAuthorizedImage.markAuthorizationSubmitting(
  reorderedAuthorization), true);
assert.equal(reorderedAuthorizedImage.accept(reorderedAsset), true,
  "an authorized asset may overtake its replayed grant result");
assert.equal(reorderedAuthorizedImage.forSession(
  "btw-session")["/tmp/reordered.png"].status, "ready");
assert.equal(reorderedAuthorizedImage.acceptAuthorizationResult({
  v: 28,
  type: "preview_authorization_result",
  ts: 9.3,
  sid: "btw-session",
  authorization_id: "reordered-authorization",
  request_id: "reordered-request",
  operation: "preview_asset",
  path: "/tmp/reordered.png",
  preview_id: "reordered-preview",
  status: "granted",
}), false, "the late grant result must not issue a duplicate asset read");
assert.equal(reorderedAuthorizedImage.forSession(
  "btw-session")["/tmp/reordered.png"].status, "ready");

assert.equal(authorizedInlineAssets.begin({
  sid: "real-session",
  path: "/tmp/denied.png",
  previewId: "preview-denied",
  requestId: "request-denied",
}), true);
assert.equal(authorizedInlineAssets.requireAuthorization({
  v: 28,
  type: "preview_authorization_required",
  ts: 10,
  sid: "real-session",
  authorization_id: "authorization-denied",
  request_id: "request-denied",
  operation: "preview_asset",
  path: "/tmp/denied.png",
  resolved_path: "/tmp/denied.png",
  format: "image",
  preview_id: "preview-denied",
}), true);
const deniedInlineAuthorization = authorizedInlineAssets.forSession(
  "real-session")["/tmp/denied.png"].authorization!;
assert.equal(authorizedInlineAssets.markAuthorizationSubmitting(
  deniedInlineAuthorization), true);
assert.equal(authorizedInlineAssets.acceptAuthorizationResult({
  v: 28,
  type: "preview_authorization_result",
  ts: 11,
  sid: "real-session",
  authorization_id: "authorization-denied",
  request_id: "request-denied",
  operation: "preview_asset",
  path: "/tmp/denied.png",
  preview_id: "preview-denied",
  status: "denied",
}), null);
assert.equal(authorizedInlineAssets.forSession(
  "real-session")["/tmp/denied.png"].status, "error");
assert.deepEqual(mutatedFilePaths("Write", {
  file_path: "/tmp/claude.txt",
}), ["/tmp/claude.txt"]);
assert.deepEqual(mutatedFilePaths("apply_patch", {
  changes: [
    { path: "/tmp/codex.txt", kind: "add" },
    { path: "/tmp/old.txt", move_path: "/tmp/new.txt", kind: "move" },
  ],
}), ["/tmp/codex.txt", "/tmp/old.txt", "/tmp/new.txt"]);
assert.deepEqual(filePathsFromInput({
  file_paths: ["/tmp/a", "/tmp/a"],
  changes: { "/tmp/b": { type: "add" } },
}), ["/tmp/a", "/tmp/b"]);
assert.deepEqual(mutatedFilePaths("Read", {
  file_path: "/tmp/secret.txt",
}), [], "read-only tools must never be treated as mutations");
assert.deepEqual(collectTurnFileChanges([
  { kind: "tool", tool: "apply_patch", input: {
    file_paths: ["/tmp/current-turn.md"],
  }, result: { diff: "--- /dev/null\n+++ /tmp/current-turn.md\n@@ -0,0 +1 @@\n+1\n" } },
  { kind: "tool", tool: "Read", input: {
    file_path: "/home/nancy/project/unrelated.py",
  }, result: { diff: "--- a/unrelated.py\n+++ b/unrelated.py\n" } },
]), {
  paths: ["/tmp/current-turn.md"],
  diff: "--- /dev/null\n+++ /tmp/current-turn.md\n@@ -0,0 +1 @@\n+1",
}, "a turn summary must use only its mutation events, never the worktree diff");

const harness = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});
try {
  const { initialState, reduce } = await harness.ssrLoadModule("/src/reducer.ts");
  const {
    advanceMarkdownMathState,
    hasMathDelimiters,
    normalizeMathDelimiters,
    preloadMarkdownMathPlugins,
  } = await harness.ssrLoadModule(
    "/src/markdown-math.ts");
  await preloadMarkdownMathPlugins();
  const malformedMath = String.raw`坏的 \(text；正常 \(x^2\)`;
  assert.equal(
    normalizeMathDelimiters(malformedMath),
    String.raw`坏的 \(text；正常 $x^2$`,
    "an unmatched opener must not consume a later complete formula",
  );
  assert.equal(hasMathDelimiters(malformedMath), true);
  let streamedMath = advanceMarkdownMathState({
    source: "",
    normalized: "",
    active: false,
  }, "\\(x\\");
  assert.equal(streamedMath.active, false);
  streamedMath = advanceMarkdownMathState(
    streamedMath, "\\(x\\)");
  assert.deepEqual(streamedMath, {
    source: "\\(x\\)",
    normalized: "$x$",
    active: true,
  }, "a closing bracket delimiter split after its slash activates immediately");
  let streamedDisplayMath = advanceMarkdownMathState({
    source: "",
    normalized: "",
    active: false,
  }, "\\[x\\");
  streamedDisplayMath = advanceMarkdownMathState(
    streamedDisplayMath, "\\[x\\]");
  assert.equal(streamedDisplayMath.active, true);
  assert.equal(streamedDisplayMath.normalized, "\n$$\nx\n$$\n");
  const escapedSplit = advanceMarkdownMathState({
    source: "literal \\\\",
    normalized: "literal \\\\",
    active: false,
  }, "literal \\\\)");
  assert.equal(escapedSplit.active, false,
    "an even trailing slash run must stay escaped across the append boundary");
  const { ArtifactPanel } = await harness.ssrLoadModule(
    "/src/components/ArtifactPanel.tsx");
  const { buildSandboxDocument } = await harness.ssrLoadModule(
    "/src/html-preview.ts");
  const { MessageBlock } = await harness.ssrLoadModule(
    "/src/components/MessageBlock.tsx");
  const { preloadMarkdownExtras } = await harness.ssrLoadModule(
    "/src/use-markdown-extras.ts");
  await preloadMarkdownExtras();
  const disclosureMarkup = renderToStaticMarkup(createElement(MessageBlock, {
    text: "Before\n\n<details>\n<summary>文件清单</summary>\n\n"
      + "- **new** `README.md`\n- [source](/tmp/source.py)\n\n"
      + "<details open>\n<summary>Nested</summary>\n\nInner body\n\n</details>\n\n"
      + "</details>\n\nAfter",
    done: true,
    onOpenFile: () => {},
  }));
  assert.match(disclosureMarkup, /<details class="message-disclosure">\s*<summary>文件清单<\/summary>/);
  assert.match(disclosureMarkup, /<strong>new<\/strong>/);
  assert.match(disclosureMarkup, /message-file-link/);
  assert.match(disclosureMarkup, /<details class="message-disclosure" open="">\s*<summary>Nested<\/summary>/);
  assert.match(disclosureMarkup, /<p>After<\/p>/);
  const unsafeDisclosure = renderToStaticMarkup(createElement(MessageBlock, {
    text: '<details>\n<summary>Safe</summary>\n\n'
      + '<script>alert(1)</script><img src=x onerror="alert(2)">\n\n'
      + '<iframe src="https://example.com"></iframe>\n\n</details>',
    done: true,
  }));
  assert.doesNotMatch(unsafeDisclosure, /<(script|img|iframe)\b/);
  assert.match(unsafeDisclosure, /&lt;script&gt;/);
  const literalDisclosure = renderToStaticMarkup(createElement(MessageBlock, {
    text: '```html\n<details><summary>Example</summary></details>\n```',
    done: true,
  }));
  assert.doesNotMatch(literalDisclosure, /<details>/);
  assert.match(literalDisclosure, /&lt;details&gt;/);
  for (const raw of [
    '<details ontoggle="alert(1)"><summary>Title</summary></details>',
    '<details style="position:fixed"><summary>Title</summary></details>',
    '<details>\n<summary>Title</summary>\n\n<a href="javascript:alert(1)">x</a>\n</details>',
    '<details>\n<summary>Title</summary>\n\n<svg><script>alert(1)</script></svg>\n</details>',
  ]) {
    const markup = renderToStaticMarkup(createElement(MessageBlock, { text: raw, done: true }));
    assert.doesNotMatch(markup, /<(?:a|svg|script)\b|<details[^>]+(?:ontoggle|style)=/);
  }
  const codeCopyMarkup = renderToStaticMarkup(createElement(MessageBlock, {
    text: "请执行：\n\n```sh\necho ready\n```",
    done: true,
  }));
  assert.match(codeCopyMarkup, /aria-label="复制代码"/,
    "fenced commands need a local copy action without scrolling to turn end");
  assert.match(codeCopyMarkup, /echo ready/);
  const streamingMermaidMarkup = renderToStaticMarkup(createElement(MessageBlock, {
    text: "```mermaid\nflowchart LR\nA --> B\n```",
    done: false,
  }));
  assert.match(streamingMermaidMarkup, /message-code-block/);
  assert.match(streamingMermaidMarkup, /aria-label="复制代码"/);
  assert.doesNotMatch(streamingMermaidMarkup, /data-mermaid-state=/,
    "streaming Mermaid stays source-only until the turn is terminal");
  const completedMermaidMarkup = renderToStaticMarkup(createElement(MessageBlock, {
    text: "```mermaid\nflowchart LR\nA --> B\n```",
    done: true,
  }));
  assert.match(completedMermaidMarkup, /data-mermaid-state="idle"/);
  assert.match(completedMermaidMarkup, /class="mermaid-source"/);
  assert.match(completedMermaidMarkup, /flowchart LR/);
  assert.match(completedMermaidMarkup, /aria-label="复制 Mermaid 源码"/);
  const completedDisplayBracketMath = renderToStaticMarkup(
    createElement(MessageBlock, {
      text: String.raw`\[ r = \frac{h}{\sin |\alpha|} \]`,
      done: true,
    }),
  );
  assert.match(completedDisplayBracketMath, /class="katex-display"/,
    "completed assistant bracket-delimited display math must render with KaTeX");
  assert.doesNotMatch(completedDisplayBracketMath, /aria-label="复制代码"/,
    "math output must not be wrapped in the fenced-code copy UI");
  const completedInlineBracketMath = renderToStaticMarkup(
    createElement(MessageBlock, {
      text: String.raw`半径为 \(r = h / \sin \alpha\)。`,
      done: true,
    }),
  );
  assert.match(completedInlineBracketMath, /class="katex"/,
    "completed assistant bracket-delimited inline math must render with KaTeX");
  const completedDollarMath = renderToStaticMarkup(createElement(MessageBlock, {
    text: String.raw`inline $r=h$

$$
h=r\sin\alpha
$$`,
    done: true,
  }));
  assert.match(completedDollarMath, /class="katex"/);
  assert.match(completedDollarMath, /class="katex-display"/,
    "both dollar-delimited inline and display math remain supported");
  const streamingMathMarkup = renderToStaticMarkup(createElement(MessageBlock, {
    text: String.raw`\[ r = \frac{h}{\sin |\alpha|} \]`,
    done: false,
  }));
  assert.match(streamingMathMarkup, /class="katex-display"/,
    "a complete streaming formula renders before the message is terminal");
  const incompleteStreamingMathMarkup = renderToStaticMarkup(
    createElement(MessageBlock, {
      text: String.raw`推导中：\( r = \frac{h}{\sin |\alpha|}`,
      done: false,
    }),
  );
  assert.doesNotMatch(incompleteStreamingMathMarkup, /class="katex/,
    "an unmatched streaming opener must remain readable source");
  assert.match(incompleteStreamingMathMarkup, /\\frac/);
  const mixedStreamingMathMarkup = renderToStaticMarkup(
    createElement(MessageBlock, {
      text: String.raw`已完成 \(x^2\)，继续 \(y^2`,
      done: false,
    }),
  );
  assert.match(mixedStreamingMathMarkup, /class="katex"/,
    "matched spans render without waiting for an incomplete tail");
  assert.match(mixedStreamingMathMarkup, /继续 \(y\^2/,
    "normalization must not consume an unmatched tail opener");
  const literalMathMarkup = renderToStaticMarkup(createElement(MessageBlock, {
    text: "Inline code: `\\(\\alpha\\)`\n\n"
      + "```text\n\\[not math\\]\n```",
    done: true,
  }));
  assert.doesNotMatch(literalMathMarkup, /class="katex/,
    "math delimiters inside inline and fenced code remain literal");
  assert.match(literalMathMarkup, /aria-label="复制代码"/);
  const plainBracketMarkup = renderToStaticMarkup(createElement(MessageBlock, {
    text: "普通数组 [1, 2, 3] 不是数学定界符。",
    done: true,
  }));
  assert.doesNotMatch(plainBracketMarkup, /class="katex/,
    "ordinary square brackets must never be guessed as display math");
  const untrustedMathMarkup = renderToStaticMarkup(createElement(MessageBlock, {
    text: String.raw`$\href{javascript:alert(1)}{unsafe}$`,
    done: true,
  }));
  assert.doesNotMatch(untrustedMathMarkup, /href="javascript:/,
    "KaTeX trust stays disabled for model-provided commands");
  const codexDirectiveMarkup = renderToStaticMarkup(createElement(MessageBlock, {
    text: "提交完成。\n\n::git-commit{cwd=\"/tmp/private-project\"}",
    done: true,
  }));
  assert.match(codexDirectiveMarkup, /Git 提交已创建/,
    "Codex App git directives need a native status instead of leaking wire text");
  assert.doesNotMatch(codexDirectiveMarkup, /::git-commit|private-project/,
    "directive attributes are local UI metadata and must not render as prose");
  const codexVisualizationMarkup = renderToStaticMarkup(createElement(MessageBlock, {
    text: "visualize{\"path\":\"/Volumes/MuggleSSD/workspace/robot-dog/"
      + ".visualizations/concept.html\",\"title\":\"双轮足结构草图\"}"
      + "\n\n请确认结构方向。",
    done: true,
    onOpenFile: () => {},
  }));
  assert.match(codexVisualizationMarkup, /codex-visualization-card/,
    "Codex visualize directives need a native artifact entry");
  assert.match(codexVisualizationMarkup, /双轮足结构草图/);
  assert.match(codexVisualizationMarkup, /HTML 可视化/);
  assert.match(codexVisualizationMarkup, /请确认结构方向/);
  assert.doesNotMatch(codexVisualizationMarkup, /visualize|\/Volumes\/MuggleSSD/,
    "visualize wire text and private absolute paths must not leak into prose");
  const invalidVisualizationMarkup = renderToStaticMarkup(createElement(MessageBlock, {
    text: "visualize{\"path\":\"https://example.com/concept.html\"}",
    done: true,
    onOpenFile: () => {},
  }));
  assert.doesNotMatch(invalidVisualizationMarkup, /codex-visualization-card/,
    "network targets must not be promoted to authenticated local previews");
  assert.match(invalidVisualizationMarkup, /visualize/,
    "an invalid directive remains visible instead of silently discarding content");
  const fencedDirectiveMarkup = renderToStaticMarkup(createElement(MessageBlock, {
    text: "```text\n::git-commit{cwd=\"/tmp/example\"}\n```",
    done: true,
  }));
  assert.match(fencedDirectiveMarkup, /::git-commit/,
    "a directive-shaped line inside a code fence remains literal code");
  const localQrMarkup = renderToStaticMarkup(createElement(MessageBlock, {
    text: "![飞书授权二维码](/Volumes/MuggleSSD/workspace/project/tmp-auth.png)",
    done: true,
    imageAssets: {},
    onLoadImage: () => true,
    onPreviewImage: () => {},
  }));
  assert.match(localQrMarkup, /message-image-loading/);
  assert.doesNotMatch(localQrMarkup, /src="\/Volumes\//,
    "a local assistant image path must never become a public HTTP request");
  const loadedQrMarkup = renderToStaticMarkup(createElement(MessageBlock, {
    text: "![飞书授权二维码](/Volumes/MuggleSSD/workspace/project/tmp-auth.png)",
    done: true,
    imageAssets: {
      "/Volumes/MuggleSSD/workspace/project/tmp-auth.png": {
        status: "ready", mediaType: "image/png", data: pngHeaderBase64,
        width: 640, height: 480,
      },
    },
    onPreviewImage: () => {},
  }));
  assert.match(loadedQrMarkup, /class="message-image-trigger"/);
  assert.match(loadedQrMarkup, /src="data:image\/png;base64,/);
  assert.match(loadedQrMarkup, /width="640"/);
  assert.match(loadedQrMarkup, /height="480"/);
  assert.match(loadedQrMarkup, /aria-label="预览图片：飞书授权二维码"/);

  const { NewChatView } = await harness.ssrLoadModule(
    "/src/components/NewChatView.tsx");
  const newChatMarkup = renderToStaticMarkup(createElement(NewChatView, {
    cwd: "/tmp/project",
    controlScopeKey: "machine-a:code:claude",
    onPickCwd: () => {},
    onSend: () => true,
  }));
  assert.match(newChatMarkup, /aria-label="添加照片"/);
  assert.match(newChatMarkup, /aria-label="添加文件"/);
  assert.equal(
    (newChatMarkup.match(/<button[^>]+aria-label="添加照片"/g) ?? []).length, 1);
  assert.equal(
    (newChatMarkup.match(/<button[^>]+aria-label="添加文件"/g) ?? []).length, 0);
  assert.match(newChatMarkup, /type="file"[^>]*accept="image\/\*"[^>]*multiple/,
    "iPhone photo selection needs a dedicated multi-select image input");
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

  let authorizationState = reduce(initialState, {
    type: "open_file_loading",
    file: "/tmp/outside.md",
    sid: "session-1",
    requestId: "external-preview",
    kind: "md",
  });
  const beforeAuthorization = authorizationState;
  authorizationState = reduce(authorizationState, {
    type: "event",
    event: {
      v: 28,
      type: "preview_authorization_required",
      ts: 20,
      sid: "other-session",
      authorization_id: "file-authorization",
      request_id: "external-preview",
      operation: "file_preview",
      path: "/tmp/outside.md",
      resolved_path: "/tmp/outside.md",
      format: "markdown",
    } as ServerEvent,
  });
  assert.equal(authorizationState, beforeAuthorization,
    "another session cannot attach a file confirmation to the open panel");
  authorizationState = reduce(authorizationState, {
    type: "event",
    event: {
      v: 28,
      type: "preview_authorization_required",
      ts: 21,
      sid: "session-1",
      authorization_id: "file-authorization",
      request_id: "external-preview",
      operation: "file_preview",
      path: "/tmp/outside.md",
      resolved_path: "/private/tmp/outside.md",
      format: "markdown",
    } as ServerEvent,
  });
  assert.equal(authorizationState.artifact?.loading, false);
  assert.equal(authorizationState.artifact?.authorization?.status, "required");
  authorizationState = reduce(authorizationState, {
    type: "submit_preview_authorization",
    sid: "session-1",
    authorizationId: "file-authorization",
    requestId: "external-preview",
  });
  assert.equal(
    authorizationState.artifact?.authorization?.status,
    "submitting",
  );
  const beforeStaleResult = authorizationState;
  authorizationState = reduce(authorizationState, {
    type: "event",
    event: {
      v: 28,
      type: "preview_authorization_result",
      ts: 22,
      sid: "session-1",
      authorization_id: "stale-authorization",
      request_id: "external-preview",
      operation: "file_preview",
      path: "/tmp/outside.md",
      status: "granted",
    } as ServerEvent,
  });
  assert.equal(authorizationState, beforeStaleResult);
  authorizationState = reduce(authorizationState, {
    type: "event",
    event: {
      v: 28,
      type: "preview_authorization_result",
      ts: 23,
      sid: "session-1",
      authorization_id: "file-authorization",
      request_id: "external-preview",
      operation: "file_preview",
      path: "/tmp/outside.md",
      status: "granted",
    } as ServerEvent,
  });
  assert.equal(authorizationState.artifact?.authorization?.status, "granted");
  const retryFailedState = reduce(authorizationState, {
    type: "preview_authorization_retry_failed",
    sid: "session-1",
    authorizationId: "file-authorization",
    requestId: "external-preview",
  });
  assert.equal(retryFailedState.artifact?.authorization, undefined);
  assert.match(retryFailedState.artifact?.error ?? "", /刷新文件/);
  authorizationState = reduce(authorizationState, {
    type: "preview_authorization_retry_started",
    sid: "session-1",
    authorizationId: "file-authorization",
    requestId: "external-preview",
  });
  assert.equal(authorizationState.artifact?.authorization, undefined);
  assert.equal(authorizationState.artifact?.loading, true);
  authorizationState = reduce(authorizationState, {
    type: "event",
    event: {
      v: 28,
      type: "file_preview",
      ts: 24,
      sid: "session-1",
      path: "/private/tmp/outside.md",
      request_id: "external-preview",
      format: "markdown",
      content: "# read only",
      size: 11,
      truncated: false,
      mtime_ns: "10",
      revision: "e".repeat(64),
      writable: false,
    } as ServerEvent,
  });
  assert.equal(authorizationState.artifact?.content, "# read only");
  assert.equal(authorizationState.artifact?.writable, false);

  let deniedState = reduce(initialState, {
    type: "open_file_loading",
    file: "/tmp/denied.md",
    sid: "session-1",
    requestId: "denied-preview",
    kind: "md",
  });
  deniedState = reduce(deniedState, {
    type: "event",
    event: {
      v: 28,
      type: "preview_authorization_required",
      ts: 25,
      sid: "session-1",
      authorization_id: "denied-authorization",
      request_id: "denied-preview",
      operation: "file_preview",
      path: "/tmp/denied.md",
      resolved_path: "/tmp/denied.md",
      format: "markdown",
    } as ServerEvent,
  });
  deniedState = reduce(deniedState, {
    type: "event",
    event: {
      v: 28,
      type: "preview_authorization_result",
      ts: 26,
      sid: "session-1",
      authorization_id: "denied-authorization",
      request_id: "denied-preview",
      operation: "file_preview",
      path: "/tmp/denied.md",
      status: "denied",
    } as ServerEvent,
  });
  assert.equal(deniedState.artifact?.authorization, undefined);
  assert.match(deniedState.artifact?.error ?? "", /取消/);

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

  state = reduce(state, {
    type: "begin_preview_asset",
    sid: "session-1",
    path: "docs/image.png",
    previewId: "preview-new",
    requestId: "asset-1",
  });
  const pendingAsset = state;
  state = reduce(state, { type: "event", event: {
    v: 10,
    type: "preview_asset",
    ts: 4,
    sid: "session-1",
    path: "docs/image.png",
    preview_id: "preview-new",
    request_id: "asset-stale",
    media_type: "image/png",
    data: "cG5n",
  } as ServerEvent });
  assert.equal(state, pendingAsset,
    "a stale asset response cannot satisfy the active image request");
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
    requestId: "asset-1",
    previewId: "preview-new",
    loading: false,
    mediaType: "image/png",
    data: "cG5n",
    error: undefined,
  });

  let assetAuthorizationState = reduce(state, {
    type: "begin_preview_asset",
    sid: "session-1",
    path: "/tmp/chart.png",
    previewId: "preview-new",
    requestId: "asset-auth",
  });
  assetAuthorizationState = reduce(assetAuthorizationState, {
    type: "event",
    event: {
      v: 28,
      type: "preview_authorization_required",
      ts: 27,
      sid: "session-1",
      authorization_id: "asset-authorization",
      request_id: "asset-auth",
      operation: "preview_asset",
      path: "/tmp/chart.png",
      resolved_path: "/private/tmp/chart.png",
      format: "image",
      preview_id: "wrong-preview",
    } as ServerEvent,
  });
  assert.equal(
    assetAuthorizationState.artifact?.assets?.["/tmp/chart.png"]
      ?.authorization,
    undefined,
  );
  assetAuthorizationState = reduce(assetAuthorizationState, {
    type: "event",
    event: {
      v: 28,
      type: "preview_authorization_required",
      ts: 28,
      sid: "session-1",
      authorization_id: "asset-authorization",
      request_id: "asset-auth",
      operation: "preview_asset",
      path: "/tmp/chart.png",
      resolved_path: "/private/tmp/chart.png",
      format: "image",
      preview_id: "preview-new",
    } as ServerEvent,
  });
  assert.equal(
    assetAuthorizationState.artifact?.assets?.["/tmp/chart.png"]
      ?.authorization?.status,
    "required",
  );
  assetAuthorizationState = reduce(assetAuthorizationState, {
    type: "submit_preview_authorization",
    sid: "session-1",
    authorizationId: "asset-authorization",
    requestId: "asset-auth",
  });
  assetAuthorizationState = reduce(assetAuthorizationState, {
    type: "event",
    event: {
      v: 28,
      type: "preview_authorization_result",
      ts: 29,
      sid: "session-1",
      authorization_id: "asset-authorization",
      request_id: "asset-auth",
      operation: "preview_asset",
      path: "/tmp/chart.png",
      preview_id: "preview-new",
      status: "granted",
    } as ServerEvent,
  });
  assert.equal(
    assetAuthorizationState.artifact?.assets?.["/tmp/chart.png"]
      ?.authorization?.status,
    "granted",
  );
  assetAuthorizationState = reduce(assetAuthorizationState, {
    type: "preview_authorization_retry_started",
    sid: "session-1",
    authorizationId: "asset-authorization",
    requestId: "asset-auth",
  });
  assert.equal(
    assetAuthorizationState.artifact?.assets?.["/tmp/chart.png"]?.loading,
    true,
  );
  assetAuthorizationState = reduce(assetAuthorizationState, {
    type: "event",
    event: {
      v: 28,
      type: "preview_asset",
      ts: 30,
      sid: "session-1",
      path: "/tmp/chart.png",
      preview_id: "preview-new",
      request_id: "asset-auth",
      media_type: "image/png",
      data: pngHeaderBase64,
    } as ServerEvent,
  });
  assert.equal(
    assetAuthorizationState.artifact?.assets?.["/tmp/chart.png"]?.data,
    pngHeaderBase64,
  );
  const rekeyedAuthorizationState = reduce(assetAuthorizationState, {
    type: "event",
    event: {
      v: 28,
      type: "session_rekey",
      ts: 31,
      old_key: "session-1",
      session_id: "session-real",
      cwd: "/tmp/project",
    } as ServerEvent,
  });
  assert.equal(rekeyedAuthorizationState.artifact?.sid, "session-real");

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
  assert.doesNotMatch(markup, /alert\(1\)/);
  assert.doesNotMatch(markup, /<script>/);

  const renderHtmlMarkdown = (content: string) => renderToStaticMarkup(createElement(ArtifactPanel, {
    artifact: {
      file: "docs/readme_zh.md", sid: "html-readme", requestId: "html-readme-r1",
      kind: "md", content, assets: {},
    },
    active: "diff", hasBtw: false, onTab: () => {}, onClose: () => {},
    onOpenFile: () => {},
  })).split('<div class="prose markdown-preview">')[1];
  const htmlMarkdown = renderHtmlMarkdown(MARKDOWN_HTML_README);
  assert.match(htmlMarkdown, /<h1 align="center">Microduck<\/h1>/);
  assert.match(htmlMarkdown, /<img[^>]*src="https:\/\/preview\.example\/header\.png"/);
  assert.match(htmlMarkdown, /<img[^>]*width="820"[^>]*height="320"/);
  assert.match(htmlMarkdown, /referrerPolicy="no-referrer"/);
  assert.match(htmlMarkdown, /<a href="#" title="docs\/README.md">English<\/a>/);
  assert.match(htmlMarkdown, /<a href="https:\/\/preview\.example\/project" target="_blank" rel="noopener noreferrer"/);
  assert.match(htmlMarkdown, /<em>通过强化学习/);
  assert.match(htmlMarkdown, /<strong>这个仓库/);
  assert.match(htmlMarkdown, /<details class="message-disclosure"><summary>安装说明<\/summary>/);
  assert.match(htmlMarkdown, /<strong>Markdown<\/strong>/);
  assert.match(htmlMarkdown, /<table>/);
  assert.match(htmlMarkdown, /<input[^>]*disabled=""/);
  assert.match(htmlMarkdown, /<h2 id="cc-preview-installation">安装<\/h2>/);
  assert.match(htmlMarkdown, /href="#cc-preview-installation"/);
  assert.doesNotMatch(htmlMarkdown, /<script|<style|<iframe|<form|<button>不可提交|<source/);
  assert.doesNotMatch(htmlMarkdown, /mdUnsafe|javascript:|onerror=|onclick=|position:fixed|srcset=/i);
  assert.doesNotMatch(htmlMarkdown, /<img[^>]*src="(?:\.\/|\/private\/|docs\/)/,
    "raw HTML images must use the existing local asset loader, never a browser-local URL");
  assert.match(renderHtmlMarkdown('```html\n<p align="center">example</p>\n```'),
    /&lt;p align=&quot;center&quot;&gt;example&lt;\/p&gt;/,
    "code examples remain literal HTML source");
  assert.match(renderHtmlMarkdown('<p>Formula: $x^2$</p>\n\n$$y = 2$$'), /class="katex"/,
    "HTML sanitation must run before the trusted math renderer");
  assert.match(renderHtmlMarkdown('<a name="旧章节"></a>\n\n[跳转](#%E6%97%A7%E7%AB%A0%E8%8A%82)'),
    /href="#cc-preview-旧章节"/);
  const footnotes = renderHtmlMarkdown('Note[^one]\n\n[^one]: Kept footnote');
  assert.match(footnotes, /id="cc-preview-user-content-fn-one"/);
  assert.match(footnotes, /href="#cc-preview-user-content-fn-one"/);
  const chatStillLiteral = renderToStaticMarkup(createElement(MessageBlock, {
    text: '<h1 align="center">Literal chat HTML</h1>', done: true,
  }));
  assert.doesNotMatch(chatStillLiteral, /<h1/);
  assert.match(chatStillLiteral, /&lt;h1/);

  for (const unsafe of [
    '<a href="jav&#x61;script:alert(1)">x</a>',
    '<a href="java&#10;script:alert(1)">x</a>',
    '<svg><a href="javascript:alert(1)">x</a><script>alert(1)</script></svg>',
    '<math><mi xlink:href="data:text/html,bad">x</mi></math>',
    '<img src="data:image/svg+xml,bad" style="width:999999px" onerror="alert(1)">',
    '<div id="__proto__" name="location" class="preview-injected"><p>content</p></div>',
    '<object data="https://preview.example/private"></object>',
  ]) {
    const safe = renderHtmlMarkdown(unsafe);
    assert.doesNotMatch(safe, /<svg|<math|<script|<object|javascript:|xlink:href|onerror=/i);
    assert.doesNotMatch(safe, /id="__proto__"|name="location"|class="preview-injected"/);
    assert.doesNotMatch(safe, /<img[^>]*(?:style=|src="data:)/);
  }
  const readOnlyMarkup = renderToStaticMarkup(createElement(ArtifactPanel, {
    artifact: authorizationState.artifact!,
    active: "diff",
    hasBtw: false,
    onTab: () => {},
    onClose: () => {},
    onSaveMarkdown: () => "save-should-not-run",
  }));
  assert.match(readOnlyMarkup, /title="此文件仅获准查看"/);
  assert.match(readOnlyMarkup, /class="markdown-save" disabled=""/,
    "a user-approved external Markdown file remains read-only");
  const mermaidPreviewMarkup = renderToStaticMarkup(createElement(ArtifactPanel, {
    artifact: {
      file: "docs/diagram.md",
      sid: "session-1",
      requestId: "preview-mermaid",
      kind: "md",
      content: "```mermaid\nsequenceDiagram\nA->>B: Hello\n```",
      size: 48,
      mtimeNs: "3",
      revision: "b".repeat(64),
      assets: {},
    },
    active: "diff",
    hasBtw: false,
    onTab: () => {},
    onClose: () => {},
  }));
  assert.match(mermaidPreviewMarkup, /data-mermaid-state="idle"/);
  assert.match(mermaidPreviewMarkup, /sequenceDiagram/);
  assert.doesNotMatch(mermaidPreviewMarkup, /<pre><div/,
    "the Mermaid component must not be nested inside an invalid pre element");
  const mathPreviewMarkup = renderToStaticMarkup(createElement(ArtifactPanel, {
    artifact: {
      file: "docs/formula.md",
      sid: "session-1",
      requestId: "preview-math",
      kind: "md",
      content: String.raw`\[ r = \frac{h}{\sin |\alpha|} \]`,
      size: 42,
      mtimeNs: "4",
      revision: "c".repeat(64),
      assets: {},
    },
    active: "diff",
    hasBtw: false,
    onTab: () => {},
    onClose: () => {},
  }));
  assert.match(mathPreviewMarkup, /class="katex-display"/,
    "artifact Markdown preview and chat messages must share math rendering");
  assert.doesNotMatch(mathPreviewMarkup, /aria-label="复制代码"/);

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

  state = reduce(state, {
    type: "open_file_loading",
    file: "report.pdf",
    sid: "session-1",
    requestId: "binary-1",
    kind: "file",
  });
  state = reduce(state, { type: "event", event: {
    v: 20,
    type: "file_preview",
    ts: 6,
    sid: "session-1",
    path: "report.pptx",
    request_id: "binary-1",
    format: "pdf",
    content: "",
    media_type: "application/pdf",
    data: "JVBERi0xLjcK",
    converted_from: "pptx",
    size: 8192,
    truncated: false,
    mtime_ns: "4",
  } as ServerEvent });
  assert.equal(state.artifact?.kind, "pdf");
  assert.equal(state.artifact?.mediaType, "application/pdf");
  assert.equal(state.artifact?.convertedFrom, "pptx");

  const pdfMarkup = renderToStaticMarkup(createElement(ArtifactPanel, {
    artifact: state.artifact!,
    active: "diff",
    hasBtw: false,
    onTab: () => {},
    onClose: () => {},
  }));
  assert.match(pdfMarkup, /rendered-artifact-body/);
  assert.match(pdfMarkup, /PPTX → PDF/);
  assert.match(pdfMarkup, /正在准备预览/);

  const sandbox = buildSandboxDocument("<h1>safe</h1>");
  assert.match(sandbox, /Content-Security-Policy/);
  assert.match(sandbox, /default-src &#39;none&#39;|default-src 'none'/);
  assert.match(sandbox, /<body><h1>safe<\/h1><\/body>/);
  const darkVisualizationSandbox = buildSandboxDocument(
    "<div style=\"color:var(--foreground)\">visual</div>", "", "dark");
  assert.match(darkVisualizationSandbox, /data-theme="dark"/);
  assert.match(darkVisualizationSandbox, /--foreground:#ecedf3/);
  assert.match(darkVisualizationSandbox, /--viz-series-1:#8590ff/,
    "Codex visualizations need their host palette inside the isolated iframe");
  const lightVisualizationSandbox = buildSandboxDocument("visual", "", "light");
  assert.match(lightVisualizationSandbox, /data-theme="light"/);
  assert.match(lightVisualizationSandbox, /--background:#fff/);

  const artifactPanelSource = readFileSync(
    resolve(process.cwd(), "src/components/ArtifactPanel.tsx"), "utf8");
  assert.match(artifactPanelSource, /new DOMParser\(\)\.parseFromString/);
  assert.match(artifactPanelSource, /sandbox="allow-scripts"/);
  assert.doesNotMatch(artifactPanelSource, /allow-same-origin/);
  assert.match(artifactPanelSource, /iframe,object,embed,form,base,link,meta\[http-equiv\],script\[src\]/);
  assert.match(artifactPanelSource, /\["md", "html"\]\.includes\(artifact\.kind\)/);
  assert.match(artifactPanelSource, /artifact\.kind === "html" && mode === "preview"/);
  assert.match(artifactPanelSource, /mode === "source"[\s\S]*?<SourceFile content=\{artifact\.content/);
} finally {
  await harness.close();
}

console.log("markdown preview tests passed");
