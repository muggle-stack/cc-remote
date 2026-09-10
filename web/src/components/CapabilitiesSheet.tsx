import { useEffect, useState, type FormEvent } from "react";
import { Icon } from "../icons";
import type {
  EngineCapabilities, EngineCapabilityItem, EngineCapabilityKind,
} from "../protocol";

export interface SkillDraft {
  name: string;
  description: string;
  instructions: string;
  scope: "user" | "project";
}

export interface HookDraft {
  event: string;
  matcher: string;
  command: string;
  timeout: number;
  scope: "user" | "project";
}

interface Props {
  open: boolean;
  engine: "claude" | "codex" | "dsh";
  activeKind: EngineCapabilityKind | "all";
  report: EngineCapabilities | null;
  loading: boolean;
  readOnly?: boolean;
  onKindChange: (kind: EngineCapabilityKind | "all") => void;
  onRefresh: () => void;
  onManagePlugin: (item: EngineCapabilityItem, action: "install" | "uninstall") => void;
  onManageSkill: (item: EngineCapabilityItem, action: "remove" | "enable" | "disable") => void;
  onCreateSkill: (draft: SkillDraft) => void;
  onRemoveHook: (item: EngineCapabilityItem) => void;
  onCreateHook: (draft: HookDraft) => void;
  onClose: () => void;
}

const LABELS: Record<EngineCapabilityKind, string> = {
  skill: "Skills", plugin: "Plugins", app: "Apps", mcp: "MCP", hook: "Hooks",
};
const KINDS: EngineCapabilityKind[] = ["skill", "plugin", "app", "mcp", "hook"];
const CLAUDE_HOOK_EVENTS = [
  "PreToolUse", "PermissionRequest", "PostToolUse", "PostToolUseFailure",
  "Notification", "UserPromptSubmit", "SessionStart", "SessionEnd", "Stop",
  "SubagentStart", "SubagentStop", "PreCompact", "PostCompact",
];

function itemMeta(item: EngineCapabilityItem): string {
  const bits: string[] = [];
  if (item.installed !== undefined) bits.push(item.installed ? "已安装" : "未安装");
  if (item.enabled !== undefined) bits.push(item.enabled ? "已启用" : "已停用");
  if (item.status) bits.push(item.status);
  if (item.scope) bits.push(item.scope);
  if (item.event && item.event !== item.name) bits.push(item.event);
  if (item.matcher) bits.push(`匹配 ${item.matcher}`);
  if (item.handler_type) bits.push(item.handler_type);
  if (item.tool_count !== undefined) bits.push(`${item.tool_count} 个工具`);
  if (item.resource_count !== undefined) bits.push(`${item.resource_count} 个资源`);
  return bits.join(" · ");
}

function hasAction(item: EngineCapabilityItem, action: string): boolean {
  return item.actions?.includes(action as never) ?? false;
}

