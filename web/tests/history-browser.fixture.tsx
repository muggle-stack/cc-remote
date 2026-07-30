import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";

import "../src/index.css";
import { OMITTED_PROCESS_ITEM_ID, type Turn } from "../src/reducer";
import type {
  PermissionProfileInfo,
  QueryFile,
  QueryImg,
  Space,
} from "../src/protocol";
import {
  ChatView,
} from "../src/components/ChatView";
import type { TextSelectionGuard } from "../src/history-selection-guard";
import { PendingImageAttachments } from "../src/components/PendingImageAttachments";
import { UsageMeter } from "../src/components/UsageMeter";
import { NewChatView } from "../src/components/NewChatView";
import {
  QueuedQueryChip,
  QueuedQueryDialog,
  type QueuedQueryEditor,
} from "../src/components/QueuedQueryDialog";
import { DirPicker } from "../src/components/DirPicker";

const LONG_PERMISSION_PROFILE_ID =
  `custom-profile-${"authorization-boundary-".repeat(12)}`.slice(0, 256);
const QUEUED_FULL_PROMPT = [
  "Review the complete deployment plan before changing any files.",
  ...Array.from(
    { length: 24 },
    (_, index) => `Requirement ${index + 1}: preserve queued execution order.`,
  ),
  "QUEUED-INSTRUCTION-END",
].join("\n");

const ROBOT_CORE_MERMAID_SOURCE = `flowchart TB
    USER["任务入口<br/>语音 · 文本 · App · API"]

    subgraph CORE["① 通用 Robot Agent Core｜所有机器人共用"]
        INPUT["任务理解<br/>目标 · 约束 · 优先级"]
        PLAN["任务规划器<br/>生成 Skill DAG"]
        CHECK["计划校验器<br/>能力 · 前置条件 · 风险 · 资源"]
        EXEC["任务执行器<br/>反馈 · 超时 · 取消 · 重试 · 恢复"]
        REG["能力注册中心<br/>Capability Registry"]
        MEM["任务状态与记忆<br/>事件日志 · 世界状态 · 经验"]
        OBS["策略与可观测性<br/>权限 · Trace · 指标 · 审计"]
        INPUT --> PLAN --> CHECK --> EXEC
        REG --> PLAN
        REG --> CHECK
        MEM <--> PLAN
        MEM <--> EXEC
        OBS -.监控与约束.-> CHECK
        OBS -.监控与约束.-> EXEC
    end

    subgraph CONTRACT["② Robot Capability Contract｜核心解耦点"]
        PROFILE["Robot Profile<br/>本体 · 传感器 · 末端 · 坐标系"]
        SKILL["Semantic Skill API<br/>goal · feedback · result<br/>cancel · timeout · error"]
        STATE["统一状态模型<br/>位置 · 电量 · 模式 · 故障 · 能力状态"]
        PROFILE --> SKILL
        STATE --> SKILL
    end

    subgraph ADAPTER["③ 本体适配层｜每种机器人单独实现"]
        DOG["四足 Adapter<br/>Go2 / Go1 / B2"]
        HUMAN["人形 Adapter<br/>G1 / H1 / 青龙 / 天工"]
        OTHER["其他机器人 Adapter<br/>轮式 · 机械臂 · 无人机"]
        SIM["仿真 Adapter<br/>Mock · MuJoCo · Gazebo"]
    end

    subgraph CONTROL["④ 实时控制与安全层｜不交给 LLM"]
        DOGCTRL["四足控制<br/>导航 · 步态 · 姿态 · HAL"]
        HUMANCTRL["人形控制<br/>平衡 · 全身控制 · 操作"]
        OTHERCTRL["设备控制器<br/>厂商 SDK · ROS 2 Control"]
        SIMCTRL["仿真控制器<br/>动力学 · 传感器 · 故障注入"]
        SAFETY["独立安全域<br/>急停 · 限幅 · 看门狗<br/>失联停车 · 人员接管"]
    end

    subgraph EMBODIMENT["⑤ 实际本体或数字本体"]
        DOGBOT["四足机器人"]
        HUMANBOT["人形机器人"]
        OTHERBOT["其他机器人"]
        SIMBOT["数字机器人"]
    end

    USER --> INPUT
    EXEC -->|"语义目标"| SKILL
    SKILL -->|"能力发现 / 调用 / 反馈"| DOG
    SKILL --> HUMAN
    SKILL --> OTHER
    SKILL --> SIM
    DOG --> DOGCTRL --> DOGBOT
    HUMAN --> HUMANCTRL --> HUMANBOT
    OTHER --> OTHERCTRL --> OTHERBOT
    SIM --> SIMCTRL --> SIMBOT
    SAFETY -.监控并可中断.-> DOGCTRL
    SAFETY -.监控并可中断.-> HUMANCTRL
    SAFETY -.监控并可中断.-> OTHERCTRL
    SAFETY -.安全事件反馈.-> EXEC`;

