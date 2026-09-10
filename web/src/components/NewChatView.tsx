import type { DshPreset } from "../protocol";
// Empty-state "new chat" page: a centered composer (a la Claude app / Codex)
// with a working directory, optional model/effort overrides, and attachments.
// A null override is intentional: the wrapper/engine keeps its own local default.
/* oxlint-disable react/only-export-components */
import {
  lazy, Suspense, useEffect, useRef, useState, type ClipboardEvent,
} from "react";
import { Icon } from "../icons";
import {
  modelsFor, parseSlash, type Catalog, type Effort, type Model,
} from "../data";
import { attachmentBytes } from "../img";
import {
  readClipboardImport, resolveClipboardImport, insertClipboardText,
  type ClipboardImport,
} from "../clipboard-import";
import type { ClaudeProfileInfo, CodexPermissionMode, CodexProfileInfo, CodexServiceTier, CodexWebSearchMode, CollaborationModeName, PermissionProfileInfo, QueryImg, QueryFile, Space, WorkDashboard } from "../protocol";
import { ImeSubmitGuard } from "../ime-submit";
import { PendingImageAttachments } from "./PendingImageAttachments";
import { CommandSheet } from "./CommandSheet";
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

type Engine = "claude" | "codex" | "dsh";
import { newChatEfforts } from "../new-chat-selection";
export { compatibleNewChatEffort, newChatCatalogRequest, resolveNewChatLocalDefaults,
  reconcileNewChatSelection, newChatEfforts } from "../new-chat-selection";

interface Props {
  dshPresets?: DshPreset[];
  dshPreset?: string | null;
  dshError?: string | null;
  onPickDshPreset?: (value: string) => void;
  cwd: string;
  /** Authorization boundary for local execution-control choices. */
  controlScopeKey: string;
  space?: Space;
  engine?: Engine;  // which backend this new chat will use
  catalog?: Catalog;
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
  onPickModel?: (model: string | null) => void;
  onPickEffort?: (effort: string | null) => void;
  onPickAutoCompact?: (selection: AutoCompactSelection) => void;
  onPickClaudeProfile?: (profileId: string) => void;
  onPickCodexProfile?: (profileId: string) => void;
  permissionProfiles?: PermissionProfileInfo[] | null;
  onGetPermissionProfiles?: (cwd: string) => void;
  onSend: (prompt: string, images?: QueryImg[], files?: QueryFile[],
           collaborationMode?: CollaborationModeName,
           permissionMode?: CodexPermissionMode,
           permissionProfile?: string,
           webSearch?: CodexWebSearchMode,
           serviceTier?: CodexServiceTier) => boolean;
}

interface SelectorOption {
  id: string | null;
  name: string;
  description: string;
  icon: string;
}

interface NewChatExecutionControls {
  scopeKey: string;
  permissionMode: CodexPermissionMode;
  permissionProfile: string | null;
  webSearch: CodexWebSearchMode | null;
  serviceTier: CodexServiceTier;
}

