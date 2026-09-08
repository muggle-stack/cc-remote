import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";
import { createRoot } from "react-dom/client";

import "../src/index.css";
import "../src/App.css";
import {
  createRuntime,
  initialState,
  OMITTED_PROCESS_ITEM_ID,
  reduce,
  type AppState,
  type Block,
  type Turn,
} from "../src/reducer";
import type {
  CodexProfileInfo,
  PermissionProfileInfo,
  QueryFile,
  QueryImg,
  ServerEvent,
  SessionInfo,
  Space,
  StatusReport,
  ThreadGoal,
} from "../src/protocol";
import { PROTOCOL_VERSION } from "../src/protocol";
import {
  ChatView,
} from "../src/components/ChatView";
import { MessageBlock } from "../src/components/MessageBlock";
import {
  historyImageAssetKey,
  type HistoryImageAsset,
  type HistoryImageVariant,
} from "../src/history-image-assets";
import {
  InlineImageAssetCache,
  INLINE_IMAGE_REQUEST_TIMEOUT_MS,
  type InlineImageAsset,
} from "../src/inline-image-assets";
import type { TextSelectionGuard } from "../src/history-selection-guard";
import { PendingImageAttachments } from "../src/components/PendingImageAttachments";
import { UsageMeter } from "../src/components/UsageMeter";
import { NewChatView } from "../src/components/NewChatView";
import { ArtifactPanel } from "../src/components/ArtifactPanel";
import {
  QueuedQueryChip,
  QueuedQueryDialog,
  type QueuedQueryEditor,
} from "../src/components/QueuedQueryDialog";
import { DirPicker } from "../src/components/DirPicker";
import { HeaderMenu } from "../src/components/HeaderMenu";
import { UsageActivitySheet } from "../src/components/UsageActivitySheet";
import { displayHistoryProjection } from "../src/history-recovery";
import { summaryHistoryTurns } from "../src/history-summary";
import { Composer } from "../src/components/Composer";
import { ComposerDraftStore } from "../src/composer-drafts";
import { GoalPanel } from "../src/components/GoalPanel";
import { ProcessTimeline } from "../src/components/ProcessTimeline";
import { MARKDOWN_HTML_GITHUB_README, MARKDOWN_HTML_HEADER_SVG, MARKDOWN_HTML_LOCAL_README } from "./fixtures/markdown-html";
import { SessionsSidebar } from "../src/components/SessionsSidebar";
import { useMobileViewport } from "../src/use-mobile-viewport";
import {
  completedGoalHasNewerUserTurn,
  latestPlanProgress,
  type TurnPlanProgress,
} from "../src/plan-progress";

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
        status: "running",
        title: "计划",
        explanation: "保持历史窗口稳定，并验证交互状态。",
        plan: [
          { step: "检查历史锚点", status: "completed" },
          { step: "验证计划弹层", status: "inProgress" },
          { step: "运行浏览器回归", status: "pending" },
        ],
        done: true,
      },
      {
        kind: "process",
        item_id: `${id}-command`,
        processKind: "command",
        phase: "end",
        status: "succeeded",
        title: "检查结果",
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

function persistentPlanTurn(): Turn {
  const base = timelineTurn("persistent-plan");
  const plan = base.blocks[0];
  return {
    ...base,
    done: false,
    doneTs: undefined,
    blocks: [
      plan.kind === "process" ? { ...plan, done: false } : plan,
      ...base.blocks.slice(1, -1),
      streamingTurn("persistent-plan-output", 180).blocks[0],
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

function streamingMathTurn(complete: boolean): Turn {
  return {
    id: "streaming-math",
    prompt: "流式公式",
    blocks: [{
      kind: "text",
      message_id: "streaming-math-message",
      channel: "final",
      text: String.raw`推导：\(x^2 + y^2 = r^2${complete ? String.raw`\)` : ""}`,
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
  autoLoad = false,
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
      processDetailState: "present",
      detailReasons: ["process"],
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
    processDetailState: "present",
    detailReasons: ["process"],
    detailEventCount: 24,
    detailLoaded: true,
    detailHasMore: page === "latest",
    detailOldestCursor: page === "latest" ? "detail-older" : undefined,
    detailHasNewer: false,
    detailNewerCursor: undefined,
    detailAutoLoad: page === "latest" && autoLoad,
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

function fixtureUsageReport(): StatusReport {
  const localNow = new Date();
  const today = Date.UTC(
    localNow.getFullYear(),
    localNow.getMonth(),
    localNow.getDate(),
  );
  const daily_usage_buckets = Array.from({ length: 210 }, (_, index) => ({
    start_date: new Date(today - index * 86_400_000)
      .toISOString().slice(0, 10),
    tokens: index % 5 === 0 ? 0 : (210 - index) * 18_731,
  }));
  return {
    v: PROTOCOL_VERSION,
    type: "status_report",
    ts: Date.now() / 1_000,
    thread: {
      thread_id: "usage-activity-fixture",
      status: "idle",
      active_flags: [],
    },
    runtime: {},
    context: {},
    account: {
      auth_type: "chatgpt",
      plan_type: "plus",
      requires_openai_auth: false,
    },
    rate_limits: [],
    usage: {
      lifetime_tokens: 987_654_321,
      peak_daily_tokens: 3_933_510,
      longest_running_turn_sec: 10_920,
      current_streak_days: 17,
      longest_streak_days: 84,
      daily_usage_buckets,
    },
    component_errors: [],
  };
}

function UsageActivityBrowserFixture({
  engine,
}: {
  engine: "claude" | "codex";
}) {
  const [activityOpen, setActivityOpen] = useState(false);
  const report = useMemo(fixtureUsageReport, []);
  useEffect(() => {
    document.documentElement.dataset.engine = engine;
    document.documentElement.dataset.theme = "dark";
  }, [engine]);
  return <main style={{ minHeight: "100dvh", background: "var(--bg)" }}>
    <header className="c-head" style={{ justifyContent: "flex-end" }}>
      <HeaderMenu
        engine={engine}
        theme="dark"
        notificationMode="off"
        notificationBinding="off"
        notificationAvailable
        onNotificationMode={async () => true}
        onOpenUsageActivity={() => setActivityOpen(true)}
        onToggleTheme={() => {}}
        onLogout={() => {}}
      />
    </header>
    <UsageActivitySheet
      open={activityOpen}
      report={report}
      hasSession
      onClose={() => setActivityOpen(false)}
      onRefresh={() => {}}
    />
  </main>;
}

const REDUCER_SESSION_A = "reducer-history-session-a";
const REDUCER_SESSION_B = "reducer-history-session-b";
const REDUCER_HISTORY_SCOPE = "fixture-reducer-history-scope";
const REDUCER_HISTORY_REVISION = "reducer-revision-1";
const REDUCER_HISTORY_GENERATION = "reducer-generation-1";

function reducerHistoryEvent(
  sid: string,
  turns: Turn[],
  options: {
    buildSeq: number;
    hasMore: boolean;
    oldestId?: string | null;
    newestId?: string | null;
    liveSeq?: number;
  },
): ServerEvent {
  return {
    v: 26,
    ts: Date.now() / 1000,
    type: "history",
    session_id: sid,
    revision: REDUCER_HISTORY_REVISION,
    generation: REDUCER_HISTORY_GENERATION,
    build_seq: options.buildSeq,
    live_seq: options.liveSeq ?? 0,
    authoritative: true,
    detail: "summary",
    events: [],
    turns,
    has_more: options.hasMore,
    oldest_id: options.oldestId ?? turns[0]?.id ?? null,
    newest_id: options.newestId ?? turns.at(-1)?.id ?? null,
    in_progress: false,
  };
}

function reducerHistoryInitialState(cachedPagingRace = false): AppState {
  let state: AppState = {
    ...initialState,
    focusedSid: REDUCER_SESSION_A,
    runtimes: {
      [REDUCER_SESSION_A]: createRuntime(),
      [REDUCER_SESSION_B]: createRuntime(),
    },
  };
  if (cachedPagingRace) {
    return reduce(state, {
      type: "hydrate_cache",
      sid: REDUCER_SESSION_A,
      revision: REDUCER_HISTORY_REVISION,
      generation: REDUCER_HISTORY_GENERATION,
      turns: [finalTurn("reducer-cached-current", 3)],
    });
  }
  state = reduce(state, {
    type: "event",
    event: reducerHistoryEvent(
      REDUCER_SESSION_A,
      Array.from({ length: 20 }, (_, index) =>
        finalTurn(`reducer-m${index + 21}`, 3)),
      {
        buildSeq: 1,
        hasMore: true,
        oldestId: "reducer-m21",
        newestId: "reducer-m40",
      },
    ),
  });
  state = reduce(state, {
    type: "event",
    event: reducerHistoryEvent(
      REDUCER_SESSION_B,
      Array.from({ length: 8 }, (_, index) =>
        finalTurn(`reducer-b${index + 1}`, 3)),
      {
        buildSeq: 1,
        hasMore: false,
        oldestId: "reducer-b1",
        newestId: "reducer-b8",
      },
    ),
  });
  return state;
}

function ReducerHistoryBrowserFixture() {
  const cachedPagingRace = useMemo(
    () => new URLSearchParams(window.location.search).has("cached-paging"),
    [],
  );
  const [state, dispatch] = useReducer(
    reduce,
    cachedPagingRace,
    reducerHistoryInitialState,
  );
  const stateRef = useRef(state);
  stateRef.current = state;
  const [loads, setLoads] = useState(0);
  const [refreshes, setRefreshes] = useState(0);
  const sid = state.focusedSid ?? REDUCER_SESSION_A;
  const runtime = state.runtimes[sid] ?? createRuntime();
  const historyView = displayHistoryProjection(
    state.historyRecovery,
    sid,
    runtime,
    state.historyBrowse,
    state.retainedHistoryBrowse,
  );

  useEffect(() => {
    if (!cachedPagingRace) return;
    const timer = window.setTimeout(() => {
      dispatch({
        type: "event",
        event: reducerHistoryEvent(
          REDUCER_SESSION_A,
          [finalTurn("reducer-cached-current", 3)],
          {
            buildSeq: 1,
            hasMore: true,
            oldestId: "reducer-server-older-cursor",
            newestId: "reducer-cached-current",
          },
        ),
      });
    }, 25);
    return () => window.clearTimeout(timer);
  }, [cachedPagingRace]);

  const loadOlder = useCallback((): boolean | {
    accepted: true;
    viewId: string;
  } => {
    const current = stateRef.current;
    const requestSid = current.focusedSid;
    const requestRuntime = requestSid
      ? current.runtimes[requestSid] : undefined;
    if (!requestSid || requestSid !== REDUCER_SESSION_A
        || !requestRuntime?.historyRevision
        || !requestRuntime.hasMore
        || !requestRuntime.oldestId
        || current.historyBrowse) return false;
    const viewId = "reducer-browse-1";
    dispatch({
      type: "begin_history_browse",
      sid: requestSid,
      scopeKey: REDUCER_HISTORY_SCOPE,
      revision: requestRuntime.historyRevision,
      generation: requestRuntime.historyGeneration,
      viewId,
      basePageKey: "reducer-latest-page",
    });
    setLoads((value) => value + 1);
    window.setTimeout(() => {
      const latest = stateRef.current;
      const browse = latest.historyBrowse;
      if (!browse || latest.focusedSid !== requestSid) return;
      dispatch({
        type: "install_history_browse_page",
        sid: requestSid,
        scopeKey: browse.scopeKey,
        revision: browse.revision,
        generation: browse.generation,
        viewId: browse.viewId,
        windowEpoch: browse.windowEpoch,
        before: browse.olderCursor!,
        page: {
          pageKey: "reducer-older-page",
          turns: Array.from({ length: 20 }, (_, index) =>
            finalTurn(`reducer-m${index + 1}`, index === 14 ? 4 : 3)),
          hasOlder: false,
          olderCursor: null,
          newerPageKey: browse.oldestPageKey,
        },
      });
    }, 25);
    return { accepted: true, viewId };
  }, []);

  const refreshLiveRuntime = useCallback(() => {
    const current = stateRef.current;
    const target = current.runtimes[REDUCER_SESSION_A];
    const nextBuildSeq = Math.max(2, (target?.historyBuildSeq ?? 0) + 1);
    dispatch({
      type: "event",
      event: reducerHistoryEvent(
        REDUCER_SESSION_A,
        Array.from({ length: 16 }, (_, index) =>
          finalTurn(`reducer-m${index + 25}`, index === 15 ? 5 : 3)),
        {
          buildSeq: nextBuildSeq,
          hasMore: true,
          oldestId: "reducer-m25",
          newestId: "reducer-m40",
          liveSeq: target?.lastLiveSeq ?? 0,
        },
      ),
    });
    setRefreshes((value) => value + 1);
  }, []);

  const switchSession = useCallback(() => {
    const target = stateRef.current.focusedSid === REDUCER_SESSION_A
      ? REDUCER_SESSION_B : REDUCER_SESSION_A;
    dispatch({ type: "focus_session", sid: target });
  }, []);

  return (
    <main style={{ height: "100dvh", display: "flex", flexDirection: "column" }}>
      <div style={{ flex: "none", minHeight: 24 }}>
        <output data-testid="load-count">{loads}</output>
        <output data-testid="reducer-refresh-count">{refreshes}</output>
        <output data-testid="reducer-focused-sid">{sid}</output>
        <output data-testid="reducer-turn-count">{historyView.turns.length}</output>
        <output data-testid="reducer-unique-turn-count">{
          new Set(historyView.turns.map(
            (turn) => turn.historyTurnId ?? turn.id,
          )).size
        }</output>
        <button data-testid="switch-session" type="button"
          onClick={switchSession}>
          switch
        </button>
        <button data-testid="reducer-live-refresh" type="button"
          onClick={refreshLiveRuntime}>
          refresh live runtime
        </button>
      </div>
      <ChatView
        sid={sid}
        turns={historyView.turns}
        engine="codex"
        hasMore={historyView.hasMore}
        historyRevision={runtime.historyRevision}
        historyViewRevision={historyView.viewRevision}
        historyViewId={historyView.viewId}
        historyScopeKey={historyView.browsing
          ? state.historyBrowse?.scopeKey : REDUCER_HISTORY_SCOPE}
        historyWindowEpoch={historyView.windowEpoch}
        historyCursor={historyView.oldestId}
        browseMode={historyView.browsing}
        hasNewer={historyView.hasNewer}
        onLoadMore={loadOlder}
        onEdit={() => {}}
        onGetDiff={() => {}}
      />
    </main>
  );
}

const CODEX_BURST_SID = "codex-live-burst-session";
const CODEX_BURST_TICKS = 96;

function codexBurstInitialState(): AppState {
  return {
    ...initialState,
    focusedSid: CODEX_BURST_SID,
    runtimes: {
      [CODEX_BURST_SID]: {
        ...createRuntime(),
        turns: Array.from(
          { length: 12 },
          (_, index) => finalTurn(`burst-history-${index + 1}`, 3),
        ),
      },
    },
  };
}

function CodexLiveBurstFixture() {
  const params = useMemo(() => new URLSearchParams(window.location.search), []);
  const composerLive = params.has("composer-live");
  const composerPaste = params.has("composer-paste");
  const [lastComposerPrompt, setLastComposerPrompt] = useState<string | null>(null);
  const [state, dispatch] = useReducer(
    reduce,
    undefined,
    codexBurstInitialState,
  );
  const runningRef = useRef(false);
  const timersRef = useRef<number[]>([]);
  const sequenceRef = useRef(0);
  const draftStoreRef = useRef(new ComposerDraftStore());
  const runtime = state.runtimes[CODEX_BURST_SID] ?? createRuntime();

  useEffect(() => () => {
    for (const timer of timersRef.current) window.clearTimeout(timer);
    timersRef.current = [];
  }, []);

  const emit = useCallback((payload: Record<string, unknown>) => {
    const event = {
      v: PROTOCOL_VERSION,
      ts: Date.now() / 1000,
      sid: CODEX_BURST_SID,
      seq: ++sequenceRef.current,
      ...payload,
    } as ServerEvent;
    dispatch({ type: "event", event });
  }, []);

  const startBurst = useCallback(() => {
    if (runningRef.current) return;
    runningRef.current = true;
    document.documentElement.dataset.codexBurst = "running";
    emit({
      type: "user_msg",
      msg_id: "burst-turn",
      prompt: "连续检查工具调用，并保持实时页面稳定。",
    });
    emit({
      type: "assistant_msg_start",
      message_id: "burst-commentary",
      channel: "commentary",
    });
    emit({
      type: "process",
      item_id: "burst-plan",
      kind: "plan",
      phase: "start",
      status: "running",
      turn_id: "burst-turn-native",
      title: "计划",
      summary: "逐项检查实时工具输出。",
    });

    let activeTool = 0;
    const finishTool = (toolIndex: number) => {
      emit({
        type: "tool_result",
        tool_use_id: `burst-tool-${toolIndex}`,
        content: `command ${toolIndex} completed`,
        is_error: false,
        status: "succeeded",
        exit_code: 0,
        duration_ms: 40,
      });
    };
    for (let tick = 1; tick <= CODEX_BURST_TICKS; tick += 1) {
      const timer = window.setTimeout(() => {
        if ((tick - 1) % 4 === 0) {
          activeTool += 1;
          emit({
            type: "tool_use",
            message_id: "burst-commentary",
            tool_use_id: `burst-tool-${activeTool}`,
            tool: activeTool % 3 === 0 ? "mcpToolCall" : "commandExecution",
            input: activeTool % 3 === 0
              ? { server: "fixture", tool: "lookup" }
              : { command: `printf tool-${activeTool}`, cwd: "/repo" },
            category: activeTool % 3 === 0 ? "mcp" : "command",
            title: activeTool % 3 === 0
              ? "查询测试数据"
              : `运行命令 ${activeTool}`,
          });
        }
        emit({
          type: "tool_delta",
          tool_use_id: `burst-tool-${activeTool}`,
          stream: "output",
          delta: `tool-${activeTool} output ${tick}\n`,
        });
        if (tick % 3 === 0) {
          emit({
            type: "delta",
            message_id: "burst-commentary",
            channel: "commentary",
            text: `已完成第 ${tick} 次检查，继续核对下一项工具状态和输出边界。\n\n`,
          });
        }
        if (tick % 4 === 0) finishTool(activeTool);
        if (tick !== CODEX_BURST_TICKS) return;
        emit({
          type: "process",
          item_id: "burst-plan",
          kind: "plan",
          phase: "end",
          status: "succeeded",
          turn_id: "burst-turn-native",
          title: "计划",
          summary: "实时工具检查完成。",
        });
        emit({
          type: "assistant_msg_end",
          message_id: "burst-commentary",
          channel: "commentary",
        });
        emit({
          type: "assistant_msg_start",
          message_id: "burst-final",
          channel: "final",
        });
        emit({
          type: "delta",
          message_id: "burst-final",
          channel: "final",
          text: "检查完成，所有工具均已返回。",
        });
        emit({
          type: "assistant_msg_end",
          message_id: "burst-final",
          channel: "final",
        });
        emit({
          type: "turn_end",
          turn_id: "burst-turn-native",
          result: {
            subtype: "success",
            duration_ms: 432,
            is_error: false,
          },
        });
        runningRef.current = false;
        document.documentElement.dataset.codexBurst = "done";
      }, tick * 12);
      timersRef.current.push(timer);
    }
  }, [emit]);

  return (
    <main style={{ height: "100dvh", display: "flex", flexDirection: "column" }}>
      <div style={{ flex: "none", minHeight: 28 }}>
        <button data-testid="start-codex-burst" type="button"
          onClick={startBurst}>
          start Codex burst
        </button>
      </div>
      <ChatView sid={CODEX_BURST_SID} turns={runtime.turns}
        engine="codex" historyRevision="codex-burst-r1"
        historyScopeKey="fixture-codex-burst"
        onEdit={() => {}} onGetDiff={() => {}} />
      {composerLive && (
        <div data-testid="live-composer-shell">
          <Composer
            draftKey="fixture-codex-live-composer"
            draftStore={draftStoreRef.current}
            state={runtime.state}
            connState="connected"
            wrapperOnline
            sendMode={runtime.sendMode}
            setSendMode={() => {}}
            queue={runtime.queue}
            pendingSend={runtime.pendingSend}
            failedDeferred={runtime.failedDeferred}
            unconfirmedQueued={[]}
            unconfirmedReplaceable={[]}
            queueCapacity={{}}
            replaceQueueCapacity={{}}
            model="gpt-5.6-sol"
            effort="xhigh"
            perm="danger-full-access"
            permissionProfile=":danger-full-access"
            permissionProfiles={null}
            webSearch="cached"
            collaborationMode="default"
            fast={false}
            engine="codex"
            editPrompt={null}
            onEditConsumed={() => {}}
            onSendQuery={(prompt) => {
              if (!composerPaste) return false;
              setLastComposerPrompt(prompt);
              return true;
            }}
            onSteerQuery={() => false}
            onInterrupt={() => {}}
            onEnqueue={() => false}
            onSetPending={() => false}
            onRemoveQueued={() => {}}
            onInspectQueued={() => {}}
            onSetModel={() => {}}
            onSetEffort={() => {}}
            onSetPerm={() => {}}
            onSetPermissionProfile={() => {}}
            onGetPermissionProfiles={() => {}}
            onSetWebSearch={() => {}}
            onSetCollaborationMode={() => {}}
            onClear={() => {}}
            onContext={() => {}}
            contextReport={null}
          />
          {composerPaste && <output data-testid="composer-paste-output">
            {lastComposerPrompt ?? ""}
          </output>}
        </div>
      )}
    </main>
  );
}

function HistoryConversationBrowserFixture() {
  const params = useMemo(() => new URLSearchParams(window.location.search), []);
  const delayMs = Number(params.get("delay") ?? "30");
  const growthDelayMs = Number(params.get("growth-delay") ?? "500");
  const manualGrowth = params.has("manual-growth");
  const largeCount = Number(params.get("large") ?? "0");
  const paragraphs = Number(params.get("paragraphs") ?? "2");
  const pageCount = Math.max(1, Number(params.get("pages") ?? "1"));
  const large = largeCount > 0;
  const timeline = params.has("timeline");
  const persistentPlan = params.has("persistent-plan");
  const historicalPlan = params.has("historical-plan");
  const interactiveTimeline = params.has("interactive-timeline");
  const dualImage = params.has("dual-image");
  const compactTools = params.has("compact-tools");
  const detailPaging = params.has("detail-paging");
  const detailErrorOnce = params.has("detail-error-once");
  const detailOlderErrorOnce = params.has("detail-older-error-once");
  const detailRetainedPreview = params.has("detail-retained-preview");
  const detailRestoredPage = params.get("detail-restored-page");
  const detailScrollCancel = params.has("detail-scroll-cancel");
  const mermaid = params.has("mermaid");
  const actualMermaid = params.has("actual-mermaid");
  const invalidMermaid = params.has("invalid-mermaid");
  const mermaidHistory = params.has("mermaid-history");
  const math = params.has("math");
  const streamingMath = params.has("streaming-math");
  const composerAttachment = params.has("composer-attachment");
  const composerResize = params.has("composer-resize");
  const quotaComposer = params.has("quota-composer");
  const queuedQueryFixture = params.has("queued-query-editor");
  const migrationPickerFixture = params.has("migration-picker")
    || params.has("migration-picker-null");
  const migrationPickerNullInitial = params.has("migration-picker-null");
  const newChatControls = params.has("newchat-controls");
  const longProfile = params.has("long-profile");
  const manyProfiles = params.has("many-profiles");
  const recoveryReplacement = params.has("recovery-replace");
  const pendingRevisionReplacement = params.has("pending-revision-replace");
  const deepBrowse = params.has("deep-browse");
  const dirtyLiveBrowse = params.has("dirty-live-browse");
  const runtimeBrowse = params.has("runtime-browse");
  const generationShift = params.has("generation-shift");
  const delayedHistoryAvailability = params.has("delayed-history-availability");
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
      let detailTurn = detailPagingTurn("deferred", false, detailRetainedPreview);
      if (detailRestoredPage === "process") {
        detailTurn = {
          ...detailPagingTurn("latest"),
          detailLoaded: false, detailRestoreIncomplete: true,
          processStartedTs: 10_000, processDoneTs: 10_000 + 129 * 60_000,
        };
      } else if (detailRestoredPage === "older" || detailRestoredPage === "newer") {
        detailTurn = {
          ...detailTurn,
          processDetailState: "unknown", detailReasons: [], detailEventCount: 0,
          detailHasMore: detailRestoredPage === "older",
          detailOldestCursor: detailRestoredPage === "older" ? "detail-older" : undefined,
          detailHasNewer: detailRestoredPage === "newer",
          detailNewerCursor: detailRestoredPage === "newer" ? "detail-newer" : undefined,
        };
      }
      return [
        detailTurn,
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
    if (streamingMath) {
      return [
        ...Array.from({ length: 8 }, (_, index) =>
          finalTurn(`math-before-${index + 1}`, 3)),
        streamingMathTurn(false),
      ];
    }
    if (mermaidHistory) {
      return [
        mermaidTurn(),
        ...Array.from({ length: 40 }, (_, index) =>
          finalTurn(`after-mermaid-${index + 1}`, 3)),
      ];
    }
    if (large) {
      return Array.from({ length: largeCount }, (_, index) =>
        finalTurn(`m${index + 1}`, paragraphs));
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
    if (persistentPlan) return [persistentPlanTurn()];
    if (historicalPlan) {
      return [
        timelineTurn("historical-plan"),
        ...Array.from({ length: 8 }, (_, index) =>
          finalTurn(`historical-followup-${index + 1}`, 3)),
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
    actualMermaid, compactTools, detailPaging, detailRetainedPreview, detailRestoredPage,
    detailScrollCancel, dualImage,
    interactiveTimeline, math, streamingMath,
    deepBrowse, invalidMermaid, large, largeCount, paragraphs, mermaid, mermaidHistory,
    historicalPlan, persistentPlan, timeline,
  ]);
  const [sid, setSid] = useState("history-browser-session-a");
  const [sessions, setSessions] = useState<Record<string, FixtureSession>>({
    "history-browser-session-a": {
      turns: initialA,
      cursor: initialA[0]?.id ?? "",
      hasMore: !compactTools && !detailPaging && !invalidMermaid && !large && !mermaid
        && !mermaidHistory && !math && !streamingMath && !timeline && !deepBrowse
        && !delayedHistoryAvailability && !historicalPlan,
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
  const [historyGeneration, setHistoryGeneration] =
    useState("fixture-generation-1");
  const [historyViewRevision, setHistoryViewRevision] = useState("revision-1");
  const [historyTransitionPending, setHistoryTransitionPending] =
    useState(false);
  const [sessionAuthorityScope, setSessionAuthorityScope] =
    useState("fixture-authority-a");
  const [historyViewId, setHistoryViewId] = useState(
    deepBrowse ? "browse-1" : "runtime",
  );
  const [browseMode, setBrowseMode] = useState(deepBrowse);
  const [newerLoads, setNewerLoads] = useState(0);
  const [latestTurns, setLatestTurns] = useState<Turn[]>(() =>
    deepBrowse
      ? Array.from({ length: 20 }, (_, index) =>
        finalTurn(`m${index + 21}`, 3))
      : runtimeBrowse ? initialA : []);
  const nextLiveTurnRef = useRef(41);
  const textSelectionGuardRef = useRef<TextSelectionGuard | null>(null);
  const detailRequestCountRef = useRef(0);
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
    ...(manyProfiles
      ? Array.from({ length: 12 }, (_, index) => ({
          id: `custom-profile-${index}`,
          description: `Custom execution profile ${index}`,
          allowed: true,
        }))
      : []),
  ], [longProfile, manyProfiles]);
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
  const fixedPlanProgress = persistentPlan || historicalPlan
    ? latestPlanProgress(active.turns) : null;
  const revealOlderHistory = useCallback(() => {
    setSessions((current) => ({
      ...current,
      [sid]: {
        ...current[sid],
        hasMore: true,
        cursor: current[sid].turns[0]?.id ?? "history-cursor",
      },
    }));
  }, [sid]);
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
    scopeKey: string;
    generation: string | null;
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
      ? {
          accepted: true,
          viewId: enteringViewId,
          scopeKey: "fixture-browse-scope",
          generation: null,
        }
      : true;
  }, [
    browseMode, delayMs, emptyFinalPage, growOlderRow, growthDelayMs,
    manualGrowth, pageCount, runtimeBrowse, sessions, sid,
  ]);

  const loadNewer = useCallback((anchorTurnId?: string) => {
    const requestSid = sid;
    const session = sessions[requestSid];
    if (!browseMode || !session?.hasNewer) return false;
    const requestedNextPage = (session.newerPagesLoaded ?? 0) + 1;
    const requestedLast = Math.min(
      40, 21 + (requestedNextPage - 1) * 8 + 7,
    );
    setNewerLoads((value) => value + 1);
    window.setTimeout(() => {
      if (dirtyLiveBrowse && requestedLast >= 40) {
        setBrowseMode(false);
        setHistoryViewId("runtime");
        setSessions((current) => ({
          ...current,
          [requestSid]: {
            ...current[requestSid],
            turns: latestTurns,
            hasNewer: false,
            windowEpoch: (current[requestSid].windowEpoch ?? 0) + 1,
          },
        }));
        return;
      }
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
    browseMode, delayMs, dirtyLiveBrowse, historyRevision, historyViewId,
    latestTurns, sessions, sid,
  ]);

  const loadDetail = useCallback((
    turnId: string,
    before?: string | null,
    autoLoad = false,
  ): boolean => {
    if (!detailPaging || turnId !== "detail-page") return false;
    detailRequestCountRef.current += 1;
    document.documentElement.dataset.detailRequests =
      String(detailRequestCountRef.current);
    // Either explicit cursor returns the terminal combined process window.
    const page: DetailFixturePage = before === "detail-older" || before === "detail-newer"
      ? "older" : "latest";
    document.documentElement.dataset.detailLastBefore = before ?? "initial";
    const failThisRequest = (
      detailErrorOnce && detailRequestCountRef.current === 1
    ) || (
      detailOlderErrorOnce && page === "older"
      && detailRequestCountRef.current === 2
    );
    const requestSid = sid;
    setSessions((current) => ({
      ...current,
      [requestSid]: {
        ...current[requestSid],
        turns: current[requestSid].turns.map((turn) =>
          turn.id === turnId ? {
            ...turn,
            detailLoading: true,
            detailError: undefined,
            detailRetryBefore: before ?? null,
            detailRetryDirection: before == null ? "initial"
              : before === "detail-newer" ? "newer" : "older",
          } : turn),
      },
    }));
    window.setTimeout(() => {
      if (failThisRequest) {
        setSessions((current) => ({
          ...current,
          [requestSid]: {
            ...current[requestSid],
            turns: current[requestSid].turns.map((turn) =>
              turn.id === turnId ? {
                ...turn,
                detailLoading: false,
                detailAutoLoad: false,
                detailError: "详细过程暂时不可用，请稍后重试",
              } : turn),
          },
        }));
        return;
      }
      setSessions((current) => ({
        ...current,
        [requestSid]: {
          ...current[requestSid],
          turns: current[requestSid].turns.map((turn) =>
            turn.id === turnId
              ? detailPagingTurn(page, false, false, autoLoad) : turn),
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
                ? detailPagingTurn(page, true, false, autoLoad) : turn),
          },
        }));
      }, growthDelayMs);
    }, delayMs);
    return true;
  }, [
    delayMs, detailErrorOnce, detailOlderErrorOnce,
    detailPaging, growthDelayMs, sid,
  ]);

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
      const next = dirtyLiveBrowse
        ? streamingTurn("live-streaming", 4)
        : finalTurn(`live-${nextLiveTurnRef.current++}`, 4);
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

  const closeStreamingFormula = () => {
    setSessions((current) => {
      const session = current[sid];
      return {
        ...current,
        [sid]: {
          ...session,
          turns: session.turns.map((turn) => turn.id === "streaming-math"
            ? streamingMathTurn(true) : turn),
        },
      };
    });
  };

  const growBackgroundStreamingTurn = () => {
    setSessions((current) => {
      const targetSid = "history-browser-session-a";
      const session = current[targetSid];
      return {
        ...current,
        [targetSid]: {
          ...session,
          turns: session.turns.map((turn) => turn.id === "streaming"
            ? streamingTurn("streaming", 48)
            : turn),
        },
      };
    });
  };

  const replaceHistoryRevision = (shiftAuthority = false) => {
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
    setHistoryTransitionPending(pendingRevisionReplacement);
    if (shiftAuthority) {
      setSessionAuthorityScope((current) => current === "fixture-authority-a"
        ? "fixture-authority-b" : "fixture-authority-a");
    }
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
      setHistoryTransitionPending(false);
    }, pendingRevisionReplacement ? 400 : 0);
  };

  return (
    <main style={{ height: "100dvh", display: "flex", flexDirection: "column" }}>
      <div style={{ flex: "none", minHeight: 24 }}>
        <output data-testid="load-count">{loads}</output>
        <output data-testid="newer-load-count">{newerLoads}</output>
        <output data-testid="newest-turn-id">{
          active.turns[active.turns.length - 1]?.id ?? ""
        }</output>
        <output data-testid="history-transition-state">{
          historyTransitionPending ? "pending" : "ready"
        }</output>
        <output data-testid="session-authority-scope">{
          sessionAuthorityScope
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
          onClick={() => replaceHistoryRevision()}>
          replace revision
        </button>
        {pendingRevisionReplacement && (
          <button data-testid="replace-authority" type="button"
            onClick={() => replaceHistoryRevision(true)}>
            replace authority
          </button>
        )}
        {generationShift && (
          <button data-testid="shift-generation" type="button"
            onClick={() => setHistoryGeneration((current) =>
              current === "fixture-generation-1"
                ? "fixture-generation-2" : "fixture-generation-3")}>
            shift generation
          </button>
        )}
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
        {delayedHistoryAvailability && (
          <button data-testid="reveal-older-history" type="button"
            onClick={revealOlderHistory}>
            reveal older history
          </button>
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
          <>
            <button data-testid="grow-stream" type="button"
              onClick={growStreamingTurn}>
              grow stream
            </button>
            <button data-testid="grow-background-stream" type="button"
              onClick={growBackgroundStreamingTurn}>
              grow background stream
            </button>
          </>
        )}
        {streamingMath && (
          <button data-testid="close-streaming-formula" type="button"
            onClick={closeStreamingFormula}>
            close formula
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
          key={sessionAuthorityScope}
          sid={sid}
          turns={active.turns}
          engine={timelineEngine}
          loading={historyTransitionPending}
          hasMore={active.hasMore}
          historyRevision={historyRevision}
          historyViewRevision={historyViewRevision}
          historyGeneration={generationShift ? historyGeneration : undefined}
          historyViewId={deepBrowse || browseMode ? historyViewId : undefined}
          historyScopeKey={runtimeBrowse && browseMode
            ? "fixture-browse-scope" : "fixture-history-scope"}
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
          activeTurnId={sid.endsWith("-a")
            ? interactiveTimeline
              ? "streaming"
              : dirtyLiveBrowse && !browseMode
                ? "live-streaming" : null
            : null}
          externalPlanProgress={fixedPlanProgress ? {
            turnId: fixedPlanProgress.turnId,
            itemId: fixedPlanProgress.block.item_id,
          } : null}
        />
      )}
      {fixedPlanProgress && <GoalPanel engine="codex" goal={null}
        revealed={false} open={false} plan={fixedPlanProgress}
        onOpen={() => {}} onClose={() => {}} onDismiss={() => {}}
        onSave={() => {}} onClear={() => {}} />}
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

export function HistoryBrowserFixture() {
  const params = new URLSearchParams(window.location.search);
  const planUi = params.get("plan-ui");
  if (params.has("profile-sidebar")) return <ProfileSidebarFixture />;
  if (planUi) return <PlanUiFixture mode={planUi} />;
  if (params.has("plan-lifecycle")) {
    return <PlanLifecycleFixture mode={params.get("plan-lifecycle") ?? ""} />;
  }
  if (params.has("goal-ui")) {
    return <GoalUiFixture status={params.get("goal-status")}
      withPlan={params.has("plan")} hidden={params.has("goal-hidden")}
      longGoal={params.has("goal-long")}
      newerTurn={params.has("goal-next-turn")}
      longPlan={params.has("plan-long")} />;
  }
  if (params.has("header-menu")) {
    return <UsageActivityBrowserFixture
      engine={params.get("engine") === "claude" ? "claude" : "codex"}
    />;
  }
  if (params.has("newchat-controls")) {
    return <MobileViewportHistoryConversationBrowserFixture />;
  }
  return <HistoryConversationBrowserFixture />;
}

function ProfileSidebarFixture() {
  const params = new URLSearchParams(window.location.search);
  const theme = params.get("theme") === "dark"
    ? "dark"
    : "light";
  const [space, setSpace] = useState<Space>(
    params.get("profile-sidebar") === "work" ? "work" : "code",
  );
  const [newProfileId, setNewProfileId] = useState("none");
  const [activeSessionId, setActiveSessionId] = useState("profile-sidebar-active");
  useEffect(() => {
    const root = document.documentElement;
    const previousEngine = root.dataset.engine;
    const previousTheme = root.dataset.theme;
    root.dataset.engine = "codex";
    root.dataset.theme = theme;
    return () => {
      if (previousEngine === undefined) delete root.dataset.engine;
      else root.dataset.engine = previousEngine;
      if (previousTheme === undefined) delete root.dataset.theme;
      else root.dataset.theme = previousTheme;
    };
  }, [theme]);
  const profiles: CodexProfileInfo[] = [
    { id: "primary", label: "Main" },
    { id: "stack", label: "Stack" },
  ];
  const sessions: SessionInfo[] = [{
    session_id: "profile-sidebar-active",
    first_prompt: "看看当前仓库。",
    cwd: "/repo/cc-remote",
    pinned: true,
    state: "idle",
    engine: "codex",
    space,
    codex_profile_id: "stack",
    codex_profile_label: "Stack",
  }, {
    session_id: "profile-sidebar-default",
    summary: "cc-remote 派生",
    cwd: "/repo/cc-remote",
    state: "idle",
    engine: "codex",
    space,
    codex_profile_id: "primary",
    codex_profile_label: "Main",
  }];
  const noop = () => {};
  return (
    <>
      <output data-testid="new-work-profile" hidden>{newProfileId}</output>
      <SessionsSidebar
        open
        engine="codex"
        space={space}
        profileScopeKey={`fixture:codex:${space}`}
        codexProfiles={profiles}
        defaultCodexProfileId="primary"
        sessions={sessions}
        machineId={params.get("machine") ?? "fixture-device"}
        activeSessionId={activeSessionId}
        onSpaceChange={setSpace}
        onSelect={setActiveSessionId}
        onNew={(profileId) => setNewProfileId(profileId ?? "none")}
        onNewInDir={noop}
        onClose={noop}
        onRename={noop}
        onArchive={noop}
        onPin={noop}
        onDelete={noop}
        onForkWorktree={noop}
        onMigrate={noop}
      />
    </>
  );
}

function PlanLifecycleFixture({ mode }: { mode: string }) {
  const interrupted = mode === "interrupted";
  const [turns, setTurns] = useState<Turn[]>(() => [{
    id: interrupted ? "interrupted-plan-turn" : "completed-plan-turn",
    prompt: interrupted ? "执行后终止" : "完成当前任务",
    done: true,
    interrupted: interrupted || undefined,
    blocks: [{
      kind: "process",
      item_id: interrupted ? "interrupted-plan-item" : "completed-plan-item",
      processKind: "plan",
      phase: "end",
      status: interrupted ? "interrupted" : "succeeded",
      title: "计划",
      plan: [
        { step: "实现功能", status: "completed" },
        { step: "完成验证", status: interrupted ? "inProgress" : "completed" },
      ],
      done: true,
    }],
  }]);
  const plan = latestPlanProgress(turns);
  return (
    <main style={{ height: "100dvh", display: "flex", flexDirection: "column" }}>
      <button type="button" data-testid="send-next-plan-message"
        onClick={() => setTurns((current) => [...current, {
          id: "next-user-turn",
          prompt: "开始下一个问题",
          done: false,
          blocks: [],
        }])}>
        send next message
      </button>
      <div style={{ flex: 1 }} />
      <GoalPanel engine="codex" goal={null} revealed={false} open={false}
        plan={plan} onOpen={() => {}} onClose={() => {}}
        onDismiss={() => {}} onSave={() => {}} onClear={() => {}} />
      <div className="composer"><div className="composer-in" /></div>
    </main>
  );
}

function MobileViewportHistoryConversationBrowserFixture() {
  useMobileViewport();
  return <HistoryConversationBrowserFixture />;
}

function PlanUiFixture({ mode }: { mode: string }) {
  const [detailRequests, setDetailRequests] = useState(0);
  const [authoritative, setAuthoritative] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const structuredPlan: Block = {
    kind: "process",
    item_id: "plan-ui-structured",
    processKind: "plan",
    phase: "end",
    status: "succeeded",
    title: "计划",
    explanation: authoritative ? "权威计划已同步" : "缓存计划",
    plan: authoritative ? [
      { step: "权威步骤一", status: "completed" },
      { step: "权威步骤二", status: "inProgress" },
      { step: "权威步骤三", status: "pending" },
    ] : [
      { step: "缓存步骤一", status: "completed" },
      { step: "缓存步骤二", status: "inProgress" },
      { step: "缓存步骤三", status: "pending" },
    ],
    done: true,
  };
  const unstructuredPlan: Block = {
    kind: "process",
    item_id: "plan-ui-unstructured",
    processKind: "plan",
    phase: "end",
    status: "succeeded",
    title: "旧版计划",
    detail: "先检查协议，再验证移动端，最后发布。",
    done: true,
  };
  const blocks = mode === "refresh" && refreshing
    ? []
    : mode === "unstructured"
    ? [unstructuredPlan]
    : mode === "mixed"
      ? [unstructuredPlan, structuredPlan]
      : [structuredPlan];
  const deferred = mode === "refresh" && !authoritative;
  return (
    <main style={{ minHeight: "100dvh", padding: 24 }}>
      <output data-testid="plan-detail-requests">{detailRequests}</output>
      <output data-testid="plan-refresh-state">{
        refreshing ? "loading" : authoritative ? "ready" : "cached"
      }</output>
      <ProcessTimeline blocks={blocks} done active={false}
        deferredCount={deferred ? 8 : 0}
        detailLoading={refreshing}
        onLoadDetail={deferred ? () => {
          setDetailRequests((value) => value + 1);
          setRefreshing(true);
          window.setTimeout(() => {
            setAuthoritative(true);
            setRefreshing(false);
          // Leave the provisional frame observable even on the slower mobile
          // WebKit project before replacing it with authoritative detail.
          }, 800);
          return true;
        } : undefined} />
    </main>
  );
}

function GoalUiFixture({ status, withPlan, hidden, longGoal, newerTurn,
  longPlan }: {
  status: string | null;
  withPlan: boolean;
  hidden: boolean;
  longGoal: boolean;
  newerTurn: boolean;
  longPlan: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [revealed, setRevealed] = useState(!hidden && status !== "none");
  const [planDetailRequests, setPlanDetailRequests] = useState(0);
  const loading = status === "loading";
  const goalStatus = status === "blocked" ? "blocked"
    : status === "complete" ? "complete" : "active";
  const goal: ThreadGoal | null = loading || status === "none" ? null : {
    threadId: "goal-fixture-thread",
    objective: longGoal
      ? "按照计划完成所有功能模块；每个模块验证无误后分别提交并推送，确保核心行为一致。".repeat(8)
      : "完成 protocol v30 发布并验证所有终端同步升级",
    status: goalStatus,
    engine: "codex",
    tokenBudget: 100_000,
    tokensUsed: 37_000,
    timeUsedSeconds: 321,
    updatedAt: 1_800_000_000,
    lastReason: "已完成协议兼容性检查，正在验证三端同步状态。",
  };
  const plan: TurnPlanProgress | null = withPlan ? {
    turnId: "goal-fixture-turn",
    block: {
      kind: "process",
      item_id: "goal-fixture-plan",
      processKind: "plan",
      phase: "update",
      status: "running",
      title: "计划",
      explanation: "让任务进度始终可以从会话底部查看。",
      plan: longPlan
        ? Array.from({ length: 28 }, (_, index) => ({
            step: `验证计划弹层内部滚动 ${index + 1}`,
            status: index < 2
              ? "completed" as const
              : index === 2 ? "inProgress" as const : "pending" as const,
          }))
        : [
            { step: "定位计划状态", status: "completed" },
            { step: "实现固定入口", status: "inProgress" },
            { step: "完成浏览器回归", status: "pending" },
          ],
      done: false,
    },
    detailLoading: false,
    needsDetail: true,
  } : null;
  const completedGoalRetired = completedGoalHasNewerUserTurn(
    goal,
    newerTurn ? [{
      id: "goal-fixture-next-turn",
      prompt: "开始 Goal 之后的新任务",
      blocks: [],
      done: false,
      ts: 1_800_000_001_000,
    }] : [],
  );
  return (
    <main className="pane"
      style={{ height: "100dvh", display: "flex", flexDirection: "column" }}>
      <div className="thread-shell" style={{ flexDirection: "row" }}>
        <div data-testid="goal-fixture-spacer" style={{ width: 0 }} />
        <div data-testid="goal-fixture-content" style={{ flex: 1 }} />
      </div>
      <output data-testid="plan-detail-requests">{planDetailRequests}</output>
      <GoalPanel engine="codex" goal={goal} revealed={revealed} open={open}
        loading={loading} completedGoalRetired={completedGoalRetired} plan={plan}
        onLoadPlanDetail={() => setPlanDetailRequests((value) => value + 1)}
        onOpen={() => setOpen(true)} onClose={() => setOpen(false)}
        onDismiss={() => setRevealed(false)} onSave={() => setOpen(false)}
        onClear={() => setRevealed(false)} />
      <div className="composer" data-testid="goal-fixture-composer"
        style={{ height: 48 }}>
        <div className="composer-in" />
      </div>
    </main>
  );
}

function InlineImageCapacityFixture() {
  const cacheRef = useRef<InlineImageAssetCache | null>(null);
  const requestSequenceRef = useRef(0);
  const occupiedRequestId = "occupied-inline-image";
  if (cacheRef.current === null) {
    cacheRef.current = new InlineImageAssetCache(1);
    if (!cacheRef.current.begin({
      sid: "other-session",
      path: "/tmp/occupied.png",
      previewId: "occupied-preview",
      requestId: occupiedRequestId,
    })) {
      throw new Error("inline image capacity fixture failed to occupy its cache");
    }
  }
  const [assets, setAssets] = useState<Record<string, InlineImageAsset>>({});
  const [attempts, setAttempts] = useState(0);
  const [networkLoads, setNetworkLoads] = useState(0);
  const loadImage = useCallback((path: string): boolean => {
    setAttempts((value) => value + 1);
    const sequence = ++requestSequenceRef.current;
    const accepted = cacheRef.current!.begin({
      sid: "capacity-session",
      path,
      previewId: `capacity-preview-${sequence}`,
      requestId: `capacity-request-${sequence}`,
    });
    if (!accepted) return false;
    setNetworkLoads((value) => value + 1);
    setAssets(cacheRef.current!.forSession("capacity-session"));
    return true;
  }, []);
  return (
    <main>
      <button type="button" data-testid="release-inline-capacity"
        onClick={() => cacheRef.current!.cancel(occupiedRequestId)}>
        release
      </button>
      <output data-testid="inline-load-attempts">{attempts}</output>
      <output data-testid="inline-network-loads">{networkLoads}</output>
      <MessageBlock text="![capacity image](/tmp/capacity.png)" done
        imageAssets={assets} onLoadImage={loadImage} />
    </main>
  );
}

function InlineImageEvictionFixture() {
  const sid = "two-visible-inline-images";
  const cacheRef = useRef<InlineImageAssetCache | null>(null);
  const requestSequenceRef = useRef(0);
  const settleTimersRef = useRef<number[]>([]);
  if (cacheRef.current === null) {
    cacheRef.current = new InlineImageAssetCache(1);
  }
  const [assets, setAssets] = useState<Record<string, InlineImageAsset>>({});
  const [attempts, setAttempts] = useState(0);
  const [networkLoads, setNetworkLoads] = useState(0);
  const noLoaderAssets = useMemo<Record<string, InlineImageAsset>>(() => ({
    "/tmp/no-loader-error.png": { status: "error" },
    "/tmp/no-loader-timeout.png": {
      status: "loading",
      startedAt: Date.now() - INLINE_IMAGE_REQUEST_TIMEOUT_MS - 1,
      requestGeneration: 1,
    },
  }), []);
  useEffect(() => () => {
    settleTimersRef.current.forEach((timer) => window.clearTimeout(timer));
  }, []);
  const loadImage = useCallback((path: string): boolean => {
    setAttempts((value) => value + 1);
    const cache = cacheRef.current!;
    if (cache.has(sid, path)) return true;
    const sequence = ++requestSequenceRef.current;
    const previewId = `visible-preview-${sequence}`;
    const requestId = `visible-request-${sequence}`;
    if (!cache.begin({ sid, path, previewId, requestId })) return false;
    setNetworkLoads((value) => value + 1);
    setAssets(cache.forSession(sid));
    settleTimersRef.current.push(window.setTimeout(() => {
      if (!cache.accept({
        v: 26,
        ts: Date.now() / 1000,
        type: "preview_asset",
        sid,
        path,
        preview_id: previewId,
        request_id: requestId,
        media_type: "image/png",
        data: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9WlK4h8AAAAASUVORK5CYII=",
      })) return;
      setAssets(cache.forSession(sid));
    }, 20));
    return true;
  }, []);
  return (
    <main>
      <output data-testid="visible-inline-attempts">{attempts}</output>
      <output data-testid="visible-inline-network-loads">{networkLoads}</output>
      <div data-testid="two-visible-inline-images">
        <MessageBlock text={[
          "![A](/tmp/visible-a.png)",
          "![B](/tmp/visible-b.png)",
        ].join("\n\n")} done imageAssets={assets} onLoadImage={loadImage} />
      </div>
      <div data-testid="inline-no-loader">
        <MessageBlock text={[
          "![error](/tmp/no-loader-error.png)",
          "![timeout](/tmp/no-loader-timeout.png)",
        ].join("\n\n")} done imageAssets={noLoaderAssets} />
      </div>
    </main>
  );
}

function HistoryImageFallbackErrorFixture() {
  const turnId = "history-fallback-error";
  const imageId = "history-fallback-image";
  const fallback = useMemo<QueryImg>(() => ({
    media_type: "image/png",
    data: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9WlK4h8AAAAASUVORK5CYII=",
  }), []);
  const [canonical, setCanonical] = useState(false);
  const [loads, setLoads] = useState(0);
  const [lastLoad, setLastLoad] = useState("");
  useEffect(() => setCanonical(true), []);
  const turns = useMemo<Turn[]>(() => [{
    id: turnId,
    prompt: "fallback 与 canonical 应共用一行图片布局",
    ...(canonical
      ? {
        imageRefs: [{
          image_id: imageId,
          media_type: "image/png" as const,
          width: 1,
          height: 1,
          byte_size: 68,
        }],
      }
      : { images: [fallback] }),
    blocks: [],
    done: true,
    ts: 1_000,
    doneTs: 2_000,
  }], [canonical, fallback]);
  const historyImageAssets = useMemo<Record<string, HistoryImageAsset>>(
    () => ({
      [historyImageAssetKey(turnId, imageId, "thumbnail")]: {
        status: "error",
      },
    }),
    [],
  );
  const loadHistoryImage = useCallback((
    requestedTurnId: string,
    requestedImageId: string,
    variant: HistoryImageVariant,
  ): boolean => {
    setLoads((value) => value + 1);
    setLastLoad(`${requestedTurnId}|${requestedImageId}|${variant}`);
    return true;
  }, []);
  return (
    <main style={{ height: "100dvh", display: "flex", flexDirection: "column" }}>
      <output data-testid="history-fallback-loads">{loads}</output>
      <output data-testid="history-fallback-last-load">{lastLoad}</output>
      <ChatView sid="history-fallback-session" turns={turns}
        engine="codex" historyRevision="history-fallback-r1"
        historyImageAssets={historyImageAssets}
        onLoadHistoryImage={loadHistoryImage}
        onEdit={() => {}} onGetDiff={() => {}} />
    </main>
  );
}

const UNSAFE_SVG = [
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 40">',
  "<script>document.documentElement.dataset.bad='yes'</script>",
  '<foreignObject x="0" y="0" width="20" height="20"><div>bad</div></foreignObject>',
  '<image href="https://example.com/tracker.png" width="10" height="10"/>',
  '<rect id="safe-svg-rect" width="120" height="40" fill="#6256b4"/>',
  "</svg>",
].join("");

function buildPdfFixture(): string {
  const firstPage = [
    "0.15 0.35 0.75 rg",
    "20 20 260 160 re f",
    "1 1 1 rg",
    "BT /F1 18 Tf 45 95 Td (cc-remote PDF page 1) Tj ET",
  ].join("\n");
  const secondPage = [
    "0.85 0.25 0.10 rg",
    "20 20 260 160 re f",
    "1 1 1 rg",
    "BT /F1 18 Tf 45 95 Td (cc-remote PDF page 2) Tj ET",
  ].join("\n");
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
    `<< /Length ${firstPage.length} >>\nstream\n${firstPage}\nendstream`,
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 7 0 R >>",
    `<< /Length ${secondPage.length} >>\nstream\n${secondPage}\nendstream`,
  ];
  let source = "%PDF-1.4\n";
  const offsets = [0];
  objects.forEach((object, index) => {
    offsets.push(source.length);
    source += `${index + 1} 0 obj\n${object}\nendobj\n`;
  });
  const xrefOffset = source.length;
  source += `xref\n0 ${objects.length + 1}\n`;
  source += "0000000000 65535 f \n";
  source += offsets.slice(1).map(
    (offset) => `${String(offset).padStart(10, "0")} 00000 n \n`,
  ).join("");
  source += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\n`;
  source += `startxref\n${xrefOffset}\n%%EOF\n`;
  return window.btoa(source);
}

function buildAnimatedGifFixture(): string {
  return [
    "R0lGODlhEAAQAPAAAAAAAP///yH/C05FVFNDQVBFMi4wAwEAAAAh+QQECgD/ACwA",
    "AAAAEAAQAAACDoSPqcvtD6OctNqLsz4FACH5BAUKAAAALAAAAAAQABAAAAIOjI+p",
    "y+0Po5y02ouzPgUAOw==",
  ].join("");
}

function ArtifactPreviewFixture({ kind }: {
  kind: "gif" | "invalid-gif" | "html" | "pdf" | "svg"
    | "markdown-svg" | "markdown-source" | "markdown-html" | "markdown-github-html";
}) {
  const [openedFile, setOpenedFile] = useState("");
  const svgData = window.btoa(UNSAFE_SVG);
  const htmlReadme = kind === "markdown-github-html"
    ? MARKDOWN_HTML_GITHUB_README : MARKDOWN_HTML_LOCAL_README;
  const artifact = kind === "pdf"
    ? {
      file: "report.pdf",
      sid: "artifact-preview-session",
      requestId: "artifact-preview-request",
      kind: "pdf" as const,
      data: buildPdfFixture(),
      mediaType: "application/pdf" as const,
      size: 1_024,
      mtimeNs: "1",
    }
    : kind === "gif" || kind === "invalid-gif"
    ? {
      file: "animation.gif",
      sid: "artifact-preview-session",
      requestId: "artifact-preview-request",
      kind: "image" as const,
      data: kind === "gif"
        ? buildAnimatedGifFixture()
        : window.btoa("GIF89a-invalid"),
      mediaType: "image/gif" as const,
      size: 128,
      mtimeNs: "1",
    }
    : kind === "html"
    ? {
      file: "preview.html",
      sid: "artifact-preview-session",
      requestId: "artifact-preview-request",
      kind: "html" as const,
      content: `<!doctype html><html><head>
        <style>#head-style { color: rgb(12, 34, 56); }</style>
        </head><body><div id="head-style">head css retained</div>
        <div id="visualization-theme" style="color:var(--foreground);background:var(--background);border:1px solid var(--border)">visualization theme</div>
        <button id="counter">计数 0</button>
        <script>
          document.body.dataset.scriptRan = "yes";
          try { parent.document.body.dataset.previewEscaped = "yes"; }
          catch (_) { document.body.dataset.parentBlocked = "yes"; }
          try { localStorage.getItem("preview-isolation-test"); }
          catch (_) { document.body.dataset.storageBlocked = "yes"; }
          fetch("https://cc-remote-preview-test.invalid/should-not-load")
            .catch(() => { document.body.dataset.networkBlocked = "yes"; });
          let count = 0;
          document.getElementById("counter").onclick = event => {
            event.currentTarget.textContent = "计数 " + (++count);
          };
        </script></body></html>`,
      size: 360,
      mtimeNs: "1",
      revision: "a".repeat(64),
    }
    : kind === "svg"
    ? {
      file: "diagram.svg",
      sid: "artifact-preview-session",
      requestId: "artifact-preview-request",
      kind: "image" as const,
      data: svgData,
      mediaType: "image/svg+xml",
      size: UNSAFE_SVG.length,
      mtimeNs: "1",
    }
    : kind === "markdown-html" || kind === "markdown-github-html"
    ? {
      file: "readme_zh.md", sid: "artifact-preview-session",
      requestId: "artifact-preview-request", kind: "md" as const,
      content: htmlReadme, size: htmlReadme.length,
      mtimeNs: "1", revision: "c".repeat(64),
      assets: {
        "header.svg": {
          mediaType: "image/svg+xml",
          data: window.btoa(MARKDOWN_HTML_HEADER_SVG),
        },
        "local-logo.png": {
          mediaType: "image/png",
          data: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3x8AAAAASUVORK5CYII=",
        },
      },
    }
    : kind === "markdown-source"
    ? {
      file: "LONG_REPORT.md",
      sid: "artifact-preview-session",
      requestId: "artifact-preview-request",
      kind: "md" as const,
      content: Array.from(
        { length: 160 },
        (_, index) => `## Section ${index + 1}\n\nMobile source line ${index + 1}.`,
      ).join("\n\n"),
      size: 12_000,
      mtimeNs: "1",
      revision: "c".repeat(64),
    }
    : {
      file: "README.md",
      sid: "artifact-preview-session",
      requestId: "artifact-preview-request",
      kind: "md" as const,
      content: "![diagram](diagram.svg)",
      size: 23,
      mtimeNs: "1",
      revision: "b".repeat(64),
      assets: {
        "diagram.svg": {
          mediaType: "image/svg+xml",
          data: svgData,
        },
      },
    };
  return <main style={{ height: "100dvh" }}>
    <output hidden data-testid="artifact-opened-file">{openedFile}</output>
    <ArtifactPanel artifact={artifact} active="diff" hasBtw={false}
      theme={kind === "html" ? "dark" : "light"}
      onOpenFile={(path, line) => setOpenedFile(`${path}:${line || ""}`)}
      onTab={() => {}} onClose={() => {}} />
  </main>;
}

function HistoricalFilesFixture() {
  const manyFiles = new URLSearchParams(location.search).has("many-files");
  const [other, setOther] = useState(false);
  const [opened, setOpened] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [dark, setDark] = useState(false);
  useEffect(() => {
    document.documentElement.dataset.engine = "codex";
    document.documentElement.dataset.theme = dark ? "dark" : "light";
  }, [dark]);
  const paths = manyFiles ? Array.from({ length: 130 }, (_, index) => `src/file-${index}.py`)
    : ["src/perception/camera.py", "tests/camera.py", "config/rig.json", "deploy/Dockerfile", "deploy/compose.yaml", "scripts/check.sh", "README.md"];
  const wireTurns = [1, 2].map((number) => ({
    id: `history-${number}`, prompt: number === 1 ? "完成相机预览相关修改" : "再调整一下相机配置",
    done: true, ts: number, blocks: [{ kind: "text", done: true, message_id: `answer-${number}`,
      channel: "final", text: number === 1 ? "已完成代码、配置和使用说明的调整。" : "相机配置已更新，上一轮的改动记录保持不变。" }],
    fileChanges: { revision: manyFiles ? `revision-${number}-${refresh}` : `revision-${number}`,
      ...(manyFiles && number === 1 ? { total_files: 130, total_additions: 130, total_deletions: 130, next_offset: 64 } : {}),
      files: (number === 1 ? paths.slice(0, 64) : paths.slice(0, 1)).map((path, index) => ({
      path: `/workspace/robot-viewer/${path}`,
      state: index === 6 ? "unavailable" as const : "available" as const,
      ...(index === 6 ? {} : { additions: 12 + index, deletions: index + 1 }),
    })) },
  }));
  const turns = summaryHistoryTurns({ v: PROTOCOL_VERSION, ts: refresh, type: "history",
    session_id: "historical-files", detail: "summary", turns: wireTurns, events: [], has_more: false }) ?? [];
  return <main style={{ display: "flex", flexDirection: "column", height: "100dvh" }}>
    <header style={{ padding: "12px 20px", flex: "none" }}>Code · 历史改动</header>
    <ChatView sid={other ? "other-history" : "historical-files"} engine="codex"
      turns={other ? [] : turns} activeTurnId={null}
      onOpenArchivedDiff={(turn, revision, path) => setOpened(`${turn}:${revision}:${path}`)}
      onLoadFilePage={async (turn, revision, offset, signal) => {
        const response = await fetch(`/fixture-turn-files?turn=${turn}&revision=${revision}&offset=${offset}`, { signal });
        if (!response.ok) throw new Error("文件清单加载失败，请重试。");
        return response.json();
      }}
      onPreviewMarkdown={(path) => setOpened(`preview:${path}`)} />
    <footer style={{ display: "flex", gap: 12, flexWrap: "wrap", padding: 12, flex: "none" }}>
      <button onClick={() => setOther((value) => !value)}>切换会话</button>
      <button onClick={() => setRefresh((value) => value + 1)}>刷新历史</button>
      <button onClick={() => setDark((value) => !value)}>切换主题</button>
      <output data-testid="historical-file-opened" style={{ overflowWrap: "anywhere", fontSize: 10 }}>{opened}</output>
    </footer>
  </main>;
}

function TurnRegressionFixture() {
  const sid = "turn-regressions";
  const [state, dispatch] = useReducer(reduce, {
    ...initialState, focusedSid: sid, runtimes: { [sid]: {
      ...createRuntime(), historyRevision: "stable", turns: [
        { id: "history-one", prompt: "long copyable answer", done: true,
          ts: 1, doneTs: 2, fileChanges: { revision: "immutable-one", files: [
            { path: "src/code.py", state: "available", additions: 1, deletions: 1 },
          ] }, blocks: [{ kind: "text", message_id: "long-answer", done: true, channel: "final",
            text: "```text\n" + Array.from({ length: 90 }, (_, i) => `Copy entire block line ${i}`).join("\n") + "\n```\n\n"
              + "```text\n" + Array.from({ length: 60 }, (_, i) => `Second block line ${i}`).join("\n") + "\n```" }] },
        { id: "working", prompt: "continue work", done: false, blocks: [
          { kind: "tool", tool_use_id: "command", message_id: "m", tool: "Bash", input: { command: "true" },
            done: true, result: { content: "ok", is_error: false } },
        ] },
      ] as Turn[],
    } },
  });
  const [opened, setOpened] = useState("");
  const runtime = state.runtimes[sid];
  return <main style={{ display: "flex", flexDirection: "column", height: "100dvh" }}>
    <header style={{ height: 54, flex: "none" }}>Thread header</header>
    <ChatView sid={sid} engine="codex" turns={runtime.turns} historyRevision="stable"
      activeTurnId={runtime.acceptancePending ?? "working"}
      onOpenArchivedDiff={(turn, revision, path) => setOpened(`${turn}:${revision}:${path}`)} />
    <footer style={{ height: 60, flex: "none" }}>
      <button data-testid="regression-send" onClick={() => dispatch({ type: "query_sent", sid,
        msg_id: "new-question", prompt: "sent without scrolling", ts: 3 })}>Send</button>
      <button data-testid="regression-stream" onClick={() => dispatch({ type: "event", event: {
        v: PROTOCOL_VERSION, ts: 4, type: "delta", sid, turn_id: "new-question", message_id: "new-answer",
        text: "streamed text\n".repeat(50), channel: "final",
      } })}>Stream</button>
      <output data-testid="regression-opened">{opened}</output>
    </footer>
  </main>;
}

function CodeCopyThemeFixture({ theme }: { theme: "light" | "dark" }) {
  useEffect(() => {
    const root = document.documentElement;
    const previousEngine = root.dataset.engine;
    const previousTheme = root.dataset.theme;
    root.dataset.engine = "codex";
    root.dataset.theme = theme;
    return () => {
      if (previousEngine === undefined) delete root.dataset.engine;
      else root.dataset.engine = previousEngine;
      if (previousTheme === undefined) delete root.dataset.theme;
      else root.dataset.theme = previousTheme;
    };
  }, [theme]);
  return <main style={{ minHeight: "100dvh", padding: 24, background: "var(--bg)" }}>
    <MessageBlock text={"```text\nfix(web): keep copy actions readable\n```"} done />
  </main>;
}

function LocalFileLinkFixture() {
  return <main style={{ minHeight: "100dvh", padding: 24 }}>
    <MessageBlock
      text="[release-test](/tmp/qwen3-tts-v017-release-test:42)"
      done onOpenFile={() => {}} />
  </main>;
}

function CodexVisualizationFixture() {
  const [opened, setOpened] = useState("");
  return <main style={{ minHeight: "100dvh", padding: 24 }}>
    <output data-testid="visualization-opened-path">{opened}</output>
    <MessageBlock
      text={"visualize{\"path\":\"/tmp/private/concept.html\","
        + "\"title\":\"结构原理草图\"}"}
      done onOpenFile={(path) => setOpened(path)} />
  </main>;
}

function MarkdownDisclosureFixture() {
  const [complete, setComplete] = useState(false);
  const [opened, setOpened] = useState("");
  const params = new URLSearchParams(window.location.search);
  const title = params.has("long-title")
    ? `文件清单：${"long_source_filename_".repeat(12)}.tsx，21 个源文件，无删除`
    : "文件清单：21 个源文件，无删除";
  const files = ["README.md", ...[
    ".gitattributes", ".gitignore", "README.md", "DESIGN.md", "tools/model.py",
    "tools/build.py", "tools/check.py", "tools/assembly_check.py",
    "tools/structural_screen.py", "tools/verify.py", "tools/publish.py",
    "tools/visual_qa.mjs", "viewer/index.html", "viewer/style.css",
    "viewer/app.js", "tests/test_model.py", "tests/test_assembly.py",
    "config/assembly.json", "config/materials.json", "output/manifest.json",
  ].map((path) => `modules/assembly/${path}`)];
  const body = params.has("file-list")
    ? "仓库：example-workspace\n\n" + files.map((path, index) =>
      `- ${index === 0 ? "修改" : "新建"}：\`${path}\``,
    ).join("\n") + "\n\n"
    : "- **Added** `README.md`\n- [source](/tmp/source.py)\n\n"
      + "<details open>\n<summary>嵌套内容</summary>\n\nInner body\n\n</details>\n\n";
  const text = `Before\n\n<details>\n<summary>${title}</summary>\n\n`
    + body
    + (complete ? "- Last streamed item\n\n</details>\n\nAfter" : "");
  return <main style={{ minHeight: "100dvh", padding: 24 }}>
    <button type="button" onClick={() => setComplete(true)}>Finish stream</button>
    <output data-testid="disclosure-opened-path">{opened}</output>
    <section data-testid="markdown-disclosure">
      <MessageBlock text={text} done={complete} onOpenFile={setOpened} />
    </section>
    <section data-testid="inert-disclosure-html">
      <MessageBlock done text={'<details>\n<summary>Unsafe content</summary>\n\n'
        + '<img src="https://example.com/raw-image" onerror="alert(1)">\n\n'
        + '<script>alert(2)</script>\n\n</details>'} />
    </section>
  </main>;
}

function CodexFileCitationFixture() {
  const [opened, setOpened] = useState("");
  return <main style={{ minHeight: "100dvh", padding: 24 }}>
    <output data-testid="citation-opened-path">{opened}</output>
    <section data-testid="valid-citations">
      <MessageBlock
        text={"英文版 :codex-file-citation{path=\"/tmp/reports/final report.pdf\" "
          + "label=\"报告 } 终版\" purpose=\"output\"}，动画 "
          + ":codex-file-citation{purpose=\"output\" "
          + "path=\"/tmp/demo} final.gif\"}。"}
        done onOpenFile={(path) => setOpened(path)} />
    </section>
    <section data-testid="invalid-citation">
      <MessageBlock
        text={'保留 :codex-file-citation{path="/tmp/report.pdf" invalid tail}'}
        done onOpenFile={(path) => setOpened(path)} />
    </section>
  </main>;
}

const rootParams = new URLSearchParams(window.location.search);
createRoot(document.getElementById("root")!).render(
  rootParams.has("artifact-html")
    ? <ArtifactPreviewFixture kind="html" />
    : rootParams.has("artifact-pdf")
    ? <ArtifactPreviewFixture kind="pdf" />
    : rootParams.has("artifact-gif")
    ? <ArtifactPreviewFixture kind="gif" />
    : rootParams.has("artifact-invalid-gif")
    ? <ArtifactPreviewFixture kind="invalid-gif" />
    : rootParams.has("artifact-svg")
    ? <ArtifactPreviewFixture kind="svg" />
    : rootParams.has("artifact-markdown-svg")
    ? <ArtifactPreviewFixture kind="markdown-svg" />
    : rootParams.has("artifact-markdown-source")
    ? <ArtifactPreviewFixture kind="markdown-source" />
    : rootParams.has("artifact-markdown-github-html")
    ? <ArtifactPreviewFixture kind="markdown-github-html" />
    : rootParams.has("artifact-markdown-html")
    ? <ArtifactPreviewFixture kind="markdown-html" />
    : rootParams.has("historical-files")
    ? <HistoricalFilesFixture />
    : rootParams.has("turn-regressions")
    ? <TurnRegressionFixture />
    : rootParams.has("code-copy-theme")
    ? <CodeCopyThemeFixture
        theme={rootParams.get("theme") === "light" ? "light" : "dark"} />
    : rootParams.has("local-file-link")
    ? <LocalFileLinkFixture />
    : rootParams.has("codex-visualization")
    ? <CodexVisualizationFixture />
    : rootParams.has("codex-file-citation")
    ? <CodexFileCitationFixture />
    : rootParams.has("markdown-disclosure")
    ? <MarkdownDisclosureFixture />
    : rootParams.has("inline-image-capacity")
    ? <InlineImageCapacityFixture />
    : rootParams.has("inline-image-eviction")
    ? <InlineImageEvictionFixture />
    : rootParams.has("history-image-fallback-error")
    ? <HistoryImageFallbackErrorFixture />
    : rootParams.has("reducer-pipeline")
    ? <ReducerHistoryBrowserFixture />
    : rootParams.has("codex-live-burst")
    ? <CodexLiveBurstFixture />
    : <HistoryBrowserFixture />,
);