function finalTurn(id: string, paragraphs: number): Turn {
  const text = Array.from(
    { length: paragraphs },
    (_, index) => `${id} 的第 ${index + 1} 段动态高度内容，用于验证历史分页后的真实浏览器布局。`,
  ).join("\n\n");
  return {
    id,
    prompt: `用户问题 ${id}`,
    blocks: [{
      kind: "text",
      message_id: `${id}-message`,
      channel: "final",
      text,
      done: true,
    }],
    done: true,
    ts: Date.now(),
    doneTs: Date.now(),
  };
}

const INITIAL = [
  finalTurn("o1", 8),
  finalTurn("o2", 8),
  finalTurn("o3", 8),
  finalTurn("o4", 8),
];
function olderPage(page: number): Turn[] {
  const prefix = page === 1 ? "n" : `p${page}-`;
  return Array.from(
    { length: 8 },
    (_, index) => finalTurn(`${prefix}${index + 1}`, index === 7 ? 2 : 4),
  );
}
const SESSION_B = Array.from(
  { length: 4 },
  (_, index) => finalTurn(`b${index + 1}`, 6),
);

function timelineTurn(id: string): Turn {
  return {
    ...finalTurn(id, 3),
    blocks: [
      {
        kind: "process",
        item_id: `${id}-plan`,
        processKind: "plan",
        phase: "end",
        status: "completed",
        title: "计划",
        summary: "这个展开状态应跨虚拟卸载保留。",
        done: true,
      },
      {
        kind: "text",
        message_id: `${id}-reasoning`,
        channel: "thinking",
        text: "这段思考应当一次点击展开，并在虚拟卸载后保留状态。",
        done: true,
      },
      ...finalTurn(id, 3).blocks,
    ],
  };
}

function streamingTurn(id: string, paragraphs = 1): Turn {
  const text = Array.from(
    { length: paragraphs },
    (_, index) => `${id} 正在输出第 ${index + 1} 段，这些内容会让最新一轮持续增高。`,
  ).join("\n\n");
  return {
    id,
    prompt: `用户问题 ${id}`,
    blocks: [{
      kind: "text",
      message_id: `${id}-message`,
      channel: "final",
      text,
      done: false,
    }],
    done: false,
    ts: Date.now(),
  };
}

function dualImageTurn(): Turn {
  return {
    id: "dual-image",
    prompt: "这条消息只应占用一行图片布局",
    images: [{
      media_type: "image/png",
      data: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9WlK4h8AAAAASUVORK5CYII=",
    }],
    imageRefs: [{
      image_id: "history-image-1",
      media_type: "image/png",
      width: 1,
      height: 1,
      byte_size: 68,
    }],
    blocks: [],
    done: true,
    ts: Date.now(),
    doneTs: Date.now(),
  };
}

function compactToolsTurn(): Turn {
  return {
    id: "compact-tools",
    prompt: "连续工具调用应保持紧凑",
    blocks: [
      {
        kind: "tool",
        message_id: "compact-tool-message-1",
        tool_use_id: "compact-tool-1",
        tool: "shell",
        input: { command: "git status --short --branch" },
        done: true,
        result: { content: "clean", is_error: false },
      },
      {
        kind: "tool",
        message_id: "compact-tool-message-2",
        tool_use_id: "compact-tool-2",
        tool: "web_search",
        input: { query: "compact tool rows" },
        done: true,
        result: { content: "result", is_error: false },
      },
      {
        kind: "tool",
        message_id: "compact-tool-message-3",
        tool_use_id: "compact-tool-3",
        tool: "web_search",
        input: { query: "dense activity list" },
        done: true,
        result: { content: "result", is_error: false },
      },
    ],
    done: true,
    ts: Date.now(),
    doneTs: Date.now(),
  };
}

type DetailFixturePage = "deferred" | "latest" | "older";

