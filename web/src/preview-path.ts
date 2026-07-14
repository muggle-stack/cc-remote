export type PreviewTarget =
  | { kind: "local"; value: string }
  | { kind: "external"; value: string }
  | { kind: "anchor"; value: string }
  | { kind: "blocked"; value: "" };

const SCHEME = /^[A-Za-z][A-Za-z0-9+.-]*:/;

/** Resolve a Markdown link/image without letting browser URL rules escape cwd. */
export function classifyPreviewTarget(markdownPath: string, rawTarget: string): PreviewTarget {
  const target = rawTarget.trim();
  if (!target) return { kind: "blocked", value: "" };
  if (target.startsWith("#")) return { kind: "anchor", value: target };
  if (/^https?:/i.test(target)) return { kind: "external", value: target };
  if (target.startsWith("//") || SCHEME.test(target) || target.startsWith("/")) {
    return { kind: "blocked", value: "" };
  }

  const pathPart = target.split(/[?#]/, 1)[0];
  let decoded: string;
  try {
    decoded = decodeURIComponent(pathPart);
  } catch {
    return { kind: "blocked", value: "" };
  }
  if (!decoded || decoded.includes("\0")) return { kind: "blocked", value: "" };

  const base = markdownPath.split("/").slice(0, -1);
  const resolved: string[] = [];
  for (const part of [...base, ...decoded.replace(/\\/g, "/").split("/")]) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (!resolved.length) return { kind: "blocked", value: "" };
      resolved.pop();
      continue;
    }
    resolved.push(part);
  }
  return resolved.length
    ? { kind: "local", value: resolved.join("/") }
    : { kind: "blocked", value: "" };
}

export function isMarkdownPath(path: string): boolean {
  return /\.(?:md|markdown)$/i.test(path.split(/[?#]/, 1)[0]);
}
