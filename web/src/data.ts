// Slash commands, models, permission modes. Slash commands split by surface:
// Work exposes only generic conversation/workspace controls; Code additionally
// exposes engine-specific coding commands. Commands then split into
// client-side ones (CLIENT_SLASHES: model/plan/normal/permissions/clear/context,
// handled in Composer.send) and cc skills (forwarded verbatim to cc). Model/perm
// chips drive set_model / set_permission_mode on the wrapper.

import type {
  AntigravityOption, CatalogModel, EngineCapabilityItem, NativeCommandInfo, PermissionProfileInfo,
} from "./protocol";

export interface CmdGroup { g: string }
export interface Cmd { slash: string; name: string; ds: string; ic: string; unavailable?: boolean }
const OPEN_FILES_COMMAND: Cmd = { slash: "open", name: "打开目录或文件", ds: "/open [路径] 浏览会话文件，点击文件在侧栏预览", ic: "folder-open" };
export type Command = CmdGroup | Cmd;
const COMPACT_COMMAND: Cmd = {
  slash: "compact", name: "压缩上下文", ds: "调用原生压缩", ic: "simplify",
};

export const COMMANDS: Command[] = [
  { g: "模式" },
  { slash: "plan", name: "Plan mode", ds: "先给方案，确认后再动手", ic: "plan" },
  { slash: "normal", name: "普通模式", ds: "直接执行，边做边说", ic: "run" },
  { slash: "permissions", name: "权限模式", ds: "选择 cc 的权限模式", ic: "shield" },
  { g: "模型" },
  { slash: "model", name: "切换模型", ds: "/model <id> 切到指定模型(支持隐藏模型),无参数则打开选择器", ic: "cpu" },
  { g: "扩展" },
  { slash: "extensions", name: "扩展管理", ds: "Skills、Plugins、Apps、MCP 与 Hooks", ic: "spark" },
  { slash: "skills", name: "Skills", ds: "查看和管理当前引擎 Skills", ic: "read" },
  { slash: "plugins", name: "Plugins", ds: "查看和管理当前引擎 Plugins", ic: "spark" },
  { slash: "apps", name: "Apps", ds: "查看当前引擎 Apps", ic: "run" },
  { slash: "mcp", name: "MCP", ds: "查看当前引擎 MCP Servers", ic: "cpu" },
  { slash: "hooks", name: "Hooks", ds: "查看当前引擎 Hooks（与 Claude 原生 /hook 区分）", ic: "shield" },
  { g: "审查" },
  { slash: "code-review", name: "代码审查", ds: "审当前 diff 的正确性与可简化项", ic: "review" },
  { slash: "security-review", name: "安全审查", ds: "扫描分支改动的安全隐患", ic: "shield" },
  { slash: "verify", name: "验证改动", ds: "真跑一遍确认行为符合预期", ic: "verify" },
  { slash: "simplify", name: "精简", ds: "复用、简化、去重", ic: "simplify" },
  { g: "技能" },
  { slash: "run", name: "运行 App", ds: "启动并驱动本项目查看效果", ic: "run" },
  { slash: "deep-research", name: "深度调研", ds: "多源检索 + 交叉验证 + 成文", ic: "research" },
  { slash: "init", name: "初始化 CLAUDE.md", ds: "生成代码库说明", ic: "init" },
  { g: "会话" },
  { slash: "rewind", name: "回退", ds: "选择一条历史消息，恢复对话或文件", ic: "history" },
  { slash: "goal", name: "目标", ds: "/goal 查看 · /goal <目标> 设置 · /goal resume 恢复 Codex · /goal clear 清除", ic: "verify" },
  { slash: "btw", name: "侧边对话 (btw)", ds: "基于当前会话开一个临时 fork 侧聊,不影响主线", ic: "spark" },
  { slash: "diff", name: "查看改动", ds: "打开当前会话的 Git diff 侧栏", ic: "edit" },
  { slash: "preview", name: "预览文件", ds: "/preview <路径> 打开 Markdown 或 UTF-8 源文件", ic: "read" },
  OPEN_FILES_COMMAND,
  { slash: "clear", name: "清空会话", ds: "开新会话，清空上下文", ic: "close" },
  { slash: "context", name: "上下文用量", ds: "查看 token 占用", ic: "cpu" },
  { slash: "autocompact", name: "自动压缩", ds: "/autocompact [数字]；不填打开设置", ic: "simplify" },
  COMPACT_COMMAND,
];

