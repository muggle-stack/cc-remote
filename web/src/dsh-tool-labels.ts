import type { DshPanelSelection } from "./components/DshPanel";

type Kind = DshPanelSelection["kind"];
export const dshToolLabels: Record<Kind, string> = {
  subagents: "子代理", jobs: "后台任务", workflows: "工作流", trajectory: "执行轨迹", stats: "性能统计",
  deliverables: "产出文件", schedules: "自动化", settings: "运行设置", presets: "预设",
  bundles: "插件", plugins: "插件明细", terminals: "终端", diagnostics: "连接诊断", conversation: "会话记录",
};