const defaultExecutionControls = (
  scopeKey: string,
): NewChatExecutionControls => ({
  scopeKey,
  permissionMode: "never",
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
    <>
      <div className={"scrim" + (open ? " show" : "")} onClick={onClose} />
      <div className={"sheet" + (open ? " show" : "")}
        role="dialog" aria-label={title}>
        <div className="sheet-grip" />
        <div className="sheet-title">{title}</div>
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
      </div>
    </>
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

export function NewChatView({ cwd, controlScopeKey,
  space = "code", engine = "claude",
  catalog = {}, model = null, effort = null,
  autoCompact = { mode: "inherit", thresholdTokens: null },
  defaultModel = null, defaultEffort = null, autoFocus = true, createError,
  claudeProfiles = [], defaultClaudeProfileId = null, claudeProfileId = null,
  codexProfiles = [], defaultCodexProfileId = null, codexProfileId = null,
  workDashboard, selectedProjectId, onSelectProject, onManageWork, onPickCwd,
  onPickModel, onPickEffort, onPickAutoCompact, onPickClaudeProfile,
  onPickCodexProfile,
  permissionProfiles, onGetPermissionProfiles,
  dshPresets = [], dshPreset, dshError, onPickDshPreset,
  onSend }: Props) {
  const [text, setText] = useState("");
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
  const [permissionsOpen, setPermissionsOpen] = useState(false);
  const photoRef = useRef<HTMLInputElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const imeSubmitRef = useRef(new ImeSubmitGuard());
  const buttonSendTimerRef = useRef<number | null>(null);

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
    permissionMode,
    permissionProfile,
    webSearch,
    serviceTier,
  } = scopedExecutionControls;
  const fastSelected = serviceTier === "fast";
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
  const canSend = !(engine === "dsh" && (dshError || !dshPresets.length)) && (text.trim().length > 0 || hasAttachments || pastes.length > 0)
    && !creating && !importing && !selectedProfileMissing;
  const modelList = modelsFor(engine, catalog);
  const effectiveModel = model ?? defaultModel;
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

  const onPick = async (fl: FileList | File[] | null, clipboard?: ClipboardImport) => {
    if (importingRef.current) return;
    importingRef.current = true;
    setImporting(true);
    try {
      const [{ pickFiles }, imported] = await Promise.all([
        import("../attachment-import"),
        clipboard ? resolveClipboardImport(clipboard)
          : Promise.resolve({ files: fl ? Array.from(fl) : null, errors: [] }),
      ]);
      const batch = await pickFiles(
        imported.files, images.length + files.length, attachmentBytes(images, files));
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
    const clipboard = readClipboardImport(e.clipboardData);
    const pastedText = clipboard.text;
    const attachments = clipboard.files.length || clipboard.images.length
      || clipboard.errors.length;
    if (!attachments && pastedText.length <= LONG_PASTE_THRESHOLD) return;
    e.preventDefault();
    if (pastedText.length > LONG_PASTE_THRESHOLD) {
      setPastes((current) => [...current, makeComposerPaste(pastedText, uuid())]);
    } else if (pastedText) insertClipboardText(e.currentTarget, pastedText, setText);
    if (attachments) void onPick(null, clipboard);
  };

  const send = (value = taRef.current?.value ?? text) => {
    const command = parseSlash(value.trim());
    if (command?.slash === "open") {
      onPickCwd();
      setText("");
      return;
    }
    if (command?.slash === "autocompact") {
      if (engine !== "claude") {
        setAutoCompactNotice("创建 Codex 会话后，可单独设置压缩阈值。");
        setText("");
        return;
      }
      if (!command.args) {
        setAutoCompactOpen(true);
        setAutoCompactNotice(null);
        setText("");
        return;
      }
      const parsed = parseAutoCompactArgument(command.args);
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
    const composed = composePastePrompt(pastes, value.trim());
    if (!composed.ok) {
      window.alert(`消息内容超过上限（最多 ${composed.maxChars.toLocaleString()} 个字符）`);
      return;
    }
    const prompt = composed.prompt;
    if ((!prompt && !hasAttachments) || creating || importing
        || selectedProfileMissing) return;
    setCreating(true);
    const queued = onSend(
      prompt, images.length ? images : undefined, files.length ? files : undefined,
      engine === "codex" ? "default" : undefined,
      engine === "codex"
        ? (space === "work" ? "never" : permissionMode)
        : undefined,
      engine === "codex" && space === "code"
        ? (permissionProfile ?? undefined)
        : undefined,
      engine === "codex" && space === "code"
        ? (webSearch ?? undefined)
        : undefined,
      engine === "codex" ? serviceTier : undefined);
    if (!queued) setCreating(false);
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
  const profileSelector = showProfileSelector ? (
    <label className="newchat-profile">
      <span>账号</span>
      <select value={accountProfileId ?? ""}
        onChange={(event) => pickAccountProfile?.(event.target.value)}
        disabled={creating || importing || !pickAccountProfile}
        aria-label={`选择 ${engine === "codex" ? "Codex" : "Claude"} 账号`}>
        {selectedProfileMissing && accountProfileId && (
          <option value={accountProfileId} disabled>已移除账号</option>
        )}
        {accountProfiles.map((profile) => (
          <option key={profile.id} value={profile.id}>
            {codexProfilePresentation(
              accountProfiles,
              defaultAccountProfileId,
              profile.id,
            )?.fullLabel ?? profile.label}
            {profile.error ? " · 目录暂不可用" : ""}
          </option>
        ))}
      </select>
    </label>
  ) : null;
  const profileWarning = selectedProfileWarning ? (
    <div className="newchat-profile-error" role="status">
      <Icon name="warning" size={14} />
      <span>{selectedProfileWarning}</span>
    </div>
  ) : null;

  return (
    <div className={"newchat " + (space === "work" ? "work-newchat" : "code-newchat")}>
      <div className="newchat-card">
        <div className="newchat-greet">{space === "work"
          ? "开始一项工作"
          : engine === "codex" ? "开始 Codex 新对话" : "开始新对话"}
          <span className={`newchat-engine ${engine}`}>{engine === "dsh" ? "DSH" : engine === "codex" ? "◇ Codex" : "✳ Claude"}</span>
        </div>
        {engine === "dsh" && <div className="dsh-preset-row">
          <label>Agent Preset
            <select aria-label="DSH Agent Preset" value={dshPreset ?? ""}
              onChange={event => onPickDshPreset?.(event.target.value)} disabled={creating}>
              <option value="">DSH 默认</option>
              {dshPresets.map(preset => <option key={preset.id} value={preset.id} disabled={!preset.available}>
                {preset.name}{preset.available ? "" : " · 不可用"}
              </option>)}
            </select>
          </label>
          <p>{dshError || dshPresets.find(preset => dshPreset ? preset.id === dshPreset : preset.is_default)?.description || "正在读取设备上的 DSH…"}</p>
        </div>}
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
        ) : profileSelector ? (
          <div className="newchat-context">
            {profileSelector}
            {cwdButton}
            {profileWarning}
          </div>
        ) : (
          <>
            {cwdButton}
            {profileWarning}
          </>
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

        <textarea className="newchat-input"
          placeholder={space === "work" ? "描述要完成的工作，或上传文档、表格、演示…" : "发条消息开始…"} ref={taRef}
          value={text} onChange={(e) => {
            setText(e.target.value);
            setAutoCompactNotice(null);
          }} onPaste={onPaste}
          autoFocus={autoFocus} rows={3}
          disabled={creating || importing}
          onCompositionStart={() => imeSubmitRef.current.startComposition()}
          onCompositionEnd={(e) => {
            imeSubmitRef.current.endComposition();
            setText(e.currentTarget.value);
          }}
          onKeyDown={(e) => {
            if (!imeSubmitRef.current.shouldSubmitKey({
              key: e.key, shiftKey: e.shiftKey,
              isComposing: e.nativeEvent.isComposing, keyCode: e.nativeEvent.keyCode,
            })) return;
            e.preventDefault();
            send(e.currentTarget.value);
          }} />

        <div className="newchat-foot">
          <div className="newchat-ctls">
            <button type="button" className="cmdbtn"
              onClick={() => (space === "work"
                ? fileRef.current : photoRef.current)?.click()}
              aria-label={space === "work" ? "添加资料" : "添加照片"}
              title={space === "work" ? "添加资料" : "添加照片"}
              disabled={creating || importing}>
              <Icon name="plus" size={18} />
            </button>
            <input ref={photoRef} type="file" accept="image/*" multiple
              aria-label="添加照片" hidden
              onChange={(e) => { void onPick(e.target.files); e.target.value = ""; }} />
            <input ref={fileRef} type="file" multiple aria-label="添加文件" hidden
              onChange={(e) => { void onPick(e.target.files); e.target.value = ""; }} />
            <button type="button" className="hint-ctl"
              onClick={() => setSheetKind("models")}
              title="选择模型" disabled={creating || importing || !onPickModel}>
              {modelLabel}
            </button>
            <button type="button" className="hint-ctl"
              onClick={() => setSheetKind("efforts")}
              title="选择思考强度"
              disabled={creating || importing || !onPickEffort}>
              {effortLabel}
            </button>
            {engine === "codex" && space === "work" && (
              <button type="button"
                className={"hint-ctl fast-chip" + (fastSelected ? " on" : "")}
                aria-label="新工作 Fast 服务档位"
                aria-pressed={fastSelected}
                onClick={() => updateExecutionControls({
                  serviceTier: fastSelected ? "default" : "fast",
                })}
                title="Fast：快速 / 标准（首条消息生效）"
                disabled={creating || importing}>
                {fastSelected ? "快速" : "标准"}
              </button>
            )}
            {engine === "codex" && space === "code" && (
              <button type="button" className="newchat-access"
                onClick={() => {
                  setPermissionsOpen(true);
                  onGetPermissionProfiles?.(cwd);
                }}
                disabled={creating || importing}
                title={`执行环境：${permissionProfileLabel(permissionProfile) ?? "默认"}；审批：${permissionMode}；网页搜索：${webSearch ?? "默认"}`}>
                <Icon name="shield" size={15} />
                <span>{permissionProfileLabel(permissionProfile) ?? "默认环境"}</span>
                <span aria-hidden="true">▾</span>
              </button>
            )}
          </div>
          <div className="newchat-foot-right">
            <span className="newchat-hint">{createError
              ? `创建失败：${createError}`
              : importing ? "正在导入附件…"
                : creating ? "正在创建会话…"
                  : autoCompactNotice ?? "Enter 发送"}</span>
            <button className="newchat-send"
              onPointerDown={() => {
                if (imeSubmitRef.current.shouldCommitBeforeButtonSubmit()) taRef.current?.blur();
              }}
              onClick={requestButtonSend}
              disabled={!canSend}>
              <Icon name="send" size={16} />开始
            </button>
          </div>
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
      <>
        <div className={"scrim" + (autoCompactOpen ? " show" : "")}
          onClick={() => setAutoCompactOpen(false)} />
        <div className={"sheet auto-compact-sheet"
          + (autoCompactOpen ? " show" : "")}
          role="dialog" aria-label="新会话自动压缩">
          <div className="sheet-grip" />
          <div className="sheet-title">新会话自动压缩</div>
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
        </div>
      </>
      <CommandSheet
        open={permissionsOpen}
        kind="perms"
        engine={engine}
        onClose={() => setPermissionsOpen(false)}
        currentPerm={permissionMode}
        onPickPerm={(mode) => updateExecutionControls({
          permissionMode: mode as CodexPermissionMode,
        })}
        currentPermissionProfile={permissionProfile}
        permissionProfiles={permissionProfiles}
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