// Work is a separate product surface, not a differently styled Code session.
// Keep its base palette limited to controls that are meaningful for a private
// work conversation. Narrow engine-native settings such as Codex Fast may be
// added below without exposing Code lifecycle commands. Engine/user-provided
// slash skills may still be typed manually; known Code commands are rejected
// by the composer instead of leaking through.
export const WORK_COMMANDS: Command[] = [
  { g: "设置" },
  { slash: "permissions", name: "权限配置", ds: "选择本次工作的权限模式与执行环境", ic: "shield" },
  { slash: "model", name: "切换模型", ds: "选择本次工作的模型与思考强度", ic: "cpu" },
  { g: "扩展" },
  { slash: "extensions", name: "扩展目录", ds: "只读查看当前 Work 可用扩展", ic: "spark" },
  { slash: "skills", name: "Skills", ds: "只读查看当前 Work Skills", ic: "read" },
  { slash: "plugins", name: "Plugins", ds: "只读查看当前 Work Plugins", ic: "spark" },
  { slash: "apps", name: "Apps", ds: "只读查看当前 Work Apps", ic: "run" },
  { slash: "mcp", name: "MCP", ds: "只读查看当前 Work MCP Servers", ic: "cpu" },
  { slash: "hooks", name: "Hooks", ds: "只读查看当前 Work Hooks", ic: "shield" },
  { g: "工作" },
  { slash: "goal", name: "工作目标", ds: "/goal 查看 · /goal <目标> 设置 · /goal resume 恢复 Codex · /goal clear 清除", ic: "verify" },
  { slash: "btw", name: "侧边对话 (btw)", ds: "临时侧聊，不影响当前工作主线", ic: "spark" },
  { slash: "preview", name: "预览 Artifacts", ds: "/preview <路径> 打开 Markdown 或 UTF-8 源文件", ic: "read" },
  { slash: "context", name: "上下文用量", ds: "查看本次工作的 token 占用", ic: "cpu" },
  { slash: "autocompact", name: "自动压缩", ds: "/autocompact [数字]；不填打开设置", ic: "simplify" },
  { slash: "clear", name: "新工作", ds: "开始一项独立的新工作", ic: "close" },
];

const CODEX_WORK_COMMANDS: Command[] = [
  ...WORK_COMMANDS.slice(0, 2),
  { slash: "fast", name: "Fast 模式", ds: "更快响应，下条消息生效", ic: "bolt" },
  ...WORK_COMMANDS.slice(2),
];

// `efforts` overrides the engine's baseline effort list for THIS model — reasoning
// levels are per-model, not per-engine.
export interface Model { id: string; name: string; ds: string; ic: string; efforts?: Effort[] }
// Offline/first-paint suggestions only. Claude's native initialization catalog
// wins once received, including models this client has never heard of.
export const MODELS: Model[] = [
  { id: "opus[1m]", name: "Opus", ds: "跟随本机 Claude Code", ic: "crown" },
  { id: "sonnet", name: "Sonnet", ds: "跟随本机 Claude Code", ic: "balance" },
  { id: "haiku", name: "Haiku", ds: "跟随本机 Claude Code", ic: "bolt" },
];

// Reasoning effort (思考强度). `name` is the RAW level id on purpose: it's what
// `~/.codex/config.toml` (model_reasoning_effort) and cc's `--effort` take, so what
// the chip shows is exactly what you can grep for in the config. No translated labels.
export interface Effort { id: string; name: string; ds: string; ic: string }

// Legacy presentation for Claude/DSH. Codex uses native descriptions and order.
const EFFORT_META: Record<string, { ds: string; ic: string }> = {
  minimal: { ds: "几乎不推理 · 最快最省", ic: "gauge1" },
  low: { ds: "更快 · 轻推理", ic: "gauge1" },
  medium: { ds: "均衡 · 日常任务", ic: "gauge2" },
  high: { ds: "深度推理 · 复杂问题", ic: "gauge3" },
  xhigh: { ds: "更深推理 · 更慢", ic: "gauge4" },
  max: { ds: "最深推理 · 最难的问题", ic: "gauge5" },
  ultra: { ds: "极限推理 · 最慢最贵", ic: "crown" },
};
const effort = (id: string): Effort => ({ id, name: id, ds: EFFORT_META[id]?.ds ?? "", ic: EFFORT_META[id]?.ic ?? "gauge3" });
const efforts = (...ids: string[]): Effort[] => ids.map(effort);

