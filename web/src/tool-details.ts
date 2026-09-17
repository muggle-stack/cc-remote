import { displayCommand } from "./tool-command.ts";
export { displayCommand } from "./tool-command.ts";
import { filePathsFromInput } from "./file-changes.ts";

type Input = Record<string, unknown>;

export interface ToolInputField { label: string; text: string }

export function readableToolInput(input: Input, omit: string[] = []): ToolInputField[] {
  const fields: ToolInputField[] = [];
  const add = (label: string, keys: string[], transform?: (value: unknown) => string) => {
    for (const key of keys) {
      if (omit.includes(key)) continue;
      const raw = input[key];
      const text = transform ? transform(raw) : typeof raw === "string" ? raw : "";
      if (text.trim()) { fields.push({ label, text }); break; }
    }
  };
  add("命令", ["command", "cmd"], displayCommand);
  add("工作目录", ["cwd", "workdir"]);
  add("文件", ["file_path", "path", "file_paths"], (raw) =>
    typeof raw === "string" ? raw : Array.isArray(raw)
      && raw.every((item) => typeof item === "string") ? raw.join("\n") : "");
  if (!fields.some((field) => field.label === "文件") && !omit.includes("changes")) {
    const paths = filePathsFromInput(input);
    if (paths.length) fields.push({ label: "文件", text: paths.join("\n") });
  }
  add("搜索内容", ["pattern", "query", "search_term"]);
  add("搜索范围", ["glob", "include"]);
  add("地址", ["url"]);
  add("说明", ["description"]);
  return fields;
}

const record = (value: unknown): value is Input =>
  !!value && typeof value === "object" && !Array.isArray(value);

/** Unwrap known transport envelopes only. Arbitrary program JSON stays data. */
function unwrap(value: unknown, depth = 0): string | null {
  if (depth > 6) return null;
  if (Array.isArray(value)) {
    if (!value.length || value.length > 64) return null;
    const texts = value.map((item) => unwrap(item, depth + 1));
    return texts.every((text) => text !== null) ? texts.join("\n\n") : null;
  }
  if (!record(value)) return null;
  if (value.status === "fulfilled" && "value" in value) {
    return unwrap(value.value, depth + 1);
  }
  if (value.status === "rejected" && "reason" in value) {
    return `工具执行失败\n${typeof value.reason === "string"
      ? value.reason : JSON.stringify(value.reason, null, 2)}`;
  }
  if (value.type === "text" && typeof value.text === "string") {
    return unwrapString(value.text, depth + 1);
  }
  if (Array.isArray(value.content) && value.content.length > 0
      && value.content.every((item) => record(item) && item.type === "text")) {
    const text = unwrap(value.content, depth + 1);
    return text === null ? null : value.isError === true ? `工具执行失败\n${text}` : text;
  }
  if (typeof value.output === "string" && (
    typeof value.chunk_id === "string" || typeof value.wall_time_seconds === "number"
    || typeof value.exit_code === "number" || typeof value.session_id === "number"
  )) {
    const text = unwrapString(value.output, depth + 1);
    const failed = typeof value.exit_code === "number" && value.exit_code !== 0;
    return failed ? `${text}\n退出码：${value.exit_code}`.trim()
      : text || (value.session_id != null ? "命令仍在运行，等待输出。" : "命令执行完成，没有文本输出。");
  }
  return null;
}

function unwrapString(text: string, depth: number): string {
  if (depth > 6) return text;
  try { return unwrap(JSON.parse(text), depth + 1) ?? text; }
  catch { return text; }
}

export function readableToolOutput(output: string): { text: string; unwrapped: boolean } {
  // Large native outputs already have transport bounds; do not repeatedly
  // allocate/reparse a multi-megabyte result while streaming.
  if (output.length > 512 * 1024) return { text: output, unwrapped: false };
  try {
    const parsed: unknown = JSON.parse(output);
    const text = unwrap(parsed);
    return text === null
      ? { text: JSON.stringify(parsed, null, 2), unwrapped: false }
      : { text, unwrapped: true };
  } catch {
    const lines = output.trim().split("\n");
    if (lines.length > 1 && lines.length <= 64) {
      try {
        const text = unwrap(lines.map((line) => JSON.parse(line)));
        if (text !== null) return { text, unwrapped: true };
      } catch { /* Ordinary terminal output stays byte-for-byte readable. */ }
    }
    return { text: output, unwrapped: false };
  }
}
