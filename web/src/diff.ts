// Bounded line previews for rendering Edit old_string -> new_string.
// Output: add (blue +), del (pink −), ctx (dim) lines.

export interface DiffLine { type: "add" | "del" | "ctx"; text: string; }

// ---- git diff parser (Claude/GitHub-style: file sections + hunks + line numbers) ----

export interface GitDiffLine { oldNo: number | null; newNo: number | null; type: "add" | "del" | "ctx"; text: string; }
export interface GitDiffHunk { header: string; lines: GitDiffLine[]; }
export interface GitDiffSection { file: string; hunks: GitDiffHunk[]; }

export const GIT_DIFF_PAGE_LINES = 300;
export const MAX_FALLBACK_DIFF_LINES = 400;
const FALLBACK_CONTEXT_LINES = 3;

export interface GitDiffPage {
  sections: GitDiffSection[];
  page: number;
  pageCount: number;
  startLine: number;
  endLine: number;
  totalLines: number;
}

export type EditDiffPreview =
  | { source: "server"; diff: string }
  | { source: "fallback"; lines: DiffLine[]; truncated: boolean; omittedLines: number };

/** Parse raw `git diff` text into structured sections with line numbers.
 * Handles both `diff --git a/X b/X` headers and `--no-index` (untracked) diffs
 * that only carry `+++ b/X`. */