export const EFFORTS: Effort[] = efforts("low", "medium", "high", "xhigh", "max");        // cc

export const CODEX_PERMS: Perm[] = [
  { id: "never", name: "Never", short: "Never", ds: "不询问 · 需要审批时拒绝", ic: "shield" },
  { id: "on-request", name: "On Request", short: "On Request", ds: "需要时才询问", ic: "shield" },
  { id: "untrusted", name: "Untrusted", short: "Untrusted", ds: "每步都先询问", ic: "shield" },
];

export interface PermissionProfileChoice extends PermissionProfileInfo {
  name: string;
  short: string;
  ds: string;
  ic: string;
  danger?: boolean;
}

const CODEX_PROFILE_LOOKS: Record<string, {
  name: string; short: string; ds: string; ic: string; danger?: boolean;
}> = {
  "cc_remote_work": {
    name: "Work 私有工作区", short: "Work 私有工作区",
    ds: "默认 · 仅工作目录可写，网络受限", ic: "lock",
  },
  ":read-only": {
    name: "Read Only", short: "Read Only",
    ds: "只读文件；适合审查和分析", ic: "lock",
  },
  ":workspace": {
    name: "Workspace", short: "Workspace",
    ds: "可修改当前工作区；工作区外仍受保护", ic: "folder",
  },
  ":danger-full-access": {
    name: "Full Access", short: "Full Access",
    ds: "危险 · 不使用文件系统或网络沙箱", ic: "bolt", danger: true,
  },
};

const boundedPermissionProfileLabel = (
  value: string,
  limit: number,
): string => {
  const characters = Array.from(value);
  return characters.length <= limit
    ? value
    : `${characters.slice(0, Math.max(1, limit - 1)).join("")}…`;
};

export const CODEX_PERMISSION_PROFILES: PermissionProfileInfo[] = [
  { id: ":read-only", allowed: false },
  { id: ":workspace", allowed: false },
  { id: ":danger-full-access", allowed: false },
];

export const permissionProfilesFor = (
  live?: PermissionProfileInfo[] | null,
): PermissionProfileChoice[] => {
  // `undefined`/null means discovery has not completed. Render the familiar
  // built-ins read-only while loading; an authoritative [] must stay empty.
  const profiles = live == null ? CODEX_PERMISSION_PROFILES : live;
  return profiles.map((profile) => {
    const look = CODEX_PROFILE_LOOKS[profile.id];
    const fallbackName = profile.id.startsWith(":")
      ? profile.id.slice(1).replaceAll("-", " ")
      : profile.id;
    return {
      ...profile,
      name: look?.name ?? boundedPermissionProfileLabel(fallbackName, 48),
      short: look?.short ?? boundedPermissionProfileLabel(fallbackName, 24),
      ds: profile.description || look?.ds || "自定义 Codex 执行环境",
      ic: look?.ic ?? "shield",
      danger: look?.danger,
    };
  });
};

export const permissionProfileLabel = (
  profile: string | null | undefined,
  live?: PermissionProfileInfo[] | null,
): string | null => {
  if (!profile) return null;
  return permissionProfilesFor(live).find((item) => item.id === profile)?.short
    ?? boundedPermissionProfileLabel(profile.startsWith(":")
      ? profile.slice(1).replaceAll("-", " ")
      : profile, 24);
};

/** Live catalogs by engine, as reported by the wrapper (`models` frame). */
export type Catalog = Record<string, CatalogModel[]>;

const fromCatalog = (entries: CatalogModel[], engine?: string): Model[] =>
  entries.map((e) => ({
    id: e.id,
    name: e.display_name || e.id,
    ds: e.description ?? "",
    ic: "cpu",
    // Empty native capabilities stay empty, including before discovery.
    efforts: e.efforts.map((id) => engine === "codex" || engine === "antigravity"
      ? { id, name: e.effort_names?.[id] ?? id, ds: e.effort_descriptions?.[id] ?? "", ic: "gauge3" }
      : effort(id)),
  }));