function detailPagingTurn(
  page: DetailFixturePage,
  expanded = false,
  retainedPreview = false,
): Turn {
  const finalBlock = finalTurn("detail-page", 2).blocks[0];
  if (page === "deferred") {
    return {
      id: "detail-page",
      prompt: "加载这个超长回合的过程详情",
      blocks: retainedPreview ? [
        {
          kind: "process",
          item_id: "detail-retained-preview",
          processKind: "command",
          phase: "end",
          status: "succeeded",
          title: "已缓存的较新命令",
          command: "fixture-retained-preview",
          output: "摘要页仍保留了一小段过程。",
          done: true,
        },
        {
          kind: "process",
          item_id: OMITTED_PROCESS_ITEM_ID,
          processKind: "compaction",
          phase: "snapshot",
          status: "succeeded",
          title: "较早过程已省略",
          summary: "为限制此回合的内存占用，较早的处理记录未显示。",
          done: true,
        },
        finalBlock,
      ] : [finalBlock],
      done: true,
      ts: Date.now(),
      doneTs: Date.now(),
      detailEventCount: 24,
      detailLoaded: false,
    };
  }
  const pages: Array<Exclude<DetailFixturePage, "deferred">> =
    page === "older" ? ["older", "latest"] : ["latest"];
  const process = pages.flatMap((detailPage) => {
    const prefix = detailPage === "older" ? "较早" : "较新";
    return Array.from({ length: 4 }, (_, index) => ({
      kind: "process" as const,
      item_id: `detail-${detailPage}-${index}`,
      processKind: "command" as const,
      phase: "end" as const,
      status: "completed" as const,
      title: `${prefix}命令 ${index + 1}`,
      command: `fixture-${detailPage}-${index + 1}`,
      output: Array.from(
        { length: expanded && index === 1 ? 18 : 4 },
        (__, line) => `${prefix}过程 ${index + 1}.${line + 1}`,
      ).join("\n"),
      done: true,
    }));
  });
  return {
    id: "detail-page",
    prompt: "加载这个超长回合的过程详情",
    blocks: [...process, finalBlock],
    done: true,
    ts: Date.now(),
    doneTs: Date.now(),
    detailEventCount: 24,
    detailLoaded: true,
    detailHasMore: page === "latest",
    detailOldestCursor: page === "latest" ? "detail-older" : undefined,
    detailHasNewer: false,
    detailNewerCursor: undefined,
    detailAutoLoad: page === "latest",
  };
}

function mermaidTurn(invalid = false, source?: string): Turn {
  const text = invalid
    ? "```mermaid\nthis is not a supported diagram\n```"
    : source
      ? `\`\`\`mermaid\n${source}\n\`\`\``
    : [
        "```mermaid",
        "flowchart LR",
        "  A[Start] --> B[Done]",
        "  click A \"https://example.com\"",
        "```",
        "",
        "```mermaid",
        "sequenceDiagram",
        "  Alice->>Bob: Hello",
        "```",
      ].join("\n");
  return {
    id: invalid ? "invalid-mermaid" : "mermaid",
    prompt: "渲染 Mermaid 图表",
    blocks: [{
      kind: "text",
      message_id: invalid ? "invalid-mermaid-message" : "mermaid-message",
      channel: "final",
      text,
      done: true,
    }],
    done: true,
    ts: Date.now(),
    doneTs: Date.now(),
  };
}

function mathTurn(): Turn {
  return {
    id: "math",
    prompt: "渲染数学公式",
    blocks: [{
      kind: "text",
      message_id: "math-message",
      channel: "final",
      text: String.raw`\[ r = \frac{h}{\sin |\alpha|} \]

Inline: \(h = r \sin \alpha\).`,
      done: true,
    }],
    done: true,
    ts: Date.now(),
    doneTs: Date.now(),
  };
}

interface FixtureSession {
  turns: Turn[];
  cursor: string;
  hasMore: boolean;
  pagesLoaded: number;
  hasNewer?: boolean;
  newerPagesLoaded?: number;
  windowEpoch?: number;
}

