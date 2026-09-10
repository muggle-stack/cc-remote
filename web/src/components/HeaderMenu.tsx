import { useEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import { Icon } from "../icons";
import type { NotificationMode } from "../notification-mode";
import type { PushBindingState } from "../push";

interface Props {
  engine: "claude" | "codex" | "dsh";
  theme: "light" | "dark";
  notificationMode: NotificationMode;
  notificationBinding: PushBindingState;
  notificationAvailable: boolean;
  onNotificationMode: (mode: NotificationMode) => Promise<boolean>;
  onOpenUsageActivity: () => void;
  onOpenViewer?: () => void;
  onOpenFiles?: () => void;
  onToggleTheme: () => void;
  onLogout: () => void;
}

interface Position {
  top: number;
  right: number;
}

const MODE_LABELS: Record<NotificationMode, string> = {
  off: "关闭",
  generic: "通用提醒",
  session: "显示会话名称",
};

export function HeaderMenu({
  engine,
  theme,
  notificationMode,
  notificationBinding,
  notificationAvailable,
  onNotificationMode,
  onOpenUsageActivity,
  onOpenViewer,
  onOpenFiles,
  onToggleTheme,
  onLogout,
}: Props) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const cardRef = useRef<HTMLElement>(null);
  const firstRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState<"main" | "notifications">("main");
  const [changingMode, setChangingMode] = useState(false);
  const [position, setPosition] = useState<Position>({ top: 58, right: 8 });

  const close = () => setOpen(false);
  const show = () => {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (rect) {
      setPosition({
        top: Math.min(rect.bottom + 8, window.innerHeight - 24),
        right: Math.max(8, window.innerWidth - rect.right),
      });
    }
    setPage("main");
    setOpen(true);
  };

  useEffect(() => {
    if (!open) return;
    const trigger = triggerRef.current;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(cardRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]),a[href],[tabindex]:not([tabindex="-1"])',
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
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", close);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", close);
      if (trigger && document.contains(trigger)) trigger.focus();
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const focusFrame = window.requestAnimationFrame(() => firstRef.current?.focus());
    return () => window.cancelAnimationFrame(focusFrame);
  }, [open, page]);

  const selectMode = async (mode: NotificationMode) => {
    if (changingMode || mode === notificationMode) return;
    setChangingMode(true);
    try {
      await onNotificationMode(mode);
    } finally {
      setChangingMode(false);
    }
  };

  const style = {
    "--header-menu-top": `${position.top}px`,
    "--header-menu-right": `${position.right}px`,
  } as CSSProperties;
  const bindingLabel = notificationMode === "off"
    ? "已关闭"
    : notificationBinding === "remote"
      ? "后台 Push"
      : notificationBinding === "binding"
        ? "正在同步"
        : "页面后台提醒";

  return (
    <>
      <button ref={triggerRef} type="button" className="iconbtn header-menu-trigger"
        data-lock-horizontal-swipe onClick={() => open ? close() : show()}
        aria-label="更多设置" aria-haspopup="dialog" aria-expanded={open}>
        <Icon name="dots" />
      </button>
      {open && typeof document !== "undefined" && createPortal(
        <div className="header-menu-scrim" data-lock-horizontal-swipe
          onClick={(event) => {
            if (event.target === event.currentTarget) close();
          }}>
          <section ref={cardRef} className="header-menu-card" style={style}
            role="dialog" aria-modal="true" aria-label="应用设置" tabIndex={-1}
            onPointerDown={(event) => event.stopPropagation()}>
            {page === "main" ? (
              <>
                <header className="header-menu-title">
                  <b>设置</b>
                </header>
                <div className="header-menu-items">
                  {onOpenViewer && <button ref={firstRef} type="button" className="header-menu-item" onClick={() => {
                    close();
                    onOpenViewer();
                  }}>
                    <Icon name="globe" size={18} />
                    <span><b>远程预览</b><small>打开设备上的交互页面</small></span>
                    <Icon name="chevron-right" size={16} />
                  </button>}
                  {onOpenFiles && <button ref={onOpenViewer ? undefined : firstRef} type="button"
                    className="header-menu-item" aria-label="打开会话文件" onClick={() => {
                      close();
                      onOpenFiles();
                    }}>
                    <Icon name="folder-open" size={18} />
                    <span><b>会话文件</b><small>浏览目录并预览文件</small></span>
                    <Icon name="chevron-right" size={16} />
                  </button>}
                  {engine === "codex" && <button ref={onOpenViewer || onOpenFiles ? undefined : firstRef} type="button"
                    className="header-menu-item" onClick={() => {
                      close();
                      onOpenUsageActivity();
                    }}>
                    <Icon name="calendar" size={18} />
                    <span><b>使用活动</b><small>每日 Token、峰值与连续使用记录</small></span>
                    <Icon name="chevron-right" size={16} />
                  </button>}
                  <button ref={engine === "codex" || onOpenViewer || onOpenFiles ? undefined : firstRef}
                    type="button" className="header-menu-item"
                    onClick={() => setPage("notifications")}>
                    <Icon name="notify" size={18} />
                    <span><b>通知</b><small>{MODE_LABELS[notificationMode]} · {bindingLabel}</small></span>
                    <Icon name="chevron-right" size={16} />
                  </button>
                  <button type="button" className="header-menu-item"
                    onClick={onToggleTheme}>
                    <Icon name={theme === "dark" ? "sun" : "moon"} size={18} />
                    <span><b>主题</b><small>{theme === "dark" ? "深色" : "浅色"}</small></span>
                  </button>
                  <button type="button" className="header-menu-item danger"
                    onClick={onLogout}>
                    <Icon name="logout" size={18} />
                    <span><b>退出登录</b><small>清除本机缓存并断开连接</small></span>
                  </button>
                </div>
              </>
            ) : (
              <>
                <header className="header-menu-title">
                  <button ref={firstRef} type="button" className="header-menu-back"
                    onClick={() => setPage("main")} aria-label="返回设置">
                    <Icon name="back" size={17} />
                  </button>
                  <b>完成通知</b>
                </header>
                <p className="header-menu-help">
                  通用提醒不会把会话名称或编号发送给 Push 服务；会话提醒可从通知精确打开对应对话。
                </p>
                {!notificationAvailable && (
                  <p className="header-menu-warning">当前浏览器不支持系统通知。</p>
                )}
                <div className="header-menu-modes" role="radiogroup" aria-label="通知隐私模式">
                  {(["off", "generic", "session"] as const).map((mode) => (
                    <button key={mode} type="button" role="radio"
                      aria-checked={notificationMode === mode}
                      disabled={changingMode || (!notificationAvailable && mode !== "off")}
                      onClick={() => void selectMode(mode)}>
                      <span className="header-menu-radio" aria-hidden="true" />
                      <span>
                        <b>{MODE_LABELS[mode]}</b>
                        <small>{mode === "off"
                          ? "不发送系统通知"
                          : mode === "generic"
                            ? "仅显示完成、失败或中断"
                            : "显示安全截断的会话名称，并支持精确跳转"}</small>
                      </span>
                    </button>
                  ))}
                </div>
              </>
            )}
          </section>
        </div>,
        document.body,
      )}
    </>
  );
}
