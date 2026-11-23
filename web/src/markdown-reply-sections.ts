import type { Blockquote, Root } from "mdast";
import type { Plugin } from "unified";

interface ReplyTag {
  name: string;
  start: number;
  end: number;
}

/** Only a whole reply can opt into this presentation. Markdown examples,
 * escaped tags, code and quotations must remain ordinary message content. */
function replyTags(source: string): ReplyTag[] {
  const tags: ReplyTag[] = [];
  let fence: { marker: string; length: number } | null = null;
  let ticks = 0;
  let offset = 0;
  for (const line of source.split("\n")) {
    const marker = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
    if (fence) {
      if (marker && marker[1][0] === fence.marker
          && marker[1].length >= fence.length && !marker[2].trim()) fence = null;
    } else if (!ticks && marker) {
      fence = { marker: marker[1][0], length: marker[1].length };
    } else if (!/^(?: {4}|\t| {0,3}>)/.test(line)) {
      for (const token of line.matchAll(/\\.|`+|<\/?(?:analysis|summary)>/gi)) {
        const value = token[0];
        if (value.startsWith("\\")) continue;
        if (value[0] === "`") {
          if (!ticks) ticks = value.length;
          else if (ticks === value.length) ticks = 0;
        } else if (!ticks) {
          tags.push({ name: value.toLowerCase(), start: offset + token.index,
            end: offset + token.index + value.length });
        }
      }
    }
    offset += line.length + 1;
  }
  return tags;
}

export function splitReplySections(source: string): { analysis: string; summary: string } | null {
  // Four leading spaces are a code block, not a reply wrapper.
  if (!/^(?:[ \t]*\r?\n)* {0,3}<analysis>/i.test(source)) return null;
  const tags = replyTags(source);
  if (tags.length !== 3 && tags.length !== 4) return null;
  const [open, close, summary, end] = tags;
  if (open.name !== "<analysis>" || close.name !== "</analysis>"
      || summary.name !== "<summary>" || (end && end.name !== "</summary>")
      || source.slice(0, open.start).trim()
      || source.slice(close.end, summary.start).trim()
      || (end && source.slice(end.end).trim())) return null;
  const analysisText = source.slice(open.end, close.start).trim();
  // Some settled native replies omit only the final </summary>. Keep all of
  // the remaining text. Missing analysis boundaries are never guessed.
  const summaryText = source.slice(summary.end, end?.start ?? source.length).trim();
  return analysisText && summaryText ? { analysis: analysisText, summary: summaryText } : null;
}

/** Display-only, enabled for settled Claude answer blocks. The Markdown
 * parser still owns links, code, math and escaping; no raw HTML is enabled.
 * Both sections share one root so reference definitions work across them. */
export const remarkReplySections: Plugin<[], Root> = function () {
  return (tree, file) => {
    const reply = splitReplySections(String(file.value));
    if (!reply) return;
    // Parse together so either section can resolve the other's reference
    // definitions. A temporary block boundary keeps adjacent lists separate.
    const boundaryOffset = reply.analysis.length + 2;
    const source = `${reply.analysis}\n\n***\n\n${reply.summary}`;
    const parsed = this.parse(source) as Root;
    const boundary = parsed.children.findIndex((node) => node.type === "thematicBreak"
      && node.position?.start.offset === boundaryOffset);
    // An unfinished Markdown construct consumed the boundary: preserve the
    // original reply rather than guessing where its blocks should be split.
    if (boundary < 0) return;
    // The following display transforms need the source that owns these offsets.
    file.value = source;
    tree.children = [{
      type: "blockquote",
      data: { hName: "section", hProperties: { className: ["message-reply-analysis"], ariaLabel: "分析" } },
      children: [{
        type: "paragraph",
        data: { hName: "div", hProperties: { className: ["message-reply-analysis-label"] } },
        children: [{ type: "text", value: "分析" }],
      }, {
        type: "blockquote",
        data: { hName: "div", hProperties: { className: ["message-reply-analysis-body"] } },
        // The parser emits block nodes at the root (Root's type also allows
        // inline nodes for plugins, which have not run on this parsed tree).
        children: parsed.children.slice(0, boundary) as Blockquote["children"],
      }],
    }, ...parsed.children.slice(boundary + 1)];
  };
};
