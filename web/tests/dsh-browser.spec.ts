import { expect, test, type Page } from "@playwright/test";
import { PROTOCOL_VERSION, type DshState, type DshItem } from "../src/protocol";
import { staticPng } from "./fixtures/png";

const sid = "dsh@native-session";
const nativeState: Omit<DshState, "v" | "ts"> = { type: "dsh_state", sid, connected: true,
  agent_preset: "standard", permission: "read-only",
  permissions: [{ value: "read-only", name: "只读", description: "仅查看文件" },
    { value: "workspace-write", name: "工作区写入", description: "修改当前工作目录" },
    { value: "danger-full-access", name: "danger-full-access", description: "完整访问权限" }],
  commands: [{ name: "goal", description: "设置并管理目标", attachments: true },
    { name: "plan", description: "进入计划模式；/plan off 退出", attachments: true },
    { name: "permission", description: "权限预设", attachments: false },
    { name: "compact", description: "压缩上下文", attachments: false }],
  goal: { id: "native-goal", revision: 2, objective: "完成 DSH 适配并验证手机界面", phase: "active",
    rounds: 3, max_rounds: 32, activation: "disarmed" },
};

async function mockDshRelay(page: Page, { running = false, commandSuccess = false, features = false, product = false, sidebarCards = false, failSettingsNamespace = "" } = {}) {
  const commands: Record<string, unknown>[] = [];
  let historyTurns: Record<string, unknown>[] | undefined;
  let historyPagination: { has_more: boolean; oldest_id?: string } = { has_more: false };
  let archived = false;
  let terminalClosed = false;
  let currentGoal = nativeState.goal;
  let recommendationDismissed = false;
  let cleanupCount = 8;
  const marketSources = [{ id: "source", title: "本机配置的插件源", data: { revision: "sources-1", url: "https://example.test/plugins.json" } }];
  const settingItems: DshItem[] = [
    { id: "subagent", title: "subagent", detail: "", state: "settings", has_children: false, controllable: true, data: { revision: 4, applies: "live", values: { maxDepth: 1, maxActiveSubagents: 8 }, constraints: { maxDepth: { min: 0, step: 1 }, maxActiveSubagents: { min: 1, step: 1 } } } },
    { id: "agent-loop", title: "agent-loop", detail: "", state: "settings", has_children: false, controllable: true, data: { revision: 7, applies: "live", values: { maxParallelToolCalls: 10 }, constraints: { maxParallelToolCalls: { min: 1, step: 1 } } } },
  ];
  const context = { type: "context_report", sid, total_tokens: 24000,
    max_tokens: 64000, percentage: 37.5, available: true,
    source: "recent_turn", categories: [] };
  let emit: (event: Record<string, unknown>) => void = () => {};
  await page.addInitScript(() => {
    localStorage.setItem("cc_remote_engine", "dsh");
    localStorage.setItem("cc_remote_machine", "dsh-machine");
  });
  await page.route("**/api/**", route => route.fulfill({ status: 404 }));
  await page.route("**/api/session", route => route.fulfill({ json: {} }));
  await page.route("**/api/viewers/pages", route => route.fulfill({ json: { pages: [] } }));
  await page.route("**/api/devices", route => route.fulfill({ json: {
    devices: [{ machine_id: "dsh-machine", label: "Development", online: true }],
  } }));
  await page.routeWebSocket(/\/ws(?:\?|$)/, socket => {
    emit = event => {
      if (event.type === "dsh_state" && "goal" in event) currentGoal = event.goal as DshState["goal"];
      socket.send(JSON.stringify({ v: PROTOCOL_VERSION, ts: Date.now() / 1000, ...event }));
    };
    const snapshot = () => {
      emit({ type: "snapshot", sid, cc_session_id: sid, state: running ? "running" : "idle",
        tail_text: "", cwd: "/tmp/dsh-test", generation: "dsh-generation" });
      emit({ ...nativeState, ...(features ? { plan_active: false, plan_pending: false, jobs: [{ id: "job-1", label: "构建任务", status: "running", detail: "正在编译" }] } : {}) });
      emit({ type: "model", sid, model: "dsh:deepseek-official:deepseek-flash" });
      emit({ type: "effort", sid, effort: "off" });
      emit({ type: "perm", sid, mode: "read-only" });
      emit(context);
      emit({ type: "replay_end", sid, to_seq: 0, truncated: false });
    };
    socket.onMessage(raw => {
      const cmd = JSON.parse(String(raw)) as Record<string, unknown>;
      commands.push(cmd);
      if (cmd.type === "hello") snapshot();
      if (cmd.type === "get_models") emit({ type: "models", engine: cmd.engine, models: cmd.engine === "dsh" ? [
        { id: "dsh:deepseek-official:deepseek-flash", display_name: "DeepSeek-V41-Flash", efforts: ["off", "low"], default_effort: "low" },
      ] : [], default_model: "dsh:deepseek-official:deepseek-flash", default_effort: "low", dsh_permissions: nativeState.permissions, dsh_presets: [
        { id: "standard", name: "标准模式", description: "处理代码、文件和资料，适合大多数任务。Agent 会按需使用检索、编辑和终端等工具。", available: true, is_default: true },
        { id: "ptc", name: "PTC 模式", description: "包含标准模式的所有能力，更适合批量调用工具，并对结果进行筛选、整理、去重、统计或汇总的任务。", available: true, is_default: false },
        { id: "minimal", name: "极简模式", description: "Agent 使用终端和图片读取工具，并支持上下文压缩，适合测试和对比其基础表现。", available: true, is_default: false },
        { id: "cordis", name: "创造模式", description: "用对话定制 DSH：让 Agent 编写插件，添加新功能或界面；也能组合工具和提示词，创建自己的模式。", available: true, is_default: false },
      ] });
      if (cmd.type === "list_sessions") {
        // Match the real runtime: DSH has no Work catalog. A permissive mock
        // would hide unsupported background reads during a harness switch.
        if (cmd.engine === "dsh" && cmd.space === "work") emit({ type: "error",
          code: "dsh_unsupported", request_id: cmd.cmd_id, message: "DSH 目前支持 Code。" });
        else emit({ type: "session_list", engine: cmd.engine ?? "claude", space: cmd.space ?? "code", request_id: cmd.cmd_id,
          sessions: cmd.engine === "dsh" ? [{ session_id: sid, engine: "dsh", space: "code", cwd: "/tmp/dsh-test",
            summary: sidebarCards ? "对齐 auriga-runtime 架构设计与当前实现" : "DSH 原生会话", state: running ? "running" : "idle", last_modified: "100", tag: archived ? "archived" : null },
            ...(sidebarCards ? [{ session_id: "dsh@other", engine: "dsh", space: "code", cwd: "/tmp/dsh-test",
              summary: "检查仓库构建问题", state: "idle", last_modified: "99" }] : [])] : [] });
      }
      if (features && cmd.type === "archive_session") {
        archived = cmd.archived !== false;
        emit({ type: "session_list_invalidated", engine: "dsh" });
      }
      if (cmd.type === "open_btw") {
        emit({ type: "btw_opened", sid: "dsh@side", btw_sid: "dsh@side", parent_sid: sid, engine: "dsh",
          request_id: cmd.request_id, revision: 1, created_at: 1 });
        emit({ type: "snapshot", sid: "dsh@side", state: "idle", tail_text: "", cwd: "/tmp/dsh-test", generation: "dsh-generation" });
        emit({ type: "model", sid: "dsh@side", model: "dsh:deepseek-official:deepseek-flash" });
        emit({ type: "effort", sid: "dsh@side", effort: "off" });
        emit({ ...nativeState, sid: "dsh@side" });
      }
      if (cmd.type === "sync_btw") {
        // Match the real private replay, including its rebuild barrier. A
        // Snapshot alone hid DSH's native-id history recovery regression.
        emit({ type: "replay_start", sid: cmd.sid, generation: "dsh-generation",
          from_seq: 0, to_seq: 0, truncated: false, rebuild: true });
        emit({ type: "replay_end", sid: cmd.sid, to_seq: 0, truncated: false });
        emit({ type: "snapshot", sid: cmd.sid, state: "idle", tail_text: "", cwd: "/tmp/dsh-test", generation: "dsh-generation" });
      }
      if (cmd.type === "close_btw") emit({ type: "btw_closed", sid: cmd.sid, btw_sid: cmd.sid, parent_sid: sid, revision: 2 });
      if (cmd.type === "switch_session") { emit({ type: "session_focus", session_id: cmd.session_id }); snapshot(); }
      if (cmd.type === "get_history") emit({ type: "history", sid, session_id: sid, revision: "dsh-history",
        generation: "dsh-generation", detail: "summary", events: [], turns: historyTurns ?? [{ id: "native-prompt", clientMsgId: "native-prompt",
          prompt: "验证图片、引导与原生控件", done: !running, forkPointId: "dsh-seq-5", blocks: [
            ...(features ? [{ kind: "tool", message_id: "present", tool_use_id: "present-file", tool: "present",
              input: { files: [{ path: "/tmp/dsh-test/report.xlsx", description: "路线图" }] },
              result: { content: "Presented", is_error: false }, done: true }] : []),
            { kind: "text", message_id: "answer", text: features ? "[项目目录](/tmp/dsh-test/dir) 和 [文件行号](/tmp/dsh-test/README.md:7)" : "DSH 已准备好。", channel: "final", done: true },
          ] }], ...historyPagination });
      if (cmd.type === "set_dsh_control" && cmd.kind === "terminal" && JSON.parse(String(cmd.value)).action === "input") {
        setTimeout(() => emit({ type: "dsh_command_result", sid: cmd.sid, request_id: cmd.cmd_id, status: "success", text: "" }), 350);
        return;
      }
      if (cmd.type === "set_dsh_control" && cmd.kind === "terminal" && JSON.parse(String(cmd.value)).action === "attach") {
        const body = JSON.parse(String(cmd.value));
        emit({ type: "dsh_read_result", sid: cmd.sid, request_id: body.lease, kind: "terminal_output", next_seq: 1,
          items: [{ id: "1", title: "screen", data: { type: "snapshot", screen: "DSH terminal ready\r\n~/workspace $ ", writable: true, info: {} } }] });
      }
      if (cmd.type === "set_dsh_control" && cmd.kind === "terminal" && JSON.parse(String(cmd.value)).action === "close") {
        terminalClosed = true;
        setTimeout(() => emit({ type: "dsh_command_result", sid, request_id: cmd.cmd_id, status: "success", text: "终端已停止" }), 350);
        return;
      }
      if (terminalClosed && cmd.type === "read_dsh" && cmd.kind === "terminal_output") {
        emit({ type: "dsh_read_result", sid, request_id: cmd.cmd_id, kind: cmd.kind, items: [], error: "终端连接已过期" }); return;
      }
      if (cmd.type === "set_dsh_control" && cmd.kind === "manage") {
        const body = JSON.parse(String(cmd.value));
        if (commandSuccess && body.action === "recommendation" && body.verb === "dismiss") recommendationDismissed = true;
        if (commandSuccess && body.action === "schedule_cleanup") cleanupCount = 0;
        if (commandSuccess && body.action === "market_source_add") marketSources.push({ id: "added", title: "新增插件源", data: { revision: "sources-2", url: body.url } });
        if (body.action === "settings") {
          const row = settingItems.find(item => item.id === body.ns)!;
          const success = commandSuccess && body.ns !== failSettingsNamespace && row.data!.revision === body.revision;
          if (success) row.data = { ...row.data, revision: body.revision + 1, values: { ...(row.data!.values as object), ...body.patch } };
          emit({ type: "dsh_command_result", sid, request_id: cmd.cmd_id, status: success ? "success" : "error", text: success ? "已保存" : "设置已变化，请刷新后重新修改" });
          emit({ type: "command_ack", client_id: cmd.client_id, cmd_id: cmd.cmd_id });
          return;
        }
      }
      if (cmd.type === "set_dsh_control") emit({ type: "dsh_command_result", sid, request_id: cmd.cmd_id,
        status: commandSuccess ? "success" : "error", text: commandSuccess ? "命令已执行" : "当前目标不能替换，请使用 /goal edit" });
      if (cmd.type === "get_context") emit({ ...context, request_id: cmd.cmd_id });
      if (cmd.type === "act_dsh_goal") {
        if (commandSuccess) {
          const next = cmd.action === "clear" ? null : cmd.action === "create" ? {
            id: "created-goal", revision: 1, objective: String(cmd.objective), phase: "active" as const,
            rounds: 0, max_rounds: Number(cmd.max_rounds), activation: "armed" as const,
          } : { ...currentGoal!, revision: currentGoal!.revision + 1,
            ...(cmd.action === "edit" ? { objective: String(cmd.objective ?? currentGoal!.objective), max_rounds: Number(cmd.max_rounds ?? currentGoal!.max_rounds) }
              : { phase: cmd.action === "resume" ? "active" : cmd.action === "pause" ? "paused" : "complete",
                activation: cmd.action === "resume" ? "armed" : "disarmed" }),
          };
          emit({ ...nativeState, goal: next });
        }
        emit({ type: "dsh_command_result", sid: cmd.sid, request_id: cmd.cmd_id,
          status: commandSuccess ? "success" : "error", text: commandSuccess ? "" : "目标已更新，请重新打开编辑后提交。" });
      }
      if (cmd.type === "get_engine_capabilities") {
        // Another browser may still own the wrapper's Codex focus. Only an
        // explicit DSH session target can safely read this native catalog.
        if (cmd.sid && cmd.sid !== sid) emit({ type: "error", code: "dsh_invalid_session",
          request_id: cmd.cmd_id, message: "请选择 DSH 会话。" });
        else emit({ type: "engine_capabilities", sid: cmd.sid ?? null, engine: "dsh", space: "code", cwd: cmd.cwd || "/tmp/dsh-test",
          request_id: cmd.cmd_id, skills_only: cmd.skills_only, dsh_commands: nativeState.commands, items: [{ kind: "skill", id: "review", name: "review", description: "审查变更", enabled: true, actions: [] }] });
      }
      if (features && cmd.type === "get_agent_detail") emit({ type: "agent_detail", sid: cmd.session_id, session_id: cmd.session_id,
        run_id: cmd.run_id, request_id: cmd.request_id, revision: "dsh-history", detail_revision: "dsh-agent-1", title: cmd.run_id === "dsh@grandchild" ? "回归测试" : "检查构建",
        status: "running", parent_run_id: cmd.run_id === "dsh@grandchild" ? "dsh@child" : null,
        authoritative: true, through_seq: 0, events: [
          { type: "user_msg", msg_id: "child-prompt", prompt: "检查构建并验证回归", v: PROTOCOL_VERSION, ts: 1 },
          { type: "tool_use", message_id: "child-work", tool_use_id: "ptc-root", tool: "run_code", input: { code: "return await tools.read({file_path: 'README.md'})" }, v: PROTOCOL_VERSION, ts: 1 },
          { type: "tool_use", message_id: "child-work", tool_use_id: "ptc-read", parent_id: "ptc-root", tool: "read", input: { file_path: "README.md" }, v: PROTOCOL_VERSION, ts: 1 },
          { type: "tool_result", tool_use_id: "ptc-read", content: "项目说明", is_error: false, v: PROTOCOL_VERSION, ts: 1 },
          { type: "tool_result", tool_use_id: "ptc-root", content: "已检查", is_error: false, v: PROTOCOL_VERSION, ts: 1 },
          { type: "assistant_msg_start", message_id: "child-answer", channel: "final", v: PROTOCOL_VERSION, ts: 2 },
          { type: "delta", message_id: "child-answer", channel: "final", text: "已经检查构建", v: PROTOCOL_VERSION, ts: 2 },
          { type: "assistant_msg_end", message_id: "child-answer", channel: "final", v: PROTOCOL_VERSION, ts: 2 },
          ...(cmd.run_id === "dsh@grandchild" ? [] : [{ type: "process", item_id: "nested", kind: "agent", phase: "end", status: "unknown", server: "dsh", tool: "subagent_reference", title: "回归测试", input: { agent_run_id: "dsh@grandchild" }, v: PROTOCOL_VERSION, ts: 3 }]),
        ] });
      if (features && cmd.type === "read_dsh") {
        const items = cmd.kind === "search" ? [{ id: "hit", sid, title: "DSH 原生会话", detail: `匹配 ${cmd.query}` }]
          : product && cmd.kind === "plugins" && cmd.query === "recommendations" ? recommendationDismissed ? [] : [{ id: "recommend-1", title: "持久终端", detail: "让开发服务在后台持续运行。", state: "proposed", controllable: true, data: { owner: "native-prompt", name: "@example/plugin" } }]
          : cmd.kind === "references" ? [
            { id: "file", title: "report notes.md", path: "report notes.md", state: "file", mention: '@"report notes.md"' },
            { id: "session", sid, title: "相关会话", state: "session", mention: "@[相关会话](dsh-session:native-session)" }]
          : cmd.kind === "subagents" ? [{ id: "child", sid: "dsh@child", title: "检查构建", detail: "", mode: "continuable", state: "running", has_children: true, controllable: true }]
          : cmd.kind === "workflows" ? [{ id: "workflow", title: "审查工作流", state: "running", data: { members: [{ sid: "dsh@child", label: "检查构建", phase: "审查", state: "running" }] } }]
          : product && cmd.kind === "trajectory" && cmd.query === "stats" ? [{ id: "12", title: "第 12 轮", state: "completed", data: { turn: 12, wall: 18600, model: 12400, tool: 6200, input: 24100, output: 1200, cacheHit: .84, partial: false, truncated: false, pendingTools: 0,
            steps: [{ step: 1, wall: 18600, model: 12400, tool: 6200, input: 24100, output: 1200, ttft: 100 }], tools: [{ name: "读取文件", step: 1, duration: 100 }, { name: "运行命令", step: 1, duration: 6100 }] } }]
          : cmd.kind === "trajectory" ? [{ id: "1", title: "第 1 轮", state: "completed", data: { input: 3000, output: 420, events: [{ type: "tool/call", label: "run_code" }] } }]
          : cmd.kind === "settings" ? settingItems
          : cmd.kind === "presets" ? [{ id: "standard", title: "标准", state: "default", detail: "文件与命令" }, { id: "cordis", title: "配置助手", state: "available" }]
          : cmd.kind === "schedules" && cmd.query === "sessions" ? [{ id: sid, title: "当前项目", detail: "/tmp/dsh-test" }]
          : product && cmd.kind === "schedules" && cmd.query === "cleanup" ? [{ id: "cleanup", data: { count: cleanupCount, revision: "cleanup-1" } }]
          : cmd.kind === "schedules" ? [{ id: "review", sid, title: "每周审查", detail: "检查变更", state: "active", data: { id: "review", kind: "every", title: "每周审查", prompt: "检查变更", everySeconds: 300, updatedAt: "rev1", target: { kind: "session", sid }, scheduledAt: "2026-10-22T00:00:00Z" } }]
          : cmd.kind === "schedule_history" ? [{ id: "delivery", sid, title: "2026-10-22 08:00", detail: "delivery-1", state: "delivered" }]
          : cmd.kind === "bundles" && cmd.query && JSON.parse(String(cmd.query)).sources ? marketSources
          : cmd.kind === "bundles" && cmd.query && JSON.parse(String(cmd.query)).detail ? [{ id: "detail", detail: "## 使用方式\n\n- 启动服务\n- 读取输出\n\n```sh\nnpm run dev\n```\n\n<script>window.fixtureBad = true</script>\n\n[unsafe](javascript:alert(1)) ![preview](https://tracker.invalid/image.png)", state: "found" }]
          : product && cmd.kind === "bundles" && cmd.query && JSON.parse(String(cmd.query)).market ? [{ id: "market-plugin", title: "市场插件", detail: "原生索引中的示例", state: "not-installed", data: { source: "source", version: "1.0.0", fetchedAt: 1, installTarget: { source: "source", name: "market-plugin", version: "1.0.0" } } }]
          : cmd.kind === "bundles" && cmd.query && JSON.parse(String(cmd.query)).versions ? [
            { id: "2.0.0", title: "2.0.0", state: "incompatible" },
            { id: "1.2.0-rc.1", title: "1.2.0-rc.1", state: "compatible", data: { prerelease: true, tags: ["next"] } },
            { id: "1.1.0", title: "1.1.0", state: "compatible", data: { tags: ["latest"] } },
          ] : cmd.kind === "bundles" ? [
            { id: "@deepseek-ai/dsh-base", title: "DSH Base", detail: "Core runtime", state: "enabled", data: { version: "0.2.2-rc.1", availability: "installation", readOnlyReason: "management-required" } },
            { id: "@deepseek-ai/dsh-session-titles", title: "Session titles", state: "disabled", controllable: true, data: { version: "0.2.2-rc.1", availability: "installation", optional: true } },
            { id: "@example/plugin", title: "Example", detail: "示例插件", state: "enabled", controllable: true, data: { version: "1.0.0", installed: true, removable: true, updatable: true } },
          ]
          : cmd.kind === "terminals" ? terminalClosed ? [] : [{ id: "pty", title: "zsh", detail: "/tmp", state: "running", data: { cols: 80, rows: 24 } }]
          : cmd.kind === "terminal_output" ? (cmd.before_seq ? [] : [{ id: "1", title: "screen", data: { type: "snapshot", screen: "DSH terminal ready\r\n\x1b[36m~/workspace\x1b[0m $ pwd\r\n/home/user/workspace\r\n\x1b[36m~/workspace\x1b[0m $ ", writable: true, info: {} } }])
          : cmd.kind === "conversation" ? [{ id: "message", title: "DSH", detail: `已经检查构建 ${cmd.query ?? ""}`, state: "message" }]
          : cmd.kind === "deliverables" ? [{ id: "file", title: "report.xlsx", path: "/tmp/dsh-test/report.xlsx", detail: "交付文件" }]
          : [{ id: "version", title: "DSH 版本", detail: "0.2.2-rc.1", state: "active" }];
        emit({ type: "dsh_read_result", sid: cmd.sid, request_id: cmd.cmd_id, kind: cmd.kind, items, available: true, has_more: false, ...(cmd.kind === "terminal_output" ? { next_seq: 1 } : {}) });
      }
      if (features && cmd.type === "act_dsh_subagent") emit({ type: "dsh_command_result", sid: cmd.sid,
        request_id: cmd.cmd_id, status: "success", text: cmd.action === "stop" ? "已请求停止子代理" : "已加入子代理队列" });
      if (features && cmd.type === "download_dsh") emit({ type: "dsh_download_chunk", sid: cmd.sid, request_id: cmd.cmd_id,
        export_id: cmd.export_id ?? cmd.cmd_id, offset: 0, total: cmd.cancel ? 0 : 4, data: cmd.cancel ? "" : "UEsDBA==", done: true });
      if (features && cmd.type === "get_file_preview") {
        if (String(cmd.path).endsWith("/dir")) emit({ type: "file_preview", sid: cmd.sid, request_id: cmd.request_id, path: cmd.path, directory: true, format: "text", content: "", size: 0 });
        else if (String(cmd.path).endsWith(".xlsx")) emit({ type: "file_preview", sid: cmd.sid, request_id: cmd.request_id, path: cmd.path,
          format: "spreadsheet", content: JSON.stringify({ sheets: [
            { name: "路线图", cells: [{ r: 1, c: 1, v: "本周进展" }, { r: 1, c: 2, v: "42", f: "SUM(B2:B3)" }], truncated: false },
            { name: "进展", cells: [{ r: 1, c: 1, v: "<script>bad()</script>" }], truncated: false }], truncated: false }),
          data: "UEsDBA==", size: 4, mtime_ns: "1", truncated: false });
        else emit({ type: "file_preview", sid: cmd.sid, request_id: cmd.request_id, path: cmd.path,
          format: "text", content: "first\nsecond\nthird\nfourth\nfifth\nsixth\nseventh", size: 42, mtime_ns: "1", truncated: false });
      }
      if (features && cmd.type === "browse_files") emit({ type: "files_listed", sid: cmd.sid, request_id: cmd.request_id,
        root: "/tmp/dsh-test", path: cmd.path, parent: "/tmp/dsh-test", kind: "directory", revision: "dir-1", next_offset: null,
        entries: [{ name: "README.md", path: "/tmp/dsh-test/dir/README.md", kind: "file" }] });
      if (cmd.type === "ping") emit({ type: "pong", n: cmd.n });
      if (cmd.cmd_id) emit({ type: "command_ack", client_id: cmd.client_id, cmd_id: cmd.cmd_id });
    });
  });
  return { commands, emit: (event: Record<string, unknown>) => emit(event),
    setHistory: (turns: Record<string, unknown>[], pagination = { has_more: false } as typeof historyPagination) => {
      historyTurns = turns;
      historyPagination = pagination;
    } };
}

