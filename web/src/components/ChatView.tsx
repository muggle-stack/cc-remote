import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type TouchEvent,
  type WheelEvent,
} from "react";
import type { Turn } from "../reducer";
import { MessageBlock } from "./MessageBlock";
import { Icon, ClaudeMark, ClaudeWorking, ClaudeSpark } from "../icons";
import { canForkTurn } from "../session-worktree";
import { ProcessTimeline } from "./ProcessTimeline";
import { finalTextBlocks, processBlocks } from "../process-blocks";
import {
  anchoredScrollTop,
  createFrameCoalescer,
  ScrollFollowController,
  type FrameCoalescer,
  type ScrollFollowSnapshot,
  type ScrollMetrics,
} from "../scroll-follow";

interface HistoryAnchor {
  sid: string | null;
  firstTurnId: string | null;
  scrollHeight: number;
  scrollTop: number;
}

function readScrollMetrics(el: HTMLDivElement): ScrollMetrics {
  return {
    scrollHeight: el.scrollHeight,
    scrollTop: el.scrollTop,
    clientHeight: el.clientHeight,
  };
}

function formatTime(ts: number): string {
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function ChatView({ sid, turns, engine = "claude", loading, hasMore,
  onLoadMore, onEdit, onGetDiff, onFork, forkingPointId }: {
  sid: string | null;
  turns: Turn[];
  engine?: "claude" | "codex";
  loading?: boolean;
  hasMore?: boolean;
  onLoadMore?: () => void;
  onEdit: (prompt: string) => void;
  onGetDiff: (file: string) => void;
  onFork?: (forkPointId: string) => void;
  forkingPointId?: string | null;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const threadInRef = useRef<HTMLDivElement>(null);
  const controllerRef = useRef<ScrollFollowController | null>(null);
  if (!controllerRef.current) controllerRef.current = new ScrollFollowController();
  const frameRef = useRef<FrameCoalescer | null>(null);
  if (!frameRef.current) {
    frameRef.current = createFrameCoalescer(
      (callback) => window.requestAnimationFrame(callback),
      (id) => window.cancelAnimationFrame(id),
    );
  }
  const [scrollState, setScrollState] = useState<ScrollFollowSnapshot>(() =>
    controllerRef.current!.snapshot());
  const [zoom, setZoom] = useState<string | null>(null);   // lightbox image src
  const [zoomBig, setZoomBig] = useState(false);           // fit-to-screen vs actual size
  const anchorRef = useRef<HistoryAnchor | null>(null);
  const renderedSidRef = useRef<string | null | undefined>(undefined);
  const touchYRef = useRef<number | null>(null);

  const syncScrollState = useCallback((next: ScrollFollowSnapshot) => {
    setScrollState((previous) =>
      previous.followOutput === next.followOutput && previous.nearBottom === next.nearBottom
        ? previous
        : next);
  }, []);

  const requestOutputFollow = useCallback(() => {
    frameRef.current?.schedule(() => {
      const el = scrollRef.current;
      const controller = controllerRef.current;
      if (!el || !controller) return;
      if (!controller.isFollowing()) {
        syncScrollState(controller.observeLayout(readScrollMetrics(el)));
        return;
      }
      // Streaming writes are immediate and coalesced once per frame. Smooth
      // scrolling is reserved for the user's explicit "bottom" button.
      el.scrollTop = el.scrollHeight;
      syncScrollState(controller.recordProgrammaticScroll(readScrollMetrics(el)));
    });
  }, [syncScrollState]);

  const pauseOutputFollow = useCallback(() => {
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!el || !controller) return;
    syncScrollState(controller.pause(readScrollMetrics(el)));
  }, [syncScrollState]);

  // Capture both dimensions and the first id. A streaming delta can arrive
  // while history is in flight; only an actual prepend should consume this
  // anchor and shift the viewport.
  const doLoadMore = () => {
    const el = scrollRef.current;
    if (el) {
      anchorRef.current = {
        sid,
        firstTurnId: turns[0]?.id ?? null,
        scrollHeight: el.scrollHeight,
        scrollTop: el.scrollTop,
      };
      pauseOutputFollow();
    }
    onLoadMore?.();
  };

  useLayoutEffect(() => {
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!el || !controller) return;

    // Initial mount and every session switch are anchored synchronously before
    // paint, so the newly focused session opens at its latest content.
    if (renderedSidRef.current !== sid) {
      renderedSidRef.current = sid;
      anchorRef.current = null;
      touchYRef.current = null;
      frameRef.current?.cancel();
      el.scrollTop = el.scrollHeight;
      syncScrollState(controller.reset(readScrollMetrics(el)));
      return;
    }

    const anchor = anchorRef.current;
    const prepended = anchor
      && anchor.sid === sid
      && anchor.firstTurnId !== (turns[0]?.id ?? null);
    if (prepended) {
      el.scrollTop = anchoredScrollTop(
        anchor.scrollTop,
        anchor.scrollHeight,
        el.scrollHeight,
      );
      anchorRef.current = null;
      syncScrollState(controller.recordProgrammaticScroll(readScrollMetrics(el)));
    } else if (!controller.isFollowing()) {
      syncScrollState(controller.observeLayout(readScrollMetrics(el)));
    }

    if (controller.isFollowing()) requestOutputFollow();
  }, [requestOutputFollow, sid, syncScrollState, turns]);

  useLayoutEffect(() => {
    const content = threadInRef.current;
    if (!content || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      const el = scrollRef.current;
      const controller = controllerRef.current;
      if (!el || !controller) return;
      if (controller.isFollowing()) requestOutputFollow();
      else syncScrollState(controller.observeLayout(readScrollMetrics(el)));
    });
    observer.observe(content);
    const viewport = scrollRef.current;
    if (viewport) observer.observe(viewport);
    return () => observer.disconnect();
  }, [requestOutputFollow, syncScrollState]);

  useEffect(() => {
    return () => frameRef.current?.cancel();
  }, []);

  const onScroll = () => {
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!el || !controller) return;
    syncScrollState(controller.observeScroll(readScrollMetrics(el)));
  };

  const onWheel = (event: WheelEvent<HTMLDivElement>) => {
    if (event.deltaY < 0) pauseOutputFollow();
  };

  const onTouchStart = (event: TouchEvent<HTMLDivElement>) => {
    touchYRef.current = event.touches[0]?.clientY ?? null;
  };

  const onTouchMove = (event: TouchEvent<HTMLDivElement>) => {
    const currentY = event.touches[0]?.clientY;
    const previousY = touchYRef.current;
    if (currentY == null || previousY == null) return;
    // A finger moving down scrolls the viewport toward earlier messages.
    if (currentY > previousY) pauseOutputFollow();
    touchYRef.current = currentY;
  };

  const clearTouch = () => { touchYRef.current = null; };

  const scrollToBottom = () => {
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!el || !controller) return;
    syncScrollState(controller.resume(readScrollMetrics(el)));
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  };

  const [copiedId, setCopiedId] = useState<string | null>(null);
  const copyText = (id: string, text: string) => {
    navigator.clipboard?.writeText(text);
    setCopiedId(id);
    setTimeout(() => setCopiedId(null), 1500);
  };
  const aiText = (t: Turn) => finalTextBlocks(t.blocks).map((block) => block.text).join("\n\n");

  // collect the file_paths this turn mutated (Edit/Write) — a summary button +
  // a list of file chips. Click summary => all diffs; click a file => that file.
  const fileChips = (t: Turn) => {
    const files = new Set<string>();
    t.blocks.forEach((b) => {
      if (b.kind === "tool" && (b.tool === "Edit" || b.tool === "Write")) {
        const fp = (b.input as { file_path?: string }).file_path;
        if (fp) files.add(fp);
      }
    });
    if (!files.size) return null;
    const arr = [...files];
    return (
      <div className="turn-files">
        <button className="turn-files-summary" onClick={() => onGetDiff("")}>
          <Icon name="edit" size={13} />改动 {arr.length} 个文件
        </button>
        <div className="turn-files-list">
          {arr.map((f) => (
            <button key={f} className="turn-file-chip" onClick={() => onGetDiff(f)} title={f}>
              {f.split("/").pop()}
            </button>
          ))}
        </div>
      </div>
    );
  };

  if (turns.length === 0) {
    if (loading) {
      return (
        <div className="empty">
          <div className="spinner" aria-label="加载中" />
          <p className="loading-tx">加载会话历史…</p>
        </div>
      );
    }
    return (
      <div className="empty">
        <div className="glyph"><ClaudeMark size={30} /></div>
        <h2>已连接</h2>
        <p>发一条消息开始，或用 <code>/</code> 唤起命令面板（Plan mode、review、技能…）。</p>
      </div>
    );
  }

  return (
    <div className="thread-shell">
      <div className="thread" ref={scrollRef} onScroll={onScroll} onWheel={onWheel}
        onTouchStart={onTouchStart} onTouchMove={onTouchMove}
        onTouchEnd={clearTouch} onTouchCancel={clearTouch}>
        <div className="thread-in" ref={threadInRef}>
          {hasMore && (
            <div className="load-more-wrap">
              <button className="load-more-btn" onClick={doLoadMore}>加载更早的历史</button>
            </div>
          )}
          {turns.map((t, ti) => (
            <div className="turn" key={t.id}>
            {(t.prompt || (t.images && t.images.length) || (t.files && t.files.length)) && (
              <div className="ubub-wrap">
                {t.prompt && <div className="ubub">{t.prompt}</div>}
                {t.images && t.images.length > 0 && (
                  <div className="ubub-imgs">
                    {t.images.map((img, i) => {
                      const src = `data:${img.media_type};base64,${img.data}`;
                      return <img key={i} src={src} className="ubub-img" alt="" title="点击放大"
                        onClick={() => setZoom(src)} />;
                    })}
                  </div>
                )}
                {t.files && t.files.length > 0 && (
                  <div className="ubub-files">
                    {t.files.map((f, i) => (
                      <span key={i} className="ubub-file"><Icon name="read" size={14} />{f.filename}</span>
                    ))}
                  </div>
                )}
                <div className="ubub-meta">
                  {t.ts && <span className="ubub-time">{formatTime(t.ts)}</span>}
                  {t.prompt && <button className="ubub-act" onClick={() => onEdit(t.prompt!)} aria-label="编辑"><Icon name="edit" size={13} /></button>}
                  {t.prompt && <button className={"ubub-act" + (copiedId === t.id ? " copied" : "")} onClick={() => copyText(t.id, t.prompt!)} aria-label="复制"><Icon name="check" size={13} /></button>}
                </div>
              </div>
            )}
            {t.blocks.length > 0 ? (
              <>
                <ProcessTimeline blocks={t.blocks} done={t.done}
                  durationMs={t.durationMs} startTs={t.ts} />
                {finalTextBlocks(t.blocks).map((block) => (
                  <MessageBlock key={block.message_id} text={block.text} done={block.done} />
                ))}
                {!t.done && processBlocks(t.blocks).length === 0
                  && finalTextBlocks(t.blocks).length === 0 && (
                  <div className="turn-working"><ClaudeWorking size={24} /><span className="turn-working-tx">{t.progress ?? "思考中"}</span></div>
                )}
                {t.done && (
                  <>
                    <div className="ubub-meta ai-meta">
                      {t.doneTs && <span className="ubub-time">{formatTime(t.doneTs)}</span>}
                      <button className={"ubub-act" + (copiedId === t.id + "-ai" ? " copied" : "")} onClick={() => copyText(t.id + "-ai", aiText(t))} aria-label="复制"><Icon name="check" size={13} /></button>
                      {onFork && canForkTurn(engine, t) && (
                        <button className="ubub-act" aria-label="派生"
                          data-tooltip="从此回复派生新会话"
                          aria-busy={forkingPointId === t.forkPointId}
                          disabled={!!forkingPointId}
                          onClick={() => onFork(t.forkPointId)}>
                          <Icon name="branch" size={13} />
                        </button>
                      )}
                    </div>
                    {ti === turns.length - 1 && <div className="turn-done-mark"><ClaudeSpark size={22} /></div>}
                  </>
                )}
              </>
            ) : (!t.done && t.prompt) ? (
              <div className="turn-working"><ClaudeWorking size={24} /><span className="turn-working-tx">{t.progress ?? "思考中"}</span></div>
            ) : null}
              {fileChips(t)}
              {t.interrupted && <div className="note interrupted">— 已打断 —</div>}
              {t.error && <div className="note interrupted">{t.error}</div>}
            </div>
          ))}
        </div>
      </div>
      {(!scrollState.followOutput || !scrollState.nearBottom) && (
        <div className="scroll-bottom-wrap">
          <button className="scroll-bottom-btn" onClick={scrollToBottom} aria-label="滚动到底部">
            <Icon name="chev" size={20} />
          </button>
        </div>
      )}
      {zoom && (
        <div className="lightbox" onClick={() => { setZoom(null); setZoomBig(false); }} role="dialog" aria-label="图片预览">
          <img src={zoom} className={"lightbox-img" + (zoomBig ? " big" : "")} alt=""
            title={zoomBig ? "点击缩小 · 点背景关闭" : "点击放大 · 点背景关闭"}
            onClick={(e) => { e.stopPropagation(); setZoomBig((b) => !b); }} />
          <button className="lightbox-close" onClick={() => { setZoom(null); setZoomBig(false); }} aria-label="关闭"><Icon name="close" size={22} /></button>
        </div>
      )}
    </div>
  );
}
