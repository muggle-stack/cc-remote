import { useEffect, useRef, useState, type TouchEvent } from "react";
import type { SessionInfo, State } from "../protocol";
import { Icon, ClaudeMark } from "../icons";
import { isWorktreeForkBlockedByState, sessionMenuCapabilities } from "../session-worktree";
import { useImeSubmit } from "../use-ime-submit";

interface Props {
  open: boolean;
  sessions: SessionInfo[];
  // Live state per resident session (from the client's runtimes), overriding the
  // list_sessions snapshot so a background session's dot updates in real time.
  liveStates?: Record<string, State>;
  activeSessionId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onNewInDir: (cwd: string) => void;
  onClose: () => void;
  onRename: (id: string, title: string) => void;
  onArchive: (id: string, archived: boolean) => void;
  onForkWorktree: (session: SessionInfo) => void;
}

function basename(cwd?: string | null): string {
  if (!cwd) return "其他";
  const parts = cwd.replace(/\/+$/, "").split("/");
  return parts[parts.length - 1] || cwd;
}

export function SessionsSidebar({ open, sessions, liveStates, activeSessionId, onSelect, onNew, onNewInDir, onClose, onRename, onArchive, onForkWorktree }: Props) {
  const [q, setQ] = useState("");
  const [menuCardId, setMenuCardId] = useState<string | null>(null);
  const [lifting, setLifting] = useState(false);
  const [renaming, setRenaming] = useState<{ id: string; value: string } | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  // "archived" group starts collapsed; project groups start expanded.
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({ archived: true });
  const pressTimer = useRef<number | null>(null);
  const pressStart = useRef<{ x: number; y: number } | null>(null);

  const filter = q.toLowerCase();
  const matches = (s: SessionInfo) =>
    !filter
    || (s.summary || "").toLowerCase().includes(filter)
    || (s.first_prompt || "").toLowerCase().includes(filter)
    || (s.cwd || "").toLowerCase().includes(filter)
    || s.session_id.toLowerCase().includes(filter);
  const filtered = sessions.filter(matches);
  const archived = filtered.filter((s) => s.tag === "archived");
  const active = filtered.filter((s) => s.tag !== "archived");

  const groups: Record<string, SessionInfo[]> = {};
  for (const s of active) {
    const key = s.cwd || "";
    (groups[key] = groups[key] || []).push(s);
  }
  for (const k of Object.keys(groups)) {
    groups[k].sort((a, b) =>
      (parseInt(b.last_modified || "0", 10) || 0) - (parseInt(a.last_modified || "0", 10) || 0));
  }
  const groupKeys = Object.keys(groups).sort();

  const toggleGroup = (key: string) => setCollapsed((c) => ({ ...c, [key]: !c[key] }));
  const commitRename = (value = renaming?.value ?? "") => {
    if (renaming && value.trim()) onRename(renaming.id, value.trim());
    setRenaming(null);
  };
  const renameIme = useImeSubmit<HTMLInputElement>(commitRename);
  const startRename = (s: SessionInfo) => {
    setRenaming({ id: s.session_id, value: s.summary || s.first_prompt || "" });
    setMenuCardId(null); setLifting(false);
  };
  const doArchive = (s: SessionInfo, archived: boolean) => {
    onArchive(s.session_id, archived);
    setMenuCardId(null); setLifting(false);
  };
  const doForkWorktree = (s: SessionInfo) => {
    onForkWorktree(s);
    setMenuCardId(null); setLifting(false);
  };
  const doCopyId = (s: SessionInfo) => {
    navigator.clipboard?.writeText(s.session_id).catch(() => {});
    setCopiedId(s.session_id);
    window.setTimeout(() => setCopiedId(null), 1200);
  };
  const closeMenu = () => { setMenuCardId(null); setLifting(false); };

  // reset menu/lift state when the sidebar closes (so it doesn't linger into the next open)
  useEffect(() => { if (!open) { setMenuCardId(null); setLifting(false); } }, [open]);

  // dismiss the ⋯ popover on any click outside it — covers the sidebar header, footer,
  // the side scrim, and the rest of the page (not just .s-scroll). The long-press lift
  // is dismissed via .s-lift-scrim instead, so we only handle the non-lifting popover here.
  useEffect(() => {
    if (menuCardId === null || lifting) return;
    const onDocClick = (e: MouseEvent) => {
      const t = e.target as Element | null;
      if (t && (t.closest?.(".card-menu") || t.closest?.(".scard-act"))) return;  // click inside menu / on ⋯
      setMenuCardId(null);
    };
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [menuCardId, lifting]);

  // long-press (500ms hold, no >10px move) => lift the card + dim the rest + show actions.
  // A swipe (handled by the shell) moves >10px and cancels the timer.
  const onCardTouchStart = (s: SessionInfo, e: TouchEvent) => {
    pressStart.current = { x: e.touches[0].clientX, y: e.touches[0].clientY };
    pressTimer.current = window.setTimeout(() => {
      setMenuCardId(s.session_id);
      setLifting(true);
      if (navigator.vibrate) navigator.vibrate(15);
    }, 500);
  };
  const onCardTouchMove = (e: TouchEvent) => {
    if (!pressStart.current) return;
    const dx = Math.abs(e.touches[0].clientX - pressStart.current.x);
    const dy = Math.abs(e.touches[0].clientY - pressStart.current.y);
    if ((dx > 10 || dy > 10) && pressTimer.current) {
      clearTimeout(pressTimer.current);
      pressTimer.current = null;
    }
  };
  const onCardTouchEnd = () => {
    if (pressTimer.current) { clearTimeout(pressTimer.current); pressTimer.current = null; }
  };

  const renderCard = (s: SessionInfo) => {
    const isActive = s.session_id === activeSessionId;
    const isArchived = s.tag === "archived";
    const isMenu = menuCardId === s.session_id;
    const capabilities = sessionMenuCapabilities(s);
    if (renaming && renaming.id === s.session_id) {
      return (
        <div key={s.session_id} className={"scard" + (isActive ? " active" : "")}>
          <div className="scard-top">
            <input
              ref={renameIme.inputRef}
              className="scard-rename"
              autoFocus
              value={renaming.value}
              onChange={(e) => setRenaming({ ...renaming, value: e.target.value })}
              onCompositionStart={renameIme.startComposition}
              onCompositionEnd={(e) => {
                renameIme.endComposition();
                setRenaming({ ...renaming, value: e.currentTarget.value });
              }}
              onKeyDown={(e) => {
                if (e.key === "Escape" && !e.nativeEvent.isComposing
                    && e.nativeEvent.keyCode !== 229) {
                  setRenaming(null);
                  return;
                }
                if (!renameIme.shouldSubmitKey({
                  key: e.key,
                  shiftKey: e.shiftKey,
                  isComposing: e.nativeEvent.isComposing,
                  keyCode: e.nativeEvent.keyCode,
                })) return;
                e.preventDefault();
                renameIme.requestSubmit();
              }}
              onBlur={(e) => commitRename(e.currentTarget.value)}
            />
          </div>
        </div>
      );
    }
    const onTitleClick = () => {
      if (lifting) { closeMenu(); return; }
      onSelect(s.session_id);
    };
    // Prefer live runtime state (resident session) over the list snapshot.
    const st = liveStates?.[s.session_id] ?? s.state;
    const forkBlocked = isWorktreeForkBlockedByState(st);
    return (
      <div
        key={s.session_id}
        className={"scard" + (isActive ? " active" : "") + (isArchived ? " archived" : "") + (isMenu ? " menu-open" : "") + (isMenu && lifting ? " lifting" : "")}
        onTouchStart={(e) => onCardTouchStart(s, e)}
        onTouchMove={onCardTouchMove}
        onTouchEnd={onCardTouchEnd}
        onClick={onTitleClick}
      >
        <div className="scard-top">
          <span className="scard-title">
            {s.summary || (s.first_prompt || "").slice(0, 40) || s.session_id.slice(0, 8)}
          </span>
          {isActive && <span className="pill idle"><span className="sd" />当前</span>}
          {(st === "running" || st === "interrupting") && (
            <span className={"pill " + st}><span className="sd" />{st === "running" ? "运行" : "中断"}</span>
          )}
        </div>
        {s.cwd && <div className="scard-path" title={s.cwd}>{s.cwd}</div>}
        {s.first_prompt && !s.summary && <div className="scard-prev">{s.first_prompt}</div>}
        <div className="scard-actions">
          <button className="scard-act" aria-label="更多操作"
            onClick={(e) => { e.stopPropagation(); setMenuCardId(isMenu ? null : s.session_id); setLifting(false); }}>
            <Icon name="dots" size={15} />
          </button>
        </div>
        {isMenu && (
          <div className="card-menu" onClick={(e) => e.stopPropagation()}>
            {capabilities.rename && (
              <button onClick={() => startRename(s)}><Icon name="edit" size={15} />重命名</button>
            )}
            {capabilities.forkWorktree && (
              <button onClick={() => doForkWorktree(s)} disabled={forkBlocked}
                title={forkBlocked ? "请等待当前任务结束" : "从当前 Git HEAD 创建新工作树"}>
                <Icon name="branch" size={15} />派生到新工作树…
              </button>
            )}
            <button onClick={() => doCopyId(s)}><Icon name={copiedId === s.session_id ? "check" : "copy"} size={15} />{copiedId === s.session_id ? "已复制" : "复制 session ID"}</button>
            {capabilities.archive && (isArchived ? (
              <button onClick={() => doArchive(s, false)}><Icon name="archive" size={15} />取消归档</button>
            ) : (
              <button onClick={() => doArchive(s, true)}><Icon name="archive" size={15} />归档</button>
            ))}
          </div>
        )}
      </div>
    );
  };

  const renderGroup = (key: string, items: SessionInfo[], label?: string) => {
    // While searching, expand every group so matches in collapsed/archived groups are visible.
    const isCollapsed = filter ? false : collapsed[key];
    const title = label || basename(key);
    const canAdd = !!key && !label;  // real cwd group (not "other"/"archived")
    return (
      <div key={key || "other"} className="sgroup-wrap">
        <div className="sgroup-head">
          <button className={"sgroup-toggle" + (isCollapsed ? " collapsed" : "")} onClick={() => toggleGroup(key)}>
            <span className="chev"><Icon name="chev" size={13} /></span>
            <span className="label" title={key || undefined}>{title}</span>
            <span className="count">{items.length}</span>
          </button>
          {canAdd && (
            <button className="sgroup-add" title={`在 ${title} 新建会话`} aria-label="在此目录新建会话"
              onClick={(e) => { e.stopPropagation(); onNewInDir(key); }}>
              <Icon name="plus" size={14} />
            </button>
          )}
        </div>
        {!isCollapsed && items.map(renderCard)}
      </div>
    );
  };

  return (
    <>
      <div className={"scrim-side" + (open ? " show" : "")} onClick={onClose} />
      <aside className={"sessions" + (open ? " show" : "")}>
        {/* fixed-width inner so the desktop column can animate its width (0→352)
            as a slide-reveal without the content reflowing/squishing. */}
        <div className="s-inner">
          <div className="s-head">
            <div className="brand" onClick={onClose}>
              <span className="brand-mark"><ClaudeMark size={17} /></span>
              <span className="name"><b>cc</b><span>·remote</span></span>
            </div>
            <button className="iconbtn" onClick={onClose} aria-label="收起"><Icon name="chevrons-left" /></button>
          </div>
          <div className="search">
            <Icon name="search" size={17} />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索会话、路径…" aria-label="搜索会话" />
          </div>
          <div className="s-scroll" onClick={closeMenu}>
            {filtered.length === 0 && <div className="s-group">{sessions.length === 0 ? "暂无会话" : "无匹配"}</div>}
            {groupKeys.map((k) => renderGroup(k, groups[k]))}
            {archived.length > 0 && renderGroup("archived", archived, "已归档")}
            <div className={"s-lift-scrim" + (lifting ? " show" : "")} onClick={closeMenu} />
          </div>
          <div className="s-foot">
            <button className="newbtn" onClick={onNew}><Icon name="plus" size={19} />新会话</button>
          </div>
        </div>
      </aside>
    </>
  );
}
