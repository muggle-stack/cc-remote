// Slash commands, models, permission modes. Slash commands split into
// client-side ones (CLIENT_SLASHES: model/plan/normal/permissions/clear/context,
// handled in Composer.send) and cc skills (forwarded verbatim to cc). Model/perm
// chips drive set_model / set_permission_mode on the wrapper.

import type { CatalogModel } from "./protocol";

export interface CmdGroup { g: string }
export interface Cmd { slash: string; name: string; ds: string; ic: string }
export type Command = CmdGroup | Cmd;

export const COMMANDS: Command[] = [
  { g: "模式" },
  { slash: "plan", name: "Plan mode", ds: "先给方案，确认后再动手", ic: "plan" },
  { slash: "normal", name: "普通模式", ds: "直接执行，边做边说", ic: "run" },
  { slash: "permissions", name: "权限模式", ds: "选择 cc 的权限模式", ic: "shield" },
  { g: "模型" },
  { slash: "model", name: "切换模型", ds: "/model <id> 切到指定模型(支持隐藏模型),无参数则打开选择器", ic: "cpu" },
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
  { slash: "goal", name: "目标", ds: "/goal 查看 · /goal <目标> 设置 · /goal clear 清除", ic: "verify" },
  { slash: "btw", name: "侧边对话 (btw)", ds: "基于当前会话开一个临时 fork 侧聊,不影响主线", ic: "spark" },
  { slash: "preview", name: "预览文件", ds: "/preview <路径> 打开 Markdown 或 UTF-8 源文件", ic: "read" },
  { slash: "clear", name: "清空会话", ds: "开新会话，清空上下文", ic: "close" },
  { slash: "context", name: "上下文用量", ds: "查看 token 占用", ic: "cpu" },
];

// `efforts` overrides the engine's baseline effort list for THIS model — reasoning
// levels are per-model, not per-engine.
export interface Model { id: string; name: string; ds: string; ic: string; efforts?: Effort[] }
export const MODELS: Model[] = [
  { id: "claude-mythos-5", name: "Mythos 5", ds: "最强王牌", ic: "crown" },
  { id: "claude-opus-4-8", name: "Opus 4.8", ds: "最强推理", ic: "gem" },
  { id: "claude-sonnet-5", name: "Sonnet 5", ds: "均衡 · 更快", ic: "balance" },
  { id: "claude-haiku-4-5", name: "Haiku 4.5", ds: "轻量 · 极速", ic: "bolt" },
  { id: "claude-fable-5", name: "Fable 5", ds: "实验模型", ic: "book" },
];

// Reasoning effort (思考强度). `name` is the RAW level id on purpose: it's what
// `~/.codex/config.toml` (model_reasoning_effort) and cc's `--effort` take, so what
// the chip shows is exactly what you can grep for in the config. No translated labels.
export interface Effort { id: string; name: string; ds: string; ic: string }

// Blurb + icon per level, low -> high. Order here IS the cost/latency order, and is
// what we rank by — never trust arrival order from the server.
const EFFORT_META: Record<string, { ds: string; ic: string }> = {
  minimal: { ds: "几乎不推理 · 最快最省", ic: "gauge1" },
  low: { ds: "更快 · 轻推理", ic: "gauge1" },
  medium: { ds: "均衡 · 日常任务", ic: "gauge2" },
  high: { ds: "深度推理 · 复杂问题", ic: "gauge3" },
  xhigh: { ds: "更深推理 · 更慢", ic: "gauge4" },
  max: { ds: "最深推理 · 最难的问题", ic: "gauge5" },
  ultra: { ds: "极限推理 · 最慢最贵", ic: "crown" },
};
export const EFFORT_ORDER = Object.keys(EFFORT_META);
const rank = (id: string) => { const i = EFFORT_ORDER.indexOf(id); return i < 0 ? EFFORT_ORDER.length : i; };
const effort = (id: string): Effort => ({ id, name: id, ds: EFFORT_META[id]?.ds ?? "", ic: EFFORT_META[id]?.ic ?? "gauge3" });
const efforts = (...ids: string[]): Effort[] => ids.map(effort);

