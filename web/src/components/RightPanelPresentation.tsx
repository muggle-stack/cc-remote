import { createContext, useContext, useEffect, useRef, type HTMLAttributes } from "react";
import { Icon } from "../icons";
import { RightPanelPresentation } from "../right-panel-presentation";
import "./RightPanelPresentation.css";
import "./PanelActions.css";

const InsidePanel = createContext(false);
const focusable = (panel: HTMLElement) => [...panel.querySelectorAll<HTMLElement>(
  'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), summary, a[href], iframe, [tabindex="0"]',
)].filter(node => !node.hasAttribute("data-panel-focus-guard") && node.getClientRects().length > 0);

/** Change presentation in place: editors, media and chat history stay mounted. */
export function PanelFrame({ children, className = "", label, ...props }:
  HTMLAttributes<HTMLDivElement> & { label: string }) {
  const presentation = useContext(RightPanelPresentation);
  const nested = useContext(InsidePanel);
  const expanded = presentation.expanded && !nested;
  const panelRef = useRef<HTMLDivElement>(null);
  const dismissRef = useRef(presentation.onDismiss);
  dismissRef.current = presentation.onDismiss;

  const openChatRef = useRef(presentation.onOpenChat);
  openChatRef.current = presentation.onOpenChat;
  useEffect(() => {
    if (nested) return;
    const onFrameChat = (event: MessageEvent) => {
      const panel = panelRef.current;
      const frame = document.activeElement;
      if (event.data?.type !== "cc-remote-preview-chat" || !panel || panel.inert
          || !(frame instanceof HTMLIFrameElement) || !panel.contains(frame)
          || event.source !== frame.contentWindow) return;
      const origin = frame.sandbox.contains("allow-same-origin")
        ? new URL(frame.src, location.href).origin : "null";
      if (event.origin !== origin) return;
      if ([...document.querySelectorAll<HTMLElement>('[role="dialog"], dialog[open], [role="menu"]')]
          .some(node => node !== panel && !node.closest("[inert]") && node.getClientRects().length)) return;
      openChatRef.current();
    };
    window.addEventListener("message", onFrameChat);
    return () => window.removeEventListener("message", onFrameChat);
  }, [nested]);

  useEffect(() => {
    const panel = panelRef.current;
    if (!expanded || !panel) return;
    const previous = document.activeElement as HTMLElement | null;
    if (!panel.inert && !panel.contains(previous)) panel.focus({ preventScroll: true });
    const hasNestedDialog = (modalOnly = false) => [...document.querySelectorAll<HTMLElement>(modalOnly
      ? '[role="dialog"][aria-modal="true"], dialog:modal'
      : '[role="dialog"], dialog[open]')]
      .some(node => node !== panel && !node.closest("[inert]") && node.getClientRects().length);
    const onPreviewEscape = (event: MessageEvent) => {
      if (panel.inert || event.data?.type !== "cc-remote-preview-escape" || hasNestedDialog()) return;
      const frame = document.activeElement;
      // Preview messages are untrusted. Only the exact, currently focused frame
      // in this expanded panel may request docking; no other command is exposed.
      if (!(frame instanceof HTMLIFrameElement) || !panel.contains(frame)
          || event.source !== frame.contentWindow) return;
      const origin = frame.sandbox.contains("allow-same-origin")
        ? new URL(frame.src, location.href).origin : "null";
      if (event.origin !== origin) return;
      panel.focus({ preventScroll: true });
      dismissRef.current();
    };
    const onKey = (event: KeyboardEvent) => {
      if (panel.inert || event.defaultPrevented || event.isComposing || event.keyCode === 229) return;
      if (event.key === "Escape") {
        // Informational popovers also own Escape, but do not trap focus.
        if (hasNestedDialog()) return;
        event.preventDefault();
        event.stopPropagation();
        dismissRef.current();
      } else if (event.key === "Tab") {
        // Keep native traversal, but do not let App's Shift+Tab mode shortcut
        // consume it after this dialog's document-level handler returns.
        event.stopPropagation();
        // A real nested modal keeps its own focus navigation. Informational
        // role=dialog portals leave the expanded panel's boundary in charge.
        if (hasNestedDialog(true)) return;
        const nodes = focusable(panel);
        const current = nodes.indexOf(document.activeElement as HTMLElement);
        // Let native traversal enter iframe documents and visit their controls.
        // Focus guards catch exits whose keydown never reaches this document.
        if (current >= 0 && (event.shiftKey ? current > 0 : current < nodes.length - 1)) return;
        const next = current < 0 ? (event.shiftKey ? nodes.length - 1 : 0)
          : (current + (event.shiftKey ? -1 : 1) + nodes.length) % nodes.length;
        event.preventDefault();
        event.stopPropagation();
        (nodes[next] ?? panel).focus({ preventScroll: true });
      }
    };
    document.addEventListener("keydown", onKey);
    window.addEventListener("message", onPreviewEscape);
    return () => {
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("message", onPreviewEscape);
      if (!panel.inert && previous?.isConnected && !previous.closest("[inert]") && !panel.contains(previous)) previous.focus({ preventScroll: true });
    };
  }, [expanded]);

  const wrapFocus = (last: boolean) => {
    const panel = panelRef.current;
    if (!panel) return;
    const nodes = focusable(panel);
    (nodes[last ? nodes.length - 1 : 0] ?? panel).focus({ preventScroll: true });
  };

  return <InsidePanel.Provider value={true}>
    {expanded && <div className={`panel-expanded-backdrop${presentation.layer ? " panel-chat-layer" : ""}`} aria-hidden="true"
      inert={presentation.covered}
      data-lock-horizontal-swipe="true" onClick={presentation.onDismiss} />}
    <div {...props} ref={panelRef}
      className={`${className} right-panel-frame${expanded ? " panel-expanded" : ""}${expanded && presentation.layer ? " panel-chat-layer" : ""}`}
      inert={presentation.covered || undefined}
      role={expanded ? "dialog" : props.role}
      aria-modal={expanded && !presentation.covered || undefined} aria-label={label}
      tabIndex={expanded ? -1 : undefined} data-lock-horizontal-swipe="true">
      {expanded && <span className="panel-focus-guard" data-panel-focus-guard="" tabIndex={0}
        aria-hidden="true" onFocus={() => wrapFocus(true)} />}
      {children}
      {expanded && <span className="panel-focus-guard" data-panel-focus-guard="" tabIndex={0}
        aria-hidden="true" onFocus={() => wrapFocus(false)} />}
    </div>
  </InsidePanel.Provider>;
}

export function PanelExpandButton({ label }: { label: string }) {
  const { expanded, onExpand, onDock, chat } = useContext(RightPanelPresentation);
  return <button type="button" className="iconbtn panel-action panel-expand"
    onClick={expanded ? onDock : onExpand}
    aria-label={expanded ? "收回右侧" : `居中展开${label}`}
    title={expanded ? "收回右侧" : chat ? "居中展开 (⌘/Ctrl+Shift+E)" : "居中展开"}
    aria-pressed={expanded} aria-keyshortcuts={chat ? "Meta+Shift+E Control+Shift+E" : undefined}>
    <Icon name={expanded ? "panel-right" : "expand"} />
  </button>;
}
