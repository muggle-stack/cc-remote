import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { TimedTaskInfo } from "../protocol";
import { Icon } from "../icons";
import "./timed-task.css";

function remaining(seconds: number) {
  if (seconds <= 0) return "正在发送";
  if (seconds < 60) return `约 ${Math.ceil(seconds)} 秒后`;
  if (seconds < 3600) return `约 ${Math.ceil(seconds / 60)} 分钟后`;
  return `约 ${Math.ceil(seconds / 3600)} 小时后`;
}

function intervalLabel(seconds: number) {
  if (seconds % 3600 === 0) return `每 ${seconds / 3600} 小时`;
  if (seconds % 60 === 0) return `每 ${seconds / 60} 分钟`;
  return `每 ${seconds} 秒`;
}

/** A task clock is independent of model activity and completion receipts. */
export function TimedTaskIndicator({ tasks, hidden = false }: {
  tasks: TimedTaskInfo[]; hidden?: boolean;
}) {
  const buttonRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const [open, setOpen] = useState(false);
  const [now, setNow] = useState(() => Date.now() / 1000);
  const id = useId();
  const active = tasks.filter(task => task.valid_until > now
    && task.sent_count < task.total_count).sort((a, b) => a.next_message_at - b.next_message_at);
  const task = active[0];
  const hasTask = !!task;
  const visible = open && !hidden && !!task;
  useEffect(() => {
    if (!hasTask) return;
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, [hasTask]);
  useEffect(() => {
    if (hidden) setOpen(false);
  }, [hidden]);
  useEffect(() => {
    const button = buttonRef.current;
    const card = button?.closest(".scard");
    if (!card) return;
    const show = () => { clearTimeout(closeTimer.current); setOpen(true); };
    const hide = () => { closeTimer.current = setTimeout(() => setOpen(false), 160); };
    const focus = (event: Event) => { if (event.target === button) show(); };
    const dismiss = (event: PointerEvent) => {
      if (!card.contains(event.target as Node) && !panelRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    card.addEventListener("mouseenter", show);
    card.addEventListener("mouseleave", hide);
    card.addEventListener("focusin", focus);
    card.addEventListener("focusout", hide);
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", escape);
    return () => {
      clearTimeout(closeTimer.current);
      card.removeEventListener("mouseenter", show);
      card.removeEventListener("mouseleave", hide);
      card.removeEventListener("focusin", focus);
      card.removeEventListener("focusout", hide);
      document.removeEventListener("pointerdown", dismiss);
      document.removeEventListener("keydown", escape);
    };
  }, [hasTask]);
  useLayoutEffect(() => {
    const trigger = buttonRef.current;
    const card = trigger?.closest<HTMLElement>(".scard");
    const panel = panelRef.current;
    if (!visible || !card || !panel) return;
    const viewport = window.visualViewport;
    const position = () => {
      const box = card.getBoundingClientRect();
      const scroll = card.closest(".s-scroll")!.getBoundingClientRect();
      if (box.bottom <= scroll.top || box.top >= scroll.bottom) {
        setOpen(false);
        return;
      }
      const left = (viewport?.offsetLeft ?? 0) + 8;
      const top = (viewport?.offsetTop ?? 0) + 8;
      const right = left + (viewport?.width ?? window.innerWidth) - 16;
      const footer = card.closest(".sessions")?.querySelector(".s-foot")?.getBoundingClientRect();
      const bottom = Math.min(top + (viewport?.height ?? window.innerHeight) - 16,
        footer?.top ?? Infinity) - 8;
      panel.style.maxWidth = `${Math.max(0, right - left)}px`;
      panel.style.maxHeight = `${Math.max(0, bottom - top)}px`;
      const size = panel.getBoundingClientRect();
      const beside = box.right + 10 + size.width <= right;
      panel.style.left = `${beside ? box.right + 10 : Math.max(left, Math.min(box.right - size.width, right - size.width))}px`;
      const below = box.bottom + 8 + size.height <= bottom;
      panel.style.top = `${Math.max(top, Math.min(beside ? box.top : below ? box.bottom + 8 : box.top - size.height - 8, bottom - size.height))}px`;
      panel.dataset.placement = beside ? "right" : below ? "below" : "above";
      panel.style.visibility = "visible";
    };
    position();
    const observer = new ResizeObserver(position);
    observer.observe(card);
    observer.observe(panel);
    window.addEventListener("resize", position);
    window.addEventListener("scroll", position, true);
    viewport?.addEventListener("resize", position);
    viewport?.addEventListener("scroll", position);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", position);
      window.removeEventListener("scroll", position, true);
      viewport?.removeEventListener("resize", position);
      viewport?.removeEventListener("scroll", position);
    };
  }, [visible]);
  if (!task) return null;
  return <>
    <span className="timed-task-orbit" aria-hidden="true" />
    <button ref={buttonRef} className="timed-task-trigger" aria-label="查看定时任务"
      aria-expanded={visible} aria-controls={visible ? id : undefined}
      onTouchStart={event => event.stopPropagation()}
      onClick={event => { event.stopPropagation(); setOpen(true); }}>
      <Icon name="clock" size={14} />
    </button>
    {visible && typeof document !== "undefined" && createPortal(
      <div id={id} ref={panelRef} className="timed-task-popover" role="tooltip"
        onMouseEnter={() => { clearTimeout(closeTimer.current); setOpen(true); }} onMouseLeave={() => setOpen(false)}
        onClick={event => event.stopPropagation()}>
        <div className="timed-task-heading"><Icon name="clock" size={17} />定时任务仍在处理</div>
        <div className="timed-task-title">{task.title}{active.length > 1 && <span> · 共 {active.length} 项</span>}</div>
        <div className="timed-task-next"><span>下一条消息</span>
          <strong>{new Date(task.next_message_at * 1000).toLocaleTimeString([], { hour12: false })}</strong></div>
        <div className="timed-task-countdown">{remaining(task.next_message_at - now)}</div>
        <div className="timed-task-progress">{task.total_count > 1 ? `${intervalLabel(task.interval_seconds)} · ` : ""}
          已发送 {task.sent_count}/{task.total_count} 次</div>
      </div>, document.body)}
  </>;
}