export function parseGitDiff(raw: string): GitDiffSection[] {
  const sections: GitDiffSection[] = [];
  let cur: GitDiffSection | null = null;
  let hunk: GitDiffHunk | null = null;
  let oldNo = 0, newNo = 0;
  for (const line of raw.split("\n")) {
    if (line.startsWith("diff --git ")) {
      const m = line.match(/ b\/(.+)$/);
      cur = { file: m ? m[1] : line.slice(11), hunks: [] };
      sections.push(cur);
      hunk = null;
      continue;
    }
    if (line.startsWith("+++ ")) {
      if (!cur) {
        const f = line.slice(4).replace(/^b\//, "");
        cur = { file: f === "/dev/null" ? "(new file)" : f, hunks: [] };
        sections.push(cur);
      }
      continue;
    }
    if (line.startsWith("--- ") || line.startsWith("index ") || line.startsWith("similarity ") || line.startsWith("rename ") || line.startsWith("new file ") || line.startsWith("deleted file ") || line.startsWith("old mode ") || line.startsWith("new mode ")) {
      continue;
    }
    if (line.startsWith("@@ ")) {
      const m = line.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      oldNo = m ? parseInt(m[1], 10) : 0;
      newNo = m ? parseInt(m[2], 10) : 0;
      hunk = { header: line, lines: [] };
      if (cur) cur.hunks.push(hunk);
      continue;
    }
    if (line.startsWith("\\ No newline")) continue;
    if (!cur || !hunk) continue;
    if (line.startsWith("+")) {
      hunk.lines.push({ oldNo: null, newNo: newNo++, type: "add", text: line.slice(1) });
    } else if (line.startsWith("-")) {
      hunk.lines.push({ oldNo: oldNo++, newNo: null, type: "del", text: line.slice(1) });
    } else {
      hunk.lines.push({ oldNo: oldNo++, newNo: newNo++, type: "ctx", text: line.slice(1) });
    }
  }
  return sections;
}

/** Return one hard-bounded page without flattening or copying the full diff.
 * Counting is linear in the parsed payload; only the selected page's line
 * references are copied for React to mount. */
export function pageGitDiff(sections: GitDiffSection[], requestedPage: number): GitDiffPage {
  let totalLines = 0;
  for (const section of sections) {
    for (const hunk of section.hunks) totalLines += hunk.lines.length;
  }
  const pageCount = Math.max(1, Math.ceil(totalLines / GIT_DIFF_PAGE_LINES));
  const finitePage = Number.isFinite(requestedPage) ? Math.floor(requestedPage) : 0;
  const page = Math.max(0, Math.min(pageCount - 1, finitePage));
  const startLine = page * GIT_DIFF_PAGE_LINES;
  const endLine = Math.min(totalLines, startLine + GIT_DIFF_PAGE_LINES);
  const windowed: GitDiffSection[] = [];
  let cursor = 0;

  for (const section of sections) {
    let pageSection: GitDiffSection | null = null;
    for (const hunk of section.hunks) {
      const hunkStart = cursor;
      const hunkEnd = cursor + hunk.lines.length;
      cursor = hunkEnd;
      if (hunkEnd <= startLine || hunkStart >= endLine) continue;
      const from = Math.max(0, startLine - hunkStart);
      const to = Math.min(hunk.lines.length, endLine - hunkStart);
      if (!pageSection) {
        pageSection = { file: section.file, hunks: [] };
        windowed.push(pageSection);
      }
      pageSection.hunks.push({ header: hunk.header, lines: hunk.lines.slice(from, to) });
    }
  }

  return { sections: windowed, page, pageCount, startLine, endLine, totalLines };
}

interface SegmentPreview { lines: DiffLine[]; omitted: number; }

function previewSegment(
  source: string[],
  type: DiffLine["type"],
  budget: number,
  omittedLabel: string,
): SegmentPreview {
  if (source.length <= budget) {
    return { lines: source.map((text) => ({ type, text })), omitted: 0 };
  }
  if (budget <= 1) {
    return {
      lines: [{ type: "ctx", text: `… ${source.length} 行${omittedLabel}已省略 …` }],
      omitted: source.length,
    };
  }
  const visible = budget - 1;
  const head = Math.ceil(visible / 2);
  const tail = visible - head;
  const omitted = source.length - visible;
  return {
    lines: [
      ...source.slice(0, head).map((text) => ({ type, text })),
      { type: "ctx", text: `… ${omitted} 行${omittedLabel}已省略 …` },
      ...source.slice(source.length - tail).map((text) => ({ type, text })),
    ],
    omitted,
  };
}

/** Low-memory fallback used only when the wrapper did not provide a diff.
 * It finds the common prefix/suffix in O(n + m), then samples the changed
 * middle under a hard output-row cap. It deliberately favors predictability
 * over a minimal edit script: the authoritative server diff is preferred. */
export function boundedDiffLines(oldStr: string, newStr: string): {
  lines: DiffLine[]; truncated: boolean; omittedLines: number;
} {
  const a = oldStr.split("\n");
  const b = newStr.split("\n");
  const n = a.length, m = b.length;
  let prefix = 0;
  while (prefix < n && prefix < m && a[prefix] === b[prefix]) prefix += 1;

  if (prefix === n && prefix === m) {
    const unchanged = previewSegment(a, "ctx", MAX_FALLBACK_DIFF_LINES, "未变内容");
    return {
      lines: unchanged.lines,
      truncated: unchanged.omitted > 0,
      omittedLines: unchanged.omitted,
    };
  }

  let suffix = 0;
  while (suffix < n - prefix && suffix < m - prefix
      && a[n - suffix - 1] === b[m - suffix - 1]) suffix += 1;

  const out: DiffLine[] = [];
  let omittedLines = 0;
  if (prefix > FALLBACK_CONTEXT_LINES) {
    const omitted = prefix - FALLBACK_CONTEXT_LINES;
    out.push({ type: "ctx", text: `… ${omitted} 行未变内容已省略 …` });
    omittedLines += omitted;
  }
  for (const text of a.slice(Math.max(0, prefix - FALLBACK_CONTEXT_LINES), prefix)) {
    out.push({ type: "ctx", text });
  }

  const suffixRows = Math.min(FALLBACK_CONTEXT_LINES, suffix) + (suffix > FALLBACK_CONTEXT_LINES ? 1 : 0);
  const changeBudget = Math.max(2, MAX_FALLBACK_DIFF_LINES - out.length - suffixRows);
  const deleted = a.slice(prefix, n - suffix);
  const added = b.slice(prefix, m - suffix);
  const deletionBudget = deleted.length && added.length ? Math.floor(changeBudget / 2) : (deleted.length ? changeBudget : 0);
  const additionBudget = added.length ? changeBudget - deletionBudget : 0;
  const deletionPreview = previewSegment(deleted, "del", deletionBudget, "删除内容");
  const additionPreview = previewSegment(added, "add", additionBudget, "新增内容");
  out.push(...deletionPreview.lines, ...additionPreview.lines);
  omittedLines += deletionPreview.omitted + additionPreview.omitted;

  for (const text of a.slice(n - suffix, n - suffix + FALLBACK_CONTEXT_LINES)) {
    out.push({ type: "ctx", text });
  }
  if (suffix > FALLBACK_CONTEXT_LINES) {
    const omitted = suffix - FALLBACK_CONTEXT_LINES;
    out.push({ type: "ctx", text: `… ${omitted} 行未变内容已省略 …` });
    omittedLines += omitted;
  }

  return {
    lines: out.slice(0, MAX_FALLBACK_DIFF_LINES),
    truncated: omittedLines > 0,
    omittedLines,
  };
}

export function diffLines(oldStr: string, newStr: string): DiffLine[] {
  return boundedDiffLines(oldStr, newStr).lines;
}

export function buildEditDiffPreview(
  oldStr: string,
  newStr: string,
  serverDiff?: string | null,
): EditDiffPreview {
  if (serverDiff) return { source: "server", diff: serverDiff };
  const fallback = boundedDiffLines(oldStr, newStr);
  return { source: "fallback", ...fallback };
}