export const modelsFor = (engine?: string, catalog?: Catalog): Model[] => {
  if (engine === "codex" || engine === "dsh" || engine === "antigravity") return fromCatalog(catalog?.[engine] ?? [], engine);
  const live = catalog?.claude;
  return live?.length ? fromCatalog(live) : MODELS;
};

/** Model a NEW chat starts on: the engine's configured default (codex config.toml's
 *  `model`), else the first catalog entry. An EXISTING session's model is per-session
 *  and comes from the wrapper, never from here. */
export const defaultModelFor = (engine?: string, catalog?: Catalog,
                                defaults?: Record<string, string>): string => {
  const list = modelsFor(engine, catalog);
  const want = engine ? defaults?.[engine] : undefined;
  // Codex defaults are accepted only when app-server's live catalog contains
  // them. Claude may legitimately use a custom/provider alias absent from the
  // common suggestions above, so preserve its wrapper-resolved id.
  return want && (engine !== "codex" || list.some((m) => m.id === want))
    ? want : (list[0]?.id ?? "");
};

/** Effort levels the selected model accepts. Codex and DSH require native
 *  capabilities before offering overrides. */
export const effortsFor = (engine?: string, model?: string | null, catalog?: Catalog): Effort[] => {
  const m = model ? modelsFor(engine, catalog).find((x) => x.id === model
    || (engine !== "codex" && x.id.replace(/\[1m\]$/i, "") === model.replace(/\[1m\]$/i, ""))) : undefined;
  if (m?.efforts) return m.efforts;
  return engine === "dsh" || engine === "codex" || engine === "antigravity" ? [] : EFFORTS;
};

/** A null app-server thread effort is a real model-default state, not loading.
 * Ordinary effort labels intentionally remain their raw CLI/config ids. */
export function effortNameForDisplay(
  id: string | null | undefined,
): string | null {
  const normalized = id?.trim();
  return normalized
    ? (normalized === "model-default" ? "模型默认" : normalized)
    : null;
}

export function effortIsSelectable(
  id: string | null | undefined,
): id is string {
  const normalized = id?.trim();
  return !!normalized && normalized !== "model-default";
}

export const permsFor = (engine?: string): Perm[] => (engine === "dsh" || engine === "antigravity" ? [] : engine === "codex" ? CODEX_PERMS : PERMS);

/** Project the advertised ACP modes into the shared permission picker. */
export function antigravityPermissions(options?: AntigravityOption[]): Perm[] {
  return (options?.find(option => option.category === "mode")?.choices ?? []).map(choice => ({
    id: choice.value, name: choice.name, short: choice.name, ds: choice.description,
    ic: choice.value === "plan" ? "plan" : "shield", danger: false,
  }));
}

export function antigravityEfforts(options?: AntigravityOption[]): Effort[] {
  return (options?.find(option => option.category === "thought_level")?.choices ?? []).map(choice => ({
    id: choice.value, name: choice.name, ds: choice.description, ic: "gauge3",
  }));
}

/** Localized display only; selections retain the exact native permission ID. */
export function dshPermissionLabel(value: string, fallback = value): string {
  return ({ "read-only": "只读", "workspace-write": "工作区写入",
    "danger-full-access": "完全访问" } as Record<string, string>)[value] ?? fallback;
}

// Map a cc-reported model id (e.g. "claude-mythos-5-1[1m]") to a MODELS entry id.
// An id we don't know (any codex model) passes through verbatim — the codex chips
// resolve it against the live catalog themselves.
export function matchModelId(m: string, engine?: string): string {
  const base = m.replace(/\[1m\]$/i, "");
  // Existing projections used these spellings before native discovery. Keep
  // their identities stable; new model IDs pass through without a version map.
  if (engine !== "codex") {
    if (base === "claude-opus-5") return `${base}[1m]`;
    if (base === "claude-mythos-5-1" || base === "claude-fable-5-1") return base;
  }
  return modelsFor(engine).find((x) => x.id.replace(/\[1m\]$/i, "") === base)?.id ?? m;
}