async function openDsh(page: Page, options = {}) {
  const relay = await mockDshRelay(page, options);
  await page.goto("/");
  await expect(page.locator(".engine-label")).toHaveText("DSH");
  await expect(page.locator(".composer textarea")).toBeVisible();
  await expect(page.getByText("验证图片、引导与原生控件", { exact: true })).toBeVisible();
  return relay;
}

async function chooseEngine(page: Page, engine: string) {
  await page.locator(".engine-toggle").click();
  await page.getByRole("menuitemradio", { name: engine === "dsh" ? "DSH" : engine === "codex" ? "Codex" : "Claude", exact: true }).click();
  await expect(page.getByRole("menu", { name: "会话引擎" })).toHaveCount(0);
}

test("Deferred login form opens and enters the existing session without starting a task", async ({ page }) => {
  const relay = await mockDshRelay(page);
  await page.route("**/api/session", route => route.fulfill({ status: 401 }));
  await page.route("**/api/auth-config", route => route.fulfill({ json: { multi_user: false } }));
  await page.route("**/api/login", route => route.fulfill({ json: {} }));
  await page.goto("/");
  await page.getByPlaceholder("访问密码").fill("test-password");
  await page.getByRole("button", { name: "进入", exact: true }).click();
  await expect(page.locator(".engine-label")).toHaveText("DSH");
  await expect(page.getByText("验证图片、引导与原生控件", { exact: true })).toBeVisible();
  expect(relay.commands.some(command => command.type === "query" || command.type === "new_session")).toBe(false);
});

for (const theme of ["light", "dark"] as const) {
test(`DSH sidebar uses session cards and directory subtitles (${theme})`, async ({ page }, info) => {
  await page.addInitScript(theme => {
    localStorage.setItem("cc_remote_appearance_v2", JSON.stringify({ dsh: { mode: theme, sessionLayout: "comfortable" } }));
  }, theme);
  await openDsh(page, { running: true, sidebarCards: true });
  await expect(page.locator(".c-head .sub")).toHaveText("session native-s");
  if (!await page.locator(".shell.sidebar-open").count()) await page.locator(".surface-head-title").click();
  const cards = page.locator(".sessions .scard");
  await expect(cards).toHaveCount(2);
  await expect(cards.locator(".scard-location")).toHaveText(["dsh-test", "dsh-test"]);
  await expect(cards.locator(".scard-profile-dot")).toHaveCount(0);
  const sizes = await cards.evaluateAll(nodes => nodes.map(node => {
    const style = getComputedStyle(node);
    return { height: node.getBoundingClientRect().height, border: style.borderTopColor,
      background: style.backgroundColor, overflow: node.scrollWidth - node.clientWidth };
  }));
  // The running badge can add a pixel of font leading across browser engines.
  expect(Math.abs(sizes[0].height - sizes[1].height)).toBeLessThanOrEqual(2);
  for (const card of sizes) {
    expect(card.border).not.toBe("rgba(0, 0, 0, 0)");
    expect(card.background).not.toBe("rgba(0, 0, 0, 0)");
    expect(card.overflow).toBeLessThanOrEqual(1);
  }
  await page.screenshot({ path: info.outputPath(`dsh-sidebar-${theme}.png`), animations: "disabled" });
});
}

test("DSH reconnect feedback stays out of the composer and brief resyncs stay quiet", async ({ page }) => {
  const relay = await openDsh(page, { running: true });
  await page.clock.install();
  const error = "DSH 实时显示暂时积压，正在重新同步；任务仍在原生端执行。";
  relay.emit({ ...nativeState, connected: false, error });
  await expect(page.locator(".hstat-label")).toHaveText("同步中");
  await expect(page.locator(".composer")).not.toContainText(error);
  await expect(page.getByText(error, { exact: true })).toHaveCount(0);
  await page.clock.runFor(1_000);
  relay.emit({ ...nativeState, connected: true, error: null });
  await expect(page.locator(".hstat-label")).toHaveText("running");
  await page.clock.runFor(8_000);
  await expect(page.getByText(error, { exact: true })).toHaveCount(0);
  relay.emit({ ...nativeState, connected: false, error });
  await expect(page.locator(".hstat-label")).toHaveText("同步中");
  await page.clock.runFor(8_100);
  await expect(page.locator(".pane > .notice-bar")).toContainText(error);
  await expect(page.locator(".composer")).not.toContainText(error);
  relay.emit({ ...nativeState, connected: true, error: null });
  await expect(page.locator(".pane > .notice-bar")).toHaveCount(0);
  expect(relay.commands.some(command => command.type === "query" || command.type === "interrupt")).toBe(false);
});

