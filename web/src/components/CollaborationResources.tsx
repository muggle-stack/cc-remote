import { lazy, Suspense, useCallback, useEffect, useMemo, useRef } from "react";
import type { CollaborationMember, CollaborationMessage } from "../protocol";
import type { RelayWs } from "../ws";
import { reduce, type Artifact, type PreviewAuthorizationState } from "../reducer";
import { type InlineImageAsset } from "../inline-image-assets";
import { isMarkdownPath } from "../preview-path";
import { ZoomableImage } from "./ZoomableImage";
import { MessageBlock } from "./MessageBlock";
import { PagePreviewLinks } from "./PagePreviewLinks";
import type { Turn } from "../domain/conversation";
import { RemoteViewerContext } from "../remote-viewer-context";
import { ViewerPagesContext, type PagesContext } from "../viewer-pages-context";
import type { ViewerPagesCache } from "../viewer-pages-cache";
import type { ViewerPage, ViewerScope, ViewerSelection } from "../remote-viewer";

const ArtifactPanel = lazy(() =>
  import("./ArtifactPanel").then(module => ({ default: module.ArtifactPanel })),
);
const PagesController = lazy(() => import("../viewer-pages-controller"));
const RemoteViewerPanel = lazy(() =>
  import("./RemoteViewerPanel").then(module => ({ default: module.RemoteViewerPanel })),
);

export interface CollaborationResourceAccess {
  openFile: (member: CollaborationMember, path: string, line?: number) => boolean;
  images: (sid: string) => Record<string, InlineImageAsset>;
  loadImage: (member: CollaborationMember, path: string, previewId?: string) => boolean;
  authorizeImage: (authorization: PreviewAuthorizationState, decision: "allow" | "deny") => boolean;
  previewImage: (src: string, alt: string) => void;
  pages: (sid: string) => PagesContext | null;
  openViewerLink: (member: CollaborationMember, href: string) => boolean;
}

export function CollaborationReply({ member, message, resources }: {
  member: CollaborationMember;
  message: CollaborationMessage;
  resources: CollaborationResourceAccess;
}) {
  const turn = useMemo<Turn>(() => ({
    id: message.id,
    prompt: "",
    done: true,
    blocks: [{
      kind: "text", channel: "final", message_id: message.id, text: message.text, done: true,
    }],
  }), [message.id, message.text]);
  return (
    <ViewerPagesContext.Provider value={member.sid ? resources.pages(member.sid) : null}>
      <RemoteViewerContext.Provider value={href => resources.openViewerLink(member, href)}>
        <MessageBlock text={message.text} done fileCwd={member.cwd || member.spec.cwd || undefined}
          onOpenFile={(path, line) => resources.openFile(member, path, line)}
          imageAssets={member.sid ? resources.images(member.sid) : undefined}
          onLoadImage={path => resources.loadImage(member, path)}
          onAuthorizeImage={resources.authorizeImage} onPreviewImage={resources.previewImage} />
        <PagePreviewLinks turn={turn} sid={member.sid} />
      </RemoteViewerContext.Provider>
    </ViewerPagesContext.Provider>
  );
}

export function CollaborationPageController({ member, machineId, cache, onOpen, onChange }: {
  member: CollaborationMember;
  machineId: string;
  cache: ViewerPagesCache;
  onOpen: (member: CollaborationMember, page: ViewerPage) => void;
  onChange: (sid: string, context: PagesContext | null) => void;
}) {
  const currentMember = useRef(member);
  currentMember.current = member;
  const scope = useMemo<ViewerScope>(() => ({
    machineId, sid: member.sid!, engine: member.spec.engine, space: "code",
  }), [machineId, member.sid, member.spec.engine]);
  const change = useCallback((context: PagesContext | null) => onChange(scope.sid, context), [onChange, scope.sid]);
  const open = useCallback((page: ViewerPage) => onOpen(currentMember.current, page), [onOpen]);
  return (
    <Suspense fallback={null}>
      <PagesController scope={scope} cache={cache} onOpen={open} onChange={change} />
    </Suspense>
  );
}

export function CollaborationViewerPreview({ scope, selection, requestedUrl, context, onSelect, onClose }: {
  scope: ViewerScope;
  selection: ViewerSelection | null;
  requestedUrl?: string;
  context: PagesContext | null;
  onSelect: (selection: ViewerSelection | null) => void;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    const trigger = document.activeElement as HTMLElement | null;
    element?.showModal();
    return () => {
      element?.close();
      if (trigger?.isConnected) trigger.focus({ preventScroll: true });
    };
  }, []);
  return (
    <dialog ref={dialog} className="collab-dialog collab-resource-dialog" aria-label="页面预览"
      onCancel={event => { event.preventDefault(); onClose(); }}
      onClick={event => {
        if (event.target !== event.currentTarget) return;
        const bounds = event.currentTarget.getBoundingClientRect();
        if (event.clientX < bounds.left || event.clientX > bounds.right
            || event.clientY < bounds.top || event.clientY > bounds.bottom) onClose();
      }}>
      <ViewerPagesContext.Provider value={context}>
        <Suspense fallback={<div className="empty" role="status"><div className="spinner" aria-label="加载中" /></div>}>
          <RemoteViewerPanel scope={scope} selection={selection} requestedUrl={requestedUrl}
            devices={[{ machine_id: scope.machineId, label: "当前设备" }]}
            onSelect={onSelect} onClose={onClose} />
        </Suspense>
      </ViewerPagesContext.Provider>
    </dialog>
  );
}

