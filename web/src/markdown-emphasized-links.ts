import type { Nodes, Root } from "mdast";
import type { Plugin } from "unified";

/** GFM's bare-URL tokenizer consumes suffixes such as `**（说明）` before
 * emphasis can close. Only repair an exact, unescaped **URL** boundary in an
 * already parsed bare link; code and explicitly authored destinations stay
 * untouched. Let the normal parser own emphasis, escaping and the suffix. */
export const remarkEmphasizedLinks: Plugin<[], Root> = function () {
  return (tree, file) => {
    const source = String(file.value);
    if (!source.includes("**http")) return;
    const insertions: number[] = [];
    const pending: Nodes[] = [tree];
    while (pending.length) {
      const node = pending.pop()!;
      if (node.type === "link") {
        const start = node.position?.start.offset;
        const end = node.position?.end.offset;
        if (start == null || end == null || source.slice(start - 2, start) !== "**"
            || source[start - 3] === "*") continue;
        let slashes = 0;
        for (let index = start - 3; index >= 0 && source[index] === "\\"; index -= 1) slashes += 1;
        if (slashes % 2) continue;
        const raw = source.slice(start, end);
        const match = /^(https?:\/\/[^\s<>\\*`]+)\*\*(?=[，。！？；：、（）】》」』〕〉])/u.exec(raw);
        if (!match) continue;
        // Angle autolinks treat character references literally. Do not change
        // their meaning while repairing an unrelated emphasis boundary.
        if (/&(?:#\d+|#x[\da-f]+|[a-z][\da-z]+);/i.test(match[1])) continue;
        insertions.push(start, start + match[1].length);
      } else if ("children" in node) {
        pending.push(...node.children);
      }
    }
    if (!insertions.length) return;
    insertions.sort((a, b) => a - b);
    const parts: string[] = [];
    let cursor = 0;
    insertions.forEach((offset, index) => {
      parts.push(source.slice(cursor, offset), index % 2 ? ">" : "<");
      cursor = offset;
    });
    parts.push(source.slice(cursor));
    // This VFile belongs only to the render; native messages and copy text are
    // unchanged. Update source positions for the following display plugins.
    file.value = parts.join("");
    tree.children = (this.parse(file) as Root).children;
  };
};
