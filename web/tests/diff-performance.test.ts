import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { performance } from "node:perf_hooks";

import {
  buildEditDiffPreview,
  diffLines,
  GIT_DIFF_PAGE_LINES,
  MAX_FALLBACK_DIFF_LINES,
  pageGitDiff,
  type GitDiffSection,
} from "../src/diff.ts";

const oldText = Array.from({ length: 5_000 }, (_, index) => `a${index}`).join("\n");
const newText = Array.from({ length: 5_000 }, (_, index) => `b${index}`).join("\n");

assert.equal(Buffer.byteLength(oldText) + Buffer.byteLength(newText), 57_778);

global.gc?.();
const rssBefore = process.memoryUsage().rss;
const startedAt = performance.now();
const lines = diffLines(oldText, newText);
const elapsedMs = performance.now() - startedAt;
const rssDeltaMiB = (process.memoryUsage().rss - rssBefore) / 1024 / 1024;

console.log(JSON.stringify({
  fixtureBytes: 57_778,
  inputLines: 5_000,
  outputLines: lines.length,
  elapsedMs: Number(elapsedMs.toFixed(1)),
  rssDeltaMiB: Number(rssDeltaMiB.toFixed(1)),
}));

// The pre-fix implementation materialized 10,000 rows and a 25,010,001-cell
// LCS table for this fixture. Keep the production path hard-bounded.
assert.ok(lines.length <= MAX_FALLBACK_DIFF_LINES, `fallback rendered ${lines.length} rows`);
assert.ok(elapsedMs < 1_000, `fallback took ${elapsedMs.toFixed(1)}ms`);
assert.ok(rssDeltaMiB < 64, `fallback used ${rssDeltaMiB.toFixed(1)} MiB RSS`);

const serverPreview = buildEditDiffPreview(oldText, newText, "@@ authoritative server diff @@");
assert.deepEqual(serverPreview, { source: "server", diff: "@@ authoritative server diff @@" });
const smallPreview = buildEditDiffPreview("a\nb\nc", "a\nx\nc");
assert.equal(smallPreview.source, "fallback");
if (smallPreview.source === "fallback") {
  assert.deepEqual(smallPreview.lines, [
    { type: "ctx", text: "a" },
    { type: "del", text: "b" },
    { type: "add", text: "x" },
    { type: "ctx", text: "c" },
  ]);
}

// Source-level guards complement the pure-function tests without adding a DOM
// dependency: closed details must not even mount their expensive descendants.
const toolCardSource = readFileSync(
  new URL("../../../../src/components/ToolCallCard.tsx", import.meta.url), "utf8",
);
assert.match(toolCardSource, /open && <div className="tool-b">/);
assert.match(toolCardSource, /buildEditDiffPreview\(oldString, newString, serverDiff\)/);
const toolGroupSource = readFileSync(
  new URL("../../../../src/components/ToolGroup.tsx", import.meta.url), "utf8",
);
assert.match(toolGroupSource, /open && <div className="tool-group-b">/);

const artifactSections: GitDiffSection[] = [{
  file: "large.ts",
  hunks: [{
    header: "@@ -1,50000 +1,50000 @@",
    lines: Array.from({ length: 50_000 }, (_, index) => ({
      oldNo: index + 1,
      newNo: index + 1,
      type: "ctx" as const,
      text: `line-${index}`,
    })),
  }],
}];
const pageStartedAt = performance.now();
const diffPage = pageGitDiff(artifactSections, 9);
const pageElapsedMs = performance.now() - pageStartedAt;
assert.equal(diffPage.totalLines, 50_000);
assert.equal(diffPage.page, 9);
assert.equal(diffPage.startLine, 9 * GIT_DIFF_PAGE_LINES);
assert.equal(diffPage.sections[0].hunks[0].lines.length, GIT_DIFF_PAGE_LINES);
assert.equal(diffPage.sections.flatMap((section) => section.hunks)
  .reduce((count, hunk) => count + hunk.lines.length, 0), GIT_DIFF_PAGE_LINES);
assert.equal(pageGitDiff(artifactSections, Number.POSITIVE_INFINITY).page, 0);
assert.equal(pageGitDiff(artifactSections, 999_999).page, Math.ceil(50_000 / GIT_DIFF_PAGE_LINES) - 1);
assert.ok(pageElapsedMs < 250, `large diff pagination took ${pageElapsedMs.toFixed(1)}ms`);
console.log(JSON.stringify({
  artifactInputLines: 50_000,
  mountedLines: GIT_DIFF_PAGE_LINES,
  pageElapsedMs: Number(pageElapsedMs.toFixed(1)),
}));

const artifactPanelSource = readFileSync(
  new URL("../../../../src/components/ArtifactPanel.tsx", import.meta.url), "utf8",
);
assert.match(artifactPanelSource, /page\.sections\.map/);
assert.doesNotMatch(artifactPanelSource, /\{sections\.map/);

console.log("diff performance tests passed");