export const EFFORTS: Effort[] = efforts("low", "medium", "high", "xhigh", "max");        // cc

// ---- Codex: the app-server IS the catalog ----
// Everything below is only a FALLBACK for the first paint (and if `model/list`
// fails). The live catalog arrives as a `models` frame and wins — see catalogFor().
// Hardcoding this table is what produced two shipped bugs: `minimal` exists on no
// model at all, and gpt-5.6-luna tops out at `max`, not `ultra`. `turn/start` accepts
// ANY effort string (measured: `bogus-zzz` starts a turn), so a level we invent here
// doesn't fail loudly — it fails deep inside the model API. Never guess these.
export const CODEX_EFFORTS: Effort[] = efforts("low", "medium", "high", "xhigh");         // gpt-5.5 and older
export const CODEX_EFFORTS_56: Effort[] = efforts("low", "medium", "high", "xhigh", "max", "ultra");
export const CODEX_MODELS: Model[] = [
  { id: "gpt-5.6-sol", name: "GPT-5.6 Sol", ds: "旗舰 · 最强 agentic 编码", ic: "crown", efforts: CODEX_EFFORTS_56 },
  { id: "gpt-5.6-terra", name: "GPT-5.6 Terra", ds: "均衡 · 日常编码", ic: "balance", efforts: CODEX_EFFORTS_56 },
  { id: "gpt-5.6-luna", name: "GPT-5.6 Luna", ds: "轻量 · 更快", ic: "bolt", efforts: efforts("low", "medium", "high", "xhigh", "max") },
  { id: "gpt-5.5", name: "GPT-5.5", ds: "上一代 · 旧会话兼容", ic: "cpu" },
  { id: "gpt-5.4", name: "GPT-5.4", ds: "更早 · 旧会话兼容", ic: "cpu" },
];
export const CODEX_PERMS: Perm[] = [
  { id: "never", name: "不询问", short: "不询问", ds: "不询问 · 需要审批时拒绝", ic: "shield" },
  { id: "on-request", name: "按需", short: "按需", ds: "需要时才询问", ic: "shield" },
  { id: "untrusted", name: "严格", short: "严格", ds: "每步都先询问", ic: "shield" },
];

/** Live catalogs by engine, as reported by the wrapper (`models` frame). */
export type Catalog = Record<string, CatalogModel[]>;

// Pretty Chinese name/blurb/icon per known id. A model we've never heard of still
// renders — it just borrows the server's own English display_name/description, so a
// newly-shipped codex model appears in the UI without a redeploy.
const CODEX_LOOKS: Record<string, { name: string; ds: string; ic: string }> = {
  "gpt-5.6-sol": { name: "GPT-5.6 Sol", ds: "旗舰 · 最强 agentic 编码", ic: "crown" },
  "gpt-5.6-terra": { name: "GPT-5.6 Terra", ds: "均衡 · 日常编码", ic: "balance" },
  "gpt-5.6-luna": { name: "GPT-5.6 Luna", ds: "轻量 · 更快", ic: "bolt" },
};

const fromCatalog = (entries: CatalogModel[]): Model[] =>
  entries.map((e) => {
    const look = CODEX_LOOKS[e.id];
    return {
      id: e.id,
      name: look?.name ?? e.display_name ?? e.id,
      ds: look?.ds ?? e.description ?? "",
      ic: look?.ic ?? "cpu",
      efforts: e.efforts?.length ? e.efforts.map(effort) : undefined,
    };
  });

export const modelsFor = (engine?: string, catalog?: Catalog): Model[] => {
  if (engine !== "codex") return MODELS;
  const live = catalog?.codex;
  return live?.length ? fromCatalog(live) : CODEX_MODELS;
};

/** Model a NEW chat starts on: the engine's configured default (codex config.toml's
 *  `model`), else the first catalog entry. An EXISTING session's model is per-session
 *  and comes from the wrapper, never from here. */
