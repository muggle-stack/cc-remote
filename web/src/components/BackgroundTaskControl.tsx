import {
  useCallback, useEffect, useId, useRef, useState, useSyncExternalStore,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { useAnchoredPopoverGeometry } from "../chat-dialog-geometry";
import type { ProcessBlock } from "../domain/conversation";
import { Icon } from "../icons";
import { CenteredSheet } from "./CenteredSheet";
import { ProcessActivity } from "./ProcessTimeline";
import "./BackgroundTaskControl.css";

interface Props {
  /** The runtime's authoritative active list; parent reply completion is unrelated. */
  processes: ProcessBlock[];
  onOpenFile?: (path: string, line?: number) => void;
  onOpenAgent?: (runId: string, title?: string) => void;
}

const MOBILE_QUERY = "(max-width: 600px)";
function subscribeMobile(listener: () => void) {
  const media = window.matchMedia(MOBILE_QUERY);
  media.addEventListener("change", listener);
  return () => media.removeEventListener("change", listener);
}
const isMobile = () => window.matchMedia(MOBILE_QUERY).matches;

function taskStatus(process: ProcessBlock, now: number): string {
  const status = process.status === "pending" ? "等待中"
    : process.status === "unknown" ? "状态待确认" : "运行中";
  if (!process.startedTs) return status;
  const seconds = Math.max(0, Math.floor((now - process.startedTs) / 1000));
  const duration = seconds < 60 ? `${seconds}秒`
    : seconds < 3600 ? `${Math.floor(seconds / 60)}分${seconds % 60}秒`
    : `${Math.floor(seconds / 3600)}小时${Math.floor(seconds / 60) % 60}分`;
  return `${status} · ${duration}`;
}

function TaskList({ processes, preview = false, onOpenFile, onOpenAgent }: Props & {
  preview?: boolean;
}) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const visible = preview ? processes.slice(0, 3) : processes;
  return <ul className={`background-task-list${preview ? " preview" : ""}`}>
    {visible.map(process => <li key={process.item_id}>
      {preview ? <strong>{process.title}</strong>
        : <ProcessActivity block={process}
            onOpenFile={onOpenFile} onOpenAgent={onOpenAgent} />}
      <span className="background-task-meta">{taskStatus(process, now)}</span>
    </li>)}
    {preview && processes.length > visible.length
      && <li className="background-task-more">还有 {processes.length - visible.length} 项 · 点击查看</li>}
  </ul>;
}

function TaskPopover({ anchor, id, preview, onClose, ...props }: Props & {
  anchor: RefObject<HTMLButtonElement | null>;
  id: string;
  preview: boolean;
  onClose: (restoreFocus?: boolean) => void;
}) {
  const ref = useRef<HTMLElement>(null);
  const position = useAnchoredPopoverGeometry({
    open: true, anchorRef: anchor, maxWidth: 360,
    maxHeight: preview ? 240 : 440, align: "start",
  });
  const visible = !!position;
  useEffect(() => {
    if (preview || !visible) return;
    ref.current?.focus({ preventScroll: true });
  }, [preview, visible]);
  useEffect(() => {
    if (preview) return;
    const outside = (event: PointerEvent) => {
      if (!(event.target instanceof Node) || anchor.current?.contains(event.target)
          || ref.current?.contains(event.target)) return;
      onClose(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      onClose(true);
    };
    document.addEventListener("pointerdown", outside, true);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside, true);
      document.removeEventListener("keydown", escape);
    };
  }, [anchor, onClose, preview]);
  if (!position) return null;
  const { placement, ...style } = position;
  return createPortal(<section ref={ref} id={id} tabIndex={preview ? undefined : -1}
    className={`background-task-popover place-${placement}${preview ? " preview" : ""}`}
    data-placement={placement} style={style}
    role={preview ? "tooltip" : "dialog"} aria-label={preview ? undefined : "后台任务"}
    aria-modal={preview ? undefined : false}>
    {!preview && <TaskHeader count={props.processes.length} onClose={() => onClose(true)} />}
    <TaskList {...props} preview={preview} />
  </section>, document.body);
}

function TaskHeader({ count, onClose }: { count: number; onClose: () => void }) {
  return <header className="background-task-header">
    <strong>后台任务</strong><span>{count}</span>
    <button type="button" aria-label="关闭后台任务" onClick={onClose}>
      <Icon name="close" size={17} />
    </button>
  </header>;
}

function ActiveBackgroundTasks(props: Props) {
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState(false);
  const mobile = useSyncExternalStore(subscribeMobile, isMobile, () => false);
  const anchor = useRef<HTMLButtonElement>(null);
  const id = useId();
  const showPreview = preview && !open && !mobile;
  const close = useCallback((restoreFocus = false) => {
    setOpen(false);
    setPreview(false);
    if (restoreFocus) anchor.current?.focus({ preventScroll: true });
  }, []);
  const details: Props = {
    ...props,
    onOpenAgent: props.onOpenAgent ? (runId, title) => {
      close();
      props.onOpenAgent?.(runId, title);
    } : undefined,
    onOpenFile: props.onOpenFile ? (path, line) => {
      close();
      props.onOpenFile?.(path, line);
    } : undefined,
  };
  return <>
    <button ref={anchor} type="button" className="background-task-trigger"
      aria-label={`后台任务，${props.processes.length} 项进行中`}
      aria-expanded={open} aria-haspopup="dialog" aria-controls={open ? id : undefined}
      aria-describedby={showPreview ? `${id}-preview` : undefined}
      onPointerEnter={event => { if (event.pointerType === "mouse") setPreview(true); }}
      onPointerLeave={() => setPreview(false)}
      onBlur={() => setPreview(false)}
      onClick={() => { setPreview(false); setOpen(value => !value); }}>
      <span className="background-task-indicator" aria-hidden="true" />
      <span>后台任务 · {props.processes.length}</span>
      <Icon name="chev" size={13} />
    </button>
    {showPreview && <TaskPopover {...props} anchor={anchor} id={`${id}-preview`}
      preview onClose={close} />}
    {open && (mobile
      ? <CenteredSheet open label="后台任务" header={false}
          className="background-task-sheet" maxWidth={400} maxHeight={480}
          returnFocusRef={anchor} onClose={() => close()}>
          <div id={id} className="background-task-sheet-content">
            <TaskHeader count={props.processes.length} onClose={() => close()} />
            <TaskList {...details} />
          </div>
        </CenteredSheet>
      : <TaskPopover {...details} anchor={anchor} id={id} preview={false} onClose={close} />)}
  </>;
}

export default function BackgroundTaskControl(props: Props) {
  // Unmount open state at the last native terminal/empty snapshot. A later task
  // starts collapsed; finishing the parent's reply alone never clears this list.
  return props.processes.length ? <ActiveBackgroundTasks {...props} /> : null;
}
