import type { Turn } from "./domain/conversation";
import { parseLocalFileTarget } from "./file-link.ts";
import { mutatedFilePaths } from "./file-changes.ts";
import { isLocalViewerUrl } from "./remote-viewer.ts";

/** Hints from explicit assistant artifacts, never permission or URL sniffing. */
export function viewerPageCandidates(turn: Turn): string[] {
  const paths = new Set<string>();
  const add = (raw: string) => {
    if (paths.size >= 32) return;
    if (raw.length <= 2048 && raw.startsWith("http://") && isLocalViewerUrl(raw)) {
      paths.add(raw);
      return;
    }
    const target = parseLocalFileTarget(raw);
    if (target && target.path.length <= 4096 && /\.html?$/i.test(target.path)
        && paths.size < 32) paths.add(target.path);
  };
  for (const block of [...turn.blocks, ...(turn.liveSpillBlocks ?? []), ...(turn.detailProjection?.blocks ?? [])]) {
    if (block.kind === "text" && block.channel !== "thinking" && block.delivery !== "async") {
      if (!/\.htm|http:\/\//i.test(block.text)) continue;
      // Examples inside fenced source code are not deliverables.
      const text = block.text.replace(/^\s*(`{3,}|~{3,})[^\n]*\n[\s\S]*?^\s*\1\s*$/gm, "");
      for (const match of text.matchAll(/(?<!!)\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s)]+))(?:\s+"[^"\n]*")?\s*\)/g)) add(match[1] ?? match[2]);
      // A filename mentioned in prose has no reliable directory. Only explicit
      // links, qualified paths and successful file writes are discovery hints.
      for (const match of text.matchAll(/(?<!`)`([^`\n]+)`(?!`)/g)) {
        if (match[1].includes("/")) add(match[1]);
      }
      for (const match of text.matchAll(/http:\/\/[^\s<>"'`\])，。；！？]+/g)) add(match[0]);
      for (const match of text.matchAll(/visualize([^\n]+?)/g)) {
        try { const value = JSON.parse(match[1]); if (typeof value?.path === "string") add(value.path); } catch { /* ordinary text */ }
      }
    } else if (block.kind === "tool" && block.done && block.result && !block.result.is_error
        && !["failed", "declined", "cancelled", "interrupted"].includes(block.result.status ?? "")) {
      mutatedFilePaths(block.tool, block.input).forEach(add);
    } else if (block.kind === "process" && block.processKind === "file_change" && block.status === "succeeded") {
      mutatedFilePaths("filechange", block.input ?? {}).forEach(add);
    }
  }
  return [...paths];
}