test("DSH canonical first-turn history clears a stale older-history button", async ({ page }) => {
  const relay = await openDsh(page);
  const row = { id: "native-prompt", clientMsgId: "native-prompt", prompt: "验证图片、引导与原生控件", done: true,
    blocks: [{ kind: "text", message_id: "answer", text: "DSH 已准备好。", channel: "final", done: true }] };
  const history = { type: "history", sid, session_id: sid, revision: "dsh-history", generation: "dsh-generation",
    detail: "summary", events: [], turns: [row], newest_id: "native-prompt", oldest_id: "dsh-seq-11",
    in_progress: false, live_seq: 0 };
  relay.emit({ ...history, build_seq: 1, has_more: true });
  await expect(page.getByTestId("load-older-history")).toBeVisible();
  relay.emit({ ...history, build_seq: 2, has_more: false });
  await expect(page.getByTestId("load-older-history")).toHaveCount(0);
  await expect(page.getByText("正在恢复历史…", { exact: true })).toHaveCount(0);
  await expect(page.getByText("该轮未正常结束", { exact: true })).toHaveCount(0);
  await expect(page.locator(".assistant-answer-segment").getByText("DSH 已准备好。", { exact: true })).toBeVisible();
  expect(relay.commands.some(command => command.type === "get_history" && command.before)).toBe(false);
});

test("DSH long running narrative stays in the process timeline across history recovery", async ({ page }) => {
  const relay = await openDsh(page, { running: true });
  let sequence = 0;
  const emit = (event: Record<string, unknown>) => relay.emit({ sid, seq: ++sequence, ...event });
  emit({ type: "turn_binding", msg_id: "native-prompt", turn_id: "dsh-seq-5" });
  const process: Record<string, unknown>[] = [];
  for (let step = 0; step < 64; step++) {
    for (const [channel, text] of [["thinking", `Reasoning ${step}`], ["commentary", `Checking item ${step}`]]) {
      const message = `step-${step}-${channel}`;
      emit({ type: "assistant_msg_start", turn_id: "dsh-seq-5", message_id: message, channel });
      emit({ type: "delta", turn_id: "dsh-seq-5", message_id: message, channel, text, replace: true });
      emit({ type: "assistant_msg_end", turn_id: "dsh-seq-5", message_id: message, channel });
      process.push({ kind: "text", message_id: message, channel, text, done: true });
    }
  }
  const header = page.locator(".turn-process-head").last();
  await expect(header).toBeVisible();
  if (await header.getAttribute("aria-expanded") !== "true") await header.click();
  await expect(page.locator(".process-commentary").getByText("Checking item 63", { exact: true })).toBeVisible();
  await expect(page.locator(".assistant-answer-segment")).not.toContainText("Checking item");
  const row = { id: "native-prompt", clientMsgId: "native-prompt", prompt: "验证图片、引导与原生控件", done: false,
    blocks: process.slice(-24), processDetailState: "present", detailReasons: ["process"], detailEventCount: 128, detailLoaded: false };
  relay.setHistory([row]);
  emit({ type: "history", session_id: sid, revision: "dsh-history", generation: "dsh-generation",
    build_seq: 1, live_seq: sequence, authoritative: true, detail: "summary", events: [], turns: [row],
    newest_id: "native-prompt", in_progress: true, has_more: false });
  await expect(page.getByText("该轮未正常结束", { exact: true })).toHaveCount(0);
  await expect(page.locator(".process-commentary").getByText("Checking item 63", { exact: true })).toBeVisible();
  await expect(page.locator(".process-reasoning")).not.toHaveCount(0);
  await expect(page.getByText("加载更早的历史", { exact: true })).toHaveCount(0);
  emit({ type: "assistant_msg_start", turn_id: "dsh-seq-5", message_id: "final-step", channel: "commentary" });
  emit({ type: "delta", turn_id: "dsh-seq-5", message_id: "final-step", channel: "commentary", text: "Inspection complete" });
  emit({ type: "assistant_msg_start", turn_id: "dsh-seq-5", message_id: "final-step", channel: "final" });
  emit({ type: "delta", turn_id: "dsh-seq-5", message_id: "final-step", channel: "final", text: "Inspection complete", replace: true });
  emit({ type: "assistant_msg_end", turn_id: "dsh-seq-5", message_id: "final-step", channel: "final" });
  await expect(page.locator(".assistant-answer-segment").getByText("Inspection complete", { exact: true })).toBeVisible();
  await expect(page.locator(".process-commentary").getByText("Inspection complete", { exact: true })).toHaveCount(0);
  emit({ type: "turn_end", turn_id: "dsh-seq-5", result: { subtype: "success", duration_ms: 64000, is_error: false } });
  emit({ type: "state", state: "idle" });
  await expect(header).toContainText("已处理");
  await expect(page.getByText("该轮未正常结束", { exact: true })).toHaveCount(0);
});

for (const running of [false, true]) {
  test(`DSH path-prefixed prompts send verbatim as ${running ? "steering" : "a new message"}`, async ({ page }) => {
    const relay = await openDsh(page, { running });
    const input = page.locator(".composer textarea");
    const prompt = "/Users/Tester/workspace/unitree-go2这里有双目的，看看我们的大脑是否能接入？";
    await input.fill(prompt);
    if (running) await input.press("Enter");
    else await page.locator(".composer .sendbtn").click();
    await expect.poll(() => relay.commands.filter(command =>
      command.type === (running ? "steer" : "query") && command.prompt === prompt,
    ).length).toBe(1);
    await expect(input).toHaveValue("");
    await expect(page.locator(".composer-notice")).toHaveCount(0);
    expect(relay.commands.some(command => command.type === "set_dsh_control")).toBe(false);
  });
}

test("DSH context reports retain their last reading during temporary failures", async ({ page }, info) => {
  const relay = await openDsh(page, { running: true });
  const ring = page.getByRole("button", { name: "上下文占用", exact: true });
  await ring.click();
  const popover = page.getByRole("dialog", { name: "上下文占用", exact: true });
  await expect(popover).toContainText("24,000 / 64,000 (38%)");
  await expect(popover.locator(".ctx-pop-bar i")).toHaveAttribute("style", "width: 37.5%;");
  await expect(popover.getByText("设置上下文上限", { exact: true })).toHaveCount(0);
  await expect(popover).not.toContainText("Codex");
  relay.emit({ type: "context_report", sid, source: "recent_turn", available: true,
    total_tokens: 6400, max_tokens: 64000, percentage: 10, categories: [] });
  await expect(popover).toContainText("6,400 / 64,000 (10%)");
  await expect(popover.locator(".ctx-pop-bar i")).toHaveAttribute("style", "width: 10%;");
  await page.screenshot({ path: info.outputPath("dsh-context.png"), animations: "disabled" });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  relay.emit({ type: "context_report", sid, source: "recent_turn", available: false,
    total_tokens: 0, max_tokens: 0, percentage: 0, categories: [] });
  await expect(popover).toContainText("6,400 / 64,000 (10%)");
  await expect(popover.locator(".ctx-pop-status")).toHaveCount(0);
  await expect(ring.locator(".hr-fill")).toHaveCount(1);
  await expect(ring.locator("text")).toHaveCount(0);
  relay.emit({ type: "model", sid, model: "different-model" });
  await expect(popover).not.toContainText("6,400");
  await expect(popover.locator(".ctx-pop-nums")).toHaveText("—");
  await expect(ring.locator(".hr-fill")).toHaveCount(0);
  await expect(ring.locator("text")).toHaveCount(0);
  expect(relay.commands.some(c => c.type === "query" || c.type === "steer")).toBe(false);
});

test("DSH native context shows small readings and updates after compaction", async ({ page }) => {
  const relay = await openDsh(page);
  await page.getByRole("button", { name: "上下文占用", exact: true }).click();
  const popover = page.getByRole("dialog", { name: "上下文占用", exact: true });
  relay.emit({ type: "context_report", sid, source: "native_estimate", available: true,
    total_tokens: 593, max_tokens: 1000000, percentage: .06, categories: [] });
  await expect(popover).toContainText("593 / 1,000,000 (<1%)");
  await expect(popover.locator(".ctx-pop-status")).toHaveCount(0);
  relay.emit({ type: "context_report", sid, source: "native_estimate", available: true,
    total_tokens: 100, max_tokens: 1000000, percentage: .01, categories: [] });
  await expect(popover).toContainText("100 / 1,000,000 (<1%)");
  await expect(popover.locator(".ctx-pop-bar i")).toHaveAttribute("style", "width: 0.01%;");
  expect(relay.commands.some(c => ["query", "steer", "compact_session"].includes(String(c.type)))).toBe(false);
});

