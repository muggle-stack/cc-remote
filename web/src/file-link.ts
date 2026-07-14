export interface LocalFileTarget {
  path: string;
  line?: number;
  column?: number;
}

const SCHEME = /^[A-Za-z][A-Za-z0-9+.-]*:/;
const WINDOWS_PATH = /^[A-Za-z]:[\\/]/;
const MAX_SOURCE_POSITION = 10_000_000;

function sourcePosition(value: string | undefined): number | undefined {
  if (!value) return undefined;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0
    && parsed <= MAX_SOURCE_POSITION ? parsed : undefined;
}

/** Parse links emitted by Claude/Codex such as `/cwd/file.py:731` or `#L731`.
 * Network URLs and in-document anchors deliberately remain normal links. */
export function parseLocalFileTarget(rawHref: string): LocalFileTarget | null {
  let value = rawHref.trim();
  if (!value || value.startsWith("#") || value.startsWith("//")) return null;

  if (/^file:\/\//i.test(value)) {
    try {
      const url = new URL(value);
      if (url.hostname && url.hostname !== "localhost") return null;
      value = `${url.pathname}${url.hash}`;
    } catch {
      return null;
    }
  } else if (SCHEME.test(value) && !WINDOWS_PATH.test(value)) {
    return null;
  }

  let line: number | undefined;
  let column: number | undefined;
  const hashAt = value.indexOf("#");
  if (hashAt >= 0) {
    const fragment = value.slice(hashAt + 1);
    value = value.slice(0, hashAt);
    const match = /^(?:L|line-)?(\d+)(?:[-:]?C?(\d+))?/i.exec(fragment);
    line = sourcePosition(match?.[1]);
    column = sourcePosition(match?.[2]);
  }

  value = value.split("?", 1)[0];
  if (!line) {
    const suffix = /:(\d+)(?::(\d+))?$/.exec(value);
    const parsedLine = sourcePosition(suffix?.[1]);
    if (suffix && parsedLine) {
      value = value.slice(0, suffix.index);
      line = parsedLine;
      column = sourcePosition(suffix[2]);
    }
  }

  try {
    value = decodeURIComponent(value);
  } catch {
    return null;
  }
  if (!value || value.includes("\0")) return null;
  return { path: value, line, column };
}
