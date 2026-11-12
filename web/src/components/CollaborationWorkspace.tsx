import {
  useCallback,
  lazy,
  Suspense,
  useEffect,
  useLayoutEffect,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
  type RefObject,
} from "react";
import type {
  CollaborationDelivery,
  CollaborationCommand,
  CollaborationMember,
  CollaborationMemberSpec,
  CollaborationMessage,
  CollaborationRoom,
  CollaborationState,
  CollaborationSession,
  DirList,
  Engine,
  Models,
  PermissionProfiles,
  QueryFile,
  QueryImg,
  ServerEvent,
} from "../protocol";
import type { RelayWs } from "../ws";
import { EngineIcon, EngineWorking, Icon } from "../icons";
import { WorkingWord } from "./WorkingWord";
import { CollaborationReply } from "./CollaborationResources";
import { SessionStatus } from "./SessionStatus";
import { DirPicker } from "./DirPicker";
import { QuestionSheet } from "./QuestionSheet";
import { pickFiles } from "../attachment-import";
import { decodedSize } from "../img";
import { PERMS } from "../data";
import type { SidebarController } from "../sidebar-state";
import { useMobileSidebar } from "../use-mobile-sidebar";
import { SidebarShell } from "./SidebarLayout";
import { SidebarToggle } from "./SidebarToggle";
import { EngineSelector } from "./EngineSelector";
import { CollaborationSelect } from "./CollaborationSelect";
import { useCollaborationResources } from "../use-collaboration-resources";
import type { SessionRuntime } from "../reducer";
import type { HistoryPageCache } from "../history-page-cache";
import {
  editRoomMentions,
  readRoomDraft,
  mergeRoomMessages,
  roomActiveStates,
  roomCatalogKey,
  roomDeliveryLabel,
  roomMemberIdentity,
  roomMemberDescription,
  shortRoomSessionId,
  roomMentionMembers,
  refreshRoomMentionNames,
  type RoomMention,
  type RoomDraft,
} from "../collaboration";
import { OlderHistoryLoadGate, isAtHistoryEdge } from "../scroll-follow";
import { HISTORY_REQUEST_TIMEOUT_MS } from "../history-requests";
import "./CollaborationWorkspace.css";
const CollaborationProgress = lazy(() => import("./CollaborationProgress"));

type Profiles = NonNullable<CollaborationState["data"]["profiles"]>;
type Catalog = { models: Models; permissions?: PermissionProfiles };
type Editor = {
  name: string;
  specs: CollaborationMemberSpec[];
  roomId?: string;
};

interface Props {
  machineId: string;
  theme: "light" | "dark";
  deviceName: string;
  connected: boolean;
  ws: RelayWs | null;
  listen: (handler: ((event: ServerEvent) => boolean) | null) => void;
  sidebar: SidebarController;
  onEngineChange: (engine: Engine) => void;
  onDevice: () => void;
  onNavigate: (member: CollaborationMember) => void;
  getSessionRuntime: (sid: string) => SessionRuntime | undefined;
  historyPageCache: HistoryPageCache;
}

