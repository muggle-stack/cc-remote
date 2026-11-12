import {
  lazy,
  Suspense,
  useEffect,
  useRef,
  type RefObject,
} from "react";
import type { CollaborationMember, ServerEvent } from "../protocol";
import type { RelayWs } from "../ws";
import type { SessionRuntime } from "../reducer";
import { useCollaborationProgress } from "../use-collaboration-progress";
import type { HistoryPageCache } from "../history-page-cache";
import { EngineIcon } from "../icons";
import { activeTurnCandidateIds, displayActiveTurnOwnerId } from "../process-blocks";
import { SessionStatus } from "./SessionStatus";
import type { CollaborationResourceAccess } from "./CollaborationResources";
import { ViewerPagesContext } from "../viewer-pages-context";
import { RemoteViewerContext } from "../remote-viewer-context";

const ChatView = lazy(() =>
  import("./ChatView").then((module) => ({ default: module.ChatView })),
);

export default function CollaborationProgress({
  member,
  ws,
  listen,
  onClose,
  onNavigate,
  resources,
  machineId,
  initialRuntime,
  returnFocusRef,
  historyPageCache,
}: {
  member: CollaborationMember;
  ws: RelayWs | null;
  listen: (handler: ((event: ServerEvent) => void) | null) => void;
  onClose: () => void;
  onNavigate: (member: CollaborationMember) => void;
  resources: CollaborationResourceAccess;
  machineId: string;
  initialRuntime?: SessionRuntime;
  returnFocusRef: RefObject<HTMLButtonElement | null>;
  historyPageCache: HistoryPageCache;
}) {
  const sid = member.sid!;
  const dialog = useRef<HTMLDialogElement>(null);
  const { runtime, view, loading, error, request, loadOlder, loadNewer,
    loadDetail, returnToLatest } = useCollaborationProgress({
      machineId, member, ws, listen, initialRuntime, cache: historyPageCache,
    });
  const activeCandidates = activeTurnCandidateIds(
    view.turns,
    view.recovering ? view.activeOwnerId
      : displayActiveTurnOwnerId(runtime.liveOwner?.turnId, runtime.acceptancePending),
    !view.browsing && (runtime.state !== "idle" || runtime.mirroredRunning || !!runtime.acceptancePending),
  );
  useEffect(() => {
    const element = dialog.current;
    const trigger = returnFocusRef.current;
    element?.showModal();
    return () => {
      element?.close();
      if (trigger?.isConnected) trigger.focus({ preventScroll: true });
    };
  }, [returnFocusRef]);

  return (
    <dialog
      ref={dialog}
      className="collab-dialog collab-progress"
      data-engine={member.spec.engine}
      aria-label={`${member.name} 的进度`}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target !== event.currentTarget) return;
        const bounds = event.currentTarget.getBoundingClientRect();
        if (event.clientX < bounds.left || event.clientX > bounds.right
            || event.clientY < bounds.top || event.clientY > bounds.bottom) onClose();
      }}
    >
      <header className="collab-progress-header">
        <EngineIcon engine={member.spec.engine} size={20} />
        <strong>{member.name}</strong>
        <SessionStatus
          state={
            loading
              ? "reconnecting"
              : runtime.mirroredRunning
                ? "running"
                : runtime.state
          }
        />
        <button type="button" className="collab-secondary collab-progress-native"
          onClick={() => onNavigate(member)}>
          查看原生会话 <span aria-hidden="true">↗</span>
        </button>
      </header>
      {error && (
        <p className="collab-error" role="alert">
          {error}
          <button type="button" className="collab-secondary" onClick={request}>重试</button>
        </p>
      )}
      <ViewerPagesContext.Provider value={resources.pages(sid)}>
        <RemoteViewerContext.Provider value={href => resources.openViewerLink(member, href)}>
          <Suspense fallback={<div className="empty" role="status"><div className="spinner" aria-label="加载中" /></div>}>
            <ChatView
              sid={sid}
              engine={member.spec.engine}
              turns={view.turns}
              loading={loading}
              hasMore={view.hasMore}
              historyPagingReady={!loading && !error && view.pagingReady}
              historyCursor={view.oldestId}
              historyRevision={runtime.historyRevision}
              historyViewRevision={view.viewRevision}
              historyGeneration={view.generation}
              historyViewId={view.viewId}
              historyScopeKey={view.scopeKey}
              historyWindowEpoch={view.windowEpoch}
              browseMode={view.browsing}
              hasNewer={view.hasNewer}
              activeTurnId={activeCandidates.length === 1 ? activeCandidates[0] : null}
              ambiguousActiveTurnIds={activeCandidates.length > 1 ? activeCandidates : []}
              onOpenFile={(path, line) => resources.openFile(member, path, line)}
              fileCwd={member.cwd || member.spec.cwd || undefined}
              imageAssets={resources.images(sid)}
              onLoadImage={(path, previewId) => resources.loadImage(member, path, previewId)}
              onAuthorizeImage={resources.authorizeImage}
              onLoadMore={loadOlder}
              onLoadNewer={loadNewer}
              onReturnLatest={returnToLatest}
              onLoadDetail={loadDetail}
            />
          </Suspense>
        </RemoteViewerContext.Provider>
      </ViewerPagesContext.Provider>
    </dialog>
  );
}