export function CollaborationPreviewDialog({ artifact, machineId, theme, ws, dispatch, onClose }: {
  artifact: Artifact;
  machineId: string;
  theme: "light" | "dark";
  ws: RelayWs | null;
  dispatch: React.Dispatch<Parameters<typeof reduce>[1]>;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const dirty = useRef(false);
  const close = () => {
    if (!dirty.current || window.confirm("放弃尚未保存的修改？")) onClose();
  };
  useEffect(() => {
    const element = dialog.current;
    const trigger = document.activeElement as HTMLElement | null;
    element?.showModal();
    return () => {
      element?.close();
      if (trigger?.isConnected) trigger.focus({ preventScroll: true });
    };
  }, []);
  const open = (path: string, line?: number) => {
    if (!artifact.sid || (dirty.current && !window.confirm("放弃尚未保存的修改？"))) return;
    const requestId = crypto.randomUUID();
    if (!ws?.sendGetFilePreview(path, requestId, artifact.sid)) return;
    dirty.current = false;
    dispatch({
      type: "open_file_loading", file: path, sid: artifact.sid, requestId,
      kind: isMarkdownPath(path) ? "md" : "file", line,
    });
  };
  return <dialog ref={dialog} className="collab-dialog collab-resource-dialog" aria-label="文件预览"
    onCancel={event => { event.preventDefault(); close(); }}
    onClick={event => {
      if (event.target !== event.currentTarget) return;
      const bounds = event.currentTarget.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right
          || event.clientY < bounds.top || event.clientY > bounds.bottom) close();
    }}>
    <Suspense fallback={<div className="empty" role="status"><div className="spinner" aria-label="加载中" /></div>}>
      <ArtifactPanel key={`${artifact.sid}:${artifact.file}:${artifact.requestId}`}
        artifact={artifact} machineId={machineId} theme={theme} active="diff" hasBtw={false}
        onTab={() => {}} onClose={close} onRefresh={open} onOpenFile={open}
        onDirtyChange={value => { dirty.current = value; }}
        onLoadPreviewAsset={(path, previewId) => {
          if (!artifact.sid) return false;
          const requestId = crypto.randomUUID();
          if (!ws?.sendGetPreviewAsset(path, previewId, requestId, artifact.sid)) return false;
          dispatch({ type: "begin_preview_asset", sid: artifact.sid, path, previewId, requestId });
          return true;
        }}
        onAuthorizePreview={(authorization, decision) => {
          if (!artifact.sid || !ws?.sendAuthorizePreview(authorization.authorizationId,
            authorization.requestId, decision, artifact.sid)) return false;
          dispatch({ type: "submit_preview_authorization", sid: artifact.sid,
            authorizationId: authorization.authorizationId, requestId: authorization.requestId });
          return true;
        }}
        onSaveMarkdown={(path, content, expectedSize, expectedMtimeNs, expectedRevision) => {
          if (!artifact.sid) return null;
          const requestId = crypto.randomUUID();
          if (!ws?.sendSaveMarkdown(path, content, expectedSize, expectedMtimeNs,
            expectedRevision, requestId, artifact.sid)) return null;
          dispatch({ type: "start_file_save", requestId, content });
          return requestId;
        }} />
    </Suspense>
  </dialog>;
}


export function CollaborationImagePreview({ image, onClose }: {
  image: { src: string; alt: string };
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    const trigger = document.activeElement as HTMLElement | null;
    element?.showModal();
    return () => {
      element?.close();
      if (trigger?.isConnected) trigger.focus({ preventScroll: true });
    };
  }, []);
  return (
    <dialog ref={dialog} className="collab-dialog collab-image-dialog" aria-label={image.alt || "图片预览"}
      onCancel={event => { event.preventDefault(); onClose(); }}
      onClick={event => {
        if (event.target !== event.currentTarget) return;
        const bounds = event.currentTarget.getBoundingClientRect();
        if (event.clientX < bounds.left || event.clientX > bounds.right
            || event.clientY < bounds.top || event.clientY > bounds.bottom) onClose();
      }}>
      <ZoomableImage src={image.src} alt={image.alt} />
      <button type="button" className="collab-secondary" onClick={onClose}>关闭预览</button>
    </dialog>
  );
}
