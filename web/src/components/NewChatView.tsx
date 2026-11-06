import { dshCommandMatches } from "../dsh-commands";
import type { DshCommandInfo, DshPreset, DshPermissionOption } from "../protocol";
// Empty-state "new chat" page: a centered composer (a la Claude app / Codex)
// with a working directory, optional model/effort overrides, and attachments.
// A null override is intentional: the wrapper/engine keeps its own local default.
/* oxlint-disable react/only-export-components */
import {
  lazy, Suspense, useEffect, useLayoutEffect, useRef, useState, type ClipboardEvent,
} from "react";
import { EngineIcon, Icon } from "../icons";
import { SpeedPicker } from "./SpeedPicker";
import { newChatSpeed, normalizeSpeed, speedsFor } from "../codex-speed";
import {
  clientSlashesFor, matchCommands, matchSkills, modelsFor, permsFor, routeComposerInput, skillToken, slashToken, dshPermissionLabel,
  CODEX_PERMISSION_PROFILES, type Catalog, type Effort, type Model,
} from "../data";
import { attachmentBytes, snapshotAttachmentFiles } from "../img";
import {
  readClipboardImport, resolveClipboardImport, insertClipboardText,
  type ClipboardImport,
} from "../clipboard-import";
import type { ClaudeProfileInfo, PermissionMode, CodexProfileInfo, CodexServiceTier, CodexWebSearchMode, CollaborationModeName, EngineCapabilityItem, EngineCapabilityKind, PermissionProfileInfo, QueryImg, QueryFile, Space, WorkDashboard } from "../protocol";
import { ImeSubmitGuard } from "../ime-submit";
import { useClipboardPasteGuard } from "../use-clipboard-paste-guard";
import type { PasteReceipt } from "../clipboard-paste-guard";
import { PendingImageAttachments } from "./PendingImageAttachments";
import { CommandSheet } from "./CommandSheet";
import { CenteredSheet } from "./CenteredSheet";
import { ChoicePicker } from "./ChoicePicker";
import { AttachmentPicker } from "./AttachmentPicker";
import { permissionProfileLabel } from "../data";
import { codexProfilePresentation } from "../codex-profile-presentation";
import {
  composePastePrompt,
  LONG_PASTE_THRESHOLD,
  makeComposerPaste,
  type ComposerPaste,
} from "../composer-pastes";
import { PasteCards } from "./PasteCards";
import { uuid } from "../util";
import {
  autoCompactSelectionLabel,
  parseAutoCompactArgument,
  type AutoCompactSelection,
} from "../auto-compact";

const AutoCompactControl = lazy(() => import("./AutoCompactControl"));
const DshPresetPicker = lazy(() => import("./DshPresetPicker"));

type Engine = "claude" | "codex" | "dsh";
import { newChatEfforts } from "../new-chat-selection";
export { compatibleNewChatEffort, newChatCatalogRequest, resolveNewChatLocalDefaults,
  reconcileNewChatSelection, newChatEfforts } from "../new-chat-selection";

interface Props {
  editPrompt?: string | null;
  onEditConsumed?: () => void;
  dshCommands?: DshCommandInfo[];
  dshPresets?: DshPreset[];
  dshPermissions?: DshPermissionOption[];
  dshPreset?: string | null;
  dshError?: string | null;
  onPickDshPreset?: (value: string) => void;
  cwd: string;
  /** Authorization boundary for local execution-control choices. */
  controlScopeKey: string;
  space?: Space;
  engine?: Engine;  // which backend this new chat will use
  catalog?: Catalog;
  onRequestModels?: () => void;
  skills?: EngineCapabilityItem[];
  skillsOnline?: boolean;
  skillsError?: boolean;
  onRequestSkills?: () => void;
  onOpenExtensions?: (kind: EngineCapabilityKind | "all") => void;
  model?: string | null;
  effort?: string | null;
  autoCompact?: AutoCompactSelection;
  defaultModel?: string | null;
  defaultEffort?: string | null;
  claudeProfiles?: ClaudeProfileInfo[];
  defaultClaudeProfileId?: string | null;
  claudeProfileId?: string | null;
  codexProfiles?: CodexProfileInfo[];
  defaultCodexProfileId?: string | null;
  codexProfileId?: string | null;
  autoFocus?: boolean;
  createError?: string | null;
  workDashboard?: WorkDashboard | null;
  selectedProjectId?: string | null;
  onSelectProject?: (projectId: string | null) => void;
  onManageWork?: () => void;
  onPickCwd: () => void;  // open the directory picker
  onPickModel?: (model: string | null, source?: "command") => void;
  onPickEffort?: (effort: string | null) => void;
  onPickAutoCompact?: (selection: AutoCompactSelection) => void;
  onPickClaudeProfile?: (profileId: string) => void;
  onPickCodexProfile?: (profileId: string) => void;
  permissionProfiles?: PermissionProfileInfo[] | null;
  onGetPermissionProfiles?: (cwd: string) => void;
  onSend: (prompt: string, images?: QueryImg[], files?: QueryFile[],
           collaborationMode?: CollaborationModeName,
           permissionMode?: PermissionMode,
           permissionProfile?: string,
           webSearch?: CodexWebSearchMode,
           serviceTier?: CodexServiceTier, dshPermission?: string) => boolean;
}

