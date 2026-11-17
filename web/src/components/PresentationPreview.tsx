/*! PPTX renderer license notices: /licenses/pptx-renderer.txt */
import { useContext, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Icon } from "../icons";
import { useAnchoredPopoverGeometry } from "../chat-dialog-geometry";
import { PanelVisible } from "../right-panel-presentation";
import type { PresentationState } from "../presentation-preview";
import "./PresentationPreview.css";

const INITIAL_STATE: PresentationState = { type: "state", pageNumber: 1,
  pageCount: 0, zoom: 100, busy: true, partial: false, error: null };

const ZOOM_OPTIONS = [100, 125, 150, 200, 300];
const zoomLabel = (value: number) => value === 100 ? "适应宽度" : `${value}%`;

function PresentationZoom({ value, disabled, onChange }: {
  value: number; disabled: boolean; onChange: (value: number) => void;
}) {
  const visible = useContext(PanelVisible);
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const id = useId();
  const expanded = open && visible && !disabled;
  const position = useAnchoredPopoverGeometry({
    open: expanded, anchorRef: trigger, maxWidth: 200,
    maxHeight: 260, minimumHeight: 180, gutter: 10, viewport: true,
  });
  const shown = expanded && position !== null;

  useEffect(() => {
    if (!visible || disabled) setOpen(false);
  }, [visible, disabled]);

  useEffect(() => {
    if (!shown) return;
    menu.current?.querySelector<HTMLButtonElement>('[aria-selected="true"]')
      ?.focus({ preventScroll: true });
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !menu.current?.contains(event.target)
          && !trigger.current?.contains(event.target)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented || event.isComposing) return;
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
      trigger.current?.focus({ preventScroll: true });
    };
    // Pointer events inside the sandbox cannot bubble to this document.
    const blur = () => setOpen(false);
    document.addEventListener("pointerdown", outside, true);
    document.addEventListener("keydown", escape, true);
    window.addEventListener("blur", blur);
    return () => {
      document.removeEventListener("pointerdown", outside, true);
      document.removeEventListener("keydown", escape, true);
      window.removeEventListener("blur", blur);
    };
  }, [shown]);

  return <>
    <button ref={trigger} type="button" className="presentation-zoom-trigger"
      role="combobox" aria-label="幻灯片缩放" aria-haspopup="listbox"
      aria-expanded={expanded} aria-controls={expanded ? id : undefined}
      disabled={disabled} onClick={() => setOpen(current => !current)}
      onKeyDown={event => {
        if (event.key === "ArrowUp" || event.key === "ArrowDown") {
          event.preventDefault();
          event.stopPropagation();
          setOpen(true);
        }
      }}>
      <span>{zoomLabel(value)}</span><Icon name="chev" size={14} />
    </button>
    {shown && createPortal(<div ref={menu} id={id} role="listbox" aria-label="幻灯片缩放选项"
      className={`presentation-zoom-menu place-${position.placement}`}
      style={{ left: position.left, top: position.top, width: position.width, maxHeight: position.maxHeight }}
      onKeyDown={event => {
        if (event.key === "Tab") {
          setOpen(false);
          trigger.current?.focus({ preventScroll: true });
          return;
        }
        if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        event.stopPropagation();
        const options = [...menu.current!.querySelectorAll<HTMLButtonElement>('[role="option"]')];
        const current = options.indexOf(document.activeElement as HTMLButtonElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1
          : (current + (event.key === "ArrowUp" ? -1 : 1) + options.length) % options.length;
        options[next]?.focus();
      }}>
      {ZOOM_OPTIONS.map(option => <button key={option} type="button" role="option"
        aria-selected={value === option} tabIndex={value === option ? 0 : -1}
        onClick={() => {
          onChange(option);
          setOpen(false);
          trigger.current?.focus({ preventScroll: true });
        }}>
        <span>{zoomLabel(option)}</span>{value === option && <Icon name="check" size={16} />}
      </button>)}
    </div>, document.body)}
  </>;
}

