import { useEffect, useId, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import "./HeaderFeedback.css";
import type { Notice } from "../protocol";
import {
  advanceConnectionFeedback, connectionFeedbackItem, headerBannerKind,
  headerNoticeItem, HEADER_SUCCESS_TTL_MS,
  type ConnectionPhase, type HeaderFeedbackItem,
} from "../header-feedback";

interface Props {
  update?: { id: string; version: string; message: string | null; updating: boolean; onReload: () => void };
  versionNotice?: string | null;
  versionBlocked?: boolean;
  engineConnectionError?: string | null;
  pageReload?: { onReload: () => void; updating: boolean; message: string | null };
  maintenance?: { id: string; label: string; message: string; tone: "info" | "warning" | "success" };
  onOpenUpdates?: () => void;
  deviceScope: string;
  sessionScope: string;
  phase: ConnectionPhase | null;
  banner?: string;
  bannerRevision?: number;
  notices: Notice[];
  onDismissBanner: (banner: string, revision: number) => void;
  onDismissNotice: (noticeId: string) => void;
}

function SuccessExpiry({ id, paused, onExpire }: {
  id: string; paused: boolean; onExpire: () => void;
}) {
  const callback = useRef(onExpire);
  useEffect(() => { callback.current = onExpire; }, [onExpire]);
  useEffect(() => {
    if (paused) return;
    const timer = window.setTimeout(() => callback.current(), HEADER_SUCCESS_TTL_MS);
    return () => window.clearTimeout(timer);
  }, [id, paused]);
  return null;
}

/** A presentation-only status slot; hiding it never changes engine/transport state. */
export function HeaderFeedback({
  deviceScope, sessionScope, phase, banner, bannerRevision = 0, notices,
  onDismissBanner, onDismissNotice, update, versionNotice, versionBlocked = false,
  engineConnectionError, pageReload, maintenance, onOpenUpdates,
}: Props) {
  const dialogId = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const card = useRef<HTMLElement>(null);
  const [connection, setConnection] = useState(() =>
    advanceConnectionFeedback(null, deviceScope, phase));
  const currentConnection = advanceConnectionFeedback(connection, deviceScope, phase);
  if (currentConnection !== connection) setConnection(currentConnection);
  // Retain a bounded, scoped receipt of dismissed events so a replay does not
  // reopen them. New notice IDs (and repeated commands' revisions) still show.
  const [dismissed, setDismissed] = useState<string[]>([]);
  const [commandFeedback, setCommandFeedback] = useState<Array<HeaderFeedbackItem & {
    deviceScope: string; sessionScope: string;
  }>>([]);
  const [openFor, setOpenFor] = useState<string | null>(null);
  const scope = JSON.stringify([deviceScope, sessionScope]);
  const [position, setPosition] = useState({ top: 60, right: 12 });
  const [engineOutage, setEngineOutage] = useState<string | null>(null);
  const engineOutageScope = engineConnectionError ? scope : null;
  useEffect(() => {
    setEngineOutage(null);
    if (!engineOutageScope) return;
    const timer = window.setTimeout(() => setEngineOutage(engineOutageScope), 8_000);
    return () => window.clearTimeout(timer);
  }, [engineOutageScope]);
  const connectionItem = connectionFeedbackItem(currentConnection);
  const updateItem: HeaderFeedbackItem | null = update ? {
    id: `web-update:${update.id}`, label: "有更新", tone: "info",
    message: `新版 ${update.version} 已就绪，更新页面即可使用，无需退出登录。`,
    detail: update.message ?? "更新会重新加载页面，设备上已开始的任务会继续运行。",
  } : null;
  const versionItem: HeaderFeedbackItem | null = versionNotice ? {
    id: JSON.stringify([deviceScope, "device-version", versionNotice]),
    label: versionBlocked ? "版本不兼容" : "版本待同步", tone: "warning", message: versionNotice,
  } : null;
  const engineItem: HeaderFeedbackItem | null = engineConnectionError && engineOutage === scope ? {
    id: JSON.stringify([scope, "engine-connection"]), label: "连接未恢复", tone: "warning",
    message: "DSH 连接尚未恢复，正在自动重连。", detail: engineConnectionError,
  } : null;
  const maintenanceItem: HeaderFeedbackItem | null = maintenance ? {
    ...maintenance, id: JSON.stringify([deviceScope, maintenance.id]),
  } : null;
  const items = notices.map(notice => headerNoticeItem(notice, scope));
  let commandItem = commandFeedback.find(item => item.deviceScope === deviceScope);
  if (banner && headerBannerKind(banner) === "failure") {
    const id = JSON.stringify([deviceScope, "banner", bannerRevision]);
    if (commandItem?.id !== id) {
      commandItem = {
        id, deviceScope, sessionScope, label: "操作未完成", tone: "warning", message: banner, banner, bannerRevision,
      };
      // Connection frames overwrite the legacy banner string. Keep the latest
      // command outcome available until acknowledged, independently of transport.
      setCommandFeedback([...commandFeedback.filter(item => item.deviceScope !== deviceScope), commandItem].slice(-16));
    }
  }
  if (commandItem?.sessionScope === sessionScope) items.push(commandItem);
  if (updateItem) items.push(updateItem);
  if (maintenanceItem) items.push(maintenanceItem);
  if (versionBlocked && versionItem) items.push(versionItem);
  const visible = items.filter(item => !dismissed.includes(item.id)).reverse();
  if (connectionItem) visible.push(connectionItem);
  if (engineItem) visible.push(engineItem);
  // Compatible version drift stays available without masking connection or
  // command feedback, and disappears as soon as the device versions align.
  const selected = visible.find(item => item.tone === "warning")
    ?? visible.find(item => item.tone === "info") ?? connectionItem ?? visible[0] ?? versionItem;
  if (versionItem && !versionBlocked) visible.push(versionItem);
  const hasFeedback = !!selected;
  const open = openFor === scope && !!selected;

  useEffect(() => { setOpenFor(null); }, [scope, hasFeedback]);

  const dismiss = (item: HeaderFeedbackItem) => {
    if (item.id === versionItem?.id || item.id === engineItem?.id) {
      setOpenFor(null);
      return;
    }
    if (item.id === connectionItem?.id) {
      setConnection(previous => connectionFeedbackItem(previous)?.id === item.id
        ? { ...previous, dismissed: true, recovered: false } : previous);
    } else {
      setDismissed(previous => [...previous.filter(id => id !== item.id), item.id].slice(-256));
      if (item.noticeId) onDismissNotice(item.noticeId);
      if (item.banner) onDismissBanner(item.banner, item.bannerRevision ?? 0);
    }
    if (visible.length === 1) setOpenFor(null);
  };

  useEffect(() => {
    if (!open) return;
    const button = trigger.current;
    const panel = card.current;
    const close = () => setOpenFor(null);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        close();
        button?.focus();
      } else if (event.key === "Tab") {
        const buttons = Array.from(panel?.querySelectorAll<HTMLButtonElement>("button") ?? []);
        const first = buttons[0];
        const last = buttons.at(-1);
        if (event.shiftKey && (document.activeElement === first || document.activeElement === panel
            || !panel?.contains(document.activeElement))) {
          event.preventDefault(); last?.focus();
        } else if (!event.shiftKey && (document.activeElement === last
            || !panel?.contains(document.activeElement))) {
          event.preventDefault(); first?.focus();
        }
      }
    };
    panel?.focus();
    window.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", close);
    return () => {
      window.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", close);
      if (panel?.contains(document.activeElement) || document.activeElement === document.body) button?.focus();
    };
  }, [open]);

  const toggle = () => {
    if (open) { setOpenFor(null); return; }
    const rect = trigger.current?.getBoundingClientRect();
    if (rect) setPosition({
      top: rect.bottom + 8,
      right: Math.max(12, Math.min(window.innerWidth - rect.right,
        window.innerWidth - Math.min(340, window.innerWidth - 24) - 12)),
    });
    setOpenFor(scope);
  };

  return <div className="header-feedback-slot">
    {selected && <>
      <button ref={trigger} type="button" className={`header-feedback tone-${selected.tone}`}
        onClick={selected.id === maintenanceItem?.id && onOpenUpdates ? onOpenUpdates : toggle} aria-label={`状态详情：${selected.label}`}
        aria-expanded={open} aria-haspopup="dialog" aria-controls={open ? dialogId : undefined}>
        <span key={selected.label} className="header-feedback-label" role="status" aria-live="polite">
          <span className="header-feedback-mark" aria-hidden="true">{selected.tone === "success" ? "✓" : ""}</span>
          <span className="header-feedback-text">{selected.label}</span>
        </span>
      </button>
      {visible.filter(item => item.tone === "success").map(item =>
        <SuccessExpiry key={item.id} id={item.id} paused={open} onExpire={() => dismiss(item)} />)}
      {open && createPortal(<div className="header-feedback-scrim" onClick={() => setOpenFor(null)}>
        <section ref={card} id={dialogId} className="header-feedback-popover" tabIndex={-1}
          role="dialog" aria-modal="true" aria-label="状态详情"
          style={{ "--feedback-top": `${position.top}px`, "--feedback-right": `${position.right}px` } as CSSProperties}
          onClick={event => event.stopPropagation()}>
          {visible.map(item => <div key={item.id} className={`header-feedback-detail tone-${item.tone}`}>
            <h2>{item.label}</h2>
            <p>{item.message}</p>
            {item.detail && <p className="header-feedback-extra">{item.detail}</p>}
            {item.banner && /(?:版本.*刷新|刷新.*版本)/.test(item.banner) && pageReload && <>
              {pageReload.message && <p>{pageReload.message}</p>}
              <button type="button" className="header-feedback-dismiss"
                disabled={pageReload.updating} onClick={pageReload.onReload}>
                {pageReload.updating ? "检查中…" : "刷新页面"}
              </button>
            </>}
            {onOpenUpdates && [updateItem?.id, versionItem?.id, maintenanceItem?.id].includes(item.id)
              ? <button type="button" className="header-feedback-dismiss" onClick={() => {
                setOpenFor(null); onOpenUpdates();
              }}>查看更新</button>
              : item.id === updateItem?.id && <button type="button" className="header-feedback-dismiss"
              disabled={update?.updating} onClick={update?.onReload}>
              {update?.updating ? "检查中…" : "更新页面"}
            </button>}
            <button type="button" className="header-feedback-dismiss" onClick={() => dismiss(item)}>
              {item.id === versionItem?.id || item.id === engineItem?.id ? "收起详情" : item.id === updateItem?.id ? "稍后"
                : item.id === connectionItem?.id && phase ? "收起提示" : "知道了"}
            </button>
          </div>)}
        </section>
      </div>, document.body)}
    </>}
  </div>;
}