interface SelectorOption {
  id: string | null;
  name: string;
  description: string;
  icon: string;
}

interface NewChatExecutionControls {
  scopeKey: string;
  collaborationMode: CollaborationModeName;
  permissionMode: PermissionMode | null;
  permissionProfile: string | null;
  webSearch: CodexWebSearchMode | null;
  serviceTier: CodexServiceTier;
  serviceTierScope?: string;
}

const defaultExecutionControls = (
  scopeKey: string,
): NewChatExecutionControls => ({
  scopeKey,
  collaborationMode: "default",
  permissionMode: null,
  permissionProfile: null,
  webSearch: null,
  serviceTier: "default",
});

function NewChatSelectorSheet({
  open, kind, options, current, onClose, onPick,
}: {
  open: boolean;
  kind: "models" | "efforts";
  options: SelectorOption[];
  current: string | null;
  onClose: () => void;
  onPick: (value: string | null) => void;
}) {
  const title = kind === "models" ? "选择模型" : "选择思考强度";
  return (
    <CenteredSheet open={open} label={title} onClose={onClose}>
        <div className="sheet-scroll">
          {options.map((option) => (
            <button key={option.id ?? "__local_default__"}
              className={"cmd" + (option.id === current ? " sel" : "")}
              onClick={() => onPick(option.id)}>
              <span className="cmd-ic"><Icon name={option.icon} size={17} /></span>
              <span className="cmd-tx">
                <span className="cmd-nm">{option.name}</span>
                <span className="cmd-ds">{option.description}</span>
              </span>
              {option.id === current
                ? <span className="cmd-check"><Icon name="check" size={19} /></span>
                : <span className="cmd-kbd" />}
            </button>
          ))}
        </div>
    </CenteredSheet>
  );
}

function displayModel(
  id: string | null | undefined, models: Model[],
): string | null {
  if (!id) return null;
  return models.find((candidate) => candidate.id === id)?.name ?? id;
}

function displayEffort(
  id: string | null | undefined, efforts: Effort[],
): string | null {
  if (!id) return null;
  return efforts.find((candidate) => candidate.id === id)?.name ?? id;
}

