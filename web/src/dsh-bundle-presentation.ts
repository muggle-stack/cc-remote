import type { DshItem } from "./protocol";

const bundled: Record<string, [string, string, string]> = {
  "dsh-base": ["DSH 核心组件", "基础能力与管理功能，随 DSH 更新。", "archive"],
  "dsh-official-bundles": ["DSH 官方插件", "随 DSH 提供的官方插件组件。", "archive"],
  "dsh-web-app": ["DSH 网页界面", "提供原生网页界面与运行连接。", "globe"],
  "dsh-session-titles": ["会话标题", "自动生成便于识别的会话标题。", "message"],
  "dsh-plugin-market": ["插件市场", "发现插件，查看版本与兼容性。", "archive"],
  "dsh-terminal": ["终端", "在会话中直接使用命令行。", "bash"],
  "dsh-session-search": ["会话搜索", "检索已保留的会话内容。", "search"],
};
export function bundlePresentation(item: DshItem) {
  const entry = item.id.startsWith("@deepseek-ai/") ? bundled[item.id.slice(13)] : undefined;
  return { title: entry?.[0] ?? item.title, detail: entry?.[1] ?? item.detail, icon: entry?.[2] ?? "archive" };
}
