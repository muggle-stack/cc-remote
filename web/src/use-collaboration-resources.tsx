import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import type { CollaborationMember, ServerEvent } from "./protocol";
import type { RelayWs } from "./ws";
import { initialState, reduce, type PreviewAuthorizationState } from "./reducer";
import { InlineImageAssetCache } from "./inline-image-assets";
import { isMarkdownPath } from "./preview-path";
import { resolveRoomFile } from "./collaboration";
import {
  CollaborationPreviewDialog,
  CollaborationImagePreview,
  CollaborationPageController,
  CollaborationViewerPreview,
  type CollaborationResourceAccess,
} from "./components/CollaborationResources";
import { ViewerPagesCache } from "./viewer-pages-cache";
import type { PagesContext } from "./viewer-pages-context";
import type { ViewerPage, ViewerScope, ViewerSelection } from "./remote-viewer";
import { useRemoteViewerLinks } from "./remote-viewer-context";

export function useCollaborationResources(
  machineId: string,
  ws: RelayWs | null,
  theme: "light" | "dark",
  members: CollaborationMember[],
) {
  const [state, dispatch] = useReducer(reduce, initialState);
  const artifact = state.artifact;
  const artifactRef = useRef(artifact);
  artifactRef.current = artifact;
  const [cache] = useState(() => new InlineImageAssetCache());
  const [, redrawImages] = useReducer((value: number) => value + 1, 0);
  const [image, setImage] = useState<{ src: string; alt: string } | null>(null);
  const [pagesCache] = useState(() => new ViewerPagesCache());
  const [pages, setPages] = useState<Record<string, PagesContext | null>>({});
  const [viewer, setViewer] = useState<{
    scope: ViewerScope;
    selection: ViewerSelection | null;
    requestedUrl?: string;
  } | null>(null);
  const viewerMember = useRef<CollaborationMember | null>(null);
  const openRegisteredLink = useRemoteViewerLinks(`collaboration:${machineId}`, href => {
    const member = viewerMember.current;
    if (!member?.sid) return;
    setViewer({
      scope: { machineId, sid: member.sid, engine: member.spec.engine, space: "code" },
      selection: null,
      requestedUrl: href,
    });
  });
  const openPage = useCallback((member: CollaborationMember, page: ViewerPage) => {
    if (!member.sid) return;
    setViewer({
      scope: { machineId, sid: member.sid, engine: member.spec.engine, space: "code" },
      selection: { machine_id: page.machine_id, site_id: page.site_id, entry: page.entry },
    });
  }, [machineId]);
  const onPages = useCallback((sid: string, context: PagesContext | null) => {
    setPages(current => current[sid] === context ? current : { ...current, [sid]: context });
  }, []);
  useEffect(() => () => cache.clear(), [cache]);

  const onEvent = useCallback((event: ServerEvent): boolean => {
    if ((event.type === "preview_asset" && cache.accept(event))
        || (event.type === "preview_authorization_required" && cache.requireAuthorization(event))) {
      redrawImages();
      return true;
    }
    if (event.type === "preview_authorization_result") {
      const retry = cache.acceptAuthorizationResult(event);
      if (retry !== false) {
        if (retry && !ws?.sendGetPreviewAsset(retry.path, retry.previewId, retry.requestId, retry.sid)) {
          cache.cancel(retry.requestId);
        }
        redrawImages();
        return true;
      }
    }
    const current = artifactRef.current;
    if (!current || ![
      "file_preview", "preview_asset", "preview_authorization_required",
      "preview_authorization_result", "file_save_result", "error",
    ].includes(event.type)) return false;
    const requestId = "request_id" in event ? event.request_id : undefined;
    const matches = requestId && (
      requestId === current.requestId || requestId === current.saveRequestId
      || Object.values(current.assets ?? {}).some(asset => asset.requestId === requestId)
    );
    if (!matches || (event.sid && event.sid !== current.sid)) return false;
    dispatch({ type: "event", event });
    return true;
  }, [cache, ws]);

  useEffect(() => {
    if (!artifact?.sid) return;
    const authorization = artifact.authorization?.status === "granted"
      ? artifact.authorization
      : Object.values(artifact.assets ?? {}).find(asset => asset.authorization?.status === "granted")?.authorization;
    if (!authorization) return;
    const sent = authorization.operation === "file_preview"
      ? !!ws?.sendGetFilePreview(authorization.path, authorization.requestId, artifact.sid)
      : !!authorization.previewId && !!ws?.sendGetPreviewAsset(
        authorization.path, authorization.previewId, authorization.requestId, artifact.sid,
      );
    dispatch({
      type: sent ? "preview_authorization_retry_started" : "preview_authorization_retry_failed",
      sid: artifact.sid,
      authorizationId: authorization.authorizationId,
      requestId: authorization.requestId,
    });
  }, [artifact, ws]);

  const openFile = useCallback((member: CollaborationMember, file: string, line?: number) => {
    if (!member.sid) return false;
    const path = resolveRoomFile(member, file);
    const requestId = crypto.randomUUID();
    if (!ws?.sendGetFilePreview(path, requestId, member.sid)) return false;
    dispatch({
      type: "open_file_loading",
      file: path,
      sid: member.sid,
      requestId,
      kind: isMarkdownPath(path) ? "md" : "file",
      line,
    });
    return true;
  }, [ws]);
  const loadImage = useCallback((member: CollaborationMember, path: string, stablePreviewId?: string) => {
    if (!member.sid || !ws) return false;
    const sid = member.sid;
    const assetKey = stablePreviewId ?? path;
    const resolved = resolveRoomFile(member, path);
    if (cache.has(sid, resolved, assetKey)) return true;
    const request = {
      sid,
      path: resolved,
      assetKey,
      previewId: stablePreviewId ?? crypto.randomUUID(),
      requestId: crypto.randomUUID(),
    };
    if (!cache.begin(request)) return false;
    if (!ws.sendGetPreviewAsset(resolved, request.previewId, request.requestId, sid)) {
      cache.cancel(request.requestId);
      return false;
    }
    redrawImages();
    return true;
  }, [cache, ws]);
  const authorizeImage = useCallback((authorization: PreviewAuthorizationState, decision: "allow" | "deny") => {
    const request = cache.authorizationRequest(authorization);
    if (!request || !ws?.sendAuthorizePreview(
      authorization.authorizationId, authorization.requestId, decision, request.sid,
    )) return false;
    if (!cache.markAuthorizationSubmitting(authorization)) return false;
    redrawImages();
    return true;
  }, [cache, ws]);

  const access: CollaborationResourceAccess = {
    openFile,
    images: sid => cache.forSession(sid),
    loadImage,
    authorizeImage,
    previewImage: (src, alt) => setImage({ src, alt }),
    pages: sid => pages[sid] ?? null,
    openViewerLink: (member, href) => {
      viewerMember.current = member;
      return openRegisteredLink(href);
    },
  };
  return {
    access,
    onEvent,
    preview: <>
      {members.filter(member => member.sid).map(member => (
        <CollaborationPageController key={member.id} member={member} machineId={machineId}
          cache={pagesCache} onOpen={openPage} onChange={onPages} />
      ))}
      {artifact && (
        <CollaborationPreviewDialog artifact={artifact} machineId={machineId} theme={theme}
          ws={ws} dispatch={dispatch} onClose={() => dispatch({ type: "clear_artifact" })} />
      )}
      {image && <CollaborationImagePreview image={image} onClose={() => setImage(null)} />}
      {viewer && (
        <CollaborationViewerPreview scope={viewer.scope} selection={viewer.selection}
          requestedUrl={viewer.requestedUrl} context={pages[viewer.scope.sid] ?? null}
          onSelect={selection => setViewer(current => current ? { ...current, selection } : null)}
          onClose={() => setViewer(null)} />
      )}
    </>,
  };
}