export function NewChatView({ cwd, controlScopeKey, editPrompt, onEditConsumed,
  space = "code", engine = "claude",
  catalog = {}, model = null, effort = null,
  autoCompact = { mode: "inherit", thresholdTokens: null },
  defaultModel = null, defaultEffort = null, autoFocus = true, createError,
  claudeProfiles = [], defaultClaudeProfileId = null, claudeProfileId = null,
  codexProfiles = [], defaultCodexProfileId = null, codexProfileId = null,
  workDashboard, selectedProjectId, onSelectProject, onManageWork, onPickCwd,
  onPickModel, onPickEffort, onPickAutoCompact, onPickClaudeProfile, onRequestModels,
  onPickCodexProfile, skills, skillsOnline = true, skillsError = false, onRequestSkills,
  permissionProfiles, onGetPermissionProfiles, onOpenExtensions,
  dshCommands, dshPresets = [], dshPermissions = [], dshPreset, dshError, onPickDshPreset,
  onSend }: Props) {
  const [text, setText] = useState("");
  useEffect(() => {
    if (editPrompt != null) { setText(editPrompt); onEditConsumed?.(); }
  }, [editPrompt, onEditConsumed]);
  const [images, setImages] = useState<QueryImg[]>([]);
  const [files, setFiles] = useState<QueryFile[]>([]);
  const [pastes, setPastes] = useState<ComposerPaste[]>([]);
  const [importing, setImporting] = useState(false);
  const importingRef = useRef(false);
  const [creating, setCreating] = useState(false);
  const [sheetKind, setSheetKind] =
    useState<"models" | "efforts" | null>(null);
  const [autoCompactOpen, setAutoCompactOpen] = useState(false);
  const [autoCompactNotice, setAutoCompactNotice] = useState<string | null>(null);
  const [executionControls, setExecutionControls] =
    useState<NewChatExecutionControls>(
      () => defaultExecutionControls(controlScopeKey));
  const [dshPermissionChoice, setDshPermissionChoice] = useState<{scope: string; value: string} | null>(null);
  const dshPermission = dshPermissionChoice?.scope === controlScopeKey ? dshPermissionChoice.value : "";
  const [permissionsOpen, setPermissionsOpen] = useState(false);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const pasteGuard = useClipboardPasteGuard(taRef, `${controlScopeKey}:${cwd}:${engine}`);
  const imeSubmitRef = useRef(new ImeSubmitGuard());
  const buttonSendTimerRef = useRef<number | null>(null);
  useEffect(() => {
    if (engine === "dsh" && skillsOnline && !skillsError && dshCommands === undefined) onRequestSkills?.();
  }, [engine, skillsOnline, skillsError, dshCommands, onRequestSkills]);
  const commandToken = slashToken(text);
  const commandMatches = commandToken === null ? [] : [...matchCommands(commandToken, engine, space),
    ...(engine === "dsh" ? dshCommandMatches(commandToken, {commands: dshCommands ?? [], agent_preset: dshPreset}) : [])];
  // Work gets a private cwd only when created; the Code cwd cannot describe it.
  const currentSkillToken = (engine === "codex" || engine === "dsh") && space === "code" ? skillToken(text) : null;
  const skillMatches = currentSkillToken !== null && skills ? matchSkills(currentSkillToken, skills) : [];
  const skillLoading = currentSkillToken !== null && skills === undefined;
  const skillOpen = currentSkillToken !== null
    && (skillLoading || skillsError || currentSkillToken === "" || skillMatches.length > 0);
  const suggestionOpen = commandMatches.length > 0 || skillOpen;
  // The App's request coordinator owns in-flight deduplication. A component
  // latch would outlive reads cancelled by reconnect or cache invalidation.
  useEffect(() => {
    if (currentSkillToken === null || skills !== undefined || !skillsOnline || skillsError) return;
    onRequestSkills?.();
  }, [currentSkillToken, skills, skillsOnline, skillsError, onRequestSkills]);

  useLayoutEffect(() => {
    const textarea = taRef.current;
    if (!textarea || space !== "code") return;
    const grow = () => {
      textarea.style.height = "auto";
      textarea.style.height = `${Math.min(textarea.scrollHeight, 132)}px`;
    };
    grow();
    // Rotation, desktop resizing and the phone keyboard can change wrapping
    // without changing the draft.
    window.addEventListener("resize", grow);
    return () => window.removeEventListener("resize", grow);
  }, [text, space]);

  useEffect(() => {
    if (createError) setCreating(false);
  }, [createError]);

  useEffect(() => {
    setExecutionControls((current) => (
      current.scopeKey === controlScopeKey
        ? current
        : defaultExecutionControls(controlScopeKey)
    ));
    setPermissionsOpen(false);
    setAutoCompactOpen(false);
    setAutoCompactNotice(null);
  }, [controlScopeKey]);

  useEffect(() => {
    setExecutionControls((current) => {
      const scoped = current.scopeKey === controlScopeKey
        ? current
        : defaultExecutionControls(controlScopeKey);
      return scoped.permissionProfile === null
        ? scoped
        : { ...scoped, permissionProfile: null };
    });
  }, [controlScopeKey, cwd]);

  useEffect(() => () => {
    if (buttonSendTimerRef.current !== null) {
      window.clearTimeout(buttonSendTimerRef.current);
    }
  }, []);

  const hasAttachments = images.length > 0 || files.length > 0;
  // Effects run after paint. Derive the fail-closed defaults synchronously too,
  // so a send in the first frame after a device/surface switch cannot reuse
  // another authorization scope's choices.
  const scopedExecutionControls =
    executionControls.scopeKey === controlScopeKey
      ? executionControls
      : defaultExecutionControls(controlScopeKey);
  const {
    collaborationMode,
    permissionMode,
    permissionProfile,
    webSearch,
    serviceTier: requestedServiceTier,
  } = scopedExecutionControls;
  const updateExecutionControls = (
    patch: Partial<Omit<NewChatExecutionControls, "scopeKey">>,
  ) => {
    setExecutionControls((current) => ({
      ...(current.scopeKey === controlScopeKey
        ? current
        : defaultExecutionControls(controlScopeKey)),
      ...patch,
    }));
  };
  const accountProfiles = engine === "dsh" ? [] : engine === "codex" ? codexProfiles : claudeProfiles;
  const defaultAccountProfileId = engine === "codex"
    ? defaultCodexProfileId : defaultClaudeProfileId;
  const accountProfileId = engine === "codex"
    ? codexProfileId : claudeProfileId;
  const selectedAccountProfile = accountProfiles.find(
    (profile) => profile.id === accountProfileId) ?? null;
  const selectedProfileMissing = !!accountProfileId
    && selectedAccountProfile === null;
  // A catalog read can fail while direct app-server startup still succeeds.
  // Warn without treating that transient read as an authentication verdict.
  const selectedProfileWarning = selectedProfileMissing
    ? `所选 ${engine === "codex" ? "Codex" : "Claude"} 账号已移除，请重新选择。`
    : selectedAccountProfile?.error ?? null;
  const canSend = !(engine === "dsh" && (dshError || !dshPresets.some(p => p.id === dshPreset && p.available))) && (text.trim().length > 0 || hasAttachments || pastes.length > 0)
    && !creating && !importing && !selectedProfileMissing;
  const modelList = modelsFor(engine, catalog);
  const effectiveModel = model ?? defaultModel;
  const speedScope = `${controlScopeKey}:${accountProfileId}:${effectiveModel}`;
  const serviceTier = newChatSpeed(
    scopedExecutionControls.serviceTierScope === speedScope ? requestedServiceTier : "default",
    effectiveModel, catalog);
  useEffect(() => {
    setExecutionControls((current) => current.serviceTier === "default"
      ? current : { ...current, serviceTier: "default" });
  }, [effectiveModel, accountProfileId]);
  const effortList = newChatEfforts(engine, effectiveModel, catalog);
  const localModelName = displayModel(defaultModel, modelList);
  const localEffortName = model === null
    ? displayEffort(defaultEffort, effortList) : null;
  const modelLabel = model
    ? displayModel(model, modelList) ?? model
    : localModelName ? `本机默认 · ${localModelName}` : "本机默认";
  const effortLabel = effort
    ? displayEffort(effort, effortList) ?? effort
    : localEffortName ? `默认 · ${localEffortName}` : "默认";
  const modelOptions: SelectorOption[] = [{
    id: null,
    name: "本机默认",
    description: localModelName
      ? `使用本机配置 · ${localModelName}`
      : "不发送模型覆盖，使用本机配置",
    icon: "cpu",
  }, ...modelList.map((candidate) => ({
    id: candidate.id,
    name: candidate.name,
    description: candidate.ds,
    icon: candidate.ic,
  }))];
  const effortOptions: SelectorOption[] = [{
    id: null,
    name: "默认",
    description: localEffortName
      ? `使用本机配置 · ${localEffortName}`
      : "不发送强度覆盖，使用所选模型或本机配置",
    icon: "gauge3",
  }, ...effortList.map((candidate) => ({
    id: candidate.id,
    name: candidate.name,
    description: candidate.ds,
    icon: candidate.ic,
  }))];

  const onPick = async (fl: FileList | File[] | null, clipboard?: ClipboardImport, paste?: PasteReceipt) => {
    if (importingRef.current) return;
    importingRef.current = true;
    setImporting(true);
    try {
      const [{ pickFiles }, imported] = await Promise.all([
        import("../attachment-import"),
        clipboard ? resolveClipboardImport(clipboard)
          : Promise.resolve(snapshotAttachmentFiles(fl, images.length + files.length)),
      ]);
      const batch = await pickFiles(
        imported.files, images.length + files.length, attachmentBytes(images, files));
      if (paste && !pasteGuard.acceptAttachments(paste, batch)) return;
      if (batch.images.length) setImages((previous) => [...previous, ...batch.images]);
      if (batch.files.length) setFiles((previous) => [...previous, ...batch.files]);
      const errors = [...imported.errors, ...batch.errors];
      if (errors.length) window.alert(errors.join("；"));
    } catch {
      window.alert("附件导入失败，请重新添加；已输入的文字会保留。");
    } finally {
      importingRef.current = false;
      setImporting(false);
    }
  };

  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const clipboard = readClipboardImport(e.clipboardData, images.length + files.length);
    const paste = pasteGuard.capture(e.currentTarget, clipboard);
    const pastedText = clipboard.text;
    const attachments = clipboard.files.length || clipboard.images.length
      || clipboard.errors.length;
    if (!paste.acceptText) {
      e.preventDefault();
      if (!attachments) return;
    }
    if (!attachments && pastedText.length <= LONG_PASTE_THRESHOLD) return;
    e.preventDefault();
    if (paste.acceptText && pastedText.length > LONG_PASTE_THRESHOLD) {
      setPastes((current) => [...current, makeComposerPaste(pastedText, uuid())]);
    } else if (paste.acceptText && pastedText) insertClipboardText(e.currentTarget, pastedText, setText);
    if (attachments) void onPick(null, clipboard, paste);
  };

  const submitPrompt = (value: string, mode = collaborationMode, permission = permissionMode) => {
    const composed = composePastePrompt(pastes, value.trim());
    if (!composed.ok) {
      window.alert(`消息内容超过上限（最多 ${composed.maxChars.toLocaleString()} 个字符）`);
      return;
    }
    const prompt = composed.prompt;
    if ((!prompt && !hasAttachments) || creating || importingRef.current
        || selectedProfileMissing) return;
    setCreating(true);
    const queued = onSend(
      prompt, images.length ? images : undefined, files.length ? files : undefined,
      engine === "codex" ? mode : undefined,
      engine === "codex" ? permission ?? "never" : permission ?? undefined,
      engine === "codex" ? permissionProfile ?? undefined : undefined,
      engine === "codex" ? webSearch ?? undefined : undefined,
      engine === "codex" ? serviceTier : undefined, engine === "dsh" ? dshPermission || undefined : undefined);
    if (!queued) setCreating(false);
  };

  const runClientSlash = (slash: string, args: string) => {
    const extensionKinds: Partial<Record<string, EngineCapabilityKind | "all">> = {
      extensions: "all", skills: "skill", plugins: "plugin",
      apps: "app", mcp: "mcp", hooks: "hook",
    };
    const extensionKind = extensionKinds[slash];
    if (extensionKind) {
      if (space === "work") {
        setAutoCompactNotice("Work 会在创建后分配独立目录，请开始对话后查看扩展。");
      } else if (onOpenExtensions) {
        onOpenExtensions(extensionKind);
        setText("");
        setAutoCompactNotice(null);
      } else setAutoCompactNotice("扩展目录当前不可用。");
      return;
    }
    if (slash === "model") {
      if (args) {
        const selected = modelList.find(candidate =>
          candidate.id === args || candidate.name.toLowerCase() === args.toLowerCase());
        if (!onPickModel) {
          setAutoCompactNotice("模型设置当前不可用。");
          return;
        }
        // Native engines accept hidden/provider IDs that discovery may omit.
        onPickModel(selected?.id ?? args, "command");
        setAutoCompactNotice(`已选择模型：${selected?.name ?? args}`);
      } else {
        onRequestModels?.();
        setSheetKind("models");
      }
      setText("");
      return;
    }
    if (slash === "open") {
      onPickCwd();
      setAutoCompactNotice(args ? "请在目录选择器中选择工作目录。" : null);
      setText("");
      return;
    }
    if (slash === "clear") {
      setText(""); setImages([]); setFiles([]); setPastes([]);
      setAutoCompactNotice(null);
      return;
    }
    if (slash === "permissions") {
      setPermissionsOpen(true);
      if (engine === "codex") onGetPermissionProfiles?.(cwd);
      setText("");
      return;
    }
    if ((slash === "plan" || slash === "normal") && engine === "claude") {
      const permission = slash === "plan" ? "plan" : "bypassPermissions";
      updateExecutionControls({ permissionMode: permission });
      if (args) submitPrompt(args, collaborationMode, permission);
      else {
        setText("");
        setAutoCompactNotice(slash === "plan" ? "新会话将使用计划模式。" : "已切换为 Bypass Permissions。");
      }
      return;
    }
    if ((slash === "plan" || slash === "normal") && engine === "codex") {
      const nextMode = slash === "plan" ? "plan" : "default";
      updateExecutionControls({ collaborationMode: nextMode });
      if (args) submitPrompt(args, nextMode);
      else {
        setText("");
        setAutoCompactNotice(nextMode === "plan" ? "新会话将使用计划模式。" : "已切换为普通模式。");
      }
      return;
    }
    if (slash === "fast" && engine === "codex") {
      const available = speedsFor(effectiveModel, catalog);
      const fastTier = normalizeSpeed("fast");
      if (!available.known) {
        onRequestModels?.();
        setAutoCompactNotice("正在读取当前模型支持的速度，请稍后重试。");
        return;
      }
      if (!available.options.some(option => option.id === fastTier)) {
        setAutoCompactNotice("当前模型不支持快速模式。");
        return;
      }
      const nextTier = serviceTier === fastTier ? "default" : fastTier;
      updateExecutionControls({ serviceTier: nextTier, serviceTierScope: speedScope });
      setText("");
      setAutoCompactNotice(nextTier === fastTier ? "已切换为快速模式。" : "已切换为标准速度。");
      return;
    }
    if (slash === "autocompact") {
      if (engine !== "claude") {
        setAutoCompactNotice("创建 Codex 会话后，可单独设置压缩阈值。");
        setText("");
        return;
      }
      if (!args) {
        setAutoCompactOpen(true);
        setAutoCompactNotice(null);
        setText("");
        return;
      }
      const parsed = parseAutoCompactArgument(args);
      if (!parsed.ok) {
        setAutoCompactNotice(parsed.error);
        return;
      }
      if (!onPickAutoCompact) {
        setAutoCompactNotice("自动压缩设置当前不可用。");
        return;
      }
      onPickAutoCompact(parsed.selection);
      setAutoCompactNotice(
        `已设置自动压缩：${autoCompactSelectionLabel(parsed.selection)}`,
      );
      setText("");
      return;
    }
    // These act on a native session. Keep the draft (including any arguments)
    // and do not create a model turn pretending that the command was executed.
    setAutoCompactNotice(`/${slash} 需要已有会话，请开始对话后使用。`);
  };

  const send = (value = taRef.current?.value ?? text) => {
    if (creating || importingRef.current || selectedProfileMissing) return;
    const route = routeComposerInput(value, engine, space);
    if (route.kind === "empty") return;
    if (route.kind === "blocked") { setAutoCompactNotice(route.message); return; }
    if (route.kind === "client") { runClientSlash(route.slash, route.args); return; }
    submitPrompt(route.prompt);
  };

  const pickCommand = (slash: string) => {
    if (commandMatches.find(command => command.slash === slash)?.unavailable) return;
    if (creating || importingRef.current || selectedProfileMissing) return;
    if (slash !== "autocompact" && clientSlashesFor(engine).has(slash)) {
      runClientSlash(slash, "");
    } else {
      setText(`/${slash} `);
      taRef.current?.focus();
    }
  };
  const pickSkill = (name: string) => {
    setText(`${engine === "dsh" ? "/" : "$"}${name} `);
    taRef.current?.focus();
  };

  const requestButtonSend = () => {
    if (buttonSendTimerRef.current !== null) return;
    buttonSendTimerRef.current = window.setTimeout(() => {
      buttonSendTimerRef.current = null;
      send();
    }, 0);
  };
  const cwdButton = (
    <button className="newchat-cwd" onClick={onPickCwd}
      title="更改工作目录" disabled={creating}>
      <Icon name="folder" size={16} />
      <span className="newchat-cwd-path">
        {cwd === "~" ? "~ · 主目录" : (cwd || "未指定目录")}
      </span>
      <Icon name="edit" size={13} />
    </button>
  );
  const showProfileSelector = accountProfiles.length > 1
    || selectedProfileMissing;
  const pickAccountProfile = engine === "codex"
    ? onPickCodexProfile : onPickClaudeProfile;
  const profileSelector = engine === "dsh" ? (
    <Suspense fallback={null}><DshPresetPicker presets={dshPresets} value={dshPreset}
      onChange={onPickDshPreset} disabled={creating || importing} /></Suspense>
  ) : showProfileSelector ? (
    <ChoicePicker key={`${controlScopeKey}:account`} className="newchat-profile"
      showChevron={space === "work"}
      label={`选择 ${engine === "codex" ? "Codex" : "Claude"} 账号`}
      value={accountProfileId ?? ""} onChange={value => pickAccountProfile?.(value)}
      disabled={creating || importing || !pickAccountProfile}
      options={[
        ...(selectedProfileMissing && accountProfileId
          ? [{ value: accountProfileId, label: "已移除账号", disabled: true }] : []),
        ...accountProfiles.map(profile => ({ value: profile.id,
          label: codexProfilePresentation(accountProfiles, defaultAccountProfileId, profile.id)?.fullLabel ?? profile.label,
          description: profile.error ? "目录暂不可用" : undefined, icon: "user",
        })),
      ]}>
      <Icon name="user" size={14} />{space === "work" && <span>账号</span>}
      <b>{selectedProfileMissing ? "已移除账号" : codexProfilePresentation(
        accountProfiles, defaultAccountProfileId, accountProfileId,
      )?.fullLabel ?? "选择账号"}</b>
    </ChoicePicker>
  ) : null;
  const profileWarning = selectedProfileWarning ? (
    <div className="newchat-profile-error" role="status">
      <Icon name="warning" size={14} />
      <span>{selectedProfileWarning}</span>
    </div>
  ) : null;
  const input = (
    <textarea className="newchat-input" data-chat-composer
      placeholder={space === "work" ? "描述要完成的工作，或上传文档、表格、演示…"
        : engine === "codex" ? "输入 / 命令，$ Skill" : "输入 / 命令"}
      aria-label="新会话消息" ref={taRef}
      value={text} onChange={(e) => {
        setText(e.target.value);
        setAutoCompactNotice(null);
      }} onPaste={onPaste}
      autoFocus={autoFocus} rows={space === "work" ? 3 : 1}
      disabled={creating || importing}
      onCompositionStart={() => imeSubmitRef.current.startComposition()}
      onCompositionEnd={(e) => {
        imeSubmitRef.current.endComposition();
        setText(e.currentTarget.value);
      }}
      onKeyDown={(e) => {
        if (e.key === "Tab" && e.shiftKey) {
          e.preventDefault();
          if (engine === "codex") {
            updateExecutionControls({ collaborationMode: collaborationMode === "plan" ? "default" : "plan" });
          } else {
            const modes = permsFor(engine);
            const current = modes.findIndex(mode => mode.id === permissionMode);
            updateExecutionControls({ permissionMode: modes[(current + 1) % modes.length].id as PermissionMode });
          }
          return;
        }
        if (e.key === "Escape" && suggestionOpen) {
          e.preventDefault(); setText(""); return;
        }
        if (!imeSubmitRef.current.shouldSubmitKey({
          key: e.key, shiftKey: e.shiftKey,
          isComposing: e.nativeEvent.isComposing, keyCode: e.nativeEvent.keyCode,
        })) return;
        e.preventDefault();
        if (commandMatches.length && commandToken) {
          pickCommand(commandMatches[0].slash);
          return;
        }
        if (skillMatches.length && currentSkillToken !== null) {
          pickSkill(skillMatches[0].name); return;
        }
        if (commandToken === "" || currentSkillToken === "") return;
        send(e.currentTarget.value);
      }} />
  );
  const attachmentPicker = (
    <AttachmentPicker key={controlScopeKey} onPick={onPick}
      label={space === "work" ? "添加资料" : "添加附件"}
      disabled={creating || importing} />
  );
  const sendButton = (
    <button className="newchat-send" aria-label="开始" title="开始新对话"
      onPointerDown={() => {
        if (imeSubmitRef.current.shouldCommitBeforeButtonSubmit()) taRef.current?.blur();
      }}
      onClick={requestButtonSend} disabled={!canSend}>
      <Icon name="send" size={20} />{space === "work" && "开始"}
    </button>
  );
  const accessControl = engine === "dsh" ? (
    <button type="button" className="newchat-access" title="选择权限模式"
      onClick={() => setPermissionsOpen(true)} disabled={creating || importing}>
      <Icon name="shield" size={14} /><span>{dshPermission ? dshPermissionLabel(dshPermission,
        dshPermissions.find(p => p.value === dshPermission)?.name) : "默认权限"}</span>
    </button>
  ) : engine === "codex" ? (
    <button type="button" className="newchat-access"
      onClick={() => {
        setPermissionsOpen(true);
        onGetPermissionProfiles?.(cwd);
      }}
      disabled={creating || importing}
      title={`执行环境：${permissionProfileLabel(permissionProfile) ?? "默认"}；审批：${permissionMode ?? "never"}；网页搜索：${webSearch ?? "默认"}`}>
      <Icon name="shield" size={14} />
      <span>{permissionProfileLabel(permissionProfile) ?? (space === "work" ? "Work 私有工作区" : "默认环境")}</span>
    </button>
  ) : (
    <button type="button" className="newchat-access" title="选择权限模式"
      onClick={() => setPermissionsOpen(true)} disabled={creating || importing}>
      <Icon name={permsFor(engine).find(mode => mode.id === permissionMode)?.ic ?? "shield"} size={14} />
      <span>{permsFor(engine).find(mode => mode.id === permissionMode)?.short ?? (space === "work" ? "Accept Edits" : "默认权限")}</span>
    </button>
  );

  return (
    <div className={"newchat " + (space === "work" ? "work-newchat" : "code-newchat")}>
      <div className="newchat-card">
        <div className="newchat-greet">
          <EngineIcon engine={engine} size={30} />
          <span>{space === "work" ? "开始一项工作" : "开始新对话"}</span>
        </div>
        {space === "work" ? (
          <>
            <div className="work-private-note"><Icon name="lock" size={14} />
              默认只访问这项工作的私有目录；需要其他资料时直接上传。
            </div>
            {(profileSelector || profileWarning) && (
              <div className="newchat-context work-profile-context">
                {profileSelector}
                {profileWarning}
              </div>
            )}
            <div className="work-project-bar">
              <select value={selectedProjectId ?? ""}
                onChange={(event) => onSelectProject?.(event.target.value || null)}>
                <option value="">不归入项目</option>
                {(workDashboard?.projects ?? []).map((project) =>
                  <option key={project.project_id} value={project.project_id}>{project.name}</option>)}
              </select>
              <button type="button" onClick={onManageWork}><Icon name="folder" size={15} />管理项目与资料</button>
            </div>
            {workDashboard && <div className="work-overview">
              <span>{workDashboard.projects.length} 个项目</span>
              <span>{workDashboard.sources.length} 份资料</span>
              <span>{workDashboard.schedules.length} 个定时任务</span>
              <span>{workDashboard.plugins.length} 个工作模板</span>
            </div>}
          </>
        ) : (
          <div className="newchat-context">
            {profileSelector}
            {cwdButton}
            {profileWarning}
            {engine === "dsh" && dshError && <p role="alert" className="newchat-profile-error">{dshError}</p>}
          </div>
        )}

        {space === "work" && !text && !hasAttachments && pastes.length === 0 && (
          <div className="work-starters" aria-label="常用工作类型">
            {[
              ["read", "整理文档", "帮我整理这份资料，输出一份结构清晰的文档。"],
              ["plan", "分析表格", "分析我上传的表格，找出关键结论并生成图表。"],
              ["book", "建立资料库", "把我提供的资料整理成可持续补充的知识库。"],
              ["spark", "制作演示", "根据我提供的内容制作一份演示文稿。"],
            ].map(([icon, label, prompt]) => (
              <button key={label} type="button" onClick={() => {
                setText(prompt); window.setTimeout(() => taRef.current?.focus(), 0);
              }}><Icon name={icon} size={16} /><span>{label}</span></button>
            ))}
          </div>
        )}

        {(hasAttachments || pastes.length > 0) && (
          <div className="attach show newchat-attach">
            <PendingImageAttachments images={images}
              onRemove={(index) => setImages((previous) =>
                previous.filter((_, candidate) => candidate !== index))} />
            {files.map((f, i) => (
              <span key={i} className="attach-file">
                <Icon name="read" size={14} />
                <span className="attach-fn">{f.filename}</span>
                <button className="attach-x" onClick={() => setFiles(files.filter((_, j) => j !== i))} aria-label="移除"><Icon name="close" size={12} /></button>
              </span>
            ))}
            <PasteCards pastes={pastes} onChange={setPastes}
              disabled={creating || importing} />
          </div>
        )}

        <div className="newchat-editor">
          {suggestionOpen && !creating && !importing && (
            <div className="cmd-pop" role="listbox" aria-label={skillOpen ? "Skills" : "命令"} data-lock-horizontal-swipe>
              {commandMatches.map(command => (
                <button key={command.slash} type="button" className="cmd" disabled={command.unavailable}
                  onClick={() => pickCommand(command.slash)}>
                  <span className="cmd-ic"><Icon name={command.ic} size={17} /></span>
                  <span className="cmd-tx">
                    <span className="cmd-nm"><span className="slash">/{command.slash}</span></span>
                    <span className="cmd-ds">{command.slash === "model"
                      ? "选择模型，或输入 /model 模型名称"
                      : `${command.name} — ${command.ds}`}</span>
                  </span>
                  <span className="cmd-kbd">↵</span>
                </button>
              ))}
              {skillOpen && (!skillsOnline ? (
                <div className="cmd-empty">连接恢复后将重新读取 Skills…</div>
              ) : skillsError ? (
                <>
                  <div className="cmd-empty" role="status">Skills 读取失败，请重试。</div>
                  <button type="button" className="cmd" onClick={onRequestSkills}>
                    <span className="cmd-tx"><span className="cmd-nm">重试读取 Skills</span></span>
                  </button>
                </>
              ) : skillLoading ? (
                <div className="cmd-empty">正在读取 Skills…</div>
              ) : skillMatches.length > 0 ? skillMatches.map(skill => (
                <button key={skill.name} type="button" className="cmd" onClick={() => pickSkill(skill.name)}>
                  <span className="cmd-ic"><Icon name="spark" size={17} /></span>
                  <span className="cmd-tx">
                    <span className="cmd-nm"><span className="slash">${skill.name}</span></span>
                    <span className="cmd-ds">{skill.description || "调用此 Skill"}</span>
                  </span>
                  <span className="cmd-kbd">↵</span>
                </button>
              )) : <div className="cmd-empty">当前目录没有已启用的 Skills</div>)}
            </div>
          )}
          {space === "code" ? (
            <div className="newchat-inputbar">
              {attachmentPicker}{input}{sendButton}
            </div>
          ) : input}
        </div>

        <div className="newchat-foot">
          {accessControl}
          <div className="newchat-ctls">
            {space === "work" && attachmentPicker}
            {space === "code" && engine === "codex" && collaborationMode === "plan" && (
              <button type="button" className="hint-ctl newchat-plan"
                disabled={creating || importing}
                title="计划模式，点击切换为普通模式"
                onClick={() => updateExecutionControls({ collaborationMode: "default" })}>
                <Icon name="plan" size={13} />计划
              </button>
            )}
            <button type="button" className="hint-ctl"
              onClick={() => { onRequestModels?.(); setSheetKind("models"); }}
              title="选择模型" disabled={creating || importing || !onPickModel}>
              <span title={modelLabel}>{space === "code"
                ? displayModel(effectiveModel, modelList) ?? "本机默认" : modelLabel}</span>
            </button>
            <button type="button" className="hint-ctl"
              onClick={() => setSheetKind("efforts")}
              title="选择思考强度"
              disabled={creating || importing || !onPickEffort}>
              <span title={effortLabel}>{space === "code"
                ? displayEffort(effort, effortList) ?? localEffortName ?? "默认" : effortLabel}</span>
            </button>
            {engine === "codex" && (
              <SpeedPicker newSession model={effectiveModel} catalog={catalog}
                value={serviceTier} scopeKey={`${controlScopeKey}:${accountProfileId}`}
                disabled={creating || importing} onRefresh={onRequestModels}
                onChange={(tier) => updateExecutionControls({
                  serviceTier: tier, serviceTierScope: speedScope,
                })} />
            )}
          </div>
          {(space === "work" || createError || importing || creating || autoCompactNotice) && (
            <div className="newchat-foot-right">
            <span className="newchat-hint" role="status">{createError
              ? `创建失败：${createError}`
              : importing ? "正在导入附件…"
                : creating ? "正在创建会话…"
                  : autoCompactNotice ?? "Enter 发送"}</span>
            {space === "work" && sendButton}
          </div>
          )}
        </div>
      </div>

      <NewChatSelectorSheet
        open={sheetKind !== null}
        kind={sheetKind ?? "models"}
        options={sheetKind === "efforts" ? effortOptions : modelOptions}
        current={sheetKind === "efforts" ? effort : model}
        onClose={() => setSheetKind(null)}
        onPick={(value) => {
          if (sheetKind === "efforts") onPickEffort?.(value);
          else onPickModel?.(value);
          setSheetKind(null);
        }}
      />
      <CenteredSheet open={autoCompactOpen} label="新会话自动压缩"
        className="auto-compact-sheet" onClose={() => setAutoCompactOpen(false)}>
          <div className="sheet-scroll">
            <Suspense fallback={
              <div className="ctx-pop-loading">读取自动压缩设置…</div>}>
              <AutoCompactControl value={autoCompact}
                newSession
                disabled={creating || importing}
                onChange={(selection) => {
                  onPickAutoCompact?.(selection);
                  setAutoCompactNotice(
                    `已设置自动压缩：${autoCompactSelectionLabel(selection)}`,
                  );
                }} />
            </Suspense>
          </div>
      </CenteredSheet>
      <CommandSheet
        open={permissionsOpen}
        kind="perms"
        engine={engine}
        dshPermissions={engine === "dsh" ? dshPermissions : undefined}
        onClose={() => setPermissionsOpen(false)}
        currentPerm={engine === "dsh" ? dshPermission : permissionMode ?? (engine === "codex" ? "never" : undefined)}
        onPickPerm={(mode) => {
          if (engine === "dsh") {
            setDshPermissionChoice({ scope: controlScopeKey, value: mode });
            setPermissionsOpen(false);
            return;
          }
          updateExecutionControls({ permissionMode: mode as PermissionMode });
          if (engine === "claude") setPermissionsOpen(false);
        }}
        onUseDefaultPerm={engine === "claude" || engine === "dsh" ? () => {
          if (engine === "dsh") setDshPermissionChoice({ scope: controlScopeKey, value: "" });
          else updateExecutionControls({ permissionMode: null });
          setPermissionsOpen(false);
        } : undefined}
        currentPermissionProfile={permissionProfile ?? (space === "work" ? "cc_remote_work" : null)}
        permissionProfiles={space === "work" ? [
          { id: "cc_remote_work", allowed: true },
          ...(permissionProfiles ?? CODEX_PERMISSION_PROFILES).filter(profile => profile.id !== "cc_remote_work"),
        ] : permissionProfiles}
        onPickPermissionProfile={(profile) => updateExecutionControls({
          permissionProfile: profile,
        })}
        currentWebSearch={webSearch}
        onPickWebSearch={(mode) => updateExecutionControls({
          webSearch: mode,
        })}
      />
    </div>
  );
}