function Dialog({
  title,
  children,
  onClose,
  className = "",
  icon,
  aside,
  error,
  returnFocusRef,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  className?: string;
  icon?: ReactNode;
  aside?: ReactNode;
  error?: string | null;
  returnFocusRef?: RefObject<HTMLElement | null>;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const heading = useId();
  useEffect(() => {
    const dialog = ref.current;
    const trigger = returnFocusRef?.current;
    dialog?.showModal();
    return () => {
      if (!returnFocusRef) return;
      dialog?.close();
      if (trigger?.isConnected) trigger.focus({ preventScroll: true });
    };
  }, [returnFocusRef]);
  return (
    <dialog
      ref={ref}
      className={`collab-dialog ${className}`}
      aria-labelledby={heading}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="collab-dialog-content">
        <header>
          <h2 id={heading}>{icon}<span>{title}</span></h2>
          <div className="collab-dialog-actions">
            {aside}
            <button
              type="button"
              className="collab-dialog-close"
              aria-label="关闭"
              onClick={onClose}
            >
              <Icon name="close" />
            </button>
          </div>
        </header>
        {error && <div className="collab-error collab-dialog-error" role="alert">{error}</div>}
        {children}
      </div>
    </dialog>
  );
}

const names: Record<Engine, string> = {
  claude: "Claude",
  codex: "Codex",
  dsh: "DSH",
};

function defaultParticipant(profiles: Profiles): CollaborationMemberSpec {
  const engine: Engine = profiles.claude?.length ? "claude"
    : profiles.codex?.length ? "codex" : "dsh";
  return { engine, profile_id: profiles[engine]?.[0]?.id ?? null };
}

export default function CollaborationWorkspace({
  machineId,
  theme,
  deviceName,
  connected,
  ws,
  listen,
  sidebar,
  onEngineChange,
  onDevice,
  onNavigate,
  getSessionRuntime,
  historyPageCache,
}: Props) {
  const [rooms, setRooms] = useState<CollaborationRoom[]>([]);
  const [roomsLoaded, setRoomsLoaded] = useState(false);
  const [roomId, setRoomId] = useState<string | null>(() =>
    localStorage.getItem(`cc_remote_collaboration:${machineId}`),
  );
  const roomIdRef = useRef(roomId);
  const [room, setRoom] = useState<CollaborationRoom | null>(null);
  const [messages, setMessages] = useState<CollaborationMessage[]>([]);
  const { access: resourceAccess, onEvent: onResourceEvent, preview: resourcePreview } =
    useCollaborationResources(machineId, ws, theme, room?.members?.filter(member =>
      !member.removed || messages.some(message => message.member_id === member.id),
    ) ?? []);
  const [deliveries, setDeliveries] = useState<CollaborationDelivery[]>([]);
  const [questions, setQuestions] = useState<
    NonNullable<CollaborationState["data"]["questions"]>
  >([]);
  const [profiles, setProfiles] = useState<Profiles>({});
  const [hasMore, setHasMore] = useState(false);
  const sidebarOpen = useSyncExternalStore(
    sidebar.subscribe,
    sidebar.getSnapshot,
    sidebar.getSnapshot,
  );
  const setSidebarOpen = sidebar.setOpen;
  const sidebarRef = useMobileSidebar(sidebarOpen, setSidebarOpen);
  const [compact, setCompact] = useState(() => window.innerWidth < 980);
  const [search, setSearch] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const [membersOpen, setMembersOpen] = useState(false);
  const [progressMember, setProgressMember] =
    useState<CollaborationMember | null>(null);
  const progressListener = useRef<((event: ServerEvent) => void) | null>(null);
  const progressTrigger = useRef<HTMLButtonElement | null>(null);
  const listenProgress = useCallback(
    (handler: ((event: ServerEvent) => void) | null) => {
      progressListener.current = handler;
    },
    [],
  );
  const [editor, setEditor] = useState<Editor | null>(null);
  const creating = !!editor && !editor.roomId;
  const creatingRef = useRef(creating);
  creatingRef.current = creating;
  const [renameMember, setRenameMember] = useState<CollaborationMember | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [sessionSpec, setSessionSpec] = useState<CollaborationMemberSpec | null>(null);
  const [sessionChoices, setSessionChoices] = useState<CollaborationSession[]>([]);
  const [sessionSearch, setSessionSearch] = useState("");
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  const sessionsRequest = useRef<string | null>(null);
  const sessionsTimer = useRef<number | null>(null);
  const [settingsIndex, setSettingsIndex] = useState<number | null>(null);
  const [catalogs, setCatalogs] = useState<Record<string, Catalog>>({});
  const [catalogLoading, setCatalogLoading] = useState<string | null>(null);
  const [directory, setDirectory] = useState<DirList | null>(null);
  const [directoryIndex, setDirectoryIndex] = useState<number | null>(null);
  const directoryOpen = directoryIndex !== null;
  const directoryTrigger = useRef<HTMLButtonElement | null>(null);
  const settingsDirectoryId = useId();
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const pendingRef = useRef<{
    id: string;
    operation: string;
    roomId: string | null;
    messageId?: string;
    draft?: string;
    attachments?: (QueryImg | QueryFile)[];
  } | null>(null);
  const versions = useRef(
    new Map<string, { generation: string; version: number }>(),
  );
  const listVersion = useRef<{ generation: string; version: number } | null>(
    null,
  );
  const importingRef = useRef(false);
  const [text, setText] = useState("");
  const [mentions, setMentions] = useState<RoomMention[]>([]);
  const [mentionQuery, setMentionQuery] = useState<{
    start: number;
    end: number;
    query: string;
  } | null>(null);
  const [mentionIndex, setMentionIndex] = useState(0);
  const [images, setImages] = useState<QueryImg[]>([]);
  const [files, setFiles] = useState<QueryFile[]>([]);
  const [importing, setImporting] = useState(false);
  const [missingAttachments, setMissingAttachments] = useState(0);
  const draftMessageId = useRef<string | undefined>(undefined);
  const input = useRef<HTMLTextAreaElement>(null);
  const mentionListId = useId();
  const pendingSelection = useRef<number | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const restoreAnchor = useRef<{
    height: number; top: number; messageId?: string; offset?: number;
  } | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const historyRequest = useRef<{
    id: string; roomId: string; before: number; timer: number;
  } | null>(null);
  const completedHistoryBefore = useRef<number | null>(null);
  const touchHistoryGate = useRef(new OlderHistoryLoadGate());
  const wheelHistoryGate = useRef(new OlderHistoryLoadGate());
  const wheelTimer = useRef<number | null>(null);
  const touchY = useRef<number | null>(null);
  const lastScrollTop = useRef(0);
  const finishHistory = useCallback(() => {
    if (historyRequest.current) window.clearTimeout(historyRequest.current.timer);
    historyRequest.current = null;
    setHistoryLoading(false);
    touchHistoryGate.current.complete();
    wheelHistoryGate.current.complete();
  }, []);
  const draftKey = `cc_remote_room_draft:${machineId}:${roomId ?? "new"}`;
  const saveDraft = useCallback((draft: RoomDraft) => {
    try {
      localStorage.setItem(draftKey, JSON.stringify(draft));
    } catch {
      setError("草稿暂存在当前页面，请保持页面打开。");
    }
  }, [draftKey]);

  const chooseRoom = useCallback(
    (id: string | null) => {
      roomIdRef.current = id;
      setRoomId(id);
      setRoom(null);
      setMessages([]);
      setDeliveries([]);
      setQuestions([]);
      setError(null);
      finishHistory();
      completedHistoryBefore.current = null;
      setHistoryError(null);
      touchHistoryGate.current.endGesture();
      wheelHistoryGate.current.endGesture();
      setEditor(null);
      setSettingsIndex(null);
      setDirectoryIndex(null);
      if (compact) setSidebarOpen(false);
      restoreAnchor.current = null;
      following.current = true;
      if (id) {
        localStorage.setItem(`cc_remote_collaboration:${machineId}`, id);
        ws?.sendCollaboration({ operation: "read", room_id: id });
      } else localStorage.removeItem(`cc_remote_collaboration:${machineId}`);
    },
    [compact, machineId, setSidebarOpen, ws, finishHistory],
  );

  const command = useCallback(
    (
      operation: CollaborationCommand["operation"],
      values: Record<string, unknown> = {},
      blocking = false,
    ) => {
      if (!connected || !ws) {
        setError("设备连接恢复后即可继续操作。");
        return null;
      }
      const id = ws.sendCollaboration({
        operation,
        room_id: roomIdRef.current,
        ...values,
      });
      if (id && blocking) {
        setError(null);
        pendingRef.current = { id, operation, roomId: roomIdRef.current };
        setPending(id);
      }
      if (!id) setError("操作暂未提交，请检查设备连接后重试。");
      return id;
    },
    [connected, ws],
  );

  useEffect(() => {
    listen((event) => {
      if (onResourceEvent(event)) return true;
      progressListener.current?.(event);
      if (
        event.type === "error" &&
        event.request_id === pendingRef.current?.id
      ) {
        setError(event.message);
        setPending(null);
        pendingRef.current = null;
        return true;
      }
      if (event.type === "dir_list" && directoryOpen) {
        setDirectory(event);
        return true;
      }
      if (event.type === "error" && event.request_id === historyRequest.current?.id) {
        finishHistory();
        setHistoryError(event.message);
        return true;
      }
      if (
        (event.type === "models" || event.type === "permission_profiles") &&
        settingsIndex !== null
      )
        return true;
      if (event.type !== "collaboration_state") return false;
      if (event.error) {
        if (event.request_id === historyRequest.current?.id) {
          finishHistory();
          setHistoryError(event.error);
          return true;
        }
        if (event.request_id === sessionsRequest.current) {
          if (sessionsTimer.current !== null) window.clearTimeout(sessionsTimer.current);
          setSessionsLoading(false);
          setSessionsError(event.error);
          return true;
        }
        setError(event.error);
        if (event.request_id === pendingRef.current?.id) {
          setPending(null);
          pendingRef.current = null;
        }
        setCatalogLoading(null);
        return true;
      }
      const data = event.data;
      if (data.sessions && event.request_id === sessionsRequest.current) {
        if (sessionsTimer.current !== null) window.clearTimeout(sessionsTimer.current);
        setSessionChoices(data.sessions);
        setSessionsLoading(false);
        setSessionsError(null);
      }
      if (data.profiles) setProfiles(data.profiles);
      const version = {
        generation: data.generation ?? "",
        version: data.version ?? 0,
      };
      if (
        data.rooms &&
        (!listVersion.current ||
          listVersion.current.generation !== version.generation ||
          listVersion.current.version <= version.version)
      ) {
        setRooms(data.rooms);
        setRoomsLoaded(true);
        listVersion.current = version;
      }
      if (data.catalog && data.catalog_spec) {
        const key = roomCatalogKey(data.catalog_spec);
        setCatalogs((current) => ({
          ...current,
          [key]: {
            models: data.catalog!,
            permissions: data.permission_profiles,
          },
        }));
        setCatalogLoading((current) => (current === key ? null : current));
      }
      const pendingAction = pendingRef.current;
      const acceptedMessage =
        pendingAction?.operation === "send" &&
        pendingAction.roomId === data.room?.id &&
        (data.messages?.some(
          (message) => message.id === pendingAction.messageId,
        ) ||
          data.deliveries?.some(
            (delivery) => delivery.message_id === pendingAction.messageId,
          ));
      const action =
        event.request_id === pendingAction?.id || acceptedMessage
          ? pendingAction
          : null;
      if (action) {
        setPending(null);
        pendingRef.current = null;
        if (action.operation === "create" && event.room_id) {
          chooseRoom(event.room_id);
          setEditor(null);
          setSettingsIndex(null);
        } else if (action.operation === "add_member") {
          setEditor(null);
          setSettingsIndex(null);
        } else if (action.operation === "rename_member") {
          setRenameMember(null);
        } else if (action.operation === "send") {
          if (action.roomId === roomIdRef.current) {
            if (action.draft === input.current?.value) {
              setText("");
              setMentions([]);
              draftMessageId.current = undefined;
              setMissingAttachments(0);
              localStorage.removeItem(draftKey);
            }
            setImages((current) =>
              current.filter((item) => !action.attachments?.includes(item)),
            );
            setFiles((current) =>
              current.filter((item) => !action.attachments?.includes(item)),
            );
          }
        }
      }
      if (data.room && data.room.id === roomIdRef.current) {
        const previous = versions.current.get(data.room.id);
        if (data.before && data.messages) {
          const request = historyRequest.current;
          if (!request || event.request_id !== request.id
              || request.roomId !== data.room.id || request.before !== data.before) return true;
          if (previous && previous.generation !== version.generation) {
            finishHistory();
            setHistoryError("连接已更新，请重试加载历史。");
            return true;
          }
          const element = scroll.current;
          const visible = element && [...element.querySelectorAll<HTMLElement>("[data-message-id]")]
            .find(node => node.getBoundingClientRect().bottom > element.getBoundingClientRect().top);
          restoreAnchor.current = element
            ? { height: element.scrollHeight, top: element.scrollTop,
              messageId: visible?.dataset.messageId,
              offset: visible ? visible.getBoundingClientRect().top - element.getBoundingClientRect().top : undefined }
            : null;
          setMessages((current) => mergeRoomMessages(data.messages!, current));
          setHasMore(!!data.has_more);
          if (!data.has_more || data.messages.some(message => message.seq < request.before))
            completedHistoryBefore.current = request.before;
          finishHistory();
          if (data.has_more && !data.messages.some(message => message.seq < request.before))
            setHistoryError("历史分页未前进，请重试。");
          return true;
        }
        if (
          previous?.generation === version.generation &&
          previous.version > version.version
        )
          return true;
        versions.current.set(data.room.id, version);
        if (previous?.generation !== version.generation) completedHistoryBefore.current = null;
        const pendingId = draftMessageId.current;
        if (
          pendingId &&
          (data.messages?.some((message) => message.id === pendingId) ||
            data.deliveries?.some(
              (delivery) => delivery.message_id === pendingId,
            ))
        ) {
          draftMessageId.current = undefined;
          setText("");
          setMentions([]);
          setMissingAttachments(0);
          localStorage.removeItem(draftKey);
        }
        setRoom((current) =>
          current && current.revision > data.room!.revision
            ? current
            : data.room!,
        );
        if (data.messages) {
          setMessages((current) =>
            mergeRoomMessages(
              previous?.generation === version.generation ? current : [],
              data.messages!,
            ),
          );
          if (data.before || !messages.length || previous?.generation !== version.generation)
            setHasMore(!!data.has_more);
        }
        if (data.deliveries) setDeliveries(data.deliveries);
        if (data.questions) setQuestions(data.questions);
      }
      if (
        data.rooms &&
        !data.room &&
        !creatingRef.current &&
        (!roomIdRef.current ||
          !data.rooms.some((item) => item.id === roomIdRef.current))
      ) {
        chooseRoom(data.rooms.find((item) => !item.archived)?.id ?? null);
      }
      return true;
    });
    return () => listen(null);
  }, [
    listen,
    chooseRoom,
    directoryOpen,
    settingsIndex,
    draftKey,
    messages.length,
    finishHistory,
    onResourceEvent,
  ]);

  useEffect(() => {
    if (!connected) return;
    command("list");
    if (roomIdRef.current) command("read");
    const timer = window.setInterval(() => {
      if (!document.hidden) command(roomIdRef.current ? "read" : "list");
    }, 3500);
    return () => window.clearInterval(timer);
  }, [command, connected]);

  useEffect(() => {
    if (!connected) finishHistory();
    return () => {
      if (historyRequest.current) window.clearTimeout(historyRequest.current.timer);
      if (wheelTimer.current !== null) window.clearTimeout(wheelTimer.current);
    };
  }, [connected, finishHistory]);

  useEffect(() => {
    const saved = readRoomDraft(localStorage.getItem(draftKey));
    setText(saved.text);
    setMentions(saved.mentions);
    draftMessageId.current = saved.messageId;
    setMissingAttachments(saved.attachmentCount ?? 0);
    setImages([]);
    setFiles([]);
    setMentionQuery(null);
    setMembersOpen(false);
    setEditor(null);
    setSettingsIndex(null);
    setProgressMember(null);
    pendingSelection.current = null;
  }, [draftKey]);

  useEffect(() => {
    if (!room?.members || draftMessageId.current) return;
    const next = refreshRoomMentionNames(text, mentions, room.members);
    if (next.text === text || next.text.length > 65536) return;
    const cursor = input.current?.selectionStart;
    const previous = [...mentions].sort((left, right) => left.start - right.start);
    if (document.activeElement === input.current && cursor !== undefined)
      pendingSelection.current = cursor + next.mentions.reduce((delta, mention, index) =>
        delta + (previous[index].end <= cursor ? mention.end - mention.start
          - (previous[index].end - previous[index].start) : 0), 0);
    setText(next.text);
    setMentions(next.mentions);
    saveDraft({ ...next, attachmentCount: Math.max(missingAttachments, images.length + files.length) });
  }, [room?.members, text, mentions, missingAttachments, images.length, files.length, saveDraft]);

  useLayoutEffect(() => {
    const element = input.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(180, Math.max(35, element.scrollHeight))}px`;
    if (pendingSelection.current !== null) {
      element.focus({ preventScroll: true });
      element.setSelectionRange(
        pendingSelection.current,
        pendingSelection.current,
      );
      pendingSelection.current = null;
    }
  }, [text]);

  useEffect(() => {
    const media = window.matchMedia("(max-width: 979px)");
    const update = () => setCompact(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    if (!compact || !sidebarOpen) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSidebarOpen(false);
    };
    document.addEventListener("keydown", close);
    return () => document.removeEventListener("keydown", close);
  }, [compact, setSidebarOpen, sidebarOpen]);

  useLayoutEffect(() => {
    if (creating) return;
    const element = scroll.current;
    const anchor = restoreAnchor.current;
    if (element && anchor) {
      const retained = [...element.querySelectorAll<HTMLElement>("[data-message-id]")]
        .find(node => node.dataset.messageId === anchor.messageId);
      element.scrollTop = retained && anchor.offset !== undefined
        ? element.scrollTop + retained.getBoundingClientRect().top
          - element.getBoundingClientRect().top - anchor.offset
        : anchor.top + element.scrollHeight - anchor.height;
      restoreAnchor.current = null;
    } else if (following.current && element)
      element.scrollTop = element.scrollHeight;
    if (element) lastScrollTop.current = element.scrollTop;
  }, [messages, deliveries, roomId, creating]);

  const activeMembers =
    room?.members?.filter((member) => !member.removed) ?? [];
  const lookup = new Map(
    room?.members?.map((member) => [member.id, member]) ?? [],
  );
  const working = deliveries.some((delivery) =>
    roomActiveStates.has(delivery.status),
  );
  const shownRooms = rooms.filter(
    (item) =>
      !!item.archived === showArchived &&
      item.name.toLowerCase().includes(search.toLowerCase()),
  );
  const currentSettings =
    settingsIndex !== null ? editor?.specs[settingsIndex] : null;
  const directorySpec =
    directoryIndex !== null ? editor?.specs[directoryIndex] : null;
  const memberChoices = (["claude", "codex", "dsh"] as Engine[]).flatMap(engine =>
    (profiles[engine] ?? []).map(profile => ({
      value: `${engine}:${profile.id}`,
      engine,
      profile_id: profile.id,
      label: `${names[engine]} · ${profile.id}`,
      description: profile.label !== profile.id ? profile.label : undefined,
      group: names[engine],
      icon: <EngineIcon engine={engine} size={19} />,
    })),
  );
  const currentCatalog = currentSettings
    ? catalogs[roomCatalogKey(currentSettings)]
    : undefined;
  const selectedModel =
    currentCatalog?.models.models.find(
      (model) => model.id === currentSettings?.model,
    ) ??
    currentCatalog?.models.models.find(
      (model) =>
        model.id === currentCatalog.models.default_model || model.is_default,
    );
  const mentionMembers = roomMentionMembers(activeMembers, mentionQuery?.query ?? "");
  const mentionSearch = mentionQuery?.query.toLocaleLowerCase() ?? "";
  const mentionChoices = [
    ...(!mentionSearch || "所有参与者".includes(mentionSearch) || "all".startsWith(mentionSearch)
      ? [{ id: "all", label: "所有参与者" }] : []),
    ...mentionMembers.map((member) => ({ id: member.id, label: member.name })),
  ];

  const updateText = (next: string, cursor: number) => {
    const nextMentions = editRoomMentions(text, next, mentions);
    setMentions(nextMentions);
    setText(next);
    draftMessageId.current = undefined;
    saveDraft({
      text: next,
      mentions: nextMentions,
      attachmentCount: Math.max(
        missingAttachments,
        images.length + files.length,
      ),
    });
    const match = /@([^@\s]{0,40})$/.exec(next.slice(0, cursor));
    setMentionQuery(
      match
        ? { start: cursor - match[0].length, end: cursor, query: match[1] }
        : null,
    );
    setMentionIndex(0);
  };

  const insertMention = () => {
    const element = input.current;
    if (!element) return;
    const start = element.selectionStart;
    const end = element.selectionEnd;
    const before = text.slice(0, start);
    element.focus({ preventScroll: true });
    if (start === end && /@[^@\s]{0,40}$/.test(before)) {
      updateText(text, start);
      return;
    }
    const token = /\S$/.test(before) ? " @" : "@";
    const next = before + token + text.slice(end);
    if (next.length > element.maxLength) return;
    const cursor = start + token.length;
    pendingSelection.current = cursor;
    updateText(next, cursor);
  };

  const chooseMention = (id: string) => {
    if (!mentionQuery) return;
    const member = lookup.get(id);
    const label = id === "all" ? "所有参与者" : member?.name;
    if (!label) return;
    const token = `@${label}`;
    const next =
      text.slice(0, mentionQuery.start) +
      token +
      " " +
      text.slice(mentionQuery.end);
    if (next.length > (input.current?.maxLength ?? 65536)) {
      setError("内容已达到长度限制，请缩短后点名。");
      return;
    }
    const kept = editRoomMentions(text, next, mentions);
    const nextMentions = [
      ...kept,
      {
        start: mentionQuery.start,
        end: mentionQuery.start + token.length,
        memberIds: id === "all" ? activeMembers.map((item) => item.id) : [id],
      },
    ];
    setMentions(nextMentions);
    pendingSelection.current = mentionQuery.start + token.length + 1;
    setText(next);
    draftMessageId.current = undefined;
    saveDraft({
      text: next,
      mentions: nextMentions,
      attachmentCount: Math.max(
        missingAttachments,
        images.length + files.length,
      ),
    });
    setMentionQuery(null);
  };

  const send = () => {
    if (
      pending ||
      importing ||
      missingAttachments > 0 ||
      (!text.trim() && !images.length && !files.length)
    )
      return;
    const targets = [
      ...new Set(mentions.flatMap((mention) => mention.memberIds)),
    ];
    const messageId = draftMessageId.current ?? crypto.randomUUID();
    draftMessageId.current = messageId;
    saveDraft({
      text,
      mentions,
      messageId,
      attachmentCount: images.length + files.length,
    });
    const id = command(
      "send",
      { message_id: messageId, prompt: text, targets, images, files },
      true,
    );
    if (id && pendingRef.current) {
      pendingRef.current.messageId = messageId;
      pendingRef.current.draft = text;
      pendingRef.current.attachments = [...images, ...files];
    }
    following.current = true;
    setMentionQuery(null);
  };

  const openEditor = (roomId?: string) => {
    if (!roomId) {
      finishHistory();
      setHistoryError(null);
      restoreAnchor.current = null;
    }
    setEditor({
      name: "",
      specs: [defaultParticipant(profiles)],
      roomId,
    });
    setSettingsIndex(null);
    setDirectoryIndex(null);
    setError(null);
    if (compact) setSidebarOpen(false);
  };

  useEffect(() => {
    if (roomsLoaded && !roomId && !editor && !rooms.some(item => !item.archived))
      setEditor({ name: "", specs: [defaultParticipant(profiles)] });
  }, [roomsLoaded, roomId, editor, rooms, profiles]);

  const addParticipant = (kind: string, targetRoom?: string) => {
    const first = targetRoom ? undefined : editor?.specs[0];
    const spec = first
      ? { engine: first.engine, profile_id: first.profile_id }
      : defaultParticipant(profiles);
    if (targetRoom) {
      setMembersOpen(false);
      setEditor({ name: "", specs: [spec], roomId: targetRoom });
    } else if (kind === "new") {
      setEditor(current => current ? { ...current, specs: [...current.specs, spec] } : current);
    }
    if (kind === "existing") {
      setSessionSpec(spec);
      setSessionSearch("");
    }
  };

  const requestSessions = useCallback((spec: CollaborationMemberSpec) => {
    if (sessionsTimer.current !== null) window.clearTimeout(sessionsTimer.current);
    setSessionChoices([]);
    setSessionsError(null);
    const id = command("sessions", { member: spec });
    sessionsRequest.current = id;
    setSessionsLoading(!!id);
    if (id) sessionsTimer.current = window.setTimeout(() => {
      if (sessionsRequest.current !== id) return;
      sessionsRequest.current = null;
      setSessionsLoading(false);
      setSessionsError("读取会话超时，请重试。");
    }, HISTORY_REQUEST_TIMEOUT_MS);
  }, [command]);

  useEffect(() => {
    if (sessionSpec) requestSessions(sessionSpec);
    return () => {
      sessionsRequest.current = null;
      if (sessionsTimer.current !== null) window.clearTimeout(sessionsTimer.current);
    };
  }, [sessionSpec, requestSessions]);

  const selectSession = (session: CollaborationSession) => {
    if (!sessionSpec || session.unavailable) return;
    const spec = { ...sessionSpec, name: session.name, cwd: session.cwd, session_id: session.sid };
    setEditor(current => current ? {
      ...current, specs: current.roomId ? [spec] : [...current.specs, spec],
    } : current);
    setSessionSpec(null);
  };

  const updateSpec = (
    index: number,
    patch: Partial<CollaborationMemberSpec>,
  ) => {
    setEditor((current) =>
      current
        ? {
            ...current,
            specs: current.specs.map((spec, n) =>
              n === index ? { ...spec, ...patch } : spec,
            ),
          }
        : null,
    );
  };

  const chooseParticipant = (index: number, value: string) => {
    const choice = memberChoices.find(option => option.value === value);
    if (!choice) return;
    setEditor(current => current ? {
      ...current,
      specs: current.specs.map((spec, n) => {
        if (n !== index || spec.engine === choice.engine && spec.profile_id === choice.profile_id)
          return spec;
        const common = spec.engine === choice.engine ? { name: spec.name, cwd: spec.cwd } : {};
        return { ...common, engine: choice.engine, profile_id: choice.profile_id };
      }),
    } : null);
  };

  const openSettings = (index: number) => {
    const spec = editor?.specs[index];
    if (!spec) return;
    setSettingsIndex(index);
    if (spec.session_id) return;
    const key = roomCatalogKey(spec);
    if (!catalogs[key]) {
      setCatalogLoading(key);
      command("catalog", { member: spec });
    }
  };

  const openDirectory = (index: number, trigger: HTMLButtonElement) => {
    directoryTrigger.current = trigger;
    setDirectoryIndex(index);
  };

  const importFiles = async (list: FileList | File[] | null) => {
    if (importingRef.current || pending) return;
    importingRef.current = true;
    const targetRoom = roomIdRef.current;
    setImporting(true);
    try {
      const bytes = [...images, ...files].reduce(
        (sum, item) => sum + decodedSize(item.data),
        0,
      );
      const batch = await pickFiles(list, images.length + files.length, bytes);
      if (targetRoom !== roomIdRef.current) return;
      setImages((current) => [...current, ...batch.images]);
      setFiles((current) => [...current, ...batch.files]);
      const attachmentCount =
        images.length + files.length + batch.images.length + batch.files.length;
      draftMessageId.current = undefined;
      saveDraft({
        text,
        mentions,
        attachmentCount: Math.max(missingAttachments, attachmentCount),
      });
      if (
        images.length +
          files.length +
          batch.images.length +
          batch.files.length >=
        missingAttachments
      )
        setMissingAttachments(0);
      if (batch.errors.length) setError(batch.errors.join("\n"));
    } finally {
      importingRef.current = false;
      setImporting(false);
    }
  };

  const removeAttachment = (kind: "image" | "file", index: number) => {
    const nextImages =
      kind === "image" ? images.filter((_, i) => i !== index) : images;
    const nextFiles =
      kind === "file" ? files.filter((_, i) => i !== index) : files;
    setImages(nextImages);
    setFiles(nextFiles);
    draftMessageId.current = undefined;
    saveDraft({
      text,
      mentions,
      attachmentCount: Math.max(
        missingAttachments,
        nextImages.length + nextFiles.length,
      ),
    });
  };

  const nativeLinks = (member: CollaborationMember) =>
    member.sid && (
      <div className="collab-native-links">
        <span>
          <Icon name="folder" size={14} />
          <span title={member.cwd || member.spec.cwd || undefined}>
            {(member.cwd || member.spec.cwd)?.split("/").filter(Boolean).slice(-2).join("/")}
          </span>
        </span>
        <button type="button" onClick={event => {
          progressTrigger.current = event.currentTarget;
          setProgressMember(member);
        }}>
          查看进度 ↗
        </button>
        <button type="button" onClick={() => onNavigate(member)}>
          查看原生会话 ↗
        </button>
      </div>
    );

  const memberHeading = (member: CollaborationMember) => {
    const sid = member.native_sid || member.sid;
    return <span className="collab-member-heading">
      <strong title={member.name}>{member.name}</strong>
      {sid && <span className="collab-session-id" title={sid}>{shortRoomSessionId(sid)}</span>}
    </span>;
  };
  const memberDetail = (member: CollaborationMember) =>
    member.name !== roomMemberIdentity(member)
      && <span className="collab-member-meta">{roomMemberIdentity(member)}</span>;

  const loadOlder = (source?: "touch" | "wheel" | "scroll") => {
    const before = messages[0]?.seq;
    if (!room || creating || !before || !hasMore || historyRequest.current
        || completedHistoryBefore.current === before || restoreAnchor.current
        || source && historyError) return;
    const gate = source === "touch" ? touchHistoryGate.current
      : source === "wheel" ? wheelHistoryGate.current : null;
    if (gate && !gate.acquire()) return;
    const id = command("read", { before });
    if (!id) { gate?.complete(); return; }
    following.current = false;
    setHistoryError(null);
    setHistoryLoading(true);
    const timer = window.setTimeout(() => {
      if (historyRequest.current?.id !== id) return;
      finishHistory();
      setHistoryError("加载历史超时，请重试。");
    }, HISTORY_REQUEST_TIMEOUT_MS);
    historyRequest.current = { id, roomId: room.id, before, timer };
  };

  const agentActions = [
    { value: "new", label: "新建 Agent", icon: <Icon name="plus" size={16} /> },
    { value: "existing", label: "关联已有会话", icon: <Icon name="collaboration" size={16} /> },
  ];

  const loadingRoom = !room && (!roomsLoaded || roomId !== null);
  const loadingLabel = !connected ? "等待设备连接…"
    : error ? "聊天暂未载入" : "正在读取聊天…";

  const editorContent = editor && (
    <>
          {!editor.roomId && (
            <label className="collab-chat-name">
              聊天名称
              <input
                value={editor.name}
                placeholder="例如：接口重构讨论"
                onChange={(event) =>
                  setEditor({ ...editor, name: event.target.value })
                }
                maxLength={120}
              />
            </label>
          )}
          <h3>参与者</h3>
          <div className="collab-participants">
            {editor.specs.map((spec, index) => (
              <div className="collab-participant" key={index}>
                <EngineIcon engine={spec.engine} size={21} />
                <CollaborationSelect label={`选择参与者 ${index + 1}`}
                  className="collab-participant-identity"
                  value={`${spec.engine}:${spec.profile_id ?? ""}`}
                  options={memberChoices} disabled={!!pending || !!spec.session_id}
                  onChange={value => chooseParticipant(index, value)}>
                  <span className="collab-participant-copy">
                    <span className="collab-member-heading"><b>{names[spec.engine]} · {spec.profile_id ?? "default"}</b>
                      {spec.session_id && <span className="collab-session-id" title={spec.session_id.split("@").pop()}>{shortRoomSessionId(spec.session_id)}</span>}
                    </span>
                    {spec.name && <small>{spec.name}</small>}
                  </span>
                </CollaborationSelect>
                <button type="button" className="collab-participant-cwd"
                  aria-label={`工作目录 ${index + 1}`} title={spec.cwd || "默认目录"}
                  disabled={!!pending || !!spec.session_id}
                  onClick={event => openDirectory(index, event.currentTarget)}>
                  <Icon name="folder" size={15} /><span>{spec.cwd?.split("/").filter(Boolean).pop() || "默认目录"}</span>
                </button>
                <button
                  type="button"
                  className="iconbtn"
                  aria-label={`配置参与者 ${index + 1}`}
                  disabled={!!pending}
                  onClick={() => openSettings(index)}
                >
                  <Icon name="sliders" size={18} />
                </button>
              </div>
            ))}
          </div>
          {!editor.roomId && editor.specs.length < 16 && (
            <CollaborationSelect label="添加 Agent" value="" actions showChevron={false}
              className="collab-add-participant"
              disabled={!!pending || !memberChoices.length}
              options={agentActions} onChange={value => addParticipant(value)}
            >
              <Icon name="plus" size={16} />
              <span>添加 Agent</span>
            </CollaborationSelect>
          )}
          <div className="collab-dialog-footer">
            <button
              type="button"
              className="collab-primary"
              disabled={
                !!pending ||
                !connected ||
                !memberChoices.length ||
                (!editor.roomId && !editor.name.trim())
              }
              onClick={() => {
                setError(null);
                if (editor.roomId)
                  command(
                    "add_member",
                    { room_id: editor.roomId, member: editor.specs[0] },
                    true,
                  );
                else
                  command(
                    "create",
                    { name: editor.name.trim(), members: editor.specs },
                    true,
                  );
              }}
            >
              {pending ? "正在保存…" : editor.roomId ? "加入聊天" : "创建聊天"}
            </button>
          </div>
    </>
  );

  return (
    <SidebarShell controller={sidebar} className="collab-workspace">
      <div className={`scrim-side${sidebarOpen ? " show" : ""}`}
        onClick={() => setSidebarOpen(false)} />
      <aside
        ref={sidebarRef}
        className={`sessions collab-sidebar${sidebarOpen ? " show" : ""}`}
        inert={!sidebarOpen}
        aria-hidden={!sidebarOpen}
      >
        <div className="s-fade" aria-hidden="true" />
        <div className="s-inner">
          <header className="s-head">
            <button type="button" className="brand" onClick={() => setSidebarOpen(false)}
              aria-label="cc-remote，收起聊天列表">
              <span className="brand-mark"><Icon name="collaboration" size={20} /></span>
              <span className="name"><b>cc</b><span>·remote</span></span>
            </button>
            <button
              className="iconbtn"
              aria-label="收起"
              onClick={() => setSidebarOpen(false)}
            >
              <Icon name="chevrons-left" />
            </button>
          </header>
          <label className="search">
            <Icon name="search" size={17} />
            <input
              aria-label="搜索聊天"
              placeholder="搜索聊天…"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
          <div className="s-scroll collab-room-list">
            <div className="collab-list-heading">
              <span>{showArchived ? "已归档" : "最近聊天"}</span>
              <button
                className="iconbtn"
                title={showArchived ? "显示最近聊天" : "显示已归档"}
                aria-label="切换归档列表"
                onClick={() => setShowArchived((value) => !value)}
              >
                <Icon name="archive" size={15} />
              </button>
            </div>
            {shownRooms.map((item) => (
              <button
                type="button"
                key={item.id}
                className={`scard${!creating && roomId === item.id ? " active" : ""}`}
                onClick={() => chooseRoom(item.id)}
              >
                <strong>{item.name}</strong>
                <span>
                  <Icon name="collaboration" size={12} />
                  {new Date(item.updated * 1000).toLocaleDateString()}
                </span>
              </button>
            ))}
            {!shownRooms.length && (
              <p className="collab-muted">
                {!connected ? "等待设备连接"
                  : roomsLoaded ? "还没有聊天" : loadingLabel}
              </p>
            )}
          </div>
          <div className="s-foot">
            <button
              type="button"
              className="newbtn"
              onClick={() => openEditor()}
              disabled={!connected || !roomsLoaded}
            >
              <Icon name="plus" size={18} />
              新建聊天
            </button>
          </div>
        </div>
      </aside>
      <section className="pane code-pane collab-pane">
        <header className="c-head">
          <div className="titlewrap">
            <div className="ttl">
              <SidebarToggle
                className="surface-head-title"
                open={sidebarOpen}
                onOpenChange={setSidebarOpen}
              >
                <span className="surface-head-mark"><Icon name="collaboration" size={18} /></span>
                <span>协作</span>
              </SidebarToggle>
            </div>
            <div className="sub">{creating ? "新建聊天" : room?.name ?? rooms.find(item => item.id === roomId)?.name
              ?? (loadingRoom ? loadingLabel : "新建聊天")}</div>
          </div>
          <SessionStatus
            state={!connected ? "reconnecting" : working && !creating ? "running" : "idle"}
          />
          <button
            type="button"
            className={`device-trigger${connected ? " online" : ""}`}
            onClick={onDevice}
            aria-label="设备中心"
          >
            <Icon name="devices" size={18} />
            <span>{deviceName}</span>
            <i />
          </button>
          {room && !creating && (
            <button
              type="button"
              className="iconbtn collab-members-button"
              aria-label="管理成员"
              onClick={() => setMembersOpen(true)}
            >
              <Icon name="users" size={19} />
              <span>{activeMembers.length}</span>
            </button>
          )}
          <EngineSelector engine="collaboration" onChange={onEngineChange} />
        </header>
        {error && (
          <div className="collab-error" role="alert">
            <span>{error}</span>
            <button
              className="iconbtn"
              aria-label="关闭提示"
              onClick={() => setError(null)}
            >
              <Icon name="close" size={15} />
            </button>
          </div>
        )}
        <div className="collab-conversation-wrap">
        {historyLoading && !creating && (
          <div className="history-page-activity" role="status" aria-live="polite">
            <span className="history-page-activity-spinner" aria-hidden="true" />
            <span>正在加载更早历史…</span>
          </div>
        )}
        <div
          className="collab-conversation"
          role="region"
          aria-label="协作对话记录"
          tabIndex={0}
          aria-busy={creating ? undefined : loadingRoom || historyLoading}
          ref={scroll}
          onScroll={(event) => {
            const el = event.currentTarget;
            following.current =
              el.scrollHeight - el.scrollTop - el.clientHeight < 100;
            const movingUp = el.scrollTop < lastScrollTop.current - .5;
            lastScrollTop.current = el.scrollTop;
            if (movingUp && isAtHistoryEdge(el))
              loadOlder(touchY.current !== null ? "touch" : wheelTimer.current !== null ? "wheel" : "scroll");
          }}
          onWheel={event => {
            if (event.deltaY >= 0) return;
            wheelHistoryGate.current.beginGesture();
            if (wheelTimer.current !== null) window.clearTimeout(wheelTimer.current);
            wheelTimer.current = window.setTimeout(() => {
              wheelHistoryGate.current.endGesture();
              wheelTimer.current = null;
            }, 180);
            if (isAtHistoryEdge(event.currentTarget)) loadOlder("wheel");
          }}
          onTouchStart={event => {
            touchY.current = event.touches[0]?.clientY ?? null;
            touchHistoryGate.current.beginGesture();
          }}
          onTouchMove={event => {
            const y = event.touches[0]?.clientY;
            if (y !== undefined && touchY.current !== null && y > touchY.current
                && isAtHistoryEdge(event.currentTarget)) loadOlder("touch");
            touchY.current = y ?? null;
          }}
          onTouchEnd={() => { touchY.current = null; touchHistoryGate.current.endGesture(); }}
          onTouchCancel={() => { touchY.current = null; touchHistoryGate.current.endGesture(); }}
        >
          {creating ? (
            <div className="newchat code-newchat collab-newchat">
              <div className="newchat-card" role="region" aria-label="新建聊天" data-collab-editor>
                <div className="newchat-greet"><Icon name="collaboration" size={30} /><span>新建协作聊天</span></div>
                {editorContent}
              </div>
            </div>
          ) : loadingRoom ? (
            <div className="empty" role="status">
              <div className="spinner" aria-label="加载中" />
              <p className="loading-tx">{connected && !error ? "加载会话历史…" : loadingLabel}</p>
            </div>
          ) : !room ? (
            <div className="empty">
              <Icon name="collaboration" size={34} />
              <h2>开始协作聊天</h2>
              <button
                type="button"
                className="collab-primary"
                onClick={() => openEditor()}
                disabled={!connected}
              >
                新建聊天
              </button>
            </div>
          ) : (
            <div className="collab-thread">
              {hasMore && historyError && <div className="load-more-wrap collab-history-wrap">
                <button
                  type="button"
                  className="load-more-btn"
                  disabled={historyLoading || !connected}
                  onClick={() => loadOlder()}
                >
                  {historyError ? "重试加载历史" : "加载更早的历史"}
                </button>
              </div>}
              {historyError && <p className="collab-history-error" role="status">{historyError}</p>}
              {!messages.length && !deliveries.length && <div className="collab-room-empty">发一条消息开始讨论</div>}
              {messages.map((message) => {
                const member = message.member_id
                  ? lookup.get(message.member_id)
                  : null;
                return message.role === "user" ? (
                  <article className="collab-user" key={message.id} data-message-id={message.id}>
                    <div className="ubub">{message.text || "已发送附件"}</div>
                    {!!message.attachments?.length && (
                      <span className="collab-muted">
                        {message.attachments
                          .map((item) => item.name)
                          .join(" · ")}
                      </span>
                    )}
                    <time>
                      {new Date(message.created * 1000).toLocaleString()}
                    </time>
                  </article>
                ) : member ? (
                  <article
                    className="collab-answer"
                    key={message.id}
                    data-message-id={message.id}
                    data-engine={member.spec.engine}
                  >
                    <header>
                      <EngineIcon engine={member.spec.engine} size={20} />
                      <span className="collab-answer-identity">{memberHeading(member)}{memberDetail(member)}</span>
                    </header>
                    <CollaborationReply member={member} message={message} resources={resourceAccess} />
                    {message.truncated && (
                      <p className="collab-muted">完整回复可在原生会话查看。</p>
                    )}
                    {nativeLinks(member)}
                  </article>
                ) : null;
              })}
              {(room.members ?? []).map((member) => {
                const active = deliveries.filter(
                  (delivery) => delivery.member_id === member.id,
                );
                const latest =
                  active.findLast(
                    (item) =>
                      item.status === "unknown" || item.status === "stopping",
                  ) ??
                  active.findLast((item) =>
                    roomActiveStates.has(item.status),
                  ) ??
                  active[active.length - 1];
                if (
                  !latest ||
                  (["succeeded", "cancelled"].includes(latest.status) &&
                    messages.some(
                      (message) =>
                        message.member_id === member.id &&
                        message.reply_to.includes(latest.message_id),
                    ))
                )
                  return null;
                const running = roomActiveStates.has(latest.status);
                return (
                  <article
                    key={member.id}
                    className="collab-activity"
                    data-engine={member.spec.engine}
                  >
                    <header>
                      <EngineIcon engine={member.spec.engine} size={20} />
                      <span className="collab-answer-identity">{memberHeading(member)}{memberDetail(member)}</span>
                      <span
                        className={`collab-working${running ? " active" : ""}`}
                      >
                        {running && (
                          <EngineWorking
                            engine={member.spec.engine}
                            size={23}
                          />
                        )}
                        {running ? (
                          <WorkingWord
                            scope={`room:${roomId}:${member.id}:${latest.id}`}
                            label="正在处理"
                          />
                        ) : (
                          <span>{roomDeliveryLabel(latest.status)}</span>
                        )}
                      </span>
                      {running && (
                        <button
                          type="button"
                          className="iconbtn"
                          aria-label={`停止 ${member.name}`}
                          title="停止"
                          onClick={() =>
                            command("stop", { member_id: member.id })
                          }
                        >
                          <Icon name="stop" size={14} />
                        </button>
                      )}
                    </header>
                    {latest.error && (
                      <p className="collab-muted">{latest.error}</p>
                    )}
                    {latest.status === "unknown" && (
                      <div className="collab-reconcile-actions">
                        <button
                          className="collab-secondary"
                          onClick={() => command("reconcile")}
                        >
                          核对原生状态
                        </button>
                        <button
                          className="collab-secondary"
                          onClick={() => {
                            if (
                              window.confirm(
                                "结束这条输入的状态核对后，后续点名会继续投递。此前输入的实际结果可在原生会话中核对。",
                              )
                            ) {
                              command("dismiss", { delivery_id: latest.id });
                            }
                          }}
                        >
                          结束等待
                        </button>
                      </div>
                    )}
                    {nativeLinks(member)}
                  </article>
                );
              })}
              {questions.map((item) => {
                const member = lookup.get(item.member_id);
                return (
                  member && (
                    <QuestionSheet
                      key={item.question.ask_id}
                      engine={member.spec.engine}
                      question={item.question.question}
                      options={item.question.options}
                      allowText={item.question.allow_text}
                      multiSelect={item.question.multi_select}
                      header={member.name}
                      secret={item.question.secret}
                      questions={item.question.questions}
                      onAnswers={(answers) =>
                        !!command("answer", {
                          member_id: member.id,
                          ask_id: item.question.ask_id,
                          answers,
                        })
                      }
                      onAnswer={(answer) =>
                        !!command("answer", {
                          member_id: member.id,
                          ask_id: item.question.ask_id,
                          answer,
                        })
                      }
                    />
                  )
                );
              })}
            </div>
          )}
        </div>
        </div>
        {room && !creating && !room.archived && (
          <div
            className="collab-composer"
            onDragOver={(event) => event.preventDefault()}
            onDrop={(event) => {
              event.preventDefault();
              void importFiles(event.dataTransfer.files);
            }}
          >
            {missingAttachments > 0 && (
              <div className="collab-attachments">
                <button
                  type="button"
                  onClick={() => {
                    setMissingAttachments(0);
                    draftMessageId.current = undefined;
                    saveDraft({ text, mentions });
                  }}
                >
                  重新选择 {missingAttachments} 个附件，或点击移除 ×
                </button>
              </div>
            )}
            {(images.length > 0 || files.length > 0) && (
              <div className="collab-attachments">
                {images.map((_image, index) => (
                  <button
                    type="button"
                    key={`image-${index}`}
                    onClick={() => removeAttachment("image", index)}
                  >
                    图片 {index + 1} ×
                  </button>
                ))}
                {files.map((file, index) => (
                  <button
                    type="button"
                    key={`file-${index}`}
                    onClick={() => removeAttachment("file", index)}
                  >
                    {file.filename} ×
                  </button>
                ))}
              </div>
            )}
            {mentionQuery && (
              <div
                id={mentionListId}
                className="collab-mention"
                role="listbox"
                aria-label="点名会话"
              >
                <div className="collab-mention-title" role="presentation">点名会话</div>
                {mentionChoices.map((choice, index) => (
                  <button
                    type="button"
                    role="option"
                    aria-label={choice.label}
                    aria-describedby={`${mentionListId}-${index}-description`}
                    aria-selected={index === mentionIndex}
                    key={choice.id}
                    onPointerDown={(event) => event.preventDefault()}
                    onClick={() => chooseMention(choice.id)}
                  >
                    {choice.id === "all" ? (
                      <Icon name="users" size={18} />
                    ) : (
                      <EngineIcon
                        engine={lookup.get(choice.id)!.spec.engine}
                        size={18}
                      />
                    )}
                    <span className="collab-mention-copy"><span>{choice.label}</span>
                      <small id={`${mentionListId}-${index}-description`}>{choice.id === "all" ? "房间内全部会话" : roomMemberDescription(lookup.get(choice.id)!)}</small>
                    </span>
                  </button>
                ))}
                {!mentionChoices.length && <p className="collab-muted collab-mention-empty">没有匹配的会话</p>}
              </div>
            )}
            <div className="collab-input">
              <button
                type="button"
                className="iconbtn"
                aria-label="添加附件"
                disabled={importing || !!pending}
                onClick={() => fileInput.current?.click()}
              >
                <Icon name="paperclip" size={21} />
              </button>
              <input
                ref={fileInput}
                hidden
                type="file"
                multiple
                onChange={(event) => {
                  void importFiles(event.target.files);
                  event.target.value = "";
                }}
              />
              <textarea
                ref={input}
                value={text}
                placeholder="输入消息…"
                aria-label="协作消息"
                rows={1}
                maxLength={65536}
                onChange={(event) =>
                  updateText(event.target.value, event.target.selectionStart)
                }
                onPaste={(event) => {
                  if (event.clipboardData.files.length) {
                    event.preventDefault();
                    void importFiles(event.clipboardData.files);
                  }
                }}
                onKeyDown={(event) => {
                  if (event.nativeEvent.isComposing || event.keyCode === 229)
                    return;
                  if (
                    mentionQuery &&
                    ["ArrowDown", "ArrowUp"].includes(event.key)
                  ) {
                    event.preventDefault();
                    if (mentionChoices.length) setMentionIndex(
                      (value) =>
                        (value +
                          (event.key === "ArrowDown"
                            ? 1
                            : mentionChoices.length - 1)) %
                        mentionChoices.length,
                    );
                  } else if (mentionQuery && event.key === "Escape") {
                    event.preventDefault();
                    setMentionQuery(null);
                  } else if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    if (mentionQuery)
                      chooseMention(mentionChoices[mentionIndex]?.id);
                    else send();
                  }
                }}
              />
              <button
                type="button"
                className="collab-send"
                aria-label="发送消息"
                disabled={
                  !connected ||
                  !!pending ||
                  importing ||
                  missingAttachments > 0 ||
                  !activeMembers.length ||
                  (!text.trim() && !images.length && !files.length)
                }
                onClick={send}
              >
                <Icon name="send" size={20} />
              </button>
            </div>
            <footer>
              {importing ? <span role="status">正在读取附件…</span> : (
                <button
                  type="button"
                  className="collab-mention-trigger"
                  aria-label="点名参与者"
                  aria-haspopup="listbox"
                  aria-expanded={!!mentionQuery}
                  aria-controls={mentionQuery ? mentionListId : undefined}
                  disabled={!activeMembers.length}
                  onPointerDown={(event) => event.preventDefault()}
                  onClick={insertMention}
                >
                  @ 点名
                </button>
              )}
              <CollaborationSelect label="默认回复者" className="collab-responder"
                value={room.default_member ?? ""} disabled={!connected || !!pending}
                options={activeMembers.map(member => ({
                  value: member.id, label: member.name,
                  description: roomMemberDescription(member),
                  icon: <EngineIcon engine={member.spec.engine} size={18} />,
                }))}
                onChange={value => command("update", {
                  default_member_id: value, revision: room.revision,
                })}>
                {lookup.get(room.default_member ?? "") && <EngineIcon
                  engine={lookup.get(room.default_member!)!.spec.engine} size={17} />}
                <span>{lookup.get(room.default_member ?? "")?.name ?? "选择回复者"}</span>
              </CollaborationSelect>
            </footer>
          </div>
        )}
      </section>
      {membersOpen && room && (
        <Dialog title="聊天成员" error={error} onClose={() => setMembersOpen(false)} className="collab-members-dialog">
          <label className="collab-chat-name">
            聊天名称
            <input
              defaultValue={room.name}
              onBlur={(event) => {
                if (
                  event.target.value.trim() &&
                  event.target.value !== room.name
                )
                  command("update", {
                    name: event.target.value,
                    revision: room.revision,
                  });
              }}
            />
          </label>
          <div className="collab-members">
            {activeMembers.map((member) => (
              <div key={member.id} className="collab-member" data-member-id={member.id}>
                <EngineIcon engine={member.spec.engine} size={20} />
                <div className="collab-member-copy">{memberHeading(member)}
                  {memberDetail(member)}
                  <small title={member.cwd || member.spec.cwd || undefined}><Icon name="folder" size={12} />
                    <span>{(member.cwd || member.spec.cwd)?.split("/").filter(Boolean).slice(-2).join("/") || "默认目录"}</span>
                  </small>
                </div>
                {member.sid && (
                  <button
                    className="collab-secondary collab-member-native"
                    aria-label={`查看 ${member.name} 的原生会话`}
                    title="打开会话"
                    onClick={() => onNavigate(member)}
                  >
                    <Icon name="open-session" size={16} /><span>打开会话</span>
                  </button>
                )}
                <CollaborationSelect label={`${member.name} 的操作`} value="" actions showChevron={false}
                  className="collab-member-menu" disabled={!!pending}
                  options={[
                    { value: "rename", label: "重命名", disabled: !!room.archived },
                    { value: "native", label: "查看原生会话", disabled: !member.sid },
                    { value: "remove", label: "移出聊天", danger: true, disabled: !!room.archived },
                  ]} onChange={value => {
                    if (value === "rename") { setRenameValue(member.name); setRenameMember(member); }
                    else if (value === "native") onNavigate(member);
                    else command("remove_member", { member_id: member.id, revision: room.revision }, true);
                  }}><Icon name="dots" size={17} /></CollaborationSelect>
              </div>
            ))}
          </div>
          {!!room.members?.some(member => member.removed) && (
            <details className="collab-removed-members">
              <summary>已移出成员 <span>{room.members.filter(member => member.removed).length}</span></summary>
              {room.members.filter(member => member.removed).map(member => (
                <div className="collab-member" key={member.id}>
                  <EngineIcon engine={member.spec.engine} size={20} />
                  <div className="collab-member-copy">{memberHeading(member)}{memberDetail(member)}</div>
                  <button type="button" className="collab-secondary"
                    disabled={!!pending || !!room.archived || !connected || activeMembers.length >= 16}
                    title={activeMembers.length >= 16 ? "聊天最多容纳 16 位 Agent，请先移出一位成员。" : undefined}
                    aria-label={`恢复 ${member.name}`}
                    onClick={() => command("restore_member", { member_id: member.id, revision: room.revision }, true)}>恢复加入</button>
                </div>
              ))}
            </details>
          )}
          <div className="collab-dialog-footer">
            <CollaborationSelect label="添加 Agent" value="" actions showChevron={false}
              className="collab-secondary" disabled={!!room.archived || !!pending || activeMembers.length >= 16}
              options={agentActions} onChange={value => addParticipant(value, room.id)}>
              <Icon name="plus" size={16} /><span>添加 Agent</span>
            </CollaborationSelect>
            <button
              type="button"
              className="collab-secondary"
              onClick={() => {
                command("update", { archived: !room.archived });
                setMembersOpen(false);
                setShowArchived(!room.archived);
              }}
            >
              {room.archived ? "恢复聊天" : "归档聊天"}
            </button>
          </div>
        </Dialog>
      )}
      {renameMember && room && (
        <Dialog title="重命名会话" error={error} onClose={() => { if (!pending) setRenameMember(null); }}>
          <label className="collab-field">会话名称
            <input value={renameValue} maxLength={80} onChange={event => setRenameValue(event.target.value)} />
          </label>
          <p className="collab-muted">{roomMemberDescription(renameMember)}</p>
          <div className="collab-dialog-footer"><button type="button" className="collab-primary"
            disabled={!!pending || !renameValue.trim() || !connected}
            onClick={() => command("rename_member", { member_id: renameMember.id, name: renameValue.trim(), revision: room.revision }, true)}>
            {pending ? "正在保存…" : "保存"}
          </button></div>
        </Dialog>
      )}
      {sessionSpec && (
        <Dialog title="关联已有会话" onClose={() => setSessionSpec(null)} className="collab-session-dialog">
          <CollaborationSelect label="选择 Harness 与账号" value={`${sessionSpec.engine}:${sessionSpec.profile_id ?? ""}`}
            options={memberChoices} onChange={value => {
              const option = memberChoices.find(item => item.value === value);
              if (option) setSessionSpec({ engine: option.engine, profile_id: option.profile_id });
            }}>
            <EngineIcon engine={sessionSpec.engine} size={20} />
            <span>{names[sessionSpec.engine]} · {sessionSpec.profile_id ?? "default"}</span>
          </CollaborationSelect>
          <label className="search collab-session-search"><Icon name="search" size={16} />
            <input aria-label="搜索已有会话" placeholder="搜索会话、目录或 ID…" value={sessionSearch}
              onChange={event => setSessionSearch(event.target.value)} />
          </label>
          {sessionsLoading ? <div className="empty" role="status"><div className="spinner" /><p className="loading-tx">正在读取会话…</p></div>
            : sessionsError ? <div className="collab-session-error" role="status"><p>{sessionsError}</p>
              <button type="button" className="collab-secondary" onClick={() => requestSessions(sessionSpec)}>重试</button></div>
            : <div className="collab-session-list">
              {sessionChoices.filter(session => [session.name, session.cwd, session.native_sid].join(" ").toLocaleLowerCase()
                .includes(sessionSearch.trim().toLocaleLowerCase())).map(session => (
                <button type="button" key={session.sid} disabled={session.unavailable}
                  onClick={() => selectSession(session)}>
                  <EngineIcon engine={sessionSpec.engine} size={20} />
                  <span><strong>{session.name}</strong><small title={session.native_sid}>{session.cwd} · {shortRoomSessionId(session.native_sid)}</small>
                    {session.unavailable && <small>已关联聊天，可在原聊天中恢复成员</small>}
                  </span>
                </button>
              ))}
              {!sessionChoices.length && <p className="collab-muted">该账号暂无可关联的 Code 会话。</p>}
            </div>}
        </Dialog>
      )}
      {editor?.roomId && (
        <Dialog
          title={editor.roomId ? "添加 Agent" : "新建聊天"}
          onClose={() => {
            if (!pending) {
              setEditor(null);
              setSettingsIndex(null);
            }
          }}
          className="collab-create-dialog"
          error={error}
          icon={<Icon name="collaboration" size={22} />}
          aside={<span className="collab-editor-device">
            <Icon name="devices" size={16} /><span>{deviceName}</span>
          </span>}
        >
          {editorContent}
        </Dialog>
      )}
      {currentSettings && settingsIndex !== null && (
        <Dialog
          title={`${names[currentSettings.engine]} · ${currentSettings.profile_id ?? "default"}`}
          onClose={() => setSettingsIndex(null)}
          className="collab-settings-dialog"
          error={error}
          icon={<EngineIcon engine={currentSettings.engine} size={22} />}
        >
          <div className="collab-settings-basics">
            <label className="collab-setting-row">
              <span>会话名称</span>
              <input
                className="collab-setting-text"
                disabled={!!currentSettings.session_id}
                value={currentSettings.name ?? ""}
                placeholder="默认名称"
                maxLength={80}
                onChange={(event) =>
                  updateSpec(settingsIndex, { name: event.target.value })
                }
              />
            </label>
            <div className="collab-setting-row">
              <label htmlFor={settingsDirectoryId}>工作目录</label>
              <span className="collab-setting-directory">
                <input
                  id={settingsDirectoryId}
                  className="collab-setting-text collab-setting-path"
                  disabled={!!currentSettings.session_id}
                  value={currentSettings.cwd ?? ""}
                  placeholder="默认目录"
                  onChange={(event) =>
                    updateSpec(settingsIndex, { cwd: event.target.value || null })
                  }
                  onBlur={() => {
                    if (currentSettings && !currentSettings.session_id) {
                      setCatalogLoading(roomCatalogKey(currentSettings));
                      command("catalog", { member: currentSettings });
                    }
                  }}
                />
                <button
                  type="button"
                  className="iconbtn"
                  aria-label="选择工作目录"
                  disabled={!!currentSettings.session_id}
                  onMouseDown={event => event.preventDefault()}
                  onClick={event => openDirectory(settingsIndex, event.currentTarget)}
                >
                  <Icon name="folder" size={17} />
                </button>
              </span>
            </div>
          </div>
          {currentSettings.session_id && <p className="collab-muted">已关联会话 {currentSettings.session_id}，沿用其原有运行配置。</p>}
          {!currentSettings.session_id && <>
          {currentCatalog?.models.error && (
            <p className="collab-muted">{currentCatalog.models.error}</p>
          )}
          <label className="collab-field">
            模型
            <CollaborationSelect label="模型"
              value={currentSettings.model ?? ""}
              onChange={value =>
                updateSpec(settingsIndex, {
                  model: value || null,
                  effort: null,
                  service_tier: null,
                })
              }
              options={[
                { value: "", label: "默认", description: currentCatalog?.models.default_model ?? undefined },
                ...(currentCatalog?.models.models ?? []).map(model => ({
                  value: model.id, label: model.display_name || model.id,
                  description: model.description,
                })),
              ]} />
          </label>
          {!!selectedModel?.efforts.length && (
            <label className="collab-field">
              思考强度
              <CollaborationSelect label="思考强度"
                value={currentSettings.effort ?? ""}
                onChange={value =>
                  updateSpec(settingsIndex, {
                    effort: value || null,
                  })
                }
                options={[{ value: "", label: "默认" },
                  ...selectedModel.efforts.map(effort => ({ value: effort, label: effort }))]} />
            </label>
          )}
          {currentSettings.engine === "claude" && (
            <label className="collab-field">
              权限
              <CollaborationSelect label="权限"
                value={currentSettings.permission_mode ?? ""}
                onChange={value =>
                  updateSpec(settingsIndex, {
                    permission_mode: value || null,
                  })
                }
                options={[{ value: "", label: "默认" },
                  ...PERMS.map(mode => ({ value: mode.id, label: mode.name, description: mode.ds }))]} />
            </label>
          )}
          {currentSettings.engine === "codex" && (
            <>
              {!!currentCatalog?.permissions?.profiles.length && (
                <label className="collab-field">
                  执行环境
                  <CollaborationSelect label="执行环境"
                    value={currentSettings.permission_profile ?? ""}
                    onChange={value =>
                      updateSpec(settingsIndex, {
                        permission_profile: value || null,
                      })
                    }
                    options={[{ value: "", label: "默认" },
                      ...currentCatalog.permissions.profiles.map(profile => ({
                        value: profile.id, label: profile.id, disabled: !profile.allowed,
                      }))]} />
                </label>
              )}
              {!!selectedModel?.service_tiers?.length && (
                <label className="collab-field">
                  速度
                  <CollaborationSelect label="速度"
                    value={currentSettings.service_tier ?? ""}
                    onChange={value =>
                      updateSpec(settingsIndex, {
                        service_tier: value || null,
                      })
                    }
                    options={[{ value: "", label: "默认" },
                      ...selectedModel.service_tiers.map(tier => ({ value: tier.id, label: tier.name }))]} />
                </label>
              )}
            </>
          )}
          {currentSettings.engine === "dsh" && (
            <>
              <label className="collab-field">
                权限
                <CollaborationSelect label="权限"
                  value={currentSettings.dsh_permission ?? ""}
                  onChange={value =>
                    updateSpec(settingsIndex, {
                      dsh_permission: value || null,
                    })
                  }
                  options={[{ value: "", label: "默认" },
                    ...(currentCatalog?.models.dsh_permissions ?? []).map(permission => ({
                      value: permission.value, label: permission.name,
                    }))]} />
              </label>
              <label className="collab-field">
                Agent Preset
                <CollaborationSelect label="Agent Preset"
                  value={currentSettings.dsh_agent_preset ?? ""}
                  onChange={value =>
                    updateSpec(settingsIndex, {
                      dsh_agent_preset: value || null,
                    })
                  }
                  options={[{ value: "", label: "默认" },
                    ...(currentCatalog?.models.dsh_presets ?? []).filter(preset => preset.available)
                      .map(preset => ({ value: preset.id, label: preset.name }))]} />
              </label>
            </>
          )}
          </>}
          {editor && editor.specs.length > 1 && (
            <button type="button" className="collab-remove-draft" aria-label={`移除参与者 ${settingsIndex + 1}`}
              onPointerDown={event => event.preventDefault()}
              onClick={() => {
                setEditor(current => current ? { ...current, specs: current.specs.filter((_, index) => index !== settingsIndex) } : current);
                setSettingsIndex(null);
              }}>移除 Agent</button>
          )}
          <div className="collab-dialog-footer">
            <p className="collab-muted collab-catalog-status" role="status">
              {catalogLoading === roomCatalogKey(currentSettings) ? "正在读取原生配置…" : ""}
            </p>
            {!currentSettings.session_id && <button
              type="button"
              className="collab-secondary"
              onPointerDown={event => event.preventDefault()}
              onClick={() =>
                setEditor((current) =>
                  current
                    ? {
                        ...current,
                        specs: current.specs.map((spec, index) =>
                          index === settingsIndex
                            ? {
                                engine: spec.engine,
                                profile_id: spec.profile_id,
                              }
                            : spec,
                        ),
                      }
                    : null,
                )
              }
            >
              恢复默认
            </button>}
            <button
              type="button"
              className="collab-primary"
              onPointerDown={event => event.preventDefault()}
              onClick={() => setSettingsIndex(null)}
            >
              完成
            </button>
          </div>
        </Dialog>
      )}
      {directorySpec && directoryIndex !== null && (
        <Dialog
          title="选择本地工作区"
          onClose={() => setDirectoryIndex(null)}
          className="collab-directory-dialog"
          returnFocusRef={directoryTrigger}
        >
          <DirPicker
            open
            embedded
            path={directory?.path ?? null}
            parent={directory?.parent ?? null}
            dirs={directory?.dirs ?? []}
            responseRequestId={directory?.request_id}
            onBrowse={(path) => ws?.sendListDir(path) ?? null}
            initialPath={directorySpec.cwd}
            onConfirm={(cwd) => {
              const spec = { ...directorySpec, cwd };
              updateSpec(directoryIndex, { cwd });
              setCatalogLoading(roomCatalogKey(spec));
              command("catalog", { member: spec });
              setDirectoryIndex(null);
            }}
            onClose={() => setDirectoryIndex(null)}
            confirmLabel="使用此目录"
            waitForInitialBrowse
          />
        </Dialog>
      )}
      {progressMember && (
        <Suspense fallback={null}>
          <CollaborationProgress
            key={`${progressMember.id}:${lookup.get(progressMember.id)?.sid}`}
            member={lookup.get(progressMember.id) ?? progressMember}
            machineId={machineId}
            initialRuntime={getSessionRuntime((lookup.get(progressMember.id) ?? progressMember).sid!)}
            returnFocusRef={progressTrigger}
            historyPageCache={historyPageCache}
            ws={ws}
            listen={listenProgress}
            onClose={() => setProgressMember(null)}
            onNavigate={onNavigate}
            resources={resourceAccess}
          />
        </Suspense>
      )}
      {resourcePreview}
    </SidebarShell>
  );
}