export const defaultModelFor = (engine?: string, catalog?: Catalog,
                                defaults?: Record<string, string>): string => {
  const list = modelsFor(engine, catalog);
  const want = engine ? defaults?.[engine] : undefined;
  // Codex defaults are accepted only when app-server's live catalog contains
  // them. Claude may legitimately use a custom/hidden alias absent from our
  // static presentation table, so preserve its wrapper-resolved id.
  return want && (engine !== "codex" || list.some((m) => m.id === want))
    ? want : list[0].id;
};

/** Effort levels the SELECTED model actually accepts. Unknown/unset model falls back
 *  to the engine's baseline list. */
export const effortsFor = (engine?: string, model?: string | null, catalog?: Catalog): Effort[] => {
  const m = model ? modelsFor(engine, catalog).find((x) => x.id === model) : undefined;
  if (m?.efforts) return m.efforts;
  return engine === "codex" ? CODEX_EFFORTS : EFFORTS;
};

/** Default effort = the HIGHEST level the selected model supports (product decision:
 *  always think as hard as the model allows — we deliberately ignore the server's
 *  own `default_effort`, which is `low` for sol). Also what we clamp to when the user
 *  switches to a model that lacks the current level (sol `ultra` -> luna `max`). */
export const defaultEffortFor = (engine?: string, model?: string | null, catalog?: Catalog): string => {
  const list = effortsFor(engine, model, catalog);
  return list.reduce((a, b) => (rank(b.id) > rank(a.id) ? b : a)).id;
};
export const permsFor = (engine?: string): Perm[] => (engine === "codex" ? CODEX_PERMS : PERMS);

// Map a cc-reported model id (e.g. "claude-mythos-5[1m]") to a MODELS entry id.
// An id we don't know (any codex model) passes through verbatim — the codex chips
// resolve it against the live catalog themselves.
export function matchModelId(m: string, engine?: string): string {
  const base = m.replace(/\[.*\]$/, "");
  const hit = modelsFor(engine).find((x) => base === x.id || base.startsWith(x.id));
  return hit ? hit.id : m;
}

export interface Perm { id: string; name: string; short: string; ds: string; ic: string; danger?: boolean }
export const PERMS: Perm[] = [
  { id: "default", name: "默认", short: "询问", ds: "每次动作前询问", ic: "shield" },
  { id: "acceptEdits", name: "自动接受编辑", short: "编辑", ds: "文件编辑免询问，命令仍询问", ic: "edit" },
  { id: "plan", name: "Plan 模式", short: "Plan", ds: "只读 · 先出方案再执行", ic: "plan" },
  { id: "auto", name: "自动", short: "自动", ds: "自动执行常规操作", ic: "run" },
  { id: "bypassPermissions", name: "危险模式", short: "危险", ds: "危险 · 不询问直接执行 · --dangerously-skip-permissions", ic: "bolt", danger: true },
];

export function isCmd(c: Command): c is Cmd {
  return (c as Cmd).slash !== undefined;
}

const CMD_LIST: Cmd[] = COMMANDS.filter(isCmd) as Cmd[];

// Slashes handled locally by the web client (never forwarded to cc as a prompt).
// Everything else (code-review, verify, run, deep-research, …) is a cc skill and
// is forwarded verbatim so cc's own slash-command layer runs it.
export const CLIENT_SLASHES = new Set(["model", "plan", "normal", "permissions", "clear", "context", "goal", "btw", "preview"]);