export interface Perm { id: string; name: string; short: string; ds: string; ic: string; danger?: boolean }
export const PERMS: Perm[] = [
  { id: "default", name: "Default", short: "Default", ds: "每次动作前询问", ic: "shield" },
  { id: "acceptEdits", name: "Accept Edits", short: "Accept Edits", ds: "文件编辑免询问，命令仍询问", ic: "edit" },
  { id: "plan", name: "Plan", short: "Plan", ds: "只读 · 先出方案再执行", ic: "plan" },
  { id: "auto", name: "Auto", short: "Auto", ds: "自动执行常规操作", ic: "run" },
  { id: "bypassPermissions", name: "Bypass Permissions", short: "Bypass Permissions", ds: "危险 · 不询问直接执行 · --dangerously-skip-permissions", ic: "bolt", danger: true },
];

export function isCmd(c: Command): c is Cmd {
  return (c as Cmd).slash !== undefined;
}

const CMD_LIST: Cmd[] = COMMANDS.filter(isCmd) as Cmd[];

// Slashes handled locally by the web client (never forwarded to cc as a prompt).
// Everything else (code-review, verify, run, deep-research, …) is a cc skill and
// is forwarded verbatim so cc's own slash-command layer runs it.
// /rewind opens the local checkpoint picker, never a model prompt.
const EXTENSION_SLASHES = ["extensions", "skills", "plugins", "apps", "mcp", "hooks"];
export const CLIENT_SLASHES = new Set(["model", "plan", "normal", "permissions", "clear", "context", "autocompact", "compact", "goal", "rewind", "btw", "diff", "preview", "open", ...EXTENSION_SLASHES]);

