import type { ToolBlock } from "./reducer";

function value(input: Record<string, unknown>, ...keys: string[]): string {
  for (const key of keys) {
    const candidate = input[key];
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
  }
  return "";
}

function basename(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || path;
}

function commandPreview(command: string): string {
  const line = command.split("\n", 1)[0].trim();
  return line.length > 96 ? line.slice(0, 93) + "…" : line;
}

export interface ToolPresentation {
  icon: string;
  title: string;
  subtitle: string;
  group: string;
}

export function isToolFailure(block: ToolBlock): boolean {
  const status = block.result?.status;
  return block.result?.is_error === true || status === "failed" || status === "declined"
    || status === "cancelled" || status === "interrupted";
}

/** Convert engine-specific tool names into short, stable activity labels. */
export function presentTool(block: ToolBlock): ToolPresentation {
  const input = block.input;
  const file = value(input, "file_path", "path");
  const command = value(input, "command", "cmd");
  const pattern = value(input, "pattern", "query");
  const url = value(input, "url");
  const explicit = block.title?.trim();
  const tool = block.tool || "tool";
  const lower = tool.toLowerCase();

  if (explicit) {
    const semantic = {
      file: { icon: "edit", group: "文件" },
      command: { icon: "bash", group: "命令" },
      mcp: { icon: "term", group: "MCP" },
      agent: { icon: "spark", group: "协作代理" },
      server_tool: { icon: "term", group: "服务端工具" },
      web_search: { icon: "research", group: "网页搜索" },
      tool: { icon: "bash", group: tool },
    }[block.category ?? "tool"];
    return { ...semantic, title: explicit,
      subtitle: file || commandPreview(command) || pattern || url };
  }
  if (lower === "read" || lower === "readfile" || lower === "listfiles") {
    return { icon: "read", title: file ? `读取 ${basename(file)}` : "读取文件",
      subtitle: file, group: "读取文件" };
  }
  if (lower === "edit" || lower === "write" || lower === "apply_patch"
      || lower === "editfile") {
    const action = lower === "write" ? "写入" : "编辑";
    return { icon: "edit", title: file ? `${action} ${basename(file)}` : `${action}文件`,
      subtitle: file, group: "修改文件" };
  }
  if (lower === "grep" || lower === "glob" || lower === "search") {
    return { icon: "research", title: pattern ? `搜索 ${pattern}` : "搜索内容",
      subtitle: file, group: "搜索" };
  }
  if (block.category === "web_search" || lower.includes("websearch")
      || lower === "web_search") {
    return { icon: "research", title: pattern ? `搜索网页：${pattern}` : "搜索网页",
      subtitle: url, group: "网页搜索" };
  }
  if (block.category === "mcp" || lower.startsWith("mcp__") || block.server) {
    const server = block.server || tool.split("__")[1] || "MCP";
    const name = tool.split("__").slice(2).join("/") || tool;
    return { icon: "term", title: `${server} · ${name}`,
      subtitle: pattern || url, group: "MCP" };
  }
  if (block.category === "agent" || lower === "agent" || lower === "task") {
    return { icon: "spark", title: "协作代理", subtitle: pattern, group: "协作代理" };
  }
  if (lower === "enterplanmode") {
    return { icon: "plan", title: "进入计划模式", subtitle: "", group: "计划" };
  }
  if (lower === "exitplanmode") {
    return { icon: "plan", title: "完成计划", subtitle: "", group: "计划" };
  }
  if (block.category === "command" || lower === "bash" || lower === "shell"
      || lower === "commandexecution") {
    return { icon: "bash", title: command ? `运行 ${commandPreview(command)}` : "运行命令",
      subtitle: value(input, "cwd"), group: "命令" };
  }
  return { icon: "bash", title: tool, subtitle: file || commandPreview(command) || pattern,
    group: tool };
}
