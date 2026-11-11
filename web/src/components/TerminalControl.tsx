import { useEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import { Icon } from "../icons";
import "./PanelActions.css";
import type { Engine, SessionControl } from "../protocol";
import {
  presentLegacyExternalControl,
  presentMissingSessionControl,
  presentSessionControl,
} from "../session-control-ui";

export type TerminalStatusAvailability = "online" | "syncing" | "offline";

interface Props {
  control?: SessionControl | null;
  engine: Engine;
  availability: TerminalStatusAvailability;
  legacyExternal?: boolean;
  legacyTakeoverPending?: boolean;
  legacyMessage?: string | null;
  onTakeover?: () => void;
}

interface CardPosition {
  top: number;
  right: number;
}

/** Match the live trigger's geometry while no actionable session is selected. */
export function TerminalControlPlaceholder({ reason }: { reason: string }) {
  return <button className="terminal-control-trigger" type="button" disabled
    aria-label={`终端状态：${reason}`} title={reason}>
    <Icon name="term" size={18} />
    <span className="terminal-control-dot" aria-hidden="true" />
  </button>;
}

export function TerminalControl({
  control,
  engine,
  availability,
  legacyExternal = false,
  legacyTakeoverPending = false,
  legacyMessage,
  onTakeover,
}: Props) {
  const buttonRef = useRef<HTMLButtonElement>(null);
  const cardRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<CardPosition>({ top: 58, right: 10 });
  const reportedUi = control
    ? presentSessionControl(control)
    : legacyExternal
      ? presentLegacyExternalControl(engine, legacyTakeoverPending, legacyMessage)
    : presentMissingSessionControl(engine);
  const ui = availability === "online" ? reportedUi : {
    ...reportedUi,
    title: availability === "offline" ? "连接中断" : "终端状态待确认",
    detail: availability === "offline"
      ? `Remote 当前离线。上次报告：${reportedUi.title}；重连后会自动核对。`
      : `正在同步当前会话状态。上次报告：${reportedUi.title}。`,
    tone: availability === "offline" ? "disconnected" as const : "attention" as const,
    backend: availability === "offline" ? "状态不可用" : "正在同步",
    terminal: `上次：${reportedUi.terminal}`,
    remote: availability === "offline" ? "暂不可用" : "等待同步",
  };
  const canTakeover = availability === "online" && (
    (control?.control_mode === "external_cli" && control.can_takeover === true)
    || (!control && legacyExternal)
  );

  const toggle = () => {
    if (open) {
      setOpen(false);
      return;
    }
    const rect = buttonRef.current?.getBoundingClientRect();
    if (rect) {
      setPosition({
        top: Math.min(rect.bottom + 8, window.innerHeight - 24),
        right: Math.max(10, window.innerWidth - rect.right),
      });
    }
    setOpen(true);
  };

  useEffect(() => {
    if (!open) return;
    const trigger = buttonRef.current;
    const close = () => setOpen(false);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(cardRef.current?.querySelectorAll<HTMLElement>(
        'a[href],button:not([disabled]),[tabindex]:not([tabindex="-1"])',
      ) ?? []).filter((element) => !element.hasAttribute("hidden"));
      if (focusable.length === 0) {
        event.preventDefault();
        cardRef.current?.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first
          || !cardRef.current?.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    const focusFrame = window.requestAnimationFrame(() => closeRef.current?.focus());
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", close);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", close);
      if (trigger && document.contains(trigger)) trigger.focus();
    };
  }, [open]);

  const cardStyle = {
    "--terminal-card-top": `${position.top}px`,
    "--terminal-card-right": `${position.right}px`,
  } as CSSProperties;

  return (
    <>
      <button ref={buttonRef}
        className={`terminal-control-trigger tone-${ui.tone}`}
        type="button" onClick={toggle}
        aria-label={`终端状态：${ui.title}`}
        aria-expanded={open} aria-haspopup="dialog" title={ui.title}>
        <Icon name="term" size={18} />
        <span className="terminal-control-dot" aria-hidden="true" />
      </button>
      {open && typeof document !== "undefined" && createPortal(
        <div className="terminal-control-scrim" onClick={() => setOpen(false)}>
          <section ref={cardRef} className={`terminal-control-card tone-${ui.tone}`}
            style={cardStyle} role="dialog" aria-modal="true"
            aria-label="终端连接状态" tabIndex={-1}
            onClick={(event) => event.stopPropagation()}>
            <header>
              <Icon name="term" size={19} />
              <b>终端状态</b>
              <button ref={closeRef} type="button" className="iconbtn panel-action terminal-control-close"
                onClick={() => setOpen(false)} aria-label="关闭终端状态">
                <Icon name="chevrons-right" size={18} />
              </button>
            </header>
            <div className="terminal-control-summary"><b><i aria-hidden="true" />{ui.title}</b><p>{ui.detail}</p></div>
            <dl>
              <div><dt>引擎</dt><dd>{engine === "codex" ? "Codex" : "Claude"}</dd></div>
              <div><dt>连接方式</dt><dd>{ui.connection}</dd></div>
              <div><dt>共享后台</dt><dd>{ui.backend}</dd></div>
              <div><dt>本机终端</dt><dd>{ui.terminal}</dd></div>
              <div><dt>Remote 写入</dt><dd>{ui.remote}</dd></div>
            </dl>
            {control?.reason?.trim() && (
              <div className="terminal-control-reason">
                <span>{availability === "online" ? "状态原因" : "上次状态原因"}</span>
                <code>{control.reason.trim()}</code>
              </div>
            )}
            {canTakeover && (
              <button type="button" className="terminal-control-takeover"
                onClick={() => onTakeover?.()} disabled={ui.pending}>
                {ui.pending ? "等待接管" : "接管到 Remote"}
              </button>
            )}
          </section>
        </div>,
        document.body,
      )}
    </>
  );
}