test("DSH harness switch fits its label and Work is visibly locked", async ({ page }, info) => {
  await openDsh(page);
  const selector = page.locator(".engine-selector");
  expect((await selector.boundingBox())!.width).toBeLessThan(90);
  await page.locator(".engine-toggle").click();
  const menu = page.getByRole("menu", { name: "会话引擎" });
  await expect(menu).toBeVisible();
  await expect(menu.getByRole("menuitemradio")).toHaveCount(3);
  await expect(menu.getByRole("menuitemradio", { name: "DSH", exact: true }).locator(".engine-brand-icon.dsh")).toHaveCount(1);
  await expect(menu.getByRole("menuitemradio", { name: "DSH", exact: true })).toHaveAttribute("aria-checked", "true");
  for (const theme of ["light", "dark"]) {
    await page.evaluate(value => document.documentElement.setAttribute("data-theme", value), theme);
    await page.screenshot({ path: info.outputPath(`harness-menu-${theme}.png`), animations: "disabled" });
    expect((await menu.boundingBox())!.x).toBeGreaterThanOrEqual(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  }
  await page.locator(".surface-head-title").click();
  const work = page.getByRole("tab", { name: "Work", exact: true });
  await expect(work).toBeDisabled();
  await expect(work.locator(".space-lock")).toBeVisible();
  await expect(work).toHaveAttribute("title", "DSH 暂不支持 Work");
  await page.locator(".s-head").getByRole("button", { name: "收起", exact: true }).click();
  await expect(menu).toHaveCount(0);
  await chooseEngine(page, "claude");
  await expect(page.locator(".engine-label")).toHaveText("Claude");
  expect((await selector.boundingBox())!.width).toBeLessThan(100);
  await chooseEngine(page, "dsh");
  await expect(page.locator(".engine-label")).toHaveText("DSH");
  await page.locator(".surface-head-title").click();
  await expect(page.getByRole("tab", { name: "Code", exact: true })).toHaveAttribute("aria-selected", "true");
});

test("DSH Skills reads carry the selected session and show completion without a focus error", async ({ page }) => {
  const relay = await openDsh(page);
  await expect.poll(() => relay.commands.filter(cmd => cmd.type === "get_engine_capabilities").length).toBeGreaterThan(0);
  expect(relay.commands.filter(cmd => cmd.type === "get_engine_capabilities").every(cmd => cmd.sid === sid)).toBe(true);
  const composer = page.locator(".composer textarea");
  await composer.fill("$rev");
  await expect(page.getByText("审查变更", { exact: true })).toBeVisible();
  await expect(page.getByText("DSH 会话尚未选定或已失效，请重新选择会话。", { exact: true })).toHaveCount(0);
  expect(relay.commands.some(cmd => cmd.type === "get_status")).toBe(false);
});

test("DSH harness selection clears pointer focus but retains the keyboard focus indicator", async ({ page }) => {
  await openDsh(page);
  const select = page.locator(".engine-toggle");
  await chooseEngine(page, "codex");
  await expect(select).not.toBeFocused();
  await expect(select).toHaveCSS("outline-style", "none");
  await select.focus();
  await select.press("ArrowDown");
  const selected = page.getByRole("menuitemradio", { name: "Codex", exact: true });
  await expect(selected).toBeFocused();
  await selected.press("ArrowDown");
  await expect(page.getByRole("menuitemradio", { name: "DSH", exact: true })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(select).toBeFocused();
  await expect(select).toHaveCSS("outline-style", "solid");
  await expect(page.locator(".engine-label")).toHaveText("DSH");
  await select.press("ArrowUp");
  await page.keyboard.press("Home");
  await expect(page.getByRole("menuitemradio", { name: "Claude", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(select).toBeFocused();
  await expect(page.getByRole("menu", { name: "会话引擎" })).toHaveCount(0);
});

test("DSH completion uses the official engine mark", async ({ page }) => {
  await openDsh(page);
  await expect(page.locator(".turn-done-mark .engine-brand-icon.dsh")).toBeVisible();
  await expect(page.locator(".turn-working")).toHaveCount(0);
});

test("DSH working spark animates while a native turn runs", async ({ page }) => {
  await page.clock.install();
  const relay = await openDsh(page, { running: true });
  relay.emit({ type: "turn_binding", sid, msg_id: "native-prompt", turn_id: "dsh-seq-5" });
  const working = page.locator(".turn-working svg");
  await expect(working).toBeVisible();
  await expect(page.locator(".turn-done-mark")).toHaveCount(0);
  const initial = await working.innerHTML();
  await page.clock.runFor(180);
  await expect.poll(() => working.innerHTML()).not.toBe(initial);
});

test("DSH child-result continuation stays connected across completion, refresh and a new question", async ({ page }, info) => {
  const relay = await openDsh(page);
  const auto = "dsh-turn-2";
  relay.emit({ type: "state", sid, state: "running" });
  relay.emit({ type: "turn_binding", sid, msg_id: auto, turn_id: auto,
    autonomous: true, continuation: "subagent" });
  relay.emit({ type: "assistant_msg_start", sid, turn_id: auto, message_id: "child-check", channel: "final" });
  relay.emit({ type: "delta", sid, turn_id: auto, message_id: "child-check", text: "子代理结果已核对，继续补充结论。" });
  const continuation = page.locator(`[data-turn-id="${auto}"]`);
  const cue = continuation.locator(".turn-continuation");
  await expect(cue).toContainText("DeepSeek 继续处理");
  await expect(cue).toHaveClass(/background-followup-boundary/);
  expect(await cue.evaluate(node => {
    const style = getComputedStyle(node);
    return { background: style.backgroundColor, radius: style.borderRadius, borderTop: style.borderTopWidth };
  })).toEqual({ background: "rgba(0, 0, 0, 0)", radius: "0px", borderTop: "1px" });
  await expect(continuation.locator(".turn-working")).toBeVisible();
  await expect(page.getByText("DSH 已准备好。", { exact: true })).toBeVisible();
  await expect(page.locator('[data-turn-id="native-prompt"] .turn-working')).toHaveCount(0);
  await expect(continuation.locator(".ubub")).toHaveCount(0);
  await page.screenshot({ path: info.outputPath("dsh-child-continuation.png") });
  relay.emit({ type: "assistant_msg_end", sid, turn_id: auto, message_id: "child-check" });
  relay.emit({ type: "turn_end", sid, turn_id: auto,
    result: { subtype: "success", duration_ms: 1200, is_error: false } });
  relay.emit({ type: "state", sid, state: "idle" });
  await expect(continuation.locator(".turn-working")).toHaveCount(0);
  await expect(cue).toBeVisible();
  relay.setHistory([
    { id: "native-prompt", clientMsgId: "native-prompt", prompt: "验证图片、引导与原生控件",
      done: true, forkPointId: "dsh-seq-5", blocks: [
        { kind: "text", message_id: "answer", text: "DSH 已准备好。", channel: "final", done: true },
      ] },
    { id: auto, prompt: "", done: true, forkPointId: auto, continuation: "subagent", blocks: [
      { kind: "text", message_id: "child-check", text: "子代理结果已核对，继续补充结论。", channel: "final", done: true },
    ] },
  ]);
  await page.reload();
  await expect(cue).toBeVisible();
  await expect(page.locator(".turn-continuation")).toHaveCount(1);
  await expect(page.getByText("DSH 已准备好。", { exact: true })).toBeVisible();
  await expect(continuation).toContainText("子代理结果已核对，继续补充结论。");
  relay.emit({ type: "user_msg", sid, msg_id: "next-task", client_msg_id: "next-task", prompt: "接下来检查另一个目录" });
  relay.emit({ type: "turn_binding", sid, msg_id: "next-task", turn_id: "dsh-seq-30" });
  await expect(page.locator('[data-turn-id="next-task"] .ubub')).toHaveText("接下来检查另一个目录");
  await expect(page.locator('[data-turn-id="next-task"] .turn-continuation')).toHaveCount(0);
  await expect(page.locator(".turn-continuation")).toHaveCount(1);
});

test("DSH scheduled prompts retain their task tag across reload", async ({ page }, info) => {
  const relay = await openDsh(page);
  const receipt = { task_id: "schedule-native", title: "自动化任务", scheduled_at: 1794130061.465 };
  relay.emit({ type: "state", sid, state: "running" });
  relay.emit({ type: "user_msg", sid, msg_id: "scheduled-input", client_msg_id: "scheduled-input", prompt: "检查构建结果", timed_task: receipt });
  relay.emit({ type: "turn_binding", sid, msg_id: "scheduled-input", turn_id: "dsh-seq-9" });
  relay.emit({ type: "assistant_msg_start", sid, message_id: "scheduled-answer", turn_id: "dsh-seq-9", channel: "final" });
  relay.emit({ type: "delta", sid, message_id: "scheduled-answer", turn_id: "dsh-seq-9", channel: "final", text: "构建通过。" });
  const row = page.locator('[data-turn-id="scheduled-input"]');
  await expect(row.locator(".ubub")).toContainText("检查构建结果");
  await expect(row.getByText("定时任务", { exact: true })).toBeVisible();
  relay.setHistory([{ id: "scheduled-input", prompt: "检查构建结果", timedTask: receipt, done: true,
    blocks: [{ kind: "text", message_id: "scheduled-answer", channel: "final", text: "构建通过。", done: true }] }]);
  relay.emit({ type: "turn_end", sid, turn_id: "dsh-seq-9", result: { subtype: "success", duration_ms: 1200, is_error: false } });
  relay.emit({ type: "state", sid, state: "idle" });
  await page.reload();
  await expect(row.locator(".ubub")).toContainText("检查构建结果");
  await row.getByText("定时任务", { exact: true }).click();
  await expect(row.getByText("自动化任务", { exact: true })).toBeVisible();
  await expect(row).toContainText("计划发送于");
  await page.screenshot({ path: info.outputPath("dsh-scheduled-prompt.png") });
});

test("DSH background-job continuation retains attribution after completion and reload", async ({ page }, info) => {
  const relay = await openDsh(page);
  const auto = "dsh-turn-job-return";
  const receipt = { item_id: "dsh-return-70", kind: "task", phase: "end", status: "unknown",
    server: "cc_remote", tool: "task_return", title: "后台任务返回", background: true, turn_id: auto };
  relay.emit({ type: "state", sid, state: "running" });
  relay.emit({ type: "turn_binding", sid, msg_id: auto, turn_id: auto, autonomous: true });
  relay.emit({ type: "process", sid, ...receipt });
  relay.emit({ type: "assistant_msg_start", sid, message_id: "job-answer", turn_id: auto, channel: "final" });
  relay.emit({ type: "delta", sid, message_id: "job-answer", turn_id: auto, channel: "final", text: "后台构建已经完成。" });
  const row = page.locator(`[data-turn-id="${auto}"]`);
  await expect(row.locator(".turn-continuation")).toContainText("DeepSeek 继续处理");
  await expect(row.locator(".ubub")).toHaveCount(0);
  await expect(row.getByRole("button", { name: /代理返回/ })).toHaveCount(0);
  relay.emit({ type: "turn_end", sid, turn_id: auto, result: { subtype: "success", duration_ms: 1300, is_error: false } });
  relay.emit({ type: "state", sid, state: "idle" });
  relay.setHistory([{ id: auto, prompt: "", done: true, ts: 1794130061465, blocks: [
    { ...receipt, kind: "process", processKind: "task", done: true },
    { kind: "text", message_id: "job-answer", channel: "final", text: "后台构建已经完成。", done: true },
  ] }]);
  await page.reload();
  await expect(row.locator(".turn-continuation")).toContainText("DeepSeek 继续处理");
  await expect(row).toContainText("后台构建已经完成。");
  await page.screenshot({ path: info.outputPath("dsh-job-continuation.png") });
  relay.emit({ type: "user_msg", sid, msg_id: "next-human", prompt: "接下来检查测试结果" });
  relay.emit({ type: "turn_binding", sid, msg_id: "next-human", turn_id: "dsh-seq-next" });
  relay.emit({ type: "process", sid, ...receipt, item_id: "dsh-return-80", turn_id: "dsh-seq-next" });
  await expect(page.locator('[data-turn-id="next-human"] .turn-continuation')).toHaveCount(0);
  await expect(page.locator(".turn-continuation")).toHaveCount(1);
});

test("DSH model picker reflects the native catalog and preserves native reasoning levels", async ({ page }) => {
  await openDsh(page);
  await page.getByTitle("选择模型", { exact: true }).click();
  const models = page.getByRole("dialog", { name: "选择模型", exact: true });
  await expect(models.locator(".cmd-nm")).toHaveText(["DeepSeek-V41-Flash"]);
  await models.locator(".cmd").click();
  await page.getByTitle("思考强度", { exact: true }).click();
  await expect(page.getByRole("dialog", { name: "选择思考强度", exact: true }).locator(".cmd")).toHaveCount(2);
});

test("DSH native controls remain usable while running and preserve command drafts", async ({ page }) => {
  const relay = await openDsh(page, { running: true });
  await page.locator(".hint-mode").click();
  await page.getByText("工作区写入", { exact: true }).click();
  await expect.poll(() => relay.commands.some(c => c.type === "set_dsh_control" && c.kind === "permission" && c.value === "workspace-write")).toBe(true);
  const input = page.locator(".composer textarea");
  await input.press("Shift+Tab");
  await expect.poll(() => relay.commands.filter(c => c.type === "set_dsh_control" && c.kind === "permission").length).toBe(2);
  expect(relay.commands.filter(c => c.type === "set_dsh_control" && c.kind === "permission")
    .every(c => c.value === "workspace-write")).toBe(true);
  await input.fill("/goal 完成修复");
  await input.press("Enter");
  await expect(page.getByText("当前目标不能替换，请使用 /goal edit", { exact: true })).toBeVisible();
  await expect(input).toHaveValue("/goal 完成修复");
  expect(relay.commands.some(c => c.type === "get_goal" || c.type === "set_perm")).toBe(false);
  await input.fill("继续检查布局");
  await input.press("Enter");
  await expect.poll(() => relay.commands.some(c => c.type === "steer" && c.prompt === "继续检查布局")).toBe(true);
});

for (const theme of ["light", "dark"] as const) {
test(`DSH native progress distinguishes inactive continuation and fits the viewport (${theme})`, async ({ page }, info) => {
  if (info.project.name === "chromium") await page.setViewportSize({ width: 1440, height: 900 });
  await page.addInitScript(theme => localStorage.setItem("cc_remote_theme", theme), theme);
  const relay = await openDsh(page, { commandSuccess: true });
  await page.getByRole("button", { name: /查看 DSH Goal/ }).click();
  const goal = page.getByRole("dialog", { name: "DSH Goal" });
  await expect(goal).toContainText("等待继续");
  await expect(goal).toContainText("3 / 32");
  await goal.getByRole("button", { name: "继续目标", exact: true }).click();
  await expect.poll(() => relay.commands.some(c => c.type === "act_dsh_goal" && c.action === "resume")).toBe(true);
  await page.screenshot({ path: info.outputPath("dsh-controls.png") });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await page.clock.install();
  relay.emit({ ...nativeState, connected: false, error: "DSH 连接中断，正在重新连接。" });
  await expect(page.locator(".hstat-label")).toHaveText("同步中");
  await expect(goal.getByRole("button", { name: "继续目标", exact: true })).toBeDisabled();
  await goal.getByRole("button", { name: "关闭" }).click();
  await page.clock.runFor(8_100);
  await expect(page.locator(".pane > .notice-bar")).toContainText("DSH 连接中断，正在重新连接。");
});
}

test("DSH command attachments wait for acceptance before clearing", async ({ page }) => {
  const relay = await openDsh(page, { commandSuccess: true });
  const input = page.locator(".composer textarea");
  await page.locator('.composer').getByLabel("添加照片", { exact: true }).setInputFiles({
    name: "portrait.png", mimeType: "image/png", buffer: staticPng(90, 180),
  });
  await expect(page.locator(".composer .attach img")).toBeVisible();
  await input.fill("/goal 检查截图");
  await input.press("Enter");
  await expect.poll(() => relay.commands.some(c => c.type === "set_dsh_control" && c.kind === "command" && Array.isArray(c.images) && c.images.length === 1)).toBe(true);
  await expect(input).toHaveValue("");
  await expect(page.locator(".composer .attach")).toHaveCount(0);
});

test("DSH cold command admission and failed goal editing retain the user's text", async ({ page }) => {
  const relay = await openDsh(page);
  relay.emit({ ...nativeState, commands: [] });
  const input = page.locator(".composer textarea");
  await input.fill("/goal 冷会话目标");
  await input.press("Enter");
  await expect.poll(() => relay.commands.some(c => c.type === "set_dsh_control" && c.value === "/goal 冷会话目标")).toBe(true);
  await expect(input).toHaveValue("/goal 冷会话目标");
  // Explicit command admission establishes native follow, which publishes its
  // actual command catalog before the existing goal's controls become usable.
  relay.emit({ ...nativeState });
  await page.getByRole("button", { name: /查看 DSH Goal/ }).click();
  const goal = page.getByRole("dialog", { name: "DSH Goal" });
  await goal.getByRole("button", { name: "编辑目标", exact: true }).click();
  await goal.getByLabel("目标内容").fill("保存失败也保留这段文字");
  await goal.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(goal.getByRole("alert")).toContainText("目标已更新");
  await expect(goal.getByLabel("目标内容")).toHaveValue("保存失败也保留这段文字");
});

test("DSH preset picker uses the native roster and carries the selected preset", async ({ page }, info) => {
  const relay = await openDsh(page);
  await page.locator(".surface-head-title").click();
  await page.getByRole("button", { name: "新会话", exact: true }).click();
  const picker = page.getByRole("button", { name: "选择 DSH 模式", exact: true });
  await expect(picker).toContainText("标准模式");
  await picker.click();
  const dialog = page.getByRole("dialog", { name: "选择 DSH 模式", exact: true });
  await expect(dialog.getByRole("button", { pressed: true })).toContainText("标准模式");
  await expect(dialog.locator(".cmd")).toHaveCount(4);
  await expect(dialog).not.toContainText("DSH 默认");
  await expect(dialog.locator(".cmd-ds")).toHaveCount(4);
  for (const description of await dialog.locator(".cmd-ds").all()) {
    expect(await description.evaluate(node => node.scrollHeight <= node.clientHeight + 1)).toBe(true);
    await expect(description).toHaveCSS("white-space", "normal");
  }
  await page.screenshot({ path: info.outputPath("dsh-preset-picker.png"), animations: "disabled" });
  await dialog.getByRole("button", { name: /PTC 模式/ }).click();
  await expect(picker).toContainText("PTC 模式");
  await page.locator(".newchat textarea").fill("开始任务");
  await page.locator(".newchat").getByRole("button", { name: "开始", exact: true }).click();
  await expect.poll(() => relay.commands.some(c => c.type === "new_session" && c.engine === "dsh" && c.dsh_agent_preset === "ptc")).toBe(true);
  const command = relay.commands.find(c => c.type === "new_session")!;
  expect(command.claude_profile_id).toBeUndefined();
  expect(command.codex_profile_id).toBeUndefined();
});

test("DSH loading and harness switches only prefetch supported session surfaces", async ({ page }) => {
  const relay = await openDsh(page);
  await expect.poll(() => relay.commands.some(cmd => cmd.type === "get_context")).toBe(true);
  const dshLists = () => relay.commands.filter(cmd => cmd.type === "list_sessions" && cmd.engine === "dsh");
  expect(dshLists().length).toBeGreaterThan(0);
  expect(dshLists().map(cmd => cmd.space ?? "code")).not.toContain("work");
  await expect(page.locator(".banner")).toHaveCount(0);

  for (const engine of ["codex", "claude"]) {
    const switches = relay.commands.filter(cmd => cmd.type === "switch_session").length;
    await chooseEngine(page, engine);
    // Supported engines still warm Work; the DSH guard must not disable this.
    await expect.poll(() => relay.commands.some(cmd => cmd.type === "list_sessions"
      && (cmd.engine ?? "claude") === engine && cmd.space === "work")).toBe(true);
    await chooseEngine(page, "dsh");
    await expect.poll(() => relay.commands.filter(cmd => cmd.type === "switch_session").length).toBeGreaterThan(switches);
    await expect(page.getByText("验证图片、引导与原生控件", { exact: true })).toBeVisible();
    expect(dshLists().map(cmd => cmd.space ?? "code")).not.toContain("work");
    await expect(page.locator(".banner")).toHaveCount(0);
  }
});

async function openDshTools(page: Page) {
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  await page.getByRole("button", { name: "会话工具" }).click();
  await expect(page.getByLabel("DSH 会话工具", { exact: true })).toBeVisible();
}

async function selectDshTool(page: Page, label: string) {
  const panel = page.getByLabel("DSH 会话工具", { exact: true });
  await panel.locator('button[aria-haspopup="menu"]').filter({ hasText: "工具" }).click();
  await panel.getByRole("menuitem", { name: label, exact: true }).click();
}

test("DSH native directory links open the file browser and retain file previews", async ({ page }) => {
  const relay = await openDsh(page, { features: true });
  await page.getByRole("button", { name: "在 Remote 中打开 /tmp/dsh-test/dir", exact: true }).click();
  await expect(page.locator(".file-browser-shell")).toBeVisible();
  await expect(page.getByRole("button", { name: "README.md", exact: true })).toBeVisible();
  expect(relay.commands.find(c => c.type === "browse_files")?.path).toBe("/tmp/dsh-test/dir");
  await page.getByRole("button", { name: "README.md", exact: true }).click();
  await expect(page.locator(".artifact-panel")).toContainText("seventh");
  await expect(page.getByRole("button", { name: "返回目录", exact: true })).toBeVisible();
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("link", { name: "下载原文件", exact: true }).click();
  expect((await downloadEvent).suggestedFilename()).toBe("README.md");
});

test("DSH archived sessions remain readable and can be unarchived without delete controls", async ({ page }) => {
  const relay = await openDsh(page, { features: true });
  if (!await page.locator(".shell.sidebar-open").count()) await page.locator(".surface-head-title").click();
  await page.getByRole("button", { name: "更多操作", exact: true }).click();
  await page.getByRole("button", { name: "归档", exact: true }).click();
  await expect.poll(() => relay.commands.some(c => c.type === "archive_session" && c.session_id === sid && c.engine === "dsh")).toBe(true);
  await page.getByText("已归档", { exact: true }).click();
  await page.getByRole("button", { name: "更多操作", exact: true }).click();
  await expect(page.getByRole("button", { name: "取消归档", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "删除会话", exact: true })).toHaveCount(0);
  await expect(page.locator(".composer textarea")).toBeDisabled();
});

test("DSH archive refresh keeps the already visible history start without an older-page button", async ({ page }, info) => {
  const relay = await openDsh(page, { features: true });
  const turns = Array.from({ length: 7 }, (_, i) => ({ id: `seen-${i}`, prompt: `已经看过的问题 ${i + 1}`, done: true,
    blocks: [{ kind: "text", message_id: `answer-${i}`, text: `答复 ${i + 1}`, channel: "final", done: true }] }));
  const head = { type: "history", sid, session_id: sid, revision: "dsh-history", generation: "dsh-generation",
    detail: "summary", events: [], authoritative: true, in_progress: false, live_seq: 0 };
  relay.setHistory(turns);
  relay.emit({ ...head, build_seq: 1, turns, has_more: false, oldest_id: "seen-0" });
  await page.locator(".thread").evaluate(el => { el.scrollTop = 0; });
  await expect(page.getByText("已经看过的问题 1", { exact: true })).toBeVisible();
  await expect(page.getByText("答复 1", { exact: true })).toBeVisible();
  await expect(page.getByTestId("load-older-history")).toHaveCount(0);
  relay.setHistory(turns.slice(-4), { has_more: true, oldest_id: "seen-3" });
  if (!await page.locator(".shell.sidebar-open").count()) await page.locator(".surface-head-title").click();
  await page.getByRole("button", { name: "更多操作", exact: true }).click();
  await page.getByRole("button", { name: "归档", exact: true }).click();
  await expect(page.locator(".composer textarea")).toBeDisabled();
  relay.emit({ ...head, build_seq: 2, turns: turns.slice(-4), has_more: true, oldest_id: "seen-3" });
  const scrim = page.locator(".scrim-side.show");
  if (await scrim.isVisible()) await scrim.click({ position: { x: page.viewportSize()!.width - 8, y: 100 } });
  await page.locator(".thread").evaluate(el => { el.scrollTop = 0; });
  await expect(page.getByText("已经看过的问题 1", { exact: true })).toBeVisible();
  await expect(page.getByText("答复 1", { exact: true })).toBeVisible();
  await expect(page.getByTestId("load-older-history")).toHaveCount(0);
  await expect(page.locator(".shell")).not.toHaveAttribute("data-sidebar-motion", /.+/);
  await page.screenshot({ path: info.outputPath("dsh-archived-history-floor.png") });
});

test("DSH references insert the native quoted file and session syntax", async ({ page }) => {
  const relay = await openDsh(page, { features: true });
  const input = page.locator(".composer textarea");
  await input.fill("检查 @report");
  await expect(page.getByRole("option", { name: "文件 report notes.md" })).toBeVisible();
  await input.press("Enter");
  await expect(input).toHaveValue('检查 @"report notes.md" ');
  expect(relay.commands.some(c => c.type === "query")).toBe(false);
  await input.fill("关联 @session");
  await page.getByRole("option", { name: "会话 相关会话" }).click();
  await expect(input).toHaveValue("关联 @[相关会话](dsh-session:native-session) ");
  await input.press("Enter");
  await expect.poll(() => relay.commands.filter(c => c.type === "query" && c.prompt === "关联 @[相关会话](dsh-session:native-session)").length).toBe(1);
});

test("DSH Tab completes a file reference before queueing a running task", async ({ page }) => {
  const relay = await openDsh(page, { features: true, running: true });
  const input = page.locator(".composer textarea");
  await input.fill("检查 @report");
  await expect(page.getByRole("option", { name: "文件 report notes.md" })).toBeVisible();
  await input.press("Tab");
  await expect(input).toHaveValue('检查 @"report notes.md" ');
  expect(relay.commands.some(c => c.type === "query" || c.type === "steer")).toBe(false);
  await input.press("Tab");
  await expect.poll(() => relay.commands.filter(c => c.type === "query"
    && c.delivery === "queue" && c.prompt === '检查 @"report notes.md"').length).toBe(1);
});

test("DSH subagent controls, jobs, diagnostics and complete export share a compact panel", async ({ page }, info) => {
  const relay = await openDsh(page, { features: true });
  await openDshTools(page);
  const panel = page.getByLabel("DSH 会话工具", { exact: true });
  await panel.getByRole("button", { name: "检查构建", exact: true }).click();
  const agent = page.getByLabel("协作代理详情", { exact: true });
  await expect(agent).toContainText("已经检查构建");
  await agent.getByRole("textbox", { name: "子代理消息" }).fill("继续检查");
  await agent.getByRole("button", { name: "排队发送", exact: true }).click();
  await expect(agent).toContainText("已加入子代理队列");
  await agent.getByRole("button", { name: "关闭协作代理详情" }).click();
  await openDshTools(page);
  const act = relay.commands.find(c => c.type === "act_dsh_subagent");
  expect(act?.sid).toBe(sid); expect(act?.target_sid).toBe("dsh@child");
  await selectDshTool(page, "后台任务");
  await expect(panel).toContainText("构建任务");
  await expect(panel).toContainText("运行中");
  await selectDshTool(page, "连接诊断");
  await expect(panel).toContainText("0.2.2-rc.1");
  await panel.getByRole("button", { name: "导出完整会话", exact: true }).click();
  await expect(panel.getByRole("link", { name: "保存 ZIP" })).toBeVisible();
  const downloadEvent = page.waitForEvent("download");
  await panel.getByRole("link", { name: "保存 ZIP" }).click();
  expect((await downloadEvent).suggestedFilename()).toBe("dsh-session-native-session.zip");
  await page.screenshot({ path: info.outputPath("dsh-tools.png") });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
});

test("DSH produced XLSX files preview multiple sheets and preserve the original download", async ({ page }, info) => {
  await openDsh(page, { features: true });
  await openDshTools(page);
  const panel = page.getByLabel("DSH 会话工具", { exact: true });
  await selectDshTool(page, "产出文件");
  await panel.getByRole("button", { name: "report.xlsx", exact: true }).click();
  const sheet = page.getByRole("region", { name: "表格预览" });
  await expect(sheet).toContainText("本周进展");
  await expect(sheet.getByRole("cell", { name: "42", exact: true })).toHaveAttribute("title", "=SUM(B2:B3)");
  await sheet.getByRole("tab", { name: "进展", exact: true }).click();
  await expect(sheet).toContainText("<script>bad()</script>");
  expect(await sheet.locator("script").count()).toBe(0);
  const downloadEvent = page.waitForEvent("download");
  await sheet.getByRole("link", { name: "下载原文件" }).click();
  expect((await downloadEvent).suggestedFilename()).toBe("report.xlsx");
  await page.screenshot({ path: info.outputPath("dsh-spreadsheet.png") });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
});

test("DSH native present exposes generated files beneath the answer", async ({ page }) => {
  await openDsh(page, { features: true });
  await page.locator('.dsh-produced').getByRole("button", { name: "report.xlsx", exact: true }).click();
  await expect(page.getByRole("region", { name: "表格预览" })).toContainText("本周进展");
});

test("DSH full text search opens matching context without sending a prompt", async ({ page }) => {
  const relay = await openDsh(page, { features: true });
  if (!await page.locator(".shell.sidebar-open").count()) await page.locator(".surface-head-title").click();
  await page.getByRole("textbox", { name: "搜索会话", exact: true }).fill("needle");
  const results = page.getByRole("region", { name: "全文搜索结果" });
  await expect(results).toContainText("匹配 needle");
  await results.getByRole("button", { name: /DSH 原生会话/ }).click();
  await expect(page.getByLabel("DSH 会话工具", { exact: true })).toContainText("已经检查构建 needle");
  expect(relay.commands.some(c => c.type === "query" || c.type === "steer")).toBe(false);
});

test("DSH plan command uses native syntax and shows pending and settled states without a toolbar button", async ({ page }) => {
  const relay = await openDsh(page, { features: true, commandSuccess: true });
  await expect(page.getByRole("button", { name: "计划", exact: true })).toHaveCount(0);
  const input = page.locator(".composer textarea");
  await input.fill("/pla");
  await page.getByRole("listbox", { name: "命令", exact: true }).getByRole("button", { name: /\/plan/ }).click();
  await input.press("Enter");
  await expect.poll(() => relay.commands.some(c => c.type === "set_dsh_control" && c.value === "/plan")).toBe(true);
  expect(relay.commands.some(c => c.type === "query" || c.type === "steer" || c.value === "/plan on")).toBe(false);
  await expect(page.getByRole("status", { name: "DSH 计划状态" })).toHaveCount(0);
  relay.emit({ ...nativeState, plan_active: false, plan_pending: true });
  await expect(page.getByRole("status", { name: "DSH 计划状态" })).toHaveText("进入计划待生效");
  relay.emit({ ...nativeState, plan_active: true, plan_pending: false });
  await expect(page.getByRole("status", { name: "DSH 计划状态" })).toHaveText("计划模式");
  await input.fill("/plan off");
  await input.press("Enter");
  await expect.poll(() => relay.commands.some(c => c.type === "set_dsh_control" && c.value === "/plan off")).toBe(true);
  relay.emit({ ...nativeState, plan_active: false, plan_pending: false });
  await expect(page.getByRole("status", { name: "DSH 计划状态" })).toHaveCount(0);
});

test("DSH cold goal and plan discovery refreshes the open menu and explains minimal capabilities", async ({ page }) => {
  const relay = await openDsh(page);
  const input = page.locator(".composer textarea");
  relay.emit({ ...nativeState, commands: [], goal: null });
  await input.fill("/goa");
  const menu = page.getByRole("listbox", { name: "命令", exact: true });
  await expect(menu.getByRole("button", { name: /\/goal/ })).toBeDisabled();
  relay.emit({ ...nativeState, goal: null });
  await expect(menu.getByRole("button", { name: /\/goal/ })).toBeEnabled();
  await menu.getByRole("button", { name: /\/goal/ }).click();
  const dialog = page.getByRole("dialog", { name: "DSH Goal" });
  await expect(dialog).toBeVisible();
  expect(relay.commands.some(c => c.type === "act_dsh_goal" || (c.type === "set_dsh_control" && c.value === "/goal"))).toBe(false);
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  relay.emit({ ...nativeState, agent_preset: "minimal", commands: nativeState.commands.filter(c => c.name === "permission"), goal: null });
  for (const prefix of ["/goa", "/pla"]) {
    await input.fill(prefix);
    await expect(menu.getByRole("button")).toBeDisabled();
    await expect(menu).toContainText("minimal 未启用");
  }
  expect(relay.commands.some(c => c.type === "query" || c.type === "steer")).toBe(false);
});

test("DSH command errors are dismissible above the input and preserve the draft", async ({ page }, info) => {
  const relay = await openDsh(page);
  const input = page.locator(".composer textarea");
  await input.fill("/goal 保留我的目标");
  await input.press("Enter");
  const notice = page.locator(".composer-in .dsh-command-result");
  await expect(notice).toContainText("当前目标不能替换");
  await expect(page.locator("details.dsh-command-result")).toHaveCount(0);
  await page.screenshot({ path: info.outputPath("dsh-command-error.png") });
  await notice.getByRole("button", { name: "关闭命令提示" }).click();
  await expect(notice).toHaveCount(0);
  await expect(input).toHaveValue("/goal 保留我的目标");
  relay.emit({ ...nativeState, plan_active: false });
  await expect(notice).toHaveCount(0);
});

test("DSH Goal form creates native round cap, pauses, edits and resumes", async ({ page }, info) => {
  const relay = await openDsh(page, { commandSuccess: true });
  relay.emit({ ...nativeState, goal: null });
  const input = page.locator(".composer textarea");
  await input.fill("/goal");
  await input.press("Enter");
  const dialog = page.getByRole("dialog", { name: "DSH Goal" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: /轮次上限 · 256/ })).toBeVisible();
  expect(relay.commands.some(c => c.type === "act_dsh_goal")).toBe(false);
  await dialog.getByLabel("目标内容").fill("完成 DSH 适配并验证手机界面");
  await dialog.getByRole("button", { name: /轮次上限/ }).click();
  await dialog.getByRole("button", { name: "64", exact: true }).click();
  await page.screenshot({ path: info.outputPath("dsh-create.png") });
  await dialog.getByRole("button", { name: "开始目标", exact: true }).click();
  await expect(dialog.getByRole("progressbar", { name: "轮次用量" })).toHaveAttribute("max", "64");
  const created = relay.commands.find(c => c.type === "act_dsh_goal")!;
  expect(created).toMatchObject({ sid, action: "create", max_rounds: 64, objective: "完成 DSH 适配并验证手机界面" });
  expect(created.goal_id).toBeUndefined();
  await dialog.getByRole("button", { name: "暂停续行", exact: true }).click();
  await expect(dialog).toContainText("已暂停");
  await dialog.getByRole("button", { name: "编辑目标", exact: true }).click();
  await dialog.getByLabel("目标内容").fill("保留目标，增加轮次");
  await dialog.getByRole("button", { name: /轮次上限/ }).click();
  await dialog.getByRole("button", { name: "128", exact: true }).click();
  await dialog.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(dialog.getByRole("progressbar")).toHaveAttribute("max", "128");
  expect(relay.commands.find(c => c.type === "act_dsh_goal" && c.action === "edit"))
    .toMatchObject({ goal_id: "created-goal", revision: 2, max_rounds: 128 });
  await expect(dialog).toContainText("已暂停");
  await dialog.getByRole("button", { name: "继续目标", exact: true }).click();
  await expect(dialog).toContainText("自动续行");
  await dialog.getByRole("button", { name: "更多目标操作", exact: true }).click();
  await dialog.getByRole("button", { name: "清除目标", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator(".dsh-goal")).toHaveCount(0);
});

test("DSH Goal editor keeps its round cap and draft through a mobile keyboard cycle", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const relay = await openDsh(page);
  relay.emit({ ...nativeState, goal: null });
  await page.locator(".composer textarea").fill("/goal");
  await page.locator(".composer textarea").press("Enter");
  const dialog = page.getByRole("dialog", { name: "DSH Goal" });
  const editor = dialog.getByLabel("目标内容");
  await editor.fill("完成手机端 Goal 回归。");
  const fullHeight = (await dialog.boundingBox())!.height;
  await page.evaluate(() => {
    Object.defineProperties(window.visualViewport!, {
      height: { configurable: true, value: 300 },
      offsetTop: { configurable: true, value: 24 },
    });
    window.visualViewport!.dispatchEvent(new Event("resize"));
  });
  await expect.poll(async () => (await dialog.locator(".goal-sheet-scroll").boundingBox())!.height).toBeGreaterThan(80);
  await expect.poll(() => dialog.evaluate(node => {
    const box = node.getBoundingClientRect();
    const viewport = window.visualViewport!;
    return box.top >= viewport.offsetTop - 1
      && box.bottom <= viewport.offsetTop + viewport.height + 1;
  })).toBe(true);
  await expect(editor).toBeFocused();
  await expect(dialog.getByRole("button", { name: /轮次上限 · 256/ })).toBeInViewport();
  await expect(dialog.getByRole("button", { name: "开始目标", exact: true })).toBeInViewport();
  await editor.blur();
  await page.evaluate(() => {
    Object.defineProperties(window.visualViewport!, {
      height: { configurable: true, value: window.innerHeight },
      offsetTop: { configurable: true, value: 0 },
    });
    window.visualViewport!.dispatchEvent(new Event("resize"));
  });
  await expect.poll(async () => Math.abs((await dialog.boundingBox())!.height - fullHeight)).toBeLessThan(2);
  await expect(editor).toHaveValue("完成手机端 Goal 回归。");
  await page.screenshot({ path: info.outputPath("dsh-goal-keyboard-restored.png") });
  expect(relay.commands.some(command => ["query", "act_dsh_goal", "set_dsh_control"].includes(String(command.type)))).toBe(false);
});

test("DSH Goal live updates preserve edit and its original revision; exhausted goals require a higher cap", async ({ page }) => {
  const relay = await openDsh(page);
  await page.getByRole("button", { name: /查看 DSH Goal/ }).click();
  const dialog = page.getByRole("dialog", { name: "DSH Goal" });
  await dialog.getByRole("button", { name: "编辑目标", exact: true }).click();
  await dialog.getByLabel("目标内容").fill("不能被实时更新覆盖");
  relay.emit({ ...nativeState, goal: { ...nativeState.goal, revision: 3, rounds: 32, phase: "blocked", activation: "disarmed" } });
  await expect(dialog.getByLabel("目标内容")).toHaveValue("不能被实时更新覆盖");
  await dialog.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("目标已更新");
  expect(relay.commands.find(c => c.type === "act_dsh_goal")).toMatchObject({ goal_id: "native-goal", revision: 2 });
  await expect(dialog.getByLabel("目标内容")).toHaveValue("不能被实时更新覆盖");
  await dialog.getByRole("button", { name: "保留输入，使用最新版本", exact: true }).click();
  expect(relay.commands.filter(c => c.type === "act_dsh_goal")).toHaveLength(1);
  await expect(dialog.getByLabel("目标内容")).toHaveValue("不能被实时更新覆盖");
  await dialog.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("目标已更新");
  expect(relay.commands.filter(c => c.type === "act_dsh_goal").at(-1))
    .toMatchObject({ goal_id: "native-goal", revision: 3, objective: "不能被实时更新覆盖" });
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await expect(dialog.getByRole("button", { name: "继续目标", exact: true })).toBeDisabled();
  await expect(dialog).toContainText("可编辑上限后继续");
});

test("DSH white blue defaults and current new-chat controls stay independent", async ({ page }, info) => {
  const relay = await openDsh(page);
  await page.evaluate(() => document.documentElement.setAttribute("data-theme", "light"));
  const colors = await page.evaluate(() => {
    const style = getComputedStyle(document.documentElement);
    return ["--bg", "--accent", "--text"].map(key => style.getPropertyValue(key).trim().toUpperCase());
  });
  expect(colors).toEqual(["#FBFCFE", "#2E6FF0", "#111726"]);
  await page.screenshot({path: info.outputPath("dsh-white-blue-chat.png"), animations: "disabled"});
  await page.locator(".surface-head-title").click();
  await page.getByRole("button", { name: "新会话", exact: true }).click();
  await expect(page.locator(".newchat .engine-brand-icon.dsh")).toBeVisible();
  await expect(page.locator(".newchat-profile").filter({hasText:"default"})).toHaveCount(0);
  await page.getByRole("button", {name:"默认权限", exact:true}).click();
  await expect(page.locator(".permission-sheet .permission-options")).toHaveCSS("display", "grid");
  await expect(page.locator(".permission-sheet .cmd")).toHaveCount(4);
  await expect(page.locator(".permission-sheet .cmd.sel")).toContainText("默认权限");
  await page.screenshot({ path: info.outputPath("dsh-new-chat-permissions.png"), animations: "disabled" });
  await page.getByRole("dialog", {name:"选择权限模式"}).getByRole("button", {name:/工作区写入/}).click();
  await page.locator(".newchat textarea").fill("/pla");
  await expect(page.locator(".newchat .cmd-pop")).toContainText("/plan");
  await page.locator(".newchat .cmd-pop").getByRole("button", {name:/\/plan/}).click();
  await expect(page.locator(".newchat textarea")).toHaveValue("/plan ");
  await page.screenshot({path: info.outputPath("dsh-white-blue-new-chat.png"), animations:"disabled"});
  await page.locator(".newchat").getByRole("button", {name:"开始",exact:true}).click();
  await expect.poll(() => relay.commands.some(c => c.type === "new_session" && c.prompt === "/plan" && c.dsh_permission === "workspace-write" && c.dsh_agent_preset === "standard")).toBe(true);
});

test("DSH new chat retains and submits full access from its native permission catalog", async ({ page }, info) => {
  const relay = await openDsh(page);
  await page.locator(".surface-head-title").click();
  await page.getByRole("button", { name: "新会话", exact: true }).click();
  await page.getByRole("button", { name: "默认权限", exact: true }).click();
  await page.getByRole("dialog", { name: "选择权限模式" }).getByRole("button", { name: /完全访问/ }).click();
  await expect(page.locator(".newchat-access")).toHaveText("完全访问");
  await page.locator(".newchat-access").click();
  const card = page.locator(".permission-sheet .cmd.danger.sel");
  await expect(card).toContainText("完全访问");
  await expect(card.locator(".cmd-check")).toBeVisible();
  const boxes = await page.locator(".permission-sheet .cmd").evaluateAll(cards => cards.map(card => {
    const { x, y, width, height } = card.getBoundingClientRect();
    return { x, y, width, height };
  }));
  expect(boxes).toHaveLength(4);
  expect(boxes[0].x).toBeCloseTo(boxes[2].x, 0);
  expect(boxes[1].x).toBeCloseTo(boxes[3].x, 0);
  expect(boxes[0].y).toBeCloseTo(boxes[1].y, 0);
  expect(boxes[2].y).toBeCloseTo(boxes[3].y, 0);
  expect(boxes[2].y).toBeGreaterThan(boxes[0].y);
  for (const box of boxes) {
    expect(box.width).toBeCloseTo(boxes[0].width, 0);
    expect(box.height).toBeCloseTo(boxes[0].height, 0);
  }
  await page.screenshot({ path: info.outputPath("dsh-full-access-permissions.png"), animations: "disabled" });
  await page.getByRole("button", { name: "关闭选择权限模式" }).click();
  await page.locator(".newchat textarea").fill("验证最高权限");
  await page.locator(".newchat").getByRole("button", { name: "开始", exact: true }).click();
  await expect.poll(() => relay.commands.some(c => c.type === "new_session" && c.dsh_permission === "danger-full-access")).toBe(true);
});

test("DSH appearance editor uses its blue defaults in both modes and shortcuts reach all engines", async ({ page }, info) => {
  const relay = await openDsh(page);
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  await page.getByRole("button", { name: /^外观/ }).click();
  const dialog = page.getByRole("dialog", { name: "外观", exact: true });
  await expect(dialog.locator(".engine-brand-icon.dsh")).toBeVisible();
  for (const [mode, label, accent, ink] of [
    ["light", "浅色", "rgb(46, 111, 240)", "rgb(27, 84, 209)"],
    ["dark", "深色", "rgb(107, 165, 255)", "rgb(148, 189, 255)"],
  ]) {
    await dialog.getByRole("button", { name: label, exact: true }).click();
    await expect(dialog.locator(".appearance-done")).toHaveCSS("background-color", accent);
    await expect(dialog.locator(".appearance-reset")).toHaveCSS("color", ink);
    await expect(dialog.locator(".theme-picker-intro > span")).toHaveCSS("color", ink);
    await expect(dialog.locator(`.appearance-miniature.${mode}`)).toHaveCSS("outline-color", accent);
    expect(await dialog.locator(".theme-picker-scroll").evaluate(node => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
    await expect(dialog.locator(".appearance-done")).toBeInViewport();
    await page.screenshot({ path: info.outputPath(`dsh-appearance-${mode}.png`), animations: "disabled" });
  }
  // Capture shortcuts work even inside an open sheet, without submitting work.
  await page.keyboard.press("Meta+2");
  await expect(page.locator(".engine-label")).toHaveText("Codex");
  await page.keyboard.press("Meta+3");
  await expect(page.locator(".engine-label")).toHaveText("DSH");
  await page.keyboard.press("Control+1");
  await expect(page.locator(".engine-label")).toHaveText("Claude");
  await page.keyboard.press("Control+3");
  await expect(page.locator(".engine-label")).toHaveText("DSH");
  expect(relay.commands.some(c => c.type === "query" || c.type === "new_session")).toBe(false);
});

test("DSH management preserves native revisions and only stages creation prompts", async ({ page }, info) => {
  const relay = await openDsh(page, { features: true, commandSuccess: true });
  await openDshTools(page);
  const panel = page.getByLabel("DSH 会话工具", { exact: true });
  await selectDshTool(page, "运行设置");
  await panel.getByRole("spinbutton", { name: "并发子代理" }).fill("4");
  await panel.getByRole("button", { name: "保存修改" }).click();
  await expect.poll(() => relay.commands.filter(command => command.type === "set_dsh_control" && command.kind === "manage").length).toBe(1);
  expect(JSON.parse(String(relay.commands.find(command => command.kind === "manage")?.value))).toEqual({ action: "settings", ns: "subagent", revision: 4, patch: { maxActiveSubagents: 4 } });
  await selectDshTool(page, "自动化");
  await panel.getByRole("button", { name: "每周审查的操作", exact: true }).click();
  await panel.getByRole("button", { name: "执行记录", exact: true }).click();
  await expect(panel.locator(".dsh-deliveries")).toContainText("已送达");
  await panel.getByRole("button", { name: "每周审查的操作", exact: true }).click();
  await panel.getByRole("button", { name: "编辑任务", exact: true }).click();
  await panel.getByRole("textbox", { name: "任务名称" }).fill("每日审查");
  await panel.getByRole("button", { name: "保存修改" }).click();
  await expect.poll(() => relay.commands.filter(command => command.kind === "manage").length).toBe(2);
  const update = JSON.parse(String(relay.commands.filter(command => command.kind === "manage").at(-1)?.value));
  expect(update).toMatchObject({ action: "schedule_update", id: "review", expectedUpdatedAt: "rev1", patch: { title: "每日审查" } });
  await page.screenshot({ path: info.outputPath("dsh-automation.png") });
  await selectDshTool(page, "预设");
  await panel.getByRole("button", { name: "创建预设", exact: true }).click();
  await expect(page.locator(".newchat textarea")).toHaveValue(/创建一个 DSH 代理预设/);
  expect(relay.commands.some(command => command.type === "new_session" || command.type === "query")).toBe(false);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
});

test("DSH workflow opens exact children and nested work without a model request", async ({ page }, info) => {
  const relay = await openDsh(page, { features: true });
  await openDshTools(page);
  const panel = page.getByLabel("DSH 会话工具", { exact: true });
  await selectDshTool(page, "工作流");
  await panel.getByRole("button", { name: /检查构建.*运行中/ }).click();
  const agent = page.getByLabel("协作代理详情", { exact: true });
  await expect(agent).toContainText("检查构建并验证回归");
  await expect(agent.locator(".dsh-tool-children > summary")).toHaveText("1 个内部调用");
  await agent.locator(".dsh-tool-children > summary").click();
  await agent.locator(".dsh-tool-children .tool-group-h").click();
  await expect(agent.locator(".dsh-tool-children")).toContainText("README.md");
  await expect(agent.locator(".process-agent-card .process-item-status")).toHaveCount(0);
  await agent.getByRole("button", { name: /回归测试/ }).click();
  await expect(agent.locator(".agent-detail-heading strong")).toHaveText("回归测试");
  await agent.getByRole("button", { name: "返回上一级协作代理" }).click();
  await expect(agent.locator(".agent-detail-heading strong")).toHaveText("检查构建");
  expect(relay.commands.filter(command => command.type === "get_agent_detail").map(command => command.run_id)).toEqual(["dsh@child", "dsh@grandchild"]);
  expect(relay.commands.some(command => command.type === "query" || command.type === "act_dsh_subagent")).toBe(false);
  await page.screenshot({ path: info.outputPath("dsh-agent-process.png") });
});

test("DSH terminal attaches existing PTY, sends explicit input and only detaches on close", async ({ page }, info) => {
  const relay = await openDsh(page, { features: true, commandSuccess: true });
  await openDshTools(page);
  const panel = page.getByLabel("DSH 会话工具", { exact: true });
  await selectDshTool(page, "终端");
  await panel.getByRole("button", { name: "选择终端", exact: true }).click();
  await page.getByRole("dialog", { name: "选择终端", exact: true }).getByRole("button", { name: /zsh/ }).click();
  await expect(panel.locator(".dsh-terminal-status").getByText("已连接", { exact: true })).toBeVisible();
  const input = panel.locator(".xterm-helper-textarea");
  await input.press("a");
  await expect.poll(() => relay.commands.filter(command => command.kind === "terminal").map(command => JSON.parse(String(command.value))).some(value => value.action === "input" && value.value.type === "write" && value.value.data === "a")).toBe(true);
  await page.screenshot({ path: info.outputPath("dsh-terminal.png") });
  await panel.getByRole("button", { name: "关闭会话工具" }).click();
  await expect.poll(() => relay.commands.filter(command => command.kind === "terminal").map(command => JSON.parse(String(command.value))).some(value => value.action === "detach")).toBe(true);
  expect(relay.commands.filter(command => command.kind === "terminal").map(command => JSON.parse(String(command.value))).some(value => value.action === "close" || value.action === "create")).toBe(false);
});

test("DSH return links decorate the native autonomous round without splitting the answer", async ({ page }, info) => {
  const relay = await openDsh(page, { features: true });
  const blocks = [
    { kind: "process", item_id: "dsh-return-child", processKind: "agent", phase: "end", status: "succeeded", done: true,
      title: "界面审查", server: "cc_remote", tool: "agent_return", input: { agent_run_id: "dsh@child" } },
    { kind: "text", message_id: "after-return", text: "收到审查结果，继续验证。", channel: "final", done: true },
  ];
  const history = (continuation: string | null) => relay.emit({ type: "history", sid, session_id: sid,
    generation: "dsh-generation", revision: "dsh-history", detail: "summary", events: [], has_more: false,
    turns: [{ id: "native-prompt", prompt: "", continuation, done: true, ts: 1794130061465, blocks }] });
  relay.emit({ type: "background_process_sync", sid, generation: "dsh-generation", items: [] });
  history(null);
  await expect(page.locator(".agent-return-link")).toHaveCount(0);
  history("subagent");
  const receipt = page.getByRole("button", { name: "代理返回 · 界面审查" });
  await expect(receipt).toBeVisible();
  await expect(page.locator(".background-followup")).toHaveCount(0);
  await expect(page.locator(".turn-continuation")).toHaveCount(1);
  const boundary = page.locator(".turn-continuation");
  await expect(boundary).toHaveClass(/background-followup-boundary/);
  await expect(boundary).toContainText("DeepSeek 继续处理");
  await expect(boundary.locator("time")).toHaveText(/2026-11-08/);
  await page.screenshot({ path: info.outputPath("dsh-agent-return.png") });
  await receipt.click();
  await expect(page.getByLabel("协作代理详情", { exact: true })).toContainText("检查构建并验证回归");
  expect(relay.commands.some(command => command.type === "get_agent_detail" && command.run_id === "dsh@child")).toBe(true);
  expect(relay.commands.some(command => command.type === "query" || command.type === "act_dsh_subagent")).toBe(false);
});

test("DSH terminal interrupt keeps shell and explicit close does not surface a racing stream error", async ({ page }, info) => {
  const relay = await openDsh(page, { features: true, commandSuccess: true });
  await openDshTools(page);
  const panel = page.getByLabel("DSH 会话工具", { exact: true });
  await selectDshTool(page, "终端");
  await panel.getByRole("button", { name: "选择终端", exact: true }).click();
  await page.getByRole("dialog", { name: "选择终端", exact: true }).getByRole("button", { name: /zsh/ }).click();
  await expect(panel.getByRole("button", { name: "中断命令 Ctrl+C" })).toBeEnabled();
  await panel.getByRole("button", { name: "中断命令 Ctrl+C" }).click();
  await expect.poll(() => relay.commands.filter(c => c.kind === "terminal").map(c => JSON.parse(String(c.value))).some(v => v.action === "input" && v.value.data === "\u0003")).toBe(true);
  expect(relay.commands.filter(c => c.kind === "terminal").map(c => JSON.parse(String(c.value))).some(v => v.action === "close")).toBe(false);
  await panel.getByRole("button", { name: "关闭终端", exact: true }).click();
  await page.screenshot({ path: info.outputPath("dsh-terminal-close.png") });
  await page.getByRole("dialog", { name: "关闭终端？" }).getByRole("button", { name: "确认关闭", exact: true }).click();
  await expect(panel.getByText("选择终端或新建一个终端", { exact: false })).toBeVisible();
  await expect(panel.getByRole("alert")).toHaveCount(0);
});

test("DSH bundle catalog filters locally and reads configured sources", async ({ page }, info) => {
  const relay = await openDsh(page, { features: true, commandSuccess: true });
  await page.screenshot({ path: info.outputPath("dsh-session-mode.png") });
  await openDshTools(page);
  const panel = page.getByLabel("DSH 会话工具", { exact: true });
  await selectDshTool(page, "运行设置");
  await expect(panel.getByRole("spinbutton", { name: "最大层级" })).toHaveValue("1");
  await page.screenshot({ path: info.outputPath("dsh-runtime-settings.png") });
  await selectDshTool(page, "插件");
  await expect(panel.getByText("示例插件", { exact: true })).toBeVisible();
  await panel.getByRole("button", { name: "本机插件", exact: true }).click();
  await expect(panel.getByText("示例插件", { exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath("dsh-plugins-all.png") });
  await panel.getByRole("textbox", { name: "搜索插件" }).fill("not-there");
  await expect(panel.getByText("没有匹配的插件")).toBeVisible();
  await panel.getByRole("textbox", { name: "搜索插件" }).fill("example");
  await expect(panel.getByText("示例插件", { exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath("dsh-plugins.png") });
  await panel.getByRole("button", { name: "插件源", exact: true }).click();
  await expect(panel.getByText("来自这台设备在 DSH 中配置的插件源。", { exact: true })).toBeVisible();
  await expect(panel.getByRole("textbox", { name: "搜索插件" })).toBeVisible();
  await expect(panel.getByRole("alert")).toHaveCount(0);
  await expect.poll(() => relay.commands.some(c => c.type === "read_dsh" && c.kind === "bundles" && c.query)).toBe(true);
  await page.screenshot({ path: info.outputPath("dsh-market-unavailable.png") });
  await panel.getByRole("button", { name: "本机插件", exact: true }).click();
  await expect(panel.getByRole("textbox", { name: "搜索插件" })).toHaveValue("example");
  await expect(panel.getByText("示例插件", { exact: true })).toBeVisible();
  expect(relay.commands.some(c => c.type === "query" || c.type === "new_session")).toBe(false);
});

test("DSH settings retain failed drafts after a partially successful save", async ({ page }) => {
  const relay = await openDsh(page, { features: true, commandSuccess: true, failSettingsNamespace: "agent-loop" });
  await openDshTools(page);
  await selectDshTool(page, "运行设置");
  const panel = page.getByLabel("DSH 会话工具", { exact: true });
  await panel.getByRole("spinbutton", { name: "并发子代理" }).fill("5");
  await panel.getByRole("spinbutton", { name: "并行调用上限" }).fill("4");
  await expect(panel.getByText("已修改 2 项")).toBeVisible();
  await panel.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(panel.getByRole("status").filter({ hasText: "已保存 1 项" })).toContainText("未确认的修改已保留");
  await expect(panel.getByRole("spinbutton", { name: "并发子代理" })).toHaveValue("5");
  await expect(panel.getByRole("spinbutton", { name: "并行调用上限" })).toHaveValue("4");
  await expect(panel.getByText("已修改 1 项")).toBeVisible();
  const writes = () => relay.commands.filter(c => c.kind === "manage").map(c => JSON.parse(String(c.value)));
  expect(writes().map(body => [body.ns, body.revision])).toEqual([["subagent", 4], ["agent-loop", 7]]);
  await panel.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect.poll(() => writes().length).toBe(3);
  expect(writes().filter(body => body.ns === "subagent")).toHaveLength(1);
  await panel.getByRole("button", { name: "撤销", exact: true }).click();
  await expect(panel.getByRole("spinbutton", { name: "并行调用上限" })).toHaveValue("10");
});

test("DSH compact tools menu supports keyboard dismissal and shipped plugin controls", async ({ page }, info) => {
  await page.emulateMedia({ colorScheme: "dark" });
  const relay = await openDsh(page, { features: true, commandSuccess: true });
  await openDshTools(page);
  const panel = page.getByLabel("DSH 会话工具", { exact: true });
  const trigger = panel.getByRole("button", { name: "工具", exact: true });
  await trigger.click();
  await expect(panel.getByRole("menuitem", { name: "子代理", exact: true })).toBeFocused();
  await page.keyboard.press("End");
  await expect(panel.getByRole("menuitem", { name: "连接诊断", exact: true })).toBeFocused();
  await page.screenshot({ path: info.outputPath("dsh-tools-menu.png") });
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
  await expect(panel.getByRole("menu")).toHaveCount(0);
  await expect(panel).toBeVisible();
  await selectDshTool(page, "插件");
  const core = panel.locator(".dsh-bundle-row").filter({ hasText: "DSH 核心组件" });
  await expect(core.getByRole("button")).toHaveCount(0);
  const optional = panel.locator(".dsh-bundle-row").filter({ hasText: "会话标题" });
  await page.screenshot({ path: info.outputPath("dsh-plugins-dark.png") });
  await optional.getByRole("button", { name: "启用", exact: true }).click();
  await expect.poll(() => relay.commands.some(c => c.kind === "manage" && JSON.parse(String(c.value)).action === "bundle_enable")).toBe(true);
  expect(relay.commands.some(c => c.type === "query" || c.type === "new_session")).toBe(false);
});

test("DSH terminal streams and preserves the last row after centering", async ({ page }, info) => {
  const relay = await openDsh(page, { features: true, commandSuccess: true });
  await openDshTools(page);
  const panel = page.getByLabel("DSH 会话工具", { exact: true });
  await selectDshTool(page, "终端");
  await panel.getByRole("button", { name: "选择终端", exact: true }).click();
  const picker = page.getByRole("dialog", { name: "选择终端", exact: true });
  await expect(picker.locator("select")).toHaveCount(0);
  await picker.getByRole("button", { name: /zsh/ }).click();
  await expect(panel.locator(".dsh-terminal-status").getByText("已连接", { exact: true })).toBeVisible();
  await panel.locator(".panel-expand").click();
  await expect(panel).toHaveClass(/panel-expanded/);
  await expect.poll(async () => panel.evaluate(element => {
    const viewport = element.querySelector(".dsh-terminal-viewport")!.getBoundingClientRect();
    const screen = element.querySelector(".xterm-screen")!.getBoundingClientRect();
    return screen.bottom <= viewport.bottom + 1 && screen.top >= viewport.top - 1;
  })).toBe(true);
  await panel.locator(".xterm-helper-textarea").press("a");
  await panel.locator(".xterm-helper-textarea").press("b");
  const writes = () => relay.commands.filter(c => c.kind === "terminal").map(c => JSON.parse(String(c.value))).filter(c => c.action === "input" && c.value.type === "write");
  await expect.poll(() => writes().map(c => c.value.data).join("")).toBe("ab");
  expect(relay.commands.some(c => c.type === "read_dsh" && c.kind === "terminal_output")).toBe(false);
  await page.screenshot({ path: info.outputPath("terminal-centered.png") });
});

test("DSH terminal scrollback supports wheel and touch without sending shell input", async ({ page }, info) => {
  const relay = await openDsh(page, { features: true, commandSuccess: true });
  await openDshTools(page); await selectDshTool(page, "终端");
  const panel = page.getByLabel("DSH 会话工具", { exact: true });
  await panel.getByRole("button", { name: "选择终端", exact: true }).click();
  await page.getByRole("dialog", { name: "选择终端", exact: true }).getByRole("button", { name: /zsh/ }).click();
  await expect(panel.locator(".dsh-terminal-status").getByText("已连接", { exact: true })).toBeVisible();
  const attach = relay.commands.filter(c => c.kind === "terminal").map(c => JSON.parse(String(c.value))).find(c => c.action === "attach");
  const output = (next_seq: number, data: string) => relay.emit({ type: "dsh_read_result", sid,
    request_id: attach.lease, kind: "terminal_output", next_seq,
    items: [{ id: String(next_seq), title: "output", data: { type: "output", data } }] });
  output(2, "\r\n" + Array.from({ length: 180 }, (_, n) => `history-${String(n).padStart(3, "0")}\r\n`).join(""));
  const rows = panel.locator(".xterm-rows");
  await expect(rows).toContainText("history-179");
  const bottom = (await rows.textContent())!;
  const screen = panel.locator(".xterm-screen");
  if (info.project.name === "desktop") {
    await screen.hover(); await page.mouse.wheel(0, -280);
    await expect(rows).not.toHaveText(bottom);
    await page.mouse.wheel(0, 10000);
    await expect(rows).toContainText("history-179");
  }
  const drag = (dy: number) => screen.evaluate((element, dy) => {
    const rect = element.getBoundingClientRect();
    const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
    const send = (type: string, offset: number) => {
      const event = new Event(type, { bubbles: true, cancelable: true });
      const touches = type === "touchend" ? [] : [{ identifier: 42, clientX: x, clientY: y + offset }];
      Object.defineProperty(event, "touches", { value: touches });
      element.dispatchEvent(event);
    };
    send("touchstart", 0);
    for (let step = 1; step <= 8; step++) send("touchmove", dy * step / 8);
    send("touchend", dy);
  }, dy);
  await drag(160);
  await expect(rows).not.toHaveText(bottom);
  const scrolled = (await rows.textContent())!;
  output(3, "new-output-while-reading\r\n");
  await expect.poll(() => relay.commands.filter(c => c.kind === "terminal").map(c => JSON.parse(String(c.value))).some(c => c.action === "ack" && c.cursor === 3)).toBe(true);
  await expect(rows).toHaveText(scrolled);
  await drag(-240);
  await expect(rows).toContainText("new-output-while-reading");
  expect(relay.commands.filter(c => c.kind === "terminal").map(c => JSON.parse(String(c.value)))
    .some(c => c.action === "input" && c.value.type === "write")).toBe(false);
});

test("DSH schedule form creates directly in the selected session", async ({ page }, info) => {
  await page.emulateMedia({ colorScheme: "dark" });
  const relay = await openDsh(page, { features: true, commandSuccess: true });
  await openDshTools(page); await selectDshTool(page, "自动化");
  const panel = page.getByLabel("DSH 会话工具", { exact: true });
  await panel.getByRole("button", { name: "新建任务", exact: true }).click();
  await panel.getByLabel("任务名称", { exact: true }).fill("每日检查");
  await panel.getByLabel("任务内容", { exact: true }).fill("总结项目的新增改动");
  await panel.getByLabel("时区", { exact: true }).fill("Asia/Taipei");
  await page.screenshot({ path: info.outputPath("schedule-create.png") });
  await panel.getByRole("button", { name: "创建任务", exact: true }).click();
  await expect.poll(() => relay.commands.filter(c => c.kind === "manage").map(c => JSON.parse(String(c.value))).find(c => c.action === "schedule_create")).toMatchObject({
    request: { title: "每日检查", prompt: "总结项目的新增改动", target: { kind: "session", sid }, timing: { kind: "daily", daily: { time: "09:00:00", time_zone: "Asia/Taipei" } } },
  });
  expect(relay.commands.some(c => c.type === "query")).toBe(false);
});

test("DSH side chat entry points stay disabled without changing drafts or creating sessions", async ({ page }, info) => {
  const relay = await openDsh(page, { features: true, commandSuccess: true });
  const composer = page.locator(".composer textarea");
  await composer.fill("原来的草稿");
  await page.getByRole("button", { name: "更多设置", exact: true }).click();
  await expect(page.getByRole("button", { name: "侧边聊天暂未开放", exact: true })).toBeDisabled();
  await expect(page.getByText("等待官方提供会话删除能力后再接入", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "打开侧边聊天", exact: true })).toHaveCount(0);
  await page.screenshot({ path: info.outputPath("sidechat-disabled.png") });
  await page.keyboard.press("Escape");
  for (const key of ["Meta+Shift+k", "Control+Shift+k", "Meta+Shift+e", "Control+Shift+e"]) {
    await page.keyboard.press(key);
    await expect(page.getByLabel("侧边对话", { exact: true })).toHaveCount(0);
  }
  await expect(composer).toHaveValue("原来的草稿");
  // An old private catalog must not reopen a dormant side panel.
  relay.emit({ type: "btw_sync", generation: "dsh-generation", revision: 1, sessions: [
    { btw_sid: "dsh@side", parent_sid: sid, engine: "dsh", created_at: 1, state: "idle" },
  ] });
  await expect(page.getByLabel("侧边对话", { exact: true })).toHaveCount(0);
  await composer.fill("/btw");
  await expect(page.getByRole("button", { name: /侧边对话/ })).toHaveCount(0);
  await composer.fill("/btw 只在旁聊解释一下");
  await composer.press("Enter");
  await expect(page.getByText("DSH 侧聊暂未开放，等待官方提供会话删除能力后再接入。", { exact: true })).toBeVisible();
  await expect(composer).toHaveValue("/btw 只在旁聊解释一下");
  await page.locator(".surface-head-title").click();
  await page.getByRole("button", { name: "新会话", exact: true }).click();
  const newComposer = page.locator(".newchat textarea");
  await newComposer.fill("/btw 新会话也不能创建侧聊");
  await newComposer.press("Enter");
  await expect(page.getByText("DSH 侧聊暂未开放，等待官方提供会话删除能力后再接入。", { exact: true })).toBeVisible();
  await expect(newComposer).toHaveValue("/btw 新会话也不能创建侧聊");
  expect(relay.commands.some(c => ["open_btw", "new_session", "query", "close_btw"].includes(String(c.type)))).toBe(false);
  expect(relay.commands.some(c => c.type === "set_dsh_control" && c.kind === "command")).toBe(false);
  expect(relay.commands.some(c => c.type === "sync_btw" && c.sid === "dsh@side")).toBe(false);
});

for (const target of ["new_once", "new"]) test(`DSH schedule destination ${target} creates directly in selected project`, async ({ page }, info) => {
  await page.emulateMedia({ colorScheme: "dark" });
  const relay = await openDsh(page, { features: true, commandSuccess: true });
  await openDshTools(page); await selectDshTool(page, "自动化");
  const panel = page.getByLabel("DSH 会话工具", { exact: true });
  await panel.getByRole("button", { name: "新建任务", exact: true }).click();
  await panel.getByLabel("任务名称", { exact: true }).fill("每日检查");
  await panel.getByLabel("任务内容", { exact: true }).fill("检查构建结果");
  await panel.getByRole("radio", { name: target === "new" ? /每次新建会话/ : /首次新建，之后复用/ }).check();
  await panel.getByLabel("项目目录", { exact: false }).fill("/tmp/dsh-test");
  await page.screenshot({ path: info.outputPath(`schedule-${target}.png`) });
  await panel.getByRole("button", { name: "创建任务", exact: true }).click();
  await expect.poll(() => relay.commands.filter(c => c.kind === "manage").map(c => JSON.parse(String(c.value))).find(c => c.action === "schedule_create")).toMatchObject({
    request: { target: { kind: target, create: { cwd: "/tmp/dsh-test" } }, timing: { kind: "daily" } },
  });
  expect(relay.commands.some(c => ["query", "new_session"].includes(String(c.type)))).toBe(false);
});

test("DSH schedule actions use revision and rejected edits retain draft", async ({ page }) => {
  const relay = await openDsh(page, { features: true });
  await openDshTools(page); await selectDshTool(page, "自动化");
  const panel = page.getByLabel("DSH 会话工具", { exact: true });
  await panel.getByRole("button", { name: "暂停", exact: true }).click();
  await expect.poll(() => relay.commands.filter(c => c.kind === "manage").map(c => JSON.parse(String(c.value)))[0]).toMatchObject({ action: "schedule_pause", id: "review", expectedUpdatedAt: "rev1" });
  await panel.getByRole("button", { name: "立即运行", exact: true }).click();
  await expect.poll(() => relay.commands.filter(c => c.kind === "manage").map(c => JSON.parse(String(c.value))).some(c => c.action === "schedule_run")).toBe(true);
  await panel.getByRole("button", { name: "每周审查的操作" }).click();
  await panel.getByRole("button", { name: "编辑任务", exact: true }).click();
  await panel.getByLabel("任务内容", { exact: true }).fill("保留我的修改");
  await panel.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(panel.getByLabel("任务内容", { exact: true })).toHaveValue("保留我的修改");
  await expect.poll(() => relay.commands.filter(c => c.kind === "manage").map(c => JSON.parse(String(c.value))).find(c => c.action === "schedule_update")).toMatchObject({
    expectedUpdatedAt: "rev1", patch: { prompt: "保留我的修改" },
  });
});

test("DSH plugin updates select compatible versions and keep selection after refusal", async ({ page }, info) => {
  await page.emulateMedia({ colorScheme: "dark" });
  const relay = await openDsh(page, { features: true });
  await openDshTools(page); await selectDshTool(page, "插件");
  const row = page.locator(".dsh-bundle-row").filter({ hasText: "示例插件" });
  await row.locator("summary").click();
  await row.getByRole("button", { name: "检查更新", exact: true }).click();
  await expect(row.getByText("1.0.0 → 1.1.0", { exact: true })).toBeVisible();
  await expect(row.getByText("不兼容的版本不可选择。", { exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath("plugin-version-picker.png") });
  await row.getByRole("button", { name: "更新到所选版本", exact: true }).click();
  await expect.poll(() => relay.commands.filter(c => c.kind === "manage").map(c => JSON.parse(String(c.value))).find(c => c.action === "bundle_update")).toMatchObject({ name: "@example/plugin", version: "1.1.0" });
  await expect(row.getByRole("button", { name: "更新到所选版本", exact: true })).toBeVisible();
  expect(relay.commands.filter(c => c.kind === "manage")).toHaveLength(1);
});

test("DSH product statistics and centered cleanup preserve the real shell", async ({ page }, info) => {
  await page.emulateMedia({ colorScheme: "dark" });
  const relay = await openDsh(page, { features: true, product: true, commandSuccess: true });
  await openDshTools(page); await selectDshTool(page, "性能统计");
  const panel = page.getByLabel("DSH 会话工具", { exact: true });
  await expect(panel.getByText("18.6 s", { exact: true })).toBeVisible();
  await expect(panel.getByText("84.0%", { exact: true })).toBeVisible();
  await panel.getByRole("button", { name: "步骤", exact: true }).click();
  await expect(panel.getByRole("cell", { name: "24,100", exact: true })).toBeVisible();
  await panel.getByRole("button", { name: "概览", exact: true }).click();
  await page.screenshot({ path: info.outputPath("dsh-product-stats.png") });
  await selectDshTool(page, "自动化");
  await panel.getByRole("button", { name: "自动化更多操作" }).click();
  await panel.getByRole("menuitem", { name: "清理已结束任务…" }).click();
  const dialog = page.getByRole("dialog", { name: "清理已结束任务？" });
  await expect(dialog.getByRole("button", { name: "清理 8 项" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "清理已结束任务…" })).toHaveCount(0);
  const box = await dialog.boundingBox();
  expect(Math.abs(box!.x + box!.width / 2 - page.viewportSize()!.width / 2)).toBeLessThan(3);
  await page.screenshot({ path: info.outputPath("dsh-product-cleanup.png") });
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  expect(relay.commands.filter(c => c.kind === "manage")).toHaveLength(0);
  await panel.getByRole("button", { name: "自动化更多操作" }).click();
  await panel.getByRole("menuitem", { name: "清理已结束任务…" }).click();
  await dialog.getByRole("button", { name: "清理 8 项" }).click();
  await expect(dialog.getByRole("button", { name: "关闭", exact: true })).toBeVisible();
  const writes = relay.commands.filter(c => c.kind === "manage").map(c => JSON.parse(String(c.value)));
  expect(writes).toEqual([{ action: "schedule_cleanup", revision: "cleanup-1" }]);
  expect(relay.commands.some(c => c.type === "query" || c.type === "new_session")).toBe(false);
});

test("DSH product plugin detail market and recommendation use native controls", async ({ page }, info) => {
  await page.emulateMedia({ colorScheme: "dark" });
  const relay = await openDsh(page, { features: true, product: true, commandSuccess: true });
  const card = page.getByLabel("插件推荐", { exact: true });
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: "查看插件" }).click();
  const panel = page.getByLabel("DSH 会话工具", { exact: true });
  await expect(panel.getByRole("textbox", { name: "搜索插件" })).toHaveValue("@example/plugin");
  await panel.locator(".dsh-bundle-row summary").click();
  await expect(panel.getByRole("heading", { name: "使用方式" })).toBeVisible();
  await expect(panel.locator(".dsh-bundle-document img")).toHaveCount(0);
  await expect(panel.locator('.dsh-bundle-document a[href^="javascript:"]')).toHaveCount(0);
  await page.screenshot({ path: info.outputPath("dsh-product-plugins.png") });
  await panel.getByRole("button", { name: "插件源", exact: true }).click();
  await expect(panel.getByText("市场插件", { exact: true })).toBeVisible();
  await panel.getByRole("button", { name: "管理插件源 · 1" }).click();
  const sources = page.getByRole("dialog", { name: "管理插件源" });
  await sources.getByRole("textbox", { name: "插件索引地址" }).fill("https://second.test/plugins.json");
  await sources.getByRole("button", { name: "添加来源" }).click();
  await expect(sources.locator(".dsh-source-row").filter({ hasText: "新增插件源" })).toBeVisible();
  await sources.getByRole("button", { name: "关闭", exact: true }).click();
  await panel.getByRole("button", { name: "安装", exact: true }).click();
  await expect.poll(() => relay.commands.filter(c => c.kind === "manage").map(c => JSON.parse(String(c.value))).some(c => c.action === "market_install" && c.source === "source" && c.name === "market-plugin" && !c.spec)).toBe(true);
  await panel.getByRole("button", { name: "关闭会话工具" }).click();
  await card.getByRole("button", { name: "本会话不再提示" }).click();
  await expect(card).toHaveCount(0);
  await expect.poll(() => relay.commands.filter(c => c.kind === "manage").map(c => JSON.parse(String(c.value))).some(c => c.action === "recommendation" && c.verb === "dismiss")).toBe(true);
  expect(relay.commands.some(c => c.type === "query" || c.type === "new_session")).toBe(false);
});