export function HistoryBrowserFixture() {
  const params = useMemo(() => new URLSearchParams(window.location.search), []);
  const delayMs = Number(params.get("delay") ?? "30");
  const growthDelayMs = Number(params.get("growth-delay") ?? "500");
  const manualGrowth = params.has("manual-growth");
  const largeCount = Number(params.get("large") ?? "0");
  const pageCount = Math.max(1, Number(params.get("pages") ?? "1"));
  const large = largeCount > 0;
  const timeline = params.has("timeline");
  const interactiveTimeline = params.has("interactive-timeline");
  const dualImage = params.has("dual-image");
  const compactTools = params.has("compact-tools");
  const detailPaging = params.has("detail-paging");
  const detailRetainedPreview = params.has("detail-retained-preview");
  const detailScrollCancel = params.has("detail-scroll-cancel");
  const mermaid = params.has("mermaid");
  const actualMermaid = params.has("actual-mermaid");
  const invalidMermaid = params.has("invalid-mermaid");
  const mermaidHistory = params.has("mermaid-history");
  const math = params.has("math");
  const composerAttachment = params.has("composer-attachment");
  const composerResize = params.has("composer-resize");
  const quotaComposer = params.has("quota-composer");
  const queuedQueryFixture = params.has("queued-query-editor");
  const migrationPickerFixture = params.has("migration-picker")
    || params.has("migration-picker-null");
  const migrationPickerNullInitial = params.has("migration-picker-null");
  const newChatControls = params.has("newchat-controls");
  const longProfile = params.has("long-profile");
  const recoveryReplacement = params.has("recovery-replace");
  const deepBrowse = params.has("deep-browse");
  const runtimeBrowse = params.has("runtime-browse");
  const timelineEngine = params.get("engine") === "claude" ? "claude" : "codex";
  const emptyFinalPage = params.has("empty-final");
  const initialA = useMemo(() => {
    if (dualImage) {
      return [dualImageTurn()];
    }
    if (compactTools) {
      return [compactToolsTurn()];
    }
    if (detailPaging) {
      return [
        detailPagingTurn("deferred", false, detailRetainedPreview),
        ...(detailScrollCancel
          ? Array.from({ length: 6 }, (_, index) =>
            finalTurn(`detail-after-${index + 1}`, 3))
          : []),
      ];
    }
    if (mermaid || invalidMermaid || actualMermaid) {
      return [mermaidTurn(
        invalidMermaid,
        actualMermaid ? ROBOT_CORE_MERMAID_SOURCE : undefined,
      )];
    }
    if (math) return [mathTurn()];
    if (mermaidHistory) {
      return [
        mermaidTurn(),
        ...Array.from({ length: 40 }, (_, index) =>
          finalTurn(`after-mermaid-${index + 1}`, 3)),
      ];
    }
    if (large) {
      return Array.from({ length: largeCount }, (_, index) =>
        finalTurn(`m${index + 1}`, 2));
    }
    if (deepBrowse) {
      return Array.from({ length: 20 }, (_, index) =>
        finalTurn(`m${index + 1}`, 3));
    }
    if (timeline) {
      return [
        timelineTurn("timeline"),
        ...Array.from({ length: 80 }, (_, index) =>
          finalTurn(`f${index + 1}`, 4)),
      ];
    }
    if (interactiveTimeline) {
      return [
        timelineTurn("timeline"),
        streamingTurn("streaming"),
      ];
    }
    return INITIAL;
  }, [
    actualMermaid, compactTools, detailPaging, detailRetainedPreview,
    detailScrollCancel, dualImage,
    interactiveTimeline, math,
    deepBrowse, invalidMermaid, large, largeCount, mermaid, mermaidHistory,
    timeline,
  ]);
  const [sid, setSid] = useState("history-browser-session-a");
  const [sessions, setSessions] = useState<Record<string, FixtureSession>>({
    "history-browser-session-a": {
      turns: initialA,
      cursor: initialA[0]?.id ?? "",
      hasMore: !compactTools && !detailPaging && !invalidMermaid && !large && !mermaid
        && !mermaidHistory && !math && !timeline && !deepBrowse,
      pagesLoaded: 0,
      hasNewer: deepBrowse,
      newerPagesLoaded: 0,
      windowEpoch: 0,
    },
    "history-browser-session-b": {
      turns: SESSION_B,
      cursor: "b1",
      hasMore: false,
      pagesLoaded: 0,
      hasNewer: false,
      newerPagesLoaded: 0,
      windowEpoch: 0,
    },
  });
  const [loads, setLoads] = useState(0);
  const [historyRevision, setHistoryRevision] = useState("revision-1");
  const [historyViewRevision, setHistoryViewRevision] = useState("revision-1");
  const [historyViewId, setHistoryViewId] = useState(
    deepBrowse ? "browse-1" : "runtime",
  );
  const [browseMode, setBrowseMode] = useState(deepBrowse);
  const [newerLoads, setNewerLoads] = useState(0);
  const [latestTurns, setLatestTurns] = useState<Turn[]>(() =>
    deepBrowse
      ? Array.from({ length: 20 }, (_, index) =>
        finalTurn(`m${index + 21}`, 3))
      : []);
  const nextLiveTurnRef = useRef(41);
  const textSelectionGuardRef = useRef<TextSelectionGuard | null>(null);
  const updateTextSelectionGuard = useCallback(
    (guard: TextSelectionGuard | null) => {
      textSelectionGuardRef.current = guard;
    },
    [],
  );
  const [composerExpanded, setComposerExpanded] = useState(false);
  const [newChatSurface, setNewChatSurface] = useState<{
    machine: string;
    engine: "claude" | "codex";
    space: Space;
  }>({
    machine: "machine-a",
    engine: "codex",
    space: "code",
  });
  const [newChatSubmission, setNewChatSubmission] =
    useState<Record<string, unknown> | null>(null);
  const [queuedPrompt, setQueuedPrompt] = useState(QUEUED_FULL_PROMPT);
  const [queuedEditor, setQueuedEditor] =
    useState<QueuedQueryEditor | null>(null);
  const newChatProfiles = useMemo<PermissionProfileInfo[]>(() => [
    { id: ":read-only", allowed: true },
    { id: ":workspace", allowed: true },
    { id: ":danger-full-access", allowed: true },
    ...(longProfile
      ? [{
          id: LONG_PERMISSION_PROFILE_ID,
          description: "A deliberately long custom execution profile",
          allowed: true,
        }]
      : []),
  ], [longProfile]);
  const [pendingImages, setPendingImages] = useState<QueryImg[]>(() =>
    composerAttachment ? [{
      media_type: "image/png",
      data: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9WlK4h8AAAAASUVORK5CYII=",
    }] : []);
  const [migrationPickerPath, setMigrationPickerPath] =
    useState("/repo/stale");
  const [migrationPickerInitialPath, setMigrationPickerInitialPath] =
    useState<string | null>(
      migrationPickerNullInitial ? null : "/repo/current",
    );
  const [migrationPickerOpen, setMigrationPickerOpen] = useState(false);
  const [migrationPickerRequest, setMigrationPickerRequest] =
    useState<string | null>(null);
  const [migrationPickerRequestId, setMigrationPickerRequestId] =
    useState<string | null>(null);
  const [migrationPickerResponseId, setMigrationPickerResponseId] =
    useState<string | null>("migration-picker-stale");
  const migrationPickerRequestSeq = useRef(0);
  const [migrationPickerConfirmed, setMigrationPickerConfirmed] =
    useState<string | null>(null);
  const active = sessions[sid];
  const growOlderRow = useCallback((targetSid: string) => {
    setSessions((current) => ({
      ...current,
      [targetSid]: {
        ...current[targetSid],
        turns: current[targetSid].turns.map((turn) => turn.id === "n8"
          ? finalTurn("n8", 28)
          : turn),
      },
    }));
  }, []);
  const growBrowseRow = useCallback((targetSid: string) => {
    setSessions((current) => ({
      ...current,
      [targetSid]: {
        ...current[targetSid],
        turns: current[targetSid].turns.map((turn) => turn.id === "m15"
          ? finalTurn("m15", 28)
          : turn),
      },
    }));
  }, []);

  const loadMore = useCallback((): boolean | {
    accepted: true;
    viewId: string;
  } => {
    const requestSid = sid;
    if (!sessions[requestSid]?.hasMore) return false;
    const enteringViewId = runtimeBrowse && !browseMode ? "browse-1" : null;
    if (enteringViewId) {
      setBrowseMode(true);
      setHistoryViewId(enteringViewId);
    }
    setLoads((value) => value + 1);
    window.setTimeout(() => {
      if (emptyFinalPage) {
        setSessions((current) => ({
          ...current,
          [requestSid]: {
            ...current[requestSid],
            cursor: "history-start",
            hasMore: false,
          },
        }));
        return;
      }
      setSessions((current) => {
        const session = current[requestSid];
        const nextPage = session.pagesLoaded + 1;
        const page = olderPage(nextPage);
        return {
          ...current,
          [requestSid]: {
            ...session,
            turns: [...page, ...session.turns],
            cursor: page[0].id,
            hasMore: nextPage < pageCount,
            pagesLoaded: nextPage,
          },
        };
      });
      // Reproduce an image/Markdown/process row settling after the old 250 ms
      // anchor window has already expired.
      if (!manualGrowth) {
        window.setTimeout(() => growOlderRow(requestSid), growthDelayMs);
      }
    }, delayMs);
    return enteringViewId
      ? { accepted: true, viewId: enteringViewId }
      : true;
  }, [
    browseMode, delayMs, emptyFinalPage, growOlderRow, growthDelayMs,
    manualGrowth, pageCount, runtimeBrowse, sessions, sid,
  ]);

  const loadNewer = useCallback((anchorTurnId?: string) => {
    const requestSid = sid;
    const session = sessions[requestSid];
    if (!browseMode || !session?.hasNewer) return false;
    setNewerLoads((value) => value + 1);
    window.setTimeout(() => {
      setSessions((current) => {
        const target = current[requestSid];
        if (!target?.hasNewer) return current;
        const nextPage = (target.newerPagesLoaded ?? 0) + 1;
        const first = 21 + (nextPage - 1) * 8;
        const last = Math.min(40, first + 7);
        const page = Array.from(
          { length: Math.max(0, last - first + 1) },
          (_, index) => finalTurn(`m${first + index}`, 3),
        );
        const ids = new Set(target.turns.map((turn) => turn.id));
        const merged = [
          ...target.turns,
          ...page.filter((turn) => !ids.has(turn.id)),
        ];
        const bounded = [...merged];
        const guard = textSelectionGuardRef.current;
        const protectedIds = new Set([
          ...(anchorTurnId ? [anchorTurnId] : []),
          ...(guard
              && guard.sid === requestSid
              && guard.revision === historyRevision
              && guard.viewId === historyViewId
              && guard.scopeKey === "fixture-history-scope"
            ? guard.turnIds : []),
        ]);
        while (bounded.length > 20
            && !protectedIds.has(bounded[0]?.id ?? "")
            && !protectedIds.has(bounded[0]?.historyTurnId ?? "")) {
          bounded.shift();
        }
        return {
          ...current,
          [requestSid]: {
            ...target,
            turns: bounded,
            hasNewer: last < 40,
            newerPagesLoaded: nextPage,
            windowEpoch: (target.windowEpoch ?? 0) + 1,
          },
        };
      });
    }, delayMs);
    return true;
  }, [
    browseMode, delayMs, historyRevision, historyViewId, sessions, sid,
  ]);

  const loadDetail = useCallback((
    turnId: string,
    before?: string | null,
  ): boolean => {
    if (!detailPaging || turnId !== "detail-page") return false;
    const requestSid = sid;
    const page: DetailFixturePage = before === "detail-older"
      ? "older" : "latest";
    setSessions((current) => ({
      ...current,
      [requestSid]: {
        ...current[requestSid],
        turns: current[requestSid].turns.map((turn) =>
          turn.id === turnId ? { ...turn, detailLoading: true } : turn),
      },
    }));
    window.setTimeout(() => {
      setSessions((current) => ({
        ...current,
        [requestSid]: {
          ...current[requestSid],
          turns: current[requestSid].turns.map((turn) =>
            turn.id === turnId ? detailPagingTurn(page) : turn),
        },
      }));
      window.setTimeout(() => {
        setSessions((current) => ({
          ...current,
          [requestSid]: {
            ...current[requestSid],
            turns: current[requestSid].turns.map((turn) =>
              turn.id === turnId
                  && (page !== "latest"
                    || turn.detailOldestCursor === "detail-older")
                ? detailPagingTurn(page, true) : turn),
          },
        }));
      }, growthDelayMs);
    }, delayMs);
    return true;
  }, [delayMs, detailPaging, growthDelayMs, sid]);

  useEffect(() => {
    if (!detailPaging) return;
    const turn = sessions[sid]?.turns.find((candidate) =>
      candidate.detailAutoLoad === true
      && candidate.detailLoading !== true
      && candidate.detailHasMore === true
      && !!candidate.detailOldestCursor);
    if (!turn?.detailOldestCursor) return;
    loadDetail(turn.id, turn.detailOldestCursor);
  }, [detailPaging, loadDetail, sessions, sid]);

  const appendTurn = () => {
    if (deepBrowse) {
      const next = finalTurn(`live-${nextLiveTurnRef.current++}`, 4);
      setLatestTurns((current) => [...current, next].slice(-20));
      if (!browseMode) {
        setSessions((current) => {
          const session = current[sid];
          return {
            ...current,
            [sid]: {
              ...session,
              turns: [...session.turns, next].slice(-20),
            },
          };
        });
      }
      return;
    }
    setSessions((current) => {
      const session = current[sid];
      const next = finalTurn(`live-${session.turns.length + 1}`, 4);
      return {
        ...current,
        [sid]: { ...session, turns: [...session.turns, next] },
      };
    });
  };

  const returnLatest = () => {
    setSessions((current) => ({
      ...current,
      [sid]: {
        ...current[sid],
        turns: latestTurns,
        hasNewer: false,
        windowEpoch: (current[sid].windowEpoch ?? 0) + 1,
      },
    }));
    setBrowseMode(false);
    setHistoryViewId("runtime");
  };

  const growStreamingTurn = () => {
    setSessions((current) => {
      const session = current[sid];
      return {
        ...current,
        [sid]: {
          ...session,
          turns: session.turns.map((turn) => turn.id === "streaming"
            ? streamingTurn(
              "streaming",
              Math.max(1, turn.blocks[0]?.kind === "text"
                ? turn.blocks[0].text.split("\n\n").length + 3
                : 4),
            )
            : turn),
        },
      };
    });
  };

  const replaceHistoryRevision = () => {
    if (recoveryReplacement) {
      setSessions((current) => {
        const session = current[sid];
        return {
          ...current,
          [sid]: {
            ...session,
            turns: session.turns.map((turn, index) =>
              finalTurn(turn.id, index % 3 === 0 ? 4 : 3)),
          },
        };
      });
      // Recovery commits a new authoritative revision while deliberately
      // retaining the old view key. A later unrelated replacement is what
      // should reset that scope.
      setHistoryRevision((current) =>
        current === "revision-1" ? "revision-2" : "revision-3");
      return;
    }
    const replacement = Array.from(
      { length: 24 },
      (_, index) => finalTurn(`r${index + 1}`, 3),
    );
    setSessions((current) => ({
      ...current,
      [sid]: {
        turns: [],
        cursor: "",
        hasMore: false,
        pagesLoaded: 0,
      },
    }));
    const nextRevision = historyRevision === "revision-1"
      ? "revision-2" : "revision-3";
    setHistoryRevision(nextRevision);
    setHistoryViewRevision(nextRevision);
    window.setTimeout(() => {
      setSessions((current) => ({
        ...current,
        [sid]: {
          turns: replacement,
          cursor: replacement[0].id,
          hasMore: false,
          pagesLoaded: 0,
        },
      }));
    }, 0);
  };

  return (
    <main style={{ height: "100dvh", display: "flex", flexDirection: "column" }}>
      <div style={{ flex: "none", minHeight: 24 }}>
        <output data-testid="load-count">{loads}</output>
        <output data-testid="newer-load-count">{newerLoads}</output>
        <output data-testid="newest-turn-id">{
          active.turns[active.turns.length - 1]?.id ?? ""
        }</output>
        <button data-testid="switch-session" type="button"
          onClick={() => setSid((current) => current.endsWith("-a")
            ? "history-browser-session-b" : "history-browser-session-a")}>
          switch
        </button>
        <button data-testid="append-turn" type="button" onClick={appendTurn}>
          append
        </button>
        <button data-testid="replace-revision" type="button"
          onClick={replaceHistoryRevision}>
          replace revision
        </button>
        {migrationPickerFixture && (
          <>
            <button data-testid="open-migration-picker" type="button"
              onClick={() => setMigrationPickerOpen(true)}>
              open migration picker
            </button>
            <button data-testid="resolve-migration-picker" type="button"
              onClick={() => {
                if (!migrationPickerRequestId) return;
                setMigrationPickerPath(
                  migrationPickerRequest ?? "/home/fixture");
                setMigrationPickerResponseId(migrationPickerRequestId);
              }}>
              resolve migration picker
            </button>
            <button data-testid="externally-migrate-picker" type="button"
              onClick={() => setMigrationPickerInitialPath("/repo/external")}>
              externally migrate picker
            </button>
            <output data-testid="migration-picker-request">
              {migrationPickerRequestId
                ? migrationPickerRequest ?? "<home>"
                : ""}
            </output>
            <output data-testid="migration-picker-confirmed">
              {migrationPickerConfirmed}
            </output>
          </>
        )}
        {newChatControls && (
          <>
            <button data-testid="switch-newchat-device" type="button"
              onClick={() => setNewChatSurface((current) => ({
                ...current,
                machine: current.machine === "machine-a"
                  ? "machine-b" : "machine-a",
              }))}>
              switch new-chat device
            </button>
            <button data-testid="switch-newchat-engine" type="button"
              onClick={() => setNewChatSurface((current) => ({
                ...current,
                engine: current.engine === "codex" ? "claude" : "codex",
              }))}>
              switch new-chat engine
            </button>
            <button data-testid="switch-newchat-space" type="button"
              onClick={() => setNewChatSurface((current) => ({
                ...current,
                space: current.space === "code" ? "work" : "code",
              }))}>
              switch new-chat space
            </button>
            <output data-testid="newchat-scope">{
              `${newChatSurface.machine}:${newChatSurface.space}:`
              + newChatSurface.engine
            }</output>
            <output data-testid="newchat-submission">{
              JSON.stringify(newChatSubmission)
            }</output>
          </>
        )}
        {queuedQueryFixture && (
          <div className="queued show" data-testid="queued-query-fixture">
            <QueuedQueryChip query={{
              msg_id: "queued-fixture-message",
              prompt: queuedPrompt.slice(0, 512),
              imageCount: 1,
              fileCount: 0,
            }}
            onOpen={() => setQueuedEditor({
              sid: "queued-fixture-session",
              msgId: "queued-fixture-message",
              preview: queuedPrompt.slice(0, 512),
              prompt: queuedPrompt,
              kind: "queue",
              state: "queued",
              imageCount: 1,
              fileCount: 0,
              loading: false,
              saving: false,
              error: null,
            })}
            onRemove={() => {}} />
          </div>
        )}
        {composerAttachment && (
          <div className="attach show" data-testid="fixture-attachments">
            <PendingImageAttachments images={pendingImages}
              onRemove={(index) => setPendingImages((current) =>
                current.filter((_, candidate) => candidate !== index))} />
          </div>
        )}
        {interactiveTimeline && (
          <button data-testid="grow-stream" type="button"
            onClick={growStreamingTurn}>
            grow stream
          </button>
        )}
        {manualGrowth && (
          <button data-testid="grow-row" type="button"
            onClick={() => deepBrowse
              ? growBrowseRow("history-browser-session-a")
              : growOlderRow("history-browser-session-a")}>
            grow
          </button>
        )}
      </div>
      {newChatControls ? (
        <div data-testid="newchat-controls-fixture"
          style={{ flex: 1, minHeight: 0, display: "flex" }}>
          <NewChatView
            cwd="/tmp/project"
            controlScopeKey={
              `${newChatSurface.machine}:${newChatSurface.space}:`
              + newChatSurface.engine
            }
            engine={newChatSurface.engine}
            space={newChatSurface.space}
            autoFocus={false}
            permissionProfiles={newChatProfiles}
            onPickCwd={() => {}}
            onSend={(
              prompt: string,
              _images?: QueryImg[],
              _files?: QueryFile[],
              collaborationMode?: string,
              permissionMode?: string,
              permissionProfile?: string,
              webSearch?: string,
              serviceTier?: string,
            ) => {
              setNewChatSubmission({
                prompt,
                collaborationMode,
                permissionMode,
                permissionProfile,
                webSearch,
                serviceTier,
              });
              return false;
            }}
          />
        </div>
      ) : (
        <ChatView
          sid={sid}
          turns={active.turns}
          engine={timelineEngine}
          hasMore={active.hasMore}
          historyRevision={historyRevision}
          historyViewRevision={historyViewRevision}
          historyViewId={deepBrowse || browseMode ? historyViewId : undefined}
          historyScopeKey="fixture-history-scope"
          historyWindowEpoch={active.windowEpoch ?? 0}
          historyCursor={active.cursor}
          browseMode={browseMode && sid.endsWith("-a")}
          hasNewer={!!active.hasNewer}
          onLoadMore={loadMore}
          onLoadNewer={loadNewer}
          onReturnLatest={returnLatest}
          onLoadDetail={detailPaging ? loadDetail : undefined}
          onTextSelectionGuardChange={updateTextSelectionGuard}
          onEdit={() => {}}
          onGetDiff={() => {}}
        />
      )}
      {composerResize && (
        <div data-testid="fixture-composer" style={{
          flex: "none",
          height: composerExpanded ? 132 : 48,
          borderTop: "1px solid #ddd",
        }}>
          <button data-testid="toggle-composer" type="button"
            onClick={() => setComposerExpanded((current) => !current)}>
            toggle composer actions
          </button>
        </div>
      )}
      {quotaComposer && (
        <div className="composer" data-testid="quota-composer">
          <div className="composer-in">
            <div className="inrow">
              <button className="cmdbtn" type="button" aria-label="add">+</button>
              <textarea rows={1} aria-label="message"
                placeholder="输入 / 命令，$ Skill" />
              <button className="sendbtn" type="button" aria-label="send">↑</button>
            </div>
            <div className="hint">
              <button className="hint-mode" type="button">
                Full Access <span className="hint-mode-ch">▾</span>
              </button>
              <span className="hint-kbds">keyboard shortcuts</span>
              <div className="hint-right">
                <button className="hint-ctl" type="button">
                  GPT-5.6 Sol
                </button>
                <button className="hint-ctl" type="button">xhigh</button>
                <button className="hint-ctl fast-chip on" type="button">
                  快速
                </button>
                <UsageMeter
                  open={false}
                  report={null}
                  onToggle={() => {}}
                  onRefresh={() => {}}
                />
                <button className="hint-ring" type="button"
                  aria-label="context usage">
                  <svg viewBox="0 0 36 36" width="20" height="20"
                    aria-hidden="true">
                    <circle className="hr-track" cx="18" cy="18" r="15" />
                    <circle className="hr-fill" cx="18" cy="18" r="15" />
                  </svg>
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
      <QueuedQueryDialog editor={queuedEditor}
        onClose={() => setQueuedEditor(null)}
        onSave={(prompt) => {
          setQueuedEditor((current) => current
            ? { ...current, saving: true, error: null }
            : current);
          window.setTimeout(() => {
            setQueuedPrompt(prompt);
            setQueuedEditor((current) => current
              ? {
                  ...current,
                  preview: prompt.slice(0, 512),
                  prompt,
                  saving: false,
                  error: null,
                }
              : current);
          }, 10);
          return true;
        }}
        onRetry={() => true} />
      {migrationPickerFixture && (
        <DirPicker
          key={`migration-picker-${migrationPickerOpen ? "thread" : "closed"}-${migrationPickerInitialPath ?? "home"}`}
          open={migrationPickerOpen}
          path={migrationPickerPath}
          parent="/repo"
          dirs={[{ name: "stale-child", path: "/repo/stale-child" }]}
          responseRequestId={migrationPickerResponseId}
          initialPath={migrationPickerInitialPath}
          title="迁移 Codex 会话"
          confirmLabel="迁移到此目录"
          waitForInitialBrowse
          onBrowse={(path) => {
            const requestId =
              `migration-picker-${++migrationPickerRequestSeq.current}`;
            setMigrationPickerRequest(path);
            setMigrationPickerRequestId(requestId);
            return requestId;
          }}
          onConfirm={(path) => setMigrationPickerConfirmed(path)}
          onClose={() => {}}
        />
      )}
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<HistoryBrowserFixture />);
