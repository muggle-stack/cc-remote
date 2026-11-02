import type { Nodes, Root } from "mdast";
import type { Plugin } from "unified";

/** GFM treats Chinese sentence punctuation as URL characters. Repair only
 * prose autolinks, never authored Markdown/angle destinations or code. */
export const remarkLinkPunctuation: Plugin<[], Root> = function () {
  return (tree, file) => {
    const source = String(file.value);
    const boundaries: { start: number; end: number }[] = [];
    const pending: Nodes[] = [tree];
    while (pending.length) {
      const node = pending.pop()!;
      if (node.type === "link") {
        const start = node.position?.start.offset, end = node.position?.end.offset;
        if (start == null || end == null) continue;
        const raw = source.slice(start, end);
        if (!/^https?:\/\//i.test(raw)) continue;
        const boundary = raw.search(/[，。！？；：、）】》」』〕〉]/u);
        if (boundary < 0) continue;
        const url = raw.slice(0, boundary);
        // Angle autolinks don't decode entities; don't change existing query
        // semantics or reinterpret explicitly escaped punctuation.
        if (/\\|&(?:#\d+|#x[\da-f]+|[a-z][\da-z]+);/i.test(url)) continue;
        boundaries.push({ start, end: start + boundary });
      } else if ("children" in node) pending.push(...node.children);
    }
    if (!boundaries.length) return;
    boundaries.sort((a, b) => a.start - b.start);
    const parts: string[] = [];
    let cursor = 0;
    for (const { start, end } of boundaries) {
      parts.push(source.slice(cursor, start), "<", source.slice(start, end), ">");
      cursor = end;
    }
    parts.push(source.slice(cursor));
    file.value = parts.join("");
    tree.children = (this.parse(file) as Root).children;
  };
};