// Codex engine command palette. Native app-server controls are handled locally
// and never expanded into natural-language lookalikes. /context is the focused
// thread's token window; /status is a separate app-server snapshot (thread,
// config, account and rate limits). /review maps to review/start, /compact maps
// to thread/compact/start. /init remains
// an explicit compatibility prompt because app-server has no init RPC. /fast maps to app-server's per-thread service tier,
// and /hook is Claude-only. /plan and /normal are handled locally by the web
// client and mapped to app-server collaborationMode, not sent as prompt text.
export const CODEX_COMMANDS: Command[] = [
  { g: "审查" },
  { slash: "review", name: "代码审查", ds: "/review [base 分支 | commit SHA | custom 要求]", ic: "review" },
  { g: "项目" },
  { slash: "init", name: "初始化 AGENTS.md", ds: "生成/更新代码库说明", ic: "init" },
  { g: "模式" },
  { slash: "plan", name: "Plan mode", ds: "先调研并给出方案 · 下条消息生效", ic: "plan" },
  { slash: "normal", name: "默认模式", ds: "退出 Plan，恢复正常执行 · 下条消息生效", ic: "run" },
  { slash: "model", name: "切换模型", ds: "选择模型与思考强度", ic: "cpu" },
  { slash: "permissions", name: "权限模式", ds: "选择 Codex 的审批策略(自动/按需/严格)", ic: "shield" },
  { slash: "fast", name: "Fast 模式", ds: "开/关 Fast 服务档位(更快响应),下条消息生效", ic: "bolt" },
  { g: "扩展" },
  { slash: "extensions", name: "扩展管理", ds: "Skills、Plugins、Apps、MCP 与 Hooks", ic: "spark" },
  { slash: "skills", name: "Skills", ds: "查看和管理 Codex Skills", ic: "read" },
  { slash: "plugins", name: "Plugins", ds: "查看和管理 Codex Plugins", ic: "spark" },
  { slash: "apps", name: "Apps", ds: "查看 Codex Apps", ic: "run" },
  { slash: "mcp", name: "MCP", ds: "查看 Codex MCP Servers", ic: "cpu" },
  { slash: "hooks", name: "Hooks", ds: "查看 Codex app-server Hooks", ic: "shield" },
  { g: "会话" },
  { slash: "goal", name: "目标", ds: "/goal 查看 · /goal <目标> 设置 · /goal resume 恢复 · /goal clear 清除", ic: "verify" },
  { slash: "btw", name: "侧边对话 (btw)", ds: "基于当前会话开一个临时 fork 侧聊,不影响主线", ic: "spark" },
  { slash: "diff", name: "查看改动", ds: "打开当前会话的 Git diff 侧栏", ic: "edit" },
  { slash: "preview", name: "预览文件", ds: "/preview <路径> 打开 Markdown 或 UTF-8 源文件", ic: "read" },
  OPEN_FILES_COMMAND,
  { slash: "status", name: "完整状态", ds: "线程 · 配置 · 账户 · 限额 · token", ic: "cpu" },
  { slash: "context", name: "上下文用量", ds: "查看 token 占用与容量", ic: "cpu" },
  { slash: "autocompact", name: "上下文上限", ds: "/autocompact [300k | default] 设置当前会话的最大上下文", ic: "simplify" },
  COMPACT_COMMAND,
  { slash: "clear", name: "新会话", ds: "开新 codex 会话", ic: "close" },
];
const CODEX_CMD_LIST: Cmd[] = CODEX_COMMANDS.filter(isCmd) as Cmd[];
export const CODEX_CLIENT_SLASHES = new Set(["model", "plan", "normal", "clear", "context", "autocompact", "status", "permissions", "fast", "goal", "btw", "diff", "preview", "open", "review", "compact", "rollback", ...EXTENSION_SLASHES]);
const HIDDEN_CODE_ONLY_SLASHES = new Set(["rollback"]);
export type CommandSurface = "code" | "work";
const GEMINI_SLASHES = new Set(["model", "permissions", "context", "diff", "preview", "open", "clear", "extensions"]);
export const commandsFor = (engine?: string, surface: CommandSurface = "code"): Command[] => (
  engine === "antigravity" ? CODEX_CMD_LIST.filter(c => GEMINI_SLASHES.has(c.slash)).map(c => {
    if (c.slash === "permissions") return { ...c, ds: "选择 Gemini 原生权限模式" };
    if (c.slash === "clear") return { ...c, ds: "开始新的 Gemini 会话" };
    if (c.slash === "extensions") return { ...c, name: "扩展目录", ds: "查看 Gemini Skills、Plugins、MCP 与 Hooks" };
    return c;
  })
    : engine === "dsh" ? CODEX_COMMANDS.filter(c => !isCmd(c) || ["model", "context", "diff", "preview", "open", "skills"].includes(c.slash)) : surface === "work"
    ? engine === "codex" ? CODEX_WORK_COMMANDS : WORK_COMMANDS
    : engine === "codex" ? CODEX_COMMANDS : COMMANDS
);
export const clientSlashesFor = (engine?: string): Set<string> => (engine === "antigravity" ? GEMINI_SLASHES : engine === "dsh" ? new Set(["model", "context", "diff", "preview", "open", "skills"]) : engine === "codex" ? CODEX_CLIENT_SLASHES : CLIENT_SLASHES);

export const DSH_SIDECHAT_UNAVAILABLE = "DSH 侧聊暂未开放，等待官方提供会话删除能力后再接入。";

/** A built-in command intentionally available on Code but absent from Work.
 * Unknown slashes return false so user-installed Claude skills remain usable. */
export function isKnownCodeOnlySlash(slash: string, engine?: string): boolean {
  const normalized = slash.toLowerCase();
  return (engine === "codex" && HIDDEN_CODE_ONLY_SLASHES.has(normalized)) || ((
    engine === "codex" ? CODEX_CMD_LIST : CMD_LIST).some(
    (command) => command.slash === normalized,
  ) && !commandsFor(engine, "work").some((command) => (
    "slash" in command && command.slash === normalized
  )));
}
// codex slash -> the prompt actually sent to codex (agentic; no TUI slash layer).
export const CODEX_PROMPTS: Record<string, string> = {
  init: "Create or update AGENTS.md at the repo root: a concise overview of this codebase, how to build/test/run it, and key conventions.",
};

// The command "token" the user is typing after "/", up to the first space.
// null when the input isn't an in-progress slash command (no leading "/", a
// path separator, or arguments already started). Drives the palette's show/hide.
export function slashToken(input: string): string | null {
  if (!input.startsWith("/")) return null;
  const after = input.slice(1);
  if (/[\s/\\]/.test(after)) return null;
  return after;
}