export function CapabilitiesSheet({
  open, engine, activeKind, report, loading, readOnly = false,
  onKindChange, onRefresh, onManagePlugin, onManageSkill, onCreateSkill,
  onRemoveHook, onCreateHook, onClose,
}: Props) {
  const [adding, setAdding] = useState<"skill" | "hook" | null>(null);
  const [skill, setSkill] = useState<SkillDraft>({
    name: "", description: "", instructions: "", scope: "user",
  });
  const [hook, setHook] = useState<HookDraft>({
    event: "PreToolUse", matcher: "", command: "", timeout: 60, scope: "user",
  });

  useEffect(() => { if (!open) setAdding(null); }, [open]);
  if (!open) return null;

  const visibleKinds = activeKind === "all" ? KINDS : [activeKind];
  const grouped = visibleKinds.map((kind) => ({
    kind, items: report?.items.filter((item) => item.kind === kind) ?? [],
  }));
  const submitSkill = (event: FormEvent) => {
    event.preventDefault();
    onCreateSkill(skill);
    setAdding(null);
    setSkill({ name: "", description: "", instructions: "", scope: "user" });
  };
  const submitHook = (event: FormEvent) => {
    event.preventDefault();
    onCreateHook(hook);
    setAdding(null);
    setHook({ event: "PreToolUse", matcher: "", command: "", timeout: 60, scope: "user" });
  };

  return <>
    <div className="scrim show" onClick={onClose} />
    <section className="capabilities-sheet" role="dialog" aria-modal="true" aria-label="Extensions">
      <header>
        <div><b>Extensions</b><small>当前引擎的真实扩展目录</small></div>
        <div className="capabilities-head-actions">
          <button className="iconbtn" onClick={onRefresh} aria-label="刷新" title="刷新"><Icon name="refresh" /></button>
          <button className="iconbtn" onClick={onClose} aria-label="关闭"><Icon name="close" /></button>
        </div>
      </header>
      <nav className="capabilities-tabs" aria-label="扩展类型">
        <button className={activeKind === "all" ? "active" : ""} onClick={() => onKindChange("all")}>全部</button>
        {KINDS.map((kind) => <button key={kind}
          className={activeKind === kind ? "active" : ""}
          onClick={() => onKindChange(kind)}>{LABELS[kind]}</button>)}
      </nav>
      <div className="capabilities-body">
        {readOnly && <div className="capabilities-note">Work 仅展示有效目录，不允许修改 Code 扩展。</div>}
        {loading && !report && <div className="capabilities-empty">正在读取扩展目录…</div>}
        {report?.notes?.map((note) => <div className="capabilities-note" key={note}>{note}</div>)}
        {report?.errors?.length ? <div className="capabilities-errors">
          <b>部分目录暂不可用</b><span>稍后刷新即可，不影响当前会话。</span>
        </div> : null}

        {!readOnly && activeKind === "skill" && <div className="capabilities-create">
          <button onClick={() => setAdding(adding === "skill" ? null : "skill")}>+ 新建 Skill</button>
          {adding === "skill" && <form onSubmit={submitSkill}>
            <input required maxLength={64} placeholder="Skill 名称" value={skill.name}
              onChange={(event) => setSkill({ ...skill, name: event.target.value })} />
            <input maxLength={4096} placeholder="一句话说明" value={skill.description}
              onChange={(event) => setSkill({ ...skill, description: event.target.value })} />
            <textarea required maxLength={131072} placeholder="何时使用，以及应该如何执行…" value={skill.instructions}
              onChange={(event) => setSkill({ ...skill, instructions: event.target.value })} />
            <select value={skill.scope} onChange={(event) => setSkill({ ...skill, scope: event.target.value as SkillDraft["scope"] })}>
              <option value="user">用户级</option><option value="project">当前项目</option>
            </select>
            <div><button type="button" onClick={() => setAdding(null)}>取消</button><button type="submit">创建</button></div>
          </form>}
        </div>}

        {!readOnly && activeKind === "hook" && engine === "claude" && <div className="capabilities-create">
          <button onClick={() => setAdding(adding === "hook" ? null : "hook")}>+ 新建 Hook</button>
          {adding === "hook" && <form onSubmit={submitHook}>
            <select value={hook.event} onChange={(event) => setHook({ ...hook, event: event.target.value })}>
              {CLAUDE_HOOK_EVENTS.map((event) => <option value={event} key={event}>{event}</option>)}
            </select>
            <input maxLength={2048} placeholder="Matcher（可选）" value={hook.matcher}
              onChange={(event) => setHook({ ...hook, matcher: event.target.value })} />
            <textarea required maxLength={16384} placeholder="要执行的本机命令" value={hook.command}
              onChange={(event) => setHook({ ...hook, command: event.target.value })} />
            <input required type="number" min={1} max={3600} value={hook.timeout}
              onChange={(event) => setHook({ ...hook, timeout: Number(event.target.value) })} />
            <select value={hook.scope} onChange={(event) => setHook({ ...hook, scope: event.target.value as HookDraft["scope"] })}>
              <option value="user">用户级</option><option value="project">当前项目</option>
            </select>
            <div><button type="button" onClick={() => setAdding(null)}>取消</button><button type="submit">创建</button></div>
          </form>}
        </div>}
        {!readOnly && activeKind === "hook" && engine === "codex" && <div className="capabilities-note">
          Codex app-server 当前只提供 Hooks 清单，没有写接口；这里按真实能力只读展示。
        </div>}

        {!loading && report && grouped.every((group) => !group.items.length)
          && <div className="capabilities-empty">当前分类没有扩展。</div>}
        {grouped.map((group) => group.items.length > 0 && <section className="capabilities-group" key={group.kind}>
          <h3>{LABELS[group.kind]}<span>{group.items.length}</span></h3>
          {group.items.map((item) => <article key={`${item.kind}:${item.id}`}>
            <div><b>{item.name}</b>{item.description && <p>{item.description}</p>}{item.detail && <p>{item.detail}</p>}</div>
            <div className="capabilities-item-actions">
              <small>{itemMeta(item)}</small>
              {!readOnly && hasAction(item, "install") && <button onClick={() => onManagePlugin(item, "install")}>安装</button>}
              {!readOnly && hasAction(item, "uninstall") && <button onClick={() => onManagePlugin(item, "uninstall")}>卸载</button>}
              {!readOnly && hasAction(item, "enable") && <button onClick={() => onManageSkill(item, "enable")}>启用</button>}
              {!readOnly && hasAction(item, "disable") && <button onClick={() => onManageSkill(item, "disable")}>停用</button>}
              {!readOnly && item.kind === "skill" && hasAction(item, "remove") && <button className="danger" onClick={() => onManageSkill(item, "remove")}>删除</button>}
              {!readOnly && item.kind === "hook" && hasAction(item, "remove") && <button className="danger" onClick={() => onRemoveHook(item)}>删除</button>}
              {item.kind === "app" && item.install_url && item.status !== "accessible" && <a
                href={item.install_url} target="_blank" rel="noopener noreferrer">连接</a>}
            </div>
          </article>)}
        </section>)}
      </div>
    </section>
  </>;
}