export default function PresentationPreview({ data, title }: { data?: string; title: string }) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const portRef = useRef<MessagePort | null>(null);
  const readyRef = useRef(false);
  const [mobile, setMobile] = useState(() => window.matchMedia("(max-width: 700px)").matches);
  const [state, setState] = useState(INITIAL_STATE);
  const [showThumbnails, setShowThumbnails] = useState(false);

  useEffect(() => {
    const query = window.matchMedia("(max-width: 700px)");
    const change = () => setMobile(query.matches);
    query.addEventListener("change", change);
    return () => query.removeEventListener("change", change);
  }, []);

  useEffect(() => {
    const frame = frameRef.current;
    if (!frame) return;
    let disposed = false;
    let channel: MessageChannel | null = null;
    setState(INITIAL_STATE);
    setShowThumbnails(false);
    const open = () => {
      if (disposed || channel || !frame.contentWindow) return;
      try {
        if (!data) throw new Error("Missing PPTX data");
        const binary = atob(data);
        const bytes = new Uint8Array(binary.length);
        for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
        channel = new MessageChannel();
        portRef.current = channel.port1;
        channel.port1.onmessage = event => {
          if (disposed) return;
          const next = event.data;
          if (next?.type === "escape") {
            frame.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
          } else if (next?.type === "state" && Number.isInteger(next.pageCount)
              && next.pageCount >= 0 && next.pageCount <= 4000
              && Number.isInteger(next.pageNumber) && next.pageNumber >= 1
              && next.pageNumber <= Math.max(1, next.pageCount)
              && [100, 125, 150, 200, 300].includes(next.zoom)
              && typeof next.busy === "boolean" && typeof next.partial === "boolean"
              && (next.error === null || typeof next.error === "string" && next.error.length <= 512)) {
            setState(next);
          }
        };
        frame.contentWindow.postMessage({ type: "cc-remote-presentation", bytes: bytes.buffer,
          mobile: window.matchMedia("(max-width: 700px)").matches,
          accent: getComputedStyle(frame).getPropertyValue("--accent").trim() },
        "*", [channel.port2, bytes.buffer]);
      } catch {
        setState({ ...INITIAL_STATE, busy: false, error: "PPTX 预览加载失败" });
      }
    };
    const onReady = (event: MessageEvent) => {
      if (event.source !== frame.contentWindow || event.origin !== "null"
          || event.data?.type !== "cc-remote-presentation-ready") return;
      readyRef.current = true;
      open();
    };
    window.addEventListener("message", onReady);
    if (readyRef.current) open();
    return () => {
      disposed = true;
      window.removeEventListener("message", onReady);
      channel?.port1.postMessage({ type: "dispose" });
      channel?.port1.close();
      if (portRef.current === channel?.port1) portRef.current = null;
    };
  }, [data]);

  useEffect(() => {
    if (state.pageCount) portRef.current?.postMessage({ type: "mobile", value: mobile });
  }, [mobile, state.pageCount]);

  const ready = state.pageCount > 0;
  return <section className={`presentation-preview${mobile ? " mobile" : ""}`} aria-label={`${title} 幻灯片预览`}>
    <nav className="presentation-controls" aria-label="幻灯片操作">
      <button type="button" disabled={!ready} aria-pressed={showThumbnails}
        onClick={() => {
          portRef.current?.postMessage({ type: "thumbnails", visible: !showThumbnails });
          setShowThumbnails(value => !value);
        }}>缩略图</button>
      <div className="presentation-pages">
        {!mobile && <button type="button" aria-label="上一页" title="上一页" disabled={!ready || state.pageNumber <= 1}
          onClick={() => portRef.current?.postMessage({ type: "page", index: state.pageNumber - 2 })}>‹</button>}
        <span aria-live="polite">{ready ? `${state.pageNumber} / ${state.pageCount}` : "加载中…"}</span>
        {!mobile && <button type="button" aria-label="下一页" title="下一页" disabled={!ready || state.pageNumber >= state.pageCount}
          onClick={() => portRef.current?.postMessage({ type: "page", index: state.pageNumber })}>›</button>}
      </div>
      <PresentationZoom value={state.zoom} disabled={!ready}
        onChange={value => portRef.current?.postMessage({ type: "zoom", value })} />
    </nav>
    <div className="presentation-stage">
      <iframe ref={frameRef} src="/presentation-preview.html" title={`${title} 幻灯片`}
        sandbox="allow-scripts" referrerPolicy="no-referrer" />
      {state.error ? <div className="presentation-error preview-error" role="alert"><Icon name="read" size={18} />{state.error}</div>
        : state.busy && <div className="presentation-loading" role="status"><span className="thinking"><span/><span/><span/></span> 正在渲染幻灯片…</div>}
    </div>
    {state.partial && !state.error && <div className="presentation-notice" role="status">部分元素未能显示，可下载原文件查看。</div>}
  </section>;
}