// Codex explicitly invokes a Skill with "$skill-name". Keep this separate from
// slash parsing: "$" is forwarded as prompt text after selection, while slash
// commands may be handled locally by cc-remote.
export function skillToken(input: string): string | null {
  if (!input.startsWith("$")) return null;
  const after = input.slice(1);
  if (/\s/.test(after)) return null;
  return after;
}

/** Enabled Skills whose trigger starts with `token`, deduplicated by trigger.
 * The app-server catalog is authoritative; disabled Skills must not be offered
 * as invokable even though extension management still lists them. */
export function matchSkills(
  token: string,
  items: EngineCapabilityItem[],
): EngineCapabilityItem[] {
  const prefix = token.toLowerCase();
  const seen = new Set<string>();
  return items.filter((item) => {
    if (item.kind !== "skill" || item.enabled === false) return false;
    const name = item.name.toLowerCase();
    if (!name.startsWith(prefix) || seen.has(name)) return false;
    seen.add(name);
    return true;
  }).sort((a, b) => a.name.localeCompare(b.name));
}

// Commands whose slash starts with `token` (case-insensitive, prefix match).
export function matchCommands(token: string, engine?: string,
                              surface: CommandSurface = "code", native: NativeCommandInfo[] = []): Cmd[] {
  const t = token.toLowerCase();
  const list = commandsFor(engine, surface).filter(isCmd) as Cmd[];
  const matches = native.filter(c => c.name.toLowerCase().startsWith(t));
  return [
    ...matches.map(c => ({ slash: c.name, name: c.name,
      ds: [c.description, c.input_hint].filter(Boolean).join(" · "), ic: "term" })),
    ...list.filter(c => c.slash.toLowerCase().startsWith(t) && !matches.some(n => n.name === c.slash)),
  ];
}

// Split "/slash rest of args" -> { slash, args }. null if not a slash line.
export function parseSlash(input: string): { slash: string; args: string } | null {
  if (!input.startsWith("/")) return null;
  const m = input.slice(1).match(/^(\S+)\s*([\s\S]*)$/);
  // A leading filesystem path is prompt text. Separators in command arguments
  // remain valid, for example "/open /Users/Tester/My Project".
  if (!m || /[/\\]/.test(m[1])) return null;
  return { slash: m[1].toLowerCase(), args: m[2].trim() };
}

/** Shared by the welcome composer and the live conversation. Client commands
 * must never accidentally become model prompts just because a session is new. */
export function routeComposerInput(
  input: string, engine?: string, surface: CommandSurface = "code", native?: NativeCommandInfo[],
): { kind: "empty" } | { kind: "blocked"; message: string }
  | { kind: "client"; slash: string; args: string }
  | { kind: "prompt"; prompt: string } {
  const raw = input.trim();
  if (raw === "/") return { kind: "empty" };
  const parsed = parseSlash(raw);
  if (engine === "dsh" && parsed?.slash === "btw") {
    return { kind: "blocked", message: DSH_SIDECHAT_UNAVAILABLE };
  }
  if (engine === "antigravity" && parsed?.slash === "goal" && !parsed.args) {
    return { kind: "blocked", message: "请在 /goal 后填写目标。" };
  }
  if (engine === "antigravity" && parsed && native?.some(c => c.name === parsed.slash)) {
    return { kind: "prompt", prompt: raw };
  }
  if (surface === "work" && parsed && isKnownCodeOnlySlash(parsed.slash, engine)) {
    return { kind: "blocked", message: `/${parsed.slash} 是 Code 指令，请切换到 Code 使用` };
  }
  if (parsed && clientSlashesFor(engine).has(parsed.slash)) {
    return { kind: "client", ...parsed };
  }
  if (engine === "codex" && parsed) {
    // Use an own-property lookup: arbitrary slash names are untrusted input.
    if (Object.hasOwn(CODEX_PROMPTS, parsed.slash)) {
      return { kind: "prompt", prompt: CODEX_PROMPTS[parsed.slash]
        + (parsed.args ? "\n\n" + parsed.args : "") };
    }
    return { kind: "blocked", message: `Codex 无此指令：/${parsed.slash}` };
  }
  return { kind: "prompt", prompt: raw };
}