// Codex engine command palette — its REAL slash commands (verified against the
// codex binary's own "get started" hint: /init /status /permissions /model /review).
// Client-handled: model/clear/context/status/permissions. /context is the focused
// thread's token window; /status is a separate app-server snapshot (thread,
// config, account and rate limits). Forwarded
// ones (review/init) expand to a prompt codex handles agentically — the app-server
// has no TUI slash layer. /fast maps to app-server's per-thread service tier,
// and /hook is Claude-only. /plan and /normal are handled locally by the web
// client and mapped to app-server collaborationMode, not sent as prompt text.
export const CODEX_COMMANDS: Command[] = [
  { g: "审查" },
  { slash: "review", name: "代码审查", ds: "审当前 git 改动的 bug 与改进", ic: "review" },
  { g: "项目" },
  { slash: "init", name: "初始化 AGENTS.md", ds: "生成/更新代码库说明", ic: "init" },
  { g: "模式" },
  { slash: "plan", name: "Plan mode", ds: "先调研并给出方案 · 下条消息生效", ic: "plan" },
  { slash: "normal", name: "默认模式", ds: "退出 Plan，恢复正常执行 · 下条消息生效", ic: "run" },
  { slash: "model", name: "切换模型", ds: "选择模型与思考强度", ic: "cpu" },
  { slash: "permissions", name: "权限模式", ds: "选择 Codex 的审批策略(自动/按需/严格)", ic: "shield" },
  { slash: "fast", name: "Fast 模式", ds: "开/关 Fast 服务档位(更快响应),下条消息生效", ic: "bolt" },
  { g: "会话" },
  { slash: "goal", name: "目标", ds: "/goal 查看 · /goal <目标> 设置 · /goal clear 清除", ic: "verify" },
  { slash: "btw", name: "侧边对话 (btw)", ds: "基于当前会话开一个临时 fork 侧聊,不影响主线", ic: "spark" },
  { slash: "preview", name: "预览文件", ds: "/preview <路径> 打开 Markdown 或 UTF-8 源文件", ic: "read" },
  { slash: "status", name: "完整状态", ds: "线程 · 配置 · 账户 · 限额 · token", ic: "cpu" },
  { slash: "context", name: "上下文用量", ds: "查看 token 占用与容量", ic: "cpu" },
  { slash: "clear", name: "新会话", ds: "开新 codex 会话", ic: "close" },
];
const CODEX_CMD_LIST: Cmd[] = CODEX_COMMANDS.filter(isCmd) as Cmd[];
export const CODEX_CLIENT_SLASHES = new Set(["model", "plan", "normal", "clear", "context", "status", "permissions", "fast", "goal", "btw", "preview"]);
export const commandsFor = (engine?: string): Command[] => (engine === "codex" ? CODEX_COMMANDS : COMMANDS);
export const clientSlashesFor = (engine?: string): Set<string> => (engine === "codex" ? CODEX_CLIENT_SLASHES : CLIENT_SLASHES);
// codex slash -> the prompt actually sent to codex (agentic; no TUI slash layer).
export const CODEX_PROMPTS: Record<string, string> = {
  review: "Review the current git changes (the diff) for correctness bugs, risks, and simplifications. Be concise.",
  init: "Create or update AGENTS.md at the repo root: a concise overview of this codebase, how to build/test/run it, and key conventions.",
};

// The command "token" the user is typing after "/", up to the first space.
// null when the input isn't an in-progress slash command (no leading "/", or a
// space already started the arguments). Drives the palette's show/hide.
export function slashToken(input: string): string | null {
  if (!input.startsWith("/")) return null;
  const after = input.slice(1);
  if (/\s/.test(after)) return null; // a space => choosing args, not the command
  return after;
}

// Commands whose slash starts with `token` (case-insensitive, prefix match).
export function matchCommands(token: string, engine?: string): Cmd[] {
  const t = token.toLowerCase();
  const list = engine === "codex" ? CODEX_CMD_LIST : CMD_LIST;
  return list.filter((c) => c.slash.toLowerCase().startsWith(t));
}

// Split "/slash rest of args" -> { slash, args }. null if not a slash line.
export function parseSlash(input: string): { slash: string; args: string } | null {
  if (!input.startsWith("/")) return null;
  const m = input.slice(1).match(/^(\S+)\s*([\s\S]*)$/);
  if (!m) return null;
  return { slash: m[1].toLowerCase(), args: m[2].trim() };
}
